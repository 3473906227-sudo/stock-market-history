#!/usr/bin/env python3
"""只做网络探测；不更改行情文件。用于验证2026-11-20第三方原始报价快照。"""
from urllib.request import Request, urlopen
from html.parser import HTMLParser
import re

class Reader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bits=[]
        self.block=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.block+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.block=max(0,self.block-1)
    def handle_data(self,data):
        if not self.block and data.strip():self.bits.append(data.strip())

for code in ('BK0438','BK1553','BK0440','BK1042','BK1289'):
    url='https://www.verityrank.com.cn/markets/sectors/'+code.lower()
    request=Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html','Accept-Language':'zh-CN,zh;q=0.9'})
    try:
        with urlopen(request,timeout=8) as resp:
            raw=resp.read(1500000).decode('utf-8','replace')
            print('PROBE',code,'status',resp.status,'bytes',len(raw),flush=True)
        h=Reader();h.feed(raw)
        text=' | '.join(h.bits)
        print('VISIBLE',code,text[:620],flush=True)
        print('HAS20261120', '2026-11-20' in text,'NUMBERS',re.findall(r'[-+]?\\d[\\d,]*\\.\\d{1,3}%',text[:400])[:10],flush=True)
        print('RAW_SCRIPT_URLS',re.findall(r'https?[^"\\s]+',raw)[:6],flush=True)
        for needle in ('2026-11-18','2026-11-19','2026-11-20','chartData','ohlcv','__NEXT_DATA__','\\"series\\"'):
            print('FOUND',code,needle,'count',raw.count(needle),'sample',raw[max(0,raw.find(needle)-120):raw.find(needle)+250] if needle in raw else 'NONE',flush=True)
    except Exception as exc:
        print('PROBE_ERROR',code,type(exc).__name__,str(exc)[:240],flush=True)
