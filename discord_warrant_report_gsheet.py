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

NTD_PER_WARRANT_POINT = float(os.getenv("NTD_PER_WARRANT_POINT", "1000"))

# 常見權證發行券商關鍵字，用來從權證名稱中反推標的股名。
WARRANT_ISSUER_TOKENS = [
    "元大", "凱基", "群益", "富邦", "國泰", "永豐", "永豐金", "國票", "中信",
    "台新", "兆豐", "元富", "玉山", "第一金", "新光", "日盛", "康和", "統一",
    "宏遠", "合庫", "犇亞", "華南永昌", "台企銀", "聯邦", "高盛", "瑞銀",
    "摩根大通", "麥格理", "法銀巴黎", "上海商銀"
]


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


def read_gsheet_table(sheet_name: str, needed_cols: list[str] | None = None) -> pd.DataFrame:
    """
    讀取一般工作表：
    - 第 1 列是表頭
    - 後面是資料
    - 自動補齊欄位數
    - 只保留 needed_cols 交集
    - 篩選 TRACKED_BROKERS
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

    if "分點" in df.columns:
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


def normalize_underlying(v) -> str:
    s = normalize_code(v)
    if s.isdigit():
        return str(int(s))
    return s


def money_to_wan(v: float) -> float:
    return round(float(v or 0) / 10000, 1)


def fmt_wan(v: float) -> str:
    return f"{money_to_wan(v):.1f} 萬"


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

    例：
    - "11.67%" -> 11.67
    - 11.67    -> 11.67
    - 0.1167   -> 11.67
    - 1.6508   -> 165.08
    """
    try:
        if v is None:
            return None

        raw = strip_gsheet_text_prefix(v)
        if raw == "" or raw == "-":
            return None

        has_percent = "%" in str(raw)
        pct = safe_float(raw, None)
        if pct is None:
            return None

        if has_percent:
            return pct

        # 無 % 符號且落在 -5~5，視為小數報酬率
        if -5 < pct < 5:
            return pct * 100.0

        # 其他視為已經是百分比
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
        return f"{pct:+.1f}%"
    except Exception:
        return "-"


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
        code = normalize_underlying(underlying)
        if not code:
            return
        name = extract_stock_name_from_warrant_text(text)
        if name:
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

    underlying_code = normalize_underlying(underlying)
    stock_name = get_stock_name_map().get(underlying_code, "")

    buys.append({
        "broker": str(broker).strip(),
        "event": event,
        "underlying": underlying_code,
        "stock_name": stock_name,
        "warrant_code": normalize_code(warrant_code),
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "warrant_list_count": count_warrants_in_text(warrant_name),
        "amount": amount,
        "qty": qty,
        "add_count": 0,
        "sheet": sheet_name,
    })


def append_sell(sells: list[dict], broker: str, status: str, event: str, underlying, warrant_name: str,
                amount: float, qty: int, sheet_name: str, return_pct=None, buy_amount=None):
    if amount < SELL_THRESHOLD and not (status == "出清" and DISPLAY_EXIT_ALWAYS):
        return

    underlying_code = normalize_underlying(underlying)
    stock_name = get_stock_name_map().get(underlying_code, "")

    sells.append({
        "broker": str(broker).strip(),
        "status": status,
        "event": event,
        "underlying": underlying_code,
        "stock_name": stock_name,
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "amount": amount,                         # 賣出 / 減碼 / 出清金額
        "buy_amount": safe_float(buy_amount, 0),  # 對應買進金額，用於同標的合計報酬率
        "qty": qty,
        "return_pct": normalize_return_pct(return_pct),
        "sheet": sheet_name,
    })


def collect_broker_underlying_add_count_map(target: date, lookback_days: int = ADD_COUNT_LOOKBACK_TRADING_DAYS) -> dict[tuple[str, str], int]:
    """
    計算「同一分點 + 同一標的」在近 N 個有效交易日內，
    出現達買超門檻事件的不同日期次數。

    顯示規則：
    - 第 1 次加碼：圖卡不顯示任何標籤
    - 第 2 次以上：圖卡顯示「第N次加碼」

    注意：
    同一天同一分點同一標的即使同時出現在 A/B/C/D，仍只算 1 次。
    這裡的 lookback_days 專門給第幾次加碼使用，不會影響原本近一個月 TOP15 圖。
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

        broker = str(row.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            return

        underlying = normalize_underlying(row.get("標的股"))
        if not underlying:
            return

        if safe_float(amount) < BUY_THRESHOLD:
            return

        counter[(broker, underlying)].add(event_date)

    # A：單檔權證大買
    try:
        A = read_gsheet_table(SHEET_A, ["分點", "標的股", "買進日", "買進金額"])
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
            df = read_gsheet_table(sheet_name, ["分點", "標的股", date_col, "買超金額"])
        except Exception:
            continue

        for _, r in df.iterrows():
            add_count_event(r, parse_date_value(r.get(date_col)), r.get("買超金額"))

    return {key: len(days) for key, days in counter.items()}


def extract_actions_from_gsheet(target: date) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []

    # A：單檔權證大買
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

        if parse_date_value(r.get("減碼日")) == target:
            # A 表沒有「減碼賣出金額」欄，用 均價 × 張數 × 1000 估算。
            amount = safe_float(r.get("減碼均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
            append_sell(
                sells, broker, "減碼", event, r.get("標的股"), r.get("權證名稱"),
                amount, safe_int(r.get("買進張數")), SHEET_A, r.get("減碼獲利%"), r.get("買進金額")
            )

        if parse_date_value(r.get("出清日")) == target:
            amount = safe_float(r.get("出清均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
            append_sell(
                sells, broker, "出清", event, r.get("標的股"), r.get("權證名稱"),
                amount, safe_int(r.get("買進張數")), SHEET_A, r.get("出清獲利%"), r.get("買進金額")
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

            if parse_date_value(r.get("減碼日")) == target:
                append_sell(
                    sells, broker, "減碼", event, r.get("標的股"), r.get("權證清單"),
                    safe_float(r.get("減碼賣出金額")), safe_int(r.get("買超張數")), sheet_name, r.get("減碼獲利%"), r.get("買超金額")
                )

            if parse_date_value(r.get("出清日")) == target:
                append_sell(
                    sells, broker, "出清", event, r.get("標的股"), r.get("權證清單"),
                    safe_float(r.get("出清賣出金額")), safe_int(r.get("買超張數")), sheet_name, r.get("出清獲利%"), r.get("買超金額")
                )

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
        # 你希望同一天 A/B/C/D 若有同一標的賣方動作，直接加總概算。
        # 因此優先使用：
        #   報酬率 = (合計賣出金額 - 合計買進金額) / 合計買進金額
        # 這樣能和「買進金額都有，直接加在一起算」的邏輯一致。
        total_buy_amount = sum(safe_float(i.get("buy_amount"), 0) for i in items)

        if kind == "sell" and total_buy_amount > 0:
            return_pct = ((amount - total_buy_amount) / total_buy_amount) * 100.0
        else:
            # fallback：若沒有買進金額，才用個別報酬率反推成本。
            valid_returns = [
                (i.get("return_pct"), i.get("amount", 0))
                for i in items
                if i.get("return_pct") is not None and float(i.get("amount", 0) or 0) > 0
            ]

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
                    content = f"{add_count_label}｜{content}"

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
    """
    buys = compress_actions(buys_raw, "buy")
    sells = compress_actions(sells_raw, "sell")

    buy_total = sum(x["amount"] for x in buys)
    sell_total = sum(x["amount"] for x in sells)
    net = buy_total - sell_total

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
    kpis = [
        ("今日買超", f"{sum(x['count'] for x in buys)} 筆", fmt_wan(buy_total), RED, PINK, "↗"),
        ("今日賣超", f"{sum(x['count'] for x in sells)} 筆", fmt_wan(sell_total), GREEN, MINT, "−"),
        ("淨買超", "", fmt_wan(net), RED if net >= 0 else GREEN, PINK if net >= 0 else MINT, "◎"),
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
                    display_val = fit(val, max(5, int(w * 6.0)))

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
    - A_單檔大買：買進日 / 買進金額
    - B_同標的單日合計：事件日 / 買超金額
    - C_同標的3日累積：結束日 / 買超金額
    - D_近10日累積淨買進：結束日 / 買超金額

    合併方式：
    - 同標的股合併
    - 淨累積買超 = 合計買超 - 合計賣方金額
    - 僅保留淨累積買超 > 0 的標的
    - 依淨累積買超由大到小排序
    """
    trading_dates = collect_recent_buy_trading_dates(target, lookback_days)
    date_set = set(trading_dates)

    if not trading_dates:
        return [], []

    agg = {}

    def ensure_item(underlying):
        code = normalize_underlying(underlying)
        if not code:
            return None, None

        stock_name = get_stock_name_map().get(code, "")
        label = f"{code} {stock_name}".strip()

        if code not in agg:
            agg[code] = {
                "underlying": code,
                "stock_name": stock_name,
                "target": label,
                "amount": 0.0,       # 合計買超
                "net_amount": 0.0,   # 淨累積買超 = 買超 - 賣方
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

        code, item = ensure_item(row.get("標的股"))
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

    def add_sell_row(row, event_date, amount):
        if not event_date or event_date not in date_set:
            return

        broker = str(row.get("分點", "")).strip()
        if broker not in TRACKED_BROKERS:
            return

        amount = safe_float(amount)
        if amount <= 0:
            return

        code = normalize_underlying(row.get("標的股"))
        if not code or code not in agg:
            return

        agg[code]["net_amount"] -= amount
        agg[code]["broker_net_amounts"][broker] -= amount

    # A：買超與賣方
    try:
        A = read_gsheet_table(
            SHEET_A,
            ["分點", "標的股", "買進日", "買進金額", "買進張數",
             "減碼日", "減碼均價", "出清日", "出清均價", "權證名稱"]
        )

        for _, r in A.iterrows():
            add_buy_row(SHEET_A, "A", r, parse_date_value(r.get("買進日")), r.get("買進金額"))

        for _, r in A.iterrows():
            d = get_sell_event_date(r, SHEET_A, "減碼")
            if d and d in date_set:
                sell_amount = safe_float(r.get("減碼均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
                add_sell_row(r, d, sell_amount)

            d = get_sell_event_date(r, SHEET_A, "出清")
            if d and d in date_set:
                sell_amount = safe_float(r.get("出清均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
                add_sell_row(r, d, sell_amount)
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
                ["分點", "標的股", date_col, "買超金額",
                 "減碼日", "減碼賣出金額", "出清日", "出清賣出金額", "權證清單"]
            )
        except Exception:
            continue

        for _, r in df.iterrows():
            add_buy_row(sheet_name, event_code, r, parse_date_value(r.get(date_col)), r.get("買超金額"))

        for _, r in df.iterrows():
            d = get_sell_event_date(r, sheet_name, "減碼")
            if d and d in date_set:
                add_sell_row(r, d, r.get("減碼賣出金額"))

            d = get_sell_event_date(r, sheet_name, "出清")
            if d and d in date_set:
                add_sell_row(r, d, r.get("出清賣出金額"))

    rows = []
    for item in agg.values():
        # 共識淨買超榜只保留目前仍為正淨買超的標的
        if item["net_amount"] <= 0:
            continue

        top_broker = ""
        top_amount = 0.0
        if item["broker_amounts"]:
            top_broker, top_amount = max(item["broker_amounts"].items(), key=lambda kv: kv[1])

        participant_brokers = [
            (broker, amount)
            for broker, amount in item["broker_net_amounts"].items()
            if amount > 0
        ]
        participant_brokers.sort(key=lambda kv: kv[1], reverse=True)

        rows.append({
            "target": item["target"],
            "amount": item["amount"],
            "net_amount": item["net_amount"],
            "count": item["count"],
            "broker_count": len(participant_brokers) if participant_brokers else len(item["brokers"]),
            "brokers": sorted(item["brokers"]),
            "events": "/".join(sorted(item["events"])),
            "top_broker": top_broker,
            "top_broker_amount": top_amount,
            "participant_brokers": participant_brokers,
            "first_date": item["first_date"],
            "last_date": item["last_date"],
        })

    rows.sort(key=lambda x: (x["net_amount"], x["amount"], x["broker_count"]), reverse=True)
    return rows[:15], trading_dates


def draw_consensus_buy_image(target: date, output_path: Path, lookback_days: int = LOOKBACK_TRADING_DAYS):
    """
    第二張圖：近一個月交易日｜五大分點共識淨買超 TOP15
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

    def fmt_participant_brokers(row, limit=5):
        """
        顯示所有參與分點的淨累積買超金額。
        多數情況只有 1 家；若有多家共識買超，會完整列出。
        """
        items = row.get("participant_brokers", [])
        if not items:
            top_broker = row.get("top_broker", "")
            top_amount = row.get("top_broker_amount", 0)
            return fit(f"{top_broker} {fmt_wan(top_amount)}", 24)

        shown = items[:limit]
        text_items = [f"{broker} {fmt_wan(amount)}" for broker, amount in shown]
        if len(items) > limit:
            text_items.append(f"等{len(items)}家")
        return fit("、".join(text_items), 30)

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
    text(margin_x + 0.15, y, "近一個月交易日｜五大分點共識淨買超 TOP15", 28, NAVY, BOLD)
    y -= 0.48
    text(margin_x + 0.18, y, f"追蹤分點：{'、'.join(TRACKED_BROKERS)}", 14, NAVY2, BOLD)
    y -= 0.30
    text(margin_x + 0.18, y, f"統計期間：近 {len(trading_dates)} 個有效交易日｜{period_text}　｜　同標的合併計算　｜　單位：萬元", 13, TEXT, BOLD)

    # 小型事件註解列：取代原本三個大 KPI 方框，避免版面過重
    y -= 0.28
    legend_y = y - legend_h
    rounded(margin_x, legend_y, content_w, legend_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)

    text(margin_x + 0.25, legend_y + legend_h / 2, f"TOP15淨累積買超：{fmt_wan(total_net_amount)}", 13.5, RED if total_net_amount >= 0 else GREEN, BOLD)

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
    text(margin_x + 0.30, table_top - section_title_h / 2, "共識淨買超 TOP15", 19, WHITE, BOLD)

    headers = ["排名", "標的", "淨累積買超", "分點數", "事件", "參與分點"]
    col_w = [0.70, 2.25, 2.05, 1.00, 1.15, 5.05]

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
        text(margin_x + content_w / 2, data_y - row_h / 2, "近一個月交易日沒有淨累積買超為正的標的", 13, MUTED, BOLD, ha="center")
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
                fmt_participant_brokers(r),
            ]

            colors = [TEXT, TEXT, net_color, TEXT, NAVY2, TEXT]
            aligns = ["center", "left", "right", "center", "center", "left"]
            bolds = [True, True, True, True, True, True]

            x = margin_x
            for val, w, c, a, is_bold in zip(values, col_w, colors, aligns, bolds):
                px = x + (w / 2 if a == "center" else 0.12 if a == "left" else w - 0.12)
                text(px, ry + row_h / 2, val, 14, c, BOLD if is_bold else FONT, ha=a)
                x += w

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
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = infer_latest_date_from_gsheet()

    output_path = Path(args.output)
    consensus_output_path = output_path.parent / "近一個月交易日_五大分點共識淨買超TOP15.png"

    history = read_history_stats_from_gsheet()
    buys, sells = extract_actions_from_gsheet(target)

    print(
        f"Google Sheet：{GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME}\n"
        f"目標日期：{target:%Y-%m-%d}\n"
        f"買超原始筆數：{len(buys)}，賣方提醒原始筆數：{len(sells)}\n"
        f"買超門檻：{BUY_THRESHOLD:.0f}，賣方門檻：{SELL_THRESHOLD:.0f}\n"
        f"加碼次數計算範圍：近 {ADD_COUNT_LOOKBACK_TRADING_DAYS} 個有效交易日\n"
        f"輸出圖檔1：{output_path}\n"
        f"輸出圖檔2：{consensus_output_path}"
    )

    draw_report_image(target, buys, sells, history, output_path)
    draw_consensus_buy_image(target, consensus_output_path, LOOKBACK_TRADING_DAYS)

    if args.no_discord:
        print("已設定 --no-discord，只輸出圖片，不發送 Discord。")
        return

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "")
    if not webhook_url:
        raise RuntimeError("找不到 DISCORD_WEBHOOK_URL_TEST，請先在 GitHub Secrets 設定。")

    send_to_discord(webhook_url, output_path, target)
    send_to_discord(webhook_url, consensus_output_path, target)

    print("Discord 已發送 2 張圖片。")


if __name__ == "__main__":
    main()
