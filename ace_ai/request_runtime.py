"""Request-scoped deadline and provider budget; safe to share across worker threads."""
from contextvars import ContextVar
from contextlib import contextmanager
import threading
import time
import pandas as pd

STATE = ContextVar('ace_request_state', default=None)
CLOCK = threading.local()
_LOCK = threading.RLock()

def state():
    return getattr(CLOCK, 'state', None) or STATE.get()

def new_state(seconds):
    return {'deadline': time.monotonic() + seconds, 'calls': 0, 'cancelled': False}

def check_budget(reserve=0):
    s = state()
    if s and (s.get('cancelled') or time.monotonic() + reserve >= s['deadline']):
        raise TimeoutError('整題處理逾時；未完成的分析不能當作無型態')

def reserve_call(max_calls):
    with _LOCK:
        check_budget()
        s = state()
        if s:
            if s['calls'] >= max_calls:
                raise TimeoutError('整題 Gemini 呼叫次數已達上限')
            s['calls'] += 1

@contextmanager
def scope(s):
    token = STATE.set(s)
    try:
        yield
    finally:
        STATE.reset(token)

def consecutive_sessions(a, b, sessions=None):
    a, b = pd.Timestamp(a).normalize(), pd.Timestamp(b).normalize()
    if b <= a:
        return False
    if sessions is not None:
        ds = sorted(set(pd.Timestamp(d).normalize() for d in sessions))
        return a in ds and b in ds and ds.index(b) == ds.index(a) + 1
    # Without a verified market calendar, never bridge a missing weekday.
    return b == a + pd.offsets.BDay(1)
