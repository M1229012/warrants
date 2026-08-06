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

import os
import re
import sys
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


API4_URL = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata/9b/6e/0a/"
    "TwWarrantData.xdjjson?a={code}&x=warrant-chip0002-4&c={day}&d={day}"
    "&revision=2018_07_31_1"
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

OUTPUT_COLUMNS = [
    "date",
    "stock_id",
    "securities_trader",
    "securities_trader_id",
    "buy",
    "sell",
    "price",
]


def _get_rows(url: str) -> list[dict]:
    """MoneyDJ 有時回單一物件、有時回陣列，兩種都要吃。"""
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
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
        return code, _get_rows(API4_URL.format(code=code, day=day))

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


def fetch_warrant_branch_daily(warrant_codes, days: int = 5) -> pd.DataFrame:
    """回傳與 FinMind TaiwanStockWarrantTradingDailyReport 相同欄位的 DataFrame。"""
    warrant_codes = [str(c).strip().upper() for c in warrant_codes if str(c).strip()]
    if not warrant_codes:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # 週末日 API4 會回 HTTP 錯誤而不是空結果，每一個都要耗掉三次重試與退避。
    # 先濾掉，探索次數少三成、時間也跟著省下來。國定假日仍會落空，那個從日期看不出來。
    today = datetime.now(timezone.utc) + timedelta(hours=8)
    calendar_days = []
    offset = 0
    while len(calendar_days) < int(days) + 2 and offset < int(days) + 12:
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

