import io
import json
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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.ticker import FuncFormatter


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
API5_DAYS = int(os.getenv("WARRANT_API5_DAYS", "12"))
API4_SCAN_CALENDAR_DAYS = int(os.getenv("WARRANT_API4_SCAN_CALENDAR_DAYS", "10"))
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

    # 1) 技術面：支撐 / 趨勢
    if close >= ma5 >= ma20:
        points.append(f"技術面：收盤 {close:.0f} 站上 5MA {ma5:.1f} 與 20MA {ma20:.1f}，下週先觀察短均是否延續上彎。")
    elif close >= ma20:
        points.append(f"技術面：收盤 {close:.0f} 仍守 20MA {ma20:.1f}，但需觀察能否重新站回 5MA {ma5:.1f}。")
    else:
        points.append(f"技術面：收盤 {close:.0f} 低於 20MA {ma20:.1f}，下週需優先觀察月線是否轉為壓力。")

    # 2) KD：過熱 / 鈍化 / 轉弱風險
    if not pd.isna(k9) and not pd.isna(d9):
        if k9 >= 80 and d9 >= 80:
            points.append(f"動能面：KD 位於高檔（K {k9:.1f} / D {d9:.1f}），若股價續強可視為高檔鈍化，若跌破短均則留意轉弱。")
        elif k9 > d9:
            points.append(f"動能面：K 值 {k9:.1f} 高於 D 值 {d9:.1f}，短線動能仍在，但需搭配量能確認。")
        else:
            points.append(f"動能面：K 值 {k9:.1f} 低於 D 值 {d9:.1f}，下週需觀察是否重新黃金交叉。")

    # 3) 權證籌碼：淨流向延續
    net = float(ctx.get("total_net", 0))
    if net > 0:
        points.append(f"權證籌碼：本週淨買超 {fmt_money(net)}，下週若單日柱狀續紅且累計線上彎，代表追價資金延續。")
    elif net < 0:
        points.append(f"權證籌碼：本週淨賣超 {fmt_money(net)}，下週若仍續綠，需留意權證資金退潮與短線降溫。")
    else:
        points.append("權證籌碼：本週淨流向接近中性，下週需觀察是否出現連續兩日以上明確紅柱或綠柱。")

    # 4) 分點集中度 / 代表分點
    e = ctx.get("week_events")
    if e is not None and not e.empty:
        by_branch = e.groupby("branch")["net_amount"].sum().sort_values(ascending=False)
        if not by_branch.empty:
            top_branch = str(by_branch.index[0])
            top_amt = float(by_branch.iloc[0])
            points.append(f"分點觀察：目前以「{top_branch}」領軍 {fmt_money(top_amt)}，下週需觀察是否續買或轉為調節。")

    # 5) 新聞 / 事件，只補一則避免版面過滿
    news_points = build_news_points(ctx.get("stock_code", ""), stock_name, news_titles, ctx)
    for p in news_points[:1]:
        points.append(p)

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
    e["branch"] = e["branch"].replace("", "未知分點")
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


def build_key_points(ctx, stock_name: str):
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

    if ctx.get("hedge_removed", 0) > 0:
        points.append(f"本週已過濾疑似造市 / 避險紀錄 {ctx['hedge_removed']} 筆，降低發行商自營單干擾。")
    elif ctx.get("hedge_candidates", 0) > 0:
        points.append(f"偵測到疑似買賣對沖紀錄 {ctx['hedge_candidates']} 筆；目前保留不刪除，避免誤刪大額主力單。")
    return points[:4]


# ============================================================
# 新聞抓取：先做可用版，後續可再換 MOPS / OpenAI 摘要
# ============================================================

def fetch_google_news_titles(stock_code: str, stock_name: str, max_items: int = 5) -> List[str]:
    manual = os.getenv("WEEKLY_NEWS_TEXT", "").strip()
    if manual:
        parts = [x.strip() for x in re.split(r"[\n；;]+", manual) if x.strip()]
        return parts[:max_items]
    if not NEWS_ENABLE:
        return []
    query = f'"{stock_code}" OR "{stock_name}" when:7d'
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
        titles = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
            if title and title not in titles:
                titles.append(title)
            if len(titles) >= max_items:
                break
        return titles
    except Exception as e:
        print(f"⚠️ Google News RSS 抓取失敗：{e}")
        return []


def build_news_points(stock_code: str, stock_name: str, news_titles: List[str], ctx: dict | None = None) -> List[str]:
    """將新聞整理成較適合週報的重點句。"""
    titles = []
    seen = set()
    for t in news_titles or []:
        s = re.sub(r"\s+", " ", str(t or "").strip())
        if not s or s in seen:
            continue
        seen.add(s)
        titles.append(s)

    def find_any(keywords):
        for tt in titles:
            if any(k in tt for k in keywords):
                return tt
        return ""

    points = []
    rating = find_any(["調升", "調降", "評等", "目標價", "外資", "投信", "券商"])
    industry = find_any(["DRAM", "記憶體", "半導體", "報價", "AI", "伺服器", "需求", "供給"])
    company = find_any([stock_name, stock_code, "法說", "營收", "資本支出", "股利", "產能"])

    if rating:
        points.append(f"法人觀點：{rating}")
    if industry:
        points.append(f"產業動向：{industry}")
    if company:
        points.append(f"公司消息：{company}")
    if not points and titles:
        points = [f"本週消息：{t}" for t in titles[:2]]

    if ctx is not None:
        direction = "偏買超" if ctx.get("total_net", 0) > 0 else "偏賣超" if ctx.get("total_net", 0) < 0 else "多空拉鋸"
        points.append(f"資金面：本週權證淨流向 {fmt_money(ctx.get('total_net', 0))}，整體籌碼 {direction}。")

    if not points:
        points = ["若要更精準的『本週重要消息』，建議以環境變數 WEEKLY_NEWS_TEXT 手動覆寫。"]
    return points[:3]


# ============================================================
# 繪圖工具
# ============================================================

def style_ax(ax, title=None, title_color=GOLD):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=20)
    for spine in ax.spines.values():
        spine.set_color(GRID)
        spine.set_linewidth(1.1)
    ax.grid(True, color=GRID, alpha=0.35, linewidth=0.7)
    if title:
        ax.set_title(title, loc="left", fontsize=26, color=title_color, fontweight="bold", pad=14)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_color(MUTED)


def add_panel_title(ax, title, subtitle=""):
    ax.text(0.01, 0.96, title, transform=ax.transAxes, ha="left", va="top", color=TEXT, fontsize=16, fontweight="bold")
    if subtitle:
        ax.text(0.01, 0.86, subtitle, transform=ax.transAxes, ha="left", va="top", color=MUTED, fontsize=11)


def add_weighted_volume_profile_overlay(ax, df: pd.DataFrame, n_bins: int = 38, color="#38BDF8", alpha=0.18, scale=1.08):
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
            rect_alpha = 0.34
        elif i == second_idx:
            rect_color = "#F59E0B"   # 第二大量：橘色
            rect_alpha = 0.30
        else:
            rect_color = color       # 其餘維持原本淺藍
            rect_alpha = alpha
        ax.add_patch(Rectangle((x_min, centers[i] - height / 2), w, height, color=rect_color, alpha=rect_alpha, zorder=0, clip_on=True))
    ax.set_xlim(x_min, x_max)


def draw_card(ax, x, y, w, h, label, value, sub="", value_color=GOLD):
    box = FancyBboxPatch((x, y), w, h, transform=ax.transAxes,
                         boxstyle="round,pad=0.016,rounding_size=0.025",
                         facecolor=PANEL2, edgecolor=GOLD, linewidth=1.5)
    ax.add_patch(box)
    ax.add_patch(Rectangle((x, y + h - 0.055), w, 0.055, transform=ax.transAxes,
                           facecolor=GOLD, edgecolor=GOLD, linewidth=0, alpha=0.95))
    ax.text(x + w / 2, y + h - 0.15, label, transform=ax.transAxes, color=MUTED, fontsize=21, ha="center", va="top")
    ax.text(x + w / 2, y + 0.43, value, transform=ax.transAxes, color=value_color, fontsize=32, fontweight="bold", ha="center", va="center")
    if sub:
        ax.text(x + w / 2, y + 0.10, sub, transform=ax.transAxes, color=MUTED, fontsize=18, ha="center", va="bottom")

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


def plot_weekly_report(stock_code: str, stock_name: str, stock_df: pd.DataFrame, warrant_events: pd.DataFrame, news_titles: List[str]):
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
    watch_points = build_watch_points(ctx, stock_name, news_titles)

    fig = plt.figure(figsize=(26, 48), facecolor=BG)
    gs = GridSpec(8, 12, figure=fig,
                  height_ratios=[1.15, 1.95, 6.7, 2.25, 2.85, 4.45, 9.1, 7.0],
                  hspace=0.28, wspace=0.25)

    # Header
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.set_axis_off()
    period = f"{ctx['week_start'].strftime('%Y/%m/%d')} - {ctx['week_end'].strftime('%Y/%m/%d')}" if pd.notna(ctx["week_start"]) else "-"
    ax_header.text(0.01, 0.62, f"{stock_code} {stock_name}｜權證資金流週報", color=GOLD, fontsize=46, fontweight="bold", ha="left", va="center")
    ax_header.text(0.01, 0.15, f"週報區間：{period}｜資訊僅供教育參考", color=MUTED, fontsize=21, ha="left", va="center")
    ax_header.text(0.99, 0.62, "By 股市艾斯出品  轉傳請註明", color=MUTED, fontsize=24, fontweight="bold", ha="right", va="center")

    # Cards
    ax_cards = fig.add_subplot(gs[1, :]); ax_cards.set_axis_off()
    card_w, gap = 0.188, 0.01
    cards = [
        ("本週股價", fmt_pct(ctx["stock_ret"]), "收盤對週初", RED if ctx["stock_ret"] >= 0 else GREEN),
        ("本週量能", fmt_pct(ctx["vol_change"]), "相對前 5 交易日", RED if (not np.isnan(ctx["vol_change"]) and ctx["vol_change"] >= 0) else GREEN),
        ("權證週淨流向", fmt_money(ctx["total_net"]), ctx["bias"], RED if ctx["total_net"] >= 0 else GREEN),
        ("本週買進", fmt_money_abs(ctx["total_buy"]), "全部分點合計", RED),
        ("本週賣出", fmt_money_abs(ctx["total_sell"]), "全部分點合計", GREEN),
    ]
    for i, (lab, val, sub, col) in enumerate(cards):
        draw_card(ax_cards, 0.01 + i * (card_w + gap), 0.06, card_w, 0.88, lab, val, sub, col)

    # K line
    candle_ax = fig.add_subplot(gs[2, :])
    style_ax(candle_ax, "股價趨勢｜K線、均線、布林與價量分布")
    plot_candles(candle_ax, plot_df, x)
    candle_ax.plot(x, plot_df["MA5"], color=RED, linewidth=1.6, label=f"5MA {plot_df['MA5'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA10"], color=ORANGE, linewidth=1.3, label=f"10MA {plot_df['MA10'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA20"], color=LIME, linewidth=1.3, label=f"20MA {plot_df['MA20'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["MA60"], color=BLUE, linewidth=1.4, label=f"60MA {plot_df['MA60'].iloc[-1]:.2f}")
    candle_ax.plot(x, plot_df["BB_UPPER"], linestyle="--", color=MUTED, linewidth=0.9, alpha=0.9)
    candle_ax.plot(x, plot_df["BB_LOWER"], linestyle="--", color=MUTED, linewidth=0.9, alpha=0.9)
    add_weighted_volume_profile_overlay(candle_ax, plot_df)
    candle_ax.legend(loc="upper left", ncol=4, frameon=False, fontsize=18, labelcolor=TEXT)
    candle_ax.yaxis.tick_right()
    latest = plot_df.iloc[-1]
    prev_close = plot_df["Close"].iloc[-2] if len(plot_df) >= 2 else latest["Close"]
    diff = latest["Close"] - prev_close
    pct = diff / prev_close * 100 if prev_close else np.nan
    latest_info = f"{plot_df.index[-1].strftime('%Y/%m/%d')}  開 {latest['Open']:.2f}  高 {latest['High']:.2f}  低 {latest['Low']:.2f}  收 {latest['Close']:.2f}  {diff:+.2f} ({pct:+.2f}%)"
    candle_ax.text(0.012, 0.92, latest_info, transform=candle_ax.transAxes, color=TEXT, fontsize=19, ha="left", va="top",
                   bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.30", alpha=0.95))
    ma_note = get_ma_kline_signals(plot_df)
    if ma_note:
        candle_ax.text(0.5, 0.08, ma_note, transform=candle_ax.transAxes, color=GOLD, fontsize=23, fontweight="bold", ha="center", va="center",
                       bbox=dict(facecolor="#F6F8FB", edgecolor=GOLD, boxstyle="round,pad=0.28", alpha=0.95))

    # Volume
    vol_ax = fig.add_subplot(gs[3, :], sharex=candle_ax)
    style_ax(vol_ax, "成交量")
    up = plot_df["Close"] >= plot_df["Open"]
    vol_lots = plot_df["Volume"] / 1000
    vol_ax.bar([i for i in x if up.iloc[i]], vol_lots[up], color=RED, width=0.72, alpha=0.72)
    vol_ax.bar([i for i in x if not up.iloc[i]], vol_lots[~up], color=GREEN, width=0.72, alpha=0.72)
    vol_ax.plot(x, plot_df["MV5"] / 1000, color=BLUE, linewidth=1.2, label=f"MV5 {plot_df['MV5'].iloc[-1] / 1000:,.0f}張")
    vol_ax.plot(x, plot_df["MV20"] / 1000, color=PURPLE, linewidth=1.2, label=f"MV20 {plot_df['MV20'].iloc[-1] / 1000:,.0f}張")
    vol_ax.legend(loc="upper left", frameon=False, fontsize=18, labelcolor=TEXT)
    vol_ax.yaxis.tick_right()

    # KD only (週報版保留單一指標節省空間)
    kd_ax = fig.add_subplot(gs[4, :], sharex=candle_ax)
    style_ax(kd_ax, "KD 指標")
    kd_ax.plot(x, plot_df["K9"], color=BLUE, linewidth=2.2, label=f"K9 {plot_df['K9'].iloc[-1]:.2f}")
    kd_ax.plot(x, plot_df["D9"], color=ORANGE, linewidth=2.2, label=f"D9 {plot_df['D9'].iloc[-1]:.2f}")
    kd_ax.plot(x, plot_df["J9"], color=LIME, linewidth=1.9, label=f"J9 {plot_df['J9'].iloc[-1]:.2f}")
    kd_ax.axhline(80, color=RED, linestyle="--", linewidth=0.8, alpha=0.5)
    kd_ax.axhline(20, color=GREEN, linestyle="--", linewidth=0.8, alpha=0.5)
    kd_ax.legend(loc="upper left", frameon=False, fontsize=18, labelcolor=TEXT)
    kd_note = get_kd_signals(plot_df)
    if kd_note:
        kd_ax.text(0.98, 0.93, kd_note, transform=kd_ax.transAxes, ha="right", va="top", color=GOLD, fontsize=15, fontweight="bold")

    # Warrant daily net bars + cumulative line
    wnet_ax = fig.add_subplot(gs[5, :], sharex=candle_ax)
    style_ax(wnet_ax, "權證資金流｜柱狀 = 單日淨買賣超；折線 = 累計淨買賣超")
    vals = daily_net["net_amount"].astype(float).values
    cum_vals = np.cumsum(vals)
    wnet_ax.bar(x, vals, color=[RED if v >= 0 else GREEN for v in vals], width=0.75, alpha=0.85, label="單日淨買賣超")
    wnet_ax.axhline(0, color=MUTED, linestyle="--", linewidth=1)
    wnet_ax.yaxis.set_major_formatter(FuncFormatter(money_tick))
    wnet_ax.yaxis.tick_right()
    wnet_ax2 = wnet_ax.twinx()
    wnet_ax2.plot(x, cum_vals, color=BLUE, linewidth=1.8, alpha=0.95, label="累計淨買賣超")
    if len(cum_vals):
        cmax, cmin = float(np.nanmax(cum_vals)), float(np.nanmin(cum_vals))
        lim = max(abs(cmax), abs(cmin), 1.0)
        # 讓累計折線的 0 軸位於面板中間，避免折線貼在最下方
        wnet_ax2.set_ylim(-lim * 2.05, lim * 2.05)
    wnet_ax2.tick_params(colors=MUTED, labelsize=14)
    wnet_ax2.yaxis.set_major_formatter(FuncFormatter(money_tick))
    for spine in wnet_ax2.spines.values():
        spine.set_visible(False)
    wnet_ax2.grid(False)
    h1, l1 = wnet_ax.get_legend_handles_labels()
    h2, l2 = wnet_ax2.get_legend_handles_labels()
    wnet_ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=18, labelcolor=TEXT)
    latest_net = vals[-1] if len(vals) else 0.0
    latest_cum = cum_vals[-1] if len(cum_vals) else 0.0
    wnet_ax.text(0.99, 0.93, f"最新日 {fmt_money(latest_net)}｜本週合計 {fmt_money(ctx['total_net'])}｜累計 {fmt_money(latest_cum)}", transform=wnet_ax.transAxes,
                 color=TEXT, fontsize=15, fontweight="bold", ha="right", va="top",
                 bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.25", alpha=0.95))

    # TOP5 tables
    ax_top = fig.add_subplot(gs[6, :])
    ax_top.set_axis_off()
    ax_top.set_facecolor(BG)
    sections = [
        (0.02, "本週淨買超分點 TOP5", buy_top, RED),
        (0.52, "本週淨賣超分點 TOP5", sell_top, GREEN),
    ]
    for x0, title, df_top, side_color in sections:
        ax_top.add_patch(FancyBboxPatch((x0, 0.025), 0.46, 0.955, transform=ax_top.transAxes,
                                        boxstyle="round,pad=0.014,rounding_size=0.02", facecolor=PANEL2, edgecolor=GOLD, linewidth=1.35))
        ax_top.text(x0 + 0.02, 0.955, title, transform=ax_top.transAxes, color=side_color, fontsize=30, fontweight="bold", ha="left", va="top")
        ax_top.text(x0 + 0.02, 0.875, "分點｜本週淨額｜代表權證（該分點本週金額最大）", transform=ax_top.transAxes, color=MUTED, fontsize=21, ha="left", va="top")
        if df_top.empty:
            ax_top.text(x0 + 0.03, 0.60, "本週無符合資料", transform=ax_top.transAxes, color=MUTED, fontsize=21, ha="left", va="center")
        else:
            y = 0.755
            row_gap = 0.147
            for rank, (_, r) in enumerate(df_top.iterrows(), 1):
                branch = str(r["branch"]) or "未知分點"
                amt = float(r["net_amount"])
                wcode = str(r.get("max_warrant_code", ""))
                wname = str(r.get("max_warrant_name", ""))
                wamt = float(r.get("max_warrant_amount", 0.0))
                # rank circle
                circ_x = x0 + 0.03
                circ_y = y - 0.005
                ax_top.text(circ_x, circ_y, str(rank), transform=ax_top.transAxes, color=WHITE, fontsize=21, fontweight="bold",
                           ha="center", va="center", bbox=dict(boxstyle="circle,pad=0.23", facecolor=GOLD, edgecolor=GOLD))
                ax_top.text(x0 + 0.06, y + 0.012, branch[:12], transform=ax_top.transAxes, color=TEXT, fontsize=20, fontweight="bold", ha="left", va="center")
                ax_top.text(x0 + 0.425, y + 0.012, fmt_money(amt), transform=ax_top.transAxes, color=side_color, fontsize=24, fontweight="bold", ha="right", va="center")
                rep = f"代表權證：{wcode} {wname[:14]}｜{fmt_money(wamt)}"
                ax_top.text(x0 + 0.06, y - 0.060, rep, transform=ax_top.transAxes, color=MUTED, fontsize=20, ha="left", va="center")
                ax_top.plot([x0 + 0.02, x0 + 0.44], [y - 0.112, y - 0.112], transform=ax_top.transAxes, color=GRID, linewidth=0.8, alpha=0.65)
                y -= row_gap

    # Notes row
    ax_notes = fig.add_subplot(gs[7, :]); ax_notes.set_axis_off(); ax_notes.set_facecolor(BG)
    for x0, title in [(0.02, "本週重點"), (0.52, "下週觀察 / 重要消息")]:
        ax_notes.add_patch(FancyBboxPatch((x0, 0.045), 0.46, 0.91, transform=ax_notes.transAxes,
                                          boxstyle="round,pad=0.014,rounding_size=0.02", facecolor=PANEL2, edgecolor=GOLD, linewidth=1.25))
        ax_notes.text(x0 + 0.02, 0.895, title, transform=ax_notes.transAxes, color=GOLD, fontsize=34, fontweight="bold", ha="left", va="top")
    y = 0.735
    for p in key_points[:4]:
        ax_notes.text(0.04, y, "• " + wrap_text(p, width=38, max_lines=2), transform=ax_notes.transAxes, color=TEXT, fontsize=21, ha="left", va="top")
        y -= 0.198
    y = 0.735
    for p in watch_points[:5]:
        ax_notes.text(0.54, y, "• " + wrap_text(p, width=38, max_lines=2), transform=ax_notes.transAxes, color=TEXT, fontsize=21, ha="left", va="top")
        y -= 0.198

    # x ticks
    interval = max(1, len(x) // 12)
    for ax in [candle_ax, vol_ax, kd_ax, wnet_ax]:
        ax.set_xlim(-1, len(x))
    wnet_ax.set_xticks(x[::interval])
    wnet_ax.set_xticklabels([date_labels[i] for i in range(0, len(date_labels), interval)], rotation=30, ha="right", color=MUTED, fontsize=18)
    for ax in [candle_ax, vol_ax, kd_ax]:
        plt.setp(ax.get_xticklabels(), visible=False)

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
        plot_df = stock_df.tail(CHART_LOOKBACK)
        start_date = pd.Timestamp(plot_df.index.min()).normalize()
        end_date = pd.Timestamp(plot_df.index.max()).normalize()

        print(f"🚀 產生 {stock_code} {stock_name} 權證資金流週報，資料區間 {start_date.date()} ~ {end_date.date()}")
        warrant_events = fetch_warrant_events_full_market(stock_code, stock_name, start_date=start_date, end_date=end_date)
        print(f"✅ 權證分點事件總筆數：{len(warrant_events):,}")
        news_titles = fetch_google_news_titles(stock_code, stock_name, max_items=5)

        fig = plot_weekly_report(stock_code, stock_name, stock_df, warrant_events, news_titles)
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
