"""FinMind 替代資料來源：股價改用 Yahoo，權證分點改用 MoneyDJ。

FinMind 會員到期後，主程式的兩個資料來源由這裡接手。兩者放同一個檔案，
因為它們是同一件事的兩半，分開只會讓 repo 多一個檔案而已。

用法（主程式只要改 import）：

    from warrant_sources import fetch_stock_data_yahoo as fetch_stock_data_yf
    from warrant_sources import fetch_warrant_branch_daily

自我檢查（趁 FinMind 還在，兩邊對照；到期後就只能盲改）：

    python warrant_sources.py price   2330 6488
    python warrant_sources.py warrant 03882T 03881T
"""


from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

YAHOO_TIMEOUT = (
    float(os.getenv("YAHOO_PRICE_CONNECT_TIMEOUT", "8")),
    float(os.getenv("YAHOO_PRICE_READ_TIMEOUT", "25")),
)
YAHOO_RETRIES = max(1, int(os.getenv("YAHOO_PRICE_RETRIES", "3")))
YAHOO_RETRY_WAIT = max(0.2, float(os.getenv("YAHOO_PRICE_RETRY_WAIT", "1.2")))

# 上市在前：多數查詢都是上市股，先試中的機率高，省一次往返。
MARKET_SUFFIXES = (("TW", "上市"), ("TWO", "上櫃"))


def _normalize_code(stock_code) -> str:
    """只留代號本體，容忍 '2330.TW'、'2330 台積電' 這類輸入。"""
    text = str(stock_code or "").strip().upper()
    match = re.match(r"^([0-9A-Z]{4,6})", text)
    return match.group(1) if match else text


def _yahoo_get_json(symbol: str, period1: int, period2: int) -> dict:
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        # 除權息調整後的價格，與 FinMind 的還原邏輯一致。
        "events": "div,splits",
    }
    last_error = None
    for attempt in range(1, YAHOO_RETRIES + 1):
        try:
            response = requests.get(
                YAHOO_CHART_URL.format(symbol=symbol),
                params=params,
                headers=YAHOO_HEADERS,
                timeout=YAHOO_TIMEOUT,
            )
            # 查無此代號時 Yahoo 回 404 並附帶 JSON 錯誤，不該當成連線失敗重試。
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - 對外部服務一律重試
            last_error = exc
            if attempt < YAHOO_RETRIES:
                time.sleep(YAHOO_RETRY_WAIT * attempt)
    raise RuntimeError(f"Yahoo 股價請求失敗：{symbol}｜{last_error}")


def _chart_to_frame(payload: dict) -> pd.DataFrame:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return pd.DataFrame()
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps:
        return pd.DataFrame()

    frame = pd.DataFrame(
        {
            # Yahoo 的 timestamp 是收盤當下的 UTC 秒數；換成台北時間才會落在正確的交易日。
            "Date": pd.to_datetime(timestamps, unit="s", utc=True)
            .tz_convert("Asia/Taipei")
            .tz_localize(None)
            .normalize(),
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        }
    )
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.dropna(subset=["Date", "Open", "High", "Low", "Close"])
        .drop_duplicates(subset=["Date"], keep="last")
        .sort_values("Date")
        .set_index("Date")
    )


def fetch_stock_data_yahoo(stock_code: str, period: str = "160d"):
    """回傳 (df, market, code)，與主程式的 fetch_stock_data_yf 相同格式。

    df 以 Date 為索引（tz-naive），欄位 Open/High/Low/Close/Volume。
    """
    code = _normalize_code(stock_code)
    match = re.search(r"(\d+)", str(period or "160d"))
    calendar_days = max(120, int(match.group(1)) if match else 160)

    end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    start_dt = end_dt - timedelta(days=calendar_days)
    # 多給一天緩衝，避免邊界剛好切掉最新一根。
    period1 = int(start_dt.timestamp())
    period2 = int((end_dt + timedelta(days=1)).timestamp())

    errors = []
    for suffix, market_label in MARKET_SUFFIXES:
        symbol = f"{code}.{suffix}"
        try:
            frame = _chart_to_frame(_yahoo_get_json(symbol, period1, period2))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
            continue
        if frame.empty:
            continue
        print(
            f"✅ Yahoo 股價資料：{code}（{symbol}）｜{len(frame):,} 筆｜"
            f"{frame.index.min().date()} ~ {frame.index.max().date()}"
        )
        return frame, market_label, code

    detail = f"｜{'；'.join(errors)}" if errors else ""
    raise RuntimeError(f"Yahoo 查無股價：{code}（已試 .TW 與 .TWO）{detail}")


# ---------------------------------------------------------------- 對照測試


def _compare_with_finmind(code: str, period: str = "160d") -> None:
    """同一檔同時抓兩邊，逐日比對收盤與成交量。

    FinMind 到期後這段就跑不動了，這正是現在做的理由。
    """
    token = (os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_TOKEN") or "").strip()
    if not token:
        print("⚠️ 未設定 FINMIND_API_TOKEN，略過對照，只顯示 Yahoo 結果")
        return

    days = max(120, int(re.search(r"(\d+)", period).group(1)))
    end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    start_dt = end_dt - timedelta(days=days)
    response = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params={
            "dataset": "TaiwanStockPrice",
            "data_id": code,
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=(8, 60),
    )
    rows = (response.json() or {}).get("data") or []
    if not rows:
        print(f"⚠️ FinMind 無資料：{code}，略過對照")
        return

    fin = pd.DataFrame(rows)
    fin["Date"] = pd.to_datetime(fin["date"]).dt.tz_localize(None)
    fin = fin.set_index("Date").sort_index()

    yahoo, market, _ = fetch_stock_data_yahoo(code, period=period)
    joined = yahoo.join(fin[["close", "Trading_Volume"]], how="inner")
    if joined.empty:
        print(f"❌ {code} 兩邊沒有共同交易日，日期對齊有問題")
        return

    close_diff = (joined["Close"] - joined["close"]).abs()
    # 成交量用比例看，絕對差在大型股上沒有意義。
    volume_ratio = (joined["Volume"] / joined["Trading_Volume"].replace(0, pd.NA)).dropna()

    print(f"\n=== {code}（{market}）對照 {len(joined)} 個共同交易日 ===")
    print(f"收盤價   最大差 {close_diff.max():.4f}｜平均差 {close_diff.mean():.4f}")
    print(f"成交量   比值中位數 {volume_ratio.median():.4f}（1.0 表示單位一致）")
    worst = close_diff.sort_values(ascending=False).head(3)
    for day, diff in worst.items():
        if diff <= 0.01:
            break
        print(
            f"  差異最大 {day.date()}：Yahoo {joined.loc[day, 'Close']}"
            f" vs FinMind {joined.loc[day, 'close']}"
        )
    verdict = "✅ 可直接替換" if close_diff.max() <= 0.05 else "⚠️ 有落差，需要確認除權息還原方式"
    print(verdict)




from concurrent.futures import ThreadPoolExecutor, as_completed


# e 控制回傳筆數。不帶這個參數時 MoneyDJ 只回前 30 個分點，成交量大的權證
# 會被默默截掉一半以上——072570 在 2026-08-05 實際有 92 個分點，不帶 e 只拿到 30。
# 帶 e=500 之後與 FinMind 逐檔比對零缺漏。其他常見寫法（n / top / rows /
# limit / count）都無效，只有 e 有用，所以這個參數不能拿掉。
API4_ROWS = max(50, int(os.getenv("MONEYDJ_API4_ROWS", "500")))

API4_URL = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata/9b/6e/0a/"
    "TwWarrantData.xdjjson?a={code}&x=warrant-chip0002-4&c={day}&d={day}"
    "&e={rows}&revision=2018_07_31_1"
)
# API4 的 c / d 本來就是「起日 / 迄日」，不是同一天。
# warrant_backtest_moneydj.py 的 api4_get_with_status(code, start, end) 就是這樣用的，
# 一次請求就能拿回整段區間的分點。原本的 discover_branches() 把兩個參數塞同一天，
# 於是請求數變成「權證數 × 天數」；改用區間後降到「權證數」，70 天窗口等於快 70 倍。
API4_RANGE_URL = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata/9b/6e/0a/"
    "TwWarrantData.xdjjson?a={code}&x=warrant-chip0002-4&c={start}&d={end}"
    "&e={rows}&revision=2018_07_31_1"
)
API5_URL = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata/d8/f5/27/"
    "twWarrantData.xdjjson?x=warrant-chip0002-5&c={days}&a={warrant}&b={broker}"
    "&revision=2018_07_31_1"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://pscnetsecrwd.moneydj.com/",
}

TIMEOUT = (
    float(os.getenv("MONEYDJ_CONNECT_TIMEOUT", "6")),
    float(os.getenv("MONEYDJ_READ_TIMEOUT", "20")),
)
RETRIES = max(1, int(os.getenv("MONEYDJ_RETRIES", "3")))
RETRY_WAIT = max(0.2, float(os.getenv("MONEYDJ_RETRY_WAIT", "1.5")))
API4_WORKERS = max(1, int(os.getenv("MONEYDJ_API4_WORKERS", "40")))
API5_WORKERS = max(1, int(os.getenv("MONEYDJ_API5_WORKERS", "50")))

# API5 的 c={days} 是「一次回幾列歷史」，不是額外請求，
# 所以放大它幾乎沒有成本：請求數固定是「已探索到的 (權證, 分點) 組數」。
#
# 這裡原本在 fetch_warrant_events_moneydj() 內被寫死成 min(span_days, 14)，
# 註解宣稱「MoneyDJ 只保留約兩週的滾動視窗」。但 warrant_backtest_moneydj.py
# 用 MONEYDJ_API5_HISTORY_LIMIT=200 打同一支端點可以取回 200 天，
# 證明兩週的假設不成立——寫死的 14 會讓資金流累計線只涵蓋最近約兩週，
# 更早的交易全部變成 0，圖上看起來像「沒有交易」。
# 預設直接對齊回測的 200。
API5_HISTORY_LIMIT = max(
    1, int(os.getenv("MONEYDJ_API5_HISTORY_LIMIT", "200"))
)

# 分點探索窗口：這個才是真正的成本來源，請求數 = 權證數 × 天數。
# 程式實測 2330（1,126 檔權證）問 16 天要 18,016 次請求、約 6 分鐘，
# 所以不跟著查詢區間無限放大，預設維持 8 天，需要更長再自行調高。
# 改用 API4 區間探索後，天數不再影響請求數（一支權證固定一次），
# 因此預設直接對齊產圖程式的 K 棒數 CHART_LOOKBACK=70。
DISCOVERY_DAYS_DEFAULT = max(2, int(os.getenv("MONEYDJ_DISCOVERY_DAYS", "70")))
# 只有在區間探索失效、退回逐日模式時才會用到的保護上限，
# 逐日模式的請求數是「權證數 × 天數」，不能讓它跟著 70 天跑。
DISCOVERY_FALLBACK_MAX_DAYS = max(
    2, int(os.getenv("MONEYDJ_DISCOVERY_FALLBACK_MAX_DAYS", "8"))
)
# 區間探索時單支權證可回傳的最大列數（API4 的 e 參數）。
# 70 天 × 多個分點會遠多於原本單日的 500 列，太小會被截斷。
API4_RANGE_ROWS = max(
    API4_ROWS, int(os.getenv("MONEYDJ_API4_RANGE_ROWS", "3000"))
)
# 區間探索失敗時是否回退逐日模式。區間模式沒有在正式環境驗證過，留一條後路。
DISCOVERY_RANGE_ENABLE = os.getenv(
    "MONEYDJ_DISCOVERY_RANGE_ENABLE", "1"
).strip().lower() not in ("0", "false", "no", "off")


_THREAD_LOCAL = threading.local()


def _get_session() -> requests.Session:
    """每個執行緒共用一個 Session。

    原本每次都直接 requests.get()，等於每個請求重做一次 TCP + TLS 交握。
    幾千次小請求下這是純浪費；改用連線池後同一執行緒可以重用連線。
    pool_maxsize 要跟著 worker 數走，否則 urllib3 會一直丟
    "Connection pool is full" 並丟棄連線，反而更慢。
    """
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8,
            pool_maxsize=max(8, API4_WORKERS, API5_WORKERS),
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
    return session

OUTPUT_COLUMNS = [
    "date",
    "stock_id",
    "securities_trader",
    "securities_trader_id",
    "buy",
    "sell",
    "price",
    # MoneyDJ 直接給金額（V4/V5），比用單價乘股數回推準確，兩者都留著。
    "buy_amount",
    "sell_amount",
]


def _get_rows(url: str) -> list[dict]:
    """MoneyDJ 有時回單一物件、有時回陣列，兩種都要吃。"""
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = _get_session().get(url, headers=HEADERS, timeout=TIMEOUT)
            response.raise_for_status()
            # MoneyDJ 不在標頭宣告 charset，requests 會猜成 latin-1，
            # 分點名稱就變成「å\x9c\x8bæ³°」這種亂碼。明確用 UTF-8 解。
            payload = json.loads(response.content.decode("utf-8", errors="replace"))
            items = payload if isinstance(payload, list) else [payload]
            rows: list[dict] = []
            for item in items:
                if isinstance(item, dict):
                    rows.extend((item.get("ResultSet") or {}).get("Result") or [])
            return rows
        except Exception as exc:  # noqa: BLE001 - 外部服務一律重試
            last_error = exc
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT * attempt)
    raise RuntimeError(f"MoneyDJ 請求失敗：{url[:110]}…｜{last_error}")


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def discover_branches(warrant_codes, days: list[str]) -> dict:
    """對每個 (權證, 日期) 跑 API4，取分點聯集。

    只跑目標日會漏掉「前幾天有交易、當天沒有」的分點，五日報告會因此少算。
    """
    jobs = [(code, day) for code in warrant_codes for day in days]
    pairs: dict[tuple[str, str], dict] = {}
    failed = 0

    def one(job):
        code, day = job
        return code, _get_rows(API4_URL.format(code=code, day=day, rows=API4_ROWS))

    with ThreadPoolExecutor(max_workers=API4_WORKERS) as executor:
        futures = {executor.submit(one, job): job for job in jobs}
        for future in as_completed(futures):
            try:
                code, rows = future.result()
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            for row in rows:
                broker_code = str(row.get("V2") or "").strip()
                if not broker_code:
                    continue
                pairs[(code, broker_code)] = {
                    "warrant_code": code,
                    "broker_code": broker_code,
                    "broker_name": str(row.get("V3") or "").strip(),
                }

    print(
        f"🔎 API4 探索：{len(warrant_codes)} 檔 × {len(days)} 天 = {len(jobs)} 次｜"
        f"找到 {len(pairs)} 組分點｜失敗 {failed}"
    )
    return pairs


def discover_branches_range(warrant_codes, start_day: str, end_day: str) -> dict:
    """一支權證一次請求整段區間，取分點聯集。

    與 discover_branches() 的差別只在請求數：
      逐日模式：權證數 × 天數
      區間模式：權證數
    盟立 140 檔權證看 70 天，9,800 次變 140 次。

    回傳格式與 discover_branches() 完全相同，呼叫端不用改。
    """
    codes = [str(c).strip().upper() for c in warrant_codes if str(c).strip()]
    if not codes:
        return {}

    pairs: dict[tuple[str, str], dict] = {}
    failed = 0
    truncated = 0

    def one(code):
        return code, _get_rows(
            API4_RANGE_URL.format(
                code=code, start=start_day, end=end_day, rows=API4_RANGE_ROWS
            )
        )

    with ThreadPoolExecutor(max_workers=API4_WORKERS) as executor:
        futures = {executor.submit(one, code): code for code in codes}
        for future in as_completed(futures):
            try:
                code, rows = future.result()
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            # 回傳列數剛好觸頂時，很可能是被 e 參數截斷，要讓使用者知道。
            if len(rows) >= API4_RANGE_ROWS:
                truncated += 1
            for row in rows:
                broker_code = str(row.get("V2") or "").strip()
                if not broker_code:
                    continue
                pairs[(code, broker_code)] = {
                    "warrant_code": code,
                    "broker_code": broker_code,
                    "broker_name": str(row.get("V3") or "").strip(),
                }

    print(
        f"🔎 API4 區間探索：{len(codes)} 檔 × 1 次 = {len(codes)} 次請求｜"
        f"{start_day} ~ {end_day}｜找到 {len(pairs)} 組分點｜失敗 {failed}"
    )
    if truncated:
        print(
            f"   ⚠️ 有 {truncated} 檔權證回傳列數觸頂（{API4_RANGE_ROWS}），可能被截斷，"
            "分點會少算；請調高 MONEYDJ_API4_RANGE_ROWS。"
        )
    return pairs


def fetch_warrant_branch_daily(
    warrant_codes,
    days: int = 5,
    discovery_days: int | None = None,
) -> pd.DataFrame:
    """回傳與 FinMind TaiwanStockWarrantTradingDailyReport 相同欄位的 DataFrame。

    days           ：API5 一次回幾列歷史（不增加請求數）
    discovery_days ：API4 分點探索窗口（請求數 = 權證數 × 天數，成本在這）
    """
    warrant_codes = [str(c).strip().upper() for c in warrant_codes if str(c).strip()]
    if not warrant_codes:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # API5 回的是每個 (權證, 分點) 的完整歷史，所以分點只要在探索窗口內出現過一次，
    # 它更早的紀錄一樣抓得到。真正會漏的只有「窗口內完全沒動」的分點，
    # 因此窗口越長結果越完整——而改用 API4 區間探索後，拉長窗口不再增加請求數。
    discovery_days = max(2, int(discovery_days or DISCOVERY_DAYS_DEFAULT))
    today = datetime.now(timezone.utc) + timedelta(hours=8)
    pairs: dict = {}

    if DISCOVERY_RANGE_ENABLE:
        pairs = discover_branches_range(
            warrant_codes,
            (today - timedelta(days=discovery_days)).strftime("%Y/%m/%d"),
            today.strftime("%Y/%m/%d"),
        )

    if not pairs:
        # 區間模式沒回東西就退回原本的逐日模式，避免這條路整個斷掉。
        # 逐日的請求數是「權證數 × 天數」，所以天數另外壓在 FALLBACK 上限。
        fallback_days = max(2, min(discovery_days, DISCOVERY_FALLBACK_MAX_DAYS))
        if DISCOVERY_RANGE_ENABLE:
            print(
                f"↩️ API4 區間探索沒有結果，退回逐日模式："
                f"{len(warrant_codes)} 檔 × {fallback_days} 天"
                f" = {len(warrant_codes) * fallback_days:,} 次請求"
            )
        # 週末 API4 會回 HTTP 錯誤而不是空結果，每一個都要耗掉三次重試與退避。
        # 先濾掉，國定假日仍會落空，那個從日期看不出來。
        calendar_days = []
        offset = 0
        while len(calendar_days) < fallback_days and offset < fallback_days + 12:
            candidate = today - timedelta(days=offset)
            offset += 1
            if candidate.weekday() >= 5:
                continue
            calendar_days.append(candidate.strftime("%Y/%m/%d"))
        pairs = discover_branches(warrant_codes, calendar_days)

    if not pairs:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    records: list[dict] = []
    failed = 0

    def one(pair):
        rows = _get_rows(
            API5_URL.format(
                days=int(days),
                warrant=pair["warrant_code"],
                broker=pair["broker_code"],
            )
        )
        return pair, rows

    with ThreadPoolExecutor(max_workers=API5_WORKERS) as executor:
        futures = {executor.submit(one, pair): pair for pair in pairs.values()}
        for future in as_completed(futures):
            try:
                pair, rows = future.result()
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            for row in rows:
                date_text = str(row.get("V1") or "").strip().replace("/", "-")
                if not date_text:
                    continue
                buy_shares = _to_float(row.get("V2"))
                sell_shares = _to_float(row.get("V3"))
                if buy_shares <= 0 and sell_shares <= 0:
                    continue
                # V4/V5 是千元；FinMind 的 price 是成交均價，這裡由金額除股數還原。
                buy_amount = _to_float(row.get("V4")) * 1000.0
                sell_amount = _to_float(row.get("V5")) * 1000.0
                if buy_shares > 0:
                    price = buy_amount / buy_shares
                elif sell_shares > 0:
                    price = sell_amount / sell_shares
                else:
                    price = 0.0
                records.append(
                    {
                        "date": date_text,
                        "stock_id": pair["warrant_code"],
                        "securities_trader": pair["broker_name"],
                        "securities_trader_id": pair["broker_code"],
                        "buy": int(buy_shares),
                        "sell": int(sell_shares),
                        "price": round(price, 4),
                        "buy_amount": buy_amount,
                        "sell_amount": sell_amount,
                    }
                )

    frame = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    if not frame.empty:
        frame = frame.sort_values(["date", "stock_id", "securities_trader_id"])
        frame = frame.reset_index(drop=True)
    print(
        f"📦 API5 明細：{len(pairs)} 組｜{len(frame):,} 筆｜"
        f"日期 {frame['date'].min() if not frame.empty else '-'}"
        f" ~ {frame['date'].max() if not frame.empty else '-'}｜失敗 {failed}"
    )
    return frame


# ------------------------------------------------- 主程式用的權證事件表


SEED_FILENAME = "mops_warrant_identity_seed.csv.gz"
TRADER_SEED_FILENAME = "securities_trader_seed.csv.gz"

EVENT_COLUMNS = [
    "Date", "branch", "broker_code", "warrant_code", "warrant_name",
    "underlying_code", "underlying_name", "buy_amount", "sell_amount",
    "net_amount", "buy_shares", "sell_shares", "side",
]

_SEED_CACHE: pd.DataFrame | None = None


def load_warrant_seed(path: str | None = None) -> pd.DataFrame:
    """MOPS 權證識別種子檔，含權證名稱與對應正股。整支程式只讀一次。"""
    global _SEED_CACHE
    if _SEED_CACHE is not None:
        return _SEED_CACHE
    seed_path = path or os.path.join(os.path.dirname(os.path.dirname(__file__)), SEED_FILENAME)
    seed = pd.read_csv(seed_path, compression="gzip", dtype={"code": str, "target_code": str})
    for column in ("list_date", "end_date"):
        seed[column] = pd.to_datetime(seed[column], errors="coerce")
    _SEED_CACHE = seed
    return seed


_TRADER_CACHE: dict | None = None


def load_trader_name_map(path: str | None = None) -> dict:
    """分點代號 → 正式名稱，取代 FinMind 的 TaiwanSecuritiesTraderInfo。

    來源是證交所「證券商基本資料」，840 筆含分行層級（合庫-台中、凱基站前…）。
    官方 OpenAPI 的 brokerService/brokerList 只有 64 家總公司，拿不到分行，
    所以這份只能靠人工下載的快照，會慢慢過時——實測 273 個實際出現的分點
    命中 272 個（99.6%），漏的是新開分點。

    漏掉不影響輸出：呼叫端取不到對照時會退回 MoneyDJ 自帶的券商簡稱。
    要更新就去證交所下載新的證券商基本資料，轉存成同名檔案即可。
    """
    global _TRADER_CACHE
    if _TRADER_CACHE is not None:
        return _TRADER_CACHE
    seed_path = path or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), TRADER_SEED_FILENAME
    )
    try:
        frame = pd.read_csv(seed_path, compression="gzip", dtype={"broker_code": str})
        _TRADER_CACHE = {
            str(row.broker_code).strip(): str(row.broker_name).strip()
            for row in frame.itertuples()
            if str(row.broker_code).strip() and str(row.broker_name).strip()
        }
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 讀不到分點名冊，改用 MoneyDJ 券商簡稱：{exc}")
        _TRADER_CACHE = {}
    return _TRADER_CACHE


def _seed_window(seed: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """
    權證代號會被回收重用：03882T 在 2023-10 是「臺股指群益39售06」，
    2026-07 重新掛牌後變成「力積電中信61售01」，正股完全不同。
    所以一定要用交易日落在 list_date~end_date 之間去篩，只用代號會對到錯的正股。
    尚未填 end_date 的視為仍在市。
    """
    return seed[
        (seed["list_date"].isna() | (seed["list_date"] <= day))
        & (seed["end_date"].isna() | (seed["end_date"] >= day))
    ]


def warrant_codes_of(underlying: str, day: pd.Timestamp, seed: pd.DataFrame | None = None) -> list[str]:
    """某檔正股在指定日期仍有效的權證代號。"""
    seed = seed if seed is not None else load_warrant_seed()
    rows = _seed_window(seed, day)
    rows = rows[rows["target_code"].astype(str) == str(underlying).strip()]
    return sorted(rows["code"].astype(str).unique())


def fetch_warrant_events_moneydj(
    stock_code: str,
    start_date,
    end_date,
    branch_normalizer=None,
    trader_name_map: dict | None = None,
    seed: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """MoneyDJ 版的權證分點事件表，欄位與 FinMind 版完全相同。

    與 FinMind 版對照過 2026-08-05 的 2408：315 檔權證、2193 組分點，
    分點清單與買賣股數 0 組不符，識別欄位補齊率 2193/2193。

    單位比照 FinMind 版：buy_shares/sell_shares 是「張」（股數除以 1000），
    buy_amount/sell_amount 是「元」，side 用中文「買超」「賣超」。
    """
    seed = seed if seed is not None else load_warrant_seed()
    if trader_name_map is None:
        trader_name_map = load_trader_name_map()
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()

    codes = warrant_codes_of(stock_code, end_ts, seed)
    if not codes:
        print(f"⚠️ 種子檔查不到 {stock_code} 在 {end_ts.date()} 的有效權證")
        return pd.DataFrame(columns=EVENT_COLUMNS)

    span_days = max(1, (end_ts - start_ts).days + 1)

    # API5 回看筆數：原本寫死 min(span_days, 14)，會讓資金流累計線只涵蓋最近約兩週。
    # 放大它不增加請求數（請求數只跟已探索到的分點組數有關），預設 200 對齊回測。
    history_days = min(span_days, API5_HISTORY_LIMIT)

    # 分點探索窗口：改用 API4 區間探索後，請求數只跟權證數有關，與天數無關，
    # 所以直接讓它蓋滿整個查詢區間；預設值 70 對齊產圖程式的 CHART_LOOKBACK。
    discovery_days = max(2, min(span_days, DISCOVERY_DAYS_DEFAULT))

    print(
        f"🪟 MoneyDJ 視窗：{stock_code}｜查詢 {start_ts.date()} ~ {end_ts.date()}"
        f"（{span_days} 天）｜API5 回看 {history_days} 列｜分點探索 {discovery_days} 天｜"
        f"權證 {len(codes)} 檔"
    )
    if discovery_days < span_days:
        # 只有「探索窗口內完全沒動」的分點會被漏掉；有動過的分點 API5 會回完整歷史。
        print(
            f"   ⚠️ 分點探索窗口（{discovery_days} 天）短於查詢區間（{span_days} 天）："
            f"最近 {discovery_days} 天完全沒交易的分點不會被發現。"
            "要補完請調高 MONEYDJ_DISCOVERY_DAYS（區間模式下不會增加請求數）。"
        )

    raw = fetch_warrant_branch_daily(
        codes,
        days=history_days,
        discovery_days=discovery_days,
    )
    if raw.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    raw = raw.copy()
    raw["Date"] = pd.to_datetime(raw["date"], errors="coerce").dt.normalize()
    raw = raw.dropna(subset=["Date"])
    raw = raw[(raw["Date"] >= start_ts) & (raw["Date"] <= end_ts)]
    if raw.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    # 逐日各自查種子檔：同一個代號跨期時，每天要對到當時那一期的正股。
    identity = _seed_window(seed, end_ts).drop_duplicates("code").set_index("code")
    name_map = identity["name"].to_dict()
    target_code_map = identity["target_code"].to_dict()
    target_name_map = identity["target_name"].to_dict()

    raw["warrant_code"] = raw["stock_id"].astype(str)
    raw["warrant_name"] = raw["warrant_code"].map(name_map).fillna("")
    raw["underlying_code"] = raw["warrant_code"].map(target_code_map).fillna("")
    raw["underlying_name"] = raw["warrant_code"].map(target_name_map).fillna("")

    raw["broker_code"] = raw["securities_trader_id"].astype(str).str.strip()
    # 官方分點對照表優先，沒有才用 MoneyDJ 給的簡稱，與 FinMind 版同一套規則。
    mapped = raw["broker_code"].map(trader_name_map or {}).fillna("").astype(str)
    raw["branch"] = mapped.where(mapped.str.strip() != "", raw["securities_trader"].astype(str).str.strip())
    if branch_normalizer is not None:
        raw["branch"] = raw["branch"].map(branch_normalizer)
    raw = raw[(raw["warrant_code"] != "") & (raw["broker_code"] != "") & (raw["branch"] != "")]

    grouped = raw.groupby(
        ["Date", "warrant_code", "broker_code", "branch"],
        as_index=False,
        dropna=False,
        sort=False,
    ).agg(
        buy_shares_raw=("buy", "sum"),
        sell_shares_raw=("sell", "sum"),
        buy_amount=("buy_amount", "sum"),
        sell_amount=("sell_amount", "sum"),
        warrant_name=("warrant_name", "first"),
        underlying_code=("underlying_code", "first"),
        underlying_name=("underlying_name", "first"),
    )
    grouped["buy_shares"] = grouped["buy_shares_raw"] / 1000.0
    grouped["sell_shares"] = grouped["sell_shares_raw"] / 1000.0
    grouped["net_amount"] = grouped["buy_amount"] - grouped["sell_amount"]
    grouped = grouped[
        (grouped["buy_amount"] != 0)
        | (grouped["sell_amount"] != 0)
        | (grouped["net_amount"] != 0)
    ].copy()
    grouped["side"] = grouped["net_amount"].map(lambda v: "買超" if v >= 0 else "賣超")

    out = grouped[EVENT_COLUMNS].sort_values(
        ["Date", "warrant_code", "broker_code"], kind="stable"
    ).reset_index(drop=True)
    print(
        f"📊 MoneyDJ 權證事件：{stock_code}｜權證 {len(codes)} 檔｜"
        f"{len(out):,} 筆｜{out['Date'].min().date()} ~ {out['Date'].max().date()}"
    )
    return out


# ------------------------------------------- 股票名稱／交易日曆／三大法人


TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

# 三大法人掛在 Yahoo 台股。國際版 query1.finance.yahoo.com 沒有台股法人資料，
# 只有這個台灣站的端點有。
YAHOO_TW_STATS_URL = (
    "https://tw.stock.yahoo.com/_td-stock/api/resource/"
    "StockServices.tradesWithQuoteStats;limit={limit};period=day;symbol={symbol}"
)
YAHOO_TW_HEADERS = {
    "User-Agent": YAHOO_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Accept-Language": "zh-TW",
    "Referer": "https://tw.stock.yahoo.com/",
}

_STOCK_NAME_CACHE: dict | None = None


def load_stock_name_map() -> dict:
    """股票代號 → 中文簡稱，取代 FinMind TaiwanStockInfo。

    這一項沒走 Yahoo：它只給英文名（2464 是 MIRLE AUTOMATION），
    中文報告用不了，所以改讀證交所與櫃買的公司基本資料。
    """
    global _STOCK_NAME_CACHE
    if _STOCK_NAME_CACHE is not None:
        return _STOCK_NAME_CACHE

    names: dict[str, str] = {}
    for label, url, code_key, name_key in (
        ("上市", TWSE_COMPANY_URL, "公司代號", "公司簡稱"),
        ("上櫃", TPEX_COMPANY_URL, "SecuritiesCompanyCode", "CompanyAbbreviation"),
    ):
        try:
            rows = requests.get(url, headers={"Accept": "application/json"}, timeout=(8, 40)).json()
            added = 0
            for row in rows:
                code = str(row.get(code_key) or "").strip()
                name = str(row.get(name_key) or "").strip()
                if code and name:
                    names.setdefault(code, name)
                    added += 1
            print(f"📦 {label}公司名稱：{added:,} 筆")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ {label}公司基本資料讀取失敗：{exc}")

    _STOCK_NAME_CACHE = names
    return names


def stock_name_of(stock_code: str) -> str:
    """查不到就回代號本身，與原本 FinMind 版的行為一致。"""
    code = _normalize_code(stock_code)
    name = load_stock_name_map().get(code, "")
    if not name:
        print(f"⚠️ 公司基本資料查不到 {code}，本次以代號當名稱")
        return code
    return name


def trading_dates_yahoo(start_date, end_date, reference: str = "0050") -> list[pd.Timestamp]:
    """市場實際有成交的日期。

    沿用原本的想法：不看公告的行事曆，改看一檔高流動性標的實際成交在哪幾天，
    臨時休市（颱風）自然不會被算進去。原本用 FinMind 的 0050 股價，
    現在直接用 Yahoo 的 K 線日期——同一個道理，少一個資料源。
    """
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    span = max(30, (end_ts - start_ts).days + 10)
    frame, _, _ = fetch_stock_data_yahoo(reference, period=f"{span}d")
    return sorted(d for d in frame.index if start_ts <= d <= end_ts)


def fetch_institutional_yahoo(stock_code: str, days: int = 80) -> pd.DataFrame:
    """三大法人買賣超，回傳 Date/foreign/invest/dealer/total，單位「張」。

    對照過 FinMind：外資與投信逐日數字完全相同。

    ⚠️ 自營商是不同口徑。Yahoo 只給一個 dealerDiffVolK，那是「自營商合計」
    （自行買賣 ＋ 避險）；原本的 FinMind 版取「自行買賣」。以 2330 在
    2026-08-06 為例：Yahoo -583、FinMind 自行 -273、避險 -311。
    這是刻意的行為變更，圖上自營商那條線會比舊版大，不是算錯。
    """
    code = _normalize_code(stock_code)
    limit = max(10, min(int(days), 200))

    rows = []
    for suffix in ("TW", "TWO"):
        url = YAHOO_TW_STATS_URL.format(limit=limit, symbol=f"{code}.{suffix}")
        try:
            payload = requests.get(url, headers=YAHOO_TW_HEADERS, timeout=(8, 40)).json()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ Yahoo 三大法人讀取失敗：{code}.{suffix}｜{exc}")
            continue
        rows = payload.get("list") or []
        if rows:
            break

    if not rows:
        print(f"⚠️ Yahoo 查無三大法人資料：{code}")
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    out = pd.DataFrame()
    out["Date"] = (
        pd.to_datetime(frame["date"], errors="coerce", utc=True)
        .dt.tz_convert("Asia/Taipei")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    # 欄名尾巴的 VolK 就是「千股」也就是張，不必再換算單位。
    for target, source in (
        ("foreign", "foreignDiffVolK"),
        ("invest", "investmentTrustDiffVolK"),
        ("dealer", "dealerDiffVolK"),
    ):
        out[target] = pd.to_numeric(frame.get(source), errors="coerce").fillna(0.0).astype(float)
    out["total"] = out["foreign"] + out["invest"] + out["dealer"]
    out = out.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    print(
        f"✅ Yahoo 三大法人：{code}｜{len(out)} 筆｜"
        f"{out['Date'].min().date()} ~ {out['Date'].max().date()}"
    )
    return out[["Date", "foreign", "invest", "dealer", "total"]]


# ---------------------------------------------------------------- 對照測試


def _finmind_one_day(warrant_code: str, day: str) -> pd.DataFrame:
    token = (os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_TOKEN") or "").strip()
    if not token:
        return pd.DataFrame()
    response = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params={
            "dataset": "TaiwanStockWarrantTradingDailyReport",
            "data_id": warrant_code,
            # 這個資料集一次只給一天，帶 end_date 會被拒絕。
            "start_date": day,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=(8, 60),
    )
    rows = (response.json() or {}).get("data") or []
    return pd.DataFrame(rows)


def _compare(warrant_codes: list[str], day: str) -> None:
    mine = fetch_warrant_branch_daily(warrant_codes, days=5)
    mine_day = mine[mine["date"] == day]

    for code in warrant_codes:
        fin = _finmind_one_day(code, day)
        got = mine_day[mine_day["stock_id"] == code]
        print(f"\n=== {code}｜{day} ===")
        if fin.empty:
            print(f"FinMind 無資料或未設 token；MoneyDJ 取得 {len(got)} 筆")
            continue

        # 同一分點同日可能有多筆（不同成交價），要先加總，否則 .loc 會回 Series。
        fin_key = fin.groupby("securities_trader_id")[["buy", "sell"]].sum().sort_index()
        got_key = got.groupby("securities_trader_id")[["buy", "sell"]].sum().sort_index()
        fin_names = fin.groupby("securities_trader_id")["securities_trader"].first()
        only_fin = sorted(set(fin_key.index) - set(got_key.index))
        only_djs = sorted(set(got_key.index) - set(fin_key.index))
        both = sorted(set(fin_key.index) & set(got_key.index))

        print(f"FinMind {len(fin_key)} 筆｜MoneyDJ {len(got_key)} 筆｜共同 {len(both)}")
        if only_fin:
            detail = ", ".join(
                f"{b}({fin_names.get(b, '')} 買{int(fin_key.loc[b, 'buy'])}/賣{int(fin_key.loc[b, 'sell'])})"
                for b in only_fin
            )
            print(f"  ⚠️ 只有 FinMind 有的分點：{detail}")
        if only_djs:
            print(f"  ⚠️ 只有 MoneyDJ 有的分點：{only_djs}")

        mismatched = 0
        for broker in both:
            f_row = fin_key.loc[broker]
            g_row = got_key.loc[broker]
            if int(f_row["buy"]) != int(g_row["buy"]) or int(f_row["sell"]) != int(g_row["sell"]):
                mismatched += 1
                if mismatched <= 5:
                    print(
                        f"  ❌ {broker}：FinMind 買{int(f_row['buy'])}/賣{int(f_row['sell'])}"
                        f" vs MoneyDJ 買{int(g_row['buy'])}/賣{int(g_row['sell'])}"
                    )
        if both and not mismatched and not only_fin and not only_djs:
            print("  ✅ 分點與買賣股數完全一致")
        elif both:
            print(f"  買賣量不一致 {mismatched}/{len(both)} 組")

# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "price"
    targets = sys.argv[2:]
    if mode == "warrant":
        codes = targets or ["03882T", "03881T"]
        day = os.getenv("COMPARE_DAY", "").strip()
        if not day:
            probe = fetch_warrant_branch_daily(codes[:1], days=5)
            day = probe["date"].max() if not probe.empty else ""
        if not day:
            print("抓不到任何交易日，無法比對")
            sys.exit(1)
        print(f"\n比對日期：{day}")
        _compare(codes, day)
    else:
        for target in (targets or ["2330", "6488"]):
            try:
                _compare_with_finmind(_normalize_code(target))
            except Exception as exc:  # noqa: BLE001
                print(f"{target}：{exc}")

