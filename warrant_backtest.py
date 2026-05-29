# -*- coding: utf-8 -*-
"""
艾斯大戶權證追蹤圖像週報產生器

功能：
1. 從 Google Sheet 讀取權證籌碼資料
2. 篩選指定股票代號近 14 日大戶買進與疑似未出清紀錄
3. 抓取最近 70 根日 K
4. 自動畫出支撐、壓力、大量支撐區、三角收斂線
5. 抓取近 14 日新聞 RSS 摘要
6. 輸出 PNG 圖像週報

本程式為 MVP 版本，欄位名稱已盡量做自動對應。
如果你的 Google Sheet 欄位名稱不同，請優先調整 COLUMN_ALIASES。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import textwrap
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus

import feedparser
import gspread
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from google.oauth2.service_account import Credentials
from matplotlib.patches import Rectangle

warnings.filterwarnings("ignore", category=UserWarning)


# ============================================================
# 1. 欄位別名設定：如果 Google Sheet 欄位抓不到，優先改這裡
# ============================================================

COLUMN_ALIASES: Dict[str, List[str]] = {
    "stock_id": [
        "股票代號", "標的代號", "標的股票代號", "個股代號", "標的", "underlying", "Underlying", "stock_id", "StockID",
    ],
    "stock_name": [
        "股票名稱", "標的名稱", "個股名稱", "名稱", "stock_name", "StockName",
    ],
    "date": [
        "日期", "買進日期", "交易日期", "首次買進日", "進場日期", "date", "Date",
    ],
    "broker": [
        "分點", "券商分點", "券商", "大戶分點", "broker", "Broker",
    ],
    "warrant_code": [
        "權證代號", "權證", "商品代號", "warrant_code", "WarrantCode",
    ],
    "warrant_name": [
        "權證名稱", "商品名稱", "warrant_name", "WarrantName",
    ],
    "buy_amount": [
        "買超金額", "買進金額", "買入金額", "大戶買進金額", "買超", "買進", "buy_amount", "BuyAmount",
    ],
    "sell_amount": [
        "賣超金額", "賣出金額", "賣出", "賣超", "sell_amount", "SellAmount",
    ],
    "net_amount": [
        "淨買超金額", "淨買超", "買賣超", "net_amount", "NetAmount",
    ],
    "status": [
        "狀態", "出清狀態", "是否出清", "持有狀態", "未出清", "status", "Status",
    ],
    "add_count": [
        "加碼", "加碼次數", "加碼第幾次", "加碼標籤", "add_count", "AddCount",
    ],
}


# ============================================================
# 2. 資料結構
# ============================================================

@dataclass
class WarrantSummary:
    stock_id: str
    stock_name: str
    total_rows: int
    recent_rows: int
    pending_rows: int
    total_buy_amount: float
    pending_buy_amount: float
    brokers: List[str]
    top_rows: pd.DataFrame
    date_start: Optional[pd.Timestamp]
    date_end: Optional[pd.Timestamp]


@dataclass
class TechnicalSummary:
    support_level: Optional[float]
    resistance_level: Optional[float]
    volume_support_zone: Optional[Tuple[float, float]]
    triangle_lines: Optional[Dict[str, Tuple[float, float]]]
    pattern_name: str
    notes: List[str]


@dataclass
class NewsItem:
    title: str
    source: str
    published: str
    link: str


# ============================================================
# 3. 共用工具
# ============================================================

def setup_chinese_font() -> str:
    """在本機與 GitHub Actions 嘗試設定中文字型。"""
    candidate_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msjhbd.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]

    for path in candidate_paths:
        if Path(path).exists():
            try:
                fm.fontManager.addfont(path)
                font_name = fm.FontProperties(fname=path).get_name()
                plt.rcParams["font.family"] = font_name
                plt.rcParams["axes.unicode_minus"] = False
                return font_name
            except Exception:
                continue

    # fallback：不一定能顯示中文，但不讓程式中斷
    plt.rcParams["axes.unicode_minus"] = False
    return "DejaVu Sans"


def normalize_col_name(s: Any) -> str:
    return str(s).strip().replace(" ", "").replace("\n", "")


def find_column(df: pd.DataFrame, logical_name: str) -> Optional[str]:
    aliases = COLUMN_ALIASES.get(logical_name, [])
    norm_to_original = {normalize_col_name(c): c for c in df.columns}
    for alias in aliases:
        key = normalize_col_name(alias)
        if key in norm_to_original:
            return norm_to_original[key]
    return None


def get_value(row: pd.Series, col: Optional[str], default: Any = "") -> Any:
    if col is None or col not in row.index:
        return default
    val = row[col]
    if pd.isna(val):
        return default
    return val


def parse_tw_amount(value: Any) -> float:
    """解析台股常見金額格式：1,234萬、2.5億、+500000、空白。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)

    s = str(value).strip()
    if not s or s in {"-", "--", "nan", "None"}:
        return 0.0

    multiplier = 1.0
    if "億" in s:
        multiplier = 100_000_000.0
    elif "萬" in s:
        multiplier = 10_000.0
    elif "千" in s:
        multiplier = 1_000.0

    s = s.replace(",", "").replace("+", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {".", "-"}:
        return 0.0
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def parse_date(value: Any) -> Optional[pd.Timestamp]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip()
    if not s or s in {"-", "--", "nan", "None"}:
        return None

    # 常見民國年格式：115/05/29
    m = re.match(r"^(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})$", s)
    if m:
        year = int(m.group(1))
        if year < 1911:
            year += 1911
        try:
            return pd.Timestamp(year=year, month=int(m.group(2)), day=int(m.group(3)))
        except Exception:
            pass

    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None
        return pd.Timestamp(ts).tz_localize(None) if getattr(ts, "tzinfo", None) else pd.Timestamp(ts)
    except Exception:
        return None


def money_tw(value: float) -> str:
    try:
        value = float(value)
    except Exception:
        return "-"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100_000_000:
        return f"{sign}{value / 100_000_000:.2f}億"
    if value >= 10_000:
        return f"{sign}{value / 10_000:.0f}萬"
    return f"{sign}{value:,.0f}"


def short_text(text: Any, limit: int = 16) -> str:
    s = str(text).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def wrap_zh_text(text: str, width: int = 34) -> str:
    if not text:
        return ""
    lines = []
    for para in str(text).split("\n"):
        if len(para) <= width:
            lines.append(para)
        else:
            lines.extend(textwrap.wrap(para, width=width, break_long_words=True, replace_whitespace=False))
    return "\n".join(lines)


def clean_stock_id(stock_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(stock_id).strip())


# ============================================================
# 4. Google Sheet 讀取
# ============================================================

def build_gspread_client() -> gspread.Client:
    """支援兩種認證：
    1. GOOGLE_SERVICE_ACCOUNT_JSON：GitHub Secrets 放完整 JSON 字串
    2. GOOGLE_APPLICATION_CREDENTIALS：本機 service_account.json 檔案路徑
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    json_str = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "") or os.getenv("GCP_SERVICE_KEY", "")).strip()
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    if json_str:
        try:
            info = json.loads(json_str)
        except json.JSONDecodeError:
            # 有些環境會把 \n 轉義弄掉，這裡做一次容錯
            info = json.loads(json_str.replace("\\n", "\n"))
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(credentials)

    if cred_path and Path(cred_path).exists():
        credentials = Credentials.from_service_account_file(cred_path, scopes=scopes)
        return gspread.authorize(credentials)

    raise RuntimeError(
        "找不到 Google Sheets 認證。請設定 GOOGLE_SERVICE_ACCOUNT_JSON、GCP_SERVICE_KEY 或 GOOGLE_APPLICATION_CREDENTIALS。"
    )


def _normalize_col_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _known_header_names() -> set:
    names = set()
    for alias_list in COLUMN_ALIASES.values():
        for name in alias_list:
            names.add(_normalize_col_name(name))
    return names


def _make_unique_headers(raw_headers: Sequence[Any]) -> List[str]:
    """
    gspread.get_all_records() 遇到空白欄名或重複欄名會直接報錯，
    這裡改成自己清洗欄位名稱：
    - 空白欄位命名為 __blank_col_N
    - 重複欄位加上 __2、__3 後綴
    """
    headers: List[str] = []
    seen: Dict[str, int] = {}

    for idx, header in enumerate(raw_headers, start=1):
        name = str(header or "").strip()
        if not name:
            name = f"__blank_col_{idx}"

        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 1

        headers.append(name)

    return headers


def _detect_header_row(values: List[List[str]], force_first_non_empty: bool = False) -> Optional[int]:
    """
    自動找出真正的標題列。
    你的 Google Sheet 可能前幾列是標題、說明、空白列或合併儲存格，
    所以不能直接假設第 1 列就是欄位名稱。
    """
    known_names = _known_header_names()
    best_idx: Optional[int] = None
    best_score = -1
    best_hits = 0

    for idx, row in enumerate(values[:30]):
        cells = [str(c or "").strip() for c in row]
        non_empty_count = sum(1 for c in cells if c)
        if non_empty_count == 0:
            continue

        alias_hits = sum(1 for c in cells if _normalize_col_name(c) in known_names)
        score = alias_hits * 100 + min(non_empty_count, 30)

        if score > best_score:
            best_score = score
            best_idx = idx
            best_hits = alias_hits

    if best_idx is not None and best_hits > 0:
        return best_idx

    if force_first_non_empty:
        for idx, row in enumerate(values[:30]):
            non_empty_count = sum(1 for c in row if str(c or "").strip())
            if non_empty_count >= 2:
                return idx

    return None


def _worksheet_to_dataframe(ws: gspread.Worksheet, force_first_non_empty_header: bool = False) -> Optional[pd.DataFrame]:
    """
    用 get_all_values() 取代 get_all_records()，避免：
    gspread.exceptions.GSpreadException: the header row contains duplicates: ['']
    """
    values = ws.get_all_values()
    if not values:
        print(f"  - 略過工作表「{ws.title}」：空白工作表")
        return None

    header_idx = _detect_header_row(values, force_first_non_empty=force_first_non_empty_header)
    if header_idx is None:
        print(f"  - 略過工作表「{ws.title}」：找不到可辨識的標題列")
        return None

    raw_headers = values[header_idx]
    max_cols = max(len(raw_headers), *(len(r) for r in values[header_idx + 1:])) if len(values) > header_idx + 1 else len(raw_headers)

    raw_headers = list(raw_headers) + [""] * (max_cols - len(raw_headers))
    headers = _make_unique_headers(raw_headers[:max_cols])

    rows: List[List[str]] = []
    for row in values[header_idx + 1:]:
        padded = list(row) + [""] * (max_cols - len(row))
        padded = padded[:max_cols]
        if any(str(cell or "").strip() for cell in padded):
            rows.append(padded)

    if not rows:
        print(f"  - 略過工作表「{ws.title}」：標題列下方沒有資料")
        return None

    df = pd.DataFrame(rows, columns=headers)

    # 移除整欄皆空的空白欄位，避免合併儲存格或多餘欄位污染資料
    blank_cols = [
        c for c in df.columns
        if str(c).startswith("__blank_col_") and df[c].astype(str).str.strip().eq("").all()
    ]
    if blank_cols:
        df = df.drop(columns=blank_cols)

    df["__worksheet__"] = ws.title
    print(f"  - 已讀取工作表「{ws.title}」：{len(df)} 筆，標題列第 {header_idx + 1} 列")
    return df


def load_warrant_sheet(sheet_name: str, worksheet_name: Optional[str] = None) -> pd.DataFrame:
    client = build_gspread_client()
    spreadsheet = client.open(sheet_name)

    frames: List[pd.DataFrame] = []
    if worksheet_name:
        worksheets = [spreadsheet.worksheet(worksheet_name)]
    else:
        worksheets = spreadsheet.worksheets()

    for ws in worksheets:
        df = _worksheet_to_dataframe(
            ws,
            force_first_non_empty_header=bool(worksheet_name),
        )
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        raise RuntimeError(
            f"Google Sheet：{sheet_name} 沒有讀到任何資料。"
            "請確認工作表有標題列，或在 workflow 的 worksheet 輸入正確工作表名稱。"
        )

    result = pd.concat(frames, ignore_index=True, sort=False)
    result.columns = [str(c).strip() for c in result.columns]
    return result


# ============================================================
# 5. 權證資料整理
# ============================================================

def row_matches_stock(row: pd.Series, stock_id: str, stock_col: Optional[str]) -> bool:
    target = clean_stock_id(stock_id)

    if stock_col:
        raw = str(row.get(stock_col, "")).strip()
        if clean_stock_id(raw) == target:
            return True
        if target and re.search(rf"(^|\D){re.escape(target)}(\D|$)", raw):
            return True

    # 欄位找不到時，做保守 fallback：整列文字含股票代號
    all_text = " ".join(str(x) for x in row.values)
    return bool(target and re.search(rf"(^|\D){re.escape(target)}(\D|$)", all_text))


def infer_pending_status(row: pd.Series, status_col: Optional[str], buy_col: Optional[str], sell_col: Optional[str], net_col: Optional[str]) -> bool:
    status = str(get_value(row, status_col, "")).strip()

    # 注意：「未出清」也包含「出清」兩個字，所以要先判斷未出清
    pending_keywords = ["未出清", "尚未出清", "持有", "未賣", "沒出", "仍在", "未明顯出清"]
    cleared_keywords = ["已出清", "出清", "賣出", "結清", "已賣"]

    if any(k in status for k in pending_keywords):
        return True
    if any(k in status for k in cleared_keywords):
        return False

    buy_amount = parse_tw_amount(get_value(row, buy_col, 0))
    sell_amount = parse_tw_amount(get_value(row, sell_col, 0))
    net_amount = parse_tw_amount(get_value(row, net_col, 0)) if net_col else 0.0

    if net_col:
        return net_amount > 0
    if buy_amount > 0 and sell_amount <= 0:
        return True
    if buy_amount > sell_amount:
        return True
    return False


def extract_add_count(value: Any) -> str:
    s = str(value).strip()
    if not s or s in {"nan", "None", "-"}:
        return "-"
    m = re.search(r"(\d+)", s)
    if m:
        return f"#{m.group(1)}"
    return short_text(s, 8)


def build_warrant_summary(raw_df: pd.DataFrame, stock_id: str) -> WarrantSummary:
    stock_col = find_column(raw_df, "stock_id")
    stock_name_col = find_column(raw_df, "stock_name")
    date_col = find_column(raw_df, "date")
    broker_col = find_column(raw_df, "broker")
    warrant_code_col = find_column(raw_df, "warrant_code")
    warrant_name_col = find_column(raw_df, "warrant_name")
    buy_col = find_column(raw_df, "buy_amount")
    sell_col = find_column(raw_df, "sell_amount")
    net_col = find_column(raw_df, "net_amount")
    status_col = find_column(raw_df, "status")
    add_col = find_column(raw_df, "add_count")

    matched = raw_df[raw_df.apply(lambda r: row_matches_stock(r, stock_id, stock_col), axis=1)].copy()
    if matched.empty:
        # 回傳空 summary，讓報告仍能產出 K 線與新聞
        return WarrantSummary(
            stock_id=stock_id,
            stock_name="",
            total_rows=0,
            recent_rows=0,
            pending_rows=0,
            total_buy_amount=0,
            pending_buy_amount=0,
            brokers=[],
            top_rows=pd.DataFrame(),
            date_start=None,
            date_end=None,
        )

    matched["_date"] = matched[date_col].apply(parse_date) if date_col else None
    matched["_buy_amount"] = matched[buy_col].apply(parse_tw_amount) if buy_col else 0.0
    matched["_sell_amount"] = matched[sell_col].apply(parse_tw_amount) if sell_col else 0.0
    matched["_pending"] = matched.apply(lambda r: infer_pending_status(r, status_col, buy_col, sell_col, net_col), axis=1)
    matched["_broker"] = matched[broker_col].astype(str).str.strip() if broker_col else "-"
    matched["_warrant_code"] = matched[warrant_code_col].astype(str).str.strip() if warrant_code_col else "-"
    matched["_warrant_name"] = matched[warrant_name_col].astype(str).str.strip() if warrant_name_col else "-"
    matched["_add"] = matched[add_col].apply(extract_add_count) if add_col else "-"
    matched["_status_text"] = matched[status_col].astype(str).str.strip() if status_col else np.where(matched["_pending"], "疑似未出清", "可能已出清")

    # stock_name：優先從欄位抓，否則空白
    stock_name = ""
    if stock_name_col:
        names = [str(x).strip() for x in matched[stock_name_col].dropna().tolist() if str(x).strip()]
        stock_name = names[0] if names else ""

    today = pd.Timestamp.today().normalize()
    recent_cutoff = today - pd.Timedelta(days=14)
    if matched["_date"].notna().any():
        recent = matched[(matched["_date"].notna()) & (matched["_date"] >= recent_cutoff)].copy()
        if recent.empty:
            # 如果資料日期不是最新，取最後 14 個自然日資料可能會空；退而取最近 14 筆有日期資料
            recent = matched.sort_values("_date", ascending=False).head(14).copy()
    else:
        recent = matched.copy()

    pending_recent = recent[recent["_pending"]].copy()

    sort_cols = []
    ascending = []
    if "_date" in pending_recent.columns:
        sort_cols.append("_date")
        ascending.append(False)
    sort_cols.append("_buy_amount")
    ascending.append(False)

    if pending_recent.empty:
        top_rows = recent.sort_values("_buy_amount", ascending=False).head(7).copy()
    else:
        top_rows = pending_recent.sort_values(sort_cols, ascending=ascending).head(7).copy()

    brokers = [b for b in pending_recent["_broker"].dropna().astype(str).unique().tolist() if b and b != "-"]

    date_start = recent["_date"].min() if "_date" in recent and recent["_date"].notna().any() else None
    date_end = recent["_date"].max() if "_date" in recent and recent["_date"].notna().any() else None

    return WarrantSummary(
        stock_id=stock_id,
        stock_name=stock_name,
        total_rows=len(matched),
        recent_rows=len(recent),
        pending_rows=len(pending_recent),
        total_buy_amount=float(recent["_buy_amount"].sum()) if "_buy_amount" in recent else 0.0,
        pending_buy_amount=float(pending_recent["_buy_amount"].sum()) if "_buy_amount" in pending_recent else 0.0,
        brokers=brokers,
        top_rows=top_rows,
        date_start=date_start,
        date_end=date_end,
    )


# ============================================================
# 6. 股價與技術型態
# ============================================================

def download_stock_history(stock_id: str, bars: int = 70) -> Tuple[pd.DataFrame, str]:
    """抓取台股資料，先試上市 .TW，再試上櫃 .TWO。"""
    stock_id = clean_stock_id(stock_id)
    candidates = [f"{stock_id}.TW", f"{stock_id}.TWO"]

    last_error = None
    for ticker in candidates:
        try:
            df = yf.download(ticker, period="9mo", interval="1d", auto_adjust=False, progress=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            df = df[df["Volume"] > 0]
            if len(df) >= max(20, bars // 2):
                df = df.tail(bars).copy()
                df.index = pd.to_datetime(df.index)
                df.index = df.index.tz_localize(None) if getattr(df.index, "tz", None) else df.index
                return df, ticker
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"抓不到 {stock_id} 的股價資料。最後錯誤：{last_error}")


def find_pivots(price_df: pd.DataFrame, window: int = 4) -> Tuple[List[int], List[int]]:
    highs = price_df["High"].values
    lows = price_df["Low"].values
    high_idx: List[int] = []
    low_idx: List[int] = []

    for i in range(window, len(price_df) - window):
        h_segment = highs[i - window : i + window + 1]
        l_segment = lows[i - window : i + window + 1]
        if highs[i] == np.max(h_segment):
            high_idx.append(i)
        if lows[i] == np.min(l_segment):
            low_idx.append(i)
    return high_idx, low_idx


def cluster_price_levels(levels: Sequence[float], tolerance: float = 0.018) -> List[Tuple[float, int]]:
    if not levels:
        return []
    levels = sorted(float(x) for x in levels if pd.notna(x))
    clusters: List[List[float]] = []
    for lv in levels:
        if not clusters:
            clusters.append([lv])
            continue
        center = np.mean(clusters[-1])
        if abs(lv - center) / max(center, 1e-9) <= tolerance:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return [(float(np.mean(c)), len(c)) for c in clusters]


def detect_technical_summary(price_df: pd.DataFrame) -> TechnicalSummary:
    high_idx, low_idx = find_pivots(price_df, window=4)
    close_now = float(price_df["Close"].iloc[-1])

    high_levels = [float(price_df["High"].iloc[i]) for i in high_idx]
    low_levels = [float(price_df["Low"].iloc[i]) for i in low_idx]

    high_clusters = cluster_price_levels(high_levels)
    low_clusters = cluster_price_levels(low_levels)

    resistance_candidates = [(lv, cnt) for lv, cnt in high_clusters if lv >= close_now * 0.98]
    support_candidates = [(lv, cnt) for lv, cnt in low_clusters if lv <= close_now * 1.02]

    resistance_level = None
    if resistance_candidates:
        resistance_level = sorted(resistance_candidates, key=lambda x: (x[1], -abs(x[0] - close_now)), reverse=True)[0][0]
    elif high_levels:
        resistance_level = max([lv for lv in high_levels if lv >= close_now] or high_levels)

    support_level = None
    if support_candidates:
        support_level = sorted(support_candidates, key=lambda x: (x[1], -abs(x[0] - close_now)), reverse=True)[0][0]
    elif low_levels:
        support_level = min([lv for lv in low_levels if lv <= close_now] or low_levels)

    # 大量支撐區：優先找上漲且大量的 K 棒
    recent = price_df.copy()
    vol_threshold = recent["Volume"].quantile(0.80)
    bullish = recent[(recent["Close"] >= recent["Open"]) & (recent["Volume"] >= vol_threshold)]
    if bullish.empty:
        bullish = recent[recent["Volume"] >= vol_threshold]
    volume_support_zone = None
    if not bullish.empty:
        row = bullish.sort_values("Volume", ascending=False).iloc[0]
        zone_low = float(min(row["Open"], row["Close"], row["Low"]))
        zone_high = float(max(row["Open"], row["Close"]))
        pad = max(close_now * 0.004, 0.05)
        volume_support_zone = (zone_low - pad, zone_high + pad)

    # 三角收斂：抓最近 45 根內 pivot high / low 做線性擬合
    start_idx = max(0, len(price_df) - 45)
    high_recent = [i for i in high_idx if i >= start_idx]
    low_recent = [i for i in low_idx if i >= start_idx]
    triangle_lines = None
    pattern_name = "區間整理"
    notes: List[str] = []

    if len(high_recent) >= 2 and len(low_recent) >= 2:
        xh = np.array(high_recent[-4:], dtype=float)
        yh = np.array([price_df["High"].iloc[i] for i in high_recent[-4:]], dtype=float)
        xl = np.array(low_recent[-4:], dtype=float)
        yl = np.array([price_df["Low"].iloc[i] for i in low_recent[-4:]], dtype=float)

        high_slope, high_intercept = np.polyfit(xh, yh, 1)
        low_slope, low_intercept = np.polyfit(xl, yl, 1)

        first_range = (high_slope * start_idx + high_intercept) - (low_slope * start_idx + low_intercept)
        last_range = (high_slope * (len(price_df) - 1) + high_intercept) - (low_slope * (len(price_df) - 1) + low_intercept)

        if high_slope < 0 and low_slope > 0 and last_range > 0 and last_range < first_range * 0.85:
            triangle_lines = {
                "upper": (float(high_slope), float(high_intercept)),
                "lower": (float(low_slope), float(low_intercept)),
            }
            pattern_name = "疑似三角收斂"
            notes.append("高點逐步下移、低點逐步墊高，價格區間有收斂跡象。")

    if resistance_level:
        notes.append(f"上方壓力約落在 {resistance_level:.2f} 附近。")
    if support_level:
        notes.append(f"下方支撐約落在 {support_level:.2f} 附近。")
    if volume_support_zone:
        notes.append(f"大量支撐區約落在 {volume_support_zone[0]:.2f}～{volume_support_zone[1]:.2f}。")

    if not notes:
        notes.append("目前型態訊號不明顯，建議以量價是否突破關鍵區間作為後續觀察。")

    return TechnicalSummary(
        support_level=support_level,
        resistance_level=resistance_level,
        volume_support_zone=volume_support_zone,
        triangle_lines=triangle_lines,
        pattern_name=pattern_name,
        notes=notes,
    )


# ============================================================
# 7. 新聞 RSS
# ============================================================

def fetch_news(stock_id: str, stock_name: str = "", limit: int = 3) -> List[NewsItem]:
    query = f"{stock_id} {stock_name} 台股 股票".strip()
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query + ' when:14d')}"
        "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception:
        return []

    items: List[NewsItem] = []
    for entry in feed.entries[: limit * 2]:
        title = re.sub(r"\s+-\s+[^-]+$", "", entry.get("title", "")).strip()
        source = ""
        if hasattr(entry, "source") and isinstance(entry.source, dict):
            source = entry.source.get("title", "")
        if not source:
            m = re.search(r" - ([^-]+)$", entry.get("title", ""))
            source = m.group(1) if m else "Google News"

        published = entry.get("published", "")
        try:
            published_dt = pd.to_datetime(published, errors="coerce")
            published_text = published_dt.strftime("%Y/%m/%d") if pd.notna(published_dt) else ""
        except Exception:
            published_text = ""

        if title:
            items.append(
                NewsItem(
                    title=title,
                    source=source,
                    published=published_text,
                    link=entry.get("link", ""),
                )
            )
        if len(items) >= limit:
            break
    return items


# ============================================================
# 8. 繪圖：K 線與週報長圖
# ============================================================

def plot_candles(ax: plt.Axes, price_df: pd.DataFrame) -> None:
    dates = mdates.date2num(price_df.index.to_pydatetime())
    width = 0.62

    for x, (_, row) in zip(dates, price_df.iterrows()):
        open_p, high_p, low_p, close_p = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        up = close_p >= open_p
        color = "#d62728" if up else "#2ca02c"  # 台股常用：紅漲綠跌
        ax.vlines(x, low_p, high_p, color=color, linewidth=1.0, alpha=0.95)
        body_low = min(open_p, close_p)
        body_height = max(abs(close_p - open_p), 0.02)
        rect = Rectangle((x - width / 2, body_low), width, body_height, facecolor=color, edgecolor=color, linewidth=0.8, alpha=0.95)
        ax.add_patch(rect)

    # 均線
    for ma, color, lw in [(5, "#444444", 1.0), (10, "#8c564b", 1.0), (20, "#1f77b4", 1.1), (60, "#9467bd", 1.1)]:
        if len(price_df) >= ma:
            ax.plot(dates, price_df["Close"].rolling(ma).mean(), label=f"MA{ma}", linewidth=lw, color=color, alpha=0.9)

    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.35)
    ax.legend(loc="upper left", ncol=4, fontsize=8, frameon=False)


def plot_volume(ax: plt.Axes, price_df: pd.DataFrame) -> None:
    dates = mdates.date2num(price_df.index.to_pydatetime())
    colors = ["#d62728" if c >= o else "#2ca02c" for o, c in zip(price_df["Open"], price_df["Close"])]
    ax.bar(dates, price_df["Volume"] / 1000, color=colors, alpha=0.55, width=0.62)
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.25)
    ax.set_ylabel("量(千張)", fontsize=9)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))


def add_technical_annotations(ax: plt.Axes, price_df: pd.DataFrame, tech: TechnicalSummary) -> None:
    dates = mdates.date2num(price_df.index.to_pydatetime())
    x0, x1 = dates[0], dates[-1]

    if tech.volume_support_zone:
        low, high = tech.volume_support_zone
        ax.axhspan(low, high, color="#ffcc80", alpha=0.25)
        ax.text(x0, high, f"大量支撐區 {low:.2f}~{high:.2f}", fontsize=9, va="bottom", color="#8a4b00")

    if tech.support_level:
        ax.axhline(tech.support_level, color="#2ca02c", linestyle="--", linewidth=1.2, alpha=0.85)
        ax.text(x1, tech.support_level, f" 支撐 {tech.support_level:.2f}", fontsize=9, va="bottom", ha="left", color="#1b7f1b")

    if tech.resistance_level:
        ax.axhline(tech.resistance_level, color="#d62728", linestyle="--", linewidth=1.2, alpha=0.85)
        ax.text(x1, tech.resistance_level, f" 壓力 {tech.resistance_level:.2f}", fontsize=9, va="bottom", ha="left", color="#a91d1d")

    if tech.triangle_lines:
        idx = np.arange(len(price_df))
        upper_slope, upper_intercept = tech.triangle_lines["upper"]
        lower_slope, lower_intercept = tech.triangle_lines["lower"]
        y_upper = upper_slope * idx + upper_intercept
        y_lower = lower_slope * idx + lower_intercept
        ax.plot(dates, y_upper, color="#111111", linewidth=1.5, linestyle="-.", alpha=0.95)
        ax.plot(dates, y_lower, color="#111111", linewidth=1.5, linestyle="-.", alpha=0.95)
        ax.text(dates[int(len(dates) * 0.60)], y_upper[int(len(dates) * 0.60)], "疑似三角收斂", fontsize=9, color="#111111", va="bottom")


def draw_section_box(ax: plt.Axes, title: str) -> None:
    ax.set_axis_off()
    ax.add_patch(
        patches.FancyBboxPatch(
            (0.015, 0.02), 0.97, 0.94,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            linewidth=1.0,
            edgecolor="#d0d0d0",
            facecolor="#ffffff",
            transform=ax.transAxes,
            zorder=0,
        )
    )
    ax.text(0.04, 0.88, title, transform=ax.transAxes, fontsize=15, weight="bold", color="#222222", va="top")


def build_summary_bullets(warrant: WarrantSummary, tech: TechnicalSummary) -> List[str]:
    bullets = []
    if warrant.recent_rows > 0:
        bullets.append(
            f"近 14 日共追蹤到 {warrant.recent_rows} 筆相關權證紀錄，其中 {warrant.pending_rows} 筆判定為疑似未出清。"
        )
        if warrant.pending_buy_amount > 0:
            bullets.append(f"疑似未出清買進金額約 {money_tw(warrant.pending_buy_amount)}，主要分點數約 {len(warrant.brokers)} 個。")
    else:
        bullets.append("Google Sheet 內未找到近 14 日明確相關權證紀錄，需確認欄位名稱或資料是否更新。")

    bullets.append(f"技術型態目前判讀為「{tech.pattern_name}」，重點觀察支撐、壓力與量能變化。")
    if tech.resistance_level and tech.support_level:
        bullets.append(f"關鍵區間：支撐約 {tech.support_level:.2f}，壓力約 {tech.resistance_level:.2f}。")
    return bullets[:4]


def render_report(
    stock_id: str,
    stock_name: str,
    ticker: str,
    price_df: pd.DataFrame,
    warrant: WarrantSummary,
    tech: TechnicalSummary,
    news_items: List[NewsItem],
    output_dir: str,
) -> Path:
    setup_chinese_font()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y/%m/%d")
    display_name = f"{stock_id} {stock_name}".strip()
    if not stock_name:
        display_name = stock_id

    file_name = f"{stock_id}_warrant_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    save_path = output_path / file_name

    fig = plt.figure(figsize=(14, 22), dpi=150, facecolor="#f5f6f8")
    gs = fig.add_gridspec(
        nrows=10,
        ncols=1,
        height_ratios=[0.75, 1.20, 4.4, 0.18, 1.45, 2.15, 1.65, 1.25, 0.75, 0.12],
        hspace=0.28,
    )

    # Header
    ax_header = fig.add_subplot(gs[0])
    ax_header.set_axis_off()
    ax_header.add_patch(Rectangle((0, 0), 1, 1, transform=ax_header.transAxes, color="#111827"))
    ax_header.text(0.03, 0.62, f"{display_name}｜大戶權證追蹤圖像週報", fontsize=24, weight="bold", color="white", va="center")
    ax_header.text(0.03, 0.22, f"70 根日 K｜近 14 日權證籌碼｜產生日期：{today}｜資料來源：Google Sheet / Yahoo Finance / Google News RSS", fontsize=10, color="#d1d5db", va="center")
    ax_header.text(0.965, 0.56, "股市艾斯", fontsize=18, weight="bold", color="#f9fafb", va="center", ha="right")
    ax_header.text(0.965, 0.25, "資訊分享非投資建議", fontsize=9, color="#d1d5db", va="center", ha="right")

    # Summary box
    ax_summary = fig.add_subplot(gs[1])
    draw_section_box(ax_summary, "本週觀察重點")
    bullets = build_summary_bullets(warrant, tech)
    y = 0.67
    for b in bullets:
        ax_summary.text(0.06, y, f"• {wrap_zh_text(b, 58)}", transform=ax_summary.transAxes, fontsize=12, color="#222222", va="top")
        y -= 0.20

    # Chart block: nested grid
    chart_gs = gs[2].subgridspec(2, 1, height_ratios=[3.3, 1.0], hspace=0.05)
    ax_price = fig.add_subplot(chart_gs[0])
    ax_vol = fig.add_subplot(chart_gs[1], sharex=ax_price)
    ax_price.set_title(f"{display_name} 近 70 根日 K 與型態標示（{ticker}）", fontsize=16, weight="bold", loc="left", pad=10)
    plot_candles(ax_price, price_df)
    add_technical_annotations(ax_price, price_df, tech)
    plot_volume(ax_vol, price_df)
    ax_price.set_ylabel("價格", fontsize=10)
    plt.setp(ax_price.get_xticklabels(), visible=False)
    for label in ax_vol.get_xticklabels():
        label.set_rotation(0)
        label.set_fontsize(8)

    # Technical notes
    ax_tech = fig.add_subplot(gs[4])
    draw_section_box(ax_tech, "型態解讀")
    tech_text = "\n".join([f"• {n}" for n in tech.notes[:4]])
    close_now = float(price_df["Close"].iloc[-1])
    prev_close = float(price_df["Close"].iloc[-2]) if len(price_df) >= 2 else close_now
    change_pct = (close_now / prev_close - 1) * 100 if prev_close else 0
    extra = f"最新收盤價 {close_now:.2f}，單日變動 {change_pct:+.2f}%。後續可觀察是否帶量突破壓力，或跌破大量支撐區。"
    ax_tech.text(0.06, 0.68, wrap_zh_text(tech_text, 70), transform=ax_tech.transAxes, fontsize=12, color="#222222", va="top", linespacing=1.5)
    ax_tech.text(0.06, 0.23, wrap_zh_text(extra, 72), transform=ax_tech.transAxes, fontsize=11.5, color="#444444", va="top", linespacing=1.5)

    # Warrant table
    ax_table = fig.add_subplot(gs[5])
    draw_section_box(ax_table, "近 14 日大戶權證追蹤")

    if warrant.top_rows.empty:
        ax_table.text(0.06, 0.58, "目前未讀到可呈現的權證追蹤資料。請確認 Google Sheet 是否有股票代號欄位，以及該代號是否存在。", transform=ax_table.transAxes, fontsize=12, color="#444444", va="center")
    else:
        table_df = warrant.top_rows.copy()
        rows = []
        for _, r in table_df.iterrows():
            dt = r.get("_date")
            dt_text = dt.strftime("%m/%d") if pd.notna(dt) else "-"
            rows.append([
                dt_text,
                short_text(r.get("_broker", "-"), 10),
                short_text(r.get("_warrant_code", "-"), 9),
                short_text(r.get("_warrant_name", "-"), 12),
                money_tw(r.get("_buy_amount", 0)),
                str(r.get("_add", "-")),
                "未出清" if bool(r.get("_pending", False)) else short_text(r.get("_status_text", "-"), 8),
            ])

        col_labels = ["日期", "分點", "權證代號", "權證名稱", "買進金額", "加碼", "狀態"]
        table = ax_table.table(
            cellText=rows,
            colLabels=col_labels,
            cellLoc="center",
            colLoc="center",
            loc="center",
            bbox=[0.04, 0.08, 0.92, 0.68],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9.2)
        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("#dddddd")
            if row == 0:
                cell.set_facecolor("#111827")
                cell.set_text_props(color="white", weight="bold")
            else:
                cell.set_facecolor("#ffffff" if row % 2 else "#f8fafc")
        summary_line = f"疑似未出清：{warrant.pending_rows} 筆｜疑似未出清買進金額：{money_tw(warrant.pending_buy_amount)}｜分點數：約 {len(warrant.brokers)} 個"
        ax_table.text(0.06, 0.84, summary_line, transform=ax_table.transAxes, fontsize=12, color="#222222", weight="bold", va="center")

    # News box
    ax_news = fig.add_subplot(gs[6])
    draw_section_box(ax_news, "近期新聞 / 題材參考")
    if not news_items:
        ax_news.text(0.06, 0.60, "目前未抓到近期新聞 RSS，建議人工補充題材或確認網路連線。", transform=ax_news.transAxes, fontsize=12, color="#444444", va="center")
    else:
        y = 0.68
        for item in news_items[:3]:
            line = f"• [{item.published or '-'}｜{item.source}] {item.title}"
            ax_news.text(0.06, y, wrap_zh_text(line, 72), transform=ax_news.transAxes, fontsize=11.5, color="#222222", va="top", linespacing=1.35)
            y -= 0.23

    # Conclusion
    ax_conclusion = fig.add_subplot(gs[7])
    draw_section_box(ax_conclusion, "結論與後續觀察")
    if warrant.pending_rows > 0:
        conclusion = (
            f"{display_name} 近兩週權證端仍有疑似未出清紀錄，代表短線資金仍值得追蹤。"
            f"技術面目前以「{tech.pattern_name}」觀察，若後續股價帶量突破壓力區，可留意多方延續；"
            "若跌破大量支撐或權證端出現明顯賣超，則需重新評估短線風險。"
        )
    else:
        conclusion = (
            f"{display_name} 目前在 Google Sheet 中未看到明確的近兩週大戶未出清訊號。"
            f"技術面仍可依「{tech.pattern_name}」觀察支撐與壓力，若後續權證端重新出現集中買盤，再納入追蹤名單。"
        )
    ax_conclusion.text(0.06, 0.62, wrap_zh_text(conclusion, 78), transform=ax_conclusion.transAxes, fontsize=12, color="#222222", va="top", linespacing=1.55)
    ax_conclusion.text(0.06, 0.18, "註：本報告由程式依資料規則自動生成，僅供研究與資訊整理，不構成任何投資建議。", transform=ax_conclusion.transAxes, fontsize=9.5, color="#6b7280", va="center")

    # Footer
    ax_footer = fig.add_subplot(gs[8])
    ax_footer.set_axis_off()
    ax_footer.text(0.03, 0.48, "By 股市艾斯出品 - 轉傳請註明", transform=ax_footer.transAxes, fontsize=10, color="#555555", va="center")
    ax_footer.text(0.97, 0.48, "資訊分享非投資建議，投資請自行評估風險", transform=ax_footer.transAxes, fontsize=10, color="#555555", va="center", ha="right")

    fig.savefig(save_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return save_path


# ============================================================
# 9. 主流程
# ============================================================

def generate_report(stock_id: str, sheet_name: str, worksheet_name: Optional[str], output_dir: str) -> Path:
    stock_id = clean_stock_id(stock_id)
    if not stock_id:
        raise ValueError("股票代號不可為空。")

    print(f"[1/6] 讀取 Google Sheet：{sheet_name}")
    raw_df = load_warrant_sheet(sheet_name=sheet_name, worksheet_name=worksheet_name)
    print(f"      讀到 {len(raw_df):,} 筆資料，欄位：{list(raw_df.columns)[:12]}...")

    print(f"[2/6] 整理 {stock_id} 權證追蹤資料")
    warrant = build_warrant_summary(raw_df, stock_id=stock_id)
    print(f"      相關資料 {warrant.total_rows} 筆，近 14 日/近期資料 {warrant.recent_rows} 筆，疑似未出清 {warrant.pending_rows} 筆")

    print("[3/6] 抓取 70 根日 K")
    price_df, ticker = download_stock_history(stock_id=stock_id, bars=70)
    print(f"      使用 ticker：{ticker}，K 棒數：{len(price_df)}")

    if not warrant.stock_name:
        # yfinance 台股名稱不一定穩定，MVP 先不強抓；若 Sheet 有名稱就會顯示
        stock_name = ""
    else:
        stock_name = warrant.stock_name

    print("[4/6] 偵測支撐、壓力、型態")
    tech = detect_technical_summary(price_df)
    print(f"      型態：{tech.pattern_name}，重點：{' / '.join(tech.notes[:3])}")

    print("[5/6] 抓取近 14 日新聞 RSS")
    news_items = fetch_news(stock_id=stock_id, stock_name=stock_name, limit=3)
    print(f"      新聞：{len(news_items)} 則")

    print("[6/6] 輸出圖片週報")
    save_path = render_report(
        stock_id=stock_id,
        stock_name=stock_name,
        ticker=ticker,
        price_df=price_df,
        warrant=warrant,
        tech=tech,
        news_items=news_items,
        output_dir=output_dir,
    )
    print(f"完成：{save_path}")
    return save_path


def main() -> None:
    parser = argparse.ArgumentParser(description="產生大戶權證追蹤圖像週報")
    parser.add_argument("--stock-id", required=True, help="股票代號，例如 2408")
    parser.add_argument("--sheet-name", default=os.getenv("GSHEET_NAME", "權證分點籌碼"), help="Google Sheet 名稱")
    parser.add_argument("--worksheet", default=os.getenv("GSHEET_WORKSHEET", "") or None, help="工作表名稱，空白代表讀取所有 worksheet")
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "output"), help="圖片輸出資料夾")
    args = parser.parse_args()

    generate_report(
        stock_id=args.stock_id,
        sheet_name=args.sheet_name,
        worksheet_name=args.worksheet,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
