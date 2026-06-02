import io
import json
import html
import base64
import math
import os
import re
import textwrap
import time
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:
    from google import genai
except Exception:
    genai = None

try:
    from googlenewsdecoder import gnewsdecoder
except Exception:
    gnewsdecoder = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle, Patch
from matplotlib.ticker import FuncFormatter

try:
    from X_function import get_institutional_stats_finmind
except Exception:
    get_institutional_stats_finmind = None


# ============================================================
# 基本設定
# ============================================================

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://pscnetsecrwd.moneydj.com/",
}

OPENAPI_WARRANT_HEADERS = {
    "User-Agent": HDR["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

TWSE_WARRANT_DAILY_OPENAPI_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap42_L"
TPEX_WARRANT_DAILY_OPENAPI_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap42_O"

API4 = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata"
    "/9b/6e/0a/TwWarrantData.xdjjson"
    "?a={code}&x=warrant-chip0002-4&c={start}&d={end}&revision=2018_07_31_1"
)
API5 = (
    "https://pscnetsecrwd.moneydj.com/b2brwdCommon/jsondata"
    "/d8/f5/27/twWarrantData.xdjjson"
    "?x=warrant-chip0002-5&c={days}&a={warrant}&b={broker}&revision=2018_07_31_1"
)

# 週報參數
WEEK_TRADING_DAYS = int(os.getenv("WARRANT_WEEK_TRADING_DAYS", "5"))
CHART_LOOKBACK = int(os.getenv("WARRANT_CHART_LOOKBACK", "70"))
API4_WORKERS = int(os.getenv("WARRANT_API4_WORKERS", "40"))
API5_WORKERS = int(os.getenv("WARRANT_API5_WORKERS", "50"))
API5_DAYS = int(os.getenv("WARRANT_API5_DAYS", "110"))
API4_SCAN_CALENDAR_DAYS = int(os.getenv("WARRANT_API4_SCAN_CALENDAR_DAYS", "110"))
MAX_WARRANTS = int(os.getenv("WARRANT_REPORT_MAX_WARRANTS", "0"))
MAX_PAIRS = int(os.getenv("WARRANT_REPORT_MAX_PAIRS", "0"))
LIVE_FETCH_ENABLE = os.getenv("WARRANT_LIVE_FETCH_ENABLE", "1").strip().lower() not in ("0", "false", "no")
GSHEET_FALLBACK_ENABLE = os.getenv("WARRANT_GSHEET_ENABLE", "1").strip().lower() not in ("0", "false", "no")
NEWS_ENABLE = os.getenv("WARRANT_NEWS_ENABLE", "1").strip().lower() not in ("0", "false", "no")
# 疑似造市 / 避險對沖設定
# 預設只標記不刪除：8% 用於提示疑似對沖；若真的啟用刪除，3% 才會過濾。
HEDGE_MARK_THRESHOLD = float(os.getenv("WARRANT_HEDGE_MARK_THRESHOLD", os.getenv("WARRANT_HEDGE_THRESHOLD", "0.08")))
HEDGE_FILTER_THRESHOLD = float(os.getenv("WARRANT_HEDGE_FILTER_THRESHOLD", "0.03"))
HEDGE_FILTER_ENABLE = os.getenv("WARRANT_HEDGE_FILTER_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
HEDGE_MIN_GROSS_AMOUNT = float(os.getenv("WARRANT_HEDGE_MIN_GROSS_AMOUNT", "3000000"))
HEDGE_MIN_SIDE_AMOUNT = float(os.getenv("WARRANT_HEDGE_MIN_SIDE_AMOUNT", "1000000"))

# TOP5 專用：同一天、同一檔權證，若不同券商 / 分點一買一賣金額高度接近，視為疑似對手單並排除。
# 預設啟用：單邊至少 100 萬，買賣金額差距 3% 以內。
CROSS_BROKER_OFFSET_FILTER_ENABLE = os.getenv("WARRANT_CROSS_BROKER_OFFSET_FILTER_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
CROSS_BROKER_OFFSET_THRESHOLD = float(os.getenv("WARRANT_CROSS_BROKER_OFFSET_THRESHOLD", "0.03"))
CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT = float(os.getenv("WARRANT_CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT", "1000000"))

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", os.getenv("GSHEET_NAME", "權證分點籌碼"))
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", os.getenv("GSHEET_ID", "")).strip()

_THREAD_LOCAL = threading.local()

# 視覺風格：淺背景 + Apple 風格藏青色元素
BG = "#F5F5F7"        # 淺灰白背景，不使用整片深藍底
PANEL = "#FFFFFF"     # 圖表面板
PANEL2 = "#FFFFFF"    # 卡片底色
GRID = "#CAD3DF"      # 淺灰藍格線
TEXT = "#101828"      # 主要文字
MUTED = "#667085"     # 次要文字
NAVY = "#1D2B44"      # 藏青色主色，接近 Apple 常用的沉穩深藍灰
GOLD = NAVY            # 既有變數沿用為主色
RED = "#E85D5D"       # 買超 / 上漲
GREEN = "#2CB39A"     # 賣超 / 下跌
BLUE = "#315F95"      # 累計資金流折線
ORANGE = "#F59E0B"
LIME = "#2E8B57"
PURPLE = "#6F5BD8"
WHITE = "#FFFFFF"

# 中央浮水印設定：圖片偏長，因此上下各放一個淡浮水印
CENTER_WATERMARK_TEXT = "股市艾斯\n台股DC討論群"
CENTER_WATERMARK_ALPHA = 0.06
CENTER_WATERMARK_FONT_SIZE = 200
CENTER_WATERMARK_ROTATION = 18

# 字型：GitHub Actions 建議安裝 fonts-noto-cjk
available_fonts = [f.name for f in fm.fontManager.ttflist]
for font_name in ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans TC", "Microsoft JhengHei", "SimHei"]:
    if font_name in available_fonts:
        plt.rcParams["font.family"] = font_name
        break
else:
    plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False


# ============================================================
# 共用工具
# ============================================================

def get_thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _THREAD_LOCAL.session = session
    return session


def _clean_code(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip().replace("'", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s.strip()


def normalize_openapi_warrant_code(code) -> str:
    s = str(code or "").strip().upper().replace("'", "")
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and len(s) == 5:
        s = s.zfill(6)
    return s


def normalize_date_str(date_str) -> str:
    dt = parse_date(date_str)
    return dt.strftime("%Y/%m/%d") if dt else str(date_str or "").strip()


def parse_date(date_str):
    try:
        if date_str is None:
            return None
        s = str(date_str).strip().replace("-", "/").replace(".", "/")
        if not s or s in ("-", "--", "nan", "None"):
            return None
        if re.fullmatch(r"\d{7}", s):  # ROC yyyMMdd
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
            return datetime(y, m, d)
        if re.fullmatch(r"\d{8}", s):
            y = int(s[:4])
            m = int(s[4:6])
            d = int(s[6:8])
            if y < 1911:
                y += 1911
            return datetime(y, m, d)
        parts = s.split("/")
        if len(parts) != 3:
            return None
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if y < 1911:
            y += 1911
        return datetime(y, m, d)
    except Exception:
        return None


def normalize_openapi_trade_date(date_value) -> str:
    dt = parse_date(date_value)
    return dt.strftime("%Y/%m/%d") if dt else str(date_value or "").strip()


def parse_openapi_trade_date_for_sort(date_value):
    return parse_date(date_value) or datetime.min


def clean_openapi_number(value) -> int:
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace(" ", "").replace("　", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in ("-", "."):
        return 0
    try:
        return int(round(float(s)))
    except Exception:
        return 0


def fmt_money(v: float) -> str:
    try:
        v = float(v)
    except Exception:
        return "-"
    sign = "+" if v > 0 else "-" if v < 0 else ""
    av = abs(v)
    if av >= 100000000:
        return f"{sign}{av / 100000000:.2f}億"
    if av >= 10000:
        return f"{sign}{av / 10000:.0f}萬"
    return f"{v:+,.0f}"


def fmt_money_abs(v: float) -> str:
    return fmt_money(abs(float(v)))


def fmt_pct(v: float) -> str:
    if v is None or pd.isna(v):
        return "-"
    return f"{v:+.2f}%"


def money_tick(v, pos=None):
    try:
        return fmt_money(v).replace("+", "")
    except Exception:
        return str(v)


def wrap_text(s: str, width: int = 18, max_lines: int = 2) -> str:
    s = str(s or "").strip()
    if len(s) <= width:
        return s
    lines = textwrap.wrap(s, width=width)
    lines = lines[:max_lines]
    if len("".join(lines)) < len(s):
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return "\n".join(lines)


# ============================================================
# 股價 / 指標
# ============================================================

def get_tw_stock_name(stock_code: str) -> str:
    stock_code = str(stock_code).strip()
    # TWSE
    try:
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
        resp = requests.get(url, timeout=8)
        for item in resp.json().get("data", []):
            if str(item[0]).strip() == stock_code:
                return str(item[1]).strip()
    except Exception:
        pass
    # TPEx
    try:
        url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json"
        resp = requests.get(url, timeout=8)
        tables = resp.json().get("tables", [])
        if tables:
            for item in tables[0].get("data", []):
                if str(item[0]).strip() == stock_code:
                    return str(item[1]).strip()
    except Exception:
        pass
    return "未知公司"


def fetch_stock_data_yf(stock_code: str, period="160d"):
    for suffix, market in [("TW", "上市"), ("TWO", "上櫃")]:
        full_code = f"{stock_code}.{suffix}"
        try:
            print(f"🔍 下載股價：{full_code}")
            df = yf.download(full_code, period=period, interval="1d", progress=False, auto_adjust=False)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            need = {"Open", "High", "Low", "Close", "Volume"}
            if not need.issubset(df.columns):
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy().dropna()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df.index.name = "Date"
            return df, market, full_code
        except Exception as e:
            print(f"⚠️ {full_code} 下載失敗：{e}")
    return None, None, None


def add_supertrend(df: pd.DataFrame, period=10, multiplier=2.5, use_atr=True) -> pd.DataFrame:
    df = df.copy()
    hl2 = (df["High"] + df["Low"]) / 2
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean() if use_atr else tr.rolling(period).mean()
    upper_basic = hl2 - multiplier * atr
    lower_basic = hl2 + multiplier * atr
    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    trend = [1]
    supertrend = [np.nan]
    buy_signal = [False]
    sell_signal = [False]
    for i in range(1, len(df)):
        upper_band.iloc[i] = max(upper_basic.iloc[i], upper_band.iloc[i - 1]) if df["Close"].iloc[i - 1] > upper_band.iloc[i - 1] else upper_basic.iloc[i]
        lower_band.iloc[i] = min(lower_basic.iloc[i], lower_band.iloc[i - 1]) if df["Close"].iloc[i - 1] < lower_band.iloc[i - 1] else lower_basic.iloc[i]
        prev = trend[-1]
        if prev == -1 and df["Close"].iloc[i] > lower_band.iloc[i - 1]:
            trend.append(1)
        elif prev == 1 and df["Close"].iloc[i] < upper_band.iloc[i - 1]:
            trend.append(-1)
        else:
            trend.append(prev)
        buy_signal.append(trend[-1] == 1 and trend[-2] == -1)
        sell_signal.append(trend[-1] == -1 and trend[-2] == 1)
        supertrend.append(upper_band.iloc[i] if trend[-1] == 1 else lower_band.iloc[i])
    return pd.DataFrame({
        "Supertrend": supertrend,
        "Supertrend_Trend": trend,
        "Supertrend_Buy": buy_signal,
        "Supertrend_Sell": sell_signal,
    }, index=df.index)


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for n in [5, 10, 20, 60]:
        df[f"MA{n}"] = df["Close"].rolling(n).mean()
    df["MV5"] = df["Volume"].rolling(5).mean()
    df["MV20"] = df["Volume"].rolling(20).mean()
    low_min = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K9"] = rsv.ewm(com=2).mean()
    df["D9"] = df["K9"].ewm(com=2).mean()
    df["J9"] = 3 * df["K9"] - 2 * df["D9"]
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["OSC"] = df["DIF"] - df["MACD"]
    df["BB_MID"] = df["Close"].rolling(20).mean()
    df["BB_STD"] = df["Close"].rolling(20).std()
    df["BB_UPPER"] = df["BB_MID"] + 2 * df["BB_STD"]
    df["BB_LOWER"] = df["BB_MID"] - 2 * df["BB_STD"]
    df["BB_WIDTH"] = df["BB_UPPER"] - df["BB_LOWER"]
    df[["Supertrend", "Supertrend_Trend", "Supertrend_Buy", "Supertrend_Sell"]] = add_supertrend(df)
    return df


def get_ma_kline_signals(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return ""
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    notes = []
    if latest["MA5"] > latest["MA10"] > latest["MA20"] > latest["MA60"]:
        notes.append("均線多頭排列")
    elif latest["MA5"] < latest["MA10"] < latest["MA20"] < latest["MA60"]:
        notes.append("均線空頭排列")
    if prev["MA5"] < prev["MA20"] and latest["MA5"] > latest["MA20"]:
        notes.append("均線黃金交叉")
    elif prev["MA5"] > prev["MA20"] and latest["MA5"] < latest["MA20"]:
        notes.append("均線死亡交叉")
    if all(latest["Close"] > latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("強勢站上均線")
    elif all(latest["Close"] < latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("全面跌破均線")
    if latest["Close"] > latest["MA60"] and latest["Close"] > latest["Open"] and latest["Volume"] > prev["Volume"]:
        notes.append("帶量突破年線")
    if latest["Close"] < latest["MA20"] and latest["Close"] < latest["Open"] and latest["Volume"] > prev["Volume"]:
        notes.append("帶量長黑跌破月線")
    return "．".join(notes)


def get_kd_signals(df):
    if len(df) < 2:
        return ""
    k, d, j = df["K9"].iloc[-1], df["D9"].iloc[-1], df["J9"].iloc[-1]
    kp, dp, jp = df["K9"].iloc[-2], df["D9"].iloc[-2], df["J9"].iloc[-2]
    notes = []
    if kp < dp and k > d:
        notes.append("KD黃金交叉")
    if kp > dp and k < d:
        notes.append("KD死亡交叉")
    if k < 20 and k > kp:
        notes.append("K低檔翻揚")
    if k > 80 and k < kp:
        notes.append("K高檔鈍化")
    if jp < kp and j > k:
        notes.append("J上穿K")
    if jp > kp and j < k:
        notes.append("J下穿K")
    if j >= 100:
        notes.append("J過熱")
    if j <= 0:
        notes.append("J過冷")
    return "．".join(notes)


def get_macd_signals(df):
    if len(df) < 7:
        return ""
    dif, macd, osc, close = df["DIF"], df["MACD"], df["OSC"], df["Close"]
    notes = []
    if dif.iloc[-2] < macd.iloc[-2] and dif.iloc[-1] > macd.iloc[-1]:
        notes.append("MACD黃叉")
    if dif.iloc[-2] > macd.iloc[-2] and dif.iloc[-1] < macd.iloc[-1]:
        notes.append("MACD死叉")
    if osc.iloc[-2] < 0 and osc.iloc[-1] > 0:
        notes.append("OSC翻多")
    if osc.iloc[-2] > 0 and osc.iloc[-1] < 0:
        notes.append("OSC翻空")
    n = 6
    if close.iloc[-1] < close.iloc[-n] and dif.iloc[-1] > dif.iloc[-n] and osc.iloc[-1] < 0:
        notes.append("多頭背離")
    if close.iloc[-1] > close.iloc[-n] and dif.iloc[-1] < dif.iloc[-n] and osc.iloc[-1] > 0:
        notes.append("空頭背離")
    return "．".join(notes)



# ============================================================
# 三大法人資料與繪圖
# ============================================================

def _to_lots(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0).astype(float)
    # FinMind 有時是股數、有時是張；若數值明顯偏大，轉成張。
    if s.abs().median(skipna=True) > 50000:
        return s / 1000.0
    return s


def _classify_finmind_inst_name(name) -> str:
    """將 FinMind 三大法人分類名稱統一成 foreign / invest / dealer。

    注意：FinMind 的資料常會把自營商拆成 Dealer_self / Dealer_Hedging。
    圖表右上角三大法人要對齊一般網站的「外資、投信、自營商自行買賣」口徑，
    因此這裡排除 Dealer_Hedging / 自營商避險，避免權證或衍生性商品避險部位被誤算成自營商大買。
    """
    s = str(name or "").strip().lower()
    if not s:
        return ""
    if "hedging" in s or "hedge" in s or "避險" in s:
        return ""
    if "investment_trust" in s or "投信" in s:
        return "invest"
    if "dealer" in s or "自營" in s:
        return "dealer"
    if "foreign" in s or "外資" in s or "陸資" in s:
        return "foreign"
    return ""

def _pick_existing_col(df: pd.DataFrame, candidates: List[str]) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    return ""


def fetch_inst_60d_from_finmind_token(stock_code: str, days: int = 80) -> pd.DataFrame:
    """
    直接使用 FINMIND_API_TOKEN 從 FinMind API 抓三大法人買賣超。
    回傳欄位: Date, foreign, invest, dealer, total，單位統一為張。
    """
    token = os.getenv("FINMIND_API_TOKEN", "").strip()
    if not token:
        print("⚠️ 未設定 FINMIND_API_TOKEN，略過 FinMind 三大法人資料")
        return pd.DataFrame()

    try:
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=max(int(days * 2.8), 120))
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": str(stock_code).strip(),
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "token": token,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": HDR["User-Agent"],
        }
        resp = requests.get(url, params=params, headers=headers, timeout=(8, 30))
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        if not data:
            msg = payload.get("msg", "") if isinstance(payload, dict) else ""
            print(f"⚠️ FinMind 三大法人資料為空：{stock_code} {msg}")
            return pd.DataFrame()

        raw = pd.DataFrame(data).fillna(0)
        date_col = _pick_existing_col(raw, ["date", "Date", "日期"])
        name_col = _pick_existing_col(raw, ["name", "institutional_investor", "investor", "type", "category", "法人"])
        net_col = _pick_existing_col(raw, ["net", "buy_sell", "buy_sell_amount", "買賣超", "買賣超股數"])
        buy_col = _pick_existing_col(raw, ["buy", "buy_amount", "buy_volume", "買進", "買進股數"])
        sell_col = _pick_existing_col(raw, ["sell", "sell_amount", "sell_volume", "賣出", "賣出股數"])

        if not date_col or not name_col:
            print(f"⚠️ FinMind 三大法人欄位不符：{raw.columns.tolist()}")
            return pd.DataFrame()

        tmp = raw.copy()
        tmp["Date"] = pd.to_datetime(tmp[date_col], errors="coerce")
        tmp = tmp.dropna(subset=["Date"])
        tmp["inst_group"] = tmp[name_col].map(_classify_finmind_inst_name)
        tmp = tmp[tmp["inst_group"].isin(["foreign", "invest", "dealer"])]
        if tmp.empty:
            print(f"⚠️ FinMind 三大法人分類不到外資/投信/自營商：{stock_code}")
            return pd.DataFrame()

        if net_col:
            tmp["net_value"] = pd.to_numeric(tmp[net_col], errors="coerce").fillna(0.0)
        elif buy_col and sell_col:
            tmp["net_value"] = (
                pd.to_numeric(tmp[buy_col], errors="coerce").fillna(0.0)
                - pd.to_numeric(tmp[sell_col], errors="coerce").fillna(0.0)
            )
        else:
            print(f"⚠️ FinMind 三大法人找不到買賣超或買進/賣出欄位：{raw.columns.tolist()}")
            return pd.DataFrame()

        grouped = tmp.groupby(["Date", "inst_group"], as_index=False)["net_value"].sum()
        pivot = grouped.pivot(index="Date", columns="inst_group", values="net_value").fillna(0.0)
        for c in ["foreign", "invest", "dealer"]:
            if c not in pivot.columns:
                pivot[c] = 0.0

        out = pivot.reset_index()[["Date", "foreign", "invest", "dealer"]].copy()
        out["foreign"] = _to_lots(out["foreign"])
        out["invest"] = _to_lots(out["invest"])
        out["dealer"] = _to_lots(out["dealer"])
        out["total"] = out["foreign"] + out["invest"] + out["dealer"]
        out = out.sort_values("Date").tail(days).reset_index(drop=True)
        print(f"✅ FinMind 三大法人資料：{stock_code}，{len(out):,} 筆")
        return out[["Date", "foreign", "invest", "dealer", "total"]]
    except Exception as e:
        print(f"⚠️ FinMind 三大法人資料抓取失敗：{e}")
        return pd.DataFrame()


def fetch_inst_60d_from_x(stock_code: str, days: int = 80) -> pd.DataFrame:
    """
    優先使用 FINMIND_API_TOKEN 直接從 FinMind API 抓三大法人資料。
    若環境變數未設定或 API 失敗，才使用 X_function 備援。
    回傳欄位: Date, foreign, invest, dealer, total，單位統一為張。
    """
    out = fetch_inst_60d_from_finmind_token(stock_code, days=days)
    if out is not None and not out.empty:
        return out

    if get_institutional_stats_finmind is None:
        print("⚠️ 找不到 X_function.get_institutional_stats_finmind，且 FinMind 未取得資料，略過三大法人資料")
        return pd.DataFrame()
    try:
        inst = get_institutional_stats_finmind(stock_code, n_days=int(days * 2.2))
    except Exception as e:
        print(f"⚠️ X_function 三大法人資料抓取失敗：{e}")
        return pd.DataFrame()
    if inst is None or inst.empty:
        return pd.DataFrame()
    need_cols = {"date", "外資", "投信", "自營商"}
    if not need_cols.issubset(inst.columns):
        print(f"⚠️ X_function 法人資料欄位不符：{inst.columns.tolist()}")
        return pd.DataFrame()
    out = inst.copy()
    out["Date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["Date"])
    out["foreign"] = _to_lots(out["外資"])
    out["invest"] = _to_lots(out["投信"])
    out["dealer"] = _to_lots(out["自營商"])
    out["total"] = out["foreign"] + out["invest"] + out["dealer"]
    out = out.sort_values("Date").tail(days).reset_index(drop=True)
    print(f"✅ X_function 三大法人備援資料：{stock_code}，{len(out):,} 筆")
    return out[["Date", "foreign", "invest", "dealer", "total"]]


def plot_institutional_stacked_bars(ax, plot_df: pd.DataFrame, x: list):
    """三大法人買賣超（正負堆疊柱狀圖），單位：張。"""
    if not {"foreign", "invest", "dealer"}.issubset(plot_df.columns):
        ax.text(0.5, 0.5, "尚無三大法人資料", transform=ax.transAxes,
                ha="center", va="center", fontsize=26, color=MUTED)
        return

    c_foreign = "#7CB5EC"  # 外資
    c_invest = "#F59E0B"   # 投信
    c_dealer = "#9CA3AF"   # 自營商

    f = pd.to_numeric(plot_df["foreign"], errors="coerce").fillna(0).astype(float).values
    i = pd.to_numeric(plot_df["invest"], errors="coerce").fillna(0).astype(float).values
    d = pd.to_numeric(plot_df["dealer"], errors="coerce").fillna(0).astype(float).values

    f_pos, i_pos, d_pos = np.clip(f, 0, None), np.clip(i, 0, None), np.clip(d, 0, None)
    f_neg, i_neg, d_neg = np.clip(f, None, 0), np.clip(i, None, 0), np.clip(d, None, 0)

    width = 0.72
    alpha = 0.78
    ax.bar(x, f_pos, width=width, bottom=0, color=c_foreign, alpha=alpha, label="外資")
    ax.bar(x, i_pos, width=width, bottom=f_pos, color=c_invest, alpha=alpha, label="投信")
    ax.bar(x, d_pos, width=width, bottom=f_pos + i_pos, color=c_dealer, alpha=alpha, label="自營商")
    ax.bar(x, f_neg, width=width, bottom=0, color=c_foreign, alpha=alpha)
    ax.bar(x, i_neg, width=width, bottom=f_neg, color=c_invest, alpha=alpha)
    ax.bar(x, d_neg, width=width, bottom=f_neg + i_neg, color=c_dealer, alpha=alpha)
    ax.axhline(0, color=GOLD, linewidth=1.1, linestyle="--", alpha=0.65)

    max_abs = np.nanmax(np.abs(np.concatenate([f, i, d]))) if len(f) else 1
    max_abs = 1 if max_abs == 0 or pd.isna(max_abs) else max_abs
    ax.set_ylim(-max_abs * 1.35, max_abs * 1.35)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos=None: f"{v:,.0f}張" if abs(v) >= 1 else "0"))


def draw_inst_header_like_legend(inst_ax, plot_df: pd.DataFrame):
    """依照原始 K 線圖樣式，在三大法人圖上方顯示外資 / 投信 / 自營商 / 合計。"""
    c_foreign = "#7CB5EC"
    c_invest = "#F59E0B"
    c_dealer = "#9CA3AF"
    c_total = GOLD
    if plot_df.empty:
        return
    last = plot_df.iloc[-1]
    f = float(last.get("foreign", 0) or 0)
    i = float(last.get("invest", 0) or 0)
    d = float(last.get("dealer", 0) or 0)
    t = f + i + d
    def fmt(v):
        return f"{v:+,.0f}張"
    handles = [
        Patch(facecolor=c_foreign, edgecolor=c_foreign, label=f"外資 {fmt(f)}"),
        Patch(facecolor=c_invest, edgecolor=c_invest, label=f"投信 {fmt(i)}"),
        Patch(facecolor=c_dealer, edgecolor=c_dealer, label=f"自營商 {fmt(d)}"),
        Patch(facecolor=c_total, edgecolor=c_total, label=f"合計 {fmt(t)}"),
    ]
    inst_ax.legend(handles=handles, loc="upper left", ncol=4, frameon=False,
                   fontsize=26, handlelength=1.1, handletextpad=0.45,
                   columnspacing=1.15, borderaxespad=0.2, labelcolor=TEXT)

# ============================================================
# Google Sheet 快取讀取：用來回補「權證 → 標的」或直接取歷史分點快取
# ============================================================

def _build_gspread_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        print(f"⚠️ gspread/google-auth 未安裝，略過 Google Sheet 快取：{e}")
        return None
    raw_key = os.getenv("GCP_SERVICE_KEY", "").strip()
    if not raw_key:
        return None
    try:
        info = json.loads(raw_key)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"⚠️ GCP_SERVICE_KEY 解析失敗：{e}")
        return None


def _open_gsheet():
    gc = _build_gspread_client()
    if gc is None:
        return None
    try:
        return gc.open_by_key(GOOGLE_SHEET_ID) if GOOGLE_SHEET_ID else gc.open(GOOGLE_SHEET_NAME)
    except Exception as e:
        print(f"⚠️ Google Sheet 開啟失敗：{e}")
        return None


def read_gsheet_worksheet(title: str) -> pd.DataFrame:
    if not GSHEET_FALLBACK_ENABLE:
        return pd.DataFrame()
    sh = _open_gsheet()
    if sh is None:
        return pd.DataFrame()
    try:
        ws = sh.worksheet(title)
        records = ws.get_all_records(empty2zero=False, head=1)
        return pd.DataFrame(records).fillna("") if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def normalize_history_cache_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    df = raw_df.copy().fillna("")
    col_map = {
        "日期": "Date",
        "權證代號": "warrant_code",
        "權證名稱": "warrant_name",
        "標的股": "underlying_code",
        "標的名稱": "underlying_name",
        "分點": "branch",
        "分點名稱": "broker_name",
        "券商代號": "broker_code",
        "買進金額": "buy_amount",
        "賣出金額": "sell_amount",
        "買超金額": "net_amount",
    }
    missing = [c for c in col_map if c not in df.columns]
    if missing:
        return pd.DataFrame()
    out = pd.DataFrame()
    for src, dst in col_map.items():
        out[dst] = df[src]
    out["Date"] = out["Date"].map(lambda x: pd.Timestamp(normalize_date_str(x)) if parse_date(x) else pd.NaT)
    out = out.dropna(subset=["Date"])
    out["Date"] = out["Date"].dt.normalize()
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(float)
    out["warrant_code"] = out["warrant_code"].map(normalize_openapi_warrant_code)
    out["underlying_code"] = out["underlying_code"].map(_clean_code)
    out["branch"] = out["branch"].astype(str).str.strip()
    out["broker_name"] = out["broker_name"].astype(str).str.strip()
    out["branch"] = np.where(out["branch"].str.len() > 0, out["branch"], out["broker_name"])
    out["side"] = np.where(out["net_amount"] >= 0, "買超", "賣超")
    return out


def load_cached_warrant_history(stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    raw = read_gsheet_worksheet("快取_分點歷史")
    events = normalize_history_cache_df(raw)
    if events.empty:
        return pd.DataFrame()
    stock_code = _clean_code(stock_code)
    events = events[events["underlying_code"].astype(str) == stock_code].copy()
    if start_date is not None:
        events = events[events["Date"] >= pd.Timestamp(start_date).normalize()]
    if end_date is not None:
        events = events[events["Date"] <= pd.Timestamp(end_date).normalize()]
    return events.reset_index(drop=True)


def load_warrant_underlying_lookup() -> Dict[str, dict]:
    lookup = {}
    for sheet_name in ["快取_權證清單", "快取_分點歷史", "快取_候選組合_OpenAPI精選5", "快取_候選組合", "快取_候選組合_精選5"]:
        df = read_gsheet_worksheet(sheet_name)
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            wcode = normalize_openapi_warrant_code(r.get("代號", r.get("權證代號", "")))
            if not wcode:
                continue
            rec = lookup.setdefault(wcode, {"warrant_name": "", "underlying_code": "", "underlying_name": ""})
            rec["warrant_name"] = rec["warrant_name"] or str(r.get("名稱", r.get("權證名稱", ""))).strip()
            rec["underlying_code"] = rec["underlying_code"] or _clean_code(r.get("標的股", r.get("標的代號", "")))
            rec["underlying_name"] = rec["underlying_name"] or str(r.get("標的名稱", "")).strip()
    return lookup


# ============================================================
# 權證全市場分點資料：OpenAPI 權證母體 + API4 分點 + API5 金額
# ============================================================

def fetch_openapi_json(url: str, source_name: str):
    try:
        r = get_thread_session().get(url, headers=OPENAPI_WARRANT_HEADERS, timeout=(8, 30))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            print(f"✅ {source_name} OpenAPI：{len(data):,} 筆")
            return data
    except Exception as e:
        print(f"⚠️ {source_name} OpenAPI 抓取失敗：{e}")
    return []


def fetch_twse_openapi_warrant_daily_df() -> pd.DataFrame:
    data = fetch_openapi_json(TWSE_WARRANT_DAILY_OPENAPI_URL, "上市 TWSE")
    df = pd.DataFrame(data).fillna("")
    if df.empty or not {"出表日期", "交易日期", "權證代號", "權證名稱", "成交金額", "成交張數"}.issubset(df.columns):
        return pd.DataFrame()
    out = pd.DataFrame()
    out["出表日期"] = df["出表日期"].map(normalize_openapi_trade_date)
    out["交易日期"] = df["交易日期"].map(normalize_openapi_trade_date)
    out["市場"] = "上市"
    out["代號"] = df["權證代號"].map(normalize_openapi_warrant_code)
    out["名稱"] = df["權證名稱"].astype(str).str.strip()
    out["成交金額"] = df["成交金額"].map(clean_openapi_number)
    out["成交量"] = df["成交張數"].map(clean_openapi_number)
    return out


def fetch_tpex_openapi_warrant_daily_df() -> pd.DataFrame:
    data = fetch_openapi_json(TPEX_WARRANT_DAILY_OPENAPI_URL, "上櫃 TPEx")
    df = pd.DataFrame(data).fillna("")
    if df.empty or not {"Date", "交易日期", "權證代號", "權證名稱", "成交金額", "成交數量"}.issubset(df.columns):
        return pd.DataFrame()
    out = pd.DataFrame()
    out["出表日期"] = df["Date"].map(normalize_openapi_trade_date)
    out["交易日期"] = df["交易日期"].map(normalize_openapi_trade_date)
    out["市場"] = "上櫃"
    out["代號"] = df["權證代號"].map(normalize_openapi_warrant_code)
    out["名稱"] = df["權證名稱"].astype(str).str.strip()
    out["成交金額"] = df["成交金額"].map(clean_openapi_number)
    out["成交量"] = df["成交數量"].map(clean_openapi_number)
    return out


def make_stock_aliases(stock_name: str) -> List[str]:
    name = str(stock_name or "").strip().replace(" ", "")
    aliases = [name] if name else []
    suffixes = ["半導體", "科技", "電子", "光電", "精密", "材料", "生技", "醫療", "資訊", "電腦", "通信", "通訊", "電機", "機械", "工業", "實業", "企業", "國際", "控股", "投控"]
    stripped = name
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if stripped.endswith(suf) and len(stripped) > len(suf) + 1:
                stripped = stripped[: -len(suf)]
                if len(stripped) >= 2 and stripped not in aliases:
                    aliases.append(stripped)
                changed = True
                break
    # 不主動切兩字，避免昇陽半/昇陽這類誤判；只保留三字以上安全前綴
    if len(name) >= 3 and name[:3] not in aliases:
        aliases.append(name[:3])
    return [a for a in aliases if a]


def get_all_active_call_warrants(stock_code: str, stock_name: str) -> List[dict]:
    frames = []
    for f in [fetch_twse_openapi_warrant_daily_df(), fetch_tpex_openapi_warrant_daily_df()]:
        if f is not None and not f.empty:
            frames.append(f)
    if not frames:
        return []
    all_df = pd.concat(frames, ignore_index=True).fillna("")
    trade_dates = sorted([d for d in all_df["交易日期"].unique() if str(d).strip()], key=parse_openapi_trade_date_for_sort)
    if not trade_dates:
        return []
    latest_trade_date = trade_dates[-1]
    active_df = all_df[
        (all_df["交易日期"] == latest_trade_date)
        & (pd.to_numeric(all_df["成交量"], errors="coerce").fillna(0) > 0)
        & (all_df["名稱"].astype(str).str.contains("購", na=False))
        & (~all_df["名稱"].astype(str).str.contains("售|牛|熊", na=False))
        & (all_df["代號"].astype(str).str.fullmatch(r"\d{6}", na=False))
    ].copy()
    lookup = load_warrant_underlying_lookup()
    aliases = make_stock_aliases(stock_name)
    warrants = []
    seen = set()
    for _, r in active_df.sort_values(["成交金額", "成交量"], ascending=[False, False]).iterrows():
        code = normalize_openapi_warrant_code(r.get("代號"))
        name = str(r.get("名稱", "")).strip()
        if not code or code in seen:
            continue
        cached = lookup.get(code, {})
        ucode = _clean_code(cached.get("underlying_code", ""))
        uname = str(cached.get("underlying_name", "")).strip()
        name_match = any(name.replace(" ", "").startswith(a) for a in aliases if a)
        if ucode == str(stock_code) or name_match:
            seen.add(code)
            warrants.append({
                "代號": code,
                "名稱": name,
                "標的股": str(stock_code),
                "標的名稱": uname or stock_name,
                "成交金額": int(r.get("成交金額", 0) or 0),
                "成交量": int(r.get("成交量", 0) or 0),
            })
    if MAX_WARRANTS > 0:
        warrants = warrants[:MAX_WARRANTS]
    print(f"✅ {stock_code} 相關有成交認購權證：{len(warrants):,} 支")
    return warrants


def api4_get(code, start, end):
    try:
        r = get_thread_session().get(API4.format(code=code, start=start, end=end), headers=HDR, timeout=(5, 15))
        data = json.loads(r.content.decode("utf-8"))
        rows = []
        for item in (data if isinstance(data, list) else [data]):
            rows.extend(item.get("ResultSet", {}).get("Result", []))
        return rows
    except Exception:
        return []


def api5_get(warrant, broker, days=None):
    try:
        days = int(days if days is not None else API5_DAYS)
        r = get_thread_session().get(API5.format(warrant=warrant, broker=broker, days=days), headers=HDR, timeout=(5, 15))
        data = json.loads(r.content.decode("utf-8"))
        rs = data[0].get("ResultSet", {}) if isinstance(data, list) else data.get("ResultSet", {})
        return rs.get("Result", [])
    except Exception:
        return []


def fetch_all_broker_pairs_for_warrants(warrants: List[dict], start_s: str, end_s: str) -> List[dict]:
    pairs = {}
    if not warrants:
        return []

    def scan_one(w):
        out = []
        for row in api4_get(w["代號"], start_s, end_s):
            broker_code = str(row.get("V2", "")).strip()
            broker_name = str(row.get("V3", "")).strip()
            if not broker_code:
                continue
            out.append({
                "warrant_code": w["代號"],
                "warrant_name": w["名稱"],
                "underlying_code": w.get("標的股", ""),
                "underlying_name": w.get("標的名稱", ""),
                "broker_code": broker_code,
                "branch": broker_name,
            })
        return out

    print(f"🔎 API4 掃描全部分點：{len(warrants):,} 支權證，workers={API4_WORKERS}")
    with ThreadPoolExecutor(max_workers=max(1, API4_WORKERS)) as ex:
        futures = {ex.submit(scan_one, w): w for w in warrants}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                for rec in fut.result():
                    pairs[(rec["warrant_code"], rec["broker_code"])] = rec
            except Exception:
                pass
            if i % 100 == 0:
                print(f"  API4 {i:,}/{len(warrants):,}，pairs={len(pairs):,}")
    pair_list = list(pairs.values())
    if MAX_PAIRS > 0:
        pair_list = pair_list[:MAX_PAIRS]
    print(f"✅ API4 完成：{len(pair_list):,} 組 權證×分點")
    return pair_list


def fetch_api5_events_for_pairs(pair_list: List[dict], start_date=None, end_date=None) -> pd.DataFrame:
    rows = []
    if not pair_list:
        return pd.DataFrame()

    def fetch_one(p):
        out = []
        api_rows = api5_get(p["warrant_code"], p["broker_code"], days=API5_DAYS)
        for row in api_rows or []:
            buy_s = int(float(row.get("V2", 0) or 0))
            sell_s = int(float(row.get("V3", 0) or 0))
            buy_a = int(float(row.get("V4", 0) or 0) * 1000)
            sell_a = int(float(row.get("V5", 0) or 0) * 1000)
            net_a = buy_a - sell_a
            if buy_a == 0 and sell_a == 0:
                continue
            dt = parse_date(row.get("V1", ""))
            if not dt:
                continue
            out.append({
                "Date": pd.Timestamp(dt).normalize(),
                "branch": p["branch"],
                "broker_code": p["broker_code"],
                "warrant_code": p["warrant_code"],
                "warrant_name": p["warrant_name"],
                "underlying_code": p.get("underlying_code", ""),
                "underlying_name": p.get("underlying_name", ""),
                "buy_amount": float(buy_a),
                "sell_amount": float(sell_a),
                "net_amount": float(net_a),
                "buy_shares": buy_s,
                "sell_shares": sell_s,
            })
        return out

    print(f"💰 API5 回查買賣金額：{len(pair_list):,} 組，workers={API5_WORKERS}")
    with ThreadPoolExecutor(max_workers=max(1, API5_WORKERS)) as ex:
        futures = {ex.submit(fetch_one, p): p for p in pair_list}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                rows.extend(fut.result())
            except Exception:
                pass
            if i % 200 == 0:
                print(f"  API5 {i:,}/{len(pair_list):,}，events={len(rows):,}")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if start_date is not None:
        df = df[df["Date"] >= pd.Timestamp(start_date).normalize()]
    if end_date is not None:
        df = df[df["Date"] <= pd.Timestamp(end_date).normalize()]
    df["side"] = np.where(df["net_amount"] >= 0, "買超", "賣超")
    return df.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)


def fetch_warrant_events_full_market(stock_code: str, stock_name: str, start_date, end_date) -> pd.DataFrame:
    # 先讀既有快取，速度最快；再用 live 補足最新全分點資料。
    cached = load_cached_warrant_history(stock_code, start_date=start_date, end_date=end_date)
    frames = []
    if not cached.empty:
        frames.append(cached)
        print(f"☁️ Google Sheet 快取_分點歷史命中：{len(cached):,} 筆")

    if LIVE_FETCH_ENABLE:
        end_dt = pd.Timestamp(end_date).to_pydatetime()
        start_s = (end_dt - timedelta(days=API4_SCAN_CALENDAR_DAYS)).strftime("%Y/%m/%d")
        end_s = end_dt.strftime("%Y/%m/%d")
        warrants = get_all_active_call_warrants(stock_code, stock_name)
        pairs = fetch_all_broker_pairs_for_warrants(warrants, start_s, end_s)
        live = fetch_api5_events_for_pairs(pairs, start_date=start_date, end_date=end_date)
        if not live.empty:
            frames.append(live)
            print(f"🌐 Live 權證全分點資料：{len(live):,} 筆")

    if not frames:
        return pd.DataFrame(columns=["Date", "branch", "broker_code", "warrant_code", "warrant_name", "underlying_code", "underlying_name", "buy_amount", "sell_amount", "net_amount"])

    events = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        events[c] = pd.to_numeric(events[c], errors="coerce").fillna(0.0)
    events["Date"] = pd.to_datetime(events["Date"]).dt.normalize()
    events["warrant_code"] = events["warrant_code"].map(normalize_openapi_warrant_code)
    events["branch"] = events["branch"].astype(str).str.strip()
    events["broker_code"] = events["broker_code"].astype(str).str.strip()
    # 合併 live/cache 重複資料
    group_cols = ["Date", "broker_code", "branch", "warrant_code", "warrant_name", "underlying_code", "underlying_name"]
    events = events.groupby(group_cols, as_index=False, dropna=False).agg({
        "buy_amount": "max",
        "sell_amount": "max",
        "net_amount": "max",
    })
    events = events[(events["buy_amount"] > 0) | (events["sell_amount"] > 0) | (events["net_amount"].abs() > 0)].copy()
    events["side"] = np.where(events["net_amount"] >= 0, "買超", "賣超")
    return events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)


# ============================================================
# 週報統計
# ============================================================

def filter_out_market_maker_hedges(
    events_df: pd.DataFrame,
    hedge_threshold: float = HEDGE_MARK_THRESHOLD,
    do_filter: bool = False,
    min_gross_amount: float = HEDGE_MIN_GROSS_AMOUNT,
    min_side_amount: float = HEDGE_MIN_SIDE_AMOUNT,
):
    """
    偵測 / 過濾疑似造市對沖。

    判斷條件：同券商、同權證、同日期，且買賣雙邊都有一定金額，
    若 abs(買進 - 賣出) / (買進 + 賣出) <= 門檻，視為疑似對沖。

    預設 do_filter=False，只回報疑似筆數，不刪資料，避免漏抓大額主力單。
    """
    if events_df is None or events_df.empty:
        return events_df, 0
    need_cols = {"broker_code", "warrant_code", "Date", "buy_amount", "sell_amount"}
    if not need_cols.issubset(events_df.columns):
        return events_df, 0

    e = events_df.copy()
    e["Date"] = pd.to_datetime(e["Date"]).dt.normalize()
    grouped = e.groupby(["broker_code", "warrant_code", "Date"], as_index=False).agg({
        "buy_amount": "sum",
        "sell_amount": "sum",
    })
    grouped["gross_amount"] = grouped["buy_amount"] + grouped["sell_amount"]
    grouped["net_amount"] = grouped["buy_amount"] - grouped["sell_amount"]
    grouped["net_ratio"] = np.where(
        grouped["gross_amount"] > 0,
        grouped["net_amount"].abs() / grouped["gross_amount"],
        1.0,
    )

    hedge_groups = grouped[
        (grouped["gross_amount"] >= min_gross_amount)
        & (grouped["buy_amount"] >= min_side_amount)
        & (grouped["sell_amount"] >= min_side_amount)
        & (grouped["net_ratio"] <= hedge_threshold)
    ][["broker_code", "warrant_code", "Date"]]

    if hedge_groups.empty:
        return e, 0

    hedge_index = hedge_groups.set_index(["broker_code", "warrant_code", "Date"]).index
    mask = e.set_index(["broker_code", "warrant_code", "Date"]).index.isin(hedge_index)
    n_rows = int(mask.sum())

    if do_filter:
        return e.loc[~mask].copy(), n_rows
    return e, n_rows




def filter_out_cross_broker_offset_trades(
    events_df: pd.DataFrame,
    amount_diff_threshold: float = CROSS_BROKER_OFFSET_THRESHOLD,
    min_side_amount: float = CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT,
    do_filter: bool = True,
):
    """
    TOP5 專用：偵測 / 過濾同日同權證的疑似對手單。

    判斷條件：
    1. 同一天、同一檔權證。
    2. 一個不同券商 / 分點為買超，另一個不同券商 / 分點為賣超。
    3. 買超與賣超單邊金額皆大於等於 min_side_amount。
    4. 兩邊絕對金額差距 / 較大金額 <= amount_diff_threshold。

    這個函式只建議用在 TOP5 分點排名，不改動整體權證資金流柱狀圖與累計線。
    """
    if events_df is None or events_df.empty:
        return events_df, 0
    need_cols = {"Date", "warrant_code", "broker_code", "branch", "net_amount"}
    if not need_cols.issubset(events_df.columns):
        return events_df, 0

    e = events_df.copy().reset_index(drop=True)
    e["Date"] = pd.to_datetime(e["Date"]).dt.normalize()
    e["net_amount"] = pd.to_numeric(e["net_amount"], errors="coerce").fillna(0.0).astype(float)
    e["broker_code"] = e["broker_code"].astype(str).str.strip()
    e["branch"] = e["branch"].astype(str).str.strip()
    e["warrant_code"] = e["warrant_code"].astype(str).str.strip()

    remove_idx = set()

    for _, sub in e.groupby(["Date", "warrant_code"], dropna=False):
        buys = sub[sub["net_amount"] >= float(min_side_amount)].copy()
        sells = sub[sub["net_amount"] <= -float(min_side_amount)].copy()
        if buys.empty or sells.empty:
            continue

        buys = buys.reindex(buys["net_amount"].abs().sort_values(ascending=False).index)
        sells = sells.reindex(sells["net_amount"].abs().sort_values(ascending=False).index)
        used_sells = set()

        for buy_idx, buy_row in buys.iterrows():
            if buy_idx in remove_idx:
                continue

            buy_amt = abs(float(buy_row.get("net_amount", 0) or 0))
            if buy_amt < float(min_side_amount):
                continue

            buy_broker = str(buy_row.get("broker_code", "") or "").strip()
            buy_branch = str(buy_row.get("branch", "") or "").strip()
            candidates = []

            for sell_idx, sell_row in sells.iterrows():
                if sell_idx in used_sells or sell_idx in remove_idx:
                    continue

                sell_amt = abs(float(sell_row.get("net_amount", 0) or 0))
                if sell_amt < float(min_side_amount):
                    continue

                sell_broker = str(sell_row.get("broker_code", "") or "").strip()
                sell_branch = str(sell_row.get("branch", "") or "").strip()

                same_broker = bool(buy_broker and sell_broker and buy_broker == sell_broker)
                same_branch = bool(buy_branch and sell_branch and buy_branch == sell_branch)
                if same_broker or same_branch:
                    continue

                diff_ratio = abs(buy_amt - sell_amt) / max(buy_amt, sell_amt, 1.0)
                if diff_ratio <= float(amount_diff_threshold):
                    candidates.append((diff_ratio, -max(buy_amt, sell_amt), sell_idx))

            if candidates:
                candidates.sort()
                _, _, matched_sell_idx = candidates[0]
                remove_idx.add(buy_idx)
                remove_idx.add(matched_sell_idx)
                used_sells.add(matched_sell_idx)

    n_rows = len(remove_idx)
    if do_filter and n_rows > 0:
        e = e.drop(index=sorted(remove_idx)).reset_index(drop=True)

    return e, n_rows


def build_watch_points(ctx, stock_name: str, news_titles: List[str]):
    points = []
    df = ctx["plot_df"]
    latest = df.iloc[-1]
    close = float(latest["Close"])
    ma5 = float(latest["MA5"])
    ma20 = float(latest["MA20"])
    ma60 = float(latest["MA60"])
    k9 = float(latest.get("K9", np.nan))
    d9 = float(latest.get("D9", np.nan))
    vol = float(latest.get("Volume", np.nan))
    mv20 = float(latest.get("MV20", np.nan))

    if close >= ma5 >= ma20:
        points.append(f"技術面：收盤 {close:.0f} 站穩 5MA {ma5:.1f} 與 20MA {ma20:.1f}，下週先看短均線是否續揚。")
    elif close >= ma20:
        points.append(f"技術面：收盤仍守 20MA {ma20:.1f}，但需觀察能否重新站回 5MA {ma5:.1f}。")
    else:
        points.append(f"技術面：收盤已落在 20MA {ma20:.1f} 下方，下週需留意月線是否轉為壓力。")

    if close > ma60:
        points.append(f"中期趨勢：目前仍在 60MA {ma60:.1f} 之上，中期架構尚未轉弱。")
    else:
        points.append(f"中期趨勢：股價已逼近或跌破 60MA {ma60:.1f}，中期防守力道需再確認。")

    if not pd.isna(vol) and not pd.isna(mv20) and mv20 > 0:
        vr = vol / mv20
        points.append(f"量能面：最新日量能約為月均量 {vr:.1f} 倍，若再放量，短線趨勢延續性會更好。")

    if not pd.isna(k9) and not pd.isna(d9):
        if k9 >= 80 and d9 >= 80:
            points.append(f"動能面：KD 位於高檔（K {k9:.1f} / D {d9:.1f}），若續強屬高檔鈍化；跌破 5MA 則要防拉回。")
        elif k9 > d9:
            points.append(f"動能面：K 值高於 D 值，短線動能仍偏多，但需搭配量能不失溫。")
        else:
            points.append(f"動能面：K 值低於 D 值，下週需觀察是否重新黃金交叉。")

    net = float(ctx.get("total_net", 0))
    if net > 0:
        points.append(f"權證籌碼：本週淨買超 {fmt_money(net)}，若下週紅柱續增、累計線續上彎，代表追價資金延續。")
    elif net < 0:
        points.append(f"權證籌碼：本週淨賣超 {fmt_money(net)}，若下週綠柱持續，需留意權證資金退潮。")
    else:
        points.append("權證籌碼：本週淨流向接近中性，下週需觀察是否出現連續性紅柱或綠柱。")

    e = ctx.get("week_events")
    if e is not None and not e.empty:
        by_branch = e.groupby("branch")["net_amount"].sum().sort_values(ascending=False)
        if not by_branch.empty:
            top_branch = str(by_branch.index[0])
            top_amt = float(by_branch.iloc[0])
            points.append(f"分點觀察：目前由「{top_branch}」領軍 {fmt_money(top_amt)}，下週可觀察是否續買或轉為調節。")

    news_points = build_news_points(ctx.get("stock_code", ""), stock_name, news_titles, ctx)
    if news_points:
        points.append(news_points[0])

    return points[:5]


def build_weekly_context(stock_df: pd.DataFrame, warrant_events: pd.DataFrame, week_days: int = WEEK_TRADING_DAYS):
    plot_df = stock_df.tail(CHART_LOOKBACK).copy()
    trading_dates = list(plot_df.index)
    week_dates = trading_dates[-week_days:] if len(trading_dates) >= week_days else trading_dates
    week_start = pd.Timestamp(week_dates[0]).normalize() if week_dates else pd.NaT
    week_end = pd.Timestamp(week_dates[-1]).normalize() if week_dates else pd.NaT

    week_stock = plot_df.loc[week_dates].copy() if week_dates else plot_df.tail(0)
    prev_stock = plot_df.iloc[max(0, len(plot_df) - week_days * 2): max(0, len(plot_df) - week_days)].copy()

    start_close = float(week_stock["Close"].iloc[0]) if not week_stock.empty else np.nan
    end_close = float(week_stock["Close"].iloc[-1]) if not week_stock.empty else np.nan
    stock_ret = (end_close / start_close - 1) * 100 if start_close and not np.isnan(start_close) else np.nan
    week_vol = float(week_stock["Volume"].sum()) if not week_stock.empty else 0.0
    prev_vol = float(prev_stock["Volume"].sum()) if not prev_stock.empty else 0.0
    vol_change = (week_vol / prev_vol - 1) * 100 if prev_vol > 0 else np.nan

    hedge_removed = 0
    if warrant_events is None or warrant_events.empty:
        week_events = pd.DataFrame(columns=["Date", "branch", "warrant_code", "warrant_name", "buy_amount", "sell_amount", "net_amount"])
        plot_events = week_events.copy()
    else:
        e = warrant_events.copy()
        e["Date"] = pd.to_datetime(e["Date"]).dt.normalize()
        # 預設只偵測疑似造市 / 避險對沖，不直接刪除，避免把大額主力單誤刪。
        # 標記門檻：8%；若主動啟用刪除，才用更嚴格的 3%。
        hedge_candidates = 0
        if HEDGE_FILTER_ENABLE:
            e, hedge_removed = filter_out_market_maker_hedges(
                e,
                hedge_threshold=HEDGE_FILTER_THRESHOLD,
                do_filter=True,
            )
        else:
            _, hedge_candidates = filter_out_market_maker_hedges(
                e,
                hedge_threshold=HEDGE_MARK_THRESHOLD,
                do_filter=False,
            )
            hedge_removed = 0
        week_events = e[(e["Date"] >= week_start) & (e["Date"] <= week_end)].copy()
        plot_events = e[(e["Date"] >= pd.Timestamp(plot_df.index.min()).normalize()) & (e["Date"] <= pd.Timestamp(plot_df.index.max()).normalize())].copy()

    total_buy = float(week_events["buy_amount"].sum()) if not week_events.empty else 0.0
    total_sell = float(week_events["sell_amount"].sum()) if not week_events.empty else 0.0
    total_net = float(week_events["net_amount"].sum()) if not week_events.empty else 0.0
    bias = "偏買超" if total_net > 0 else "偏賣超" if total_net < 0 else "中性"

    return {
        "plot_df": plot_df,
        "plot_events": plot_events,
        "week_events": week_events,
        "week_start": week_start,
        "week_end": week_end,
        "stock_ret": stock_ret,
        "week_vol": week_vol,
        "vol_change": vol_change,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "total_net": total_net,
        "bias": bias,
        "hedge_removed": hedge_removed,
        "hedge_candidates": hedge_candidates if "hedge_candidates" in locals() else 0,
    }


def daily_warrant_net(plot_df: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    dates = pd.to_datetime(plot_df.index).normalize()
    out = pd.DataFrame({"Date": dates})
    if events is None or events.empty:
        out["net_amount"] = 0.0
        out["buy_amount"] = 0.0
        out["sell_amount"] = 0.0
        return out
    e = events.copy()
    e["Date"] = pd.to_datetime(e["Date"]).dt.normalize()
    g = e.groupby("Date", as_index=False).agg({"net_amount": "sum", "buy_amount": "sum", "sell_amount": "sum"})
    out = out.merge(g, on="Date", how="left").fillna(0.0)
    return out


def top_branch_tables(week_events: pd.DataFrame, topn: int = 5):
    cols = ["branch", "net_amount", "max_warrant_code", "max_warrant_name", "max_warrant_amount"]
    if week_events is None or week_events.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    e = week_events.copy()
    if CROSS_BROKER_OFFSET_FILTER_ENABLE:
        e, offset_removed = filter_out_cross_broker_offset_trades(
            e,
            amount_diff_threshold=CROSS_BROKER_OFFSET_THRESHOLD,
            min_side_amount=CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT,
            do_filter=True,
        )
        if offset_removed > 0:
            print(f"🧹 TOP5 已排除疑似對手單 / 換手單：{offset_removed:,} 筆")
    e["branch"] = e["branch"].replace("", "未知分點")
    if e.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    branch_sum = e.groupby("branch", as_index=False).agg({"net_amount": "sum", "buy_amount": "sum", "sell_amount": "sum"})

    def add_max_warrant(df, positive=True):
        rows = []
        for _, br in df.iterrows():
            branch = br["branch"]
            sub = e[e["branch"] == branch]
            wg = sub.groupby(["warrant_code", "warrant_name"], as_index=False).agg({"net_amount": "sum"})
            if wg.empty:
                max_code, max_name, max_amt = "", "", 0.0
            else:
                pick = wg.sort_values("net_amount", ascending=not positive).iloc[0]
                max_code, max_name, max_amt = pick["warrant_code"], pick["warrant_name"], float(pick["net_amount"])
            rows.append({
                "branch": branch,
                "net_amount": float(br["net_amount"]),
                "max_warrant_code": max_code,
                "max_warrant_name": max_name,
                "max_warrant_amount": max_amt,
            })
        return pd.DataFrame(rows, columns=cols)

    buy_br = branch_sum[branch_sum["net_amount"] > 0].sort_values("net_amount", ascending=False).head(topn)
    sell_br = branch_sum[branch_sum["net_amount"] < 0].sort_values("net_amount", ascending=True).head(topn)
    return add_max_warrant(buy_br, positive=True), add_max_warrant(sell_br, positive=False)


def _rule_based_key_points(ctx, stock_name: str):
    points = []
    df = ctx["plot_df"]
    latest = df.iloc[-1]
    net = ctx["total_net"]

    close = float(latest["Close"])
    ma20 = float(latest["MA20"])
    ma60 = float(latest["MA60"])
    ma_state = get_ma_kline_signals(df)
    if close > ma20 and close > ma60:
        pos = "站穩月線、季線之上"
    elif close > ma60:
        pos = "回到季線之上、月線之下"
    else:
        pos = "跌破月線或季線"
    points.append(f"股價本週 {fmt_pct(ctx['stock_ret'])}，最新收盤 {close:.0f}，{pos}" + (f"，{ma_state}" if ma_state else "") + "。")

    vol_ratio = latest["Volume"] / latest["MV20"] if latest.get("MV20", np.nan) and not pd.isna(latest.get("MV20", np.nan)) else np.nan
    if not pd.isna(vol_ratio):
        tag = "爆量" if vol_ratio >= 2 else "增溫" if vol_ratio >= 1.2 else "量縮"
        points.append(f"本週量能較前週 {fmt_pct(ctx['vol_change'])}，最新日約為月均量 {vol_ratio:.1f} 倍（{tag}）。")

    e = ctx["week_events"]
    if e is not None and not e.empty:
        by_branch = e.groupby("branch")["net_amount"].sum().sort_values(ascending=False)
        top_branch = str(by_branch.index[0])
        top_amt = float(by_branch.iloc[0])
        pos_sum = by_branch.clip(lower=0).sum()
        share = by_branch.head(3).clip(lower=0).sum() / max(1.0, pos_sum) * 100 if pos_sum > 0 else 0.0
        points.append(f"權證淨流向 {fmt_money(net)}（{ctx['bias']}），由「{top_branch}」領軍 {fmt_money(top_amt)}，前三大分點佔買超 {share:.0f}%。")
    return points[:4]


def _trim_weekly_point(text: str, max_len: int | None = None) -> str:
    max_len = int(max_len or WEEKLY_KEYPOINT_POINT_MAX_LEN)
    s = _normalize_news_text(text)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = re.sub(r"^(本週重點|重點|摘要)[:：]\s*", "", s).strip()
    s = s.strip("。；;，, ")
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    last = max(cut.rfind("，"), cut.rfind("、"), cut.rfind("；"), cut.rfind(";"))
    if last >= 36:
        cut = cut[:last]
    return cut.rstrip("，、；; ") + "…"


def _clean_weekly_key_points(raw_points: List[str]) -> List[str]:
    points = []
    for p in raw_points or []:
        s = _trim_weekly_point(p, max_len=WEEKLY_KEYPOINT_POINT_MAX_LEN)
        if not s:
            continue
        if s in points:
            continue
        points.append(s)
        if len(points) >= WEEKLY_KEYPOINT_MAX_POINTS:
            break
    return points


def _parse_weekly_gemini_points(output_text: str) -> List[str]:
    return _clean_weekly_key_points(_parse_raw_points_from_llm(output_text))


def _build_weekly_expansion_points(ctx: dict, stock_name: str) -> List[str]:
    """Gemini 本週重點太短時，用同一份技術面 / 權證資料補足資訊量。"""
    points = []
    try:
        df = ctx.get("plot_df", pd.DataFrame())
        latest = df.iloc[-1] if df is not None and not df.empty else pd.Series(dtype=float)
        close = _safe_float(latest.get("Close"))
        ma5 = _safe_float(latest.get("MA5"))
        ma20 = _safe_float(latest.get("MA20"))
        ma60 = _safe_float(latest.get("MA60"))
        vol = _safe_float(latest.get("Volume"))
        mv20 = _safe_float(latest.get("MV20"))
        vol_ratio = vol / mv20 if mv20 and np.isfinite(mv20) and mv20 > 0 else np.nan
        ma_signal = get_ma_kline_signals(df) if df is not None and not df.empty else ""
        kd_signal = get_kd_signals(df) if df is not None and not df.empty else ""
        macd_signal = get_macd_signals(df) if df is not None and not df.empty else ""
        if np.isfinite(close):
            points.append(
                f"股價本週 {fmt_pct(ctx.get('stock_ret', np.nan))}，最新收盤 {close:.0f}，目前與 5MA {ma5:.1f}、20MA {ma20:.1f}、60MA {ma60:.1f} 的相對位置，搭配 {ma_signal or '均線結構'} 判斷短中期趨勢。"
            )
        if np.isfinite(vol_ratio):
            points.append(
                f"量能面本週較前週 {fmt_pct(ctx.get('vol_change', np.nan))}，最新日約為月均量 {vol_ratio:.1f} 倍，需搭配 {kd_signal or 'KD'} 與 {macd_signal or 'MACD'} 觀察動能是否延續。"
            )
        total_net = float(ctx.get("total_net", 0) or 0)
        total_buy = float(ctx.get("total_buy", 0) or 0)
        total_sell = float(ctx.get("total_sell", 0) or 0)
        points.append(
            f"權證資金流本週買進 {fmt_money_abs(total_buy)}、賣出 {fmt_money(-abs(total_sell))}，合計淨流向 {fmt_money(total_net)}（{ctx.get('bias', '')}），可觀察資金是否與股價方向一致。"
        )
        e = ctx.get("week_events")
        if e is not None and not e.empty:
            by_branch = e.groupby("branch")["net_amount"].sum().sort_values(ascending=False)
            top_buy = str(by_branch.index[0]) if len(by_branch) else ""
            top_buy_amt = float(by_branch.iloc[0]) if len(by_branch) else 0.0
            top_sell = str(by_branch.index[-1]) if len(by_branch) else ""
            top_sell_amt = float(by_branch.iloc[-1]) if len(by_branch) else 0.0
            points.append(
                f"分點結構以「{top_buy}」買超 {fmt_money(top_buy_amt)} 與「{top_sell}」賣超 {fmt_money(top_sell_amt)} 最明顯，若買賣集中度升高，代表籌碼方向更需要追蹤。"
            )
    except Exception:
        pass
    return _clean_weekly_key_points(points)


def _ensure_weekly_keypoint_min_total(points: List[str], ctx: dict, stock_name: str) -> List[str]:
    points = _clean_weekly_key_points(points)
    if _count_summary_chars(points) >= WEEKLY_KEYPOINT_MIN_TOTAL_CHARS and len(points) >= min(WEEKLY_KEYPOINT_MIN_POINTS, WEEKLY_KEYPOINT_MAX_POINTS):
        return points[:WEEKLY_KEYPOINT_MAX_POINTS]

    expanded = points[:]
    for p in _build_weekly_expansion_points(ctx, stock_name):
        if len(expanded) >= WEEKLY_KEYPOINT_MAX_POINTS:
            break
        if p not in expanded:
            expanded.append(p)
        if _count_summary_chars(expanded) >= WEEKLY_KEYPOINT_MIN_TOTAL_CHARS and len(expanded) >= min(WEEKLY_KEYPOINT_MIN_POINTS, WEEKLY_KEYPOINT_MAX_POINTS):
            break

    if _count_summary_chars(expanded) >= WEEKLY_KEYPOINT_MIN_TOTAL_CHARS:
        return expanded[:WEEKLY_KEYPOINT_MAX_POINTS]

    # 如果點數已滿但仍太短，嘗試用較完整的規則式重點替換較短項目。
    for cand in _build_weekly_expansion_points(ctx, stock_name):
        if not expanded:
            expanded.append(cand)
        else:
            shortest_idx = min(range(len(expanded)), key=lambda i: len(expanded[i]))
            if len(cand) > len(expanded[shortest_idx]) and cand not in expanded:
                expanded[shortest_idx] = cand
        if _count_summary_chars(expanded) >= WEEKLY_KEYPOINT_MIN_TOTAL_CHARS:
            break
    return expanded[:WEEKLY_KEYPOINT_MAX_POINTS]


def build_key_points(ctx, stock_name: str):
    """本週重點：優先交給 Gemini 讀取權證資金流與技術面資料後統整；失敗則走原本規則式重點。"""
    ai_points = _summarize_weekly_context_with_gemini(ctx, stock_name)
    if ai_points:
        return _ensure_weekly_keypoint_min_total(ai_points, ctx, stock_name)
    return _ensure_weekly_keypoint_min_total(_rule_based_key_points(ctx, stock_name), ctx, stock_name)


# ============================================================
# 新聞抓取：抓一週內新聞內文並整理成真正重點
# ============================================================

NEWS_BODY_MAX_CHARS = int(os.getenv("WARRANT_NEWS_BODY_MAX_CHARS", "3500"))
NEWS_FETCH_TIMEOUT = float(os.getenv("WARRANT_NEWS_FETCH_TIMEOUT", "10"))
NEWS_SUMMARY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_SUMMARY_MAX_POINTS", "3"))
NEWS_DISPLAY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_DISPLAY_MAX_POINTS", "3"))
NEWS_SUMMARY_POINT_MAX_LEN = int(os.getenv("WARRANT_NEWS_SUMMARY_POINT_MAX_LEN", "90"))
NEWS_SUMMARY_MIN_TOTAL_CHARS = int(os.getenv("WARRANT_NEWS_SUMMARY_MIN_TOTAL_CHARS", "150"))
NEWS_SUMMARY_MIN_POINTS = int(os.getenv("WARRANT_NEWS_SUMMARY_MIN_POINTS", "2"))
# 只用真正抓到的新聞內文產生摘要；不要把 RSS 標題或導流摘要直接當成重點。
NEWS_MIN_BODY_CHARS = int(os.getenv("WARRANT_NEWS_MIN_BODY_CHARS", "260"))
# 預設：優先用新聞原文；若原文被擋，允許用 RSS 摘要文字「改寫成重點」，但不直接輸出標題。
NEWS_REQUIRE_ARTICLE_BODY = os.getenv("WARRANT_NEWS_REQUIRE_BODY", "0").strip().lower() not in ("0", "false", "no", "off")
NEWS_RSS_DESCRIPTION_FALLBACK = os.getenv("WARRANT_NEWS_RSS_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")
NEWS_OPENAI_ENABLE = os.getenv("WARRANT_NEWS_OPENAI_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
NEWS_OPENAI_MODEL = os.getenv("WARRANT_NEWS_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
# Gemini / LLM 設定：GitHub Actions 請設定 Repository Secret / Variable：WARRANTS_API_KEY
GEMINI_ENABLE = os.getenv("WARRANT_GEMINI_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
GEMINI_RETRY_TIMES = int(os.getenv("WARRANT_GEMINI_RETRY_TIMES", "5"))
GEMINI_RETRY_BASE_WAIT = float(os.getenv("WARRANT_GEMINI_RETRY_BASE_WAIT", "4"))
NEWS_MAX_ARTICLES_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLES_TO_GEMINI", "8"))
NEWS_MAX_ARTICLE_CHARS_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLE_CHARS_TO_GEMINI", "3500"))
WEEKLY_KEYPOINT_LLM_ENABLE = os.getenv("WARRANT_WEEKLY_KEYPOINT_LLM_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
WEEKLY_KEYPOINT_MAX_POINTS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MAX_POINTS", "3"))
WEEKLY_KEYPOINT_POINT_MAX_LEN = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_POINT_MAX_LEN", "90"))
WEEKLY_KEYPOINT_MIN_TOTAL_CHARS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MIN_TOTAL_CHARS", "150"))
WEEKLY_KEYPOINT_MIN_POINTS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MIN_POINTS", "3"))
# 新聞抓取速度版：只抓 Google News 重要新聞，不再掃 PTT，避免 GitHub Actions 執行時間過長。
NEWS_GOOGLE_MAX_ITEMS = int(os.getenv("WARRANT_NEWS_GOOGLE_MAX_ITEMS", "8"))

STOCK_NEWS_ALIAS_MAP = {
    "2330": ["台積電", "GG", "護國神山"],
    "2317": ["鴻海", "海公公"],
    "2408": ["南亞科", "牙科"],
    "2344": ["華邦電", "華崩"],
    "2454": ["聯發科", "發哥", "MTK"],
    "2303": ["聯電", "UMC"],
    "2308": ["台達電"],
    "2412": ["中華電"],
    "2357": ["華碩"],
    "2382": ["廣達"],
    "3231": ["緯創"],
    "6669": ["緯穎"],
    "3661": ["世芯", "世芯-KY"],
    "3037": ["欣興"],
    "3260": ["威剛"],
    "2379": ["瑞昱"],
    "3034": ["聯詠"],
    "3035": ["智原"],
    "3443": ["創意"],
    "3529": ["力旺"],
    "3653": ["健策"],
    "3665": ["貿聯-KY", "貿聯"],
    "5274": ["信驊"],
    "4966": ["譜瑞-KY", "譜瑞"],
    "6515": ["穎崴"],
    "6223": ["旺矽"],
    "6643": ["M31"],
    "6781": ["AES-KY", "AES"],
    "6789": ["采鈺"],
    "6770": ["力積電"],
    "6531": ["愛普"],
    "2337": ["旺宏"],
    "8299": ["群聯"],
}


def _clean_news_title(title: str) -> str:
    s = html.unescape(str(title or "")).strip()
    s = re.sub(r"\s+", " ", s)
    # Google News RSS 常見格式為「標題 - 來源」，這裡移除最後來源字樣，避免圖片右下角太冗長。
    s = re.sub(r"\s+-\s+[^-]{1,40}$", "", s).strip()
    s = _remove_news_boilerplate(s)
    return s


def _html_to_readable_text(raw_html: str) -> str:
    """將 HTML 粗略轉成可讀文字；不依賴 BeautifulSoup，避免 GitHub Actions 缺套件。"""
    if not raw_html:
        return ""
    text = str(raw_html)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<svg[^>]*>.*?</svg>", " ", text)
    text = re.sub(r"(?is)<(br|/p|/div|/li|/h[1-6]|/article|/section|/main)\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _remove_news_boilerplate(text: str) -> str:
    """移除 RSS 摘要、新聞網頁常見的導流字與雜訊，避免出現「完整看」等字樣。"""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"完整看[^。！？；;\n]*", " ", s)
    s = re.sub(r"全文見[^。！？；;\n]*", " ", s)
    s = re.sub(r"更多[^。！？；;\n]*", " ", s)
    s = re.sub(r"看更多[^。！？；;\n]*", " ", s)
    s = re.sub(r"延伸閱讀[^。！？；;\n]*", " ", s)
    s = re.sub(r"相關新聞[^。！？；;\n]*", " ", s)
    s = re.sub(r"熱門新聞[^。！？；;\n]*", " ", s)
    s = re.sub(r"推薦閱讀[^。！？；;\n]*", " ", s)
    s = re.sub(r"請繼續往下閱讀[^。！？；;\n]*", " ", s)
    s = re.sub(r"ADVERTISEMENT[^。！？；;\n]*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"Copyright[^。！？；;\n]*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"版權所有[^。！？；;\n]*", " ", s)
    s = re.sub(r"不得轉載[^。！？；;\n]*", " ", s)
    s = re.sub(r"加入會員[^。！？；;\n]*", " ", s)
    s = re.sub(r"下載APP[^。！？；;\n]*", " ", s)
    s = re.sub(r"APP下載[^。！？；;\n]*", " ", s)
    s = re.sub(r"登入[^。！？；;\n]*", " ", s)
    s = re.sub(r"訂閱[^。！？；;\n]*", " ", s)
    s = re.sub(r"Google News", " ", s)
    s = re.sub(r"Yahoo奇摩", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_news_text(text: str) -> str:
    if not text:
        return ""
    s = html.unescape(str(text))
    if "<" in s and ">" in s:
        s = _html_to_readable_text(s)
    s = s.replace("\u3000", " ")
    s = s.replace("\u200b", " ").replace("\ufeff", " ")
    s = re.sub(r"https?://\S+", " ", s)
    s = _remove_news_boilerplate(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _title_compare_text(text: str) -> str:
    s = _normalize_news_text(text)
    s = re.sub(r'''[\s，。！？；：、,.!?;:()（）\[\]【】《》〈〉『』「」"\'’‘“”|｜\-–—_]+''', "", s)
    return s


def _char_overlap_ratio(a: str, b: str) -> float:
    aa = set(_title_compare_text(a))
    bb = set(_title_compare_text(b))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, min(len(aa), len(bb)))


def _looks_like_news_headline(text: str, title: str = "") -> bool:
    """判斷句子是否比較像新聞標題或導流摘要，而不是可整理的內文。"""
    s = _normalize_news_text(text)
    if not s:
        return True
    headline_marks = ["焦點股", "個股", "優於大盤", "新目標價", "目標價曝光", "上看", "爆漲", "飆漲", "強勢股", "題材股"]
    if any(k in s for k in headline_marks) and len(s) <= 70:
        return True
    if any(mark in s for mark in ["》", "｜", "|", "【", "】"] ) and len(s) <= 90:
        return True
    if title:
        tc = _title_compare_text(title)
        sc = _title_compare_text(s)
        if tc and sc:
            # 短句與標題高度相似才視為標題；完整內文常會包含標題，不能因此整篇丟掉。
            if len(s) <= 120 and (sc in tc or tc in sc):
                return True
            if len(s) <= 90 and _char_overlap_ratio(s, title) >= 0.72:
                return True
    return False


def _is_valid_article_body(body: str, title: str = "", description: str = "") -> bool:
    """確認抓到的是新聞內文，而不是 RSS 標題、摘要或網站導流文字。"""
    body = _normalize_news_text(body)
    title = _clean_news_title(title)
    description = _normalize_news_text(description)
    if len(body) < NEWS_MIN_BODY_CHARS:
        return False
    if _looks_like_news_headline(body, title):
        return False
    # 若抓到的是 meta description 或 RSS 摘要，仍可視為備援素材，但不當成完整原文。
    if description and len(body) <= max(len(description) + 40, 260) and _char_overlap_ratio(body, description) >= 0.80:
        return False
    sentence_count = len(re.findall(r"[。！？!?；;]", body))
    if sentence_count < 1 and len(body) < 260:
        return False
    bad_ratio_hits = len(re.findall(r"完整看|看更多|延伸閱讀|相關新聞|熱門新聞|三大法人買賣超|買超排行|賣超排行", body))
    if bad_ratio_hits >= 2 and len(body) < 500:
        return False
    return True


def _is_valid_news_fallback_text(text: str, title: str = "", stock_code: str = "", stock_name: str = "") -> bool:
    """當原文頁擋爬蟲時，判斷 RSS 摘要是否可作為改寫素材；不直接輸出這段文字。"""
    s = _normalize_news_text(text)
    title = _clean_news_title(title)
    if len(s) < 42:
        return False
    if _is_bad_news_sentence(s):
        return False
    if _looks_like_news_headline(s, title) and len(s) < 95:
        return False
    if title and _char_overlap_ratio(s, title) >= 0.88 and len(s) <= len(title) + 30:
        return False
    topic_keywords = [
        stock_code, stock_name, "營收", "財報", "獲利", "EPS", "毛利", "法說", "展望", "接單", "出貨", "產能",
        "AI", "伺服器", "記憶體", "DRAM", "NAND", "半導體", "報價", "HBM", "法人", "目標價", "評等", "需求", "漲價",
    ]
    return any(k and k in s for k in topic_keywords)


def _walk_json_objects(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_json_objects(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json_objects(v)


def _extract_json_ld_article_body(page_html: str) -> str:
    """優先從 JSON-LD 的 articleBody / description 擷取新聞內文。"""
    if not page_html:
        return ""
    bodies = []
    pattern = r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    for m in re.finditer(pattern, page_html):
        raw = html.unescape(m.group(1)).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _walk_json_objects(data):
            body = obj.get("articleBody") or obj.get("description") or ""
            if isinstance(body, str):
                body = _normalize_news_text(body)
                if len(body) >= 80:
                    bodies.append(body)
    if not bodies:
        return ""
    return max(bodies, key=len)

def _extract_meta_descriptions_from_html(page_html: str) -> List[str]:
    """抓取 og:description / meta description，作為新聞原文被擋時的備援摘要來源。"""
    if not page_html:
        return []
    metas = []
    patterns = [
        r'(?is)<meta[^>]+(?:property|name)=["\'](?:og:description|twitter:description|description)["\'][^>]+content=["\'](.*?)["\'][^>]*>',
        r'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\'](?:og:description|twitter:description|description)["\'][^>]*>',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, page_html):
            txt = _normalize_news_text(m.group(1))
            if len(txt) >= 40 and txt not in metas:
                metas.append(txt)
    return metas


def _extract_article_text_from_html(page_html: str) -> str:
    """從新聞頁 HTML 取出最像內文的文字；優先使用 JSON-LD 與 BeautifulSoup，邏輯接近獨立 Gemini 新聞測試程式。"""
    if not page_html:
        return ""

    json_body = _extract_json_ld_article_body(page_html)
    if len(json_body) >= NEWS_MIN_BODY_CHARS:
        return json_body[:NEWS_BODY_MAX_CHARS]

    candidates = []

    # 優先用 BeautifulSoup / lxml 解析，抓 article、main、常見新聞內容容器與 p 段落。
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(page_html, "lxml")

            for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
                tag.decompose()

            selectors = [
                "article",
                "main",
                '[data-test-locator="articleBody"]',
                '[class*="article"]',
                '[class*="content"]',
                '[class*="story"]',
                '[class*="news"]',
                '[class*="post"]',
                '[class*="entry"]',
                '[class*="body"]',
                '[class*="text"]',
                '[class*="paragraph"]',
                '[id*="article"]',
                '[id*="content"]',
                '[id*="story"]',
                '[id*="news"]',
                '[id*="body"]',
            ]

            for selector in selectors:
                for node in soup.select(selector)[:12]:
                    txt = _normalize_news_text(node.get_text(" "))
                    if len(txt) >= NEWS_MIN_BODY_CHARS:
                        candidates.append(txt)

            paragraphs = []
            for p_tag in soup.find_all("p"):
                txt = _normalize_news_text(p_tag.get_text(" "))
                if 24 <= len(txt) <= 450 and not _is_bad_news_sentence(txt):
                    paragraphs.append(txt)
            if len(paragraphs) >= 3:
                candidates.append("。".join(paragraphs))

            meta_attrs = [
                {"property": "og:description"},
                {"name": "description"},
                {"name": "twitter:description"},
            ]
            for attr in meta_attrs:
                meta = soup.find("meta", attrs=attr)
                if meta and meta.get("content"):
                    txt = _normalize_news_text(meta.get("content"))
                    if len(txt) >= 120:
                        candidates.append(txt)
        except Exception as e:
            print(f"⚠️ BeautifulSoup 解析新聞內文失敗，改用正則備援：{e}")

    # 備援：不依賴 BeautifulSoup 的粗略解析，避免環境缺套件時完全抓不到。
    for tag in ["article", "main"]:
        for m in re.finditer(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", page_html):
            txt = _normalize_news_text(_html_to_readable_text(m.group(1)))
            if len(txt) >= NEWS_MIN_BODY_CHARS:
                candidates.append(txt)

    for m in re.finditer(r'(?is)<(?:div|section)[^>]+(?:class|id)=["\'][^"\']*(?:article|content|story|news|post|entry|text|paragraph|body|main|cnt|article-body|article_content)[^"\']*["\'][^>]*>(.*?)</(?:div|section)>', page_html):
        txt = _normalize_news_text(_html_to_readable_text(m.group(1)))
        if len(txt) >= NEWS_MIN_BODY_CHARS:
            candidates.append(txt)

    paragraphs = []
    for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", page_html):
        txt = _normalize_news_text(_html_to_readable_text(m.group(1)))
        if 24 <= len(txt) <= 450 and not _is_bad_news_sentence(txt):
            paragraphs.append(txt)
    if len(paragraphs) >= 3:
        candidates.append("。".join(paragraphs))

    # 原文被擋時，meta description 至少比標題更接近內文摘要；後續只作備援，不直接當正式內文。
    for meta_txt in _extract_meta_descriptions_from_html(page_html):
        if len(meta_txt) >= 120:
            candidates.append(meta_txt)

    if candidates:
        return max(candidates, key=len)[:NEWS_BODY_MAX_CHARS]

    fallback = _normalize_news_text(_html_to_readable_text(page_html))
    if len(fallback) >= NEWS_MIN_BODY_CHARS:
        return fallback[:NEWS_BODY_MAX_CHARS]
    return ""

def _decode_google_news_url_from_path(url: str) -> str:
    """先嘗試從 Google News RSS encoded path 直接解出原始新聞網址。"""
    try:
        parsed = urllib.parse.urlparse(url or "")
        if "news.google.com" not in parsed.netloc or "/articles/" not in parsed.path:
            return ""
        encoded = parsed.path.split("/articles/", 1)[1].split("/", 1)[0]
        encoded = encoded.split("?", 1)[0]
        if not encoded:
            return ""
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("utf-8"))
        text = raw.decode("latin1", errors="ignore")
        m = re.search(r"https?://[^\x00-\x20\"'<>]+", text)
        if m:
            return html.unescape(m.group(0)).strip()
    except Exception:
        pass
    return ""


def _maybe_resolve_google_news_link(url: str) -> str:
    """Google News RSS 有時是跳轉頁；這裡嘗試解析成原始新聞網址。"""
    if not url or "news.google.com" not in url:
        return url or ""
    decoded_url = _decode_google_news_url_from_path(url)
    if decoded_url:
        return decoded_url
    if gnewsdecoder is not None:
        try:
            decoded = gnewsdecoder(url, interval=1)
            if isinstance(decoded, dict):
                real = decoded.get("decoded_url", "")
                if real and str(real).startswith("http"):
                    return str(real).strip()
            elif isinstance(decoded, str) and decoded.startswith("http"):
                return decoded.strip()
        except Exception as e:
            print(f"⚠️ googlenewsdecoder 解碼失敗：{e}")
    try:
        headers = {
            "User-Agent": HDR["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://news.google.com/",
        }
        r = get_thread_session().get(url, headers=headers, timeout=(5, NEWS_FETCH_TIMEOUT), allow_redirects=True)
        final_url = str(r.url or "").strip()
        if final_url and "news.google.com" not in final_url:
            return final_url
        hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', r.text or "")
        for h in hrefs:
            h = html.unescape(h)
            if "news.google.com" not in h and "google.com" not in h:
                return h
    except Exception:
        pass
    return url


def _fetch_article_body(url: str) -> str:
    """嘗試進入新聞原文頁抓內文；失敗時回傳空字串。"""
    if not url:
        return ""
    try:
        final_url = _maybe_resolve_google_news_link(url)
        headers = {
            "User-Agent": HDR["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://news.google.com/",
        }
        r = get_thread_session().get(final_url, headers=headers, timeout=(5, NEWS_FETCH_TIMEOUT), allow_redirects=True)
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type and not r.text.lstrip().startswith("<"):
            return ""
        body = _extract_article_text_from_html(r.text)
        if body and len(body) >= 80:
            return body
    except Exception as e:
        print(f"⚠️ 新聞內文抓取失敗：{url}｜{e}")
    return ""


def _get_news_aliases(stock_code: str, stock_name: str) -> List[str]:
    aliases = []
    for a in [stock_code, stock_name]:
        a = str(a or "").strip()
        if a and a not in aliases:
            aliases.append(a)
    for a in STOCK_NEWS_ALIAS_MAP.get(str(stock_code).strip(), []):
        a = str(a or "").strip()
        if a and a not in aliases:
            aliases.append(a)
    return aliases


def _parse_rss_pub_date(pub_date: str):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _is_within_recent_days_from_rss(pub_date: str, days: int = 7) -> bool:
    dt = _parse_rss_pub_date(pub_date)
    if dt is None:
        return True
    return dt >= datetime.now() - timedelta(days=days)



def fetch_google_news_articles(stock_code: str, stock_name: str, max_items: int = 10) -> List[dict]:
    """
    抓取最近一週 Google News RSS 新聞，再嘗試進入原文頁擷取內文。
    回傳 dict 格式，讓後續 build_news_points 可以根據內文整理重點。
    """
    manual = os.getenv("WEEKLY_NEWS_TEXT", "").strip()
    if manual:
        parts = [x.strip() for x in re.split(r"[\n；;]+", manual) if x.strip()]
        return [{
            "title": "手動新聞重點",
            "url": "",
            "source": "manual",
            "published": "",
            "description": "",
            "content": p,
            "body_ok": True,
            "body_length": len(p),
        } for p in parts[:max_items]]

    if not NEWS_ENABLE:
        return []

    aliases = _get_news_aliases(stock_code, stock_name)
    strict_part = " OR ".join([f'"{a}"' for a in aliases[:5]]) if aliases else f'"{stock_code}"'
    query = (
        f'({strict_part}) '
        f'(營收 OR 財報 OR 獲利 OR 法說 OR 展望 OR 接單 OR 出貨 OR 產能 OR AI OR 伺服器 OR 記憶體 OR DRAM OR 半導體 OR 報價 OR HBM OR 法人 OR 目標價 OR 評等 OR EPS OR ASP) '
        # 不再排除「焦點股 / 漲停 / 強漲」等字眼，因為很多重要新聞標題會包含這些詞；
        # 後續會用內文與本股票關聯性過濾，不會直接把標題放進重點。
        f'-三大法人 -買賣超 -排行 -完整看 when:7d'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query,
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    })

    try:
        r = requests.get(url, headers={"User-Agent": HDR["User-Agent"]}, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        articles = []
        seen_titles = set()
        scanned = 0

        for item in root.findall(".//item"):
            scanned += 1
            title = _clean_news_title(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            source = (source_el.text if source_el is not None and source_el.text else "").strip()
            description = _normalize_news_text(_html_to_readable_text(item.findtext("description") or ""))

            if not _is_within_recent_days_from_rss(published, days=7):
                continue
            if not title or title in seen_titles:
                continue
            combined_for_target_check = f"{title} {description}"
            if aliases and not any(alias in combined_for_target_check for alias in aliases):
                # Google News 搜尋有時會回傳同產業但非本股票的多股新聞；先擋掉標題/摘要完全沒有本股票的項目。
                continue
            seen_titles.add(title)

            article_body = _fetch_article_body(link)
            body_ok = _is_valid_article_body(article_body, title=title, description=description)

            # 重點：優先使用原文內文；若新聞站擋爬蟲，才用 RSS 摘要當「改寫素材」，不直接輸出標題。
            fallback_ok = False
            content_source = "article" if body_ok else ""
            if body_ok:
                content = article_body
            else:
                fallback_ok = NEWS_RSS_DESCRIPTION_FALLBACK and _is_valid_news_fallback_text(description, title, stock_code, stock_name)
                content = description if fallback_ok else ""
                content_source = "rss_description" if fallback_ok else ""

            articles.append({
                "title": title,
                "url": link,
                "source": source,
                "published": published,
                "description": description,
                "content": content,
                "body_ok": body_ok,
                "fallback_ok": fallback_ok,
                "content_source": content_source,
                "body_length": len(article_body or ""),
            })
            status = "原文可摘要" if body_ok else "RSS摘要改寫" if fallback_ok else "略過標題"
            print(f"📰 新聞抓取：{title[:36]}｜原文 {len(article_body or ''):,} 字｜{status}")

            # 為了避免前面幾篇都抓不到內文，RSS 可以多掃一些；只用可摘要內文做重點。
            if len(articles) >= max_items and sum(1 for a in articles if a.get("body_ok") or a.get("fallback_ok")) >= max(2, min(4, NEWS_SUMMARY_MAX_POINTS)):
                break
            if scanned >= max_items * 4:
                break

        return articles
    except Exception as e:
        print(f"⚠️ Google News RSS 抓取失敗：{e}")
        return []


def fetch_google_news_titles(stock_code: str, stock_name: str, max_items: int = 5) -> List[str]:
    """保留舊函式相容性；新流程請優先使用 fetch_google_news_articles。"""
    articles = fetch_google_news_articles(stock_code, stock_name, max_items=max_items)
    titles = []
    for a in articles:
        if isinstance(a, dict):
            title = _clean_news_title(a.get("title", ""))
            if title:
                titles.append(title)
        else:
            title = _clean_news_title(str(a))
            if title:
                titles.append(title)
    return titles[:max_items]


def _news_items_to_records(news_items) -> List[dict]:
    records = []
    for item in news_items or []:
        if isinstance(item, dict):
            title = _clean_news_title(item.get("title", ""))
            description = _normalize_news_text(item.get("description", ""))
            source = str(item.get("source", "") or "").strip()
            published = str(item.get("published", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            body_ok = bool(item.get("body_ok"))
            fallback_ok = bool(item.get("fallback_ok"))
            content_source = str(item.get("content_source", "") or "").strip()
            raw_content = _normalize_news_text(item.get("content", ""))
            content = raw_content if (body_ok or fallback_ok) else ""
        else:
            # 舊版相容：純字串只當標題，不拿來產生新聞重點。
            title = _clean_news_title(str(item))
            content = ""
            description = ""
            source = ""
            published = ""
            url = ""
            body_ok = False
            fallback_ok = False
            content_source = ""
        if not title and not content and not description:
            continue
        records.append({
            "title": title,
            "content": content,
            "description": description,
            "source": source,
            "published": published,
            "url": url,
            "body_ok": body_ok,
            "fallback_ok": fallback_ok,
            "content_source": content_source,
        })
    return records


def _is_bad_news_sentence(sentence: str) -> bool:
    """過濾新聞標題、三大法人清單、導流文字與非內文內容。"""
    s = _normalize_news_text(sentence)
    if not s or len(s) < 16:
        return True
    bad_keywords = [
        "完整看", "三大法人買賣超", "外資買超", "外資賣超", "投信買超", "投信賣超",
        "自營商買超", "自營商賣超", "買超排行", "賣超排行", "熱門股", "熱門新聞",
        "新聞標題", "點擊", "下載", "加入會員", "登入", "訂閱", "廣告", "版權",
        "看更多", "更多新聞", "延伸閱讀", "相關新聞", "Yahoo", "Facebook", "LINE分享",
        "焦點股", "優於大盤", "目標價曝光", "新目標價", "強勢股", "題材股",
        "關鍵字", "標籤", "追蹤我們", "追蹤我", "追蹤", "分享給朋友", "分享給好友",
        "分享本文", "本文", "※本文", "免責聲明", "投稿", "留言", "按讚",
        "SETN", "UDN", "自由財經", "中時新聞", "工商時報", "經濟日報", "鉅亨網", "MoneyDJ",
    ]
    if any(k in s for k in bad_keywords):
        return True
    if re.search(r"關鍵字[:：]|標籤[:：]|追蹤我們|分享給朋友|分享給好友|分享本文|※本文|免責聲明", s):
        return True
    if re.search(r"[》｜|【】]", s) and len(s) <= 100:
        return True
    code_count = len(re.findall(r"\(?\d{4}\)?", s))
    if code_count >= 3:
        return True
    if re.search(r"(外資|投信|自營商).{0,12}(買超|賣超).{0,80}\(?\d{4}\)?", s):
        return True
    if s.count("、") >= 5 and code_count >= 2:
        return True
    if re.search(r"^[0-9]{4}\s*[^，。；;]{1,24}[！!？?]?$", s):
        return True
    return False


def _get_non_target_stock_aliases(stock_code: str, stock_name: str) -> List[str]:
    """取得已知的非本股票名稱 / 代號，避免 LLM 把其他公司的目標價或財務數字誤植到本股票。"""
    target_aliases = set(_get_news_aliases(stock_code, stock_name))
    target_aliases.update({str(stock_code or "").strip(), str(stock_name or "").strip()})
    common_company_aliases = [
        "台積電", "聯發科", "瑞昱", "聯詠", "智原", "創意", "世芯", "世芯-KY", "信驊",
        "穎崴", "旺矽", "力旺", "譜瑞", "譜瑞-KY", "力積電", "南亞科", "華邦電",
        "旺宏", "群聯", "威剛", "愛普", "台達電", "廣達", "緯創", "緯穎", "鴻海",
        "欣興", "健策", "貿聯", "貿聯-KY", "M31", "采鈺", "印能", "辛耘", "弘塑",
        "台光電", "金像電", "台燿", "臻鼎", "景碩", "矽力", "矽力-KY",
    ]
    aliases = []
    for code, names in STOCK_NEWS_ALIAS_MAP.items():
        all_names = [code] + list(names or [])
        for name in all_names:
            name = str(name or "").strip()
            if not name or name in target_aliases:
                continue
            if len(name) < 3 and not re.fullmatch(r"\d{4}", name):
                continue
            if name not in aliases:
                aliases.append(name)
    for name in common_company_aliases:
        name = str(name or "").strip()
        if not name or name in target_aliases:
            continue
        if name not in aliases:
            aliases.append(name)
    return aliases

def _contains_non_target_stock_alias(text: str, stock_code: str, stock_name: str) -> bool:
    s = _normalize_news_text(text)
    if not s:
        return False
    return any(alias and alias in s for alias in _get_non_target_stock_aliases(stock_code, stock_name))


def _is_cross_company_target_value_sentence(text: str, stock_code: str, stock_name: str) -> bool:
    """
    避免多家公司新聞中，將其他公司的目標價 / 評等數字誤歸給本股票。
    例如聯發科報告中若出現「台積電目標價 3000 元」，這句不能進入聯發科新聞重點。
    """
    s = _normalize_news_text(text)
    if not s:
        return False
    value_terms = ["目標價", "評等", "升評", "降評", "調升", "調降", "上看", "喊到", "喊出"]
    if not any(term in s for term in value_terms):
        return False
    if not _contains_non_target_stock_alias(s, stock_code, stock_name):
        return False
    # 若同一重點同時提到其他公司與目標價，寧可略過，避免將台積電 / 瑞昱等公司的數字誤放到本股票。
    return True


def _split_news_clauses(sentence: str) -> List[str]:
    s = _normalize_news_text(sentence)
    if not s:
        return []
    parts = re.split(r"(?<=[，,；;、])\s*", s)
    out = []
    for part in parts:
        part = _normalize_news_text(part).strip("，,；;、 ")
        if part:
            out.append(part)
    return out


def _strip_target_news_label(sentence: str) -> str:
    """移除會影響判斷的標題式前綴，但保留後面的真正內文。"""
    s = _normalize_news_text(sentence)
    s = re.sub(r"^(焦點股|個股|強勢股|題材股|盤中|盤後|台股)[:：｜|]?\s*", "", s).strip()
    return s


def _has_company_value_terms(text: str) -> bool:
    """判斷句子是否含有與公司基本面或股價可能有關的資訊。"""
    s = _normalize_news_text(text)
    return bool(re.search(
        r"目標價|評等|升評|降評|調升|調降|EPS|每股純益|營收|月增|年增|毛利|毛利率|獲利|虧損|轉盈|ASP|報價|漲價|供需|需求|接單|出貨|產能|長約|法說|展望|AI|伺服器|半導體|記憶體|DRAM|HBM|NAND|測試|探針卡|載板|PCB|先進封裝|CoWoS|客戶|訂單",
        s,
    ))


def _is_safe_target_context_sentence(text: str, stock_code: str, stock_name: str) -> bool:
    """判斷沒有明確股票名稱的承接句是否可安全保留為本股票上下文。"""
    s = _strip_target_news_label(text)
    if not s:
        return False
    if _is_cross_company_target_value_sentence(s, stock_code, stock_name):
        return False
    # 若承接句同時出現其他公司與容易混用的數字 / 題材，直接排除。
    if _contains_non_target_stock_alias(s, stock_code, stock_name) and re.search(
        r"目標價|評等|EPS|每股純益|營收|毛利|獲利|預估|上看|調升|調降|ASP|報價|記憶體|DRAM|HBM|PCB|載板|伺服器|AI",
        s,
    ):
        return False
    if _is_bad_news_sentence(s) and not _has_company_value_terms(s):
        return False
    return _has_company_value_terms(s) or bool(re.search(r"^(該公司|公司|其|法人|市場|報告|預估|預期|因此|由於|受惠|展望)", s))


def _extract_target_focused_news_body(content: str, stock_code: str, stock_name: str) -> str:
    """
    多家公司新聞常同時提到台積電、聯發科、瑞昱、記憶體股等不同主題。
    送入 Gemini 前先壓成「本股票明確相關片段」。

    這版改成「嚴格防混用，但不要過度嚴格到完全抓不到」：
    1. 明確提到本股票的句子會保留。
    2. 本股票句子的前後承接句，若沒有其他公司名且含基本面 / 產業關鍵資訊，也會保留。
    3. 目標價、EPS、營收、ASP、報價、產業題材若出現其他公司名稱，仍會排除。
    """
    aliases = [a for a in _get_news_aliases(stock_code, stock_name) if a]
    content = _normalize_news_text(content)
    if not content or not aliases:
        return content

    sentences = _split_news_sentences(content)
    selected = []
    seen = set()
    context_window = 0

    def add_sentence(raw_sent: str):
        sent = _strip_target_news_label(raw_sent)
        sent = sent.strip("。；;，, ")
        if not sent or sent in seen:
            return
        if _is_cross_company_target_value_sentence(sent, stock_code, stock_name):
            return
        if _contains_non_target_stock_alias(sent, stock_code, stock_name) and re.search(
            r"目標價|評等|EPS|每股純益|營收|毛利|獲利|預估|上看|調升|調降|ASP|報價|記憶體|DRAM|HBM|PCB|載板|伺服器|AI",
            sent,
        ):
            return
        if _is_bad_news_sentence(sent) and not any(alias in sent for alias in aliases) and not _has_company_value_terms(sent):
            return
        selected.append(sent)
        seen.add(sent)

    for i, sent in enumerate(sentences):
        if not sent:
            continue
        sent = _strip_target_news_label(sent)
        has_target = any(alias in sent for alias in aliases)
        has_non_target = _contains_non_target_stock_alias(sent, stock_code, stock_name)
        has_cross_risk = _is_cross_company_target_value_sentence(sent, stock_code, stock_name)

        if has_target:
            # 先嘗試拆分句，避免「A 公司目標價、B 公司目標價」混在同一句。
            clauses = []
            for clause in _split_news_clauses(sent):
                clause = _strip_target_news_label(clause).strip("，,；;、 ")
                if not clause:
                    continue
                clause_has_target = any(alias in clause for alias in aliases)
                clause_has_non_target = _contains_non_target_stock_alias(clause, stock_code, stock_name)
                clause_has_value_risk = _is_cross_company_target_value_sentence(clause, stock_code, stock_name)
                if clause_has_target and not clause_has_value_risk:
                    if clause_has_non_target and re.search(
                        r"目標價|評等|EPS|每股純益|營收|毛利|獲利|預估|上看|調升|調降|ASP|報價|記憶體|DRAM|HBM|PCB|載板|伺服器|AI",
                        clause,
                    ):
                        continue
                    clauses.append(clause)

            target_sentence = "，".join(clauses).strip("，,；;、 ") if clauses else sent
            if not has_cross_risk:
                add_sentence(target_sentence)
                # 保留後面 2 句承接句，避免正文後續用「該公司 / 其 / 法人指出」而不再重複公司名導致抓不到。
                context_window = 2
            continue

        if context_window > 0:
            if _is_safe_target_context_sentence(sent, stock_code, stock_name):
                add_sentence(sent)
                context_window -= 1
                continue
            # 遇到其他公司或明顯無關內容，就結束本股票上下文。
            if has_non_target:
                context_window = 0

    focused = "。".join(selected)
    if len(_normalize_news_text(focused)) >= 40:
        return focused

    # 若全文沒有其他公司名，代表不是多股混雜新聞；可保守保留含公司名附近的前段內容，避免完全抓不到。
    if any(alias in content for alias in aliases) and not _contains_non_target_stock_alias(content, stock_code, stock_name):
        cleaned = _normalize_news_text(content)
        return cleaned[: min(len(cleaned), NEWS_MAX_ARTICLE_CHARS_TO_GEMINI)]

    return focused

def _split_news_sentences(text: str) -> List[str]:
    s = _normalize_news_text(text)
    if not s:
        return []
    # 新聞內文常會用句號、分號、驚嘆號或換行分段。
    parts = re.split(r"(?<=[。！？!?；;])\s*|[\r\n]+", s)
    out = []
    for p in parts:
        p = _normalize_news_text(p)
        p = re.sub(r"^[,，、。\s]+", "", p).strip()
        if not p or _is_bad_news_sentence(p):
            continue
        if len(p) > 120:
            sub_parts = re.split(r"(?<=[，,])\s*", p)
            buf = ""
            for sp in sub_parts:
                sp = _normalize_news_text(sp)
                if not sp:
                    continue
                if len(buf + sp) <= 90:
                    buf += sp
                else:
                    buf = buf.strip("，, ")
                    if buf and not _is_bad_news_sentence(buf):
                        out.append(buf)
                    buf = sp
            buf = buf.strip("，, ")
            if buf and not _is_bad_news_sentence(buf):
                out.append(buf)
        else:
            out.append(p.strip("，, "))
    return out


def _trim_news_point(text: str, max_len: int | None = None) -> str:
    max_len = int(max_len or NEWS_SUMMARY_POINT_MAX_LEN)
    s = _normalize_news_text(text)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = re.sub(r"^(新聞重點|新聞線索|重點|摘要)[:：]\s*", "", s).strip()
    s = re.sub(r"\s+-\s+[^-]{1,40}$", "", s).strip()
    s = _remove_news_boilerplate(s)
    s = s.strip("。；;，, ")
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    # 優先在逗號或頓號處截斷，避免句子突然斷掉。
    last = max(cut.rfind("，"), cut.rfind("、"), cut.rfind("；"), cut.rfind(";"))
    if last >= 28:
        cut = cut[:last]
    return cut.rstrip("，、；; ") + "…"


def _score_news_sentence(sentence: str, keywords: List[str], stock_code: str, stock_name: str) -> int:
    s = str(sentence or "")
    if _is_bad_news_sentence(s):
        return -99
    score = 0
    for k in keywords:
        if k and k in s:
            score += 5
    if stock_code and stock_code in s:
        score += 2
    if stock_name and stock_name in s:
        score += 2
    for k in ["本週", "近期", "今年", "明年", "上半年", "下半年", "第1季", "第2季", "第3季", "第4季", "Q1", "Q2", "Q3", "Q4"]:
        if k in s:
            score += 1
    if re.search(r"\d+(?:\.\d+)?\s*(%|％|元|億元|萬|季|月|年|倍|美元)", s):
        score += 2
    if 22 <= len(s) <= 86:
        score += 2
    elif len(s) > 100:
        score -= 2
    return score


def _collect_news_sentences(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    candidates = []
    seen = set()
    aliases = [a for a in _get_news_aliases(stock_code, stock_name) if a] if (stock_code or stock_name) else []
    for rec in records:
        if NEWS_REQUIRE_ARTICLE_BODY and not rec.get("body_ok"):
            continue
        if not (rec.get("body_ok") or rec.get("fallback_ok")):
            continue
        content = rec.get("content", "")
        title = rec.get("title", "")
        source = str(rec.get("source", "") or "").strip()
        if aliases:
            content = _extract_target_focused_news_body(content, stock_code, stock_name)
            if len(_normalize_news_text(content)) < 40:
                continue
        # 這裡刻意不把 title 當候選句；RSS description 只在原文被擋時作為改寫素材。
        for sent in _split_news_sentences(content):
            sent = _trim_news_point(sent, max_len=NEWS_SUMMARY_POINT_MAX_LEN + 12)
            if not sent or sent in seen or _is_bad_news_sentence(sent):
                continue
            if aliases and not any(alias in sent for alias in aliases):
                # 規則式補字數時也必須明確指向本股票，避免拿同篇新聞其他公司的題材來補。
                continue
            if aliases and _contains_non_target_stock_alias(sent, stock_code, stock_name) and re.search(r"目標價|評等|EPS|每股純益|營收|毛利|獲利|預估|上看|調升|調降|記憶體|DRAM|HBM", sent):
                continue
            if _looks_like_news_headline(sent, title):
                continue
            if source and source in sent and len(sent) <= 90:
                continue
            candidates.append({
                "text": sent,
                "source": source,
                "title": title,
            })
            seen.add(sent)
    return candidates

def _clean_summary_points(raw_points: List[str]) -> List[str]:
    points = []
    for p in raw_points or []:
        s = _trim_news_point(p, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
        if not s or _is_bad_news_sentence(s):
            continue
        if s in points:
            continue
        points.append(s)
        if len(points) >= NEWS_SUMMARY_MAX_POINTS:
            break
    return points


def _count_summary_chars(points: List[str]) -> int:
    """計算新聞重點實際文字量；排除項目符號與空白，避免低於圖片需要的資訊密度。"""
    joined = "".join(str(p or "") for p in points or [])
    joined = re.sub(r"[\s•\-–—\d\.、\)）:：，,。；;]", "", joined)
    return len(joined)


def _parse_raw_points_from_llm(output_text: str) -> List[str]:
    parsed = _extract_json_from_text(output_text)
    raw_points = []
    if isinstance(parsed, dict):
        raw_points = parsed.get("points", []) or []
    elif isinstance(parsed, list):
        raw_points = parsed

    if not raw_points:
        for line in str(output_text or "").splitlines():
            line = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", line).strip()
            if line:
                raw_points.append(line)
    return [str(p) for p in raw_points]


def _clean_news_summary_points(raw_points: List[str]) -> List[str]:
    """新聞專用清理：保留較完整的重點，使總字數可達 150 字以上。"""
    points = []
    for p in raw_points or []:
        s = _trim_news_point(p, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
        if not s or _is_bad_news_sentence(s):
            continue
        if s in points:
            continue
        points.append(s)
        if len(points) >= NEWS_SUMMARY_MAX_POINTS:
            break
    return points


def _clean_news_summary_points_for_stock(raw_points: List[str], stock_code: str, stock_name: str) -> List[str]:
    """新聞重點清理時加入跨公司數字防呆，避免把其他公司的目標價寫成本股票重點。"""
    points = []
    for p in raw_points or []:
        s = _trim_news_point(p, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
        if not s or _is_bad_news_sentence(s):
            continue
        if _is_cross_company_target_value_sentence(s, stock_code, stock_name):
            print(f"⚠️ 略過疑似跨公司目標價 / 評等重點：{s}")
            continue
        if s in points:
            continue
        points.append(s)
        if len(points) >= NEWS_SUMMARY_MAX_POINTS:
            break
    return points


def _build_news_expansion_points(records: List[dict], stock_code: str, stock_name: str, used_points: List[str] | None = None) -> List[str]:
    """Gemini 輸出太短時，從 7 天內原文候選句補足重點字數；不使用新聞標題硬湊。"""
    used_points = used_points or []
    candidates = _collect_news_sentences(records, stock_code, stock_name)
    if not candidates:
        return []

    broad_keywords = [
        stock_code, stock_name, "營收", "財報", "獲利", "EPS", "毛利", "毛利率", "AI", "伺服器",
        "半導體", "記憶體", "DRAM", "NAND", "HBM", "報價", "漲價", "供需", "需求",
        "法說", "展望", "接單", "出貨", "產能", "擴產", "合作", "法人", "外資", "投信",
        "評等", "目標價", "調升", "調降", "客戶", "長約", "庫存", "價格", "景氣",
    ]
    scored = []
    used_compare = {_title_compare_text(p) for p in used_points if p}
    for c in candidates:
        text = c.get("text", "")
        if not text or _is_bad_news_sentence(text):
            continue
        if _is_cross_company_target_value_sentence(text, stock_code, stock_name):
            continue
        cmp_text = _title_compare_text(text)
        if not cmp_text or cmp_text in used_compare:
            continue
        score = _score_news_sentence(text, broad_keywords, stock_code, stock_name)
        if score > 0:
            scored.append((score, text))
    scored.sort(key=lambda x: x[0], reverse=True)

    extra = []
    for _, text in scored:
        point = _trim_news_point(text, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
        if not point or _is_bad_news_sentence(point):
            continue
        cmp_point = _title_compare_text(point)
        if cmp_point in used_compare:
            continue
        extra.append(point)
        used_compare.add(cmp_point)
        if len(extra) >= NEWS_SUMMARY_MAX_POINTS:
            break
    return extra


def _ensure_news_summary_min_total(points: List[str], records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """確保新聞區塊至少約 150 字；資料不足時仍只從 7 天內新聞素材補充。"""
    points = _clean_news_summary_points_for_stock(points, stock_code, stock_name)
    if _count_summary_chars(points) >= NEWS_SUMMARY_MIN_TOTAL_CHARS and len(points) >= min(NEWS_SUMMARY_MIN_POINTS, NEWS_SUMMARY_MAX_POINTS):
        return points[:NEWS_SUMMARY_MAX_POINTS]

    expanded = points[:]
    for p in _build_news_expansion_points(records, stock_code, stock_name, used_points=expanded):
        if len(expanded) >= NEWS_SUMMARY_MAX_POINTS:
            break
        if p not in expanded:
            expanded.append(p)
        if _count_summary_chars(expanded) >= NEWS_SUMMARY_MIN_TOTAL_CHARS and len(expanded) >= min(NEWS_SUMMARY_MIN_POINTS, NEWS_SUMMARY_MAX_POINTS):
            break

    if _count_summary_chars(expanded) >= NEWS_SUMMARY_MIN_TOTAL_CHARS:
        return expanded[:NEWS_SUMMARY_MAX_POINTS]

    # 若點數已滿但總字數仍不足，嘗試用更完整候選句替換較短重點。
    longer_candidates = _build_news_expansion_points(records, stock_code, stock_name, used_points=[])
    for cand in longer_candidates:
        if not expanded:
            expanded.append(cand)
        else:
            shortest_idx = min(range(len(expanded)), key=lambda i: len(expanded[i]))
            if len(cand) > len(expanded[shortest_idx]) and cand not in expanded:
                expanded[shortest_idx] = cand
        if _count_summary_chars(expanded) >= NEWS_SUMMARY_MIN_TOTAL_CHARS:
            break

    return expanded[:NEWS_SUMMARY_MAX_POINTS]


def _parse_gemini_news_points(output_text: str, records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    raw_points = _parse_raw_points_from_llm(output_text)
    return _ensure_news_summary_min_total(raw_points, records, stock_code, stock_name)


def _get_warrants_api_key() -> str:
    """讀取 Gemini API Key；GitHub Actions 使用 WARRANTS_API_KEY。"""
    return (
        os.getenv("WARRANTS_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def _extract_json_from_text(text: str):
    if not text:
        return None
    s = str(text).strip()
    s = re.sub(r"^```json\s*", "", s)
    s = re.sub(r"^```\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _is_retryable_gemini_error(err) -> bool:
    err_text = str(err)
    retry_keywords = [
        "503", "UNAVAILABLE", "high demand", "temporarily unavailable", "429",
        "RESOURCE_EXHAUSTED", "rate limit", "quota", "timeout", "Deadline", "deadline",
    ]
    return any(k in err_text for k in retry_keywords)


def _call_gemini_with_retry(prompt: str):
    if not GEMINI_ENABLE:
        return None
    if genai is None:
        print("⚠️ 未安裝 google-genai，無法使用 Gemini 摘要；將改用規則式摘要")
        return None
    api_key = _get_warrants_api_key()
    if not api_key:
        print("⚠️ 未設定 WARRANTS_API_KEY，無法使用 Gemini 摘要；將改用規則式摘要")
        return None

    client = genai.Client(api_key=api_key)
    last_error = None
    for attempt in range(1, GEMINI_RETRY_TIMES + 1):
        try:
            print(f"Gemini 呼叫第 {attempt}/{GEMINI_RETRY_TIMES} 次，模型：{GEMINI_MODEL}")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            return response.text or ""
        except Exception as e:
            last_error = e
            if _is_retryable_gemini_error(e) and attempt < GEMINI_RETRY_TIMES:
                wait_sec = GEMINI_RETRY_BASE_WAIT * attempt
                print(f"⚠️ Gemini 暫時忙碌或限流，{wait_sec:.0f} 秒後重試：{str(e)[:180]}")
                time.sleep(wait_sec)
                continue
            print(f"⚠️ Gemini 呼叫失敗：{e}")
            return None
    if last_error:
        print(f"⚠️ Gemini 呼叫失敗：{last_error}")
    return None


def _parse_gemini_points(output_text: str) -> List[str]:
    return _clean_summary_points(_parse_raw_points_from_llm(output_text))


def _build_gemini_news_articles(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    """只把「有足夠內文」的文章送給 Gemini，並先萃取本股票相關片段，避免多家公司新聞數字混用。"""
    usable = []
    ordered = [r for r in records if r.get("body_ok") or r.get("fallback_ok")]
    for rec in ordered:
        content = _normalize_news_text(rec.get("content", ""))
        if len(content) < 80:
            continue
        title = _clean_news_title(rec.get("title", ""))
        focused_content = _extract_target_focused_news_body(content, stock_code, stock_name)
        if len(_normalize_news_text(focused_content)) < 40:
            print(f"⚠️ 略過多股混雜新聞：{title[:36]}｜找不到足夠的 {stock_code} {stock_name} 明確片段")
            continue
        usable.append({
            "id": f"A{len(usable) + 1}",
            "source": rec.get("source", ""),
            "title": title,
            "published": rec.get("published", ""),
            "url": rec.get("url", ""),
            "content_source": rec.get("content_source", ""),
            "target_aliases": _get_news_aliases(stock_code, stock_name),
            "body": focused_content[:NEWS_MAX_ARTICLE_CHARS_TO_GEMINI],
        })
        if len(usable) >= NEWS_MAX_ARTICLES_TO_GEMINI:
            break
    return usable

def _summarize_news_with_gemini(records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """依照新聞原文讓 Gemini 統整成圖片可用的短重點；邏輯接近獨立 Gemini 新聞測試程式。"""
    usable_articles = _build_gemini_news_articles(records, stock_code, stock_name)
    if not usable_articles:
        print("⚠️ 沒有足夠新聞原文可送入 Gemini；不使用標題硬湊新聞重點")
        return []

    display_name = stock_name if stock_name else stock_code
    article_json = json.dumps(usable_articles, ensure_ascii=False, indent=2)
    prompt = f"""
你是台股新聞重點整理助手。
你只能根據我提供的新聞內文整理，不可以使用外部知識，不可以自行補充。
請使用繁體中文。

股票：{stock_code} {display_name}

任務：
整理近 7 天的「本週新聞重點」，輸出給圖片週報右下角使用。
請綜合多篇新聞內文與媒體 / 法人說法，統整出重點中的重點，不要逐篇列標題。

嚴格規則：
1. 不要直接複製新聞標題。
2. 不要輸出新聞網站名稱、作者、網址、完整看、看更多、延伸閱讀。
3. 不要把只有股價漲停、亮燈、強漲、創高、焦點股這類描述當成重點。
4. 每一點必須來自近 7 天新聞內文，不可以幻想，不可以使用外部知識。
5. 如果新聞同時提到多家公司，所有目標價、評等、EPS、營收、獲利預估等數字，必須確認該數字在同一句或同一分句中明確指向「{stock_code} {display_name}」。
6. 嚴禁把台積電、瑞昱、聯詠或其他公司的目標價 / EPS / 營收預估寫成「{display_name}」的重點；若無法判斷數字屬於哪家公司，就不要使用該數字。
7. 若句子格式像「A 公司目標價 3000 元、B 公司目標價 5922 元」，整理 {display_name} 時只能保留 B 公司明確對應的數字，不可混用 A 公司數字。
8. 若新聞片段出現記憶體、DRAM、HBM、伺服器、PCB、載板等產業詞，必須確認該產業詞在同一句或相鄰句明確連到「{stock_code} {display_name}」；不能把同篇文章中其他股票的產業題材寫成本股票重點。
9. 優先整理與「{stock_code} {display_name}」公司本身產業、基本面或股價可能受影響的消息，不要整理同篇文章中其他公司的題材。
10. 具體重點優先順序：法人目標價 / 評等 / 升降評、EPS / 每股純益、營收 / 毛利率 / 獲利、ASP / 報價 / 供需、接單 / 出貨 / 產能 / 長約、公司本身所屬產業趨勢。
11. 若產業詞、目標價、EPS、營收、ASP、毛利率或獲利預估沒有在同一句或相鄰句明確連到「{stock_code} {display_name}」，不要寫進重點。
12. 請最多輸出 3 點，建議 2～3 點；整體至少 {NEWS_SUMMARY_MIN_TOTAL_CHARS} 個中文字，若只有 2 點，每點要更完整。
13. 若只有 2 個高品質重點且已達整體字數要求，可以只輸出 2 點；不要為了湊第 3 點而輸出關鍵字、標籤、追蹤文字或看不懂的摘要。
14. 不要輸出投資建議，不要寫「可以買進」「建議進場」。
15. 圖片區塊不大，但新聞內容必須有資訊量；每點約 42～90 個中文字。
16. 若不同文章報同一件事，合併成一點，並寫出共同核心。
17. 請保留最關鍵的數字或事件，但不要塞滿數字。
18. 嚴禁輸出「關鍵字：」、「追蹤我們」、「分享給朋友」、「本文」、「標籤」或任何社群導流、SEO 關鍵字內容。

請只回傳 JSON，不要 markdown，不要多餘說明。
格式：
{{
  "points": [
    "第一點",
    "第二點"
  ],
  "note": "資料是否充足的簡短說明"
}}

以下是新聞內文 JSON：
{article_json}
"""
    print("=" * 100)
    print("開始呼叫 Gemini 統整新聞重點")
    print(f"模型：{GEMINI_MODEL}")
    print(f"送入 Gemini 的文章數：{len(usable_articles)}")
    print("=" * 100)
    output_text = _call_gemini_with_retry(prompt)
    points = _parse_gemini_news_points(output_text or "", records, stock_code, stock_name)
    if points:
        print(f"✅ Gemini 新聞重點完成：{len(points)} 點，總字數約 {_count_summary_chars(points)} 字")
    return points

def _safe_float(v, default=np.nan):
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _build_weekly_llm_payload(ctx: dict, stock_name: str) -> dict:
    df = ctx.get("plot_df", pd.DataFrame())
    latest = df.iloc[-1] if df is not None and not df.empty else pd.Series(dtype=float)
    prev = df.iloc[-2] if df is not None and len(df) >= 2 else latest
    stock_code = str(ctx.get("stock_code", "") or "")

    close = _safe_float(latest.get("Close"))
    prev_close = _safe_float(prev.get("Close"))
    latest_pct = (close / prev_close - 1) * 100 if prev_close and np.isfinite(prev_close) and prev_close != 0 else np.nan
    vol = _safe_float(latest.get("Volume"))
    mv20 = _safe_float(latest.get("MV20"))
    vol_ratio = vol / mv20 if mv20 and np.isfinite(mv20) and mv20 > 0 else np.nan

    week_events = ctx.get("week_events")
    branch_rows = []
    warrant_rows = []
    if week_events is not None and not week_events.empty:
        e = week_events.copy()
        e["branch"] = e["branch"].replace("", "未知分點")
        by_branch = e.groupby("branch", as_index=False)["net_amount"].sum().sort_values("net_amount", ascending=False)
        for _, r in by_branch.head(5).iterrows():
            branch_rows.append({"branch": str(r["branch"]), "net": fmt_money(float(r["net_amount"]))})
        for _, r in by_branch.tail(5).sort_values("net_amount", ascending=True).iterrows():
            branch_rows.append({"branch": str(r["branch"]), "net": fmt_money(float(r["net_amount"]))})

        wg = e.groupby(["warrant_code", "warrant_name"], as_index=False)["net_amount"].sum()
        for _, r in wg.reindex(wg["net_amount"].abs().sort_values(ascending=False).index).head(6).iterrows():
            warrant_rows.append({
                "warrant": f"{r.get('warrant_code', '')} {str(r.get('warrant_name', ''))[:10]}",
                "net": fmt_money(float(r.get("net_amount", 0))),
            })

    payload = {
        "stock": f"{stock_code} {stock_name}",
        "period": f"{ctx['week_start'].strftime('%Y/%m/%d')} - {ctx['week_end'].strftime('%Y/%m/%d')}" if pd.notna(ctx.get("week_start")) else "",
        "technical": {
            "weekly_return": fmt_pct(ctx.get("stock_ret", np.nan)),
            "latest_close": f"{close:.2f}" if np.isfinite(close) else "-",
            "latest_day_return": fmt_pct(latest_pct),
            "ma5": f"{_safe_float(latest.get('MA5')):.2f}" if np.isfinite(_safe_float(latest.get('MA5'))) else "-",
            "ma10": f"{_safe_float(latest.get('MA10')):.2f}" if np.isfinite(_safe_float(latest.get('MA10'))) else "-",
            "ma20": f"{_safe_float(latest.get('MA20')):.2f}" if np.isfinite(_safe_float(latest.get('MA20'))) else "-",
            "ma60": f"{_safe_float(latest.get('MA60')):.2f}" if np.isfinite(_safe_float(latest.get('MA60'))) else "-",
            "ma_signal": get_ma_kline_signals(df) if df is not None and not df.empty else "",
            "kd_signal": get_kd_signals(df) if df is not None and not df.empty else "",
            "macd_signal": get_macd_signals(df) if df is not None and not df.empty else "",
            "volume_change_vs_prev_week": fmt_pct(ctx.get("vol_change", np.nan)),
            "latest_volume_vs_mv20": f"{vol_ratio:.2f} 倍" if np.isfinite(vol_ratio) else "-",
        },
        "institutional_latest": {
            "foreign": f"{_safe_float(latest.get('foreign'), 0):+,.0f}張",
            "invest": f"{_safe_float(latest.get('invest'), 0):+,.0f}張",
            "dealer": f"{_safe_float(latest.get('dealer'), 0):+,.0f}張",
            "total": f"{_safe_float(latest.get('total'), 0):+,.0f}張",
        },
        "warrant_flow": {
            "weekly_buy": fmt_money_abs(ctx.get("total_buy", 0)),
            "weekly_sell": fmt_money(-abs(float(ctx.get("total_sell", 0) or 0))),
            "weekly_net": fmt_money(ctx.get("total_net", 0)),
            "bias": ctx.get("bias", ""),
            "top_branches_and_sellers": branch_rows,
            "major_warrants": warrant_rows,
        },
    }
    return payload


def _summarize_weekly_context_with_gemini(ctx: dict, stock_name: str) -> List[str]:
    """讓 Gemini 讀取技術面、法人與權證資金流資料，產生左下角本週重點。"""
    if not WEEKLY_KEYPOINT_LLM_ENABLE:
        return []
    try:
        payload = _build_weekly_llm_payload(ctx, stock_name)
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        prompt = f"""
你是台股權證週報分析助手，只能根據我提供的資料整理，不可以使用外部知識，不可以自行補充。
請使用繁體中文。

任務：根據技術面、三大法人與權證分點資金流，整理左下角「本週重點」。

嚴格規則：
1. 請輸出 3 到 4 點，整體至少 {WEEKLY_KEYPOINT_MIN_TOTAL_CHARS} 個中文字，每點約 45 到 90 個中文字。
2. 只寫重點中的重點，適合放在圖片小區塊，但資訊量要足夠。
3. 必須整合技術面與權證資料，不要只複述單一數字。
4. 可以描述偏多、偏弱、量能、分點集中、資金流向，但不要寫投資建議。
5. 不要寫「建議買進」「可以進場」「目標價」。
6. 數字可保留最關鍵者，不要每點塞太多數字。
7. 若資料互相矛盾，請用「股價偏強但資金流需觀察」這種保守語氣。
8. 不要輸出關鍵字、標籤、追蹤、分享或任何導流文字。

請只回傳 JSON，不要 markdown，不要多餘說明。
格式：
{{
  "points": [
    "第一點",
    "第二點"
  ]
}}

以下是本週資料 JSON：
{payload_json}
"""
        output_text = _call_gemini_with_retry(prompt)
        points = _parse_weekly_gemini_points(output_text or "")
        if points:
            print(f"✅ Gemini 本週重點完成：{len(points)} 點，總字數約 {_count_summary_chars(points)} 字")
        return points
    except Exception as e:
        print(f"⚠️ Gemini 本週重點整理失敗，改用規則式重點：{e}")
        return []


def _summarize_news_with_openai(records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """若有 OPENAI_API_KEY，優先用新聞內文整理成真正重點；失敗則自動走規則式摘要。"""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not NEWS_OPENAI_ENABLE or not api_key:
        return []

    blocks = []
    total_len = 0
    body_records = [r for r in records if r.get("body_ok") and _normalize_news_text(r.get("content", ""))]
    for idx, rec in enumerate(body_records, 1):
        content = _normalize_news_text(rec.get("content", ""))
        sentences = _split_news_sentences(content)
        if not sentences:
            continue
        clean_content = "。".join(sentences[:10])
        if len(clean_content) < 60:
            continue
        title = _clean_news_title(rec.get("title", ""))
        block = f"新聞{idx}\n標題：{title}\n內文：{clean_content[:1600]}"
        blocks.append(block)
        total_len += len(block)
        if total_len >= 6500:
            break

    if not blocks:
        return []

    prompt = (
        f"請根據以下一週內新聞內文，整理 {stock_code} {stock_name} 的新聞重點。\n"
        "要求：\n"
        "1. 最多輸出 3 點，每點 45 到 90 個中文字。\n"
        "2. 只能根據『內文』重寫成重點，不要直接複製新聞標題或原句。\n"
        "3. 不要出現『完整看』、『新聞線索』、『來源』、新聞網站名稱或多檔股名清單。\n"
        "4. 每點要像研究摘要，說明原因、影響或觀察方向，不要寫成聳動標題。\n"
        "5. 只聚焦公司本身可能影響股價的消息：公司產業、法人目標價/評等、EPS/每股純益、營收、毛利率、獲利、ASP/報價、接單出貨、產能與供需。\n"
        "6. 若目標價、EPS、營收、ASP、毛利率或產業題材沒有明確指向本公司，請不要使用。\n"
        "7. 若資料不足，寧可保守，不要臆測。\n\n"
        + "\n\n".join(blocks)
    )

    try:
        payload = {
            "model": NEWS_OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "你是台股產業新聞摘要助理，輸出繁體中文、重點清楚、避免標題式內容。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=(8, 40))
        resp.raise_for_status()
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        raw_points = []
        for line in str(text).splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", line).strip()
            if line:
                raw_points.append(line)
        points = _clean_summary_points(raw_points)
        if points:
            print(f"✅ OpenAI 新聞摘要完成：{len(points)} 點")
        return points
    except Exception as e:
        print(f"⚠️ OpenAI 新聞摘要失敗，改用規則式摘要：{e}")
        return []


def _make_news_keypoint(label: str, sentence: str, stock_code: str, stock_name: str) -> str:
    """規則式備援：保留內文中的具體事實，避免改成空泛模板。"""
    s = _normalize_news_text(sentence)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = s.strip("。；;，, ")
    if not s or _is_bad_news_sentence(s):
        return ""

    # 移除過度像標題的前綴，保留真正資訊。
    s = re.sub(r"^(焦點股|個股|台股|盤中|盤後)[:：]?", "", s).strip()
    max_body_len = max(24, NEWS_SUMMARY_POINT_MAX_LEN - len(label) - 1)
    body = _trim_news_point(s, max_len=max_body_len)
    if not body or _is_bad_news_sentence(body):
        return ""
    return f"{label}：{body}"

def _rule_based_news_summary(records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    candidates = _collect_news_sentences(records, stock_code, stock_name)
    if not candidates:
        return []

    categories = [
        ("業績面", ["營收", "月增", "年增", "業績", "財報", "獲利", "EPS", "毛利", "毛利率", "每股盈餘", "虧損", "轉盈"]),
        ("產業面", ["AI", "伺服器", "記憶體", "DRAM", "NAND", "半導體", "報價", "HBM", "漲價", "缺貨", "先進封裝", "CoWoS", "ASIC", "散熱"]),
        ("展望面", ["轉型", "布局", "擴產", "合作", "投資", "新產品", "法說", "展望", "接單", "出貨", "產能", "需求", "訂單", "客戶"]),
        ("法人面", ["外資", "投信", "券商", "法人", "評等", "目標價", "調升", "調降", "買進", "中立", "賣出", "大摩", "摩根士丹利", "高盛", "里昂"]),
    ]

    points = []
    used = set()
    for label, keywords in categories:
        scored = []
        for c in candidates:
            text = c["text"]
            if text in used:
                continue
            score = _score_news_sentence(text, keywords, stock_code, stock_name)
            if score > 0:
                scored.append((score, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            pick = _make_news_keypoint(label, scored[0][1], stock_code, stock_name)
            if pick and not _is_bad_news_sentence(pick):
                points.append(pick)
                used.add(scored[0][1])

    if len(points) < NEWS_SUMMARY_MAX_POINTS:
        broad_keywords = [
            stock_code, stock_name, "營收", "財報", "AI", "伺服器", "半導體", "記憶體", "DRAM", "HBM", "法說", "展望",
            "外資", "投信", "法人", "報價", "獲利", "接單", "出貨", "擴產", "合作", "題材", "需求", "產能",
        ]
        scored = []
        for c in candidates:
            text = c["text"]
            if text in used:
                continue
            score = _score_news_sentence(text, broad_keywords, stock_code, stock_name)
            if score > 0:
                scored.append((score, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, text in scored:
            if len(points) >= NEWS_SUMMARY_MAX_POINTS:
                break
            pick = _make_news_keypoint("新聞面", text, stock_code, stock_name)
            if pick and not _is_bad_news_sentence(pick):
                points.append(pick)
                used.add(text)

    return _ensure_news_summary_min_total(points, records, stock_code, stock_name)


def build_news_points(stock_code: str, stock_name: str, news_items, ctx: dict | None = None) -> List[str]:
    """根據最近一週新聞內文整理重點；優先只用足夠新聞原文交給 Gemini，不直接把新聞標題放進圖表。"""
    records = _news_items_to_records(news_items)
    body_records = [
        r for r in records
        if r.get("body_ok") and len(_normalize_news_text(r.get("content", ""))) >= NEWS_MIN_BODY_CHARS
    ]
    fallback_records = [r for r in records if r.get("fallback_ok") and _normalize_news_text(r.get("content", ""))]

    if not records:
        return ["本週未抓到可整理的新聞素材；可用 WEEKLY_NEWS_TEXT 手動填入新聞重點。"]

    # 優先用足夠新聞原文；若原文抓不到或本股票片段太短，允許使用已驗證相關的 RSS 摘要作為 AI 改寫素材，避免完全沒有新聞。
    ai_source_records = body_records if body_records else fallback_records
    ai_points = _summarize_news_with_gemini(ai_source_records, stock_code, stock_name)
    if ai_points:
        return _ensure_news_summary_min_total(ai_points, body_records, stock_code, stock_name)[:NEWS_DISPLAY_MAX_POINTS]

    # Gemini 不可用或失敗時，仍優先從真正內文抽重點；最後才用 RSS 摘要作為備援素材。
    rule_source = body_records if body_records else fallback_records
    rule_points = _rule_based_news_summary(rule_source, stock_code, stock_name)
    if rule_points:
        return _ensure_news_summary_min_total(rule_points, rule_source, stock_code, stock_name)[:NEWS_DISPLAY_MAX_POINTS]

    if not body_records:
        return ["本週近7天新聞多為標題或短摘要，未取得足夠原文可統整；目前不輸出標題式內容，建議稍後重跑或補手動新聞重點。"]
    return ["本週近7天新聞雖有原文素材，但有效句不足以整理成完整重點；目前不輸出標題式內容，可用 WEEKLY_NEWS_TEXT 手動補充。"]


# ============================================================
# 繪圖工具
# ============================================================

def style_ax(ax, title=None, title_color=GOLD):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=28)
    for spine in ax.spines.values():
        spine.set_color(GRID)
        spine.set_linewidth(1.1)
    ax.grid(True, color=GRID, alpha=0.35, linewidth=0.7)
    if title:
        ax.set_title(title, loc="left", fontsize=38, color=title_color, fontweight="bold", pad=14)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_color(MUTED)


def add_panel_title(ax, title, subtitle=""):
    ax.text(0.01, 0.96, title, transform=ax.transAxes, ha="left", va="top", color=TEXT, fontsize=16, fontweight="bold")
    if subtitle:
        ax.text(0.01, 0.86, subtitle, transform=ax.transAxes, ha="left", va="top", color=MUTED, fontsize=11)


def add_weighted_volume_profile_overlay(ax, df: pd.DataFrame, n_bins: int = 38, color="#38BDF8", alpha=0.15, scale=1.08):
    if df is None or df.empty:
        return
    lows, highs, opens, closes, volumes = df["Low"], df["High"], df["Open"], df["Close"], df["Volume"]
    price_min, price_max = lows.min(), highs.max()
    if price_max <= price_min:
        return
    bins = np.linspace(price_min, price_max, n_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2
    height = bins[1] - bins[0]
    profile = np.zeros(n_bins)
    for i in range(len(df)):
        vol, low, high, open_, close = volumes.iloc[i], lows.iloc[i], highs.iloc[i], opens.iloc[i], closes.iloc[i]
        body_min, body_max = min(open_, close), max(open_, close)
        ranges = [((low, body_min), 0.2), ((body_min, body_max), 0.6), ((body_max, high), 0.2)]
        for (start, end), weight in ranges:
            if end - start < 1e-6:
                continue
            idxs = np.where((centers >= start) & (centers <= end))[0]
            if len(idxs):
                profile[idxs] += vol * weight / len(idxs)
    if profile.max() <= 0:
        return
    scaled = profile / profile.max()
    x_min, x_max = ax.get_xlim()
    width_max = (x_max - x_min) / scale
    sorted_idx = np.argsort(profile)[::-1]
    max_idx = int(sorted_idx[0]) if len(sorted_idx) else -1
    second_idx = int(sorted_idx[1]) if len(sorted_idx) > 1 else -1
    for i in range(n_bins):
        w = scaled[i] * width_max
        if i == max_idx:
            rect_color = "#DC2626"   # 第一大量：紅色
            rect_alpha = 0.2
        elif i == second_idx:
            rect_color = "#F59E0B"   # 第二大量：橘色
            rect_alpha = 0.2
        else:
            rect_color = color       # 其餘維持原本淺藍
            rect_alpha = alpha
        ax.add_patch(Rectangle((x_min, centers[i] - height / 2), w, height, color=rect_color, alpha=rect_alpha, zorder=0, clip_on=True))
    ax.set_xlim(x_min, x_max)


def draw_card(ax, x, y, w, h, label, value, sub="", value_color=GOLD):
    # 單張摘要卡片：保留獨立卡片感，並讓上方藏青色 band 與圓角外框貼齊。
    rounding = 0.026
    band_h = 0.078

    box = FancyBboxPatch(
        (x, y), w, h,
        transform=ax.transAxes,
        boxstyle=f"round,pad=0.000,rounding_size={rounding}",
        facecolor=PANEL2,
        edgecolor=GOLD,
        linewidth=1.25,
        zorder=1,
    )
    ax.add_patch(box)

    # 上方藏青色 band：使用 Rectangle 並裁切到外框圓角，避免左右縮短或圓角不貼合。
    band = Rectangle(
        (x, y + h - band_h),
        w,
        band_h,
        transform=ax.transAxes,
        facecolor=GOLD,
        edgecolor=GOLD,
        linewidth=0,
        alpha=0.96,
        zorder=2,
    )
    band.set_clip_path(box)
    ax.add_patch(band)

    # 標題
    ax.text(
        x + w / 2,
        y + h - 0.15,
        label,
        transform=ax.transAxes,
        color=MUTED,
        fontsize=29,
        fontweight="bold",
        ha="center",
        va="top",
        zorder=4,
    )

    # 數字：固定同一水平線，避免每格看起來不整齊。
    ax.text(
        x + w / 2,
        y + 0.30,
        value,
        transform=ax.transAxes,
        color=value_color,
        fontsize=42,
        fontweight="bold",
        ha="center",
        va="center",
        zorder=4,
    )

    if sub:
        ax.text(
            x + w / 2,
            y + 0.10,
            sub,
            transform=ax.transAxes,
            color=MUTED,
            fontsize=22,
            fontweight="bold",
            ha="center",
            va="bottom",
            zorder=4,
        )

def draw_rounded_panel_with_top_band(ax, x, y, w, h, band_h=0.035, rounding=0.02, linewidth=1.25):
    """畫出與摘要卡片一致的圓角面板，並讓上方藏青色條跟著圓角完整貼齊。"""
    # 先畫白底面板，確保下方仍是乾淨白底。
    base = FancyBboxPatch(
        (x, y), w, h,
        transform=ax.transAxes,
        boxstyle=f"round,pad=0.000,rounding_size={rounding}",
        facecolor=PANEL2,
        edgecolor="none",
        linewidth=0,
        zorder=1,
        clip_on=False,
    )
    ax.add_patch(base)

    # 深藍色先用完整圓角面板畫一次，再用白色遮住下半部；
    # 這樣上方左右圓角會與外框完全貼齊，不會出現方角或縮短。
    band_shape = FancyBboxPatch(
        (x, y), w, h,
        transform=ax.transAxes,
        boxstyle=f"round,pad=0.000,rounding_size={rounding}",
        facecolor=GOLD,
        edgecolor="none",
        linewidth=0,
        alpha=0.96,
        zorder=2,
        clip_on=False,
    )
    ax.add_patch(band_shape)

    body_cover = Rectangle(
        (x, y),
        w,
        max(0.0, h - band_h),
        transform=ax.transAxes,
        facecolor=PANEL2,
        edgecolor="none",
        linewidth=0,
        zorder=3,
        clip_on=False,
    )
    body_cover.set_clip_path(base)
    ax.add_patch(body_cover)

    # 最後補外框，避免白色遮罩蓋掉邊線。
    border = FancyBboxPatch(
        (x, y), w, h,
        transform=ax.transAxes,
        boxstyle=f"round,pad=0.000,rounding_size={rounding}",
        facecolor="none",
        edgecolor=GOLD,
        linewidth=linewidth,
        zorder=4,
        clip_on=False,
    )
    ax.add_patch(border)
    return base


def plot_candles(ax, plot_df: pd.DataFrame, x: list):
    up = plot_df["Close"] >= plot_df["Open"]
    width = 0.72
    for i in x:
        color = RED if up.iloc[i] else GREEN
        op, cl = float(plot_df["Open"].iloc[i]), float(plot_df["Close"].iloc[i])
        hi, lo = float(plot_df["High"].iloc[i]), float(plot_df["Low"].iloc[i])
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.3, zorder=3)
        body_low = min(op, cl)
        body_h = abs(cl - op)
        if body_h < max(0.01, cl * 0.0005):
            ax.plot([i - width / 2, i + width / 2], [cl, cl], color=color, linewidth=2.5, zorder=4)
        else:
            ax.bar(i, body_h, bottom=body_low, width=width, color=color, edgecolor=color, align="center", zorder=4)


def adjust_candle_price_ylim(ax, plot_df: pd.DataFrame):
    """放大 K 線圖 Y 軸顯示範圍，並增加上方留白，避免股價飆高時貼近圖框。"""
    if plot_df is None or plot_df.empty:
        return

    price_cols = [
        "Low", "High",
        "MA5", "MA10", "MA20", "MA60",
        "BB_UPPER", "BB_LOWER",
    ]

    values = []
    for col in price_cols:
        if col in plot_df.columns:
            s = pd.to_numeric(plot_df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if not s.empty:
                values.append(s)

    if not values:
        return

    all_values = pd.concat(values, ignore_index=True)
    y_min = float(all_values.min())
    y_max = float(all_values.max())
    if not np.isfinite(y_min) or not np.isfinite(y_max):
        return

    span = y_max - y_min
    latest_close = float(pd.to_numeric(plot_df["Close"], errors="coerce").dropna().iloc[-1]) if "Close" in plot_df.columns and not pd.to_numeric(plot_df["Close"], errors="coerce").dropna().empty else 1.0
    if span <= 0:
        span = max(abs(latest_close) * 0.08, 1.0)

    # 上方留白刻意比下方大，讓高檔 K 線不會貼到圖框，看起來會往下一點。
    lower_pad = max(span * 0.12, abs(latest_close) * 0.015, 1.0)
    upper_pad = max(span * 0.26, abs(latest_close) * 0.035, 1.0)

    ax.set_ylim(y_min - lower_pad, y_max + upper_pad)


def adjust_volume_ylim(ax, plot_df: pd.DataFrame):
    """增加成交量圖上方留白，避免大量柱狀或均量線貼到 legend / 圖框。"""
    if plot_df is None or plot_df.empty:
        return

    values = []
    if "Volume" in plot_df.columns:
        s = pd.to_numeric(plot_df["Volume"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna() / 1000
        if not s.empty:
            values.append(s)
    for col in ["MV5", "MV20"]:
        if col in plot_df.columns:
            s = pd.to_numeric(plot_df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna() / 1000
            if not s.empty:
                values.append(s)

    if not values:
        return

    all_values = pd.concat(values, ignore_index=True)
    y_max = float(all_values.max())
    if not np.isfinite(y_max) or y_max <= 0:
        return

    # 成交量沒有負值，直接把上緣放大，讓 legend 與最高量柱中間有空間。
    ax.set_ylim(0, y_max * 1.45)


def adjust_institutional_ylim(ax, plot_df: pd.DataFrame):
    """增加三大法人圖上下留白，避免正負堆疊柱貼到 legend、文字或圖框。"""
    if plot_df is None or plot_df.empty:
        return
    if not {"foreign", "invest", "dealer"}.issubset(plot_df.columns):
        return

    f = pd.to_numeric(plot_df["foreign"], errors="coerce").fillna(0).astype(float).values
    i = pd.to_numeric(plot_df["invest"], errors="coerce").fillna(0).astype(float).values
    d = pd.to_numeric(plot_df["dealer"], errors="coerce").fillna(0).astype(float).values
    if len(f) == 0:
        return

    pos_stack = np.clip(f, 0, None) + np.clip(i, 0, None) + np.clip(d, 0, None)
    neg_stack = np.clip(f, None, 0) + np.clip(i, None, 0) + np.clip(d, None, 0)
    y_min = min(float(np.nanmin(neg_stack)), 0.0)
    y_max = max(float(np.nanmax(pos_stack)), 0.0)
    if not np.isfinite(y_min) or not np.isfinite(y_max):
        return

    span = y_max - y_min
    if span <= 0:
        span = max(abs(y_max), abs(y_min), 1.0)

    # 上方留白比下方多一點，避免 legend 與正值堆疊柱互相壓到。
    upper_pad = span * 0.32
    lower_pad = span * 0.18
    ax.set_ylim(y_min - lower_pad, y_max + upper_pad)



def add_center_watermarks(fig):
    """在長圖中央區域加入上下兩個淡浮水印。"""
    try:
        if not CENTER_WATERMARK_TEXT:
            return

        for y in (0.66, 0.31):
            fig.text(
                0.5,
                y,
                CENTER_WATERMARK_TEXT,
                ha="center",
                va="center",
                fontsize=CENTER_WATERMARK_FONT_SIZE,
                fontweight="bold",
                color=GOLD,
                alpha=CENTER_WATERMARK_ALPHA,
                rotation=CENTER_WATERMARK_ROTATION,
                linespacing=1.12,
                zorder=1000,
            )
    except Exception:
        pass

def plot_weekly_report(stock_code: str, stock_name: str, stock_df: pd.DataFrame, warrant_events: pd.DataFrame, news_items: List[dict]):
    ctx = build_weekly_context(stock_df, warrant_events, WEEK_TRADING_DAYS)
    ctx["stock_code"] = stock_code
    plot_df = ctx["plot_df"].copy()
    plot_events = ctx["plot_events"]
    week_events = ctx["week_events"]
    x = list(range(len(plot_df)))
    date_labels = [pd.Timestamp(d).strftime("%m-%d") for d in plot_df.index]
    daily_net = daily_warrant_net(plot_df, plot_events)
    buy_top, sell_top = top_branch_tables(week_events, topn=5)
    key_points = build_key_points(ctx, stock_name)
    news_points = build_news_points(stock_code, stock_name, news_items, ctx)

    fig = plt.figure(figsize=(28, 54), facecolor=BG)
    gs = GridSpec(8, 12, figure=fig,
                  height_ratios=[1.45, 2.05, 9.8, 2.45, 3.1, 5.0, 9.55, 9.05],
                  hspace=0.20, wspace=0.25)

    # Header
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.set_axis_off()
    period = f"{ctx['week_start'].strftime('%Y/%m/%d')} - {ctx['week_end'].strftime('%Y/%m/%d')}" if pd.notna(ctx["week_start"]) else "-"
    ax_header.text(0.01, 0.50, f"{stock_code} {stock_name}｜權證資金流週報", color=GOLD, fontsize=68, fontweight="bold", ha="left", va="center")
    ax_header.text(0.01, -0.10, f"週報區間：{period}｜資訊僅供教育參考", color=MUTED, fontsize=32, ha="left", va="center")
    ax_header.text(1.03, 0.62, "By 股市艾斯出品  請勿轉傳", color=GOLD, fontsize=30, fontweight="bold", ha="right", va="center")

    # Cards
    ax_cards = fig.add_subplot(gs[1, :])
    ax_cards.set_axis_off()

    cards = [
        ("本週股價", fmt_pct(ctx["stock_ret"]), "", RED if ctx["stock_ret"] >= 0 else GREEN),
        ("本週量能", fmt_pct(ctx["vol_change"]), "", RED if (not np.isnan(ctx["vol_change"]) and ctx["vol_change"] >= 0) else GREEN),
        ("權證週淨流向", fmt_money(ctx["total_net"]), "", RED if ctx["total_net"] >= 0 else GREEN),
        ("本週買進", fmt_money_abs(ctx["total_buy"]), "", RED),
        ("本週賣出", fmt_money(-abs(float(ctx["total_sell"]))), "", GREEN),
    ]

    card_w, gap = 0.183, 0.01
    start_x = (1 - (len(cards) * card_w + (len(cards) - 1) * gap)) / 2
    for i, (lab, val, sub, col) in enumerate(cards):
        draw_card(ax_cards, start_x + i * (card_w + gap), 0.06, card_w, 0.88, lab, val, sub, col)

    # K line
    candle_ax = fig.add_subplot(gs[2, :])
    style_ax(candle_ax, "股價趨勢｜K線、均線、布林與價量分布")
    plot_candles(candle_ax, plot_df, x)
    candle_ax.plot(x, plot_df["MA5"], color=RED, linewidth=2.1, label=f"5MA {plot_df['MA5'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA10"], color=ORANGE, linewidth=2.1, label=f"10MA {plot_df['MA10'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA20"], color=LIME, linewidth=2.1, label=f"20MA {plot_df['MA20'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA60"], color=BLUE, linewidth=2.1, label=f"60MA {plot_df['MA60'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["BB_UPPER"], linestyle="--", color=MUTED, linewidth=1.4, alpha=0.9)
    candle_ax.plot(x, plot_df["BB_LOWER"], linestyle="--", color=MUTED, linewidth=1.4, alpha=0.9)
    add_weighted_volume_profile_overlay(candle_ax, plot_df)
    adjust_candle_price_ylim(candle_ax, plot_df)
    candle_ax.legend(loc="upper left", ncol=4, frameon=False, fontsize=26, labelcolor=TEXT)
    candle_ax.yaxis.tick_right()
    for label in candle_ax.get_yticklabels():
        label.set_fontweight("bold")
    latest = plot_df.iloc[-1]
    prev_close = plot_df["Close"].iloc[-2] if len(plot_df) >= 2 else latest["Close"]
    diff = latest["Close"] - prev_close
    pct = diff / prev_close * 100 if prev_close else np.nan
    latest_info = f"{plot_df.index[-1].strftime('%Y/%m/%d')}  開 {latest['Open']:.2f}  高 {latest['High']:.2f}  低 {latest['Low']:.2f}  收 {latest['Close']:.2f}  {diff:+.2f} ({pct:+.2f}%)"
    candle_ax.text(0.012, 0.88, latest_info, transform=candle_ax.transAxes, color=TEXT, fontsize=27, ha="left", va="top",
                   bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.30", alpha=0.95))
    ma_note = get_ma_kline_signals(plot_df)
    if ma_note:
        candle_ax.text(0.5, 0.08, ma_note, transform=candle_ax.transAxes, color=GOLD, fontsize=31, fontweight="bold", ha="center", va="center",
                       bbox=dict(facecolor="#F6F8FB", edgecolor=GOLD, boxstyle="round,pad=0.28", alpha=0.95))

    # Volume
    vol_ax = fig.add_subplot(gs[3, :], sharex=candle_ax)
    style_ax(vol_ax, "成交量")
    up = plot_df["Close"] >= plot_df["Open"]
    vol_lots = plot_df["Volume"] / 1000
    vol_ax.bar([i for i in x if up.iloc[i]], vol_lots[up], color=RED, width=0.72, alpha=0.72)
    vol_ax.bar([i for i in x if not up.iloc[i]], vol_lots[~up], color=GREEN, width=0.72, alpha=0.72)
    vol_ax.plot(x, plot_df["MV5"] / 1000, color=BLUE, linewidth=2.1, label=f"MV5 {plot_df['MV5'].iloc[-1] / 1000:,.0f}張")
    vol_ax.plot(x, plot_df["MV20"] / 1000, color=PURPLE, linewidth=2.1, label=f"MV20 {plot_df['MV20'].iloc[-1] / 1000:,.0f}張")
    adjust_volume_ylim(vol_ax, plot_df)
    vol_ax.legend(loc="upper left", frameon=False, fontsize=26, labelcolor=TEXT)
    vol_ax.yaxis.tick_right()

    # 三大法人買賣超（取代 KD）
    inst_ax = fig.add_subplot(gs[4, :], sharex=candle_ax)
    style_ax(inst_ax, "三大法人買賣超")
    plot_institutional_stacked_bars(inst_ax, plot_df, x)
    adjust_institutional_ylim(inst_ax, plot_df)
    draw_inst_header_like_legend(inst_ax, plot_df)
    inst_ax.yaxis.tick_right()

    # Warrant daily net bars + cumulative line
    wnet_ax = fig.add_subplot(gs[5, :], sharex=candle_ax)
    style_ax(wnet_ax)
    vals = daily_net["net_amount"].astype(float).values
    cum_vals = np.cumsum(vals)
    latest_net = vals[-1] if len(vals) else 0.0
    latest_cum = cum_vals[-1] if len(cum_vals) else 0.0
    latest_bar_color = RED if latest_net >= 0 else GREEN
    week_color = RED if ctx["total_net"] >= 0 else GREEN

    # 權證資金流標題列：用小圖示與分隔線接在標題後方，不使用 legend / 膠囊，避免擋住圖表本體。
    # 這裡改成「動態接續排列」：每一段畫完後，依照實際文字寬度自動接下一段，
    # 避免遇到幾十萬、幾千萬或億級數字時，固定 x 座標造成間距忽大忽小。
    header_y = 1.062

    def advance_x_by_px(ax, x0, gap_px):
        base_xy = ax.transAxes.transform((x0, header_y))
        return ax.transAxes.inverted().transform((base_xy[0] + gap_px, base_xy[1]))[0]

    def draw_header_text_and_advance(ax, x0, text, color, fontsize=22, fontweight="bold", gap_px=16, alpha=1.0):
        t = ax.text(
            x0, header_y, text,
            transform=ax.transAxes,
            color=color,
            fontsize=fontsize,
            fontweight=fontweight,
            ha="left",
            va="center",
            alpha=alpha,
            clip_on=False,
            zorder=12,
        )
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bbox = t.get_window_extent(renderer=renderer)
        y_disp = ax.transAxes.transform((0, header_y))[1]
        return ax.transAxes.inverted().transform((bbox.x1 + gap_px, y_disp))[0]

    def draw_header_bar_and_advance(ax, x0, color, gap_px=8):
        bar_w = 0.013
        ax.add_patch(Rectangle(
            (x0, header_y - 0.012), bar_w, 0.024,
            transform=ax.transAxes,
            facecolor=color,
            edgecolor=color,
            linewidth=0,
            alpha=0.92,
            clip_on=False,
            zorder=12,
        ))
        return advance_x_by_px(ax, x0 + bar_w, gap_px)

    def draw_header_line_and_advance(ax, x0, color, gap_px=10):
        line_w = 0.030
        ax.plot(
            [x0, x0 + line_w], [header_y, header_y],
            transform=ax.transAxes,
            color=color,
            linewidth=2.6,
            alpha=0.95,
            solid_capstyle="round",
            clip_on=False,
            zorder=12,
        )
        return advance_x_by_px(ax, x0 + line_w, gap_px)

    xpos = 0.000
    xpos = draw_header_text_and_advance(
        wnet_ax, xpos, "權證資金流", GOLD,
        fontsize=34, fontweight="bold", gap_px=22,
    )

    xpos = draw_header_text_and_advance(wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_bar_and_advance(wnet_ax, xpos, latest_bar_color, gap_px=8)
    xpos = draw_header_text_and_advance(wnet_ax, xpos, f"最新日 {fmt_money(latest_net)}", latest_bar_color, gap_px=22)

    xpos = draw_header_text_and_advance(wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_line_and_advance(wnet_ax, xpos, week_color, gap_px=10)
    xpos = draw_header_text_and_advance(wnet_ax, xpos, f"本週合計 {fmt_money(ctx['total_net'])}", week_color, gap_px=22)

    xpos = draw_header_text_and_advance(wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_line_and_advance(wnet_ax, xpos, BLUE, gap_px=10)
    draw_header_text_and_advance(wnet_ax, xpos, f"累計 {fmt_money(latest_cum)}", BLUE, gap_px=0)

    wnet_ax.bar(x, vals, color=[RED if v >= 0 else GREEN for v in vals], width=0.75, alpha=0.85)
    wnet_ax.axhline(0, color=MUTED, linestyle="--", linewidth=1)

    # 柱狀圖 Y 軸自動貼合資料，但一定包含 0
    if len(vals):
        vmin = min(float(np.nanmin(vals)), 0.0)
        vmax = max(float(np.nanmax(vals)), 0.0)
        vspan = max(vmax - vmin, 1.0)
        vpad = vspan * 0.15
        wnet_ax.set_ylim(vmin - vpad, vmax + vpad)

    wnet_ax.yaxis.set_major_formatter(FuncFormatter(money_tick))
    wnet_ax.yaxis.tick_right()
    wnet_ax2 = wnet_ax.twinx()
    wnet_ax2.plot(x, cum_vals, color=BLUE, linewidth=2.1, alpha=0.95)
    wnet_ax.tick_params(axis="y", labelsize=22)

    if len(cum_vals):
        cmax = max(float(np.nanmax(cum_vals)), 0.0)
        cmin = min(float(np.nanmin(cum_vals)), 0.0)

    # 取得柱狀圖 0 軸在畫面中的相對位置
        y1_min, y1_max = wnet_ax.get_ylim()
        zero_frac = (0 - y1_min) / (y1_max - y1_min)

    # 避免極端情況
        zero_frac = min(max(zero_frac, 0.05), 0.95)

    # 讓折線圖右軸的 0 軸對齊柱狀圖 0 軸
        upper_need = cmax / (1 - zero_frac) if (1 - zero_frac) > 0 else cmax
        lower_need = abs(cmin) / zero_frac if zero_frac > 0 else abs(cmin)
        scale = max(upper_need, lower_need, 1.0) * 1.12

        wnet_ax2.set_ylim(-zero_frac * scale, (1 - zero_frac) * scale)
    
    wnet_ax2.tick_params(colors=MUTED, labelsize=22)
    wnet_ax2.yaxis.set_major_formatter(FuncFormatter(money_tick))
    for spine in wnet_ax2.spines.values():
        spine.set_visible(False)
    wnet_ax2.grid(False)

    # TOP5 tables
    ax_top = fig.add_subplot(gs[6, :])
    ax_top.set_axis_off()
    ax_top.set_facecolor(BG)
    sections = [
        (0.02, "本週淨買超分點 TOP5", buy_top, RED),
        (0.52, "本週淨賣超分點 TOP5", sell_top, GREEN),
    ]
    for x0, title, df_top, side_color in sections:
        # TOP5 卡片：上緣位置維持，底部往下拓一點，讓內容與外框更有呼吸感。
        card_y = -0.045
        card_w = 0.52
        card_h = 0.970
        band_h = 0.035
        draw_rounded_panel_with_top_band(
            ax_top,
            x0,
            card_y,
            card_w,
            card_h,
            band_h=band_h,
            rounding=0.02,
            linewidth=1.35,
        )
        ax_top.text(x0 + 0.02, 0.845, title, transform=ax_top.transAxes, color=side_color, fontsize=42, fontweight="bold", ha="left", va="top", zorder=6)
        ax_top.text(x0 + 0.02, 0.772, "分點｜本週淨額｜代表權證（該分點本週金額最大）", transform=ax_top.transAxes, color=MUTED, fontsize=29, ha="left", va="top", zorder=6)
        if df_top.empty:
            ax_top.text(x0 + 0.03, 0.58, "本週無符合資料", transform=ax_top.transAxes, color=MUTED, fontsize=25, ha="left", va="center", zorder=6)
        else:
            y = 0.645
            row_gap = 0.142
            for rank, (_, r) in enumerate(df_top.iterrows(), 1):
                branch = str(r["branch"]) or "未知分點"
                amt = float(r["net_amount"])
                wcode = str(r.get("max_warrant_code", ""))
                wname = str(r.get("max_warrant_name", ""))
                wamt = float(r.get("max_warrant_amount", 0.0))
                # rank circle
                circ_x = x0 + 0.03
                circ_y = y - 0.012
                ax_top.text(circ_x, circ_y, str(rank), transform=ax_top.transAxes, color=WHITE, fontsize=29, fontweight="bold",
                           ha="center", va="center", bbox=dict(boxstyle="circle,pad=0.25", facecolor=GOLD, edgecolor=GOLD), zorder=6)
                branch_y = y + 0.002
                rep_y = y - 0.047
                amount_y = (branch_y + rep_y) / 2 + 0.010
                ax_top.text(x0 + 0.06, branch_y, branch[:12], transform=ax_top.transAxes, color=TEXT, fontsize=28, fontweight="bold", ha="left", va="center", zorder=6)
                ax_top.text(x0 + card_w - 0.012, amount_y, fmt_money(amt), transform=ax_top.transAxes, color=side_color, fontsize=36, fontweight="bold", ha="right", va="center", zorder=6)
                rep = f"代表權證：{wcode} {wname[:10]}｜{fmt_money(wamt)}"
                ax_top.text(x0 + 0.06, rep_y, rep, transform=ax_top.transAxes, color=MUTED, fontsize=28, ha="left", va="center", zorder=6)
                ax_top.plot([x0 + 0.02, x0 + 0.44], [y - 0.100, y - 0.100], transform=ax_top.transAxes, color=GRID, linewidth=0.8, alpha=0.65, zorder=5)
                y -= row_gap

    # Notes row
    ax_notes = fig.add_subplot(gs[7, :]); ax_notes.set_axis_off(); ax_notes.set_facecolor(BG)
    for x0, title in [(0.02, "本週重點"), (0.52, "本週新聞 / 題材")]:
        note_y = 0.005
        note_w = 0.52
        note_h = 0.975
        note_band_h = 0.040
        draw_rounded_panel_with_top_band(
            ax_notes,
            x0,
            note_y,
            note_w,
            note_h,
            band_h=note_band_h,
            rounding=0.022,
            linewidth=1.25,
        )
        ax_notes.text(x0 + 0.02, note_y + note_h - 0.105, title, transform=ax_notes.transAxes, color=GOLD, fontsize=46, fontweight="bold", ha="left", va="top", clip_on=False, zorder=6)
    notes_fontsize = 32
    notes_line_height = 0.058
    notes_item_gap = 0.036
    notes_max_lines = 5
    notes_right_padding = 0.025

    def wrap_text_by_pixel(ax, fig, text, max_width_axes, fontsize=33, fontweight="normal", max_lines=3, first_prefix="", next_prefix=""):
        """依照實際像素寬度自動換行，避免固定字數造成太早換行或超出區塊邊界。"""
        s = str(text or "").strip()
        if not s:
            return ""

        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        ax_bbox = ax.get_window_extent(renderer=renderer)
        max_width_px = max(float(max_width_axes), 0.01) * ax_bbox.width

        width_cache = {}

        def measure_px(candidate: str) -> float:
            if candidate in width_cache:
                return width_cache[candidate]
            tmp = ax.text(
                0, 0, candidate,
                transform=ax.transAxes,
                fontsize=fontsize,
                fontweight=fontweight,
                ha="left",
                va="top",
                alpha=0,
            )
            bbox = tmp.get_window_extent(renderer=renderer)
            tmp.remove()
            width_cache[candidate] = bbox.width
            return bbox.width

        lines = []
        current = ""
        for ch in s:
            prefix = first_prefix if not lines else next_prefix
            candidate = current + ch
            if measure_px(prefix + candidate) <= max_width_px or not current:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = ch.lstrip()

        if current:
            lines.append(current.rstrip())

        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            last_prefix = first_prefix if max_lines == 1 else next_prefix
            last = lines[-1].rstrip()
            while last and measure_px(last_prefix + last + "…") > max_width_px:
                last = last[:-1].rstrip()
            lines[-1] = (last + "…") if last else "…"

        return "\n".join(lines)

    def draw_note_items(items, x_left, x_right, y_start):
        y = y_start
        max_width_axes = max(0.05, x_right - x_left)
        for p in items:
            body = wrap_text_by_pixel(
                ax_notes,
                fig,
                p,
                max_width_axes=max_width_axes,
                fontsize=notes_fontsize,
                fontweight="normal",
                max_lines=notes_max_lines,
                first_prefix="• ",
                next_prefix="  ",
            )
            note_text = "• " + body.replace("\n", "\n  ")
            line_count = note_text.count("\n") + 1
            ax_notes.text(
                x_left, y, note_text,
                transform=ax_notes.transAxes,
                color=TEXT,
                fontsize=notes_fontsize,
                ha="left",
                va="top",
                linespacing=1.12,
                clip_on=True,
            )
            y -= notes_line_height * line_count + notes_item_gap

    draw_note_items(key_points[:4], 0.04, 0.02 + 0.55 - notes_right_padding, 0.775)
    draw_note_items(news_points[:NEWS_DISPLAY_MAX_POINTS], 0.54, 0.52 + 0.55 - notes_right_padding, 0.775)

    # x ticks
    interval = max(1, len(x) // 12)
    for ax in [candle_ax, vol_ax, inst_ax, wnet_ax]:
        ax.set_xlim(-1, len(x))
    wnet_ax.set_xticks(x[::interval])
    wnet_ax.set_xticklabels([date_labels[i] for i in range(0, len(date_labels), interval)], rotation=30, ha="right", color=MUTED, fontsize=26)
    for ax in [candle_ax, vol_ax, inst_ax]:
        plt.setp(ax.get_xticklabels(), visible=False)

    add_center_watermarks(fig)

    fig.subplots_adjust(left=0.035, right=0.965, top=0.975, bottom=0.03)
    return fig


# ============================================================
# 對外入口
# ============================================================

def generate_warrant_report(stock_code: str) -> io.BytesIO:
    try:
        stock_code = str(stock_code).strip()
        stock_name = get_tw_stock_name(stock_code)
        stock_df, market, yf_code = fetch_stock_data_yf(stock_code, period="180d")
        if stock_df is None or stock_df.empty:
            print(f"❌ 股價資料不足：{stock_code}")
            return None
        stock_df = calculate_indicators(stock_df)
        stock_df["Close_prev"] = stock_df["Close"].shift(1)

        # 三大法人資料：對齊股價日期，讓週報可顯示三大法人買賣超。
        inst_df = fetch_inst_60d_from_x(stock_code, days=max(CHART_LOOKBACK + 10, 80))
        if inst_df is not None and not inst_df.empty:
            inst_df = inst_df.copy()
            inst_df["Date"] = pd.to_datetime(inst_df["Date"]).dt.tz_localize(None)
            inst_df = inst_df.set_index("Date").sort_index()
            stock_df = stock_df.join(inst_df[["foreign", "invest", "dealer", "total"]], how="left")
        for c in ["foreign", "invest", "dealer", "total"]:
            if c not in stock_df.columns:
                stock_df[c] = 0.0
        stock_df[["foreign", "invest", "dealer", "total"]] = stock_df[["foreign", "invest", "dealer", "total"]].fillna(0.0)

        plot_df = stock_df.tail(CHART_LOOKBACK)
        start_date = pd.Timestamp(plot_df.index.min()).normalize()
        end_date = pd.Timestamp(plot_df.index.max()).normalize()

        print(f"🚀 產生 {stock_code} {stock_name} 權證資金流週報，資料區間 {start_date.date()} ~ {end_date.date()}")
        warrant_events = fetch_warrant_events_full_market(stock_code, stock_name, start_date=start_date, end_date=end_date)
        print(f"✅ 權證分點事件總筆數：{len(warrant_events):,}")
        news_items = fetch_google_news_articles(stock_code, stock_name, max_items=NEWS_GOOGLE_MAX_ITEMS)

        fig = plot_weekly_report(stock_code, stock_name, stock_df, warrant_events, news_items)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=220, bbox_inches="tight", pad_inches=0.18, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        import traceback
        print(f"❌ 產生權證週報錯誤：{e}")
        traceback.print_exc()
        return None


def generate_k_chart(stock_code: str) -> io.BytesIO:
    """保留相容舊呼叫。"""
    return generate_warrant_report(stock_code)
