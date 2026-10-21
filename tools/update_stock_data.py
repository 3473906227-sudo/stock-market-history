"""A股个股历史日线更新器，由 GitHub Actions 调用。

优先使用 AKShare 新浪前复权日线，备用东方财富日线和 Yahoo Finance。
每只股票独立处理：单只失败不覆盖旧文件；所有股票均失败时以非零状态退出。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SYMBOL_FILE = ROOT / "stock_symbols.txt"
OUT = ROOT / "data"
MAX_ROWS = 4000
TZ_CN = ZoneInfo("Asia/Shanghai")

COLUMN_MAP = {
    "日期": "date", "日期时间": "date", "date": "date", "Date": "date",
    "datetime": "date", "Datetime": "date", "index": "date",
    "开盘": "open", "open": "open", "Open": "open",
    "最高": "high", "high": "high", "High": "high",
    "最低": "low", "low": "low", "Low": "low",
    "收盘": "close", "close": "close", "Close": "close",
    "成交量": "volume", "volume": "volume", "Volume": "volume",
}


def read_symbols() -> list[str]:
    if not SYMBOL_FILE.exists():
        raise FileNotFoundError(f"找不到股票清单：{SYMBOL_FILE}")
    symbols = []
    seen = set()
    for line in SYMBOL_FILE.read_text(encoding="utf-8-sig").splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        symbols.append(code)
    if not symbols:
        raise ValueError("stock_symbols.txt 中没有可更新的股票代码")
    return symbols


def is_cn_a_share(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit()


def sina_symbol(symbol: str) -> str:
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("4", "8")):
        return "bj" + symbol
    return "sz" + symbol


def yahoo_symbol(symbol: str) -> str:
    if is_cn_a_share(symbol):
        if symbol.startswith("6"):
            return symbol + ".SS"
        if symbol.startswith(("4", "8")):
            return symbol + ".BJ"
        return symbol + ".SZ"
    return symbol


def normalize(frame: pd.DataFrame, check_freshness: bool = True) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("行情接口返回空数据")

    df = frame.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]).strip() for col in df.columns]
    else:
        df.columns = [str(col).strip() for col in df.columns]

    if not any(str(c).lower() in {"date", "日期", "日期时间", "datetime", "index"} for c in df.columns):
        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [str(col[0]).strip() for col in df.columns]
        else:
            df.columns = [str(col).strip() for col in df.columns]

    df = df.rename(columns=COLUMN_MAP)
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"行情字段缺失 {missing}；接口字段={list(df.columns)[:15]}")

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
    price_max = df[["open", "high", "low", "close"]].max(axis=1)
    price_min = df[["open", "high", "low", "close"]].min(axis=1)
    df = df[(df["high"] >= price_max) & (df["low"] <= price_min)]

    if len(df) < 20:
        raise ValueError(f"有效行情不足20个交易日：{len(df)}")

    df = df.tail(MAX_ROWS).reset_index(drop=True)
    latest = pd.to_datetime(df.iloc[-1]["date"]).date()
    today = datetime.now(TZ_CN).date()
    if check_freshness and (today - latest).days > 12:
        raise ValueError(f"最新行情日期 {latest} 距今超过12天，拒绝用陈旧数据覆盖")
    return df


def fetch_one(symbol: str) -> tuple[pd.DataFrame, str]:
    errors = []

    if is_cn_a_share(symbol):
        try:
            import akshare as ak
            try:
                frame = ak.stock_zh_a_daily(symbol=sina_symbol(symbol), adjust="qfq")
                return normalize(frame), "AKShare/Sina 前复权"
            except Exception as exc:
                errors.append("AKShare新浪: " + str(exc)[:180])

            try:
                start = "19900101"
                end = (datetime.now(TZ_CN).date() + timedelta(days=1)).strftime("%Y%m%d")
                frame = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="qfq",
                )
                return normalize(frame), "AKShare/东方财富前复权"
            except Exception as exc:
                errors.append("AKShare东方财富: " + str(exc)[:180])
        except Exception as exc:
            errors.append("AKShare模块: " + str(exc)[:180])

    try:
        import yfinance as yf
        frame = yf.download(
            yahoo_symbol(symbol),
            period="max",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
            timeout=20,
        )
        return normalize(frame), "Yahoo Finance"
    except Exception as exc:
        errors.append("Yahoo Finance: " + str(exc)[:180])

    raise ValueError(" | ".join(errors))


def merge_with_existing(symbol: str, fresh: pd.DataFrame) -> pd.DataFrame:
    target = OUT / f"{symbol}.csv"
    if not target.exists():
        return fresh.tail(MAX_ROWS).reset_index(drop=True)

    try:
        old = normalize(pd.read_csv(target, encoding="utf-8-sig"), check_freshness=False)
        old_latest = old.iloc[-1]["date"]
        fresh_latest = fresh.iloc[-1]["date"]
        if fresh_latest < old_latest:
            raise ValueError(
                f"源数据最新日期 {fresh_latest} 早于现有数据 {old_latest}，保留旧文件"
            )
        combined = pd.concat([old, fresh], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date"], keep="last")
        combined = combined.sort_values("date").tail(MAX_ROWS).reset_index(drop=True)
        return combined
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"读取现有历史数据失败，保留旧文件：{exc}") from exc


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    symbols = read_symbols()
    succeeded = 0
    failed = []

    for number, symbol in enumerate(symbols, start=1):
        target = OUT / f"{symbol}.csv"
        meta_path = OUT / f"{symbol}.json"
        try:
            fresh, source = fetch_one(symbol)
            combined = merge_with_existing(symbol, fresh)
            latest = str(combined.iloc[-1]["date"])

            temp_path = OUT / f"{symbol}.csv.tmp"
            combined.to_csv(temp_path, index=False, encoding="utf-8-sig")
            temp_path.replace(target)

            metadata = {
                "symbol": symbol,
                "source": source,
                "rows": len(combined),
                "latest_trading_day": latest,
                "synced_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            meta_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            succeeded += 1
            print(
                f"[{number}/{len(symbols)}] OK {symbol}: "
                f"{len(combined)} rows; latest={latest}; source={source}",
                flush=True,
            )
        except Exception as exc:
            failed.append(symbol)
            print(
                f"[{number}/{len(symbols)}] FAIL {symbol}: {str(exc)[:300]} "
                "(保留旧数据)",
                flush=True,
            )
        time.sleep(0.6)

    print(
        f"个股行情同步结束：成功 {succeeded}/{len(symbols)}，失败 {len(failed)}；"
        f"失败代码={','.join(failed) if failed else '无'}",
        flush=True,
    )
    if succeeded == 0:
        raise SystemExit("全部股票均更新失败；未生成虚假行情。请检查 Actions 日志和数据源连接。")


if __name__ == "__main__":
    main()
