import io
import json
import html
import base64
import hashlib
import os
import re
import time
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    wait,
    FIRST_COMPLETED,
    TimeoutError as FuturesTimeoutError,
)
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

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


# ============================================================
# 基本設定
# ============================================================

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
}

OPENAPI_WARRANT_HEADERS = {
    "User-Agent": HDR["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

TWSE_WARRANT_DAILY_OPENAPI_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap42_L"
TPEX_WARRANT_DAILY_OPENAPI_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap42_O"

# 週報參數
WEEK_TRADING_DAYS = int(os.getenv("WARRANT_WEEK_TRADING_DAYS", "5"))
CHART_LOOKBACK = int(os.getenv("WARRANT_CHART_LOOKBACK", "70"))

# GitHub Actions 的 0 / 1 主控：
# - 0：優先使用 Google Sheet 當日快取與完整權證快照；若快取不存在或不完整，才回退 Live 抓取。
# - 1：跳過快取，強制重新抓 Live 權證資料並重新產生新聞 / 本週重點。
# 為維持既有 workflow 相容，沿用 WARRANT_LLM_CACHE_FORCE_REFRESH 作為主控值。
ACTION_FORCE_REFRESH = os.getenv(
    "WARRANT_LLM_CACHE_FORCE_REFRESH",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
ACTION_REFRESH_CONTROLS_REPORT_DATA = os.getenv(
    "WARRANT_ACTION_REFRESH_CONTROLS_REPORT_DATA",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# Action 主控啟用時：
# - 0：快取優先；快取存在就直接使用，快取缺少或不完整時自動回退 Live，並在成功後建立新快照。
# - 1：強制重新抓 Live，忽略既有快取並覆寫快照。
ACTION_CACHE_PREFERRED_MODE = bool(
    ACTION_REFRESH_CONTROLS_REPORT_DATA
    and not ACTION_FORCE_REFRESH
)
# 若真的需要「只允許快取、禁止回退 Live」，可另外明確開啟；預設關閉。
ACTION_CACHE_ONLY_MODE = bool(
    ACTION_CACHE_PREFERRED_MODE
    and os.getenv(
        "WARRANT_ACTION_CACHE_ONLY_MODE",
        "0",
    ).strip().lower() in ("1", "true", "yes", "on")
)

LIVE_FETCH_ENABLE = os.getenv("WARRANT_LIVE_FETCH_ENABLE", "1").strip().lower() not in ("0", "false", "no")
# 純 Live 週報模式：圖片內容不讀取、不合併、不寫入 Google Sheet 或本機快取。
# 當 ACTION 主控啟用時，Action=1 才進純 Live；Action=0 則優先讀 Google Sheet 快取。
if ACTION_REFRESH_CONTROLS_REPORT_DATA:
    REPORT_LIVE_ONLY = bool(ACTION_FORCE_REFRESH)
else:
    REPORT_LIVE_ONLY = os.getenv("WARRANT_REPORT_LIVE_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
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

# FinMind 全市場權證分點包含完整買賣雙邊，直接把所有分點加總時淨額會接近 0。
# 上方「權證資金流」與 TOP5 先逐檔辨識發行商，只排除該權證的發行／造市總公司列；
# TOP5 再沿用原本的總公司／自營型分點過濾，正常地方分點仍保留。
# 下方精選分點固定使用完整原始資料，不受上述排除影響。
FINMIND_ISSUER_FLOW_ENABLE = os.getenv(
    "FINMIND_ISSUER_FLOW_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
FINMIND_ISSUER_FLOW_DEBUG_ENABLE = os.getenv(
    "FINMIND_ISSUER_FLOW_DEBUG_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
FINMIND_ISSUER_FLOW_DEBUG_MAX_ROWS = max(
    1,
    int(os.getenv("FINMIND_ISSUER_FLOW_DEBUG_MAX_ROWS", "30")),
)
FINMIND_ISSUER_FLOW_EXCLUDE_UNRESOLVED_ENABLE = os.getenv(
    "FINMIND_ISSUER_FLOW_EXCLUDE_UNRESOLVED_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")

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
BRANCH_PERF_DISK_CACHE_ENABLE = os.getenv(
    "WARRANT_BRANCH_PERF_DISK_CACHE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
_BRANCH_PERF_DISK_CACHE_ROOT = os.path.join(
    os.getenv("FINMIND_CACHE_DIR", "finmind_cache").strip() or "finmind_cache",
    "reference",
)
_BRANCH_PERF_DISK_CACHE_PATH = os.path.join(_BRANCH_PERF_DISK_CACHE_ROOT, "branch_performance.parquet")
_BRANCH_PERF_DISK_META_PATH = os.path.join(_BRANCH_PERF_DISK_CACHE_ROOT, "branch_performance.meta.json")
_BRANCH_PERF_CACHE_DF = None
_BRANCH_PERF_CACHE_LOCK = threading.Lock()


# 權證快取設定：Action=0 時優先讀 Google Sheet 完整快照；Action=1 時才強制 Live。
# 若停用 ACTION_REFRESH_CONTROLS_REPORT_DATA，才回到原本各環境變數獨立控制的方式。
if ACTION_REFRESH_CONTROLS_REPORT_DATA:
    WARRANT_ALWAYS_REFRESH_WARRANT_FLOW = bool(ACTION_FORCE_REFRESH)
    WARRANT_CACHE_FORCE_REFRESH = bool(ACTION_FORCE_REFRESH)
else:
    WARRANT_ALWAYS_REFRESH_WARRANT_FLOW = os.getenv(
        "WARRANT_ALWAYS_REFRESH_WARRANT_FLOW",
        "1",
    ).strip().lower() not in ("0", "false", "no", "off")
    WARRANT_CACHE_FORCE_REFRESH = WARRANT_ALWAYS_REFRESH_WARRANT_FLOW or os.getenv(
        "WARRANT_CACHE_FORCE_REFRESH",
        os.getenv("WARRANT_LOCAL_CACHE_FORCE_REFRESH", "0"),
    ).strip().lower() in ("1", "true", "yes", "on")
GSHEET_WARRANT_CACHE_ENABLE = os.getenv("WARRANT_GSHEET_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GSHEET_WARRANT_STATUS_SHEET = os.getenv("WARRANT_GSHEET_STATUS_SHEET", "快取_分點歷史_狀態").strip() or "快取_分點歷史_狀態"
# 權證完整快照預設使用獨立試算表，避免主試算表超過 Google Sheets 1,000 萬儲存格上限。
# 可直接設定 WARRANT_CACHE_GOOGLE_SHEET_ID；未設定時會依名稱開啟，找不到則自動建立。
WARRANT_CACHE_USE_SEPARATE_SHEET = os.getenv(
    "WARRANT_CACHE_USE_SEPARATE_SHEET",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
WARRANT_CACHE_GOOGLE_SHEET_ID = os.getenv(
    "WARRANT_CACHE_GOOGLE_SHEET_ID",
    "",
).strip()
WARRANT_CACHE_GOOGLE_SHEET_NAME = os.getenv(
    "WARRANT_CACHE_GOOGLE_SHEET_NAME",
    "權證週報完整快取",
).strip() or "權證週報完整快取"
WARRANT_CACHE_GOOGLE_SHEET_AUTO_CREATE = os.getenv(
    "WARRANT_CACHE_GOOGLE_SHEET_AUTO_CREATE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# 過渡期間若獨立試算表還沒有資料，可讀取舊主試算表既有快照；新資料一律寫入獨立試算表。
WARRANT_CACHE_LEGACY_MAIN_FALLBACK_ENABLE = os.getenv(
    "WARRANT_CACHE_LEGACY_MAIN_FALLBACK_ENABLE",
    "0",
).strip().lower() not in ("0", "false", "no", "off")
# 每個股票／日期區間使用獨立工作表，避免每次讀取與重寫整張巨型「快取_分點歷史」。

# Gemini / LLM 結果快取：同一份 prompt 重跑時直接重用，不再重打 API。
# 注意：純 Live 模式只禁止交易資料快取；Gemini 文字快取仍應依 ACTION 參數運作。
# 因此當 WARRANT_LLM_CACHE_FORCE_REFRESH=0 時，同股票同任務同一天會優先使用當日快取；
# 設為 1 時才會跳過快取並重新呼叫 Gemini。
LLM_CACHE_ENABLE = os.getenv("WARRANT_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
LLM_CACHE_DIR = os.getenv("WARRANT_LLM_CACHE_DIR", "llm_cache").strip() or "llm_cache"
# Gemini 結果寫回 Google Sheet：同股票同任務當天跑過一次，當天再跑直接讀快取，不再呼叫 Gemini。
GSHEET_LLM_CACHE_ENABLE = os.getenv("WARRANT_GSHEET_LLM_CACHE_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GSHEET_LLM_CACHE_READ_ENABLE = os.getenv(
    "WARRANT_GSHEET_LLM_CACHE_READ_ENABLE",
    "1" if GSHEET_LLM_CACHE_ENABLE else "0",
).strip().lower() not in ("0", "false", "no", "off")
GSHEET_LLM_CACHE_WRITE_ENABLE = os.getenv(
    "WARRANT_GSHEET_LLM_CACHE_WRITE_ENABLE",
    "1" if GSHEET_LLM_CACHE_ENABLE else "0",
).strip().lower() not in ("0", "false", "no", "off")
LLM_DAILY_TASK_CACHE_ENABLE = os.getenv(
    "WARRANT_LLM_DAILY_TASK_CACHE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
GSHEET_LLM_CACHE_SHEET = os.getenv("WARRANT_GSHEET_LLM_CACHE_SHEET", "快取_Gemini結果").strip() or "快取_Gemini結果"
LLM_CACHE_FORCE_REFRESH = bool(ACTION_FORCE_REFRESH)


_THREAD_LOCAL = threading.local()

# 同一場程式執行中共用 Google Sheet 授權 client 與 spreadsheet handle，
# 避免股票名稱、勝率統計、Gemini 快取等功能反覆重新授權與開啟同一份試算表。
_GSPREAD_CLIENT_CACHE = None
_GSHEET_HANDLE_CACHE = None
_WARRANT_CACHE_GSHEET_HANDLE_CACHE = None
_WARRANT_CACHE_GSHEET_DISABLED_FOR_RUN = False
_GSHEET_CONNECTION_LOCK = threading.RLock()
_DAILY_LLM_CACHE_MEM = {}
_DAILY_LLM_CACHE_LOCK = threading.RLock()

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


REPORT_TONE_VALUES = {"positive", "negative", "neutral", "mixed", "watch"}
REPORT_TONE_ALIASES = {
    "positive": "positive", "bull": "positive", "bullish": "positive", "正向": "positive", "利多": "positive", "偏多": "positive",
    "negative": "negative", "bear": "negative", "bearish": "negative", "負向": "negative", "利空": "negative", "偏空": "negative", "偏弱": "negative",
    "neutral": "neutral", "中性": "neutral", "無方向": "neutral", "方向不明": "neutral",
    "mixed": "mixed", "混合": "mixed", "正負並存": "mixed", "多空交錯": "mixed",
    "watch": "watch", "觀察": "watch", "待觀察": "watch", "追蹤": "watch",
}


def normalize_report_tone(value: str) -> str:
    raw_original = str(value or "").strip().strip("。；;，,、 ")
    raw = raw_original.lower()
    if not raw:
        return ""
    if raw in REPORT_TONE_VALUES:
        return raw
    return REPORT_TONE_ALIASES.get(raw, REPORT_TONE_ALIASES.get(raw_original, ""))


def get_report_tone_color(tone: str, status_text: str = "") -> str:
    """優先依結構化 tone 決定顏色；舊資料沒有 tone 時才退回舊關鍵字規則。"""
    normalized = normalize_report_tone(tone)
    if normalized == "positive":
        return STATUS_BULL_COLOR
    if normalized == "negative":
        return STATUS_BEAR_COLOR
    if normalized in {"neutral", "mixed", "watch"}:
        return STATUS_NEUTRAL_COLOR
    return get_report_status_color(status_text)


def _parse_report_point_fields(point) -> dict:
    """解析「面向／分類、結果、說明、方向」欄位，供 tone 與舊流程共用。"""
    if isinstance(point, dict):
        label = point.get("label") or point.get("面向") or point.get("分類") or ""
        status = point.get("status") or point.get("結果") or point.get("結論") or ""
        detail = point.get("detail") or point.get("說明") or point.get("重點") or point.get("依據") or ""
        tone = normalize_report_tone(point.get("tone") or point.get("方向") or "")
        return {"label": str(label or "").strip(), "status": str(status or "").strip(), "detail": str(detail or "").strip(), "tone": tone}

    s = _normalize_news_text(str(point or "")).replace("|", "｜").strip()
    if not s:
        return {"label": "", "status": "", "detail": "", "tone": ""}
    is_watch = bool(re.match(r"^下週觀察[:：]", s))
    work = re.sub(r"^下週觀察[:：]\s*", "", s)
    aliases = {
        "面向": "label", "分類": "label", "結果": "status", "狀態": "status", "結論": "status",
        "說明": "detail", "重點": "detail", "依據": "detail", "方向": "tone", "tone": "tone",
    }
    values = {}
    parts = [p.strip() for p in re.split(r"｜+", work) if p.strip()]
    for idx, part in enumerate(parts):
        m = re.match(r"^([^:：]{1,10})[:：]\s*(.*)$", part, flags=re.DOTALL)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            normalized_key = aliases.get(key) or aliases.get(key.lower())
            if normalized_key and value:
                values[normalized_key] = value
                continue
        if idx == 0 and "label" not in values and len(part) <= 10:
            values["label"] = part
        elif idx == 1 and "status" not in values:
            values["status"] = part
        elif "detail" not in values:
            values["detail"] = part
    if is_watch:
        values["label"] = "下週觀察"
        values["tone"] = "watch"
    return {
        "label": str(values.get("label", "") or "").strip(),
        "status": str(values.get("status", "") or "").strip(),
        "detail": str(values.get("detail", "") or "").strip(),
        "tone": normalize_report_tone(values.get("tone", "")),
    }


def _point_item_to_canonical_text(item) -> str:
    """將 Gemini 結構化物件轉回既有字串格式，保留舊驗證、快取與排版流程。"""
    if not isinstance(item, dict):
        return str(item or "").strip()
    fields = _parse_report_point_fields(item)
    label = fields.get("label") or "新聞面"
    status = fields.get("status") or "事件待追蹤"
    detail = fields.get("detail") or "後續觀察實際營運與資金變化。"
    tone = normalize_report_tone(fields.get("tone")) or "neutral"
    try:
        confidence = float(item.get("confidence", 1.0))
    except Exception:
        confidence = 1.0
    if tone in {"positive", "negative"} and confidence < 0.65:
        tone = "neutral"
    prefix = "下週觀察：" if label == "下週觀察" or tone == "watch" else ""
    if prefix:
        label = "下週觀察"
        tone = "watch"
    return f"{prefix}面向：{label}｜結果：{status}｜說明：{detail}｜方向：{tone}"


def _extract_report_tone_from_point(point) -> str:
    return normalize_report_tone(_parse_report_point_fields(point).get("tone", ""))


def _replace_report_point_tone(point: str, tone: str) -> str:
    """只替換 tone 欄位，不改面向、結果與說明。"""
    s = _normalize_news_text(str(point or "")).replace("|", "｜").strip()
    normalized = normalize_report_tone(tone) or "neutral"
    if not s:
        return s
    s = re.sub(r"｜(?:方向|tone)[:：]\s*[^｜]+", "", s, flags=re.I)
    if re.match(r"^下週觀察[:：]", s):
        normalized = "watch"
    return s.rstrip("｜ ") + f"｜方向：{normalized}"


def _strip_report_tone_metadata(point: str) -> str:
    s = _normalize_news_text(str(point or "")).replace("|", "｜").strip()
    return re.sub(r"｜(?:方向|tone)[:：]\s*[^｜。]+(?=。?$)", "", s, flags=re.I).strip()


def _fallback_tone_from_status(status_text: str) -> str:
    color = get_report_status_color(status_text)
    if color == STATUS_BULL_COLOR:
        return "positive"
    if color == STATUS_BEAR_COLOR:
        return "negative"
    return "neutral"

# 中央浮水印設定：圖片偏長，因此上下各放一個淡浮水印
CENTER_WATERMARK_TEXT = "股市艾斯\n台股DC討論群"
CENTER_WATERMARK_ALPHA = 0.06
CENTER_WATERMARK_FONT_SIZE = 200
CENTER_WATERMARK_ROTATION = 18

# Supertrend 目前圖表沒有繪製，預設不計算；若未來要畫再用環境變數打開。
ENABLE_SUPERTREND = os.getenv("WARRANT_ENABLE_SUPERTREND", "0").strip().lower() in ("1", "true", "yes", "on")

# 字型：優先使用 Workflow 自動下載並快取的 Noto Sans CJK TC 字型，避免每次 apt 安裝。
REPORT_FONT_DIR = os.getenv("WARRANT_REPORT_FONT_DIR", ".cache/report-fonts").strip() or ".cache/report-fonts"
_REPORT_FONT_FILES = [
    os.path.join(REPORT_FONT_DIR, "NotoSansCJKtc-Regular.otf"),
    os.path.join(REPORT_FONT_DIR, "NotoSansCJKtc-Bold.otf"),
]
_registered_report_fonts = []
for _font_path in _REPORT_FONT_FILES:
    if not os.path.isfile(_font_path):
        continue
    try:
        fm.fontManager.addfont(_font_path)
        _font_name = fm.FontProperties(fname=_font_path).get_name()
        if _font_name:
            _registered_report_fonts.append(_font_name)
        print(f"✅ 已註冊報表字型：{os.path.basename(_font_path)}｜family={_font_name}")
    except Exception as exc:
        print(f"⚠️ 報表字型註冊失敗：{_font_path}｜{exc}")

available_fonts = {f.name for f in fm.fontManager.ttflist}
font_candidates = _registered_report_fonts + [
    "Noto Sans CJK TC",
    "Noto Sans CJK JP",
    "Noto Sans TC",
    "Microsoft JhengHei",
    "SimHei",
]
for font_name in font_candidates:
    if font_name and font_name in available_fonts:
        plt.rcParams["font.family"] = font_name
        print(f"✅ Matplotlib 中文字型：{font_name}")
        break
else:
    plt.rcParams["font.family"] = "DejaVu Sans"
    print("⚠️ 找不到中文字型，暫時使用 DejaVu Sans")
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


_WARRANT_EVENT_NUMERIC_COLUMNS = {
    "buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares",
}
_WARRANT_EVENT_STRING_COLUMNS = {
    "branch", "broker_code", "warrant_code", "warrant_name",
    "underlying_code", "underlying_name", "side",
}


def _fill_warrant_event_missing_values(df: pd.DataFrame) -> pd.DataFrame:
    """只填補權證事件中需要的欄位，避免整張表 ``fillna("")`` 將數值欄升成 object。"""
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    for col in _WARRANT_EVENT_NUMERIC_COLUMNS.intersection(out.columns):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    for col in _WARRANT_EVENT_STRING_COLUMNS.intersection(out.columns):
        out[col] = out[col].fillna("").astype(str)
    return out


def _concat_warrant_event_frames(frames) -> pd.DataFrame:
    """合併權證事件時保留數值 dtype，只對字串欄補空字串。"""
    valid_frames = [frame for frame in (frames or []) if frame is not None and not frame.empty]
    if not valid_frames:
        return pd.DataFrame()
    return _fill_warrant_event_missing_values(
        pd.concat(valid_frames, ignore_index=True, sort=False)
    )


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
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y/%m/%d")


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


def _daily_task_llm_cache_path(task: str, stock_code: str, cache_date: str | None = None) -> str:
    key = _gsheet_llm_cache_key(task, stock_code, cache_date=cache_date)
    safe_key = re.sub(r"[^A-Za-z0-9_.一-鿿-]", "_", key)
    return os.path.join(LLM_CACHE_DIR, "daily", f"{safe_key}.txt")


def load_daily_task_llm_cache(task: str, stock_code: str) -> str:
    """同股票、同任務、同模型、同一台北日期的本機快取。

    與 prompt hash 快取不同，這份快取可直接取代原本每次都要連線 Google Sheet
    才能判斷「今天是否已產生摘要」的慢速路徑。
    """
    if not LLM_CACHE_ENABLE or not LLM_DAILY_TASK_CACHE_ENABLE or LLM_CACHE_FORCE_REFRESH:
        return ""
    if not task or not stock_code:
        return ""
    key = _gsheet_llm_cache_key(task, stock_code)
    with _DAILY_LLM_CACHE_LOCK:
        if key in _DAILY_LLM_CACHE_MEM:
            return str(_DAILY_LLM_CACHE_MEM.get(key, "") or "")

    path = _daily_task_llm_cache_path(task, stock_code)
    text = ""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
    except Exception as exc:
        print(f"⚠️ Gemini 每日任務本機快取讀取失敗：{path}｜{exc}")
        text = ""

    with _DAILY_LLM_CACHE_LOCK:
        _DAILY_LLM_CACHE_MEM[key] = text
    if text:
        print(f"⚡ Gemini 每日任務本機快取命中：{key}")
    return text


def save_daily_task_llm_cache(task: str, stock_code: str, output_text: str, cache_date: str | None = None):
    if not LLM_CACHE_ENABLE or not LLM_DAILY_TASK_CACHE_ENABLE or not output_text:
        return
    if not task or not stock_code:
        return
    key = _gsheet_llm_cache_key(task, stock_code, cache_date=cache_date)
    path = _daily_task_llm_cache_path(task, stock_code, cache_date=cache_date)
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(output_text))
        with _DAILY_LLM_CACHE_LOCK:
            _DAILY_LLM_CACHE_MEM[key] = str(output_text)
        print(f"💾 Gemini 每日任務結果已存本機：{key}")
    except Exception as exc:
        print(f"⚠️ Gemini 每日任務本機快取寫入失敗：{path}｜{exc}")


def load_gsheet_llm_cache(task: str, stock_code: str, stock_name: str = "", prompt: str = "") -> str:
    """本機每日任務快取優先；只有允許時才回退 Google Sheet。

    正式產圖可停用 Google Sheet 讀取，避免一次快取命中仍等待十多秒；
    預熱或舊資料相容模式仍可保留 Google Sheet 作為備援來源。
    """
    if LLM_CACHE_FORCE_REFRESH or not task or not stock_code:
        return ""

    local_text = load_daily_task_llm_cache(task, stock_code)
    if local_text:
        return local_text

    if not GSHEET_LLM_CACHE_ENABLE or not GSHEET_LLM_CACHE_READ_ENABLE:
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
            save_daily_task_llm_cache(task, stock_code, output_text)
            return output_text
    except Exception as exc:
        print(f"⚠️ Google Sheet Gemini 快取讀取失敗：{key}｜{exc}")
    return ""

def save_gsheet_llm_cache(task: str, stock_code: str, stock_name: str, prompt: str, output_text: str):
    """先寫本機每日快取；Google Sheet 寫入可獨立停用以縮短正式產圖時間。"""
    if not output_text or not task or not stock_code:
        return
    save_daily_task_llm_cache(task, stock_code, output_text)
    if not GSHEET_LLM_CACHE_ENABLE or not GSHEET_LLM_CACHE_WRITE_ENABLE:
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


def add_supertrend(df: pd.DataFrame, period=10, multiplier=2.5, use_atr=True) -> pd.DataFrame:
    """計算 Supertrend；使用 NumPy 陣列迴圈，避免逐列 ``.iloc`` 寫入的額外成本。"""
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["Supertrend", "Supertrend_Trend", "Supertrend_Buy", "Supertrend_Sell"],
            index=getattr(df, "index", None),
        )

    work = df.copy()
    hl2 = (work["High"] + work["Low"]) / 2
    tr = pd.concat([
        work["High"] - work["Low"],
        (work["High"] - work["Close"].shift()).abs(),
        (work["Low"] - work["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean() if use_atr else tr.rolling(period).mean()

    close = pd.to_numeric(work["Close"], errors="coerce").to_numpy(dtype=float)
    upper_basic = (hl2 - multiplier * atr).to_numpy(dtype=float)
    lower_basic = (hl2 + multiplier * atr).to_numpy(dtype=float)
    n = len(work)

    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    trend = np.ones(n, dtype=int)
    supertrend = np.full(n, np.nan, dtype=float)
    buy_signal = np.zeros(n, dtype=bool)
    sell_signal = np.zeros(n, dtype=bool)

    for i in range(1, n):
        prev_close = close[i - 1]
        prev_upper = upper_band[i - 1]
        prev_lower = lower_band[i - 1]

        upper_band[i] = (
            max(upper_basic[i], prev_upper)
            if prev_close > prev_upper
            else upper_basic[i]
        )
        lower_band[i] = (
            min(lower_basic[i], prev_lower)
            if prev_close < prev_lower
            else lower_basic[i]
        )

        previous_trend = trend[i - 1]
        if previous_trend == -1 and close[i] > prev_lower:
            trend[i] = 1
        elif previous_trend == 1 and close[i] < prev_upper:
            trend[i] = -1
        else:
            trend[i] = previous_trend

        buy_signal[i] = trend[i] == 1 and previous_trend == -1
        sell_signal[i] = trend[i] == -1 and previous_trend == 1
        supertrend[i] = upper_band[i] if trend[i] == 1 else lower_band[i]

    return pd.DataFrame({
        "Supertrend": supertrend,
        "Supertrend_Trend": trend,
        "Supertrend_Buy": buy_signal,
        "Supertrend_Sell": sell_signal,
    }, index=work.index)


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


def _open_warrant_cache_gsheet(force_refresh: bool = False, create_if_missing: bool = True):
    """開啟權證完整快照專用試算表；同次執行遇到 Drive 配額錯誤後不再重試。"""
    global _WARRANT_CACHE_GSHEET_HANDLE_CACHE, _WARRANT_CACHE_GSHEET_DISABLED_FOR_RUN

    if _WARRANT_CACHE_GSHEET_DISABLED_FOR_RUN and not force_refresh:
        return None

    if not WARRANT_CACHE_USE_SEPARATE_SHEET:
        return _open_gsheet(force_refresh=force_refresh)

    with _GSHEET_CONNECTION_LOCK:
        if _WARRANT_CACHE_GSHEET_HANDLE_CACHE is not None and not force_refresh:
            return _WARRANT_CACHE_GSHEET_HANDLE_CACHE

        gc = _build_gspread_client(force_refresh=force_refresh)
        if gc is None:
            return None

        try:
            if WARRANT_CACHE_GOOGLE_SHEET_ID:
                sh = gc.open_by_key(WARRANT_CACHE_GOOGLE_SHEET_ID)
            else:
                try:
                    sh = gc.open(WARRANT_CACHE_GOOGLE_SHEET_NAME)
                except Exception:
                    if not create_if_missing or not WARRANT_CACHE_GOOGLE_SHEET_AUTO_CREATE:
                        return None
                    sh = gc.create(WARRANT_CACHE_GOOGLE_SHEET_NAME)
                    print(
                        "🆕 已建立獨立權證快取試算表："
                        f"{WARRANT_CACHE_GOOGLE_SHEET_NAME}｜ID={getattr(sh, 'id', '')}"
                    )
            _WARRANT_CACHE_GSHEET_HANDLE_CACHE = sh
            print(
                "♻️ 權證快取試算表連線已建立並快取："
                f"{getattr(sh, 'title', WARRANT_CACHE_GOOGLE_SHEET_NAME)}"
            )
            return sh
        except Exception as e:
            _WARRANT_CACHE_GSHEET_HANDLE_CACHE = None
            error_text = str(e or "")
            if any(token in error_text.lower() for token in [
                "storage quota", "quota has been exceeded", "drive storage quota", "insufficient storage",
            ]):
                _WARRANT_CACHE_GSHEET_DISABLED_FOR_RUN = True
                print(f"⚠️ 權證快取試算表開啟失敗：{e}｜本次執行不再重試權證快取")
            else:
                print(f"⚠️ 權證快取試算表開啟失敗：{e}")
            return None


def _read_worksheet_from_spreadsheet(sh, title: str) -> pd.DataFrame:
    if sh is None:
        return pd.DataFrame()
    try:
        ws = sh.worksheet(title)
        records = ws.get_all_records(empty2zero=False, head=1)
        return pd.DataFrame(records).fillna("") if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def read_warrant_cache_worksheet(title: str, allow_legacy: bool = True) -> pd.DataFrame:
    """讀取權證快照專用工作表；查不到時不自動建立試算表，直接交由上層決定是否回退 Live。"""
    sh = _open_warrant_cache_gsheet(create_if_missing=False)
    df = _read_worksheet_from_spreadsheet(sh, title)
    if df is not None and not df.empty:
        return df

    if (
        allow_legacy
        and WARRANT_CACHE_USE_SEPARATE_SHEET
        and WARRANT_CACHE_LEGACY_MAIN_FALLBACK_ENABLE
    ):
        legacy_sh = _open_gsheet()
        legacy_df = _read_worksheet_from_spreadsheet(legacy_sh, title)
        if legacy_df is not None and not legacy_df.empty:
            print(f"📦 獨立權證快取尚無 {title}，暫時使用主試算表舊快取")
            return legacy_df
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


def _load_branch_perf_disk_cache() -> pd.DataFrame:
    if not BRANCH_PERF_DISK_CACHE_ENABLE:
        return pd.DataFrame()
    try:
        if not os.path.exists(_BRANCH_PERF_DISK_CACHE_PATH) or not os.path.exists(_BRANCH_PERF_DISK_META_PATH):
            return pd.DataFrame()
        with open(_BRANCH_PERF_DISK_META_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if str(meta.get("cache_date", "") or "") != _taipei_today_str():
            return pd.DataFrame()
        cached = pd.read_parquet(_BRANCH_PERF_DISK_CACHE_PATH)
        if cached is not None and not cached.empty:
            print(f"⚡ 分點勝率統計本機快取命中：{len(cached):,} 個分點")
            return cached
    except Exception as exc:
        print(f"⚠️ 分點勝率統計本機快取讀取失敗：{exc}")
    return pd.DataFrame()


def _save_branch_perf_disk_cache(df: pd.DataFrame, source_sheet: str = ""):
    if not BRANCH_PERF_DISK_CACHE_ENABLE or df is None or df.empty:
        return
    try:
        _ensure_dir(_BRANCH_PERF_DISK_CACHE_ROOT)
        tmp_path = _BRANCH_PERF_DISK_CACHE_PATH + ".tmp"
        df.to_parquet(tmp_path, index=False, compression="zstd")
        os.replace(tmp_path, _BRANCH_PERF_DISK_CACHE_PATH)
        meta = {
            "cache_date": _taipei_today_str(),
            "source_sheet": str(source_sheet or ""),
            "rows": int(len(df)),
            "updated_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        }
        with open(_BRANCH_PERF_DISK_META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"💾 分點勝率統計已存本機共用快取：{len(df):,} 個分點")
    except Exception as exc:
        print(f"⚠️ 分點勝率統計本機快取寫入失敗：{exc}")


def read_gsheet_branch_perf_df(force_refresh: bool = False) -> pd.DataFrame:
    global _BRANCH_PERF_CACHE_DF
    # 勝率統計只提供「分點歷史績效」作為文字輔助，不屬於本次交易資料快取；
    # 因此即使 REPORT_LIVE_ONLY=1，也允許讀取 Google Sheet 勝率統計。
    if not BRANCH_PERF_ENABLE:
        return pd.DataFrame()

    with _BRANCH_PERF_CACHE_LOCK:
        if _BRANCH_PERF_CACHE_DF is not None and not force_refresh:
            return _BRANCH_PERF_CACHE_DF.copy()

    if not force_refresh:
        disk_cached = _load_branch_perf_disk_cache()
        if disk_cached is not None and not disk_cached.empty:
            with _BRANCH_PERF_CACHE_LOCK:
                _BRANCH_PERF_CACHE_DF = disk_cached.copy()
            return disk_cached.copy()

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
            _save_branch_perf_disk_cache(result, source_sheet=ws.title)
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


GSHEET_WARRANT_HISTORY_HEADERS = [
    "日期", "權證代號", "權證名稱", "標的股", "標的名稱",
    "分點", "分點名稱", "券商代號", "買進金額", "賣出金額", "買超金額",
    "買進張數", "賣出張數", "資料來源", "快取起日", "快取迄日", "更新時間",
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


def _warrant_snapshot_worksheet_title(stock_code: str, start_date=None, end_date=None) -> str:
    """每檔股票固定使用一張快照工作表；每日更新直接覆寫，避免工作表數量與儲存格持續膨脹。"""
    code = _clean_code(stock_code) or "UNKNOWN"
    title = f"W_{code}"
    title = re.sub(r"[\/\?\*\[\]:]", "_", title)
    return title[:99]


@contextmanager


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


def _read_gsheet_warrant_status() -> pd.DataFrame:
    if not GSHEET_WARRANT_CACHE_ENABLE:
        return pd.DataFrame()
    return read_warrant_cache_worksheet(GSHEET_WARRANT_STATUS_SHEET)


# ============================================================
# 官方權證 OpenAPI：僅供發行商辨識與當日成交量完整性驗證
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


# 權證名稱中的發行券商簡稱，統一映射成券商根名稱。
# 這份表只用來辨識「該檔權證的發行券商」，不會用來過濾其他券商。
_FINMIND_WARRANT_ISSUER_ALIASES = {
    "中國信託": ["中國信託", "中信"],
    "華南永昌": ["華南永昌", "華南"],
    "永豐金": ["永豐金", "永豐"],
    "群益": ["群益金鼎", "群益"],
    "第一金": ["第一金"],
    "摩根士丹利": ["摩根士丹利", "大摩"],
    "摩根大通": ["摩根大通", "小摩"],
    "法銀巴黎": ["法銀巴黎", "法巴", "巴黎"],
    "花旗": ["花旗環球", "花旗"],
    "匯豐": ["香港上海滙豐", "香港上海匯豐", "滙豐", "匯豐"],
    "麥格理": ["港商麥格理", "麥格理"],
    "野村": ["香港商野村", "野村"],
    "里昂": ["港商里昂", "里昂"],
    "瑞銀": ["新加坡商瑞銀", "瑞銀"],
    "高盛": ["美商高盛", "高盛"],
    "美林": ["美商美林", "美林"],
    "德意志": ["德意志"],
    "元大": ["元大"],
    "凱基": ["凱基"],
    "國泰": ["國泰"],
    "統一": ["統一"],
    "元富": ["元富"],
    "兆豐": ["兆豐"],
    "富邦": ["富邦"],
    "國票": ["國票綜合", "國票"],
    "玉山": ["玉山"],
    "台新": ["台新"],
    "康和": ["康和"],
    "新光": ["新光"],
    "合庫": ["合作金庫", "合庫"],
    "台中銀": ["台中銀"],
    "大華銀": ["大華銀"],
    "星展": ["星展"],
}


@lru_cache(maxsize=8192)
def _finmind_compact_issuer_text(value: str) -> str:
    """壓縮發行商文字；高頻名稱透過 LRU 避免重複正規化與正則處理。"""
    s = html.unescape(str(value or "")).strip().replace("臺", "台")
    s = re.sub(r"(股份有限公司|有限公司|證券股份|證券公司|證券|分公司|營業處|營業部)", "", s)
    s = re.sub(r"[\s　\-＿_－—–/\\|｜·．・•()（）［］\[\]{}｛｝]+", "", s)
    return s.strip()


# Alias 在模組載入時只正規化一次；canonical 查詢不再每次重建 alias_key。
_FINMIND_WARRANT_ISSUER_ALIAS_KEYS = tuple(sorted(
    (
        (_finmind_compact_issuer_text(alias), canonical)
        for canonical, aliases in _FINMIND_WARRANT_ISSUER_ALIASES.items()
        for alias in aliases
        if _finmind_compact_issuer_text(alias)
    ),
    key=lambda item: (-len(item[0]), item[1], item[0]),
))

_FINMIND_ISSUER_HQ_BROKER_CODE_SET = frozenset(
    token.strip().upper()
    for token in re.split(r"[,，;；\s]+", os.getenv("FINMIND_ISSUER_HQ_BROKER_CODES", ""))
    if token.strip()
)


@lru_cache(maxsize=8192)
def _finmind_canonical_issuer_key(value: str) -> str:
    """將權證名稱或券商名稱轉成同一發行券商鍵；使用預編譯 alias 與 LRU。"""
    s = _finmind_compact_issuer_text(value)
    if not s:
        return ""
    for alias_key, canonical in _FINMIND_WARRANT_ISSUER_ALIAS_KEYS:
        if alias_key in s:
            return canonical
    return ""

def _finmind_extract_warrant_issuer_key(warrant_name: str, underlying_name: str = "") -> str:
    """由權證名稱辨識發行券商，例如「松川國票5B購01」→「國票」。"""
    name = _finmind_compact_issuer_text(warrant_name)
    if not name:
        return ""

    # 先嘗試移除標的名稱及常見的前 2～4 字標的簡稱，降低標的名稱誤含券商字樣的機率。
    underlying = _finmind_compact_issuer_text(underlying_name)
    search_texts = []
    if underlying and name.startswith(underlying):
        search_texts.append(name[len(underlying):])
    if underlying:
        for n in range(min(4, len(underlying)), 1, -1):
            prefix = underlying[:n]
            if name.startswith(prefix):
                search_texts.append(name[n:])
    search_texts.append(name)

    for candidate_text in search_texts:
        key = _finmind_canonical_issuer_key(candidate_text)
        if key:
            return key
    return ""


def _finmind_is_issuer_hq_or_market_maker_branch(
    branch: str,
    broker_code: str,
    issuer_key: str,
) -> bool:
    """只判斷同一發行券商中的明確總公司／自營／造市端。

    不再用 ``broker_code.endswith("0")`` 猜總公司，避免代碼尾碼巧合造成地方分點誤刪。
    特殊代碼可透過 ``FINMIND_ISSUER_HQ_BROKER_CODES`` 明確列入白名單式判定。
    """
    if not issuer_key:
        return False

    branch_raw = str(branch or "").strip()
    branch_key = _finmind_canonical_issuer_key(branch_raw)
    if not branch_key or branch_key != issuer_key:
        return False

    compact = _finmind_compact_issuer_text(branch_raw)
    code = str(broker_code or "").strip().upper()

    explicit_market_maker_terms = (
        "總公司", "總部", "本部", "自營", "承銷", "權證", "衍生", "金融商品",
        "金融交易", "綜合", "證券總公司",
    )
    if any(term in branch_raw for term in explicit_market_maker_terms):
        return True

    # 分點名稱正好就是券商根名稱，視為總公司列。
    issuer_compact = _finmind_compact_issuer_text(issuer_key)
    if compact == issuer_compact:
        return True

    # 僅接受明確設定的例外代碼，不再以尾碼推測；集合於模組載入時建立一次。
    return bool(code and code in _FINMIND_ISSUER_HQ_BROKER_CODE_SET)


def _official_row_value(row: dict, candidates: List[str]) -> str:
    """以大小寫與符號寬鬆方式取得官方欄位值。"""
    if not isinstance(row, dict):
        return ""
    for key in candidates:
        if key in row and str(row.get(key, "") or "").strip():
            return str(row.get(key, "") or "").strip()
    normalized = {
        re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(k or "").lower()): k
        for k in row.keys()
    }
    for key in candidates:
        nk = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(key or "").lower())
        actual = normalized.get(nk)
        if actual is not None and str(row.get(actual, "") or "").strip():
            return str(row.get(actual, "") or "").strip()
    return ""


def _fetch_official_warrant_issuer_source(url: str, source_label: str) -> tuple[dict, pd.DataFrame]:
    """讀取官方權證資料，建立「權證代號 → 發行商」對照；欄位未知時保留完整 Debug。"""
    try:
        response = get_thread_session().get(
            url,
            headers=OPENAPI_WARRANT_HEADERS,
            timeout=(8, 45),
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            rows = payload.get("data", payload.get("records", payload.get("result", [])))
        else:
            rows = payload
        if not isinstance(rows, list):
            raise RuntimeError(f"官方回應不是 list：type={type(rows).__name__}")
        raw = pd.DataFrame(rows).fillna("")
        if raw.empty:
            print(f"ℹ️ {source_label} 無資料")
            return {}, raw

        code_fields = [
            "權證代號", "證券代號", "代號", "warrant_code", "WarrantCode",
            "SecuritiesCode", "SecurityCode", "stock_id", "StockId",
        ]
        name_fields = [
            "權證名稱", "證券名稱", "名稱", "warrant_name", "WarrantName",
            "SecuritiesName", "SecurityName", "stock_name", "StockName",
        ]
        issuer_code_fields = [
            "發行人代號", "發行券商代號", "issuer_code", "IssuerCode",
            "SecuritiesCompanyCode", "SecuritiesFirmCode", "BrokerCode",
        ]
        issuer_name_fields = [
            "發行人名稱", "發行券商", "發行券商名稱", "issuer_name", "IssuerName",
            "SecuritiesCompany", "SecuritiesCompanyName", "SecuritiesFirmName", "BrokerName",
        ]
        liquidity_fields = [
            "流動量提供者", "流動量提供者證券商", "造市商", "造市券商",
            "liquidity_provider", "LiquidityProvider", "LiquidityProviderName", "MarketMaker",
        ]

        result = {}
        explicit_count = 0
        name_fallback_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = normalize_openapi_warrant_code(_official_row_value(row, code_fields))
            if not code:
                continue
            official_name = _official_row_value(row, name_fields)
            issuer_code = _official_row_value(row, issuer_code_fields)
            issuer_name = _official_row_value(row, issuer_name_fields)
            liquidity_provider = _official_row_value(row, liquidity_fields)

            issuer_key = (
                _finmind_canonical_issuer_key(liquidity_provider)
                or _finmind_canonical_issuer_key(issuer_name)
            )
            match_method = "official_field" if issuer_key else ""
            if issuer_key:
                explicit_count += 1
            elif official_name:
                issuer_key = _finmind_extract_warrant_issuer_key(official_name, "")
                if issuer_key:
                    name_fallback_count += 1
                    match_method = "official_name"

            if not issuer_key and not official_name and not issuer_name and not liquidity_provider:
                continue

            rec = {
                "issuer_key": issuer_key,
                "issuer_code": issuer_code,
                "issuer_name": issuer_name,
                "liquidity_provider": liquidity_provider,
                "official_warrant_name": official_name,
                "source": source_label,
                "match_method": match_method or "unresolved",
            }
            old = result.get(code)
            # 有官方發行人／流動量提供者欄位者優先於只靠權證名稱解析者。
            if old is None or (old.get("match_method") != "official_field" and match_method == "official_field"):
                result[code] = rec

        print(
            f"✅ {source_label} 發行商對照：權證={len(result):,}｜"
            f"官方欄位={explicit_count:,}｜官方名稱解析={name_fallback_count:,}"
        )
        if FINMIND_OFFICIAL_ISSUER_DEBUG_ENABLE and not result:
            _finmind_debug_print_df(
                f"{source_label} 無法辨識發行商，實際欄位",
                raw,
                once_key=f"official-issuer-schema:{source_label}",
            )
        return result, raw
    except Exception as exc:
        print(f"⚠️ {source_label} 發行商資料讀取失敗，改用其他官方來源／權證名稱：{exc}")
        return {}, pd.DataFrame()



def _finmind_official_warrant_issuer_map_impl(force_refresh: bool = False) -> dict:
    """真正執行官方發行商快取讀取／下載；同一輪只允許一個背景工作呼叫。"""
    global _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE
    if not FINMIND_OFFICIAL_ISSUER_ENABLE:
        return {}
    with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
        if _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE is not None and not force_refresh:
            return dict(_FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE)

    if not force_refresh:
        disk_mapping = _finmind_read_reference_json("official_warrant_issuer", max_age_days=1)
        if isinstance(disk_mapping, dict):
            with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
                _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE = dict(disk_mapping)
            return dict(disk_mapping)

    sources = [
        (TWSE_WARRANT_ISSUER_OPENAPI_URL, "TWSE 官方權證資料"),
        (TPEX_WARRANT_ISSUER_OPENAPI_URL, "TPEx 官方權證發行資料"),
    ]
    combined = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_map = {
            executor.submit(_fetch_official_warrant_issuer_source, url, label): label
            for url, label in sources
        }
        for future in as_completed(future_map):
            try:
                mapping, _ = future.result()
            except Exception as exc:
                print(f"⚠️ {future_map[future]} 發行商對照工作失敗：{exc}")
                mapping = {}
            for code, rec in mapping.items():
                old = combined.get(code)
                if old is None or (old.get("match_method") != "official_field" and rec.get("match_method") == "official_field"):
                    combined[code] = rec

    with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
        _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE = dict(combined)
    _finmind_write_reference_json("official_warrant_issuer", combined)
    print(f"📦 官方權證發行商合併對照：{len(combined):,} 支")
    return combined


def _finmind_start_official_warrant_issuer_prefetch():
    """在股價、新聞與權證流程開始時背景預載官方發行商；同一輪絕不重抓。"""
    global _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE, _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR
    if not FINMIND_OFFICIAL_ISSUER_ENABLE:
        return None
    with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
        if _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE is not None:
            return None
        if _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE is not None:
            return _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE
        _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR = ThreadPoolExecutor(max_workers=1)
        _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE = _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR.submit(
            _finmind_official_warrant_issuer_map_impl,
            False,
        )
        print("🚀 官方發行商對照已在背景預載，與新聞／歷史／最新日 API 同時進行")
        return _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE


def _finmind_official_warrant_issuer_map(force_refresh: bool = False) -> dict:
    """官方來源優先建立權證發行商對照；同一執行只等待既有背景工作，不重抓。"""
    global _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE, _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR
    if force_refresh:
        return _finmind_official_warrant_issuer_map_impl(True)
    with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
        if _FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE is not None:
            return dict(_FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE)
        future = _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE
    if future is None:
        return _finmind_official_warrant_issuer_map_impl(False)
    try:
        return dict(future.result() or {})
    finally:
        with _FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK:
            executor = _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR
            _FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE = None
            _FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR = None
        if executor is not None:
            executor.shutdown(wait=False)



def filter_warrant_flow_excluding_issuer_market_makers(events_df: pd.DataFrame) -> pd.DataFrame:
    """建立上方權證資金流與 TOP5 的可解讀口徑。

    計算口徑與舊版完全相同，但所有高成本文字解析都只針對唯一權證、唯一分點與
    唯一 ``(branch, broker_code, issuer_key)`` 組合執行一次，再向量化映射回全表。
    """
    if events_df is None or events_df.empty:
        return events_df.copy() if isinstance(events_df, pd.DataFrame) else pd.DataFrame()
    if not FINMIND_ISSUER_FLOW_ENABLE:
        print("ℹ️ FinMind 發行券商造市端排除已關閉；權證資金流將使用完整雙邊資料")
        return events_df.copy()

    required = {"warrant_code", "warrant_name", "branch", "broker_code", "buy_amount", "sell_amount", "net_amount"}
    missing = required - set(events_df.columns)
    if missing:
        print(f"⚠️ 無法建立發行券商權證資金流，缺少欄位：{sorted(missing)}")
        return events_df.copy()

    stage_started = time.perf_counter()
    already_normalized = bool(events_df.attrs.get("_warrant_events_normalized"))
    e = events_df.copy().reset_index(drop=True)

    # 數值欄已是數值 dtype 時不重做 to_numeric；compact / API 正規化事件不再重跑字串 normalize。
    for col in ["buy_amount", "sell_amount", "net_amount"]:
        if not pd.api.types.is_numeric_dtype(e[col]):
            e[col] = pd.to_numeric(e[col], errors="coerce").fillna(0.0)
        if e[col].dtype != float:
            e[col] = e[col].astype(float, copy=False)

    if not already_normalized:
        e["branch"] = e["branch"].astype(str).map(normalize_branch_name)
        e["broker_code"] = e["broker_code"].astype(str).str.strip()
        e["warrant_code"] = e["warrant_code"].map(normalize_openapi_warrant_code)
    e["warrant_name"] = e["warrant_name"].fillna("").astype(str).str.strip()
    if "underlying_name" not in e.columns:
        e["underlying_name"] = ""
    else:
        e["underlying_name"] = e["underlying_name"].fillna("").astype(str)

    official_map = _finmind_official_warrant_issuer_map()

    # 發行商只按不同權證辨識一次。
    unique_warrants = (
        e[["warrant_code", "warrant_name", "underlying_name"]]
        .drop_duplicates(subset=["warrant_code"], keep="last")
        .reset_index(drop=True)
    )
    issuer_lookup = {}
    for row in unique_warrants.itertuples(index=False):
        code = str(row.warrant_code or "")
        rec = official_map.get(code, {})
        key = str(rec.get("issuer_key", "") or "").strip()
        method = str(rec.get("match_method", "") or "").strip()
        source = str(rec.get("source", "") or "").strip()
        if key and method == "official_field":
            issuer_source = f"{source}:official_field"
        elif key:
            issuer_source = f"{source}:official_name"
        else:
            key = _finmind_extract_warrant_issuer_key(row.warrant_name, row.underlying_name)
            issuer_source = "FinMind權證名稱解析" if key else "unresolved"
        issuer_lookup[code] = {
            "issuer_key": key,
            "issuer_source": issuer_source,
            "issuer_name": str(rec.get("issuer_name", "") or ""),
            "liquidity_provider": str(rec.get("liquidity_provider", "") or ""),
        }

    # 四個 lambda map 改成純 dict map，避免 10～20 萬次 Python callback。
    issuer_key_map = {code: rec["issuer_key"] for code, rec in issuer_lookup.items()}
    issuer_source_map = {code: rec["issuer_source"] for code, rec in issuer_lookup.items()}
    issuer_name_map = {code: rec["issuer_name"] for code, rec in issuer_lookup.items()}
    liquidity_provider_map = {code: rec["liquidity_provider"] for code, rec in issuer_lookup.items()}
    e["_issuer_key"] = e["warrant_code"].map(issuer_key_map).fillna("")
    e["_issuer_source"] = e["warrant_code"].map(issuer_source_map).fillna("unresolved")
    e["_official_issuer_name"] = e["warrant_code"].map(issuer_name_map).fillna("")
    e["_official_liquidity_provider"] = e["warrant_code"].map(liquidity_provider_map).fillna("")

    source_s = e["_issuer_source"].astype(str)
    official_field_mask = source_s.str.contains("official_field", na=False)
    official_name_mask = source_s.str.contains("official_name", na=False)
    finmind_name_mask = e["_issuer_source"].eq("FinMind權證名稱解析")
    official_field_count = int(official_field_mask.sum())
    official_name_count = int(official_name_mask.sum())
    finmind_name_count = int(finmind_name_mask.sum())

    # branch 只正規解析不同名稱一次，再 map 回全表。
    unique_branch_values = pd.unique(e["branch"])
    branch_issuer_map = {
        branch: _finmind_canonical_issuer_key(branch)
        for branch in unique_branch_values
    }
    e["_branch_issuer_key"] = e["branch"].map(branch_issuer_map).fillna("")
    e["_issuer_same_broker"] = (
        e["_issuer_key"].ne("")
        & e["_branch_issuer_key"].eq(e["_issuer_key"])
    )

    # 只對唯一 (branch, broker_code, issuer_key) 組合做 Python 判斷。
    combo_cols = ["branch", "broker_code", "_issuer_key"]
    candidate_combos = (
        e.loc[e["_issuer_same_broker"], combo_cols]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    if candidate_combos.empty:
        e["_issuer_market_maker"] = False
        unique_combo_count = 0
    else:
        combo_flags = np.fromiter(
            (
                _finmind_is_issuer_hq_or_market_maker_branch(branch, broker, issuer)
                for branch, broker, issuer in candidate_combos.itertuples(index=False, name=None)
            ),
            dtype=bool,
            count=len(candidate_combos),
        )
        combo_index = pd.MultiIndex.from_frame(candidate_combos[combo_cols])
        combo_flag_map = pd.Series(combo_flags, index=combo_index)
        row_index = pd.MultiIndex.from_frame(e[combo_cols])
        e["_issuer_market_maker"] = combo_flag_map.reindex(row_index, fill_value=False).to_numpy(dtype=bool)
        unique_combo_count = len(candidate_combos)

    issuer_market_maker_mask = e["_issuer_same_broker"] & e["_issuer_market_maker"]
    unresolved_mask = e["_issuer_key"].eq("")
    removed_rows = int(issuer_market_maker_mask.sum())
    unresolved_rows = int(unresolved_mask.sum())
    final_remove_mask = issuer_market_maker_mask.copy()
    if FINMIND_ISSUER_FLOW_EXCLUDE_UNRESOLVED_ENABLE:
        final_remove_mask = final_remove_mask | unresolved_mask
    kept = e.loc[~final_remove_mask].copy()

    raw_buy = float(e["buy_amount"].sum())
    raw_sell = float(e["sell_amount"].sum())
    raw_net = float(e["net_amount"].sum())
    kept_buy = float(kept["buy_amount"].sum())
    kept_sell = float(kept["sell_amount"].sum())
    kept_net = float(kept["net_amount"].sum())

    parsed_warrants = int(e.loc[e["_issuer_key"].ne(""), "warrant_code"].nunique())
    total_warrants = int(e["warrant_code"].nunique())
    unresolved_warrants = int(e.loc[unresolved_mask, "warrant_code"].nunique()) if unresolved_rows else 0
    print(
        "💰 FinMind 權證資金流口徑：逐檔排除官方辨識的發行造市端｜"
        f"權證辨識={parsed_warrants}/{total_warrants}｜造市端排除={removed_rows:,}筆｜"
        f"未辨識排除={unresolved_warrants}支/{unresolved_rows:,}筆｜保留={len(kept):,}筆"
    )
    official_field_warrants = int(e.loc[official_field_mask, "warrant_code"].nunique())
    official_name_warrants = int(e.loc[official_name_mask, "warrant_code"].nunique())
    finmind_name_warrants = int(e.loc[finmind_name_mask, "warrant_code"].nunique())
    print(
        "🔎 發行商辨識來源："
        f"官方欄位={official_field_warrants}支/{official_field_count:,}列｜"
        f"官方名稱={official_name_warrants}支/{official_name_count:,}列｜"
        f"FinMind名稱備援={finmind_name_warrants}支/{finmind_name_count:,}列"
    )
    print(
        f"💰 權證資金流檢查：原始買進={fmt_money(raw_buy)}｜原始賣出={fmt_money(-raw_sell)}｜原始淨額={fmt_money(raw_net)}｜"
        f"排除後買進={fmt_money(kept_buy)}｜排除後賣出={fmt_money(-kept_sell)}｜排除後淨額={fmt_money(kept_net)}"
    )

    if unresolved_rows:
        unknown_summary = (
            e.loc[unresolved_mask, [
                "warrant_code", "warrant_name", "underlying_name", "_issuer_source",
                "_official_issuer_name", "_official_liquidity_provider",
            ]]
            .drop_duplicates()
            .head(FINMIND_ISSUER_FLOW_DEBUG_MAX_ROWS)
        )
        action_text = "已排除正式資金流" if FINMIND_ISSUER_FLOW_EXCLUDE_UNRESOLVED_ENABLE else "目前仍保留"
        print(f"⚠️ 發行商仍無法辨識的權證：{unresolved_warrants:,} 支｜{action_text}")
        print(unknown_summary.to_string(index=False))

    if FINMIND_ISSUER_FLOW_DEBUG_ENABLE and removed_rows:
        removed = e.loc[issuer_market_maker_mask]
        removed_summary = (
            removed.groupby(
                ["warrant_code", "warrant_name", "_issuer_key", "_issuer_source", "broker_code", "branch"],
                as_index=False,
                dropna=False,
            )
            .agg(
                rows=("net_amount", "size"),
                buy_amount=("buy_amount", "sum"),
                sell_amount=("sell_amount", "sum"),
                net_amount=("net_amount", "sum"),
            )
        )
        removed_summary["abs_net"] = removed_summary["net_amount"].abs()
        removed_summary = removed_summary.sort_values(
            ["abs_net", "rows"], ascending=[False, False]
        ).drop(columns=["abs_net"])
        print("🧪 逐檔排除的發行券商造市列（前幾筆）：")
        print(removed_summary.head(FINMIND_ISSUER_FLOW_DEBUG_MAX_ROWS).to_string(index=False))

    elapsed = time.perf_counter() - stage_started
    print(
        f"⚡ 發行商造市端排除效能：事件={len(e):,}列｜權證={len(unique_warrants):,}支｜"
        f"分點名稱={len(unique_branch_values):,}個｜唯一判斷組合={unique_combo_count:,}組｜"
        f"normalized_cache={'命中' if already_normalized else '未命中'}｜{elapsed:.2f}秒"
    )

    result = kept.drop(columns=[
        "_issuer_key", "_issuer_source", "_official_issuer_name", "_official_liquidity_provider",
        "_branch_issuer_key", "_issuer_same_broker", "_issuer_market_maker",
    ], errors="ignore").reset_index(drop=True)
    result.attrs["_warrant_events_normalized"] = True
    return result


def build_weekly_context(stock_df: pd.DataFrame, warrant_events: pd.DataFrame, week_days: int = WEEK_TRADING_DAYS):
    plot_df = stock_df.tail(CHART_LOOKBACK).copy()
    trading_dates = [pd.Timestamp(d).normalize() for d in list(plot_df.index)]
    warrant_data_end = pd.NaT
    if warrant_events is not None and not warrant_events.empty and "Date" in warrant_events.columns:
        warrant_dates_for_label = pd.to_datetime(warrant_events["Date"], errors="coerce").dropna()
        if not warrant_dates_for_label.empty:
            warrant_data_end = pd.Timestamp(warrant_dates_for_label.max()).normalize()

    # 週報的權證統計區間改用「股價日期 + 權證事件日期」合併日期軸。
    # 原本只用股價 K 線最新日當週報結束日，若股價資料晚一天更新，
    # TOP5 買賣超與本週權證淨流向就會漏掉 FinMind 已更新的今日分點資料。
    if warrant_events is not None and not warrant_events.empty and "Date" in warrant_events.columns and trading_dates:
        report_dates = build_flow_axis_dates(plot_df, warrant_events)
    else:
        report_dates = trading_dates

    report_dates = [pd.Timestamp(d).normalize() for d in report_dates]
    week_start, week_end, week_dates = _resolve_report_week_range(report_dates, fallback_count=week_days)

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
        if WARRANT_WEEK_RANGE_MODE in {"calendar7", "calendar", "7d", "seven_days"}:
            prev_week_end = week_start - pd.Timedelta(days=1)
            prev_week_start = prev_week_end - pd.Timedelta(days=WARRANT_WEEK_CALENDAR_DAYS - 1)
            prev_stock = plot_df[
                (pd.to_datetime(plot_df.index).normalize() >= prev_week_start)
                & (pd.to_datetime(plot_df.index).normalize() <= prev_week_end)
            ].copy()
        else:
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
        raw_week_events = week_events.copy()
        raw_plot_events = week_events.copy()
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

        # 兩種口徑分開保存：
        # 1. flow_e：上方權證資金流與 TOP5，使用歷史 Parquet＋最新日完整 API，並逐檔排除發行造市端。
        # 2. e：原始完整分點事件，供下方精選分點使用，不套用發行造市端排除。
        with report_stage_timer("發行商造市端排除"):
            flow_e = filter_warrant_flow_excluding_issuer_market_makers(e)
        flow_dates_for_label = pd.to_datetime(
            flow_e.get("Date", pd.Series(dtype="datetime64[ns]")),
            errors="coerce",
        ).dropna()
        warrant_data_end = (
            pd.Timestamp(flow_dates_for_label.max()).normalize()
            if not flow_dates_for_label.empty
            else pd.NaT
        )

        if pd.notna(week_start) and pd.notna(week_end):
            week_events = flow_e[(flow_e["Date"] >= week_start) & (flow_e["Date"] <= week_end)].copy()
            raw_week_events = e[(e["Date"] >= week_start) & (e["Date"] <= week_end)].copy()
        else:
            week_events = flow_e.iloc[0:0].copy()
            raw_week_events = e.iloc[0:0].copy()

        if trading_dates:
            plot_start = pd.Timestamp(plot_df.index.min()).normalize()
            plot_end = pd.Timestamp(report_dates[-1]).normalize() if report_dates else pd.Timestamp(plot_df.index.max()).normalize()
            plot_events = flow_e[(flow_e["Date"] >= plot_start) & (flow_e["Date"] <= plot_end)].copy()
            raw_plot_events = e[(e["Date"] >= plot_start) & (e["Date"] <= plot_end)].copy()
        else:
            plot_events = flow_e.copy()
            raw_plot_events = e.copy()

    total_buy = float(week_events["buy_amount"].sum()) if not week_events.empty else 0.0
    total_sell = float(week_events["sell_amount"].sum()) if not week_events.empty else 0.0
    total_net = float(week_events["net_amount"].sum()) if not week_events.empty else 0.0
    bias = "偏買超" if total_net > 0 else "偏賣超" if total_net < 0 else "中性"

    print(
        f"📅 週報統計區間：{week_start.strftime('%Y/%m/%d') if pd.notna(week_start) else '-'} - "
        f"{week_end.strftime('%Y/%m/%d') if pd.notna(week_end) else '-'}｜"
        f"模式={WARRANT_WEEK_RANGE_MODE}｜實際股價日={len(stock_week_dates)}｜"
        f"權證事件={len(week_events):,}｜"
        f"權證資料截至={warrant_data_end.strftime('%Y/%m/%d') if pd.notna(warrant_data_end) else '-'}"
    )

    return {
        "plot_df": plot_df,

        "plot_events": plot_events,
        "week_events": week_events,
        "raw_plot_events": raw_plot_events,
        "raw_week_events": raw_week_events,
        "week_start": week_start,
        "week_end": week_end,
        "warrant_data_end": warrant_data_end,
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


def daily_warrant_net_from_dates(dates, events: pd.DataFrame) -> pd.DataFrame:
    """依指定日期軸彙總每日權證淨額。

    原本 daily_warrant_net() 只會使用股價 K 線日期作為日期軸，因此若權證分點資料已經更新到
    最新交易日，但股價資料還停在前一個交易日，最新一日的權證資金流就會被圖表日期軸排除。
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
    """取得台北日期的今日 00:00，並統一回傳 tz-naive Timestamp。

    GitHub Actions runner 常用 UTC，若直接 datetime.today() 可能在台灣盤後仍停在前一天；
    權證分點資料抓取區間應以台北日期為準。回傳 tz-naive 可避免與股價索引比較時
    出現 Cannot compare tz-naive and tz-aware timestamps。
    """
    return pd.Timestamp.now(tz="Asia/Taipei").normalize().tz_localize(None)


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


def _parse_finmind_selected_branch_id_overrides() -> dict:
    """解析 FINMIND_SELECTED_BRANCH_ID_OVERRIDES：分點名稱=代碼，多筆可逗號分隔。"""
    out = {}
    raw = str(FINMIND_SELECTED_BRANCH_ID_OVERRIDES_RAW or "").strip()
    if not raw:
        return out
    for item in re.split(r"[,，;；\n\r]+", raw):
        item = str(item or "").strip()
        if not item:
            continue
        parts = re.split(r"[=:：]", item, maxsplit=1)
        if len(parts) != 2:
            print(f"⚠️ FINMIND_SELECTED_BRANCH_ID_OVERRIDES 格式錯誤，略過：{item}")
            continue
        name = normalize_branch_name(parts[0])
        trader_id = str(parts[1] or "").strip()
        if name and trader_id:
            out.setdefault(name, set()).add(trader_id)
    return out


def _branch_location_tokens(value: str) -> list:
    """從使用者分點名稱取出可能的地名尾碼，僅用於候選排序，不直接猜 ID。"""
    key = _finmind_branch_lookup_key(value)
    locations = [
        "台北", "台中", "中壢", "桃園", "新竹", "高雄", "台南", "板橋", "中和", "內湖",
        "南屯", "敦南", "公益", "忠孝", "南京", "三重", "新店", "士林", "基隆", "彰化",
        "員林", "竹北", "竹科", "市政", "信義", "淡水", "頭份", "內壢", "東大", "古亭",
        "民權", "汐止", "虎尾", "東港", "苑裡", "民生", "三多", "土城", "建成", "溪湖",
        "豐中", "忠明", "復興", "城中", "松山", "永和", "嘉義", "屏東", "羅東", "花蓮",
    ]
    return [loc for loc in locations if loc in key]


def _rank_finmind_branch_candidates(info: pd.DataFrame, requested: str) -> pd.DataFrame:
    """列出名稱最接近的官方分點；只提供 Debug，不在多解時自行亂選。"""
    if info is None or info.empty:
        return pd.DataFrame()
    key = _finmind_branch_lookup_key(requested)
    root = _finmind_branch_root_key(requested)
    location_tokens = _branch_location_tokens(requested)
    work = info.copy()
    work["_score"] = 0
    lookup = work["branch_lookup_key"].astype(str)
    roots = work["branch_root_key"].astype(str)
    work.loc[lookup.eq(key), "_score"] += 100
    if key:
        work.loc[lookup.str.contains(key, regex=False), "_score"] += 50
        work.loc[lookup.map(lambda x: bool(x) and x in key), "_score"] += 35
    if root:
        work.loc[roots.eq(root), "_score"] += 30
        work.loc[lookup.str.contains(root, regex=False), "_score"] += 15
    for token in location_tokens:
        work.loc[lookup.str.contains(token, regex=False), "_score"] += 25
        if "address" in work.columns:
            work.loc[work["address"].astype(str).str.contains(token, regex=False), "_score"] += 8
    work = work[work["_score"] > 0].copy()
    return work.sort_values(["_score", "securities_trader_id"], ascending=[False, True])


def _resolve_selected_branch_ids(selected_branches: set) -> dict:
    """將使用者分點名稱解析為 FinMind securities_trader_id；結果以輸入分點為 key。

    僅在官方名稱正規化後唯一命中時自動採用。模糊候選只印出，不會亂選，避免
    把「第一金中壢」錯配成第一金總公司或其他分點。
    """
    cache_key = tuple(sorted(normalize_branch_name(x) for x in selected_branches if normalize_branch_name(x)))
    with _FINMIND_DATA_CACHE_LOCK:
        cached = _FINMIND_SELECTED_BRANCH_ID_CACHE.get(cache_key)
        if cached is not None:
            return {k: set(v) for k, v in cached.items()}

    resolved = {}
    overrides = _parse_finmind_selected_branch_id_overrides()
    try:
        info = _finmind_load_securities_trader_info()
    except Exception as exc:
        print(f"⚠️ 無法載入 TaiwanSecuritiesTraderInfo，精選分點無法使用正式 ID：{exc}")
        for requested in cache_key:
            resolved[requested] = set(overrides.get(requested, set()))
        return resolved

    print(
        f"🧩 FinMind 分點解析啟動｜build={FINMIND_BUILD_VERSION}｜"
        f"官方分點代碼={len(info):,}｜輸入={'、'.join(cache_key) or '無'}"
    )

    for requested in cache_key:
        if requested in overrides and overrides[requested]:
            ids = set(overrides[requested])
            resolved[requested] = ids
            print(f"✅ 精選分點使用手動 ID：{requested} → {sorted(ids)}")
            continue

        key = _finmind_branch_lookup_key(requested)
        exact = info[info["branch_lookup_key"].astype(str) == key].copy()
        exact_ids = sorted(set(exact["securities_trader_id"].astype(str))) if not exact.empty else []

        if len(exact_ids) == 1:
            resolved[requested] = {exact_ids[0]}
            label_rows = exact[[c for c in [
                "securities_trader_id", "securities_trader", "date", "address", "phone",
                "branch_lookup_key",
            ] if c in exact.columns]]
            print(f"✅ 精選分點 ID 對照成功：{requested} → {exact_ids[0]}")
            print(label_rows.head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))
            continue

        if len(exact_ids) > 1:
            resolved[requested] = set()
            print(f"⚠️ 精選分點名稱對應多個 ID，為避免誤配暫不自動選擇：{requested} → {exact_ids}")
            print(exact.head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))
            continue

        ranked = _rank_finmind_branch_candidates(info, requested)
        resolved[requested] = set()
        print(
            f"⚠️ 精選分點 ID 對照失敗：{requested}｜lookup_key={key}｜"
            f"候選={len(ranked):,}"
        )
        if ranked.empty:
            _finmind_debug_print_df(
                f"TaiwanSecuritiesTraderInfo 無候選｜輸入={requested}",
                info,
                max_rows=FINMIND_DEBUG_MAX_ROWS,
            )
        else:
            show_cols = [c for c in [
                "_score", "securities_trader_id", "securities_trader", "date", "address", "phone",
                "branch_lookup_key", "branch_root_key",
            ] if c in ranked.columns]
            print("🧪 請將以下候選完整複製回來：")
            print(ranked[show_cols].head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))

    with _FINMIND_DATA_CACHE_LOCK:
        _FINMIND_SELECTED_BRANCH_ID_CACHE[cache_key] = {k: set(v) for k, v in resolved.items()}
    return resolved


def filter_selected_branch_flow_events(events_df: pd.DataFrame) -> pd.DataFrame:
    """篩出精選分點資金流事件；優先使用 securities_trader_id，不再只靠券商簡稱。"""
    if not SELECTED_BRANCH_FLOW_ENABLE:
        return pd.DataFrame()
    if events_df is None or events_df.empty:
        return pd.DataFrame()
    need_cols = {"Date", "branch", "broker_code", "net_amount", "buy_amount", "sell_amount"}
    if not need_cols.issubset(events_df.columns):
        _finmind_debug_print_df("精選分點事件欄位不足", events_df)
        print(f"⚠️ 精選分點事件缺少欄位：{sorted(need_cols - set(events_df.columns))}")
        return pd.DataFrame()

    selected_branches = _get_selected_branch_flow_set()
    if not selected_branches:
        return pd.DataFrame()

    e = events_df.copy()
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    e["branch"] = e["branch"].map(normalize_branch_name)
    e["broker_code"] = e["broker_code"].astype(str).str.strip()
    for c in ["buy_amount", "sell_amount", "net_amount"]:
        e[c] = pd.to_numeric(e[c], errors="coerce").fillna(0.0).astype(float)

    resolved_map = _resolve_selected_branch_ids(selected_branches)
    selected_ids = set().union(*(ids for ids in resolved_map.values())) if resolved_map else set()

    # 主要條件：正式分點 ID。名稱比對僅作為對照表失敗時的安全備援。
    id_mask = e["broker_code"].isin(selected_ids) if selected_ids else pd.Series(False, index=e.index)
    selected_names_normalized = {normalize_branch_name(x) for x in selected_branches}
    selected_keys = {_finmind_branch_lookup_key(x) for x in selected_branches if _finmind_branch_lookup_key(x)}
    exact_name_mask = e["branch"].isin(selected_names_normalized)
    alias_name_mask = e["branch"].map(_finmind_branch_lookup_key).isin(selected_keys) if selected_keys else pd.Series(False, index=e.index)
    mask = id_mask | exact_name_mask | alias_name_mask
    matched = e.loc[mask].copy().reset_index(drop=True)

    if matched.empty:
        print(f"⚠️ 精選分點沒有權證交易：{'、'.join(sorted(selected_branches))}")
        _debug_selected_branch_candidates(selected_branches, e)
    else:
        matched_names = "、".join(sorted(set(matched["branch"].astype(str))))
        matched_ids = "、".join(sorted(set(matched["broker_code"].astype(str))))
        print(
            f"✅ 精選分點 FinMind ID 比對成功：{matched_names}｜"
            f"ID={matched_ids}｜{len(matched):,} 筆"
        )
        if FINMIND_DEBUG_SELECTED_BRANCH_ENABLE:
            summary = matched.groupby(["broker_code", "branch"], as_index=False).agg(
                rows=("net_amount", "size"),
                first_date=("Date", "min"),
                last_date=("Date", "max"),
                buy_amount=("buy_amount", "sum"),
                sell_amount=("sell_amount", "sum"),
                net_amount=("net_amount", "sum"),
            )
            print("🧪 精選分點命中彙總：")
            print(summary.head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))

    return matched


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
        s = _strip_report_tone_metadata(str(p or "").strip())
        if not s or s.startswith(dependent_starts):
            return False
        if "…" in s or "..." in s:
            return False
        if s[-1] not in "。！？":
            return False
    return True


def _trim_weekly_point(text: str, max_len: int | None = None) -> str:
    max_len = int(max_len or WEEKLY_KEYPOINT_POINT_MAX_LEN)
    tone = _extract_report_tone_from_point(text)
    s = _strip_report_tone_metadata(text)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = re.sub(r"^(本週重點|重點|摘要)[:：]\s*", "", s).strip()
    # 舊版 prompt 可能輸出「結論｜依據」；顯示時改成更容易一眼辨識的標籤。
    s = re.sub(r"^結論[｜|]", "結論：", s)
    s = re.sub(r"^下週觀察[:：]結論[｜|]", "下週觀察：結論：", s)
    s = s.replace("｜依據：", "｜依據：").replace("｜觀察：", "｜觀察：")
    trimmed = _finish_complete_summary_point(s, max_len=max_len, min_cut_len=36)
    return _replace_report_point_tone(trimmed, tone) if trimmed and tone else trimmed

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
        cleaned = _clean_weekly_key_points(ai_points)[:3]
        return _apply_programmatic_weekly_tones(cleaned, ctx)

    # AI 不可用、呼叫失敗、格式不合格或內容過短時，才回到條件式備援。
    rule_points = _ensure_branch_perf_point(_rule_based_key_points(ctx, stock_name), ctx)
    ensured = _ensure_weekly_keypoint_min_total(rule_points, ctx, stock_name)
    return _apply_programmatic_weekly_tones(ensured, ctx)

# ============================================================
# 新聞抓取：抓一週內新聞內文並整理成真正重點
# ============================================================

NEWS_BODY_MAX_CHARS = int(os.getenv("WARRANT_NEWS_BODY_MAX_CHARS", "3500"))
NEWS_FETCH_TIMEOUT = float(os.getenv("WARRANT_NEWS_FETCH_TIMEOUT", "6"))
NEWS_SUMMARY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_SUMMARY_MAX_POINTS", "3"))
NEWS_DISPLAY_MAX_POINTS = int(os.getenv("WARRANT_NEWS_DISPLAY_MAX_POINTS", "3"))
NEWS_SUMMARY_POINT_MAX_LEN = int(os.getenv("WARRANT_NEWS_SUMMARY_POINT_MAX_LEN", "150"))
NEWS_SUMMARY_MIN_TOTAL_CHARS = int(os.getenv("WARRANT_NEWS_SUMMARY_MIN_TOTAL_CHARS", "120"))
# 新聞 detail 驗收：必須同時包含具體事件與營運意涵／後續觀察。
NEWS_SUMMARY_DETAIL_MIN_CHARS = int(os.getenv("WARRANT_NEWS_SUMMARY_DETAIL_MIN_CHARS", "50"))
NEWS_SUMMARY_DETAIL_MAX_CHARS = int(os.getenv("WARRANT_NEWS_SUMMARY_DETAIL_MAX_CHARS", "70"))
# 驗證後少於 2 點、但仍有至少 3 篇合格文章時，在唯一一次補正呼叫內一併補不同事件。
NEWS_SUPPLEMENT_TRIGGER_POINTS = int(os.getenv("WARRANT_NEWS_SUPPLEMENT_TRIGGER_POINTS", "2"))
NEWS_SUPPLEMENT_MIN_USABLE_ARTICLES = int(os.getenv("WARRANT_NEWS_SUPPLEMENT_MIN_USABLE_ARTICLES", "3"))
NEWS_GEMINI_REPAIR_ENABLE = os.getenv(
    "WARRANT_NEWS_GEMINI_REPAIR_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
NEWS_GEMINI_SUPPLEMENT_ENABLE = os.getenv(
    "WARRANT_NEWS_GEMINI_SUPPLEMENT_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# 新聞摘要風格版本：調整 prompt 後使用新快取鍵，避免 Google Sheet 當日舊快取繼續輸出舊版空泛摘要。
NEWS_SUMMARY_STYLE_VERSION = os.getenv("WARRANT_NEWS_SUMMARY_STYLE_VERSION", "v15_arabic_digits_news").strip() or "v15_arabic_digits_news"
NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK = os.getenv("WARRANT_NEWS_ALLOW_OLD_STYLE_CACHE_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


def _news_points_cache_task() -> str:
    safe_version = re.sub(r"[^A-Za-z0-9_.-]", "_", str(NEWS_SUMMARY_STYLE_VERSION or "v15_arabic_digits_news"))
    # 內部版本固定加在任務鍵後面，避免 Actions 環境變數仍停在舊版時，
    # 繼續讀到先前 0 點或壞格式的新聞快取。
    internal_version = "validated_v32_rich_local_grounding_two_call"
    return f"news_points_{safe_version}_{internal_version}"

# 只用真正抓到的新聞內文產生摘要；不要把 RSS 標題或導流摘要直接當成重點。
NEWS_MIN_BODY_CHARS = int(os.getenv("WARRANT_NEWS_MIN_BODY_CHARS", "260"))
# 預設：優先用新聞原文；若原文被擋，允許用 RSS 摘要文字「改寫成重點」，但不直接輸出標題。
NEWS_REQUIRE_ARTICLE_BODY = os.getenv("WARRANT_NEWS_REQUIRE_BODY", "0").strip().lower() not in ("0", "false", "no", "off")
NEWS_RSS_DESCRIPTION_FALLBACK = os.getenv("WARRANT_NEWS_RSS_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")
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
NEWS_MAX_ARTICLES_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLES_TO_GEMINI", "8"))
NEWS_MAX_ARTICLE_CHARS_TO_GEMINI = int(os.getenv("WARRANT_NEWS_MAX_ARTICLE_CHARS_TO_GEMINI", "3500"))
WEEKLY_KEYPOINT_LLM_ENABLE = os.getenv("WARRANT_WEEKLY_KEYPOINT_LLM_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
WEEKLY_GEMINI_REPAIR_ENABLE = os.getenv(
    "WARRANT_WEEKLY_GEMINI_REPAIR_ENABLE",
    "0",
).strip().lower() not in ("0", "false", "no", "off")
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
NEWS_GOOGLE_MAX_ITEMS = int(os.getenv("WARRANT_NEWS_GOOGLE_MAX_ITEMS", "24"))
NEWS_GOOGLE_SCAN_MULTIPLIER = int(os.getenv("WARRANT_NEWS_GOOGLE_SCAN_MULTIPLIER", "4"))
NEWS_GOOGLE_MIN_USABLE_ARTICLES = int(os.getenv("WARRANT_NEWS_GOOGLE_MIN_USABLE_ARTICLES", str(max(2, min(4, NEWS_SUMMARY_MAX_POINTS)))))
NEWS_GOOGLE_FALLBACK_DAYS = os.getenv("WARRANT_NEWS_FALLBACK_DAYS", "7,14,30").strip() or "7,14,30"
# 極速新聞模式：預設開啟。只使用 Google News RSS 的標題 / 摘要 / URL，不進新聞網站抓原文。
# 若想回到高品質原文抓取模式，可在 GitHub Actions 設 WARRANT_NEWS_FAST_MODE=0。
NEWS_FAST_MODE = os.getenv("WARRANT_NEWS_FAST_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
# 混合新聞模式：先用 RSS 快速掃描與排序，再只替相關性最高的前 2～3 篇補抓原文。
# 預設開啟；其餘新聞仍維持 RSS 快速素材，兼顧速度與 detail 的具體度。
# 若要完全停用 Top-K 原文補抓，可設 WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_ENABLE=0。
# NEWS_FAST_MODE=0 時仍沿用既有完整原文模式。
NEWS_FAST_HYBRID_BODY_FETCH_ENABLE = os.getenv(
    "WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
NEWS_FAST_HYBRID_BODY_FETCH_TOPK = max(
    0,
    int(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_TOPK", "2")),
)
# 即使環境變數設得過大，混合補抓仍強制限制單篇最多 4 秒、整批最多 6 秒，
# 並限制下載大小，避免慢速串流或超大網頁拖住 GitHub Actions 數分鐘。
NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT = min(
    4.0,
    max(
        1.0,
        float(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT", "3")),
    ),
)
NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT = min(
    6.0,
    max(
        1.0,
        float(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT", "5")),
    ),
)
NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES = max(
    32768,
    min(
        524288,
        int(os.getenv("WARRANT_NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES", "131072")),
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
NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE = int(os.getenv("WARRANT_NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE", "8"))
NEWS_MULTI_SOURCE_RETURN_LIMIT = int(os.getenv(
    "WARRANT_NEWS_MULTI_SOURCE_RETURN_LIMIT",
    str(max(NEWS_MAX_ARTICLES_TO_GEMINI, NEWS_GOOGLE_MIN_USABLE_ARTICLES, 12)),
))
# 六個來源平行抓取；個別來源失敗或逾時只略過該來源。
NEWS_SOURCE_FETCH_WORKERS = max(1, int(os.getenv("WARRANT_NEWS_SOURCE_FETCH_WORKERS", "6")))
NEWS_SOURCE_BATCH_TIMEOUT = min(
    15.0,
    max(3.0, float(os.getenv("WARRANT_NEWS_SOURCE_BATCH_TIMEOUT", "9"))),
)
# 原始新聞候選短期快取與 Gemini 摘要快取分離。
# refresh_news_summary=1 只重跑 Gemini；剛抓過的六來源候選仍可重用，避免重複等待網站。
NEWS_RAW_CACHE_ENABLE = os.getenv(
    "WARRANT_NEWS_RAW_CACHE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
NEWS_RAW_CACHE_TTL_SECONDS = max(
    0,
    int(os.getenv("WARRANT_NEWS_RAW_CACHE_TTL_SECONDS", "1800")),
)
NEWS_RAW_CACHE_DIR = os.getenv(
    "WARRANT_NEWS_RAW_CACHE_DIR",
    os.path.join(LLM_CACHE_DIR, "raw_news"),
).strip() or os.path.join(LLM_CACHE_DIR, "raw_news")
NEWS_RAW_CACHE_FORCE_REFRESH = os.getenv(
    "WARRANT_NEWS_RAW_CACHE_FORCE_REFRESH",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
# 新聞最低素材／顯示目標：正常情況至少整理 2 則不同事件；
# 只有所有來源與 7/14/30 日範圍都查完後仍只有一個事件，才允許只顯示 1 則。
NEWS_MIN_DISTINCT_ARTICLES = max(1, int(os.getenv("WARRANT_NEWS_MIN_DISTINCT_ARTICLES", "2")))
# 公開資訊觀測站重大訊息補強：使用證交所 OpenAPI 的上市／上櫃每日重大訊息。
NEWS_MOPS_ENABLE = os.getenv("WARRANT_NEWS_MOPS_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")

# FinMind TaiwanStockNews 作為第六個新聞來源。
# 僅標題資料只作低優先級事實素材；不可人工補上公司名稱，也不可覆蓋其他多來源新聞。
FINMIND_NEWS_ENABLE = os.getenv(
    "WARRANT_FINMIND_NEWS_ENABLE",
    os.getenv("FINMIND_NEWS_ENABLE", "1"),
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_NEWS_WORKERS = max(1, int(os.getenv("FINMIND_NEWS_WORKERS", "8")))
FINMIND_NEWS_LOOKBACK_DAYS = max(1, int(os.getenv("FINMIND_NEWS_LOOKBACK_DAYS", "30")))
FINMIND_NEWS_LOOKBACK_STAGES = os.getenv(
    "FINMIND_NEWS_LOOKBACK_STAGES",
    "7,14,30",
).strip() or "7,14,30"
FINMIND_NEWS_REQUIRE_DIRECT_TARGET = os.getenv(
    "WARRANT_FINMIND_NEWS_REQUIRE_DIRECT_TARGET",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
NEWS_MOPS_MAX_ITEMS = max(1, int(os.getenv("WARRANT_NEWS_MOPS_MAX_ITEMS", "8")))
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
    # 至少要含一個真正的中文數字字元。只有「億、萬、十」等單位字的普通詞彙
    #（例如「億萬富翁」）不是數字，絕不可轉成 0。
    if not any(ch in _CN_NUMERAL_DIGITS for ch in t):
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
    """新聞區塊數字統一用阿拉伯數字，但只轉換真正含數字字元的量詞。

    量詞改成「明確捕捉單位」而非 lookahead，避免把「十二億元」誤讀成
    「十二億」再乘一次，也避免將「億萬富翁」中的單位字當成數字 0。
    """
    s = str(text or "")
    if not s:
        return ""

    units = r"億元|萬元|百分點|個百分點|元|億|萬|%|％|倍|天|張|季|月|年|日|檔|筆|項|座|家|人|台|套"

    def repl_percent(m):
        num = _format_chinese_numeral_number(m.group("int"), m.groupdict().get("frac"))
        return f"{num}%"

    s = re.sub(
        rf"百分之(?P<int>[{_CN_NUMERAL_CHARS}]+?)(?:點(?P<frac>[零〇○一二兩两三四五六七八九]+))?(?![{_CN_NUMERAL_CHARS}])",
        repl_percent,
        s,
    )

    s = re.sub(
        rf"第(?P<num>[{_CN_NUMERAL_CHARS}]+?)(?P<unit>季|期|屆|次)",
        lambda m: (
            f"第{_format_chinese_numeral_number(m.group('num'))}{m.group('unit')}"
            if _parse_chinese_numeral_to_int(m.group("num")) is not None
            else m.group(0)
        ),
        s,
    )

    def repl_decimal_quantity(m):
        if _parse_chinese_numeral_to_int(m.group("int")) is None:
            return m.group(0)
        return f"{_format_chinese_numeral_number(m.group('int'), m.group('frac'))}{m.group('unit')}"

    s = re.sub(
        rf"(?<![第0-9.])(?P<int>[{_CN_NUMERAL_CHARS}]+?)點"
        rf"(?P<frac>[零〇○一二兩两三四五六七八九]+?)(?P<unit>{units})",
        repl_decimal_quantity,
        s,
    )

    def repl_integer_quantity(m):
        value = _parse_chinese_numeral_to_int(m.group("num"))
        if value is None:
            return m.group(0)
        return f"{value}{m.group('unit')}"

    s = re.sub(
        rf"(?<![第0-9.])(?P<num>[{_CN_NUMERAL_CHARS}]+?)(?P<unit>{units})",
        repl_integer_quantity,
        s,
    )

    # 僅修復已存在的舊快取錯字；新流程本身不再生成這種格式。
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


def _strip_news_meta_noise(text: str) -> str:
    """移除 RSS / 網站常見的來源、日期、標題等雜訊，避免被當成新聞事實。"""
    s = _normalize_news_text(text)
    if not s:
        return ""
    s = re.sub(r'https?://\S+', '', s)
    s = re.sub(r'(?:^|[。；;])\s*(?:來源|source)[:：][^。；;]{0,80}', '。', s, flags=re.I)
    s = re.sub(r'(?:^|[。；;])\s*(?:日期|date|published)[:：][^。；;]{0,120}', '。', s, flags=re.I)
    s = re.sub(r'(?:^|[。；;])\s*標題[:：][^。；;]{0,160}', '。', s)
    s = re.sub(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT', '', s, flags=re.I)
    s = re.sub(r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b,?', '', s, flags=re.I)
    s = re.sub(r'\s*GMT\b', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[。；;]{2,}', '。', s)
    return _normalize_news_text(s).strip('。；;，, ')


def _is_price_only_news_without_fundamentals(text: str) -> bool:
    """排除只有股價漲跌、盤中變化的即時新聞；若同時含基本面資訊則保留。"""
    s = _strip_news_meta_noise(text)
    if not s:
        return False
    price_terms = [
        '即時新聞', '股價走強', '股價上漲', '股價下跌', '股價走弱', '股價震盪', '急拉', '大漲', '重挫',
        '站上', '衝上', '跌破', '漲停', '跌停', '漲幅', '跌幅', '盤中', '走強至', '上漲至'
    ]
    fundamental_terms = [
        '營收', '獲利', 'EPS', '每股純益', '毛利', '毛利率', '法說', '接單', '訂單', '產能', '擴產',
        '出貨', '評等', '目標價', '合作', '投資', '新產品', '需求', '法人', '客戶', '展望', '財報',
        '先進封裝', '液冷', '散熱', 'ASIC', 'AI', 'CoWoS', '長約'
    ]
    return any(k in s for k in price_terms) and not any(k in s for k in fundamental_terms)


def _can_use_news_title_as_fact(title: str) -> bool:
    """只有標題本身就是具體營運／產業事件時，才允許作為規則式備援素材。"""
    s = _strip_news_meta_noise(_clean_news_title(title))
    if not s:
        return False
    if _is_price_only_news_without_fundamentals(s):
        return False
    if any(k in s for k in ['完整看', '看更多', '熱門股', '焦點股', '強勢股', '盤中']) and not _has_substantive_company_news(s):
        return False
    return bool(re.search(
        r'營收|月增|月減|年增|年減|財報|獲利|EPS|毛利|毛利率|法說|接單|訂單|出貨|產能|擴產|'
        r'評等|目標價|調升|調降|合作|投資|公告|AI|ASIC|伺服器|散熱|液冷|CoWoS|先進封裝|長約|需求|供需',
        s,
        re.I,
    ))


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
    s = _strip_news_meta_noise(text)
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


def _maybe_resolve_google_news_link(url: str, request_timeout: float | None = None) -> str:
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
        timeout_value = float(NEWS_FETCH_TIMEOUT if request_timeout is None else request_timeout)
        timeout_value = max(1.0, min(float(NEWS_FETCH_TIMEOUT), timeout_value))
        r = get_thread_session().get(
            url,
            headers=headers,
            timeout=(min(5.0, timeout_value), timeout_value),
            allow_redirects=True,
        )
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
        timeout_value = float(NEWS_FETCH_TIMEOUT if request_timeout is None else request_timeout)
        timeout_value = max(1.0, timeout_value)
        if hard_deadline_seconds is not None:
            timeout_value = min(timeout_value, max(1.0, float(hard_deadline_seconds)))
        final_url = _maybe_resolve_google_news_link(url, request_timeout=timeout_value)
        headers = {
            "User-Agent": HDR["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://news.google.com/",
        }

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


def _taipei_now_naive() -> datetime:
    """取得台北目前時間並移除時區資訊，供既有 naive datetime 比較使用。"""
    return datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)


def _parse_rss_pub_date(pub_date: str):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _is_within_recent_days_from_rss(pub_date: str, days: int = 7) -> bool:
    dt = _parse_rss_pub_date(pub_date)
    if dt is None:
        return True
    return dt >= _taipei_now_naive() - timedelta(days=days)



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


def _news_published_datetime(published: str):
    """將 RSS／MOPS 日期轉成 datetime；失敗時回傳 None。"""
    raw = str(published or "").strip()
    if not raw:
        return None
    dt = _parse_rss_pub_date(raw)
    if dt is not None:
        return dt
    try:
        ts = pd.Timestamp(raw)
        if pd.isna(ts):
            return None
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert("Asia/Taipei").tz_localize(None)
        return ts.to_pydatetime()
    except Exception:
        return None


def _news_month_period_key(text: str, published: str = "") -> str:
    """擷取新聞中的年月；只有月份時，使用發布年份補齊。"""
    s = _normalize_news_text(text)
    published_dt = _news_published_datetime(published)

    # 西元年月，例如 2026年6月、2026/06、2026-06。
    # 不允許 145.28 這類營收小數被誤判成民國年月。
    m = re.search(
        r"(?<!\d)(?P<year>20\d{2})\s*(?:年|[/\-])\s*0?(?P<month>1[0-2]|[1-9])(?:\s*月)?(?!\d)(?!\s*[/\-]\s*\d{1,2})",
        s,
    )
    if m:
        year = int(m.group("year"))
        month = int(m.group("month"))
        return f"{year:04d}-{month:02d}"

    # 民國年月只接受明確的「115年6月」，避免三位數金額被誤判。
    m = re.search(
        r"(?<!\d)(?P<year>1\d{2})\s*年\s*0?(?P<month>1[0-2]|[1-9])\s*月",
        s,
    )
    if m:
        year = int(m.group("year")) + 1911
        month = int(m.group("month"))
        return f"{year:04d}-{month:02d}"

    # 一般新聞最常見的「6月營收」。
    m = re.search(r"(?<!\d)0?(?P<month>1[0-2]|[1-9])\s*月", s)
    if m:
        month = int(m.group("month"))
        year = published_dt.year if published_dt is not None else 0
        return f"{year:04d}-{month:02d}" if year else f"month-{month:02d}"

    return ""


def _news_quarter_period_key(text: str, published: str = "") -> str:
    """擷取新聞中的年度季度，例如 2026年第2季、Q2。"""
    s = _normalize_news_text(text)
    published_dt = _news_published_datetime(published)

    year_match = re.search(r"(?<!\d)(20\d{2}|1\d{2})\s*年", s)
    year = 0
    if year_match:
        year = int(year_match.group(1))
        if year < 1911:
            year += 1911
    elif published_dt is not None:
        year = published_dt.year

    quarter_match = re.search(r"第?\s*([1-4])\s*季", s)
    if not quarter_match:
        quarter_match = re.search(r"\bQ([1-4])\b", s, flags=re.I)
    if not quarter_match:
        return ""

    quarter = int(quarter_match.group(1))
    return f"{year:04d}-Q{quarter}" if year else f"Q{quarter}"


def _news_event_numeric_signature(text: str, limit: int = 5) -> str:
    """抽取事件中的主要數字，供非月營收事件做保守去重。"""
    s = _normalize_news_text(text).replace("％", "%")
    tokens = re.findall(
        r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?:%|億元|億|萬元|萬|元|季|年|月)?",
        s,
    )
    unique = []
    for token in tokens:
        token = str(token or "").strip()
        if not token or token in unique:
            continue
        unique.append(token)
        if len(unique) >= max(1, int(limit)):
            break
    return "|".join(unique)


def _news_event_fingerprint(
    text: str,
    published: str = "",
    stock_code: str = "",
    stock_name: str = "",
) -> str:
    """建立新聞事件鍵。

    這裡刻意把「同一月份營收」視為同一事件，不論新聞來源、標題、
    status 或 detail 的寫法是否不同；其他題材採較保守鍵值，避免誤合併。
    """
    s = _normalize_news_text(text)
    if not s:
        return ""

    lower = s.lower()
    month_period = _news_month_period_key(s, published=published)
    quarter_period = _news_quarter_period_key(s, published=published)
    published_dt = _news_published_datetime(published)
    published_day = published_dt.strftime("%Y-%m-%d") if published_dt is not None else "unknown"
    numeric_sig = _news_event_numeric_signature(s)

    # 最重要的修正：月營收多來源／多標題只能算一個事件。
    if "營收" in s:
        period = month_period or quarter_period
        if period:
            return f"revenue:{period}"
        return f"revenue:{published_day}:{numeric_sig or 'no-number'}"

    financial_terms = ["財報", "每股盈餘", "eps", "毛利率", "營業利益", "稅後淨利", "獲利"]
    if any(term in lower for term in financial_terms):
        period = quarter_period or month_period or published_day
        return f"earnings:{period}:{numeric_sig or 'no-number'}"

    if any(term in lower for term in ["目標價", "評等", "升評", "降評", "買進評等", "中立評等"]):
        return f"rating:{published_day}:{numeric_sig or _title_compare_text(s)[:40]}"

    event_families = [
        ("dividend", ["股利", "配息", "除息", "除權"]),
        ("board", ["董事會", "股東會", "決議"]),
        ("guidance", ["法說", "展望", "財測"]),
        ("capacity", ["擴產", "新產能", "量產", "新廠"]),
        ("order", ["接單", "訂單", "合約", "標案"]),
        ("product", ["新品", "新產品", "新平台"]),
        ("etf_index", ["etf", "指數", "成分股", "被動資金"]),
    ]
    for family, keywords in event_families:
        if any(keyword in lower for keyword in keywords):
            compact = _title_compare_text(s)
            for removable in [stock_code, stock_name, "新聞", "即時", "公告", "MoneyDJ理財網"]:
                if removable:
                    compact = compact.replace(_title_compare_text(removable), "")
            anchor = re.sub(r"\d+(?:\.\d+)?", "", compact)[:36]
            return f"{family}:{published_day}:{numeric_sig}:{anchor}"

    compact = _title_compare_text(s)
    for removable in [stock_code, stock_name, "新聞", "即時", "公告", "MoneyDJ理財網"]:
        if removable:
            compact = compact.replace(_title_compare_text(removable), "")
    return f"text:{compact[:120]}" if compact else ""


def _news_article_event_key(article: dict, stock_code: str = "", stock_name: str = "") -> str:
    if not isinstance(article, dict):
        return ""
    existing = str(article.get("event_key", "") or "").strip()
    if existing:
        return existing
    combined = _normalize_news_text(
        "。".join([
            str(article.get("title", "") or ""),
            str(article.get("description", "") or ""),
            str(article.get("content", article.get("body", "")) or ""),
        ])
    )
    return _news_event_fingerprint(
        combined,
        published=str(article.get("published", "") or ""),
        stock_code=stock_code,
        stock_name=stock_name,
    )


def _dedupe_news_articles_by_event(
    articles: List[dict],
    stock_code: str = "",
    stock_name: str = "",
    limit: int = 0,
    log_label: str = "新聞素材",
) -> List[dict]:
    """依事件去重並保留原排序中的最佳文章。"""
    result = []
    seen_events = set()
    seen_titles = set()
    removed = 0

    for raw_article in articles or []:
        article = dict(raw_article or {})
        title_key = _article_seen_key(article.get("title", ""), article.get("url", ""))
        event_key = _news_article_event_key(article, stock_code, stock_name)
        if (title_key and title_key in seen_titles) or (event_key and event_key in seen_events):
            removed += 1
            continue
        if title_key:
            seen_titles.add(title_key)
        if event_key:
            seen_events.add(event_key)
            article["event_key"] = event_key
        result.append(article)
        if limit > 0 and len(result) >= limit:
            break

    if removed > 0:
        print(f"🧹 {log_label}事件去重：移除 {removed} 篇重複事件｜保留 {len(result)} 篇")
    return result


def _news_point_event_key(
    point: str,
    stock_code: str = "",
    stock_name: str = "",
    source_article: dict | None = None,
) -> str:
    """建立輸出新聞點的事件鍵；優先依 point 本身判斷，再參考來源文章。"""
    fields = _parse_report_point_fields(point)
    point_text = _normalize_news_text(
        "。".join([
            str(fields.get("label", "") or ""),
            str(fields.get("status", "") or ""),
            str(fields.get("detail", "") or ""),
        ])
    )
    point_key = _news_event_fingerprint(
        point_text,
        stock_code=stock_code,
        stock_name=stock_name,
    )

    source_key = ""
    if isinstance(source_article, dict):
        source_key = _news_article_event_key(source_article, stock_code, stock_name)

    # point 常只有「6月」而沒有年份；來源文章帶有發布日期，可補成 2026-06。
    # 只有事件家族一致時才採來源鍵，避免一篇多事件文章把不同題材誤合併。
    if point_key and source_key:
        point_family = point_key.split(":", 1)[0]
        source_family = source_key.split(":", 1)[0]
        if point_family == source_family and point_family != "text":
            return source_key

    if point_key and not point_key.startswith("text:"):
        return point_key
    if source_key:
        return source_key
    return point_key


def _dedupe_news_points_by_event(
    points: List[str],
    stock_code: str = "",
    stock_name: str = "",
    log_label: str = "新聞重點",
) -> List[str]:
    """最終新聞顯示前的事件級防線；同月份營收最多保留一點。"""
    result = []
    seen_text = set()
    seen_events = set()
    removed = 0

    for raw_point in points or []:
        point = str(raw_point or "").strip()
        text_key = _title_compare_text(point)
        event_key = _news_point_event_key(point, stock_code, stock_name)
        if not point or not text_key:
            continue
        if text_key in seen_text or (event_key and event_key in seen_events):
            removed += 1
            continue
        seen_text.add(text_key)
        if event_key:
            seen_events.add(event_key)
        result.append(point)

    if removed > 0:
        print(f"🧹 {log_label}事件去重：移除 {removed} 個重複事件｜保留 {len(result)} 點")
    return result


def _build_fast_rss_news_content(title: str, description: str, source: str = "", published: str = "") -> str:
    """極速模式用：只保留可讀摘要，不把標題／來源／日期混進內文，避免 Gemini 直接複製標題。"""
    title = _clean_news_title(title)
    description = _strip_news_meta_noise(description)

    if description and title and _char_overlap_ratio(description, title) >= 0.80:
        description = ""
    if description and _looks_like_news_headline(description, title):
        description = ""
    return _normalize_news_text(description)


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

    # 公司名或代號必須出現在標題或正文前 200 字；只在文末順帶提到的不收。
    head = _normalize_news_text(f"{title} {content[:200]}")
    if not _news_text_matches_target_stock(head, stock_code, stock_name):
        return False

    # 正文出現 3 個以上其他四碼代號時，視為產業綜述候選；
    # 除非標題明確包含本公司，否則排除。年份不列入代號計數。
    target_code = _clean_code(stock_code)
    other_codes = set()
    for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", content):
        if code == target_code:
            continue
        try:
            if 1900 <= int(code) <= 2099:
                continue
        except Exception:
            pass
        other_codes.add(code)
    if len(other_codes) >= 3 and not _news_text_matches_target_stock(title, stock_code, stock_name):
        return False

    if _is_price_only_news_without_fundamentals(combined):
        return False
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

    feed_results = []
    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as executor:
        future_map = {
            executor.submit(_read_rss_items, url, family, max(20, max_items * 3)): family
            for family, url in urls
        }
        for future in as_completed(future_map):
            try:
                feed_results.extend(future.result() or [])
            except Exception as exc:
                print(f"⚠️ {future_map[future]} RSS 工作失敗：{exc}")

    for item in feed_results:
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
    for query in [stock_name, stock_code]:
        query = str(query or "").strip()
        if query and query not in queries:
            queries.append(query)
    headers = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def fetch_query(query: str) -> List[dict]:
        url = "https://www.moneydj.com/kmdj/search/list.aspx?" + urllib.parse.urlencode({
            "_QueryType_": "NW",
            "_Query_": query,
        })
        try:
            response = get_thread_session().get(
                url,
                headers=headers,
                timeout=(4, max(2.0, NEWS_FETCH_TIMEOUT)),
            )
            response.raise_for_status()
            return _extract_moneydj_search_candidates(response.text, stock_code, stock_name)
        except Exception as exc:
            print(f"⚠️ MoneyDJ 新聞搜尋失敗：{query}｜{exc}")
            return []

    candidates = []
    seen = set()
    with ThreadPoolExecutor(max_workers=max(1, min(2, len(queries)))) as executor:
        future_map = {executor.submit(fetch_query, query): query for query in queries}
        for future in as_completed(future_map):
            for candidate in future.result() or []:
                key = _article_seen_key(candidate.get("title", ""), candidate.get("url", ""))
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                candidates.append(candidate)

    articles = []
    max_days = max(_get_news_search_day_list())
    candidate_limit = max(max_items * 2, max_items, NEWS_SUMMARY_MAX_POINTS)
    for candidate in candidates[:candidate_limit]:
        title = candidate.get("title", "")
        description = _normalize_news_text(candidate.get("description", ""))
        fallback_ok = _passes_news_quality_gate(title, description, stock_code, stock_name)
        if not fallback_ok:
            continue
        content = _build_fast_rss_news_content(
            title,
            description,
            source="MoneyDJ",
            published=candidate.get("published", ""),
        )
        article = {
            **candidate,
            "content": content,
            "body_ok": False,
            "fallback_ok": True,
            "content_source": "moneydj_search",
            "body_length": 0,
            "search_days": int(max_days),
            "query_stage": "MoneyDJ關鍵字搜尋",
        }
        article["relevance_score"] = _score_news_article_relevance(article, stock_code, stock_name)
        articles.append(article)
    articles = sorted(articles, key=lambda article: -int(article.get("relevance_score", 0) or 0))
    print(f"📰 MoneyDJ 新聞搜尋：{stock_code} {stock_name}｜保留 {len(articles):,} 筆｜前段不抓原文")
    return articles[:max(1, int(max_items))]




def _news_article_usable(article: dict) -> bool:
    return bool(article and (article.get("body_ok") or article.get("fallback_ok")))


def _count_distinct_usable_news_articles(
    articles: List[dict],
    stock_code: str = "",
    stock_name: str = "",
) -> int:
    seen = set()
    for article in articles or []:
        if not _news_article_usable(article):
            continue
        key = _news_article_event_key(article, stock_code, stock_name)
        if not key:
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
    """從公開資訊觀測站每日重大訊息補充公司直接公告；兩市場平行讀取。"""
    if not NEWS_MULTI_SOURCE_ENABLE or not NEWS_MOPS_ENABLE:
        return []
    stock_key = _clean_code(stock_code)
    max_days = max(_get_news_search_day_list())
    cutoff = _taipei_now_naive() - timedelta(days=max_days)
    articles = []
    seen = set()
    headers = {
        "User-Agent": HDR["User-Agent"],
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }

    def fetch_endpoint(market_name: str, url: str):
        try:
            response = get_thread_session().get(
                url,
                headers=headers,
                timeout=(4, max(2.0, NEWS_FETCH_TIMEOUT)),
            )
            response.raise_for_status()
            data = response.json()
            return market_name, data if isinstance(data, list) else []
        except Exception as exc:
            print(f"⚠️ MOPS {market_name}讀取失敗：{exc}")
            return market_name, []

    endpoint_results = []
    with ThreadPoolExecutor(max_workers=min(2, len(NEWS_MOPS_ENDPOINTS))) as executor:
        future_map = {
            executor.submit(fetch_endpoint, market_name, url): market_name
            for market_name, url in NEWS_MOPS_ENDPOINTS
        }
        for future in as_completed(future_map):
            endpoint_results.append(future.result())

    for market_name, data in endpoint_results:
        for row in data:
            if not isinstance(row, dict):
                continue
            row_code = _clean_code(row.get("公司代號", row.get("公司代碼", "")))
            if row_code != stock_key:
                continue
            title = _clean_news_title(row.get("主旨", row.get("公告主旨", "")))
            detail = _normalize_news_text(row.get("說明", row.get("公告內容", "")))
            speech_date = row.get("發言日期", row.get("公告日期", row.get("事實發生日", "")))
            published_dt = _parse_mops_date(speech_date)
            if published_dt is not None and published_dt < cutoff:
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
            published = published_dt.strftime("%Y-%m-%d") if published_dt else str(speech_date or "")
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
        parsed = _parse_rss_pub_date(article.get("published", ""))
        if parsed is None:
            try:
                parsed = pd.Timestamp(article.get("published", "")).to_pydatetime()
            except Exception:
                parsed = datetime.min
        return (
            -int(article.get("relevance_score", 0) or 0),
            -parsed.timestamp() if parsed != datetime.min else 0,
        )

    articles = sorted(articles, key=sort_key)
    print(f"📰 MOPS 重大訊息：{stock_code} {stock_name}｜保留 {len(articles):,} 筆")
    return articles[:max(1, int(max_items))]




def _finmind_fetch_news_day(stock_code: str, trade_date) -> pd.DataFrame:
    """依 FinMind 官方逐日限制取得單日 TaiwanStockNews。"""
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    return _finmind_get_data(
        "TaiwanStockNews",
        data_id=_normalize_stock_name_code_key(stock_code),
        start_date=date_s,
        allow_empty=True,
    )


def fetch_finmind_news_articles(stock_code: str, stock_name: str, max_items: int = 10) -> List[dict]:
    """漸進式取得 FinMind TaiwanStockNews，避免每次固定發出 30 個日期請求。"""
    if not NEWS_ENABLE or not FINMIND_NEWS_ENABLE:
        return []

    code = _normalize_stock_name_code_key(stock_code)
    today = get_taipei_today_ts()

    stages = []
    for token in re.split(r"[,，;；\s]+", FINMIND_NEWS_LOOKBACK_STAGES):
        try:
            value = int(token)
        except Exception:
            continue
        if value > 0:
            stages.append(min(value, FINMIND_NEWS_LOOKBACK_DAYS))
    stages.append(FINMIND_NEWS_LOOKBACK_DAYS)
    stages = sorted(set(value for value in stages if value > 0))

    frames = []
    failures = []
    queried_days = set()
    minimum_needed = max(1, int(NEWS_MIN_DISTINCT_ARTICLES))

    def convert_frames_to_articles(search_days: int) -> List[dict]:
        raw = pd.concat(frames, ignore_index=True, sort=False).fillna("") if frames else pd.DataFrame()
        if raw.empty:
            return []
        required = {"date", "stock_id", "link", "source", "title"}
        missing = required - set(raw.columns)
        if missing:
            _finmind_debug_print_df(f"TaiwanStockNews 必要欄位不足｜{code}", raw)
            print(
                f"⚠️ FinMind TaiwanStockNews 必要欄位不足：{sorted(missing)}｜"
                f"實際欄位={raw.columns.tolist()}｜本來源略過"
            )
            return []

        description_col = next(
            (column for column in ["description", "content", "summary", "snippet", "text"] if column in raw.columns),
            "",
        )
        raw = raw.copy()
        raw["published_dt"] = pd.to_datetime(raw["date"], errors="coerce")
        raw = raw.dropna(subset=["published_dt"]).sort_values("published_dt", ascending=False)

        articles = []
        seen = set()
        for _, row in raw.iterrows():
            title = _clean_news_title(row.get("title", ""))
            raw_description = row.get(description_col, "") if description_col else ""
            description = _normalize_news_text(_html_to_readable_text(raw_description))
            url = str(row.get("link", "") or "").strip()
            source = str(row.get("source", "") or "FinMind").strip() or "FinMind"
            if not title and not description:
                continue

            row_stock_id = _normalize_stock_name_code_key(row.get("stock_id", ""))
            if row_stock_id and row_stock_id != code:
                continue
            source_text = _normalize_news_text("。".join(part for part in [title, description] if part))
            if _has_conflicting_similar_company_name(source_text, code, stock_name):
                continue
            if FINMIND_NEWS_REQUIRE_DIRECT_TARGET and not _news_text_matches_target_stock(
                source_text,
                code,
                stock_name,
            ):
                continue

            key = _article_seen_key(title, url)
            if key and key in seen:
                continue
            if key:
                seen.add(key)

            fact_text = _normalize_news_text(description or title)
            if not fact_text:
                continue
            has_description = bool(description)
            article = {
                "title": title or fact_text[:80],
                "url": url,
                "source": source,
                "source_family": "FinMind",
                "published": pd.Timestamp(row["published_dt"]).strftime("%Y-%m-%d %H:%M:%S"),
                "description": description,
                "content": fact_text,
                "body_ok": bool(has_description and len(fact_text) >= 80),
                "fallback_ok": True,
                "content_source": "finmind_description" if has_description else "finmind_title_fact",
                "finmind_title_only": not has_description,
                "finmind_target_verified": True,
                "search_days": int(search_days),
                "query_stage": "FinMind TaiwanStockNews",
                "body_length": len(fact_text),
            }
            article["relevance_score"] = _score_news_article_relevance(article, code, stock_name)
            articles.append(article)

        def sort_key(article: dict):
            published_dt = pd.to_datetime(article.get("published", ""), errors="coerce")
            timestamp = float(published_dt.timestamp()) if pd.notna(published_dt) else 0.0
            material_rank = 0 if article.get("body_ok") else 1
            return (material_rank, -int(article.get("relevance_score", 0) or 0), -timestamp)

        articles = sorted(articles, key=sort_key)
        return articles[:max(1, int(NEWS_MULTI_SOURCE_RETURN_LIMIT), int(max_items))]

    result = []
    for stage_days in stages:
        new_dates = [
            today - pd.Timedelta(days=offset)
            for offset in range(stage_days)
            if offset not in queried_days
        ]
        queried_days.update(range(stage_days))
        if new_dates:
            with ThreadPoolExecutor(max_workers=min(FINMIND_NEWS_WORKERS, len(new_dates))) as executor:
                future_map = {
                    executor.submit(_finmind_fetch_news_day, code, day): day
                    for day in new_dates
                }
                for future in as_completed(future_map):
                    day = future_map[future]
                    try:
                        day_df = future.result()
                        if day_df is not None and not day_df.empty:
                            frames.append(day_df)
                    except Exception as exc:
                        failures.append((str(pd.Timestamp(day).date()), str(exc)))

        result = convert_frames_to_articles(stage_days)
        distinct_count = _count_distinct_usable_news_articles(result, code, stock_name)
        print(
            f"📰 FinMind TaiwanStockNews 漸進查詢：{code} {stock_name}｜"
            f"近 {stage_days} 天｜保留 {len(result):,} 筆｜不同事件 {distinct_count:,} 則"
        )
        if distinct_count >= minimum_needed:
            break

    if failures:
        print(
            f"⚠️ FinMind 新聞部分日期失敗：{len(failures)}/{len(queried_days)}｜"
            + "；".join(f"{day}:{error[:80]}" for day, error in failures[:3])
        )
    if not result:
        print(f"ℹ️ FinMind TaiwanStockNews 無近期合格新聞：{code} {stock_name}")
        return []

    title_only_count = sum(bool(article.get("finmind_title_only")) for article in result)
    searched_days = max((int(article.get("search_days", 0) or 0) for article in result), default=0)
    print(
        f"📰 FinMind TaiwanStockNews：{code} {stock_name}｜"
        f"實際查至近 {searched_days} 天｜保留 {len(result):,} 筆｜僅標題 {title_only_count:,} 筆"
    )
    return result


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

    def sort_key(article: dict):
        published_dt = _parse_rss_pub_date(article.get("published", "")) or datetime.min
        if article.get("body_ok"):
            material_rank = 0
        elif article.get("fallback_ok") and not article.get("finmind_title_only"):
            material_rank = 1
        elif article.get("finmind_title_only"):
            material_rank = 2
        else:
            material_rank = 3

        source_family = str(article.get("source_family", article.get("source", "")) or "")
        official_rank = 0 if source_family == "MOPS" else 1
        return (
            material_rank,
            official_rank,
            -int(article.get("relevance_score", 0) or 0),
            -published_dt.timestamp() if published_dt != datetime.min else 0,
        )

    merged = sorted(merged, key=sort_key)
    # 先依事件去重，再做來源多樣化；避免同一月份營收因來自 Google／Yahoo／MoneyDJ
    # 而各占一個名額，最後送進 Gemini 的其實仍是同一件事。
    merged = _dedupe_news_articles_by_event(
        merged,
        stock_code,
        stock_name,
        log_label="多來源新聞",
    )

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



def _news_raw_cache_signature() -> str:
    payload = {
        "schema": "v24_parallel_sources",
        "finmind": FINMIND_NEWS_ENABLE,
        "multi": NEWS_MULTI_SOURCE_ENABLE,
        "yahoo": NEWS_YAHOO_RSS_ENABLE,
        "bing": NEWS_BING_RSS_ENABLE,
        "moneydj": NEWS_MONEYDJ_SEARCH_ENABLE,
        "mops": NEWS_MOPS_ENABLE,
        "google_max": NEWS_GOOGLE_MAX_ITEMS,
        "google_scan": NEWS_GOOGLE_SCAN_MULTIPLIER,
        "external_max": NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE,
        "return_limit": NEWS_MULTI_SOURCE_RETURN_LIMIT,
        "finmind_stages": FINMIND_NEWS_LOOKBACK_STAGES,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _news_raw_cache_path(stock_code: str) -> str:
    date_key = _compact_date_key(_taipei_today_str())
    filename = f"{date_key}_{_safe_cache_part(stock_code)}_{_news_raw_cache_signature()}.json"
    return os.path.join(NEWS_RAW_CACHE_DIR, filename)


def _load_news_raw_cache(stock_code: str, stock_name: str) -> List[dict]:
    if not NEWS_RAW_CACHE_ENABLE or NEWS_RAW_CACHE_FORCE_REFRESH or NEWS_RAW_CACHE_TTL_SECONDS <= 0:
        return []
    path = _news_raw_cache_path(stock_code)
    try:
        if not os.path.exists(path):
            return []
        age_seconds = max(0.0, time.time() - os.path.getmtime(path))
        if age_seconds > NEWS_RAW_CACHE_TTL_SECONDS:
            return []
        with open(path, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        if not isinstance(articles, list) or not articles:
            return []
        print(
            f"⚡ 六來源原始新聞短期快取命中：{stock_code} {stock_name}｜"
            f"{len(articles):,} 筆｜age={age_seconds:.0f}秒"
        )
        return [dict(article) for article in articles if isinstance(article, dict)]
    except Exception as exc:
        print(f"⚠️ 六來源原始新聞快取讀取失敗：{stock_code}｜{exc}")
        return []


def _save_news_raw_cache(stock_code: str, stock_name: str, articles: List[dict]):
    if not NEWS_RAW_CACHE_ENABLE or not articles:
        return
    path = _news_raw_cache_path(stock_code)
    try:
        _ensure_dir(os.path.dirname(path))
        payload = {
            "stock_code": str(stock_code),
            "stock_name": str(stock_name),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "articles": [dict(article) for article in articles if isinstance(article, dict)],
        }
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as cache_file:
            json.dump(payload, cache_file, ensure_ascii=False)
        os.replace(temp_path, path)
        print(f"💾 六來源原始新聞短期快取已更新：{stock_code}｜{len(articles):,} 筆")
    except Exception as exc:
        print(f"⚠️ 六來源原始新聞快取寫入失敗：{stock_code}｜{exc}")


def fetch_multi_source_news_articles(stock_code: str, stock_name: str, max_items: int = 10, cancel_event: threading.Event | None = None) -> List[dict]:
    """平行合併六個新聞來源；短期快取避免同日手動重跑反覆等待網站。"""
    if cancel_event is not None and cancel_event.is_set():
        print(f"🛑 新聞管線已取消：{stock_code}")
        return []
    manual = os.getenv("WEEKLY_NEWS_TEXT", "").strip()
    if manual:
        return fetch_google_news_articles(stock_code, stock_name, max_items=max_items)
    if not NEWS_ENABLE:
        return []

    cached_articles = _load_news_raw_cache(stock_code, stock_name)
    if cached_articles:
        return cached_articles

    minimum_needed = max(1, int(NEWS_MIN_DISTINCT_ARTICLES))
    per_source = max(minimum_needed, 3, int(NEWS_EXTERNAL_MAX_ITEMS_PER_SOURCE))
    jobs = [
        (
            "GoogleNews",
            fetch_google_news_articles,
            (stock_code, stock_name, max(max_items, minimum_needed)),
        ),
    ]
    if FINMIND_NEWS_ENABLE:
        jobs.append(("FinMind", fetch_finmind_news_articles, (stock_code, stock_name, per_source)))
    if NEWS_MULTI_SOURCE_ENABLE:
        if NEWS_YAHOO_RSS_ENABLE:
            jobs.append(("Yahoo", fetch_yahoo_finance_rss_articles, (stock_code, stock_name, per_source)))
        if NEWS_BING_RSS_ENABLE:
            jobs.append(("Bing", fetch_bing_news_rss_articles, (stock_code, stock_name, per_source)))
        if NEWS_MONEYDJ_SEARCH_ENABLE:
            jobs.append(("MoneyDJ", fetch_moneydj_news_articles, (stock_code, stock_name, per_source)))
        if NEWS_MOPS_ENABLE:
            jobs.append(("MOPS", fetch_mops_material_info_articles, (stock_code, stock_name, max(NEWS_MOPS_MAX_ITEMS, per_source))))

    groups = []
    completed_sources = []
    started = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=min(NEWS_SOURCE_FETCH_WORKERS, len(jobs)))
    future_map = {
        executor.submit(function, *arguments): label
        for label, function, arguments in jobs
    }
    pending = set(future_map)
    deadline = started + NEWS_SOURCE_BATCH_TIMEOUT
    try:
        while pending:
            if cancel_event is not None and cancel_event.is_set():
                print(f"🛑 六來源新聞抓取收到取消訊號：{stock_code}")
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=min(0.25, max(0.05, remaining)),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                label = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"⚠️ 新聞來源工作失敗：{label}｜{exc}")
                    result = []
                groups.append(result or [])
                completed_sources.append(label)
    finally:
        if pending:
            pending_labels = [future_map[future] for future in pending]
            for future in pending:
                future.cancel()
            print(
                f"⚠️ 六來源新聞整批超過 {NEWS_SOURCE_BATCH_TIMEOUT:g} 秒，"
                f"略過尚未完成來源：{'、'.join(pending_labels)}"
            )
        executor.shutdown(wait=False, cancel_futures=True)

    limit = max(
        minimum_needed,
        NEWS_SUMMARY_MAX_POINTS,
        NEWS_GOOGLE_MIN_USABLE_ARTICLES,
        min(max(1, int(NEWS_MULTI_SOURCE_RETURN_LIMIT)), max(minimum_needed, int(max_items))),
    )
    articles = _merge_and_rank_news_articles(groups, stock_code, stock_name, limit=limit)
    distinct_count = _count_distinct_usable_news_articles(articles, stock_code, stock_name)
    source_counts = {}
    for article in articles:
        family = str(article.get("source_family", article.get("source", "unknown")) or "unknown")
        source_counts[family] = source_counts.get(family, 0) + 1
    source_text = "、".join(f"{key}:{value}" for key, value in source_counts.items()) or "無"
    elapsed = time.perf_counter() - started

    print(
        f"📰 六來源平行抓取完成：{stock_code} {stock_name}｜{elapsed:.2f} 秒｜"
        f"完成來源={'、'.join(completed_sources) or '無'}｜保留 {len(articles):,} 筆｜"
        f"不同事件 {distinct_count:,} 則｜來源 {source_text}"
    )
    if distinct_count < minimum_needed:
        print(
            f"⚠️ 六來源僅取得 {distinct_count:,} 則不同合格事件；"
            "允許單則輸出，不以盤勢或重複事件硬湊"
        )
    if cancel_event is not None and cancel_event.is_set():
        return []
    _save_news_raw_cache(stock_code, stock_name, articles)
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
        usable_count = usable_count_now()
        distinct_needed = min(
            max(1, int(NEWS_MIN_DISTINCT_ARTICLES)),
            max(1, int(required_usable_count)),
        )
        distinct_count = _count_distinct_usable_news_articles(
            all_articles,
            stock_code,
            stock_name,
        )
        return usable_count >= required_usable_count and distinct_count >= distinct_needed

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
                r = requests.get(url, headers={"User-Agent": HDR["User-Agent"]}, timeout=(4, max(2.0, NEWS_FETCH_TIMEOUT)))
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
    all_articles = _dedupe_news_articles_by_event(
        all_articles,
        stock_code,
        stock_name,
        limit=fetch_limit,
        log_label="Google News",
    )
    return all_articles[:fetch_limit]

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
    s = _strip_news_meta_noise(sentence)
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
    tone = _extract_report_tone_from_point(text)
    s = _strip_report_tone_metadata(text)
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
    # 圖卡前清理若需要縮短，優先回退到上限內最後一個完整句號；
    # 找不到完整句才交由既有分句／硬切備援，避免「第2季。」「需留。」這類殘句。
    if len(s) > max_len:
        prefix = s[:max_len]
        sentence_idx = max(prefix.rfind("。"), prefix.rfind("！"), prefix.rfind("？"))
        if sentence_idx >= 0:
            s = prefix[:sentence_idx + 1].strip()
    trimmed = _finish_complete_summary_point(s, max_len=max_len, min_cut_len=1)
    return _replace_report_point_tone(trimmed, tone) if trimmed and tone else trimmed

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

def _count_summary_chars(points: List[str]) -> int:
    """計算重點實際文字量；tone 為控制欄位，不計入圖卡內容字數。"""
    joined = "".join(_strip_report_tone_metadata(str(p or "")) for p in points or [])
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

    points = []
    for item in raw_points:
        point = _point_item_to_canonical_text(item)
        if point:
            points.append(point)
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

def _parse_gemini_news_points(
    output_text: str,
    usable_articles: List[dict],
    stock_code: str,
    stock_name: str,
) -> Tuple[List[str], List[dict]]:
    """解析 Gemini 新聞物件；先驗證 evidence，再送入既有清理流程。"""
    parsed = _extract_json_from_text(output_text)
    if isinstance(parsed, dict):
        raw_items = parsed.get("points", [])
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = None

    # 合法空陣列代表本週沒有直接相關重大新聞，不算錯誤。
    if raw_items == []:
        return [], []
    if not isinstance(raw_items, list):
        rejected = []
        if str(output_text or "").strip():
            rejected.append(
                _format_rejected_news_point(
                    "",
                    "Gemini 輸出不是合法的 points 物件陣列",
                )
            )
        return [], rejected

    points = []
    rejected = []
    seen = set()
    seen_events = set()
    article_by_id = {
        str(article.get("id", "") or "").strip(): article
        for article in usable_articles or []
        if str(article.get("id", "") or "").strip()
    }
    for item in raw_items:
        working_item = _repair_news_item_number_units_locally(item, usable_articles)
        working_item = _repair_news_item_evidence_locally(
            working_item,
            usable_articles,
            stock_code,
            stock_name,
        )
        ok, reason = _validate_news_point_evidence(
            working_item,
            usable_articles,
            stock_code,
            stock_name,
        )
        if not ok:
            rejected.append(_format_rejected_news_point(working_item, reason))
            continue

        canonical = _point_item_to_canonical_text(working_item)
        cleaned = _clean_news_summary_points_for_stock(
            [canonical],
            stock_code,
            stock_name,
        )
        if not cleaned:
            rejected.append(
                _format_rejected_news_point(
                    item,
                    "point 未通過既有新聞內容清理",
                )
            )
            continue

        point = _trim_news_point_detail_to_max(cleaned[0], log_label="Gemini 新聞")
        point = _enrich_short_news_point_detail(point, log_label="Gemini 新聞")
        point = _trim_news_point_detail_to_max(point, log_label="Gemini 新聞")
        if not _points_are_independent_and_complete([point]):
            rejected.append(
                _format_rejected_news_point(
                    item,
                    "point 不是可獨立閱讀的完整句",
                )
            )
            continue

        key = _title_compare_text(point)
        source_id = str(working_item.get("source_id", "") or "").strip() if isinstance(working_item, dict) else ""
        event_key = _news_point_event_key(
            point,
            stock_code,
            stock_name,
            source_article=article_by_id.get(source_id),
        )
        if not key or key in seen:
            continue
        if event_key and event_key in seen_events:
            print(
                f"🧹 Gemini 新聞事件去重：略過同一事件的重複點｜"
                f"source_id={source_id or '-'}｜event_key={event_key}"
            )
            continue
        points.append(point)
        seen.add(key)
        if event_key:
            seen_events.add(event_key)
        if len(points) >= NEWS_SUMMARY_MAX_POINTS:
            break

    return points, rejected



def _extract_validated_news_source_ids(
    output_text: str,
    usable_articles: List[dict],
    stock_code: str,
    stock_name: str,
) -> List[str]:
    """取得已通過 source_id、evidence 與公司主體驗證的來源編號。"""
    parsed = _extract_json_from_text(output_text)
    raw_items = parsed.get("points", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
    source_ids = []
    for item in raw_items or []:
        working_item = _repair_news_item_number_units_locally(item, usable_articles)
        working_item = _repair_news_item_evidence_locally(
            working_item,
            usable_articles,
            stock_code,
            stock_name,
        )
        ok, _ = _validate_news_point_evidence(
            working_item,
            usable_articles,
            stock_code,
            stock_name,
        )
        if not ok or not isinstance(working_item, dict):
            continue
        source_id = str(working_item.get("source_id", "") or "").strip()
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
    return source_ids


def _count_nonspace_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _slice_by_nonspace_chars(text: str, max_chars: int) -> str:
    """依不含空白的字數切出前段，避免中文與空白混排時長度計算不一致。"""
    s = str(text or "")
    if max_chars <= 0:
        return ""
    count = 0
    for idx, ch in enumerate(s):
        if not ch.isspace():
            count += 1
        if count >= max_chars:
            return s[:idx + 1]
    return s


def _trim_news_detail_text(detail: str, max_chars: int | None = None) -> str:
    """將新聞 detail 控制在上限內，同時避免裁成比最低門檻更短的殘句。

    先尋找最低保留字數到上限之間的完整句號；若第二句在上限內尚未收尾，
    再找該區間最後一個分號／逗號。只有完全沒有可用斷點時才硬切，確保
    原本 70～90 字的兩句內容不會因第一個句號太早而只剩 30 多字。
    """
    s = _normalize_news_text(str(detail or "")).strip()
    if not s:
        return ""

    limit = max(1, int(max_chars or NEWS_SUMMARY_DETAIL_MAX_CHARS))
    min_keep = min(
        limit,
        max(40, int(NEWS_SUMMARY_DETAIL_MIN_CHARS) - 5),
    )
    if _count_nonspace_chars(s) <= limit:
        return s if s[-1:] in "。！？" else s.rstrip("；;，,、 ") + "。"

    prefix = _slice_by_nonspace_chars(s, limit).strip()

    def _boundary_positions(chars: str) -> List[int]:
        return [idx for idx, ch in enumerate(prefix) if ch in chars]

    # 只有位於最低保留字數之後的完整句才可作為裁切點。
    sentence_positions = _boundary_positions("。！？")
    sentence_candidates = [
        idx for idx in sentence_positions
        if _count_nonspace_chars(prefix[:idx + 1]) >= min_keep
    ]
    if sentence_candidates:
        return prefix[:sentence_candidates[-1] + 1].strip()

    # 第二句可能超過上限，保留其主要子句，不回退到過短的第一句。
    clause_positions = _boundary_positions("；;，,")
    clause_candidates = [
        idx for idx in clause_positions
        if _count_nonspace_chars(prefix[:idx + 1]) >= min_keep
    ]
    if clause_candidates:
        trimmed = prefix[:clause_candidates[-1]].rstrip("；;，,、 ")
        if trimmed:
            return trimmed + "。"

    hard_prefix = prefix
    if _count_nonspace_chars(hard_prefix) >= limit and hard_prefix[-1:] not in "。！？":
        hard_prefix = _slice_by_nonspace_chars(hard_prefix, max(1, limit - 1))
    return hard_prefix.rstrip("；;，,、 ") + "。"


def _news_detail_observation_suffix(label: str, status: str, tone: str) -> str:
    """依既有題材補一個不新增外部事實的觀察句，供過短 detail 本地補足。"""
    context = f"{label} {status}".strip()
    if re.search(r"營收|獲利|財報|EPS|毛利|業績|法說", context, flags=re.I):
        return "後續觀察出貨節奏、產品組合與獲利表現能否延續。"
    if re.search(r"接單|訂單|合約|得標|出貨|量產|擴產|產能", context, flags=re.I):
        return "後續觀察執行進度、產能配置與營收認列時程。"
    if re.search(r"報價|供需|缺貨|庫存|需求|產業|AI|晶片|半導體", context, flags=re.I):
        return "後續觀察需求延續性、報價變化與公司實際出貨表現。"
    if re.search(r"目標價|評等|法人|外資|市場", context, flags=re.I):
        return "後續仍需以實際營收、獲利與訂單進度驗證市場預期。"
    if tone == "negative":
        return "後續觀察負面因素是否持續影響出貨、成本與獲利表現。"
    if tone == "positive":
        return "後續觀察題材能否轉化為實際訂單、出貨與獲利貢獻。"
    return "後續觀察事件進度及其對營收、出貨與獲利的實際影響。"


def _enrich_short_news_point_detail(point: str, log_label: str = "新聞重點") -> str:
    """過短 detail 先用既有題材補足觀察句，避免為純長度問題重打 Gemini。"""
    fields = _parse_report_point_fields(point)
    detail = str(fields.get("detail", "") or "").strip()
    min_chars = max(1, int(NEWS_SUMMARY_DETAIL_MIN_CHARS))
    before_chars = _count_nonspace_chars(detail)
    if not detail or before_chars >= min_chars:
        return str(point or "").strip()

    suffix = _news_detail_observation_suffix(
        str(fields.get("label", "") or ""),
        str(fields.get("status", "") or ""),
        normalize_report_tone(fields.get("tone", "")) or "neutral",
    )
    base = detail if detail[-1:] in "。！？" else detail.rstrip("；;，,、 ") + "。"
    sentence_matches = list(re.finditer(r"[。！？]", base))
    if len(sentence_matches) >= 2:
        last_boundary = sentence_matches[-2].end()
        first_part = base[:last_boundary]
        second_part = base[last_boundary:].rstrip("。！？ ")
        suffix_core = re.sub(r"^後續(?:仍需)?觀察", "", suffix).rstrip("。！？ ")
        if not suffix_core:
            suffix_core = suffix.rstrip("。！？ ")
        if second_part:
            combined = f"{first_part}{second_part}，並追蹤{suffix_core}。"
        else:
            combined = f"{first_part}{suffix}"
    else:
        combined = f"{base}{suffix}"
    combined = _normalize_news_text(combined)
    combined = _trim_news_detail_text(combined, max_chars=NEWS_SUMMARY_DETAIL_MAX_CHARS)
    after_chars = _count_nonspace_chars(combined)
    if after_chars > before_chars:
        print(
            f"🧩 {log_label} detail 本地補足：{before_chars} 字 → {after_chars} 字｜"
            "沿用原事件，只補營運觀察句"
        )
        return _replace_news_point_detail(point, combined)
    return str(point or "").strip()

def _replace_news_point_detail(point: str, new_detail: str) -> str:
    fields = _parse_report_point_fields(point)
    label = fields.get("label") or "新聞面"
    status = fields.get("status") or "新聞事件待追蹤"
    tone = normalize_report_tone(fields.get("tone")) or "neutral"
    prefix = "下週觀察：" if label == "下週觀察" or tone == "watch" else ""
    if prefix:
        label = "下週觀察"
        tone = "watch"
    return f"{prefix}面向：{label}｜結果：{status}｜說明：{new_detail}｜方向：{tone}"


def _trim_news_point_detail_to_max(point: str, log_label: str = "新聞重點") -> str:
    fields = _parse_report_point_fields(point)
    detail = str(fields.get("detail", "") or "").strip()
    if not detail:
        return str(point or "").strip()
    max_chars = max(1, int(NEWS_SUMMARY_DETAIL_MAX_CHARS))
    before_chars = _count_nonspace_chars(detail)
    if before_chars <= max_chars:
        return str(point or "").strip()
    trimmed_detail = _trim_news_detail_text(detail, max_chars=max_chars)
    after_chars = _count_nonspace_chars(trimmed_detail)
    print(
        f"✂️ {log_label} detail 程式裁切：{before_chars} 字 → {after_chars} 字｜"
        "超長屬格式問題，不觸發 Gemini repair"
    )
    return _replace_news_point_detail(point, trimmed_detail)


def _find_news_detail_length_problems(points: List[str]) -> List[str]:
    """只把 detail 過短視為內容不足；超長由程式端裁切，不觸發 repair。"""
    problems = []
    min_chars = max(1, int(NEWS_SUMMARY_DETAIL_MIN_CHARS))
    for idx, point in enumerate(points or [], 1):
        detail = str(_parse_report_point_fields(point).get("detail", "") or "").strip()
        detail_chars = _count_nonspace_chars(detail)
        if detail_chars < min_chars:
            problems.append(f"第{idx}點 detail 僅 {detail_chars} 字，少於 {min_chars} 字")
    return problems



def _merge_distinct_news_points(
    primary: List[str],
    supplements: List[str],
    stock_code: str = "",
    stock_name: str = "",
) -> List[str]:
    """合併補點並做事件級去重，最多保留新聞點數上限。"""
    merged = _dedupe_news_points_by_event(
        list(primary or []) + list(supplements or []),
        stock_code,
        stock_name,
        log_label="新聞補點",
    )
    return merged[:NEWS_SUMMARY_MAX_POINTS]

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
    include_news_grounding: bool = False,
) -> dict:
    """建立新聞與本週重點共用的結構化 Gemini JSON Schema。"""
    min_points = max(0, int(min_points or 0))
    max_points = max(min_points, int(max_points or min_points or 1))

    point_properties = {
        "label": {"type": "string"},
        "status": {"type": "string"},
        "detail": {"type": "string"},
        "tone": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed", "watch"]},
        "confidence": {"type": "number"},
    }
    required_fields = ["label", "status", "detail", "tone", "confidence"]

    if include_news_grounding:
        # 新聞任務新增來源文章與逐字原文；本週重點維持原有 evidence 陣列。
        point_properties["source_id"] = {"type": "string"}
        point_properties["evidence"] = {"type": "string"}
        required_fields.extend(["source_id", "evidence"])
    else:
        point_properties["evidence"] = {"type": "array", "items": {"type": "string"}}
        required_fields.append("evidence")

    point_schema = {
        "type": "object",
        "properties": point_properties,
        "required": required_fields,
    }
    properties = {
        "points": {
            "type": "array",
            "items": point_schema,
            "minItems": min_points,
            "maxItems": max_points,
        },
    }
    if include_note:
        properties["note"] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": ["points"]}



_EVIDENCE_STRIP_RE = re.compile(
    r"[\s，。、；：「」『』（）()\[\]【】,.;:'\"!?！？…\-—~～]+"
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n；;]")


def _normalize_for_evidence_match(text: str) -> str:
    """只移除格式與標點，保留文字內容供 evidence 逐字比對。"""
    return _EVIDENCE_STRIP_RE.sub("", str(text or "")).lower()


def _find_article_by_source_id(usable_articles: List[dict], source_id: str):
    """依實際送入 Gemini 的 A1、A2…文章編號尋找來源。"""
    source_id = str(source_id or "").strip()
    if not source_id:
        return None
    for article in usable_articles or []:
        if str(article.get("id", "") or "").strip() == source_id:
            return article
    return None


_NEWS_SCALED_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>兆|億|萬)?\s*(?P<currency>元)?"
)
_NEWS_UNIT_MULTIPLIERS = {
    "萬": 10_000,
    "億": 100_000_000,
    "兆": 1_000_000_000_000,
}


def _decimal_news_number(raw: str):
    try:
        return Decimal(str(raw or "").replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _build_scaled_source_number_map(article: dict) -> Dict[Decimal, str]:
    """建立單篇來源中「500億元 → 50000000000」的可逆對照。"""
    source_text = _normalize_news_text(
        f"{article.get('title', '')}。{article.get('body', '')}"
    )
    mapping: Dict[Decimal, str] = {}
    for matched in _NEWS_SCALED_NUMBER_RE.finditer(source_text):
        unit = str(matched.group("unit") or "")
        if unit not in _NEWS_UNIT_MULTIPLIERS:
            continue
        number = _decimal_news_number(matched.group("num"))
        if number is None:
            continue
        base_value = number * Decimal(_NEWS_UNIT_MULTIPLIERS[unit])
        phrase = re.sub(r"\s+", "", matched.group(0))
        previous = mapping.get(base_value, "")
        if not previous or ("元" in phrase and "元" not in previous) or len(phrase) < len(previous):
            mapping[base_value] = phrase
    return mapping


def _repair_scaled_numbers_in_text(text: str, article: dict) -> tuple[str, int]:
    """只在同一 source_id 文章存在完全等值單位表達時，還原 Gemini 的錯誤展開。"""
    raw = str(text or "")
    source_map = _build_scaled_source_number_map(article)
    if not raw or not source_map:
        return raw, 0

    repaired_count = 0

    def replace_match(matched):
        nonlocal repaired_count
        # 已帶萬／億／兆的輸出不處理；只修正 Gemini 展開成長整數的情況。
        if matched.group("unit"):
            return matched.group(0)
        number_text = str(matched.group("num") or "").replace(",", "")
        integer_digits = len(number_text.split(".", 1)[0].lstrip("0"))
        if integer_digits < 8:
            return matched.group(0)
        number = _decimal_news_number(number_text)
        if number is None:
            return matched.group(0)
        replacement = source_map.get(number)
        if not replacement:
            return matched.group(0)
        repaired_count += 1
        return replacement

    return _NEWS_SCALED_NUMBER_RE.sub(replace_match, raw), repaired_count


def _repair_news_item_number_units_locally(item: dict, usable_articles: List[dict]) -> dict:
    """依 point 的 source_id 本地還原數字單位，不跨文章借用數字。"""
    if not isinstance(item, dict):
        return item
    source_id = str(item.get("source_id", "") or "").strip()
    article = _find_article_by_source_id(usable_articles, source_id)
    if article is None:
        return dict(item)

    repaired = dict(item)
    total_repairs = 0
    for field in ("status", "detail"):
        new_text, count = _repair_scaled_numbers_in_text(repaired.get(field, ""), article)
        repaired[field] = new_text
        total_repairs += count
    if total_repairs:
        print(
            f"🧩 新聞數字單位本地還原：source_id={source_id}｜"
            f"修正 {total_repairs} 處｜保留來源原始萬／億／兆表達"
        )
    return repaired


def _news_evidence_match_score(sentence: str, point_obj: dict) -> int:
    """以數字、財經關鍵詞與中文字雙字詞，挑選最接近 point 的來源原句。"""
    sentence_text = _normalize_news_text(sentence)
    query_text = _normalize_news_text(
        f"{point_obj.get('label', '')} {point_obj.get('status', '')} {point_obj.get('detail', '')}"
    )
    if not sentence_text or not query_text:
        return 0

    score = 0
    sentence_numbers = set(_extract_grounded_number_tokens(sentence_text))
    query_numbers = set(_extract_grounded_number_tokens(query_text))
    score += 12 * len(sentence_numbers & query_numbers)

    keywords = [
        "營收", "獲利", "毛利", "EPS", "接單", "訂單", "得標", "出貨", "量產",
        "擴產", "產能", "需求", "供給", "報價", "庫存", "法說", "財測", "目標價",
        "評等", "AI", "ASIC", "晶片", "半導體", "伺服器", "手機", "車用",
    ]
    score += 5 * sum(1 for keyword in keywords if keyword in query_text and keyword in sentence_text)

    def bigrams(text: str) -> set:
        compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
        return {compact[idx:idx + 2] for idx in range(max(0, len(compact) - 1))}

    score += min(20, len(bigrams(sentence_text) & bigrams(query_text)))
    return score


def _repair_news_item_evidence_locally(
    item: dict,
    usable_articles: List[dict],
    stock_code: str,
    stock_name: str,
) -> dict:
    """Gemini evidence 有改寫時，從同一 source_id 找回可逐字驗證的最佳原句。"""
    if not isinstance(item, dict):
        return item
    repaired = dict(item)
    ok, _ = _validate_news_point_evidence(repaired, usable_articles, stock_code, stock_name)
    if ok:
        return repaired

    source_id = str(repaired.get("source_id", "") or "").strip()
    article = _find_article_by_source_id(usable_articles, source_id)
    if article is None:
        return repaired

    raw_text = _normalize_news_text(
        f"{article.get('title', '')}。{article.get('body', '')}"
    )
    sentences = [
        str(sentence or "").strip()
        for sentence in _SENTENCE_SPLIT_RE.split(raw_text)
        if str(sentence or "").strip()
    ]
    candidates = []
    for idx, sentence in enumerate(sentences):
        window_start = max(0, idx - 1)
        subject_window = "。".join(sentences[window_start:idx + 1])
        if not _news_text_matches_target_stock(subject_window, stock_code, stock_name):
            continue
        if len(_normalize_for_evidence_match(sentence)) < 8:
            continue
        candidates.append((_news_evidence_match_score(sentence, repaired), -idx, sentence))

    if not candidates:
        return repaired
    candidates.sort(reverse=True)
    best_sentence = candidates[0][2]
    original = str(repaired.get("evidence", "") or "").strip()
    repaired["evidence"] = best_sentence
    ok, _ = _validate_news_point_evidence(repaired, usable_articles, stock_code, stock_name)
    if ok and best_sentence != original:
        print(
            f"🧩 新聞 evidence 本地修復：source_id={source_id}｜"
            f"改用來源原句「{best_sentence[:48]}」"
        )
        return repaired
    return dict(item)


def _validate_news_point_evidence(
    point_obj: dict,
    usable_articles: List[dict],
    stock_code: str,
    stock_name: str,
) -> tuple[bool, str]:
    """驗證來源文章、逐字原文，以及 evidence 所在句與前一句的公司主體。"""
    if not isinstance(point_obj, dict):
        return False, "points item 不是物件"

    evidence = str(point_obj.get("evidence", "") or "").strip()
    source_id = str(point_obj.get("source_id", "") or "").strip()
    ev_norm = _normalize_for_evidence_match(evidence)
    if len(ev_norm) < 8:
        return False, "evidence 過短或缺漏"

    article = _find_article_by_source_id(usable_articles, source_id)
    if article is None:
        return False, f"source_id {source_id or '(空白)'} 不存在"

    raw_text = _normalize_news_text(
        f"{article.get('title', '')}。{article.get('body', '')}"
    )
    sentences = [
        str(sentence or "").strip()
        for sentence in _SENTENCE_SPLIT_RE.split(raw_text)
        if str(sentence or "").strip()
    ]

    matched_sentence = ""
    matched_index = -1
    for idx, sentence in enumerate(sentences):
        sentence_norm = _normalize_for_evidence_match(sentence)
        if sentence_norm and ev_norm in sentence_norm:
            matched_sentence = sentence
            matched_index = idx
            break

    if not matched_sentence:
        return False, "evidence 無法在指定文章的單一原文句中找到（疑似改寫或編造）"

    # 中文新聞常先用公司名建立主體，下一句再用「公司」承接具體內容。
    # 文章級品質門檻仍會擋掉產業綜述，因此這裡只放寬為 evidence 所在句 + 前一句。
    window_start = max(0, matched_index - 1)
    subject_window = "。".join(sentences[window_start: matched_index + 1])
    if not _news_text_matches_target_stock(subject_window, stock_code, stock_name):
        display_name = stock_name or stock_code
        return False, f"evidence 所在句與前一句均未出現 {display_name} 或 {stock_code}（疑似產業文誤植）"

    return True, ""


def _format_rejected_news_point(point_obj, reason: str) -> dict:
    """整理 evidence 驗證刪除原因，供 repair payload 使用。"""
    if isinstance(point_obj, dict):
        point_text = _point_item_to_canonical_text(point_obj)
        source_id = str(point_obj.get("source_id", "") or "").strip()
        evidence = str(point_obj.get("evidence", "") or "").strip()
    else:
        point_text = str(point_obj or "").strip()
        source_id = ""
        evidence = ""
    return {
        "point": point_text,
        "source_id": source_id,
        "evidence": evidence,
        "reason": str(reason or "未通過 evidence 驗證"),
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

    if ACTION_CACHE_ONLY_MODE:
        print(
            "☁️ Action=0 嚴格快取模式未命中 Gemini 快取，"
            f"不呼叫 Gemini：{cache_task or '未指定任務'}｜{stock_code}"
        )
        return None

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


def _hybrid_news_event_key(record: dict, stock_code: str = "", stock_name: str = "") -> str:
    """建立混合模式用的事件鍵；與新聞素材／輸出點共用同一套事件去重規則。"""
    return _news_article_event_key(record, stock_code, stock_name)


def _enrich_fast_news_records_with_topk_bodies(
    records: List[dict],
    stock_code: str,
    stock_name: str,
) -> List[dict]:
    """只替最高分的前兩個不同事件平行補抓原文，避免六來源搜尋後再次長時間等待。"""
    enriched = [dict(record) for record in (records or [])]
    if (
        not NEWS_FAST_MODE
        or not NEWS_FAST_HYBRID_BODY_FETCH_ENABLE
        or NEWS_FAST_HYBRID_BODY_FETCH_TOPK <= 0
        or not enriched
    ):
        return enriched

    candidates_by_event = {}
    for index, record in enumerate(enriched):
        if record.get("body_ok") or not record.get("fallback_ok"):
            continue
        url = str(record.get("url", "") or "").strip()
        if not _is_fetchable_news_article_url(url):
            continue
        if str(record.get("content_source", "") or "") == "manual":
            continue
        if record.get("finmind_title_only"):
            # FinMind 標題仍可提供事件線索，但沒有直接新聞頁原文可補抓時不浪費請求。
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
        direct_rank = 0 if "news.google." in host else 1
        event_key = _hybrid_news_event_key(record, stock_code, stock_name) or f"row:{index}"
        candidate = (index, relevance, published_dt, direct_rank, event_key)

        previous = candidates_by_event.get(event_key)
        quality = (
            direct_rank,
            relevance,
            published_dt.timestamp() if published_dt != datetime.min else 0,
            -index,
        )
        if previous is None:
            candidates_by_event[event_key] = candidate
        else:
            prev_index, prev_relevance, prev_dt, prev_direct_rank, _ = previous
            previous_quality = (
                prev_direct_rank,
                prev_relevance,
                prev_dt.timestamp() if prev_dt != datetime.min else 0,
                -prev_index,
            )
            if quality > previous_quality:
                candidates_by_event[event_key] = candidate

    if not candidates_by_event:
        print("ℹ️ 混合新聞模式沒有值得補抓的原文網址，直接使用已取得摘要")
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
        f"📰 混合新聞模式：從 {len(enriched):,} 筆素材中，"
        f"事件去重後平行補抓最高分 {len(candidates):,} 篇原文"
    )
    started = time.perf_counter()
    deadline = started + max(1.0, float(NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT))
    upgraded = 0

    def fetch_candidate(candidate):
        index, _, _, _, _ = candidate
        record = enriched[index]
        body = _fetch_article_body(
            str(record.get("url", "") or "").strip(),
            request_timeout=float(NEWS_FAST_HYBRID_BODY_FETCH_REQUEST_TIMEOUT),
            max_bytes=NEWS_FAST_HYBRID_BODY_FETCH_MAX_BYTES,
            hard_deadline_seconds=float(NEWS_FAST_HYBRID_BODY_FETCH_BATCH_TIMEOUT),
        )
        return index, body

    executor = ThreadPoolExecutor(max_workers=max(1, min(2, len(candidates))))
    future_map = {executor.submit(fetch_candidate, candidate): candidate for candidate in candidates}
    pending = set(future_map)
    try:
        while pending:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=min(0.2, max(0.05, remaining)),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                candidate = future_map[future]
                index = candidate[0]
                record = enriched[index]
                try:
                    _, body = future.result()
                except Exception as exc:
                    print(f"⚠️ 混合新聞原文抓取失敗：{record.get('title', '')[:36]}｜{exc}")
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
                    continue
                record["content"] = body
                record["body_ok"] = True
                record["fallback_ok"] = False
                record["content_source"] = "hybrid_article"
                record["body_length"] = len(body)
                record["relevance_score"] = _score_news_article_relevance(record, stock_code, stock_name)
                upgraded += 1
                print(f"✅ 混合新聞補到原文：{record.get('title', '')[:36]}｜{len(body):,} 字")
    finally:
        if pending:
            for future in pending:
                future.cancel()
            print(f"⚠️ 混合新聞原文整批逾時，略過剩餘 {len(pending):,} 篇")
        executor.shutdown(wait=False, cancel_futures=True)

    indexed = list(enumerate(enriched))
    indexed.sort(
        key=lambda item: (
            0 if item[1].get("body_ok") else 1,
            -int(item[1].get("relevance_score", 0) or 0),
            item[0],
        )
    )
    elapsed = time.perf_counter() - started
    print(f"⏱️ 混合新聞原文補抓：{elapsed:.2f} 秒｜成功 {upgraded}/{len(candidates)} 篇")
    return [record for _, record in indexed]



def _build_gemini_news_articles(records: List[dict], stock_code: str = "", stock_name: str = "") -> List[dict]:
    """只把可用的新聞素材送給 Gemini，並先萃取本股票相關片段，避免多家公司新聞數字混用。

    修正重點：
    1. 極速 RSS 模式本來只有標題 / 摘要，不能用原文模式的 40～80 字門檻硬擋。
    2. 若官方名稱暫時查不到，從「公司名(代號)」格式自動補別名。
    3. 標題或摘要明確包含本股票代號 / 名稱，且有營收、法說、接單、目標價等公司資訊時，允許短素材進 Gemini。
    """
    usable = []
    seen_event_keys = set()
    ordered = [r for r in records if r.get("body_ok") or r.get("fallback_ok")]
    for rec in ordered:
        title = _clean_news_title(rec.get("title", ""))
        raw_content = _normalize_news_text(rec.get("content", ""))
        description = _normalize_news_text(rec.get("description", ""))
        content_source = str(rec.get("content_source", ""))
        is_fast_rss = content_source in (
            "google_news_rss_fast",
            "rss_description",
            "rss_title_fact",
            "finmind_description",
            "finmind_title_fact",
            "manual",
        )

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

        # 極速 RSS 模式只保留乾淨摘要，不再把標題、來源、日期混進 body。
        if not raw_content and is_fast_rss:
            raw_content = _build_fast_rss_news_content(
                title,
                description,
                source=rec.get("source", ""),
                published=rec.get("published", ""),
            )

        raw_content = _strip_news_meta_noise(raw_content)
        if _is_price_only_news_without_fundamentals(f"{title} {raw_content}"):
            continue
        if is_fast_rss and not raw_content:
            # Google News RSS 常沒有真正摘要；若標題本身就是營收、法說、訂單、產能、評等等
            # 明確基本面事實，允許以「標題型事實素材」送入 Gemini，再由輸出後檢查阻擋照抄。
            if has_target and has_value and _can_use_news_title_as_fact(title):
                raw_content = title
                content_source = "rss_title_fact"
            else:
                continue

        min_content_len = 18 if is_fast_rss else 80
        if len(raw_content) < min_content_len and not (is_fast_rss and has_target and has_value and raw_content):
            continue

        focused_content = _extract_target_focused_news_body(raw_content, stock_code, stock_name)
        focused_norm = _normalize_news_text(focused_content)

        # 對 RSS 短素材做保守 fallback：標題 / 摘要已明確包含本股票與公司資訊時，保留原素材。
        if len(focused_norm) < 40 and is_fast_rss and has_target and has_value:
            focused_content = raw_content
            focused_norm = _normalize_news_text(focused_content)

        focused_min_len = (
            8
            if content_source in ("rss_title_fact", "finmind_title_fact")
            else 20 if is_fast_rss else 40
        )
        if len(focused_norm) < focused_min_len:
            print(f"⚠️ 略過多股混雜新聞：{title[:36]}｜找不到足夠的 {stock_code} {stock_name} 明確片段")
            continue

        event_key = _news_event_fingerprint(
            f"{title}。{focused_content}",
            published=str(rec.get("published", "") or ""),
            stock_code=stock_code,
            stock_name=stock_name,
        )
        if event_key and event_key in seen_event_keys:
            print(f"🧹 送入 Gemini 前事件去重：略過重複事件｜{event_key}｜{title[:36]}")
            continue
        if event_key:
            seen_event_keys.add(event_key)

        usable.append({
            "id": f"A{len(usable) + 1}",
            "source": rec.get("source", ""),
            "source_family": rec.get("source_family", rec.get("source", "")),
            "title": title,
            "published": rec.get("published", ""),
            "url": rec.get("url", ""),
            "content_source": content_source,
            "finmind_title_only": bool(rec.get("finmind_title_only")),
            "event_key": event_key,
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
    valid_points = _dedupe_news_points_by_event(
        valid_points,
        stock_code,
        stock_name,
        log_label="新聞快取寫入前",
    )
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
    """將合格新聞交給 Gemini；每點都必須通過句級主體與數字接地驗證。"""
    usable_articles = _build_gemini_news_articles(records, stock_code, stock_name)
    if not usable_articles:
        print("⚠️ 沒有足夠且具體的公司新聞可送入 Gemini；不使用標題或盤勢新聞硬湊")
        return []

    display_name = stock_name if stock_name else stock_code
    article_json = json.dumps(usable_articles, ensure_ascii=False, indent=2)
    number_grounding_source = _build_news_number_grounding_source(usable_articles)
    response_schema = _build_gemini_points_response_schema(
        min_points=0,
        max_points=NEWS_SUMMARY_MAX_POINTS,
        include_note=True,
        include_news_grounding=True,
    )

    prompt = f"""
你是台股財經新聞編輯。只能使用下方素材，整理 {stock_code} {display_name} 的新聞／題材重點，使用繁體中文。

分析原則：
1. 最多輸出 {NEWS_SUMMARY_MAX_POINTS} 點，只有「不同事件」才能分成不同點；若素材中有 3 個直接相關且具體的不同事件，優先完整輸出 3 點；只有 2 個就輸出 2 點，只有 1 個才輸出 1 點。同一月份營收、同一份財報、同一公告或同一法人報告，即使來源、標題、source_id 不同，也只能保留 1 點；不得拿股價漲跌、熱門排行或大盤盤勢湊數。
2. 每個 points item 必須回傳 label、status、detail、tone、confidence、source_id、evidence；status 不可寫「重點待確認」「題材待觀察」等空句。
2-1. tone 只能是 positive、negative、neutral、mixed、watch：明確利多用 positive，明確利空用 negative，正負並存用 mixed，無明確方向用 neutral，單純後續觀察用 watch。
2-2. 「受關注」不等於 positive；創新高必須確認是營收、獲利、毛利率、接單等正向指標，若是虧損、庫存、負債創高則為 negative。
2-3. confidence 為 0 到 1。
2-4. status 必須是 8～14 字的具體結論短句，需包含事件主體或數據方向，例如「6月營收年增328%創新高」「DRAM供給緊縮推升報價」；不得只寫 2～4 字的詞，如「創新高」「供給緊縮」「需求強勁」。status 不要以 {display_name} 或股票代號 {stock_code} 開頭（圖卡主體已是該公司）。
3. 每點必須附 source_id（文章編號）與 evidence（逐字抄自該文章、能直接支持這個結論的一句原文，不可改寫）。evidence 所在句或其前一句必須明確出現 {stock_code} 或 {display_name}，可接受「公司名建立主體，下一句以公司承接」的寫法；文章若只是順帶提到本公司、主體是產業或其他公司，不得使用。
4. 若所有素材都沒有以 {display_name} 為主體的具體事件，points 回傳空陣列 []，這是正確行為，嚴禁硬湊。
5. 所有數字與單位必須原樣存在於素材；不得換算、推估或補充素材沒有的數字。素材寫「500億元」時必須維持「500億元」，禁止改成「50000000000元」；素材寫「1.5億」時不得改成「150000000」。
6. 只寫公司新聞、重大訊息、營運、產業供需或具體法人觀點；不得寫權證、分點、K線、均線、買賣建議、網址或媒體資訊。每點需獨立完整、自然收尾並以句號結束。
7. detail 必須寫成與本週重點同等完整的 2 個短句，總長度控制在 {NEWS_SUMMARY_DETAIL_MIN_CHARS}～{NEWS_SUMMARY_DETAIL_MAX_CHARS} 字：第一句交代事件、關鍵數字與方向，第二句說明對營運的意涵或具體後續觀察。不得只寫「後續持續關注」等空泛句，也不得為了縮短而只保留第一句。

好範例：
- {{"label":"公司動態","status":"取得12億元大單、下半年出貨","detail":"公司取得新客戶大單，金額約12億元、預計下半年開始出貨。後續觀察產能配置與營收認列時程，若如期放量將挹注第4季營運動能。","tone":"positive","confidence":0.92,"source_id":"A1","evidence":"{display_name}取得金額約12億元的新客戶大單，預計下半年開始出貨"}}
- {{"label":"業績更新","status":"營收年增但月減、動能待確認","detail":"本月營收仍較去年同期成長，但較上月回落，顯示長期需求尚有支撐、短線出貨節奏轉弱。後續觀察新產品放量與毛利率能否改善。","tone":"mixed","confidence":0.90,"source_id":"A2","evidence":"{display_name}本月營收年增但較上月減少"}}

壞範例：
- {{"label":"業績更新","status":"創新高","detail":"6月營收年增328%並創新高。","tone":"positive","confidence":0.95,"source_id":"A2","evidence":"{display_name}6月營收年增328%並創新高"}}（錯誤：status 太短且缺少事件主體）
- {{"label":"題材觀察","status":"題材仍待確認","detail":"後續持續關注。","tone":"positive","confidence":0.95,"source_id":"A3","evidence":"AI散熱需求持續強勁"}}
- {{"label":"法人觀點","status":"市場偏多","detail":"公司未來值得期待。","tone":"positive","confidence":0.90,"source_id":"A1","evidence":"市場看好相關族群"}}

只回傳符合 JSON Schema 的 JSON，不要 markdown 或其他說明。

新聞素材：
{article_json}
"""
    print("=" * 100)
    print("開始呼叫 Gemini 統整高品質新聞重點")
    print(f"模型：{GEMINI_MODEL}")
    print(f"送入 Gemini 的文章數：{len(usable_articles)}｜允許輸出 0～{NEWS_SUMMARY_MAX_POINTS} 點")
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
    points, rejected_points = _parse_gemini_news_points(
        output_text or "",
        usable_articles,
        stock_code,
        stock_name,
    )
    accepted_source_ids = _extract_validated_news_source_ids(
        output_text or "",
        usable_articles,
        stock_code,
        stock_name,
    )

    content_problems = []
    if points and not _points_are_independent_and_complete(points):
        content_problems.append("出現承接句、半句或省略號")
    ungrounded = _find_ungrounded_number_tokens(points, number_grounding_source)
    if ungrounded:
        content_problems.append("含素材找不到的數字：" + _format_ungrounded_number_problems(ungrounded))
    detail_length_problems = _find_news_detail_length_problems(points)
    if (
        len(points) >= NEWS_SUPPLEMENT_TRIGGER_POINTS
        and _count_summary_chars(points) < NEWS_SUMMARY_MIN_TOTAL_CHARS
    ):
        content_problems.append(
            f"總字數約 {_count_summary_chars(points)} 字，低於 {NEWS_SUMMARY_MIN_TOTAL_CHARS} 字"
        )
    problems = list(content_problems) + list(detail_length_problems)

    for rejected in rejected_points:
        print(
            "⚠️ 新聞 evidence 驗證刪除："
            f"{rejected.get('reason', '')}｜{str(rejected.get('point', ''))[:80]}"
        )

    needs_distinct_event_completion = bool(
        NEWS_GEMINI_SUPPLEMENT_ENABLE
        and len(points) < NEWS_SUPPLEMENT_TRIGGER_POINTS
        and len(usable_articles) >= NEWS_SUPPLEMENT_MIN_USABLE_ARTICLES
    )
    if (
        NEWS_GEMINI_SUPPLEMENT_ENABLE
        and len(points) < NEWS_SUPPLEMENT_TRIGGER_POINTS
        and len(usable_articles) < NEWS_SUPPLEMENT_MIN_USABLE_ARTICLES
    ):
        print(
            f"ℹ️ 新聞不同事件補充未觸發：驗證後 {len(points)} 點｜"
            f"合格文章 {len(usable_articles)} 篇｜至少需要 {NEWS_SUPPLEMENT_MIN_USABLE_ARTICLES} 篇"
        )

    # 最多只再呼叫一次 Gemini：同一輪完成格式補正、保留合格題材與補充不同事件。
    combined_repair_needed = bool(
        ((problems or rejected_points) and NEWS_GEMINI_REPAIR_ENABLE)
        or needs_distinct_event_completion
    )
    if combined_repair_needed:
        log_problems = list(problems)
        log_problems.extend(
            str(item.get("reason", "") or "evidence 驗證失敗")
            for item in rejected_points
        )
        if needs_distinct_event_completion:
            log_problems.append(
                f"目前僅 {len(points)} 點，但有 {len(usable_articles)} 篇不同事件素材可補充"
            )
        print("⚠️ Gemini 新聞重點進入一次性補正／補點：" + "；".join(log_problems))

        repair_payload = {
            "content_or_format_problems": content_problems,
            "evidence_deleted_points": rejected_points,
            "validated_points_to_preserve": points,
            "validated_source_ids": accepted_source_ids,
            "need_distinct_event_completion": needs_distinct_event_completion,
            "articles": usable_articles,
        }
        repair_prompt = f"""
你是台股財經新聞編輯。上一版輸出未完全通過檢查，請只依修正資料重新輸出 {stock_code} {display_name} 的「最終完整 points 清單」。

修正原則：
1. 最多輸出 {NEWS_SUMMARY_MAX_POINTS} 點，每點必須是不同事件；同一月份營收、同一份財報、同一公告或同一法人報告不論來源多少都只能保留 1 點。若素材中有 3 個直接相關且具體的不同事件，優先完整輸出 3 點；只有 2 個就輸出 2 點，只有 1 個才輸出 1 點，不得用股價漲跌或空泛題材湊數。
2. validated_points_to_preserve 已通過本地 evidence、公司主體與數字驗證，除非與更完整的同事件版本重複，否則必須保留其題材與事實；再從其他文章補入不同事件。
3. 每個 item 必須包含 label、status、detail、tone、confidence、source_id、evidence。status 必須是 8～14 字的具體結論短句，不要以 {display_name} 或 {stock_code} 開頭。
4. detail 必須是 {NEWS_SUMMARY_DETAIL_MIN_CHARS}～{NEWS_SUMMARY_DETAIL_MAX_CHARS} 字的 2 個完整短句：第一句交代事件、關鍵數字與方向，第二句說明營運意涵或具體後續觀察。不得只寫「後續持續關注」；不得把完整內容縮成單句。
5. source_id 必須存在於 articles；evidence 必須逐字抄自該文章的一句原文，且 evidence 所在句或前一句必須出現 {stock_code} 或 {display_name}。
6. 所有數字與單位都必須原樣保留來源寫法。來源寫「500億元」時必須寫「500億元」，禁止換算成「50000000000元」；來源寫「1.5億」時不得改成「150000000」。不得推估、補充或跨文章借用數字。
7. tone 只能是 positive、negative、neutral、mixed、watch；只寫公司新聞、重大訊息、營運、產業供需或具體法人觀點，不得寫權證、分點、K線、買賣建議、網址或媒體資訊。
8. 找不到直接相關事件時可回傳 points: []，但不可刪掉已在 validated_points_to_preserve 中通過驗證的有效題材。

只回傳符合 JSON Schema 的 JSON。

修正資料：
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
"""
        repaired_text = _call_gemini_with_retry(
            repair_prompt,
            cache_task=f"{_news_points_cache_task()}_repair_complete_v32",
            stock_code=stock_code,
            stock_name=stock_name,
            write_cache=False,
            response_schema=response_schema,
            temperature=GEMINI_ANALYSIS_TEMPERATURE,
        )
        repaired, repaired_rejected = _parse_gemini_news_points(
            repaired_text or "",
            usable_articles,
            stock_code,
            stock_name,
        )
        repaired_source_ids = _extract_validated_news_source_ids(
            repaired_text or "",
            usable_articles,
            stock_code,
            stock_name,
        )
        accepted_source_ids = list(dict.fromkeys(accepted_source_ids + repaired_source_ids))

        repaired_ungrounded = _find_ungrounded_number_tokens(
            repaired,
            number_grounding_source,
        )
        repaired_detail_problems = _find_news_detail_length_problems(repaired)
        repaired_total_ok = bool(
            not repaired
            or len(repaired) < NEWS_SUPPLEMENT_TRIGGER_POINTS
            or _count_summary_chars(repaired) >= NEWS_SUMMARY_MIN_TOTAL_CHARS
        )
        repaired_ok = bool(
            not repaired_rejected
            and (not repaired or _points_are_independent_and_complete(repaired))
            and not repaired_ungrounded
            and not repaired_detail_problems
            and repaired_total_ok
            and (bool(repaired) or not points)
        )

        if repaired_ok:
            # 補正稿應是完整清單；若模型意外漏掉原本合格點，合併後再做事件去重。
            points = _merge_distinct_news_points(
                repaired,
                points,
                stock_code,
                stock_name,
            )
            print(
                f"✅ Gemini 新聞一次性補正／補點完成：{len(points)} 點｜"
                f"總字數約 {_count_summary_chars(points)} 字"
            )
        else:
            for rejected in repaired_rejected:
                print(
                    "⚠️ Gemini 新聞一次性補正後 evidence 仍不合格："
                    f"{rejected.get('reason', '')}｜{str(rejected.get('point', ''))[:80]}"
                )
            if repaired_ungrounded:
                print(
                    "⚠️ Gemini 新聞一次性補正後仍有無依據數字："
                    + _format_ungrounded_number_problems(repaired_ungrounded)
                )
            if repaired_detail_problems:
                print(
                    "⚠️ Gemini 新聞一次性補正後 detail 仍過短："
                    + "；".join(repaired_detail_problems)
                )
            if not repaired_total_ok:
                print(
                    f"⚠️ Gemini 新聞一次性補正後總字數約 {_count_summary_chars(repaired)} 字，"
                    f"仍低於 {NEWS_SUMMARY_MIN_TOTAL_CHARS} 字"
                )

            valid_repaired = _filter_points_with_grounded_numbers(
                repaired,
                number_grounding_source,
                "新聞一次性補正",
            )
            valid_repaired = [
                point
                for point in valid_repaired
                if not _find_news_detail_length_problems([point])
                and _points_are_independent_and_complete([point])
            ]
            points = _merge_distinct_news_points(
                points,
                valid_repaired,
                stock_code,
                stock_name,
            )
            print(
                f"ℹ️ 一次性補正未完全通過，保留本地已驗證結果：{len(points)} 點"
            )

    points = _dedupe_news_points_by_event(
        points,
        stock_code,
        stock_name,
        log_label="新聞最終輸出前",
    )[:NEWS_SUMMARY_MAX_POINTS]

    final_ungrounded = _find_ungrounded_number_tokens(
        points,
        number_grounding_source,
    )
    final_detail_problems = _find_news_detail_length_problems(points)
    final_total_ok = bool(
        not points
        or len(points) < NEWS_SUPPLEMENT_TRIGGER_POINTS
        or _count_summary_chars(points) >= NEWS_SUMMARY_MIN_TOTAL_CHARS
    )
    fully_valid = (
        (not points or _points_are_independent_and_complete(points))
        and not final_ungrounded
        and not final_detail_problems
        and final_total_ok
    )
    if points and fully_valid:
        _save_validated_news_points_cache(
            _news_points_cache_task(),
            stock_code,
            stock_name,
            prompt,
            points,
            note=f"validated_news_points_sentence_grounded_{len(points)}",
        )
        print(f"✅ Gemini 新聞重點完成：{len(points)} 點，總字數約 {_count_summary_chars(points)} 字")
    elif not points and fully_valid:
        print(f"ℹ️ Gemini 判定本週無與 {display_name} 直接相關之重大新聞")
    elif points:
        if final_detail_problems:
            print("⚠️ Gemini 新聞重點 detail 過短，未通過最終驗收：" + "；".join(final_detail_problems))
        if not final_total_ok:
            print(
                f"⚠️ Gemini 新聞重點總字數約 {_count_summary_chars(points)} 字，"
                f"低於 {NEWS_SUMMARY_MIN_TOTAL_CHARS} 字"
            )
        print(f"⚠️ Gemini 新聞重點有 {len(points)} 點未通過完整驗證，不寫入快取")
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


def _derive_technical_tone_from_ctx(ctx: dict) -> str:
    """技術面顏色只依程式已計算的技術卡片結果，不依 AI 新詞猜測。"""
    try:
        card = _build_technical_card_summary(ctx)
        headline = str(card.get("headline", "") or "")
    except Exception:
        return "neutral"
    if any(k in headline for k in ["跌破", "轉弱", "空頭", "死亡交叉", "修正壓力"]):
        return "negative"
    if any(k in headline for k in ["站上均線", "偏強", "多頭排列", "黃金交叉", "突破"]):
        return "positive"
    return "neutral"


def _infer_face_label_from_text(text, fallback="重點面"):
    """依自由文字推斷週報面向，供模組層級 fallback 與畫圖流程共用。"""
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


def _infer_status_from_text(text, fallback="重點待確認"):
    """依自由文字推斷簡短狀態，供純文字 fallback 與畫圖流程共用。"""
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
    if any(k in s for k in [
        "轉弱", "賣壓", "跌破", "空頭", "空頭排列", "均線空頭", "死亡交叉", "均線死亡交叉",
        "死叉", "資金流出", "淨流出", "賣超", "年減", "利空", "調降", "下修", "看壞",
        "衰退", "需求疲弱",
    ]):
        return "偏弱"
    if "月減" in s and "年增" not in s:
        return "偏弱"
    if any(k in s for k in ["中性", "觀望", "待確認", "有限", "接近中性"]):
        return "中性觀察"
    return fallback


def _canonicalize_weekly_point_structure(point: str, ctx: dict) -> str:
    """將舊版條件式備援文字轉成統一的面向／結果／說明格式，避免 tone 在備援路徑遺失。"""
    s = _normalize_news_text(str(point or "")).replace("|", "｜").strip()
    if not s:
        return ""

    fields = _parse_report_point_fields(s)
    existing_label = str(fields.get("label", "") or "").strip()
    existing_status = str(fields.get("status", "") or "").strip()
    existing_detail = str(fields.get("detail", "") or "").strip()
    if existing_label and existing_status:
        detail = existing_detail or _strip_report_tone_metadata(s)
        return (
            f"面向：{existing_label}｜結果：{existing_status}｜"
            f"說明：{detail}｜方向：{normalize_report_tone(fields.get('tone')) or 'neutral'}"
        )

    # 下週觀察固定為 watch，不依文字猜顏色。
    if re.match(r"^下週觀察[:：]", s):
        detail = re.sub(r"^下週觀察[:：]\s*", "", s).strip()
        status = "確認價量與資金延續性"
        pattern = _build_price_volume_pattern_payload(ctx)
        label_text = str(pattern.get("current_pattern_label", "") or "").strip()
        if label_text:
            status = _compact_text_for_card_headline(f"確認{label_text}延續性", 16)
        return f"面向：下週觀察｜結果：{status}｜說明：{detail}｜方向：watch"

    # 型態面／技術面舊字串直接改用程式技術卡片，確保文字與 tone 同源。
    if s.startswith("型態面") or s.startswith("技術面"):
        card = _build_technical_card_summary(ctx) or {}
        status = str(card.get("headline", "技術訊號待確認") or "技術訊號待確認")
        detail = str(card.get("detail", s) or s)
        return f"面向：技術面｜結果：{status}｜說明：{detail}｜方向：neutral"

    # 代表性分點與籌碼集中度統一歸為權證面。
    if re.match(r"^(買超|賣超)TOP\d+", s) or s.startswith("籌碼集中度"):
        net_value = float(ctx.get("total_net", 0) or 0)
        if net_value > 0:
            status = "權證資金偏向流入"
        elif net_value < 0:
            status = "權證資金偏向流出"
        else:
            status = "權證資金方向有限"
        return f"面向：權證面｜結果：{status}｜說明：{s}｜方向：neutral"

    # 法人與權證資金交叉比較統一歸為法人面，tone 後續仍由實際法人數據覆蓋。
    if s.startswith("資金交叉"):
        inst_ctx = _get_weekly_institutional_context(ctx)
        inst_class = str(inst_ctx.get("classification", "") or "")
        warrant_net = float(ctx.get("total_net", 0) or 0)
        if inst_class == "偏多" and warrant_net > 0:
            status = "法人與權證資金同向偏多"
        elif inst_class == "偏空" and warrant_net < 0:
            status = "法人與權證資金同向偏空"
        elif inst_class == "接近中性":
            status = "法人買賣幅度有限"
        else:
            status = "法人與權證方向分歧"
        detail = re.sub(r"^資金交叉[:：]\s*", "", s).strip()
        return f"面向：法人面｜結果：{status}｜說明：{detail}｜方向：neutral"

    inferred_label = _infer_face_label_from_text(s, fallback="重點面")
    inferred_status = _infer_status_from_text(s, fallback="重點待確認")
    return f"面向：{inferred_label}｜結果：{inferred_status}｜說明：{s}｜方向：neutral"


def _derive_weekly_point_tone(point: str, ctx: dict) -> str:
    """技術／法人／權證由實際數據決定 tone；新聞保留 Gemini 結構化判斷。"""
    fields = _parse_report_point_fields(point)
    label = str(fields.get("label", "") or "")
    current_tone = normalize_report_tone(fields.get("tone", ""))
    full_text = str(point or "")
    if label == "下週觀察" or re.match(r"^下週觀察[:：]", full_text):
        return "watch"
    if "技術" in label:
        return _derive_technical_tone_from_ctx(ctx)
    if "法人" in label:
        classification = str(_get_weekly_institutional_context(ctx).get("classification", "") or "")
        if classification == "偏多":
            return "positive"
        if classification == "偏空":
            return "negative"
        return "neutral"
    if "權證" in label:
        required = _get_required_representative_branch_analysis(ctx)
        if required:
            branch = str(required.get("branch", "") or "")
            side = str(required.get("side", "") or "")
            if branch and branch in full_text:
                if "買" in side:
                    return "positive"
                if "賣" in side:
                    return "negative"
        net_value = float(ctx.get("total_net", 0) or 0)
        if net_value > 0:
            return "positive"
        if net_value < 0:
            return "negative"
        return "neutral"
    return current_tone or "neutral"


def _apply_programmatic_weekly_tones(points: List[str], ctx: dict) -> List[str]:
    out = []
    for point in points or []:
        canonical = _canonicalize_weekly_point_structure(point, ctx)
        if canonical:
            out.append(
                _replace_report_point_tone(
                    canonical,
                    _derive_weekly_point_tone(canonical, ctx),
                )
            )
    return out


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
    return f"weekly_keypoints_{safe_version}_structured_tone_v25"


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
    current_start, current_end, current_report_dates = _resolve_report_week_range(
        report_dates,
        fallback_count=WEEK_TRADING_DAYS,
    )
    if report_dates and pd.notna(current_start):
        previous_end_boundary = current_start - pd.Timedelta(days=1)
        previous_start_boundary = previous_end_boundary - pd.Timedelta(days=WARRANT_WEEK_CALENDAR_DAYS - 1)
        previous_report_dates = [
            d for d in report_dates
            if previous_start_boundary <= d <= previous_end_boundary
        ]
    else:
        previous_report_dates = []
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
1. 剛好 3 點；前 2 點分析本週已發生事件，第 3 點的 label 必須是「下週觀察」。
2. 每個 item 必須包含 label、status、detail、tone、confidence、evidence；第 3 點 label 為「下週觀察」且 tone 為 watch。
2-1. tone 只能是 positive、negative、neutral、mixed、watch，並依數據真正方向判斷；正負並存用 mixed，方向有限用 neutral。
3. 優先指出 previous_week_comparison 與本週之間的延續、反轉或方向分歧，不要只播報本週單一數字。
4. 點名分點時，只能使用代表性分點，並完整寫出本週方向與金額、歷史勝率、平均持有天數及歷史加權報酬率。
5. 技術面使用 price_ma_volume.ma_signal 與 price_volume_pattern.current_pattern_label；法人接近中性時不得誇大。
6. 每個數字都必須在 full_weekly_data 中找到；不得換算、推估、補數字或提供買賣建議。每點獨立完整並以句號結束。

好範例：
- {{"label":"權證面","status":"分點買盤延續","detail":"代表性分點本週維持買超，並結合完整歷史績效說明時間尺度。","tone":"positive","confidence":0.95,"evidence":["代表性分點本週維持買超"]}}
- {{"label":"下週觀察","status":"確認資金是否續強","detail":"若權證淨流向與法人方向同步改善，再觀察價量是否確認。","tone":"watch","confidence":0.90,"evidence":["權證淨流向與法人方向"]}}

壞範例：
- {{"label":"新聞面","status":"重點待確認","detail":"後續持續觀察。","tone":"positive","confidence":0.90,"evidence":[]}}
- {{"label":"技術面","status":"偏多","detail":"KD與MACD值得留意。","tone":"positive","confidence":0.90,"evidence":[]}}

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
1. 剛好輸出 3 點：前 2 點分析本週已發生事件，第 3 點的 label 必須是「下週觀察」。
2. 每個 points item 必須回傳 label、status、detail、tone、confidence、evidence；第 3 點 label 必須是「下週觀察」且 tone 必須是 watch。不得寫「重點待確認」「題材待觀察」等空句。
2-1. tone 只能是 positive、negative、neutral、mixed、watch。技術面、法人面與權證面的 tone 必須依 JSON 數據方向判斷；正負訊號並存使用 mixed，方向有限使用 neutral。
2-2. confidence 為 0 到 1；evidence 只列 JSON 中直接支持判斷的資料片段。
3. 優先比較 previous_week_comparison，指出權證淨流向、法人合計或 TOP5 分點的延續、反轉與方向分歧；不要只播報本週數字。
4. 點名分點時，只能使用代表性分點，並完整使用本週方向與金額、歷史勝率、平均持有天數、歷史加權報酬率；小額分點不得點名。
5. 技術面使用 price_ma_volume.ma_signal 與 price_volume_pattern.current_pattern_label；法人分類為接近中性時，只能描述方向有限或尚未明確。
6. 所有數字都必須在 JSON 中找到，不得換算、推估或補充；不得提供買賣建議。每點需獨立完整並以句號結束。

好範例：
- {{"label":"權證面","status":"權證買盤連續增強","detail":"本週淨流向較上週改善，代表性分點同步買超並有完整歷史績效支持。","tone":"positive","confidence":0.95,"evidence":["本週淨流向較上週改善"]}}
- {{"label":"下週觀察","status":"確認價量能否轉強","detail":"若權證與法人方向同步改善，再觀察程式判定型態是否獲得量能確認。","tone":"watch","confidence":0.90,"evidence":["權證與法人方向"]}}

壞範例：
- {{"label":"新聞面","status":"題材仍待確認","detail":"後續持續關注。","tone":"positive","confidence":0.90,"evidence":[]}}
- {{"label":"技術面","status":"偏多","detail":"KD、MACD與均線值得留意。","tone":"positive","confidence":0.90,"evidence":[]}}

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
        points = _apply_programmatic_weekly_tones(points, ctx)
        problems = _validate_weekly_points(points, payload, ctx)

        if problems:
            print("⚠️ Gemini 本週重點與下週觀察需要修正：" + "；".join(problems))
            if not WEEKLY_GEMINI_REPAIR_ENABLE:
                print("⚡ 最快模式：略過第 2 次 Gemini repair，直接使用既有條件式備援")
                return []
            repaired_points = _repair_weekly_expert_points(
                points,
                payload,
                ctx,
                stock_name,
                problems,
            )
            repaired_points = _apply_programmatic_weekly_tones(repaired_points, ctx)
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
        content = _strip_news_meta_noise(content)
        combined = _normalize_news_text(f"{title}。{content}")
        if aliases and not _news_text_matches_target_stock(combined, stock_code, stock_name):
            continue
        if not _passes_news_quality_gate(title, content or title, stock_code, stock_name):
            continue
        if _is_price_only_news_without_fundamentals(combined):
            continue
        # 純股價標題仍排除；但若同句有營收、EPS、需求、訂單等基本面字眼則保留。
        if _is_low_value_market_news(combined) and not _has_substantive_company_news(combined):
            continue
        # 避免 RSS title 與 description 完全相同時重複一次；只有標題本身具體時才允許當備援。
        if content and _title_compare_text(content) != _title_compare_text(title) and len(content) >= 18:
            text = combined
        elif _can_use_news_title_as_fact(title):
            text = title
        else:
            continue
        text = _strip_news_meta_noise(text)
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
    s = _strip_news_meta_noise(text)
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
    s = _strip_news_meta_noise(sentence)
    s = re.sub(r"^[•\-–—\d\.、\)）\s]+", "", s).strip()
    s = s.strip("。；;，, ")
    if not s:
        return ""

    label = _infer_news_label_from_text(s, fallback_label=label)
    fact = _compact_news_fact_text(s, max_len=54)
    if not fact:
        return ""
    # 不替產業綜述素材自行建立「目標公司 × 題材」關係；只保留原候選句內容。
    headline = _infer_news_headline(label, s)
    watch = _infer_news_watch(label, s).replace("追蹤", "後續看", 1)
    detail = f"{fact}；{watch}"
    tone = _fallback_tone_from_status(headline)
    if "年增" in s and "月減" in s:
        tone = "mixed"
    point = f"{label}｜結果：{headline}｜說明：{detail}｜方向：{tone}"
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
    # 最後一道規則式出口也採嚴格主體閘門。
    candidates = [
        candidate
        for candidate in candidates
        if _news_text_matches_target_stock(
            str(candidate.get("text", "") or ""),
            stock_code,
            stock_name,
        )
    ]
    seen_candidate_keys = {
        _title_compare_text(candidate.get("text", ""))
        for candidate in candidates
        if candidate.get("text")
    }
    for candidate in _collect_news_title_candidates(records, stock_code, stock_name) or []:
        candidate_key = _title_compare_text(candidate.get("text", ""))
        if not candidate_key or candidate_key in seen_candidate_keys:
            continue
        candidate_text = str(candidate.get("text", "") or "")
        if not _news_text_matches_target_stock(candidate_text, stock_code, stock_name):
            continue
        candidates.append(candidate)
        seen_candidate_keys.add(candidate_key)
    if not candidates:
        return []

    def make_clean_point(label: str, text: str) -> str:
        return _make_news_keypoint(label, text, stock_code, stock_name)

    points = []
    used_keys = set()
    target_points = max(0, min(3, int(NEWS_SUMMARY_MAX_POINTS)))

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

    return _clean_news_summary_points_for_stock(points, stock_code, stock_name)[:NEWS_SUMMARY_MAX_POINTS]


def _load_gsheet_news_points_cache_for_display(stock_code: str, stock_name: str, allow_stale: bool = False) -> List[str]:
    """直接讀取 Google Sheet 的 news_points 快取，供新聞區塊顯示使用。

    原本快取只在 _call_gemini_with_retry() 內讀取；如果 FinMind 沒抓到素材，
    流程會在 build_news_points() 提早 return，導致永遠不會讀到 Google Sheet 快取。
    這個函式放在 build_news_points() 前面直接查快取，確保當天跑過的新聞摘要能直接被圖片使用。
    """
    if LLM_CACHE_FORCE_REFRESH:
        return []
    stock_key = _clean_code(stock_code)
    if not stock_key:
        return []

    cached_text = load_daily_task_llm_cache(_news_points_cache_task(), stock_key)
    if not cached_text and GSHEET_LLM_CACHE_ENABLE and GSHEET_LLM_CACHE_READ_ENABLE:
        cached_text = load_gsheet_llm_cache(_news_points_cache_task(), stock_key, stock_name, prompt="")
    if cached_text:
        points = _clean_news_summary_points_for_stock(_parse_raw_points_from_llm(cached_text), stock_key, stock_name)
        if points:
            print(f"📦 直接使用當日新聞本機快取：{stock_key}｜{len(points)} 點")
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
    cleaned = _dedupe_news_points_by_event(
        cleaned, stock_code, stock_name, log_label="新聞顯示前"
    )
    return cleaned[:NEWS_DISPLAY_MAX_POINTS]

def build_news_points(stock_code: str, stock_name: str, news_items, ctx: dict | None = None, cache_lookup: bool = True) -> List[str]:
    """整理高品質公司新聞；允許 0 點，寧缺勿濫，不強制補足顯示數量。"""
    if cache_lookup:
        cached_points = _load_gsheet_news_points_cache_for_display(
            stock_code,
            stock_name,
            allow_stale=False,
        )
        if cached_points:
            return _finalize_news_points_for_display(
                cached_points,
                stock_code,
                stock_name,
                ctx,
            )

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
    fallback_records = []
    for r in records:
        if not r.get("fallback_ok"):
            continue
        title = _clean_news_title(r.get("title", ""))
        content = _normalize_news_text(r.get("content", ""))
        content_ok = bool(
            content
            and _passes_news_quality_gate(title, content, stock_code, stock_name)
        )
        title_fact_ok = bool(
            not content
            and _can_use_news_title_as_fact(title)
            and _passes_news_quality_gate(title, title, stock_code, stock_name)
        )
        if content_ok or title_fact_ok:
            fallback_records.append(r)

    usable_records = body_records + [r for r in fallback_records if r not in body_records]
    usable_records = _dedupe_news_articles_by_event(
        usable_records,
        stock_code,
        stock_name,
        log_label="新聞管線送模前",
    )
    if not usable_records:
        # 沒有當期合格素材時直接回傳 0 點，不以舊日期新聞或大盤題材填補。
        return []

    # 無論素材是完整摘要或 FinMind 原始標題，都使用同一套 Gemini 結構化規則、
    # evidence 驗證、數字接地、事件級去重、repair 與 supplement 流程。
    ai_points = _summarize_news_with_gemini(
        usable_records,
        stock_code,
        stock_name,
    )
    final_points = (
        _finalize_news_points_for_display(
            ai_points,
            stock_code,
            stock_name,
            ctx,
        )
        if ai_points
        else []
    )
    if final_points:
        return final_points

    # Gemini 合法回傳 0 點或驗證後全數刪除時，只允許句子本身含本公司的規則式備援。
    rule_points = _rule_based_news_summary(
        usable_records,
        stock_code,
        stock_name,
    )
    final_points = _finalize_news_points_for_display(
        rule_points,
        stock_code,
        stock_name,
        ctx,
    )
    if final_points:
        _save_validated_news_points_cache(
            _news_points_cache_task(),
            stock_code,
            stock_name,
            f"rule_based_news_cache::{stock_code}::{_taipei_today_str()}",
            final_points,
            note="validated_rule_based_direct_subject_news_points",
        )
        return final_points

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

    股價資料有時會比 FinMind 三大法人晚一天，原本三大法人圖完全綁定
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

def plot_weekly_report(stock_code: str, stock_name: str, stock_df: pd.DataFrame, warrant_events: pd.DataFrame, news_items: List[dict], precomputed_news_points: List[str] | None = None, precomputed_ctx: dict | None = None):
    ctx = precomputed_ctx if isinstance(precomputed_ctx, dict) else build_weekly_context(stock_df, warrant_events, WEEK_TRADING_DAYS)
    ctx["stock_code"] = stock_code
    plot_df = ctx["plot_df"].copy()
    plot_events = ctx["plot_events"]
    week_events = ctx["week_events"]
    x = list(range(len(plot_df)))

    # 權證資金流改用「股價日期 + 權證事件日期」合併日期軸。
    # 避免股價最新日尚未更新，但 FinMind 權證分點資料已經有今日資料時，
    # 今日權證資金流被 plot_df.index.max() 擋掉。
    warrant_flow_dates = build_flow_axis_dates(plot_df, plot_events)
    warrant_x = list(range(len(warrant_flow_dates)))
    warrant_date_labels = [pd.Timestamp(d).strftime("%m-%d") for d in warrant_flow_dates]
    daily_net = daily_warrant_net_from_dates(warrant_flow_dates, plot_events)

    # 三大法人圖允許使用 FinMind 已更新、但股價尚未更新的最新法人日期。
    inst_plot_df = build_institutional_axis_df(plot_df, stock_df)
    x_inst = list(range(len(inst_plot_df)))

    # 精選分點資金流改用「股價日期 + 精選分點權證事件日期」合併日期軸。
    # 避免股價最新日尚未更新，但 FinMind 權證分點資料已經有今日資料時，
    # 今日精選分點大買 / 大賣被 plot_df.index.max() 擋掉。
    selected_branch_events_all = ctx.get("_selected_branch_events_all_cache")
    if selected_branch_events_all is None:
        selected_branch_events_all = filter_selected_branch_flow_events(warrant_events)
    else:
        selected_branch_events_all = selected_branch_events_all.copy()
    selected_flow_dates = build_flow_axis_dates(plot_df, selected_branch_events_all)
    selected_x = list(range(len(selected_flow_dates)))
    selected_date_labels = [pd.Timestamp(d).strftime("%m-%d") for d in selected_flow_dates]
    if selected_flow_dates:
        selected_branch_events = filter_events_by_date_range(
            selected_branch_events_all,
            selected_flow_dates[0],
            selected_flow_dates[-1],
        )
        selected_week_start = ctx.get("week_start", pd.NaT)
        selected_week_end = ctx.get("week_end", pd.NaT)
        selected_branch_week_events = filter_events_by_date_range(
            selected_branch_events,
            selected_week_start,
            selected_week_end,
        ) if pd.notna(selected_week_start) and pd.notna(selected_week_end) else pd.DataFrame()
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
        if precomputed_news_points is not None:
            news_points = _finalize_news_points_for_display(
                precomputed_news_points,
                stock_code,
                stock_name,
                ctx,
            )
            print(f"♻️ 使用平行新聞管線結果：{stock_code}｜{len(news_points)} 點")
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
    warrant_data_end = ctx.get("warrant_data_end", pd.NaT)
    warrant_period_note = ""
    if pd.notna(warrant_data_end) and pd.notna(ctx.get("week_end")) and pd.Timestamp(warrant_data_end).normalize() < pd.Timestamp(ctx.get("week_end")).normalize():
        warrant_period_note = f"｜權證資料至 {pd.Timestamp(warrant_data_end).strftime('%Y/%m/%d')}"
    ax_header.text(0.01, 0.50, f"{stock_code} {stock_name}｜權證資金流週報", color=GOLD, fontsize=68, fontweight="bold", ha="left", va="center")
    ax_header.text(0.01, -0.10, f"週報區間：{period}{warrant_period_note}｜資訊僅供參考", color=MUTED, fontsize=32, ha="left", va="center")
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

    selected_branch_names_for_render = list(ctx.get("_selected_branch_names_snapshot") or _get_selected_branch_flow_list())
    selected_branch_label = "、".join(selected_branch_names_for_render)
    print(f"🖼️ 圖片實際使用精選分點：{selected_branch_label or '未設定'}")
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
                # 先把可見行合併後往回找最後一個完整句號，再重新換行；
                # 找不到完整句時才退回硬切，避免把「第2季」「需留」這種殘片補成句號。
                visible_text = "".join(lines[:max_lines_int]).strip()
                sentence_idx = max(
                    visible_text.rfind("。"),
                    visible_text.rfind("！"),
                    visible_text.rfind("？"),
                )
                if sentence_idx < 0:
                    # 一整句只有句尾句號、但句號落在行數預算外時，
                    # 改在最後一個完整子句邊界收尾，避免硬切成「第2季。」「需留。」等殘句。
                    sentence_idx = max(
                        visible_text.rfind("；"),
                        visible_text.rfind(";"),
                        visible_text.rfind("，"),
                        visible_text.rfind(","),
                    )
                if sentence_idx >= 0:
                    boundary_char = visible_text[sentence_idx]
                    visible_text = visible_text[:sentence_idx + 1].strip()
                    if boundary_char in "；;，,":
                        visible_text = visible_text[:-1].rstrip() + "。"
                    rebuilt = []
                    current = ""
                    for ch in visible_text:
                        prefix = first_prefix if not rebuilt else next_prefix
                        candidate = current + ch
                        if measure_px(prefix + candidate) <= max_width_px or not current:
                            current = candidate
                        else:
                            rebuilt.append(current.rstrip())
                            current = ch.lstrip()
                    if current:
                        rebuilt.append(current.rstrip())
                    lines = rebuilt[:max_lines_int]
                else:
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
                "面向", "結果", "說明", "分類", "結論", "依據", "重點", "觀察", "條件", "追蹤", "影響", "狀態", "方向", "tone", "信心", "證據",
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
            s = re.sub(r"^(面向|結果|說明|結論|依據|重點|觀察|條件|追蹤|影響|狀態|方向|tone|信心|證據)[:：]", "", s)
            s = re.sub(r"｜\s*(面向|結果|說明|結論|依據|重點|觀察|條件|追蹤|影響|狀態|方向|tone|信心|證據)[:：]", "。", s)
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

            # 優先保留 max_chars 以內最後一個完整句；不再要求句號必須落在後半段，
            # 避免已有完整前句卻仍硬切成「第2季。」「需留。」等殘句。
            prefix = s[:max_chars]
            sentence_ends = [m.end() for m in re.finditer(r"[。！？]", prefix)]
            if sentence_ends:
                return finish(prefix[:sentence_ends[-1]])

            # 沒有完整句時，切在最接近的分號或逗號；最後才硬切。
            clause_positions = [prefix.rfind(p) for p in ["；", ";", "，", ",", "、"]]
            clause_idx = max(clause_positions)
            if clause_idx >= max(24, int(max_chars * 0.45)):
                return finish(prefix[:clause_idx])

            # 最後才保留前段，但會去掉看起來未完成的尾巴。
            return finish(prefix)

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

        def _compact_status_text(status_text, max_chars=15, fallback="重點待確認", label="", body=""):
            """產生第一行上色的具體結論短句；過短時由說明補強，過長時先去除公司名，再依標點或內文提煉完整截斷。"""
            raw = _normalize_card_text(status_text)
            raw = re.sub(r"^(結論|結果|狀態)[:：]\s*", "", raw).strip("。；;，,、 ")
            if len(raw) > max_chars:
                for alias in (stock_name, stock_code):
                    alias_text = _normalize_card_text(alias).strip("。；;，,、 ")
                    if alias_text and raw.startswith(alias_text) and len(raw) - len(alias_text) >= 6:
                        raw = raw[len(alias_text):].lstrip("：:，, ")
                        break
            if (not raw) or _is_generic_status_phrase(raw) or len(raw) < 6:
                derived = _derive_headline_from_body(label, body, fallback=raw or fallback)
                if len(derived) > len(raw):
                    raw = derived
            if len(raw) > max_chars:
                seg = re.split(r"[，、；;]", raw)[0].strip("。；;，,、 ")
                if 6 <= len(seg) <= max_chars:
                    raw = seg
                else:
                    derived = _derive_headline_from_body(label, body, fallback="")
                    raw = derived if 6 <= len(derived) <= max_chars else raw[:max_chars].rstrip("。；;，,、 ")
            return raw or fallback

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
                # 技術面、法人面與權證面一律由實際數據覆蓋 AI / 備援 tone；
                # 新聞等其他面向才保留既有結構化 tone。
                tone = _derive_weekly_point_tone(s, ctx)
                rows.append((label, status, body, 3, tone))
                if len(rows) >= 3:
                    break
            tech_card = _build_technical_card_summary(ctx)
            if tech_card and not any(str(r[0]) == "技術面" for r in rows):
                tech_row = (
                    "技術面",
                    str(tech_card.get("headline", "技術訊號待確認") or "技術訊號待確認"),
                    str(tech_card.get("detail", "目前技術訊號仍需確認。") or "目前技術訊號仍需確認。"),
                    3,
                    _derive_technical_tone_from_ctx(ctx),
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
                rows.append(("重點面", "重點待確認", "本週暫無足夠明確資料可整理成重點。", 2, "neutral"))
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
                tone = _extract_report_tone_from_point(s) or "neutral"
                rows.append((label, status, body, 3, tone))
            if not rows:
                rows.append(("新聞面", f"本週無與{stock_name}直接相關之重大新聞", "經公司主體與原文證據驗證後，本週未篩選到可直接支持的重大事件。", 3, "neutral"))
            return rows[:NEWS_DISPLAY_MAX_POINTS]

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
            for idx, section in enumerate(sections):
                if len(section) >= 5:
                    label, status, body, max_lines, tone = section[:5]
                else:
                    label, status, body, max_lines = section[:4]
                    tone = ""
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
                    color=get_report_tone_color(tone, status),
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
# FinMind-only 唯一市場資料來源
# ============================================================
# 市場資料正式只使用下方 FinMind 實作；歷史權證分點使用全市場 Parquet，最新交易日使用權證代號 API。
# TWSE／TPEx 權證 OpenAPI 僅保留發行商辨識與當日成交量完整性驗證，不作為資金流來源。
#
# Google Sheet 只保留 FinMind 權證結果快照、Gemini 當日摘要快取與使用者勝率統計。

FINMIND_BUILD_VERSION = "2026-07-16-finmind-probe-issuer-vectorized-v33"
FINMIND_PERFORMANCE_PATCH = "probe-first+issuer-alias-precompiled+lru+unique-combo-map+skip-renormalize+timed-context"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_STORAGE_URL = "https://api.finmindtrade.com/api/v4/storage_objects"
FINMIND_WARRANT_BRANCH_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_warrant_trading_daily_report"
FINMIND_REQUEST_RETRIES = max(1, int(os.getenv("FINMIND_REQUEST_RETRIES", "5")))
FINMIND_RETRY_BASE_WAIT = max(0.2, float(os.getenv("FINMIND_RETRY_BASE_WAIT", "1.5")))
FINMIND_CONNECT_TIMEOUT = max(3.0, float(os.getenv("FINMIND_CONNECT_TIMEOUT", "10")))
FINMIND_READ_TIMEOUT = max(15.0, float(os.getenv("FINMIND_READ_TIMEOUT", "180")))
# FinMind 額度超限與授權失敗分流：
# - 401 / 403，以及不含額度關鍵字的 402：視為 Token／方案權限錯誤，立即中止。
# - 402 / 429 且訊息包含 upper limit、reach、quota、rate limit 等：視為每小時額度超限，等待後重試。
FINMIND_RATE_LIMIT_RETRIES = max(0, int(os.getenv("FINMIND_RATE_LIMIT_RETRIES", "4")))
FINMIND_RATE_LIMIT_BASE_WAIT = max(1.0, float(os.getenv("FINMIND_RATE_LIMIT_BASE_WAIT", "60")))
FINMIND_RATE_LIMIT_MAX_WAIT = max(
    FINMIND_RATE_LIMIT_BASE_WAIT,
    float(os.getenv("FINMIND_RATE_LIMIT_MAX_WAIT", "300")),
)
FINMIND_WARRANT_DOWNLOAD_WORKERS = max(1, int(os.getenv("FINMIND_WARRANT_DOWNLOAD_WORKERS", "4")))
FINMIND_CACHE_DIR = os.getenv("FINMIND_CACHE_DIR", "finmind_cache").strip() or "finmind_cache"
FINMIND_WARRANT_DAY_CACHE_DIR = os.path.join(FINMIND_CACHE_DIR, "warrant_daily")

# 全市場共用精簡事件快取：每天只把原始全市場 Parquet 正規化與聚合一次。
# 手動搜尋任何新股票時，只需以權證代號掃描這批共用小檔，不再重做 60～70 日全市場解析。
FINMIND_MARKET_COMPACT_CACHE_ENABLE = os.getenv(
    "FINMIND_MARKET_COMPACT_CACHE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_MARKET_COMPACT_CACHE_VERSION = os.getenv(
    "FINMIND_MARKET_COMPACT_CACHE_VERSION",
    "v1",
).strip() or "v1"
FINMIND_MARKET_COMPACT_CACHE_DIR = os.path.join(
    FINMIND_CACHE_DIR,
    f"warrant_market_compact_{FINMIND_MARKET_COMPACT_CACHE_VERSION}",
)
FINMIND_MARKET_COMPACT_ROW_GROUP_SIZE = max(
    1000,
    int(os.getenv("FINMIND_MARKET_COMPACT_ROW_GROUP_SIZE", "50000")),
)
FINMIND_REFERENCE_CACHE_DIR = os.path.join(FINMIND_CACHE_DIR, "reference")
FINMIND_PREWARM_ONLY = os.getenv(
    "WARRANT_PREWARM_ONLY",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
FINMIND_PREWARM_CALENDAR_DAYS = max(
    110,
    int(os.getenv("FINMIND_PREWARM_CALENDAR_DAYS", "150")),
)
FINMIND_PREWARM_WORKERS = max(
    1,
    int(os.getenv("FINMIND_PREWARM_WORKERS", "4")),
)
FINMIND_PREWARM_REFRESH_RECENT_DAYS = max(
    0,
    int(os.getenv("FINMIND_PREWARM_REFRESH_RECENT_DAYS", "2")),
)
FINMIND_PREWARM_KEEP_RAW_DAYS = max(
    0,
    int(os.getenv("FINMIND_PREWARM_KEEP_RAW_DAYS", "7")),
)
# 多股票時，每個交易日的全市場 Parquet 只解析一次：
# 先建立所有目標股票有效認購權證代號聯集，再一次讀取、聚合並依標的股拆分。
FINMIND_MULTI_STOCK_DAILY_READ_ONCE_ENABLE = os.getenv(
    "FINMIND_MULTI_STOCK_DAILY_READ_ONCE_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_MULTI_STOCK_PREFETCH_MIN_STOCKS = max(2, int(os.getenv(
    "FINMIND_MULTI_STOCK_PREFETCH_MIN_STOCKS",
    "2",
)))
FINMIND_MULTI_STOCK_PREFETCH_CALENDAR_DAYS = max(90, int(os.getenv(
    "FINMIND_MULTI_STOCK_PREFETCH_CALENDAR_DAYS",
    "150",
)))
FINMIND_WARRANT_LATEST_PROBE_DAYS = max(1, int(os.getenv("FINMIND_WARRANT_LATEST_PROBE_DAYS", "7")))
# TaiwanStockTradingDate 可能尚未反映颱風等臨時休市；權證分點實際下載日期
# 改以高流動性 ETF 的 TaiwanStockPrice 實際成交日期為準。
FINMIND_TRADING_DATE_REFERENCE_STOCK = (
    os.getenv("FINMIND_TRADING_DATE_REFERENCE_STOCK", "0050").strip() or "0050"
)
FINMIND_STRICT_WARRANT_COMPLETENESS = os.getenv(
    "FINMIND_STRICT_WARRANT_COMPLETENESS",
    "1",
).strip().lower() not in ("0", "false", "no", "off")

# FinMind 欄位／分點對照 Debug：
# 預設開啟。遇到欄位不足、分點名稱無法對照或資料為空時，會把實際欄位、dtype、
# 前幾筆資料與候選分點代碼印到 GitHub Actions log，方便直接複製回來檢查。
FINMIND_DEBUG_SCHEMA_ENABLE = os.getenv(
    "FINMIND_DEBUG_SCHEMA_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_DEBUG_MAX_ROWS = max(1, int(os.getenv("FINMIND_DEBUG_MAX_ROWS", "20")))
FINMIND_DEBUG_SELECTED_BRANCH_ENABLE = os.getenv(
    "FINMIND_DEBUG_SELECTED_BRANCH_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_DEBUG_VERBOSE_SUCCESS_ENABLE = os.getenv(
    "FINMIND_DEBUG_VERBOSE_SUCCESS_ENABLE",
    "0",
).strip().lower() in ("1", "true", "yes", "on")

# 權證發行商辨識：官方資料優先，官方資料缺欄位時才退回官方權證名稱／FinMind 權證名稱解析。
FINMIND_OFFICIAL_ISSUER_ENABLE = os.getenv(
    "FINMIND_OFFICIAL_ISSUER_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_OFFICIAL_ISSUER_DEBUG_ENABLE = os.getenv(
    "FINMIND_OFFICIAL_ISSUER_DEBUG_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
TWSE_WARRANT_ISSUER_OPENAPI_URL = os.getenv(
    "TWSE_WARRANT_ISSUER_OPENAPI_URL",
    TWSE_WARRANT_DAILY_OPENAPI_URL,
).strip() or TWSE_WARRANT_DAILY_OPENAPI_URL
TPEX_WARRANT_ISSUER_OPENAPI_URL = os.getenv(
    "TPEX_WARRANT_ISSUER_OPENAPI_URL",
    "https://www.tpex.org.tw/openapi/v1/tpex_warrant_issue",
).strip() or "https://www.tpex.org.tw/openapi/v1/tpex_warrant_issue"

# 最新交易日固定改用 FinMind「query by 權證代號」API：
# 每支有效認購權證只需一個請求，即可取得該權證當日所有分點；歷史日期仍使用 Parquet。
# API 結果會完整取代同一天可能尚未同步完成的 Parquet，避免半成品混入正式週報。
FINMIND_WARRANT_LATEST_DAY_API_ENABLE = os.getenv(
    "FINMIND_WARRANT_LATEST_DAY_API_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_WARRANT_LATEST_DAY_API_WORKERS = max(
    1,
    int(os.getenv("FINMIND_WARRANT_LATEST_DAY_API_WORKERS", "8")),
)
FINMIND_WARRANT_PIPELINE_PARALLEL_ENABLE = os.getenv(
    "FINMIND_WARRANT_PIPELINE_PARALLEL_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_WARRANT_LATEST_DAY_API_STRICT = os.getenv(
    "FINMIND_WARRANT_LATEST_DAY_API_STRICT",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# 最新交易日若所有逐權證請求都成功、但完全沒有成交資料，代表 FinMind 當日權證資料
# 多半尚未更新完成。此情況不是 API 失敗，預設保留歷史資料並自動回退到最近已有資料的交易日；
# 欄位缺漏、授權失敗、請求失敗或部分權證失敗仍維持嚴格報錯。
FINMIND_WARRANT_LATEST_DAY_EMPTY_FALLBACK_ENABLE = os.getenv(
    "FINMIND_WARRANT_LATEST_DAY_EMPTY_FALLBACK_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# 最新日權證母體不能只相信 Summary 的有效期間：
# 1. 補入最近 N 個歷史交易日實際出現的權證。
# 2. 直接查詢本次精選分點當日資料，發現漏網權證後再補做逐權證全分點查詢。
# 3. 最後以精選分點端點回傳值核對逐權證端點；若有缺漏或金額差異，僅替換該分點該權證列。
FINMIND_WARRANT_LATEST_DAY_HISTORY_BACKFILL_TRADING_DAYS = max(
    1,
    int(os.getenv("FINMIND_WARRANT_LATEST_DAY_HISTORY_BACKFILL_TRADING_DAYS", "10")),
)
FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_DISCOVERY_ENABLE = os.getenv(
    "FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_DISCOVERY_ENABLE",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_STRICT = os.getenv(
    "FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_STRICT",
    "1",
).strip().lower() not in ("0", "false", "no", "off")
# 可手動指定名稱到 ID，例如：第一金中壢=5380,華南永昌台中=9A9g
# 正常情況不需要；只有 FinMind 對照表名稱真的無法辨識時才使用。
FINMIND_SELECTED_BRANCH_ID_OVERRIDES_RAW = os.getenv(
    "FINMIND_SELECTED_BRANCH_ID_OVERRIDES",
    "",
).strip()

# 週報區間恢復成第一張圖的口徑：最新資料日往前含當日共 7 個日曆日。
# 例如最新日 2026/07/14，週報區間即為 2026/07/08～2026/07/14；
# 休市日不會產生資料列，但仍保留正確的日曆週邊界。
WARRANT_WEEK_RANGE_MODE = os.getenv(
    "WARRANT_WEEK_RANGE_MODE",
    "calendar7",
).strip().lower() or "calendar7"
WARRANT_WEEK_CALENDAR_DAYS = max(1, int(os.getenv("WARRANT_WEEK_CALENDAR_DAYS", "7")))

_FINMIND_STOCK_INFO_CACHE = None
_FINMIND_STOCK_INFO_WITH_WARRANT_CACHE = None
_FINMIND_SECURITIES_TRADER_INFO_CACHE = None
_FINMIND_TRADING_DATE_CACHE = {}
_FINMIND_WARRANT_SUMMARY_CACHE = {}
_FINMIND_SELECTED_BRANCH_ID_CACHE = {}
_FINMIND_OFFICIAL_WARRANT_ISSUER_CACHE = None
_FINMIND_OFFICIAL_WARRANT_ISSUER_FUTURE = None
_FINMIND_OFFICIAL_WARRANT_ISSUER_EXECUTOR = None
_FINMIND_OFFICIAL_WARRANT_ISSUER_LOCK = threading.RLock()
_FINMIND_DEBUG_ONCE_KEYS = set()
_FINMIND_DATA_CACHE_LOCK = threading.RLock()
_FINMIND_STORAGE_LOCKS = {}
_FINMIND_STORAGE_LOCKS_GUARD = threading.Lock()
_FINMIND_MARKET_COMPACT_LOCKS = {}
_FINMIND_MARKET_COMPACT_LOCKS_GUARD = threading.Lock()
_FINMIND_RATE_LIMIT_GATE_LOCK = threading.Lock()
_FINMIND_RATE_LIMIT_UNTIL_MONOTONIC = 0.0
_FINMIND_WARRANT_RUN_STATS = {}
_FINMIND_MULTI_STOCK_PREFETCH_READY = False
_FINMIND_MULTI_STOCK_PREFETCH_RANGE = (pd.NaT, pd.NaT)
_FINMIND_MULTI_STOCK_EVENT_CACHE = {}
_FINMIND_MULTI_STOCK_SUMMARY_CACHE = {}
_FINMIND_MULTI_STOCK_NAME_MAP_CACHE = {}
_FINMIND_MULTI_STOCK_PREFETCH_LOCK = threading.RLock()

FINMIND_WARRANT_SOURCE_LABEL = "FinMind_TaiwanStockWarrantTradingDailyReport"

# 快取狀態欄位使用 FinMind 日期完整度名稱，避免與舊版欄位混淆。
GSHEET_WARRANT_STATUS_HEADERS = [
    "快取鍵", "標的股", "標的名稱", "快取起日", "快取迄日", "完整度狀態",
    "資料來源", "FinMind交易日總數", "FinMind成功日期", "FinMind空資料日期", "FinMind失敗日期",
    "資料筆數", "快照工作表", "更新時間",
]


def _require_finmind_token() -> str:
    token = os.getenv("FINMIND_API_TOKEN", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError(
            "找不到 FINMIND_API_TOKEN。請在 GitHub Actions 將 "
            "FINMIND_API_TOKEN 映射到 secrets.FINMIND_API_0714。"
        )
    return token


def _finmind_headers() -> dict:
    return {
        "Authorization": f"Bearer {_require_finmind_token()}",
        "User-Agent": HDR["User-Agent"],
        "Accept": "application/json, application/octet-stream, */*",
    }


def _finmind_error_message(payload, fallback: str = "") -> str:
    if isinstance(payload, dict):
        return str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("detail")
            or fallback
            or payload
        )
    return str(fallback or payload or "未知錯誤")


class FinMindAuthorizationError(RuntimeError):
    """FinMind Token、方案或資料集權限錯誤；等待不會改善，必須立即中止。"""


class FinMindRateLimitError(RuntimeError):
    """FinMind 每小時／短時間請求額度超限；已完成等待重試後仍失敗。"""


class FinMindBadRequestError(RuntimeError):
    """FinMind HTTP 400 或回傳狀態 400；相同參數重試不會改善，立即停止該請求。"""


def _finmind_error_payload_from_response(resp) -> tuple[dict, str]:
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    try:
        fallback = str(resp.text or "")[:500]
    except Exception:
        fallback = ""
    return payload, _finmind_error_message(payload, fallback)


def _finmind_is_rate_limit(status_code, message: str) -> bool:
    """只把明確的額度訊息視為可等待重試，避免把真正的 402 方案權限問題誤當限流。"""
    try:
        status = int(status_code)
    except Exception:
        status = 0
    msg = str(message or "").strip().lower()
    if status not in (402, 429):
        return False
    tokens = (
        "upper limit", "reach", "reached", "rate limit", "request limit",
        "too many request", "too many requests", "quota", "exceed", "exceeded",
        "hourly limit", "per hour", "frequency limit", "額度", "上限",
        "請求次數", "每小時", "超限", "超過",
    )
    return any(token in msg for token in tokens)


def _finmind_rate_limit_wait_seconds(quota_attempt: int, resp=None) -> float:
    retry_after = 0.0
    if resp is not None:
        try:
            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
        except Exception:
            retry_after = 0.0
    exponential = FINMIND_RATE_LIMIT_BASE_WAIT * (2 ** max(0, int(quota_attempt) - 1))
    return min(FINMIND_RATE_LIMIT_MAX_WAIT, max(FINMIND_RATE_LIMIT_BASE_WAIT, retry_after, exponential))


def _finmind_wait_for_rate_limit_gate():
    """所有 FinMind worker 共用同一個限流閘門，避免多執行緒在等待期間繼續撞 API。"""
    with _FINMIND_RATE_LIMIT_GATE_LOCK:
        remaining = max(0.0, _FINMIND_RATE_LIMIT_UNTIL_MONOTONIC - time.monotonic())
    if remaining > 0:
        time.sleep(remaining)


def _finmind_wait_after_rate_limit(label: str, quota_attempt: int, message: str, resp=None):
    global _FINMIND_RATE_LIMIT_UNTIL_MONOTONIC
    if quota_attempt > FINMIND_RATE_LIMIT_RETRIES:
        raise FinMindRateLimitError(
            f"FinMind 額度超限，等待重試 {FINMIND_RATE_LIMIT_RETRIES} 次後仍未恢復："
            f"{label}｜{str(message or '')[:300]}"
        )
    wait_sec = _finmind_rate_limit_wait_seconds(quota_attempt, resp=resp)
    with _FINMIND_RATE_LIMIT_GATE_LOCK:
        target = time.monotonic() + wait_sec
        _FINMIND_RATE_LIMIT_UNTIL_MONOTONIC = max(_FINMIND_RATE_LIMIT_UNTIL_MONOTONIC, target)
        remaining = max(0.0, _FINMIND_RATE_LIMIT_UNTIL_MONOTONIC - time.monotonic())
    print(
        f"⏳ FinMind 額度超限，所有 worker 暫停後重試 {quota_attempt}/{FINMIND_RATE_LIMIT_RETRIES}｜"
        f"{label}｜等待 {remaining:.0f} 秒｜{str(message or '')[:220]}"
    )
    if remaining > 0:
        time.sleep(remaining)


def _finmind_debug_print_df(label: str, df: pd.DataFrame, max_rows: int | None = None, once_key: str = ""):
    """將 FinMind 實際欄位、型別與資料樣本印到 Actions log。

    once_key 有值時，同一執行只印一次，避免 60～70 個交易日重複洗版。
    """
    if not FINMIND_DEBUG_SCHEMA_ENABLE:
        return
    key = str(once_key or "").strip()
    if key:
        with _FINMIND_DATA_CACHE_LOCK:
            if key in _FINMIND_DEBUG_ONCE_KEYS:
                return
            _FINMIND_DEBUG_ONCE_KEYS.add(key)
    try:
        rows = max(1, int(max_rows or FINMIND_DEBUG_MAX_ROWS))
        if df is None:
            print(f"🧪 FinMind Debug｜{label}｜DataFrame=None")
            return
        print("=" * 110)
        print(f"🧪 FinMind Debug｜{label}")
        print(f"資料筆數：{len(df):,}")
        print(f"實際欄位：{list(df.columns)}")
        try:
            dtype_text = ", ".join(f"{c}={df[c].dtype}" for c in df.columns)
            print(f"欄位型別：{dtype_text}")
        except Exception:
            pass
        if not df.empty:
            preview = df.head(rows).copy()
            for col in preview.columns:
                if preview[col].dtype == object:
                    preview[col] = preview[col].astype(str).str.slice(0, 180)
            print(f"前 {min(rows, len(preview))} 筆：")
            print(preview.to_string(index=False))
        print("=" * 110)
    except Exception as exc:
        print(f"⚠️ FinMind Debug 輸出失敗：{label}｜{exc}")


def _finmind_branch_lookup_key(value: str) -> str:
    """分點對照鍵：保留券商與地名，移除公司型態及格式符號。"""
    s = html.unescape(str(value or "")).strip().replace("臺", "台")
    s = re.sub(r"(股份有限公司|有限公司|證券股份|證券公司|證券|分公司|營業處|營業部|辦事處)", "", s)
    s = re.sub(r"[\s　\-＿_－—–/\\|｜·．・•()（）［］\[\]{}｛｝]+", "", s)
    return s.strip().lower()


def _finmind_branch_root_key(value: str) -> str:
    """只供失敗時列出同券商候選，不拿來直接決定分點。"""
    key = _finmind_branch_lookup_key(value)
    locations = (
        "台北", "台中", "中壢", "桃園", "新竹", "高雄", "台南", "板橋", "中和", "內湖",
        "南屯", "敦南", "公益", "忠孝", "南京", "三重", "新店", "士林", "基隆", "彰化",
        "員林", "竹北", "竹科", "市政", "信義", "淡水", "頭份", "內壢", "東大", "古亭",
        "民權", "汐止", "虎尾", "東港", "苑裡", "民生", "三多", "土城", "建成", "溪湖",
    )
    for suffix in sorted(locations, key=len, reverse=True):
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[:-len(suffix)]
    return key


def _resolve_report_week_range(report_dates, fallback_count: int = WEEK_TRADING_DAYS):
    """回傳週報邊界與落在邊界內的實際資料日期。"""
    dates = sorted({pd.Timestamp(d).normalize() for d in (report_dates or []) if pd.notna(d)})
    if not dates:
        return pd.NaT, pd.NaT, []
    week_end = dates[-1]
    if WARRANT_WEEK_RANGE_MODE in {"calendar7", "calendar", "7d", "seven_days"}:
        week_start = week_end - pd.Timedelta(days=WARRANT_WEEK_CALENDAR_DAYS - 1)
        week_dates = [d for d in dates if week_start <= d <= week_end]
    else:
        count = max(1, int(fallback_count or WEEK_TRADING_DAYS))
        week_dates = dates[-count:]
        week_start = week_dates[0]
    return pd.Timestamp(week_start).normalize(), pd.Timestamp(week_end).normalize(), week_dates


def _finmind_get_data(
    dataset: str,
    data_id: str = "",
    start_date: str = "",
    end_date: str = "",
    extra_params: dict | None = None,
    allow_empty: bool = True,
) -> pd.DataFrame:
    params = {"dataset": str(dataset).strip()}
    if data_id:
        params["data_id"] = str(data_id).strip()
    if start_date:
        params["start_date"] = str(start_date).strip()
    if end_date:
        params["end_date"] = str(end_date).strip()
    if extra_params:
        params.update({k: v for k, v in extra_params.items() if v not in (None, "")})

    last_error = None
    normal_attempt = 0
    quota_attempt = 0
    while normal_attempt < FINMIND_REQUEST_RETRIES:
        try:
            _finmind_wait_for_rate_limit_gate()
            resp = get_thread_session().get(
                FINMIND_API_URL,
                headers=_finmind_headers(),
                params=params,
                timeout=(FINMIND_CONNECT_TIMEOUT, FINMIND_READ_TIMEOUT),
            )
            if resp.status_code == 400:
                payload, message = _finmind_error_payload_from_response(resp)
                raise FinMindBadRequestError(
                    f"FinMind 請求參數錯誤：HTTP 400｜dataset={dataset}｜"
                    f"data_id={data_id}｜{message}"
                )

            if resp.status_code in (401, 402, 403, 429):
                payload, message = _finmind_error_payload_from_response(resp)
                if _finmind_is_rate_limit(resp.status_code, message):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"dataset={dataset}｜data_id={data_id}",
                        quota_attempt,
                        message,
                        resp=resp,
                    )
                    continue
                raise FinMindAuthorizationError(
                    f"FinMind 授權或方案權限失敗：HTTP {resp.status_code}｜{message}"
                )

            resp.raise_for_status()
            payload = resp.json()
            status = payload.get("status", 200) if isinstance(payload, dict) else 200
            if str(status) not in ("200", "success", "True", "true"):
                message = _finmind_error_message(payload)
                if _finmind_is_rate_limit(status, message):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"dataset={dataset}｜data_id={data_id}",
                        quota_attempt,
                        message,
                    )
                    continue
                try:
                    status_code = int(status)
                except Exception:
                    status_code = 0
                if status_code == 400:
                    raise FinMindBadRequestError(
                        f"FinMind 請求參數錯誤：status=400｜dataset={dataset}｜"
                        f"data_id={data_id}｜{message}"
                    )
                raise RuntimeError(
                    f"FinMind API 回傳失敗：dataset={dataset}｜status={status}｜{message}"
                )

            data = payload.get("data", []) if isinstance(payload, dict) else []
            df = pd.DataFrame(data)
            if df.empty and not allow_empty:
                _finmind_debug_print_df(
                    f"{dataset} 空資料｜data_id={data_id}｜{start_date}～{end_date}",
                    df,
                )
                if isinstance(payload, dict):
                    print(f"🧪 FinMind Debug｜{dataset} 原始回應鍵：{list(payload.keys())}")
                    print(f"🧪 FinMind Debug｜{dataset} msg：{_finmind_error_message(payload)}")
                raise RuntimeError(
                    f"FinMind API 回傳空資料：dataset={dataset}｜data_id={data_id}｜"
                    f"start_date={start_date}｜end_date={end_date}"
                )
            if (
                FINMIND_DEBUG_VERBOSE_SUCCESS_ENABLE
                and dataset in {"TaiwanSecuritiesTraderInfo", "TaiwanStockNews"}
            ):
                _finmind_debug_print_df(
                    f"{dataset} 實際回傳格式",
                    df,
                    once_key=f"schema:{dataset}",
                )
            return df
        except (FinMindAuthorizationError, FinMindRateLimitError, FinMindBadRequestError):
            raise
        except Exception as exc:
            last_error = exc
            normal_attempt += 1
            if normal_attempt >= FINMIND_REQUEST_RETRIES:
                break
            wait_sec = FINMIND_RETRY_BASE_WAIT * normal_attempt
            print(
                f"⚠️ FinMind API 重試 {normal_attempt}/{FINMIND_REQUEST_RETRIES - 1}："
                f"dataset={dataset}｜{exc}｜等待 {wait_sec:.1f} 秒"
            )
            time.sleep(wait_sec)
    raise RuntimeError(f"FinMind API 最終失敗：dataset={dataset}｜{last_error}")


class FinMindStorageObjectUnavailable(RuntimeError):
    """指定日期沒有 sponsorpro 全市場 Parquet；屬不可重試的 HTTP 4xx。"""


def _finmind_storage_lock(date_s: str):
    with _FINMIND_STORAGE_LOCKS_GUARD:
        return _FINMIND_STORAGE_LOCKS.setdefault(date_s, threading.Lock())


def _finmind_warrant_day_path(trade_date) -> str:
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    return os.path.join(FINMIND_WARRANT_DAY_CACHE_DIR, f"{date_s}.parquet")


def _finmind_market_compact_lock(date_s: str):
    with _FINMIND_MARKET_COMPACT_LOCKS_GUARD:
        return _FINMIND_MARKET_COMPACT_LOCKS.setdefault(date_s, threading.Lock())


def _finmind_market_compact_day_path(trade_date) -> str:
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    return os.path.join(FINMIND_MARKET_COMPACT_CACHE_DIR, f"{date_s}.parquet")


def _finmind_market_compact_meta_path(trade_date) -> str:
    return _finmind_market_compact_day_path(trade_date) + ".meta.json"


def _finmind_trader_map_hash(trader_name_map: Dict[str, str] | None) -> str:
    payload = "\n".join(
        f"{str(k)}={str(v)}"
        for k, v in sorted((trader_name_map or {}).items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _finmind_reference_paths(cache_name: str) -> tuple[str, str]:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(cache_name or "reference")).strip("_") or "reference"
    return (
        os.path.join(FINMIND_REFERENCE_CACHE_DIR, f"{safe}.parquet"),
        os.path.join(FINMIND_REFERENCE_CACHE_DIR, f"{safe}.meta.json"),
    )


def _finmind_read_reference_df(cache_name: str, max_age_days: int) -> pd.DataFrame | None:
    data_path, meta_path = _finmind_reference_paths(cache_name)
    if not os.path.exists(data_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        updated = pd.to_datetime(meta.get("updated_at", ""), errors="coerce")
        if pd.isna(updated):
            return None
        updated = pd.Timestamp(updated).tz_localize(None).normalize()
        age_days = int((get_taipei_today_ts() - updated).days)
        if age_days < 0 or age_days > max(0, int(max_age_days)):
            return None
        df = pd.read_parquet(data_path)
        print(f"⚡ FinMind 參考資料磁碟快取命中：{cache_name}｜{len(df):,} 筆｜age={age_days}日")
        return df
    except Exception as exc:
        print(f"⚠️ FinMind 參考資料快取讀取失敗：{cache_name}｜{exc}")
        return None


def _finmind_write_reference_df(cache_name: str, df: pd.DataFrame):
    if df is None:
        return
    data_path, meta_path = _finmind_reference_paths(cache_name)
    _ensure_dir(FINMIND_REFERENCE_CACHE_DIR)
    tmp_data = f"{data_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    tmp_meta = f"{meta_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        df.to_parquet(tmp_data, index=False, compression="zstd")
        payload = {
            "cache_name": cache_name,
            "updated_at": get_taipei_today_ts().strftime("%Y-%m-%d"),
            "rows": int(len(df)),
            "build": FINMIND_BUILD_VERSION,
        }
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_data, data_path)
        os.replace(tmp_meta, meta_path)
    except Exception as exc:
        print(f"⚠️ FinMind 參考資料快取寫入失敗：{cache_name}｜{exc}")
        for path in [tmp_data, tmp_meta]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def _finmind_reference_json_path(cache_name: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(cache_name or "reference")).strip("_") or "reference"
    return os.path.join(FINMIND_REFERENCE_CACHE_DIR, f"{safe}.json")


def _finmind_read_reference_json(cache_name: str, max_age_days: int):
    path = _finmind_reference_json_path(cache_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        updated = pd.to_datetime(payload.get("updated_at", ""), errors="coerce")
        if pd.isna(updated):
            return None
        updated = pd.Timestamp(updated).tz_localize(None).normalize()
        age_days = int((get_taipei_today_ts() - updated).days)
        if age_days < 0 or age_days > max(0, int(max_age_days)):
            return None
        print(f"⚡ FinMind JSON 參考快取命中：{cache_name}｜age={age_days}日")
        return payload.get("data")
    except Exception as exc:
        print(f"⚠️ FinMind JSON 參考快取讀取失敗：{cache_name}｜{exc}")
        return None


def _finmind_write_reference_json(cache_name: str, data):
    path = _finmind_reference_json_path(cache_name)
    _ensure_dir(FINMIND_REFERENCE_CACHE_DIR)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": get_taipei_today_ts().strftime("%Y-%m-%d"),
                "build": FINMIND_BUILD_VERSION,
                "data": data,
            }, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(f"⚠️ FinMind JSON 參考快取寫入失敗：{cache_name}｜{exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _finmind_market_compact_day_is_valid(
    trade_date,
    trader_name_map: Dict[str, str] | None = None,
) -> bool:
    if not FINMIND_MARKET_COMPACT_CACHE_ENABLE:
        return False
    path = _finmind_market_compact_day_path(trade_date)
    meta_path = _finmind_market_compact_meta_path(trade_date)
    if not os.path.exists(path) or os.path.getsize(path) <= 0 or not os.path.exists(meta_path):
        return False
    required = {
        "Date", "branch", "broker_code", "warrant_code",
        "buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares", "side",
    }
    try:
        import pyarrow.parquet as pq
        schema_names = set(pq.ParquetFile(path).schema.names)
        if not required.issubset(schema_names):
            return False
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if str(meta.get("cache_version", "")) != FINMIND_MARKET_COMPACT_CACHE_VERSION:
            return False
        if trader_name_map is not None:
            expected_hash = _finmind_trader_map_hash(trader_name_map)
            if str(meta.get("trader_map_hash", "")) != expected_hash:
                return False
        return True
    except Exception:
        return False



def _finmind_build_market_compact_day(
    trade_date,
    trader_name_map: Dict[str, str],
    force_refresh: bool = False,
    allow_not_ready: bool = False,
    return_frame: bool = False,
):
    """將單日全市場原始 Parquet 正規化、聚合成所有股票共用的精簡事件檔。

    ``return_frame=True`` 時同時回傳本次已在記憶體完成的全市場精簡表，讓
    手動查詢第一支股票時不必為了寫共用快取再重讀一次 Parquet。
    """
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    path = _finmind_market_compact_day_path(date_s)
    meta_path = _finmind_market_compact_meta_path(date_s)
    _ensure_dir(FINMIND_MARKET_COMPACT_CACHE_DIR)

    with _finmind_market_compact_lock(date_s):
        if not force_refresh and _finmind_market_compact_day_is_valid(date_s, trader_name_map):
            if return_frame:
                try:
                    return path, pd.read_parquet(path)
                except Exception:
                    pass
            else:
                return path

        raw_path = _finmind_download_warrant_day(date_s, allow_not_ready=allow_not_ready)
        if not raw_path:
            return (None, pd.DataFrame()) if return_frame else None
        columns = [
            "securities_trader", "price", "buy", "sell",
            "securities_trader_id", "stock_id", "date",
        ]
        raw = pd.read_parquet(raw_path, columns=columns)
        if raw.empty:
            return (None, pd.DataFrame()) if return_frame else None

        raw = raw.copy()
        stock_ids = raw["stock_id"].astype(str).str.strip().str.upper().str.replace(r"\.0$", "", regex=True)
        numeric_5 = stock_ids.str.fullmatch(r"\d{5}", na=False)
        stock_ids.loc[numeric_5] = stock_ids.loc[numeric_5].str.zfill(6)
        raw["warrant_code"] = stock_ids
        raw["price"] = pd.to_numeric(raw["price"], errors="coerce").fillna(0.0)
        raw["buy"] = pd.to_numeric(raw["buy"], errors="coerce").fillna(0.0)
        raw["sell"] = pd.to_numeric(raw["sell"], errors="coerce").fillna(0.0)
        raw["buy_amount_row"] = raw["price"] * raw["buy"]
        raw["sell_amount_row"] = raw["price"] * raw["sell"]
        raw["broker_code"] = raw["securities_trader_id"].astype(str).str.strip()
        raw["raw_branch"] = raw["securities_trader"].astype(str).str.strip()
        raw["mapped_branch"] = raw["broker_code"].map(trader_name_map or {}).fillna("").astype(str)
        raw["branch"] = np.where(raw["mapped_branch"].str.strip() != "", raw["mapped_branch"], raw["raw_branch"])
        raw["branch"] = pd.Series(raw["branch"], index=raw.index).map(normalize_branch_name)
        raw = raw[(raw["warrant_code"] != "") & (raw["broker_code"] != "") & (raw["branch"] != "")]

        grouped = raw.groupby(
            ["warrant_code", "broker_code", "branch"],
            as_index=False,
            dropna=False,
            sort=False,
        ).agg(
            buy_shares_raw=("buy", "sum"),
            sell_shares_raw=("sell", "sum"),
            buy_amount=("buy_amount_row", "sum"),
            sell_amount=("sell_amount_row", "sum"),
        )
        grouped["Date"] = pd.Timestamp(date_s)
        grouped["buy_shares"] = grouped["buy_shares_raw"] / 1000.0
        grouped["sell_shares"] = grouped["sell_shares_raw"] / 1000.0
        grouped["net_amount"] = grouped["buy_amount"] - grouped["sell_amount"]
        grouped = grouped[
            (grouped["buy_amount"] != 0)
            | (grouped["sell_amount"] != 0)
            | (grouped["net_amount"] != 0)
        ].copy()
        grouped["side"] = np.where(grouped["net_amount"] >= 0, "買超", "賣超")
        grouped = grouped[[
            "Date", "branch", "broker_code", "warrant_code",
            "buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares", "side",
        ]].sort_values(["warrant_code", "broker_code"], kind="stable").reset_index(drop=True)

        tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        tmp_meta = f"{meta_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            grouped.to_parquet(
                tmp_path,
                index=False,
                engine="pyarrow",
                compression="zstd",
                row_group_size=FINMIND_MARKET_COMPACT_ROW_GROUP_SIZE,
            )
            meta = {
                "cache_version": FINMIND_MARKET_COMPACT_CACHE_VERSION,
                "date": date_s,
                "rows": int(len(grouped)),
                "trader_map_hash": _finmind_trader_map_hash(trader_name_map),
                "source_size": int(os.path.getsize(raw_path)),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "build": FINMIND_BUILD_VERSION,
            }
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(tmp_path, path)
            os.replace(tmp_meta, meta_path)
            print(
                f"✅ 全市場精簡權證事件快取：{date_s}｜{len(grouped):,} 筆｜"
                f"{os.path.getsize(path) / 1024 / 1024:.2f} MB"
            )
            return (path, grouped) if return_frame else path
        except Exception:
            for temp in [tmp_path, tmp_meta]:
                try:
                    if os.path.exists(temp):
                        os.remove(temp)
                except Exception:
                    pass
            raise



def _finmind_load_target_events_from_market_compact(
    summary_df: pd.DataFrame,
    jobs: List[tuple],
    warrant_name_map: Dict[str, str],
    stock_code: str,
    stock_name: str,
    trader_name_map: Dict[str, str],
) -> tuple[pd.DataFrame, set]:
    """一次掃描所有已預熱精簡日檔，取得任意新股票的完整區間事件。"""
    if not FINMIND_MARKET_COMPACT_CACHE_ENABLE or summary_df is None or summary_df.empty or not jobs:
        return pd.DataFrame(), set()

    valid_paths = []
    compact_dates = set()
    for day, _ in jobs:
        day_ts = pd.Timestamp(day).normalize()
        if _finmind_market_compact_day_is_valid(day_ts, trader_name_map):
            valid_paths.append(_finmind_market_compact_day_path(day_ts))
            compact_dates.add(day_ts)
    if not valid_paths:
        return pd.DataFrame(), set()

    all_codes = sorted(set(summary_df["stock_id"].astype(str).map(normalize_openapi_warrant_code)) - {""})
    if not all_codes:
        return pd.DataFrame(), compact_dates

    try:
        import pyarrow.dataset as ds
        dataset = ds.dataset(valid_paths, format="parquet")
        min_date = min(compact_dates).to_datetime64()
        max_date = max(compact_dates).to_datetime64()
        filter_expr = (
            ds.field("warrant_code").isin(all_codes)
            & (ds.field("Date") >= min_date)
            & (ds.field("Date") <= max_date)
        )
        table = dataset.to_table(filter=filter_expr)
        compact = table.to_pandas()
    except Exception as exc:
        print(f"⚠️ 全市場精簡快取單次掃描失敗，退回逐日原始 Parquet：{exc}")
        return pd.DataFrame(), set()

    if compact.empty:
        print(f"⚡ 全市場精簡快取命中：{stock_code}｜{len(compact_dates)} 日｜0 筆")
        return pd.DataFrame(), compact_dates

    compact["Date"] = pd.to_datetime(compact["Date"], errors="coerce").dt.normalize()
    compact["warrant_code"] = compact["warrant_code"].astype(str).map(normalize_openapi_warrant_code)
    intervals = summary_df[["stock_id", "listing_date", "last_trade_date"]].copy()
    intervals["stock_id"] = intervals["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    intervals = intervals.dropna(subset=["listing_date", "last_trade_date"]).drop_duplicates()
    merged = compact.merge(intervals, left_on="warrant_code", right_on="stock_id", how="inner")
    merged = merged[
        (merged["Date"] >= merged["listing_date"])
        & (merged["Date"] <= merged["last_trade_date"])
    ].copy()
    merged = merged.drop(columns=["stock_id", "listing_date", "last_trade_date"], errors="ignore")
    merged = merged.drop_duplicates(
        subset=["Date", "broker_code", "branch", "warrant_code", "buy_amount", "sell_amount"],
        keep="last",
    )
    merged["warrant_name"] = merged["warrant_code"].map(warrant_name_map).fillna(merged["warrant_code"])
    merged["underlying_code"] = _normalize_stock_name_code_key(stock_code)
    merged["underlying_name"] = str(stock_name or "")
    merged["side"] = np.where(pd.to_numeric(merged["net_amount"], errors="coerce").fillna(0.0) >= 0, "買超", "賣超")
    output = merged[[
        "Date", "branch", "broker_code", "warrant_code", "warrant_name",
        "underlying_code", "underlying_name", "buy_amount", "sell_amount",
        "net_amount", "buy_shares", "sell_shares", "side",
    ]].sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    output.attrs["_warrant_events_normalized"] = True
    print(
        f"⚡ 全市場精簡快取命中：{stock_code}｜{len(compact_dates)} 日｜"
        f"{len(output):,} 筆｜單次 Dataset 掃描"
    )
    return output, compact_dates


def _finmind_prewarm_market_compact_cache():
    """排程預熱：更新全市場共用精簡事件與每日參考資料，不產圖。"""
    start_clock = time.perf_counter()
    today = get_taipei_today_ts()
    start_date = today - pd.Timedelta(days=FINMIND_PREWARM_CALENDAR_DAYS - 1)
    print(
        f"🔥 開始預熱 FinMind 全市場快取｜{start_date.date()} ~ {today.date()}｜"
        f"workers={FINMIND_PREWARM_WORKERS}"
    )

    # 參考資料在預熱流程先更新，手動查任何新股票時直接讀本機快取。
    _finmind_load_stock_info(force_refresh=False)
    trader_name_map, _ = _finmind_securities_trader_maps()
    if not trader_name_map:
        raise RuntimeError("預熱失敗：TaiwanSecuritiesTraderInfo 無法建立分點對照")

    reference_executor = ThreadPoolExecutor(max_workers=3)
    reference_futures = [
        reference_executor.submit(_finmind_load_stock_info_with_warrant, False),
        reference_executor.submit(_finmind_official_warrant_issuer_map, False),
    ]
    if BRANCH_PERF_ENABLE:
        reference_futures.append(
            reference_executor.submit(read_gsheet_branch_perf_df, False)
        )

    trading_dates = _finmind_get_trading_dates(start_date, today)
    if not trading_dates:
        raise RuntimeError("預熱失敗：找不到實際交易日")

    if FINMIND_PREWARM_REFRESH_RECENT_DAYS > 0:
        cutoff = today - pd.Timedelta(days=FINMIND_PREWARM_REFRESH_RECENT_DAYS - 1)
        for day in trading_dates:
            if day < cutoff:
                continue
            for path in [
                _finmind_warrant_day_path(day),
                _finmind_market_compact_day_path(day),
                _finmind_market_compact_meta_path(day),
            ]:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as exc:
                    print(f"⚠️ 預熱更新舊檔刪除失敗：{path}｜{exc}")

    latest_available = None
    for day in reversed(trading_dates[-FINMIND_WARRANT_LATEST_PROBE_DAYS:]):
        probe = _finmind_download_warrant_day(day, allow_not_ready=True)
        if probe:
            latest_available = pd.Timestamp(day).normalize()
            break
    if latest_available is None:
        raise RuntimeError("預熱失敗：最近交易日沒有可用權證分點日檔")
    target_dates = [d for d in trading_dates if d <= latest_available]

    failures = []
    completed = 0
    with ThreadPoolExecutor(max_workers=FINMIND_PREWARM_WORKERS) as executor:
        future_map = {
            executor.submit(
                _finmind_build_market_compact_day,
                day,
                trader_name_map,
                False,
                False,
            ): day
            for day in target_dates
        }
        for future in as_completed(future_map):
            day = future_map[future]
            completed += 1
            try:
                future.result()
            except Exception as exc:
                failures.append((pd.Timestamp(day).strftime("%Y-%m-%d"), str(exc)))
                print(f"❌ 全市場精簡快取預熱失敗：{pd.Timestamp(day).date()}｜{exc}")
            if completed == 1 or completed % 10 == 0 or completed == len(target_dates):
                print(f"📊 全市場快取預熱進度：{completed}/{len(target_dates)}｜失敗={len(failures)}")

    for future in reference_futures:
        try:
            future.result()
        except Exception as exc:
            print(f"⚠️ 預熱參考資料工作失敗：{exc}")
    reference_executor.shutdown(wait=True)

    # 全市場精簡檔已可服務任何股票；只保留最近幾天原始檔，降低 Actions cache 體積與還原時間。
    if FINMIND_PREWARM_KEEP_RAW_DAYS >= 0:
        raw_cutoff = latest_available - pd.Timedelta(days=FINMIND_PREWARM_KEEP_RAW_DAYS)
        for raw_path in Path(FINMIND_WARRANT_DAY_CACHE_DIR).glob("*.parquet"):
            try:
                raw_date = pd.Timestamp(raw_path.stem).normalize()
                if raw_date < raw_cutoff and _finmind_market_compact_day_is_valid(raw_date, trader_name_map):
                    raw_path.unlink()
            except Exception:
                pass

    if failures and FINMIND_STRICT_WARRANT_COMPLETENESS:
        sample = "；".join(f"{d}:{e[:100]}" for d, e in failures[:5])
        raise RuntimeError(f"全市場快取預熱不完整：{len(failures)}/{len(target_dates)} 日｜{sample}")
    print(
        f"✅ 全市場快取預熱完成｜最新日={latest_available.date()}｜"
        f"交易日={len(target_dates)}｜耗時={time.perf_counter() - start_clock:.2f}秒"
    )


def _looks_like_json_or_html_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            prefix = f.read(32).lstrip().lower()
        return prefix.startswith(b"{") or prefix.startswith(b"[") or prefix.startswith(b"<html") or prefix.startswith(b"<!doctype")
    except Exception:
        return False


def _read_small_error_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(1200).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _finmind_download_warrant_day(trade_date, allow_not_ready: bool = False) -> str | None:
    """下載 sponsorpro 全市場權證分點 Parquet；同一執行中每個日期只下載一次。"""
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    path = _finmind_warrant_day_path(date_s)
    _ensure_dir(os.path.dirname(path))

    with _finmind_storage_lock(date_s):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                pd.read_parquet(path, columns=["date", "stock_id"])
                return path
            except Exception:
                try:
                    os.remove(path)
                except Exception:
                    pass

        last_error = None
        normal_attempt = 0
        quota_attempt = 0
        while normal_attempt < FINMIND_REQUEST_RETRIES:
            tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            try:
                _finmind_wait_for_rate_limit_gate()
                resp = get_thread_session().get(
                    FINMIND_STORAGE_URL,
                    headers=_finmind_headers(),
                    params={
                        "dataset": "TaiwanStockWarrantTradingDailyReport",
                        "date": date_s,
                    },
                    timeout=(FINMIND_CONNECT_TIMEOUT, FINMIND_READ_TIMEOUT),
                    stream=True,
                    allow_redirects=True,
                )

                if resp.status_code in (401, 402, 403, 429):
                    payload, message = _finmind_error_payload_from_response(resp)
                    if _finmind_is_rate_limit(resp.status_code, message):
                        quota_attempt += 1
                        _finmind_wait_after_rate_limit(
                            f"storage_objects｜date={date_s}",
                            quota_attempt,
                            message,
                            resp=resp,
                        )
                        continue
                    raise FinMindAuthorizationError(
                        "FinMind sponsorpro 權證分點權限失敗："
                        f"HTTP {resp.status_code}｜{message}"
                    )

                if resp.status_code in (400, 404, 422):
                    message = resp.text[:500]
                    if allow_not_ready:
                        print(
                            f"ℹ️ FinMind 權證分點尚無 {date_s}："
                            f"HTTP {resp.status_code}｜{message[:160]}"
                        )
                        return None
                    raise FinMindStorageObjectUnavailable(
                        f"FinMind 權證分點物件不存在：{date_s}｜"
                        f"HTTP {resp.status_code}｜{message[:300]}"
                    )

                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) <= 0:
                    raise RuntimeError("FinMind storage_objects 回傳空檔")

                if _looks_like_json_or_html_file(tmp_path):
                    message = _read_small_error_file(tmp_path)
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    if _finmind_is_rate_limit(402, message):
                        quota_attempt += 1
                        _finmind_wait_after_rate_limit(
                            f"storage_objects｜date={date_s}",
                            quota_attempt,
                            message,
                        )
                        continue
                    if allow_not_ready and any(
                        token in message.lower()
                        for token in ["no data", "not found", "尚無", "查無", "empty"]
                    ):
                        print(f"ℹ️ FinMind 權證分點尚無 {date_s}：{message[:200]}")
                        return None
                    raise RuntimeError(f"FinMind storage_objects 未回傳 Parquet：{message[:500]}")

                required_columns = [
                    "securities_trader", "price", "buy", "sell",
                    "securities_trader_id", "stock_id", "date",
                ]
                check_df = pd.read_parquet(tmp_path, columns=required_columns)
                missing = set(required_columns) - set(check_df.columns)
                if missing:
                    raise RuntimeError(f"FinMind 權證分點欄位不足：{sorted(missing)}")

                os.replace(tmp_path, path)
                print(
                    f"✅ FinMind 全市場權證分點下載：{date_s}｜"
                    f"{os.path.getsize(path) / 1024 / 1024:.2f} MB"
                )
                return path
            except (FinMindStorageObjectUnavailable, FinMindAuthorizationError, FinMindRateLimitError):
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                raise
            except Exception as exc:
                last_error = exc
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                normal_attempt += 1
                if normal_attempt >= FINMIND_REQUEST_RETRIES:
                    break
                wait_sec = FINMIND_RETRY_BASE_WAIT * normal_attempt
                print(
                    f"⚠️ FinMind 權證分點下載重試 {normal_attempt}/{FINMIND_REQUEST_RETRIES - 1}｜"
                    f"{date_s}｜{exc}｜等待 {wait_sec:.1f} 秒"
                )
                time.sleep(wait_sec)
        raise RuntimeError(f"FinMind 權證分點下載最終失敗：{date_s}｜{last_error}")


def _finmind_load_stock_info(force_refresh: bool = False) -> pd.DataFrame:
    global _FINMIND_STOCK_INFO_CACHE
    with _FINMIND_DATA_CACHE_LOCK:
        if _FINMIND_STOCK_INFO_CACHE is not None and not force_refresh:
            return _FINMIND_STOCK_INFO_CACHE.copy()
    df = None
    if not force_refresh:
        df = _finmind_read_reference_df("TaiwanStockInfo", max_age_days=1)
    if df is None:
        df = _finmind_get_data("TaiwanStockInfo", allow_empty=False).fillna("")
        _finmind_write_reference_df("TaiwanStockInfo", df)
    required = {"stock_id", "stock_name", "type"}
    missing = required - set(df.columns)
    if missing:
        _finmind_debug_print_df("TaiwanStockInfo 欄位不足", df)
        raise RuntimeError(f"FinMind TaiwanStockInfo 欄位不足：{sorted(missing)}｜實際欄位={df.columns.tolist()}")
    df = df.copy()
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["stock_name"] = df["stock_name"].astype(str).str.strip()
    with _FINMIND_DATA_CACHE_LOCK:
        _FINMIND_STOCK_INFO_CACHE = df.copy()
    print(f"📦 FinMind 股票總覽載入：{len(df):,} 筆")
    return df


def _finmind_load_stock_info_with_warrant(force_refresh: bool = False) -> pd.DataFrame:
    global _FINMIND_STOCK_INFO_WITH_WARRANT_CACHE
    with _FINMIND_DATA_CACHE_LOCK:
        if _FINMIND_STOCK_INFO_WITH_WARRANT_CACHE is not None and not force_refresh:
            return _FINMIND_STOCK_INFO_WITH_WARRANT_CACHE.copy()
    df = None
    if not force_refresh:
        df = _finmind_read_reference_df("TaiwanStockInfoWithWarrant", max_age_days=1)
    if df is None:
        df = _finmind_get_data("TaiwanStockInfoWithWarrant", allow_empty=False).fillna("")
        _finmind_write_reference_df("TaiwanStockInfoWithWarrant", df)
    required = {"stock_id", "stock_name"}
    missing = required - set(df.columns)
    if missing:
        _finmind_debug_print_df("TaiwanStockInfoWithWarrant 欄位不足", df)
        raise RuntimeError(f"FinMind TaiwanStockInfoWithWarrant 欄位不足：{sorted(missing)}｜實際欄位={df.columns.tolist()}")
    df = df.copy()
    df["stock_id"] = df["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    df["stock_name"] = df["stock_name"].astype(str).str.strip()
    with _FINMIND_DATA_CACHE_LOCK:
        _FINMIND_STOCK_INFO_WITH_WARRANT_CACHE = df.copy()
    print(f"📦 FinMind 股票與權證名稱總覽載入：{len(df):,} 筆")
    return df


def _finmind_load_securities_trader_info(force_refresh: bool = False) -> pd.DataFrame:
    """讀取 FinMind 證券商資訊表，供 securities_trader_id 還原完整分點名稱。"""
    global _FINMIND_SECURITIES_TRADER_INFO_CACHE
    with _FINMIND_DATA_CACHE_LOCK:
        if _FINMIND_SECURITIES_TRADER_INFO_CACHE is not None and not force_refresh:
            return _FINMIND_SECURITIES_TRADER_INFO_CACHE.copy()

    df = None
    if not force_refresh:
        # 正式產圖一律優先信任既有分點參考快取，即使超過七天也不在正式 job 臨時刷新。
        # 只有預熱流程會依七天 TTL 更新，避免正式產圖因名稱微調造成全部 compact hash 失效。
        reference_max_age_days = 7 if FINMIND_PREWARM_ONLY else 3650
        df = _finmind_read_reference_df(
            "TaiwanSecuritiesTraderInfo",
            max_age_days=reference_max_age_days,
        )
    if df is None:
        df = _finmind_get_data("TaiwanSecuritiesTraderInfo", allow_empty=False).fillna("")
        _finmind_write_reference_df("TaiwanSecuritiesTraderInfo", df)
    required = {"securities_trader_id", "securities_trader"}
    missing = required - set(df.columns)
    if missing:
        _finmind_debug_print_df("TaiwanSecuritiesTraderInfo 欄位不足", df)
        raise RuntimeError(
            f"FinMind TaiwanSecuritiesTraderInfo 欄位不足：{sorted(missing)}｜"
            f"實際欄位={df.columns.tolist()}"
        )

    out = df.copy()
    out["securities_trader_id"] = out["securities_trader_id"].astype(str).str.strip()
    out["securities_trader"] = out["securities_trader"].astype(str).str.strip()
    if "date" in out.columns:
        out["_sort_date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.sort_values(["securities_trader_id", "_sort_date"])
    out = out[(out["securities_trader_id"] != "") & (out["securities_trader"] != "")]
    out = out.drop_duplicates(subset=["securities_trader_id"], keep="last").copy()
    out["branch"] = out["securities_trader"].map(normalize_branch_name)
    out["branch_lookup_key"] = out["securities_trader"].map(_finmind_branch_lookup_key)
    out["branch_root_key"] = out["securities_trader"].map(_finmind_branch_root_key)
    out = out.drop(columns=["_sort_date"], errors="ignore")

    with _FINMIND_DATA_CACHE_LOCK:
        _FINMIND_SECURITIES_TRADER_INFO_CACHE = out.copy()
    print(f"📦 FinMind 證券商分點對照載入：{len(out):,} 個代碼")
    return out


def _finmind_securities_trader_maps() -> tuple[Dict[str, str], pd.DataFrame]:
    try:
        info = _finmind_load_securities_trader_info()
    except Exception as exc:
        print(f"⚠️ FinMind 證券商分點對照讀取失敗，暫用 Parquet 券商簡稱：{exc}")
        return {}, pd.DataFrame()
    name_map = dict(zip(
        info["securities_trader_id"].astype(str),
        info["branch"].astype(str),
    ))
    return name_map, info


def _debug_selected_branch_candidates(selected_names, events_df: pd.DataFrame | None = None):
    """分點無法對照時，把官方對照表與權證事件候選完整印出。"""
    if not FINMIND_DEBUG_SELECTED_BRANCH_ENABLE:
        return
    try:
        selected = [normalize_branch_name(x) for x in (selected_names or []) if normalize_branch_name(x)]
        info = _finmind_load_securities_trader_info()
        for requested in selected:
            requested_key = _finmind_branch_lookup_key(requested)
            requested_root = _finmind_branch_root_key(requested)
            print("=" * 110)
            print(f"🧪 精選分點 Debug｜使用者輸入={requested}｜lookup_key={requested_key}｜root={requested_root}")
            candidate_mask = (
                info["branch_lookup_key"].astype(str).str.contains(requested_key, regex=False)
                | info["branch_lookup_key"].astype(str).map(lambda x: requested_key in x if x else False)
                | info["branch_root_key"].astype(str).eq(requested_root)
            )
            candidates = info.loc[candidate_mask].copy()
            if candidates.empty and requested_root:
                candidates = info[
                    info["branch_lookup_key"].astype(str).str.contains(requested_root, regex=False)
                ].copy()
            show_cols = [c for c in [
                "securities_trader_id", "securities_trader", "branch", "date", "address", "phone",
                "branch_lookup_key", "branch_root_key",
            ] if c in candidates.columns]
            if candidates.empty:
                print("官方 TaiwanSecuritiesTraderInfo 找不到候選分點。")
            else:
                print(f"官方候選共 {len(candidates):,} 筆：")
                print(candidates[show_cols].head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))

            if events_df is not None and not events_df.empty:
                e = events_df.copy()
                e["branch"] = e.get("branch", "").astype(str).map(normalize_branch_name)
                e["broker_code"] = e.get("broker_code", "").astype(str).str.strip()
                event_mask = e["branch"].map(_finmind_branch_root_key).eq(requested_root)
                candidate_ids = set(candidates.get("securities_trader_id", pd.Series(dtype=str)).astype(str))
                if candidate_ids:
                    event_mask = event_mask | e["broker_code"].isin(candidate_ids)
                event_candidates = e.loc[event_mask].copy()
                if event_candidates.empty:
                    print("本次權證事件中沒有相符候選。")
                else:
                    agg = event_candidates.groupby(["broker_code", "branch"], as_index=False).agg(
                        rows=("net_amount", "size"),
                        buy_amount=("buy_amount", "sum"),
                        sell_amount=("sell_amount", "sum"),
                        net_amount=("net_amount", "sum"),
                    )
                    agg["abs_net"] = agg["net_amount"].abs()
                    agg = agg.sort_values(["abs_net", "rows"], ascending=[False, False]).drop(columns=["abs_net"])
                    print(f"本次權證事件候選共 {len(agg):,} 組：")
                    print(agg.head(FINMIND_DEBUG_MAX_ROWS).to_string(index=False))
            print("=" * 110)
    except Exception as exc:
        print(f"⚠️ 精選分點 Debug 失敗：{exc}")


def _finmind_market_label(raw_type: str) -> str:
    key = str(raw_type or "").strip().lower()
    return {
        "twse": "上市",
        "tpex": "上櫃",
        "otc": "上櫃",
        "emerging": "興櫃",
        "rotc": "興櫃",
    }.get(key, key or "FinMind")


def get_tw_stock_name(stock_code: str) -> str:
    """FinMind-only：優先讀 TaiwanStockInfo；暫缺時以股票代號作名稱並保留警告。"""
    code = _normalize_stock_name_code_key(stock_code)
    if not code:
        raise ValueError("股票代號不可為空")
    df = _finmind_load_stock_info()
    hit = df[df["stock_id"].astype(str) == code]
    if hit.empty:
        print(
            f"⚠️ FinMind TaiwanStockInfo 暫時找不到股票代號：{code}｜"
            f"本次先以代號「{code}」作為名稱，股價資料仍會繼續驗證"
        )
        return code
    name = str(hit.iloc[-1].get("stock_name", "") or "").strip()
    if not name:
        print(
            f"⚠️ FinMind TaiwanStockInfo 股票名稱為空：{code}｜"
            f"本次先以代號「{code}」作為名稱"
        )
        return code
    print(f"✅ 股票名稱查詢成功：{code} {name}｜來源：FinMind TaiwanStockInfo")
    return name


def fetch_stock_data_yf(stock_code: str, period="160d"):
    """保留舊函式名稱相容性；實際只呼叫 FinMind TaiwanStockPrice。"""
    code = _normalize_stock_name_code_key(stock_code)
    match = re.search(r"(\d+)", str(period or "160d"))
    calendar_days = max(120, int(match.group(1)) if match else 160)
    end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    start_dt = end_dt - timedelta(days=calendar_days)
    raw = _finmind_get_data(
        "TaiwanStockPrice",
        data_id=code,
        start_date=start_dt.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        allow_empty=False,
    ).fillna(0)
    required = {"date", "open", "max", "min", "close", "Trading_Volume"}
    missing = required - set(raw.columns)
    if missing:
        _finmind_debug_print_df(f"TaiwanStockPrice 欄位不足｜{code}", raw)
        raise RuntimeError(f"FinMind TaiwanStockPrice 欄位不足：{sorted(missing)}｜實際欄位={raw.columns.tolist()}")

    df = raw.rename(columns={
        "date": "Date",
        "open": "Open",
        "max": "High",
        "min": "Low",
        "close": "Close",
        "Trading_Volume": "Volume",
    })[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.tz_localize(None)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (
        df.dropna(subset=["Date", "Open", "High", "Low", "Close"])
        .drop_duplicates(subset=["Date"], keep="last")
        .sort_values("Date")
        .set_index("Date")
    )
    if df.empty:
        raise RuntimeError(f"FinMind TaiwanStockPrice 無有效股價：{code}")

    info = _finmind_load_stock_info()
    hit = info[info["stock_id"].astype(str) == code]
    market = _finmind_market_label(hit.iloc[-1].get("type", "")) if not hit.empty else "FinMind"
    print(
        f"✅ FinMind 股價資料：{code}｜{len(df):,} 筆｜"
        f"{df.index.min().date()} ~ {df.index.max().date()}"
    )
    return df, market, code


def fetch_inst_60d_from_finmind_token(stock_code: str, days: int = 80) -> pd.DataFrame:
    """FinMind-only：三大法人不再使用公開模式或 X_function 備援。"""
    code = _normalize_stock_name_code_key(stock_code)
    end_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    start_dt = end_dt - timedelta(days=max(int(days * 3.0), 160))
    raw = _finmind_get_data(
        "TaiwanStockInstitutionalInvestorsBuySell",
        data_id=code,
        start_date=start_dt.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        allow_empty=True,
    )
    if raw.empty:
        print(f"⚠️ FinMind 三大法人資料為空：{code}")
        return pd.DataFrame()
    out = _standardize_institutional_long_df(raw.fillna(0), code, days, "FinMind")
    if out is None or out.empty:
        raise RuntimeError(f"FinMind 三大法人欄位無法標準化：{code}｜{raw.columns.tolist()}")
    return out


def fetch_inst_60d_from_x(stock_code: str, days: int = 80) -> pd.DataFrame:
    """保留舊函式名稱；只使用 FinMind。"""
    return fetch_inst_60d_from_finmind_token(stock_code, days=days)


def _finmind_get_trading_dates(start_date, end_date) -> List[pd.Timestamp]:
    """取得市場實際有成交的日期。

    不直接採用 TaiwanStockTradingDate，因為該表可能先列入原訂開市日，
    但臨時颱風休市後未即時移除。改用 0050（可由環境變數調整）的
    TaiwanStockPrice 實際成交日期，避免把臨時休市日送進 storage_objects。
    """
    global _FINMIND_TRADING_DATE_CACHE

    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    reference_stock = _normalize_stock_name_code_key(FINMIND_TRADING_DATE_REFERENCE_STOCK)
    cache_key = (
        reference_stock,
        start_ts.strftime("%Y-%m-%d"),
        end_ts.strftime("%Y-%m-%d"),
    )

    with _FINMIND_DATA_CACHE_LOCK:
        cache_store = (
            _FINMIND_TRADING_DATE_CACHE
            if isinstance(_FINMIND_TRADING_DATE_CACHE, dict)
            else {}
        )
        cached = cache_store.get(cache_key)
        if cached is not None:
            return [pd.Timestamp(x).normalize() for x in cached]

    raw = _finmind_get_data(
        "TaiwanStockPrice",
        data_id=reference_stock,
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        allow_empty=False,
    )
    if "date" not in raw.columns:
        raise RuntimeError(
            "FinMind TaiwanStockPrice 缺少 date 欄位，"
            f"無法建立實際交易日：{raw.columns.tolist()}"
        )

    actual_dates = (
        pd.to_datetime(raw["date"], errors="coerce")
        .dropna()
        .dt.normalize()
        .drop_duplicates()
        .sort_values()
    )
    actual_dates = actual_dates[
        (actual_dates >= start_ts) & (actual_dates <= end_ts)
    ]
    dates = [pd.Timestamp(x).normalize() for x in actual_dates.tolist()]
    if not dates:
        raise RuntimeError(
            f"FinMind TaiwanStockPrice 找不到 {reference_stock} 在 "
            f"{start_ts.date()} ~ {end_ts.date()} 的實際成交日"
        )

    with _FINMIND_DATA_CACHE_LOCK:
        if not isinstance(_FINMIND_TRADING_DATE_CACHE, dict):
            _FINMIND_TRADING_DATE_CACHE = {}
        _FINMIND_TRADING_DATE_CACHE[cache_key] = list(dates)

    print(
        f"📅 FinMind 實際交易日：以 {reference_stock} TaiwanStockPrice 為準｜"
        f"{len(dates):,} 日｜{dates[0].date()} ~ {dates[-1].date()}"
    )
    return dates


def _finmind_get_warrant_summary(stock_code: str, start_date, end_date) -> pd.DataFrame:
    code = _normalize_stock_name_code_key(stock_code)
    cache_key = code
    with _FINMIND_DATA_CACHE_LOCK:
        cached = _FINMIND_WARRANT_SUMMARY_CACHE.get(cache_key)
        if cached is not None:
            raw = cached.copy()
        else:
            raw = None
    if raw is None:
        raw = _finmind_get_data(
            "TaiwanStockInfoWithWarrantSummary",
            data_id=code,
            allow_empty=True,
        ).fillna("")
        with _FINMIND_DATA_CACHE_LOCK:
            _FINMIND_WARRANT_SUMMARY_CACHE[cache_key] = raw.copy()

    if raw.empty:
        print(f"ℹ️ FinMind 找不到 {code} 的權證標的對照")
        return pd.DataFrame()
    required = {"stock_id", "target_stock_id", "type", "date", "end_date"}
    missing = required - set(raw.columns)
    if missing:
        _finmind_debug_print_df("TaiwanStockInfoWithWarrantSummary 欄位不足", raw)
        raise RuntimeError(
            f"FinMind TaiwanStockInfoWithWarrantSummary 欄位不足：{sorted(missing)}｜"
            f"實際欄位={raw.columns.tolist()}"
        )

    df = raw.copy()
    df["stock_id"] = df["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    df["target_stock_id"] = df["target_stock_id"].astype(str).map(_normalize_stock_name_code_key)
    df["listing_date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["last_trade_date"] = pd.to_datetime(df["end_date"], errors="coerce").dt.normalize()
    df["type"] = df["type"].astype(str).str.strip()
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    df = df[
        (df["target_stock_id"] == code)
        & df["type"].str.contains("認購", na=False)
        & df["listing_date"].notna()
        & df["last_trade_date"].notna()
        & (df["listing_date"] <= end_ts)
        & (df["last_trade_date"] >= start_ts)
    ].copy()
    df = df.drop_duplicates(
        subset=["stock_id", "target_stock_id", "listing_date", "last_trade_date"],
        keep="last",
    )
    print(
        f"✅ FinMind 認購權證區間對照：{code}｜{len(df):,} 段｜"
        f"查詢 {start_ts.date()} ~ {end_ts.date()}"
    )
    return df


def _finmind_warrant_name_map() -> Dict[str, str]:
    try:
        df = _finmind_load_stock_info_with_warrant()
    except Exception as exc:
        print(f"⚠️ FinMind 權證名稱總覽讀取失敗，名稱暫以代號顯示：{exc}")
        return {}
    if df.empty:
        return {}
    work = df[["stock_id", "stock_name"]].copy()
    if "date" in df.columns:
        work["date"] = pd.to_datetime(df["date"], errors="coerce")
        work = work.sort_values("date")
    work = work.drop_duplicates(subset=["stock_id"], keep="last")
    return dict(zip(work["stock_id"].astype(str), work["stock_name"].astype(str)))


def _finmind_target_warrant_name_map(summary_df: pd.DataFrame) -> Dict[str, str]:
    """只建立本次目標股票的權證名稱表；官方資料完整時不載入 19 萬筆全市場名稱。"""
    if summary_df is None or summary_df.empty or "stock_id" not in summary_df.columns:
        return {}

    codes = set(summary_df["stock_id"].astype(str).map(normalize_openapi_warrant_code))
    codes.discard("")
    result = {}

    # FinMind summary 若未來直接提供名稱，優先就地使用。
    for name_col in ["stock_name", "warrant_name", "name", "證券名稱", "權證名稱"]:
        if name_col not in summary_df.columns:
            continue
        for code, name in zip(summary_df["stock_id"], summary_df[name_col]):
            normalized_code = normalize_openapi_warrant_code(code)
            clean_name = str(name or "").strip()
            if normalized_code and clean_name:
                result[normalized_code] = clean_name
        if result:
            break

    # 官方 TWSE／TPEx 權證資料通常已涵蓋目前有效權證，直接補名稱與發行商。
    official_map = _finmind_official_warrant_issuer_map()
    for code in codes:
        if code in result:
            continue
        rec = official_map.get(code, {})
        official_name = str(rec.get("official_warrant_name", "") or "").strip()
        if official_name:
            result[code] = official_name

    missing = codes - set(result)
    if not missing:
        print(f"⚡ 目標權證名稱表：官方／Summary 已涵蓋 {len(result):,} 支，略過全市場 19 萬筆名稱總覽")
        return result

    # 官方資料暫時缺漏時才保留原本全市場 FinMind 名稱備援，避免圖卡顯示權證代號。
    print(f"ℹ️ 目標權證名稱尚缺 {len(missing):,} 支，啟用 FinMind 全市場名稱備援")
    full_map = _finmind_warrant_name_map()
    for code in missing:
        name = str(full_map.get(code, "") or "").strip()
        if name:
            result[code] = name
    return result


def _finmind_active_warrant_codes(summary_df: pd.DataFrame, trade_date) -> set:
    if summary_df is None or summary_df.empty:
        return set()
    day = pd.Timestamp(trade_date).normalize()
    hit = summary_df[
        (summary_df["listing_date"] <= day)
        & (summary_df["last_trade_date"] >= day)
    ]
    return set(hit["stock_id"].astype(str))



def _finmind_process_warrant_day(
    trade_date,
    active_codes: set,
    warrant_name_map: Dict[str, str],
    stock_code: str,
    stock_name: str,
    trader_name_map: Dict[str, str] | None = None,
) -> pd.DataFrame:
    """處理單日歷史事件；缺少共用精簡檔時，本次直接建立並立即重用。"""
    if not active_codes:
        return pd.DataFrame()
    trader_name_map = trader_name_map or {}

    if FINMIND_MARKET_COMPACT_CACHE_ENABLE:
        compact_result = _finmind_build_market_compact_day(
            trade_date,
            trader_name_map,
            force_refresh=False,
            allow_not_ready=False,
            return_frame=True,
        )
        compact_path, compact = compact_result if isinstance(compact_result, tuple) else (compact_result, pd.DataFrame())
        if compact is None or compact.empty:
            return pd.DataFrame()
        work = compact.copy()
        work["warrant_code"] = work["warrant_code"].astype(str).map(normalize_openapi_warrant_code)
        work = work[work["warrant_code"].isin(set(active_codes))].copy()
        if work.empty:
            return pd.DataFrame()
        work["warrant_name"] = work["warrant_code"].map(warrant_name_map).fillna(work["warrant_code"])
        work["underlying_code"] = _normalize_stock_name_code_key(stock_code)
        work["underlying_name"] = str(stock_name or "")
        return work[[
            "Date", "branch", "broker_code", "warrant_code", "warrant_name",
            "underlying_code", "underlying_name", "buy_amount", "sell_amount",
            "net_amount", "buy_shares", "sell_shares", "side",
        ]].reset_index(drop=True)

    # 關閉共用精簡快取時保留原本的目標權證快速篩選路徑。
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    path = _finmind_download_warrant_day(trade_date, allow_not_ready=False)
    columns = [
        "securities_trader", "price", "buy", "sell",
        "securities_trader_id", "stock_id", "date",
    ]
    codes = sorted(active_codes)
    try:
        raw = pd.read_parquet(path, columns=columns, filters=[("stock_id", "in", codes)])
    except Exception:
        raw = pd.read_parquet(path, columns=columns)
        raw["stock_id"] = raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
        raw = raw[raw["stock_id"].isin(active_codes)].copy()
    if raw.empty:
        return pd.DataFrame()

    raw = raw.copy()
    raw["stock_id"] = raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    raw = raw[raw["stock_id"].isin(active_codes)].copy()
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce").fillna(0.0)
    raw["buy"] = pd.to_numeric(raw["buy"], errors="coerce").fillna(0.0)
    raw["sell"] = pd.to_numeric(raw["sell"], errors="coerce").fillna(0.0)
    raw["buy_amount_row"] = raw["price"] * raw["buy"]
    raw["sell_amount_row"] = raw["price"] * raw["sell"]
    raw["broker_code"] = raw["securities_trader_id"].astype(str).str.strip()
    raw_branch = raw["securities_trader"].astype(str).str.strip()
    mapped_branch = raw["broker_code"].map(trader_name_map).fillna("").astype(str)
    raw["branch"] = np.where(mapped_branch.str.strip() != "", mapped_branch, raw_branch)
    raw["branch"] = pd.Series(raw["branch"], index=raw.index).map(normalize_branch_name)
    raw = raw[(raw["branch"] != "") & (raw["broker_code"] != "")]

    grouped = raw.groupby(["broker_code", "branch", "stock_id"], as_index=False, dropna=False).agg(
        buy_shares_raw=("buy", "sum"),
        sell_shares_raw=("sell", "sum"),
        buy_amount=("buy_amount_row", "sum"),
        sell_amount=("sell_amount_row", "sum"),
    ).rename(columns={"stock_id": "warrant_code"})
    grouped["Date"] = pd.Timestamp(date_s)
    grouped["warrant_name"] = grouped["warrant_code"].map(warrant_name_map).fillna(grouped["warrant_code"])
    grouped["underlying_code"] = _normalize_stock_name_code_key(stock_code)
    grouped["underlying_name"] = str(stock_name or "")
    grouped["buy_shares"] = grouped["buy_shares_raw"] / 1000.0
    grouped["sell_shares"] = grouped["sell_shares_raw"] / 1000.0
    grouped["net_amount"] = grouped["buy_amount"] - grouped["sell_amount"]
    grouped = grouped[(grouped["buy_amount"] != 0) | (grouped["sell_amount"] != 0)].copy()
    grouped["side"] = np.where(grouped["net_amount"] >= 0, "買超", "賣超")
    return grouped[[
        "Date", "branch", "broker_code", "warrant_code", "warrant_name",
        "underlying_code", "underlying_name", "buy_amount", "sell_amount",
        "net_amount", "buy_shares", "sell_shares", "side",
    ]]



def _finmind_process_warrant_day_union(
    trade_date,
    active_codes_by_stock: Dict[str, set],
    warrant_name_maps: Dict[str, Dict[str, str]],
    stock_names: Dict[str, str],
    trader_name_map: Dict[str, str],
) -> Dict[str, pd.DataFrame]:
    """多股票共用：單日全市場 Parquet 只讀一次，再依有效權證代號拆回各標的股。"""
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    union_codes = set()
    for codes in active_codes_by_stock.values():
        union_codes.update(set(codes or set()))
    if not union_codes:
        return {}

    path = _finmind_download_warrant_day(trade_date, allow_not_ready=False)
    columns = [
        "securities_trader", "price", "buy", "sell",
        "securities_trader_id", "stock_id", "date",
    ]
    sorted_codes = sorted(union_codes)
    try:
        raw = pd.read_parquet(path, columns=columns, filters=[("stock_id", "in", sorted_codes)])
    except Exception:
        raw = pd.read_parquet(path, columns=columns)
        raw["stock_id"] = raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
        raw = raw[raw["stock_id"].isin(union_codes)].copy()
    if raw.empty:
        return {code: pd.DataFrame() for code in active_codes_by_stock}

    raw = raw.copy()
    _finmind_debug_print_df(
        f"多股票聯集權證分點 Parquet 實際格式｜{date_s}",
        raw,
        once_key="schema:TaiwanStockWarrantTradingDailyReport:parquet",
    )
    raw["stock_id"] = raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    raw = raw[raw["stock_id"].isin(union_codes)].copy()
    if raw.empty:
        return {code: pd.DataFrame() for code in active_codes_by_stock}

    raw["price"] = pd.to_numeric(raw["price"], errors="coerce").fillna(0.0)
    raw["buy"] = pd.to_numeric(raw["buy"], errors="coerce").fillna(0.0)
    raw["sell"] = pd.to_numeric(raw["sell"], errors="coerce").fillna(0.0)
    raw["buy_amount_row"] = raw["price"] * raw["buy"]
    raw["sell_amount_row"] = raw["price"] * raw["sell"]
    raw["raw_securities_trader"] = raw["securities_trader"].astype(str).str.strip()
    raw["broker_code"] = raw["securities_trader_id"].astype(str).str.strip()
    raw["mapped_branch"] = raw["broker_code"].map(trader_name_map).fillna("").astype(str)
    raw["branch"] = np.where(
        raw["mapped_branch"].astype(str).str.strip() != "",
        raw["mapped_branch"],
        raw["raw_securities_trader"],
    )
    raw["branch"] = pd.Series(raw["branch"], index=raw.index).map(normalize_branch_name)
    raw = raw[(raw["branch"] != "") & (raw["broker_code"] != "")]

    grouped = raw.groupby(
        ["broker_code", "branch", "stock_id"],
        as_index=False,
        dropna=False,
    ).agg(
        buy_shares_raw=("buy", "sum"),
        sell_shares_raw=("sell", "sum"),
        buy_amount=("buy_amount_row", "sum"),
        sell_amount=("sell_amount_row", "sum"),
    )

    output = {}
    for stock_code, active_codes in active_codes_by_stock.items():
        code = _normalize_stock_name_code_key(stock_code)
        part = grouped[grouped["stock_id"].isin(set(active_codes or set()))].copy()
        if part.empty:
            output[code] = pd.DataFrame()
            continue
        part = part.rename(columns={"stock_id": "warrant_code"})
        part["Date"] = pd.Timestamp(date_s)
        name_map = warrant_name_maps.get(code, {}) or {}
        part["warrant_name"] = part["warrant_code"].map(name_map).fillna(part["warrant_code"])
        part["underlying_code"] = code
        part["underlying_name"] = str(stock_names.get(code, code) or code)
        part["buy_shares"] = part["buy_shares_raw"] / 1000.0
        part["sell_shares"] = part["sell_shares_raw"] / 1000.0
        part["net_amount"] = part["buy_amount"] - part["sell_amount"]
        part = part[
            (part["buy_amount"] != 0)
            | (part["sell_amount"] != 0)
            | (part["net_amount"] != 0)
        ].copy()
        part["side"] = np.where(part["net_amount"] >= 0, "買超", "賣超")
        output[code] = part[[
            "Date", "branch", "broker_code", "warrant_code", "warrant_name",
            "underlying_code", "underlying_name", "buy_amount", "sell_amount",
            "net_amount", "buy_shares", "sell_shares", "side",
        ]]
    return output


def _finmind_prepare_multi_stock_warrant_events(stock_codes: List[str]):
    """多股票預處理：每個交易日只解析一次全市場 Parquet，結果按股票保存在記憶體。"""
    global _FINMIND_MULTI_STOCK_PREFETCH_READY
    global _FINMIND_MULTI_STOCK_PREFETCH_RANGE
    global _FINMIND_MULTI_STOCK_EVENT_CACHE
    global _FINMIND_MULTI_STOCK_SUMMARY_CACHE
    global _FINMIND_MULTI_STOCK_NAME_MAP_CACHE

    codes = []
    for raw_code in stock_codes or []:
        code = _normalize_stock_name_code_key(raw_code)
        if code and code not in codes:
            codes.append(code)
    if (
        not FINMIND_MULTI_STOCK_DAILY_READ_ONCE_ENABLE
        or len(codes) < FINMIND_MULTI_STOCK_PREFETCH_MIN_STOCKS
    ):
        return False

    with _FINMIND_MULTI_STOCK_PREFETCH_LOCK:
        if _FINMIND_MULTI_STOCK_PREFETCH_READY and set(codes).issubset(_FINMIND_MULTI_STOCK_EVENT_CACHE):
            return True

        end_ts = get_taipei_today_ts()
        start_ts = end_ts - pd.Timedelta(days=FINMIND_MULTI_STOCK_PREFETCH_CALENDAR_DAYS - 1)
        print(
            f"🚀 多股票權證聯集預處理：{len(codes)} 檔｜"
            f"{start_ts.date()} ~ {end_ts.date()}｜每日 Parquet 只讀一次"
        )

        # 多檔摘要並行前先啟動單一官方發行商 Future，避免每個 metadata worker 各自重抓官方來源。
        _finmind_start_official_warrant_issuer_prefetch()

        stock_names = {}
        summaries = {}
        name_maps = {}
        metadata_failures = []

        def prepare_stock_metadata(code: str):
            stock_name = get_tw_stock_name(code)
            summary = _finmind_get_warrant_summary(code, start_ts, end_ts)
            name_map = _finmind_target_warrant_name_map(summary) if summary is not None and not summary.empty else {}
            return code, stock_name, summary, name_map

        with ThreadPoolExecutor(max_workers=min(8, len(codes))) as metadata_executor:
            metadata_future_map = {
                metadata_executor.submit(prepare_stock_metadata, code): code
                for code in codes
            }
            for future in as_completed(metadata_future_map):
                code = metadata_future_map[future]
                try:
                    result_code, stock_name, summary, name_map = future.result()
                    stock_names[result_code] = stock_name
                    summaries[result_code] = summary
                    name_maps[result_code] = name_map
                except Exception as exc:
                    metadata_failures.append((code, str(exc)))
                    print(f"❌ 多股票標的／權證摘要預載失敗：{code}｜{exc}")

        if metadata_failures:
            sample = "；".join(f"{code}:{error[:120]}" for code, error in metadata_failures[:5])
            message = f"多股票標的／權證摘要預載不完整：{len(metadata_failures)}/{len(codes)} 檔｜{sample}"
            if FINMIND_STRICT_WARRANT_COMPLETENESS:
                raise RuntimeError(message)
            print(f"⚠️ {message}｜退回逐股票流程")
            return False

        trading_dates = _finmind_get_trading_dates(start_ts, end_ts)
        if not trading_dates:
            return False

        latest_available = None
        for day in reversed(trading_dates[-FINMIND_WARRANT_LATEST_PROBE_DAYS:]):
            probe_path = _finmind_download_warrant_day(day, allow_not_ready=True)
            if probe_path:
                latest_available = pd.Timestamp(day).normalize()
                break
        if latest_available is None:
            raise RuntimeError("多股票預處理找不到最近可用的 FinMind 權證分點日檔")

        missing_price_based_dates = [
            pd.Timestamp(day).normalize()
            for day in trading_dates
            if pd.Timestamp(day).normalize() > latest_available
        ]
        # 最新日 API 只能補最末一個交易日；若 Parquet 落後兩個以上交易日，
        # 中間日期會永久缺漏。API 關閉時，即使只落後一日也沒有任何補齊來源。
        parquet_gap_unrecoverable = bool(
            len(missing_price_based_dates) > 1
            or (missing_price_based_dates and not FINMIND_WARRANT_LATEST_DAY_API_ENABLE)
        )
        if parquet_gap_unrecoverable:
            missing_text = ",".join(day.strftime("%Y-%m-%d") for day in missing_price_based_dates)
            recovery_reason = (
                "最新日 API 無法補齊中間缺日"
                if len(missing_price_based_dates) > 1
                else "最新日 API 已關閉，無法補齊缺日"
            )
            message = (
                f"多股票預處理偵測到 FinMind Parquet 落後 {len(missing_price_based_dates)} 個交易日："
                f"latest_available={latest_available.date()}｜price_based_missing={missing_text}｜"
                f"{recovery_reason}"
            )
            if FINMIND_STRICT_WARRANT_COMPLETENESS:
                raise RuntimeError(message)
            print(f"⚠️ {message}｜退回逐股票流程")
            return False
        if missing_price_based_dates:
            print(
                f"ℹ️ 多股票預處理 Parquet 僅落後最新交易日：{missing_price_based_dates[-1].date()}｜"
                "後續由最新日權證 API 補齊"
            )
        trading_dates = [d for d in trading_dates if d <= latest_available]

        trader_name_map, _ = _finmind_securities_trader_maps()
        jobs = []
        active_date_count = {code: 0 for code in codes}
        for day in trading_dates:
            active_by_stock = {}
            for code in codes:
                active_codes = _finmind_active_warrant_codes(summaries.get(code), day)
                if active_codes:
                    active_by_stock[code] = active_codes
                    active_date_count[code] += 1
            if active_by_stock:
                jobs.append((day, active_by_stock))

        frames_by_stock = {code: [] for code in codes}
        failures = []
        failed_dates_by_stock = {code: set() for code in codes}
        completed = 0
        with ThreadPoolExecutor(max_workers=FINMIND_WARRANT_DOWNLOAD_WORKERS) as executor:
            future_map = {
                executor.submit(
                    _finmind_process_warrant_day_union,
                    day,
                    active_by_stock,
                    name_maps,
                    stock_names,
                    trader_name_map,
                ): (day, tuple(active_by_stock.keys()))
                for day, active_by_stock in jobs
            }
            for future in as_completed(future_map):
                day, active_stock_codes = future_map[future]
                completed += 1
                try:
                    day_map = future.result()
                    for code, day_df in (day_map or {}).items():
                        if day_df is not None and not day_df.empty:
                            frames_by_stock.setdefault(code, []).append(day_df)
                except Exception as exc:
                    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
                    failures.append((day_s, str(exc)))
                    for failed_code in active_stock_codes:
                        failed_dates_by_stock.setdefault(failed_code, set()).add(day_s)
                    print(f"❌ 多股票聯集 Parquet 日期失敗：{pd.Timestamp(day).date()}｜{exc}")
                if completed == 1 or completed % 5 == 0 or completed == len(jobs):
                    print(
                        f"📊 多股票聯集進度：{completed}/{len(jobs)}｜"
                        f"失敗={len(failures)}"
                    )

        if failures and FINMIND_STRICT_WARRANT_COMPLETENESS:
            samples = "；".join(f"{d}:{err[:120]}" for d, err in failures[:5])
            raise RuntimeError(
                f"多股票 FinMind 權證分點不完整：失敗 {len(failures)}/{len(jobs)} 日｜{samples}"
            )

        event_cache = {}
        for code in codes:
            frames = frames_by_stock.get(code, [])
            if frames:
                events = _concat_warrant_event_frames(frames)
                events["Date"] = pd.to_datetime(events["Date"], errors="coerce").dt.normalize()
                events = events.dropna(subset=["Date"])
                events = events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
                events.attrs["_warrant_events_normalized"] = True
            else:
                events = pd.DataFrame()
            event_cache[code] = events
            total_active_dates = int(active_date_count.get(code, 0))
            failed_date_list = sorted(failed_dates_by_stock.get(code, set()))
            failed_date_count = len(failed_date_list)
            successful_date_count = max(0, total_active_dates - failed_date_count)
            event_date_count = int(events["Date"].nunique()) if not events.empty else 0
            _FINMIND_WARRANT_RUN_STATS[code] = {
                "total_dates": total_active_dates,
                "success_dates": successful_date_count,
                "empty_dates": max(0, successful_date_count - event_date_count),
                "failed_dates": failed_date_count,
                "failed_date_list": failed_date_list,
                "latest_available_date": latest_available.strftime("%Y-%m-%d"),
            }
            print(f"✅ 多股票聯集拆分：{code} {stock_names.get(code, code)}｜{len(events):,} 筆")

        _FINMIND_MULTI_STOCK_EVENT_CACHE = event_cache
        _FINMIND_MULTI_STOCK_SUMMARY_CACHE = summaries
        _FINMIND_MULTI_STOCK_NAME_MAP_CACHE = name_maps
        _FINMIND_MULTI_STOCK_PREFETCH_RANGE = (start_ts, end_ts)
        _FINMIND_MULTI_STOCK_PREFETCH_READY = True
        print(
            f"✅ 多股票權證聯集預處理完成：{len(codes)} 檔｜"
            f"交易日 {len(jobs)} 日｜失敗 {len(failures)} 日"
        )
        return True


def _finmind_get_multi_stock_prefetched_events(stock_code: str, start_date, end_date):
    code = _normalize_stock_name_code_key(stock_code)
    with _FINMIND_MULTI_STOCK_PREFETCH_LOCK:
        if not _FINMIND_MULTI_STOCK_PREFETCH_READY or code not in _FINMIND_MULTI_STOCK_EVENT_CACHE:
            return None
        range_start, range_end = _FINMIND_MULTI_STOCK_PREFETCH_RANGE
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        if pd.isna(range_start) or pd.isna(range_end) or start_ts < range_start or end_ts > range_end:
            return None
        events = _FINMIND_MULTI_STOCK_EVENT_CACHE.get(code)
        if events is None:
            return pd.DataFrame()
        out = events.copy()
        if not out.empty:
            out = out[(out["Date"] >= start_ts) & (out["Date"] <= end_ts)].copy()
        result = out.reset_index(drop=True)
        if not result.empty:
            result.attrs["_warrant_events_normalized"] = True
        return result

def _events_to_gsheet_history_df(events_df: pd.DataFrame, stock_code: str, stock_name: str, start_date=None, end_date=None) -> pd.DataFrame:
    if events_df is None or events_df.empty:
        return pd.DataFrame(columns=GSHEET_WARRANT_HISTORY_HEADERS)
    e = events_df.copy().fillna("")
    e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["Date"])
    updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    out = pd.DataFrame()
    out["日期"] = e["Date"].dt.strftime("%Y/%m/%d")
    out["權證代號"] = e["warrant_code"].map(normalize_openapi_warrant_code)
    out["權證名稱"] = e["warrant_name"].astype(str).str.strip()
    out["標的股"] = e["underlying_code"].astype(str).str.strip().replace("", str(stock_code))
    out["標的名稱"] = e["underlying_name"].astype(str).str.strip().replace("", str(stock_name))
    out["分點"] = e["branch"].astype(str).map(normalize_branch_name)
    out["分點名稱"] = out["分點"]
    out["券商代號"] = e["broker_code"].astype(str).str.strip()
    out["買進金額"] = pd.to_numeric(e["buy_amount"], errors="coerce").fillna(0.0)
    out["賣出金額"] = pd.to_numeric(e["sell_amount"], errors="coerce").fillna(0.0)
    out["買超金額"] = pd.to_numeric(e["net_amount"], errors="coerce").fillna(0.0)
    out["買進張數"] = pd.to_numeric(e.get("buy_shares", 0), errors="coerce").fillna(0.0)
    out["賣出張數"] = pd.to_numeric(e.get("sell_shares", 0), errors="coerce").fillna(0.0)
    out["資料來源"] = FINMIND_WARRANT_SOURCE_LABEL
    out["快取起日"] = _gsheet_cache_date_str(start_date)
    out["快取迄日"] = _gsheet_cache_date_str(end_date)
    out["更新時間"] = updated_at
    return out[GSHEET_WARRANT_HISTORY_HEADERS]


def load_gsheet_warrant_events_snapshot(stock_code: str, start_date=None, end_date=None) -> pd.DataFrame:
    """只接受新版 FinMind 快照；舊 MoneyDJ 快照不再讀取。"""
    if not GSHEET_WARRANT_CACHE_ENABLE or WARRANT_CACHE_FORCE_REFRESH:
        return pd.DataFrame()
    key = _gsheet_cache_key(stock_code, start_date=start_date, end_date=end_date)
    status_df = _read_gsheet_warrant_status()
    if status_df is None or status_df.empty or "快取鍵" not in status_df.columns:
        return pd.DataFrame()
    matched = status_df[status_df["快取鍵"].astype(str) == key].copy()
    if matched.empty:
        return pd.DataFrame()
    row = matched.tail(1).iloc[0]
    if str(row.get("完整度狀態", "")).strip().lower() != "complete":
        return pd.DataFrame()
    if str(row.get("資料來源", "") or "").strip() != FINMIND_WARRANT_SOURCE_LABEL:
        print(f"⚠️ 拒絕舊資料源權證快照：{key}｜將重新抓 FinMind")
        return pd.DataFrame()
    failed_count = int(pd.to_numeric(row.get("FinMind失敗日期", 0), errors="coerce") or 0)
    expected_rows = int(pd.to_numeric(row.get("資料筆數", 0), errors="coerce") or 0)
    if failed_count != 0 or expected_rows <= 0:
        return pd.DataFrame()
    snapshot_sheet = str(row.get("快照工作表", "") or "").strip() or _warrant_snapshot_worksheet_title(stock_code)
    sh = _open_warrant_cache_gsheet(create_if_missing=False)
    raw_snapshot = _read_worksheet_from_spreadsheet(sh, snapshot_sheet)
    events = normalize_history_cache_df(raw_snapshot)
    if events.empty:
        return pd.DataFrame()
    code = _normalize_stock_name_code_key(stock_code)
    events = events[events["underlying_code"].astype(str) == code].copy()
    if start_date is not None:
        events = events[events["Date"] >= pd.Timestamp(start_date).normalize()]
    if end_date is not None:
        events = events[events["Date"] <= pd.Timestamp(end_date).normalize()]
    if len(events) != expected_rows:
        print(f"⚠️ FinMind 快照筆數不一致：{key}｜狀態={expected_rows:,}｜實際={len(events):,}")
        return pd.DataFrame()
    print(f"☁️ FinMind Google Sheet 快照命中：{key}｜{len(events):,} 筆")
    return events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)


def save_gsheet_warrant_events_snapshot(stock_code: str, stock_name: str, events_df: pd.DataFrame, start_date=None, end_date=None):
    if not GSHEET_WARRANT_CACHE_ENABLE or events_df is None or events_df.empty:
        return
    stats = dict(_FINMIND_WARRANT_RUN_STATS.get(_normalize_stock_name_code_key(stock_code), {}))
    failed_dates = int(stats.get("failed_dates", 0) or 0)
    if failed_dates > 0:
        print(f"⚠️ FinMind 權證日期仍有失敗，不寫入完整快照：failed_dates={failed_dates}")
        return
    sh = _open_warrant_cache_gsheet(create_if_missing=True)
    if sh is None:
        print("⚠️ 權證快取試算表無法開啟，略過 FinMind 權證快取寫回")
        return
    key = _gsheet_cache_key(stock_code, start_date=start_date, end_date=end_date)
    snapshot_sheet = _warrant_snapshot_worksheet_title(stock_code, start_date, end_date)
    updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    new_history = _events_to_gsheet_history_df(events_df, stock_code, stock_name, start_date, end_date)
    history_ws = _get_or_create_worksheet(
        sh,
        snapshot_sheet,
        rows=max(len(new_history) + 1, 1000),
        cols=len(GSHEET_WARRANT_HISTORY_HEADERS),
    )
    _update_worksheet_from_df(history_ws, new_history, GSHEET_WARRANT_HISTORY_HEADERS)

    status_ws = _get_or_create_worksheet(
        sh,
        GSHEET_WARRANT_STATUS_SHEET,
        rows=200,
        cols=len(GSHEET_WARRANT_STATUS_HEADERS),
    )
    old_status = _worksheet_to_df(status_ws)
    if old_status is not None and not old_status.empty and "快取鍵" in old_status.columns:
        # 只覆蓋完全相同的快取鍵；同一標的不同日期區間的快照狀態必須同時保留。
        old_status = old_status[old_status["快取鍵"].astype(str) != str(key)].copy()
    else:
        old_status = pd.DataFrame(columns=GSHEET_WARRANT_STATUS_HEADERS)

    new_status = pd.DataFrame([{
        "快取鍵": key,
        "標的股": _normalize_stock_name_code_key(stock_code),
        "標的名稱": str(stock_name or ""),
        "快取起日": _gsheet_cache_date_str(start_date),
        "快取迄日": _gsheet_cache_date_str(end_date),
        "完整度狀態": "complete",
        "資料來源": FINMIND_WARRANT_SOURCE_LABEL,
        "FinMind交易日總數": int(stats.get("total_dates", 0) or 0),
        "FinMind成功日期": int(stats.get("success_dates", 0) or 0),
        "FinMind空資料日期": int(stats.get("empty_dates", 0) or 0),
        "FinMind失敗日期": failed_dates,
        "資料筆數": int(len(new_history)),
        "快照工作表": snapshot_sheet,
        "更新時間": updated_at,
    }])
    all_status = pd.concat([old_status, new_status], ignore_index=True, sort=False).fillna("")
    _update_worksheet_from_df(status_ws, all_status, GSHEET_WARRANT_STATUS_HEADERS)
    print(
        f"✅ FinMind 權證快照已寫入 Google Sheet：{key}｜"
        f"工作表={snapshot_sheet}｜{len(new_history):,} 筆"
    )


def _finmind_recent_history_warrant_codes(
    historical_events: pd.DataFrame | None,
    latest_day,
    lookback_trading_days: int,
) -> set:
    """取得最新日前最近 N 個實際有事件交易日出現過的權證代號。"""
    if historical_events is None or historical_events.empty:
        return set()
    if "Date" not in historical_events.columns or "warrant_code" not in historical_events.columns:
        return set()

    day = pd.Timestamp(latest_day).normalize()
    work = historical_events[["Date", "warrant_code"]].copy()
    work["Date"] = pd.to_datetime(work["Date"], errors="coerce").dt.normalize()
    work["warrant_code"] = work["warrant_code"].map(normalize_openapi_warrant_code)
    work = work.dropna(subset=["Date"])
    work = work[(work["Date"] < day) & (work["warrant_code"] != "")]
    if work.empty:
        return set()

    recent_dates = sorted(work["Date"].drop_duplicates().tolist())[-max(1, int(lookback_trading_days)):]
    return set(work.loc[work["Date"].isin(recent_dates), "warrant_code"].astype(str))


def _finmind_fetch_selected_branch_day_raw(
    securities_trader_id: str,
    trade_date,
) -> tuple[pd.DataFrame, dict]:
    """以分點 ID 查詢單日全部權證，用於最新日權證母體補漏與精選分點核對。"""
    broker_id = str(securities_trader_id or "").strip()
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    diagnostic = {
        "securities_trader_id": broker_id,
        "date": date_s,
        "http_status": 0,
        "payload_keys": [],
        "message": "",
        "rows": 0,
    }
    if not broker_id:
        diagnostic["message"] = "empty securities_trader_id"
        return pd.DataFrame(), diagnostic

    last_error = None
    normal_attempt = 0
    quota_attempt = 0
    while normal_attempt < FINMIND_REQUEST_RETRIES:
        try:
            _finmind_wait_for_rate_limit_gate()
            resp = get_thread_session().get(
                FINMIND_WARRANT_BRANCH_URL,
                headers=_finmind_headers(),
                params={"securities_trader_id": broker_id, "date": date_s},
                timeout=(FINMIND_CONNECT_TIMEOUT, FINMIND_READ_TIMEOUT),
            )
            diagnostic["http_status"] = int(resp.status_code)
            if resp.status_code in (401, 402, 403, 429):
                _, message = _finmind_error_payload_from_response(resp)
                diagnostic["message"] = message
                if _finmind_is_rate_limit(resp.status_code, message):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"最新日精選分點 API｜分點={broker_id}｜date={date_s}",
                        quota_attempt,
                        message,
                        resp=resp,
                    )
                    continue
                raise FinMindAuthorizationError(
                    f"FinMind 最新日精選分點 API 授權失敗：HTTP {resp.status_code}｜{message}"
                )
            if resp.status_code == 404:
                diagnostic["message"] = resp.text[:500]
                return pd.DataFrame(), diagnostic
            resp.raise_for_status()
            payload = resp.json()
            diagnostic["payload_keys"] = list(payload.keys()) if isinstance(payload, dict) else []
            diagnostic["message"] = _finmind_error_message(payload)
            payload_status = payload.get("status", 200) if isinstance(payload, dict) else 200
            if str(payload_status) not in ("200", "success", "True", "true"):
                if _finmind_is_rate_limit(payload_status, diagnostic["message"]):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"最新日精選分點 API｜分點={broker_id}｜date={date_s}",
                        quota_attempt,
                        diagnostic["message"],
                    )
                    continue
                raise RuntimeError(
                    f"FinMind 最新日精選分點 API 回傳失敗：status={payload_status}｜"
                    f"{diagnostic['message']}"
                )
            data = payload.get("data", []) if isinstance(payload, dict) else []
            df = pd.DataFrame(data)
            if not df.empty:
                if "securities_trader_id" not in df.columns:
                    df["securities_trader_id"] = broker_id
                if "date" in df.columns:
                    df["date"] = df["date"].astype(str)
                    df = df[df["date"] == date_s].copy()
            diagnostic["rows"] = int(len(df))
            _finmind_debug_print_df(
                f"最新日精選分點 API 實際格式｜分點={broker_id}｜{date_s}",
                df,
                once_key="schema:latest-selected-branch-api",
            )
            return df, diagnostic
        except (FinMindAuthorizationError, FinMindRateLimitError):
            raise
        except Exception as exc:
            last_error = exc
            normal_attempt += 1
            if normal_attempt >= FINMIND_REQUEST_RETRIES:
                break
            wait_sec = FINMIND_RETRY_BASE_WAIT * normal_attempt
            print(
                f"⚠️ FinMind 最新日精選分點 API 重試 "
                f"{normal_attempt}/{FINMIND_REQUEST_RETRIES - 1}｜"
                f"分點={broker_id}｜date={date_s}｜{exc}｜等待 {wait_sec:.1f} 秒"
            )
            time.sleep(wait_sec)

    diagnostic["message"] = str(last_error or "unknown error")
    raise RuntimeError(
        f"FinMind 最新日精選分點 API 最終失敗："
        f"分點={broker_id}｜date={date_s}｜{last_error}"
    )


def _finmind_discover_selected_branch_latest_day_rows(
    selected_branch_id_map: Dict[str, set] | None,
    trade_date,
    target_warrant_codes: set,
) -> tuple[pd.DataFrame, dict]:
    """直接查本次精選分點，找出最新日漏網權證並保留核對用原始列。"""
    branch_ids = sorted({
        str(branch_id or "").strip()
        for ids in (selected_branch_id_map or {}).values()
        for branch_id in (ids or set())
        if str(branch_id or "").strip()
    })
    stats = {
        "branch_ids": len(branch_ids),
        "success_branch_ids": 0,
        "failed_branch_ids": 0,
        "raw_rows": 0,
        "target_rows": 0,
        "target_codes": 0,
    }
    if (
        not FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_DISCOVERY_ENABLE
        or not branch_ids
        or not target_warrant_codes
    ):
        return pd.DataFrame(), stats

    frames = []
    failures = []
    workers = min(max(1, FINMIND_WARRANT_LATEST_DAY_API_WORKERS), len(branch_ids))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_finmind_fetch_selected_branch_day_raw, branch_id, trade_date): branch_id
            for branch_id in branch_ids
        }
        for future in as_completed(future_map):
            branch_id = future_map[future]
            try:
                raw, _ = future.result()
                stats["success_branch_ids"] += 1
                stats["raw_rows"] += int(len(raw))
                if raw is None or raw.empty or "stock_id" not in raw.columns:
                    continue
                work = raw.copy().fillna("")
                work["stock_id"] = work["stock_id"].astype(str).map(normalize_openapi_warrant_code)
                work = work[work["stock_id"].isin(target_warrant_codes)].copy()
                if not work.empty:
                    frames.append(work)
            except (FinMindAuthorizationError, FinMindRateLimitError):
                for pending in future_map:
                    pending.cancel()
                raise
            except Exception as exc:
                stats["failed_branch_ids"] += 1
                failures.append((branch_id, str(exc)))

    if failures:
        preview = "；".join(f"{branch_id}:{error[:140]}" for branch_id, error in failures[:8])
        message = (
            f"最新日精選分點補漏查詢不完整：失敗 {len(failures)}/{len(branch_ids)} 個分點｜{preview}"
        )
        if FINMIND_WARRANT_LATEST_DAY_SELECTED_BRANCH_STRICT:
            raise RuntimeError(message)
        print(f"⚠️ {message}")

    if not frames:
        print(
            f"🔎 最新日精選分點直接核對：日期={pd.Timestamp(trade_date).date()}｜"
            f"分點={len(branch_ids)}個｜原始列={stats['raw_rows']:,}｜"
            "目標標的列=0"
        )
        return pd.DataFrame(), stats

    rows = pd.concat(frames, ignore_index=True, sort=False).fillna("")
    rows["stock_id"] = rows["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    rows = rows[rows["stock_id"].isin(target_warrant_codes)].copy()
    stats["target_rows"] = int(len(rows))
    stats["target_codes"] = int(rows["stock_id"].nunique())
    print(
        f"🔎 最新日精選分點直接核對：日期={pd.Timestamp(trade_date).date()}｜"
        f"分點={len(branch_ids)}個｜原始列={stats['raw_rows']:,}｜"
        f"目標標的列={len(rows):,}｜權證={stats['target_codes']:,}支"
    )
    return rows.reset_index(drop=True), stats


def _finmind_fetch_warrant_code_day_raw(warrant_code: str, trade_date, cancel_event: threading.Event | None = None) -> tuple[pd.DataFrame, dict]:
    """以權證代號查詢單日所有分點明細，回傳 DataFrame 與診斷資訊。"""
    code = normalize_openapi_warrant_code(warrant_code)
    date_s = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    diagnostic = {
        "warrant_code": code,
        "date": date_s,
        "http_status": 0,
        "payload_keys": [],
        "message": "",
        "rows": 0,
    }
    if not code:
        diagnostic["message"] = "empty warrant code"
        return pd.DataFrame(), diagnostic
    if cancel_event is not None and cancel_event.is_set():
        diagnostic["message"] = "cancelled"
        return pd.DataFrame(), diagnostic

    last_error = None
    normal_attempt = 0
    quota_attempt = 0
    while normal_attempt < FINMIND_REQUEST_RETRIES:
        if cancel_event is not None and cancel_event.is_set():
            diagnostic["message"] = "cancelled"
            return pd.DataFrame(), diagnostic
        try:
            _finmind_wait_for_rate_limit_gate()
            if cancel_event is not None and cancel_event.is_set():
                diagnostic["message"] = "cancelled"
                return pd.DataFrame(), diagnostic
            resp = get_thread_session().get(
                FINMIND_WARRANT_BRANCH_URL,
                headers=_finmind_headers(),
                params={"data_id": code, "date": date_s},
                timeout=(FINMIND_CONNECT_TIMEOUT, FINMIND_READ_TIMEOUT),
            )
            diagnostic["http_status"] = int(resp.status_code)
            if resp.status_code in (401, 402, 403, 429):
                _, message = _finmind_error_payload_from_response(resp)
                diagnostic["message"] = message
                if _finmind_is_rate_limit(resp.status_code, message):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"最新日權證 API｜權證={code}｜date={date_s}",
                        quota_attempt,
                        message,
                        resp=resp,
                    )
                    continue
                raise FinMindAuthorizationError(
                    f"FinMind 最新日權證 API 授權失敗：HTTP {resp.status_code}｜{message}"
                )
            if resp.status_code == 404:
                diagnostic["message"] = resp.text[:500]
                return pd.DataFrame(), diagnostic
            resp.raise_for_status()
            payload = resp.json()
            diagnostic["payload_keys"] = list(payload.keys()) if isinstance(payload, dict) else []
            diagnostic["message"] = _finmind_error_message(payload)
            payload_status = payload.get("status", 200) if isinstance(payload, dict) else 200
            if str(payload_status) not in ("200", "success", "True", "true"):
                if _finmind_is_rate_limit(payload_status, diagnostic["message"]):
                    quota_attempt += 1
                    _finmind_wait_after_rate_limit(
                        f"最新日權證 API｜權證={code}｜date={date_s}",
                        quota_attempt,
                        diagnostic["message"],
                    )
                    continue
                raise RuntimeError(
                    f"FinMind 最新日權證 API 回傳失敗：status={payload_status}｜"
                    f"{diagnostic['message']}"
                )
            data = payload.get("data", []) if isinstance(payload, dict) else []
            df = pd.DataFrame(data)
            diagnostic["rows"] = int(len(df))
            _finmind_debug_print_df(
                f"最新日權證 API 實際格式｜權證={code}｜{date_s}",
                df,
                once_key="schema:latest-warrant-code-api",
            )
            return df, diagnostic
        except (FinMindAuthorizationError, FinMindRateLimitError):
            raise
        except Exception as exc:
            last_error = exc
            normal_attempt += 1
            if normal_attempt >= FINMIND_REQUEST_RETRIES:
                break
            wait_sec = FINMIND_RETRY_BASE_WAIT * normal_attempt
            print(
                f"⚠️ FinMind 最新日權證 API 重試 {normal_attempt}/{FINMIND_REQUEST_RETRIES - 1}｜"
                f"權證={code}｜date={date_s}｜{exc}｜等待 {wait_sec:.1f} 秒"
            )
            if cancel_event is not None and cancel_event.wait(wait_sec):
                diagnostic["message"] = "cancelled"
                return pd.DataFrame(), diagnostic

    diagnostic["message"] = str(last_error or "unknown error")
    raise RuntimeError(
        f"FinMind 最新日權證 API 最終失敗：權證={code}｜date={date_s}｜{last_error}"
    )


def _finmind_convert_warrant_code_rows(
    raw: pd.DataFrame,
    trade_date,
    warrant_code: str,
    warrant_name_map: Dict[str, str],
    stock_code: str,
    stock_name: str,
    trader_name_map: Dict[str, str],
) -> pd.DataFrame:
    """將單一權證 API 回傳的所有分點轉成既有 warrant_events 格式。"""
    output_cols = [
        "Date", "branch", "broker_code", "warrant_code", "warrant_name",
        "underlying_code", "underlying_name", "buy_amount", "sell_amount",
        "net_amount", "buy_shares", "sell_shares", "side",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=output_cols)

    required = {"price", "buy", "sell", "stock_id", "securities_trader_id"}
    missing = required - set(raw.columns)
    if missing:
        raise RuntimeError(
            f"最新日權證 API 缺少欄位：權證={warrant_code}｜缺少={sorted(missing)}｜"
            f"實際欄位={raw.columns.tolist()}"
        )

    expected_code = normalize_openapi_warrant_code(warrant_code)
    work = raw.copy().fillna("")
    work["stock_id"] = work["stock_id"].astype(str).map(normalize_openapi_warrant_code)
    work = work[work["stock_id"] == expected_code].copy()
    if work.empty:
        raise RuntimeError(
            f"最新日權證 API 回傳代號不符：要求={expected_code}｜"
            f"實際={sorted(set(raw.get('stock_id', pd.Series(dtype=str)).astype(str)))[:10]}"
        )

    work["price"] = pd.to_numeric(work["price"], errors="coerce").fillna(0.0)
    work["buy"] = pd.to_numeric(work["buy"], errors="coerce").fillna(0.0)
    work["sell"] = pd.to_numeric(work["sell"], errors="coerce").fillna(0.0)
    work["buy_amount_row"] = work["price"] * work["buy"]
    work["sell_amount_row"] = work["price"] * work["sell"]
    work["broker_code"] = work["securities_trader_id"].astype(str).str.strip()
    raw_branch = work.get("securities_trader", pd.Series("", index=work.index)).astype(str).str.strip()
    mapped_branch = work["broker_code"].map(trader_name_map or {}).fillna("").astype(str)
    work["branch"] = np.where(mapped_branch.str.strip() != "", mapped_branch, raw_branch)
    work["branch"] = pd.Series(work["branch"], index=work.index).map(normalize_branch_name)
    work = work[(work["broker_code"] != "") & (work["branch"] != "")].copy()
    if work.empty:
        raise RuntimeError(f"最新日權證 API 無法還原任何分點：權證={expected_code}")

    grouped = work.groupby(
        ["broker_code", "branch", "stock_id"],
        as_index=False,
        dropna=False,
    ).agg(
        buy_shares_raw=("buy", "sum"),
        sell_shares_raw=("sell", "sum"),
        buy_amount=("buy_amount_row", "sum"),
        sell_amount=("sell_amount_row", "sum"),
    )
    grouped = grouped.rename(columns={"stock_id": "warrant_code"})
    grouped["Date"] = pd.Timestamp(trade_date).normalize()
    grouped["warrant_name"] = grouped["warrant_code"].map(warrant_name_map).fillna(grouped["warrant_code"])
    grouped["underlying_code"] = _normalize_stock_name_code_key(stock_code)
    grouped["underlying_name"] = str(stock_name or "")
    grouped["buy_shares"] = grouped["buy_shares_raw"] / 1000.0
    grouped["sell_shares"] = grouped["sell_shares_raw"] / 1000.0
    grouped["net_amount"] = grouped["buy_amount"] - grouped["sell_amount"]
    grouped = grouped[(grouped["buy_amount"] != 0) | (grouped["sell_amount"] != 0)].copy()
    grouped["side"] = np.where(grouped["net_amount"] >= 0, "買超", "賣超")
    return grouped[output_cols]


def _finmind_select_latest_day_probe_codes(
    query_codes,
    historical_events: pd.DataFrame | None,
    trade_date,
    max_count: int = 5,
) -> List[str]:
    """挑選最新日輕量探測權證；優先使用前一交易日成交金額最高者。"""
    normalized_codes = sorted({
        normalize_openapi_warrant_code(code)
        for code in (query_codes or [])
        if normalize_openapi_warrant_code(code)
    })
    if not normalized_codes:
        return []
    limit = max(1, min(int(max_count or 5), len(normalized_codes)))
    query_set = set(normalized_codes)
    selected = []

    if historical_events is not None and not historical_events.empty and {
        "Date", "warrant_code"
    }.issubset(historical_events.columns):
        work = historical_events[[
            col for col in ["Date", "warrant_code", "buy_amount", "sell_amount", "net_amount"]
            if col in historical_events.columns
        ]].copy()
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce").dt.normalize()
        work = work[work["Date"].notna() & (work["Date"] < pd.Timestamp(trade_date).normalize())]
        if not work.empty:
            latest_history_day = work["Date"].max()
            work = work[work["Date"] == latest_history_day].copy()
            if not bool(historical_events.attrs.get("_warrant_events_normalized")):
                work["warrant_code"] = work["warrant_code"].map(normalize_openapi_warrant_code)
            else:
                work["warrant_code"] = work["warrant_code"].astype(str)
            work = work[work["warrant_code"].isin(query_set)]
            if not work.empty:
                buy = pd.to_numeric(work.get("buy_amount", 0.0), errors="coerce").fillna(0.0)
                sell = pd.to_numeric(work.get("sell_amount", 0.0), errors="coerce").fillna(0.0)
                work["_probe_turnover"] = buy.abs() + sell.abs()
                ranked = (
                    work.groupby("warrant_code", as_index=False)["_probe_turnover"]
                    .sum()
                    .sort_values(["_probe_turnover", "warrant_code"], ascending=[False, True])
                )
                selected.extend(ranked["warrant_code"].head(limit).tolist())

    # 歷史資料不足時，以固定分散抽樣補足，確保結果可重現且涵蓋代號區間。
    if len(selected) < limit:
        remaining = [code for code in normalized_codes if code not in set(selected)]
        need = limit - len(selected)
        if need >= len(remaining):
            selected.extend(remaining)
        elif need > 0 and remaining:
            positions = np.linspace(0, len(remaining) - 1, num=need, dtype=int)
            selected.extend(remaining[int(pos)] for pos in positions)

    return list(dict.fromkeys(selected))[:limit]


def _finmind_fetch_latest_day_events_by_warrant_api(
    summary_df: pd.DataFrame,
    trade_date,
    warrant_name_map: Dict[str, str],
    stock_code: str,
    stock_name: str,
    trader_name_map: Dict[str, str],
    historical_events: pd.DataFrame | None = None,
    selected_branch_id_map: Dict[str, set] | None = None,
    all_empty_is_error: bool = True,
    cancel_event: threading.Event | None = None,
) -> tuple[pd.DataFrame, dict]:
    """最新日以「Summary有效 + 近期歷史 + 精選分點當日發現」聯集逐檔查 API。"""
    day = pd.Timestamp(trade_date).normalize()
    if cancel_event is not None and cancel_event.is_set():
        return pd.DataFrame(), {
            "date": day.strftime("%Y-%m-%d"),
            "active_codes": 0,
            "success_codes": 0,
            "empty_codes": 0,
            "failed_codes": 0,
            "endpoint_rows": 0,
            "event_rows": 0,
            "query_codes": [],
            "cancelled": True,
        }
    summary_active_codes = set(_finmind_active_warrant_codes(summary_df, day))
    summary_all_codes = set(
        summary_df.get("stock_id", pd.Series(dtype=str))
        .astype(str)
        .map(normalize_openapi_warrant_code)
    ) if summary_df is not None and not summary_df.empty else set()
    summary_all_codes.discard("")

    recent_history_codes = _finmind_recent_history_warrant_codes(
        historical_events,
        day,
        FINMIND_WARRANT_LATEST_DAY_HISTORY_BACKFILL_TRADING_DAYS,
    )
    target_warrant_codes = summary_all_codes | recent_history_codes
    selected_branch_raw, branch_discovery_stats = _finmind_discover_selected_branch_latest_day_rows(
        selected_branch_id_map,
        day,
        target_warrant_codes,
    )
    selected_branch_discovered_codes = set()
    if selected_branch_raw is not None and not selected_branch_raw.empty and "stock_id" in selected_branch_raw.columns:
        selected_branch_discovered_codes = set(
            selected_branch_raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
        )
        selected_branch_discovered_codes.discard("")

    query_codes = sorted(
        summary_active_codes
        | recent_history_codes
        | selected_branch_discovered_codes
    )
    stats = {
        "date": day.strftime("%Y-%m-%d"),
        "active_codes": len(query_codes),
        "summary_active_codes": len(summary_active_codes),
        "recent_history_codes": len(recent_history_codes),
        "selected_branch_discovered_codes": len(selected_branch_discovered_codes),
        "selected_branch_raw_rows": int(branch_discovery_stats.get("target_rows", 0) or 0),
        "selected_branch_repaired_rows": 0,
        "success_codes": 0,
        "empty_codes": 0,
        "failed_codes": 0,
        "endpoint_rows": 0,
        "event_rows": 0,
        "query_codes": list(query_codes),
        "cancelled": False,
        "all_empty": False,
        "not_ready": False,
        "fallback_used": False,
        "requested_date": day.strftime("%Y-%m-%d"),
        "resolved_date": "",
    }
    output_cols = [
        "Date", "branch", "broker_code", "warrant_code", "warrant_name",
        "underlying_code", "underlying_name", "buy_amount", "sell_amount",
        "net_amount", "buy_shares", "sell_shares", "side",
    ]
    if not query_codes:
        print(f"ℹ️ 最新交易日沒有可查詢的認購權證：{stock_code}｜日期={day.date()}")
        return pd.DataFrame(columns=output_cols), stats

    recent_only = recent_history_codes - summary_active_codes
    branch_only = selected_branch_discovered_codes - summary_active_codes - recent_history_codes
    print(
        f"🚀 最新交易日改用 FinMind 權證代號 API：{stock_code} {stock_name}｜"
        f"日期={day.date()}｜Summary有效={len(summary_active_codes):,}支｜"
        f"近期歷史補入={len(recent_only):,}支｜精選分點當日補入={len(branch_only):,}支｜"
        f"最終查詢={len(query_codes):,}支｜workers={FINMIND_WARRANT_LATEST_DAY_API_WORKERS}"
    )
    frames = []
    failures = []
    completed = 0
    successful_raw_data_codes = set()
    actual_queried_codes = []

    def _run_latest_code_batch(batch_codes: List[str], phase_label: str) -> None:
        nonlocal completed
        batch_codes = [code for code in batch_codes if code]
        if not batch_codes:
            return
        executor = ThreadPoolExecutor(
            max_workers=min(FINMIND_WARRANT_LATEST_DAY_API_WORKERS, len(batch_codes))
        )
        future_map = {
            executor.submit(_finmind_fetch_warrant_code_day_raw, code, day, cancel_event): code
            for code in batch_codes
        }
        actual_queried_codes.extend(batch_codes)
        phase_completed = 0
        try:
            pending_futures = set(future_map)
            while pending_futures:
                if cancel_event is not None and cancel_event.is_set():
                    stats["cancelled"] = True
                    for pending in pending_futures:
                        pending.cancel()
                    print(
                        f"🛑 最新日權證 API 已協作式中止：{stock_code}｜"
                        f"階段={phase_label}｜完成={phase_completed}/{len(batch_codes)}"
                    )
                    break

                done_futures, pending_futures = wait(
                    pending_futures,
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )
                if not done_futures:
                    continue

                for future in done_futures:
                    code = future_map[future]
                    completed += 1
                    phase_completed += 1
                    try:
                        raw, diagnostic = future.result()
                        if str((diagnostic or {}).get("message", "")) == "cancelled":
                            stats["cancelled"] = True
                            continue
                        stats["success_codes"] += 1
                        stats["endpoint_rows"] += int(len(raw))
                        if raw is None or raw.empty:
                            stats["empty_codes"] += 1
                        else:
                            successful_raw_data_codes.add(code)
                            converted = _finmind_convert_warrant_code_rows(
                                raw,
                                day,
                                code,
                                warrant_name_map,
                                stock_code,
                                stock_name,
                                trader_name_map,
                            )
                            if not converted.empty:
                                frames.append(converted)
                    except (FinMindAuthorizationError, FinMindRateLimitError):
                        if cancel_event is not None:
                            cancel_event.set()
                        for pending in pending_futures:
                            pending.cancel()
                        raise
                    except Exception as exc:
                        failures.append((code, str(exc)))
                        stats["failed_codes"] += 1

                    if phase_completed == 1 or phase_completed % 25 == 0 or phase_completed == len(batch_codes):
                        icon = "🧪" if phase_label == "輕量探測" else "📊"
                        print(
                            f"{icon} 最新日權證 API {phase_label}進度："
                            f"{phase_completed}/{len(batch_codes)}｜"
                            f"有原始資料權證={len(successful_raw_data_codes)}｜"
                            f"空資料={stats['empty_codes']}｜失敗={len(failures)}"
                        )
        finally:
            cancelled = bool(cancel_event is not None and cancel_event.is_set())
            executor.shutdown(wait=not cancelled, cancel_futures=True)

    probe_codes = _finmind_select_latest_day_probe_codes(
        query_codes,
        historical_events,
        day,
        max_count=5,
    )
    probe_code_set = set(probe_codes)
    remaining_codes = [code for code in query_codes if code not in probe_code_set]
    stats["intended_active_codes"] = len(query_codes)
    stats["probe_codes"] = list(probe_codes)
    stats["probe_count"] = len(probe_codes)
    stats["probe_short_circuit"] = False

    print(
        f"🧪 最新日權證輕量探測：{stock_code} {stock_name}｜日期={day.date()}｜"
        f"探測={len(probe_codes)}支｜來源=前一交易日成交金額優先｜"
        f"代號={','.join(probe_codes)}"
    )
    _run_latest_code_batch(probe_codes, "輕量探測")

    branch_ids = int(branch_discovery_stats.get("branch_ids", 0) or 0)
    branch_success = int(branch_discovery_stats.get("success_branch_ids", 0) or 0)
    branch_failed = int(branch_discovery_stats.get("failed_branch_ids", 0) or 0)
    branch_target_rows = int(branch_discovery_stats.get("target_rows", 0) or 0)
    branch_signal_reliable = bool(
        branch_ids > 0 and branch_failed == 0 and branch_success == branch_ids
    )
    branch_has_target_data = branch_target_rows > 0
    branch_corroborates_empty = branch_signal_reliable and not branch_has_target_data
    probe_all_success_empty = bool(
        probe_codes
        and stats["failed_codes"] == 0
        and stats["success_codes"] == len(probe_codes)
        and not successful_raw_data_codes
    )
    # 有設定精選分點時，必須五分點核對也可靠且全空才提前回退；
    # 沒有精選分點訊號時，使用 5 支高成交權證探測結果判斷。
    can_short_circuit = bool(
        probe_all_success_empty
        and not branch_has_target_data
        and (branch_corroborates_empty or branch_ids == 0)
        and FINMIND_WARRANT_LATEST_DAY_EMPTY_FALLBACK_ENABLE
    )

    if can_short_circuit:
        stats["all_empty"] = True
        stats["not_ready"] = True
        stats["probe_short_circuit"] = True
        stats["query_codes"] = list(actual_queried_codes)
        stats["active_codes"] = len(actual_queried_codes)
        stats["event_rows"] = 0
        corroboration = (
            f"精選分點={branch_success}/{branch_ids}個成功且目標列全空"
            if branch_ids
            else "未提供精選分點訊號"
        )
        print(
            f"⚠️ 最新日權證探測全空，提前判定資料尚未更新：{stock_code} {stock_name}｜"
            f"日期={day.date()}｜探測成功={stats['success_codes']}/{len(probe_codes)}｜"
            f"{corroboration}｜略過剩餘 {len(remaining_codes):,} 支逐權證請求"
        )
        return pd.DataFrame(columns=output_cols), stats

    if remaining_codes:
        reason = (
            "精選分點已出現當日目標資料"
            if branch_has_target_data
            else "探測權證已有當日資料"
            if successful_raw_data_codes
            else "探測訊號不足，為確保完整性"
        )
        print(
            f"✅ 最新日探測通過，啟動完整逐權證查詢：{stock_code}｜"
            f"原因={reason}｜剩餘={len(remaining_codes):,}支"
        )
        _run_latest_code_batch(remaining_codes, "完整查詢")

    stats["query_codes"] = list(actual_queried_codes)
    stats["active_codes"] = len(actual_queried_codes)

    if stats.get("cancelled") or (cancel_event is not None and cancel_event.is_set()):
        stats["cancelled"] = True
        return pd.DataFrame(columns=output_cols), stats

    if failures:
        preview = "；".join(f"{code}:{error[:140]}" for code, error in failures[:8])
        message = (
            f"最新日權證 API 不完整：失敗 {len(failures)}/{len(query_codes)} 支｜{preview}"
        )
        if FINMIND_WARRANT_LATEST_DAY_API_STRICT:
            raise RuntimeError(message)
        print(f"⚠️ {message}")

    events = (
        _concat_warrant_event_frames(frames)
        if frames
        else pd.DataFrame(columns=output_cols)
    )
    if not events.empty:
        events["Date"] = pd.to_datetime(events["Date"], errors="coerce").dt.normalize()
        events = events.dropna(subset=["Date"])
        for col in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
            events[col] = pd.to_numeric(events[col], errors="coerce").fillna(0.0)
        events["warrant_code"] = events["warrant_code"].map(normalize_openapi_warrant_code)
        events["branch"] = events["branch"].map(normalize_branch_name)
        events["broker_code"] = events["broker_code"].astype(str).str.strip()

    # 用「按分點查詢」的原始結果核對精選分點。若逐權證 API 漏列或金額不同，
    # 只替換相同日期 × 分點 ID × 權證的列，不影響其他分點與其他權證。
    branch_frames = []
    if selected_branch_raw is not None and not selected_branch_raw.empty:
        selected_branch_raw = selected_branch_raw.copy().fillna("")
        selected_branch_raw["stock_id"] = selected_branch_raw["stock_id"].astype(str).map(normalize_openapi_warrant_code)
        for warrant_code, raw_code in selected_branch_raw.groupby("stock_id", sort=False):
            if not warrant_code:
                continue
            converted = _finmind_convert_warrant_code_rows(
                raw_code,
                day,
                warrant_code,
                warrant_name_map,
                stock_code,
                stock_name,
                trader_name_map,
            )
            if not converted.empty:
                branch_frames.append(converted)

    if branch_frames:
        branch_events = _concat_warrant_event_frames(branch_frames)
        key_cols = ["Date", "broker_code", "warrant_code"]
        branch_events["Date"] = pd.to_datetime(branch_events["Date"], errors="coerce").dt.normalize()
        branch_events["broker_code"] = branch_events["broker_code"].astype(str).str.strip()
        branch_events["warrant_code"] = branch_events["warrant_code"].map(normalize_openapi_warrant_code)
        for col in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
            branch_events[col] = pd.to_numeric(branch_events[col], errors="coerce").fillna(0.0)

        if events.empty:
            repaired_count = len(branch_events)
        else:
            audit = branch_events[key_cols + ["buy_amount", "sell_amount"]].merge(
                events[key_cols + ["buy_amount", "sell_amount"]],
                on=key_cols,
                how="left",
                suffixes=("_branch", "_warrant"),
            )
            missing_mask = audit["buy_amount_warrant"].isna() | audit["sell_amount_warrant"].isna()
            mismatch_mask = (
                (audit["buy_amount_branch"] - audit["buy_amount_warrant"].fillna(0.0)).abs() > 0.01
            ) | (
                (audit["sell_amount_branch"] - audit["sell_amount_warrant"].fillna(0.0)).abs() > 0.01
            )
            repaired_count = int((missing_mask | mismatch_mask).sum())

        branch_key = branch_events[key_cols].astype(str).agg("|".join, axis=1)
        if not events.empty:
            event_key = events[key_cols].astype(str).agg("|".join, axis=1)
            events = events[~event_key.isin(set(branch_key))].copy()
        events = _concat_warrant_event_frames([events, branch_events])
        stats["selected_branch_repaired_rows"] = repaired_count
        print(
            f"✅ 最新日精選分點核對完成：{stock_code}｜日期={day.date()}｜"
            f"直接端點={len(branch_events):,}筆｜逐權證端點需修補={repaired_count:,}筆｜"
            "已以分點端點替換相同鍵值"
        )

    if events.empty:
        clean_all_empty = bool(
            query_codes
            and stats["failed_codes"] == 0
            and stats["success_codes"] == len(query_codes)
        )
        stats["all_empty"] = True
        stats["not_ready"] = clean_all_empty
        message = (
            f"最新日權證 API 全部無成交資料：{stock_code} {stock_name}｜"
            f"日期={day.date()}｜成功請求={stats['success_codes']}/{len(query_codes)}｜"
            "可能是 API 尚未完成更新"
        )

        # 所有請求均成功但結果全空，屬於資料發布時點問題，不是 API 完整性失敗。
        # 主流程會保留既有歷史事件，並自動使用最近已有資料的交易日產生週報。
        if clean_all_empty and FINMIND_WARRANT_LATEST_DAY_EMPTY_FALLBACK_ENABLE:
            print(
                f"⚠️ {message}｜已啟用最近有效交易日回退，"
                "不將本次全空結果視為致命錯誤"
            )
            return pd.DataFrame(columns=output_cols), stats

        if FINMIND_WARRANT_LATEST_DAY_API_STRICT and query_codes and all_empty_is_error:
            raise RuntimeError(message)
        if query_codes and not all_empty_is_error:
            print(
                f"ℹ️ 最新日補查權證皆無成交：{stock_code} {stock_name}｜"
                f"日期={day.date()}｜成功請求={stats['success_codes']}/{len(query_codes)}｜"
                "視為合法空結果，不影響主要最新日資料"
            )
        else:
            print(f"⚠️ {message}")
        return pd.DataFrame(columns=output_cols), stats

    events["Date"] = pd.to_datetime(events["Date"], errors="coerce").dt.normalize()
    events = events.dropna(subset=["Date"])
    for col in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
        events[col] = pd.to_numeric(events[col], errors="coerce").fillna(0.0)
    events["warrant_code"] = events["warrant_code"].map(normalize_openapi_warrant_code)
    events["branch"] = events["branch"].map(normalize_branch_name)
    events["broker_code"] = events["broker_code"].astype(str).str.strip()
    events["side"] = np.where(events["net_amount"] >= 0, "買超", "賣超")
    events = events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    stats["event_rows"] = int(len(events))
    print(
        f"✅ 最新日權證 API 完成：{stock_code}｜日期={day.date()}｜"
        f"請求成功={stats['success_codes']}/{len(query_codes)}｜"
        f"有成交權證={events['warrant_code'].nunique():,}支｜分點事件={len(events):,}筆｜"
        f"買進={fmt_money(float(events['buy_amount'].sum()))}｜"
        f"賣出={fmt_money(-float(events['sell_amount'].sum()))}｜"
        f"淨額={fmt_money(float(events['net_amount'].sum()))}"
    )
    return events, stats


def _finmind_replace_latest_day_with_warrant_api(
    base_events: pd.DataFrame,
    summary_df: pd.DataFrame,
    latest_day,
    warrant_name_map: Dict[str, str],
    stock_code: str,
    stock_name: str,
    trader_name_map: Dict[str, str],
    selected_branch_id_map: Dict[str, set] | None = None,
    precomputed_latest_events: pd.DataFrame | None = None,
    precomputed_api_stats: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """刪除最新日 Parquet 半成品，再以權證代號 API 完整結果取代。"""
    day = pd.Timestamp(latest_day).normalize()
    base = base_events.copy() if base_events is not None else pd.DataFrame()
    if not base.empty:
        base["Date"] = pd.to_datetime(base["Date"], errors="coerce").dt.normalize()
        base = base.dropna(subset=["Date"])
    if not FINMIND_WARRANT_LATEST_DAY_API_ENABLE:
        # 關閉 API 時，最新交易日應完整保留在 Parquet 路徑；不能先刪掉再回傳。
        print(
            f"ℹ️ 最新日權證 API 已關閉：{stock_code}｜日期={day.date()}｜"
            "正式資料保留完整 Parquet 交易日"
        )
        return base.reset_index(drop=True), {
            "date": day.strftime("%Y-%m-%d"),
            "active_codes": 0,
            "success_codes": 0,
            "empty_codes": 0,
            "failed_codes": 0,
            "endpoint_rows": 0,
            "event_rows": 0,
            "query_codes": [],
        }

    if precomputed_latest_events is None or precomputed_api_stats is None:
        latest_events, api_stats = _finmind_fetch_latest_day_events_by_warrant_api(
            summary_df,
            day,
            warrant_name_map,
            stock_code,
            stock_name,
            trader_name_map,
            historical_events=base,
            selected_branch_id_map=selected_branch_id_map,
        )
    else:
        latest_events = precomputed_latest_events.copy()
        api_stats = dict(precomputed_api_stats)

    if latest_events is None or latest_events.empty:
        historical_only = (
            base.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
            if not base.empty
            else pd.DataFrame(columns=[
                "Date", "branch", "broker_code", "warrant_code", "warrant_name",
                "underlying_code", "underlying_name", "buy_amount", "sell_amount",
                "net_amount", "buy_shares", "sell_shares", "side",
            ])
        )
        fallback_date = (
            pd.Timestamp(historical_only["Date"].max()).normalize()
            if not historical_only.empty and "Date" in historical_only.columns
            else pd.NaT
        )
        fallback_used = bool(
            api_stats.get("not_ready", False)
            and FINMIND_WARRANT_LATEST_DAY_EMPTY_FALLBACK_ENABLE
            and pd.notna(fallback_date)
        )
        api_stats["fallback_used"] = fallback_used
        api_stats["requested_date"] = day.strftime("%Y-%m-%d")
        api_stats["resolved_date"] = (
            fallback_date.strftime("%Y-%m-%d") if pd.notna(fallback_date) else ""
        )
        if fallback_used:
            print(
                f"↩️ 最新日權證資料尚未更新，已自動回退最近有效交易日："
                f"{stock_code}｜要求={day.date()}｜實際={fallback_date.date()}｜"
                f"保留歷史事件={len(historical_only):,}筆"
            )
        elif api_stats.get("not_ready", False):
            print(
                f"⚠️ 最新日權證資料尚未更新，且找不到可回退的歷史事件："
                f"{stock_code}｜要求={day.date()}"
            )
        return historical_only, api_stats

    removed_rows = int((base["Date"] == day).sum()) if not base.empty and "Date" in base.columns else 0
    if removed_rows:
        base = base[base["Date"] != day].copy()
        print(
            f"🧹 已移除最新日 Parquet 資料，準備由 API 取代："
            f"{stock_code}｜日期={day.date()}｜移除={removed_rows:,}筆"
        )

    api_stats["fallback_used"] = False
    api_stats["requested_date"] = day.strftime("%Y-%m-%d")
    api_stats["resolved_date"] = day.strftime("%Y-%m-%d")
    merged = _concat_warrant_event_frames([base, latest_events])
    merged["Date"] = pd.to_datetime(merged["Date"], errors="coerce").dt.normalize()
    merged = merged.dropna(subset=["Date"])
    merged = merged.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    print(
        f"✅ 最新交易日已由 API 完整取代 Parquet：{stock_code}｜"
        f"日期={day.date()}｜API事件={len(latest_events):,}筆｜合併後={len(merged):,}筆"
    )
    return merged, api_stats




def fetch_warrant_events_full_market(stock_code: str, stock_name: str, start_date, end_date, cancel_event: threading.Event | None = None) -> pd.DataFrame:
    """FinMind 權證分點主流程。

    歷史日全市場 Parquet、最新日主要權證 API 與官方發行商預載同時進行；
    歷史完成後只補查最近實際出現、但第一階段尚未查詢的少數權證。
    """
    code = _normalize_stock_name_code_key(stock_code)
    empty_columns = [
        "Date", "branch", "broker_code", "warrant_code", "warrant_name",
        "underlying_code", "underlying_name", "buy_amount", "sell_amount",
        "net_amount", "buy_shares", "sell_shares", "side",
    ]

    if not REPORT_LIVE_ONLY:
        snapshot = load_gsheet_warrant_events_snapshot(code, start_date=start_date, end_date=end_date)
        if snapshot is not None and not snapshot.empty:
            return snapshot
        if ACTION_CACHE_ONLY_MODE:
            raise RuntimeError(
                f"嚴格快取模式找不到 FinMind 權證快照：{_gsheet_cache_key(code, start_date, end_date)}"
            )
    if not LIVE_FETCH_ENABLE:
        return pd.DataFrame(columns=empty_columns)

    summary = _FINMIND_MULTI_STOCK_SUMMARY_CACHE.get(code)
    if summary is None:
        summary = _finmind_get_warrant_summary(code, start_date, end_date)
    if summary is None or summary.empty:
        return pd.DataFrame(columns=empty_columns)

    warrant_name_map = _FINMIND_MULTI_STOCK_NAME_MAP_CACHE.get(code, {}) or _finmind_target_warrant_name_map(summary)
    trader_name_map, _ = _finmind_securities_trader_maps()
    if not trader_name_map:
        print("⚠️ FinMind 分點 ID 對照表為空；本次會保留 API／Parquet 券商簡稱")

    selected_branch_names = _get_selected_branch_flow_set() if SELECTED_BRANCH_FLOW_ENABLE else set()
    selected_branch_id_map = _resolve_selected_branch_ids(selected_branch_names) if selected_branch_names else {}
    print(f"🧩 精選分點正式 ID 結果：{ {name: sorted(ids) for name, ids in selected_branch_id_map.items()} }")

    trading_dates = _finmind_get_trading_dates(start_date, end_date)
    if not trading_dates:
        return pd.DataFrame(columns=empty_columns)
    trading_dates = [pd.Timestamp(day).normalize() for day in trading_dates]
    latest_day = trading_dates[-1]
    # API 啟用時，最後一個交易日由逐權證 API 完整取代；API 關閉時，
    # 最後一日仍必須保留在 Parquet 歷史路徑，不能平白少掉最新交易日。
    historical_dates = (
        trading_dates[:-1]
        if FINMIND_WARRANT_LATEST_DAY_API_ENABLE
        else trading_dates
    )
    historical_jobs = [
        (day, active_codes)
        for day in historical_dates
        if (active_codes := _finmind_active_warrant_codes(summary, day))
    ]

    # 第一階段最新日查詢只使用 Summary + 精選分點直接發現，與歷史 69 日同步進行。
    latest_executor = None
    latest_future = None
    latest_cancel_event = cancel_event if cancel_event is not None else threading.Event()
    if FINMIND_WARRANT_LATEST_DAY_API_ENABLE and FINMIND_WARRANT_PIPELINE_PARALLEL_ENABLE:
        latest_executor = ThreadPoolExecutor(max_workers=1)
        latest_future = latest_executor.submit(
            _finmind_fetch_latest_day_events_by_warrant_api,
            summary,
            latest_day,
            warrant_name_map,
            code,
            stock_name,
            trader_name_map,
            None,
            selected_branch_id_map,
            True,
            latest_cancel_event,
        )
        print(
            f"🚀 權證流水線平行啟動：歷史 {len(historical_jobs)} 日 + 最新日主要 API + 官方發行商預載"
        )

    prefetched_events = _finmind_get_multi_stock_prefetched_events(code, start_date, end_date)
    historical_failures = []
    historical_empty_dates = 0

    if prefetched_events is not None:
        prefetched_stats = dict(_FINMIND_WARRANT_RUN_STATS.get(code, {}) or {})
        prefetched_failed_dates = list(prefetched_stats.get("failed_date_list", []) or [])
        historical_failures.extend((day_s, "multi-stock prefetch failed") for day_s in prefetched_failed_dates)
        historical_events = prefetched_events.copy()
        if not historical_events.empty:
            historical_events["Date"] = pd.to_datetime(historical_events["Date"], errors="coerce").dt.normalize()
            historical_events = historical_events.dropna(subset=["Date"])
            latest_comparison = (
                historical_events["Date"] < latest_day
                if FINMIND_WARRANT_LATEST_DAY_API_ENABLE
                else historical_events["Date"] <= latest_day
            )
            historical_events = historical_events[latest_comparison].copy()
        historical_event_dates = int(historical_events["Date"].nunique()) if not historical_events.empty else 0
        historical_empty_dates = max(0, len(historical_jobs) - historical_event_dates)
        print(
            f"⚡ 使用多股票聯集歷史預處理結果：{code}｜{len(historical_events):,}筆｜"
            f"歷史日至 {historical_dates[-1].date() if historical_dates else '-'}"
        )
    else:
        compact_events, compact_dates = _finmind_load_target_events_from_market_compact(
            summary, historical_jobs, warrant_name_map, code, stock_name, trader_name_map,
        ) if historical_jobs else (pd.DataFrame(), set())
        remaining_jobs = [
            (day, active_codes) for day, active_codes in historical_jobs
            if pd.Timestamp(day).normalize() not in compact_dates
        ]
        print(
            f"🚀 FinMind 歷史權證分點處理：{code} {stock_name}｜"
            f"歷史交易日 {len(historical_jobs):,} 日｜精簡快取 {len(compact_dates):,} 日｜"
            f"原始 Parquet 補處理 {len(remaining_jobs):,} 日｜workers={FINMIND_WARRANT_DOWNLOAD_WORKERS}"
        )
        frames = [compact_events] if compact_events is not None and not compact_events.empty else []
        compact_event_dates = set(
            pd.to_datetime(compact_events["Date"], errors="coerce").dropna().dt.normalize().tolist()
        ) if compact_events is not None and not compact_events.empty else set()
        historical_empty_dates = max(0, len(compact_dates - compact_event_dates))
        completed = len(compact_dates)

        if remaining_jobs:
            with ThreadPoolExecutor(max_workers=FINMIND_WARRANT_DOWNLOAD_WORKERS) as executor:
                future_map = {
                    executor.submit(
                        _finmind_process_warrant_day,
                        day, active_codes, warrant_name_map, code, stock_name, trader_name_map,
                    ): day
                    for day, active_codes in remaining_jobs
                }
                for future in as_completed(future_map):
                    day = future_map[future]
                    completed += 1
                    try:
                        day_df = future.result()
                        if day_df is None or day_df.empty:
                            historical_empty_dates += 1
                        else:
                            frames.append(day_df)
                    except Exception as exc:
                        historical_failures.append((pd.Timestamp(day).strftime("%Y-%m-%d"), str(exc)))
                        print(f"❌ FinMind 歷史權證日期失敗：{pd.Timestamp(day).date()}｜{exc}")
                    if completed == 1 or completed % 5 == 0 or completed == len(historical_jobs):
                        print(
                            f"📊 FinMind 歷史權證進度：{completed}/{len(historical_jobs)}｜"
                            f"資料區塊={len(frames)}｜空資料={historical_empty_dates}｜失敗={len(historical_failures)}"
                        )
        historical_events = _concat_warrant_event_frames(frames) if frames else pd.DataFrame(columns=empty_columns)

    if historical_failures and FINMIND_STRICT_WARRANT_COMPLETENESS:
        latest_cancel_event.set()
        if latest_executor is not None:
            latest_executor.shutdown(wait=False, cancel_futures=True)
        samples = "；".join(f"{day}:{error[:120]}" for day, error in historical_failures[:5])
        raise RuntimeError(
            f"FinMind 歷史權證分點不完整：失敗 {len(historical_failures)}/{len(historical_jobs)} 日｜{samples}"
        )

    if historical_events is None or historical_events.empty:
        historical_events = pd.DataFrame(columns=empty_columns)
    else:
        already_normalized = bool(historical_events.attrs.get("_warrant_events_normalized"))
        historical_events["Date"] = pd.to_datetime(historical_events["Date"], errors="coerce").dt.normalize()
        historical_events = historical_events.dropna(subset=["Date"])
        for col in ["buy_amount", "sell_amount", "net_amount", "buy_shares", "sell_shares"]:
            historical_events[col] = pd.to_numeric(historical_events[col], errors="coerce").fillna(0.0)
        if not already_normalized:
            historical_events["warrant_code"] = historical_events["warrant_code"].map(normalize_openapi_warrant_code)
            historical_events["branch"] = historical_events["branch"].map(normalize_branch_name)
            historical_events["broker_code"] = historical_events["broker_code"].astype(str).str.strip()
        historical_events["side"] = np.where(historical_events["net_amount"] >= 0, "買超", "賣超")
        historical_events.attrs["_warrant_events_normalized"] = True

    if not FINMIND_WARRANT_LATEST_DAY_API_ENABLE:
        latest_events = pd.DataFrame(columns=empty_columns)
        latest_api_stats = {
            "date": latest_day.strftime("%Y-%m-%d"),
            "active_codes": 0,
            "success_codes": 0,
            "empty_codes": 0,
            "failed_codes": 0,
            "endpoint_rows": 0,
            "event_rows": 0,
            "query_codes": [],
            "cancelled": False,
        }
        print(f"ℹ️ 最新日權證 API 已關閉，完全略過逐權證與精選分點補查：{code}")
    elif latest_future is not None:
        try:
            latest_events, latest_api_stats = latest_future.result()
        finally:
            latest_executor.shutdown(wait=True)
    else:
        # 非平行組態已完成歷史事件，直接帶入可一次涵蓋近期歷史權證，避免第二輪重查。
        latest_events, latest_api_stats = _finmind_fetch_latest_day_events_by_warrant_api(
            summary, latest_day, warrant_name_map, code, stock_name, trader_name_map,
            historical_events=historical_events,
            selected_branch_id_map=selected_branch_id_map,
            cancel_event=latest_cancel_event,
        )

    # 第二階段只補平行第一階段尚未看見、但歷史完成後確認近期曾出現的權證。
    recent_history_codes = _finmind_recent_history_warrant_codes(
        historical_events,
        latest_day,
        FINMIND_WARRANT_LATEST_DAY_HISTORY_BACKFILL_TRADING_DAYS,
    ) if FINMIND_WARRANT_LATEST_DAY_API_ENABLE else set()
    queried_codes = set(latest_api_stats.get("query_codes", []) or [])
    missing_recent_codes = sorted(recent_history_codes - queried_codes)
    if (
        FINMIND_WARRANT_LATEST_DAY_API_ENABLE
        and missing_recent_codes
        and not bool(latest_api_stats.get("not_ready", False))
    ):
        print(
            f"🔁 最新日第二階段補查：近期歷史額外權證={len(missing_recent_codes):,}支｜"
            "只補第一階段未查代號"
        )
        backfill_summary = pd.DataFrame({
            "stock_id": missing_recent_codes,
            "listing_date": [latest_day] * len(missing_recent_codes),
            "last_trade_date": [latest_day] * len(missing_recent_codes),
        })
        backfill_events, backfill_stats = _finmind_fetch_latest_day_events_by_warrant_api(
            backfill_summary,
            latest_day,
            warrant_name_map,
            code,
            stock_name,
            trader_name_map,
            historical_events=None,
            selected_branch_id_map={},
            all_empty_is_error=False,
            cancel_event=latest_cancel_event,
        )
        latest_events = _concat_warrant_event_frames([latest_events, backfill_events])
        if not latest_events.empty:
            latest_events["Date"] = pd.to_datetime(latest_events["Date"], errors="coerce").dt.normalize()
            latest_events["broker_code"] = latest_events["broker_code"].astype(str).str.strip()
            latest_events["warrant_code"] = latest_events["warrant_code"].map(normalize_openapi_warrant_code)
            latest_events = latest_events.drop_duplicates(
                subset=["Date", "broker_code", "warrant_code"], keep="last"
            ).reset_index(drop=True)
        all_query_codes = queried_codes | set(backfill_stats.get("query_codes", []) or [])
        latest_api_stats["query_codes"] = sorted(all_query_codes)
        latest_api_stats["active_codes"] = len(all_query_codes)
        latest_api_stats["recent_history_codes"] = len(recent_history_codes)
        for key in ["success_codes", "empty_codes", "failed_codes", "endpoint_rows"]:
            latest_api_stats[key] = int(latest_api_stats.get(key, 0) or 0) + int(backfill_stats.get(key, 0) or 0)
        latest_api_stats["event_rows"] = int(len(latest_events))
    else:
        latest_api_stats["recent_history_codes"] = len(recent_history_codes)
        if (
            FINMIND_WARRANT_LATEST_DAY_API_ENABLE
            and missing_recent_codes
            and bool(latest_api_stats.get("not_ready", False))
        ):
            print(
                f"⏭️ 最新日探測已確認尚未更新，略過第二階段補查與額外精選分點發現："
                f"{code}｜原可補={len(missing_recent_codes):,}支"
            )

    events, latest_api_stats = _finmind_replace_latest_day_with_warrant_api(
        historical_events,
        summary,
        latest_day,
        warrant_name_map,
        code,
        stock_name,
        trader_name_map,
        selected_branch_id_map=selected_branch_id_map,
        precomputed_latest_events=latest_events,
        precomputed_api_stats=latest_api_stats,
    )

    latest_failed = int(latest_api_stats.get("failed_codes", 0) or 0)
    latest_success = int(latest_api_stats.get("success_codes", 0) or 0)
    latest_active = int(latest_api_stats.get("active_codes", 0) or 0)
    latest_has_data = int(latest_api_stats.get("event_rows", 0) or 0) > 0
    latest_fallback_used = bool(latest_api_stats.get("fallback_used", False))
    latest_date_count = 1 if FINMIND_WARRANT_LATEST_DAY_API_ENABLE else 0
    latest_success_date_count = (
        1
        if latest_date_count
        and latest_has_data
        and latest_failed == 0
        and (latest_success == latest_active or latest_active == 0)
        else 0
    )
    stats = {
        "total_dates": len(historical_jobs) + latest_date_count,
        "success_dates": max(0, len(historical_jobs) - len(historical_failures)) + latest_success_date_count,
        "empty_dates": historical_empty_dates + (1 if latest_date_count and not latest_has_data else 0),
        "failed_dates": len(historical_failures) + (1 if latest_date_count and latest_failed > 0 else 0),
        "latest_available_date": latest_day.strftime("%Y-%m-%d") if latest_has_data else (
            events["Date"].max().strftime("%Y-%m-%d") if events is not None and not events.empty else ""
        ),
        "latest_api_active_codes": latest_active,
        "latest_api_success_codes": latest_success,
        "latest_api_empty_codes": int(latest_api_stats.get("empty_codes", 0) or 0),
        "latest_api_event_rows": int(latest_api_stats.get("event_rows", 0) or 0),
        "latest_api_all_empty": bool(latest_api_stats.get("all_empty", False)),
        "latest_api_fallback_used": latest_fallback_used,
        "latest_api_requested_date": str(latest_api_stats.get("requested_date", "") or ""),
        "latest_api_resolved_date": str(latest_api_stats.get("resolved_date", "") or ""),
    }
    _FINMIND_WARRANT_RUN_STATS[code] = stats

    should_write_snapshot = bool(
        stats["failed_dates"] == 0 and events is not None and not events.empty and (
            not REPORT_LIVE_ONLY
            or (ACTION_REFRESH_CONTROLS_REPORT_DATA and ACTION_FORCE_REFRESH)
            or WARRANT_CACHE_FORCE_REFRESH
        )
    )
    if should_write_snapshot:
        save_gsheet_warrant_events_snapshot(code, stock_name, events, start_date, end_date)

    if events is None or events.empty:
        return pd.DataFrame(columns=empty_columns)
    events = events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    latest_source_label = (
        "API尚未更新，回退最近有效交易日"
        if latest_fallback_used
        else "平行權證代號API"
        if FINMIND_WARRANT_LATEST_DAY_API_ENABLE and FINMIND_WARRANT_PIPELINE_PARALLEL_ENABLE
        else "權證代號API"
        if FINMIND_WARRANT_LATEST_DAY_API_ENABLE
        else "已關閉"
    )
    print(
        f"✅ FinMind 權證分點完成：{code}｜{len(events):,}筆｜"
        f"歷史=Parquet/全市場精簡快取｜最新日={latest_source_label}｜"
        f"日期 {events['Date'].min().date()} ~ {events['Date'].max().date()}"
    )
    return events




# ============================================================
# 對外入口
# ============================================================

def _prepare_report_news_items(stock_code: str, stock_name: str, cancel_event: threading.Event | None = None) -> List[dict]:
    """準備週報新聞素材；快取檢查由完整新聞管線統一執行一次。"""
    with report_stage_timer(f"{stock_code}｜新聞資料準備"):
        if cancel_event is not None and cancel_event.is_set():
            return []
        if is_compact_report_mode():
            print("📄 精簡週報模式：略過本週重點、多來源新聞抓取與 Gemini 新聞統整")
            return []
        if ACTION_CACHE_ONLY_MODE:
            print(
                f"☁️ Action=0 嚴格快取模式未命中當日新聞快取：{stock_code}，"
                "不執行新聞搜尋"
            )
            return []
        return fetch_multi_source_news_articles(
            stock_code,
            stock_name,
            max_items=NEWS_GOOGLE_MAX_ITEMS,
            cancel_event=cancel_event,
        )


def _prepare_report_news_pipeline(stock_code: str, stock_name: str, cancel_event: threading.Event | None = None) -> dict:
    """新聞快取只檢查一次；命中本機後不再連 Google Sheet或重進 build_news_points。"""
    with report_stage_timer(f"{stock_code}｜完整新聞管線"):
        if cancel_event is not None and cancel_event.is_set():
            return {"news_items": [], "news_points": []}
        if is_compact_report_mode():
            return {"news_items": [], "news_points": []}

        cached_news_points = _load_gsheet_news_points_cache_for_display(
            stock_code,
            stock_name,
            allow_stale=False,
        )
        if cached_news_points:
            print(
                f"⚡ 今日新聞本機快取已存在，略過六來源新聞抓取與 Gemini："
                f"{stock_code}｜{len(cached_news_points)} 點"
            )
            return {
                "news_items": [],
                "news_points": list(cached_news_points or []),
            }

        news_items = _prepare_report_news_items(stock_code, stock_name, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return {"news_items": [], "news_points": []}
        news_points = build_news_points(
            stock_code,
            stock_name,
            news_items,
            ctx=None,
            cache_lookup=False,
        )
        return {
            "news_items": news_items,
            "news_points": list(news_points or []),
        }


_REPORT_NEWS_PREFETCH_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_REPORT_NEWS_PREFETCH_FUTURES = {}
_REPORT_NEWS_PREFETCH_CANCEL_EVENTS = {}
_REPORT_NEWS_PREFETCH_LOCK = threading.RLock()


def _prepare_report_news_pipeline_by_code(stock_code: str, cancel_event: threading.Event) -> dict:
    code = _normalize_stock_name_code_key(stock_code)
    if cancel_event.is_set():
        return {"news_items": [], "news_points": []}
    stock_name = get_tw_stock_name(code)
    return _prepare_report_news_pipeline(code, stock_name, cancel_event=cancel_event)


def _schedule_next_report_news_prefetch(stock_code: str):
    """在目前股票建圖時預抓下一檔新聞，下一檔開始後直接接續既有 Future。"""
    code = _normalize_stock_name_code_key(stock_code)
    if not code or is_compact_report_mode():
        return None
    with _REPORT_NEWS_PREFETCH_LOCK:
        existing = _REPORT_NEWS_PREFETCH_FUTURES.get(code)
        if existing is not None:
            return existing
        cancel_event = threading.Event()
        future = _REPORT_NEWS_PREFETCH_EXECUTOR.submit(
            _prepare_report_news_pipeline_by_code,
            code,
            cancel_event,
        )
        _REPORT_NEWS_PREFETCH_FUTURES[code] = future
        _REPORT_NEWS_PREFETCH_CANCEL_EVENTS[code] = cancel_event
        print(f"🚀 已提前啟動下一檔新聞管線：{code}｜與目前建圖重疊")
        return future


def _take_report_news_prefetch(stock_code: str):
    code = _normalize_stock_name_code_key(stock_code)
    with _REPORT_NEWS_PREFETCH_LOCK:
        return (
            _REPORT_NEWS_PREFETCH_FUTURES.get(code),
            _REPORT_NEWS_PREFETCH_CANCEL_EVENTS.get(code),
        )


def _clear_report_news_prefetch(stock_code: str):
    code = _normalize_stock_name_code_key(stock_code)
    with _REPORT_NEWS_PREFETCH_LOCK:
        _REPORT_NEWS_PREFETCH_FUTURES.pop(code, None)
        _REPORT_NEWS_PREFETCH_CANCEL_EVENTS.pop(code, None)


def _shutdown_report_news_prefetch():
    with _REPORT_NEWS_PREFETCH_LOCK:
        for event in _REPORT_NEWS_PREFETCH_CANCEL_EVENTS.values():
            event.set()
        for future in _REPORT_NEWS_PREFETCH_FUTURES.values():
            future.cancel()
        _REPORT_NEWS_PREFETCH_FUTURES.clear()
        _REPORT_NEWS_PREFETCH_CANCEL_EVENTS.clear()
    _REPORT_NEWS_PREFETCH_EXECUTOR.shutdown(wait=False, cancel_futures=True)


# ============================================================
# ============================================================
# FinMind 當日資料完整性保護
# ============================================================
# FinMind 當日權證分點可能在盤後分批更新。正式圖以 TWSE／TPEx 官方權證
# 當日成交量做交叉驗證；採「代號覆蓋率門檻 + 全體總量差異容忍度」，
# 不再因單一冷門權證延遲或官方／FinMind 多出一碼就否決整批資料。
FINMIND_WARRANT_CURRENT_DAY_GUARD_ENABLE = os.getenv(
    "FINMIND_WARRANT_CURRENT_DAY_GUARD_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
FINMIND_WARRANT_CURRENT_DAY_OPENAPI_VERIFY_ENABLE = os.getenv(
    "FINMIND_WARRANT_CURRENT_DAY_OPENAPI_VERIFY_ENABLE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE = min(
    1.0,
    max(0.0, float(os.getenv("FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE", "0.95"))),
)
FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_LOTS = max(
    0.0,
    float(os.getenv("FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_LOTS", "1.0")),
)
FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_PCT = max(
    0.0,
    float(os.getenv("FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_PCT", "0.001")),
)
FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_LOTS = max(
    0.0,
    float(os.getenv("FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_LOTS", "20.0")),
)
FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_PCT = max(
    0.0,
    float(os.getenv("FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_PCT", "0.002")),
)
FINMIND_WARRANT_CURRENT_DAY_MIN_OFFICIAL_CODES = max(
    1,
    int(os.getenv("FINMIND_WARRANT_CURRENT_DAY_MIN_OFFICIAL_CODES", "1")),
)


def _normalize_official_trade_date_series(series: pd.Series) -> pd.Series:
    """統一 TWSE / TPEx OpenAPI 的斜線、連字號與空白日期格式。"""
    raw = series.astype(str).str.strip().str.replace(".", "-", regex=False).str.replace("/", "-", regex=False)
    return pd.to_datetime(raw, errors="coerce").dt.tz_localize(None).dt.normalize()


def _finmind_current_day_official_volume_audit(
    events_df: pd.DataFrame,
    stock_code: str,
    stock_name: str,
    target_date,
) -> dict:
    """用官方權證成交量驗證 FinMind 當日分點資料是否完整。

    同時檢查官方端與 FinMind 端的權證代號覆蓋，避免只比到已發布市場的一小部分，
    或因 TWSE / TPEx 日期格式不同而誤判。官方相關市場尚未更新時，明確標示為等待官方資料。
    """
    result = {
        "verified": False,
        "reason": "尚未驗證",
        "audit_df": pd.DataFrame(),
        "official_codes": 0,
        "presence_codes": 0,
        "matched_codes": 0,
        "coverage": 0.0,
        "matched_volume_coverage": 0.0,
        "official_total_lots": 0.0,
        "finmind_buy_total_lots": 0.0,
        "finmind_sell_total_lots": 0.0,
        "total_allowed_diff_lots": 0.0,
        "status": "unverified",
    }
    if not FINMIND_WARRANT_CURRENT_DAY_OPENAPI_VERIFY_ENABLE:
        result["reason"] = "官方成交量驗證已關閉"
        return result
    if events_df is None or events_df.empty:
        result["reason"] = "FinMind 當日事件為空"
        return result

    target_ts = pd.Timestamp(target_date).normalize()
    code = _normalize_stock_name_code_key(stock_code)

    try:
        summary = _finmind_get_warrant_summary(code, target_ts, target_ts)
        active_codes = set(
            summary.get("stock_id", pd.Series(dtype=str))
            .astype(str)
            .map(normalize_openapi_warrant_code)
        )
        active_codes.discard("")
        if not active_codes:
            result["reason"] = "FinMind 找不到當日有效認購權證母體"
            return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            twse_future = executor.submit(fetch_twse_openapi_warrant_daily_df)
            tpex_future = executor.submit(fetch_tpex_openapi_warrant_daily_df)
            try:
                twse_df = twse_future.result()
            except Exception as exc:
                print(f"⚠️ 當日完整性驗證 TWSE OpenAPI 失敗：{exc}")
                twse_df = pd.DataFrame()
            try:
                tpex_df = tpex_future.result()
            except Exception as exc:
                print(f"⚠️ 當日完整性驗證 TPEx OpenAPI 失敗：{exc}")
                tpex_df = pd.DataFrame()

        official_parts = []
        source_latest = {}
        for source_name, source_df in [("TWSE", twse_df), ("TPEx", tpex_df)]:
            if not isinstance(source_df, pd.DataFrame) or source_df.empty:
                source_latest[source_name] = "-"
                continue
            part = source_df.copy()
            for col in ["交易日期", "代號", "成交量", "市場", "名稱"]:
                if col not in part.columns:
                    part[col] = "" if col != "成交量" else 0
            part["_trade_date"] = _normalize_official_trade_date_series(part["交易日期"])
            part["_source"] = source_name
            latest = part["_trade_date"].dropna().max()
            source_latest[source_name] = latest.strftime("%Y-%m-%d") if pd.notna(latest) else "-"
            official_parts.append(part)

        if not official_parts:
            result["reason"] = "TWSE / TPEx 官方權證資料皆無法取得"
            return result

        official_all = pd.concat(official_parts, ignore_index=True, sort=False)
        official_all["代號"] = official_all["代號"].map(normalize_openapi_warrant_code)
        official_all["成交量"] = pd.to_numeric(official_all["成交量"], errors="coerce").fillna(0.0)
        relevant_all = official_all[official_all["代號"].isin(active_codes)].copy()
        official_target = relevant_all[
            (relevant_all["_trade_date"] == target_ts)
            & (relevant_all["成交量"] > 0)
        ].copy()

        current = events_df.copy()
        current["Date"] = pd.to_datetime(current["Date"], errors="coerce").dt.tz_localize(None).dt.normalize()
        current = current[current["Date"] == target_ts].copy()
        current["warrant_code"] = current["warrant_code"].map(normalize_openapi_warrant_code)
        for col in ["buy_shares", "sell_shares"]:
            if col not in current.columns:
                current[col] = 0.0
            current[col] = pd.to_numeric(current[col], errors="coerce").fillna(0.0)
        fin = current.groupby("warrant_code", as_index=False).agg(
            finmind_buy_lots=("buy_shares", "sum"),
            finmind_sell_lots=("sell_shares", "sum"),
            finmind_rows=("warrant_code", "size"),
        )
        fin = fin[(fin["finmind_buy_lots"].abs() + fin["finmind_sell_lots"].abs()) > 0].copy()

        if official_target.empty:
            relevant_latest = relevant_all["_trade_date"].dropna().max() if not relevant_all.empty else pd.NaT
            relevant_latest_text = relevant_latest.strftime("%Y-%m-%d") if pd.notna(relevant_latest) else "-"
            result["status"] = "official_pending"
            result["reason"] = (
                f"與本標的有效權證相符的官方資料尚未發布到 {target_ts.strftime('%Y-%m-%d')}"
                f"（相關權證最新 {relevant_latest_text}；TWSE最新 {source_latest.get('TWSE', '-')}；"
                f"TPEx最新 {source_latest.get('TPEx', '-')}）"
            )
            print(f"ℹ️ FinMind 當日完整性等待官方資料：{code} {stock_name}｜{result['reason']}")
            return result

        official = (
            official_target.sort_values("成交量")
            .drop_duplicates(subset=["代號"], keep="last")
            [["代號", "名稱", "市場", "成交量", "_source"]]
            .rename(columns={
                "代號": "warrant_code",
                "名稱": "official_name",
                "市場": "official_market",
                "成交量": "official_volume_lots",
                "_source": "official_source",
            })
        )

        # outer merge 保留雙方缺碼診斷；正式門檻改採「代號存在覆蓋率 + 全體總量差異」。
        # 單一冷門權證逐檔延遲不再直接否決整批，但若缺碼造成總量差異過大仍會退回。
        audit = official.merge(fin, on="warrant_code", how="outer", indicator=True)
        for col in ["official_volume_lots", "finmind_buy_lots", "finmind_sell_lots", "finmind_rows"]:
            audit[col] = pd.to_numeric(audit[col], errors="coerce").fillna(0.0)
        for col in ["official_name", "official_market", "official_source"]:
            if col not in audit.columns:
                audit[col] = ""
            audit[col] = audit[col].fillna("").astype(str)

        audit["buy_diff_lots"] = audit["finmind_buy_lots"] - audit["official_volume_lots"]
        audit["sell_diff_lots"] = audit["finmind_sell_lots"] - audit["official_volume_lots"]
        audit["allowed_diff_lots"] = np.maximum(
            FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_LOTS,
            audit["official_volume_lots"].abs() * FINMIND_WARRANT_CURRENT_DAY_VOLUME_TOLERANCE_PCT,
        )
        audit["code_coverage_match"] = audit["_merge"].eq("both")
        audit["buy_match"] = audit["code_coverage_match"] & (
            audit["buy_diff_lots"].abs() <= audit["allowed_diff_lots"]
        )
        audit["sell_match"] = audit["code_coverage_match"] & (
            audit["sell_diff_lots"].abs() <= audit["allowed_diff_lots"]
        )
        audit["matched"] = audit["buy_match"] & audit["sell_match"]
        audit["abs_max_diff_lots"] = audit[["buy_diff_lots", "sell_diff_lots"]].abs().max(axis=1)
        audit = audit.sort_values(
            ["code_coverage_match", "matched", "_merge", "abs_max_diff_lots"],
            ascending=[True, True, True, False],
        ).reset_index(drop=True)

        official_codes = int(len(official))
        union_codes = int(len(audit))
        presence_codes = int(audit["code_coverage_match"].sum())
        matched_codes = int(audit["matched"].sum())
        coverage = presence_codes / union_codes if union_codes else 0.0
        matched_volume_coverage = matched_codes / union_codes if union_codes else 0.0

        official_total = float(official["official_volume_lots"].sum())
        fin_buy_total = float(fin["finmind_buy_lots"].sum())
        fin_sell_total = float(fin["finmind_sell_lots"].sum())
        total_allowed = max(
            FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_LOTS,
            abs(official_total) * FINMIND_WARRANT_CURRENT_DAY_TOTAL_VOLUME_TOLERANCE_PCT,
        )
        buy_total_diff = fin_buy_total - official_total
        sell_total_diff = fin_sell_total - official_total
        total_match = (
            abs(buy_total_diff) <= total_allowed
            and abs(sell_total_diff) <= total_allowed
        )
        verified = (
            official_codes >= FINMIND_WARRANT_CURRENT_DAY_MIN_OFFICIAL_CODES
            and coverage >= FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE
            and total_match
        )

        failed_reasons = []
        if official_codes < FINMIND_WARRANT_CURRENT_DAY_MIN_OFFICIAL_CODES:
            failed_reasons.append(
                f"官方代號數 {official_codes} < {FINMIND_WARRANT_CURRENT_DAY_MIN_OFFICIAL_CODES}"
            )
        if coverage < FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE:
            failed_reasons.append(
                f"代號覆蓋率 {coverage:.2%} < {FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE:.2%}"
            )
        if not total_match:
            failed_reasons.append(
                f"總量差超限：買{buy_total_diff:+,.0f}張／賣{sell_total_diff:+,.0f}張，容忍±{total_allowed:,.0f}張"
            )
        reason = (
            f"代號覆蓋率 {coverage:.2%}，買賣總量差均在 ±{total_allowed:,.0f} 張內"
            if verified
            else "；".join(failed_reasons) or "未通過官方成交量核對"
        )

        result.update({
            "verified": bool(verified),
            "reason": reason,
            "audit_df": audit,
            "official_codes": official_codes,
            "presence_codes": presence_codes,
            "matched_codes": matched_codes,
            "coverage": float(coverage),
            "matched_volume_coverage": float(matched_volume_coverage),
            "official_total_lots": official_total,
            "finmind_buy_total_lots": fin_buy_total,
            "finmind_sell_total_lots": fin_sell_total,
            "total_allowed_diff_lots": float(total_allowed),
            "status": "verified" if verified else "mismatch",
        })
        print(
            "🔬 FinMind 當日官方成交量核對："
            f"{code} {stock_name}｜日期={target_ts.date()}｜"
            f"代號存在={presence_codes}/{union_codes}（{coverage:.2%}；門檻{FINMIND_WARRANT_CURRENT_DAY_MIN_CODE_COVERAGE:.2%}）｜"
            f"逐檔量吻合={matched_codes}/{union_codes}（{matched_volume_coverage:.2%}；僅供診斷）｜"
            f"官方代號={official_codes}｜FinMind代號={len(fin)}｜"
            f"官方={official_total:,.0f}張｜FinMind買={fin_buy_total:,.0f}張（差{buy_total_diff:+,.0f}）｜"
            f"FinMind賣={fin_sell_total:,.0f}張（差{sell_total_diff:+,.0f}）｜容忍±{total_allowed:,.0f}張"
        )
        if not verified:
            bad = audit[(~audit["code_coverage_match"]) | (~audit["matched"])].head(30)
            if not bad.empty:
                print("⚠️ 當日完整性差異權證（前30筆）：")
                print(bad[[
                    "warrant_code", "official_name", "official_market", "official_source", "_merge",
                    "official_volume_lots", "finmind_buy_lots", "finmind_sell_lots",
                    "buy_diff_lots", "sell_diff_lots",
                ]].to_string(index=False))
        return result
    except Exception as exc:
        result["reason"] = f"官方成交量驗證例外：{exc}"
        result["status"] = "error"
        print(f"⚠️ FinMind 當日完整性驗證失敗：{exc}")
        return result

def _apply_finmind_current_day_safety_guard(
    events_df: pd.DataFrame,
    stock_code: str = "",
    stock_name: str = "",
    requested_end=None,
) -> pd.DataFrame:
    """以官方權證成交量驗證最新日 API；未通過時整批退回前一交易日。"""
    if events_df is None or events_df.empty or "Date" not in events_df.columns:
        return events_df.copy() if isinstance(events_df, pd.DataFrame) else pd.DataFrame()

    out = events_df.copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    out = out.dropna(subset=["Date"])
    if out.empty:
        return out
    out = out.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)

    if not FINMIND_WARRANT_CURRENT_DAY_GUARD_ENABLE:
        return out

    taipei_today = get_taipei_today_ts()
    latest_date = pd.Timestamp(out["Date"].max()).normalize()
    if latest_date != taipei_today:
        print(
            f"ℹ️ FinMind 最新日 API 尚未提供今日資料：今日={taipei_today.date()}｜"
            f"目前最新日={latest_date.date()}"
        )
        return out

    current_mask = out["Date"] == taipei_today
    current_rows = out.loc[current_mask].copy()
    kept = out.loc[~current_mask].copy()
    if current_rows.empty:
        return out

    audit = _finmind_current_day_official_volume_audit(
        out,
        stock_code=stock_code,
        stock_name=stock_name,
        target_date=taipei_today,
    )
    if audit.get("verified"):
        out.attrs["finmind_guard_status"] = "verified_keep"
        out.attrs["finmind_guard_verified_date"] = taipei_today.strftime("%Y-%m-%d")
        print(
            "✅ FinMind 最新日 API 完整性驗證通過：正式圖納入當日全市場權證分點｜"
            f"日期={taipei_today.date()}｜事件={len(current_rows):,}筆｜"
            f"原因={audit.get('reason', '')}"
        )
        return out

    if kept.empty:
        print(
            "⚠️ FinMind 最新日 API 完整性未通過，但沒有較早資料可退回；"
            f"保留當日並標記未驗證｜日期={taipei_today.date()}｜原因={audit.get('reason', '')}"
        )
        out.attrs["finmind_guard_status"] = "unverified_no_fallback"
        return out

    for col in ["buy_amount", "sell_amount", "net_amount"]:
        current_rows[col] = pd.to_numeric(current_rows.get(col, 0), errors="coerce").fillna(0.0)
    guard_status = str(audit.get("status", "") or "")
    guard_title = "官方相關市場資料尚未更新" if guard_status == "official_pending" else "官方成交量核對未通過"
    result = kept.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)
    print(
        f"🛡️ FinMind 最新日 API 完整性保護：{guard_title}，正式圖退回前一交易日｜"
        f"日期={taipei_today.date()}｜排除={len(current_rows):,}筆｜"
        f"買進={fmt_money(float(current_rows['buy_amount'].sum()))}｜"
        f"賣出={fmt_money(-float(current_rows['sell_amount'].sum()))}｜"
        f"淨額={fmt_money(float(current_rows['net_amount'].sum()))}｜"
        f"正式最新日={result['Date'].max().date()}｜原因={audit.get('reason', '')}"
    )
    result.attrs["finmind_guard_status"] = "excluded_incomplete_latest_api"
    result.attrs["finmind_guard_excluded_date"] = taipei_today.strftime("%Y-%m-%d")
    result.attrs["finmind_guard_excluded_rows"] = int(len(current_rows))
    return result


def generate_warrant_report(stock_code: str, next_stock_code: str = "") -> io.BytesIO:
    report_total_start = time.perf_counter()
    stock_code = str(stock_code).strip()
    selected_branch_snapshot = tuple(_get_selected_branch_flow_list())
    report_data_executor = None
    news_future = None
    report_cancel_event = threading.Event()
    prefetched_news_cancel_event = None

    try:
        print(
            f"🧭 本次產圖精選分點快照：模式={get_selected_branch_flow_mode_label()}｜"
            f"分點={'、'.join(selected_branch_snapshot) or '未設定'}"
        )
        if REPORT_LIVE_ONLY:
            print("🔴 本次啟用純 Live 週報模式：圖片內容不使用 Google Sheet / 本機快取資料")
        else:
            if ACTION_CACHE_ONLY_MODE:
                print(
                    "☁️ 本次啟用 Action=0 嚴格 Google Sheet 快取模式："
                    "只讀當日 Gemini 快取與完整權證快照；缺少或不完整時直接停止，不回退 Live"
                )
            else:
                print(
                    "☁️ 本次啟用 Google Sheet 快取優先模式："
                    "先讀當日 Gemini 快取與完整權證快照，缺少或不完整時才回退 Live 抓取"
                )

        with report_stage_timer(f"{stock_code}｜股票名稱查詢"):
            stock_name = get_tw_stock_name(stock_code)

        # 官方發行商對照不依賴本次權證事件，先在背景抓取／讀快取，避免後段阻塞。
        _finmind_start_official_warrant_issuer_prefetch()

        # 股票名稱取得後立刻啟動完整新聞管線，讓 RSS、Top-K 原文、Gemini 統整、
        # 新聞管線與後續股價、法人、FinMind 權證下載等待時間重疊。
        report_data_executor = ThreadPoolExecutor(max_workers=2)
        prefetched_news_future, prefetched_news_cancel_event = _take_report_news_prefetch(stock_code)
        if prefetched_news_future is not None:
            news_future = prefetched_news_future
            print(f"⚡ 接續前一檔建圖期間已啟動的新聞管線：{stock_code}")
        else:
            news_future = report_data_executor.submit(
                _prepare_report_news_pipeline,
                stock_code,
                stock_name,
                report_cancel_event,
            )

        with report_stage_timer(f"{stock_code}｜股價資料抓取"):
            stock_df, market, yf_code = fetch_stock_data_yf(stock_code, period="180d")

        if stock_df is None or stock_df.empty:
            print(f"❌ 股價資料不足：{stock_code}")
            report_cancel_event.set()
            if prefetched_news_cancel_event is not None:
                prefetched_news_cancel_event.set()
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
        # 權證分點資料的更新時間可能比股價資料更快。
        # 因此抓權證資料時，結束日不能只用股價最新日，否則盤後會漏掉今日分點買賣超。
        end_date = max(stock_end_date, taipei_today)

        print(
            f"🚀 產生 {stock_code} {stock_name} 權證資金流週報，"
            f"股價最新日 {stock_end_date.date()}｜權證資料區間 {start_date.date()} ~ {end_date.date()}"
        )

        # FinMind 權證流程與已提前啟動的完整新聞管線彼此沒有資料相依。
        warrant_future = report_data_executor.submit(
            fetch_warrant_events_full_market,
            stock_code,
            stock_name,
            start_date,
            end_date,
            report_cancel_event,
        )

        with report_stage_timer(f"{stock_code}｜權證完整流程"):
            warrant_events = warrant_future.result()
        warrant_events = _apply_finmind_current_day_safety_guard(
            warrant_events,
            stock_code=stock_code,
            stock_name=stock_name,
            requested_end=end_date,
        )
        try:
            news_pipeline_result = news_future.result()
        except Exception as e:
            print(f"⚠️ 背景新聞管線失敗，改以無新聞模式產圖：{e}")
            news_pipeline_result = {"news_items": [], "news_points": []}
        news_items = list(news_pipeline_result.get("news_items", []) or [])
        precomputed_news_points = list(news_pipeline_result.get("news_points", []) or [])
        _clear_report_news_prefetch(stock_code)
        report_data_executor.shutdown(wait=True)
        report_data_executor = None

        # 目前股票資料已齊全，下一段主要是統計與建圖；此時預抓下一檔新聞最能隱藏網路等待。
        if next_stock_code:
            _schedule_next_report_news_prefetch(next_stock_code)

        print(f"✅ 權證分點事件總筆數：{len(warrant_events):,}")
        selected_debug_cache = None
        if warrant_events is not None and not warrant_events.empty and "Date" in warrant_events.columns:
            latest_event_date = pd.to_datetime(warrant_events["Date"], errors="coerce").dropna().max()
            if pd.notna(latest_event_date):
                print(f"🔎 權證分點事件最新日期：{pd.Timestamp(latest_event_date).date()}")
            selected_debug = filter_selected_branch_flow_events(warrant_events)
            selected_debug_cache = selected_debug.copy() if selected_debug is not None else pd.DataFrame()
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


        weekly_ctx = None
        if warrant_events is not None and not warrant_events.empty:
            try:
                with report_stage_timer(f"{stock_code}｜build_weekly_context"):
                    weekly_ctx = build_weekly_context(stock_df, warrant_events, WEEK_TRADING_DAYS)
                if selected_debug_cache is not None:
                    weekly_ctx["_selected_branch_events_all_cache"] = selected_debug_cache.copy()
                weekly_ctx["_selected_branch_names_snapshot"] = list(selected_branch_snapshot)
                debug_week_events = weekly_ctx.get("week_events", pd.DataFrame())
                if debug_week_events is not None and not debug_week_events.empty:
                    debug_buy_top, debug_sell_top = _get_cached_top_branch_tables(
                        weekly_ctx,
                        "current_week",
                        debug_week_events,
                        topn=5,
                    )
                    print(
                        f"🔎 TOP5統計區間：{pd.Timestamp(weekly_ctx['week_start']).date()} ~ {pd.Timestamp(weekly_ctx['week_end']).date()}｜"
                        f"週事件 {len(debug_week_events):,} 筆｜買超TOP5 {len(debug_buy_top):,} 筆｜賣超TOP5 {len(debug_sell_top):,} 筆"
                    )
            except Exception as e:
                weekly_ctx = None
                print(f"⚠️ TOP5統計區間檢查失敗：{e}")

        with report_stage_timer(f"{stock_code}｜週報內容生成與建圖總流程"):
            fig = plot_weekly_report(
                stock_code,
                stock_name,
                stock_df,
                warrant_events,
                news_items,
                precomputed_news_points=precomputed_news_points,
                precomputed_ctx=weekly_ctx,
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
        report_cancel_event.set()
        if prefetched_news_cancel_event is not None and news_future is not None and not news_future.done():
            prefetched_news_cancel_event.set()
        if report_data_executor is not None:
            try:
                report_data_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                report_data_executor.shutdown(wait=False)
            except Exception:
                pass
        if REPORT_TIMING_ENABLE:
            print(f"⏱️ {stock_code or 'UNKNOWN'}｜週報總時間：{time.perf_counter() - report_total_start:.2f} 秒")


# ============================================================
# GitHub Actions 手動執行入口
# ============================================================

def _send_discord_file(webhook_url: str, file_path: str, content: str = ""):
    if not webhook_url or not file_path or not os.path.exists(file_path):
        return
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "image/png")}
            data = {}
            clean_content = str(content or "").strip()
            if clean_content:
                data["content"] = clean_content
            resp = requests.post(webhook_url, data=data, files=files, timeout=(8, 40))
            resp.raise_for_status()
        print(f"✅ Discord 測試頻道已送出：{file_path}")
    except Exception as e:
        print(f"⚠️ Discord 測試頻道送出失敗：{e}")


def main():
    print("=" * 100)
    print(f"🧩 FINMIND_BUILD_VERSION={FINMIND_BUILD_VERSION}")
    print(f"🧩 FINMIND_PERFORMANCE_PATCH={FINMIND_PERFORMANCE_PATCH}")
    print(f"🧩 EXECUTED_PYTHON_FILE={os.path.abspath(__file__)}")
    print(
        "🧩 ACTIVE_FEATURES="
        "official-issuer-refresh+unresolved-issuer-exclusion+coverage-total-current-day-check+"
        "latest-day-warrant-code-api+latest-day-empty-fallback+latest-day-probe-first+latest-day-selected-branch-backfill+parallel-six-source-news+progressive-finmind-news+raw-news-ttl-cache+top2-parallel-body+event-level-news-dedup+local-evidence-repair+local-number-unit-repair+rich-two-sentence-news+gemini-max-two-calls+"
        "discord-image-only+atomic-output+market-compact-prewarm+branch-perf-disk+"
        "single-context+direct-runtime-cache+parallel-history-latest+cooperative-cancel+next-stock-news-prefetch+shared-market-compact-write+issuer-background-once+issuer-unique-vectorized+calendar7+deadcode-cleanup"
    )
    print(
        f"🧩 FUNCTION_LINES：fetch_warrant_events_full_market="
        f"{fetch_warrant_events_full_market.__code__.co_firstlineno}｜"
        f"fetch_multi_source_news_articles={fetch_multi_source_news_articles.__code__.co_firstlineno}｜"
        f"filter_selected_branch_flow_events={filter_selected_branch_flow_events.__code__.co_firstlineno}"
    )
    print("=" * 100)
    output_dir = os.getenv("OUTPUT_DIR", "output").strip() or "output"
    os.makedirs(output_dir, exist_ok=True)

    if FINMIND_PREWARM_ONLY:
        print("🔥 WARRANT_PREWARM_ONLY=1：本次只建立全市場共用快取，不產圖、不呼叫 Gemini、不送 Discord")
        _finmind_prewarm_market_compact_cache()
        return

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
    print(
        f"📌 Google Sheet 權證快照：enable={GSHEET_WARRANT_CACHE_ENABLE}｜"
        f"force_refresh={WARRANT_CACHE_FORCE_REFRESH}"
    )
    print(
        f"📌 FinMind 本機快取目錄：raw={FINMIND_WARRANT_DAY_CACHE_DIR}｜"
        f"compact={FINMIND_MARKET_COMPACT_CACHE_DIR}"
    )

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "").strip()

    if len(stock_codes) >= FINMIND_MULTI_STOCK_PREFETCH_MIN_STOCKS:
        try:
            _finmind_prepare_multi_stock_warrant_events(stock_codes)
        except Exception as exc:
            # 預處理失敗時保留原本逐股票流程，不讓效率功能改變既有可用性。
            print(f"⚠️ 多股票權證聯集預處理失敗，退回逐股票處理：{exc}")

    ok_count = 0
    for stock_index, stock_code in enumerate(stock_codes):
        next_stock_code = stock_codes[stock_index + 1] if stock_index + 1 < len(stock_codes) else ""
        out_path = os.path.join(output_dir, f"{stock_code}_warrant_report.png")
        tmp_path = out_path + f".{os.getpid()}.tmp"
        for stale_path in [out_path, tmp_path]:
            try:
                if os.path.exists(stale_path):
                    os.remove(stale_path)
                    print(f"🧹 已刪除舊輸出：{stale_path}")
            except Exception as exc:
                print(f"⚠️ 舊輸出刪除失敗：{stale_path}｜{exc}")

        buf = generate_warrant_report(stock_code, next_stock_code=next_stock_code)
        if buf is None:
            print(f"❌ {stock_code} 報告產生失敗")
            continue
        payload = buf.getvalue()
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
        image_sha = hashlib.sha256(payload).hexdigest()[:16]
        ok_count += 1
        selected_branch_label = "、".join(_get_selected_branch_flow_list()) or "未設定"
        print(
            f"✅ 已原子輸出圖片：{out_path}｜sha256={image_sha}｜"
            f"精選分點={selected_branch_label}｜build={FINMIND_BUILD_VERSION}"
        )
        _send_discord_file(
            webhook_url,
            out_path,
        )

    _shutdown_report_news_prefetch()

    if ok_count <= 0:
        raise SystemExit("沒有任何報告成功產生")


if __name__ == "__main__":
    main()
