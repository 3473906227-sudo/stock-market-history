#!/usr/bin/env python3
"""GitHub Actions 同步东方财富 A 股行业板块行情。

数据输出（与 stock_ai_web.py v2.19 兼容）：
  data/sectors/quotes.json                  行业行情、近5/20日收益
  data/sectors/symbols.json                 A股代码 -> 东方财富真实行业名称
  data/sectors/history/<name-sha20>.csv    行业收盘日K（date,close）
失败不覆写已有行情；盘中未完成的当日日K不进入历史序列。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time

import akshare as ak
import pandas as pd

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


def live_quotes():
    df = ak.stock_board_industry_name_em()
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


def history(name, board_code=None):
    now = clock()
    df = ak.stock_board_industry_hist_em(
        symbol=(board_code if board_code and str(board_code).startswith('BK') else name), period='日k',
        start_date=(now - timedelta(days=365 * 9)).strftime('%Y%m%d'),
        end_date=(now + timedelta(days=1)).strftime('%Y%m%d'), adjust='')
    if df is None or df.empty or '日期' not in df or '收盘' not in df:
        raise ValueError('缺少行业历史日期或收盘价')
    frame = pd.DataFrame({
        'date': pd.to_datetime(df['日期'], errors='coerce'),
        'close': pd.to_numeric(df['收盘'], errors='coerce')})
    frame = frame.dropna().loc[lambda f: f['close'] > 0]
    frame = frame.drop_duplicates('date', keep='last').sort_values('date')
    frame = frame[frame['date'].dt.date <= now.date()]
    if (now.hour, now.minute) < (15, 20):
        frame = frame[frame['date'].dt.date < now.date()]
    if len(frame) < 260:
        raise ValueError('行业有效历史不足260交易日')
    dest = HIST / (digest(name) + '.csv')
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
    df = ak.stock_board_industry_cons_em(symbol=(board_code if board_code and str(board_code).startswith('BK') else name))
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


def returns(boards):
    for b in boards:
        file = HIST / (digest(b['name']) + '.csv')
        if not file.is_file():
            continue
        try:
            frame = pd.read_csv(file, usecols=['date', 'close']).dropna().sort_values('date')
            v = pd.to_numeric(frame['close'], errors='coerce')
            if len(v) >= 21 and v.iloc[-6] > 0 and v.iloc[-21] > 0:
                b['ret5'] = round((v.iloc[-1] / v.iloc[-6] - 1) * 100, 3)
                b['ret20'] = round((v.iloc[-1] / v.iloc[-21] - 1) * 100, 3)
                b['history_asof'] = str(frame.iloc[-1]['date'])[:10]
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quotes-only', action='store_true')
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    prior = load_json(OUT / 'quotes.json')
    new_live = False
    try:
        boards = live_quotes()
        new_live = True
        print('行情快照读取成功：', len(boards), stamp(), flush=True)
    except Exception as exc:
        print('行情快照失败，旧数据保留：', str(exc)[:250], flush=True)
        if args.quotes_only:
            return 2
        boards = prior.get('boards', [])
        if not isinstance(boards, list) or len(boards) < 30:
            return 2
    if args.quotes_only:
        older = {r['name']: r for r in prior.get('boards', []) if isinstance(r, dict) and r.get('name')}
        for b in boards:
            before = older.get(b['name'], {})
            for key in ('ret5', 'ret20', 'history_asof'):
                if key in before:
                    b[key] = before[key]
        save_json(OUT / 'quotes.json', {
            'ok': True, 'source': '东方财富行业板块行情 / AKShare',
            'fetched_at': stamp(),
            'session': 'intraday' if (clock().hour, clock().minute) < (15, 20) else 'final_or_delayed',
            'boards': boards})
        return 0

    names = sorted({b['name'] for b in boards if isinstance(b, dict) and b.get('name')})
    existing = load_json(OUT / 'symbols.json').get('symbols', {})
    mapping = dict(existing) if isinstance(existing, dict) else {}
    succeeded_hist = 0
    succeeded_members = 0
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        board_codes = {str(b.get('name')): str(b.get('code') or '') for b in boards if isinstance(b, dict)}
        jobs = {pool.submit(fetch_both, name, board_codes.get(name)): name for name in names}
        for job in as_completed(jobs):
            name, ok_hist, codes, errors = job.result()
            if ok_hist:
                succeeded_hist += 1
            if codes:
                succeeded_members += 1
                mapping = {code: industry for code, industry in mapping.items() if industry != name}
                for code in codes:
                    mapping[code] = name
            if errors:
                failed.append({'name': name, 'error': '; '.join(errors)})
            print(f'{name}: 历史={ok_hist}; 成分={len(codes) if codes else 0}; 错误={errors}', flush=True)
    if succeeded_members:
        save_json(OUT / 'symbols.json', {
            'ok': True, 'source': '东方财富行业成分股 / AKShare',
            'fetched_at': stamp(), 'symbols': mapping})
    returns(boards)
    if new_live:
        save_json(OUT / 'quotes.json', {
            'ok': True, 'source': '东方财富行业板块行情 / AKShare',
            'fetched_at': stamp(), 'session': 'final_or_delayed',
            'boards': boards})
    save_json(OUT / 'status.json', {
        'run_at': stamp(), 'mode': 'full', 'board_count': len(names),
        'history_success': succeeded_hist, 'members_success': succeeded_members,
        'failed_count': len(failed), 'failed': failed[:60],
        'quotes_updated': new_live})
    print(f'行业全量更新完成：历史 {succeeded_hist}/{len(names)}，成分 {succeeded_members}/{len(names)}', flush=True)
    return 0 if (succeeded_hist or succeeded_members) else 2


if __name__ == '__main__':
    raise SystemExit(main())
