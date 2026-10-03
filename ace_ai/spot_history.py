"""Bounded, persistent spot history. Shares the existing DB; never resets it."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import json
import threading
import time
import pandas as pd
import local_market_cache as store

VERSION = 'spot-history-year-v4'
KEEP_DAYS = 365
MAX_STOCKS = min(200, max(1, int(os.getenv('DISCORD_AI_SPOT_HISTORY_MAX_STOCKS', '200'))))
MAX_BYTES = min(1_500_000_000, max(1_000_000, int(os.getenv('DISCORD_AI_SPOT_HISTORY_MAX_BYTES', '1500000000'))))
_ACTIVE = {}
_GUARD = threading.RLock()
_TABLES = ('spot_branch_daily', 'spot_branch_days', 'spot_history_stocks', 'spot_history_bars')
_SCHEMA_READY = set()

def _schema(conn):
    key=str(store.DB_PATH.resolve())
    if key in _SCHEMA_READY:return
    conn.execute('CREATE TABLE IF NOT EXISTS spot_history_stocks(stock_code TEXT PRIMARY KEY,last_query REAL NOT NULL,last_price_check REAL DEFAULT 0)')
    conn.execute('''CREATE TABLE IF NOT EXISTS spot_history_bars(stock_code TEXT NOT NULL,date TEXT NOT NULL,
        open REAL,high REAL,low REAL,close REAL,volume REAL,market TEXT DEFAULT '',source TEXT DEFAULT '',
        confirmed INTEGER DEFAULT 1,updated_at TEXT NOT NULL,PRIMARY KEY(stock_code,date))''')
    # Lazy, lossless import: old cache dates are not downloaded again after upgrading.
    conn.execute('''INSERT OR IGNORE INTO spot_history_stocks(stock_code,last_query)
        SELECT stock_code,COALESCE(MAX(CAST(strftime('%s',checked_at) AS REAL)),0) FROM spot_branch_days GROUP BY stock_code''')
    conn.commit()
    _SCHEMA_READY.add(key)

def tracked(code):
    with store._LOCK, store._db() as conn:
        _schema(conn)
        found = conn.execute('SELECT 1 FROM spot_history_stocks WHERE stock_code=?',(str(code),)).fetchone()
        conn.commit()
        return bool(found)

def codes():
    with store._LOCK, store._db() as conn:
        _schema(conn)
        values = [r[0] for r in conn.execute('SELECT stock_code FROM spot_history_stocks ORDER BY last_query DESC,stock_code')]
        conn.commit()
        return values

def touch(code, stamp=None):
    code = str(code)
    with _GUARD, store._LOCK, store._db() as conn:
        _schema(conn)
        conn.execute('INSERT INTO spot_history_stocks(stock_code,last_query) VALUES(?,?) ON CONFLICT(stock_code) DO UPDATE SET last_query=excluded.last_query',
                     (code,time.time() if stamp is None else float(stamp)))
        conn.execute('''INSERT OR IGNORE INTO spot_history_bars
            SELECT * FROM daily_bars WHERE stock_code=? AND confirmed=1''',(code,))
        conn.commit()
    maintain(protect={code})

@contextmanager
def active(code, query=False):
    code=str(code)
    with _GUARD:
        _ACTIVE[code]=_ACTIVE.get(code,0)+1
    try:
        if query: touch(code)
        yield
    finally:
        with _GUARD:
            _ACTIVE[code]-=1
            if not _ACTIVE[code]:_ACTIVE.pop(code)
        maintain()

def usage():
    """Measure allocated pages including indexes; freed pages are reusable by SQLite."""
    with store._LOCK, store._db() as conn:
        _schema(conn)
        try:
            marks=','.join('?' for _ in _TABLES)
            size=conn.execute(f'''SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN
                (SELECT name FROM sqlite_master WHERE tbl_name IN ({marks}))''',_TABLES).fetchone()[0]
            exact=True
        except Exception:
            # Conservative fallback; never silently estimate zero and keep downloading.
            size=conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]
            exact=False
        stocks=conn.execute('SELECT COUNT(*) FROM spot_history_stocks').fetchone()[0]
        conn.commit()
    try:
        import shutil
        free=shutil.disk_usage(store.DB_PATH.parent).free
    except OSError:free=None
    return {'bytes':int(size),'stocks':stocks,'exact':exact,'limit_bytes':MAX_BYTES,
            'warning':size>=MAX_BYTES*.8,'pause':size>=MAX_BYTES*.95 or (free is not None and free<300_000_000),
            'free_bytes':free}

def maintain(today='',protect=()):
    today=today or (datetime.now(timezone.utc)+timedelta(hours=8)).strftime('%Y-%m-%d')
    cutoff=(datetime.strptime(today,'%Y-%m-%d')-timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    removed=[]
    with _GUARD, store._LOCK, store._db() as conn:
        _schema(conn)
        row=conn.execute("SELECT value FROM kv WHERE key='spot_history_pruned_day'").fetchone()
        if not row or row[0] not in (today,json.dumps(today)):
            for table in ('spot_branch_daily','spot_branch_days','spot_history_bars'):
                conn.execute(f'DELETE FROM {table} WHERE date<?',(cutoff,))
            conn.execute("INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES('spot_history_pruned_day',?,?)",(json.dumps(today),today))
        conn.commit()
        protected=set(_ACTIVE)|set(protect)
        state=usage()
        while state['stocks']>MAX_STOCKS or state['bytes']>=MAX_BYTES:
            candidates=[r[0] for r in conn.execute('SELECT stock_code FROM spot_history_stocks ORDER BY last_query,stock_code') if r[0] not in protected]
            if not candidates:break
            code=candidates[0]
            for table in _TABLES:
                conn.execute(f'DELETE FROM {table} WHERE stock_code=?',(code,))
            # Cancel persistent queued work, not unrelated kv/user statistics.
            for key in ('spot_history_queue','spot_today_queue'):
                row=conn.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
                if row:
                    value=json.loads(row[0])
                    if isinstance(value,list):value=[c for c in value if c!=code]
                    elif isinstance(value,dict):value['codes']=[c for c in value.get('codes',[]) if c!=code]
                    conn.execute('UPDATE kv SET value=? WHERE key=?',(json.dumps(value),key))
            conn.commit();removed.append(code);state=usage()
    if removed:print('🗑️ 現股歷史LRU淘汰｜'+','.join(removed)+'｜未變更全市場日K或會員統計',flush=True)
    return dict(state,evicted=removed)

def save_prices(code,frame,market='',source=''):
    with active(code):
        return _save_prices_inner(code,frame,market,source)

def _save_prices_inner(code,frame,market='',source=''):
    if frame is None or frame.empty or not tracked(code):return
    # Store RAW confirmed prices only. The existing corporate-action loader adjusts on read.
    cutoff=(datetime.now(timezone.utc)+timedelta(hours=8)-timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    now=datetime.now(timezone.utc).isoformat()
    rows=[]
    for index,row in frame.iterrows():
        day=pd.Timestamp(index).strftime('%Y-%m-%d')
        try:values=[float(row.get(c)) for c in ('Open','High','Low','Close','Volume')]
        except (TypeError,ValueError):continue
        if day>=cutoff and store.valid_bar(*values):rows.append((str(code),day,*values,market,source,1,now))
    with store._LOCK, store._db() as conn:
        _schema(conn)
        conn.executemany('INSERT OR REPLACE INTO spot_history_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)',rows)
        conn.commit()

def load_prices(code):
    with store._LOCK, store._db() as conn:
        _schema(conn)
        rows=conn.execute('SELECT date,open,high,low,close,volume FROM spot_history_bars WHERE stock_code=? AND confirmed=1 ORDER BY date',(str(code),)).fetchall()
        conn.commit()
    if not rows:return None
    frame=pd.DataFrame(rows,columns=['Date','Open','High','Low','Close','Volume']).set_index('Date')
    frame.index=pd.to_datetime(frame.index)
    return frame

def price_check_due(code):
    with store._LOCK, store._db() as conn:
        _schema(conn)
        row=conn.execute('SELECT last_price_check FROM spot_history_stocks WHERE stock_code=?',(str(code),)).fetchone()
        conn.commit()
    return bool(row and time.time()-float(row[0] or 0)>86400)

def mark_price_check(code):
    with store._LOCK, store._db() as conn:
        _schema(conn)
        conn.execute('UPDATE spot_history_stocks SET last_price_check=? WHERE stock_code=?',(time.time(),str(code)))
        conn.commit()
