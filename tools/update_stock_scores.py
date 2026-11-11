#!/usr/bin/env python3
"""Build a transparent A-share quality/valuation/sector/technical score snapshot.

Inputs are existing GitHub Actions OHLCV files, Eastmoney/AKShare quarterly
results and market valuation snapshot, and the official industry-history mirror.
Missing factors remain missing; incomplete names are never ranked as if complete.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import re
import time

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SECTORS = DATA / "sectors"
INDEXES = ROOT / "indexes"
OUT = DATA / "scores.json"
CN = timezone(timedelta(hours=8))
SYMBOL_FILE = ROOT / "stock_symbols.txt"


def stamp() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value):
    try:
        if value is None or pd.isna(value):
            return None
        value = float(str(value).replace(",", "").replace("%", "").strip())
        return round(value, 4) if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def read_symbols() -> list[str]:
    if not SYMBOL_FILE.is_file():
        raise FileNotFoundError(f"找不到股票清单：{SYMBOL_FILE}")
    result, seen = [], set()
    for line in SYMBOL_FILE.read_text(encoding="utf-8-sig").splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if re.fullmatch(r"\d{6}", code) and code not in seen:
            result.append(code)
            seen.add(code)
    if not result:
        raise ValueError("stock_symbols.txt 中没有A股代码")
    return result


def report_period_candidates() -> list[str]:
    today = datetime.now(CN).date()
    result = []
    for year in range(today.year, today.year - 3, -1):
        for month, day in ((9, 30), (6, 30), (3, 31), (12, 31)):
            value = f"{year:04d}{month:02d}{day:02d}"
            if datetime.strptime(value, "%Y%m%d").date() <= today:
                result.append(value)
    return result


def load_financials(ak):
    errors = []
    for period in report_period_candidates():
        try:
            frame = ak.stock_yjbb_em(date=period)
            if frame is not None and not frame.empty and {"股票代码", "净资产收益率"}.issubset(frame.columns):
                frame = frame.copy()
                frame["股票代码"] = frame["股票代码"].astype(str).str.extract(r"(\d{6})", expand=False)
                numeric = [
                    "每股收益", "营业总收入-营业总收入", "营业总收入-同比增长",
                    "净利润-净利润", "净利润-同比增长", "每股经营现金流量",
                    "销售毛利率", "净资产收益率",
                ]
                for col in numeric:
                    if col in frame.columns:
                        frame[col] = pd.to_numeric(frame[col], errors="coerce")
                frame = frame.dropna(subset=["股票代码"]).drop_duplicates("股票代码", keep="last")
                if len(frame) >= 500:
                    print(f"财务报告读取成功：{period}，{len(frame)}只", flush=True)
                    return frame, period
                errors.append(f"{period}: rows={len(frame)}")
        except Exception as exc:
            errors.append(f"{period}: {type(exc).__name__} {str(exc)[:140]}")
        time.sleep(0.4)
    raise RuntimeError("近年业绩报表均未取得有效全市场数据：" + " | ".join(errors[-5:]))


def load_spot(ak):
    frame = ak.stock_zh_a_spot_em()
    if frame is None or frame.empty or not {"代码", "市盈率-动态", "市净率"}.issubset(frame.columns):
        raise ValueError("A股估值快照为空或缺少PE/PB字段")
    frame = frame.copy()
    frame["代码"] = frame["代码"].astype(str).str.extract(r"(\d{6})", expand=False)
    for col in ("市盈率-动态", "市净率", "总市值", "流通市值", "最新价", "涨跌幅", "换手率"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["代码"]).drop_duplicates("代码", keep="last")
    return frame


def load_sector_map():
    path = SECTORS / "symbols.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        mapping = payload.get("symbols", {})
        return mapping if isinstance(mapping, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_index_return(code: str):
    path = INDEXES / f"{code}.csv"
    if not path.is_file():
        return None
    try:
        frame = pd.read_csv(path, usecols=lambda c: c in {"date", "close"})
        close = pd.to_numeric(frame.get("close"), errors="coerce").dropna()
        if len(close) < 21 or close.iloc[-21] <= 0:
            return None
        return float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
    except Exception:
        return None


def load_ohlcv_return(path: Path, min_rows: int = 21):
    if not path.is_file():
        return None
    try:
        frame = pd.read_csv(path, usecols=lambda c: c in {"date", "close"})
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["close"]).sort_values("date")
        if len(frame) < min_rows or frame.iloc[-min_rows]["close"] <= 0:
            return None
        return float((frame.iloc[-1]["close"] / frame.iloc[-min_rows]["close"] - 1) * 100), str(frame.iloc[-1]["date"])[:10]
    except Exception:
        return None


def load_technical(code: str):
    path = DATA / f"{code}.csv"
    if not path.is_file():
        return None
    try:
        frame = pd.read_csv(path, usecols=lambda c: c in {"date", "close", "volume"})
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        if "volume" in frame:
            frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
        frame = frame.dropna(subset=["close"]).sort_values("date").drop_duplicates("date", keep="last")
        if len(frame) < 61:
            return None
        close = frame["close"].astype(float)
        ma20, ma60 = float(close.tail(20).mean()), float(close.tail(60).mean())
        last = float(close.iloc[-1])
        ret20 = float((last / float(close.iloc[-21]) - 1) * 100)
        ret60 = float((last / float(close.iloc[-61]) - 1) * 100)
        volume_ratio = None
        if "volume" in frame and frame["volume"].notna().sum() >= 25:
            v = frame["volume"].fillna(0).astype(float)
            base = float(v.tail(20).mean())
            if base > 0:
                volume_ratio = float(v.tail(5).mean() / base)
        return {
            "asof": str(frame.iloc[-1]["date"])[:10], "close": last,
            "ma20": ma20, "ma60": ma60, "ret20": ret20, "ret60": ret60,
            "volume_ratio": volume_ratio,
        }
    except Exception:
        return None


def score_fundamentals(row):
    fields = ["净资产收益率", "营业总收入-同比增长", "净利润-同比增长",
              "每股经营现金流量", "销售毛利率"]
    values = {k: safe_float(row.get(k)) for k in fields}
    eps = safe_float(row.get("每股收益"))
    available = sum(v is not None for v in values.values())
    if available < 4:
        return None, values, available
    points = 0.0
    maxima = 0.0
    roe = values["净资产收益率"]
    if roe is not None:
        maxima += 10
        points += 10 if roe >= 20 else 9 if roe >= 15 else 8 if roe >= 10 else 6 if roe >= 5 else 3 if roe >= 0 else 0
    rev = values["营业总收入-同比增长"]
    if rev is not None:
        maxima += 6
        points += 6 if rev >= 20 else 5 if rev >= 10 else 4 if rev >= 0 else 2 if rev >= -10 else 0
    profit = values["净利润-同比增长"]
    if profit is not None:
        maxima += 6
        points += 6 if profit >= 30 else 5 if profit >= 10 else 4 if profit >= 0 else 2 if profit >= -10 else 0
    cash = values["每股经营现金流量"]
    if cash is not None:
        maxima += 4
        if eps is not None and eps > 0:
            ratio = cash / eps
            points += 4 if ratio >= 1.2 else 3 if ratio >= .8 else 2 if ratio >= .3 else 1 if ratio >= 0 else 0
        else:
            points += 2 if cash > 0 else 0
    margin = values["销售毛利率"]
    if margin is not None:
        maxima += 4
        points += 4 if margin >= 50 else 3.5 if margin >= 35 else 3 if margin >= 20 else 2 if margin >= 10 else 1
    return round(points / maxima * 30, 2) if maxima else None, values, available


def relative_valuation_score(pe, pb, growth, pe_median, pb_median):
    if pe is None or pb is None or pe <= 0 or pb <= 0 or not pe_median or not pb_median:
        return None, {"pe_peer_ratio": None, "pb_peer_ratio": None, "peg": None}
    pe_ratio, pb_ratio = pe / pe_median, pb / pb_median
    pe_points = 12 if pe_ratio <= .7 else 10 if pe_ratio <= 1 else 7 if pe_ratio <= 1.3 else 4 if pe_ratio <= 1.8 else 1
    pb_points = 8 if pb_ratio <= .7 else 7 if pb_ratio <= 1 else 5 if pb_ratio <= 1.3 else 3 if pb_ratio <= 1.8 else 1
    peg = pe / max(float(growth), 5) if growth is not None and growth > 0 else None
    peg_points = (5 if peg <= 1 else 4 if peg <= 1.5 else 3 if peg <= 2.5 else 1 if peg <= 4 else 0) if peg is not None else 0
    return round(pe_points + pb_points + peg_points, 2), {
        "pe_peer_ratio": round(pe_ratio, 3), "pb_peer_ratio": round(pb_ratio, 3),
        "peg": round(peg, 3) if peg is not None else None,
    }


def score_sector(ret5, ret20, excess20):
    if ret5 is None or ret20 is None or excess20 is None:
        return None
    p5 = 5 if ret5 >= 3 else 4 if ret5 >= 1 else 3 if ret5 >= 0 else 1.5 if ret5 >= -2 else 0
    p20 = 5 if ret20 >= 8 else 4 if ret20 >= 3 else 3 if ret20 >= 0 else 1.5 if ret20 >= -5 else 0
    pex = 10 if excess20 >= 5 else 8 if excess20 >= 2 else 6 if excess20 >= 0 else 3 if excess20 >= -3 else 0
    return round(p5 + p20 + pex, 2)


def score_technical(tech):
    if not tech:
        return None
    close, ma20, ma60 = tech["close"], tech["ma20"], tech["ma60"]
    ret20, ret60 = tech["ret20"], tech["ret60"]
    p_close_ma20 = 6 if close > ma20 else 0
    p_ma = 6 if ma20 > ma60 else 0
    p20 = 6 if ret20 >= 8 else 5 if ret20 >= 3 else 3 if ret20 >= 0 else 1 if ret20 >= -5 else 0
    p60 = 7 if ret60 >= 15 else 6 if ret60 >= 5 else 4 if ret60 >= 0 else 2 if ret60 >= -10 else 0
    return round(p_close_ma20 + p_ma + p20 + p60, 2)


def score_label(score):
    if score is None:
        return "数据不完整"
    if score >= 85:
        return "优秀观察"
    if score >= 75:
        return "重点跟踪"
    if score >= 65:
        return "中性观察"
    return "谨慎观察"


def main():
    import akshare as ak

    symbols = read_symbols()
    print(f"开始生成综合评分：{len(symbols)}只观察股票", flush=True)
    financials, report_period = load_financials(ak)
    spot = load_spot(ak)
    print(f"估值快照读取成功：{len(spot)}只", flush=True)

    peers = financials[["股票代码", "所处行业"]].merge(
        spot[["代码", "市盈率-动态", "市净率"]], left_on="股票代码", right_on="代码", how="inner"
    )
    peers["所处行业"] = peers["所处行业"].fillna("").astype(str).str.strip()
    pe_medians = peers[(peers["市盈率-动态"] > 0) & (peers["所处行业"] != "")].groupby("所处行业")["市盈率-动态"].median().to_dict()
    pb_medians = peers[(peers["市净率"] > 0) & (peers["所处行业"] != "")].groupby("所处行业")["市净率"].median().to_dict()

    fin_by_code = financials.set_index("股票代码", drop=False).to_dict("index")
    spot_by_code = spot.set_index("代码", drop=False).to_dict("index")
    sector_map = load_sector_map()
    market_returns = [load_index_return("sh000001"), load_index_return("sz399001")]
    market_returns = [x for x in market_returns if x is not None]
    market_ret20 = float(np.mean(market_returns)) if market_returns else None

    results = []
    for code in symbols:
        fin = fin_by_code.get(code, {})
        quote = spot_by_code.get(code, {})
        raw_industry = sector_map.get(code)
        if raw_industry is None or str(raw_industry).strip().lower() in ("", "nan", "none"):
            raw_industry = fin.get("所处行业")
        industry = str(raw_industry or "").strip()
        if industry.lower() in ("nan", "none"):
            industry = ""
        technical = load_technical(code)
        report_score, fin_values, fin_coverage = score_fundamentals(fin) if fin else (None, {}, 0)
        pe = safe_float(quote.get("市盈率-动态"))
        pb = safe_float(quote.get("市净率"))
        peer_industry = str(fin.get("所处行业") or industry or "").strip()
        if peer_industry.lower() in ("nan", "none"):
            peer_industry = industry
        industry_pe = safe_float(pe_medians.get(peer_industry))
        industry_pb = safe_float(pb_medians.get(peer_industry))
        valuation_score, valuation_ratios = relative_valuation_score(
            pe, pb, fin_values.get("净利润-同比增长"), industry_pe, industry_pb
        )

        sector_file = SECTORS / "history" / (hashlib.sha256(industry.encode("utf-8")).hexdigest()[:20] + ".csv") if industry else None
        ret5 = ret20 = sector_asof = None
        if sector_file:
            r5 = load_ohlcv_return(sector_file, 6)
            r20 = load_ohlcv_return(sector_file, 21)
            if r5:
                ret5, sector_asof = r5
            if r20:
                ret20, sector_asof = r20
        excess20 = (ret20 - market_ret20) if ret20 is not None and market_ret20 is not None else None
        sector_score = score_sector(ret5, ret20, excess20)
        technical_score = score_technical(technical)
        components = [report_score, valuation_score, sector_score, technical_score]
        complete = all(x is not None for x in components)
        total = round(sum(components), 2) if complete else None

        flags = []
        if pe is None or pe <= 0:
            flags.append("市盈率无效或亏损，估值评分不完整")
        if fin_values.get("净资产收益率") is not None and fin_values["净资产收益率"] < 0:
            flags.append("ROE为负")
        if fin_values.get("净利润-同比增长") is not None and fin_values["净利润-同比增长"] < -30:
            flags.append("净利润同比大幅下滑")
        if fin_values.get("每股经营现金流量") is not None and fin_values["每股经营现金流量"] < 0:
            flags.append("每股经营现金流为负")
        if technical and technical["ret60"] < -15:
            flags.append("60日技术趋势偏弱")
        if not industry:
            flags.append("未匹配官方行业")

        display_name = quote.get("名称")
        if display_name is None or str(display_name).strip().lower() in ("", "nan", "none"):
            display_name = fin.get("股票简称")
        if display_name is None or str(display_name).strip().lower() in ("", "nan", "none"):
            display_name = code
        results.append({
            "symbol": code,
            "name": str(display_name)[:48],
            "industry": industry or "未匹配官方行业",
            "score": total,
            "label": score_label(total),
            "complete": complete,
            "coverage": sum(x is not None for x in components),
            "components": {
                "fundamental": report_score,
                "valuation": valuation_score,
                "sector": sector_score,
                "technical": technical_score,
            },
            "fundamental": {
                "roe": fin_values.get("净资产收益率"),
                "revenue_growth": fin_values.get("营业总收入-同比增长"),
                "profit_growth": fin_values.get("净利润-同比增长"),
                "operating_cashflow_per_share": fin_values.get("每股经营现金流量"),
                "gross_margin": fin_values.get("销售毛利率"),
                "eps": safe_float(fin.get("每股收益")),
                "report_period": report_period,
                "coverage": f"{fin_coverage}/5",
            },
            "valuation": {
                "pe_ttm": pe, "pb": pb,
                "industry_pe_median": industry_pe,
                "industry_pb_median": industry_pb,
                **valuation_ratios,
                "peer_basis": "东方财富全市场同业中位数",
            },
            "sector": {
                "ret5": round(ret5, 2) if ret5 is not None else None,
                "ret20": round(ret20, 2) if ret20 is not None else None,
                "excess20": round(excess20, 2) if excess20 is not None else None,
                "asof": sector_asof,
            },
            "technical": technical,
            "flags": flags,
            "asof": technical.get("asof") if technical else None,
        })

    results.sort(key=lambda x: (x["score"] is not None, x["score"] or -1, x["components"]["fundamental"] or -1), reverse=True)
    complete_count = sum(bool(x["complete"]) for x in results)
    payload = {
        "ok": True,
        "generated_at": stamp(),
        "quote_asof": datetime.now(CN).strftime("%Y-%m-%d"),
        "report_period": report_period,
        "source": "AKShare / 东方财富全市场业绩报表与估值快照 + GitHub Actions真实日线与官方行业历史",
        "weights": {"fundamental": 30, "valuation": 25, "sector": 20, "technical": 25},
        "count": len(results),
        "complete_count": complete_count,
        "incomplete_count": len(results) - complete_count,
        "market_ret20": round(market_ret20, 3) if market_ret20 is not None else None,
        "stocks": results,
        "method": "综合研究评分=财务质量30+相对估值25+行业景气20+技术趋势25。只有四项均有真实数据才进入完整排名；评分用于研究排序，不是买入信号。",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(OUT)
    print(f"综合评分快照已生成：{complete_count}/{len(results)}只完整；财报期={report_period}；文件={OUT}", flush=True)
    if complete_count == 0:
        raise RuntimeError("没有任何股票获得完整四维评分；请检查财报、估值、行业和日线数据源。")


if __name__ == "__main__":
    main()
