"""證交所核實除權參考價備援。成功資料保存在各 Bot 自己的 SQLite。"""
import json
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import local_market_cache
import price_adjustment

URL = 'https://www.twse.com.tw/rwd/zh/exRight/TWT49U'
_LOCK = threading.RLock()
_CACHE = {}


def parse_twse(payload):
    fields = payload.get('fields') or []
    required = ['資料日期', '股票代號', '除權息前收盤價', '除權息參考價', '權/息']
    if payload.get('stat') != 'OK' or any(k not in fields for k in required):
        raise ValueError('證交所除權資料回應未通過欄位核對')
    events = []
    for values in payload.get('data') or []:
        row = dict(zip(fields, values))
        kind = str(row.get('權/息') or '').strip()
        if kind not in ('權', '權息'):
            continue  # 本入口處理股數變動；純除息不換算成交股數。
        parts = re.fullmatch(r'(\d+)年(\d+)月(\d+)日', str(row.get('資料日期', '')).strip())
        if not parts:
            raise ValueError('證交所公司行動日期格式不符')
        year, month, day = map(int, parts.groups())
        if year < 1911:
            year += 1911
        date = f'{year:04d}-{month:02d}-{day:02d}'
        def number(key):
            return float(str(row[key]).replace(',', '').strip())
        event = price_adjustment.event_from_row({
            'date': date, 'before_price': number('除權息前收盤價'),
            'reference_price': number('除權息參考價')}, kind)
        if not event['factor']:
            raise ValueError('證交所公司行動缺少有效參考價')
        event.update(stock_code=str(row['股票代號']).strip(), source='TWSE TWT49U')
        events.append(event)
    return events


def _fetch(start, today):
    query = urlencode({'startDate': start.replace('-', ''),
                       'endDate': today.replace('-', ''), 'response': 'json'})
    request = Request(URL + '?' + query, headers={'User-Agent': 'AceResearch/1.0'})
    with urlopen(request, timeout=12) as response:
        return parse_twse(json.loads(response.read().decode('utf-8')))


def twse_events(code, start, today):
    # 一個日期範圍抓全市場一次；併發查詢共用，不讓每個 Tool 各打一遍。
    key = (start, today)
    with _LOCK:
        cached = _CACHE.get(key)
        if not cached or time.monotonic() >= cached['until']:
            saved = local_market_cache.get_state('corporate_twse_v1', {}) or {}
            items = saved.get('items') or []
            exact = saved.get('start') == start and saved.get('end') == today
            if exact:
                cached = {'items': items, 'until': time.monotonic()+3600}
            else:
                try:
                    items = _fetch(start, today)
                    local_market_cache.set_state('corporate_twse_v1',
                                                 {'start': start, 'end': today, 'items': items})
                    cached = {'items': items, 'until': time.monotonic()+3600}
                except Exception as exc:
                    # 保存的「已核實事件」仍可使用，但不能宣稱涵蓋今天的新事件。
                    print(f'⚠️ 公司行動證交所備援失敗｜{type(exc).__name__}｜已保存核實事件 {len(items)} 筆', flush=True)
                    cached = {'items': items, 'until': time.monotonic()+60}
            _CACHE[key] = cached
        return [dict(e) for e in cached['items']
                if e.get('stock_code') == str(code) and start <= e['date'] <= today]
