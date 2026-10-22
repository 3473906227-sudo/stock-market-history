"""采集上证指数、深证成指盘中快照，供 PythonAnywhere 通过 GitHub Raw 读取。
数据源顺序：东方财富主节点/备用节点 -> 腾讯财经 -> 新浪财经。
全部数据源失败或报价不完整时以非零状态退出，不覆盖上一份有效快照。
GitHub Actions 计划每 5 分钟运行一次，属于定时快照，非逐笔实时。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math
import re
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "indexes" / "realtime.json"
TZ_CN = ZoneInfo("Asia/Shanghai")
INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指"}
TENCENT_CODES = {"sh000001": "s_sh000001", "sz399001": "s_sz399001"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"


def number(value, default=0.0):
    try:
        value = float(str(value).strip().replace(",", ""))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def price_value(value):
    value = number(value)
    return value / 100.0 if abs(value) >= 100000 else value


def request_bytes(url, referer, limit=120000):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise ValueError("HTTP " + str(response.status))
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("响应超过大小限制")
    return payload


def parse_provider_time(value, now_cn):
    """只有可识别且时间合理的供应商时间戳才标为已核验。"""
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    dt = None
    try:
        if len(digits) == 10:
            dt = datetime.fromtimestamp(int(digits), TZ_CN)
        elif len(digits) == 13:
            dt = datetime.fromtimestamp(int(digits) / 1000.0, TZ_CN)
        elif len(digits) >= 14 and digits[:4].isdigit() and 2000 <= int(digits[:4]) <= 2100:
            dt = datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=TZ_CN)
        elif len(digits) == 12 and digits[:4].isdigit() and 2000 <= int(digits[:4]) <= 2100:
            dt = datetime.strptime(digits, "%Y%m%d%H%M").replace(tzinfo=TZ_CN)
        elif "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_CN)
            dt = dt.astimezone(TZ_CN)
    except (ValueError, OverflowError, OSError):
        return None
    if dt is None or abs((now_cn - dt).total_seconds()) > 6 * 3600:
        return None
    return dt


def normalize_quote(code, price, prev_close, source, now_cn, *, change_pct=None,
                    provider_time=None, open_price=None, high=None, low=None,
                    volume=0, amount=0, has_ohlc=False):
    price = price_value(price)
    prev_close = price_value(prev_close)
    if price <= 0 or prev_close <= 0:
        raise ValueError("现价或昨收无效")
    # 以同一快照的现价和昨收统一计算涨跌，避免供应商字段单位差异。
    delta = price - prev_close
    pct = delta / prev_close * 100
    if change_pct is not None:
        provider_pct = number(change_pct, pct)
        if abs(provider_pct) > 100:
            provider_pct /= 100.0
        if abs(provider_pct - pct) < 0.15:
            pct = provider_pct
    if abs(pct) > 35:
        raise ValueError("涨跌幅异常，拒绝保存")

    dt = parse_provider_time(provider_time, now_cn)
    op = price_value(open_price) if open_price is not None else price
    hi = price_value(high) if high is not None else price
    lo = price_value(low) if low is not None else price
    valid_ohlc = bool(has_ohlc and op > 0 and hi > 0 and lo > 0 and hi >= lo
                      and hi >= max(op, lo, price) and lo <= min(op, hi, price))
    if not valid_ohlc:
        op = hi = lo = price
    return {
        "price": round(price, 4), "prev_close": round(prev_close, 4),
        "change": round(delta, 4), "change_pct": round(pct, 4),
        "provider_time": dt.isoformat(timespec="seconds") if dt else None,
        "time_verified": bool(dt), "date": dt.strftime("%Y-%m-%d") if dt else "",
        "source": source, "has_ohlc": valid_ohlc,
        "open": round(op, 4), "high": round(hi, 4), "low": round(lo, 4),
        "volume": round(max(0.0, number(volume)), 4),
        "amount": round(max(0.0, number(amount)), 4),
    }


def fetch_eastmoney(now_cn):
    errors = []
    hosts = ("push2.eastmoney.com", "push2delay.eastmoney.com")
    for host in hosts:
        url = (f"https://{host}/api/qt/ulist.np/get?fltt=2&invt=2"
               "&fields=f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f86,f124"
               "&secids=1.000001,0.399001"
               "&ut=fa5fd1943c7b386f172d6893dbbd1d0c&_=" + str(int(time.time() * 1000)))
        try:
            payload = json.loads(request_bytes(url, "https://quote.eastmoney.com/").decode("utf-8-sig"))
            diff = (payload.get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            quotes = {}
            for row in diff if isinstance(diff, list) else []:
                if not isinstance(row, dict):
                    continue
                raw_code = str(row.get("f12") or "").strip().zfill(6)
                code = "sh000001" if raw_code == "000001" else "sz399001" if raw_code == "399001" else None
                if not code:
                    continue
                try:
                    quotes[code] = normalize_quote(
                        code, row.get("f2"), row.get("f18"),
                        "东方财富实时行情" if host == hosts[0] else "东方财富备用节点",
                        now_cn, change_pct=row.get("f3"),
                        provider_time=row.get("f86") or row.get("f124"),
                        open_price=row.get("f17"), high=row.get("f15"), low=row.get("f16"),
                        volume=row.get("f5"), amount=row.get("f6"), has_ohlc=True,
                    )
                except Exception as exc:
                    errors.append(INDEX_NAMES[code] + "报价校验失败：" + str(exc)[:100])
            if len(quotes) == len(INDEX_NAMES):
                return quotes, errors
            errors.append(host + "返回指数不完整")
        except Exception as exc:
            errors.append(host + "：" + str(exc)[:140])
    return {}, errors


def fetch_tencent(now_cn):
    keys = ",".join(TENCENT_CODES.values())
    errors = []
    for scheme in ("https", "http"):
        url = f"{scheme}://qt.gtimg.cn/q={keys}&_={int(time.time() * 1000)}"
        try:
            payload = request_bytes(url, "https://gu.qq.com/", 24000)
            text = payload.decode("gb18030", errors="replace")
            found = dict(re.findall(r'v_(s_sh000001|s_sz399001)\s*=\s*"([^"]*)"', text))
            quotes = {}
            for code, key in TENCENT_CODES.items():
                fields = [x.strip() for x in found.get(key, "").split("~")]
                if len(fields) < 6:
                    continue
                try:
                    price, change, pct = number(fields[3]), number(fields[4]), number(fields[5])
                    prev_close = price - change
                    if prev_close <= 0 and 1 + pct / 100 > 0:
                        prev_close = price / (1 + pct / 100)
                    quotes[code] = normalize_quote(
                        code, price, prev_close, "腾讯财经备用行情", now_cn,
                        change_pct=pct, volume=fields[6] if len(fields) > 6 else 0,
                        amount=fields[9] if len(fields) > 9 else 0,
                    )
                except Exception as exc:
                    errors.append("腾讯 " + INDEX_NAMES[code] + "：" + str(exc)[:100])
            if len(quotes) == len(INDEX_NAMES):
                return quotes, errors
            errors.append(scheme + " 腾讯接口未返回完整指数报价")
        except Exception as exc:
            errors.append(scheme + " 腾讯接口：" + str(exc)[:120])
    return {}, errors


def fetch_sina(now_cn):
    keys = "s_sh000001,s_sz399001"
    errors = []
    for scheme in ("https", "http"):
        url = f"{scheme}://hq.sinajs.cn/list={keys}&_={int(time.time() * 1000)}"
        try:
            payload = request_bytes(url, "https://finance.sina.com.cn/", 24000)
            text = payload.decode("gb18030", errors="replace")
            found = dict(re.findall(r'var\s+hq_str_(s_sh000001|s_sz399001)="([^"]*)"', text))
            quotes = {}
            for code, key in (("sh000001", "s_sh000001"), ("sz399001", "s_sz399001")):
                fields = [x.strip() for x in found.get(key, "").split(",")]
                if len(fields) < 4:
                    continue
                try:
                    price, change, pct = number(fields[1]), number(fields[2]), number(fields[3])
                    prev_close = price - change
                    if prev_close <= 0 and 1 + pct / 100 > 0:
                        prev_close = price / (1 + pct / 100)
                    quotes[code] = normalize_quote(
                        code, price, prev_close, "新浪财经备用行情", now_cn,
                        change_pct=pct, volume=fields[4] if len(fields) > 4 else 0,
                        amount=fields[5] if len(fields) > 5 else 0,
                    )
                except Exception as exc:
                    errors.append("新浪 " + INDEX_NAMES[code] + "：" + str(exc)[:100])
            if len(quotes) == len(INDEX_NAMES):
                return quotes, errors
            errors.append(scheme + " 新浪接口未返回完整指数报价")
        except Exception as exc:
            errors.append(scheme + " 新浪接口：" + str(exc)[:120])
    return {}, errors


def main():
    now_utc = datetime.now(timezone.utc)
    now_cn = now_utc.astimezone(TZ_CN)
    all_errors, quotes = [], {}
    for name, fetcher in (("东方财富", fetch_eastmoney), ("腾讯", fetch_tencent), ("新浪", fetch_sina)):
        result, errors = fetcher(now_cn)
        all_errors.extend(errors)
        if len(result) == len(INDEX_NAMES):
            quotes = result
            print("实时指数数据源：" + name, flush=True)
            break
        if result:
            all_errors.append(name + "只返回部分指数，拒绝混合拼接")
    if len(quotes) != len(INDEX_NAMES):
        print("实时指数采集失败：" + " | ".join(all_errors[-10:]), flush=True)
        raise SystemExit("未能从同一公开数据源取得上证指数和深证成指的完整有效报价；旧快照保持不变。")

    snapshot = {
        "schema_version": 1,
        "generated_at": now_utc.isoformat(timespec="seconds"),
        "source": "GitHub Actions intraday index quote relay",
        "refresh_target_seconds": 300,
        "market_time": now_cn.strftime("%Y-%m-%d %H:%M:%S"),
        "quotes": quotes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    checked = json.loads(temp.read_text(encoding="utf-8"))
    if set(checked.get("quotes", {})) != set(INDEX_NAMES):
        temp.unlink(missing_ok=True)
        raise SystemExit("快照完整性校验失败；旧文件保持不变。")
    for code, quote in checked["quotes"].items():
        if number(quote.get("price")) <= 0 or number(quote.get("prev_close")) <= 0:
            temp.unlink(missing_ok=True)
            raise SystemExit(code + "报价校验失败；旧文件保持不变。")
    temp.replace(OUT)
    for code, quote in quotes.items():
        print(f"OK {code} {INDEX_NAMES[code]} price={quote['price']} change={quote['change']} ({quote['change_pct']}%) source={quote['source']}", flush=True)
    print("快照已写入 " + str(OUT) + "；采集时间=" + snapshot["market_time"] + "；目标间隔=5分钟，非逐笔实时。", flush=True)


if __name__ == "__main__":
    main()
