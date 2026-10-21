"""可靠更新上证指数、深证成指日线行情。
同时检查东方财富、新浪和 Yahoo Finance，选择最新交易日的数据。
若个股历史数据已更新到更晚交易日而指数仍滞后，则明确报错并保留旧文件。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "indexes"
STOCK_DATA = ROOT / "data"
TZ_CN = ZoneInfo("Asia/Shanghai")
MAX_ROWS = 800

INDEX = {
    "sh000001": ("上证指数", "000001", "sh000001", "000001.SS"),
    "sz399001": ("深证成指", "399001", "sz399001", "399001.SZ"),
}

MAPPING = {
    "日期": "date", "Date": "date", "date": "date", "日期时间": "date",
    "开盘": "open", "Open": "open", "open": "open",
    "最高": "high", "High": "high", "high": "high",
    "最低": "low", "Low": "low", "low": "low",
    "收盘": "close", "Close": "close", "close": "close",
    "成交量": "volume", "Volume": "volume", "volume": "volume",
}


def normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("没有返回行情")

    df = frame.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]).strip() for col in df.columns]
    else:
        df.columns = [str(col).strip() for col in df.columns]

    if not any(str(col).lower() in {"date", "日期", "日期时间"} for col in df.columns):
        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [str(col[0]).strip() for col in df.columns]
        else:
            df.columns = [str(col).strip() for col in df.columns]

    df = df.rename(columns=MAPPING)
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"源接口缺少字段 {missing}；实际字段={list(df.columns)[:20]}")

    df = df[required].copy()
    dates = pd.to_datetime(df["date"], errors="coerce")
    try:
        if dates.dt.tz is not None:
            dates = dates.dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    df["date"] = dates.dt.strftime("%Y-%m-%d")

    for col in required[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df = df[
        (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (df["volume"] >= 0)
    ]
    max_price = df[["open", "high", "low", "close"]].max(axis=1)
    min_price = df[["open", "high", "low", "close"]].min(axis=1)
    df = df[(df["high"] >= max_price) & (df["low"] <= min_price)]

    today = datetime.now(TZ_CN).date()
    df = df[pd.to_datetime(df["date"]).dt.date <= today]
    if len(df) < 100:
        raise ValueError(f"有效交易日不足100个：{len(df)}")

    latest = pd.to_datetime(df.iloc[-1]["date"]).date()
    if (today - latest).days > 12:
        raise ValueError(f"最新行情日期 {latest} 距今超过12天，拒绝使用陈旧数据")

    return df.tail(MAX_ROWS).reset_index(drop=True)


def fetch_one(info: tuple[str, str, str, str]) -> tuple[pd.DataFrame, str, list[str]]:
    name, east_code, sina_code, yahoo_code = info
    candidates = []
    errors = []
    # 历史序列优先级：东方财富 > 新浪 > Yahoo Finance。
    # 较低优先级的数据源只补充主数据源尚未覆盖的更新日期，避免整段历史成交量口径突变。
    priorities = {
        "AKShare / 东方财富指数": 3,
        "AKShare / 新浪指数": 2,
        "Yahoo Finance": 1,
    }

    try:
        import akshare as ak
        start = (datetime.now(TZ_CN).date() - timedelta(days=365 * 4)).strftime("%Y%m%d")
        end = (datetime.now(TZ_CN).date() + timedelta(days=1)).strftime("%Y%m%d")

        try:
            raw = ak.index_zh_a_hist(
                symbol=east_code,
                period="daily",
                start_date=start,
                end_date=end,
            )
            frame = normalize(raw)
            candidates.append((frame, "AKShare / 东方财富指数"))
        except Exception as exc:
            errors.append("东方财富: " + str(exc)[:220])

        try:
            raw = ak.stock_zh_index_daily(symbol=sina_code)
            frame = normalize(raw)
            candidates.append((frame, "AKShare / 新浪指数"))
        except Exception as exc:
            errors.append("新浪: " + str(exc)[:220])
    except Exception as exc:
        errors.append("AKShare模块: " + str(exc)[:220])

    try:
        import yfinance as yf
        raw = yf.download(
            yahoo_code,
            period="4y",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
            timeout=20,
        )
        frame = normalize(raw)
        candidates.append((frame, "Yahoo Finance"))
    except Exception as exc:
        errors.append("Yahoo Finance: " + str(exc)[:220])

    diagnostics = []
    for frame, source in candidates:
        latest = str(frame.iloc[-1]["date"])
        diagnostics.append(f"{source}={latest}")

    if not candidates:
        raise ValueError("所有数据源均失败：" + " | ".join(errors))

    # 先用最高优先级的成功来源作为历史主序列。
    candidates.sort(key=lambda item: priorities.get(item[1], 0), reverse=True)
    selected_frame, primary_source = candidates[0]
    selected_frame = selected_frame.copy()
    used_sources = [primary_source]
    current_latest = str(selected_frame.iloc[-1]["date"])

    # 其他来源仅补入主序列之后的新日期，不用备用源重写已有历史行。
    for candidate_frame, candidate_source in sorted(
        candidates, key=lambda item: str(item[0].iloc[-1]["date"])
    ):
        candidate_latest = str(candidate_frame.iloc[-1]["date"])
        if candidate_latest <= current_latest:
            continue
        extra = candidate_frame[
            pd.to_datetime(candidate_frame["date"]) > pd.to_datetime(current_latest)
        ].copy()
        if extra.empty:
            continue
        selected_frame = pd.concat([selected_frame, extra], ignore_index=True)
        selected_frame = (
            selected_frame.drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        current_latest = str(selected_frame.iloc[-1]["date"])
        if candidate_source not in used_sources:
            used_sources.append(candidate_source)

    selected_frame = selected_frame.tail(MAX_ROWS).reset_index(drop=True)
    selected_source = " + ".join(used_sources)
    print(
        f"{name} 数据源核验：" + "；".join(diagnostics)
        + f"；历史主源={primary_source}；补充来源={used_sources[1:] or '无'}"
        + f"；最终最新={selected_frame.iloc[-1]['date']}",
        flush=True,
    )
    if errors:
        print(f"{name} 部分数据源不可用：" + " | ".join(errors), flush=True)

    return selected_frame, selected_source, diagnostics


def latest_stock_trading_day() -> str | None:
    """以已保存的个股行情作交叉核验，防止指数数据落后却显示成功。"""
    if not STOCK_DATA.exists():
        return None

    dates = []
    for path in STOCK_DATA.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            value = item.get("latest_trading_day")
            if value:
                dates.append(str(value)[:10])
        except Exception:
            continue
    return max(dates) if dates else None


def merge_with_existing(code: str, fresh: pd.DataFrame) -> pd.DataFrame:
    target = OUT / f"{code}.csv"
    if not target.exists():
        return fresh.tail(MAX_ROWS).reset_index(drop=True)

    try:
        old = normalize(pd.read_csv(target, encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"无法验证现有文件，拒绝覆盖：{exc}") from exc

    old_latest = str(old.iloc[-1]["date"])
    fresh_latest = str(fresh.iloc[-1]["date"])
    if fresh_latest < old_latest:
        raise ValueError(
            f"新数据最新日期 {fresh_latest} 早于现有文件 {old_latest}，保留旧文件"
        )

    combined = pd.concat([old, fresh], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    combined = combined.sort_values("date").tail(MAX_ROWS).reset_index(drop=True)
    return combined


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stock_latest = latest_stock_trading_day()
    if stock_latest:
        print(f"个股数据最新交易日交叉核验基准：{stock_latest}", flush=True)

    summary = []
    success_count = 0
    failures = []

    for code, info in INDEX.items():
        name = info[0]
        try:
            frame, source, diagnostics = fetch_one(info)
            latest = str(frame.iloc[-1]["date"])

            # 若个股数据已出现更晚交易日，指数不得以陈旧行情冒充更新成功。
            if stock_latest and latest < stock_latest:
                raise ValueError(
                    f"指数最新日期 {latest} 落后于个股最新日期 {stock_latest}；"
                    f"各源结果：{'；'.join(diagnostics)}。保留原文件并将任务标记为失败"
                )

            combined = merge_with_existing(code, frame)
            target = OUT / f"{code}.csv"
            tmp = OUT / f"{code}.csv.tmp"
            combined.to_csv(tmp, index=False, encoding="utf-8-sig")
            tmp.replace(target)

            newest = str(combined.iloc[-1]["date"])
            item = {
                "code": code,
                "name": name,
                "latest_trading_day": newest,
                "rows": len(combined),
                "source": source,
                "source_dates": diagnostics,
                "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": "ok",
            }
            summary.append(item)
            (OUT / f"{code}.json").write_text(
                json.dumps(item, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            success_count += 1
            print(
                f"{name} {code}: {len(combined)} rows; latest={newest}; source={source}",
                flush=True,
            )
        except Exception as exc:
            failures.append(code)
            print(f"{name} {code} 更新失败：{str(exc)[:700]}（旧数据保留）", flush=True)
        time.sleep(0.5)

    if summary:
        (OUT / "index.json").write_text(
            json.dumps(
                {
                    "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "success_count": success_count,
                    "failed_codes": failures,
                    "indices": summary,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if success_count != len(INDEX):
        raise SystemExit(
            f"指数同步未全部成功：成功 {success_count}/{len(INDEX)}，失败={failures}。"
            "请查看 Actions 日志中的各数据源日期及错误。"
        )


if __name__ == "__main__":
    main()
