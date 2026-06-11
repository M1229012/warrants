#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日精選分點買賣超追蹤圖卡｜Google Sheet 讀取版

用途：
- 直接讀取 Google Sheet「權證分點籌碼」內的 A/B/C/D 與勝率統計工作表
- 不需要本機 Excel
- 產生一頁式 PNG
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
    "永豐金竹北",
    "永豐金內湖",
    "富邦敦南",
]

# 近10日分點買賣明細圖，只輸出元大南屯。
BROKER_10D_IMAGE_BROKERS = [
    "元大南屯",
]

DATA_SCOPE_SELECTED5 = os.getenv("DATA_SCOPE_SELECTED5", "精選五分點")
DATA_SCOPE_ALL = os.getenv("DATA_SCOPE_ALL", "全分點")

BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "1000000"))
SELL_RATIO = float(os.getenv("SELL_THRESHOLD_RATIO", "0.2"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", str(BUY_THRESHOLD * SELL_RATIO)))
LOOKBACK_TRADING_DAYS = int(os.getenv("LOOKBACK_TRADING_DAYS", "22"))
# 專門給「第幾次加碼」使用，不影響原本近一個月共識買超圖。
ADD_COUNT_LOOKBACK_TRADING_DAYS = int(os.getenv("ADD_COUNT_LOOKBACK_TRADING_DAYS", "50"))

# 若你未來想讓「出清不管金額都顯示」，改成 "1"
DISPLAY_EXIT_ALWAYS = os.getenv("DISPLAY_EXIT_ALWAYS", "0") == "1"

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "權證分點籌碼")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

SHEET_A = "A_單檔大買"
SHEET_B = "B_同標的單日合計"
SHEET_C = "C_同標的3日累積"
SHEET_D = "D_近10日累積淨買進"
SHEET_STAT = "勝率統計"
SHEET_DAILY_SELL = os.getenv("SHEET_DAILY_SELL", "每日賣出明細")
SHEET_HISTORY = os.getenv("SHEET_HISTORY", "快取_分點歷史")
SHEET_TOP15_RETURN_CACHE = os.getenv("SHEET_TOP15_RETURN_CACHE", "快取_TOP15分點報酬率")
SHEET_TOP15_CONSENSUS_CACHE = os.getenv("SHEET_TOP15_CONSENSUS_CACHE", "快取_TOP15共識淨買超")
SHEET_TOP15_POSITION_DETAIL = os.getenv("SHEET_TOP15_POSITION_DETAIL", "快取_TOP15部位明細")
SHEET_WARRANT_CONSENSUS_7D = os.getenv("SHEET_WARRANT_CONSENSUS_7D", "快取_近7日權證分點共識TOP15")
SHEET_BROKER_10D_DETAIL = os.getenv("SHEET_BROKER_10D_DETAIL", "快取_近10日分點買賣明細")
# 近10日分點圖若 Google Sheet 同一天有多批 run_id，可用此環境變數指定要讀哪一批。
# 例如：BROKER_10D_FORCE_RUN_ID=20260611_061357
BROKER_10D_FORCE_RUN_ID = os.getenv("BROKER_10D_FORCE_RUN_ID", os.getenv("BROKER_10D_RUN_ID", "")).strip()


NTD_PER_WARRANT_POINT = float(os.getenv("NTD_PER_WARRANT_POINT", "1000"))
# 若某權證不在 A/B/C/D 白名單，但同一分點 + 同一標的於同一天賣出合計達此門檻，
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
CENTER_WATERMARK_FONT_SIZE = 108
CENTER_WATERMARK_ROTATION = 18

# 事件代號說明
EVENT_LEGEND_ITEMS = [
    ("A", "單檔權證單日大買"),
    ("B", "同標的單日合買"),
    ("C", "同標的3日累積"),
    ("D", "近10日累積淨買"),
]


# ══════════════════════════════════════════════════════════════════════
# Google Sheet 讀取
# ══════════════════════════════════════════════════════════════════════

_GSHEET = None


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
    sh = get_gsheet()
    ws = sh.worksheet(sheet_name)
    return ws.get_all_values()


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

        # Google Sheet 可能讀到 1,003,600、1，003，600、+30.36% 或 +30.36％
        s = (
            s.replace(",", "")
             .replace("，", "")
             .replace("%", "")
             .replace("％", "")
             .replace("﹪", "")
             .replace("＋", "+")
             .strip()
        )
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
    # Google Sheet 表頭有時會因手動換行 / 全形符號 / 多餘空白造成精確欄名對不到。
    # 這裡統一移除所有空白字元，並把全形百分比轉成半形百分比。
    s = re.sub(r"\s+", "", s)
    return (
        s.replace("％", "%")
         .replace("﹪", "%")
         .replace("＋", "+")
         .replace("（", "(")
         .replace("）", ")")
    )


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


def _ensure_exact_column_aliases(df, exact_columns: list[str]):
    """
    將 Google Sheet 可能帶有空白 / 換行 / 全形％ 的實際欄名，
    映射成程式內要用的「精確欄名」。

    例如若 Sheet 實際欄名是「賣超平均報酬％」或「賣超平均報酬%
」，
    這裡會複製成一個真正叫做「賣超平均報酬%」的欄位。
    之後其他邏輯就能用 row.get("賣超平均報酬%") 一字不變地抓值。
    """
    try:
        if df is None or df.empty:
            return df

        norm_to_real = {}
        for col in df.columns:
            norm_col = _normalize_gsheet_col_name(col)
            if norm_col and norm_col not in norm_to_real:
                norm_to_real[norm_col] = col

        for exact_col in exact_columns:
            norm_exact = _normalize_gsheet_col_name(exact_col)
            real_col = norm_to_real.get(norm_exact)
            if real_col and real_col != exact_col:
                # 無論 exact_col 是否存在，都以實際欄位內容覆蓋，確保後續精確抓值一定吃到最新正確資料。
                df[exact_col] = df[real_col]

        return df
    except Exception:
        return df


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


def _parse_top15_broker_json(value) -> list[tuple[str, float, float | None]]:
    """
    解析「快取_TOP15共識淨買超」的分點明細_JSON。

    回傳格式：
        [(分點, 淨買超成本, 報酬率), ...]
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

        out.append((broker, amount, return_pct))

    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _parse_top15_broker_text(value) -> list[tuple[str, float, float | None]]:
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

        if amount > 0:
            out.append((broker, amount, return_pct))

    out.sort(key=lambda x: x[1], reverse=True)
    return out


def read_top15_position_detail_cache_from_gsheet(target: date | None = None) -> dict[str, list[tuple[str, float, float | None]]]:
    """
    讀取新版「快取_TOP15部位明細」，彙總成：
        {標的股: [(分點, 剩餘成本, 報酬率), ...]}

    近一個月 TOP15 圖固定只讀 資料範圍=精選五分點。
    若新版工作表尚不存在，會 fallback 回舊版「快取_TOP15分點報酬率」。
    """
    needed_cols = [
        "資料範圍",
        "統計日期", "日期", "目標日期",
        "分點", "分點名稱", "券商代號",
        "標的股", "標的代號", "標的", "標的名稱", "股票名稱",
        "剩餘成本", "目前剩餘成本", "淨買超成本", "remaining_cost",
        "目前市值", "未實現損益", "可估成本", "缺價格成本",
        "報酬率", "報酬率文字", "價格狀態",
    ]

    df = read_gsheet_table_optional(
        SHEET_TOP15_POSITION_DETAIL,
        needed_cols,
        filter_tracked_brokers=False,
    )
    df = filter_df_by_data_scope(df, DATA_SCOPE_SELECTED5)

    if df.empty:
        old_cache = read_top15_return_cache_from_gsheet(target)
        by_underlying: dict[str, list[tuple[str, float, float | None]]] = defaultdict(list)

        for (underlying, broker), info in old_cache.items():
            amount = safe_float(info.get("remaining_cost"), 0)
            if amount <= 0:
                continue
            by_underlying[underlying].append((broker, amount, info.get("return_pct")))

        for underlying in by_underlying:
            by_underlying[underlying].sort(key=lambda x: x[1], reverse=True)

        return dict(by_underlying)

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

        by_underlying[underlying].append((broker, amount, return_pct))

    for underlying in by_underlying:
        by_underlying[underlying].sort(key=lambda x: x[1], reverse=True)

    return dict(by_underlying)

def read_top15_consensus_cache_from_gsheet(target: date | None = None) -> tuple[list[dict], dict]:
    """
    直接讀取新版「快取_TOP15共識淨買超」。

    這是近一個月 TOP15 圖的唯一排名來源；圖片端不再重新計算 A/B/C/D。
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
    從 A/B/C/D 工作表蒐集 標的股代碼 -> 股名 映射。
    """
    name_counter: dict[str, Counter] = defaultdict(Counter)

    def add_mapping(underlying, text):
        code = normalize_underlying(underlying, text)
        if not code:
            return
        name = extract_stock_name_from_warrant_text(text)
        if name:
            mapped_code = KNOWN_UNDERLYING_NAME_CODE_MAP.get(name)
            if mapped_code:
                code = mapped_code
            name_counter[code][name] += 1

    # A 表：直接用權證名稱
    try:
        A = read_gsheet_table(SHEET_A, ["標的股", "權證名稱"])
        for _, r in A.iterrows():
            add_mapping(r.get("標的股", ""), r.get("權證名稱", ""))
    except Exception:
        pass

    # B/C/D：用權證清單
    for sheet_name in [SHEET_B, SHEET_C, SHEET_D]:
        try:
            df = read_gsheet_table(sheet_name, ["標的股", "權證清單"])
            for _, r in df.iterrows():
                add_mapping(r.get("標的股", ""), r.get("權證清單", ""))
        except Exception:
            pass

    stock_map = {}
    for code, counter in name_counter.items():
        if counter:
            # 次數最多優先；同次數時名稱較短者優先
            best_name = sorted(counter.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
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
    candidates = []

    read_plan = [
        (SHEET_A, ["買進日", "減碼日", "出清日"]),
        (SHEET_B, ["事件日", "減碼日", "出清日"]),
        (SHEET_C, ["結束日", "減碼日", "出清日"]),
        (SHEET_D, ["結束日", "減碼日", "出清日"]),
    ]

    for sheet, cols in read_plan:
        try:
            df = read_gsheet_table(sheet, cols)
        except Exception:
            continue

        for c in cols:
            if c not in df.columns:
                continue
            for v in df[c].dropna().tolist():
                d = parse_date_value(v)
                if d:
                    candidates.append(d)

    if not candidates:
        raise RuntimeError("無法從 Google Sheet 推斷日期，請用 TARGET_DATE=YYYY-MM-DD 指定。")

    return max(candidates)


def read_history_stats_from_gsheet() -> dict:
    """
    勝率統計表格式：
    某列為：
    分點, 事件類型, 事件數, 已出清筆數, ... 勝率, 平均持有天數 ...
    其中事件類型為「全部-A+B+C+D合併」。
    """
    result = {}
    try:
        stat = read_gsheet_stat_raw()
    except Exception:
        stat = pd.DataFrame()

    for broker in TRACKED_BROKERS:
        result[broker] = {
            "total_events": 0,
            "win_rate": 0.0,
            "avg_hold_days": 0.0,
        }

        if stat.empty or stat.shape[1] < 10:
            continue

        rows = stat[(stat[0].astype(str).str.strip() == broker) &
                    (stat[1].astype(str).str.strip() == "全部-A+B+C+D合併")]

        if not rows.empty:
            r = rows.iloc[0]
            result[broker] = {
                "total_events": safe_int(r[2]),
                "win_rate": safe_float(r[8]),
                "avg_hold_days": safe_float(r[9]),
            }

    return result


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


def collect_broker_underlying_add_count_map(target: date, lookback_days: int = ADD_COUNT_LOOKBACK_TRADING_DAYS) -> dict[tuple[str, str], int]:
    """
    計算「同一分點 + 同一標的」在近 N 個有效交易日內，
    出現達買超門檻且尚未出清事件的不同日期次數。

    顯示規則：
    - 第 1 次加碼：圖卡不顯示任何標籤
    - 第 2 次以上：圖卡顯示「加碼N」

    注意：
    同一天同一分點同一標的即使同時出現在 A/B/C/D，仍只算 1 次。
    這裡的 lookback_days 專門給第幾次加碼使用，不會影響原本近一個月 TOP15 圖。
    已在目標日前或目標日出清的買超事件不納入加碼次數；減碼但尚未出清仍會納入。
    """
    try:
        trading_dates = collect_recent_buy_trading_dates(target, lookback_days)
    except Exception:
        trading_dates = []

    if not trading_dates:
        return {}

    date_set = set(trading_dates)
    counter: dict[tuple[str, str], set[date]] = defaultdict(set)

    def add_count_event(row, event_date, amount):
        if not event_date or event_date not in date_set or event_date > target:
            return

        # 已出清的權證不算入「第幾次加碼」。
        # 規則：出清日空白或出清日在目標日之後才納入；出清日 <= 目標日則排除。
        exit_date = parse_date_value(row.get("出清日"))
        if exit_date and exit_date <= target:
            return

        broker = str(row.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            return

        warrant_text = row.get("權證名稱") or row.get("權證清單") or ""
        underlying = normalize_underlying(row.get("標的股"), warrant_text)
        if not underlying:
            return

        if safe_float(amount) < BUY_THRESHOLD:
            return

        counter[(broker, underlying)].add(event_date)

    # A：單檔權證大買
    try:
        A = read_gsheet_table(SHEET_A, ["分點", "標的股", "買進日", "買進金額", "出清日"])
        for _, r in A.iterrows():
            add_count_event(r, parse_date_value(r.get("買進日")), r.get("買進金額"))
    except Exception:
        pass

    # B/C/D：同標的合買、3 日累積、10 日累積
    plans = [
        (SHEET_B, "事件日"),
        (SHEET_C, "結束日"),
        (SHEET_D, "結束日"),
    ]

    for sheet_name, date_col in plans:
        try:
            df = read_gsheet_table(sheet_name, ["分點", "標的股", date_col, "買超金額", "出清日"])
        except Exception:
            continue

        for _, r in df.iterrows():
            add_count_event(r, parse_date_value(r.get(date_col)), r.get("買超金額"))

    return {key: len(days) for key, days in counter.items()}



def normalize_event_code(v) -> str:
    s = strip_gsheet_text_prefix(v).strip()
    if not s or s == "-":
        return ""

    # 常見格式：
    # A
    # A | 066145 聯發永豐63購02
    # A-單檔權證大買
    # B/C/D
    hits = []
    for code in ["A", "B", "C", "D"]:
        if re.search(rf"(^|[^A-Z]){code}([^A-Z]|$)", s):
            hits.append(code)

    if hits:
        return "/".join(hits)

    return s


def is_unclassified_event(v) -> bool:
    s = normalize_event_code(v)
    return s in ["", "-", "未歸類", "ABCD合計"]


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
    從 A/B/C/D 工作表建立「分點 + 權證代號」對照，
    用於每日賣出明細中事件為「未歸類」時補回 A/B/C/D，
    並盡量補上該事件的減碼 / 出清報酬率。
    """
    lookup: dict[tuple[str, str], dict] = {}

    def put(broker, warrant_code, info):
        broker = str(broker).strip()
        warrant_code = normalize_warrant_code(warrant_code)

        if not broker or not warrant_code:
            return

        key = (broker, warrant_code)

        old = lookup.get(key)
        if old:
            # A 優先於 B/C/D；同事件則保留較新的事件日資訊。
            priority = {"A": 4, "B": 3, "C": 2, "D": 1}
            old_p = priority.get(str(old.get("event", "")), 0)
            new_p = priority.get(str(info.get("event", "")), 0)
            if old_p > new_p:
                return

        lookup[key] = info

    # A：單檔權證大買
    try:
        A = read_gsheet_table(
            SHEET_A,
            [
                "分點", "權證代碼", "權證代號", "權證名稱", "標的股",
                "買進日", "買進張數", "買進金額", "減碼日", "減碼獲利%", "出清日", "出清獲利%"
            ]
        )

        for _, r in A.iterrows():
            broker = r.get("分點", "")
            warrant_code = r.get("權證代碼") or r.get("權證代號")
            warrant_name = r.get("權證名稱", "")
            underlying = normalize_underlying(r.get("標的股"), warrant_name)

            return_pct = None
            if target and parse_date_value(r.get("出清日")) == target:
                return_pct = r.get("出清獲利%")
            elif target and parse_date_value(r.get("減碼日")) == target:
                return_pct = r.get("減碼獲利%")

            buy_amount = safe_float(r.get("買進金額"), 0)
            buy_qty = safe_float(r.get("買進張數"), 0)
            buy_avg = buy_amount / (buy_qty * NTD_PER_WARRANT_POINT) if buy_amount > 0 and buy_qty > 0 else 0

            put(broker, warrant_code, {
                "event": "A",
                "underlying": underlying,
                "warrant_name": strip_gsheet_text_prefix(warrant_name),
                "return_pct": return_pct,
                "buy_amount": buy_amount,
                "buy_avg": buy_avg,
                "event_date": parse_date_value(r.get("買進日")),
            })
    except Exception:
        pass

    # B/C/D：用權證清單拆出每一檔權證
    plans = [
        (SHEET_B, "B", "事件日"),
        (SHEET_C, "C", "結束日"),
        (SHEET_D, "D", "結束日"),
    ]

    for sheet_name, event_code, date_col in plans:
        try:
            df = read_gsheet_table(
                sheet_name,
                [
                    "分點", "標的股", date_col, "權證清單",
                    "買超金額", "買超張數", "減碼日", "減碼獲利%",
                    "出清日", "出清獲利%"
                ]
            )
        except Exception:
            continue

        for _, r in df.iterrows():
            broker = r.get("分點", "")
            warrant_list = r.get("權證清單", "")
            underlying = normalize_underlying(r.get("標的股"), warrant_list)

            return_pct = None
            if target and parse_date_value(r.get("出清日")) == target:
                return_pct = r.get("出清獲利%")
            elif target and parse_date_value(r.get("減碼日")) == target:
                return_pct = r.get("減碼獲利%")

            buy_amount = safe_float(r.get("買超金額"), 0)
            buy_qty = safe_float(r.get("買超張數"), 0)
            buy_avg = buy_amount / (buy_qty * NTD_PER_WARRANT_POINT) if buy_amount > 0 and buy_qty > 0 else 0

            for warrant_code, warrant_name in parse_warrant_items_from_text(warrant_list):
                put(broker, warrant_code, {
                    "event": event_code,
                    "underlying": underlying,
                    "warrant_name": strip_gsheet_text_prefix(warrant_name),
                    "return_pct": return_pct,
                    "buy_amount": buy_amount,
                    "buy_avg": buy_avg,
                    "event_date": parse_date_value(r.get(date_col)),
                })

    return lookup



def build_warrant_sell_history_return_lookup(target: date) -> dict[tuple[str, str], dict]:
    """
    從「快取_分點歷史」估算指定日期各分點 + 權證代號的賣出報酬率與對應成本。

    用途：
    1. A/B/C/D 白名單權證若事件表本身沒有報酬率，可用歷史實際買賣資料補估。
    2. 不在 A/B/C/D，但因「同分點 + 同標的今日賣出合計 >= 100 萬」而被納入圖卡的權證，
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
    3. 分點、權證、標的、事件、狀態等文字欄位會盡量保留非空值；事件代號優先保留 A/B/C/D。
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

        # 文字資訊補強：保留非空值，事件代號優先保留可解析的 A/B/C/D。
        for col in [
            "分點名稱", "券商代號", "標的股", "標的名稱", "權證代號", "權證名稱",
            "狀態", "事件日"
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
    1. 若該筆「分點 + 權證代號」曾出現在 A/B/C/D，照原本事件邏輯顯示。
    2. 若該權證未出現在 A/B/C/D，但同一分點今天對同一標的的賣出金額合計
       達 NON_ABCD_SELL_UNDERLYING_THRESHOLD，仍納入賣超明細。
       這類資料不標 A/B/C/D，直接顯示權證與賣出金額。
    3. 非 A/B/C/D 的大額單標的賣超，會從「快取_分點歷史」估算該權證的
       加權平均成本與賣出報酬率。
    """
    needed_cols = [
        "日期", "分點", "分點名稱", "券商代號",
        "事件", "狀態",
        "標的股", "標的名稱",
        "權證代號", "權證名稱",
        "賣出張數", "賣出股數", "賣出金額", "賣出均價",
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
    history_return_lookup = build_warrant_sell_history_return_lookup(target)

    # 先統計「不在 A/B/C/D 白名單」的權證，在同一天 + 同分點 + 同標的的實際賣出合計。
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
        hist_info = history_return_lookup.get((broker, warrant_code), {})

        if lookup_info:
            if is_unclassified_event(event):
                event = lookup_info.get("event", event)

            if not underlying:
                underlying = lookup_info.get("underlying", underlying)

            if not warrant_name:
                warrant_name = lookup_info.get("warrant_name", warrant_name)

            return_pct = normalize_return_pct(lookup_info.get("return_pct"))
            buy_amount = 0.0

            if is_unclassified_event(event):
                # 權證雖可對到 A/B/C/D 白名單，但事件代號仍無法解析時，
                # 不強行標註 A/B/C/D，直接當作單一賣超顯示。
                event = "單一賣超"

            buy_avg = safe_float(lookup_info.get("buy_avg"), 0)
            sell_avg = safe_float(r.get("賣出均價"), 0)

            if buy_avg > 0 and sell_qty > 0:
                buy_amount = buy_avg * sell_qty * NTD_PER_WARRANT_POINT
                if return_pct is None and sell_avg > 0:
                    return_pct = ((sell_avg - buy_avg) / buy_avg) * 100.0
            else:
                buy_amount = safe_float(lookup_info.get("buy_amount"), 0)

            # 若事件表本身沒有足夠資訊，再用快取_分點歷史補估
            if (return_pct is None or safe_float(buy_amount, 0) <= 0) and hist_info:
                if return_pct is None:
                    return_pct = hist_info.get("return_pct")
                if safe_float(buy_amount, 0) <= 0:
                    buy_amount = safe_float(hist_info.get("buy_amount"), 0)

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

        # 不在 A/B/C/D 的權證：同一分點 + 同一標的今日實際賣出合計 >= 100 萬才納入。
        if (broker, underlying) not in qualifying_non_abcd_underlyings:
            continue

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
            hist_info.get("return_pct"),
            safe_float(hist_info.get("buy_amount"), 0),
            warrant_code=warrant_code,
            force_include=True,
            defer_threshold=True,
        )



def extract_actions_from_gsheet(target: date) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []

    # A：單檔權證大買
    # 注意：買超明細仍維持原本 A/B/C/D 事件邏輯；
    # 賣超明細改由「每日賣出明細」讀取實際賣出金額，避免部分減碼被整筆買進張數放大。
    a_cols = [
        "事件類型", "分點", "權證代碼", "權證代號", "權證名稱", "標的股", "買進日", "買進張數", "買進金額",
        "減碼日", "減碼均價", "減碼獲利%", "出清日", "出清均價", "出清獲利%"
    ]
    A = read_gsheet_table(SHEET_A, a_cols)

    for _, r in A.iterrows():
        broker = r.get("分點", "")
        event = "A"

        if parse_date_value(r.get("買進日")) == target:
            append_buy(
                buys, broker, event, r.get("標的股"), r.get("權證名稱"),
                safe_float(r.get("買進金額")), safe_int(r.get("買進張數")), SHEET_A,
                r.get("權證代碼") or r.get("權證代號")
            )

    # B/C/D：同標的合買、3 日累積、10 日累積
    plans = [
        (SHEET_B, "事件日", "B"),
        (SHEET_C, "結束日", "C"),
        (SHEET_D, "結束日", "D"),
    ]

    common_cols = [
        "事件類型", "分點", "標的股", "事件日", "起始日", "結束日", "涵蓋權證數", "權證清單",
        "買超金額", "買超張數", "減碼日", "減碼賣出金額", "減碼獲利%",
        "出清日", "出清賣出金額", "出清獲利%"
    ]

    for sheet_name, event_date_col, event in plans:
        df = read_gsheet_table(sheet_name, common_cols)

        for _, r in df.iterrows():
            broker = r.get("分點", "")

            if parse_date_value(r.get(event_date_col)) == target:
                append_buy(
                    buys, broker, event, r.get("標的股"), r.get("權證清單"),
                    safe_float(r.get("買超金額")), safe_int(r.get("買超張數")), sheet_name
                )

    # 今日賣超明細一律讀取主程式產生的「每日賣出明細」。
    append_daily_sell_rows_from_gsheet(sells, target)

    add_count_map = collect_broker_underlying_add_count_map(target, ADD_COUNT_LOOKBACK_TRADING_DAYS)
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
        # 若同一標的同一天同時出現在 A/B/C/D，會合併為一列，報酬率用買進金額與賣出金額概算。
        # 買方仍保留事件別，避免買超訊號被過度合併。
        if kind == "sell":
            key = (a["broker"], a.get("status", ""), "ABCD合計", a["underlying"] or a["warrant"])
        else:
            key = (a["broker"], a.get("status", ""), a["event"], a["underlying"] or a["warrant"])
        groups[key].append(a)

    result = []
    for (broker, status, event, key_name), items in groups.items():
        amount = sum(i["amount"] for i in items)
        qty = sum(i.get("qty", 0) for i in items)
        warrant_count = len(items)

        # 賣方報酬率合計邏輯：
        # 同一天同分點同標的可能會把 A/B/C/D 權證與「單一賣超」合併。
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
            if kind == "buy" and event in {"B", "C", "D"} and list_count >= 2 and warrant_label:
                first_warrant = re.split(r"[；;]", warrant_label)[0].strip()
                content = f"{first_warrant}；..."
            else:
                content = warrant_label or f"{warrant_count} 檔權證"

            if kind == "sell":
                sell_event_label = event
                if not sell_event_label or sell_event_label == "ABCD合計":
                    sell_event_label = "/".join(sorted({
                        str(i.get("event", "")).strip()
                        for i in items
                        if str(i.get("event", "")).strip()
                        and str(i.get("event", "")).strip() not in {"未歸類", "單一賣超"}
                    }))
                if sell_event_label:
                    content = f"{sell_event_label}｜{content}"

        # 賣超明細內容欄最前面固定保留 A/B/C/D 事件代號。
        # 注意：賣方資料可能在前面被合併成 warrant_count >= 2，
        # 因此不能只在單筆 else 分支加前綴，必須在 result.append 前統一處理。
        if kind == "sell":
            sell_event_label = event
            if not sell_event_label or sell_event_label == "ABCD合計":
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

    result.sort(key=lambda x: x["amount"], reverse=True)
    return result


def read_actual_daily_net_from_history(target: date) -> dict:
    """
    從「快取_分點歷史」計算指定日期五大追蹤分點的實際買賣超。

    用途：
    - 第一張圖 KPI 的「實際淨買超」必須用同一個資料來源計算。
    - 不再用 A/B/C/D 買超事件金額去扣「每日賣出明細」的實際賣出金額，
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

    # KPI 的「實際淨買超」改用同一個資料來源「快取_分點歷史」計算：
    # 今日實際買進金額 - 今日實際賣出金額。
    # 不再混用 A/B/C/D 買超事件金額與每日賣出明細成交金額。
    actual_daily_flow = read_actual_daily_net_from_history(target)
    has_actual_daily_flow = bool(actual_daily_flow.get("has_data"))
    actual_net = safe_float(actual_daily_flow.get("net_amount"), 0)

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
    text(margin_x + 0.18, y, f"精選 5 家分點｜華南永昌台中、元大南屯、富邦敦南、永豐金內湖、永豐金竹北", 15, NAVY2, BOLD)
    y -= 0.32
    text(margin_x + 0.18, y, "紅色＝買超　綠色＝賣超　單位：萬元", 13, TEXT, BOLD)

    # ─────────────────────────────────────────────
    # KPI cards
    # ─────────────────────────────────────────────
    y -= 0.25
    kpi_y = y - kpi_h
    kpi_gap = 0.30
    kpi_w = (content_w - 2 * kpi_gap) / 3
    actual_net_text = fmt_wan(actual_net) if has_actual_daily_flow else "-"
    actual_net_color = RED if actual_net >= 0 else GREEN
    actual_net_bg = PINK if actual_net >= 0 else MINT

    kpis = [
        ("今日買超", f"{sum(x['count'] for x in buys)} 筆", fmt_wan(buy_total), RED, PINK, "↗"),
        ("今日賣超", f"{sum(x['count'] for x in sells)} 筆", fmt_wan(sell_total), GREEN, MINT, "−"),
        ("實際淨買超", "", actual_net_text, actual_net_color, actual_net_bg, "◎"),
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

    legend_items = [
        ("A", "單檔權證單日大買"),
        ("B", "同標的單日合買"),
        ("C", "同標的3日累積"),
        ("D", "近10日累積淨買"),
    ]

    lx = margin_x + 2.00
    for code_name, desc in legend_items:
        rounded(lx, legend_y + 0.10, 0.32, 0.25, fc="#334155", ec="#334155", lw=0.8, r=0.07)
        text(lx + 0.16, legend_y + event_legend_h / 2, code_name, 10, WHITE, BOLD, ha="center")
        text(lx + 0.40, legend_y + event_legend_h / 2, desc, 10.8, TEXT, FONT)
        lx += 2.20 if code_name in {"A", "B"} else 1.98

    # footer
    y -= event_legend_h
    text(fig_w / 2, 0.18, "本圖為籌碼追蹤整理，不構成投資建議。", 11, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)




# ══════════════════════════════════════════════════════════════════════
# 近一個月交易日｜五大分點共識買超 TOP10
# ══════════════════════════════════════════════════════════════════════

def get_buy_event_date(row, sheet_name: str) -> date | None:
    """依事件工作表取得該筆買超事件日期。"""
    if sheet_name == SHEET_A:
        return parse_date_value(row.get("買進日"))
    if sheet_name == SHEET_B:
        return parse_date_value(row.get("事件日"))
    if sheet_name in [SHEET_C, SHEET_D]:
        return parse_date_value(row.get("結束日"))
    return None


def get_sell_event_date(row, sheet_name: str, status: str) -> date | None:
    """依事件工作表取得該筆賣方事件日期。status = 減碼 / 出清"""
    col = "減碼日" if status == "減碼" else "出清日"
    return parse_date_value(row.get(col))


def collect_recent_buy_trading_dates(target: date, lookback_days: int = LOOKBACK_TRADING_DAYS) -> list[date]:
    """
    從 A/B/C/D 買超事件中抓出 <= target 的有效事件日期，
    再往前取最近 N 個「有資料的交易日」。

    這樣春節、連假、休市時不會因為日曆天不足而失真。
    """
    dates = set()

    plans = [
        (SHEET_A, ["分點", "買進日"]),
        (SHEET_B, ["分點", "事件日"]),
        (SHEET_C, ["分點", "結束日"]),
        (SHEET_D, ["分點", "結束日"]),
    ]

    for sheet_name, cols in plans:
        try:
            df = read_gsheet_table(sheet_name, cols)
        except Exception:
            continue

        for _, r in df.iterrows():
            d = get_buy_event_date(r, sheet_name)
            if d and d <= target:
                dates.add(d)

    return sorted(dates, reverse=True)[:lookback_days]


def collect_consensus_buy_top10(target: date, lookback_days: int = LOOKBACK_TRADING_DAYS) -> tuple[list[dict], dict]:
    """
    直接讀取 Google Sheet「快取_TOP15共識淨買超」。

    這張近一個月 TOP15 圖不再由圖片端重新計算 A/B/C/D 或快取_分點歷史，
    lookback_days 只保留相容舊呼叫，實際期間與排名以主程式 Step 4b 產生的快取為準。
    """
    rows, meta = read_top15_consensus_cache_from_gsheet(target)
    return rows, meta

def draw_consensus_buy_image(target: date, output_path: Path, lookback_days: int = LOOKBACK_TRADING_DAYS):
    """
    第二張圖：近一個月交易日｜五大分點共識淨買超成本 TOP15
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
    footer_h = 0.45

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
            return [(top_broker, top_amount, None)], False

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

            prefix = f"{broker} {fmt_wan(amount)} / "
            if not draw_piece(prefix, TEXT):
                return

            ret_text = fmt_return_pct(return_pct)
            ret_color = RED if safe_float(return_pct, 0) > 0 else GREEN if safe_float(return_pct, 0) < 0 else TEXT
            if not draw_piece(ret_text, ret_color):
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
    text(margin_x + 0.15, y, "近一個月交易日｜五大分點共識淨買超成本 TOP15", 28, NAVY, BOLD)
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

    legend_items = [
        ("A", "單檔權證單日大買"),
        ("B", "同標的單日合買"),
        ("C", "同標的3日累積"),
        ("D", "近10日累積淨買"),
    ]

    lx = margin_x + 3.40
    for code_name, desc in legend_items:
        rounded(lx, legend_y + 0.10, 0.32, 0.25, fc="#334155", ec="#334155", lw=0.8, r=0.07)
        text(lx + 0.16, legend_y + legend_h / 2, code_name, 10, WHITE, BOLD, ha="center")
        text(lx + 0.40, legend_y + legend_h / 2, desc, 10.8, TEXT, FONT)
        lx += 2.15 if code_name in {"A", "B"} else 1.95

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
        text(margin_x + content_w / 2, data_y - row_h / 2, "近一個月交易日沒有淨買超成本為正的標的", 13, MUTED, BOLD, ha="center")
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

    text(fig_w / 2, 0.18, "本圖為籌碼追蹤整理，不構成投資建議。", 11, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)



# ══════════════════════════════════════════════════════════════════════
# 本週｜全市場分點標的共識買賣超金額 TOP15
# ══════════════════════════════════════════════════════════════════════

IMAGE_ACTION_DAILY_BUNDLE = "精選五分點每日圖"
IMAGE_ACTION_CONSENSUS_BUY = "近一個月共識淨買超TOP15"
IMAGE_ACTION_WEEKLY_WARRANT = "本週權證共識買賣超TOP15"
IMAGE_ACTION_BROKER_10D = "近10日分點買賣明細圖"
IMAGE_ACTION_ALL = "全部圖片"


def normalize_image_action(action_text: str) -> str:
    """
    將 GitHub Actions / CLI 傳進來的選項轉成程式內部動作。

    支援常見名稱：
    - 精選五分點每日圖 / 精選5分點當日買賣超產圖 / 每日精選分點買賣超追蹤
    - 近一個月共識淨買超TOP15
    - 本週權證共識買賣超TOP15 / 本週買賣超金額各TOP15 / 近7日權證分點共識TOP15
    - 近10日分點買賣明細圖 / 近10日分點明細 / 近10日分點買賣明細
    - 全部圖片
    """
    raw = str(action_text or "").strip()
    key = re.sub(r"[\s_\-｜|/\\]+", "", raw).lower()

    if not key:
        return IMAGE_ACTION_DAILY_BUNDLE

    if "不使用快取" in raw or "重新抓完整資料" in raw or "強制重抓" in raw:
        return IMAGE_ACTION_ALL

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
    prefer_sheet_order: bool = False,
    force_run_id: str = "",
) -> pd.DataFrame:
    """
    同一個統計日期的快取表可能因為 GitHub Action 重跑而累積多個 run_id。

    圖片端若只用「統計日期」過濾，會把舊 run 與新 run 一起讀進來，
    造成 TOP15 / TOP10 出現同標的重複列，或賣超報酬率抓到舊資料。

    處理方式：
    1. 若有 force_run_id，優先讀指定批次。
    2. 若 prefer_sheet_order=True：
       - 優先用 run_id 字串最大的批次，例如 20260611_061357。
       - 這比更新時間更穩，因為 Google Sheet 顯示的新資料可能統計日期仍是前一交易日。
       - 若 run_id 不是 YYYYMMDD_HHMMSS 格式，才退回 Sheet 最上方第一個 run_id。
    3. 其他情況才用「更新時間最大」與 run_id 字串排序作為備援。
    """
    if df is None or df.empty or "run_id" not in df.columns:
        return df

    run_series = df["run_id"].astype(str).map(strip_gsheet_text_prefix).str.strip()

    force_run_id = strip_gsheet_text_prefix(force_run_id).strip()
    if force_run_id:
        forced = df[run_series == force_run_id].copy()
        if not forced.empty:
            if len(forced) != len(df):
                print(f"  ✅ {cache_name} 已套用指定快照：run_id={force_run_id}｜{len(df):,} 筆 → {len(forced):,} 筆")
            return forced
        print(f"  ⚠️ {cache_name} 找不到指定 run_id={force_run_id}，改用自動挑選最新快照。")

    if prefer_sheet_order:
        # 近10日分點明細專用：直接選 run_id 最大的批次。
        # 例如 20260611_061357 會明確大於 20260610_xxxxxx，
        # 避免圖片吃到舊快照。
        valid_run_ids = [
            run_id for run_id in run_series.tolist()
            if re.fullmatch(r"\d{8}_\d{6}", str(run_id).strip())
        ]
        best_run_id = max(valid_run_ids) if valid_run_ids else ""

        if not best_run_id:
            for run_id in run_series.tolist():
                if run_id:
                    best_run_id = run_id
                    break

        if best_run_id:
            filtered = df[run_series == best_run_id].copy()
            if not filtered.empty:
                if len(filtered) != len(df):
                    print(f"  ✅ {cache_name} 已依最大 run_id 套用最新快照：run_id={best_run_id}｜{len(df):,} 筆 → {len(filtered):,} 筆")
                else:
                    print(f"  ✅ {cache_name} 採用 run_id={best_run_id}｜{len(filtered):,} 筆")
                return filtered

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

        base.update({
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


def read_warrant_consensus_7d_rows_from_gsheet(target: date | None = None) -> tuple[list[dict], list[dict], str, date | None]:
    """
    直接讀取「快取_近7日權證分點共識TOP15」。

    重要：
    - 最終排名來源以 warrant_backtest.py 產生的快取工作表為準。
    - 不再從「快取_分點歷史」於圖片端重新計算，避免圖片端與主程式快取口徑不一致。
    - 這張快取表已在主程式端依「同標的所有認購權證金額加總」完成排名。
    - 本圖固定只讀 資料範圍=全分點。

    回傳：
    - 共識買超標的 rows
    - 共識賣超標的 rows
    - 統計期間文字
    - 實際採用的統計日期
    """
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

    df = read_gsheet_table_optional(
        SHEET_WARRANT_CONSENSUS_7D,
        needed_cols,
        filter_tracked_brokers=False,
    )
    df = filter_df_by_data_scope(df, DATA_SCOPE_ALL)

    if df.empty:
        return [], [], "無資料", None

    def pick_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    available_dates = []
    for _, r in df.iterrows():
        d = pick_date(r)
        if d:
            available_dates.append(d)

    if not available_dates:
        return [], [], "無資料", None

    if target is not None:
        valid_dates = [d for d in available_dates if d <= target]
        chosen_date = max(valid_dates) if valid_dates else max(available_dates)
    else:
        chosen_date = max(available_dates)

    df = df[df.apply(lambda r: pick_date(r) == chosen_date, axis=1)].copy()
    df = filter_latest_cache_snapshot(df, SHEET_WARRANT_CONSENSUS_7D)

    if df.empty:
        return [], [], "無資料", chosen_date

    period_values = [strip_gsheet_text_prefix(v) for v in df.get("統計期間", []) if strip_gsheet_text_prefix(v)]
    period_text = period_values[0] if period_values else ""
    if not period_text:
        first_dates = [parse_date_value(v) for v in df.get("第一筆日期", []) if parse_date_value(v)]
        last_dates = [parse_date_value(v) for v in df.get("最後筆日期", []) if parse_date_value(v)]
        if first_dates and last_dates:
            period_text = f"{min(first_dates):%Y/%m/%d} ～ {max(last_dates):%Y/%m/%d}"
        else:
            period_text = "無資料"

    def normalize_rank_type(raw_value: str) -> str:
        s = strip_gsheet_text_prefix(raw_value).strip()
        if s in {"共識買超", "買超", "buy", "BUY"}:
            return "共識買超"
        if s in {"共識賣超", "賣超", "sell", "SELL"}:
            return "共識賣超"
        return s

    def build_rows(rank_type: str) -> list[dict]:
        rows = []
        for _, r in df.iterrows():
            row_rank_type = normalize_rank_type(_pick_first_existing_value(r, ["排名類型", "方向"]))
            if row_rank_type != rank_type:
                continue

            underlying, underlying_name, target_label = resolve_target_identity(
                r,
                code_cols=["標的股", "標的代號", "標的"],
                name_cols=["標的名稱", "股票名稱"],
                raw_target_cols=["標的", "標的名稱", "股票名稱"],
                warrant_text_cols=["權證名稱", "權證清單", "權證代號"],
            )

            buy_amount = safe_float(_pick_first_existing_value(r, ["買進金額"]), 0)
            sell_amount = safe_float(_pick_first_existing_value(r, ["賣出金額"]), 0)
            net_buy_amount = safe_float(_pick_first_existing_value(r, ["淨買超金額"]), buy_amount - sell_amount)
            net_sell_amount = safe_float(_pick_first_existing_value(r, ["淨賣超金額"]), sell_amount - buy_amount)

            rank_amount = safe_float(_pick_first_existing_value(r, ["排名金額"]), 0)
            if rank_amount == 0:
                rank_amount = net_buy_amount if rank_type == "共識買超" else net_sell_amount

            warrant_count = safe_int(_pick_first_existing_value(r, ["權證檔數"]), 0)
            if warrant_count <= 0:
                warrant_count = _warrant_consensus_warrant_count(_pick_first_existing_value(r, ["權證清單", "權證代號"]))

            same_direction_count = safe_int(_pick_first_existing_value(r, ["同向分點數", "參與分點數"]), 0)
            opposite_direction_count = safe_int(_pick_first_existing_value(r, ["反向分點數"]), 0)
            broker_count = safe_int(_pick_first_existing_value(r, ["參與分點數", "同向分點數"]), 0)
            main_brokers = strip_gsheet_text_prefix(_pick_first_existing_value(r, ["主要同向分點", "參與分點"]))
            main_brokers = main_brokers.replace("；", "、")

            rows.append({
                "stat_date": chosen_date,
                "period": period_text,
                "rank_type": rank_type,
                "rank": safe_int(_pick_first_existing_value(r, ["排名"]), 0),
                "underlying": underlying,
                "underlying_name": underlying_name,
                "target": target_label or underlying,
                "rank_amount": rank_amount,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "net_buy_amount": net_buy_amount,
                "net_sell_amount": net_sell_amount,
                "buy_qty": safe_float(_pick_first_existing_value(r, ["買進股數"]), 0),
                "sell_qty": safe_float(_pick_first_existing_value(r, ["賣出股數"]), 0),
                "warrant_count": warrant_count,
                "same_direction_count": same_direction_count,
                "opposite_direction_count": opposite_direction_count,
                "broker_count": broker_count if broker_count > 0 else same_direction_count,
                "main_brokers": main_brokers,
            })

        rows = dedupe_ranked_rows_by_underlying(rows, label=rank_type)
        rows.sort(key=lambda x: (safe_int(x.get("rank"), 999999), -safe_float(x.get("rank_amount"), 0)))
        return rows[:15]

    buy_rows = build_rows("共識買超")
    sell_rows = build_rows("共識賣超")
    return buy_rows, sell_rows, period_text or "無資料", chosen_date
def draw_weekly_warrant_consensus_image(target: date, output_path: Path):
    """
    新增圖片：本週標的分點共識買賣超金額各 TOP15。

    資料來源改為直接讀取「快取_近7日權證分點共識TOP15」。
    排名口徑以主程式快取為準：同標的所有認購權證金額加總後排名。
    """
    buy_rows, sell_rows, period_text, cache_date = read_warrant_consensus_7d_rows_from_gsheet(target)

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
    text(margin_x + 0.15, y, "本週標的分點共識買賣超金額 TOP15", 30, NAVY, BOLD)
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
    text(margin_x + 7.55, summary_y + summary_h / 2, "排名金額＝同標的所有權證近7日買賣互抵後金額", 12.5, TEXT, FONT)
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

    y = draw_top15_table("本週共識買超金額 TOP15", buy_rows, y, NAVY, "買超金額")
    y -= gap
    y = draw_top15_table("本週共識賣超金額 TOP15", sell_rows, y, NAVY, "賣超金額")

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
        "近10日買進股數", "買進股數",
        "近10日買進金額", "買進金額",
        "近10日賣出股數", "賣出股數",
        "近10日賣出金額", "賣出金額",
        "近10日淨買超股數", "淨買超股數",
        "近10日淨買超金額", "淨買超金額",
        "近10日淨賣超金額", "淨賣超金額",
        "買賣方向", "方向",
        "涉及權證檔數", "權證檔數", "權證清單",
        # 近10日快取表的新欄位：圖片端也要讀進來，否則雖然 Sheet 有資料，
        # 但 read_gsheet_table() 會因 needed_cols 沒列到而提前丟掉，導致賣超報酬率顯示成「-」。
        "買超剩餘股數", "買超剩餘成本", "買超目前市值", "買超報酬有效權證數", "買超報酬缺價權證數",
        "賣超實現賣出金額", "賣超實現成本", "賣超成本不足金額",
        "用於勝率報酬%", "用於勝率報酬率", "判定", "備註",
        "買超平均報酬%", "買超平均報酬率", "買超平均報酬率%", "買超報酬%", "買超報酬率", "買超報酬率%", "買超平均損益%", "買超平均損益率", "買超平均損益率%", "買超損益%", "買超損益率", "買超損益率%",
        "賣超平均報酬%", "賣超平均報酬率", "賣超平均報酬率%", "賣超報酬%", "賣超報酬率", "賣超報酬率%", "賣超平均損益%", "賣超平均損益率", "賣超平均損益率%", "賣超損益%", "賣超損益率", "賣超損益率%",
        "買進平均報酬%", "買進平均報酬率", "買進平均報酬率%", "買進報酬%", "買進報酬率", "買進報酬率%",
        "賣出平均報酬%", "賣出平均報酬率", "賣出平均報酬率%", "賣出報酬%", "賣出報酬率", "賣出報酬率%",
        "平均報酬%", "平均報酬率", "報酬率", "報酬率%", "primary_return", "主要報酬率",
        "分點近10日勝率", "近10日勝率", "勝率",
        "分點近10日勝筆數", "近10日勝筆數", "勝筆數",
        "分點近10日敗筆數", "近10日敗筆數", "敗筆數",
        "更新時間", "run_id",
    ]

    # 這裡刻意讀取全部欄位，不再傳 needed_cols。
    # 原因：Google Sheet 表頭可能出現全形％、換行或微小空白，
    # 若先用 needed_cols 精確過濾，真正有值的「賣超平均報酬％ / 用於勝率報酬％」欄位會被提前丟掉，
    # 後面的 fuzzy 欄名比對與成本回推就完全看不到資料，圖片仍會顯示「-」。
    df = read_gsheet_table_optional(
        SHEET_BROKER_10D_DETAIL,
        None,
        filter_tracked_brokers=False,
    )
    # 近10日分點明細這裡先把關鍵欄位統一成「精確欄名」，
    # 特別是使用者要求務必直接抓「賣超平均報酬%」這個欄位名稱一字不變。
    df = _ensure_exact_column_aliases(df, [
        "賣超平均報酬%",
        "買超平均報酬%",
        "用於勝率報酬%",
        "統計日期",
        "分點",
        "標的股",
        "標的名稱",
        "買賣方向",
        "近10日買進金額",
        "近10日賣出金額",
        "近10日淨買超金額",
        "近10日淨賣超金額",
        "賣超實現賣出金額",
        "賣超實現成本",
        "更新時間",
        "run_id",
    ])
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
    }

    if df.empty:
        return [], empty_meta

    def pick_date(row):
        return _pick_first_existing_date(row, ["統計日期", "日期", "目標日期"])

    # 若使用者明確指定 run_id，就先依 run_id 鎖定快照，再從該批資料推統計日期。
    # 這可避免 target date / 統計日期欄位與實際最新 run_id 不一致時，圖片吃到舊資料。
    if BROKER_10D_FORCE_RUN_ID:
        df = filter_latest_cache_snapshot(
            df,
            SHEET_BROKER_10D_DETAIL,
            force_run_id=BROKER_10D_FORCE_RUN_ID,
        )

    available_dates = []
    for _, r in df.iterrows():
        d = pick_date(r)
        if d:
            available_dates.append(d)

    if not available_dates:
        return [], empty_meta

    if BROKER_10D_FORCE_RUN_ID:
        chosen_date = max(available_dates)
    elif target is not None:
        valid_dates = [d for d in available_dates if d <= target]
        chosen_date = max(valid_dates) if valid_dates else max(available_dates)
    else:
        chosen_date = max(available_dates)

    df = df[df.apply(lambda r: pick_date(r) == chosen_date, axis=1)].copy()
    # 近10日分點明細的 Google Sheet 最新資料會在最上方，所以這裡不要再單純用更新時間猜。
    # 直接以 Sheet 最上方的 run_id 作為最新快照；若要手動指定，可設定 BROKER_10D_FORCE_RUN_ID。
    df = filter_latest_cache_snapshot(
        df,
        SHEET_BROKER_10D_DETAIL,
        prefer_sheet_order=True,
        force_run_id=BROKER_10D_FORCE_RUN_ID,
    )
    if broker:
        broker_series = df.apply(lambda r: strip_gsheet_text_prefix(_pick_first_existing_value(r, ["分點", "分點名稱"])).strip(), axis=1)
        df = df[broker_series == broker].copy()

    if df.empty:
        empty_meta["chosen_date"] = chosen_date
        return [], empty_meta

    # 這段固定印出來，因為近10日圖最容易因 run_id 或欄位被讀錯而顯示「-」。
    # 只要 GitHub Actions log 沒看到這幾行，就代表 YML 根本沒有跑到這份程式。
    try:
        selected_run_ids = sorted({
            strip_gsheet_text_prefix(x).strip()
            for x in df.get("run_id", [])
            if strip_gsheet_text_prefix(x).strip()
        }, reverse=True)
        exact_sell_col_exists = "賣超平均報酬%" in df.columns
        exact_sell_nonempty = 0
        if exact_sell_col_exists:
            exact_sell_nonempty = int(df["賣超平均報酬%"].astype(str).map(strip_gsheet_text_prefix).str.strip().replace("-", "").astype(bool).sum())
        print(
            f"  ✅ 近10日分點明細實際採用 run_id：{selected_run_ids[0] if selected_run_ids else '-'}｜"
            f"分點：{broker or '-'}｜筆數：{len(df):,}"
        )
        print(
            f"  ✅ 精確欄位「賣超平均報酬%」存在：{exact_sell_col_exists}｜"
            f"非空筆數：{exact_sell_nonempty:,}"
        )
    except Exception as e:
        print(f"  ⚠️ 近10日分點明細除錯輸出失敗：{type(e).__name__}: {e}")

    if os.getenv("DEBUG_BROKER_10D_DETAIL", "0").strip().lower() in ("1", "true", "yes"):
        debug_cols = []
        for c in [
            "資料範圍", "統計日期", "分點", "標的股", "標的名稱", "買賣方向",
            "近10日買進金額", "近10日賣出金額", "近10日淨賣超金額",
            "賣超平均報酬%", "賣超平均報酬％", "用於勝率報酬%", "用於勝率報酬％",
            "賣超實現賣出金額", "賣超實現成本", "更新時間", "run_id",
        ]:
            real_col = None
            if c in df.columns:
                real_col = c
            else:
                norm_c = _normalize_gsheet_col_name(c)
                for existing_col in df.columns:
                    if _normalize_gsheet_col_name(existing_col) == norm_c:
                        real_col = existing_col
                        break
            if real_col and real_col not in debug_cols:
                debug_cols.append(real_col)
        print("近10日產圖實際讀到欄位：", list(df.columns))
        if debug_cols:
            print(df[debug_cols].to_string(index=False))

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

    def calc_return_pct_from_sell_cost(row_data: dict) -> float | None:
        """
        近10日快取若沒有直接提供「賣超平均報酬%」欄位，
        就用主程式已寫入 Sheet 的「賣超實現賣出金額 / 賣超實現成本」回推報酬率。

        報酬率 = (賣超實現賣出金額 - 賣超實現成本) / 賣超實現成本 * 100
        """
        realized_sell_amount = safe_float(_pick_first_existing_value_fuzzy(row_data, [
            "賣超實現賣出金額", "賣超實現金額", "實現賣出金額",
        ]), 0)
        realized_cost = safe_float(_pick_first_existing_value_fuzzy(row_data, [
            "賣超實現成本", "賣超成本", "實現成本",
        ]), 0)

        if realized_sell_amount > 0 and realized_cost > 0:
            return round((realized_sell_amount - realized_cost) / realized_cost * 100.0, 2)

        return None

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

        # 先直接抓精確欄名：
        # - 買超一定先抓「買超平均報酬%」
        # - 賣超一定先抓「賣超平均報酬%」
        # 使用者要求此欄位名稱必須一字不變，因此這裡先做 direct get，
        # 只有 exact 欄位真的空白時，才退回其他別名與 fuzzy 比對。
        buy_ret = normalize_return_pct(row.get("買超平均報酬%"))
        if buy_ret is None:
            buy_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
                "買超平均報酬%", "買超平均報酬率", "買超平均報酬率%",
                "買超報酬%", "買超報酬率", "買超報酬率%",
                "買超平均損益%", "買超平均損益率", "買超平均損益率%",
                "買超損益%", "買超損益率", "買超損益率%",
                "買進平均報酬%", "買進平均報酬率", "買進平均報酬率%",
                "買進報酬%", "買進報酬率", "買進報酬率%",
            ]))

        sell_ret = normalize_return_pct(row.get("賣超平均報酬%"))
        if sell_ret is None:
            sell_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
                "賣超平均報酬%", "賣超平均報酬率", "賣超平均報酬率%",
                "賣超報酬%", "賣超報酬率", "賣超報酬率%",
                "賣超平均損益%", "賣超平均損益率", "賣超平均損益率%",
                "賣超損益%", "賣超損益率", "賣超損益率%",
                "賣出平均報酬%", "賣出平均報酬率", "賣出平均報酬率%",
                "賣出報酬%", "賣出報酬率", "賣出報酬率%",
            ]))

        generic_ret = normalize_return_pct(row.get("用於勝率報酬%"))
        if generic_ret is None:
            generic_ret = normalize_return_pct(_pick_first_existing_value_fuzzy(row, [
                "用於勝率報酬%", "用於勝率報酬率",
                "平均報酬%", "平均報酬率", "報酬率", "報酬率%", "primary_return", "主要報酬率",
            ]))

        if buy_ret is None and direction == "買超":
            buy_ret = generic_ret

        if sell_ret is None and direction == "賣超":
            sell_ret = generic_ret

        # 最後備援：Sheet 明明有「賣超實現賣出金額 / 賣超實現成本」時，
        # 即使沒有任何賣超報酬率欄位，也要能回推出賣超報酬率，避免圖卡顯示「-」。
        if sell_ret is None and direction == "賣超":
            sell_ret = calc_return_pct_from_sell_cost(row)
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
            "warrant_list": strip_gsheet_text_prefix(_pick_first_existing_value(row, ["權證清單"])),
        })

    rows = merge_broker_10d_rows_by_underlying(rows)

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

    buy_avg_return = weighted_avg_return(buy_rows, "net_buy_amount", "buy_return")
    sell_avg_return = weighted_avg_return(sell_rows, "net_sell_amount", "sell_return")

    shown_rows = buy_rows + sell_rows
    display_win_count = sum(1 for r in shown_rows if normalize_return_pct(r.get("primary_return")) is not None and safe_float(r.get("primary_return"), 0) > 0)
    display_loss_count = sum(1 for r in shown_rows if normalize_return_pct(r.get("primary_return")) is not None and safe_float(r.get("primary_return"), 0) <= 0)
    display_valid_count = display_win_count + display_loss_count
    display_win_rate = round(display_win_count / display_valid_count * 100, 2) if display_valid_count > 0 else None

    # 近10日圖片的重點不是只縮欄位，而是整個輸出畫布要跟表格寬度貼近。
    # 否則即使表格本身不寬，只要 fig_w 太大，左右留白仍會非常巨大。
    # 這裡改成先決定表格寬度，再反推整張圖寬度，讓表格約佔整體寬度 90% 左右。
    broker10d_table_col_w = [0.52, 2.35, 1.62, 1.62, 1.55, 1.12]
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
    summary_cards2 = [
        ("買超TOP10平均報酬", fmt_pct_plain(buy_avg_return), return_color(buy_avg_return), None),
        ("賣超TOP10平均報酬", fmt_pct_plain(sell_avg_return), return_color(sell_avg_return), None),
        ("前10勝率", fmt_pct_plain(display_win_rate), ORANGE, f"勝 {display_win_count} / 敗 {display_loss_count}"),
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
        headers = ["排名", "標的", "買進金額", "賣出金額", "淨額", "報酬率"]
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
                values = [
                    str(i),
                    r.get("target", ""),
                    fmt_amount_wan(r.get("buy_amount", 0)),
                    fmt_amount_wan(r.get("sell_amount", 0)),
                    fmt_amount_wan(net_value),
                    fmt_pct_plain(ret_val),
                ]
                colors = [TEXT, TEXT, RED, GREEN, net_color, ret_color]
                aligns = ["center", "left", "right", "right", "right", "right"]
                bolds = [True, True, True, True, True, True]
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

    text_draw(fig_w / 2, 0.18, "勝率僅以本圖顯示之買超TOP10與賣超TOP10報酬率計算；報酬率 > 0 視為勝，<= 0 視為敗。", 10.8, MUTED, FONT, ha="center")

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
    parser.add_argument("--broker10d-output-dir", default=os.getenv("BROKER_10D_OUTPUT_DIR", ""))
    parser.add_argument(
        "--action",
        default=os.getenv("IMAGE_ACTION", os.getenv("ACTION", os.getenv("RUN_PLAN", ""))),
        help=(
            "圖片產生選項：精選五分點每日圖 / 近一個月共識淨買超TOP15 / "
            "本週權證共識買賣超TOP15 / 近10日分點買賣明細圖 / 全部圖片。也支援 GitHub Actions 的 RUN_PLAN。"
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
    consensus_output_path = Path(args.consensus_output) if args.consensus_output else output_path.parent / "近一個月交易日_五大分點共識淨買超成本TOP15.png"
    weekly_output_path = Path(args.weekly_output) if args.weekly_output else output_path.parent / "本週權證分點共識買賣超TOP15.png"
    broker10d_output_dir = Path(args.broker10d_output_dir) if args.broker10d_output_dir else output_path.parent

    image_paths: list[Path] = []

    print(
        f"Google Sheet：{GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME}\n"
        f"目標日期：{target:%Y-%m-%d}\n"
        f"Action選項：{args.action or '(預設)'}\n"
        f"實際執行：{action}\n"
        f"買超門檻：{BUY_THRESHOLD:.0f}，賣方門檻：{SELL_THRESHOLD:.0f}\n"
        f"加碼次數計算範圍：近 {ADD_COUNT_LOOKBACK_TRADING_DAYS} 個有效交易日"
    )

    if action in [IMAGE_ACTION_DAILY_BUNDLE, IMAGE_ACTION_ALL]:
        history = read_history_stats_from_gsheet()
        buys, sells = extract_actions_from_gsheet(target)

        print(
            f"買超原始筆數：{len(buys)}，賣方提醒原始筆數：{len(sells)}\n"
            f"輸出圖檔1：{output_path}\n"
            f"輸出圖檔2：{consensus_output_path}"
        )

        draw_report_image(target, buys, sells, history, output_path)
        draw_consensus_buy_image(target, consensus_output_path, LOOKBACK_TRADING_DAYS)
        image_paths.append(output_path)
        image_paths.append(consensus_output_path)

    elif action == IMAGE_ACTION_CONSENSUS_BUY:
        print(f"輸出圖檔：{consensus_output_path}")
        draw_consensus_buy_image(target, consensus_output_path, LOOKBACK_TRADING_DAYS)
        image_paths.append(consensus_output_path)

    if action in [IMAGE_ACTION_WEEKLY_WARRANT, IMAGE_ACTION_ALL]:
        print(f"輸出圖檔：{weekly_output_path}")
        draw_weekly_warrant_consensus_image(target, weekly_output_path)
        image_paths.append(weekly_output_path)

    if action in [IMAGE_ACTION_BROKER_10D, IMAGE_ACTION_ALL]:
        print(f"輸出資料來源：{SHEET_BROKER_10D_DETAIL}｜指定分點：{'、'.join(BROKER_10D_IMAGE_BROKERS)}")
        for broker in BROKER_10D_IMAGE_BROKERS:
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
