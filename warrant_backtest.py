"""
認購權證特定分點買超回測系統
=================================
A：單檔權證單日買進金額 >= 100萬
B：同一分點 + 同一標的 + 同一天，多檔認購權證合計買超金額 >= 100萬
C：同一分點 + 同一標的，連續3個交易日多檔認購權證累積買超金額 >= 100萬
D：同一分點 + 同一標的，近10個交易日累積淨買進金額 >= 100萬

互斥規則：同一檔權證代號只會出現在 A / B / C / D 其中一類，優先順序為 A > B > C > D。

輸出 Excel：
1. A_單檔大買
2. B_同標的單日合計
3. C_同標的3日累積
4. D_近10日累積淨買進
5. 勝率統計
6. 近兩月買賣金額排行
7. 近兩月分點數排行
8. 券商查詢
9. 顏色說明

執行：python warrant_backtest.py
依賴：pip install requests pandas openpyxl
"""

import json, re, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


# ══════════════════════════════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════════════════════════════

DEFAULT_OUTPUT_DIR = "output" if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true" else r"C:\Users\chen1_ukw0m7r\Downloads"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
AMOUNT_THRESH = 1_000_000
MAX_WORKERS   = 50
DAYS_HISTORY  = 250
RECENT_RANKING_DAYS = 62
D_WINDOW_DAYS = 10

# 快取設定：
# 第一次執行沒有快取時會完整爬取並建立快取；
# 第二次之後會優先讀取快取，只針對最近有出現目標分點的候選組合補抓新資料。
USE_CACHE = os.getenv("USE_CACHE", "1").strip().lower() not in ("0", "false", "no")
FORCE_FULL_CACHE_REFRESH = os.getenv("FORCE_FULL_CACHE_REFRESH", "0").strip().lower() in ("1", "true", "yes")
CACHE_RECENT_SCAN_DAYS = int(os.getenv("CACHE_RECENT_SCAN_DAYS", "3"))
PRICE_WORKERS = int(os.getenv("PRICE_WORKERS", "60"))
PRESCAN_WORKERS = int(os.getenv("PRESCAN_WORKERS", "60"))
FIND_BROKER_WORKERS = int(os.getenv("FIND_BROKER_WORKERS", "40"))

# 加速模式：
# 1. 有候選組合快取時，預設不再每天掃描全市場權證，只更新既有候選組合的 API5 歷史資料。
#    若需要重新發現新權證 / 新候選組合，可執行前設定 FAST_SKIP_RECENT_PRESCAN=0。
# 2. B / C / D 工作表的 D+ 欄位只使用標的股價格，預設不再額外抓群組事件中每一檔權證價格。
#    若未來需要群組事件權證明細價格，可設定 FETCH_GROUP_WARRANT_PRICES=1。
FAST_SKIP_RECENT_PRESCAN = os.getenv("FAST_SKIP_RECENT_PRESCAN", "1").strip().lower() not in ("0", "false", "no")
FETCH_GROUP_WARRANT_PRICES = os.getenv("FETCH_GROUP_WARRANT_PRICES", "0").strip().lower() in ("1", "true", "yes")

CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(OUTPUT_DIR, "warrant_cache"))
CACHE_ENCODING = "utf-8-sig"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

WARRANTS_CACHE_PATH   = os.path.join(CACHE_DIR, "warrants_cache.csv")
BROKER_MAP_CACHE_PATH = os.path.join(CACHE_DIR, "broker_map_cache.csv")
CANDIDATES_CACHE_PATH = os.path.join(CACHE_DIR, "candidates_cache.csv")
HISTORY_CACHE_PATH    = os.path.join(CACHE_DIR, "broker_warrant_history_cache.csv")
PRICE_CACHE_PATH      = os.path.join(CACHE_DIR, "price_cache.csv")

# prescan_all() 會更新這個集合，主流程用它判斷哪些候選組合需要重新 api5_get。
PRESCAN_REFRESH_KEYS = set()

TARGET_PATTERNS = {
    "富邦公益":       r"富邦.*公益",
    "富邦北高雄":     r"富邦.*北高雄",
    "富邦台北":       r"富邦.*台北",
    "富邦敦南":       r"富邦.*敦南",
    "新光":           r"^新光$",
    "永豐金內湖":     r"永豐.*內湖",
    "永豐金竹北":     r"永豐.*竹北",
    "永豐金竹科":     r"永豐.*竹科",
    "永豐金萬盛":     r"永豐.*萬盛",
    "華南永昌世貿":   r"華南.*世貿",
    "華南永昌台中":   r"華南.*台中",
    "華南永昌岡山":   r"華南.*岡山",
    "福邦":           r"^福邦",
    "第一金":         r"^第一金$",
    "第一金安和":     r"第一金.*安和",
    "群益金鼎中壢":   r"群益.*中壢",
    "群益金鼎北高雄": r"群益.*北高雄",
    "群益金鼎古亭":   r"群益.*古亭",
    "元大內湖民權":   r"元大.*(內湖.*民權|民權)",
    "元大南屯":       r"元大.*南屯",
    "元大善化":       r"元大.*善化",
    "元大敦化":       r"元大.*敦化",
    "元大雙和":       r"元大.*雙和",
    "兆豐小港":       r"兆豐.*小港",
    "凱基士林":       r"凱基.*士林",
    "凱基科園":       r"凱基.*科園",
    "國票中正":       r"國票.*中正",
    "國票敦北法人":   r"國票.*(敦北|法人)",
}

FALLBACK = {
    "富邦公益":       ("富邦-公益",       "961F"),
    "富邦北高雄":     ("富邦-北高雄",     "962Q"),
    "富邦台北":       ("富邦-台北",       "9623"),
    "富邦敦南":       ("富邦-敦南",       "9663"),
    "新光":           ("新光",             "8560"),
    "永豐金內湖":     ("永豐金-內湖",     "9A9g"),
    "永豐金竹北":     ("永豐金-竹北",     "9A9P"),
    "永豐金竹科":     ("永豐金-竹科",     "9A9X"),
    "永豐金萬盛":     ("永豐金-萬盛",     "9A92"),
    "華南永昌世貿":   ("華南永昌-世貿",   "9334"),
    "華南永昌台中":   ("華南永昌-台中",   "9302"),
    "華南永昌岡山":   ("華南永昌-岡山",   "9324"),
    "福邦":           ("福邦",             "6480"),
    "第一金":         ("第一金",           "5380"),
    "第一金安和":     ("第一金-安和",      "538j"),
    "群益金鼎中壢":   ("群益金鼎-中壢",   "918A"),
    "群益金鼎北高雄": ("群益金鼎-北高雄", "913R"),
    "群益金鼎古亭":   ("群益金鼎-古亭",   "918C"),
    "元大內湖民權":   ("元大-內湖民權",   "9867"),
    "元大南屯":       ("元大-南屯",       "9853"),
    "元大善化":       ("元大-善化",       "981y"),
    "元大敦化":       ("元大-敦化",       "9833"),
    "元大雙和":       ("元大-雙和",       "9874"),
    "兆豐小港":       ("兆豐-小港",       "700R"),
    "凱基士林":       ("凱基-士林",       "9238"),
    "凱基科園":       ("凱基-科園",       "9254"),
    "國票中正":       ("國票-中正",       "7797"),
    "國票敦北法人":   ("國票-敦北法人",   "779c"),
}

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://pscnetsecrwd.moneydj.com/",
}

API4 = ("https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata"
        "/9b/6e/0a/TwWarrantData.xdjjson"
        "?a={code}&x=warrant-chip0002-4&c={start}&d={end}&revision=2018_07_31_1")

API5 = ("https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata"
        "/d8/f5/27/twWarrantData.xdjjson"
        "?x=warrant-chip0002-5&c=250&a={warrant}&b={broker}&revision=2018_07_31_1")

# Excel 顏色（柔和舒適版）
RED    = PatternFill("solid", fgColor="F4CCCC")
GREEN  = PatternFill("solid", fgColor="D9EAD3")
BLUE   = PatternFill("solid", fgColor="D9EAF7")
ORANGE = PatternFill("solid", fgColor="FCE5CD")
YELLOW = PatternFill("solid", fgColor="FFF2CC")
GRAY   = PatternFill("solid", fgColor="E7E6E6")
WHITE  = PatternFill("solid", fgColor="FFFFFF")

# 出清且獲利時使用的外框（不改底色，只用外框凸顯）
PROFIT_EXIT_SIDE = Side(style="thick", color="C00000")
LOSS_EXIT_SIDE   = Side(style="thick", color="38761D")


# ══════════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════════

def parse_date(date_str):
    try:
        if date_str is None:
            return None
        s = str(date_str).strip()
        if not s or s == "-":
            return None
        s = s.replace("-", "/")
        parts = s.split("/")
        if len(parts) != 3:
            return None
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return datetime(y, m, d)
    except:
        return None


def normalize_date_str(date_str):
    dt = parse_date(date_str)
    return dt.strftime("%Y/%m/%d") if dt else str(date_str).strip()


def add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    return datetime(year, month, 1)


def iter_month_starts(start_dt, end_dt):
    start_dt = datetime(start_dt.year, start_dt.month, 1)
    end_dt   = datetime(end_dt.year, end_dt.month, 1)

    cur = start_dt
    while cur <= end_dt:
        yield cur.strftime("%Y%m01")
        cur = add_months(cur, 1)


def fmt_pct(v):
    return "-" if v is None else f"{v:+.2f}%"


def fmt_num(v):
    return "-" if v is None else v


def fmt_amount(v):
    if v is None:
        return "-"
    try:
        return f"{int(round(float(v))):,}"
    except:
        return str(v)


def calc_result_tag(return_pct):
    if return_pct is None:
        return "未出清"
    if return_pct > 0:
        return "勝"
    if return_pct < 0:
        return "敗"
    return "平手"


def match_target(name):
    for label, pat in TARGET_PATTERNS.items():
        if re.search(pat, name):
            return label
    return ""


def api4_get(code, start, end):
    try:
        r = requests.get(API4.format(code=code, start=start, end=end), headers=HDR, timeout=15)
        data = json.loads(r.content.decode("utf-8"))
        rows = []
        for item in (data if isinstance(data, list) else [data]):
            rows.extend(item.get("ResultSet", {}).get("Result", []))
        return rows
    except:
        return []


def api5_get(warrant, broker):
    try:
        r = requests.get(API5.format(warrant=warrant, broker=broker), headers=HDR, timeout=15)
        data = json.loads(r.content.decode("utf-8"))
        rs = data[0].get("ResultSet", {}) if isinstance(data, list) else data.get("ResultSet", {})
        return rs.get("Result", [])
    except:
        return []


def safe_price_float(x):
    try:
        s = str(x).replace(",", "").replace("--", "").replace("X", "").strip()

        if s in ["", "-", "---", "除權息", "None", "nan", "null"]:
            return None

        v = float(s)

        # 權證 / 股價不應該用 0 當有效收盤價。
        # 測試時發現部分權證會回傳 0.0，不能拿來計算 D+。
        if v <= 0:
            return None

        return v
    except:
        return None


def merge_price_dicts(*dicts):
    merged = {}

    for prices in dicts:
        if not prices:
            continue

        for d, p in prices.items():
            if p is not None and p > 0:
                merged[d] = p

    return merged


def normalize_price_code(code):
    s = str(code).strip()

    if s.endswith(".0"):
        s = s[:-2]

    s = "".join(ch for ch in s if ch.isdigit())

    if not s:
        return ""

    # 權證通常為 6 碼；股票通常為 4 碼。
    # 若是 5 碼權證，很可能是 Excel / pandas 吃掉前導 0，補回 6 碼。
    if len(s) == 5:
        return s.zfill(6)

    return s


def price_code_variants(code):
    code = normalize_price_code(code)

    if not code:
        return []

    variants = [code]

    no_zero = code.lstrip("0")
    if no_zero and no_zero != code:
        variants.append(no_zero)

    out = []
    for v in variants:
        if v not in out:
            out.append(v)

    return out


def yahoo_symbol_variants(code):
    symbols = []

    for c in price_code_variants(code):
        symbols.append(f"{c}.TW")
        symbols.append(f"{c}.TWO")

    out = []
    for s in symbols:
        if s not in out:
            out.append(s)

    return out


def fetch_twse_stock_day_prices(code, start_dt=None, end_dt=None):
    today = datetime.today()
    prices = {}
    code = normalize_price_code(code)

    if not code:
        return prices

    if start_dt is None:
        start_dt = add_months(datetime(today.year, today.month, 1), -13)
    if end_dt is None:
        end_dt = today

    if end_dt > today:
        end_dt = today

    if start_dt > end_dt:
        start_dt = end_dt

    for month_dt in iter_month_starts(start_dt, end_dt):
        try:
            rp = requests.get(
                f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
                f"?response=json&date={month_dt}&stockNo={code}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8
            )
            data = rp.json()

            for row in data.get("data", []):
                try:
                    parts = str(row[0]).split("/")
                    dk = f"{int(parts[0]) + 1911}/{int(parts[1]):02d}/{int(parts[2]):02d}"
                    close_price = safe_price_float(row[6])

                    if close_price is not None:
                        prices[dk] = close_price
                except:
                    pass
        except:
            pass

    return prices


def fetch_tpex_new_trading_stock_prices(code, start_dt=None, end_dt=None):
    today = datetime.today()
    prices = {}
    code = normalize_price_code(code)

    if not code:
        return prices

    if start_dt is None:
        start_dt = add_months(datetime(today.year, today.month, 1), -13)
    if end_dt is None:
        end_dt = today

    if end_dt > today:
        end_dt = today

    if start_dt > end_dt:
        start_dt = end_dt

    for month_start in iter_month_starts(start_dt, end_dt):
        try:
            month_dt = datetime.strptime(month_start, "%Y%m%d")
            date_str = month_dt.strftime("%Y/%m/01")

            urls = [
                (
                    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
                    f"?code={code}&date={date_str}&response=json"
                ),
                (
                    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
                    f"?code={code}&date={date_str}&type=EW&response=json"
                ),
            ]

            for url in urls:
                try:
                    rp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                    data = rp.json()

                    rows = []

                    if isinstance(data, dict):
                        if isinstance(data.get("tables"), list):
                            for table in data.get("tables", []):
                                if isinstance(table, dict) and isinstance(table.get("data"), list):
                                    rows.extend(table.get("data", []))

                        if isinstance(data.get("data"), list):
                            rows.extend(data.get("data", []))

                        if isinstance(data.get("aaData"), list):
                            rows.extend(data.get("aaData", []))

                    for row in rows:
                        try:
                            if not isinstance(row, (list, tuple)) or not row:
                                continue

                            raw_date = str(row[0]).strip().replace("-", "/")
                            parts = raw_date.split("/")

                            if len(parts) != 3:
                                continue

                            y = int(parts[0])

                            if y < 1911:
                                y += 1911

                            dk = f"{y}/{int(parts[1]):02d}/{int(parts[2]):02d}"

                            close_price = None

                            # 測試結果顯示 TPEx 新版 tradingStock 是 70xxxx 權證與上櫃股最穩來源。
                            # 常見收盤價欄位落在 6 / 5 / 4 / 7 / 3，逐一嘗試。
                            for idx in [6, 5, 4, 7, 3]:
                                if idx < len(row):
                                    v = safe_price_float(row[idx])

                                    if v is not None:
                                        close_price = v
                                        break

                            if close_price is not None:
                                prices[dk] = close_price
                        except:
                            pass

                except:
                    pass

        except:
            pass

    return prices


def fetch_tpex_old_st43_prices(code, start_dt=None, end_dt=None):
    today = datetime.today()
    prices = {}
    code = normalize_price_code(code)

    if not code:
        return prices

    if start_dt is None:
        start_dt = add_months(datetime(today.year, today.month, 1), -13)
    if end_dt is None:
        end_dt = today

    if end_dt > today:
        end_dt = today

    if start_dt > end_dt:
        start_dt = end_dt

    for month_start in iter_month_starts(start_dt, end_dt):
        try:
            month_dt = datetime.strptime(month_start, "%Y%m%d")
            roc_month = f"{month_dt.year - 1911}/{month_dt.month:02d}"

            url = (
                "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/"
                f"st43_result.php?l=zh-tw&d={roc_month}&stkno={code}"
            )

            rp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            data = rp.json()

            rows = data.get("aaData", []) if isinstance(data, dict) else []

            for row in rows:
                try:
                    if not isinstance(row, (list, tuple)) or not row:
                        continue

                    raw_date = str(row[0]).strip().replace("-", "/")
                    parts = raw_date.split("/")

                    if len(parts) != 3:
                        continue

                    y = int(parts[0])

                    if y < 1911:
                        y += 1911

                    dk = f"{y}/{int(parts[1]):02d}/{int(parts[2]):02d}"

                    close_price = None

                    for idx in [6, 5, 4, 7, 3]:
                        if idx < len(row):
                            v = safe_price_float(row[idx])

                            if v is not None:
                                close_price = v
                                break

                    if close_price is not None:
                        prices[dk] = close_price
                except:
                    pass
        except:
            pass

    return prices


def fetch_yahoo_chart_prices(symbol, start_dt=None, end_dt=None, host="query1"):
    today = datetime.today()
    prices = {}

    if start_dt is None:
        start_dt = add_months(datetime(today.year, today.month, 1), -13)
    if end_dt is None:
        end_dt = today

    if end_dt > today:
        end_dt = today

    if start_dt > end_dt:
        start_dt = end_dt

    period1 = int(start_dt.timestamp())
    period2 = int((end_dt + timedelta(days=1)).timestamp())

    url = (
        f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history"
    )

    try:
        rp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        data = rp.json()

        result = data.get("chart", {}).get("result", [])

        if not result:
            return prices

        result = result[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])

        for ts, close_price in zip(timestamps, closes):
            v = safe_price_float(close_price)

            if v is None:
                continue

            dt = datetime.fromtimestamp(int(ts))
            prices[dt.strftime("%Y/%m/%d")] = v
    except:
        pass

    return prices


def fetch_yahoo_range_prices(symbol):
    prices = {}

    # 只在官方來源不足時才會進入 Yahoo，這裡用 5y + max 做快速備援。
    for range_value in ["5y", "max"]:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range={range_value}&interval=1d&events=history"
        )

        try:
            rp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            data = rp.json()

            result = data.get("chart", {}).get("result", [])

            if not result:
                continue

            result = result[0]
            timestamps = result.get("timestamp", [])
            quote = result.get("indicators", {}).get("quote", [{}])[0]
            closes = quote.get("close", [])

            for ts, close_price in zip(timestamps, closes):
                v = safe_price_float(close_price)

                if v is None:
                    continue

                dt = datetime.fromtimestamp(int(ts))
                prices[dt.strftime("%Y/%m/%d")] = v

            if prices:
                break
        except:
            pass

    return prices


def fetch_yahoo_download_prices(symbol, start_dt=None, end_dt=None):
    today = datetime.today()
    prices = {}

    if start_dt is None:
        start_dt = add_months(datetime(today.year, today.month, 1), -13)
    if end_dt is None:
        end_dt = today

    if end_dt > today:
        end_dt = today

    if start_dt > end_dt:
        start_dt = end_dt

    period1 = int(start_dt.timestamp())
    period2 = int((end_dt + timedelta(days=1)).timestamp())

    url = (
        f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )

    try:
        rp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)

        if rp.status_code != 200 or "Date" not in rp.text:
            return prices

        df = pd.read_csv(StringIO(rp.text))

        if "Date" not in df.columns or "Close" not in df.columns:
            return prices

        for _, row in df.iterrows():
            dt = parse_date(row["Date"])
            close_price = safe_price_float(row["Close"])

            if dt and close_price is not None:
                prices[dt.strftime("%Y/%m/%d")] = close_price
    except:
        pass

    return prices


def fetch_yahoo_prices(code, start_dt=None, end_dt=None):
    """
    Yahoo 備援：
    1. 同時測補零版與去零版，例如 064390.TW / 64390.TW。
    2. 同時測 .TW / .TWO。
    3. 優先 query1 chart，其次 query2、range、download。
    """
    prices = {}

    for symbol in yahoo_symbol_variants(code):
        p = fetch_yahoo_chart_prices(symbol, start_dt, end_dt, host="query1")

        if p:
            return p

        p = fetch_yahoo_chart_prices(symbol, start_dt, end_dt, host="query2")

        if p:
            return p

    for symbol in yahoo_symbol_variants(code):
        p = fetch_yahoo_range_prices(symbol)

        if p:
            return p

    for symbol in yahoo_symbol_variants(code):
        p = fetch_yahoo_download_prices(symbol, start_dt, end_dt)

        if p:
            return p

    return prices


def prices_need_yahoo_fallback(prices, start_dt=None, end_dt=None):
    """
    判斷官方來源價格是否不足，需要 Yahoo 備援。

    只用 len(prices) < 2 不夠，因為有些來源雖然有幾筆，
    但覆蓋不到 D+1 ~ D+20 的日期，Excel 還是會出現大量 權:- / 標:-。
    """
    if not prices:
        return True

    valid_dates = sorted([d for d, p in prices.items() if p is not None and p > 0])

    if not valid_dates:
        return True

    # 如果測試區間超過 30 天，但有效價格少於 10 筆，代表覆蓋率明顯不足。
    if start_dt and end_dt:
        try:
            span_days = (end_dt - start_dt).days
            if span_days >= 30 and len(valid_dates) < 10:
                return True
        except:
            pass

    # 如果最後一筆價格離需要的結束日太遠，也要補 Yahoo。
    if end_dt:
        try:
            latest_dt = parse_date(valid_dates[-1])
            target_end = min(end_dt, datetime.today())

            if latest_dt and (target_end - latest_dt).days > 10:
                return True
        except:
            pass

    return False


def fetch_twse_prices(code, start_dt=None, end_dt=None):
    """
    統一價格抓取函式（保留原函式名稱，避免改動其他流程）：

    速度與準確率策略：
    1. 先用官方資料源：
       - 上市股票 / 上市權證：TWSE STOCK_DAY
       - 上櫃股票 / 上櫃權證：TPEx tradingStock
    2. 若官方來源覆蓋率不足，再補 Yahoo。
    3. 價格 <= 0 一律不採用。
    4. 權證 5 碼會自動補回 6 碼，避免前導 0 被吃掉。
    """
    code = normalize_price_code(code)

    if not code:
        return {}

    prices = {}

    # 先根據代號型態決定優先順序，減少不必要請求。
    # 70xxxx 權證大多走 TPEx；0xxxxx 權證大多走 TWSE。
    if len(code) == 6 and code.startswith("7"):
        prices = merge_price_dicts(
            prices,
            fetch_tpex_new_trading_stock_prices(code, start_dt, end_dt)
        )

        if prices_need_yahoo_fallback(prices, start_dt, end_dt):
            prices = merge_price_dicts(
                prices,
                fetch_tpex_old_st43_prices(code, start_dt, end_dt)
            )

        if prices_need_yahoo_fallback(prices, start_dt, end_dt):
            prices = merge_price_dicts(
                prices,
                fetch_twse_stock_day_prices(code, start_dt, end_dt)
            )

    elif len(code) == 6 and code.startswith("0"):
        prices = merge_price_dicts(
            prices,
            fetch_twse_stock_day_prices(code, start_dt, end_dt)
        )

        if prices_need_yahoo_fallback(prices, start_dt, end_dt):
            prices = merge_price_dicts(
                prices,
                fetch_tpex_new_trading_stock_prices(code, start_dt, end_dt)
            )

    else:
        prices = merge_price_dicts(
            prices,
            fetch_twse_stock_day_prices(code, start_dt, end_dt)
        )

        if prices_need_yahoo_fallback(prices, start_dt, end_dt):
            prices = merge_price_dicts(
                prices,
                fetch_tpex_new_trading_stock_prices(code, start_dt, end_dt)
            )

        if prices_need_yahoo_fallback(prices, start_dt, end_dt):
            prices = merge_price_dicts(
                prices,
                fetch_tpex_old_st43_prices(code, start_dt, end_dt)
            )

    # 官方來源覆蓋率不足時才用 Yahoo，兼顧速度與完整度。
    if prices_need_yahoo_fallback(prices, start_dt, end_dt):
        prices = merge_price_dicts(
            prices,
            fetch_yahoo_prices(code, start_dt, end_dt)
        )

    return prices


def get_price_nearest(prices, date):
    date = normalize_date_str(date)

    if date in prices:
        return prices[date]

    before = [d for d in sorted(prices) if d <= date]
    return prices[before[-1]] if before else None







# ══════════════════════════════════════════════════════════════════════
# Google Sheet 快取 / 結果同步工具（GitHub Actions 部署用）
# ══════════════════════════════════════════════════════════════════════

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "權證分點籌碼")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GSHEET_CACHE_ENABLED = os.getenv("GSHEET_CACHE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
GSHEET_RESULT_ENABLED = os.getenv("GSHEET_RESULT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
GSHEET_CHUNK_ROWS = int(os.getenv("GSHEET_CHUNK_ROWS", "3000"))

_GSHEET_CLIENT = None
_GSHEET_SPREADSHEET = None

CACHE_SHEET_NAME_MAP = {
    "warrants_cache.csv": "快取_權證清單",
    "broker_map_cache.csv": "快取_分點代號",
    "candidates_cache.csv": "快取_候選組合",
    "broker_warrant_history_cache.csv": "快取_分點歷史",
    "price_cache.csv": "快取_價格",
}


def gsheet_enabled():
    return bool(os.getenv("GCP_SERVICE_KEY", "").strip())


def get_gsheet_client():
    global _GSHEET_CLIENT

    if _GSHEET_CLIENT is not None:
        return _GSHEET_CLIENT

    service_key = os.getenv("GCP_SERVICE_KEY", "").strip()

    if not service_key:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(service_key)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _GSHEET_CLIENT = gspread.authorize(creds)
        return _GSHEET_CLIENT
    except Exception as e:
        print(f"  ⚠️ Google Sheet 金鑰初始化失敗：{type(e).__name__}: {e}")
        return None


def get_gsheet_spreadsheet():
    global _GSHEET_SPREADSHEET

    if _GSHEET_SPREADSHEET is not None:
        return _GSHEET_SPREADSHEET

    gc = get_gsheet_client()

    if gc is None:
        return None

    try:
        if GOOGLE_SHEET_ID:
            _GSHEET_SPREADSHEET = gc.open_by_key(GOOGLE_SHEET_ID)
        else:
            _GSHEET_SPREADSHEET = gc.open(GOOGLE_SHEET_NAME)
        return _GSHEET_SPREADSHEET
    except Exception as e:
        try:
            _GSHEET_SPREADSHEET = gc.create(GOOGLE_SHEET_NAME)
            print(f"  ✅ 已建立 Google Sheet：{GOOGLE_SHEET_NAME}")
            return _GSHEET_SPREADSHEET
        except Exception as e2:
            print(f"  ⚠️ Google Sheet 開啟/建立失敗：{type(e).__name__}: {e} / {type(e2).__name__}: {e2}")
            return None


def safe_worksheet_title(title):
    title = str(title).strip()
    bad_chars = [":", "\\", "/", "?", "*", "[", "]"]
    for ch in bad_chars:
        title = title.replace(ch, "_")
    return title[:100] if title else "工作表"


def cache_sheet_name_from_path(path):
    base = os.path.basename(str(path))
    return CACHE_SHEET_NAME_MAP.get(base, safe_worksheet_title(f"快取_{os.path.splitext(base)[0]}"))


def get_or_create_worksheet(title, rows=100, cols=20):
    sh = get_gsheet_spreadsheet()

    if sh is None:
        return None

    title = safe_worksheet_title(title)

    try:
        return sh.worksheet(title)
    except Exception:
        try:
            return sh.add_worksheet(title=title, rows=max(int(rows), 1), cols=max(int(cols), 1))
        except Exception as e:
            print(f"  ⚠️ 建立工作表失敗：{title}，原因：{type(e).__name__}: {e}")
            return None


def get_or_recreate_result_worksheet(title, rows=100, cols=20):
    """
    結果工作表每次同步前重新建立，避免沿用舊的 Google Sheet 純文字格式。

    先前「券商查詢」的公式欄位曾被套用 TEXT 格式，導致 =IFERROR(...) 被當成文字顯示。
    Google Sheet 的 clear() 只會清內容，不一定會清掉舊格式，因此結果工作表改用刪除後重建。
    快取工作表不使用此函式，仍保留原本的快取讀寫流程。
    """
    sh = get_gsheet_spreadsheet()

    if sh is None:
        return None

    title = safe_worksheet_title(title)
    rows = max(int(rows), 1)
    cols = max(int(cols), 1)

    try:
        existing = sh.worksheet(title)

        try:
            worksheets = sh.worksheets()
            if len(worksheets) <= 1:
                sh.add_worksheet(title="__tmp_delete_guard__", rows=1, cols=1)
        except Exception:
            pass

        sh.del_worksheet(existing)
    except Exception:
        pass

    try:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)
    except Exception as e:
        print(f"  ⚠️ 重建結果工作表失敗：{title}，原因：{type(e).__name__}: {e}")
        return get_or_create_worksheet(title, rows=rows, cols=cols)


GSHEET_TEXT_HEADER_KEYWORDS = (
    "權證代號",
    "權證代碼",
    "權證清單",
    "券商代號",
    "券商代碼",
    "股票代號",
    "股票代碼",
    "標的股",
    "標的代號",
    "標的代碼",
    "證券代號",
    "證券代碼",
    "商品代號",
    "商品代碼",
    "代號",
)


def clean_gsheet_value(value):
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        return value

    return str(value)


def is_gsheet_text_header(header):
    header = str(header).strip()
    return any(keyword in header for keyword in GSHEET_TEXT_HEADER_KEYWORDS)


def add_gsheet_text_prefix(value):
    """
    讓 Google Sheet 用文字格式寫入代號欄位。

    若使用 USER_ENTERED 寫入 064390，Google Sheet 可能會自動轉成數字 64390；
    對權證代號 / 券商代號欄位加上前導單引號，可保留開頭 0。
    Google Sheet 顯示時不會顯示這個單引號，只會把儲存格視為文字。

    注意：如果儲存格內容是公式，不能加單引號，否則 Google Sheet 會把公式當成純文字顯示。
    例如「券商查詢」工作表的前 15 名查詢欄位就是公式欄位，必須保持 =IFERROR(...) 可計算。
    """
    if value is None:
        return ""

    s = str(value).strip()

    if s == "":
        return ""

    if s.startswith("="):
        return s

    if s.startswith("'"):
        return s

    return "'" + s


def strip_gsheet_text_prefix(value):
    if value is None:
        return ""

    s = str(value)

    if s.startswith("'"):
        return s[1:]

    return s


def normalize_gsheet_values_for_text_columns(values):
    """
    依據表頭自動判斷權證代號 / 券商代號 / 代號欄位，
    寫入 Google Sheet 前強制改成文字，避免前導 0 被吃掉。

    支援：
    1. 快取工作表：表頭通常在第 1 列。
    2. 一般結果工作表：表頭多數也在第 1 列。
    3. 例如 ABCD組合勝率 這種表頭在第 4 列的工作表，會掃前 10 列找表頭。
    """
    if not values:
        return values

    out = [list(row) for row in values]
    header_rows = []

    scan_limit = min(len(out), 10)
    for row_idx in range(scan_limit):
        row = out[row_idx]
        text_cols = []

        for col_idx, header in enumerate(row):
            if is_gsheet_text_header(header):
                text_cols.append(col_idx)

        if text_cols:
            header_rows.append((row_idx, text_cols))

    if not header_rows:
        return out

    for header_row_idx, text_cols in header_rows:
        next_header_rows = [idx for idx, _ in header_rows if idx > header_row_idx]
        end_row = min(next_header_rows) if next_header_rows else len(out)

        for row_idx in range(header_row_idx + 1, end_row):
            for col_idx in text_cols:
                if col_idx >= len(out[row_idx]):
                    continue

                cell_value = out[row_idx][col_idx]
                if isinstance(cell_value, str) and cell_value.strip().startswith("="):
                    continue

                out[row_idx][col_idx] = add_gsheet_text_prefix(cell_value)

    return out


def collect_gsheet_text_column_ranges(values):
    """
    根據工作表前 10 列的表頭，找出需要強制使用「純文字」格式的欄位範圍。

    這裡除了「權證代號 / 券商代號 / 代號」之外，也包含「權證清單」、
    「股票代號」、「標的股」等欄位，避免 Google Sheet 把 0 開頭的代號自動轉成數字。
    """
    ranges = []

    if not values:
        return ranges

    scan_limit = min(len(values), 10)
    header_rows = []

    for row_idx in range(scan_limit):
        row = list(values[row_idx])
        text_cols = []

        for col_idx, header in enumerate(row):
            if is_gsheet_text_header(header):
                text_cols.append(col_idx)

        if text_cols:
            header_rows.append((row_idx, text_cols))

    for header_row_idx, text_cols in header_rows:
        next_header_rows = [idx for idx, _ in header_rows if idx > header_row_idx]
        end_row = min(next_header_rows) if next_header_rows else len(values)

        if end_row <= header_row_idx + 1:
            continue

        for col_idx in text_cols:
            ranges.append({
                "start_row": header_row_idx + 1,
                "end_row": end_row,
                "start_col": col_idx,
                "end_col": col_idx + 1,
            })

    return ranges


def apply_text_format_to_gsheet(gws, values):
    """
    將 Google Sheet 中的代號相關欄位套用純文字格式。

    write_values_to_worksheet() 已經會在代號欄位前加單引號，這裡再補上
    Google Sheets 的 TEXT numberFormat，雙重避免權證代號、股票代號、券商代號
    或權證清單中的 0 開頭代號被吃掉。

    注意：「券商查詢」工作表的資料列是公式查詢結果，不可把公式欄位套成純文字，
    否則 Google Sheet 會顯示 =IFERROR(...) 文字而不是計算結果。
    權證代號與權證清單的文字格式會保留在「券商查詢資料」隱藏工作表中。
    """
    if gws is None or not values:
        return

    if str(getattr(gws, "title", "")).strip() == "券商查詢":
        return

    requests = []

    for r in collect_gsheet_text_column_ranges(values):
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": gws.id,
                    "startRowIndex": r["start_row"],
                    "endRowIndex": r["end_row"],
                    "startColumnIndex": r["start_col"],
                    "endColumnIndex": r["end_col"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {
                            "type": "TEXT"
                        }
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    _gsheet_batch_update(requests)



GSHEET_COMMA_NUMBER_HEADER_KEYWORDS = (
    "排名",
    "金額",
    "股數",
    "張數",
    "筆數",
    "事件數",
    "權證檔數",
    "買進分點數",
    "涵蓋權證數",
    "持有天數",
    "價格筆數",
)

GSHEET_COMMA_NUMBER_EXCLUDE_KEYWORDS = (
    "代號",
    "日期",
    "名稱",
    "清單",
    "類型",
    "狀態",
    "均價",
    "勝率",
    "占比",
    "比例",
    "報酬%",
    "獲利%",
)


def is_gsheet_comma_number_header(header):
    """
    判斷 Google Sheet 結果工作表中，哪些欄位要用千分位逗號顯示。

    注意：
    1. 權證代號 / 券商代號 / 代號欄位不能套數字格式，否則開頭 0 會被吃掉。
    2. 日期、名稱、清單、百分比、均價等欄位不套用千分位。
    3. 這個函式只用在「結果工作表」同步，不改快取工作表邏輯。
    """
    header = str(header).strip()

    if not header:
        return False

    if header.startswith("D+"):
        return False

    if is_gsheet_text_header(header):
        return False

    for keyword in GSHEET_COMMA_NUMBER_EXCLUDE_KEYWORDS:
        if keyword in header:
            return False

    return any(keyword in header for keyword in GSHEET_COMMA_NUMBER_HEADER_KEYWORDS)


def _parse_comma_number_for_gsheet(value):
    """
    將 1,234 / 12,345.67 這類字串轉成數字，讓 Google Sheet 可以搭配 numberFormat 顯示逗號。
    公式、空值、百分比、文字說明都保持原樣。
    """
    if value is None:
        return ""

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return value

    s = str(value).strip()

    if s == "" or s == "-":
        return value

    if s.startswith("="):
        return value

    if "%" in s or "\n" in s or "；" in s:
        return value

    raw = s.replace(",", "").strip()

    if raw.startswith("+"):
        raw = raw[1:]

    if raw.startswith("-"):
        sign = "-"
        num_part = raw[1:]
    else:
        sign = ""
        num_part = raw

    if not num_part:
        return value

    if num_part.replace(".", "", 1).isdigit():
        try:
            if "." in num_part:
                return float(sign + num_part)
            return int(sign + num_part)
        except Exception:
            return value

    return value


def normalize_result_values_for_comma_numbers(values):
    """
    Google Sheet 結果同步前，將需要千分位的欄位轉成數字。

    原因：若直接把 "1,234" 用 USER_ENTERED 寫入 Google Sheet，可能被解析成數字但顯示為 1234，
    或在部分語系下變成文字。這裡先依表頭把金額 / 股數 / 張數 / 筆數等欄位轉成數字，
    後續再用 Google Sheets numberFormat 套 #,##0，確保畫面會顯示逗號。
    """
    if not values:
        return values

    out = [list(row) for row in values]
    header_rows = []

    scan_limit = min(len(out), 10)
    for row_idx in range(scan_limit):
        row = out[row_idx]
        number_cols = []

        for col_idx, header in enumerate(row):
            if is_gsheet_comma_number_header(header):
                number_cols.append(col_idx)

        if number_cols:
            header_rows.append((row_idx, number_cols))

    if not header_rows:
        return out

    for header_row_idx, number_cols in header_rows:
        next_header_rows = [idx for idx, _ in header_rows if idx > header_row_idx]
        end_row = min(next_header_rows) if next_header_rows else len(out)

        for row_idx in range(header_row_idx + 1, end_row):
            for col_idx in number_cols:
                if col_idx >= len(out[row_idx]):
                    continue

                out[row_idx][col_idx] = _parse_comma_number_for_gsheet(out[row_idx][col_idx])

    return out


def _gsheet_number_pattern_for_header(header):
    header = str(header).strip()

    if "平均" in header or "收盤價" in header:
        return "#,##0.00"

    return "#,##0"


def apply_comma_number_format_to_gsheet(ws_xlsx, gws):
    """
    將結果工作表的金額 / 股數 / 張數 / 筆數等欄位套用 Google Sheets 千分位格式。

    這裡只補 Google Sheet 顯示格式，不改原本 Excel 產生邏輯，也不影響快取讀寫。
    """
    if gws is None:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    header_rows = []
    scan_limit = min(ws_xlsx.max_row, 10)

    for row_idx in range(1, scan_limit + 1):
        number_cols = []

        for col_idx in range(1, ws_xlsx.max_column + 1):
            header = ws_xlsx.cell(row_idx, col_idx).value

            if is_gsheet_comma_number_header(header):
                number_cols.append((col_idx, _gsheet_number_pattern_for_header(header)))

        if number_cols:
            header_rows.append((row_idx, number_cols))

    if not header_rows:
        return

    requests = []

    for idx, (header_row_idx, number_cols) in enumerate(header_rows):
        if idx + 1 < len(header_rows):
            end_row = header_rows[idx + 1][0] - 1
        else:
            end_row = ws_xlsx.max_row

        start_data_row = header_row_idx + 1

        if start_data_row > end_row:
            continue

        for col_idx, pattern in number_cols:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_data_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_idx - 1,
                        "endColumnIndex": col_idx,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "NUMBER",
                                "pattern": pattern,
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            })

    _gsheet_batch_update(requests)


GSHEET_DATE_HEADER_KEYWORDS = (
    "日期",
    "買進日",
    "賣出日",
    "事件日",
    "起始日",
    "結束日",
    "減碼日",
    "出清日",
    "最近買進日",
    "第一筆日期",
    "最後筆日期",
)


def is_gsheet_date_header(header):
    """
    判斷 Google Sheet 中哪些欄位應該以日期格式顯示。

    主要修正「券商查詢」工作表的「最近買進日」：
    Google Sheet 公式 INDEX/MATCH 從資料表抓到日期時，常會以日期序號顯示，
    例如 46160。這裡統一把日期欄位套成 yyyy/mm/dd，讓畫面顯示正常日期。
    """
    header = str(header).strip()

    if not header:
        return False

    if "天數" in header:
        return False

    return any(keyword in header for keyword in GSHEET_DATE_HEADER_KEYWORDS)


def apply_date_format_to_gsheet(ws_xlsx, gws):
    """
    將日期相關欄位套用 Google Sheets 日期格式 yyyy/mm/dd。

    這裡只修正 Google Sheet 顯示格式，不改原本 Excel 產生邏輯，
    也不改快取內容。尤其可避免「最近買進日」顯示成 46160 這類日期序號。
    """
    if gws is None:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    header_rows = []
    scan_limit = min(ws_xlsx.max_row, 10)

    for row_idx in range(1, scan_limit + 1):
        date_cols = []

        for col_idx in range(1, ws_xlsx.max_column + 1):
            header = ws_xlsx.cell(row_idx, col_idx).value

            if is_gsheet_date_header(header):
                date_cols.append(col_idx)

        if date_cols:
            header_rows.append((row_idx, date_cols))

    if not header_rows:
        return

    requests = []

    for idx, (header_row_idx, date_cols) in enumerate(header_rows):
        if idx + 1 < len(header_rows):
            end_row = header_rows[idx + 1][0] - 1
        else:
            end_row = ws_xlsx.max_row

        start_data_row = header_row_idx + 1

        if start_data_row > end_row:
            continue

        for col_idx in date_cols:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_data_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_idx - 1,
                        "endColumnIndex": col_idx,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "DATE",
                                "pattern": "yyyy/mm/dd",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            })

    _gsheet_batch_update(requests)


def write_values_to_worksheet(ws, values):
    if ws is None:
        return False

    if not values:
        values = [[""]]

    row_count = max(len(values), 1)
    col_count = max(max((len(row) for row in values), default=1), 1)

    normalized_values = []
    for row in values:
        row = list(row)
        if len(row) < col_count:
            row = row + [""] * (col_count - len(row))
        normalized_values.append([clean_gsheet_value(v) for v in row])

    normalized_values = normalize_gsheet_values_for_text_columns(normalized_values)

    try:
        ws.clear()
        ws.resize(rows=max(row_count, 1), cols=max(col_count, 1))

        for start in range(0, len(normalized_values), GSHEET_CHUNK_ROWS):
            chunk = normalized_values[start:start + GSHEET_CHUNK_ROWS]
            start_row = start + 1
            cell_range = f"A{start_row}"
            ws.update(values=chunk, range_name=cell_range, value_input_option="USER_ENTERED")

        apply_text_format_to_gsheet(ws, normalized_values)

        return True
    except Exception as e:
        print(f"  ⚠️ Google Sheet 寫入失敗：{ws.title}，原因：{type(e).__name__}: {e}")
        return False


def read_cache_from_gsheet(path):
    if not GSHEET_CACHE_ENABLED or not gsheet_enabled():
        return pd.DataFrame()

    title = cache_sheet_name_from_path(path)

    try:
        sh = get_gsheet_spreadsheet()
        if sh is None:
            return pd.DataFrame()

        ws = sh.worksheet(title)
        values = ws.get_all_values()

        if not values or len(values) < 2:
            return pd.DataFrame()

        headers = [str(h).strip() for h in values[0]]
        rows = values[1:]

        if not headers or all(h == "" for h in headers):
            return pd.DataFrame()

        fixed_rows = []
        n_cols = len(headers)
        for row in rows:
            row = list(row)
            if len(row) < n_cols:
                row = row + [""] * (n_cols - len(row))
            elif len(row) > n_cols:
                row = row[:n_cols]
            fixed_rows.append(row)

        df = pd.DataFrame(fixed_rows, columns=headers).fillna("")

        for col in df.columns:
            if is_gsheet_text_header(col):
                df[col] = df[col].map(strip_gsheet_text_prefix)

        print(f"  ☁️ 已從 Google Sheet 讀取快取：{title}，共 {len(df):,} 筆")
        return df
    except Exception:
        return pd.DataFrame()


def write_cache_to_gsheet(df, path):
    if not GSHEET_CACHE_ENABLED or not gsheet_enabled():
        return

    if df is None:
        return

    try:
        title = cache_sheet_name_from_path(path)
        df2 = df.copy().fillna("")
        values = [list(df2.columns)] + df2.astype(str).values.tolist()
        ws = get_or_create_worksheet(title, rows=max(len(values), 100), cols=max(len(df2.columns), 20))

        if write_values_to_worksheet(ws, values):
            print(f"  ☁️ 已同步快取到 Google Sheet：{title}，共 {len(df2):,} 筆")
    except Exception as e:
        print(f"  ⚠️ 快取同步到 Google Sheet 失敗：{path}，原因：{type(e).__name__}: {e}")



def _hex_to_gsheet_color(hex_value):
    if not hex_value:
        return None

    s = str(hex_value).strip()

    if not s or s in ("00000000", "FFFFFFFF"):
        # 00000000 在 openpyxl 常代表無填色，不是真的黑色背景。
        return None

    if len(s) == 8:
        s = s[-6:]

    if len(s) != 6:
        return None

    try:
        return {
            "red": int(s[0:2], 16) / 255,
            "green": int(s[2:4], 16) / 255,
            "blue": int(s[4:6], 16) / 255,
        }
    except Exception:
        return None


def _openpyxl_color_to_gsheet(color_obj):
    if color_obj is None:
        return None

    try:
        if color_obj.type == "rgb" and color_obj.rgb:
            return _hex_to_gsheet_color(color_obj.rgb)
    except Exception:
        pass

    return None


def _openpyxl_fill_to_gsheet(cell):
    try:
        fill = cell.fill
        if fill is None or fill.fill_type is None:
            return None

        color = _openpyxl_color_to_gsheet(fill.fgColor)
        return color
    except Exception:
        return None


def _openpyxl_font_to_gsheet(cell):
    text_format = {}

    try:
        font = cell.font

        if font is None:
            return text_format

        if font.bold is not None:
            text_format["bold"] = bool(font.bold)

        if font.italic is not None:
            text_format["italic"] = bool(font.italic)

        if font.sz:
            text_format["fontSize"] = int(float(font.sz))

        color = _openpyxl_color_to_gsheet(font.color)
        if color:
            text_format["foregroundColor"] = color
    except Exception:
        pass

    return text_format


def _openpyxl_alignment_to_gsheet(cell):
    fmt = {}

    try:
        alignment = cell.alignment

        horizontal_map = {
            "center": "CENTER",
            "left": "LEFT",
            "right": "RIGHT",
        }
        vertical_map = {
            "center": "MIDDLE",
            "top": "TOP",
            "bottom": "BOTTOM",
        }

        if alignment.horizontal in horizontal_map:
            fmt["horizontalAlignment"] = horizontal_map[alignment.horizontal]

        if alignment.vertical in vertical_map:
            fmt["verticalAlignment"] = vertical_map[alignment.vertical]

        if alignment.wrap_text:
            fmt["wrapStrategy"] = "WRAP"
    except Exception:
        pass

    return fmt


def _openpyxl_number_format_to_gsheet(cell):
    try:
        nf = str(cell.number_format or "").strip()

        if not nf or nf == "General":
            return None

        if "%" in nf:
            return {"type": "PERCENT", "pattern": nf}

        if "#" in nf or "0" in nf:
            return {"type": "NUMBER", "pattern": nf}
    except Exception:
        pass

    return None


def _cell_gsheet_format(cell):
    fmt = {}

    bg = _openpyxl_fill_to_gsheet(cell)
    if bg:
        fmt["backgroundColor"] = bg

    text_format = _openpyxl_font_to_gsheet(cell)
    if text_format:
        fmt["textFormat"] = text_format

    align_format = _openpyxl_alignment_to_gsheet(cell)
    fmt.update(align_format)

    number_format = _openpyxl_number_format_to_gsheet(cell)
    if number_format:
        fmt["numberFormat"] = number_format

    return fmt


def _format_fields(fmt):
    fields = []

    if "backgroundColor" in fmt:
        fields.append("userEnteredFormat.backgroundColor")
    if "textFormat" in fmt:
        for key in fmt["textFormat"].keys():
            fields.append(f"userEnteredFormat.textFormat.{key}")
    if "horizontalAlignment" in fmt:
        fields.append("userEnteredFormat.horizontalAlignment")
    if "verticalAlignment" in fmt:
        fields.append("userEnteredFormat.verticalAlignment")
    if "wrapStrategy" in fmt:
        fields.append("userEnteredFormat.wrapStrategy")
    if "numberFormat" in fmt:
        fields.append("userEnteredFormat.numberFormat")

    return ",".join(fields)


def _gsheet_batch_update(requests, chunk_size=400):
    if not requests:
        return

    sh = get_gsheet_spreadsheet()

    if sh is None:
        return

    for start in range(0, len(requests), chunk_size):
        chunk = requests[start:start + chunk_size]
        try:
            sh.batch_update({"requests": chunk})
        except Exception as e:
            print(f"  ⚠️ Google Sheet 格式套用失敗：{type(e).__name__}: {e}")
            return


def _openpyxl_freeze_to_grid_properties(ws_xlsx):
    frozen_rows = 0
    frozen_cols = 0

    try:
        pane = ws_xlsx.freeze_panes

        if pane:
            from openpyxl.utils.cell import coordinate_to_tuple
            row, col = coordinate_to_tuple(str(pane))
            frozen_rows = max(row - 1, 0)
            frozen_cols = max(col - 1, 0)
    except Exception:
        pass

    return frozen_rows, frozen_cols


def _excel_width_to_pixels(width):
    try:
        return max(20, int(float(width) * 7 + 5))
    except Exception:
        return None


def _excel_height_to_pixels(height):
    try:
        return max(18, int(float(height) * 1.333))
    except Exception:
        return None


def normalize_formula_for_gsheet(value):
    if not isinstance(value, str):
        return value

    if not value.startswith("="):
        return value

    # Google Sheets 對中文工作表名稱建議加單引號，避免公式解析失敗。
    value = value.replace("INDEX(券商查詢資料!", "INDEX('券商查詢資料'!")
    value = value.replace(",券商查詢資料!", ",'券商查詢資料'!")
    value = value.replace("MATCH($B$2&\"|\"&$A", "MATCH($B$2&\"|\"&$A")
    return value


def apply_excel_style_to_gsheet(ws_xlsx, gws):
    """
    將 openpyxl 產生的 Excel 樣式轉成 Google Sheets 格式。

    會同步：
    1. 背景色、字體粗細 / 字色 / 字級
    2. 文字置中、換行、數字格式
    3. 欄寬、列高、凍結列 / 欄
    4. 合併儲存格
    5. 隱藏工作表
    6. 券商查詢 B2 下拉選單

    Google Sheets API 與 Excel 格式模型不同，因此外框只保留主要視覺效果，
    但 A/B/C/D 的紅綠藍橘狀態色、標頭色與查詢頁互動功能會完整保留。
    """
    if gws is None:
        return

    sheet_id = int(gws.id)
    requests = []

    frozen_rows, frozen_cols = _openpyxl_freeze_to_grid_properties(ws_xlsx)

    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "hidden": bool(ws_xlsx.sheet_state == "hidden"),
                "gridProperties": {
                    "frozenRowCount": frozen_rows,
                    "frozenColumnCount": frozen_cols,
                },
            },
            "fields": "hidden,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })

    # 先清除舊合併範圍，避免重複執行時 mergeCells 失敗。
    requests.append({
        "unmergeCells": {
            "range": {
                "sheetId": sheet_id,
            }
        }
    })

    # 合併儲存格。
    for merged_range in ws_xlsx.merged_cells.ranges:
        try:
            requests.append({
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": merged_range.min_row - 1,
                        "endRowIndex": merged_range.max_row,
                        "startColumnIndex": merged_range.min_col - 1,
                        "endColumnIndex": merged_range.max_col,
                    },
                    "mergeType": "MERGE_ALL",
                }
            })
        except Exception:
            pass

    # 欄寬。
    for col_idx in range(1, ws_xlsx.max_column + 1):
        letter = get_column_letter(col_idx)
        width = ws_xlsx.column_dimensions[letter].width
        pixel_size = _excel_width_to_pixels(width) if width else None

        if pixel_size:
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx - 1,
                        "endIndex": col_idx,
                    },
                    "properties": {
                        "pixelSize": pixel_size,
                    },
                    "fields": "pixelSize",
                }
            })

    # 列高。
    for row_idx in range(1, ws_xlsx.max_row + 1):
        height = ws_xlsx.row_dimensions[row_idx].height
        pixel_size = _excel_height_to_pixels(height) if height else None

        if pixel_size:
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_idx - 1,
                        "endIndex": row_idx,
                    },
                    "properties": {
                        "pixelSize": pixel_size,
                    },
                    "fields": "pixelSize",
                }
            })

    # 逐列壓縮相同格式的連續儲存格，減少 batchUpdate 請求數量。
    import json as _json

    for row_idx in range(1, ws_xlsx.max_row + 1):
        run_start = None
        run_fmt = None
        run_key = None

        for col_idx in range(1, ws_xlsx.max_column + 2):
            if col_idx <= ws_xlsx.max_column:
                cell = ws_xlsx.cell(row_idx, col_idx)
                fmt = _cell_gsheet_format(cell)
                key = _json.dumps(fmt, sort_keys=True, ensure_ascii=False) if fmt else None
            else:
                fmt = None
                key = None

            if key and run_key is None:
                run_start = col_idx
                run_fmt = fmt
                run_key = key
            elif key and key == run_key:
                continue
            else:
                if run_key and run_start is not None and run_fmt:
                    fields = _format_fields(run_fmt)
                    if fields:
                        requests.append({
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": row_idx - 1,
                                    "endRowIndex": row_idx,
                                    "startColumnIndex": run_start - 1,
                                    "endColumnIndex": col_idx - 1,
                                },
                                "cell": {
                                    "userEnteredFormat": run_fmt,
                                },
                                "fields": fields,
                            }
                        })

                if key:
                    run_start = col_idx
                    run_fmt = fmt
                    run_key = key
                else:
                    run_start = None
                    run_fmt = None
                    run_key = None

    # Google Sheets 版券商查詢：B2 下拉選單。
    if ws_xlsx.title == "券商查詢":
        try:
            # 使用 ONE_OF_LIST 直接寫入券商清單，比 ONE_OF_RANGE 更穩定，
            # 可避免部分環境下跨工作表範圍驗證未顯示下拉箭頭。
            sh = get_gsheet_spreadsheet()
            data_ws = sh.worksheet("券商查詢資料") if sh else None
            broker_values = []

            if data_ws:
                for value in data_ws.col_values(16)[1:]:
                    value = str(value).strip()
                    if value and value not in broker_values:
                        broker_values.append(value)

            if broker_values:
                requests.append({
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 2,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": broker}
                                    for broker in broker_values
                                ],
                            },
                            "showCustomUi": True,
                            "strict": True,
                        },
                    }
                })
        except Exception as e:
            print(f"  ⚠️ 券商查詢下拉選單建立失敗：{type(e).__name__}: {e}")

    _gsheet_batch_update(requests)


def upload_excel_to_google_sheet(xlsx_path):
    if not GSHEET_RESULT_ENABLED or not gsheet_enabled():
        print("  ⚠️ 未設定 GCP_SERVICE_KEY，略過 Google Sheet 結果同步")
        return

    try:
        from openpyxl import load_workbook

        wb = load_workbook(xlsx_path, data_only=False)

        for ws_xlsx in wb.worksheets:
            title = safe_worksheet_title(ws_xlsx.title)
            values = []

            for row in ws_xlsx.iter_rows(values_only=False):
                row_values = []
                for cell in row:
                    value = cell.value
                    value = normalize_formula_for_gsheet(value)
                    row_values.append(clean_gsheet_value(value))
                values.append(row_values)

            if not values:
                values = [[""]]

            values = normalize_result_values_for_comma_numbers(values)

            max_cols = max(max((len(row) for row in values), default=1), 1)
            gws = get_or_recreate_result_worksheet(title, rows=max(len(values), 100), cols=max(max_cols, 20))

            if write_values_to_worksheet(gws, values):
                apply_excel_style_to_gsheet(ws_xlsx, gws)
                apply_comma_number_format_to_gsheet(ws_xlsx, gws)
                apply_date_format_to_gsheet(ws_xlsx, gws)
                print(f"  ☁️ 已同步結果到 Google Sheet：{title}")

        try:
            sh = get_gsheet_spreadsheet()
            if sh is not None:
                tmp_ws = sh.worksheet("__tmp_delete_guard__")
                if len(sh.worksheets()) > 1:
                    sh.del_worksheet(tmp_ws)
        except Exception:
            pass

    except Exception as e:
        print(f"  ⚠️ Excel 同步 Google Sheet 失敗：{type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════
# 快取工具：避免每次重爬舊資料
# ══════════════════════════════════════════════════════════════════════

def cache_enabled():
    return USE_CACHE and not FORCE_FULL_CACHE_REFRESH


def read_cache_csv(path):
    if not cache_enabled():
        return pd.DataFrame()

    # 本機執行時：優先讀本機快取，並順手上傳到 Google Sheet，方便先把本機快取種到雲端。
    # GitHub Actions 執行時：優先讀 Google Sheet 快取，因為 runner 通常是乾淨環境。
    local_first = os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true"

    if local_first and os.path.exists(path):
        try:
            df = pd.read_csv(path, dtype=str, encoding=CACHE_ENCODING).fillna("")
            write_cache_to_gsheet(df, path)
            return df
        except Exception as e:
            print(f"  ⚠️ 本機快取讀取失敗：{path}，原因：{e}")

    df_from_gsheet = read_cache_from_gsheet(path)

    if df_from_gsheet is not None and not df_from_gsheet.empty:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            df_from_gsheet.to_csv(path, index=False, encoding=CACHE_ENCODING)
        except Exception:
            pass
        return df_from_gsheet

    if not os.path.exists(path):
        return pd.DataFrame()

    try:
        df = pd.read_csv(path, dtype=str, encoding=CACHE_ENCODING).fillna("")
        write_cache_to_gsheet(df, path)
        return df
    except Exception as e:
        print(f"  ⚠️ 快取讀取失敗：{path}，原因：{e}")
        return pd.DataFrame()


def write_cache_csv(df, path):
    if not USE_CACHE:
        return

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_csv(path, index=False, encoding=CACHE_ENCODING)
    except Exception as e:
        print(f"  ⚠️ 快取寫入失敗：{path}，原因：{e}")

    write_cache_to_gsheet(df, path)


def load_price_cache():
    """
    讀取價格持久化快取。

    快取欄位：
    1. 代號
    2. 日期
    3. 收盤價

    代號會統一用 normalize_price_code() 正規化，避免同一檔股票 / 權證
    因為補零或去零產生重複抓取。
    """
    df = read_cache_csv(PRICE_CACHE_PATH)

    if df.empty:
        return {}

    required_cols = ["代號", "日期", "收盤價"]
    for col in required_cols:
        if col not in df.columns:
            print(f"  ⚠️ 價格快取欄位不完整，缺少：{col}")
            return {}

    price_cache = {}

    for _, row in df.iterrows():
        code = normalize_price_code(row.get("代號", ""))

        if not code:
            continue

        date_str = normalize_date_str(row.get("日期", ""))
        dt = parse_date(date_str)

        if not dt:
            continue

        price = safe_price_float(row.get("收盤價", ""))

        if price is None:
            continue

        price_cache.setdefault(code, {})[dt.strftime("%Y/%m/%d")] = price

    return price_cache


def save_price_cache(price_cache):
    """
    寫入價格持久化快取。

    寫入前會再做一次代號正規化，避免 064390 / 64390 之類別名重複存檔。
    """
    if not USE_CACHE or not price_cache:
        return

    canonical = {}

    for code, prices in price_cache.items():
        norm_code = normalize_price_code(code)

        if not norm_code or not prices:
            continue

        for date_str, price in prices.items():
            dt = parse_date(date_str)
            price = safe_price_float(price)

            if not dt or price is None:
                continue

            canonical.setdefault(norm_code, {})[dt.strftime("%Y/%m/%d")] = price

    rows = []

    for code in sorted(canonical.keys()):
        for date_str in sorted(canonical[code].keys()):
            rows.append({
                "代號": code,
                "日期": date_str,
                "收盤價": canonical[code][date_str],
            })

    if not rows:
        return

    df = pd.DataFrame(rows, columns=["代號", "日期", "收盤價"])
    write_cache_csv(df, PRICE_CACHE_PATH)
    print(f"  💾 已更新價格快取：{PRICE_CACHE_PATH}，共 {len(df):,} 筆")


def get_cached_prices_for_code(price_cache, code):
    """
    從價格快取中取出指定代號的價格。

    同時支援補零版與去零版查找，最後回傳單一合併後 dict。
    """
    out = {}
    norm_code = normalize_price_code(code)

    if not norm_code:
        return out

    lookup_codes = []
    for c in price_code_variants(norm_code):
        if c and c not in lookup_codes:
            lookup_codes.append(c)

        no_zero = c.lstrip("0")
        if no_zero and no_zero not in lookup_codes:
            lookup_codes.append(no_zero)

        norm_c = normalize_price_code(c)
        if norm_c and norm_c not in lookup_codes:
            lookup_codes.append(norm_c)

    for c in lookup_codes:
        cached = price_cache.get(c)

        if not cached:
            continue

        out = merge_price_dicts(out, cached)

    return out


def add_price_aliases(price_cache, code, prices):
    """
    在記憶體 price_cache 中建立補零 / 去零別名，
    避免 Excel 或 pandas 吃掉前導 0 時查不到。
    """
    if not prices:
        prices = {}

    norm_code = normalize_price_code(code)

    if not norm_code:
        return

    price_cache[norm_code] = prices

    no_zero = norm_code.lstrip("0")

    if no_zero:
        price_cache[no_zero] = prices

    raw_code = str(code).strip()

    if raw_code:
        price_cache[raw_code] = prices



def candidate_key_from_tuple(c):
    return (str(c[0]).strip(), str(c[6]).strip())


def candidate_key_from_values(warrant_code, broker_code):
    return (str(warrant_code).strip(), str(broker_code).strip())


def save_warrants_cache(warrants):
    if not USE_CACHE or not warrants:
        return

    df = pd.DataFrame(warrants)

    wanted_cols = ["代號", "名稱", "標的股", "標的名稱"]
    for col in wanted_cols:
        if col not in df.columns:
            df[col] = ""

    write_cache_csv(df[wanted_cols], WARRANTS_CACHE_PATH)
    print(f"  💾 已更新權證清單快取：{WARRANTS_CACHE_PATH}")


def load_warrants_cache():
    df = read_cache_csv(WARRANTS_CACHE_PATH)

    if df.empty:
        return []

    required_cols = ["代號", "名稱", "標的股", "標的名稱"]
    for col in required_cols:
        if col not in df.columns:
            return []

    warrants = []
    for _, row in df.iterrows():
        code = str(row["代號"]).strip()
        name = str(row["名稱"]).strip()

        if not code or not name:
            continue

        warrants.append({
            "代號": code,
            "名稱": name,
            "標的股": str(row.get("標的股", "")).strip(),
            "標的名稱": str(row.get("標的名稱", "")).strip(),
        })

    return warrants


def save_broker_map_cache(broker_map):
    if not USE_CACHE or not broker_map:
        return

    rows = []
    for label, (name, code) in broker_map.items():
        rows.append({
            "分點": label,
            "分點名稱": name,
            "券商代號": code,
        })

    df = pd.DataFrame(rows)
    write_cache_csv(df, BROKER_MAP_CACHE_PATH)
    print(f"  💾 已更新分點代號快取：{BROKER_MAP_CACHE_PATH}")


def load_broker_map_cache():
    df = read_cache_csv(BROKER_MAP_CACHE_PATH)

    if df.empty:
        return {}

    required_cols = ["分點", "分點名稱", "券商代號"]
    for col in required_cols:
        if col not in df.columns:
            return {}

    broker_map = {}

    for _, row in df.iterrows():
        label = str(row["分點"]).strip()
        name = str(row["分點名稱"]).strip()
        code = str(row["券商代號"]).strip()

        if label and name and code:
            broker_map[label] = (name, code)

    missing = [k for k in TARGET_PATTERNS if k not in broker_map]
    if missing:
        print(f"  ⚠️ 分點代號快取不完整，缺少：{missing}")
        return {}

    return broker_map


def save_candidates_cache(candidates):
    if not USE_CACHE or not candidates:
        return

    rows = []

    for c in candidates:
        rows.append({
            "權證代號": c[0],
            "權證名稱": c[1],
            "標的股": c[2],
            "標的名稱": c[3],
            "分點": c[4],
            "分點名稱": c[5],
            "券商代號": c[6],
        })

    df = pd.DataFrame(rows)
    write_cache_csv(df, CANDIDATES_CACHE_PATH)
    print(f"  💾 已更新候選組合快取：{CANDIDATES_CACHE_PATH}")


def load_candidates_cache():
    df = read_cache_csv(CANDIDATES_CACHE_PATH)

    if df.empty:
        return []

    required_cols = ["權證代號", "權證名稱", "標的股", "標的名稱", "分點", "分點名稱", "券商代號"]
    for col in required_cols:
        if col not in df.columns:
            return []

    candidates = []

    for _, row in df.iterrows():
        warrant_code = str(row["權證代號"]).strip()
        broker_code = str(row["券商代號"]).strip()

        if not warrant_code or not broker_code:
            continue

        candidates.append((
            warrant_code,
            str(row["權證名稱"]).strip(),
            str(row["標的股"]).strip(),
            str(row["標的名稱"]).strip(),
            str(row["分點"]).strip(),
            str(row["分點名稱"]).strip(),
            broker_code,
        ))

    return candidates


def merge_candidates(old_candidates, new_candidates):
    merged = {}

    for c in old_candidates:
        merged[candidate_key_from_tuple(c)] = c

    for c in new_candidates:
        merged[candidate_key_from_tuple(c)] = c

    return list(merged.values())


def load_history_cache():
    df = read_cache_csv(HISTORY_CACHE_PATH)

    if df.empty:
        return pd.DataFrame()

    required_cols = [
        "權證代號", "權證名稱", "標的股", "標的名稱",
        "分點", "分點名稱", "券商代號", "日期",
        "買進股數", "賣出股數", "買進金額", "賣出金額",
        "買超股數", "買超金額",
    ]

    for col in required_cols:
        if col not in df.columns:
            print(f"  ⚠️ 原始分點資料快取欄位不完整，缺少：{col}")
            return pd.DataFrame()

    return df[required_cols].copy()


def history_cache_keys(history_df):
    keys = set()

    if history_df is None or history_df.empty:
        return keys

    for _, row in history_df[["權證代號", "券商代號"]].drop_duplicates().iterrows():
        keys.add(candidate_key_from_values(row["權證代號"], row["券商代號"]))

    return keys


def item_to_history_rows(item):
    rows = []
    df = item["df"]

    for _, row in df.iterrows():
        rows.append({
            "權證代號": item["warrant_code"],
            "權證名稱": item["warrant_name"],
            "標的股": item["underlying_code"],
            "標的名稱": item.get("underlying_name", ""),
            "分點": item["broker_label"],
            "分點名稱": item["broker_name"],
            "券商代號": item["broker_code"],
            "日期": normalize_date_str(row["日期"]),
            "買進股數": int(row["買進股數"]),
            "賣出股數": int(row["賣出股數"]),
            "買進金額": int(row["買進金額"]),
            "賣出金額": int(row["賣出金額"]),
            "買超股數": int(row["買超股數"]),
            "買超金額": int(row["買超金額"]),
        })

    return rows


def merge_items_into_history_cache(history_df, new_items):
    if not new_items:
        return history_df if history_df is not None else pd.DataFrame()

    new_rows = []
    new_keys = set()

    for item in new_items:
        new_keys.add(candidate_key_from_values(item["warrant_code"], item["broker_code"]))
        new_rows.extend(item_to_history_rows(item))

    new_df = pd.DataFrame(new_rows)

    if new_df.empty:
        return history_df if history_df is not None else pd.DataFrame()

    if history_df is None or history_df.empty:
        combined = new_df
    else:
        history_df = history_df.copy()
        remove_mask = pd.Series(
            [candidate_key_from_values(w, b) in new_keys for w, b in zip(history_df["權證代號"], history_df["券商代號"])],
            index=history_df.index
        )
        old_keep_df = history_df[~remove_mask].copy()
        combined = pd.concat([old_keep_df, new_df], ignore_index=True)

    numeric_cols = ["買進股數", "賣出股數", "買進金額", "賣出金額", "買超股數", "買超金額"]
    for col in numeric_cols:
        combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0).astype(int)

    combined["日期"] = combined["日期"].map(normalize_date_str)

    combined = combined.drop_duplicates(
        subset=["權證代號", "券商代號", "日期"],
        keep="last"
    )

    combined = combined.sort_values(
        ["權證代號", "券商代號", "日期"]
    ).reset_index(drop=True)

    return combined


def save_history_cache(history_df):
    if not USE_CACHE or history_df is None or history_df.empty:
        return

    write_cache_csv(history_df, HISTORY_CACHE_PATH)
    print(f"  💾 已更新原始分點資料快取：{HISTORY_CACHE_PATH}")


def items_from_history_cache(history_df, candidate_filter=None):
    items = []

    if history_df is None or history_df.empty:
        return items

    df = history_df.copy().fillna("")

    if candidate_filter:
        mask = pd.Series(
            [candidate_key_from_values(w, b) in candidate_filter for w, b in zip(df["權證代號"], df["券商代號"])],
            index=df.index
        )
        df = df[mask].copy()

    if df.empty:
        return items

    numeric_cols = ["買進股數", "賣出股數", "買進金額", "賣出金額", "買超股數", "買超金額"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    group_cols = ["權證代號", "權證名稱", "標的股", "標的名稱", "分點", "分點名稱", "券商代號"]

    for key, g in df.groupby(group_cols, dropna=False):
        warrant_code, warrant_name, underlying_code, underlying_name, broker_label, broker_name, broker_code = key

        item_df = g[[
            "日期", "買進股數", "賣出股數",
            "買進金額", "賣出金額", "買超股數", "買超金額"
        ]].copy()

        item_df["日期"] = item_df["日期"].map(normalize_date_str)
        item_df = item_df.sort_values("日期").reset_index(drop=True)

        item = {
            "warrant_code": str(warrant_code).strip(),
            "warrant_name": str(warrant_name).strip(),
            "underlying_code": str(underlying_code).strip(),
            "underlying_name": str(underlying_name).strip(),
            "broker_label": str(broker_label).strip(),
            "broker_name": str(broker_name).strip(),
            "broker_code": str(broker_code).strip(),
            "df": item_df,
        }

        item["events_a"] = build_a_events_from_df(item)
        items.append(item)

    return items



# ══════════════════════════════════════════════════════════════════════
# Step 1：取所有認購權證 + 標的股代號
# ══════════════════════════════════════════════════════════════════════

def build_stock_map(df):
    stock_map = {}

    for _, row in df.iterrows():
        cell = str(row.iloc[0]).strip()

        if "　" in cell:
            parts = cell.split("　", 1)
            code, name = parts[0].strip(), parts[1].strip()
        else:
            m = re.match(r"^(\d{4})\s+(.+)$", cell)
            if m:
                code, name = m.group(1), m.group(2)
            else:
                continue

        if len(code) == 4 and code.isdigit():
            stock_map[name] = code

    return stock_map


def make_stock_aliases(stock_name):
    name = str(stock_name).strip()
    aliases = set()

    if not name:
        return aliases

    aliases.add(name)

    suffixes = [
        "半導體", "科技", "電子", "光電", "精密", "材料", "生技", "醫療",
        "資訊", "電腦", "通信", "通訊", "電機", "機械", "工業", "實業",
        "企業", "國際", "控股", "投控", "控", "建設", "營造", "食品",
        "鋼鐵", "化學", "化工", "紡織", "玻璃", "塑膠", "水泥",
    ]

    stripped = name
    changed = True

    while changed:
        changed = False
        for suffix in suffixes:
            if stripped.endswith(suffix) and len(stripped) > len(suffix) + 1:
                stripped = stripped[:-len(suffix)]
                aliases.add(stripped)
                changed = True
                break

    for n in range(min(4, len(name)), 1, -1):
        aliases.add(name[:n])

    return {a for a in aliases if len(a) >= 2}


def find_underlying_info(warrant_name, stock_map):
    wname = str(warrant_name).strip().replace(" ", "").replace("　", "")

    # 第一層：完整股名直接比對，最安全。
    for stock_name in sorted(stock_map.keys(), key=len, reverse=True):
        sname = str(stock_name).strip().replace(" ", "").replace("　", "")

        if sname and wname.startswith(sname):
            return stock_map[stock_name], stock_name

    # 第二層：權證名稱常會使用簡稱，例如「雍智國票59購01」對應「雍智科技」。
    # 因此用常見股名簡稱補判斷，但仍採最長別名優先，降低誤判。
    candidates = []

    for stock_name, stock_code in stock_map.items():
        for alias in make_stock_aliases(stock_name):
            alias_norm = alias.replace(" ", "").replace("　", "")

            if alias_norm and wname.startswith(alias_norm):
                candidates.append((len(alias_norm), len(str(stock_name)), stock_code, stock_name, alias_norm))

    if candidates:
        candidates = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)
        _, _, stock_code, stock_name, _ = candidates[0]
        return stock_code, stock_name

    return "", ""


def find_underlying(warrant_name, stock_map):
    code, _ = find_underlying_info(warrant_name, stock_map)
    return code


def get_all_call_warrants_live():
    print("【Step 1】取所有認購權證清單...")
    warrants = []

    # strMode=2：上市有價證券
    # strMode=4：上櫃有價證券
    # 兩邊都要抓，否則上櫃標的的權證，例如 70xxxx 權證會漏掉。
    isin_modes = [
        ("上市", "2"),
        ("上櫃", "4"),
    ]

    all_dfs = []
    stock_map = {}

    for market_name, mode in isin_modes:
        try:
            resp = requests.get(
                f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30
            )
            resp.raise_for_status()
            resp.encoding = "cp950"

            tables = pd.read_html(StringIO(resp.text))
            df = tables[0].iloc[2:].reset_index(drop=True)

            all_dfs.append((market_name, df))
            stock_map.update(build_stock_map(df))

            print(f"  ✅ 已取得{market_name} ISIN 清單：{len(df)} 筆")

        except Exception as e:
            print(f"  ⚠️ {market_name} ISIN 清單取得失敗：{e}")

    seen_warrants = set()

    for market_name, df in all_dfs:
        for _, row in df.iterrows():
            cell = str(row.iloc[0]).strip()

            if "\u3000" in cell:
                parts = cell.split("\u3000", 1)
                code, name = parts[0].strip(), parts[1].strip()
            else:
                m = re.match(r"^(\d{6})\s+(.+)$", cell)
                if m:
                    code, name = m.group(1), m.group(2)
                else:
                    continue

            if len(code) == 6 and code.isdigit() and "購" in name:
                if code in seen_warrants:
                    continue

                seen_warrants.add(code)
                underlying, underlying_name = find_underlying_info(name, stock_map)

                warrants.append({
                    "代號": code,
                    "名稱": name,
                    "標的股": underlying,
                    "標的名稱": underlying_name
                })

    print(f"  ✅ 共 {len(warrants)} 支認購權證")
    return warrants




def get_all_call_warrants():
    cached_warrants = load_warrants_cache()

    if cached_warrants:
        print("【Step 1】讀取認購權證清單快取...")
        print(f"  ✅ 已讀取權證清單快取：{len(cached_warrants)} 支")
        return cached_warrants

    warrants = get_all_call_warrants_live()
    save_warrants_cache(warrants)
    return warrants


# ══════════════════════════════════════════════════════════════════════
# Step 2：找目標分點券商代號
# ══════════════════════════════════════════════════════════════════════

def find_broker_codes_live(warrants):
    print("【Step 2】掃描目標分點券商代號...")

    today = datetime.today()
    end_s   = today.strftime("%Y/%m/%d")
    start_s = (today - timedelta(days=3)).strftime("%Y/%m/%d")
    found = {}

    def scan_one(code):
        hits = {}
        for row in api4_get(code, start_s, end_s):
            name = row.get("V3", "")
            label = match_target(name)
            if label and label not in found:
                hits[label] = (name, row.get("V2", ""))
        return hits

    with ThreadPoolExecutor(max_workers=FIND_BROKER_WORKERS) as ex:
        futures = {ex.submit(scan_one, w["代號"]): w for w in warrants[:300]}

        for future in as_completed(futures):
            try:
                result = future.result()
            except:
                result = {}

            for label, (name, code) in result.items():
                if label not in found:
                    found[label] = (name, code)

            if len(found) == len(TARGET_PATTERNS):
                for pending_future in futures:
                    if not pending_future.done():
                        pending_future.cancel()
                break

    for label, (name, code) in found.items():
        print(f"    ✅ {label:14s} → {name} ({code})")

    missing = [k for k in TARGET_PATTERNS if k not in found]

    if missing:
        print(f"    備援補入：{missing}")
        for k in missing:
            if k in FALLBACK:
                found[k] = FALLBACK[k]
                print(f"    ✅ {k:14s} → {FALLBACK[k][0]} ({FALLBACK[k][1]}) [備援]")

    return found




def find_broker_codes(warrants):
    broker_map = load_broker_map_cache()

    if broker_map:
        print("【Step 2】讀取目標分點券商代號快取...")
        for label, (name, code) in broker_map.items():
            print(f"    ✅ {label:14s} → {name} ({code}) [快取]")
        return broker_map

    broker_map = find_broker_codes_live(warrants)
    save_broker_map_cache(broker_map)
    return broker_map


# ══════════════════════════════════════════════════════════════════════
# Step 3a：預篩有目標分點出現的 (權證, 分點) 組合
# ══════════════════════════════════════════════════════════════════════

def prescan_all_live(warrants, broker_map, scan_days=40):
    print("【Step 3a】預篩：找有目標分點的權證...")

    today = datetime.today()
    end_s   = today.strftime("%Y/%m/%d")
    start_s = (today - timedelta(days=scan_days)).strftime("%Y/%m/%d")

    broker_codes_set = {code for _, code in broker_map.values()}
    code_to_label    = {code: label for label, (_, code) in broker_map.items()}

    candidates = []
    done = 0

    def prescan_one(w):
        hits = []

        for row in api4_get(w["代號"], start_s, end_s):
            bcode = row.get("V2", "")

            if bcode in broker_codes_set:
                label = code_to_label.get(bcode, "")

                if label:
                    bname = next((n for l, (n, c) in broker_map.items() if c == bcode), bcode)
                    hits.append((w["代號"], w["名稱"], w["標的股"], w.get("標的名稱", ""), label, bname, bcode))

        return hits

    with ThreadPoolExecutor(max_workers=PRESCAN_WORKERS) as ex:
        futures = {ex.submit(prescan_one, w): w for w in warrants}

        for future in as_completed(futures):
            done += 1

            try:
                result = future.result()
            except:
                result = []

            for hit in result:
                candidates.append(hit)

            if done % 1000 == 0:
                print(f"  [{done:,}/{len(warrants):,}] 預篩中，候選 {len(candidates)} 組...")

    unique_candidates = []
    seen = set()

    for c in candidates:
        key = (c[0], c[6])
        if key not in seen:
            seen.add(key)
            unique_candidates.append(c)

    print(f"  ✅ 預篩完成：{len(warrants)} 支 → {len(candidates)} 組候選，去重後 {len(unique_candidates)} 組")
    return unique_candidates




def prescan_all(warrants, broker_map):
    global PRESCAN_REFRESH_KEYS

    cached_candidates = load_candidates_cache()

    if cached_candidates:
        print("【Step 3a】讀取候選組合快取...")
        print(f"  ✅ 已讀取候選組合快取：{len(cached_candidates)} 組")

        if FAST_SKIP_RECENT_PRESCAN:
            # 每日執行時最大的耗時通常不是日期範圍，而是 prescan_all_live 仍然會掃描全市場所有權證。
            # 有候選組合快取時，直接更新既有候選組合的 API5 歷史資料，避免每天對全市場權證逐一 api4_get。
            # 若要重新發現新權證 / 新候選組合，請設定 FAST_SKIP_RECENT_PRESCAN=0。
            PRESCAN_REFRESH_KEYS = {candidate_key_from_tuple(c) for c in cached_candidates}
            print("  ⚡ 加速模式：已略過全市場最近資料預掃描，改為更新既有候選組合。")
            print("  ⚠️ 若需重新發現新權證候選，請設定 FAST_SKIP_RECENT_PRESCAN=0 後再執行。")
            print(f"  ✅ 本次需檢查更新的候選組合：{len(PRESCAN_REFRESH_KEYS)} 組")
            return cached_candidates

        print(f"  🔄 補掃最近 {CACHE_RECENT_SCAN_DAYS} 天，用來判斷需要更新的候選組合...")

        recent_candidates = prescan_all_live(warrants, broker_map, scan_days=CACHE_RECENT_SCAN_DAYS)
        PRESCAN_REFRESH_KEYS = {candidate_key_from_tuple(c) for c in recent_candidates}

        merged_candidates = merge_candidates(cached_candidates, recent_candidates)
        save_candidates_cache(merged_candidates)

        print(
            f"  ✅ 候選組合快取合併完成：舊 {len(cached_candidates)} 組，"
            f"最近掃到 {len(recent_candidates)} 組，合併後 {len(merged_candidates)} 組"
        )
        print(f"  ✅ 本次需檢查更新的候選組合：{len(PRESCAN_REFRESH_KEYS)} 組")

        return merged_candidates

    candidates = prescan_all_live(warrants, broker_map, scan_days=40)
    PRESCAN_REFRESH_KEYS = {candidate_key_from_tuple(c) for c in candidates}
    save_candidates_cache(candidates)
    return candidates


# ══════════════════════════════════════════════════════════════════════
# A 事件：單檔權證單日買進金額 >= 100萬
# ══════════════════════════════════════════════════════════════════════

def build_a_events_from_df(item):
    df = item["df"]
    events = []

    for _, row in df.iterrows():
        date   = row["日期"]
        buy_s  = int(row["買進股數"])
        sell_s = int(row["賣出股數"])
        buy_a  = int(row["買進金額"])
        sell_a = int(row["賣出金額"])

        # 權證不可當沖：
        # 同一天若同時有買進與賣出，當日賣出視為賣之前庫存，
        # 不能拿來扣當天新建立的買進事件。
        # 因此每日處理順序改為：
        # 1. 先處理當日賣出，只扣買進日早於賣出日的舊事件
        # 2. 再建立當日買進事件
        if sell_s > 0 and events:
            sell_p = round(sell_a / sell_s, 4) if sell_s > 0 else 0
            sell_left = sell_s
            sell_dt = parse_date(date)

            for ev in events:
                if ev["剩餘股數"] <= 0:
                    continue

                ev_buy_dt = parse_date(ev["買進日"])
                if ev_buy_dt and sell_dt and ev_buy_dt >= sell_dt:
                    continue

                alloc = min(sell_left, ev["剩餘股數"])

                if alloc <= 0:
                    continue

                ev["剩餘股數"] -= alloc
                sell_left -= alloc

                if ev["剩餘股數"] > 0:
                    if ev["減碼日"] is None:
                        ev["減碼日"] = date
                        ev["減碼均價"] = sell_p
                        ev["減碼獲利%"] = round((sell_p - ev["買進均價"]) / ev["買進均價"] * 100, 2) if ev["買進均價"] else None
                    ev["狀態"] = "減碼"
                else:
                    ev["狀態"] = "出清"
                    ev["出清日"] = date
                    ev["出清均價"] = sell_p
                    ev["出清獲利%"] = round((sell_p - ev["買進均價"]) / ev["買進均價"] * 100, 2) if ev["買進均價"] else None

                    buy_dt = parse_date(ev["買進日"])
                    exit_dt = parse_date(date)
                    if buy_dt and exit_dt:
                        ev["持有天數"] = (exit_dt - buy_dt).days

                if sell_left <= 0:
                    break

        # A：買進金額 >= 100萬，不扣賣出金額
        # 當日買進事件在當日賣出處理後才建立，避免出現買進日當天減碼/出清。
        if buy_a >= AMOUNT_THRESH and buy_s > 0:
            buy_p = round(buy_a / buy_s, 4)

            events.append({
                "事件類型": "A-單檔權證大買",
                "事件代碼": "A",
                "分點": item["broker_label"],
                "分點名稱": item["broker_name"],
                "券商代號": item["broker_code"],
                "權證代號": item["warrant_code"],
                "權證名稱": item["warrant_name"],
                "標的股": item["underlying_code"],
                "買進日": date,
                "事件日": date,
                "買進股數": buy_s,
                "買進張數": buy_s // 1000,
                "買進金額": buy_a,
                "買進均價": buy_p,
                "剩餘股數": buy_s,
                "狀態": "持有",
                "減碼日": None,
                "減碼均價": None,
                "減碼獲利%": None,
                "出清日": None,
                "出清均價": None,
                "出清獲利%": None,
                "持有天數": None,
            })

    return events


# ══════════════════════════════════════════════════════════════════════
# Step 3b：抓候選組合歷史資料
# ══════════════════════════════════════════════════════════════════════

def process_candidate(warrant_code, warrant_name, underlying_code, underlying_name, broker_label, broker_name, broker_code):
    rows = api5_get(warrant_code, broker_code)

    if not rows:
        return None

    records = []

    for row in rows:
        buy_s  = int(float(row.get("V2", 0) or 0))
        sell_s = int(float(row.get("V3", 0) or 0))
        buy_a  = int(float(row.get("V4", 0) or 0) * 1000)
        sell_a = int(float(row.get("V5", 0) or 0) * 1000)

        records.append({
            "日期": normalize_date_str(row.get("V1", "")),
            "買進股數": buy_s,
            "賣出股數": sell_s,
            "買進金額": buy_a,
            "賣出金額": sell_a,
            "買超股數": buy_s - sell_s,
            "買超金額": buy_a - sell_a,
        })

    df = pd.DataFrame(records).sort_values("日期").reset_index(drop=True)

    item = {
        "warrant_code":    warrant_code,
        "warrant_name":    warrant_name,
        "underlying_code": underlying_code,
        "underlying_name": underlying_name,
        "broker_label":    broker_label,
        "broker_name":     broker_name,
        "broker_code":     broker_code,
        "df":              df,
    }

    item["events_a"] = build_a_events_from_df(item)
    return item


# ══════════════════════════════════════════════════════════════════════
# 建立每日資料，用於 B / C
# ══════════════════════════════════════════════════════════════════════

def build_daily_records(items):
    daily_records = []

    for item in items:
        if not item["underlying_code"]:
            continue

        for _, row in item["df"].iterrows():
            daily_records.append({
                "日期": row["日期"],
                "分點": item["broker_label"],
                "分點名稱": item["broker_name"],
                "券商代號": item["broker_code"],
                "權證代號": item["warrant_code"],
                "權證名稱": item["warrant_name"],
                "標的股": item["underlying_code"],
                "標的名稱": item.get("underlying_name", ""),
                "買進股數": int(row["買進股數"]),
                "賣出股數": int(row["賣出股數"]),
                "買進金額": int(row["買進金額"]),
                "賣出金額": int(row["賣出金額"]),
                "買超股數": int(row["買超股數"]),
                "買超金額": int(row["買超金額"]),
            })

    return daily_records


def make_daily_key(broker_code, underlying_code, date, warrant_code):
    return (
        str(broker_code),
        str(underlying_code),
        normalize_date_str(date),
        str(warrant_code),
    )


def make_a_exclude_keys(a_events):
    """
    A > B > C 去重：
    已經被 A 單檔大買抓到的「券商分點 + 標的股 + 日期 + 權證」
    不再進入 B / C 的同標的合計買超判斷。
    """
    keys = set()

    for ev in a_events:
        if not ev.get("標的股"):
            continue

        keys.add(make_daily_key(
            ev.get("券商代號"),
            ev.get("標的股"),
            ev.get("買進日"),
            ev.get("權證代號"),
        ))

    return keys


def make_b_exclude_keys(b_events):
    """
    A > B > C 去重：
    已經被 B 同標的單日合計買超抓到的資料，
    不再進入 C 的 3 日累積買超判斷。
    """
    keys = set()

    for ev in b_events:
        broker_code = ev.get("券商代號")
        underlying_code = ev.get("標的股")

        for lot in ev.get("lots", []):
            keys.add(make_daily_key(
                broker_code,
                underlying_code,
                lot.get("買進日"),
                lot.get("權證代號"),
            ))

    return keys


def filter_daily_records(daily_records, exclude_keys):
    if not exclude_keys:
        return daily_records

    filtered = []

    for row in daily_records:
        key = make_daily_key(
            row.get("券商代號"),
            row.get("標的股"),
            row.get("日期"),
            row.get("權證代號"),
        )

        if key not in exclude_keys:
            filtered.append(row)

    return filtered



def normalize_warrant_code_for_unique(warrant_code):
    """
    用於 A / B / C / D 權證互斥判斷。

    只要是同一檔權證代號，不論出現在不同日期、不同事件類型，
    都只允許進入 A / B / C / D 其中一類。
    """
    return str(warrant_code).strip()


def collect_event_warrant_codes(events):
    """
    從事件清單收集已經被使用的權證代號。

    A 類事件：直接看 ev["權證代號"]
    B / C / D 類事件：從 ev["lots"] 裡收集所有權證代號
    """
    codes = set()

    for ev in events:
        direct_code = ev.get("權證代號")
        if direct_code:
            codes.add(normalize_warrant_code_for_unique(direct_code))

        for lot in ev.get("lots", []):
            lot_code = lot.get("權證代號")
            if lot_code:
                codes.add(normalize_warrant_code_for_unique(lot_code))

    return codes


def filter_daily_records_by_warrant_codes(daily_records, exclude_warrant_codes):
    """
    以「權證代號」層級過濾資料。

    原本 make_daily_key 是同一筆「券商 + 標的 + 日期 + 權證」不重複；
    這裡改成只要同一檔權證已經出現在較高優先權事件，
    後面的 B / C / D 就完全不再使用這檔權證。
    """
    if not exclude_warrant_codes:
        return daily_records

    exclude_warrant_codes = {
        normalize_warrant_code_for_unique(code)
        for code in exclude_warrant_codes
        if str(code).strip()
    }

    filtered = []

    for row in daily_records:
        warrant_code = normalize_warrant_code_for_unique(row.get("權證代號", ""))

        if warrant_code not in exclude_warrant_codes:
            filtered.append(row)

    return filtered


def filter_a_events_unique_warrants(a_events):
    """
    A 類內部也避免同一檔權證重複出現。

    若同一檔權證有多筆 A 事件：
    1. 優先保留買進日較新的事件
    2. 同日則保留買進金額較大的事件
    """
    if not a_events:
        return []

    sorted_events = sorted(
        a_events,
        key=lambda ev: (
            (parse_date(ev.get("買進日")) or datetime.min),
            int(ev.get("買進金額") or 0),
        ),
        reverse=True
    )

    used_codes = set()
    filtered_events = []

    for ev in sorted_events:
        warrant_code = normalize_warrant_code_for_unique(ev.get("權證代號", ""))

        if not warrant_code:
            continue

        if warrant_code in used_codes:
            continue

        used_codes.add(warrant_code)
        filtered_events.append(ev)

    return filtered_events


# ══════════════════════════════════════════════════════════════════════
# B / C 群組事件出清推估
# ══════════════════════════════════════════════════════════════════════


_GROUP_OUTCOME_SALE_ROWS_CACHE = {}


def get_group_sale_rows_for_warrant(item_map, broker_code, warrant_code):
    """
    B / C / D 出清推估用的賣出資料快取。

    原本 simulate_group_outcome() 每產生一筆群組事件，都會重新掃該權證的 item["df"]。
    D 類事件數量一多，這裡會被重複執行很多次。
    這版改成同一個「券商代號 + 權證代號」只整理一次賣出資料，後面直接重用。
    """
    key = (str(broker_code).strip(), str(warrant_code).strip())

    if key in _GROUP_OUTCOME_SALE_ROWS_CACHE:
        return _GROUP_OUTCOME_SALE_ROWS_CACHE[key]

    item = item_map.get(key)
    rows = []

    if item:
        df = item.get("df", pd.DataFrame())

        if df is not None and not df.empty:
            needed_cols = ["日期", "賣出股數", "賣出金額"]

            if all(col in df.columns for col in needed_cols):
                for date, sell_s, sell_a in df[needed_cols].itertuples(index=False, name=None):
                    try:
                        sell_s = int(sell_s)
                        sell_a = int(sell_a)
                    except:
                        continue

                    if sell_s > 0:
                        rows.append({
                            "日期": normalize_date_str(date),
                            "權證代號": key[1],
                            "賣出股數": sell_s,
                            "賣出金額": sell_a,
                        })

    rows = sorted(rows, key=lambda x: (x["日期"], x["權證代號"]))
    _GROUP_OUTCOME_SALE_ROWS_CACHE[key] = rows
    return rows


def simulate_group_outcome(event, item_map):
    lots = []

    for lot in event["lots"]:
        if lot["股數"] <= 0 or lot["金額"] <= 0:
            continue

        lots.append({
            "權證代號": lot["權證代號"],
            "買進日": lot["買進日"],
            "股數": lot["股數"],
            "金額": lot["金額"],
            "均價": lot["金額"] / lot["股數"] if lot["股數"] else 0,
            "剩餘股數": lot["股數"],
        })

    total_cost = sum(lot["金額"] for lot in lots)

    event["減碼日"] = None
    event["減碼賣出金額"] = None
    event["減碼獲利%"] = None
    event["出清日"] = None
    event["出清賣出金額"] = None
    event["出清獲利%"] = None
    event["持有天數"] = None
    event["狀態"] = "持有"

    if total_cost <= 0 or not lots:
        return event

    future_sales = []
    event_end_date = normalize_date_str(event["結束日"])
    broker_code = str(event["券商代號"]).strip()

    for warrant_code in sorted(set(lot["權證代號"] for lot in lots)):
        for sale in get_group_sale_rows_for_warrant(item_map, broker_code, warrant_code):
            if sale["日期"] > event_end_date:
                future_sales.append(sale)

    future_sales = sorted(future_sales, key=lambda x: (x["日期"], x["權證代號"]))

    realized_revenue = 0
    realized_cost = 0

    def remaining_total():
        return sum(lot["剩餘股數"] for lot in lots)

    for sale in future_sales:
        sell_s = sale["賣出股數"]
        sell_a = sale["賣出金額"]

        if sell_s <= 0 or sell_a <= 0:
            continue

        sell_price = sell_a / sell_s
        sell_left = sell_s

        before_remaining = remaining_total()
        sale_revenue = 0
        sale_cost = 0

        for lot in lots:
            if lot["權證代號"] != sale["權證代號"]:
                continue
            if lot["剩餘股數"] <= 0:
                continue

            alloc = min(sell_left, lot["剩餘股數"])

            if alloc <= 0:
                continue

            revenue = alloc * sell_price
            cost = alloc * lot["均價"]

            lot["剩餘股數"] -= alloc
            sell_left -= alloc

            realized_revenue += revenue
            realized_cost += cost

            sale_revenue += revenue
            sale_cost += cost

            if sell_left <= 0:
                break

        after_remaining = remaining_total()

        if before_remaining > after_remaining:
            if event["減碼日"] is None and after_remaining > 0:
                event["減碼日"] = sale["日期"]
                event["減碼賣出金額"] = round(sale_revenue, 0)
                event["減碼獲利%"] = round((sale_revenue - sale_cost) / sale_cost * 100, 2) if sale_cost else None
                event["狀態"] = "減碼"

            if after_remaining <= 0:
                event["出清日"] = sale["日期"]
                event["出清賣出金額"] = round(realized_revenue, 0)
                event["出清獲利%"] = round((realized_revenue - total_cost) / total_cost * 100, 2)
                event["狀態"] = "出清"

                start_dt = parse_date(event["起始日"])
                exit_dt = parse_date(event["出清日"])
                if start_dt and exit_dt:
                    event["持有天數"] = (exit_dt - start_dt).days

                break

    return event


# ══════════════════════════════════════════════════════════════════════
# B：同分點 + 同標的 + 同一天，多檔權證合計買超 >= 100萬
# ══════════════════════════════════════════════════════════════════════

def build_b_events(daily_records, item_map):
    events = []
    used_b_warrant_codes = set()

    if not daily_records:
        return events

    df = pd.DataFrame(daily_records)
    df = df[(df["買超金額"] > 0) & (df["買超股數"] > 0)]

    if df.empty:
        return events

    group_cols = ["分點", "分點名稱", "券商代號", "標的股", "日期"]

    for key, g in df.groupby(group_cols):
        broker_label, broker_name, broker_code, underlying_code, date = key

        lots = []

        warrant_rows = g.groupby(["權證代號", "權證名稱"], as_index=False).agg({
            "買超金額": "sum",
            "買超股數": "sum",
        })

        for _, wr in warrant_rows.iterrows():
            warrant_code = normalize_warrant_code_for_unique(wr["權證代號"])

            # B 類內部已經使用過的權證，不再進入後續 B 事件。
            if warrant_code in used_b_warrant_codes:
                continue

            if int(wr["買超金額"]) <= 0 or int(wr["買超股數"]) <= 0:
                continue

            lots.append({
                "買進日": date,
                "權證代號": wr["權證代號"],
                "權證名稱": wr["權證名稱"],
                "金額": int(wr["買超金額"]),
                "股數": int(wr["買超股數"]),
            })

        warrant_count = len(set(normalize_warrant_code_for_unique(lot["權證代號"]) for lot in lots))
        total_amount = int(sum(lot["金額"] for lot in lots))
        total_shares = int(sum(lot["股數"] for lot in lots))

        if warrant_count < 2:
            continue
        if total_amount < AMOUNT_THRESH:
            continue
        if total_shares <= 0:
            continue

        event = {
            "事件類型": "B-同標的單日合計買超",
            "事件代碼": "B",
            "分點": broker_label,
            "分點名稱": broker_name,
            "券商代號": broker_code,
            "標的股": underlying_code,
            "起始日": date,
            "結束日": date,
            "事件日": date,
            "涵蓋權證數": warrant_count,
            "權證清單": "；".join([f'{lot["權證代號"]} {lot["權證名稱"]}' for lot in lots]),
            "買超金額": total_amount,
            "買超股數": total_shares,
            "買超張數": total_shares // 1000,
            "lots": lots,
        }

        event = simulate_group_outcome(event, item_map)
        events.append(event)
        used_b_warrant_codes.update(
            normalize_warrant_code_for_unique(lot["權證代號"])
            for lot in lots
        )

    return events


# ══════════════════════════════════════════════════════════════════════
# C：同分點 + 同標的，連續 3 交易日多檔權證累積買超 >= 100萬
# ══════════════════════════════════════════════════════════════════════


def build_c_events(daily_records, item_map):
    events = []
    used_c_keys = set()
    used_c_warrant_codes = set()

    if not daily_records:
        return events

    df = pd.DataFrame(daily_records)
    df = df[(df["買超金額"] > 0) & (df["買超股數"] > 0)].copy()

    if df.empty:
        return events

    # C 類必須使用「連續 3 個交易日」視窗。
    # 速度優化：同一群組內改用滑動視窗累加 / 扣除，避免每個視窗重複 isin + groupby。
    trade_dates = sorted(df["日期"].dropna().unique())

    if len(trade_dates) < 3:
        return events

    window_days = 3
    date_to_idx = {d: i for i, d in enumerate(trade_dates)}
    df["日期序號"] = df["日期"].map(date_to_idx)
    df = df.dropna(subset=["日期序號"]).copy()
    df["日期序號"] = df["日期序號"].astype(int)

    if df.empty:
        return events

    main_group_cols = ["分點", "分點名稱", "券商代號", "標的股"]

    for key, g in df.groupby(main_group_cols, sort=False):
        broker_label, broker_name, broker_code, underlying_code = key

        if g.empty:
            continue

        g = g.sort_values(["日期序號", "權證代號"]).reset_index(drop=True)
        rows_by_idx = {}

        for row in g[[
            "日期序號", "日期", "券商代號", "標的股", "權證代號", "權證名稱", "買超金額", "買超股數"
        ]].itertuples(index=False, name=None):
            idx = int(row[0])
            rows_by_idx.setdefault(idx, []).append(row)

        if not rows_by_idx:
            continue

        min_idx = min(rows_by_idx.keys())
        max_idx = max(rows_by_idx.keys())

        start_i_min = max(0, min_idx - window_days + 1)
        start_i_max = min(max_idx, len(trade_dates) - window_days)

        if start_i_max < start_i_min:
            continue

        window_lot_map = {}
        window_keys = set()

        def add_row_to_window(row):
            _, date, row_broker_code, row_underlying_code, warrant_code, warrant_name, buy_amount, buy_shares = row
            lot_key = (date, normalize_warrant_code_for_unique(warrant_code), warrant_name)

            rec = window_lot_map.setdefault(lot_key, {
                "買進日": date,
                "權證代號": warrant_code,
                "權證名稱": warrant_name,
                "金額": 0,
                "股數": 0,
            })

            rec["金額"] += int(buy_amount)
            rec["股數"] += int(buy_shares)

            window_keys.add(make_daily_key(
                row_broker_code,
                row_underlying_code,
                date,
                warrant_code,
            ))

        def remove_row_from_window(row):
            _, date, row_broker_code, row_underlying_code, warrant_code, warrant_name, buy_amount, buy_shares = row
            lot_key = (date, normalize_warrant_code_for_unique(warrant_code), warrant_name)
            rec = window_lot_map.get(lot_key)

            if rec:
                rec["金額"] -= int(buy_amount)
                rec["股數"] -= int(buy_shares)

                if rec["金額"] == 0 and rec["股數"] == 0:
                    window_lot_map.pop(lot_key, None)

            window_keys.discard(make_daily_key(
                row_broker_code,
                row_underlying_code,
                date,
                warrant_code,
            ))

        first_start = start_i_min
        first_end = first_start + window_days - 1

        for idx in range(first_start, first_end + 1):
            for row in rows_by_idx.get(idx, []):
                add_row_to_window(row)

        for start_idx in range(start_i_min, start_i_max + 1):
            end_idx = start_idx + window_days - 1

            if start_idx > start_i_min:
                remove_idx = start_idx - 1
                add_idx = end_idx

                for row in rows_by_idx.get(remove_idx, []):
                    remove_row_from_window(row)

                for row in rows_by_idx.get(add_idx, []):
                    add_row_to_window(row)

            if not window_lot_map:
                continue

            if window_keys & used_c_keys:
                continue

            lots = []

            for lot_key in sorted(window_lot_map.keys()):
                rec = window_lot_map[lot_key]
                warrant_code = normalize_warrant_code_for_unique(rec["權證代號"])

                if warrant_code in used_c_warrant_codes:
                    continue

                lot_amount = int(rec["金額"])
                lot_shares = int(rec["股數"])

                if lot_amount <= 0 or lot_shares <= 0:
                    continue

                lots.append({
                    "買進日": rec["買進日"],
                    "權證代號": rec["權證代號"],
                    "權證名稱": rec["權證名稱"],
                    "金額": lot_amount,
                    "股數": lot_shares,
                })

            warrant_count = len(set(normalize_warrant_code_for_unique(lot["權證代號"]) for lot in lots))
            total_amount = int(sum(lot["金額"] for lot in lots))
            total_shares = int(sum(lot["股數"] for lot in lots))

            if warrant_count < 2:
                continue
            if total_amount < AMOUNT_THRESH:
                continue
            if total_shares <= 0:
                continue

            window_dates = trade_dates[start_idx:end_idx + 1]

            event = {
                "事件類型": "C-同標的3日累積買超",
                "事件代碼": "C",
                "分點": broker_label,
                "分點名稱": broker_name,
                "券商代號": broker_code,
                "標的股": underlying_code,
                "起始日": window_dates[0],
                "結束日": window_dates[-1],
                "事件日": window_dates[-1],
                "涵蓋權證數": warrant_count,
                "權證清單": "；".join([f'{lot["權證代號"]} {lot["權證名稱"]}' for lot in lots]),
                "買超金額": total_amount,
                "買超股數": total_shares,
                "買超張數": total_shares // 1000,
                "lots": lots,
            }

            event = simulate_group_outcome(event, item_map)
            events.append(event)
            used_c_keys.update(window_keys)
            used_c_warrant_codes.update(
                normalize_warrant_code_for_unique(lot["權證代號"])
                for lot in lots
            )

    return events


def make_c_exclude_keys(c_events):
    """
    A > B > C > D 去重：
    已經被 C 同標的 3 日累積買超抓到的資料，
    不再進入 D 的近 N 日累積淨買進判斷。
    """
    return make_b_exclude_keys(c_events)


# ══════════════════════════════════════════════════════════════════════
# D：同分點 + 同標的，近 N 個交易日累積淨買進 >= 100萬
# ══════════════════════════════════════════════════════════════════════


def build_d_events(daily_records, item_map, window_days=None):
    """
    D 類補強慢慢買 / 分批買情境：

    條件：
    1. 同一分點 + 同一標的
    2. 近 N 個交易日內，所有相關認購權證合計
    3. 累積淨買進金額 >= AMOUNT_THRESH
    4. 累積淨買進股數 > 0
    5. A / B / C 已使用過的權證代號不再重複進 D

    速度優化版：
    原本每個群組、每個視窗都重新做 DataFrame isin + groupby，
    會非常慢。這版改成同一群組內用滑動視窗累加 / 扣除，
    避免大量重複篩選與 groupby。
    """
    if window_days is None:
        window_days = D_WINDOW_DAYS

    events = []
    used_d_keys = set()
    used_d_warrant_codes = set()

    if not daily_records:
        return events

    df = pd.DataFrame(daily_records)

    if df.empty:
        return events

    df = df[(df["買進金額"] > 0) | (df["賣出金額"] > 0)].copy()

    if df.empty:
        return events

    trade_dates = sorted(df["日期"].dropna().unique())

    if len(trade_dates) < window_days:
        return events

    date_to_idx = {d: i for i, d in enumerate(trade_dates)}
    df["日期序號"] = df["日期"].map(date_to_idx)
    df = df.dropna(subset=["日期序號"]).copy()
    df["日期序號"] = df["日期序號"].astype(int)

    if df.empty:
        return events

    main_group_cols = ["分點", "分點名稱", "券商代號", "標的股"]

    for key, g in df.groupby(main_group_cols, sort=False):
        broker_label, broker_name, broker_code, underlying_code = key

        if g.empty:
            continue

        g = g.sort_values(["日期序號", "權證代號"]).reset_index(drop=True)
        rows_by_idx = {}

        for row in g[[
            "日期序號", "日期", "券商代號", "標的股", "權證代號", "權證名稱",
            "買進金額", "賣出金額", "買進股數", "賣出股數"
        ]].itertuples(index=False, name=None):
            idx = int(row[0])
            rows_by_idx.setdefault(idx, []).append(row)

        if not rows_by_idx:
            continue

        min_idx = min(rows_by_idx.keys())
        max_idx = max(rows_by_idx.keys())

        start_i_min = max(0, min_idx - window_days + 1)
        start_i_max = min(max_idx, len(trade_dates) - window_days)

        if start_i_max < start_i_min:
            continue

        window_warrant_map = {}
        window_keys = set()

        def add_row_to_window(row):
            _, date, row_broker_code, row_underlying_code, warrant_code, warrant_name, buy_amount, sell_amount, buy_shares, sell_shares = row
            warrant_code_norm = normalize_warrant_code_for_unique(warrant_code)

            net_amount = int(buy_amount) - int(sell_amount)
            net_shares = int(buy_shares) - int(sell_shares)

            rec = window_warrant_map.setdefault(warrant_code_norm, {
                "權證代號": warrant_code,
                "權證名稱": warrant_name,
                "買超金額": 0,
                "買超股數": 0,
            })

            rec["買超金額"] += net_amount
            rec["買超股數"] += net_shares

            window_keys.add(make_daily_key(
                row_broker_code,
                row_underlying_code,
                date,
                warrant_code,
            ))

        def remove_row_from_window(row):
            _, date, row_broker_code, row_underlying_code, warrant_code, warrant_name, buy_amount, sell_amount, buy_shares, sell_shares = row
            warrant_code_norm = normalize_warrant_code_for_unique(warrant_code)

            net_amount = int(buy_amount) - int(sell_amount)
            net_shares = int(buy_shares) - int(sell_shares)

            rec = window_warrant_map.get(warrant_code_norm)

            if rec:
                rec["買超金額"] -= net_amount
                rec["買超股數"] -= net_shares

                if rec["買超金額"] == 0 and rec["買超股數"] == 0:
                    window_warrant_map.pop(warrant_code_norm, None)

            window_keys.discard(make_daily_key(
                row_broker_code,
                row_underlying_code,
                date,
                warrant_code,
            ))

        first_start = start_i_min
        first_end = first_start + window_days - 1

        for idx in range(first_start, first_end + 1):
            for row in rows_by_idx.get(idx, []):
                add_row_to_window(row)

        for start_idx in range(start_i_min, start_i_max + 1):
            end_idx = start_idx + window_days - 1

            if start_idx > start_i_min:
                remove_idx = start_idx - 1
                add_idx = end_idx

                for row in rows_by_idx.get(remove_idx, []):
                    remove_row_from_window(row)

                for row in rows_by_idx.get(add_idx, []):
                    add_row_to_window(row)

            if not window_warrant_map:
                continue

            # 避免 D 類滑動視窗彼此重複使用同一批資料
            if window_keys & used_d_keys:
                continue

            start_date = trade_dates[start_idx]
            end_date = trade_dates[end_idx]
            lots = []

            for warrant_code in sorted(window_warrant_map.keys()):
                rec = window_warrant_map[warrant_code]
                warrant_code_norm = normalize_warrant_code_for_unique(warrant_code)

                # D 類內部已經使用過的權證，不再進入後續 D 事件。
                if warrant_code_norm in used_d_warrant_codes:
                    continue

                lot_amount = int(rec["買超金額"])
                lot_shares = int(rec["買超股數"])

                if lot_amount <= 0 or lot_shares <= 0:
                    continue

                lots.append({
                    "買進日": end_date,
                    "權證代號": rec["權證代號"],
                    "權證名稱": rec["權證名稱"],
                    "金額": lot_amount,
                    "股數": lot_shares,
                })

            total_amount = int(sum(lot["金額"] for lot in lots))
            total_shares = int(sum(lot["股數"] for lot in lots))

            if total_amount < AMOUNT_THRESH:
                continue

            if total_shares <= 0:
                continue

            if not lots:
                continue

            event = {
                "事件類型": f"D-近{window_days}日累積淨買進",
                "事件代碼": "D",
                "分點": broker_label,
                "分點名稱": broker_name,
                "券商代號": broker_code,
                "標的股": underlying_code,
                "起始日": start_date,
                "結束日": end_date,
                "事件日": end_date,
                "涵蓋權證數": len(set(lot["權證代號"] for lot in lots)),
                "權證清單": "；".join([f'{lot["權證代號"]} {lot["權證名稱"]}' for lot in lots]),
                "買超金額": total_amount,
                "買超股數": total_shares,
                "買超張數": total_shares // 1000,
                "lots": lots,
            }

            event = simulate_group_outcome(event, item_map)
            events.append(event)
            used_d_keys.update(window_keys)
            used_d_warrant_codes.update(
                normalize_warrant_code_for_unique(lot["權證代號"])
                for lot in lots
            )

    return events


# ══════════════════════════════════════════════════════════════════════
# Step 4：抓收盤價
# ══════════════════════════════════════════════════════════════════════

def fetch_all_prices(a_events, b_events, c_events, d_events):
    print("【Step 4】抓收盤價...")

    code_ranges = {}

    def update_code_range(code, start_dt, end_dt):
        if not code or start_dt is None or end_dt is None:
            return

        code = normalize_price_code(code)

        if not code:
            return

        if end_dt < start_dt:
            end_dt = start_dt

        if code not in code_ranges:
            code_ranges[code] = [start_dt, end_dt]
        else:
            code_ranges[code][0] = min(code_ranges[code][0], start_dt)
            code_ranges[code][1] = max(code_ranges[code][1], end_dt)

    for ev in a_events:
        dt = parse_date(ev["買進日"])
        if dt:
            start_dt = dt - timedelta(days=10)
            end_dt = dt + timedelta(days=160)
            update_code_range(ev["權證代號"], start_dt, end_dt)
            update_code_range(ev["標的股"], start_dt, end_dt)

    for ev in b_events + c_events + d_events:
        dt = parse_date(ev["結束日"])
        if dt:
            start_dt = dt - timedelta(days=10)
            end_dt = dt + timedelta(days=160)
            update_code_range(ev["標的股"], start_dt, end_dt)

            # B / C / D 的 D+ 欄位目前只看標的股，因此預設不再抓群組事件中每一檔權證價格。
            # 這可以大幅降低價格補抓數量；若未來需要群組事件權證明細價格，
            # 可設定 FETCH_GROUP_WARRANT_PRICES=1。
            if FETCH_GROUP_WARRANT_PRICES:
                for lot in ev.get("lots", []):
                    update_code_range(lot.get("權證代號"), start_dt, end_dt)

    all_codes = list(code_ranges.keys())
    price_cache = {}
    total = len(all_codes)

    if total == 0:
        print(f"  ✅ 共 {len(price_cache)} 支股票/權證收盤價")
        return price_cache

    persistent_price_cache = load_price_cache()
    print(f"  價格快取讀取：{len(persistent_price_cache):,} 個代號")

    today = datetime.today()
    fetch_plan = {}

    for code in all_codes:
        start_dt, end_dt = code_ranges[code]

        if end_dt > today:
            end_dt = today

        if start_dt > end_dt:
            start_dt = end_dt

        cached_prices = get_cached_prices_for_code(persistent_price_cache, code)

        if cached_prices:
            add_price_aliases(price_cache, code, cached_prices)

            valid_dates = sorted([d for d, p in cached_prices.items() if p is not None and p > 0])
            latest_dt = parse_date(valid_dates[-1]) if valid_dates else None

            # 快取不只看最後日期，也要檢查覆蓋率品質。
            # 若快取筆數太少、有效日期不足，或覆蓋不到需求區間，仍重新補抓完整區間，避免 D+ 欄位大量出現 -。
            if prices_need_yahoo_fallback(cached_prices, start_dt, end_dt):
                fetch_plan[code] = [start_dt, end_dt]
            elif latest_dt and latest_dt < end_dt:
                fetch_start_dt = latest_dt + timedelta(days=1)

                if fetch_start_dt <= end_dt:
                    fetch_plan[code] = [fetch_start_dt, end_dt]
            elif not latest_dt:
                fetch_plan[code] = [start_dt, end_dt]
        else:
            add_price_aliases(price_cache, code, {})
            fetch_plan[code] = [start_dt, end_dt]

    print(f"  價格代號去重後：{total:,} 檔")
    print(f"  價格快取命中：{total - len(fetch_plan):,} 檔")
    print(f"  本次需補抓價格：{len(fetch_plan):,} 檔")

    if not fetch_plan:
        print(f"  ✅ 共 {len(price_cache)} 支股票/權證收盤價")
        return price_cache

    def fetch_one(code):
        start_dt, end_dt = fetch_plan[code]
        return code, fetch_twse_prices(code, start_dt, end_dt)

    price_workers = PRICE_WORKERS
    print(f"  價格抓取執行緒：{price_workers}")

    done = 0

    with ThreadPoolExecutor(max_workers=price_workers) as ex:
        futures = {ex.submit(fetch_one, code): code for code in fetch_plan}

        for future in as_completed(futures):
            done += 1

            try:
                code, fetched_prices = future.result()
                old_prices = get_cached_prices_for_code(persistent_price_cache, code)
                merged_prices = merge_price_dicts(old_prices, fetched_prices)

                norm_code = normalize_price_code(code)

                if norm_code:
                    persistent_price_cache[norm_code] = merged_prices

                add_price_aliases(price_cache, code, merged_prices)

            except:
                code = futures[future]
                old_prices = get_cached_prices_for_code(persistent_price_cache, code)
                add_price_aliases(price_cache, code, old_prices)

            if done % 20 == 0:
                print(f"  [{done}/{len(fetch_plan)}] 收盤價補抓中...")

    save_price_cache(persistent_price_cache)

    print(f"  ✅ 共 {len(price_cache)} 支股票/權證收盤價")
    return price_cache

# ══════════════════════════════════════════════════════════════════════
# D+1 ~ D+20
# ══════════════════════════════════════════════════════════════════════

def get_price_series_from_cache(price_cache, code):
    if not code:
        return {}

    code1 = str(code).strip()
    code2 = normalize_price_code(code1)
    code3 = code2.lstrip("0") if code2 else ""

    for c in [code1, code2, code3]:
        if c and c in price_cache:
            return price_cache.get(c, {})

    return {}


def price_dates_after(prices, base_date):
    base_date = normalize_date_str(base_date)
    return [d for d in sorted(prices.keys()) if d > base_date]


def build_dplus_dates(base_date, primary_prices, secondary_prices=None, limit=20):
    """
    建立 D+1 ~ D+20 的交易日序列。

    原本用「權證價格日期 + 標的價格日期」聯集，容易造成 D+ 日期亂跳；
    現在改成：
    1. 優先使用標的股價格日期，因為標的股交易日最完整。
    2. 標的股不足時，用權證價格日期補。
    3. 最後才用兩者聯集。
    """
    base_date = normalize_date_str(base_date)

    primary_dates = price_dates_after(primary_prices or {}, base_date)
    secondary_dates = price_dates_after(secondary_prices or {}, base_date)

    if len(primary_dates) >= limit:
        return primary_dates[:limit]

    merged = sorted(set(primary_dates + secondary_dates))
    return merged[:limit]


def get_price_on_or_before(prices, date):
    return get_price_nearest(prices, date)


def calc_pct_by_base(current_price, base_price):
    if current_price is None or base_price is None:
        return None

    try:
        current_price = float(current_price)
        base_price = float(base_price)

        if base_price <= 0 or current_price <= 0:
            return None

        return round((current_price - base_price) / base_price * 100, 2)
    except:
        return None


def get_buy_avg_as_base(ev):
    try:
        v = float(ev.get("買進均價"))
        return v if v > 0 else None
    except:
        return None


def make_a_day_cells(ev, item_map, price_cache):
    day_values = []
    day_status = []

    item = item_map.get((ev["券商代號"], ev["權證代號"]))
    if not item:
        return [""] * 20, ["none"] * 20

    w_prices = get_price_series_from_cache(price_cache, ev["權證代號"])
    u_prices = get_price_series_from_cache(price_cache, ev["標的股"]) if ev["標的股"] else {}

    buy_date = normalize_date_str(ev["買進日"])

    # 權證基準價：優先使用買進日之前最近收盤價；若沒有，使用實際買進均價補救。
    buy_w = get_price_on_or_before(w_prices, buy_date) if w_prices else None
    if buy_w is None:
        buy_w = get_buy_avg_as_base(ev)

    # 標的基準價：使用買進日之前最近收盤價。
    buy_u = get_price_on_or_before(u_prices, buy_date) if u_prices else None

    # D+ 日期以標的股交易日為主，權證日期為輔。
    future_dates = build_dplus_dates(
        buy_date,
        primary_prices=u_prices,
        secondary_prices=w_prices,
        limit=20
    )

    sell_by_date = item["df"].groupby("日期")["賣出股數"].sum().to_dict()

    stopped = False

    for i, check_date in enumerate(future_dates[:20]):
        check_date = normalize_date_str(check_date)

        # 權證與標的獨立抓價；一邊沒有資料，不影響另一邊。
        w_p = get_price_on_or_before(w_prices, check_date) if w_prices else None
        u_p = get_price_on_or_before(u_prices, check_date) if u_prices else None

        w_chg = calc_pct_by_base(w_p, buy_w)
        u_chg = calc_pct_by_base(u_p, buy_u)

        w_str = fmt_pct(w_chg)
        u_str = fmt_pct(u_chg)

        cell_text = f"權:{w_str}\n標:{u_str}"

        day_sell = int(sell_by_date.get(check_date, 0))

        if ev["出清日"] == check_date:
            status = "exit"
        elif ev["減碼日"] == check_date or day_sell > 0:
            status = "reduce"
        elif w_chg is not None and w_chg > 0:
            status = "win"
        else:
            status = "lose"

        day_values.append(cell_text)
        day_status.append(status)

        if ev["出清日"] and check_date >= ev["出清日"]:
            for _ in range(20 - i - 1):
                day_values.append("")
                day_status.append("none")
            stopped = True
            break

    if not stopped:
        while len(day_values) < 20:
            day_values.append("")
            day_status.append("none")

    return day_values, day_status


def make_group_day_cells(ev, price_cache):
    day_values = []
    day_status = []

    u_prices = get_price_series_from_cache(price_cache, ev["標的股"])
    base_date = normalize_date_str(ev["結束日"])

    buy_u = get_price_on_or_before(u_prices, base_date) if u_prices else None

    future_dates = build_dplus_dates(
        base_date,
        primary_prices=u_prices,
        secondary_prices=None,
        limit=20
    )

    stopped = False

    for i, check_date in enumerate(future_dates[:20]):
        check_date = normalize_date_str(check_date)

        u_p = get_price_on_or_before(u_prices, check_date) if u_prices else None
        u_chg = calc_pct_by_base(u_p, buy_u)

        cell_text = f"標:{fmt_pct(u_chg)}"

        if ev["出清日"] == check_date:
            status = "exit"
        elif ev["減碼日"] == check_date:
            status = "reduce"
        elif u_chg is not None and u_chg > 0:
            status = "win"
        else:
            status = "lose"

        day_values.append(cell_text)
        day_status.append(status)

        if ev["出清日"] and check_date >= ev["出清日"]:
            for _ in range(20 - i - 1):
                day_values.append("")
                day_status.append("none")
            stopped = True
            break

    if not stopped:
        while len(day_values) < 20:
            day_values.append("")
            day_status.append("none")

    return day_values, day_status


# ══════════════════════════════════════════════════════════════════════
# Excel 樣式
# ══════════════════════════════════════════════════════════════════════

def style_sheet(ws, col_widths, status_rows=None, header_row=1):
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for cell in ws[header_row]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.row_dimensions[header_row].height = 24

    fill_map = {
        "win": RED,
        "lose": GREEN,
        "reduce": BLUE,
        "exit": ORANGE,
        "none": PatternFill(),
    }

    if status_rows:
        for r_idx, status_row in enumerate(status_rows, header_row + 1):
            for c_idx, status in enumerate(status_row, 1):
                cell = ws.cell(r_idx, c_idx)
                cell.fill = fill_map.get(status, PatternFill())
                cell.font = Font(color="000000")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.row_dimensions[r_idx].height = 36
    else:
        for row in ws.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.font = Font(color="000000")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.freeze_panes = f"A{header_row + 1}"


def apply_exit_profit_result_outline(ws, row_idx, col_idx, return_pct):
    """
    針對實際「減碼獲利% / 出清獲利%」欄位加外框：
    獲利% > 0：粗紅色外框
    獲利% < 0：粗綠色外框

    不再框 D+N 橘色出清格，避免把「收盤價漲跌」誤解成實際出清損益。
    """
    if return_pct is None:
        return

    try:
        pct = float(return_pct)
    except:
        return

    if pct > 0:
        side = PROFIT_EXIT_SIDE
    elif pct < 0:
        side = LOSS_EXIT_SIDE
    else:
        return

    cell = ws.cell(row_idx, col_idx)
    cell.border = Border(
        left=side,
        right=side,
        top=side,
        bottom=side,
    )


# ══════════════════════════════════════════════════════════════════════
# 建立 Excel：A / B / C / 勝率統計
# ══════════════════════════════════════════════════════════════════════

def write_a_sheet(wb, a_events, item_map, price_cache):
    ws = wb.create_sheet("A_單檔大買")

    day_cols = [f"D+{i}" for i in range(1, 21)]

    headers = [
        "事件類型", "分點", "權證代號", "權證名稱", "標的股",
        "買進日", "買進張數", "買進金額", "買進均價",
        "減碼日", "減碼均價", "減碼獲利%",
        "出清日", "出清均價", "出清獲利%",
        "持有天數",
    ] + day_cols

    ws.append(headers)
    status_rows = []
    exit_profit_result_cells = []

    sorted_events = sorted(
        a_events,
        key=lambda x: (
            -((parse_date(x.get("買進日")) or datetime.min).toordinal()),
            str(x.get("分點", "")),
            str(x.get("權證代號", "")),
        )
    )

    for ev in sorted_events:
        day_values, day_status = make_a_day_cells(ev, item_map, price_cache)

        row = [
            ev["事件類型"],
            ev["分點"],
            ev["權證代號"],
            ev["權證名稱"],
            ev["標的股"],
            ev["買進日"],
            ev["買進張數"],
            fmt_amount(ev["買進金額"]),
            ev["買進均價"],
            ev["減碼日"] or "-",
            fmt_num(ev["減碼均價"]),
            fmt_pct(ev["減碼獲利%"]),
            ev["出清日"] or "-",
            fmt_num(ev["出清均價"]),
            fmt_pct(ev["出清獲利%"]),
            fmt_num(ev["持有天數"]),
        ] + day_values

        ws.append(row)
        current_row_idx = ws.max_row
        status_rows.append(["none"] * 16 + day_status)

        if ev["減碼獲利%"] is not None:
            # A 工作表第 12 欄為「減碼獲利%」
            exit_profit_result_cells.append((current_row_idx, 12, ev["減碼獲利%"]))

        if ev["出清獲利%"] is not None:
            # A 工作表第 15 欄為「出清獲利%」
            exit_profit_result_cells.append((current_row_idx, 15, ev["出清獲利%"]))

    col_widths = [18, 14, 10, 22, 8, 12, 10, 12, 10, 12, 10, 12, 12, 10, 12, 10] + [16] * 20
    style_sheet(ws, col_widths, status_rows)

    for row_idx, col_idx, return_pct in exit_profit_result_cells:
        apply_exit_profit_result_outline(ws, row_idx, col_idx, return_pct)


def write_group_sheet(wb, sheet_name, events, price_cache, is_c=False):
    ws = wb.create_sheet(sheet_name)

    day_cols = [f"D+{i}" for i in range(1, 21)]

    if is_c:
        headers = [
            "事件類型", "分點", "標的股",
            "起始日", "結束日",
            "涵蓋權證數", "權證清單",
            "買超金額", "買超張數",
            "減碼日", "減碼賣出金額", "減碼獲利%",
            "出清日", "出清賣出金額", "出清獲利%",
            "持有天數",
        ] + day_cols
        fixed_len = 16
    else:
        headers = [
            "事件類型", "分點", "標的股",
            "事件日",
            "涵蓋權證數", "權證清單",
            "買超金額", "買超張數",
            "減碼日", "減碼賣出金額", "減碼獲利%",
            "出清日", "出清賣出金額", "出清獲利%",
            "持有天數",
        ] + day_cols
        fixed_len = 15

    ws.append(headers)
    status_rows = []
    exit_profit_result_cells = []

    sort_date_field = "結束日" if is_c else "事件日"
    sorted_events = sorted(
        events,
        key=lambda x: (
            -((parse_date(x.get(sort_date_field) or x.get("事件日") or x.get("起始日")) or datetime.min).toordinal()),
            str(x.get("分點", "")),
            str(x.get("標的股", "")),
        )
    )

    for ev in sorted_events:
        day_values, day_status = make_group_day_cells(ev, price_cache)

        if is_c:
            row = [
                ev["事件類型"],
                ev["分點"],
                ev["標的股"],
                ev["起始日"],
                ev["結束日"],
                ev["涵蓋權證數"],
                ev["權證清單"],
                fmt_amount(ev["買超金額"]),
                ev["買超張數"],
                ev["減碼日"] or "-",
                fmt_amount(ev["減碼賣出金額"]),
                fmt_pct(ev["減碼獲利%"]),
                ev["出清日"] or "-",
                fmt_amount(ev["出清賣出金額"]),
                fmt_pct(ev["出清獲利%"]),
                fmt_num(ev["持有天數"]),
            ] + day_values
        else:
            row = [
                ev["事件類型"],
                ev["分點"],
                ev["標的股"],
                ev["事件日"],
                ev["涵蓋權證數"],
                ev["權證清單"],
                fmt_amount(ev["買超金額"]),
                ev["買超張數"],
                ev["減碼日"] or "-",
                fmt_amount(ev["減碼賣出金額"]),
                fmt_pct(ev["減碼獲利%"]),
                ev["出清日"] or "-",
                fmt_amount(ev["出清賣出金額"]),
                fmt_pct(ev["出清獲利%"]),
                fmt_num(ev["持有天數"]),
            ] + day_values

        ws.append(row)
        current_row_idx = ws.max_row
        status_rows.append(["none"] * fixed_len + day_status)

        if ev["減碼獲利%"] is not None:
            # B 工作表第 11 欄為「減碼獲利%」；C 工作表第 12 欄為「減碼獲利%」
            reduce_profit_col_idx = 12 if is_c else 11
            exit_profit_result_cells.append((current_row_idx, reduce_profit_col_idx, ev["減碼獲利%"]))

        if ev["出清獲利%"] is not None:
            # B 工作表第 14 欄為「出清獲利%」；C 工作表第 15 欄為「出清獲利%」
            exit_profit_col_idx = 15 if is_c else 14
            exit_profit_result_cells.append((current_row_idx, exit_profit_col_idx, ev["出清獲利%"]))

    if is_c:
        col_widths = [20, 14, 8, 12, 12, 12, 45, 14, 10, 12, 14, 12, 12, 14, 12, 10] + [14] * 20
    else:
        col_widths = [20, 14, 8, 12, 12, 45, 14, 10, 12, 14, 12, 12, 14, 12, 10] + [14] * 20

    style_sheet(ws, col_widths, status_rows)

    for row_idx, col_idx, return_pct in exit_profit_result_cells:
        apply_exit_profit_result_outline(ws, row_idx, col_idx, return_pct)


def collect_stat_records(a_events, b_events, c_events, d_events):
    records = []

    for ev in a_events:
        return_pct = ev["出清獲利%"]
        closed = return_pct is not None

        records.append({
            "分點": ev["分點"],
            "事件類型": "A-單檔權證大買",
            "事件代碼": "A",
            "是否出清": closed,
            "結果": calc_result_tag(return_pct),
            "持有天數": ev["持有天數"],
            "報酬%": return_pct,
        })

    for ev in b_events:
        return_pct = ev["出清獲利%"]
        closed = return_pct is not None

        records.append({
            "分點": ev["分點"],
            "事件類型": "B-同標的單日合計買超",
            "事件代碼": "B",
            "是否出清": closed,
            "結果": calc_result_tag(return_pct),
            "持有天數": ev["持有天數"],
            "報酬%": return_pct,
        })

    for ev in c_events:
        return_pct = ev["出清獲利%"]
        closed = return_pct is not None

        records.append({
            "分點": ev["分點"],
            "事件類型": "C-同標的3日累積買超",
            "事件代碼": "C",
            "是否出清": closed,
            "結果": calc_result_tag(return_pct),
            "持有天數": ev["持有天數"],
            "報酬%": return_pct,
        })

    for ev in d_events:
        return_pct = ev["出清獲利%"]
        closed = return_pct is not None

        records.append({
            "分點": ev["分點"],
            "事件類型": ev.get("事件類型", f"D-近{D_WINDOW_DAYS}日累積淨買進"),
            "事件代碼": "D",
            "是否出清": closed,
            "結果": calc_result_tag(return_pct),
            "持有天數": ev["持有天數"],
            "報酬%": return_pct,
        })

    return records


def calc_summary_for_group(g, broker, event_type):
    total_events = len(g)
    closed_g = g[g["是否出清"] == True]
    open_g = g[g["是否出清"] == False]

    closed_count = len(closed_g)
    open_count = len(open_g)

    win_count = int((closed_g["結果"] == "勝").sum()) if closed_count > 0 else 0
    loss_count = int((closed_g["結果"] == "敗").sum()) if closed_count > 0 else 0
    flat_count = int((closed_g["結果"] == "平手").sum()) if closed_count > 0 else 0

    win_rate = round(win_count / closed_count * 100, 2) if closed_count > 0 else None

    avg_holding_days = None
    if closed_count > 0:
        holding_series = closed_g["持有天數"].dropna()
        if len(holding_series) > 0:
            avg_holding_days = round(float(holding_series.mean()), 2)

    avg_return = None
    max_return = None
    min_return = None

    if closed_count > 0:
        return_series = closed_g["報酬%"].dropna()
        if len(return_series) > 0:
            avg_return = round(float(return_series.mean()), 2)
            max_return = round(float(return_series.max()), 2)
            min_return = round(float(return_series.min()), 2)

    return {
        "分點": broker,
        "事件類型": event_type,
        "事件數": total_events,
        "已出清筆數": closed_count,
        "未出清筆數": open_count,
        "勝筆數": win_count,
        "敗筆數": loss_count,
        "平手筆數": flat_count,
        "勝率": win_rate,
        "平均持有天數": avg_holding_days,
        "平均報酬%": avg_return,
        "最高報酬%": max_return,
        "最低報酬%": min_return,
    }


def calc_empty_summary(broker, event_type):
    return {
        "分點": broker,
        "事件類型": event_type,
        "事件數": 0,
        "已出清筆數": 0,
        "未出清筆數": 0,
        "勝筆數": 0,
        "敗筆數": 0,
        "平手筆數": 0,
        "勝率": None,
        "平均持有天數": None,
        "平均報酬%": None,
        "最高報酬%": None,
        "最低報酬%": None,
    }


def make_summary_map(stat_records):
    if not stat_records:
        stat_df = pd.DataFrame(columns=["分點", "事件代碼", "事件類型", "是否出清", "結果", "持有天數", "報酬%"])
    else:
        stat_df = pd.DataFrame(stat_records)

    broker_order = list(TARGET_PATTERNS.keys())

    if not stat_df.empty:
        for broker in sorted(stat_df["分點"].dropna().unique()):
            if broker not in broker_order:
                broker_order.append(broker)

    summary_map = {}

    event_types = {
        "A": "A-單檔權證大買",
        "B": "B-同標的單日合計買超",
        "C": "C-同標的3日累積買超",
        "D": f"D-近{D_WINDOW_DAYS}日累積淨買進",
        "ALL": "全部-A+B+C+D合併",
    }

    for broker in broker_order:
        summary_map[broker] = {}

        broker_g = stat_df[stat_df["分點"] == broker] if not stat_df.empty else pd.DataFrame()

        for code in ["A", "B", "C", "D"]:
            if not broker_g.empty:
                g = broker_g[broker_g["事件代碼"] == code]
            else:
                g = pd.DataFrame()

            if len(g) > 0:
                summary_map[broker][code] = calc_summary_for_group(g, broker, event_types[code])
            else:
                summary_map[broker][code] = calc_empty_summary(broker, event_types[code])

        if len(broker_g) > 0:
            summary_map[broker]["ALL"] = calc_summary_for_group(broker_g, broker, event_types["ALL"])
        else:
            summary_map[broker]["ALL"] = calc_empty_summary(broker, event_types["ALL"])

    return summary_map, broker_order


def write_stats_sheet(wb, a_events, b_events, c_events, d_events):
    ws = wb.create_sheet("勝率統計")

    headers = [
        "分點",
        "事件類型",
        "事件數",
        "已出清筆數",
        "未出清筆數",
        "勝筆數",
        "敗筆數",
        "平手筆數",
        "勝率",
        "平均持有天數",
        "平均報酬%",
        "最高報酬%",
        "最低報酬%",
    ]

    stat_records = collect_stat_records(a_events, b_events, c_events, d_events)
    summary_map, broker_order = make_summary_map(stat_records)

    thin_gray = Side(style="thin", color="B7B7B7")
    medium_gray = Side(style="medium", color="999999")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
    broker_border = Border(left=medium_gray, right=medium_gray, top=medium_gray, bottom=medium_gray)

    current_row = 1

    for broker in broker_order:
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(headers))
        title_cell = ws.cell(current_row, 1)
        title_cell.value = f"分點：{broker}"
        title_cell.font = Font(bold=True, color="000000", size=12)
        title_cell.fill = GRAY
        title_cell.alignment = Alignment(horizontal="left", vertical="center")
        title_cell.border = broker_border
        ws.row_dimensions[current_row].height = 22

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(current_row, col_idx)
            cell.fill = GRAY
            cell.border = broker_border

        current_row += 1

        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(current_row, col_idx)
            cell.value = header
            cell.font = Font(bold=True, color="000000")
            cell.fill = YELLOW
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws.row_dimensions[current_row].height = 24

        current_row += 1

        for code in ["A", "B", "C", "D", "ALL"]:
            row = summary_map[broker][code]

            values = [
                row["分點"],
                row["事件類型"],
                row["事件數"],
                row["已出清筆數"],
                row["未出清筆數"],
                row["勝筆數"],
                row["敗筆數"],
                row["平手筆數"],
                "-" if row["勝率"] is None else f'{row["勝率"]:.2f}%',
                "-" if row["平均持有天數"] is None else row["平均持有天數"],
                "-" if row["平均報酬%"] is None else f'{row["平均報酬%"]:+.2f}%',
                "-" if row["最高報酬%"] is None else f'{row["最高報酬%"]:+.2f}%',
                "-" if row["最低報酬%"] is None else f'{row["最低報酬%"]:+.2f}%',
            ]

            for col_idx, value in enumerate(values, 1):
                cell = ws.cell(current_row, col_idx)
                cell.value = value
                cell.font = Font(color="000000", bold=True if code == "ALL" else False)
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = normal_border

                if code == "ALL":
                    cell.fill = PatternFill("solid", fgColor="EAF2F8")
                else:
                    cell.fill = WHITE

            ws.row_dimensions[current_row].height = 22
            current_row += 1

        current_row += 1

    col_widths = [16, 24, 10, 12, 12, 10, 10, 10, 10, 14, 12, 12, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A1"


def write_combo_winrate_sheet(wb, a_events, b_events, c_events, d_events):
    """
    新增「ABCD組合勝率」工作表。

    統計邏輯：
    1. 以分點為單位。
    2. 組合包含 AB / AC / AD / BC / BD / CD / ABC / ABD / ACD / BCD / ABCD。
    3. 只有該分點同時具備該組合內所有事件類型，才列入該組合勝率。
    4. 勝率只用「已出清」事件計算，未出清不列入勝敗。

    排版邏輯：
    參考「勝率統計」工作表，每個分點獨立區塊顯示，方便閱讀與截圖。
    """
    ws = wb.create_sheet("ABCD組合勝率")

    headers = [
        "分點",
        "組合",
        "包含事件",
        "是否同時出現",
        "A事件數",
        "B事件數",
        "C事件數",
        "D事件數",
        "組合事件數",
        "已出清筆數",
        "未出清筆數",
        "勝筆數",
        "敗筆數",
        "平手筆數",
        "勝率",
        "平均持有天數",
        "平均報酬%",
        "最高報酬%",
        "最低報酬%",
    ]

    stat_records = collect_stat_records(a_events, b_events, c_events, d_events)

    if not stat_records:
        stat_df = pd.DataFrame(columns=[
            "分點", "事件代碼", "事件類型", "是否出清",
            "結果", "持有天數", "報酬%"
        ])
    else:
        stat_df = pd.DataFrame(stat_records)

    broker_order = list(TARGET_PATTERNS.keys())

    if not stat_df.empty:
        for broker in sorted(stat_df["分點"].dropna().unique()):
            if broker not in broker_order:
                broker_order.append(broker)

    combo_defs = [
        ("AB",   ["A", "B"]),
        ("AC",   ["A", "C"]),
        ("AD",   ["A", "D"]),
        ("BC",   ["B", "C"]),
        ("BD",   ["B", "D"]),
        ("CD",   ["C", "D"]),
        ("ABC",  ["A", "B", "C"]),
        ("ABD",  ["A", "B", "D"]),
        ("ACD",  ["A", "C", "D"]),
        ("BCD",  ["B", "C", "D"]),
        ("ABCD", ["A", "B", "C", "D"]),
    ]

    thin_gray = Side(style="thin", color="B7B7B7")
    medium_gray = Side(style="medium", color="999999")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
    broker_border = Border(left=medium_gray, right=medium_gray, top=medium_gray, bottom=medium_gray)

    current_row = 1

    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(headers))
    title_cell = ws.cell(current_row, 1)
    title_cell.value = "ABCD 所有組合勝率統計（依分點）"
    title_cell.font = Font(bold=True, color="000000", size=14)
    title_cell.fill = YELLOW
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    title_cell.border = normal_border
    ws.row_dimensions[current_row].height = 28

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(current_row, col_idx)
        cell.fill = YELLOW
        cell.border = normal_border

    current_row += 1

    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(headers))
    note_cell = ws.cell(current_row, 1)
    note_cell.value = "統計邏輯：只有該分點同時具備該組合內所有事件類型，才列入該組合勝率；勝率只用「已出清」事件計算，未出清不列入勝敗。"
    note_cell.font = Font(color="666666")
    note_cell.fill = WHITE
    note_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    note_cell.border = normal_border
    ws.row_dimensions[current_row].height = 24

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(current_row, col_idx)
        cell.fill = WHITE
        cell.border = normal_border

    current_row += 2

    for broker in broker_order:
        if stat_df.empty:
            broker_g = pd.DataFrame(columns=stat_df.columns)
        else:
            broker_g = stat_df[stat_df["分點"] == broker].copy()

        event_counts = {}
        for code in ["A", "B", "C", "D"]:
            if broker_g.empty:
                event_counts[code] = 0
            else:
                event_counts[code] = int((broker_g["事件代碼"] == code).sum())

        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(headers))
        title_cell = ws.cell(current_row, 1)
        title_cell.value = f"分點：{broker}"
        title_cell.font = Font(bold=True, color="000000", size=12)
        title_cell.fill = GRAY
        title_cell.alignment = Alignment(horizontal="left", vertical="center")
        title_cell.border = broker_border
        ws.row_dimensions[current_row].height = 22

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(current_row, col_idx)
            cell.fill = GRAY
            cell.border = broker_border

        current_row += 1

        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(current_row, col_idx)
            cell.value = header
            cell.font = Font(bold=True, color="000000")
            cell.fill = YELLOW
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws.row_dimensions[current_row].height = 24

        current_row += 1

        for combo_name, combo_codes in combo_defs:
            has_all = all(event_counts.get(code, 0) > 0 for code in combo_codes)
            include_text = " + ".join(combo_codes)

            if has_all and not broker_g.empty:
                combo_g = broker_g[broker_g["事件代碼"].isin(combo_codes)].copy()

                combo_event_count = len(combo_g)
                closed_g = combo_g[combo_g["是否出清"] == True]
                open_g = combo_g[combo_g["是否出清"] == False]

                closed_count = len(closed_g)
                open_count = len(open_g)

                win_count = int((closed_g["結果"] == "勝").sum()) if closed_count > 0 else 0
                loss_count = int((closed_g["結果"] == "敗").sum()) if closed_count > 0 else 0
                flat_count = int((closed_g["結果"] == "平手").sum()) if closed_count > 0 else 0

                win_rate = round(win_count / closed_count * 100, 2) if closed_count > 0 else None

                avg_holding_days = None
                if closed_count > 0:
                    holding_series = pd.to_numeric(closed_g["持有天數"], errors="coerce").dropna()
                    if len(holding_series) > 0:
                        avg_holding_days = round(float(holding_series.mean()), 2)

                avg_return = None
                max_return = None
                min_return = None
                if closed_count > 0:
                    return_series = pd.to_numeric(closed_g["報酬%"], errors="coerce").dropna()
                    if len(return_series) > 0:
                        avg_return = round(float(return_series.mean()), 2)
                        max_return = round(float(return_series.max()), 2)
                        min_return = round(float(return_series.min()), 2)

                row_values = [
                    broker,
                    combo_name,
                    include_text,
                    "是",
                    event_counts["A"],
                    event_counts["B"],
                    event_counts["C"],
                    event_counts["D"],
                    combo_event_count,
                    closed_count,
                    open_count,
                    win_count,
                    loss_count,
                    flat_count,
                    "-" if win_rate is None else f"{win_rate:.2f}%",
                    "-" if avg_holding_days is None else avg_holding_days,
                    "-" if avg_return is None else f"{avg_return:+.2f}%",
                    "-" if max_return is None else f"{max_return:+.2f}%",
                    "-" if min_return is None else f"{min_return:+.2f}%",
                ]
            else:
                row_values = [
                    broker,
                    combo_name,
                    include_text,
                    "否",
                    event_counts["A"],
                    event_counts["B"],
                    event_counts["C"],
                    event_counts["D"],
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                ]

            for col_idx, value in enumerate(row_values, 1):
                cell = ws.cell(current_row, col_idx)
                cell.value = value
                cell.font = Font(color="000000")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = normal_border

                if col_idx == 4:
                    if value == "是":
                        cell.fill = PatternFill("solid", fgColor="EAF2F8")
                    else:
                        cell.fill = GRAY
                else:
                    cell.fill = WHITE

            ws.row_dimensions[current_row].height = 22
            current_row += 1

        current_row += 1

    col_widths = [16, 8, 14, 14, 10, 10, 10, 10, 12, 12, 12, 10, 10, 10, 10, 14, 12, 12, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A4"

def fmt_ratio_value(numerator, denominator):
    try:
        numerator = float(numerator)
        denominator = float(denominator)
        if denominator == 0:
            return "-"
        return f"{(numerator / denominator * 100):.2f}%"
    except:
        return "-"


def collect_recent_trade_date_sets(items, cutoff_dt):
    trade_dates = set()

    for item in items:
        df = item["df"]

        for _, row in df.iterrows():
            trade_dt = parse_date(row["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_dates.add(normalize_date_str(row["日期"]))

    sorted_dates = sorted(trade_dates)
    last_5_dates = set(sorted_dates[-5:])
    last_20_dates = set(sorted_dates[-20:])

    return last_5_dates, last_20_dates


def write_recent_warrant_amount_ranking_sheet(wb, items):
    ws = wb.create_sheet("近兩月買賣金額排行")

    headers = [
        "排名",
        "權證代號",
        "權證名稱",
        "標的股",
        "標的名稱",
        "買進金額",
        "賣出金額",
        "淨買進金額",
        "近20日淨買進金額",
        "近5日淨買進金額",
        "近20日占比",
        "近5日占比",
        "買進分點",
        "最近買進日",
    ]

    ws.append(headers)

    cutoff_dt = datetime.today() - timedelta(days=RECENT_RANKING_DAYS)
    last_5_dates, last_20_dates = collect_recent_trade_date_sets(items, cutoff_dt)
    ranking_map = {}

    for item in items:
        warrant_code = item["warrant_code"]
        warrant_name = item["warrant_name"]
        underlying_code = item["underlying_code"]
        underlying_name = item.get("underlying_name", "")
        broker_label = item["broker_label"]

        df = item["df"]

        for _, row in df.iterrows():
            trade_dt = parse_date(row["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_date_str = normalize_date_str(row["日期"])
            buy_amount = int(row["買進金額"])
            sell_amount = int(row["賣出金額"])

            if buy_amount <= 0 and sell_amount <= 0:
                continue

            key = (warrant_code, warrant_name, underlying_code, underlying_name)

            if key not in ranking_map:
                ranking_map[key] = {
                    "權證代號": warrant_code,
                    "權證名稱": warrant_name,
                    "標的股": underlying_code,
                    "標的名稱": underlying_name,
                    "買進金額": 0,
                    "賣出金額": 0,
                    "淨買進金額": 0,
                    "近20日淨買進金額": 0,
                    "近5日淨買進金額": 0,
                    "分點買進金額": {},
                    "分點賣出金額": {},
                    "最近買進日": "",
                }

            rec = ranking_map[key]
            net_amount = buy_amount - sell_amount

            rec["買進金額"] += buy_amount
            rec["賣出金額"] += sell_amount
            rec["淨買進金額"] += net_amount

            if trade_date_str in last_20_dates:
                rec["近20日淨買進金額"] += net_amount

            if trade_date_str in last_5_dates:
                rec["近5日淨買進金額"] += net_amount

            if buy_amount > 0:
                rec["分點買進金額"][broker_label] = rec["分點買進金額"].get(broker_label, 0) + buy_amount
                if not rec["最近買進日"] or trade_date_str > rec["最近買進日"]:
                    rec["最近買進日"] = trade_date_str

            if sell_amount > 0:
                rec["分點賣出金額"][broker_label] = rec["分點賣出金額"].get(broker_label, 0) + sell_amount

    ranking_rows = [
        rec for rec in ranking_map.values()
        if rec["淨買進金額"] > 0
    ]

    ranking_rows = sorted(
        ranking_rows,
        key=lambda x: x["淨買進金額"],
        reverse=True
    )[:20]

    for rank, rec in enumerate(ranking_rows, 1):
        broker_net_rows = []

        for broker, buy_amount in rec["分點買進金額"].items():
            sell_amount = rec["分點賣出金額"].get(broker, 0)
            net_amount = buy_amount - sell_amount

            if net_amount > 0:
                broker_net_rows.append((broker, net_amount))

        broker_net_rows = sorted(
            broker_net_rows,
            key=lambda x: x[1],
            reverse=True
        )

        if broker_net_rows:
            broker_text = "；".join([f"{broker}({fmt_amount(amount)})" for broker, amount in broker_net_rows])
        else:
            broker_text = "-"

        ws.append([
            rank,
            rec["權證代號"],
            rec["權證名稱"],
            rec["標的股"],
            rec["標的名稱"] or "-",
            fmt_amount(rec["買進金額"]),
            fmt_amount(rec["賣出金額"]),
            fmt_amount(rec["淨買進金額"]),
            fmt_amount(rec["近20日淨買進金額"]),
            fmt_amount(rec["近5日淨買進金額"]),
            fmt_ratio_value(rec["近20日淨買進金額"], rec["淨買進金額"]),
            fmt_ratio_value(rec["近5日淨買進金額"], rec["淨買進金額"]),
            broker_text,
            rec["最近買進日"] or "-",
        ])

    col_widths = [8, 12, 24, 10, 12, 16, 16, 18, 18, 18, 12, 12, 70, 14]

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws.row_dimensions[row[0].row].height = 36

    ws.freeze_panes = "A2"


def write_underlying_broker_count_ranking_sheet(wb, items):
    ws = wb.create_sheet("近兩月分點數排行")

    headers = [
        "排名",
        "標的股",
        "標的名稱",
        "權證檔數",
        "買進分點數",
        "買進分點清單",
        "近兩月買進金額",
        "近兩月賣出金額",
        "近兩月淨買進金額",
        "近20日淨買進金額",
        "近5日淨買進金額",
        "近20日占比",
        "近5日占比",
        "最近買進日",
    ]

    ws.append(headers)

    cutoff_dt = datetime.today() - timedelta(days=RECENT_RANKING_DAYS)
    last_5_dates, last_20_dates = collect_recent_trade_date_sets(items, cutoff_dt)
    underlying_map = {}

    for item in items:
        warrant_code = item["warrant_code"]
        underlying_code = item["underlying_code"]
        underlying_name = item.get("underlying_name", "")
        broker_label = item["broker_label"]

        if not underlying_code:
            continue

        df = item["df"]

        for _, row in df.iterrows():
            trade_dt = parse_date(row["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_date_str = normalize_date_str(row["日期"])
            buy_amount = int(row["買進金額"])
            sell_amount = int(row["賣出金額"])
            buy_shares = int(row["買進股數"])
            sell_shares = int(row["賣出股數"])

            if buy_amount <= 0 and sell_amount <= 0:
                continue

            if underlying_code not in underlying_map:
                underlying_map[underlying_code] = {
                    "標的股": underlying_code,
                    "標的名稱": underlying_name,
                    "權證集合": set(),
                    "分點資料": {},
                }

            rec = underlying_map[underlying_code]
            if not rec["標的名稱"] and underlying_name:
                rec["標的名稱"] = underlying_name

            broker_rec = rec["分點資料"].setdefault(broker_label, {
                "買進金額": 0,
                "賣出金額": 0,
                "淨買進金額": 0,
                "近20日淨買進金額": 0,
                "近5日淨買進金額": 0,
                "買進股數": 0,
                "賣出股數": 0,
                "淨買進股數": 0,
                "最近買進日": "",
                "權證集合": set(),
            })

            net_amount = buy_amount - sell_amount
            net_shares = buy_shares - sell_shares

            broker_rec["權證集合"].add(warrant_code)
            broker_rec["買進金額"] += buy_amount
            broker_rec["賣出金額"] += sell_amount
            broker_rec["淨買進金額"] += net_amount
            broker_rec["買進股數"] += buy_shares
            broker_rec["賣出股數"] += sell_shares
            broker_rec["淨買進股數"] += net_shares

            if trade_date_str in last_20_dates:
                broker_rec["近20日淨買進金額"] += net_amount

            if trade_date_str in last_5_dates:
                broker_rec["近5日淨買進金額"] += net_amount

            if buy_amount > 0:
                if not broker_rec["最近買進日"] or trade_date_str > broker_rec["最近買進日"]:
                    broker_rec["最近買進日"] = trade_date_str

    ranking_rows = []

    for rec in underlying_map.values():
        active_broker_rows = []
        active_warrants = set()
        active_buy_amount = 0
        active_sell_amount = 0
        active_net_amount = 0
        active_20_net_amount = 0
        active_5_net_amount = 0
        latest_buy_date = ""

        for broker, broker_rec in rec["分點資料"].items():
            # 分點若已出清，淨買進股數 <= 0，不列入分點數排行與金額統計。
            if broker_rec["淨買進股數"] <= 0:
                continue

            # 若金額面也沒有正向淨買進，避免已賣出獲利但零庫存的分點被列入。
            if broker_rec["淨買進金額"] <= 0:
                continue

            active_broker_rows.append((broker, broker_rec["淨買進金額"]))
            active_warrants.update(broker_rec["權證集合"])
            active_buy_amount += broker_rec["買進金額"]
            active_sell_amount += broker_rec["賣出金額"]
            active_net_amount += broker_rec["淨買進金額"]
            active_20_net_amount += broker_rec["近20日淨買進金額"]
            active_5_net_amount += broker_rec["近5日淨買進金額"]

            if broker_rec["最近買進日"] and (not latest_buy_date or broker_rec["最近買進日"] > latest_buy_date):
                latest_buy_date = broker_rec["最近買進日"]

        if active_net_amount <= 0:
            continue

        if not active_broker_rows:
            continue

        active_broker_rows = sorted(
            active_broker_rows,
            key=lambda x: x[1],
            reverse=True
        )

        rec["權證檔數"] = len(active_warrants)
        rec["買進分點數"] = len(active_broker_rows)
        rec["買進分點清單"] = "；".join([f"{broker}({fmt_amount(amount)})" for broker, amount in active_broker_rows])
        rec["近兩月買進金額"] = active_buy_amount
        rec["近兩月賣出金額"] = active_sell_amount
        rec["近兩月淨買進金額"] = active_net_amount
        rec["近20日淨買進金額"] = active_20_net_amount
        rec["近5日淨買進金額"] = active_5_net_amount
        rec["最近買進日"] = latest_buy_date

        ranking_rows.append(rec)

    ranking_rows = sorted(
        ranking_rows,
        key=lambda x: (x["買進分點數"], x["近兩月淨買進金額"]),
        reverse=True
    )[:20]

    for rank, rec in enumerate(ranking_rows, 1):
        ws.append([
            rank,
            rec["標的股"],
            rec["標的名稱"] or "-",
            rec["權證檔數"],
            rec["買進分點數"],
            rec["買進分點清單"],
            fmt_amount(rec["近兩月買進金額"]),
            fmt_amount(rec["近兩月賣出金額"]),
            fmt_amount(rec["近兩月淨買進金額"]),
            fmt_amount(rec["近20日淨買進金額"]),
            fmt_amount(rec["近5日淨買進金額"]),
            fmt_ratio_value(rec["近20日淨買進金額"], rec["近兩月淨買進金額"]),
            fmt_ratio_value(rec["近5日淨買進金額"], rec["近兩月淨買進金額"]),
            rec["最近買進日"] or "-",
        ])

    col_widths = [8, 10, 12, 10, 12, 70, 16, 16, 18, 18, 18, 12, 12, 14]

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws.row_dimensions[row[0].row].height = 36

    ws.freeze_panes = "A2"



def write_broker_query_sheet(wb, items):
    """
    新增「券商查詢」工作表：
    可用下拉選單選擇券商，並顯示該券商目前「標的股」相關權證總淨買進金額前 15 名。
    不是單一權證排名，而是同一券商買進同一標的股底下所有相關認購權證的合計排名。

    排名邏輯改回：淨買進金額由高到低排序。
    淨買進金額 = 買進金額 - 賣出金額，代表扣掉賣出後仍留在裡面的推估金額。
    這版不使用 FILTER / SORTBY，改為先在隱藏工作表預先整理每個券商前 15 名，
    再用 INDEX / MATCH 查詢，提高 Excel 相容性。
    """
    data_ws = wb.create_sheet("券商查詢資料")
    data_ws.sheet_state = "hidden"

    data_headers = [
        "分點",
        "排名",
        "標的股",
        "標的名稱",
        "權證檔數",
        "權證清單",
        "買進金額",
        "賣出金額",
        "淨買進金額",
        "買進張數",
        "賣出張數",
        "淨買進張數",
        "最近買進日",
        "查詢鍵",
    ]
    data_ws.append(data_headers)

    broker_underlying_map = {}

    for item in items:
        broker_label = item["broker_label"]
        warrant_code = item["warrant_code"]
        warrant_name = item["warrant_name"]
        underlying_code = item["underlying_code"]
        underlying_name = item.get("underlying_name", "")

        if not underlying_code:
            continue

        df = item["df"]

        buy_amount = int(df["買進金額"].sum()) if not df.empty else 0
        sell_amount = int(df["賣出金額"].sum()) if not df.empty else 0
        buy_shares = int(df["買進股數"].sum()) if not df.empty else 0
        sell_shares = int(df["賣出股數"].sum()) if not df.empty else 0

        if buy_amount <= 0 and sell_amount <= 0:
            continue

        net_amount = buy_amount - sell_amount
        net_shares = buy_shares - sell_shares

        latest_buy_date = ""
        if not df.empty:
            buy_dates = df[df["買進金額"] > 0]["日期"].dropna().tolist()
            if buy_dates:
                latest_buy_date = max([normalize_date_str(d) for d in buy_dates])

        key = (broker_label, underlying_code)

        if key not in broker_underlying_map:
            broker_underlying_map[key] = {
                "分點": broker_label,
                "標的股": underlying_code,
                "標的名稱": underlying_name,
                "權證集合": set(),
                "權證清單": [],
                "買進金額": 0,
                "賣出金額": 0,
                "淨買進金額": 0,
                "買進股數": 0,
                "賣出股數": 0,
                "淨買進股數": 0,
                "最近買進日": "",
            }

        rec = broker_underlying_map[key]

        if not rec["標的名稱"] and underlying_name:
            rec["標的名稱"] = underlying_name

        rec["權證集合"].add(warrant_code)

        warrant_label = f"{warrant_code} {warrant_name}"
        if warrant_label not in rec["權證清單"]:
            rec["權證清單"].append(warrant_label)

        rec["買進金額"] += buy_amount
        rec["賣出金額"] += sell_amount
        rec["淨買進金額"] += net_amount
        rec["買進股數"] += buy_shares
        rec["賣出股數"] += sell_shares
        rec["淨買進股數"] += net_shares

        if latest_buy_date and (not rec["最近買進日"] or latest_buy_date > rec["最近買進日"]):
            rec["最近買進日"] = latest_buy_date

    broker_map = {}

    for rec in broker_underlying_map.values():
        # 券商查詢主排序改回「淨買進金額」由高到低，
        # 但列入條件必須同時符合：
        # 1. 買進金額 > 0：代表這個券商確實有買這個標的底下的權證
        # 2. 淨買進金額 > 0：避免買很多但賣更多、實際已轉為淨賣出的標的進榜
        # 3. 淨買進股數 > 0：避免金額面為正但張數面已經接近或完全出清
        #
        # 這樣比較符合「主力還留著沒賣的總金額」這個籌碼意圖，
        # 同時也不會出現淨買進金額為負的標的排在前面。
        if rec["買進金額"] <= 0:
            continue

        if rec["淨買進金額"] <= 0:
            continue

        if rec["淨買進股數"] <= 0:
            continue

        broker_label = rec["分點"]
        broker_map.setdefault(broker_label, []).append(rec)

    broker_order = list(TARGET_PATTERNS.keys())
    active_brokers = sorted(broker_map.keys())

    broker_list = []
    for broker in broker_order:
        if broker in active_brokers and broker not in broker_list:
            broker_list.append(broker)

    for broker in active_brokers:
        if broker not in broker_list:
            broker_list.append(broker)

    for broker in broker_list:
        rows = sorted(
            broker_map.get(broker, []),
            key=lambda x: (x["淨買進金額"], x["最近買進日"], x["買進金額"], x["標的股"]),
            reverse=True
        )[:15]

        for rank, rec in enumerate(rows, 1):
            warrant_list = sorted(rec["權證清單"])

            data_ws.append([
                broker,
                rank,
                rec["標的股"],
                rec["標的名稱"] or "-",
                len(rec["權證集合"]),
                "；".join(warrant_list),
                rec["買進金額"],
                rec["賣出金額"],
                rec["淨買進金額"],
                rec["買進股數"] // 1000,
                rec["賣出股數"] // 1000,
                rec["淨買進股數"] // 1000,
                rec["最近買進日"],
                f"{broker}|{rank}",
            ])

    broker_start_col = 16  # P 欄
    data_ws.cell(1, broker_start_col).value = "券商清單"

    for idx, broker in enumerate(broker_list, 2):
        data_ws.cell(idx, broker_start_col).value = broker

    for col_idx, width in enumerate([16, 8, 10, 12, 10, 70, 16, 16, 18, 12, 12, 12, 14, 24], 1):
        data_ws.column_dimensions[get_column_letter(col_idx)].width = width

    data_ws.column_dimensions[get_column_letter(broker_start_col)].width = 18

    ws = wb.create_sheet("券商查詢")

    ws["A1"] = "券商標的股淨買進金額前 15 名查詢"
    ws.merge_cells("A1:M1")
    ws["A1"].font = Font(bold=True, size=14, color="000000")
    ws["A1"].fill = YELLOW
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    ws["A2"] = "選擇券商"
    ws["A2"].font = Font(bold=True, color="000000")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws["A2"].fill = GRAY

    ws["B2"] = broker_list[0] if broker_list else ""
    ws["B2"].font = Font(bold=True, color="000000")
    ws["B2"].alignment = Alignment(horizontal="center", vertical="center")
    ws["B2"].fill = PatternFill("solid", fgColor="EAF2F8")

    ws["D2"] = "排序邏輯：同一券商買進同一標的股底下所有相關權證合計；先排除淨買進金額<=0或淨買進張數<=0，再依淨買進金額由高到低排序。"
    ws.merge_cells("D2:M2")
    ws["D2"].font = Font(color="666666")
    ws["D2"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    if broker_list:
        broker_formula = f"'券商查詢資料'!$P$2:$P${len(broker_list) + 1}"
        dv = DataValidation(type="list", formula1=broker_formula, allow_blank=False)
        ws.add_data_validation(dv)
        dv.add(ws["B2"])

    headers = [
        "排名",
        "標的股",
        "標的名稱",
        "權證檔數",
        "權證清單",
        "買進金額",
        "賣出金額",
        "淨買進金額",
        "買進張數",
        "賣出張數",
        "淨買進張數",
        "最近買進日",
    ]

    header_row = 5

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(header_row, col_idx)
        cell.value = header
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    max_data_row = max(data_ws.max_row, 2)

    for i in range(15):
        row_idx = header_row + 1 + i
        rank = i + 1

        ws.cell(row_idx, 1).value = rank

        for col_idx in range(2, 13):
            data_col_idx = col_idx + 1
            data_col_letter = get_column_letter(data_col_idx)

            formula = (
                f'=IFERROR(INDEX(券商查詢資料!${data_col_letter}$2:${data_col_letter}${max_data_row},'
                f'MATCH($B$2&"|"&$A{row_idx},券商查詢資料!$N$2:$N${max_data_row},0)),"")'
            )
            ws.cell(row_idx, col_idx).value = formula

    col_widths = [8, 10, 12, 10, 70, 16, 16, 18, 12, 12, 12, 14]

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for row in ws.iter_rows(min_row=1, max_row=header_row + 15, min_col=1, max_col=12):
        for cell in row:
            cell.border = normal_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if cell.row > header_row:
                cell.font = Font(color="000000")

    for row_idx in range(header_row + 1, header_row + 16):
        ws.row_dimensions[row_idx].height = 32

    ws.row_dimensions[2].height = 24
    ws.freeze_panes = "A6"

    try:
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
    except:
        pass


def write_price_status_sheet(wb, price_cache):
    ws = wb.create_sheet("價格抓取狀態")

    headers = [
        "代號",
        "價格筆數",
        "第一筆日期",
        "最後筆日期",
        "狀態",
    ]

    ws.append(headers)

    rows = []

    seen = set()

    for code, prices in price_cache.items():
        code = str(code).strip()

        if not code or code in seen:
            continue

        seen.add(code)

        valid_dates = sorted([d for d, p in prices.items() if p is not None and p > 0])

        if valid_dates:
            rows.append([
                code,
                len(valid_dates),
                valid_dates[0],
                valid_dates[-1],
                "OK",
            ])
        else:
            rows.append([
                code,
                0,
                "-",
                "-",
                "NO DATA",
            ])

    rows = sorted(rows, key=lambda x: (x[4], x[0]))

    for row in rows:
        ws.append(row)

    col_widths = [12, 12, 14, 14, 12]

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border

            if cell.column == 5 and cell.value == "NO DATA":
                cell.fill = GREEN

        ws.row_dimensions[row[0].row].height = 20

    ws.freeze_panes = "A2"


def apply_global_amount_comma_format(wb):
    """
    全工作表套用金額千分位格式。

    只要欄位名稱包含「金額」，例如：
    買進金額、賣出金額、淨買進金額、近20日淨買進金額、買超金額，
    就統一顯示為 1,234,567。

    不處理「均價」、「比例」、「獲利%」等欄位，避免小數或百分比被誤改。
    """
    for ws in wb.worksheets:
        # 掃前 10 列，因為大部分工作表標題在第 1 列，
        # 「券商查詢」的欄位標題在第 5 列。
        for header_row in range(1, min(ws.max_row, 10) + 1):
            amount_cols = []

            for col_idx in range(1, ws.max_column + 1):
                header_value = ws.cell(header_row, col_idx).value
                header = str(header_value).strip() if header_value is not None else ""

                if not header:
                    continue

                if "金額" in header:
                    # 排除非金額欄位，避免誤格式化
                    if "%" in header or "比例" in header or "均價" in header:
                        continue

                    amount_cols.append(col_idx)

            if not amount_cols:
                continue

            for col_idx in amount_cols:
                for row_idx in range(header_row + 1, ws.max_row + 1):
                    cell = ws.cell(row_idx, col_idx)

                    if cell.value is None or cell.value == "":
                        continue

                    # 公式欄位不動，只套顯示格式
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        cell.number_format = '#,##0'
                        continue

                    if str(cell.value).strip() == "-":
                        continue

                    try:
                        raw = str(cell.value).replace(",", "").strip()

                        if raw.startswith("-"):
                            numeric_part = raw[1:]
                        else:
                            numeric_part = raw

                        if numeric_part.replace(".", "", 1).isdigit():
                            num = float(raw)

                            if num.is_integer():
                                cell.value = int(num)
                                cell.number_format = '#,##0'
                            else:
                                cell.value = num
                                cell.number_format = '#,##0.00'

                    except:
                        pass


def write_color_legend_sheet(wb):
    ws = wb.create_sheet("顏色說明")

    headers = ["色塊", "顏色用途", "出現位置", "說明"]
    ws.append(headers)

    legend_rows = [
        {
            "fill": YELLOW,
            "用途": "標頭 / 欄位名稱",
            "位置": "所有工作表",
            "說明": "用於表格欄位名稱與重要標題列。",
        },
        {
            "fill": RED,
            "用途": "勝 / 上漲",
            "位置": "A、B、C、D 工作表的 D+1～D+20 欄位",
            "說明": "代表該追蹤日為正報酬或上漲狀態；依台股習慣使用紅色表示上漲。",
        },
        {
            "fill": GREEN,
            "用途": "敗 / 下跌或未上漲",
            "位置": "A、B、C、D 工作表的 D+1～D+20 欄位",
            "說明": "代表該追蹤日為負報酬、下跌或未形成正報酬；依台股習慣使用綠色表示下跌。",
        },
        {
            "fill": BLUE,
            "用途": "減碼",
            "位置": "A、B、C、D 工作表的 D+1～D+20 欄位",
            "說明": "代表程式偵測到該分點後續有賣出，但尚未完全出清。",
        },
        {
            "fill": ORANGE,
            "用途": "出清",
            "位置": "A、B、C、D 工作表的 D+1～D+20 欄位",
            "說明": "代表依 FIFO 推估，該筆事件已經被完全賣出。",
        },
        {
            "fill": WHITE,
            "border": "profit",
            "用途": "粗紅色外框",
            "位置": "A、B、C、D 工作表的「減碼獲利% / 出清獲利%」欄位",
            "說明": "代表該筆事件已減碼或出清，且實際獲利% > 0。",
        },
        {
            "fill": WHITE,
            "border": "loss",
            "用途": "粗綠色外框",
            "位置": "A、B、C、D 工作表的「減碼獲利% / 出清獲利%」欄位",
            "說明": "代表該筆事件已減碼或出清，且實際獲利% < 0。",
        },
        {
            "fill": GRAY,
            "用途": "分點區隔列",
            "位置": "勝率統計工作表",
            "說明": "用於區隔不同分點，讓每個分點的統計區塊更清楚。",
        },
        {
            "fill": PatternFill("solid", fgColor="EAF2F8"),
            "用途": "全部-A+B+C+D合併",
            "位置": "勝率統計工作表",
            "說明": "代表該分點 A、B、C、D 四類事件合併後的統計列。",
        },
        {
            "fill": WHITE,
            "用途": "一般資料列",
            "位置": "勝率統計工作表",
            "說明": "一般事件類型統計列，沒有特殊狀態標記。",
        },
    ]

    for row_info in legend_rows:
        ws.append(["", row_info["用途"], row_info["位置"], row_info["說明"]])
        row_idx = ws.max_row
        ws.cell(row_idx, 1).fill = row_info["fill"]

        if row_info.get("border"):
            side = PROFIT_EXIT_SIDE if row_info.get("border") == "profit" else LOSS_EXIT_SIDE
            ws.cell(row_idx, 1).border = Border(
                left=side,
                right=side,
                top=side,
                bottom=side,
            )

    col_widths = [12, 24, 34, 70]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    for row in ws.iter_rows(min_row=2):
        legend_name = str(row[1].value).strip()
        is_profit_outline_legend = legend_name == "粗紅色外框"
        is_loss_outline_legend = legend_name == "粗綠色外框"

        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            if is_profit_outline_legend and cell.column == 1:
                cell.border = Border(
                    left=PROFIT_EXIT_SIDE,
                    right=PROFIT_EXIT_SIDE,
                    top=PROFIT_EXIT_SIDE,
                    bottom=PROFIT_EXIT_SIDE,
                )
            elif is_loss_outline_legend and cell.column == 1:
                cell.border = Border(
                    left=LOSS_EXIT_SIDE,
                    right=LOSS_EXIT_SIDE,
                    top=LOSS_EXIT_SIDE,
                    bottom=LOSS_EXIT_SIDE,
                )
            else:
                cell.border = normal_border

        ws.row_dimensions[row[0].row].height = 36

    ws.freeze_panes = "A2"


def build_excel(a_events, b_events, c_events, d_events, item_map, price_cache, items, output_path):
    print("【Step 5】建立 Excel...")

    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    write_a_sheet(wb, a_events, item_map, price_cache)
    write_group_sheet(wb, "B_同標的單日合計", b_events, price_cache, is_c=False)
    write_group_sheet(wb, "C_同標的3日累積", c_events, price_cache, is_c=True)
    write_group_sheet(wb, f"D_近{D_WINDOW_DAYS}日累積淨買進", d_events, price_cache, is_c=True)
    write_stats_sheet(wb, a_events, b_events, c_events, d_events)
    write_recent_warrant_amount_ranking_sheet(wb, items)
    write_underlying_broker_count_ranking_sheet(wb, items)
    write_broker_query_sheet(wb, items)
    write_price_status_sheet(wb, price_cache)
    write_color_legend_sheet(wb)
    write_combo_winrate_sheet(wb, a_events, b_events, c_events, d_events)

    apply_global_amount_comma_format(wb)

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    wb.save(output_path)

    print(
        f"  ✅ 已存：{output_path} "
        f"（A:{len(a_events)} 筆，B:{len(b_events)} 筆，C:{len(c_events)} 筆，D:{len(d_events)} 筆）"
    )


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

def main():
    _GROUP_OUTCOME_SALE_ROWS_CACHE.clear()
    program_start = time.time()

    today_fn = datetime.today().strftime("%Y%m%d")
    output_path = os.path.join(OUTPUT_DIR, f"warrant_backtest_ABCD_{today_fn}.xlsx")

    print(f"\n認購權證特定分點買超回測 ABCD 版 | {today_fn}")
    print(f"A：單檔權證買進金額 >= {AMOUNT_THRESH // 10000}萬")
    print(f"B：同分點 + 同標的 + 單日多檔權證合計買超 >= {AMOUNT_THRESH // 10000}萬")
    print(f"C：同分點 + 同標的 + 連續3交易日多檔權證累積買超 >= {AMOUNT_THRESH // 10000}萬")
    print(f"D：同分點 + 同標的 + 近{D_WINDOW_DAYS}交易日累積淨買進 >= {AMOUNT_THRESH // 10000}萬")
    print(f"分點數：{len(TARGET_PATTERNS)} 個")
    print(f"加速模式：FAST_SKIP_RECENT_PRESCAN={FAST_SKIP_RECENT_PRESCAN}，FETCH_GROUP_WARRANT_PRICES={FETCH_GROUP_WARRANT_PRICES}")
    print("=" * 70)

    warrants = get_all_call_warrants()

    if not warrants:
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    broker_map = find_broker_codes(warrants)

    if not broker_map:
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    candidates = prescan_all(warrants, broker_map)

    if not candidates:
        print("⚠️ 預篩後無候選")
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    print(f"\n【Step 3b】處理 {len(candidates)} 組候選...")

    candidate_keys = {candidate_key_from_tuple(c) for c in candidates}
    history_cache_df = load_history_cache()
    history_keys = history_cache_keys(history_cache_df)

    cached_items = items_from_history_cache(history_cache_df, candidate_filter=candidate_keys)

    if cached_items:
        print(f"  ✅ 已從原始分點資料快取還原 {len(cached_items)} 組資料")

    candidates_to_fetch = []

    for c in candidates:
        key = candidate_key_from_tuple(c)

        # 沒有歷史快取：一定要抓
        # 最近 prescan 有看到目標分點：重新抓 API5，補進最新資料
        if key not in history_keys or key in PRESCAN_REFRESH_KEYS:
            candidates_to_fetch.append(c)

    print(f"  ✅ 快取已有候選：{len(history_keys & candidate_keys)} 組")
    print(f"  ✅ 本次需要 API5 更新：{len(candidates_to_fetch)} 組")

    fetched_items = []
    done = 0

    if candidates_to_fetch:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {
                ex.submit(process_candidate, *c): c
                for c in candidates_to_fetch
            }

            for future in as_completed(futures):
                done += 1

                try:
                    item = future.result()
                except:
                    item = None

                if item:
                    fetched_items.append(item)

                if done % 100 == 0:
                    print(f"  [{done:,}/{len(candidates_to_fetch):,}] 已更新，成功取得 {len(fetched_items)} 組資料")
    else:
        print("  ✅ 所有候選組合皆可使用快取，略過 API5 歷史資料重抓")

    if fetched_items:
        history_cache_df = merge_items_into_history_cache(history_cache_df, fetched_items)
        save_history_cache(history_cache_df)

    items = items_from_history_cache(history_cache_df, candidate_filter=candidate_keys)

    if not items and fetched_items:
        items = fetched_items

    if not items:
        print("⚠️ 無任何候選資料")
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    item_map = {}
    a_events = []

    for item in items:
        item_map[(item["broker_code"], item["warrant_code"])] = item
        a_events.extend(item.get("events_a", []))

    # A 類內部先做權證代號唯一化：
    # 同一檔權證若有多筆 A 事件，只保留買進日較新、同日買進金額較大的那筆。
    a_events = filter_a_events_unique_warrants(a_events)

    daily_records = build_daily_records(items)

    # A > B > C > D 權證代號層級互斥：
    # 只要同一檔權證已經進入 A，後續 B / C / D 完全不再使用這檔權證。
    a_warrant_codes = collect_event_warrant_codes(a_events)
    daily_records_for_b = filter_daily_records_by_warrant_codes(daily_records, a_warrant_codes)

    print("【Step 3c】建立 B 類事件：同標的單日合計買超...")
    b_events = build_b_events(daily_records_for_b, item_map)
    print(f"  ✅ B 類事件：{len(b_events)} 筆")

    # 已經進入 B 的權證代號，不再進入 C / D。
    b_warrant_codes = collect_event_warrant_codes(b_events)
    daily_records_for_c = filter_daily_records_by_warrant_codes(
        daily_records_for_b,
        b_warrant_codes
    )

    print("【Step 3d】建立 C 類事件：同標的 3 日累積買超...")
    c_events = build_c_events(daily_records_for_c, item_map)
    print(f"  ✅ C 類事件：{len(c_events)} 筆")

    # 已經進入 C 的權證代號，不再進入 D。
    c_warrant_codes = collect_event_warrant_codes(c_events)
    daily_records_for_d = filter_daily_records_by_warrant_codes(
        daily_records_for_c,
        c_warrant_codes
    )

    print(f"【Step 3e】建立 D 類事件：同標的近 {D_WINDOW_DAYS} 日累積淨買進...")
    d_events = build_d_events(daily_records_for_d, item_map, window_days=D_WINDOW_DAYS)
    print(f"  ✅ D 類事件：{len(d_events)} 筆")

    print(f"  ✅ A 類事件：{len(a_events)} 筆")

    if not a_events and not b_events and not c_events and not d_events:
        print("⚠️ A/B/C/D 皆無事件")
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    price_cache = fetch_all_prices(a_events, b_events, c_events, d_events)

    build_excel(a_events, b_events, c_events, d_events, item_map, price_cache, items, output_path)
    upload_excel_to_google_sheet(output_path)

    elapsed = time.time() - program_start
    minutes = int(elapsed // 60)
    seconds = elapsed % 60

    print(f"\n{'=' * 70}")
    print("✅ 完成！")
    print(f"📄 {output_path}")
    print(f"⏱️ 總執行時間：{elapsed:.2f} 秒")
    print(f"⏱️ 約為：{minutes} 分 {seconds:.2f} 秒")


if __name__ == "__main__":
    main()
