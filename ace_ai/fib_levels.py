"""費波南西參考價（測試版 10-27）：沿用 kline_patterns 的還原價、ATR 與 ZigZag，選大級別波段算回撤／延伸價位，
再找前高低、大量區、整數關卡與其他波段費波的共振。純計算、不打 API；驗證圖與之後的 AI 解讀共用這一份結果。

錨點規則（寫死，同一檔同一天一定抓同一組點）：
  1. 大級別轉折＝ZigZag 門檻放大 MAJOR_MULT 倍；確認日當天不用（與型態驗證一致）。
  2. 先找終點在最近 NEAR_LOOKBACK 根內、幅度 ≥ LEG_ATR 倍 ATR 的最近一段＝選用波段 A→B；找不到可用的才放寬到 LOOKBACK（約一年）。
     後續同向小段創更高（低）時併入，B 往後移。
  3. 上漲段：B 回撤 0.382／0.5／0.618 為下方支撐；B 之後已確認的回檔低點 C 存在時，C＋(B−A)×1.0／1.382／1.618 為延伸目標；
     沒有 C 就創新高時，A＋(B−A)×1.382／1.618／2／2.618。
     下跌段：B＋(A−B)×0.382／0.5／0.618 為反彈參考，A 為前波高點；收盤已站上 A 時改看 B＋(A−B)×1.382／1.618／2／2.618。
  4. 價位落在其他結構 CONF_ATR 倍 ATR 內＝共振（TIGHT_ATR 內另標「緊密」）；只有「在收盤之上且有共振」的費波價位（及前波高點）才交給 AI。
     創新高（延伸模式）上方沒有結構可共振，收盤之上最近 EXTEND_AI 區延伸價位直接交給 AI。
  5. 最近結束的整理區（≥ BOX_MIN 根、收盤高低差 ≤ BOX_ATR 倍 ATR）：上緣＋整理高度＝等幅測量，畫成價位並當共振對象。
  6. 下跌低點還沒確認（最後一段是幅度 ≥ LEG_ATR 的未確認下跌）：另算下方參考——收盤所在大波段的 0.618／0.786／1.0 回撤、
     前波低點、上一段大跌從這次高點往下量 0.618／1.0；收盤之下最近 SUPPORT_AI 區交給 AI。
  7. 依未確認錨點（終點還沒確認的波段）算出的價位＝暫定（tentative）：仍交給 AI，但註明確認前可能調整。
  8. 離收盤 ≤ TEST_ATR 倍 ATR 的價位＝現價附近觀察位（watch）：不當目標價，以現況資訊交給 AI。
  9. 相距 ≤ ZONE_ATR 倍 ATR 的價位先合併成一區，交給 AI 的名額以「區」計算。
 10. 整數關卡只顯示、不單獨給 AI 資格；所有模式收盤之上最多 ABOVE_AI 區（資格→合併→延伸加選→留最近幾區）。
 11. 整理區上下緣本身也是價位；未突破時等幅標「突破上緣後才成立」，拉回中的延伸要先站上 B——兩者都是條件目標。
 12. 收盤之下有結構依據的價位也交給 AI（最多 SUPPORT_AI 區）；摘要分「目前壓力／突破後條件目標／下方候選支撐」。
 13. 主圖固定近段；起點 A 在圖外時，回撤名稱帶「大波段」與錨點日期，摘要另寫大波段背景。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import kline_patterns as kp


def _env(name: str, default: float) -> float:
    return kp._env("FIB_" + name, default)   # 環境變數 DISCORD_AI_KLINE_FIB_*


MAJOR_MULT = _env("MAJOR_MULT", 2.5)      # 大級別轉折門檻＝ZIGZAG_ATR × 此倍數
HISTORY = int(_env("HISTORY", 260))       # 計算只用最後 N 根（含 ATR 暖機），結果不隨載入的歷史長短改變
NEAR_LOOKBACK = int(_env("NEAR_LOOKBACK", 70))   # 10-28：先找終點在最近 N 根內的波段，驗證圖也只顯示這段
LOOKBACK = int(_env("LOOKBACK", HISTORY))        # 近段找不到可用波段才放寬到這裡（約一年）
REF_LOOKBACK = int(_env("REF_LOOKBACK", 120))    # 共振用的前高低、大量區、整理區回看根數（不隨選段範圍改變）
LEG_ATR = _env("LEG_ATR", 6.0)            # 合格波段幅度 ≥ 此倍數 ATR（波段終點當日的前一日 ATR）
CONF_ATR = _env("CONF_ATR", 1.0)          # 共振距離：目前 ATR 的倍數
TIGHT_ATR = _env("TIGHT_ATR", 0.25)       # 共振中「緊密重合」的距離
ZONE_ATR = _env("ZONE_ATR", 0.25)         # 相距在此倍數 ATR 內的價位合併成一區（2308：1406／1425 相差 0.23 ATR）
OTHER_LEGS = int(_env("OTHER_LEGS", 2))   # 另外拿最近幾段合格波段的回撤價做群聚比對
VOL_BIN_ATR = _env("VOL_BIN_ATR", 0.5)    # 大量區價格格寬（ATR 倍數）
VOL_TOP = int(_env("VOL_TOP", 3))         # 最多取幾個大量區
VOL_MIN_RATIO = _env("VOL_MIN_RATIO", 1.5)   # 大量區成交量 ≥ 平均格的倍數
RETRACE = (0.382, 0.5, 0.618)
EXTEND = (1.0, 1.382, 1.618)
BOX_MIN = int(_env("BOX_MIN", 20))        # 整理區至少幾根
BOX_ATR = _env("BOX_ATR", 4.5)            # 整理區收盤高低差 ≤ 此倍數 ATR（整理區最後一天的前一日 ATR）
BOX_RECENT = int(_env("BOX_RECENT", 10))  # 整理區要在最近幾根內結束（離太久的整理不算這次突破）
BREAKOUT = (1.382, 1.618, 2.0, 2.618)   # 越過波段端點、沒有 C 可用時，從低點起算的延伸倍數
EXTEND_AI = int(_env("EXTEND_AI", 2))     # 創新高時收盤之上最近幾區延伸價位直接交給 AI（上方沒有結構可共振）
SUPPORT_RETRACE = (0.618, 0.786, 1.0)   # 下跌低點未確認時：大波段回撤（只取收盤之下的）
DROP_PROJECT = (0.618, 1.0)             # 下跌低點未確認時：上一段大跌幅度從這次高點往下量
SUPPORT_AI = int(_env("SUPPORT_AI", 2))   # 下跌低點未確認時，收盤之下最近幾區交給 AI
ABOVE_AI = int(_env("ABOVE_AI", 2))       # 所有模式：收盤之上最多幾區交給 AI（2881：延伸一路給到 300）
TEST_ATR = _env("TEST_ATR", 0.5)          # 離收盤在此倍數 ATR 內＝現價附近觀察位，不當目標價（2308：1504 貼著收盤 1510）


def round_step(price: float) -> float:
    """整數關卡間距：10 元以下 1 元、50 元以下 5 元、100 元以下 10 元、500 元以下 50 元、1000 元以下 100 元，以上 500 元。"""
    for limit, step in ((10, 1), (50, 5), (100, 10), (500, 50), (1000, 100)):
        if price < limit:
            return step
    return 500


def _pivot(p: Dict, dates: List[str]) -> Dict[str, Any]:
    return {"type": p["type"], "idx": int(p["idx"]), "price": float(p["price"]),
            "confirm": int(p["confirm"]), "date": dates[p["idx"]], "confirm_date": dates[p["confirm"]]}


def _volume_zones(h, l, c, v, begin: int, last: int, ref: float) -> List[Dict[str, Any]]:
    """回看區間的成交量依典型價分格，取量最大的幾格（需明顯高於平均格）。"""
    width = VOL_BIN_ATR * ref
    vol = np.nan_to_num(v[begin:last + 1], nan=0.0)
    if width <= 0 or vol.sum() <= 0:
        return []
    typical = (h[begin:last + 1] + l[begin:last + 1] + c[begin:last + 1]) / 3
    low = float(l[begin:last + 1].min())
    totals = np.bincount(((typical - low) // width).astype(int), weights=vol)
    mean = totals[totals > 0].mean()
    zones = []
    for k in np.argsort(totals)[::-1][:VOL_TOP]:
        if totals[k] < VOL_MIN_RATIO * mean:
            break
        zones.append({"low": low + k * width, "high": low + (k + 1) * width, "price": low + (k + 0.5) * width})
    return zones


def find_box(c, atr, last: int) -> Optional[Dict[str, Any]]:
    """最近結束的整理區：終點在最近 BOX_RECENT 根內，往回延伸到收盤高低差超過 BOX_ATR 倍 ATR 為止，取最長的一段。"""
    floor = max(0, last - REF_LOOKBACK + 1)
    best = None
    for e in range(last - 1, max(floor + BOX_MIN - 1, last - BOX_RECENT) - 1, -1):
        if np.isnan(atr[e]):
            continue
        hi = lo = c[e]
        s = e
        while s - 1 >= floor and max(hi, c[s - 1]) - min(lo, c[s - 1]) <= BOX_ATR * atr[e]:
            hi, lo, s = max(hi, c[s - 1]), min(lo, c[s - 1]), s - 1
        if e - s + 1 >= BOX_MIN and (best is None or e - s > best["end"] - best["start"]):
            best = {"start": s, "end": e, "top": float(hi), "bottom": float(lo)}
    return best


def compute(frame: pd.DataFrame, events: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """10-28：先用最近 NEAR_LOOKBACK 根找波段（大多數股票這樣就畫得出來，圖也只顯示這段）；
    找不到可用波段（只剩備援或失效段）才放寬到約一年。"""
    near = _compute(frame, events, NEAR_LOOKBACK)
    if near["found"] or LOOKBACK <= NEAR_LOOKBACK:
        return near
    return _compute(frame, events, LOOKBACK, widened=True)


def _hits(item: Dict[str, Any], refs: List[Dict[str, Any]], zones: List[Dict[str, Any]], tol: float) -> List[tuple]:
    """價位的共振對象（文字, 距離）。10-28：依來源身分排除自己（同一個轉折、同一個整理區），
    不能用「價格完全相同」排除——不同日期、不同波段剛好同價是最強的重合。"""
    price = item["price"]
    out = [(x["text"], abs(x["price"] - price)) for x in refs
           if abs(x["price"] - price) <= tol and x.get("level") is not item
           and not (x.get("idx") is not None and x["idx"] == item.get("src_idx"))]
    out += [(f"大量區 {z['low']:.2f}～{z['high']:.2f}", max(0.0, abs(z["price"] - price) - (z["high"] - z["low"]) / 2))
            for z in zones if abs(z["price"] - price) <= tol + (z["high"] - z["low"]) / 2]
    step = round_step(price)
    nearest = round(price / step) * step
    if nearest > 0 and abs(nearest - price) <= tol:
        out.append((f"整數關卡 {nearest:g}", abs(nearest - price)))
    return out


def ai_reference(res: Dict[str, Any]) -> Dict[str, Any]:
    """10-29：問型態時給 AI 的費波參考價（和費波驗證圖同一份 compute 結果）。只放交給 AI 的價位區，分成
    目前壓力／突破後條件目標／下方候選支撐／現價附近觀察；不帶比例計算過程與錨點代號。"""
    if not res.get("selected"):
        return {"狀態": "沒有可用的費波波段，不提費波價位"}

    def item(z):
        out = {"依據": "、".join(z["labels"])}
        if z["high"] - z["low"] < 1e-9:
            out["價位"] = round(z["low"], 2)
        else:
            out["價位區"] = f"{z['low']:.2f}～{z['high']:.2f}"
        if z["tentative"]:
            out["註記"] = "依目前高低點試算，確認前可能調整"
        return out

    areas = [z for z in res.get("areas") or [] if z["ai"]]
    ups = sorted([z for z in areas if z["above"]], key=lambda z: z["low"])
    out = {"資料日": res["bars"][res["last"]]["date"], "收盤": round(res["close"], 2),
           "模式": res["mode"], "ATR": round(res["atr"], 4),   # Bot 用來分上漲／還在下跌、和觀察重點價位去重
           "說明": res["summary"][0].split("｜", 1)[-1],
           "目前壓力": [item(z) for z in ups if not z["conditional"]],
           "突破後條件目標": [item(z) for z in ups if z["conditional"]],
           "下方候選支撐": [item(z) for z in sorted([z for z in areas if not z["above"]], key=lambda z: -z["high"])],
           "現價附近觀察": [{"依據": lv["label"], "價位": round(lv["price"], 2)} for lv in res["levels"] if lv["watch"]]}
    if res["selected"].get("fallback"):
        out["可信度"] = "沒有合格費波主段，以下僅為區間量測"
    return {k: v for k, v in out.items() if v not in ([], "", None)}


def _area_text(area: Dict[str, Any]) -> str:
    if len(area["members"]) == 1:
        x = area["members"][0]
        return f"{x['label']} {x['price']:.2f}"
    return f"{area['low']:.2f}～{area['high']:.2f}（" + "、".join(x["label"] for x in area["members"]) + "）"


def _compute(frame: pd.DataFrame, events: Optional[Dict[str, Any]], lookback: int, widened: bool = False) -> Dict[str, Any]:
    need = ["Open", "High", "Low", "Close"]
    if frame is None or not set(need) <= set(frame.columns):
        raise ValueError("日 K 欄位不足，無法計算費波")
    # 10-27：固定只用最後 HISTORY 根，ZigZag 起算點不隨載入的歷史長短改變（3034 兩次抓到不同 A）
    df = frame.dropna(subset=need).sort_index().iloc[-HISTORY:]
    if len(df) < kp.WARMUP_MIN + kp.MIN_BARS + 2:
        raise ValueError(f"日 K 只有 {len(df)} 根，無法計算費波")
    adj, adjust_flags = kp.adjust(df, events)
    o, h, l, c = (adj[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    v = adj["Volume"].to_numpy(dtype=float) if "Volume" in adj else np.zeros(len(c))
    dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in adj.index]
    last = len(c) - 1
    atr = kp._atr_prev(h, l, c)
    ref = float(atr[last])
    if np.isnan(ref) or ref <= 0:
        raise ValueError("ATR 資料不足，無法計算費波")
    close = float(c[last])
    begin = max(0, last - lookback + 1)
    ref_begin = max(0, last - REF_LOOKBACK + 1)
    tol, tight = CONF_ATR * ref, TIGHT_ATR * ref
    minor = [p for p in kp.zigzag(h, l, c, atr) if p["confirm"] < last]
    major = [p for p in kp.zigzag(h, l, c, atr * MAJOR_MULT) if p["confirm"] < last]

    legs = []
    for a, b in zip(major, major[1:]):
        if b["idx"] < begin:   # 終點在回看範圍內就算；起點可以在範圍外（3034：04-23 起漲）
            continue
        unit = atr[b["idx"]] if not np.isnan(atr[b["idx"]]) else ref
        amp = abs(b["price"] - a["price"])
        status = f"幅度 {amp / unit:.1f} ATR，未達 {LEG_ATR:g} ATR" if amp / unit < LEG_ATR else ""
        legs.append({"a": a, "b": b, "dir": 1 if b["type"] == "H" else -1, "amp": amp,
                     "amp_atr": amp / unit, "status": status})
    qualified = [g for g in legs if not g["status"]]

    def anchors(leg):
        """A、B（同向小段創更高／低時 B 往後移）、C（B 之後的回檔極值）與是否已被反向越過 A。"""
        d, a, b = leg["dir"], leg["a"], leg["b"]
        broken = merged = False
        for p in [p for p in major if p["idx"] > b["idx"]]:
            if p["type"] == a["type"] and (p["price"] - a["price"]) * d < 0:
                broken = True
                break
            if p["type"] == b["type"] and (p["price"] - b["price"]) * d > 0:
                b, merged = p, True
        after = [p for p in (minor if leg.get("fallback") else major) if p["idx"] > b["idx"] and p["type"] == a["type"]]
        cpt = (min(after, key=lambda p: p["price"]) if d > 0 else max(after, key=lambda p: p["price"])) if after and not broken else None
        return a, b, cpt, merged, broken

    def stale(leg, a, b, cpt, broken):
        """10-28：錨點過時分三種——原方向判斷失效、漲過最遠延伸要重新量測、跌破 B＝跌勢延續。回傳（類別, 原因）。"""
        if broken:
            return "已失效", "後續已反向越過波段起點 A"
        amp = abs(b["price"] - a["price"])
        if leg["dir"] > 0:
            if b["idx"] < last and l[b["idx"] + 1:].min() < a["price"]:
                return "已失效", "股價已跌破波段起點 A，原上漲判斷失效"
            top = cpt["price"] + amp * EXTEND[-1] if cpt else a["price"] + amp * BREAKOUT[-1]
        else:
            if b["idx"] < last and l[b["idx"] + 1:].min() < b["price"]:
                return "跌勢延續", "股價已跌破波段低點 B，改量新的一段"
            top = b["price"] + amp * BREAKOUT[-1]
        return ("需重新量測", "收盤已漲過最遠的延伸價位，原量尺用完") if close > top else ("", "")

    provisional = None   # 最後一個大轉折到目前極值：終點尚未確認，但幅度夠大就是最新的一段
    if major and major[-1]["idx"] < last:
        p = major[-1]
        span = (h if p["type"] == "L" else l)[p["idx"] + 1:]
        j = p["idx"] + 1 + int(np.argmax(span) if p["type"] == "L" else np.argmin(span))
        e = {"type": "H" if p["type"] == "L" else "L", "idx": j, "price": float(span[j - p["idx"] - 1]), "confirm": last}
        amp = abs(e["price"] - p["price"])
        if amp / ref >= LEG_ATR and j >= begin:   # 10-28：終點也要在回看範圍內，與已確認波段同一條件
            provisional = {"a": p, "b": e, "dir": 1 if e["type"] == "H" else -1, "amp": amp, "amp_atr": amp / ref,
                           "status": "", "provisional": True}

    def support_levels(top) -> List[Dict[str, Any]]:
        """下跌低點未確認時的下方參考（都在收盤之下）：大波段回撤、前波低點、下跌等幅。top＝這次下跌的起點（最後一個大轉折高點）。"""
        pairs = []
        for a, b in zip(major, major[1:]):
            unit = atr[b["idx"]] if not np.isnan(atr[b["idx"]]) else ref
            if abs(b["price"] - a["price"]) / unit >= LEG_ATR:
                pairs.append({"a": a, "b": b, "dir": 1 if b["type"] == "H" else -1})
        out = []
        ups = []
        for g in pairs:   # 1. 收盤所在、起點最近的一段上漲（同向小段併入）：0.618／0.786／1.0
            if g["dir"] > 0:
                ga, gb = anchors(g)[:2]
                if ga["price"] < close < gb["price"] and gb["idx"] != top["idx"]:   # 終點就是這次下跌起點＝正在回撤的那段反彈，不算大波段
                    ups.append((gb["price"] - ga["price"], ga, gb))
        if ups:   # 10-28：取起點最近的一段，不取幅度最大的（一年資料會選到太早的起漲點，價位離現價太遠）
            amp_u, ga, gb = max(ups, key=lambda x: x[1]["idx"])
            for r in SUPPORT_RETRACE:
                price = gb["price"] - amp_u * r
                if price < close:
                    out.append({"label": f"大波段回撤 {r:g}（{dates[ga['idx']][5:]}～{dates[gb['idx']][5:]}）",
                                "tag": f"大段{r:g}", "kind": "support", "ratio": r, "price": price,
                                "src_idx": int(ga["idx"]) if r == 1.0 else None})   # 回撤 1.0＝起點那個轉折本身
        lows = [p for p in major if p["type"] == "L" and p["price"] < close]
        if lows:   # 2. 收盤下方最近的大轉折低點（已跌破的前低在收盤之上，自然不列）
            p = max(lows, key=lambda p: p["price"])
            out.append({"label": f"前波低點（{dates[p['idx']]}）", "tag": "前低", "kind": "support", "ratio": 1.0,
                        "price": float(p["price"]), "src_idx": int(p["idx"])})
        downs = [g for g in pairs if g["dir"] < 0 and g["b"]["idx"] <= top["idx"] and g["a"]["price"] > top["price"]]
        if downs:   # 3. 上一段大跌（高點比這次高）的幅度，從這次的較低高點往下量；10-28：跌破那段的低點不影響投射
            g = downs[-1]
            amp_d = g["a"]["price"] - g["b"]["price"]
            for r in DROP_PROJECT:
                price = top["price"] - amp_d * r
                if 0 < price < close:
                    out.append({"label": f"下跌等幅 {r:g}（{dates[g['a']['idx']][5:]}～{dates[g['b']['idx']][5:]}）",
                                "tag": f"等幅{r:g}", "kind": "support", "ratio": r, "price": price})
        return out

    # 選段順序：最近的合格大波段 → 過時改用未確認的最後一段 → 再沒有就用回看區間最低／最高點（備援）
    chosen, note, primary = None, "", None
    if qualified:
        leg = qualified[-1]
        used = [leg]
        k = legs.index(leg)
        prev = legs[k - 1] if k else None
        if leg["dir"] < 0 and prev and not prev["status"] and prev["b"] is leg["a"] and leg["b"]["price"] > prev["a"]["price"]:
            leg["status"] = "合格；守住起點，視為上漲段的回檔 B→C"
            leg = prev   # 大回檔但守住起點＝上漲段的 A→B→C，仍以上漲段為主（才算得出延伸）
            used.append(leg)
        primary = (leg, used, anchors(leg))
        kind, why = stale(leg, *primary[2][:3], primary[2][4])
        if kind:
            leg["status"] = f"{kind}：{why}"
            note = f"原波段 {leg['a']['price']:.2f}→{leg['b']['price']:.2f} {kind}（{why}）"
        else:
            chosen = primary
    if chosen is None and provisional:
        legs.append(provisional)
        chosen = (provisional, [provisional], (provisional["a"], provisional["b"], None, False, False))
    if chosen is None:   # 備援：每檔都畫得出來，但只算區間量測
        lo_i = begin + int(np.argmin(l[begin:last + 1]))
        hi_i = begin + int(np.argmax(h[begin:last + 1]))
        if lo_i != hi_i and h[hi_i] > l[lo_i]:
            ends = sorted(((lo_i, "L", l[lo_i]), (hi_i, "H", h[hi_i])))
            a, b = ({"type": t, "idx": i, "price": float(p), "confirm": i} for i, t, p in ends)
            amp = abs(b["price"] - a["price"])
            legs.append({"a": a, "b": b, "dir": 1 if b["type"] == "H" else -1, "amp": amp,
                         "amp_atr": amp / ref, "status": "", "fallback": True})
            chosen = (legs[-1], legs[-1:], anchors(legs[-1]))
    if chosen is None and primary:   # 連備援都沒有（價格不動）：保留過時的原波段並註明
        chosen = primary
    fallback = bool(chosen and chosen[0].get("fallback"))
    found = bool(chosen and not fallback and not (chosen is primary and note))

    selected, levels, areas, summary = None, [], [], []
    mode, mode_text = "none", f"最近 {lookback} 根價格沒有波動，不給費波價位"
    if chosen:
        leg, used, (a, b, cpt, merged, broken) = chosen
        d = leg["dir"]
        unconfirmed = bool(leg.get("provisional") or leg.get("fallback"))
        amp = abs(b["price"] - a["price"])
        lo, hi = min(a["price"], b["price"]), max(a["price"], b["price"])
        leg["selected"] = True
        unit = atr[b["idx"]] if not np.isnan(atr[b["idx"]]) else ref
        selected = {"a": a, "b": b, "c": cpt, "dir": d, "amp": amp, "amp_atr": amp / unit,
                    "merged": merged, "broken": broken, "fallback": bool(leg.get("fallback")),
                    "provisional": bool(leg.get("provisional"))}
        at_high = close > hi or (unconfirmed and d > 0 and b["idx"] >= last - 5)   # 終點就是近日新高＝還在創高
        show = 0 if widened else begin   # 10-28（2881）：主圖固定近段，不再為了起點 A 往前拉
        # 起點 A 在圖外＝這是大波段的量測，名稱帶上錨點日期，與近期結構分開
        span = f"（大波段 {dates[a['idx']][5:]}～{dates[b['idx']][5:]}）" if a["idx"] < show else ""
        if d > 0:
            for r in RETRACE:
                levels.append({"label": f"回撤 {r:g}{span}", "tag": f"回撤{r:g}", "kind": "retrace", "ratio": r,
                               "price": hi - amp * r})
            levels.append({"label": "前波高點 B", "kind": "prior", "ratio": 1.0, "price": hi})
            if cpt and close < hi:   # 還在 B 之下：C→B 的反彈價位也是上方參考
                for r in RETRACE:
                    levels.append({"label": f"反彈 {r:g}", "kind": "retrace", "ratio": r,
                                   "price": cpt["price"] + (hi - cpt["price"]) * r})
            if cpt:
                for r in EXTEND:
                    levels.append({"label": f"延伸 {r:g}", "kind": "extend", "ratio": r, "price": cpt["price"] + amp * r})
            elif not broken:   # 10-27：沒有 C 時延伸從 A 起算（創新高時才是主要目標，否則是站上 B 後的參考）
                for r in BREAKOUT:
                    levels.append({"label": f"延伸 {r:g}", "kind": "extend", "ratio": r, "price": lo + amp * r})
            if at_high:
                mode, mode_text = "extend", "收盤高於波段高點 B（創波段新高），上方看延伸價位"
            else:
                mode, mode_text = "pullback", "上漲波段後拉回，上方先看前波高點 B，站上後看延伸價位"
            if not cpt and not broken:
                mode_text += "；B 之後沒有已確認的回檔低點 C，延伸價位以 A 起算"
        else:
            for r in RETRACE:
                levels.append({"label": f"反彈 {r:g}{span}", "tag": f"反彈{r:g}", "kind": "retrace", "ratio": r,
                               "price": lo + amp * r})
            levels.append({"label": "前波高點 A", "kind": "prior", "ratio": 1.0, "price": hi})
            if close > hi and not broken:   # 10-27：已站上 A＝反彈超過 100%，改看 B 起算的延伸（4566 案例）
                for r in BREAKOUT:
                    levels.append({"label": f"延伸 {r:g}", "kind": "extend", "ratio": r, "price": lo + amp * r})
                mode, mode_text = "extend", "收盤已站上下跌段起點 A（反彈超過 100%），上方看 B＋(A−B) 的延伸價位"
            else:
                mode, mode_text = "rebound", "下跌波段後反彈，上方看下跌段的回撤反彈價位與前波高點 A"
        if mode == "pullback":   # 還在 B 之下：延伸要先站上 B 才成立＝條件目標
            for item in levels:
                if item["kind"] == "extend":
                    item["conditional"] = True
        if broken:
            mode_text += "；後續已反向越過波段起點 A，此段僅供參考"
        if fallback:
            mode_text = f"（備援）沒有合格費波主段，以下僅為區間量測（最近 {lookback} 根最低／最高點）；" + mode_text
        elif selected["provisional"]:
            mode_text = "（未確認波段）最後一個大轉折到目前極值，終點還沒確認；" + mode_text
        if note:
            mode_text = note + "；" + mode_text

        refs = []
        anchor_idx = {a["idx"], b["idx"]} | ({cpt["idx"]} if cpt else set())
        for p in minor:
            if p["idx"] >= ref_begin and p["idx"] not in anchor_idx:
                refs.append({"price": float(p["price"]), "idx": int(p["idx"]),
                             "text": f"{'前高' if p['type'] == 'H' else '前低'} {dates[p['idx']][5:]}"})
        zones = _volume_zones(h, l, c, v, ref_begin, last, ref)
        for g in [g for g in qualified if all(g is not u for u in used)][-OTHER_LEGS:]:
            g_lo, g_hi = sorted((g["a"]["price"], g["b"]["price"]))
            for r in RETRACE:
                refs.append({"price": g_lo + (g_hi - g_lo) * r,
                             "text": f"另一段 {r:g}（{dates[g['a']['idx']][5:]}～{dates[g['b']['idx']][5:]}）"})
        box = find_box(c, atr, last)
        if box:   # 10-27（3034 案例）：整理區等幅測量＝上緣＋整理高度；畫成價位，也當費波的共振對象
            box.update(height=box["top"] - box["bottom"], target=2 * box["top"] - box["bottom"], broke=close > box["top"],
                       start_date=dates[box["start"]], end_date=dates[box["end"]])
            item = {"label": "整理區等幅" if box["broke"] else "整理區等幅（突破上緣後才成立）",
                    "tag": "整理區等幅" if box["broke"] else "等幅（突破後）", "kind": "box", "ratio": 1.0,
                    "price": box["target"], "conditional": not box["broke"]}
            levels.append(item)
            refs.append({"price": box["target"], "text": f"整理區等幅 {box['target']:.2f}", "level": item})
            # 10-28（2881）：整理區上下緣本身也是價位（貼著收盤時就是現價附近觀察位）
            for name, edge in (("上緣", box["top"]), ("下緣", box["bottom"])):
                edge_item = {"label": f"整理區{name}", "tag": f"整理區{name}", "kind": "box_edge", "ratio": 1.0, "price": edge}
                levels.append(edge_item)
                refs.append({"price": edge, "text": f"整理區{name} {edge:.2f}", "level": edge_item})
            box["line"] = (f"整理區：{box['start_date'][5:]}～{box['end_date'][5:]} 收盤 {box['bottom']:.2f}～{box['top']:.2f}"
                           f"（{box['end'] - box['start'] + 1} 根），{'已向上突破' if box['broke'] else '尚未向上突破'}；"
                           f"等幅測量 {box['top']:.2f}＋{box['height']:.2f}＝{box['target']:.2f}")
        selected["box"] = box
        # 10-28：下跌低點還沒確認就看下方支撐（不只最近 5 根創低；橫盤沒再破底不等於低點已確認）
        falling = bool(provisional and provisional["dir"] < 0)
        if falling:
            pa, pb = provisional["a"], provisional["b"]
            mode = "falling"
            head = "還在下跌" if pb["idx"] >= last - 5 else f"下跌後低點還沒確認（{dates[pb['idx']][5:]} 低點之後反彈未達確認門檻）"
            mode_text = (note + "；" if note else "") + (
                f"{head}：{dates[pa['idx']][5:]} 高點 {pa['price']:.2f} 跌到 {pb['price']:.2f}（約 {provisional['amp_atr']:.1f} ATR，"
                f"低點還沒確認）；下方看支撐，反彈先看最近的壓力")
            levels += support_levels(pa)
        test_tol = TEST_ATR * ref
        for item in levels:
            price = item["price"]
            hits = _hits(item, refs, zones, tol)
            watch = abs(price - close) <= test_tol   # 10-28：貼著收盤＝股價正在這條線附近，不是目標價
            # 10-28：價位依賴終點還沒確認的錨點（未確認波段的 B 本身、由它算出的回撤／延伸）＝暫定
            tentative = selected["provisional"] and (item["kind"] in ("retrace", "extend")
                                                     or (item["kind"] == "prior" and abs(price - b["price"]) < 1e-9))
            # 10-28（2881）：整數關卡保留顯示，但不能單獨讓價位取得 AI 資格（遠處延伸很容易剛好靠近整數）
            basis = [t for t, _ in hits if not t.startswith("整數關卡")]
            # 10-28（2881）：收盤之下也要有候選支撐（整理區下緣、有結構共振的回撤），名額在合併成區後再限
            item.update(price=float(price), above=price > close, confluence=[t for t, _ in hits],
                        tight=[t for t, dist in hits if dist <= tight], watch=watch, tentative=bool(tentative),
                        conditional=bool(item.get("conditional")),
                        ai=bool(not watch and (basis or item["kind"] in ("prior", "box", "box_edge", "support"))))
        selected["zones"] = zones
        levels.sort(key=lambda x: x["price"])

        # 10-28：相近價位先合併成一區，名額以區計算（同一區兩種依據不是兩道防線）
        gap = ZONE_ATR * ref
        for item in [x for x in levels if not x["watch"]]:
            last_area = areas[-1] if areas else None
            if (last_area and item["price"] - last_area["members"][-1]["price"] <= gap and item["price"] - last_area["low"] <= 2 * gap
                    and (item["price"] > close) == (last_area["low"] > close)):   # 不跨過收盤
                last_area["members"].append(item)
                last_area["high"] = item["price"]
            else:
                areas.append({"low": item["price"], "high": item["price"], "members": [item]})
        for area in areas:
            area["above"] = area["low"] > close
            area["ai"] = any(x["ai"] for x in area["members"])
        if mode == "extend":
            for area in [z for z in areas if z["above"] and any(x["kind"] == "extend" for x in z["members"])][:EXTEND_AI]:
                area["ai"] = True
        below = sorted([z for z in areas if not z["above"]], key=lambda z: -z["high"])
        if falling:   # 下跌低點未確認：收盤之下最近幾區不論共振直接交給 AI
            for area in below[:SUPPORT_AI]:
                area["ai"] = True
        # 10-28（2881）：所有模式統一——資格、合併、加選之後，上方只留最近 ABOVE_AI 區、下方只留最近 SUPPORT_AI 區；不足就少給
        for area in [z for z in areas if z["above"] and z["ai"]][ABOVE_AI:]:
            area["ai"] = False
        for area in [z for z in below if z["ai"]][SUPPORT_AI:]:
            area["ai"] = False
        for k, area in enumerate(areas):
            area["tentative"] = any(x["tentative"] for x in area["members"])
            area["conditional"] = all(x["conditional"] for x in area["members"])
            for x in area["members"]:
                x["ai"], x["area"] = area["ai"], k
        for item in levels:
            item.setdefault("area", None)

        word = "上漲" if d > 0 else "下跌"
        summary.append(f"選用波段：A {a['price']:.2f}（{dates[a['idx']]}）→ B {b['price']:.2f}（{dates[b['idx']]}），"
                       f"{word} {amp:.2f}（約 {selected['amp_atr']:.1f} ATR）" + ("；後續同向小段已併入" if merged else "")
                       + (f"；C {cpt['price']:.2f}（{dates[cpt['idx']]}）" if cpt else ""))
        if span:
            summary.append(f"大波段背景：起點 A {dates[a['idx']]} {a['price']:.2f} 在圖外（主圖只顯示近 {lookback} 根）；"
                           f"標「大波段」的回撤量的是 A→B 整段，不是近期拉回")
        if box:
            summary.append(box["line"])
        # 10-28（2881）：交給 AI 分三類——目前壓力、突破後條件目標、下方候選支撐
        ups = [z for z in areas if z["ai"] and z["above"]]
        now = [z for z in ups if not z["conditional"]]
        later = [z for z in ups if z["conditional"]]
        downs = sorted([z for z in areas if z["ai"] and not z["above"]], key=lambda z: -z["high"])
        end_word = "低點" if d < 0 else "高點"
        prefix = "交給 AI（區間量測，非費波主段）｜" if fallback else "交給 AI｜"
        head = "反彈壓力" if falling else "目前壓力"
        if any(z["tentative"] for z in now):
            head += f"（依目前{end_word}試算，{end_word}確認前可能調整）"
        summary.append(f"{prefix}{head}：" + ("、".join(_area_text(z) for z in now) or "收盤之上沒有合格價位"))
        if later:
            summary.append(f"{prefix}突破後條件目標（尚未成立）：" + "、".join(_area_text(z) for z in later))
        summary.append(f"{prefix}下方候選支撐：" + ("、".join(_area_text(z) for z in downs) or "收盤之下沒有合格價位"))
        near = [x for x in levels if x["watch"]]
        if near:
            summary.append(f"現價附近的觀察價位（離收盤 ≤ {TEST_ATR:g} ATR，不列為目標，尚待確認支撐／壓力反應）："
                           + "、".join(f"{x['label']} {x['price']:.2f}" for x in near))
    if widened:
        mode_text = f"最近 {NEAR_LOOKBACK} 根找不到可用波段，放寬到約一年；" + mode_text
    summary.insert(0, f"最新收盤 {close:.2f}｜{mode_text}")
    for g in legs:
        g.setdefault("selected", False)

    bars = [{"date": dates[i], "Open": float(o[i]), "High": float(h[i]), "Low": float(l[i]), "Close": float(c[i]),
             "Volume": float(v[i]) if not np.isnan(v[i]) else 0.0} for i in range(len(c))]
    flags = list(adjust_flags.get("F2") or [])
    show = 0 if widened else begin   # 10-28：近段找得到就只顯示近段（A 在圖外時摘要另寫大波段背景）；放寬時顯示整年
    return {"bars": bars, "display_start": show, "last": last, "atr": ref, "close": close, "tolerance": tol,
            "found": found, "widened": widened,
            "minor": [_pivot(p, dates) for p in minor if p["idx"] >= show],
            "major": [_pivot(p, dates) for p in major if p["idx"] >= show],
            "legs": [{"a": _pivot(g["a"], dates), "b": _pivot(g["b"], dates), "dir": g["dir"], "amp": float(g["amp"]),
                      "amp_atr": float(g["amp_atr"]), "status": g["status"], "selected": g["selected"],
                      "fallback": bool(g.get("fallback")), "provisional": bool(g.get("provisional"))} for g in legs],
            "selected": None if selected is None else dict(
                selected, a=_pivot(selected["a"], dates), b=_pivot(selected["b"], dates),
                c=_pivot(selected["c"], dates) if selected["c"] else None, amp=float(selected["amp"]),
                amp_atr=float(selected["amp_atr"])),
            "mode": mode, "levels": levels,
            "areas": [{"low": z["low"], "high": z["high"], "above": z["above"], "ai": z["ai"], "tentative": z["tentative"],
                       "conditional": z["conditional"], "labels": [x["label"] for x in z["members"]]} for z in areas],
            "summary": summary, "flags": flags,
            "params": {"major_atr": kp.ZIGZAG_ATR * MAJOR_MULT, "minor_atr": kp.ZIGZAG_ATR, "leg_atr": LEG_ATR,
                       "lookback": lookback, "near_lookback": NEAR_LOOKBACK, "conf_atr": CONF_ATR,
                       "tight_atr": TIGHT_ATR, "zone_atr": ZONE_ATR}}
