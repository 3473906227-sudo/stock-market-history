#!/usr/bin/env python3
"""Build non-predictive A-share index strength labels from completed GitHub OHLCV candles.

Inputs:
  indexes/sh000001.csv
  indexes/sz399001.csv
Output:
  data/market_strength.json

No AI output, future-return probability, or forecast is used.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_DIR = ROOT / "indexes"
OUTPUT = ROOT / "data" / "market_strength.json"
CN = timezone(timedelta(hours=8))
INDEXES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
}
TONE_LABELS = {
    "strong": "强势",
    "bias-strong": "偏强",
    "neutral": "震荡",
    "bias-weak": "偏弱",
    "weak": "弱势",
    "pending": "待确认",
}


def read_candles(code: str) -> list[dict]:
    path = INDEX_DIR / f"{code}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {path.relative_to(ROOT)}")
    rows = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "open", "high", "low", "close", "volume"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path.name} 缺少 OHLCV 字段")
        for raw in reader:
            day = str(raw.get("date") or "").strip()
            try:
                datetime.strptime(day, "%Y-%m-%d")
                session = str(raw.get("session") or "legacy").strip().lower()
                # Only completed candles are accepted. Legacy rows are retained only
                # for historical dates; today's unmarked row is never treated as final.
                if session not in ("final", "legacy"):
                    continue
                today = datetime.now(CN).strftime("%Y-%m-%d")
                if session == "legacy" and day >= today:
                    continue
                o, h, low, close, volume = [
                    float(raw[k]) for k in ("open", "high", "low", "close", "volume")
                ]
                if not all(math.isfinite(x) for x in (o, h, low, close, volume)):
                    continue
                if min(o, h, low, close) <= 0 or volume < 0 or h < max(o, low, close) or low > min(o, h, close):
                    continue
                rows[day] = {
                    "date": day, "open": o, "high": h, "low": low,
                    "close": close, "volume": volume, "session": session,
                }
            except (ValueError, TypeError, KeyError):
                continue
    result = [rows[k] for k in sorted(rows)]
    if len(result) < 61:
        raise ValueError(f"{code} 完整收盘日线不足61条")
    return result[-800:]


def pct(new: float, old: float) -> float:
    if old <= 0:
        raise ValueError("历史收盘价无效")
    return (new / old - 1.0) * 100.0


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def technical(rows: list[dict]) -> dict:
    close = [x["close"] for x in rows]
    volume = [x["volume"] for x in rows]
    last = rows[-1]
    ma20 = avg(close[-20:])
    ma60 = avg(close[-60:])
    if last["close"] >= ma20 >= ma60:
        trend = "bullish"
        trend_text = "收盘价位于MA20、MA60上方，且MA20高于MA60"
    elif last["close"] <= ma20 <= ma60:
        trend = "bearish"
        trend_text = "收盘价位于MA20、MA60下方，且MA20低于MA60"
    elif last["close"] >= ma20:
        trend = "above_ma20"
        trend_text = "收盘价站上MA20，但中期均线尚未形成多头排列"
    else:
        trend = "below_ma20"
        trend_text = "收盘价处于MA20下方，趋势仍需观察"
    prior_volume = avg(volume[-6:-1])
    volume_ratio = round(volume[-1] / prior_volume, 3) if prior_volume > 0 else None
    return {
        "asof": last["date"],
        "close": round(last["close"], 3),
        "change_pct": round(pct(last["close"], close[-2]), 3),
        "ret5": round(pct(close[-1], close[-6]), 3),
        "ret20": round(pct(close[-1], close[-21]), 3),
        "ret60": round(pct(close[-1], close[-61]), 3),
        "ma20": round(ma20, 3),
        "ma60": round(ma60, 3),
        "trend": trend,
        "trend_text": trend_text,
        "volume_ratio": volume_ratio,
        "session": last["session"],
    }


def classify(item: dict, peer: dict) -> dict:
    r5, r20 = item["ret5"], item["ret20"]
    rel5 = round(r5 - peer["ret5"], 3)
    rel20 = round(r20 - peer["ret20"], 3)
    trend = item["trend"]
    volume = item["volume_ratio"]
    volume_confirm = volume is not None and volume >= 0.8
    strong_base = r5 >= 0.8 and r20 >= 1.5 and rel5 >= 0 and rel20 >= 0
    weak_base = r5 <= -0.8 and r20 <= -1.5 and rel5 <= 0 and rel20 <= 0

    if strong_base and trend == "bullish" and volume_confirm:
        label, tone = "强势", "strong"
    elif strong_base or (r5 > 0.2 and r20 > 0.5 and trend != "bearish"):
        label, tone = "偏强", "bias-strong"
    elif weak_base and trend == "bearish" and volume_confirm:
        label, tone = "弱势", "weak"
    elif weak_base or (r5 < -0.2 and r20 < -0.5 and trend != "bullish"):
        label, tone = "偏弱", "bias-weak"
    else:
        label, tone = "震荡", "neutral"

    volume_text = "量能不足或未确认" if volume is None else f"最新成交量为前5日均量的{volume:.2f}倍"
    reason = (
        f"截至{item['asof']}：近5/20/60日涨跌{item['ret5']:+.2f}%/"
        f"{item['ret20']:+.2f}%/{item['ret60']:+.2f}%；相对另一指数超额"
        f"{rel5:+.2f}/{rel20:+.2f}个百分点；{item['trend_text']}；{volume_text}。"
        "仅为已发生行情的强弱诊断，不是未来预测或买卖信号。"
    )
    return {
        "label": label, "tone": tone, "reason": reason,
        "relative5_vs_peer": rel5, "relative20_vs_peer": rel20,
    }


def classify_overall(sh: dict, sz: dict) -> dict:
    avg5 = avg([sh["ret5"], sz["ret5"]])
    avg20 = avg([sh["ret20"], sz["ret20"]])
    bullish = [sh["trend"] == "bullish", sz["trend"] == "bullish"]
    bearish = [sh["trend"] == "bearish", sz["trend"] == "bearish"]
    if all(bullish) and sh["ret5"] > 0 and sz["ret5"] > 0 and sh["ret20"] > 0 and sz["ret20"] > 0:
        label, tone = "强势", "strong"
    elif avg5 > 0.25 and avg20 > 0.5 and not all(bearish):
        label, tone = "偏强", "bias-strong"
    elif all(bearish) and sh["ret5"] < 0 and sz["ret5"] < 0 and sh["ret20"] < 0 and sz["ret20"] < 0:
        label, tone = "弱势", "weak"
    elif avg5 < -0.25 and avg20 < -0.5 and not all(bullish):
        label, tone = "偏弱", "bias-weak"
    else:
        label, tone = "震荡", "neutral"
    reason = (
        f"两市等权近5/20日平均涨跌为{avg5:+.2f}%/{avg20:+.2f}%；"
        f"上证趋势：{sh['trend_text']}；深证趋势：{sz['trend_text']}。"
        "综合状态依据两市真实收盘日线、MA20/MA60及多周期涨跌计算，不使用预测概率。"
    )
    return {"label": label, "tone": tone, "reason": reason,
            "average_ret5": round(avg5, 3), "average_ret20": round(avg20, 3)}


def main() -> None:
    rows = {code: read_candles(code) for code in INDEXES}
    dates = {code: rows[code][-1]["date"] for code in INDEXES}
    if len(set(dates.values())) != 1:
        raise ValueError("上证与深证最新完整收盘日期不一致：" + str(dates))
    asof = next(iter(dates.values()))
    tech = {code: technical(rows[code]) for code in INDEXES}
    if any(x["asof"] != asof for x in tech.values()):
        raise ValueError("指数强弱快照日期未对齐")
    for code, peer_code in (("sh000001", "sz399001"), ("sz399001", "sh000001")):
        state = classify(tech[code], tech[peer_code])
        tech[code].update(state)
        tech[code]["name"] = INDEXES[code]
    overall = classify_overall(tech["sh000001"], tech["sz399001"])
    generated = datetime.now(CN).isoformat(timespec="seconds")
    payload = {
        "ok": True,
        "source": "GitHub Actions · validated completed index OHLCV",
        "method": "真实收盘日线 + 1/5/20/60日收益 + 同期另一指数相对强弱 + MA20/MA60 + 成交量确认；不使用AI预测或未来收益标签。",
        "generated_at_cn": generated,
        "asof": asof,
        "count": 2,
        "overall": overall,
        "indices": tech,
        "notice": "本状态仅描述截至最新完整收盘交易日已经发生的市场强弱，不代表未来走势，不构成投资建议。",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, OUTPUT)
    print(f"已生成大盘强弱状态：{asof}；综合={overall['label']}；"
          + "；".join(f"{INDEXES[c]}={tech[c]['label']}" for c in INDEXES))


if __name__ == "__main__":
    main()
