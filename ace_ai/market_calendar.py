"""Official session snapshots; missing calendar coverage remains explicit."""
import pandas as pd
import local_market_cache as db

KEY = 'official_market_sessions_v1'

def save_sessions(first, last, sessions, source):
    return db.save_market_sessions(first, last, sessions, source)

def sessions_between(first, last):
    return db.market_sessions_between(first, last)

def close_status(now, ready_minute):
    return db.market_close_status(now, ready_minute)
