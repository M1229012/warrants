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


def normalize_return_pct(v):
    """
    將 Google Sheet 的權證報酬率統一轉成「百分比數值」。

    修正重點：
    - Google Sheet 有時會把 +28.1% 讀成 "28.1%"，這種已經是百分比，直接用 28.1。
    - 有時會把 +165.08% 存成 1.6508，這是小數報酬率，要乘上 100 變成 165.08。
    - 若原始值已經大於 5，例如 28.1、-10.2，視為已經是百分比，不再乘 100。
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

        # 有 % 符號：代表 Google Sheet 已經格式化成百分比字串
        if has_percent:
            return pct

        # 沒有 % 符號：若數值在 -5~5 之間，通常是小數報酬率，例如 1.6508 = +165.08%
        if -5 < pct < 5:
            return pct * 100.0

        # 其他情況視為已經是百分比，例如 28.1 = +28.1%
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
               amount: float, qty: int, sheet_name: str):
    if amount < BUY_THRESHOLD:
        return

    underlying_code = normalize_underlying(underlying)
    stock_name = get_stock_name_map().get(underlying_code, "")

    buys.append({
        "broker": str(broker).strip(),
        "event": event,
        "underlying": underlying_code,
        "stock_name": stock_name,
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "amount": amount,
        "qty": qty,
        "sheet": sheet_name,
    })


def append_sell(sells: list[dict], broker: str, status: str, event: str, underlying, warrant_name: str,
                amount: float, qty: int, sheet_name: str, return_pct=None):
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
        "amount": amount,
        "qty": qty,
        "return_pct": normalize_return_pct(return_pct),
        "sheet": sheet_name,
    })


def extract_actions_from_gsheet(target: date) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []

    # A：單檔權證大買
    a_cols = [
        "事件類型", "分點", "權證名稱", "標的股", "買進日", "買進張數", "買進金額",
        "減碼日", "減碼均價", "減碼獲利%", "出清日", "出清均價", "出清獲利%"
    ]
    A = read_gsheet_table(SHEET_A, a_cols)

    for _, r in A.iterrows():
        broker = r.get("分點", "")
        event = "A"

        if parse_date_value(r.get("買進日")) == target:
            append_buy(
                buys, broker, event, r.get("標的股"), r.get("權證名稱"),
                safe_float(r.get("買進金額")), safe_int(r.get("買進張數")), SHEET_A
            )

        if parse_date_value(r.get("減碼日")) == target:
            # A 表沒有「減碼賣出金額」欄，用 均價 × 張數 × 1000 估算。
            amount = safe_float(r.get("減碼均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
            append_sell(
                sells, broker, "減碼", event, r.get("標的股"), r.get("權證名稱"),
                amount, safe_int(r.get("買進張數")), SHEET_A, r.get("減碼獲利%")
            )

        if parse_date_value(r.get("出清日")) == target:
            amount = safe_float(r.get("出清均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
            append_sell(
                sells, broker, "出清", event, r.get("標的股"), r.get("權證名稱"),
                amount, safe_int(r.get("買進張數")), SHEET_A, r.get("出清獲利%")
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
                    safe_float(r.get("減碼賣出金額")), safe_int(r.get("買超張數")), sheet_name, r.get("減碼獲利%")
                )

            if parse_date_value(r.get("出清日")) == target:
                append_sell(
                    sells, broker, "出清", event, r.get("標的股"), r.get("權證清單"),
                    safe_float(r.get("出清賣出金額")), safe_int(r.get("買超張數")), sheet_name, r.get("出清獲利%")
                )

    return buys, sells


def compress_actions(actions: list[dict], kind: str) -> list[dict]:
    """
    同一分點、同一事件、同一標的若有多筆權證，合併成標的顯示。
    單筆則顯示權證名稱。
    """
    groups = defaultdict(list)

    for a in actions:
        key = (a["broker"], a.get("status", ""), a["event"], a["underlying"] or a["warrant"])
        groups[key].append(a)

    result = []
    for (broker, status, event, key_name), items in groups.items():
        amount = sum(i["amount"] for i in items)
        qty = sum(i.get("qty", 0) for i in items)
        warrant_count = len(items)

        # 權證報酬率彙總：
        # 單筆直接使用 Google Sheet 內的「減碼獲利% / 出清獲利%」。
        # 多筆同標的權證合併時，不用標的股報酬率，也不是單純平均；
        # 以每筆權證的賣出金額與權證報酬率反推成本，再計算整體累積報酬率。
        #
        # 若 r = 權證報酬率，例如 +20% = 20
        # 賣出金額 = 成本 * (1 + r/100)
        # 成本 = 賣出金額 / (1 + r/100)
        # 合併報酬率 = (總賣出金額 - 總成本) / 總成本 * 100
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

                # 避免 -100% 附近造成除以 0
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

        if warrant_count >= 2 and underlying:
            display_target = target_label if target_label else f"{underlying}"
            content = f"{warrant_count} 檔權證"
        else:
            display_target = target_label if target_label else (underlying if underlying else items[0].get("warrant", ""))
            content = items[0].get("warrant", "") or f"{warrant_count} 檔權證"

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

    active_rows = max(1, math.ceil(len(active_brokers) / 3)) if active_brokers else 0
    broker_card_h = 1.55
    broker_area_h = active_rows * broker_card_h + max(0, active_rows - 1) * gap

    inactive_h = 0.58 if inactive_brokers else 0.0

    section_title_h = 0.55
    header_h = 0.42
    row_h = 0.48

    buy_rows = buys
    sell_rows = sells

    buy_table_h = section_title_h + header_h + max(1, len(buy_rows)) * row_h
    sell_table_h = 0.0
    if sell_rows:
        sell_table_h = section_title_h + header_h + len(sell_rows) * row_h

    event_legend_h = 0.82
    footer_h = 0.48

    fig_h = (
        top_h
        + kpi_h
        + gap
        + broker_area_h
        + (gap + inactive_h if inactive_brokers else 0)
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
    # Broker cards：依 active_brokers 自動換行
    # ─────────────────────────────────────────────
    if active_brokers:
        cards_per_row = 3
        card_gap = 0.25
        card_w = (content_w - (cards_per_row - 1) * card_gap) / cards_per_row
        for idx, b in enumerate(active_brokers):
            row = idx // cards_per_row
            col = idx % cards_per_row
            x = margin_x + col * (card_w + card_gap)
            cy = y - row * (broker_card_h + gap) - broker_card_h
            s = broker_summary[b]
            rounded(x, cy, card_w, broker_card_h, fc=WHITE, ec=NAVY2, lw=1.1, r=0.08)
            rect(x, cy + broker_card_h - 0.42, card_w, 0.42, fc=NAVY)
            text(x + card_w / 2, cy + broker_card_h - 0.21, b, 15, WHITE, BOLD, ha="center")
            text(x + card_w / 2, cy + broker_card_h - 0.62, f"平均持有 {s['avg_hold_days']:.1f} 天", 11, TEXT, FONT, ha="center")
            ax.plot([x + 0.18, x + card_w - 0.18], [cy + 0.80, cy + 0.80], color=BORDER, linewidth=0.8)
            text(x + 0.18, cy + 0.58, "買超", 12, RED, BOLD)
            text(x + 0.88, cy + 0.58, f"{s['buy_count']}筆 / {fmt_wan(s['buy_amount'])}", 13, RED, BOLD)
            text(x + 0.18, cy + 0.28, "賣方", 12, GREEN, BOLD)
            text(x + 0.88, cy + 0.28, f"{s['sell_count']}筆 / {fmt_wan(s['sell_amount'])}", 12, GREEN, BOLD)

    y -= broker_area_h

    # 無動作分點縮小顯示
    if inactive_brokers:
        y -= gap
        rounded(margin_x, y - inactive_h, content_w, inactive_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
        text(margin_x + 0.25, y - inactive_h / 2, "今日無動作分點", 13, NAVY, BOLD)
        text(margin_x + 2.35, y - inactive_h / 2, "、".join(inactive_brokers), 13, TEXT, BOLD)
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
                for val, w, c, a, is_bold in zip(values, col_widths, colors, aligns, bolds):
                    px = x + (w / 2 if a == "center" else 0.12 if a == "left" else w - 0.12)
                    text(px, ry + row_h / 2, fit(val, max(5, int(w * 6.0))), 14, c, BOLD if is_bold else FONT, ha=a)
                    x += w
        return y_top - table_h

    # Buy table
    buy_headers = ["排名", "分點", "事件", "標的 / 權證", "內容", "買超金額"]
    buy_col_w = [0.75, 2.25, 0.90, 2.55, 3.05, 2.50]

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
        sell_col_w = [2.05, 1.05, 2.75, 2.55, 1.25, 2.35]

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

    # Event legend
    y -= gap
    rounded(margin_x, y - event_legend_h, content_w, event_legend_h, fc=WHITE, ec=BORDER, lw=1.0, r=0.08)
    text(margin_x + 0.25, y - 0.25, "事件代號說明", 14, NAVY, BOLD)

    legend_x = margin_x + 1.72
    legend_y1 = y - 0.25
    legend_y2 = y - 0.58
    per_w = 5.10

    for idx, (code_name, desc) in enumerate(EVENT_LEGEND_ITEMS):
        lx = legend_x + (idx % 2) * per_w
        ly = legend_y1 if idx < 2 else legend_y2
        # 事件代號屬於分類說明，不使用紅/綠，避免被誤解成買方或賣方訊號
        badge_color = "#334155"

        rounded(lx, ly - 0.13, 0.34, 0.26, fc=badge_color, ec=badge_color, lw=0.8, r=0.08)
        text(lx + 0.17, ly, code_name, 10.5, WHITE, BOLD, ha="center")
        text(lx + 0.44, ly, desc, 11.5, TEXT, FONT)

    # footer
    y -= event_legend_h
    text(fig_w / 2, 0.18, "本圖為籌碼追蹤整理，不構成投資建議。", 11, MUTED, FONT, ha="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, format="png", dpi=130, facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)


# ══════════════════════════════════════════════════════════════════════
# Discord
# ══════════════════════════════════════════════════════════════════════

def send_to_discord(webhook_url: str, image_path: Path, target: date):
    content = f"📊 {target:%Y/%m/%d} 精選分點買賣超追蹤"
    with image_path.open("rb") as f:
        files = {"file": (image_path.name, f, "image/png")}
        data = {"content": content}
        resp = requests.post(webhook_url, data=data, files=files, timeout=60)
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

    history = read_history_stats_from_gsheet()
    buys, sells = extract_actions_from_gsheet(target)

    print(
        f"Google Sheet：{GOOGLE_SHEET_ID or GOOGLE_SHEET_NAME}\n"
        f"目標日期：{target:%Y-%m-%d}\n"
        f"買超原始筆數：{len(buys)}，賣方提醒原始筆數：{len(sells)}\n"
        f"買超門檻：{BUY_THRESHOLD:.0f}，賣方門檻：{SELL_THRESHOLD:.0f}\n"
        f"輸出圖檔：{output_path}"
    )

    draw_report_image(target, buys, sells, history, output_path)

    if args.no_discord:
        print("已設定 --no-discord，只輸出圖片，不發送 Discord。")
        return

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "")
    if not webhook_url:
        raise RuntimeError("找不到 DISCORD_WEBHOOK_URL_TEST，請先在 GitHub Secrets 設定。")

    send_to_discord(webhook_url, output_path, target)
    print("Discord 已發送。")


if __name__ == "__main__":
    main()
