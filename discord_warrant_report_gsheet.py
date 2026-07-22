#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日精選分點買賣超追蹤圖卡｜MoneyDJ 新制 A～E Google Sheet 讀取版

用途：
- 直接讀取 Google Sheet「權證分點籌碼」內的 A/B/C/D/E 與勝率統計工作表
- 不需要本機 Excel
- 產生一頁式 PNG
- 可產生「所有分點勝率統計圖」，列出 A 勝率、A 事件數、總勝率、總事件數、平均持有天數與加權報酬率
- 發送到 DISCORD_WEBHOOK_URL_TEST

必要 GitHub Secrets：
- GCP_SERVICE_KEY
- DISCORD_WEBHOOK_URL_TEST

可選環境變數：
- GOOGLE_SHEET_ID：建議使用，最穩
- GOOGLE_SHEET_NAME：沒有 GOOGLE_SHEET_ID 時才用名稱開啟，預設「權證分點籌碼」
- TARGET_DATE：指定日期，例如 2026-05-18；沒指定會從 Google Sheet 內自動抓最新日期
- IMAGE_ACTION / ACTION / RUN_PLAN：圖片產生選項，用於 GitHub Actions workflow_dispatch 區別要跑哪張圖
- ADD_COUNT_LOOKBACK_TRADING_DAYS：第幾次加碼計算用，預設 50 個有效交易日
- WEEKLY_WARRANT_CONSENSUS_OUTPUT_IMAGE：近7日權證共識圖輸出路徑
- WARRANT_CONSENSUS_14D_OUTPUT_IMAGE：近14日權證共識圖輸出路徑
- WARRANT_CONSENSUS_21D_OUTPUT_IMAGE：近21日權證共識圖輸出路徑
- WIN_RATE_STATS_OUTPUT_IMAGE：所有分點勝率統計圖輸出路徑
"""

from __future__ import annotations

import os
import math
import re
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, date, timedelta

import requests
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib import font_manager


# ══════════════════════════════════════════════════════════════════════
# 基本設定
# ══════════════════════════════════════════════════════════════════════

TRACKED_BROKERS = [
    "華南永昌台中",
    "元大南屯",
    "新光",
    "永豐金內湖",
    "富邦敦南",
]

# 近10日分點買賣明細圖預設輸出清單。
# 若由 Discord 指令 / GitHub Actions 指定 broker_name，會在 main() 內覆蓋為單一分點。
BROKER_10D_IMAGE_BROKERS = [
    "元大南屯",
]


def split_broker_names(value: str) -> list[str]:
    """
    將 CLI / GitHub Actions / 環境變數傳進來的分點字串轉成清單。

    支援：
    - 元大南屯
    - 元大南屯,華南永昌台中
    - 元大南屯、華南永昌台中
    - 元大南屯；華南永昌台中
    - 多行分點清單
    """
    raw = str(value or "").strip()
    if not raw:
        return []

    parts = [
        x.strip()
        for x in re.split(r"[,，;；、\n\r\t]+", raw)
        if x.strip()
    ]

    out = []
    for broker in parts:
        if broker not in out:
            out.append(broker)
    return out


def resolve_broker_10d_image_brokers(cli_broker: str = "") -> list[str]:
    """
    決定近10日分點買賣明細圖要輸出的分點。

    優先順序：
    1. CLI --broker / --broker-name
    2. GitHub Actions / Discord 傳入的 BROKER_NAME
    3. 環境變數 BROKER_10D_BROKER
    4. 環境變數 BROKER_10D_IMAGE_BROKERS
    5. 程式原本的 BROKER_10D_IMAGE_BROKERS 預設清單

    這樣 Discord 指令選到單一分點時，只會產生該分點的圖；
    排程或手動沒有指定分點時，仍維持原本固定清單。
    """
    for source in [
        cli_broker,
        os.getenv("BROKER_NAME", ""),
        os.getenv("BROKER_10D_BROKER", ""),
        os.getenv("BROKER_10D_IMAGE_BROKERS", ""),
    ]:
        brokers = split_broker_names(source)
        if brokers:
            return brokers

    return list(BROKER_10D_IMAGE_BROKERS)

DATA_SCOPE_SELECTED5 = os.getenv("DATA_SCOPE_SELECTED5", "精選五分點")
DATA_SCOPE_ALL = os.getenv("DATA_SCOPE_ALL", "全分點")

BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "1000000"))
SELL_RATIO = float(os.getenv("SELL_THRESHOLD_RATIO", "0.5"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", str(BUY_THRESHOLD * SELL_RATIO)))
LOOKBACK_TRADING_DAYS = int(os.getenv("LOOKBACK_TRADING_DAYS", "40"))
TOP15_TRADED_PRICE_COVERAGE_NOTE_THRESHOLD_PCT = min(
    max(float(os.getenv("TOP15_TRADED_PRICE_COVERAGE_NOTE_THRESHOLD_PCT", "60")), 0.0),
    100.0,
)
TOP15_LOW_TRADED_COVERAGE_SYMBOL = "*"
# 專門給「第幾次加碼」使用，不影響原本近一個月共識買超圖。
ADD_COUNT_LOOKBACK_TRADING_DAYS = int(os.getenv("ADD_COUNT_LOOKBACK_TRADING_DAYS", "50"))

# 若你未來想讓「出清不管金額都顯示」，改成 "1"
DISPLAY_EXIT_ALWAYS = os.getenv("DISPLAY_EXIT_ALWAYS", "0") == "1"

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "權證分點資料_NEW_MoneyDJ")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

SHEET_A = os.getenv("SHEET_A", "A_基礎買超")
SHEET_B = os.getenv("SHEET_B", "B_明顯買超")
SHEET_C = os.getenv("SHEET_C", "C_強勢買超")
SHEET_D = os.getenv("SHEET_D", "D_大額布局")
SHEET_E = os.getenv("SHEET_E", "E_超大額布局")

# 新制金額強度分類。五張事件表欄位結構相同：
# 事件日、單日累積買進金額、買超張數、權證清單、減碼／出清資訊。
AMOUNT_CLASS_SHEET_PLAN = [
    ("A", "基礎買超", SHEET_A),
    ("B", "明顯買超", SHEET_B),
    ("C", "強勢買超", SHEET_C),
    ("D", "大額布局", SHEET_D),
    ("E", "超大額布局", SHEET_E),
]
AMOUNT_CLASS_SHEETS = [sheet_name for _code, _label, sheet_name in AMOUNT_CLASS_SHEET_PLAN]
AMOUNT_CLASS_LABELS = {code: label for code, label, _sheet_name in AMOUNT_CLASS_SHEET_PLAN}
SHEET_STAT = "勝率統計"
WIN_RATE_STATS_LAYOUT_VERSION = "dual-event-count-v2"
SHEET_DAILY_SELL = os.getenv("SHEET_DAILY_SELL", "每日賣出明細")
SHEET_HISTORY = os.getenv("SHEET_HISTORY", "快取_分點歷史")
SHEET_TOP15_RETURN_CACHE = os.getenv("SHEET_TOP15_RETURN_CACHE", "快取_TOP15分點報酬率")
SHEET_TOP15_CONSENSUS_CACHE = os.getenv("SHEET_TOP15_CONSENSUS_CACHE", "快取_TOP15共識淨買超")
SHEET_TOP15_POSITION_DETAIL = os.getenv("SHEET_TOP15_POSITION_DETAIL", "快取_TOP15部位明細")
SHEET_WARRANT_CONSENSUS_7D = os.getenv("SHEET_WARRANT_CONSENSUS_7D", "快取_近7日權證分點共識TOP15")
SHEET_BROKER_10D_DETAIL = os.getenv("SHEET_BROKER_10D_DETAIL", "快取_近10日分點買賣明細")
# 近10日分點明細圖的「共識訊號」判斷門檻。
# 會以同一批「快取_近10日分點買賣明細」內所有分點一起判斷，而不是只看目前圖片的單一分點。
# 分歧 / 逆向不只看有沒有反方向分點，也會看反方向金額占比，避免同一標的只要有人買、有人賣就被判成分歧。
BROKER_10D_CONSENSUS_MIN_SAME_BROKERS = int(os.getenv("BROKER_10D_CONSENSUS_MIN_SAME_BROKERS", "2"))
BROKER_10D_CONSENSUS_OPPOSITE_WARNING_BROKERS = int(os.getenv("BROKER_10D_CONSENSUS_OPPOSITE_WARNING_BROKERS", "2"))
BROKER_10D_CONSENSUS_DIVERGENCE_AMOUNT_RATIO = float(os.getenv("BROKER_10D_CONSENSUS_DIVERGENCE_AMOUNT_RATIO", "0.5"))
BROKER_10D_CONSENSUS_REVERSE_AMOUNT_RATIO = float(os.getenv("BROKER_10D_CONSENSUS_REVERSE_AMOUNT_RATIO", "1.0"))


NTD_PER_WARRANT_POINT = float(os.getenv("NTD_PER_WARRANT_POINT", "1000"))
# 若某權證不在 A/B/C/D/E 白名單，但同一分點 + 同一標的於同一天賣出合計達此門檻，
# 仍納入今日賣超明細。預設沿用買超門檻 100 萬。
NON_ABCD_SELL_UNDERLYING_THRESHOLD = float(
    os.getenv("NON_ABCD_SELL_UNDERLYING_THRESHOLD", str(BUY_THRESHOLD))
)

# TOP15 計算明細除錯輸出。
# 預設會在 console 印出 2408 南亞科 / 元大南屯 的買進與扣減明細，
# 用來確認 TOP15 金額與手算差異。正式部署若不想顯示，設定 DEBUG_TOP15_DETAIL=0。
DEBUG_TOP15_DETAIL = os.getenv("DEBUG_TOP15_DETAIL", "1") == "1"
DEBUG_TOP15_UNDERLYING = os.getenv("DEBUG_TOP15_UNDERLYING", "2408").strip()
DEBUG_TOP15_BROKER = os.getenv("DEBUG_TOP15_BROKER", "元大南屯").strip()

# 常見權證發行券商關鍵字，用來從權證名稱中反推標的股名。
WARRANT_ISSUER_TOKENS = [
    "元大", "凱基", "群益", "富邦", "國泰", "永豐", "永豐金", "國票", "中信",
    "台新", "兆豐", "元富", "玉山", "第一金", "新光", "日盛", "康和", "統一",
    "宏遠", "合庫", "犇亞", "華南永昌", "台企銀", "聯邦", "高盛", "瑞銀",
    "摩根大通", "麥格理", "法銀巴黎", "上海商銀"
]

# 已知 ETF / 指數型商品名稱對應。
# 目的：避免權證名稱「台灣50...」被主表舊標的股代號誤帶成 3045。
KNOWN_UNDERLYING_NAME_CODE_MAP = {
    "台灣50": "0050",
    "臺灣50": "0050",
    "元大台灣50": "0050",
    "元大臺灣50": "0050",
}


_STOCK_NAME_MAP = None
_STOCK_CODE_BY_NAME_MAP = None

# 浮水印設定
WATERMARK_TEXT = "By 股市艾斯出品-轉傳請註明\n資訊分享非投資建議 投資請自行評估風險"
WATERMARK_ALPHA = 0.80

CENTER_WATERMARK_TEXT = "股市艾斯\n台股DC討論群"
CENTER_WATERMARK_ALPHA = 0.06
CENTER_WATERMARK_FONT_SIZE = 80
CENTER_WATERMARK_ROTATION = 18

# 事件代號說明
EVENT_LEGEND_ITEMS = [
    ("A", "基礎買超 100–159萬"),
    ("B", "明顯買超 160–249萬"),
    ("C", "強勢買超 250–499萬"),
    ("D", "大額布局 500–999萬"),
    ("E", "超大額布局 1000萬以上"),
]


# ══════════════════════════════════════════════════════════════════════
# Google Sheet 讀取
# ══════════════════════════════════════════════════════════════════════

_GSHEET = None
_WORKSHEET_VALUES_CACHE: dict[str, list[list[str]]] = {}


def get_gsheet():
    global _GSHEET
    if _GSHEET is not None:
        return _GSHEET

    service_key = os.getenv("GCP_SERVICE_KEY", "").strip()
    if not service_key:
        raise RuntimeError("找不到 GCP_SERVICE_KEY，請先在 GitHub Secrets 設定。")

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(service_key)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)

    if GOOGLE_SHEET_ID:
        _GSHEET = gc.open_by_key(GOOGLE_SHEET_ID)
    else:
        _GSHEET = gc.open(GOOGLE_SHEET_NAME)

    return _GSHEET


def strip_gsheet_text_prefix(v):
    s = "" if v is None else str(v).strip()
    return s[1:] if s.startswith("'") else s


def worksheet_values(sheet_name: str) -> list[list[str]]:
    if sheet_name in _WORKSHEET_VALUES_CACHE:
        return [
            list(row)
            for row in _WORKSHEET_VALUES_CACHE[sheet_name]
        ]

    sh = get_gsheet()
    ws = sh.worksheet(sheet_name)
    values = ws.get_all_values()

    _WORKSHEET_VALUES_CACHE[sheet_name] = [
        list(row)
        for row in values
    ]

    print(
        f"  📥 已讀取 Google Sheet：{sheet_name}｜"
        f"{len(values):,} 列"
    )

    return [
        list(row)
        for row in values
    ]


def read_gsheet_table(
    sheet_name: str,
    needed_cols: list[str] | None = None,
    filter_tracked_brokers: bool = True,
) -> pd.DataFrame:
    """
    讀取一般工作表：
    - 第 1 列是表頭
    - 後面是資料
    - 自動補齊欄位數
    - 只保留 needed_cols 交集
    - 預設只保留 TRACKED_BROKERS；若要讀全分點資料，可傳 filter_tracked_brokers=False
    """
    values = worksheet_values(sheet_name)
    if not values:
        return pd.DataFrame()

    headers = [str(h).strip() for h in values[0]]
    if not headers or all(h == "" for h in headers):
        return pd.DataFrame()

    n_cols = len(headers)
    rows = []
    for row in values[1:]:
        row = list(row)
        if len(row) < n_cols:
            row += [""] * (n_cols - len(row))
        elif len(row) > n_cols:
            row = row[:n_cols]
        rows.append([strip_gsheet_text_prefix(x) for x in row])

    df = pd.DataFrame(rows, columns=headers).fillna("")

    if needed_cols is not None:
        keep_cols = [c for c in needed_cols if c in df.columns]
        df = df[keep_cols].copy()

    if filter_tracked_brokers and "分點" in df.columns:
        df = df[df["分點"].isin(TRACKED_BROKERS)].copy()

    return df


def read_gsheet_stat_raw() -> pd.DataFrame:
    """
    勝率統計表不是標準單一表頭，因此用 header=None 方式讀。
    """
    values = worksheet_values(SHEET_STAT)
    max_cols = max((len(r) for r in values), default=0)
    fixed = []
    for row in values:
        row = list(row)
        if len(row) < max_cols:
            row += [""] * (max_cols - len(row))
        fixed.append([strip_gsheet_text_prefix(x) for x in row])
    return pd.DataFrame(fixed).fillna("")


def read_gsheet_table_optional(
    sheet_name: str,
    needed_cols: list[str] | None = None,
    filter_tracked_brokers: bool = True,
) -> pd.DataFrame:
    """
    讀取可能不存在的工作表。
    主要用於「每日賣出明細」：若舊版主程式尚未產生該表，圖片程式不應直接中斷。
    """
    try:
        return read_gsheet_table(sheet_name, needed_cols, filter_tracked_brokers=filter_tracked_brokers)
    except Exception:
        return pd.DataFrame()


def filter_df_by_data_scope(df: pd.DataFrame, wanted_scope: str) -> pd.DataFrame:
    """
    依「資料範圍」欄位過濾。
    - 若工作表尚未有「資料範圍」欄位，直接回傳原資料（相容舊版）
    - 若有欄位但無符合 wanted_scope 的資料，回傳空表，避免混用不同模式資料
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if "資料範圍" not in df.columns:
        return df

    scope_series = df["資料範圍"].astype(str).map(strip_gsheet_text_prefix).str.strip()
    part = df[scope_series == str(wanted_scope).strip()].copy()
    if part.empty:
        print(f"  ⚠️ 工作表找不到 資料範圍={wanted_scope} 的資料。")
    return part


# ══════════════════════════════════════════════════════════════════════
# 資料清洗工具
# ══════════════════════════════════════════════════════════════════════

def parse_google_serial_date(s: str) -> date | None:
    """
    Google Sheet 若日期格式沒有套好，可能會變成 46160 這種日期序號。
    Google Sheets 日期序號：1899-12-30 為 day 0。
    """
    try:
        if not re.fullmatch(r"\d+(\.0)?", str(s).strip()):
            return None
        serial = int(float(str(s).strip()))
        if serial < 20000 or serial > 80000:
            return None
        return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
    except Exception:
        return None


def parse_date_value(v) -> date | None:
    if v is None:
        return None

    s = strip_gsheet_text_prefix(v)
    if not s or s == "-":
        return None

    serial_date = parse_google_serial_date(s)
    if serial_date:
        return serial_date

    try:
        # 支援 2026/05/18、2026-05-18、2026-05-18 00:00:00
        t = pd.to_datetime(s.replace("年", "/").replace("月", "/").replace("日", ""), errors="coerce")
        if pd.isna(t):
            return None
        return t.date()
    except Exception:
        return None


def safe_float(v, default=0.0) -> float:
    try:
        if v is None:
            return default

        s = strip_gsheet_text_prefix(v)
        if s == "" or s == "-":
            return default

        # Google Sheet 可能讀到 1,003,600 或 +30.36%
        s = s.replace(",", "").replace("%", "").replace("＋", "+").strip()
        if s.startswith("+"):
            s = s[1:]

        return float(s)
    except Exception:
        return default


def safe_int(v, default=0) -> int:
    try:
        return int(float(str(safe_float(v))))
    except Exception:
        return default


def normalize_code(v) -> str:
    s = strip_gsheet_text_prefix(v)
    if not s or s == "-":
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def normalize_warrant_code(v) -> str:
    s = normalize_code(v)
    if not s:
        return ""

    if s.isdigit() and len(s) == 5:
        return s.zfill(6)

    return s


def normalize_underlying(v, warrant_text: str = "") -> str:
    s = normalize_code(v)

    # 若權證名稱可明確辨識為特定 ETF / 商品，優先用名稱修正代號。
    # 例如「台灣50元大5A購08」應為 0050，不應顯示成 3045。
    name = ""
    try:
        if warrant_text:
            name = extract_stock_name_from_warrant_text(warrant_text)
    except Exception:
        name = ""

    if name:
        mapped_code = KNOWN_UNDERLYING_NAME_CODE_MAP.get(name)
        if mapped_code:
            return mapped_code

    if s.isdigit():
        # 股票 / ETF 標的代號應保留前導 0，例如 0050。
        # 若 Google Sheet 曾把 0050 轉成 50，這裡補回 4 碼。
        if len(s) < 4:
            return s.zfill(4)
        return s

    return s


def money_to_wan(v: float) -> float:
    return round(float(v or 0) / 10000, 1)


def fmt_wan(v: float) -> str:
    """
    金額顯示：
    - 未滿 1 億：維持「萬」
    - 達 1 億以上：自動換算成「億」，避免圖卡欄位出現五位數以上的萬而太長
    """
    try:
        value = float(v or 0)
    except Exception:
        value = 0.0

    if abs(value) >= 100000000:
        yi = value / 100000000
        txt = f"{yi:.2f}".rstrip("0").rstrip(".")
        return f"{txt} 億"

    return f"{money_to_wan(value):.1f} 萬"


def count_warrants_in_text(text: str) -> int:
    """
    計算權證名稱 / 權證清單中的權證檔數。
    用於圖卡顯示時，把很長的權證清單改成「N 檔權證」，
    避免表格內出現截斷的「...」而影響閱讀。
    """
    s = strip_gsheet_text_prefix(text)
    if not s or s == "-":
        return 0

    parts = []
    for p in re.split(r"[；;]", s):
        p = p.strip()
        if p:
            parts.append(p)

    return len(parts) if parts else 1


def normalize_return_pct(v):
    """
    將 Google Sheet 權證報酬率統一轉成「百分比數值」。

    重要：
    - "11.67%" -> 11.67
    - 11.67    -> 11.67
    - -3       -> -3

    不再用「-5 < pct < 5 就乘以 100」的啟發式判斷，
    避免 Google Sheet 裸百分比數值 3 被誤判成 300%。
    若未來某個來源真的使用 0.03 代表 3%，應針對該來源另外轉換，
    不應放在通用函式中猜測。
    """
    try:
        if v is None:
            return None

        raw = strip_gsheet_text_prefix(v)
        if raw == "" or raw == "-":
            return None

        pct = safe_float(raw, None)
        if pct is None:
            return None

        return pct

    except Exception:
        return None


def normalize_top15_cache_return_pct(v):
    """
    將「快取_TOP15分點報酬率」的報酬率欄位轉成百分比數值。

    重要：
    TOP15 快取表的「報酬率」欄位已經是百分比口徑，例如：
    - -1.87 代表 -1.87%，不可再乘以 100。
    - -3.31 代表 -3.31%，不可再變成 -331%。
    - "12.34%" 代表 12.34%。

    權證剩餘部位報酬率最低只能到 -100%，因此低於 -100 的異常值會限制為 -100。
    """
    try:
        if v is None:
            return None

        raw = strip_gsheet_text_prefix(v)
        if raw == "" or raw == "-":
            return None

        pct = safe_float(raw, None)
        if pct is None:
            return None

        if pct < -100.0:
            pct = -100.0

        return pct

    except Exception:
        return None


def fmt_return_pct(v) -> str:
    try:
        if v is None or v == "":
            return "-"
        pct = safe_float(v, None)
        if pct is None:
            return "-"
        if pct < -100.0:
            pct = -100.0
        return f"{pct:+.1f}%"
    except Exception:
        return "-"


def read_top15_return_cache_from_gsheet(target: date | None = None) -> dict[tuple[str, str], dict]:
    """
    讀取 Google Sheet「快取_TOP15分點報酬率」，回傳：
        {(標的代號, 分點): {
            "return_pct": 報酬率百分比數值或 None,
            "remaining_cost": 目前剩餘淨買超成本,
            "estimated_cost": 可估成本,
            "missing_price_cost": 缺價格成本,
        }}

    重要：
    - 快取表只要該「分點 + 標的」仍有剩餘成本，就應該保留。
    - 就算權證價格抓不到、報酬率為空，也不能直接跳過，否則圖片會誤以為該分點已出清。
    - 圖片端會用 remaining_cost 判斷是否仍有部位，用 return_pct 顯示報酬率。
    """
    needed_cols = [
        "統計日期", "日期", "目標日期",
        "分點", "標的股", "標的",
        "報酬率", "報酬率文字",
        "淨買超成本", "可估成本", "缺價格成本",
        "目前市值", "未實現損益", "價格覆蓋率", "價格覆蓋率文字",
        "最新價格日期", "事件", "權證檔數", "權證清單",
    ]

    df = read_gsheet_table_optional(SHEET_TOP15_RETURN_CACHE, needed_cols)
    if df.empty:
        return {}

    def pick_cache_date(row):
        for col in ["統計日期", "日期", "目標日期"]:
            d = parse_date_value(row.get(col))
            if d:
                return d
        return None

    available_dates = []
    for _, r in df.iterrows():
        d = pick_cache_date(r)
        if d:
            available_dates.append(d)

    chosen_date = None
    if available_dates:
        if target is not None:
            valid = [d for d in available_dates if d <= target]
            chosen_date = max(valid) if valid else max(available_dates)
        else:
            chosen_date = max(available_dates)

    result: dict[tuple[str, str], dict] = {}
    for _, r in df.iterrows():
        row_date = pick_cache_date(r)
        if chosen_date and row_date and row_date != chosen_date:
            continue

        broker = str(r.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        underlying = normalize_underlying(r.get("標的股", ""), r.get("標的", ""))
        if not underlying:
            target_text = strip_gsheet_text_prefix(r.get("標的", ""))
            m = re.match(r"^(\d{1,4})", target_text)
            if m:
                underlying = normalize_underlying(m.group(1), target_text)

        if not underlying:
            continue

        remaining_cost = safe_float(r.get("淨買超成本"), None)
        estimated_cost = safe_float(r.get("可估成本"), None)
        missing_price_cost = safe_float(r.get("缺價格成本"), None)

        # 舊版快取若沒有「淨買超成本」，用可估成本 + 缺價格成本回推。
        if remaining_cost is None:
            estimated_for_total = safe_float(estimated_cost, 0)
            missing_for_total = safe_float(missing_price_cost, 0)
            remaining_cost = estimated_for_total + missing_for_total

        remaining_cost = safe_float(remaining_cost, 0)
        estimated_cost = safe_float(estimated_cost, 0)
        missing_price_cost = safe_float(missing_price_cost, 0)

        # 快取表理論上只會寫入仍有剩餘部位的分點標的。
        # 若 remaining_cost <= 0，視為已出清，不放入 result。
        if remaining_cost <= 0:
            continue

        pct = normalize_top15_cache_return_pct(r.get("報酬率"))
        if pct is None:
            pct = normalize_top15_cache_return_pct(r.get("報酬率文字"))

        result[(underlying, broker)] = {
            "return_pct": pct,
            "remaining_cost": remaining_cost,
            "estimated_cost": estimated_cost,
            "missing_price_cost": missing_price_cost,
            "market_value": safe_float(r.get("目前市值"), 0),
            "unrealized_pnl": safe_float(r.get("未實現損益"), None),
            "coverage_pct": normalize_return_pct(r.get("價格覆蓋率")) if r.get("價格覆蓋率") not in (None, "") else None,
            "latest_price_date": strip_gsheet_text_prefix(r.get("最新價格日期", "")),
            "event": strip_gsheet_text_prefix(r.get("事件", "")),
            "warrant_count": safe_int(r.get("權證檔數"), 0),
            "warrant_list": strip_gsheet_text_prefix(r.get("權證清單", "")),
        }

    return result


def _pick_first_existing_value(row, candidates: list[str]):
    """
    從同一列依序挑第一個有值的欄位。
    """
    for col in candidates:
        try:
            raw = row.get(col)
        except Exception:
            raw = None
        s = strip_gsheet_text_prefix(raw)
        if s and s != "-":
            return raw
    return ""


def _normalize_gsheet_col_name(name: str) -> str:
    s = strip_gsheet_text_prefix(name)
    s = str(s).strip()
    return s.replace("％", "%").replace(" ", "")


def _pick_first_existing_value_fuzzy(row, candidates: list[str]):
    """
    先用精確欄名抓取；若抓不到，再以「忽略空白、%/％差異」方式模糊比對欄名。
    主要用於 Google Sheet 欄名可能出現全形百分比或微小空白差異的情況。
    """
    direct = _pick_first_existing_value(row, candidates)
    if strip_gsheet_text_prefix(direct) not in ("", "-"):
        return direct

    try:
        keys = list(row.keys())
    except Exception:
        return direct

    norm_to_real = {}
    for k in keys:
        nk = _normalize_gsheet_col_name(k)
        if nk and nk not in norm_to_real:
            norm_to_real[nk] = k

    for col in candidates:
        real_col = norm_to_real.get(_normalize_gsheet_col_name(col))
        if not real_col:
            continue
        try:
            raw = row.get(real_col)
        except Exception:
            raw = None
        s = strip_gsheet_text_prefix(raw)
        if s and s != "-":
            return raw
    return direct


def _pick_first_existing_date(row, candidates: list[str]) -> date | None:
    """
    從同一列依序挑第一個可解析的日期欄位。
    """
    for col in candidates:
        try:
            d = parse_date_value(row.get(col))
        except Exception:
            d = None
        if d:
            return d
    return None


def _parse_period_text_to_dates(period_text: str) -> tuple[date | None, date | None]:
    """
    從「2026/05/08 ～ 2026/06/09」這類統計期間文字抓開始 / 結束日期。
    """
    s = strip_gsheet_text_prefix(period_text)
    if not s:
        return None, None

    matches = re.findall(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", s)
    if len(matches) >= 2:
        return parse_date_value(matches[0]), parse_date_value(matches[1])
    if len(matches) == 1:
        d = parse_date_value(matches[0])
        return d, d
    return None, None


def _parse_top15_broker_json(value) -> list[tuple[str, float, float | None, str]]:
    """
    解析「快取_TOP15共識淨買超」的分點明細_JSON。

    回傳格式：
        [(分點, 淨買超成本, 報酬率, 估值符號), ...]

    估值符號「*」代表當日成交價覆蓋成本低於設定門檻；
    無成交部位已優先使用權證資訊揭露平台 LP 委買價估值。
    """
    raw = strip_gsheet_text_prefix(value)
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except Exception:
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    out = []
    for rec in data:
        if not isinstance(rec, dict):
            continue

        broker = str(rec.get("分點", "") or rec.get("券商", "") or rec.get("broker", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        amount = safe_float(
            rec.get("淨買超成本", rec.get("剩餘成本", rec.get("remaining_cost", 0))),
            0,
        )
        if amount <= 0:
            continue

        return_pct = normalize_top15_cache_return_pct(
            rec.get("報酬率", rec.get("報酬率文字", rec.get("return_pct", "")))
        )

        # 若 JSON 沒有直接給報酬率，就用未實現損益 / 可估成本回推。
        if return_pct is None:
            estimated_cost = safe_float(rec.get("可估成本", rec.get("estimated_cost", 0)), 0)
            pnl = safe_float(rec.get("未實現損益", rec.get("unrealized_pnl", 0)), 0)
            if estimated_cost > 0:
                return_pct = round(pnl / estimated_cost * 100, 2)

        coverage_pct = safe_float(rec.get("成交價覆蓋率"), None)
        if coverage_pct is None:
            traded_cost = safe_float(rec.get("成交價成本"), None)
            if traded_cost is not None and amount > 0:
                coverage_pct = traded_cost / amount * 100

        marker = strip_gsheet_text_prefix(rec.get("估值符號", ""))
        if (
            not marker
            and return_pct is not None
            and coverage_pct is not None
            and coverage_pct < TOP15_TRADED_PRICE_COVERAGE_NOTE_THRESHOLD_PCT
        ):
            marker = TOP15_LOW_TRADED_COVERAGE_SYMBOL

        out.append((broker, amount, return_pct, marker))

    out.sort(key=lambda x: x[1], reverse=True)
    return out

def _parse_top15_broker_text(value) -> list[tuple[str, float, float | None, str]]:
    """
    備援解析「參與分點明細」文字。
    常見格式：元大南屯 123.4萬（+5.20%｜A｜3檔）；...
    """
    raw = strip_gsheet_text_prefix(value)
    if not raw:
        return []

    out = []
    for part in re.split(r"[；;]", raw):
        part = part.strip()
        if not part:
            continue

        broker = ""
        for b in TRACKED_BROKERS:
            if part.startswith(b):
                broker = b
                break
        if not broker:
            continue

        amount = 0.0
        m_amount = re.search(r"([+-]?\d+(?:\.\d+)?)\s*萬", part)
        if m_amount:
            amount = safe_float(m_amount.group(1), 0) * 10000

        return_pct = None
        m_ret = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", part)
        if m_ret:
            return_pct = normalize_top15_cache_return_pct(m_ret.group(1))

        marker = TOP15_LOW_TRADED_COVERAGE_SYMBOL if TOP15_LOW_TRADED_COVERAGE_SYMBOL in part else ""
        if amount > 0:
            out.append((broker, amount, return_pct, marker))

    out.sort(key=lambda x: x[1], reverse=True)
    return out


def read_top15_position_detail_cache_from_gsheet(target: date | None = None) -> dict[str, list[tuple[str, float, float | None]]]:
    """
    讀取新版「快取_TOP15部位明細」，彙總成：
        {標的股: [(分點, 剩餘成本, 報酬率), ...]}

    近一個月 TOP15 圖固定只讀 資料範圍=精選五分點。
    若新版工作表不存在或沒有資料，直接回傳空結果，
    不再讀取舊版「快取_TOP15分點報酬率」。
    """
    needed_cols = [
        "資料範圍",
        "統計日期", "日期", "目標日期",
        "分點", "分點名稱", "券商代號",
        "標的股", "標的代號", "標的", "標的名稱", "股票名稱",
        "剩餘成本", "目前剩餘成本", "淨買超成本", "remaining_cost",
        "目前市值", "未實現損益", "可估成本", "缺價格成本",
        "報酬率", "報酬率文字", "價格狀態", "估值價格來源",
    ]

    df = read_gsheet_table_optional(
        SHEET_TOP15_POSITION_DETAIL,
        needed_cols,
        filter_tracked_brokers=False,
    )
    df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)

    if df.empty:
        print(
            f"  ⚠️ 找不到新版 TOP15 部位快取："
            f"{SHEET_TOP15_POSITION_DETAIL}，"
            "本次不再回退讀取舊版快取_TOP15分點報酬率。"
        )
        return {}

    def pick_cache_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    available_dates = []
    for _, r in df.iterrows():
        d = pick_cache_date(r)
        if d:
            available_dates.append(d)

    chosen_date = None
    if available_dates:
        if target is not None:
            valid = [d for d in available_dates if d <= target]
            chosen_date = max(valid) if valid else max(available_dates)
        else:
            chosen_date = max(available_dates)

    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "cost": 0.0,
        "estimated_cost": 0.0,
        "traded_price_cost": 0.0,
        "lp_quote_cost": 0.0,
        "pnl": 0.0,
        "weighted_return_cost": 0.0,
        "weighted_return_sum": 0.0,
    })

    for _, r in df.iterrows():
        row_date = pick_cache_date(r)
        if chosen_date and row_date and row_date != chosen_date:
            continue

        broker = str(_pick_first_existing_value(r, ["分點", "分點名稱"])).strip()
        if broker not in TRACKED_BROKERS:
            continue

        target_text = _pick_first_existing_value(r, ["標的名稱", "股票名稱", "標的"])
        underlying = normalize_underlying(
            _pick_first_existing_value(r, ["標的股", "標的代號", "標的"]),
            target_text,
        )

        if not underlying:
            raw_target = strip_gsheet_text_prefix(target_text)
            m = re.match(r"^(\d{1,4})", raw_target)
            if m:
                underlying = normalize_underlying(m.group(1), raw_target)

        if not underlying:
            continue

        amount = safe_float(
            _pick_first_existing_value(r, ["剩餘成本", "目前剩餘成本", "淨買超成本", "remaining_cost"]),
            0,
        )
        if amount <= 0:
            continue

        estimated_cost = safe_float(_pick_first_existing_value(r, ["可估成本"]), 0)
        if estimated_cost <= 0 and strip_gsheet_text_prefix(r.get("目前市值", "")):
            estimated_cost = amount

        pnl = safe_float(r.get("未實現損益"), 0)
        return_pct = normalize_top15_cache_return_pct(
            _pick_first_existing_value(r, ["報酬率", "報酬率文字"])
        )

        rec = agg[(underlying, broker)]
        rec["cost"] += amount
        rec["estimated_cost"] += estimated_cost
        price_source = strip_gsheet_text_prefix(r.get("估值價格來源", ""))
        if price_source == "當日成交價":
            rec["traded_price_cost"] += amount
        elif price_source == "LP流動量提供者委買價":
            rec["lp_quote_cost"] += amount
        rec["pnl"] += pnl

        if return_pct is not None and amount > 0:
            rec["weighted_return_sum"] += return_pct * amount
            rec["weighted_return_cost"] += amount

    by_underlying: dict[str, list[tuple[str, float, float | None]]] = defaultdict(list)

    for (underlying, broker), rec in agg.items():
        amount = safe_float(rec.get("cost"), 0)
        if amount <= 0:
            continue

        estimated_cost = safe_float(rec.get("estimated_cost"), 0)
        pnl = safe_float(rec.get("pnl"), 0)
        return_pct = None

        if estimated_cost > 0:
            return_pct = round(pnl / estimated_cost * 100, 2)
        elif safe_float(rec.get("weighted_return_cost"), 0) > 0:
            return_pct = round(rec["weighted_return_sum"] / rec["weighted_return_cost"], 2)

        traded_price_cost = safe_float(rec.get("traded_price_cost"), 0)
        traded_coverage_pct = traded_price_cost / amount * 100 if amount > 0 else None
        marker = (
            TOP15_LOW_TRADED_COVERAGE_SYMBOL
            if return_pct is not None
            and traded_coverage_pct is not None
            and traded_coverage_pct < TOP15_TRADED_PRICE_COVERAGE_NOTE_THRESHOLD_PCT
            else ""
        )
        by_underlying[underlying].append((broker, amount, return_pct, marker))

    for underlying in by_underlying:
        by_underlying[underlying].sort(key=lambda x: x[1], reverse=True)

    return dict(by_underlying)

def read_top15_consensus_cache_from_gsheet(target: date | None = None) -> tuple[list[dict], dict]:
    """
    直接讀取新版「快取_TOP15共識淨買超」。

    這是近一個月 TOP15 圖的唯一排名來源；圖片端不再重新計算 A/B/C/D/E。
    近一個月 TOP15 圖固定只讀 資料範圍=精選五分點。
    """
    needed_cols = [
        "資料範圍",
        "統計日期", "日期", "目標日期", "統計期間", "有效交易日數",
        "排名", "rank",
        "標的股", "標的代號", "標的", "標的名稱", "股票名稱",
        "淨買超成本", "剩餘淨買超成本", "淨買超金額", "淨買超", "remaining_cost",
        "買超成本", "原始買超成本", "總買超成本", "買超金額", "合計買超成本", "amount",
        "目前市值", "未實現損益", "報酬率", "報酬率文字",
        "價格覆蓋率", "價格覆蓋率文字",
        "參與分點數", "分點數", "broker_count",
        "參與分點明細", "分點明細_JSON",
        "事件", "事件代號", "事件類型",
        "權證檔數", "權證清單", "最新價格日期", "資料狀態", "完成狀態", "更新時間", "run_id",
        "第一筆日期", "起始日", "開始日", "first_date",
        "最後筆日期", "結束日", "截止日", "last_date",
    ]

    df = read_gsheet_table_optional(
        SHEET_TOP15_CONSENSUS_CACHE,
        needed_cols,
        filter_tracked_brokers=False,
    )
    df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)

    empty_meta = {
        "effective_days": 0,
        "start_date": None,
        "end_date": None,
        "chosen_date": None,
    }

    if df.empty:
        return [], empty_meta

    def pick_cache_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    available_dates = []
    for _, r in df.iterrows():
        d = pick_cache_date(r)
        if d:
            available_dates.append(d)

    if not available_dates:
        return [], empty_meta

    if target is not None:
        valid = [d for d in available_dates if d <= target]
        chosen_date = max(valid) if valid else max(available_dates)
    else:
        chosen_date = max(available_dates)

    df = df[df.apply(lambda r: pick_cache_date(r) == chosen_date, axis=1)].copy()
    if df.empty:
        return [], empty_meta

    position_map = read_top15_position_detail_cache_from_gsheet(chosen_date)

    first_row = df.iloc[0].to_dict()
    period_text = strip_gsheet_text_prefix(first_row.get("統計期間", ""))
    period_start, period_end = _parse_period_text_to_dates(period_text)

    start_date = _pick_first_existing_date(first_row, ["第一筆日期", "起始日", "開始日", "first_date"]) or period_start
    end_date = _pick_first_existing_date(first_row, ["最後筆日期", "結束日", "截止日", "last_date"]) or period_end
    effective_days = safe_int(_pick_first_existing_value(first_row, ["有效交易日數", "有效日期數", "統計天數", "交易日數"]), 0)

    rows = []

    for _, r in df.iterrows():
        row = r.to_dict()

        stock_name = strip_gsheet_text_prefix(
            _pick_first_existing_value(row, ["標的名稱", "股票名稱"])
        )
        raw_target = strip_gsheet_text_prefix(_pick_first_existing_value(row, ["標的", "標的名稱", "股票名稱"]))

        underlying = normalize_underlying(
            _pick_first_existing_value(row, ["標的股", "標的代號", "標的"]),
            stock_name or raw_target,
        )

        if not underlying and raw_target:
            m = re.match(r"^(\d{1,4})\s*(.*)$", raw_target)
            if m:
                underlying = normalize_underlying(m.group(1), raw_target)
                if not stock_name:
                    stock_name = m.group(2).strip()

        if not underlying:
            continue

        if not stock_name:
            stock_name = get_stock_name_map().get(underlying, "")

        target_label = f"{underlying} {stock_name}".strip() if stock_name else underlying

        participant_brokers = _parse_top15_broker_json(row.get("分點明細_JSON", ""))
        if not participant_brokers:
            participant_brokers = position_map.get(underlying, [])
        if not participant_brokers:
            participant_brokers = _parse_top15_broker_text(row.get("參與分點明細", ""))

        net_amount = safe_float(
            _pick_first_existing_value(row, ["淨買超成本", "剩餘淨買超成本", "淨買超金額", "淨買超", "remaining_cost"]),
            0,
        )
        if net_amount <= 0 and participant_brokers:
            net_amount = sum(safe_float(x[1], 0) for x in participant_brokers)

        amount = safe_float(
            _pick_first_existing_value(row, ["買超成本", "原始買超成本", "總買超成本", "買超金額", "合計買超成本", "amount"]),
            0,
        )
        if amount <= 0:
            amount = net_amount

        if net_amount <= 0:
            continue

        broker_count = safe_int(
            _pick_first_existing_value(row, ["參與分點數", "分點數", "broker_count"]),
            0,
        )
        if broker_count <= 0 and participant_brokers:
            broker_count = len(participant_brokers)

        events = strip_gsheet_text_prefix(_pick_first_existing_value(row, ["事件", "事件代號", "事件類型"]))
        if not events and participant_brokers:
            events = "-"

        top_broker = ""
        top_broker_amount = 0.0
        if participant_brokers:
            top_broker = participant_brokers[0][0]
            top_broker_amount = safe_float(participant_brokers[0][1], 0)

        row_first_date = _pick_first_existing_date(row, ["第一筆日期", "起始日", "開始日", "first_date"]) or start_date
        row_last_date = _pick_first_existing_date(row, ["最後筆日期", "結束日", "截止日", "last_date"]) or end_date

        warrant_count = safe_int(_pick_first_existing_value(row, ["權證檔數"]), 0)
        if warrant_count <= 0:
            warrant_count = count_warrants_in_text(_pick_first_existing_value(row, ["權證清單"])) or 1

        rows.append({
            "rank": safe_int(_pick_first_existing_value(row, ["排名", "rank"]), 0),
            "target": target_label,
            "underlying": underlying,
            "stock_name": stock_name,
            "amount": amount,
            "net_amount": net_amount,
            "count": warrant_count,
            "broker_count": broker_count,
            "brokers": [x[0] for x in participant_brokers],
            "events": events,
            "top_broker": top_broker,
            "top_broker_amount": top_broker_amount,
            "participant_brokers": participant_brokers,
            "first_date": row_first_date,
            "last_date": row_last_date,
        })

    has_rank = any(safe_int(x.get("rank"), 0) > 0 for x in rows)
    if has_rank:
        rows.sort(key=lambda x: (safe_int(x.get("rank"), 999999), -safe_float(x.get("net_amount"), 0)))
    else:
        rows.sort(key=lambda x: (safe_float(x.get("net_amount"), 0), safe_float(x.get("amount"), 0)), reverse=True)

    rows = rows[:15]

    if start_date is None:
        starts = [r.get("first_date") for r in rows if r.get("first_date")]
        if starts:
            start_date = min(starts)

    if end_date is None:
        ends = [r.get("last_date") for r in rows if r.get("last_date")]
        if ends:
            end_date = max(ends)

    meta = {
        "effective_days": effective_days,
        "start_date": start_date,
        "end_date": end_date,
        "chosen_date": chosen_date,
    }

    print(
        f"  ✅ 近一個月 TOP15 直接讀取快取：{SHEET_TOP15_CONSENSUS_CACHE}｜"
        f"資料範圍：{DATA_SCOPE_SELECTED5}｜統計日期：{chosen_date:%Y-%m-%d}｜筆數：{len(rows)}"
    )

    return rows, meta


# ══════════════════════════════════════════════════════════════════════
# 日期推斷 / 勝率統計
# ══════════════════════════════════════════════════════════════════════


def extract_stock_name_from_warrant_text(text: str) -> str:
    """
    從權證名稱或權證清單中的單筆名稱，推估標的股名。
    例如：
    - 聯發科元大5B購04 -> 聯發科
    - 047358 台積電永豐6C購01 -> 台積電
    """
    s = strip_gsheet_text_prefix(text).strip()
    if not s:
        return ""

    # 若是一整串清單，取第一筆非空項目
    for sep in ["；", ";"]:
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if parts:
                s = parts[0]
                break

    # 去掉前面的權證代碼，如 047358
    s = re.sub(r"^\d+\s*", "", s).strip()
    if not s:
        return ""

    # 找最早出現的券商關鍵字，前面那段通常就是股名
    hit_idx = None
    for token in WARRANT_ISSUER_TOKENS:
        idx = s.find(token)
        if idx > 0:
            if hit_idx is None or idx < hit_idx:
                hit_idx = idx

    if hit_idx is not None:
        name = s[:hit_idx].strip()
        # 避免抓到過長雜訊
        if 0 < len(name) <= 12:
            return name

    return ""


def build_stock_name_map_from_gsheet() -> dict[str, str]:
    """
    從新制 A／B／C／D／E 工作表蒐集「標的股代碼 -> 股名」映射。

    新制五張表都以「權證清單」保存事件內所有權證，因此不再把 A 表當成
    單一權證特殊格式處理。
    """
    name_counter: dict[str, Counter] = defaultdict(Counter)

    def add_mapping(underlying, text_value):
        code = normalize_underlying(underlying, text_value)
        if not code:
            return
        name = extract_stock_name_from_warrant_text(text_value)
        if name:
            mapped_code = KNOWN_UNDERLYING_NAME_CODE_MAP.get(name)
            if mapped_code:
                code = mapped_code
            name_counter[code][name] += 1

    for _event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                ["資料範圍", "標的股", "權證清單", "最大單筆權證"],
                filter_tracked_brokers=False,
            )
        except Exception:
            continue

        for _, row in df.iterrows():
            warrant_text = _first_non_empty_value(
                row.get("權證清單", ""),
                row.get("最大單筆權證", ""),
            )
            add_mapping(row.get("標的股", ""), warrant_text)

    stock_map = {}
    for code, counter in name_counter.items():
        if counter:
            best_name = sorted(
                counter.items(),
                key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
            )[0][0]
            stock_map[code] = best_name

    return stock_map


def get_stock_name_map() -> dict[str, str]:
    global _STOCK_NAME_MAP
    if _STOCK_NAME_MAP is None:
        _STOCK_NAME_MAP = build_stock_name_map_from_gsheet()
    return _STOCK_NAME_MAP

def get_stock_code_by_name_map() -> dict[str, str]:
    global _STOCK_CODE_BY_NAME_MAP
    if _STOCK_CODE_BY_NAME_MAP is None:
        code_map: dict[str, str] = {}

        for code, name in get_stock_name_map().items():
            nm = strip_gsheet_text_prefix(name).strip()
            if nm and nm not in code_map:
                code_map[nm] = code

        for nm, code in KNOWN_UNDERLYING_NAME_CODE_MAP.items():
            nm = strip_gsheet_text_prefix(nm).strip()
            code = normalize_underlying(code, nm)
            if nm and code and nm not in code_map:
                code_map[nm] = code

        _STOCK_CODE_BY_NAME_MAP = code_map
    return _STOCK_CODE_BY_NAME_MAP


def get_stock_code_by_name(name: str) -> str:
    nm = strip_gsheet_text_prefix(name).strip()
    if not nm:
        return ""

    direct = get_stock_code_by_name_map().get(nm, "")
    if direct:
        return direct

    nm2 = nm.replace("臺", "台").replace(" ", "")
    for key, code in get_stock_code_by_name_map().items():
        key2 = str(key).replace("臺", "台").replace(" ", "")
        if key2 == nm2:
            return code
    return ""


def looks_like_warrant_code(code_value) -> bool:
    s = normalize_code(code_value)
    if not s or not s.isdigit():
        return False

    # 台股標的股通常為 4 碼；ETF 代號常見為 00 開頭。
    # 若是 5~6 碼且不是 00 開頭，極可能是權證代號而不是標的股代號。
    return len(s) in (5, 6) and not s.startswith("00")


def resolve_target_identity(
    row: dict,
    code_cols: list[str],
    name_cols: list[str],
    raw_target_cols: list[str],
    warrant_text_cols: list[str] | None = None,
) -> tuple[str, str, str]:
    """
    從快取列中盡量解析出正確的「標的代號、標的名稱、顯示文字」。

    主要是修正某些快取欄位把「權證代號」誤塞進標的欄位，
    導致圖卡顯示成 501504 這種權證代碼，而不是正確標的名稱。
    """
    warrant_text_cols = warrant_text_cols or []

    raw_name = strip_gsheet_text_prefix(_pick_first_existing_value(row, name_cols)).strip()
    raw_target = strip_gsheet_text_prefix(_pick_first_existing_value(row, raw_target_cols)).strip()
    warrant_text = strip_gsheet_text_prefix(_pick_first_existing_value(row, warrant_text_cols + raw_target_cols + name_cols)).strip()

    underlying = normalize_underlying(_pick_first_existing_value(row, code_cols), raw_name or raw_target or warrant_text)
    underlying_name = raw_name

    parsed_target_code = ""
    parsed_target_name = ""
    m_target = re.match(r"^(\d{4})(?:\s+|[-_])?(.*)$", raw_target)
    if m_target:
        parsed_target_code = normalize_underlying(m_target.group(1), raw_target)
        parsed_target_name = m_target.group(2).strip()

    inferred_name = extract_stock_name_from_warrant_text(warrant_text)

    if not underlying_name and parsed_target_name:
        underlying_name = parsed_target_name
    if not underlying_name and inferred_name:
        underlying_name = inferred_name

    if (not underlying or looks_like_warrant_code(underlying)) and parsed_target_code and not looks_like_warrant_code(parsed_target_code):
        underlying = parsed_target_code

    if underlying and not looks_like_warrant_code(underlying):
        mapped_name = get_stock_name_map().get(underlying, "")
        if not underlying_name and mapped_name:
            underlying_name = mapped_name

    if underlying_name and (not underlying or looks_like_warrant_code(underlying)):
        mapped_code = get_stock_code_by_name(underlying_name)
        if mapped_code:
            underlying = mapped_code

    if looks_like_warrant_code(underlying) and underlying_name:
        mapped_code = get_stock_code_by_name(underlying_name)
        underlying = mapped_code or ""

    if underlying_name and underlying:
        target_label = f"{underlying} {underlying_name}".strip()
    elif underlying_name:
        target_label = underlying_name
    else:
        target_label = raw_target or underlying

    return underlying, underlying_name, target_label


def infer_latest_date_from_gsheet() -> date:
    """從新制 A～E 事件表與每日賣出明細推斷最新可用日期。"""
    candidates = []

    for _event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                ["資料範圍", "事件日", "減碼日", "出清日"],
                filter_tracked_brokers=False,
            )
        except Exception:
            continue

        for col in ["事件日", "減碼日", "出清日"]:
            if col not in df.columns:
                continue
            for value in df[col].dropna().tolist():
                parsed = parse_date_value(value)
                if parsed:
                    candidates.append(parsed)

    # 若當日只有賣出、沒有新 A～E 事件，仍可由每日賣出明細取得最新日期。
    try:
        sell_df = read_gsheet_table_optional(
            SHEET_DAILY_SELL,
            ["日期"],
            filter_tracked_brokers=False,
        )
        for value in sell_df.get("日期", pd.Series(dtype=object)).tolist():
            parsed = parse_date_value(value)
            if parsed:
                candidates.append(parsed)
    except Exception:
        pass

    if not candidates:
        raise RuntimeError(
            "無法從 Google Sheet 的 A～E 或每日賣出明細推斷日期，"
            "請用 TARGET_DATE=YYYY-MM-DD 指定。"
        )

    return max(candidates)


def read_history_stats_from_gsheet() -> dict:
    """讀取新制勝率統計中的「全部 A+B+C+D+E 合併」資料。"""
    result = {
        broker: {
            "total_events": 0,
            "win_rate": 0.0,
            "avg_hold_days": 0.0,
        }
        for broker in TRACKED_BROKERS
    }

    try:
        stat = read_gsheet_stat_raw()
    except Exception:
        return result

    if stat.empty or stat.shape[1] < 10:
        return result

    broker_series = stat[0].astype(str).str.strip()
    event_series = stat[1].astype(str).str.strip()
    total_mask = event_series.map(lambda value: _classify_stat_event(value) == "total")

    for broker in TRACKED_BROKERS:
        rows = stat[(broker_series == broker) & total_mask]
        if rows.empty:
            continue
        row = rows.iloc[0]
        result[broker] = {
            "total_events": safe_int(row[2]),
            "win_rate": safe_float(row[8]),
            "avg_hold_days": safe_float(row[9]),
        }

    return result


def read_selected5_history_from_abcd() -> dict:
    """
    從新制 A／B／C／D／E 計算精選五分點平均持有天數。

    保留原函式名稱，避免既有 main() 與外部呼叫需要同步改名。
    """
    hold_days_map: dict[str, list[float]] = defaultdict(list)

    for _event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                ["資料範圍", "分點", "持有天數"],
            )
            df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)
        except Exception:
            continue

        if df.empty or "持有天數" not in df.columns:
            continue

        for _, row in df.iterrows():
            broker = str(row.get("分點", "")).strip()
            if broker not in TRACKED_BROKERS:
                continue

            raw_hold_days = strip_gsheet_text_prefix(row.get("持有天數", ""))
            if raw_hold_days in ("", "-"):
                continue

            hold_days = safe_float(raw_hold_days, None)
            if hold_days is None or hold_days < 0:
                continue

            hold_days_map[broker].append(hold_days)

    result = {}
    for broker in TRACKED_BROKERS:
        values = hold_days_map.get(broker, [])
        avg_hold_days = sum(values) / len(values) if values else 0.0
        result[broker] = {
            "total_events": 0,
            "win_rate": 0.0,
            "avg_hold_days": avg_hold_days,
        }

    return result


def _normalize_stat_header_name(value) -> str:
    """統一勝率統計表欄名，忽略空白、全半形百分比與常見符號差異。"""
    s = strip_gsheet_text_prefix(value)
    s = str(s).strip().replace("％", "%")
    s = re.sub(r"[\s\n\r\t_\-－—:：｜|/\\()（）\[\]【】%]+", "", s)
    return s


def _build_stat_header_map(values: list) -> dict[str, int]:
    """
    從勝率統計表的一列辨識欄位位置。

    勝率統計可能包含標題列、空白列或多層表頭，因此不依賴固定的單一表頭列。
    """
    result: dict[str, int] = {}

    for idx, value in enumerate(values):
        name = _normalize_stat_header_name(value)
        if not name:
            continue

        if name in {"資料範圍", "範圍", "模式"}:
            result.setdefault("scope", idx)
        elif name in {"分點", "分點名稱", "券商分點", "券商名稱"}:
            result.setdefault("broker", idx)
        elif name in {"事件類型", "事件", "統計類型", "訊號類型"}:
            result.setdefault("event", idx)
        elif "加權" in name and ("報酬" in name or "損益" in name):
            result.setdefault("weighted_return", idx)
        elif "平均持有" in name and ("天" in name or "日" in name):
            result.setdefault("avg_hold_days", idx)
        elif name in {"事件數", "事件筆數", "總事件數", "訊號數", "樣本數"}:
            result.setdefault("event_count", idx)
        elif name in {"勝率", "總勝率", "勝率百分比"} or name.endswith("勝率"):
            result.setdefault("win_rate", idx)

    return result


def _parse_stat_win_rate(value):
    """將勝率欄位統一轉成百分比數值，例如 72.5% -> 72.5、0.725 -> 72.5。"""
    raw = strip_gsheet_text_prefix(value)
    if raw in ("", "-"):
        return None

    number = safe_float(raw, None)
    if number is None:
        return None

    # get_all_values 通常會讀到格式化後的 72.5%；若來源是 0.725，才轉成 72.5。
    if "%" not in raw and "％" not in raw and 0 < abs(number) <= 1:
        number *= 100.0

    return number


def _parse_stat_plain_number(value):
    """解析平均持有天數或加權報酬率；空白回傳 None。"""
    raw = strip_gsheet_text_prefix(value)
    if raw in ("", "-"):
        return None
    return safe_float(raw, None)


def _classify_stat_event(value) -> str:
    """將勝率統計表事件名稱分類成 A 或 total；其他事件不納入本圖。"""
    raw = strip_gsheet_text_prefix(value).strip()
    compact = re.sub(r"[\s_－—｜|/\\]+", "", raw)
    upper = compact.upper()

    if not compact:
        return ""

    if (
        "全部" in compact
        or "總計" in compact
        or "合併" in compact
        or "ABCDE" in upper
        or "A+B+C+D+E" in upper
        or "ABCD" in upper
        or "A+B+C+D" in upper
    ):
        return "total"

    if upper == "A" or (
        upper.startswith("A")
        and ("基礎買超" in compact or "基礎" in compact or "單檔" in compact)
    ):
        return "A"

    return ""


def _stat_scope_priority(value) -> int:
    """同一分點若同時存在精選五分點與全分點資料，優先採用全分點列。"""
    raw = strip_gsheet_text_prefix(value).strip()
    if raw == DATA_SCOPE_ALL or "全分點" in raw:
        return 3
    if not raw:
        return 2
    if raw == DATA_SCOPE_SELECTED5 or "精選五分點" in raw or "五分點" in raw:
        return 1
    return 2


def read_all_broker_win_rate_stats_from_gsheet() -> list[dict]:
    """
    讀取 Google Sheet「勝率統計」，整理所有分點的：
    - A：單檔權證大買勝率
    - 總勝率：全部 A+B+C+D+E 合併
    - A 事件數：採 A 列
    - 總事件數：採全部 A+B+C+D+E 合併列
    - 平均持有天數：採總計列，總計缺值時才用 A 列備援
    - 加權報酬率：採總計列，總計缺值時才用 A 列備援

    表頭採名稱辨識，不把欄位位置寫死；若舊版工作表沒有可辨識表頭，
    才沿用既有欄位位置：勝率第 9 欄、平均持有天數第 10 欄、加權報酬率第 11 欄。
    """
    try:
        stat = read_gsheet_stat_raw()
    except Exception as exc:
        print(f"  ⚠️ 讀取 {SHEET_STAT} 失敗：{exc}")
        return []

    if stat.empty:
        return []

    # 勝率統計可能是多層表頭，先掃過整張表，合併所有可辨識欄位位置。
    global_header_map: dict[str, int] = {}
    for _, row in stat.iterrows():
        row_map = _build_stat_header_map(row.tolist())
        for key, idx in row_map.items():
            global_header_map.setdefault(key, idx)

    broker_idx = global_header_map.get("broker", 0)
    event_idx = global_header_map.get("event", 1)
    scope_idx = global_header_map.get("scope")
    win_rate_idx = global_header_map.get("win_rate", 8)
    avg_hold_idx = global_header_map.get("avg_hold_days", 9)
    event_count_idx = global_header_map.get("event_count", 2)
    weighted_return_idx = global_header_map.get("weighted_return", 10)

    broker_order: list[str] = []
    by_broker: dict[str, dict] = {}
    current_scope = ""

    def get_value(values: list, idx: int | None):
        if idx is None or idx < 0 or idx >= len(values):
            return ""
        return values[idx]

    for _, row in stat.iterrows():
        values = [strip_gsheet_text_prefix(x) for x in row.tolist()]
        joined = " ".join(str(x) for x in values if str(x).strip())

        # 支援以獨立標題列區分「精選五分點 / 全分點」的工作表格式。
        if "全分點" in joined and _classify_stat_event(get_value(values, event_idx)) == "":
            current_scope = DATA_SCOPE_ALL
        elif ("精選五分點" in joined or "五分點" in joined) and _classify_stat_event(get_value(values, event_idx)) == "":
            current_scope = DATA_SCOPE_SELECTED5

        broker = str(get_value(values, broker_idx)).strip()
        event_kind = _classify_stat_event(get_value(values, event_idx))
        if not broker or broker in {"分點", "分點名稱", "券商分點"} or not event_kind:
            continue

        row_scope = str(get_value(values, scope_idx)).strip() if scope_idx is not None else current_scope
        priority = _stat_scope_priority(row_scope)

        metric = {
            "event_count": safe_int(get_value(values, event_count_idx), 0),
            "win_rate": _parse_stat_win_rate(get_value(values, win_rate_idx)),
            "avg_hold_days": _parse_stat_plain_number(get_value(values, avg_hold_idx)),
            "weighted_return": _parse_stat_plain_number(get_value(values, weighted_return_idx)),
            "scope": row_scope,
            "priority": priority,
        }

        if broker not in by_broker:
            by_broker[broker] = {"A": None, "total": None}
            broker_order.append(broker)

        old = by_broker[broker].get(event_kind)
        if old is None or priority > safe_int(old.get("priority"), 0):
            by_broker[broker][event_kind] = metric
        elif priority == safe_int(old.get("priority"), 0):
            # 同一優先層級重複時，保留資訊較完整的列。
            old_score = sum(old.get(k) not in (None, 0, "") for k in ["event_count", "win_rate", "avg_hold_days", "weighted_return"])
            new_score = sum(metric.get(k) not in (None, 0, "") for k in ["event_count", "win_rate", "avg_hold_days", "weighted_return"])
            if new_score > old_score:
                by_broker[broker][event_kind] = metric

    rows: list[dict] = []
    for broker in broker_order:
        a_metric = by_broker[broker].get("A") or {}
        total_metric = by_broker[broker].get("total") or {}

        a_event_count = safe_int(a_metric.get("event_count"), 0)
        total_event_count = safe_int(total_metric.get("event_count"), 0)

        avg_hold_days = total_metric.get("avg_hold_days")
        if avg_hold_days is None:
            avg_hold_days = a_metric.get("avg_hold_days")

        weighted_return = total_metric.get("weighted_return")
        if weighted_return is None:
            weighted_return = a_metric.get("weighted_return")

        rows.append({
            "broker": broker,
            "a_win_rate": a_metric.get("win_rate"),
            "a_event_count": a_event_count,
            "total_win_rate": total_metric.get("win_rate"),
            "total_event_count": total_event_count,
            # 保留舊欄位名稱供其他既有呼叫相容；其內容仍代表全部 A+B+C+D+E 合併事件數。
            "event_count": total_event_count,
            "avg_hold_days": avg_hold_days,
            "weighted_return": weighted_return,
        })

    print(
        f"  ✅ 已讀取 {SHEET_STAT} 全分點統計：{len(rows):,} 個分點｜"
        f"A 勝率有效 {sum(r.get('a_win_rate') is not None for r in rows):,} 個｜"
        f"總勝率有效 {sum(r.get('total_win_rate') is not None for r in rows):,} 個"
    )
    return rows


# ══════════════════════════════════════════════════════════════════════
# 買賣資料抽取
# ══════════════════════════════════════════════════════════════════════

def append_buy(buys: list[dict], broker: str, event: str, underlying, warrant_name: str,
               amount: float, qty: int, sheet_name: str, warrant_code: str = ""):
    if amount < BUY_THRESHOLD:
        return

    underlying_code = normalize_underlying(underlying, warrant_name)
    stock_name = get_stock_name_map().get(underlying_code, "")
    if not stock_name:
        stock_name = extract_stock_name_from_warrant_text(warrant_name)

    buys.append({
        "broker": str(broker).strip(),
        "event": event,
        "underlying": underlying_code,
        "stock_name": stock_name,
        "warrant_code": normalize_warrant_code(warrant_code),
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "warrant_list_count": count_warrants_in_text(warrant_name),
        "amount": amount,
        "qty": qty,
        "add_count": 0,
        "sheet": sheet_name,
    })


def dedupe_buy_actions(actions: list[dict]) -> list[dict]:
    """
    清理 A/B/C/D/E 買超事件可能重複列，避免今日買超明細金額被重複加總。

    設計原則：
    - 同一分點、同一事件、同一標的、同一權證 / 權證清單、同一來源工作表、同一金額與張數，視為重複同步資料。
    - 只移除完全相同的重複事件，不合併不同金額，避免誤刪真正不同的買超事件。
    """
    if not actions:
        return actions

    seen = set()
    out = []
    removed = 0

    for item in actions:
        warrant_key = str(item.get("warrant_code") or item.get("warrant") or "").strip()
        key = (
            str(item.get("broker", "")).strip(),
            str(item.get("event", "")).strip(),
            str(item.get("underlying", "")).strip(),
            warrant_key,
            str(item.get("sheet", "")).strip(),
            round(safe_float(item.get("amount"), 0), 4),
            round(safe_float(item.get("qty"), 0), 4),
        )

        if key in seen:
            removed += 1
            continue

        seen.add(key)
        out.append(item)

    if removed > 0:
        print(
            f"  ⚠️ 今日買超明細已去重：原始 {len(actions):,} 筆 → {len(out):,} 筆，"
            f"移除重複買超事件 {removed:,} 筆。"
        )

    return out


def append_sell(sells: list[dict], broker: str, status: str, event: str, underlying, warrant_name: str,
                amount: float, qty: int, sheet_name: str, return_pct=None, buy_amount=None,
                warrant_code: str = "", force_include: bool = False, defer_threshold: bool = False):
    # defer_threshold=True 用於「每日賣出明細」：
    # 先把同一天、同分點、同標的的賣出候選全部放進 sells，
    # 等 compress_actions() 合併後再由 draw_report_image() 用 SELL_THRESHOLD 做最終過濾。
    # 這樣可以避免多檔權證單筆低於 20 萬，但同標的合計超過門檻時被提前丟掉。
    if not defer_threshold and not force_include and amount < SELL_THRESHOLD and not (status == "出清" and DISPLAY_EXIT_ALWAYS):
        return

    underlying_code = normalize_underlying(underlying, warrant_name)
    stock_name = get_stock_name_map().get(underlying_code, "")
    if not stock_name:
        stock_name = extract_stock_name_from_warrant_text(warrant_name)

    sells.append({
        "broker": str(broker).strip(),
        "status": str(status).strip() or "賣超",
        "event": str(event).strip() or "未歸類",
        "underlying": underlying_code,
        "stock_name": stock_name,
        "warrant_code": normalize_warrant_code(warrant_code),
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "warrant_list_count": count_warrants_in_text(warrant_name),
        "amount": amount,                         # 實際賣出金額
        "buy_amount": safe_float(buy_amount, 0),  # 對應買進金額；每日賣出明細無法精準對應時保留 0
        "qty": qty,
        "return_pct": normalize_return_pct(return_pct),
        "sheet": sheet_name,
    })


def collect_broker_underlying_add_count_map(
    target: date,
    lookback_days: int = ADD_COUNT_LOOKBACK_TRADING_DAYS,
) -> dict[tuple[str, str], int]:
    """
    計算同一分點＋同一標的，在近 N 個有效事件交易日內出現新制 A～E
    買超事件的不同日期次數。

    同一天即使資料因同步重複出現，仍只算一次；已在目標日前出清的事件不納入。
    """
    trading_dates = collect_recent_buy_trading_dates(target, lookback_days)
    if not trading_dates:
        return {}

    date_set = set(trading_dates)
    counter: dict[tuple[str, str], set[date]] = defaultdict(set)

    for _event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                [
                    "資料範圍", "分點", "標的股", "事件日",
                    "單日累積買進金額", "買超金額", "權證清單", "出清日",
                ],
            )
            df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)
        except Exception:
            continue

        for _, row in df.iterrows():
            event_date = parse_date_value(row.get("事件日"))
            if not event_date or event_date not in date_set or event_date > target:
                continue

            exit_date = parse_date_value(row.get("出清日"))
            if exit_date and exit_date <= target:
                continue

            broker = str(row.get("分點", "")).strip()
            if broker not in TRACKED_BROKERS:
                continue

            warrant_text = row.get("權證清單", "")
            underlying = normalize_underlying(row.get("標的股", ""), warrant_text)
            if not underlying:
                continue

            amount = safe_float(
                _first_non_empty_value(
                    row.get("單日累積買進金額", ""),
                    row.get("買超金額", ""),
                ),
                0,
            )
            if amount < BUY_THRESHOLD:
                continue

            counter[(broker, underlying)].add(event_date)

    return {key: len(days) for key, days in counter.items()}


def normalize_event_code(v) -> str:
    s = strip_gsheet_text_prefix(v).strip()
    if not s or s == "-":
        return ""

    # 支援 A～E、A-基礎買超、E-超大額布局，以及 A/B/C 等合併文字。
    hits = []
    for code in ["A", "B", "C", "D", "E"]:
        if re.search(rf"(^|[^A-Z]){code}([^A-Z]|$)", s.upper()):
            hits.append(code)

    if hits:
        return "/".join(hits)

    return s


def is_unclassified_event(v) -> bool:
    s = normalize_event_code(v)
    return s in ["", "-", "未歸類", "ABCDE合計"]


def parse_warrant_items_from_text(text_value: str) -> list[tuple[str, str]]:
    """
    將「權證清單」拆成 [(權證代號, 權證名稱), ...]。
    例如：
    066145 聯發永豐63購02；066594 華邦富邦5C購02
    """
    s = strip_gsheet_text_prefix(text_value)
    if not s or s == "-":
        return []

    items = []
    for part in re.split(r"[；;]", s):
        part = part.strip()
        if not part:
            continue

        m = re.match(r"^(\d{5,6})\s*(.*)$", part)
        if m:
            wcode = normalize_warrant_code(m.group(1))
            wname = m.group(2).strip()
            items.append((wcode, wname))
        else:
            items.append(("", part))

    return items


def build_sell_event_lookup_from_abcd(target: date | None = None) -> dict[tuple[str, str], dict]:
    """
    從新制 A／B／C／D／E 工作表建立「分點＋權證代號」事件備援對照。

    新版每日賣出明細本身已包含事件代號；只有該欄缺漏時才使用此對照補回。
    保留原函式名稱以相容既有呼叫。
    """
    lookup: dict[tuple[str, str], dict] = {}

    def put(broker, warrant_code, info):
        broker = str(broker).strip()
        warrant_code = normalize_warrant_code(warrant_code)
        if not broker or not warrant_code:
            return

        key = (broker, warrant_code)
        old = lookup.get(key)
        if old is None:
            lookup[key] = info
            return

        # 同一權證可能多次進場。備援時優先保留較早事件，貼近 FIFO 歸屬。
        old_date = old.get("event_date")
        new_date = info.get("event_date")
        if old_date is None or (new_date is not None and new_date < old_date):
            lookup[key] = info

    for event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                [
                    "資料範圍", "分點", "標的股", "事件日", "權證清單",
                    "單日累積買進金額", "買超金額", "買超張數",
                    "減碼日", "減碼獲利%", "出清日", "出清獲利%",
                ],
            )
            df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)
        except Exception:
            continue

        for _, row in df.iterrows():
            broker = row.get("分點", "")
            warrant_list = row.get("權證清單", "")
            underlying = normalize_underlying(row.get("標的股", ""), warrant_list)

            return_pct = None
            if target and parse_date_value(row.get("出清日")) == target:
                return_pct = row.get("出清獲利%")
            elif target and parse_date_value(row.get("減碼日")) == target:
                return_pct = row.get("減碼獲利%")

            buy_amount = safe_float(
                _first_non_empty_value(
                    row.get("單日累積買進金額", ""),
                    row.get("買超金額", ""),
                ),
                0,
            )
            buy_qty = safe_float(row.get("買超張數", ""), 0)
            buy_avg = (
                buy_amount / (buy_qty * NTD_PER_WARRANT_POINT)
                if buy_amount > 0 and buy_qty > 0
                else 0
            )
            event_date = parse_date_value(row.get("事件日"))

            for warrant_code, warrant_name in parse_warrant_items_from_text(warrant_list):
                put(
                    broker,
                    warrant_code,
                    {
                        "event": event_code,
                        "underlying": underlying,
                        "warrant_name": strip_gsheet_text_prefix(warrant_name),
                        "return_pct": return_pct,
                        "buy_amount": buy_amount,
                        "buy_avg": buy_avg,
                        "event_date": event_date,
                    },
                )

    return lookup


def build_warrant_sell_history_return_lookup(target: date) -> dict[tuple[str, str], dict]:
    """
    從「快取_分點歷史」估算指定日期各分點 + 權證代號的賣出報酬率與對應成本。

    用途：
    1. A/B/C/D/E 白名單權證若事件表本身沒有報酬率，可用歷史實際買賣資料補估。
    2. 不在 A/B/C/D/E，但因「同分點 + 同標的今日賣出合計 >= 100 萬」而被納入圖卡的權證，
       也能顯示合理的報酬率。

    估算法：
    - 以「同分點 + 同權證代號」為單位
    - 使用加權平均成本法（依快取_分點歷史的每日買進 / 賣出金額與股數）
    - 對目標日的賣出，先以目標日前累積持有成本計算賣出報酬率，再更新剩餘持有部位
    """
    needed_cols = [
        "日期", "分點", "權證代號", "權證代碼", "權證名稱",
        "買進股數", "賣出股數", "買進金額", "賣出金額"
    ]

    df = read_gsheet_table_optional(SHEET_HISTORY, needed_cols)
    if df.empty:
        return {}

    grouped: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(lambda: {
        "buy_qty": 0.0, "sell_qty": 0.0, "buy_amt": 0.0, "sell_amt": 0.0
    }))

    for _, r in df.iterrows():
        d = parse_date_value(r.get("日期"))
        if not d or d > target:
            continue

        broker = str(r.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        warrant_code = normalize_warrant_code(r.get("權證代號") or r.get("權證代碼"))
        if not warrant_code:
            continue

        bucket = grouped[(broker, warrant_code)][d]
        bucket["buy_qty"] += safe_float(r.get("買進股數"), 0)
        bucket["sell_qty"] += safe_float(r.get("賣出股數"), 0)
        bucket["buy_amt"] += safe_float(r.get("買進金額"), 0)
        bucket["sell_amt"] += safe_float(r.get("賣出金額"), 0)

    result: dict[tuple[str, str], dict] = {}

    for key, by_date in grouped.items():
        hold_qty = 0.0      # 股數
        hold_cost = 0.0     # 金額

        for d in sorted(by_date.keys()):
            row = by_date[d]
            buy_qty = safe_float(row.get("buy_qty"), 0)
            sell_qty = safe_float(row.get("sell_qty"), 0)
            buy_amt = safe_float(row.get("buy_amt"), 0)
            sell_amt = safe_float(row.get("sell_amt"), 0)

            # 先處理賣出，確保目標日報酬率是用「賣出前持有成本」估算。
            if sell_qty > 0:
                avg_cost = (hold_cost / hold_qty) if hold_qty > 0 else 0.0
                sell_avg = (sell_amt / sell_qty) if sell_qty > 0 else 0.0

                est_buy_amount = 0.0
                est_return_pct = None

                if avg_cost > 0:
                    est_buy_amount = avg_cost * sell_qty
                    if sell_avg > 0:
                        est_return_pct = ((sell_avg - avg_cost) / avg_cost) * 100.0

                if d == target:
                    result[key] = {
                        "buy_amount": est_buy_amount,
                        "return_pct": est_return_pct,
                    }

                remove_qty = min(sell_qty, hold_qty)
                if remove_qty > 0 and avg_cost > 0:
                    hold_cost -= avg_cost * remove_qty
                    hold_qty -= remove_qty
                    if hold_qty < 1e-9:
                        hold_qty = 0.0
                        hold_cost = 0.0

            if buy_qty > 0:
                hold_qty += buy_qty
                hold_cost += buy_amt

    return result



def _first_non_empty_value(*values):
    for value in values:
        s = strip_gsheet_text_prefix(value)
        if s and s != "-":
            return value
    return ""


def _prefer_daily_sell_event_value(old_value, new_value):
    old_event = normalize_event_code(old_value)
    new_event = normalize_event_code(new_value)

    if is_unclassified_event(old_event) and not is_unclassified_event(new_event):
        return new_value

    if not old_event and new_event:
        return new_value

    return old_value


def dedupe_daily_sell_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    清理「每日賣出明細」可能重複列，避免今日賣方金額被重複計算。

    設計原則：
    1. 完全相同的「日期 + 分點 + 權證 + 賣出金額 + 賣出股數/張數」視為重複同步資料，直接去除。
    2. 同一天、同分點、同一檔權證理論上只會有一筆官方 API5 日資料。
       若 Google Sheet 因同步或快取異常出現多列，保留賣出金額較大的那筆作為當日值，
       不把多列相加，避免同一筆日資料被重複放大。
    3. 分點、權證、標的、事件、狀態等文字欄位會盡量保留非空值；事件代號優先保留 A/B/C/D/E。
    """
    if df is None or df.empty:
        return df

    grouped = {}
    exact_seen = set()
    exact_duplicate_count = 0
    same_key_duplicate_count = 0

    for _, r in df.iterrows():
        row = r.to_dict()
        d = parse_date_value(row.get("日期"))
        date_key = d.isoformat() if d else strip_gsheet_text_prefix(row.get("日期", ""))
        broker = str(row.get("分點", "")).strip()
        warrant_code = normalize_warrant_code(row.get("權證代號", ""))
        warrant_name = strip_gsheet_text_prefix(row.get("權證名稱", ""))
        warrant_key = warrant_code or warrant_name

        sell_amount = safe_float(row.get("賣出金額"), 0)
        sell_qty_shares = safe_float(row.get("賣出股數"), 0)
        sell_qty_lots = safe_float(row.get("賣出張數"), 0)

        exact_key = (
            date_key,
            broker,
            warrant_key,
            round(sell_amount, 4),
            round(sell_qty_shares, 4),
            round(sell_qty_lots, 4),
        )

        if exact_key in exact_seen:
            exact_duplicate_count += 1
            continue
        exact_seen.add(exact_key)

        group_key = (date_key, broker, warrant_key)

        if group_key not in grouped:
            row["_dedupe_sell_amount"] = sell_amount
            row["_dedupe_sell_qty_shares"] = sell_qty_shares
            row["_dedupe_sell_qty_lots"] = sell_qty_lots
            grouped[group_key] = row
            continue

        same_key_duplicate_count += 1
        keep = grouped[group_key]
        keep_amount = safe_float(keep.get("_dedupe_sell_amount"), 0)

        # 同一日同分點同權證若出現多列，視為同一筆日資料的重複/更新版本，
        # 保留賣出金額較大的版本，不做加總，避免重複計算賣方金額。
        if sell_amount > keep_amount:
            for col in ["賣出金額", "賣出股數", "賣出張數", "賣出均價"]:
                keep[col] = row.get(col, keep.get(col, ""))
            keep["_dedupe_sell_amount"] = sell_amount
            keep["_dedupe_sell_qty_shares"] = sell_qty_shares
            keep["_dedupe_sell_qty_lots"] = sell_qty_lots

        # 文字資訊補強：保留非空值，事件代號優先保留可解析的 A/B/C/D/E。
        # 新版每日賣出明細若已由主程式算好 FIFO 報酬 / 成本，這些欄位也要保留下來，
        # 否則同日同分點同權證去重後，圖片端可能又退回顯示「-」。
        for col in [
            "分點名稱", "券商代號", "標的股", "標的名稱", "權證代號", "權證名稱",
            "狀態", "事件日",
            "報酬率", "報酬率%", "賣出報酬率", "賣出報酬%",
            "已實現報酬率", "已實現報酬%", "FIFO報酬率", "FIFO報酬%",
            "賣出成本", "對應買進成本", "買進成本", "已實現成本", "FIFO成本",
            "賣出損益", "已實現損益", "FIFO損益",
            "歷史買進張數", "歷史買進股數", "歷史買進金額", "歷史平均成本", "成本狀態",
        ]:
            if not strip_gsheet_text_prefix(keep.get(col, "")) and strip_gsheet_text_prefix(row.get(col, "")):
                keep[col] = row.get(col, "")

        keep["事件"] = _prefer_daily_sell_event_value(keep.get("事件", ""), row.get("事件", ""))
        keep["事件來源"] = _prefer_daily_sell_event_value(keep.get("事件來源", ""), row.get("事件來源", ""))

    rows = []
    for row in grouped.values():
        row = dict(row)
        for col in ["_dedupe_sell_amount", "_dedupe_sell_qty_shares", "_dedupe_sell_qty_lots"]:
            row.pop(col, None)
        rows.append(row)

    out = pd.DataFrame(rows, columns=df.columns).fillna("")

    removed_count = exact_duplicate_count + same_key_duplicate_count
    if removed_count > 0:
        print(
            f"  ⚠️ 每日賣出明細已去重：原始 {len(df):,} 筆 → {len(out):,} 筆，"
            f"完全重複 {exact_duplicate_count:,} 筆，同日同分點同權證重複/更新 {same_key_duplicate_count:,} 筆。"
        )

    return out

def append_daily_sell_rows_from_gsheet(sells: list[dict], target: date):
    """
    今日賣超明細改讀「每日賣出明細」。

    這張表是主程式由原始 API5 分點歷史資料整理出來，
    欄位中的「賣出金額 / 賣出張數」即為官方分點每日資料，
    不再用 A 表的「減碼均價 × 買進張數」推估。

    顯示規則：
    1. 若該筆「分點 + 權證代號」曾出現在 A/B/C/D/E，照原本事件邏輯顯示。
    2. 若該權證未出現在 A/B/C/D/E，但同一分點今天對同一標的的賣出金額合計
       達 NON_ABCD_SELL_UNDERLYING_THRESHOLD，仍納入賣超明細。
       這類資料不標 A/B/C/D/E，直接顯示權證與賣出金額。
    3. 非 A/B/C/D/E 的大額單標的賣超，直接使用「每日賣出明細」內
       主程式已計算的 FIFO 成本與報酬率。
    """
    needed_cols = [
        "日期", "分點", "分點名稱", "券商代號",
        "事件", "狀態",
        "標的股", "標的名稱",
        "權證代號", "權證名稱",
        "賣出張數", "賣出股數", "賣出金額", "賣出均價",
        "報酬率", "報酬率%", "賣出報酬率", "賣出報酬%",
        "已實現報酬率", "已實現報酬%", "FIFO報酬率", "FIFO報酬%",
        "賣出成本", "對應買進成本", "買進成本", "已實現成本", "FIFO成本",
        "賣出損益", "已實現損益", "FIFO損益",
        "歷史買進張數", "歷史買進股數", "歷史買進金額", "歷史平均成本", "成本狀態",
        "事件日", "事件來源",
    ]

    df = read_gsheet_table_optional(SHEET_DAILY_SELL, needed_cols)
    if df.empty:
        return

    # 先清理每日賣出明細可能的重複列，避免同一筆日賣出資料被重複加總。
    df = dedupe_daily_sell_rows(df)
    if df.empty:
        return

    event_lookup = build_sell_event_lookup_from_abcd(target)

    # 先統計「不在 A/B/C/D/E 白名單」的權證，在同一天 + 同分點 + 同標的的實際賣出合計。
    non_abcd_underlying_amounts: dict[tuple[str, str], float] = defaultdict(float)

    for _, r in df.iterrows():
        if parse_date_value(r.get("日期")) != target:
            continue

        broker = str(r.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        warrant_code = normalize_warrant_code(r.get("權證代號", ""))
        warrant_name = strip_gsheet_text_prefix(r.get("權證名稱", ""))
        underlying = normalize_underlying(r.get("標的股"), warrant_name)
        sell_amount = safe_float(r.get("賣出金額"), 0)

        if sell_amount <= 0 or not underlying:
            continue

        if not event_lookup.get((broker, warrant_code)):
            non_abcd_underlying_amounts[(broker, underlying)] += sell_amount

    qualifying_non_abcd_underlyings = {
        key
        for key, amount in non_abcd_underlying_amounts.items()
        if amount >= NON_ABCD_SELL_UNDERLYING_THRESHOLD
    }

    for _, r in df.iterrows():
        if parse_date_value(r.get("日期")) != target:
            continue

        broker = str(r.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        warrant_code = normalize_warrant_code(r.get("權證代號", ""))
        warrant_name = strip_gsheet_text_prefix(r.get("權證名稱", ""))
        event_raw = r.get("事件") or r.get("事件來源")
        event = normalize_event_code(event_raw)
        status = str(r.get("狀態", "")).strip() or "賣超"
        underlying = normalize_underlying(r.get("標的股"), warrant_name)

        sell_amount = safe_float(r.get("賣出金額"), 0)
        sell_qty = safe_int(r.get("賣出張數"), 0)
        if sell_qty <= 0:
            sell_qty = int(safe_float(r.get("賣出股數"), 0) // 1000)

        if sell_amount <= 0:
            continue

        lookup_info = event_lookup.get((broker, warrant_code))

        # 主程式新版「每日賣出明細」若已經寫入 FIFO 報酬率 / 賣出成本，
        # 圖片端優先採用這張表的結果。
        # 只有每日賣出明細沒有報酬 / 成本時，才退回 A/B/C/D/E 或快取_分點歷史估算。
        daily_return_pct = normalize_return_pct(_pick_first_existing_value_fuzzy(r, [
            "報酬率", "報酬率%", "賣出報酬率", "賣出報酬%",
            "已實現報酬率", "已實現報酬%", "FIFO報酬率", "FIFO報酬%",
        ]))
        daily_buy_amount = safe_float(_pick_first_existing_value_fuzzy(r, [
            "賣出成本", "對應買進成本", "買進成本", "已實現成本", "FIFO成本",
        ]), 0)

        if lookup_info:
            if is_unclassified_event(event):
                event = lookup_info.get("event", event)

            if not underlying:
                underlying = lookup_info.get("underlying", underlying)

            if not warrant_name:
                warrant_name = lookup_info.get("warrant_name", warrant_name)

            return_pct = daily_return_pct
            if return_pct is None:
                return_pct = normalize_return_pct(lookup_info.get("return_pct"))
            buy_amount = daily_buy_amount

            if is_unclassified_event(event):
                # 權證雖可對到 A/B/C/D/E 白名單，但事件代號仍無法解析時，
                # 不強行標註 A/B/C/D/E，直接當作單一賣超顯示。
                event = "單一賣超"

            buy_avg = safe_float(lookup_info.get("buy_avg"), 0)
            sell_avg = safe_float(r.get("賣出均價"), 0)

            if buy_avg > 0 and sell_qty > 0 and safe_float(buy_amount, 0) <= 0:
                buy_amount = buy_avg * sell_qty * NTD_PER_WARRANT_POINT
                if return_pct is None and sell_avg > 0:
                    return_pct = ((sell_avg - buy_avg) / buy_avg) * 100.0
            elif safe_float(buy_amount, 0) <= 0:
                buy_amount = safe_float(lookup_info.get("buy_amount"), 0)

            append_sell(
                sells,
                broker,
                status,
                event,
                underlying,
                warrant_name,
                sell_amount,
                sell_qty,
                SHEET_DAILY_SELL,
                return_pct,
                buy_amount,
                warrant_code=warrant_code,
                defer_threshold=True,
            )
            continue

        # 不在 A/B/C/D/E 的權證：同一分點 + 同一標的今日實際賣出合計 >= 100 萬才納入。
        if (broker, underlying) not in qualifying_non_abcd_underlyings:
            continue

        non_abcd_return_pct = daily_return_pct
        non_abcd_buy_amount = daily_buy_amount

        append_sell(
            sells,
            broker,
            status,
            "單一賣超",
            underlying,
            warrant_name,
            sell_amount,
            sell_qty,
            SHEET_DAILY_SELL,
            non_abcd_return_pct,
            non_abcd_buy_amount,
            warrant_code=warrant_code,
            force_include=True,
            defer_threshold=True,
        )



def extract_actions_from_gsheet(target: date) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []

    event_cols = [
        "資料範圍", "事件類型", "分點", "標的股", "事件日",
        "涵蓋權證數", "權證清單", "最大單筆權證",
        "單日累積買進金額", "買超金額", "買超張數",
        "減碼日", "減碼賣出金額", "減碼獲利%",
        "出清日", "出清賣出金額", "出清獲利%",
    ]

    for event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(sheet_name, event_cols)
            df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)
        except Exception as exc:
            print(f"  ⚠️ 讀取 {sheet_name} 失敗：{type(exc).__name__}: {exc}")
            continue

        for _, row in df.iterrows():
            if parse_date_value(row.get("事件日")) != target:
                continue

            amount = safe_float(
                _first_non_empty_value(
                    row.get("單日累積買進金額", ""),
                    row.get("買超金額", ""),
                ),
                0,
            )
            warrant_text = _first_non_empty_value(
                row.get("權證清單", ""),
                row.get("最大單筆權證", ""),
            )

            append_buy(
                buys,
                row.get("分點", ""),
                event_code,
                row.get("標的股", ""),
                warrant_text,
                amount,
                safe_int(row.get("買超張數", ""), 0),
                sheet_name,
            )

    buys = dedupe_buy_actions(buys)

    # 賣方資料仍直接讀新版主程式輸出的「每日賣出明細」。
    append_daily_sell_rows_from_gsheet(sells, target)

    add_count_map = collect_broker_underlying_add_count_map(
        target,
        ADD_COUNT_LOOKBACK_TRADING_DAYS,
    )
    for item in buys:
        key = (item.get("broker", ""), item.get("underlying", ""))
        item["add_count"] = safe_int(add_count_map.get(key, 1), 1)

    return buys, sells


def compress_actions(actions: list[dict], kind: str) -> list[dict]:
    """
    同一分點、同一事件、同一標的若有多筆權證，合併成標的顯示。
    單筆則顯示權證名稱。
    """
    groups = defaultdict(list)

    for a in actions:
        # 賣方明細採「同一天、同分點、同狀態、同標的」直接合計。
        # 若同一標的同一天同時出現在 A/B/C/D/E，會合併為一列，報酬率用買進金額與賣出金額概算。
        # 買方仍保留事件別，避免買超訊號被過度合併。
        if kind == "sell":
            key = (a["broker"], a.get("status", ""), "ABCDE合計", a["underlying"] or a["warrant"])
        else:
            key = (a["broker"], a.get("status", ""), a["event"], a["underlying"] or a["warrant"])
        groups[key].append(a)

    result = []
    for (broker, status, event, key_name), items in groups.items():
        amount = sum(i["amount"] for i in items)
        qty = sum(i.get("qty", 0) for i in items)
        warrant_count = len(items)

        # 賣方報酬率合計邏輯：
        # 同一天同分點同標的可能會把 A/B/C/D/E 權證與「單一賣超」合併。
        # 因此報酬率計算時，分子與分母必須限定在同一批「有成本」的項目，
        # 避免分子含全部賣出金額、分母只含部分買進成本，造成報酬率被高估。
        costed_items = [
            i for i in items
            if safe_float(i.get("buy_amount"), 0) > 0 and safe_float(i.get("amount"), 0) > 0
        ]
        total_buy_amount = sum(safe_float(i.get("buy_amount"), 0) for i in costed_items)
        costed_sell_amount = sum(safe_float(i.get("amount"), 0) for i in costed_items)

        if kind == "sell" and total_buy_amount > 0 and costed_sell_amount > 0:
            return_pct = ((costed_sell_amount - total_buy_amount) / total_buy_amount) * 100.0
        else:
            # fallback：若沒有買進金額，才用個別報酬率反推成本。
            # 接近 -100% 的報酬率會讓反推成本被極端放大，直接排除避免扭曲合計報酬率。
            valid_returns = []
            for i in items:
                pct = i.get("return_pct")
                sell_amount = safe_float(i.get("amount", 0), 0)
                if pct is None or sell_amount <= 0:
                    continue

                pct = safe_float(pct, None)
                if pct is None:
                    continue

                if pct <= -95.0:
                    continue

                valid_returns.append((pct, sell_amount))

            if valid_returns:
                total_sell_amount = 0.0
                total_cost_amount = 0.0

                for pct, sell_amount in valid_returns:
                    pct = float(pct)
                    sell_amount = float(sell_amount or 0)
                    denominator = 1.0 + pct / 100.0

                    if denominator <= 0:
                        continue

                    cost_amount = sell_amount / denominator
                    total_sell_amount += sell_amount
                    total_cost_amount += cost_amount

                if total_cost_amount > 0:
                    return_pct = ((total_sell_amount - total_cost_amount) / total_cost_amount) * 100.0
                else:
                    return_pct = sum(float(p) for p, _ in valid_returns) / len(valid_returns)
            else:
                return_pct = None

        underlying = items[0].get("underlying", "")
        stock_name = items[0].get("stock_name", "")
        target_label = f"{underlying} {stock_name}".strip() if underlying else ""

        if kind == "sell":
            event = "/".join(sorted({str(i.get("event", "")) for i in items if str(i.get("event", "")).strip()})) or event
            if event in {"未歸類", "單一賣超"}:
                event = ""

        if warrant_count >= 2 and underlying:
            display_target = target_label if target_label else f"{underlying}"

            first_item = items[0]
            first_code = first_item.get("warrant_code", "")
            first_name = first_item.get("warrant", "")
            first_label = f"{first_code} {first_name}".strip() if first_code else first_name

            # 多筆同標的合併時，內容欄仍顯示其中一檔權證，再用 ... 表示還有其他權證
            content = f"{first_label}；..." if first_label else f"{warrant_count} 檔權證"
        else:
            warrant_code = items[0].get("warrant_code", "")
            warrant_name = items[0].get("warrant", "")
            warrant_label = f"{warrant_code} {warrant_name}".strip() if warrant_code else warrant_name
            list_count = safe_int(items[0].get("warrant_list_count", 0))

            display_target = target_label if target_label else (underlying if underlying else warrant_label)

            # B/C/D 通常是權證清單；若有多檔，顯示第一檔權證 + ...
            # 這樣至少能看到其中一支權證代碼/名稱，不會只剩「N 檔權證」。
            if kind == "buy" and event in {"A", "B", "C", "D", "E"} and list_count >= 2 and warrant_label:
                first_warrant = re.split(r"[；;]", warrant_label)[0].strip()
                content = f"{first_warrant}；..."
            else:
                content = warrant_label or f"{warrant_count} 檔權證"

            if kind == "sell":
                sell_event_label = event
                if not sell_event_label or sell_event_label == "ABCDE合計":
                    sell_event_label = "/".join(sorted({
                        str(i.get("event", "")).strip()
                        for i in items
                        if str(i.get("event", "")).strip()
                        and str(i.get("event", "")).strip() not in {"未歸類", "單一賣超"}
                    }))
                if sell_event_label:
                    content = f"{sell_event_label}｜{content}"

        # 賣超明細內容欄最前面固定保留 A/B/C/D/E 事件代號。
        # 注意：賣方資料可能在前面被合併成 warrant_count >= 2，
        # 因此不能只在單筆 else 分支加前綴，必須在 result.append 前統一處理。
        if kind == "sell":
            sell_event_label = event
            if not sell_event_label or sell_event_label == "ABCDE合計":
                sell_event_label = "/".join(sorted({
                    str(i.get("event", "")).strip()
                    for i in items
                    if str(i.get("event", "")).strip()
                    and str(i.get("event", "")).strip() not in {"未歸類", "單一賣超"}
                }))

            if sell_event_label and not str(content).startswith(f"{sell_event_label}｜"):
                content = f"{sell_event_label}｜{content}"

        add_count = 0
        add_count_label = ""
        if kind == "buy":
            add_count = max((safe_int(i.get("add_count", 0), 0) for i in items), default=0)
            if add_count > 1:
                add_count_label = f"加碼{add_count}"
                if not str(content).startswith(f"{add_count_label}｜"):
                    content = f"{add_count_label}｜ {content}"

        result.append({
            "broker": broker,
            "status": status,
            "event": event,
            "underlying": underlying,
            "target": display_target,
            "content": content,
            "amount": amount,
            "qty": qty,
            "return_pct": return_pct,
            "count": warrant_count,
            "kind": kind,
            "add_count": add_count,
            "add_count_label": add_count_label,
        })

    # 每日精選分點買賣超追蹤排序：
    # 1. 先依 TRACKED_BROKERS 的分點順序分組
    # 2. 同一分點內依金額由大到小
    # 3. 同一分點且金額相同時，依標的代號數字由小到大
    broker_order = {
        broker_name: index
        for index, broker_name in enumerate(TRACKED_BROKERS)
    }

    def underlying_sort_key(item: dict):
        code = normalize_underlying(item.get("underlying", ""), item.get("target", ""))
        if code.isdigit():
            return (0, int(code), code)

        target_text = str(item.get("target", "")).strip()
        match = re.match(r"^(\d+)", target_text)
        if match:
            matched_code = match.group(1)
            return (0, int(matched_code), matched_code)

        return (1, 999999999, target_text)

    result.sort(
        key=lambda x: (
            broker_order.get(str(x.get("broker", "")).strip(), len(TRACKED_BROKERS)),
            -safe_float(x.get("amount"), 0),
            underlying_sort_key(x),
        )
    )
    return result


def read_actual_daily_net_from_history(target: date) -> dict:
    """
    從「快取_分點歷史」計算指定日期五大追蹤分點的實際買賣超。

    用途：
    - 第一張圖 KPI 的「實際淨買超」必須用同一個資料來源計算。
    - 不再用 A/B/C/D/E 買超事件金額去扣「每日賣出明細」的實際賣出金額，
      避免買方與賣方來源集合不同，造成數學口徑不一致。

    回傳：
        {
            "buy_amount": 今日實際買進金額,
            "sell_amount": 今日實際賣出金額,
            "net_amount": 今日實際買進金額 - 今日實際賣出金額,
            "has_data": 是否有讀到該日快取_分點歷史資料,
        }
    """
    needed_cols = ["日期", "分點", "買進金額", "賣出金額"]
    df = read_gsheet_table_optional(SHEET_HISTORY, needed_cols)

    buy_amount = 0.0
    sell_amount = 0.0
    has_data = False

    if df.empty:
        return {
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "net_amount": buy_amount - sell_amount,
            "has_data": False,
        }

    for _, r in df.iterrows():
        if parse_date_value(r.get("日期")) != target:
            continue

        broker = str(r.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            continue

        has_data = True
        buy_amount += safe_float(r.get("買進金額"), 0)
        sell_amount += safe_float(r.get("賣出金額"), 0)

    return {
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "net_amount": buy_amount - sell_amount,
        "has_data": has_data,
    }


# ══════════════════════════════════════════════════════════════════════
# 繪圖
# ══════════════════════════════════════════════════════════════════════

def get_font_path(bold=False):
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[-1]


def make_font(size, bold=False):
    return ImageFont.truetype(get_font_path(bold), size)


def draw_report_image(target: date, buys_raw: list[dict], sells_raw: list[dict], history: dict, output_path: Path):
    """
    Matplotlib 動態版面引擎：
    - 不固定圖片高度
    - 不固定表格高度
    - 高度完全依照：有動作分點數、無動作分點列、買超筆數、賣方筆數自動增加
    - 邏輯參考處置股圖卡的 get_base_layout / setup_canvas 概念

    賣方顯示門檻補強：
    - 最終圖卡上顯示的每一列賣超明細，最低都必須達 SELL_THRESHOLD（預設 20 萬）。
    - 不再因 DISPLAY_EXIT_ALWAYS 或 force_include 等例外邏輯，讓 20 萬以下的小單顯示到圖片上。
    - 這樣可以避免抓到只是同分點散戶零星買賣，而不是你要追蹤的分點大戶行為。
    """
    buys = compress_actions(buys_raw, "buy")
    sells = compress_actions(sells_raw, "sell")

    # 嚴格套用最終圖卡賣方顯示門檻：
    # 只要是顯示在圖片上的賣超列，合併後金額最低都必須 >= SELL_THRESHOLD。
    # 這裡放在 compress_actions 之後，代表即使同標的多筆小單合併，只要合併後未達 20 萬，
    # 也不會顯示在圖卡上。
    sells = [x for x in sells if safe_float(x.get("amount"), 0) >= SELL_THRESHOLD]

    buy_total = sum(x["amount"] for x in buys)
    sell_total = sum(x["amount"] for x in sells)

    # KPI 的「今日淨額」直接使用圖卡目前顯示的買超與賣超金額計算，
    # 確保第三個 KPI 可直接由前兩個 KPI 驗算。
    actual_net = buy_total - sell_total

    broker_summary = {}
    for b in TRACKED_BROKERS:
        b_buys = [x for x in buys if x["broker"] == b]
        b_sells = [x for x in sells if x["broker"] == b]
        broker_summary[b] = {
            "buy_count": sum(x["count"] for x in b_buys),
            "buy_amount": sum(x["amount"] for x in b_buys),
            "sell_count": sum(x["count"] for x in b_sells),
            "sell_amount": sum(x["amount"] for x in b_sells),
            "has_action": bool(b_buys or b_sells),
            "avg_hold_days": history.get(b, {}).get("avg_hold_days", 0.0),
        }

    active_brokers = [b for b in TRACKED_BROKERS if broker_summary[b]["has_action"]]
    inactive_brokers = [b for b in TRACKED_BROKERS if not broker_summary[b]["has_action"]]

    # ─────────────────────────────────────────────
    # 動態版面參數：整體高度由資料量計算，不寫死
    # ─────────────────────────────────────────────
    fig_w = 13.0
    margin_x = 0.40
    content_w = fig_w - 2 * margin_x

    top_h = 1.55
    kpi_h = 1.25
    gap = 0.18

    # 分點卡片顯示規則：
    # - 有動作分點 3~5 個：只顯示有動作分點，並排同一排；今日無動作分點顯示在下方長條
    # - 有動作分點 0~2 個：加入「今日無動作分點」摘要框，避免畫面只剩少數卡片
    show_inactive_card = len(active_brokers) <= 2 and bool(inactive_brokers)
    show_inactive_bar = len(active_brokers) >= 3 and bool(inactive_brokers)
    broker_card_items = active_brokers[:] + (["__INACTIVE__"] if show_inactive_card else [])

    active_rows = 1 if broker_card_items else 0
    broker_card_h = 1.55
    broker_area_h = active_rows * broker_card_h

    inactive_h = 0.58 if show_inactive_bar else 0.0

    section_title_h = 0.55
    header_h = 0.42
    row_h = 0.48

    buy_rows = buys
    sell_rows = sells

    buy_table_h = section_title_h + header_h + max(1, len(buy_rows)) * row_h
    sell_table_h = 0.0
    if sell_rows:
        sell_table_h = section_title_h + header_h + len(sell_rows) * row_h

    event_legend_h = 0.45
    footer_h = 0.48

    fig_h = (
        top_h
        + kpi_h
        + gap
        + broker_area_h
        + (gap + inactive_h if show_inactive_bar else 0)
        + gap
        + buy_table_h
        + (gap + sell_table_h if sell_rows else 0)
        + gap
        + event_legend_h
        + footer_h
    )

    # 避免資料太少時圖片過扁
    fig_h = max(fig_h, 9.5)

    # ─────────────────────────────────────────────
    # 顏色與字型
    # ─────────────────────────────────────────────
    BG = "#F6F8FB"
    WHITE = "#FFFFFF"
    NAVY = "#061D3D"
    NAVY2 = "#0B2E5B"
    RED = "#D92323"
    GREEN = "#0B7A32"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#C9D5E3"
    ROW_ALT = "#FAFCFF"
    HEADER_BG = "#F3F7FC"
    PINK = "#FFF2F2"
    MINT = "#EFFAF2"

    font_path = get_font_path(False)
    bold_path = get_font_path(True)
    FONT = font_manager.FontProperties(fname=font_path)
    BOLD = font_manager.FontProperties(fname=bold_path)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=BG)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def rounded(x, y, w, h, fc=WHITE, ec=BORDER, lw=1.2, r=0.12, z=1):
        patch = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc, zorder=z
        )
        ax.add_patch(patch)
        return patch

    def rect(x, y, w, h, fc=WHITE, ec=None, lw=0.8, z=1):
        patch = patches.Rectangle((x, y), w, h, linewidth=lw if ec else 0,
                                  edgecolor=ec, facecolor=fc, zorder=z)
        ax.add_patch(patch)
        return patch

    def text(x, y, s, size=12, color=TEXT, fp=None, ha="left", va="center", z=5, weight=None):
        ax.text(x, y, str(s), fontsize=size, color=color, fontproperties=fp or FONT,
                ha=ha, va=va, zorder=z)

    def fit(s, n):
        s = str(s)
        return s if len(s) <= n else s[:n - 1] + "…"

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def measure_text_width(s, size=14, fp=None):
        ghost = ax.text(0, 0, str(s), fontsize=size, fontproperties=fp or FONT, alpha=0)
        bb = ghost.get_window_extent(renderer=renderer)
        ghost.remove()
        x0_disp, y0_disp = ax.transData.transform((0, 0))
        x1_disp = x0_disp + bb.width
        x0_data = ax.transData.inverted().transform((x0_disp, y0_disp))[0]
        x1_data = ax.transData.inverted().transform((x1_disp, y0_disp))[0]
        return x1_data - x0_data

    def fit_to_cell_width(s, cell_w, size=14, fp=None):
        s = str(s or "")
        if not s:
            return ""
        max_w = max(float(cell_w), 0.08)
        if measure_text_width(s, size=size, fp=fp) <= max_w:
            return s

        ellipsis = "…"
        if measure_text_width(ellipsis, size=size, fp=fp) >= max_w:
            return ellipsis

        low, high = 0, len(s)
        best = ellipsis
        while low <= high:
            mid = (low + high) // 2
            candidate = s[:mid] + ellipsis
            if measure_text_width(candidate, size=size, fp=fp) <= max_w:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def draw_center_watermark():
        try:
            ax.text(
                0.5, 0.50, CENTER_WATERMARK_TEXT,
                transform=ax.transAxes,
                ha="center", va="center",
                fontsize=CENTER_WATERMARK_FONT_SIZE,
                fontproperties=BOLD,
                color="#2C3440",
                alpha=CENTER_WATERMARK_ALPHA,
                rotation=CENTER_WATERMARK_ROTATION,
                linespacing=1.18,
                # 浮水印放在所有圖層最上方，但透明度很低，不影響閱讀
                zorder=50,
            )
        except Exception:
            pass

    def draw_bottom_watermark():
        # 已移除右下角「股市艾斯出品」浮水印，只保留中央淡色浮水印。
        # 保留空函式是為了避免舊版殘留呼叫時發生 NameError。
        pass

    date_label = f"{target.month}/{target.day}"
    draw_center_watermark()

    # ─────────────────────────────────────────────
    # Header
    # ─────────────────────────────────────────────
    y = fig_h - 0.45
    text(margin_x + 0.15, y, f"{date_label} 精選分點買賣超追蹤", 31, NAVY, BOLD)
    y -= 0.42
    text(margin_x + 0.18, y, f"精選 5 家分點｜華南永昌台中、元大南屯、富邦敦南、永豐金內湖、新光", 15, NAVY2, BOLD)
    y -= 0.32
    text(margin_x + 0.18, y, "紅色＝買超　綠色＝賣超　單位：萬元", 13, TEXT, BOLD)

    # ─────────────────────────────────────────────
    # KPI cards
    # ─────────────────────────────────────────────
    y -= 0.25
    kpi_y = y - kpi_h
    kpi_gap = 0.30
    kpi_w = (content_w - 2 * kpi_gap) / 3
    actual_net_text = fmt_wan(actual_net)
    actual_net_color = RED if actual_net >= 0 else GREEN
    actual_net_bg = PINK if actual_net >= 0 else MINT

    kpis = [
        ("今日買超", f"{sum(x['count'] for x in buys)} 筆", fmt_wan(buy_total), RED, PINK, "↗"),
        ("今日賣超", f"{sum(x['count'] for x in sells)} 筆", fmt_wan(sell_total), GREEN, MINT, "−"),
        ("今日淨額", "", actual_net_text, actual_net_color, actual_net_bg, "◎"),
    ]
    for i, (title, mid, val, color, bg, icon) in enumerate(kpis):
        x = margin_x + i * (kpi_w + kpi_gap)
        rounded(x, kpi_y, kpi_w, kpi_h, fc=bg, ec=color, lw=1.3, r=0.09)
        circle = patches.Circle((x + 0.48, kpi_y + kpi_h / 2), radius=0.28, facecolor=color, edgecolor=color, zorder=3)
        ax.add_patch(circle)
        text(x + 0.48, kpi_y + kpi_h / 2, icon, 22, WHITE, BOLD, ha="center")
        text(x + 0.88, kpi_y + 0.86, title, 16, TEXT, BOLD)
        if mid:
            text(x + 0.88, kpi_y + 0.54, mid, 15, color, BOLD)
            text(x + 0.88, kpi_y + 0.23, val, 18, color, BOLD)
        else:
            text(x + 0.88, kpi_y + 0.42, val, 20, color, BOLD)

    y = kpi_y - gap

    # ─────────────────────────────────────────────
    # Broker cards：維持單排 3~5 欄；若有動作分點 0~2 個，加入「今日無動作分點」摘要框
    # ─────────────────────────────────────────────
    if broker_card_items:
        cards_per_row = min(5, max(3, len(broker_card_items)))
        card_gap = 0.14
        card_w = (content_w - (cards_per_row - 1) * card_gap) / cards_per_row

        for idx, item in enumerate(broker_card_items):
            col = idx % cards_per_row
            x = margin_x + col * (card_w + card_gap)
            cy = y - broker_card_h

            if item == "__INACTIVE__":
                # 0~2 個有動作分點時，把今日無動作分點做成一個方框。
                # 設計與一般分點卡片一致：同樣 NAVY 標題列、同樣白底與外框。
                # 名稱改成單行顯示，避免明明放得下卻被硬換行。
                rounded(x, cy, card_w, broker_card_h, fc=WHITE, ec=NAVY2, lw=1.1, r=0.08)
                rect(x, cy + broker_card_h - 0.42, card_w, 0.42, fc=NAVY)
                text(x + card_w / 2, cy + broker_card_h - 0.21, "今日無動作分點", 14.5, WHITE, BOLD, ha="center")

                text(x + card_w / 2, cy + broker_card_h - 0.60, "無買超 / 賣方", 11.5, TEXT, FONT, ha="center")
                ax.plot([x + 0.12, x + card_w - 0.12], [cy + 0.78, cy + 0.78], color=BORDER, linewidth=0.8)

                inactive_text = "、".join(inactive_brokers)
                text(x + 0.12, cy + 0.48, inactive_text, 12.0, TEXT, BOLD)
                continue

            b = item
            s = broker_summary[b]
            rounded(x, cy, card_w, broker_card_h, fc=WHITE, ec=NAVY2, lw=1.1, r=0.08)
            rect(x, cy + broker_card_h - 0.42, card_w, 0.42, fc=NAVY)

            # 此區字體比前一版放大 2
            text(x + card_w / 2, cy + broker_card_h - 0.21, b, 14.5, WHITE, BOLD, ha="center")
            text(x + card_w / 2, cy + broker_card_h - 0.60, f"平均 {s['avg_hold_days']:.1f} 天", 11.5, TEXT, FONT, ha="center")
            ax.plot([x + 0.12, x + card_w - 0.12], [cy + 0.78, cy + 0.78], color=BORDER, linewidth=0.8)

            text(x + 0.12, cy + 0.56, "買超", 12.5, RED, BOLD)
            text(x + 0.70, cy + 0.56, f"{s['buy_count']}筆 / {fmt_wan(s['buy_amount'])}", 12.5, RED, BOLD)

            text(x + 0.12, cy + 0.28, "賣超", 12.5, GREEN, BOLD)
            text(x + 0.70, cy + 0.28, f"{s['sell_count']}筆 / {fmt_wan(s['sell_amount'])}", 12.5, GREEN, BOLD)

    y -= broker_area_h

    # 有動作分點 3～5 個時，維持原本方式：今日無動作分點顯示在下方長條
    if show_inactive_bar:
        y -= gap
        rounded(margin_x, y - inactive_h, content_w, inactive_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
        text(margin_x + 0.25, y - inactive_h / 2, "今日無動作分點：", 15, NAVY, BOLD)
        text(margin_x + 2.02, y - inactive_h / 2, "、".join(inactive_brokers), 15, TEXT, BOLD)
        y -= inactive_h

    y -= gap

    # ─────────────────────────────────────────────
    # 通用表格繪製
    # ─────────────────────────────────────────────
    def draw_table(title, rows, headers, col_widths, row_builder, title_color, amount_color, y_top):
        table_h = section_title_h + header_h + max(1, len(rows)) * row_h
        rounded(margin_x, y_top - table_h, content_w, table_h, fc=WHITE, ec=title_color, lw=1.2, r=0.08)
        rect(margin_x, y_top - section_title_h, content_w, section_title_h, fc=title_color)
        text(margin_x + 0.30, y_top - section_title_h / 2, title, 19, WHITE, BOLD)

        header_y_top = y_top - section_title_h
        rect(margin_x, header_y_top - header_h, content_w, header_h, fc=HEADER_BG, ec=BORDER, lw=0.6)
        x = margin_x
        for h, w in zip(headers, col_widths):
            text(x + w / 2, header_y_top - header_h / 2, h, 12, NAVY, BOLD, ha="center")
            ax.plot([x, x], [y_top - table_h, header_y_top], color=BORDER, linewidth=0.6)
            x += w
        ax.plot([margin_x + content_w, margin_x + content_w], [y_top - table_h, header_y_top], color=BORDER, linewidth=0.6)

        data_y = header_y_top - header_h
        if not rows:
            rect(margin_x, data_y - row_h, content_w, row_h, fc=WHITE, ec=BORDER, lw=0.6)
            text(margin_x + content_w / 2, data_y - row_h / 2, "今日沒有達顯示條件的資料", 13, MUTED, BOLD, ha="center")
        else:
            for i, r in enumerate(rows):
                ry = data_y - (i + 1) * row_h
                rect(margin_x, ry, content_w, row_h, fc=WHITE if i % 2 == 0 else ROW_ALT, ec=BORDER, lw=0.5)
                values, colors, aligns, bolds = row_builder(i, r)
                x = margin_x
                for val, w, c, a, is_bold, h in zip(values, col_widths, colors, aligns, bolds, headers):
                    px = x + (w / 2 if a == "center" else 0.12 if a == "left" else w - 0.12)
                    display_val = fit_to_cell_width(val, max(0.2, w - 0.24), size=14, fp=BOLD if is_bold else FONT)

                    # 只針對買超明細「內容」欄，把「加碼N｜」前綴獨立畫成粗體。
                    # 後面的權證名稱維持原本一般字體，避免整格都變粗。
                    if h == "內容" and a == "left":
                        m = re.match(r"^(加碼\d+｜)(.*)$", str(display_val))
                        if m:
                            prefix = m.group(1)
                            rest = m.group(2)

                            text(px, ry + row_h / 2, prefix, 14, c, BOLD, ha="left")

                            prefix_offset = 0.0
                            for ch in prefix:
                                prefix_offset += 0.085 if ord(ch) < 128 else 0.17
                            prefix_offset += 0.03

                            text(px + prefix_offset, ry + row_h / 2, rest, 14, c, FONT, ha="left")
                        else:
                            text(px, ry + row_h / 2, display_val, 14, c, BOLD if is_bold else FONT, ha=a)
                    else:
                        text(px, ry + row_h / 2, display_val, 14, c, BOLD if is_bold else FONT, ha=a)
                    x += w
        return y_top - table_h

    # Buy table
    buy_headers = ["排名", "分點", "事件", "標的 / 權證", "內容", "買超金額"]
    buy_col_w = [0.75, 2.25, 0.90, 2.25, 3.35, 2.50]

    def buy_builder(i, r):
        return (
            [str(i + 1), r["broker"], r["event"], r["target"], r["content"], fmt_wan(r["amount"])],
            [TEXT, TEXT, RED, TEXT, TEXT, RED],
            ["center", "left", "center", "left", "left", "right"],
            [True, True, True, True, False, True],
        )

    y = draw_table(f"{date_label} 今日買超明細", buy_rows, buy_headers, buy_col_w, buy_builder, NAVY, RED, y)

    # Sell table
    if sell_rows:
        y -= gap
        sell_headers = ["分點", "狀態", "標的 / 權證", "內容", "報酬率", "賣方金額"]
        sell_col_w = [2.05, 1.05, 2.25, 2.75, 1.55, 2.35]

        def sell_builder(i, r):
            ret_text = fmt_return_pct(r.get("return_pct"))
            ret_color = RED if safe_float(r.get("return_pct"), 0) > 0 else GREEN if safe_float(r.get("return_pct"), 0) < 0 else TEXT
            return (
                [r["broker"], r["status"], r["target"], r["content"], ret_text, fmt_wan(r["amount"])],
                [TEXT, GREEN, TEXT, TEXT, ret_color, GREEN],
                ["left", "center", "left", "left", "right", "right"],
                [True, True, True, False, True, True],
            )

        y = draw_table(f"{date_label} 今日賣超明細", sell_rows, sell_headers, sell_col_w, sell_builder, GREEN, GREEN, y)

    # Event legend：改成與近一個月圖相同的橫條式說明
    y -= gap
    legend_y = y - event_legend_h
    rounded(margin_x, legend_y, content_w, event_legend_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)

    text(margin_x + 0.25, legend_y + event_legend_h / 2, "事件代號說明", 13.5, NAVY, BOLD)

    legend_items = EVENT_LEGEND_ITEMS

    lx = margin_x + 1.72
    for code_name, desc in legend_items:
        rounded(lx, legend_y + 0.10, 0.32, 0.25, fc="#334155", ec="#334155", lw=0.8, r=0.07)
        text(lx + 0.16, legend_y + event_legend_h / 2, code_name, 10, WHITE, BOLD, ha="center")
        text(lx + 0.39, legend_y + event_legend_h / 2, desc, 9.6, TEXT, FONT)
        lx += 2.08 if code_name in {"A", "B"} else 1.98

    # footer
    y -= event_legend_h
    text(fig_w / 2, 0.18, "本圖為籌碼追蹤整理，不構成投資建議。", 11, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)




# ══════════════════════════════════════════════════════════════════════
# 近40個交易日｜五大分點共識買超 TOP15
# ══════════════════════════════════════════════════════════════════════

def get_buy_event_date(row, sheet_name: str) -> date | None:
    """新制 A～E 五張事件表一律使用「事件日」。"""
    return parse_date_value(row.get("事件日"))


def get_sell_event_date(row, sheet_name: str, status: str) -> date | None:
    """依事件工作表取得該筆賣方事件日期。status = 減碼 / 出清"""
    col = "減碼日" if status == "減碼" else "出清日"
    return parse_date_value(row.get(col))


def collect_recent_buy_trading_dates(
    target: date,
    lookback_days: int = LOOKBACK_TRADING_DAYS,
) -> list[date]:
    """從新制 A～E 事件表取得最近 N 個有事件資料的交易日。"""
    dates = set()

    for _event_code, _event_label, sheet_name in AMOUNT_CLASS_SHEET_PLAN:
        try:
            df = read_gsheet_table(
                sheet_name,
                ["資料範圍", "分點", "事件日"],
            )
            df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)
        except Exception:
            continue

        for _, row in df.iterrows():
            event_date = parse_date_value(row.get("事件日"))
            if event_date and event_date <= target:
                dates.add(event_date)

    return sorted(dates, reverse=True)[:lookback_days]


def collect_consensus_buy_top10(target: date, lookback_days: int = LOOKBACK_TRADING_DAYS) -> tuple[list[dict], dict]:
    """
    直接讀取 Google Sheet「快取_TOP15共識淨買超」。

    這張近一個月 TOP15 圖不再由圖片端重新計算 A/B/C/D/E 或快取_分點歷史，
    lookback_days 只保留相容舊呼叫，實際期間與排名以主程式 Step 4b 產生的快取為準。
    """
    rows, meta = read_top15_consensus_cache_from_gsheet(target)
    return rows, meta

def draw_consensus_buy_image(target: date, output_path: Path, lookback_days: int = LOOKBACK_TRADING_DAYS):
    """
    第二張圖：近40個交易日｜五大分點共識淨買超成本 TOP15
    """
    rows, period_meta = collect_consensus_buy_top10(target, lookback_days)
    n = len(rows)

    period_start = period_meta.get("start_date")
    period_end = period_meta.get("end_date")
    effective_days = safe_int(period_meta.get("effective_days"), 0)

    if period_start and period_end:
        period_text = f"{period_start:%Y/%m/%d} ～ {period_end:%Y/%m/%d}"
    elif period_end:
        period_text = f"{period_end:%Y/%m/%d}"
    else:
        period_text = "無有效期間"

    total_amount = sum(r["amount"] for r in rows)
    total_net_amount = sum(r["net_amount"] for r in rows)

    # 動態版面
    fig_w = 13.0
    margin_x = 0.40
    content_w = fig_w - 2 * margin_x

    top_h = 1.95
    legend_h = 0.45
    gap = 0.18
    section_title_h = 0.55
    header_h = 0.42
    row_h = 0.50
    footer_h = 0.66

    table_h = section_title_h + header_h + max(1, n) * row_h

    fig_h = top_h + legend_h + gap + table_h + footer_h
    fig_h = max(fig_h, 7.6)

    BG = "#F6F8FB"
    WHITE = "#FFFFFF"
    NAVY = "#061D3D"
    NAVY2 = "#0B2E5B"
    RED = "#D92323"
    GREEN = "#16803C"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#C9D5E3"
    ROW_ALT = "#FAFCFF"
    HEADER_BG = "#F3F7FC"
    PINK = "#FFF2F2"

    font_path = get_font_path(False)
    bold_path = get_font_path(True)
    FONT = font_manager.FontProperties(fname=font_path)
    BOLD = font_manager.FontProperties(fname=bold_path)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=BG)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def rounded(x, y, w, h, fc=WHITE, ec=BORDER, lw=1.2, r=0.12, z=1):
        patch = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc, zorder=z
        )
        ax.add_patch(patch)
        return patch

    def rect(x, y, w, h, fc=WHITE, ec=None, lw=0.8, z=1):
        patch = patches.Rectangle((x, y), w, h, linewidth=lw if ec else 0,
                                  edgecolor=ec, facecolor=fc, zorder=z)
        ax.add_patch(patch)
        return patch

    def text(x, y, s, size=12, color=TEXT, fp=None, ha="left", va="center", z=5):
        ax.text(x, y, str(s), fontsize=size, color=color, fontproperties=fp or FONT,
                ha=ha, va=va, zorder=z)

    def fit(s, n_chars):
        s = str(s)
        return s if len(s) <= n_chars else s[:n_chars - 1] + "…"

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def measure_text_width(s, size=14, fp=None):
        ghost = ax.text(0, 0, str(s), fontsize=size, fontproperties=fp or FONT, alpha=0)
        bb = ghost.get_window_extent(renderer=renderer)
        ghost.remove()
        x0_disp, y0_disp = ax.transData.transform((0, 0))
        x1_disp = x0_disp + bb.width
        x0_data = ax.transData.inverted().transform((x0_disp, y0_disp))[0]
        x1_data = ax.transData.inverted().transform((x1_disp, y0_disp))[0]
        return x1_data - x0_data

    def fit_to_cell_width(s, cell_w, size=14, fp=None):
        s = str(s or "")
        if not s:
            return ""
        max_w = max(float(cell_w), 0.08)
        if measure_text_width(s, size=size, fp=fp) <= max_w:
            return s

        ellipsis = "…"
        if measure_text_width(ellipsis, size=size, fp=fp) >= max_w:
            return ellipsis

        low, high = 0, len(s)
        best = ellipsis
        while low <= high:
            mid = (low + high) // 2
            candidate = s[:mid] + ellipsis
            if measure_text_width(candidate, size=size, fp=fp) <= max_w:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def build_participant_broker_items(row, limit=5):
        """
        顯示所有參與分點的淨累積買超金額與快取報酬率。
        回傳 [(broker, amount, return_pct), ...]。
        """
        items = row.get("participant_brokers", [])
        if not items:
            top_broker = row.get("top_broker", "")
            top_amount = row.get("top_broker_amount", 0)
            return [(top_broker, top_amount, None, "")], False

        shown = items[:limit]
        has_more = len(items) > limit
        return shown, has_more

    def draw_participant_brokers_cell(x_left, y_center, row, cell_w, size=13):
        items, has_more = build_participant_broker_items(row, limit=5)
        cur_x = x_left + 0.12
        max_x = x_left + cell_w - 0.12

        def draw_piece(piece_text, piece_color):
            nonlocal cur_x
            piece_text = str(piece_text)
            if not piece_text:
                return True
            w = measure_text_width(piece_text, size=size, fp=BOLD)
            if cur_x + w > max_x:
                ellipsis = "…"
                ell_w = measure_text_width(ellipsis, size=size, fp=BOLD)
                if cur_x + ell_w <= max_x:
                    text(cur_x, y_center, ellipsis, size, TEXT, BOLD, ha="left")
                return False
            text(cur_x, y_center, piece_text, size, piece_color, BOLD, ha="left")
            cur_x += w
            return True

        for idx, item in enumerate(items):
            broker = item[0]
            amount = item[1] if len(item) > 1 else 0
            return_pct = item[2] if len(item) > 2 else None
            valuation_marker = item[3] if len(item) > 3 else ""

            prefix = f"{broker} {fmt_wan(amount)} / "
            if not draw_piece(prefix, TEXT):
                return

            ret_text = fmt_return_pct(return_pct)
            ret_color = RED if safe_float(return_pct, 0) > 0 else GREEN if safe_float(return_pct, 0) < 0 else TEXT
            if not draw_piece(ret_text, ret_color):
                return
            if valuation_marker:
                if not draw_piece(valuation_marker, ret_color):
                    return

            if idx < len(items) - 1:
                if not draw_piece("、", TEXT):
                    return

        if has_more:
            draw_piece(f"、等{len(row.get('participant_brokers', []))}家", TEXT)

    # 中央浮水印
    try:
        ax.text(
            0.5, 0.50, CENTER_WATERMARK_TEXT,
            transform=ax.transAxes,
            ha="center", va="center",
            fontsize=CENTER_WATERMARK_FONT_SIZE,
            fontproperties=BOLD,
            color="#2C3440",
            alpha=CENTER_WATERMARK_ALPHA,
            rotation=CENTER_WATERMARK_ROTATION,
            linespacing=1.18,
            zorder=50,
        )
    except Exception:
        pass

    # Header
    y = fig_h - 0.45
    text(margin_x + 0.15, y, "近40個交易日｜五大分點共識淨買超成本 TOP15", 28, NAVY, BOLD)
    y -= 0.48
    text(margin_x + 0.18, y, f"追蹤分點：{'、'.join(TRACKED_BROKERS)}", 14, NAVY2, BOLD)
    y -= 0.30
    effective_days_text = effective_days if effective_days > 0 else "-"
    text(margin_x + 0.18, y, f"統計期間：近 {effective_days_text} 個有效交易日｜{period_text}　｜　同標的合併計算　｜　單位：萬元", 13, TEXT, BOLD)

    # 小型事件註解列：取代原本三個大 KPI 方框，避免版面過重
    y -= 0.28
    legend_y = y - legend_h
    rounded(margin_x, legend_y, content_w, legend_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)

    text(margin_x + 0.25, legend_y + legend_h / 2, f"TOP15淨買超成本：{fmt_wan(total_net_amount)}", 13.5, RED if total_net_amount >= 0 else GREEN, BOLD)

    legend_items = EVENT_LEGEND_ITEMS

    lx = margin_x + 2.62
    for code_name, desc in legend_items:
        rounded(lx, legend_y + 0.10, 0.32, 0.25, fc="#334155", ec="#334155", lw=0.8, r=0.07)
        text(lx + 0.16, legend_y + legend_h / 2, code_name, 10, WHITE, BOLD, ha="center")
        text(lx + 0.39, legend_y + legend_h / 2, desc, 9.4, TEXT, FONT)
        lx += 2.05 if code_name in {"A", "B"} else 1.93

    y = legend_y - gap

    # Table
    table_top = y
    rounded(margin_x, table_top - table_h, content_w, table_h, fc=WHITE, ec=NAVY, lw=1.2, r=0.08)
    rect(margin_x, table_top - section_title_h, content_w, section_title_h, fc=NAVY)
    text(margin_x + 0.30, table_top - section_title_h / 2, "共識淨買超成本 TOP15", 19, WHITE, BOLD)

    headers = ["排名", "標的", "淨買超成本", "分點數", "事件", "參與分點 / 報酬率"]
    col_w = [0.70, 2.15, 1.45, 0.65, 0.85, 6.40]

    header_y_top = table_top - section_title_h
    rect(margin_x, header_y_top - header_h, content_w, header_h, fc=HEADER_BG, ec=BORDER, lw=0.6)

    x = margin_x
    for h, w in zip(headers, col_w):
        text(x + w / 2, header_y_top - header_h / 2, h, 12, NAVY, BOLD, ha="center")
        ax.plot([x, x], [table_top - table_h, header_y_top], color=BORDER, linewidth=0.6)
        x += w
    ax.plot([margin_x + content_w, margin_x + content_w], [table_top - table_h, header_y_top], color=BORDER, linewidth=0.6)

    data_y = header_y_top - header_h
    if not rows:
        rect(margin_x, data_y - row_h, content_w, row_h, fc=WHITE, ec=BORDER, lw=0.6)
        text(margin_x + content_w / 2, data_y - row_h / 2, "近40個交易日沒有淨買超成本為正的標的", 13, MUTED, BOLD, ha="center")
    else:
        for i, r in enumerate(rows):
            ry = data_y - (i + 1) * row_h
            rect(margin_x, ry, content_w, row_h, fc=WHITE if i % 2 == 0 else ROW_ALT, ec=BORDER, lw=0.5)

            net_color = RED if r["net_amount"] > 0 else GREEN if r["net_amount"] < 0 else TEXT
            values = [
                str(i + 1),
                fit(r["target"], 14),
                fmt_wan(r["net_amount"]),
                str(r["broker_count"]),
                r["events"],
                None,
            ]

            colors = [TEXT, TEXT, net_color, TEXT, NAVY2, TEXT]
            aligns = ["center", "left", "right", "center", "center", "left"]
            bolds = [True, True, True, True, True, True]

            x = margin_x
            for col_idx, (val, w, c, a, is_bold) in enumerate(zip(values, col_w, colors, aligns, bolds)):
                if col_idx == len(values) - 1:
                    draw_participant_brokers_cell(x, ry + row_h / 2, r, w, size=13)
                    x += w
                    continue

                display_val = fit_to_cell_width(val, max(0.2, w - 0.24), size=14, fp=BOLD if is_bold else FONT)
                px = x + (w / 2 if a == "center" else 0.12 if a == "left" else w - 0.12)
                text(px, ry + row_h / 2, display_val, 14, c, BOLD if is_bold else FONT, ha=a)
                x += w


    text(
    fig_w / 2,
    0.24,
    f"{TOP15_LOW_TRADED_COVERAGE_SYMBOL} 代表當日成交價覆蓋率低於 "
    f"{TOP15_TRADED_PRICE_COVERAGE_NOTE_THRESHOLD_PCT:.0f}%｜"
    "本圖為籌碼追蹤整理，不構成投資建議。",
    11.5,
    MUTED,
    FONT,
    ha="center",
)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)



# ══════════════════════════════════════════════════════════════════════
# 本週｜全市場分點標的共識買賣超金額 TOP15
# ══════════════════════════════════════════════════════════════════════



def draw_all_broker_win_rate_stats_image(target: date, output_path: Path):
    """
    新增圖片：所有分點勝率統計。

    一張圖直接顯示 Google Sheet「勝率統計」內所有分點，版面與既有圖卡一致：
    深藍標題、白底表格、交錯列、中央淡色浮水印與風險提醒。
    """
    rows = read_all_broker_win_rate_stats_from_gsheet()
    n = len(rows)

    print(
        f"  ✅ 勝率統計圖版面：{WIN_RATE_STATS_LAYOUT_VERSION}｜"
        "欄位：A勝率、A事件數、總勝率、總事件數、持有天數、加權報酬"
    )

    # 新增 A／總事件數兩欄後，勝率統計圖加寬，避免勝率與事件數被截斷。
    fig_w = 15.0
    margin_x = 0.40
    content_w = fig_w - 2 * margin_x
    panel_gap = 0.18
    panel_w = (content_w - panel_gap) / 2

    top_h = 1.28
    summary_h = 0.55
    gap = 0.18
    section_title_h = 0.55
    header_h = 0.46
    row_h = 0.46 if n <= 120 else 0.42
    footer_h = 0.45

    rows_per_panel = max(1, math.ceil(max(n, 1) / 2))
    table_h = header_h + rows_per_panel * row_h
    fig_h = top_h + summary_h + gap + section_title_h + table_h + footer_h
    fig_h = max(fig_h, 8.5)

    BG = "#F6F8FB"
    WHITE = "#FFFFFF"
    NAVY = "#061D3D"
    NAVY2 = "#0B2E5B"
    RED = "#D92323"
    GREEN = "#16803C"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#C9D5E3"
    ROW_ALT = "#FAFCFF"
    HEADER_BG = "#F3F7FC"

    font_path = get_font_path(False)
    bold_path = get_font_path(True)
    FONT = font_manager.FontProperties(fname=font_path)
    BOLD = font_manager.FontProperties(fname=bold_path)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=BG)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def rounded(x, y, w, h, fc=WHITE, ec=BORDER, lw=1.2, r=0.12, z=1):
        patch = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc, zorder=z,
        )
        ax.add_patch(patch)
        return patch

    def rect(x, y, w, h, fc=WHITE, ec=None, lw=0.8, z=1):
        patch = patches.Rectangle(
            (x, y), w, h,
            linewidth=lw if ec else 0,
            edgecolor=ec, facecolor=fc, zorder=z,
        )
        ax.add_patch(patch)
        return patch

    def text_draw(x, y, s, size=12, color=TEXT, fp=None, ha="left", va="center", z=5):
        ax.text(
            x, y, str(s), fontsize=size, color=color,
            fontproperties=fp or FONT, ha=ha, va=va, zorder=z,
        )

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def measure_text_width(s, size=11.5, fp=None):
        ghost = ax.text(0, 0, str(s), fontsize=size, fontproperties=fp or FONT, alpha=0)
        bb = ghost.get_window_extent(renderer=renderer)
        ghost.remove()
        x0_disp, y0_disp = ax.transData.transform((0, 0))
        x1_disp = x0_disp + bb.width
        x0_data = ax.transData.inverted().transform((x0_disp, y0_disp))[0]
        x1_data = ax.transData.inverted().transform((x1_disp, y0_disp))[0]
        return x1_data - x0_data

    def fit_to_cell_width(s, cell_w, size=11.5, fp=None):
        s = str(s or "")
        if not s:
            return ""
        max_w = max(float(cell_w), 0.08)
        if measure_text_width(s, size=size, fp=fp) <= max_w:
            return s

        ellipsis = "…"
        low, high = 0, len(s)
        best = ellipsis
        while low <= high:
            mid = (low + high) // 2
            candidate = s[:mid] + ellipsis
            if measure_text_width(candidate, size=size, fp=fp) <= max_w:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def fmt_rate(value, signed=False):
        if value is None:
            return "-"
        number = safe_float(value, None)
        if number is None:
            return "-"
        return f"{number:+.1f}%" if signed else f"{number:.1f}%"

    def fmt_hold_days(value):
        if value is None:
            return "-"
        number = safe_float(value, None)
        if number is None:
            return "-"
        return f"{number:.1f} 天"

    def rate_color(value, threshold=None):
        if value is None:
            return TEXT
        number = safe_float(value, 0)
        if threshold is not None:
            return RED if number >= threshold else GREEN
        return RED if number > 0 else GREEN if number < 0 else TEXT

    # 中央淡色浮水印，與其他圖片維持一致。
    try:
        ax.text(
            0.5, 0.50, CENTER_WATERMARK_TEXT,
            transform=ax.transAxes,
            ha="center", va="center",
            fontsize=CENTER_WATERMARK_FONT_SIZE,
            fontproperties=BOLD,
            color="#2C3440",
            alpha=CENTER_WATERMARK_ALPHA,
            rotation=CENTER_WATERMARK_ROTATION,
            linespacing=1.18,
            zorder=50,
        )
    except Exception:
        pass

    y = fig_h - 0.45
    text_draw(margin_x + 0.15, y, "所有分點勝率統計", 30, NAVY, BOLD)
    y -= 0.46
    text_draw(
        margin_x + 0.18,
        y,
        "A勝率、A事件數＝基礎買超（100–159萬）｜總勝率、總事件數、平均持有天數與加權報酬率＝全部 A+B+C+D+E 合併",
        13,
        TEXT,
        BOLD,
    )

    y -= 0.28
    summary_y = y - summary_h
    rounded(margin_x, summary_y, content_w, summary_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
    text_draw(margin_x + 0.25, summary_y + summary_h / 2, f"資料來源：{SHEET_STAT}", 13.5, NAVY2, BOLD)
    text_draw(margin_x + 3.15, summary_y + summary_h / 2, f"共 {n} 個分點", 13.5, NAVY, BOLD)
    text_draw(
        margin_x + 5.15,
        summary_y + summary_h / 2,
        f"統計基準日：{target:%Y/%m/%d}",
        12.5,
        TEXT,
        FONT,
    )

    y = summary_y - gap
    rounded(margin_x, y - section_title_h, content_w, section_title_h, fc=NAVY, ec=NAVY, lw=1.0, r=0.08)
    text_draw(margin_x + 0.30, y - section_title_h / 2, "全分點 A／ABCDE總勝率、A／ABCDE總事件數、持有天數與加權報酬", 19, WHITE, BOLD)
    table_top = y - section_title_h

    headers = ["序號", "分點", "A勝率", "A事件數", "總勝率", "總事件數", "持有天數", "加權報酬"]
    # 每個左右分欄寬度固定為 7.01，讓兩組勝率與事件數都能完整顯示。
    col_w = [0.42, 1.45, 0.75, 0.80, 0.75, 0.84, 0.88, 1.12]

    def draw_panel(panel_x: float, panel_rows: list[dict], global_start_index: int):
        rounded(panel_x, table_top - table_h, panel_w, table_h, fc=WHITE, ec=NAVY, lw=1.0, r=0.06)
        rect(panel_x, table_top - header_h, panel_w, header_h, fc=HEADER_BG, ec=BORDER, lw=0.6)

        x = panel_x
        for header, width in zip(headers, col_w):
            text_draw(x + width / 2, table_top - header_h / 2, header, 11.0, NAVY, BOLD, ha="center")
            ax.plot([x, x], [table_top - table_h, table_top], color=BORDER, linewidth=0.55)
            x += width
        ax.plot([panel_x + panel_w, panel_x + panel_w], [table_top - table_h, table_top], color=BORDER, linewidth=0.55)

        if not panel_rows:
            ry = table_top - header_h - row_h
            rect(panel_x, ry, panel_w, row_h, fc=WHITE, ec=BORDER, lw=0.5)
            text_draw(panel_x + panel_w / 2, ry + row_h / 2, "無資料", 11.5, MUTED, BOLD, ha="center")
            return

        for local_idx, row in enumerate(panel_rows):
            ry = table_top - header_h - (local_idx + 1) * row_h
            rect(panel_x, ry, panel_w, row_h, fc=WHITE if local_idx % 2 == 0 else ROW_ALT, ec=BORDER, lw=0.45)

            a_rate = row.get("a_win_rate")
            total_rate = row.get("total_win_rate")
            weighted_return = row.get("weighted_return")
            values = [
                str(global_start_index + local_idx + 1),
                row.get("broker", ""),
                fmt_rate(a_rate),
                f"{safe_int(row.get('a_event_count'), 0):,}",
                fmt_rate(total_rate),
                f"{safe_int(row.get('total_event_count'), 0):,}",
                fmt_hold_days(row.get("avg_hold_days")),
                fmt_rate(weighted_return, signed=True),
            ]
            colors = [
                TEXT,
                TEXT,
                rate_color(a_rate, 50),
                NAVY2,
                rate_color(total_rate, 50),
                NAVY2,
                TEXT,
                rate_color(weighted_return),
            ]
            aligns = ["center", "left", "right", "right", "right", "right", "right", "right"]
            bolds = [True, True, True, True, True, True, False, True]

            x = panel_x
            for value, width, color, align, is_bold in zip(values, col_w, colors, aligns, bolds):
                fp = BOLD if is_bold else FONT
                display_value = fit_to_cell_width(value, max(0.16, width - 0.16), size=11.4, fp=fp)
                px = x + (width / 2 if align == "center" else 0.08 if align == "left" else width - 0.08)
                text_draw(px, ry + row_h / 2, display_value, 11.4, color, fp, ha=align)
                x += width

    left_rows = rows[:rows_per_panel]
    right_rows = rows[rows_per_panel:]
    draw_panel(margin_x, left_rows, 0)
    draw_panel(margin_x + panel_w + panel_gap, right_rows, rows_per_panel)

    text_draw(
        fig_w / 2,
        0.18,
        "本圖為歷史勝率統計整理，不構成投資建議；歷史績效不代表未來結果。",
        10.8,
        MUTED,
        FONT,
        ha="center",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)

IMAGE_ACTION_DAILY_BUNDLE = "精選五分點每日圖"
IMAGE_ACTION_CONSENSUS_BUY = "近一個月共識淨買超TOP15"
IMAGE_ACTION_WEEKLY_WARRANT = "本週權證共識買賣超TOP15"
IMAGE_ACTION_BROKER_10D = "近10日分點買賣明細圖"
IMAGE_ACTION_WIN_RATE_STATS = "所有分點勝率統計圖"
IMAGE_ACTION_ALL = "全部圖片"


def normalize_image_action(action_text: str) -> str:
    """
    將 GitHub Actions / CLI 傳進來的選項轉成程式內部動作。

    支援常見名稱：
    - 精選五分點每日圖 / 精選5分點當日買賣超產圖 / 每日精選分點買賣超追蹤
    - 近一個月共識淨買超TOP15
    - 本週權證共識買賣超TOP15 / 近7／14／21日權證分點共識TOP15
    - 近10日分點買賣明細圖 / 近10日分點明細 / 近10日分點買賣明細
    - 所有分點勝率統計圖 / 全分點勝率統計
    - 全部圖片
    """
    raw = str(action_text or "").strip()
    key = re.sub(r"[\s_\-｜|/\\]+", "", raw).lower()

    if not key:
        return IMAGE_ACTION_DAILY_BUNDLE

    if "不使用快取" in raw or "重新抓完整資料" in raw or "強制重抓" in raw:
        return IMAGE_ACTION_ALL

    if (
        "勝率統計" in raw
        or ("勝率" in raw and "分點" in raw)
        or "winrate" in key
        or "brokerstats" in key
    ):
        return IMAGE_ACTION_WIN_RATE_STATS

    if "全部" in raw or key in {"all", "全部圖片", "全部產圖"}:
        return IMAGE_ACTION_ALL

    if (
        "近10日分點" in raw
        or "10日分點" in raw
        or "10dbroker" in key
        or "broker10d" in key
        or "近10日分點買賣明細圖" in raw
    ):
        return IMAGE_ACTION_BROKER_10D

    if (
        "本週" in raw
        or "近7" in raw
        or "7日" in raw
        or "近14" in raw
        or "14日" in raw
        or "近21" in raw
        or "21日" in raw
        or "買賣超金額各" in raw
        or "權證分點共識" in raw
        or "weekly" in key
        or "warrantconsensus" in key
    ):
        return IMAGE_ACTION_WEEKLY_WARRANT

    if "近一個月" in raw or "共識淨買超成本" in raw or "五大分點共識" in raw or "consensus" in key:
        return IMAGE_ACTION_CONSENSUS_BUY

    return IMAGE_ACTION_DAILY_BUNDLE


def _warrant_consensus_warrant_count(value) -> int:
    """從快取欄位「權證代號 / 權證清單 / 權證檔數」推回同標的涵蓋權證檔數。"""
    s = strip_gsheet_text_prefix(value).strip()
    if not s:
        return 0

    m = re.search(r"共\s*(\d+)\s*檔", s)
    if m:
        return safe_int(m.group(1), 0)

    parts = [p.strip() for p in re.split(r"[；;、,\s]+", s) if p.strip()]
    if parts:
        return len(parts)

    return 1


def _parse_cache_update_datetime(value):
    """解析快取工作表的更新時間，供同一統計日期挑選最新快照使用。"""
    raw = strip_gsheet_text_prefix(value)
    if not raw or raw == "-":
        return None

    try:
        ts = pd.to_datetime(raw, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    except Exception:
        return None


def filter_latest_cache_snapshot(
    df: pd.DataFrame,
    cache_name: str = "快取",
    prefer_max_run_id: bool = False,
) -> pd.DataFrame:
    """
    同一個統計日期的快取表可能因為 GitHub Action 重跑而累積多個 run_id。

    圖片端若只用「統計日期」過濾，會把舊 run 與新 run 一起讀進來，
    造成 TOP15 / TOP10 出現同標的重複列。

    處理方式：
    1. 預設仍沿用原本邏輯：優先用「更新時間」最大者挑最新 run_id；
       沒有可解析更新時間時，用 run_id 字串排序作為備援。
    2. 若 prefer_max_run_id=True，則直接用 run_id 字串最大者作為最新快照。
       這是給「快取_近10日分點買賣明細」使用，避免指定圖卡日期仍是前一交易日時，
       先被統計日期卡住而吃到舊 run_id。
    3. 若工作表沒有 run_id，則不硬切更新時間，避免同一批資料各列更新秒數不同時誤刪。
       後續仍會再用標的層級去重 / 合併。
    """
    if df is None or df.empty or "run_id" not in df.columns:
        return df

    run_series = df["run_id"].astype(str).map(strip_gsheet_text_prefix).str.strip()

    if prefer_max_run_id:
        run_ids = [str(x).strip() for x in run_series.tolist() if str(x).strip()]
        valid_run_ids = [x for x in run_ids if re.fullmatch(r"\d{8}_\d{6}", x)]
        best_run_id = max(valid_run_ids) if valid_run_ids else (max(run_ids) if run_ids else "")

        if not best_run_id:
            return df

        filtered = df[run_series == best_run_id].copy()
        if not filtered.empty:
            if len(filtered) != len(df):
                print(f"  ✅ {cache_name} 已自動套用最新 run_id：{best_run_id}｜{len(df):,} 筆 → {len(filtered):,} 筆")
            else:
                print(f"  ✅ {cache_name} 採用最新 run_id：{best_run_id}｜{len(filtered):,} 筆")
            return filtered

        return df

    best_run_id = ""
    best_score = None

    for _, row in df.iterrows():
        run_id = strip_gsheet_text_prefix(row.get("run_id", "")).strip()
        if not run_id:
            continue

        update_dt = _parse_cache_update_datetime(row.get("更新時間", "")) if "更新時間" in df.columns else None
        update_score = update_dt.timestamp() if update_dt else float("-inf")
        score = (update_score, run_id)

        if best_score is None or score > best_score:
            best_score = score
            best_run_id = run_id

    if not best_run_id:
        return df

    filtered = df[run_series == best_run_id].copy()
    if not filtered.empty and len(filtered) != len(df):
        print(f"  ✅ {cache_name} 已套用最新快照：run_id={best_run_id}｜{len(df):,} 筆 → {len(filtered):,} 筆")
        return filtered

    return df


def dedupe_ranked_rows_by_underlying(rows: list[dict], label: str = "") -> list[dict]:
    """
    排名圖最後一道保護：同一標的只保留一列。

    正常情況快取端已完成同標的合併；若工作表殘留重複快照或重複列，
    圖片端不能再把同一標的畫兩次，也不能讓合計金額被重複計入。
    """
    if not rows:
        return []

    ordered = sorted(rows, key=lambda x: (safe_int(x.get("rank"), 999999), -safe_float(x.get("rank_amount"), 0)))
    seen = set()
    out = []
    removed = 0

    for row in ordered:
        key = str(row.get("underlying") or row.get("target") or "").strip()
        if not key:
            key = str(row.get("target", "")).strip()

        if key in seen:
            removed += 1
            continue

        seen.add(key)
        out.append(row)

    if removed > 0:
        prefix = f"{label}：" if label else ""
        print(f"  ⚠️ {prefix}已移除同標的重複列 {removed:,} 筆，避免 TOP 排名重複顯示。")

    return out


def merge_broker_10d_rows_by_underlying(rows: list[dict]) -> list[dict]:
    """
    近10日分點買賣明細以「分點 + 標的」作為唯一顯示單位。

    - 先移除完全相同的列，避免同一批快取重複同步時金額被加倍。
    - 再把同一分點、同一標的的不同列合併，確保 TOP10 不會出現重複標的。
    - 若合併後買進 > 賣出，歸為買超；賣出 > 買進，歸為賣超。
    """
    if not rows:
        return []

    unique_rows = []
    fingerprints = set()
    exact_removed = 0

    for row in rows:
        fp = (
            str(row.get("broker", "")).strip(),
            str(row.get("underlying") or row.get("target") or "").strip(),
            str(row.get("direction", "")).strip(),
            round(safe_float(row.get("buy_amount"), 0), 4),
            round(safe_float(row.get("sell_amount"), 0), 4),
            round(safe_float(row.get("net_buy_amount"), 0), 4),
            round(safe_float(row.get("net_sell_amount"), 0), 4),
            str(row.get("buy_return", "")),
            str(row.get("sell_return", "")),
            str(row.get("warrant_list", "")),
        )
        if fp in fingerprints:
            exact_removed += 1
            continue
        fingerprints.add(fp)
        unique_rows.append(row)

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in unique_rows:
        key = (
            str(row.get("broker", "")).strip(),
            str(row.get("underlying") or row.get("target") or "").strip(),
        )
        grouped[key].append(row)

    merged_rows = []
    merged_group_count = 0

    for (_, _), items in grouped.items():
        if len(items) == 1:
            merged_rows.append(items[0])
            continue

        merged_group_count += 1
        base = dict(items[0])

        buy_qty = sum(safe_float(i.get("buy_qty"), 0) for i in items)
        sell_qty = sum(safe_float(i.get("sell_qty"), 0) for i in items)
        buy_amount = sum(safe_float(i.get("buy_amount"), 0) for i in items)
        sell_amount = sum(safe_float(i.get("sell_amount"), 0) for i in items)
        net_amount = buy_amount - sell_amount

        def weighted_return(return_key: str, amount_key: str, direction_name: str):
            weighted_sum = 0.0
            weighted_amount = 0.0
            for item in items:
                ret = normalize_return_pct(item.get(return_key))
                if ret is None and str(item.get("direction", "")).strip() == direction_name:
                    ret = normalize_return_pct(item.get("primary_return"))
                amount = safe_float(item.get(amount_key), 0)
                if ret is None or amount <= 0:
                    continue
                weighted_sum += ret * amount
                weighted_amount += amount
            return round(weighted_sum / weighted_amount, 2) if weighted_amount > 0 else None

        buy_return = weighted_return("buy_return", "buy_amount", "買超")
        sell_return = weighted_return("sell_return", "sell_amount", "賣超")

        warrant_parts = []
        warrant_seen = set()
        for item in items:
            raw_list = strip_gsheet_text_prefix(item.get("warrant_list", ""))
            if raw_list:
                for part in re.split(r"[；;]", raw_list):
                    part = part.strip()
                    if part and part not in warrant_seen:
                        warrant_seen.add(part)
                        warrant_parts.append(part)

        if net_amount > 0:
            direction = "買超"
            net_buy_amount = net_amount
            net_sell_amount = 0.0
            primary_return = buy_return
        elif net_amount < 0:
            direction = "賣超"
            net_buy_amount = 0.0
            net_sell_amount = abs(net_amount)
            primary_return = sell_return
        else:
            direction = "持平"
            net_buy_amount = 0.0
            net_sell_amount = 0.0
            primary_return = None

        outcome = "-"
        if primary_return is not None:
            outcome = "勝" if safe_float(primary_return, 0) > 0 else "敗"

        underlying_10d_return = None
        for item in items:
            underlying_10d_return = normalize_return_pct(item.get("underlying_10d_return"))
            if underlying_10d_return is not None:
                break

        base.update({
            "underlying_10d_return": underlying_10d_return,
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "net_buy_amount": net_buy_amount,
            "net_sell_amount": net_sell_amount,
            "direction": direction,
            "buy_return": buy_return,
            "sell_return": sell_return,
            "primary_return": primary_return,
            "outcome": outcome,
            "warrant_count": len(warrant_seen) if warrant_seen else sum(safe_int(i.get("warrant_count"), 0) for i in items),
            "warrant_list": "；".join(warrant_parts) if warrant_parts else strip_gsheet_text_prefix(base.get("warrant_list", "")),
        })
        merged_rows.append(base)

    removed_total = exact_removed + max(0, len(unique_rows) - len(merged_rows))
    if exact_removed > 0 or merged_group_count > 0:
        print(
            f"  ⚠️ 近10日分點明細已按標的去重合併："
            f"完全重複移除 {exact_removed:,} 筆，合併同標的群組 {merged_group_count:,} 組，"
            f"{len(rows):,} 筆 → {len(merged_rows):,} 筆。"
        )

    merged_rows.sort(
        key=lambda x: max(
            safe_float(x.get("buy_amount"), 0),
            safe_float(x.get("sell_amount"), 0),
            safe_float(x.get("net_buy_amount"), 0),
            safe_float(x.get("net_sell_amount"), 0),
        ),
        reverse=True,
    )
    return merged_rows


def read_warrant_consensus_multi_section_table(
    sheet_name: str,
    needed_cols: list[str] | None = None,
) -> pd.DataFrame:
    """
    讀取「近7／14／21日權證共識」這種多區塊工作表。

    主程式目前會在同一張工作表由上往下放入三個區塊：
    - 標題列
    - 統計期間／統計分點說明列
    - 欄位表頭列
    - 排名資料列

    因此不能使用 read_gsheet_table() 把第 1 列固定當成表頭。
    本函式會逐列偵測每個區塊的真正表頭，再把三個區塊資料合併成同一個 DataFrame。
    同時相容舊版「第 1 列就是表頭」的平面工作表。
    """
    try:
        values = worksheet_values(sheet_name)
    except Exception as exc:
        print(f"  ⚠️ 讀取工作表失敗：{sheet_name}｜{type(exc).__name__}: {exc}")
        return pd.DataFrame()

    if not values:
        return pd.DataFrame()

    normalized_rows = [
        [strip_gsheet_text_prefix(cell) for cell in list(row)]
        for row in values
    ]

    header_required = {
        "資料範圍",
        "統計日期",
        "統計天數",
        "排名類型",
        "排名",
        "標的股",
        "排名金額",
    }

    current_headers: list[str] | None = None
    records: list[dict] = []
    detected_header_count = 0

    for raw_row in normalized_rows:
        row = [str(cell).strip() for cell in raw_row]
        row_set = {cell for cell in row if cell}

        # 每個 7／14／21 日區塊都會重複出現完整表頭。
        # 只要關鍵欄位大多存在，就視為新的表頭列。
        if (
            "統計天數" in row_set
            and "排名類型" in row_set
            and "排名" in row_set
            and len(header_required & row_set) >= 6
        ):
            current_headers = row[:]
            while current_headers and current_headers[-1] == "":
                current_headers.pop()
            detected_header_count += 1
            continue

        if not current_headers:
            continue

        row_values = list(raw_row)
        if len(row_values) < len(current_headers):
            row_values += [""] * (len(current_headers) - len(row_values))
        elif len(row_values) > len(current_headers):
            row_values = row_values[:len(current_headers)]

        rec = {
            header: strip_gsheet_text_prefix(row_values[idx])
            for idx, header in enumerate(current_headers)
            if header
        }

        stat_days = safe_int(rec.get("統計天數"), 0)
        rank_type = strip_gsheet_text_prefix(rec.get("排名類型", "")).strip()
        stat_date = _pick_first_existing_date(rec, ["統計日期", "日期", "目標日期"])

        # 標題列、說明列、空白列與「無符合排名資料」列都不會同時具備這三項。
        if stat_days <= 0 or not rank_type or stat_date is None:
            continue

        records.append(rec)

    if not records:
        print(
            f"  ⚠️ {sheet_name} 未辨識到有效排名資料。"
            f"偵測到表頭區塊 {detected_header_count} 個。"
        )
        return pd.DataFrame()

    all_headers: list[str] = []
    for rec in records:
        for header in rec.keys():
            if header and header not in all_headers:
                all_headers.append(header)

    df = pd.DataFrame(
        [{header: rec.get(header, "") for header in all_headers} for rec in records],
        columns=all_headers,
    ).fillna("")

    if needed_cols is not None:
        keep_cols = [col for col in needed_cols if col in df.columns]
        df = df[keep_cols].copy()

    print(
        f"  ✅ 已解析多區塊工作表：{sheet_name}｜"
        f"表頭區塊 {detected_header_count} 個｜有效資料 {len(df):,} 筆"
    )
    return df


def filter_warrant_consensus_selected_scope(df: pd.DataFrame) -> pd.DataFrame:
    """
    近7／14／21日共識表雖然只在 RUN_MODE=2 更新，
    實際排名內容是指定精選分點，因此「資料範圍」會寫成「精選13分點」
    （數字會依主程式設定變動），不是「全分點」。

    優先讀取「精選N分點」；若是舊版資料只有「全分點」，才退回全分點。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if "資料範圍" not in df.columns:
        return df

    scope_series = df["資料範圍"].astype(str).map(strip_gsheet_text_prefix).str.strip()
    unique_scopes = [scope for scope in scope_series.drop_duplicates().tolist() if scope]

    selected_scopes = [
        scope
        for scope in unique_scopes
        if re.fullmatch(r"精選\d+分點", scope)
        or ("精選" in scope and "分點" in scope)
    ]

    if selected_scopes:
        part = df[scope_series.isin(selected_scopes)].copy()
        print(f"  ✅ 權證共識資料範圍：{'、'.join(selected_scopes)}｜{len(part):,} 筆")
        return part

    if DATA_SCOPE_ALL in unique_scopes:
        part = df[scope_series == DATA_SCOPE_ALL].copy()
        print(f"  ✅ 權證共識沿用舊版資料範圍：{DATA_SCOPE_ALL}｜{len(part):,} 筆")
        return part

    print(
        f"  ⚠️ 權證共識工作表找不到「精選N分點」或「{DATA_SCOPE_ALL}」資料，"
        f"現有資料範圍：{'、'.join(unique_scopes) if unique_scopes else '-'}"
    )
    return pd.DataFrame(columns=df.columns)


def read_warrant_consensus_7d_rows_from_gsheet(
    target: date | None = None,
    window_days: int = 7,
) -> tuple[list[dict], list[dict], str, date | None]:
    """
    直接讀取「快取_近7日權證分點共識TOP15」內指定期間資料。

    重要：
    - 工作表同時包含近 7／14／21 日三個獨立區塊。
    - 自動辨識每個區塊真正的表頭，不再把第 1 列標題誤當表頭。
    - 實際排名是指定精選分點，因此讀取「資料範圍=精選N分點」。
    - 依 window_days 固定切出 7、14 或 21 日資料，三個期間不會混在一起。
    - 最終排名來源仍完全以 warrant_backtest.py 寫入的快取為準。

    回傳：
    - 共識買超標的 rows
    - 共識賣超標的 rows
    - 統計期間文字
    - 實際採用的統計日期
    """
    window_days = max(safe_int(window_days, 7), 1)

    needed_cols = [
        "資料範圍",
        "統計日期", "日期", "目標日期", "統計期間", "統計天數", "有效日期數", "第一筆日期", "最後筆日期",
        "排名類型", "方向", "排名",
        "權證代號", "權證名稱", "權證清單", "權證檔數",
        "標的股", "標的代號", "標的", "標的名稱", "股票名稱",
        "排名金額", "買進金額", "賣出金額", "淨買超金額", "淨賣超金額",
        "買進股數", "賣出股數",
        "參與分點數", "同向分點數", "反向分點數", "主要同向分點", "參與分點",
        "完成狀態", "更新時間", "run_id", "分點明細_JSON",
    ]

    df = read_warrant_consensus_multi_section_table(
        SHEET_WARRANT_CONSENSUS_7D,
        needed_cols,
    )
    df = filter_warrant_consensus_selected_scope(df)

    if df.empty:
        return [], [], "無資料", None

    if "統計天數" not in df.columns:
        print(f"  ⚠️ {SHEET_WARRANT_CONSENSUS_7D} 找不到「統計天數」欄位。")
        return [], [], "無資料", None

    stat_days_series = df["統計天數"].map(lambda value: safe_int(value, 0))
    df = df[stat_days_series == window_days].copy()

    if df.empty:
        print(f"  ⚠️ {SHEET_WARRANT_CONSENSUS_7D} 找不到近{window_days}日資料。")
        return [], [], "無資料", None

    def pick_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    available_dates = []
    for _, row in df.iterrows():
        stat_date = pick_date(row)
        if stat_date:
            available_dates.append(stat_date)

    if not available_dates:
        return [], [], "無資料", None

    if target is not None:
        valid_dates = [stat_date for stat_date in available_dates if stat_date <= target]
        chosen_date = max(valid_dates) if valid_dates else max(available_dates)
    else:
        chosen_date = max(available_dates)

    df = df[df.apply(lambda row: pick_date(row) == chosen_date, axis=1)].copy()
    df = filter_latest_cache_snapshot(
        df,
        f"{SHEET_WARRANT_CONSENSUS_7D}（近{window_days}日）",
    )

    if df.empty:
        return [], [], "無資料", chosen_date

    period_values = [
        strip_gsheet_text_prefix(value)
        for value in df.get("統計期間", [])
        if strip_gsheet_text_prefix(value)
    ]
    period_text = period_values[0] if period_values else ""

    if not period_text:
        first_dates = [
            parse_date_value(value)
            for value in df.get("第一筆日期", [])
            if parse_date_value(value)
        ]
        last_dates = [
            parse_date_value(value)
            for value in df.get("最後筆日期", [])
            if parse_date_value(value)
        ]
        if first_dates and last_dates:
            period_text = f"{min(first_dates):%Y/%m/%d} ～ {max(last_dates):%Y/%m/%d}"
        else:
            period_text = "無資料"

    def normalize_rank_type(raw_value: str) -> str:
        value = strip_gsheet_text_prefix(raw_value).strip()
        if value in {"共識買超", "買超", "buy", "BUY"}:
            return "共識買超"
        if value in {"共識賣超", "賣超", "sell", "SELL"}:
            return "共識賣超"
        return value

    def build_rows(rank_type: str) -> list[dict]:
        rows = []

        for _, row in df.iterrows():
            row_rank_type = normalize_rank_type(
                _pick_first_existing_value(row, ["排名類型", "方向"])
            )
            if row_rank_type != rank_type:
                continue

            underlying, underlying_name, target_label = resolve_target_identity(
                row,
                code_cols=["標的股", "標的代號", "標的"],
                name_cols=["標的名稱", "股票名稱"],
                raw_target_cols=["標的", "標的名稱", "股票名稱"],
                warrant_text_cols=["權證名稱", "權證清單", "權證代號"],
            )

            buy_amount = safe_float(
                _pick_first_existing_value(row, ["買進金額"]),
                0,
            )
            sell_amount = safe_float(
                _pick_first_existing_value(row, ["賣出金額"]),
                0,
            )
            net_buy_amount = safe_float(
                _pick_first_existing_value(row, ["淨買超金額"]),
                buy_amount - sell_amount,
            )
            net_sell_amount = safe_float(
                _pick_first_existing_value(row, ["淨賣超金額"]),
                sell_amount - buy_amount,
            )

            rank_amount = safe_float(
                _pick_first_existing_value(row, ["排名金額"]),
                0,
            )
            if rank_amount == 0:
                rank_amount = (
                    net_buy_amount
                    if rank_type == "共識買超"
                    else net_sell_amount
                )

            warrant_count = safe_int(
                _pick_first_existing_value(row, ["權證檔數"]),
                0,
            )
            if warrant_count <= 0:
                warrant_count = _warrant_consensus_warrant_count(
                    _pick_first_existing_value(row, ["權證清單", "權證代號"])
                )

            same_direction_count = safe_int(
                _pick_first_existing_value(row, ["同向分點數", "參與分點數"]),
                0,
            )
            opposite_direction_count = safe_int(
                _pick_first_existing_value(row, ["反向分點數"]),
                0,
            )
            broker_count = safe_int(
                _pick_first_existing_value(row, ["參與分點數", "同向分點數"]),
                0,
            )
            main_brokers = strip_gsheet_text_prefix(
                _pick_first_existing_value(row, ["主要同向分點", "參與分點"])
            ).replace("；", "、")

            rows.append({
                "stat_date": chosen_date,
                "window_days": window_days,
                "period": period_text,
                "rank_type": rank_type,
                "rank": safe_int(_pick_first_existing_value(row, ["排名"]), 0),
                "underlying": underlying,
                "underlying_name": underlying_name,
                "target": target_label or underlying,
                "rank_amount": rank_amount,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "net_buy_amount": net_buy_amount,
                "net_sell_amount": net_sell_amount,
                "buy_qty": safe_float(_pick_first_existing_value(row, ["買進股數"]), 0),
                "sell_qty": safe_float(_pick_first_existing_value(row, ["賣出股數"]), 0),
                "warrant_count": warrant_count,
                "same_direction_count": same_direction_count,
                "opposite_direction_count": opposite_direction_count,
                "broker_count": broker_count if broker_count > 0 else same_direction_count,
                "main_brokers": main_brokers,
            })

        rows = dedupe_ranked_rows_by_underlying(
            rows,
            label=f"近{window_days}日{rank_type}",
        )
        rows.sort(
            key=lambda item: (
                safe_int(item.get("rank"), 999999),
                -safe_float(item.get("rank_amount"), 0),
            )
        )
        return rows[:15]

    buy_rows = build_rows("共識買超")
    sell_rows = build_rows("共識賣超")

    print(
        f"  ✅ 近{window_days}日權證共識圖資料完成："
        f"統計日期={chosen_date:%Y-%m-%d}｜"
        f"買超 {len(buy_rows)} 筆｜賣超 {len(sell_rows)} 筆｜"
        f"期間={period_text or '無資料'}"
    )

    return buy_rows, sell_rows, period_text or "無資料", chosen_date

def draw_weekly_warrant_consensus_image(target: date, output_path: Path, window_days: int = 7):
    """
    產生近 7／14／21 日指定期間的標的分點共識買賣超金額各 TOP15。

    資料來源直接讀取「快取_近7日權證分點共識TOP15」內對應期間區塊。
    排名口徑以主程式快取為準：同標的所有認購權證金額加總後排名。
    """
    window_days = max(safe_int(window_days, 7), 1)
    buy_rows, sell_rows, period_text, cache_date = read_warrant_consensus_7d_rows_from_gsheet(
        target,
        window_days=window_days,
    )

    total_buy_rank_amount = sum(safe_float(r.get("rank_amount"), 0) for r in buy_rows)
    total_sell_rank_amount = sum(safe_float(r.get("rank_amount"), 0) for r in sell_rows)
    cache_date_text = cache_date.strftime("%Y/%m/%d") if cache_date else target.strftime("%Y/%m/%d")

    fig_w = 13.0
    margin_x = 0.40
    content_w = fig_w - 2 * margin_x

    top_h = 1.30
    summary_h = 0.55
    gap = 0.20
    section_title_h = 0.55
    header_h = 0.42
    row_h = 0.48
    footer_h = 0.45

    buy_table_h = section_title_h + header_h + max(1, len(buy_rows)) * row_h
    sell_table_h = section_title_h + header_h + max(1, len(sell_rows)) * row_h

    fig_h = top_h + summary_h + gap + buy_table_h + gap + sell_table_h + footer_h
    fig_h = max(fig_h, 9.5)

    BG = "#F6F8FB"
    WHITE = "#FFFFFF"
    NAVY = "#061D3D"
    NAVY2 = "#0B2E5B"
    RED = "#D92323"
    GREEN = "#16803C"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#C9D5E3"
    ROW_ALT = "#FAFCFF"
    HEADER_BG = "#F3F7FC"

    font_path = get_font_path(False)
    bold_path = get_font_path(True)
    FONT = font_manager.FontProperties(fname=font_path)
    BOLD = font_manager.FontProperties(fname=bold_path)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=BG)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def rounded(x, y, w, h, fc=WHITE, ec=BORDER, lw=1.2, r=0.12, z=1):
        patch = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc, zorder=z
        )
        ax.add_patch(patch)
        return patch

    def rect(x, y, w, h, fc=WHITE, ec=None, lw=0.8, z=1):
        patch = patches.Rectangle((x, y), w, h, linewidth=lw if ec else 0,
                                  edgecolor=ec, facecolor=fc, zorder=z)
        ax.add_patch(patch)
        return patch

    def text(x, y, s, size=12, color=TEXT, fp=None, ha="left", va="center", z=5):
        ax.text(x, y, str(s), fontsize=size, color=color, fontproperties=fp or FONT,
                ha=ha, va=va, zorder=z)

    def fit(s, n_chars):
        s = str(s)
        return s if len(s) <= n_chars else s[:n_chars - 1] + "…"

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def measure_text_width(s, size=13.0, fp=None):
        ghost = ax.text(0, 0, str(s), fontsize=size, fontproperties=fp or FONT, alpha=0)
        bb = ghost.get_window_extent(renderer=renderer)
        ghost.remove()
        x0_disp, y0_disp = ax.transData.transform((0, 0))
        x1_disp = x0_disp + bb.width
        x0_data = ax.transData.inverted().transform((x0_disp, y0_disp))[0]
        x1_data = ax.transData.inverted().transform((x1_disp, y0_disp))[0]
        return x1_data - x0_data

    def fit_to_cell_width(s, cell_w, size=13.0, fp=None):
        s = str(s or "")
        if not s:
            return ""
        max_w = max(float(cell_w), 0.1)
        if measure_text_width(s, size=size, fp=fp) <= max_w:
            return s

        ellipsis = "…"
        ell_w = measure_text_width(ellipsis, size=size, fp=fp)
        if ell_w >= max_w:
            return ellipsis

        low, high = 0, len(s)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = s[:mid] + ellipsis
            if measure_text_width(candidate, size=size, fp=fp) <= max_w:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best or ellipsis

    def draw_clipped_text(x_left, y_center, cell_w, text_value, size=13.2, color=TEXT, fp=None, padding_left=0.12, padding_right=0.12):
        display_text = fit_to_cell_width(text_value, cell_w - padding_left - padding_right, size=size, fp=fp)
        clip_rect = patches.Rectangle((x_left, y_center - row_h / 2 + 0.02), cell_w, row_h - 0.04, linewidth=0, facecolor="none")
        ax.add_patch(clip_rect)
        t = ax.text(
            x_left + padding_left, y_center, display_text,
            fontsize=size, color=color, fontproperties=fp or FONT,
            ha="left", va="center", zorder=5, clip_on=True
        )
        t.set_clip_path(clip_rect)

    try:
        ax.text(
            0.5, 0.50, CENTER_WATERMARK_TEXT,
            transform=ax.transAxes,
            ha="center", va="center",
            fontsize=CENTER_WATERMARK_FONT_SIZE,
            fontproperties=BOLD,
            color="#2C3440",
            alpha=CENTER_WATERMARK_ALPHA,
            rotation=CENTER_WATERMARK_ROTATION,
            linespacing=1.18,
            zorder=50,
        )
    except Exception:
        pass

    # Header
    y = fig_h - 0.45
    text(margin_x + 0.15, y, f"近{window_days}日標的分點共識買賣超金額 TOP15", 30, NAVY, BOLD)
    y -= 0.45
    text(
        margin_x + 0.18,
        y,
        f"統計期間：{period_text}｜上半部買超、下半部賣超｜單位：萬元｜統計日期：{cache_date_text}",
        13,
        TEXT,
        BOLD
    )

    # Summary row
    y -= 0.28
    summary_y = y - summary_h
    rounded(margin_x, summary_y, content_w, summary_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
    text(margin_x + 0.25, summary_y + summary_h / 2, f"共識買超TOP15合計：{fmt_wan(total_buy_rank_amount)}", 13.5, NAVY, BOLD)
    text(margin_x + 3.90, summary_y + summary_h / 2, f"共識賣超TOP15合計：{fmt_wan(total_sell_rank_amount)}", 13.5, NAVY2, BOLD)
    text(margin_x + 7.55, summary_y + summary_h / 2, f"排名金額＝同標的所有權證近{window_days}日買賣互抵後金額", 12.5, TEXT, FONT)
    y = summary_y - gap

    def draw_top15_table(title: str, rows: list[dict], y_top: float, theme_color: str, amount_label: str) -> float:
        table_h = section_title_h + header_h + max(1, len(rows)) * row_h
        rounded(margin_x, y_top - table_h, content_w, table_h, fc=WHITE, ec=theme_color, lw=1.2, r=0.08)
        rect(margin_x, y_top - section_title_h, content_w, section_title_h, fc=theme_color)
        text(margin_x + 0.30, y_top - section_title_h / 2, title, 19, WHITE, BOLD)

        headers = ["排名", "標的", amount_label, "買進", "賣出", "權證數", "分點", "主要同向分點"]
        col_w = [0.55, 2.45, 1.35, 1.25, 1.25, 0.75, 0.75, 3.85]

        header_y_top = y_top - section_title_h
        rect(margin_x, header_y_top - header_h, content_w, header_h, fc=HEADER_BG, ec=BORDER, lw=0.6)

        x = margin_x
        for h, w in zip(headers, col_w):
            text(x + w / 2, header_y_top - header_h / 2, h, 12, NAVY, BOLD, ha="center")
            ax.plot([x, x], [y_top - table_h, header_y_top], color=BORDER, linewidth=0.6)
            x += w
        ax.plot([margin_x + content_w, margin_x + content_w], [y_top - table_h, header_y_top], color=BORDER, linewidth=0.6)

        data_y = header_y_top - header_h
        if not rows:
            rect(margin_x, data_y - row_h, content_w, row_h, fc=WHITE, ec=BORDER, lw=0.6)
            text(margin_x + content_w / 2, data_y - row_h / 2, "目前沒有可顯示資料", 13, MUTED, BOLD, ha="center")
        else:
            for i, r in enumerate(rows):
                ry = data_y - (i + 1) * row_h
                rect(margin_x, ry, content_w, row_h, fc=WHITE if i % 2 == 0 else ROW_ALT, ec=BORDER, lw=0.5)

                target_label = r.get("target", "") or r.get("underlying", "")
                main_brokers = r.get("main_brokers", "")

                values = [
                    str(safe_int(r.get("rank"), i + 1)),
                    fit(target_label, 16),
                    fmt_wan(r.get("rank_amount", 0)),
                    fmt_wan(r.get("buy_amount", 0)),
                    fmt_wan(r.get("sell_amount", 0)),
                    str(safe_int(r.get("warrant_count"), 0)),
                    str(safe_int(r.get("same_direction_count"), 0)),
                    fit(main_brokers, 34),
                ]
                colors = [TEXT, TEXT, theme_color, RED, GREEN, TEXT, TEXT, TEXT]
                aligns = ["center", "left", "right", "right", "right", "center", "center", "left"]
                bolds = [True, True, True, True, True, True, True, False]

                x = margin_x
                for col_idx, (val, w, c, a, is_bold) in enumerate(zip(values, col_w, colors, aligns, bolds)):
                    fp = BOLD if is_bold else FONT
                    if a == "left":
                        draw_clipped_text(x, ry + row_h / 2, w, val, size=13.2, color=c, fp=fp)
                    else:
                        display_val = fit_to_cell_width(val, max(0.2, w - 0.24), size=13.2, fp=fp)
                        px = x + (w / 2 if a == "center" else w - 0.12)
                        text(px, ry + row_h / 2, display_val, 13.2, c, fp, ha=a)
                    x += w

        return y_top - table_h

    y = draw_top15_table(f"近{window_days}日共識買超金額 TOP15", buy_rows, y, NAVY, "買超金額")
    y -= gap
    y = draw_top15_table(f"近{window_days}日共識賣超金額 TOP15", sell_rows, y, NAVY, "賣超金額")

    text(fig_w / 2, 0.18, "本圖為籌碼追蹤整理，不構成投資建議。", 11, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)


def read_broker_10d_detail_rows_from_gsheet(target: date | None = None, broker: str = "") -> tuple[list[dict], dict]:
    """
    讀取「快取_近10日分點買賣明細」，只取 資料範圍=全分點，
    並回傳單一分點的所有標的合併明細。
    """
    needed_cols = [
        "資料範圍",
        "統計日期", "日期", "目標日期", "統計期間", "統計天數", "有效日期數", "第一筆日期", "最後筆日期",
        "分點", "分點名稱", "券商代號",
        "標的股", "標的代號", "標的", "標的名稱", "股票名稱",
        "現股10日報酬率%", "標的10日報酬率%", "現股10日報酬率", "標的10日報酬率",
        "標的10日漲跌幅%", "現股10日漲跌幅%", "標的10日漲跌幅", "現股10日漲跌幅",
        "近10日買進股數", "買進股數",
        "近10日買進金額", "買進金額",
        "近10日賣出股數", "賣出股數",
        "近10日賣出金額", "賣出金額",
        "近10日淨買超股數", "淨買超股數",
        "近10日淨買超金額", "淨買超金額",
        "近10日淨賣超金額", "淨賣超金額",
        "買賣方向", "方向",
        "涉及權證檔數", "權證檔數", "權證清單",
        "買超平均報酬%", "買超平均報酬率", "買超報酬%", "買超報酬率", "買超平均損益%", "買超平均損益率", "買超損益%", "買超損益率",
        "賣超平均報酬%", "賣超平均報酬率", "賣超報酬%", "賣超報酬率", "賣超平均損益%", "賣超平均損益率", "賣超損益%", "賣超損益率",
        "買進平均報酬%", "買進平均報酬率", "買進報酬%", "買進報酬率",
        "賣出平均報酬%", "賣出平均報酬率", "賣出報酬%", "賣出報酬率",
        "平均報酬%", "平均報酬率", "報酬率", "報酬率%", "primary_return", "主要報酬率",
        # 主程式新增的分點層級加權報酬欄位。
        # 近10日圖卡上方摘要會優先使用這些快取欄位，避免圖片端只用 TOP10 重新估算而與主程式統計口徑不一致。
        "分點近10日加權平均報酬%", "分點近10日買超加權平均報酬%", "分點近10日賣超加權平均報酬%",
        "分點近10日加權平均勝報酬%", "分點近10日加權平均敗報酬%", "分點近10日盈虧比",
        "分點近10日加權期望值%", "分點近10日加權報酬權重金額",
        "分點近10日勝率", "近10日勝率", "勝率",
        "分點近10日勝筆數", "近10日勝筆數", "勝筆數",
        "分點近10日敗筆數", "近10日敗筆數", "敗筆數",
        "更新時間", "run_id",
    ]

    df = read_gsheet_table_optional(
        SHEET_BROKER_10D_DETAIL,
        needed_cols,
        filter_tracked_brokers=False,
    )
    df = filter_df_by_data_scope(df, DATA_SCOPE_ALL)

    empty_meta = {
        "broker": broker,
        "period_text": "無資料",
        "chosen_date": None,
        "win_rate": None,
        "win_count": 0,
        "loss_count": 0,
        "buy_total": 0.0,
        "sell_total": 0.0,
        "net_total": 0.0,
        "avg_buy_return": None,
        "avg_sell_return": None,
        "weighted_avg_return": None,
        "weighted_buy_return": None,
        "weighted_sell_return": None,
        "weighted_win_return": None,
        "weighted_loss_return": None,
        "profit_loss_ratio": None,
        "weighted_expectancy": None,
        "weighted_return_amount": 0.0,
    }

    if df.empty:
        return [], empty_meta

    def pick_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    # 近10日分點買賣明細要先鎖定最新 run_id，再從該批資料推統計日期。
    # 這樣即使圖卡 target_date 仍是前一個交易日，也不會先被統計日期卡住而吃到舊快照。
    df = filter_latest_cache_snapshot(
        df,
        SHEET_BROKER_10D_DETAIL,
        prefer_max_run_id=True,
    )

    available_dates = []
    for _, r in df.iterrows():
        d = pick_date(r)
        if d:
            available_dates.append(d)

    if not available_dates:
        return [], empty_meta

    if target is not None:
        valid_dates = [d for d in available_dates if d <= target]
        chosen_date = max(valid_dates) if valid_dates else max(available_dates)
    else:
        chosen_date = max(available_dates)

    df = df[df.apply(lambda r: pick_date(r) == chosen_date, axis=1)].copy()

    # 共識訊號要用同一批快取、同一統計日期的「所有分點」一起判斷，
    # 不能先篩成單一分點，否則永遠只會看到單點買 / 單點賣。
    consensus_source_df = df.copy()

    def build_broker_10d_consensus_map(source_df: pd.DataFrame):
        """
        建立近10日「同標的、同分點」的共識統計表。

        重要：
        - 先用「標的 + 分點」彙總近10日買進 / 賣出金額。
        - 同一個分點在同一檔標的，即使原始快取出現多列，也只能被算成一個分點。
        - 彙總後再依該分點的最終淨方向判定：淨買超算 1 個買方分點，淨賣超算 1 個賣方分點。
        - 避免同一分點同時出現在買方、賣方，造成「幾點同買 / 同賣」被買單或賣單列數放大。
        """
        broker_target_totals = defaultdict(lambda: defaultdict(lambda: {"buy": 0.0, "sell": 0.0}))

        for _, rr in source_df.iterrows():
            row = rr.to_dict()
            broker_name = strip_gsheet_text_prefix(_pick_first_existing_value(row, ["分點", "分點名稱"])).strip()
            if not broker_name:
                continue

            underlying, _, target_label = resolve_target_identity(
                row,
                code_cols=["標的股", "標的代號", "標的"],
                name_cols=["標的名稱", "股票名稱"],
                raw_target_cols=["標的", "標的名稱", "股票名稱"],
                warrant_text_cols=["權證清單"],
            )
            key = str(underlying or target_label or "").strip()
            if not key:
                continue

            buy_amount = safe_float(_pick_first_existing_value(row, ["近10日買進金額", "買進金額"]), 0)
            sell_amount = safe_float(_pick_first_existing_value(row, ["近10日賣出金額", "賣出金額"]), 0)
            net_buy_amount = safe_float(_pick_first_existing_value(row, ["近10日淨買超金額", "淨買超金額"]), 0)
            net_sell_amount = safe_float(_pick_first_existing_value(row, ["近10日淨賣超金額", "淨賣超金額"]), 0)

            # 正常新版快取會有買進 / 賣出金額，優先用它們彙總出同分點同標的的最終淨方向。
            # 若遇到舊版快取只有淨買超 / 淨賣超欄位，才退回使用淨額欄位補算。
            if buy_amount > 0 or sell_amount > 0:
                broker_target_totals[key][broker_name]["buy"] += max(buy_amount, 0.0)
                broker_target_totals[key][broker_name]["sell"] += max(sell_amount, 0.0)
            else:
                broker_target_totals[key][broker_name]["buy"] += max(net_buy_amount, 0.0)
                broker_target_totals[key][broker_name]["sell"] += max(net_sell_amount, 0.0)

        consensus = defaultdict(lambda: {"buy": defaultdict(float), "sell": defaultdict(float)})
        for key, broker_map in broker_target_totals.items():
            for broker_name, amounts in broker_map.items():
                buy_total = safe_float(amounts.get("buy"), 0)
                sell_total = safe_float(amounts.get("sell"), 0)
                net_amount = buy_total - sell_total
                if net_amount > 0:
                    consensus[key]["buy"][broker_name] = net_amount
                elif net_amount < 0:
                    consensus[key]["sell"][broker_name] = abs(net_amount)

        return consensus

    def broker_10d_consensus_signal(consensus_map, key: str, direction: str) -> str:
        info = consensus_map.get(str(key or "").strip())
        if not info:
            return "-"

        buy_amount_by_broker = info.get("buy", {}) or {}
        sell_amount_by_broker = info.get("sell", {}) or {}
        buy_count = len(buy_amount_by_broker)
        sell_count = len(sell_amount_by_broker)
        buy_total = sum(safe_float(v, 0) for v in buy_amount_by_broker.values())
        sell_total = sum(safe_float(v, 0) for v in sell_amount_by_broker.values())
        direction = str(direction or "").strip()

        if direction == "買超":
            same_count = buy_count
            opposite_count = sell_count
            same_amount = buy_total
            opposite_amount = sell_total
            same_word = "買"
        elif direction == "賣超":
            same_count = sell_count
            opposite_count = buy_count
            same_amount = sell_total
            opposite_amount = buy_total
            same_word = "賣"
        else:
            return "-"

        if same_count <= 0 or same_amount <= 0:
            return "-"

        opposite_ratio = opposite_amount / same_amount if same_amount > 0 else 0.0

        # 逆向：反方向金額已經大於同方向，且反方向分點數也不輸同方向，才視為真正逆向。
        if (
            opposite_ratio >= BROKER_10D_CONSENSUS_REVERSE_AMOUNT_RATIO
            and opposite_count >= same_count
        ):
            return "逆向"

        # 分歧：同向、反向都至少有一定分點數，而且反方向金額至少達同方向的一半，才視為分歧。
        # 這樣可以避免同一標的只要有人買、有人賣，就被過度判成分歧。
        if (
            same_count >= BROKER_10D_CONSENSUS_MIN_SAME_BROKERS
            and opposite_count >= BROKER_10D_CONSENSUS_OPPOSITE_WARNING_BROKERS
            and opposite_ratio >= BROKER_10D_CONSENSUS_DIVERGENCE_AMOUNT_RATIO
        ):
            return "分歧"

        if same_count >= BROKER_10D_CONSENSUS_MIN_SAME_BROKERS:
            return f"{same_count}點同{same_word}"
        return f"單點{same_word}"

    consensus_map = build_broker_10d_consensus_map(consensus_source_df)

    if broker:
        broker_series = df.apply(lambda r: strip_gsheet_text_prefix(_pick_first_existing_value(r, ["分點", "分點名稱"])).strip(), axis=1)
        df = df[broker_series == broker].copy()

    if df.empty:
        empty_meta["chosen_date"] = chosen_date
        return [], empty_meta

    try:
        selected_run_ids = sorted({
            strip_gsheet_text_prefix(x).strip()
            for x in df.get("run_id", [])
            if strip_gsheet_text_prefix(x).strip()
        }, reverse=True)
        print(
            f"  ✅ 近10日分點明細實際採用 run_id：{selected_run_ids[0] if selected_run_ids else '-'}｜"
            f"分點：{broker or '-'}｜筆數：{len(df):,}"
        )
    except Exception:
        pass

    first_row = df.iloc[0].to_dict()
    period_text = strip_gsheet_text_prefix(_pick_first_existing_value(first_row, ["統計期間"]))
    if not period_text:
        first_dates = [pick_date(r) for _, r in df.iterrows() if pick_date(r)]
        if first_dates:
            start_date = min(first_dates)
            end_date = max(first_dates)
            period_text = f"{start_date:%Y/%m/%d} ～ {end_date:%Y/%m/%d}"
        else:
            period_text = "無資料"

    rows = []
    buy_total = 0.0
    sell_total = 0.0
    weighted_buy_ret = 0.0
    weighted_buy_amt = 0.0
    weighted_sell_ret = 0.0
    weighted_sell_amt = 0.0

    for _, r in df.iterrows():
        row = r.to_dict()
        broker_name = strip_gsheet_text_prefix(_pick_first_existing_value(row, ["分點", "分點名稱"])).strip() or broker
        underlying, underlying_name, target_label = resolve_target_identity(
            row,
            code_cols=["標的股", "標的代號", "標的"],
            name_cols=["標的名稱", "股票名稱"],
            raw_target_cols=["標的", "標的名稱", "股票名稱"],
            warrant_text_cols=["權證清單"],
        )

        buy_qty = safe_float(_pick_first_existing_value(row, ["近10日買進股數", "買進股數"]), 0)
        sell_qty = safe_float(_pick_first_existing_value(row, ["近10日賣出股數", "賣出股數"]), 0)
        buy_amount = safe_float(_pick_first_existing_value(row, ["近10日買進金額", "買進金額"]), 0)
        sell_amount = safe_float(_pick_first_existing_value(row, ["近10日賣出金額", "賣出金額"]), 0)
        net_buy_amount = safe_float(_pick_first_existing_value(row, ["近10日淨買超金額", "淨買超金額"]), buy_amount - sell_amount)
        net_sell_amount = safe_float(_pick_first_existing_value(row, ["近10日淨賣超金額", "淨賣超金額"]), sell_amount - buy_amount)
        direction = strip_gsheet_text_prefix(_pick_first_existing_value(row, ["買賣方向", "方向"]))
        if not direction:
            if buy_amount > sell_amount:
                direction = "買超"
            elif sell_amount > buy_amount:
                direction = "賣超"
            else:
                direction = "持平"

        buy_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
            "買超平均報酬%", "買超平均報酬率", "買超報酬%", "買超報酬率",
            "買超平均損益%", "買超平均損益率", "買超損益%", "買超損益率",
            "買進平均報酬%", "買進平均報酬率", "買進報酬%", "買進報酬率",
        ]))
        sell_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
            "賣超平均報酬%", "賣超平均報酬率", "賣超報酬%", "賣超報酬率",
            "賣超平均損益%", "賣超平均損益率", "賣超損益%", "賣超損益率",
            "賣出平均報酬%", "賣出平均報酬率", "賣出報酬%", "賣出報酬率",
        ]))
        generic_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
            "平均報酬%", "平均報酬率", "報酬率", "報酬率%", "primary_return", "主要報酬率",
        ]))
        if buy_ret is None and direction == "買超":
            buy_ret = generic_ret
        if sell_ret is None and direction == "賣超":
            sell_ret = generic_ret
        warrant_count = safe_int(_pick_first_existing_value(row, ["涉及權證檔數", "權證檔數"]), 0)
        if warrant_count <= 0:
            warrant_count = count_warrants_in_text(_pick_first_existing_value(row, ["權證清單"]))

        primary_ret = buy_ret if direction == "買超" else sell_ret if direction == "賣超" else None
        outcome = "-"
        if primary_ret is not None:
            outcome = "勝" if primary_ret > 0 else "敗"

        buy_total += buy_amount
        sell_total += sell_amount
        if buy_ret is not None and buy_amount > 0:
            weighted_buy_ret += buy_ret * buy_amount
            weighted_buy_amt += buy_amount
        if sell_ret is not None and sell_amount > 0:
            weighted_sell_ret += sell_ret * sell_amount
            weighted_sell_amt += sell_amount

        consensus_key = str(underlying or target_label or "").strip()
        consensus_signal = broker_10d_consensus_signal(consensus_map, consensus_key, direction)
        underlying_10d_return = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
            "現股10日報酬率%", "標的10日報酬率%",
            "現股10日報酬率", "標的10日報酬率",
            "標的10日漲跌幅%", "現股10日漲跌幅%",
            "標的10日漲跌幅", "現股10日漲跌幅",
        ]))

        rows.append({
            "broker": broker_name,
            "underlying": underlying,
            "underlying_name": underlying_name,
            "target": target_label,
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "net_buy_amount": net_buy_amount,
            "net_sell_amount": net_sell_amount,
            "direction": direction,
            "buy_return": buy_ret,
            "sell_return": sell_ret,
            "primary_return": primary_ret,
            "outcome": outcome,
            "warrant_count": warrant_count,
            "underlying_10d_return": underlying_10d_return,
            "consensus_signal": consensus_signal,
            "warrant_list": strip_gsheet_text_prefix(_pick_first_existing_value(row, ["權證清單"])),
        })

    rows = merge_broker_10d_rows_by_underlying(rows)

    # 合併同分點同標的資料後，再用最終淨方向重算一次共識訊號。
    # 避免合併前同一標的多列資料的方向不同，導致圖卡顯示的共識訊號與最終列方向不一致。
    for r in rows:
        consensus_key = str(r.get("underlying") or r.get("target") or "").strip()
        r["consensus_signal"] = broker_10d_consensus_signal(consensus_map, consensus_key, str(r.get("direction", "")).strip())

    buy_total = sum(safe_float(r.get("buy_amount"), 0) for r in rows)
    sell_total = sum(safe_float(r.get("sell_amount"), 0) for r in rows)

    weighted_buy_ret = 0.0
    weighted_buy_amt = 0.0
    weighted_sell_ret = 0.0
    weighted_sell_amt = 0.0
    for r in rows:
        buy_ret = normalize_return_pct(r.get("buy_return"))
        sell_ret = normalize_return_pct(r.get("sell_return"))
        buy_amount = safe_float(r.get("buy_amount"), 0)
        sell_amount = safe_float(r.get("sell_amount"), 0)
        if buy_ret is not None and buy_amount > 0:
            weighted_buy_ret += buy_ret * buy_amount
            weighted_buy_amt += buy_amount
        if sell_ret is not None and sell_amount > 0:
            weighted_sell_ret += sell_ret * sell_amount
            weighted_sell_amt += sell_amount

    win_count = sum(1 for r in rows if r.get("primary_return") is not None and safe_float(r.get("primary_return"), 0) > 0)
    loss_count = sum(1 for r in rows if r.get("primary_return") is not None and safe_float(r.get("primary_return"), 0) <= 0)
    valid_count = win_count + loss_count

    win_rate = normalize_return_pct(_pick_first_existing_value(first_row, ["分點近10日勝率", "近10日勝率", "勝率"]))
    if win_rate is None and valid_count > 0:
        win_rate = round(win_count / valid_count * 100, 2)

    first_win_count = safe_int(_pick_first_existing_value(first_row, ["分點近10日勝筆數", "近10日勝筆數", "勝筆數"]), 0)
    first_loss_count = safe_int(_pick_first_existing_value(first_row, ["分點近10日敗筆數", "近10日敗筆數", "敗筆數"]), 0)
    if first_win_count > 0 or first_loss_count > 0:
        win_count, loss_count = first_win_count, first_loss_count
        valid_count = win_count + loss_count
        if win_rate is None and valid_count > 0:
            win_rate = round(win_count / valid_count * 100, 2)

    avg_buy_return = round(weighted_buy_ret / weighted_buy_amt, 2) if weighted_buy_amt > 0 else None
    avg_sell_return = round(weighted_sell_ret / weighted_sell_amt, 2) if weighted_sell_amt > 0 else None

    def pick_broker_metric(candidates: list[str]):
        """
        從近10日分點明細快取中讀取「分點層級」統計值。

        主程式會把同一分點的勝率、加權報酬、加權期望值等欄位重複寫在每一列標的資料中。
        圖卡端優先讀第一列；若第一列剛好是空值，再往同一批資料其他列找第一個有效值。
        """
        raw = _pick_first_existing_value_fuzzy(first_row, candidates)
        if strip_gsheet_text_prefix(raw) not in ("", "-"):
            return raw

        for _, rr in df.iterrows():
            raw = _pick_first_existing_value_fuzzy(rr.to_dict(), candidates)
            if strip_gsheet_text_prefix(raw) not in ("", "-"):
                return raw

        return ""

    weighted_avg_return = normalize_return_pct(pick_broker_metric(["分點近10日加權平均報酬%", "分點近10日加權平均報酬率", "分點加權平均報酬%", "加權平均報酬%"]))
    weighted_buy_return = normalize_return_pct(pick_broker_metric(["分點近10日買超加權平均報酬%", "分點近10日買超加權平均報酬率", "買超加權平均報酬%", "買超加權報酬%"]))
    weighted_sell_return = normalize_return_pct(pick_broker_metric(["分點近10日賣超加權平均報酬%", "分點近10日賣超加權平均報酬率", "賣超加權平均報酬%", "賣超加權報酬%"]))
    weighted_win_return = normalize_return_pct(pick_broker_metric(["分點近10日加權平均勝報酬%", "分點近10日加權平均勝報酬率", "加權平均勝報酬%", "加權勝報酬%"]))
    weighted_loss_return = normalize_return_pct(pick_broker_metric(["分點近10日加權平均敗報酬%", "分點近10日加權平均敗報酬率", "加權平均敗報酬%", "加權敗報酬%"]))
    weighted_expectancy = normalize_return_pct(pick_broker_metric(["分點近10日加權期望值%", "分點近10日加權期望值", "加權期望值%", "期望值%"]))
    profit_loss_ratio_raw = pick_broker_metric(["分點近10日盈虧比", "近10日盈虧比", "盈虧比"])
    profit_loss_ratio = None if strip_gsheet_text_prefix(profit_loss_ratio_raw) in ("", "-") else safe_float(profit_loss_ratio_raw, None)
    weighted_return_amount = safe_float(pick_broker_metric(["分點近10日加權報酬權重金額", "加權報酬權重金額", "報酬權重金額"]), 0)

    # 舊版主程式尚未產生加權快取欄位時，維持原本圖片端依明細金額加權的備援邏輯。
    if weighted_buy_return is None:
        weighted_buy_return = avg_buy_return
    if weighted_sell_return is None:
        weighted_sell_return = avg_sell_return
    if weighted_avg_return is None:
        primary_weighted_sum = 0.0
        primary_weighted_amt = 0.0
        for r in rows:
            primary_ret = normalize_return_pct(r.get("primary_return"))
            direction = str(r.get("direction", "")).strip()
            if direction == "買超":
                amt = safe_float(r.get("net_buy_amount"), 0)
            elif direction == "賣超":
                amt = safe_float(r.get("net_sell_amount"), 0)
            else:
                amt = max(safe_float(r.get("buy_amount"), 0), safe_float(r.get("sell_amount"), 0))
            if primary_ret is None or amt <= 0:
                continue
            primary_weighted_sum += primary_ret * amt
            primary_weighted_amt += amt
        weighted_avg_return = round(primary_weighted_sum / primary_weighted_amt, 2) if primary_weighted_amt > 0 else None

    meta = {
        "broker": broker or strip_gsheet_text_prefix(_pick_first_existing_value(first_row, ["分點", "分點名稱"])),
        "period_text": period_text,
        "chosen_date": chosen_date,
        "win_rate": win_rate,
        "win_count": win_count,
        "loss_count": loss_count,
        "buy_total": buy_total,
        "sell_total": sell_total,
        "net_total": buy_total - sell_total,
        "avg_buy_return": avg_buy_return,
        "avg_sell_return": avg_sell_return,
        "weighted_avg_return": weighted_avg_return,
        "weighted_buy_return": weighted_buy_return,
        "weighted_sell_return": weighted_sell_return,
        "weighted_win_return": weighted_win_return,
        "weighted_loss_return": weighted_loss_return,
        "profit_loss_ratio": profit_loss_ratio,
        "weighted_expectancy": weighted_expectancy,
        "weighted_return_amount": weighted_return_amount,
    }
    return rows, meta


def draw_broker_10d_detail_image(target: date, broker: str, output_path: Path):
    rows, meta = read_broker_10d_detail_rows_from_gsheet(target, broker)
    if not rows:
        raise RuntimeError(f"{broker} 在 {SHEET_BROKER_10D_DETAIL} 找不到可用資料。")

    buy_rows = [r for r in rows if str(r.get("direction", "")).strip() == "買超" and safe_float(r.get("net_buy_amount"), 0) > 0]
    sell_rows = [r for r in rows if str(r.get("direction", "")).strip() == "賣超" and safe_float(r.get("net_sell_amount"), 0) > 0]
    buy_rows.sort(key=lambda r: safe_float(r.get("net_buy_amount"), 0), reverse=True)
    sell_rows.sort(key=lambda r: safe_float(r.get("net_sell_amount"), 0), reverse=True)
    buy_rows = buy_rows[:10]
    sell_rows = sell_rows[:10]

    display_buy_total = sum(safe_float(r.get("net_buy_amount"), 0) for r in buy_rows)
    display_sell_total = sum(safe_float(r.get("net_sell_amount"), 0) for r in sell_rows)
    display_net_total = display_buy_total - display_sell_total

    def weighted_avg_return(display_rows, amount_key, return_key):
        weighted_sum = 0.0
        weighted_amt = 0.0
        for r in display_rows:
            amt = safe_float(r.get(amount_key), 0)
            ret = normalize_return_pct(r.get(return_key))
            if ret is None or amt <= 0:
                continue
            weighted_sum += ret * amt
            weighted_amt += amt
        return round(weighted_sum / weighted_amt, 2) if weighted_amt > 0 else None

    # 圖卡摘要優先使用主程式寫入「快取_近10日分點買賣明細」的分點層級加權報酬。
    # 若舊版快取尚未有這些欄位，才退回圖片端依 TOP10 金額自行加權估算。
    buy_avg_return = normalize_return_pct(meta.get("weighted_buy_return"))
    if buy_avg_return is None:
        buy_avg_return = weighted_avg_return(buy_rows, "net_buy_amount", "buy_return")

    sell_avg_return = normalize_return_pct(meta.get("weighted_sell_return"))
    if sell_avg_return is None:
        sell_avg_return = weighted_avg_return(sell_rows, "net_sell_amount", "sell_return")

    overall_weighted_return = normalize_return_pct(meta.get("weighted_avg_return"))
    weighted_expectancy = normalize_return_pct(meta.get("weighted_expectancy"))

    shown_rows = buy_rows + sell_rows
    display_win_count = sum(1 for r in shown_rows if normalize_return_pct(r.get("primary_return")) is not None and safe_float(r.get("primary_return"), 0) > 0)
    display_loss_count = sum(1 for r in shown_rows if normalize_return_pct(r.get("primary_return")) is not None and safe_float(r.get("primary_return"), 0) <= 0)
    display_valid_count = display_win_count + display_loss_count
    display_win_rate = round(display_win_count / display_valid_count * 100, 2) if display_valid_count > 0 else None

    # 近10日圖片的重點不是只縮欄位，而是整個輸出畫布要跟表格寬度貼近。
    # 否則即使表格本身不寬，只要 fig_w 太大，左右留白仍會非常巨大。
    # 這裡改成先決定表格寬度，再反推整張圖寬度，讓表格約佔整體寬度 90% 左右。
    broker10d_table_col_w = [0.56, 2.50, 1.55, 1.25, 1.45]
    broker10d_table_w = sum(broker10d_table_col_w)

    outer_pad_x = 0.32
    fig_w = broker10d_table_w + outer_pad_x * 2
    margin_x = outer_pad_x
    content_w = fig_w - 2 * margin_x
    broker10d_table_left = outer_pad_x

    top_h = 1.35
    summary_card_h = 0.88
    summary_gap = 0.14
    section_gap = 0.24
    table_title_h = 0.58
    header_h = 0.44
    row_h = 0.44
    footer_h = 0.42

    summary_rows = 2
    max_rows = max(len(buy_rows), len(sell_rows), 1)
    section_table_h = table_title_h + header_h + max_rows * row_h
    fig_h = top_h + summary_rows * summary_card_h + (summary_rows - 1) * summary_gap + section_gap + section_table_h * 2 + section_gap + footer_h + 0.42
    fig_h = max(fig_h, 12.8)

    BG = "#F6F8FB"
    WHITE = "#FFFFFF"
    NAVY = "#061D3D"
    NAVY2 = "#0B2E5B"
    RED = "#D92323"
    GREEN = "#16803C"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#C9D5E3"
    ROW_ALT = "#FAFCFF"
    HEADER_BG = "#F3F7FC"
    ORANGE = "#C76900"

    font_path = get_font_path(False)
    bold_path = get_font_path(True)
    FONT = font_manager.FontProperties(fname=font_path)
    BOLD = font_manager.FontProperties(fname=bold_path)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=BG)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def rounded(x, y, w, h, fc=WHITE, ec=BORDER, lw=1.1, r=0.10, z=1):
        patch = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc, zorder=z
        )
        ax.add_patch(patch)
        return patch

    def rect(x, y, w, h, fc=WHITE, ec=None, lw=0.8, z=1):
        patch = patches.Rectangle((x, y), w, h, linewidth=lw if ec else 0,
                                  edgecolor=ec, facecolor=fc, zorder=z)
        ax.add_patch(patch)
        return patch

    def text_draw(x, y, s, size=12, color=TEXT, fp=None, ha="left", va="center", z=5):
        ax.text(x, y, str(s), fontsize=size, color=color, fontproperties=fp or FONT,
                ha=ha, va=va, zorder=z)

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def measure_text_width(s, size=13.0, fp=None):
        ghost = ax.text(0, 0, str(s), fontsize=size, fontproperties=fp or FONT, alpha=0)
        bb = ghost.get_window_extent(renderer=renderer)
        ghost.remove()
        x0_disp, y0_disp = ax.transData.transform((0, 0))
        x1_disp = x0_disp + bb.width
        x0_data = ax.transData.inverted().transform((x0_disp, y0_disp))[0]
        x1_data = ax.transData.inverted().transform((x1_disp, y0_disp))[0]
        return x1_data - x0_data

    def fit_to_cell_width(s, cell_w, size=13.0, fp=None):
        s = str(s or "")
        if not s:
            return ""
        max_w = max(float(cell_w), 0.1)
        if measure_text_width(s, size=size, fp=fp) <= max_w:
            return s
        ellipsis = "…"
        ell_w = measure_text_width(ellipsis, size=size, fp=fp)
        if ell_w >= max_w:
            return ellipsis
        low, high = 0, len(s)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = s[:mid] + ellipsis
            if measure_text_width(candidate, size=size, fp=fp) <= max_w:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best or ellipsis

    def draw_clipped_text(x_left, y_center, cell_w, text_value, size=13.0, color=TEXT, fp=None, padding_left=0.12, padding_right=0.12, clip_h=None):
        display_text = fit_to_cell_width(text_value, cell_w - padding_left - padding_right, size=size, fp=fp)
        h = clip_h if clip_h is not None else row_h
        clip_rect = patches.Rectangle((x_left, y_center - h / 2 + 0.02), cell_w, h - 0.04, linewidth=0, facecolor="none")
        ax.add_patch(clip_rect)
        t = ax.text(
            x_left + padding_left, y_center, display_text,
            fontsize=size, color=color, fontproperties=fp or FONT,
            ha="left", va="center", zorder=5, clip_on=True
        )
        t.set_clip_path(clip_rect)

    def fmt_pct_plain(v):
        pct = normalize_return_pct(v)
        if pct is None:
            return "-"
        if pct < -100.0:
            pct = -100.0
        return f"{pct:.2f}%"

    def return_color(v):
        pct = normalize_return_pct(v)
        if pct is None:
            return TEXT
        if pct > 0:
            return RED
        if pct < 0:
            return GREEN
        return TEXT

    def fmt_amount_wan(v):
        return f"{safe_float(v, 0) / 10000:,.1f}萬"

    try:
        ax.text(
            0.5, 0.50, CENTER_WATERMARK_TEXT,
            transform=ax.transAxes,
            ha="center", va="center",
            fontsize=CENTER_WATERMARK_FONT_SIZE,
            fontproperties=BOLD,
            color="#2C3440",
            alpha=CENTER_WATERMARK_ALPHA,
            rotation=CENTER_WATERMARK_ROTATION,
            linespacing=1.18,
            zorder=50,
        )
    except Exception:
        pass

    cache_date = meta.get("chosen_date")
    cache_date_text = cache_date.strftime("%Y/%m/%d") if cache_date else target.strftime("%Y/%m/%d")
    period_text = meta.get("period_text") or "無資料"

    y = fig_h - 0.44
    text_draw(margin_x + 0.15, y, f"{broker}｜近10日分點買賣明細", 28, NAVY, BOLD)
    y -= 0.42
    text_draw(
        margin_x + 0.18,
        y,
        f"統計期間：{period_text}｜單位：萬元",
        11.2,
        TEXT,
        BOLD,
    )

    card_gap_x = 0.16
    summary_left = broker10d_table_left
    summary_w = broker10d_table_w
    card_w = (summary_w - card_gap_x * 2) / 3
    card_y1 = y - 0.28 - summary_card_h
    summary_cards1 = [
        ("買超TOP10合計", fmt_amount_wan(display_buy_total), RED, None),
        ("賣超TOP10合計", fmt_amount_wan(display_sell_total), GREEN, None),
        ("淨額(買超-賣超)", fmt_amount_wan(display_net_total), NAVY2, None),
    ]
    for i, (label, value, color, extra) in enumerate(summary_cards1):
        x = summary_left + i * (card_w + card_gap_x)
        rounded(x, card_y1, card_w, summary_card_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
        text_draw(x + 0.18, card_y1 + summary_card_h - 0.18, label, 12.5, MUTED, BOLD, va="top")
        text_draw(x + 0.18, card_y1 + 0.20, value, 17.2, color, BOLD, va="bottom")
        if extra:
            text_draw(x + card_w - 0.18, card_y1 + 0.18, extra, 10.8, TEXT, FONT, ha="right", va="bottom")

    card_y2 = card_y1 - summary_gap - summary_card_h
    win_rate_for_card = display_win_rate
    if win_rate_for_card is None:
        win_rate_for_card = normalize_return_pct(meta.get("win_rate"))
    win_count_for_card = display_win_count
    loss_count_for_card = display_loss_count

    summary_cards2 = [
        ("近10日加權報酬", fmt_pct_plain(overall_weighted_return), return_color(overall_weighted_return), None),
        ("前10勝率", fmt_pct_plain(win_rate_for_card), ORANGE, None),
        ("勝敗筆數", f"勝 {win_count_for_card} / 敗 {loss_count_for_card}", NAVY2, None),
    ]
    for i, (label, value, color, extra) in enumerate(summary_cards2):
        x = summary_left + i * (card_w + card_gap_x)
        rounded(x, card_y2, card_w, summary_card_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
        text_draw(x + 0.18, card_y2 + summary_card_h - 0.18, label, 12.5, MUTED, BOLD, va="top")
        text_draw(x + 0.18, card_y2 + 0.20, value, 17.2, color, BOLD, va="bottom")
        if extra:
            text_draw(x + card_w - 0.18, card_y2 + 0.18, extra, 10.8, TEXT, FONT, ha="right", va="bottom")

    y = card_y2 - section_gap

    def draw_section(title, section_rows, y_top, title_bg, section_type):
        headers = ["排名", "標的", "10日淨額", "現股10日漲跌", "權證報酬"]
        col_w = broker10d_table_col_w
        table_w = broker10d_table_w
        left = broker10d_table_left
        sec_rows = max(len(section_rows), 1)
        sec_h = table_title_h + header_h + sec_rows * row_h

        rounded(left, y_top - sec_h, table_w, sec_h, fc=WHITE, ec=NAVY, lw=1.2, r=0.08)
        rect(left, y_top - table_title_h, table_w, table_title_h, fc=title_bg)
        text_draw(left + 0.24, y_top - table_title_h / 2, title, 17.2, WHITE, BOLD)

        header_y_top = y_top - table_title_h
        rect(left, header_y_top - header_h, table_w, header_h, fc=HEADER_BG, ec=BORDER, lw=0.6)
        x = left
        for h, w in zip(headers, col_w):
            text_draw(x + w / 2, header_y_top - header_h / 2, h, 11.0, NAVY, BOLD, ha="center")
            ax.plot([x, x], [y_top - sec_h, header_y_top], color=BORDER, linewidth=0.6)
            x += w
        ax.plot([left + table_w, left + table_w], [y_top - sec_h, header_y_top], color=BORDER, linewidth=0.6)

        data_y = header_y_top - header_h
        if not section_rows:
            rect(left, data_y - row_h, table_w, row_h, fc=WHITE, ec=BORDER, lw=0.6)
            text_draw(left + table_w / 2, data_y - row_h / 2, "目前沒有可顯示資料", 12.8, MUTED, BOLD, ha="center")
        else:
            for i, r in enumerate(section_rows, start=1):
                ry = data_y - i * row_h
                rect(left, ry, table_w, row_h, fc=WHITE if i % 2 == 1 else ROW_ALT, ec=BORDER, lw=0.5)
                if section_type == "buy":
                    net_value = safe_float(r.get("net_buy_amount"), 0)
                    net_color = RED
                else:
                    net_value = safe_float(r.get("net_sell_amount"), 0)
                    net_color = GREEN
                ret_val = r.get("buy_return") if section_type == "buy" else r.get("sell_return")
                if ret_val is None:
                    ret_val = r.get("primary_return")
                ret_color = return_color(ret_val)
                underlying_ret_val = r.get("underlying_10d_return")
                underlying_ret_color = return_color(underlying_ret_val)
                values = [
                    str(i),
                    r.get("target", ""),
                    fmt_amount_wan(net_value),
                    fmt_pct_plain(underlying_ret_val),
                    fmt_pct_plain(ret_val),
                ]
                colors = [TEXT, TEXT, net_color, underlying_ret_color, ret_color]
                aligns = ["center", "left", "right", "right", "right"]
                bolds = [True, True, True, True, True]
                x = left
                for val, w, c, a, is_bold in zip(values, col_w, colors, aligns, bolds):
                    fp = BOLD if is_bold else FONT
                    if a == "left":
                        draw_clipped_text(x, ry + row_h / 2, w, val, size=11.8, color=c, fp=fp)
                    else:
                        display_val = fit_to_cell_width(val, max(0.2, w - 0.20), size=11.8, fp=fp)
                        px = x + (w / 2 if a == "center" else w - 0.10)
                        text_draw(px, ry + row_h / 2, display_val, 11.8, c, fp, ha=a)
                    x += w
        return y_top - sec_h - section_gap

    y = draw_section("近10日買超 TOP10", buy_rows, y, NAVY, "buy")
    y = draw_section("近10日賣超 TOP10", sell_rows, y, NAVY, "sell")

    text_draw(fig_w / 2, 0.18, "本圖資料僅供籌碼觀察參考，不構成投資建議；交易請自行評估風險。", 10.8, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)



# ══════════════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════════════

def send_to_discord(webhook_url: str, image_path: Path, target: date):
    """
    只上傳圖片到 Discord，不另外傳送文字內容。
    """
    with image_path.open("rb") as f:
        files = {"file": (image_path.name, f, "image/png")}
        resp = requests.post(webhook_url, files=files, timeout=60)

    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Discord webhook 發送失敗：{resp.status_code} {resp.text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=os.getenv("TARGET_DATE", ""))
    parser.add_argument("--output", default=os.getenv("OUTPUT_IMAGE", "output/精選分點買賣超追蹤.png"))
    parser.add_argument("--consensus-output", default=os.getenv("CONSENSUS_OUTPUT_IMAGE", ""))
    parser.add_argument("--weekly-output", default=os.getenv("WEEKLY_WARRANT_CONSENSUS_OUTPUT_IMAGE", ""))
    parser.add_argument("--weekly14-output", default=os.getenv("WARRANT_CONSENSUS_14D_OUTPUT_IMAGE", ""))
    parser.add_argument("--weekly21-output", default=os.getenv("WARRANT_CONSENSUS_21D_OUTPUT_IMAGE", ""))
    parser.add_argument("--win-rate-output", default=os.getenv("WIN_RATE_STATS_OUTPUT_IMAGE", ""))
    parser.add_argument("--broker10d-output-dir", default=os.getenv("BROKER_10D_OUTPUT_DIR", ""))
    parser.add_argument(
        "--broker",
        "--broker-name",
        dest="broker",
        default=os.getenv("BROKER_NAME", os.getenv("BROKER_10D_BROKER", "")),
        help="指定近10日分點買賣明細圖的分點名稱，例如：元大南屯。可由 Discord/GitHub Actions 的 broker_name 傳入。",
    )
    parser.add_argument(
        "--action",
        default=os.getenv("IMAGE_ACTION", os.getenv("ACTION", os.getenv("RUN_PLAN", ""))),
        help=(
            "圖片產生選項：精選五分點每日圖 / 近一個月共識淨買超TOP15 / "
            "本週權證共識買賣超TOP15（輸出近7／14／21日三張圖） / 近10日分點買賣明細圖 / "
            "所有分點勝率統計圖 / 全部圖片。也支援 GitHub Actions 的 RUN_PLAN。"
        ),
    )
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    action = normalize_image_action(args.action)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = infer_latest_date_from_gsheet()

    output_path = Path(args.output)
    consensus_output_path = Path(args.consensus_output) if args.consensus_output else output_path.parent / "近40個交易日_五大分點共識淨買超成本TOP15.png"
    weekly_output_path = Path(args.weekly_output) if args.weekly_output else output_path.parent / "本週權證分點共識買賣超TOP15.png"
    weekly14_output_path = Path(args.weekly14_output) if args.weekly14_output else weekly_output_path.parent / "近14日權證分點共識買賣超TOP15.png"
    weekly21_output_path = Path(args.weekly21_output) if args.weekly21_output else weekly_output_path.parent / "近21日權證分點共識買賣超TOP15.png"
    win_rate_output_path = Path(args.win_rate_output) if args.win_rate_output else output_path.parent / "所有分點勝率統計.png"
    broker10d_output_dir = Path(args.broker10d_output_dir) if args.broker10d_output_dir else output_path.parent

    image_paths: list[Path] = []
    broker_10d_image_brokers = resolve_broker_10d_image_brokers(args.broker)

    print(
        f"Google Sheet：{GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME}\n"
        f"目標日期：{target:%Y-%m-%d}\n"
        f"Action選項：{args.action or '(預設)'}\n"
        f"實際執行：{action}\n"
        f"買超門檻：{BUY_THRESHOLD:.0f}，賣方門檻：{SELL_THRESHOLD:.0f}\n"
        f"加碼次數計算範圍：近 {ADD_COUNT_LOOKBACK_TRADING_DAYS} 個有效交易日\n"
        f"近10日分點圖指定分點：{'、'.join(broker_10d_image_brokers) if broker_10d_image_brokers else '-'}"
    )


    if action in [IMAGE_ACTION_DAILY_BUNDLE, IMAGE_ACTION_ALL]:
        history = read_selected5_history_from_abcd()
        buys, sells = extract_actions_from_gsheet(target)

        print(
            f"買超原始筆數：{len(buys)}，賣方提醒原始筆數：{len(sells)}\n"
            f"輸出圖檔：{output_path}"
        )

        draw_report_image(target, buys, sells, history, output_path)
        image_paths.append(output_path)
    

    elif action == IMAGE_ACTION_CONSENSUS_BUY:
        print(f"輸出圖檔：{consensus_output_path}")
        draw_consensus_buy_image(target, consensus_output_path, LOOKBACK_TRADING_DAYS)
        image_paths.append(consensus_output_path)

    if action in [IMAGE_ACTION_WEEKLY_WARRANT, IMAGE_ACTION_ALL]:
        warrant_consensus_outputs = [
            (7, weekly_output_path),
            (14, weekly14_output_path),
            (21, weekly21_output_path),
        ]

        for window_days, image_path in warrant_consensus_outputs:
            print(f"輸出近{window_days}日權證共識圖：{image_path}")
            draw_weekly_warrant_consensus_image(
                target,
                image_path,
                window_days=window_days,
            )
            image_paths.append(image_path)

    if action in [IMAGE_ACTION_WIN_RATE_STATS, IMAGE_ACTION_ALL]:
        print(f"輸出圖檔：{win_rate_output_path}")
        draw_all_broker_win_rate_stats_image(target, win_rate_output_path)
        image_paths.append(win_rate_output_path)

    if action == IMAGE_ACTION_BROKER_10D:
        if not broker_10d_image_brokers:
            raise RuntimeError("近10日分點買賣明細圖沒有指定任何分點。")

        print(f"輸出資料來源：{SHEET_BROKER_10D_DETAIL}｜指定分點：{'、'.join(broker_10d_image_brokers)}")
        for broker in broker_10d_image_brokers:
            broker_path = broker10d_output_dir / f"近10日分點買賣明細_{broker}.png"
            print(f"輸出圖檔：{broker_path}")
            draw_broker_10d_detail_image(target, broker, broker_path)
            image_paths.append(broker_path)

    if not image_paths:
        raise RuntimeError(f"無法辨識或沒有產生任何圖片：{args.action}")

    if args.no_discord:
        print("已設定 --no-discord，只輸出圖片，不發送 Discord。")
        return

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "")
    if not webhook_url:
        raise RuntimeError("找不到 DISCORD_WEBHOOK_URL_TEST，請先在 GitHub Secrets 設定。")

    for image_path in image_paths:
        send_to_discord(webhook_url, image_path, target)

    print(f"Discord 已發送 {len(image_paths)} 張圖片。")

if __name__ == "__main__":
    main()
