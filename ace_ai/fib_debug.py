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
KIND_COLOR = {"retrace": "#1478B5", "prior": "#C76C00", "extend": "#8044B8", "box": "#2F855A", "box_edge": "#2F855A",
              "support": "#B4232C"}


_FIB_WORD = re.compile(r"費波|斐波|費氏|費布|fibo|黃金切割", re.IGNORECASE)
_NEGATED = re.compile(r"不要|不用|別幫|不需要")


def mentions_fib(question: str) -> bool:
    q = str(question or "")
    return bool(_FIB_WORD.search(q)) and not _NEGATED.search(q)


def is_request(question: str) -> bool:
    """11-03：「幫我驗證一下台指期的費波」這種白話也算——有費波字眼、句子裡有「驗證」就是驗證圖（不必連在一起）。"""
    q = str(question or "").strip()
    return not _NEGATED.search(q) and bool(_COMMAND.search(q) or (_FIB_WORD.search(q) and "驗證" in q))


def rule_codes(question: str, limit: int = 10) -> list[str]:
    """不用 AI 就看得出的標的：台指期名稱、股票代號、股名；看不出回空清單。"""
    import taifex_futures
    q = str(question or "")
    out = []
    if taifex_futures.is_futures(q):   # 11-03：台指期＝台指期近月自己算（不是加權現貨）
        out.append(taifex_futures.CODE)
        q = taifex_futures._CODE_RE.sub(" ", q)
    try:
        out += find_codes(q, "費波驗證 2330", limit - len(out))
    except ValueError as exc:
        if "最多" in str(exc):
            raise
    return out


_TARGET_SCHEMA = {"type": "object", "properties": {"targets": {"type": "array", "items": {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["futures", "taiex", "tpex", "stock", "none"]}, "stock": {"type": "string"}},
    "required": ["kind", "stock"]}}}, "required": ["targets"]}


def ai_codes(question: str, ask_ai, limit: int = 10) -> list[str]:
    """11-03 使用者：任何白話都要能用（「夜盤的費波位置」）→ 規則看不出標的時讓 AI 讀整句，不補關鍵字。
    ask_ai(prompt, schema) 回 dict 或 None。"""
    import taifex_futures
    prompt = ("你是台股問句解析器，只輸出 JSON，不要回答問題。使用者想看費波南西（費波）價位，讀完整句判斷要看哪些標的，依句中順序列出：\n"
              "futures＝台指期（台股期貨、期指；日盤、夜盤、大台、小台、微台都算）；taiex＝加權指數（大盤、現貨）；"
              "tpex＝櫃買指數；stock＝個股，stock 欄寫代號或名稱；其餘 stock 欄留空；看不出標的寫 none。\n問題：" + question)
    out = ask_ai(prompt, _TARGET_SCHEMA) or {}
    codes = []
    fixed = {"futures": taifex_futures.CODE, "taiex": "TAIEX", "tpex": "TPEX"}
    for item in out.get("targets") or []:
        kind = str(item.get("kind") or "")
        if kind in fixed:
            codes.append(fixed[kind])
        elif kind == "stock" and str(item.get("stock") or "").strip():
            try:
                codes += find_codes(str(item["stock"]), "費波驗證 2330", limit)
            except ValueError:
                continue
    return list(dict.fromkeys(codes))[:limit]


def parse_codes(question: str, limit: int = 10, ask_ai=None) -> list[str]:
    """「費波驗證 2330」或一次多檔「費波驗證 2344 1608」，依輸入順序、最多 limit 檔；沒有代號時比對股名。
    規則看不出標的、又有 ask_ai 時，讓 AI 讀整句（夜盤、期貨…白話問法）。"""
    q = str(question or "").strip()
    if not is_request(q):
        raise ValueError("請說：費波驗證 2330（一次最多10檔）")
    codes = rule_codes(q, limit)
    if not codes and ask_ai is not None:
        codes = ai_codes(q, ask_ai, limit)
    if not codes:
        raise ValueError("請指定股票，例如：費波驗證 2330、費波驗證 台指期")
    return codes


def futures_request(question: str, ask_ai) -> list[str]:
    """11-03：沒說「驗證」的白話（「台指期費波在哪」「夜盤費波位置」）——標的是台指期才走驗證圖，回代號清單；
    個股、大盤照舊走一般回答（回空清單）。句子裡有股票代號或股名就不問 AI。"""
    if not mentions_fib(question) or is_request(question):
        return []
    import taifex_futures
    try:
        codes = rule_codes(question)
        if not codes and ask_ai is not None:
            codes = ai_codes(question, ask_ai)
    except ValueError:       # 一次超過 10 檔等：不是這條路，交給後面的流程回覆
        return []
    return codes if taifex_futures.CODE in codes else []


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
    import taifex_futures
    if code == taifex_futures.CODE:
        return futures_panel()
    code, name, frame, events = kline_debug.load_closed(code)
    last_closed = f"{pd.Timestamp(frame.index[-1]):%Y-%m-%d}"
    frame, note = extend_history(code, frame, events)
    panel = build_panel(frame, events, code, name)
    if note:
        panel["fib_debug"]["flags"].append(note)
    attach_live(panel["fib_debug"], live_bar(code, last_closed))
    return panel


def futures_panel() -> dict[str, Any]:
    """11-03 使用者：台指期獨立一張——用台指期近月連續日 K（前一晚夜盤＋當天日盤）自己找錨點、算價位，
    和加權現貨的費波不混用（有正逆價差）；最右邊那根是奇摩即時價（日盤或夜盤），只顯示、不計算。"""
    import taifex_futures
    frame = taifex_futures.daily_frame()
    panel = build_panel(frame, None, taifex_futures.CODE, taifex_futures.NAME)
    data = panel["fib_debug"]
    data["flags"].append("台指期近月連續：每天一根＝前一晚夜盤＋當天日盤；換月不還原價差"
                         + (f"（最近換月 {frame.attrs['rolls'][-1][5:]}）" if frame.attrs.get("rolls") else ""))
    attach_live(data, taifex_futures.live_bar(frame))
    return panel


def attach_live(data: dict[str, Any], live: dict[str, Any] | None) -> None:
    """最右邊那根只畫在圖上；錨點、價位、ATR 都是 build_panel 用正式收盤 K 算好的。"""
    if not live:
        return
    data["live_bar"] = live
    # 11-03 使用者：盤中已經跌到 0.618 附近就要看得到 0.786（正式收盤還沒到也標；只畫線，不交給 AI）
    deep = fib_levels.live_deep_retrace(data, live["Close"])
    if deep:
        data["levels"].append(deep)
        data["summary"].append(f"次要參考（{_live_state(live)} {live['Close']:.2f} 已到 0.618 附近或之下才標，只畫在圖上、不交給 AI）："
                               f"{deep['label']} {deep['price']:.2f}")


def _live_state(live: dict[str, Any]) -> str:
    """股票：盤中／盤後暫定；台指期：日盤／夜盤（加成交時間）。"""
    label = live.get("label") or ("盤中" if live.get("is_live") else "盤後暫定")
    return f"{label} {live.get('time') or ''}".strip()


def live_bar(code: str, last_closed: str) -> dict[str, Any] | None:
    """11-03 使用者：驗證圖要看得到盤中 K 棒，但費波不拿它計算。
    和 load_closed 同一份價格資料（_load_price_bundle 有快取，不另外打 API）；只有今天還沒正式收盤時才有這一根。"""
    import warrant_ai_tools as tools
    try:
        bundle = tools._load_price_bundle(code)
    except Exception as exc:
        print(f"⚠️ 費波驗證：盤中 K 棒略過｜{code}｜{tools.err_text(exc)}", flush=True)
        return None
    info, df = bundle.get("intraday") or {}, bundle.get("df")
    if not info or info.get("is_close_confirmed") or df is None or df.empty:
        return None
    day = f"{pd.Timestamp(df.index[-1]):%Y-%m-%d}"
    if day <= last_closed:
        return None
    row = df.iloc[-1]
    volume = row.get("Volume")
    return {"date": day, "Open": float(row["Open"]), "High": float(row["High"]), "Low": float(row["Low"]),
            "Close": float(row["Close"]), "Volume": float(volume) if pd.notna(volume) else 0.0,
            "time": str(info.get("time") or "")[:5], "is_live": bool(info.get("is_live"))}


def ai_reference(code: str) -> dict[str, Any]:
    """10-29（測試版）：問型態時給 AI 的費波參考價；與費波驗證圖同一份資料（正式收盤 K＋一年日K）與計算。"""
    code, _, frame, events = kline_debug.load_closed(code)
    frame, _ = extend_history(code, frame, events)
    return fib_levels.ai_reference(fib_levels.compute(frame, events))


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
    scope = (f"近 {params['near_lookback']} 根找不到，放寬到 {params['lookback']} 根" if data.get("widened")
             else f"終點在最近 {params['lookback']} 根")
    text(margin, 120, f"大轉折門檻 {params['major_atr']:g} ATR｜合格波段 ≥ {params['leg_atr']:g} ATR｜{scope}"
                      f"｜確認日當天的轉折不使用", 22, muted)
    x = margin
    for label, color in (("回撤／反彈", KIND_COLOR["retrace"]), ("前波高點", KIND_COLOR["prior"]), ("延伸", KIND_COLOR["extend"]),
                         ("整理區上下緣／等幅", KIND_COLOR["box"]), ("下跌支撐", KIND_COLOR["support"])):
        draw.line((x, 180, x + 50, 180), fill=color, width=4)
        text(x + 60, 166, label, 21, color)
        x += 60 + tw(label, 21) + 34
    draw.rectangle((x, 170, x + 44, 190), fill="#FFEFC7")
    text(x + 54, 166, "AI 價位區", 21, muted)
    x += 54 + tw("AI 價位區", 21) + 34
    draw.rectangle((x, 170, x + 44, 190), outline=KIND_COLOR["box"], width=2)
    text(x + 54, 166, "整理區", 21, muted)
    text(margin, 202, "實線＝交給 AI｜點線＝錨點未確認｜長短線＝突破後條件目標｜虛線＝不交給 AI｜灰點小轉折、黑圈大轉折", 21, muted)

    left, right, top, bottom = 115, 1215, 255, 755
    live = data.get("live_bar")      # 11-03：盤中 K 棒接在最後一根正式收盤 K 後面，只顯示、不計算
    step = (right - left) / max(1, len(visible) + (1 if live else 0))
    px = lambda i: left + (i - start + 0.5) * step
    prices = [b[k] for b in visible + ([live] if live else []) for k in ("Low", "High")]
    # 10-28：只顯示近段時，交給 AI／現價附近的價位一定入圖；其餘離圖太遠的只列在下方表格，不把 K 線壓扁
    # 11-03：0.618 跌到附近才標的次要 0.786 是要觀察的，也一定入圖
    prices += [lv["price"] for lv in levels
               if lv["ai"] or lv.get("watch") or lv.get("secondary") or min(prices) <= lv["price"] <= max(prices)]
    low, high = min(prices), max(prices)
    padding = max((high - low) * 0.06, abs(high) * 0.003, 0.01)
    low, high = low - padding, high + padding
    levels = [lv for lv in levels if low <= lv["price"] <= high]
    py = lambda p: bottom - (p - low) / (high - low) * (bottom - top)
    for j in range(7):
        value = low + (high - low) * j / 6
        yy = py(value)
        draw.line((left, yy, right, yy), fill=grid)
        label = f"{value:.2f}"
        text(left - 10 - tw(label, 18), yy - 12, label, 18, muted)

    for area in data.get("areas") or []:   # 交給 AI 的價位區先畫，K 棒蓋在上面；其餘共振寫在下方表格
        if area["ai"] and not area.get("conditional") and low <= area["low"] and area["high"] <= high:   # 條件目標不畫黃帶
            draw.rectangle((left, py(area["high"]) - 5, right, py(area["low"]) + 5), fill="#FFEFC7")
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
    if live:   # 11-03 使用者：空心看不清楚 → 和其他 K 棒一樣實心；盤中與否看右側標籤和圖下說明
        color = "#DB4949" if live["Close"] >= live["Open"] else "#23997E"
        xx, half = px(end + 1), max(1.5, min(7, step * 0.30))
        draw.line((xx, py(live["High"]), xx, py(live["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(live["Open"]), py(live["Close"])))
        draw.rectangle((xx - half, y1, xx + half, max(y1 + 2, y2)), fill=color)

    x0 =px(max(start, sel["a"]["idx"])) if sel else left
    for lv in levels:
        yy, color = py(lv["price"]), KIND_COLOR[lv["kind"]]
        x1 = left if lv["kind"] == "support" else x0   # 下跌支撐來自更早的結構，整條畫滿才看得到
        if lv["ai"] and lv.get("conditional"):   # 突破後才成立：長短線
            xx = x1
            while xx < right:
                draw.line((xx, yy, min(xx + 16, right), yy), fill=color, width=3)
                draw.line((min(xx + 22, right), yy, min(xx + 25, right), yy), fill=color, width=3)
                xx += 31
        elif lv["ai"] and lv.get("tentative"):   # 暫定：點線
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
            x1, y1, x2, y2 = px(p["idx"]), py(p["price"]), px(q["idx"]), py(q["price"])
            if x1 < left < x2:   # 起點在圖外：連線從圖左緣開始，不畫到 Y 軸刻度上
                y1 += (y2 - y1) * (left - x1) / (x2 - x1)
                x1 = left
            if x1 >= left:
                draw.line((x1, y1, x2, y2), fill="#8A94A6", width=2)
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
                                           + (" 現價附近" if lv.get("watch") else " 條件" if lv["ai"] and lv.get("conditional")
                                              else " 暫定" if lv["ai"] and lv.get("tentative") else ""),
             "color": KIND_COLOR[lv["kind"]],
             "bold": lv["ai"]} for lv in levels]
    tags.append({"y": py(data["close"]), "text": f"收盤｜{data['close']:.2f}", "color": ink, "bold": True})
    if live:
        tags.append({"y": py(live["Close"]), "text": f"{live.get('label') or ('盤中' if live['is_live'] else '盤後')}{live['time']}"
                                                     f"｜{live['Close']:.2f}",
                     "color": muted, "bold": False})
    _spread(tags, 25, top - 10, bottom)
    for tag in tags:
        if abs(tag["ly"] - tag["y"]) > 2:
            draw.line((right + 4, tag["y"], right + 14, tag["ly"]), fill=tag["color"], width=1)
        text(right + 16, tag["ly"] - 12, tag["text"], 19, tag["color"], tag["bold"])   # 與最後一根的錨點圓圈留空隙

    tick_gap = max(1, math.ceil(len(visible) / 9))
    for i in range(start, end + 1, tick_gap):
        text(px(i) - 24, 779, bars[i]["date"][5:], 19, muted)
    max_vol = max([max(0, b["Volume"]) for b in visible + ([live] if live else [])] + [1.0])
    text(margin, 817, "成交量", 20, muted)
    for i in range(start, end + 1):
        bar = bars[i]
        xx, hh = px(i), max(0, bar["Volume"]) / max_vol * 83
        draw.rectangle((xx - step * 0.28, 935 - hh, xx + step * 0.28, 935), fill="#DB4949" if bar["Close"] >= bar["Open"] else "#23997E")
    if live and live["Volume"] > 0:   # 指數沒有成交量，不畫
        xx, hh = px(end + 1), live["Volume"] / max_vol * 83
        draw.rectangle((xx - step * 0.28, 935 - hh, xx + step * 0.28, 935),
                       fill="#DB4949" if live["Close"] >= live["Open"] else "#23997E")
    draw.line((margin, 955, width - margin, 955), fill=grid, width=2)

    y = 980
    if live:
        state = _live_state(live)
        night = "（含前一晚夜盤）" if live.get("with_night") else ""
        for line in wrap(f"最右邊那根 K 棒是 {live['date'][5:]} 交易日{state}的價格{night}，只顯示、不加入計算；"
                         f"錨點、ATR、費波價位都以 {bars[end]['date'][5:]} 正式收盤計算，收盤後才會更新。", 23, width - 2 * margin):
            text(margin, y, line, 23, muted)
            y += 33
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
    def conf_text(lv):
        tight = lv.get("tight") or []
        near = [t for t in lv["confluence"] if t not in tight]
        return "｜".join(s for s in (("緊密：" + "、".join(tight)) if tight else "",
                                     ("附近：" + "、".join(near)) if near else "") if s) or "無"

    level_rows = [([lv["label"], f"{lv['price']:.2f}",
                    "現價附近" if lv.get("watch") else "收盤之上" if lv["above"] else "收盤之下",
                    conf_text(lv),
                    ("是（條件）" if lv.get("conditional") else "是（暫定）" if lv.get("tentative") else "是") if lv["ai"]
                    else "否（次要）" if lv.get("secondary") else "否"],
                   ink if lv["ai"] else muted)
                  for lv in sorted(data.get("levels") or [], key=lambda x: -x["price"])]   # 表格列全部價位（含圖外的）
    table(f"費波價位｜緊密＝{params['tight_atr']:g} ATR 內、附近＝{params['conf_atr']:g} ATR 內｜相距 {params['zone_atr']:g} ATR 內併成一區",
          [(margin + 12, "價位", 330), (410, "價格", 130), (550, "位置", 130), (690, "共振對象", 620), (1330, "交給 AI", 110)], level_rows)

    y += 20
    text(margin, y, "僅管理員驗證｜忠實呈現目前演算法，找不到合格波段時不補畫｜費波為技術參考，不代表預測能力或買賣建議", 20, muted)
    return image.crop((0, 0, width, int(y + 50)))
