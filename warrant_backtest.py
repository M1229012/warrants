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
9. 快取_TOP15共識淨買超
10. 快取_TOP15部位明細
11. 快取_近7日權證分點共識TOP15（僅 RUN_MODE=2 全市場分點模式更新）
12. 快取_近10日分點買賣明細（僅 RUN_MODE=2 全市場分點模式更新）
13. 顏色說明

執行：python warrant_backtest.py
依賴：pip install requests pandas openpyxl
"""

import json, re, time, os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
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
RECENT_RANKING_DAYS = 62
SELL_DETAIL_DAYS = int(os.getenv("SELL_DETAIL_DAYS", "3"))
D_WINDOW_DAYS = 10

# 快取設定：
# 第一次執行沒有快取時會完整爬取並建立快取；
# 第二次之後會優先讀取快取，只針對最近有出現目標分點的候選組合補抓新資料。
USE_CACHE = os.getenv("USE_CACHE", "1").strip().lower() not in ("0", "false", "no")
FORCE_FULL_CACHE_REFRESH = os.getenv("FORCE_FULL_CACHE_REFRESH", "0").strip().lower() in ("1", "true", "yes")
CACHE_RECENT_SCAN_DAYS = int(os.getenv("CACHE_RECENT_SCAN_DAYS", "3"))
PRICE_WORKERS = int(os.getenv("PRICE_WORKERS", "80"))
PRESCAN_WORKERS = int(os.getenv("PRESCAN_WORKERS", "60"))
FIND_BROKER_WORKERS = int(os.getenv("FIND_BROKER_WORKERS", "40"))

# 加速模式：
# 1. 有候選組合快取時，仍會每天補掃全市場最近 CACHE_RECENT_SCAN_DAYS 天，
#    用來發現新權證 / 新候選組合；舊候選資料則優先使用快取，避免重抓完整歷史。
#    FAST_SKIP_RECENT_PRESCAN 僅保留為相容舊設定，不再作為每日主流程的跳過依據。
# 2. B / C / D 工作表的 D+ 欄位只使用標的股價格，預設不再額外抓群組事件中每一檔權證價格。
#    若未來需要群組事件權證明細價格，可設定 FETCH_GROUP_WARRANT_PRICES=1。
FAST_SKIP_RECENT_PRESCAN = os.getenv("FAST_SKIP_RECENT_PRESCAN", "0").strip().lower() not in ("0", "false", "no")
FETCH_GROUP_WARRANT_PRICES = os.getenv("FETCH_GROUP_WARRANT_PRICES", "0").strip().lower() in ("1", "true", "yes")

CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(OUTPUT_DIR, "warrant_cache"))
CACHE_ENCODING = "utf-8-sig"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

WARRANTS_CACHE_PATH   = os.path.join(CACHE_DIR, "warrants_cache.csv")
BROKER_MAP_CACHE_PATH = os.path.join(CACHE_DIR, "broker_map_cache.csv")
CANDIDATES_CACHE_PATH = os.path.join(CACHE_DIR, "candidates_cache.csv")
CANDIDATES_CACHE_ALL_PATH = CANDIDATES_CACHE_PATH
CANDIDATES_CACHE_SELECTED5_PATH = os.path.join(CACHE_DIR, "candidates_cache_selected5.csv")
HISTORY_CACHE_PATH    = os.path.join(CACHE_DIR, "broker_warrant_history_cache.csv")
PRICE_CACHE_PATH      = os.path.join(CACHE_DIR, "price_cache.csv")

# 價格快取同步加速：
# 價格快取筆數很大時，若每次都整張重寫 Google Sheet 會拖慢整體執行。
# 本機 CSV 仍會完整保存；Google Sheet 則在快取很大且本次只有部分代號更新時，改用增量 append。
PRICE_CACHE_GSHEET_INCREMENTAL_APPEND = os.getenv("PRICE_CACHE_GSHEET_INCREMENTAL_APPEND", "1").strip().lower() not in ("0", "false", "no")
PRICE_CACHE_FULL_SYNC_THRESHOLD_ROWS = int(os.getenv("PRICE_CACHE_FULL_SYNC_THRESHOLD_ROWS", "80000"))

# TOP15 圖片用固定資料集：
# 主程式在同一次 RUN 內，會把 TOP15 所需的所有部位明細、賣出扣減、價格快照與報酬率
# 一次整理成「快取_TOP15部位明細」與「快取_TOP15共識淨買超」。
# 圖片程式之後應只讀這兩張固定資料集，不要再從 A/B/C/D 或快取歷史即時計算。
TOP15_CACHE_ENABLED = os.getenv("TOP15_CACHE_ENABLED", os.getenv("TOP15_RETURN_CACHE_ENABLED", "1")).strip().lower() not in ("0", "false", "no")
TOP15_POSITION_DETAIL_SHEET = os.getenv("TOP15_POSITION_DETAIL_SHEET", "快取_TOP15部位明細")
TOP15_CONSENSUS_SHEET = os.getenv("TOP15_CONSENSUS_SHEET", "快取_TOP15共識淨買超")
TOP15_LOOKBACK_TRADING_DAYS = int(os.getenv("TOP15_LOOKBACK_TRADING_DAYS", os.getenv("TOP15_RETURN_LOOKBACK_TRADING_DAYS", os.getenv("LOOKBACK_TRADING_DAYS", "22"))))
TOP15_PRICE_LOOKBACK_DAYS = int(os.getenv("TOP15_PRICE_LOOKBACK_DAYS", os.getenv("TOP15_RETURN_PRICE_LOOKBACK_DAYS", "75")))
TOP15_PRICE_STALE_DAYS = int(os.getenv("TOP15_PRICE_STALE_DAYS", os.getenv("TOP15_RETURN_PRICE_STALE_DAYS", "10")))
TOP15_FAIL_ON_MISSING_PRICE = os.getenv("TOP15_FAIL_ON_MISSING_PRICE", "1").strip().lower() not in ("0", "false", "no")
# 若 TOP15 剩餘部位因多日未造市 / 無成交而缺少有效權證價格，
# 預設不讓 RUN 失敗，而是保留淨買超成本，但該筆部位不納入報酬率估算。
TOP15_EXCLUDE_MISSING_PRICE_FROM_RETURN = os.getenv("TOP15_EXCLUDE_MISSING_PRICE_FROM_RETURN", "1").strip().lower() not in ("0", "false", "no")
TOP15_TARGET_DATE = os.getenv("TOP15_TARGET_DATE", "").strip()

# 近 7 日「所有追蹤分點」權證共識買賣超 TOP15：
# 這張工作表只會在 RUN_MODE=2 完整分點清單模式建立 / 更新。
# RUN_MODE=1 精選分點模式不會建立這張 sheet，因此同步到 Google Sheet 時也不會動到既有工作表。
WARRANT_CONSENSUS_7D_ENABLED = os.getenv("WARRANT_CONSENSUS_7D_ENABLED", "1").strip().lower() not in ("0", "false", "no")
WARRANT_CONSENSUS_7D_SHEET = os.getenv("WARRANT_CONSENSUS_7D_SHEET", "快取_近7日權證分點共識TOP15")
WARRANT_CONSENSUS_7D_DAYS = int(os.getenv("WARRANT_CONSENSUS_7D_DAYS", "7"))
WARRANT_CONSENSUS_7D_TOP_N = int(os.getenv("WARRANT_CONSENSUS_7D_TOP_N", "15"))

# 近 10 日「單一分點 + 標的股」買賣明細快取：
# 這張工作表只會在 RUN_MODE=2 完整分點清單模式建立 / 更新。
# RUN_MODE=1 精選分點模式不會建立這張 sheet，因此同步到 Google Sheet 時也不會動到既有工作表。
# 不分類 A/B/C/D，只要 API5 / 快取_分點歷史有抓到資料，就依分點與標的股合併統計。
BROKER_10D_DETAIL_ENABLED = os.getenv("BROKER_10D_DETAIL_ENABLED", "1").strip().lower() not in ("0", "false", "no")
BROKER_10D_DETAIL_SHEET = os.getenv("BROKER_10D_DETAIL_SHEET", "快取_近10日分點買賣明細")
BROKER_10D_DETAIL_DAYS = int(os.getenv("BROKER_10D_DETAIL_DAYS", "10"))
BROKER_10D_PRICE_LOOKBACK_DAYS = int(os.getenv("BROKER_10D_PRICE_LOOKBACK_DAYS", "90"))
# 近10日明細價格補抓加速：先抓較短區間；完全沒有價格時才補抓完整區間。
BROKER_10D_PRICE_FAST_LOOKBACK_DAYS = int(os.getenv("BROKER_10D_PRICE_FAST_LOOKBACK_DAYS", "30"))
BROKER_10D_PRICE_STALE_DAYS = int(os.getenv("BROKER_10D_PRICE_STALE_DAYS", "10"))
# 預設不再為所有純賣超權證補抓最新價；賣超報酬優先使用 API5 歷史 FIFO 成本。
# 若賣超完全找不到歷史買進成本，仍會補抓最新價作為備援成本，避免報酬率空白。
BROKER_10D_FETCH_ALL_TRADED_WARRANT_PRICES = os.getenv("BROKER_10D_FETCH_ALL_TRADED_WARRANT_PRICES", "0").strip().lower() in ("1", "true", "yes")
BROKER_10D_FETCH_SELL_FALLBACK_PRICES = os.getenv("BROKER_10D_FETCH_SELL_FALLBACK_PRICES", "1").strip().lower() not in ("0", "false", "no")

# 執行模式：
# RUN_MODE=1：精選 5 分點模式。只追蹤 SELECTED_TARGET_LABELS，但會對這 5 間分點做全市場最近資料補掃，
#             讓今日買賣超明細盡量完整，例如元大南屯今日賣南亞科所有相關認購權證。
# RUN_MODE=2：完整清單模式。使用目前 TARGET_PATTERNS 內所有分點，維持原本完整分點清單邏輯。
RUN_MODE = int(os.getenv("RUN_MODE", os.getenv("BROKER_RUN_MODE", "1")) or "1")
SELECTED_TARGET_LABELS_DEFAULT = [
    "華南永昌台中",
    "元大南屯",
    "富邦敦南",
    "永豐金內湖",
    "永豐金竹北",
]
SELECTED_TARGET_LABELS_ENV = os.getenv("SELECTED_TARGET_LABELS", "").strip()
SELECTED_FULL_SCAN_DAYS = int(os.getenv("SELECTED_FULL_SCAN_DAYS", str(CACHE_RECENT_SCAN_DAYS)))
# RUN_MODE=1 / RUN_MODE=2 加速追蹤設定：
# 舊版 RUN_MODE=1 會建立「所有認購權證 × 精選分點」，再用 SELECTED_REFRESH_ALL_WARRANTS
# 每次全部重打 API5，速度會非常慢。新版改成：
# 1. 先用 API4 掃最近有目標分點活動的標的股。
# 2. 再展開成「該標的所有認購權證 × 追蹤分點」。
# 3. 若歷史快取已有該候選資料，就走增量判斷，不再每天重抓 250 日 API5。
# 下列兩個舊環境變數保留相容，但預設流程已改成「活動標的擴展 + 快取增量」。
SELECTED_FORCE_ALL_WARRANTS = os.getenv("SELECTED_FORCE_ALL_WARRANTS", "1").strip().lower() not in ("0", "false", "no")
SELECTED_REFRESH_ALL_WARRANTS = os.getenv("SELECTED_REFRESH_ALL_WARRANTS", "1").strip().lower() not in ("0", "false", "no")
EXPAND_ACTIVE_UNDERLYING_WARRANTS = os.getenv("EXPAND_ACTIVE_UNDERLYING_WARRANTS", "1").strip().lower() not in ("0", "false", "no")

# 全面增量更新設定：
# 只要快取已有該「權證代號 + 券商代號」，就不再無差別重抓。
# API4 近期直接掃到有活動的候選，只有在快取最後日期落後時才補抓。
CACHE_INCREMENTAL_UPDATE_ENABLED = os.getenv("CACHE_INCREMENTAL_UPDATE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
CACHE_INCREMENTAL_REFRESH_LAG_DAYS = int(os.getenv("CACHE_INCREMENTAL_REFRESH_LAG_DAYS", "0"))
HISTORY_GSHEET_INCREMENTAL_APPEND = os.getenv("HISTORY_GSHEET_INCREMENTAL_APPEND", "1").strip().lower() not in ("0", "false", "no")
HISTORY_CACHE_FULL_SYNC_THRESHOLD_ROWS = int(os.getenv("HISTORY_CACHE_FULL_SYNC_THRESHOLD_ROWS", "200000"))

# prescan_all() 會更新這個集合，主流程用它判斷哪些候選組合需要重新 api5_get。
# 注意：新版只把「API4 直接掃到近期有活動」的 key 放進來。
# 活動標的擴展出的 key 若已有快取，不再強制刷新，避免 RUN_MODE=2 候選爆量。
PRESCAN_REFRESH_KEYS = set()
# 這個集合是「本次 API4 直接候選 + 活動標的擴展候選」。
# 若某候選沒有歷史快取，只有在這個集合內才補抓；避免舊候選快取裡的全市場空候選全部打 API5。
PRESCAN_MISSING_FETCH_KEYS = set()

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
    "永豐金潮州":     r"永豐.*潮州",
    "華南永昌世貿":   r"華南.*世貿",
    "華南永昌台中":   r"華南.*台中",
    "華南永昌岡山":   r"華南.*岡山",
    "福邦":           r"^福邦",
    "第一金":         r"^第一金$",
    "第一金中壢":     r"第一金.*中壢",
    "第一金安和":     r"第一金.*安和",
    "群益東大":       r"群益.*東大",
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
    "永豐金潮州":     ("永豐金-潮州",     "9A9q"),
    "華南永昌世貿":   ("華南永昌-世貿",   "9334"),
    "華南永昌台中":   ("華南永昌-台中",   "9302"),
    "華南永昌岡山":   ("華南永昌-岡山",   "9324"),
    "福邦":           ("福邦",             "6480"),
    "第一金":         ("第一金",           "5380"),
    "第一金中壢":     ("第一金-中壢",      "538Y"),
    "第一金安和":     ("第一金-安和",      "538j"),
    "群益東大":       ("群益金鼎-東大",   "9135"),
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

FULL_TARGET_PATTERNS = dict(TARGET_PATTERNS)
FULL_FALLBACK = dict(FALLBACK)


def parse_selected_target_labels():
    if SELECTED_TARGET_LABELS_ENV:
        labels = [
            x.strip()
            for x in re.split(r"[,;；、\n\r\t]+", SELECTED_TARGET_LABELS_ENV)
            if x.strip()
        ]
    else:
        labels = list(SELECTED_TARGET_LABELS_DEFAULT)

    out = []
    for label in labels:
        if label not in out:
            out.append(label)

    return out


def configure_run_mode():
    """
    依 RUN_MODE 切換分點範圍與候選快取。

    RUN_MODE=1：
    - 只保留 SELECTED_TARGET_LABELS 指定的精選分點。
    - 候選快取使用 candidates_cache_selected5.csv，避免與完整清單模式混在一起。
    - 歷史分點快取仍共用 broker_warrant_history_cache.csv，因為 key 是權證代號 + 券商代號 + 日期，
      可讓精選模式抓到的新資料補強整體歷史資料。

    RUN_MODE=2：
    - 使用完整 TARGET_PATTERNS 分點清單。
    - 候選快取使用原本 candidates_cache.csv。
    """
    global RUN_MODE, TARGET_PATTERNS, FALLBACK, CANDIDATES_CACHE_PATH

    try:
        RUN_MODE = int(os.getenv("RUN_MODE", os.getenv("BROKER_RUN_MODE", str(RUN_MODE))) or "1")
    except Exception:
        RUN_MODE = 1

    if RUN_MODE not in (1, 2):
        print(f"  ⚠️ RUN_MODE={RUN_MODE} 不支援，改用 RUN_MODE=1 精選分點模式。")
        RUN_MODE = 1

    if RUN_MODE == 1:
        selected_labels = parse_selected_target_labels()
        missing = [label for label in selected_labels if label not in FULL_TARGET_PATTERNS]

        if missing:
            print(f"  ⚠️ SELECTED_TARGET_LABELS 中有不存在於 TARGET_PATTERNS 的分點，已略過：{missing}")

        active_labels = [label for label in selected_labels if label in FULL_TARGET_PATTERNS]

        if not active_labels:
            print("  ⚠️ 精選分點清單為空，改用預設 5 間分點。")
            active_labels = [
                label for label in SELECTED_TARGET_LABELS_DEFAULT
                if label in FULL_TARGET_PATTERNS
            ]

        TARGET_PATTERNS = {
            label: FULL_TARGET_PATTERNS[label]
            for label in active_labels
        }
        FALLBACK = {
            label: FULL_FALLBACK[label]
            for label in active_labels
            if label in FULL_FALLBACK
        }
        CANDIDATES_CACHE_PATH = CANDIDATES_CACHE_SELECTED5_PATH
        print("  ✅ RUN_MODE=1：精選分點全市場追蹤模式")
        print(f"  ✅ 精選分點：{', '.join(TARGET_PATTERNS.keys())}")
        print(f"  ✅ 候選快取：{CANDIDATES_CACHE_PATH}")
    else:
        TARGET_PATTERNS = dict(FULL_TARGET_PATTERNS)
        FALLBACK = dict(FULL_FALLBACK)
        CANDIDATES_CACHE_PATH = CANDIDATES_CACHE_ALL_PATH
        print("  ✅ RUN_MODE=2：完整分點清單模式")
        print(f"  ✅ 分點數：{len(TARGET_PATTERNS)}")
        print(f"  ✅ 候選快取：{CANDIDATES_CACHE_PATH}")


def filter_broker_map_for_active_targets(broker_map):
    if not broker_map:
        return {}

    active_labels = set(TARGET_PATTERNS.keys())
    return {
        label: value
        for label, value in broker_map.items()
        if label in active_labels
    }


def filter_candidates_by_broker_map(candidates, broker_map):
    if not candidates:
        return []

    if not broker_map:
        return []

    allowed_labels = set(broker_map.keys())
    allowed_codes = {str(code).strip() for _, code in broker_map.values()}

    out = []
    for c in candidates:
        try:
            label = str(c[4]).strip()
            broker_code = str(c[6]).strip()
        except Exception:
            continue

        if label in allowed_labels or broker_code in allowed_codes:
            out.append(c)

    return out


HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://pscnetsecrwd.moneydj.com/",
}

_THREAD_LOCAL = threading.local()


def get_thread_session():
    """
    每個執行緒各自建立並重用 requests.Session。

    目的：
    1. 避免 api4_get / api5_get 每次呼叫都重新建立連線。
    2. 在 ThreadPoolExecutor 多執行緒抓資料時，每個 thread 使用自己的 Session，
       避免多執行緒共用同一個 Session 造成不穩定。
    3. 不改變任何抓資料邏輯，只改善大量 API 請求時的連線重用效率。
    """
    session = getattr(_THREAD_LOCAL, "session", None)

    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=1)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _THREAD_LOCAL.session = session

    return session


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
        session = get_thread_session()
        r = session.get(API4.format(code=code, start=start, end=end), headers=HDR, timeout=(5, 12))
        data = json.loads(r.content.decode("utf-8"))
        rows = []
        for item in (data if isinstance(data, list) else [data]):
            rows.extend(item.get("ResultSet", {}).get("Result", []))
        return rows
    except:
        return []


def api5_get(warrant, broker):
    try:
        session = get_thread_session()
        r = session.get(API5.format(warrant=warrant, broker=broker), headers=HDR, timeout=(5, 12))
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
            session = get_thread_session()
            rp = session.get(
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
                    session = get_thread_session()
                    rp = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
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

            session = get_thread_session()
            rp = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
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
        session = get_thread_session()
        rp = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
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
            session = get_thread_session()
            rp = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
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
        session = get_thread_session()
        rp = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)

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
GSHEET_SYNC_CACHE_ON_READ = os.getenv("GSHEET_SYNC_CACHE_ON_READ", "0").strip().lower() in ("1", "true", "yes")

# Google Sheets API 有「每分鐘寫入請求」限制。
# 結果工作表很多、又要同步格式時，如果沒有節流與 429 重試，
# 會出現後面工作表建立 / 寫入失敗，甚至因先刪後建導致工作表消失。
GSHEET_WRITE_SLEEP_SECONDS = float(os.getenv("GSHEET_WRITE_SLEEP_SECONDS", "1.25"))
GSHEET_MAX_RETRIES = int(os.getenv("GSHEET_MAX_RETRIES", "6"))
GSHEET_RETRY_BASE_SECONDS = float(os.getenv("GSHEET_RETRY_BASE_SECONDS", "12"))

_GSHEET_CLIENT = None
_GSHEET_SPREADSHEET = None
_GSHEET_LAST_WRITE_TS = 0.0

CACHE_SHEET_NAME_MAP = {
    "warrants_cache.csv": "快取_權證清單",
    "broker_map_cache.csv": "快取_分點代號",
    "candidates_cache.csv": "快取_候選組合",
    "candidates_cache_selected5.csv": "快取_候選組合_精選5",
    "broker_warrant_history_cache.csv": "快取_分點歷史",
    "price_cache.csv": "快取_價格",
}


def gsheet_enabled():
    return bool(os.getenv("GCP_SERVICE_KEY", "").strip())


def is_gsheet_quota_error(exc):
    msg = str(exc)
    return (
        "429" in msg
        or "Quota exceeded" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "Write requests per minute" in msg
    )


def gsheet_write_sleep():
    """
    Google Sheets 寫入節流。

    這不是改資料邏輯，而是避免短時間連續建立 / 清除 / 寫入 / 套格式
    造成 429 quota exceeded。
    """
    global _GSHEET_LAST_WRITE_TS

    if GSHEET_WRITE_SLEEP_SECONDS <= 0:
        return

    now = time.time()
    elapsed = now - _GSHEET_LAST_WRITE_TS

    if elapsed < GSHEET_WRITE_SLEEP_SECONDS:
        time.sleep(GSHEET_WRITE_SLEEP_SECONDS - elapsed)

    _GSHEET_LAST_WRITE_TS = time.time()


def gsheet_api_call(description, func, *args, **kwargs):
    """
    所有 Google Sheet 寫入動作統一走這裡：
    1. 先節流
    2. 遇到 429 自動等待重試
    3. 不讓暫時 quota 造成後續工作表消失
    """
    last_error = None

    for attempt in range(1, GSHEET_MAX_RETRIES + 1):
        try:
            gsheet_write_sleep()
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e

            if not is_gsheet_quota_error(e):
                raise

            wait_seconds = min(90, GSHEET_RETRY_BASE_SECONDS * attempt)
            print(
                f"  ⚠️ Google Sheet 觸發寫入配額限制，{description} 第 {attempt}/{GSHEET_MAX_RETRIES} 次重試，"
                f"等待 {wait_seconds:.0f} 秒..."
            )
            time.sleep(wait_seconds)

    raise last_error


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
            return gsheet_api_call(
                f"建立工作表 {title}",
                sh.add_worksheet,
                title=title,
                rows=max(int(rows), 1),
                cols=max(int(cols), 1),
            )
        except Exception as e:
            print(f"  ⚠️ 建立工作表失敗：{title}，原因：{type(e).__name__}: {e}")
            return None


def get_or_recreate_result_worksheet(title, rows=100, cols=20):
    """
    取得結果工作表。

    重要修正：
    以前為了清掉舊格式，會先刪除再重建結果工作表。
    但 Google Sheets API 有每分鐘寫入限制，一旦刪除後重建遇到 429，
    會導致「券商查詢」、「ABCD組合勝率」等工作表直接消失。

    這版改成不刪除工作表：
    1. 已存在就沿用
    2. 不存在才建立
    3. 寫入前會清除舊格式與資料，再重新寫入與套樣式
    """
    sh = get_gsheet_spreadsheet()

    if sh is None:
        return None

    title = safe_worksheet_title(title)
    rows = max(int(rows), 1)
    cols = max(int(cols), 1)

    try:
        return sh.worksheet(title)
    except Exception:
        try:
            return gsheet_api_call(
                f"建立結果工作表 {title}",
                sh.add_worksheet,
                title=title,
                rows=rows,
                cols=cols,
            )
        except Exception as e:
            print(f"  ⚠️ 建立結果工作表失敗：{title}，原因：{type(e).__name__}: {e}")
            return None


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


def _strip_trailing_excel_decimal_for_code(s):
    s = str(s).strip()

    if s.endswith(".0"):
        head = s[:-2]
        if head.isdigit():
            return head

    return s


def normalize_gsheet_code_text_value(header, value):
    """
    Google Sheet 寫入 / 讀回時的代號欄位修正。

    目的：
    1. 權證代號若被 Google Sheet 轉成 5 碼，例如 30004，補回 030004。
    2. 權證清單內的每一個 5 碼權證代號也補回 6 碼。
    3. 股票代號 / 標的股 / 券商代號只維持文字，不任意補零，避免破壞原代號。
    4. 公式欄位不可處理，避免 =IFERROR(...) 被改成純文字。
    """
    if value is None:
        return ""

    s = str(value).strip()

    if s == "":
        return ""

    if s.startswith("="):
        return s

    had_prefix = s.startswith("'")
    if had_prefix:
        s = s[1:]

    header = str(header).strip()
    s = _strip_trailing_excel_decimal_for_code(s)

    if "權證清單" in header:
        # 權證清單常見格式：30004 海華國票...；60234 文華統一...
        # 若 Google Sheet 已經把 030004 轉成 30004，這裡將每段開頭的 5 碼權證補回 6 碼。
        def repl(match):
            token = match.group(0)
            return token.zfill(6) if token.isdigit() and len(token) == 5 else token

        s = re.sub(r"(?<!\d)\d{5}(?!\d)", repl, s)
        return ("'" + s) if had_prefix else s

    if "權證代號" in header or "權證代碼" in header:
        if s.isdigit() and len(s) == 5:
            s = s.zfill(6)
        return ("'" + s) if had_prefix else s

    # 快取_權證清單 的欄位名稱是「代號」，價格快取也可能是「代號」。
    # 只有 5 碼純數字才視為權證前導 0 被吃掉，補回 6 碼；4 碼股票不補。
    if header in ("代號", "證券代號", "證券代碼", "商品代號", "商品代碼"):
        if s.isdigit() and len(s) == 5:
            s = s.zfill(6)
        return ("'" + s) if had_prefix else s

    return ("'" + s) if had_prefix else s


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

                header = out[header_row_idx][col_idx] if col_idx < len(out[header_row_idx]) else ""
                cell_value = normalize_gsheet_code_text_value(header, cell_value)
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
    "成本",
    "市值",
    "損益",
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

GSHEET_DECIMAL_NUMBER_HEADER_KEYWORDS = (
    "均價",
    "收盤價",
    "最新權證價格",
    "權證價格",
    "股價",
    "報酬率",
    "報酬%",
    "獲利%",
    "勝率",
    "占比",
    "比例",
    "平均",
)

GSHEET_DECIMAL_NUMBER_EXCLUDE_KEYWORDS = (
    "日期",
    "代號",
    "名稱",
    "清單",
    "類型",
    "狀態",
    "文字",
    "說明",
    "來源",
)


def is_gsheet_percent_header(header):
    header = str(header).strip()

    if not header:
        return False

    if "文字" in header or "日期" in header or "代號" in header:
        return False

    return ("%" in header) or ("勝率" in header) or ("占比" in header) or ("比例" in header)


def is_gsheet_decimal_number_header(header):
    """
    判斷 Google Sheet 中需要數字格式但不一定要整數千分位的欄位。

    主要修正：
    1. 買進均價 / 減碼均價 / 出清均價不可被誤套日期格式。
    2. 減碼獲利% / 出清獲利% 不可被誤套日期格式。
    3. 報酬率、勝率、占比等欄位要保持數字或百分比顯示。
    """
    header = str(header).strip()

    if not header:
        return False

    if header.startswith("D+"):
        return False

    if is_gsheet_text_header(header):
        return False

    for keyword in GSHEET_DECIMAL_NUMBER_EXCLUDE_KEYWORDS:
        if keyword in header:
            return False

    return any(keyword in header for keyword in GSHEET_DECIMAL_NUMBER_HEADER_KEYWORDS)


def is_gsheet_numeric_format_header(header):
    return is_gsheet_comma_number_header(header) or is_gsheet_decimal_number_header(header)


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
            if is_gsheet_numeric_format_header(header):
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


def _gsheet_number_format_for_header(header):
    header = str(header).strip()

    # 重要：Excel / Google Sheet 用 USER_ENTERED 寫入「+7.00%」這種字串時，
    # 儲存格底層值會變成 0.07。這類欄位必須套用真正的 PERCENT 格式，
    # 才會顯示成 +7.00%，不能用 NUMBER + 文字百分號，否則會被顯示成 +0.07%。
    #
    # 但「報酬率」這種沒有 % 符號的 TOP15 快取欄位，程式內多為 70.30 這種百分點數值，
    # 所以不歸類為 PERCENT，仍以一般數字顯示，避免變成 7,030%。
    if is_gsheet_percent_header(header):
        # 勝率 / 占比 / 比例不是報酬率，不應該顯示 + 號。
        if "勝率" in header or "占比" in header or "比例" in header:
            return {
                "type": "PERCENT",
                "pattern": '0.00%',
            }

        return {
            "type": "PERCENT",
            "pattern": '+0.00%;-0.00%;0.00%',
        }

    if is_gsheet_decimal_number_header(header):
        return {
            "type": "NUMBER",
            "pattern": "#,##0.00",
        }

    return {
        "type": "NUMBER",
        "pattern": "#,##0",
    }


def _gsheet_number_pattern_for_header(header):
    # 保留舊函式名稱供既有呼叫相容；實際格式型別請用 _gsheet_number_format_for_header()。
    return _gsheet_number_format_for_header(header).get("pattern", "#,##0")


def _format_source_rows_from_values_or_xlsx(ws_xlsx, values=None):
    """
    Google Sheet 格式套用時的表頭來源。

    upsert 模式會在 Google Sheet 寫入前新增「資料範圍」欄位，
    因此不能再只看原本 Excel 的欄位位置，否則日期格式會往左/往右套錯，
    造成買進均價、買超張數、獲利% 被顯示成 1900 年代日期。
    """
    if values:
        rows = [list(row) for row in values]
        max_row = max(len(rows), 1)
        max_col = max(max((len(row) for row in rows), default=1), 1)
        return rows, max_row, max_col

    rows = []
    if ws_xlsx is not None:
        scan_limit = min(ws_xlsx.max_row, 10)
        for row_idx in range(1, scan_limit + 1):
            rows.append([ws_xlsx.cell(row_idx, col_idx).value for col_idx in range(1, ws_xlsx.max_column + 1)])
        return rows, ws_xlsx.max_row, ws_xlsx.max_column

    return [], 1, 1


def apply_comma_number_format_to_gsheet(ws_xlsx, gws, values=None):
    """
    將 Google Sheet 結果工作表的金額 / 股數 / 張數 / 成本 / 均價 / 獲利% 等欄位套用正確數字格式。

    重點修正：
    - upsert 後的實際 Google Sheet 表頭可能比原 Excel 多「資料範圍」欄。
    - 這裡改用實際寫入的 values 找欄位位置，避免格式位移。
    - 所有金額 / 成本 / 市值 / 損益使用千分位逗號顯示。
    - 均價、報酬率、獲利%、勝率等欄位也強制套數字/百分比格式，避免被日期格式污染。
    """
    if gws is None:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    source_rows, max_row, max_col = _format_source_rows_from_values_or_xlsx(ws_xlsx, values)
    header_rows = []
    scan_limit = min(len(source_rows), 10)

    for row_idx in range(1, scan_limit + 1):
        row = source_rows[row_idx - 1]
        number_cols = []

        for col_idx in range(1, max_col + 1):
            header = row[col_idx - 1] if col_idx - 1 < len(row) else ""

            if is_gsheet_numeric_format_header(header):
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
            end_row = max_row

        start_data_row = header_row_idx + 1

        if start_data_row > end_row:
            continue

        for col_idx, pattern in number_cols:
            header = ""
            try:
                row = source_rows[header_row_idx - 1]
                header = row[col_idx - 1] if col_idx - 1 < len(row) else ""
            except Exception:
                header = ""

            number_format = _gsheet_number_format_for_header(header)

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
                            "numberFormat": number_format
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


def apply_date_format_to_gsheet(ws_xlsx, gws, values=None):
    """
    將日期相關欄位套用 Google Sheets 日期格式 yyyy/mm/dd。

    upsert 模式會新增「資料範圍」欄位，因此日期欄位置必須以實際寫入 values 的表頭判斷，
    不可只看原本 Excel 欄位位置。這可避免買進均價 / 張數 / 獲利% 被誤套成日期格式。
    """
    if gws is None:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    source_rows, max_row, max_col = _format_source_rows_from_values_or_xlsx(ws_xlsx, values)
    header_rows = []
    scan_limit = min(len(source_rows), 10)

    for row_idx in range(1, scan_limit + 1):
        row = source_rows[row_idx - 1]
        date_cols = []

        for col_idx in range(1, max_col + 1):
            header = row[col_idx - 1] if col_idx - 1 < len(row) else ""

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
            end_row = max_row

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


def _gsheet_pixel_width_for_header(header):
    header = str(header).strip()

    if not header:
        return None

    if header == "資料範圍":
        return 105

    # 有權證資訊的欄位加寬，避免新增「資料範圍」後欄寬位移造成內容被擠在一起。
    if "權證清單" in header or "權證集合" in header:
        return 520
    if "分點明細_JSON" in header:
        return 700
    if "權證名稱" in header:
        return 190
    if "權證代號" in header or "權證代碼" in header:
        return 105
    if "權證檔數" in header:
        return 95

    if "標的名稱" in header or header == "股票名稱":
        return 140
    if "標的股" in header or "標的代號" in header:
        return 95
    if "分點名稱" in header or header == "分點":
        return 140
    if "券商代號" in header:
        return 95

    if "日期" in header or header in ("買進日", "事件日", "起始日", "結束日", "減碼日", "出清日"):
        return 105

    if any(k in header for k in ("金額", "成本", "市值", "損益")):
        return 125

    if any(k in header for k in ("均價", "報酬率", "獲利%", "勝率", "占比", "比例")):
        return 105

    if any(k in header for k in ("股數", "張數", "筆數", "天數", "排名")):
        return 85

    return None


def apply_header_widths_to_gsheet(gws, values=None):
    """
    依照實際 Google Sheet 表頭重新調整欄寬。

    因為 upsert 會新增「資料範圍」欄位，不能只沿用原 Excel 欄寬；
    否則有權證名稱 / 權證清單的欄位會被擠到太窄。
    """
    if gws is None or not values:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    header_row_idx = _find_simple_header_row(values)
    if header_row_idx is None or header_row_idx >= len(values):
        return

    headers = list(values[header_row_idx])
    requests = []

    for col_idx, header in enumerate(headers):
        pixel_size = _gsheet_pixel_width_for_header(header)
        if not pixel_size:
            continue

        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1,
                },
                "properties": {
                    "pixelSize": pixel_size,
                },
                "fields": "pixelSize",
            }
        })

    _gsheet_batch_update(requests)


def reset_worksheet_before_value_write(ws, row_count, col_count):
    """
    寫入值之前先清掉舊格式、舊資料驗證與舊合併範圍。

    這可以解決「券商查詢」公式被舊的純文字格式吃掉，
    同時避免因刪除 / 重建工作表造成 429 後工作表消失。
    """
    if ws is None:
        return

    try:
        sheet_id = int(ws.id)
    except Exception:
        return

    requests = [
        {
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                }
            }
        },
        {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(int(row_count), 1),
                    "startColumnIndex": 0,
                    "endColumnIndex": max(int(col_count), 1),
                },
                "fields": "userEnteredFormat,dataValidation",
            }
        },
    ]

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
        # 不再使用 delete/recreate。先 resize，再清格式，最後寫入值。
        # 這樣遇到 429 時不會把既有工作表刪掉。
        gsheet_api_call(
            f"調整工作表大小 {ws.title}",
            ws.resize,
            rows=max(row_count, 1),
            cols=max(col_count, 1),
        )

        reset_worksheet_before_value_write(ws, row_count, col_count)

        for start in range(0, len(normalized_values), GSHEET_CHUNK_ROWS):
            chunk = normalized_values[start:start + GSHEET_CHUNK_ROWS]
            start_row = start + 1
            cell_range = f"A{start_row}"
            gsheet_api_call(
                f"寫入工作表資料 {ws.title} A{start_row}",
                ws.update,
                values=chunk,
                range_name=cell_range,
                value_input_option="USER_ENTERED",
            )

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
                df[col] = df[col].map(lambda v, _col=col: normalize_gsheet_code_text_value(_col, v))

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


def _gsheet_batch_update(requests, chunk_size=1000):
    if not requests:
        return

    sh = get_gsheet_spreadsheet()

    if sh is None:
        return

    for start in range(0, len(requests), chunk_size):
        chunk = requests[start:start + chunk_size]
        try:
            gsheet_api_call(
                f"套用 Google Sheet 格式 batchUpdate {start + 1}-{start + len(chunk)}",
                sh.batch_update,
                {"requests": chunk},
            )
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



# ══════════════════════════════════════════════════════════════════════
# Google Sheet 結果同步：補充 / 更新模式
# ══════════════════════════════════════════════════════════════════════

GSHEET_RESULT_UPSERT_ENABLED = os.getenv("GSHEET_RESULT_UPSERT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
GSHEET_LEGACY_SCOPE_LABEL = os.getenv("GSHEET_LEGACY_SCOPE_LABEL", "未標記舊資料").strip() or "未標記舊資料"

GSHEET_RESULT_UPSERT_TITLES = {
    "A_單檔大買",
    "B_同標的單日合計",
    "C_同標的3日累積",
    "每日賣出明細",
    "近兩月買賣金額排行",
    "近兩月分點數排行",
    TOP15_POSITION_DETAIL_SHEET,
    TOP15_CONSENSUS_SHEET,
    WARRANT_CONSENSUS_7D_SHEET,
    BROKER_10D_DETAIL_SHEET,
}

GSHEET_RESULT_OVERWRITE_TITLES = {
    "券商查詢",
    "券商查詢資料",
    "價格抓取狀態",
    "顏色說明",
}


def get_result_data_scope():
    """
    Google Sheet 結果同步用的資料範圍標籤。

    目的：
    - RUN_MODE=1 跑精選五分點時，只更新「資料範圍=精選五分點」的資料。
    - RUN_MODE=2 跑完整分點時，只更新「資料範圍=全分點」的資料。
    - 同一張 Google Sheet 內不同資料範圍可以並存，避免五分點覆蓋全分點資料。
    """
    return "精選五分點" if RUN_MODE == 1 else "全分點"


def should_upsert_result_sheet(title):
    title = safe_worksheet_title(title)

    if not GSHEET_RESULT_UPSERT_ENABLED:
        return False

    if title in GSHEET_RESULT_OVERWRITE_TITLES:
        return False

    if title in GSHEET_RESULT_UPSERT_TITLES:
        return True

    if title.startswith("D_近") and "日累積淨買進" in title:
        return True

    return False


def read_existing_worksheet_values(title):
    try:
        sh = get_gsheet_spreadsheet()
        if sh is None:
            return []
        ws = sh.worksheet(safe_worksheet_title(title))
        values = ws.get_all_values()
        return values or []
    except Exception:
        return []


def _find_simple_header_row(values):
    if not values:
        return None

    scan_limit = min(len(values), 10)
    key_headers = {
        "統計日期", "事件類型", "日期", "排名", "權證代號", "標的股", "分點",
        "買進金額", "賣出金額", "淨買超成本", "排名類型",
    }

    for idx in range(scan_limit):
        row = [str(x).strip() for x in values[idx]]
        non_empty = [x for x in row if x]
        if not non_empty:
            continue
        if len(set(row) & key_headers) >= 2:
            return idx

    return 0 if values and any(str(x).strip() for x in values[0]) else None


def _values_to_records(values, header_row_idx=0, default_scope=None):
    if not values or header_row_idx is None or header_row_idx >= len(values):
        return [], []

    headers = [str(h).strip() for h in values[header_row_idx]]
    if not headers or all(h == "" for h in headers):
        return [], []

    # 去掉尾端完全空白表頭，避免 Google Sheet 舊資料多出空欄造成 key 錯亂。
    while headers and headers[-1] == "":
        headers.pop()

    records = []
    for raw_row in values[header_row_idx + 1:]:
        row = list(raw_row)
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[:len(headers)]

        if not any(str(v).strip() for v in row):
            continue

        rec = {h: row[i] if i < len(row) else "" for i, h in enumerate(headers)}
        if default_scope is not None:
            rec["資料範圍"] = str(rec.get("資料範圍", "") or default_scope).strip()
        records.append(rec)

    return headers, records


def _records_to_values(headers, records):
    out = [list(headers)]
    for rec in records:
        out.append([rec.get(h, "") for h in headers])
    return out


def _ensure_scope_header(headers):
    headers = [str(h).strip() for h in headers]
    if "資料範圍" not in headers:
        return ["資料範圍"] + headers
    return headers


def _sheet_upsert_key_columns(title, headers):
    title = safe_worksheet_title(title)
    hset = set(headers)

    def keep(cols):
        return [c for c in cols if c in hset]

    if title == TOP15_CONSENSUS_SHEET:
        return keep(["資料範圍", "統計日期", "標的股"])

    if title == TOP15_POSITION_DETAIL_SHEET:
        return keep(["資料範圍", "統計日期", "分點", "券商代號", "標的股", "權證代號", "事件", "事件日", "買進日"])

    if title == WARRANT_CONSENSUS_7D_SHEET:
        # 近7日共識已改為「標的層級」先合併再排名，key 不再包含單一權證代號。
        return keep(["資料範圍", "統計日期", "排名類型", "標的股"])

    if title == BROKER_10D_DETAIL_SHEET:
        return keep(["資料範圍", "統計日期", "分點", "券商代號", "標的股"])

    if title == "A_單檔大買":
        return keep(["資料範圍", "事件類型", "分點", "權證代號", "買進日"])

    if title == "B_同標的單日合計":
        return keep(["資料範圍", "事件類型", "分點", "標的股", "事件日", "權證清單"])

    if title == "C_同標的3日累積":
        return keep(["資料範圍", "事件類型", "分點", "標的股", "起始日", "結束日", "權證清單"])

    if title.startswith("D_近") and "日累積淨買進" in title:
        return keep(["資料範圍", "事件類型", "分點", "標的股", "起始日", "結束日", "權證清單"])

    if title == "每日賣出明細":
        return keep(["資料範圍", "日期", "分點", "券商代號", "標的股", "權證代號", "事件", "狀態", "事件日"])

    if title == "近兩月買賣金額排行":
        return keep(["資料範圍", "權證代號", "買進分點"])

    if title == "近兩月分點數排行":
        return keep(["資料範圍", "標的股", "買進分點清單"])

    # 通用備援：至少要有資料範圍，加上一些穩定欄位。
    cols = keep([
        "資料範圍", "統計日期", "日期", "事件類型", "分點", "券商代號",
        "標的股", "權證代號", "買進日", "事件日", "起始日", "結束日",
        "排名類型",
    ])
    return cols if len(cols) >= 2 else []



def normalize_underlying_code_for_group(value, fallback_text=""):
    """
    近7日共識與 Google Sheet upsert 用的標的股代號正規化。

    修正目的：
    同一標的可能因 Google Sheet / pandas 讀寫變成 2408、'2408、2408.0，
    若直接拿原字串當 group key，就會造成本週 TOP15 同標的重複出現。
    這裡統一轉成穩定代號後再合併與排序。
    """
    candidates = []
    for raw in (value, fallback_text):
        if raw is None:
            continue
        s = strip_gsheet_text_prefix(str(raw)).strip()
        if not s:
            continue
        candidates.append(s)

    for s in candidates:
        s = s.replace("，", ",").replace(",", "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]

        m = re.match(r"^(\d{4,6})(?:\s|$)", s)
        if m:
            return m.group(1)

        if s.isdigit() and 4 <= len(s) <= 6:
            return s

        digits = "".join(ch for ch in s if ch.isdigit())
        if 4 <= len(digits) <= 6:
            return digits

    return ""

def _record_key(rec, key_cols):
    parts = []
    for col in key_cols:
        value = rec.get(col, "")
        if col in ("日期", "統計日期", "買進日", "事件日", "起始日", "結束日", "第一筆日期", "最後筆日期"):
            value = normalize_date_str(strip_gsheet_text_prefix(value))
        elif col in ("標的股", "標的代號", "標的代碼"):
            value = normalize_underlying_code_for_group(value)
        else:
            value = strip_gsheet_text_prefix(value)
        parts.append(str(value).strip())
    return tuple(parts)


def merge_result_values_for_gsheet(title, new_values):
    """
    將本次 Excel 結果與 Google Sheet 既有結果做 upsert 合併。

    規則：
    - 本次資料會加上「資料範圍」：精選五分點 / 全分點。
    - 舊資料若沒有「資料範圍」，會保留為「未標記舊資料」，不會被本次資料誤覆蓋。
    - key 重複時，以本次新資料為準。
    - key 不重複時，舊資料保留在工作表中。
    """
    if not should_upsert_result_sheet(title):
        return new_values

    header_row_idx = _find_simple_header_row(new_values)
    if header_row_idx is None:
        return new_values

    if header_row_idx != 0:
        # 目前只對第一列就是表頭的資料表啟用 upsert。
        # 多段式報表例如勝率統計、ABCD組合勝率，仍維持原本覆蓋模式，避免破壞版面與公式。
        return new_values

    current_scope = get_result_data_scope()
    new_headers, new_records = _values_to_records(new_values, header_row_idx=0, default_scope=current_scope)
    if not new_headers:
        return new_values

    new_headers = _ensure_scope_header(new_headers)
    for rec in new_records:
        rec["資料範圍"] = current_scope

    old_values = read_existing_worksheet_values(title)
    old_headers, old_records = _values_to_records(old_values, header_row_idx=0, default_scope=GSHEET_LEGACY_SCOPE_LABEL)
    old_headers = _ensure_scope_header(old_headers) if old_headers else []

    headers = []
    for h in new_headers + old_headers:
        h = str(h).strip()
        if h and h not in headers:
            headers.append(h)

    if not headers:
        return new_values

    key_cols = _sheet_upsert_key_columns(title, headers)
    if not key_cols or "資料範圍" not in key_cols:
        return new_values

    old_map = {}
    old_order = []
    for rec in old_records:
        if not rec.get("資料範圍"):
            rec["資料範圍"] = GSHEET_LEGACY_SCOPE_LABEL
        key = _record_key(rec, key_cols)
        if not any(key):
            continue
        if key not in old_map:
            old_order.append(key)
        old_map[key] = rec

    new_map = {}
    new_order = []
    for rec in new_records:
        rec["資料範圍"] = current_scope
        key = _record_key(rec, key_cols)
        if not any(key):
            continue
        if key not in new_map:
            new_order.append(key)
        new_map[key] = rec

    merged_records = []
    used_keys = set()

    # 本次資料排在最前面，畫面上可以優先看到最新結果。
    for key in new_order:
        merged_records.append(new_map[key])
        used_keys.add(key)

    # 舊資料中沒有被本次 key 覆蓋的保留下來，避免五分點覆蓋全分點。
    for key in old_order:
        if key in used_keys:
            continue
        merged_records.append(old_map[key])
        used_keys.add(key)

    merged_values = _records_to_values(headers, merged_records)

    print(
        f"  ☁️ Google Sheet upsert：{safe_worksheet_title(title)}｜"
        f"資料範圍={current_scope}｜本次 {len(new_records):,} 筆｜舊資料 {len(old_records):,} 筆｜合併後 {len(merged_records):,} 筆"
    )

    return merged_values




def apply_safe_result_table_style_to_gsheet(gws, values=None):
    """
    針對 upsert 結果表套用安全版基本樣式。

    為什麼不用 apply_excel_style_to_gsheet：
    upsert 會在最左邊新增「資料範圍」欄位，而且會把舊資料與本次資料合併。
    如果繼續照原 Excel 欄位位置套樣式，日期格式會套到錯欄，
    例如「買進均價」會被顯示成 1899/12/30。

    這個函式只根據實際寫入 Google Sheet 的 values 表頭套安全樣式：
    - 凍結表頭列
    - 表頭粗體、淡黃色底、置中
    - 資料列垂直置中、換行
    - 不套任何日期或數字格式；日期 / 數字格式由後面的專用函式處理
    """
    if gws is None or not values:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    header_row_idx = _find_simple_header_row(values)
    if header_row_idx is None:
        return

    max_cols = max(max((len(row) for row in values), default=1), 1)
    max_rows = max(len(values), 1)

    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": header_row_idx + 1,
                    },
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_row_idx,
                    "endRowIndex": header_row_idx + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": max_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.949, "blue": 0.8},
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold,userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
            }
        },
    ]

    if max_rows > header_row_idx + 1:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_row_idx + 1,
                    "endRowIndex": max_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": max_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                    }
                },
                "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
            }
        })
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": header_row_idx + 1,
                    "endIndex": max_rows,
                },
                "properties": {
                    "pixelSize": 30,
                },
                "fields": "pixelSize",
            }
        })

    _gsheet_batch_update(requests)


def clear_all_number_formats_for_written_range(gws, values=None):
    """
    寫入結果後，先把實際資料範圍內的 numberFormat 全部清掉。

    這是為了處理舊版已經把「買進均價 / 減碼均價 / 出清獲利%」誤套成日期格式的工作表。
    單純寫入值不一定會移除舊格式，因此必須先清 numberFormat，再由日期 / 數字專用函式重套。
    """
    if gws is None or not values:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    max_rows = max(len(values), 1)
    max_cols = max(max((len(row) for row in values), default=1), 1)

    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": max_rows,
                "startColumnIndex": 0,
                "endColumnIndex": max_cols,
            },
            "cell": {
                "userEnteredFormat": {}
            },
            "fields": "userEnteredFormat.numberFormat",
        }
    }]

    # 這裡只清 numberFormat，不動其他樣式。
    # 後續 TEXT / DATE / NUMBER 專用函式會依實際表頭重新套正確格式。
    _gsheet_batch_update(requests)



def _openpyxl_border_to_gsheet(cell):
    """
    只把 Excel 裡真正有設定的外框轉到 Google Sheet。

    用途：保留「減碼獲利% / 出清獲利%」的粗紅 / 粗綠外框。
    一般沒有外框的儲存格不處理，避免新增資料範圍欄位後破壞既有版面。
    """
    try:
        border = cell.border
    except Exception:
        return None

    if border is None:
        return None

    def side_to_gsheet(side):
        try:
            style = str(side.style or "").strip()
        except Exception:
            style = ""

        if not style:
            return None

        style_map = {
            "hair": "DOTTED",
            "dotted": "DOTTED",
            "dashDot": "DASHED",
            "dashDotDot": "DASHED",
            "dashed": "DASHED",
            "mediumDashDot": "DASHED",
            "mediumDashDotDot": "DASHED",
            "mediumDashed": "DASHED",
            "thin": "SOLID",
            "medium": "SOLID_MEDIUM",
            "thick": "SOLID_THICK",
            "double": "DOUBLE",
        }

        out = {"style": style_map.get(style, "SOLID")}
        color = _openpyxl_color_to_gsheet(getattr(side, "color", None))
        if color:
            out["color"] = color
        return out

    out = {}
    for attr, gname in [
        ("top", "top"),
        ("bottom", "bottom"),
        ("left", "left"),
        ("right", "right"),
    ]:
        side_fmt = side_to_gsheet(getattr(border, attr, None))
        if side_fmt:
            out[gname] = side_fmt

    return out or None


def _cell_gsheet_visual_format(cell):
    """
    只同步視覺樣式，不同步 numberFormat。

    目的：
    1. 保留 A/B/C/D 原本 D+1～D+20 的紅綠藍橘配色。
    2. 保留獲利欄位的粗紅 / 粗綠外框。
    3. 不把 Excel 舊欄位位置的日期格式帶進 Google Sheet，避免「買進均價」再次變成 1899/12/30。
    """
    fmt = {}

    bg = _openpyxl_fill_to_gsheet(cell)
    if bg:
        fmt["backgroundColor"] = bg

    text_format = _openpyxl_font_to_gsheet(cell)
    if text_format:
        fmt["textFormat"] = text_format

    align_format = _openpyxl_alignment_to_gsheet(cell)
    fmt.update(align_format)

    borders = _openpyxl_border_to_gsheet(cell)
    if borders:
        fmt["borders"] = borders

    return fmt


def _format_fields_visual(fmt):
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
    if "borders" in fmt:
        fields.append("userEnteredFormat.borders")

    return ",".join(fields)


def _source_record_key_to_excel_row_map(title, source_values, final_headers):
    """
    建立 upsert 後資料列 key -> 原 Excel row number 對照。

    upsert 會把「資料範圍」插到最左邊，但原本 Excel 沒有這欄。
    這個 mapping 用 key 找回原 Excel 列，讓 Google Sheet 可以照原 Excel 的狀態色套回去，
    而不是用錯位的欄位位置硬套樣式。
    """
    header_row_idx = _find_simple_header_row(source_values)
    if header_row_idx is None:
        return {}, []

    source_headers = [str(h).strip() for h in source_values[header_row_idx]]
    while source_headers and source_headers[-1] == "":
        source_headers.pop()

    key_cols = _sheet_upsert_key_columns(title, final_headers)
    if not key_cols:
        return {}, source_headers

    current_scope = get_result_data_scope()
    out = {}

    for offset, raw_row in enumerate(source_values[header_row_idx + 1:], start=header_row_idx + 2):
        row = list(raw_row)
        if len(row) < len(source_headers):
            row = row + [""] * (len(source_headers) - len(row))
        elif len(row) > len(source_headers):
            row = row[:len(source_headers)]

        if not any(str(v).strip() for v in row):
            continue

        rec = {h: row[i] if i < len(row) else "" for i, h in enumerate(source_headers)}
        rec["資料範圍"] = str(rec.get("資料範圍", "") or current_scope).strip()
        key = _record_key(rec, key_cols)
        if any(key):
            out[key] = offset

    return out, source_headers


def _dplus_status_format_from_text(value):
    """
    依 D+ 欄位文字補回基本紅綠色。

    這只是保護機制：
    - 本次新資料會優先從原 Excel 樣式精準套回紅 / 綠 / 藍 / 橘。
    - 舊資料沒有原 Excel 樣式可參照時，至少依文字中的正負報酬補回紅綠底色。
    """
    s = str(value or "").strip()
    if not s:
        return None

    nums = []
    for m in re.finditer(r"([+-]?\d+(?:\.\d+)?)%", s):
        try:
            nums.append(float(m.group(1)))
        except Exception:
            pass

    if not nums:
        return None

    # 只要有正報酬，就以台股習慣用紅色；否則用綠色。
    fill = RED if any(v > 0 for v in nums) else GREEN
    color = _openpyxl_color_to_gsheet(fill.fgColor)
    if not color:
        return None

    return {
        "backgroundColor": color,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}},
    }


def apply_upsert_original_excel_visual_style_to_gsheet(ws_xlsx, gws, source_values=None, final_values=None, title=""):
    """
    upsert 工作表專用：在不套錯日期 / 數字格式的前提下，把原本 Excel 的配色套回 Google Sheet。

    修正重點：
    - 不再因為新增「資料範圍」欄位而放棄原本配色。
    - 透過表頭名稱與 upsert key 對齊原 Excel 列，避免欄位位移。
    - 只套背景色、字型、對齊與外框；numberFormat 仍交給後面的日期 / 數字專用函式處理。
    """
    if gws is None or ws_xlsx is None or not source_values or not final_values:
        return

    try:
        sheet_id = int(gws.id)
    except Exception:
        return

    title = safe_worksheet_title(title)
    final_header_row_idx = _find_simple_header_row(final_values)
    if final_header_row_idx is None:
        return

    final_headers = [str(h).strip() for h in final_values[final_header_row_idx]]
    while final_headers and final_headers[-1] == "":
        final_headers.pop()

    source_key_to_row, source_headers = _source_record_key_to_excel_row_map(title, source_values, final_headers)
    if not final_headers:
        return

    source_col_by_header = {h: idx + 1 for idx, h in enumerate(source_headers) if h}
    key_cols = _sheet_upsert_key_columns(title, final_headers)
    if not key_cols:
        return

    current_scope = get_result_data_scope()
    requests = []

    # 先處理表頭：如果原 Excel 表頭有黃底等樣式，依欄名套回；資料範圍欄則維持安全樣式。
    for final_col_idx, header in enumerate(final_headers, start=1):
        source_col_idx = source_col_by_header.get(header)
        if not source_col_idx:
            continue

        src_cell = ws_xlsx.cell(1, source_col_idx)
        fmt = _cell_gsheet_visual_format(src_cell)
        fields = _format_fields_visual(fmt)
        if fmt and fields:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": final_header_row_idx,
                        "endRowIndex": final_header_row_idx + 1,
                        "startColumnIndex": final_col_idx - 1,
                        "endColumnIndex": final_col_idx,
                    },
                    "cell": {"userEnteredFormat": fmt},
                    "fields": fields,
                }
            })

    final_records = []
    for raw_row in final_values[final_header_row_idx + 1:]:
        row = list(raw_row)
        if len(row) < len(final_headers):
            row = row + [""] * (len(final_headers) - len(row))
        elif len(row) > len(final_headers):
            row = row[:len(final_headers)]

        rec = {h: row[i] if i < len(row) else "" for i, h in enumerate(final_headers)}
        final_records.append((row, rec))

    import json as _json

    for row_offset, (row_values, rec) in enumerate(final_records, start=final_header_row_idx + 2):
        rec_scope = str(strip_gsheet_text_prefix(rec.get("資料範圍", ""))).strip()
        key = _record_key(rec, key_cols)
        src_excel_row = source_key_to_row.get(key)

        run_start = None
        run_fmt = None
        run_key = None

        for final_col_idx in range(1, len(final_headers) + 2):
            fmt = None
            if final_col_idx <= len(final_headers):
                header = final_headers[final_col_idx - 1]
                source_col_idx = source_col_by_header.get(header)

                if src_excel_row and source_col_idx and rec_scope == current_scope:
                    src_cell = ws_xlsx.cell(src_excel_row, source_col_idx)
                    fmt = _cell_gsheet_visual_format(src_cell)
                elif str(header).startswith("D+"):
                    # 舊資料或沒有對到原 Excel key 的資料，至少補回 D+ 欄位紅綠底色。
                    cell_value = row_values[final_col_idx - 1] if final_col_idx - 1 < len(row_values) else ""
                    fmt = _dplus_status_format_from_text(cell_value)

            key_json = _json.dumps(fmt, sort_keys=True, ensure_ascii=False) if fmt else None

            if key_json and run_key is None:
                run_start = final_col_idx
                run_fmt = fmt
                run_key = key_json
            elif key_json and key_json == run_key:
                continue
            else:
                if run_key and run_start is not None and run_fmt:
                    fields = _format_fields_visual(run_fmt)
                    if fields:
                        requests.append({
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": row_offset - 1,
                                    "endRowIndex": row_offset,
                                    "startColumnIndex": run_start - 1,
                                    "endColumnIndex": final_col_idx - 1,
                                },
                                "cell": {"userEnteredFormat": run_fmt},
                                "fields": fields,
                            }
                        })

                if key_json:
                    run_start = final_col_idx
                    run_fmt = fmt
                    run_key = key_json
                else:
                    run_start = None
                    run_fmt = None
                    run_key = None

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

            source_values = [list(row) for row in values]
            values = merge_result_values_for_gsheet(title, values)
            values = normalize_result_values_for_comma_numbers(values)

            max_cols = max(max((len(row) for row in values), default=1), 1)
            gws = get_or_recreate_result_worksheet(title, rows=max(len(values), 100), cols=max(max_cols, 20))

            if write_values_to_worksheet(gws, values):
                if should_upsert_result_sheet(title):
                    # upsert 表會新增「資料範圍」欄，不能再照原 Excel 欄位位置硬套 numberFormat。
                    # 但原本 A/B/C/D 的 D+ 紅綠藍橘配色必須保留，所以改成：
                    # 1. 先清掉舊錯誤 numberFormat
                    # 2. 套基本表格樣式
                    # 3. 依 upsert key 與表頭名稱，把原 Excel 視覺配色精準套回來
                    # 4. 最後再依實際 Google Sheet 表頭重套文字 / 數字 / 日期格式
                    clear_all_number_formats_for_written_range(gws, values=values)
                    apply_safe_result_table_style_to_gsheet(gws, values=values)
                    apply_upsert_original_excel_visual_style_to_gsheet(
                        ws_xlsx,
                        gws,
                        source_values=source_values,
                        final_values=values,
                        title=title,
                    )
                    apply_text_format_to_gsheet(gws, values)
                    apply_comma_number_format_to_gsheet(ws_xlsx, gws, values=values)
                    apply_date_format_to_gsheet(ws_xlsx, gws, values=values)
                    apply_header_widths_to_gsheet(gws, values=values)
                else:
                    apply_excel_style_to_gsheet(ws_xlsx, gws)
                    apply_comma_number_format_to_gsheet(ws_xlsx, gws, values=values)
                    apply_date_format_to_gsheet(ws_xlsx, gws, values=values)
                    apply_header_widths_to_gsheet(gws, values=values)
                print(f"  ☁️ 已同步結果到 Google Sheet：{title}")

        try:
            sh = get_gsheet_spreadsheet()
            if sh is not None:
                tmp_ws = sh.worksheet("__tmp_delete_guard__")
                if len(sh.worksheets()) > 1:
                    gsheet_api_call("刪除暫時工作表 __tmp_delete_guard__", sh.del_worksheet, tmp_ws)
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
            if GSHEET_SYNC_CACHE_ON_READ:
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
        if GSHEET_SYNC_CACHE_ON_READ:
            write_cache_to_gsheet(df, path)
        return df
    except Exception as e:
        print(f"  ⚠️ 快取讀取失敗：{path}，原因：{e}")
        return pd.DataFrame()


def write_cache_csv(df, path, sync_gsheet=True):
    if not USE_CACHE:
        return

    cache_changed = True

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_csv_text = df.to_csv(index=False)
        new_csv_bytes = new_csv_text.encode(CACHE_ENCODING)

        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    old_csv_bytes = f.read()

                if old_csv_bytes == new_csv_bytes:
                    cache_changed = False
            except Exception:
                cache_changed = True

        if cache_changed:
            with open(path, "wb") as f:
                f.write(new_csv_bytes)
        else:
            print(f"  ✅ 快取內容未變動，略過寫入與 Google Sheet 同步：{path}")
            return
    except Exception as e:
        print(f"  ⚠️ 快取寫入失敗：{path}，原因：{e}")
        return

    if sync_gsheet:
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

    for row in df.itertuples(index=False):
        row = row._asdict()
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


def append_price_cache_rows_to_gsheet(canonical_price_cache, changed_codes):
    """
    將本次新增 / 更新的價格資料增量 append 到 Google Sheet「快取_價格」。

    本機 price_cache.csv 仍會完整保存，因此不會遺失舊價格資料；
    Google Sheet 端改用 append 可避免每次把十幾萬筆價格快取整張重寫。
    讀回時 load_price_cache() 仍會依「代號 + 日期」覆蓋同日價格，因此少量重複列不會影響計算正確性。
    """
    if not PRICE_CACHE_GSHEET_INCREMENTAL_APPEND:
        return False

    if not GSHEET_CACHE_ENABLED or not gsheet_enabled():
        return False

    if not canonical_price_cache or not changed_codes:
        return False

    changed_codes = {
        normalize_price_code(code)
        for code in changed_codes
        if normalize_price_code(code)
    }

    if not changed_codes:
        return False

    rows = []
    for code in sorted(changed_codes):
        prices = canonical_price_cache.get(code, {})
        if not prices:
            continue

        for date_str in sorted(prices.keys()):
            dt = parse_date(date_str)
            price = safe_price_float(prices.get(date_str))

            if not dt or price is None:
                continue

            rows.append({
                "代號": code,
                "日期": dt.strftime("%Y/%m/%d"),
                "收盤價": price,
            })

    if not rows:
        return False

    try:
        title = cache_sheet_name_from_path(PRICE_CACHE_PATH)
        headers = ["代號", "日期", "收盤價"]
        ws = get_or_create_worksheet(title, rows=max(len(rows) + 1, 100), cols=len(headers))
        if ws is None:
            return False

        try:
            existing_headers = ws.row_values(1)
        except Exception:
            existing_headers = []

        if not existing_headers or all(str(x).strip() == "" for x in existing_headers):
            header_values = normalize_gsheet_values_for_text_columns([headers])
            gsheet_api_call(
                f"寫入價格快取表頭 {title}",
                ws.update,
                values=header_values,
                range_name="A1",
                value_input_option="USER_ENTERED",
            )

        values = [[row.get(h, "") for h in headers] for row in rows]
        normalized = normalize_gsheet_values_for_text_columns([headers] + values)[1:]

        for start in range(0, len(normalized), GSHEET_CHUNK_ROWS):
            chunk = normalized[start:start + GSHEET_CHUNK_ROWS]
            gsheet_api_call(
                f"增量追加價格快取 {title} {start + 1}-{start + len(chunk)}",
                ws.append_rows,
                chunk,
                value_input_option="USER_ENTERED",
            )

        print(f"  ☁️ 已增量追加價格快取到 Google Sheet：{title}，本次 {len(rows):,} 筆")
        return True
    except Exception as e:
        print(f"  ⚠️ 價格快取增量追加到 Google Sheet 失敗：{type(e).__name__}: {e}")
        return False


def save_price_cache(price_cache, changed_codes=None):
    """
    寫入價格持久化快取。

    寫入前會再做一次代號正規化，避免 064390 / 64390 之類別名重複存檔。

    加速修正：
    - 本機 CSV 一律完整保存，確保舊價格資料不遺失。
    - Google Sheet「快取_價格」在資料量很大且本次只更新部分代號時，改用增量 append，
      避免每次重新寫入整張價格快取造成執行時間大幅增加。
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

    normalized_changed_codes = {
        normalize_price_code(code)
        for code in (changed_codes or [])
        if normalize_price_code(code)
    }

    use_incremental_gsheet = (
        PRICE_CACHE_GSHEET_INCREMENTAL_APPEND
        and bool(normalized_changed_codes)
        and len(df) >= PRICE_CACHE_FULL_SYNC_THRESHOLD_ROWS
    )

    if use_incremental_gsheet:
        write_cache_csv(df, PRICE_CACHE_PATH, sync_gsheet=False)
        if not append_price_cache_rows_to_gsheet(canonical, normalized_changed_codes):
            # 增量 append 失敗時退回原本整張同步，避免雲端價格快取落後。
            write_cache_to_gsheet(df, PRICE_CACHE_PATH)
    else:
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
    for row in df.itertuples(index=False):
        row = row._asdict()
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

    for row in df.itertuples(index=False):
        row = row._asdict()
        label = str(row["分點"]).strip()
        name = str(row["分點名稱"]).strip()
        code = str(row["券商代號"]).strip()

        if label and name and code and label in TARGET_PATTERNS:
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

    for row in df.itertuples(index=False):
        row = row._asdict()
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

    out_df = df[required_cols].copy()
    out_df["日期"] = out_df["日期"].map(normalize_date_str)
    out_df = out_df.drop_duplicates(
        subset=["權證代號", "券商代號", "日期"],
        keep="last"
    ).reset_index(drop=True)

    return out_df


def history_cache_keys(history_df):
    keys = set()

    if history_df is None or history_df.empty:
        return keys

    for row in history_df[["權證代號", "券商代號"]].drop_duplicates().itertuples(index=False):
        row_dict = row._asdict()
        keys.add(candidate_key_from_values(row_dict["權證代號"], row_dict["券商代號"]))

    return keys


def history_cache_latest_dates(history_df):
    """
    回傳每組「權證代號 + 券商代號」在歷史快取中的最後日期。
    有快取且最後日期夠新時，就不用重打 API5。
    """
    latest = {}

    if history_df is None or history_df.empty:
        return latest

    needed = {"權證代號", "券商代號", "日期"}
    if not needed.issubset(set(history_df.columns)):
        return latest

    df = history_df[["權證代號", "券商代號", "日期"]].copy().fillna("")
    df["日期"] = df["日期"].map(normalize_date_str)

    for (warrant_code, broker_code), g in df.groupby(["權證代號", "券商代號"], dropna=False):
        dates = []
        for d in g["日期"].tolist():
            dt = parse_date(d)
            if dt:
                dates.append(dt)
        if dates:
            latest[candidate_key_from_values(warrant_code, broker_code)] = max(dates)

    return latest


def get_incremental_refresh_target_dt():
    lag_days = max(int(CACHE_INCREMENTAL_REFRESH_LAG_DAYS or 0), 0)
    return datetime.today() - timedelta(days=lag_days)


def should_fetch_candidate_incremental(key, history_keys, history_latest_map, direct_refresh_keys, missing_fetch_keys=None):
    """
    增量抓取判斷：
    1. 沒快取：只有本次近期活動候選 / 活動標的擴展候選才抓。
       這可以清掉舊候選快取裡大量從未成交的空候選。
    2. 關閉增量：維持舊邏輯，direct refresh 命中就抓。
    3. 有快取但不是 API4 直接近期活動候選：不抓，直接用快取。
    4. 有快取且 API4 直接近期活動候選：只有快取最後日期落後目標日期才抓。
    """
    missing_fetch_keys = missing_fetch_keys or set()

    if key not in history_keys:
        if not CACHE_INCREMENTAL_UPDATE_ENABLED:
            return True
        return key in missing_fetch_keys

    if not CACHE_INCREMENTAL_UPDATE_ENABLED:
        return key in direct_refresh_keys

    if key not in direct_refresh_keys:
        return False

    latest_dt = history_latest_map.get(key)
    if latest_dt is None:
        return True

    target_dt = get_incremental_refresh_target_dt()
    return latest_dt.date() < target_dt.date()


def item_to_history_rows(item):
    rows = []
    df = item["df"]

    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        rows.append({
            "權證代號": item["warrant_code"],
            "權證名稱": item["warrant_name"],
            "標的股": item["underlying_code"],
            "標的名稱": item.get("underlying_name", ""),
            "分點": item["broker_label"],
            "分點名稱": item["broker_name"],
            "券商代號": item["broker_code"],
            "日期": normalize_date_str(row_dict["日期"]),
            "買進股數": int(row_dict["買進股數"]),
            "賣出股數": int(row_dict["賣出股數"]),
            "買進金額": int(row_dict["買進金額"]),
            "賣出金額": int(row_dict["賣出金額"]),
            "買超股數": int(row_dict["買超股數"]),
            "買超金額": int(row_dict["買超金額"]),
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


def append_history_rows_to_gsheet(new_rows):
    """
    將本次 API5 新增 / 更新的原始分點資料增量 append 到 Google Sheet。
    下次讀回時 load_history_cache() 會用 權證代號 + 券商代號 + 日期 去重，保留最後一筆。
    """
    if not HISTORY_GSHEET_INCREMENTAL_APPEND:
        return False

    if not GSHEET_CACHE_ENABLED or not gsheet_enabled():
        return False

    if not new_rows:
        return False

    try:
        title = cache_sheet_name_from_path(HISTORY_CACHE_PATH)
        headers = [
            "權證代號", "權證名稱", "標的股", "標的名稱",
            "分點", "分點名稱", "券商代號", "日期",
            "買進股數", "賣出股數", "買進金額", "賣出金額",
            "買超股數", "買超金額",
        ]

        ws = get_or_create_worksheet(title, rows=max(len(new_rows) + 1, 100), cols=len(headers))
        if ws is None:
            return False

        try:
            existing_headers = ws.row_values(1)
        except Exception:
            existing_headers = []

        if not existing_headers or all(str(x).strip() == "" for x in existing_headers):
            header_values = normalize_gsheet_values_for_text_columns([headers])
            gsheet_api_call(
                f"寫入原始分點歷史快取表頭 {title}",
                ws.update,
                values=header_values,
                range_name="A1",
                value_input_option="USER_ENTERED",
            )

        values = []
        for row in new_rows:
            values.append([row.get(h, "") for h in headers])

        normalized = normalize_gsheet_values_for_text_columns([headers] + values)[1:]

        for start in range(0, len(normalized), GSHEET_CHUNK_ROWS):
            chunk = normalized[start:start + GSHEET_CHUNK_ROWS]
            gsheet_api_call(
                f"增量追加原始分點歷史 {title} {start + 1}-{start + len(chunk)}",
                ws.append_rows,
                chunk,
                value_input_option="USER_ENTERED",
            )

        print(f"  ☁️ 已增量追加快取到 Google Sheet：{title}，本次 {len(new_rows):,} 筆")
        return True
    except Exception as e:
        print(f"  ⚠️ 原始分點資料快取增量追加到 Google Sheet 失敗：{type(e).__name__}: {e}")
        return False


def save_history_cache(history_df, fetched_items=None, previous_history_empty=False):
    if not USE_CACHE or history_df is None or history_df.empty:
        return

    # 本地 / runner 內仍寫完整 CSV，讓同一次執行後續計算使用完整資料。
    # Google Sheet 若已經有大型歷史快取，改成只 append 本次更新 rows，避免百萬筆整表重寫。
    do_full_gsheet_sync = True

    if HISTORY_GSHEET_INCREMENTAL_APPEND and not previous_history_empty and len(history_df) > HISTORY_CACHE_FULL_SYNC_THRESHOLD_ROWS:
        do_full_gsheet_sync = False

    write_cache_csv(history_df, HISTORY_CACHE_PATH, sync_gsheet=do_full_gsheet_sync)

    if not do_full_gsheet_sync:
        delta_rows = []
        for item in (fetched_items or []):
            delta_rows.extend(item_to_history_rows(item))
        append_history_rows_to_gsheet(delta_rows)

    print(f"  💾 已更新原始分點資料快取：{HISTORY_CACHE_PATH}，共 {len(history_df):,} 筆")


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

    for row in df.itertuples(index=False, name=None):
        cell = str(row[0]).strip()

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


def normalize_stock_name_text(s):
    return str(s).strip().replace(" ", "").replace("　", "")


def make_stock_aliases(stock_name, exact_stock_names=None):
    """
    建立股票名稱候選別名，但避免把某一檔股票的簡稱撞到另一檔真實股票名稱。

    修正重點：
    1. 8028 昇陽半導體可產生「昇陽半」，讓「昇陽半XXX購」正確對到 8028。
    2. 8028 昇陽半導體不可產生「昇陽」，因為「昇陽」本身是 3266。
    3. 後續比對會用最長前綴優先，因此「昇陽半」會優先於「昇陽」。
    """
    name = normalize_stock_name_text(stock_name)
    aliases = set()

    if not name:
        return aliases

    exact_stock_names = exact_stock_names or set()

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
                candidate = stripped[:-len(suffix)]
                stripped = candidate

                # 如果切出來的簡稱剛好是另一檔股票的完整名稱，就不要加入。
                # 例如「昇陽半導體」切成「昇陽」，但「昇陽」本身是 3266。
                if candidate not in exact_stock_names or candidate == name:
                    aliases.add(candidate)

                changed = True
                break

    for n in range(min(4, len(name)), 1, -1):
        candidate = name[:n]

        # 前綴簡稱若撞到另一檔真實股票名稱，也不要加入。
        if candidate in exact_stock_names and candidate != name:
            continue

        aliases.add(candidate)

    return {a for a in aliases if len(a) >= 2}


def build_underlying_resolver(stock_map):
    """
    預先建立完整股名與安全 alias 對照表。

    重要：
    原本 find_underlying_info() 會先用完整股名比對，導致「昇陽半XXX購」
    先被短股名「昇陽」吃到，誤判成 3266。
    這版改成完整股名與 alias 全部放在同一個候選表，統一採「最長前綴優先」。
    因此「昇陽半」會優先於「昇陽」。
    """
    exact_stock_names = set()

    for stock_name in stock_map.keys():
        sname = normalize_stock_name_text(stock_name)
        if sname:
            exact_stock_names.add(sname)

    candidates = []
    seen = set()

    def add_candidate(prefix, stock_code, stock_name, is_exact_alias):
        prefix_norm = normalize_stock_name_text(prefix)
        stock_name_norm = normalize_stock_name_text(stock_name)

        if not prefix_norm:
            return

        key = (prefix_norm, str(stock_code), stock_name_norm)

        if key in seen:
            return

        seen.add(key)

        candidates.append({
            "prefix": prefix_norm,
            "prefix_len": len(prefix_norm),
            "is_exact_alias": 1 if is_exact_alias else 0,
            "stock_name_len": len(stock_name_norm),
            "stock_code": stock_code,
            "stock_name": stock_name,
        })

    for stock_name, stock_code in stock_map.items():
        stock_name_norm = normalize_stock_name_text(stock_name)

        # 完整股名也是候選，但不再獨立提前回傳，避免短完整股名壓過較長 alias。
        add_candidate(stock_name_norm, stock_code, stock_name, True)

        for alias in make_stock_aliases(stock_name, exact_stock_names):
            alias_norm = normalize_stock_name_text(alias)
            add_candidate(alias_norm, stock_code, stock_name, alias_norm == stock_name_norm)

    candidates = sorted(
        candidates,
        key=lambda x: (
            x["prefix_len"],
            x["is_exact_alias"],
            -x["stock_name_len"],
        ),
        reverse=True
    )

    return candidates


def find_underlying_info(warrant_name, stock_map, resolver=None):
    wname = normalize_stock_name_text(warrant_name)

    if not wname:
        return "", ""

    if resolver is None:
        resolver = build_underlying_resolver(stock_map)

    for rec in resolver:
        prefix = rec["prefix"]

        if prefix and wname.startswith(prefix):
            return rec["stock_code"], rec["stock_name"]

    return "", ""




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

    # 只建立一次 resolver，避免每檔權證都重新掃全部股票與 alias，保持執行速度。
    underlying_resolver = build_underlying_resolver(stock_map)

    seen_warrants = set()

    for market_name, df in all_dfs:
        for row in df.itertuples(index=False, name=None):
            cell = str(row[0]).strip()

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
                underlying, underlying_name = find_underlying_info(name, stock_map, underlying_resolver)

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

        # 每日執行仍要即時更新權證清單，避免新上市 / 新出現的權證不在 warrants_cache.csv，
        # 導致後續全市場最近資料預掃描也完全掃不到該權證。
        print("  🔄 即時更新今日認購權證清單，並與快取合併...")
        live_warrants = get_all_call_warrants_live()

        if not live_warrants:
            print("  ⚠️ 即時權證清單取得失敗，改用既有權證清單快取。")
            return cached_warrants

        merged = {}
        for w in cached_warrants:
            code = str(w.get("代號", "")).strip()
            if code:
                merged[code] = w

        old_count = len(merged)
        new_count = 0

        for w in live_warrants:
            code = str(w.get("代號", "")).strip()
            if not code:
                continue

            if code not in merged:
                new_count += 1

            # 以即時清單為準更新名稱、標的股與標的名稱，
            # 避免舊快取的標的資訊不完整或過期。
            merged[code] = w

        warrants = list(merged.values())
        save_warrants_cache(warrants)

        print(
            f"  ✅ 權證清單更新完成：原快取 {old_count:,} 支，"
            f"即時清單 {len(live_warrants):,} 支，新增 {new_count:,} 支，合併後 {len(warrants):,} 支"
        )

        return warrants

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




def collect_underlying_codes_from_candidates(candidates):
    """從 API4 prescan 掃到的候選中取得近期有活動的標的股。"""
    underlying_codes = set()

    if not candidates:
        return underlying_codes

    for c in candidates:
        try:
            underlying_code = str(c[2]).strip()
        except Exception:
            underlying_code = ""

        if not underlying_code:
            continue

        norm_code = normalize_underlying_code_for_group(underlying_code)
        underlying_codes.add(norm_code or underlying_code)

    return {code for code in underlying_codes if str(code).strip()}


def collect_underlying_broker_pairs_from_candidates(candidates):
    """
    從 API4 prescan 候選整理出「標的股 + 券商代號」活動配對。

    舊版只要某標的近期有活動，就展開成「該標的所有認購權證 × 所有追蹤分點」，
    RUN_MODE=2 會膨脹到百萬組候選。

    新版只展開同標的 + 同一個近期真的有活動的分點。
    例如 API4 掃到「元大南屯 × 南亞科」，才展開：
    南亞科所有認購權證 × 元大南屯。
    """
    pairs = set()

    if not candidates:
        return pairs

    for c in candidates:
        try:
            underlying_code = normalize_underlying_code_for_group(c[2]) or str(c[2]).strip()
            broker_code = str(c[6]).strip()
        except Exception:
            underlying_code = ""
            broker_code = ""

        if underlying_code and broker_code:
            pairs.add((underlying_code, broker_code))

    return pairs


def build_underlying_expanded_candidates(warrants, broker_map, underlying_codes, source_label="", active_pairs=None):
    """
    依近期有活動的標的股建立候選池。

    核心邏輯：
    1. API4 先找出最近有活動的「標的股 + 分點」。
    2. 只對同一個活動配對展開：同標的所有認購權證 × 同分點。
    3. 若 active_pairs 沒有傳入，才退回舊的「標的 × 追蹤分點」相容模式。

    這樣仍可抓到「同一分點在同一標的多檔權證分散式買賣超」，
    但不會在 RUN_MODE=2 膨脹成全市場權證 × 全部分點。
    """
    if not warrants or not broker_map or not underlying_codes:
        return []

    normalized_underlyings = set()
    for code in underlying_codes:
        norm_code = normalize_underlying_code_for_group(code)
        if norm_code:
            normalized_underlyings.add(norm_code)
        elif str(code).strip():
            normalized_underlyings.add(str(code).strip())

    if not normalized_underlyings:
        return []

    active_pairs = active_pairs or set()
    active_pairs = {
        (normalize_underlying_code_for_group(u) or str(u).strip(), str(b).strip())
        for u, b in active_pairs
        if str(u).strip() and str(b).strip()
    }

    broker_code_to_info = {}
    for label, (broker_name, broker_code) in broker_map.items():
        broker_code = str(broker_code).strip()
        if broker_code:
            broker_code_to_info[broker_code] = (label, str(broker_name).strip(), broker_code)

    title = source_label.strip() or "活動標的"

    if active_pairs:
        print(
            f"【Step 3a】{title} 候選池擴展：近期活動標的 {len(normalized_underlyings):,} 檔，"
            f"活動標的×分點配對 {len(active_pairs):,} 組 "
            f"→ 同標的所有認購權證 × 同活動分點..."
        )
    else:
        print(
            f"【Step 3a】{title} 候選池擴展：近期活動標的 {len(normalized_underlyings):,} 檔 "
            f"→ 同標的所有認購權證 × 追蹤分點 [相容模式]..."
        )

    candidates = []
    seen = set()
    matched_warrants = 0
    matched_pairs = set()

    for w in warrants:
        warrant_code = str(w.get("代號", "")).strip()
        warrant_name = str(w.get("名稱", "")).strip()
        underlying_code = str(w.get("標的股", "")).strip()
        underlying_name = str(w.get("標的名稱", "")).strip()

        if not warrant_code or not warrant_name or not underlying_code:
            continue

        norm_underlying = normalize_underlying_code_for_group(underlying_code) or underlying_code

        if norm_underlying not in normalized_underlyings:
            continue

        if active_pairs:
            active_broker_codes = [
                broker_code
                for u, broker_code in active_pairs
                if u == norm_underlying and broker_code in broker_code_to_info
            ]
            broker_infos = [broker_code_to_info[broker_code] for broker_code in active_broker_codes]
        else:
            broker_infos = [
                (label, str(broker_name).strip(), str(broker_code).strip())
                for label, (broker_name, broker_code) in broker_map.items()
                if str(broker_code).strip()
            ]

        if not broker_infos:
            continue

        matched_warrants += 1

        for label, broker_name, broker_code in broker_infos:
            c = (
                warrant_code,
                warrant_name,
                norm_underlying,
                underlying_name,
                label,
                broker_name,
                broker_code,
            )
            key = candidate_key_from_tuple(c)

            if key in seen:
                continue

            seen.add(key)
            matched_pairs.add((norm_underlying, broker_code))
            candidates.append(c)

    print(
        f"  ✅ {title} 候選池擴展完成：命中權證 {matched_warrants:,} 支，"
        f"命中活動配對 {len(matched_pairs):,} 組 → {len(candidates):,} 組候選"
    )
    return candidates

def build_selected_full_market_candidates(warrants, broker_map):
    """
    RUN_MODE=1 精選分點完整追蹤候選池。

    目的：
    原本候選池依賴 api4 prescan，若某檔權證的分點資料沒有在 api4 回傳清單中出現，
    即使該分點今天實際有大額賣出，後續也不會進入 API5 歷史抓取與每日賣出明細。

    這裡改成針對精選分點建立「所有認購權證 × 精選分點」候選組合，
    再由 API5 抓該分點該權證完整 250 天歷史，確保像「元大南屯賣南亞科」這類
    分散在多檔權證的大額賣超不會因候選池漏抓而少算。
    """
    print("【Step 3a】RUN_MODE=1 精選分點完整候選池：所有認購權證 × 精選分點...")

    if not warrants or not broker_map:
        return []

    candidates = []
    seen = set()

    for w in warrants:
        warrant_code = str(w.get("代號", "")).strip()
        warrant_name = str(w.get("名稱", "")).strip()
        underlying_code = str(w.get("標的股", "")).strip()
        underlying_name = str(w.get("標的名稱", "")).strip()

        if not warrant_code or not warrant_name:
            continue

        for label, (broker_name, broker_code) in broker_map.items():
            broker_code = str(broker_code).strip()
            if not broker_code:
                continue

            c = (
                warrant_code,
                warrant_name,
                underlying_code,
                underlying_name,
                label,
                str(broker_name).strip(),
                broker_code,
            )
            key = candidate_key_from_tuple(c)

            if key in seen:
                continue

            seen.add(key)
            candidates.append(c)

    print(
        f"  ✅ 精選分點完整候選池建立完成：權證 {len(warrants):,} 支 × 分點 {len(broker_map):,} 間 "
        f"→ {len(candidates):,} 組候選"
    )
    return candidates


def prescan_all(warrants, broker_map):
    global PRESCAN_REFRESH_KEYS, PRESCAN_MISSING_FETCH_KEYS

    broker_map = filter_broker_map_for_active_targets(broker_map)
    cached_candidates = filter_candidates_by_broker_map(load_candidates_cache(), broker_map)

    if cached_candidates:
        if RUN_MODE == 1:
            print("【Step 3a】讀取精選分點候選組合快取...")
            print(f"  ✅ 已讀取精選候選組合快取：{len(cached_candidates):,} 組")
        else:
            print("【Step 3a】讀取候選組合快取...")
            print(f"  ✅ 已讀取候選組合快取：{len(cached_candidates):,} 組")

    if FAST_SKIP_RECENT_PRESCAN:
        print("  ⚠️ 偵測到 FAST_SKIP_RECENT_PRESCAN=1，但目前仍會補掃最近資料，避免漏掉新權證 / 新候選組合。")

    # 新版 RUN_MODE=1 / RUN_MODE=2 共用增量流程：
    # 1. 用 API4 補掃最近資料，找出追蹤分點近期有活動的權證與標的。
    # 2. 將活動標的展開成「同標的所有認購權證 × 追蹤分點」。
    # 3. 只有 API4 直接掃到的 key 會放入 PRESCAN_REFRESH_KEYS；
    #    擴展出的 key 若快取已有資料就不強制 API5，只在缺快取時補抓。
    if cached_candidates:
        scan_days = SELECTED_FULL_SCAN_DAYS if RUN_MODE == 1 else CACHE_RECENT_SCAN_DAYS
    else:
        scan_days = max(40, SELECTED_FULL_SCAN_DAYS if RUN_MODE == 1 else CACHE_RECENT_SCAN_DAYS)

    if RUN_MODE == 1:
        print(
            f"  🔄 RUN_MODE=1：補掃全市場最近 {scan_days} 天，"
            "先鎖定精選分點有活動的標的，再展開同標的所有權證。"
        )
    else:
        print(
            f"  🔄 RUN_MODE=2：補掃全市場最近 {scan_days} 天，"
            "先鎖定完整分點清單有活動的標的，再展開同標的所有權證。"
        )

    recent_candidates = prescan_all_live(warrants, broker_map, scan_days=scan_days)
    recent_candidates = filter_candidates_by_broker_map(recent_candidates, broker_map)

    active_underlyings = collect_underlying_codes_from_candidates(recent_candidates)
    active_underlying_broker_pairs = collect_underlying_broker_pairs_from_candidates(recent_candidates)

    if EXPAND_ACTIVE_UNDERLYING_WARRANTS and active_underlyings:
        mode_label = "精選分點活動標的" if RUN_MODE == 1 else "全分點活動標的"
        expanded_candidates = build_underlying_expanded_candidates(
            warrants,
            broker_map,
            active_underlyings,
            source_label=mode_label,
            active_pairs=active_underlying_broker_pairs,
        )
        expanded_candidates = filter_candidates_by_broker_map(expanded_candidates, broker_map)
    else:
        expanded_candidates = []
        if not active_underlyings:
            print("  ⚠️ 最近 API4 未掃到可展開的活動標的，本次只使用既有候選快取或 API4 直接候選。")
        else:
            print("  ⚠️ EXPAND_ACTIVE_UNDERLYING_WARRANTS=0，略過活動標的候選池擴展。")

    refresh_candidates = merge_candidates(recent_candidates, expanded_candidates)
    refresh_candidates = filter_candidates_by_broker_map(refresh_candidates, broker_map)

    # 只有 API4 直接掃到的候選才需要檢查是否更新；擴展候選若快取已有資料不強制刷新。
    PRESCAN_REFRESH_KEYS = {candidate_key_from_tuple(c) for c in recent_candidates}
    # 若候選沒有歷史快取，只有本次近期活動標的相關候選才需要補抓。
    # 這可避免舊版留下的「全市場權證 × 分點」空候選在有候選快取時仍全部打 API5。
    PRESCAN_MISSING_FETCH_KEYS = {candidate_key_from_tuple(c) for c in refresh_candidates}

    merged_candidates = merge_candidates(cached_candidates, refresh_candidates)
    merged_candidates = filter_candidates_by_broker_map(merged_candidates, broker_map)

    save_candidates_cache(merged_candidates)

    print(
        f"  ✅ 候選組合完成：快取 {len(cached_candidates):,} 組，"
        f"API4直接候選 {len(recent_candidates):,} 組，"
        f"活動標的×分點配對 {len(active_underlying_broker_pairs):,} 組，"
        f"活動標的擴展 {len(expanded_candidates):,} 組，"
        f"合併後 {len(merged_candidates):,} 組"
    )
    print(f"  ✅ 本次需用 API5 檢查更新的直接候選組合：{len(PRESCAN_REFRESH_KEYS):,} 組")

    return merged_candidates


# ══════════════════════════════════════════════════════════════════════
# A 事件：單檔權證單日買進金額 >= 100萬
# ══════════════════════════════════════════════════════════════════════

def build_a_events_from_df(item):
    df = item["df"]
    events = []

    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        date   = row_dict["日期"]
        buy_s  = int(row_dict["買進股數"])
        sell_s = int(row_dict["賣出股數"])
        buy_a  = int(row_dict["買進金額"])
        sell_a = int(row_dict["賣出金額"])

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

                # ★ 新增：累計這個事件「已實現賣出金額」（依每批實際賣價加權）。
                #   出清時用此累計值對原始買進成本算報酬，
                #   才不會只用最後一筆賣價，與 B/C/D 的累積實現報酬一致。
                ev["已實現賣出金額"] += alloc * sell_p

                if ev["剩餘股數"] > 0:
                    if ev["減碼日"] is None:
                        ev["減碼日"] = date
                        ev["減碼均價"] = sell_p
                        ev["減碼獲利%"] = round((sell_p - ev["買進均價"]) / ev["買進均價"] * 100, 2) if ev["買進均價"] else None
                    ev["狀態"] = "減碼"
                else:
                    ev["狀態"] = "出清"
                    ev["出清日"] = date

                    # ★ 修改：出清均價改用「加權平均賣出價」= 累計賣出金額 / 原始買進股數，
                    #   出清獲利% 改用「累計賣出金額 vs 原始買進金額」。
                    #   注意成本基準必須用 ev["買進金額"]，不是迴圈中的 buy_a。
                    cost_basis = ev["買進金額"]
                    total_sold_shares = ev["買進股數"]

                    if total_sold_shares > 0:
                        ev["出清均價"] = round(ev["已實現賣出金額"] / total_sold_shares, 4)
                    else:
                        ev["出清均價"] = sell_p

                    if cost_basis:
                        ev["出清獲利%"] = round(
                            (ev["已實現賣出金額"] - cost_basis) / cost_basis * 100, 2
                        )
                    else:
                        ev["出清獲利%"] = None

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
                "已實現賣出金額": 0,   # ★ 新增：累計已賣出金額，供出清時加權計算報酬
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

        for row in item["df"].itertuples(index=False):
            row_dict = row._asdict()
            daily_records.append({
                "日期": row_dict["日期"],
                "分點": item["broker_label"],
                "分點名稱": item["broker_name"],
                "券商代號": item["broker_code"],
                "權證代號": item["warrant_code"],
                "權證名稱": item["warrant_name"],
                "標的股": item["underlying_code"],
                "標的名稱": item.get("underlying_name", ""),
                "買進股數": int(row_dict["買進股數"]),
                "賣出股數": int(row_dict["賣出股數"]),
                "買進金額": int(row_dict["買進金額"]),
                "賣出金額": int(row_dict["賣出金額"]),
                "買超股數": int(row_dict["買超股數"]),
                "買超金額": int(row_dict["買超金額"]),
            })

    return daily_records


def make_daily_key(broker_code, underlying_code, date, warrant_code):
    return (
        str(broker_code),
        str(underlying_code),
        normalize_date_str(date),
        str(warrant_code),
    )









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

        for wr in warrant_rows.itertuples(index=False):
            wr = wr._asdict()
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
            start_dt = dt - timedelta(days=60)
            end_dt = dt + timedelta(days=160)
            update_code_range(ev["權證代號"], start_dt, end_dt)
            update_code_range(ev["標的股"], start_dt, end_dt)

    for ev in b_events + c_events + d_events:
        dt = parse_date(ev["結束日"])
        if dt:
            start_dt = dt - timedelta(days=60)
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
    changed_price_codes = set()

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
                    if fetched_prices:
                        changed_price_codes.add(norm_code)

                add_price_aliases(price_cache, code, merged_prices)

            except:
                code = futures[future]
                old_prices = get_cached_prices_for_code(persistent_price_cache, code)
                add_price_aliases(price_cache, code, old_prices)

            if done % 20 == 0:
                print(f"  [{done}/{len(fetch_plan)}] 收盤價補抓中...")

    save_price_cache(persistent_price_cache, changed_codes=changed_price_codes)

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


def fmt_price_value(v):
    if v is None:
        return "-"

    try:
        v = float(v)
        if v <= 0:
            return "-"
        if v.is_integer():
            return int(v)
        return round(v, 2)
    except:
        return "-"


def get_ma_status_cells(price_cache, underlying_code, base_date):
    """
    依照事件選出日計算標的股技術狀態：
    1. 標的股股價：選出日當天或之前最近一個交易日收盤價
    2. 5MA：若標的股股價 > 當天 5 日均線，顯示 ✓，否則空白
    3. 20MA：若當天 20 日均線 > 前一交易日 20 日均線，顯示 ✓，否則空白
    """
    prices = get_price_series_from_cache(price_cache, underlying_code)

    if not prices:
        return "-", "", ""

    base_date = normalize_date_str(base_date)
    valid_rows = []

    for d, p in prices.items():
        dt = parse_date(d)
        price = safe_price_float(p)

        if not dt or price is None:
            continue

        if normalize_date_str(d) <= base_date:
            valid_rows.append((normalize_date_str(d), price))

    valid_rows = sorted(valid_rows, key=lambda x: x[0])

    if not valid_rows:
        return "-", "", ""

    current_price = valid_rows[-1][1]
    close_values = [p for _, p in valid_rows]

    ma5_mark = ""
    ma20_mark = ""

    if len(close_values) >= 5:
        ma5 = sum(close_values[-5:]) / 5
        if current_price > ma5:
            ma5_mark = "✓"

    if len(close_values) >= 21:
        ma20_today = sum(close_values[-20:]) / 20
        ma20_prev = sum(close_values[-21:-1]) / 20
        if ma20_today > ma20_prev:
            ma20_mark = "✓"

    return fmt_price_value(current_price), ma5_mark, ma20_mark



# ══════════════════════════════════════════════════════════════════════
# TOP15 圖片用：固定部位明細與共識淨買超資料集
# ══════════════════════════════════════════════════════════════════════

def get_latest_price_info_on_or_before(price_cache, code, target_date):
    """
    取得指定代號在 target_date 當天或之前最近一筆有效收盤價。

    回傳：
    - (價格, 日期字串)
    - 找不到則回傳 (None, "")
    """
    prices = get_price_series_from_cache(price_cache, code)

    if not prices:
        return None, ""

    target_str = normalize_date_str(target_date)
    valid = []

    for d, p in prices.items():
        dt = parse_date(d)
        price = safe_price_float(p)

        if not dt or price is None:
            continue

        d_norm = normalize_date_str(d)
        if d_norm <= target_str:
            valid.append((d_norm, price))

    if not valid:
        return None, ""

    valid.sort(key=lambda x: x[0])
    return valid[-1][1], valid[-1][0]


def collect_top15_return_recent_dates(a_events, b_events, c_events, d_events, lookback_days=None):
    """
    從 A/B/C/D 事件抓近 N 個有效事件交易日。

    這個範圍要和 TOP15 圖的「近一個月交易日」概念一致，
    讓報酬率快取可以直接對應圖片中的 TOP15 參與分點。
    """
    if lookback_days is None:
        lookback_days = TOP15_LOOKBACK_TRADING_DAYS

    dates = set()

    for ev in a_events:
        d = parse_date(ev.get("買進日") or ev.get("事件日"))
        if d:
            dates.add(normalize_date_str(d.strftime("%Y/%m/%d")))

    for ev in b_events + c_events + d_events:
        d = parse_date(ev.get("事件日") or ev.get("結束日") or ev.get("起始日"))
        if d:
            dates.add(normalize_date_str(d.strftime("%Y/%m/%d")))

    recent_dates = sorted(dates, reverse=True)[:max(int(lookback_days), 1)]
    return recent_dates


def _top15_return_event_date(ev, is_a=False):
    if is_a:
        return normalize_date_str(ev.get("買進日") or ev.get("事件日") or "")
    return normalize_date_str(ev.get("事件日") or ev.get("結束日") or ev.get("起始日") or "")


def collect_top15_return_position_lots(a_events, b_events, c_events, d_events, recent_dates, item_map=None):
    """
    將近 N 個有效事件交易日內的 A/B/C/D 買超事件轉成可計算未實現報酬率的 lot。

    定義：
    - 這裡只看「近 N 個有效事件交易日內」被 A/B/C/D 納入的權證部位。
    - 報酬率要表達的是：這批近 N 日買進後，持有到目前的帳面報酬。
    - A 直接使用原本單檔買進毛額。
    - B/C/D 優先回到 item_map 的原始逐日資料抓「毛買進金額 / 毛買進股數」建立 lot；
      後續賣出一律交給 apply_sales_to_top15_return_lots() 統一扣減。
      這樣報酬率就是「當時實際買進均價 vs 目前權證價格」，不會用淨額先扣一次又再扣賣出。
    """
    date_set = set(recent_dates or [])
    lots = []
    item_map = item_map or {}

    def _find_item(broker_code, warrant_code):
        broker_code = str(broker_code or "").strip()
        warrant_code = str(warrant_code or "").strip()

        if not broker_code or not warrant_code:
            return None

        candidates = []
        for wc in [
            warrant_code,
            normalize_warrant_code_for_unique(warrant_code),
            normalize_price_code(warrant_code),
        ]:
            wc = str(wc or "").strip()
            if wc and wc not in candidates:
                candidates.append(wc)

        for wc in candidates:
            item = item_map.get((broker_code, wc))
            if item:
                return item

        return None

    def add_lot(
        event_code, event_type, broker_label, broker_name, broker_code,
        underlying_code, underlying_name, event_date, buy_date,
        warrant_code, warrant_name, buy_amount, buy_qty, source_text
    ):
        event_date = normalize_date_str(event_date)
        buy_date = normalize_date_str(buy_date or event_date)

        if not event_date or event_date not in date_set:
            return False

        warrant_code = normalize_warrant_code_for_unique(warrant_code)
        buy_amount = float(buy_amount or 0)
        buy_qty = float(buy_qty or 0)

        if not warrant_code or buy_amount <= 0 or buy_qty <= 0:
            return False

        lots.append({
            "事件": event_code,
            "事件類型": event_type,
            "事件日": event_date,
            "買進日": buy_date,
            "分點": str(broker_label or "").strip(),
            "分點名稱": str(broker_name or "").strip(),
            "券商代號": str(broker_code or "").strip(),
            "標的股": str(underlying_code or "").strip(),
            "標的名稱": str(underlying_name or "").strip(),
            "權證代號": warrant_code,
            "權證名稱": str(warrant_name or "").strip(),
            "原始股數": buy_qty,
            "原始成本": buy_amount,
            "剩餘股數": buy_qty,
            "剩餘成本": buy_amount,
            "來源": source_text,
        })
        return True

    def add_group_lots_from_history(event_code, event_type, ev, lot, event_date, start_date, end_date):
        """
        B/C/D TOP15 報酬率用毛買進資料建立 lot。
        回傳 True 代表已成功用原始流水建立；False 則外層可退回舊欄位資料。
        """
        event_date = normalize_date_str(event_date)
        if not event_date or event_date not in date_set:
            return False

        warrant_code = normalize_warrant_code_for_unique(lot.get("權證代號", ""))
        if not warrant_code:
            return False

        start_date = normalize_date_str(start_date or event_date)
        end_date = normalize_date_str(end_date or start_date)

        item = _find_item(ev.get("券商代號", ""), warrant_code)
        if not item:
            return False

        df = item.get("df", pd.DataFrame())
        if df is None or df.empty:
            return False

        added = False
        df2 = df.copy()
        df2["日期"] = df2["日期"].map(normalize_date_str)
        df2 = df2.sort_values("日期").reset_index(drop=True)

        for row in df2.itertuples(index=False):
            row_dict = row._asdict()
            buy_date = normalize_date_str(row_dict.get("日期", ""))

            if not buy_date or buy_date < start_date or buy_date > end_date:
                continue

            buy_qty = float(row_dict.get("買進股數", 0) or 0)
            buy_amount = float(row_dict.get("買進金額", 0) or 0)

            if buy_qty <= 0 or buy_amount <= 0:
                continue

            added = add_lot(
                event_code,
                event_type,
                ev.get("分點", ""),
                ev.get("分點名稱", ""),
                ev.get("券商代號", ""),
                ev.get("標的股", ""),
                ev.get("標的名稱", ""),
                event_date,
                buy_date,
                warrant_code,
                lot.get("權證名稱", ""),
                buy_amount,
                buy_qty,
                f'{event_code} | {buy_date} | {warrant_code} {lot.get("權證名稱", "")}',
            ) or added

        return added

    for ev in a_events:
        event_date = _top15_return_event_date(ev, is_a=True)
        add_lot(
            "A",
            ev.get("事件類型", "A-單檔權證大買"),
            ev.get("分點", ""),
            ev.get("分點名稱", ""),
            ev.get("券商代號", ""),
            ev.get("標的股", ""),
            ev.get("標的名稱", ""),
            event_date,
            event_date,
            ev.get("權證代號", ""),
            ev.get("權證名稱", ""),
            ev.get("買進金額", 0),
            ev.get("買進股數", 0),
            f'A | {ev.get("權證代號", "")} {ev.get("權證名稱", "")}',
        )

    for event_code, events in [("B", b_events), ("C", c_events), ("D", d_events)]:
        for ev in events:
            event_date = _top15_return_event_date(ev, is_a=False)
            event_type = ev.get("事件類型", f"{event_code}-事件")

            for lot in ev.get("lots", []):
                if event_code == "D":
                    # D 是近 N 日累積事件，報酬率要用該 D 視窗內的實際毛買進流水。
                    buy_start_date = ev.get("起始日") or lot.get("買進日") or event_date
                    buy_end_date = ev.get("結束日") or event_date
                else:
                    # B/C 的 lot 本身已有實際買進日，直接抓該日毛買進流水。
                    buy_start_date = lot.get("買進日") or ev.get("事件日") or ev.get("結束日") or event_date
                    buy_end_date = buy_start_date

                used_history = add_group_lots_from_history(
                    event_code,
                    event_type,
                    ev,
                    lot,
                    event_date,
                    buy_start_date,
                    buy_end_date,
                )

                if used_history:
                    continue

                # 備援：若 item_map 找不到原始流水，才沿用事件內既有 lot 金額。
                # 這個分支正常情況很少用到，保留是避免舊快取缺資料時整批報酬率消失。
                add_lot(
                    event_code,
                    event_type,
                    ev.get("分點", ""),
                    ev.get("分點名稱", ""),
                    ev.get("券商代號", ""),
                    ev.get("標的股", ""),
                    ev.get("標的名稱", ""),
                    event_date,
                    lot.get("買進日") or event_date,
                    lot.get("權證代號", ""),
                    lot.get("權證名稱", ""),
                    lot.get("金額", 0),
                    lot.get("股數", 0),
                    f'{event_code} | {lot.get("權證代號", "")} {lot.get("權證名稱", "")}',
                )

    return lots



def apply_sales_to_top15_return_lots(position_lots, item_map, target_date):
    """
    依照原始分點歷史資料，把近 N 日事件 lot 買進日之後的賣出股數扣掉，得到目前剩餘部位。

    扣減邏輯：
    - 同一分點 + 同一權證代號的 lot 依「買進日」FIFO 扣。
    - 只扣「賣出日 > 買進日」的賣出，避免權證不可當沖時，同日賣出誤扣當日新買。
    - 扣掉的是賣出股數對應的原始成本，不是賣出成交金額。
    - 這裡不回溯計算 22 日以前舊庫存，因為 TOP15 報酬率定義為近 N 日事件部位的帳面報酬。
    """
    if not position_lots:
        return position_lots

    target_dt = parse_date(target_date)
    if not target_dt:
        target_dt = datetime.today()

    lots_by_key = {}
    for lot in position_lots:
        key = (str(lot.get("券商代號", "")).strip(), str(lot.get("權證代號", "")).strip())
        lots_by_key.setdefault(key, []).append(lot)

    for key, lots in lots_by_key.items():
        broker_code, warrant_code = key
        item = item_map.get((broker_code, warrant_code))

        if not item:
            item = item_map.get((broker_code, normalize_price_code(warrant_code)))

        if not item:
            continue

        df = item.get("df", pd.DataFrame())
        if df is None or df.empty:
            continue

        lots.sort(key=lambda x: (x.get("買進日", "") or x.get("事件日", ""), x.get("事件日", ""), x.get("權證代號", "")))
        df2 = df.copy()
        df2["日期"] = df2["日期"].map(normalize_date_str)
        df2 = df2.sort_values("日期").reset_index(drop=True)

        for row in df2.itertuples(index=False):
            row_dict = row._asdict()
            sell_date = normalize_date_str(row_dict.get("日期", ""))
            sell_dt = parse_date(sell_date)

            if not sell_dt or sell_dt > target_dt:
                continue

            sell_qty_left = float(row_dict.get("賣出股數", 0) or 0)
            if sell_qty_left <= 0:
                continue

            for lot in lots:
                if sell_qty_left <= 0:
                    break

                buy_dt = parse_date(lot.get("買進日") or lot.get("事件日", ""))
                if not buy_dt or sell_dt <= buy_dt:
                    continue

                remaining_qty = float(lot.get("剩餘股數", 0) or 0)
                remaining_cost = float(lot.get("剩餘成本", 0) or 0)
                original_qty = float(lot.get("原始股數", 0) or 0)
                original_cost = float(lot.get("原始成本", 0) or 0)

                if remaining_qty <= 0 or remaining_cost <= 0 or original_qty <= 0 or original_cost <= 0:
                    continue

                avg_cost = original_cost / original_qty
                alloc_qty = min(sell_qty_left, remaining_qty)
                alloc_cost = min(remaining_cost, alloc_qty * avg_cost)

                lot["剩餘股數"] = max(remaining_qty - alloc_qty, 0)
                lot["剩餘成本"] = max(remaining_cost - alloc_cost, 0)
                sell_qty_left -= alloc_qty

    return position_lots



def ensure_top15_return_warrant_prices(price_cache, position_lots, target_date):
    """
    TOP15 固定資料集需要權證目前價格。

    原本 fetch_all_prices() 為了加速，B/C/D 預設只抓標的股價格，
    因此這裡會針對目前仍有剩餘部位的權證補抓最新價格，並同步回 price_cache.csv / Google Sheet。
    """
    if not position_lots:
        return price_cache

    target_dt = parse_date(target_date)
    if not target_dt:
        target_dt = datetime.today()

    target_dt = min(target_dt, datetime.today())
    start_dt = target_dt - timedelta(days=max(TOP15_PRICE_LOOKBACK_DAYS, 10))

    needed_codes = sorted({
        normalize_price_code(lot.get("權證代號", ""))
        for lot in position_lots
        if float(lot.get("剩餘成本", 0) or 0) > 0
        and normalize_price_code(lot.get("權證代號", ""))
    })

    if not needed_codes:
        return price_cache

    persistent_price_cache = load_price_cache()
    fetch_plan = []

    for code in needed_codes:
        cached_prices = get_cached_prices_for_code(persistent_price_cache, code)
        current_prices = get_price_series_from_cache(price_cache, code)
        merged_prices = merge_price_dicts(cached_prices, current_prices)

        if merged_prices:
            add_price_aliases(price_cache, code, merged_prices)
            persistent_price_cache[normalize_price_code(code)] = merged_prices

        latest_price, latest_date = get_latest_price_info_on_or_before(price_cache, code, target_dt.strftime("%Y/%m/%d"))
        latest_dt = parse_date(latest_date) if latest_date else None

        need_fetch = latest_price is None
        if latest_dt and (target_dt - latest_dt).days > TOP15_PRICE_STALE_DAYS:
            need_fetch = True

        if need_fetch:
            fetch_plan.append(code)

    print(f"  TOP15固定資料集需檢查權證價格：{len(needed_codes):,} 檔")
    print(f"  TOP15固定資料集需補抓權證價格：{len(fetch_plan):,} 檔")

    if not fetch_plan:
        return price_cache

    def fetch_one(code):
        return code, fetch_twse_prices(code, start_dt, target_dt)

    done = 0
    changed_price_codes = set()
    with ThreadPoolExecutor(max_workers=PRICE_WORKERS) as ex:
        futures = {ex.submit(fetch_one, code): code for code in fetch_plan}

        for future in as_completed(futures):
            done += 1
            code = futures[future]

            try:
                code, fetched_prices = future.result()
            except Exception:
                fetched_prices = {}

            old_prices = get_cached_prices_for_code(persistent_price_cache, code)
            merged_prices = merge_price_dicts(old_prices, fetched_prices)

            if merged_prices:
                norm_code = normalize_price_code(code)
                if norm_code:
                    persistent_price_cache[norm_code] = merged_prices
                    if fetched_prices:
                        changed_price_codes.add(norm_code)
                add_price_aliases(price_cache, code, merged_prices)

            if done % 20 == 0:
                print(f"  [{done}/{len(fetch_plan)}] TOP15固定資料集權證價格補抓中...")

    save_price_cache(persistent_price_cache, changed_codes=changed_price_codes)
    return price_cache



def normalize_top15_target_date(target_date=None):
    raw = str(target_date or TOP15_TARGET_DATE or "").strip()

    if raw:
        dt = parse_date(raw)
        if not dt:
            raise RuntimeError(f"TOP15_TARGET_DATE 格式錯誤，請使用 YYYY/MM/DD 或 YYYY-MM-DD：{raw}")
        return dt.strftime("%Y/%m/%d")

    return datetime.today().strftime("%Y/%m/%d")


def top15_safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        s = str(value).replace(",", "").strip()
        if s in ("", "-", "None", "nan", "null"):
            return default
        return float(s)
    except Exception:
        return default


def top15_fmt_amount_wan(value):
    try:
        return f"{float(value) / 10000:.1f}萬"
    except Exception:
        return "0.0萬"


def build_top15_position_detail_and_consensus_rows(a_events, b_events, c_events, d_events, item_map, price_cache, target_date=None):
    """
    建立 TOP15 圖片用固定資料集。

    產出兩份資料：
    1. 快取_TOP15部位明細：一列代表一筆實際買進 lot，包含原始成本、賣出扣減、剩餘成本、價格與報酬率。
    2. 快取_TOP15共識淨買超：由部位明細加總而成，一列代表一檔標的股，圖片程式可直接讀取排名。

    嚴格規則：
    - 價格日期若距離估值日超過 TOP15_PRICE_STALE_DAYS，視為缺價格。
    - 若剩餘部位缺少有效權證價格，預設保留淨買超成本，但該筆部位不納入報酬率估算。
    - TOP15 總表完全由部位明細加總，不再另外重算。
    """
    if not TOP15_CACHE_ENABLED:
        return [], []

    print("【Step 4b】建立 TOP15 圖片用固定資料集...")

    target_date = normalize_top15_target_date(target_date)
    target_dt = parse_date(target_date)
    update_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    recent_dates = collect_top15_return_recent_dates(
        a_events, b_events, c_events, d_events, TOP15_LOOKBACK_TRADING_DAYS
    )

    if not recent_dates:
        print("  ⚠️ TOP15固定資料集：沒有可用事件日期")
        return [], []

    period_text = f"{min(recent_dates)} ～ {max(recent_dates)}"

    position_lots = collect_top15_return_position_lots(
        a_events, b_events, c_events, d_events, recent_dates, item_map=item_map
    )

    if not position_lots:
        print("  ⚠️ TOP15固定資料集：沒有可用買超 lot")
        return [], []

    position_lots = apply_sales_to_top15_return_lots(position_lots, item_map, target_date)
    position_lots = [
        lot for lot in position_lots
        if top15_safe_float(lot.get("剩餘成本", 0)) > 0 and top15_safe_float(lot.get("剩餘股數", 0)) > 0
    ]

    if not position_lots:
        print("  ⚠️ TOP15固定資料集：扣除賣出後沒有剩餘部位")
        return [], []

    ensure_top15_return_warrant_prices(price_cache, position_lots, target_date)

    detail_rows = []
    validation_errors = []
    missing_price_rows = []

    for lot in position_lots:
        original_qty = top15_safe_float(lot.get("原始股數", 0))
        original_cost = top15_safe_float(lot.get("原始成本", 0))
        remaining_qty = top15_safe_float(lot.get("剩餘股數", 0))
        remaining_cost = top15_safe_float(lot.get("剩餘成本", 0))

        if original_qty <= 0 or original_cost <= 0 or remaining_qty <= 0 or remaining_cost <= 0:
            continue

        sold_qty = max(original_qty - remaining_qty, 0)
        sold_cost = max(original_cost - remaining_cost, 0)
        original_avg = original_cost / original_qty if original_qty else None
        remaining_avg = remaining_cost / remaining_qty if remaining_qty else None

        if remaining_qty - original_qty > 0.0001:
            validation_errors.append(
                f"剩餘股數大於原始股數：{lot.get('分點')} {lot.get('權證代號')} 原始={original_qty} 剩餘={remaining_qty}"
            )

        if remaining_cost - original_cost > 1:
            validation_errors.append(
                f"剩餘成本大於原始成本：{lot.get('分點')} {lot.get('權證代號')} 原始={original_cost} 剩餘={remaining_cost}"
            )

        latest_price, latest_price_date = get_latest_price_info_on_or_before(
            price_cache,
            lot.get("權證代號", ""),
            target_date,
        )

        price_status = "OK"
        latest_dt = parse_date(latest_price_date) if latest_price_date else None

        if latest_price is None:
            price_status = "缺價格"
        elif latest_dt and target_dt and (target_dt - latest_dt).days > TOP15_PRICE_STALE_DAYS:
            price_status = "價格過舊"
            latest_price = None
            latest_price_date = ""

        if latest_price is None:
            market_value = ""
            unrealized_pnl = ""
            return_pct = ""
            return_text = "-"
            if price_status == "缺價格":
                price_status = "未造市不計報酬率"
            elif price_status == "價格過舊":
                price_status = "價格過舊不計報酬率"

            missing_price_rows.append(
                f"TOP15剩餘部位不計報酬率：{lot.get('分點')} {lot.get('標的股')} {lot.get('權證代號')} {lot.get('權證名稱')}，剩餘成本={round(remaining_cost, 0)}，原因={price_status}"
            )

            if not TOP15_EXCLUDE_MISSING_PRICE_FROM_RETURN:
                validation_errors.append(
                    f"TOP15剩餘部位缺價格：{lot.get('分點')} {lot.get('標的股')} {lot.get('權證代號')} {lot.get('權證名稱')}，剩餘成本={round(remaining_cost, 0)}"
                )
        else:
            market_value_float = remaining_qty * latest_price
            unrealized_pnl_float = market_value_float - remaining_cost
            return_pct_float = unrealized_pnl_float / remaining_cost * 100 if remaining_cost > 0 else None
            market_value = round(market_value_float, 0)
            unrealized_pnl = round(unrealized_pnl_float, 0)
            return_pct = "" if return_pct_float is None else round(return_pct_float, 2)
            return_text = "-" if return_pct_float is None else f"{return_pct_float:+.2f}%"

        detail_rows.append({
            "資料範圍": get_result_data_scope(),
            "統計日期": target_date,
            "統計期間": period_text,
            "有效交易日數": len(recent_dates),
            "分點": str(lot.get("分點", "")).strip(),
            "分點名稱": str(lot.get("分點名稱", "")).strip(),
            "券商代號": str(lot.get("券商代號", "")).strip(),
            "標的股": str(lot.get("標的股", "")).strip(),
            "標的名稱": str(lot.get("標的名稱", "")).strip(),
            "事件": str(lot.get("事件", "")).strip(),
            "事件類型": str(lot.get("事件類型", "")).strip(),
            "事件日": normalize_date_str(lot.get("事件日", "")),
            "買進日": normalize_date_str(lot.get("買進日", "")),
            "權證代號": str(lot.get("權證代號", "")).strip(),
            "權證名稱": str(lot.get("權證名稱", "")).strip(),
            "原始股數": round(original_qty, 0),
            "原始成本": round(original_cost, 0),
            "原始均價": "" if original_avg is None else round(original_avg, 4),
            "已扣賣出股數": round(sold_qty, 0),
            "已扣賣出成本": round(sold_cost, 0),
            "剩餘股數": round(remaining_qty, 0),
            "剩餘成本": round(remaining_cost, 0),
            "剩餘均價": "" if remaining_avg is None else round(remaining_avg, 4),
            "最新權證價格": "" if latest_price is None else latest_price,
            "最新價格日期": latest_price_date,
            "目前市值": market_value,
            "未實現損益": unrealized_pnl,
            "報酬率": return_pct,
            "報酬率文字": return_text,
            "價格狀態": price_status,
            "完成狀態": "DONE",
            "來源": str(lot.get("來源", "")).strip(),
            "run_id": run_id,
            "更新時間": update_time,
        })

    if missing_price_rows:
        preview = "\n".join(missing_price_rows[:20])
        extra = "" if len(missing_price_rows) <= 20 else f"\n... 其餘 {len(missing_price_rows) - 20} 筆略"
        print(
            "  ⚠️ TOP15固定資料集：部分權證因多日未造市 / 無有效價格，不納入報酬率估算，但仍保留淨買超成本：\n"
            + preview
            + extra
        )

    if validation_errors and TOP15_FAIL_ON_MISSING_PRICE:
        preview = "\n".join(validation_errors[:20])
        extra = "" if len(validation_errors) <= 20 else f"\n... 其餘 {len(validation_errors) - 20} 筆略"
        raise RuntimeError(
            "TOP15固定資料集驗證失敗，為避免淨買超成本錯誤，本次 RUN 已中止：\n"
            + preview
            + extra
        )

    consensus_rows = build_top15_consensus_rows_from_detail(detail_rows, run_id, update_time)

    print(
        f"  ✅ TOP15固定資料集完成：部位明細 {len(detail_rows):,} 筆，"
        f"共識淨買超 {len(consensus_rows):,} 檔標的"
    )
    return detail_rows, consensus_rows


def build_top15_consensus_rows_from_detail(detail_rows, run_id, update_time):
    """
    由「快取_TOP15部位明細」加總出「快取_TOP15共識淨買超」。
    這裡不再讀 A/B/C/D 或分點歷史，確保圖片用資料只來自同一份固定明細。
    """
    if not detail_rows:
        return []

    agg = {}

    for row in detail_rows:
        underlying = str(row.get("標的股", "")).strip()
        if not underlying:
            continue

        key = underlying
        rec = agg.setdefault(key, {
            "資料範圍": row.get("資料範圍", get_result_data_scope()),
            "統計日期": row.get("統計日期", ""),
            "統計期間": row.get("統計期間", ""),
            "有效交易日數": row.get("有效交易日數", ""),
            "標的股": underlying,
            "標的名稱": str(row.get("標的名稱", "")).strip(),
            "淨買超成本": 0.0,
            "可估成本": 0.0,
            "缺價格成本": 0.0,
            "目前市值": 0.0,
            "未實現損益": 0.0,
            "分點": {},
            "事件集合": set(),
            "權證集合": set(),
            "權證清單": [],
            "最新價格日期集合": set(),
            "資料狀態": "OK",
        })

        remaining_cost = top15_safe_float(row.get("剩餘成本", 0))
        market_value = top15_safe_float(row.get("目前市值", 0), 0.0) if row.get("目前市值", "") != "" else 0.0
        pnl = top15_safe_float(row.get("未實現損益", 0), 0.0) if row.get("未實現損益", "") != "" else 0.0
        price_status = str(row.get("價格狀態", "")).strip()
        broker = str(row.get("分點", "")).strip()
        broker_name = str(row.get("分點名稱", "")).strip()
        broker_code = str(row.get("券商代號", "")).strip()
        event_code = str(row.get("事件", "")).strip()
        warrant_code = str(row.get("權證代號", "")).strip()
        warrant_name = str(row.get("權證名稱", "")).strip()
        latest_price_date = str(row.get("最新價格日期", "")).strip()

        rec["淨買超成本"] += remaining_cost

        if price_status == "OK":
            rec["可估成本"] += remaining_cost
            rec["目前市值"] += market_value
            rec["未實現損益"] += pnl
        else:
            rec["缺價格成本"] += remaining_cost
            rec["資料狀態"] = "部分報酬率未估"

        if event_code:
            rec["事件集合"].add(event_code)
        if warrant_code:
            rec["權證集合"].add(warrant_code)
            warrant_label = f"{warrant_code} {warrant_name}".strip()
            if warrant_label and warrant_label not in rec["權證清單"]:
                rec["權證清單"].append(warrant_label)
        if latest_price_date:
            rec["最新價格日期集合"].add(latest_price_date)

        broker_key = (broker, broker_name, broker_code)
        broker_rec = rec["分點"].setdefault(broker_key, {
            "分點": broker,
            "分點名稱": broker_name,
            "券商代號": broker_code,
            "淨買超成本": 0.0,
            "可估成本": 0.0,
            "缺價格成本": 0.0,
            "目前市值": 0.0,
            "未實現損益": 0.0,
            "事件集合": set(),
            "權證集合": set(),
        })
        broker_rec["淨買超成本"] += remaining_cost
        if price_status == "OK":
            broker_rec["可估成本"] += remaining_cost
            broker_rec["目前市值"] += market_value
            broker_rec["未實現損益"] += pnl
        else:
            broker_rec["缺價格成本"] += remaining_cost
        if event_code:
            broker_rec["事件集合"].add(event_code)
        if warrant_code:
            broker_rec["權證集合"].add(warrant_code)

    rows = []

    sorted_records = sorted(
        agg.values(),
        key=lambda r: (float(r.get("淨買超成本", 0) or 0), float(r.get("可估成本", 0) or 0)),
        reverse=True,
    )

    for rank, rec in enumerate(sorted_records, 1):
        total_cost = float(rec.get("淨買超成本", 0) or 0)
        estimated_cost = float(rec.get("可估成本", 0) or 0)
        missing_cost = float(rec.get("缺價格成本", 0) or 0)
        market_value = float(rec.get("目前市值", 0) or 0)
        pnl = float(rec.get("未實現損益", 0) or 0)
        return_pct = round(pnl / estimated_cost * 100, 2) if estimated_cost > 0 else None
        coverage_pct = round(estimated_cost / total_cost * 100, 2) if total_cost > 0 else None

        broker_rows = []
        broker_json = []

        for broker_rec in sorted(rec["分點"].values(), key=lambda x: x["淨買超成本"], reverse=True):
            b_cost = float(broker_rec.get("淨買超成本", 0) or 0)
            b_estimated_cost = float(broker_rec.get("可估成本", 0) or 0)
            b_pnl = float(broker_rec.get("未實現損益", 0) or 0)
            b_return_pct = round(b_pnl / b_estimated_cost * 100, 2) if b_estimated_cost > 0 else None
            b_events = "/".join(sorted(x for x in broker_rec["事件集合"] if x))
            b_warrant_count = len(broker_rec["權證集合"])
            b_return_text = "-" if b_return_pct is None else f"{b_return_pct:+.2f}%"

            b_missing_cost = float(broker_rec.get("缺價格成本", 0) or 0)
            b_coverage_text = ""
            if b_missing_cost > 0 and b_cost > 0:
                b_coverage_text = f"｜估值{b_estimated_cost / b_cost * 100:.0f}%"

            broker_rows.append(
                f"{broker_rec['分點']} {top15_fmt_amount_wan(b_cost)}（{b_return_text}{b_coverage_text}｜{b_events}｜{b_warrant_count}檔）"
            )
            broker_json.append({
                "分點": broker_rec["分點"],
                "分點名稱": broker_rec["分點名稱"],
                "券商代號": broker_rec["券商代號"],
                "淨買超成本": round(b_cost, 0),
                "可估成本": round(b_estimated_cost, 0),
                "缺價格成本": round(float(broker_rec.get("缺價格成本", 0) or 0), 0),
                "目前市值": round(float(broker_rec.get("目前市值", 0) or 0), 0),
                "未實現損益": round(b_pnl, 0),
                "報酬率": "" if b_return_pct is None else b_return_pct,
                "事件": b_events,
                "權證檔數": b_warrant_count,
            })

        rows.append({
            "資料範圍": rec.get("資料範圍", get_result_data_scope()),
            "統計日期": rec.get("統計日期", ""),
            "統計期間": rec.get("統計期間", ""),
            "有效交易日數": rec.get("有效交易日數", ""),
            "排名": rank,
            "標的股": rec.get("標的股", ""),
            "標的名稱": rec.get("標的名稱", ""),
            "淨買超成本": round(total_cost, 0),
            "可估成本": round(estimated_cost, 0),
            "缺價格成本": round(missing_cost, 0),
            "目前市值": round(market_value, 0),
            "未實現損益": round(pnl, 0),
            "報酬率": "" if return_pct is None else return_pct,
            "報酬率文字": "-" if return_pct is None else (f"{return_pct:+.2f}%" if missing_cost <= 0 else f"{return_pct:+.2f}%（部分估）"),
            "價格覆蓋率": "" if coverage_pct is None else coverage_pct,
            "價格覆蓋率文字": "-" if coverage_pct is None else f"{coverage_pct:.2f}%",
            "參與分點數": len(rec["分點"]),
            "參與分點明細": "\n".join(broker_rows),
            "事件": "/".join(sorted(x for x in rec["事件集合"] if x)),
            "權證檔數": len(rec["權證集合"]),
            "權證清單": "；".join(rec["權證清單"]),
            "最新價格日期": max(rec["最新價格日期集合"]) if rec["最新價格日期集合"] else "",
            "資料狀態": rec.get("資料狀態", "OK"),
            "完成狀態": "DONE",
            "更新時間": update_time,
            "run_id": run_id,
            "分點明細_JSON": json.dumps(broker_json, ensure_ascii=False),
        })

    return rows


def write_top15_position_detail_sheet(wb, detail_rows):
    """寫入 TOP15 部位明細固定資料集。"""
    ws = wb.create_sheet(TOP15_POSITION_DETAIL_SHEET)

    headers = [
        "資料範圍", "統計日期", "統計期間", "有效交易日數",
        "分點", "分點名稱", "券商代號",
        "標的股", "標的名稱",
        "事件", "事件類型", "事件日", "買進日",
        "權證代號", "權證名稱",
        "原始股數", "原始成本", "原始均價",
        "已扣賣出股數", "已扣賣出成本",
        "剩餘股數", "剩餘成本", "剩餘均價",
        "最新權證價格", "最新價格日期",
        "目前市值", "未實現損益", "報酬率", "報酬率文字",
        "價格狀態", "完成狀態", "來源", "run_id", "更新時間",
    ]

    ws.append(headers)

    for row in detail_rows or []:
        ws.append([row.get(h, "") for h in headers])

    col_widths = [12, 12, 24, 12, 14, 18, 12, 10, 12, 8, 22, 12, 12, 12, 24, 12, 14, 10, 14, 14, 12, 14, 10, 12, 12, 14, 14, 10, 12, 10, 10, 44, 16, 20]
    _style_top15_cache_sheet(ws, col_widths, return_col_name="報酬率", status_col_name="價格狀態")


def write_top15_consensus_cache_sheet(wb, consensus_rows):
    """寫入 TOP15 共識淨買超固定資料集，圖片程式應直接讀這張表。"""
    ws = wb.create_sheet(TOP15_CONSENSUS_SHEET)

    headers = [
        "資料範圍", "統計日期", "統計期間", "有效交易日數", "排名",
        "標的股", "標的名稱",
        "淨買超成本", "可估成本", "缺價格成本",
        "目前市值", "未實現損益", "報酬率", "報酬率文字",
        "價格覆蓋率", "價格覆蓋率文字",
        "參與分點數", "參與分點明細",
        "事件", "權證檔數", "權證清單",
        "最新價格日期", "資料狀態", "完成狀態",
        "更新時間", "run_id", "分點明細_JSON",
    ]

    ws.append(headers)

    for row in consensus_rows or []:
        ws.append([row.get(h, "") for h in headers])

    col_widths = [12, 12, 24, 12, 8, 10, 12, 14, 14, 14, 14, 14, 10, 12, 12, 14, 12, 48, 10, 10, 60, 12, 10, 10, 20, 16, 80]
    _style_top15_cache_sheet(ws, col_widths, return_col_name="報酬率", status_col_name="資料狀態")


def _style_top15_cache_sheet(ws, col_widths, return_col_name="報酬率", status_col_name="資料狀態"):
    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    header_map = {str(cell.value).strip(): idx + 1 for idx, cell in enumerate(ws[1])}
    return_col_idx = header_map.get(return_col_name)
    status_col_idx = header_map.get(status_col_name)

    for row in ws.iter_rows(min_row=2):
        pct = None
        status_text = ""

        if return_col_idx:
            try:
                value = row[return_col_idx - 1].value
                if value not in (None, "", "-"):
                    pct = float(value)
            except Exception:
                pct = None

        if status_col_idx:
            status_text = str(row[status_col_idx - 1].value or "").strip()

        if status_text and status_text != "OK":
            row_fill = ORANGE
        elif pct is not None and pct > 0:
            row_fill = RED
        elif pct is not None and pct < 0:
            row_fill = GREEN
        else:
            row_fill = WHITE

        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
            cell.fill = row_fill

        ws.row_dimensions[row[0].row].height = 30

    ws.freeze_panes = "A2"

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
        "買進日", "標的股股價", "5MA", "20MA", "買進張數", "買進金額", "買進均價",
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
        underlying_price, ma5_mark, ma20_mark = get_ma_status_cells(
            price_cache,
            ev.get("標的股"),
            ev.get("買進日"),
        )

        row = [
            ev["事件類型"],
            ev["分點"],
            ev["權證代號"],
            ev["權證名稱"],
            ev["標的股"],
            ev["買進日"],
            underlying_price,
            ma5_mark,
            ma20_mark,
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
        status_rows.append(["none"] * 19 + day_status)

        if ev["減碼獲利%"] is not None:
            # A 工作表第 15 欄為「減碼獲利%」
            exit_profit_result_cells.append((current_row_idx, 15, ev["減碼獲利%"]))

        if ev["出清獲利%"] is not None:
            # A 工作表第 18 欄為「出清獲利%」
            exit_profit_result_cells.append((current_row_idx, 18, ev["出清獲利%"]))

    col_widths = [18, 14, 10, 22, 8, 12, 12, 8, 8, 10, 12, 10, 12, 10, 12, 12, 10, 12, 10] + [16] * 20
    style_sheet(ws, col_widths, status_rows)

    for row_idx, col_idx, return_pct in exit_profit_result_cells:
        apply_exit_profit_result_outline(ws, row_idx, col_idx, return_pct)


def write_group_sheet(wb, sheet_name, events, price_cache, is_c=False):
    ws = wb.create_sheet(sheet_name)

    day_cols = [f"D+{i}" for i in range(1, 21)]

    if is_c:
        headers = [
            "事件類型", "分點", "標的股",
            "起始日", "結束日", "標的股股價", "5MA", "20MA",
            "涵蓋權證數", "權證清單",
            "買超金額", "買超張數",
            "減碼日", "減碼賣出金額", "減碼獲利%",
            "出清日", "出清賣出金額", "出清獲利%",
            "持有天數",
        ] + day_cols
        fixed_len = 19
    else:
        headers = [
            "事件類型", "分點", "標的股",
            "事件日", "標的股股價", "5MA", "20MA",
            "涵蓋權證數", "權證清單",
            "買超金額", "買超張數",
            "減碼日", "減碼賣出金額", "減碼獲利%",
            "出清日", "出清賣出金額", "出清獲利%",
            "持有天數",
        ] + day_cols
        fixed_len = 18

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
        ma_base_date = ev.get("結束日") if is_c else ev.get("事件日")
        underlying_price, ma5_mark, ma20_mark = get_ma_status_cells(
            price_cache,
            ev.get("標的股"),
            ma_base_date,
        )

        if is_c:
            row = [
                ev["事件類型"],
                ev["分點"],
                ev["標的股"],
                ev["起始日"],
                ev["結束日"],
                underlying_price,
                ma5_mark,
                ma20_mark,
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
                underlying_price,
                ma5_mark,
                ma20_mark,
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
            # B 工作表第 14 欄為「減碼獲利%」；C / D 工作表第 15 欄為「減碼獲利%」
            reduce_profit_col_idx = 15 if is_c else 14
            exit_profit_result_cells.append((current_row_idx, reduce_profit_col_idx, ev["減碼獲利%"]))

        if ev["出清獲利%"] is not None:
            # B 工作表第 17 欄為「出清獲利%」；C / D 工作表第 18 欄為「出清獲利%」
            exit_profit_col_idx = 18 if is_c else 17
            exit_profit_result_cells.append((current_row_idx, exit_profit_col_idx, ev["出清獲利%"]))

    if is_c:
        col_widths = [20, 14, 8, 12, 12, 12, 8, 8, 12, 45, 14, 10, 12, 14, 12, 12, 14, 12, 10] + [14] * 20
    else:
        col_widths = [20, 14, 8, 12, 12, 8, 8, 12, 45, 14, 10, 12, 14, 12, 12, 14, 12, 10] + [14] * 20

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

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            trade_dt = parse_date(row_dict["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_dates.add(normalize_date_str(row_dict["日期"]))

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

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            trade_dt = parse_date(row_dict["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_date_str = normalize_date_str(row_dict["日期"])
            buy_amount = int(row_dict["買進金額"])
            sell_amount = int(row_dict["賣出金額"])

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

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            trade_dt = parse_date(row_dict["日期"])

            if not trade_dt:
                continue

            if trade_dt < cutoff_dt:
                continue

            trade_date_str = normalize_date_str(row_dict["日期"])
            buy_amount = int(row_dict["買進金額"])
            sell_amount = int(row_dict["賣出金額"])
            buy_shares = int(row_dict["買進股數"])
            sell_shares = int(row_dict["賣出股數"])

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




def build_event_warrant_source_map(a_events, b_events, c_events, d_events):
    """
    建立「權證代號 -> A/B/C/D 事件來源」對照表。

    用途：
    1. 每日賣出明細直接來自原始分點歷史資料 items，不再用 A/B/C/D 工作表的減碼日推估。
    2. 若賣出的權證屬於 B / C / D 事件中的其中一檔，也能標示它原本歸屬哪一類事件。
    3. A > B > C > D 已在主流程做權證互斥，因此這裡只做標記，不改事件判斷邏輯。
    """
    source_map = {}

    def put_source(warrant_code, event_code, event_type, event_date, event_source):
        warrant_code = normalize_warrant_code_for_unique(warrant_code)

        if not warrant_code:
            return

        if warrant_code in source_map:
            return

        source_map[warrant_code] = {
            "事件": event_code,
            "事件類型": event_type,
            "事件日": normalize_date_str(event_date),
            "事件來源": event_source,
        }

    for ev in a_events:
        put_source(
            ev.get("權證代號", ""),
            "A",
            ev.get("事件類型", "A-單檔權證大買"),
            ev.get("事件日") or ev.get("買進日"),
            f'A | {ev.get("權證代號", "")} {ev.get("權證名稱", "")}',
        )

    for event_code, events in [
        ("B", b_events),
        ("C", c_events),
        ("D", d_events),
    ]:
        for ev in events:
            event_type = ev.get("事件類型", "")
            event_date = ev.get("事件日") or ev.get("結束日") or ev.get("起始日")

            for lot in ev.get("lots", []):
                put_source(
                    lot.get("權證代號", ""),
                    event_code,
                    event_type,
                    event_date,
                    f'{event_code} | {lot.get("權證代號", "")} {lot.get("權證名稱", "")}',
                )

    return source_map


def write_daily_sell_detail_sheet(wb, items, a_events, b_events, c_events, d_events):
    """
    新增「每日賣出明細」工作表。

    速度修正：
    只輸出最近 SELL_DETAIL_DAYS 天的賣出明細，避免每次把 250 天全歷史賣出資料整張寫入 Google Sheet。
    原始歷史快取與 A/B/C/D、近兩月排行、券商查詢仍照舊使用完整 items，不受影響。

    重要修正：
    1. 今日賣超圖片不應再用 A/B/C/D 工作表的「減碼日 / 出清日」去反推賣出金額。
       A 類如果只賣 1 張，不能用「減碼均價 × 原始買進張數」估算，否則會把金額放大。
    2. 本表直接使用原始分點歷史資料 item["df"] 的「賣出股數 / 賣出金額」，
       因此會與官方分點資料一致。
    3. B / C / D 事件中的其中一檔權證若發生賣出，也會被列出，不會被群組事件的
       第一個減碼日 / 出清日限制而漏掉。
    """
    ws = wb.create_sheet("每日賣出明細")

    headers = [
        "日期",
        "分點",
        "分點名稱",
        "券商代號",
        "事件",
        "狀態",
        "標的股",
        "標的名稱",
        "權證代號",
        "權證名稱",
        "賣出張數",
        "賣出股數",
        "賣出金額",
        "賣出均價",
        "事件日",
        "事件來源",
    ]
    ws.append(headers)

    source_map = build_event_warrant_source_map(a_events, b_events, c_events, d_events)
    rows = []
    cutoff_dt = datetime.today() - timedelta(days=max(SELL_DETAIL_DAYS - 1, 0))
    cutoff_dt = datetime(cutoff_dt.year, cutoff_dt.month, cutoff_dt.day)

    for item in items:
        df = item.get("df", pd.DataFrame())

        if df is None or df.empty:
            continue

        position = 0
        df2 = df.copy()
        df2["日期"] = df2["日期"].map(normalize_date_str)
        df2 = df2.sort_values("日期").reset_index(drop=True)

        for row in df2.itertuples(index=False):
            row_dict = row._asdict()
            date = normalize_date_str(row_dict.get("日期", ""))
            trade_dt = parse_date(date)
            buy_s = int(row_dict.get("買進股數", 0) or 0)
            sell_s = int(row_dict.get("賣出股數", 0) or 0)
            sell_a = int(row_dict.get("賣出金額", 0) or 0)

            before_position = position
            within_sell_detail_range = trade_dt is not None and trade_dt >= cutoff_dt

            if within_sell_detail_range and sell_s > 0 and sell_a > 0:
                if before_position > 0 and sell_s >= before_position:
                    status = "出清"
                elif before_position > 0:
                    status = "減碼"
                else:
                    status = "賣超"

                warrant_code = normalize_warrant_code_for_unique(item.get("warrant_code", ""))
                source = source_map.get(warrant_code, {})
                sell_avg = round(sell_a / sell_s, 4) if sell_s > 0 else ""

                rows.append({
                    "日期": date,
                    "分點": item.get("broker_label", ""),
                    "分點名稱": item.get("broker_name", ""),
                    "券商代號": item.get("broker_code", ""),
                    "事件": source.get("事件", "未歸類"),
                    "狀態": status,
                    "標的股": item.get("underlying_code", ""),
                    "標的名稱": item.get("underlying_name", ""),
                    "權證代號": item.get("warrant_code", ""),
                    "權證名稱": item.get("warrant_name", ""),
                    "賣出張數": sell_s // 1000,
                    "賣出股數": sell_s,
                    "賣出金額": sell_a,
                    "賣出均價": sell_avg,
                    "事件日": source.get("事件日", ""),
                    "事件來源": source.get("事件來源", ""),
                })

            # 權證不可當沖：同日賣出先扣舊庫存，再把當日買進加入庫存。
            if sell_s > 0:
                position = max(position - sell_s, 0)

            if buy_s > 0:
                position += buy_s

    rows = sorted(
        rows,
        key=lambda r: (
            -((parse_date(r.get("日期")) or datetime.min).toordinal()),
            str(r.get("分點", "")),
            str(r.get("標的股", "")),
            -int(r.get("賣出金額", 0) or 0),
        )
    )

    for r in rows:
        ws.append([r.get(h, "") for h in headers])

    col_widths = [12, 14, 18, 12, 8, 8, 10, 12, 12, 24, 10, 12, 16, 10, 12, 40]

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    fill_map = {
        "減碼": BLUE,
        "出清": ORANGE,
        "賣超": GREEN,
    }

    for row in ws.iter_rows(min_row=2):
        status = str(row[5].value or "").strip()
        row_fill = fill_map.get(status, WHITE)

        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
            cell.fill = row_fill

        ws.row_dimensions[row[0].row].height = 28

    ws.freeze_panes = "A2"



# ══════════════════════════════════════════════════════════════════════
# 近 10 日分點買賣明細快取：單一分點 + 標的股層級
# ══════════════════════════════════════════════════════════════════════

def _fmt_pct_text(value, signed=True):
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def _near10_window_dates(target_date=None, window_days=None):
    target_date = normalize_top15_target_date(target_date)
    target_dt = parse_date(target_date)

    if not target_dt:
        target_dt = datetime.today()
        target_date = target_dt.strftime("%Y/%m/%d")

    if window_days is None:
        window_days = BROKER_10D_DETAIL_DAYS

    window_days = max(int(window_days), 1)
    start_dt = target_dt - timedelta(days=window_days - 1)
    start_date = start_dt.strftime("%Y/%m/%d")
    period_text = f"{start_date} ～ {target_date}"
    return target_date, target_dt, start_date, start_dt, window_days, period_text


def _sell_needs_latest_price_fallback_for_item(item, start_dt, target_dt):
    """
    判斷近10日賣超是否真的需要最新權證價格作為成本備援。

    若 API5 歷史中已有賣出前的買進成本，賣超報酬可用 FIFO / 歷史均價估算，
    不需要再補抓最新權證價格；只有在賣出時完全沒有歷史買進成本可參考時，
    才補抓最新價作為備援，避免報酬率空白。
    """
    df = item.get("df", pd.DataFrame())
    if df is None or df.empty or "日期" not in df.columns:
        return False

    df = df.copy()
    df["dt_parsed"] = df["日期"].map(parse_date)
    df = df.dropna(subset=["dt_parsed"]).sort_values(["dt_parsed", "日期"]).reset_index(drop=True)

    historical_buy_qty = 0.0
    historical_buy_amount = 0.0

    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        dt = row_dict.get("dt_parsed")
        buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
        buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
        sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))
        sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))

        in_window = bool(dt and start_dt <= dt <= target_dt)

        # 權證不可當沖：同一天先賣舊庫存，因此這裡要在加入當日買進前先判斷賣出。
        if in_window and (sell_qty > 0 or sell_amount > 0):
            if historical_buy_qty <= 0 or historical_buy_amount <= 0:
                return True

        if buy_qty > 0 and buy_amount > 0:
            historical_buy_qty += buy_qty
            historical_buy_amount += buy_amount

    return False


def _collect_recent_warrant_codes_for_10d(items, target_date=None):
    target_date, target_dt, start_date, start_dt, window_days, period_text = _near10_window_dates(target_date)
    codes = set()
    skipped_no_remaining_position = 0
    skipped_sell_has_cost = 0

    for item in items or []:
        df = item.get("df", pd.DataFrame())
        if df is None or df.empty:
            continue

        warrant_code = normalize_warrant_code_for_unique(item.get("warrant_code", ""))
        if not warrant_code:
            continue

        recent_buy_amount = 0.0
        recent_sell_amount = 0.0
        recent_buy_qty = 0.0
        recent_sell_qty = 0.0

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            date_str = normalize_date_str(row_dict.get("日期", ""))
            dt = parse_date(date_str)

            if not dt or dt < start_dt or dt > target_dt:
                continue

            buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
            sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))
            buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
            sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))

            recent_buy_amount += buy_amount
            recent_sell_amount += sell_amount
            recent_buy_qty += buy_qty
            recent_sell_qty += sell_qty

        if recent_buy_amount <= 0 and recent_sell_amount <= 0 and recent_buy_qty <= 0 and recent_sell_qty <= 0:
            continue

        if BROKER_10D_FETCH_ALL_TRADED_WARRANT_PRICES:
            codes.add(warrant_code)
            continue

        # 買超報酬需要「近10日買進後仍未賣掉的部位」目前市值，這類權證一定要補最新價。
        if recent_buy_amount > 0 or recent_buy_qty > 0:
            position_summary = _recent_buy_position_summary_for_item(
                item,
                start_dt,
                target_dt,
                latest_price=None,
            )
            remaining_cost = top15_safe_float(position_summary.get("remaining_cost", 0.0))
            if remaining_cost > 0:
                codes.add(warrant_code)
            else:
                skipped_no_remaining_position += 1
            continue

        # 純賣超通常可用 API5 歷史 FIFO 成本估報酬，不需要市場最新價。
        # 只有完全找不到賣出前買進成本時，才補抓最新價作為備援。
        if recent_sell_amount > 0 or recent_sell_qty > 0:
            if BROKER_10D_FETCH_SELL_FALLBACK_PRICES and _sell_needs_latest_price_fallback_for_item(item, start_dt, target_dt):
                codes.add(warrant_code)
            else:
                skipped_sell_has_cost += 1

    print(
        f"  近10日分點明細價格篩選：需最新價 {len(codes):,} 檔｜"
        f"已排除無剩餘買超部位 {skipped_no_remaining_position:,} 檔｜"
        f"已排除可用歷史成本計算的純賣超 {skipped_sell_has_cost:,} 檔"
    )

    return codes

def ensure_broker_10d_warrant_prices(price_cache, items, target_date=None):
    """
    近10日分點買賣明細需要用「最新權證價格」估算買超部位放到現在的報酬。

    加速修正：
    1. 不再對近10日所有有買賣的權證一律抓價。
       - 近10日買進後仍有剩餘部位：一定補最新價。
       - 純賣超且 API5 歷史已有成本：不補最新價，直接用 FIFO / 歷史成本算報酬。
       - 純賣超但完全沒有歷史成本：才補最新價作為備援。
    2. 價格補抓採兩段式：先抓 BROKER_10D_PRICE_FAST_LOOKBACK_DAYS，完全沒價格才補抓完整 BROKER_10D_PRICE_LOOKBACK_DAYS。
    3. 本機價格快取完整保存；Google Sheet 價格快取可增量 append，避免整張重寫拖慢。
    """
    if not BROKER_10D_DETAIL_ENABLED:
        return price_cache

    codes = _collect_recent_warrant_codes_for_10d(items, target_date)
    if not codes:
        return price_cache

    target_date = normalize_top15_target_date(target_date)
    target_dt = parse_date(target_date) or datetime.today()
    end_dt = min(target_dt, datetime.today())

    full_lookback_days = max(int(BROKER_10D_PRICE_LOOKBACK_DAYS), 1)
    fast_lookback_days = max(int(BROKER_10D_PRICE_FAST_LOOKBACK_DAYS), 1)
    fast_lookback_days = min(fast_lookback_days, full_lookback_days)
    stale_days = max(int(BROKER_10D_PRICE_STALE_DAYS), 0)

    fast_start_dt = target_dt - timedelta(days=fast_lookback_days)
    full_start_dt = target_dt - timedelta(days=full_lookback_days)

    persistent_price_cache = load_price_cache()
    fetch_plan = {}

    for code in sorted(codes):
        cached_prices = get_cached_prices_for_code(persistent_price_cache, code)
        in_memory_prices = get_price_series_from_cache(price_cache, code)
        merged_cached = merge_price_dicts(cached_prices, in_memory_prices)

        if merged_cached:
            add_price_aliases(price_cache, code, merged_cached)

        latest_price, latest_date = get_latest_price_info_on_or_before(price_cache, code, target_date)
        latest_dt = parse_date(latest_date) if latest_date else None

        if latest_price is None or latest_dt is None or (target_dt - latest_dt).days > stale_days:
            fetch_plan[code] = (fast_start_dt, end_dt, full_start_dt)

    print(f"【Step 4d】近10日分點明細需檢查權證價格：{len(codes):,} 檔")
    print(f"  近10日分點明細需補抓權證價格：{len(fetch_plan):,} 檔")
    print(f"  近10日分點明細價格補抓策略：先抓近 {fast_lookback_days} 天，完全無價格才補抓近 {full_lookback_days} 天")

    if not fetch_plan:
        return price_cache

    def fetch_one(code):
        fast_sdt, edt, full_sdt = fetch_plan[code]
        fetched_prices = fetch_twse_prices(code, fast_sdt, edt)

        # 若短區間完全沒有價格，而且本地快取也沒有任何可用價格，再補抓完整區間。
        old_prices = get_cached_prices_for_code(persistent_price_cache, code)
        merged_after_fast = merge_price_dicts(old_prices, fetched_prices)
        has_any_price = any(
            p is not None and p > 0
            for p in merged_after_fast.values()
        )

        if not has_any_price and full_sdt < fast_sdt:
            full_prices = fetch_twse_prices(code, full_sdt, edt)
            fetched_prices = merge_price_dicts(fetched_prices, full_prices)

        return code, fetched_prices

    done = 0
    changed_price_codes = set()
    with ThreadPoolExecutor(max_workers=PRICE_WORKERS) as ex:
        futures = {ex.submit(fetch_one, code): code for code in fetch_plan}

        for future in as_completed(futures):
            done += 1
            code = futures[future]

            try:
                _, fetched_prices = future.result()
            except Exception:
                fetched_prices = {}

            old_prices = get_cached_prices_for_code(persistent_price_cache, code)
            merged_prices = merge_price_dicts(old_prices, fetched_prices)

            if merged_prices:
                norm_code = normalize_price_code(code)
                if norm_code:
                    persistent_price_cache[norm_code] = merged_prices
                    if fetched_prices:
                        changed_price_codes.add(norm_code)
                add_price_aliases(price_cache, code, merged_prices)

            if done % 20 == 0:
                print(f"  [{done}/{len(fetch_plan)}] 近10日分點明細權證價格補抓中...")

    save_price_cache(persistent_price_cache, changed_codes=changed_price_codes)
    return price_cache

def _sell_return_summary_for_item(item, start_dt, target_dt, fallback_price=None):
    """
    用同一分點 + 同一權證的 API5 歷史資料估算近10日賣出實現報酬。

    規則：
    - 權證不可當沖：同一天先賣舊庫存，再把當日買進加入庫存。
    - 賣出成本優先用 FIFO 持倉成本估算。
    - 若近10日賣出找不到足夠舊庫存成本，改用可取得的成本備援估算，避免賣超報酬率空白：
      1. 優先用該權證歷史已出現買進均價
      2. 其次用最新權證價格
      3. 最後用當筆賣出均價，讓報酬率保守落在 0%
    """
    df = item.get("df", pd.DataFrame())
    if df is None or df.empty:
        return {
            "revenue": 0.0,
            "cost": 0.0,
            "unmatched_amount": 0.0,
            "unmatched_qty": 0.0,
        }

    df = df.copy()
    if "日期" not in df.columns:
        return {
            "revenue": 0.0,
            "cost": 0.0,
            "unmatched_amount": 0.0,
            "unmatched_qty": 0.0,
        }

    df["dt_parsed"] = df["日期"].map(parse_date)
    df = df.dropna(subset=["dt_parsed"]).sort_values(["dt_parsed", "日期"]).reset_index(drop=True)

    lots = []
    revenue = 0.0
    cost = 0.0
    unmatched_amount = 0.0
    unmatched_qty = 0.0

    historical_buy_amount = 0.0
    historical_buy_qty = 0.0

    try:
        fallback_price = float(fallback_price) if fallback_price is not None else None
    except Exception:
        fallback_price = None

    if fallback_price is not None and fallback_price <= 0:
        fallback_price = None

    def fallback_unit_cost(sell_price):
        if historical_buy_qty > 0 and historical_buy_amount > 0:
            return historical_buy_amount / historical_buy_qty
        if fallback_price is not None and fallback_price > 0:
            return fallback_price
        if sell_price and sell_price > 0:
            return sell_price
        return None

    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        dt = row_dict.get("dt_parsed")
        date_str = normalize_date_str(row_dict.get("日期", ""))
        buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
        sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))
        buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
        sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))

        in_window = bool(dt and start_dt <= dt <= target_dt)

        # 權證不能當沖：同一天先處理賣出，只能扣舊庫存。
        if sell_amount > 0:
            if sell_qty <= 0:
                # API 異常時仍保留數據，不讓近10日賣超報酬率變空白。
                if in_window:
                    revenue += sell_amount
                    cost += sell_amount
                sell_qty = 0
            else:
                sell_price = sell_amount / sell_qty
                sell_left = sell_qty
                allocated_revenue = 0.0
                allocated_cost = 0.0

                for lot in lots:
                    if sell_left <= 0:
                        break
                    if lot.get("剩餘股數", 0) <= 0:
                        continue

                    alloc = min(sell_left, lot["剩餘股數"])
                    if alloc <= 0:
                        continue

                    lot["剩餘股數"] -= alloc
                    sell_left -= alloc
                    allocated_revenue += alloc * sell_price
                    allocated_cost += alloc * lot["均價"]

                if sell_left > 0:
                    fallback_cost_price = fallback_unit_cost(sell_price)
                    if fallback_cost_price is not None and fallback_cost_price > 0:
                        allocated_revenue += sell_left * sell_price
                        allocated_cost += sell_left * fallback_cost_price
                    else:
                        unmatched_qty += sell_left
                        unmatched_amount += sell_left * sell_price

                if in_window:
                    revenue += allocated_revenue
                    cost += allocated_cost

        # 同日買進放到賣出後面，避免當沖錯扣。
        if buy_qty > 0 and buy_amount > 0:
            lots.append({
                "買進日": date_str,
                "股數": buy_qty,
                "剩餘股數": buy_qty,
                "金額": buy_amount,
                "均價": buy_amount / buy_qty if buy_qty else 0,
            })
            historical_buy_qty += buy_qty
            historical_buy_amount += buy_amount

    return {
        "revenue": revenue,
        "cost": cost,
        "unmatched_amount": unmatched_amount,
        "unmatched_qty": unmatched_qty,
    }


def _recent_buy_position_summary_for_item(item, start_dt, target_dt, latest_price=None):
    """
    計算近10日買進 lot 在統計日仍然留下的真實 FIFO 持倉。

    這個函式用完整歷史跑到統計日：
    - 每天先賣出扣 FIFO 舊庫存，再加入當天買進。
    - 只把「買進日落在近10日視窗內」且統計日尚未被賣掉的剩餘 lot 納入買超持倉報酬。
    - 買超報酬 = (有最新價格的剩餘股數 × 最新權證價格 - 有最新價格的剩餘成本) / 有最新價格的剩餘成本。
    - 缺最新權證價格的剩餘部位不納入報酬率，改在明細備註統計省略檔數。
    """
    df = item.get("df", pd.DataFrame())
    if df is None or df.empty:
        return {
            "remaining_qty": 0.0,
            "remaining_cost": 0.0,
            "market_value": 0.0,
            "missing_price": False,
        }

    df = df.copy()
    if "日期" not in df.columns:
        return {
            "remaining_qty": 0.0,
            "remaining_cost": 0.0,
            "market_value": 0.0,
            "missing_price": False,
        }

    df["dt_parsed"] = df["日期"].map(parse_date)
    df = df.dropna(subset=["dt_parsed"])
    df = df[df["dt_parsed"] <= target_dt].sort_values(["dt_parsed", "日期"]).reset_index(drop=True)

    lots = []

    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        dt = row_dict.get("dt_parsed")
        date_str = normalize_date_str(row_dict.get("日期", ""))
        buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
        sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))
        buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
        sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))

        # 權證不能當沖：同一天先賣出，只扣舊庫存。
        if sell_qty > 0 and sell_amount > 0:
            sell_left = sell_qty
            for lot in lots:
                if sell_left <= 0:
                    break
                if lot.get("剩餘股數", 0) <= 0:
                    continue

                alloc = min(sell_left, lot["剩餘股數"])
                if alloc <= 0:
                    continue

                lot["剩餘股數"] -= alloc
                sell_left -= alloc

        if buy_qty > 0 and buy_amount > 0:
            lots.append({
                "買進日": date_str,
                "股數": buy_qty,
                "剩餘股數": buy_qty,
                "金額": buy_amount,
                "均價": buy_amount / buy_qty if buy_qty else 0,
                "近10日買進": bool(dt and start_dt <= dt <= target_dt),
            })

    remaining_qty = 0.0
    remaining_cost = 0.0

    for lot in lots:
        if not lot.get("近10日買進"):
            continue
        qty = top15_safe_float(lot.get("剩餘股數", 0))
        avg = top15_safe_float(lot.get("均價", 0))
        if qty <= 0 or avg <= 0:
            continue
        remaining_qty += qty
        remaining_cost += qty * avg

    try:
        latest_price = float(latest_price) if latest_price is not None else None
    except Exception:
        latest_price = None

    if latest_price is not None and latest_price > 0 and remaining_qty > 0:
        market_value = remaining_qty * latest_price
        missing_price = False
    else:
        market_value = 0.0
        missing_price = remaining_cost > 0

    return {
        "remaining_qty": remaining_qty,
        "remaining_cost": remaining_cost,
        "market_value": market_value,
        "missing_price": missing_price,
    }


def build_10d_broker_underlying_detail_rows(items, price_cache, target_date=None):
    """
    建立「快取_近10日分點買賣明細」。

    統計單位：資料範圍 + 統計日期 + 單一分點 + 標的股。
    同一分點同一標的底下的所有權證會先完整合併，再計算買賣金額、買超 / 賣超報酬與勝率。
    """
    if not BROKER_10D_DETAIL_ENABLED:
        return None

    print("【Step 4e】建立近10日分點買賣明細快取...")

    if not items:
        print("  ⚠️ 近10日分點買賣明細：沒有 items 資料")
        return []

    target_date, target_dt, start_date, start_dt, window_days, period_text = _near10_window_dates(target_date)
    update_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    scope = get_result_data_scope()

    agg = {}

    for item in items:
        df = item.get("df", pd.DataFrame())
        if df is None or df.empty:
            continue

        warrant_code = normalize_warrant_code_for_unique(item.get("warrant_code", ""))
        if not warrant_code:
            continue

        warrant_name = str(item.get("warrant_name", "")).strip()
        underlying_code = str(item.get("underlying_code", "")).strip()
        underlying_name = str(item.get("underlying_name", "")).strip()
        broker_label = str(item.get("broker_label", "")).strip()
        broker_name = str(item.get("broker_name", "")).strip()
        broker_code = str(item.get("broker_code", "")).strip()

        if not underlying_code or not broker_label:
            continue

        key = (broker_label, broker_name, broker_code, underlying_code)
        rec = agg.setdefault(key, {
            "資料範圍": scope,
            "統計日期": add_gsheet_text_prefix(target_date),
            "統計期間": period_text,
            "統計天數": window_days,
            "第一筆日期": "",
            "最後筆日期": "",
            "分點": broker_label,
            "分點名稱": broker_name,
            "券商代號": broker_code,
            "標的股": underlying_code,
            "標的名稱": underlying_name,
            "近10日買進股數": 0.0,
            "近10日買進金額": 0.0,
            "近10日賣出股數": 0.0,
            "近10日賣出金額": 0.0,
            "日期集合": set(),
            "權證": {},
            "買超報酬加權分子": 0.0,
            "買超報酬權重": 0.0,
            "買超剩餘股數": 0.0,
            "買超剩餘成本": 0.0,
            "買超目前市值": 0.0,
            "買超報酬有效權證數": 0,
            "買超報酬缺價權證數": 0,
            "買超缺價省略權證清單": [],
            "賣超實現賣出金額": 0.0,
            "賣超實現成本": 0.0,
            "賣超成本不足金額": 0.0,
            "賣超成本不足股數": 0.0,
            "更新時間": update_time,
            "run_id": run_id,
        })

        if underlying_name and not rec.get("標的名稱"):
            rec["標的名稱"] = underlying_name

        item_buy_qty = 0.0
        item_buy_amount = 0.0
        item_sell_qty = 0.0
        item_sell_amount = 0.0
        item_dates = set()

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            date_str = normalize_date_str(row_dict.get("日期", ""))
            dt = parse_date(date_str)

            if not dt or dt < start_dt or dt > target_dt:
                continue

            buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
            sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))
            buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
            sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))

            if buy_qty <= 0 and sell_qty <= 0 and buy_amount <= 0 and sell_amount <= 0:
                continue

            rec["近10日買進股數"] += buy_qty
            rec["近10日買進金額"] += buy_amount
            rec["近10日賣出股數"] += sell_qty
            rec["近10日賣出金額"] += sell_amount
            rec["日期集合"].add(date_str)

            item_buy_qty += buy_qty
            item_buy_amount += buy_amount
            item_sell_qty += sell_qty
            item_sell_amount += sell_amount
            item_dates.add(date_str)

        if item_buy_amount <= 0 and item_sell_amount <= 0:
            continue

        warrant_rec = rec["權證"].setdefault(warrant_code, {
            "權證代號": warrant_code,
            "權證名稱": warrant_name,
            "買進金額": 0.0,
            "賣出金額": 0.0,
            "買進股數": 0.0,
            "賣出股數": 0.0,
            "日期集合": set(),
            "最新權證價格": None,
            "最新權證價格日": "",
        })
        warrant_rec["買進金額"] += item_buy_amount
        warrant_rec["賣出金額"] += item_sell_amount
        warrant_rec["買進股數"] += item_buy_qty
        warrant_rec["賣出股數"] += item_sell_qty
        warrant_rec["日期集合"].update(item_dates)

        latest_price, latest_price_date = get_latest_price_info_on_or_before(price_cache, warrant_code, target_date)
        if latest_price is not None:
            warrant_rec["最新權證價格"] = latest_price
            warrant_rec["最新權證價格日"] = latest_price_date

        if item_buy_qty > 0 and item_buy_amount > 0:
            position_summary = _recent_buy_position_summary_for_item(
                item,
                start_dt,
                target_dt,
                latest_price=latest_price,
            )
            remaining_cost = top15_safe_float(position_summary.get("remaining_cost", 0.0))
            remaining_qty = top15_safe_float(position_summary.get("remaining_qty", 0.0))
            market_value = top15_safe_float(position_summary.get("market_value", 0.0))

            if remaining_cost > 0:
                if position_summary.get("missing_price"):
                    # 缺最新權證價格的部位直接從買超平均報酬分子 / 分母排除，
                    # 避免用 0 市值把整體報酬率不合理壓低。
                    rec["買超報酬缺價權證數"] += 1
                    rec.setdefault("買超缺價省略權證清單", []).append(f"{warrant_code} {warrant_name}".strip())
                    warrant_rec["買超報酬省略原因"] = "缺最新權證價格，未納入買超平均報酬"
                else:
                    rec["買超剩餘成本"] += remaining_cost
                    rec["買超剩餘股數"] += remaining_qty
                    rec["買超目前市值"] += market_value
                    rec["買超報酬權重"] += remaining_cost
                    rec["買超報酬加權分子"] += market_value - remaining_cost
                    rec["買超報酬有效權證數"] += 1

            warrant_rec["近10日買進剩餘股數"] = remaining_qty
            warrant_rec["近10日買進剩餘成本"] = remaining_cost
            warrant_rec["近10日買進目前市值"] = market_value

        if item_sell_qty > 0 and item_sell_amount > 0:
            sell_summary = _sell_return_summary_for_item(
                item,
                start_dt,
                target_dt,
                fallback_price=latest_price,
            )
            rec["賣超實現賣出金額"] += sell_summary.get("revenue", 0.0)
            rec["賣超實現成本"] += sell_summary.get("cost", 0.0)
            rec["賣超成本不足金額"] += sell_summary.get("unmatched_amount", 0.0)
            rec["賣超成本不足股數"] += sell_summary.get("unmatched_qty", 0.0)

    rows = []

    for rec in agg.values():
        dates = sorted(rec.get("日期集合", set()))
        if not dates:
            continue

        buy_amount = float(rec.get("近10日買進金額", 0) or 0)
        sell_amount = float(rec.get("近10日賣出金額", 0) or 0)
        buy_qty = float(rec.get("近10日買進股數", 0) or 0)
        sell_qty = float(rec.get("近10日賣出股數", 0) or 0)
        net_buy_amount = buy_amount - sell_amount
        net_sell_amount = sell_amount - buy_amount
        net_buy_qty = buy_qty - sell_qty
        net_sell_qty = sell_qty - buy_qty

        buy_return_pct = None
        buy_return_weight = float(rec.get("買超報酬權重", 0) or 0)
        buy_return_numerator = float(rec.get("買超報酬加權分子", 0) or 0)

        if buy_return_weight > 0:
            # 買超平均報酬只使用「有最新價格」的剩餘部位；缺價部位已在上方省略。
            buy_return_pct = buy_return_numerator / buy_return_weight * 100
        elif buy_amount > 0:
            # 近10日有買進但沒有任何可估剩餘部位時，仍給出 0.00%，避免勝率報酬空白。
            buy_return_pct = 0.0

        sell_return_pct = None
        sell_realized_cost = float(rec.get("賣超實現成本", 0) or 0)
        sell_realized_revenue = float(rec.get("賣超實現賣出金額", 0) or 0)

        if sell_realized_cost > 0:
            sell_return_pct = (sell_realized_revenue - sell_realized_cost) / sell_realized_cost * 100
        elif sell_amount > 0:
            # API 賣出資料異常或成本仍不足時，保守帶入 0.00%，確保賣超報酬率一定有值。
            sell_return_pct = 0.0

        if net_buy_amount > 0:
            direction = "買超"
            win_return_pct = buy_return_pct
        elif net_sell_amount > 0:
            direction = "賣超"
            win_return_pct = sell_return_pct
        else:
            direction = "買賣平衡"
            win_return_pct = buy_return_pct if buy_return_pct is not None else sell_return_pct

        if win_return_pct is None:
            win_return_pct = 0.0

        # 分點層級加權報酬使用「主要方向淨額」作為權重：
        # - 買超標的：使用近10日淨買超金額
        # - 賣超標的：使用近10日淨賣超金額
        # - 買賣平衡：退回使用買進 / 賣出金額較大者
        # 這樣可以避免單純平均讓小金額標的過度影響整體分點表現。
        primary_return_weight = net_buy_amount if direction == "買超" else net_sell_amount if direction == "賣超" else max(buy_amount, sell_amount)
        if primary_return_weight <= 0:
            primary_return_weight = max(buy_amount, sell_amount, 0.0)

        broker_buy_return_weight = net_buy_amount if net_buy_amount > 0 else 0.0
        broker_sell_return_weight = net_sell_amount if net_sell_amount > 0 else 0.0

        if win_return_pct > 0:
            result = "勝"
        else:
            # 使用者指定 0% 要算賠錢，所以 <= 0 都是敗。
            result = "敗"

        notes = []
        missing_price_count = int(rec.get("買超報酬缺價權證數", 0) or 0)

        if missing_price_count > 0:
            notes.append(f"買超報酬已省略缺價權證 {missing_price_count} 檔")

        if buy_amount > 0 and buy_return_weight <= 0:
            if missing_price_count > 0:
                notes.append("買超剩餘部位全數缺價或無可估價格，買超報酬以 0.00% 保守帶入")
            else:
                notes.append("近10日買進目前無可估剩餘部位，買超報酬以 0.00% 帶入")

        if sell_amount > 0 and sell_realized_cost <= 0:
            notes.append("賣超成本不足，賣超報酬以 0.00% 帶入")

        note_text = "；".join(notes)

        warrant_rows = []
        warrant_json = []
        for w in sorted(
            rec.get("權證", {}).values(),
            key=lambda x: (float(x.get("買進金額", 0) or 0) + float(x.get("賣出金額", 0) or 0)),
            reverse=True,
        ):
            warrant_rows.append(
                f"{w.get('權證代號', '')} {w.get('權證名稱', '')}"
                f"｜買{_fmt_wan_text(w.get('買進金額', 0))}／賣{_fmt_wan_text(w.get('賣出金額', 0))}"
            )
            warrant_json.append({
                "權證代號": w.get("權證代號", ""),
                "權證名稱": w.get("權證名稱", ""),
                "買進金額": round(float(w.get("買進金額", 0) or 0), 0),
                "賣出金額": round(float(w.get("賣出金額", 0) or 0), 0),
                "買進股數": round(float(w.get("買進股數", 0) or 0), 0),
                "賣出股數": round(float(w.get("賣出股數", 0) or 0), 0),
                "最新權證價格": w.get("最新權證價格"),
                "最新權證價格日": w.get("最新權證價格日", ""),
                "近10日買進剩餘股數": round(float(w.get("近10日買進剩餘股數", 0) or 0), 0),
                "近10日買進剩餘成本": round(float(w.get("近10日買進剩餘成本", 0) or 0), 0),
                "近10日買進目前市值": round(float(w.get("近10日買進目前市值", 0) or 0), 0),
                "買超報酬省略原因": w.get("買超報酬省略原因", ""),
                "日期數": len(w.get("日期集合", set())),
            })

        rows.append({
            "資料範圍": rec.get("資料範圍", scope),
            "統計日期": add_gsheet_text_prefix(target_date),
            "統計期間": period_text,
            "統計天數": window_days,
            "有效日期數": len(dates),
            "第一筆日期": add_gsheet_text_prefix(dates[0]),
            "最後筆日期": add_gsheet_text_prefix(dates[-1]),
            "分點": rec.get("分點", ""),
            "分點名稱": rec.get("分點名稱", ""),
            "券商代號": rec.get("券商代號", ""),
            "標的股": rec.get("標的股", ""),
            "標的名稱": rec.get("標的名稱", ""),
            "買賣方向": direction,
            "近10日買進股數": round(buy_qty, 0),
            "近10日買進金額": round(buy_amount, 0),
            "近10日賣出股數": round(sell_qty, 0),
            "近10日賣出金額": round(sell_amount, 0),
            "近10日淨買超股數": round(net_buy_qty, 0),
            "近10日淨買超金額": round(net_buy_amount, 0),
            "近10日淨賣超股數": round(net_sell_qty, 0),
            "近10日淨賣超金額": round(net_sell_amount, 0),
            "涉及權證檔數": len(rec.get("權證", {})),
            "權證清單": "；".join(warrant_rows),
            "買超平均報酬%": _fmt_pct_text(buy_return_pct, signed=True),
            "買超剩餘股數": round(float(rec.get("買超剩餘股數", 0) or 0), 0),
            "買超剩餘成本": round(float(rec.get("買超剩餘成本", 0) or 0), 0),
            "買超目前市值": round(float(rec.get("買超目前市值", 0) or 0), 0),
            "買超報酬有效權證數": int(rec.get("買超報酬有效權證數", 0) or 0),
            "買超報酬缺價權證數": int(rec.get("買超報酬缺價權證數", 0) or 0),
            "賣超平均報酬%": _fmt_pct_text(sell_return_pct, signed=True),
            "賣超實現賣出金額": round(float(rec.get("賣超實現賣出金額", 0) or 0), 0),
            "賣超實現成本": round(float(rec.get("賣超實現成本", 0) or 0), 0),
            "賣超成本不足金額": round(float(rec.get("賣超成本不足金額", 0) or 0), 0),
            "用於勝率報酬%": _fmt_pct_text(win_return_pct, signed=True),
            "判定": result,
            "備註": note_text,
            "分點近10日勝率": "-",
            "分點近10日勝筆數": 0,
            "分點近10日敗筆數": 0,
            "分點近10日加權平均報酬%": "-",
            "分點近10日買超加權平均報酬%": "-",
            "分點近10日賣超加權平均報酬%": "-",
            "分點近10日加權平均勝報酬%": "-",
            "分點近10日加權平均敗報酬%": "-",
            "分點近10日盈虧比": "-",
            "分點近10日加權期望值%": "-",
            "分點近10日加權報酬權重金額": 0,
            "更新時間": rec.get("更新時間", update_time),
            "run_id": rec.get("run_id", run_id),
            "權證明細_JSON": json.dumps(warrant_json, ensure_ascii=False),
            "_分點統計報酬率數值": win_return_pct,
            "_分點統計權重": primary_return_weight,
            "_分點統計買超報酬率數值": buy_return_pct,
            "_分點統計買超權重": broker_buy_return_weight,
            "_分點統計賣超報酬率數值": sell_return_pct,
            "_分點統計賣超權重": broker_sell_return_weight,
        })

    broker_stats = {}
    for row in rows:
        broker_key = (row.get("分點", ""), row.get("券商代號", ""))
        stat = broker_stats.setdefault(broker_key, {
            "win": 0,
            "loss": 0,
            "return_weighted_numerator": 0.0,
            "return_weight": 0.0,
            "buy_return_weighted_numerator": 0.0,
            "buy_return_weight": 0.0,
            "sell_return_weighted_numerator": 0.0,
            "sell_return_weight": 0.0,
            "win_return_weighted_numerator": 0.0,
            "win_return_weight": 0.0,
            "loss_return_weighted_numerator": 0.0,
            "loss_return_weight": 0.0,
        })

        if row.get("判定") == "勝":
            stat["win"] += 1
        elif row.get("判定") == "敗":
            stat["loss"] += 1

        ret = row.get("_分點統計報酬率數值")
        weight = top15_safe_float(row.get("_分點統計權重", 0), 0.0)

        if ret is not None and weight > 0:
            ret = top15_safe_float(ret, None)
            if ret is not None:
                stat["return_weighted_numerator"] += ret * weight
                stat["return_weight"] += weight

                if ret > 0:
                    stat["win_return_weighted_numerator"] += ret * weight
                    stat["win_return_weight"] += weight
                else:
                    stat["loss_return_weighted_numerator"] += ret * weight
                    stat["loss_return_weight"] += weight

        buy_ret = row.get("_分點統計買超報酬率數值")
        buy_weight = top15_safe_float(row.get("_分點統計買超權重", 0), 0.0)
        if buy_ret is not None and buy_weight > 0:
            buy_ret = top15_safe_float(buy_ret, None)
            if buy_ret is not None:
                stat["buy_return_weighted_numerator"] += buy_ret * buy_weight
                stat["buy_return_weight"] += buy_weight

        sell_ret = row.get("_分點統計賣超報酬率數值")
        sell_weight = top15_safe_float(row.get("_分點統計賣超權重", 0), 0.0)
        if sell_ret is not None and sell_weight > 0:
            sell_ret = top15_safe_float(sell_ret, None)
            if sell_ret is not None:
                stat["sell_return_weighted_numerator"] += sell_ret * sell_weight
                stat["sell_return_weight"] += sell_weight

    for row in rows:
        broker_key = (row.get("分點", ""), row.get("券商代號", ""))
        stat = broker_stats.get(broker_key, {
            "win": 0,
            "loss": 0,
            "return_weighted_numerator": 0.0,
            "return_weight": 0.0,
            "buy_return_weighted_numerator": 0.0,
            "buy_return_weight": 0.0,
            "sell_return_weighted_numerator": 0.0,
            "sell_return_weight": 0.0,
            "win_return_weighted_numerator": 0.0,
            "win_return_weight": 0.0,
            "loss_return_weighted_numerator": 0.0,
            "loss_return_weight": 0.0,
        })

        total = stat["win"] + stat["loss"]
        win_rate_value = stat["win"] / total * 100 if total else None

        avg_return = (
            stat["return_weighted_numerator"] / stat["return_weight"]
            if stat["return_weight"] > 0 else None
        )
        avg_buy_return = (
            stat["buy_return_weighted_numerator"] / stat["buy_return_weight"]
            if stat["buy_return_weight"] > 0 else None
        )
        avg_sell_return = (
            stat["sell_return_weighted_numerator"] / stat["sell_return_weight"]
            if stat["sell_return_weight"] > 0 else None
        )
        avg_win_return = (
            stat["win_return_weighted_numerator"] / stat["win_return_weight"]
            if stat["win_return_weight"] > 0 else None
        )
        avg_loss_return = (
            stat["loss_return_weighted_numerator"] / stat["loss_return_weight"]
            if stat["loss_return_weight"] > 0 else None
        )

        profit_loss_ratio = None
        if avg_win_return is not None and avg_win_return > 0 and avg_loss_return is not None and avg_loss_return < 0:
            profit_loss_ratio = avg_win_return / abs(avg_loss_return)

        expectancy = None
        if total > 0 and (avg_win_return is not None or avg_loss_return is not None):
            win_rate_decimal = stat["win"] / total
            loss_rate_decimal = stat["loss"] / total
            expectancy = win_rate_decimal * (avg_win_return or 0.0) + loss_rate_decimal * (avg_loss_return or 0.0)
        elif avg_return is not None:
            expectancy = avg_return

        row["分點近10日勝筆數"] = stat["win"]
        row["分點近10日敗筆數"] = stat["loss"]
        row["分點近10日勝率"] = _fmt_pct_text(win_rate_value, signed=False) if win_rate_value is not None else "-"
        row["分點近10日加權平均報酬%"] = _fmt_pct_text(avg_return, signed=True)
        row["分點近10日買超加權平均報酬%"] = _fmt_pct_text(avg_buy_return, signed=True)
        row["分點近10日賣超加權平均報酬%"] = _fmt_pct_text(avg_sell_return, signed=True)
        row["分點近10日加權平均勝報酬%"] = _fmt_pct_text(avg_win_return, signed=True)
        row["分點近10日加權平均敗報酬%"] = _fmt_pct_text(avg_loss_return, signed=True)
        row["分點近10日盈虧比"] = f"{profit_loss_ratio:.2f}" if profit_loss_ratio is not None else "-"
        row["分點近10日加權期望值%"] = _fmt_pct_text(expectancy, signed=True)
        row["分點近10日加權報酬權重金額"] = round(float(stat.get("return_weight", 0.0) or 0.0), 0)

    rows = sorted(
        rows,
        key=lambda r: (
            str(r.get("分點", "")),
            -abs(float(r.get("近10日淨買超金額", 0) or 0)),
            str(r.get("標的股", "")),
        )
    )

    print(f"  ✅ 近10日分點買賣明細完成：{len(rows):,} 筆，統計期間 {period_text}")
    return rows


def write_10d_broker_underlying_detail_sheet(wb, rows):
    """寫入近10日分點買賣明細。rows=None 代表不建立工作表。"""
    if rows is None:
        return

    ws = wb.create_sheet(BROKER_10D_DETAIL_SHEET)

    headers = [
        "資料範圍", "統計日期", "統計期間", "統計天數", "有效日期數", "第一筆日期", "最後筆日期",
        "分點", "分點名稱", "券商代號", "標的股", "標的名稱", "買賣方向",
        "近10日買進股數", "近10日買進金額", "近10日賣出股數", "近10日賣出金額",
        "近10日淨買超股數", "近10日淨買超金額", "近10日淨賣超股數", "近10日淨賣超金額",
        "涉及權證檔數", "權證清單",
        "買超平均報酬%", "買超剩餘股數", "買超剩餘成本", "買超目前市值", "買超報酬有效權證數", "買超報酬缺價權證數",
        "賣超平均報酬%", "賣超實現賣出金額", "賣超實現成本", "賣超成本不足金額",
        "用於勝率報酬%", "判定", "備註", "分點近10日勝率", "分點近10日勝筆數", "分點近10日敗筆數",
        "分點近10日加權平均報酬%", "分點近10日買超加權平均報酬%", "分點近10日賣超加權平均報酬%",
        "分點近10日加權平均勝報酬%", "分點近10日加權平均敗報酬%", "分點近10日盈虧比",
        "分點近10日加權期望值%", "分點近10日加權報酬權重金額",
        "更新時間", "run_id", "權證明細_JSON",
    ]

    ws.append(headers)

    for row in rows or []:
        ws.append([row.get(h, "") for h in headers])

    col_widths = [
        12, 12, 24, 10, 12, 12, 12,
        14, 18, 12, 10, 14, 10,
        14, 16, 14, 16,
        16, 18, 16, 18,
        12, 72,
        14, 14, 16, 16, 14, 14,
        14, 16, 16, 16,
        14, 10, 36, 14, 14, 14,
        16, 20, 20, 18, 18, 12, 16, 18,
        20, 18, 90,
    ]

    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 26

    header_map = {str(cell.value).strip(): idx + 1 for idx, cell in enumerate(ws[1])}
    direction_col = header_map.get("買賣方向")

    for row in ws.iter_rows(min_row=2):
        direction = str(row[direction_col - 1].value or "").strip() if direction_col else ""
        row_fill = RED if direction == "買超" else GREEN if direction == "賣超" else WHITE

        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
            cell.fill = row_fill

        ws.row_dimensions[row[0].row].height = 32

    ws.freeze_panes = "A2"


# ══════════════════════════════════════════════════════════════════════
# 近 7 日權證分點共識買賣超 TOP15（僅 RUN_MODE=2 更新）
# ══════════════════════════════════════════════════════════════════════

def _fmt_wan_text(value):
    try:
        return f"{float(value) / 10000:.1f}萬"
    except Exception:
        return "0.0萬"


def build_7d_warrant_consensus_top15_rows(items, target_date=None):
    """
    建立「快取_近7日權證分點共識TOP15」。

    重要規則：
    1. 只在 RUN_MODE=2 完整分點清單模式執行。
    2. RUN_MODE=1 精選分點模式直接回傳 None，build_excel 不會建立該工作表，
       因此 upload_excel_to_google_sheet() 也不會更新 / 清空 Google Sheet 上原本的同名工作表。
    3. 統計來源為本次已抓到並還原的 items 分點歷史資料。
    4. 主程式先用「標的股」層級完整合併同標的全部權證，再排序取 TOP15。
    5. 共識買超 TOP15：買進金額 - 賣出金額 > 0，依淨買超金額排序。
    6. 共識賣超 TOP15：賣出金額 - 買進金額 > 0，依淨賣超金額排序。
    """
    if not WARRANT_CONSENSUS_7D_ENABLED:
        return None

    if RUN_MODE != 2:
        print("  ✅ RUN_MODE=1 精選分點模式：略過近7日權證分點共識TOP15工作表，避免動到 Google Sheet 既有資料。")
        return None

    print("【Step 4c】建立近7日權證分點共識買賣超 TOP15（標的層級，RUN_MODE=2 專用）...")

    if not items:
        print("  ⚠️ 近7日權證分點共識TOP15：沒有 items 資料")
        return []

    target_date = normalize_top15_target_date(target_date)
    target_dt = parse_date(target_date)

    if not target_dt:
        target_dt = datetime.today()
        target_date = target_dt.strftime("%Y/%m/%d")

    window_days = max(int(WARRANT_CONSENSUS_7D_DAYS), 1)
    top_n = max(int(WARRANT_CONSENSUS_7D_TOP_N), 1)
    start_dt = target_dt - timedelta(days=window_days - 1)
    start_date = start_dt.strftime("%Y/%m/%d")
    period_text = f"{start_date} ～ {target_date}"
    update_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    scope = get_result_data_scope()

    # 核心修正：group_key = 標的股。
    # 同標的底下所有權證、所有追蹤分點先完整加總，再做 TOP15 排名。
    agg = {}

    for item in items:
        df = item.get("df", pd.DataFrame())

        if df is None or df.empty:
            continue

        warrant_code = normalize_warrant_code_for_unique(item.get("warrant_code", ""))
        if not warrant_code:
            continue

        warrant_name = str(item.get("warrant_name", "")).strip()
        raw_underlying_code = str(item.get("underlying_code", "")).strip()
        underlying_name = str(item.get("underlying_name", "")).strip()
        underlying_code = normalize_underlying_code_for_group(raw_underlying_code, underlying_name or warrant_name)
        broker_label = str(item.get("broker_label", "")).strip()
        broker_name = str(item.get("broker_name", "")).strip()
        broker_code = str(item.get("broker_code", "")).strip()

        if not underlying_code:
            continue

        rec = agg.setdefault(underlying_code, {
            "標的股": underlying_code,
            "標的名稱": underlying_name,
            "買進金額": 0.0,
            "賣出金額": 0.0,
            "買進股數": 0.0,
            "賣出股數": 0.0,
            "分點": {},
            "權證": {},
            "日期集合": set(),
        })

        if underlying_name and not rec.get("標的名稱"):
            rec["標的名稱"] = underlying_name

        warrant_rec = rec["權證"].setdefault(warrant_code, {
            "權證代號": warrant_code,
            "權證名稱": warrant_name,
            "買進金額": 0.0,
            "賣出金額": 0.0,
            "買進股數": 0.0,
            "賣出股數": 0.0,
            "日期集合": set(),
        })
        if warrant_name and not warrant_rec.get("權證名稱"):
            warrant_rec["權證名稱"] = warrant_name

        broker_key = (broker_label, broker_name, broker_code)
        broker_rec = rec["分點"].setdefault(broker_key, {
            "分點": broker_label,
            "分點名稱": broker_name,
            "券商代號": broker_code,
            "買進金額": 0.0,
            "賣出金額": 0.0,
            "買進股數": 0.0,
            "賣出股數": 0.0,
            "權證": {},
            "日期集合": set(),
        })
        broker_warrant_rec = broker_rec["權證"].setdefault(warrant_code, {
            "權證代號": warrant_code,
            "權證名稱": warrant_name,
            "買進金額": 0.0,
            "賣出金額": 0.0,
            "買進股數": 0.0,
            "賣出股數": 0.0,
            "日期集合": set(),
        })

        for row in df.itertuples(index=False):
            row_dict = row._asdict()
            date_str = normalize_date_str(row_dict.get("日期", ""))
            dt = parse_date(date_str)

            if not dt or dt < start_dt or dt > target_dt:
                continue

            buy_amount = top15_safe_float(row_dict.get("買進金額", 0))
            sell_amount = top15_safe_float(row_dict.get("賣出金額", 0))
            buy_qty = top15_safe_float(row_dict.get("買進股數", 0))
            sell_qty = top15_safe_float(row_dict.get("賣出股數", 0))

            if buy_amount <= 0 and sell_amount <= 0 and buy_qty <= 0 and sell_qty <= 0:
                continue

            rec["日期集合"].add(date_str)
            rec["買進金額"] += buy_amount
            rec["賣出金額"] += sell_amount
            rec["買進股數"] += buy_qty
            rec["賣出股數"] += sell_qty

            warrant_rec["日期集合"].add(date_str)
            warrant_rec["買進金額"] += buy_amount
            warrant_rec["賣出金額"] += sell_amount
            warrant_rec["買進股數"] += buy_qty
            warrant_rec["賣出股數"] += sell_qty

            broker_rec["日期集合"].add(date_str)
            broker_rec["買進金額"] += buy_amount
            broker_rec["賣出金額"] += sell_amount
            broker_rec["買進股數"] += buy_qty
            broker_rec["賣出股數"] += sell_qty

            broker_warrant_rec["日期集合"].add(date_str)
            broker_warrant_rec["買進金額"] += buy_amount
            broker_warrant_rec["賣出金額"] += sell_amount
            broker_warrant_rec["買進股數"] += buy_qty
            broker_warrant_rec["賣出股數"] += sell_qty

    records = []

    for rec in agg.values():
        buy_amount = float(rec.get("買進金額", 0) or 0)
        sell_amount = float(rec.get("賣出金額", 0) or 0)
        buy_qty = float(rec.get("買進股數", 0) or 0)
        sell_qty = float(rec.get("賣出股數", 0) or 0)
        net_buy = buy_amount - sell_amount
        net_sell = sell_amount - buy_amount

        if buy_amount <= 0 and sell_amount <= 0:
            continue

        records.append({
            **rec,
            "買進金額": buy_amount,
            "賣出金額": sell_amount,
            "買進股數": buy_qty,
            "賣出股數": sell_qty,
            "淨買超金額": net_buy,
            "淨賣超金額": net_sell,
        })

    # 最後再做一次標的層級保護性合併。
    # 若舊快取或資料來源仍出現 2408 / '2408 / 2408.0 這種不同寫法，
    # 這裡會再收斂成同一檔標的，確保排序前同標的只剩一筆。
    merged_records = {}
    for rec in records:
        key = normalize_underlying_code_for_group(rec.get("標的股", ""), rec.get("標的名稱", ""))
        if not key:
            continue

        if key not in merged_records:
            rec["標的股"] = key
            merged_records[key] = rec
            continue

        dst = merged_records[key]
        for numeric_col in ["買進金額", "賣出金額", "買進股數", "賣出股數", "淨買超金額", "淨賣超金額"]:
            dst[numeric_col] = float(dst.get(numeric_col, 0) or 0) + float(rec.get(numeric_col, 0) or 0)

        if not dst.get("標的名稱") and rec.get("標的名稱"):
            dst["標的名稱"] = rec.get("標的名稱")

        dst.setdefault("日期集合", set()).update(rec.get("日期集合", set()))

        for warrant_code, warrant_rec in rec.get("權證", {}).items():
            if warrant_code in dst.get("權證", {}):
                dw = dst["權證"][warrant_code]
                for numeric_col in ["買進金額", "賣出金額", "買進股數", "賣出股數"]:
                    dw[numeric_col] = float(dw.get(numeric_col, 0) or 0) + float(warrant_rec.get(numeric_col, 0) or 0)
                dw.setdefault("日期集合", set()).update(warrant_rec.get("日期集合", set()))
            else:
                dst.setdefault("權證", {})[warrant_code] = warrant_rec

        for broker_key, broker_rec in rec.get("分點", {}).items():
            if broker_key not in dst.get("分點", {}):
                dst.setdefault("分點", {})[broker_key] = broker_rec
                continue
            db = dst["分點"][broker_key]
            for numeric_col in ["買進金額", "賣出金額", "買進股數", "賣出股數"]:
                db[numeric_col] = float(db.get(numeric_col, 0) or 0) + float(broker_rec.get(numeric_col, 0) or 0)
            db.setdefault("日期集合", set()).update(broker_rec.get("日期集合", set()))
            for warrant_code, bw in broker_rec.get("權證", {}).items():
                if warrant_code in db.get("權證", {}):
                    dbw = db["權證"][warrant_code]
                    for numeric_col in ["買進金額", "賣出金額", "買進股數", "賣出股數"]:
                        dbw[numeric_col] = float(dbw.get(numeric_col, 0) or 0) + float(bw.get(numeric_col, 0) or 0)
                    dbw.setdefault("日期集合", set()).update(bw.get("日期集合", set()))
                else:
                    db.setdefault("權證", {})[warrant_code] = bw

    records = list(merged_records.values())

    buy_top = [r for r in records if float(r.get("淨買超金額", 0) or 0) > 0]
    sell_top = [r for r in records if float(r.get("淨賣超金額", 0) or 0) > 0]

    buy_top = sorted(
        buy_top,
        key=lambda r: (float(r.get("淨買超金額", 0) or 0), float(r.get("買進金額", 0) or 0)),
        reverse=True,
    )[:top_n]
    sell_top = sorted(
        sell_top,
        key=lambda r: (float(r.get("淨賣超金額", 0) or 0), float(r.get("賣出金額", 0) or 0)),
        reverse=True,
    )[:top_n]

    def make_rank_rows(rank_type, records_for_rank):
        rows = []
        is_buy = rank_type == "共識買超"

        for rank, rec in enumerate(records_for_rank, 1):
            broker_rows = []
            broker_json = []
            same_direction_count = 0
            opposite_direction_count = 0

            for broker_rec in sorted(
                rec["分點"].values(),
                key=lambda x: (
                    (float(x.get("買進金額", 0) or 0) - float(x.get("賣出金額", 0) or 0))
                    if is_buy else
                    (float(x.get("賣出金額", 0) or 0) - float(x.get("買進金額", 0) or 0))
                ),
                reverse=True,
            ):
                b_buy = float(broker_rec.get("買進金額", 0) or 0)
                b_sell = float(broker_rec.get("賣出金額", 0) or 0)
                b_net_buy = b_buy - b_sell
                b_net_sell = b_sell - b_buy
                direction_amount = b_net_buy if is_buy else b_net_sell

                if direction_amount > 0:
                    same_direction_count += 1
                    broker_rows.append(f"{broker_rec['分點']} {_fmt_wan_text(direction_amount)}")
                elif direction_amount < 0:
                    opposite_direction_count += 1

                broker_warrants = []
                for bw in sorted(
                    broker_rec.get("權證", {}).values(),
                    key=lambda x: (float(x.get("買進金額", 0) or 0) + float(x.get("賣出金額", 0) or 0)),
                    reverse=True,
                ):
                    broker_warrants.append({
                        "權證代號": bw.get("權證代號", ""),
                        "權證名稱": bw.get("權證名稱", ""),
                        "買進金額": round(float(bw.get("買進金額", 0) or 0), 0),
                        "賣出金額": round(float(bw.get("賣出金額", 0) or 0), 0),
                        "買進股數": round(float(bw.get("買進股數", 0) or 0), 0),
                        "賣出股數": round(float(bw.get("賣出股數", 0) or 0), 0),
                        "日期數": len(bw.get("日期集合", set())),
                    })

                broker_json.append({
                    "分點": broker_rec["分點"],
                    "分點名稱": broker_rec["分點名稱"],
                    "券商代號": broker_rec["券商代號"],
                    "買進金額": round(b_buy, 0),
                    "賣出金額": round(b_sell, 0),
                    "淨買超金額": round(b_net_buy, 0),
                    "淨賣超金額": round(b_net_sell, 0),
                    "權證檔數": len(broker_rec.get("權證", {})),
                    "日期數": len(broker_rec.get("日期集合", set())),
                    "權證明細": broker_warrants,
                })

            rank_amount = float(rec.get("淨買超金額", 0) or 0) if is_buy else float(rec.get("淨賣超金額", 0) or 0)
            dates = sorted(rec.get("日期集合", set()))
            warrant_values = sorted(
                rec.get("權證", {}).values(),
                key=lambda x: (float(x.get("買進金額", 0) or 0) + float(x.get("賣出金額", 0) or 0)),
                reverse=True,
            )
            warrant_list = "；".join([
                f"{w.get('權證代號', '')} {w.get('權證名稱', '')}"
                f"｜買{_fmt_wan_text(w.get('買進金額', 0))}／賣{_fmt_wan_text(w.get('賣出金額', 0))}"
                for w in warrant_values
            ])
            top_warrant = warrant_values[0] if warrant_values else {}

            rows.append({
                "資料範圍": scope,
                "統計日期": target_date,
                "統計期間": period_text,
                "統計天數": window_days,
                "有效日期數": len(dates),
                "第一筆日期": dates[0] if dates else "",
                "最後筆日期": dates[-1] if dates else "",
                "排名類型": rank_type,
                "排名": rank,
                # 保留舊欄位，避免圖片端或舊公式依欄名讀取時壞掉；但實際排名已是標的層級。
                "權證代號": top_warrant.get("權證代號", ""),
                "權證名稱": top_warrant.get("權證名稱", "同標的合計"),
                "標的股": rec.get("標的股", ""),
                "標的名稱": rec.get("標的名稱", ""),
                "權證檔數": len(rec.get("權證", {})),
                "權證清單": warrant_list,
                "排名金額": round(rank_amount, 0),
                "買進金額": round(float(rec.get("買進金額", 0) or 0), 0),
                "賣出金額": round(float(rec.get("賣出金額", 0) or 0), 0),
                "淨買超金額": round(float(rec.get("淨買超金額", 0) or 0), 0),
                "淨賣超金額": round(float(rec.get("淨賣超金額", 0) or 0), 0),
                "買進股數": round(float(rec.get("買進股數", 0) or 0), 0),
                "賣出股數": round(float(rec.get("賣出股數", 0) or 0), 0),
                "參與分點數": len(rec.get("分點", {})),
                "同向分點數": same_direction_count,
                "反向分點數": opposite_direction_count,
                "主要同向分點": "；".join(broker_rows[:8]),
                "完成狀態": "DONE",
                "更新時間": update_time,
                "run_id": run_id,
                "分點明細_JSON": json.dumps(broker_json, ensure_ascii=False),
            })

        return rows

    rows = []
    rows.extend(make_rank_rows("共識買超", buy_top))
    rows.extend(make_rank_rows("共識賣超", sell_top))

    print(
        f"  ✅ 近7日權證分點共識TOP15完成："
        f"共識買超 {len(buy_top):,} 檔標的，共識賣超 {len(sell_top):,} 檔標的，"
        f"統計期間 {period_text}"
    )
    return rows


def write_7d_warrant_consensus_top15_sheet(wb, rows):
    """寫入近 7 日權證分點共識買賣超 TOP15。rows=None 代表不建立工作表。"""
    if rows is None:
        return

    ws = wb.create_sheet(WARRANT_CONSENSUS_7D_SHEET)

    headers = [
        "資料範圍", "統計日期", "統計期間", "統計天數", "有效日期數", "第一筆日期", "最後筆日期",
        "排名類型", "排名",
        "權證代號", "權證名稱", "標的股", "標的名稱", "權證檔數", "權證清單",
        "排名金額", "買進金額", "賣出金額", "淨買超金額", "淨賣超金額",
        "買進股數", "賣出股數",
        "參與分點數", "同向分點數", "反向分點數", "主要同向分點",
        "完成狀態", "更新時間", "run_id", "分點明細_JSON",
    ]

    ws.append(headers)

    for row in rows or []:
        ws.append([row.get(h, "") for h in headers])

    col_widths = [12, 12, 24, 10, 12, 12, 12, 12, 8, 12, 24, 10, 12, 10, 72, 14, 14, 14, 14, 14, 14, 14, 12, 12, 12, 56, 10, 20, 16, 90]
    thin_gray = Side(style="thin", color="B7B7B7")
    normal_border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for cell in ws[1]:
        cell.font = Font(bold=True, color="000000")
        cell.fill = YELLOW
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = normal_border

    ws.row_dimensions[1].height = 24

    header_map = {str(cell.value).strip(): idx + 1 for idx, cell in enumerate(ws[1])}
    rank_type_col = header_map.get("排名類型")

    for row in ws.iter_rows(min_row=2):
        rank_type = str(row[rank_type_col - 1].value or "").strip() if rank_type_col else ""
        row_fill = RED if rank_type == "共識買超" else GREEN if rank_type == "共識賣超" else WHITE

        for cell in row:
            cell.font = Font(color="000000")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
            cell.fill = row_fill

        ws.row_dimensions[row[0].row].height = 30

    ws.freeze_panes = "A2"

def build_excel(a_events, b_events, c_events, d_events, item_map, price_cache, items, output_path, top15_detail_rows=None, top15_consensus_rows=None, warrant_consensus_7d_rows=None, broker_10d_detail_rows=None):
    print("【Step 5】建立 Excel...")

    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    write_a_sheet(wb, a_events, item_map, price_cache)
    write_group_sheet(wb, "B_同標的單日合計", b_events, price_cache, is_c=False)
    write_group_sheet(wb, "C_同標的3日累積", c_events, price_cache, is_c=True)
    write_group_sheet(wb, f"D_近{D_WINDOW_DAYS}日累積淨買進", d_events, price_cache, is_c=True)
    write_daily_sell_detail_sheet(wb, items, a_events, b_events, c_events, d_events)
    write_top15_consensus_cache_sheet(wb, top15_consensus_rows or [])
    write_top15_position_detail_sheet(wb, top15_detail_rows or [])
    write_7d_warrant_consensus_top15_sheet(wb, warrant_consensus_7d_rows)
    write_10d_broker_underlying_detail_sheet(wb, broker_10d_detail_rows)
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

    configure_run_mode()

    today_fn = datetime.today().strftime("%Y%m%d")
    output_path = os.path.join(OUTPUT_DIR, f"warrant_backtest_ABCD_{today_fn}.xlsx")

    print(f"\n認購權證特定分點買超回測 ABCD 版 | {today_fn}")
    print(f"A：單檔權證買進金額 >= {AMOUNT_THRESH // 10000}萬")
    print(f"B：同分點 + 同標的 + 單日多檔權證合計買超 >= {AMOUNT_THRESH // 10000}萬")
    print(f"C：同分點 + 同標的 + 連續3交易日多檔權證累積買超 >= {AMOUNT_THRESH // 10000}萬")
    print(f"D：同分點 + 同標的 + 近{D_WINDOW_DAYS}交易日累積淨買進 >= {AMOUNT_THRESH // 10000}萬")
    print(f"執行模式：RUN_MODE={RUN_MODE}（1=精選分點全市場追蹤，2=完整分點清單）")
    print(f"分點數：{len(TARGET_PATTERNS)} 個")
    print(f"追蹤分點：{', '.join(TARGET_PATTERNS.keys())}")
    print(f"加速模式：FAST_SKIP_RECENT_PRESCAN={FAST_SKIP_RECENT_PRESCAN}，FETCH_GROUP_WARRANT_PRICES={FETCH_GROUP_WARRANT_PRICES}")
    print("=" * 70)

    warrants = get_all_call_warrants()

    if not warrants:
        elapsed = time.time() - program_start
        print(f"\n⏱️ 總執行時間：{elapsed:.2f} 秒")
        return

    broker_map = filter_broker_map_for_active_targets(find_broker_codes(warrants))

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
    history_was_empty = history_cache_df is None or history_cache_df.empty
    history_keys = history_cache_keys(history_cache_df)
    history_latest_map = history_cache_latest_dates(history_cache_df)

    if CACHE_INCREMENTAL_UPDATE_ENABLED and not history_was_empty:
        before_prune_count = len(candidates)
        keep_keys = (history_keys & candidate_keys) | PRESCAN_MISSING_FETCH_KEYS
        candidates = [c for c in candidates if candidate_key_from_tuple(c) in keep_keys]
        candidate_keys = {candidate_key_from_tuple(c) for c in candidates}
        pruned_count = before_prune_count - len(candidates)
        if pruned_count > 0:
            print(f"  ✅ 增量模式已略過舊候選快取中的無歷史資料空候選：{pruned_count:,} 組")

    cached_items = items_from_history_cache(history_cache_df, candidate_filter=candidate_keys)

    if cached_items:
        print(f"  ✅ 已從原始分點資料快取還原 {len(cached_items)} 組資料")

    candidates_to_fetch = []

    for c in candidates:
        key = candidate_key_from_tuple(c)

        if should_fetch_candidate_incremental(key, history_keys, history_latest_map, PRESCAN_REFRESH_KEYS, PRESCAN_MISSING_FETCH_KEYS):
            candidates_to_fetch.append(c)

    print(f"  ✅ 快取已有候選：{len(history_keys & candidate_keys)} 組")
    print(f"  ✅ API4 直接近期活動候選：{len(PRESCAN_REFRESH_KEYS & candidate_keys)} 組")
    print(f"  ✅ 本次允許缺快取補抓候選：{len(PRESCAN_MISSING_FETCH_KEYS & candidate_keys)} 組")
    print(f"  ✅ 增量更新模式：CACHE_INCREMENTAL_UPDATE_ENABLED={CACHE_INCREMENTAL_UPDATE_ENABLED}，目標日期={get_incremental_refresh_target_dt().strftime('%Y/%m/%d')}")
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
        save_history_cache(history_cache_df, fetched_items=fetched_items, previous_history_empty=history_was_empty)

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
    top15_detail_rows, top15_consensus_rows = build_top15_position_detail_and_consensus_rows(
        a_events, b_events, c_events, d_events, item_map, price_cache
    )
    warrant_consensus_7d_rows = build_7d_warrant_consensus_top15_rows(items)

    if RUN_MODE == 2:
        price_cache = ensure_broker_10d_warrant_prices(price_cache, items)
        broker_10d_detail_rows = build_10d_broker_underlying_detail_rows(items, price_cache)
    else:
        print("  ✅ RUN_MODE=1 精選分點模式：略過近10日分點買賣明細工作表，避免動到 Google Sheet 既有資料。")
        broker_10d_detail_rows = None

    build_excel(
        a_events, b_events, c_events, d_events,
        item_map, price_cache, items, output_path,
        top15_detail_rows, top15_consensus_rows, warrant_consensus_7d_rows, broker_10d_detail_rows
    )
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
