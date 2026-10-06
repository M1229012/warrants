"""K 線型態描述器 v1（規格：docs/kline_spec_v1.md，條號標在註解）。

用途：描述「今天」的技術結構給 AI；不計分、不回測、不保存逐日狀態、圖卡不顯示。
輸入日 K（Open／High／Low／Close／Volume，index 為日期）＋公司行動事件；純計算、不打 API。
"""
from __future__ import annotations

import os
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
def trend(piv: List[Dict], c, ma20, atr_prev, day: int) -> Optional[Dict[str, Any]]:
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
    line = _at(_line(base[0], base[1]), day)
    m = BREAK * atr
    kind = "上升趨勢" if up else "下降趨勢"
    notes = []
    if up and c[day] < line - m:
        notes.append("跌破上升趨勢線")
    if down and c[day] > line + m:
        notes.append("站上下降趨勢線")
    if up and c[day] < ls[1]["price"] - m:
        notes.append("原上升結構受破壞")
    if down and c[day] > hs[1]["price"] + m:
        notes.append("原下降結構受破壞")
    text = f"{kind}（轉折高低點{'墊高' if up else '降低'}），趨勢線約 {_p(line)}" + ("；" + "、".join(notes) if notes else "")
    return {"kind": kind, "line": _p(line), "notes": notes, "text": text,
            "line_coefficients": _line(base[0], base[1]), "points": [dict(p) for p in base]}


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
    line = _at(tr["line_coefficients"], day)
    status = ("broken" if (close - last_pivot) * sign < -margin else
              "line_crossed" if (close - line) * sign < -margin else "intact")
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


# ---------------------------------------------------------------- §4b 使用者畫法三角（09-30 使用者手繪 40+ 張歸納）
TRI_DAYS, TRI_TOUCH, TRI_RECENT = int(_env("TRI_DAYS", 180)), _env("TRI_TOUCH", 0.3), int(_env("TRI_RECENT", 20))
TRI_MIN_WIDTH, TRI_MIN_APEX = _env("TRI_MIN_WIDTH", 0.5), int(_env("TRI_MIN_APEX", 5))
TRI_UP_RECENT = int(_env("TRI_UP_RECENT", 20))
TRI_APEX_POS = _env("TRI_APEX_POS", 0.9)   # 走到型態長度 9 成還沒突破＝不是三角（4576 手繪約 9 成）   # 上緣最近一次接觸要在 20 日內（試過 10 日會丟掉 2467 的長上緣）
TRI_DN_TOUCHES = int(_env("TRI_DN_TOUCHES", 2))   # 10-06：使用者手繪下緣多為「起漲點＋最近低點」兩次
TRI_UP_POKE, TRI_DN_POKE, TRI_POKES, TRI_FLAT = _env("TRI_UP_POKE", 2.0), _env("TRI_DN_POKE", 1.5), int(_env("TRI_POKES", 3)), _env("TRI_FLAT", 1.0)


def _tri_lines(H, L, O, C, A, upper: bool, lo: int, end: int, first: Optional[set] = None) -> List[Dict[str, Any]]:
    """單邊候選線：局部轉折（左右各 2 根）的影線或實體兩兩相連；收盤不可有效穿越、影線刺穿有上限、接觸 3 日內合併。
    first 有給時只用這些 index 當第一個錨點（上緣＝整理區最高峰）。"""
    top, bot = np.maximum(O, C), np.minimum(O, C)
    wick, body = (H, top) if upper else (L, bot)
    sg = 1 if upper else -1
    ext = (lambda v, i: v[i] == v[i - 2:i + 3].max()) if upper else (lambda v, i: v[i] == v[i - 2:i + 3].min())
    piv = [i for i in range(max(lo, 2), end - 1) if ext(wick, i) or ext(body, i)]
    firsts = sorted(first) if first else piv
    x_all = np.arange(end + 1)
    out = []
    for i in firsts:
        for j in piv:
            if j - i < 5:
                continue
            for pi, pj in ((wick, wick), (wick, body), (body, wick), (body, body)):
                s = (pj[j] - pi[i]) / (j - i); k = pi[i] - s * i
                x = x_all[i:end]                                     # 形成期到前一日；今天另判突破
                ln = s * x + k
                if ((C[x] - ln) * sg > 0.5 * A[x]).any():
                    continue
                poke = (wick[x] - ln) * sg
                if (poke > (TRI_UP_POKE if upper else TRI_DN_POKE) * A[x]).any() or (poke > TRI_TOUCH * A[x]).sum() > TRI_POKES:
                    continue
                hit = x[(np.abs(wick[x] - ln) <= TRI_TOUCH * A[x]) | (np.abs(body[x] - ln) <= TRI_TOUCH * A[x])]
                groups, last = 0, -99
                for h in hit:
                    groups += h - last >= 3
                    last = h
                if groups < (2 if upper else TRI_DN_TOUCHES) or hit[-1] < end - (TRI_UP_RECENT if upper else TRI_RECENT):
                    continue
                out.append({"s": float(s), "k": float(k), "a": (int(i), int(j)), "g": int(groups)})
    return out


def _gap_uppers(H, L, C, A, lo: int, end: int) -> List[Dict[str, Any]]:
    """未回補的向下跳空缺口＝壓力（3715 手繪）：缺口下緣畫水平上緣，之後高點碰到缺口區 ≥2 次、最近 20 日碰過。"""
    out = []
    for i in range(max(lo, 1), end - 5):
        if H[i] >= L[i - 1] - 0.3 * A[i]:                  # 缺口至少 0.3 ATR
            continue
        edge, ceil = float(H[i]), float(L[i - 1])
        after = np.arange(i + 1, end)
        if (C[after] > ceil).any():
            continue                                        # 收盤站上缺口上緣＝已回補
        hit = after[H[after] >= edge - TRI_TOUCH * A[after]]
        groups, last = 0, -99
        for h in hit:
            groups += h - last >= 3
            last = h
        if groups >= 2 and hit[-1] >= end - TRI_RECENT:
            out.append({"s": 0.0, "k": edge, "a": (int(i), int(hit[-1])), "g": int(groups), "gap": (edge, ceil)})
    return out


TRI_ZIGZAG = _env("TRI_ZIGZAG", 1.5)        # 主要轉折：高低點來回幅度 ≥ 1.5 ATR 才算（濾掉小雜訊）


def _zigzag(H, L, A, lo: int, end: int, k: float) -> List[Tuple[int, str]]:
    """ATR ZigZag：回傳主要轉折 [(index, 'H'/'L')]，最後一個是進行中的末端（未確認）。"""
    pts: List[Tuple[int, str]] = []
    hi = lw = lo
    mode, cand = 0, lo
    for i in range(lo + 1, end + 1):
        th = k * A[i]
        if mode == 0:
            hi = i if H[i] >= H[hi] else hi
            lw = i if L[i] <= L[lw] else lw
            if H[hi] - L[lw] >= th:
                if hi > lw:
                    pts.append((lw, "L")); mode, cand = 1, hi
                else:
                    pts.append((hi, "H")); mode, cand = -1, lw
        elif mode == 1:
            if H[i] >= H[cand]:
                cand = i
            elif H[cand] - L[i] >= th:
                pts.append((cand, "H")); mode, cand = -1, i
        else:
            if L[i] <= L[cand]:
                cand = i
            elif H[i] - L[cand] >= th:
                pts.append((cand, "L")); mode, cand = 1, i
    if mode:
        pts.append((cand, "H" if mode == 1 else "L"))
    return pts


def _hull_ray(P, B, anchor: int, later: List[int], upper: bool) -> Optional[Tuple[float, float, int]]:
    """從錨點畫一條讓之後所有主要轉折都在線的同一側的線（上緣：全部在線下；下緣：全部在線上）＝人眼沿頂點／底點畫的線。"""
    best = None
    for j in later:
        if j - anchor < 3:
            continue
        s = (B[j] - P[anchor]) / (j - anchor)          # 起點用影線極值、之後用實體（長影線可刺穿，人眼畫法）
        if best is None or (s > best[0] if upper else s < best[0]):
            best = (s, P[anchor] - s * anchor, j)
    return best


def _line_touches(P, A, s: float, k: float, start: int, end: int) -> Tuple[int, int]:
    """接觸＝影線距線 ≤ TRI_TOUCH ATR，3 日內合併；回傳 (次數, 最後接觸 index)。"""
    x = np.arange(start, end + 1)
    hit = x[np.abs(P[x] - (s * x + k)) <= TRI_TOUCH * A[x]]
    groups, last = 0, -99
    for h in hit:
        groups += h - last >= 3
        last = h
    return groups, (int(hit[-1]) if len(hit) else -1)


def user_triangle(df: pd.DataFrame, atr_prev, end: int) -> Optional[Dict[str, Any]]:
    """10-06 技術分析版三角（使用者手繪 7 檔驗證）：
    1) ATR ZigZag 找主要高低點；2) 從每個主要高點畫「之後所有主要高點都在線下」的上緣、從每個主要低點畫
    「之後所有主要低點都在線上」的下緣（或未回補向下缺口當水平上緣）；3) 上緣不往上、下緣不往下、兩線收斂、
    收盤沒有實質穿越、今天仍有開口；4) 選接觸最多、跨越最長的一組。"""
    H, L, O, C = (df[k].to_numpy(dtype=float) for k in ("High", "Low", "Open", "Close"))
    A = np.where(np.isnan(atr_prev), np.nanmedian(atr_prev), atr_prev)
    lo = max(21, end - TRI_DAYS)
    if end - lo < 30:
        return None
    a_ref = float(np.nanmedian(A[end - 19:end + 1]))
    flat = lambda l: abs(l["s"]) * (end - l["a"][0]) <= TRI_FLAT * a_ref
    piv = _zigzag(H, L, A, lo, end, TRI_ZIGZAG)
    piv_all = list(piv)                                    # 含進行中的末端：用來檢查來回震盪
    piv = piv[:-1]                                         # 最後一個是進行中的末端（可能就是今天），不拿來畫線
    highs = [i for i, t in piv if t == "H"]
    lows = [i for i, t in piv if t == "L"]
    x_all = np.arange(end + 1)

    top, bot = np.maximum(O, C), np.minimum(O, C)

    def check(P, B, anchor, s, k, upper):
        x = x_all[anchor:end]
        sg = 1 if upper else -1
        if ((C[x] - (s * x + k)) * sg > 0.5 * A[x]).any():
            return None                                    # 收盤實質穿越（今天另判突破）
        poke = (P[x] - (s * x + k)) * sg
        pk_days = x[poke > TRI_TOUCH * A[x]]
        pk_groups = int(np.sum(np.diff(pk_days) >= 3) + 1) if len(pk_days) else 0   # 連續 3 日內的刺穿算同一次
        if (poke > (TRI_UP_POKE if upper else TRI_DN_POKE) * A[x]).any() or pk_groups > TRI_POKES:
            return None                                    # 影線刺穿太深或太多次
        if upper and end - anchor > 6:
            # 線不可浮在「起點之後的低點」之後那段整理區最高峰上方（2344 要壓到 H8）
            trough = anchor + 1 + int(np.argmin(L[anchor + 1:end]))
            seg = np.arange(trough + 1, end)
            if len(seg):
                pk = int(seg[np.argmax(H[seg])])
                if s * pk + k - H[pk] > TRI_TOUCH * A[pk]:
                    return None
        g1, l1 = _line_touches(P, A, s, k, anchor, end)
        g2, l2 = _line_touches(B, A, s, k, anchor, end)
        g, last = max(g1, g2), max(l1, l2)
        if g < 2 or last < end - TRI_RECENT:
            return None
        return g

    def build(P, anchor, later, upper):
        """包絡線：從主要轉折起點畫一條讓之後所有 K 棒（上緣看實體頂、下緣看影線低）都在線同一側的線。"""
        B = top if upper else L                           # 上緣讓長上影線刺穿（用實體）；下緣貼影線低點
        ray = _hull_ray(P, B, anchor, later, upper)
        if ray is None:
            return None
        s, k, j = ray
        g = check(P, B, anchor, s, k, upper)
        return None if g is None else {"s": float(s), "k": float(k), "a": (int(anchor), int(j)), "g": int(g)}

    # 起點只用主要轉折；畫線時看之後「所有 K 棒」的外緣（2421 的 09/22 小高點也要壓到）
    # 起點影線頂、實體頂兩種都試（2421 手繪上緣從 09/01 實體頂畫起）
    uppers = [u for h in highs if h < end - 5 for P0 in (H, top)
              for later in (list(range(h + 3, end)), [i for i in highs if i > h + 2])   # 包絡線＋只連主要高點兩種
              for u in [build(P0, h, later, True)]
              if u and (u["s"] <= 0 or flat(u))]
    uppers += _gap_uppers(H, L, C, A, lo, end)
    lowers = [d for l in lows if l < end - 5 for d in [build(L, l, list(range(l + 3, end)), False)]
              if d and d["s"] >= -0.002 * a_ref]
    best = None
    for u in uppers:
        for d in lowers:
            up_now, dn_now = u["s"] * end + u["k"], d["s"] * end + d["k"]
            broke = C[end] > up_now + BREAK * A[end] or C[end] < dn_now - BREAK * A[end]
            if up_now - dn_now < (0 if broke else TRI_MIN_WIDTH * a_ref):
                continue                                   # 今天沒突破時開口至少 0.5 ATR；今天收盤突破（2421）可以接近尖端
            if not (flat(u) and flat(d)):
                if u["s"] >= d["s"] or (d["k"] - u["k"]) / (u["s"] - d["s"]) < end + (0 if broke else TRI_MIN_APEX):
                    continue
            if abs(u["a"][0] - d["a"][0]) > 120:
                continue                                   # 兩條邊起點差太遠＝不是同一段整理
            first = min(u["a"][0], d["a"][0])
            if end - first < 15:
                continue                                   # 型態至少 15 個交易日
            if min(end - u["a"][0], end - d["a"][0]) < 0.4 * (end - first):
                continue                                   # 兩條線都要涵蓋型態 4 成以上（2412 下緣只有最近 7 天）
            if u["s"] > 0 and u["s"] * (end - u["a"][0]) > 0.25 * a_ref:
                continue                                   # 上緣往上超過 0.5 ATR＝不是水平（2412、2603 那種）
            # 三角定義：主要高低點在兩線之間來回（高→低→高→低），兩條線各被主要轉折碰到至少 2 次
            seq = []
            for i, t in piv_all:
                if i < first:
                    continue
                near_up = (H[i] >= u["k"] - 0.5 * A[i]) if u.get("gap") else                     min(abs(u["s"] * i + u["k"] - H[i]), abs(u["s"] * i + u["k"] - top[i])) <= 0.5 * A[i]
                if t == "H" and near_up:                   # 缺口上緣：高點進到缺口區就算碰到
                    seq.append("H")
                elif t == "L" and abs(d["s"] * i + d["k"] - L[i]) <= 0.5 * A[i]:
                    seq.append("L")
            turns = sum(1 for a1, a2 in zip(seq, seq[1:]) if a1 != a2)
            if seq.count("H") < 2 or seq.count("L") < 2 or turns < 3:
                continue                                   # 沒有來回震盪（3008 急漲後回檔一段不算三角）
            if not (flat(u) and flat(d)):
                apex = (d["k"] - u["k"]) / (u["s"] - d["s"])
                if (end - first) / max(apex - first, 1e-9) > (0.95 if broke else TRI_APEX_POS):
                    continue                               # 已走到尖端附近還沒出方向＝不是三角（通常 2/3～3/4 就會突破）
            key = ((end - u["a"][0]) + (end - d["a"][0]), u["g"] + d["g"])   # 先比兩邊長度（主要結構），再比接觸次數
            if best is None or key > best[0]:
                best = (key, u, d)
    if not best:
        return None
    _, u, d = best
    level = lambda l: abs(l["s"]) * (end - l["a"][0]) <= 0.5 * a_ref   # 命名用：整段高低差 ≤0.5 ATR 才叫水平（4576 降 1 ATR 不算）
    kind = ("箱型整理" if level(u) and level(d) else "上升三角" if level(u) else "下降三角" if level(d) else "三角收斂")
    up, dn = u["s"] * end + u["k"], d["s"] * end + d["k"]
    state = ("收盤向上突破上緣" if C[end] > up + BREAK * A[end] else "收盤向下跌破下緣" if C[end] < dn - BREAK * A[end]
             else "位於型態內")
    start = df.index[min(u["a"][0], d["a"][0])]
    text = (f"{_d(start)} 起形成{kind}，最新收盤 {_p(C[end])}，{state}（上緣 {_p(up)}、下緣 {_p(dn)}；"
            f"上緣接觸 {u['g']} 次、下緣 {d['g']} 次）")
    if u.get("gap"):
        text += f"；上緣為 {_d(df.index[u['a'][0]])} 未回補缺口 {_p(u['gap'][0])}～{_p(u['gap'][1])}"
    return {"kind": kind, "upper": (u["s"], u["k"]), "lower": (d["s"], d["k"]), "anchors": {"upper": u["a"], "lower": d["a"]},
            "state": state, "text": text, "gap": u.get("gap")}


def detect(df: pd.DataFrame, events: Optional[Dict[str, Any]] = None, provisional_today: bool = False,
           *, include_debug: bool = False) -> Dict[str, Any]:
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

    tr = trend(piv, c, ma20, atr_prev, last_official)
    if tr:
        summary.insert(0, tr["text"])
        names.append(tr["kind"])
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
    old = ev or cur
    # 10-06：舊算法的線收斂（上緣不往上、下緣不往下）就保留它的突破追蹤；同方向（楔形、通道）才改用使用者畫法三角
    same_dir = bool(old) and (old["upper"][0] > 0 and old["lower"][0] > 0 or old["upper"][0] < 0 and old["lower"][0] < 0)
    tri = None if ev and not same_dir else user_triangle(adj, atr_prev, last_official)
    if tri:     # §4b 使用者畫法三角優先：取代舊畫線型態的描述，避免兩套線互相矛盾
        old = {"箱型整理", "上升三角", "下降三角", "對稱三角收斂", "上升楔形", "下降楔形", "上升通道", "下降通道"}
        summary = [x for x in summary if "起形成" not in x and "原上緣" not in x and "原下緣" not in x]
        names = [x for x in names if x not in old]
        summary.insert(0, tri["text"])
        names.insert(0, tri["kind"])
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
