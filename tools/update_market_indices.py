"""A股上证指数、深证成指的真实日线行情定时采集器。GitHub Actions运行，不在PythonAnywhere运行。
优先 AKShare，备用 Yahoo Finance；没有合法真实行情时绝不构造或覆盖旧CSV。
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import time
import os
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'indexes'
INDEX = {
    'sh000001': ('上证指数', '000001', 'sh000001', '000001.SS'),
    'sz399001': ('深证成指', '399001', 'sz399001', '399001.SZ'),
}
MAPPING = {
    '日期':'date','Date':'date','date':'date','日期时间':'date',
    '开盘':'open','Open':'open','open':'open',
    '最高':'high','High':'high','high':'high',
    '最低':'low','Low':'low','low':'low',
    '收盘':'close','Close':'close','close':'close',
    '成交量':'volume','Volume':'volume','volume':'volume',
}


def normalize(frame):
    if frame is None or len(frame)==0:raise ValueError('没有返回行情')
    df=frame.copy()
    if 'date' not in df.columns and '日期' not in df.columns and 'Date' not in df.columns:
        df=df.reset_index()
    df.columns=[str(x[0] if isinstance(x,tuple) else x).strip() for x in df.columns]
    df=df.rename(columns=MAPPING)
    need=['date','open','high','low','close','volume']
    if not set(need).issubset(df.columns):raise ValueError('源接口字段缺少OHLCV: '+str(list(df.columns)[:20]))
    df=df[need].copy()
    df['date']=pd.to_datetime(df['date'],errors='coerce',utc=True).dt.tz_convert(None)
    for col in need[1:]:df[col]=pd.to_numeric(df[col],errors='coerce')
    df=df.dropna().drop_duplicates(subset=['date'],keep='last').sort_values('date')
    df=df[(df[['open','high','low','close']]>0).all(axis=1) & (df['volume']>=0)]
    df=df[(df['high']>=df[['open','high','low','close']].max(axis=1)) & (df['low']<=df[['open','high','low','close']].min(axis=1))]
    today=pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None)
    df=df[df['date']<=today+pd.Timedelta(days=1)]
    if len(df)<100:raise ValueError('有效交易日不足100个：'+str(len(df)))
    if (today-df.iloc[-1]['date']).days>12:raise ValueError('数据距离现在超过12天，不更新陈旧日线')
    df=df.tail(800).reset_index(drop=True)
    df['date']=df['date'].dt.strftime('%Y-%m-%d')
    return df


def fetch_one(info):
    name,east,sina,yahoo=info
    errors=[]
    try:
        import akshare as ak
        start=(datetime.now()-timedelta(days=365*4)).strftime('%Y%m%d')
        end=(datetime.now()+timedelta(days=1)).strftime('%Y%m%d')
        try:
            df=ak.index_zh_a_hist(symbol=east,period='daily',start_date=start,end_date=end)
            return normalize(df),'AKShare / 东方财富指数'
        except Exception as e: errors.append('AKShare东方财富: '+str(e)[:100])
        try:
            df=ak.stock_zh_index_daily(symbol=sina)
            return normalize(df),'AKShare / 新浪指数'
        except Exception as e: errors.append('AKShare新浪: '+str(e)[:100])
    except Exception as e:errors.append('AKShare模块: '+str(e)[:100])
    try:
        import yfinance as yf
        df=yf.download(yahoo,period='4y',interval='1d',auto_adjust=True,progress=False,threads=False,timeout=15)
        return normalize(df),'Yahoo Finance / '+yahoo
    except Exception as e:errors.append('Yahoo Finance: '+str(e)[:100])
    raise ValueError(' | '.join(errors))


def main():
    OUT.mkdir(exist_ok=True)
    summary=[];ok=0
    for code,info in INDEX.items():
        name=info[0]
        try:
            frame,source=fetch_one(info)
            target=OUT/(code+'.csv')
            tmp=OUT/(code+'.tmp')
            frame.to_csv(tmp,index=False,encoding='utf-8')
            tmp.replace(target)
            newest=frame.iloc[-1]['date']
            item={'code':code,'name':name,'latest_trading_day':newest,'rows':len(frame),'source':source,'status':'ok'}
            summary.append(item)
            (OUT/(code+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
            ok+=1
            print(f'{name} {code}: {len(frame)} rows; latest={newest}; source={source}',flush=True)
        except Exception as exc:
            print(f'{name} {code} 更新失败：{str(exc)[:260]}（旧数据保留）',flush=True)
        time.sleep(.5)
    if ok:
        (OUT/'index.json').write_text(json.dumps({'updated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),'indices':summary},ensure_ascii=False,indent=2),encoding='utf-8')
    if not ok:raise SystemExit('两个指数本次都未抓取成功；不提交假行情。检查接口与网络限制。')

if __name__=='__main__':main()

