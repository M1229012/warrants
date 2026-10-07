"""管理員型態驗證圖：只呈現 kline_patterns 的同次計算結果，不另外擬合線。"""
from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw

import kline_patterns

_COMMAND = re.compile(r"型態驗證|驗證型態|趨勢線驗證")
_ACTION = re.compile(r"驗證|核對|檢查|確認|檢視|看一下|看看|畫出|顯示|列出")
_TARGET = re.compile(r"型態|形態|趨勢線|錨點|轉折確認|轉折點")


def is_request(question: str) -> bool:
    q=str(question or '').strip()
    if re.search(r'不要|不用|別幫|不需要',q):return False
    return bool(_COMMAND.search(q) or
                (re.search(r'驗證|核對',q) and _TARGET.search(q)) or
                (_ACTION.search(q) and re.search(r'錨點|轉折確認|趨勢線.*(?:正確|對不對|怎麼畫|畫法)|(?:偵測|判斷|計算).*(?:型態|形態)',q)) or
                (re.search(r'型態|形態',q) and re.search(r'畫出|正確|對不對',q)))


def parse_code(question: str) -> str:
    q=str(question or '').strip()
    if not is_request(q):raise ValueError('請說：幫我驗證2330的型態（一次一檔）')
    codes=set(re.findall(r'(?<![0-9A-Za-z])(?:[0-9]{4,6}[A-Za-z]?|TAIEX|TPEX)(?![0-9A-Za-z])',q,re.I))
    codes={c.upper() for c in codes}
    # 代號已有時不額外載入股名；純股名沿用既有正式名稱表。
    if not codes:
        import warrant_ai_tools as tools
        names=tools.get_stock_name_map()
        codes={str(c).upper() for c,n in names.items() if len(str(n))>=2 and str(n) in q}
    if len(codes)!=1:raise ValueError('請指定一檔股票，例如：幫我驗證2330的型態，或核對台積電的趨勢線。')
    return next(iter(codes))


def parse_codes(question: str, limit: int = 10) -> list[str]:
    """一次驗證多檔（10-06）：「型態驗證 2344 1608 2421」依輸入順序、最多 limit 檔；只有一檔時沿用 parse_code。"""
    q=str(question or '').strip()
    found=list(dict.fromkeys(c.upper() for c in re.findall(r'(?<![0-9A-Za-z])(?:[0-9]{4,6}[A-Za-z]?|TAIEX|TPEX)(?![0-9A-Za-z])',q,re.I)))
    if len(found)<=1:return [parse_code(q)]
    if not is_request(q):raise ValueError('請說：型態驗證 2344 1608（一次最多10檔）')
    return found[:limit]


def load_panel(code: str) -> dict[str, Any]:
    """僅管理員路由呼叫；沿用既有行情與公司行動入口。"""
    import warrant_ai_tools as tools

    code, name = tools._stock_identity(code)
    bundle = tools._load_price_bundle(code)
    frame = tools.closed_frame(bundle).copy()
    info = bundle.get("intraday") or {}
    # 即使上游未提供 closed_df，也不能把同日暫定 K 當成正式收盤。
    pending = bool(info.get("is_live") or info.get("post_close_provisional")) and not info.get("is_close_confirmed")
    if pending and not frame.empty:
        pending_day = pd.to_datetime(info.get("date"), errors="coerce")
        if pd.isna(pending_day):
            raise ValueError("暫定 K 的日期不明，無法確認正式收盤範圍")
        frame = frame[pd.to_datetime(frame.index).date < pending_day.date()]
    if frame.empty:
        raise ValueError("沒有可使用的正式收盤日 K")
    events = tools.get_corporate_actions(code)
    return build_panel(frame, events, code, name, state_key=f"tri_state_{code}")


def _state_summary(state_key: str) -> str:
    """三角存檔摘要（重啟前後比對用）：as_of、revision、正式紀錄數、各筆首次突破（辨識）日。"""
    store = kline_patterns._tri_store()
    if store is None:
        return "三角存檔：未啟用（不是正式資料庫，未讀寫）"
    try:
        st = store.get_state(state_key) or {}
    except Exception as exc:
        return f"三角存檔：讀取失敗（{type(exc).__name__}）"
    if not st:
        return "三角存檔：尚無紀錄"
    recs = st.get("records") or []
    firsts = "、".join(r.get("recognized", "")[5:] + ("（回溯）" if r.get("reconstructed_at") else "") for r in recs) or "無"
    return f"三角存檔：as_of {st.get('as_of')}｜revision {st.get('revision', 0)}｜正式紀錄 {len(recs)} 筆｜首次突破 {firsts}"


def build_panel(frame: pd.DataFrame, events: dict | None, code: str, name: str = "", state_key: str = "") -> dict:
    result = kline_patterns.detect(frame, events, include_debug=True, state_key=state_key)
    snapshot = result.get("debug")
    if not snapshot:
        raise ValueError("；".join(result.get("summary") or ["日 K 資料不足或無法判定"]))
    used = snapshot["frame"]
    bars = [{"date": pd.Timestamp(day).strftime("%Y-%m-%d"),
             **{k: float(row[k]) for k in ("Open", "High", "Low", "Close")},
             "Volume": float(row.get("Volume", 0)) if pd.notna(row.get("Volume", 0)) else 0.0}
            for day, row in used.iterrows()]
    start = max(0, len(bars) - kline_patterns.SEARCH_DAYS)
    tri = result.get("triangle")
    if tri:   # 10-06：三角起點比 60 根更早（換尺度、長三角）時，顯示範圍跟著延伸，看得到抓了哪些點
        start = max(0, min([start] + [int(a[0]) - 5 for a in tri["anchors"].values()]))
    return {"kline_debug": {"stock_code": code, "stock_name": name, "bars": bars,
                            "display_start": start,
                            "last_official": snapshot["last_official"], "pivots": result["pivots"],
                            "triangle": result.get("triangle"),
                            "formation": result.get("formation"), "ended": result.get("ended"),
                            "invalid": snapshot.get("invalid"), "trend": snapshot.get("trend"),
                            "summary": result["summary"],
                            "flags": (result.get("flags") or []) + ([_state_summary(state_key)] if state_key else []),
                            "atr20": result.get("atr20"), "break_multiplier": snapshot["break_multiplier"],
                            "retest_multiplier": snapshot["retest_multiplier"]}}


def line_specs(data: dict) -> list[dict]:
    """保持原始整段資料的 x 座標；裁切 60 根顯示時不改斜率或截距。"""
    last = data["last_official"]
    formation = data.get("formation")
    historical = not bool(formation)
    formation = formation or data.get("ended") or data.get("invalid")
    lines = []
    triangle = data.get("triangle")
    if triangle:
        # detect 的文字優先採用 triangle；畫線也必須使用同組係數。
        # 兩條邊的錨點起訖可不同，保持完整計算框架的 index，不重新擬合。
        for edge, label, color in (("upper", "上緣", "#C76C00"), ("lower", "下緣", "#1478B5")):
            anchors = list(triangle["anchors"][edge])
            lines.append({"label": label, "coef": triangle[edge],
                          "start": anchors[0], "fit_end": anchors[-1], "stop": last,
                          "ref_end": triangle.get("reference_until") or -1,   # 參考線段（收斂區間之前）畫虛線
                          "hist_from": triangle.get("first_bday") if triangle.get("first_bday") is not None else 10**9,   # 突破後＝歷史線（灰虛線）
                          "anchors": anchors, "color": color, "historical": False})
    elif False and formation:   # 10-06：驗證圖只畫新三角；舊算法的整理線（通道、楔形）不再畫，避免兩套線混在一起
        # 10-06：舊算法的歷史線若上下同方向（楔形／通道）不畫，避免 2421 那種離譜的灰線
        stop = min(last, formation.get("end_day", formation.get("inv_day", last)))
        for edge, label, color in (("upper", "上緣", "#C76C00"), ("lower", "下緣", "#1478B5")):
            lines.append({"label": label + ("（歷史）" if historical else ""),
                          "coef": formation[edge], "start": formation["start"],
                          "fit_end": formation["end"], "stop": stop,
                          "anchors": (formation.get("anchors") or {}).get(edge, []),
                          "color": "#87909E" if historical else color, "historical": historical})
    tr = data.get("trend")
    if tr and tr.get("line_coefficients"):
        points = tr["points"]
        lines.append({"label": tr["kind"] + "線", "coef": tr["line_coefficients"],
                      "start": points[0]["idx"], "fit_end": points[1]["idx"], "stop": last,
                      "anchors": [p["idx"] for p in points], "color": "#8044B8", "historical": False})
    return lines


def render(data: dict) -> Image.Image:
    """Pillow 繪圖，沿用既有字型；不需要新增外部繪圖套件。"""
    from answer_image import font, wrap

    bars = data["bars"]
    start, end = data["display_start"], data["last_official"]
    visible = bars[start:end + 1]
    if not visible:
        raise ValueError("沒有可顯示的正式日 K")
    lines = line_specs(data)
    all_pivots = data.get("pivots") or []
    counts = {"H": 0, "L": 0}
    points = []
    for p in all_pivots:
        counts[p["type"]] += 1
        if start <= p["idx"] <= end:
            points.append(dict(p, tag=p["type"] + str(counts[p["type"]])))

    width, margin = 1500, 52
    ink, muted, grid = "#1D2B44", "#64748B", "#E7EBF1"
    notes = []
    for value in data.get("summary") or []:
        notes.extend(wrap(value, 23, width - 2 * margin))
    flags = []
    for value in data.get("flags") or []:
        flags.extend(wrap("資料旗標：" + str(value), 21, width - 2 * margin))
    metrics = []
    chosen = data.get("triangle")   # 10-06：突破日只標新三角（舊算法的線已不畫，標了像亂點）
    if chosen and chosen.get("ref") is not None:
        ref = chosen["ref"]
        metrics.append(f"型態 ATR 基準 {ref:.4f}｜突破距離 {data['break_multiplier'] * ref:.4f}｜回測半寬 {data['retest_multiplier'] * ref:.4f}")
    for line in lines:
        s, k = line["coef"]
        anchor_dates = "、".join(bars[i]["date"] for i in line["anchors"])
        metrics.extend(wrap(f"{line['label']}：末端 {s * line['stop'] + k:.2f}｜斜率 {s:.6f}／交易日｜錨點 {anchor_dates or '未提供'}", 21, width - 2 * margin))
    if not lines:
        metrics.append("程式未選出合格的型態線或趨勢線；保留 K 棒與已確認轉折供檢查，不補畫其他線。")

    body_y = 980
    table_y = body_y + len(notes) * 33 + 20 + len(metrics) * 31 + 15 + len(flags) * 30 + 56
    height = table_y + 42 + max(1, len(points)) * 35 + 92
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=23, color=ink, bold=False):
        draw.text((x, y), str(value), font=font(size, bold), fill=color)

    text(margin, 25, f"{data['stock_code']} {data.get('stock_name', '')}｜管理員型態驗證", 37, bold=True)
    text(margin, 86, f"正式收盤截至 {bars[end]['date']}｜顯示 {len(visible)} 根；計算使用 {len(bars)} 根｜前一日 ATR20：{data.get('atr20')}", 22, muted)
    text(margin, 120, "同次計算的價格與線係數｜錨點外圈標記｜虛線為延伸段｜轉折編號對照下表", 22, muted)
    for x, label, color in ((margin, "上緣", "#C76C00"), (265, "下緣", "#1478B5"), (480, "趨勢線", "#8044B8"), (740, "歷史線", "#87909E")):
        draw.line((x, 180, x + 55, 180), fill=color, width=4)
        text(x + 68, 162, label, 22, color)
    text(1040, 162, "紅漲／綠跌；H 高點／L 低點", 21, muted)

    left, right, top, bottom = 85, 1380, 220, 755
    step = (right - left) / max(1, len(visible))
    px = lambda i: left + (i - start + 0.5) * step
    prices = [b[key] for b in visible for key in ("Low", "High")]
    for line in lines:
        a, b = max(start, line["start"]), min(end, line["stop"])
        if a <= b:
            prices += [line["coef"][0] * i + line["coef"][1] for i in (a, b)]
    low, high = min(prices), max(prices)
    padding = max((high - low) * 0.13, abs(high) * 0.003, 0.01)
    low, high = low - padding, high + padding
    py = lambda p: bottom - (p - low) / (high - low) * (bottom - top)
    for j in range(7):
        value = low + (high - low) * j / 6
        yy = py(value)
        draw.line((left, yy, right, yy), fill=grid)
        text(right + 12, yy - 13, f"{value:.2f}", 19, muted)

    if chosen and chosen.get("bday") is not None and start <= chosen["bday"] <= end:
        xx = px(chosen["bday"])
        draw.line((xx, top, xx, bottom), fill="#BFC7D3", width=2)
        first = chosen.get("first_bday")
        label = ("突破日 " + bars[chosen["bday"]]["date"][5:]) if first in (None, chosen["bday"]) else \
            f"首次 {bars[first]['date'][5:]}｜最近 {bars[chosen['bday']]['date'][5:]}"   # 再次突破時首次日期也要看得到
        if first not in (None, chosen["bday"]) and start <= first <= end:
            draw.line((px(first), top, px(first), bottom), fill="#D8DEE6", width=2)
        text(min(xx + 5, right - 260), top - 31, label, 21, ink)

    for i in range(start, end + 1):
        bar = bars[i]
        color = "#DB4949" if bar["Close"] >= bar["Open"] else "#23997E"
        x, half = px(i), max(1.5, min(7, step * 0.30))
        draw.line((x, py(bar["High"]), x, py(bar["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(bar["Open"]), py(bar["Close"])))
        draw.rectangle((x - half, y1, x + half, max(y1 + 2, y2)), fill=color)

    for line in lines:
        a, b = max(start, line["start"]), min(end, line["stop"])
        s, k = line["coef"]
        for i in range(a, b):
            if i >= line["fit_end"] or line["historical"] or i < line.get("ref_end", -1) or i >= line.get("hist_from", 10**9):
                color = "#87909E" if i >= line.get("hist_from", 10**9) else line["color"]
                for t0, t1 in ((0.0, 0.32), (0.55, 0.87)):
                    draw.line((px(i + t0), py(s * (i + t0) + k), px(i + t1), py(s * (i + t1) + k)), fill=color, width=3)
            else:
                draw.line((px(i), py(s * i + k), px(i + 1), py(s * (i + 1) + k)), fill=line["color"], width=3)
        for i in line["anchors"]:
            if start <= i <= end:
                x, y = px(i), py(s * i + k)
                draw.ellipse((x - 9, y - 9, x + 9, y + 9), outline=line["color"], width=3)

    for p in points:
        x, y = px(p["idx"]), py(p["price"])
        available = p["confirm"] < end
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=ink if available else "white", outline=ink)
        yy = y - 31 if p["type"] == "H" else y + 10
        text(x - 12, yy, p["tag"], 17, ink if available else muted)

    tick_gap = max(1, math.ceil(len(visible) / 9))
    for i in range(start, end + 1, tick_gap):
        text(px(i) - 24, 779, bars[i]["date"][5:], 19, muted)
    max_vol = max([max(0, b["Volume"]) for b in visible] + [1.0])
    text(margin, 817, "成交量（原始股數）", 20, muted)
    for i in range(start, end + 1):
        bar = bars[i]
        x, hh = px(i), max(0, bar["Volume"]) / max_vol * 83
        draw.rectangle((x - step * 0.28, 935 - hh, x + step * 0.28, 935), fill="#DB4949" if bar["Close"] >= bar["Open"] else "#23997E")
    draw.line((margin, 955, width - margin, 955), fill=grid, width=2)
    y = body_y
    for value in notes:
        text(margin, y, value, 23)
        y += 33
    y += 20
    for value in metrics:
        text(margin, y, value, 21, muted)
        y += 31
    y += 15
    for value in flags:
        text(margin, y, value, 21, "#9A5B13")
        y += 30
    text(margin, table_y - 43, "轉折明細｜發生日與確認日分開；確認日當天尚不供當日畫線使用", 23, bold=True)
    columns = [(margin + 12, "編號"), (190, "發生日"), (400, "確認日"), (610, "轉折價"), (815, "用途／當日可用狀態")]
    draw.rectangle((margin, table_y, width - margin, table_y + 39), fill="#EDF1F7")
    for x, label in columns:
        text(x, table_y + 4, label, 21, bold=True)
    y = table_y + 44
    if not points:
        text(margin + 12, y, "顯示區間內沒有已確認轉折。", 22, muted)
    for p in points:
        uses = [line["label"] + "錨點" for line in lines if p["idx"] in line["anchors"]]
        if p["confirm"] >= end:
            uses.append("本日才確認，下個交易日起可用")
        values = [p["tag"], bars[p["idx"]]["date"], bars[p["confirm"]]["date"], f"{p['price']:.4f}", "、".join(uses) or "已確認轉折"]
        for (x, _), value in zip(columns, values):
            text(x, y, value, 20, muted if p["confirm"] >= end else ink)
        y += 35
    text(margin, height - 53, "僅管理員驗證｜線條忠實呈現目前演算法，未辨識到型態時不補畫｜驗證描述，不代表預測能力", 20, muted)
    return image
