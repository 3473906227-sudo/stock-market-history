#!/usr/bin/env python3
"""为观察池个股生成真实行情强弱状态（非预测）。

输入均来自 GitHub Actions 已发布的完整收盘日线：
  data/{symbol}.csv                         个股 OHLCV
  indexes/sh000001.csv / sz399001.csv       上证、深证完整收盘日线
  data/sectors/symbols.json                 东方财富官方行业成分映射
  data/sectors/history/<sha256(name)[:20]>.csv 官方行业 OHLCV
输出：data/stock_strength.json

强弱状态是规则化历史行情诊断，不是价格预测、买卖信号或 AI 概率。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
INDEXES = ROOT / "indexes"
SECTOR_DIR = DATA / "sectors"
SECTOR_HISTORY = SECTOR_DIR / "history"
SYMBOL_FILE = ROOT / "stock_symbols.txt"
OUTPUT = DATA / "stock_strength.json"
CN = timezone(timedelta(hours=8))


def read_symbols() -> list[str]:
    if not SYMBOL_FILE.is_file():
        raise FileNotFoundError("stock_symbols.txt 不存在")
    result, seen = [], set()
    for line in SYMBOL_FILE.read_text(encoding="utf-8-sig").splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if re.fullmatch(r"\d{6}", code) and code not in seen:
            seen.add(code)
            result.append(code)
    if not result:
        raise ValueError("股票观察池为空")
    return result


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def sector_digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]


def read_series(path: Path, *, index: bool = False) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path.relative_to(ROOT).as_posix())
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"date", "close"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{path.name} 缺少 date/close 字段")
    keep = ["date", "close"] + (["volume"] if "volume" in frame.columns else [])
    frame = frame[keep].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "volume" in frame:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[(frame["close"] > 0) & frame["date"].str.match(r"^\d{4}-\d{2}-\d{2}$")]
    if index and "session" in pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns:
        raw = pd.read_csv(path, encoding="utf-8-sig", usecols=["date", "close", "session"])
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
        raw["session"] = raw["session"].fillna("legacy").astype(str).str.lower()
        raw = raw[~raw["session"].isin({"intraday", "unknown", "partial", "preclose"})]
        frame = frame[frame["date"].isin(set(raw["date"].dropna()))]
    if "volume" not in frame:
        frame["volume"] = float("nan")
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"{path.name} 没有有效完整收盘日线")
    return frame


def pct(series: pd.Series, days: int) -> float | None:
    if len(series) < days + 1:
        return None
    old, new = float(series.iloc[-days - 1]), float(series.iloc[-1])
    if old <= 0:
        return None
    return (new / old - 1.0) * 100.0


def classify(stock: dict) -> dict:
    """采用与行业板块一致的多周期相对强弱+均线+量能规则。"""
    asof = stock.get("asof", "")
    r5, r20 = stock.get("ret5"), stock.get("ret20")
    rm5, rm20 = stock.get("relative_market5"), stock.get("relative_market20")
    rs5, rs20 = stock.get("relative_sector5"), stock.get("relative_sector20")
    trend = stock.get("trend") or ""
    volume_ratio = stock.get("volume_ratio")
    required = (r5, r20, rm5, rm20, rs5, rs20)
    if not asof or any(v is None for v in required):
        return {"label": "待确认", "tone": "pending", "reason": "个股、所属行业或两市指数共同完整收盘日线不足，暂不作强弱判断。"}

    rel5 = (rm5 + rs5) / 2.0
    rel20 = (rm20 + rs20) / 2.0
    trend_bull = trend == "bullish"
    trend_bear = trend == "bearish"
    volume_confirm = volume_ratio is not None and volume_ratio >= 0.8

    strong_base = (rel5 >= 1.0 and rel20 >= 2.0 and r5 > 0 and r20 > 0
                   and rm5 >= 0 and rm20 >= 0 and rs5 >= 0 and rs20 >= 0)
    weak_base = (rel5 <= -1.0 and rel20 <= -2.0 and r5 < 0 and r20 < 0
                 and rm5 <= 0 and rm20 <= 0 and rs5 <= 0 and rs20 <= 0)

    if strong_base and trend_bull and volume_confirm:
        label, tone = "强势", "strong"
    elif strong_base:
        label, tone = "偏强", "bias-strong"
    elif rel5 > 0.2 and rel20 > 0.5 and not trend_bear:
        label, tone = "偏强", "bias-strong"
    elif weak_base and trend_bear and volume_ratio is not None:
        label, tone = "弱势", "weak"
    elif weak_base or (rel5 < -0.2 and rel20 < -0.5):
        label, tone = "偏弱", "bias-weak"
    elif rm5 >= 1.0 and rs5 >= 1.0 and rel20 <= -0.5 and r5 > 0:
        label, tone = "短期反弹", "rebound"
    elif rm5 <= -1.0 and rs5 <= -1.0 and rel20 >= 0.5 and r5 < 0:
        label, tone = "短期转弱", "bias-weak"
    else:
        label, tone = "震荡", "neutral"

    trend_text = {
        "bullish": "收盘价在MA20、MA60上方且MA20高于MA60",
        "bearish": "收盘价在MA20、MA60下方且MA20低于MA60",
        "above_ma20": "收盘价站上MA20，中期均线尚未完全走强",
        "below_ma20": "收盘价处于MA20下方",
        "mixed": "MA20/MA60多空交错",
        "insufficient": "均线样本不足",
    }.get(trend, "均线趋势待确认")
    volume_text = ("量能数据不足" if volume_ratio is None else
                   f"当日成交量约为前5日均量的{volume_ratio:.2f}倍")
    reason = (
        f"截至{asof}：个股近5/20日{r5:+.2f}%/{r20:+.2f}%；"
        f"较上证、深证均值超额{rm5:+.2f}/{rm20:+.2f}个百分点；"
        f"较所属行业超额{rs5:+.2f}/{rs20:+.2f}个百分点；"
        f"{trend_text}；{volume_text}。此为真实历史行情状态，不是预测。"
    )
    return {"label": label, "tone": tone, "reason": reason,
            "relative5": round(rel5, 3), "relative20": round(rel20, 3)}


def pending(symbol: str, name: str, sector: str, reason: str) -> dict:
    return {"symbol": symbol, "name": name or symbol, "sector": sector or "待核实",
            "asof": "", "label": "待确认", "tone": "pending", "reason": reason}


def build_one(symbol: str, sector: str, name: str,
              stock_frame: pd.DataFrame | None,
              sector_frame: pd.DataFrame | None,
              sh_frame: pd.DataFrame, sz_frame: pd.DataFrame) -> dict:
    if stock_frame is None:
        return pending(symbol, name, sector, "个股历史日线缺失，暂不作强弱判断。")
    if not sector or sector_frame is None:
        return pending(symbol, name, sector, "未取得该股官方所属行业或行业历史日线，暂不作强弱判断。")

    frames = [stock_frame, sector_frame, sh_frame, sz_frame]
    latest_dates = [str(f.iloc[-1]["date"]) for f in frames]
    if len(set(latest_dates)) != 1:
        return pending(symbol, name, sector,
                       "个股、所属行业、上证和深证最新完整收盘日期未同步一致：" + " / ".join(latest_dates) + "。")

    date_sets = [set(f["date"].astype(str)) for f in frames]
    common = sorted(set.intersection(*date_sets))
    if len(common) < 61:
        return pending(symbol, name, sector, f"四类真实行情共同交易日仅{len(common)}日，尚不足以稳定计算MA60。")
    if common[-1] != latest_dates[0]:
        return pending(symbol, name, sector, "四类数据最新完整收盘日未能对齐，暂不使用旧数据判断。")

    indexed = [f.set_index("date").reindex(common) for f in frames]
    closes = [pd.to_numeric(f["close"], errors="coerce").dropna().reset_index(drop=True) for f in indexed]
    if any(len(c) != len(common) for c in closes):
        return pending(symbol, name, sector, "共同交易日存在缺失收盘价，暂不作强弱判断。")

    stock_close, sector_close, sh_close, sz_close = closes
    stock5, stock20 = pct(stock_close, 5), pct(stock_close, 20)
    sector5, sector20 = pct(sector_close, 5), pct(sector_close, 20)
    sh5, sh20 = pct(sh_close, 5), pct(sh_close, 20)
    sz5, sz20 = pct(sz_close, 5), pct(sz_close, 20)
    if any(v is None for v in (stock5, stock20, sector5, sector20, sh5, sh20, sz5, sz20)):
        return pending(symbol, name, sector, "四类行情的5日/20日收益样本不足。")

    market5, market20 = (sh5 + sz5) / 2.0, (sh20 + sz20) / 2.0
    relative_market5, relative_market20 = stock5 - market5, stock20 - market20
    relative_sector5, relative_sector20 = stock5 - sector5, stock20 - sector20

    stock_volume = pd.to_numeric(indexed[0]["volume"], errors="coerce")
    volume_ratio = None
    if len(stock_volume) >= 6 and pd.notna(stock_volume.iloc[-1]):
        prior = stock_volume.iloc[-6:-1].dropna()
        if len(prior) == 5 and float(prior.mean()) > 0:
            volume_ratio = round(float(stock_volume.iloc[-1]) / float(prior.mean()), 3)

    ma20 = float(stock_close.iloc[-20:].mean())
    ma60 = float(stock_close.iloc[-60:].mean())
    latest_close = float(stock_close.iloc[-1])
    if latest_close >= ma20 >= ma60:
        trend = "bullish"
    elif latest_close <= ma20 <= ma60:
        trend = "bearish"
    elif latest_close >= ma20:
        trend = "above_ma20"
    elif latest_close < ma20:
        trend = "below_ma20"
    else:
        trend = "mixed"

    prev_close = float(stock_close.iloc[-2])
    day_pct = (latest_close / prev_close - 1.0) * 100.0 if prev_close > 0 else None
    row = {
        "symbol": symbol, "name": name or symbol, "sector": sector,
        "asof": common[-1], "day_pct": round(day_pct, 3) if day_pct is not None else None,
        "ret5": round(stock5, 3), "ret20": round(stock20, 3),
        "sector_ret5": round(sector5, 3), "sector_ret20": round(sector20, 3),
        "market_ret5": round(market5, 3), "market_ret20": round(market20, 3),
        "sh_ret5": round(sh5, 3), "sh_ret20": round(sh20, 3),
        "sz_ret5": round(sz5, 3), "sz_ret20": round(sz20, 3),
        "relative_market5": round(relative_market5, 3),
        "relative_market20": round(relative_market20, 3),
        "relative_sector5": round(relative_sector5, 3),
        "relative_sector20": round(relative_sector20, 3),
        "ma20": round(ma20, 4), "ma60": round(ma60, 4),
        "trend": trend, "volume_ratio": volume_ratio,
    }
    row.update(classify(row))
    return row


def main() -> int:
    symbols = read_symbols()
    sector_payload = load_json(SECTOR_DIR / "symbols.json")
    sector_map = sector_payload.get("symbols", {})
    if not isinstance(sector_map, dict) or not sector_map:
        raise ValueError("data/sectors/symbols.json 不存在或官方行业映射为空")
    score_payload = load_json(DATA / "scores.json")
    name_map = {str(x.get("symbol", "")).zfill(6): str(x.get("name", ""))
                for x in score_payload.get("stocks", []) if isinstance(x, dict)}

    sh = read_series(INDEXES / "sh000001.csv", index=True)
    sz = read_series(INDEXES / "sz399001.csv", index=True)
    sector_cache: dict[str, pd.DataFrame | None] = {}
    stock_cache: dict[str, pd.DataFrame | None] = {}
    rows = []

    for i, symbol in enumerate(symbols, 1):
        sector = str(sector_map.get(symbol, "")).strip()
        if symbol not in stock_cache:
            try:
                stock_cache[symbol] = read_series(DATA / f"{symbol}.csv")
            except Exception as exc:
                print(f"WARN {symbol}: 个股日线不可用：{exc}", flush=True)
                stock_cache[symbol] = None
        if sector and sector not in sector_cache:
            try:
                sector_cache[sector] = read_series(SECTOR_HISTORY / (sector_digest(sector) + ".csv"))
            except Exception as exc:
                print(f"WARN {sector}: 行业日线不可用：{exc}", flush=True)
                sector_cache[sector] = None
        item = build_one(symbol, sector, name_map.get(symbol, symbol),
                         stock_cache[symbol], sector_cache.get(sector), sh, sz)
        rows.append(item)
        print(f"[{i}/{len(symbols)}] {symbol} {item['label']} {item.get('asof') or '待确认'}", flush=True)

    ready = sum(1 for x in rows if x.get("asof") and x.get("tone") != "pending")
    if ready == 0:
        raise RuntimeError("没有任何股票取得四类行情同日完整数据；拒绝发布空白或过期强弱状态。")

    counts: dict[str, int] = {}
    for item in rows:
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    payload = {
        "ok": True,
        "source": "GitHub Actions / 个股、官方行业、上证、深证完整收盘日线",
        "method": "非预测：使用同一完整收盘交易日的1/5/20日真实收益、相对两市与所属行业超额收益、MA20/MA60和成交量比进行规则判断。",
        "generated_at_cn": datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S"),
        "asof": max((x.get("asof", "") for x in rows), default=""),
        "count": len(rows), "ready_count": ready, "pending_count": len(rows) - ready,
        "label_counts": counts,
        "stocks": rows,
        "notice": "强弱状态只反映已完成的历史收盘行情，不使用AI预测概率，不构成买卖建议；数据日期未同步时标记待确认。",
    }
    DATA.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(OUTPUT)
    print(f"已生成 {OUTPUT.relative_to(ROOT)}：{ready}/{len(rows)}只完成判断；日期={payload['asof']}；分布={counts}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"::error::个股强弱状态生成失败：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)