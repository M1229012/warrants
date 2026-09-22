"""個人交易覆盤筆記：使用者只給「股票＋買進日＋買進理由」（賣出日／賣出理由可選），
程式補齊當時盤面、進場後走勢與目前結構，再請 Gemini 寫「只有 AI 才需要寫」的幾段。

覆盤結構（持倉中／完整交易分開）：
  ① 交易摘要　　　　程式產生
  ② 我的進場理由　　程式產生：只列使用者當時講過的理由，不替他補理由
  ③ 理由核對　　　　程式產生：✅ 成立／⚠️ 部分成立／❌ 不成立／❓ 資料不足
  ④ 進場後發展　　　Gemini：只記錄進場後出現的事實，明確標示「不是原始進場理由」，禁止「主要歸功於」這類事後歸因
  ⑤ 本次覆盤　　　　Gemini：做對／修正／下次，各一兩句
  持倉中 → 目前觀察（Gemini 一兩句）；已賣出 → 賣出檢討（實現報酬、MFE／MAE、賣後 5 日、賣太早或太晚）

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
_INST_RE = re.compile(r"外資|投信|自營|法人")
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


def _kd_cross_days_ago(part: pd.DataFrame, lookback: int = 3) -> Optional[int]:
    """最近幾根內 K 由下往上穿過 D：0＝當天、1＝前一天…；沒有交叉回 None。"""
    k, d = part.get("K9"), part.get("D9")
    if k is None or d is None or len(part) < 2:
        return None
    for back in range(0, min(lookback, len(part) - 1)):
        i = len(part) - 1 - back
        k0, d0, k1, d1 = _f(k.iloc[i - 1]), _f(d.iloc[i - 1]), _f(k.iloc[i]), _f(d.iloc[i])
        if None not in (k0, d0, k1, d1) and k0 <= d0 and k1 > d1:
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
    return {
        "date": tools._fmt_date(part.index[-1]), "close": close,
        "change_pct": _f((row["Close"] / prev["Close"] - 1) * 100) if len(part) > 1 and prev.get("Close") else None,
        "moving_averages": {f"MA{n}": tools._ma_position(close, v) for n, v in mas.items()},
        "ma_alignment": tools._ma_alignment(mas),
        "bollinger": {"upper": upper, "lower": lower, "mid": mas[20],
                      "bandwidth_pct": _f(widths[-1]) if widths and widths[-1] is not None else None,
                      "bandwidth_trend": widths, "signals": (bollinger or {}).get("signals", [])},
        "kd": {"K9": _f(row.get("K9")), "D9": _f(row.get("D9")), "cross_days_ago": _kd_cross_days_ago(part)},
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


def inst_summary(frame: pd.DataFrame, buy_day: pd.Timestamp) -> Dict[str, Any]:
    before = frame[frame["Date"] <= buy_day].tail(5)
    after = frame[frame["Date"] > buy_day]

    def streak(col: str) -> int:
        count = 0
        for value in reversed(before[col].tolist()):
            if value > 0:
                count += 1
            else:
                break
        return count

    out = {"buy_day": {}, "before_5d_sum": {}, "buy_streak_days": {}, "after_sum": {}}
    last = before.iloc[-1] if not before.empty else None
    for col, label in (("foreign", "外資"), ("invest", "投信"), ("dealer", "自營商")):
        out["buy_day"][label] = round(float(last[col])) if last is not None else None
        out["before_5d_sum"][label] = round(float(before[col].sum())) if not before.empty else None
        out["buy_streak_days"][label] = streak(col) if not before.empty else None
        out["after_sum"][label] = round(float(after[col].sum())) if not after.empty else None
    out["unit"] = "張"
    return out


# ============================================================
# 理由核對：✅ 成立／⚠️ 部分成立／❌ 不成立／❓ 資料不足
# ============================================================

_CN_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_MA_WORDS = (("MA5", r"5日線|五日線|週線|周線|MA5"), ("MA10", r"10日線|十日線|MA10"),
             ("MA20", r"月線|20日線|二十日線|MA20|中軌"), ("MA60", r"季線|60日線|六十日線|MA60"))


def _split_claims(reason: str) -> List[str]:
    parts = re.split(r"[，,、；;。\n]|以及|並且|而且|加上|還有", reason)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _n_days(text: str) -> int:
    hit = re.search(r"連\s*(\d+|[一二兩三四五六七八九十])\s*[天日]", text)
    if not hit:
        return 1
    raw = hit.group(1)
    return int(raw) if raw.isdigit() else _CN_NUM.get(raw, 1)


def _result(claim: str, status: str, evidence: str) -> Dict[str, str]:
    return {"claim": claim, "status": status, "status_text": STATUS_TEXT[status], "evidence": evidence}


def check_claim(claim: str, snap: Dict[str, Any], inst: Optional[Dict[str, Any]],
                warrant_events: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    close = snap.get("close")
    day = str(snap.get("date") or "")[5:]
    for key, pattern in _MA_WORDS:
        if re.search(pattern, claim):
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
        if re.search(r"上軌", claim) and bb.get("upper") and close is not None:
            gap = (close / bb["upper"] - 1) * 100
            status = "✅" if gap >= 0 else "⚠️" if gap >= -1 else "❌"
            return _result(claim, status, f"{day} 收盤 {close:g}，上軌 {bb['upper']:g}")
    if re.search(r"爆量|量增|放量|出量|大量", claim):
        vol, mv5 = snap.get("volume_lots"), snap.get("mv5_lots")
        if not vol or not mv5:
            return _result(claim, "❓", "成交量資料不足")
        ratio = vol / mv5
        status = "✅" if ratio >= 1.5 else "⚠️" if ratio >= 1.2 else "❌"
        return _result(claim, status, f"{day} 成交 {vol:,} 張，為 5 日均量 {mv5:,} 張的 {ratio:.1f} 倍")
    if re.search(r"KD", claim, re.IGNORECASE) and re.search(r"金叉|黃金交叉|交叉向上", claim):
        kd = snap.get("kd") or {}
        if kd.get("K9") is None or kd.get("D9") is None:
            return _result(claim, "❓", "KD 資料不足")
        ago = kd.get("cross_days_ago")
        values = f"K {kd['K9']:.1f}／D {kd['D9']:.1f}"
        if ago == 0:
            return _result(claim, "✅", f"{day} {values}，當日黃金交叉")
        if ago is not None:
            return _result(claim, "✅", f"{day} {values}，{ago} 天前黃金交叉")
        if kd["K9"] > kd["D9"]:
            return _result(claim, "⚠️", f"{day} {values}，K 在 D 之上，但近 3 日沒有交叉")
        return _result(claim, "❌", f"{day} {values}，K 仍在 D 之下")
    if re.search(r"突破.*(前高|新高|20日高)", claim):
        high = snap.get("high_20d")
        if high is None or close is None:
            return _result(claim, "❓", "資料不足")
        gap = (close / high - 1) * 100
        status = "✅" if gap >= 0 else "⚠️" if gap >= -1 else "❌"
        return _result(claim, status, f"{day} 收盤 {close:g}，近 20 日高 {high:g}")
    for label in ("外資", "投信", "自營商"):
        if label[:2] in claim and re.search(r"買超|賣超|買|賣", claim):
            if not inst:
                return _result(claim, "❓", "三大法人資料取不到")
            need = _n_days(claim)
            streak = inst["buy_streak_days"].get(label)
            value = inst["buy_day"].get(label)
            if value is None:
                return _result(claim, "❓", f"{label}資料不足")
            evidence = f"{day} {label}{'買' if value >= 0 else '賣'}超 {abs(value):,} 張，連續買超 {streak} 天"
            if "賣" in claim:
                return _result(claim, "✅" if value < 0 else "❌", evidence)
            if streak is not None and streak >= need:
                return _result(claim, "✅", evidence)
            if value > 0:
                return _result(claim, "⚠️", evidence + f"（理由寫連 {need} 天）")
            return _result(claim, "❌", evidence)
    if _WARRANT_RE.search(claim):
        if warrant_events is None:
            return _result(claim, "❓", "權證分點資料取不到")
        return _result(claim, "✅" if warrant_events else "❌",
                       f"買進日前後 10 個交易日有 {len(warrant_events)} 筆分點事件" if warrant_events
                       else "買進日前後 10 個交易日沒有 A～E 分點事件")
    return _result(claim, "❓", "系統沒有可對照的資料")


# ============================================================
# 持股狀態卡：沿用一般個股的型態評分卡（同一套關鍵價位、均線扣抵、盤中觀察），但不顯示分數
# ============================================================

def _position_card(code: str, cost: float, result: Dict[str, Any], need_warrant: bool
                   ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """回傳 (卡片, 給 Gemini 的現況資料)。任何一步失敗只少這張卡，筆記照寫。"""
    import weekly_pick
    try:
        tech = tools.get_technical_analysis(code)
        vp = tools.get_volume_profile(code)
        extras = weekly_pick._technical_extras(code)
        chips = tools.get_sheet_stock_chips(code) if need_warrant else None
        card = weekly_pick.build_pattern_scorecard(tech, vp, extras, chips, cost)
    except Exception as exc:
        print(f"⚠️ 覆盤持股狀態卡略過｜{code}｜{type(exc).__name__}: {exc}", flush=True)
        return {}, {}
    card.update({
        "hide_score": True,                        # 覆盤不給評分：拿掉分數、五大項與得分／失分
        "level_limits": (2, 6),                    # 關鍵價位多列幾道支撐（均線、量區、布林）
        "card_title": "持股狀態",
        "card_note": f"{card.get('score_basis') or '收盤確認'}｜成本與均線、量區、布林的距離",
        "show_tracked_branches": bool(card.get("show_tracked_branches")) and need_warrant,
        "extra_tags": [
            ("買進", f"{result.get('buy_date_text', '')}（持有 {result['trading_days']} 個交易日）"),
            ("區間", f"最大浮盈 {result['max_gain_pct']:+.2f}%｜最大回撤 {result['max_drawdown_pct']:+.2f}%"),
        ],
    })
    current = {
        "data_basis": card.get("score_basis"),
        "close": card.get("close"),
        "unrealized_pct": card.get("unrealized_pct"),
        "pattern_label": card.get("pattern_label"),
        "ma_alignment": card.get("ma_alignment"),
        "moving_averages": tech.get("moving_averages"),
        "bollinger_signals": (tech.get("bollinger") or {}).get("signals"),
        "position_vs_two_zones": vp.get("position_vs_two_zones"),
        "supports_below": card.get("supports_below_close"),
        "intraday_changes": card.get("intraday_changes"),
    }
    return card, current


# ============================================================
# 組資料
# ============================================================

def build_review(req: Dict[str, Any]) -> Dict[str, Any]:
    """回傳 {payload, panel}；payload 給 Gemini 與事實核對，panel 給 K 線圖卡。"""
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
        dates = [bar["date"] for bar in panel.get("bars") or []]
        buy_text = tools._fmt_date(buy_day)
        window = set()
        if buy_text in dates:
            pos = dates.index(buy_text)
            window = set(dates[max(0, pos - 10):pos + 11])
        warrant_events = [e for e in (panel.get("marks") or {}).get("events") or []
                          if e.get("buy_date") in window or e.get("action_date") in window]

    inst = None
    if req["need_inst"]:
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
    basis = "實現" if closed else ("盤中" if result["price_basis"].startswith("盤中") else "至今")
    panel["trade_summary"] = (f"我的交易｜{trades[0]['date'][5:]} 買進 {buy_price:g}"
                              + (f"｜{trades[1]['date'][5:]} 賣出 {sell_price:g}" if closed else "")
                              + f"｜{basis} {result['return_pct']:+.2f}%")

    current = {}
    if not closed:                                   # 已賣出就不需要「持股狀態」卡
        card, current = _position_card(code, buy_price, result, req["need_warrant"])
        if card:
            panel["scorecard"] = card

    checks = [check_claim(c, snap, inst, warrant_events) for c in _split_claims(req["reason"])]
    payload = {
        "mode": "closed" if closed else "holding",
        "stock": {"code": code, "name": req.get("name") or panel.get("stock_name", "")},
        "trade": {"buy_date": tools._fmt_date(buy_day), "buy_price": buy_price,
                  "buy_price_source": "使用者提供" if req.get("price") else "買進日收盤價",
                  "lots": req.get("lots"), "entry_reasons": _split_claims(req["reason"]),
                  "sell_date": trades[1]["date"] if closed else None, "sell_price": sell_price,
                  "sell_reason": req.get("sell_reason") or None},
        "at_buy": snap,
        "after_buy": result,
        "after_sell": after_sell(live, sell_idx, sell_price) if closed else None,
        "current": current or None,
        "reason_checks": checks,
        "institutional": inst,
        "warrant_events_near_buy": [
            {k: e.get(k) for k in ("no", "branch", "event_codes", "buy_date", "action_date", "action_text",
                                   "net_amount_text", "status") if e.get(k) not in (None, "")}
            for e in (warrant_events or [])][:10] if warrant_events is not None else None,
    }
    # 轉成純 JSON（numpy 數值、Timestamp 都先轉掉），後面的事實核對會直接 json.dumps(payload)
    payload = json.loads(json.dumps(payload, ensure_ascii=False, default=tools.json_safe))
    return {"payload": payload, "panel": panel}


# ============================================================
# Gemini：只寫「進場後發展／本次覆盤／目前觀察或賣出檢討」，其餘由程式組
# ============================================================

AI_FIELDS_HOLDING = ("after_entry", "did_right", "to_fix", "next_time", "watch")
AI_FIELDS_CLOSED = ("after_entry", "did_right", "to_fix", "next_time", "sell_review")


def ai_schema(mode: str) -> Dict[str, Any]:
    fields = AI_FIELDS_CLOSED if mode == "closed" else AI_FIELDS_HOLDING
    return {"type": "object", "properties": {f: {"type": "string"} for f in fields}, "required": list(fields)}


REVIEW_PROMPT = """你在幫交易者寫交易日記的其中幾段。交易摘要、進場理由、理由核對已經由程式寫好，你只負責下面的欄位，
每個欄位 1～2 句、繁體中文、陳述句，像交易者寫給半年後的自己看，20 秒內要能讀完。

欄位：
- after_entry（進場後發展）：只記錄進場後「實際發生的事實」，例如最大回撤、之後的價格走勢、進場後才出現的均線或布林變化。
  最後要清楚標示「這些是進場後出現的現象，不是原始進場理由」。
- did_right（做對的）：只能根據 reason_checks 裡 ✅／⚠️ 的理由，或進場位置的品質（例如進場後最大回撤很小）。
- to_fix（需要修正的）：針對 ❌／⚠️／❓ 的理由，具體說哪一條與當日資料不符、差在哪裡；全部成立就寫進場理由上可以再補的確認條件。
- next_time（下次可沿用）：一句可以直接執行的做法，例如「KD 黃金交叉搭配整理區突破或量能確認後再進場」。
- watch（目前觀察，只有持倉中）：依 current 用一兩句說目前趨勢與最值得觀察的一件事，不要列一串價位。
- sell_review（賣出檢討，只有已賣出）：依 trade.sell_reason 與 after_sell，說明賣出理由是否站得住、賣後 5 日走勢顯示是賣早還是賣晚；沒寫賣出理由就只談時機。

嚴格禁止：
- 事後歸因：不可寫「主要歸功於」「多虧」「正是因為」「證明了」「果然」，也不可把進場後才出現的現象說成當初的買進原因。
- 替交易者補理由：只談 trade.entry_reasons 裡的理由。
- 打招呼、自我介紹、說教（「請務必」「建議你應該」「持續優化」）、勉勵、目標價、買賣指令。
- 使用 review_data 以外的數字；reason_checks 的結果不可改判。

回傳 JSON。review_data：
"""


def build_prompt(payload: Dict[str, Any]) -> str:
    return REVIEW_PROMPT + json.dumps(payload, ensure_ascii=False, default=str)


def strip_hindsight(text: str) -> Tuple[str, List[str]]:
    """刪掉事後歸因的句子（Gemini 偶爾還是會寫），回傳 (剩下文字, 被刪句子)。"""
    kept, removed = [], []
    for sentence in re.findall(r"[^。！？\n]+[。！？]?", str(text or "")):
        (removed if _HINDSIGHT_RE.search(sentence) else kept).append(sentence)
    return "".join(kept).strip(), removed


def rule_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    """Gemini 失敗或被核對刪光時用的規則式內容。"""
    a = payload["after_buy"]
    checks = payload["reason_checks"]
    good = [c["claim"] for c in checks if c["status"] in ("✅", "⚠️")]
    bad = [f"{c['claim']}（{c['status_text']}）" for c in checks if c["status"] in ("❌", "❓")]
    out = {
        "after_entry": (f"進場後最大回撤 {a['max_drawdown_pct']:+.2f}%，最大浮盈 {a['max_gain_pct']:+.2f}%，"
                        f"{'出場' if a['closed_trade'] else '目前'}報酬 {a['return_pct']:+.2f}%。"
                        "以上是進場後的走勢，不是原始進場理由。"),
        "did_right": ("成立的理由：" + "、".join(good) + "。") if good else "進場理由沒有一條被資料確認。",
        "to_fix": ("與資料不符或無法驗證：" + "、".join(bad) + "。") if bad else "各項理由都與當日資料相符。",
        "next_time": "",
    }
    current = payload.get("current") or {}
    supports = [f"{lv.get('label')} {lv.get('price'):g}" for lv in (current.get("supports_below") or [])[:2]
                if lv.get("price") is not None]
    out["watch"] = (f"目前{current.get('pattern_label') or ''}，均線{current.get('ma_alignment') or ''}"
                    + (f"；下方最近支撐 {'、'.join(supports)}。" if supports else "。")) if current else ""
    post = payload.get("after_sell") or {}
    out["sell_review"] = (f"賣出後 {post['days_available']} 個交易日收盤相對賣價 {post['close_change_pct']:+.2f}%，"
                          f"期間最高 {post['max_gain_after_pct']:+.2f}%、最低 {post['max_drop_after_pct']:+.2f}%。"
                          if post.get("days_available") else "賣出後還沒有足夠的交易日可以比較。")
    return out


def compose(payload: Dict[str, Any], fields: Dict[str, str]) -> str:
    """把程式寫的段落與 Gemini 的欄位組成最終筆記。"""
    t, a = payload["trade"], payload["after_buy"]
    closed = payload["mode"] == "closed"
    live_note = "（盤中）" if str(a.get("price_basis", "")).startswith("盤中") else ""
    if closed:
        summary = [f"{t['buy_date'][5:]} 買進 {t['buy_price']:g}｜{t['sell_date'][5:]} 賣出 {t['sell_price']:g}"
                   f"｜實現報酬 {a['return_pct']:+.2f}%",
                   f"持有 {a['trading_days']} 個交易日｜最大浮盈 {a['max_gain_pct']:+.2f}%（MFE）"
                   f"｜最大回撤 {a['max_drawdown_pct']:+.2f}%（MAE）"]
    else:
        summary = [f"{t['buy_date'][5:]} 買進 {t['buy_price']:g}｜現價 {a['last_price']:g}{live_note}"
                   f"｜{a['return_pct']:+.2f}%",
                   f"持有 {a['trading_days']} 個交易日｜最大浮盈 {a['max_gain_pct']:+.2f}%"
                   f"｜最大回撤 {a['max_drawdown_pct']:+.2f}%"]
    lines = ["【交易摘要】", *summary,
             "【我的進場理由】", "｜".join(t["entry_reasons"]) or t.get("reason", ""),
             "【理由核對】"]
    lines += [f"{c['status']} {c['claim']}：{c['status_text']}｜{c['evidence']}" for c in payload["reason_checks"]]
    lines += ["【進場後發展】", fields.get("after_entry", "")]
    lines += ["【本次覆盤】"]
    for key, label in (("did_right", "做對"), ("to_fix", "修正"), ("next_time", "下次")):
        if fields.get(key):
            lines.append(f"{label}：{fields[key]}")
    if closed:
        lines += ["【賣出檢討】"]
        if t.get("sell_reason"):
            lines.append(f"賣出理由：{t['sell_reason']}")
        lines.append(fields.get("sell_review", ""))
    elif fields.get("watch"):
        lines += ["【目前觀察】", fields["watch"]]
    return "\n".join(line for line in lines if line is not None)


def title(payload: Dict[str, Any]) -> str:
    return f"{payload['stock']['name']}｜{'完整交易覆盤' if payload['mode'] == 'closed' else '持倉中覆盤'}"


# ============================================================
# 保存與清單
# ============================================================

def _user_key(context_key: str) -> str:
    return STATE_PREFIX + (str(context_key or "").split(":")[-1] or "anonymous")


def save_note(context_key: str, payload: Dict[str, Any], note: str) -> None:
    key = _user_key(context_key)
    notes = list(local_market_cache.get_state(key, []) or [])
    notes.append({"at": time.time(), "code": payload["stock"]["code"], "name": payload["stock"]["name"],
                  "mode": payload["mode"], "buy_date": payload["trade"]["buy_date"],
                  "buy_price": payload["trade"]["buy_price"], "sell_date": payload["trade"].get("sell_date"),
                  "sell_price": payload["trade"].get("sell_price"),
                  "reasons": payload["trade"]["entry_reasons"],
                  # 每條理由的核對結果都存下來，之後可以統計「哪種理由常常不成立」
                  "checks": [{"claim": c["claim"], "status": c["status"]} for c in payload["reason_checks"]],
                  "return_pct": payload["after_buy"]["return_pct"],
                  "mfe_pct": payload["after_buy"]["max_gain_pct"], "mae_pct": payload["after_buy"]["max_drawdown_pct"],
                  "note": note})
    local_market_cache.set_state(key, notes[-KEEP_NOTES:])


def list_notes(context_key: str) -> str:
    notes = list(local_market_cache.get_state(_user_key(context_key), []) or [])
    if not notes:
        return "目前沒有覆盤紀錄。輸入「覆盤 2454 9/1 買進，理由：…」就能建立第一筆。"
    lines = ["**我的覆盤紀錄**（最近 10 筆）"]
    for n in reversed(notes[-10:]):
        when = datetime.fromtimestamp(n["at"]).strftime("%m/%d")
        marks = "".join(c["status"] for c in n.get("checks") or [])
        state = "已賣出" if n.get("mode") == "closed" else "持倉中"
        lines.append(f"• {n['name']}（{n['code']}）{n['buy_date']} 買進 {n['buy_price']:g}｜{state} "
                     f"{n['return_pct']:+.2f}%｜理由 {marks}｜{when} 建立")
    return "\n".join(lines)
