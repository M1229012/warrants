"""管理員 CDP 驗證圖（測試版 10-27）：逐日用前一日高低收算 CDP 五價，畫在 K 線上並統計觸及與反應，供和費波驗證比較。

CDP＝(高＋低＋2×收)／4；AH＝CDP＋(高−低)、NH＝2×CDP−低、NL＝2×CDP−高、AL＝CDP−(高−低)。
第 i 根算出的五價給第 i＋1 個交易日用；價格沿用型態驗證的還原價（除息日前一日已還原，避免除息缺口算錯）。
統計口徑：上方價位（AH／NH）最高價 ≥ 價位＝觸及，觸及後收盤仍在價位之下＝有反應；下方價位（NL／AL）反之。
"""
from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw

import fib_debug
import kline_debug
import kline_patterns as kp

SHOW_DAYS = int(kp._env("CDP_SHOW_DAYS", 60))      # 圖上顯示幾根
STATS_DAYS = int(kp._env("CDP_STATS_DAYS", 120))   # 統計最近幾個交易日
NAMES = ("AH", "NH", "CDP", "NL", "AL")
COLOR = {"AH": "#C2410C", "NH": "#E08A1E", "CDP": "#87909E", "NL": "#2E8BC0", "AL": "#1F4E9C"}
LABEL = {"AH": "AH 最高值", "NH": "NH 近高值", "CDP": "CDP 中價", "NL": "NL 近低值", "AL": "AL 最低值"}
_COMMAND = re.compile(r"CDP\s*\w{0,4}?\s*驗證|驗證\s*CDP|逆勢操作\s*\w{0,2}?\s*驗證", re.IGNORECASE)


def is_request(question: str) -> bool:
    q = str(question or "").strip()
    return not re.search(r"不要|不用|別幫|不需要", q) and bool(_COMMAND.search(q))


def parse_codes(question: str, limit: int = 10) -> list[str]:
    q = str(question or "").strip()
    if not is_request(q):
        raise ValueError("請說：CDP驗證 2330（一次最多10檔）")
    return fib_debug.find_codes(q, "CDP驗證 2330", limit)


def five_prices(h, l, c) -> dict[str, np.ndarray]:
    cdp = (h + l + 2 * c) / 4
    rng = h - l
    return {"AH": cdp + rng, "NH": 2 * cdp - l, "CDP": cdp, "NL": 2 * cdp - h, "AL": cdp - rng}


def compute(frame: pd.DataFrame, events: dict | None = None) -> dict[str, Any]:
    need = ["Open", "High", "Low", "Close"]
    if frame is None or not set(need) <= set(frame.columns):
        raise ValueError("日 K 欄位不足，無法計算 CDP")
    df = frame.dropna(subset=need).sort_index()
    if len(df) < 3:
        raise ValueError(f"日 K 只有 {len(df)} 根，無法計算 CDP")
    adj, adjust_flags = kp.adjust(df, events)
    o, h, l, c = (adj[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    v = adj["Volume"].to_numpy(dtype=float) if "Volume" in adj else np.zeros(len(c))
    dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in adj.index]
    last = len(c) - 1
    lv = five_prices(h, l, c)

    begin = max(1, last - STATS_DAYS + 1)
    stats = []
    for name, side in (("AH", 1), ("NH", 1), ("NL", -1), ("AL", -1)):
        ref = lv[name][begin - 1:last]     # 第 d 日用 d−1 日算出的價位
        hi, lo, cl, op = h[begin:last + 1], l[begin:last + 1], c[begin:last + 1], o[begin:last + 1]
        touch = hi >= ref if side > 0 else lo <= ref
        held = touch & ((cl < ref) if side > 0 else (cl > ref))
        gap = op > ref if side > 0 else op < ref
        stats.append({"name": name, "days": int(len(ref)), "touch": int(touch.sum()), "held": int(held.sum()),
                      "gap": int(gap.sum())})

    start = max(1, last - SHOW_DAYS + 1)
    nxt = {k: float(lv[k][last]) for k in NAMES}
    close = float(c[last])
    above = [k for k in NAMES if nxt[k] > close]
    where = (f"介於 {above[-1]} {nxt[above[-1]]:.2f} 與 {NAMES[len(above)]} {nxt[NAMES[len(above)]]:.2f} 之間"
             if 0 < len(above) < len(NAMES) else "高於 AH" if not above else "低於 AL")
    summary = [f"下一交易日 CDP 五價（用 {dates[last]} 高低收）：" + "｜".join(f"{k} {nxt[k]:.2f}" for k in NAMES),
               f"最新收盤 {close:.2f}，{where}；CDP 每天換一次，只代表下一個交易日的價位",
               f"近 {stats[0]['days']} 日：" + "；".join(
                   f"{s['name']} 觸及 {s['touch']} 天、觸及後收回 {s['held']} 天" for s in stats)]
    bars = [{"date": dates[i], "Open": float(o[i]), "High": float(h[i]), "Low": float(l[i]), "Close": float(c[i]),
             "Volume": float(v[i]) if not np.isnan(v[i]) else 0.0} for i in range(len(c))]
    return {"bars": bars, "display_start": start, "last": last, "close": close, "next": nxt,
            "daily": {k: [float(lv[k][i - 1]) for i in range(start, last + 1)] for k in NAMES},
            "stats": stats, "summary": summary, "flags": list(adjust_flags.get("F2") or []),
            "params": {"show_days": SHOW_DAYS, "stats_days": STATS_DAYS}}


def load_panel(code: str) -> dict[str, Any]:
    """僅管理員路由呼叫；行情入口與型態驗證相同（只用正式收盤 K）。"""
    code, name, frame, events = kline_debug.load_closed(code)
    return build_panel(frame, events, code, name)


def build_panel(frame: pd.DataFrame, events: dict | None, code: str, name: str = "") -> dict:
    return {"cdp_debug": dict(compute(frame, events), stock_code=code, stock_name=name)}


def render(data: dict) -> Image.Image:
    """Pillow 繪圖；每根 K 棒疊當日適用的五價短線，右側空一格畫下一交易日的五價。"""
    from answer_image import font, wrap

    bars = data["bars"]
    start, end = data["display_start"], data["last"]
    visible = bars[start:end + 1]
    width, margin = 1500, 52
    ink, muted, grid = "#1D2B44", "#64748B", "#E7EBF1"
    image = Image.new("RGB", (width, 6000), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=23, color=ink, bold=False):
        draw.text((x, y), str(value), font=font(size, bold), fill=color)

    def tw(value, size=23, bold=False):
        return draw.textlength(str(value), font=font(size, bold))

    text(margin, 25, f"{data['stock_code']} {data.get('stock_name', '')}｜管理員 CDP 驗證", 37, bold=True)
    text(margin, 86, f"正式收盤截至 {bars[end]['date']}｜顯示 {len(visible)} 根（還原價）｜統計最近 {data['stats'][0]['days']} 個交易日", 22, muted)
    text(margin, 120, "每根 K 棒上的短線＝當天適用的五價（前一日高低收算出）｜最右虛框＝下一交易日", 22, muted)
    x = margin
    for name in NAMES:
        draw.line((x, 180, x + 44, 180), fill=COLOR[name], width=4)
        text(x + 54, 166, LABEL[name], 21, COLOR[name])
        x += 54 + tw(LABEL[name], 21) + 34

    left, right, top, bottom = 115, 1150, 225, 755
    slots = len(visible) + 2                       # 空一格再畫下一交易日
    step = (right - left) / slots
    px = lambda i: left + (i - start + 0.5) * step
    nx = px(end + 2)
    prices = [b[k] for b in visible for k in ("Low", "High")] + list(data["next"].values())
    for values in data["daily"].values():
        prices += values
    low, high = min(prices), max(prices)
    padding = max((high - low) * 0.05, abs(high) * 0.003, 0.01)
    low, high = low - padding, high + padding
    py = lambda p: bottom - (p - low) / (high - low) * (bottom - top)
    for j in range(7):
        value = low + (high - low) * j / 6
        yy = py(value)
        draw.line((left, yy, right, yy), fill=grid)
        label = f"{value:.2f}"
        text(left - 10 - tw(label, 18), yy - 12, label, 18, muted)

    half = max(1.5, min(7, step * 0.30))
    for k, i in enumerate(range(start, end + 1)):
        bar = bars[i]
        color = "#DB4949" if bar["Close"] >= bar["Open"] else "#23997E"
        xx = px(i)
        draw.line((xx, py(bar["High"]), xx, py(bar["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(bar["Open"]), py(bar["Close"])))
        draw.rectangle((xx - half, y1, xx + half, max(y1 + 2, y2)), fill=color)
        for name in NAMES:
            yy = py(data["daily"][name][k])
            draw.line((xx - step * 0.48, yy, xx + step * 0.48, yy), fill=COLOR[name], width=1 if name == "CDP" else 2)
    box = (nx - step * 0.9, py(data["next"]["AH"]) - 8, nx + step * 0.9, py(data["next"]["AL"]) + 8)
    for xx in range(int(box[0]), int(box[2]), 8):
        draw.line((xx, box[1], xx + 4, box[1]), fill=muted)
        draw.line((xx, box[3], xx + 4, box[3]), fill=muted)
    for name in NAMES:
        yy = py(data["next"][name])
        draw.line((nx - step * 0.8, yy, nx + step * 0.8, yy), fill=COLOR[name], width=3)
    text(nx - tw("次日", 18) / 2, box[3] + 6, "次日", 18, muted)

    tags = [{"y": py(data["next"][n]), "text": f"{LABEL[n]}｜{data['next'][n]:.2f}", "color": COLOR[n]} for n in NAMES]
    tags.append({"y": py(data["close"]), "text": f"收盤｜{data['close']:.2f}", "color": ink})
    fib_debug._spread(tags, 25, top - 10, bottom)
    lx = right + 45
    for tag in tags:
        draw.line((nx + step * 0.9, tag["y"], lx - 6, tag["ly"]), fill=tag["color"], width=1)
        text(lx, tag["ly"] - 12, tag["text"], 19, tag["color"], True)

    tick_gap = max(1, math.ceil(len(visible) / 9))
    for i in range(start, end + 1, tick_gap):
        text(px(i) - 24, 779, bars[i]["date"][5:], 19, muted)
    max_vol = max([max(0, b["Volume"]) for b in visible] + [1.0])
    text(margin, 817, "成交量", 20, muted)
    for i in range(start, end + 1):
        bar = bars[i]
        xx, hh = px(i), max(0, bar["Volume"]) / max_vol * 83
        draw.rectangle((xx - step * 0.28, 935 - hh, xx + step * 0.28, 935), fill="#DB4949" if bar["Close"] >= bar["Open"] else "#23997E")
    draw.line((margin, 955, width - margin, 955), fill=grid, width=2)

    y = 980
    for value in data.get("summary") or []:
        for line in wrap(value, 23, width - 2 * margin):
            text(margin, y, line, 23)
            y += 33
    for value in data.get("flags") or []:
        for line in wrap("資料旗標：" + str(value), 21, width - 2 * margin):
            text(margin, y, line, 21, "#9A5B13")
            y += 30

    y += 22
    text(margin, y, "觸及與反應統計｜上方價位：最高價碰到＝觸及、收盤仍在下方＝收回；下方價位反之", 23, bold=True)
    y += 40
    columns = [(margin + 12, "價位"), (250, "統計天數"), (420, "觸及天數"), (590, "觸及率"), (750, "觸及後收回"),
               (950, "收回率"), (1110, "開盤直接越過")]
    draw.rectangle((margin, y, width - margin, y + 39), fill="#EDF1F7")
    for cx, label in columns:
        text(cx, y + 4, label, 21, bold=True)
    y += 44
    pct = lambda a, b: f"{a / b * 100:.0f}%" if b else "—"
    for s in data["stats"]:
        values = [LABEL[s["name"]], s["days"], s["touch"], pct(s["touch"], s["days"]), s["held"],
                  pct(s["held"], s["touch"]), s["gap"]]
        for (cx, _), value in zip(columns, values):
            text(cx, y, value, 20, COLOR[s["name"]] if cx == margin + 12 else ink)
        y += 35
    y += 20
    text(margin, y, "僅管理員驗證｜CDP 為隔日逆勢參考，每天重算；統計描述過去，不代表預測能力或買賣建議", 20, muted)
    return image.crop((0, 0, width, int(y + 50)))
