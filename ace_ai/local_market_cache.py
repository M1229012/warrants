"""Persistent lightweight market cache for Ace AI.

Stores confirmed daily OHLCV bars and pattern-score snapshots in SQLite so restarts do not
force every sector query to refetch the same 69/70 days from FinMind. The DB is intentionally
small: only the latest N trading days per stock are kept.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

KEEP_DAYS = max(70, int(os.getenv("DISCORD_AI_PATTERN_HISTORY_DAYS", "70") or 70))
DEFAULT_PATH = "/data/ace_ai_market_cache.sqlite3" if Path("/data").exists() else str(Path(__file__).parent / ".cache" / "ace_ai_market_cache.sqlite3")
DB_PATH = Path(os.getenv("DISCORD_AI_MARKET_CACHE_DB", DEFAULT_PATH))
_LOCK = threading.RLock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    global _INITIALIZED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not _INITIALIZED:
        with _LOCK:
            if not _INITIALIZED:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_bars (
                        stock_code TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL, volume REAL,
                        market TEXT DEFAULT '', source TEXT DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (stock_code, date)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pattern_scores (
                        stock_code TEXT NOT NULL,
                        date TEXT NOT NULL,
                        score REAL NOT NULL,
                        grade TEXT DEFAULT '', basis TEXT DEFAULT '', components_json TEXT DEFAULT '[]',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (stock_code, date)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_code_date ON daily_bars(stock_code, date DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pattern_scores_code_date ON pattern_scores(stock_code, date DESC)")
                conn.commit()
                _INITIALIZED = True
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def save_bars(stock_code: str, df: pd.DataFrame, market: str = "", source: str = "", confirmed: bool = True) -> None:
    if df is None or df.empty:
        return
    code = str(stock_code).strip()
    frame = df.tail(max(KEEP_DAYS + 10, 90)).copy()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for idx, row in frame.iterrows():
        try:
            date = pd.Timestamp(idx).strftime("%Y-%m-%d")
            values = [float(row.get(c)) for c in ("Open", "High", "Low", "Close")]
            volume = float(row.get("Volume") or 0)
        except Exception:
            continue
        rows.append((code, date, *values, volume, str(market or ""), str(source or ""), int(bool(confirmed)), now))
    if not rows:
        return
    with _LOCK:
        with _db() as conn:
            conn.executemany("""
                INSERT INTO daily_bars(stock_code,date,open,high,low,close,volume,market,source,confirmed,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_code,date) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                  volume=excluded.volume, market=excluded.market, source=excluded.source,
                  confirmed=excluded.confirmed, updated_at=excluded.updated_at
            """, rows)
            # Keep a little calendar headroom; exact trading-day cleanup is handled by row count.
            old = conn.execute("SELECT date FROM daily_bars WHERE stock_code=? ORDER BY date DESC LIMIT 1 OFFSET ?", (code, KEEP_DAYS + 9)).fetchone()
            if old:
                conn.execute("DELETE FROM daily_bars WHERE stock_code=? AND date<=?", (code, old[0]))
            conn.commit()


def save_confirmed_bar(stock_code: str, date: Any, open_: float, high: float, low: float, close: float, volume: float,
                       market: str = "", source: str = "Fugle-close") -> None:
    idx = pd.DatetimeIndex([pd.Timestamp(date)])
    df = pd.DataFrame([[open_, high, low, close, volume]], index=idx, columns=["Open", "High", "Low", "Close", "Volume"])
    save_bars(stock_code, df, market=market, source=source, confirmed=True)


def load_bars(stock_code: str, limit: int = KEEP_DAYS, confirmed_only: bool = True) -> Optional[Dict[str, Any]]:
    code = str(stock_code).strip()
    try:
        with _LOCK:
            with _db() as conn:
                sql = "SELECT date,open,high,low,close,volume,market,source,confirmed FROM daily_bars WHERE stock_code=?"
                if confirmed_only:
                    sql += " AND confirmed=1"
                sql += " ORDER BY date DESC LIMIT ?"
                rows = conn.execute(sql, (code, int(limit))).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    rows.reverse()
    df = pd.DataFrame(rows, columns=["Date","Open","High","Low","Close","Volume","market","source","confirmed"])
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for col in ("Open","High","Low","Close","Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Date","Open","High","Low","Close"]).set_index("Date")
    if df.empty:
        return None
    market = next((str(v) for v in reversed(df["market"].tolist()) if v), "")
    source = next((str(v) for v in reversed(df["source"].tolist()) if v), "")
    return {"df": df[["Open","High","Low","Close","Volume"]], "market": market, "source": source,
            "last_date": pd.Timestamp(df.index.max()).normalize(), "count": len(df)}


def has_recent_history(stock_code: str, min_rows: int = 69, max_calendar_gap_days: int = 5, now: Optional[pd.Timestamp] = None) -> bool:
    data = load_bars(stock_code, limit=max(min_rows, KEEP_DAYS))
    if not data or data["count"] < min_rows:
        return False
    now = pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None) if now is None else pd.Timestamp(now).tz_localize(None) if pd.Timestamp(now).tzinfo else pd.Timestamp(now)
    return (now.normalize() - data["last_date"]).days <= max_calendar_gap_days


def save_pattern_score(stock_code: str, date: str, score: float, grade: str, components: Any, basis: str) -> None:
    if not stock_code or not date:
        return
    try:
        value = float(score)
    except Exception:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(components or [], ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        try:
            with _db() as conn:
                conn.execute("""
                    INSERT INTO pattern_scores(stock_code,date,score,grade,basis,components_json,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(stock_code,date) DO UPDATE SET score=excluded.score,grade=excluded.grade,
                      basis=excluded.basis,components_json=excluded.components_json,updated_at=excluded.updated_at
                """, (str(stock_code), str(date).replace("/", "-"), value, str(grade or ""), str(basis or ""), payload, now))
                old = conn.execute("SELECT date FROM pattern_scores WHERE stock_code=? ORDER BY date DESC LIMIT 1 OFFSET ?",
                                   (str(stock_code), KEEP_DAYS - 1)).fetchone()
                if old:
                    conn.execute("DELETE FROM pattern_scores WHERE stock_code=? AND date<?", (str(stock_code), old[0]))
                conn.commit()
        except Exception:
            return


def latest_pattern_score(stock_code: str) -> Optional[Dict[str, Any]]:
    try:
        with _LOCK:
            with _db() as conn:
                row = conn.execute("SELECT date,score,grade,basis,components_json FROM pattern_scores WHERE stock_code=? ORDER BY date DESC LIMIT 1",
                                   (str(stock_code),)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        components = json.loads(row[4] or "[]")
    except Exception:
        components = []
    return {"date": row[0], "score": row[1], "grade": row[2], "basis": row[3], "components": components}


def stats() -> Dict[str, Any]:
    try:
        with _LOCK:
            with _db() as conn:
                bars = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
                stocks = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM daily_bars").fetchone()[0]
                scores = conn.execute("SELECT COUNT(*) FROM pattern_scores").fetchone()[0]
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {"bars": bars, "stocks": stocks, "scores": scores, "bytes": size, "path": str(DB_PATH)}
    except Exception:
        return {"bars": 0, "stocks": 0, "scores": 0, "bytes": 0, "path": str(DB_PATH)}
