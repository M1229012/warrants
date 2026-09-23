"""個人交易覆盤筆記：使用者只給「股票＋買進日＋買進理由」（賣出日／賣出理由可選）。

兩層分工：
  Python：事實（買賣價、報酬、持有日、MFE／MAE、原始理由、進場當日技術與法人、進場後走勢、目前結構）
          ＋ 理由核對（✅ 成立／⚠️ 部分成立／❌ 不成立／❓ 資料不足）
  Gemini：讀事實、挑 2～4 個值得記錄的重點，寫成投資人自己的覆盤筆記，固定 JSON：
          headline／body／highlights（2～3 條）／watch（持倉中）或 lesson（已賣出）
          每欄都做數字核對、刪事後歸因句、限字數；Gemini 失敗就用程式版 fallback，同一個格式。

原則：
- 「買進當時」只用買進日（含）以前的日 K 計算，不混入之後才知道的資料。
- 理由有提到權證／分點才抓分點事件；有提到外資／投信／自營商／法人才抓三大法人。都沒提就不抓。
- 筆記依 Discord 使用者分開存（SQLite），保存每條理由的核對狀態，之後可以統計「哪種理由常常不成立」。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import warrant_ai_tools as tools
import local_market_cache

_TRIGGER_RE = re.compile(r"覆盤|复盘|復盤")
_WARRANT_RE = re.compile(r"權證|分點|主力|券商|大戶|[A-E]\s*事件|ABCDE", re.IGNORECASE)
_INST_RE = re.compile(r"外資|投信|自營|法人|老外|外國人|外人|投顧|大戶投信")
# 日期分隔只認 / - 月（不認小數點，免得「買在 12.5」被當成 12 月 5 日）
_DATE_RE = re.compile(r"(?<!\d)(?:(20\d{2})[/\-年])?(\d{1,2})[/\-月](\d{1,2})日?(?!\d)")
_PRICE_RE = re.compile(r"(?:買在|買進價|買進|買入|成本|價格|均價|@)\s*(\d+(?:\.\d+)?)(?![\d.]|\s*張)|(\d+(?:\.\d+)?)\s*元")
_NOT_CODE_RE = re.compile(r"(?:買在|買進價|買進|買入|賣出|賣在|賣掉|成本|價格|均價|@)\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*[元張]")
_LOTS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*張")
_SELL_REASON_RE = re.compile(r"賣出?(?:的)?(?:理由|原因)\s*[:：是]?\s*(.+)$", re.S)
_REASON_RE = re.compile(r"(?:買進?(?:的)?)?(?:理由|原因|因為)\s*[:：是]?\s*(.+)$", re.S)
_SELL_WORD_RE = re.compile(r"賣|出場|出清")
_CODE_RE = re.compile(r"(?<![0-9A-Z])(\d{4,6}[A-Z]?)(?![0-9A-Z])")
# 事後歸因的寫法一律刪掉：我們證明不了「就是因為 X 所以賺錢」
_HINDSIGHT_RE = re.compile(r"歸功|多虧|得益於|正是因為|才能賺|所以賺|證明了|果然|就是因為")
STATE_PREFIX = "trade_review:"
KEEP_NOTES = 50
CHART_MIN_BARS, CHART_MAX_BARS, CHART_BEFORE = 70, 140, 20
POST_SELL_DAYS = 5
STATUS_TEXT = {"✅": "成立", "⚠️": "部分成立", "❌": "不成立", "❓": "資料不足"}


def is_review_request(text: str) -> bool:
    return bool(_TRIGGER_RE.search(str(text or "")))


def is_list_request(text: str) -> bool:
    return bool(re.search(r"我的覆盤|覆盤清單|覆盤紀錄", str(text or "")))


# ============================================================
# 解析
# ============================================================

def _parse_date(month: int, day: int, year: Optional[int], today: date) -> Optional[date]:
    try:
        value = date(year or today.year, month, day)
    except ValueError:
        return None
    if not year and value > today:           # 沒寫年份又比今天晚＝去年
        value = date(today.year - 1, month, day)
    return value


def _find_stock(text: str) -> Tuple[str, str]:
    names = tools.get_stock_name_map()
    # 先拿掉價格、張數與日期，「買進 1285」「2 張」才不會被當成股票代號
    cleaned = _DATE_RE.sub(" ", _NOT_CODE_RE.sub(" ", text))
    for hit in _CODE_RE.finditer(cleaned):
        if hit.group(1) in names:
            return hit.group(1), names[hit.group(1)]
    # 沒寫代號就用名稱找：取句子裡最長的那個股票名稱，避免「聯發」蓋過「聯發科」
    best = ("", "")
    for code, name in names.items():
        if len(name) >= 2 and name in text and len(name) > len(best[1]) and re.fullmatch(r"\d{4}", code):
            best = (code, name)
    return best


def _segment_price(segment: str, code: str) -> Optional[float]:
    """日期後面那一段裡的第一個價格（排除張數、股票代號）。"""
    explicit = _PRICE_RE.search(segment)
    if explicit:
        return float(explicit.group(1) or explicit.group(2))
    for hit in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])(?!\s*張)", segment):
        if hit.group(1) != code:
            return float(hit.group(1))
    return None


def parse_request(text: str) -> Dict[str, Any]:
    """「覆盤 2454 9/1 買進 4315 9/30 5600 賣掉，理由：…，賣出理由：…」→ 結構化欄位。"""
    raw = str(text or "").strip()
    today = tools.taipei_now().date()
    sell_hit = _SELL_REASON_RE.search(raw)
    sell_reason = sell_hit.group(1).strip() if sell_hit else ""
    main = raw[:sell_hit.start()] if sell_hit else raw
    reason_hit = _REASON_RE.search(main)
    reason = reason_hit.group(1).strip(" ，,。；;") if reason_hit else ""
    head = main[:reason_hit.start()] if reason_hit else main
    code, name = _find_stock(head) if head else ("", "")
    if not code:
        code, name = _find_stock(raw)

    buy_date = sell_date = buy_price = sell_price = None
    matches = [m for m in _DATE_RE.finditer(head)]
    for n, match in enumerate(matches):
        value = _parse_date(int(match.group(2)), int(match.group(3)),
                            int(match.group(1)) if match.group(1) else None, today)
        if not value:
            continue
        segment = head[match.end():matches[n + 1].start() if n + 1 < len(matches) else len(head)]
        if _SELL_WORD_RE.search(segment[:14]):
            sell_date = sell_date or value
            sell_price = sell_price or _segment_price(segment, code)
        elif buy_date is None:
            buy_date = value
            buy_price = _segment_price(segment, code)
    # 沒寫「理由：」時，把股票、日期、價格拿掉後剩下的字當理由
    if not reason:
        rest = _DATE_RE.sub("", _NOT_CODE_RE.sub("", _TRIGGER_RE.sub("", main)))
        rest = _CODE_RE.sub("", rest)
        if name:
            rest = rest.replace(name, "")
        reason = re.sub(r"^[\s，,。:：]*(買進|買入|買|進場)?[\s，,。:：]*", "", rest).strip()
    lots_hit = _LOTS_RE.search(head)
    return {"code": code, "name": name, "buy_date": buy_date, "sell_date": sell_date,
            "reason": reason, "sell_reason": sell_reason, "price": buy_price, "sell_price": sell_price,
            "lots": float(lots_hit.group(1)) if lots_hit else None,
            "need_warrant": bool(_WARRANT_RE.search(reason)), "need_inst": bool(_INST_RE.search(reason))}


def missing_fields(req: Dict[str, Any]) -> List[str]:
    return [label for key, label in (("code", "股票"), ("buy_date", "買進日"), ("reason", "買進理由"))
            if not req.get(key)]


# ============================================================
# 買進當時的盤面（只用買進日含以前的資料）
# ============================================================

def _f(value: Any, digits: int = 2) -> Optional[float]:
    return tools._num(value, digits)


def _index_on_or_after(df: pd.DataFrame, day: date) -> Optional[int]:
    dates = pd.DatetimeIndex(df.index).normalize()
    hits = [i for i, d in enumerate(dates) if d.date() >= day]
    return hits[0] if hits else None


def _kd_cross_days_ago(part: pd.DataFrame, lookback: int = 3, down: bool = False) -> Optional[int]:
    """最近幾根內 K 穿過 D：down=False 由下往上（黃金交叉）、True 由上往下（死亡交叉）；
    0＝當天、1＝前一天…；沒有交叉回 None。"""
    k, d = part.get("K9"), part.get("D9")
    if k is None or d is None or len(part) < 2:
        return None
    for back in range(0, min(lookback, len(part) - 1)):
        i = len(part) - 1 - back
        k0, d0, k1, d1 = _f(k.iloc[i - 1]), _f(d.iloc[i - 1]), _f(k.iloc[i]), _f(d.iloc[i])
        if None in (k0, d0, k1, d1):
            continue
        if (not down and k0 <= d0 and k1 > d1) or (down and k0 >= d0 and k1 < d1):
            return back
    return None


def snapshot_at(df: pd.DataFrame, idx: int) -> Dict[str, Any]:
    """第 idx 根 K 棒收盤時看得到的盤面；指標都是滾動計算，切片後不會偷看到未來。"""
    part = df.iloc[:idx + 1]
    row, prev = part.iloc[-1], part.iloc[-2] if len(part) > 1 else part.iloc[-1]
    close = _f(row.get("Close"))
    mas = {n: _f(row.get(f"MA{n}")) for n in (5, 10, 20, 60)}
    upper, lower = _f(row.get("BB_UPPER")), _f(row.get("BB_LOWER"))
    widths = []
    for _, r in part.tail(4).iterrows():
        u, l, m = _f(r.get("BB_UPPER")), _f(r.get("BB_LOWER")), _f(r.get("MA20"))
        widths.append((u - l) / m * 100 if None not in (u, l, m) and m else None)
    volume = _f(row.get("Volume"), 0)
    try:
        bollinger = tools.analyze_bollinger(part)
    except Exception:
        bollinger = {}
    # 布林帶寬近 60 日歷史（壓縮＝目前帶寬在近期低檔）
    width_hist = []
    for _, r in part.tail(60).iterrows():
        u, l, m = _f(r.get("BB_UPPER")), _f(r.get("BB_LOWER")), _f(r.get("MA20"))
        if None not in (u, l, m) and m:
            width_hist.append((u - l) / m * 100)
    # 均線方向：和 3 根前比；MA5／MA20 交叉用最近 4 根
    back = part.iloc[-4] if len(part) >= 4 else part.iloc[0]
    ma_prev3 = {f"MA{n}": _f(back.get(f"MA{n}")) for n in (5, 10, 20, 60)}
    ma5_hist = [_f(v) for v in part["MA5"].tail(4)] if "MA5" in part else []
    ma20_hist = [_f(v) for v in part["MA20"].tail(4)] if "MA20" in part else []
    osc_hist = [_f(v, 4) for v in part["OSC"].tail(4)] if "OSC" in part else []
    prior = part.iloc[:-1]
    # 最近 3 根（含當天）收盤站上布林上軌的是第幾天前：0＝當天、None＝沒有
    upper_break = None
    for back in range(0, min(3, len(part))):
        r = part.iloc[len(part) - 1 - back]
        c, u = _f(r.get("Close")), _f(r.get("BB_UPPER"))
        if c is not None and u is not None and c >= u:
            upper_break = back
            break
    return {
        "date": tools._fmt_date(part.index[-1]), "close": close,
        "change_pct": _f((row["Close"] / prev["Close"] - 1) * 100) if len(part) > 1 and prev.get("Close") else None,
        "moving_averages": {f"MA{n}": tools._ma_position(close, v) for n, v in mas.items()},
        "ma_alignment": tools._ma_alignment(mas),
        "bollinger": {"upper": upper, "lower": lower, "mid": mas[20],
                      "bandwidth_pct": _f(widths[-1]) if widths and widths[-1] is not None else None,
                      "bandwidth_trend": widths, "signals": (bollinger or {}).get("signals", []),
                      "upper_break_days_ago": upper_break},
        "kd": {"K9": _f(row.get("K9")), "D9": _f(row.get("D9")),
               "K_prev": _f(prev.get("K9")) if len(part) > 1 else None,
               "D_prev": _f(prev.get("D9")) if len(part) > 1 else None,
               "K_last3": [_f(v) for v in part["K9"].tail(3)] if "K9" in part else [],
               "cross_days_ago": _kd_cross_days_ago(part),
               "death_cross_days_ago": _kd_cross_days_ago(part, down=True)},
        "ma_prev3": ma_prev3, "ma5_hist": ma5_hist, "ma20_hist": ma20_hist,
        "bb_width_hist": [round(w, 3) for w in width_hist],
        "osc_hist": osc_hist,
        "open": _f(row.get("Open")), "low": _f(row.get("Low")), "high": _f(row.get("High")),
        "prev_close": _f(prev.get("Close")) if len(part) > 1 else None,
        "prev_high": _f(prev.get("High")) if len(part) > 1 else None,
        "prior_high_20d": _f(prior["High"].tail(20).max()) if len(prior) else None,
        "prior_high_60d": _f(prior["High"].tail(60).max()) if len(prior) else None,
        "volume_lots": round(volume / 1000) if volume else None,
        "mv5_lots": round(_f(row.get("MV5"), 0) / 1000) if _f(row.get("MV5"), 0) else None,
        "mv20_lots": round(_f(row.get("MV20"), 0) / 1000) if _f(row.get("MV20"), 0) else None,
        "high_20d": _f(part["High"].tail(20).max()), "low_20d": _f(part["Low"].tail(20).min()),
    }


def after_buy(df: pd.DataFrame, idx: int, price: float, sell_idx: Optional[int],
              sell_price: Optional[float]) -> Dict[str, Any]:
    """進場到現在（或到賣出日）的報酬、最大浮盈（MFE）、最大回撤（MAE）。"""
    end = sell_idx if sell_idx is not None else len(df) - 1
    part = df.iloc[idx:end + 1]
    last = float(sell_price) if sell_idx is not None and sell_price else float(part["Close"].iloc[-1])
    high, low = float(part["High"].max()), float(part["Low"].min())
    return {
        "until": tools._fmt_date(part.index[-1]), "trading_days": len(part) - 1,
        "last_price": round(last, 2), "return_pct": round((last / price - 1) * 100, 2),
        "max_gain_pct": round((high / price - 1) * 100, 2),
        "max_drawdown_pct": round((low / price - 1) * 100, 2),
        "high_date": tools._fmt_date(part["High"].idxmax()), "low_date": tools._fmt_date(part["Low"].idxmin()),
        "closed_trade": sell_idx is not None,
    }


def after_sell(df: pd.DataFrame, sell_idx: int, sell_price: float) -> Dict[str, Any]:
    """賣出後 5 個交易日：用來判斷賣太早／賣太晚（只給數字，判讀交給文字）。"""
    post = df.iloc[sell_idx + 1:sell_idx + 1 + POST_SELL_DAYS]
    if post.empty:
        return {"days_available": 0}
    return {
        "days_available": len(post),
        "close_change_pct": round((float(post["Close"].iloc[-1]) / sell_price - 1) * 100, 2),
        "max_gain_after_pct": round((float(post["High"].max()) / sell_price - 1) * 100, 2),
        "max_drop_after_pct": round((float(post["Low"].min()) / sell_price - 1) * 100, 2),
        "until": tools._fmt_date(post.index[-1]),
    }


# ============================================================
# 三大法人（理由有提到才抓）
# ============================================================

def fetch_institutional(code: str, days: int) -> pd.DataFrame:
    kf = tools.core()
    fetch = getattr(kf, "fetch_inst_60d_from_finmind_token", None)
    if fetch is None:
        raise tools.ToolDataError("主程式沒有三大法人函式")
    frame = fetch(code, days=days)
    if frame is None or frame.empty:
        raise tools.ToolDataError("三大法人資料為空")
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    return frame


INST_KEEP_DAYS = 30          # 理由常見最長寫到「近一個月」＝20 個交易日，多留一些緩衝


def inst_summary(frame: pd.DataFrame, buy_day: pd.Timestamp) -> Dict[str, Any]:
    def streak(col: str) -> int:
        """到買進日為止連續買超幾天（最多看 INST_KEEP_DAYS 天，「連 7 天」也算得出來）。"""
        count = 0
        for value in reversed(history[col].tolist()):
            if value > 0:
                count += 1
            else:
                break
        return count

    out = {"buy_day": {}, "before_5d_sum": {}, "buy_streak_days": {}, "after_sum": {}}
    frame = frame.assign(total=frame["foreign"] + frame["invest"] + frame["dealer"])
    before = frame[frame["Date"] <= buy_day].tail(5)
    after = frame[frame["Date"] > buy_day]
    history = frame[frame["Date"] <= buy_day].tail(INST_KEEP_DAYS)
    last = before.iloc[-1] if not before.empty else None
    for col, label in (("foreign", "外資"), ("invest", "投信"), ("dealer", "自營商"), ("total", "三大法人")):
        out["buy_day"][label] = round(float(last[col])) if last is not None else None
        out["before_5d_sum"][label] = round(float(before[col].sum())) if not before.empty else None
        out["buy_streak_days"][label] = streak(col) if not before.empty else None
        out["after_sum"][label] = round(float(after[col].sum())) if not after.empty else None
    # 買進日（含）以前每日買賣超，給「近 N 日」核對用（舊→新）
    out["recent"] = [{"date": tools._fmt_date(r.Date), "外資": round(float(r.foreign)),
                      "投信": round(float(r.invest)), "自營商": round(float(r.dealer)),
                      "三大法人": round(float(r.total))}
                     for r in history.itertuples()]
    out["unit"] = "張"
    return out


# ============================================================
# 理由核對：✅ 成立／⚠️ 部分成立／❌ 不成立／❓ 資料不足
# ============================================================

_CN_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_MA_WORDS = (("MA5", r"5日線|五日線|週線|周線|MA5"), ("MA10", r"10日線|十日線|MA10"),
             ("MA20", r"月線|20日線|二十日線|MA20|中軌"), ("MA60", r"季線|60日線|六十日線|MA60"))


# 自動斷詞用的「主題錨點」：一條理由通常圍繞一個錨點（KD、布林、外資、月線…）。長的寫前面，
# 「均線多頭排列」才不會被拆成「均線」＋「多頭排列」。
_ANCHOR_RE = re.compile(
    r"均線多頭排列|均線空頭排列|多頭排列|空頭排列|KD|K值|D值|MACD|布林|外資|投信|自營商?|三大法人|法人|"
    r"5日線|五日線|10日線|十日線|20日線|60日線|週線|周線|月線|季線|半年線|年線|MA\d+|均線|"
    r"爆量|量增|放量|出量|大量|前高|新高|頸線|缺口|權證|分點|主力|券商|大戶",
    re.IGNORECASE)
# 放在錨點前面的動作詞屬於「後面」那個錨點：「KD向上突破布林」→「KD向上」＋「突破布林」
_PREFIX_VERB_RE = re.compile(r"(突破|站上|站回|跌破|跌落|失守|守住|回測|回踩|沿著|沿|貼著|碰到|觸及)$")
_CONNECTOR_ONLY_RE = re.compile(r"^(和|與|跟|及|、|還有|以及)?$")
_FAMILIES = (("權證", "分點", "主力", "券商", "大戶"),)


def _same_family(a: str, b: str) -> bool:
    return any(a in fam and b in fam for fam in _FAMILIES)


def _segment(text: str) -> List[str]:
    """一段沒有標點的理由依錨點切開。兩個錨點中間的字，句尾是動作詞就歸後面，其餘歸前面。"""
    anchors = list(_ANCHOR_RE.finditer(text))
    if len(anchors) <= 1:
        return [text] if text else []
    cuts = [0]
    for prev, nxt in zip(anchors, anchors[1:]):
        gap = text[prev.end():nxt.start()]
        if not gap and _same_family(prev.group(0), nxt.group(0)):
            continue                                  # 「權證分點」「主力券商」是同一件事，不拆
        verb = _PREFIX_VERB_RE.search(gap)
        cuts.append(prev.end() + verb.start() if verb else nxt.start())
    cuts.append(len(text))
    pieces = [text[a:b] for a, b in zip(cuts, cuts[1:])]
    # 「外資和投信買超」→ 前一段只有錨點＋連接詞，就借用後一段的述語 →「外資買超」「投信買超」
    out: List[str] = []
    for i, piece in enumerate(pieces):
        hit = _ANCHOR_RE.search(piece)
        rest = piece[hit.end():] if hit else piece
        if hit and _CONNECTOR_ONLY_RE.match(rest) and i + 1 < len(pieces):
            nxt = _ANCHOR_RE.search(pieces[i + 1])
            if nxt:
                piece = piece[:hit.end()] + pieces[i + 1][nxt.end():]
        out.append(piece)
    return out


def _split_claims(reason: str) -> List[str]:
    """理由拆成一條一條：先依標點／連接詞切，每段再去掉空白後依錨點自動斷詞。
    「KD 黃金交叉」（空白）仍是一條；「KD向上 突破布林」「KD黃金交叉外資買超」會拆成兩條。"""
    parts = re.split(r"[，,、；;。\n/／|｜]|以及|並且|而且|加上|還有", str(reason or ""))
    claims: List[str] = []
    for part in parts:
        compact = re.sub(r"\s+", "", part)
        claims += [c for c in _segment(compact) if len(c) >= 2]
    return claims


def _cn_int(raw: str) -> Optional[int]:
    """「3」「三」「十」「十五」「二十」→ 整數。"""
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        head, _, tail = raw.partition("十")
        return (_CN_NUM.get(head, 1) if head else 1) * 10 + (_CN_NUM.get(tail, 0) if tail else 0)
    return _CN_NUM.get(raw)


def _n_days(text: str) -> int:
    hit = re.search(r"連\s*(\d+|[一二兩三四五六七八九十]+)\s*[天日]", text)
    if not hit:
        return 1
    return _cn_int(hit.group(1)) or 1


def _window_days(text: str) -> Optional[int]:
    """理由裡寫的期間：近5日／近10天／最近3日／5日內／一週＝5／兩週＝10／一個月＝20；沒寫回 None。"""
    hit = re.search(r"(?:近|最近|過去)\s*(\d+|[一二兩三四五六七八九十]+)\s*(?:個)?(?:交易)?[日天]|"
                    r"(\d+|[一二兩三四五六七八九十]+)\s*(?:個)?(?:交易)?[日天]內", text)
    if hit:
        return _cn_int(hit.group(1) or hit.group(2))
    if re.search(r"兩週|二週|兩周|雙週", text):
        return 10
    if re.search(r"一週|本週|這週|一周|近週", text):
        return 5
    if re.search(r"一個月|近月|這個月|本月", text):
        return 20
    return None


def _inst_window(inst: Dict[str, Any], label: str, days: int) -> Optional[Dict[str, Any]]:
    rows = (inst.get("recent") or [])[-days:]
    if not rows:
        return None
    values = [r.get(label) or 0 for r in rows]
    return {"days": len(rows), "sum": sum(values), "buy_days": sum(1 for v in values if v > 0),
            "sell_days": sum(1 for v in values if v < 0),
            "start": str(rows[0]["date"])[5:], "end": str(rows[-1]["date"])[5:]}


def _check_inst_window(claim: str, label: str, inst: Dict[str, Any], day: str, value: int,
                       windows: List[int]) -> Dict[str, str]:
    """期間型法人理由：寫幾天就看幾天；沒寫就算 5 日與 10 日，取合計買超（或賣超）較明顯的那個。"""
    selling = "賣" in claim
    stats = [w for w in (_inst_window(inst, label, n) for n in windows) if w]
    if not stats:
        return _result(claim, "❓", f"{label}資料不足")
    pick = (min if selling else max)(stats, key=lambda w: w["sum"])
    side_days = pick["sell_days"] if selling else pick["buy_days"]
    evidence = (f"{pick['start']}～{pick['end']} 近 {pick['days']} 日{label}合計"
                f"{'買' if pick['sum'] >= 0 else '賣'}超 {abs(pick['sum']):,} 張，"
                f"{pick['days']} 天中 {side_days} 天{'賣' if selling else '買'}超")
    if value and ((value < 0) != selling):
        evidence += f"（買進當天{'買' if value > 0 else '賣'}超 {abs(value):,} 張）"
    ok_sum = pick["sum"] < 0 if selling else pick["sum"] > 0
    if ok_sum and side_days >= pick["days"] * 0.6:
        return _result(claim, "✅", evidence)
    if ok_sum:
        return _result(claim, "⚠️", evidence)
    return _result(claim, "❌", evidence)


def _result(claim: str, status: str, evidence: str) -> Dict[str, str]:
    return {"claim": claim, "status": status, "status_text": STATUS_TEXT[status], "evidence": evidence}


def _check_kd(claim: str, kd: Dict[str, Any], day: str) -> Dict[str, str]:
    """KD 各種說法：黃金交叉／死亡交叉／高檔鈍化／低檔／向下；其餘（向上、翻揚、轉強、只寫 KD）當成向上。"""
    k, d, kp = kd.get("K9"), kd.get("D9"), kd.get("K_prev")
    if k is None or d is None:
        return _result(claim, "❓", "KD 資料不足")
    values = f"{day} K {k:.1f}／D {d:.1f}"
    if re.search(r"金叉|黃金交叉|交叉向上", claim):
        ago = kd.get("cross_days_ago")
        if ago is not None:
            return _result(claim, "✅", f"{values}，{'當日' if ago == 0 else f'{ago} 天前'}黃金交叉")
        if k > d:
            return _result(claim, "⚠️", f"{values}，K 在 D 之上，但近 3 日沒有交叉")
        return _result(claim, "❌", f"{values}，K 仍在 D 之下")
    if re.search(r"死叉|死亡交叉|交叉向下", claim):
        ago = kd.get("death_cross_days_ago")
        if ago is not None:
            return _result(claim, "✅", f"{values}，{'當日' if ago == 0 else f'{ago} 天前'}死亡交叉")
        if k < d:
            return _result(claim, "⚠️", f"{values}，K 在 D 之下，但近 3 日沒有交叉")
        return _result(claim, "❌", f"{values}，K 仍在 D 之上")
    if re.search(r"高檔|鈍化|超買", claim):
        last3 = [v for v in kd.get("K_last3") or [] if v is not None]
        if len(last3) >= 3 and min(last3) >= 80:
            return _result(claim, "✅", f"{values}，K 連 3 日在 80 以上（高檔鈍化）")
        if k >= 80:
            return _result(claim, "⚠️", f"{values}，K 在 80 以上，但未連 3 日")
        return _result(claim, "❌", f"{values}，K 未達 80")
    if re.search(r"低檔|超賣", claim):
        return _result(claim, "✅" if k <= 20 else "⚠️" if k <= 30 else "❌", f"{values}，K 值 {k:.1f}")
    if kp is None:
        return _result(claim, "❓", "缺前一日 KD，無法判斷方向")
    rising = k > kp
    move = f"{values}（前一日 K {kp:.1f}），K 值{'上升' if rising else '下降' if k < kp else '持平'}"
    if re.search(r"向下|下彎|轉弱|翻空|走弱|往下", claim):
        falling = k < kp
        if falling and k < d:
            return _result(claim, "✅", move + "，且 K 在 D 之下")
        return _result(claim, "⚠️" if falling or k < d else "❌", move + f"，K {'在' if k < d else '高於'} D")
    # 向上／翻揚／轉強／上揚／勾頭／只寫 KD：K 值上升＋K 在 D 之上＝成立，只符合一項＝部分成立
    above = k > d
    if rising and above:
        return _result(claim, "✅", move + "，且 K 在 D 之上")
    if rising or above:
        return _result(claim, "⚠️", move + f"，K {'在 D 之上' if above else '仍低於 D'}")
    return _result(claim, "❌", move + "，且 K 低於 D")


def check_claim(claim: str, snap: Dict[str, Any], inst: Optional[Dict[str, Any]],
                warrant_events: Optional[Dict[str, Any]]) -> Dict[str, str]:
    close = snap.get("close")
    day = str(snap.get("date") or "")[5:]
    if re.search(_NOT_AUTO_RE, claim):                    # 「營收創新高」「W底」不能當成價格新高／均線來判
        return _result(claim, "❓", "屬於型態／基本面／消息面理由，系統不自動核對")
    concept = _check_concepts(claim, snap, day)          # 均線方向／糾結／交叉、布林壓縮、量縮、K 棒、MACD…
    if concept:
        return concept
    for key, pattern in _MA_WORDS:
        if re.search(pattern, claim):
            if re.search(_SLOPE_WORDS, claim) and not re.search(r"站上|跌破|站回|失守|突破|回測|守住", claim):
                return _check_ma_slope(claim, snap, [key], day)
            ma = (snap["moving_averages"].get(key) or {}).get("value")
            if ma is None or close is None:
                return _result(claim, "❓", f"{key} 資料不足")
            above = close > ma
            want_above = not re.search(r"跌破|跌落|失守|在.*之下|低於", claim)
            return _result(claim, "✅" if above == want_above else "❌",
                           f"{day} 收盤 {close:g}，{key} {ma:g}（{'站上' if above else '跌破'}）")
    if re.search(r"多頭排列", claim):
        align = snap.get("ma_alignment")
        return _result(claim, "✅" if align == "多頭排列" else "❌", f"{day} 均線{align}")
    if re.search(r"布林", claim):
        bb = snap.get("bollinger") or {}
        if re.search(r"開口|擴張|擴大|張口", claim):
            trend = [w for w in bb.get("bandwidth_trend") or [] if w is not None]
            if len(trend) < 3:
                return _result(claim, "❓", "布林帶寬資料不足")
            ups = sum(1 for a, b in zip(trend[-3:], trend[-2:]) if b > a)
            status = "✅" if ups == 2 else "⚠️" if ups == 1 else "❌"
            return _result(claim, status, "帶寬 " + " → ".join(f"{w:.1f}%" for w in trend[-3:]))
        if re.search(r"下軌", claim) and bb.get("lower") and close is not None:
            below = close <= bb["lower"]
            want_below = bool(re.search(r"跌破|跌落|失守|低於", claim))
            return _result(claim, "✅" if below == want_below else "❌",
                           f"{day} 收盤 {close:g}，下軌 {bb['lower']:g}")
        # 「突破布林」「布林突破」「站上布林上軌」「布林壓縮突破」都當成向上突破上軌來核對
        if (re.search(r"上軌", claim) or re.search(r"突破|站上|衝出|噴出", claim)) and bb.get("upper") and close is not None:
            gap = (close / bb["upper"] - 1) * 100
            ago = bb.get("upper_break_days_ago")
            evidence = f"{day} 收盤 {close:g}，布林上軌 {bb['upper']:g}（{gap:+.2f}%）"
            squeeze = any("壓縮" in str(s) and "突破" in str(s) for s in bb.get("signals") or [])
            if gap >= 0:
                return _result(claim, "✅", evidence + ("，壓縮後向上突破" if squeeze else ""))
            if ago is not None:
                return _result(claim, "⚠️", evidence + f"，{ago} 天前收盤曾站上上軌，當天已回到軌道內")
            if gap >= -1:
                return _result(claim, "⚠️", evidence + "，貼近上軌但收盤未站上")
            return _result(claim, "❌", evidence + "，收盤仍在上軌之下")
    if re.search(r"爆量|量增|放量|出量|大量", claim):
        vol, mv5 = snap.get("volume_lots"), snap.get("mv5_lots")
        if not vol or not mv5:
            return _result(claim, "❓", "成交量資料不足")
        ratio = vol / mv5
        status = "✅" if ratio >= 1.5 else "⚠️" if ratio >= 1.2 else "❌"
        return _result(claim, status, f"{day} 成交 {vol:,} 張，為 5 日均量 {mv5:,} 張的 {ratio:.1f} 倍")
    if re.search(r"KD|K值|K線值|D值", claim, re.IGNORECASE):
        return _check_kd(claim, snap.get("kd") or {}, day)
    if re.search(r"突破.*(前高|新高|20日高)", claim):
        high = snap.get("high_20d")
        if high is None or close is None:
            return _result(claim, "❓", "資料不足")
        gap = (close / high - 1) * 100
        status = "✅" if gap >= 0 else "⚠️" if gap >= -1 else "❌"
        return _result(claim, status, f"{day} 收盤 {close:g}，近 20 日高 {high:g}")
    for label, keyword in (("外資", "外資"), ("投信", "投信"), ("自營商", "自營"), ("三大法人", "法人")):
        if keyword in claim and re.search(r"買超|賣超|買|賣", claim):
            if not inst:
                return _result(claim, "❓", "三大法人資料取不到")
            value = inst["buy_day"].get(label)
            if value is None:
                return _result(claim, "❓", f"{label}資料不足")
            # ① 「連 N 天」：看到買進日為止連續買超幾天
            if re.search(r"連\s*(\d+|[一二兩三四五六七八九十]+)\s*[天日]", claim):
                need = _n_days(claim)
                streak = inst["buy_streak_days"].get(label)
                evidence = f"{day} {label}{'買' if value >= 0 else '賣'}超 {abs(value):,} 張，連續買超 {streak} 天"
                if "賣" in claim:
                    return _result(claim, "✅" if value < 0 else "❌", evidence)
                if streak is not None and streak >= need:
                    return _result(claim, "✅", evidence)
                if value > 0:
                    return _result(claim, "⚠️", evidence + f"（理由寫連 {need} 天）")
                return _result(claim, "❌", evidence)
            # ② 「當天／當日」：只看買進日
            if re.search(r"當天|當日|今天|今日", claim):
                selling = "賣" in claim
                ok = value < 0 if selling else value > 0
                return _result(claim, "✅" if ok else "❌",
                               f"{day} {label}{'買' if value >= 0 else '賣'}超 {abs(value):,} 張")
            # ③ 寫了「近 N 日」就看 N 日；沒寫就算 5 日與 10 日，取較明顯的那個
            window = _window_days(claim)
            return _check_inst_window(claim, label, inst, day, value, [window] if window else [5, 10])
    if _WARRANT_RE.search(claim) or (isinstance(warrant_events, dict)
                                     and any(str(e.get("branch") or "") and str(e.get("branch")) in claim
                                             for e in warrant_events.get("all") or [])):
        return _check_warrant(claim, warrant_events)
    return dict(_result(claim, "❓", "系統沒有可對照的資料"), unmatched=True)


# ============================================================
# 常見技術面說法（規則層）：全部只用買進日（含）以前的資料
# ============================================================

_SLOPE_WORDS = r"上揚|向上|翻揚|走揚|上彎|揚升|抬頭|翻多|下彎|向下|走平|翻空|下滑|走弱|轉弱|轉強"
_NOT_AUTO_RE = r"W底|M頭|頭肩|杯柄|旗形|三角收斂|營收|財報|EPS|法說|新聞|消息|題材|利多|政策|感覺|直覺"


def _slope(snap: Dict[str, Any], key: str) -> Optional[float]:
    now = (snap["moving_averages"].get(key) or {}).get("value")
    before = (snap.get("ma_prev3") or {}).get(key)
    return None if now is None or before is None else now - before


def _check_ma_slope(claim: str, snap: Dict[str, Any], keys: List[str], day: str) -> Dict[str, str]:
    """均線方向（和 3 根前比）：單一均線看它自己；「均線向上」看 MA5／MA10／MA20 幾條在上揚。"""
    down = bool(re.search(r"下彎|向下|翻空|下滑|走弱|轉弱", claim))
    parts, moving = [], 0
    for key in keys:
        s = _slope(snap, key)
        value = (snap["moving_averages"].get(key) or {}).get("value")
        if s is None or not value:
            continue
        flat = abs(s) / value < 0.001
        state = "走平" if flat else ("上揚" if s > 0 else "下彎")
        parts.append(f"{key} {state}（3 日 {s:+.2f}）")
        moving += 1 if (not flat and ((s < 0) if down else (s > 0))) else 0
    if not parts:
        return _result(claim, "❓", "均線資料不足")
    evidence = f"{day} " + "、".join(parts)
    if moving == len(parts):
        return _result(claim, "✅", evidence)
    return _result(claim, "⚠️" if moving else "❌", evidence)


def _check_concepts(claim: str, snap: Dict[str, Any], day: str) -> Optional[Dict[str, str]]:
    """規則認得的常見說法；認不得回 None，交給原本的均線／布林／KD／法人判斷。"""
    close = snap.get("close")
    mas = {k: (v or {}).get("value") for k, v in (snap.get("moving_averages") or {}).items()}
    # 均線整體方向：均線向上／均線上揚／均線翻揚（沒指定哪一條）
    if re.search(r"均線", claim) and re.search(_SLOPE_WORDS, claim) and not re.search(r"排列|糾結|交叉", claim):
        return _check_ma_slope(claim, snap, ["MA5", "MA10", "MA20"], day)
    if re.search(r"均線.*(糾結|黏合|收斂|整理)|(糾結|黏合)", claim):
        vals = [mas.get(k) for k in ("MA5", "MA10", "MA20")]
        if None in vals or not close:
            return _result(claim, "❓", "均線資料不足")
        spread = (max(vals) - min(vals)) / close * 100
        status = "✅" if spread <= 3 else "⚠️" if spread <= 5 else "❌"
        return _result(claim, status, f"{day} MA5／MA10／MA20 最大差距 {spread:.1f}%（3% 內算糾結）")
    if re.search(r"均線", claim) and re.search(r"黃金交叉|金叉|死亡交叉|死叉", claim):
        a, b = snap.get("ma5_hist") or [], snap.get("ma20_hist") or []
        if len(a) < 2 or len(b) < 2 or None in a + b:
            return _result(claim, "❓", "均線資料不足")
        up = not re.search(r"死亡|死叉", claim)
        crossed = any(((a[i - 1] <= b[i - 1] and a[i] > b[i]) if up else (a[i - 1] >= b[i - 1] and a[i] < b[i]))
                      for i in range(1, len(a)))
        side = a[-1] > b[-1] if up else a[-1] < b[-1]
        evidence = f"{day} MA5 {a[-1]:g}／MA20 {b[-1]:g}"
        if crossed:
            return _result(claim, "✅", evidence + f"，近 3 日 MA5 {'上穿' if up else '下穿'} MA20")
        return _result(claim, "⚠️" if side else "❌", evidence + "，近 3 日沒有交叉")
    # 布林壓縮／收斂／收窄／帶寬縮小
    if re.search(r"布林", claim) and re.search(r"壓縮|收斂|收窄|縮口|帶寬縮|帶寬低|窄", claim):
        hist = snap.get("bb_width_hist") or []
        if len(hist) < 20:
            return _result(claim, "❓", "布林帶寬歷史不足")
        now = hist[-1]
        rank = sum(1 for w in hist if w <= now) / len(hist) * 100
        status = "✅" if rank <= 20 else "⚠️" if rank <= 35 else "❌"
        return _result(claim, status, f"{day} 帶寬 {now:.1f}%，在近 {len(hist)} 日由窄到寬排第 {rank:.0f} 百分位"
                                      "（20 以內算壓縮）")
    vol, mv5 = snap.get("volume_lots"), snap.get("mv5_lots")
    if re.search(r"量縮|量能萎縮|窒息量|量能縮|縮量|量少", claim):
        if not vol or not mv5:
            return _result(claim, "❓", "成交量資料不足")
        ratio = vol / mv5
        status = "✅" if ratio <= 0.7 else "⚠️" if ratio <= 0.85 else "❌"
        return _result(claim, status, f"{day} 成交 {vol:,} 張，為 5 日均量 {mv5:,} 張的 {ratio:.2f} 倍")
    if re.search(r"價漲量增|量價齊揚|帶量上漲|帶量", claim):
        change = snap.get("change_pct")
        if not vol or not mv5 or change is None:
            return _result(claim, "❓", "量價資料不足")
        ok_price, ok_vol = change > 0, vol > mv5
        status = "✅" if ok_price and ok_vol else "⚠️" if ok_price or ok_vol else "❌"
        return _result(claim, status, f"{day} 漲跌 {change:+.2f}%，成交量為 5 日均量 {vol / mv5:.2f} 倍")
    if re.search(r"長紅|大紅K|紅K|紅棒|大漲|漲停", claim):
        change, open_ = snap.get("change_pct"), snap.get("open")
        if change is None or open_ is None or close is None:
            return _result(claim, "❓", "K 棒資料不足")
        red = close > open_
        need = 9.5 if "漲停" in claim else 3 if re.search(r"長紅|大紅|大漲", claim) else 0
        ok = red and change >= need
        status = "✅" if ok else "⚠️" if red or change > 0 else "❌"
        return _result(claim, status, f"{day} 開 {open_:g}、收 {close:g}，漲跌 {change:+.2f}%")
    if re.search(r"跳空|缺口", claim):
        low, prev_high = snap.get("low"), snap.get("prev_high")
        if low is None or prev_high is None:
            return _result(claim, "❓", "K 棒資料不足")
        return _result(claim, "✅" if low > prev_high else "❌",
                       f"{day} 最低 {low:g}，前一日最高 {prev_high:g}（{'有' if low > prev_high else '沒有'}向上跳空）")
    if re.search(r"創新高|新高|創高", claim):
        high = snap.get("prior_high_60d")
        if high is None or close is None:
            return _result(claim, "❓", "資料不足")
        gap = (close / high - 1) * 100
        status = "✅" if gap >= 0 else "⚠️" if gap >= -1 else "❌"
        return _result(claim, status, f"{day} 收盤 {close:g}，前 60 日最高 {high:g}（{gap:+.2f}%）")
    if re.search(r"突破.*(整理|盤整|區間|箱型|平台|壓力)", claim):
        high = snap.get("prior_high_20d")
        if high is None or close is None:
            return _result(claim, "❓", "資料不足")
        gap = (close / high - 1) * 100
        status = "✅" if gap >= 0 else "⚠️" if gap >= -1 else "❌"
        return _result(claim, status, f"{day} 收盤 {close:g}，前 20 日最高 {high:g}（{gap:+.2f}%）")
    if re.search(r"回測|拉回|回踩", claim):
        key = next((k for k, pat in _MA_WORDS if re.search(pat, claim)), "MA20")
        ma, low = mas.get(key), snap.get("low")
        if ma is None or low is None or close is None:
            return _result(claim, "❓", "資料不足")
        touched = low <= ma * 1.015
        held = close >= ma
        status = "✅" if touched and held else "⚠️" if held else "❌"
        return _result(claim, status, f"{day} 最低 {low:g}、收盤 {close:g}，{key} {ma:g}"
                                      f"（{'有' if touched else '沒有'}回測到、{'守住' if held else '跌破'}）")
    if re.search(r"MACD|柱狀體|OSC", claim, re.IGNORECASE):
        osc = [v for v in snap.get("osc_hist") or [] if v is not None]
        if len(osc) < 2:
            return _result(claim, "❓", "MACD 資料不足")
        down = bool(re.search(r"翻綠|轉負|死亡交叉|死叉|向下|轉弱", claim))
        turned = any(((osc[i - 1] <= 0 < osc[i]) if not down else (osc[i - 1] >= 0 > osc[i]))
                     for i in range(1, len(osc)))
        on_side = osc[-1] < 0 if down else osc[-1] > 0
        evidence = f"{day} MACD 柱狀體 {osc[-1]:+.3f}"
        if turned:
            return _result(claim, "✅", evidence + f"，近 3 日{'翻綠' if down else '翻紅'}")
        return _result(claim, "⚠️" if on_side else "❌", evidence + "，近 3 日沒有翻轉")
    return None


# AI 翻譯層：規則認不得的說法，請 Gemini 對應到下面其中一個「標準說法」，再用規則判斷。
# Gemini 只負責「這句話是什麼意思」，對錯仍由程式用數字決定。
CANONICAL_CLAIMS = (
    "站上5日線", "站上10日線", "站上月線", "站上季線", "跌破5日線", "跌破月線", "跌破季線",
    "均線向上", "均線向下", "5日線上揚", "月線上揚", "季線上揚", "月線下彎",
    "均線多頭排列", "均線空頭排列", "均線糾結", "均線黃金交叉", "均線死亡交叉",
    "布林壓縮", "布林開口", "突破布林上軌", "跌破布林下軌", "站上布林中軌",
    "爆量", "量縮", "價漲量增", "長紅K", "紅K", "漲停", "跳空缺口",
    "突破整理區", "突破前高", "創新高", "回測月線不破", "回測10日線不破",
    "KD黃金交叉", "KD死亡交叉", "KD向上", "KD向下", "KD高檔鈍化", "KD低檔",
    "MACD翻紅", "MACD翻綠",
    "外資買超", "外資賣超", "投信買超", "投信賣超", "三大法人買超", "權證分點買進",
    "無法核對",
)


def claim_map_prompt(claims: List[str]) -> str:
    return ("以下是台股投資人寫的進場理由片段。請把每一句對應到 options 裡意思最接近的一個標準說法；"
            "意思不在清單裡、或屬於型態、基本面、消息面、主觀感覺的，填「無法核對」。不要自己判斷對錯。\n"
            f"options：{json.dumps(list(CANONICAL_CLAIMS), ensure_ascii=False)}\n"
            f"claims：{json.dumps(claims, ensure_ascii=False)}\n回傳 JSON。")


CLAIM_MAP_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"claim": {"type": "string"}, "canonical": {"type": "string", "enum": list(CANONICAL_CLAIMS)}},
        "required": ["claim", "canonical"]}}},
    "required": ["items"],
}


def remap_unmatched(checks: List[Dict[str, Any]], mapper, snap: Dict[str, Any], inst: Optional[Dict[str, Any]],
                    warrant_events: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把規則認不得的理由交給 mapper（Gemini）翻成標準說法後重新核對；mapper 失敗就維持 ❓。"""
    pending = [c["claim"] for c in checks if c.get("unmatched")]
    if not pending or mapper is None:
        return checks
    try:
        mapping = {str(i.get("claim")): str(i.get("canonical")) for i in (mapper(pending) or {}).get("items") or []}
    except Exception as exc:
        print(f"⚠️ 理由翻譯略過｜{type(exc).__name__}: {exc}", flush=True)
        return checks
    out = []
    for c in checks:
        canonical = mapping.get(c["claim"])
        if c.get("unmatched") and canonical in CANONICAL_CLAIMS and canonical != "無法核對":
            again = check_claim(canonical, snap, inst, warrant_events)
            if not again.get("unmatched"):
                again = dict(again, claim=c["claim"], evidence=f"依「{canonical}」核對｜{again['evidence']}",
                             mapped_to=canonical)
                print(f"   理由翻譯｜{c['claim']} → {canonical}｜{again['status']}", flush=True)
                out.append(again)
                continue
        out.append({k: v for k, v in c.items() if k != "unmatched"})
    return out


def _event_text(e: Dict[str, Any]) -> str:
    return f"{str(e.get('buy_date') or '')[5:]} {e.get('event') or ''} {e.get('buy_amount_text') or ''}".strip()


def _check_warrant(claim: str, info: Any) -> Dict[str, str]:
    """權證分點理由：理由有點名分點（例：國票台南）就只看那個分點；只算買進日（含）以前 10 個交易日的 A～E 買進。"""
    if not isinstance(info, dict):
        return _result(claim, "❓", "權證分點資料取不到")
    window, every = info.get("window") or [], info.get("all") or []
    named = sorted({str(e.get("branch")) for e in every if e.get("branch") and str(e.get("branch")) in claim},
                   key=len, reverse=True)
    if named:
        branch = named[0]
        hits = [e for e in window if str(e.get("branch")) == branch]
        if hits:
            big = any(str(e.get("event") or "") in ("D", "E") for e in hits)
            text = f"{branch} 買進前 10 日有 {len(hits)} 筆事件：" + "、".join(_event_text(e) for e in hits[:3])
            # 理由寫「大買／大額」但只有 A～C 小額事件＝部分成立
            if re.search(r"大買|大額|重押|大量買", claim) and not big:
                return _result(claim, "⚠️", text + "（沒有 D／E 大額事件）")
            return _result(claim, "✅", text)
        later = [e for e in every if str(e.get("branch")) == branch and str(e.get("buy_date")) > str(info.get("buy_date"))]
        return _result(claim, "❌", f"{branch} 買進前 10 日沒有 A～E 事件"
                       + (f"（買進後才出現：{_event_text(later[0])}，下單時看不到）" if later else ""))
    if window:
        return _result(claim, "✅", f"買進前 10 日有 {len(window)} 筆分點事件：" + "、".join(
            f"{e.get('branch')} {_event_text(e)}" for e in window[:2]))
    return _result(claim, "❌", "買進前 10 日沒有 A～E 分點事件")


# ============================================================
# 目前結構（只給 Gemini 寫「目前觀察」用；圖片上不再另列一張持股狀態卡，K 線數值卡已經有這些數字）
# ============================================================

def _current_facts(code: str, cost: float) -> Dict[str, Any]:
    """現在的型態、均線、布林與最近支撐；任何一步失敗就回空 dict，筆記照寫。"""
    try:
        tech = tools.get_technical_analysis(code)
        vp = tools.get_volume_profile(code)
        levels = tools.key_price_levels(tech, vp)
    except Exception as exc:
        print(f"⚠️ 覆盤目前結構略過｜{code}｜{type(exc).__name__}: {exc}", flush=True)
        return {}
    close = levels.get("close")
    return {
        "data_basis": tech.get("signal_status"),
        "close": close,
        "unrealized_pct": tools._pct(close, cost) if close else None,
        "pattern_label": vp.get("pattern_label"),
        "ma_alignment": tech.get("ma_alignment"),
        "ma20_distance_pct": ((tech.get("moving_averages") or {}).get("MA20") or {}).get("distance_pct"),
        "bollinger_signals": (tech.get("bollinger") or {}).get("signals"),
        "position_vs_two_zones": vp.get("position_vs_two_zones"),
        # 目前觀察只拿均線／量區當防守位；布林上軌是強勢時的「壓力／通道」，拿來當防守位不自然
        "nearest_supports": [lv for lv in levels.get("supports") or []
                             if "上軌" not in str(lv.get("label", ""))][:2],
    }


def _summary_lines(result: Dict[str, Any], buy_price: float, trades: List[Dict[str, Any]], closed: bool) -> List[str]:
    """K 線下方的小交易摘要列（兩行），取代原本整張「持股狀態」卡。"""
    buy = f"{trades[0]['date'][5:]} 買進 {buy_price:,.0f}" if buy_price >= 100 else f"{trades[0]['date'][5:]} 買進 {buy_price:g}"
    if closed:
        sell = trades[1]
        first = (f"{buy}｜{sell['date'][5:]} 賣出 {sell['price']:,.6g}｜持有 {result['trading_days']} 日"
                 f"｜實現 {result['return_pct']:+.2f}%")
    else:
        live = "（盤中）" if str(result.get("price_basis", "")).startswith("盤中") else ""
        first = (f"{buy}｜持有 {result['trading_days']} 日｜現價 {result['last_price']:,.6g}{live}"
                 f"｜{result['return_pct']:+.2f}%")
    second = f"最大浮盈 {result['max_gain_pct']:+.2f}%｜最大回撤 {result['max_drawdown_pct']:+.2f}%"
    return [first, second]


# ============================================================
# 組資料
# ============================================================

def build_review(req: Dict[str, Any], mapper=None) -> Dict[str, Any]:
    """回傳 {payload, panel}；payload 給 Gemini 與事實核對，panel 給 K 線圖卡。
    mapper(claims) → {"items": [{claim, canonical}]}：規則認不得的理由交給 Gemini 翻成標準說法（可省略）。"""
    code = req["code"]
    bundle = tools._load_price_bundle(code)
    # 買進當時用已收盤 K 棒；「到現在」用含盤中即時 K 的完整資料，損益才會和圖上的現價一致
    df = tools.closed_frame(bundle).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    live = bundle["df"].sort_index()
    live = live[~live.index.duplicated(keep="last")]
    idx = _index_on_or_after(df, req["buy_date"])
    if idx is None:
        raise tools.ToolDataError("買進日晚於目前最新的日 K 資料")
    if idx == 0:
        raise tools.ToolDataError(f"本地日 K 只回溯到 {tools._fmt_date(df.index[0])}，買進日太早，無法覆盤")
    live_idx = _index_on_or_after(live, req["buy_date"])
    sell_idx = _index_on_or_after(live, req["sell_date"]) if req.get("sell_date") else None
    if sell_idx is not None and sell_idx <= live_idx:  # 賣出日早於（或等於）買進日：視為沒填
        sell_idx = None
    buy_price = float(req.get("price") or df["Close"].iloc[idx])
    sell_price = None
    if sell_idx is not None:
        sell_price = float(req.get("sell_price") or live["Close"].iloc[sell_idx])
    snap = snapshot_at(df, idx)
    result = after_buy(live, live_idx, buy_price, sell_idx, sell_price)
    intraday = bundle.get("intraday") or {}
    result["price_basis"] = (f"盤中暫定（{intraday.get('time', '')}）" if intraday.get("is_live") and sell_idx is None
                             else "收盤")
    buy_day = pd.Timestamp(df.index[idx]).normalize()
    result["buy_date_text"] = tools._fmt_date(buy_day)[5:]
    closed = sell_idx is not None

    # 圖表視窗：從買進日往前 20 根一直畫到今天（至少 70、最多 140 根）
    bars_needed = min(CHART_MAX_BARS, max(CHART_MIN_BARS, len(df) - idx + CHART_BEFORE))
    panel = tools.get_chart_panel(code, with_marks=req["need_warrant"], lookback=bars_needed)

    warrant_events = None
    if req["need_warrant"]:
        # 只算買進日（含）以前 10 個交易日內的分點買進：買進之後才出現的事件，下單當下看不到，不能拿來證明理由
        dates = [bar["date"] for bar in panel.get("bars") or []]
        buy_text = tools._fmt_date(buy_day)
        window = set()
        if buy_text in dates:
            pos = dates.index(buy_text)
            window = set(dates[max(0, pos - 10):pos + 1])
        every = [e for e in (panel.get("marks") or {}).get("events") or [] if e.get("buy_date")]
        warrant_events = {"window": [e for e in every if e.get("buy_date") in window],
                          "all": every, "buy_date": buy_text}

    inst = frame = None
    need_inst = req["need_inst"] or bool(_INST_RE.search(req.get("sell_reason") or ""))
    if need_inst:
        try:
            frame = fetch_institutional(code, days=bars_needed + 10)
            inst = inst_summary(frame, buy_day)
            bar_dates = {bar["date"] for bar in panel.get("bars") or []}
            panel["institutional"] = [
                {"date": tools._fmt_date(r.Date), "foreign": float(r.foreign), "invest": float(r.invest),
                 "dealer": float(r.dealer)}
                for r in frame.itertuples() if tools._fmt_date(r.Date) in bar_dates]
        except Exception as exc:
            print(f"⚠️ 覆盤三大法人略過｜{code}｜{type(exc).__name__}: {exc}", flush=True)

    trades = [{"date": tools._fmt_date(buy_day), "price": buy_price, "side": "buy"}]
    if closed:
        trades.append({"date": tools._fmt_date(live.index[sell_idx]), "price": sell_price, "side": "sell"})
    panel["trades"] = trades
    panel["trade_summary_lines"] = _summary_lines(result, buy_price, trades, closed)
    panel["hide_trade_legend"] = True        # 覆盤卡的統計列已經有這些數字，K 線下方不重複

    current = {} if closed else _current_facts(code, buy_price)

    checks = [check_claim(c, snap, inst, warrant_events) for c in _split_claims(req["reason"])]
    checks = remap_unmatched(checks, mapper, snap, inst, warrant_events)
    sell_checks = []
    if closed and req.get("sell_reason"):
        # 出場理由也用「賣出當天（含）以前」的資料核對，不看賣後走勢
        sell_close_idx = _index_on_or_after(df, live.index[sell_idx].date())
        if sell_close_idx is not None:
            sell_snap = snapshot_at(df, sell_close_idx)
            sell_inst = inst_summary(frame, pd.Timestamp(df.index[sell_close_idx]).normalize()) \
                if frame is not None else None
            sell_checks = [check_claim(c, sell_snap, sell_inst, None) for c in _split_claims(req["sell_reason"])]
            sell_checks = remap_unmatched(sell_checks, mapper, sell_snap, sell_inst, None)
    payload = {
        "mode": "closed" if closed else "holding",
        "trade_id": f"{code}-{buy_day:%Y%m%d}-{int(time.time())}",
        "stock": {"code": code, "name": req.get("name") or panel.get("stock_name", "")},
        "trade": {"buy_date": tools._fmt_date(buy_day), "buy_price": buy_price,
                  "buy_price_source": "使用者提供" if req.get("price") else "買進日收盤價",
                  "lots": req.get("lots"), "entry_reasons": _split_claims(req["reason"]),
                  "entry_reason_raw": req["reason"],
                  "sell_date": trades[1]["date"] if closed else None, "sell_price": sell_price,
                  "sell_reason": req.get("sell_reason") or None},
        "sell_reason_checks": sell_checks,
        "at_buy": snap,
        "after_buy": result,
        "after_sell": after_sell(live, sell_idx, sell_price) if closed else None,
        "current": current or None,
        "reason_checks": checks,
        "institutional": inst,
        "warrant_events_before_buy": [
            {k: e.get(k) for k in ("no", "branch", "event", "buy_date", "buy_amount_text", "status")
             if e.get(k) not in (None, "")}
            for e in warrant_events["window"]][:10] if warrant_events is not None else None,
    }
    # 轉成純 JSON（numpy 數值、Timestamp 都先轉掉），後面的事實核對會直接 json.dumps(payload)
    payload = json.loads(json.dumps(payload, ensure_ascii=False, default=tools.json_safe))
    return {"payload": payload, "panel": panel}


# ============================================================
# Gemini：讀結構化事實，挑 2～4 個值得記錄的重點，寫成投資人自己的覆盤筆記（固定 JSON 欄位）
# ============================================================

HEADLINE_MAX, BODY_MAX, HIGHLIGHT_MAX, WATCH_MAX = 32, 120, 34, 40
HIGHLIGHT_FACETS = ("理由", "過程", "學習")     # 本次記住的三條固定分別講這三件事


def ai_schema(mode: str) -> Dict[str, Any]:
    last = "lesson" if mode == "closed" else "watch"
    return {"type": "object",
            "properties": {"headline": {"type": "string"}, "body": {"type": "string"},
                           "highlights": {"type": "array", "items": {"type": "string"}},
                           last: {"type": "string"}},
            "required": ["headline", "body", "highlights", last]}


REVIEW_PROMPT = """幫投資人把這筆交易寫成「自己的覆盤筆記」。review_data 是程式整理好的事實，你只負責讀懂、挑重點、用自然的話寫出來。

圖片上已經有統計列（買進日、買進價、現價、報酬、最大浮盈、最大回撤、持有天數）和逐條理由核對，
文字不要再把這些數字重報一次；要寫的是「這些數字代表什麼」。每筆交易重點不同，不要套同一個模板。

回傳 JSON：
- headline：一句話的覆盤結論，15～28 字，例如「分點籌碼理由有對到，但進場後仍經歷明顯震盪」。
  不要用「成功」「完美」「精準」「迎來」這類替結果打分數的字眼；結論講的是判斷，不是結果。
- body：60～100 字，像投資人自己寫的交易日記，自然、簡潔、客觀、台灣投資人口吻。
  講這筆交易的核心邏輯有沒有對到、進場後實際承受了什麼、最值得記住的一件事。
  統計列已有的數字最多只引用一個（通常是最大回撤），不要逐一列出日期、價格、報酬。
  好的例子：「這筆進場的核心是國票台南分點訊號，回頭核對確實有對到。進場後並不是一路上漲，中間最大回撤約 8%，之後才重新轉強。比較值得記的是，籌碼理由成立，但進場位置仍有不小的震盪空間。」
- highlights：剛好 3 條、每條最多 30 字，順序固定、三條講不同的事，不要三條都在講結果：
  1. 理由：原始進場邏輯是否成立（依 reason_checks）。
  2. 過程：這筆交易進場後真正承受了什麼風險（例如回撤多深、震盪多久）。
  3. 學習：下次同類型進場要注意或可沿用什麼。
  不要把「目前報酬多少」當成一條重點。
- watch（持倉中）：目前最值得觀察的「一件事」，最多 35 字，要具體、自然。
  若要提價位，只從 current.nearest_supports 挑最近的一個（例如「突破後能否守住 10 日線」）；
  不要拿布林上軌當防守位或風險條件，也不要列一串條件。
- lesson（已賣出）：這筆交易下次最值得沿用或修正的一件事，最多 35 字。

一定要遵守：
1. 所有數字（價格、報酬、KD、法人張數、日期）只能照抄 review_data，不能自己算、不能自己編。
2. 原始理由只看 trade.entry_reasons 與 reason_checks；reason_checks 的結果（✅ 成立／⚠️ 部分成立／❌ 不成立／❓ 資料不足）
   不能改判。不成立就直接講，資料不足就說資料不足，不要猜。
3. 避免事後諸葛：after_buy、current 是「進場後」才發生的事，只能寫成進場後的發展，
   不能說成當初買進正確的原因；不要因為最後賺錢，就把錯的進場理由合理化。
   禁止「主要歸功於」「多虧」「正是因為」「證明了」「果然」這類沒辦法證明的因果句。
4. 持倉中（mode=holding）不要寫最終交易結論；已賣出（mode=closed）才談實現報酬與出場理由（sell_reason_checks）。
5. 不要列一串 MA5、MA10、MA20，不要把每個指標都講一次；只留真正影響這筆判斷的資訊。
6. 不打招呼、不自我介紹、不說教（「請務必」「建議你應該」）、不勉勵、不給目標價或買賣指令。
7. after_buy.price_basis 是盤中暫定時，提到現價或報酬要註明「盤中」。

review_data：
"""


_PROMPT_DROP = ("bb_width_hist", "osc_hist", "ma5_hist", "ma20_hist", "K_last3", "bandwidth_trend", "ma_prev3",
                "recent")


def _slim(value: Any) -> Any:
    """給 Gemini 的資料拿掉核對用的長序列（帶寬歷史、每日法人…），省 token；核對結果已經在 reason_checks。"""
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items() if k not in _PROMPT_DROP}
    if isinstance(value, list):
        return [_slim(v) for v in value]
    return value


def build_prompt(payload: Dict[str, Any]) -> str:
    return REVIEW_PROMPT + json.dumps(_slim(payload), ensure_ascii=False, default=str)


def _clip(text: str, limit: int) -> str:
    """超過字數就截在最後一個完整句子（。！？；）；找不到才截在逗號並改成句號。
    不讓正文停在「…呈現多頭排列，」這種半句話。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    cut = value[:limit]
    stop = max(cut.rfind(p) for p in "。！？；")
    if stop >= limit * 0.45:
        return cut[:stop + 1]
    comma = cut.rfind("，")
    if comma >= limit * 0.45:
        return cut[:comma] + "。"
    return cut.rstrip("，、；") + "…"


def strip_hindsight(text: str) -> Tuple[str, List[str]]:
    """刪掉事後歸因的句子（Gemini 偶爾還是會寫），回傳 (剩下文字, 被刪句子)。"""
    kept, removed = [], []
    for sentence in re.findall(r"[^。！？\n]+[。！？]?", str(text or "")):
        (removed if _HINDSIGHT_RE.search(sentence) else kept).append(sentence)
    return "".join(kept).strip(), removed


def fallback_review(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini 失敗時的程式版本：原始理由＋核對結果＋目前觀察，一樣是 headline/body/highlights 格式。"""
    a, t = payload["after_buy"], payload["trade"]
    checks = payload["reason_checks"]
    bad = [c for c in checks if c["status"] in ("❌", "⚠️")]
    unknown = [c for c in checks if c["status"] == "❓"]
    if not checks:
        headline = "沒有可核對的進場理由"
    elif bad:
        headline = f"{len(bad)} 項進場理由與當天資料不完全相符"
    elif unknown:
        headline = "部分進場理由缺資料，無法核對"
    else:
        headline = "進場理由都與當天資料相符"
    body = (f"原始理由是{t.get('entry_reason_raw') or '、'.join(t['entry_reasons'])}，"
            f"核對結果見上方。進場後最大回撤 {a['max_drawdown_pct']:+.2f}%，這是進場後的走勢，不是原始理由。")
    good = [c["claim"] for c in checks if c["status"] == "✅"]
    wrong = [c["claim"] for c in checks if c["status"] in ("❌", "⚠️")]
    reason_line = (f"{'、'.join(wrong[:2])}與當天資料不完全相符" if wrong
                   else f"{'、'.join(good[:2])}有對到" if good else "進場理由缺資料可核對")
    process_line = (f"進場後曾回撤 {a['max_drawdown_pct']:+.2f}%，並非低風險位置" if a["max_drawdown_pct"] <= -5
                    else f"進場後最大回撤 {a['max_drawdown_pct']:+.2f}%，過程相對平穩")
    learn_line = (f"下次下單前先核對{wrong[0]}" if wrong else "訊號成立時，再搭配價格結構確認進場位置")
    highlights = [reason_line, process_line, learn_line]
    current = payload.get("current") or {}
    review = {"headline": headline, "body": body, "highlights": highlights}
    if payload["mode"] == "closed":
        post = payload.get("after_sell") or {}
        review["lesson"] = (f"賣出後 {post['days_available']} 個交易日收盤相對賣價 {post['close_change_pct']:+.2f}%"
                            if post.get("days_available") else "賣出後還沒有足夠的交易日可以比較")
    else:
        support = next((s for s in current.get("nearest_supports") or [] if s.get("label")), None)
        review["watch"] = (f"能否守住{support['label']}（{support['price']:g}）" if support and support.get("price")
                           else f"目前{current.get('pattern_label') or '結構'}，觀察突破後結構是否能維持"
                           if current else "目前結構資料暫時取不到")
    return review


def sanitize_review(data: Any, payload: Dict[str, Any], prune) -> Tuple[Dict[str, Any], List[str]]:
    """Gemini JSON → 每欄做數字核對（prune＝discord_ai_bot.prune_ungrounded_sentences）、刪事後歸因、限字數；
    某欄刪光就用 fallback 的同欄補，highlights 不足 2 條也補。回傳 (review, 被刪句子)。"""
    fallback = fallback_review(payload)
    data = data if isinstance(data, dict) else {}
    last = "lesson" if payload["mode"] == "closed" else "watch"
    removed_all: List[str] = []

    def clean(text: Any, limit: int) -> str:
        pruned, removed = prune(str(text or ""), payload)
        pruned, hindsight = strip_hindsight(pruned)
        removed_all.extend(removed + hindsight)
        return _clip(pruned, limit)

    review = {
        "headline": clean(data.get("headline"), HEADLINE_MAX) or fallback["headline"],
        "body": clean(data.get("body"), BODY_MAX) or fallback["body"],
        last: clean(data.get(last), WATCH_MAX) or fallback[last],
    }
    # 三條依序是 理由／過程／學習：哪一條被刪光，就用 fallback 同一個位置補，順序不亂
    raw = list(data.get("highlights") or [])[:3]
    raw += [""] * (3 - len(raw))
    review["highlights"] = [clean(h, HIGHLIGHT_MAX) or _clip(fb, HIGHLIGHT_MAX)
                            for h, fb in zip(raw, fallback["highlights"])]
    return review, removed_all


def review_text(payload: Dict[str, Any], review: Dict[str, Any]) -> str:
    """Discord 文字版（圖片以外的純文字、LOG 與存檔用）。"""
    last_label, last_key = (("下次", "lesson") if payload["mode"] == "closed" else ("目前觀察", "watch"))
    summary = "｜".join(text for text, _ in review_summary(payload))
    lines = [summary, "", f"我的理由｜{payload['trade'].get('entry_reason_raw', '')}"]
    lines += [f"{c['status']} {c['claim']}｜{c['evidence']}" for c in payload["reason_checks"]]
    lines += ["", review["headline"], review["body"], "", "交易心得"]
    lines += [f"• {h}" for h in review["highlights"]]
    lines += ["", f"{last_label}｜{review.get(last_key, '')}"]
    return "\n".join(lines).strip()


_UP_COLOR, _DOWN_COLOR = "#E85D5D", "#2CB39A"      # 台股慣例：漲紅跌綠（和 K 線同色）


def _pct_color(value: Optional[float]) -> str:
    return _UP_COLOR if (value or 0) > 0 else _DOWN_COLOR if (value or 0) < 0 else "#101828"


def _price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}" if value >= 100 else f"{value:g}"


def review_summary(payload: Dict[str, Any]) -> List[Tuple[str, str]]:
    """覆盤卡標題下的一行摘要（程式數字，不經 AI）：[(文字, 顏色)]，渲染端用「｜」串起來。
    持倉中：09/11 @30.2｜現價 36.65（盤中）｜+21.36%｜MFE +21.36%｜MAE -8.11%｜持有 7 日
    已賣出：09/11 @30.2 → 09/20 @36.5｜實現 +20.86%｜MFE …｜MAE …｜持有 …"""
    t, a = payload["trade"], payload["after_buy"]
    live = "（盤中）" if str(a.get("price_basis", "")).startswith("盤中") else ""
    if payload["mode"] == "closed":
        parts = [(f"{t['buy_date'][5:]} 買進 {_price(t['buy_price'])}", ""),
                 (f"{str(t.get('sell_date') or '')[5:]} 賣出 {_price(t.get('sell_price'))}", ""),
                 (f"實現報酬 {a['return_pct']:+.2f}%", _pct_color(a["return_pct"]))]
    else:
        parts = [(f"{t['buy_date'][5:]} 買進 {_price(t['buy_price'])}", ""),
                 (f"現價 {_price(a.get('last_price'))}{live}", ""),
                 (f"報酬 {a['return_pct']:+.2f}%", _pct_color(a["return_pct"]))]
    # 用中文寫：最大浮盈＝持有期間最高曾賺多少（MFE）、最大回撤＝持有期間最低曾虧多少（MAE）
    parts += [(f"最大浮盈 {a['max_gain_pct']:+.2f}%", _pct_color(a["max_gain_pct"])),
              (f"最大回撤 {a['max_drawdown_pct']:+.2f}%", _pct_color(a["max_drawdown_pct"])),
              (f"持有 {a['trading_days']} 日", "")]
    return parts


_STATUS_RANK = {"❌": 3, "⚠️": 2, "❓": 1, "✅": 0}


def highlight_icons(payload: Dict[str, Any]) -> List[str]:
    """本次記住三條的圖示（程式決定，不看 AI 文字）：理由＝理由核對裡最差的狀態、
    過程＝最大回撤 ≤ -5% 為 ⚠️ 否則 ✅、學習＝💡。"""
    checks = payload.get("reason_checks") or []
    reason = max((c["status"] for c in checks), key=lambda s: _STATUS_RANK.get(s, 1)) if checks else "❓"
    process = "⚠️" if payload["after_buy"]["max_drawdown_pct"] <= -5 else "✅"
    return [reason, process, "💡"]


def review_panel(payload: Dict[str, Any], review: Dict[str, Any], source: str) -> Dict[str, Any]:
    """圖片用的覆盤卡資料（answer_image.review_card 畫）。已賣出時，出場理由核對接在進場理由後面。"""
    closed = payload["mode"] == "closed"
    checks = list(payload["reason_checks"])
    checks += [dict(c, claim=f"出場｜{c['claim']}") for c in payload.get("sell_reason_checks") or []]
    return {"review": {
        "title": title(payload), "headline": review["headline"], "body": review["body"],
        "highlights": review["highlights"],
        "last_label": "下次" if closed else "目前觀察",
        "last_text": review.get("lesson" if closed else "watch", ""),
        "source": source,
        "reason_raw": payload["trade"].get("entry_reason_raw", ""),
        "summary": review_summary(payload),
        "highlight_icons": highlight_icons(payload),
        "checks": checks,
    }}


def title(payload: Dict[str, Any]) -> str:
    """持倉中＝「2454 聯發科｜持倉中覆盤」；已賣出＝「2454 聯發科｜交易覆盤」。"""
    stock = payload["stock"]
    return f"{stock['code']} {stock['name']}｜{'交易覆盤' if payload['mode'] == 'closed' else '持倉中覆盤'}"


# ============================================================
# 保存與清單
# ============================================================

def _user_key(context_key: str) -> str:
    return STATE_PREFIX + (str(context_key or "").split(":")[-1] or "anonymous")


def save_note(context_key: str, payload: Dict[str, Any], review: Dict[str, Any], source: str,
              raw_input: str = "") -> Dict[str, Any]:
    """使用者原始輸入（user_entry_reason_raw／user_input_raw）與 Gemini 覆盤（gemini_review）分開存，
    Gemini 的結果永遠不會覆蓋使用者原文。回傳存進去的那一筆。"""
    key = _user_key(context_key)
    trade = payload["trade"]
    record = {
        "trade_id": payload.get("trade_id"), "at": time.time(),
        "code": payload["stock"]["code"], "name": payload["stock"]["name"], "mode": payload["mode"],
        "buy_date": trade["buy_date"], "buy_price": trade["buy_price"],
        "sell_date": trade.get("sell_date"), "sell_price": trade.get("sell_price"),
        # ↓ 使用者原文：永久保留、不經改寫
        "user_input_raw": raw_input,
        "user_entry_reason_raw": trade.get("entry_reason_raw", ""),
        "user_sell_reason_raw": trade.get("sell_reason") or "",
        # ↓ 程式核對結果（每條理由的狀態，之後可統計「哪種理由常常不成立」）
        "checks": [{"claim": c["claim"], "status": c["status"]} for c in payload["reason_checks"]],
        "sell_checks": [{"claim": c["claim"], "status": c["status"]} for c in payload.get("sell_reason_checks") or []],
        "return_pct": payload["after_buy"]["return_pct"],
        "mfe_pct": payload["after_buy"]["max_gain_pct"], "mae_pct": payload["after_buy"]["max_drawdown_pct"],
        # ↓ AI 產生的覆盤：另外一欄
        "gemini_review": review, "review_source": source,
    }
    # 同一個 SQLite transaction 內讀→附加→寫；原紀錄損壞時丟 StateCorrupt，不會當空清單覆蓋掉
    local_market_cache.append_state_list(key, record, keep=KEEP_NOTES)
    return record


def list_notes(context_key: str) -> str:
    notes = list(local_market_cache.get_state(_user_key(context_key), []) or [])
    if not notes:
        return "目前沒有覆盤紀錄。輸入「覆盤 2454 9/1 買進，理由：…」就能建立第一筆。"
    lines = ["**我的覆盤紀錄**（最近 10 筆）"]
    for n in reversed(notes[-10:]):
        when = datetime.fromtimestamp(n["at"]).strftime("%m/%d")
        marks = "".join(c["status"] for c in n.get("checks") or [])
        state = "已賣出" if n.get("mode") == "closed" else "持倉中"
        headline = str((n.get("gemini_review") or {}).get("headline") or "")
        lines.append(f"• {n['name']}（{n['code']}）{n['buy_date']} 買進 {n['buy_price']:g}｜{state} "
                     f"{n['return_pct']:+.2f}%｜理由 {marks}｜{when} 建立" + (f"｜{headline}" if headline else ""))
    return "\n".join(lines)
