"""個人交易覆盤筆記：使用者只給「股票＋買進日＋買進理由」，程式補齊當時盤面與買進後走勢，
再交給 Gemini 寫固定結構的覆盤筆記；K 線圖上用紫色 ◆ 標出自己的買賣點（與權證分點 ▲▼ 區分）。

原則：
- 「買進當時」只用買進日（含）以前的日 K 計算，不混入之後才知道的資料。
- 理由有提到權證／分點才抓分點事件；有提到外資／投信／自營商／法人才抓三大法人。都沒提就不抓。
- 理由逐條用規則對照當時資料：✅ 符合／❌ 不符／⚪ 無法驗證（系統沒有該資料，不亂判）。
- 筆記依 Discord 使用者分開存（SQLite），只存本人的交易紀錄。
"""
from __future__ import annotations

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
_NOT_CODE_RE = re.compile(r"(?:買在|買進價|買進|買入|賣出|賣在|成本|價格|均價|@)\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*[元張]")
_LOTS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*張")
_REASON_RE = re.compile(r"(?:理由|原因|因為)\s*[:：是]?\s*(.+)$", re.S)
_CODE_RE = re.compile(r"(?<![0-9A-Z])(\d{4,6}[A-Z]?)(?![0-9A-Z])")
STATE_PREFIX = "trade_review:"
KEEP_NOTES = 50
CHART_MIN_BARS, CHART_MAX_BARS, CHART_BEFORE = 70, 140, 20
TRADE_COLOR = "#6D28D9"


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


def parse_request(text: str) -> Dict[str, Any]:
    """「覆盤 2454 9/1 買進 1285，理由：…」→ {code, name, buy_date, reason, price, lots, sell_date}。"""
    raw = str(text or "").strip()
    today = tools.taipei_now().date()
    reason_hit = _REASON_RE.search(raw)
    reason = reason_hit.group(1).strip() if reason_hit else ""
    head = raw[:reason_hit.start()] if reason_hit else raw
    code, name = _find_stock(head) if head else ("", "")
    if not code:
        code, name = _find_stock(raw)
    dates = []
    for match in _DATE_RE.finditer(head):
        value = _parse_date(int(match.group(2)), int(match.group(3)),
                            int(match.group(1)) if match.group(1) else None, today)
        if value:
            tail = head[match.end():match.end() + 4]
            dates.append((value, "賣" in tail or "出場" in tail))
    buy_date = next((d for d, is_sell in dates if not is_sell), None)
    sell_date = next((d for d, is_sell in dates if is_sell), None)
    # 沒寫「理由：」時，把股票、日期拿掉後剩下的字當理由
    if not reason:
        rest = _TRIGGER_RE.sub("", raw)
        rest = _DATE_RE.sub("", rest)
        rest = _CODE_RE.sub("", rest)
        if name:
            rest = rest.replace(name, "")
        reason = re.sub(r"^[\s，,。:：]*(買進|買入|買|進場)?[\s，,。:：]*", "", rest).strip()
    price = lots = None
    for match in _PRICE_RE.finditer(head):
        price = float(match.group(1) or match.group(2))
        break
    lots_hit = _LOTS_RE.search(head)
    if lots_hit:
        lots = float(lots_hit.group(1))
    return {"code": code, "name": name, "buy_date": buy_date, "sell_date": sell_date,
            "reason": reason, "price": price, "lots": lots,
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
    k9, d9 = _f(row.get("K9")), _f(row.get("D9"))
    pk, pd9 = _f(prev.get("K9")), _f(prev.get("D9"))
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
        "kd": {"K9": k9, "D9": d9,
               "golden_cross_today": None if None in (k9, d9, pk, pd9) else (pk <= pd9 and k9 > d9)},
        "volume_lots": round(volume / 1000) if volume else None,
        "mv5_lots": round(_f(row.get("MV5"), 0) / 1000) if _f(row.get("MV5"), 0) else None,
        "mv20_lots": round(_f(row.get("MV20"), 0) / 1000) if _f(row.get("MV20"), 0) else None,
        "high_20d": _f(part["High"].tail(20).max()), "low_20d": _f(part["Low"].tail(20).min()),
    }


def after_buy(df: pd.DataFrame, idx: int, price: float, sell_idx: Optional[int]) -> Dict[str, Any]:
    end = sell_idx if sell_idx is not None else len(df) - 1
    part = df.iloc[idx:end + 1]
    last = float(part["Close"].iloc[-1])
    high, low = float(part["High"].max()), float(part["Low"].min())
    return {
        "until": tools._fmt_date(part.index[-1]), "trading_days": len(part) - 1,
        "last_close": round(last, 2), "return_pct": round((last / price - 1) * 100, 2),
        "max_gain_pct": round((high / price - 1) * 100, 2),
        "max_drawdown_pct": round((low / price - 1) * 100, 2),
        "high_date": tools._fmt_date(part["High"].idxmax()), "low_date": tools._fmt_date(part["Low"].idxmin()),
        "closed_trade": sell_idx is not None,
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
# 理由核對（規則式；對不上資料的一律標 ⚪ 無法驗證）
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


def check_claim(claim: str, snap: Dict[str, Any], inst: Optional[Dict[str, Any]],
                warrant_events: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    close = snap.get("close")
    for key, pattern in _MA_WORDS:
        if re.search(pattern, claim):
            ma = (snap["moving_averages"].get(key) or {}).get("value")
            if ma is None or close is None:
                return {"claim": claim, "status": "⚪", "evidence": f"{key} 資料不足"}
            above = close > ma
            want_above = not re.search(r"跌破|跌落|失守|在.*之下|低於", claim)
            ok = above == want_above
            return {"claim": claim, "status": "✅" if ok else "❌",
                    "evidence": f"收盤 {close:g}，{key} {ma:g}（{'站上' if above else '跌破'}）"}
    if re.search(r"多頭排列", claim):
        ok = snap.get("ma_alignment") == "多頭排列"
        return {"claim": claim, "status": "✅" if ok else "❌", "evidence": f"均線：{snap.get('ma_alignment')}"}
    if re.search(r"布林", claim):
        bb = snap.get("bollinger") or {}
        if re.search(r"開口|擴張|擴大|張口", claim):
            trend = [w for w in bb.get("bandwidth_trend") or [] if w is not None]
            if len(trend) < 3:
                return {"claim": claim, "status": "⚪", "evidence": "布林帶寬資料不足"}
            ok = all(b > a for a, b in zip(trend[-3:], trend[-2:]))
            return {"claim": claim, "status": "✅" if ok else "❌",
                    "evidence": "帶寬 " + " → ".join(f"{w:.1f}%" for w in trend[-3:])}
        if re.search(r"上軌", claim) and bb.get("upper") and close is not None:
            ok = close >= bb["upper"]
            return {"claim": claim, "status": "✅" if ok else "❌", "evidence": f"收盤 {close:g}，上軌 {bb['upper']:g}"}
    if re.search(r"爆量|量增|放量|出量|大量", claim):
        vol, mv5 = snap.get("volume_lots"), snap.get("mv5_lots")
        if not vol or not mv5:
            return {"claim": claim, "status": "⚪", "evidence": "成交量資料不足"}
        ratio = vol / mv5
        return {"claim": claim, "status": "✅" if ratio >= 1.5 else "❌",
                "evidence": f"當日 {vol:,} 張，5 日均量 {mv5:,} 張（{ratio:.1f} 倍）"}
    if re.search(r"KD", claim, re.IGNORECASE) and re.search(r"金叉|黃金交叉|交叉向上", claim):
        kd = snap.get("kd") or {}
        if kd.get("K9") is None:
            return {"claim": claim, "status": "⚪", "evidence": "KD 資料不足"}
        ok = bool(kd.get("golden_cross_today")) or kd["K9"] > kd["D9"]
        return {"claim": claim, "status": "✅" if ok else "❌", "evidence": f"K {kd['K9']:.1f}／D {kd['D9']:.1f}"}
    if re.search(r"突破.*(前高|新高|20日高)", claim):
        high = snap.get("high_20d")
        if high is None or close is None:
            return {"claim": claim, "status": "⚪", "evidence": "資料不足"}
        ok = close >= high
        return {"claim": claim, "status": "✅" if ok else "❌", "evidence": f"收盤 {close:g}，近 20 日高 {high:g}"}
    for label in ("外資", "投信", "自營商"):
        if label[:2] in claim and re.search(r"買超|賣超|買|賣", claim):
            if not inst:
                return {"claim": claim, "status": "⚪", "evidence": "三大法人資料取不到"}
            days = _n_days(claim)
            selling = "賣" in claim
            streak = inst["buy_streak_days"].get(label)
            day_value = inst["buy_day"].get(label)
            if selling:
                ok = day_value is not None and day_value < 0
            else:
                ok = streak is not None and streak >= days
            return {"claim": claim, "status": "✅" if ok else "❌",
                    "evidence": f"買進日{label} {day_value:+,} 張，連續買超 {streak} 天" if day_value is not None
                    else f"{label}資料不足"}
    if _WARRANT_RE.search(claim):
        if warrant_events is None:
            return {"claim": claim, "status": "⚪", "evidence": "權證分點資料取不到"}
        return {"claim": claim, "status": "✅" if warrant_events else "❌",
                "evidence": f"買進日前後 10 個交易日有 {len(warrant_events)} 筆分點事件" if warrant_events
                else "買進日前後 10 個交易日沒有 A～E 分點事件"}
    return {"claim": claim, "status": "⚪", "evidence": "系統沒有可對照的資料"}


# ============================================================
# 組資料
# ============================================================

def build_review(req: Dict[str, Any]) -> Dict[str, Any]:
    """回傳 {payload, panel, trades}；payload 給 Gemini 與事實核對，panel 給 K 線圖卡。"""
    code = req["code"]
    bundle = tools._load_price_bundle(code)
    df = tools.closed_frame(bundle).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    idx = _index_on_or_after(df, req["buy_date"])
    if idx is None:
        raise tools.ToolDataError("買進日晚於目前最新的日 K 資料")
    if idx == 0:
        raise tools.ToolDataError(f"本地日 K 只回溯到 {tools._fmt_date(df.index[0])}，買進日太早，無法覆盤")
    sell_idx = _index_on_or_after(df, req["sell_date"]) if req.get("sell_date") else None
    if sell_idx is not None and sell_idx <= idx:      # 賣出日早於（或等於）買進日：視為沒填
        sell_idx = None
    buy_price = float(req.get("price") or df["Close"].iloc[idx])
    snap = snapshot_at(df, idx)
    result = after_buy(df, idx, buy_price, sell_idx)
    buy_day = pd.Timestamp(df.index[idx]).normalize()

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
    if sell_idx is not None:
        trades.append({"date": tools._fmt_date(df.index[sell_idx]), "price": float(df["Close"].iloc[sell_idx]),
                       "side": "sell"})
    panel["trades"] = trades
    panel["trade_summary"] = (f"我的交易｜{trades[0]['date'][5:]} 買進 {buy_price:g}"
                              + (f"｜{trades[1]['date'][5:]} 賣出 {trades[1]['price']:g}" if len(trades) > 1 else "")
                              + f"｜{'出場' if result['closed_trade'] else '至今'} {result['return_pct']:+.2f}%")

    checks = [check_claim(c, snap, inst, warrant_events) for c in _split_claims(req["reason"])]
    payload = {
        "stock": {"code": code, "name": req.get("name") or panel.get("stock_name", "")},
        "trade": {"buy_date": tools._fmt_date(buy_day), "buy_price": buy_price,
                  "buy_price_source": "使用者提供" if req.get("price") else "買進日收盤價",
                  "lots": req.get("lots"), "reason": req["reason"],
                  "sell_date": trades[1]["date"] if len(trades) > 1 else None},
        "at_buy": snap,
        "after_buy": result,
        "reason_checks": checks,
        "institutional": inst,
        "warrant_events_near_buy": [
            {k: e.get(k) for k in ("no", "branch", "event_codes", "buy_date", "action_date", "action_text",
                                   "net_amount_text", "status") if e.get(k) not in (None, "")}
            for e in (warrant_events or [])][:10] if warrant_events is not None else None,
    }
    return {"payload": payload, "panel": panel}


# ============================================================
# Gemini prompt 與保存
# ============================================================

REVIEW_PROMPT = """你是「艾斯 AI」的交易覆盤教練。請依 review_data 為使用者寫一篇覆盤筆記。

規則：
1. 只能使用 review_data 裡的事實與數字，不可自創資料；沒有的資料直接略過，不要猜。
2. reason_checks 已經由程式逐條核對（✅ 符合／❌ 不符／⚪ 無法驗證）。照這個結果寫，不可自己改判；⚪ 的理由要說明「系統沒有資料可驗證」，不能說它錯。
3. 「買進當時」只談 at_buy（買進日收盤時看得到的盤面），不可用 after_buy 的結果回頭說當時就該知道。
4. 語氣像教練：肯定做對的地方，具體指出可以改進的地方（例如進場位置、風險控管、該觀察卻沒寫到的條件），不給目標價、不保證未來。
5. 繁體中文，精簡，總長 350～550 字。

固定結構（用這些小標題）：
【買進理由】一句話重述。
【當時盤面】1～3 個最關鍵的數據。
【理由核對】逐條列出 ✅／❌／⚪ 與依據。
【買進後走勢】報酬、最大漲幅、最大回檔。
【做得好的地方】
【可以改進的地方】

review_data：
"""


def build_prompt(payload: Dict[str, Any]) -> str:
    import json
    return REVIEW_PROMPT + json.dumps(payload, ensure_ascii=False, default=str)


def rule_note(payload: Dict[str, Any]) -> str:
    """Gemini 失敗時的規則式筆記（只列資料）。"""
    t, a, s = payload["trade"], payload["after_buy"], payload["at_buy"]
    lines = [f"【買進理由】{t['reason']}",
             f"【當時盤面】{s['date']} 收盤 {s['close']:g}｜均線 {s['ma_alignment']}",
             "【理由核對】"]
    lines += [f"{c['status']} {c['claim']}：{c['evidence']}" for c in payload["reason_checks"]]
    lines.append(f"【買進後走勢】至 {a['until']}（{a['trading_days']} 個交易日）報酬 {a['return_pct']:+.2f}%｜"
                 f"最大漲幅 {a['max_gain_pct']:+.2f}%｜最大回檔 {a['max_drawdown_pct']:+.2f}%")
    return "\n".join(lines)


def _user_key(context_key: str) -> str:
    return STATE_PREFIX + (str(context_key or "").split(":")[-1] or "anonymous")


def save_note(context_key: str, payload: Dict[str, Any], note: str) -> None:
    key = _user_key(context_key)
    notes = list(local_market_cache.get_state(key, []) or [])
    notes.append({"at": time.time(), "code": payload["stock"]["code"], "name": payload["stock"]["name"],
                  "buy_date": payload["trade"]["buy_date"], "buy_price": payload["trade"]["buy_price"],
                  "reason": payload["trade"]["reason"], "return_pct": payload["after_buy"]["return_pct"],
                  "note": note})
    local_market_cache.set_state(key, notes[-KEEP_NOTES:])


def list_notes(context_key: str) -> str:
    notes = list(local_market_cache.get_state(_user_key(context_key), []) or [])
    if not notes:
        return "目前沒有覆盤紀錄。輸入「覆盤 2454 9/1 買進，理由：…」就能建立第一筆。"
    lines = ["**我的覆盤紀錄**（最近 10 筆）"]
    for n in reversed(notes[-10:]):
        when = datetime.fromtimestamp(n["at"]).strftime("%m/%d")
        lines.append(f"• {n['name']}（{n['code']}）{n['buy_date']} 買進 {n['buy_price']:g}｜"
                     f"覆盤時 {n['return_pct']:+.2f}%｜{when} 建立｜理由：{n['reason'][:30]}")
    return "\n".join(lines)
