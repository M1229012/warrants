"""費波南西參考價（測試版 10-27）：沿用 kline_patterns 的還原價、ATR 與 ZigZag，選大級別波段算回撤／延伸價位，
再找前高低、大量區、整數關卡與其他波段費波的共振。純計算、不打 API；驗證圖與之後的 AI 解讀共用這一份結果。

錨點規則（寫死，同一檔同一天一定抓同一組點）：
  1. 大級別轉折＝ZigZag 門檻放大 MAJOR_MULT 倍；確認日當天不用（與型態驗證一致）。
  2. 回看 LOOKBACK 根內、幅度 ≥ LEG_ATR 倍 ATR 的最近一段＝選用波段 A→B；後續同向小段創更高（低）時併入，B 往後移。
  3. 上漲段：B 回撤 0.382／0.5／0.618 為下方支撐；B 之後已確認的回檔低點 C 存在時，C＋(B−A)×1.0／1.618 為延伸目標。
     下跌段：B＋(A−B)×0.382／0.5／0.618 為反彈參考，A 為前波高點。
  4. 價位落在其他結構 CONF_ATR 倍 ATR 內＝共振；只有「在收盤之上且有共振」的費波價位（及前波高點）才交給 AI。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import kline_patterns as kp


def _env(name: str, default: float) -> float:
    return kp._env("FIB_" + name, default)   # 環境變數 DISCORD_AI_KLINE_FIB_*


MAJOR_MULT = _env("MAJOR_MULT", 2.5)      # 大級別轉折門檻＝ZIGZAG_ATR × 此倍數
LOOKBACK = int(_env("LOOKBACK", 120))     # 只在最近 N 根內找波段
LEG_ATR = _env("LEG_ATR", 6.0)            # 合格波段幅度 ≥ 此倍數 ATR（波段終點當日的前一日 ATR）
CONF_ATR = _env("CONF_ATR", 1.0)          # 共振距離：目前 ATR 的倍數
OTHER_LEGS = int(_env("OTHER_LEGS", 2))   # 另外拿最近幾段合格波段的回撤價做群聚比對
VOL_BIN_ATR = _env("VOL_BIN_ATR", 0.5)    # 大量區價格格寬（ATR 倍數）
VOL_TOP = int(_env("VOL_TOP", 3))         # 最多取幾個大量區
VOL_MIN_RATIO = _env("VOL_MIN_RATIO", 1.5)   # 大量區成交量 ≥ 平均格的倍數
RETRACE = (0.382, 0.5, 0.618)
EXTEND = (1.0, 1.618)


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


def compute(frame: pd.DataFrame, events: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    need = ["Open", "High", "Low", "Close"]
    if frame is None or not set(need) <= set(frame.columns):
        raise ValueError("日 K 欄位不足，無法計算費波")
    df = frame.dropna(subset=need).sort_index()
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
    begin = max(0, last - LOOKBACK + 1)
    tol = CONF_ATR * ref
    minor = [p for p in kp.zigzag(h, l, c, atr) if p["confirm"] < last]
    major = [p for p in kp.zigzag(h, l, c, atr * MAJOR_MULT) if p["confirm"] < last]

    legs = []
    for a, b in zip(major, major[1:]):
        if b["idx"] < begin:
            continue
        unit = atr[b["idx"]] if not np.isnan(atr[b["idx"]]) else ref
        amp = abs(b["price"] - a["price"])
        status = (f"起點早於回看 {LOOKBACK} 根" if a["idx"] < begin else
                  f"幅度 {amp / unit:.1f} ATR，未達 {LEG_ATR:g} ATR" if amp / unit < LEG_ATR else "")
        legs.append({"a": a, "b": b, "dir": 1 if b["type"] == "H" else -1, "amp": amp,
                     "amp_atr": amp / unit, "status": status})
    qualified = [g for g in legs if not g["status"]]

    selected, levels, summary = None, [], []
    mode, mode_text = "none", f"最近 {LOOKBACK} 根沒有 ≥{LEG_ATR:g} ATR 的大級別波段，不給費波價位"
    if qualified:
        leg = qualified[-1]
        used = [leg]
        k = legs.index(leg)
        prev = legs[k - 1] if k else None
        if leg["dir"] < 0 and prev and not prev["status"] and prev["b"] is leg["a"] and leg["b"]["price"] > prev["a"]["price"]:
            leg["status"] = "合格；守住起點，視為上漲段的回檔 B→C"
            leg = prev   # 大回檔但守住起點＝上漲段的 A→B→C，仍以上漲段為主（才算得出延伸）
            used.append(leg)
        d, a, b = leg["dir"], leg["a"], leg["b"]
        later = [p for p in major if p["idx"] > b["idx"]]
        broken, merged = False, False
        for p in later:   # 同向小段創更高（低）＝波段延續，B 往後移；反向越過 A＝波段結束
            if p["type"] == a["type"] and (p["price"] - a["price"]) * d < 0:
                broken = True
                break
            if p["type"] == b["type"] and (p["price"] - b["price"]) * d > 0:
                b, merged = p, True
        after = [p for p in major if p["idx"] > b["idx"] and p["type"] == a["type"]]
        cpt = (min(after, key=lambda p: p["price"]) if d > 0 else max(after, key=lambda p: p["price"])) if after and not broken else None
        amp = abs(b["price"] - a["price"])
        lo, hi = min(a["price"], b["price"]), max(a["price"], b["price"])
        leg["selected"] = True
        unit = atr[b["idx"]] if not np.isnan(atr[b["idx"]]) else ref
        selected = {"a": a, "b": b, "c": cpt, "dir": d, "amp": amp, "amp_atr": amp / unit,
                    "merged": merged, "broken": broken}
        if d > 0:
            for r in RETRACE:
                levels.append({"label": f"回撤 {r:g}", "kind": "retrace", "ratio": r, "price": hi - amp * r})
            levels.append({"label": "前波高點 B", "kind": "prior", "ratio": 1.0, "price": hi})
            if cpt and close < hi:   # 還在 B 之下：C→B 的反彈價位也是上方參考
                for r in RETRACE:
                    levels.append({"label": f"反彈 {r:g}", "kind": "retrace", "ratio": r,
                                   "price": cpt["price"] + (hi - cpt["price"]) * r})
            if cpt:
                for r in EXTEND:
                    levels.append({"label": f"延伸 {r:g}", "kind": "extend", "ratio": r, "price": cpt["price"] + amp * r})
            if close > hi:
                mode, mode_text = "extend", "收盤高於波段高點 B（創波段新高），上方看延伸價位"
            else:
                mode, mode_text = "pullback", "上漲波段後拉回，上方先看前波高點 B，站上後看延伸價位"
            if not cpt and not broken:
                mode_text += "；B 之後的回檔低點 C 尚未確認，延伸價位暫不計算"
        else:
            for r in RETRACE:
                levels.append({"label": f"反彈 {r:g}", "kind": "retrace", "ratio": r, "price": lo + amp * r})
            levels.append({"label": "前波高點 A", "kind": "prior", "ratio": 1.0, "price": hi})
            mode, mode_text = "rebound", "下跌波段後反彈，上方看下跌段的回撤反彈價位與前波高點 A"
        if broken:
            mode_text += "；後續已反向越過波段起點 A，此段僅供參考"

        refs = []
        anchor_idx = {a["idx"], b["idx"]} | ({cpt["idx"]} if cpt else set())
        for p in minor:
            if p["idx"] >= begin and p["idx"] not in anchor_idx:
                refs.append({"price": float(p["price"]), "text": f"{'前高' if p['type'] == 'H' else '前低'} {dates[p['idx']][5:]}"})
        zones = _volume_zones(h, l, c, v, begin, last, ref)
        for g in [g for g in qualified if all(g is not u for u in used)][-OTHER_LEGS:]:
            g_lo, g_hi = sorted((g["a"]["price"], g["b"]["price"]))
            for r in RETRACE:
                refs.append({"price": g_lo + (g_hi - g_lo) * r,
                             "text": f"另一段 {r:g}（{dates[g['a']['idx']][5:]}～{dates[g['b']['idx']][5:]}）"})
        for item in levels:
            price = item["price"]
            hits = [x["text"] for x in refs if abs(x["price"] - price) <= tol]
            hits += [f"大量區 {z['low']:.2f}～{z['high']:.2f}" for z in zones
                     if abs(z["price"] - price) <= tol + (z["high"] - z["low"]) / 2]
            step = round_step(price)
            nearest = round(price / step) * step
            if nearest > 0 and abs(nearest - price) <= tol:
                hits.append(f"整數關卡 {nearest:g}")
            item.update(price=float(price), above=price > close, confluence=hits,
                        ai=bool(price > close and (hits or item["kind"] == "prior")))
        selected["zones"] = zones
        levels.sort(key=lambda x: x["price"])
        word = "上漲" if d > 0 else "下跌"
        summary.append(f"選用波段：A {a['price']:.2f}（{dates[a['idx']]}）→ B {b['price']:.2f}（{dates[b['idx']]}），"
                       f"{word} {amp:.2f}（約 {selected['amp_atr']:.1f} ATR）" + ("；後續同向小段已併入" if merged else "")
                       + (f"；C {cpt['price']:.2f}（{dates[cpt['idx']]}）" if cpt else ""))
        picks = [x for x in levels if x["ai"]]
        summary.append("交給 AI 的上方參考：" + ("、".join(f"{x['label']} {x['price']:.2f}" for x in picks) if picks
                                                 else "沒有在收盤之上且有共振的價位，AI 不提費波參考價"))
    summary.insert(0, f"最新收盤 {close:.2f}｜{mode_text}")
    for g in legs:
        g.setdefault("selected", False)

    bars = [{"date": dates[i], "Open": float(o[i]), "High": float(h[i]), "Low": float(l[i]), "Close": float(c[i]),
             "Volume": float(v[i]) if not np.isnan(v[i]) else 0.0} for i in range(len(c))]
    flags = list(adjust_flags.get("F2") or [])
    return {"bars": bars, "display_start": begin, "last": last, "atr": ref, "close": close, "tolerance": tol,
            "minor": [_pivot(p, dates) for p in minor if p["idx"] >= begin],
            "major": [_pivot(p, dates) for p in major if p["idx"] >= begin],
            "legs": [{"a": _pivot(g["a"], dates), "b": _pivot(g["b"], dates), "dir": g["dir"], "amp": float(g["amp"]),
                      "amp_atr": float(g["amp_atr"]), "status": g["status"], "selected": g["selected"]} for g in legs],
            "selected": None if selected is None else dict(
                selected, a=_pivot(selected["a"], dates), b=_pivot(selected["b"], dates),
                c=_pivot(selected["c"], dates) if selected["c"] else None, amp=float(selected["amp"]),
                amp_atr=float(selected["amp_atr"])),
            "mode": mode, "levels": levels, "summary": summary, "flags": flags,
            "params": {"major_atr": kp.ZIGZAG_ATR * MAJOR_MULT, "minor_atr": kp.ZIGZAG_ATR, "leg_atr": LEG_ATR,
                       "lookback": LOOKBACK, "conf_atr": CONF_ATR}}
