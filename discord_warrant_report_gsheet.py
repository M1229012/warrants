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
SHEET_WARRANT_CONSENSUS_7D = os.getenv("SHEET_WARRANT_CONSENSUS_7D", "快取_近7日權證分點共識TOP15")


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


def collect_consensus_buy_top10(target: date, lookback_days: int = LOOKBACK_TRADING_DAYS) -> tuple[list[dict], list[date]]:
    """
    統計近 N 個有效交易日內，五大追蹤分點對同一標的的共識淨買超 TOP15。

    統計來源：
    - A_單檔大買：買進日 / 買進金額 / 買進張數
    - B_同標的單日合計：事件日 / 買超金額 / 買超張數
    - C_同標的3日累積：結束日 / 買超金額 / 買超張數
    - D_近10日累積淨買進：結束日 / 買超金額 / 買超張數

    合併方式：
    - 同標的股合併
    - 淨買超成本 = 合計買超成本 - 已賣出張數對應的原始買進成本
    - 不再用「賣出成交金額」直接扣買進金額，避免權證大漲時把剩餘庫存低估
    - 僅保留淨買超成本 > 0 的標的
    - 依淨買超成本由大到小排序
    """
    trading_dates = collect_recent_buy_trading_dates(target, lookback_days)
    date_set = set(trading_dates)

    if not trading_dates:
        return [], []

    agg = {}

    # 只記錄本次近 N 個有效交易日內，真正被 A/B/C/D 買超事件納入統計的
    # 「分點 + 權證代號」。後續賣方扣減只扣這些權證，避免把同分點其他散戶賣單、
    # 舊部位賣單，或不屬於本策略事件的權證賣出拿來扣，導致 TOP15 金額被低估。
    counted_warrant_keys: set[tuple[str, str]] = set()

    # 以「分點 + 權證代號」建立持倉成本批次。
    # 重點：賣出時扣的是「賣出張數對應的原始買進成本」，不是扣賣出成交金額。
    # A 表可精準對到單檔權證；B/C/D 若是多檔權證合計，則使用該列的平均成本作為估算。
    position_lots_by_warrant: dict[tuple[str, str], list[dict]] = defaultdict(list)
    lot_seq = 0

    debug_underlying_code = normalize_underlying(DEBUG_TOP15_UNDERLYING) if DEBUG_TOP15_UNDERLYING else ""
    debug_broker = str(DEBUG_TOP15_BROKER).strip()
    debug_buy_rows: list[dict] = []
    debug_sell_rows: list[dict] = []

    def is_debug_top15_target(broker: str, underlying_code: str) -> bool:
        return (
            DEBUG_TOP15_DETAIL
            and debug_broker
            and debug_underlying_code
            and str(broker).strip() == debug_broker
            and normalize_underlying(underlying_code) == debug_underlying_code
        )

    def get_row_buy_qty_units(row, sheet_name: str) -> float:
        """取得買進數量，統一轉為權證單位數；表內張數 × 1000。"""
        if sheet_name == SHEET_A:
            qty_lots = safe_float(row.get("買進張數"), 0)
        else:
            qty_lots = safe_float(row.get("買超張數"), 0)
        return qty_lots * NTD_PER_WARRANT_POINT if qty_lots > 0 else 0.0

    def get_row_sell_qty_units(row) -> float:
        """取得賣出數量，優先使用賣出股數；沒有時用賣出張數 × 1000。"""
        sell_qty_units = safe_float(row.get("賣出股數"), 0)
        if sell_qty_units <= 0:
            sell_qty_lots = safe_float(row.get("賣出張數"), 0)
            if sell_qty_lots > 0:
                sell_qty_units = sell_qty_lots * NTD_PER_WARRANT_POINT
        return sell_qty_units

    def build_period_sell_cost_lookup(sell_period_start: date, sell_period_end: date) -> dict[tuple[date, str, str], float]:
        """
        從「快取_分點歷史」估算統計期間內每一筆賣出的原始成本。

        這是給以下情況備援：
        - 權證沒有在 A/B/C/D 的持倉批次中建立到可用張數。
        - 非 A/B/C/D 白名單但符合大額同標的賣超扣減條件。

        估算法：
        - 以同分點 + 同權證代號為單位。
        - 依歷史買進股數 / 買進金額建立加權平均成本。
        - 賣出時用「賣出股數 × 賣出前平均成本」作為扣減成本。
        - 同一天若同時有買有賣，沿用前面報酬率估算函式的邏輯：先處理賣出，再處理買進。
        """
        needed_cols = [
            "日期", "分點", "權證代號", "權證代碼",
            "買進股數", "賣出股數", "買進金額", "賣出金額"
        ]
        hist_df = read_gsheet_table_optional(SHEET_HISTORY, needed_cols)
        if hist_df.empty:
            return {}

        grouped: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(lambda: {
            "buy_qty": 0.0,
            "sell_qty": 0.0,
            "buy_amt": 0.0,
            "sell_amt": 0.0,
        }))

        for _, r in hist_df.iterrows():
            d = parse_date_value(r.get("日期"))
            if not d or d > sell_period_end:
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

        result: dict[tuple[date, str, str], float] = defaultdict(float)

        for (broker, warrant_code), by_date in grouped.items():
            hold_qty = 0.0
            hold_cost = 0.0

            for d in sorted(by_date.keys()):
                row = by_date[d]
                buy_qty = safe_float(row.get("buy_qty"), 0)
                sell_qty = safe_float(row.get("sell_qty"), 0)
                buy_amt = safe_float(row.get("buy_amt"), 0)

                if sell_qty > 0:
                    avg_cost = (hold_cost / hold_qty) if hold_qty > 0 else 0.0
                    sell_cost = avg_cost * sell_qty if avg_cost > 0 else 0.0

                    if sell_period_start <= d <= sell_period_end and sell_cost > 0:
                        result[(d, broker, warrant_code)] += sell_cost

                    remove_qty = min(sell_qty, hold_qty)
                    if remove_qty > 0 and avg_cost > 0:
                        remove_cost = avg_cost * remove_qty
                        hold_cost -= remove_cost
                        hold_qty -= remove_qty
                        if hold_qty < 1e-9:
                            hold_qty = 0.0
                            hold_cost = 0.0

                if buy_qty > 0:
                    hold_qty += buy_qty
                    hold_cost += buy_amt

        return dict(result)

    def register_position_lot(
        broker: str,
        underlying_code: str,
        event_code: str,
        event_date: date,
        amount: float,
        buy_qty_units: float,
        warrant_codes: list[str],
    ):
        """建立買進成本批次，之後用賣出張數扣原始成本。"""
        nonlocal lot_seq

        broker = str(broker).strip()
        amount = safe_float(amount, 0)
        buy_qty_units = safe_float(buy_qty_units, 0)
        warrant_codes = [normalize_warrant_code(w) for w in warrant_codes if normalize_warrant_code(w)]

        if not broker or not underlying_code or amount <= 0 or buy_qty_units <= 0 or not warrant_codes:
            return

        lot_seq += 1
        avg_cost_per_unit = amount / buy_qty_units
        lot = {
            "lot_id": lot_seq,
            "broker": broker,
            "underlying": underlying_code,
            "event": event_code,
            "event_date": event_date,
            "buy_amount": amount,
            "remaining_cost": amount,
            "buy_qty_units": buy_qty_units,
            "remaining_qty_units": buy_qty_units,
            "avg_cost_per_unit": avg_cost_per_unit,
            "warrant_codes": set(warrant_codes),
        }

        # 同一個 B/C/D 群組批次可能包含多檔權證，因此同一個 lot 會掛到多個權證代號底下。
        # 之後任一檔權證賣出，都會從同一個 lot 的剩餘張數與剩餘成本扣除，避免重複扣減。
        for warrant_code in warrant_codes:
            position_lots_by_warrant[(broker, warrant_code)].append(lot)
            counted_warrant_keys.add((broker, warrant_code))

    def has_eligible_position_lot(broker: str, warrant_code: str, sell_date: date | None) -> bool:
        """檢查是否存在事件日 <= 賣出日的買進批次，避免較早賣出扣到較晚買進。"""
        broker = str(broker).strip()
        warrant_code = normalize_warrant_code(warrant_code)

        if not broker or not warrant_code or not sell_date:
            return False

        lots = position_lots_by_warrant.get((broker, warrant_code), [])
        for lot in lots:
            lot_event_date = lot.get("event_date")
            if lot_event_date and lot_event_date <= sell_date:
                return True

        return False

    def deduct_sell_cost_from_positions(broker: str, warrant_code: str, sell_qty_units: float, sell_date: date | None) -> float:
        """
        用賣出張數扣掉對應的原始買進成本，回傳本次應扣成本。

        重要：
        - 只能扣「事件日 <= 賣出日」的買進批次。
        - 避免統計期間內較早的賣出，錯誤扣到後面才新出現的 A/B/C/D 買進。
        """
        broker = str(broker).strip()
        warrant_code = normalize_warrant_code(warrant_code)
        sell_qty_units = safe_float(sell_qty_units, 0)

        if not broker or not warrant_code or sell_qty_units <= 0 or not sell_date:
            return 0.0

        lots = position_lots_by_warrant.get((broker, warrant_code), [])
        if not lots:
            return 0.0

        # 依事件日期 FIFO 扣成本；同日則依建立順序。
        lots = sorted(lots, key=lambda x: (x.get("event_date") or date.min, x.get("lot_id", 0)))
        remaining_sell_qty = sell_qty_units
        deducted_cost = 0.0
        used_lot_ids = set()

        for lot in lots:
            lot_id = lot.get("lot_id")
            if lot_id in used_lot_ids:
                continue
            used_lot_ids.add(lot_id)

            lot_event_date = lot.get("event_date")
            if not lot_event_date or lot_event_date > sell_date:
                # 賣出日早於該買進事件日，不可扣這筆新的買進成本。
                continue

            lot_qty = safe_float(lot.get("remaining_qty_units"), 0)
            lot_cost = safe_float(lot.get("remaining_cost"), 0)
            avg_cost_per_unit = safe_float(lot.get("avg_cost_per_unit"), 0)

            if remaining_sell_qty <= 0:
                break
            if lot_qty <= 0 or lot_cost <= 0 or avg_cost_per_unit <= 0:
                continue

            remove_qty = min(remaining_sell_qty, lot_qty)
            remove_cost = min(lot_cost, remove_qty * avg_cost_per_unit)

            lot["remaining_qty_units"] = lot_qty - remove_qty
            lot["remaining_cost"] = lot_cost - remove_cost

            if lot["remaining_qty_units"] < 1e-9:
                lot["remaining_qty_units"] = 0.0
            if lot["remaining_cost"] < 1e-6:
                lot["remaining_cost"] = 0.0

            deducted_cost += remove_cost
            remaining_sell_qty -= remove_qty

        return deducted_cost

    def print_top15_debug_detail():
        """印出指定分點 + 標的的 TOP15 買進與扣減明細，方便核對手算差異。"""
        if not DEBUG_TOP15_DETAIL or not debug_broker or not debug_underlying_code:
            return

        item = agg.get(debug_underlying_code)
        if not item and not debug_buy_rows and not debug_sell_rows:
            return

        buy_total = sum(safe_float(r.get("amount"), 0) for r in debug_buy_rows)
        sell_cost_total = sum(safe_float(r.get("deduct_cost"), 0) for r in debug_sell_rows)
        broker_net = safe_float(item.get("broker_net_amounts", {}).get(debug_broker, 0), 0) if item else 0.0
        broker_raw = safe_float(item.get("broker_amounts", {}).get(debug_broker, 0), 0) if item else 0.0

        print("\n" + "=" * 100)
        print(f"TOP15 計算明細 DEBUG｜分點：{debug_broker}｜標的：{debug_underlying_code}")
        if trading_dates:
            print(f"統計期間：{min(trading_dates):%Y-%m-%d} ～ {max(trading_dates):%Y-%m-%d}｜有效交易日：{len(trading_dates)}")
        print("-" * 100)
        print(f"買進成本合計：{buy_total:,.0f} 元（{fmt_wan(buy_total)}）")
        print(f"扣減成本合計：{sell_cost_total:,.0f} 元（{fmt_wan(sell_cost_total)}）")
        print(f"分點原始買超成本 broker_amounts：{broker_raw:,.0f} 元（{fmt_wan(broker_raw)}）")
        print(f"分點淨買超成本 broker_net_amounts：{broker_net:,.0f} 元（{fmt_wan(broker_net)}）")
        print("-" * 100)

        if debug_buy_rows:
            print("【買進納入明細】")
            for i, r in enumerate(sorted(debug_buy_rows, key=lambda x: (x.get("event_date") or date.min, x.get("event", ""), x.get("warrant_codes", ""))), 1):
                d = r.get("event_date")
                d_text = d.strftime("%Y-%m-%d") if d else "-"
                print(
                    f"{i:02d}. {d_text}｜事件 {r.get('event', '-')}｜"
                    f"權證 {r.get('warrant_codes', '-')}｜"
                    f"買進張數 {safe_float(r.get('buy_qty_units'), 0) / NTD_PER_WARRANT_POINT:,.0f}｜"
                    f"買進金額 {safe_float(r.get('amount'), 0):,.0f} 元（{fmt_wan(r.get('amount', 0))}）"
                )
        else:
            print("【買進納入明細】沒有資料")

        print("-" * 100)
        if debug_sell_rows:
            print("【賣出扣減明細】")
            for i, r in enumerate(sorted(debug_sell_rows, key=lambda x: (x.get("date") or date.min, x.get("warrant_code", ""))), 1):
                d = r.get("date")
                d_text = d.strftime("%Y-%m-%d") if d else "-"
                print(
                    f"{i:02d}. {d_text}｜權證 {r.get('warrant_code', '-')}｜"
                    f"賣出張數 {safe_float(r.get('sell_qty_units'), 0) / NTD_PER_WARRANT_POINT:,.0f}｜"
                    f"賣出金額 {safe_float(r.get('sell_amount'), 0):,.0f} 元｜"
                    f"扣減原始成本 {safe_float(r.get('deduct_cost'), 0):,.0f} 元（{fmt_wan(r.get('deduct_cost', 0))}）"
                )
        else:
            print("【賣出扣減明細】沒有扣減資料")

        print("=" * 100 + "\n")

    def apply_sell_deduction_from_df(sell_df: pd.DataFrame, code_col_candidates: list[str]):
        """
        TOP15 賣方扣減規則：
        1. 只扣「本次近 N 個有效交易日內，被 A/B/C/D 買超事件納入統計的
           同一分點 + 同一權證代號」。
        2. 不再扣非 A/B/C/D 白名單的同標的大額賣出，避免舊部位或非策略事件賣單
           誤扣本次 TOP15 的淨買超成本。

        重要修正：
        - 舊版是直接扣「賣出成交金額」。
        - 新版改為扣「賣出張數對應的原始買進成本」。
        - 這樣權證大賺時，不會因為賣出成交金額變大而低估剩餘買超成本。
        - 只扣事件日 <= 賣出日的買進批次，避免較早賣出扣到後面新買進。
        """
        if sell_df.empty:
            return

        sell_period_start = min(trading_dates)
        sell_period_end = target
        history_sell_cost_lookup = build_period_sell_cost_lookup(sell_period_start, sell_period_end)

        usable_sell_rows = []

        for _, r in sell_df.iterrows():
            d = parse_date_value(r.get("日期"))
            if not d or d < sell_period_start or d > sell_period_end:
                continue

            broker = str(r.get("分點", "")).strip()
            if broker not in TRACKED_BROKERS:
                continue

            warrant_text = r.get("權證名稱", "")
            code = normalize_underlying(r.get("標的股"), warrant_text)
            if not code or code not in agg:
                continue

            sell_amount = safe_float(r.get("賣出金額"), 0)
            if sell_amount <= 0:
                continue

            sell_qty_units = get_row_sell_qty_units(r)

            warrant_code = ""
            for col in code_col_candidates:
                warrant_code = normalize_warrant_code(r.get(col, ""))
                if warrant_code:
                    break

            is_counted_warrant = bool(warrant_code and (broker, warrant_code) in counted_warrant_keys)

            # TOP15 只扣 A/B/C/D 白名單內的同一權證代號。
            # 非 A/B/C/D 的同標的大額賣出不再納入扣減，避免舊部位賣單誤扣新買超事件。
            if not is_counted_warrant:
                continue

            usable_sell_rows.append({
                "date": d,
                "broker": broker,
                "underlying": code,
                "warrant_code": warrant_code,
                "sell_amount": sell_amount,
                "sell_qty_units": sell_qty_units,
            })

        # 同一天、同分點、同標的、同權證先合併，避免 Google Sheet 若有多列時重複扣同一筆歷史成本。
        grouped_sell_rows: dict[tuple[date, str, str, str], dict] = defaultdict(lambda: {
            "sell_amount": 0.0,
            "sell_qty_units": 0.0,
        })

        for row in usable_sell_rows:
            d = row["date"]
            broker = row["broker"]
            code = row["underlying"]
            warrant_code = row["warrant_code"]

            key = (d, broker, code, warrant_code)
            grouped_sell_rows[key]["sell_amount"] += safe_float(row.get("sell_amount"), 0)
            grouped_sell_rows[key]["sell_qty_units"] += safe_float(row.get("sell_qty_units"), 0)

        for (d, broker, code, warrant_code), row in grouped_sell_rows.items():
            sell_qty_units = safe_float(row.get("sell_qty_units"), 0)
            if sell_qty_units <= 0:
                # 沒有張數就無法換算原始成本；不要退回扣賣出金額，避免再次低估。
                continue

            sell_cost = 0.0

            # A/B/C/D 白名單權證優先用本次統計範圍內建立的買進成本批次扣除。
            # 重要：只能扣事件日 <= 賣出日的買進批次，避免較早賣出扣到後面新買進。
            sell_cost = deduct_sell_cost_from_positions(broker, warrant_code, sell_qty_units, d)

            # 若本次統計範圍沒有可扣成本，才用快取_分點歷史估算的加權平均成本作備援。
            # 但若根本沒有事件日 <= 賣出日的買進批次，代表這筆賣出早於本次策略買進，
            # 不可用備援成本去扣新買進。
            if sell_cost <= 0 and warrant_code and has_eligible_position_lot(broker, warrant_code, d):
                sell_cost = safe_float(history_sell_cost_lookup.get((d, broker, warrant_code), 0), 0)

            if sell_cost <= 0:
                continue

            if is_debug_top15_target(broker, code):
                debug_sell_rows.append({
                    "date": d,
                    "warrant_code": warrant_code,
                    "sell_qty_units": sell_qty_units,
                    "sell_amount": safe_float(row.get("sell_amount"), 0),
                    "deduct_cost": sell_cost,
                })

            agg[code]["net_amount"] -= sell_cost
            agg[code]["broker_net_amounts"][broker] -= sell_cost

    def ensure_item(underlying, warrant_text=""):
        code = normalize_underlying(underlying, warrant_text)
        if not code:
            return None, None

        stock_name = get_stock_name_map().get(code, "")
        if not stock_name:
            stock_name = extract_stock_name_from_warrant_text(warrant_text)
        label = f"{code} {stock_name}".strip()

        if code not in agg:
            agg[code] = {
                "underlying": code,
                "stock_name": stock_name,
                "target": label,
                "amount": 0.0,       # 合計買超成本
                "net_amount": 0.0,   # 淨買超成本 = 買超成本 - 已賣出張數對應的原始成本
                "count": 0,
                "brokers": set(),
                "events": set(),
                "broker_amounts": defaultdict(float),
                "broker_net_amounts": defaultdict(float),
                "first_date": None,
                "last_date": None,
            }

        return code, agg[code]

    def add_buy_row(sheet_name, event_code, row, event_date, amount):
        if not event_date or event_date not in date_set:
            return

        broker = str(row.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            return

        amount = safe_float(amount)
        if amount <= 0:
            return

        warrant_text = row.get("權證名稱") or row.get("權證清單") or ""
        code, item = ensure_item(row.get("標的股"), warrant_text)
        if not item:
            return

        item["amount"] += amount
        item["net_amount"] += amount
        item["count"] += 1
        item["brokers"].add(broker)
        item["events"].add(event_code)
        item["broker_amounts"][broker] += amount
        item["broker_net_amounts"][broker] += amount

        if item["first_date"] is None or event_date < item["first_date"]:
            item["first_date"] = event_date
        if item["last_date"] is None or event_date > item["last_date"]:
            item["last_date"] = event_date

        # 建立本次 TOP15 統計範圍內的買進成本批次。
        # A 表通常是一檔權證；B/C/D 則從權證清單拆出多檔權證。
        buy_qty_units = get_row_buy_qty_units(row, sheet_name)
        debug_warrant_codes: list[str] = []

        if sheet_name == SHEET_A:
            warrant_code = normalize_warrant_code(row.get("權證代碼") or row.get("權證代號"))
            if warrant_code:
                debug_warrant_codes = [warrant_code]
                register_position_lot(
                    broker=broker,
                    underlying_code=code,
                    event_code=event_code,
                    event_date=event_date,
                    amount=amount,
                    buy_qty_units=buy_qty_units,
                    warrant_codes=[warrant_code],
                )
        else:
            warrant_codes = [warrant_code for warrant_code, _ in parse_warrant_items_from_text(warrant_text) if warrant_code]
            if warrant_codes:
                debug_warrant_codes = warrant_codes[:]
                register_position_lot(
                    broker=broker,
                    underlying_code=code,
                    event_code=event_code,
                    event_date=event_date,
                    amount=amount,
                    buy_qty_units=buy_qty_units,
                    warrant_codes=warrant_codes,
                )

        if is_debug_top15_target(broker, code):
            debug_buy_rows.append({
                "event_date": event_date,
                "event": event_code,
                "warrant_codes": ",".join(debug_warrant_codes) if debug_warrant_codes else "-",
                "amount": amount,
                "buy_qty_units": buy_qty_units,
                "sheet_name": sheet_name,
            })

    def add_sell_row(row, event_date, amount):
        """
        保留舊函式名稱避免未來擴充時找不到；目前 TOP15 賣方扣減統一由
        apply_sell_deduction_from_df() 依張數換算原始成本處理。
        """
        return

    # A：買超與賣方
    try:
        A = read_gsheet_table(
            SHEET_A,
            ["分點", "標的股", "權證代碼", "權證代號", "權證名稱",
             "買進日", "買進金額", "買進張數",
             "減碼日", "減碼均價", "出清日", "出清均價"]
        )

        for _, r in A.iterrows():
            add_buy_row(SHEET_A, "A", r, parse_date_value(r.get("買進日")), r.get("買進金額"))

        # 賣方扣減改由「快取_分點歷史 / 每日賣出明細」統一處理，並依賣出張數扣原始成本。
    except Exception:
        pass

    # B/C/D：買超與賣方
    plans = [
        (SHEET_B, "B", "事件日"),
        (SHEET_C, "C", "結束日"),
        (SHEET_D, "D", "結束日"),
    ]

    for sheet_name, event_code, date_col in plans:
        try:
            df = read_gsheet_table(
                sheet_name,
                ["分點", "標的股", date_col, "買超金額", "買超張數",
                 "減碼日", "減碼賣出金額", "出清日", "出清賣出金額", "權證清單"]
            )
        except Exception:
            continue

        for _, r in df.iterrows():
            add_buy_row(sheet_name, event_code, r, parse_date_value(r.get(date_col)), r.get("買超金額"))

        # 賣方扣減改由「快取_分點歷史 / 每日賣出明細」統一處理，並依賣出張數扣原始成本。

    # 使用「快取_分點歷史」扣減近 N 個有效交易日內的實際賣出張數對應成本。
    # 注意：每日賣出明細通常只輸出最近幾天，不能拿來做近一個月 TOP15，
    # 否則會只扣到最近幾天的賣出，造成 TOP15 跟過去版本差很多。
    #
    # 扣減規則：
    # 1. 只扣本次近 N 個有效交易日內，已被 A/B/C/D 買超事件納入統計的
    #    「同一分點 + 同一權證代號」。
    # 2. 不再扣非 A/B/C/D 白名單的同標的大額賣出，避免舊部位或非策略事件賣單
    #    誤扣本次 TOP15 的淨買超成本。
    sell_rows_loaded = False
    try:
        sell_df = read_gsheet_table_optional(
            SHEET_HISTORY,
            ["日期", "分點", "標的股", "權證代號", "權證代碼", "權證名稱", "賣出股數", "賣出金額"]
        )

        if not sell_df.empty:
            sell_rows_loaded = True
            apply_sell_deduction_from_df(sell_df, ["權證代號", "權證代碼"])
    except Exception:
        pass

    # 舊版主程式若尚未同步「快取_分點歷史」到 Google Sheet，才退回每日賣出明細。
    # 但每日賣出明細可能只含最近幾天，因此只作備援，不作主要來源。
    if not sell_rows_loaded:
        try:
            sell_df = read_gsheet_table_optional(
                SHEET_DAILY_SELL,
                ["日期", "分點", "標的股", "權證代號", "權證名稱", "賣出張數", "賣出股數", "賣出金額"]
            )

            if not sell_df.empty:
                apply_sell_deduction_from_df(sell_df, ["權證代號"])
        except Exception:
            pass

    top15_return_cache = read_top15_return_cache_from_gsheet(target)
    has_return_cache = bool(top15_return_cache)

    rows = []
    for item in agg.values():
        # 共識淨買超成本榜只保留目前仍為正淨買超成本的標的
        if item["net_amount"] <= 0:
            continue

        top_broker = ""
        top_amount = 0.0
        if item["broker_amounts"]:
            top_broker, top_amount = max(item["broker_amounts"].items(), key=lambda kv: kv[1])

        participant_brokers = []
        for broker, amount in item["broker_net_amounts"].items():
            amount = safe_float(amount, 0)
            if amount <= 0:
                continue

            cache_info = top15_return_cache.get((item["underlying"], broker))

            # 若報酬率快取存在，代表主程式已經算過目前仍有剩餘部位的分點標的。
            # 因此圖片端用快取做最後一道過濾：
            # - 快取沒有這個「標的 + 分點」：視為已出清或不在剩餘部位，該分點不顯示。
            # - 快取有資料但報酬率為空：代表仍有部位但價格不足，保留該分點並顯示「-」。
            if has_return_cache:
                if not cache_info:
                    continue
                if safe_float(cache_info.get("remaining_cost"), 0) <= 0:
                    continue
                return_pct = cache_info.get("return_pct")
            else:
                return_pct = None

            participant_brokers.append((broker, amount, return_pct))

        participant_brokers.sort(key=lambda kv: kv[1], reverse=True)

        # 若快取存在且該標的所有分點都沒有剩餘部位，就不要再排名上來。
        if has_return_cache and not participant_brokers:
            continue

        # 快取存在時，淨買超成本以仍有剩餘部位的分點合計為準，
        # 避免已出清分點的成本還留在標的總額中影響排名。
        display_net_amount = sum(amount for _, amount, _ in participant_brokers) if has_return_cache else item["net_amount"]
        if display_net_amount <= 0:
            continue

        rows.append({
            "target": item["target"],
            "amount": item["amount"],
            "net_amount": display_net_amount,
            "count": item["count"],
            "broker_count": len(participant_brokers) if participant_brokers else len(item["brokers"]),
            "brokers": [broker for broker, _, _ in participant_brokers] if participant_brokers else sorted(item["brokers"]),
            "events": "/".join(sorted(item["events"])),
            "top_broker": top_broker,
            "top_broker_amount": top_amount,
            "participant_brokers": participant_brokers,
            "first_date": item["first_date"],
            "last_date": item["last_date"],
        })

    rows.sort(key=lambda x: (x["net_amount"], x["amount"], x["broker_count"]), reverse=True)
    print_top15_debug_detail()
    return rows[:15], trading_dates

def draw_consensus_buy_image(target: date, output_path: Path, lookback_days: int = LOOKBACK_TRADING_DAYS):
    """
    第二張圖：近一個月交易日｜五大分點共識淨買超成本 TOP15
    """
    rows, trading_dates = collect_consensus_buy_top10(target, lookback_days)
    n = len(rows)

    if trading_dates:
        period_text = f"{min(trading_dates):%Y/%m/%d} ～ {max(trading_dates):%Y/%m/%d}"
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
    text(margin_x + 0.18, y, f"統計期間：近 {len(trading_dates)} 個有效交易日｜{period_text}　｜　同標的合併計算　｜　單位：萬元", 13, TEXT, BOLD)

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
IMAGE_ACTION_ALL = "全部圖片"


def normalize_image_action(action_text: str) -> str:
    """
    將 GitHub Actions / CLI 傳進來的選項轉成程式內部動作。

    支援常見名稱：
    - 精選五分點每日圖 / 精選5分點當日買賣超產圖 / 每日精選分點買賣超追蹤
    - 近一個月共識淨買超TOP15
    - 本週權證共識買賣超TOP15 / 本週買賣超金額各TOP15 / 近7日權證分點共識TOP15
    - 全部圖片
    """
    raw = str(action_text or "").strip()
    key = re.sub(r"[\s_\-｜|/\\]+", "", raw).lower()

    if not key:
        return IMAGE_ACTION_DAILY_BUNDLE

    if "全部" in raw or key in {"all", "全部圖片", "全部產圖"}:
        return IMAGE_ACTION_ALL

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


def read_warrant_consensus_7d_rows_from_gsheet(target: date | None = None) -> tuple[list[dict], list[dict], str, date | None]:
    """
    直接從「快取_分點歷史」計算近 7 天全分點標的共識買賣超 TOP15。

    重要：
    - 不再讀取「快取_近7日權證分點共識TOP15」作為最終排名來源。
    - 會直接讀完整的「快取_分點歷史」全分點資料。
    - 先把同一標的底下所有權證合併，再排序買超 / 賣超 TOP15。

    回傳：
    - 共識買超標的 rows
    - 共識賣超標的 rows
    - 統計期間文字
    - 實際採用的統計日期（期末日）
    """
    needed_cols = [
        "日期", "分點", "標的股", "權證代號", "權證代碼", "權證名稱",
        "買進金額", "賣出金額", "買進股數", "賣出股數",
    ]

    df = read_gsheet_table_optional(SHEET_HISTORY, needed_cols, filter_tracked_brokers=False)
    if df.empty:
        return [], [], "無資料", None

    available_dates = []
    for _, r in df.iterrows():
        d = parse_date_value(r.get("日期"))
        if d:
            available_dates.append(d)

    if not available_dates:
        return [], [], "無資料", None

    if target is not None:
        valid_dates = [d for d in available_dates if d <= target]
        chosen_date = max(valid_dates) if valid_dates else max(available_dates)
    else:
        chosen_date = max(available_dates)

    period_start = chosen_date - timedelta(days=6)
    period_text = f"{period_start:%Y/%m/%d} ～ {chosen_date:%Y/%m/%d}"

    stock_name_map = get_stock_name_map()
    grouped: dict[str, dict] = {}

    for _, r in df.iterrows():
        d = parse_date_value(r.get("日期"))
        if not d or d < period_start or d > chosen_date:
            continue

        broker = str(r.get("分點", "")).strip()
        if not broker:
            continue

        warrant_name = strip_gsheet_text_prefix(r.get("權證名稱", ""))
        underlying = normalize_underlying(r.get("標的股", ""), warrant_name)
        if not underlying:
            continue

        buy_amount = safe_float(r.get("買進金額"), 0)
        sell_amount = safe_float(r.get("賣出金額"), 0)
        buy_qty = safe_float(r.get("買進股數"), 0)
        sell_qty = safe_float(r.get("賣出股數"), 0)
        if buy_amount <= 0 and sell_amount <= 0 and buy_qty <= 0 and sell_qty <= 0:
            continue

        underlying_name = stock_name_map.get(underlying, "")
        if not underlying_name:
            underlying_name = extract_stock_name_from_warrant_text(warrant_name)

        item = grouped.setdefault(
            underlying,
            {
                "underlying": underlying,
                "underlying_name": underlying_name,
                "target": f"{underlying} {underlying_name}".strip(),
                "buy_amount": 0.0,
                "sell_amount": 0.0,
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "warrant_codes": set(),
                "warrant_names": set(),
                "broker_buy": defaultdict(float),
                "broker_sell": defaultdict(float),
            },
        )

        if not item.get("underlying_name") and underlying_name:
            item["underlying_name"] = underlying_name
            item["target"] = f"{underlying} {underlying_name}".strip()

        item["buy_amount"] += buy_amount
        item["sell_amount"] += sell_amount
        item["buy_qty"] += buy_qty
        item["sell_qty"] += sell_qty

        warrant_code = normalize_warrant_code(r.get("權證代號") or r.get("權證代碼"))
        if warrant_code:
            item["warrant_codes"].add(warrant_code)
        if warrant_name:
            item["warrant_names"].add(warrant_name)

        item["broker_buy"][broker] += buy_amount
        item["broker_sell"][broker] += sell_amount

    def build_direction_rows(direction: str) -> list[dict]:
        rows = []
        for item in grouped.values():
            buy_amount = safe_float(item.get("buy_amount"), 0)
            sell_amount = safe_float(item.get("sell_amount"), 0)
            net_buy_amount = max(buy_amount - sell_amount, 0.0)
            net_sell_amount = max(sell_amount - buy_amount, 0.0)
            rank_amount = net_buy_amount if direction == "buy" else net_sell_amount
            if rank_amount <= 0:
                continue

            same_direction_brokers = []
            opposite_direction_count = 0
            all_brokers = set(item.get("broker_buy", {}).keys()) | set(item.get("broker_sell", {}).keys())
            for broker in all_brokers:
                broker_buy = safe_float(item.get("broker_buy", {}).get(broker), 0)
                broker_sell = safe_float(item.get("broker_sell", {}).get(broker), 0)
                net = broker_buy - broker_sell
                if direction == "buy":
                    if net > 0:
                        same_direction_brokers.append((broker, net))
                    elif net < 0:
                        opposite_direction_count += 1
                else:
                    if net < 0:
                        same_direction_brokers.append((broker, -net))
                    elif net > 0:
                        opposite_direction_count += 1

            same_direction_brokers.sort(key=lambda kv: kv[1], reverse=True)
            same_direction_count = len(same_direction_brokers)
            main_brokers = "、".join(
                f"{broker} {fmt_wan(amount)}"
                for broker, amount in same_direction_brokers[:5]
            )
            if len(same_direction_brokers) > 5:
                main_brokers += f"、等{len(same_direction_brokers)}家"

            warrant_count = len(item.get("warrant_codes", set()))
            if warrant_count <= 0:
                warrant_count = len(item.get("warrant_names", set()))
            if warrant_count <= 0:
                warrant_count = 1

            rows.append({
                "stat_date": chosen_date,
                "period": period_text,
                "rank_type": "共識買超" if direction == "buy" else "共識賣超",
                "rank": 0,
                "underlying": item.get("underlying", ""),
                "underlying_name": item.get("underlying_name", ""),
                "target": item.get("target", "") or item.get("underlying", ""),
                "rank_amount": rank_amount,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "net_buy_amount": net_buy_amount,
                "net_sell_amount": net_sell_amount,
                "buy_qty": safe_float(item.get("buy_qty"), 0),
                "sell_qty": safe_float(item.get("sell_qty"), 0),
                "warrant_count": warrant_count,
                "same_direction_count": same_direction_count,
                "opposite_direction_count": opposite_direction_count,
                "broker_count": same_direction_count,
                "main_brokers": main_brokers,
            })

        rows.sort(
            key=lambda x: (
                safe_float(x.get("rank_amount"), 0),
                safe_float(x.get("buy_amount"), 0) + safe_float(x.get("sell_amount"), 0),
                safe_int(x.get("warrant_count"), 0),
                safe_int(x.get("same_direction_count"), 0),
            ),
            reverse=True,
        )

        for idx, row in enumerate(rows, 1):
            row["rank"] = idx

        return rows[:15]

    buy_rows = build_direction_rows("buy")
    sell_rows = build_direction_rows("sell")
    return buy_rows, sell_rows, period_text, chosen_date


def draw_weekly_warrant_consensus_image(target: date, output_path: Path):
    """
    新增圖片：本週標的分點共識買賣超金額各 TOP15。

    資料來源改為直接讀取「快取_分點歷史」全分點資料，
    並在圖片端先依標的合併所有權證後再重新排序。
    """
    buy_rows, sell_rows, period_text, cache_date = read_warrant_consensus_7d_rows_from_gsheet(target)

    total_buy_rank_amount = sum(safe_float(r.get("rank_amount"), 0) for r in buy_rows)
    total_sell_rank_amount = sum(safe_float(r.get("rank_amount"), 0) for r in sell_rows)
    cache_date_text = cache_date.strftime("%Y/%m/%d") if cache_date else target.strftime("%Y/%m/%d")

    fig_w = 13.0
    margin_x = 0.40
    content_w = fig_w - 2 * margin_x

    top_h = 1.60
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
    parser.add_argument(
        "--action",
        default=os.getenv("IMAGE_ACTION", os.getenv("ACTION", os.getenv("RUN_PLAN", ""))),
        help=(
            "圖片產生選項：精選五分點每日圖 / 近一個月共識淨買超TOP15 / "
            "本週權證共識買賣超TOP15 / 全部圖片。也支援 GitHub Actions 的 RUN_PLAN。"
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
