#!/usr/bin/env python3
"""交易日行业BK收盘快照备用源：只有经过代码/收盘日期/价格交叉核验才写入。

维瑞评级转引东方财富BK板块数据，不是东方财富官方直接API，也不是完整OHLCV。
禁止使用较旧快照填补最新交易日；缺少5/20日历史基准时返回None。
"""
from __future__ import annotations
import csv
import gzip
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from tools.github_schedule_trigger import a_share_trading_status

BASE = "https://www.verityrank.com.cn/markets/sectors/"
SOURCE = "维瑞评级转引东方财富BK行业收盘快照（第三方、非官方直接API）"

class SnapshotNotFresh(ValueError):
    pass

class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if self.hidden == 0 and data.strip():
            self.parts.append(data.strip())

def parse_snapshot(raw: str, code: str, target: str):
    if not re.fullmatch(r"BK\d{4,6}", code):
        raise ValueError("非官方BK格式，拒绝数据")
    h = Visible()
    h.feed(raw)
    text = " | ".join(h.parts)
    header = (r"行业板块\s*\|\s*·\s*\|\s*" + re.escape(code) +
              r"\s*\|\s*· CN\s*\|\s*([^|]{1,60})\s*\|")
    identity = re.search(header, text)
    if not identity:
        raise ValueError("页面BK代码与真实板块身份未核验")
    m = re.search(
        r"沪深 A 股行业板块与概念板块，交易时段每 5 分钟刷新，展示涨跌幅、成交额、涨跌家数与领涨股。"
        r"\s*\|\s*([\d,]+(?:\.\d+)?)\s*\|\s*([+-]?\d+(?:\.\d+)?)%\s*\|"
        r"\s*更新于\s*\|\s*(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})", text)
    if not m:
        raise ValueError("缺少可核验板块收盘、涨跌及交易日")
    close = float(m.group(1).replace(",", ""))
    pct = float(m.group(2))
    day, clock = m.group(3), m.group(4)
    if day != target or clock < "15:00":
        raise SnapshotNotFresh("数据日为 " + day + " " + clock + "，目标收盘日 " + target)
    if not (0 < close < 1000000 and -40 < pct < 40):
        raise ValueError("指数价格或涨跌幅越界")
    second = re.search(r"收盘\s*\|\s*([\d,]+(?:\.\d+)?)", text)
    if not second or abs(float(second.group(1).replace(",", "")) - close) > .011:
        raise ValueError("页面头部和收盘区的价格无法相互核对")
    return dict(close=close, pct=pct, day=day, timestamp=day + " " + clock,
                label=identity.group(1).strip())

def download(code):
    req = Request(BASE + code.lower(), headers={
        "User-Agent":"Mozilla/5.0 (compatible; VerifiedAShareBKSnapshot/1.0)",
        "Accept":"text/html", "Accept-Language":"zh-CN,zh;q=0.9",
        "Accept-Encoding":"identity",
    })
    with urlopen(req, timeout=12) as response:
        if response.status != 200:
            raise IOError("快照HTTP状态 " + str(response.status))
        data = response.read(1800001)
    if len(data) > 1800000 or len(data) < 1000:
        raise IOError("快照空白或异常过大")
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if len(data) > 3_000_000:
        raise IOError("解压后快照体积异常")
    return data.decode("utf-8", errors="replace")

def reference_days(target: str):
    day = date.fromisoformat(target)
    out = {}
    count = 0
    for _ in range(90):
        day -= timedelta(days=1)
        st, _ = a_share_trading_status(day)
        if st == "unknown":
            raise ValueError("交易日历未知，不能计算区间收益")
        if st == "open":
            count += 1
            if count in (5, 20):
                out[count] = day.isoformat()
            if count >= 20:
                break
    return out

def historical_prices(name, history_dir):
    import hashlib
    code = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
    path = Path(history_dir) / (code + ".csv")
    prices = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    d = str(row.get("date") or "")[:10]
                    p = float(row.get("close") or 0)
                    if re.fullmatch(r"\d{4}-\d\d-\d\d", d) and p > 0:
                        prices[d] = p
                except (TypeError, ValueError):
                    continue
    except OSError:
        pass
    return prices

def apply_snapshot(board, quote, refs, history_dir):
    before = str(board.get("history_asof") or "")
    if before >= quote["day"]:
        return False
    prices = historical_prices(board["name"], history_dir)
    def ret(days):
        ref = prices.get(refs.get(days, ""), 0)
        return round((quote["close"] / ref - 1) * 100, 3) if ref > 0 else None
    board.update({
        "last":round(quote["close"], 3),
        "change_pct":round(quote["pct"], 3),
        "history_asof":quote["day"], "history_close":round(quote["close"], 3),
        "quote_asof":quote["day"], "ret5":ret(5), "ret20":ret(20),
        "quote_basis":"third_party_verified_eastmoney_BK_close",
        "quote_source":SOURCE, "quote_source_url":BASE + str(board["code"]).lower(),
        "quote_source_label":quote["label"], "source_time":quote["timestamp"],
        "history_ohlcv_asof":max(prices.keys()) if prices else "",
        "ohlcv_complete_to_target":False,
        "ma20":None, "ma60":None, "trend":None, "volume_ratio":None,
    })
    return True

def update_missing(boards, *, expected_day, history_dir, sleep_seconds=1.2):
    """尝试一次备用源。前5个均未返回目标收盘日时立刻止损；无重复轮询。"""
    pending = [b for b in boards if b.get("name") and b.get("history_asof", "") < expected_day
               and re.fullmatch(r"BK\d{4,6}", str(b.get("code") or ""))]
    if not pending:
        return dict(recovered=0, attempted=0, stale_source=False, failed=[])
    refs = reference_days(expected_day)
    failures = []
    updated = 0
    attempts = 0
    consecutive_network_errors = 0
    preflight_confirmed = False
    for board in pending:
        if not preflight_confirmed and attempts >= 5:
            print("备用源前5个板块均未核验到最新收盘日；跳过余下请求", flush=True)
            break
        if consecutive_network_errors >= 5:
            print("备用源连续5个请求出现网络错误；停止本轮采集", flush=True)
            break
        attempts += 1
        try:
            quote = parse_snapshot(download(board["code"]), board["code"], expected_day)
            preflight_confirmed = True
            if apply_snapshot(board, quote, refs, history_dir):
                updated += 1
                print("BACKUP_VERIFIED",board["name"],board["code"],
                      quote["day"],quote["close"],quote["pct"],flush=True)
            consecutive_network_errors = 0
        except Exception as exc:
            failures.append(dict(name=board["name"],code=board["code"],
                                 error=type(exc).__name__+": "+str(exc)[:150]))
            if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                consecutive_network_errors += 1
            else:
                consecutive_network_errors = 0
            print("BACKUP_UNAVAILABLE",board["name"],type(exc).__name__,str(exc)[:110],flush=True)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return dict(recovered=updated,attempted=attempts,
                stale_source=not preflight_confirmed, failed=failures[:40])
