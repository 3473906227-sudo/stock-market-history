#!/usr/bin/env python3
"""Fetch, merge and audit closed Eastmoney BK daily bars independently of quotes."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tools import apply_sector_pages
from tools.fetch_sector_page import HOSTS as SNAPSHOT_ENDPOINTS
from tools.github_schedule_trigger import a_share_trading_status
from tools.sector_batch_snapshot import _adjacent_trade_day, _history_file, _save_close_prices, validate_row
from tools.sector_daily_gate import CN, expected_close_day


OUT = apply_sector_pages.OUT
GAP_START = date(2026, 11, 18)
FIELDS = ('date', 'open', 'high', 'low', 'close', 'volume')
HOSTS = ('push2his.eastmoney.com', '33.push2his.eastmoney.com')
ENDPOINTS = tuple(f'https://{host}/api/qt/stock/kline/get' for host in HOSTS)
SOURCE = '东方财富官方BK不复权历史日K'
SNAPSHOT_SOURCE = '东方财富官方BK盘后完整OHLCV'


def trade_days(start, end):
    while start <= end:
        state, message = a_share_trading_status(start)
        if state == 'unknown':
            raise ValueError(message)
        if state == 'open':
            yield start.isoformat()
        start += timedelta(days=1)


def dates_for(target):
    end = date.fromisoformat(target)
    if end.isoformat() != target or target > expected_close_day(datetime.now(CN)):
        raise ValueError('目标必须是已收盘的A股交易日')
    if a_share_trading_status(end)[0] != 'open':
        raise ValueError(f'{target} 不是已核实的交易日')
    return list(trade_days(min(GAP_START, end), end))


def boards_from(data):
    boards = data.get('boards')
    if not isinstance(boards, list) or not 100 <= len(boards) <= 150:
        raise ValueError('仓库行业名单数量异常')
    codes, names = set(), set()
    for board in boards:
        code, name = board['code'], board['name']
        if not re.fullmatch(r'BK\d{4,6}', code) or not name.strip() or code in codes or name in names:
            raise ValueError(f'行业名单含无效或重复代码/名称: {code} {name}')
        codes.add(code)
        names.add(name)
    return boards


def bar(day, open_, high, low, close, volume):
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError(f'日K日期格式无效: {day}')
    values = (open_, high, low, close, volume)
    try:
        o, h, l, c, v = prices = tuple(Decimal(raw) for raw in values)
    except InvalidOperation as exc:
        raise ValueError(f'{day} 有非数值OHLCV字段') from exc
    if (not all(value.is_finite() for value in prices) or not 0 < l <= min(o, c)
            or not max(o, c) <= h < 1_000_000 or not 0 <= v < 1_000_000_000_000):
        raise ValueError(f'{day} 开高低收或成交量无效')
    return values


def read_history(path):
    try:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise ValueError(f'{path.name} 日K CSV字段不匹配')
            return {row['date']: tuple(row[key] for key in FIELDS[1:]) for row in reader}
    except FileNotFoundError:
        return {}


def valid_history(rows):
    for day, values in rows.items():
        try:
            bar(day, *values)
        except (TypeError, ValueError):
            continue
        # 旧档跨多年，只对现行年度的记录应用已维护的交易所日历。
        state, _ = a_share_trading_status(date.fromisoformat(day))
        if state != 'closed':
            yield day


def parse_payload(payload, board, target):
    data = payload.get('data') if isinstance(payload, dict) and payload.get('rc') == 0 else None
    if (not isinstance(data, dict) or data.get('code') != board['code']
            or str(data.get('market')) != '90' or data.get('name') != board['name']):
        raise ValueError('返回的东方财富市场、代码或板块名称不符')
    raw_rows = data.get('klines')
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError('东方财富历史日K为空')
    rows, rejected = {}, []
    for raw in raw_rows:
        try:
            day, open_, close, high, low, volume, *_ = raw.split(',')
            if date.fromisoformat(day).isoformat() != day or day > target:
                raise ValueError(f'{day} 超出已收盘日期范围')
            state, reason = a_share_trading_status(date.fromisoformat(day))
            if state == 'closed' or (state == 'unknown' and day >= GAP_START.isoformat()):
                raise ValueError(f'{day} {reason}')
            values = bar(day, open_, high, low, close, volume)
        except (AttributeError, TypeError, ValueError) as exc:
            rejected.append(str(exc))
            continue
        if day in rows and tuple(map(Decimal, rows[day])) != tuple(map(Decimal, values)):
            raise ValueError(f'{day} 同一板块日K返回冲突记录')
        rows[day] = values
    if not rows:
        raise ValueError('没有可验证的已收盘OHLCV')
    return rows, rejected


def verified_rows(payload, board, target, local, valid):
    rows, rejected = parse_payload(payload, board, target)
    overlap = rows.keys() & valid
    if valid and not overlap:
        raise ValueError('源数据与仓库完整历史没有共同交易日')
    conflicts = {day for day in overlap if tuple(map(Decimal, rows[day])) != tuple(map(Decimal, local[day]))}
    if conflicts:
        if target not in overlap - conflicts:
            raise ValueError(f'{min(conflicts)} 官方源与已存完整日K不一致')
        # A matching current bar plus independent close-only records can validate
        # a backfill even when the provider revises some already archived bars.
        stored_closes = read_close_history(board)
        references = (rows.keys() - valid) & stored_closes.keys()
        if not references or any(abs(float(rows[day][3]) - stored_closes[day]) > 0.011 for day in references):
            raise ValueError('历史重叠记录存在差异，补缺日期也未通过独立收盘价核验')
        rejected.extend(f'{day} 官方历史与已存完整日K不一致，保留原记录' for day in sorted(conflicts))
    return rows, rejected


def request_payload(endpoint, code, start, target):
    limit = max(10, (date.fromisoformat(target) - start).days + 1)
    params = {
        'secid': f'90.{code}', 'klt': '101', 'fqt': '0', 'lmt': limit,
        'end': target.replace('-', ''), 'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57',
    }
    if endpoint != ENDPOINTS[0]:
        params.update(beg=start.strftime('%Y%m%d'), ut='fa5fd1943c7b386f172d6893dbfba10b')
    request = Request(f'{endpoint}?{urlencode(params, safe=",.")}', headers={
        'User-Agent': 'Mozilla/5.0 (compatible; VerifiedBKHistory/1.0)',
        'Referer': 'https://quote.eastmoney.com/', 'Accept': 'application/json',
    })
    with urlopen(request, timeout=12) as response:
        body = response.read(1_000_001)
    if not body or len(body) > 1_000_000:
        raise ValueError('东方财富响应为空或过大')
    return json.loads(body.decode('utf-8-sig'))


def fetch_board(board, required):
    local = read_history(_history_file(board, OUT / 'history'))
    valid = set(valid_history(local))
    needed = set(required) - valid
    results, errors = [], []
    if needed:
        start = date.fromisoformat(min(needed)) - timedelta(days=10)
        for endpoint in ENDPOINTS:
            try:
                payload = request_payload(endpoint, board['code'], start, required[-1])
                rows, rejected = verified_rows(payload, board, required[-1], local, valid)
                results.append({'endpoint': endpoint, 'payload': payload})
                needed.difference_update(rows)
                errors.extend(rejected)
                if not needed:
                    break
            except (OSError, ValueError) as exc:
                errors.append(f'{endpoint}: {type(exc).__name__}: {str(exc)[:200]}')
            time.sleep(1)
    return {'responses': results, 'errors': errors, 'missing_dates': sorted(needed)}


def fetch_shard(target, index, count, output):
    required = dates_for(target)
    boards = sorted(boards_from(apply_sector_pages.load_json(OUT / 'quotes.json')), key=lambda b: b['code'])
    records = {}
    for board in boards[index::count]:
        result = fetch_board(board, required)
        records[board['code']] = result
        print(board['code'], board['name'], '缺失交易日', result['missing_dates'], result['errors'], flush=True)
        time.sleep(0.4)
    apply_sector_pages.atomic_json(output, {'target': target, 'boards': records})


def write_history(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(FIELDS)
        writer.writerows((day, *rows[day]) for day in sorted(rows))
    temp.replace(path)


def merge_board(board, local, valid, target, attempts, errors):
    additions, sources, conflicts = {}, {}, set()
    for response in attempts:
        try:
            endpoint = response['endpoint']
            if endpoint not in ENDPOINTS:
                raise ValueError('非受支持的东方财富历史源')
            rows, rejected = verified_rows(response['payload'], board, target, local, valid)
            errors.extend(rejected)
            for day in rows.keys() - valid:
                values = rows[day]
                if day in additions and tuple(map(Decimal, additions[day])) != tuple(map(Decimal, values)):
                    conflicts.add(day)
                additions[day], sources[day] = values, endpoint
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
    for day in sorted(conflicts):
        errors.append(f'{day} 不同官方历史响应不一致')
        del additions[day], sources[day]
    return additions, sources


def read_close_history(board):
    path = _history_file(board, OUT / 'close_history')
    closes = {}
    if path.exists():
        with path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                try:
                    day = row['date']
                    value = Decimal(row['close'])
                    if date.fromisoformat(day).isoformat() == day and value.is_finite() and 0 < value < 1_000_000:
                        closes[day] = float(value)
                except (InvalidOperation, KeyError, TypeError, ValueError):
                    continue
    return closes


def sync_close_history(board, additions):
    closes = read_close_history(board)
    closes.update({day: float(values[3]) for day, values in additions.items()})
    _save_close_prices(board, OUT / 'close_history', closes)


def quote_day(board):
    return board.get('quote_asof') or board.get('history_asof') or ''


def read_final_pages(directory):
    observations = {}
    for path in sorted(directory.glob('page_*.json')):
        page = apply_sector_pages.load_json(path)
        if page.get('ok') and page.get('endpoint') in SNAPSHOT_ENDPOINTS:
            for row in page.get('rows', []):
                if isinstance(row, dict) and all(field in row for field in ('f17', 'f15', 'f16', 'f5')):
                    observations.setdefault(row.get('f12'), []).append((page['endpoint'], row))
    return observations


def prior_quote_close(board, previous_day):
    if quote_day(board) == previous_day and str(board.get('quote_basis') or '').startswith('official'):
        return board.get('history_close') or board.get('last')
    return read_close_history(board).get(previous_day)


def snapshot_bar(board, row, target, prior_close):
    quote = validate_row(row, {board['code']}, target)
    if row['f14'] != board['name']:
        raise ValueError('盘后行情板块名称不匹配')
    previous = quote['previous_close']
    if (not prior_close or previous is None
            or not abs(float(prior_close) - previous) <= 0.011):
        raise ValueError('盘后快照昨收未与仓库独立记录核对一致')
    values = bar(target, *(str(row[key]) for key in ('f17', 'f15', 'f16', 'f2', 'f5')))
    close = float(values[3])
    if abs((close / previous - 1) * 100 - quote['pct']) > 0.02:
        raise ValueError('盘后快照收盘价、昨收价与当日涨幅不一致')
    return values


def verified_snapshot(board, target, prior_close, candidates, errors):
    observations = {}
    for endpoint, row in candidates:
        try:
            values = snapshot_bar(board, row, target, prior_close)
            observations[tuple(map(Decimal, values))] = values, endpoint
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            errors.append(str(exc))
    if len(observations) > 1:
        errors.append(f'{target} 官方不同盘后响应的OHLCV不一致')
    if len(observations) == 1:
        return next(iter(observations.values()))
    return None


def refresh_quote(board, local, valid, latest, endpoint, required):
    existing = quote_day(board)
    if not latest or latest < existing or latest < required[0]:
        return False
    sessions = list(trade_days(date.fromisoformat(latest) - timedelta(days=105), date.fromisoformat(latest)))
    closes = read_close_history(board)
    closes.update({day: float(local[day][3]) for day in sessions if day in valid})
    close = closes[latest]
    volume = float(local[latest][4])

    def rate(days):
        base = closes.get(sessions[-days - 1]) if len(sessions) > days else None
        return round((close / base - 1) * 100, 3) if base else None

    def average(size):
        recent = sessions[-size:]
        return sum(closes[day] for day in recent) / size if len(recent) == size and all(day in closes for day in recent) else None

    ma20, ma60 = average(20), average(60)
    prior = sessions[-6:-1]
    volume_base = (sum(float(local[day][4]) for day in prior) / 5
                   if len(prior) == 5 and all(day in valid for day in prior) else 0)
    trend = None
    if ma20 is not None and ma60 is not None:
        if close >= ma20 >= ma60:
            trend = 'bullish'
        elif close <= ma20 <= ma60:
            trend = 'bearish'
        else:
            trend = 'above_ma20' if close >= ma20 else 'below_ma20'
    previous = board.copy()
    if latest > existing or close != board.get('last') or board.get('quote_basis') == 'official_bk_previous_close':
        # These snapshot fields cannot be recovered from historical OHLCV.
        board.update(dict.fromkeys(('turnover', 'turnover_rate', 'up', 'down', 'leader', 'leader_pct')))
    snapshot = endpoint in SNAPSHOT_ENDPOINTS
    source_time = board.get('source_time') if snapshot and existing == latest else None
    board.update(last=close, volume=volume, change_pct=rate(1), ret5=rate(5), ret20=rate(20),
                 history_asof=latest, history_close=close, quote_asof=latest,
                 quote_basis='official_bk_eod_ohlcv' if snapshot else 'official_daily_close',
                 quote_source=SNAPSHOT_SOURCE if snapshot else SOURCE,
                 quote_source_url=endpoint if endpoint in ENDPOINTS + SNAPSHOT_ENDPOINTS else ENDPOINTS[0],
                 quote_source_label=board['name'],
                 source_time=source_time, trend=trend, ma20=round(ma20, 3) if ma20 else None,
                 ma60=round(ma60, 3) if ma60 else None,
                 volume_ratio=round(volume / volume_base, 3) if volume_base else None)
    return previous != board


def read_artifacts(directory, target):
    output = {}
    for file in sorted(directory.glob('*.json')):
        artifact = apply_sector_pages.load_json(file)
        if artifact.get('target') != target or not isinstance(artifact.get('boards'), dict):
            continue
        for code, record in artifact['boards'].items():
            aggregate = output.setdefault(code, {'responses': [], 'errors': []})
            aggregate['responses'].extend(record.get('responses', []))
            aggregate['errors'].extend(record.get('errors', []))
    return output


def publish(target, input_dir, *, page_dir=None, quotes_only=False):
    required = dates_for(target)
    data = apply_sector_pages.load_json(OUT / 'quotes.json')
    boards = boards_from(data)
    status = apply_sector_pages.load_json(OUT / 'status.json')
    if target < max(status.get('expected_trading_day') or '', data.get('expected_trade_date') or ''):
        print('本次目标早于已发布批次，保留更新的批次。', flush=True)
        return
    final_pages, prior_close, previous_day = {}, {}, None
    if page_dir is not None and any(page_dir.glob('*.json')):
        if not quotes_only:
            previous_day = _adjacent_trade_day(target, -1)
            final_pages = read_final_pages(page_dir)
            prior_close = {board['code']: prior_quote_close(board, previous_day)
                           for board in boards if board['code'] in final_pages}
        status = apply_sector_pages.run(page_dir, target=target)
        data = apply_sector_pages.load_json(OUT / 'quotes.json')
    boards = boards_from(data)
    records = read_artifacts(input_dir, target)
    history_fresh, gaps, quote_missing, error_summary = [], [], [], {}
    added_count = 0
    snapshot_count = 0
    new_quotes = False
    for board in boards:
        code = board['code']
        path = _history_file(board, OUT / 'history')
        local = read_history(path)
        valid = set(valid_history(local))
        record = records.get(code, {})
        errors = record.get('errors', []).copy()
        additions, sources = merge_board(board, local, valid, target, record.get('responses', []), errors)
        if target not in valid and target not in additions and code in final_pages:
            anchor = (additions.get(previous_day) or (local.get(previous_day) if previous_day in valid else None))
            snapshot = verified_snapshot(board, target, anchor[3] if anchor else prior_close.get(code),
                                         final_pages[code], errors)
            if snapshot:
                additions[target], sources[target] = snapshot
                snapshot_count += 1
        if additions:
            local.update(additions)
            valid.update(additions)
            write_history(path, local)
            sync_close_history(board, additions)
            added_count += len(additions)
        latest = max(valid, default='')
        missing = [day for day in required if day not in valid]
        latest_endpoint = sources.get(latest) or board.get('history_ohlcv_source_url')
        if not latest_endpoint and quote_day(board) == latest and board.get('quote_source_url') in ENDPOINTS:
            latest_endpoint = board['quote_source_url']
        if latest and latest_endpoint:
            board.update(history_ohlcv_source_url=latest_endpoint,
                         history_ohlcv_source=(SNAPSHOT_SOURCE if latest_endpoint in SNAPSHOT_ENDPOINTS else SOURCE))
        board.update(history_ohlcv_asof=latest or None, history_ohlcv_expected_asof=target,
                     history_ohlcv_missing_dates=missing, ohlcv_complete_to_target=not missing)
        if target in valid:
            history_fresh.append({'code': code, 'name': board['name']})
        if missing:
            gaps.append({'code': code, 'name': board['name'], 'latest': latest or None, 'dates': missing})
        reported = errors if missing else [message for message in errors if '保留原记录' in message]
        if reported:
            error_summary[code] = list(dict.fromkeys(reported))[:4]
        if not quotes_only and latest <= target:
            new_quotes |= refresh_quote(board, local, valid, latest, latest_endpoint, required)
        if quote_day(board) < target:
            quote_missing.append(board['name'])

    total, fresh = len(boards), len(boards) - len(quote_missing)
    run_at = datetime.now(CN).strftime('%Y-%m-%d %H:%M:%S')
    complete = not gaps
    quote_ok = not quote_missing
    reason = (f'{target} 报价 {fresh}/{total}，最新完整日K {len(history_fresh)}/{total}，'
              f'仍缺交易日的板块 {len(gaps)}，新增完整日K {added_count} 条')
    data.update(expected_trade_date=target, quote_fresh_count=fresh, quote_pending_count=len(quote_missing),
                ok=quote_ok, history_fresh_count=len(history_fresh), history_pending_count=total-len(history_fresh),
                history_gap_board_count=len(gaps), ohlcv_complete=complete)
    if new_quotes:
        data.update(fetched_at=run_at, source='东方财富官方BK历史日K与逐板块已核验收盘报价',
                    historical_recovery_only=False)
    status.update(run_at=run_at, github_run_id=os.environ.get('GITHUB_RUN_ID', ''),
                  expected_trading_day=target, mode='quotes' if quotes_only else 'full',
                  ok=quote_ok and (quotes_only or complete), reason=reason, board_count=total,
                  quote_fresh_count=fresh, quote_stale_count=len(quote_missing),
                  quote_missing_names=quote_missing, quotes_updated=new_quotes or status.get('quotes_updated', False),
                  ohlcv_complete=complete, history_fresh_count=len(history_fresh),
                  history_pending_count=total-len(history_fresh), history_gap_board_count=len(gaps),
                  history_required_from=required[0], history_fresh_boards=history_fresh, history_gaps=gaps,
                  history_added_rows=added_count, history_added_snapshot_rows=snapshot_count,
                  history_source_errors=error_summary)
    apply_sector_pages.atomic_json(OUT / 'quotes.json', data)
    apply_sector_pages.atomic_json(OUT / 'status.json', status)
    print(reason, flush=True)


def check(target, *, quotes_only=False):
    status = apply_sector_pages.load_json(OUT / 'status.json')
    if status.get('expected_trading_day', '') > target:
        print('已有更新的批次。')
        return 0
    print(status.get('reason') or '缺少行业同步状态')
    quote_ok = 100 <= status.get('board_count', 0) == status.get('quote_fresh_count')
    history_ok = status.get('ohlcv_complete') is True and status.get('board_count') == status.get('history_fresh_count')
    if status.get('expected_trading_day') == target and quote_ok and (quotes_only or history_ok):
        return 0
    for row in status.get('history_gaps', []) if not quotes_only else []:
        print(row['code'], row['name'], '缺', ', '.join(row['dates']))
    print('::error::行业行情仍有缺数，已发布通过核验的记录，详见 data/sectors/status.json。')
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('fetch', 'publish', 'check'))
    parser.add_argument('--target', required=True)
    parser.add_argument('--directory', type=Path, default=Path('sector_history'))
    parser.add_argument('--page-dir', type=Path)
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=1)
    parser.add_argument('--quotes-only', action='store_true')
    args = parser.parse_args()
    if args.command == 'fetch':
        if not 0 <= args.shard < args.shards:
            parser.error('分片编号超出范围')
        fetch_shard(args.target, args.shard, args.shards, args.directory / f'history-{args.shard}.json')
    elif args.command == 'publish':
        publish(args.target, args.directory, page_dir=args.page_dir, quotes_only=args.quotes_only)
    else:
        return check(args.target, quotes_only=args.quotes_only)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
