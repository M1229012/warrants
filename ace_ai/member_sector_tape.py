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


def current(codes: List[str], max_age: float = STALE_SECONDS) -> Optional[Tuple[float, Dict[str, Dict[str, Any]]]]:
    """查詢用：最新快照不夠新（超過一個取樣間隔）就當場補拍；拍不到就回最後一張（由呼叫端判斷過期）。
    max_age：每檔報價最多可以多舊（盤中 180 秒；收盤後找最後一張快照時放寬，並由呼叫端標「最後快照」）。"""
    last = latest()
    if not last or last[1] != _day() or _now_ts() - last[0] > TAPE_SECONDS * 1.5:
        try:
            sample(codes, force=True)
        except Exception as exc:
            print(f"⚠️ 族群排行補拍快照失敗｜{tools.err_text(exc)}", flush=True)
        last = latest()
    if not last or last[1] != _day():
        return None
    return last[0], merged(max_age)[1]


def merged(max_age: float = STALE_SECONDS, now_ts: Optional[float] = None) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]]]:
    """每檔取最新一筆，某張快照漏抓的股票用前幾張補上，不會整檔消失。回傳 ({代號: 該筆時間戳}, {代號: 報價})。
    10-27 審核：新鮮度以「現在時間」逐檔判斷（原本相對最新快照再疊加整批容忍，A 已 350 秒沒更新仍算有效）。"""
    with _LOCK:
        snaps = list(_SNAPS)
    if not snaps:
        return {}, {}
    cutoff = (now_ts if now_ts is not None else _now_ts()) - max_age
    stamps: Dict[str, float] = {}
    out: Dict[str, Dict[str, Any]] = {}
    for ts, day, quotes in reversed(snaps):
        if day != snaps[-1][1] or ts < cutoff:
            break
        for code, q in quotes.items():
            if code not in out:
                out[code], stamps[code] = q, ts
    return stamps, out


def trade_price(q: Optional[Dict[str, Any]]) -> Optional[float]:
    """實際成交價：MIS 的 z／pz（trade_price）；舊格式只有 price 時，traded=True 才代表 price 是成交價。
    五檔中價、單邊掛單價一律不算（10-27 審核：買 108／賣 110 的中價 109 曾被算成 +9%）。"""
    if not q:
        return None
    value = float(q.get("trade_price") or 0)
    if value > 0:
        return value
    if "trade_price" not in q and q.get("traded") and float(q.get("price") or 0) > 0:
        return float(q["price"])
    return None


def last_trade(code: str, max_age: float = KEEP_SECONDS) -> Optional[Tuple[float, float]]:
    """這一檔今天最近一次的實際成交價（價格, 觀測時間）；超過 max_age 秒或今天沒有就回 None。"""
    cutoff, day = _now_ts() - max_age, _day()
    with _LOCK:
        for ts, snap_day, quotes in reversed(_SNAPS):
            if snap_day != day or ts < cutoff:
                return None
            price = trade_price(quotes.get(code))
            if price:
                return price, ts
    return None


def _quote_before(code: str, target: float) -> Optional[Tuple[float, Dict[str, Any]]]:
    """這一檔在時間 ≤ target 的最後一筆（觀測時間, 報價），且不早於 target − TOLERANCE。"""
    with _LOCK:
        for ts, _, quotes in reversed(_SNAPS):
            if ts > target + 1:
                continue
            if ts < target - TOLERANCE:
                return None
            if code in quotes:
                return ts, quotes[code]
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


M5_MIN, M5_MAX = 240.0, 420.0          # 「近 5 分」實際區間要落在 4～7 分鐘，否則不算
MIN_SEGMENT = 120.0                    # 量的每一段至少 2 分鐘（太短的一段量速雜訊太大）


def moves(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """每檔：m5＝近 5 分漲跌%（實際成交價、實際區間 4～7 分鐘，否則 None）；
    vol5＝近 5 分成交張數（換算成 300 秒）；ratio＝近段量增；bars＝最近三段 5 分鐘量（舊→新，缺資料＝None）。
    10-27 審核修正：
    - 所有股票用同一組目標時點（以整體最新快照往回 5／10／15／20 分），不再各自從自己的最新時間往回算
    - 量增改用「每秒成交量」比較，區間不等長（例如 7 分鐘對 3 分鐘）也不會算出假放量
    - 累積量變少（負差）＝資料異常 → 量增 None；缺段留 None，不當成 0 張
    沒有 5 分鐘前快照的股票不列入。"""
    last = latest()
    if not last:
        return {}
    anchor = last[0]
    stamps, now = merged(now_ts=anchor)          # 價量形狀以最新快照為準；報價本身新不新鮮由排行的 quotes() 把關
    out: Dict[str, Dict[str, Any]] = {}
    for code in codes:
        q = now.get(code)
        if not q:
            continue
        marks = [_quote_before(code, anchor - WINDOW * i) for i in range(1, SEGMENTS + 2)]   # −5、−10、−15、−20 分
        if not marks[0]:
            continue
        points = [(stamps[code], q)] + [m for m in marks]
        price_now, (t1, before) = trade_price(q), marks[0]
        price_before = trade_price(before)
        span = stamps[code] - t1
        m5 = ((price_now / price_before - 1) * 100
              if price_now and price_before and M5_MIN <= span <= M5_MAX else None)
        rates: List[Optional[float]] = []
        segs: List[Optional[float]] = []
        anomaly = False
        for i in range(SEGMENTS + 1):
            a, b = points[i], points[i + 1]
            if not a or not b:
                rates.append(None)
                segs.append(None)
                continue
            seconds = a[0] - b[0]
            diff = float(a[1].get("lots") or 0) - float(b[1].get("lots") or 0)
            if diff < 0:
                anomaly = True
            if seconds < MIN_SEGMENT or diff < 0:
                rates.append(None)
                segs.append(None)
                continue
            rates.append(diff / seconds)
            segs.append(diff * WINDOW / seconds)                 # 換算成 5 分鐘的張數，三根量柱才可比較
        prev = [r for r in rates[1:] if r is not None]
        ratio = (rates[0] / (sum(prev) / len(prev))
                 if not anomaly and rates[0] is not None and len(prev) == SEGMENTS and sum(prev) > 0 else None)
        bars = list(reversed(segs[:3]))
        out[code] = {"m5": m5, "vol5": segs[0], "ratio": ratio, "bars": bars, "anomaly": anomaly}
    return out


def group_m5(codes: List[str], stock_moves: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """族群近 5 分＝成分股近 5 分的等權平均；有資料的不到 80% 就不算。"""
    values = [stock_moves[c]["m5"] for c in codes if c in stock_moves and stock_moves[c].get("m5") is not None]
    if not codes or len(values) < len(codes) * MIN_COVERAGE:
        return None
    return sum(values) / len(values)
