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
from contextlib import contextmanager
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
# 純 Live 週報模式：圖片內容不讀取、不合併、不寫入 Google Sheet 或本機快取。
# 目的：避免權證分點、權證母體、股票名稱、勝率統計、LLM 快取等內容受到舊快取影響。
# 設為 1 時，本次週報只使用 yfinance / FinMind / TWSE / TPEx / MoneyDJ 等即時來源。
# 若未來需要恢復 Google Sheet 快取輔助，可設 WARRANT_REPORT_LIVE_ONLY=0。
REPORT_LIVE_ONLY = os.getenv("WARRANT_REPORT_LIVE_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
GSHEET_FALLBACK_ENABLE = (
    os.getenv("WARRANT_GSHEET_ENABLE", "1").strip().lower() not in ("0", "false", "no")
    and not REPORT_LIVE_ONLY
)
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
# 截圖式輸出設定：先用合理的中間解析度產圖，再等比例縮小後輸出，模擬「截圖後送出」以降低檔案大小。
SCREENSHOT_OUTPUT_ENABLE = os.getenv("WARRANT_SCREENSHOT_OUTPUT_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
SCREENSHOT_OUTPUT_SCALE = float(os.getenv("WARRANT_SCREENSHOT_OUTPUT_SCALE", "1.0"))
# 截圖式輸出改用最大寬度限制，讓實際效果更接近「螢幕截圖」；0 代表不限制。
SCREENSHOT_OUTPUT_MAX_WIDTH = int(os.getenv("WARRANT_SCREENSHOT_OUTPUT_MAX_WIDTH", "2400"))
SCREENSHOT_OUTPUT_FORMAT = os.getenv("WARRANT_SCREENSHOT_OUTPUT_FORMAT", "PNG").strip().upper() or "PNG"
SCREENSHOT_OUTPUT_JPEG_QUALITY = int(os.getenv("WARRANT_SCREENSHOT_OUTPUT_JPEG_QUALITY", "88"))
# PNG 仍是無損格式，長圖可能很大；轉成 256 色調色盤 PNG 可大幅縮檔，文字線條通常仍清楚。
SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE = os.getenv("WARRANT_SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")

# 執行時間與圖片輸出效能設定：
# 1. 原始週報會在 28 吋寬畫布上以 220 DPI 產生超大 PNG，之後又縮到 2400px 寬。
#    預設改成 130 DPI，在明顯降低中間圖像素的同時，保留較佳的細線與小字抗鋸齒。
# 2. 中間 PNG 只會立刻交給 Pillow 解碼與縮圖，不需要高壓縮；最終 PNG 再使用適度壓縮。
# 3. 所有效能參數都保留環境變數，可在不改程式的情況下恢復原設定。
REPORT_TIMING_ENABLE = os.getenv("WARRANT_REPORT_TIMING_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
REPORT_OUTPUT_DPI = int(os.getenv("WARRANT_REPORT_OUTPUT_DPI", "130"))
REPORT_INTERMEDIATE_PNG_COMPRESS_LEVEL = int(os.getenv("WARRANT_REPORT_INTERMEDIATE_PNG_COMPRESS_LEVEL", "1"))
SCREENSHOT_OUTPUT_PNG_COMPRESS_LEVEL = int(os.getenv("WARRANT_SCREENSHOT_OUTPUT_PNG_COMPRESS_LEVEL", "6"))
SCREENSHOT_OUTPUT_PNG_OPTIMIZE = os.getenv("WARRANT_SCREENSHOT_OUTPUT_PNG_OPTIMIZE", "0").strip().lower() in ("1", "true", "yes", "on")

# K 線圖 Y 軸留白設定：避免價格已經很集中時，上下空白仍過大。
# 調小後會讓股價區更貼近實際波動範圍，但仍保留少量空間給均線、布林與文字標註。
CANDLE_Y_PAD_LOWER_RATIO = float(os.getenv("WARRANT_CANDLE_Y_PAD_LOWER_RATIO", "0.06"))
CANDLE_Y_PAD_UPPER_RATIO = float(os.getenv("WARRANT_CANDLE_Y_PAD_UPPER_RATIO", "0.12"))
CANDLE_Y_PAD_LOWER_MIN_PCT = float(os.getenv("WARRANT_CANDLE_Y_PAD_LOWER_MIN_PCT", "0.008"))
CANDLE_Y_PAD_UPPER_MIN_PCT = float(os.getenv("WARRANT_CANDLE_Y_PAD_UPPER_MIN_PCT", "0.015"))

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

# 指定分點權證明細 Debug：
# 用來確認特定分點在近 N 天與本週區間內，到底有沒有被 warrant_events 抓進來。
# 正式執行預設關閉；需要檢查明細時可設 WARRANT_DEBUG_BRANCH_FLOW_ENABLE=1，分點預設跟隨精選分點。
DEBUG_BRANCH_WARRANT_FLOW_ENABLE = os.getenv("WARRANT_DEBUG_BRANCH_FLOW_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
DEBUG_BRANCH_WARRANT_FLOW_BRANCHES = os.getenv("WARRANT_DEBUG_BRANCH_FLOW_BRANCHES", SELECTED_BRANCH_FLOW_BRANCHES).strip()
DEBUG_BRANCH_WARRANT_FLOW_DAYS = int(os.getenv("WARRANT_DEBUG_BRANCH_FLOW_DAYS", "20"))
DEBUG_BRANCH_WARRANT_FLOW_MAX_ROWS = int(os.getenv("WARRANT_DEBUG_BRANCH_FLOW_MAX_ROWS", "300"))
DEBUG_BRANCH_WARRANT_FLOW_WARRANT_CODES = os.getenv("WARRANT_DEBUG_BRANCH_FLOW_WARRANT_CODES", "").strip()

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", os.getenv("GSHEET_NAME", "權證分點籌碼"))
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", os.getenv("GSHEET_ID", "")).strip()
GSHEET_STOCK_NAME_SHEET = os.getenv("WARRANT_STOCK_NAME_SHEET", "快取_股票名稱").strip() or "快取_股票名稱"
# 股票名稱快取只作為「代號 → 名稱」對照，不屬於交易資料快取。
# 因此即使 REPORT_LIVE_ONLY=1，也允許讀取這張表，避免官方名稱來源短暫失敗時，
# 後續新聞搜尋變成「未知公司」而把明確公司新聞全部過濾掉。
STOCK_NAME_GSHEET_ENABLE = os.getenv("WARRANT_STOCK_NAME_GSHEET_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
# 股票名稱優先使用本機小型 JSON 與官方公司基本資料；Google Sheet 改為背景預載與備援。
# 這只改變名稱取得順序，不改變交易資料、新聞篩選或權證統計邏輯。
STOCK_NAME_LOCAL_CACHE_ENABLE = os.getenv(
    "WARRANT_STOCK_NAME_LOCAL_CACHE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
STOCK_NAME_OFFICIAL_TIMEOUT = float(os.getenv("WARRANT_STOCK_NAME_OFFICIAL_TIMEOUT", "8"))
_STOCK_NAME_MAP_CACHE = None
_STOCK_NAME_MAP_CACHE_LOCK = threading.Lock()
_STOCK_NAME_LOCAL_MAP_CACHE = None
_STOCK_NAME_LOCAL_MAP_CACHE_LOCK = threading.Lock()
_OFFICIAL_STOCK_NAME_INFO_CACHE = None
_OFFICIAL_STOCK_NAME_INFO_CACHE_LOCK = threading.Lock()


# 分點勝率 / 歷史加權報酬率：供「本週重點」引用 Google Sheet 勝率統計。
# 可用環境變數 WARRANT_BRANCH_PERF_SHEETS 指定工作表名稱，若未指定，
# 會依序嘗試常見名稱，找不到時再掃描整份 Google Sheet 的工作表欄位。
BRANCH_PERF_ENABLE = os.getenv("WARRANT_BRANCH_PERF_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
BRANCH_PERF_SHEETS_RAW = os.getenv(
    "WARRANT_BRANCH_PERF_SHEETS",
    "勝率統計",
).strip() or "勝率統計"
BRANCH_PERF_MAX_MATCHES = int(os.getenv("WARRANT_BRANCH_PERF_MAX_MATCHES", "3"))
# 本週重點個別分點的最低金額代表性：以買賣超 TOP5 全體最大絕對金額為基準。
# 低於此比例的分點，即使歷史勝率很高，也不提供給 AI 作為可點名的個別分析候選。
WEEKLY_KEYPOINT_BRANCH_MIN_OVERALL_RATIO = float(
    os.getenv("WARRANT_WEEKLY_KEYPOINT_BRANCH_MIN_OVERALL_RATIO", "0.25")
)
# 三大法人本週合計接近中性時，不放大解讀為明顯多空或與權證資金分歧。
# 門檻取「至少固定張數」與「20日均量一定比例」兩者較大值。
WEEKLY_KEYPOINT_INST_NEUTRAL_MIN_SHARES = float(
    os.getenv("WARRANT_WEEKLY_KEYPOINT_INST_NEUTRAL_MIN_SHARES", "100")
)
WEEKLY_KEYPOINT_INST_NEUTRAL_MV20_RATIO = float(
    os.getenv("WARRANT_WEEKLY_KEYPOINT_INST_NEUTRAL_MV20_RATIO", "0.05")
)
_BRANCH_PERF_CACHE_DF = None
_BRANCH_PERF_CACHE_LOCK = threading.Lock()


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

# 歷史分點買賣明細補漏：
# OpenAPI / MoneyDJ Search 只能建立「目前可取得」的權證母體，若某檔權證近 90 天曾有分點買賣超，
# 但最新權證清單或 MoneyDJ Search 已經找不到，就會漏掉，例如 062599 這類已在明細表出現過的權證。
# 這裡會從既有「分點買賣明細」工作表補進：
# 1. 歷史權證代號，讓 API4 可以掃這檔權證。
# 2. 歷史 權證×分點 pair，若 API4 掃不到，也能直接交給 API5 回查近 110 天金額。
HISTORICAL_BRANCH_DETAIL_SUPPLEMENT_ENABLE = os.getenv("WARRANT_HISTORICAL_BRANCH_DETAIL_SUPPLEMENT_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS = int(os.getenv("WARRANT_HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS", "90"))
HISTORICAL_BRANCH_DETAIL_SHEETS_RAW = os.getenv(
    "WARRANT_HISTORICAL_BRANCH_DETAIL_SHEETS",
    "快取_近10日分點買賣明細,快取_近20日分點買賣明細,快取_近30日分點買賣明細,快取_近60日分點買賣明細,快取_近90日分點買賣明細,快取_權證分點買賣明細,快取_分點買賣明細",
).strip()
# 純 Live 模式下，若使用者明確知道某些仍有效但 MoneyDJ Search / OpenAPI 沒列出的權證，
# 可用環境變數補入代號，程式仍會透過 API4 / API5 即時回查，不讀 Google Sheet。
# 格式支援：062599 或 062599:晶技國票5B購01，多筆用逗號分隔。
EXTRA_LIVE_WARRANTS_RAW = os.getenv("WARRANT_EXTRA_LIVE_WARRANTS", os.getenv("WARRANT_EXTRA_WARRANT_CODES", "")).strip()
# 純 Live 指定 pair 補抓：
# 當 MoneyDJ Search 有權證，但 API4 沒有回傳某分點清單時，可直接把 權證×券商代號×分點 丟給 API5 回查。
# 格式支援：
#   062599:9A9g:永豐金內湖
#   062599:9A9g:永豐金內湖:晶技國票5B購01
# 多筆用逗號、分號或換行分隔。
EXTRA_LIVE_PAIRS_RAW = os.getenv("WARRANT_EXTRA_LIVE_PAIRS", "").strip()
# API4 空回應 / 指定權證補查：
# 預設啟用。若 API4 對某支權證回 empty，會用本次 API4 已知的精選分點券商代號，
# 自動補出該權證 × 精選分點 pair，再交給 API5 純 Live 回查。
AUTO_BACKFILL_SELECTED_BRANCH_PAIRS_ENABLE = os.getenv(
    "WARRANT_AUTO_BACKFILL_SELECTED_BRANCH_PAIRS_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
# 權證追蹤 Debug：預設追蹤本次問題權證 062599；可設空字串關閉，或用逗號追蹤多檔。
DEBUG_WARRANT_CODES_RAW = os.getenv("WARRANT_DEBUG_WARRANT_CODES", "").strip()
DEBUG_API5_ROWS_ENABLE = os.getenv("WARRANT_DEBUG_API5_ROWS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")

# 本機快照快取：預設關閉，避免 GitHub runner 本機快照蓋過 Google Sheet 快取。
LOCAL_WARRANT_CACHE_ENABLE = (
    os.getenv("WARRANT_LOCAL_CACHE_ENABLE", "0").strip().lower() not in ("0", "false", "no", "off")
    and not REPORT_LIVE_ONLY
)
LOCAL_WARRANT_CACHE_DIR = os.getenv("WARRANT_LOCAL_CACHE_DIR", "warrant_cache").strip() or "warrant_cache"
STOCK_NAME_LOCAL_CACHE_FILE = os.getenv(
    "WARRANT_STOCK_NAME_LOCAL_CACHE_FILE",
    os.path.join(LOCAL_WARRANT_CACHE_DIR, "stock_names.json"),
).strip() or os.path.join(LOCAL_WARRANT_CACHE_DIR, "stock_names.json")
LOCAL_WARRANT_CACHE_FORCE_REFRESH = WARRANT_CACHE_FORCE_REFRESH or REPORT_LIVE_ONLY

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
# 注意：純 Live 模式只禁止交易資料快取；Gemini 文字快取仍應依 ACTION 參數運作。
# 因此當 WARRANT_LLM_CACHE_FORCE_REFRESH=0 時，同股票同任務同一天會優先使用當日快取；
# 設為 1 時才會跳過快取並重新呼叫 Gemini。
LLM_CACHE_ENABLE = os.getenv("WARRANT_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
LLM_CACHE_DIR = os.getenv("WARRANT_LLM_CACHE_DIR", "llm_cache").strip() or "llm_cache"
# Gemini 結果寫回 Google Sheet：同股票同任務當天跑過一次，當天再跑直接讀快取，不再呼叫 Gemini。
GSHEET_LLM_CACHE_ENABLE = os.getenv("WARRANT_GSHEET_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GSHEET_LLM_CACHE_SHEET = os.getenv("WARRANT_GSHEET_LLM_CACHE_SHEET", "快取_Gemini結果").strip() or "快取_Gemini結果"
LLM_CACHE_FORCE_REFRESH = os.getenv("WARRANT_LLM_CACHE_FORCE_REFRESH", "0").strip().lower() in ("1", "true", "yes", "on")

_THREAD_LOCAL = threading.local()
_FETCH_STATS_LOCK = threading.Lock()
_FETCH_STATS = {}

# 同一場程式執行中共用 Google Sheet 授權 client 與 spreadsheet handle，
# 避免股票名稱、勝率統計、Gemini 快取等功能反覆重新授權與開啟同一份試算表。
_GSPREAD_CLIENT_CACHE = None
_GSHEET_HANDLE_CACHE = None
_GSHEET_CONNECTION_LOCK = threading.RLock()
_STOCK_NAME_GSHEET_PRELOAD_THREAD = None
_STOCK_NAME_GSHEET_PRELOAD_LOCK = threading.Lock()

# TWSE / TPEx 全市場權證 OpenAPI 在同一個 Python process 只下載一次。
# 多股票批次執行時，第二支股票開始直接重用記憶體資料；每次新 Actions run 仍會重新抓 Live。
_OPENAPI_WARRANT_DAILY_CACHE = {}
_OPENAPI_WARRANT_DAILY_CACHE_LOCK = threading.Lock()

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

# 週報結論標籤顏色：只使用三種，方便讀者理解，也符合台股紅漲綠跌習慣。
STATUS_BULL_COLOR = RED       # 紅色：偏多 / 買方有利 / 資金流入
STATUS_BEAR_COLOR = GREEN     # 綠色：偏弱 / 賣方有利 / 資金流出
STATUS_NEUTRAL_COLOR = GOLD   # 深藍色：中性 / 觀望 / 尚未確認方向，與標題主色一致


def _has_negative_target_price_or_rating(text: str) -> bool:
    """只在目標價 / 評等有明確負向語意時，才判定為負面。"""
    t = _normalize_news_text(str(text or ""))
    if not t:
        return False
    patterns = [
        r"(調降|下修|下調|降低).{0,8}(目標價|評等|EPS|獲利預估|財測)",
        r"(目標價|評等|EPS|獲利預估|財測).{0,8}(調降|下修|下調|降低)",
        r"(降評|評等調降|賣出評等|減碼評等|劣於大盤|中立以下|目標價低於現價|低於現價|下看)",
    ]
    return any(re.search(p, t) for p in patterns)


def _has_positive_target_price_or_rating(text: str) -> bool:
    """只在目標價 / 評等有明確正向語意時，才判定為正面；單獨出現「目標價」不算。"""
    t = _normalize_news_text(str(text or ""))
    if not t:
        return False
    if _has_negative_target_price_or_rating(t):
        return False
    patterns = [
        r"(調升|上修|上調|提高).{0,8}(目標價|評等|EPS|獲利預估|財測)",
        r"(目標價|評等|EPS|獲利預估|財測).{0,8}(調升|上修|上調|提高)",
        r"(升評|評等調升|買進評等|維持買進|重申買進|維持加碼|重申加碼|優於大盤)",
        r"(券商看好|法人看好|法說看好)",
        r"(上看|喊到|喊出)\s*\d",
    ]
    return any(re.search(p, t) for p in patterns)


def _has_neutral_target_price_or_rating(text: str) -> bool:
    """目標價 / 評等只有維持或不變時，維持中性，不染成紅色。"""
    t = _normalize_news_text(str(text or ""))
    if not t:
        return False
    if _has_positive_target_price_or_rating(t) or _has_negative_target_price_or_rating(t):
        return False
    patterns = [
        r"(維持|持平|不變).{0,8}(目標價|評等)",
        r"(目標價|評等).{0,8}(維持|持平|不變)",
        r"(維持中立|維持持有|中立評等|持有評等)",
    ]
    return any(re.search(p, t) for p in patterns)



def _report_branch_positive_color_allowed(text: str):
    """判斷「分點偏買 / 積極偏買」類結果是否允許顯示紅色。

    原則：
    1. 精選五分點出現積極買超 / 偏買，可顯示紅色。
    2. 非精選分點必須在勝率統計中事件數 > 50 且勝率 >= 80%，才可顯示紅色。
    3. 若文字是分點偏買語意但不符合上述條件，回傳 False，後續改用中性深藍色。
    4. 若文字不是分點偏買語意，回傳 None，不干涉一般正負面判斷。
    """
    raw = str(text or "")
    s = _normalize_news_text(raw)
    norm = normalize_branch_name(raw)
    if not s or not norm:
        return None

    positive_terms = [
        "積極偏買", "積極買", "偏買", "買超", "加碼", "買盤", "承接",
        "淨流入", "資金流入", "買方", "偏多",
    ]
    if not any(k in s for k in positive_terms):
        return None

    try:
        selected_branches = [normalize_branch_name(x) for x in _get_selected_branch_flow_list()]
    except Exception:
        selected_branches = []
    selected_branches = [b for b in selected_branches if b]

    # 精選五分點允許紅色。
    for branch in selected_branches:
        if branch and branch in norm:
            return True

    candidate_rows = []
    try:
        perf_df = read_gsheet_branch_perf_df(force_refresh=False)
        if perf_df is not None and not perf_df.empty:
            for _, r in perf_df.iterrows():
                branch_norm = normalize_branch_name(r.get("branch", "") or r.get("branch_display", ""))
                if branch_norm:
                    candidate_rows.append((branch_norm, r.to_dict()))
    except Exception:
        candidate_rows = []

    # 長分點名優先，避免短名稱誤配。
    for branch_norm, row in sorted(candidate_rows, key=lambda x: len(x[0]), reverse=True):
        if not branch_norm or branch_norm not in norm:
            continue
        event_count = _parse_number_like_value(row.get("event_count", np.nan))
        win_rate = _parse_percent_like_value(row.get("win_rate", np.nan), ratio_if_small=True)
        if np.isfinite(event_count) and np.isfinite(win_rate) and event_count > 50 and win_rate >= 80:
            return True
        return False

    # 文字有分點語意但沒有可驗證分點績效時，不用紅色，避免把一般分點買超誤標成高品質訊號。
    if "分點" in s or any(k in norm for k in ["元大", "富邦", "凱基", "國票", "群益", "第一金", "台新", "永豐", "華南", "新光", "兆豐", "統一"]):
        return False

    return None

def get_report_status_color(status_text: str) -> str:
    """依台股閱讀習慣回傳結論文字顏色。

    只讓「結果文字」上色，底下詳細說明維持原本 TEXT / MUTED 顏色。
    顏色規則固定三種：紅色=正面/偏多，綠色=負面/偏弱，深藍=中性。
    """
    s = _normalize_news_text(str(status_text or "")).strip()
    if not s:
        return STATUS_NEUTRAL_COLOR

    # 台股週報閱讀邏輯：營收「年增但月減」代表基本面仍有成長，
    # 圖卡結論先視為偏正向；月減只放在說明文字提醒。
    if "營收" in s and "年增" in s and "月減" in s:
        return STATUS_BULL_COLOR

    # 目標價 / 評等要看前後語意，不能因為單獨出現「目標價」就判紅色。
    if _has_negative_target_price_or_rating(s):
        return STATUS_BEAR_COLOR
    if _has_positive_target_price_or_rating(s):
        return STATUS_BULL_COLOR
    if _has_neutral_target_price_or_rating(s):
        return STATUS_NEUTRAL_COLOR

    # 業績、營收與營運動能的明確方向判斷。
    # 「第2季營收創13季新高」「營收動能維持強勁」等屬於正向結果，應顯示紅色；
    # 若同句含低於預期、轉弱或衰退等明確負向語意，則不套用正向顏色。
    performance_negative_terms = [
        "營收動能轉弱", "營收動能降溫", "營收動能疲弱", "營收成長放緩",
        "業績動能轉弱", "獲利動能轉弱", "營運動能轉弱", "低於預期",
        "不如預期", "年減", "衰退", "下滑",
    ]
    performance_positive_terms = [
        "營收創高", "營收創新高", "營收續創新高", "續創新高", "改寫新高",
        "營收動能維持強勁", "營收動能強勁", "營收動能延續", "營收動能續強",
        "營收維持強勁", "營收維持成長", "營收成長動能延續",
        "業績動能維持強勁", "業績動能延續", "獲利動能向上",
        "獲利動能維持強勁", "獲利動能延續", "營運動能向上",
        "營運動能維持強勁", "營運動能延續",
    ]
    performance_positive_pattern = bool(
        re.search(r"營收.{0,8}(?:創|續創|改寫).{0,8}(?:新高|同期高)", s)
        or re.search(r"創\d+季新高", s)
        or re.search(r"(?:營收|業績|獲利|營運)動能.{0,6}(?:向上|強勁|續強|延續)", s)
    )
    if (
        (any(k in s for k in performance_positive_terms) or performance_positive_pattern)
        and not any(k in s for k in performance_negative_terms)
    ):
        return STATUS_BULL_COLOR

    # 分點偏買 / 積極偏買的紅色標示要更嚴格：
    # 只有精選五分點，或勝率統計事件數 > 50 且勝率 >= 80% 的分點，才允許用紅色。
    branch_positive_allowed = _report_branch_positive_color_allowed(s)
    if branch_positive_allowed is True:
        return STATUS_BULL_COLOR
    if branch_positive_allowed is False:
        return STATUS_NEUTRAL_COLOR

    bull_keywords = [
        "偏多", "轉強", "強勢", "多頭", "多頭排列", "買方", "買超", "偏買", "積極偏買", "積極買", "買盤", "承接", "加碼",
        "資金流入", "淨流入", "站回", "突破", "支撐", "上修", "調升", "上調", "年增", "月增",
        "正向", "利多", "傳捷報", "捷報", "看好", "看旺", "樂觀", "受惠", "成長", "增長", "推升",
        "需求強勁", "需求延續", "需求強", "題材強", "訂單", "接單", "出貨增", "擴產", "新產能",
        "評等調升", "買進評等", "營運看旺", "展望正向", "獲利成長", "EPS上修", "毛利率改善",
        "AI散熱需求強勁", "AI散熱需求延續", "液冷需求", "營收表現偏正向", "公司動態偏正向", "市場預期上修",
        "營收創高", "營收創新高", "續創新高", "改寫新高", "營收動能強勁", "營收動能延續",
        "業績動能延續", "獲利動能向上", "營運動能向上",
    ]
    bear_keywords = [
        "偏弱", "轉弱", "弱勢", "賣壓", "賣方", "賣超", "調節", "偏賣", "減碼",
        "資金流出", "淨流出", "跌破", "失守", "壓力", "下修", "調降", "下調", "年減", "負向", "利空",
        "空頭", "空頭排列", "均線空頭", "均線空頭排列", "均線空頭排列延續",
        "死亡交叉", "均線死亡交叉", "死叉", "看壞", "保守", "衰退", "下滑", "減少", "出貨減", "需求疲弱", "需求降溫", "營收動能轉弱", "低於預期", "不如預期",
    ]

    # 先處理明確負面；但月減若伴隨年增，前面已視為偏正向。
    if any(k in s for k in bear_keywords) or ("月減" in s and "年增" not in s):
        # 若同一句同時有明確利多與負面字，除非是跌破/賣壓/調降這類強負面，否則讓正面主題優先。
        strong_bear = any(k in s for k in ["跌破", "失守", "賣壓", "資金流出", "淨流出", "調降", "下修", "利空", "年減", "空頭", "空頭排列", "均線空頭", "死亡交叉", "均線死亡交叉", "死叉"])
        if strong_bear or not any(k in s for k in bull_keywords):
            return STATUS_BEAR_COLOR
    if any(k in s for k in bull_keywords):
        return STATUS_BULL_COLOR
    return STATUS_NEUTRAL_COLOR

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


@contextmanager
def report_stage_timer(label: str):
    """量測單一週報階段耗時；關閉計時時不改變原本執行流程。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        if REPORT_TIMING_ENABLE:
            elapsed = time.perf_counter() - start
            print(f"⏱️ {label}：{elapsed:.2f} 秒")


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
    - WARRANT_SCREENSHOT_OUTPUT_SCALE=1.0：縮放倍率上限；預設由最大寬度 2400px 控制最終尺寸。
    - WARRANT_SCREENSHOT_OUTPUT_MAX_WIDTH=2400：輸出最大寬度，0 代表不限制。
    - WARRANT_SCREENSHOT_OUTPUT_FORMAT=PNG/JPEG：輸出格式。
    - WARRANT_SCREENSHOT_OUTPUT_JPEG_QUALITY=88：JPEG 品質。
    - WARRANT_SCREENSHOT_OUTPUT_PNG_PALETTE_ENABLE=1：PNG 轉 256 色調色盤以縮小檔案。
    - WARRANT_SCREENSHOT_OUTPUT_PNG_COMPRESS_LEVEL=6：最終 PNG 壓縮等級。
    - WARRANT_SCREENSHOT_OUTPUT_PNG_OPTIMIZE=0：是否啟用 Pillow 額外最佳化。
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
            save_img.save(
                out,
                format="PNG",
                optimize=SCREENSHOT_OUTPUT_PNG_OPTIMIZE,
                compress_level=max(0, min(9, int(SCREENSHOT_OUTPUT_PNG_COMPRESS_LEVEL))),
            )

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
    if not GSHEET_LLM_CACHE_ENABLE or LLM_CACHE_FORCE_REFRESH:
        return ""
    if not task or not stock_code:
        return ""
    key = _gsheet_llm_cache_key(task, stock_code)
    try:
        sh = _open_gsheet()
        if sh is None:
            return ""
        try:
            ws = sh.worksheet(GSHEET_LLM_CACHE_SHEET)
        except Exception:
            return ""
        headers = [str(x or "").strip() for x in ws.row_values(1)]
        if "快取鍵" not in headers or "Gemini輸出" not in headers:
            return ""

        # 只讀第一欄尋找最後一筆相同快取鍵，再讀取該列；
        # 避免每次命中檢查都下載整張包含長篇 Gemini 文字的工作表。
        key_values = ws.col_values(1)
        matched_row_numbers = [
            row_number
            for row_number, value in enumerate(key_values, start=1)
            if row_number > 1 and str(value or "").strip() == key
        ]
        if not matched_row_numbers:
            return ""

        row_values = ws.row_values(matched_row_numbers[-1])
        row = {
            headers[i]: row_values[i] if i < len(row_values) else ""
            for i in range(len(headers))
        }
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
    if not GSHEET_LLM_CACHE_ENABLE or not output_text:
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
        ws = _get_or_create_worksheet(
            sh,
            GSHEET_LLM_CACHE_SHEET,
            rows=300,
            cols=len(GSHEET_LLM_CACHE_HEADERS),
        )

        current_headers = [str(x or "").strip() for x in ws.row_values(1)]
        if current_headers != GSHEET_LLM_CACHE_HEADERS:
            ws.update([GSHEET_LLM_CACHE_HEADERS], value_input_option="RAW")

        row_values = [
            key,
            cache_date,
            str(task or ""),
            _clean_code(stock_code),
            str(stock_name or ""),
            GEMINI_MODEL,
            _llm_prompt_hash(prompt),
            str(output_text or ""),
            updated_at,
        ]
        ws.append_row(row_values, value_input_option="RAW")
        print(f"💾 Gemini 結果已追加至 Google Sheet 快取：{key}")
    except Exception as e:
        print(f"⚠️ Google Sheet Gemini 快取寫入失敗：{key}｜{e}")


# ============================================================
# 股價 / 指標
# ============================================================

def _load_local_stock_name_map(force_refresh: bool = False) -> Dict[str, str]:
    """讀取本機股票名稱 JSON；檔案不存在時回傳空表，不影響原有備援流程。"""
    global _STOCK_NAME_LOCAL_MAP_CACHE

    if not STOCK_NAME_LOCAL_CACHE_ENABLE:
        return {}

    with _STOCK_NAME_LOCAL_MAP_CACHE_LOCK:
        if _STOCK_NAME_LOCAL_MAP_CACHE is not None and not force_refresh:
            return dict(_STOCK_NAME_LOCAL_MAP_CACHE)

    lookup = {}
    path = str(STOCK_NAME_LOCAL_CACHE_FILE or "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw_map = payload.get("stocks", payload) if isinstance(payload, dict) else {}
            if isinstance(raw_map, dict):
                for raw_code, raw_name in raw_map.items():
                    code = _normalize_stock_name_code_key(raw_code)
                    name = str(raw_name or "").strip()
                    if code and name and name != "未知公司":
                        lookup[code] = name
            if lookup:
                print(f"📦 已讀取本機股票名稱對照：{path}｜{len(lookup):,} 筆")
        except Exception as e:
            print(f"⚠️ 本機股票名稱對照讀取失敗：{path}｜{e}")

    with _STOCK_NAME_LOCAL_MAP_CACHE_LOCK:
        _STOCK_NAME_LOCAL_MAP_CACHE = dict(lookup)
    return lookup


def _save_local_stock_name_entry(stock_code: str, stock_name: str):
    """將新查到的股票名稱寫入本機 JSON，供同次執行與有 Actions cache 的後續執行重用。"""
    global _STOCK_NAME_LOCAL_MAP_CACHE

    if not STOCK_NAME_LOCAL_CACHE_ENABLE:
        return
    code = _normalize_stock_name_code_key(stock_code)
    name = str(stock_name or "").strip()
    if not code or not name or name == "未知公司":
        return

    try:
        lookup = _load_local_stock_name_map(force_refresh=False)
        if lookup.get(code) == name:
            return
        lookup[code] = name
        path = str(STOCK_NAME_LOCAL_CACHE_FILE or "").strip()
        if not path:
            return
        _ensure_dir(os.path.dirname(path))
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks": dict(sorted(lookup.items())),
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        with _STOCK_NAME_LOCAL_MAP_CACHE_LOCK:
            _STOCK_NAME_LOCAL_MAP_CACHE = dict(lookup)
        print(f"💾 股票名稱已寫入本機對照：{code} {name}")
    except Exception as e:
        print(f"⚠️ 本機股票名稱對照寫入失敗：{code}｜{e}")


def _fetch_official_stock_name_source(source: dict) -> Dict[str, dict]:
    """抓取單一官方公司基本資料來源，回傳代號到名稱資訊。"""
    try:
        resp = get_thread_session().get(
            source["url"],
            headers={
                "User-Agent": HDR["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=(5, max(5.0, STOCK_NAME_OFFICIAL_TIMEOUT)),
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return {}

        lookup = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            code = _normalize_stock_name_code_key(_pick_row_value(row, source["code_keys"]))
            name = str(_pick_row_value(row, source["name_keys"]) or "").strip()
            if code and name and name != "未知公司":
                lookup[code] = {
                    "name": name,
                    "market": source["market"],
                    "source": source["label"],
                }
        return lookup
    except Exception as e:
        print(f"⚠️ {source['label']} 股票名稱對照抓取失敗：{e}")
        return {}


def read_official_stock_name_info_map(force_refresh: bool = False) -> Dict[str, dict]:
    """TWSE 與 TPEx 公司基本資料平行抓取，並在同次執行中共用結果。"""
    global _OFFICIAL_STOCK_NAME_INFO_CACHE

    with _OFFICIAL_STOCK_NAME_INFO_CACHE_LOCK:
        if _OFFICIAL_STOCK_NAME_INFO_CACHE is not None and not force_refresh:
            return {k: dict(v) for k, v in _OFFICIAL_STOCK_NAME_INFO_CACHE.items()}

    sources = [
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

    combined = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_fetch_official_stock_name_source, source) for source in sources]
        for future in futures:
            try:
                combined.update(future.result())
            except Exception as e:
                print(f"⚠️ 官方股票名稱對照工作失敗：{e}")

    with _OFFICIAL_STOCK_NAME_INFO_CACHE_LOCK:
        _OFFICIAL_STOCK_NAME_INFO_CACHE = {k: dict(v) for k, v in combined.items()}
    if combined:
        print(f"📦 官方股票名稱對照已載入並快取：{len(combined):,} 筆")
    return combined


def _preload_stock_name_gsheet_worker():
    """背景開啟 Google Sheet 並預載名稱表，讓網路等待與股價／權證流程重疊。"""
    try:
        read_gsheet_stock_name_map(force_refresh=False)
    except Exception as e:
        print(f"⚠️ Google Sheet 股票名稱背景預載失敗：{e}")


def _start_stock_name_gsheet_preload():
    global _STOCK_NAME_GSHEET_PRELOAD_THREAD

    if not STOCK_NAME_GSHEET_ENABLE:
        return
    with _STOCK_NAME_GSHEET_PRELOAD_LOCK:
        if _STOCK_NAME_MAP_CACHE is not None:
            return
        if _STOCK_NAME_GSHEET_PRELOAD_THREAD is not None and _STOCK_NAME_GSHEET_PRELOAD_THREAD.is_alive():
            return
        _STOCK_NAME_GSHEET_PRELOAD_THREAD = threading.Thread(
            target=_preload_stock_name_gsheet_worker,
            name="stock-name-gsheet-preload",
            daemon=True,
        )
        _STOCK_NAME_GSHEET_PRELOAD_THREAD.start()
        print("🚀 Google Sheet 股票名稱對照已在背景預載")


def _save_gsheet_stock_name_cache_async(stock_code: str, stock_name: str, market: str = "", source: str = ""):
    """維持原本名稱寫回功能，但不讓 Google Sheet 網路等待卡住主報告。"""
    if not STOCK_NAME_GSHEET_ENABLE:
        return

    def worker():
        try:
            code = _normalize_stock_name_code_key(stock_code)
            name = str(stock_name or "").strip()
            # 背景名稱表若已經有相同對照，就不再讀全表與寫回。
            existing = read_gsheet_stock_name_map(force_refresh=False)
            if code and str(existing.get(code, "") or "").strip() == name:
                return
            save_gsheet_stock_name_cache(stock_code, stock_name, market=market, source=source)
        except Exception as e:
            print(f"⚠️ 股票名稱背景寫回 Google Sheet 失敗：{stock_code}｜{e}")

    threading.Thread(
        target=worker,
        name=f"stock-name-save-{_normalize_stock_name_code_key(stock_code)}",
        daemon=True,
    ).start()


def get_tw_stock_name(stock_code: str) -> str:
    stock_code = _normalize_stock_name_code_key(stock_code)
    if not stock_code:
        return "未知公司"

    # 1) 本機名稱對照最快；若 GitHub Actions 有快取 warrant_cache，後續 run 可直接命中。
    try:
        local_name = str(_load_local_stock_name_map().get(stock_code, "") or "").strip()
        if local_name and local_name != "未知公司":
            print(f"✅ 股票名稱本機對照命中：{stock_code} {local_name}｜來源：{STOCK_NAME_LOCAL_CACHE_FILE}")
            _start_stock_name_gsheet_preload()
            return local_name
    except Exception as e:
        print(f"⚠️ 本機股票名稱對照查詢失敗：{stock_code}｜{e}")

    # Google Sheet 在背景預載，不再先阻塞股票名稱查詢。
    _start_stock_name_gsheet_preload()

    # 2) 上市與上櫃公司基本資料平行下載，通常比首次 Google Sheet 授權與讀全表更快。
    try:
        official_info = read_official_stock_name_info_map(force_refresh=False).get(stock_code, {})
        official_name = str(official_info.get("name", "") or "").strip()
        if official_name and official_name != "未知公司":
            market = str(official_info.get("market", "") or "")
            source = str(official_info.get("source", "") or "官方公司基本資料")
            print(f"✅ 股票名稱查詢成功：{stock_code} {official_name}｜來源：{source}")
            _save_local_stock_name_entry(stock_code, official_name)
            _save_gsheet_stock_name_cache_async(stock_code, official_name, market=market, source=source)
            return official_name
    except Exception as e:
        print(f"⚠️ 官方股票名稱對照查詢失敗：{stock_code}｜{e}")

    # 3) ETF / ETN 或官方基本資料暫時缺漏時，才等待 Google Sheet 名稱對照備援。
    if STOCK_NAME_GSHEET_ENABLE:
        try:
            name_map = read_gsheet_stock_name_map()
            cached_name = str(name_map.get(stock_code, "") or "").strip()
            if cached_name and cached_name != "未知公司":
                mode_note = "純 Live 名稱對照" if REPORT_LIVE_ONLY else "名稱快取"
                print(f"✅ 股票名稱{mode_note}命中：{stock_code} {cached_name}｜來源：{GSHEET_STOCK_NAME_SHEET}")
                _save_local_stock_name_entry(stock_code, cached_name)
                return cached_name
        except Exception as e:
            print(f"⚠️ Google Sheet 股票名稱對照讀取失敗：{stock_code}｜{e}")
    elif REPORT_LIVE_ONLY:
        print(f"🔴 純 Live 模式：股票名稱對照已關閉，改查官方即時資料源：{stock_code}")

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
                    _save_local_stock_name_entry(stock_code, name)
                    _save_gsheet_stock_name_cache_async(stock_code, name, market="上市", source="TWSE每日行情")
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
                        _save_local_stock_name_entry(stock_code, name)
                        _save_gsheet_stock_name_cache_async(stock_code, name, market="上櫃", source="TPEx每日行情")
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

def _build_gspread_client(force_refresh: bool = False):
    global _GSPREAD_CLIENT_CACHE

    with _GSHEET_CONNECTION_LOCK:
        if _GSPREAD_CLIENT_CACHE is not None and not force_refresh:
            return _GSPREAD_CLIENT_CACHE

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
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            _GSPREAD_CLIENT_CACHE = gspread.authorize(creds)
            print("♻️ Google Sheet client 已建立並快取")
            return _GSPREAD_CLIENT_CACHE
        except Exception as e:
            _GSPREAD_CLIENT_CACHE = None
            print(f"⚠️ GCP_SERVICE_KEY 解析失敗：{e}")
            return None


def _open_gsheet(force_refresh: bool = False):
    global _GSHEET_HANDLE_CACHE

    with _GSHEET_CONNECTION_LOCK:
        if _GSHEET_HANDLE_CACHE is not None and not force_refresh:
            return _GSHEET_HANDLE_CACHE

        gc = _build_gspread_client(force_refresh=force_refresh)
        if gc is None:
            return None

        try:
            _GSHEET_HANDLE_CACHE = (
                gc.open_by_key(GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID
                else gc.open(GOOGLE_SHEET_NAME)
            )
            print("♻️ Google Sheet 試算表連線已建立並快取")
            return _GSHEET_HANDLE_CACHE
        except Exception as e:
            _GSHEET_HANDLE_CACHE = None
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

BRANCH_PERF_BRANCH_COL_CANDIDATES = [
    "分點", "分點名稱", "券商分點", "券商名稱", "券商", "分公司", "分點別"
]
BRANCH_PERF_WIN_RATE_COL_CANDIDATES = [
    "勝率", "近10日勝率", "歷史勝率", "總勝率", "整體勝率", "A+B+C+D勝率", "ABCD勝率"
]
BRANCH_PERF_WEIGHTED_RETURN_COL_CANDIDATES = [
    "歷史加權報酬率", "加權報酬率", "歷史加權獲利率", "加權平均報酬率", "歷史報酬率", "平均加權報酬率"
]
BRANCH_PERF_EVENT_COUNT_COL_CANDIDATES = [
    "事件數", "總事件數", "樣本數", "總樣本數", "筆數", "總筆數", "交易次數"
]
BRANCH_PERF_AVG_HOLDING_DAYS_COL_CANDIDATES = [
    "平均持有天數", "平均持有日數", "平均持有天", "平均持有期間", "平均持有週期", "平均持有交易日"
]


def _split_env_csv(raw: str) -> List[str]:
    return [x.strip() for x in re.split(r"[,，;；\n\r]+", str(raw or "")) if str(x or "").strip()]


def _get_historical_branch_detail_sheet_names() -> List[str]:
    """取得用來補漏歷史權證 / 歷史權證×分點 pair 的分點買賣明細工作表清單。"""
    names = []
    if HISTORICAL_BRANCH_DETAIL_SHEETS_RAW:
        for item in re.split(r"[,，;；\n\r]+", HISTORICAL_BRANCH_DETAIL_SHEETS_RAW):
            item = str(item or "").strip()
            if item and item not in names:
                names.append(item)
    return names



def parse_extra_live_warrants(stock_code: str, stock_name: str) -> List[dict]:
    """解析手動指定的純 Live 補抓權證代號。

    這不是 Google Sheet 快取；只是把使用者指定的權證代號補進本次 live API4/API5 查詢母體。
    若不設定 WARRANT_EXTRA_LIVE_WARRANTS / WARRANT_EXTRA_WARRANT_CODES，預設不會新增任何權證。
    """
    raw = str(EXTRA_LIVE_WARRANTS_RAW or "").strip()
    if not raw:
        return []

    stock_code_clean = _clean_code(stock_code)
    out = []
    seen = set()
    for item in re.split(r"[,，;；\n\r]+", raw):
        item = str(item or "").strip()
        if not item:
            continue
        parts = [p.strip() for p in re.split(r"[:：|｜]", item) if p.strip()]
        code = normalize_openapi_warrant_code(parts[0] if parts else item)
        if not code or not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        name = parts[1] if len(parts) >= 2 else code
        seen.add(code)
        out.append({
            "代號": code,
            "名稱": name,
            "標的股": str(stock_code_clean),
            "標的名稱": stock_name,
            "成交金額": 0,
            "成交量": 0,
            "母體來源": "ExtraLiveWarrant",
        })

    if out:
        preview = "、".join([f"{x['代號']} {x['名稱']}" for x in out[:12]])
        if len(out) > 12:
            preview += "…"
        print(f"🔁 指定權證純 Live 補抓：{len(out):,} 支｜{preview}")
    return out


def get_debug_warrant_codes() -> set:
    """取得要追蹤的權證代號集合；預設不指定，避免跨股票時固定追蹤舊案例。"""
    raw = str(DEBUG_WARRANT_CODES_RAW or "").strip()
    codes = set()
    if raw:
        for item in re.split(r"[,，;；\n\r|｜]+", raw):
            code = normalize_openapi_warrant_code(str(item or "").strip())
            if code and re.fullmatch(r"\d{6}", code):
                codes.add(code)

    # 使用者若有指定純 Live 權證，也一併納入 debug。
    for item in re.split(r"[,，;；\n\r]+", str(EXTRA_LIVE_WARRANTS_RAW or "")):
        parts = [p.strip() for p in re.split(r"[:：|｜]", str(item or "")) if p.strip()]
        if parts:
            code = normalize_openapi_warrant_code(parts[0])
            if code and re.fullmatch(r"\d{6}", code):
                codes.add(code)

    # 使用者若有指定純 Live pair，也一併納入 debug。
    for item in re.split(r"[,，;；\n\r]+", str(EXTRA_LIVE_PAIRS_RAW or "")):
        parts = [p.strip() for p in re.split(r"[:：|｜]", str(item or "")) if p.strip()]
        if parts:
            code = normalize_openapi_warrant_code(parts[0])
            if code and re.fullmatch(r"\d{6}", code):
                codes.add(code)

    return codes


def parse_extra_live_pairs(stock_code: str, stock_name: str, warrant_lookup: dict | None = None) -> List[dict]:
    """解析手動指定的純 Live 補抓 pair。

    這不是 Google Sheet 快取；只是讓使用者在完全純 Live 模式下，
    直接指定「權證代號 × 券商代號 × 分點」，再由 API5 即時回查買賣金額。
    """
    raw = str(EXTRA_LIVE_PAIRS_RAW or "").strip()
    if not raw:
        return []

    warrant_lookup = warrant_lookup or {}
    stock_code_clean = _clean_code(stock_code)
    out = []
    seen = set()

    for item in re.split(r"[,，;；\n\r]+", raw):
        item = str(item or "").strip()
        if not item:
            continue

        parts = [p.strip() for p in re.split(r"[:：|｜]", item) if p.strip()]
        if len(parts) < 3:
            print(f"⚠️ 指定 pair 格式不足，略過：{item}｜格式：權證代號:券商代號:分點[:權證名稱]")
            continue

        code = normalize_openapi_warrant_code(parts[0])
        broker_code = str(parts[1] or "").strip()
        branch = normalize_branch_name(parts[2])
        if not code or not re.fullmatch(r"\d{6}", code) or not broker_code or not branch:
            print(f"⚠️ 指定 pair 欄位不完整，略過：{item}")
            continue

        key = (code, broker_code)
        if key in seen:
            continue
        seen.add(key)

        w = warrant_lookup.get(code, {}) if isinstance(warrant_lookup, dict) else {}
        warrant_name = parts[3] if len(parts) >= 4 else str(w.get("名稱", "") or w.get("warrant_name", "") or code).strip()
        out.append({
            "warrant_code": code,
            "warrant_name": warrant_name,
            "underlying_code": str(w.get("標的股", "") or stock_code_clean),
            "underlying_name": str(w.get("標的名稱", "") or stock_name),
            "broker_code": broker_code,
            "branch": branch,
            "pair_source": "ExtraLivePair",
        })

    if out:
        preview = "、".join([
            f"{p['warrant_code']}×{p['broker_code']}×{p['branch']}"
            for p in out[:12]
        ])
        if len(out) > 12:
            preview += "…"
        print(f"🔁 指定 權證×分點 純 Live pair 補抓：{len(out):,} 組｜{preview}")
    return out


def _build_selected_branch_broker_code_map(pair_values: List[dict]) -> dict:
    """從本次 API4 成功回傳的 pair 中推回分點對應券商代號。"""
    branch_to_brokers = {}
    for p in pair_values or []:
        branch = normalize_branch_name(p.get("branch", ""))
        broker_code = str(p.get("broker_code", "") or "").strip()
        if not branch or not broker_code:
            continue
        branch_to_brokers.setdefault(branch, set()).add(broker_code)

    out = {}
    for branch, brokers in branch_to_brokers.items():
        if len(brokers) == 1:
            out[branch] = sorted(brokers)[0]
        elif len(brokers) > 1:
            # 理論上同一分點應該只會對應一個券商代號；若有多個，保守取排序第一個並印出提醒。
            chosen = sorted(brokers)[0]
            out[branch] = chosen
            print(f"⚠️ 分點 {branch} 對應多個券商代號：{sorted(brokers)}，暫用 {chosen}")
    return out


def build_auto_selected_branch_backfill_pairs(
    warrants: List[dict],
    existing_pairs: List[dict],
    api4_status_by_code: dict,
    stock_code: str,
    stock_name: str,
) -> List[dict]:
    """替所有精選分點補齊「權證 × 分點」pair，讓 API5 直接回查歷史買賣金額。

    MoneyDJ API4 回傳的分點清單可能只包含最新日或目前頁面中的部分分點，
    不一定會列出近 N 天曾經交易過該權證的所有分點。
    因此這裡只要能從本次 API4 其他權證結果推回精選分點的券商代號，
    就會把所有本次權證母體中尚未存在的「權證 × 精選分點」pair 補進 API5。
    這樣使用者選哪個精選分點，就會針對那個分點做完整補查。
    """
    if not AUTO_BACKFILL_SELECTED_BRANCH_PAIRS_ENABLE:
        return []
    if not warrants:
        return []

    selected_branches = _get_selected_branch_flow_list()
    selected_branches = [normalize_branch_name(x) for x in selected_branches if normalize_branch_name(x)]
    if not selected_branches:
        return []

    branch_code_map = _build_selected_branch_broker_code_map(existing_pairs)
    missing_branch_codes = [b for b in selected_branches if b not in branch_code_map]
    if missing_branch_codes:
        print(f"⚠️ API4 pair 中找不到精選分點券商代號，無法自動補查：{', '.join(missing_branch_codes)}")

    existing_keys = {
        (normalize_openapi_warrant_code(p.get("warrant_code", "")), str(p.get("broker_code", "") or "").strip())
        for p in existing_pairs or []
    }

    out = []
    for w in warrants:
        code = normalize_openapi_warrant_code(w.get("代號", ""))
        if not code:
            continue

        for branch in selected_branches:
            broker_code = branch_code_map.get(branch, "")
            if not broker_code:
                continue

            key = (code, broker_code)
            if key in existing_keys:
                continue
            existing_keys.add(key)

            out.append({
                "warrant_code": code,
                "warrant_name": str(w.get("名稱", "") or code).strip(),
                "underlying_code": str(w.get("標的股", "") or _clean_code(stock_code)),
                "underlying_name": str(w.get("標的名稱", "") or stock_name),
                "broker_code": broker_code,
                "branch": branch,
                "pair_source": "AutoSelectedBranchBackfill",
            })

    if out:
        preview = "、".join([
            f"{p['warrant_code']}×{p['broker_code']}×{p['branch']}"
            for p in out[:12]
        ])
        if len(out) > 12:
            preview += "…"
        empty_count = sum(
            1
            for w in warrants
            if str(api4_status_by_code.get(normalize_openapi_warrant_code(w.get("代號", "")), "")) == "empty"
        )
        print(
            f"🔁 精選分點 pair 完整補查："
            f"精選分點 {len(selected_branches):,} 個｜API4 empty權證 {empty_count:,} 支｜"
            f"新增 {len(out):,} 組｜{preview}"
        )

    return out


def _normalize_perf_header(v) -> str:
    s = html.unescape(str(v or "")).strip()
    s = s.replace("臺", "台")
    s = re.sub(r"[\s　_\-－—–/\\|｜:：,，.。()（）\[\]【】{}｛｝]+", "", s)
    return s


def _find_header_idx(row: List[str], candidates: List[str], required_keywords: List[str] | None = None) -> int:
    normalized_row = [_normalize_perf_header(x) for x in row]
    normalized_candidates = [_normalize_perf_header(x) for x in candidates]
    for cand in normalized_candidates:
        if cand in normalized_row:
            return normalized_row.index(cand)
    if required_keywords:
        keys = [_normalize_perf_header(k) for k in required_keywords]
        for i, col in enumerate(normalized_row):
            if col and all(k in col for k in keys):
                return i
    return -1


def _parse_percent_like_value(v, ratio_if_small: bool = True) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        num = float(v)
        if ratio_if_small and np.isfinite(num) and abs(num) <= 1:
            num *= 100.0
        return num if np.isfinite(num) else np.nan
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "--", "nan", "None"):
        return np.nan
    has_pct = ("%" in s) or ("％" in s)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return np.nan
    num = float(m.group(0))
    if not has_pct and ratio_if_small and abs(num) <= 1:
        num *= 100.0
    return num


def _parse_number_like_value(v) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        num = float(v)
        return num if np.isfinite(num) else np.nan
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "--", "nan", "None"):
        return np.nan
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan


def _is_all_abcd_summary_row(event_type_value) -> bool:
    s = _normalize_perf_header(event_type_value).upper()
    if not s:
        return False
    # 勝率統計的每個分點都有 A/B/C/D 明細與「全部-A+B+C+D合併」總計列；
    # 週報只採總計列，避免同一分點被拆成多個事件類型。
    return ("全部" in s) and ("合併" in s) and all(letter in s for letter in ["A", "B", "C", "D"])


def _extract_branch_perf_values(values: List[List[str]], source_sheet: str) -> pd.DataFrame:
    """解析「勝率統計」的分段式表格。

    該工作表不是單一首列表頭，而是每個分點區塊都重複一次表頭，因此必須逐列掃描：
    1. 遇到包含「分點 / 事件類型 / 勝率 / 加權報酬率」的列時，更新欄位位置。
    2. 只讀取事件類型為「全部-A+B+C+D合併」的總計列。
    3. 取得分點、勝率、歷史加權報酬率與平均持有天數；事件數只作內部參考，不放進圖片。
    """
    rows = []
    header_map = None

    for raw_row in values or []:
        row = [str(x or "").strip() for x in list(raw_row)]
        if not any(row):
            continue

        branch_idx = _find_header_idx(row, BRANCH_PERF_BRANCH_COL_CANDIDATES, required_keywords=["分點"])
        event_type_idx = _find_header_idx(row, ["事件類型"], required_keywords=["事件", "類型"])
        win_idx = _find_header_idx(row, BRANCH_PERF_WIN_RATE_COL_CANDIDATES, required_keywords=["勝率"])
        weighted_idx = _find_header_idx(
            row,
            BRANCH_PERF_WEIGHTED_RETURN_COL_CANDIDATES,
            required_keywords=["加權", "報酬"],
        )
        event_count_idx = _find_header_idx(row, BRANCH_PERF_EVENT_COUNT_COL_CANDIDATES, required_keywords=["事件數"])
        avg_holding_days_idx = _find_header_idx(
            row,
            BRANCH_PERF_AVG_HOLDING_DAYS_COL_CANDIDATES,
            required_keywords=["平均", "持有"],
        )

        # 這是某個分點區塊的欄位標題列。
        if branch_idx >= 0 and event_type_idx >= 0 and win_idx >= 0:
            header_map = {
                "branch": branch_idx,
                "event_type": event_type_idx,
                "win_rate": win_idx,
                "weighted_return": weighted_idx,
                "event_count": event_count_idx,
                "avg_holding_days": avg_holding_days_idx,
            }
            continue

        if not header_map:
            continue

        max_needed = max(
            header_map["branch"],
            header_map["event_type"],
            header_map["win_rate"],
            header_map["weighted_return"] if header_map["weighted_return"] >= 0 else 0,
            header_map["event_count"] if header_map["event_count"] >= 0 else 0,
            header_map["avg_holding_days"] if header_map["avg_holding_days"] >= 0 else 0,
        )
        if len(row) <= max_needed:
            row += [""] * (max_needed + 1 - len(row))

        event_type = row[header_map["event_type"]]
        if not _is_all_abcd_summary_row(event_type):
            continue

        branch_raw = row[header_map["branch"]]
        branch = normalize_branch_name(branch_raw)
        if not branch:
            continue

        win_rate = _parse_percent_like_value(row[header_map["win_rate"]], ratio_if_small=True)
        weighted_return = (
            _parse_percent_like_value(row[header_map["weighted_return"]], ratio_if_small=True)
            if header_map["weighted_return"] >= 0
            else np.nan
        )
        event_count = (
            _parse_number_like_value(row[header_map["event_count"]])
            if header_map["event_count"] >= 0
            else np.nan
        )
        avg_holding_days = (
            _parse_number_like_value(row[header_map["avg_holding_days"]])
            if header_map["avg_holding_days"] >= 0
            else np.nan
        )

        # 本週重點要求同時顯示勝率與歷史加權報酬率；缺少任一值就不引用該分點。
        if not np.isfinite(win_rate) or not np.isfinite(weighted_return):
            continue

        rows.append({
            "branch": branch,
            "branch_display": branch_raw or branch,
            "win_rate": win_rate,
            "weighted_return": weighted_return,
            "event_count": event_count,
            "avg_holding_days": avg_holding_days,
            "source_sheet": source_sheet,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out["_event_sort"] = pd.to_numeric(out["event_count"], errors="coerce").fillna(-1)
    out = out.sort_values(["_event_sort"], ascending=[False])
    out = out.drop_duplicates(subset=["branch"], keep="first").reset_index(drop=True)
    return out.drop(columns=["_event_sort"])


def read_gsheet_branch_perf_df(force_refresh: bool = False) -> pd.DataFrame:
    global _BRANCH_PERF_CACHE_DF
    # 勝率統計只提供「分點歷史績效」作為文字輔助，不屬於本次交易資料快取；
    # 因此即使 REPORT_LIVE_ONLY=1，也允許讀取 Google Sheet 勝率統計。
    if not BRANCH_PERF_ENABLE:
        return pd.DataFrame()

    with _BRANCH_PERF_CACHE_LOCK:
        if _BRANCH_PERF_CACHE_DF is not None and not force_refresh:
            return _BRANCH_PERF_CACHE_DF.copy()

    sh = _open_gsheet()
    if sh is None:
        return pd.DataFrame()

    sheet_titles = _split_env_csv(BRANCH_PERF_SHEETS_RAW) or ["勝率統計"]
    for title in sheet_titles:
        try:
            ws = sh.worksheet(title)
            values = ws.get_all_values()
        except Exception as e:
            print(f"⚠️ Google Sheet 勝率統計讀取失敗：{title}｜{e}")
            continue

        result = _extract_branch_perf_values(values, ws.title)
        if result is not None and not result.empty:
            print(
                f"✅ 讀取分點勝率統計：{ws.title}｜"
                f"全部-A+B+C+D合併 {len(result):,} 個分點"
            )
            with _BRANCH_PERF_CACHE_LOCK:
                _BRANCH_PERF_CACHE_DF = result.copy()
            return result

        print(
            f"⚠️ 已找到工作表「{ws.title}」，但未讀到同時具備勝率與歷史加權報酬率的「全部-A+B+C+D合併」列。"
        )

    with _BRANCH_PERF_CACHE_LOCK:
        _BRANCH_PERF_CACHE_DF = pd.DataFrame()
    return pd.DataFrame()


def _describe_branch_holding_style(avg_holding_days) -> str:
    """依平均持有天數直接描述實際操作時間尺度，避免長週期分點仍拿隔日沖作比較。"""
    days = _parse_number_like_value(avg_holding_days)
    if not np.isfinite(days) or days <= 0:
        return ""
    if days <= 2.5:
        return "操作週期極短，接近隔日沖或快速進出"
    if days <= 5.0:
        return "操作週期偏短，較接近短線波段"
    if days <= 10.0:
        return "操作週期介於短線與中期之間"
    if days <= 20.0:
        return "操作週期偏向中期波段"
    return "持有週期較長，偏向中期至中長波段"


def _format_avg_holding_days(avg_holding_days) -> str:
    days = _parse_number_like_value(avg_holding_days)
    if not np.isfinite(days) or days <= 0:
        return "-"
    return f"{days:.1f}天"


def _build_weekly_branch_perf_matches(ctx: dict) -> List[dict]:
    """只保留本週金額具代表性的 TOP5 分點，再比對歷史勝率統計。

    代表性採買超與賣超 TOP5 的全體最大絕對金額為基準，不因為分點在賣超側排名靠前
    就自動視為重要。這可避免小額高勝率分點被拿來與主要大額分點對等比較。
    """
    cache_key = "_branch_perf_matches_cache"
    if cache_key in ctx:
        return list(ctx.get(cache_key) or [])

    week_events = ctx.get("week_events")
    perf_df = read_gsheet_branch_perf_df(force_refresh=False)
    if week_events is None or week_events.empty or perf_df is None or perf_df.empty:
        ctx[cache_key] = []
        return []

    perf_map = {
        str(r.get("branch", "") or ""): r
        for _, r in perf_df.iterrows()
        if str(r.get("branch", "") or "")
    }
    buy_top, sell_top = _get_cached_top_branch_tables(ctx, "current_week", week_events, topn=5)

    all_abs_amounts = []
    for df_top in [buy_top, sell_top]:
        if df_top is not None and not df_top.empty:
            all_abs_amounts.extend(
                pd.to_numeric(df_top["net_amount"], errors="coerce").fillna(0.0).abs().tolist()
            )
    overall_largest_abs = max(all_abs_amounts) if all_abs_amounts else 0.0
    min_ratio = max(0.0, float(WEEKLY_KEYPOINT_BRANCH_MIN_OVERALL_RATIO))
    matches = []

    def collect(df_top: pd.DataFrame, side: str):
        for rank, (_, r) in enumerate(df_top.iterrows(), 1):
            branch_display = str(r.get("branch", "") or "").strip()
            branch_norm = normalize_branch_name(branch_display)
            if not branch_norm:
                continue

            net_value = pd.to_numeric(r.get("net_amount", 0), errors="coerce")
            net_amount = 0.0 if pd.isna(net_value) else float(net_value)
            abs_amount = abs(net_amount)
            overall_ratio = abs_amount / overall_largest_abs if overall_largest_abs > 0 else 0.0

            # 金額不具代表性時，直接不進入個別歷史績效分析候選。
            if overall_ratio < min_ratio:
                continue

            perf_row = perf_map.get(branch_norm)
            if perf_row is None:
                continue

            win_rate = _parse_percent_like_value(perf_row.get("win_rate"), ratio_if_small=True)
            weighted_return = _parse_percent_like_value(perf_row.get("weighted_return"), ratio_if_small=True)
            event_count = _parse_number_like_value(perf_row.get("event_count"))
            avg_holding_days = _parse_number_like_value(perf_row.get("avg_holding_days"))
            if not np.isfinite(win_rate) or not np.isfinite(weighted_return):
                continue

            matches.append({
                "side": side,
                "rank": int(rank),
                "branch": branch_display or str(perf_row.get("branch_display", "") or branch_norm),
                "branch_norm": branch_norm,
                "weekly_net_amount": net_amount,
                "relative_to_overall_largest_pct": round(overall_ratio * 100, 2),
                "win_rate": win_rate,
                "weighted_return": weighted_return,
                "event_count": event_count,
                "avg_holding_days": avg_holding_days,
                "holding_style": _describe_branch_holding_style(avg_holding_days),
                "source_sheet": str(perf_row.get("source_sheet", "") or ""),
            })

    collect(buy_top, "buy")
    collect(sell_top, "sell")
    matches = sorted(
        matches,
        key=lambda x: (-abs(float(x.get("weekly_net_amount", 0) or 0)), int(x.get("rank", 999))),
    )
    if BRANCH_PERF_MAX_MATCHES > 0:
        matches = matches[:BRANCH_PERF_MAX_MATCHES]

    if matches:
        preview = "、".join(
            f"{m['branch']}({('買' if m['side'] == 'buy' else '賣')}TOP{m['rank']}，最大分點比{m['relative_to_overall_largest_pct']:.0f}%)"
            for m in matches
        )
        print(f"📈 金額具代表性且命中勝率統計：{preview}")

    ctx[cache_key] = matches
    return matches

def _format_branch_perf_focus_point(match: dict) -> str:
    side = str(match.get("side", "") or "")
    branch = str(match.get("branch", "") or "")
    net_amount = float(match.get("weekly_net_amount", 0) or 0)
    win_rate = match.get("win_rate", np.nan)
    weighted_return = match.get("weighted_return", np.nan)
    avg_holding_days = match.get("avg_holding_days", np.nan)
    holding_style = str(match.get("holding_style", "") or "")
    side_label = "買超" if side == "buy" else "賣超"
    rank = int(match.get("rank", 0) or 0)

    if side == "buy":
        ending = "本週買超與歷史績效同步偏多，可作為分點籌碼品質的輔助觀察。"
    else:
        ending = "具歷史績效的分點本週轉為調節，籌碼方向值得持續追蹤。"

    holding_text = ""
    parsed_holding_days = _parse_number_like_value(avg_holding_days)
    if np.isfinite(parsed_holding_days) and parsed_holding_days > 0:
        holding_text = f"，平均持有約 {_format_avg_holding_days(avg_holding_days)}"
        if holding_style:
            holding_text += f"，{holding_style}"

    return (
        f"{side_label}TOP{rank} 分點「{branch}」本週淨流向 {fmt_money(net_amount)}，"
        f"歷史勝率 {fmt_pct(win_rate)}、歷史加權報酬率 {fmt_pct(weighted_return)}"
        f"{holding_text}；{ending}"
    )


def _build_branch_perf_focus_points(ctx: dict, limit: int = 2) -> List[str]:
    points = []
    for match in _build_weekly_branch_perf_matches(ctx)[:max(0, int(limit or 0))]:
        pt = _format_branch_perf_focus_point(match)
        if pt and pt not in points:
            points.append(pt)
    return points


def _points_already_cover_branch_perf(points: List[str], ctx: dict) -> bool:
    if not points:
        return False
    matches = _build_weekly_branch_perf_matches(ctx)
    if not matches:
        return False
    merged = "\n".join([str(p or "") for p in points])
    if ("勝率" not in merged) or ("加權報酬率" not in merged):
        return False
    short_holding_matches = [
        m for m in matches
        if str(m.get("holding_style", "") or "")
    ]
    if short_holding_matches and not any(k in merged for k in ["持有", "隔日沖", "短線波段", "快速進出"]):
        return False
    for match in matches:
        branch = str(match.get("branch", "") or "")
        if branch and branch in merged:
            return True
    return False


def _ensure_branch_perf_point(points: List[str], ctx: dict) -> List[str]:
    points = list(points or [])
    if _points_already_cover_branch_perf(points, ctx):
        return points
    focus_points = _build_branch_perf_focus_points(ctx, limit=1)
    if not focus_points:
        return points
    focus = focus_points[0]
    merged = [focus] + [p for p in points if p != focus]
    return _clean_weekly_key_points(merged)


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

    if not STOCK_NAME_GSHEET_ENABLE:
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

    if not STOCK_NAME_GSHEET_ENABLE:
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
    if REPORT_LIVE_ONLY:
        return {}
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


def fetch_twse_openapi_warrant_daily_df(force_refresh: bool = False) -> pd.DataFrame:
    cache_key = "TWSE"
    with _OPENAPI_WARRANT_DAILY_CACHE_LOCK:
        cached = _OPENAPI_WARRANT_DAILY_CACHE.get(cache_key)
        if cached is not None and not force_refresh:
            print(f"♻️ 重用同次執行 TWSE 權證 OpenAPI：{len(cached):,} 筆")
            return cached.copy()

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
    with _OPENAPI_WARRANT_DAILY_CACHE_LOCK:
        _OPENAPI_WARRANT_DAILY_CACHE[cache_key] = out.copy()
    return out


def fetch_tpex_openapi_warrant_daily_df(force_refresh: bool = False) -> pd.DataFrame:
    cache_key = "TPEx"
    with _OPENAPI_WARRANT_DAILY_CACHE_LOCK:
        cached = _OPENAPI_WARRANT_DAILY_CACHE.get(cache_key)
        if cached is not None and not force_refresh:
            print(f"♻️ 重用同次執行 TPEx 權證 OpenAPI：{len(cached):,} 筆")
            return cached.copy()

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
    with _OPENAPI_WARRANT_DAILY_CACHE_LOCK:
        _OPENAPI_WARRANT_DAILY_CACHE[cache_key] = out.copy()
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
    if HISTORICAL_BRANCH_DETAIL_SUPPLEMENT_ENABLE:
        for detail_sheet in _get_historical_branch_detail_sheet_names():
            if detail_sheet not in sheet_names:
                sheet_names.append(detail_sheet)

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



def _historical_detail_effective_start(start_date=None, end_date=None):
    """取得歷史分點明細補漏的起始日期。"""
    starts = []
    if start_date is not None:
        try:
            starts.append(pd.Timestamp(start_date).normalize())
        except Exception:
            pass
    if end_date is not None and HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS > 0:
        try:
            starts.append(pd.Timestamp(end_date).normalize() - pd.Timedelta(days=int(HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS)))
        except Exception:
            pass
    if not starts:
        return None
    return max(starts)


def _row_date_in_historical_detail_range(row: dict, start_date=None, end_date=None) -> bool:
    """歷史分點買賣明細補漏用日期範圍。

    與一般權證母體快取不同，這裡預設額外限制近 HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS 日，
    避免太舊的明細把已經完全失效的權證大量塞進 API4/API5。
    """
    date_value = _pick_row_value(row, ["日期", "Date", "交易日期", "出表日期", "date", "trade_date"])
    if not str(date_value or "").strip():
        return False
    dt = parse_date(date_value)
    if dt is None:
        return False
    ts = pd.Timestamp(dt).normalize()
    effective_start = _historical_detail_effective_start(start_date=start_date, end_date=end_date)
    if effective_start is not None and ts < effective_start:
        return False
    if end_date is not None and ts > pd.Timestamp(end_date).normalize():
        return False
    return True


def _row_matches_stock_for_historical_detail(row: dict, stock_code: str, stock_name: str) -> bool:
    stock_code_clean = _clean_code(stock_code)
    aliases = make_stock_aliases(stock_name)
    aliases_norm = [_normalize_warrant_match_text(a) for a in aliases if _normalize_warrant_match_text(a)]

    ucode = _clean_code(_pick_row_value(row, [
        "標的股", "標的代號", "股票代號", "underlying_code", "UnderlyingCode",
    ]))
    uname = str(_pick_row_value(row, [
        "標的名稱", "股票名稱", "underlying_name", "UnderlyingName",
    ]) or "").strip()
    wname = str(_pick_row_value(row, [
        "權證名稱", "名稱", "warrant_name", "WarrantName",
    ]) or "").strip()

    if ucode and ucode == stock_code_clean:
        return True
    if uname and any(alias and alias in _normalize_warrant_match_text(uname) for alias in aliases_norm):
        return True
    name_front = _normalize_warrant_match_text(wname)[:16]
    if name_front and any(alias and alias in name_front for alias in aliases_norm):
        return True
    return False


def _row_has_historical_detail_amount(row: dict) -> bool:
    amount_keys = [
        "買進金額", "賣出金額", "買超金額", "淨買超金額", "淨額", "成交金額",
        "buy_amount", "sell_amount", "net_amount",
        "買進張數", "賣出張數", "賣出股數", "買進股數",
    ]
    for key in amount_keys:
        if key in row and abs(clean_openapi_number(row.get(key))) > 0:
            return True
    return False


def _build_historical_detail_pair_record(row: dict, stock_code: str, stock_name: str, source_sheet: str) -> dict:
    code = normalize_openapi_warrant_code(_pick_row_value(row, [
        "權證代號", "代號", "warrant_code", "WarrantCode",
    ]))
    if not code or not re.fullmatch(r"\d{6}", code):
        return {}

    name = str(_pick_row_value(row, [
        "權證名稱", "名稱", "warrant_name", "WarrantName",
    ]) or "").strip()
    if not _is_call_warrant_name(name):
        return {}

    broker_code = str(_pick_row_value(row, [
        "券商代號", "broker_code", "BrokerCode", "分點代號",
    ]) or "").strip()
    branch = normalize_branch_name(_pick_row_value(row, [
        "分點", "分點名稱", "券商分點", "branch", "broker_name", "BrokerName",
    ]))

    if not broker_code or not branch:
        return {}

    ucode = _clean_code(_pick_row_value(row, [
        "標的股", "標的代號", "股票代號", "underlying_code", "UnderlyingCode",
    ])) or _clean_code(stock_code)
    uname = str(_pick_row_value(row, [
        "標的名稱", "股票名稱", "underlying_name", "UnderlyingName",
    ]) or stock_name).strip()

    return {
        "warrant_code": code,
        "warrant_name": name or code,
        "underlying_code": ucode or _clean_code(stock_code),
        "underlying_name": uname or stock_name,
        "broker_code": broker_code,
        "branch": branch,
        "pair_source": source_sheet,
    }


def load_historical_branch_detail_pairs_from_cache(stock_code: str, stock_name: str, start_date=None, end_date=None) -> List[dict]:
    """從近 90 天分點買賣明細補進歷史 權證×分點 pair。

    目的：如果某檔權證近 90 天有分點買賣超，但 OpenAPI / MoneyDJ Search 最新母體沒列入，
    或 API4 掃不到該檔已知分點，仍可直接把「權證代號 × 券商代號 × 分點」交給 API5 回查，
    避免 062599 這類有實際買賣超的權證漏出週報統計。
    """
    if not HISTORICAL_BRANCH_DETAIL_SUPPLEMENT_ENABLE or not GSHEET_FALLBACK_ENABLE:
        return []

    pair_map = {}
    sheet_names = _get_historical_branch_detail_sheet_names()
    if not sheet_names:
        return []

    total_hit_rows = 0
    sheet_summaries = []

    for sheet_name in sheet_names:
        df = read_gsheet_worksheet(sheet_name)
        if df is None or df.empty:
            continue

        sheet_hit_rows = 0
        sheet_pair_added = 0
        for _, r in df.iterrows():
            row = r.to_dict()
            if not _row_date_in_historical_detail_range(row, start_date=start_date, end_date=end_date):
                continue
            if not _row_matches_stock_for_historical_detail(row, stock_code, stock_name):
                continue
            if not _row_has_historical_detail_amount(row):
                continue

            pair = _build_historical_detail_pair_record(row, stock_code, stock_name, sheet_name)
            if not pair:
                continue

            key = (pair["warrant_code"], pair["broker_code"])
            if key not in pair_map:
                pair_map[key] = pair
                sheet_pair_added += 1
            sheet_hit_rows += 1

        if sheet_hit_rows > 0:
            total_hit_rows += sheet_hit_rows
            sheet_summaries.append(f"{sheet_name} {sheet_hit_rows:,}列/{sheet_pair_added:,}組")

    pairs = list(pair_map.values())
    if pairs:
        preview_codes = sorted({p.get("warrant_code", "") for p in pairs if p.get("warrant_code")})[:12]
        preview = "、".join(preview_codes)
        if len(preview_codes) < len({p.get("warrant_code", "") for p in pairs if p.get("warrant_code")}):
            preview += "…"
        print(
            f"☁️ 歷史分點買賣明細補 pair：近 {HISTORICAL_BRANCH_DETAIL_LOOKBACK_DAYS} 日｜"
            f"命中 {total_hit_rows:,} 列｜新增候選 {len(pairs):,} 組 權證×分點｜權證 {preview}"
        )
        if sheet_summaries:
            print(f"   來源：{'；'.join(sheet_summaries[:8])}")
    return pairs


def merge_pair_lists(primary_pairs: List[dict], supplement_pairs: List[dict]) -> List[dict]:
    """合併 API4 掃出的 pair 與歷史明細補進的 pair。"""
    merged = {}
    for p in list(primary_pairs or []) + list(supplement_pairs or []):
        code = normalize_openapi_warrant_code(p.get("warrant_code", ""))
        broker_code = str(p.get("broker_code", "") or "").strip()
        if not code or not broker_code:
            continue
        p = dict(p)
        p["warrant_code"] = code
        p["branch"] = normalize_branch_name(p.get("branch", ""))
        key = (code, broker_code)
        if key not in merged:
            merged[key] = p
        else:
            # 優先保留 API4 的權證 / 分點名稱；若原資料缺漏，再用補充 pair 補上。
            old = merged[key]
            for col in ["warrant_name", "underlying_code", "underlying_name", "branch"]:
                if not str(old.get(col, "") or "").strip() and str(p.get(col, "") or "").strip():
                    old[col] = p.get(col, "")
    return list(merged.values())



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

    def fetch_one_suffix(suffix: str):
        target = f"{stock_code_clean}.{suffix}"
        raw = fetch_moneydj_warrant_search_raw(target)
        rows = parse_moneydj_warrant_search_rows(raw, stock_code_clean, target, stock_name=stock_name)
        return rows

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch_one_suffix, suffix) for suffix in ["TW", "TWO"]]
        for future in futures:
            try:
                rows = future.result()
            except Exception as e:
                print(f"⚠️ MoneyDJ 權證搜尋平行工作失敗：{stock_code_clean}｜{e}")
                rows = []
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
    # TWSE、TPEx 與 MoneyDJ Search 彼此沒有資料相依，平行抓取可縮短母體建立等待。
    prefetched_moneydj_warrants = None
    with ThreadPoolExecutor(max_workers=3) as executor:
        twse_future = executor.submit(fetch_twse_openapi_warrant_daily_df)
        tpex_future = executor.submit(fetch_tpex_openapi_warrant_daily_df)
        moneydj_future = (
            executor.submit(fetch_moneydj_call_warrants_fallback, stock_code, stock_name)
            if MONEYDJ_WARRANT_SEARCH_SUPPLEMENT_ENABLE
            else None
        )
        try:
            twse_df = twse_future.result()
        except Exception as e:
            print(f"⚠️ TWSE 權證 OpenAPI 平行工作失敗：{e}")
            twse_df = pd.DataFrame()
        try:
            tpex_df = tpex_future.result()
        except Exception as e:
            print(f"⚠️ TPEx 權證 OpenAPI 平行工作失敗：{e}")
            tpex_df = pd.DataFrame()
        if moneydj_future is not None:
            try:
                prefetched_moneydj_warrants = moneydj_future.result()
            except Exception as e:
                print(f"⚠️ MoneyDJ 權證搜尋平行工作失敗：{stock_code}｜{e}")
                prefetched_moneydj_warrants = []

    frames = []
    for source_label, f in [("TWSE", twse_df), ("TPEx", tpex_df)]:
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

        moneydj_warrants = (
            list(prefetched_moneydj_warrants or [])
            if prefetched_moneydj_warrants is not None
            else fetch_moneydj_call_warrants_fallback(stock_code_clean, stock_name)
        )
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

    extra_live_added = 0
    for rec in parse_extra_live_warrants(stock_code_clean, stock_name):
        code = normalize_openapi_warrant_code(rec.get("代號"))
        if not code or code in seen:
            continue
        seen.add(code)
        extra_live_added += 1
        warrants.append({
            "代號": code,
            "名稱": str(rec.get("名稱", "") or code).strip(),
            "標的股": str(stock_code_clean),
            "標的名稱": str(rec.get("標的名稱", "") or stock_name).strip(),
            "成交金額": int(rec.get("成交金額", 0) or 0),
            "成交量": int(rec.get("成交量", 0) or 0),
            "母體來源": "ExtraLiveWarrant",
        })

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

    debug_codes = get_debug_warrant_codes()
    if debug_codes:
        warrant_map_for_debug = {normalize_openapi_warrant_code(w.get("代號", "")): w for w in warrants}
        for debug_code in sorted(debug_codes):
            if debug_code in warrant_map_for_debug:
                print(f"✅ Debug 權證母體包含 {debug_code}：{warrant_map_for_debug[debug_code]}")
            else:
                print(f"⚠️ Debug 權證母體不包含 {debug_code}，後續 API4/API5 不會查到這檔")

    print(
        f"🔎 {stock_code_clean} {stock_name} 權證比對："
        f"lookup命中 {lookup_match_count:,} 支｜名稱命中 {name_match_count:,} 支｜"
        f"MoneyDJ補漏/備援新增 {moneydj_added:,} 支｜指定Live新增 {extra_live_added:,} 支｜歷史補充新增 {historical_added:,} 支"
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
    api4_status_by_code = {}
    api4_rows_count_by_code = {}
    api4_error_by_code = {}
    debug_codes = get_debug_warrant_codes()
    warrant_lookup = {
        normalize_openapi_warrant_code(w.get("代號", "")): w
        for w in warrants or []
        if normalize_openapi_warrant_code(w.get("代號", ""))
    }

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

    def remember_api4_result(result):
        w = result.get("warrant", {})
        code = normalize_openapi_warrant_code(w.get("代號", ""))
        if not code:
            return
        api4_status_by_code[code] = str(result.get("status", "") or "")
        api4_rows_count_by_code[code] = len(result.get("rows", []) or [])
        api4_error_by_code[code] = str(result.get("error", "") or "")

    def print_api4_debug(result, stage: str = "第一輪"):
        w = result.get("warrant", {})
        code = normalize_openapi_warrant_code(w.get("代號", ""))
        if code not in debug_codes:
            return

        rows = result.get("rows", []) or []
        status = result.get("status", "")
        error = result.get("error", "")
        print("========== API4 指定權證 Debug ==========")
        print(f"權證：{code}｜名稱：{w.get('名稱', '')}｜來源：{w.get('母體來源', '')}｜階段：{stage}")
        print(f"API4 status={status}｜rows={len(rows):,}｜error={error}")
        if rows:
            for row in rows[:20]:
                print(row)
            if len(rows) > 20:
                print(f"... API4 rows 尚有 {len(rows) - 20:,} 筆未列印")
        print("========== API4 指定權證 Debug 結束 ==========")

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
                remember_api4_result(result)
                print_api4_debug(result, stage="第一輪")
                if result.get("status") == "failed":
                    failed_results.append(result)
                else:
                    record_final_result(result, second_pass=False)
                    for rec in rows_to_pair_records(result.get("warrant", {}), result.get("rows", [])):
                        pairs[(rec["warrant_code"], rec["broker_code"])] = rec
            except Exception as e:
                code = normalize_openapi_warrant_code(w.get("代號", ""))
                if code:
                    api4_status_by_code[code] = "failed"
                    api4_rows_count_by_code[code] = 0
                    api4_error_by_code[code] = f"future {w.get('代號', '')}｜{e}"
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
                    remember_api4_result(result)
                    print_api4_debug(result, stage="第二輪")
                    if result.get("status") == "failed":
                        second_failed.append(result)
                    record_final_result(result, second_pass=True)
                    if result.get("status") != "failed":
                        for rec in rows_to_pair_records(result.get("warrant", {}), result.get("rows", [])):
                            pairs[(rec["warrant_code"], rec["broker_code"])] = rec
                except Exception as e:
                    code = normalize_openapi_warrant_code(w.get("代號", ""))
                    if code:
                        api4_status_by_code[code] = "failed"
                        api4_rows_count_by_code[code] = 0
                        api4_error_by_code[code] = f"second future {w.get('代號', '')}｜{e}"
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
            remember_api4_result(result)
            record_final_result(result, second_pass=False)

    if debug_codes:
        for debug_code in sorted(debug_codes):
            if debug_code in warrant_lookup:
                status = api4_status_by_code.get(debug_code, "未執行")
                rows_count = api4_rows_count_by_code.get(debug_code, 0)
                err = api4_error_by_code.get(debug_code, "")
                print(f"🔎 API4 指定權證總結：{debug_code}｜status={status}｜rows={rows_count:,}｜error={err}")
            else:
                print(f"⚠️ API4 指定權證總結：{debug_code} 不在本次權證母體，所以 API4 未執行")

    # 完全純 Live 的 pair 補查：
    # 1. 使用者手動指定的 權證×券商代號×分點。
    # 2. API4 對某權證回 empty，或指定 debug 權證時，自動用本次已知精選分點券商代號補 pair。
    pair_values_before_backfill = list(pairs.values())
    extra_pairs = parse_extra_live_pairs(
        warrants[0].get("標的股", "") if warrants else "",
        warrants[0].get("標的名稱", "") if warrants else "",
        warrant_lookup=warrant_lookup,
    )
    auto_pairs = build_auto_selected_branch_backfill_pairs(
        warrants,
        pair_values_before_backfill,
        api4_status_by_code,
        warrants[0].get("標的股", "") if warrants else "",
        warrants[0].get("標的名稱", "") if warrants else "",
    )

    backfill_pairs = extra_pairs + auto_pairs
    if backfill_pairs:
        before = len(pairs)
        for p in backfill_pairs:
            code = normalize_openapi_warrant_code(p.get("warrant_code", ""))
            broker_code = str(p.get("broker_code", "") or "").strip()
            branch = normalize_branch_name(p.get("branch", ""))
            if not code or not broker_code:
                continue
            p = dict(p)
            p["warrant_code"] = code
            p["branch"] = branch
            p.setdefault("warrant_name", str(warrant_lookup.get(code, {}).get("名稱", "") or code))
            p.setdefault("underlying_code", str(warrant_lookup.get(code, {}).get("標的股", "") or ""))
            p.setdefault("underlying_name", str(warrant_lookup.get(code, {}).get("標的名稱", "") or ""))
            pairs[(code, broker_code)] = p

        added = len(pairs) - before
        print(
            f"🔁 純 Live API5 pair 補查合併完成：原始 {before:,} 組｜"
            f"補查候選 {len(backfill_pairs):,} 組｜實際新增 {added:,} 組｜合併後 {len(pairs):,} 組"
        )

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

    debug_codes = get_debug_warrant_codes()
    selected_branches_for_debug = set(_get_selected_branch_flow_list()) if SELECTED_BRANCH_FLOW_ENABLE else set()
    selected_branches_for_debug = {normalize_branch_name(x) for x in selected_branches_for_debug if normalize_branch_name(x)}

    if debug_codes:
        debug_pairs = [
            p for p in pair_list
            if normalize_openapi_warrant_code(p.get("warrant_code", "")) in debug_codes
        ]
        if debug_pairs:
            print("========== API5 指定權證 pair Debug ==========")
            print(f"指定權證：{', '.join(sorted(debug_codes))}｜pair 數：{len(debug_pairs):,}")
            for p in debug_pairs[:30]:
                print(
                    f"{normalize_openapi_warrant_code(p.get('warrant_code', ''))}｜"
                    f"{p.get('warrant_name', '')}｜broker={p.get('broker_code', '')}｜"
                    f"branch={normalize_branch_name(p.get('branch', ''))}｜source={p.get('pair_source', 'API4')}"
                )
            if len(debug_pairs) > 30:
                print(f"... 指定權證 pair 尚有 {len(debug_pairs) - 30:,} 組未列印")
            print("========== API5 指定權證 pair Debug 結束 ==========")
        else:
            print(f"⚠️ API5 指定權證沒有任何 pair：{', '.join(sorted(debug_codes))}")

    def fetch_one(p):
        out = []
        code = normalize_openapi_warrant_code(p.get("warrant_code", ""))
        branch = normalize_branch_name(p.get("branch", ""))
        broker_code = str(p.get("broker_code", "") or "").strip()
        api_rows = api5_get(p["warrant_code"], p["broker_code"], days=API5_DAYS)

        debug_this_pair = code in debug_codes and (
            not selected_branches_for_debug or branch in selected_branches_for_debug
        )
        if debug_this_pair:
            print(
                f"🔎 API5 指定權證回查：{code}｜{p.get('warrant_name', '')}｜"
                f"broker={broker_code}｜branch={branch}｜raw_rows={len(api_rows or []):,}｜source={p.get('pair_source', 'API4')}"
            )

        debug_nonzero_rows = []
        for row in api_rows or []:
            buy_s = int(float(row.get("V2", 0) or 0))
            sell_s = int(float(row.get("V3", 0) or 0))
            buy_a = int(float(row.get("V4", 0) or 0) * 1000)
            sell_a = int(float(row.get("V5", 0) or 0) * 1000)
            net_a = buy_a - sell_a
            if debug_this_pair and (buy_a != 0 or sell_a != 0):
                debug_nonzero_rows.append({
                    "Date": row.get("V1", ""),
                    "buy_shares": buy_s,
                    "sell_shares": sell_s,
                    "buy_amount": buy_a,
                    "sell_amount": sell_a,
                    "net_amount": net_a,
                })
            if buy_a == 0 and sell_a == 0:
                continue
            dt = parse_date(row.get("V1", ""))
            if not dt:
                continue
            out.append({
                "Date": pd.Timestamp(dt).normalize(),
                "branch": branch,
                "broker_code": broker_code,
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

        if debug_this_pair and DEBUG_API5_ROWS_ENABLE:
            if debug_nonzero_rows:
                print(f"------ API5 指定權證非零買賣明細：{code} × {branch} ------")
                for r in debug_nonzero_rows[:30]:
                    print(
                        f"{r['Date']}｜買金額 {r['buy_amount']:,}｜賣金額 {r['sell_amount']:,}｜"
                        f"淨額 {r['net_amount']:,}｜買張 {r['buy_shares']:,}｜賣張 {r['sell_shares']:,}"
                    )
                if len(debug_nonzero_rows) > 30:
                    print(f"... 非零明細尚有 {len(debug_nonzero_rows) - 30:,} 筆未列印")
            else:
                print(f"⚠️ API5 指定權證沒有非零買賣明細：{code} × {branch}")

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
    frames = []

    if REPORT_LIVE_ONLY:
        print("🔴 純 Live 模式：權證分點資料不讀取 Google Sheet 快照、不合併 Google Sheet 歷史列、不讀本機快取")
    else:
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
        if not cached.empty:
            frames.append(cached)
            print(f"☁️ Google Sheet {GSHEET_WARRANT_HISTORY_SHEET} 既有歷史列命中：{len(cached):,} 筆，將與 100% live 資料合併去重")

    live_fetched = False
    if LIVE_FETCH_ENABLE:
        end_dt = pd.Timestamp(end_date).to_pydatetime()
        start_s = (end_dt - timedelta(days=API4_SCAN_CALENDAR_DAYS)).strftime("%Y/%m/%d")
        end_s = end_dt.strftime("%Y/%m/%d")
        with report_stage_timer(f"{stock_code}｜權證母體建立"):
            warrants = get_all_active_call_warrants(
                stock_code,
                stock_name,
                start_date=start_date,
                end_date=end_date,
            )

        with report_stage_timer(f"{stock_code}｜API4 分點掃描"):
            pairs = fetch_all_broker_pairs_for_warrants(warrants, start_s, end_s)

        # 歷史分點買賣明細補漏：
        # 若某檔權證近 90 天在明細表已出現買賣超，但 OpenAPI / MoneyDJ 最新母體或 API4 沒掃到，
        # 這裡直接把已知的「權證×分點」補進 API5 回查清單。
        supplement_pairs = load_historical_branch_detail_pairs_from_cache(
            stock_code,
            stock_name,
            start_date=start_date,
            end_date=end_date,
        )
        if supplement_pairs:
            before_pairs = len(pairs)
            pairs = merge_pair_lists(pairs, supplement_pairs)
            added_pairs = len(pairs) - before_pairs
            print(
                f"🔁 API5 pair 補漏合併完成：API4原始 {before_pairs:,} 組｜"
                f"歷史明細候選 {len(supplement_pairs):,} 組｜實際新增 {added_pairs:,} 組｜合併後 {len(pairs):,} 組"
            )
            if MAX_PAIRS > 0 and len(pairs) > MAX_PAIRS:
                pairs = pairs[:MAX_PAIRS]
                print(f"⚠️ MAX_PAIRS 限制啟用，合併後 pair 截斷為 {len(pairs):,} 組")

        with report_stage_timer(f"{stock_code}｜API5 買賣金額回查｜pairs={len(pairs):,}"):
            live = fetch_api5_events_for_pairs(
                pairs,
                start_date=start_date,
                end_date=end_date,
            )
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

    # 只有本次真的完成 live 抓取，且 API4/API5 皆 100% 無 failed，才寫回快取；純 Live 模式不寫回任何快取。
    if live_fetched and not REPORT_LIVE_ONLY:
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


def _get_debug_branch_warrant_flow_branches() -> List[str]:
    """取得指定分點權證明細 Debug 的分點清單，保留設定順序。"""
    branches = _parse_selected_branch_flow_names(DEBUG_BRANCH_WARRANT_FLOW_BRANCHES)
    return branches


def _get_debug_branch_warrant_flow_warrant_codes() -> set:
    """取得指定分點權證明細 Debug 的權證代號篩選清單；空白代表不篩權證。"""
    codes = set()
    raw = str(DEBUG_BRANCH_WARRANT_FLOW_WARRANT_CODES or "")
    for item in re.split(r"[,，;；\n\r|｜\s]+", raw):
        code = normalize_openapi_warrant_code(item)
        if code:
            codes.add(code)
    return codes


def _format_debug_branch_flow_rows(df: pd.DataFrame, max_rows: int = DEBUG_BRANCH_WARRANT_FLOW_MAX_ROWS) -> str:
    """將指定分點 Debug 明細轉成較容易閱讀的表格文字。"""
    if df is None or df.empty:
        return ""
    show_cols = [
        "Date", "branch", "warrant_code", "warrant_name",
        "buy_amount", "sell_amount", "net_amount",
    ]
    show_cols = [c for c in show_cols if c in df.columns]
    out = df.copy()
    if "Date" in out.columns:
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).map(lambda x: f"{float(x):,.0f}")
    out = out[show_cols].copy()
    max_rows = max(1, int(max_rows or 1))
    if len(out) > max_rows:
        out = out.head(max_rows)
        return out.to_string(index=False) + f"\n... 已截斷顯示前 {max_rows:,} 筆，完整符合筆數請看上方統計"
    return out.to_string(index=False)


def print_debug_branch_warrant_flow(
    stock_code: str,
    stock_name: str,
    stock_df: pd.DataFrame,
    warrant_events: pd.DataFrame,
    start_date=None,
    end_date=None,
):
    """輸出指定分點近 N 天與本週區間的權證買賣超明細。

    這段只印 log，不改變原本圖片、排行、權證資金流與任何計算結果。
    目的：
    1. 確認指定分點是否有進入本次 warrant_events。
    2. 區分「近 N 天有資料」與「本週 TOP5 區間有資料」。
    3. 區分「有賣出但被買進抵銷」與「權證母體漏抓」。
    """
    if not DEBUG_BRANCH_WARRANT_FLOW_ENABLE:
        return
    branches = _get_debug_branch_warrant_flow_branches()
    if not branches:
        return

    print("========== 指定分點權證明細 Debug ==========")
    print(
        f"🔎 Debug 標的：{_clean_code(stock_code)} {stock_name}｜"
        f"分點：{'、'.join(branches)}｜近 {DEBUG_BRANCH_WARRANT_FLOW_DAYS} 日曆天"
    )

    if warrant_events is None or warrant_events.empty:
        print("⚠️ warrant_events 為空，無法檢查指定分點")
        print("========== 指定分點權證明細 Debug 結束 ==========")
        return

    need_cols = {"Date", "branch", "warrant_code", "buy_amount", "sell_amount", "net_amount"}
    if not need_cols.issubset(warrant_events.columns):
        print(f"⚠️ warrant_events 缺少必要欄位：{sorted(need_cols - set(warrant_events.columns))}")
        print("========== 指定分點權證明細 Debug 結束 ==========")
        return

    e = warrant_events.copy()
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    e["branch"] = e["branch"].map(normalize_branch_name)
    e["warrant_code"] = e["warrant_code"].map(normalize_openapi_warrant_code)
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        e[c] = pd.to_numeric(e[c], errors="coerce").fillna(0.0).astype(float)

    warrant_code_filter = _get_debug_branch_warrant_flow_warrant_codes()
    if warrant_code_filter:
        print(f"🔎 Debug 只檢查指定權證：{', '.join(sorted(warrant_code_filter))}")
        e = e[e["warrant_code"].isin(warrant_code_filter)].copy()

    event_end = pd.Timestamp(end_date).normalize() if end_date is not None else e["Date"].max()
    lookback_days = max(0, int(DEBUG_BRANCH_WARRANT_FLOW_DAYS or 0))
    lookback_start = pd.Timestamp(event_end).normalize() - pd.Timedelta(days=lookback_days)

    branch_set = set(branches)
    debug_lookback = e[
        (e["branch"].isin(branch_set))
        & (e["Date"] >= lookback_start)
        & (e["Date"] <= pd.Timestamp(event_end).normalize())
    ].copy()

    print(f"🔎 指定分點近 {lookback_days} 日檢查區間：{lookback_start.date()} ~ {pd.Timestamp(event_end).date()}")
    if debug_lookback.empty:
        print("⚠️ 近 N 天 warrant_events 內沒有指定分點資料")
    else:
        total_buy = float(debug_lookback["buy_amount"].sum())
        total_sell = float(debug_lookback["sell_amount"].sum())
        total_net = float(debug_lookback["net_amount"].sum())
        print(
            f"✅ 近 N 天指定分點資料：{len(debug_lookback):,} 筆｜"
            f"買進 {fmt_money(total_buy)}｜賣出 {fmt_money(-total_sell)}｜淨額 {fmt_money(total_net)}"
        )

        by_warrant = (
            debug_lookback.groupby(["branch", "warrant_code", "warrant_name"], as_index=False, dropna=False)
            .agg({"buy_amount": "sum", "sell_amount": "sum", "net_amount": "sum"})
        )
        by_warrant["_abs_net"] = by_warrant["net_amount"].abs()
        by_warrant = by_warrant.sort_values(["_abs_net", "sell_amount", "buy_amount"], ascending=[False, False, False]).drop(columns=["_abs_net"])
        print("------ 近 N 天指定分點依權證彙總 ------")
        print(_format_debug_branch_flow_rows(by_warrant, max_rows=DEBUG_BRANCH_WARRANT_FLOW_MAX_ROWS))
        print("------ 近 N 天指定分點逐日明細 ------")
        detail = debug_lookback.sort_values(["Date", "warrant_code", "net_amount"], ascending=[True, True, True])
        print(_format_debug_branch_flow_rows(detail, max_rows=DEBUG_BRANCH_WARRANT_FLOW_MAX_ROWS))

    if stock_df is not None and not stock_df.empty:
        try:
            debug_ctx = build_weekly_context(stock_df, warrant_events, WEEK_TRADING_DAYS)
            week_start = pd.Timestamp(debug_ctx.get("week_start")).normalize()
            week_end = pd.Timestamp(debug_ctx.get("week_end")).normalize()
            debug_week = e[
                (e["branch"].isin(branch_set))
                & (e["Date"] >= week_start)
                & (e["Date"] <= week_end)
            ].copy()
            print(f"🔎 指定分點本週 TOP5 統計區間：{week_start.date()} ~ {week_end.date()}")
            if debug_week.empty:
                print("⚠️ 本週 TOP5 區間內沒有指定分點資料")
            else:
                week_buy = float(debug_week["buy_amount"].sum())
                week_sell = float(debug_week["sell_amount"].sum())
                week_net = float(debug_week["net_amount"].sum())
                print(
                    f"✅ 本週指定分點資料：{len(debug_week):,} 筆｜"
                    f"買進 {fmt_money(week_buy)}｜賣出 {fmt_money(-week_sell)}｜淨額 {fmt_money(week_net)}"
                )
                week_by_warrant = (
                    debug_week.groupby(["branch", "warrant_code", "warrant_name"], as_index=False, dropna=False)
                    .agg({"buy_amount": "sum", "sell_amount": "sum", "net_amount": "sum"})
                )
                week_by_warrant["_abs_net"] = week_by_warrant["net_amount"].abs()
                week_by_warrant = week_by_warrant.sort_values(["_abs_net", "sell_amount", "buy_amount"], ascending=[False, False, False]).drop(columns=["_abs_net"])
                print("------ 本週指定分點依權證彙總 ------")
                print(_format_debug_branch_flow_rows(week_by_warrant, max_rows=DEBUG_BRANCH_WARRANT_FLOW_MAX_ROWS))
        except Exception as e_debug:
            print(f"⚠️ 指定分點本週區間 Debug 失敗：{e_debug}")

    print("========== 指定分點權證明細 Debug 結束 ==========")


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


def _get_cached_top_branch_tables(
    ctx: dict,
    scope: str,
    events_df: pd.DataFrame,
    topn: int = 5,
):
    """同一份週報內容中重用 TOP5 統計，避免重複過濾、分組與輸出相同 Debug log。"""
    if not isinstance(ctx, dict):
        return top_branch_tables(events_df, topn=topn)

    cache = ctx.setdefault("_top_branch_tables_cache", {})
    cache_key = (str(scope or "default"), int(topn or 5))
    cached = cache.get(cache_key)
    if cached is not None:
        buy_cached, sell_cached = cached
        return buy_cached.copy(), sell_cached.copy()

    buy_top, sell_top = top_branch_tables(events_df, topn=topn)
    cache[cache_key] = (buy_top.copy(), sell_top.copy())
    return buy_top, sell_top


def _build_rule_based_next_week_watch(ctx: dict) -> str:
    """AI 失敗時的條件式下週觀察，不預測漲跌，也不提供買賣建議。"""
    pattern = _build_price_volume_pattern_payload(ctx)
    pattern_label = str(pattern.get("current_pattern_label", "") or "").strip()
    position = str(pattern.get("latest_position_relative_to_two_zones", "") or "").strip()
    recent_event = str(pattern.get("recent_maximum_zone_pattern", "") or "").strip()
    price_volume = str(pattern.get("weekly_price_volume_relationship", "") or "").strip()
    warrant_net = float(ctx.get("total_net", 0) or 0)
    inst_ctx = _get_weekly_institutional_context(ctx)
    inst_class = str(inst_ctx.get("classification", "") or "")

    if pattern.get("available"):
        if any(k in pattern_label for k in ["區間", "整理", "震盪"]):
            return (
                f"下週觀察：目前屬於{pattern_label}，可留意股價能否脫離主要成交成本區，"
                f"以及突破或回測時量能是否配合；若仍在區間內反覆，代表方向尚未明確。"
            )
        if "突破" in pattern_label:
            return (
                f"下週觀察：目前屬於{pattern_label}，重點在突破後能否守住主要成交成本區，"
                f"並觀察量能與權證資金是否持續，避免突破後快速回落。"
            )
        if "跌破" in pattern_label or "轉弱" in pattern_label:
            return (
                f"下週觀察：目前屬於{pattern_label}，可留意股價能否重新站回主要成交成本區；"
                f"若反彈量能不足且權證資金轉弱，型態修復力道仍有限。"
            )
        return (
            f"下週觀察：目前型態為{pattern_label or '整理格局'}，{position}；"
            f"後續可追蹤{recent_event or '主要成交成本區的突破與回測'}，並確認{price_volume or '價量是否配合'}。"
        )

    if warrant_net > 0:
        flow_text = "權證資金能否延續淨買超"
    elif warrant_net < 0:
        flow_text = "權證資金是否持續調節"
    else:
        flow_text = "權證資金能否形成明確方向"
    inst_text = "法人是否由中性轉為明確方向" if inst_class == "接近中性" else "法人與權證資金是否維持同向"
    return f"下週觀察：可追蹤{flow_text}，以及{inst_text}；若價格與資金方向開始同步，訊號的延續性會較具參考價值。"


def _rule_based_key_points(ctx, stock_name: str):
    """AI 失敗時的備援：保留兩個本週分析，再加入一個條件式下週觀察。"""
    candidates = []

    branch_points = _build_branch_perf_focus_points(ctx, limit=1)
    candidates.extend(branch_points)

    pattern_point = _build_rule_based_pattern_point(ctx)
    if pattern_point:
        candidates.append(pattern_point)

    crossflow_point = _build_rule_based_crossflow_point(ctx)
    if crossflow_point:
        candidates.append(crossflow_point)

    if len(candidates) < 2:
        e = ctx.get("week_events")
        if e is not None and not e.empty:
            by_branch = e.groupby("branch")["net_amount"].sum().sort_values(ascending=False)
            positive = by_branch[by_branch > 0]
            if not positive.empty:
                top_share = float(positive.iloc[0] / max(positive.sum(), 1.0) * 100)
                candidates.append(
                    f"籌碼集中度：本週最大買超分點占全部正買超約 {top_share:.1f}%，"
                    "買盤集中度偏高時，需與後續分點連續性一併判斷，不能只看單週金額。"
                )

    points = []
    for p in candidates:
        if p and p not in points:
            points.append(p)
        if len(points) >= 2:
            break

    watch = _build_rule_based_next_week_watch(ctx)
    if watch:
        points.append(watch)
    return _clean_weekly_key_points(points)[:3]

def _finish_complete_summary_point(text: str, max_len: int, min_cut_len: int = 30) -> str:
    """將週報重點整理成獨立完整句，不用省略號，也不在句子中間硬切。"""
    s = _normalize_news_text(text)
    s = re.sub(r"(?:\.{3,}|…+)$", "", s).strip()
    s = s.strip("；;，, ")
    if not s:
        return ""

    max_len = max(1, int(max_len or len(s)))
    if len(s) > max_len:
        end_positions = [s.rfind(p, 0, max_len + 1) for p in ["。", "！", "？"]]
        end_idx = max(end_positions)
        if end_idx >= min_cut_len:
            s = s[:end_idx + 1].strip()
        else:
            clause_positions = [s.rfind(p, 0, max_len + 1) for p in ["；", ";", "，", ","]]
            clause_idx = max(clause_positions)
            if clause_idx >= min_cut_len:
                s = s[:clause_idx].rstrip("；;，, ") + "。"
            # 找不到合理斷點時保留完整原句，避免半句或省略號。

    s = re.sub(r"(?:\.{3,}|…+)$", "", s).strip()
    if s and s[-1] not in "。！？":
        s = s.rstrip("；;，, ") + "。"
    return s


def _is_structured_report_point(text: str) -> bool:
    """辨識已經被整理成週報格式的句子，避免被一般新聞標題過濾規則誤刪。"""
    s = _normalize_news_text(text)
    if not s:
        return False
    if "｜" not in s and "|" not in s:
        return False
    required_terms = ["分類：", "分類:", "結果：", "結果:", "說明：", "說明:", "結論：", "結論:", "重點：", "重點:", "觀察：", "觀察:", "依據：", "依據:", "條件：", "條件:", "追蹤：", "追蹤:"]
    return any(k in s for k in required_terms)


def _points_are_independent_and_complete(points: List[str]) -> bool:
    """檢查每點是否可獨立閱讀，避免上一點講一半、下一點接續。"""
    if not points:
        return False
    dependent_starts = (
        "此外", "另外", "再者", "承上", "延續前述", "延續上述",
        "前述", "上述", "另一方面", "相較之下", "相對地",
    )
    for p in points:
        s = str(p or "").strip()
        if not s or s.startswith(dependent_starts):
            return False
        if "…" in s or "..." in s:
            return False
        if s[-1] not in "。！？":
            return False
    return True


def _trim_weekly_point(text: str, max_len: int | None = None) -> str:
    max_len = int(max_len or WEEKLY_KEYPOINT_POINT_MAX_LEN)
    s = _normalize_news_text(text)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = re.sub(r"^(本週重點|重點|摘要)[:：]\s*", "", s).strip()
    # 舊版 prompt 可能輸出「結論｜依據」；顯示時改成更容易一眼辨識的標籤。
    s = re.sub(r"^結論[｜|]", "結論：", s)
    s = re.sub(r"^下週觀察[:：]結論[｜|]", "下週觀察：結論：", s)
    s = s.replace("｜依據：", "｜依據：").replace("｜觀察：", "｜觀察：")
    return _finish_complete_summary_point(s, max_len=max_len, min_cut_len=36)

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
    """Gemini 本週重點太短時，只用具分析意義的固定備援補足。"""
    return _rule_based_key_points(ctx, stock_name)


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
    """本週重點與下週觀察：AI 自主挑選重要訊號；AI 失敗才走條件式備援。"""
    ai_points = _summarize_weekly_context_with_gemini(ctx, stock_name)
    if ai_points:
        # AI 成功時不再強制插入固定勝率句型，也不使用規則式文字補字數；
        # 讓 AI 依完整資料自行判斷最值得呈現的三個重點。
        return _clean_weekly_key_points(ai_points)[:3]

    # AI 不可用、呼叫失敗、格式不合格或內容過短時，才回到條件式備援。
    rule_points = _ensure_branch_perf_point(_rule_based_key_points(ctx, stock_name), ctx)
    return _ensure_weekly_keypoint_min_total(rule_points, ctx, stock_name)

# ============================================================
# 新聞抓取：抓一週內新聞內文並整理成真正重點
# ============================================================

NEWS_BODY_MAX_CHARS = int(os.getenv("WARRANT_NEWS_BODY_MAX_CHARS", "3500"))
NEWS_FETCH_TIMEOUT = float(os.getenv("WARRANT_NEWS_FETCH_TIMEOUT", "10"))
NEWS_SUMMARY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_SUMMARY_MAX_POINTS", "3"))
NEWS_DISPLAY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_DISPLAY_MAX_POINTS", "3"))
NEWS_SUMMARY_POINT_MAX_LEN = int(os.getenv("WARRANT_NEWS_SUMMARY_POINT_MAX_LEN", "125"))
NEWS_SUMMARY_MIN_TOTAL_CHARS = int(os.getenv("WARRANT_NEWS_SUMMARY_MIN_TOTAL_CHARS", "90"))
NEWS_SUMMARY_MIN_POINTS = int(os.getenv("WARRANT_NEWS_SUMMARY_MIN_POINTS", "1"))
# 新聞摘要風格版本：調整 prompt 後使用新快取鍵，避免 Google Sheet 當日舊快取繼續輸出舊版空泛摘要。
NEWS_SUMMARY_STYLE_VERSION = os.getenv("WARRANT_NEWS_SUMMARY_STYLE_VERSION", "v15_arabic_digits_news").strip() or "v15_arabic_digits_news"
NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK = os.getenv("WARRANT_NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


def _news_points_cache_task() -> str:
    safe_version = re.sub(r"[^A-Za-z0-9_.-]", "_", str(NEWS_SUMMARY_STYLE_VERSION or "v15_arabic_digits_news"))
    # 內部版本固定加在任務鍵後面，避免 Actions 環境變數仍停在舊版時，
    # 繼續讀到先前 0 點或壞格式的新聞快取。
    internal_version = "validated_v18_fast_three_points"
    return f"news_points_{safe_version}_{internal_version}"

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
# 新聞統整與本週重點共用較低 temperature，降低格式漂移與無依據改寫。
# 模型名稱維持 GEMINI_MODEL 原設定，不另外切換分析模型。
GEMINI_ANALYSIS_TEMPERATURE = float(os.getenv("WARRANT_GEMINI_ANALYSIS_TEMPERATURE", "0.25"))
GEMINI_STRUCTURED_OUTPUT_ENABLE = os.getenv(
    "WARRANT_GEMINI_STRUCTURED_OUTPUT_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
NEWS_MAX_ARTICLES_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLES_TO_GEMINI", "12"))
NEWS_MAX_ARTICLE_CHARS_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLE_CHARS_TO_GEMINI", "3500"))
WEEKLY_KEYPOINT_LLM_ENABLE = os.getenv("WARRANT_WEEKLY_KEYPOINT_LLM_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
WEEKLY_KEYPOINT_MAX_POINTS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MAX_POINTS", "3"))
WEEKLY_KEYPOINT_POINT_MAX_LEN = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_POINT_MAX_LEN", "100"))
WEEKLY_KEYPOINT_MIN_TOTAL_CHARS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MIN_TOTAL_CHARS", "120"))
WEEKLY_KEYPOINT_MIN_POINTS = int(os.getenv("WARRANT_WEEKLY_KEYPOINT_MIN_POINTS", "3"))
# 本週重點快取版本：只有通過格式、內容與數字接地驗證的結果才會寫入。
WEEKLY_KEYPOINT_STYLE_VERSION = os.getenv(
    "WARRANT_WEEKLY_KEYPOINT_STYLE_VERSION",
    "validated_v24_json_grounded_previous_week",
).strip() or "validated_v24_json_grounded_previous_week"
# 新聞抓取速度版：只抓 Google News 重要新聞，不再掃 PTT，避免 GitHub Actions 執行時間過長。
# 預設提高搜尋母體，避免部分冷門股因前幾篇原文被擋或 RSS 摘要太短而沒有新聞輸出。
NEWS_GOOGLE_MAX_ITEMS = int(os.getenv("WARRANT_NEWS_GOOGLE_MAX_ITEMS", "36"))
NEWS_GOOGLE_SCAN_MULTIPLIER = int(os.getenv("WARRANT_NEWS_GOOGLE_SCAN_MULTIPLIER", "10"))
NEWS_GOOGLE_MIN_USABLE_ARTICLES = int(os.getenv("WARRANT_NEWS_GOOGLE_MIN_USABLE_ARTICLES", str(max(2, min(4, NEWS_SUMMARY_MAX_POINTS)))))
NEWS_GOOGLE_FALLBACK_DAYS = os.getenv("WARRANT_NEWS_FALLBACK_DAYS", "7,14,30").strip() or "7,14,30"
# 極速新聞模式：預設開啟。只使用 Google News RSS 的標題 / 摘要 / URL，不進新聞網站抓原文。
# 若想回到高品質原文抓取模式，可在 GitHub Actions 設 WARRANT_NEWS_FAST_MODE=0。
NEWS_FAST_MODE = os.getenv("WARRANT_NEWS_FAST_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
# 混合新聞模式：先用 RSS 快速掃描與排序，再只替最高分的前 2～3 篇補抓原文。
# 預設關閉，避免新聞網站持續慢速回應時拖住整份週報；需要測試原文補抓時可手動設為 1。
# NEWS_FAST_MODE=0 時仍沿用既有完整原文模式。
NEWS_FAST_HYBRID_BODY_FETCH_ENABLE = os.getenv(
    "WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_ENABLE",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
NEWS_FAST_HYBRID_BODY_FETCH_TOPK = max(
    0,
    int(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_TOPK", "3")),
)
NEWS_FAST_HYBRID_BODY_FETCH_WORKERS = max(
    1,
    int(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_WORKERS", "3")),
)
# 即使環境變數設得過大，混合補抓仍強制限制單篇最多 5 秒、整批最多 10 秒，
# 並限制下載大小，避免慢速串流或超大網頁拖住 GitHub Actions 數分鐘。
NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT = min(
    5.0,
    max(
        1.0,
        float(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT", "4")),
    ),
)
NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT = min(
    10.0,
    max(
        1.0,
        float(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT", "8")),
    ),
)
NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES = max(
    32768,
    min(
        524288,
        int(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES", "196608")),
    ),
)
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

# 多來源新聞：Google News 之外，再從 Yahoo 股市 RSS、Bing News RSS 與 MoneyDJ 新聞搜尋補充。
# 任一來源失敗時只略過該來源，不影響其他來源或週報產生。
NEWS_MULTI_SOURCE_ENABLE = os.getenv("WARRANT_NEWS_MULTI_SOURCE_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_YAHOO_RSS_ENABLE = os.getenv("WARRANT_NEWS_YAHOO_RSS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_BING_RSS_ENABLE = os.getenv("WARRANT_NEWS_BING_RSS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_MONEYDJ_SEARCH_ENABLE = os.getenv("WARRANT_NEWS_MONEYDJ_SEARCH_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE = int(os.getenv("WARRANT_NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE", "12"))
NEWS_MONEYDJ_BODY_FETCH_LIMIT = int(os.getenv("WARRANT_NEWS_MONEYDJ_BODY_FETCH_LIMIT", "5"))
NEWS_MULTI_SOURCE_RETURN_LIMIT = int(os.getenv(
    "WARRANT_NEWS_MULTI_SOURCE_RETURN_LIMIT",
    str(max(NEWS_MAX_ARTICLES_TO_GEMINI * 2, NEWS_GOOGLE_MIN_USABLE_ARTICLES, 24)),
))
# 新聞最低素材／顯示目標：正常情況至少整理 2 則不同事件；
# 只有所有來源與 7/14/30 日範圍都查完後仍只有一個事件，才允許只顯示 1 則。
NEWS_MIN_DISTINCT_ARTICLES = max(1, int(os.getenv("WARRANT_NEWS_MIN_DISTINCT_ARTICLES", "2")))
# 公開資訊觀測站重大訊息補強：使用證交所 OpenAPI 的上市／上櫃每日重大訊息。
NEWS_MOPS_ENABLE = os.getenv("WARRANT_NEWS_MOPS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
NEWS_MOPS_MAX_ITEMS = max(1, int(os.getenv("WARRANT_NEWS_MOPS_MAX_ITEMS", "12")))
NEWS_MOPS_ENDPOINTS = [
    ("上市重大訊息", "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"),
    ("上櫃重大訊息", "https://openapi.twse.com.tw/v1/opendata/t187ap04_O"),
]

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
    "3583": ["辛耘"],
    "6285": ["啟碁"],
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



_CN_NUMERAL_DIGITS = {
    "零": 0, "〇": 0, "○": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUMERAL_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000, "億": 100000000, "亿": 100000000}
_CN_NUMERAL_CHARS = "零〇○一二兩两三四五六七八九十百千萬万億亿"


def _parse_chinese_numeral_to_int(token: str):
    """將常見中文數字轉為整數；失敗時回傳 None。

    用途只限新聞文字顯示，例如「十二點六億元」→「12.6億元」。
    不處理模糊財經語意，只處理明確出現在數量單位前的中文數字。
    """
    t = str(token or "").strip()
    if not t:
        return None
    if not all(ch in _CN_NUMERAL_DIGITS or ch in _CN_NUMERAL_UNITS for ch in t):
        return None

    # 二零二六、二〇二六 這種逐字年份寫法。
    if not any(ch in _CN_NUMERAL_UNITS for ch in t):
        digits = "".join(str(_CN_NUMERAL_DIGITS[ch]) for ch in t if ch in _CN_NUMERAL_DIGITS)
        return int(digits) if digits else None

    total = 0
    section = 0
    number = 0
    for ch in t:
        if ch in _CN_NUMERAL_DIGITS:
            number = _CN_NUMERAL_DIGITS[ch]
            continue
        unit = _CN_NUMERAL_UNITS.get(ch)
        if unit is None:
            return None
        if unit < 10000:
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
    return total + section + number


def _format_chinese_numeral_number(int_part: str, frac_part: str | None = None) -> str:
    value = _parse_chinese_numeral_to_int(int_part)
    if value is None:
        return str(int_part or "") + (("點" + frac_part) if frac_part else "")
    if frac_part:
        frac_digits = "".join(str(_CN_NUMERAL_DIGITS.get(ch, "")) for ch in frac_part)
        frac_digits = re.sub(r"[^0-9]", "", frac_digits)
        if frac_digits:
            return f"{value}.{frac_digits}"
    return str(value)


def _normalize_chinese_numbers_for_news(text: str) -> str:
    """新聞區塊數字統一用阿拉伯數字。

    只轉換明確接數量單位的中文數字，避免把券商分點名稱或一般中文詞誤改。
    例：十二點六億元 → 12.6億元、第三季 → 第3季、六月 → 6月。
    """
    s = str(text or "")
    if not s:
        return ""

    unit_pattern = r"(?:億元|萬元|元|億|萬|%|％|百分點|個百分點|倍|天|張|季|月|年|日|檔|筆|項|座|家|人|台|套)"

    # 百分之十二點六 → 12.6%
    def repl_percent(m):
        num = _format_chinese_numeral_number(m.group("int"), m.groupdict().get("frac"))
        return f"{num}%"

    s = re.sub(
        rf"百分之(?P<int>[{_CN_NUMERAL_CHARS}]+)(?:點(?P<frac>[零〇○一二兩两三四五六七八九]+))?",
        repl_percent,
        s,
    )

    # 第三季、第二季 → 第3季、第2季。
    s = re.sub(
        rf"第(?P<num>[{_CN_NUMERAL_CHARS}]+)(?P<unit>季|期|屆|次)",
        lambda m: f"第{_format_chinese_numeral_number(m.group('num'))}{m.group('unit')}",
        s,
    )

    # 十二點六億元 → 12.6億元。
    s = re.sub(
        rf"(?P<int>[{_CN_NUMERAL_CHARS}]+)點(?P<frac>[零〇○一二兩两三四五六七八九]+)(?=\s*{unit_pattern})",
        lambda m: _format_chinese_numeral_number(m.group("int"), m.group("frac")),
        s,
    )

    # 十二億元、六月、一百二十家 → 12億元、6月、120家。
    # 注意：前一段會先把「十二點零六億元」轉成「12.06億元」。
    # 這裡不能再把「億元」中的「億」當成中文數字轉成 0，
    # 否則會誤變成「12.060元」，造成億元單位消失。
    s = re.sub(
        rf"(?<![第0-9.])(?P<num>[{_CN_NUMERAL_CHARS}]+)(?=\s*{unit_pattern})",
        lambda m: _format_chinese_numeral_number(m.group("num")),
        s,
    )

    # 舊快取若已被前一版誤轉成「12.060元 / 12.60元」，
    # 且前文明確是營收、訂單、接單、合約、工程金額等金額語境，
    # 顯示前補回「億元」，避免圖卡繼續出現單位消失的文字。
    def _repair_bad_billion_unit(m):
        start = m.start()
        ctx = s[max(0, start - 22): start]
        if any(k in ctx for k in ["營收", "收入", "訂單", "接單", "合約", "工程", "金額", "新台幣", "投資", "標案", "採購"]):
            return f"{m.group('num')}億元"
        return m.group(0)

    s = re.sub(r"(?P<num>\d+\.\d{1,3})0元", _repair_bad_billion_unit, s)
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
    s = _normalize_chinese_numbers_for_news(s)
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
    """原文被擋時，RSS 摘要仍須通過公司相關性與實質內容門檻。"""
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
    return _passes_news_quality_gate(title, s, stock_code, stock_name)

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


def _fetch_article_body(
    url: str,
    request_timeout: float | None = None,
    max_bytes: int | None = None,
    hard_deadline_seconds: float | None = None,
) -> str:
    """嘗試進入新聞原文頁抓內文；失敗時回傳空字串。

    一般慢速原文模式未傳入額外參數時，維持原本 NEWS_FETCH_TIMEOUT 與完整下載行為。
    混合 RSS 補抓會傳入較短 timeout、最大下載量與硬截止時間，避免慢速串流拖住週報。
    """
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

        timeout_value = float(NEWS_FETCH_TIMEOUT if request_timeout is None else request_timeout)
        timeout_value = max(1.0, timeout_value)
        use_limited_stream = max_bytes is not None or hard_deadline_seconds is not None
        r = get_thread_session().get(
            final_url,
            headers=headers,
            timeout=(min(5.0, timeout_value), timeout_value),
            allow_redirects=True,
            stream=use_limited_stream,
        )
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")

        if use_limited_stream:
            byte_limit = max(32768, int(max_bytes or NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES))
            deadline = (
                time.perf_counter() + max(1.0, float(hard_deadline_seconds))
                if hard_deadline_seconds is not None
                else None
            )
            chunks = []
            downloaded = 0
            for chunk in r.iter_content(chunk_size=16384):
                if deadline is not None and time.perf_counter() >= deadline:
                    return ""
                if not chunk:
                    continue
                remaining = byte_limit - downloaded
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                chunks.append(chunk)
                downloaded += len(chunk)
                if downloaded >= byte_limit:
                    break
            encoding = r.encoding or "utf-8"
            html_text = b"".join(chunks).decode(encoding, errors="replace")
        else:
            html_text = r.text

        if (
            "text/html" not in content_type
            and "application/xhtml" not in content_type
            and not html_text.lstrip().startswith("<")
        ):
            return ""
        body = _extract_article_text_from_html(html_text)
        if body and len(body) >= 80:
            return body
    except Exception as e:
        print(f"⚠️ 新聞內文抓取失敗：{url}｜{e}")
    return ""


def _is_unknown_stock_name(stock_name: str) -> bool:
    s = str(stock_name or "").strip()
    return (not s) or s in ("未知公司", "未知", "-", "--", "nan", "None")


def _extract_company_name_near_code(text: str, stock_code: str) -> str:
    """從新聞標題 / 摘要中擷取「公司名(代號)」或「代號 公司名」格式，供名稱失敗時補別名。"""
    code = str(stock_code or "").strip()
    s = _normalize_news_text(text)
    if not code or not s:
        return ""

    patterns = [
        rf"([一-鿿A-Za-z][一-鿿A-Za-z0-9\-]{1,14})\s*[（(]\s*{re.escape(code)}\s*[）)]",
        rf"{re.escape(code)}\s*[）)]?\s*([一-鿿A-Za-z][一-鿿A-Za-z0-9\-]{{1,14}})",
        rf"([一-鿿A-Za-z][一-鿿A-Za-z0-9\-]{{1,14}})\s*{re.escape(code)}",
    ]
    bad_prefixes = ("營收", "公告", "新聞", "焦點股", "個股", "台股", "本週", "近期", "市場")
    for pattern in patterns:
        m = re.search(pattern, s)
        if not m:
            continue
        name = str(m.group(1) or "").strip(" ：:，,。；;｜|()（）[]【】")
        name = re.sub(r"^(營收|公告|新聞|焦點股|個股|台股)", "", name).strip()
        if len(name) < 2 or name.startswith(bad_prefixes):
            continue
        if re.fullmatch(r"\d+", name):
            continue
        return name
    return ""


def _get_news_aliases(stock_code: str, stock_name: str) -> List[str]:
    aliases = []
    code = str(stock_code or "").strip()
    name = str(stock_name or "").strip()

    for a in [code, name]:
        a = str(a or "").strip()
        if not a or a in ("未知公司", "未知", "-", "--", "nan", "None"):
            continue
        if a not in aliases:
            aliases.append(a)
    for a in STOCK_NEWS_ALIAS_MAP.get(code, []):
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
        "目標價 OR 評等 OR EPS OR ASP OR 毛利 OR 毛利率 OR 供需 OR 漲價 OR "
        "ETF OR 成分股 OR 指數調整 OR 權重調整 OR 被動資金"
    )
    broad_topics = (
        "新聞 OR 題材 OR 法人 OR 展望 OR 營運 OR 產業 OR 報價 OR 需求 OR "
        "接單 OR 出貨 OR 財報 OR 營收 OR 法說 OR 目標價 OR 評等"
    )

    etf_topics = (
        "ETF OR 成分股 OR 成分證券 OR 納入 OR 剔除 OR 換股 OR 指數調整 OR "
        "權重調整 OR 被動資金 OR 加碼 OR 減碼 OR 增持 OR 減持"
    )
    queries = [
        ("嚴格基本面", f"({strict_part}) ({strict_topics}) {safe_excludes} when:{days}d"),
        ("ETF與指數", f"({strict_part}) ({etf_topics}) {safe_excludes} when:{days}d"),
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


def _has_representative_etf_or_index_event(text: str) -> bool:
    """只接受真正涉及 ETF／指數成分調整或被動資金變動的代表性事件。"""
    s = _normalize_news_text(text)
    if not s:
        return False
    has_vehicle = bool(re.search(r"ETF|指數|成分股|成分證券|被動資金|追蹤指數", s, re.I))
    has_event = bool(re.search(
        r"納入|剔除|新增|刪除|換股|定期調整|成分調整|權重調整|權重升降|調升權重|調降權重|"
        r"加碼|減碼|增持|減持|買進|賣出|持股增加|持股減少|季度審核|定期審核|審核結果",
        s,
    ))
    return bool(has_vehicle and has_event)


def _score_news_article_relevance(article: dict, stock_code: str, stock_name: str) -> int:
    """新聞候選排序：公司直接相關、具體數字與 ETF／指數事件優先。"""
    title = _clean_news_title(article.get("title", ""))
    content = _normalize_news_text(article.get("content", article.get("description", "")))
    combined = _normalize_news_text(f"{title} {content}")
    aliases = _get_news_aliases(stock_code, stock_name)
    score = 0
    if any(a and a in title for a in aliases):
        score += 12
    elif any(a and a in combined for a in aliases):
        score += 5
    if _has_representative_etf_or_index_event(combined):
        score += 7
    if re.search(r"營收|EPS|每股純益|毛利率|獲利|法說|財測|接單|出貨|產能|目標價|評等|報價|供需", combined, re.I):
        score += 5
    if re.search(r"\d+(?:\.\d+)?\s*(%|％|元|億元|萬|倍|張|股)", combined):
        score += 3
    if _is_low_value_market_news(combined):
        score -= 8
    return score


def _has_substantive_company_news(text: str) -> bool:
    """判斷內容是否包含公司基本面、營運、法人觀點或具體產業供需事件。"""
    s = _normalize_news_text(text)
    if not s:
        return False
    if _has_representative_etf_or_index_event(s):
        return True

    direct_terms = re.search(
        r"營收|財報|獲利|虧損|轉盈|EPS|每股純益|毛利|毛利率|法說|財測|展望|"
        r"接單|訂單|出貨|產能|擴產|減產|客戶|合作|合約|長約|產品|新品|量產|認證|"
        r"目標價|評等|升評|降評|調升|調降|併購|處分|投資|增資|減資|股利|配息|"
        r"董事會|重大訊息|公告|供應鏈|庫存|ASP|報價|漲價|降價|供需|需求|市占",
        s,
        re.I,
    )
    if direct_terms:
        return True

    # 產業名詞本身不夠；必須同時出現與公司影響有關的動詞或供需描述。
    industry_term = re.search(r"AI|伺服器|半導體|記憶體|DRAM|NAND|HBM|PCB|載板|CoWoS|先進封裝", s, re.I)
    relation_term = re.search(r"受惠|受影響|帶動|推升|挹注|貢獻|需求|供需|報價|接單|出貨|產能|布局|導入|合作|量產|進展|告捷|突破|開發", s)
    return bool(industry_term and relation_term)


def _is_low_value_market_news(text: str) -> bool:
    """辨識只有盤勢、漲跌、熱門股清單或導流性質的低資訊新聞。"""
    s = _normalize_news_text(text)
    if not s:
        return True
    if _has_representative_etf_or_index_event(s):
        return False
    low_value_terms = [
        "焦點股", "強勢股", "熱門股", "飆股", "漲停股", "亮燈", "盤中焦點", "今日焦點",
        "多檔", "名單", "排行", "選股", "存股", "高股息", "值得關注", "完整看", "看更多",
        "三大法人買賣超", "買超排行", "賣超排行", "大盤", "台股盤勢", "類股齊揚",
    ]
    pure_price_terms = ["漲停", "創高", "爆量", "飆漲", "大漲", "重挫", "跌停", "漲幅", "跌幅"]
    if any(k in s for k in low_value_terms):
        return True
    if any(k in s for k in pure_price_terms) and not _has_substantive_company_news(s):
        return True
    return False


def _passes_news_quality_gate(title: str, description_or_body: str, stock_code: str, stock_name: str) -> bool:
    """新聞進入 Gemini 前的品質門檻：需明確對應本公司，且具有具體資訊。"""
    title = _clean_news_title(title)
    content = _normalize_news_text(description_or_body)
    combined = _normalize_news_text(f"{title} {content}")
    if len(_title_compare_text(combined)) < 16:
        return False
    if _has_conflicting_similar_company_name(combined, stock_code, stock_name):
        return False

    aliases = _get_news_aliases(stock_code, stock_name)
    if aliases and not _news_text_matches_target_stock(combined, stock_code, stock_name):
        return False

    # 股票名稱若暫時查不到，Google News 常仍會以「公司名(代號)」呈現；
    # 只要代號明確出現，且能從標題 / 摘要反推公司名，就視為明確對應本股票。
    if _is_unknown_stock_name(stock_name) and stock_code:
        inferred_name = _extract_company_name_near_code(combined, stock_code)
        if inferred_name and inferred_name not in aliases:
            aliases.append(inferred_name)

    if _is_low_value_market_news(combined):
        return False
    if not _has_substantive_company_news(combined):
        return False
    return True


def _is_valid_fast_rss_news_item(title: str, description: str, stock_code: str = "", stock_name: str = "") -> bool:
    """極速 RSS 模式只保留明確對應公司且含具體資訊的新聞，不再因只有股票名稱就放行。"""
    return _passes_news_quality_gate(title, description, stock_code, stock_name)

def _read_rss_items(url: str, source_family: str, max_items: int = 40) -> List[dict]:
    """讀取一般 RSS 2.0 新聞來源，失敗時回傳空陣列。"""
    try:
        headers = {
            "User-Agent": HDR["User-Agent"],
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        r = get_thread_session().get(url, headers=headers, timeout=(5, NEWS_FETCH_TIMEOUT))
        r.raise_for_status()
        root = ET.fromstring(r.content)
        rows = []
        for item in root.findall(".//item")[:max(1, int(max_items))]:
            title = _clean_news_title(item.findtext("title") or "")
            link = str(item.findtext("link") or "").strip()
            published = str(item.findtext("pubDate") or item.findtext("date") or "").strip()
            description = _normalize_news_text(_html_to_readable_text(item.findtext("description") or ""))
            source_el = item.find("source")
            source = (source_el.text if source_el is not None and source_el.text else source_family).strip()
            rows.append({
                "title": title,
                "url": link,
                "source": source or source_family,
                "source_family": source_family,
                "published": published,
                "description": description,
            })
        return rows
    except Exception as e:
        print(f"⚠️ {source_family} RSS 讀取失敗：{e}")
        return []


def _build_external_rss_article(item: dict, stock_code: str, stock_name: str, search_days: int, source_tag: str) -> dict | None:
    title = _clean_news_title(item.get("title", ""))
    description = _normalize_news_text(item.get("description", ""))
    link = str(item.get("url", "") or "").strip()
    published = str(item.get("published", "") or "").strip()
    if not title or not _is_within_recent_days_from_rss(published, days=search_days):
        return None
    if not _passes_news_quality_gate(title, description, stock_code, stock_name):
        return None

    body = ""
    body_ok = False
    if not NEWS_FAST_MODE and link:
        body = _fetch_article_body(link)
        body_ok = (
            _is_valid_article_body(body, title=title, description=description)
            and _passes_news_quality_gate(title, body, stock_code, stock_name)
        )

    fallback_ok = False
    if body_ok:
        content = body
        content_source = f"{source_tag}_article"
    else:
        fallback_ok = NEWS_RSS_DESCRIPTION_FALLBACK and _is_valid_fast_rss_news_item(
            title, description, stock_code, stock_name
        )
        if not fallback_ok:
            return None
        content = _build_fast_rss_news_content(
            title,
            description,
            source=item.get("source", source_tag),
            published=published,
        )
        content_source = f"{source_tag}_rss"

    article = {
        "title": title,
        "url": link,
        "source": str(item.get("source", source_tag) or source_tag),
        "source_family": str(item.get("source_family", source_tag) or source_tag),
        "published": published,
        "description": description,
        "content": content,
        "body_ok": bool(body_ok),
        "fallback_ok": bool(fallback_ok),
        "content_source": content_source,
        "body_length": len(content),
        "search_days": int(search_days),
        "query_stage": source_tag,
    }
    article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name)
    return article


def fetch_yahoo_finance_rss_articles(stock_code: str, stock_name: str, max_items: int = 8) -> List[dict]:
    if not NEWS_MULTI_SOURCE_ENABLE or not NEWS_YAHOO_RSS_ENABLE:
        return []
    urls = [
        ("Yahoo最新新聞", "https://tw.stock.yahoo.com/rss?category=news"),
        ("Yahoo台股動態", "https://tw.stock.yahoo.com/rss?category=tw-market"),
        ("Yahoo基金ETF", "https://tw.stock.yahoo.com/rss?category=funds-news"),
    ]
    max_days = max(_get_news_search_day_list())
    aliases = _get_news_aliases(stock_code, stock_name)
    articles = []
    seen = set()
    for family, url in urls:
        for item in _read_rss_items(url, family, max_items=max(30, max_items * 5)):
            combined = f"{item.get('title', '')} {item.get('description', '')}"
            if aliases and not any(a and a in combined for a in aliases):
                continue
            key = _article_seen_key(item.get("title", ""), item.get("url", ""))
            if key and key in seen:
                continue
            article = _build_external_rss_article(item, stock_code, stock_name, max_days, "yahoo_finance")
            if article is None:
                continue
            if key:
                seen.add(key)
            articles.append(article)
    articles = sorted(articles, key=lambda a: -int(a.get("relevance_score", 0) or 0))
    print(f"📰 Yahoo 股市 RSS：{stock_code} {stock_name}｜保留 {len(articles):,} 筆")
    return articles[:max(1, int(max_items))]


def fetch_bing_news_rss_articles(stock_code: str, stock_name: str, max_items: int = 8) -> List[dict]:
    if not NEWS_MULTI_SOURCE_ENABLE or not NEWS_BING_RSS_ENABLE:
        return []
    aliases = _get_news_aliases(stock_code, stock_name)
    quoted = [f'"{a}"' for a in aliases[:5] if a]
    target = " OR ".join(quoted) if quoted else f'"{stock_code}"'
    topics = (
        "營收 OR 獲利 OR 財報 OR 法說 OR 展望 OR 接單 OR 出貨 OR 產能 OR "
        "產品 OR AI OR ASIC OR 半導體 OR 目標價 OR 評等 OR ETF OR 成分股 OR 指數調整"
    )
    query = f"({target}) ({topics})"
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({
        "q": query,
        "format": "rss",
        "mkt": "zh-TW",
        "setlang": "zh-hant",
    })
    max_days = max(_get_news_search_day_list())
    items = _read_rss_items(url, "BingNews", max_items=max(30, max_items * 5))
    articles = []
    seen = set()
    for item in items:
        combined = f"{item.get('title', '')} {item.get('description', '')}"
        if aliases and not any(a and a in combined for a in aliases):
            continue
        key = _article_seen_key(item.get("title", ""), item.get("url", ""))
        if key and key in seen:
            continue
        article = _build_external_rss_article(item, stock_code, stock_name, max_days, "bing_news")
        if article is None:
            continue
        if key:
            seen.add(key)
        articles.append(article)
    articles = sorted(articles, key=lambda a: -int(a.get("relevance_score", 0) or 0))
    print(f"📰 Bing News RSS：{stock_code} {stock_name}｜保留 {len(articles):,} 筆")
    return articles[:max(1, int(max_items))]


def _extract_moneydj_search_candidates(page_html: str, stock_code: str, stock_name: str) -> List[dict]:
    aliases = _get_news_aliases(stock_code, stock_name)
    base_url = "https://www.moneydj.com/"
    candidates = []
    seen = set()

    def add_candidate(title: str, href: str, context: str = ""):
        title = _clean_news_title(title)
        href = html.unescape(str(href or "").strip())
        if not title or len(title) < 8 or not href:
            return
        combined = f"{title} {context}"
        if aliases and not any(a and a in combined for a in aliases):
            return
        low_href = href.lower()
        if not any(k in low_href for k in ["newsviewer", "/news/", "newsv", "newsv.aspx"]):
            return
        url = urllib.parse.urljoin(base_url, href)
        key = _article_seen_key(title, url)
        if key in seen:
            return
        seen.add(key)
        date_match = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", context)
        published = date_match.group(0) if date_match else ""
        candidates.append({
            "title": title,
            "url": url,
            "source": "MoneyDJ",
            "source_family": "MoneyDJ",
            "published": published,
            "description": _normalize_news_text(context),
        })

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(page_html, "lxml")
            for a in soup.find_all("a", href=True):
                title = a.get_text(" ", strip=True)
                parent_text = a.parent.get_text(" ", strip=True) if a.parent else title
                add_candidate(title, a.get("href", ""), parent_text)
        except Exception:
            pass

    if not candidates:
        for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html or ""):
            href = m.group(1)
            title = _normalize_news_text(_html_to_readable_text(m.group(2)))
            around = _normalize_news_text(_html_to_readable_text((page_html or "")[max(0, m.start()-120):m.end()+160]))
            add_candidate(title, href, around)
    return candidates


def fetch_moneydj_news_articles(stock_code: str, stock_name: str, max_items: int = 8) -> List[dict]:
    if not NEWS_MULTI_SOURCE_ENABLE or not NEWS_MONEYDJ_SEARCH_ENABLE:
        return []
    queries = []
    for q in [stock_name, stock_code]:
        q = str(q or "").strip()
        if q and q not in queries:
            queries.append(q)
    candidates = []
    seen = set()
    headers = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    for q in queries:
        url = "https://www.moneydj.com/kmdj/search/list.aspx?" + urllib.parse.urlencode({
            "_QueryType_": "NW",
            "_Query_": q,
        })
        try:
            r = get_thread_session().get(url, headers=headers, timeout=(5, NEWS_FETCH_TIMEOUT))
            r.raise_for_status()
            for c in _extract_moneydj_search_candidates(r.text, stock_code, stock_name):
                key = _article_seen_key(c.get("title", ""), c.get("url", ""))
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                candidates.append(c)
        except Exception as e:
            print(f"⚠️ MoneyDJ 新聞搜尋失敗：{q}｜{e}")

    articles = []
    max_days = max(_get_news_search_day_list())
    for candidate in candidates[:max(1, int(NEWS_MONEYDJ_BODY_FETCH_LIMIT))]:
        title = candidate.get("title", "")
        body = _fetch_article_body(candidate.get("url", ""))
        body_ok = (
            _is_valid_article_body(body, title=title, description=candidate.get("description", ""))
            and _passes_news_quality_gate(title, body, stock_code, stock_name)
        )
        if body_ok:
            content = body
            fallback_ok = False
            content_source = "moneydj_article"
        else:
            description = candidate.get("description", "")
            fallback_ok = _passes_news_quality_gate(title, description, stock_code, stock_name)
            if not fallback_ok:
                continue
            content = _build_fast_rss_news_content(title, description, source="MoneyDJ", published=candidate.get("published", ""))
            content_source = "moneydj_search"
        article = {
            **candidate,
            "content": content,
            "body_ok": bool(body_ok),
            "fallback_ok": bool(fallback_ok),
            "content_source": content_source,
            "body_length": len(content),
            "search_days": int(max_days),
            "query_stage": "MoneyDJ關鍵字搜尋",
        }
        article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name)
        articles.append(article)
    articles = sorted(articles, key=lambda a: -int(a.get("relevance_score", 0) or 0))
    print(f"📰 MoneyDJ 新聞搜尋：{stock_code} {stock_name}｜保留 {len(articles):,} 筆")
    return articles[:max(1, int(max_items))]



def _news_article_usable(article: dict) -> bool:
    return bool(article and (article.get("body_ok") or article.get("fallback_ok")))


def _count_distinct_usable_news_articles(articles: List[dict]) -> int:
    seen = set()
    for article in articles or []:
        if not _news_article_usable(article):
            continue
        key = _article_seen_key(article.get("title", ""), article.get("url", ""))
        if not key:
            key = _normalize_news_text(article.get("content", ""))[:120]
        if key:
            seen.add(key)
    return len(seen)


def _parse_mops_date(value):
    """解析 MOPS 常見民國日期（1150626、115/06/26）或西元日期。"""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = parse_date(s)
        if dt:
            return dt
    except Exception:
        pass
    digits = re.sub(r"\D", "", s)
    try:
        if len(digits) == 7:
            return datetime(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
        if len(digits) == 8:
            return datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except Exception:
        return None
    return None


def _mops_announcement_has_information_value(title: str, detail: str) -> bool:
    """排除只有例行格式、沒有可讀事件內容的公告；保留具體公司營運／財務／治理事件。"""
    combined = _normalize_news_text(f"{title} {detail}")
    if not combined:
        return False
    useful_keywords = [
        "營收", "財報", "獲利", "盈餘", "每股", "股利", "法說", "展望", "財測",
        "董事會", "投資", "擴產", "產能", "接單", "出貨", "產品", "合作", "合約",
        "取得", "處分", "資產", "增資", "減資", "庫藏股", "買回", "現金增資",
        "澄清", "媒體報導", "訴訟", "停工", "復工", "重大訊息", "組織重整",
        "供需", "價格", "報價", "客戶", "子公司", "併購", "股東會", "除權息",
    ]
    if any(k in combined for k in useful_keywords):
        return True
    # 說明欄夠長且有數字，通常代表確實有具體事件內容。
    return len(_normalize_news_text(detail)) >= 140 and bool(re.search(r"\d", combined))


def fetch_mops_material_info_articles(stock_code: str, stock_name: str, max_items: int = 8) -> List[dict]:
    """從公開資訊觀測站每日重大訊息補充公司直接公告；來源失敗不影響週報。"""
    if not NEWS_MULTI_SOURCE_ENABLE or not NEWS_MOPS_ENABLE:
        return []
    stock_key = _clean_code(stock_code)
    max_days = max(_get_news_search_day_list())
    cutoff = datetime.now() - timedelta(days=max_days)
    articles = []
    seen = set()
    headers = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    for market_name, url in NEWS_MOPS_ENDPOINTS:
        try:
            r = get_thread_session().get(url, headers=headers, timeout=(5, NEWS_FETCH_TIMEOUT))
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                continue
        except Exception as e:
            print(f"⚠️ MOPS {market_name}讀取失敗：{e}")
            continue

        for row in data:
            if not isinstance(row, dict):
                continue
            row_code = _clean_code(row.get("公司代號", row.get("公司代碼", "")))
            if row_code != stock_key:
                continue
            title = _clean_news_title(row.get("主旨", row.get("公告主旨", "")))
            detail = _normalize_news_text(row.get("說明", row.get("公告內容", "")))
            speech_date = row.get("發言日期", row.get("公告日期", row.get("事實發生日", "")))
            dt = _parse_mops_date(speech_date)
            if dt is not None and dt < cutoff:
                continue
            if not title or not _mops_announcement_has_information_value(title, detail):
                continue
            content = _normalize_news_text(
                f"公司公告：{title}。{detail}" if detail else f"公司公告：{title}。"
            )
            key = _article_seen_key(title, f"mops:{market_name}:{speech_date}:{title}")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            published = dt.strftime("%Y-%m-%d") if dt else str(speech_date or "")
            article = {
                "title": title,
                "url": "",
                "source": "公開資訊觀測站",
                "source_family": "MOPS",
                "published": published,
                "description": detail,
                "content": content,
                "body_ok": True,
                "fallback_ok": False,
                "content_source": "mops_openapi",
                "body_length": len(content),
                "search_days": int(max_days),
                "query_stage": market_name,
            }
            article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name) + 8
            articles.append(article)

    def sort_key(article: dict):
        dt = _parse_rss_pub_date(article.get("published", ""))
        if dt is None:
            try:
                dt = pd.Timestamp(article.get("published", "")).to_pydatetime()
            except Exception:
                dt = datetime.min
        return (-int(article.get("relevance_score", 0) or 0), -dt.timestamp() if dt != datetime.min else 0)

    articles = sorted(articles, key=sort_key)
    print(f"📰 MOPS 重大訊息：{stock_code} {stock_name}｜保留 {len(articles):,} 筆")
    return articles[:max(1, int(max_items))]


def _merge_and_rank_news_articles(article_groups: List[List[dict]], stock_code: str, stock_name: str, limit: int) -> List[dict]:
    merged = []
    seen = set()
    for group in article_groups:
        for article in group or []:
            key = _article_seen_key(article.get("title", ""), article.get("url", ""))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            article = dict(article)
            article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name)
            merged.append(article)

    def sort_key(a: dict):
        dt = _parse_rss_pub_date(a.get("published", "")) or datetime.min
        body_rank = 0 if a.get("body_ok") else 1 if a.get("fallback_ok") else 2
        return (-int(a.get("relevance_score", 0) or 0), body_rank, -dt.timestamp() if dt != datetime.min else 0)

    merged = sorted(merged, key=sort_key)

    # 先取各來源的第一篇，避免最後又全部只剩單一聚合來源；再依總分補滿。
    selected = []
    selected_ids = set()
    used_families = set()
    for article in merged:
        family = str(article.get("source_family", article.get("source", "unknown")) or "unknown")
        if family in used_families:
            continue
        selected.append(article)
        selected_ids.add(id(article))
        used_families.add(family)
        if len(selected) >= limit:
            return selected[:limit]
    for article in merged:
        if id(article) in selected_ids:
            continue
        selected.append(article)
        if len(selected) >= limit:
            break
    return selected[:limit]


def fetch_multi_source_news_articles(stock_code: str, stock_name: str, max_items: int = 10) -> List[dict]:
    """合併多來源新聞；至少蒐集 2 則不同合格事件後才視為素材充足。"""
    manual = os.getenv("WEEKLY_NEWS_TEXT", "").strip()
    if manual:
        return fetch_google_news_articles(stock_code, stock_name, max_items=max_items)
    if not NEWS_ENABLE:
        return []

    minimum_needed = max(1, int(NEWS_MIN_DISTINCT_ARTICLES))
    per_source = max(minimum_needed, 3, int(NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE))

    # 所有來源都會實際查詢，不因 Google 先找到一篇就停止。
    groups = [fetch_google_news_articles(stock_code, stock_name, max_items=max(max_items, minimum_needed))]
    if NEWS_MULTI_SOURCE_ENABLE:
        groups.append(fetch_yahoo_finance_rss_articles(stock_code, stock_name, max_items=per_source))
        groups.append(fetch_bing_news_rss_articles(stock_code, stock_name, max_items=per_source))
        groups.append(fetch_moneydj_news_articles(stock_code, stock_name, max_items=per_source))
        groups.append(fetch_mops_material_info_articles(stock_code, stock_name, max_items=max(NEWS_MOPS_MAX_ITEMS, per_source)))

    limit = max(
        minimum_needed,
        NEWS_SUMMARY_MAX_POINTS,
        NEWS_GOOGLE_MIN_USABLE_ARTICLES,
        min(max(1, int(NEWS_MULTI_SOURCE_RETURN_LIMIT)), max(minimum_needed, int(max_items))),
    )
    articles = _merge_and_rank_news_articles(groups, stock_code, stock_name, limit=limit)
    distinct_count = _count_distinct_usable_news_articles(articles)

    source_counts = {}
    for article in articles:
        family = str(article.get("source_family", article.get("source", "unknown")) or "unknown")
        source_counts[family] = source_counts.get(family, 0) + 1
    source_text = "、".join(f"{k}:{v}" for k, v in source_counts.items()) or "無"

    if distinct_count >= minimum_needed:
        print(
            f"📰 多來源新聞完成：{stock_code} {stock_name}｜保留 {len(articles):,} 筆｜"
            f"不同合格事件 {distinct_count:,} 則｜來源 {source_text}"
        )
    else:
        print(
            f"⚠️ 多來源與最長 {max(_get_news_search_day_list())} 日範圍均已查完："
            f"{stock_code} {stock_name} 僅取得 {distinct_count:,} 則不同合格事件；允許單則輸出｜來源 {source_text}"
        )
    return articles


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

    # 快速 RSS 模式先建立較完整候選池，避免前兩篇普通新聞使後面更具代表性的公司／ETF新聞被漏掉。
    # 慢速原文模式仍保守限制篇數，避免抓取時間過長。
    required_usable_count = fetch_limit if fast_mode else max(3, min(fetch_limit, int(NEWS_GOOGLE_MIN_USABLE_ARTICLES)))

    def enough_articles() -> bool:
        return usable_count_now() >= required_usable_count

    def build_article_from_candidate(candidate: dict) -> dict:
        title = candidate.get("title", "")
        link = candidate.get("url", "")
        description = candidate.get("description", "")
        days = int(candidate.get("search_days", 0) or 0)
        stage_label = candidate.get("query_stage", "")

        # 官方股票名稱短暫查不到時，從 RSS 標題 / 摘要的「公司名(代號)」反推別名，
        # 避免後續多股過濾把「辛耘(3583)」這類明確新聞誤判成非目標公司。
        inferred_name = _extract_company_name_near_code(f"{title} {description}", stock_code)
        if inferred_name and inferred_name not in STOCK_NEWS_ALIAS_MAP.get(str(stock_code).strip(), []):
            STOCK_NEWS_ALIAS_MAP.setdefault(str(stock_code).strip(), []).append(inferred_name)
            print(f"📰 新聞別名自動補充：{stock_code} → {inferred_name}")

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
            body_ok = (
                _is_valid_article_body(article_body, title=title, description=description)
                and _passes_news_quality_gate(title, article_body, stock_code, stock_name)
            )

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
            "source_family": "GoogleNews",
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
        article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name)
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
                if aliases and not _news_text_matches_target_stock(combined_for_target_check, stock_code, stock_name):
                    # Google News 搜尋有時會回傳同產業但非本股票的多股新聞；先擋掉標題/摘要沒有明確對應本股票的項目。
                    continue
                if not _passes_news_quality_gate(title, description, stock_code, stock_name):
                    # 排除只有漲跌、熱門股清單、大盤盤勢或缺乏具體公司資訊的新聞。
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
        relevance_rank = -int(article.get("relevance_score", 0) or 0)
        return (usable_rank, days_rank, relevance_rank, -published_dt.timestamp() if published_dt != datetime.min else 0)

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
            source_family = str(item.get("source_family", "") or "").strip()
            published = str(item.get("published", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            body_ok = bool(item.get("body_ok"))
            fallback_ok = bool(item.get("fallback_ok"))
            content_source = str(item.get("content_source", "") or "").strip()
            raw_content = _normalize_news_text(item.get("content", ""))
            content = raw_content if (body_ok or fallback_ok) else ""
            search_days = int(item.get("search_days", 0) or 0)
            query_stage = str(item.get("query_stage", "") or "").strip()
            relevance_score = int(item.get("relevance_score", 0) or 0)
            body_length = int(item.get("body_length", len(raw_content)) or 0)
        else:
            # 舊版相容：純字串只當標題，不拿來產生新聞重點。
            title = _clean_news_title(str(item))
            content = ""
            description = ""
            source = ""
            source_family = ""
            published = ""
            url = ""
            body_ok = False
            fallback_ok = False
            content_source = ""
            search_days = 0
            query_stage = ""
            relevance_score = 0
            body_length = 0
        if not title and not content and not description:
            continue
        records.append({
            "title": title,
            "content": content,
            "description": description,
            "source": source,
            "source_family": source_family,
            "published": published,
            "url": url,
            "body_ok": body_ok,
            "fallback_ok": fallback_ok,
            "content_source": content_source,
            "search_days": search_days,
            "query_stage": query_stage,
            "relevance_score": relevance_score,
            "body_length": body_length,
        })
    return records


def _is_bad_news_sentence(sentence: str) -> bool:
    """過濾新聞標題、三大法人清單、導流文字與非內文內容。"""
    s = _normalize_news_text(sentence)
    if not s or len(s) < 16:
        return True
    # 已整理成「分類｜結論｜重點｜觀察」的週報句，不能再用一般標題分隔符規則誤刪。
    structured_point = _is_structured_report_point(s)
    bad_keywords = [
        "完整看", "三大法人買賣超", "外資買超", "外資賣超", "投信買超", "投信賣超",
        "自營商買超", "自營商賣超", "買超排行", "賣超排行", "熱門股", "熱門新聞",
        "新聞標題", "點擊", "下載", "加入會員", "登入", "訂閱", "廣告", "版權",
        "看更多", "更多新聞", "延伸閱讀", "相關新聞", "Yahoo", "Facebook", "LINE分享",
        "焦點股", "優於大盤", "目標價曝光", "新目標價", "強勢股", "題材股",
        "關鍵字", "標籤", "追蹤我們", "追蹤我", "分享給朋友", "分享給好友",
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
    if (not structured_point) and re.search(r"[》｜|【】]", s) and len(s) <= 100:
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


_SIMILAR_COMPANY_NAME_SUFFIXES = (
    "生技", "生醫", "醫療", "製藥", "藥品", "醫材",
    "科技", "電子", "精密", "材料", "光電", "半導體",
    "國際", "控股", "投控", "建設", "營造", "工程",
    "化學", "電機", "機械", "工業", "資訊", "資通", "通訊",
)


def _has_target_stock_code_in_news(text: str, stock_code: str) -> bool:
    code = _clean_code(stock_code)
    if not code:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(code)}(?!\d)", str(text or "")))


def _has_conflicting_similar_company_name(text: str, stock_code: str, stock_name: str) -> bool:
    """排除名稱相近但不是同一檔股票的新聞。

    例如 3033 威健 與「威健生技」是不同公司；如果新聞沒有明確出現
    3033，不能只因為包含「威健」兩字就當成 3033 的新聞。
    """
    s = _normalize_news_text(text)
    if not s:
        return False
    if _has_target_stock_code_in_news(s, stock_code):
        return False

    code = _clean_code(stock_code)
    explicit_codes = set(re.findall(r"[（(]\s*(\d{4})\s*[)）]", s))
    if explicit_codes and code and code not in explicit_codes:
        # 同句有其他股票代號，且沒有本股票代號，保守視為非目標股票。
        return True

    aliases = [a for a in _get_news_aliases(stock_code, stock_name) if a]
    for alias in aliases:
        alias = str(alias or "").strip()
        if not alias or alias == code or re.fullmatch(r"\d+", alias):
            continue
        # 只處理短中文股票名被接成另一家公司名的情境，避免誤擋一般句子。
        if len(alias) < 2:
            continue
        for suffix in _SIMILAR_COMPANY_NAME_SUFFIXES:
            if re.search(rf"{re.escape(alias)}{re.escape(suffix)}", s):
                return True
    return False


def _news_text_matches_target_stock(text: str, stock_code: str, stock_name: str) -> bool:
    """判斷新聞文字是否明確對應目標股票。

    優先相信股票代號；若只有公司名，需排除「公司名 + 生技/科技/電子...」
    這類容易誤抓相似公司的情況。
    """
    s = _normalize_news_text(text)
    if not s:
        return False
    if _has_target_stock_code_in_news(s, stock_code):
        return True
    if _has_conflicting_similar_company_name(s, stock_code, stock_name):
        return False
    code = _clean_code(stock_code)
    for alias in _get_news_aliases(stock_code, stock_name):
        alias = str(alias or "").strip()
        if not alias or alias == code or re.fullmatch(r"\d+", alias):
            continue
        if alias in s:
            return True
    return False


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
        has_target = _news_text_matches_target_stock(sent, stock_code, stock_name)
        has_non_target = _contains_non_target_stock_alias(sent, stock_code, stock_name)
        has_cross_risk = _is_cross_company_target_value_sentence(sent, stock_code, stock_name)

        if has_target:
            # 先嘗試拆分句，避免「A 公司目標價、B 公司目標價」混在同一句。
            clauses = []
            for clause in _split_news_clauses(sent):
                clause = _strip_target_news_label(clause).strip("，,；;、 ")
                if not clause:
                    continue
                clause_has_target = _news_text_matches_target_stock(clause, stock_code, stock_name)
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
    if _news_text_matches_target_stock(content, stock_code, stock_name) and not _contains_non_target_stock_alias(content, stock_code, stock_name):
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
    s = re.sub(r"^市場觀察到", "", s).strip()
    s = s.replace("可能因", "受")
    s = s.replace("而獲得重新評價的機會", "，使市場關注度升溫")
    s = s.replace("重新評價的機會", "市場關注度升溫")
    s = s.replace("相關成長動能與市場關注度", "相關訂單、營收與市場關注度")
    return _finish_complete_summary_point(s, max_len=max_len, min_cut_len=32)

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
            if aliases and not _news_text_matches_target_stock(sent, stock_code, stock_name):
                # 規則式補字數時也必須明確指向本股票，避免拿相似公司或同篇新聞其他公司的題材來補。
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
    """清理新聞重點並排除跨公司數字、純盤勢或缺乏實質內容的句子。"""
    points = []
    for p in raw_points or []:
        s = _trim_news_point(p, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
        if not s or _is_bad_news_sentence(s):
            continue
        if _has_conflicting_similar_company_name(s, stock_code, stock_name):
            print(f"⚠️ 略過疑似相似公司名稱誤植的新聞重點：{s}")
            continue
        if _is_cross_company_target_value_sentence(s, stock_code, stock_name):
            print(f"⚠️ 略過疑似跨公司目標價 / 評等重點：{s}")
            continue
        if _is_low_value_market_news(s) and not _has_substantive_company_news(s):
            continue
        if not _has_substantive_company_news(s):
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
    sentence_candidates = _collect_news_sentences(records, stock_code, stock_name)
    title_candidates = _collect_news_title_candidates(records, stock_code, stock_name)
    candidates = list(sentence_candidates or [])
    seen_candidate_keys = {_title_compare_text(c.get("text", "")) for c in candidates if c.get("text")}
    for c in title_candidates or []:
        key = _title_compare_text(c.get("text", ""))
        if key and key not in seen_candidate_keys:
            candidates.append(c)
            seen_candidate_keys.add(key)
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
        if not _has_substantive_company_news(text):
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
        label = _infer_news_label_from_text(text, fallback_label="新聞焦點")
        point = _make_news_keypoint(label, text, stock_code, stock_name)
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
    """新聞品質優先：素材足夠時由程式端補到 3 點，不為字數硬塞無關內容。"""
    points = _clean_news_summary_points_for_stock(points, stock_code, stock_name)
    target_points = max(1, min(3, int(NEWS_SUMMARY_MAX_POINTS)))

    if len(points) >= target_points:
        return points[:NEWS_SUMMARY_MAX_POINTS]

    # Gemini 只有 0～2 點時，從同批已通過品質門檻的素材補真正不同的事件。
    expanded = points[:]
    for p in _build_news_expansion_points(records, stock_code, stock_name, used_points=expanded):
        if len(expanded) >= target_points:
            break
        if p not in expanded:
            expanded.append(p)
    return _clean_news_summary_points_for_stock(expanded, stock_code, stock_name)[:NEWS_SUMMARY_MAX_POINTS]

def _parse_gemini_news_points(output_text: str, records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """只讀取 Gemini JSON 的 points；note 僅供內部說明，絕不可畫進新聞區。"""
    parsed = _extract_json_from_text(output_text)
    if isinstance(parsed, dict):
        raw_points = parsed.get("points", []) or []
    elif isinstance(parsed, list):
        raw_points = parsed
    else:
        # 只有非 JSON 回覆才允許退回逐行解析；避免 {"points": [], "note": ...}
        # 被當成一般文字後將 note 誤畫進圖片。
        raw_points = _parse_raw_points_from_llm(output_text)
    raw_points = [str(p) for p in raw_points if str(p or "").strip()]
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



def _build_gemini_points_response_schema(
    min_points: int = 1,
    max_points: int = 3,
    include_note: bool = False,
) -> dict:
    """建立新聞與本週重點共用的 Gemini JSON Schema。"""
    min_points = max(0, int(min_points or 0))
    max_points = max(min_points, int(max_points or min_points or 1))
    properties = {
        "points": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": min_points,
            "maxItems": max_points,
        },
    }
    if include_note:
        properties["note"] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": ["points"],
    }


_GROUNDED_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)


def _canonical_grounded_number_token(value: str) -> str:
    """將 1,200.00、+1200、01200 等數字統一成可比對格式。"""
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return ""

    sign = ""
    if raw[0] in "+-":
        sign = "-" if raw[0] == "-" else ""
        raw = raw[1:]

    if not raw or not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return ""

    if "." in raw:
        integer, decimal = raw.split(".", 1)
        integer = integer.lstrip("0") or "0"
        decimal = decimal.rstrip("0")
        normalized = f"{integer}.{decimal}" if decimal else integer
    else:
        normalized = raw.lstrip("0") or "0"

    if normalized == "0":
        sign = ""
    return sign + normalized


def _extract_grounded_number_tokens(text_or_payload) -> List[str]:
    """抽出文字或 JSON payload 中所有阿拉伯數字字串。"""
    if isinstance(text_or_payload, (dict, list, tuple)):
        source_text = json.dumps(text_or_payload, ensure_ascii=False, sort_keys=True)
    else:
        source_text = str(text_or_payload or "")
    source_text = _normalize_chinese_numbers_for_news(source_text)

    out = []
    for matched in _GROUNDED_NUMBER_RE.findall(source_text):
        token = _canonical_grounded_number_token(matched)
        if token:
            out.append(token)
    return out


def _build_grounded_number_source_set(source_text_or_payload) -> set:
    """建立可接受數字集合；來源有正負號時，也允許輸出用文字表達方向後省略正號或負號。"""
    source_tokens = set()
    for token in _extract_grounded_number_tokens(source_text_or_payload):
        source_tokens.add(token)
        if token.startswith("-"):
            source_tokens.add(token[1:])
    return source_tokens


def _find_ungrounded_number_tokens(
    points: List[str],
    source_text_or_payload,
) -> Dict[int, List[str]]:
    """找出 AI 輸出中沒有出現在輸入素材的數字。"""
    allowed = _build_grounded_number_source_set(source_text_or_payload)
    problems = {}
    for idx, point in enumerate(points or []):
        missing = []
        for token in _extract_grounded_number_tokens(str(point or "")):
            if token not in allowed and token not in missing:
                missing.append(token)
        if missing:
            problems[idx] = missing
    return problems


def _format_ungrounded_number_problems(problems: Dict[int, List[str]]) -> str:
    parts = []
    for idx, tokens in sorted((problems or {}).items()):
        parts.append(f"第{idx + 1}點：" + "、".join(tokens))
    return "；".join(parts)


def _filter_points_with_grounded_numbers(
    points: List[str],
    source_text_or_payload,
    label: str,
) -> List[str]:
    problems = _find_ungrounded_number_tokens(points, source_text_or_payload)
    if not problems:
        return list(points or [])

    for idx, tokens in sorted(problems.items()):
        point_text = str((points or [""])[idx] or "")[:80]
        print(
            f"⚠️ {label}第 {idx + 1} 點含輸入素材找不到的數字 "
            f"{'、'.join(tokens)}，已丟棄：{point_text}"
        )
    return [
        point
        for idx, point in enumerate(points or [])
        if idx not in problems
    ]


def _build_news_number_grounding_source(usable_articles: List[dict]) -> str:
    """數字接地只看實際送入 Gemini 的標題、日期與正文，不讓 A1/A2 文章 ID 誤放行。"""
    chunks = []
    for article in usable_articles or []:
        chunks.extend([
            str(article.get("title", "") or ""),
            str(article.get("published", "") or ""),
            str(article.get("body", "") or ""),
        ])
    return "\n".join(chunks)


def _call_gemini_with_retry(
    prompt: str,
    cache_task: str = "",
    stock_code: str = "",
    stock_name: str = "",
    write_cache: bool = True,
    response_schema: dict | None = None,
    temperature: float | None = None,
):
    # 第一優先：Google Sheet 每日快取。
    # 同股票、同任務、同模型、同一天只要跑過一次，當天再跑就不會重打 Gemini。
    # 這段必須放在 GEMINI_ENABLE 判斷前面，避免關閉 Gemini 時連當日快取也讀不到。
    cached_text = load_gsheet_llm_cache(cache_task, stock_code, stock_name, prompt) if cache_task and stock_code else ""
    if cached_text:
        save_llm_cache(prompt, cached_text)
        return cached_text

    # 第二優先：本機 prompt hash 快取。
    # 若本機命中，也順便補寫 Google Sheet，讓下次不同 runner 也能直接命中。
    # 但當 Action 設定 WARRANT_LLM_CACHE_FORCE_REFRESH=1 時，必須連本機快取也跳過，
    # 否則使用者選 1 仍可能吃到同 prompt 的舊壞結果。
    cached_text = "" if LLM_CACHE_FORCE_REFRESH else load_llm_cache(prompt)
    if cached_text:
        if write_cache and cache_task and stock_code:
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

    generation_config = {}
    use_temperature = GEMINI_ANALYSIS_TEMPERATURE if temperature is None else float(temperature)
    generation_config["temperature"] = max(0.0, min(2.0, use_temperature))
    if GEMINI_STRUCTURED_OUTPUT_ENABLE and response_schema:
        generation_config["response_mime_type"] = "application/json"
        generation_config["response_schema"] = response_schema

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
                json_mode_label = "JSON Schema" if response_schema and GEMINI_STRUCTURED_OUTPUT_ENABLE else "文字模式"
                print(
                    f"Gemini 呼叫第 {attempt}/{GEMINI_RETRY_TIMES} 次，模型：{GEMINI_MODEL}｜"
                    f"API Key {key_idx}/{total_keys}｜{json_mode_label}｜temperature={generation_config['temperature']:g}"
                )
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=generation_config,
                )
                output_text = response.text or ""
                if write_cache:
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



def _is_fetchable_news_article_url(url: str) -> bool:
    """只把具備 http(s) scheme 與網域的網址送入原文抓取。"""
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlparse(raw)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _is_direct_fetchable_news_article_url(url: str) -> bool:
    """混合模式只補抓直接新聞網址；Google News 導流網址直接略過，避免無效解碼與等待。"""
    if not _is_fetchable_news_article_url(url):
        return False
    try:
        host = (urllib.parse.urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host == "google.com" or host.endswith(".google.com") or host == "news.google.com":
        return False
    return True


def _hybrid_news_event_key(record: dict, stock_code: str = "", stock_name: str = "") -> str:
    """建立混合模式用的事件鍵，避免同一營收或公司事件重複占滿 top-K。"""
    text = _normalize_news_text(
        f"{record.get('title', '')} {record.get('description', '')} {record.get('content', '')}"
    )
    compact = _title_compare_text(text)
    if not compact:
        return str(record.get("url", "") or "").strip()

    # 月營收新聞常有多個來源與不同標題，但屬於同一事件。
    if "營收" in text:
        month_match = re.search(r"(?:20\d{2}[年/.-]?)?0?([1-9]|1[0-2])月", text)
        month_key = month_match.group(1) if month_match else "unknown"
        # 同一個月營收常有些標題帶年份、有些不帶；混合補抓時以月份視為同一事件。
        return f"revenue:{month_key}"

    event_families = [
        ("board", ["董事會", "股東會", "決議"]),
        ("dividend", ["股利", "配息", "除息", "除權"]),
        ("earnings", ["財報", "獲利", "EPS", "每股盈餘"]),
        ("guidance", ["法說", "展望", "財測"]),
        ("capacity", ["擴產", "新產能", "量產", "新廠"]),
        ("order", ["接單", "訂單", "出貨"]),
        ("product", ["新品", "新產品", "新平台"]),
        ("etf_index", ["ETF", "指數", "成分股", "被動資金"]),
    ]
    for family, keywords in event_families:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            return f"{family}:{str(record.get('published', '') or '')[:10]}"

    for removable in [stock_code, stock_name, "新聞", "即時", "公告", "MoneyDJ理財網"]:
        if removable:
            compact = compact.replace(_title_compare_text(removable), "")
    compact = re.sub(r"\d+(?:\.\d+)?", "", compact)
    return compact[:48] or str(record.get("url", "") or "").strip()


def _enrich_fast_news_records_with_topk_bodies(
    records: List[dict],
    stock_code: str,
    stock_name: str,
) -> List[dict]:
    """RSS 快掃後只替最高分的前幾篇補抓原文，兼顧速度與摘要具體度。"""
    enriched = [dict(record) for record in (records or [])]
    if (
        not NEWS_FAST_MODE
        or not NEWS_FAST_HYBRID_BODY_FETCH_ENABLE
        or NEWS_FAST_HYBRID_BODY_FETCH_TOPK <= 0
        or not enriched
    ):
        return enriched

    candidates_by_event = {}
    for idx, record in enumerate(enriched):
        if record.get("body_ok") or not record.get("fallback_ok"):
            continue
        url = str(record.get("url", "") or "").strip()
        if not _is_direct_fetchable_news_article_url(url):
            continue
        if str(record.get("content_source", "") or "") == "manual":
            continue

        combined = _normalize_news_text(
            f"{record.get('title', '')} {record.get('description', '')} {record.get('content', '')}"
        )
        if not _news_text_matches_target_stock(combined, stock_code, stock_name):
            continue
        if not _passes_news_quality_gate(
            record.get("title", ""),
            record.get("content", record.get("description", "")),
            stock_code,
            stock_name,
        ):
            continue

        relevance = int(record.get("relevance_score", 0) or 0)
        if relevance == 0:
            relevance = _score_news_article_relevance(record, stock_code, stock_name)
        published_dt = _parse_rss_pub_date(record.get("published", "")) or datetime.min
        host = (urllib.parse.urlparse(url).netloc or "").lower()
        direct_url_rank = 0 if "news.google." in host else 1
        event_key = _hybrid_news_event_key(record, stock_code, stock_name) or f"row:{idx}"
        candidate = (idx, relevance, published_dt, direct_url_rank, event_key)

        # 同一事件只保留一篇；有直接原文網址時優先於 Google News 導流網址。
        previous = candidates_by_event.get(event_key)
        candidate_quality = (
            direct_url_rank,
            relevance,
            published_dt.timestamp() if published_dt != datetime.min else 0,
            -idx,
        )
        if previous is None:
            candidates_by_event[event_key] = candidate
        else:
            prev_idx, prev_relevance, prev_dt, prev_direct_rank, _ = previous
            previous_quality = (
                prev_direct_rank,
                prev_relevance,
                prev_dt.timestamp() if prev_dt != datetime.min else 0,
                -prev_idx,
            )
            if candidate_quality > previous_quality:
                candidates_by_event[event_key] = candidate

    if not candidates_by_event:
        print("ℹ️ RSS 混合模式沒有可直接存取的新聞原文網址，略過原文補抓")
        return enriched

    candidates = sorted(
        candidates_by_event.values(),
        key=lambda item: (
            -item[1],
            -item[3],
            -(item[2].timestamp() if item[2] != datetime.min else 0),
            item[0],
        ),
    )[:NEWS_FAST_HYBRID_BODY_FETCH_TOPK]

    print(
        f"📰 RSS 混合模式：從 {len(enriched):,} 筆素材中，"
        f"事件去重後替最高分 {len(candidates):,} 篇補抓原文"
    )
    fetch_started = time.perf_counter()
    batch_timeout = max(1.0, float(NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT))
    batch_deadline = fetch_started + batch_timeout
    upgraded = 0
    attempted = 0

    # 不再使用 ThreadPoolExecutor：requests 執行緒無法被安全強制終止，
    # 即使 future.cancel() 成功印出，Python 仍可能在收尾時等待卡住的執行緒。
    # 這裡改成最多 3 篇循序補抓，每篇與整批皆有硬上限，預設則完全不執行此功能。
    for candidate_pos, (idx, _, _, _, _) in enumerate(candidates):
        remaining_batch = batch_deadline - time.perf_counter()
        if remaining_batch <= 0:
            remaining_count = len(candidates) - candidate_pos
            print(f"⚠️ RSS 混合模式批次逾時，略過剩餘 {remaining_count:,} 篇")
            break

        record = enriched[idx]
        attempted += 1
        per_request_timeout = min(
            float(NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT),
            max(1.0, remaining_batch),
        )
        try:
            body = _fetch_article_body(
                str(record.get("url", "") or "").strip(),
                request_timeout=per_request_timeout,
                max_bytes=NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES,
                hard_deadline_seconds=remaining_batch,
            )
        except Exception as e:
            print(f"⚠️ RSS 混合模式原文抓取失敗：{record.get('title', '')[:36]}｜{e}")
            continue

        body = _normalize_news_text(body)
        body_ok = (
            _is_valid_article_body(
                body,
                title=record.get("title", ""),
                description=record.get("description", ""),
            )
            and _passes_news_quality_gate(
                record.get("title", ""),
                body,
                stock_code,
                stock_name,
            )
        )
        if not body_ok:
            print(f"ℹ️ RSS 混合模式未取得可用原文，保留摘要：{record.get('title', '')[:36]}")
            continue

        record["content"] = body
        record["body_ok"] = True
        record["fallback_ok"] = False
        record["content_source"] = "hybrid_article"
        record["body_length"] = len(body)
        record["relevance_score"] = _score_news_article_relevance(record, stock_code, stock_name)
        upgraded += 1
        print(f"✅ RSS 混合模式補到原文：{record.get('title', '')[:36]}｜{len(body):,} 字")

    # 原文優先，再依相關性排序；相同條件保留原始順序。
    indexed = list(enumerate(enriched))
    indexed.sort(
        key=lambda item: (
            0 if item[1].get("body_ok") else 1,
            -int(item[1].get("relevance_score", 0) or 0),
            item[0],
        )
    )
    elapsed = time.perf_counter() - fetch_started
    print(f"⏱️ RSS 混合模式原文補抓：{elapsed:.2f} 秒｜成功 {upgraded}/{attempted} 篇")
    return [record for _, record in indexed]


def _build_gemini_news_articles(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    """只把可用的新聞素材送給 Gemini，並先萃取本股票相關片段，避免多家公司新聞數字混用。

    修正重點：
    1. 極速 RSS 模式本來只有標題 / 摘要，不能用原文模式的 40～80 字門檻硬擋。
    2. 若官方名稱暫時查不到，從「公司名(代號)」格式自動補別名。
    3. 標題或摘要明確包含本股票代號 / 名稱，且有營收、法說、接單、目標價等公司資訊時，允許短素材進 Gemini。
    """
    usable = []
    ordered = [r for r in records if r.get("body_ok") or r.get("fallback_ok")]
    for rec in ordered:
        title = _clean_news_title(rec.get("title", ""))
        raw_content = _normalize_news_text(rec.get("content", ""))
        description = _normalize_news_text(rec.get("description", ""))
        content_source = str(rec.get("content_source", ""))
        is_fast_rss = content_source in ("google_news_rss_fast", "rss_description", "manual")

        inferred_name = _extract_company_name_near_code(f"{title} {description} {raw_content}", stock_code)
        if inferred_name and inferred_name not in STOCK_NEWS_ALIAS_MAP.get(str(stock_code).strip(), []):
            STOCK_NEWS_ALIAS_MAP.setdefault(str(stock_code).strip(), []).append(inferred_name)
            print(f"📰 新聞別名自動補充：{stock_code} → {inferred_name}")

        aliases = _get_news_aliases(stock_code, stock_name)
        combined_for_check = _normalize_news_text("。".join([title, description, raw_content]))
        if _has_conflicting_similar_company_name(combined_for_check, stock_code, stock_name):
            print(f"⚠️ 略過相似公司新聞：{title[:36]}｜未明確對應 {stock_code} {stock_name}")
            continue
        has_target = _news_text_matches_target_stock(combined_for_check, stock_code, stock_name)
        has_value = _has_company_value_terms(combined_for_check) or _has_substantive_company_news(combined_for_check)

        # 極速 RSS 模式至少保留標題 + 摘要，不因摘要短就直接跳過。
        if not raw_content and is_fast_rss:
            raw_content = _build_fast_rss_news_content(
                title,
                description,
                source=rec.get("source", ""),
                published=rec.get("published", ""),
            )

        min_content_len = 18 if is_fast_rss else 80
        if len(raw_content) < min_content_len and not (is_fast_rss and has_target and has_value):
            continue

        focused_content = _extract_target_focused_news_body(raw_content, stock_code, stock_name)
        focused_norm = _normalize_news_text(focused_content)

        # 對 RSS 短素材做保守 fallback：標題 / 摘要已明確包含本股票與公司資訊時，保留原素材。
        if len(focused_norm) < 40 and is_fast_rss and has_target and has_value:
            focused_content = raw_content
            focused_norm = _normalize_news_text(focused_content)

        if len(focused_norm) < (20 if is_fast_rss else 40):
            print(f"⚠️ 略過多股混雜新聞：{title[:36]}｜找不到足夠的 {stock_code} {stock_name} 明確片段")
            continue

        usable.append({
            "id": f"A{len(usable) + 1}",
            "source": rec.get("source", ""),
            "title": title,
            "published": rec.get("published", ""),
            "url": rec.get("url", ""),
            "content_source": content_source,
            "target_aliases": aliases,
            "body": focused_content[:NEWS_MAX_ARTICLE_CHARS_TO_GEMINI],
        })
        if len(usable) >= NEWS_MAX_ARTICLES_TO_GEMINI:
            break
    return usable


def _save_validated_news_points_cache(
    task: str,
    stock_code: str,
    stock_name: str,
    prompt: str,
    points: List[str],
    note: str = "validated",
):
    # 只把已通過解析與品質檢查的新聞重點寫入 Google Sheet 快取。
    valid_points = _clean_news_summary_points_for_stock(points or [], stock_code, stock_name)
    valid_points = [p for p in valid_points if _points_are_independent_and_complete([p])]
    if not valid_points:
        print("⚠️ 新聞重點未通過品質檢查，不寫入 Gemini 快取")
        return
    payload = {
        "points": valid_points[:NEWS_DISPLAY_MAX_POINTS],
        "note": note,
    }
    save_gsheet_llm_cache(
        task,
        stock_code,
        stock_name,
        prompt,
        json.dumps(payload, ensure_ascii=False),
    )


def _summarize_news_with_gemini(records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """將合格新聞交給 Gemini；只快取格式、內容與數字接地皆通過的結果。"""
    usable_articles = _build_gemini_news_articles(records, stock_code, stock_name)
    if not usable_articles:
        print("⚠️ 沒有足夠且具體的公司新聞可送入 Gemini；不使用標題或盤勢新聞硬湊")
        return []

    minimum_points = min(
        NEWS_SUMMARY_MAX_POINTS,
        max(1, NEWS_MIN_DISTINCT_ARTICLES),
        max(1, len(usable_articles)),
    )
    display_name = stock_name if stock_name else stock_code
    article_json = json.dumps(usable_articles, ensure_ascii=False, indent=2)
    number_grounding_source = _build_news_number_grounding_source(usable_articles)
    response_schema = _build_gemini_points_response_schema(
        min_points=minimum_points,
        max_points=NEWS_SUMMARY_MAX_POINTS,
        include_note=True,
    )

    prompt = f"""
你是台股財經新聞編輯。只能使用下方素材，整理 {stock_code} {display_name} 的新聞／題材重點，使用繁體中文。

分析原則：
1. 輸出 {minimum_points}～{NEWS_SUMMARY_MAX_POINTS} 點不同事件；同一事件多來源合併，不得拿股價漲跌、熱門排行或大盤盤勢湊數。
2. 每點固定為「分類｜結果：具體結論｜說明：關鍵事實與後續追蹤。」；結果不可寫「重點待確認」「題材待觀察」等空句。
3. 公司身分必須明確對應代號 {stock_code} 或名稱 {display_name}；相似公司名但沒有代號時排除，不得混用其他公司的營收、EPS、目標價或題材。
4. 所有數字必須使用阿拉伯數字，而且必須原樣存在於素材；不得換算、推估或補充素材沒有的數字。
5. 只寫公司新聞、重大訊息、營運、產業供需或具體法人觀點；不得寫權證、分點、K線、均線、買賣建議、網址或媒體資訊。
6. 每點需獨立完整、自然收尾並以句號結束；字數與過長內容由程式端清理，你只需優先保留最具體的事實。

好範例：
- 公司動態｜結果：新訂單提高能見度｜說明：公司取得新案，後續觀察出貨時程與營收認列進度。
- 業績更新｜結果：營收成長仍待延續｜說明：營收維持年增但月減，後續看成長動能與毛利率變化。

壞範例：
- 題材觀察｜結果：題材仍待確認｜說明：後續持續關注。
- 法人觀點｜結果：市場偏多｜說明：公司未來值得期待。

只回傳符合 JSON Schema 的 JSON，不要 markdown 或其他說明。

新聞素材：
{article_json}
"""
    print("=" * 100)
    print("開始呼叫 Gemini 統整高品質新聞重點")
    print(f"模型：{GEMINI_MODEL}")
    print(f"送入 Gemini 的文章數：{len(usable_articles)}｜最低輸出：{minimum_points} 點")
    print("=" * 100)
    output_text = _call_gemini_with_retry(
        prompt,
        cache_task=_news_points_cache_task(),
        stock_code=stock_code,
        stock_name=stock_name,
        write_cache=False,
        response_schema=response_schema,
        temperature=GEMINI_ANALYSIS_TEMPERATURE,
    )
    points = _parse_gemini_news_points(output_text or "", records, stock_code, stock_name)

    problems = []
    if len(points) < minimum_points:
        problems.append(f"只輸出 {len(points)} 點，至少需要 {minimum_points} 點")
    if points and not _points_are_independent_and_complete(points):
        problems.append("出現承接句、半句或省略號")
    ungrounded = _find_ungrounded_number_tokens(points, number_grounding_source)
    if ungrounded:
        problems.append("含素材找不到的數字：" + _format_ungrounded_number_problems(ungrounded))

    if problems:
        print("⚠️ Gemini 新聞重點需要補正：" + "；".join(problems))
        repair_payload = {
            "problems": problems,
            "original_points": points,
            "articles": usable_articles,
        }
        repair_prompt = f"""
你是台股財經新聞編輯。上一版輸出未通過檢查，請只依修正資料重新整理 {stock_code} {display_name}。

修正原則：
1. 輸出 {minimum_points}～{NEWS_SUMMARY_MAX_POINTS} 點不同事件，固定格式為「分類｜結果：具體結論｜說明：關鍵事實與後續追蹤。」。
2. 每點必須獨立完整並以句號結束，不得使用空泛結果或承接上一點。
3. 公司必須明確對應 {stock_code} {display_name}，不得混入相似公司或其他公司的資料。
4. 每個阿拉伯數字都必須在 articles 的 title、published 或 body 中找到完全相同的數字；找不到就刪除該數字或改寫該點。
5. 不得寫技術分析、權證、分點、買賣建議、網址或外部資訊。
6. 只回傳符合 JSON Schema 的 JSON。

好範例：
- 公司動態｜結果：新訂單提高能見度｜說明：公司取得新案，後續觀察出貨時程與營收認列進度。
壞範例：
- 題材觀察｜結果：重點待確認｜說明：後續持續關注。

修正資料：
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
"""
        repaired_text = _call_gemini_with_retry(
            repair_prompt,
            cache_task=f"{_news_points_cache_task()}_repair_v18",
            stock_code=stock_code,
            stock_name=stock_name,
            write_cache=False,
            response_schema=response_schema,
            temperature=GEMINI_ANALYSIS_TEMPERATURE,
        )
        repaired = _parse_gemini_news_points(repaired_text or "", records, stock_code, stock_name)
        repaired_ungrounded = _find_ungrounded_number_tokens(
            repaired,
            number_grounding_source,
        )
        repaired_ok = (
            len(repaired) >= minimum_points
            and _points_are_independent_and_complete(repaired)
            and not repaired_ungrounded
        )
        if repaired_ok:
            points = repaired
            problems = []
            print(f"✅ Gemini 新聞補正完成：{len(points)} 點")
        else:
            if repaired_ungrounded:
                print(
                    "⚠️ Gemini 新聞補正後仍有無依據數字："
                    + _format_ungrounded_number_problems(repaired_ungrounded)
                )
            # 補正仍失敗時，先保留有接地且完整的點，其餘交給既有規則式摘要補足。
            candidate_points = repaired if len(repaired) >= len(points) else points
            points = _filter_points_with_grounded_numbers(
                candidate_points,
                number_grounding_source,
                "新聞重點",
            )
            points = [
                point
                for point in points
                if _points_are_independent_and_complete([point])
            ]

    final_ungrounded = _find_ungrounded_number_tokens(
        points,
        number_grounding_source,
    )
    fully_valid = (
        len(points) >= minimum_points
        and _points_are_independent_and_complete(points)
        and not final_ungrounded
    )
    if fully_valid:
        _save_validated_news_points_cache(
            _news_points_cache_task(),
            stock_code,
            stock_name,
            prompt,
            points,
            note=f"validated_news_points_json_grounded_{len(points)}",
        )
        print(f"✅ Gemini 新聞重點完成：{len(points)} 點，總字數約 {_count_summary_chars(points)} 字")
    elif points:
        print(f"⚠️ Gemini 新聞重點只有 {len(points)} 點或未通過完整驗證，不寫入快取，交給規則式摘要補足")
    return points


def _safe_float(v, default=np.nan):
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _build_weekly_top5_ai_rows(ctx: dict) -> List[dict]:
    """整理實際顯示的 TOP5，並提供金額代表性與歷史統計供 AI 判斷。"""
    cache_key = "_weekly_top5_ai_rows_cache"
    if cache_key in ctx:
        return [dict(row) for row in (ctx.get(cache_key) or [])]

    week_events = ctx.get("week_events")
    if week_events is None or week_events.empty:
        ctx[cache_key] = []
        return []

    buy_top, sell_top = _get_cached_top_branch_tables(ctx, "current_week", week_events, topn=5)
    perf_df = read_gsheet_branch_perf_df(force_refresh=False)
    perf_map = {}
    if perf_df is not None and not perf_df.empty:
        perf_map = {
            str(r.get("branch", "") or ""): r
            for _, r in perf_df.iterrows()
            if str(r.get("branch", "") or "")
        }

    all_amounts = []
    for df_top in [buy_top, sell_top]:
        if df_top is not None and not df_top.empty:
            all_amounts.extend(pd.to_numeric(df_top["net_amount"], errors="coerce").fillna(0.0).abs().tolist())
    overall_largest_abs = max(all_amounts) if all_amounts else 0.0
    rows = []

    def append_rows(df_top: pd.DataFrame, side: str):
        if df_top is None or df_top.empty:
            return
        side_amounts = pd.to_numeric(df_top["net_amount"], errors="coerce").fillna(0.0).abs()
        same_side_total_abs = float(side_amounts.sum())
        same_side_largest_abs = float(side_amounts.max()) if len(side_amounts) else 0.0

        for rank, (_, r) in enumerate(df_top.iterrows(), 1):
            branch = str(r.get("branch", "") or "").strip()
            branch_norm = normalize_branch_name(branch)
            perf = perf_map.get(branch_norm)
            net_value = pd.to_numeric(r.get("net_amount", 0), errors="coerce")
            net_amount = 0.0 if pd.isna(net_value) else float(net_value)
            abs_amount = abs(net_amount)
            overall_ratio = abs_amount / overall_largest_abs if overall_largest_abs > 0 else 0.0
            side_share = abs_amount / same_side_total_abs if same_side_total_abs > 0 else 0.0
            side_ratio = abs_amount / same_side_largest_abs if same_side_largest_abs > 0 else 0.0

            amount_representative = bool(overall_ratio >= max(0.0, float(WEEKLY_KEYPOINT_BRANCH_MIN_OVERALL_RATIO)))
            if amount_representative:
                amount_significance = "主要代表"
            elif rank <= 3 or overall_ratio >= 0.15:
                amount_significance = "次要參考"
            else:
                amount_significance = "金額偏小"

            row = {
                "side": "買超" if side == "buy" else "賣超",
                "rank": int(rank),
                "branch": branch,
                "weekly_net": fmt_money(net_amount),
                "weekly_net_value": net_amount,
                "absolute_amount": abs_amount,
                "relative_to_overall_largest_pct": round(overall_ratio * 100, 2),
                "relative_to_same_side_largest_pct": round(side_ratio * 100, 2),
                "share_of_same_side_top5_pct": round(side_share * 100, 2),
                "amount_significance": amount_significance,
                "amount_representative": amount_representative,
                "representative_warrant": f"{str(r.get('max_warrant_code', '') or '')} {str(r.get('max_warrant_name', '') or '')[:12]}".strip(),
                "representative_warrant_net": fmt_money(float(r.get("max_warrant_amount", 0) or 0)),
                "historical_statistics_available": bool(perf is not None and amount_representative),
                "eligible_for_individual_analysis": bool(amount_representative),
            }

            if perf is not None and amount_representative:
                win_rate = _parse_percent_like_value(perf.get("win_rate"), ratio_if_small=True)
                weighted_return = _parse_percent_like_value(perf.get("weighted_return"), ratio_if_small=True)
                avg_holding_days = _parse_number_like_value(perf.get("avg_holding_days"))
                event_count = _parse_number_like_value(perf.get("event_count"))
                row.update({
                    "historical_win_rate": fmt_pct(win_rate) if np.isfinite(win_rate) else "-",
                    "historical_weighted_return": fmt_pct(weighted_return) if np.isfinite(weighted_return) else "-",
                    "average_holding_days": _format_avg_holding_days(avg_holding_days),
                    "holding_period_interpretation": _describe_branch_holding_style(avg_holding_days),
                    "event_count": int(round(event_count)) if np.isfinite(event_count) else None,
                    "historical_analysis_priority": bool(amount_representative),
                })
            rows.append(row)

    append_rows(buy_top, "buy")
    append_rows(sell_top, "sell")
    ctx[cache_key] = [dict(row) for row in rows]
    return rows


def _calculate_weighted_volume_profile_stats(df: pd.DataFrame, n_bins: int = 40) -> dict:
    """使用與 K 線價量累積圖完全相同的算法，計算最大量區與第二大量區。"""
    required_cols = {"Low", "High", "Open", "Close", "Volume"}
    if df is None or df.empty or not required_cols.issubset(df.columns):
        return {}

    work = df[["Low", "High", "Open", "Close", "Volume"]].copy()
    for col in work.columns:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["Low", "High", "Open", "Close", "Volume"])
    if work.empty:
        return {}

    price_min = float(work["Low"].min())
    price_max = float(work["High"].max())
    if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
        return {}

    n_bins = max(5, int(n_bins or 40))
    bins = np.linspace(price_min, price_max, n_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2
    height = float(bins[1] - bins[0])
    profile = np.zeros(n_bins, dtype=float)

    for _, row in work.iterrows():
        vol = float(row["Volume"])
        low = float(row["Low"])
        high = float(row["High"])
        open_ = float(row["Open"])
        close = float(row["Close"])
        body_min, body_max = min(open_, close), max(open_, close)
        ranges = [((low, body_min), 0.2), ((body_min, body_max), 0.6), ((body_max, high), 0.2)]
        for (start, end), weight in ranges:
            if end - start < 1e-6:
                continue
            idxs = np.where((centers >= start) & (centers <= end))[0]
            if len(idxs):
                profile[idxs] += vol * weight / len(idxs)

    if len(profile) == 0 or float(profile.max()) <= 0:
        return {}

    sorted_idx = np.argsort(profile)[::-1]
    max_idx = int(sorted_idx[0]) if len(sorted_idx) else -1
    second_idx = int(sorted_idx[1]) if len(sorted_idx) > 1 else -1
    if max_idx < 0:
        return {}

    return {
        "work": work,
        "bins": bins,
        "centers": centers,
        "height": height,
        "profile": profile,
        "max_idx": max_idx,
        "second_idx": second_idx,
    }


def _price_zone_relation(close: float, zone_low: float, zone_high: float) -> str:
    if not np.isfinite(close):
        return "未知"
    if close > zone_high:
        return "位於量區上方"
    if close < zone_low:
        return "位於量區下方"
    return "位於量區內"


def _format_price_level(v) -> str:
    try:
        value = float(v)
    except Exception:
        return "-"
    if not np.isfinite(value):
        return "-"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _classify_price_volume_pattern(
    relative_to_two_zones: str,
    recent_event: str,
    swing_structure: str,
    price_volume_relation: str,
) -> tuple[str, str]:
    """將大量區位置、穿越狀態與高低點結構整理成明確且中立的型態標籤。"""
    relative = str(relative_to_two_zones or "")
    event = str(recent_event or "")
    swing = str(swing_structure or "")
    pv = str(price_volume_relation or "")

    if "突破最大量區後曾回踩" in event and "仍守在量區上方" in event:
        label = "突破後回踩整理"
    elif "突破最大量區後維持在量區上方" in event:
        label = "突破後高檔整理"
    elif "突破最大量區後回到量區內" in event or "突破尚未站穩" in event:
        label = "突破未站穩的區間整理"
    elif "跌破最大量區後已重新站回" in event or "跌破最大量區後重新回到量區內" in event:
        label = "跌破後修復整理"
    elif "跌破最大量區後" in event and ("仍未站回" in event or "仍位於量區下方" in event):
        label = "跌破轉弱"
    elif "第一大量區與第二大量區之間" in relative:
        if "低點墊高，但高點尚未有效突破" in swing:
            label = "低點墊高的區間盤整"
        elif "高點下移，但低點尚未明顯跌破" in swing:
            label = "高點下移的區間盤整"
        else:
            label = "區間盤整"
    elif "第一大量區與第二大量區之上" in relative:
        if "高點與低點同步墊高" in swing:
            label = "多方趨勢整理"
        elif "低點墊高" in swing:
            label = "高檔墊高整理"
        else:
            label = "高檔震盪"
    elif "第一大量區與第二大量區之下" in relative:
        if "高點與低點同步下移" in swing:
            label = "空方趨勢整理"
        elif "高點下移" in swing:
            label = "弱勢整理"
        else:
            label = "低檔區間整理"
    elif "高點與低點同步墊高" in swing:
        label = "多方趨勢整理"
    elif "高點與低點同步下移" in swing:
        label = "空方趨勢整理"
    elif "低點墊高" in swing:
        label = "低點墊高整理"
    elif "高點下移" in swing:
        label = "高點下移整理"
    else:
        label = "區間整理"

    evidence_parts = [p for p in [relative, swing, event, pv] if p and p != "資料不足"]
    evidence = "；".join(evidence_parts[:4])
    return label, evidence


def _build_price_volume_pattern_payload(ctx: dict, n_bins: int = 40) -> dict:
    """整理紅色最大量區、橘色第二大量區與近期型態，供 AI 做客觀型態分析。"""
    df = ctx.get("plot_df", pd.DataFrame())
    stats = _calculate_weighted_volume_profile_stats(df, n_bins=n_bins)
    if not stats:
        return {"available": False}

    work = stats["work"]
    bins = stats["bins"]
    centers = stats["centers"]
    profile = stats["profile"]
    max_idx = int(stats["max_idx"])
    second_idx = int(stats["second_idx"])

    latest_close = float(work["Close"].iloc[-1])

    def zone_record(idx: int, label: str, chart_color: str) -> dict:
        if idx < 0 or idx >= len(centers):
            return {}
        zone_low = float(bins[idx])
        zone_high = float(bins[idx + 1])
        center = float(centers[idx])
        distance_pct = (latest_close / center - 1) * 100 if center else np.nan
        return {
            "label": label,
            "chart_color": chart_color,
            "zone_low": _format_price_level(zone_low),
            "zone_high": _format_price_level(zone_high),
            "center_price": _format_price_level(center),
            "latest_close_relation": _price_zone_relation(latest_close, zone_low, zone_high),
            "latest_close_distance_from_center_pct": round(float(distance_pct), 2) if np.isfinite(distance_pct) else None,
            "relative_profile_strength_pct": round(float(profile[idx] / profile[max_idx] * 100), 2) if profile[max_idx] > 0 else None,
        }

    max_zone = zone_record(max_idx, "最大量區", "紅色")
    second_zone = zone_record(second_idx, "第二大量區", "橘色") if second_idx >= 0 else {}

    max_low = float(bins[max_idx])
    max_high = float(bins[max_idx + 1])
    closes = work["Close"].astype(float).reset_index(drop=True)
    lows = work["Low"].astype(float).reset_index(drop=True)
    highs = work["High"].astype(float).reset_index(drop=True)
    volumes = work["Volume"].astype(float).reset_index(drop=True)

    recent_start = max(1, len(work) - 15)
    events = []
    for i in range(recent_start, len(work)):
        prev_close = float(closes.iloc[i - 1])
        curr_close = float(closes.iloc[i])
        if prev_close <= max_high and curr_close > max_high:
            events.append((i, "向上突破最大量區"))
        if prev_close >= max_low and curr_close < max_low:
            events.append((i, "向下跌破最大量區"))

    recent_event = "近15個交易日未出現明確穿越最大量區"
    event_volume_ratio = None
    if events:
        event_idx, event_type = events[-1]
        subsequent_lows = lows.iloc[event_idx:]
        subsequent_highs = highs.iloc[event_idx:]
        prior_vol = volumes.iloc[max(0, event_idx - 20):event_idx]
        prior_vol_mean = float(prior_vol.mean()) if len(prior_vol) else np.nan
        if prior_vol_mean > 0:
            event_volume_ratio = round(float(volumes.iloc[event_idx] / prior_vol_mean), 2)

        if event_type == "向上突破最大量區":
            retested = bool(len(subsequent_lows) and float(subsequent_lows.min()) <= max_high * 1.01)
            if latest_close > max_high and retested:
                recent_event = "突破最大量區後曾回踩，目前仍守在量區上方"
            elif latest_close > max_high:
                recent_event = "突破最大量區後維持在量區上方"
            elif latest_close >= max_low:
                recent_event = "突破最大量區後回到量區內整理"
            else:
                recent_event = "突破最大量區後跌回量區下方，突破尚未站穩"
        else:
            rebounded = bool(len(subsequent_highs) and float(subsequent_highs.max()) >= max_low * 0.99)
            if latest_close < max_low and rebounded:
                recent_event = "跌破最大量區後曾反彈測試，目前仍未站回量區"
            elif latest_close < max_low:
                recent_event = "跌破最大量區後仍位於量區下方"
            elif latest_close <= max_high:
                recent_event = "跌破最大量區後重新回到量區內"
            else:
                recent_event = "跌破最大量區後已重新站回量區上方"

    # 以最近 20 個交易日的前後半段高低點比較，提供中立的波段結構。
    structure_window = work.tail(min(20, len(work))).copy()
    swing_structure = "資料不足"
    if len(structure_window) >= 8:
        split = max(4, len(structure_window) // 2)
        first = structure_window.iloc[:split]
        second = structure_window.iloc[split:]
        first_high = float(first["High"].max())
        first_low = float(first["Low"].min())
        second_high = float(second["High"].max())
        second_low = float(second["Low"].min())
        tol = 0.005
        high_up = second_high > first_high * (1 + tol)
        high_down = second_high < first_high * (1 - tol)
        low_up = second_low > first_low * (1 + tol)
        low_down = second_low < first_low * (1 - tol)
        if high_up and low_up:
            swing_structure = "近期高點與低點同步墊高"
        elif high_down and low_down:
            swing_structure = "近期高點與低點同步下移"
        elif not high_up and low_up:
            swing_structure = "近期低點墊高，但高點尚未有效突破"
        elif high_down and not low_down:
            swing_structure = "近期高點下移，但低點尚未明顯跌破"
        else:
            swing_structure = "近期高低點呈區間整理或方向不一致"

    weekly_return = _safe_float(ctx.get("stock_ret"))
    volume_change = _safe_float(ctx.get("vol_change"))
    if np.isfinite(weekly_return) and np.isfinite(volume_change):
        if weekly_return > 0 and volume_change > 0:
            price_volume_relation = "本週價漲量增"
        elif weekly_return > 0 and volume_change <= 0:
            price_volume_relation = "本週價漲量縮"
        elif weekly_return < 0 and volume_change > 0:
            price_volume_relation = "本週價跌量增"
        elif weekly_return < 0 and volume_change <= 0:
            price_volume_relation = "本週價跌量縮"
        else:
            price_volume_relation = "本週價格變化有限"
    else:
        price_volume_relation = "資料不足"

    zone_centers = [float(centers[max_idx])]
    if second_idx >= 0:
        zone_centers.append(float(centers[second_idx]))
    lower_center, upper_center = min(zone_centers), max(zone_centers)
    if latest_close > upper_center:
        relative_to_two_zones = "最新收盤位於第一大量區與第二大量區之上"
        neutral_zone_interpretation = "兩個主要成交成本區位於股價下方，可作為下方支撐觀察，但仍需配合近期價量與資金方向判斷是否有效"
    elif latest_close < lower_center:
        relative_to_two_zones = "最新收盤位於第一大量區與第二大量區之下"
        neutral_zone_interpretation = "兩個主要成交成本區位於股價上方，型態上較接近上方壓力，尚未重新站回前不宜直接視為轉強"
    else:
        relative_to_two_zones = "最新收盤位於第一大量區與第二大量區之間"
        neutral_zone_interpretation = "股價位於兩個主要成交成本區之間，型態較接近成本區整理，上下方向仍需等待價量與資金進一步確認"

    if event_volume_ratio is None:
        crossing_volume_character = "近期沒有明確穿越第一大量區的事件，或穿越日量能資料不足"
    elif event_volume_ratio >= 1.5:
        crossing_volume_character = "穿越第一大量區時量能明顯高於近20日平均"
    elif event_volume_ratio >= 1.1:
        crossing_volume_character = "穿越第一大量區時量能略高於近20日平均"
    elif event_volume_ratio <= 0.8:
        crossing_volume_character = "穿越第一大量區時量能低於近20日平均"
    else:
        crossing_volume_character = "穿越第一大量區時量能接近近20日平均"

    pattern_label, pattern_evidence = _classify_price_volume_pattern(
        relative_to_two_zones,
        recent_event,
        swing_structure,
        price_volume_relation,
    )

    # 只提供型態分類與相對位置給 AI，不提供大量區實際價格、中心價或距離百分比，
    # 避免本週重點變成數字播報。實際量區價格仍保留在繪圖算法內，不影響圖表。
    maximum_volume_zone_for_ai = {
        "label": "第一大量區（最大量區）",
        "chart_color": "紅色",
        "latest_close_relation": str(max_zone.get("latest_close_relation", "") or ""),
    }
    second_volume_zone_for_ai = {
        "label": "第二大量區",
        "chart_color": "橘色",
        "latest_close_relation": str(second_zone.get("latest_close_relation", "") or ""),
    } if second_zone else {}

    return {
        "available": True,
        "chart_definition": "價量累積圖中紅色代表第一大量區（最大量區），橘色代表第二大量區",
        "maximum_volume_zone": maximum_volume_zone_for_ai,
        "second_largest_volume_zone": second_volume_zone_for_ai,
        "current_pattern_label": pattern_label,
        "pattern_evidence": pattern_evidence,
        "latest_position_relative_to_two_zones": relative_to_two_zones,
        "recent_maximum_zone_pattern": recent_event,
        "crossing_volume_character": crossing_volume_character,
        "recent_swing_structure": swing_structure,
        "weekly_price_volume_relationship": price_volume_relation,
        "neutral_zone_interpretation": neutral_zone_interpretation,
        "analysis_scope": "必須直接使用 current_pattern_label 說明目前型態，再用相對位置、突破或跌破、回踩、價量配合與高低點結構解釋原因；不得輸出第一大量區或第二大量區的實際價格、中心價、距離百分比，也不可直接視為買賣訊號",
    }




def _build_technical_card_summary(ctx: dict) -> dict:
    """依圖上已計算的均線 / 價量型態，產生下方技術面卡片文字。

    目的：避免下方技術面只寫「重點待確認」，或與上方 K 線圖顯示的
    「均線多頭排列 / 均線空頭排列 / 全面跌破均線」等訊號矛盾。
    此函式只整理圖卡文字，不改變任何股價、權證或法人計算邏輯。
    """
    df = ctx.get("plot_df", pd.DataFrame())
    if df is None or df.empty:
        return {}

    latest = df.iloc[-1]
    close = _safe_float(latest.get("Close"))
    ma5 = _safe_float(latest.get("MA5"))
    ma10 = _safe_float(latest.get("MA10"))
    ma20 = _safe_float(latest.get("MA20"))
    ma60 = _safe_float(latest.get("MA60"))
    if not np.isfinite(close):
        return {}

    def _finite(v):
        return np.isfinite(_safe_float(v))

    ma_values = {
        "5MA": ma5,
        "10MA": ma10,
        "20MA": ma20,
        "60MA": ma60,
    }
    above = [name for name, value in ma_values.items() if _finite(value) and close >= float(value)]
    below = [name for name, value in ma_values.items() if _finite(value) and close < float(value)]

    ma_signal = get_ma_kline_signals(df) if df is not None and not df.empty else ""
    ma_signal_main = str(ma_signal or "").split("．")[0].strip()
    pattern = _build_price_volume_pattern_payload(ctx)
    pattern_label = str(pattern.get("current_pattern_label", "") or "").strip()
    price_volume_relation = str(pattern.get("weekly_price_volume_relationship", "") or "").strip()
    zone_position = str(pattern.get("latest_position_relative_to_two_zones", "") or "").strip()

    def _volume_zone_hint() -> str:
        """把第一大量區 / 第二大量區相對位置納入技術面說明。"""
        pos = zone_position
        if not pos:
            return ""
        if "第一大量區與第二大量區之間" in pos:
            return "仍在第一與第二大量區附近整理"
        if "第一大量區與第二大量區之上" in pos:
            return "仍站在第一與第二大量區上方"
        if "第一大量區與第二大量區之下" in pos:
            return "第一與第二大量區轉為上方壓力"
        return ""

    ma_all_available = all(_finite(v) for v in [ma5, ma10, ma20, ma60])
    ma_bull = ma_all_available and ma5 > ma10 > ma20 > ma60
    ma_bear = ma_all_available and ma5 < ma10 < ma20 < ma60

    below_set = set(below)
    above_set = set(above)

    if ma_bull:
        if close >= ma5 and close >= ma10:
            headline = "均線多頭排列延續"
            detail = "上方訊號為均線多頭排列，收盤仍站在短中長期均線之上。"
        elif {"5MA", "10MA"}.issubset(below_set) and {"20MA", "60MA"}.issubset(above_set):
            headline = "多頭排列下短線拉回"
            detail = "上方仍是均線多頭排列，但收盤跌破5MA與10MA。"
        elif "20MA" in below_set and "60MA" in above_set:
            headline = "多頭結構轉為修正"
            detail = "均線結構仍偏多，但收盤跌破20MA，短線修正壓力升高。"
        elif "60MA" in below_set:
            headline = "跌破中長均線轉弱"
            detail = "原本多頭結構遭破壞，收盤已跌破60MA，趨勢需要重新修復。"
        else:
            headline = "多頭排列震盪整理"
            detail = "上方訊號為均線多頭排列，但短線位置轉為整理。"
    elif ma_bear:
        if all(name in below_set for name in ["5MA", "10MA", "20MA", "60MA"]):
            headline = "均線空頭排列延續"
            detail = "上方訊號偏弱，收盤仍落在主要均線下方。"
        elif close >= ma5 or close >= ma10:
            headline = "空頭排列下反彈"
            detail = "均線結構仍偏弱，但短線嘗試站回短均，仍需確認延續性。"
        else:
            headline = "均線空頭排列整理"
            detail = "均線結構偏空，短線仍以整理與修復觀察為主。"
    else:
        if ma_signal_main:
            if "全面跌破均線" in ma_signal_main:
                headline = "全面跌破均線轉弱"
                detail = "上方訊號顯示全面跌破均線，短線仍以修復主要均線為重點。"
            elif "強勢站上均線" in ma_signal_main:
                headline = "站上均線偏強"
                detail = "上方訊號顯示強勢站上均線，短線仍需搭配量能確認。"
            elif "均線多頭排列" in ma_signal_main:
                headline = "均線多頭排列延續"
                detail = "上方訊號為均線多頭排列，趨勢結構仍偏正向。"
            elif "均線空頭排列" in ma_signal_main:
                headline = "均線空頭排列延續"
                detail = "上方訊號為均線空頭排列，短線仍以修復均線為主。"
            elif "黃金交叉" in ma_signal_main:
                headline = "均線黃金交叉"
                detail = "上方訊號出現均線黃金交叉，後續需確認量能是否配合。"
            elif "死亡交叉" in ma_signal_main:
                headline = "均線死亡交叉"
                detail = "上方訊號出現均線死亡交叉，短線轉弱風險升高。"
            else:
                headline = ma_signal_main[:16]
                detail = f"上方技術訊號為{ma_signal_main}，短線仍需搭配價量確認。"
        elif pattern_label:
            headline = f"價量結構{pattern_label}"[:16]
            detail = f"價量型態顯示{pattern_label}，短線仍需觀察量能與收盤位置。"
        else:
            headline = "技術訊號待確認"
            detail = "目前均線與價量訊號未形成明確方向，短線以確認收盤位置為主。"

    # 第一大量區 / 第二大量區是上方圖中已畫出的成本區，技術面說明需同步參考，
    # 避免只看均線而忽略股價其實仍在主要量區附近。
    zone_hint = _volume_zone_hint()
    if zone_hint and zone_hint not in detail:
        if any(k in headline for k in ["跌破", "修正", "轉弱", "拉回", "整理"]):
            detail = f"{zone_hint}，{detail.lstrip('。')}"
        elif len(detail.rstrip("。") + "，" + zone_hint + "。") <= 68:
            detail = detail.rstrip("。") + f"，{zone_hint}。"

    # 若價量型態提供的是明確「本週價跌量縮 / 價跌量增」等關係，補到說明後段；
    # 但避免句子過長，僅在不會造成擁擠時加入。
    if price_volume_relation and price_volume_relation != "資料不足" and price_volume_relation not in detail:
        extra = f"並呈現{price_volume_relation}。"
        if len(detail.rstrip("。") + "，" + extra) <= 68:
            detail = detail.rstrip("。") + f"，並呈現{price_volume_relation}。"

    return {
        "label": "技術面",
        "headline": _compact_text_for_card_headline(headline, max_chars=16),
        "detail": _compact_text_for_card_detail(detail, max_chars=68),
        "ma_signal": ma_signal_main,
        "pattern_label": pattern_label,
        "zone_position": zone_position,
    }


def _compact_text_for_card_headline(text: str, max_chars: int = 16) -> str:
    s = str(text or "").strip("。；;，,、 ")
    return s[:max_chars].rstrip("。；;，,、 ")


def _compact_text_for_card_detail(text: str, max_chars: int = 58) -> str:
    s = str(text or "").strip()
    s = re.sub(r"(?:\.\.\.|…+)", "", s).strip("；;，,、 ")
    if len(s) <= max_chars:
        return s if s.endswith("。") else s + "。"
    prefix = s[:max_chars]
    ends = [m.end() for m in re.finditer(r"[。！？]", prefix)]
    if ends and ends[-1] >= max(24, int(max_chars * 0.5)):
        return prefix[:ends[-1]]
    cut = max([prefix.rfind(p) for p in ["；", ";", "，", ",", "、"]])
    if cut >= max(24, int(max_chars * 0.5)):
        prefix = prefix[:cut]
    prefix = prefix.rstrip("；;，,、 ")
    return prefix + ("。" if prefix and prefix[-1] not in "。！？" else "")

def _build_rule_based_pattern_point(ctx: dict) -> str:
    pattern = _build_price_volume_pattern_payload(ctx)
    if not pattern.get("available"):
        return ""
    pattern_label = str(pattern.get("current_pattern_label", "") or "區間整理")
    return (
        f"型態面目前屬於{pattern_label}：{pattern.get('latest_position_relative_to_two_zones', '')}，"
        f"{pattern.get('recent_swing_structure', '')}；{pattern.get('recent_maximum_zone_pattern', '')}，"
        f"並呈現{pattern.get('weekly_price_volume_relationship', '')}。"
    )


def _get_weekly_institutional_context(ctx: dict) -> dict:
    """計算三大法人本週合計及其中性門檻，避免極小買賣超被放大解讀。"""
    df = ctx.get("plot_df", pd.DataFrame())
    week_dates = [pd.Timestamp(d) for d in (ctx.get("stock_week_dates") or [])]
    valid_week_dates = [d for d in week_dates if df is not None and not df.empty and d in df.index]
    inst_week = (
        df.loc[valid_week_dates].copy()
        if valid_week_dates
        else (df.tail(WEEK_TRADING_DAYS).copy() if df is not None and not df.empty else pd.DataFrame())
    )
    inst_total = 0.0
    if inst_week is not None and not inst_week.empty and "total" in inst_week.columns:
        inst_total = float(pd.to_numeric(inst_week["total"], errors="coerce").fillna(0.0).sum())

    latest_mv20 = np.nan
    if df is not None and not df.empty:
        latest_mv20 = _safe_float(df.iloc[-1].get("MV20"))
    volume_based_threshold = (
        abs(float(latest_mv20)) * max(0.0, float(WEEKLY_KEYPOINT_INST_NEUTRAL_MV20_RATIO))
        if np.isfinite(latest_mv20)
        else 0.0
    )
    neutral_threshold = max(
        0.0,
        float(WEEKLY_KEYPOINT_INST_NEUTRAL_MIN_SHARES),
        volume_based_threshold,
    )

    if abs(inst_total) <= neutral_threshold:
        classification = "接近中性"
        interpretation = "三大法人本週買賣幅度有限，尚未形成明確法人方向"
    elif inst_total > 0:
        classification = "偏多"
        interpretation = "三大法人本週呈現具一定幅度的淨買超"
    else:
        classification = "偏空"
        interpretation = "三大法人本週呈現具一定幅度的淨賣超"

    return {
        "weekly_total": inst_total,
        "neutral_threshold": neutral_threshold,
        "classification": classification,
        "interpretation": interpretation,
    }


def _build_rule_based_crossflow_point(ctx: dict) -> str:
    inst_ctx = _get_weekly_institutional_context(ctx)
    inst_total = float(inst_ctx.get("weekly_total", 0) or 0)
    inst_class = str(inst_ctx.get("classification", "") or "")
    warrant_net = float(ctx.get("total_net", 0) or 0)
    stock_ret = _safe_float(ctx.get("stock_ret"))

    if inst_class == "接近中性":
        if warrant_net > 0:
            relation = "三大法人方向接近中性，權證資金則偏向淨買超，短線權證買盤尚未獲得法人資金明確呼應"
        elif warrant_net < 0:
            relation = "三大法人方向接近中性，權證資金則偏向淨賣超，短線權證調節尚未形成法人同步賣壓"
        else:
            relation = "三大法人與權證資金變化皆有限，整體籌碼方向尚未明確"
    elif inst_total > 0 and warrant_net > 0:
        relation = "三大法人與權證資金同向偏多"
    elif inst_total < 0 and warrant_net < 0:
        relation = "三大法人與權證資金同向偏空"
    else:
        relation = "三大法人與權證資金方向分歧，反映不同資金的操作時間尺度可能不一致"

    return (
        f"資金交叉：股價本週 {fmt_pct(stock_ret)}，三大法人本週合計 {inst_total:+,.0f} 張，"
        f"權證週淨流向 {fmt_money(warrant_net)}；{relation}。"
    )


def _weekly_keypoints_cache_task() -> str:
    safe_version = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        str(WEEKLY_KEYPOINT_STYLE_VERSION or "validated_v24_json_grounded_previous_week"),
    )
    return f"weekly_keypoints_{safe_version}"


def _build_weekly_llm_payload(ctx: dict, stock_name: str) -> dict:
    """將股價、均線、量能、法人、權證與 TOP5 分點完整整理給 AI。"""
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
    warrant_rows = []
    if week_events is not None and not week_events.empty:
        e = week_events.copy()
        e["branch"] = e["branch"].map(normalize_branch_name).replace("", "未知分點")
        wg = e.groupby(["warrant_code", "warrant_name"], as_index=False)["net_amount"].sum()
        for _, r in wg.reindex(wg["net_amount"].abs().sort_values(ascending=False).index).head(6).iterrows():
            warrant_rows.append({
                "warrant": f"{r.get('warrant_code', '')} {str(r.get('warrant_name', ''))[:10]}",
                "net": fmt_money(float(r.get("net_amount", 0))),
            })

    top5_rows = _build_weekly_top5_ai_rows(ctx)
    representative_buy_rows = [
        r for r in top5_rows
        if r.get("side") == "買超" and bool(r.get("amount_representative"))
    ]
    representative_sell_rows = [
        r for r in top5_rows
        if r.get("side") == "賣超" and bool(r.get("amount_representative"))
    ]
    representative_history_candidates = [
        r for r in (representative_buy_rows + representative_sell_rows)
        if bool(r.get("historical_statistics_available"))
    ]
    representative_history_candidates = sorted(
        representative_history_candidates,
        key=lambda r: -abs(float(r.get("weekly_net_value", 0) or 0)),
    )
    required_representative_branch_analysis = (
        dict(representative_history_candidates[0]) if representative_history_candidates else None
    )
    small_rows = [r for r in top5_rows if not bool(r.get("amount_representative"))]
    small_amount_summary = {
        "count": len(small_rows),
        "combined_absolute_amount": fmt_money_abs(sum(abs(float(r.get("weekly_net_value", 0) or 0)) for r in small_rows)),
        "rule": (
            f"低於全體最大分點絕對金額的 "
            f"{max(0.0, float(WEEKLY_KEYPOINT_BRANCH_MIN_OVERALL_RATIO)) * 100:.0f}% 時，"
            "不提供分點名稱與歷史績效供個別分析"
        ),
    }

    price_volume_pattern = _build_price_volume_pattern_payload(ctx)

    # 三大法人除最新日外，再提供本週合計，讓 AI 能比較法人與權證方向是否一致。
    inst_week = pd.DataFrame()
    if df is not None and not df.empty:
        week_dates = [pd.Timestamp(d) for d in (ctx.get("stock_week_dates") or [])]
        valid_week_dates = [d for d in week_dates if d in df.index]
        if valid_week_dates:
            inst_week = df.loc[valid_week_dates].copy()
        else:
            inst_week = df.tail(WEEK_TRADING_DAYS).copy()

    def inst_sum(col: str) -> float:
        if inst_week is None or inst_week.empty or col not in inst_week.columns:
            return 0.0
        return float(pd.to_numeric(inst_week[col], errors="coerce").fillna(0.0).sum())

    institutional_context = _get_weekly_institutional_context(ctx)

    # 上週對照：使用既有日期軸、權證事件與法人欄位，不新增任何外部資料來源。
    report_dates = sorted({
        pd.Timestamp(d).normalize()
        for d in (ctx.get("report_dates") or [])
        if pd.notna(d)
    })
    current_report_dates = report_dates[-WEEK_TRADING_DAYS:] if report_dates else []
    previous_report_dates = (
        report_dates[-WEEK_TRADING_DAYS * 2:-WEEK_TRADING_DAYS]
        if len(report_dates) > WEEK_TRADING_DAYS
        else []
    )
    previous_week_start = previous_report_dates[0] if previous_report_dates else pd.NaT
    previous_week_end = previous_report_dates[-1] if previous_report_dates else pd.NaT

    plot_events = ctx.get("plot_events")
    if (
        plot_events is not None
        and not plot_events.empty
        and previous_report_dates
        and "Date" in plot_events.columns
    ):
        previous_week_events = plot_events.copy()
        previous_week_events["Date"] = pd.to_datetime(
            previous_week_events["Date"],
            errors="coerce",
        ).dt.normalize()
        previous_week_events = previous_week_events[
            previous_week_events["Date"].isin(previous_report_dates)
        ].copy()
    else:
        previous_week_events = pd.DataFrame(
            columns=[
                "Date", "branch", "warrant_code", "warrant_name",
                "buy_amount", "sell_amount", "net_amount",
            ]
        )

    previous_warrant_net = (
        float(pd.to_numeric(
            previous_week_events.get("net_amount", pd.Series(dtype=float)),
            errors="coerce",
        ).fillna(0.0).sum())
        if previous_week_events is not None and not previous_week_events.empty
        else 0.0
    )
    previous_buy_top, previous_sell_top = _get_cached_top_branch_tables(
        ctx,
        "previous_week",
        previous_week_events,
        topn=5,
    )

    def previous_top5_rows(frame: pd.DataFrame, side: str) -> List[dict]:
        rows = []
        if frame is None or frame.empty:
            return rows
        for _, row in frame.iterrows():
            rows.append({
                "branch": str(row.get("branch", "") or ""),
                "side": side,
                "net": fmt_money(float(row.get("net_amount", 0) or 0)),
            })
        return rows

    stock_week_dates = [
        pd.Timestamp(d).normalize()
        for d in (ctx.get("stock_week_dates") or [])
        if pd.notna(d)
    ]
    previous_inst_week = pd.DataFrame()
    if df is not None and not df.empty:
        if stock_week_dates:
            first_current_date = stock_week_dates[0]
            earlier_positions = [
                idx
                for idx, date_value in enumerate(df.index)
                if pd.Timestamp(date_value).normalize() < first_current_date
            ]
            current_start_pos = earlier_positions[-1] + 1 if earlier_positions else 0
            previous_inst_week = df.iloc[
                max(0, current_start_pos - WEEK_TRADING_DAYS):current_start_pos
            ].copy()
        elif len(df) > WEEK_TRADING_DAYS:
            previous_inst_week = df.iloc[
                max(0, len(df) - WEEK_TRADING_DAYS * 2):-WEEK_TRADING_DAYS
            ].copy()

    def previous_inst_sum(col: str) -> float:
        if (
            previous_inst_week is None
            or previous_inst_week.empty
            or col not in previous_inst_week.columns
        ):
            return 0.0
        return float(
            pd.to_numeric(
                previous_inst_week[col],
                errors="coerce",
            ).fillna(0.0).sum()
        )

    previous_week_comparison = {
        "period": (
            f"{previous_week_start.strftime('%Y/%m/%d')} - "
            f"{previous_week_end.strftime('%Y/%m/%d')}"
            if pd.notna(previous_week_start) and pd.notna(previous_week_end)
            else ""
        ),
        "warrant_weekly_net": fmt_money(previous_warrant_net),
        "institutional_weekly_total": f"{previous_inst_sum('total'):+,.0f}張",
        "buy_top5_branches": previous_top5_rows(previous_buy_top, "買超"),
        "sell_top5_branches": previous_top5_rows(previous_sell_top, "賣超"),
    }

    payload = {
        "stock": f"{stock_code} {stock_name}",
        "period": f"{ctx['week_start'].strftime('%Y/%m/%d')} - {ctx['week_end'].strftime('%Y/%m/%d')}" if pd.notna(ctx.get("week_start")) else "",
        "price_ma_volume": {
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
            "weekly_volume_change_vs_previous_week": fmt_pct(ctx.get("vol_change", np.nan)),
            "latest_volume_vs_20day_average": f"{vol_ratio:.2f} 倍" if np.isfinite(vol_ratio) else "-",
        },
        "price_volume_pattern": price_volume_pattern,
        "institutional": {
            "latest_day": {
                "foreign": f"{_safe_float(latest.get('foreign'), 0):+,.0f}張",
                "investment_trust": f"{_safe_float(latest.get('invest'), 0):+,.0f}張",
                "dealer_self_trading": f"{_safe_float(latest.get('dealer'), 0):+,.0f}張",
                "total": f"{_safe_float(latest.get('total'), 0):+,.0f}張",
            },
            "weekly_total": {
                "foreign": f"{inst_sum('foreign'):+,.0f}張",
                "investment_trust": f"{inst_sum('invest'):+,.0f}張",
                "dealer_self_trading": f"{inst_sum('dealer'):+,.0f}張",
                "total": f"{inst_sum('total'):+,.0f}張",
                "classification": str(institutional_context.get("classification", "") or ""),
                "neutral_threshold": f"{float(institutional_context.get('neutral_threshold', 0) or 0):,.0f}張",
                "interpretation": str(institutional_context.get("interpretation", "") or ""),
            },
        },
        "warrant_weekly_flow": {
            "weekly_buy": fmt_money_abs(ctx.get("total_buy", 0)),
            "weekly_sell": fmt_money(-abs(float(ctx.get("total_sell", 0) or 0))),
            "weekly_net": fmt_money(ctx.get("total_net", 0)),
            "bias": ctx.get("bias", ""),
            "major_warrants": warrant_rows,
        },
        "representative_buy_top5_with_history": representative_buy_rows,
        "representative_sell_top5_with_history": representative_sell_rows,
        "required_representative_branch_analysis": required_representative_branch_analysis,
        "non_representative_top5_summary": small_amount_summary,
        "previous_week_comparison": previous_week_comparison,
        "recent_news_summary": [
            str(x or "").strip()
            for x in (ctx.get("weekly_news_points") or [])
            if str(x or "").strip()
        ][:NEWS_DISPLAY_MAX_POINTS],
    }
    return payload

def _weekly_points_mention_nonrepresentative_branch(points: List[str], ctx: dict) -> List[str]:
    """回傳 AI 不應點名、但實際出現在重點中的小額分點名稱。"""
    merged = "\n".join(str(p or "") for p in points)
    if not merged:
        return []
    rows = _build_weekly_top5_ai_rows(ctx)
    forbidden = []
    for row in rows:
        if bool(row.get("amount_representative")):
            continue
        branch = str(row.get("branch", "") or "").strip()
        if branch and branch in merged and branch not in forbidden:
            forbidden.append(branch)
    return forbidden


def _get_required_representative_branch_analysis(ctx: dict) -> dict | None:
    """取得本週金額最具代表性，且有完整歷史績效資料的分點。"""
    rows = _build_weekly_top5_ai_rows(ctx)
    candidates = [
        r for r in rows
        if bool(r.get("amount_representative"))
        and bool(r.get("historical_statistics_available"))
    ]
    if not candidates:
        return None
    candidates = sorted(
        candidates,
        key=lambda r: -abs(float(r.get("weekly_net_value", 0) or 0)),
    )
    return dict(candidates[0])


def _normalize_metric_text_for_check(v) -> str:
    s = str(v or "").strip()
    s = s.replace("＋", "+").replace("％", "%")
    s = re.sub(r"[\s,，]", "", s)
    return s


def _weekly_points_cover_required_representative_analysis(points: List[str], ctx: dict) -> bool:
    """確認 AI 有完整分析代表性分點，而不是只寫金額或集中度。"""
    required = _get_required_representative_branch_analysis(ctx)
    if not required:
        return True

    merged = _normalize_metric_text_for_check("\n".join(str(p or "") for p in points))
    branch = _normalize_metric_text_for_check(required.get("branch", ""))
    win_rate = _normalize_metric_text_for_check(required.get("historical_win_rate", ""))
    weighted_return = _normalize_metric_text_for_check(required.get("historical_weighted_return", ""))
    avg_holding_days = _normalize_metric_text_for_check(required.get("average_holding_days", ""))

    if not branch or branch not in merged:
        return False
    if "勝率" not in merged or "加權報酬率" not in merged:
        return False
    if win_rate and win_rate != "-" and win_rate not in merged:
        return False
    if weighted_return and weighted_return != "-" and weighted_return not in merged:
        return False
    if avg_holding_days and avg_holding_days != "-":
        if avg_holding_days not in merged or "持有" not in merged:
            return False
    return True



def _weekly_points_cover_required_pattern_analysis(points: List[str], ctx: dict) -> bool:
    pattern = _build_price_volume_pattern_payload(ctx)
    if not pattern.get("available"):
        return True
    merged = "\n".join(str(p or "") for p in points)
    required_label = str(pattern.get("current_pattern_label", "") or "").strip()
    has_required_label = bool(required_label and required_label in merged)
    has_zone = any(k in merged for k in ["最大量區", "第一大量", "第二大量區", "價量累積", "大量區", "成本區"])
    has_pattern_meaning = any(k in merged for k in ["突破", "跌破", "回踩", "站回", "支撐", "壓力", "盤整", "整理", "震盪", "量區上方", "量區下方", "量區內", "兩個主要成交成本區之間", "兩個主要成交成本區之上", "兩個主要成交成本區之下"])
    has_price_volume = any(k in merged for k in ["價漲量增", "價漲量縮", "價跌量增", "價跌量縮", "放量", "量縮", "價量"])
    # 大量區只允許相對位置與型態判讀，不應出現「大量區約 4,xxx」或「量區價格 4,xxx」等明確價位。
    explicit_zone_price = bool(re.search(r"(?:第一大量區|第二大量區|最大量區|大量區|成本區)[^。；，]{0,12}(?:約|為|落在|位於)?\s*[0-9][0-9,]*(?:\.[0-9]+)?", merged))
    return bool(has_required_label and has_zone and has_pattern_meaning and has_price_volume and not explicit_zone_price)


def _weekly_points_overstate_neutral_institutional(points: List[str], ctx: dict) -> bool:
    """法人接近中性時，不允許 AI 放大成明顯分歧、偏空或法人賣壓。"""
    inst_ctx = _get_weekly_institutional_context(ctx)
    if str(inst_ctx.get("classification", "") or "") != "接近中性":
        return False
    merged = "\n".join(str(p or "") for p in points)
    overstated_phrases = [
        "三大法人與權證資金方向分歧", "法人與權證資金方向分歧",
        "法人賣壓", "法人明顯偏空", "法人同步賣超", "法人資金偏空",
        "法人籌碼轉弱", "法人全面賣超", "法人明顯賣超",
    ]
    return any(phrase in merged for phrase in overstated_phrases)


def _weekly_points_low_value_technical_reasons(points: List[str]) -> List[str]:
    """抓出只列均線或用空泛技術語句湊數的重點。"""
    reasons = []
    bad_phrases = [
        "搭配KD", "搭配 KD", "搭配MACD", "搭配 MACD",
        "需觀察動能是否延續", "觀察動能是否延續", "判斷短中期趨勢",
        "需搭配量能", "等待KD", "等待 KD", "MACD觀察", "MACD 觀察",
    ]
    for idx, point in enumerate(points, 1):
        s = str(point or "")
        for phrase in bad_phrases:
            if phrase in s:
                reasons.append(f"第{idx}點含空泛技術語句「{phrase}」")
                break
        ma_mentions = len(re.findall(r"(?:5|10|20|60)MA", s, flags=re.I))
        ma_price_mentions = len(re.findall(r"(?:5|10|20|60)MA\s*[-+]?\d", s, flags=re.I))
        if ma_mentions >= 2 or ma_price_mentions >= 1:
            reasons.append(f"第{idx}點單純羅列多條均線或均線價格")
    return list(dict.fromkeys(reasons))

def _weekly_points_watch_count(points: List[str]) -> int:
    prefixes = ("下週觀察：", "下週觀察:", "下週留意：", "下週留意:", "下週焦點：", "下週焦點:")
    return sum(1 for p in points if str(p or "").strip().startswith(prefixes))


def _weekly_points_have_current_week_analysis(points: List[str]) -> bool:
    prefixes = ("下週觀察：", "下週觀察:", "下週留意：", "下週留意:", "下週焦點：", "下週焦點:")
    return any(not str(p or "").strip().startswith(prefixes) for p in points)


def _weekly_points_conditionally_cover_branch(points: List[str], ctx: dict) -> bool:
    """未選擇分點時不強制；若點名代表性分點，需完整使用其歷史資料。"""
    required = _get_required_representative_branch_analysis(ctx)
    if not required:
        return True
    branch = str(required.get("branch", "") or "").strip()
    merged = "\n".join(str(p or "") for p in points)
    if not branch or branch not in merged:
        return True
    return _weekly_points_cover_required_representative_analysis(points, ctx)


def _supplement_weekly_branch_metrics_in_points(points: List[str], ctx: dict) -> List[str]:
    """AI 已點名代表性分點但漏寫歷史欄位時，由程式以接地資料補齊，避免額外 repair 呼叫。"""
    cleaned = _clean_weekly_key_points(points or [])[:3]
    required = _get_required_representative_branch_analysis(ctx)
    if not cleaned or not required:
        return cleaned
    if _weekly_points_cover_required_representative_analysis(cleaned, ctx):
        return cleaned

    branch = str(required.get("branch", "") or "").strip()
    if not branch:
        return cleaned

    target_idx = None
    for idx, point in enumerate(cleaned):
        point_text = str(point or "")
        if branch in point_text and not point_text.startswith(("下週觀察：", "下週觀察:")):
            target_idx = idx
            break
    if target_idx is None:
        return cleaned

    side = str(required.get("side", "") or "").strip()
    weekly_net = str(required.get("weekly_net", "") or "").strip()
    win_rate = str(required.get("historical_win_rate", "") or "").strip()
    weighted_return = str(required.get("historical_weighted_return", "") or "").strip()
    avg_holding_days = str(required.get("average_holding_days", "") or "").strip()

    if not all([side, weekly_net, win_rate, weighted_return, avg_holding_days]):
        return cleaned
    if "-" in [win_rate, weighted_return, avg_holding_days]:
        return cleaned

    replacement = (
        f"面向：權證面｜結果：{branch}{side}具代表性｜說明："
        f"本週淨流向{weekly_net}，歷史勝率{win_rate}、"
        f"加權報酬率{weighted_return}，平均持有{avg_holding_days}。"
    )
    replacement = _trim_weekly_point(
        replacement,
        max_len=WEEKLY_KEYPOINT_POINT_MAX_LEN,
    )
    if not replacement:
        return cleaned

    updated = list(cleaned)
    updated[target_idx] = replacement
    updated = _clean_weekly_key_points(updated)[:3]
    if _weekly_points_cover_required_representative_analysis(updated, ctx):
        print(f"✅ 程式端已補齊代表性分點歷史績效：{branch}")
        return updated
    return cleaned


def _weekly_points_conditionally_cover_pattern(points: List[str], ctx: dict) -> bool:
    """未選擇型態面時不強制；若談型態或大量區，必須使用程式判定的型態標籤。"""
    pattern = _build_price_volume_pattern_payload(ctx)
    if not pattern.get("available"):
        return True
    merged = "\n".join(str(p or "") for p in points)
    # 單純在「下週觀察」中寫能否突破，不代表 AI 已選擇完整型態分析；
    # 只有明確談到型態、量區或程式型態標籤時，才要求使用完整價量型態依據。
    pattern_terms = ["型態", "大量區", "成本區", "價量累積"]
    required_label = str(pattern.get("current_pattern_label", "") or "").strip()
    if not any(k in merged for k in pattern_terms) and not (required_label and required_label in merged):
        return True
    return _weekly_points_cover_required_pattern_analysis(points, ctx)



def _validate_weekly_points(
    points: List[str],
    payload: dict,
    ctx: dict,
) -> List[str]:
    """集中驗證本週重點，包含既有內容規則與新增的數字接地檢查。"""
    problems = []
    if len(points) != 3:
        problems.append(f"重點數量為 {len(points)}，應為 3 點")
    if any(_count_summary_chars([p]) < 24 for p in points) or _count_summary_chars(points) < 100:
        problems.append("內容過短")
    if points and not _points_are_independent_and_complete(points):
        problems.append("出現承接句、半句或省略號")

    watch_count = _weekly_points_watch_count(points)
    if watch_count < 1 or watch_count > 2:
        problems.append("下週觀察應為 1 至 2 點，且需以『下週觀察：』開頭")
    if points and not _weekly_points_have_current_week_analysis(points):
        problems.append("缺少本週已發生的重點分析")

    forbidden_branches = _weekly_points_mention_nonrepresentative_branch(points, ctx)
    if forbidden_branches:
        problems.append("點名金額不具代表性的分點：" + "、".join(forbidden_branches))

    problems.extend(_weekly_points_low_value_technical_reasons(points))

    if _weekly_points_overstate_neutral_institutional(points, ctx):
        problems.append("法人接近中性卻被放大解讀")
    if not _weekly_points_conditionally_cover_branch(points, ctx):
        problems.append("既然選擇分析代表性分點，就必須完整使用勝率、加權報酬率與平均持有天數")
    if not _weekly_points_conditionally_cover_pattern(points, ctx):
        problems.append("既然選擇分析型態，就必須使用程式判定的型態標籤與價量依據")

    ungrounded = _find_ungrounded_number_tokens(points, payload)
    if ungrounded:
        problems.append("含輸入 JSON 找不到的數字：" + _format_ungrounded_number_problems(ungrounded))

    return list(dict.fromkeys(problems))


def _save_validated_weekly_points_cache(
    task: str,
    stock_code: str,
    stock_name: str,
    prompt: str,
    points: List[str],
    payload: dict,
    ctx: dict,
):
    """本週重點僅在完整驗證通過後寫入每日快取。"""
    valid_points = _clean_weekly_key_points(points or [])[:3]
    problems = _validate_weekly_points(valid_points, payload, ctx)
    if problems:
        print("⚠️ 本週重點未通過完整驗證，不寫入 Gemini 快取：" + "；".join(problems))
        return

    cache_payload = {
        "points": valid_points,
        "note": "validated_weekly_points_json_grounded",
    }
    save_gsheet_llm_cache(
        task,
        stock_code,
        stock_name,
        prompt,
        json.dumps(cache_payload, ensure_ascii=False),
    )


def _repair_weekly_expert_points(
    points: List[str],
    payload: dict,
    ctx: dict,
    stock_name: str,
    problems: List[str],
) -> List[str]:
    """保留 AI 選題，只修正格式、內容誤讀與無依據數字。"""
    stock_code = str(ctx.get("stock_code", "") or "")
    repair_payload = {
        "problems": list(problems or []),
        "original_points": [str(p or "").strip() for p in points if str(p or "").strip()],
        "full_weekly_data": payload,
    }
    response_schema = _build_gemini_points_response_schema(
        min_points=3,
        max_points=3,
        include_note=False,
    )
    repair_prompt = f"""
你是專業且中立的台股研究員。上一版重點未通過檢查，請只依修正資料重新輸出。

修正原則：
1. 剛好 3 點；前 2 點分析本週已發生事件，第 3 點以「下週觀察：」開頭。
2. 固定格式為「面向：技術面/權證面/法人面/新聞面｜結果：具體結論｜說明：資料依據。」；下週點使用面向：下週觀察。
3. 優先指出 previous_week_comparison 與本週之間的延續、反轉或方向分歧，不要只播報本週單一數字。
4. 點名分點時，只能使用代表性分點，並完整寫出本週方向與金額、歷史勝率、平均持有天數及歷史加權報酬率。
5. 技術面使用 price_ma_volume.ma_signal 與 price_volume_pattern.current_pattern_label；法人接近中性時不得誇大。
6. 每個數字都必須在 full_weekly_data 中找到；不得換算、推估、補數字或提供買賣建議。每點獨立完整並以句號結束。

好範例：
- 面向：權證面｜結果：分點買盤延續｜說明：代表性分點本週維持買超，並結合完整歷史績效說明時間尺度。
- 下週觀察：面向：下週觀察｜結果：確認資金是否續強｜說明：若權證淨流向與法人方向同步改善，再觀察價量是否確認。

壞範例：
- 面向：新聞面｜結果：重點待確認｜說明：後續持續觀察。
- 面向：技術面｜結果：偏多｜說明：KD與MACD值得留意。

只回傳符合 JSON Schema 的 JSON。

修正資料：
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
"""
    output_text = _call_gemini_with_retry(
        repair_prompt,
        cache_task=f"{_weekly_keypoints_cache_task()}_repair",
        stock_code=stock_code,
        stock_name=stock_name,
        write_cache=False,
        response_schema=response_schema,
        temperature=GEMINI_ANALYSIS_TEMPERATURE,
    )
    return _parse_weekly_gemini_points(output_text or "")


def _repair_weekly_points_with_required_branch(
    points: List[str],
    payload: dict,
    ctx: dict,
    stock_name: str,
) -> List[str]:
    """相容舊流程：若需強制補充分點資料，仍使用同一套 JSON 與接地規則。"""
    required = payload.get("required_representative_branch_analysis")
    if not required:
        return points

    stock_code = str(ctx.get("stock_code", "") or "")
    repair_payload = {
        "required_representative_branch_analysis": required,
        "required_price_volume_pattern_analysis": payload.get("price_volume_pattern", {}),
        "original_points": [str(p or "").strip() for p in points if str(p or "").strip()],
        "full_weekly_data": payload,
    }
    response_schema = _build_gemini_points_response_schema(
        min_points=3,
        max_points=3,
        include_note=False,
    )
    repair_prompt = f"""
你是專業且中立的台股研究員。上一版漏掉必要的代表性分點資料，請只依修正資料重寫。

原則：
1. 剛好 3 點；前 2 點為本週分析，第 3 點以「下週觀察：」開頭。
2. 每點使用「面向：...｜結果：具體結論｜說明：資料依據。」並以句號結束。
3. 其中 1 點完整使用 required_representative_branch_analysis，包含分點名稱、本週方向與金額、勝率、平均持有天數與歷史加權報酬率。
4. 另 1 點使用均線訊號與程式判定價量型態；不得羅列均線價格或只寫 KD、MACD。
5. 優先比較 previous_week_comparison，指出延續、反轉或分歧。
6. 每個數字都必須存在於 full_weekly_data；不得補數字、誇大法人中性訊號或提供買賣建議。

只回傳符合 JSON Schema 的 JSON。

修正資料：
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
"""
    output_text = _call_gemini_with_retry(
        repair_prompt,
        cache_task=f"{_weekly_keypoints_cache_task()}_required_branch_repair",
        stock_code=stock_code,
        stock_name=stock_name,
        write_cache=False,
        response_schema=response_schema,
        temperature=GEMINI_ANALYSIS_TEMPERATURE,
    )
    return _parse_weekly_gemini_points(output_text or "")


def _summarize_weekly_context_with_gemini(ctx: dict, stock_name: str) -> List[str]:
    """讓 Gemini 比較本週與上週，只快取完整驗證通過的三點結果。"""
    if not WEEKLY_KEYPOINT_LLM_ENABLE:
        return []
    try:
        payload = _build_weekly_llm_payload(ctx, stock_name)
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        stock_code = str(ctx.get("stock_code", "") or "")
        response_schema = _build_gemini_points_response_schema(
            min_points=3,
            max_points=3,
            include_note=False,
        )

        prompt = f"""
你是專業且中立的台股研究員。只能使用下方 JSON，整理「本週重點與下週觀察」，使用繁體中文。

分析原則：
1. 剛好輸出 3 點：前 2 點分析本週已發生事件，第 3 點必須以「下週觀察：」開頭。
2. 每點固定為「面向：技術面/權證面/法人面/新聞面｜結果：具體結論｜說明：資料依據。」；下週點使用面向：下週觀察。不得寫「重點待確認」「題材待觀察」等空句。
3. 優先比較 previous_week_comparison，指出權證淨流向、法人合計或 TOP5 分點的延續、反轉與方向分歧；不要只播報本週數字。
4. 點名分點時，只能使用代表性分點，並完整使用本週方向與金額、歷史勝率、平均持有天數、歷史加權報酬率；小額分點不得點名。
5. 技術面使用 price_ma_volume.ma_signal 與 price_volume_pattern.current_pattern_label；法人分類為接近中性時，只能描述方向有限或尚未明確。
6. 所有數字都必須在 JSON 中找到，不得換算、推估或補充；不得提供買賣建議。每點需獨立完整並以句號結束。

好範例：
- 面向：權證面｜結果：權證買盤連續增強｜說明：本週淨流向較上週改善，代表性分點同步買超並有完整歷史績效支持。
- 下週觀察：面向：下週觀察｜結果：確認價量能否轉強｜說明：若權證與法人方向同步改善，再觀察程式判定型態是否獲得量能確認。

壞範例：
- 面向：新聞面｜結果：題材仍待確認｜說明：後續持續關注。
- 面向：技術面｜結果：偏多｜說明：KD、MACD與均線值得留意。

只回傳符合 JSON Schema 的 JSON，不要 markdown 或其他說明。

本週完整資料：
{payload_json}
"""
        output_text = _call_gemini_with_retry(
            prompt,
            cache_task=_weekly_keypoints_cache_task(),
            stock_code=stock_code,
            stock_name=stock_name,
            write_cache=False,
            response_schema=response_schema,
            temperature=GEMINI_ANALYSIS_TEMPERATURE,
        )
        points = _parse_weekly_gemini_points(output_text or "")
        points = _supplement_weekly_branch_metrics_in_points(points, ctx)
        problems = _validate_weekly_points(points, payload, ctx)

        if problems:
            print("⚠️ Gemini 本週重點與下週觀察需要修正：" + "；".join(problems))
            repaired_points = _repair_weekly_expert_points(
                points,
                payload,
                ctx,
                stock_name,
                problems,
            )
            repaired_problems = _validate_weekly_points(
                repaired_points,
                payload,
                ctx,
            )

            if repaired_problems:
                print("⚠️ Gemini 修正後仍未達要求，改用條件式備援：" + "；".join(repaired_problems))
                return []

            points = repaired_points
            print("✅ Gemini 本週重點與下週觀察修正完成")

        _save_validated_weekly_points_cache(
            _weekly_keypoints_cache_task(),
            stock_code,
            stock_name,
            prompt,
            points,
            payload,
            ctx,
        )
        print(
            f"✅ Gemini 股票研究員分析完成：{len(points)} 點｜"
            f"下週觀察 {_weekly_points_watch_count(points)} 點｜"
            f"總字數約 {_count_summary_chars(points)} 字"
        )
        return points[:3]
    except Exception as e:
        print(f"⚠️ Gemini 本週重點與下週觀察整理失敗，改用條件式備援：{e}")
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
        "1. 最多輸出 3 點，每點 42 到 78 個中文字，說明必須是一句完整短句。\n"
        "2. 只能根據『內文』重寫成重點，不要直接複製新聞標題或原句。\n"
        "3. 不要出現『完整看』、『新聞線索』、『來源』、新聞網站名稱或多檔股名清單。\n"
        "4. 每點要像財經新聞摘要，格式盡量為「短標籤：具體事件／數字／市場消息 + 對公司或產業的影響」，不要寫成空泛研究報告。\n"
        "4-1. 不得輸出「法人尚未表態」「法人消息不足」「法人看法待確認」這類沒有資訊量的法人觀點；沒有具體券商、評等、目標價、EPS上修/下修或法人買賣超，就改寫其他有內容的新聞或直接少輸出。\n"
        "5. 只聚焦公司本身可能影響股價的消息：公司產業、法人目標價/評等、EPS/每股純益、營收、毛利率、獲利、ASP/報價、接單出貨、產能與供需。\n"
        "6. 若目標價、EPS、營收、ASP、毛利率或產業題材沒有明確指向本公司，請不要使用。\n"
        "6-1. 必須確認新聞明確對應目標股票代號或公司名；若公司名稱相近，例如威健與威健生技這種不同公司，沒有目標股票代號就不得使用。\n"
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


def _collect_news_title_candidates(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    """極速 RSS 模式常只有標題與摘要；當原文句子不足時，用通過品質門檻的標題作備援。"""
    candidates = []
    seen = set()
    aliases = [a for a in _get_news_aliases(stock_code, stock_name) if a] if (stock_code or stock_name) else []
    for rec in records or []:
        title = _clean_news_title(rec.get("title", ""))
        content = _normalize_news_text(rec.get("content", ""))
        if not title:
            continue
        combined = _normalize_news_text(f"{title}。{content}")
        if aliases and not _news_text_matches_target_stock(combined, stock_code, stock_name):
            continue
        if not _passes_news_quality_gate(title, content or title, stock_code, stock_name):
            continue
        # 純股價標題仍排除；但若同句有營收、EPS、需求、訂單等基本面字眼則保留。
        if _is_low_value_market_news(combined) and not _has_substantive_company_news(combined):
            continue
        # 避免 RSS title 與 description 完全相同時重複一次，造成代號 / 數字被過濾規則誤判。
        if content and _title_compare_text(content) != _title_compare_text(title) and len(content) >= 18:
            text = combined
        else:
            text = title
        text = _trim_news_point(text, max_len=NEWS_SUMMARY_POINT_MAX_LEN + 26)
        if not text:
            continue
        if _is_bad_news_sentence(text) and not _has_substantive_company_news(text):
            continue
        key = _title_compare_text(text)
        if not key or key in seen:
            continue
        candidates.append({
            "text": text,
            "source": str(rec.get("source", "") or ""),
            "title": title,
        })
        seen.add(key)
    return candidates


def _infer_news_label_from_text(text: str, fallback_label: str = "新聞焦點") -> str:
    s = _normalize_news_text(text)
    if re.search(r"營收|月增|年增|財報|獲利|EPS|毛利|毛利率|每股", s, re.I):
        return "業績更新"
    if re.search(r"AI|伺服器|散熱|液冷|ASIC|GPU|供需|需求|報價|漲價|長約", s, re.I):
        return "產業題材"
    if re.search(r"法人|外資|投信|券商|評等|目標價|調升|調降", s):
        return "法人觀點"
    if re.search(r"公告|重大訊息|董事會|投資|合作|擴產|產能|接單|出貨|客戶", s):
        return "公司動態"
    return str(fallback_label or "新聞焦點").strip() or "新聞焦點"


def _infer_news_conclusion(label: str, text: str) -> str:
    s = _normalize_news_text(text)
    if re.search(r"營收", s):
        has_year_up = re.search(r"年增|年成長|YoY|同期高|創同期高|創高", s, re.I)
        has_month_down = re.search(r"月減|月衰退|月增率-|-\d+(?:\.\d+)?%", s)
        has_month_up = re.search(r"月增|月成長", s) and not has_month_down
        if has_year_up and has_month_down:
            return "營收表現偏正向，月減留待後續觀察。"
        if has_year_up or has_month_up:
            return "營收表現偏正向。"
        return "營收變化是本週主要基本面訊息。"
    if re.search(r"散熱|液冷|水冷", s, re.I):
        return "AI散熱題材仍是市場焦點。"
    if re.search(r"AI|伺服器|GPU|ASIC", s, re.I):
        return "AI業務布局仍是市場焦點。"
    if re.search(r"法人|評等|目標價|EPS|調升|調降", s):
        return "市場預期仍在重新評估。"
    if re.search(r"公告|重大訊息|董事會|投資|合作|擴產", s):
        return "公司事件需觀察後續落地。"
    return "本週新聞提供後續追蹤線索。"


def _infer_news_watch(label: str, text: str) -> str:
    s = _normalize_news_text(text)
    if re.search(r"營收|月增|年增|財報|獲利|EPS|毛利", s, re.I):
        return "追蹤下月營收、毛利率與法說展望。"
    if re.search(r"散熱|液冷|水冷", s, re.I):
        return "追蹤AI散熱訂單與出貨延續性。"
    if re.search(r"AI|伺服器|GPU|ASIC|長約|需求", s, re.I):
        return "追蹤AI／ASIC業務進展與營收貢獻。"
    if re.search(r"法人|評等|目標價|調升|調降", s):
        return "追蹤EPS預估與法人看法是否延續。"
    if re.search(r"公告|重大訊息|合作|投資|擴產", s):
        return "追蹤公告後續進度與實際貢獻。"
    return "追蹤後續公告與營運數字驗證。"


def _compact_news_fact_text(text: str, max_len: int = 54) -> str:
    s = _normalize_news_text(text)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip("。；;，, ")
    s = re.sub(r"^(焦點股|個股|台股|盤中|盤後|標題)[:：]?", "", s).strip()
    s = re.sub(r"(?:^|[。；;])\s*標題[:：]\s*", "", s).strip()
    # 優先保留含數字或基本面關鍵字的片段。
    parts = _split_news_sentences(s) or [s]
    scored = []
    for part in parts:
        part = _normalize_news_text(part).strip("。；;，, ")
        if not part:
            continue
        score = 0
        if re.search(r"\d+(?:\.\d+)?\s*(%|％|元|億元|萬|月|年)", part):
            score += 4
        if re.search(r"營收|年增|月增|月減|EPS|毛利|AI|伺服器|散熱|液冷|需求|訂單|出貨|法人|目標價|評等", part, re.I):
            score += 3
        scored.append((score, part))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        s = scored[0][1]
    if len(s) > max_len:
        cut = max(s.rfind("，", 0, max_len + 1), s.rfind("、", 0, max_len + 1))
        if cut >= 18:
            s = s[:cut]
        else:
            s = s[:max_len]
    return s.strip("。；;，, ")


def _infer_news_headline(label: str, text: str) -> str:
    """新聞卡第一行的具體短結論；避免只顯示偏多/偏弱或被月減誤判成綠色。"""
    s = _normalize_news_text(text)
    label_s = str(label or "")
    if _has_negative_target_price_or_rating(s):
        return "市場預期轉弱"
    if _has_positive_target_price_or_rating(s):
        return "市場預期偏正向"
    if _has_neutral_target_price_or_rating(s):
        return "法人看法待確認"
    if any(k in s for k in ["傳捷報", "捷報", "接單", "訂單", "新加坡", "取得訂單", "新增訂單"]):
        return "公司動態偏正向"
    if any(k in s for k in ["看好", "看旺", "評等調升", "上修", "調升", "法說看好", "未來展望"]):
        return "市場預期偏正向"
    if any(k in s for k in ["利多", "受惠", "需求強勁", "需求延續", "AI散熱", "液冷"]):
        return "AI散熱需求強勁"
    if _has_negative_target_price_or_rating(s) or any(k in s for k in ["調降", "下修", "看壞", "利空", "需求疲弱"]):
        return "市場預期轉弱"
    if "營收" in s:
        if "年增" in s and "月減" in s:
            return "營收表現偏正向"
        if (
            any(k in s for k in ["年增", "月增", "成長", "創高", "同期高", "續創新高", "改寫新高"])
            or re.search(r"創\d+季新高", s)
            or re.search(r"營收.{0,8}(?:創|續創|改寫).{0,8}新高", s)
        ):
            return "營收表現偏正向"
        if any(k in s for k in ["年減", "月減", "衰退"]):
            return "營收動能轉弱"
        return "營收變化待追蹤"
    if re.search(r"散熱|液冷|水冷", s, re.I):
        return "AI散熱需求強勁"
    if re.search(r"AI|伺服器|GPU|ASIC", s, re.I):
        return "AI業務布局受關注"
    if re.search(r"研發|平台|新應用|新產品|推出|開發", s):
        return "產品布局待追蹤"
    if re.search(r"法人|評等|目標價|EPS|調升|調降", s):
        if _has_positive_target_price_or_rating(s) or any(k in s for k in ["調升", "上修", "看旺"]):
            return "市場預期偏正向"
        return "法人看法待確認"
    if re.search(r"公告|重大訊息|董事會|投資|合作|擴產|接單|出貨|客戶|研發|平台|新應用|新產品|推出|開發", s):
        return "公司動態待追蹤"
    if "業績" in label_s:
        return "業績表現待觀察"
    if "公司" in label_s:
        return "公司動態待追蹤"
    if "產業" in label_s or "題材" in label_s:
        return "題材熱度待觀察"
    return "新聞事件待追蹤"


def _make_news_keypoint(label: str, sentence: str, stock_code: str, stock_name: str) -> str:
    """規則式備援：輸出可直接放入圖片的「分類｜結論｜重點｜觀察」短格式。"""
    s = _normalize_news_text(sentence)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = s.strip("。；;，, ")
    if not s:
        return ""

    label = _infer_news_label_from_text(s, fallback_label=label)
    fact = _compact_news_fact_text(s, max_len=54)
    if not fact:
        return ""
    # 多家公司題材新聞若直接顯示原始標題，容易看起來像把其他公司內容混進來；
    # 圖卡只保留本股票與題材的關係，避免右下角變成新聞標題列表。
    if label == "產業題材" and stock_name and "、" in fact and re.search(r"AI|伺服器|散熱|液冷|GPU|ASIC", fact, re.I):
        fact = f"{stock_name}仍被市場放在AI散熱需求題材中觀察"
    headline = _infer_news_headline(label, s)
    watch = _infer_news_watch(label, s).replace("追蹤", "後續看", 1)
    detail = f"{fact}；{watch}"
    point = f"{label}｜結果：{headline}｜說明：{detail}"
    point = _normalize_chinese_numbers_for_news(point)
    point = _trim_news_point(point, max_len=NEWS_SUMMARY_POINT_MAX_LEN)
    if not point or _is_bad_news_sentence(point):
        return ""
    return point

def _rule_based_news_summary(records: List[dict], stock_code: str, stock_name: str) -> List[str]:
    """規則式新聞摘要備援。

    Gemini 若輸出 0 點，仍優先從已通過新聞品質門檻的 RSS 標題 / 摘要整理，
    但避免直接把原始標題丟進圖卡；輸出仍維持「分類｜結果：...｜說明：...」。
    """
    # 同時使用合格的摘要句與標題候選；原本只要摘要句不為空，就完全不看標題，
    # 容易讓法說、ASIC 進展或法人評等等獨立事件無法補成第 3 點。
    candidates = list(_collect_news_sentences(records, stock_code, stock_name) or [])
    seen_candidate_keys = {
        _title_compare_text(candidate.get("text", ""))
        for candidate in candidates
        if candidate.get("text")
    }
    for candidate in _collect_news_title_candidates(records, stock_code, stock_name) or []:
        candidate_key = _title_compare_text(candidate.get("text", ""))
        if not candidate_key or candidate_key in seen_candidate_keys:
            continue
        candidates.append(candidate)
        seen_candidate_keys.add(candidate_key)
    if not candidates:
        return []

    def make_clean_point(label: str, text: str) -> str:
        return _make_news_keypoint(label, text, stock_code, stock_name)

    points = []
    used_keys = set()
    target_points = max(1, min(3, int(NEWS_SUMMARY_MAX_POINTS)))

    # 1) 業績更新：優先抓營收年增 / 月減 / 月增，雙鴻這類「年增但月減」要呈現偏正向但提醒月減。
    revenue_candidates = []
    for c in candidates:
        text = c.get("text", "")
        if not text:
            continue
        if re.search(r"營收|月增|月減|年增|業績|財報|EPS|毛利", text, re.I):
            score = _score_news_sentence(text, ["營收", "年增", "月減", "月增", "業績", stock_code, stock_name], stock_code, stock_name)
            # 同時有年增與月減者，應優先保留，避免被股價類標題蓋掉。
            if "營收" in text and "年增" in text and "月減" in text:
                score += 20
            revenue_candidates.append((score, text))
    revenue_candidates.sort(key=lambda x: x[0], reverse=True)
    if revenue_candidates:
        p = make_clean_point("業績更新", revenue_candidates[0][1])
        if p:
            points.append(p)
            used_keys.add(_title_compare_text(revenue_candidates[0][1]))

    # 2) 產業題材：AI / 散熱 / 液冷等題材，與營收事件分開顯示。
    industry_candidates = []
    for c in candidates:
        text = c.get("text", "")
        if not text:
            continue
        key = _title_compare_text(text)
        if key in used_keys:
            continue
        if re.search(r"AI|伺服器|散熱|液冷|GPU|ASIC|需求|訂單|出貨|長約", text, re.I):
            score = _score_news_sentence(text, ["AI", "伺服器", "散熱", "液冷", "需求", "訂單", "出貨", stock_code, stock_name], stock_code, stock_name)
            industry_candidates.append((score, text))
    industry_candidates.sort(key=lambda x: x[0], reverse=True)
    if industry_candidates and len(points) < NEWS_SUMMARY_MAX_POINTS:
        p = make_clean_point("產業題材", industry_candidates[0][1])
        if p:
            points.append(p)
            used_keys.add(_title_compare_text(industry_candidates[0][1]))

    # 3) 其他公司資訊：法人觀點 / 公司動態，用於補足第 3 個不同事件。
    if len(points) < target_points:
        other_categories = [
            ("法人觀點", ["外資", "投信", "券商", "法人", "評等", "目標價", "調升", "調降", "EPS"]),
            ("公司動態", ["公告", "重大訊息", "董事會", "投資", "合作", "擴產", "產能", "接單", "出貨"]),
            ("新聞焦點", [stock_code, stock_name, "營收", "AI", "散熱", "需求", "獲利", "毛利", "法說", "展望"]),
        ]
        for label, keywords in other_categories:
            scored = []
            for c in candidates:
                text = c.get("text", "")
                if not text:
                    continue
                key = _title_compare_text(text)
                if key in used_keys:
                    continue
                score = _score_news_sentence(text, keywords, stock_code, stock_name)
                if score > 0:
                    scored.append((score, text))
            scored.sort(key=lambda x: x[0], reverse=True)
            for _, text in scored:
                if len(points) >= target_points:
                    break
                p = make_clean_point(label, text)
                if p and not _is_bad_news_sentence(p):
                    points.append(p)
                    used_keys.add(_title_compare_text(text))
            if len(points) >= target_points:
                break

    return _ensure_news_summary_min_total(points, records, stock_code, stock_name)


def _load_gsheet_news_points_cache_for_display(stock_code: str, stock_name: str, allow_stale: bool = False) -> List[str]:
    """直接讀取 Google Sheet 的 news_points 快取，供新聞區塊顯示使用。

    原本快取只在 _call_gemini_with_retry() 內讀取；如果 Google News 沒抓到素材，
    流程會在 build_news_points() 提早 return，導致永遠不會讀到 Google Sheet 快取。
    這個函式放在 build_news_points() 前面直接查快取，確保當天跑過的新聞摘要能直接被圖片使用。
    """
    if not GSHEET_LLM_CACHE_ENABLE or LLM_CACHE_FORCE_REFRESH:
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
        sh = _open_gsheet()
        if sh is None:
            return []
        try:
            ws = sh.worksheet(GSHEET_LLM_CACHE_SHEET)
        except Exception:
            return []
        records = ws.get_all_records(empty2zero=False, head=1)
        df = pd.DataFrame(records).fillna("") if records else pd.DataFrame()
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

def _finalize_news_points_for_display(points: List[str], stock_code: str, stock_name: str, ctx: dict | None = None) -> List[str]:
    """只顯示真正通過品質門檻的新聞；結構化重點不得被一般標題分隔符誤刪。"""
    cleaned = _clean_news_summary_points_for_stock(points, stock_code, stock_name)
    cleaned = [p for p in cleaned if _points_are_independent_and_complete([p])]
    return cleaned[:NEWS_DISPLAY_MAX_POINTS]

def build_news_points(stock_code: str, stock_name: str, news_items, ctx: dict | None = None) -> List[str]:
    """整理高品質公司新聞；素材足夠時顯示 3 個不同事件，不足時保留實際可用點數。"""
    display_target = max(
        1,
        min(
            int(NEWS_DISPLAY_MAX_POINTS),
            int(NEWS_SUMMARY_MAX_POINTS),
        ),
    )
    cached_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=False)
    if cached_points and len(cached_points) >= display_target:
        return _finalize_news_points_for_display(cached_points, stock_code, stock_name, ctx)
    if cached_points:
        print(f"ℹ️ 當日新聞快取僅 {len(cached_points)} 點，低於顯示目標 {display_target} 點，繼續搜尋其他來源")

    records = _news_items_to_records(news_items)
    records = _enrich_fast_news_records_with_topk_bodies(
        records,
        stock_code,
        stock_name,
    )
    body_records = [
        r for r in records
        if r.get("body_ok")
        and len(_normalize_news_text(r.get("content", ""))) >= NEWS_MIN_BODY_CHARS
        and _passes_news_quality_gate(r.get("title", ""), r.get("content", ""), stock_code, stock_name)
    ]
    fallback_records = [
        r for r in records
        if r.get("fallback_ok")
        and _normalize_news_text(r.get("content", ""))
        and _passes_news_quality_gate(r.get("title", ""), r.get("content", ""), stock_code, stock_name)
    ]
    usable_records = body_records + [r for r in fallback_records if r not in body_records]
    available_count = _count_distinct_usable_news_articles(usable_records)
    required_points = min(display_target, max(1, available_count))

    if not usable_records:
        stale_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=True)
        if stale_points:
            return _finalize_news_points_for_display(stale_points, stock_code, stock_name, ctx)
        return []

    ai_points = _summarize_news_with_gemini(usable_records, stock_code, stock_name)
    final_points = _finalize_news_points_for_display(ai_points, stock_code, stock_name, ctx) if ai_points else []

    # 已有至少兩個不同事件，AI卻仍只留下1點時，用同一批合格素材的規則式摘要補足，
    # 不創造新聞、不拆分同一事件。
    if len(final_points) < required_points:
        rule_points = _rule_based_news_summary(usable_records, stock_code, stock_name)
        combined = list(final_points)
        for point in _finalize_news_points_for_display(rule_points, stock_code, stock_name, ctx):
            if point in combined:
                continue
            # 避免文字高度相似的同一事件重複顯示。
            point_key = _title_compare_text(point)
            if any(_title_compare_text(existing) == point_key for existing in combined):
                continue
            combined.append(point)
            if len(combined) >= required_points:
                break
        final_points = combined[:NEWS_DISPLAY_MAX_POINTS]

    if final_points:
        if len(final_points) < required_points:
            print(
                f"⚠️ 所有來源與規則式補充完成後仍只有 {len(final_points)} 個可獨立呈現的新聞事件；"
                "為避免垃圾新聞或重複事件，保留現有內容"
            )
        else:
            # Gemini 失敗但規則式摘要成功時，也要把通過品質檢查的最終顯示結果寫入當日快取；
            # 下次 Action 選 0 才會固定使用同一份正常新聞，不會再重跑出不同文字。
            _save_validated_news_points_cache(
                _news_points_cache_task(),
                stock_code,
                stock_name,
                f"rule_based_news_cache::{stock_code}::{_taipei_today_str()}",
                final_points,
                note="validated_rule_based_news_points",
            )
        return final_points

    stale_points = _load_gsheet_news_points_cache_for_display(stock_code, stock_name, allow_stale=True)
    if stale_points:
        return _finalize_news_points_for_display(stale_points, stock_code, stock_name, ctx)
    return []

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


def add_weighted_volume_profile_overlay(ax, df: pd.DataFrame, n_bins: int = 40, color="#38BDF8", alpha=0.15, scale=1.08):
    stats = _calculate_weighted_volume_profile_stats(df, n_bins=n_bins)
    if not stats:
        return
    centers = stats["centers"]
    height = float(stats["height"])
    profile = stats["profile"]
    max_idx = int(stats["max_idx"])
    second_idx = int(stats["second_idx"])
    if len(profile) == 0 or float(profile.max()) <= 0:
        return
    scaled = profile / profile.max()
    x_min, x_max = ax.get_xlim()
    width_max = (x_max - x_min) / scale
    for i in range(len(profile)):
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
    # K 棒本體略放大，搭配 K 線區塊高度提高後，顯示效果更接近技術分析圖，
    # 不是只靠縮小 Y 軸留白來「假性放大」。
    width = 0.82
    for i in x:
        color = RED if up.iloc[i] else GREEN
        op, cl = float(plot_df["Open"].iloc[i]), float(plot_df["Close"].iloc[i])
        hi, lo = float(plot_df["High"].iloc[i]), float(plot_df["Low"].iloc[i])
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.65, zorder=3)
        body_low = min(op, cl)
        body_h = abs(cl - op)
        if body_h < max(0.01, cl * 0.0005):
            ax.plot([i - width / 2, i + width / 2], [cl, cl], color=color, linewidth=3.0, zorder=4)
        else:
            ax.bar(i, body_h, bottom=body_low, width=width, color=color, edgecolor=color, linewidth=0.8, align="center", zorder=4)


def adjust_candle_price_ylim(ax, plot_df: pd.DataFrame):
    """讓週報 K 線 Y 軸顯示方式接近 K_function。

    重點：
    1. Y 軸主要只依照 K 棒 Low / High 決定。
    2. 不再把 MA60、BB_LOWER 納入最低範圍。
    3. 因此早期 MA60 / 布林下軌若低於 K 棒區間，會自然被底部裁切。
    4. 不改 GridSpec，不壓縮 K 棒，只改視窗裁切範圍。
    """
    if plot_df is None or plot_df.empty:
        return

    low_s = pd.to_numeric(plot_df["Low"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    high_s = pd.to_numeric(plot_df["High"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()

    if low_s.empty or high_s.empty:
        return

    y_min = float(low_s.min())
    y_max = float(high_s.max())

    if not np.isfinite(y_min) or not np.isfinite(y_max):
        return

    y_span = max(y_max - y_min, 1e-6)

    # 模仿 K_function：下方保留一點空間，但不為 MA60 / BB_LOWER 額外拉低 Y 軸。
    lower_pad = y_span * 0.11
    upper_pad = y_span * 0.05

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
    buy_top, sell_top = _get_cached_top_branch_tables(ctx, "current_week", week_events, topn=5)
    compact_mode = is_compact_report_mode()
    if compact_mode:
        # 精簡模式不建立本週重點與新聞內容，避免不必要的新聞抓取 / Gemini 呼叫。
        key_points = []
        news_points = []
        # K 線區塊改為實際放大：同步增加整張圖高度與 K 線 row ratio，
        # 避免只是壓縮下方指標或單純縮小 Y 軸上下留白。
        fig = plt.figure(figsize=(28, 51.0), facecolor=BG)
        gs = GridSpec(8, 12, figure=fig,
                      height_ratios=[1.45, 2.05, 13.1, 2.45, 3.1, 5.0, 4.7, 9.55],
                      hspace=0.20, wspace=0.25)
    else:
        with report_stage_timer(f"{stock_code}｜Gemini 新聞統整"):
            news_points = build_news_points(stock_code, stock_name, news_items, ctx)
        ctx["weekly_news_points"] = list(news_points or [])
        with report_stage_timer(f"{stock_code}｜Gemini 本週重點"):
            key_points = build_key_points(ctx, stock_name)
        # K 線區塊改為實際放大：同步增加整張圖高度與 K 線 row ratio，
        # 避免只是壓縮下方指標或單純縮小 Y 軸上下留白。
        fig = plt.figure(figsize=(28, 62.3), facecolor=BG)
        gs = GridSpec(9, 12, figure=fig,
                      height_ratios=[1.45, 2.05, 13.1, 2.45, 3.1, 5.0, 4.7, 9.55, 9.05],
                      hspace=0.20, wspace=0.25)

    # Matplotlib renderer 共用快取：
    # 第一次需要量測文字時才完整 draw 一次，之後所有文字寬度量測都重用同一 renderer，
    # 避免 draw_header_text_and_advance / wrap_text_by_pixel / _measure_text_width_axes
    # 每次都重新繪製整張長圖。
    renderer_cache = {"renderer": None}

    def get_cached_renderer():
        renderer = renderer_cache.get("renderer")
        if renderer is None:
            render_start = time.perf_counter()
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            renderer_cache["renderer"] = renderer
            if REPORT_TIMING_ENABLE:
                print(f"⏱️ {stock_code}｜Matplotlib 首次完整 render：{time.perf_counter() - render_start:.2f} 秒")
        return renderer

    # Header
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.set_axis_off()
    period = f"{ctx['week_start'].strftime('%Y/%m/%d')} - {ctx['week_end'].strftime('%Y/%m/%d')}" if pd.notna(ctx["week_start"]) else "-"
    ax_header.text(0.01, 0.50, f"{stock_code} {stock_name}｜權證資金流週報", color=GOLD, fontsize=68, fontweight="bold", ha="left", va="center")
    ax_header.text(0.01, -0.10, f"週報區間：{period}｜資訊僅供參考", color=MUTED, fontsize=32, ha="left", va="center")
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
    candle_ax.text(0.012, 0.94, latest_info, transform=candle_ax.transAxes, color=TEXT, fontsize=27, ha="left", va="top",
                   bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.30", alpha=0.95))
    ma_note = get_ma_kline_signals(plot_df)
    if ma_note:
        candle_ax.text(0.5, 0.08, ma_note, transform=candle_ax.transAxes, color=GOLD, fontsize=34, fontweight="bold", ha="center", va="center",
                       bbox=dict(facecolor="#F6F8FB", edgecolor=GOLD, boxstyle="round,pad=0.28", alpha=0.95))

        # 權證資金流 / 成交量標題列共用 helper
    # 這段一定要放在 Volume 前面，因為成交量區塊會先用到這幾個函式。
    header_y = 1.062

    def advance_x_by_px(ax, x0, gap_px, y=None):
        y = header_y if y is None else y
        base_xy = ax.transAxes.transform((x0, y))
        return ax.transAxes.inverted().transform((base_xy[0] + gap_px, base_xy[1]))[0]

    def draw_header_text_and_advance(
        ax, x0, text, color,
        fontsize=22, fontweight="bold", gap_px=16, alpha=1.0, y=None
    ):
        y = header_y if y is None else y
        t = ax.text(
            x0, y, text,
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
        renderer = get_cached_renderer()
        bbox = t.get_window_extent(renderer=renderer)
        y_disp = ax.transAxes.transform((0, y))[1]
        return ax.transAxes.inverted().transform((bbox.x1 + gap_px, y_disp))[0]

    def draw_header_bar_and_advance(ax, x0, color, gap_px=8, y=None):
        y = header_y if y is None else y
        bar_w = 0.013
        ax.add_patch(Rectangle(
            (x0, y - 0.012), bar_w, 0.024,
            transform=ax.transAxes,
            facecolor=color,
            edgecolor=color,
            linewidth=0,
            alpha=0.92,
            clip_on=False,
            zorder=12,
        ))
        return advance_x_by_px(ax, x0 + bar_w, gap_px, y=y)

    def draw_header_line_and_advance(ax, x0, color, gap_px=10, y=None):
        y = header_y if y is None else y
        line_w = 0.030
        ax.plot(
            [x0, x0 + line_w], [y, y],
            transform=ax.transAxes,
            color=color,
            linewidth=2.6,
            alpha=0.95,
            solid_capstyle="round",
            clip_on=False,
            zorder=12,
        )
        return advance_x_by_px(ax, x0 + line_w, gap_px, y=y)

    # Volume
    vol_ax = fig.add_subplot(gs[3, :], sharex=candle_ax)
    style_ax(vol_ax)
    up = plot_df["Close"] >= plot_df["Open"]
    vol_lots = plot_df["Volume"] / 1000

    vol_ax.bar([i for i in x if up.iloc[i]], vol_lots[up], color=RED, width=0.72, alpha=0.72)
    vol_ax.bar([i for i in x if not up.iloc[i]], vol_lots[~up], color=GREEN, width=0.72, alpha=0.72)

    mv5_lots = plot_df["MV5"] / 1000
    mv20_lots = plot_df["MV20"] / 1000
    vol_ax.plot(x, mv5_lots, color=BLUE, linewidth=2.1)
    vol_ax.plot(x, mv20_lots, color=PURPLE, linewidth=2.1)

    latest_vol = float(vol_lots.iloc[-1]) if len(vol_lots) else 0.0
    latest_mv5 = float(mv5_lots.iloc[-1]) if len(mv5_lots) else 0.0
    latest_mv20 = float(mv20_lots.iloc[-1]) if len(mv20_lots) else 0.0
    latest_vol_color = NAVY
    vol_header_y = 1.14
    
    xpos = 0.001
    xpos = draw_header_text_and_advance(
        vol_ax, xpos, "成交量", GOLD,
        fontsize=34, fontweight="bold", gap_px=22, y=vol_header_y,
    )

    xpos = draw_header_text_and_advance(
        vol_ax, xpos, "|", MUTED,
        fontsize=25, fontweight="bold", gap_px=14, alpha=0.82, y=vol_header_y,
    )
    xpos = draw_header_bar_and_advance(
        vol_ax, xpos, latest_vol_color, gap_px=8, y=vol_header_y,
    )
    xpos = draw_header_text_and_advance(
        vol_ax, xpos, f"成交量 {latest_vol:,.0f}張",
        latest_vol_color, gap_px=22, y=vol_header_y,
    )

    xpos = draw_header_text_and_advance(
        vol_ax, xpos, "|", MUTED,
        fontsize=25, fontweight="bold", gap_px=14, alpha=0.82, y=vol_header_y,
    )
    xpos = draw_header_line_and_advance(
        vol_ax, xpos, BLUE, gap_px=10, y=vol_header_y,
    )
    xpos = draw_header_text_and_advance(
        vol_ax, xpos, f"MV5 {latest_mv5:,.0f}張",
        BLUE, gap_px=22, y=vol_header_y,
    )

    xpos = draw_header_text_and_advance(
        vol_ax, xpos, "|", MUTED,
        fontsize=25, fontweight="bold", gap_px=14, alpha=0.82, y=vol_header_y,
    )
    xpos = draw_header_line_and_advance(
        vol_ax, xpos, PURPLE, gap_px=10, y=vol_header_y,
    )
    draw_header_text_and_advance(
        vol_ax, xpos, f"MV20 {latest_mv20:,.0f}張",
        PURPLE, gap_px=0, y=vol_header_y,
    )
    adjust_volume_ylim(vol_ax, plot_df)
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
            fontsize=22,
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
        # 版面恢復接近原始大字級文字區塊：卡片、標題與內文空間維持原本配置；
        # 只在每個面向第一行拆出「面向 + 結果」，並只讓結果文字依台股習慣上紅 / 綠 / 灰。
        ax_notes = fig.add_subplot(gs[8, :]); ax_notes.set_axis_off(); ax_notes.set_facecolor(BG)
        for x0, title in [(0.02, "本週重點與下週觀察"), (0.52, "本週新聞 / 題材觀察")]:
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
            ax_notes.text(
                x0 + 0.02,
                note_y + note_h - 0.105,
                title,
                transform=ax_notes.transAxes,
                color=GOLD,
                fontsize=46,
                fontweight="bold",
                ha="left",
                va="top",
                clip_on=False,
                zorder=6,
            )

        notes_right_padding = 0.025

        def wrap_text_by_pixel(ax, fig, text, max_width_axes, fontsize=33, fontweight="normal", max_lines=0, first_prefix="", next_prefix="", width_boost=1.0):
            """依照實際像素寬度自動換行，避免固定字數造成太早換行或超出區塊邊界。"""
            s = str(text or "").strip()
            if not s:
                return []

            renderer = get_cached_renderer()
            ax_bbox = ax.get_window_extent(renderer=renderer)
            # width_boost 保留原本可放寬每行字數的行為；左下與右下文字卡會依不同區塊傳入放寬比例。
            safe_width_boost = float(width_boost or 1.0)
            safe_width_boost = min(1.35, max(0.70, safe_width_boost))
            max_width_px = max(float(max_width_axes), 0.01) * ax_bbox.width * safe_width_boost

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

            max_lines_int = int(max_lines or 0)
            if max_lines_int > 0 and len(lines) > max_lines_int:
                # 超過可顯示行數時，只保留可完整呈現的行，並用句號收尾；不使用省略號。
                # 主要的防線仍是在 Gemini prompt 與 _compact_card_sentence 先把內容壓短，避免畫面出現「句子被截斷」的感覺。
                lines = lines[:max_lines_int]
                lines[-1] = lines[-1].rstrip("；;，,、｜:： ")
                if lines[-1] and lines[-1][-1] not in "。！？":
                    lines[-1] += "。"
            return lines

        def _normalize_card_text(text: str) -> str:
            s = _normalize_news_text(text)
            s = s.replace("|", "｜")
            s = re.sub(r"\s+", " ", s).strip()
            return s

        def _parse_status_fields(text):
            """解析新版「面向｜結果｜說明」與舊版「分類｜結論｜重點｜觀察」。"""
            s = _normalize_card_text(text)
            if not s:
                return {}

            fields = {}
            if re.match(r"^下週觀察[:：]", s):
                fields["面向"] = "下週觀察"
                s = re.sub(r"^下週觀察[:：]\s*", "", s)

            parts = [p.strip() for p in re.split(r"｜+", s) if p.strip()]
            known_keys = {
                "面向", "結果", "說明", "分類", "結論", "依據", "重點", "觀察", "條件", "追蹤", "影響", "狀態",
            }

            # 新版最理想格式：技術面｜偏弱整理｜說明文字
            if len(parts) >= 3:
                p0 = re.sub(r"[：:。；;，,、\s]+", "", parts[0])
                p1 = parts[1].strip()
                if p0 and p0 not in known_keys and not re.match(r"^[^:：]{2,8}[:：]", parts[0]) and not re.match(r"^[^:：]{2,8}[:：]", parts[1]):
                    if len(p0) <= 6:
                        fields.setdefault("面向", p0)
                        fields.setdefault("結果", p1)
                        fields.setdefault("說明", "；".join(parts[2:]).strip())

            for idx, part in enumerate(parts):
                m = re.match(r"^([^:：]{2,8})[:：]\s*(.+)$", part)
                if m:
                    key = m.group(1).strip()
                    val = m.group(2).strip()
                    if key in known_keys:
                        fields[key] = val
                    elif idx == 0:
                        fields.setdefault("分類", key)
                        fields.setdefault("重點", val)
                    else:
                        fields.setdefault("重點", part)
                else:
                    clean_part = part.strip("。；;，,、 ")
                    if not clean_part or clean_part in known_keys:
                        continue
                    if idx == 0 and len(clean_part) <= 8:
                        if any(k in clean_part for k in ["技術", "權證", "法人", "新聞", "業績", "產業", "公司", "題材", "下週"]):
                            fields.setdefault("面向", clean_part if clean_part != "下週" else "下週觀察")
                        else:
                            fields.setdefault("分類", clean_part)
                    elif "重點" not in fields:
                        fields["重點"] = clean_part
                    elif "觀察" not in fields:
                        fields["觀察"] = clean_part

            return fields

        def _strip_status_labels(text):
            s = _normalize_card_text(text)
            s = re.sub(r"^下週觀察[:：]\s*", "", s)
            s = re.sub(r"^(面向|結果|說明|結論|依據|重點|觀察|條件|追蹤|影響|狀態)[:：]", "", s)
            s = re.sub(r"｜\s*(面向|結果|說明|結論|依據|重點|觀察|條件|追蹤|影響|狀態)[:：]", "。", s)
            s = re.sub(r"^[^｜:：]{2,8}｜[^｜:：]{1,12}｜", "", s)
            s = re.sub(r"^[^｜:：]{2,8}｜", "", s)
            s = re.sub(r"\s+", " ", s).strip(" ｜")
            return s.strip()

        def _compact_card_sentence(text, max_chars=92):
            """將說明壓成可放進圖卡的完整短句；避免半句被硬加句號。"""
            s = _normalize_card_text(text)
            if not s:
                return ""
            s = re.sub(r"(?:^|[。；;])\s*標題[:：]\s*", "", s).strip()
            s = re.sub(r"(?:\.\.\.|…+)", "", s).strip("；;，,、 ")
            if not s:
                return ""

            unfinished_tail = ("仍存在", "持續", "以及", "並", "且", "是否", "上方存在", "觀察", "追蹤", "若")

            def finish(t: str) -> str:
                t = str(t or "").strip("；;，,、｜:： ")
                for tail in unfinished_tail:
                    if t.endswith(tail):
                        t = t[: -len(tail)].rstrip("；;，,、｜:： ")
                if t and t[-1] not in "。！？":
                    t += "。"
                return t

            if len(s) <= max_chars:
                return finish(s)

            # 優先保留 max_chars 以內的完整句。
            prefix = s[:max_chars]
            sentence_ends = [m.end() for m in re.finditer(r"[。！？]", prefix)]
            if sentence_ends and sentence_ends[-1] >= max(26, int(max_chars * 0.45)):
                return finish(prefix[:sentence_ends[-1]])

            # 沒有完整句時，切在最接近的分號或逗號；不要直接切在任意字元。
            clause_positions = [prefix.rfind(p) for p in ["；", ";", "，", ",", "、"]]
            clause_idx = max(clause_positions)
            if clause_idx >= max(24, int(max_chars * 0.45)):
                return finish(prefix[:clause_idx])

            # 最後才保留前段，但會去掉看起來未完成的尾巴。
            return finish(prefix)

        def _infer_face_label_from_text(text, fallback="重點面"):
            s = str(text or "")
            if any(k in s for k in ["技術", "均線", "K線", "布林", "跌破", "站回", "量能", "型態", "價量"]):
                return "技術面"
            if any(k in s for k in ["權證", "分點", "資金流", "買超", "賣超", "淨流入", "淨流出"]):
                return "權證面"
            if any(k in s for k in ["法人", "外資", "投信", "自營", "三大法人"]):
                return "法人面"
            if any(k in s for k in ["新聞", "營收", "產業", "題材", "法說", "訂單", "毛利"]):
                return "新聞面"
            if any(k in s for k in ["下週", "觀察", "追蹤", "留意"]):
                return "下週觀察"
            return fallback

        GENERIC_STATUS_WORDS = {
            "偏多", "中性偏多", "偏多觀察", "偏弱", "中性偏弱", "偏弱整理", "偏弱觀察",
            "中性", "中性觀察", "觀望", "待確認", "方向未明", "仍待確認", "偏正向", "偏負向", "正向", "負向",
            "題材仍待確認", "重點待確認", "新聞重點待確認", "公司動態待驗證", "法人看法待確認",
            "法說展望待確認", "業績表現待確認", "產業題材待確認", "題材熱度待確認", "題材熱度待觀察", "公司動態待確認", "公司動態待追蹤", "新聞事件待追蹤",
        }

        def _is_generic_status_phrase(text: str) -> bool:
            s = _normalize_card_text(text)
            s = re.sub(r"^(結論|結果|狀態)[:：]\s*", "", s).strip("。；;，,、 ")
            if not s:
                return True
            if s in GENERIC_STATUS_WORDS:
                return True
            # 只有方向詞、沒有具體事件詞時，視為太籠統，改從說明內提煉重點短句。
            concrete_terms = [
                "股價", "短均", "均線", "量區", "型態", "權證", "資金", "分點", "法人", "營收",
                "AI", "散熱", "需求", "訂單", "報價", "獲利", "毛利", "法說", "突破", "跌破", "元大", "新光", "富邦", "永豐", "華南",
            ]
            direction_terms = ["偏多", "偏弱", "中性", "觀察", "轉強", "轉弱", "正向", "負向"]
            return any(k in s for k in direction_terms) and not any(k in s for k in concrete_terms)

        def _derive_headline_from_body(label: str, body: str, fallback: str = "重點待確認") -> str:
            label_s = str(label or "")
            s = _normalize_card_text(body)
            merged = label_s + "｜" + s
            if not s:
                return fallback

            # 正負面新聞與公司事件優先轉成具體重點短句，避免只顯示「題材仍待確認」。
            if any(k in merged for k in ["傳捷報", "捷報", "接單", "訂單", "新加坡", "取得訂單", "新增訂單"]):
                return "公司動態偏正向"
            if _has_positive_target_price_or_rating(merged) or any(k in merged for k in ["看好", "看旺", "評等調升", "調升", "上修", "法說看好", "未來展望"]):
                return "市場預期偏正向"
            if _has_neutral_target_price_or_rating(merged):
                return "法人看法待確認"
            if any(k in merged for k in ["AI散熱", "散熱需求", "液冷", "水冷", "需求強勁", "需求延續", "營運看旺"]):
                return "AI散熱需求強勁"
            if _has_negative_target_price_or_rating(merged) or any(k in merged for k in ["利空", "調降", "下修", "看壞", "需求疲弱", "年減", "衰退"]):
                return "市場預期轉弱"

            if "下週" in label_s or "下週" in merged:
                if any(k in merged for k in ["站回", "突破", "轉強"]):
                    return "先看站回訊號"
                if any(k in merged for k in ["跌破", "轉弱", "賣壓"]):
                    return "留意續弱風險"
                return "先看止跌訊號"

            if "技術" in label_s or any(k in merged for k in ["均線", "短均", "K線", "布林", "型態", "大量區", "價量"]):
                if "跌破" in merged:
                    return "股價跌破短均"
                if "站回" in merged:
                    return "股價站回短均"
                if "突破" in merged:
                    return "股價突破壓力"
                if any(k in merged for k in ["轉弱", "弱勢"]):
                    return "型態轉弱整理"
                return "技術尚待確認"

            if "權證" in label_s or any(k in merged for k in ["權證", "分點", "資金流"]):
                try:
                    selected_branches = [normalize_branch_name(x) for x in _get_selected_branch_flow_list()]
                except Exception:
                    selected_branches = []
                matched_branch = ""
                for b in selected_branches:
                    if b and b in normalize_branch_name(merged):
                        matched_branch = b
                        break
                if matched_branch and any(k in merged for k in ["淨流入", "資金流入", "買超", "加碼", "偏買", "承接"]):
                    return f"{matched_branch}積極偏買"
                if matched_branch and any(k in merged for k in ["淨流出", "資金流出", "賣超", "調節", "偏賣"]):
                    return f"{matched_branch}偏向調節"
                if any(k in merged for k in ["淨流入", "資金流入", "買超", "加碼"]):
                    return "權證資金流入"
                if any(k in merged for k in ["淨流出", "資金流出", "賣超", "調節"]):
                    return "權證資金流出"
                if any(k in merged for k in ["勝率", "加權報酬率", "平均持有"]):
                    return "代表分點可追蹤"
                return "權證方向待確認"

            if "法人" in label_s or any(k in merged for k in ["法人", "外資", "投信", "自營"]):
                if any(k in merged for k in ["接近中性", "幅度有限", "方向有限", "不明"]):
                    return "法人方向有限"
                if any(k in merged for k in ["買超", "偏買", "回補"]):
                    return "法人偏向買超"
                if any(k in merged for k in ["賣超", "調節", "偏賣"]):
                    return "法人偏向調節"
                return "法人消息不足"

            if any(k in label_s for k in ["業績", "新聞", "產業", "題材", "公司", "法人觀點"]):
                if "營收" in merged and "年增" in merged and "月減" in merged:
                    return "營收表現偏正向"
                if "營收" in merged and any(k in merged for k in ["年增", "月增", "成長"]):
                    return "營收表現偏正向"
                if any(k in merged for k in ["AI", "散熱", "液冷", "水冷"]):
                    return "AI散熱需求強勁"
                if any(k in merged for k in ["研發", "平台", "新應用", "新產品", "推出", "開發"]):
                    return "產品布局待追蹤"
                if any(k in merged for k in ["上修", "調升", "看旺"]):
                    return "市場預期上修"
                if "公司" in label_s:
                    return "公司動態待追蹤"
                if "產業" in label_s or "題材" in label_s:
                    return "題材熱度待觀察"
                return "新聞事件待追蹤"

            return fallback

        def _compact_status_text(status_text, max_chars=13, fallback="重點待確認", label="", body=""):
            """產生第一行上色的具體結論短句；若 AI 只給偏多/偏弱/中性，改從說明提煉。"""
            raw = _normalize_card_text(status_text)
            raw = re.sub(r"^(結論|結果|狀態)[:：]\s*", "", raw).strip("。；;，,、 ")
            if (not raw) or _is_generic_status_phrase(raw):
                raw = _derive_headline_from_body(label, body, fallback=fallback)
            if len(raw) > max_chars:
                raw = raw[:max_chars].rstrip("。；;，,、 ")
            return raw or fallback

        def _infer_status_from_text(text, fallback="重點待確認"):
            # 保留舊呼叫相容性；實際顯示時會再由 _compact_status_text 轉成具體短句。
            s = str(text or "")
            if "營收" in s and "年增" in s and "月減" in s:
                return "偏多"
            if _has_negative_target_price_or_rating(s):
                return "偏弱"
            if _has_positive_target_price_or_rating(s):
                return "偏多"
            if _has_neutral_target_price_or_rating(s):
                return "中性觀察"
            if any(k in s for k in [
                "轉強", "買超", "資金流入", "淨流入", "站回", "突破", "月增", "年增", "正向",
                "利多", "傳捷報", "捷報", "看好", "看旺", "調升", "上修", "評等調升",
                "接單", "訂單", "受惠", "成長", "需求強勁", "需求延續", "AI散熱", "液冷", "營運看旺",
            ]):
                return "偏多"
            if _has_negative_target_price_or_rating(s) or any(k in s for k in [
                "轉弱", "賣壓", "跌破", "空頭", "空頭排列", "均線空頭", "死亡交叉", "均線死亡交叉", "死叉", "資金流出", "淨流出", "賣超", "年減", "利空", "調降", "下修", "看壞", "衰退", "需求疲弱",
            ]):
                return "偏弱"
            if "月減" in s and "年增" not in s:
                return "偏弱"
            if any(k in s for k in ["中性", "觀望", "待確認", "有限", "接近中性"]):
                return "中性觀察"
            return fallback

        def _format_note_pct_value(value, digits=0, force_sign=False):
            try:
                num = _parse_percent_like_value(value, ratio_if_small=True)
                if not np.isfinite(num):
                    return ""
                sign = "+" if force_sign and num > 0 else ""
                if digits <= 0:
                    return f"{sign}{num:.0f}%"
                return f"{sign}{num:.{digits}f}%"
            except Exception:
                return ""

        def _format_branch_perf_note_suffix(perf_row) -> str:
            """將 Google Sheet 勝率統計壓成圖卡可讀的一句績效補充。"""
            if perf_row is None:
                return ""
            win_text = _format_note_pct_value(perf_row.get("win_rate", np.nan), digits=0, force_sign=False)
            weighted_text = _format_note_pct_value(perf_row.get("weighted_return", np.nan), digits=0, force_sign=True)
            holding_text = _format_avg_holding_days(perf_row.get("avg_holding_days", np.nan))
            parts = []
            if win_text:
                parts.append(f"勝率{win_text}")
            if holding_text and holding_text != "-":
                parts.append(f"持有{holding_text}")
            if weighted_text:
                parts.append(f"加權{weighted_text}")
            return "、".join(parts)

        def _find_branch_perf_for_note_text(text_value):
            """若文字點名精選分點或勝率統計表中的分點，回傳該分點的歷史績效。"""
            target_text = normalize_branch_name(str(text_value or ""))
            if not target_text:
                return None

            candidate_rows = []
            try:
                # 優先使用本週具代表性的 TOP5 分點績效，避免同名或小額分點誤配。
                for m in _build_weekly_branch_perf_matches(ctx):
                    branch_norm = normalize_branch_name(m.get("branch_norm", "") or m.get("branch", ""))
                    if branch_norm:
                        candidate_rows.append((branch_norm, m))
            except Exception:
                pass

            try:
                perf_df = read_gsheet_branch_perf_df(force_refresh=False)
                if perf_df is not None and not perf_df.empty:
                    for _, r in perf_df.iterrows():
                        branch_norm = normalize_branch_name(r.get("branch", "") or r.get("branch_display", ""))
                        if branch_norm:
                            candidate_rows.append((branch_norm, r.to_dict()))
            except Exception:
                pass

            # 分點名稱長的優先，避免「新光」這類短名稱先誤吃掉其他內容。
            seen = set()
            for branch_norm, row in sorted(candidate_rows, key=lambda x: len(x[0]), reverse=True):
                if branch_norm in seen:
                    continue
                seen.add(branch_norm)
                if branch_norm and branch_norm in target_text:
                    return row
            return None

        def _inject_branch_perf_into_warrant_body(label, status, body, original_text):
            """權證面若點名精選分點 / 勝率表分點，自動補上勝率、持有天數、加權報酬率。"""
            merged = "｜".join([str(label or ""), str(status or ""), str(body or ""), str(original_text or "")])
            if "權證" not in str(label or "") and not any(k in merged for k in ["分點", "元大", "新光", "富邦", "永豐", "華南", "勝率", "加權", "持有"]):
                return body
            if all(k in str(body or "") for k in ["勝率", "加權", "持有"]):
                return body

            perf_row = _find_branch_perf_for_note_text(merged)
            suffix = _format_branch_perf_note_suffix(perf_row)
            if not suffix:
                return body

            base = _compact_card_sentence(body, 58).rstrip("。")
            if base:
                return f"{base}；績效：{suffix}。"
            return f"績效：{suffix}。"

        def _format_key_status_sections(items):
            rows = []
            for p in items or []:
                s = str(p or "").strip()
                if not s:
                    continue
                f = _parse_status_fields(s)
                label = f.get("面向") or _infer_face_label_from_text(s, fallback="重點面")
                label = re.sub(r"[：:。；;，,、｜\s]+", "", str(label or "重點面"))[:6] or "重點面"

                body = f.get("說明") or ""
                if not body:
                    if f.get("依據") and f.get("觀察"):
                        body = f"{f.get('依據')}；觀察：{f.get('觀察')}"
                    elif f.get("依據"):
                        body = f.get("依據")
                    elif f.get("重點") and f.get("觀察"):
                        body = f"{f.get('重點')}；觀察：{f.get('觀察')}"
                    elif f.get("重點"):
                        body = f.get("重點")
                    elif f.get("條件") or f.get("追蹤"):
                        body = "；".join([x for x in [f.get("條件"), f.get("追蹤")] if x])
                    else:
                        body = _strip_status_labels(s)
                body = re.sub(r"(?:^|[。；;])\s*標題[:：]\s*", "", body).strip()
                body = _compact_card_sentence(body, 96)

                raw_status = f.get("結果") or f.get("狀態") or f.get("結論") or _infer_status_from_text(s, fallback="重點待確認")
                status = _compact_status_text(
                    raw_status,
                    max_chars=16,
                    fallback="重點待確認" if label != "下週觀察" else "先看止跌訊號",
                    label=label,
                    body=body,
                )
                if label == "技術面":
                    # 技術面優先參考上方 K 線圖已計算出的均線 / 價量訊號，
                    # 避免下方文字出現「重點待確認」或與「均線多頭排列」等圖上訊號矛盾。
                    tech_card = _build_technical_card_summary(ctx)
                    if tech_card:
                        status = str(tech_card.get("headline", status) or status)
                        body = str(tech_card.get("detail", body) or body)
                body = _inject_branch_perf_into_warrant_body(label, status, body, s)
                rows.append((label, status, body, 3))
                if len(rows) >= 3:
                    break
            tech_card = _build_technical_card_summary(ctx)
            if tech_card and not any(str(r[0]) == "技術面" for r in rows):
                tech_row = (
                    "技術面",
                    str(tech_card.get("headline", "技術訊號待確認") or "技術訊號待確認"),
                    str(tech_card.get("detail", "目前技術訊號仍需確認。") or "目前技術訊號仍需確認。"),
                    3,
                )
                if len(rows) < 3:
                    rows.insert(0, tech_row)
                else:
                    replace_idx = 0
                    for i, r in enumerate(rows):
                        if str(r[0]) in ("新聞面", "重點面"):
                            replace_idx = i
                            break
                    rows[replace_idx] = tech_row
            if not rows:
                rows.append(("重點面", "重點待確認", "本週暫無足夠明確資料可整理成重點。", 2))
            return rows[:3]

        def _has_substantive_news_analyst_view(text: str) -> bool:
            s = _normalize_card_text(text)
            if _has_positive_target_price_or_rating(s) or _has_negative_target_price_or_rating(s) or _has_neutral_target_price_or_rating(s):
                return True
            return bool(re.search(r"券商看好|券商調升|券商調降|評等調升|評等調降|升評|降評|維持買進|重申買進|買進評等|賣出評等|EPS(?:預估)?(?:上修|下修)|法人(?:買超|賣超|回補|調節)", s))

        def _is_useless_news_analyst_row(label: str, status: str, body: str, raw_text: str) -> bool:
            merged = _normalize_card_text("｜".join([str(label or ""), str(status or ""), str(body or ""), str(raw_text or "")]))
            if "法人" not in str(label or "") and "法人" not in merged:
                return False
            if _has_substantive_news_analyst_view(merged):
                return False
            return any(k in merged for k in ["法人尚未表態", "法人消息不足", "法人看法待確認", "尚未表態", "未表態", "待確認", "消息不足"])

        def _format_news_status_sections(items):
            rows = []
            for p in (items or [])[:NEWS_DISPLAY_MAX_POINTS]:
                s = str(p or "").strip()
                if not s:
                    continue
                f = _parse_status_fields(s)
                label = f.get("面向") or f.get("分類") or _infer_face_label_from_text(s, fallback="新聞面")
                label = re.sub(r"[：:。；;，,、｜\s]+", "", str(label or "新聞面"))[:6] or "新聞面"

                body_parts = []
                if f.get("說明"):
                    body_parts.append(f.get("說明"))
                else:
                    if f.get("重點"):
                        body_parts.append(f.get("重點"))
                    elif f.get("依據"):
                        body_parts.append(f.get("依據"))
                    if f.get("觀察"):
                        body_parts.append("觀察：" + f.get("觀察"))
                    elif f.get("影響"):
                        body_parts.append("影響：" + f.get("影響"))
                body = "；".join([x for x in body_parts if x]) or _strip_status_labels(s)
                body = re.sub(r"(?:^|[。；;])\s*標題[:：]\s*", "", body).strip()
                body = _compact_card_sentence(body, 116)

                raw_status = f.get("結果") or f.get("狀態") or f.get("結論") or _infer_status_from_text(s, fallback="新聞事件待追蹤")
                status = _compact_status_text(
                    raw_status,
                    max_chars=16,
                    fallback="新聞事件待追蹤",
                    label=label,
                    body=body,
                )
                if _is_useless_news_analyst_row(label, status, body, s):
                    continue
                rows.append((label, status, body, 3))
            if not rows:
                rows.append(("新聞面", "新聞事件待追蹤", "本週未篩選到足夠明確的公司新聞，右側暫不硬湊摘要。", 3))
            return rows[:2]

        def _measure_text_width_axes(ax, fig, text, fontsize=33, fontweight="normal") -> float:
            """量測文字在目前 notes 軸中的寬度，讓「面向：結果」用冒號自然銜接，不再靠固定空格對齊。"""
            try:
                renderer = get_cached_renderer()
                ax_bbox = ax.get_window_extent(renderer=renderer)
                if ax_bbox.width <= 0:
                    return 0.0
                tmp = ax.text(
                    0, 0, str(text or ""),
                    transform=ax.transAxes,
                    fontsize=fontsize,
                    fontweight=fontweight,
                    ha="left",
                    va="top",
                    alpha=0,
                )
                bbox = tmp.get_window_extent(renderer=renderer)
                tmp.remove()
                return max(0.0, bbox.width / ax_bbox.width)
            except Exception:
                return 0.0

        def draw_status_note_items(
            sections,
            x_left,
            x_right,
            y_start,
            body_fontsize=32,
            label_fontsize=35,
            status_fontsize=38,
            header_gap=0.060,
            line_height=0.052,
            section_gap=0.038,
            status_offset=0.140,
            y_min=0.060,
            body_width_boost=1.0,
            body_linespacing=1.12,
        ):
            y = y_start
            max_width_axes = max(0.05, x_right - x_left)
            for idx, (label, status, body, max_lines) in enumerate(sections):
                if y <= y_min:
                    break
                label = str(label or "重點面").strip()
                status = str(status or "中性觀察").strip()
                body_lines = wrap_text_by_pixel(
                    ax_notes,
                    fig,
                    body,
                    max_width_axes=max_width_axes,
                    fontsize=body_fontsize,
                    fontweight="normal",
                    max_lines=max_lines,
                    first_prefix="",
                    next_prefix="",
                    width_boost=body_width_boost,
                )
                if not body_lines:
                    continue

                # 改成「技術面：跌破轉弱趨勢明確」這種冒號式排列。
                # 面向與冒號維持深藍，只有結果短句上色；底下說明維持原本文字色。
                label_text = f"{label} : "
                label_width = _measure_text_width_axes(
                    ax_notes,
                    fig,
                    label_text,
                    fontsize=label_fontsize,
                    fontweight="bold",
                )
                measured_status_x = x_left + label_width + 0.001
                compact_offset = 0.110 if len(label) <= 3 else 0.148
                dynamic_status_x = min(measured_status_x, x_left + compact_offset)
                # 若量測失敗，才退回舊的 offset，避免圖片中斷。
                if dynamic_status_x <= x_left + 0.010:
                    dynamic_status_x = x_left + compact_offset

                ax_notes.text(
                    x_left,
                    y,
                    label_text,
                    transform=ax_notes.transAxes,
                    color=GOLD,
                    fontsize=label_fontsize,
                    fontweight="bold",
                    ha="left",
                    va="top",
                    clip_on=True,
                    zorder=6,
                )
                ax_notes.text(
                    dynamic_status_x,
                    y,
                    status,
                    transform=ax_notes.transAxes,
                    color=get_report_status_color(status),
                    fontsize=status_fontsize,
                    fontweight="bold",
                    ha="left",
                    va="top",
                    clip_on=True,
                    zorder=6,
                )
                y -= header_gap
                ax_notes.text(
                    x_left,
                    y,
                    "\n".join(body_lines),
                    transform=ax_notes.transAxes,
                    color=TEXT,
                    fontsize=body_fontsize,
                    ha="left",
                    va="top",
                    linespacing=body_linespacing,
                    clip_on=True,
                    zorder=6,
                )
                y -= line_height * len(body_lines) + section_gap
                if idx < len(sections) - 1 and y > y_min + 0.012:
                    ax_notes.plot(
                        [x_left, x_right],
                        [y + 0.010, y + 0.010],
                        transform=ax_notes.transAxes,
                        color=GRID,
                        linewidth=0.9,
                        alpha=0.50,
                        zorder=5,
                    )

        # 下方兩張文字卡：恢復原本大字級閱讀感，只把第一行「結果」做紅 / 綠 / 灰標示；詳細說明維持原本 TEXT 顏色。
        draw_status_note_items(
            _format_key_status_sections(key_points[:3]),
            0.04,
            0.500,
            0.775,
            body_fontsize=31,
            label_fontsize=35,
            status_fontsize=36,
            header_gap=0.062,
            line_height=0.050,
            section_gap=0.034,
            status_offset=0.125,
            body_width_boost=1.18,
            body_linespacing=1.22
        )

        draw_status_note_items(
            _format_news_status_sections(news_points[:NEWS_DISPLAY_MAX_POINTS]),
            0.54,
            0.995,
            0.775,
            body_fontsize=31,
            label_fontsize=35,
            status_fontsize=36,
            header_gap=0.060,
            line_height=0.048,
            section_gap=0.034,
            status_offset=0.125,
            body_width_boost=1.18,
            body_linespacing=1.16
        )

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

def _prepare_report_news_items(stock_code: str, stock_name: str) -> List[dict]:
    """準備週報新聞素材；可與權證 API 流程平行執行。"""
    with report_stage_timer(f"{stock_code}｜新聞資料準備"):
        if is_compact_report_mode():
            print("📄 精簡週報模式：略過本週重點、多來源新聞抓取與 Gemini 新聞統整")
            return []

        cached_news_points = _load_gsheet_news_points_cache_for_display(
            stock_code,
            stock_name,
            allow_stale=False,
        )
        if cached_news_points:
            print(
                f"📦 今日新聞快取已存在，略過多來源新聞抓取與 Gemini 新聞統整："
                f"{stock_code}｜{len(cached_news_points)} 點"
            )
            return []

        return fetch_multi_source_news_articles(
            stock_code,
            stock_name,
            max_items=NEWS_GOOGLE_MAX_ITEMS,
        )


def generate_warrant_report(stock_code: str) -> io.BytesIO:
    report_total_start = time.perf_counter()
    stock_code = str(stock_code).strip()

    try:
        if REPORT_LIVE_ONLY:
            print("🔴 本次啟用純 Live 週報模式：圖片內容不使用 Google Sheet / 本機快取資料")

        with report_stage_timer(f"{stock_code}｜股票名稱查詢"):
            stock_name = get_tw_stock_name(stock_code)

        with report_stage_timer(f"{stock_code}｜股價資料抓取"):
            stock_df, market, yf_code = fetch_stock_data_yf(stock_code, period="180d")

        if stock_df is None or stock_df.empty:
            print(f"❌ 股價資料不足：{stock_code}")
            return None

        with report_stage_timer(f"{stock_code}｜技術指標計算"):
            stock_df = calculate_indicators(stock_df)
            stock_df["Close_prev"] = stock_df["Close"].shift(1)

        # 三大法人資料：對齊股價日期，讓週報可顯示三大法人買賣超。
        # 另外保留原始 FinMind 日期到 stock_df.attrs["institutional_df"]，讓三大法人圖可先顯示
        # 股價尚未更新、但 FinMind 已更新的最新法人買賣超。
        with report_stage_timer(f"{stock_code}｜三大法人資料"):
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

        # 權證 API 與新聞搜尋彼此沒有資料相依，平行準備可直接縮短等待時間。
        with ThreadPoolExecutor(max_workers=2) as report_data_executor:
            warrant_future = report_data_executor.submit(
                fetch_warrant_events_full_market,
                stock_code,
                stock_name,
                start_date,
                end_date,
            )
            news_future = report_data_executor.submit(
                _prepare_report_news_items,
                stock_code,
                stock_name,
            )

            with report_stage_timer(f"{stock_code}｜權證完整流程"):
                warrant_events = warrant_future.result()
            news_items = news_future.result()

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
            print_debug_branch_warrant_flow(
                stock_code,
                stock_name,
                stock_df,
                warrant_events,
                start_date=start_date,
                end_date=end_date,
            )

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

        with report_stage_timer(f"{stock_code}｜週報內容生成與建圖總流程"):
            fig = plot_weekly_report(
                stock_code,
                stock_name,
                stock_df,
                warrant_events,
                news_items,
            )

        buf = io.BytesIO()
        with report_stage_timer(f"{stock_code}｜Matplotlib PNG 輸出｜dpi={REPORT_OUTPUT_DPI}"):
            try:
                fig.savefig(
                    buf,
                    format="png",
                    dpi=REPORT_OUTPUT_DPI,
                    bbox_inches="tight",
                    pad_inches=0.18,
                    facecolor=fig.get_facecolor(),
                    pil_kwargs={
                        "compress_level": max(
                            0,
                            min(9, int(REPORT_INTERMEDIATE_PNG_COMPRESS_LEVEL)),
                        ),
                        "optimize": False,
                    },
                )
            except TypeError as e:
                # 相容較舊 Matplotlib：若不支援 pil_kwargs，保留相同 DPI 與版面設定重新輸出。
                print(f"⚠️ Matplotlib 不支援 pil_kwargs，改用相容輸出：{e}")
                buf.seek(0)
                buf.truncate(0)
                fig.savefig(
                    buf,
                    format="png",
                    dpi=REPORT_OUTPUT_DPI,
                    bbox_inches="tight",
                    pad_inches=0.18,
                    facecolor=fig.get_facecolor(),
                )
            finally:
                plt.close(fig)

        # 模擬「截圖後輸出」：以較合理的中間解析度產圖，再等比例縮小並重新壓縮，降低檔案大小。
        with report_stage_timer(f"{stock_code}｜Pillow 縮圖與最終壓縮"):
            buf = screenshot_like_output_buffer(buf)

        return buf

    except Exception as e:
        import traceback
        print(f"❌ 產生權證週報錯誤：{e}")
        traceback.print_exc()
        return None
    finally:
        if REPORT_TIMING_ENABLE:
            print(f"⏱️ {stock_code or 'UNKNOWN'}｜週報總時間：{time.perf_counter() - report_total_start:.2f} 秒")


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
