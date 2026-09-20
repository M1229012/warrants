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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

KEEP_DAYS = max(70, int(os.getenv("DISCORD_AI_PATTERN_HISTORY_DAYS", "150") or 150))
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
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS kv (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS usage_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        day TEXT NOT NULL,
                        route TEXT DEFAULT '',
                        gemini_calls INTEGER DEFAULT 0,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        token_source TEXT DEFAULT '',
                        api_json TEXT DEFAULT '{}',
                        elapsed REAL DEFAULT 0,
                        cache_hit INTEGER DEFAULT 0
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_log(day)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(date)")
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


def save_market_day(rows: Iterable[Dict[str, Any]], date: str, source: str = "") -> int:
    """整個市場某一天的收盤一次寫入（證交所／櫃買每日行情）。回傳實際寫入筆數。"""
    day = str(date).strip()
    if not day:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for row in rows:
        try:
            code = str(row["stock_code"]).strip()
            values = [float(row[k]) for k in ("open", "high", "low", "close")]
        except (KeyError, TypeError, ValueError):
            continue
        if not code or any(v <= 0 for v in values):
            continue
        try:
            volume = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        payload.append((code, day, *values, volume, str(row.get("market") or ""), source, 1, now))
    if not payload:
        return 0
    with _LOCK:
        with _db() as conn:
            conn.executemany("""
                INSERT INTO daily_bars(stock_code,date,open,high,low,close,volume,market,source,confirmed,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_code,date) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                  volume=excluded.volume,
                  market=CASE WHEN excluded.market<>'' THEN excluded.market ELSE daily_bars.market END,
                  source=excluded.source, confirmed=1, updated_at=excluded.updated_at
            """, payload)
            conn.commit()
    return len(payload)


def trim_history(keep_days: int = KEEP_DAYS) -> int:
    """只留最近 keep_days 個交易日（全市場共用同一批日期），避免 SQLite 無限長大。"""
    with _LOCK:
        with _db() as conn:
            row = conn.execute("SELECT date FROM daily_bars GROUP BY date ORDER BY date DESC LIMIT 1 OFFSET ?",
                               (max(1, int(keep_days)) - 1,)).fetchone()
            if not row:
                return 0
            cursor = conn.execute("DELETE FROM daily_bars WHERE date<?", (row[0],))
            conn.commit()
            return int(cursor.rowcount or 0)


def known_dates(limit: int = 400) -> List[str]:
    """已存在的交易日（新到舊）。"""
    try:
        with _LOCK:
            with _db() as conn:
                rows = conn.execute("SELECT date FROM daily_bars GROUP BY date ORDER BY date DESC LIMIT ?",
                                    (int(limit),)).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def codes_with_history(min_rows: int = 69) -> List[str]:
    try:
        with _LOCK:
            with _db() as conn:
                rows = conn.execute("SELECT stock_code FROM daily_bars GROUP BY stock_code HAVING COUNT(*)>=? ORDER BY stock_code",
                                    (int(min_rows),)).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def latest_changes(codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """每檔最新兩根收盤 → 收盤價與漲跌幅（純本地，不打任何 API）。"""
    wanted = [str(c).strip() for c in codes if str(c).strip()]
    if not wanted:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _LOCK:
            with _db() as conn:
                for chunk_start in range(0, len(wanted), 400):
                    chunk = wanted[chunk_start:chunk_start + 400]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"""SELECT stock_code, date, close, volume FROM daily_bars
                            WHERE stock_code IN ({marks}) AND confirmed=1
                            ORDER BY stock_code, date DESC""", chunk).fetchall()
                    grouped: Dict[str, List[Any]] = {}
                    for code, date, close, volume in rows:
                        bucket = grouped.setdefault(str(code), [])
                        if len(bucket) < 2:
                            bucket.append((str(date), float(close), float(volume or 0)))
                    for code, bucket in grouped.items():
                        if len(bucket) < 2 or not bucket[1][1]:
                            continue
                        out[code] = {"date": bucket[0][0], "close": bucket[0][1],
                                     "change_pct": (bucket[0][1] / bucket[1][1] - 1) * 100,
                                     "volume": bucket[0][2]}
    except Exception:
        return out
    return out


def pattern_scores_for(codes: Iterable[str], max_age_days: int = 5) -> Dict[str, Dict[str, Any]]:
    """多檔最新型態分數（只讀本地）。"""
    wanted = [str(c).strip() for c in codes if str(c).strip()]
    if not wanted:
        return {}
    cutoff = (pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None).normalize()
              - pd.Timedelta(days=max(1, int(max_age_days)))).strftime("%Y-%m-%d")
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _LOCK:
            with _db() as conn:
                for chunk_start in range(0, len(wanted), 400):
                    chunk = wanted[chunk_start:chunk_start + 400]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"""SELECT stock_code, date, score, grade FROM pattern_scores
                            WHERE stock_code IN ({marks}) AND date>=? ORDER BY stock_code, date DESC""",
                        chunk + [cutoff]).fetchall()
                    for code, date, score, grade in rows:
                        out.setdefault(str(code), {"date": str(date), "score": float(score), "grade": str(grade or "")})
    except Exception:
        return out
    return out


def liquidity_map(days: int = 20) -> Dict[str, Dict[str, float]]:
    """近 N 個交易日的平均成交量（張）與平均成交金額（元）；一次 SQL 算完全市場。"""
    dates = known_dates(limit=max(1, int(days)))
    if not dates:
        return {}
    marks = ",".join("?" * len(dates))
    try:
        with _LOCK:
            with _db() as conn:
                rows = conn.execute(
                    f"""SELECT stock_code, AVG(volume)/1000.0, AVG(volume*close), COUNT(*)
                        FROM daily_bars WHERE date IN ({marks}) GROUP BY stock_code""", dates).fetchall()
    except Exception:
        return {}
    return {str(code): {"avg_lots": float(lots or 0), "avg_value": float(value or 0), "days": int(count or 0)}
            for code, lots, value, count in rows}


def get_state(key: str, default: Any = None) -> Any:
    try:
        with _LOCK:
            with _db() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (str(key),)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:
        return default


def set_state(key: str, value: Any) -> None:
    try:
        with _LOCK:
            with _db() as conn:
                conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                             (str(key), json.dumps(value, ensure_ascii=False, default=str),
                              datetime.now(timezone.utc).isoformat()))
                conn.commit()
    except Exception:
        return


def delete_state(key: str) -> None:
    try:
        with _LOCK:
            with _db() as conn:
                conn.execute("DELETE FROM kv WHERE key=?", (str(key),))
                conn.commit()
    except Exception:
        return


def stats() -> Dict[str, Any]:
    try:
        with _LOCK:
            with _db() as conn:
                bars = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
                stocks = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM daily_bars").fetchone()[0]
                scores = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM pattern_scores").fetchone()[0]
                days = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_bars").fetchone()[0]
                last_day = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {"bars": bars, "stocks": stocks, "scores": scores, "days": days, "last_day": last_day,
                "bytes": size, "path": str(DB_PATH), "persistent": str(DB_PATH).startswith("/data")}
    except Exception:
        return {"bars": 0, "stocks": 0, "scores": 0, "days": 0, "last_day": "", "bytes": 0,
                "path": str(DB_PATH), "persistent": str(DB_PATH).startswith("/data")}


# ============================================================
# 使用量記錄（Gemini 與各 API，供「/ace 用量」與負載評估）
# ============================================================

USAGE_KEEP_DAYS = 30


def log_usage(route: str, gemini_calls: int, input_tokens: int, output_tokens: int,
              token_source: str, api_usage: Any, elapsed: float, cache_hit: bool) -> None:
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    try:
        with _LOCK:
            with _db() as conn:
                conn.execute(
                    "INSERT INTO usage_log (ts,day,route,gemini_calls,input_tokens,output_tokens,"
                    "token_source,api_json,elapsed,cache_hit) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"), str(route or ""),
                     int(gemini_calls or 0), int(input_tokens or 0), int(output_tokens or 0),
                     str(token_source or ""), json.dumps(api_usage or {}, ensure_ascii=False),
                     float(elapsed or 0.0), 1 if cache_hit else 0))
                conn.execute("DELETE FROM usage_log WHERE day < ?",
                             ((now - timedelta(days=USAGE_KEEP_DAYS)).strftime("%Y-%m-%d"),))
                conn.commit()
    except Exception:
        pass


def usage_summary(day: str = "") -> Dict[str, Any]:
    """單日用量統計：題數、Gemini 次數與 token、各 API 次數、快取命中率、平均耗時。"""
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    target = day or now.strftime("%Y-%m-%d")
    try:
        with _LOCK:
            with _db() as conn:
                rows = conn.execute(
                    "SELECT gemini_calls,input_tokens,output_tokens,api_json,elapsed,cache_hit,route,ts "
                    "FROM usage_log WHERE day=?", (target,)).fetchall()
    except Exception:
        rows = []
    api_counts: Dict[str, int] = {}
    routes: Dict[str, int] = {}
    hours: Dict[str, int] = {}
    total_elapsed = 0.0
    slowest = 0.0
    for gemini, tin, tout, api_json, elapsed, cache_hit, route, ts in rows:
        try:
            for name, info in (json.loads(api_json or "{}") or {}).items():
                api_counts[name] = api_counts.get(name, 0) + int((info or {}).get("calls") or 0)
        except Exception:
            pass
        routes[str(route or "")] = routes.get(str(route or ""), 0) + 1
        hours[str(ts or "")[11:13]] = hours.get(str(ts or "")[11:13], 0) + 1
        total_elapsed += float(elapsed or 0.0)
        slowest = max(slowest, float(elapsed or 0.0))
    count = len(rows)
    return {
        "day": target, "questions": count,
        "gemini_calls": sum(int(r[0] or 0) for r in rows),
        "input_tokens": sum(int(r[1] or 0) for r in rows),
        "output_tokens": sum(int(r[2] or 0) for r in rows),
        "cache_hits": sum(1 for r in rows if int(r[5] or 0)),
        "cache_hit_rate": round(sum(1 for r in rows if int(r[5] or 0)) / count * 100, 1) if count else 0.0,
        "avg_elapsed": round(total_elapsed / count, 2) if count else 0.0,
        "slowest": round(slowest, 2),
        "api_counts": dict(sorted(api_counts.items(), key=lambda x: -x[1])),
        "routes": dict(sorted(routes.items(), key=lambda x: -x[1])),
        "busiest_hour": max(hours.items(), key=lambda x: x[1])[0] + ":00" if hours else "",
    }
