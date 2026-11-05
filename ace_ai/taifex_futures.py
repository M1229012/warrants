"""台指期近月連續日 K（日盤＋夜盤）與奇摩即時報價（測試版 11-03，費波驗證 台指期 用）。

日 K：FinMind TaiwanFuturesDaily（TX）。11-03 實際資料：夜盤那一列的日期是夜盤「結束」那天
（11-02 15:00～11-03 05:00 記在 11-03），和期交所「夜盤屬於下一個交易日」一致，所以某交易日的完整 K 棒＝
當天日期的夜盤列（前一晚）＋當天日盤列：開盤用夜盤開盤、收盤用日盤收盤、高低取兩段最大最小、量相加。
近月＝到期日（第 3 個週三）還沒過的最近月契約（和奇摩「台指期近一」同一個）；換月不還原價差，換月日記在 attrs。
只收日盤已經有資料的交易日；日盤還沒出來的那天只把夜盤記在 attrs["pending_night"]，給即時 K 棒拼接用。

即時：奇摩股市批次報價 WTX&（台指期近一，exchangeDataDelayedBy＝0），日盤與夜盤同一個代號；
13:45 收盤後到 15:00 夜盤開始前沒有成交價。費波驗證圖只用來畫最右邊那根，不加入任何計算。
11-03 台指期型態頁（price_bundle）：和個股型態頁一樣，即時那根當盤中暫定 K 棒（均線、型態跟著變，最終以收盤為準）。
已知限制：期交所日盤資料發布到 FinMind 之前，當天那根只看得到夜盤＋即時報價。
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

import warrant_ai_tools as tools

CODE = "TXF"
NAME = "台指期近月（日盤＋夜盤）"
FUTURES_ID = "TX"
SYMBOL = "WTX&"                 # 奇摩：台指期近一
HISTORY_DAYS = 420              # 費波要約 260 根交易日
DAY_TTL = 1800                  # 日 K 每 30 分鐘重抓一次（日盤資料收盤後才發布）
QUOTE_TTL = 20
_CODE_RE = re.compile(r"台指期|台指|期指|(?<![A-Za-z])TXF?(?![A-Za-z])", re.IGNORECASE)


def is_futures(text: str) -> bool:
    return bool(_CODE_RE.search(str(text or "")))


# 只用來決定「要不要請 AI 讀整句」：句子有提到期貨、夜盤才多花一次 AI；是不是問台指期走勢由 AI 判斷
# 11-05 Log：「外資空單如何」沒提期貨也是在問台指期未平倉 → 空單、多單、未平倉也請 AI 讀整句
_MENTION_RE = re.compile(r"台指|期指|期貨|夜盤|日盤|大台|小台|微台|空單|多單|未平倉|(?<![A-Za-z])TXF?(?![A-Za-z])", re.IGNORECASE)
_KIND_SCHEMA = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["trend", "positions", "other"]}},
                "required": ["kind"]}


def question_kind(question: str, ask_ai) -> str:
    """11-03 使用者：「現在夜盤如何」「台指期如何」要出台指期型態頁＋AI 解讀，任何白話都可以 → 讓 AI 讀整句，不補關鍵字。
    回 trend＝問台指期本身（日盤、夜盤）走勢；positions＝法人／散戶期貨未平倉、外資空單（11-05：加權 K 線＋未平倉副圖）；
    other＝其他；沒提期貨相關字、沒有 AI 或讀題失敗回空字串（照舊）。ask_ai(prompt, schema) 回 dict 或 None。"""
    q = str(question or "")
    if ask_ai is None or not _MENTION_RE.search(q):
        return ""
    prompt = ("你是台股問句解析器，只輸出 JSON，不要回答問題。使用者的問題提到期貨、夜盤或空單多單，讀完整句判斷他要看什麼：\n"
              "trend＝台指期本身的走勢、型態、現在怎樣、支撐壓力、會不會漲跌（日盤、夜盤、大台、小台都算）；\n"
              "positions＝三大法人、外資、散戶的期貨未平倉、多單空單口數、多空比（「外資空單」「外資多單」沒講個股就是台指期未平倉）；\n"
              "other＝其他（個股的融券或個股期貨、選擇權、加權指數現貨、美股夜盤、和台指期無關的問題）。\n問題：" + q)
    try:
        out = ask_ai(prompt, _KIND_SCHEMA) or {}
    except Exception:
        return ""
    kind = str(out.get("kind") or "")
    return kind if kind in ("trend", "positions", "other") else ""


def wants_trend(question: str, ask_ai) -> bool:
    return question_kind(question, ask_ai) == "trend"


def _fetch_rows() -> pd.DataFrame:
    kf = tools.core()
    end = tools.taipei_now()
    started = time.perf_counter()
    try:
        raw = kf._finmind_get_data("TaiwanFuturesDaily", data_id=FUTURES_ID,
                                   start_date=(end - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d"),
                                   end_date=end.strftime("%Y-%m-%d"), allow_empty=False)
        tools.record_api_event("FinMindData", status=200, latency=time.perf_counter() - started)
    except Exception as exc:
        tools.record_api_event("FinMindData", status=500, latency=time.perf_counter() - started)
        raise tools.ToolDataError("台指期日 K 暫時無法取得") from exc
    return raw


def continuous(raw: pd.DataFrame) -> pd.DataFrame:
    """FinMind 原始列（所有契約、兩個時段）→ 近月連續日 K（Open/High/Low/Close/Volume，index＝交易日）。"""
    df = raw.copy()
    df["contract_date"] = df["contract_date"].astype(str).str.strip()
    df = df[df["contract_date"].str.fullmatch(r"\d{6}")]               # 去掉週契約、價差單
    for col in ("open", "max", "min", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["date"] = df["date"].astype(str).str[:10]
    df = df[df["close"] > 0]
    bars, contracts, pending = [], {}, {}

    def near(rows: pd.DataFrame, day: str) -> Optional[pd.Series]:
        if rows.empty:
            return None
        rows = rows[pd.Series([tools._contract_expiry(c) >= day for c in rows["contract_date"]], index=rows.index)]
        return rows.sort_values("contract_date").iloc[0] if not rows.empty else None

    for day, g in sorted(df.groupby("date"), key=lambda x: x[0]):
        d = near(g[g["trading_session"] == "position"], day)
        nights = g[g["trading_session"] == "after_market"]
        if d is None:
            n = near(nights, day)
            if n is not None:
                pending[day] = {"contract": n["contract_date"], "open": float(n["open"]), "high": float(n["max"]),
                                "low": float(n["min"]), "close": float(n["close"]), "volume": float(n["volume"])}
            continue
        same = nights[nights["contract_date"] == d["contract_date"]]
        n = same.iloc[0] if not same.empty else None
        bars.append({"Date": pd.Timestamp(day),
                     "Open": float(n["open"]) if n is not None else float(d["open"]),
                     "High": max(float(d["max"]), float(n["max"]) if n is not None else 0.0),
                     "Low": min(float(d["min"]), float(n["min"]) if n is not None else float("inf")),
                     "Close": float(d["close"]),
                     "Volume": float(d["volume"]) + (float(n["volume"]) if n is not None else 0.0)})
        contracts[day] = d["contract_date"]
    if not bars:
        raise tools.ToolDataError("台指期日 K 沒有資料")
    frame = pd.DataFrame(bars).set_index("Date").sort_index()
    days = list(contracts)
    frame.attrs.update(contracts=contracts, pending_night=pending,
                       rolls=[d for p, d in zip(days, days[1:]) if contracts[p] != contracts[d]])
    return frame


def daily_frame() -> pd.DataFrame:
    return tools._cached(f"taifex_daily_{tools.taipei_now():%Y-%m-%d-%H}", DAY_TTL, lambda: continuous(_fetch_rows()))


HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def closed_days() -> set:
    """11-03 程式碼檢查：平日休市（國定假日、補假、春節前後只辦結算）也不是交易日。
    證交所 OpenAPI 年度休市表（期貨跟現貨同一套）：名稱有「交易日」的是開始／最後交易日（有開市），其餘都是休市。
    抓不到就回空集合（退回只跳週末）。"""
    import requests

    def fetch() -> list:
        resp = requests.get(HOLIDAY_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=8)
        resp.raise_for_status()
        out = []
        for row in resp.json() or []:
            text = re.sub(r"\D", "", str(row.get("Date") or ""))
            if "交易日" in str(row.get("Name") or "") or len(text) != 7:
                continue
            out.append(f"{int(text[:3]) + 1911:04d}-{text[3:5]}-{text[5:7]}")
        return out

    try:
        return set(tools._cached(f"twse_holidays_{tools.taipei_now():%Y-%m-%d}", 86400, fetch) or [])
    except Exception as exc:
        print(f"⚠️ 休市表讀取失敗，交易日只跳過週末｜{tools.err_text(exc)}", flush=True)
        return set()


FIXED_CLOSED = ("01-01",)       # 元旦每年固定休市（休市表還沒涵蓋新年度時也不會算成交易日）


def _is_trading(day: date, closed: set) -> bool:
    return day.weekday() < 5 and day.isoformat() not in closed and day.strftime("%m-%d") not in FIXED_CLOSED


def calendar_note(day: str, closed: set) -> str:
    """11-03 複查：休市表只涵蓋它發布的年度（目前只有 115 年）。推算出的交易日不在表的年度內，就註明是平日推算，
    不默默當成已確認的交易日。表的年度＝表裡日期的年份。"""
    if not closed:
        return "休市表讀取失敗，交易日以平日推算"
    if str(day)[:4] not in {d[:4] for d in closed}:
        return f"休市表還沒涵蓋 {str(day)[:4]} 年，交易日以平日推算"
    return ""


def _next_trading_day(day: date, closed: set) -> date:
    day += timedelta(days=1)
    while not _is_trading(day, closed):
        day += timedelta(days=1)
    return day


def _prev_trading_day(day: date, closed: set) -> date:
    day -= timedelta(days=1)
    while not _is_trading(day, closed):
        day -= timedelta(days=1)
    return day


def session_of(moment: datetime, closed: Optional[set] = None) -> tuple[str, str]:
    """成交時間 → (所屬交易日, 日盤／夜盤)。15:00 後的夜盤屬於下一個交易日；凌晨 05:00 前是前一晚夜盤的延續。
    「下一個交易日」跳過週末和休市日（closed＝休市日 ISO 字串集合；沒給就讀證交所休市表）。"""
    closed = closed_days() if closed is None else closed
    minutes = moment.hour * 60 + moment.minute
    day = moment.date()
    if minutes >= 15 * 60:
        return _next_trading_day(day, closed).isoformat(), "夜盤"
    if minutes <= 5 * 60:
        return (day if _is_trading(day, closed) else _next_trading_day(day, closed)).isoformat(), "夜盤"
    return day.isoformat(), "日盤"


def yahoo_quote() -> Dict[str, Any]:
    """奇摩 WTX& 最新成交；沒有成交價（13:45～15:00、休市）或被擋時回空 dict。"""
    import requests
    import urllib.parse
    import yahoo_quotes
    if not tools.stock_live_quotes() or not yahoo_quotes.enabled():
        return {}

    def fetch() -> Dict[str, Any]:
        started = time.perf_counter()
        resp = requests.get(yahoo_quotes.URL + urllib.parse.quote(SYMBOL), headers=yahoo_quotes.HEADERS,
                            timeout=yahoo_quotes.TIMEOUT_SECONDS)
        tools.record_api_event("Yahoo", status=int(resp.status_code), latency=time.perf_counter() - started)
        resp.raise_for_status()
        item = next(iter(resp.json() or []), {}) or {}
        price, stamp = yahoo_quotes._raw(item.get("price")), str(item.get("regularMarketTime") or "")
        if price <= 0 or not stamp or stamp == "-":
            return {}
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tools.TAIPEI_TZ)
        return {"price": price, "open": yahoo_quotes._raw(item.get("regularMarketOpen")),
                "high": yahoo_quotes._raw(item.get("regularMarketDayHigh")),
                "low": yahoo_quotes._raw(item.get("regularMarketDayLow")),
                "volume": yahoo_quotes._raw(item.get("volume")), "moment": moment,
                "name": str(item.get("symbolName") or "")}

    try:
        return tools._cached("taifex_yahoo_quote", QUOTE_TTL, fetch) or {}
    except Exception as exc:
        print(f"⚠️ 台指期即時報價略過｜{tools.err_text(exc)}", flush=True)
        return {}


def live_bar(frame: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """最右邊那根（只顯示）：奇摩即時價所屬交易日還沒有正式日 K 時才有。
    日盤進行中：開盤用 FinMind 已發布的前一晚夜盤開盤、高低和夜盤合併，收盤＝現價；夜盤進行中：只有夜盤這段。"""
    quote = yahoo_quote()
    if not quote:
        return None
    closed = closed_days()
    day, session = session_of(quote["moment"], closed)
    if day <= f"{pd.Timestamp(frame.index[-1]):%Y-%m-%d}":
        return None
    price = quote["price"]
    open_ = quote["open"] or price
    high = max(quote["high"] or price, price, open_)
    low = min(quote["low"] or price, price, open_)
    volume = quote["volume"]
    night = (frame.attrs.get("pending_night") or {}).get(day) if session == "日盤" else None
    if night:
        open_, high, low, volume = night["open"], max(high, night["high"]), min(low, night["low"]), volume + night["volume"]
    return {"date": day, "Open": float(open_), "High": float(high), "Low": float(low), "Close": float(price),
            "Volume": float(volume), "time": quote["moment"].strftime("%H:%M"), "is_live": True, "label": session,
            "with_night": bool(night), "calendar_note": calendar_note(day, closed)}


_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def price_bundle() -> Dict[str, Any]:
    """11-03 台指期型態頁：和個股 _load_price_bundle 同一種格式，get_technical_analysis／get_volume_profile／
    get_stock_overview／get_chart_panel 直接沿用。closed_df＝近月連續日 K；奇摩即時那根（日盤或夜盤）接在後面當盤中暫定，
    均量沿用前一日（盤中累計量不算進均量，和個股、指數一樣）。成交量單位是口。"""
    kf = tools.core()
    frame = daily_frame()
    closed = kf.calculate_indicators(frame[_COLUMNS].copy())
    closed["Close_prev"] = closed["Close"].shift(1)
    base = {"df": closed, "closed_df": closed, "market": "futures", "intraday": {}, "data_gaps": [],
            "daily_source": "台指期近月日K（前一晚夜盤＋當天日盤；換月不還原價差）"}
    live = live_bar(frame)
    if not live:
        return base
    prev = _prev_trading_day(date.fromisoformat(live["date"]), closed_days()).isoformat()
    if f"{pd.Timestamp(frame.index[-1]):%Y-%m-%d}" < prev:
        # 前一個交易日的日K還沒發布到 FinMind（例：晚上夜盤時當天日盤還沒出來）：硬接即時那根會少一天、均線跟著錯 → 先不接
        return dict(base, daily_source=base["daily_source"] + f"；{prev[5:]} 日K尚未發布，{live['label']}即時價先不接")
    merged = pd.concat([frame[_COLUMNS], pd.DataFrame([{k: live[k] for k in _COLUMNS}], index=[pd.Timestamp(live["date"])])])
    df = kf.calculate_indicators(merged)
    df["Close_prev"] = df["Close"].shift(1)
    for column in ("MV5", "MV20"):
        if column in df.columns and len(df) > 1:
            df.iloc[-1, df.columns.get_loc(column)] = df[column].iloc[-2]
    last_close = float(frame["Close"].iloc[-1])
    # 夜盤問題要回答「夜盤現在比日盤收盤漲跌多少」：程式先算好給 AI（事實核對才對得上，不讓 AI 自己算）
    intraday = {"is_live": True, "date": live["date"], "time": live["time"], "session": live["label"],
                "price": live["Close"], "with_night": live["with_night"], "calendar_note": live["calendar_note"],
                "last_close": last_close, "last_close_date": f"{pd.Timestamp(frame.index[-1]):%Y-%m-%d}",
                "change_vs_last_close": round(live["Close"] - last_close, 2),
                "change_pct_vs_last_close": round((live["Close"] / last_close - 1) * 100, 2) if last_close else None}
    return dict(base, df=df, intraday=intraday)
