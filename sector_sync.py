#!/usr/bin/env python3
"""GitHub Actions 同步东方财富 A 股行业板块行情。

数据输出（与 stock_ai_web.py v2.19 兼容）：
  data/sectors/quotes.json                  行业行情、近5/20日收益
  data/sectors/symbols.json                 A股代码 -> 东方财富真实行业名称
  data/sectors/history/<name-sha20>.csv    行业OHLCV日K（date,open,high,low,close,volume）
失败不覆写已有行情；盘中未完成的当日日K不进入历史序列。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import akshare as ak
import pandas as pd

from tools.github_schedule_trigger import a_share_trading_status

CN = timezone(timedelta(hours=8))
OUT = Path(__file__).resolve().parent / 'data' / 'sectors'
HIST = OUT / 'history'


def clock():
    return datetime.now(CN)


def stamp():
    return clock().strftime('%Y-%m-%d %H:%M:%S')


def digest(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]


def number(value):
    try:
        if pd.isna(value):
            return None
        value = float(str(value).replace(',', '').replace('%', '').strip())
        if not -1e14 < value < 1e14:
            return None
        return round(value, 3)
    except (TypeError, ValueError):
        return None


def load_json(path):
    try:
        data = json.loads(path.read_text('utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    tmp.replace(path)



# 统一行情请求节流；一旦连续出现东方财富连接拒绝/限流，就熔断本轮任务，
# 而不是将失败请求放大到 496 个板块（先前会产生近千次请求）。
_API_LAST = 0.0
_API_SUSPECT = 0
_API_BLOCKED = False
_API_CALLS = 0
_API_GAP = max(1.0, min(float(os.environ.get('SECTOR_REQUEST_GAP_SECONDS', '2.2')), 15.0))

class MarketRateLimited(RuntimeError):
    """上游临时限流，必须结束本轮采集，不能继续重试整个板块池。"""

def _throttling_error(exc):
    msg = str(exc).lower()
    return any(key in msg for key in (
        'push2', '限流', '频繁', 'rate limit', 'too many requests',
        'remotedisconnected', 'remote end closed', 'connection aborted',
        'connection reset', '远程主机', '断开连接', '暂时无法连接'
    ))

def _call_market(fn, *args, **kwargs):
    global _API_LAST, _API_SUSPECT, _API_BLOCKED, _API_CALLS
    if _API_BLOCKED:
        raise MarketRateLimited('行情源连续限流，本轮已熔断；保留已有有效数据')
    gap = _API_GAP - (time.monotonic() - _API_LAST)
    if gap > 0:
        time.sleep(gap)
    _API_LAST = time.monotonic()
    _API_CALLS += 1
    try:
        data = fn(*args, **kwargs)
        _API_SUSPECT = 0
        return data
    except Exception as exc:
        if isinstance(exc, MarketRateLimited) or _throttling_error(exc):
            # 一旦确认历史或实时报价源不可用，立即停止本轮，防止扩大为上百次请求。
            _API_SUSPECT += 1
            _API_BLOCKED = True
            raise MarketRateLimited('东方财富行情源拒绝请求，本轮立即熔断：' + str(exc)[:145]) from None
        raise

def _expected_day():
    """仅将交易所已知休市日与 15:20 盘后收盘判定作为目标日期。"""
    now = clock()
    day = now.date()
    if (now.hour, now.minute) < (15, 20):
        day -= timedelta(days=1)
    for _ in range(18):
        state, _ = a_share_trading_status(day)
        if state == 'open':
            return day.isoformat()
        if state == 'unknown':
            return ''
        day -= timedelta(days=1)
    return ''

def _history_asof(name):
    file = HIST / (digest(name) + '.csv')
    if not file.exists():
        return ''
    try:
        # csv 正常以日期升序写入；只读取日期列即可校验实际收盘交易日。
        data = pd.read_csv(file, usecols=['date'])
        if data.empty:
            return ''
        return str(data['date'].iloc[-1])[:10]
    except (OSError, ValueError, KeyError, IndexError):
        return ''

# 官方行业白名单由 data/sectors/selection.json 的17个看板分组维护，
# 额外保留 stock_symbols.txt 中158只已跟踪股票实际所属行业；
# 原始板块列表仅用于获取官方代码，绝不再把496个行业全部缓存。
def _selection():
    conf = load_json(OUT / 'selection.json')
    groups = conf.get('groups')
    if not isinstance(groups, dict) or len(groups) != 17:
        raise ValueError('行业白名单缺失或不是17个看板分组，已拒绝清理数据')
    references = {
        name.strip() for values in groups.values() if isinstance(values, list)
        for name in values if isinstance(name, str) and name.strip()
    }
    stock_file = OUT.parent.parent / 'stock_symbols.txt'
    try:
        raw = stock_file.read_text('utf-8')
    except OSError:
        raise ValueError('未找到stock_symbols.txt，拒绝删除任何行业历史') from None
    stocks = {line.strip() for line in raw.splitlines()
              if re.fullmatch(r'\d{6}', line.strip())}
    if not stocks:
        raise ValueError('跟踪股票清单为空，拒绝清理行业历史')
    raw_mapping = load_json(OUT / 'symbols.json').get('symbols', {})
    if not isinstance(raw_mapping, dict):
        raise ValueError('股票官方行业映射无效，拒绝清理历史数据')
    tracked = {str(raw_mapping[code]).strip() for code in stocks
               if isinstance(raw_mapping.get(code), str) and str(raw_mapping[code]).strip()}
    allowed = references | tracked
    if len(allowed) < 30:
        raise ValueError('有效行业白名单不足30个，拒绝危险清理')
    missing_codes = sorted(stocks - raw_mapping.keys())
    if missing_codes:
        print('警告：有股票尚未映射到官方行业：' + ','.join(missing_codes[:12]),flush=True)
    return allowed, stocks, references, tracked

def _tracked_sectors():
    """返回官方行业白名单；17个分组为展示口径，不冒充真实指数。"""
    return _selection()[0]

def _prune_storage(allowed, tracked_codes):
    """只清理仓库托管的行业行情及映射，不接触个股日线或两市指数。"""
    old_quotes = load_json(OUT / 'quotes.json')
    old_rows = old_quotes.get('boards', [])
    removed_quotes = 0
    if isinstance(old_rows, list):
        selected = [item for item in old_rows if isinstance(item, dict)
                    and item.get('name') in allowed]
        removed_quotes = len(old_rows) - len(selected)
        if removed_quotes:
            new_quotes = dict(old_quotes)
            new_quotes['boards'] = selected
            new_quotes['selection_policy'] = '17个看板分组与跟踪个股官方行业'
            save_json(OUT / 'quotes.json', new_quotes)
    symbols_obj = load_json(OUT / 'symbols.json')
    existing = symbols_obj.get('symbols', {})
    removed_symbols = 0
    if isinstance(existing, dict):
        filtered = {str(code): sector for code, sector in existing.items()
                    if code in tracked_codes and sector in allowed}
        removed_symbols = len(existing) - len(filtered)
        if removed_symbols:
            next_symbols = dict(symbols_obj)
            next_symbols['symbols'] = filtered
            next_symbols['selection_policy'] = 'stock_symbols.txt当前跟踪股票'
            save_json(OUT / 'symbols.json', next_symbols)
    # 历史文件 SHA 由行业真实名称生成，避免删除任何保留行业的缓存。
    keep_hashes = {digest(name) for name in allowed}
    removed_files = 0
    if HIST.exists():
        for file in HIST.glob('*.csv'):
            if file.stem not in keep_hashes:
                file.unlink()
                removed_files += 1
    report = {'boards_removed': removed_quotes, 'symbols_removed': removed_symbols,
              'history_files_removed': removed_files,
              'selected_official_names': len(allowed)}
    print('行业白名单整理：'+json.dumps(report,ensure_ascii=False),flush=True)
    return report


def live_quotes():
    df = _call_market(ak.stock_board_industry_name_em)
    if df is None or len(df) < 30:
        raise ValueError('有效行业数量少于30，拒绝覆写行情')
    rows = []
    for _, r in df.iterrows():
        row = {str(k).strip(): v for k, v in r.items()}
        name = str(row.get('板块名称') or row.get('名称') or '').strip()
        if not name or name.lower() == 'nan':
            continue
        rows.append({
            'name': name, 'code': str(row.get('板块代码') or row.get('代码') or '').strip(),
            'change_pct': number(row.get('涨跌幅')), 'last': number(row.get('最新价')),
            'turnover': number(row.get('成交额')), 'volume': number(row.get('成交量')),
            'turnover_rate': number(row.get('换手率')),
            'up': number(row.get('上涨家数')), 'down': number(row.get('下跌家数')),
            'leader': str(row.get('领涨股票') or row.get('领涨股') or '')[:45],
            'leader_pct': number(row.get('领涨股票-涨跌幅') or row.get('领涨股涨跌幅')),
        })
    if len(rows) < 30:
        raise ValueError('有效行业数据不足30项')
    return rows


def _direct_industry_history(board_code, start_day, now):
    """直接向东财官方历史日K端点按BK代码取数，跳过需全市场列表的push2接口。

    本接口使用已核验过的板块代码；与同花顺等不同分类的指数不混用。
    网络中断只请求一次，不能连锁访问一百多个被限制的接口。
    """
    code = str(board_code or '').strip()
    if not re.fullmatch(r'BK\d{4,6}', code):
        raise ValueError('无可信的东方财富BK行业代码，不能调用直接历史端点')
    params = {
        'secid': '90.' + code, 'klt':'101', 'fqt':'0',
        'beg':start_day.strftime('%Y%m%d'),
        'end':(now + timedelta(days=1)).strftime('%Y%m%d'),
        'fields1':'f1,f2,f3,f4,f5,f6',
        'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'ut':'fa5fd1943c7b386f172d6893dbfba10b',
    }
    url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + urlencode(params)
    request = Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; VerifiedSectorDaily/1.0)',
                                    'Referer':'https://quote.eastmoney.com/',
                                    'Accept':'application/json'})
    try:
        with urlopen(request,timeout=14) as resp:
            if resp.status != 200:
                raise MarketRateLimited('东方财富历史日K接口拒绝HTTP '+str(resp.status))
            body = resp.read(2_000_001)
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise MarketRateLimited('东方财富官方历史日K连接失败：'+str(exc)[:120]) from None
    if len(body) > 2_000_000 or not body:
        raise MarketRateLimited('东方财富官方历史日K响应为空或异常超长')
    try:
        payload = json.loads(body.decode('utf-8-sig'))
    except (UnicodeError, ValueError):
        raise MarketRateLimited('东方财富官方历史日K响应不是合法JSON') from None
    if not isinstance(payload,dict):
        raise MarketRateLimited('东方财富官方历史日K响应格式异常')
    data = payload.get('data')
    if not isinstance(data,dict):
        raise MarketRateLimited('官方行业历史日K接口为空响应或暂不可用，限流熔断：'+code)
    response_code = str(data.get('code') or '').strip()
    if response_code and response_code != code:
        raise ValueError('历史板块代码不一致，拒绝混淆行情：'+code)
    rows = data.get('klines')
    if not isinstance(rows,list) or not rows:
        raise ValueError('行业历史日K没有有效数据：'+code)
    parsed = []
    for raw in rows:
        parts = str(raw).split(',')
        if len(parts) < 6:
            continue
        parsed.append(parts[:6])
    if not parsed:
        raise ValueError('行业历史日K无有效OHLCV记录：'+code)
    return pd.DataFrame(parsed, columns=['日期','开盘','收盘','最高','最低','成交量'])


def history(name, board_code=None):
    now = clock()
    # 读取已核验旧历史；日常只请求增量，避免每天反复下载九年日K。
    dest = HIST / (digest(name) + '.csv')
    old_frame = None
    start_day = now - timedelta(days=365 * 9)
    if dest.is_file():
        try:
            cached = pd.read_csv(dest)
            if {'date','open','high','low','close','volume'}.issubset(cached.columns) and len(cached) >= 260:
                old_frame = cached.copy()
                old_frame['date'] = pd.to_datetime(old_frame['date'],errors='coerce')
                newest = old_frame['date'].dropna().max()
                if pd.notna(newest):
                    start_day = max(start_day, newest.to_pydatetime().replace(tzinfo=now.tzinfo) - timedelta(days=15))
        except (OSError,ValueError,TypeError,KeyError):
            old_frame = None
    if board_code and re.fullmatch(r'BK\d{4,6}', str(board_code)):
        # 直连push2his历史端点，绕过经常被限流的push2行业列表接口。
        df = _call_market(_direct_industry_history, board_code, start_day, now)
    else:
        raise ValueError('缺少历史BK板块代码，拒绝触发全市场列表请求')
    required = {'日期', '开盘', '最高', '最低', '收盘', '成交量'}
    if df is None or df.empty or not required.issubset(set(df.columns)):
        raise ValueError('缺少行业历史OHLCV字段（日期/开盘/最高/最低/收盘/成交量）')
    frame = pd.DataFrame({
        'date': pd.to_datetime(df['日期'], errors='coerce'),
        'open': pd.to_numeric(df['开盘'], errors='coerce'),
        'high': pd.to_numeric(df['最高'], errors='coerce'),
        'low': pd.to_numeric(df['最低'], errors='coerce'),
        'close': pd.to_numeric(df['收盘'], errors='coerce'),
        'volume': pd.to_numeric(df['成交量'], errors='coerce')})
    frame = frame.dropna()
    frame = frame.loc[(frame['open'] > 0) & (frame['high'] > 0) &
                      (frame['low'] > 0) & (frame['close'] > 0) &
                      (frame['volume'] >= 0) &
                      (frame['high'] >= frame[['open','close','low']].max(axis=1)) &
                      (frame['low'] <= frame[['open','close','high']].min(axis=1))]
    frame = frame.drop_duplicates('date', keep='last').sort_values('date')
    if old_frame is not None:
        frame = pd.concat([old_frame, frame],ignore_index=True)
        frame = frame.dropna(subset=['date','close'])
        frame = frame.drop_duplicates('date',keep='last').sort_values('date')
    frame = frame[frame['date'].dt.date <= now.date()]
    if (now.hour, now.minute) < (15, 20):
        frame = frame[frame['date'].dt.date < now.date()]
    if len(frame) < 2:
        raise ValueError('行业可核验的收盘记录不足2个交易日')
    if dest.exists():
        old = pd.read_csv(dest, usecols=['date', 'close'])
        old_dates = pd.to_datetime(old['date'], errors='coerce').dropna()
        if not old_dates.empty and frame.iloc[-1]['date'] < old_dates.max():
            raise ValueError('新行情日期早于现有文件，保留原行情')
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.csv.tmp')
    frame.to_csv(tmp, index=False, date_format='%Y-%m-%d', encoding='utf-8-sig')
    tmp.replace(dest)
    return len(frame)


def members(name, board_code=None):
    df = _call_market(ak.stock_board_industry_cons_em,symbol=(board_code if board_code and str(board_code).startswith('BK') else name))
    if df is None or df.empty or '代码' not in df:
        raise ValueError('行业成分股列表为空')
    codes = set()
    for raw in df['代码']:
        value = str(raw).strip().split('.')[0].zfill(6)
        if re.fullmatch(r'\d{6}', value):
            codes.add(value)
    if not codes:
        raise ValueError('无法识别行业成分股代码')
    return codes


def fetch_both(name, board_code=None):
    hist_ok = False
    codes = None
    errors = []
    try:
        n = history(name, board_code)
        hist_ok = True
        print(f'{name}: 历史日K {n} 条', flush=True)
    except Exception as exc:
        errors.append('history: ' + str(exc)[:160])
    time.sleep(.3)
    try:
        codes = members(name, board_code)
    except Exception as exc:
        errors.append('members: ' + str(exc)[:160])
    return name, hist_ok, codes, errors


def _history_may_replace_quote(history_date, quote_date):
    """历史日K只可以覆盖同日或更旧的快照，禁止日期回退。"""
    return str(history_date or '') >= str(quote_date or '')


def returns(boards, *, expected='', use_history_prices=False):
    """从实测完整收盘K线计算5/20日表现；报价失败仍可用真实日线收盘补齐。"""
    for b in boards:
        file = HIST / (digest(b['name']) + '.csv')
        if not file.is_file():
            continue
        try:
            frame = pd.read_csv(file, usecols=['date', 'close', 'volume']).dropna().sort_values('date')
            v = pd.to_numeric(frame['close'], errors='coerce')
            volume = pd.to_numeric(frame['volume'], errors='coerce')
            hist_date = str(frame.iloc[-1]['date'])[:10] if not frame.empty else ''
            # 重要：11月24日曾因旧CSV覆盖11月20日已核验快照；永远禁止回退日期。
            if not _history_may_replace_quote(hist_date, b.get('history_asof')):
                continue
            if len(v) >= 2 and v.iloc[-2] > 0 and (
                    use_history_prices or (expected and hist_date >= expected)):
                b['change_pct'] = round((v.iloc[-1] / v.iloc[-2] - 1) * 100, 3)
                b['last'] = round(float(v.iloc[-1]), 3)
                b['quote_asof'] = hist_date
                b['quote_basis'] = 'official_daily_close'
            # 行情交易日不能依赖21根K线才能更新；新板块历史不足时只把
            # 实际可核验的收益显示出来，其余留空（不能沿用上次旧的5/20日值）。
            if len(v) >= 2 and v.iloc[-1] > 0:
                b['history_asof'] = hist_date
                b['history_close'] = round(float(v.iloc[-1]), 3)
                b['ret5'] = (round((v.iloc[-1] / v.iloc[-6] - 1) * 100, 3)
                             if len(v) >= 6 and v.iloc[-6] > 0 else None)
                b['ret20'] = (round((v.iloc[-1] / v.iloc[-21] - 1) * 100, 3)
                              if len(v) >= 21 and v.iloc[-21] > 0 else None)
            if len(v) >= 60:
                ma20 = float(v.iloc[-20:].mean())
                ma60 = float(v.iloc[-60:].mean())
                close = float(v.iloc[-1])
                b['ma20'] = round(ma20, 3)
                b['ma60'] = round(ma60, 3)
                if close >= ma20 >= ma60:
                    b['trend'] = 'bullish'
                elif close <= ma20 <= ma60:
                    b['trend'] = 'bearish'
                elif close >= ma20:
                    b['trend'] = 'above_ma20'
                else:
                    b['trend'] = 'below_ma20'
            if len(volume) >= 6:
                prior_avg = float(volume.iloc[-6:-1].mean())
                if prior_avg > 0:
                    b['volume_ratio'] = round(float(volume.iloc[-1]) / prior_avg, 3)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            continue



def _write_status(payload):
    save_json(OUT / 'status.json', payload)

def _validate_status():
    state = load_json(OUT / 'status.json')
    # 防止读取其他工作流上次残留的成功状态。
    current_run = os.environ.get('GITHUB_RUN_ID', '').strip()
    if current_run and str(state.get('github_run_id', '')) != current_run:
        print('::error::未找到本次GitHub任务的采集状态，拒绝误报成功', flush=True)
        return 2
    if not state.get('ok'):
        print('::error::行业行情未达到最新交易日覆盖标准：' +
              str(state.get('reason') or '采集不完整'), flush=True)
        return 2
    print('行业采集质量验收通过：' + str(state.get('reason', '')), flush=True)
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quotes-only', action='store_true')
    parser.add_argument('--prune-only', action='store_true',
                        help='仅清理非看板及非个股所需行业的行情文件，不发起网络请求')
    parser.add_argument('--validate-status', action='store_true',
                        help='发布完成后校验本轮数据质量；缺数或熔断让Actions显示失败')
    parser.add_argument('--workers', type=int, default=1,
                        help='兼容旧参数；限流保护强制串行，不再并发访问行情源')
    parser.add_argument('--max-boards', type=int, default=140,
                        help='默认一次覆盖所有117个保留板块，避免多轮重复触发')
    parser.add_argument('--max-members', type=int, default=4,
                        help='每轮至多更新的成分股板块数')
    args = parser.parse_args()
    if args.validate_status:
        return _validate_status()
    OUT.mkdir(parents=True, exist_ok=True)
    allowed, tracked_codes, references, tracked = _selection()
    cleanup = _prune_storage(allowed, tracked_codes)
    if args.prune_only:
        return 0
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    expected = _expected_day()
    old_status = load_json(OUT / 'status.json')
    attempts = dict(old_status.get('history_last_attempt') or {})
    attempts = {str(k): str(v) for k,v in attempts.items()}
    base_status = {
        'run_at': stamp(), 'github_run_id': run_id,
        'expected_trading_day': expected, 'rate_limited': False,
        'request_count': 0, 'ok': False,
    }
    prior = load_json(OUT / 'quotes.json')
    new_live = False
    boards = [dict(item) for item in prior.get('boards', [])
              if isinstance(item, dict) and item.get('name') in allowed]
    cached_codes_ok = len(boards) >= 30 and all(
        re.fullmatch(r'BK\d{4,6}',str(x.get('code') or '')) for x in boards)
    if not args.quotes_only and cached_codes_ok:
        # 每日盘后无需重新查询push2全市场行业列表，直接按缓存的官方BK代码查真实日K。
        print('复用已验证BK行业代码：',len(boards),
              '；本轮只采一次官方收盘日K，不重复拉取全市场实时列表',flush=True)
    else:
        try:
            boards = [b for b in live_quotes() if b.get('name') in allowed]
            if len(boards) < 30:
                raise ValueError('可用白名单行业不足30个，保留历史数据')
            new_live = True
            print('行情快照读取成功：', len(boards), stamp(), flush=True)
        except Exception as exc:
            print('行情快照失败，旧数据保留：', str(exc)[:250], flush=True)
            boards = [dict(item) for item in prior.get('boards', [])
                      if isinstance(item, dict) and item.get('name') in allowed]
            if len(boards) < 30:
                _write_status({**base_status, 'mode': 'quotes' if args.quotes_only else 'full',
                               'reason': '无法获取有效行业快照且本地没有可用数据',
                               'rate_limited': _API_BLOCKED, 'request_count': _API_CALLS})
                return 0
    if args.quotes_only:
        # 盘中报价与历史日线日期分开表示；绝不冒充最新完整日K。
        if new_live:
            older = {r['name']: r for r in prior.get('boards', [])
                     if isinstance(r, dict) and r.get('name')}
            for board in boards:
                before = older.get(board['name'], {})
                for key in ('ret5', 'ret20', 'history_asof', 'history_close',
                            'ma20', 'ma60', 'trend', 'volume_ratio'):
                    if key in before:
                        board[key] = before[key]
            save_json(OUT / 'quotes.json', {
                'ok': True, 'source': '东方财富行业板块行情 / AKShare',
                'fetched_at': stamp(),
                'session': 'intraday' if (clock().hour, clock().minute) < (15, 20) else 'final_or_delayed',
                'boards': boards
            })
        _write_status({**base_status, 'mode': 'quotes', 'ok': new_live,
                       'quotes_updated': new_live, 'board_count': len(boards),
                       'request_count': _API_CALLS,
                       'reason': '盘中报价成功；历史日K另行校验' if new_live else '报价源失败，保留旧快照'})
        return 0

    names = sorted({b['name'] for b in boards if isinstance(b, dict) and b.get('name')})
    board_codes = {str(b.get('name')): str(b.get('code') or '')
                   for b in boards if isinstance(b, dict)}
    target_sectors = _tracked_sectors()
    core = sorted(set(names) & target_sectors)
    if not expected:
        _write_status({**base_status, 'mode': 'full', 'board_count': len(names),
                       'reason': '未能确认最近完整交易日，拒绝将旧行情标记为最新',
                       'request_count': _API_CALLS})
        return 0

    # 优先使用东方财富官方批量BK收盘报价：117板块只需两次请求，
    # 与逐板块历史OHLCV独立；本日已验证收盘价不再回退。
    batch = {'recovered':0,'attempted':0,'valid_rows':0,'failed':[]}
    try:
        from tools.sector_batch_snapshot import update as update_batch_closes
        batch = update_batch_closes(
            boards, expected_day=expected, history_dir=HIST,
            close_dir=OUT / 'close_history', chunk_size=3)
    except Exception as exc:
        batch['failed'] = [{'error':'官方批量收盘请求失败：'+type(exc).__name__+': '+str(exc)[:180]}]
        print('批量BK收盘数据失败：',str(exc)[:180],flush=True)
    batch_fresh = sum(str(b.get('history_asof') or '') >= expected for b in boards)
    if batch['recovered']:
        save_json(OUT / 'quotes.json', {
            'ok':True, 'source':'东方财富官方BK批量收盘报价（不包含完整OHLCV）',
            'fetched_at':stamp(), 'session':'final_or_delayed',
            'expected_trade_date':expected, 'verified_batch_count':batch['recovered'],
            'boards':boards,
        })
    if batch_fresh == len(names) and batch['recovered']:
        full_history_fresh = sum(_history_asof(name) >= expected for name in names)
        _write_status({
            **base_status, 'ok':True, 'mode':'full',
            'reason':(f'{batch_fresh}/{len(names)}个板块已由东方财富官方批量报价'
                      f'核验到{expected}收盘；完整OHLCV已覆盖{full_history_fresh}个'),
            'board_count':len(names), 'quote_fresh_count':batch_fresh,
            'quote_stale_count':0, 'history_fresh_count':full_history_fresh,
            'history_pending_count':len(names)-full_history_fresh,
            'ohlcv_complete':full_history_fresh==len(names),
            'official_batch_recovered':batch['recovered'],
            'official_batch_requests':batch['attempted'], 'official_batch_failures':batch['failed'],
            'quotes_updated':True, 'request_count':batch['attempted'],
        })
        print('行业行情一次批量同步完成，'+str(batch_fresh)+'个已核验到最新收盘日；'
              '无需继续向上游逐板块请求',flush=True)
        return 0

    old_dates = {name: _history_asof(name) for name in names}
    board_by_name={b['name']:b for b in boards}
    pending = [name for name in names if old_dates[name] < expected and
               str(board_by_name[name].get('history_asof') or '') < expected]
    # 核心行业优先；未命中的板块按上一次尝试时间轮转，避免每轮只请求同一批失败对象。
    pending.sort(key=lambda name: (
        0 if name in target_sectors else 1,
        attempts.get(name, ''), old_dates.get(name, ''), name
    ))
    limit = max(1, min(args.max_boards, 150))
    selected = pending[:limit]
    previous_symbols = load_json(OUT / 'symbols.json').get('symbols', {})
    mapping = {code:industry for code,industry in previous_symbols.items()
               if code in tracked_codes and industry in allowed} if isinstance(previous_symbols, dict) else {}
    mapping_names = set(mapping.values())
    max_members = max(0, min(args.max_members, 12))
    history_success = 0
    history_fresh_success = 0
    members_success = 0
    failed = []
    attempted = []
    breaker_reason = ''
    for name in selected:
        if _API_BLOCKED:
            breaker_reason = '东方财富连续限流，已停止后续行业请求'
            break
        attempts[name] = stamp()
        attempted.append(name)
        try:
            total = history(name, board_codes.get(name))
            history_success += 1
            newest = _history_asof(name)
            if newest >= expected:
                history_fresh_success += 1
            else:
                failed.append({'name': name, 'error': '历史日线只到 ' + (newest or '未知') +
                               '，预期 ' + expected})
            print(f'{name}: 完整日K {total} 条，最新日期 {newest}，预期 {expected}', flush=True)
        except MarketRateLimited as exc:
            breaker_reason = str(exc)
            failed.append({'name': name, 'error': breaker_reason})
            break
        except Exception as exc:
            failed.append({'name': name, 'error': 'history: ' + str(exc)[:170]})
            print(f'{name}: 历史抓取失败：{str(exc)[:140]}', flush=True)
            if _API_BLOCKED:
                breaker_reason = '行情上游连续限流'
                break
        # 成分映射未建立才优先补全，且每轮最多4个，避免每个历史请求再追加一个高频API。
        if (tracked_codes - mapping.keys()) and members_success < max_members and name not in mapping_names and not _API_BLOCKED:
            try:
                codes = members(name, board_codes.get(name))
                mapping = {code: sector for code, sector in mapping.items() if sector != name}
                mapping.update({code: name for code in codes if code in tracked_codes})
                mapping_names.add(name)
                members_success += 1
            except MarketRateLimited as exc:
                breaker_reason = str(exc)
                failed.append({'name': name, 'error': 'members: ' + breaker_reason})
                break
            except Exception as exc:
                failed.append({'name': name, 'error': 'members: ' + str(exc)[:160]})
                if _API_BLOCKED:
                    breaker_reason = '行情上游连续限流'
                    break

    if members_success:
        save_json(OUT / 'symbols.json', {
            'ok': True, 'source': '东方财富行业成分股 / AKShare',
            'fetched_at': stamp(), 'symbols': mapping
        })
    returns(boards, expected=expected, use_history_prices=not new_live)
    # 官网API达到单日限流时，最多一次调用有严格BK代码/真实收盘日期验证的备用源；
    # 备用源若仍停留于旧交易日，前5个未通过即停止，不再循环重试。
    backup = {'recovered':0,'attempted':0,'stale_source':False,'failed':[]}
    if any(str(b.get('history_asof') or '') < expected for b in boards):
        try:
            from tools.sector_daily_backup import update_missing
            backup = update_missing(boards, expected_day=expected, history_dir=HIST)
        except Exception as exc:
            backup['failed'] = [{'error':'备用收盘快照处理失败：'+type(exc).__name__+': '+str(exc)[:150]}]
            print('备用行情校验失败：',str(exc)[:180],flush=True)
    # 仅发布实际可验的日期与涨跌；保留行情源和OHLCV完整性标识。
    if new_live or history_success or batch['recovered'] or backup['recovered']:
        save_json(OUT / 'quotes.json', {
            'ok': True,
            'source': '官方BK日K/官方批量收盘/第三方已核验快照（逐板块source字段注明）',
            'fetched_at': stamp(), 'session': 'final_or_delayed',
            'expected_trade_date': expected,
            'historical_recovery_only': bool(not new_live),
            'verified_backup_count':backup['recovered'],
            'boards': boards,
        })
    actual_dates = {name: _history_asof(name) for name in names}
    fresh_count = sum(d >= expected for d in actual_dates.values())
    missing_core = [name for name in core if actual_dates[name] < expected]
    remaining = [name for name in names if actual_dates[name] < expected]
    quote_fresh_count = sum(str(b.get('history_asof') or '') >= expected for b in boards)
    quote_stale_count = len(names) - quote_fresh_count
    # 严格区分「本轮有少量成功」与「实际跟踪的核心板块已达最近收盘日」。
    # 任何一次上游熔断或核心缺数都不能绿灯；数据可分批安全发布并继续补齐。
    ok = (not _API_BLOCKED and not missing_core and not remaining and
          (new_live or fresh_count == len(names)))
    if _API_BLOCKED:
        reason = (f'官方日K接口限流；备用收盘快照本轮核验更新{backup["recovered"]}个，'
                  f'当前报价已更新{quote_fresh_count}/{len(names)}个至{expected}；'
                  f'完整历史日K已更新{fresh_count}/{len(names)}个')
        if backup['stale_source']:
            reason += '；备用源自身尚未发布最新交易日'

    elif missing_core:
        reason = f'{len(missing_core)}个核心板块仍未更新到 {expected}（本轮已补采 {history_fresh_success} 个）'
    elif selected and history_fresh_success < max(1, int(len(attempted) * .8)):
        reason = f'本轮有效补采 {history_fresh_success}/{len(attempted)}，未达到80%验收线'
    elif not new_live and not history_success and remaining:
        reason = '官方历史日K未取得更新，旧日线仍保留'
    elif remaining:
        reason = f'核心行业行情已达 {expected}；其余 {len(remaining)} 个待分批补齐'
    else:
        reason = f'{len(names)}个行业最新交易日均已更新到 {expected}'
    status = {
        **base_status, 'mode': 'full', 'ok': bool(ok), 'reason': reason,
        'board_count': len(names), 'core_board_count': len(core),
        'core_stale_count': len(missing_core), 'core_stale_sample': missing_core[:40],
        'expected_trading_day': expected,
        'history_fresh_count': fresh_count, 'history_pending_count': len(remaining),
        'history_success': history_success, 'history_fresh_success': history_fresh_success,
        'history_attempted': len(attempted), 'history_selected': len(selected),
        'members_success': members_success,
        'failed_count': len(failed), 'failed': failed[:35],
        'rate_limited': _API_BLOCKED, 'breaker_reason': breaker_reason,
        'quotes_updated': bool(new_live or history_success or batch['recovered'] or backup['recovered']),
        'request_count': _API_CALLS,
        'quote_fresh_count': quote_fresh_count,
        'quote_stale_count': quote_stale_count,
        'official_batch_recovered': batch['recovered'],
        'official_batch_requests': batch['attempted'],
        'official_batch_failures': batch['failed'],
        'backup_recovered_count': backup['recovered'],
        'backup_attempted': backup['attempted'],
        'backup_source_stale': backup['stale_source'],
        'backup_failures': backup['failed'][:25],
        'history_last_attempt': {name: value for name,value in attempts.items() if name in allowed},
        'selected_group_count': 17, 'selection_official_count': len(allowed),
        'selected_stock_count': len(tracked_codes), 'prune_report': cleanup,
    }
    _write_status(status)
    print('行业分批采集：' + reason, flush=True)
    print(f'已核验最新报价 {quote_fresh_count}/{len(names)}；完整历史日K {fresh_count}/{len(names)}；'
          f'本轮备用源恢复 {backup["recovered"]}；官方请求 {_API_CALLS}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
