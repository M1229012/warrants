import io
import json
import html
import base64
import hashlib
import math
import os
import re
import textwrap
import time
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, TimeoutError as FuturesTimeoutError
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

try:
    from PIL import Image
except Exception:
    Image = None

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

# MoneyDJ 權證搜尋備援 / 補漏：
# 1. OpenAPI 抓不到任何權證母體時，仍維持原本備援：MoneyDJ Search 全部補進，不過濾成交量 0。
# 2. OpenAPI 已抓到部分權證時，也會額外跑 MoneyDJ Search 補漏；
#    預設只補 MoneyDJ Search 顯示成交量 >= 1 的漏網認購權證，避免把同標的全部 0 量權證都丟進 API4/API5 拖慢。
# 3. 若要完全不看 MoneyDJ 成交量、全部補漏，可設 WARRANT_MONEYDJ_SEARCH_SUPPLEMENT_MIN_VOLUME=0。
MONEYDJ_WARRANT_SEARCH_PAGE = "https://www.moneydj.com/warrant/xdjhtm/Search.xdjhtm"
MONEYDJ_WARRANT_PROXY_URL = "https://www.moneydj.com/warrant/xdjjs/ProxyXQ.xdjjs"
MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_ENABLE = os.getenv("WARRANT_MONEYDJ_SEARCH_SUPPLEMENT_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_MIN_VOLUME = int(os.getenv("WARRANT_MONEYDJ_SEARCH_SUPPLEMENT_MIN_VOLUME", "1"))

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

# 週報輸出模式：
# - full：完整週報，維持原本所有區塊。
# - compact：精簡週報，移除「本週重點」與「本週新聞 / 題材觀察」兩個區塊，
#   並直接縮短畫布高度，不保留空白；同時略過新聞抓取與相關 Gemini 統整。
# GitHub Actions 可將 workflow_dispatch 的選項值傳入 WARRANT_REPORT_MODE。
REPORT_MODE_RAW = os.getenv("WARRANT_REPORT_MODE", "full").strip() or "full"
_REPORT_MODE_KEY = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", REPORT_MODE_RAW.lower())
_REPORT_MODE_COMPACT_ALIASES = {
    "compact", "simple", "lite", "short", "nonews", "withoutnews", "chartsonly",
    "精簡", "精簡模式", "精簡週報", "不含本週重點與本週新聞",
    "精簡週報不含本週重點與本週新聞",
}
REPORT_MODE = "compact" if _REPORT_MODE_KEY in _REPORT_MODE_COMPACT_ALIASES else "full"


def is_compact_report_mode() -> bool:
    return REPORT_MODE == "compact"


def get_report_mode_label() -> str:
    return "精簡週報（不含本週重點與本週新聞）" if is_compact_report_mode() else "完整週報"
# 截圖式輸出設定：先用原本高解析度產圖，再等比例縮小後輸出，模擬「截圖後送出」以降低檔案大小。
SCREENSHOT_OUTPUT_ENABLE = os.getenv("WARRANT_SCREENSHOT_OUTPUT_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
SCREENSHOT_OUTPUT_SCALE = float(os.getenv("WARRANT_SCREENSHOT_OUTPUT_SCALE", "0.6"))
# 截圖式輸出改用最大寬度限制，讓實際效果更接近「螢幕截圖」；0 代表不限制。
SCREENSHOT_OUTPUT_MAX_WIDTH = int(os.getenv("WARRANT_SCREENSHOT_OUTPUT_MAX_WIDTH", "2400"))
SCREENSHOT_OUTPUT_FORMAT = os.getenv("WARRANT_SCREENSHOT_OUTPUT_FORMAT", "PNG").strip().upper() or "PNG"
SCREENSHOT_OUTPUT_JPEG_QUALITY = int(os.getenv("WARRANT_SCREENSHOT_OUTPUT_JPEG_QUALITY", "88"))
# PNG 仍是無損格式，長圖可能很大；轉成 256 色調色盤 PNG 可大幅縮檔，文字線條通常仍清楚。
SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE = os.getenv("WARRANT_SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")

# 疑似造市 / 避險對沖設定
# 預設只標記不刪除：8% 用於提示疑似對沖；若真的啟用刪除，3% 才會過濾。
HEDGE_MARK_THRESHOLD = float(os.getenv("WARRANT_HEDGE_MARK_THRESHOLD", os.getenv("WARRANT_HEDGE_THRESHOLD", "0.08")))
HEDGE_FILTER_THRESHOLD = float(os.getenv("WARRANT_HEDGE_FILTER_THRESHOLD", "0.03"))
HEDGE_FILTER_ENABLE = os.getenv("WARRANT_HEDGE_FILTER_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
HEDGE_MIN_GROSS_AMOUNT = float(os.getenv("WARRANT_HEDGE_MIN_GROSS_AMOUNT", "3000000"))
HEDGE_MIN_SIDE_AMOUNT = float(os.getenv("WARRANT_HEDGE_MIN_SIDE_AMOUNT", "1000000"))

# TOP5 專用：同一天、同一檔權證，若不同券商 / 分點一買一賣金額高度接近，視為疑似對手單。
# 預設關閉：避免 TOP5 因疑似對手單條件過嚴而誤刪；如未來要手動啟用，可設 WARRANT_CROSS_BROKER_OFFSET_FILTER_ENABLE=1。
CROSS_BROKER_OFFSET_FILTER_ENABLE = os.getenv("WARRANT_CROSS_BROKER_OFFSET_FILTER_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
CROSS_BROKER_OFFSET_THRESHOLD = float(os.getenv("WARRANT_CROSS_BROKER_OFFSET_THRESHOLD", "0.03"))
CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT = float(os.getenv("WARRANT_CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT", "1000000"))

# TOP5 專用：排除券商總公司型分點。
# 只排除「總公司本身」或明確含總公司 / 總部 / 本部 / 自營等字樣的分點，
# 不排除地方分點，例如富邦公益、元大南屯、群益金鼎某分點。
TOP5_EXCLUDE_HEAD_OFFICE_BRANCH_ENABLE = os.getenv("WARRANT_TOP5_EXCLUDE_HEAD_OFFICE_BRANCH_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
TOP5_EXTRA_HEAD_OFFICE_BRANCHES = os.getenv("WARRANT_TOP5_EXTRA_HEAD_OFFICE_BRANCHES", "").strip()
# TOP5 總公司型分點白名單：
# 預設仍過濾總公司型分點，但「新光」與「第一金」這兩個分點保留，不列入總公司過濾。
TOP5_HEAD_OFFICE_BRANCH_ALLOWLIST = os.getenv("WARRANT_TOP5_HEAD_OFFICE_BRANCH_ALLOWLIST", "新光,第一金,福邦證券").strip()

# 精選分點資金流：只統計指定分點的權證買賣金額，不再設定單筆金額門檻。
# 預設仍是原本五分點；Discord / GitHub Actions 可用 WARRANT_SELECTED_BRANCH_FLOW_BRANCHES 傳入自訂分點。
# 若明確設定 WARRANT_SELECTED_BRANCH_FLOW_MODE=default / five / 五分點，會強制使用預設五分點。
SELECTED_BRANCH_FLOW_ENABLE = os.getenv("WARRANT_SELECTED_BRANCH_FLOW_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES = os.getenv(
    "WARRANT_SELECTED_BRANCH_FLOW_DEFAULT_BRANCHES",
    "華南永昌台中,元大南屯,新光,永豐金內湖,富邦敦南",
).strip() or "華南永昌台中,元大南屯,新光,永豐金內湖,富邦敦南"
SELECTED_BRANCH_FLOW_MODE = os.getenv("WARRANT_SELECTED_BRANCH_FLOW_MODE", "").strip().lower()
_SELECTED_BRANCH_FLOW_BRANCHES_RAW = os.getenv("WARRANT_SELECTED_BRANCH_FLOW_BRANCHES", "").strip()
_SELECTED_BRANCH_FLOW_DEFAULT_MODE_ALIASES = {
    "default", "preset", "five", "five_points", "fivepoints",
    "5", "5points", "五分點", "預設", "預設五分點",
}
if (
    SELECTED_BRANCH_FLOW_MODE in _SELECTED_BRANCH_FLOW_DEFAULT_MODE_ALIASES
    or _SELECTED_BRANCH_FLOW_BRANCHES_RAW.strip().lower() in _SELECTED_BRANCH_FLOW_DEFAULT_MODE_ALIASES
):
    SELECTED_BRANCH_FLOW_BRANCHES = DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES
else:
    SELECTED_BRANCH_FLOW_BRANCHES = _SELECTED_BRANCH_FLOW_BRANCHES_RAW or DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", os.getenv("GSHEET_NAME", "權證分點籌碼"))
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", os.getenv("GSHEET_ID", "")).strip()
GSHEET_STOCK_NAME_SHEET = os.getenv("WARRANT_STOCK_NAME_SHEET", "快取_股票名稱").strip() or "快取_股票名稱"
_STOCK_NAME_MAP_CACHE = None
_STOCK_NAME_MAP_CACHE_LOCK = threading.Lock()

# 權證快取設定：權證籌碼預設每次都重新抓 live，避免圖片使用舊籌碼；完整 live 結果仍會寫入 Google Sheet 當備援。
# 這裡刻意不受舊版 Actions 的「0=優先用快取」影響；除非明確設定 WARRANT_ALWAYS_REFRESH_WARRANT_FLOW=0，否則籌碼每次都走 live。
WARRANT_ALWAYS_REFRESH_WARRANT_FLOW = os.getenv("WARRANT_ALWAYS_REFRESH_WARRANT_FLOW", "1").strip().lower() not in ("0", "false", "no", "off")
WARRANT_CACHE_FORCE_REFRESH = WARRANT_ALWAYS_REFRESH_WARRANT_FLOW or os.getenv(
    "WARRANT_CACHE_FORCE_REFRESH",
    os.getenv("WARRANT_LOCAL_CACHE_FORCE_REFRESH", "0"),
).strip().lower() in ("1", "true", "yes", "on")
GSHEET_WARRANT_CACHE_ENABLE = os.getenv("WARRANT_GSHEET_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GSHEET_WARRANT_HISTORY_SHEET = os.getenv("WARRANT_GSHEET_HISTORY_SHEET", "快取_分點歷史").strip() or "快取_分點歷史"
GSHEET_WARRANT_STATUS_SHEET = os.getenv("WARRANT_GSHEET_STATUS_SHEET", "快取_分點歷史_狀態").strip() or "快取_分點歷史_狀態"

# 本機快照快取：預設關閉，避免 GitHub runner 本機快照蓋過 Google Sheet 快取。
LOCAL_WARRANT_CACHE_ENABLE = os.getenv("WARRANT_LOCAL_CACHE_ENABLE", "0").strip().lower() not in ("0", "false", "no", "off")
LOCAL_WARRANT_CACHE_DIR = os.getenv("WARRANT_LOCAL_CACHE_DIR", "warrant_cache").strip() or "warrant_cache"
LOCAL_WARRANT_CACHE_FORCE_REFRESH = WARRANT_CACHE_FORCE_REFRESH

# API 重試與完整度檢查：預設仍保守，但允許極少數硬失敗，避免大母體請求因單筆 timeout 整張中止。
# MoneyDJ 偶爾會短暫回 500，因此預設重試次數拉高，並在 API4 第一輪失敗後做第二輪低併發補抓。
API_RETRY_TIMES = int(os.getenv("WARRANT_API_RETRY_TIMES", "6"))
API_RETRY_BASE_WAIT = float(os.getenv("WARRANT_API_RETRY_BASE_WAIT", "2.0"))
API_FAILURE_ABORT_RATIO = float(os.getenv("WARRANT_API_FAILURE_ABORT_RATIO", "0.005"))
API_FAILURE_ABORT_ABS_COUNT = int(os.getenv("WARRANT_API_FAILURE_ABORT_ABS_COUNT", "3"))
API_FAILURE_ABORT_MIN_REQUESTS = int(os.getenv("WARRANT_API_FAILURE_ABORT_MIN_REQUESTS", "1"))
API_REQUIRE_FULL_SUCCESS = os.getenv("WARRANT_API_REQUIRE_FULL_SUCCESS", "1").strip().lower() not in ("0", "false", "no", "off")
API_ALLOW_TINY_FAILURE = os.getenv("WARRANT_API_ALLOW_TINY_FAILURE", "1").strip().lower() in ("1", "true", "yes", "on")
API_EMPTY_AS_FAILURE = os.getenv("WARRANT_API_EMPTY_AS_FAILURE", "0").strip().lower() in ("1", "true", "yes", "on")
API4_SECOND_PASS_ENABLE = os.getenv("WARRANT_API4_SECOND_PASS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
API4_SECOND_PASS_WORKERS = int(os.getenv("WARRANT_API4_SECOND_PASS_WORKERS", "6"))
API4_SECOND_PASS_WAIT = float(os.getenv("WARRANT_API4_SECOND_PASS_WAIT", "5"))
# 由 Google Sheet 歷史快取補進來的權證，若 API4 仍查不到，保留既有快取資料，不讓已到期 / 已下市權證拖垮整張圖。
API4_HISTORY_FAILURE_AS_EMPTY = os.getenv("WARRANT_API4_HISTORY_FAILURE_AS_EMPTY", "1").strip().lower() in ("1", "true", "yes", "on")

# Gemini / LLM 結果快取：同一份 prompt 重跑時直接重用，不再重打 API。
LLM_CACHE_ENABLE = os.getenv("WARRANT_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
LLM_CACHE_DIR = os.getenv("WARRANT_LLM_CACHE_DIR", "llm_cache").strip() or "llm_cache"
# Gemini 結果寫回 Google Sheet：同股票同任務當天跑過一次，當天再跑直接讀快取，不再呼叫 Gemini。
GSHEET_LLM_CACHE_ENABLE = os.getenv("WARRANT_GSHEET_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GSHEET_LLM_CACHE_SHEET = os.getenv("WARRANT_GSHEET_LLM_CACHE_SHEET", "快取_Gemini結果").strip() or "快取_Gemini結果"
LLM_CACHE_FORCE_REFRESH = os.getenv("WARRANT_LLM_CACHE_FORCE_REFRESH", "0").strip().lower() in ("1", "true", "yes", "on")

_THREAD_LOCAL = threading.local()
_FETCH_STATS_LOCK = threading.Lock()
_FETCH_STATS = {}

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

# Supertrend 目前圖表沒有繪製，預設不計算；若未來要畫再用環境變數打開。
ENABLE_SUPERTREND = os.getenv("WARRANT_ENABLE_SUPERTREND", "0").strip().lower() in ("1", "true", "yes", "on")

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


def normalize_branch_name(branch_name: str) -> str:
    """將分點名稱做全域標準化，避免同分點因空白、短橫線、全形符號或台/臺差異被拆成不同分點。"""
    if branch_name is None or (isinstance(branch_name, float) and pd.isna(branch_name)):
        return ""
    s = str(branch_name).strip()
    if not s or s in ("-", "--", "nan", "None"):
        return ""
    s = html.unescape(s)
    s = s.replace("臺", "台")
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("／", "/").replace("﹣", "-").replace("－", "-").replace("–", "-").replace("—", "-").replace("―", "-")
    # 只移除格式用分隔符，不移除券商或地名本身，避免把不同分點誤合併。
    s = re.sub(r"[\s　\-_\u2010-\u2015/\\|｜·．・•]+", "", s)
    s = re.sub(r"[()（）［］\[\]{}｛｝]+", "", s)
    s = s.strip()
    return s


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


def _safe_cache_part(v) -> str:
    s = str(v or "").strip()
    s = re.sub(r"[^0-9A-Za-z_\-]+", "_", s)
    return s.strip("_") or "unknown"


def _cache_date_part(v) -> str:
    dt = parse_date(v)
    if dt:
        return dt.strftime("%Y%m%d")
    try:
        return pd.Timestamp(v).strftime("%Y%m%d")
    except Exception:
        return _safe_cache_part(v)


def _ensure_dir(path: str):
    if path:
        os.makedirs(path, exist_ok=True)


def screenshot_like_output_buffer(buf: io.BytesIO) -> io.BytesIO:
    """將 matplotlib 原始高解析 PNG 做一次「截圖式」二次輸出。

    目的：
    1. 保留原本圖表排版與繪圖邏輯。
    2. 模擬使用者把大圖縮放到螢幕後再截圖的效果。
    3. 透過等比例縮小像素與重新壓縮，降低 Discord 圖片檔案大小。

    預設仍輸出 PNG，避免長圖大量文字在 JPEG 下出現明顯壓縮雜訊。
    但會把輸出寬度限制在 WARRANT_SCREENSHOT_OUTPUT_MAX_WIDTH，讓效果更接近螢幕截圖。
    可用環境變數調整：
    - WARRANT_SCREENSHOT_OUTPUT_ENABLE=0：關閉二次輸出。
    - WARRANT_SCREENSHOT_OUTPUT_SCALE=0.6：縮放倍率上限。
    - WARRANT_SCREENSHOT_OUTPUT_MAX_WIDTH=2400：輸出最大寬度，0 代表不限制。
    - WARRANT_SCREENSHOT_OUTPUT_FORMAT=PNG/JPEG：輸出格式。
    - WARRANT_SCREENSHOT_OUTPUT_JPEG_QUALITY=88：JPEG 品質。
    - WARRANT_SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE=1：PNG 轉 256 色調色盤以縮小檔案。
    """
    if not SCREENSHOT_OUTPUT_ENABLE:
        buf.seek(0)
        return buf

    if Image is None:
        print("⚠️ Pillow 未安裝，略過截圖式二次輸出")
        buf.seek(0)
        return buf

    try:
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
        old_w, old_h = img.size

        scale = float(SCREENSHOT_OUTPUT_SCALE)
        if scale <= 0:
            scale = 1.0

        max_width = int(SCREENSHOT_OUTPUT_MAX_WIDTH or 0)
        if max_width > 0 and old_w * scale > max_width:
            # 真正模擬截圖：不要只固定縮 60%，而是限制成比較像螢幕寬度的圖片。
            scale = max_width / max(old_w, 1)

        if scale != 1.0:
            new_w = max(1, int(old_w * scale))
            new_h = max(1, int(old_h * scale))
            resample_filter = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((new_w, new_h), resample_filter)
        else:
            new_w, new_h = old_w, old_h

        out = io.BytesIO()
        output_format = SCREENSHOT_OUTPUT_FORMAT if SCREENSHOT_OUTPUT_FORMAT in ("PNG", "JPEG", "JPG") else "PNG"
        palette_used = False

        if output_format in ("JPEG", "JPG"):
            img.save(
                out,
                format="JPEG",
                quality=max(1, min(100, int(SCREENSHOT_OUTPUT_JPEG_QUALITY))),
                optimize=True,
                progressive=True,
            )
        else:
            save_img = img
            if SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE:
                try:
                    palette_mode = getattr(getattr(Image, "Palette", Image), "ADAPTIVE", Image.ADAPTIVE)
                    save_img = img.convert("P", palette=palette_mode, colors=256)
                    palette_used = True
                except Exception as e:
                    print(f"⚠️ PNG 調色盤縮檔失敗，改用 RGB PNG：{e}")
                    save_img = img
            save_img.save(out, format="PNG", optimize=True, compress_level=9)

        out.seek(0)
        print(
            f"🖼️ 截圖式二次輸出：{old_w}x{old_h} → {new_w}x{new_h}｜"
            f"scale={scale:g}｜max_width={max_width}｜format={output_format}｜"
            f"palette={1 if palette_used else 0}｜size={out.getbuffer().nbytes / 1024 / 1024:.2f} MB"
        )
        return out
    except Exception as e:
        print(f"⚠️ 截圖式二次輸出失敗，改用原始圖片：{e}")
        buf.seek(0)
        return buf


def _local_warrant_cache_path(stock_code: str, start_date=None, end_date=None) -> str:
    start_s = _cache_date_part(start_date) if start_date is not None else "start"
    end_s = _cache_date_part(end_date) if end_date is not None else "end"
    filename = f"warrant_events_{_safe_cache_part(stock_code)}_{start_s}_{end_s}.json"
    return os.path.join(LOCAL_WARRANT_CACHE_DIR, filename)


def _normalize_warrant_events_for_cache(events_df: pd.DataFrame) -> pd.DataFrame:
    if events_df is None or events_df.empty:
        return pd.DataFrame()
    out = events_df.copy().fillna("")
    if "Date" in out.columns:
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.normalize()
        out = out.dropna(subset=["Date"])
        out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    for c in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    for c in ["warrant_code", "underlying_code", "broker_code", "branch", "warrant_name", "underlying_name", "side"]:
        if c in out.columns:
            out[c] = out[c].astype(str).str.strip()
    if "branch" in out.columns:
        out["branch"] = out["branch"].map(normalize_branch_name)
    return out


def load_local_warrant_events_snapshot(stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    if not LOCAL_WARRANT_CACHE_ENABLE or LOCAL_WARRANT_CACHE_FORCE_REFRESH:
        return pd.DataFrame()
    path = _local_warrant_cache_path(stock_code, start_date=start_date, end_date=end_date)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        records = payload.get("records", []) if isinstance(payload, dict) else []
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records).fillna("")
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
            df = df.dropna(subset=["Date"])
        for c in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        if "side" not in df.columns and "net_amount" in df.columns:
            df["side"] = np.where(df["net_amount"] >= 0, "買超", "賣超")
        print(f"📦 本機權證快照命中：{path}｜{len(df):,} 筆")
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"⚠️ 本機權證快照讀取失敗，改走原本資料流程：{path}｜{e}")
        return pd.DataFrame()


def save_local_warrant_events_snapshot(stock_code: str, events_df: pd.DataFrame, start_date=None, end_date=None):
    if not LOCAL_WARRANT_CACHE_ENABLE or events_df is None or events_df.empty:
        return
    path = _local_warrant_cache_path(stock_code, start_date=start_date, end_date=end_date)
    try:
        _ensure_dir(os.path.dirname(path))
        out = _normalize_warrant_events_for_cache(events_df)
        payload = {
            "stock_code": str(stock_code),
            "start_date": _cache_date_part(start_date),
            "end_date": _cache_date_part(end_date),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows": int(len(out)),
            "records": out.to_dict(orient="records"),
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        print(f"💾 已寫入本機權證快照：{path}｜{len(out):,} 筆")
    except Exception as e:
        print(f"⚠️ 本機權證快照寫入失敗：{path}｜{e}")


def reset_api_fetch_stats(scope: str):
    with _FETCH_STATS_LOCK:
        _FETCH_STATS[scope] = {
            "total": 0,
            "success": 0,
            "empty": 0,
            "failed": 0,
            "retry": 0,
            "errors": [],
        }


def _record_api_fetch(scope: str, status: str, error: str = "", retry_count: int = 0):
    with _FETCH_STATS_LOCK:
        st = _FETCH_STATS.setdefault(scope, {
            "total": 0,
            "success": 0,
            "empty": 0,
            "failed": 0,
            "retry": 0,
            "errors": [],
        })
        st["total"] += 1
        if status not in ("success", "empty", "failed"):
            status = "failed"
        st[status] += 1
        st["retry"] += int(retry_count or 0)
        if error:
            errors = st.setdefault("errors", [])
            if len(errors) < 8:
                errors.append(str(error)[:220])


def get_api_fetch_stats(scope: str) -> dict:
    with _FETCH_STATS_LOCK:
        return dict(_FETCH_STATS.get(scope, {}))


def print_api_fetch_stats(scope: str, label: str):
    st = get_api_fetch_stats(scope)
    total = int(st.get("total", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    success = int(st.get("success", 0) or 0)
    empty = int(st.get("empty", 0) or 0)
    retry = int(st.get("retry", 0) or 0)
    fail_ratio = failed / total if total else 0.0
    print(f"📊 {label} 完整度：total={total:,}｜success={success:,}｜empty={empty:,}｜failed={failed:,}｜retry={retry:,}｜fail_ratio={fail_ratio:.1%}")
    for err in st.get("errors", [])[:5]:
        print(f"   ⚠️ {label} 錯誤樣本：{err}")


def abort_if_api_failure_too_high(scope: str, label: str):
    st = get_api_fetch_stats(scope)
    total = int(st.get("total", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    empty = int(st.get("empty", 0) or 0)
    if total < API_FAILURE_ABORT_MIN_REQUESTS:
        return

    if API_REQUIRE_FULL_SUCCESS:
        bad_count = failed + (empty if API_EMPTY_AS_FAILURE else 0)
        if bad_count <= 0:
            return

        bad_ratio = bad_count / total if total else 0.0
        empty_msg = f"，empty={empty:,}" if API_EMPTY_AS_FAILURE else f"，empty={empty:,}（不列入失敗）"

        if API_ALLOW_TINY_FAILURE and bad_count <= API_FAILURE_ABORT_ABS_COUNT and bad_ratio <= API_FAILURE_ABORT_RATIO:
            print(
                f"⚠️ {label} 有極少數請求失敗但低於容許門檻，仍繼續輸出："
                f"total={total:,}，failed={failed:,}{empty_msg}，"
                f"bad_ratio={bad_ratio:.2%}，門檻={API_FAILURE_ABORT_ABS_COUNT}筆 / {API_FAILURE_ABORT_RATIO:.2%}"
            )
            return

        raise RuntimeError(
            f"{label} 完整度未達輸出門檻：total={total:,}，failed={failed:,}{empty_msg}，"
            f"bad_ratio={bad_ratio:.2%}，容許門檻={API_FAILURE_ABORT_ABS_COUNT}筆 / {API_FAILURE_ABORT_RATIO:.2%}。"
            f"本次資料已中止輸出，避免產生錯誤資金流圖。"
        )

    fail_ratio = failed / total if total else 0.0
    if fail_ratio > API_FAILURE_ABORT_RATIO:
        raise RuntimeError(
            f"{label} 失敗比例過高：{failed:,}/{total:,} = {fail_ratio:.2%}，"
            f"超過門檻 {API_FAILURE_ABORT_RATIO:.2%}。本次資料可能嚴重不完整，已中止輸出，避免產生錯誤資金流圖。"
        )


class ApiFetchRetryError(RuntimeError):
    """保留 MoneyDJ API 最終失敗前實際重試次數，避免 log 顯示 retry=0。"""
    def __init__(self, message: str, retry_count: int = 0):
        super().__init__(message)
        self.retry_count = int(retry_count or 0)


def _moneydj_get_json_with_retry(url: str, scope: str):
    last_error = None
    retry_count = 0
    max_times = max(1, int(API_RETRY_TIMES))
    for attempt in range(1, max_times + 1):
        try:
            r = get_thread_session().get(url, headers=HDR, timeout=(5, 15))
            r.raise_for_status()
            text = r.content.decode("utf-8", errors="replace")
            return json.loads(text), retry_count
        except Exception as e:
            last_error = e
            if attempt < max_times:
                retry_count += 1
                wait_sec = API_RETRY_BASE_WAIT * attempt
                time.sleep(wait_sec)
                continue
    raise ApiFetchRetryError(str(last_error), retry_count=retry_count)


def _llm_cache_path(prompt: str) -> str:
    digest = hashlib.sha256((GEMINI_MODEL + "\n" + str(prompt or "")).encode("utf-8")).hexdigest()
    return os.path.join(LLM_CACHE_DIR, f"gemini_{digest}.txt")


def load_llm_cache(prompt: str) -> str:
    if not LLM_CACHE_ENABLE:
        return ""
    path = _llm_cache_path(prompt)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if text.strip():
            print(f"📦 Gemini 快取命中：{path}")
            return text
    except Exception as e:
        print(f"⚠️ Gemini 快取讀取失敗：{e}")
    return ""


def save_llm_cache(prompt: str, output_text: str):
    if not LLM_CACHE_ENABLE or not output_text:
        return
    path = _llm_cache_path(prompt)
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(output_text))
        print(f"💾 Gemini 結果已快取：{path}")
    except Exception as e:
        print(f"⚠️ Gemini 快取寫入失敗：{e}")


GSHEET_LLM_CACHE_HEADERS = [
    "快取鍵", "日期", "任務", "標的股", "標的名稱",
    "模型", "PromptHash", "Gemini輸出", "更新時間",
]


def _taipei_today_str() -> str:
    """GitHub runner 預設常是 UTC；這裡固定用台北日期判斷「當天」。"""
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d")


def _compact_date_key(date_str: str) -> str:
    return re.sub(r"[^0-9]", "", str(date_str or ""))


def _llm_prompt_hash(prompt: str) -> str:
    return hashlib.sha256((GEMINI_MODEL + "\n" + str(prompt or "")).encode("utf-8")).hexdigest()


def _gsheet_llm_cache_key(task: str, stock_code: str, cache_date: str | None = None) -> str:
    date_key = _compact_date_key(cache_date or _taipei_today_str())
    task_key = re.sub(r"[^A-Za-z0-9_一-鿿-]", "_", str(task or "gemini")).strip("_") or "gemini"
    stock_key = _clean_code(stock_code) or "UNKNOWN"
    model_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(GEMINI_MODEL or "gemini"))
    return f"{date_key}_{stock_key}_{task_key}_{model_key}"


def load_gsheet_llm_cache(task: str, stock_code: str, stock_name: str = "", prompt: str = "") -> str:
    """讀取 Google Sheet Gemini 每日快取。

    設計原則：同股票、同任務、同模型、同一個台北日期，只要跑過一次，
    當天再跑就直接使用該輸出，不再呼叫 Gemini。PromptHash 只保留作檢查紀錄，
    不拿來阻擋當日快取命中。
    """
    if not GSHEET_LLM_CACHE_ENABLE or not GSHEET_FALLBACK_ENABLE or LLM_CACHE_FORCE_REFRESH:
        return ""
    if not task or not stock_code:
        return ""
    key = _gsheet_llm_cache_key(task, stock_code)
    try:
        df = read_gsheet_worksheet(GSHEET_LLM_CACHE_SHEET)
        if df is None or df.empty or "快取鍵" not in df.columns or "Gemini輸出" not in df.columns:
            return ""
        matched = df[df["快取鍵"].astype(str) == key].copy()
        if matched.empty:
            return ""
        row = matched.tail(1).iloc[0]
        output_text = str(row.get("Gemini輸出", "") or "").strip()
        if output_text:
            prompt_hash = _llm_prompt_hash(prompt) if prompt else ""
            old_hash = str(row.get("PromptHash", "") or "").strip()
            if prompt_hash and old_hash and prompt_hash != old_hash:
                print(f"📦 Google Sheet Gemini 當日快取命中：{key}｜PromptHash 不同，但依當日快取規則直接重用")
            else:
                print(f"📦 Google Sheet Gemini 當日快取命中：{key}")
            return output_text
    except Exception as e:
        print(f"⚠️ Google Sheet Gemini 快取讀取失敗：{key}｜{e}")
    return ""


def save_gsheet_llm_cache(task: str, stock_code: str, stock_name: str, prompt: str, output_text: str):
    """將 Gemini 原始輸出寫回 Google Sheet，供同日重跑直接重用。"""
    if not GSHEET_LLM_CACHE_ENABLE or not GSHEET_FALLBACK_ENABLE or not output_text:
        return
    if not task or not stock_code:
        return
    sh = _open_gsheet()
    if sh is None:
        print("⚠️ Google Sheet 無法開啟，略過 Gemini 快取寫回")
        return

    cache_date = _taipei_today_str()
    key = _gsheet_llm_cache_key(task, stock_code, cache_date=cache_date)
    updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    try:
        ws = _get_or_create_worksheet(sh, GSHEET_LLM_CACHE_SHEET, rows=300, cols=len(GSHEET_LLM_CACHE_HEADERS))
        old_df = _worksheet_to_df(ws)
        if old_df is not None and not old_df.empty and "快取鍵" in old_df.columns:
            old_df = old_df[old_df["快取鍵"].astype(str) != key].copy()
        else:
            old_df = pd.DataFrame(columns=GSHEET_LLM_CACHE_HEADERS)

        new_df = pd.DataFrame([{
            "快取鍵": key,
            "日期": cache_date,
            "任務": str(task or ""),
            "標的股": _clean_code(stock_code),
            "標的名稱": str(stock_name or ""),
            "模型": GEMINI_MODEL,
            "PromptHash": _llm_prompt_hash(prompt),
            "Gemini輸出": str(output_text or ""),
            "更新時間": updated_at,
        }])
        all_df = pd.concat([old_df, new_df], ignore_index=True, sort=False).fillna("")
        _update_worksheet_from_df(ws, all_df, GSHEET_LLM_CACHE_HEADERS)
        print(f"💾 Gemini 結果已寫入 Google Sheet 快取：{key}")
    except Exception as e:
        print(f"⚠️ Google Sheet Gemini 快取寫入失敗：{key}｜{e}")


# ============================================================
# 股價 / 指標
# ============================================================

def get_tw_stock_name(stock_code: str) -> str:
    stock_code = _normalize_stock_name_code_key(stock_code)
    if not stock_code:
        return "未知公司"

    # 1) 優先讀取 Google Sheet「快取_股票名稱」對照表。
    #    這張表建議欄位為：代號｜名稱。
    try:
        name_map = read_gsheet_stock_name_map()
        cached_name = str(name_map.get(stock_code, "") or "").strip()
        if cached_name and cached_name != "未知公司":
            print(f"✅ 股票名稱快取命中：{stock_code} {cached_name}｜來源：{GSHEET_STOCK_NAME_SHEET}")
            return cached_name
    except Exception as e:
        print(f"⚠️ Google Sheet 股票名稱快取讀取失敗：{stock_code}｜{e}")

    # 2) 快取沒有時，改查上市 / 上櫃公司基本資料。
    basic_sources = [
        {
            "label": "TWSE上市公司基本資料",
            "market": "上市",
            "url": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            "code_keys": ["公司代號", "股票代號", "有價證券代號", "代號"],
            "name_keys": ["公司簡稱", "公司名稱", "有價證券名稱", "名稱"],
        },
        {
            "label": "TPEx上櫃公司基本資料",
            "market": "上櫃",
            "url": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
            "code_keys": ["公司代號", "股票代號", "有價證券代號", "代號"],
            "name_keys": ["公司簡稱", "公司名稱", "有價證券名稱", "名稱"],
        },
    ]

    for src in basic_sources:
        try:
            resp = requests.get(
                src["url"],
                headers={
                    "User-Agent": HDR["User-Agent"],
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                continue

            for row in data:
                if not isinstance(row, dict):
                    continue
                row_code = _pick_row_value(row, src["code_keys"])
                row_name = _pick_row_value(row, src["name_keys"])
                row_code = _normalize_stock_name_code_key(row_code)
                row_name = str(row_name or "").strip()
                if row_code == stock_code and row_name and row_name != "未知公司":
                    print(f"✅ 股票名稱查詢成功：{stock_code} {row_name}｜來源：{src['label']}")
                    save_gsheet_stock_name_cache(stock_code, row_name, market=src["market"], source=src["label"])
                    return row_name
        except Exception as e:
            print(f"⚠️ {src['label']} 查詢失敗：{stock_code}｜{e}")

    # 3) 公司基本資料查不到時，保留原本每日行情備援。ETF / ETN 常會靠這段或 Google Sheet 對照表取得名稱。
    # TWSE
    try:
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
        resp = requests.get(url, headers={"User-Agent": HDR["User-Agent"]}, timeout=8)
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if len(item) >= 2 and _normalize_stock_name_code_key(item[0]) == stock_code:
                name = str(item[1]).strip()
                if name and name != "未知公司":
                    print(f"✅ 股票名稱查詢成功：{stock_code} {name}｜來源：TWSE每日行情")
                    save_gsheet_stock_name_cache(stock_code, name, market="上市", source="TWSE每日行情")
                    return name
    except Exception as e:
        print(f"⚠️ TWSE 每日行情股票名稱查詢失敗：{stock_code}｜{e}")

    # TPEx
    try:
        url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json"
        resp = requests.get(url, headers={"User-Agent": HDR["User-Agent"]}, timeout=8)
        resp.raise_for_status()
        tables = resp.json().get("tables", [])
        if tables:
            for item in tables[0].get("data", []):
                if len(item) >= 2 and _normalize_stock_name_code_key(item[0]) == stock_code:
                    name = str(item[1]).strip()
                    if name and name != "未知公司":
                        print(f"✅ 股票名稱查詢成功：{stock_code} {name}｜來源：TPEx每日行情")
                        save_gsheet_stock_name_cache(stock_code, name, market="上櫃", source="TPEx每日行情")
                        return name
    except Exception as e:
        print(f"⚠️ TPEx 每日行情股票名稱查詢失敗：{stock_code}｜{e}")

    print(f"⚠️ 股票名稱查詢失敗：{stock_code}，請確認 Google Sheet「{GSHEET_STOCK_NAME_SHEET}」是否有此代號，或官方資料源是否可連線")
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
    if ENABLE_SUPERTREND:
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
    # 舊版相容函式：只用在單一欄位備援。三大法人主流程請使用 _convert_inst_amounts_to_lots，
    # 避免外資 / 投信被轉成張，但數值較小的自營商仍停留在股。
    if s.abs().median(skipna=True) > 50000:
        return s / 1000.0
    return s


def _convert_inst_amounts_to_lots(amount_df: pd.DataFrame, source_label: str = "三大法人") -> pd.DataFrame:
    """將外資 / 投信 / 自營商統一轉成張。

    FinMind TaiwanStockInstitutionalInvestorsBuySell 的買賣超資料以「股」為單位，
    因此只要來源標籤是 FinMind，就固定 /1000 轉成「張」，不再用買賣超淨額中位數猜單位。

    X_function 備援來源的單位不一定已知，仍保留原本「三類法人最大量級」判斷，
    避免備援資料若已經是張時被再次除以 1000。
    """
    if amount_df is None or amount_df.empty:
        return pd.DataFrame()

    out = amount_df.copy()
    cols = [c for c in ["foreign", "invest", "dealer"] if c in out.columns]
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).astype(float)

    source_text = str(source_label or "").strip()
    if source_text.lower().startswith("finmind"):
        ref_label = "FinMind固定股轉張"
        unit_div = 1000.0
    else:
        medians = {}
        for c in cols:
            non_zero = out[c].abs().replace(0, np.nan)
            medians[c] = non_zero.median(skipna=True)

        valid_medians = [float(v) for v in medians.values() if pd.notna(v)]
        ref_median = max(valid_medians) if valid_medians else 0.0
        ref_label = f"{ref_median:,.0f}"
        unit_div = 1000.0 if ref_median > 50000 else 1.0

    for c in cols:
        out[c] = out[c] / unit_div

    print(f"🔎 {source_label} 單位換算：ref_basis={ref_label}｜unit_div={unit_div:.0f}")
    return out

def _classify_finmind_inst_name(name) -> str:
    """將 FinMind / X_function 長表法人名稱分類。

    自營商特別採保守口徑：
    - Dealer_self / 自營商自行買賣：視為圖表要顯示的自營商。
    - Dealer_Hedging / 自營商避險：另外標記，不能直接併入自營商。
    - Dealer / 自營商 這種泛稱：先視為可能的自營商合計或不明口徑，
      必須在 _standardize_institutional_long_df 內確認是否能扣掉避險，不能在這裡直接當成自行買賣。
    """
    s = str(name or "").strip().lower()
    if not s:
        return ""
    compact = _normalize_inst_column_key(s)

    if "investment_trust" in s or "investmenttrust" in compact or "投信" in compact:
        return "invest"
    if "foreign" in s or "foreigninvestor" in compact or "外資" in compact or "陸資" in compact:
        return "foreign"

    if "hedging" in s or "hedge" in s or "hedg" in compact or "避險" in compact:
        if "dealer" in s or "dealer" in compact or "自營" in compact:
            return "dealer_hedge"
        return ""
    if "dealer_self" in s or "dealerself" in compact or "self_dealer" in s or "selfdealer" in compact:
        return "dealer_self"
    if ("dealer" in s or "dealer" in compact or "自營" in compact) and ("自行" in compact or "self" in compact):
        return "dealer_self"
    if "dealer" in s or "dealer" in compact or "自營" in compact:
        return "dealer_total"
    return ""

def _pick_existing_col(df: pd.DataFrame, candidates: List[str]) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    return ""


def _normalize_inst_column_key(col) -> str:
    s = str(col or "").strip().lower()
    s = html.unescape(s)
    s = s.replace("臺", "台")
    s = re.sub(r"[\s　_\-－—–/\\|｜:：,，.。()（）\[\]【】{}｛｝]+", "", s)
    return s


def _pick_existing_col_loose(df: pd.DataFrame, candidates: List[str]) -> str:
    """比 _pick_existing_col 更寬鬆的欄位尋找，處理括號、空白、全形符號差異。"""
    exact = _pick_existing_col(df, candidates)
    if exact:
        return exact
    norm_map = {_normalize_inst_column_key(c): c for c in df.columns}
    for cand in candidates:
        key = _normalize_inst_column_key(cand)
        if key in norm_map:
            return norm_map[key]
    return ""


def _find_institutional_col_by_keywords(
    df: pd.DataFrame,
    include_groups: List[List[str]],
    exclude_keywords: List[str] | None = None,
) -> str:
    """依欄位名稱關鍵字尋找法人欄位；include_groups 任一組全命中即符合。"""
    exclude_keys = [_normalize_inst_column_key(k) for k in (exclude_keywords or []) if str(k or "").strip()]
    include_keys = [
        [_normalize_inst_column_key(k) for k in group if str(k or "").strip()]
        for group in include_groups
    ]
    for col in df.columns:
        key = _normalize_inst_column_key(col)
        if not key:
            continue
        if any(ex and ex in key for ex in exclude_keys):
            continue
        for group in include_keys:
            if group and all(k in key for k in group):
                return col
    return ""


def _find_dealer_total_col(df: pd.DataFrame) -> str:
    """只找自營商合計欄位；排除自行買賣與避險欄位。"""
    exact_candidates = [
        "自營商", "自營商買賣超", "自營商買賣超股數", "自營商買賣超張數",
        "dealer", "Dealer", "Dealer買賣超", "Dealer_BuySell",
    ]
    col = _pick_existing_col_loose(df, exact_candidates)
    if col:
        key = _normalize_inst_column_key(col)
        if not any(k in key for k in ["自行", "self", "避險", "hedg"]):
            return col
    return _find_institutional_col_by_keywords(
        df,
        include_groups=[["自營商", "買賣超"], ["dealer"]],
        exclude_keywords=["自行", "self", "避險", "hedg"],
    )


def _standardize_institutional_long_df(raw: pd.DataFrame, stock_code: str, days: int, source_label: str) -> pd.DataFrame:
    """處理 FinMind / X_function 原始長表格式。

    自營商採最保守口徑：
    1. 有 Dealer_self / 自營商自行買賣，就直接採用。
    2. 若只有 Dealer / 自營商泛稱，但同時有 Dealer_Hedging / 自營商避險，則用「泛稱欄位 - 避險」還原自行買賣。
    3. 若只有 Dealer / 自營商泛稱，且無法判斷是否含避險，預設不採用該自營商數值，避免再次出現把避險誤算進圖表的問題。
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    date_col = _pick_existing_col_loose(raw, ["date", "Date", "日期"])
    name_col = _pick_existing_col_loose(raw, [
        "name", "institutional_investor", "institutional_investors", "investor",
        "type", "category", "法人", "身份別", "投資人類別",
    ])
    net_col = _pick_existing_col_loose(raw, [
        "net", "buy_sell", "buy_sell_amount", "buy_sell_volume",
        "買賣超", "買賣超股數", "買賣超張數",
    ])
    buy_col = _pick_existing_col_loose(raw, [
        "buy", "buy_amount", "buy_volume", "買進", "買進股數", "買進張數",
    ])
    sell_col = _pick_existing_col_loose(raw, [
        "sell", "sell_amount", "sell_volume", "賣出", "賣出股數", "賣出張數",
    ])

    if not date_col or not name_col:
        return pd.DataFrame()

    tmp = raw.copy()
    tmp["Date"] = tmp[date_col].map(parse_date)
    tmp["Date"] = pd.to_datetime(tmp["Date"], errors="coerce")
    if tmp["Date"].isna().any():
        fallback_date = pd.to_datetime(tmp.loc[tmp["Date"].isna(), date_col], errors="coerce")
        tmp.loc[tmp["Date"].isna(), "Date"] = fallback_date
    tmp = tmp.dropna(subset=["Date"])
    tmp["inst_group"] = tmp[name_col].map(_classify_finmind_inst_name)
    tmp = tmp[tmp["inst_group"].isin(["foreign", "invest", "dealer_self", "dealer_total", "dealer_hedge"])]
    if tmp.empty:
        print(f"⚠️ {source_label} 三大法人分類不到外資/投信/自營商：{stock_code}")
        return pd.DataFrame()

    if net_col:
        tmp["net_value"] = pd.to_numeric(tmp[net_col], errors="coerce").fillna(0.0)
    elif buy_col and sell_col:
        tmp["net_value"] = (
            pd.to_numeric(tmp[buy_col], errors="coerce").fillna(0.0)
            - pd.to_numeric(tmp[sell_col], errors="coerce").fillna(0.0)
        )
    else:
        print(f"⚠️ {source_label} 三大法人找不到買賣超或買進/賣出欄位：{raw.columns.tolist()}")
        return pd.DataFrame()

    grouped = tmp.groupby(["Date", "inst_group"], as_index=False)["net_value"].sum()
    pivot = grouped.pivot(index="Date", columns="inst_group", values="net_value").fillna(0.0)

    foreign_raw = pivot["foreign"] if "foreign" in pivot.columns else pd.Series(0.0, index=pivot.index)
    invest_raw = pivot["invest"] if "invest" in pivot.columns else pd.Series(0.0, index=pivot.index)

    if "dealer_self" in pivot.columns:
        dealer_raw = pivot["dealer_self"]
        dealer_note = "自營商採用 Dealer_self / 自營商自行買賣"
    elif "dealer_total" in pivot.columns and "dealer_hedge" in pivot.columns:
        dealer_raw = pivot["dealer_total"] - pivot["dealer_hedge"]
        dealer_note = "自營商採用 Dealer / 自營商泛稱扣除 Dealer_Hedging / 自營商避險"
    elif "dealer_total" in pivot.columns:
        allow_aggregate = os.getenv("WARRANT_ALLOW_AGGREGATE_DEALER_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")
        if allow_aggregate:
            dealer_raw = pivot["dealer_total"]
            dealer_note = "⚠️ 自營商使用 Dealer / 自營商泛稱欄位（未確認是否含避險）"
        else:
            dealer_raw = pd.Series(0.0, index=pivot.index)
            dealer_note = "⚠️ 只有 Dealer / 自營商泛稱，無法確認是否含避險；自營商以 0 顯示，避免誤算避險"
            print(f"⚠️ {source_label} {stock_code} {dealer_note}")
    else:
        dealer_raw = pd.Series(0.0, index=pivot.index)
        dealer_note = "未取得自營商自行買賣資料，自營商以 0 顯示"
        print(f"⚠️ {source_label} {stock_code} {dealer_note}")

    out = pd.DataFrame(index=pivot.index)
    out["Date"] = out.index
    out["foreign"] = pd.to_numeric(foreign_raw, errors="coerce").fillna(0.0).astype(float).values
    out["invest"] = pd.to_numeric(invest_raw, errors="coerce").fillna(0.0).astype(float).values
    out["dealer"] = pd.to_numeric(dealer_raw, errors="coerce").fillna(0.0).astype(float).values
    out = _convert_inst_amounts_to_lots(out, source_label=f"{source_label} 三大法人")
    out["total"] = out["foreign"] + out["invest"] + out["dealer"]
    out = out.reset_index(drop=True).sort_values("Date").tail(days).reset_index(drop=True)
    print(f"✅ {source_label} 三大法人資料：{stock_code}，{len(out):,} 筆｜{dealer_note}")
    return out[["Date", "foreign", "invest", "dealer", "total"]]

def _standardize_institutional_wide_df(raw: pd.DataFrame, stock_code: str, days: int, source_label: str) -> pd.DataFrame:
    """處理 X_function 可能回傳的寬表格式；自營商只採自行買賣，不採避險。"""
    if raw is None or raw.empty:
        return pd.DataFrame()

    date_col = _pick_existing_col_loose(raw, ["date", "Date", "日期"])
    foreign_col = _pick_existing_col_loose(raw, [
        "外資", "外資買賣超", "外資買賣超股數", "外資及陸資", "外資及陸資買賣超股數",
        "foreign", "Foreign", "Foreign_Investor",
    ]) or _find_institutional_col_by_keywords(
        raw,
        include_groups=[["外資"], ["foreign"]],
        exclude_keywords=["自營", "dealer", "投信", "trust", "避險", "hedg"],
    )
    invest_col = _pick_existing_col_loose(raw, [
        "投信", "投信買賣超", "投信買賣超股數", "investment_trust", "Investment_Trust",
    ]) or _find_institutional_col_by_keywords(
        raw,
        include_groups=[["投信"], ["investment", "trust"]],
        exclude_keywords=["自營", "dealer", "外資", "foreign", "避險", "hedg"],
    )
    dealer_self_col = _pick_existing_col_loose(raw, [
        "自營商自行買賣", "自營商自行買賣買賣超", "自營商自行買賣買賣超股數",
        "自營商買賣超股數自行買賣", "自營商(自行買賣)", "自營商-自行買賣",
        "Dealer_self", "dealer_self", "DealerSelf", "self_dealer",
    ]) or _find_institutional_col_by_keywords(
        raw,
        include_groups=[["自營", "自行"], ["dealer", "self"]],
        exclude_keywords=["避險", "hedg"],
    )
    dealer_hedge_col = _pick_existing_col_loose(raw, [
        "自營商避險", "自營商避險買賣超", "自營商避險買賣超股數",
        "自營商買賣超股數避險", "自營商(避險)", "自營商-避險",
        "Dealer_Hedging", "dealer_hedging", "DealerHedging",
    ]) or _find_institutional_col_by_keywords(
        raw,
        include_groups=[["自營", "避險"], ["dealer", "hedg"]],
    )
    dealer_total_col = _find_dealer_total_col(raw)

    if not date_col or not foreign_col or not invest_col:
        print(f"⚠️ {source_label} 法人資料欄位不符：{raw.columns.tolist()}")
        return pd.DataFrame()

    if dealer_self_col:
        dealer_raw = pd.to_numeric(raw[dealer_self_col], errors="coerce").fillna(0.0)
        dealer_note = f"自營商採用自行買賣欄位：{dealer_self_col}"
    elif dealer_total_col and dealer_hedge_col:
        dealer_raw = (
            pd.to_numeric(raw[dealer_total_col], errors="coerce").fillna(0.0)
            - pd.to_numeric(raw[dealer_hedge_col], errors="coerce").fillna(0.0)
        )
        dealer_note = f"自營商合計扣除避險：{dealer_total_col} - {dealer_hedge_col}"
    elif dealer_total_col:
        allow_aggregate = os.getenv("WARRANT_ALLOW_AGGREGATE_DEALER_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")
        if allow_aggregate:
            dealer_raw = pd.to_numeric(raw[dealer_total_col], errors="coerce").fillna(0.0)
            dealer_note = f"⚠️ 自營商使用合計欄位：{dealer_total_col}（未排除避險）"
        else:
            print(
                f"⚠️ {source_label} 只有自營商合計欄位「{dealer_total_col}」，無法確認是否含避險；"
                "本次略過 X_function 三大法人備援，避免把 Dealer_Hedging 算進圖表。"
            )
            return pd.DataFrame()
    else:
        print(f"⚠️ {source_label} 找不到自營商自行買賣欄位，略過三大法人備援：{raw.columns.tolist()}")
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Date"] = raw[date_col].map(parse_date)
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    if out["Date"].isna().any():
        fallback_date = pd.to_datetime(raw.loc[out["Date"].isna(), date_col], errors="coerce")
        out.loc[out["Date"].isna(), "Date"] = fallback_date
    out = out.dropna(subset=["Date"])
    if out.empty:
        print(f"⚠️ {source_label} 日期欄位無法解析：{date_col}")
        return pd.DataFrame()

    raw2 = raw.loc[out.index].copy()
    out["foreign"] = pd.to_numeric(raw2[foreign_col], errors="coerce").fillna(0.0).astype(float).values
    out["invest"] = pd.to_numeric(raw2[invest_col], errors="coerce").fillna(0.0).astype(float).values
    out["dealer"] = pd.to_numeric(dealer_raw.loc[out.index], errors="coerce").fillna(0.0).astype(float).values
    out = _convert_inst_amounts_to_lots(out, source_label=f"{source_label} 三大法人備援")
    out["total"] = out["foreign"] + out["invest"] + out["dealer"]
    out = out.sort_values("Date").tail(days).reset_index(drop=True)
    print(f"✅ {source_label} 三大法人備援資料：{stock_code}，{len(out):,} 筆｜{dealer_note}")
    return out[["Date", "foreign", "invest", "dealer", "total"]]


def fetch_inst_60d_from_finmind_token(stock_code: str, days: int = 80) -> pd.DataFrame:
    """
    直接使用 FinMind API 抓三大法人買賣超。
    若有 FINMIND_API_TOKEN 會帶 token；若沒有 token，仍先嘗試 FinMind 公開 API。
    回傳欄位: Date, foreign, invest, dealer, total，單位統一為張。
    """
    token = os.getenv("FINMIND_API_TOKEN", "").strip()
    if not token:
        print("⚠️ 未設定 FINMIND_API_TOKEN，先嘗試 FinMind 公開 API；若失敗再走 X_function 安全備援")

    try:
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=max(int(days * 2.8), 120))
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": str(stock_code).strip(),
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
        }
        headers = {
            "User-Agent": HDR["User-Agent"],
        }
        if token:
            params["token"] = token
            headers["Authorization"] = f"Bearer {token}"

        resp = requests.get(url, params=params, headers=headers, timeout=(8, 30))
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        if not data:
            msg = payload.get("msg", "") if isinstance(payload, dict) else ""
            print(f"⚠️ FinMind 三大法人資料為空：{stock_code} {msg}")
            return pd.DataFrame()

        raw = pd.DataFrame(data).fillna(0)
        out = _standardize_institutional_long_df(raw, stock_code, days, "FinMind")
        if out is None or out.empty:
            print(f"⚠️ FinMind 三大法人欄位不符：{raw.columns.tolist()}")
            return pd.DataFrame()
        return out
    except Exception as e:
        print(f"⚠️ FinMind 三大法人資料抓取失敗：{e}")
        return pd.DataFrame()


def fetch_inst_60d_from_x(stock_code: str, days: int = 80) -> pd.DataFrame:
    """
    優先使用 FinMind API 抓三大法人資料，並排除 Dealer_Hedging / 自營商避險。
    若 FinMind API 失敗，才使用 X_function 備援；備援也必須能拆出自營商自行買賣，
    否則直接略過，避免把權證或衍生性商品避險部位誤算進自營商。
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

    # 若 X_function 回傳的是 FinMind 長表格式，使用同一套分類邏輯，會排除 Dealer_Hedging。
    maybe_name_col = _pick_existing_col_loose(inst, [
        "name", "institutional_investor", "institutional_investors", "investor",
        "type", "category", "法人", "身份別", "投資人類別",
    ])
    if maybe_name_col:
        out = _standardize_institutional_long_df(inst, stock_code, days, "X_function")
        if out is not None and not out.empty:
            return out

    # 若 X_function 回傳的是外資 / 投信 / 自營商寬表，必須確認自營商是「自行買賣」口徑。
    return _standardize_institutional_wide_df(inst, stock_code, days, "X_function")

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


def _normalize_stock_name_code_key(code) -> str:
    """股票名稱快取用代號正規化。

    Google Sheet 內可能有 0050、006208、00625K 這類代號，不能用 get_all_records
    讓前導 0 消失；讀取時一律用字串比對。
    """
    if code is None or (isinstance(code, float) and pd.isna(code)):
        return ""
    s = str(code).strip().upper().replace("'", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    s = re.sub(r"\s+", "", s)
    if s.isdigit() and len(s) < 4:
        s = s.zfill(4)
    return s


def _pick_row_value(row: dict, keys: List[str]) -> str:
    for k in keys:
        if k in row and str(row.get(k, "") or "").strip():
            return str(row.get(k, "") or "").strip()
    return ""


def _find_header_index(headers: List[str], candidates: List[str]) -> int:
    normalized = [str(h or "").strip() for h in headers]
    for cand in candidates:
        if cand in normalized:
            return normalized.index(cand)
    return -1


def read_gsheet_stock_name_map(force_refresh: bool = False) -> Dict[str, str]:
    """讀取 Google Sheet「快取_股票名稱」工作表。

    預期欄位至少包含：代號、名稱。
    使用 get_all_values() 而不是 get_all_records()，避免 0050 被轉成 50。
    """
    global _STOCK_NAME_MAP_CACHE

    if not GSHEET_FALLBACK_ENABLE:
        return {}

    with _STOCK_NAME_MAP_CACHE_LOCK:
        if _STOCK_NAME_MAP_CACHE is not None and not force_refresh:
            return dict(_STOCK_NAME_MAP_CACHE)

    sh = _open_gsheet()
    if sh is None:
        return {}

    try:
        ws = sh.worksheet(GSHEET_STOCK_NAME_SHEET)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            lookup = {}
        else:
            headers = [str(x or "").strip() for x in values[0]]
            code_idx = _find_header_index(headers, ["代號", "股票代號", "證券代號", "有價證券代號", "公司代號"])
            name_idx = _find_header_index(headers, ["名稱", "股票名稱", "證券名稱", "有價證券名稱", "公司名稱", "公司簡稱"])
            if code_idx < 0 or name_idx < 0:
                print(f"⚠️ {GSHEET_STOCK_NAME_SHEET} 缺少「代號」或「名稱」欄位")
                lookup = {}
            else:
                lookup = {}
                for row in values[1:]:
                    if len(row) <= max(code_idx, name_idx):
                        continue
                    code = _normalize_stock_name_code_key(row[code_idx])
                    name = str(row[name_idx] or "").strip()
                    if code and name and name != "未知公司":
                        lookup[code] = name
        with _STOCK_NAME_MAP_CACHE_LOCK:
            _STOCK_NAME_MAP_CACHE = dict(lookup)
        if lookup:
            print(f"📦 已讀取股票名稱快取：{GSHEET_STOCK_NAME_SHEET}｜{len(lookup):,} 筆")
        return lookup
    except Exception as e:
        print(f"⚠️ Google Sheet 股票名稱對照表讀取失敗：{GSHEET_STOCK_NAME_SHEET}｜{e}")
        return {}


def save_gsheet_stock_name_cache(stock_code: str, stock_name: str, market: str = "", source: str = ""):
    """官方資料查到名稱後，寫回 Google Sheet「快取_股票名稱」。

    若工作表只有「代號、名稱」兩欄，就只寫這兩欄；若使用者之後自行加上
    「市場、來源、更新時間」欄位，程式也會順便補上。
    """
    global _STOCK_NAME_MAP_CACHE

    if not GSHEET_FALLBACK_ENABLE:
        return

    code = _normalize_stock_name_code_key(stock_code)
    name = str(stock_name or "").strip()
    if not code or not name or name == "未知公司":
        return

    sh = _open_gsheet()
    if sh is None:
        return

    try:
        try:
            ws = sh.worksheet(GSHEET_STOCK_NAME_SHEET)
        except Exception:
            ws = sh.add_worksheet(title=GSHEET_STOCK_NAME_SHEET, rows=1000, cols=2)
            ws.update([['代號', '名稱']], value_input_option="RAW")

        values = ws.get_all_values()
        if not values:
            ws.update([['代號', '名稱']], value_input_option="RAW")
            values = ws.get_all_values()

        headers = [str(x or "").strip() for x in values[0]]
        code_idx = _find_header_index(headers, ["代號", "股票代號", "證券代號", "有價證券代號", "公司代號"])
        name_idx = _find_header_index(headers, ["名稱", "股票名稱", "證券名稱", "有價證券名稱", "公司名稱", "公司簡稱"])
        if code_idx < 0 or name_idx < 0:
            print(f"⚠️ {GSHEET_STOCK_NAME_SHEET} 缺少「代號」或「名稱」欄位，略過股票名稱快取寫入")
            return

        market_idx = _find_header_index(headers, ["市場"])
        source_idx = _find_header_index(headers, ["來源"])
        updated_idx = _find_header_index(headers, ["更新時間"])
        updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        target_row_number = None
        for row_i, row in enumerate(values[1:], start=2):
            if len(row) > code_idx and _normalize_stock_name_code_key(row[code_idx]) == code:
                target_row_number = row_i
                break

        if target_row_number is not None:
            current_name = ""
            row_values = values[target_row_number - 1]
            if len(row_values) > name_idx:
                current_name = str(row_values[name_idx] or "").strip()
            if not current_name or current_name == "未知公司":
                ws.update_cell(target_row_number, name_idx + 1, name)
                if market_idx >= 0 and market:
                    ws.update_cell(target_row_number, market_idx + 1, market)
                if source_idx >= 0 and source:
                    ws.update_cell(target_row_number, source_idx + 1, source)
                if updated_idx >= 0:
                    ws.update_cell(target_row_number, updated_idx + 1, updated_at)
                print(f"💾 已更新股票名稱快取：{code} {name}｜{GSHEET_STOCK_NAME_SHEET}")
        else:
            new_row = [""] * len(headers)
            new_row[code_idx] = code
            new_row[name_idx] = name
            if market_idx >= 0:
                new_row[market_idx] = market
            if source_idx >= 0:
                new_row[source_idx] = source
            if updated_idx >= 0:
                new_row[updated_idx] = updated_at
            ws.append_row(new_row, value_input_option="RAW")
            print(f"💾 已新增股票名稱快取：{code} {name}｜{GSHEET_STOCK_NAME_SHEET}")

        with _STOCK_NAME_MAP_CACHE_LOCK:
            if _STOCK_NAME_MAP_CACHE is None:
                _STOCK_NAME_MAP_CACHE = {}
            _STOCK_NAME_MAP_CACHE[code] = name
    except Exception as e:
        print(f"⚠️ Google Sheet 股票名稱快取寫入失敗：{code} {name}｜{e}")


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
    out["branch"] = pd.Series(out["branch"]).map(normalize_branch_name).values
    out["broker_name"] = out["broker_name"].map(normalize_branch_name)
    out["side"] = np.where(out["net_amount"] >= 0, "買超", "賣超")
    return out


def load_cached_warrant_history(stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    raw = read_gsheet_worksheet(GSHEET_WARRANT_HISTORY_SHEET)
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


GSHEET_WARRANT_HISTORY_HEADERS = [
    "日期", "權證代號", "權證名稱", "標的股", "標的名稱",
    "分點", "分點名稱", "券商代號", "買進金額", "賣出金額", "買超金額",
    "買進張數", "賣出張數", "資料來源", "快取起日", "快取迄日", "更新時間",
]

GSHEET_WARRANT_STATUS_HEADERS = [
    "快取鍵", "標的股", "標的名稱", "快取起日", "快取迄日", "完整度狀態",
    "API4總請求", "API4成功", "API4空回應", "API4失敗",
    "API5總請求", "API5成功", "API5空回應", "API5失敗",
    "資料筆數", "更新時間",
]


def _gsheet_cache_date_str(v) -> str:
    dt = parse_date(v)
    if not dt:
        try:
            dt = pd.Timestamp(v).to_pydatetime()
        except Exception:
            dt = None
    return dt.strftime("%Y/%m/%d") if dt else str(v or "").strip()


def _gsheet_cache_key(stock_code: str, start_date=None, end_date=None) -> str:
    return f"{_clean_code(stock_code)}_{_cache_date_part(start_date)}_{_cache_date_part(end_date)}"


def _get_or_create_worksheet(sh, title: str, rows: int = 1000, cols: int = 20):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=max(1, rows), cols=max(1, cols))


def _worksheet_to_df(ws) -> pd.DataFrame:
    try:
        records = ws.get_all_records(empty2zero=False, head=1)
        return pd.DataFrame(records).fillna("") if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _update_worksheet_from_df(ws, df: pd.DataFrame, headers: List[str]):
    cols = list(headers)
    if df is not None and not df.empty:
        for c in df.columns:
            if c not in cols:
                cols.append(c)
        out = df.copy().fillna("")
        for c in cols:
            if c not in out.columns:
                out[c] = ""
        out = out[cols]
        values = [cols] + out.astype(str).values.tolist()
    else:
        values = [cols]

    ws.clear()
    ws.resize(rows=max(len(values), 1), cols=max(len(cols), 1))
    ws.update(values, value_input_option="USER_ENTERED")


def _events_to_gsheet_history_df(events_df: pd.DataFrame, stock_code: str, stock_name: str, start_date=None, end_date=None) -> pd.DataFrame:
    if events_df is None or events_df.empty:
        return pd.DataFrame(columns=GSHEET_WARRANT_HISTORY_HEADERS)
    e = events_df.copy().fillna("")
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    out = pd.DataFrame()
    out["日期"] = e["Date"].dt.strftime("%Y/%m/%d")
    out["權證代號"] = e.get("warrant_code", "").map(normalize_openapi_warrant_code) if "warrant_code" in e.columns else ""
    out["權證名稱"] = e.get("warrant_name", "").astype(str).str.strip() if "warrant_name" in e.columns else ""
    out["標的股"] = e.get("underlying_code", str(stock_code)).astype(str).str.strip() if "underlying_code" in e.columns else str(stock_code)
    out["標的股"] = out["標的股"].replace("", str(stock_code))
    out["標的名稱"] = e.get("underlying_name", str(stock_name)).astype(str).str.strip() if "underlying_name" in e.columns else str(stock_name)
    out["標的名稱"] = out["標的名稱"].replace("", str(stock_name))
    out["分點"] = e.get("branch", "").astype(str).str.strip() if "branch" in e.columns else ""
    out["分點"] = out["分點"].map(normalize_branch_name)
    out["分點名稱"] = out["分點"]
    out["券商代號"] = e.get("broker_code", "").astype(str).str.strip() if "broker_code" in e.columns else ""
    out["買進金額"] = pd.to_numeric(e.get("buy_amount", 0), errors="coerce").fillna(0).astype(float)
    out["賣出金額"] = pd.to_numeric(e.get("sell_amount", 0), errors="coerce").fillna(0).astype(float)
    out["買超金額"] = pd.to_numeric(e.get("net_amount", 0), errors="coerce").fillna(0).astype(float)
    out["買進張數"] = pd.to_numeric(e.get("buy_shares", 0), errors="coerce").fillna(0).astype(float) if "buy_shares" in e.columns else 0
    out["賣出張數"] = pd.to_numeric(e.get("sell_shares", 0), errors="coerce").fillna(0).astype(float) if "sell_shares" in e.columns else 0
    out["資料來源"] = "MoneyDJ_API4_API5_100pct"
    out["快取起日"] = _gsheet_cache_date_str(start_date)
    out["快取迄日"] = _gsheet_cache_date_str(end_date)
    out["更新時間"] = updated_at
    return out[GSHEET_WARRANT_HISTORY_HEADERS]


def _remove_same_stock_range_rows(raw_df: pd.DataFrame, stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    df = raw_df.copy().fillna("")
    if "標的股" not in df.columns or "日期" not in df.columns:
        return df
    start_ts = pd.Timestamp(start_date).normalize() if start_date is not None else pd.Timestamp.min
    end_ts = pd.Timestamp(end_date).normalize() if end_date is not None else pd.Timestamp.max
    date_s = df["日期"].map(parse_date)
    date_s = pd.to_datetime(date_s, errors="coerce").dt.normalize()
    stock_s = df["標的股"].map(_clean_code).astype(str)
    mask = (stock_s == _clean_code(stock_code)) & (date_s >= start_ts) & (date_s <= end_ts)
    return df.loc[~mask].copy()


def _read_gsheet_warrant_status() -> pd.DataFrame:
    if not GSHEET_WARRANT_CACHE_ENABLE or not GSHEET_FALLBACK_ENABLE:
        return pd.DataFrame()
    return read_gsheet_worksheet(GSHEET_WARRANT_STATUS_SHEET)


def load_gsheet_warrant_events_snapshot(stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    if not GSHEET_WARRANT_CACHE_ENABLE or WARRANT_CACHE_FORCE_REFRESH:
        return pd.DataFrame()
    key = _gsheet_cache_key(stock_code, start_date=start_date, end_date=end_date)
    status_df = _read_gsheet_warrant_status()
    if status_df is None or status_df.empty or "快取鍵" not in status_df.columns:
        return pd.DataFrame()

    matched = status_df[status_df["快取鍵"].astype(str) == key].copy()
    if matched.empty:
        return pd.DataFrame()
    matched = matched.tail(1)
    row = matched.iloc[0]
    if str(row.get("完整度狀態", "")).strip().lower() != "complete":
        print(f"⚠️ Google Sheet 快取狀態不是 complete：{key}，改走 live 抓取")
        return pd.DataFrame()

    def _safe_int_from_status(value, default=0):
        try:
            v = pd.to_numeric(value, errors="coerce")
            if pd.isna(v):
                return int(default)
            return int(v)
        except Exception:
            return int(default)

    api4_failed = _safe_int_from_status(row.get("API4失敗", 0))
    api5_failed = _safe_int_from_status(row.get("API5失敗", 0))
    expected_rows = _safe_int_from_status(row.get("資料筆數", 0))
    if api4_failed != 0 or api5_failed != 0 or expected_rows <= 0:
        print(f"⚠️ Google Sheet 快取完整度紀錄不合格：{key}，改走 live 抓取")
        return pd.DataFrame()

    events = load_cached_warrant_history(stock_code, start_date=start_date, end_date=end_date)
    if events.empty:
        print(f"⚠️ Google Sheet 快取狀態存在，但 {GSHEET_WARRANT_HISTORY_SHEET} 找不到資料：{key}，改走 live 抓取")
        return pd.DataFrame()

    events = events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    if len(events) != expected_rows:
        print(f"⚠️ Google Sheet 快取筆數不一致：狀態 {expected_rows:,} 筆，實際 {len(events):,} 筆；改走 live 抓取")
        return pd.DataFrame()

    print(f"☁️ Google Sheet 完整快照命中：{key}｜{len(events):,} 筆")
    return events


def save_gsheet_warrant_events_snapshot(stock_code: str, stock_name: str, events_df: pd.DataFrame, start_date=None, end_date=None):
    if not GSHEET_WARRANT_CACHE_ENABLE or not GSHEET_FALLBACK_ENABLE or events_df is None or events_df.empty:
        return

    api4_stats = get_api_fetch_stats("api4")
    api5_stats = get_api_fetch_stats("api5")
    api4_failed = int(api4_stats.get("failed", 0) or 0)
    api5_failed = int(api5_stats.get("failed", 0) or 0)
    if api4_failed != 0 or api5_failed != 0:
        print(f"⚠️ API 未達 100%，不寫入 Google Sheet 快取：API4 failed={api4_failed}｜API5 failed={api5_failed}")
        return

    sh = _open_gsheet()
    if sh is None:
        print("⚠️ Google Sheet 無法開啟，略過權證快取寫回")
        return

    key = _gsheet_cache_key(stock_code, start_date=start_date, end_date=end_date)
    updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    new_history = _events_to_gsheet_history_df(events_df, stock_code, stock_name, start_date=start_date, end_date=end_date)
    if new_history.empty:
        return

    try:
        history_ws = _get_or_create_worksheet(sh, GSHEET_WARRANT_HISTORY_SHEET, rows=max(len(new_history) + 10, 1000), cols=len(GSHEET_WARRANT_HISTORY_HEADERS))
        old_history = _worksheet_to_df(history_ws)
        kept_history = _remove_same_stock_range_rows(old_history, stock_code, start_date=start_date, end_date=end_date)
        all_history = pd.concat([kept_history, new_history], ignore_index=True, sort=False).fillna("")
        _update_worksheet_from_df(history_ws, all_history, GSHEET_WARRANT_HISTORY_HEADERS)
        print(f"💾 已寫入 Google Sheet {GSHEET_WARRANT_HISTORY_SHEET}：{key}｜{len(new_history):,} 筆")

        status_ws = _get_or_create_worksheet(sh, GSHEET_WARRANT_STATUS_SHEET, rows=200, cols=len(GSHEET_WARRANT_STATUS_HEADERS))
        old_status = _worksheet_to_df(status_ws)
        if old_status is not None and not old_status.empty and "快取鍵" in old_status.columns:
            old_status = old_status[old_status["快取鍵"].astype(str) != key].copy()
        else:
            old_status = pd.DataFrame(columns=GSHEET_WARRANT_STATUS_HEADERS)

        new_status = pd.DataFrame([{
            "快取鍵": key,
            "標的股": _clean_code(stock_code),
            "標的名稱": str(stock_name or ""),
            "快取起日": _gsheet_cache_date_str(start_date),
            "快取迄日": _gsheet_cache_date_str(end_date),
            "完整度狀態": "complete",
            "API4總請求": int(api4_stats.get("total", 0) or 0),
            "API4成功": int(api4_stats.get("success", 0) or 0),
            "API4空回應": int(api4_stats.get("empty", 0) or 0),
            "API4失敗": api4_failed,
            "API5總請求": int(api5_stats.get("total", 0) or 0),
            "API5成功": int(api5_stats.get("success", 0) or 0),
            "API5空回應": int(api5_stats.get("empty", 0) or 0),
            "API5失敗": api5_failed,
            "資料筆數": int(len(new_history)),
            "更新時間": updated_at,
        }])
        all_status = pd.concat([old_status, new_status], ignore_index=True, sort=False).fillna("")
        _update_worksheet_from_df(status_ws, all_status, GSHEET_WARRANT_STATUS_HEADERS)
        print(f"✅ Google Sheet 快取狀態已更新：{key}｜complete")
    except Exception as e:
        print(f"⚠️ Google Sheet 權證快取寫入失敗：{key}｜{e}")


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


def _normalize_warrant_match_text(text: str) -> str:
    """權證名稱 / 股票別名比對用正規化，避免空白、符號、台臺差異造成名稱比對失敗。"""
    s = html.unescape(str(text or "")).strip()
    s = s.replace("臺", "台")
    s = re.sub(r"[\s　\-＿_－—–/\\|｜·．・•()（）［］\[\]{}｛｝]+", "", s)
    return s.strip()


def _row_date_in_range_for_warrant_cache(row: dict, start_date=None, end_date=None) -> bool:
    """判斷 Google Sheet 權證快取列是否落在指定區間；沒有日期欄位時視為可用。"""
    date_value = _pick_row_value(row, ["日期", "Date", "交易日期", "出表日期", "date", "trade_date"])
    if not str(date_value or "").strip():
        return True
    dt = parse_date(date_value)
    if dt is None:
        return True
    ts = pd.Timestamp(dt).normalize()
    if start_date is not None and ts < pd.Timestamp(start_date).normalize():
        return False
    if end_date is not None and ts > pd.Timestamp(end_date).normalize():
        return False
    return True


def _is_call_warrant_name(name: str) -> bool:
    """保守判斷是否為認購權證；名稱空白時不在這裡否決，交由標的對照判斷。"""
    s = str(name or "").strip()
    if not s:
        return True
    if re.search(r"售|牛|熊", s):
        return False
    return "購" in s


def load_historical_call_warrants_from_cache(stock_code: str, stock_name: str, start_date=None, end_date=None) -> List[dict]:
    """從 Google Sheet 歷史 / 候選快取補權證母體。

    OpenAPI 只能取得最新交易日仍有量的權證，若某檔權證在圖表期間內曾爆量，
    但最新交易日無量或已不在最新清單，原本就不會被 API4/API5 回查。
    這裡改從既有 Google Sheet 快取補回同標的、區間內曾出現過的認購權證代號，
    再與 OpenAPI 母體合併去重。
    """
    if not GSHEET_FALLBACK_ENABLE:
        return []

    stock_code_clean = _clean_code(stock_code)
    aliases = make_stock_aliases(stock_name)
    aliases_norm = [_normalize_warrant_match_text(a) for a in aliases if _normalize_warrant_match_text(a)]
    records = {}

    sheet_names = [
        "快取_分點歷史",
        "快取_權證清單",
        "快取_候選組合_OpenAPI精選5",
        "快取_候選組合",
        "快取_候選組合_精選5",
    ]

    for sheet_name in sheet_names:
        df = read_gsheet_worksheet(sheet_name)
        if df is None or df.empty:
            continue

        sheet_hit = 0
        for _, r in df.iterrows():
            row = r.to_dict()
            if not _row_date_in_range_for_warrant_cache(row, start_date=start_date, end_date=end_date):
                continue

            code = normalize_openapi_warrant_code(_pick_row_value(row, [
                "代號", "權證代號", "warrant_code", "WarrantCode",
            ]))
            if not code or not re.fullmatch(r"\d{6}", code):
                continue

            name = str(_pick_row_value(row, [
                "名稱", "權證名稱", "warrant_name", "WarrantName",
            ]) or "").strip()
            if not _is_call_warrant_name(name):
                continue

            ucode = _clean_code(_pick_row_value(row, [
                "標的股", "標的代號", "underlying_code", "UnderlyingCode",
            ]))
            uname = str(_pick_row_value(row, [
                "標的名稱", "underlying_name", "UnderlyingName",
            ]) or "").strip()

            name_key = _normalize_warrant_match_text(name)
            name_front = name_key[:16]
            name_match = bool(name_front and any(alias and alias in name_front for alias in aliases_norm))
            lookup_match = bool(ucode and ucode == stock_code_clean)
            underlying_name_match = bool(uname and any(alias and alias in _normalize_warrant_match_text(uname) for alias in aliases_norm))

            if not (lookup_match or name_match or underlying_name_match):
                continue

            rec = records.setdefault(code, {
                "代號": code,
                "名稱": "",
                "標的股": str(stock_code_clean),
                "標的名稱": stock_name,
                "成交金額": 0,
                "成交量": 0,
                "資料來源": set(),
            })
            if name and not rec["名稱"]:
                rec["名稱"] = name
            if ucode and not rec["標的股"]:
                rec["標的股"] = ucode
            if uname and (not rec["標的名稱"] or rec["標的名稱"] == stock_name):
                rec["標的名稱"] = uname
            rec["成交金額"] = max(int(rec.get("成交金額", 0) or 0), clean_openapi_number(_pick_row_value(row, ["成交金額", "成交金額(元)"])))
            rec["成交量"] = max(int(rec.get("成交量", 0) or 0), clean_openapi_number(_pick_row_value(row, ["成交量", "成交張數", "成交數量"])))
            rec["資料來源"].add(sheet_name)
            sheet_hit += 1

        if sheet_hit > 0:
            print(f"☁️ 權證歷史母體快取命中：{sheet_name}｜{sheet_hit:,} 筆")

    out = []
    for rec in records.values():
        rec = dict(rec)
        rec["資料來源"] = "+".join(sorted(rec.get("資料來源", [])))
        if not rec.get("名稱"):
            rec["名稱"] = rec["代號"]
        out.append(rec)

    out = sorted(out, key=lambda x: (int(x.get("成交金額", 0) or 0), int(x.get("成交量", 0) or 0)), reverse=True)
    if out:
        print(f"☁️ Google Sheet 歷史母體補充候選：{len(out):,} 支")
    return out



def _moneydj_fix_mojibake(value) -> str:
    """修正 MoneyDJ ProxyXQ 回傳中偶爾出現的 UTF-8 / latin1 亂碼。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value)
    try:
        return s.encode("latin1").decode("utf-8")
    except Exception:
        return s


def _moneydj_clean_text(value) -> str:
    s = _moneydj_fix_mojibake(value)
    s = html.unescape(str(s or ""))
    s = s.replace("\ufeff", "")
    s = s.replace("\xa0", " ")
    s = s.replace("\u3000", " ")
    s = s.replace("臺", "台")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _moneydj_safe_int(value, default=0) -> int:
    try:
        s = str(value or "").replace(",", "").strip()
        if not s:
            return int(default)
        return int(float(s))
    except Exception:
        return int(default)


def _moneydj_build_warrant_search_param(target: str) -> str:
    """建立 MoneyDJ 權證搜尋 ProxyXQ 參數。

    關鍵條件：
    - C-csv2：要求回傳 rows 結構。
    - P-S1[3]B2[xxxx.TW/TWO]：以標的股代碼查詢。
    """
    target = str(target or "").strip().upper()
    return (
        "A-57"
        "^B-7"
        "^C-csv2"
        "^P-S1[3]"
        f"B2[{target}]"
        "C1[]"
        "C2[]"
        "E1[]"
        "E2[]"
        "S5[]"
        "S6[]"
        "S7[]"
        "S2[]"
        "S3[]"
        "S4[]"
        "H1[]"
        "H2["
    )


def fetch_moneydj_warrant_search_raw(target: str) -> str:
    """直接呼叫 MoneyDJ 權證搜尋備援 API，回傳原始文字。"""
    target = str(target or "").strip().upper()
    if not target:
        return ""

    session = get_thread_session()
    headers_page = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.moneydj.com/",
    }
    headers_api = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": headers_page["Accept-Language"],
        "Referer": MONEYDJ_WARRANT_SEARCH_PAGE,
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        # 先進搜尋頁建立 Cookie / Session，再打 ProxyXQ。
        try:
            session.get(MONEYDJ_WARRANT_SEARCH_PAGE, headers=headers_page, timeout=(8, 20))
        except Exception as e:
            print(f"⚠️ MoneyDJ 權證搜尋頁預載失敗，仍嘗試直接查詢：{target}｜{e}")

        param = _moneydj_build_warrant_search_param(target)
        url = MONEYDJ_WARRANT_PROXY_URL + "?param=" + urllib.parse.quote(param, safe="")
        resp = session.get(url, headers=headers_api, timeout=(8, 30))
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="replace")
        print(f"✅ MoneyDJ 權證搜尋備援：{target}｜回傳長度 {len(text):,}")
        return text
    except Exception as e:
        print(f"⚠️ MoneyDJ 權證搜尋備援失敗：{target}｜{e}")
        return ""


def parse_moneydj_warrant_search_rows(raw_text: str, stock_code: str, target: str, stock_name: str = "") -> List[dict]:
    """解析 MoneyDJ ProxyXQ 權證搜尋 rows，回傳認購權證母體。

    注意：此備援刻意不過濾成交量為 0 的權證，避免 MoneyDJ 搜尋頁成交量欄位尚未更新時漏掉權證。
    """
    if not raw_text:
        return []

    try:
        payload = json.loads(raw_text)
    except Exception as e:
        print(f"⚠️ MoneyDJ 權證搜尋備援 JSON 解析失敗：{target}｜{e}")
        return []

    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if not rows:
        print(f"⚠️ MoneyDJ 權證搜尋備援 rows 為空：{target}")
        return []

    stock_code_clean = _clean_code(stock_code)
    target = str(target or "").strip().upper()
    out = []
    seen = set()

    for item in rows:
        row = item.get("Row", []) if isinstance(item, dict) else []
        if not row or len(row) < 58:
            continue

        full_code = _moneydj_clean_text(row[0]).upper()
        code = normalize_openapi_warrant_code(full_code.replace(".TW", "").replace(".TWO", ""))
        warrant_name = _moneydj_clean_text(row[1])
        display_name = _moneydj_clean_text(row[48]) if len(row) > 48 else ""
        underlying_code = _moneydj_clean_text(row[29]).upper() if len(row) > 29 else ""
        underlying_name = _moneydj_clean_text(row[30]) if len(row) > 30 else ""
        full_type = _moneydj_clean_text(row[8]) if len(row) > 8 else ""
        display_type = _moneydj_clean_text(row[51]) if len(row) > 51 else ""

        # 僅取指定標的；MoneyDJ 對上市通常是 xxxx.TW，上櫃可能是 xxxx.TWO。
        if underlying_code != target:
            continue

        # 只取認購，不取認售 / 牛熊。
        if display_type != "認購" and "認購" not in full_type:
            continue

        if not code or not re.fullmatch(r"\d{6}", code):
            continue

        if code in seen:
            continue
        seen.add(code)

        name = display_name or warrant_name or code
        out.append({
            "代號": code,
            "名稱": name,
            "標的股": str(stock_code_clean),
            "標的名稱": underlying_name or stock_name,
            "成交金額": 0,
            "成交量": _moneydj_safe_int(row[28]) if len(row) > 28 else 0,
            "母體來源": "MoneyDJSearch",
        })

    out = sorted(out, key=lambda x: (int(x.get("成交量", 0) or 0), x.get("代號", "")), reverse=True)
    print(f"🔎 MoneyDJ 權證搜尋備援解析：{target}｜認購 {len(out):,} 支（未過濾成交量=0）")
    return out


def fetch_moneydj_call_warrants_fallback(stock_code: str, stock_name: str) -> List[dict]:
    """OpenAPI 沒抓到權證母體時，改用 MoneyDJ 依標的股代碼抓認購權證。

    會依序嘗試：
    1. xxxx.TW
    2. xxxx.TWO

    並合併去重。此備援不過濾成交量為 0。
    """
    stock_code_clean = _clean_code(stock_code)
    if not stock_code_clean:
        return []

    records = {}
    for suffix in ["TW", "TWO"]:
        target = f"{stock_code_clean}.{suffix}"
        raw = fetch_moneydj_warrant_search_raw(target)
        rows = parse_moneydj_warrant_search_rows(raw, stock_code_clean, target, stock_name=stock_name)
        for rec in rows:
            code = normalize_openapi_warrant_code(rec.get("代號"))
            if not code:
                continue
            old = records.get(code)
            if old is None:
                records[code] = rec
            else:
                # 若兩個市場來源重複，保留成交量較大的資料。
                if int(rec.get("成交量", 0) or 0) > int(old.get("成交量", 0) or 0):
                    records[code] = rec

    out = list(records.values())
    out = sorted(out, key=lambda x: (int(x.get("成交量", 0) or 0), x.get("代號", "")), reverse=True)
    if out:
        print(f"✅ MoneyDJ 權證搜尋備援完成：{stock_code_clean} {stock_name}｜認購 {len(out):,} 支（未過濾成交量=0）")
    else:
        print(f"⚠️ MoneyDJ 權證搜尋備援沒有取得認購權證：{stock_code_clean} {stock_name}")
    return out


def get_all_active_call_warrants(stock_code: str, stock_name: str, start_date=None, end_date=None) -> List[dict]:
    frames = []
    for source_label, f in [("TWSE", fetch_twse_openapi_warrant_daily_df()), ("TPEx", fetch_tpex_openapi_warrant_daily_df())]:
        if f is None or f.empty:
            continue
        trade_dates = sorted([d for d in f["交易日期"].unique() if str(d).strip()], key=parse_openapi_trade_date_for_sort)
        if not trade_dates:
            continue
        latest_trade_date = trade_dates[-1]
        latest_df = f[f["交易日期"] == latest_trade_date].copy()
        frames.append(latest_df)
        print(f"🔎 {source_label} OpenAPI 最新交易日：{latest_trade_date}｜當日權證：{len(latest_df):,} 支")

    if frames:
        all_df = pd.concat(frames, ignore_index=True).fillna("")
        active_df = all_df[
            (pd.to_numeric(all_df["成交量"], errors="coerce").fillna(0) > 0)
            & (all_df["名稱"].astype(str).str.contains("購", na=False))
            & (~all_df["名稱"].astype(str).str.contains("售|牛|熊", na=False))
            & (all_df["代號"].astype(str).str.fullmatch(r"\d{6}", na=False))
        ].copy()
    else:
        active_df = pd.DataFrame(columns=["代號", "名稱", "成交金額", "成交量"])
    print(f"🔎 OpenAPI 認購候選（最新交易日成交量 > 0）：{len(active_df):,} 支")

    lookup = load_warrant_underlying_lookup()
    aliases = make_stock_aliases(stock_name)
    aliases_norm = [_normalize_warrant_match_text(a) for a in aliases if _normalize_warrant_match_text(a)]
    stock_code_clean = _clean_code(stock_code)
    warrants = []
    seen = set()
    name_match_count = 0
    lookup_match_count = 0
    moneydj_added = 0

    for _, r in active_df.sort_values(["成交金額", "成交量"], ascending=[False, False]).iterrows():
        code = normalize_openapi_warrant_code(r.get("代號"))
        name = str(r.get("名稱", "")).strip()
        if not code or code in seen:
            continue
        cached = lookup.get(code, {})
        ucode = _clean_code(cached.get("underlying_code", ""))
        uname = str(cached.get("underlying_name", "")).strip()

        name_key = _normalize_warrant_match_text(name)
        # 權證名稱通常格式為「標的 + 發行券商 + 到期年月 + 購xx」，但不同來源可能有空白或符號。
        # 這裡改成檢查股票別名是否出現在名稱前段，而不是硬性 startswith。
        name_front = name_key[:16]
        name_match = any(alias and alias in name_front for alias in aliases_norm)
        lookup_match = bool(ucode and ucode == stock_code_clean)

        if lookup_match or name_match:
            if lookup_match:
                lookup_match_count += 1
            if name_match:
                name_match_count += 1
            seen.add(code)
            warrants.append({
                "代號": code,
                "名稱": name,
                "標的股": str(stock_code_clean),
                "標的名稱": uname or stock_name,
                "成交金額": int(r.get("成交金額", 0) or 0),
                "成交量": int(r.get("成交量", 0) or 0),
                "母體來源": "OpenAPI",
            })

    # MoneyDJ 權證搜尋補漏：
    # 原本只有在 OpenAPI 完全抓不到母體時才啟用 MoneyDJ Search。
    # 這會造成 OpenAPI 已有部分權證、但漏掉 MoneyDJ 當天已有成交量的權證時，後續 API4/API5 完全不會查該檔。
    # 這版改成：
    # - OpenAPI 有抓到權證：MoneyDJ Search 也跑一次，只補「OpenAPI 沒有、且 MoneyDJ Search 成交量達門檻」的漏網權證。
    # - OpenAPI 完全沒有權證：維持原本備援行為，MoneyDJ Search 找到的認購權證全部補進，不過濾成交量 0。
    moneydj_skipped_low_volume = 0
    if MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_ENABLE or not warrants:
        if warrants:
            print(
                f"🔁 MoneyDJ 權證搜尋補漏啟用：OpenAPI 已命中 {len(warrants):,} 支，"
                f"額外補 MoneyDJ Search 成交量 >= {MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_MIN_VOLUME:,} 的漏網權證"
            )
        else:
            print("⚠️ OpenAPI 未取得可用認購權證母體或比對後為 0，啟用 MoneyDJ 權證搜尋備援（不過濾成交量=0）")

        moneydj_warrants = fetch_moneydj_call_warrants_fallback(stock_code_clean, stock_name)
        had_openapi_warrants = bool(warrants)

        for rec in moneydj_warrants:
            code = normalize_openapi_warrant_code(rec.get("代號"))
            if not code or code in seen:
                continue

            mdj_volume = int(rec.get("成交量", 0) or 0)

            # OpenAPI 已有母體時只補 MoneyDJ 有量的漏網權證，避免把全部 0 量權證都丟進 API4/API5。
            # OpenAPI 完全沒有母體時維持原本備援：不過濾成交量 0。
            if (
                had_openapi_warrants
                and MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_MIN_VOLUME > 0
                and mdj_volume < MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_MIN_VOLUME
            ):
                moneydj_skipped_low_volume += 1
                continue

            seen.add(code)
            moneydj_added += 1
            warrants.append({
                "代號": code,
                "名稱": str(rec.get("名稱", "") or code).strip(),
                "標的股": str(stock_code_clean),
                "標的名稱": str(rec.get("標的名稱", "") or stock_name).strip(),
                "成交金額": int(rec.get("成交金額", 0) or 0),
                "成交量": mdj_volume,
                "母體來源": "MoneyDJSearchSupplement" if had_openapi_warrants else "MoneyDJSearch",
            })

        if had_openapi_warrants:
            print(
                f"🔁 MoneyDJ Search 補漏完成：新增 {moneydj_added:,} 支｜"
                f"低於成交量門檻略過 {moneydj_skipped_low_volume:,} 支"
            )

    historical_warrants = load_historical_call_warrants_from_cache(
        stock_code,
        stock_name,
        start_date=start_date,
        end_date=end_date,
    )
    historical_added = 0
    for rec in historical_warrants:
        code = normalize_openapi_warrant_code(rec.get("代號"))
        if not code or code in seen:
            continue
        seen.add(code)
        historical_added += 1
        warrants.append({
            "代號": code,
            "名稱": str(rec.get("名稱", "") or code).strip(),
            "標的股": str(stock_code_clean),
            "標的名稱": str(rec.get("標的名稱", "") or stock_name).strip(),
            "成交金額": int(rec.get("成交金額", 0) or 0),
            "成交量": int(rec.get("成交量", 0) or 0),
            "母體來源": "GoogleSheetHistory",
        })

    if MAX_WARRANTS > 0:
        warrants = warrants[:MAX_WARRANTS]
    print(
        f"🔎 {stock_code_clean} {stock_name} 權證比對："
        f"lookup命中 {lookup_match_count:,} 支｜名稱命中 {name_match_count:,} 支｜"
        f"MoneyDJ補漏/備援新增 {moneydj_added:,} 支｜歷史補充新增 {historical_added:,} 支"
    )
    print(f"✅ {stock_code_clean} 相關認購權證：{len(warrants):,} 支")
    return warrants

def _api4_fetch_raw(code, start, end):
    """API4 原始抓取，不直接寫入統計；由呼叫端決定第一輪/第二輪後的最終狀態。"""
    retry_count = 0
    try:
        url = API4.format(code=code, start=start, end=end)
        data, retry_count = _moneydj_get_json_with_retry(url, scope="api4")
        rows = []
        for item in (data if isinstance(data, list) else [data]):
            rows.extend(item.get("ResultSet", {}).get("Result", []))
        return rows, ("success" if rows else "empty"), "", retry_count
    except Exception as e:
        retry_count = int(getattr(e, "retry_count", retry_count) or 0)
        return [], "failed", str(e), retry_count


def api4_get(code, start, end):
    rows, status, error, retry_count = _api4_fetch_raw(code, start, end)
    _record_api_fetch("api4", status, error=f"{code}｜{error}" if error else "", retry_count=retry_count)
    return rows


def api5_get(warrant, broker, days=None):
    retry_count = 0
    try:
        days = int(days if days is not None else API5_DAYS)
        url = API5.format(warrant=warrant, broker=broker, days=days)
        data, retry_count = _moneydj_get_json_with_retry(url, scope="api5")
        rs = data[0].get("ResultSet", {}) if isinstance(data, list) else data.get("ResultSet", {})
        rows = rs.get("Result", [])
        _record_api_fetch("api5", "success" if rows else "empty", retry_count=retry_count)
        return rows
    except Exception as e:
        retry_count = int(getattr(e, "retry_count", retry_count) or 0)
        _record_api_fetch("api5", "failed", error=f"{warrant}/{broker}｜{e}", retry_count=retry_count)
        return []


def fetch_all_broker_pairs_for_warrants(warrants: List[dict], start_s: str, end_s: str) -> List[dict]:
    pairs = {}
    if not warrants:
        return []

    def rows_to_pair_records(w, rows):
        out = []
        for row in rows or []:
            broker_code = str(row.get("V2", "")).strip()
            broker_name = normalize_branch_name(row.get("V3", ""))
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

    def scan_one(w):
        rows, status, error, retry_count = _api4_fetch_raw(w["代號"], start_s, end_s)
        return {
            "warrant": w,
            "rows": rows,
            "status": status,
            "error": error,
            "retry_count": retry_count,
        }

    def record_final_result(result, second_pass: bool = False):
        w = result.get("warrant", {})
        code = str(w.get("代號", "") or "").strip()
        source = str(w.get("母體來源", "") or "").strip()
        status = result.get("status", "failed")
        error = result.get("error", "")
        retry_count = int(result.get("retry_count", 0) or 0)

        # 歷史快取補進來的權證，若第二輪仍查不到 API4，代表 live 無法補齊；
        # 但既有快取資料已在 fetch_warrant_events_full_market 前段合併，不把它列為硬失敗。
        if (
            second_pass
            and API4_HISTORY_FAILURE_AS_EMPTY
            and status == "failed"
            and source == "GoogleSheetHistory"
        ):
            _record_api_fetch(
                "api4",
                "empty",
                error=f"{code}｜歷史補充權證 API4 二次補抓仍失敗，保留既有快取資料：{error}",
                retry_count=retry_count,
            )
            return

        _record_api_fetch(
            "api4",
            status,
            error=f"{code}｜{error}" if error and status == "failed" else "",
            retry_count=retry_count,
        )

    reset_api_fetch_stats("api4")
    print(f"🔎 API4 掃描全部分點：{len(warrants):,} 支權證，workers={API4_WORKERS}")

    failed_results = []
    with ThreadPoolExecutor(max_workers=max(1, API4_WORKERS)) as ex:
        futures = {ex.submit(scan_one, w): w for w in warrants}
        for i, fut in enumerate(as_completed(futures), 1):
            w = futures.get(fut, {})
            try:
                result = fut.result()
                if result.get("status") == "failed":
                    failed_results.append(result)
                else:
                    record_final_result(result, second_pass=False)
                    for rec in rows_to_pair_records(result.get("warrant", {}), result.get("rows", [])):
                        pairs[(rec["warrant_code"], rec["broker_code"])] = rec
            except Exception as e:
                failed_results.append({
                    "warrant": w,
                    "rows": [],
                    "status": "failed",
                    "error": f"future {w.get('代號', '')}｜{e}",
                    "retry_count": 0,
                })
            if i % 100 == 0:
                print(f"  API4 {i:,}/{len(warrants):,}，pairs={len(pairs):,}，待補抓={len(failed_results):,}")

    if failed_results and API4_SECOND_PASS_ENABLE:
        retry_warrants = [r.get("warrant", {}) for r in failed_results]
        print(
            f"🔁 API4 第一輪仍有 {len(retry_warrants):,} 支失敗，"
            f"{API4_SECOND_PASS_WAIT:g} 秒後進行第二輪低併發補抓，workers={API4_SECOND_PASS_WORKERS}"
        )
        time.sleep(max(0.0, float(API4_SECOND_PASS_WAIT)))
        second_failed = []
        with ThreadPoolExecutor(max_workers=max(1, API4_SECOND_PASS_WORKERS)) as ex:
            futures = {ex.submit(scan_one, w): w for w in retry_warrants}
            for i, fut in enumerate(as_completed(futures), 1):
                w = futures.get(fut, {})
                try:
                    result = fut.result()
                    if result.get("status") == "failed":
                        second_failed.append(result)
                    record_final_result(result, second_pass=True)
                    if result.get("status") != "failed":
                        for rec in rows_to_pair_records(result.get("warrant", {}), result.get("rows", [])):
                            pairs[(rec["warrant_code"], rec["broker_code"])] = rec
                except Exception as e:
                    result = {
                        "warrant": w,
                        "rows": [],
                        "status": "failed",
                        "error": f"second future {w.get('代號', '')}｜{e}",
                        "retry_count": 0,
                    }
                    second_failed.append(result)
                    record_final_result(result, second_pass=True)
        if second_failed:
            print(f"⚠️ API4 第二輪後仍失敗：{len(second_failed):,} 支")
    else:
        for result in failed_results:
            record_final_result(result, second_pass=False)

    print_api_fetch_stats("api4", "API4")
    abort_if_api_failure_too_high("api4", "API4")
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
                "branch": normalize_branch_name(p.get("branch", "")),
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

    reset_api_fetch_stats("api5")
    print(f"💰 API5 回查買賣金額：{len(pair_list):,} 組，workers={API5_WORKERS}")
    with ThreadPoolExecutor(max_workers=max(1, API5_WORKERS)) as ex:
        futures = {ex.submit(fetch_one, p): p for p in pair_list}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures.get(fut, {})
            try:
                rows.extend(fut.result())
            except Exception as e:
                _record_api_fetch("api5", "failed", error=f"future {p.get('warrant_code', '')}/{p.get('broker_code', '')}｜{e}")
            if i % 200 == 0:
                print(f"  API5 {i:,}/{len(pair_list):,}，events={len(rows):,}")
    print_api_fetch_stats("api5", "API5")
    abort_if_api_failure_too_high("api5", "API5")
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
    # 優先讀 Google Sheet 完整快照；只有狀態表標記 complete 且 API4/API5 失敗數為 0 才會直接使用。
    gsheet_snapshot = load_gsheet_warrant_events_snapshot(stock_code, start_date=start_date, end_date=end_date)
    if gsheet_snapshot is not None and not gsheet_snapshot.empty:
        return gsheet_snapshot

    # 保留本機快照相容舊流程，但預設關閉；若使用，仍排在 Google Sheet 完整快照之後。
    local_snapshot = load_local_warrant_events_snapshot(stock_code, start_date=start_date, end_date=end_date)
    if local_snapshot is not None and not local_snapshot.empty:
        return local_snapshot

    # 既有 Google Sheet 歷史列只作 live 合併備援；沒有完整狀態紀錄時，不會直接當作完整快照。
    cached = load_cached_warrant_history(stock_code, start_date=start_date, end_date=end_date)
    frames = []
    if not cached.empty:
        frames.append(cached)
        print(f"☁️ Google Sheet {GSHEET_WARRANT_HISTORY_SHEET} 既有歷史列命中：{len(cached):,} 筆，將與 100% live 資料合併去重")

    live_fetched = False
    if LIVE_FETCH_ENABLE:
        end_dt = pd.Timestamp(end_date).to_pydatetime()
        start_s = (end_dt - timedelta(days=API4_SCAN_CALENDAR_DAYS)).strftime("%Y/%m/%d")
        end_s = end_dt.strftime("%Y/%m/%d")
        warrants = get_all_active_call_warrants(stock_code, stock_name, start_date=start_date, end_date=end_date)
        pairs = fetch_all_broker_pairs_for_warrants(warrants, start_s, end_s)
        live = fetch_api5_events_for_pairs(pairs, start_date=start_date, end_date=end_date)
        live_fetched = True
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
    events["branch"] = events["branch"].map(normalize_branch_name)
    events["broker_code"] = events["broker_code"].astype(str).str.strip()
    # 合併 live/cache 重複資料
    # 注意：net_amount 是有正負號的欄位，不能直接用 max。
    # 若賣超資料為負數，max 會選到絕對值較小、較接近 0 的那筆，造成賣超被低估。
    # 因此只對買進 / 賣出金額取 max，最後再重新計算 net_amount，確保：
    # net_amount = buy_amount - sell_amount。
    group_cols = ["Date", "broker_code", "branch", "warrant_code", "warrant_name", "underlying_code", "underlying_name"]
    events = events.groupby(group_cols, as_index=False, dropna=False).agg({
        "buy_amount": "max",
        "sell_amount": "max",
    })
    events["net_amount"] = events["buy_amount"] - events["sell_amount"]
    events = events[(events["buy_amount"] > 0) | (events["sell_amount"] > 0) | (events["net_amount"].abs() > 0)].copy()
    events["side"] = np.where(events["net_amount"] >= 0, "買超", "賣超")
    events = events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)

    # 只有本次真的完成 live 抓取，且 API4/API5 皆 100% 無 failed，才寫回 Google Sheet 完整快照。
    if live_fetched:
        save_gsheet_warrant_events_snapshot(stock_code, stock_name, events, start_date=start_date, end_date=end_date)
        save_local_warrant_events_snapshot(stock_code, events, start_date=start_date, end_date=end_date)

    return events


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
    e["branch"] = e["branch"].map(normalize_branch_name)
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


def _normalize_branch_for_head_office_check(branch_name: str) -> str:
    """將分點名稱正規化，用於判斷是否為券商總公司型分點。"""
    s = normalize_branch_name(branch_name)
    s = s.replace("股份有限公司", "").replace("有限公司", "")
    return s


_DEFAULT_TOP5_HEAD_OFFICE_BRANCHES = {
    # 本土主要權證 / 經紀券商總公司常見顯示名稱
    "元大", "元大證券",
    "富邦", "富邦證券",
    "凱基", "凱基證券",
    "群益", "群益證券", "群益金鼎", "群益金鼎證券",
    "國泰", "國泰證券",
    "永豐", "永豐證券", "永豐金", "永豐金證券",
    "統一", "統一證券",
    "台新", "台新證券",
    "元富", "元富證券",
    "兆豐", "兆豐證券",
    "玉山", "玉山證券",
    "華南永昌", "華南永昌證券",
    "中國信託", "中國信託證券", "中信", "中信證券",
    "第一金", "第一金證券",
    "合庫", "合庫證券", "合作金庫", "合作金庫證券",
    "國票", "國票證券",
    "康和", "康和證券",
    "宏遠", "宏遠證券",
    "新光", "新光證券",
    "日盛", "日盛證券",
    "臺銀", "臺銀證券", "台銀", "台銀證券",
    "土銀", "土銀證券",
    "彰銀", "彰銀證券",
    "大昌", "大昌證券",
    "大展", "大展證券",
    "大慶", "大慶證券",
    "福邦", "福邦證券",
    "犇亞", "犇亞證券",
    "高橋", "高橋證券",
    "光和", "光和證券",
    "美好", "美好證券",
    "陽信", "陽信證券",
    "致和", "致和證券",
    "遠智", "遠智證券",
    "安泰", "安泰證券",
    "台中銀", "台中銀證券",
    "三信", "三信證券",
    "聯邦", "聯邦證券",
    "亞東", "亞東證券",
    "大和國泰", "大和國泰證券",
    "上海商銀", "上海商銀證券",
    # 外資 / 發行或造市常見總公司名稱
    "美林", "美林證券", "美商美林", "美商美林證券",
    "摩根士丹利", "台灣摩根士丹利", "台灣摩根士丹利證券",
    "摩根大通", "摩根大通證券", "摩根大通證券台北",
    "高盛", "高盛證券", "美商高盛", "美商高盛證券",
    "瑞銀", "瑞銀證券", "新加坡商瑞銀", "新加坡商瑞銀證券",
    "麥格理", "麥格理證券", "港商麥格理", "港商麥格理證券",
    "花旗", "花旗環球", "花旗環球證券",
    "法銀巴黎", "法銀巴黎證券", "巴黎證券",
    "野村", "野村證券", "香港商野村", "香港商野村證券",
    "里昂", "里昂證券", "港商里昂", "港商里昂證券",
    "滙豐", "滙豐證券", "匯豐", "匯豐證券", "香港上海滙豐", "香港上海匯豐",
    "德意志", "德意志證券",
    "渣打", "渣打證券",
    "瑞士信貸", "瑞信", "瑞信證券",
}


def _get_top5_head_office_branch_set() -> set:
    """取得 TOP5 要排除的總公司型分點名稱集合，可用環境變數額外補充。"""
    names = set(_DEFAULT_TOP5_HEAD_OFFICE_BRANCHES)
    if TOP5_EXTRA_HEAD_OFFICE_BRANCHES:
        for item in re.split(r"[,，;；\n\r]+", TOP5_EXTRA_HEAD_OFFICE_BRANCHES):
            item = _normalize_branch_for_head_office_check(item)
            if item:
                names.add(item)
    return {_normalize_branch_for_head_office_check(x) for x in names if _normalize_branch_for_head_office_check(x)}


def _get_top5_head_office_branch_allowlist() -> set:
    """取得 TOP5 總公司型分點白名單，名單內分點即使像總公司名稱也保留。"""
    names = set()
    if TOP5_HEAD_OFFICE_BRANCH_ALLOWLIST:
        for item in re.split(r"[,，;；\n\r]+", TOP5_HEAD_OFFICE_BRANCH_ALLOWLIST):
            item = _normalize_branch_for_head_office_check(item)
            if item:
                names.add(item)
    return names


def is_top5_head_office_branch(branch_name: str) -> bool:
    """
    判斷是否為券商總公司型分點。

    原則：
    1. 分點名稱「完全等於」券商總公司名稱才排除。
    2. 分點名稱明確含總公司、總部、本部、自營等字樣才排除。
    3. 不用 startswith 排除，避免富邦公益、元大南屯、群益金鼎某分點被誤刪。
    """
    s = _normalize_branch_for_head_office_check(branch_name)
    if not s or s == "未知分點":
        return False

    # 白名單優先：新光、第一金雖然名稱看起來像總公司型分點，
    # 但依需求保留在 TOP5 排名，不做總公司過濾。
    if s in _get_top5_head_office_branch_allowlist():
        return False

    explicit_head_office_keywords = [
        "總公司", "證券總公司", "總部", "證券總部", "總管理處",
        "證券本部", "本部", "自營部", "自營", "承銷部", "金融交易部",
        "衍生性商品部", "衍商部", "權證部", "權證交易部", "金融商品部",
    ]
    if any(k in s for k in explicit_head_office_keywords):
        return True

    return s in _get_top5_head_office_branch_set()


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
    trading_dates = [pd.Timestamp(d).normalize() for d in list(plot_df.index)]

    # 週報的權證統計區間改用「股價日期 + 權證事件日期」合併日期軸。
    # 原本只用股價 K 線最新日當週報結束日，若 yfinance 晚一天更新，
    # TOP5 買賣超與本週權證淨流向就會漏掉 MoneyDJ / Google Sheet 已經更新的今日分點資料。
    if warrant_events is not None and not warrant_events.empty and "Date" in warrant_events.columns and trading_dates:
        report_dates = build_flow_axis_dates(plot_df, warrant_events)
    else:
        report_dates = trading_dates

    report_dates = [pd.Timestamp(d).normalize() for d in report_dates]
    week_dates = report_dates[-week_days:] if len(report_dates) >= week_days else report_dates
    week_start = pd.Timestamp(week_dates[0]).normalize() if week_dates else pd.NaT
    week_end = pd.Timestamp(week_dates[-1]).normalize() if week_dates else pd.NaT

    if pd.notna(week_start) and pd.notna(week_end):
        stock_week_dates = [d for d in trading_dates if pd.Timestamp(d).normalize() >= week_start and pd.Timestamp(d).normalize() <= week_end]
    else:
        stock_week_dates = []
    if not stock_week_dates:
        stock_week_dates = trading_dates[-week_days:] if len(trading_dates) >= week_days else trading_dates

    week_stock = plot_df.loc[stock_week_dates].copy() if stock_week_dates else plot_df.tail(0)

    if stock_week_dates:
        prev_end_pos = plot_df.index.get_loc(stock_week_dates[0])
        if isinstance(prev_end_pos, slice):
            prev_end_pos = prev_end_pos.start or 0
        elif isinstance(prev_end_pos, np.ndarray):
            prev_end_pos = int(np.where(prev_end_pos)[0][0]) if prev_end_pos.any() else 0
        prev_start_pos = max(0, int(prev_end_pos) - week_days)
        prev_stock = plot_df.iloc[prev_start_pos:int(prev_end_pos)].copy()
    else:
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
        if "branch" in e.columns:
            e["branch"] = e["branch"].map(normalize_branch_name)
        e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
        e = e.dropna(subset=["Date"])
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

        if pd.notna(week_start) and pd.notna(week_end):
            week_events = e[(e["Date"] >= week_start) & (e["Date"] <= week_end)].copy()
        else:
            week_events = e.iloc[0:0].copy()

        if trading_dates:
            plot_start = pd.Timestamp(plot_df.index.min()).normalize()
            plot_end = pd.Timestamp(report_dates[-1]).normalize() if report_dates else pd.Timestamp(plot_df.index.max()).normalize()
            plot_events = e[(e["Date"] >= plot_start) & (e["Date"] <= plot_end)].copy()
        else:
            plot_events = e.copy()

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
        "report_dates": report_dates,
        "stock_week_dates": stock_week_dates,
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
    return daily_warrant_net_from_dates(dates, events)


def daily_warrant_net_from_dates(dates, events: pd.DataFrame) -> pd.DataFrame:
    """依指定日期軸彙總每日權證淨額。

    原本 daily_warrant_net() 只會使用股價 K 線日期作為日期軸，因此若權證分點資料已經更新到
    最新交易日，但 yfinance 股價還停在前一個交易日，最新一日的權證資金流就會被圖表日期軸排除。
    這個函式保留原本欄位格式，但允許呼叫端傳入「股價日期 + 權證事件日期」合併後的日期軸。
    """
    dates = pd.to_datetime(pd.Index(list(dates)), errors="coerce").dropna().normalize()
    dates = pd.Index(sorted(pd.unique(dates)))
    out = pd.DataFrame({"Date": dates})
    if events is None or events.empty:
        out["net_amount"] = 0.0
        out["buy_amount"] = 0.0
        out["sell_amount"] = 0.0
        return out
    e = events.copy()
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    if e.empty:
        out["net_amount"] = 0.0
        out["buy_amount"] = 0.0
        out["sell_amount"] = 0.0
        return out
    g = e.groupby("Date", as_index=False).agg({"net_amount": "sum", "buy_amount": "sum", "sell_amount": "sum"})
    out = out.merge(g, on="Date", how="left").fillna(0.0)
    return out


def build_flow_axis_dates(plot_df: pd.DataFrame, events: pd.DataFrame) -> List[pd.Timestamp]:
    """建立精選分點資金流專用日期軸。

    會以股價 K 線日期為基礎，再補上同區間後方已出現的權證事件日期。
    這樣可避免股價來源晚一天更新時，精選分點當日買賣超被排除在圖外。
    """
    stock_dates = pd.to_datetime(plot_df.index, errors="coerce").dropna().normalize()
    if len(stock_dates) == 0:
        return []

    min_date = pd.Timestamp(stock_dates.min()).normalize()
    dates = set(pd.Timestamp(d).normalize() for d in stock_dates)

    if events is not None and not events.empty and "Date" in events.columns:
        event_dates = pd.to_datetime(events["Date"], errors="coerce").dropna().dt.normalize()
        for d in event_dates:
            d = pd.Timestamp(d).normalize()
            if d >= min_date:
                dates.add(d)

    return sorted(dates)


def filter_events_by_date_range(events_df: pd.DataFrame, start_date, end_date) -> pd.DataFrame:
    """依日期區間篩選事件，保留原本欄位。"""
    if events_df is None or events_df.empty:
        return pd.DataFrame()
    if pd.isna(start_date) or pd.isna(end_date):
        return events_df.copy()
    e = events_df.copy()
    if "Date" not in e.columns:
        return pd.DataFrame()
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    return e[(e["Date"] >= start_ts) & (e["Date"] <= end_ts)].copy().reset_index(drop=True)


def get_taipei_today_ts() -> pd.Timestamp:
    """取得台北日期的今日 00:00。

    GitHub Actions runner 常用 UTC，若直接 datetime.today() 可能在台灣盤後仍停在前一天；
    權證分點資料抓取區間應以台北日期為準。
    """
    return pd.Timestamp(datetime.utcnow() + timedelta(hours=8)).normalize()


def _parse_selected_branch_flow_names(raw: str) -> List[str]:
    """解析精選分點字串，支援逗號、分號、換行與直線分隔，並保留設定順序。"""
    names = []
    raw = str(raw or "")
    for item in re.split(r"[,，;；\n\r|｜]+", raw):
        name = normalize_branch_name(item)
        if name and name not in names:
            names.append(name)
    return names


def _get_default_selected_branch_flow_list() -> List[str]:
    """取得預設五分點名單。"""
    return _parse_selected_branch_flow_names(DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES)


def _get_selected_branch_flow_list() -> List[str]:
    """取得精選分點名單，保留設定順序，並做與主程式一致的分點標準化。"""
    return _parse_selected_branch_flow_names(SELECTED_BRANCH_FLOW_BRANCHES)


def get_selected_branch_flow_mode_label() -> str:
    """取得精選分點資金流模式顯示文字。"""
    selected = _get_selected_branch_flow_list()
    default_selected = _get_default_selected_branch_flow_list()
    if selected and selected == default_selected:
        return "預設五分點"
    return "自訂分點"


def _get_selected_branch_flow_set() -> set:
    """取得精選分點名單，會先做與主程式一致的分點標準化。"""
    return set(_get_selected_branch_flow_list())


def filter_selected_branch_flow_events(events_df: pd.DataFrame) -> pd.DataFrame:
    """篩出精選分點資金流事件。

    條件：
    1. 分點名稱屬於 SELECTED_BRANCH_FLOW_BRANCHES。

    回傳後可直接丟給 daily_warrant_net()，產生每日淨額柱狀圖與累計折線圖。
    """
    if not SELECTED_BRANCH_FLOW_ENABLE:
        return pd.DataFrame()
    if events_df is None or events_df.empty:
        return pd.DataFrame()
    need_cols = {"Date", "branch", "net_amount", "buy_amount", "sell_amount"}
    if not need_cols.issubset(events_df.columns):
        return pd.DataFrame()

    selected_branches = _get_selected_branch_flow_set()
    if not selected_branches:
        return pd.DataFrame()

    e = events_df.copy()
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    e["branch"] = e["branch"].map(normalize_branch_name)
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        e[c] = pd.to_numeric(e[c], errors="coerce").fillna(0.0).astype(float)

    mask = e["branch"].isin(selected_branches)
    return e.loc[mask].copy().reset_index(drop=True)


def top_branch_tables(week_events: pd.DataFrame, topn: int = 5):
    cols = ["branch", "net_amount", "max_warrant_code", "max_warrant_name", "max_warrant_amount"]
    if week_events is None or week_events.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    e = week_events.copy()
    if "branch" in e.columns:
        e["branch"] = e["branch"].map(normalize_branch_name)
    if CROSS_BROKER_OFFSET_FILTER_ENABLE:
        e, offset_removed = filter_out_cross_broker_offset_trades(
            e,
            amount_diff_threshold=CROSS_BROKER_OFFSET_THRESHOLD,
            min_side_amount=CROSS_BROKER_OFFSET_MIN_SIDE_AMOUNT,
            do_filter=True,
        )
        if offset_removed > 0:
            print(f"🧹 TOP5 已排除疑似對手單 / 換手單：{offset_removed:,} 筆")

    e["branch"] = e["branch"].map(normalize_branch_name).replace("", "未知分點")

    if TOP5_EXCLUDE_HEAD_OFFICE_BRANCH_ENABLE and not e.empty:
        head_office_mask = e["branch"].map(is_top5_head_office_branch)
        head_office_removed = int(head_office_mask.sum())
        if head_office_removed > 0:
            removed_branches = sorted(set(e.loc[head_office_mask, "branch"].astype(str)))
            preview = "、".join(removed_branches[:10])
            if len(removed_branches) > 10:
                preview += "…"
            print(f"🏢 TOP5 已排除券商總公司型分點：{head_office_removed:,} 筆｜{preview}")
        e = e.loc[~head_office_mask].copy()

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
# 新聞摘要風格版本：調整 prompt 後使用新快取鍵，避免 Google Sheet 當日舊快取繼續輸出舊版空泛摘要。
NEWS_SUMMARY_STYLE_VERSION = os.getenv("WARRANT_NEWS_SUMMARY_STYLE_VERSION", "v2_newslike").strip() or "v2_newslike"
NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK = os.getenv("WARRANT_NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


def _news_points_cache_task() -> str:
    safe_version = re.sub(r"[^A-Za-z0-9_.-]", "_", str(NEWS_SUMMARY_STYLE_VERSION or "v2_newslike"))
    return f"news_points_{safe_version}"

# 只用真正抓到的新聞內文產生摘要；不要把 RSS 標題或導流摘要直接當成重點。
NEWS_MIN_BODY_CHARS = int(os.getenv("WARRANT_NEWS_MIN_BODY_CHARS", "260"))
# 預設：優先用新聞原文；若原文被擋，允許用 RSS 摘要文字「改寫成重點」，但不直接輸出標題。
NEWS_REQUIRE_ARTICLE_BODY = os.getenv("WARRANT_NEWS_REQUIRE_BODY", "0").strip().lower() not in ("0", "false", "no", "off")
NEWS_RSS_DESCRIPTION_FALLBACK = os.getenv("WARRANT_NEWS_RSS_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")
NEWS_OPENAI_ENABLE = os.getenv("WARRANT_NEWS_OPENAI_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
NEWS_OPENAI_MODEL = os.getenv("WARRANT_NEWS_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
# Gemini / LLM 設定：GitHub Actions 請設定 Repository Secret / Variable：WARRANTS_API_KEY
GEMINI_ENABLE = os.getenv("WARRANT_GEMINI_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
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
# 預設提高搜尋母體，避免部分冷門股因前幾篇原文被擋或 RSS 摘要太短而沒有新聞輸出。
NEWS_GOOGLE_MAX_ITEMS = int(os.getenv("WARRANT_NEWS_GOOGLE_MAX_ITEMS", "24"))
NEWS_GOOGLE_SCAN_MULTIPLIER = int(os.getenv("WARRANT_NEWS_GOOGLE_SCAN_MULTIPLIER", "8"))
NEWS_GOOGLE_MIN_USABLE_ARTICLES = int(os.getenv("WARRANT_NEWS_GOOGLE_MIN_USABLE_ARTICLES", str(max(2, min(4, NEWS_SUMMARY_MAX_POINTS)))))
NEWS_GOOGLE_FALLBACK_DAYS = os.getenv("WARRANT_NEWS_FALLBACK_DAYS", "7,14,30").strip() or "7,14,30"
# 極速新聞模式：預設開啟。只使用 Google News RSS 的標題 / 摘要 / URL，不進新聞網站抓原文。
# 若想回到高品質原文抓取模式，可在 GitHub Actions 設 WARRANT_NEWS_FAST_MODE=0。
NEWS_FAST_MODE = os.getenv("WARRANT_NEWS_FAST_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
# 極速模式最多建立幾篇 RSS 新聞素材；預設等於真正會送進 Gemini 的篇數，避免掃太多新聞拖慢速度。
NEWS_FAST_FETCH_MAX_ARTICLES = int(os.getenv(
    "WARRANT_NEWS_FAST_FETCH_MAX_ARTICLES",
    str(max(NEWS_MAX_ARTICLES_TO_GEMINI, NEWS_GOOGLE_MIN_USABLE_ARTICLES, NEWS_SUMMARY_MAX_POINTS)),
))
# 慢速原文模式最多真的進站抓幾篇；避免 Google News 搜到很多篇時，逐站爬文卡住整個 pipeline。
NEWS_SLOW_FETCH_MAX_ARTICLES = int(os.getenv(
    "WARRANT_NEWS_SLOW_FETCH_MAX_ARTICLES",
    str(max(NEWS_MAX_ARTICLES_TO_GEMINI, NEWS_GOOGLE_MIN_USABLE_ARTICLES, NEWS_SUMMARY_MAX_POINTS)),
))
# 新聞原文頁抓取 workers：只在 NEWS_FAST_MODE=0 時使用；極速模式會完全跳過原文抓取。
NEWS_ARTICLE_FETCH_WORKERS = int(os.getenv("WARRANT_NEWS_ARTICLE_FETCH_WORKERS", "8"))
# 慢速原文模式防卡死設定：future / batch 都有硬性時間上限，逾時就取消剩餘新聞。
NEWS_ARTICLE_FUTURE_TIMEOUT = float(os.getenv("WARRANT_NEWS_ARTICLE_FUTURE_TIMEOUT", str(max(12.0, NEWS_FETCH_TIMEOUT + 5.0))))
NEWS_ARTICLE_BATCH_TIMEOUT = float(os.getenv("WARRANT_NEWS_ARTICLE_BATCH_TIMEOUT", str(max(18.0, NEWS_ARTICLE_FUTURE_TIMEOUT + 5.0))))
# gnewsdecoder 只在慢速原文模式可能用到；若它卡住，超過秒數就放棄解碼，不拖住整份報告。
NEWS_GNEWSDECODER_ENABLE = os.getenv("WARRANT_NEWS_GNEWSDECODER_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
NEWS_GNEWSDECODER_TIMEOUT = float(os.getenv("WARRANT_NEWS_GNEWSDECODER_TIMEOUT", "3"))

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
    if len(s) < 34:
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


def _run_gnewsdecoder_with_timeout(url: str) -> str:
    """以硬 timeout 包住 googlenewsdecoder，避免單一 Google News 解碼卡死整份報告。"""
    if not NEWS_GNEWSDECODER_ENABLE or gnewsdecoder is None or NEWS_GNEWSDECODER_TIMEOUT <= 0:
        return ""

    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(gnewsdecoder, url, interval=1)
    try:
        decoded = fut.result(timeout=NEWS_GNEWSDECODER_TIMEOUT)
        if isinstance(decoded, dict):
            real = decoded.get("decoded_url", "")
            if real and str(real).startswith("http"):
                return str(real).strip()
        elif isinstance(decoded, str) and decoded.startswith("http"):
            return decoded.strip()
    except FuturesTimeoutError:
        fut.cancel()
        print(f"⚠️ googlenewsdecoder 超過 {NEWS_GNEWSDECODER_TIMEOUT:g} 秒未回應，已略過：{str(url)[:120]}")
    except Exception as e:
        print(f"⚠️ googlenewsdecoder 解碼失敗：{e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return ""


def _maybe_resolve_google_news_link(url: str) -> str:
    """Google News RSS 有時是跳轉頁；這裡嘗試解析成原始新聞網址。"""
    if not url or "news.google.com" not in url:
        return url or ""
    decoded_url = _decode_google_news_url_from_path(url)
    if decoded_url:
        return decoded_url

    decoded_url = _run_gnewsdecoder_with_timeout(url)
    if decoded_url:
        return decoded_url

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



def _get_news_search_day_list() -> List[int]:
    """新聞搜尋天數：先抓 7 天，不足時自動放寬到 14 / 30 天。"""
    days = []
    for part in re.split(r"[,，;；\s]+", str(NEWS_GOOGLE_FALLBACK_DAYS or "7,14,30")):
        part = str(part or "").strip()
        if not part:
            continue
        try:
            d = int(float(part))
        except Exception:
            continue
        if d > 0 and d not in days:
            days.append(d)
    return days or [7, 14, 30]


def _build_google_news_queries(stock_code: str, stock_name: str, days: int) -> List[tuple]:
    """建立多階段 Google News RSS 查詢。

    查詢策略：
    1. 嚴格：股票別名 + 基本面 / 產業 / 法人關鍵字。
    2. 放寬：股票別名 + 新聞 / 題材 / 展望等較廣關鍵字。
    3. 最寬：股票別名本身，讓冷門股也有機會抓到少量近期新聞。
    """
    aliases = _get_news_aliases(stock_code, stock_name)
    quoted_aliases = [f'"{a}"' for a in aliases[:6] if str(a or "").strip()]
    strict_part = " OR ".join(quoted_aliases) if quoted_aliases else f'"{stock_code}"'
    safe_excludes = "-三大法人 -買賣超 -排行 -完整看"

    strict_topics = (
        "營收 OR 財報 OR 獲利 OR 法說 OR 展望 OR 接單 OR 出貨 OR 產能 OR "
        "AI OR 伺服器 OR 記憶體 OR DRAM OR 半導體 OR 報價 OR HBM OR 法人 OR "
        "目標價 OR 評等 OR EPS OR ASP OR 毛利 OR 毛利率 OR 供需 OR 漲價"
    )
    broad_topics = (
        "新聞 OR 題材 OR 法人 OR 展望 OR 營運 OR 產業 OR 報價 OR 需求 OR "
        "接單 OR 出貨 OR 財報 OR 營收 OR 法說 OR 目標價 OR 評等"
    )

    queries = [
        ("嚴格基本面", f"({strict_part}) ({strict_topics}) {safe_excludes} when:{days}d"),
        ("放寬題材", f"({strict_part}) ({broad_topics}) {safe_excludes} when:{days}d"),
        ("股票別名", f"({strict_part}) {safe_excludes} when:{days}d"),
    ]
    # 去重但保留順序
    out = []
    seen = set()
    for label, q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in seen:
            out.append((label, q))
            seen.add(q)
    return out


def _is_enough_usable_news(articles: List[dict]) -> bool:
    usable = sum(1 for a in articles if a.get("body_ok") or a.get("fallback_ok"))
    return usable >= max(1, int(NEWS_GOOGLE_MIN_USABLE_ARTICLES))


def _make_google_news_rss_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query,
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    })


def _article_seen_key(title: str, link: str) -> str:
    title_key = _title_compare_text(title)
    if title_key:
        return title_key
    return str(link or "").strip()


def _build_fast_rss_news_content(title: str, description: str, source: str = "", published: str = "") -> str:
    """極速模式用：把 Google News RSS 可取得的欄位組成可送 Gemini 的素材，不抓原文。"""
    title = _clean_news_title(title)
    description = _normalize_news_text(description)
    source = str(source or "").strip()
    published = str(published or "").strip()

    parts = []
    if title:
        parts.append(f"標題：{title}")
    if description:
        # Google News RSS 的 description 有時會重複標題；仍保留，讓 Gemini 至少知道 RSS 摘要 / 導流文字。
        parts.append(f"摘要：{description}")
    if source:
        parts.append(f"來源：{source}")
    if published:
        parts.append(f"日期：{published}")
    return _normalize_news_text("。".join(parts))


def _is_valid_fast_rss_news_item(title: str, description: str, stock_code: str = "", stock_name: str = "") -> bool:
    """極速模式用：RSS 沒有原文時，放寬判斷，只要標題 / 摘要明確與本股票或公司題材相關即可。"""
    combined = _normalize_news_text(f"{title} {description}")
    if len(_title_compare_text(combined)) < 12:
        return False

    aliases = _get_news_aliases(stock_code, stock_name)
    if aliases and not any(alias and alias in combined for alias in aliases):
        return False

    # 排除明顯無效導流，但不要過度排除，否則 RSS 模式會因摘要偏短而完全抓不到新聞。
    bad_hits = [
        "三大法人買賣超", "買超排行", "賣超排行", "完整看", "看更多",
        "延伸閱讀", "相關新聞", "熱門新聞", "追蹤我們", "分享給朋友",
    ]
    if any(k in combined for k in bad_hits) and not _has_company_value_terms(combined):
        return False

    topic_keywords = [
        "營收", "財報", "獲利", "EPS", "毛利", "法說", "展望", "接單", "出貨", "產能",
        "AI", "伺服器", "記憶體", "DRAM", "NAND", "半導體", "報價", "HBM", "法人",
        "目標價", "評等", "需求", "漲價", "降價", "ASP", "供需", "訂單", "客戶",
    ]
    if any(k in combined for k in topic_keywords):
        return True

    # 若沒有明確基本面關鍵字，但標題 / 摘要有本股票名稱，仍保留給 Gemini 判斷，避免冷門股完全無新聞。
    return True


def fetch_google_news_articles(stock_code: str, stock_name: str, max_items: int = 10) -> List[dict]:
    """
    多階段抓取 Google News RSS 新聞。

    預設 NEWS_FAST_MODE=1：只使用 RSS 標題 / 摘要 / URL，不進新聞網站抓原文，速度最快。
    若設 NEWS_FAST_MODE=0：才會嘗試進入原文頁擷取內文，品質較高但速度較慢。

    先抓 7 天嚴格新聞；若有效新聞不足，會自動放寬關鍵字與天數到 14 / 30 天。
    回傳 dict 格式，讓後續 build_news_points 可以根據 RSS 摘要或新聞原文整理重點。
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
            "fallback_ok": False,
            "content_source": "manual",
            "body_length": len(p),
            "search_days": 0,
            "query_stage": "manual",
        } for p in parts[:max_items]]

    if not NEWS_ENABLE:
        return []

    max_items = max(int(max_items or NEWS_GOOGLE_MAX_ITEMS), NEWS_SUMMARY_MAX_POINTS)
    scan_limit_per_query = max(max_items, int(max_items * max(1, NEWS_GOOGLE_SCAN_MULTIPLIER)))
    article_workers = max(1, int(NEWS_ARTICLE_FETCH_WORKERS))
    fast_mode = bool(NEWS_FAST_MODE)

    # 這裡用「真正會送進 Gemini 的篇數」當作抓取上限，避免為了 max_items=24 去爬大量新聞。
    mode_fetch_limit = NEWS_FAST_FETCH_MAX_ARTICLES if fast_mode else NEWS_SLOW_FETCH_MAX_ARTICLES
    fetch_limit = max(
        NEWS_SUMMARY_MAX_POINTS,
        NEWS_GOOGLE_MIN_USABLE_ARTICLES,
        min(max_items, max(1, int(mode_fetch_limit))),
    )
    # RSS 掃描仍可略大於 fetch_limit，避免前幾筆被標題 / 導流過濾後完全無新聞；但慢速原文模式只抓前 N 篇候選。
    candidate_limit_per_query = max(fetch_limit, fetch_limit * 2) if fast_mode else fetch_limit

    aliases = _get_news_aliases(stock_code, stock_name)
    all_articles = []
    seen_keys = set()
    total_scanned = 0

    def usable_count_now() -> int:
        return sum(1 for a in all_articles if a.get("body_ok") or a.get("fallback_ok"))

    def enough_articles() -> bool:
        return usable_count_now() >= fetch_limit

    def build_article_from_candidate(candidate: dict) -> dict:
        title = candidate.get("title", "")
        link = candidate.get("url", "")
        description = candidate.get("description", "")
        days = int(candidate.get("search_days", 0) or 0)
        stage_label = candidate.get("query_stage", "")

        if fast_mode:
            # 極速模式：完全跳過 _fetch_article_body，不進新聞網站、不碰 gnewsdecoder，直接用 RSS 標題 / 摘要 / URL 給 Gemini 統整。
            article_body = ""
            body_ok = False
            fallback_ok = NEWS_RSS_DESCRIPTION_FALLBACK and _is_valid_fast_rss_news_item(
                title,
                description,
                stock_code,
                stock_name,
            )
            content = _build_fast_rss_news_content(
                title,
                description,
                source=candidate.get("source", ""),
                published=candidate.get("published", ""),
            ) if fallback_ok else ""
            content_source = "google_news_rss_fast" if fallback_ok else ""
        else:
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

        article = {
            "title": title,
            "url": link,
            "source": candidate.get("source", ""),
            "published": candidate.get("published", ""),
            "description": description,
            "content": content,
            "body_ok": body_ok,
            "fallback_ok": fallback_ok,
            "content_source": content_source,
            "body_length": len(article_body or ""),
            "search_days": days,
            "query_stage": stage_label,
        }
        if body_ok:
            status = "原文可摘要"
        elif fallback_ok and fast_mode:
            status = "極速RSS摘要"
        elif fallback_ok:
            status = "RSS摘要改寫"
        else:
            status = "略過標題"
        print(f"📰 新聞抓取：{title[:36]}｜近 {days} 天｜{stage_label}｜原文 {len(article_body or ''):,} 字｜{status}")
        return article

    def collect_slow_articles_with_timeout(chunk: List[dict]):
        """慢速原文模式專用：每批 future 有硬性批次 timeout，足夠就提早取消剩餘任務。"""
        if not chunk or enough_articles():
            return

        ex = ThreadPoolExecutor(max_workers=article_workers)
        futures = {ex.submit(build_article_from_candidate, candidate): candidate for candidate in chunk}
        pending = set(futures.keys())
        deadline = time.monotonic() + max(1.0, float(NEWS_ARTICLE_BATCH_TIMEOUT))

        try:
            while pending and not enough_articles():
                remain = deadline - time.monotonic()
                if remain <= 0:
                    print(f"⚠️ 新聞原文批次抓取超過 {NEWS_ARTICLE_BATCH_TIMEOUT:g} 秒，取消剩餘 {len(pending)} 篇")
                    break

                done, pending = wait(
                    pending,
                    timeout=min(0.5, max(0.05, remain)),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                for fut in done:
                    candidate = futures.get(fut, {})
                    try:
                        # fut 已完成，這裡再給 result(timeout=...) 是保險，避免極端狀況卡住。
                        article = fut.result(timeout=max(0.1, min(float(NEWS_ARTICLE_FUTURE_TIMEOUT), 1.0)))
                    except FuturesTimeoutError:
                        title = candidate.get("title", "")
                        fut.cancel()
                        print(f"⚠️ 新聞 future 超過 {NEWS_ARTICLE_FUTURE_TIMEOUT:g} 秒未回傳，已略過：{title[:36]}")
                        continue
                    except Exception as e:
                        title = candidate.get("title", "")
                        print(f"⚠️ 新聞平行抓取失敗：{title[:36]}｜{e}")
                        continue

                    all_articles.append(article)
                    if enough_articles():
                        break
        finally:
            if pending:
                for fut in pending:
                    fut.cancel()
                print(f"🧹 已取消未完成新聞原文任務：{len(pending)} 篇")
            ex.shutdown(wait=False, cancel_futures=True)

    for days in _get_news_search_day_list():
        for stage_label, query in _build_google_news_queries(stock_code, stock_name, days):
            if enough_articles():
                break

            url = _make_google_news_rss_url(query)
            mode_label = "極速RSS" if fast_mode else f"原文抓取 workers={article_workers} / limit={fetch_limit}"
            print(
                f"📰 Google News 搜尋：{stock_code} {stock_name}｜{stage_label}｜近 {days} 天｜"
                f"fetch_limit={fetch_limit}｜mode={mode_label}"
            )
            try:
                r = requests.get(url, headers={"User-Agent": HDR["User-Agent"]}, timeout=10)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                print(f"⚠️ Google News RSS 抓取失敗：{stage_label}｜近 {days} 天｜{e}")
                continue

            scanned_this_query = 0
            candidates = []
            for item in root.findall(".//item"):
                scanned_this_query += 1
                total_scanned += 1
                if scanned_this_query > scan_limit_per_query:
                    break

                title = _clean_news_title(item.findtext("title") or "")
                link = (item.findtext("link") or "").strip()
                published = (item.findtext("pubDate") or "").strip()
                source_el = item.find("source")
                source = (source_el.text if source_el is not None and source_el.text else "").strip()
                description = _normalize_news_text(_html_to_readable_text(item.findtext("description") or ""))

                if not _is_within_recent_days_from_rss(published, days=days):
                    continue
                if not title:
                    continue

                seen_key = _article_seen_key(title, link)
                if seen_key and seen_key in seen_keys:
                    continue

                combined_for_target_check = f"{title} {description}"
                if aliases and not any(alias in combined_for_target_check for alias in aliases):
                    # Google News 搜尋有時會回傳同產業但非本股票的多股新聞；先擋掉標題/摘要完全沒有本股票的項目。
                    continue

                if seen_key:
                    seen_keys.add(seen_key)

                candidates.append({
                    "title": title,
                    "url": link,
                    "source": source,
                    "published": published,
                    "description": description,
                    "search_days": int(days),
                    "query_stage": stage_label,
                })

                # 慢速模式只拿前 N 篇真的進站抓原文；極速模式也不建立過量素材。
                if len(candidates) >= candidate_limit_per_query:
                    break

            if candidates:
                if fast_mode:
                    # 極速模式不需要 ThreadPoolExecutor，因為不抓原文；逐筆用 RSS 摘要建立素材即可。
                    for candidate in candidates:
                        if enough_articles():
                            break
                        try:
                            article = build_article_from_candidate(candidate)
                        except Exception as e:
                            title = candidate.get("title", "")
                            print(f"⚠️ 新聞 RSS 素材建立失敗：{title[:36]}｜{e}")
                            continue
                        all_articles.append(article)
                else:
                    # 慢速模式只抓前 N 篇候選，並且有 batch timeout；足夠就提早取消剩餘任務。
                    chunk_size = max(1, min(fetch_limit, article_workers * 2))
                    for chunk_start in range(0, len(candidates), chunk_size):
                        if enough_articles():
                            break
                        chunk = candidates[chunk_start: chunk_start + chunk_size]
                        collect_slow_articles_with_timeout(chunk)

            if enough_articles():
                break
        if enough_articles():
            break

    usable_count = sum(1 for a in all_articles if a.get("body_ok") or a.get("fallback_ok"))
    print(f"📰 Google News 搜尋完成：掃描約 {total_scanned:,} 筆 RSS｜保留 {len(all_articles):,} 筆｜可摘要 {usable_count:,} 筆")

    # 排序：原文優先，其次 RSS 摘要；同類別中越近越前，最後保留真正要送 Gemini 的篇數。
    def _sort_key(article: dict):
        published_dt = _parse_rss_pub_date(article.get("published", "")) or datetime.min
        usable_rank = 0 if article.get("body_ok") else 1 if article.get("fallback_ok") else 2
        days_rank = int(article.get("search_days", 999) or 999)
        return (usable_rank, days_rank, -published_dt.timestamp() if published_dt != datetime.min else 0)

    all_articles = sorted(all_articles, key=_sort_key)
    return all_articles[:fetch_limit]

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
        "以下為您", "以下是", "為您整理", "整理如下", "統整如下", "重點如下",
        "根據您提供", "根據提供", "結合您提供", "結合全新", "深度預判", "深度預測",
        "市場觀察到", "重新評價機會", "重新評價的機會", "成長動能與市場關注度",
        "資料是否充足", "新聞內文 JSON", "本週資料 JSON", "markdown", "JSON格式", "請只回傳",
        "第一點", "第二點", "第三點", "圖中", "如圖", "第三張", "符號", "問號",
    ]
    if any(k in s for k in bad_keywords):
        return True
    if re.search(r"[?？]{1,}|[�□■◆◇●○★☆]{1,}", s):
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
    s = re.sub(r"^(新聞重點|新聞線索|本週新聞重點|本週重點|重點|摘要)[:：]\s*", "", s).strip()
    s = re.sub(r"^(以下為您|以下是|為您整理|整理如下|統整如下|重點如下)[：:，,。\s]*", "", s).strip()
    s = re.sub(r"^(根據您提供的資料|根據提供的資料|根據新聞內文|根據本週資料)[，,：:\s]*", "", s).strip()
    s = re.sub(r"\s+-\s+[^-]{1,40}$", "", s).strip()
    s = _remove_news_boilerplate(s)
    # 將 LLM 常見的空泛分析語氣改成較像財經新聞摘要的說法，避免「市場觀察到、重新評價」這類字眼太像 AI 分析。
    s = re.sub(r"^市場觀察到", "", s).strip()
    s = s.replace("可能因", "受")
    s = s.replace("而獲得重新評價的機會", "，使市場關注度升溫")
    s = s.replace("重新評價的機會", "市場關注度升溫")
    s = s.replace("相關成長動能與市場關注度", "相關訂單、營收與市場關注度")
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
    """Gemini 輸出太短時，從近期原文 / RSS 摘要候選句補足重點字數；不使用新聞標題硬湊。"""
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
    """確保新聞區塊至少約 150 字；資料不足時仍只從近期新聞素材補充。"""
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


def _get_warrants_api_keys() -> List[str]:
    """讀取 Gemini API Key 清單；GitHub Actions 可設定 WARRANTS_API_KEY、WARRANTS_API_KEY_2、WARRANTS_API_KEY_3。"""
    candidates = [
        os.getenv("WARRANTS_API_KEY", "").strip(),
        os.getenv("WARRANTS_API_KEY_2", "").strip(),
        os.getenv("WARRANTS_API_KEY_3", "").strip(),
        os.getenv("GEMINI_API_KEY", "").strip(),
        os.getenv("GOOGLE_API_KEY", "").strip(),
    ]
    keys = []
    seen = set()
    for key in candidates:
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _get_warrants_api_key() -> str:
    """保留舊函式相容性；回傳第一組可用 Gemini API Key。"""
    keys = _get_warrants_api_keys()
    return keys[0] if keys else ""


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


def _should_switch_gemini_key(err) -> bool:
    """Gemini 失敗時是否直接切換下一組 key；quota / 429 / 503 / timeout 都優先換 key。"""
    err_text = str(err)
    switch_keywords = [
        "429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "exceeded",
        "403", "401", "API key", "INVALID_ARGUMENT", "PERMISSION_DENIED",
        "503", "UNAVAILABLE", "high demand", "temporarily unavailable",
        "timeout", "Deadline", "deadline",
    ]
    return any(k in err_text for k in switch_keywords)


def _call_gemini_with_retry(prompt: str, cache_task: str = "", stock_code: str = "", stock_name: str = ""):
    # 第一優先：Google Sheet 每日快取。
    # 同股票、同任務、同模型、同一天只要跑過一次，當天再跑就不會重打 Gemini。
    # 這段必須放在 GEMINI_ENABLE 判斷前面，避免關閉 Gemini 時連當日快取也讀不到。
    cached_text = load_gsheet_llm_cache(cache_task, stock_code, stock_name, prompt) if cache_task and stock_code else ""
    if cached_text:
        save_llm_cache(prompt, cached_text)
        return cached_text

    # 第二優先：本機 prompt hash 快取。
    # 若本機命中，也順便補寫 Google Sheet，讓下次不同 runner 也能直接命中。
    cached_text = load_llm_cache(prompt)
    if cached_text:
        if cache_task and stock_code:
            save_gsheet_llm_cache(cache_task, stock_code, stock_name, prompt, cached_text)
        return cached_text

    if not GEMINI_ENABLE:
        print("ℹ️ Gemini 已關閉，且沒有命中當日快取，改用規則式摘要 / 既有資料流程")
        return None

    if genai is None:
        print("⚠️ 未安裝 google-genai，無法使用 Gemini 摘要；將改用規則式摘要")
        return None

    api_keys = _get_warrants_api_keys()
    if not api_keys:
        print("⚠️ 未設定 WARRANTS_API_KEY / WARRANTS_API_KEY_2 / WARRANTS_API_KEY_3，無法使用 Gemini 摘要；將改用規則式摘要")
        return None

    last_error = None
    total_keys = len(api_keys)
    for key_idx, api_key in enumerate(api_keys, 1):
        try:
            client = genai.Client(api_key=api_key)
        except Exception as e:
            last_error = e
            if key_idx < total_keys:
                print(f"⚠️ Gemini API Key {key_idx}/{total_keys} 初始化失敗，改用下一組：{str(e)[:180]}")
                continue
            print(f"⚠️ Gemini API Key 初始化失敗：{e}")
            return None

        for attempt in range(1, GEMINI_RETRY_TIMES + 1):
            try:
                print(f"Gemini 呼叫第 {attempt}/{GEMINI_RETRY_TIMES} 次，模型：{GEMINI_MODEL}｜API Key {key_idx}/{total_keys}")
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                )
                output_text = response.text or ""
                save_llm_cache(prompt, output_text)
                if cache_task and stock_code:
                    save_gsheet_llm_cache(cache_task, stock_code, stock_name, prompt, output_text)
                return output_text
            except Exception as e:
                last_error = e
                has_next_key = key_idx < total_keys

                # 有備用 Key 時，quota / 429 / 503 / timeout 直接換下一組，不在同一組 key 上空等。
                if has_next_key and _should_switch_gemini_key(e):
                    print(f"⚠️ Gemini API Key {key_idx}/{total_keys} 呼叫失敗，改用下一組：{str(e)[:180]}")
                    break

                if _is_retryable_gemini_error(e) and attempt < GEMINI_RETRY_TIMES:
                    wait_sec = GEMINI_RETRY_BASE_WAIT * attempt
                    print(f"⚠️ Gemini 暫時忙碌或限流，{wait_sec:.0f} 秒後重試：{str(e)[:180]}")
                    time.sleep(wait_sec)
                    continue

                if has_next_key:
                    print(f"⚠️ Gemini API Key {key_idx}/{total_keys} 重試後仍失敗，改用下一組：{str(e)[:180]}")
                    break

                print(f"⚠️ Gemini 呼叫失敗，所有 API Key 均無法完成：{e}")
                return None

    if last_error:
        print(f"⚠️ Gemini 呼叫失敗，所有 API Key 均已嘗試：{last_error}")
    return None


def _parse_gemini_points(output_text: str) -> List[str]:
    return _clean_summary_points(_parse_raw_points_from_llm(output_text))


def _build_gemini_news_articles(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    """只把可用的新聞素材送給 Gemini，並先萃取本股票相關片段，避免多家公司新聞數字混用。"""
    usable = []
    ordered = [r for r in records if r.get("body_ok") or r.get("fallback_ok")]
    for rec in ordered:
        content = _normalize_news_text(rec.get("content", ""))
        # 極速 RSS 模式本來就只有標題 / 摘要，門檻需比原文模式低；原文模式仍維持較高門檻。
        min_content_len = 40 if str(rec.get("content_source", "")) in ("google_news_rss_fast", "rss_description", "manual") else 80
        if len(content) < min_content_len:
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
        print("⚠️ 沒有足夠新聞原文 / RSS 摘要可送入 Gemini；不使用標題硬湊新聞重點")
        return []

    display_name = stock_name if stock_name else stock_code
    article_json = json.dumps(usable_articles, ensure_ascii=False, indent=2)
    prompt = f"""
你是台股財經新聞編輯，負責把新聞素材整理成圖片週報右下角的「新聞 / 題材觀察」。
你只能根據我提供的新聞素材整理，不可以使用外部知識，不可以自行補充。
請使用繁體中文。

股票：{stock_code} {display_name}

任務：
把近期新聞素材改寫成「像財經新聞摘要」的 2～3 點重點，給圖片週報右下角使用。
寫法要像新聞編輯整理，不要像投資研究報告，也不要像 AI 分析。
每一點都要清楚呈現：事件主軸 → 具體新聞內容 / 市場消息 → 對公司或產業的影響。

寫作風格要求：
1. 每點開頭請用 4～8 個字的短標籤，後面接全形冒號，例如「業績更新：」、「法人觀點：」、「AI 題材：」、「報價動向：」、「公司動態：」。
2. 句子要像財經新聞摘要，優先寫「誰發生什麼事、關鍵數字或事件、對公司可能影響」。
3. 優先使用具體新聞元素：營收、月增 / 年增、EPS、毛利率、法說、法人報告、目標價、評等、接單、出貨、報價、產能、供需、產品布局、公司公告。
4. 如果素材只有 Google News RSS 標題 / 摘要，不能硬說「本週宣布」或「公司證實」，請改成「市場關注」、「近期題材」、「媒體報導指出」等保守新聞語氣。
5. 避免空泛分析語氣，不要使用「市場觀察到」、「可能因」、「重新評價機會」、「成長動能與市場關注度」、「投資人可關注」這類句子。
6. 不要寫成純技術分析，不要提權證資金流、分點籌碼、K 線、均線或圖表內容；新聞區塊只整理新聞與題材。
7. 不要寫投資建議，不要寫「可以買進」「建議進場」「不追高」。

嚴格事實規則：
1. 不要直接複製新聞標題。
2. 不要輸出新聞網站名稱、作者、網址、完整看、看更多、延伸閱讀。
3. 不要把只有股價漲停、亮燈、強漲、創高、焦點股這類描述當成重點。
4. 每一點必須來自我提供的近期新聞素材，不可以幻想；若素材來自 14～30 天內舊新聞，請避免寫成「本週宣布」。
5. 如果新聞同時提到多家公司，所有目標價、評等、EPS、營收、獲利預估等數字，必須確認該數字在同一句或同一分句中明確指向「{stock_code} {display_name}」。
6. 嚴禁把台積電、瑞昱、聯詠或其他公司的目標價 / EPS / 營收預估寫成「{display_name}」的重點；若無法判斷數字屬於哪家公司，就不要使用該數字。
7. 若句子格式像「A 公司目標價 3000 元、B 公司目標價 5922 元」，整理 {display_name} 時只能保留 B 公司明確對應的數字，不可混用 A 公司數字。
8. 若新聞片段出現記憶體、DRAM、HBM、伺服器、PCB、載板等產業詞，必須確認該產業詞在同一句或相鄰句明確連到「{stock_code} {display_name}」；不能把同篇文章中其他股票的產業題材寫成本股票重點。
9. 優先整理與「{stock_code} {display_name}」公司本身產業、基本面或股價可能受影響的消息，不要整理同篇文章中其他公司的題材。
10. 若產業詞、目標價、EPS、營收、ASP、毛利率或獲利預估沒有在同一句或相鄰句明確連到「{stock_code} {display_name}」，不要寫進重點。
11. 最多輸出 3 點，建議 2～3 點；整體至少 {NEWS_SUMMARY_MIN_TOTAL_CHARS} 個中文字，若只有 2 點，每點要更完整。
12. 若只有 2 個高品質重點且已達整體字數要求，可以只輸出 2 點；不要為了湊第 3 點而輸出關鍵字、標籤、追蹤文字或看不懂的摘要。
13. 圖片區塊不大，但新聞內容必須有資訊量；每點約 45～90 個中文字。
14. 若不同文章報同一件事，合併成一點，並寫出共同核心。
15. 請保留最關鍵的數字或事件，但不要塞滿數字。
16. 嚴禁輸出「關鍵字：」、「追蹤我們」、「分享給朋友」、「本文」、「標籤」或任何社群導流、SEO 關鍵字內容。
17. 嚴禁輸出「以下為您」、「根據提供資料」、「結合資料整理」這類 AI 助理語氣；每一點都必須直接像新聞摘要。
18. 嚴禁輸出問號、奇怪符號、圖表說明、圖片說明或「圖中顯示」這類文字。

請只回傳 JSON，不要 markdown，不要多餘說明。
格式：
{{
  "points": [
    "業績更新：第一點",
    "法人觀點：第二點"
  ],
  "note": "資料是否充足的簡短說明"
}}

以下是新聞素材 JSON（可能是原文，也可能是 Google News RSS 的標題 / 摘要 / URL）：
{article_json}
"""
    print("=" * 100)
    print("開始呼叫 Gemini 統整新聞重點")
    print(f"模型：{GEMINI_MODEL}")
    print(f"送入 Gemini 的文章數：{len(usable_articles)}")
    print("=" * 100)
    output_text = _call_gemini_with_retry(prompt, cache_task=_news_points_cache_task(), stock_code=stock_code, stock_name=stock_name)
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
        e["branch"] = e["branch"].map(normalize_branch_name).replace("", "未知分點")
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
9. 不要輸出「以下為您」、「根據提供資料」、「結合資料整理」這類 AI 助理語氣；每一點要直接像週報重點。
10. 不要輸出問號、奇怪符號、圖表說明、圖片說明或「圖中顯示」這類文字。

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
        output_text = _call_gemini_with_retry(prompt, cache_task="weekly_keypoints", stock_code=str(ctx.get("stock_code", "") or ""), stock_name=stock_name)
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
        "4. 每點要像財經新聞摘要，格式盡量為「短標籤：具體事件／數字／市場消息 + 對公司或產業的影響」，不要寫成空泛研究報告。\n"
        "5. 只聚焦公司本身可能影響股價的消息：公司產業、法人目標價/評等、EPS/每股純益、營收、毛利率、獲利、ASP/報價、接單出貨、產能與供需。\n"
        "6. 若目標價、EPS、營收、ASP、毛利率或產業題材沒有明確指向本公司，請不要使用。\n"
        "7. 若資料不足，寧可保守，不要臆測。\n\n"
        + "\n\n".join(blocks)
    )

    try:
        payload = {
            "model": NEWS_OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "你是台股財經新聞編輯，輸出繁體中文、重點清楚、像新聞摘要，避免空泛分析語氣。"},
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
        ("業績更新", ["營收", "月增", "年增", "業績", "財報", "獲利", "EPS", "毛利", "毛利率", "每股盈餘", "虧損", "轉盈"]),
        ("產業題材", ["AI", "伺服器", "記憶體", "DRAM", "NAND", "半導體", "報價", "HBM", "漲價", "缺貨", "先進封裝", "CoWoS", "ASIC", "散熱"]),
        ("公司動態", ["轉型", "布局", "擴產", "合作", "投資", "新產品", "法說", "展望", "接單", "出貨", "產能", "需求", "訂單", "客戶"]),
        ("法人觀點", ["外資", "投信", "券商", "法人", "評等", "目標價", "調升", "調降", "買進", "中立", "賣出", "大摩", "摩根士丹利", "高盛", "里昂"]),
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
            pick = _make_news_keypoint("新聞焦點", text, stock_code, stock_name)
            if pick and not _is_bad_news_sentence(pick):
                points.append(pick)
                used.add(text)

    return _ensure_news_summary_min_total(points, records, stock_code, stock_name)



def _load_gsheet_news_points_cache_for_display(stock_code: str, stock_name: str, allow_stale: bool = False) -> List[str]:
    """直接讀取 Google Sheet 的 news_points 快取，供新聞區塊顯示使用。

    原本快取只在 _call_gemini_with_retry() 內讀取；如果 Google News 沒抓到素材，
    流程會在 build_news_points() 提早 return，導致永遠不會讀到 Google Sheet 快取。
    這個函式放在 build_news_points() 前面直接查快取，確保當天跑過的新聞摘要能直接被圖片使用。
    """
    if not GSHEET_LLM_CACHE_ENABLE or not GSHEET_FALLBACK_ENABLE or LLM_CACHE_FORCE_REFRESH:
        return []
    stock_key = _clean_code(stock_code)
    if not stock_key:
        return []

    cached_text = load_gsheet_llm_cache(_news_points_cache_task(), stock_key, stock_name, prompt="")
    if cached_text:
        points = _clean_news_summary_points_for_stock(_parse_raw_points_from_llm(cached_text), stock_key, stock_name)
        if points:
            print(f"📦 直接使用 Google Sheet 當日新聞快取：{stock_key}｜{len(points)} 點")
            return points[:NEWS_DISPLAY_MAX_POINTS]

    if not allow_stale:
        return []

    try:
        df = read_gsheet_worksheet(GSHEET_LLM_CACHE_SHEET)
        if df is None or df.empty or "Gemini輸出" not in df.columns:
            return []

        work = df.copy().fillna("")
        task_candidates = [_news_points_cache_task()]
        if NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK:
            task_candidates.append("news_points")

        if "任務" in work.columns:
            work = work[work["任務"].astype(str).str.strip().isin(task_candidates)].copy()
        else:
            pattern = "|".join(re.escape(t) for t in task_candidates)
            work = work[work.get("快取鍵", "").astype(str).str.contains(pattern, na=False)].copy()

        if "標的股" in work.columns:
            work = work[work["標的股"].map(_clean_code).astype(str) == stock_key].copy()
        elif "快取鍵" in work.columns:
            work = work[work["快取鍵"].astype(str).str.contains(f"_{stock_key}_", na=False)].copy()

        if work.empty:
            return []

        if "模型" in work.columns:
            same_model = work[work["模型"].astype(str).str.strip() == GEMINI_MODEL].copy()
            if not same_model.empty:
                work = same_model

        sort_cols = [c for c in ["日期", "更新時間"] if c in work.columns]
        if sort_cols:
            work = work.sort_values(sort_cols)
        row = work.tail(1).iloc[0]
        cached_text = str(row.get("Gemini輸出", "") or "").strip()
        points = _clean_news_summary_points_for_stock(_parse_raw_points_from_llm(cached_text), stock_key, stock_name)
        if points:
            cache_date = str(row.get("日期", "") or "")
            print(f"📦 Google Sheet 舊新聞快取備援命中：{stock_key}｜{cache_date}｜{len(points)} 點")
            return points[:NEWS_DISPLAY_MAX_POINTS]
    except Exception as e:
        print(f"⚠️ Google Sheet 舊新聞快取讀取失敗：{stock_key}｜{e}")
    return []

def _build_no_news_fallback_point(stock_code: str, stock_name: str, ctx: dict | None = None) -> str:
    """新聞抓不到時的中性備援文字，避免圖片新聞區塊空白，也避免亂編新聞。"""
    display_name = str(stock_name or stock_code or "該股").strip()
    try:
        if ctx:
            total_net = float(ctx.get("total_net", 0) or 0)
            bias = str(ctx.get("bias", "") or "中性")
            if total_net != 0:
                return f"近期待追蹤：本次未抓到足夠明確的 {display_name} 公司新聞，先以權證資金流 {fmt_money(total_net)}（{bias}）、分點籌碼與技術面變化作為觀察重點。"
    except Exception:
        pass
    return f"近期待追蹤：本次未抓到足夠明確的 {display_name} 公司新聞，先以權證資金流、分點籌碼與技術面變化作為觀察重點。"


def build_news_points(stock_code: str, stock_name: str, news_items, ctx: dict | None = None) -> List[str]:
    """根據最近一週新聞內文整理重點；優先讀 Google Sheet 快取，再使用新聞原文整理。"""
    # 先讀 Google Sheet news_points 快取。
    # 這一步必須放在 records 判斷之前，否則 Google News 沒抓到素材時會提早 return，導致明明有快取也不會被使用。
    cached_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=False)
    if cached_points:
        return cached_points[:NEWS_DISPLAY_MAX_POINTS]

    records = _news_items_to_records(news_items)
    body_records = [
        r for r in records
        if r.get("body_ok") and len(_normalize_news_text(r.get("content", ""))) >= NEWS_MIN_BODY_CHARS
    ]
    fallback_records = [r for r in records if r.get("fallback_ok") and _normalize_news_text(r.get("content", ""))]

    if not records:
        stale_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=True)
        if stale_points:
            return stale_points[:NEWS_DISPLAY_MAX_POINTS]
        return [_build_no_news_fallback_point(stock_code, stock_name, ctx)]

    # 優先把「所有可用新聞」一次交給 Gemini 統整。
    # 這裡不要逐篇呼叫 Gemini；_summarize_news_with_gemini() 會將多篇原文 / RSS 摘要合併成同一份 article_json，
    # 並透過單一次 _call_gemini_with_retry(cache_task=_news_points_cache_task()) 產生最多 3 點新聞重點。
    ai_source_records = records
    ai_points = _summarize_news_with_gemini(ai_source_records, stock_code, stock_name)
    if ai_points:
        return _ensure_news_summary_min_total(ai_points, ai_source_records, stock_code, stock_name)[:NEWS_DISPLAY_MAX_POINTS]

    # Gemini 不可用或失敗時，仍優先從真正內文抽重點；最後才用 RSS 摘要作為備援素材。
    rule_source = body_records if body_records else fallback_records
    rule_points = _rule_based_news_summary(rule_source, stock_code, stock_name)
    if rule_points:
        return _ensure_news_summary_min_total(rule_points, rule_source, stock_code, stock_name)[:NEWS_DISPLAY_MAX_POINTS]

    stale_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=True)
    if stale_points:
        return stale_points[:NEWS_DISPLAY_MAX_POINTS]

    if not body_records:
        return [_build_no_news_fallback_point(stock_code, stock_name, ctx)]
    return [_build_no_news_fallback_point(stock_code, stock_name, ctx)]


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




def build_institutional_axis_df(plot_df: pd.DataFrame, stock_df: pd.DataFrame) -> pd.DataFrame:
    """建立三大法人圖專用日期軸。

    股價資料來源（yfinance）有時會比 FinMind 三大法人晚一天，原本三大法人圖完全綁定
    plot_df.index，因此即使 FinMind 已經更新最新一日，也會被股價日期軸排除。
    這裡會保留原本股價日期，再補上 stock_df.attrs["institutional_df"] 中較新的法人日期，
    讓三大法人資料能更新就先顯示。
    """
    if plot_df is None or plot_df.empty:
        return pd.DataFrame(columns=["foreign", "invest", "dealer", "total"])

    stock_dates = pd.to_datetime(plot_df.index, errors="coerce").dropna().normalize()
    if len(stock_dates) == 0:
        return pd.DataFrame(columns=["foreign", "invest", "dealer", "total"])

    min_date = pd.Timestamp(stock_dates.min()).normalize()
    dates = set(pd.Timestamp(d).normalize() for d in stock_dates)

    inst_raw = None
    try:
        inst_raw = stock_df.attrs.get("institutional_df")
    except Exception:
        inst_raw = None

    if inst_raw is not None and not inst_raw.empty:
        inst_tmp = inst_raw.copy()
        if "Date" in inst_tmp.columns:
            inst_tmp["Date"] = pd.to_datetime(inst_tmp["Date"], errors="coerce").dt.tz_localize(None).dt.normalize()
            inst_tmp = inst_tmp.dropna(subset=["Date"])
            for d in inst_tmp["Date"]:
                d = pd.Timestamp(d).normalize()
                if d >= min_date:
                    dates.add(d)

    axis_dates = sorted(dates)
    out = pd.DataFrame(index=pd.DatetimeIndex(axis_dates))

    base_cols = [c for c in ["foreign", "invest", "dealer", "total"] if c in plot_df.columns]
    if base_cols:
        base = plot_df[base_cols].copy()
        base.index = pd.to_datetime(base.index, errors="coerce").normalize()
        out = out.join(base, how="left")

    if inst_raw is not None and not inst_raw.empty:
        inst_tmp = inst_raw.copy()
        if "Date" in inst_tmp.columns:
            inst_tmp["Date"] = pd.to_datetime(inst_tmp["Date"], errors="coerce").dt.tz_localize(None).dt.normalize()
            inst_tmp = inst_tmp.dropna(subset=["Date"])
            inst_tmp = inst_tmp.set_index("Date").sort_index()
        else:
            inst_tmp.index = pd.to_datetime(inst_tmp.index, errors="coerce").normalize()
            inst_tmp = inst_tmp[~inst_tmp.index.isna()].sort_index()
        for c in ["foreign", "invest", "dealer", "total"]:
            if c in inst_tmp.columns:
                inst_tmp[c] = pd.to_numeric(inst_tmp[c], errors="coerce").fillna(0.0).astype(float)
        inst_cols = [c for c in ["foreign", "invest", "dealer", "total"] if c in inst_tmp.columns]
        if inst_cols:
            inst_tmp = inst_tmp[inst_cols]
            inst_tmp = inst_tmp[inst_tmp.index >= min_date]
            # 法人資料以 FinMind 原始日期為準覆蓋同日資料，並補上股價尚未更新但法人已更新的新日期。
            out.update(inst_tmp)
            missing_dates = [d for d in inst_tmp.index if d not in out.index]
            if missing_dates:
                out = pd.concat([out, inst_tmp.loc[missing_dates]], axis=0, sort=False)
                out = out[~out.index.duplicated(keep="last")].sort_index()

    for c in ["foreign", "invest", "dealer", "total"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).astype(float)

    return out[["foreign", "invest", "dealer", "total"]]


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

    # 權證資金流改用「股價日期 + 權證事件日期」合併日期軸。
    # 避免 yfinance 股價最新日尚未更新，但 MoneyDJ / Google Sheet 權證分點資料已經有今日資料時，
    # 今日權證資金流被 plot_df.index.max() 擋掉。
    warrant_flow_dates = build_flow_axis_dates(plot_df, plot_events)
    warrant_x = list(range(len(warrant_flow_dates)))
    warrant_date_labels = [pd.Timestamp(d).strftime("%m-%d") for d in warrant_flow_dates]
    daily_net = daily_warrant_net_from_dates(warrant_flow_dates, plot_events)

    # 三大法人圖允許使用 FinMind 已更新、但 yfinance 股價尚未更新的最新法人日期。
    inst_plot_df = build_institutional_axis_df(plot_df, stock_df)
    x_inst = list(range(len(inst_plot_df)))

    # 精選分點資金流改用「股價日期 + 精選分點權證事件日期」合併日期軸。
    # 避免 yfinance 股價最新日尚未更新，但 MoneyDJ / Google Sheet 權證分點資料已經有今日資料時，
    # 今日精選分點大買 / 大賣被 plot_df.index.max() 擋掉。
    selected_branch_events_all = filter_selected_branch_flow_events(warrant_events)
    selected_flow_dates = build_flow_axis_dates(plot_df, selected_branch_events_all)
    selected_x = list(range(len(selected_flow_dates)))
    selected_date_labels = [pd.Timestamp(d).strftime("%m-%d") for d in selected_flow_dates]
    if selected_flow_dates:
        selected_branch_events = filter_events_by_date_range(
            selected_branch_events_all,
            selected_flow_dates[0],
            selected_flow_dates[-1],
        )
        selected_week_dates = selected_flow_dates[-WEEK_TRADING_DAYS:]
        selected_week_start = selected_week_dates[0]
        selected_week_end = selected_week_dates[-1]
        selected_branch_week_events = filter_events_by_date_range(
            selected_branch_events,
            selected_week_start,
            selected_week_end,
        )
    else:
        selected_branch_events = pd.DataFrame()
        selected_branch_week_events = pd.DataFrame()
    selected_branch_daily_net = daily_warrant_net_from_dates(selected_flow_dates, selected_branch_events)

    # TOP5 買賣超使用 build_weekly_context 產生的 week_events；
    # week_events 已改用「股價日期 + 權證事件日期」的最新週區間，因此會納入今日已更新的權證分點資料。
    buy_top, sell_top = top_branch_tables(week_events, topn=5)
    compact_mode = is_compact_report_mode()
    if compact_mode:
        # 精簡模式不建立本週重點與新聞內容，避免不必要的新聞抓取 / Gemini 呼叫。
        key_points = []
        news_points = []
        fig = plt.figure(figsize=(28, 47.7), facecolor=BG)
        gs = GridSpec(8, 12, figure=fig,
                      height_ratios=[1.45, 2.05, 9.8, 2.45, 3.1, 5.0, 4.7, 9.55],
                      hspace=0.20, wspace=0.25)
    else:
        key_points = build_key_points(ctx, stock_name)
        news_points = build_news_points(stock_code, stock_name, news_items, ctx)
        fig = plt.figure(figsize=(28, 59), facecolor=BG)
        gs = GridSpec(9, 12, figure=fig,
                      height_ratios=[1.45, 2.05, 9.8, 2.45, 3.1, 5.0, 4.7, 9.55, 9.05],
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
    # 不再與 K 線共用 x 軸，避免法人資料已更新但股價資料尚未更新時被股價日期排除。
    inst_ax = fig.add_subplot(gs[4, :])
    style_ax(inst_ax, "三大法人買賣超")
    plot_institutional_stacked_bars(inst_ax, inst_plot_df, x_inst)
    adjust_institutional_ylim(inst_ax, inst_plot_df)
    draw_inst_header_like_legend(inst_ax, inst_plot_df)
    inst_ax.yaxis.tick_right()

    # Warrant daily net bars + cumulative line
    # 不再與 K 線共用 x 軸，讓今日已更新的權證事件可以先出現在資金流圖。
    wnet_ax = fig.add_subplot(gs[5, :])
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

    wnet_ax.bar(warrant_x, vals, color=[RED if v >= 0 else GREEN for v in vals], width=0.75, alpha=0.85)
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
    wnet_ax2.plot(warrant_x, cum_vals, color=BLUE, linewidth=2.1, alpha=0.95)
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

    # 精選分點資金流：只統計指定分點的權證買賣金額。
    # 不再與 K 線共用 x 軸，讓今日已更新的精選分點事件可以先出現在資金流圖。
    selected_wnet_ax = fig.add_subplot(gs[6, :])
    style_ax(selected_wnet_ax)
    selected_vals = selected_branch_daily_net["net_amount"].astype(float).values
    selected_cum_vals = np.cumsum(selected_vals)
    selected_latest_net = selected_vals[-1] if len(selected_vals) else 0.0
    selected_latest_cum = selected_cum_vals[-1] if len(selected_cum_vals) else 0.0
    selected_total_net = float(selected_branch_week_events["net_amount"].sum()) if selected_branch_week_events is not None and not selected_branch_week_events.empty else 0.0
    selected_latest_bar_color = RED if selected_latest_net >= 0 else GREEN
    selected_total_color = RED if selected_total_net >= 0 else GREEN

    xpos = 0.000
    xpos = draw_header_text_and_advance(
        selected_wnet_ax, xpos, "精選分點資金流", GOLD,
        fontsize=34, fontweight="bold", gap_px=22,
    )

    xpos = draw_header_text_and_advance(selected_wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_bar_and_advance(selected_wnet_ax, xpos, selected_latest_bar_color, gap_px=8)
    xpos = draw_header_text_and_advance(selected_wnet_ax, xpos, f"最新日 {fmt_money(selected_latest_net)}", selected_latest_bar_color, gap_px=22)

    xpos = draw_header_text_and_advance(selected_wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_line_and_advance(selected_wnet_ax, xpos, selected_total_color, gap_px=10)
    xpos = draw_header_text_and_advance(selected_wnet_ax, xpos, f"本週合計 {fmt_money(selected_total_net)}", selected_total_color, gap_px=22)

    xpos = draw_header_text_and_advance(selected_wnet_ax, xpos, "|", MUTED, fontsize=25, fontweight="bold", gap_px=14, alpha=0.82)
    xpos = draw_header_line_and_advance(selected_wnet_ax, xpos, BLUE, gap_px=10)
    draw_header_text_and_advance(selected_wnet_ax, xpos, f"累計 {fmt_money(selected_latest_cum)}", BLUE, gap_px=0)

    selected_branch_label = "、".join(_get_selected_branch_flow_list())
    if selected_branch_label:
        selected_wnet_ax.text(
            0.001, 0.985,
            f"分點：{selected_branch_label}",
            transform=selected_wnet_ax.transAxes,
            color=MUTED,
            fontsize=18,
            fontweight="bold",
            ha="left",
            va="top",
            alpha=0.92,
            clip_on=True,
            zorder=12,
            bbox=dict(facecolor=PANEL, edgecolor="none", boxstyle="round,pad=0.12", alpha=0.82),
        )

    selected_wnet_ax.bar(selected_x, selected_vals, color=[RED if v >= 0 else GREEN for v in selected_vals], width=0.75, alpha=0.85)
    selected_wnet_ax.axhline(0, color=MUTED, linestyle="--", linewidth=1)

    if len(selected_vals):
        svmin = min(float(np.nanmin(selected_vals)), 0.0)
        svmax = max(float(np.nanmax(selected_vals)), 0.0)
        svspan = max(svmax - svmin, 1.0)
        svpad = svspan * 0.15
        selected_wnet_ax.set_ylim(svmin - svpad, svmax + svpad)

    selected_wnet_ax.yaxis.set_major_formatter(FuncFormatter(money_tick))
    selected_wnet_ax.yaxis.tick_right()
    selected_wnet_ax.tick_params(axis="y", labelsize=22)
    selected_wnet_ax2 = selected_wnet_ax.twinx()
    selected_wnet_ax2.plot(selected_x, selected_cum_vals, color=BLUE, linewidth=2.1, alpha=0.95)

    if len(selected_cum_vals):
        scmax = max(float(np.nanmax(selected_cum_vals)), 0.0)
        scmin = min(float(np.nanmin(selected_cum_vals)), 0.0)
        sy1_min, sy1_max = selected_wnet_ax.get_ylim()
        selected_zero_frac = (0 - sy1_min) / (sy1_max - sy1_min)
        selected_zero_frac = min(max(selected_zero_frac, 0.05), 0.95)
        selected_upper_need = scmax / (1 - selected_zero_frac) if (1 - selected_zero_frac) > 0 else scmax
        selected_lower_need = abs(scmin) / selected_zero_frac if selected_zero_frac > 0 else abs(scmin)
        selected_scale = max(selected_upper_need, selected_lower_need, 1.0) * 1.12
        selected_wnet_ax2.set_ylim(-selected_zero_frac * selected_scale, (1 - selected_zero_frac) * selected_scale)

    selected_wnet_ax2.tick_params(colors=MUTED, labelsize=22)
    selected_wnet_ax2.yaxis.set_major_formatter(FuncFormatter(money_tick))
    for spine in selected_wnet_ax2.spines.values():
        spine.set_visible(False)
    selected_wnet_ax2.grid(False)

    if selected_branch_events.empty:
        selected_wnet_ax.text(
            0.5, 0.48,
            "70日內無精選分點權證買賣資料",
            transform=selected_wnet_ax.transAxes,
            color=MUTED,
            fontsize=27,
            ha="center",
            va="center",
            bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.28", alpha=0.92),
        )

    # TOP5 tables
    ax_top = fig.add_subplot(gs[7, :])
    ax_top.set_axis_off()
    ax_top.set_facecolor(BG)
    sections = [
        (0.02, "本週淨買超分點 TOP5", buy_top, RED),
        (0.52, "本週淨賣超分點 TOP5", sell_top, GREEN),
    ]
    for x0, title, df_top, side_color in sections:
        # TOP5 卡片：上緣位置維持，底部往下拓一點，讓內容與外框更有呼吸感。
        card_y = -0.045
        card_w = 0.48
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

    if not compact_mode:
        # Notes row
        ax_notes = fig.add_subplot(gs[8, :]); ax_notes.set_axis_off(); ax_notes.set_facecolor(BG)
        for x0, title in [(0.02, "本週重點"), (0.52, "本週新聞 / 題材觀察")]:
            note_y = 0.005
            note_w = 0.48
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

        draw_note_items(key_points[:4], 0.04, 0.02 + 0.57 - notes_right_padding, 0.775)
        draw_note_items(news_points[:NEWS_DISPLAY_MAX_POINTS], 0.54, 0.52 + 0.57 - notes_right_padding, 0.775)

    # x ticks
    interval = max(1, len(x) // 12)
    for ax in [candle_ax, vol_ax]:
        ax.set_xlim(-1, len(x))
    inst_ax.set_xlim(-1, len(x_inst))
    wnet_ax.set_xlim(-1, len(warrant_x))

    warrant_interval = max(1, len(warrant_x) // 12)
    wnet_ax.set_xticks(warrant_x[::warrant_interval])
    wnet_ax.set_xticklabels(
        [warrant_date_labels[i] for i in range(0, len(warrant_date_labels), warrant_interval)],
        rotation=30,
        ha="right",
        color=MUTED,
        fontsize=26,
    )

    selected_interval = max(1, len(selected_x) // 12)
    selected_wnet_ax.set_xlim(-1, len(selected_x))
    selected_wnet_ax.set_xticks(selected_x[::selected_interval])
    selected_wnet_ax.set_xticklabels(
        [selected_date_labels[i] for i in range(0, len(selected_date_labels), selected_interval)],
        rotation=30,
        ha="right",
        color=MUTED,
        fontsize=26,
    )
    for ax in [candle_ax, vol_ax, inst_ax, wnet_ax]:
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
        # 另外保留原始 FinMind 日期到 stock_df.attrs["institutional_df"]，讓三大法人圖可先顯示
        # 股價尚未更新、但 FinMind 已更新的最新法人買賣超。
        inst_df = fetch_inst_60d_from_x(stock_code, days=max(CHART_LOOKBACK + 10, 80))
        if inst_df is not None and not inst_df.empty:
            inst_df = inst_df.copy()
            inst_df["Date"] = pd.to_datetime(inst_df["Date"], errors="coerce").dt.tz_localize(None)
            inst_df = inst_df.dropna(subset=["Date"]).sort_values("Date")
            stock_df.attrs["institutional_df"] = inst_df.copy()
            latest_inst_date = inst_df["Date"].max()
            if pd.notna(latest_inst_date):
                print(f"🔎 三大法人資料最新日期：{pd.Timestamp(latest_inst_date).date()}")
            inst_join = inst_df.set_index("Date").sort_index()
            stock_df = stock_df.join(inst_join[["foreign", "invest", "dealer", "total"]], how="left")
            stock_df.attrs["institutional_df"] = inst_df.copy()
        for c in ["foreign", "invest", "dealer", "total"]:
            if c not in stock_df.columns:
                stock_df[c] = 0.0
        stock_df[["foreign", "invest", "dealer", "total"]] = stock_df[["foreign", "invest", "dealer", "total"]].fillna(0.0)

        plot_df = stock_df.tail(CHART_LOOKBACK)
        start_date = pd.Timestamp(plot_df.index.min()).normalize()
        stock_end_date = pd.Timestamp(plot_df.index.max()).normalize()
        taipei_today = get_taipei_today_ts()
        # 權證分點資料的更新時間可能比 yfinance 股價更快。
        # 因此抓權證資料時，結束日不能只用股價最新日，否則盤後會漏掉今日分點買賣超。
        end_date = max(stock_end_date, taipei_today)

        print(
            f"🚀 產生 {stock_code} {stock_name} 權證資金流週報，"
            f"股價最新日 {stock_end_date.date()}｜權證資料區間 {start_date.date()} ~ {end_date.date()}"
        )
        warrant_events = fetch_warrant_events_full_market(stock_code, stock_name, start_date=start_date, end_date=end_date)
        print(f"✅ 權證分點事件總筆數：{len(warrant_events):,}")
        if warrant_events is not None and not warrant_events.empty and "Date" in warrant_events.columns:
            latest_event_date = pd.to_datetime(warrant_events["Date"], errors="coerce").dropna().max()
            if pd.notna(latest_event_date):
                print(f"🔎 權證分點事件最新日期：{pd.Timestamp(latest_event_date).date()}")
            selected_debug = filter_selected_branch_flow_events(warrant_events)
            if selected_debug is not None and not selected_debug.empty:
                selected_debug = selected_debug.copy()
                selected_debug["Date"] = pd.to_datetime(selected_debug["Date"], errors="coerce").dt.normalize()
                selected_debug = selected_debug.dropna(subset=["Date"])
                if not selected_debug.empty:
                    latest_selected_date = selected_debug["Date"].max()
                    latest_selected = selected_debug[selected_debug["Date"] == latest_selected_date]
                    latest_selected_sum = float(pd.to_numeric(latest_selected["net_amount"], errors="coerce").fillna(0.0).sum())
                    print(f"🔎 精選分點最新日期：{pd.Timestamp(latest_selected_date).date()}｜合計 {fmt_money(latest_selected_sum)}")
        if warrant_events is not None and not warrant_events.empty:
            try:
                debug_ctx = build_weekly_context(stock_df, warrant_events, WEEK_TRADING_DAYS)
                debug_week_events = debug_ctx.get("week_events", pd.DataFrame())
                if debug_week_events is not None and not debug_week_events.empty:
                    debug_buy_top, debug_sell_top = top_branch_tables(debug_week_events, topn=5)
                    print(
                        f"🔎 TOP5統計區間：{pd.Timestamp(debug_ctx['week_start']).date()} ~ {pd.Timestamp(debug_ctx['week_end']).date()}｜"
                        f"週事件 {len(debug_week_events):,} 筆｜買超TOP5 {len(debug_buy_top):,} 筆｜賣超TOP5 {len(debug_sell_top):,} 筆"
                    )
            except Exception as e:
                print(f"⚠️ TOP5統計區間檢查失敗：{e}")
        if is_compact_report_mode():
            print("📄 精簡週報模式：略過本週重點、Google News 抓取與 Gemini 新聞統整")
            news_items = []
        else:
            cached_news_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=False)
            if cached_news_points:
                print(f"📦 今日新聞快取已存在，略過 Google News 抓取與 Gemini 新聞統整：{stock_code}｜{len(cached_news_points)} 點")
                news_items = []
            else:
                news_items = fetch_google_news_articles(stock_code, stock_name, max_items=NEWS_GOOGLE_MAX_ITEMS)

        fig = plot_weekly_report(stock_code, stock_name, stock_df, warrant_events, news_items)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=220, bbox_inches="tight", pad_inches=0.18, facecolor=fig.get_facecolor())
        plt.close(fig)

        # 模擬「截圖後輸出」：先保留原本高解析產圖，再等比例縮小並重新壓縮，降低檔案大小。
        buf = screenshot_like_output_buffer(buf)
        return buf
    except Exception as e:
        import traceback
        print(f"❌ 產生權證週報錯誤：{e}")
        traceback.print_exc()
        return None


def generate_k_chart(stock_code: str) -> io.BytesIO:
    """保留相容舊呼叫。"""
    return generate_warrant_report(stock_code)

# ============================================================
# GitHub Actions 手動執行入口
# ============================================================

def _send_discord_file(webhook_url: str, file_path: str, content: str = ""):
    if not webhook_url or not file_path or not os.path.exists(file_path):
        return
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "image/png")}
            data = {"content": content or os.path.basename(file_path)}
            resp = requests.post(webhook_url, data=data, files=files, timeout=(8, 40))
            resp.raise_for_status()
        print(f"✅ Discord 測試頻道已送出：{file_path}")
    except Exception as e:
        print(f"⚠️ Discord 測試頻道送出失敗：{e}")


def main():
    output_dir = os.getenv("OUTPUT_DIR", "output").strip() or "output"
    os.makedirs(output_dir, exist_ok=True)

    raw_codes = os.getenv("STOCK_CODES", "2408").strip() or "2408"
    stock_codes = [c.strip() for c in re.split(r"[,，\s]+", raw_codes) if c.strip()]
    if not stock_codes:
        stock_codes = ["2408"]

    print(f"📌 本次執行股票：{', '.join(stock_codes)}")
    print(f"📌 週報模式：{get_report_mode_label()}｜WARRANT_REPORT_MODE={REPORT_MODE_RAW}")
    print(f"📌 Gemini 開關：WARRANT_GEMINI_ENABLE={os.getenv('WARRANT_GEMINI_ENABLE', '')}")
    print(f"📌 Gemini API Key 組數：{len(_get_warrants_api_keys())}")
    print(f"📌 新聞開關：WARRANT_NEWS_ENABLE={os.getenv('WARRANT_NEWS_ENABLE', '')}")
    selected_branch_label = "、".join(_get_selected_branch_flow_list()) or "未設定"
    print(f"📌 精選分點資金流：{get_selected_branch_flow_mode_label()}｜{selected_branch_label}")
    print(f"📌 權證快照：enable={os.getenv('WARRANT_LOCAL_CACHE_ENABLE', '')}｜force_refresh={WARRANT_CACHE_FORCE_REFRESH}｜dir={LOCAL_WARRANT_CACHE_DIR}")

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "").strip()
    ok_count = 0
    for stock_code in stock_codes:
        buf = generate_warrant_report(stock_code)
        if buf is None:
            print(f"❌ {stock_code} 報告產生失敗")
            continue
        out_path = os.path.join(output_dir, f"{stock_code}_warrant_report.png")
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        ok_count += 1
        print(f"✅ 已輸出圖片：{out_path}")
        _send_discord_file(webhook_url, out_path, content=f"{stock_code} 權證資金流週報測試")

    if ok_count <= 0:
        raise SystemExit("沒有任何報告成功產生")


if __name__ == "__main__":
    main()
