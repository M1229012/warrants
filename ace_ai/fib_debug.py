"""管理員費波驗證圖（測試版 10-27）：只呈現 fib_levels 的同次計算結果，不另外挑點。"""
from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw

import fib_levels
import kline_debug

_COMMAND = re.compile(r"(?:費波|斐波|費氏|費布|fibo?)\s*\w{0,4}?\s*驗證|驗證\s*\w{0,3}?(?:費波|斐波|費氏|費布)", re.IGNORECASE)
_CODE_RE = re.compile(r"(?<![0-9A-Za-z])(?:[0-9]{4,6}[A-Za-z]?|TAIEX|TPEX)(?![0-9A-Za-z])", re.IGNORECASE)
YEAR_PERIOD = "400d"         # 與分點一年日K同一個抓取區間（約 270 個交易日）
YEAR_TTL_SECONDS = 6 * 3600  # 同檔同日只抓一次 FinMind
KIND_COLOR = {"retrace": "#1478B5", "prior": "#C76C00", "extend": "#8044B8", "box": "#2F855A", "support": "#B4232C"}


def is_request(question: str) -> bool:
    q = str(question or "").strip()
    return not re.search(r"不要|不用|別幫|不需要", q) and bool(_COMMAND.search(q))


def parse_codes(question: str, limit: int = 10) -> list[str]:
    """「費波驗證 2330」或一次多檔「費波驗證 2344 1608」，依輸入順序、最多 limit 檔；沒有代號時比對股名。"""
    q = str(question or "").strip()
    if not is_request(q):
        raise ValueError("請說：費波驗證 2330（一次最多10檔）")
    return find_codes(q, "費波驗證 2330", limit)


def find_codes(q: str, example: str, limit: int = 10) -> list[str]:
    """代號依輸入順序去重；沒有代號時比對股名（費波驗證、CDP 驗證共用）。"""
    codes = list(dict.fromkeys(c.upper() for c in _CODE_RE.findall(q)))
    if not codes:
        import warrant_ai_tools as tools
        codes = [str(c).upper() for c, n in tools.get_stock_name_map().items() if len(str(n)) >= 2 and str(n) in q]
    if not codes:
        raise ValueError(f"請指定股票，例如：{example}")
    if len(codes) > limit:
        raise ValueError(f"共 {len(codes)} 檔，每次最多 {limit} 檔，請分批驗證")
    return codes


def load_panel(code: str) -> dict[str, Any]:
    """僅管理員路由呼叫；行情入口與型態驗證相同（只用正式收盤 K），前面再接約一年日K看大結構。"""
    code, name, frame, events = kline_debug.load_closed(code)
    frame, note = extend_history(code, frame, events)
    panel = build_panel(frame, events, code, name)
    if note:
        panel["fib_debug"]["flags"].append(note)
    return panel


def extend_history(code: str, closed: pd.DataFrame, events: dict | None) -> tuple[pd.DataFrame, str]:
    """10-28：另抓約一年日K（與分點一年日K同區間），整段用同一份公司行動、同一個 as_of 還原後，
    只取正式收盤 K 之前的部分接上；同檔同日只抓一次。抓不到就用原本的日K並註明，不擋驗證。"""
    import price_adjustment
    import warrant_ai_tools as tools

    def fetch():
        raw, _, _ = tools.core().fetch_stock_data_yf(code, period=YEAR_PERIOD)
        tools.record_api_event("FinMindData", status=200)
        return tools._formal_daily_frame(tools._drop_invalid_bars(code, raw, "費波一年日K"))

    columns = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in closed.columns]
    try:
        raw = tools._cached(f"fib_year_{code}_{tools.taipei_now():%Y-%m-%d}", YEAR_TTL_SECONDS, fetch)
        if raw is None or raw.empty:
            return closed, "一年日K：來源沒有資料，只用原本的日K"
        raw = raw.copy()
        raw.index = pd.to_datetime(raw.index)
        adjusted = price_adjustment.adjust_shares(raw[[c for c in columns if c in raw.columns]], events,
                                                  as_of=closed.index[-1], allow_jumps=str(code) in tools.get_emerging_codes())
        older = adjusted.loc[adjusted.index < closed.index[0]]
        if older.empty:
            return closed, ""
        merged = pd.concat([older, closed[columns]]).sort_index()
        # 較早的權值事件已在整段還原時套用；記進 attrs，費波的除息還原才不會把它們當成「未還原」
        merged.attrs = dict(closed.attrs, share_adjustments=adjusted.attrs.get("share_adjustments") or [])
        return merged, ""
    except Exception as exc:
        return closed, f"一年日K未接上（{tools.err_text(exc)}），只用原本的日K"


def build_panel(frame: pd.DataFrame, events: dict | None, code: str, name: str = "") -> dict:
    return {"fib_debug": dict(fib_levels.compute(frame, events), stock_code=code, stock_name=name)}


def _spread(items: list[dict], gap: float, low: float, high: float) -> None:
    """右側標籤依 y 由上而下排開，至少相隔 gap；超出底部時整串往上推。"""
    items.sort(key=lambda x: x["y"])
    prev = None
    for item in items:
        item["ly"] = max(item["y"], low) if prev is None else max(item["y"], prev + gap)
        prev = item["ly"]
    if items and prev > high:
        nxt = None
        for item in reversed(items):
            item["ly"] = min(item["ly"], high) if nxt is None else min(item["ly"], nxt - gap)
            nxt = item["ly"]


def render(data: dict) -> Image.Image:
    """Pillow 繪圖，沿用既有字型；畫在高畫布上，最後依實際內容裁切。"""
    from answer_image import font, wrap

    bars = data["bars"]
    start, end = data["display_start"], data["last"]
    visible = bars[start:end + 1]
    if not visible:
        raise ValueError("沒有可顯示的正式日 K")
    sel, levels, params = data.get("selected"), data.get("levels") or [], data["params"]
    width, margin = 1500, 52
    ink, muted, grid = "#1D2B44", "#64748B", "#E7EBF1"
    image = Image.new("RGB", (width, 8000), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=23, color=ink, bold=False):
        draw.text((x, y), str(value), font=font(size, bold), fill=color)

    def tw(value, size=23, bold=False):
        return draw.textlength(str(value), font=font(size, bold))

    text(margin, 25, f"{data['stock_code']} {data.get('stock_name', '')}｜管理員費波驗證", 37, bold=True)
    text(margin, 86, f"正式收盤截至 {bars[end]['date']}｜顯示 {len(visible)} 根／計算 {len(bars)} 根（還原價）｜前一日 ATR20：{data['atr']:.2f}"
                     f"｜共振距離 {data['tolerance']:.2f}", 22, muted)
    text(margin, 120, f"大轉折門檻 {params['major_atr']:g} ATR｜合格波段 ≥ {params['leg_atr']:g} ATR｜終點在最近 {params['lookback']} 根"
                      f"｜確認日當天的轉折不使用", 22, muted)
    x = margin
    for label, color in (("回撤／反彈", KIND_COLOR["retrace"]), ("前波高點", KIND_COLOR["prior"]), ("延伸", KIND_COLOR["extend"]),
                         ("整理區等幅", KIND_COLOR["box"]), ("下跌支撐", KIND_COLOR["support"])):
        draw.line((x, 180, x + 50, 180), fill=color, width=4)
        text(x + 60, 166, label, 21, color)
        x += 60 + tw(label, 21) + 34
    draw.rectangle((x, 170, x + 44, 190), fill="#FFEFC7")
    text(x + 54, 166, "共振區", 21, muted)
    x += 54 + tw("共振區", 21) + 34
    draw.rectangle((x, 170, x + 44, 190), outline=KIND_COLOR["box"], width=2)
    text(x + 54, 166, "整理區", 21, muted)
    text(margin, 202, "實線＝交給 AI｜點線＝交給 AI 但暫定（低點確認後才成立）｜虛線＝不交給 AI｜灰點小轉折、黑圈大轉折", 21, muted)

    left, right, top, bottom = 115, 1215, 255, 755
    step = (right - left) / max(1, len(visible))
    px = lambda i: left + (i - start + 0.5) * step
    prices = [b[k] for b in visible for k in ("Low", "High")] + [lv["price"] for lv in levels]
    low, high = min(prices), max(prices)
    padding = max((high - low) * 0.06, abs(high) * 0.003, 0.01)
    low, high = low - padding, high + padding
    py = lambda p: bottom - (p - low) / (high - low) * (bottom - top)
    for j in range(7):
        value = low + (high - low) * j / 6
        yy = py(value)
        draw.line((left, yy, right, yy), fill=grid)
        label = f"{value:.2f}"
        text(left - 10 - tw(label, 18), yy - 12, label, 18, muted)

    for lv in levels:   # 共振區先畫，K 棒蓋在上面；10-28：只畫交給 AI 的，其餘共振寫在下方表格
        if lv["confluence"] and lv["ai"]:
            draw.rectangle((left, py(lv["price"]) - 5, right, py(lv["price"]) + 5), fill="#FFEFC7")
    box = (sel or {}).get("box")
    if box and box["end"] >= start:   # 整理區：收盤高低框（虛線上下緣），不加文字以免壓到 K 棒
        bx0, bx1 = px(max(start, box["start"])) - step / 2, px(box["end"]) + step / 2
        by0, by1 = py(box["top"]), py(box["bottom"])
        for xx in range(int(bx0), int(bx1), 10):
            for yy in (by0, by1):
                draw.line((xx, yy, min(xx + 6, bx1), yy), fill=KIND_COLOR["box"], width=2)
        for xx in (bx0, bx1):
            draw.line((xx, by0, xx, by1), fill=KIND_COLOR["box"], width=2)

    for i in range(start, end + 1):
        bar = bars[i]
        color = "#DB4949" if bar["Close"] >= bar["Open"] else "#23997E"
        xx, half = px(i), max(1.5, min(7, step * 0.30))
        draw.line((xx, py(bar["High"]), xx, py(bar["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(bar["Open"]), py(bar["Close"])))
        draw.rectangle((xx - half, y1, xx + half, max(y1 + 2, y2)), fill=color)

    x0 = px(max(start, sel["a"]["idx"])) if sel else left
    for lv in levels:
        yy, color = py(lv["price"]), KIND_COLOR[lv["kind"]]
        x1 = left if lv["kind"] == "support" else x0   # 下跌支撐來自更早的結構，整條畫滿才看得到
        if lv["ai"] and lv.get("tentative"):   # 暫定：點線
            xx = x1
            while xx < right:
                draw.line((xx, yy, min(xx + 4, right), yy), fill=color, width=3)
                xx += 9
        elif lv["ai"]:
            draw.line((x1, yy, right, yy), fill=color, width=3)
        else:
            xx = x1
            while xx < right:
                draw.line((xx, yy, min(xx + 12, right), yy), fill=color, width=2)
                xx += 20

    for p in data.get("minor") or []:
        if start <= p["idx"] <= end:
            xx, yy = px(p["idx"]), py(p["price"])
            draw.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill="#9AA4B2")
    for p in data.get("major") or []:
        if start <= p["idx"] <= end:
            xx, yy = px(p["idx"]), py(p["price"])
            draw.ellipse((xx - 7, yy - 7, xx + 7, yy + 7), outline=ink, width=2)
    if sel:
        points = [(k, sel[k]) for k in ("a", "b", "c") if sel.get(k)]
        for (_, p), (_, q) in zip(points, points[1:]):
            draw.line((px(p["idx"]), py(p["price"]), px(q["idx"]), py(q["price"])), fill="#8A94A6", width=2)
        for key, p in points:
            if not start <= p["idx"] <= end:
                continue
            xx, yy = px(p["idx"]), py(p["price"])
            draw.ellipse((xx - 10, yy - 10, xx + 10, yy + 10), outline="#C2410C", width=4)
            label = key.upper()
            ly = yy - 42 if p["type"] == "H" else yy + 14
            cx = min(max(xx, left + 16), right - 16)   # 10-28：最後幾根的錨點字框不能伸進右側價位標籤
            draw.rectangle((cx - 14, ly, cx + 14, ly + 28), fill="#FFFFFF", outline="#C2410C")
            text(cx - tw(label, 21, True) / 2, ly + 1, label, 21, "#C2410C", True)

    tags = [{"y": py(lv["price"]), "text": f"{lv.get('tag') or lv['label'].replace('前波高點', '前高').replace(' ', '')}｜{lv['price']:.2f}"
                                           + (" 測試中" if lv.get("testing") else " 暫定" if lv["ai"] and lv.get("tentative") else ""),
             "color": KIND_COLOR[lv["kind"]],
             "bold": lv["ai"]} for lv in levels]
    tags.append({"y": py(data["close"]), "text": f"收盤｜{data['close']:.2f}", "color": ink, "bold": True})
    _spread(tags, 25, top - 10, bottom)
    for tag in tags:
        if abs(tag["ly"] - tag["y"]) > 2:
            draw.line((right + 4, tag["y"], right + 14, tag["ly"]), fill=tag["color"], width=1)
        text(right + 16, tag["ly"] - 12, tag["text"], 19, tag["color"], tag["bold"])   # 與最後一根的錨點圓圈留空隙

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

    def table(title, columns, rows):
        nonlocal y
        y += 22
        text(margin, y, title, 23, bold=True)
        y += 40
        draw.rectangle((margin, y, width - margin, y + 39), fill="#EDF1F7")
        for cx, label, _ in columns:
            text(cx, y + 4, label, 21, bold=True)
        y += 44
        if not rows:
            text(margin + 12, y, "（無）", 21, muted)
            y += 35
        for values, color in rows:
            cells = [wrap(str(v), 20, cw) for (_, _, cw), v in zip(columns, values)]
            for (cx, _, _), lines in zip(columns, cells):
                for k, line in enumerate(lines):
                    text(cx, y + k * 28, line, 20, color)
            y += max(len(c) for c in cells) * 28 + 8

    lw = lambda p: f"{p['date'][5:]} {p['price']:.2f}"
    leg_rows = []
    for g in (data.get("legs") or [])[-10:]:
        if g["selected"]:
            note = "選用" + (f"（後續併入，B 移到 {sel['b']['date'][5:]}）" if sel and sel.get("merged") else "") \
                + ("（備援：回看區間最低／最高點，不是大轉折）" if g.get("fallback") else "") \
                + ("（終點未確認：最後一個大轉折到目前極值）" if g.get("provisional") else "")
        else:
            note = g["status"] or "合格（較舊，未選用）"
        leg_rows.append(([lw(g["a"]), lw(g["b"]), "上漲" if g["dir"] > 0 else "下跌", f"{g['amp_atr']:.1f}", note],
                         ink if g["selected"] else muted))
    table("大級別波段（最近 10 段）｜沒選上的寫出原因",
          [(margin + 12, "起點", 230), (300, "終點", 230), (545, "方向", 90), (650, "幅度 ATR", 130), (800, "狀態", 640)], leg_rows)
    level_rows = [([lv["label"], f"{lv['price']:.2f}",
                    "正在測試" if lv.get("testing") else "收盤之上" if lv["above"] else "收盤之下",
                    "、".join(lv["confluence"]) or "無",
                    ("是（暫定）" if lv.get("tentative") else "是") if lv["ai"] else "否"], ink if lv["ai"] else muted)
                  for lv in sorted(levels, key=lambda x: -x["price"])]
    table("費波價位｜共振＝距其他結構在共振距離內",
          [(margin + 12, "價位", 330), (410, "價格", 130), (550, "位置", 130), (690, "共振對象", 620), (1330, "交給 AI", 110)], level_rows)

    y += 20
    text(margin, y, "僅管理員驗證｜忠實呈現目前演算法，找不到合格波段時不補畫｜費波為技術參考，不代表預測能力或買賣建議", 20, muted)
    return image.crop((0, 0, width, int(y + 50)))
