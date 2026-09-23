"""Persistent lightweight market cache for Ace AI.

Stores confirmed daily OHLCV bars and pattern-score snapshots in SQLite so restarts do not
force every sector query to refetch the same 69/70 days from FinMind. The DB is intentionally
small: only the latest N trading days per stock are kept.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

KEEP_DAYS = max(70, int(os.getenv("DISCORD_AI_PATTERN_HISTORY_DAYS", "150") or 150))
DEFAULT_PATH = "/data/ace_ai_market_cache.sqlite3" if Path("/data").exists() else str(Path(__file__).parent / ".cache" / "ace_ai_market_cache.sqlite3")
DB_PATH = Path(os.getenv("DISCORD_AI_MARKET_CACHE_DB", DEFAULT_PATH))
_LOCK = threading.RLock()
_INITIALIZED = False
# 單日最高／最低價比值上限（台股漲跌幅 10%，新上市前 5 日無漲跌幅；超過這個比值視為來源錯誤）
BAR_MAX_RANGE = max(1.2, float(os.getenv("DISCORD_AI_BAR_MAX_RANGE", "2.0") or 2.0))


class DBError(Exception):
    """SQLite 讀寫失敗（database locked、disk I/O、損毀）；和「查無資料」不同，呼叫端不可當成空結果。"""


class StateCorrupt(DBError):
    """kv 裡的 JSON 無法解析或型別不對；不可當成空清單覆蓋。"""


def _warn(action: str, exc: BaseException) -> None:
    print(f"⚠️ 本地資料庫{action}失敗｜{type(exc).__name__}: {exc}", flush=True)


def valid_bar(open_: Any, high: Any, low: Any, close: Any, volume: Any = 0.0) -> bool:
    """OHLCV 合理性：有限數字、價格 > 0、量 ≥ 0、High ≥ max(O,C,L)、Low ≤ min(O,C)、High／Low 不超過 BAR_MAX_RANGE。"""
    try:
        o, h, l, c, v = (float(x) for x in (open_, high, low, close, volume))
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in (o, h, l, c, v)):
        return False
    if min(o, h, l, c) <= 0 or v < 0:
        return False
    return h >= max(o, c, l) and l <= min(o, c) and h / l <= BAR_MAX_RANGE


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
                # 盤中高頻資料一律「一筆一列」往後加，不再每 5 分鐘整包重寫 JSON（降低底層區塊重寫量）
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS radar_snapshots (
                        day TEXT NOT NULL, time TEXT NOT NULL, data TEXT NOT NULL,
                        PRIMARY KEY (day, time)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS radar_turnover (
                        day TEXT NOT NULL, bucket TEXT NOT NULL, data TEXT NOT NULL,
                        PRIMARY KEY (day, bucket)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ivol_samples (
                        day TEXT NOT NULL, code TEXT NOT NULL, bucket INTEGER NOT NULL,
                        market TEXT DEFAULT 'twse', lots REAL, pred REAL,
                        PRIMARY KEY (day, code, bucket)
                    )
                """)
                # 現股券商分點：每交易日 × 每股票 × 每分點一列（net = buy - sell，正＝買超、負＝賣超），UPSERT 不刪舊資料。
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS spot_branch_daily (
                        stock_code TEXT NOT NULL, date TEXT NOT NULL, branch_name TEXT NOT NULL,
                        buy REAL NOT NULL DEFAULT 0, sell REAL NOT NULL DEFAULT 0, net REAL NOT NULL DEFAULT 0,
                        source TEXT DEFAULT '', updated_at TEXT NOT NULL,
                        PRIMARY KEY (stock_code, date, branch_name)
                    )
                """)
                # 每股票每交易日的抓取狀態：complete／pending_update／market_closed／stock_no_trade／source_error／retry
                # （查不到資料不能當成 0，也不能一律當停牌）。
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS spot_branch_days (
                        stock_code TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
                        checked_at TEXT NOT NULL, source TEXT DEFAULT '', rows INTEGER DEFAULT 0, detail TEXT DEFAULT '',
                        PRIMARY KEY (stock_code, date)
                    )
                """)
                # 全市場收盤完整性：上市（twse）與上櫃（tpex）分開記錄，一邊成功不代表另一邊完整。
                # status：complete／closed（交易所明確回覆當天無交易＝休市）／source_error／pending／unknown
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS market_days (
                        date TEXT NOT NULL, market TEXT NOT NULL, status TEXT NOT NULL,
                        rows INTEGER DEFAULT 0, source TEXT DEFAULT '', checked_at TEXT NOT NULL, detail TEXT DEFAULT '',
                        PRIMARY KEY (date, market)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_spot_branch_daily_branch ON spot_branch_daily(branch_name, date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_radar_turnover_bucket ON radar_turnover(bucket, day)")
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
    rows, rejected = [], 0
    for idx, row in frame.iterrows():
        try:
            date = pd.Timestamp(idx).strftime("%Y-%m-%d")
            values = [float(row.get(c)) for c in ("Open", "High", "Low", "Close")]
            raw_volume = row.get("Volume")
            volume = 0.0 if raw_volume is None else float(raw_volume)
        except Exception:
            rejected += 1
            continue
        if not valid_bar(*values, volume):
            rejected += 1   # NaN／Inf／負價格／OHLC 矛盾／區間不合理：不入庫
            continue
        rows.append((code, date, *values, volume, str(market or ""), str(source or ""), int(bool(confirmed)), now))
    if rejected:
        print(f"⚠️ {code} 日K 有 {rejected} 根數值不合理，未寫入本地底庫（來源 {source or '-'}）", flush=True)
    if not rows:
        return
    with _LOCK:
        with _db() as conn:
            # 暫定（confirmed=0）行情不可覆蓋已確認的正式收盤；正式收盤可以覆蓋暫定。
            conn.executemany("""
                INSERT INTO daily_bars(stock_code,date,open,high,low,close,volume,market,source,confirmed,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_code,date) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                  volume=excluded.volume,
                  market=CASE WHEN excluded.market<>'' THEN excluded.market ELSE daily_bars.market END,
                  source=excluded.source, confirmed=excluded.confirmed, updated_at=excluded.updated_at
                WHERE excluded.confirmed=1 OR daily_bars.confirmed=0
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
    except Exception as exc:
        _warn(f"讀取日K（{code}）", exc)
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
        try:
            volume = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        if not code or not valid_bar(*values, volume):
            continue
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


def last_bar_dates() -> Dict[str, str]:
    """每檔自己最後一根收盤的日期。停牌股不會跟全市場最新日一致，拿來判斷「算過了沒」才不會每輪重算。"""
    try:
        with _LOCK:
            with _db() as conn:
                rows = conn.execute("SELECT stock_code,MAX(date) FROM daily_bars WHERE confirmed=1 "
                                    "GROUP BY stock_code").fetchall()
        return {str(code): str(date) for code, date in rows if date}
    except Exception:
        return {}


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


def latest_pattern_score_dates(codes: Iterable[str]) -> Dict[str, str]:
    """指定股票各自最新的型態分數日期；純本地查詢，不限制日期範圍。"""
    wanted = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    out: Dict[str, str] = {}
    if not wanted:
        return out
    try:
        with _LOCK:
            with _db() as conn:
                for chunk_start in range(0, len(wanted), 400):
                    chunk = wanted[chunk_start:chunk_start + 400]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT stock_code, MAX(date) FROM pattern_scores "
                        f"WHERE stock_code IN ({marks}) GROUP BY stock_code", chunk).fetchall()
                    out.update({str(code): str(date) for code, date in rows if date})
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
    except Exception as exc:
        _warn(f"讀取狀態（{key}）", exc)
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
    except Exception as exc:
        _warn(f"寫入狀態（{key}）", exc)
        return


def append_state_list(key: str, record: Any, keep: int = 0) -> List[Any]:
    """在同一個 SQLite transaction 內 讀 → 附加 → 寫 → commit（BEGIN IMMEDIATE 先取得寫入鎖），
    兩個請求同時新增時不會後寫蓋掉前寫。原資料壞掉（JSON 錯誤或不是清單）時丟 StateCorrupt，不當成空清單覆蓋。"""
    with _LOCK:
        try:
            with _db() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute("SELECT value FROM kv WHERE key=?", (str(key),)).fetchone()
                    if row:
                        try:
                            items = json.loads(row[0])
                        except (TypeError, ValueError) as exc:
                            raise StateCorrupt(f"{key} 內容無法解析：{exc}") from exc
                        if not isinstance(items, list):
                            raise StateCorrupt(f"{key} 不是清單（{type(items).__name__}）")
                    else:
                        items = []
                    items.append(record)
                    if keep:
                        items = items[-int(keep):]
                    conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                                 (str(key), json.dumps(items, ensure_ascii=False, default=str),
                                  datetime.now(timezone.utc).isoformat()))
                    conn.execute("COMMIT")
                    return items
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except DBError:
            raise
        except sqlite3.Error as exc:
            raise DBError(f"寫入 {key} 失敗：{exc}") from exc


def delete_state(key: str) -> None:
    try:
        with _LOCK:
            with _db() as conn:
                conn.execute("DELETE FROM kv WHERE key=?", (str(key),))
                conn.commit()
    except Exception:
        return


# ============================================================
# 盤中高頻資料（append-only）：族群雷達快照、成交占比、盤中量能原始取樣
# ============================================================

def _write(sql: str, params: tuple) -> None:
    try:
        with _LOCK:
            with _db() as conn:
                conn.execute(sql, params)
                conn.commit()
    except Exception as exc:
        _warn("寫入", exc)
        return


def _read_strict(sql: str, params: tuple) -> List[tuple]:
    """讀取失敗丟 DBError（查無資料＝空清單；DB 壞掉／被鎖＝DBError，兩者不可混用）。"""
    try:
        with _LOCK:
            with _db() as conn:
                return conn.execute(sql, params).fetchall()
    except Exception as exc:
        _warn("讀取", exc)
        raise DBError(f"{type(exc).__name__}: {exc}") from exc


def _read(sql: str, params: tuple) -> List[tuple]:
    """非關鍵資料的讀取：失敗記 Log 後回空清單（盤中雷達、量能取樣這類可以晚點再試的資料）。"""
    try:
        return _read_strict(sql, params)
    except DBError:
        return []


SPOT_STATUSES = ("complete", "pending_update", "market_closed", "stock_no_trade", "source_error", "retry")
SPOT_KEEP_CALENDAR_DAYS = 160   # 約 110 個交易日，足夠 70 日統計＋延續性／事件回測


def save_spot_day(stock_code: str, date: str, rows: Iterable[Dict[str, Any]], status: str,
                  source: str = "", detail: str = "") -> int:
    """寫入單一股票單一交易日的現股分點與狀態（同一個 transaction）。
    - complete：先刪掉該股該日所有舊分點，再寫入這次完整 snapshot（來源更正後不會殘留舊分點）。
    - 其他狀態（pending_update／retry／source_error…）：不動分點資料；已經是 complete 的日子也不會被降級。"""
    if status not in SPOT_STATUSES:
        raise ValueError(f"unknown spot status: {status}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = []
    if status == "complete":
        for row in rows or []:
            name = str(row.get("branch_name") or "").strip()
            if not name:
                continue
            buy, sell = float(row.get("buy") or 0), float(row.get("sell") or 0)
            net = float(row["net"]) if row.get("net") is not None else buy - sell
            items.append((str(stock_code), str(date), name, buy, sell, net, source, now))
    with _LOCK:
        with _db() as conn, conn:
            if status == "complete":
                conn.execute("DELETE FROM spot_branch_daily WHERE stock_code=? AND date=?", (str(stock_code), str(date)))
                conn.executemany(
                    "INSERT INTO spot_branch_daily(stock_code,date,branch_name,buy,sell,net,source,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(stock_code,date,branch_name) DO UPDATE SET "
                    "buy=excluded.buy, sell=excluded.sell, net=excluded.net, source=excluded.source, updated_at=excluded.updated_at",
                    items)
            conn.execute(
                "INSERT INTO spot_branch_days(stock_code,date,status,checked_at,source,rows,detail) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(stock_code,date) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at, "
                "source=excluded.source, rows=excluded.rows, detail=excluded.detail "
                "WHERE excluded.status='complete' OR spot_branch_days.status<>'complete'",
                (str(stock_code), str(date), status, now, source, len(items), str(detail)[:300]))
    return len(items)


def spot_day_status(stock_code: str, dates: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """讀取失敗丟 DBError（不可當成「都還沒抓」而整批重抓富邦）。"""
    dates = [str(d) for d in dates]
    if not dates:
        return {}
    marks = ",".join("?" * len(dates))
    rows = _read_strict(f"SELECT date,status,checked_at,rows FROM spot_branch_days WHERE stock_code=? AND date IN ({marks})",
                        (str(stock_code), *dates))
    return {r[0]: {"status": r[1], "checked_at": r[2], "rows": int(r[3] or 0)} for r in rows}


def load_spot_rows(stock_code: str, dates: Iterable[str]) -> List[Dict[str, Any]]:
    dates = [str(d) for d in dates]
    if not dates:
        return []
    marks = ",".join("?" * len(dates))
    rows = _read_strict(f"SELECT date,branch_name,buy,sell,net FROM spot_branch_daily WHERE stock_code=? AND date IN ({marks})",
                        (str(stock_code), *dates))
    return [{"date": r[0], "branch_name": r[1], "buy": float(r[2]), "sell": float(r[3]), "net": float(r[4])} for r in rows]


def spot_branch_history(branch_name: str, since: str) -> List[Dict[str, Any]]:
    """某分點在本地已有的所有股票現股紀錄（只含已建置過的股票）。"""
    rows = _read("SELECT stock_code,date,buy,sell,net FROM spot_branch_daily WHERE branch_name=? AND date>=? ORDER BY date",
                 (str(branch_name).strip(), str(since)))
    return [{"stock_code": r[0], "date": r[1], "buy": float(r[2]), "sell": float(r[3]), "net": float(r[4])} for r in rows]


def spot_branch_names() -> List[str]:
    return [r[0] for r in _read("SELECT DISTINCT branch_name FROM spot_branch_daily", ())]


# ============================================================
# 全市場收盤完整性（上市／上櫃分開）
# ============================================================

MARKETS = ("twse", "tpex")
MARKET_STATUSES = ("complete", "closed", "source_error", "pending", "unknown")


def save_market_status(date: str, market: str, status: str, rows: int = 0, source: str = "", detail: str = "") -> None:
    if market not in MARKETS or status not in MARKET_STATUSES:
        raise ValueError(f"unknown market status: {market}/{status}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LOCK:
        with _db() as conn, conn:
            # 已確認 complete 的日子不會被之後一次失敗的重抓降級
            conn.execute(
                "INSERT INTO market_days(date,market,status,rows,source,checked_at,detail) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(date,market) DO UPDATE SET status=excluded.status, rows=excluded.rows, source=excluded.source, "
                "checked_at=excluded.checked_at, detail=excluded.detail "
                "WHERE excluded.status='complete' OR market_days.status<>'complete'",
                (str(date), market, status, int(rows or 0), str(source or ""), now, str(detail or "")[:300]))


def market_status(dates: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """{date: {"twse": status, "tpex": status}}；沒記錄的市場＝unknown。讀取失敗丟 DBError。"""
    dates = [str(d) for d in dates]
    out = {d: {m: "unknown" for m in MARKETS} for d in dates}
    for start in range(0, len(dates), 400):
        chunk = dates[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for date, market, status in _read_strict(
                f"SELECT date,market,status FROM market_days WHERE date IN ({marks})", tuple(chunk)):
            if date in out and market in MARKETS:
                out[date][market] = status
    return out


def market_closed_days(dates: Iterable[str]) -> List[str]:
    """上市與上櫃都「明確回覆當天無交易」才算全市場休市（颱風假等）；只有一邊或沒紀錄都不算。"""
    status = market_status(dates)
    return [d for d, s in status.items() if all(s[m] == "closed" for m in MARKETS)]


def stock_market(stock_code: str) -> str:
    """這檔股票屬於上市（twse）還是上櫃（tpex）：以全市場底庫寫入的 market 欄為準；不知道回空字串。"""
    rows = _read_strict("SELECT market FROM daily_bars WHERE stock_code=? AND market IN ('twse','tpex') "
                        "ORDER BY date DESC LIMIT 1", (str(stock_code).strip(),))
    return str(rows[0][0]) if rows else ""


def bar_dates(stock_code: str, dates: Iterable[str]) -> List[str]:
    """指定日期中，本地底庫有這檔日K 的日子。"""
    dates = [str(d) for d in dates]
    if not dates:
        return []
    marks = ",".join("?" * len(dates))
    return [str(r[0]) for r in _read_strict(
        f"SELECT date FROM daily_bars WHERE stock_code=? AND date IN ({marks})", (str(stock_code).strip(), *dates))]


def stock_absent_confirmed(stock_code: str, dates: Iterable[str]) -> List[str]:
    """「市場有開、這檔確定沒成交」的日子：該股所屬市場當天收盤快照 complete，而快照裡沒有這檔。
    不知道所屬市場、或該市場當天不是 complete（例如櫃買抓失敗）一律不算，交給呼叫端 retry。"""
    dates = [str(d) for d in dates]
    market = stock_market(stock_code)
    if not dates or market not in MARKETS:
        return []
    status = market_status(dates)
    have = set(bar_dates(stock_code, dates))
    return [d for d in dates if status[d][market] == "complete" and d not in have]


def append_radar_snapshot(day: str, snapshot: Dict[str, Any]) -> None:
    _write("INSERT INTO radar_snapshots(day,time,data) VALUES(?,?,?) "
           "ON CONFLICT(day,time) DO UPDATE SET data=excluded.data",
           (day, str(snapshot.get("time") or ""), json.dumps(snapshot, ensure_ascii=False, default=str)))


def load_radar_snapshots(day: str) -> List[Dict[str, Any]]:
    return [json.loads(r[0]) for r in _read("SELECT data FROM radar_snapshots WHERE day=? ORDER BY time", (day,))]


def save_radar_turnover(day: str, bucket: str, data: Dict[str, Any]) -> None:
    _write("INSERT INTO radar_turnover(day,bucket,data) VALUES(?,?,?) "
           "ON CONFLICT(day,bucket) DO UPDATE SET data=excluded.data",
           (day, bucket, json.dumps(data, ensure_ascii=False, default=str)))


def load_radar_turnover(bucket: str, before_day: str, limit: int = 40) -> List[Dict[str, Any]]:
    """同一個 5 分鐘桶、今天以前的成交占比歷史（新到舊）。"""
    rows = _read("SELECT data FROM radar_turnover WHERE bucket=? AND day<? ORDER BY day DESC LIMIT ?",
                 (bucket, before_day, int(limit)))
    return [json.loads(r[0]) for r in rows]


def ivol_record(day: str, code: str, market: str, bucket: int, lots: float,
                pred: Optional[float] = None) -> None:
    _write("INSERT INTO ivol_samples(day,code,bucket,market,lots,pred) VALUES(?,?,?,?,?,?) "
           "ON CONFLICT(day,code,bucket) DO UPDATE SET lots=excluded.lots, market=excluded.market, "
           "pred=COALESCE(excluded.pred,ivol_samples.pred)",
           (day, str(code), int(bucket), market, float(lots), float(pred) if pred is not None else None))


def ivol_record_pred(day: str, code: str, bucket: int, pred: float) -> None:
    _write("UPDATE ivol_samples SET pred=? WHERE day=? AND code=? AND bucket=?", (float(pred), day, str(code), int(bucket)))


def ivol_load_day(day: str) -> Dict[str, Dict[str, Any]]:
    """{代號: {market, points: {bucket: 累積張數}, pred: {bucket: 當時預估全日量}}}（和舊 JSON 格式相同）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for code, bucket, market, lots, pred in _read(
            "SELECT code,bucket,market,lots,pred FROM ivol_samples WHERE day=?", (day,)):
        entry = out.setdefault(str(code), {"market": market or "twse", "points": {}})
        if lots is not None:
            entry["points"][str(bucket)] = float(lots)
        if pred is not None:
            entry.setdefault("pred", {})[str(bucket)] = float(pred)
    return out


def ivol_delete_day(day: str) -> None:
    _write("DELETE FROM ivol_samples WHERE day=?", (day,))


def ivol_pending_days(today: str) -> List[str]:
    cutoff, today = _retention_window(IVOL_RAW_KEEP_DAYS, today)
    days = {r[0] for r in _read("SELECT DISTINCT day FROM ivol_samples WHERE day>=? AND day<=?", (cutoff, today))}
    days.update(d for d in (get_state("ivol_samples", {}) or {}) if cutoff <= d <= today)
    return sorted(days)


def volume_calibration_inputs(day: str) -> Tuple[Dict[str, float], Dict[str, float]]:
    """只用指定日期的正式收盤量；重試舊樣本時不能誤用今天的量。"""
    rows = _read("""WITH ranked AS (
        SELECT stock_code,date,volume,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY date DESC) AS rn
        FROM daily_bars WHERE confirmed=1 AND date<=?
    ) SELECT stock_code,MAX(CASE WHEN date=? THEN volume END)/1000.0,AVG(volume)/1000.0
      FROM ranked WHERE rn<=20 GROUP BY stock_code""", (day, day))
    return ({code: lots for code, lots, _ in rows if lots is not None},
            {code: avg for code, _, avg in rows if avg is not None})


def _put_states(conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     [(key, json.dumps(value, ensure_ascii=False, allow_nan=False), now)
                      for key, value in state.items()])


def save_ivol_learning_state(day: str, daily: dict, curves: dict,
                             errors: dict, errors_hist: dict) -> None:
    """學習結果、讀回確認與新舊原始樣本清除共用一個 transaction；失敗向上拋出並回滾。"""
    state = {"ivol_daily_medians": daily, "ivol_curves": curves,
             "ivol_errors": errors, "ivol_errors_hist": errors_hist}
    with _LOCK, _db() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _put_states(conn, state)
        for key, value in state.items():
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row is None or json.loads(row[0]) != value:
                raise RuntimeError(f"量能學習寫入驗證失敗：{key}")
        conn.execute("DELETE FROM ivol_samples WHERE day=?", (day,))
        row = conn.execute("SELECT value FROM kv WHERE key='ivol_samples'").fetchone()
        if row:
            legacy = json.loads(row[0])
            legacy.pop(day, None)
            if legacy:
                _put_states(conn, {"ivol_samples": legacy})
            else:
                conn.execute("DELETE FROM kv WHERE key='ivol_samples'")


# ============================================================
# 每日維護：所有過期資料集中在這裡一天清一次，最後做 WAL checkpoint（不做 VACUUM）
# ============================================================

RADAR_KEEP_DAYS = 3          # 族群雷達快照只用當天，多留幾天方便查問題
IVOL_RAW_KEEP_DAYS = 3       # 盤中量能原始取樣：收盤校正後就刪，這裡只是保險
TURNOVER_KEEP_DAYS = 45      # 同時點成交占比歷史（成交熱度基準）


def _retention_window(days: int, today: str = "") -> Tuple[str, str]:
    today = today or (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    return cutoff, today


def _recent_turnover(conn: sqlite3.Connection, today: str = "") -> List[Dict[str, Any]]:
    """新舊格式合併，同日同桶以新表為準，保留與每日維護相同的日曆日範圍。"""
    cutoff, today = _retention_window(TURNOVER_KEEP_DAYS, today)
    entries = {}
    for key, value in conn.execute("SELECT key,value FROM kv WHERE key GLOB 'radar_turnover:*'"):
        day = key.split(":", 1)[1]
        if cutoff <= day <= today:
            for bucket, data in json.loads(value).items():
                entries[(day, bucket)] = data
    for day, bucket, data in conn.execute(
            "SELECT day,bucket,data FROM radar_turnover WHERE day>=? AND day<=?", (cutoff, today)):
        entries[(day, bucket)] = json.loads(data)
    return [{"day": day, "bucket": bucket, "data": data}
            for (day, bucket), data in sorted(entries.items())]


def daily_maintenance(today: str = "") -> Dict[str, int]:
    """清過期的雷達快照／量能原始取樣／成交占比／用量紀錄／舊版 JSON 快照，裁掉超過保留天數的日 K，
    最後 PRAGMA wal_checkpoint(TRUNCATE)。刻意不 VACUUM：整顆重寫反而放大底層區塊重寫量。"""
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    today = today or now.strftime("%Y-%m-%d")
    base = datetime.strptime(today, "%Y-%m-%d")
    cut = lambda days: (base - timedelta(days=days)).strftime("%Y-%m-%d")
    removed: Dict[str, int] = {}
    try:
        with _LOCK:
            with _db() as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                # 舊成交占比不能重建，先搬到新表才清 JSON；新表已有的桶不覆蓋。
                conn.executemany("INSERT INTO radar_turnover(day,bucket,data) VALUES(?,?,?) "
                                 "ON CONFLICT(day,bucket) DO NOTHING",
                                 [(r["day"], r["bucket"], json.dumps(r["data"], ensure_ascii=False))
                                  for r in _recent_turnover(conn, today)])
                row = conn.execute("SELECT value FROM kv WHERE key='ivol_samples'").fetchone()
                removed["legacy_ivol_samples"] = 0
                if row:
                    legacy = json.loads(row[0])
                    kept = {d: v for d, v in legacy.items() if d >= cut(IVOL_RAW_KEEP_DAYS)}
                    removed["legacy_ivol_samples"] = len(legacy) - len(kept)
                    if not kept:
                        conn.execute("DELETE FROM kv WHERE key='ivol_samples'")
                    elif kept != legacy:
                        _put_states(conn, {"ivol_samples": kept})
                for label, sql, arg in (
                        ("radar_snapshots", "DELETE FROM radar_snapshots WHERE day < ?", cut(RADAR_KEEP_DAYS)),
                        ("ivol_samples", "DELETE FROM ivol_samples WHERE day < ?", cut(IVOL_RAW_KEEP_DAYS)),
                        ("radar_turnover", "DELETE FROM radar_turnover WHERE day < ?", cut(TURNOVER_KEEP_DAYS)),
                        ("usage_log", "DELETE FROM usage_log WHERE day < ?", cut(USAGE_KEEP_DAYS)),
                        ("spot_branch_daily", "DELETE FROM spot_branch_daily WHERE date < ?", cut(SPOT_KEEP_CALENDAR_DAYS)),
                        ("spot_branch_days", "DELETE FROM spot_branch_days WHERE date < ?", cut(SPOT_KEEP_CALENDAR_DAYS)),
                        # 舊版每 5 分鐘整包重寫的 JSON（改成資料表後就不再使用）；當天的先留著給當天讀
                        ("legacy_radar_snap", "DELETE FROM kv WHERE key LIKE 'radar_snap:%' AND key < ?",
                         "radar_snap:" + today),
                        ("legacy_radar_turnover", "DELETE FROM kv WHERE key GLOB 'radar_turnover:*' AND key <= ?",
                         "radar_turnover:" + today)):
                    removed[label] = conn.execute(sql, (arg,)).rowcount
                conn.commit()
        removed["daily_bars"] = trim_history(KEEP_DAYS)
        with _LOCK:
            with _db() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        print(f"⚠️ 本地資料庫每日維護失敗｜{type(exc).__name__}: {exc}", flush=True)
        return removed
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print("🧹 本地資料庫每日維護｜" + "｜".join(f"{k} -{v}" for k, v in removed.items())
          + f"｜檔案 {size / 1024 / 1024:.1f}MB（WAL 已 checkpoint）", flush=True)
    return removed


# ============================================================
# 狀態匯出／匯入：只搬「不能重建」的資料（使用者覆盤、盤中量能學習結果、成交占比），
# 日 K、型態分數、雷達快照、用量紀錄、名冊都會自己重建，不搬。
# ============================================================

EXPORT_PREFIXES = ("trade_review:",)
EXPORT_KEYS = ("ivol_daily_medians", "ivol_curves", "ivol_errors", "ivol_errors_hist")
EXPORT_VERSION = 2
STATE_FILE_MAX_BYTES = 10 * 1024 * 1024
STATE_JSON_MAX_BYTES = 50 * 1024 * 1024  # 解壓時也設上限，避免小 gzip 展開耗盡記憶體


def _exportable(key: str) -> bool:
    return key in EXPORT_KEYS or any(key.startswith(p) for p in EXPORT_PREFIXES)


def _normalize_trade_reviews(key: str, records: Any) -> List[Dict[str, Any]]:
    """匯出、匯入及目標資料庫共用 legacy ID；不修改原紀錄或已存在的 ID。"""
    if not key.removeprefix("trade_review:") or not isinstance(records, list):
        raise ValueError("覆盤資料必須是使用者的紀錄清單")
    normalized = []
    used = {r["trade_id"] for r in records if isinstance(r, dict) and isinstance(r.get("trade_id"), str)}
    occurrences: Dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict) or not _finite_number(record.get("at", 0)):
            raise ValueError("覆盤紀錄或 at 時間格式不符")
        trade_id = record.get("trade_id")
        if trade_id is None or trade_id == "":
            stamp = Decimal(str(record.get("at", 0)))
            timestamp_ms = int(stamp * 1000)
            # 加上內容雜湊，避免同一毫秒的不同舊紀錄在合併時被丟棄。
            # 排序欄位並統一 timestamp 表示法，JSON 欄位順序、1 與 1.0 不影響 ID。
            identity = {k: v for k, v in record.items() if k not in ("trade_id", "at")}
            identity["at"] = str(stamp.normalize())
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            user_id = key.removeprefix("trade_review:")
            base_id = f"legacy-{user_id}-{timestamp_ms}-{digest}"
            # 完全相同的舊紀錄也保留原筆數；同一備份重跑時序號仍相同。
            occurrence = occurrences.get(base_id, 0) + 1
            trade_id = base_id if occurrence == 1 else f"{base_id}-{occurrence}"
            while trade_id in used:
                occurrence += 1
                trade_id = f"{base_id}-{occurrence}"
            occurrences[base_id] = occurrence
            used.add(trade_id)
            record = {**record, "trade_id": trade_id}
        elif not isinstance(trade_id, str):
            raise ValueError("覆盤 trade_id 格式不符")
        normalized.append(record)
    return normalized


def export_state() -> Tuple[bytes, Dict[str, Any]]:
    """回傳 (gzip 後的 JSON, 摘要)。"""
    with _LOCK, _db() as conn, conn:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT key,value FROM kv").fetchall()
        state = {k: json.loads(v) for k, v in rows if _exportable(k)}
        state = {k: _normalize_trade_reviews(k, v) if k.startswith("trade_review:") else v
                 for k, v in state.items()}
        turnover = _recent_turnover(conn)
    payload = {"version": EXPORT_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
               "kv": state, "radar_turnover": turnover}
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > STATE_JSON_MAX_BYTES:
        raise ValueError("狀態內容超過解壓後 50MB 上限")
    data = gzip.compress(raw)
    if len(data) > STATE_FILE_MAX_BYTES:
        raise ValueError("狀態匯出檔超過 10MB 上限")
    return data, _state_summary(state, turnover)


def _state_summary(state: Dict[str, Any], turnover: Optional[List[dict]] = None) -> Dict[str, Any]:
    reviews = {k: v for k, v in state.items() if k.startswith("trade_review:")}
    curves = state.get("ivol_curves") or {}
    return {"users": len(reviews), "reviews": sum(len(v or []) for v in reviews.values()),
            "ivol_days": max([int((curves.get(m) or {}).get("days") or 0) for m in ("twse", "tpex")] or [0]),
            "other_keys": sum(1 for k in state if k in EXPORT_KEYS),
            "turnover_rows": len(turnover or []),
            "turnover_days": len({r["day"] for r in turnover or []})}


def _valid_day(value: Any) -> bool:
    try:
        return isinstance(value, str) and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _validate_state_payload(payload: Any) -> None:
    """交易開始前驗證結構與白名單並補齊 legacy ID，錯誤檔不能造成部分匯入。"""
    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("檔案格式、版本或資料不符，請用 /ace 匯出狀態 產生的檔案")

    def points(value: Any, ratio: bool = False) -> None:
        require(isinstance(value, dict))
        for bucket, number in value.items():
            require(bucket.isascii() and bucket.isdigit() and 540 <= int(bucket) <= 810 and int(bucket) % 5 == 0)
            require(_finite_number(number) and number >= 0 and (not ratio or number <= 1))

    require(isinstance(payload, dict))
    require(type(payload.get("version")) is int and payload["version"] in (1, EXPORT_VERSION))
    require(set(payload) <= {"version", "exported_at", "kv", "radar_turnover"})
    require(isinstance(payload.get("exported_at"), str) and isinstance(payload.get("kv"), dict))
    require(payload["version"] == 1 or "radar_turnover" in payload)
    for key, value in payload["kv"].items():
        require(_exportable(key))
        if key.startswith("trade_review:"):
            payload["kv"][key] = _normalize_trade_reviews(key, value)
            continue
        require(isinstance(value, dict) and set(value) <= {"twse", "tpex"})
        for market, entry in value.items():
            require(isinstance(entry, dict))
            if key == "ivol_curves":
                require(set(entry) <= {"days", "points", "at"})
                require(type(entry.get("days")) is int and entry["days"] >= 0)
                require(_finite_number(entry.get("at", 0)))
                points(entry.get("points", {}), ratio=True)
            elif key == "ivol_errors":
                points(entry)
            else:
                for day, values in entry.items():
                    require(_valid_day(day))
                    points(values, ratio=key == "ivol_daily_medians")
    require(isinstance(payload.get("radar_turnover", []), list))
    for row in payload.get("radar_turnover", []):
        require(isinstance(row, dict) and set(row) == {"day", "bucket", "data"})
        require(_valid_day(row["day"]))
        bucket = row["bucket"]
        require(isinstance(bucket, str) and len(bucket) == 4 and bucket.isascii() and bucket.isdigit())
        require(int(bucket[:2]) < 24 and int(bucket[2:]) < 60 and int(bucket[2:]) % 5 == 0)
        entry = row["data"]
        require(isinstance(entry, dict) and set(entry) == {"market", "sectors"})
        require(_finite_number(entry["market"]) and entry["market"] > 0)
        require(isinstance(entry["sectors"], dict))
        require(all(sid and _finite_number(v) and v >= 0 for sid, v in entry["sectors"].items()))


def import_state(data: bytes) -> Dict[str, Any]:
    """覆盤與成交占比只補缺漏；量能依市場比較日數，四份學習狀態一起更新。"""
    if len(data) > STATE_FILE_MAX_BYTES:
        raise ValueError("檔案太大（上限 10MB）")
    try:
        if data[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                raw = stream.read(STATE_JSON_MAX_BYTES + 1)
        else:
            raw = data
        if len(raw) > STATE_JSON_MAX_BYTES:
            raise ValueError("解壓後超過 50MB 上限")
        payload = json.loads(raw.decode("utf-8"))
        _validate_state_payload(payload)
    except Exception as exc:
        raise ValueError(f"檔案不是有效的狀態匯出檔：{exc}") from exc
    incoming = payload["kv"]
    cutoff, today = _retention_window(TURNOVER_KEEP_DAYS)
    turnover = [r for r in payload.get("radar_turnover", []) if cutoff <= r["day"] <= today]
    with _LOCK, _db() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = {k: json.loads(v) for k, v in conn.execute("SELECT key,value FROM kv") if _exportable(k)}
        updates = {}
        for key, value in incoming.items():
            if not key.startswith("trade_review:"):
                continue
            original = list(current.get(key) or [])
            existing = _normalize_trade_reviews(key, original)
            merged = list(existing)
            seen = {r.get("trade_id") for r in existing}
            for record in value:
                if record["trade_id"] not in seen:
                    merged.append(record)
                    seen.add(record["trade_id"])
            if merged != original:
                updates[key] = sorted(merged, key=lambda r: float(r.get("at") or 0))
        for market in ("twse", "tpex"):
            curve = (incoming.get("ivol_curves") or {}).get(market) or {}
            old_curve = (current.get("ivol_curves") or {}).get(market) or {}
            if int(curve.get("days") or 0) <= int(old_curve.get("days") or 0):
                continue
            for key in EXPORT_KEYS:
                updated = dict(updates.get(key, current.get(key)) or {})
                if market in incoming.get(key, {}):
                    updated[market] = incoming[key][market]
                else:
                    updated.pop(market, None)  # 舊版不完整檔不沿用另一條曲線的學習歷史
                if updated != current.get(key, {}):
                    updates[key] = updated
        _put_states(conn, updates)
        written_turnover = 0
        for row in turnover:
            written_turnover += conn.execute(
                "INSERT INTO radar_turnover(day,bucket,data) VALUES(?,?,?) ON CONFLICT(day,bucket) DO NOTHING",
                (row["day"], row["bucket"], json.dumps(row["data"], ensure_ascii=False))).rowcount
        current.update(updates)
        summary = _state_summary(current, _recent_turnover(conn, today))
    summary.update({"written_keys": len(updates), "written_turnover": written_turnover,
                    "exported_at": payload["exported_at"]})
    return summary


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
                # 舊紀錄不在每題刪：統一由 daily_maintenance() 一天清一次
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
