"""K 線型態描述器 v1（規格：docs/kline_spec_v1.md，條號標在註解）。

用途：描述「今天」的技術結構給 AI；不計分、不回測、不保存逐日狀態、圖卡不顯示。
輸入日 K（Open／High／Low／Close／Volume，index 為日期）＋公司行動事件；純計算、不打 API。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


def _env(name: str, default: float) -> float:
    try:
        return float(os.getenv(f"DISCORD_AI_KLINE_{name}", default))
    except (TypeError, ValueError):
        return default


# §13 參數（第一版凍結）
SEARCH_DAYS = int(_env("SEARCH_DAYS", 60))
WARMUP_MIN = int(_env("WARMUP_MIN", 21))
ZIGZAG_ATR = _env("ZIGZAG_ATR", 1.5)
MIN_BARS = int(_env("MIN_BARS", 15))
ANCHOR_GAP = int(_env("ANCHOR_GAP", 3))
WICK_TOL = _env("WICK_TOL", 0.7)
TOUCH = _env("TOUCH", 0.3)
HIGH_DAYS = (70, 20)                                             # §9a 創新高／新低：只看圖上 70 根 K 棒內（09-30 使用者決定）
PREV_BREAK, BREAK_LOOKBACK = _env("PREV_BREAK", 0.5), int(_env("BREAK_LOOKBACK", 5))
RANGE_MIN, RANGE_MAX, RANGE_ATR = int(_env("RANGE_MIN", 15)), int(_env("RANGE_MAX", 40)), _env("RANGE_ATR", 4.0)
SPIKE_RATIO =_env("SPIKE_RATIO", 2.0)   # §4.3a：進、出兩段都 ≥ 區間波段中位數×此倍數＝V 型轉折，不當錨點
CROSS_LOW, CROSS_HIGH, CROSS_MAX = _env("CROSS_LOW", 0.25), _env("CROSS_HIGH", 0.5), _env("CROSS_MAX", 0.10)
FIT_MAX = _env("FIT_MAX", 0.5)
FLAT_SLOPE, FLAT_DRIFT = _env("FLAT_SLOPE", 0.05), _env("FLAT_DRIFT", 0.25)
CONVERGE = _env("CONVERGE", 0.7)
STABLE_LO, STABLE_HI = _env("STABLE_LO", 0.8), _env("STABLE_HI", 1.25)
BREAK = _env("BREAK", 0.5)
VOL_RATIO = _env("VOL_RATIO", 1.5)
RETEST_ZONE, RETEST_DAYS = _env("RETEST_ZONE", 0.3), int(_env("RETEST_DAYS", 5))
EVENT_DAYS = int(_env("EVENT_DAYS", 20))
APEX_NEAR = _env("APEX_NEAR", 0.75)
TREND_DIFF = _env("TREND_DIFF", 0.2)
GAP_ATR = _env("GAP_ATR", 0.3)
SCAN_DAYS = EVENT_DAYS + RETEST_DAYS + 1          # 逐日掃描天數：更早的事件到今天一定已結束
SHARE_EVENTS = {"權", "權息", "分割", "面額變更", "減資"}
_LOGGED: set = set()


def _p(v: float) -> float:
    return round(float(v), 2)


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%m/%d")


# ---------------------------------------------------------------- §1.3 價格還原
def adjust(df: pd.DataFrame, events: Optional[Dict[str, Any]]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """只還原已核對的「純現金除息」；其他事件只加旗標。events＝{"status": ok/failed, "items": [...], "coverage": [...]}"""
    out = df.copy()
    flags: Dict[str, Any] = {"F1": False, "F2": [], "F3_dates": [], "F2_days": []}
    if not events or events.get("status") != "ok":
        flags["F2"].append("公司行動資料未核實")
        return out, flags
    # 資料源涵蓋範圍（例：免費版查不到減資）只寫 Log，不給 AI、不上圖（使用者決定）
    for note in events.get("coverage") or []:
        if note not in _LOGGED:
            _LOGGED.add(note)
            print(f"ℹ️ K 線型態：{note}", flush=True)
    first, last = out.index[0], out.index[-1]
    for ev in sorted(events.get("items") or [], key=lambda e: e["date"]):
        day = pd.Timestamp(ev["date"])
        if day > last or day <= first:
            continue
        kind = ev.get("kind", "")
        applied = {e['date']: e for e in out.attrs.get('share_adjustments') or []}
        if ev['date'] in applied:
            flags['F1'] = True
            if not applied[ev['date']].get('volume_factor'):
                flags['F3_dates'].append(day)
            continue
        if kind in {"息", "除息"} and ev.get("factor"):
            mask = out.index < day
            out.loc[mask, ["Open", "High", "Low", "Close"]] *= float(ev["factor"])
            flags["F1"] = True
        else:
            flags["F2"].append(f"{_d(day)} {kind}事件未還原")
            flags["F2_days"].append(day)
        if kind in SHARE_EVENTS:
            flags["F3_dates"].append(day)
    return out, flags


# ---------------------------------------------------------------- §2 ATR
def _atr_prev(h, l, c) -> np.ndarray:
    """atr_prev[i]＝到 i 前一日為止的 SMA(TR,20)；資料不足為 NaN。"""
    n = len(c)
    tr = np.full(n, np.nan)
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    out = np.full(n, np.nan)
    for i in range(21, n):
        out[i] = np.mean(tr[i - 20:i])
    out[out <= 0] = np.nan
    return out


# ---------------------------------------------------------------- §3 ZigZag
def zigzag(h, l, c, atr_prev) -> List[Dict[str, Any]]:
    """轉折點 [{type, idx, price, confirm}]；收盤確認、每根最多確認一個、新方向自確認日收盤起追蹤。"""
    piv: List[Dict[str, Any]] = []
    n = len(c)
    start = next((i for i in range(n) if not np.isnan(atr_prev[i])), None)
    if start is None:
        return piv
    direction, hi, lo = 0, start, start
    ext_i, ext_p, track_from = start, float(c[start]), start
    for i in range(start, n):
        thr = ZIGZAG_ATR * atr_prev[i]
        if direction == 0:
            hi = i if h[i] > h[hi] else hi
            lo = i if l[i] < l[lo] else lo
            down, up = c[i] <= h[hi] - thr, c[i] >= l[lo] + thr
            if down and up:
                if hi == lo:
                    continue
                down, up = hi < lo, lo < hi
            if down:
                piv.append({"type": "H", "idx": hi, "price": float(h[hi]), "confirm": i})
                direction, ext_i, ext_p, track_from = -1, i, float(c[i]), i
            elif up:
                piv.append({"type": "L", "idx": lo, "price": float(l[lo]), "confirm": i})
                direction, ext_i, ext_p, track_from = 1, i, float(c[i]), i
            continue
        if i > track_from:
            if direction == 1 and h[i] > ext_p:
                ext_i, ext_p = i, float(h[i])
            elif direction == -1 and l[i] < ext_p:
                ext_i, ext_p = i, float(l[i])
        if direction == 1 and c[i] <= ext_p - thr:
            piv.append({"type": "H", "idx": ext_i, "price": ext_p, "confirm": i})
            direction, ext_i, ext_p, track_from = -1, i, float(c[i]), i
        elif direction == -1 and c[i] >= ext_p + thr:
            piv.append({"type": "L", "idx": ext_i, "price": ext_p, "confirm": i})
            direction, ext_i, ext_p, track_from = 1, i, float(c[i]), i
    return piv


# ---------------------------------------------------------------- §4 整理型態
def _line(a: Dict, b: Dict) -> Tuple[float, float]:
    s = (b["price"] - a["price"]) / (b["idx"] - a["idx"])
    return s, a["price"] - s * a["idx"]


def _v_spikes(seq: List[Dict]) -> set:
    """§4.3a：進、出兩段波段都 ≥ 區間波段中位數×SPIKE_RATIO 的轉折（急殺後急拉／急拉後急殺）。
    只用已確認的前後轉折；最後一個轉折沒有出段，不判。"""
    legs = [abs(b["price"] - a["price"]) for a, b in zip(seq, seq[1:])]
    if len(legs) < 3:
        return set()
    lim = SPIKE_RATIO * float(np.median(legs))
    return {seq[i]["idx"] for i in range(1, len(seq) - 1) if legs[i - 1] >= lim and legs[i] >= lim}


def _side_lines(points: List[Dict], upper: bool, closes: np.ndarray, start: int, end: int, ref: float,
                spikes: set = frozenset()) -> List[Dict]:
    """某一邊所有合格的邊界線（§4.3、§4.4 單邊條件）；V 型轉折不當錨點（§4.3a）。"""
    out = []
    idx = np.arange(start, end + 1)
    seg = closes[start:end + 1]
    for i, a in enumerate(points):
        for b in points[i + 1:]:
            if b["idx"] - a["idx"] < ANCHOR_GAP or a["idx"] in spikes or b["idx"] in spikes:
                continue
            s, k = _line(a, b)
            dist = [(p["price"] - (s * p["idx"] + k)) * (1 if upper else -1) for p in points]
            if max(dist) > WICK_TOL * ref:
                continue
            touches = sum(1 for x in dist if abs(x) <= TOUCH * ref)
            if touches < 2:
                continue
            over = (seg - (s * idx + k)) * (1 if upper else -1)
            if (over > CROSS_HIGH * ref).any():
                continue                                   # §附則C：形成期已有有效突破
            cross = float(((over > CROSS_LOW * ref) & (over <= CROSS_HIGH * ref)).mean())
            out.append({"s": s, "k": k, "a": a["idx"], "b": b["idx"], "touches": touches, "cross": cross,
                        "fit": float(np.mean(np.abs(dist))) / ref})
    return out


def _classify(u: Dict, lo: Dict, start: int, end: int, ref: float) -> Optional[Dict[str, Any]]:
    common = max(u["a"], lo["a"])
    t = np.arange(common, end + 1)
    w = (u["s"] * t + u["k"]) - (lo["s"] * t + lo["k"])
    if len(t) < 2 or (w <= 0).any():
        return None
    w_ref = float(np.median(w))
    def flat(line):
        drift = abs(line["s"] * (end - start))
        return abs(line["s"]) / ref < FLAT_SLOPE and drift <= FLAT_DRIFT * w_ref
    fu, fl = flat(u), flat(lo)
    su, sl = u["s"], lo["s"]
    converging = w[-1] <= w[0] * CONVERGE
    stable = STABLE_LO <= w[-1] / w[0] <= STABLE_HI
    kind = None
    if fu and fl:
        kind = "箱型整理"
    elif fu and sl > 0 and not fl and converging:
        kind = "上升三角"
    elif fl and su < 0 and not fu and converging:
        kind = "下降三角"
    elif not fu and not fl:
        if su < 0 < sl and converging:
            kind = "對稱三角收斂"
        elif 0 < su < sl and converging:
            kind = "上升楔形"
        elif su < sl < 0 and converging:
            kind = "下降楔形"
        elif su > 0 and sl > 0 and stable:
            kind = "上升通道"
        elif su < 0 and sl < 0 and stable:
            kind = "下降通道"
    return {"kind": kind, "w_ref": w_ref} if kind else None


def build_formation(piv: List[Dict], closes: np.ndarray, day: int, lo_bound: int, ref: float,
                    reached: Optional[List[Dict]] = None) -> Optional[Dict[str, Any]]:
    """用 day 前一日以前已確認的轉折，建立 day 當天可用的最佳型態（§4.2～§4.6）。
    day 當天上下緣已交會（線距 ≤0 或過交會點）的候選不參與排序；reached 有給時收集其中排序最好的一個。"""
    end = day - 1
    usable = [p for p in piv if p["confirm"] <= end and p["idx"] >= lo_bound]
    best, best_key, gone_key = None, None, None
    spikes = _v_spikes(usable)
    for start in sorted({p["idx"] for p in usable}):
        if end - start + 1 < MIN_BARS:
            continue
        hs = [p for p in usable if p["type"] == "H" and p["idx"] >= start]
        ls = [p for p in usable if p["type"] == "L" and p["idx"] >= start]
        if len(hs) < 2 or len(ls) < 2:
            continue
        ups = _side_lines(hs, True, closes, start, end, ref, spikes)
        downs = _side_lines(ls, False, closes, start, end, ref, spikes)
        for u in ups:
            for lo in downs:
                if (u["cross"] + lo["cross"]) > CROSS_MAX or (u["fit"] + lo["fit"]) / 2 > FIT_MAX:
                    continue
                cls = _classify(u, lo, start, end, ref)
                if not cls:
                    continue
                key = (-(u["touches"] + lo["touches"]), u["cross"] + lo["cross"], (u["fit"] + lo["fit"]) / 2,
                       -(end - start), -start, u["a"], lo["a"])
                cand = {"kind": cls["kind"], "start": start, "upper": (u["s"], u["k"]), "lower": (lo["s"], lo["k"]),
                        "ref": ref, "end": end,
                        "anchors": {"upper": [u["a"], u["b"]], "lower": [lo["a"], lo["b"]]}}
                apex = _apex(cand)
                if _at(cand["upper"], day) - _at(cand["lower"], day) <= 0 or (apex is not None and day >= apex):
                    if reached is not None and (gone_key is None or key < gone_key):
                        gone_key = key
                        reached[:] = [dict(cand, apex=apex)]
                    continue
                if best_key is None or key < best_key:
                    best_key, best = key, cand
    return best


def _at(line: Tuple[float, float], i: int) -> float:
    return line[0] * i + line[1]


def _apex(f: Dict) -> Optional[float]:
    (su, ku), (sl, kl) = f["upper"], f["lower"]
    if abs(su - sl) < 1e-12:
        return None
    x = (kl - ku) / (su - sl)
    return x if x > f["start"] else None


# ---------------------------------------------------------------- §5 突破事件
def track(df: pd.DataFrame, piv: List[Dict], atr_prev: np.ndarray, vol_ratio: np.ndarray, f3_days: set,
          last_official: int) -> Dict[str, Any]:
    """逐日掃描（§5.1）；回傳今天的型態與狀態。last_official＝最後一根正式收盤的 index。"""
    c, h, l = (df[k].to_numpy(dtype=float) for k in ("Close", "High", "Low"))
    n = len(c)
    search_lo = max(0, n - SEARCH_DAYS)
    lo_bound = search_lo
    event, ended, current, invalid = None, None, None, None
    first_day = max(search_lo + MIN_BARS, 22)       # 從搜尋範圍逐日重建（不可截短，見審查 #1）
    for d in range(first_day, last_official + 1):
        if event is None:
            ref = atr_prev[d]
            if np.isnan(ref):
                continue
            reached: List[Dict] = []
            f = build_formation(piv, c, d, lo_bound, ref, reached)
            current = f
            if reached and not f:
                # 未突破就到交會點：不再當有效區間；只在剛交會時客觀描述一次（不下失效結論）
                invalid = dict(reached[0], inv_day=d)
            if not f:
                continue
            m = BREAK * ref
            up, dn = _at(f["upper"], d), _at(f["lower"], d)
            if c[d] > up + m or c[d] < dn - m:
                event = dict(f, dir=1 if c[d] > up + m else -1, bday=d, ended=None)
                ended = None
            continue
        # 事件進行中：先查結束條件（§5.4）
        f, ref = event, event["ref"]
        m = BREAK * ref
        up, dn = _at(f["upper"], d), _at(f["lower"], d)
        apex = _apex(f)
        reason = None
        if up - dn <= 0 or (apex is not None and d >= apex) or d - event["bday"] > EVENT_DAYS:
            reason = "expired"                              # 交會或超過 20 日：不再描述舊事件（使用者決定）
        elif (event["dir"] == 1 and c[d] < dn - m) or (event["dir"] == -1 and c[d] > up + m):
            reason = "crossed"                              # 當天客觀描述「收盤有效越過原另一側邊界」
        if reason:
            event["ended"], event["end_day"], event["other_edge"] = reason, d, (dn if event["dir"] == 1 else up)
            ended, lo_bound, event, current = event, event["bday"] + 1, None, None
    return {"event": event, "ended": ended, "current": current, "invalid": invalid}


def _event_status(ev: Dict, day: int, c, h, l, official: bool) -> Tuple[str, List[str]]:
    """§5.3 主狀態（依優先順序）＋附加描述；向下對稱。"""
    ref, sign = ev["ref"], ev["dir"]
    m = BREAK * ref
    edge = _at(ev["upper"] if sign == 1 else ev["lower"], day)
    other = _at(ev["lower"] if sign == 1 else ev["upper"], day)
    k = day - ev["bday"]
    beyond = (c[day] - edge) * sign
    word, oword = ("上緣", "下緣") if sign == 1 else ("下緣", "上緣")
    act = "突破" if sign == 1 else "跌破"
    extra: List[str] = []
    if k == 0:
        main = f"首日有效{act}{word} {_p(edge)}"
    elif beyond > m and k == 1:
        main = f"連兩日收在{word} {_p(edge)} {'之上' if sign == 1 else '之下'}"
    elif beyond > m:
        main = f"持續位於{word} {_p(edge)} {'之上' if sign == 1 else '之下'}（{act}後第 {k} 日）"
    elif abs(beyond) <= m:
        main = f"回到原{word} {_p(edge)} 附近（{act}後第 {k} 日）"
    else:
        main = f"收盤回到原上下緣之間（{word} {_p(edge)}、{oword} {_p(other)}；{act}後第 {k} 日）"
    if k >= 1:
        zone_lo, zone_hi = edge - RETEST_ZONE * ref, edge + RETEST_ZONE * ref
        if l[day] <= zone_hi and h[day] >= zone_lo:
            if k <= RETEST_DAYS:
                extra.append(f"日 K 碰到回測區（{act}後第 {k} 日）"
                             + ("，收盤守在" + word + ("之上" if sign == 1 else "之下") if beyond >= 0 else ""))
            else:
                extra.append("接近原邊界")
    if not official:
        main = "盤中：" + main + "（待收盤確認）"
    return main, extra


# ---------------------------------------------------------------- §6 趨勢
def trend(piv: List[Dict], c, ma20, atr_prev, day: int, ohlc=None) -> Optional[Dict[str, Any]]:
    atr = atr_prev[day]
    if np.isnan(atr) or day < 6 or np.isnan(ma20[day]) or np.isnan(ma20[day - 5]):
        return None
    ok = [p for p in piv if p["confirm"] <= day - 1]
    hs, ls = [p for p in ok if p["type"] == "H"][-2:], [p for p in ok if p["type"] == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return None
    d = TREND_DIFF * atr
    up = hs[1]["price"] - hs[0]["price"] >= d and ls[1]["price"] - ls[0]["price"] >= d and ma20[day] > ma20[day - 5]
    down = hs[0]["price"] - hs[1]["price"] >= d and ls[0]["price"] - ls[1]["price"] >= d and ma20[day] < ma20[day - 5]
    if not (up or down):
        return None
    base = ls if up else hs
    coef = _line(base[0], base[1])
    if ohlc is not None:     # 10-06：趨勢線跟三角線同一套品質（碰線要回測、有反應、≥3 次）；只連兩點不算線
        H, L, O = ohlc
        A = np.where(np.isnan(atr_prev), np.nanmedian(atr_prev), atr_prev)
        P, B, Q = (L, np.minimum(O, c), H) if up else (H, np.maximum(O, c), L)
        q = _tri_line(P, B, Q, c, A, coef[0], coef[1], int(base[0]["idx"]), day - 1, -1 if up else 1, float(A[day - 1]))
        if q is None or len(q["a"]) < 3 or q["brk"] is not None:
            coef = None
    line = _at(coef, day) if coef else None
    m = BREAK * atr
    kind = "上升趨勢" if up else "下降趨勢"
    notes = []
    if line is not None and up and c[day] < line - m:
        notes.append("跌破上升趨勢線")
    if line is not None and down and c[day] > line + m:
        notes.append("站上下降趨勢線")
    if up and c[day] < ls[1]["price"] - m:
        notes.append("原上升結構受破壞")
    if down and c[day] > hs[1]["price"] + m:
        notes.append("原下降結構受破壞")
    text = f"{kind}（轉折高低點{'墊高' if up else '降低'}）" + (f"，趨勢線約 {_p(line)}" if line is not None else "")         + ("；" + "、".join(notes) if notes else "")
    return {"kind": kind, "line": _p(line) if line is not None else None, "notes": notes, "text": text,
            "line_coefficients": coef, "points": [dict(p) for p in base]}


# ---------------------------------------------------------------- §7 缺口
def gaps(df: pd.DataFrame, atr_prev, last_official: int, f2_days: set) -> Dict[str, Any]:
    h, l, c = (df[k].to_numpy(dtype=float) for k in ("High", "Low", "Close"))
    n = len(c)
    zones: List[Dict[str, Any]] = []
    today_notes: List[str] = []
    for i in range(max(1, n - SEARCH_DAYS), last_official + 1):
        for z in zones:                                    # 先用今天的 K 更新既有缺口（§7.2）
            if z["filled"]:
                continue
            before = (z["bottom"], z["top"])
            if z["dir"] == 1 and l[i] < z["top"]:
                z["top"] = max(z["bottom"], l[i])
                z["filled"] = l[i] <= z["bottom"]
            elif z["dir"] == -1 and h[i] > z["bottom"]:
                z["bottom"] = min(z["top"], h[i])
                z["filled"] = h[i] >= z["top"]
            if i == last_official and (z["bottom"], z["top"]) != before:
                today_notes.append(f"{z['date']} {z['label']}今日{'已回補' if z['filled'] else '部分回補'}")
        atr = atr_prev[i]
        if np.isnan(atr):
            continue
        mark = "（跨公司行動事件，未排除）" if df.index[i] in f2_days else ""
        if l[i] - h[i - 1] >= GAP_ATR * atr:
            zones.append({"dir": 1, "bottom": h[i - 1], "top": l[i], "date": _d(df.index[i]), "label": "向上缺口" + mark, "filled": False})
        if l[i - 1] - h[i] >= GAP_ATR * atr:
            zones.append({"dir": -1, "bottom": h[i], "top": l[i - 1], "date": _d(df.index[i]), "label": "向下缺口" + mark, "filled": False})
    price = c[-1]
    if last_official < n - 1:
        # 今天是盤中／暫定 K：用今天截至目前的高低價更新，標「截至目前」，不和昨日狀態混用（審查 #6）
        i = n - 1
        for z in zones:
            if z["filled"]:
                continue
            if z["dir"] == 1 and l[i] < z["top"]:
                z["filled"] = l[i] <= z["bottom"]
                z["top"] = max(z["bottom"], l[i])
                today_notes.append(f"{z['date']} {z['label']}盤中{'已回補' if z['filled'] else '部分回補'}（截至目前）")
            elif z["dir"] == -1 and h[i] > z["bottom"]:
                z["filled"] = h[i] >= z["top"]
                z["bottom"] = min(z["top"], h[i])
                today_notes.append(f"{z['date']} {z['label']}盤中{'已回補' if z['filled'] else '部分回補'}（截至目前）")
    live = [z for z in zones if not z["filled"]]
    inside = [z for z in live if z["bottom"] <= price <= z["top"]]
    below = [z for z in live if z["top"] < price]
    above = [z for z in live if z["bottom"] > price]
    lines = list(today_notes)
    for z in inside[:1]:
        lines.append(f"現價在 {z['date']} {z['label']} {_p(z['bottom'])}～{_p(z['top'])} 區間內（進入缺口區）")
    sup = max(below, key=lambda z: z["top"]) if below else None
    res = min(above, key=lambda z: z["bottom"]) if above else None
    if sup:
        lines.append(f"{sup['date']} {sup['label']} {_p(sup['bottom'])}～{_p(sup['top'])} 未回補（下方候選支撐）")
    if res:
        lines.append(f"{res['date']} {res['label']} {_p(res['bottom'])}～{_p(res['top'])} 未回補（上方候選壓力）")
    return {"support": sup, "resistance": res, "inside": inside[:1], "text": lines}


# ---------------------------------------------------------------- §8 近期 K 線
def candles(df: pd.DataFrame, atr_prev, day: int, f3: bool) -> List[str]:
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    vol = df["Volume"].to_numpy(dtype=float) if "Volume" in df else None
    out: List[str] = []

    def prior(s: int) -> int:                               # §8.1：+1 前段上漲、-1 前段下跌、0 無
        if s - 6 < 0 or np.isnan(atr_prev[s]):
            return 0
        chg = c[s - 1] - c[s - 6]
        return 1 if chg > atr_prev[s] else -1 if chg < -atr_prev[s] else 0

    def geo(i):
        rng = max(h[i] - l[i], 1e-9)
        return rng, abs(c[i] - o[i]), min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])

    if day < 8:
        return out
    s1, a1 = day, atr_prev[day]
    if not np.isnan(a1):
        rng, body, lower, upper = geo(day)
        big = body >= 1.2 * a1 and body >= 0.6 * rng
        pr = prior(s1)
        if big and c[day] > o[day]:
            out.append("長紅K（跌勢中轉強）" if pr == -1 else "長紅K")
        if big and c[day] < o[day]:
            heavy = (vol is not None and not f3 and np.mean(vol[day - 5:day]) > 0
                     and vol[day] >= 1.3 * np.mean(vol[day - 5:day]))
            out.append("長黑K（漲多後帶量長黑，留意反轉）" if pr == 1 and heavy else "長黑K")
        if body <= 0.1 * rng and rng >= 0.5 * a1:
            out.append("十字線（多空拉鋸）")
        if pr == -1 and lower >= max(2 * body, 0.5 * rng) and rng >= 0.8 * a1:
            out.append("長下影線（下跌後出現下影，外觀描述）")
        if pr == 1 and upper >= max(2 * body, 0.5 * rng) and rng >= 0.8 * a1:
            out.append("長上影線（上漲後出現上影，外觀描述）")
    s2 = day - 1
    if not np.isnan(atr_prev[s2]):
        pr = prior(s2)
        body1, body2 = abs(c[s2] - o[s2]), abs(c[day] - o[day])
        if pr == -1 and c[s2] < o[s2] and c[day] > o[day] and o[day] <= c[s2] and c[day] >= o[s2] and body2 > body1:
            out.append("多頭吞噬")
        if pr == 1 and c[s2] > o[s2] and c[day] < o[day] and o[day] >= c[s2] and c[day] <= o[s2] and body2 > body1:
            out.append("空頭吞噬")
    s3 = day - 2
    a3 = atr_prev[s3]
    if not np.isnan(a3):
        pr = prior(s3)
        bars = [s3, s3 + 1, day]
        g = [geo(i) for i in bars]
        opens_in = all(min(o[i - 1], c[i - 1]) <= o[i] <= max(o[i - 1], c[i - 1]) for i in bars[1:])
        if (all(c[i] > o[i] for i in bars) and c[day] > c[s3 + 1] > c[s3] and opens_in and pr != 1
                and all(x[1] >= 0.3 * a3 and x[3] <= 0.3 * x[0] for x in g)):
            out.append("紅三兵")
        if (all(c[i] < o[i] for i in bars) and c[day] < c[s3 + 1] < c[s3] and opens_in and pr != -1
                and all(x[1] >= 0.3 * a3 and x[2] <= 0.3 * x[0] for x in g)):
            out.append("黑三兵")
        (r1, b1, _, _), (r2, b2, _, _) = g[0], g[1]
        small = b2 <= 0.3 * r2 and b2 <= 0.3 * b1
        long1 = b1 >= 1.2 * a3 and b1 >= 0.6 * r1
        mid = (o[s3] + c[s3]) / 2
        if pr == -1 and c[s3] < o[s3] and long1 and small and c[day] > o[day] and c[day] > mid:
            out.append("晨星類型")
        if pr == 1 and c[s3] > o[s3] and long1 and small and c[day] < o[day] and c[day] < mid:
            out.append("夜星類型")
    return out


# ---------------------------------------------------------------- 主函式
def structure_observation(df: pd.DataFrame, state: Dict, day: int, provisional: bool) -> Optional[Dict]:
    """Fixed fields for the selected formation; never infer states from prose."""
    f = state.get("event") or state.get("current")
    ended = state.get("ended")
    if not f:
        if not ended or ended.get("ended") != "crossed" or ended.get("end_day") != day - int(provisional):
            return None
        f = ended
        validity, position = "failed", "failed_down" if f["dir"] == 1 else "failed_up"
    else:
        up, dn = _at(f["upper"], day), _at(f["lower"], day)
        apex = _apex(f)
        if up <= dn or (apex is not None and day >= apex):
            return None
        close = float(df["Close"].iloc[day])
        margin = BREAK * f["ref"]
        validity = "active"
        if close > up + margin:
            position = "break_up"
        elif close < dn - margin:
            position = "break_down"
        elif dn <= close <= up:
            position = "returned_inside" if "bday" in f else "inside"
        else:
            position = "near_upper" if close > up else "near_lower"
    previous = float(df["Close"].iloc[day - 1]) if day else float(df["Close"].iloc[day])
    close = float(df["Close"].iloc[day])
    bday = f.get("bday")
    return {"kind": f["kind"], "state": position, "validity": validity,
            "formation_date": df.index[f["start"]].strftime("%Y-%m-%d"),
            "event_date": df.index[bday].strftime("%Y-%m-%d") if bday is not None else None,
            "event_age": day - bday if bday is not None else None,
            "event_direction": f.get("dir"),
            "is_provisional": provisional,
            "daily_direction": "up" if close > previous else "down" if close < previous else "flat"}


def trend_observation(tr: Optional[Dict], closes, atr_prev, day: int, provisional: bool) -> Optional[Dict]:
    if not tr:
        return None
    sign = 1 if tr["kind"] == "上升趨勢" else -1
    close, margin = closes[day], BREAK * atr_prev[day]
    last_pivot = tr["points"][-1]["price"]
    line = _at(tr["line_coefficients"], day) if tr.get("line_coefficients") else None
    status = ("broken" if (close - last_pivot) * sign < -margin else
              "line_crossed" if line is not None and (close - line) * sign < -margin else "intact")
    return {"kind": tr["kind"], "state": status, "is_provisional": provisional}


def levels_break(df: pd.DataFrame, piv: List[Dict], atr_prev, today: int) -> Dict[str, Any]:
    """§9a（09-30 使用者同意）：創新高／越過前高／越過區間高低點，只寫客觀事實、只給 AI。"""
    c = df["Close"].to_numpy(dtype=float)
    text, names, observations = [], [], []
    def record(kind, d, **fields):
        observations.append({"type": kind, "event_date": df.index[d].strftime("%Y-%m-%d"),
                             "event_age": today - d, **fields})
    ref = atr_prev[today]
    if np.isnan(ref) or ref <= 0:
        return {"text": text, "names": names, "observations": observations}
    # 1. 收盤創近 N 日新高／新低（只寫最長的 N）
    for n_days in HIGH_DAYS:
        if today >= n_days and c[today] > c[today - n_days:today].max():
            text.append(f"收盤創近 {n_days} 日新高"); names.append("創新高")
            record("new_high", today, lookback=n_days); break
        if today >= n_days and c[today] < c[today - n_days:today].min():
            text.append(f"收盤創近 {n_days} 日新低"); names.append("創新低")
            record("new_low", today, lookback=n_days); break
    top70 = c[max(0, today - HIGH_DAYS[0]):today].max() if today > 0 else None
    if top70 and not any("新高" in t for t in text) and c[today] >= top70 * 0.97:
        start = max(0, today - HIGH_DAYS[0])
        peak = start + int(np.argmax(c[start:today]))
        gap = (top70 / c[today] - 1) * 100
        if today - peak <= 5:     # 台股慣用語：5 日內創高後小幅回落＝創高拉回；更早高點只能描述位置，不能證明整理
            text.append(f"{_d(df.index[peak])} 收盤創近 {HIGH_DAYS[0]} 日新高 {_p(top70)} 後拉回，目前距高點約 {gap:.1f}%")
            names.append("創高拉回")
            record("high_pullback", peak, lookback=HIGH_DAYS[0])
        else:
            text.append(f"接近近 {HIGH_DAYS[0]} 日高點：收盤距 {_d(df.index[peak])} 最高收盤 {_p(top70)} 約 {gap:.1f}%")
            names.append("接近近期高點")
            record("near_high", today, lookback=HIGH_DAYS[0])
    # 2. 前高／前低：最近一個今天以前已確認的轉折
    for kind, sign, word in (("H", 1, "前高"), ("L", -1, "前低")):
        p = next((x for x in reversed(piv) if x["type"] == kind and x["confirm"] < today), None)
        if not p:
            continue
        line = p["price"] + sign * PREV_BREAK * atr_prev[p["confirm"]]
        beyond = [d for d in range(max(p["confirm"] + 1, today - BREAK_LOOKBACK + 1), today + 1)
                  if (c[d] - line) * sign > 0]
        fresh = beyond and (beyond[0] == p["confirm"] + 1 or (c[beyond[0] - 1] - line) * sign <= 0)   # 更早就越過＝舊事件不寫
        if fresh and all((c[d] - line) * sign > 0 for d in range(beyond[0], today + 1)):
            act = "越過" if sign > 0 else "跌破"
            k = today - beyond[0]
            text.append(f"{_d(df.index[beyond[0]])} 收盤{act} {_d(df.index[p['idx']])} {word} {_p(p['price'])}"
                        + (f"（{act}後第 {k + 1} 日）" if k else ""))
            names.append("突破前高" if sign > 0 else "跌破前低")
            record("pivot_break_up" if sign > 0 else "pivot_break_down", beyond[0])
        elif sign > 0 and 0 < (p["price"] - c[today]) / c[today] <= 0.1:
            text.append(f"距 {_d(df.index[p['idx']])} 前高 {_p(p['price'])} 約 {(p['price'] - c[today]) / c[today] * 100:.1f}%")
    # 3. 區間高低點事件：振幅有限不能證明橫盤，禁止命名為盤整／箱型。
    for d in range(max(1, today - BREAK_LOOKBACK + 1), today + 1):
        a = atr_prev[d]
        if np.isnan(a):
            continue
        for n_days in range(RANGE_MAX, RANGE_MIN - 1, -1):
            if d < n_days:
                continue
            w = c[d - n_days:d]
            top, bot = w.max(), w.min()
            a0 = atr_prev[d - n_days] if not np.isnan(atr_prev[d - n_days]) else a
            if top - bot > RANGE_ATR * min(a, a0):   # 用盤整開始時的 ATR：急漲會把 ATR 撐大、誤把上漲段當盤整（09-30 嘉晶）
                continue
            sign = 1 if c[d] > top + PREV_BREAK * a else -1 if c[d] < bot - PREV_BREAK * a else 0
            if sign and all((c[x] - (top if sign > 0 else bot)) * sign > 0 for x in range(d, today + 1)):
                k = today - d
                text.append(f"{_d(df.index[d])} 收盤{'越過' if sign > 0 else '跌破'}近 {n_days} 日區間{'高點' if sign > 0 else '低點'} "
                            f"{_p(bot)}～{_p(top)}" + (f"（脫離後第 {k + 1} 日）" if k else ""))
                names.append("越過區間高點" if sign > 0 else "跌破區間低點")
                record("range_break_up" if sign > 0 else "range_break_down", d, lookback=n_days,
                       upper=float(top), lower=float(bot))
                return {"text": text, "names": names, "observations": observations}
            break                                          # 最長的盤整窗口沒突破：不再找較短的
    return {"text": text, "names": names, "observations": observations}


# ---------------------------------------------------------------- §4b 三角（10-07 v2：形成截止日 F、逐日 ATR、獨立碰線事件、整組排序）
# 三角＝上下兩條趨勢線聚合，通常在尖端前表態。流程：
#   1) 形成截止日 F：還在形成＝昨天；近期突破 b＝F=b-1。候選線、碰線、轉折、品質都只用 F 時已知的資料（快照不因 F 後資料改變）。
#   2) 每根 K 的誤差用自己的 atr_prev[t]（已只含 t-1 以前）；斜率用起點 ATR、型態高度用共同起點 ATR。
#   3) 碰線＝獨立事件：碰線後要有收盤離線 ≥0.6 ATR 的分離證據，再碰才算下一次；同日碰上下緣不算交替。
#   4) 整組排序：兩側較少的主要轉折 → 主要轉折總數 → 兩側較少的碰線 → 碰線總數 → 反應 → 假突破少 → 貼線誤差小。
#   5) F 之後只掃事件（突破、回到型態內、反向突破），不回頭改形成期。
TRI_DAYS = int(_env("TRI_DAYS", 150))       # 往回找幾根
TRI_PIVOT = int(_env("TRI_PIVOT", 3))       # 局部轉折＝前後 3 日內最高／最低，idx+3 日才可用
TRI_TOUCH = _env("TRI_TOUCH", 0.4)          # 碰線：影線或實體距線 0.4 ATR（當日）內
TRI_SEP = _env("TRI_SEP", 0.6)              # 分離證據：收盤向內離線 0.6 ATR（當日）
TRI_OUT = _env("TRI_OUT", 0.15)             # 收盤穿出 0.15 ATR＝穿出（歷史與今日同一門檻）
TRI_OUT_MAX = _env("TRI_OUT_MAX", 1.0)      # 收盤穿出 >1 ATR＝線被破
TRI_POKE_MAX = _env("TRI_POKE_MAX", 2.0)    # 影線刺穿 >2 ATR＝線被破
TRI_BACK = int(_env("TRI_BACK", 2))         # 小幅穿出 2 天內收回＝假突破；第 3 天還在外＝線被破
TRI_BREAK_RECENT = int(_env("TRI_BREAK_RECENT", 5))
# 往回重建最近 5 天內的突破（逐日重播窗口；已知限制：窗口首日沒有更早的固定紀錄可帶，試過暖機 10 日反而更糟，已撤回）
TRI_REACT = _env("TRI_REACT", 1.0)          # 碰線後 5 日內離線 1 ATR＝有反應
TRI_RECENT = int(_env("TRI_RECENT", 25))    # 兩線在 F 前 25 日內都要有碰線事件
TRI_TIP = _env("TRI_TIP", 0.15)             # F 時寬度 <15%：標「接近尖端」
TRI_TIP_BREAK = _env("TRI_TIP_BREAK", 0.08)  # F 時寬度 <8%：已收到尖端＝不成立（同一政策，不分突破前後）
TRI_NARROW = _env("TRI_NARROW", 0.65)       # F 時寬度 ≤ 共同起點的 65%＝收斂
TRI_FLAT = _env("TRI_FLAT", 1.0)
TRI_MAJOR_POKE = _env("TRI_MAJOR_POKE", 0.4)   # 主要轉折實體刺穿 >0.4 ATR＝線作廢；影線刺穿 >0.4 ATR＝扣分
TRI_MIN_HEIGHT = _env("TRI_MIN_HEIGHT", 3.0)   # 共同起點寬度 ≥3 ATR（共同起點日 ATR）


def _tri_turns(P, sg: int, lo: int, last: int) -> List[int]:
    """局部轉折：前後 TRI_PIVOT 日內最高（sg=1）／最低（sg=-1）；右邊 TRI_PIVOT 天確認，只回傳到 last 已可用者。"""
    w = TRI_PIVOT
    key = (id(P), sg, len(P))
    cache = getattr(_TRI_LOCAL, "turns", _TURN_CACHE)
    full = cache.get(key) if cache.get("on") else None
    if full is None:
        full = [t for t in range(0, len(P) - w) if P[t] * sg >= (P[max(0, t - w):t + w + 1] * sg).max()]
        if cache.get("on"):
            cache[key] = full
    return [t for t in full if lo <= t <= last - w]


_TRI_MEMO = {}
_TRI_MEMO_LOCK = __import__("threading").RLock()
_TRI_LOCAL = __import__("threading").local()
_TURN_CACHE: Dict[Any, Any] = {}               # 只在 user_triangle 呼叫期間啟用，結束清空（避免 id 重用）


def _tri_line(P, B, Q, C, A, s: float, k: float, i0: int, last: int, sg: int, ref: Optional[float] = None,
              majors=(), src_major: Optional[int] = None, confirm: bool = True) -> Optional[Dict[str, Any]]:
    """驗證一條線在形成截止日 F（＝last）的品質；A＝逐日 ATR。sg=1 上緣（P=高、B=實體頂、Q=低），-1 下緣。
    majors＝[(idx, confirm)] 或 [idx]；只用 confirm ≤ F 的。None＝線被破或品質不足（含原因不回傳，呼叫端只需要合格線）。"""
    F = last
    line = lambda t: s * t + k
    if np.isnan(A[i0:F + 1]).any():
        return None                               # ATR 不足不硬判（不用未來或中位數補值）
    fake_days: List[int] = []
    touches: List[int] = []                       # 每根實際碰線 K 棒都保留
    run = 0
    for t in range(i0, F + 1):
        a, y = A[t], line(t)
        if (P[t] - y) * sg > TRI_POKE_MAX * a:
            return None                           # 影線刺穿太深
        out = (C[t] - y) * sg
        if out > TRI_OUT * a:
            run += 1
            if out > TRI_OUT_MAX * a or run > TRI_BACK:
                return None                       # 真突破：線在 F 前已被破
            fake_days.append(t)
        else:
            run = 0
        if out > TRI_OUT * a or ((P[t] - y) * sg >= -TRI_TOUCH * a and (B[t] - y) * sg <= TRI_TOUCH * a):
            touches.append(t)
    # 獨立回測波段（v5）：只用已確認的接觸（t+TRI_PIVOT ≤ F）建立正式事件；未確認接觸另存 pending，不充數。
    # 新事件 b：事件內任一較早接觸 a 與 b 之間，要有已確認的反向轉折 v（Q 的局部轉折、v+TRI_PIVOT ≤ F），
    # 且 g(v)−g(a)、g(v)−g(b) ≥ TRI_REACT×A[v]（g＝收盤向內離線距離）；否則同一波段。
    # confirm=False（三角候選）：未確認接觸也納入、v 不要求是已確認轉折，只用來標「候選」。
    inside = (s * np.arange(len(C)) + k - C) * sg
    g = lambda t: inside[t]
    conf = [t for t in touches if not confirm or t + TRI_PIVOT <= F]
    pending = [t for t in touches if t + TRI_PIVOT > F]          # 依確認期限獨立計算（候選模式也一樣）
    if run or len(conf) < 2 or F - touches[-1] > TRI_RECENT:
        return None
    confirmed_turns = set(_tri_turns(Q, -sg, i0, F))
    turns_v = sorted(confirmed_turns) if confirm else None
    reasons: set = set()
    events: List[Dict[str, Any]] = []
    for t in conf:
        if events:
            days = events[-1]["days"]
            candidates = turns_v if confirm else range(days[0] + 1, t)
            # Advance through sorted contact days once; no repeated all-contact scan.
            cursor, minimum = 0, float("inf")
            sep = None
            for v in candidates:
                if v <= days[0] or v >= t or v + (TRI_PIVOT if confirm else 0) > F:
                    continue
                while cursor < len(days) and days[cursor] < v:
                    minimum = min(minimum, inside[days[cursor]])
                    cursor += 1
                if cursor and g(v) - g(t) >= TRI_REACT * A[v] and g(v) - minimum >= TRI_REACT * A[v]:
                    sep = v
                    break
            if sep is None:
                days.append(t)
                events[-1]["fake"] = events[-1]["fake"] or t in fake_days
                continue
            if not confirm and sep not in confirmed_turns:
                reasons.add("反向轉折未確認")
        else:
            sep = None
        if not confirm and t in pending:
            reasons.add("接觸尚待確認")
        events.append({"rep": t, "days": [t], "sep": sep, "react": None, "fake": t in fake_days})
    if run:
        return None                               # F 當天收在線外：F 不是形成期
    if len(events) < 2 or F - touches[-1] > TRI_RECENT:
        return None
    fakes = sum(1 for i, t in enumerate(fake_days) if i == 0 or t - fake_days[i - 1] > 1)
    if fakes > 2:
        return None                               # 假突破最多 2 段
    reacted = 0
    for ev in events:
        e = ev["days"][-1]
        for n in range(e + 1, min(e + 6, F + 1)):
            if (line(n) - Q[n]) * sg >= TRI_REACT * A[n]:
                ev["react"] = n
                reacted += 1
                break
    if reacted < 1:
        return None
    mj = [(m if not isinstance(m, tuple) else m) for m in majors]
    mj = [(m[0], m[1]) if isinstance(m, tuple) else (m, m) for m in mj]
    mj = [m for m, c in mj if c <= F and i0 - 3 <= m <= F]
    fake_set = set(fake_days)
    for m in mj:
        if m > i0 + 3 and m not in fake_set and (B[m] - line(m)) * sg > TRI_MAJOR_POKE * A[m]:
            return None                           # 主要轉折實體刺穿線＝線畫錯（假突破日除外）
    contact = {d: ev for ev in events for d in ev["days"]}
    near = lambda m: m in contact and (P[m] - line(m)) * sg >= -TRI_TOUCH * A[m] and (B[m] - line(m)) * sg <= TRI_TOUCH * A[m]
    touched = {m for m in mj if near(m)}
    borrowed = 0
    if src_major is not None and src_major in mj and events and events[0]["rep"] == i0 and src_major not in touched:
        if TRI_SPLIT_BORROWED:
            borrowed = 1                          # 10-08：借用來源本身沒貼線，另計、不算實際貼線支持
        else:
            touched.add(src_major)               # 起點借用的主要轉折＝第一事件的來源（只加這一個）
    pierced = {m for m in mj if m > i0 + 3 and m not in fake_set and (P[m] - line(m)) * sg > TRI_MAJOR_POKE * A[m]}
    turns = set(_tri_turns(P, sg, i0, F))
    zone = lambda d: max(0.0, (line(d) - P[d]) * sg, (B[d] - line(d)) * sg) / A[d]   # 線落在影線區間內＝0
    err = float(np.mean([min(zone(d) for d in ev["days"]) for ev in events]))
    return {"s": float(s), "k": float(k), "i0": int(i0), "a": [ev["rep"] for ev in events], "events": events,
            "major": len(touched) - len(pierced), "mtouch": len(touched), "borrowed": borrowed, "reacted": reacted, "fakes": fakes,
            "peaks": sum(1 for ev in events if turns.intersection(ev["days"])), "err": err,
            "turn_events": sum(1 for ev in events if (turns | set(mj)).intersection(ev["days"])),
            "density": len(contact), "score": reacted, "brk": None, "R_line": float(A[i0]), "pending": pending,
            "reasons": sorted(reasons)}


def _tri_lines(P, B, Q, C, A, lo: int, F: int, sg: int, majors: List[Tuple[int, int]],
               confirm: bool = True) -> List[Dict[str, Any]]:
    """F 時已可用的轉折點兩兩連線；起點可在主要轉折前後 3 根（可用日＝該主要轉折確認日）。"""
    line_cache = {}
    mj = [(m, c) for m, c in majors if c <= F and lo <= m]
    anchors = {t: t + TRI_PIVOT for t in _tri_turns(P, sg, lo, F)}
    for m, c in mj:
        __import__("request_runtime").check_budget()
        anchors[m] = min(anchors.get(m, c), c)
    pts = [(t, float(v), "影線" if v == P[t] else "實體" if v == B[t] else "實體內")
           for t in sorted(anchors) for v in {P[t], B[t], B[t] - sg * TRI_TOUCH * A[t]}]
    near_m = {t: m for m, c in mj for t in range(m - 3, m + 4) if lo <= t <= F and t not in anchors}
    starts = sorted(pts + [(t, float(v), "近轉折") for t in near_m for v in {P[t], B[t]}])
    out = []
    from request_runtime import check_budget
    for x1, y1, src1 in starts:
        check_budget()
        R = A[x1]
        for x2, y2, src2 in pts:
            if x2 - x1 < 3:
                continue
            s = (y2 - y1) / (x2 - x1)
            if s * sg > 0.3 * R / 60 or abs(s) > (0.12 if sg > 0 else 0.2) * R:
                continue                          # 斜率尺度＝起點 ATR
            key = (s, y1 - s * x1, x1, near_m.get(x1))
            if key not in line_cache:
                line_cache[key] = _tri_line(P, B, Q, C, A, *key[:3], F, sg, majors=mj, src_major=key[3], confirm=confirm)
            r = dict(line_cache[key]) if line_cache[key] is not None else None
            if r:
                r.update(src=(x1, src1, x2, src2))
                out.append(r)
    out.sort(key=(lambda r: (-r["major"], -r["borrowed"], -len(r["a"]), -r["peaks"], -r["reacted"], r["err"], r["src"])) if TRI_SPLIT_BORROWED
             else (lambda r: (-r["major"], -len(r["a"]), -r["peaks"], -r["reacted"], r["err"], r["src"])))
    uniq: List[Dict[str, Any]] = []
    for r in out:
        def same(u):
            x0 = max(r["i0"], u["i0"])
            g = lambda x: abs((r["s"] - u["s"]) * x + r["k"] - u["k"])
            return max(g(x0), g(F)) <= 0.2 * A[x0]   # 共同期間兩端都接近（共同起點 ATR）才算同一條
        if not any(same(u) for u in uniq):
            uniq.append(r)
        if len(uniq) >= 30:
            break
    # 分層：主要轉折與最高差 1 以內，層內碰線與最多者差 1 以內
    layer = [x for x in uniq if x["major"] >= uniq[0]["major"] - 1] if uniq else []
    top = max((len(x["a"]) for x in layer), default=0)
    return [x for x in layer if len(x["a"]) >= top - 1]


def _tri_sequence(u, d) -> List[Tuple[int, str]]:
    """上下緣共用的「順序明確接觸序列」：每個事件取第一個不是同日雙側的接觸日；整個事件都不明才略過。
    交替次數與初期高度都用這份序列。"""
    ud = {t for ev in u["events"] for t in ev["days"]}
    dd = {t for ev in d["events"] for t in ev["days"]}
    both = ud & dd
    seq = []
    for side, ln in (("U", u), ("D", d)):
        for ev in ln["events"]:
            clear = [t for t in ev["days"] if t not in both]
            if clear:
                seq.append((clear[0], side))
    return sorted(seq)


def _tri_alternations(u, d) -> int:
    seq = [x for _, x in _tri_sequence(u, d)]
    return sum(1 for x, y in zip(seq, seq[1:]) if x != y)


def _tri_height(H, L, O, C, A, u, d) -> Optional[Dict[str, Any]]:
    """初期接觸高度：_tri_sequence 最早 4 個交替接觸（U-D-U-D 或 D-U-D-U）。
    接觸價＝線值夾在 [實體頂, High]（上緣）／[Low, 實體底]（下緣）；H＝最高上緣接觸價−最低下緣接觸價；
    R_height＝四接觸首日至末日逐日 ATR 中位數。"""
    from request_runtime import check_budget
    check_budget()
    top, bot = np.maximum(O, C), np.minimum(O, C)
    pick: List[Tuple[int, str]] = []
    for t, side in _tri_sequence(u, d):
        if not pick or pick[-1][1] != side:
            pick.append((t, side))
        if len(pick) == 4:
            break
    if len(pick) < 4:
        return None
    price = lambda t, side: (min(max(u["s"] * t + u["k"], top[t]), H[t]) if side == "U"
                             else max(min(d["s"] * t + d["k"], bot[t]), L[t]))
    hi = max(price(t, x) for t, x in pick if x == "U")
    lo_ = min(price(t, x) for t, x in pick if x == "D")
    return {"H": float(hi - lo_), "R": float(np.nanmedian(A[pick[0][0]:pick[-1][0] + 1])),
            "contacts": [(int(t), x) for t, x in pick]}


def _tri_form(H, L, O, C, A, lo: int, F: int, piv, confirm: bool = True, local: bool = False) -> Optional[Dict[str, Any]]:
    """形成截止日 F 的三角快照（只用 F 時已知資料）。confirm=False＝三角候選（未確認接觸也算）。
    local=True＝補充候選：主要轉折支持不足時，改要求每側 ≥2 個含已確認局部／主要轉折的接觸事件、且至少 1 個主要轉折。"""
    from request_runtime import check_budget
    check_budget()
    top, bot = np.maximum(O, C), np.minimum(O, C)
    mh = [(p["idx"], p["confirm"]) for p in piv if p["type"] == "H"]
    ml = [(p["idx"], p["confirm"]) for p in piv if p["type"] == "L"]
    required = 1 if local else 2
    if min(sum(lo <= i <= F and cf <= F for i, cf in side) for side in (mh, ml)) < required:
        return None
    ups = _tri_lines(H, top, L, C, A, lo, F, 1, mh, confirm)
    dns = _tri_lines(L, bot, H, C, A, lo, F, -1, ml, confirm) if ups else []
    best = None
    for u in ups:
        for d in dns:
            support = (min(u["turn_events"], d["turn_events"]) >= 2 and min(u["mtouch"], d["mtouch"]) >= 1) if local \
                else min(u["mtouch"], d["mtouch"]) >= 2
            if not support or len(u["a"]) + len(d["a"]) < 5:
                continue                          # 每側 2 個主要轉折、合計 5 次碰線事件
            start = min(u["a"][0], d["a"][0])     # formation_start：兩側有效首接觸較早者
            joint = max(u["i0"], d["i0"])         # joint_start：兩線都開始的共同幾何起點
            if F - start < 15:
                continue
            hgt = _tri_height(H, L, O, C, A, u, d)
            if hgt is None or hgt["H"] < TRI_MIN_HEIGHT * hgt["R"]:
                continue                          # 初期接觸高度 H ≥ 3×R_height
            w0 = (u["s"] - d["s"]) * joint + u["k"] - d["k"]
            wF = (u["s"] - d["s"]) * F + u["k"] - d["k"]
            if wF <= 0 or w0 <= 0 or wF > TRI_NARROW * w0 or wF < TRI_TIP_BREAK * w0:
                continue                          # 共同開口 w0 只管收斂、尖端、交叉
            if _tri_alternations(u, d) < 3:
                continue
            n = len(u["a"]) + len(d["a"])
            key = (min(u["major"], d["major"]), u["major"] + d["major"], min(len(u["a"]), len(d["a"])), n,
                   u["reacted"] + d["reacted"], -(u["fakes"] + d["fakes"]), -round(u["err"] + d["err"], 3),
                   tuple(-x if isinstance(x, int) else 0 for x in u["src"] + d["src"]))
            if best is None or key > best[0]:
                best = (key, u, d, start, joint, w0, wF, hgt)
    if not best:
        return None
    _, u, d, start, joint, w0, wF, hgt = best
    return {"u": u, "d": d, "F": F, "start": start, "joint": joint, "R_geom": float(A[joint]), "w0": w0, "wF": wF,
            "height": hgt, "near_tip": wF < TRI_TIP * w0, "joint_days": F - joint, "candidate": not confirm or local,
            "reasons": (["主要轉折支持不足，局部轉折支持"] if local else [])
            + (sorted(set(u.get("reasons", [])) | set(d.get("reasons", []))) if not confirm else [])}


def _tri_kind(snap) -> str:
    u, d, F = snap["u"], snap["d"], snap["F"]
    level = lambda l: abs(l["s"]) * (F - l["i0"]) <= 0.5 * snap["R_geom"]
    return "箱型整理" if level(u) and level(d) else "上升三角" if level(u) else "下降三角" if level(d) else "三角收斂"


def _tri_track(df, C, A, snap, end: int) -> Dict[str, Any]:
    """沿形成快照的固定上下線，逐日掃 F+1～end 的事件（門檻 TRI_OUT×當日 ATR）；交會後停止判位。"""
    u, d = snap["u"], snap["d"]
    up_at = lambda t: u["s"] * t + u["k"]
    dn_at = lambda t: d["s"] * t + d["k"]
    history: List[Dict[str, Any]] = []
    pos, crossed = "inside", False
    for t in range(snap["F"] + 1, end + 1):
        if up_at(t) <= dn_at(t):
            crossed = True
            break
        o = TRI_OUT * A[t]
        strong = lambda dist: "強" if dist > TRI_OUT_MAX * A[t] else ""
        if C[t] > up_at(t) + o and pos != "above":
            history.append({"type": "break_up", "date": _d(df.index[t]), "idx": t, "strength": strong(C[t] - up_at(t))})
            pos = "above"
        elif C[t] < dn_at(t) - o and pos != "below":
            history.append({"type": "break_down", "date": _d(df.index[t]), "idx": t, "strength": strong(dn_at(t) - C[t])})
            pos = "below"
        elif dn_at(t) <= C[t] <= up_at(t) and pos != "inside":
            history.append({"type": "returned_inside", "date": _d(df.index[t]), "idx": t})
            pos = "inside"
    breaks = [h for h in history if h["type"] != "returned_inside"]
    for h in breaks:
        # 10-08 兩日確認：首次越線後（到下一個事件前），連續兩根完整收盤 K 同方向都超出 TRI_OUT×當日 ATR 才確認；
        # 中間一天幅度不足就重新計數（100.2、100.1、100.2、100.2 → 第 4 天確認）；過交會點不再確認
        nxt = next((x["idx"] for x in history if x["idx"] > h["idx"]), end + 1)
        run, h["confirm"] = 0, "越線、尚未確認"
        for t in range(h["idx"], min(nxt, end + 1)):
            if up_at(t) <= dn_at(t):
                h["confirm"] = "已過交會點，不確認"
                break
            beyond = (C[t] > up_at(t) + TRI_OUT * A[t]) if h["type"] == "break_up" else (C[t] < dn_at(t) - TRI_OUT * A[t])
            from request_runtime import consecutive_sessions
            adjacent = t == h["idx"] or consecutive_sessions(df.index[t - 1], df.index[t], df.attrs.get("trading_sessions"))
            run = (run + 1 if adjacent else 1) if beyond else 0
            if run >= 2:
                h["confirm"], h["confirmed_date"] = "確認", _d(df.index[t])
                break
        else:
            if nxt <= end:
                h["confirm"] = "越線後收回，未確認"
            elif run == 1 and h["idx"] == end:
                h["confirm"] = "待下一日收盤"
    return {"history": history, "pos": pos, "crossed": crossed, "breaks": breaks,
            "reversed": any(a["type"] != b["type"] for a, b in zip(breaks, breaks[1:]))}


TRI_STATE_VERSION = "tri-v9"   # 10-08：指紋含 ZIGZAG_ATR 等全部參數、窗口外紀錄不載入、兩日確認欄位
TRI_INIT_REPLAY = int(_env("TRI_INIT_REPLAY", 20))   # 沒有狀態快照時，從固定起點（今天往回 20 日）重播建立
TRI_TRACK_DAYS = int(_env("TRI_TRACK_DAYS", 20))     # 已突破型態沿固定線追蹤最多 20 日（或交會）就結束
TRI_CONFIRM2 = int(_env("TRI_CONFIRM2", 0))          # 10-08：1＝文字顯示兩日確認（比較驗證前預設關；欄位一律計算）
TRI_SPLIT_BORROWED = int(_env("TRI_SPLIT_BORROWED", 0))   # 10-08：1＝借用旁邊 K 棒的主要轉折不算實際貼線支持（另計排序）


def _tri_params_sig() -> str:
    # 10-08：原本只收 TRI_*，改 ZIGZAG_ATR 等參數後仍沿用舊快照；改成所有大寫數值參數
    return "|".join(f"{k}={v}" for k, v in sorted(globals().items())
                    if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool))


def _shift_snap(snap: Dict[str, Any], delta: int) -> Dict[str, Any]:
    """快照所有 K 棒序號平移 delta（新序號＝舊＋delta），線截距同步換算；用來跨資料窗口保存／載入。"""
    sh = lambda x: None if x is None else int(x) + delta
    out = dict(snap)
    for side in ("u", "d"):
        ln = dict(snap[side])
        ln["k"] = float(ln["k"]) - float(ln["s"]) * delta
        ln["i0"] = sh(ln["i0"])
        ln["a"] = [sh(x) for x in ln["a"]]
        ln["events"] = [dict(e, rep=sh(e["rep"]), days=[sh(x) for x in e["days"]], sep=sh(e.get("sep")),
                             react=sh(e.get("react"))) for e in ln["events"]]
        ln["pending"] = [sh(x) for x in ln.get("pending", [])]
        ln["src"] = tuple(sh(x) if isinstance(x, (int, np.integer)) else x for x in ln.get("src", ()))
        out[side] = ln
    for key in ("F", "start", "joint"):
        out[key] = sh(snap[key])
    out["height"] = dict(snap["height"], contacts=[(sh(t), x) for t, x in snap["height"]["contacts"]])
    return out


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def _tri_status(tr: Dict[str, Any]) -> str:
    """一組型態事件的最新狀態（白話）。"""
    if not tr["breaks"]:
        return "仍在型態內"
    b0, h = tr["breaks"][0], tr["history"][-1]
    if tr["crossed"]:
        return "已過交會點"
    if h["type"] == "returned_inside":
        return "後回內待確認"
    if tr["reversed"]:
        return f"{h['date']} 反向{'突破' if h['type'] == 'break_up' else '跌破'}"
    return "仍在線外" if h is b0 else f"{h['date']} 再次同向"


def user_triangle(df: pd.DataFrame, atr_prev, end: int, state: Optional[Dict[str, Any]] = None,
                  out_state: Optional[Dict[str, Any]] = None, replay_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """end＝今天。單日選擇流程依日期執行，正式突破紀錄以「狀態快照」跨日保存：
      state＝前次保存的快照（同規則版本、價格指紋相符才用），從它的截至日隔天補跑；沒有就從固定起點重播 TRI_INIT_REPLAY 日。
      out_state（dict）會被填入新的快照（呼叫端決定是否寫入；盤中不寫）。
    X 日：型態＝F=X−1 的正式快照（搜尋起點 lo_F＝max(first_valid, F−TRI_DAYS)），不成立看三角候選；
          與已固定紀錄當日判定同型態就沿用固定線；正式型態、尚無紀錄、X 日收盤穿出 0.15×A[X] → 建立紀錄。
    目前型態＝今天形成的正式／候選型態（同型態沿用固定線）；沒有時才用保留期限內、仍在追蹤的已突破型態。"""
    _TRI_LOCAL.turns = {}
    _TRI_LOCAL.turns["on"] = True
    try:
        from request_runtime import check_budget
        check_budget()
        import copy, hashlib, json
        raw = pd.util.hash_pandas_object(df[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in df]], index=True).values.tobytes()
        key = hashlib.sha256(raw + np.asarray(atr_prev, dtype=float).tobytes() + json.dumps(
            [end, {k: state.get(k) for k in ("sig", "as_of", "fingerprint", "records")} if isinstance(state, dict) else state, replay_days, _tri_params_sig(), TRI_STATE_VERSION, df.attrs.get("trading_sessions")], sort_keys=True, default=str).encode()).digest()
        cacheable = getattr(_tri_form, "__module__", "") == __name__
        with _TRI_MEMO_LOCK:
            cached = _TRI_MEMO.get(key) if cacheable else None
        if cached is not None:
            result, snapshot = copy.deepcopy(cached)
        else:
            snapshot = {}
            result = _user_triangle(df, atr_prev, end, state, snapshot, replay_days)
            if cacheable:
                with _TRI_MEMO_LOCK:
                    if len(_TRI_MEMO) >= 128:
                        _TRI_MEMO.pop(next(iter(_TRI_MEMO)))
                    _TRI_MEMO[key] = copy.deepcopy((result, snapshot))
        if out_state is not None:
            out_state.clear()
            out_state.update(snapshot)
        return result
    finally:
        _TRI_LOCAL.turns = {}


def _user_triangle(df, atr_prev, end, state=None, out_state=None, replay_days=None):
    H, L, O, C = (df[k].to_numpy(dtype=float) for k in ("High", "Low", "Open", "Close"))
    A = np.asarray(atr_prev, dtype=float)
    last = end - 1
    valid = np.where(~np.isnan(A))[0]
    if not len(valid) or np.isnan(A[end]):
        return None                               # ATR 資料不足
    first_valid = int(valid[0])
    dates = [ts.strftime("%Y-%m-%d") for ts in df.index]
    pos = {d: i for i, d in enumerate(dates)}
    forms: Dict[Tuple[int, bool], Any] = {}

    def form(F: int, confirm: bool = True, local: bool = False):   # 每個截止日只算一次；搜尋起點依各自截止日計算
        if (F, confirm, local) not in forms:
            lo_F = max(first_valid, F - TRI_DAYS)
            if F - lo_F < 30:
                forms[(F, confirm, local)] = None
            else:
                piv = [p for p in zigzag(H[:F + 1], L[:F + 1], C[:F + 1], A[:F + 1]) if p["confirm"] <= F]
                forms[(F, confirm, local)] = _tri_form(H, L, O, C, A, lo_F, F, piv, confirm, local)
        return forms[(F, confirm, local)]

    def same_shape(x, y, X: int) -> bool:         # 當日判定：共同有效期間起點與 X 日，上下緣都在 0.5 ATR 內
        t0 = max(x["u"]["i0"], x["d"]["i0"], y["u"]["i0"], y["d"]["i0"])
        return all(abs((x[e]["s"] - y[e]["s"]) * t + x[e]["k"] - y[e]["k"]) <= 0.5 * A[t]
                   for e in ("u", "d") for t in (t0, X))

    # 載入狀態快照：規則版本、參數、價格指紋（截至日前 30 根收盤）都要相符
    sig = TRI_STATE_VERSION + "|" + _tri_params_sig()
    bar_hash = lambda i: _bar_hash(O[i], H[i], L[i], C[i])
    records: List[Dict[str, Any]] = []
    start_X = max(first_valid + 31, end - (TRI_INIT_REPLAY if replay_days is None else max(0, int(replay_days))))   # 族群總覽等大量呼叫可設 0（只看今天）
    reconstructed = True                          # 沒有有效快照：從固定起點重建，紀錄標「回溯辨識」
    held_records = []
    window_dropped = False                        # 有紀錄因資料窗口較短無法載入：這次只顯示、不寫回（避免把仍有效的紀錄洗掉）
    if state and state.get("sig") == sig and state.get("as_of") in pos and pos[state["as_of"]] <= end:
        a = pos[state["as_of"]]
        fp = state.get("fingerprint") or {}
        mine = [d for d in dates[:a + 1] if d >= min(fp, default="9")]
        if fp and mine == [d for d in sorted(fp) if d >= dates[0]] and all(bar_hash(pos[d]) == fp[d] for d in mine):
            historical_dates = sorted(set(fp) | set(dates))
            origin = historical_dates.index(dates[0])
            virtual_pos = {d: i - origin for i, d in enumerate(historical_dates)}
            for r in state.get("records", []):
                if r["F_date"] in virtual_pos and r["recognized"] in virtual_pos:
                    snap = _shift_snap(r["snap"], virtual_pos[r["F_date"]])
                    if min([snap["start"], snap["joint"], snap["u"]["i0"], snap["d"]["i0"]] + snap["u"]["a"] + snap["d"]["a"]) < 0:
                        window_dropped = True
                        held_records.append(r)
                        continue                  # 10-08：形成期間超出目前資料窗口（不代表紀錄失效）
                    records.append({"snap": snap, "recognized": pos[r["recognized"]],
                                    "reconstructed_at": r.get("reconstructed_at"), "detected_on": r.get("detected_on")})
            start_X, reconstructed = a + 1, False
    run_day = dates[end]

    def expired(r, X):                            # 1) 追蹤結束：辨識後超過 TRI_TRACK_DAYS，或兩線在 X 日前已交會
        u, d = r["snap"]["u"], r["snap"]["d"]
        return X - r["recognized"] > TRI_TRACK_DAYS or (u["s"] - d["s"]) * X + u["k"] - d["k"] <= 0
    def held_snap(r):
        return {"snap": _shift_snap(r["snap"], virtual_pos[r["F_date"]]),
                "recognized": virtual_pos[r["recognized"]]}
    for X in range(start_X, end + 1):
        from request_runtime import check_budget
        check_budget()
        held_records = [r for r in held_records if not expired(held_snap(r), X)]
        records = [r for r in records if not expired(r, X)]   # 每天先清過期紀錄，再判斷新突破（逐日＝一次補算）
        F = X - 1
        pick = form(F)                            # 10-08 提速：候選／補充候選必為 candidate、不建紀錄，歷史日不必搜尋（結果相同）
        if pick is None or pick.get("candidate"):
            continue
        if any(same_shape(r["snap"], pick, X) for r in records) or any(same_shape(held_snap(r)["snap"], pick, X) for r in held_records):
            continue
        u, d = pick["u"], pick["d"]
        o = TRI_OUT * A[X]
        if C[X] > u["s"] * X + u["k"] + o or C[X] < d["s"] * X + d["k"] - o:
            records.append({"snap": pick, "recognized": X,      # 當天主流程真的發出突破，才建立正式紀錄
                            "reconstructed_at": run_day if reconstructed else None,
                            "detected_on": None if reconstructed else run_day})
    records = [r for r in records if not expired(r, end)]
    held_records = [r for r in held_records if not expired(held_snap(r), end)]
    # 追蹤結束：超過 TRI_TRACK_DAYS 或已過交會點
    tracks = {id(r): _tri_track(df, C, A, r["snap"], end) for r in records}
    if out_state is not None:
        out_state.clear()
    if out_state is not None:
        out_state.update(_jsonable({
            "sig": sig, "as_of": dates[end],
            "fingerprint": {dates[i]: bar_hash(i) for i in range(0, end + 1)},
            "records": [{"F_date": dates[r["snap"]["F"]], "recognized": dates[r["recognized"]],
                         "reconstructed_at": r.get("reconstructed_at"), "detected_on": r.get("detected_on"),
                         "snap": _shift_snap(r["snap"], -r["snap"]["F"])} for r in records] + held_records}))
    keep_from = end - TRI_BREAK_RECENT
    live = list(records)                          # 迴圈已清掉過期（>TRI_TRACK_DAYS 或交會）＝全部仍在追蹤
    pick = form(last) or form(last, False) or (None if live or held_records else form(last, True, True))   # 補充候選不和仍在追蹤的正式突破競爭
    own = next((r for r in records if pick is not None and same_shape(r["snap"], pick, end)), None)
    if own is not None:
        main = own["snap"]                        # 今天的型態就是先前已突破的那組：沿用固定線與首次日期
    elif pick is not None:
        main = pick
    else:                                         # 今天沒有形成型態：只用保留期限內、仍在追蹤的已突破型態
        recent = [r for r in records if r["recognized"] >= keep_from]   # 當主型態只用保留期限內辨識的（舊紀錄不因存在就當成目前成立）
        own = recent[-1] if recent else None
        main = own["snap"] if own else None
    if main is None:
        return None
    tr = tracks.get(id(own)) if own is not None else _tri_track(df, C, A, main, end)
    u, d, F = main["u"], main["d"], main["F"]
    kind = _tri_kind(main)
    up, dn = u["s"] * end + u["k"], d["s"] * end + d["k"]
    breaks, posn = tr["breaks"], tr["pos"]
    cand = bool(main.get("candidate"))
    if cand:                                      # 候選：只描述價格相對候選線的位置，不產生正式突破
        state_txt = {"above": "價格位於候選上緣之上", "below": "價格位於候選下緣之下"}.get(posn, "位於候選線之間")
        if tr["crossed"]:
            state_txt = "已過候選線交會點"
        breaks, compat = [], "inside"
    elif not breaks:
        state_txt, compat = ("位於型態內" + ("（接近尖端）" if main["near_tip"] else "")), "inside"
    else:
        last_b = breaks[-1]
        act = "收盤向上突破上緣" if last_b["type"] == "break_up" else "收盤向下跌破下緣"
        state_txt = act if last_b["idx"] == end else f"{last_b['date']} {act}"
        if TRI_CONFIRM2 and last_b.get("confirm"):
            state_txt += f"（{last_b['confirmed_date']} 兩日確認）" if last_b["confirm"] == "確認" else f"（{last_b['confirm']}）"
        if tr["reversed"]:
            state_txt = f"{breaks[0]['date']} {'突破' if breaks[0]['type'] == 'break_up' else '跌破'}後反向，" + state_txt
        elif len(breaks) > 1:
            state_txt = f"{breaks[0]['date']} 首次{'突破' if breaks[0]['type'] == 'break_up' else '跌破'}，" + state_txt + "（再次同向）"
        if posn == "inside":
            state_txt += "，今日回到型態內（待確認）"
        compat = last_b["type"]
    if tr["crossed"] and not cand:
        state_txt += "（已過上下緣交會點，位置不適用）"
    rec_out = []
    for r in records:                             # 顯示：保留期限內辨識的紀錄＋目前型態本身的紀錄
        if r["recognized"] < keep_from and r is not own:
            continue
        rt = tracks[id(r)]
        b0 = rt["breaks"][0] if rt["breaks"] else None
        rec_out.append({"kind": _tri_kind(r["snap"]), "formation_cutoff": _d(df.index[r["snap"]["F"]]),
                        "recognized": _d(df.index[r["recognized"]]), "is_current": r is own,
                        "event_date": _d(df.index[r["recognized"]]), "reconstructed_at": r.get("reconstructed_at"),
                        "detected_on": r.get("detected_on"),
                        "retrospective": bool(r.get("reconstructed_at")),   # 回溯辨識：不是當天實際發出的訊號
                        "first_break": {k: v for k, v in b0.items() if k != "idx"} if b0 else None,
                        "history": [{k: v for k, v in h.items() if k != "idx"} for h in rt["history"]],
                        "status": _tri_status(rt), "failed": rt["reversed"],
                        "upper": (r["snap"]["u"]["s"], r["snap"]["u"]["k"]), "lower": (r["snap"]["d"]["s"], r["snap"]["d"]["k"]),
                        "_last": (rt["history"][-1]["idx"] if rt["history"] else r["recognized"])})
    others = sorted([x for x in rec_out if not x["is_current"]], key=lambda x: x["_last"])
    if others:                                    # 圖卡只留最近一組歷史型態的最新狀態，其餘見明細
        o = others[-1]
        fb = o["first_break"]
        state_txt += (f"；歷史另一組{o['kind']}：{fb['date']} {'上破' if fb['type'] == 'break_up' else '下破'}，{o['status']}"
                      + (f"；其餘 {len(others) - 1} 組見明細" if len(others) > 1 else ""))
    for x in rec_out:
        x.pop("_last")
    reasons = main.get("reasons", [])
    label = f"三角候選（{'、'.join(reasons) or '證據待確認'}）" if cand else kind
    early = u if u["i0"] < d["i0"] else d          # 較早那條線在收斂區間前只有起點一次碰線＝參考線（2474 上緣 06/08）
    ref_only = main["joint"] - main["start"] > 10 and sum(1 for t in early["a"] if t < main["joint"]) <= 1
    lead = (f"{_d(df.index[main['start']])} 起{'上緣' if early is u else '下緣'}參考線、{_d(df.index[main['joint']])} 起收斂形成{label}"
            if ref_only else f"{_d(df.index[main['start']])} 起形成{label}")
    text = (f"{lead}，最新收盤 {_p(C[end])}，{state_txt}（上緣 {_p(up)}、下緣 {_p(dn)}；"
            f"上緣碰線 {len(u['a'])} 次、下緣 {len(d['a'])} 次）")
    hg = main["height"]
    day = lambda ts: [_d(df.index[t]) for t in ts]
    return {"kind": kind, "candidate": cand, "candidate_reasons": reasons,
            "upper": (u["s"], u["k"]), "lower": (d["s"], d["k"]),
            "anchors": {"upper": u["a"], "lower": d["a"]}, "state": state_txt, "pos": compat,
            "current_position": "unknown" if tr["crossed"] else posn, "history": [] if cand else tr["history"],
            "failed": tr["reversed"] and not cand, "pending": bool(breaks) and posn == "inside" and not tr["crossed"],
            "first_break": breaks[0] if breaks else None, "first_bday": breaks[0]["idx"] if breaks else None,
            "bday": breaks[-1]["idx"] if breaks else None, "eval": F, "text": text, "gap": None,
            "start": main["start"], "joint_start": main["joint"], "near_tip": main["near_tip"],
            "reference_until": main["joint"] if ref_only else None,
            "height": {"H": hg["H"], "R_height": hg["R"], "contacts": [(_d(df.index[t]), x) for t, x in hg["contacts"]]},
            "scale": {"R_geom": main["R_geom"], "R_geom_date": _d(df.index[main["joint"]]),
                      "R_line_upper": u["R_line"], "R_line_lower": d["R_line"]},
            "events": {"upper": u["events"], "lower": d["events"]},
            "pending_touches": {"upper": day(u.get("pending", [])), "lower": day(d.get("pending", []))},
            "recent_pattern_events": rec_out}


def triangle_observation(df: pd.DataFrame, tri: Dict[str, Any], day: int, provisional: bool) -> Dict[str, Any]:
    """AI 結構欄位：當前位置、最近突破事件、事件是否失敗分開給。"""
    close, prev = float(df["Close"].iloc[day]), float(df["Close"].iloc[max(0, day - 1)])
    bday = tri.get("bday")
    cur = tri.get("current_position", "inside")
    if tri.get("candidate"):
        state = {"above": "outside_candidate_upper", "below": "outside_candidate_lower"}.get(cur, "inside_candidate")
        validity = "candidate"                             # 候選：價格可在候選線外，但不是正式突破
    elif cur == "unknown":
        state, validity = "past_apex", "not_applicable"     # 已過交會點：不說目前在型態內
    else:
        state = tri["pos"] if cur in ("above", "below") else ("returned_inside" if bday is not None else "inside")
        validity = "failed" if tri.get("failed") else "pending" if tri.get("pending") else "active"
    return {"kind": tri["kind"], "candidate": bool(tri.get("candidate")), "candidate_reasons": tri.get("candidate_reasons", []),
            "pattern_events": tri.get("recent_pattern_events", []), "validity": validity,
            "state": state,
            "current_position": cur, "events": [{"type": h["type"], "date": h["date"]} for h in tri.get("history", [])],
            "near_tip": tri.get("near_tip", False),
            "formation_date": df.index[tri["start"]].strftime("%Y-%m-%d"),
            "event_date": df.index[bday].strftime("%Y-%m-%d") if bday is not None else None,
            "event_age": day - bday if bday is not None else None,
            "event_direction": (1 if tri["pos"] == "break_up" else -1) if bday is not None else None,
            "is_provisional": provisional,
            "daily_direction": "up" if close > prev else "down" if close < prev else "flat"}


def _bar_hash(o, h, l, c) -> str:
    import hashlib
    return hashlib.md5(f"{o:.4f}|{h:.4f}|{l:.4f}|{c:.4f}".encode()).hexdigest()[:8]


def _tri_same_prices(df: pd.DataFrame, state: Optional[Dict[str, Any]]) -> bool:
    """這次請求的行情與已存快照的指紋（雙方都有的日期）完全一致。"""
    fp = (state or {}).get("fingerprint") or {}
    mine = {ts.strftime("%Y-%m-%d"): _bar_hash(*row) for ts, row in
            zip(df.index, df[["Open", "High", "Low", "Close"]].to_numpy(dtype=float))}
    if not fp or not mine:
        return False
    lo_d, hi_d = max(min(fp), min(mine)), min(max(fp), max(mine))   # 共同時間區間：容許前端截窗、尾端新增交易日
    a = [d for d in sorted(fp) if lo_d <= d <= hi_d]
    b = [d for d in sorted(mine) if lo_d <= d <= hi_d]
    return bool(a) and a == b and all(fp[d] == mine[d] for d in a)   # 區間內日期序列（補入／刪除 K 棒）與逐日 OHLC 都要一致


TRI_STATE_STORE: Any = None     # 測試可換成假儲存器；None＝正式資料庫（Railway /data 或 DISCORD_AI_TRI_STATE_DB=1 才啟用）


def _tri_store():
    if TRI_STATE_STORE is not None:
        return TRI_STATE_STORE
    try:
        import local_market_cache
    except Exception:
        return None
    on = Path(local_market_cache.DB_PATH).as_posix().startswith("/data") or os.getenv("DISCORD_AI_TRI_STATE_DB", "") == "1"
    return local_market_cache if on else None


def detect(df: pd.DataFrame, events: Optional[Dict[str, Any]] = None, provisional_today: bool = False,
           *, include_debug: bool = False, state_key: str = "", replay_days: Optional[int] = None,
           state_write: bool = True) -> Dict[str, Any]:
    """provisional_today＝最後一根是盤中／收盤後暫定 K（§11）。回傳 {summary, names, flags, ...}。"""
    need = ["Open", "High", "Low", "Close"]
    if df is None or not set(need) <= set(df.columns):
        return {}
    df = df.dropna(subset=need).sort_index()
    if len(df) < WARMUP_MIN + MIN_BARS + 2:
        return {"summary": [f"日 K 只有 {len(df)} 根，型態無法判定"], "names": [], "flags": {}}
    adj, flags = adjust(df, events)
    h, l, c = (adj[k].to_numpy(dtype=float) for k in ("High", "Low", "Close"))
    n = len(c)
    atr_prev = _atr_prev(h, l, c)
    last_official = n - 2 if provisional_today else n - 1
    piv = zigzag(h[:last_official + 1], l[:last_official + 1], c[:last_official + 1], atr_prev[:last_official + 1])
    ma20 = pd.Series(c).rolling(20).mean().to_numpy()
    vol = adj["Volume"].to_numpy(dtype=float) if "Volume" in adj else None
    vol_ratio = np.full(n, np.nan)
    if vol is not None:
        for i in range(20, n):
            base = np.mean(vol[i - 20:i])
            vol_ratio[i] = vol[i] / base if base > 0 else np.nan
    f3_days = set(flags.get("F3_dates") or [])

    def f3_window(end: int, length: int) -> bool:
        """量能比較窗口（end 當天與前 length 日）是否跨股數變動事件；每個被引用的比較各自判斷（審查 #5）。"""
        start = adj.index[max(0, end - length)]
        return any(start < d <= adj.index[end] for d in f3_days)

    f3_recent = f3_window(n - 1, 20)
    summary: List[str] = []
    names: List[str] = []
    levels: List[str] = []
    if n - 1 < SEARCH_DAYS:
        summary.append(f"（搜尋範圍只有 {n} 根日 K）")

    state = track(adj, piv, atr_prev, vol_ratio, f3_days, last_official)
    ev, ended, cur = state["event"], state["ended"], state["current"]
    today = n - 1
    shape_observation = structure_observation(adj, state, today, provisional_today)
    if ev:
        main, extra = _event_status(ev, last_official, c, h, l, True)
        if provisional_today:
            edge = _at(ev["upper"] if ev["dir"] == 1 else ev["lower"], today)
            where = "之上" if c[today] > edge else "之下"
            extra.append(f"今日盤中位於原{'上緣' if ev['dir'] == 1 else '下緣'} {_p(edge)} {where}（日 K 尚未完成）")
        vol_tag = ("股數基準變動，量比可比性受限" if f3_window(ev["bday"], 20) else "量能資料不足" if np.isnan(vol_ratio[ev["bday"]])
                   else ("突破日放量" if vol_ratio[ev["bday"]] >= VOL_RATIO else "突破日未達放量門檻"))
        # 起點日、突破日、目前收盤分開寫清楚，避免 AI 把「型態起點」誤當成「突破日」
        act = "向上突破上緣" if ev["dir"] == 1 else "向下跌破下緣"
        summary.append(f"{_d(adj.index[ev['start']])} 起形成{ev['kind']}，{_d(adj.index[ev['bday']])} 收盤{act}；"
                       f"最新收盤 {_p(c[last_official])}，{main}" + ("；" + "；".join(extra) if extra else "") + f"；{vol_tag}")
        names.append(ev["kind"])
        up, dn = _at(ev["upper"], today), _at(ev["lower"], today)
        role_edge = "原壓力，突破待回測" if ev["dir"] == 1 else "原支撐，跌破待反抽"
        levels.append(f"{ev['kind']}{'上緣' if ev['dir'] == 1 else '下緣'} {_p(up if ev['dir'] == 1 else dn)}（{role_edge}）")
    elif ended and ended["ended"] == "crossed" and ended["end_day"] == last_official:
        side = "下緣" if ended["dir"] == 1 else "上緣"
        act = "突破上緣" if ended["dir"] == 1 else "跌破下緣"
        summary.append(f"{ended['kind']}（{_d(adj.index[ended['start']])} 起）{act}後，今日收盤已有效越過原{side} {_p(ended['other_edge'])}")
        names.append(ended["kind"])
    elif not cur and state.get("invalid") and state["invalid"]["inv_day"] == last_official \
            and (state["invalid"]["apex"] is None or state["invalid"]["apex"] >= last_official - 1):
        iv = state["invalid"]
        meet = _at(iv["upper"], last_official)
        summary.append(f"{iv['kind']}（{_d(adj.index[iv['start']])} 起）上下緣已收斂到交會點附近（約 {_p(meet)}），"
                       f"目前收盤 {_p(c[last_official])}，尚未有效突破任一邊")
        names.append(iv["kind"])
    elif cur:
        day = today
        up, dn = _at(cur["upper"], day), _at(cur["lower"], day)
        apex = _apex(cur)
        near = apex is not None and (day - cur["start"]) >= APEX_NEAR * (apex - cur["start"])
        status = f"上下緣距離剩約 {_p(up - dn)}，接近交會點" if near else "型態內整理"
        if cur["kind"] in ("上升通道", "下降通道") and shape_observation and shape_observation["state"] == "inside":
            direction = shape_observation["daily_direction"]
            status = cur["kind"] + "內" + ("走高" if direction == "up" else "回落" if direction == "down" else "")
        if provisional_today:
            m = BREAK * cur["ref"]
            if c[today] > up + m:
                status = f"盤中越過上緣 {_p(up)}，待收盤確認"
            elif c[today] < dn - m:
                status = f"盤中跌破下緣 {_p(dn)}，待收盤確認"
        summary.append(f"{_d(adj.index[cur['start']])} 起形成{cur['kind']}，尚未有效突破；最新收盤 {_p(c[today])}，"
                       f"{status}（上緣 {_p(up)}、下緣 {_p(dn)}）")
        names.append(cur["kind"])
        levels += [f"{cur['kind']}上緣 {_p(up)}（上方候選壓力）", f"{cur['kind']}下緣 {_p(dn)}（下方候選支撐）"]

    tr = trend(piv, c, ma20, atr_prev, last_official, (h, l, adj["Open"].to_numpy(dtype=float)))
    if tr:
        summary.insert(0, tr["text"])
        names.append(tr["kind"])
    if tr and tr["line"] is not None:
        levels.append(f"趨勢線 {tr['line']}（{'下方候選支撐' if tr['line'] < c[today] else '上方候選壓力'}）")
    gp = gaps(adj, atr_prev, last_official, set(flags.get("F2_days") or []))
    summary += gp["text"]
    if gp["support"] or gp["resistance"] or gp["inside"]:
        names.append("缺口")
    cd = candles(adj, atr_prev, today, f3_window(today, 5))   # 帶量長黑用 5 日均量窗口
    if cd:
        summary.append(("目前呈現" if provisional_today else "近期 K 線：") + "、".join(cd)
                       + ("（日 K 尚未完成）" if provisional_today else ""))
        names += cd
    lv = levels_break(adj, piv, atr_prev, today)
    if lv:
        tag = "（盤中暫時，尚待收盤確認）" if provisional_today else ""
        summary += [t + tag for t in lv["text"]]
        names += lv["names"]
    part = adj.iloc[-SEARCH_DAYS:]
    hi, lo = float(part["High"].max()), float(part["Low"].min())
    if hi > lo:
        pos = (c[today] - lo) / (hi - lo) * 100
        summary.append("股價位於近 60 日區間" + ("上緣附近" if pos >= 80 else "下緣附近" if pos <= 20 else "中段"))
    flag_text = []
    if flags.get("F1"):
        flag_text.append("型態分析採還原價，歷史圖形可能與圖卡不同")
    flag_text += flags.get("F2") or []
    if f3_recent or (ev and f3_window(ev["bday"], 20)) or f3_window(today, 5):
        flag_text.append("股數基準變動，量比可比性受限")
    tri_state, new_state = None, {}
    store = _tri_store() if state_key else None   # 三角正式突破紀錄的跨日狀態快照；盤中只讀不寫
    if store is not None:
        try:
            tri_state = store.get_state(state_key)
        except Exception as exc:
            print(f"⚠️ 三角狀態讀取失敗｜{state_key}｜{type(exc).__name__}: {exc}", flush=True)
    tri = user_triangle(adj, atr_prev, last_official, tri_state, new_state, replay_days)
    for attempt in range(2):
        if store is None or not new_state or provisional_today or not state_write:   # state_write=False：輸入不同的入口只讀
            break
        __import__("request_runtime").check_budget()
        rev = int((tri_state or {}).get("revision") or 0)
        new_state["revision"] = rev + 1
        try:
            if store.set_state_if_newer(state_key, new_state, "as_of", expect_revision=rev):
                break
            latest = store.get_state(state_key)        # 期間被別的請求更新：行情相同才重讀、重算一次
            if (attempt or not latest or str(latest.get("as_of") or "") > str(new_state.get("as_of") or "")
                    or not _tri_same_prices(adj, latest)):   # 行情不同（對方較新或已更正）：停止寫入，不用舊行情覆寫
                print(f"ℹ️ 三角狀態未覆寫（已有較新或同時更新的結果）｜{state_key}｜{new_state.get('as_of')}", flush=True)
                break
            tri_state, new_state = latest, {}
            tri = user_triangle(adj, atr_prev, last_official, tri_state, new_state, replay_days)
        except Exception as exc:
            print(f"⚠️ 三角狀態寫入失敗｜{state_key}｜{type(exc).__name__}: {exc}", flush=True)
            break
    # 10-06：三角只認新算法；有新三角時舊算法型態全部讓位，沒有時只拿掉舊算法的三角（楔形、通道、箱型保留）
    old = {"箱型整理", "上升三角", "下降三角", "對稱三角收斂", "上升楔形", "下降楔形", "上升通道", "下降通道"}
    drop = old if tri and not tri.get("candidate") else {"上升三角", "下降三角", "對稱三角收斂"}   # 候選不讓其他已確認型態讓位
    summary = [x for x in summary if not any(o in x for o in drop)]
    names = [x for x in names if x not in drop]
    levels = [x for x in levels if not any(x.startswith(o) for o in drop)]
    if shape_observation and shape_observation["kind"] in drop:
        shape_observation = None
    if tri:     # §4b 艾斯畫法三角：文字、名稱、價位、AI 結構欄位、驗證圖全部用同一個結果
        summary.insert(0, tri["text"])
        label = tri["kind"] + ("候選" if tri.get("candidate") else "")
        names.insert(0, label)
        if tri.get("first_bday") is not None:     # 10-08：已突破的固定線只驗證到形成截止日，不是現行支撐壓力
            levels += [f"原{label}上緣 {_p(_at(tri['upper'], today))}（已突破，非現行支撐壓力）",
                       f"原{label}下緣 {_p(_at(tri['lower'], today))}（已突破，非現行支撐壓力）"]
        else:
            levels += [f"{label}上緣 {_p(_at(tri['upper'], today))}（上方候選壓力）",
                       f"{label}下緣 {_p(_at(tri['lower'], today))}（下方候選支撐）"]
        shape_observation = triangle_observation(adj, tri, last_official, provisional_today)
    if not any(x for x in summary if not x.startswith("股價位於")):
        summary.insert(0, "目前沒有明確的整理型態或趨勢")
    result = {"summary": summary, "names": names, "levels": levels, "flags": flag_text, "triangle": tri,
            "atr20": _p(atr_prev[last_official]) if not np.isnan(atr_prev[last_official]) else None,
            "pivots": piv, "formation": ev or cur, "ended": ended}
    result["observations"] = {
        "structure": shape_observation,
        "trend": trend_observation(tr, c, atr_prev, last_official, provisional_today),
        "price_events": [dict(x, is_provisional=provisional_today) for x in lv.get("observations", [])],
    }
    if include_debug:
        # Admin-only caller consumes the exact calculation frame; never redraw from raw prices.
        result["debug"] = {"frame": adj.copy(), "trend": tr, "invalid": state.get("invalid"),
                           "last_official": last_official, "atr_prev": atr_prev.copy(),
                           "break_multiplier": BREAK, "retest_multiplier": RETEST_ZONE}
    return result


def names(result: Dict[str, Any]) -> List[str]:
    """程式判斷到的型態名稱（事實核對用：AI 只能講這些）。"""
    return list(result.get("names") or [])
