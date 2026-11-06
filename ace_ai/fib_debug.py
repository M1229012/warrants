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
    except Exception as exc:
        # 11-03 程式碼檢查：股票名稱表暫時抓不到（官方、FinMind 都失敗）時，已認出的台指期、代號照用，不讓整題失敗
        import warrant_ai_tools as tools
        print(f"⚠️ 費波驗證：股名比對略過｜{tools.err_text(exc)}", flush=True)
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
            except Exception:    # 對不到這檔或名稱表暫時抓不到：其他標的照用
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


BIG_WAVE_MIN_MULT = 2.0                       # 大波段幅度至少是小波段的幾倍才算（不然是同一段）
BIG_WAVE_RATIOS = (0.382, 0.5, 0.618, 0.786)


def _big_wave(data: dict) -> dict | None:
    """11-05 使用者（「跌破 0.786 才看更大範圍會不會太慢」）：往下看支撐時，最近一段上漲（小波段）之外，一開始就一起看大一級的波段——
    這一年資料裡、小波段起點之前的最低點 → 同一個高點。幅度至少是小波段的 BIG_WAVE_MIN_MULT 倍才算；選用的不是上漲段就不看。"""
    sel = data.get("selected") or {}
    bars = data.get("bars") or []
    if sel.get("dir", 0) <= 0 or not bars:
        return None
    a, b = sel["a"], sel["b"]
    before = bars[:int(a["idx"])]
    if not before:
        return None
    k = min(range(len(before)), key=lambda i: float(before[i]["Low"]))
    low, high = float(before[k]["Low"]), float(b["price"])
    amp = high - low
    if low >= float(a["price"]) or amp < BIG_WAVE_MIN_MULT * (high - float(a["price"])):
        return None
    return {"low": low, "low_idx": k, "low_date": str(before[k]["date"]), "high": high, "high_date": str(b["date"]), "amp": amp,
            "small_low": float(a["price"]), "small_date": str(a["date"])}


def _big_wave_levels(data: dict, close: float) -> tuple[list[dict], dict | None]:
    wave = _big_wave(data)
    if not wave:
        return [], None
    span = f"（大波段 {wave['low_date']}～{wave['high_date'][5:]}）"
    return [{"label": f"大波段回撤 {r:g}{span}", "tag": f"大段{r:g}", "kind": "retrace", "ratio": r,
             "price": wave["high"] - wave["amp"] * r, "ai": True}
            for r in BIG_WAVE_RATIOS if wave["high"] - wave["amp"] * r < close], wave


def _wave_text(wave: dict) -> str:
    return (f"大波段 {wave['low_date']} 低點 {wave['low']:.2f} → {wave['high_date']} 高點 {wave['high']:.2f}（這一年的主升段）；"
            f"名稱有「大段」的是這段的回撤（跌深時的支撐），其餘是最近一段上漲 {wave['small_date'][5:]}～{wave['high_date'][5:]} 的回撤（短線拉回）")


BIG_WAVE_SWITCH = 0.618                       # 跌破小波段這個回撤比例＝小波段的上漲結構被破壞，改看大波段


def _use_big_wave(wave: dict | None, close: float) -> bool:
    """11-05 使用者（「小範圍大範圍都列出來很奇怪，應該有判斷看哪一個」）：現價還在小波段 0.618 之上＝拉回不到 61.8%，
    最近這段上漲還算完整 → 看小波段；跌破 0.618＝這段上漲的結構被破壞，價格在修正更大的波段 → 改看大波段。"""
    if not wave:
        return False
    switch = wave["high"] - (wave["high"] - wave["small_low"]) * BIG_WAVE_SWITCH
    return close < switch


def _wave_choice_text(wave: dict, big: bool) -> str:
    switch = wave["high"] - (wave["high"] - wave["small_low"]) * BIG_WAVE_SWITCH
    small = f"最近一段上漲 {wave['small_date'][5:]}～{wave['high_date'][5:]}"
    if big:
        return (f"看大波段：現價已跌破{small}的 0.618（{switch:.2f}），這段上漲的結構被破壞，改看{_wave_text(wave).split('；')[0]}；"
                "名稱有「大段」的是大波段的回撤，前低是小波段的起點")
    return f"看小波段：現價還在{small}的 0.618（{switch:.2f}）之上，這段上漲的結構還在，先看這段的回撤"


def _with_big_wave(ref: dict, res: dict) -> dict:
    """型態頁的費波參考：下方候選支撐也加上大波段回撤（收盤之下），和費波頁同一套。"""
    if ref.get("狀態"):
        return ref
    close = float(res.get("close") or 0)
    big, wave = _big_wave_levels(res, close)
    if not big or not _use_big_wave(wave, close):       # 11-05：還在小波段 0.618 之上 → 照原本的小波段支撐
        return ref
    # 跌破小波段 0.618 → 下方支撐改看大波段（保留前低，小波段的回撤與等幅拿掉）
    downs = [x for x in ref.get("下方候選支撐") or [] if "前波低點" in str(x.get("依據")) and "大波段" not in str(x.get("依據"))]
    downs += [{"依據": lv["label"], "價位": round(lv["price"], 2)} for lv in big]
    top = lambda x: float(x["價位"]) if "價位" in x else float(str(x.get("價位區") or "0").split("～")[-1] or 0)
    return dict(ref, **{"下方候選支撐": sorted(downs, key=top, reverse=True), "看的範圍": _wave_choice_text(wave, True)})


def ai_reference(code: str) -> dict[str, Any]:
    """10-29（測試版）：問型態時給 AI 的費波參考價；與費波驗證圖同一份資料（正式收盤 K＋一年日K）與計算。
    11-05：下方候選支撐加上大一級波段的回撤（_with_big_wave）。"""
    import taifex_futures
    if code == taifex_futures.CODE:     # 11-03：台指期型態頁也給費波參考（近月連續日 K，正式收盤）
        res = fib_levels.compute(taifex_futures.daily_frame(), None)
    else:
        code, _, frame, events = kline_debug.load_closed(code)
        frame, _ = extend_history(code, frame, events)
        res = fib_levels.compute(frame, events)
    return _with_big_wave(fib_levels.ai_reference(res), res)


PAGE_LIMIT = 2      # 費波型態頁和型態頁一樣，一次最多 2 檔（兩檔＝比較圖）
_DIRECTION_SCHEMA = {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "both"]}},
                     "required": ["direction"]}
_PLAIN = re.compile(r"[\s\d,.，。、？?！!~～:：()（）]")
FOCUS_TEXT = {"up": "往上（目標價、壓力、能漲到哪）", "down": "往下（支撐、可能止跌的位置、會跌到哪）"}


def fib_direction(question: str, ask_ai=None) -> str:
    """11-04 使用者：費波可以往上看（目標價、能漲到哪）也可以往下看（跌到哪、哪裡止跌），任何白話都要懂 →
    讓 AI 讀整句判斷 up／down／both，不補關鍵字。只有代號＋費波、沒有別的字就不問 AI；讀題失敗回 both（上下都標，和以前一樣）。"""
    q = str(question or "")
    if ask_ai is None or not _PLAIN.sub("", _CODE_RE.sub("", _FIB_WORD.sub("", q))):
        return "both"
    prompt = ("你是台股問句解析器，只輸出 JSON，不要回答問題。使用者想看費波南西（費波）價位，讀完整句判斷他想看哪個方向：\n"
              "up＝想看往上：目標價、能漲到哪、滿足點、上面壓力、反彈到哪、突破後看哪、解套價；\n"
              "down＝想看往下：會跌到哪、哪裡止跌、底部在哪、下面支撐、回檔到哪、守不守得住；\n"
              "both＝沒特別說方向，或上下都想看（只問標的、問費波在哪、問怎麼看）。\n"
              "「如果止跌反彈、回升的話會到哪」是假設止跌之後往上看＝up；「會不會止跌、在哪止跌」才是 down。\n問題：" + q)
    try:
        out = ask_ai(prompt, _DIRECTION_SCHEMA) or {}
    except Exception:
        return "both"
    direction = str(out.get("direction") or "")
    return direction if direction in FOCUS_TEXT else "both"   # both＝沒指定，page_levels 照趨勢只看一邊


def page_codes(question: str, ask_ai=None) -> list[str]:
    """11-03 使用者：沒說「驗證」的費波問句（「2330 費波」「台指期費波在哪」「夜盤費波」）→ 費波型態頁的標的。
    規則看得出來（代號、股名、台指期）就不問 AI；看不出來才讓 AI 讀整句。都看不出來回空清單（交給一般問答）。"""
    try:
        codes = rule_codes(question, PAGE_LIMIT)
    except ValueError as exc:      # 規則就數到超過 2 檔
        raise ValueError(f"費波型態頁一次最多 {PAGE_LIMIT} 檔，請分開問") from exc
    if not codes and ask_ai is not None:
        codes = ai_codes(question, ask_ai, PAGE_LIMIT + 1)
    if len(codes) > PAGE_LIMIT:
        raise ValueError(f"費波型態頁一次最多 {PAGE_LIMIT} 檔，請分開問")
    return codes


def _page_name(lv: dict) -> str:
    """價格框／價位條上的名稱：短、看得懂是哪一種費波價位（比例、條件、次要）。"""
    name = str(lv.get("tag") or lv["label"]).replace("前波高點 A", "前波高點").replace("前波高點 B", "前波高點").replace(" ", "")
    name = name.replace("次要", "（次要）")
    if lv.get("conditional") and "突破" not in name:
        name += "（突破後）"
    return name


def _chart_name(lv: dict) -> str:
    """11-03 使用者：K 線下方價位條直接寫費波比例（「0.382」接價格）。主波段回撤／反彈、延伸寫比例（大波段回撤寫「大段0.786」）；
    前波高點、前低、整理區、等幅這類不是比例的照名稱。B 之後 C→B 的反彈（沒有 tag）和主波段比例會同名，保留「反彈」字樣。"""
    tag = str(lv.get("tag") or "")
    if tag.startswith("大段"):
        # 11-04 使用者（「0.786 怎麼在前低」）：大波段回撤是另一段波段的比例，只寫「0.786」會被看成這一段的 0.786
        return tag
    if lv.get("kind") == "extend" or (lv.get("kind") == "retrace" and tag):
        return f"{lv['ratio']:g}"
    return _page_name(lv)


def _page_rank(lv: dict) -> int:
    """11-04：相距 0.15 ATR 內只標一個時誰優先：這一段的比例 > 前低、前波高點、整理區 > 大波段回撤、等幅。"""
    tag, label = str(lv.get("tag") or ""), str(lv.get("label") or "")
    if lv.get("kind") in ("retrace", "extend") and not tag.startswith("大段"):
        return 0
    if tag.startswith(("前低", "整理區")) or "前波高點" in label or "前波低點" in label:
        return 1
    return 2


def page_levels(code: str, close: float, focus: str = "both") -> tuple[dict, dict]:
    """11-03 使用者：費波型態頁——型態頁的價格框與價位條改標費波價位。和費波驗證圖同一份計算（load_panel：
    正式收盤 K＋一年日K；台指期近月連續），close＝型態頁最後一根（盤中是即時價），只用來分上下方、算距離，不加入費波計算。
    回傳（key_levels 卡, 交給 AI 的費波資料）。相距 0.15 ATR 內的價位只留一個，價格框不重疊。
    11-04 使用者（「同時顯示往上和往下的費波太亂」）：一律只看一邊——
    - 沒指定（focus＝both）：照趨勢選（default_side）：還在下跌、上漲後拉回 → 往下，用前面上漲段的回撤看哪裡有支撐；
      下跌後反彈、創新高 → 往上，用下跌段的反彈（或延伸）看哪裡有壓力。
    - 使用者指定（fib_direction 回 up／down）：照指定；還在下跌卻要看往上，用這段下跌（高點→目前低點）量反彈。
    圖、觀察重點、給 AI 的費波價位都只有那一邊。"""
    data = load_panel(code)["fib_debug"]
    if not data.get("selected"):
        return ({"levels_only": True, "hide_score": True, "show_tracked_branches": False, "close": close, "stock_code": code,
                 "resistances_above_close": [], "supports_below_close": []},
                {"stock_code": code, "狀態": "沒有可用的費波波段，價位照一般型態頁"})
    gap = 0.15 * float(data["atr"] or 0)
    side = focus if focus in FOCUS_TEXT else default_side(data)
    picked, same, drop, low_text, note, wave = _side_levels(data, close, side, gap)
    if not picked:                         # 這一邊算不出價位（極少見）：改看另一邊，不讓整頁沒有費波
        side = "down" if side == "up" else "up"
        picked, same, drop, low_text, note, wave = _side_levels(data, close, side, gap)
    rows = [{"label": _chart_name(lv), "price": round(lv["price"], 2), "source": "fib",
             "distance_from_close_pct": (lv["price"] / close - 1) * 100 if close else None} for lv in picked]
    card = {"levels_only": True, "hide_score": True, "show_tracked_branches": False, "close": close, "stock_code": code,
            "fib_page": True, "fib_focus": side,
            "resistances_above_close": sorted([r for r in rows if r["price"] > close], key=lambda r: r["price"]),
            "supports_below_close": sorted([r for r in rows if r["price"] <= close], key=lambda r: -r["price"])}
    ai = {"stock_code": code, "stock_name": data.get("stock_name", ""),
          "說明": (data.get("summary") or [""])[0].split("｜", 1)[-1],
          "計算基準": f"{data['bars'][data['last']]['date']} 正式收盤（盤中價不加入費波計算）",
          "看的方向": FOCUS_TEXT[side] + ("（使用者指定）" if focus in FOCUS_TEXT else "（依目前趨勢）"),
          "費波價位": [dict({"圖上標示": _chart_name(lv), "名稱": _page_name(lv), "價位": round(lv["price"], 2),
                            "位置": "現價之上" if lv["price"] > close else "現價之下"},
                           **({"條件": "突破前波高點後才成立"} if lv.get("conditional") else {}),
                           **({"註記": lv.get("note") or "依目前高低點試算，確認前可能調整"} if lv.get("tentative") else {}),
                           **({"次要": "已跌到 0.618 附近或之下才標，0.618 守不住時的下一個參考"} if lv.get("secondary") else {}),
                           **({"同區還有": "、".join(same[id(lv)])} if same.get(id(lv)) else {}))
                      for lv in sorted(picked, key=lambda x: -x["price"])]}
    if data["selected"].get("fallback"):
        ai["可信度"] = "沒有合格費波主段，以下僅為區間量測"
    if drop:
        ai["反彈量測"] = (f"{drop['a']['date'][5:]} 高點 {drop['a']['price']:.2f} → {low_text}"
                      f"；「反彈」價位用這段下跌量（{note}）")
    if wave:
        ai["看的範圍"] = _wave_choice_text(wave, not wave.get("small_only"))
    return card, ai


VERIFY_PAD = 5          # 費波驗證圖：大波段起點前多留幾根，起點不貼著圖的左邊
BIG_COLOR = "#7C3AED"   # 大波段 A／B 與連線


def _full_ratios(data: dict) -> dict:
    """11-05 使用者：費波驗證圖 0.382／0.5／0.618／0.786 都要畫。演算法只算到 0.618（0.786 要盤中跌深才標），
    選用波段缺的比例補成虛線、不交給 AI、名稱寫「只畫線」；已經有的（含次要 0.786）不重複。回傳新的 dict。"""
    sel = data.get("selected")
    if not sel or not sel.get("a") or not sel.get("b"):
        return data
    have = {round(float(lv.get("ratio") or 0), 3) for lv in data.get("levels") or []
            if lv.get("kind") == "retrace" and not str(lv.get("tag") or "").startswith("大段")}
    up = sel.get("dir", 0) > 0
    end, amp = float(sel["b"]["price"]), abs(float(sel["b"]["price"]) - float(sel["a"]["price"]))
    close, name = float(data.get("close") or 0), "回撤" if up else "反彈"
    add = []
    for r in BIG_WAVE_RATIOS:
        if round(r, 3) in have:
            continue
        price = end - amp * r if up else end + amp * r
        add.append({"label": f"{name} {r:g}（只畫線）", "tag": f"{name}{r:g}", "kind": "retrace", "ratio": r, "price": price,
                    "ai": False, "above": price > close, "confluence": [], "tight": []})
    return dict(data, levels=list(data.get("levels") or []) + add) if add else data


def verify_view(data: dict) -> dict:
    """11-05 使用者（「費波驗證的時候 K 線圖要顯示完整，A、B 點要畫出來；一般問費波照常 70 根」）：
    只給費波驗證圖（render）用，回傳新的 dict，不改原資料。和費波頁同一套判斷：照趨勢往下看、現價（盤中用即時價）
    跌破小波段 0.618 才看大波段——這時圖從大波段起點前 VERIFY_PAD 根開始顯示、畫出大A→大B，價位線加上大波段回撤；
    交給 AI 的下方價位和 _with_big_wave 一樣改成大波段回撤＋前波低點（小波段收盤之下的回撤改成不交給 AI），
    舊的相鄰轉折「大段」拿掉，避免同名卻是不同段。
    11-05 使用者（「只要是費波驗證，0.382、0.5、0.618、0.786 全部顯示在 K 線圖」）：選用波段缺的比例補成虛線（_full_ratios）；
    大波段四個比例不論在收盤上下都畫，收盤之上的不交給 AI。"""
    data = _full_ratios(data)
    if default_side(data) != "down":
        return data
    live = data.get("live_bar") or {}
    close = float(live.get("Close") or data.get("close") or 0)
    big, wave = _big_wave_levels(data, close)
    if not big or not _use_big_wave(wave, close):
        return data
    a_idx, b_idx = wave["low_idx"], int(data["selected"]["b"]["idx"])
    keep = lambda lv: lv["price"] >= close or ("前波低點" in lv["label"] and "大波段" not in lv["label"])
    levels = [lv if keep(lv) else dict(lv, ai=False) for lv in data.get("levels") or []
              if not str(lv.get("tag") or "").startswith("大段")]
    span = f"（大波段 {wave['low_date']}～{wave['high_date'][5:]}）"
    for r in BIG_WAVE_RATIOS:
        price = wave["high"] - wave["amp"] * r
        levels.append({"label": f"大波段回撤 {r:g}{span}", "tag": f"大段{r:g}", "kind": "retrace", "ratio": r, "price": price,
                       "ai": price < close, "above": price >= close, "confluence": [], "tight": [], "from_idx": a_idx})
    areas = [area if area.get("low", 0) >= close else dict(area, ai=False) for area in data.get("areas") or []]
    return dict(data, levels=levels, areas=areas, display_start=min(int(data["display_start"]), max(0, a_idx - VERIFY_PAD)),
                big_wave={"a": {"idx": a_idx, "price": wave["low"], "type": "L"},
                          "b": {"idx": b_idx, "price": wave["high"], "type": "H"}},
                summary=[f"{_wave_choice_text(wave, True)}；圖從大波段起點顯示，紫色大A→大B 是這段，"
                         "名稱有「大段」的是它的回撤（交給 AI），小波段收盤之下的回撤改成不交給 AI"]
                        + list(data.get("summary") or []))


def default_side(data: dict) -> str:
    """11-04 使用者：沒指定方向時照趨勢只看一邊——還在下跌（falling）、上漲後拉回（pullback）看往下；
    下跌後反彈（rebound）、創新高（extend）看往上。"""
    return "down" if data.get("mode") in ("falling", "pullback", "none") else "up"


def _side_levels(data: dict, close: float, side: str, gap: float):
    """只取一邊的費波價位，相距 gap 內併成一個（_page_rank 選代表）。回傳 (picked, 同區名稱, 反彈量測段, 低點文字, 註記, 大波段)。
    還在下跌卻看往上：用這段下跌（高點→目前低點；盤中破低用盤中低點）量反彈，不用上漲段的回撤。"""
    levels, drop, low_text, note = list(data["levels"]), None, "", ""
    if side == "up":
        drop = _rebound_leg(data)
        if drop:
            hi, lo = drop["a"]["price"], drop["b"]["price"]
            low_text, live = f"{drop['b']['date'][5:]} 低點 {lo:.2f}", data.get("live_bar") or {}
            note = "依目前低點試算，低點確認前可能調整"
            if live.get("Low") is not None and float(live["Low"]) < lo:
                # 11-04 使用者：盤中問就是想看盤中 → 今天盤中破了低點就用盤中低點量反彈，說明「盤中推算，以收盤價為主」
                lo = float(live["Low"])
                low_text, note = f"{_live_state(live)} 低點 {lo:.2f}", "盤中推算，以收盤價為主"
            levels = [{"label": f"反彈 {r:g}", "kind": "retrace", "ratio": r, "price": lo + (hi - lo) * r, "ai": True,
                       "tentative": True, "note": note} for r in fib_levels.RETRACE] + [
                      {"label": "前波高點 B", "kind": "prior", "ratio": 1.0, "price": hi, "ai": True}]
    wave = None
    if side == "down":
        big, wave = _big_wave_levels(data, close)
        # 11-05 使用者：小波段／大波段只看一個（_use_big_wave 判斷）；原本用相鄰轉折算的「大段」一律拿掉，避免同名卻是不同段
        levels = [lv for lv in levels if not str(lv.get("tag") or "").startswith("大段")]
        if big and _use_big_wave(wave, close):
            levels = [lv for lv in levels if lv.get("tag") == "前低"] + big     # 跌破小波段 0.618：大波段回撤＋前低
        else:
            wave = dict(wave, small_only=True) if wave else None
    levels = [lv for lv in levels if (lv["price"] > close) == (side == "up")]
    groups = []
    for lv in sorted(levels, key=lambda x: (x["price"], not x["ai"])):
        if groups and abs(lv["price"] - groups[-1][0]["price"]) <= gap:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    # 11-04：同一區只標一個，照 _page_rank 選（例：前低 322.74 和大段0.786 322.50 → 標前低）；同區其他的名稱交給 AI
    picked, same = [], {}
    for group in groups:
        best = min(group, key=lambda x: (_page_rank(x), group.index(x)))
        picked.append(best)
        same[id(best)] = [_page_name(x) for x in group if x is not best]
    return picked, same, drop, low_text, note, wave



def _rebound_leg(data: dict) -> dict | None:
    """11-04：還在下跌（mode＝falling）時那段還沒確認低點的下跌（fib_levels.compute 的 falling_leg：高點 a → 目前低點 b）；
    其他模式回 None。"""
    return data.get("falling_leg") if data.get("mode") == "falling" else None


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


def _render_detail(data: dict) -> Image.Image:
    """舊的詳細驗證圖（延伸、整理區、共振表）；11-06 起只在沒有選用波段時當 render 的備援。
    Pillow 繪圖，沿用既有字型；畫在高畫布上，最後依實際內容裁切。"""
    from answer_image import font, wrap

    data = verify_view(data)         # 11-05：看大波段時從大波段起點顯示、畫出大A／大B
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
        if "from_idx" in lv:                           # 11-05：大波段回撤從大A畫起
            x1 = px(max(start, lv["from_idx"]))
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
        marks = [(p, key.upper(), "#C2410C") for key, p in points]
        wave = data.get("big_wave")      # 11-05：看大波段時畫出大A→大B（和小波段同一個高點時標「B＝大B」）
        if wave:
            ba, bb = wave["a"], wave["b"]
            if start <= ba["idx"] and bb["idx"] <= end:
                draw.line((px(ba["idx"]), py(ba["price"]), px(bb["idx"]), py(bb["price"])), fill=BIG_COLOR, width=2)
            marks.append((ba, "大A", BIG_COLOR))
            same = [k for k, (p, _, _) in enumerate(marks) if p is not ba and p["idx"] == bb["idx"]]
            if same:
                p, label, _ = marks[same[0]]
                marks[same[0]] = (p, f"{label}＝大B", BIG_COLOR)
            else:
                marks.append((bb, "大B", BIG_COLOR))
        for p, label, mark in marks:
            if not start <= p["idx"] <= end:
                continue
            xx, yy = px(p["idx"]), py(p["price"])
            draw.ellipse((xx - 10, yy - 10, xx + 10, yy + 10), outline=mark, width=4)
            ly = yy - 42 if p["type"] == "H" else yy + 14
            half = max(14, tw(label, 21, True) / 2 + 8)
            cx = min(max(xx, left + half + 2), right - half - 2)   # 10-28：最後幾根的錨點字框不能伸進右側價位標籤
            draw.rectangle((cx - half, ly, cx + half, ly + 28), fill="#FFFFFF", outline=mark)
            text(cx - tw(label, 21, True) / 2, ly + 1, label, 21, mark, True)

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
        size = 19                                      # 11-05：加權數字長，標籤超出圖右緣就縮字（不截字）
        while size > 14 and right + 16 + tw(tag["text"], size, tag["bold"]) > width - 6:
            size -= 1
        text(right + 16, tag["ly"] - 12, tag["text"], size, tag["color"], tag["bold"])   # 與最後一根的錨點圓圈留空隙

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
        calendar = f"（{live['calendar_note']}）" if live.get("calendar_note") else ""   # 11-03 複查：交易日沒法用休市表確認時照實寫
        for line in wrap(f"最右邊那根 K 棒是 {live['date'][5:]} 交易日{calendar}{state}的價格{night}，只顯示、不加入計算；"
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


# ---- 11-06 費波驗證圖：一般費波工具畫法 ----
V_UP, V_DOWN = "#DB4949", "#23997E"
V_INK, V_MUTED, V_GRID = "#1D2B44", "#64748B", "#E7EBF1"
V_LINE, V_EDGE, V_BAND = "#2F6FB0", "#94A3B8", "#EEF4FB"
VERIFY_BOX = (118, 150, 1190, 760)      # 驗證圖 K 線區：左、上、右、下


def _vfmt(p: float) -> str:
    return f"{p:,.0f}" if abs(p) >= 10000 else f"{p:,.2f}"


def _verify_wave(data: dict):
    """回 {a, b, up, amp, why}；和費波頁同一套：往下看、跌破小波段 0.618 才量大波段。"""
    sel = data.get("selected")
    if not sel or not sel.get("a") or not sel.get("b"):
        return None
    live = data.get("live_bar") or {}
    price = float(live.get("Close") or data["close"])
    a, b = dict(sel["a"]), dict(sel["b"])
    atr = float(data.get("atr") or 0) or 1.0
    big = _big_wave(data) if default_side(data) == "down" else None
    up = float(b["price"]) > float(a["price"])
    if big and _use_big_wave(big, price):
        switch = big["high"] - (big["high"] - big["small_low"]) * BIG_WAVE_SWITCH
        why = (f"最近一段上漲（{sel['a']['date'][5:]}～{sel['b']['date'][5:]}）的 0.618 在 {_vfmt(switch)}，"
               f"現價已跌破，這段上漲結構被破壞，改量一年來的主升段")
        a = {"idx": big["low_idx"], "price": big["low"], "date": big["low_date"]}
    elif sel.get("fallback"):
        why = "沒有夠大的波段，用回看區間的最高、最低點量（僅供參考）"
    elif up:
        amp = float(b["price"]) - float(a["price"])
        why = f"最近一段夠大的上漲（約 {amp / atr:.1f} 倍 ATR）"
        if big:
            switch = big["high"] - (big["high"] - big["small_low"]) * BIG_WAVE_SWITCH
            why += f"；現價還在這段的 0.618（{_vfmt(switch)}）之上，上漲結構還在，先量這段"
    else:
        amp = float(a["price"]) - float(b["price"])
        why = f"最近一段夠大的下跌（約 {amp / atr:.1f} 倍 ATR）"
        if sel.get("provisional"):
            why += "；低點還沒確認，之後再破低，B 和各價位會跟著往下移"
    amp = abs(float(b["price"]) - float(a["price"]))
    return {"a": a, "b": b, "up": up, "amp": amp, "why": why, "price": price}


def _verify_levels(wave: dict) -> list:
    b, amp, up = float(wave["b"]["price"]), wave["amp"], wave["up"]
    out = [{"r": 0.0, "price": b, "edge": True}]
    out += [{"r": r, "price": b - amp * r if up else b + amp * r, "edge": False} for r in BIG_WAVE_RATIOS]
    out.append({"r": 1.0, "price": float(wave["a"]["price"]), "edge": True})
    return out


def _short_flag(flag: str) -> str:
    flag = str(flag)
    if flag.startswith("一年日K未接上"):
        return "一年日 K 沒抓到，只用近半年資料，比較大的波段可能量不到"
    return flag


def render(data: dict) -> Image.Image:
    """11-06 使用者（看了費波教學：「這樣誰看得懂，照大師高手的做法畫」）：一張圖只量一段波段——
    上漲段從低點 A 拉到高點 B、下跌段從高點 A 拉到低點 B（和費波頁同一套選段，跌破小波段 0.618 量大波段），
    0（B）／0.382／0.5／0.618／0.786／1（A）水平線從 A 往右畫，右側標比例＋價位；A、B 標價格日期並連虛線。
    圖下白話寫量哪一段、為什麼、現在位置，表格列四個比例（離現價、支撐或壓力、AI 有沒有拿到）。
    沒有選用波段時改畫舊的詳細驗證圖（_render_detail）。"""
    from answer_image import font, wrap

    wave = _verify_wave(data)
    if not wave:
        return _render_detail(data)
    bars, end = data["bars"], data["last"]
    a, b, up, amp, price = wave["a"], wave["b"], wave["up"], wave["amp"], wave["price"]
    start = min(int(data["display_start"]), max(0, int(a["idx"]) - 5))
    visible = bars[start:end + 1]
    live = data.get("live_bar")
    lvls = _verify_levels(wave)

    width, margin = 1500, 56
    image = Image.new("RGB", (width, 4000), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=23, color=V_INK, bold=False):
        draw.text((x, y), str(value), font=font(size, bold), fill=color)

    def tw(value, size=23, bold=False):
        return draw.textlength(str(value), font=font(size, bold))

    name = f"{data['stock_code']} {data.get('stock_name', '')}".strip()
    text(margin, 28, f"{name}｜費波驗證", 38, bold=True)
    text(margin, 86, f"日 K｜正式收盤至 {bars[end]['date']}", 22, V_MUTED)

    left, top, right, bottom = VERIFY_BOX
    step = (right - left) / max(1, len(visible) + (1 if live else 0))
    px = lambda i: left + (i - start + 0.5) * step
    prices = [x[k] for x in visible + ([live] if live else []) for k in ("Low", "High")] + [lv["price"] for lv in lvls]
    low, high = min(prices), max(prices)
    pad = (high - low) * 0.05
    low, high = low - pad, high + pad
    py = lambda p: bottom - (p - low) / (high - low) * (bottom - top)

    for j in range(6):                                   # 格線＋左側價格
        value = low + (high - low) * j / 5
        yy = py(value)
        draw.line((left, yy, right, yy), fill=V_GRID)
        text(left - 12 - tw(_vfmt(value), 18), yy - 12, _vfmt(value), 18, V_MUTED)

    xa = px(int(a["idx"]))
    by = {lv["r"]: py(lv["price"]) for lv in lvls}
    draw.rectangle((xa, by[0.5], right, by[0.618]) if by[0.5] < by[0.618] else (xa, by[0.618], right, by[0.5]), fill=V_BAND)
    for lv in lvls:                                      # 費波水平線：從 A 往右畫
        yy = py(lv["price"])
        if lv["edge"]:
            xx = xa
            while xx < right:
                draw.line((xx, yy, min(xx + 10, right), yy), fill=V_EDGE, width=2)
                xx += 16
        else:
            draw.line((xa, yy, right, yy), fill=V_LINE, width=2)

    half = max(1.5, min(7, step * 0.30))
    for i in range(start, end + 1):                      # K 棒
        x = bars[i]
        color = V_UP if x["Close"] >= x["Open"] else V_DOWN
        xx = px(i)
        draw.line((xx, py(x["High"]), xx, py(x["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(x["Open"]), py(x["Close"])))
        draw.rectangle((xx - half, y1, xx + half, max(y1 + 2, y2)), fill=color)
    if live:
        color = V_UP if live["Close"] >= live["Open"] else V_DOWN
        xx = px(end + 1)
        draw.line((xx, py(live["High"]), xx, py(live["Low"])), fill=color, width=2)
        y1, y2 = sorted((py(live["Open"]), py(live["Close"])))
        draw.rectangle((xx - half, y1, xx + half, max(y1 + 2, y2)), fill=color)

    ax, ay, bx, by_ = xa, py(float(a["price"])), px(int(b["idx"])), py(float(b["price"]))
    n = max(1, int(math.hypot(bx - ax, by_ - ay) // 14))
    for k in range(n):                                   # A→B 虛線
        t0, t1 = k / n, (k + 0.55) / n
        draw.line((ax + (bx - ax) * t0, ay + (by_ - ay) * t0, ax + (bx - ax) * t1, ay + (by_ - ay) * t1), fill=V_MUTED, width=2)
    for key, p, xx, yy in (("A", a, ax, ay), ("B", b, bx, by_)):
        high_point = (key == "A") != up                  # 上漲段 A 是低點、B 是高點
        draw.ellipse((xx - 9, yy - 9, xx + 9, yy + 9), outline=V_INK, width=3, fill="#FFFFFF")
        label = f"{key}  {_vfmt(float(p['price']))}  {str(p['date'])[5:].replace('-', '/')}"
        w = tw(label, 20, True) + 20
        cx = min(max(xx, left + w / 2 + 2), right - w / 2 - 2)
        ly = yy - 46 if high_point else yy + 16
        ly = min(max(ly, top - 30), bottom - 30)
        draw.rounded_rectangle((cx - w / 2, ly, cx + w / 2, ly + 30), radius=6, fill="#FFFFFF", outline=V_INK, width=2)
        text(cx - w / 2 + 10, ly + 2, label, 20, V_INK, True)

    tags = []
    for lv in lvls:
        name_ = ("0（B）" if lv["r"] == 0 else "1（A）") if lv["edge"] else f"{lv['r']:g}"
        tags.append({"y": py(lv["price"]), "text": f"{name_}  {_vfmt(lv['price'])}", "color": V_EDGE if lv["edge"] else V_LINE,
                     "bold": not lv["edge"]})
    now_label = ("盤中 " if live and live.get("is_live") else "現價 ") + _vfmt(price)
    tags.append({"y": py(price), "text": now_label, "color": V_INK, "bold": True, "now": True})
    _spread(tags, 28, top - 8, bottom + 8)
    for tag in tags:
        if tag.get("now"):
            draw.line((right - 14, tag["y"], right, tag["y"]), fill=V_INK, width=3)
        if abs(tag["ly"] - tag["y"]) > 2:
            draw.line((right + 3, tag["y"], right + 12, tag["ly"]), fill=tag["color"], width=1)
        size = 21
        while size > 15 and right + 16 + tw(tag["text"], size, tag["bold"]) > width - 8:
            size -= 1
        text(right + 16, tag["ly"] - 13, tag["text"], size, tag["color"], tag["bold"])

    count = len(visible)                                 # 日期：約 8 個，不互疊
    gap = max(1, math.ceil(count / 8))
    last_x = -999
    for i in range(start, end + 1, gap):
        xx = px(i) - 28
        if xx - last_x >= 70:
            text(xx, bottom + 14, bars[i]["date"][5:].replace("-", "/"), 19, V_MUTED)
            last_x = xx

    vt, vb = bottom + 60, bottom + 150                   # 成交量（指數資料缺口是 0，不畫）
    text(margin, vt - 6, "成交量", 19, V_MUTED)
    vols = [max(0.0, float(x.get("Volume") or 0)) for x in visible]
    top_vol = max(vols + [1.0])
    for i, v in zip(range(start, end + 1), vols):
        if v > 0:
            x = bars[i]
            hh = v / top_vol * (vb - vt - 20)
            draw.rectangle((px(i) - step * 0.3, vb - hh, px(i) + step * 0.3, vb), fill=V_UP if x["Close"] >= x["Open"] else V_DOWN)
    draw.line((margin, vb + 18, width - margin, vb + 18), fill=V_GRID, width=2)

    y = vb + 40                                          # 白話說明
    move = "上漲" if up else "下跌"
    lines = [
        ("量哪一段", f"A {str(a['date']).replace('-', '/')} {'低點' if up else '高點'} {_vfmt(float(a['price']))} → "
                    f"B {str(b['date']).replace('-', '/')} {'高點' if up else '低點'} {_vfmt(float(b['price']))}，{move} {_vfmt(amp)}"),
        ("為什麼", wave["why"]),
    ]
    pct = ((float(b["price"]) - price) if up else (price - float(b["price"]))) / amp * 100 if amp else 0
    ratio_now = pct / 100
    below = [r for r in (0.0,) + BIG_WAVE_RATIOS + (1.0,) if r <= ratio_now]
    above = [r for r in (0.0,) + BIG_WAVE_RATIOS + (1.0,) if r > ratio_now]
    where = (f"在 {below[-1]:g} 和 {above[0]:g} 之間" if below and above else "已超過整段" if not above else "")
    lines.append(("現在", f"{now_label}，{'從 B 拉回' if up else '從 B 反彈'} {pct:.1f}%，{where}"))
    if live:
        lines.append(("盤中", f"最右邊一根是 {str(live['date'])[5:].replace('-', '/')} {_live_state(live)} 的價格，只顯示、不加入計算；"
                              f"A、B 和各價位都用 {bars[end]['date'][5:].replace('-', '/')} 正式收盤計算"))
    for head, body in lines:
        text(margin, y, head, 23, V_MUTED, True)
        rows = wrap(body, 23, width - margin * 2 - 120)
        for k, line in enumerate(rows):
            text(margin + 120, y + k * 34, line, 23, V_INK)
        y += len(rows) * 34 + 8

    ai_prices = [lv["price"] for lv in verify_view(data).get("levels") or [] if lv.get("ai")]
    y += 16
    cols = [(margin + 14, "比例"), (margin + 190, "價位"), (margin + 420, "離現價"), (margin + 610, "看法")]
    draw.rectangle((margin, y, width - margin, y + 42), fill="#EEF2F7")
    for cx, head in cols:
        text(cx, y + 7, head, 21, V_INK, True)
    y += 50
    atr = float(data.get("atr") or 0)
    for lv in [x for x in lvls if not x["edge"]]:
        p = lv["price"]
        dist = (p / price - 1) * 100 if price else 0
        if atr and abs(p - price) <= 0.25 * atr:
            view = "現價附近，看這裡守不守得住"
        elif up:
            view = "已跌破，反彈先看這裡（壓力）" if p > price else "下方支撐"
        else:
            view = "上方壓力" if p > price else "已站上，拉回先看這裡（支撐）"
        if any(abs(p - q) <= max(0.01, abs(p) * 1e-6) for q in ai_prices):
            view += "｜AI 有拿到"
        text(cols[0][0], y, f"{lv['r']:g}", 22, V_INK, True)
        text(cols[1][0], y, _vfmt(p), 22, V_INK)
        text(cols[2][0], y, f"{dist:+.1f}%", 22, V_UP if dist > 0 else V_DOWN)
        text(cols[3][0], y, view, 22, V_INK)
        y += 38
        draw.line((margin, y - 6, width - margin, y - 6), fill=V_GRID)

    flags = [_short_flag(f) for f in data.get("flags") or [] if "公司行動資料未核實" not in str(f)]
    y += 14
    for flag in flags:
        for line in wrap("資料：" + flag, 20, width - margin * 2):
            text(margin, y, line, 20, "#9A5B13")
            y += 30
    y += 10
    text(margin, y, "僅管理員驗證｜A→B 是程式選的波段，0.382～0.786 是這段的回撤比例｜費波為技術參考，不構成投資建議", 19, V_MUTED)
    return image.crop((0, 0, width, int(y + 46)))
