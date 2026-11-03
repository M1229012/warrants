"""台指期近月連續日 K（日盤＋夜盤）與奇摩即時報價（測試版 11-03，費波驗證 台指期 用）。

日 K：FinMind TaiwanFuturesDaily（TX）。11-03 實際資料：夜盤那一列的日期是夜盤「結束」那天
（11-02 15:00～11-03 05:00 記在 11-03），和期交所「夜盤屬於下一個交易日」一致，所以某交易日的完整 K 棒＝
當天日期的夜盤列（前一晚）＋當天日盤列：開盤用夜盤開盤、收盤用日盤收盤、高低取兩段最大最小、量相加。
近月＝到期日（第 3 個週三）還沒過的最近月契約（和奇摩「台指期近一」同一個）；換月不還原價差，換月日記在 attrs。
只收日盤已經有資料的交易日；日盤還沒出來的那天只把夜盤記在 attrs["pending_night"]，給即時 K 棒拼接用。

即時：奇摩股市批次報價 WTX&（台指期近一，exchangeDataDelayedBy＝0），日盤與夜盤同一個代號；
13:45 收盤後到 15:00 夜盤開始前沒有成交價。只用來畫最右邊那根，不加入任何計算。
已知限制：國定假日沒有排除（交易日用平日推算）；期交所日盤資料發布到 FinMind 之前，當天那根只看得到夜盤＋即時報價。
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


def _next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def session_of(moment: datetime) -> tuple[str, str]:
    """成交時間 → (所屬交易日, 日盤／夜盤)。15:00 後的夜盤屬於下一個交易日；凌晨 05:00 前是前一晚夜盤的延續。"""
    minutes = moment.hour * 60 + moment.minute
    day = moment.date()
    if minutes >= 15 * 60:
        return _next_weekday(day).isoformat(), "夜盤"
    if minutes <= 5 * 60:
        return (day if day.weekday() < 5 else _next_weekday(day)).isoformat(), "夜盤"
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
    day, session = session_of(quote["moment"])
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
            "with_night": bool(night)}
