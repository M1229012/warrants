"""Official session snapshots; missing calendar coverage remains explicit."""
import pandas as pd
import local_market_cache as db

KEY = 'official_market_sessions_v1'

def save_sessions(first, last, sessions, source):
    first, last = pd.Timestamp(first).normalize(), pd.Timestamp(last).normalize()
    dates = sorted({pd.Timestamp(x).strftime('%Y-%m-%d') for x in sessions
                    if first <= pd.Timestamp(x).normalize() <= last})
    if not dates:
        raise ValueError('官方交易日資料空白，不記成完整日曆')
    old = db.get_state(KEY, {}) or {}
    if old and first <= pd.Timestamp(old['last']) and last >= pd.Timestamp(old['first']):
        # An authoritative correction replaces only its covered interval.
        dates = sorted(set(dates) | {x for x in old.get('sessions', [])
                       if pd.Timestamp(x) < first or pd.Timestamp(x) > last})
        first, last = min(first, pd.Timestamp(old['first'])), max(last, pd.Timestamp(old['last']))
    db.set_state(KEY, {'first': first.strftime('%Y-%m-%d'), 'last': last.strftime('%Y-%m-%d'),
                       'sessions': dates, 'source': source})

def sessions_between(first, last):
    saved = db.get_state(KEY, {}) or {}
    first, last = pd.Timestamp(first).normalize(), pd.Timestamp(last).normalize()
    if not saved or first < pd.Timestamp(saved['first']) or last > pd.Timestamp(saved['last']):
        return None
    keys = [d.strftime('%Y-%m-%d') for d in pd.date_range(first, last)]
    closed = set(db.market_closed_days(keys))
    return [d for d in saved['sessions'] if first <= pd.Timestamp(d) <= last and d not in closed]

def close_status(now, ready_minute):
    day = pd.Timestamp(now.date())
    if now.hour * 60 + now.minute < ready_minute:
        day -= pd.Timedelta(days=1)
    first = day - pd.Timedelta(days=25)
    sessions = sessions_between(first, day)
    if sessions is None:
        closed = set(db.market_closed_days([d.strftime('%Y-%m-%d') for d in pd.date_range(first, day)]))
        while day.weekday() >= 5 or day.strftime('%Y-%m-%d') in closed:
            day -= pd.Timedelta(days=1)
    elif sessions:
        day = pd.Timestamp(sessions[-1])
    else:
        raise ValueError('官方日曆範圍內沒有收盤交易日')
    status = db.market_status([day.strftime('%Y-%m-%d')])[day.strftime('%Y-%m-%d')]
    return {'expected_date': day, 'calendar_verified': sessions is not None,
            'markets_complete': all(status[m] == 'complete' for m in db.MARKETS), 'market_status': status}
