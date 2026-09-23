"""權證分點逐日買賣歷史（GitHub release「data-store」）→ 本地 SQLite。

- broker_warrant_history_store.parquet：分點 × 權證 × 日期的買進／賣出股數與金額（含 100 萬以下的小額買賣）
- warrant_meta_store.parquet：權證基本資料（含已下市）：類型、履約價、行使比例、最後交易日、到期日
- 回測 Actions 每天更新 release；Bot 只在 release 的檔案有更新時才重新下載（約 17MB），逐批寫進 SQLite，
  不把整張表放進記憶體（整張 DataFrame 約 750MB）。查詢一律讀本地，不打網路。
- release 通常落後幾個交易日；最近幾天由呼叫端用 Google Sheet（每日賣出明細、A～E 事件）補上。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import local_market_cache

RELEASE_API = os.getenv("DISCORD_AI_WARRANT_STORE_API",
                        "https://api.github.com/repos/M1229012/warrants/releases/tags/data-store").strip()
HISTORY_FILE = "broker_warrant_history_store.parquet"
META_FILE = "warrant_meta_store.parquet"
STATE_KEY = "warrant_store_sync"
CHECK_SECONDS = max(300, int(os.getenv("DISCORD_AI_WARRANT_STORE_CHECK_SECONDS", "1800") or 1800))
BATCH_ROWS = 100_000
# 只保留近 N 個日曆日：權證存續期多在一年內，更早買進的部位早已到期歸零，不影響剩餘張數（DB 約減半）
KEEP_DAYS = max(200, int(os.getenv("DISCORD_AI_WARRANT_STORE_KEEP_DAYS", "550") or 550))
_SYNC_LOCK = threading.Lock()
_LAST_CHECK = [0.0]

_HISTORY_COLUMNS = ["權證代號", "權證名稱", "標的股", "標的名稱", "分點", "日期", "買進股數", "賣出股數", "買進金額", "賣出金額"]
_META_COLUMNS = ["權證代號", "權證簡稱", "市場", "權證類型", "標的代號", "標的名稱", "最後交易日", "履約截止日",
                 "履約價_最新", "行使比例_最新", "快照日"]


def _ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS bw_daily (
        branch TEXT NOT NULL, warrant TEXT NOT NULL, stock TEXT NOT NULL, date TEXT NOT NULL,
        buy_sh INTEGER NOT NULL DEFAULT 0, sell_sh INTEGER NOT NULL DEFAULT 0,
        buy_amt INTEGER NOT NULL DEFAULT 0, sell_amt INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (branch, warrant, date))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bw_branch_date ON bw_daily(branch, date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS bw_names (
        warrant TEXT PRIMARY KEY, name TEXT DEFAULT '', stock TEXT DEFAULT '', stock_name TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS warrant_meta (
        warrant TEXT PRIMARY KEY, name TEXT DEFAULT '', market TEXT DEFAULT '', type TEXT DEFAULT '',
        stock TEXT DEFAULT '', stock_name TEXT DEFAULT '', last_trade TEXT DEFAULT '', expiry TEXT DEFAULT '',
        strike REAL, ratio REAL)""")


def _iso(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    return text[:10] if len(text) >= 10 else text


def _text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def _int(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if number == number else 0


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _query(sql: str, params: tuple = ()) -> List[tuple]:
    """讀取失敗丟 local_market_cache.DBError（查無＝空清單）。"""
    try:
        with local_market_cache._LOCK:
            with local_market_cache._db() as conn:
                _ensure_tables(conn)
                return conn.execute(sql, params).fetchall()
    except Exception as exc:
        raise local_market_cache.DBError(f"{type(exc).__name__}: {exc}") from exc


# ============================================================
# 同步：release 有更新才下載
# ============================================================

def release_assets(timeout: float = 20.0) -> Dict[str, Dict[str, Any]]:
    import requests
    response = requests.get(RELEASE_API, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    return {a["name"]: {"url": a["browser_download_url"], "updated_at": a.get("updated_at", ""), "size": a.get("size", 0)}
            for a in response.json().get("assets") or []}


def _download(url: str, timeout: float = 180.0) -> Path:
    import requests
    handle, name = tempfile.mkstemp(suffix=".parquet", prefix="ace-warrant-store-")
    os.close(handle)
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with open(name, "wb") as out:
            for chunk in response.iter_content(chunk_size=1 << 20):
                out.write(chunk)
    return Path(name)


def load_history(path: Path) -> int:
    """逐批（10 萬列）寫進 bw_daily／bw_names；整份在同一個 transaction 內替換，失敗全部回滾。"""
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(str(path))
    count, names = 0, {}
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=KEEP_DAYS)).isoformat()
    with local_market_cache._LOCK:
        with local_market_cache._db() as conn, conn:
            _ensure_tables(conn)
            conn.execute("DELETE FROM bw_daily")
            for batch in parquet.iter_batches(batch_size=BATCH_ROWS, columns=_HISTORY_COLUMNS):
                data = batch.to_pydict()
                rows = []
                for i in range(len(data["權證代號"])):
                    warrant, branch = _text(data["權證代號"][i]), _text(data["分點"][i])
                    if not warrant or not branch:
                        continue
                    stock = _text(data["標的股"][i])
                    names[warrant] = (warrant, _text(data["權證名稱"][i]), stock, _text(data["標的名稱"][i]))
                    day = _iso(data["日期"][i])
                    if day < cutoff:
                        continue
                    rows.append((branch, warrant, stock, day,
                                 _int(data["買進股數"][i]), _int(data["賣出股數"][i]),
                                 _int(data["買進金額"][i]), _int(data["賣出金額"][i])))
                conn.executemany("INSERT OR REPLACE INTO bw_daily VALUES(?,?,?,?,?,?,?,?)", rows)
                count += len(rows)
            conn.execute("DELETE FROM bw_names")
            conn.executemany("INSERT OR REPLACE INTO bw_names VALUES(?,?,?,?)", list(names.values()))
    return count


def load_meta(path: Path) -> int:
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(str(path))
    columns = [c for c in _META_COLUMNS if c in parquet.schema_arrow.names]
    rows, snapshot = [], ""
    for batch in parquet.iter_batches(batch_size=BATCH_ROWS, columns=columns):
        data = batch.to_pydict()
        get = lambda key, i: data[key][i] if key in data else None
        for i in range(len(data["權證代號"])):
            warrant = _text(get("權證代號", i))
            snapshot = max(snapshot, _iso(get("快照日", i)))
            if warrant:
                rows.append((warrant, _text(get("權證簡稱", i)), _text(get("市場", i)), _text(get("權證類型", i)),
                             _text(get("標的代號", i)), _text(get("標的名稱", i)), _iso(get("最後交易日", i)),
                             _iso(get("履約截止日", i)), _float(get("履約價_最新", i)), _float(get("行使比例_最新", i))))
    with local_market_cache._LOCK:
        with local_market_cache._db() as conn, conn:
            _ensure_tables(conn)
            conn.execute("DELETE FROM warrant_meta")
            conn.executemany("INSERT OR REPLACE INTO warrant_meta VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    local_market_cache.set_state("warrant_meta_snapshot", snapshot)
    return len(rows)


def meta_snapshot_date() -> str:
    """權證基本資料的快照日：快照當下所有掛牌中的權證都在表內；之前有交易、表內卻沒有＝已下市／到期。"""
    return str(local_market_cache.get_state("warrant_meta_snapshot", "") or "")


def sync(force: bool = False, log: Callable[[str], None] = print,
         assets: Optional[Dict[str, Dict[str, Any]]] = None,
         download: Callable[[str], Path] = _download) -> Dict[str, Any]:
    """release 的檔案 updated_at 變了才下載並替換本地資料；同一時間只跑一個同步。"""
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        state = dict(local_market_cache.get_state(STATE_KEY, {}) or {})
        assets = assets if assets is not None else release_assets()
        done = {}
        for kind, name, loader in (("history", HISTORY_FILE, load_history), ("meta", META_FILE, load_meta)):
            asset = assets.get(name)
            if not asset or (not force and state.get(kind) == asset.get("updated_at")):
                continue
            started = time.monotonic()
            path = download(asset["url"])
            try:
                rows = loader(path)
            finally:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            state[kind], state[f"{kind}_rows"] = asset.get("updated_at"), rows
            done[kind] = rows
            log(f"權證分點歷史庫：{name} 更新 {rows:,} 列｜{time.monotonic() - started:.0f} 秒")
        state["max_date"] = latest_date()
        state["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        local_market_cache.set_state(STATE_KEY, state)
        return {"updated": done, "max_date": state["max_date"]}
    finally:
        _SYNC_LOCK.release()


def maybe_sync(log: Callable[[str], None] = print) -> Optional[Dict[str, Any]]:
    """背景維護用：最多每 CHECK_SECONDS 秒問一次 GitHub（未驗證 API 每小時 60 次）。"""
    if time.monotonic() - _LAST_CHECK[0] < CHECK_SECONDS:
        return None
    _LAST_CHECK[0] = time.monotonic()
    return sync(log=log)


# ============================================================
# 查詢
# ============================================================

def available() -> bool:
    try:
        return bool(_query("SELECT 1 FROM bw_daily LIMIT 1"))
    except local_market_cache.DBError:
        return False


def latest_date() -> str:
    rows = _query("SELECT MAX(date) FROM bw_daily")
    return str(rows[0][0] or "") if rows else ""


def branch_history(branch: str) -> List[Dict[str, Any]]:
    """某分點全部權證的逐日買賣（舊→新）。"""
    rows = _query("SELECT warrant, stock, date, buy_sh, sell_sh, buy_amt, sell_amt FROM bw_daily "
                  "WHERE branch=? ORDER BY date, warrant", (str(branch).strip(),))
    return [{"warrant": r[0], "stock": r[1], "date": r[2], "buy_sh": int(r[3]), "sell_sh": int(r[4]),
             "buy_amt": int(r[5]), "sell_amt": int(r[6])} for r in rows]


def names(codes: Iterable[str]) -> Dict[str, Dict[str, str]]:
    codes = [str(c) for c in dict.fromkeys(codes) if c]
    out: Dict[str, Dict[str, str]] = {}
    for start in range(0, len(codes), 400):
        chunk = codes[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for warrant, name, stock, stock_name in _query(
                f"SELECT warrant, name, stock, stock_name FROM bw_names WHERE warrant IN ({marks})", tuple(chunk)):
            out[warrant] = {"name": name, "stock": stock, "stock_name": stock_name}
    return out


def meta(codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    codes = [str(c) for c in dict.fromkeys(codes) if c]
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(codes), 400):
        chunk = codes[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in _query(f"SELECT warrant, name, market, type, stock, stock_name, last_trade, expiry, strike, ratio "
                          f"FROM warrant_meta WHERE warrant IN ({marks})", tuple(chunk)):
            out[row[0]] = {"name": row[1], "market": row[2], "type": row[3], "stock": row[4], "stock_name": row[5],
                           "last_trade": row[6], "expiry": row[7], "strike": row[8], "ratio": row[9]}
    return out


def stock_names() -> Tuple[Dict[str, str], Dict[str, str]]:
    """（代號→名稱, 名稱→代號）：以歷史庫裡最常出現的名稱為準（權證代號會回收重用，不能用單一權證的標的名稱）。"""
    by_code: Dict[str, Tuple[str, int]] = {}
    for stock, name, count in _query("SELECT stock, stock_name, COUNT(*) FROM bw_names WHERE stock<>'' AND stock_name<>'' "
                                     "GROUP BY stock, stock_name"):
        if stock not in by_code or count > by_code[stock][1]:
            by_code[stock] = (name, count)
    names = {code: name for code, (name, _) in by_code.items()}
    return names, {name: code for code, name in names.items()}


def status() -> Dict[str, Any]:
    state = local_market_cache.get_state(STATE_KEY, {}) or {}
    return {"max_date": state.get("max_date", ""), "history_rows": state.get("history_rows", 0),
            "meta_rows": state.get("meta_rows", 0), "checked_at": state.get("checked_at", "")}
