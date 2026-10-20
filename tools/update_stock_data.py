"""GitHub Actions 公共行情同步器：A股示例；不会生成模拟或缺失数据。"""
from __future__ import annotations
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
ALIASES = {
    '日期':'date','date':'date','Date':'date',
    '开盘':'open','Open':'open','open':'open',
    '最高':'high','High':'high','high':'high',
    '最低':'low','Low':'low','low':'low',
    '收盘':'close','Close':'close','close':'close',
    '成交量':'volume','Volume':'volume','volume':'volume'
}

def valid_frame(df):
    if df is None or df.empty:
        raise ValueError('上游返回空数据')
    result=df.reset_index() if 'date' not in [str(c).lower() for c in df.columns] and '日期' not in df.columns else df.copy()
    result.columns=[str(c[0] if isinstance(c,tuple) else c).strip() for c in result.columns]
    result=result.rename(columns=ALIASES)
    wanted=['date','open','high','low','close','volume']
    if not all(c in result.columns for c in wanted):
        raise ValueError('上游字段不完整：'+str(result.columns.tolist()[:15]))
    result=result[wanted].copy()
    result['date']=pd.to_datetime(result['date'],errors='coerce')
    for c in wanted[1:]:
        result[c]=pd.to_numeric(result[c],errors='coerce')
    result=result.dropna().sort_values('date').drop_duplicates('date',keep='last')
    result=result[(result[['open','high','low','close']]>0).all(axis=1)&(result['volume']>=0)]
    result=result[(result['high']>=result[['open','high','low','close']].max(axis=1)) &
                  (result['low']<=result[['open','high','low','close']].min(axis=1))]
    if len(result)<320:
        raise ValueError(f'有效历史行情只有 {len(result)} 行（要求>=320）')
    if result['date'].iloc[-1]>pd.Timestamp.now()+pd.Timedelta(days=1):
        raise ValueError('历史行情包含未来日期')
    result['date']=result['date'].dt.strftime('%Y-%m-%d')
    return result.tail(4000).reset_index(drop=True)


def yahoo_code(code):
    c=code.upper()
    if re.fullmatch(r'\d{6}',c):
        if c[0] in '69':return c+'.SS'
        if c[0] in '023':return c+'.SZ'
        if c[0] in '48':return c+'.BJ'
    return c


def fetch_symbol(symbol):
    err=[]
    if re.fullmatch(r'\d{6}',symbol):
        try:
            import akshare as ak
            a=ak.stock_zh_a_hist(symbol=symbol,period='daily',
               start_date=(datetime.now()-timedelta(days=365*9)).strftime('%Y%m%d'),
               end_date=(datetime.now()+timedelta(days=1)).strftime('%Y%m%d'),
               adjust='qfq',timeout=12)
            return valid_frame(a),'AKShare/Eastmoney 前复权'
        except Exception as e:
            err.append('AKShare: '+str(e)[:150])
        # 新浪 A股接口仅作备用，复权/量单位可能与东方财富不一致，标签如实标注
        try:
            import akshare as ak
            market=('sh' if symbol[0] in '69' else 'bj' if symbol[0] in '48' else 'sz')
            a=ak.stock_zh_a_daily(symbol=market+symbol,adjust='qfq')
            return valid_frame(a),'AKShare/Sina 前复权'
        except Exception as e:
            err.append('新浪: '+str(e)[:150])
    try:
        import yfinance as yf
        code=yahoo_code(symbol)
        frame=yf.download(code,period='8y',auto_adjust=True,progress=False,threads=False,timeout=18)
        return valid_frame(frame),'Yahoo/'+code+'/调整价格'
    except Exception as e:
        err.append('Yahoo: '+str(e)[:150])
    raise RuntimeError('; '.join(err))


def main():
    items=[]
    for line in (ROOT/'stock_symbols.txt').read_text(encoding='utf-8-sig').splitlines():
        symbol=line.split('#')[0].strip().upper()
        if not symbol:continue
        if not re.fullmatch(r'[A-Z0-9.\-^]{1,24}',symbol):raise ValueError('非法证券代码 '+symbol)
        if symbol not in items:items.append(symbol)
    if not items:raise RuntimeError('stock_symbols.txt 没有股票')
    saved=0
    for idx,symbol in enumerate(items,1):
        print(f'[{idx}/{len(items)}] 获取 {symbol}',flush=True)
        try:
            frame,source=fetch_symbol(symbol)
            target=DATA/(symbol+'.csv')
            temp=DATA/(symbol+'.new')
            frame.to_csv(temp,index=False,encoding='utf-8-sig')
            os.replace(temp,target)
            (DATA/(symbol+'.json')).write_text(json.dumps({
                'symbol':symbol,'source':source,'rows':len(frame),
                'latest_trading_day':frame.iloc[-1]['date'],
                'synced_utc':datetime.now(timezone.utc).isoformat(timespec='seconds')
            },ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'  成功：{len(frame)}行，最后交易日 {frame.iloc[-1]["date"]}，来源 {source}',flush=True)
            saved+=1
        except Exception as exc:
            print(f'  失败：{exc}；原CSV如存在则保留，绝不以模拟数据覆盖。',flush=True)
        time.sleep(1.2)
    print(f'成功 {saved}/{len(items)}',flush=True)
    if saved==0:raise SystemExit('全部行情同步失败，检查 GitHub Actions 日志及上游网络访问。')

if __name__=='__main__':main()
