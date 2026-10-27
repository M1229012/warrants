"""今日族群排行 v2（10-25）：盤中每 60 秒拍一張流動性名單的 MIS 報價（價格、累計張數），只放記憶體。

用途：
- 族群排行改用最新一張快照（每 60 秒更新），不再走 sector_radar 的 5 分鐘共用快取。
- 近 5 分：現價 ÷ 5 分鐘前那張快照的價格。
- 近 5 分量／近段量增：累計張數相減；近段量增＝最近 5 分鐘量 ÷ 前三段 5 分鐘平均（需要 20 分鐘資料，不是歷史同時段量比）。

快照補不回來，只能從開始取樣後累積：剛開盤或 Bot 剛重啟時近 5 分顯示「累積中」，跟漲觀察約 20 分鐘後才出現。
取樣由 discord_ai_bot 的盤中取樣執行緒呼叫 sample()；查詢時快照太舊也會當場補拍一張。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import warrant_ai_tools as tools

TAPE_SECONDS = max(30, tools._env_int("DISCORD_AI_MEMBER_TAPE_SECONDS", 60))
WINDOW = 300                      # 近 5 分
SEGMENTS = 3                      # 量增比較前幾段 5 分鐘
TOLERANCE = 120                   # 找「5 分鐘前」那張快照時，最多容許再早 2 分鐘
STALE_SECONDS = 180               # 最新快照超過 3 分鐘＝資料過期
KEEP_SECONDS = WINDOW * (SEGMENTS + 1) + TOLERANCE + 600
MIN_COVERAGE = 0.80

_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_SNAPS: List[Tuple[float, str, Dict[str, Dict[str, Any]]]] = []     # [(時間戳, 日期, {代號: 報價})]


def _now_ts() -> float:
    return tools.taipei_now().timestamp()


def _day() -> str:
    return tools.taipei_now().strftime("%Y-%m-%d")


def clear() -> None:
    with _LOCK:
        _SNAPS.clear()


def record(quotes: Dict[str, Dict[str, Any]], ts: Optional[float] = None, day: Optional[str] = None) -> None:
    """存一張快照；換日清空、只留最近 KEEP_SECONDS 秒。空的快照不存。"""
    if not quotes:
        return
    ts, day = ts if ts is not None else _now_ts(), day or _day()
    with _LOCK:
        _SNAPS[:] = [s for s in _SNAPS if s[1] == day and ts - s[0] <= KEEP_SECONDS and s[0] < ts]
        _SNAPS.append((ts, day, dict(quotes)))


# 10-27：MIS 常常 20 秒內只回一半（Log：766 檔只拿到 198～600 檔），背景取樣給長一點時限一次抓齊；
# 查詢時補拍仍用 sector_radar 的 20 秒，避免整題逾時。
BACKGROUND_DEADLINE = max(20.0, tools._env_float("DISCORD_AI_MEMBER_TAPE_DEADLINE", 50.0))


def _fetch(codes: List[str], deadline: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    import sector_radar
    return sector_radar._fetch_stock_quotes(list(codes), deadline)


def sample(codes: Optional[List[str]] = None, force: bool = False) -> bool:
    """拍一張快照（同一時間只有一個執行緒在抓；距離上一張不到 TAPE_SECONDS 就跳過）。回傳是否有新快照。
    force＝查詢當下補拍（用短時限）；背景取樣用 BACKGROUND_DEADLINE。"""
    import member_sector_rank as rank
    if not rank._live_allowed():
        return False
    # 查詢補拍不排隊：背景正在抓（最多 50 秒）就直接用現有快照，避免整題逾時
    if not _FETCH_LOCK.acquire(blocking=not force):
        return False
    try:
        last = latest()
        if not force and last and _now_ts() - last[0] < 10:
            return False                  # 剛被查詢補拍過；間隔由 run_loop 控制
        if force and last and last[1] == _day() and _now_ts() - last[0] <= TAPE_SECONDS * 1.5:
            return False                  # 背景剛拍好一張，直接用
        codes = sorted(codes if codes is not None else rank.liquid_pool())
        if not codes:
            return False
        began = time.perf_counter()
        with rank._live_scope():
            quotes = _fetch(codes, None if force else BACKGROUND_DEADLINE)
        record(quotes)
        print(f"🎞️ 族群排行快照｜{len(quotes)}/{len(codes)} 檔｜{time.perf_counter() - began:.1f}s｜累積 {len(_SNAPS)} 張"
              f"｜{'查詢補拍' if force else '背景'}", flush=True)
        return bool(quotes)
    finally:
        _FETCH_LOCK.release()


def run_loop(stop: threading.Event, gate) -> None:
    """獨立執行緒：gate() 為真（盤中、取樣開著）時每 TAPE_SECONDS 秒拍一張；抓取時間算在間隔裡。"""
    while not stop.is_set():
        began = time.monotonic()
        try:
            if gate():
                sample()
        except Exception as exc:
            print(f"⚠️ 族群排行快照略過｜{tools.err_text(exc)}", flush=True)
        if stop.wait(max(5.0, TAPE_SECONDS - (time.monotonic() - began))):
            break


def latest() -> Optional[Tuple[float, str, Dict[str, Dict[str, Any]]]]:
    with _LOCK:
        return _SNAPS[-1] if _SNAPS else None


def current(codes: List[str]) -> Optional[Tuple[float, Dict[str, Dict[str, Any]]]]:
    """查詢用：最新快照不夠新（超過一個取樣間隔）就當場補拍；拍不到就回最後一張（由呼叫端判斷過期）。"""
    last = latest()
    if not last or last[1] != _day() or _now_ts() - last[0] > TAPE_SECONDS * 1.5:
        try:
            sample(codes, force=True)
        except Exception as exc:
            print(f"⚠️ 族群排行補拍快照失敗｜{tools.err_text(exc)}", flush=True)
        last = latest()
    if not last or last[1] != _day():
        return None
    return last[0], merged()[1]


def merged(max_age: float = STALE_SECONDS) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]]]:
    """10-27：每檔取最近 max_age 秒內最新的一筆（某張快照漏抓的股票用前幾張補上，不會整檔消失）。
    回傳 ({代號: 該筆時間戳}, {代號: 報價})。"""
    with _LOCK:
        snaps = list(_SNAPS)
    if not snaps:
        return {}, {}
    newest = snaps[-1][0]
    stamps: Dict[str, float] = {}
    out: Dict[str, Dict[str, Any]] = {}
    for ts, day, quotes in reversed(snaps):
        if day != snaps[-1][1] or newest - ts > max_age:
            break
        for code, q in quotes.items():
            if code not in out:
                out[code], stamps[code] = q, ts
    return stamps, out


def _quote_before(code: str, target: float) -> Optional[Dict[str, Any]]:
    """這一檔在時間 ≤ target 的最後一筆，且不早於 target − TOLERANCE（中間漏抓的快照會往前找）。"""
    with _LOCK:
        for ts, _, quotes in reversed(_SNAPS):
            if ts > target + 1:
                continue
            if ts < target - TOLERANCE:
                return None
            if code in quotes:
                return quotes[code]
    return None


def status(now_ts: Optional[float] = None) -> Dict[str, Any]:
    """{state: none|warming|ok|stale, age, span, time, follow_ready, follow_at}。
    warming＝還沒有 5 分鐘前的快照；follow_ready＝已有 20 分鐘資料，可以算近段量增。"""
    now_ts = now_ts if now_ts is not None else _now_ts()
    with _LOCK:
        snaps = [s for s in _SNAPS if s[1] == _day()]
    if not snaps:
        return {"state": "none", "age": None, "span": 0, "time": "", "follow_ready": False, "follow_at": ""}
    first, last = snaps[0][0], snaps[-1][0]
    span, age = last - first, now_ts - last
    follow_need = WINDOW * (SEGMENTS + 1)
    from datetime import datetime
    zone = tools.taipei_now().tzinfo
    clock = lambda ts: datetime.fromtimestamp(ts, zone).strftime("%H:%M")
    state = "stale" if age > STALE_SECONDS else "warming" if span < WINDOW else "ok"
    return {"state": state, "age": age, "span": span, "time": datetime.fromtimestamp(last, zone).strftime("%H:%M:%S"),
            "follow_ready": span >= follow_need, "follow_at": clock(first + follow_need)}


def moves(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """每檔：m5＝近 5 分漲跌%；vol5＝近 5 分成交張數；ratio＝近段量增（前三段平均為 0 或資料不足＝None）；
    bars＝最近三段 5 分鐘量（舊→新）。沒有 5 分鐘前快照的股票不列入。"""
    stamps, now = merged()
    out: Dict[str, Dict[str, Any]] = {}
    for code in codes:
        q = now.get(code)
        if not q:
            continue
        # 以這一檔自己最新那筆的時間往回推（每檔抓到的時間可能差一兩張快照）
        marks = [_quote_before(code, stamps[code] - WINDOW * i) for i in range(1, SEGMENTS + 2)]   # −5、−10、−15、−20 分
        before = marks[0]
        if not before or not float(before.get("price") or 0):
            continue
        m5 = (float(q["price"]) / float(before["price"]) - 1) * 100
        lots = [float(q.get("lots") or 0)] + [float(m.get("lots") or 0) if m else None for m in marks]
        segs = [lots[i] - lots[i + 1] if lots[i] is not None and lots[i + 1] is not None else None
                for i in range(len(lots) - 1)]                                    # [近5分, 前一段, 前二段, 前三段]
        vol5 = max(0.0, segs[0]) if segs[0] is not None else None
        prev = [max(0.0, s) for s in segs[1:] if s is not None]
        ratio = (vol5 / (sum(prev) / len(prev))) if vol5 is not None and len(prev) == SEGMENTS and sum(prev) > 0 else None
        bars = [max(0.0, s) if s is not None else 0.0 for s in reversed(segs[:3])]
        out[code] = {"m5": m5, "vol5": vol5, "ratio": ratio, "bars": bars}
    return out


def group_m5(codes: List[str], stock_moves: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """族群近 5 分＝成分股近 5 分的等權平均；有資料的不到 80% 就不算。"""
    values = [stock_moves[c]["m5"] for c in codes if c in stock_moves]
    if not codes or len(values) < len(codes) * MIN_COVERAGE:
        return None
    return sum(values) / len(values)
