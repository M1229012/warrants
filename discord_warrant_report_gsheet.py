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
import re
import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date, timedelta

import requests
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


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


# ══════════════════════════════════════════════════════════════════════
# 日期推斷 / 勝率統計
# ══════════════════════════════════════════════════════════════════════

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

    buys.append({
        "broker": str(broker).strip(),
        "event": event,
        "underlying": normalize_underlying(underlying),
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "amount": amount,
        "qty": qty,
        "sheet": sheet_name,
    })


def append_sell(sells: list[dict], broker: str, status: str, event: str, underlying, warrant_name: str,
                amount: float, qty: int, sheet_name: str):
    if amount < SELL_THRESHOLD and not (status == "出清" and DISPLAY_EXIT_ALWAYS):
        return

    sells.append({
        "broker": str(broker).strip(),
        "status": status,
        "event": event,
        "underlying": normalize_underlying(underlying),
        "warrant": strip_gsheet_text_prefix(warrant_name),
        "amount": amount,
        "qty": qty,
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
                amount, safe_int(r.get("買進張數")), SHEET_A
            )

        if parse_date_value(r.get("出清日")) == target:
            amount = safe_float(r.get("出清均價")) * safe_float(r.get("買進張數")) * NTD_PER_WARRANT_POINT
            append_sell(
                sells, broker, "出清", event, r.get("標的股"), r.get("權證名稱"),
                amount, safe_int(r.get("買進張數")), SHEET_A
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
                    safe_float(r.get("減碼賣出金額")), safe_int(r.get("買超張數")), sheet_name
                )

            if parse_date_value(r.get("出清日")) == target:
                append_sell(
                    sells, broker, "出清", event, r.get("標的股"), r.get("權證清單"),
                    safe_float(r.get("出清賣出金額")), safe_int(r.get("買超張數")), sheet_name
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

        underlying = items[0].get("underlying", "")
        if warrant_count >= 2 and underlying:
            display_target = f"{underlying}"
            content = f"{warrant_count} 檔權證"
        else:
            display_target = underlying if underlying else items[0].get("warrant", "")
            content = items[0].get("warrant", "") or f"{warrant_count} 檔權證"

        result.append({
            "broker": broker,
            "status": status,
            "event": event,
            "target": display_target,
            "content": content,
            "amount": amount,
            "qty": qty,
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

    W, H = 1200, 1500
    img = Image.new("RGB", (W, H), "#F7F9FC")
    draw = ImageDraw.Draw(img)

    NAVY = "#071E41"
    NAVY2 = "#0B2E5B"
    RED = "#D61F1F"
    GREEN = "#0B7A32"
    TEXT = "#111827"
    MUTED = "#64748B"
    BORDER = "#CBD5E1"
    WHITE = "#FFFFFF"
    PINK = "#FFF1F1"
    MINT = "#EFFAF2"

    def F(size, bold=False):
        return make_font(size, bold)

    def rounded_rect(x1, y1, x2, y2, r=18, fill=WHITE, outline=BORDER, width=2):
        draw.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=fill, outline=outline, width=width)

    def rect(x1, y1, x2, y2, fill, outline=None, width=1):
        draw.rectangle([x1, y1, x2, y2], fill=fill, outline=outline, width=width)

    def center_text(text, x1, y1, x2, y2, font, fill=TEXT):
        bbox = draw.textbbox((0, 0), str(text), font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2 - 2), str(text), font=font, fill=fill)

    def fit_text(text, max_chars):
        text = str(text)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    date_label = f"{target.month}/{target.day}"
    draw.text((50, 28), f"{date_label} 精選分點買賣超追蹤", font=F(58, True), fill=NAVY)
    draw.text((55, 104), f"精選 5 家分點｜只看 {target:%Y/%m/%d} 當日動作", font=F(28, True), fill=NAVY2)
    draw.text((55, 148), "紅色＝買超　綠色＝賣方提醒　單位：萬元", font=F(22, True), fill=TEXT)

    kpis = [
        ("今日買超", f"{sum(x['count'] for x in buys)} 筆", fmt_wan(buy_total), RED, PINK),
        ("賣方提醒", f"{sum(x['count'] for x in sells)} 筆", fmt_wan(sell_total), GREEN, MINT),
        ("淨買超", "", fmt_wan(net), RED if net >= 0 else GREEN, PINK if net >= 0 else MINT),
    ]

    x0, y0 = 40, 205
    card_w, card_h, gap = 350, 145, 25
    for i, (title, mid, val, color, bg) in enumerate(kpis):
        x = x0 + i * (card_w + gap)
        rounded_rect(x, y0, x + card_w, y0 + card_h, 18, fill=bg, outline=color, width=2)
        draw.ellipse([x + 24, y0 + 34, x + 86, y0 + 96], fill=color)
        icon = "↗" if title == "今日買超" else "−" if title == "賣方提醒" else "◎"
        center_text(icon, x + 24, y0 + 34, x + 86, y0 + 96, F(38, True), WHITE)
        draw.text((x + 110, y0 + 28), title, font=F(27, True), fill=TEXT)
        if mid:
            draw.text((x + 110, y0 + 65), mid, font=F(26, True), fill=color)
            draw.text((x + 110, y0 + 102), val, font=F(31, True), fill=color)
        else:
            draw.text((x + 110, y0 + 76), val, font=F(34, True), fill=color)

    # 有動作分點卡；無動作分點縮小
    y = 380
    max_cards = min(len(active_brokers), 4)
    card_gap = 22
    card_w2 = int((1120 - card_gap * (max_cards - 1)) / max_cards) if max_cards else 270
    card_h2 = 215

    for i, b in enumerate(active_brokers[:4]):
        bx = 40 + i * (card_w2 + card_gap)
        s = broker_summary[b]
        rounded_rect(bx, y, bx + card_w2, y + card_h2, 16, fill=WHITE, outline=NAVY2, width=2)
        rect(bx, y, bx + card_w2, y + 50, fill=NAVY, outline=NAVY)
        center_text(b, bx, y, bx + card_w2, y + 50, F(24, True), WHITE)
        center_text(f"平均持有 {s['avg_hold_days']:.1f} 天", bx, y + 58, bx + card_w2, y + 90, F(19), TEXT)
        draw.line([bx + 18, y + 98, bx + card_w2 - 18, y + 98], fill=BORDER, width=1)

        draw.text((bx + 18, y + 112), "買超", font=F(20, True), fill=RED)
        draw.text((bx + 18, y + 142), f"{s['buy_count']}筆 / {fmt_wan(s['buy_amount'])}", font=F(21, True), fill=RED)
        draw.line([bx + 18, y + 174, bx + card_w2 - 18, y + 174], fill=BORDER, width=1)
        draw.text((bx + 18, y + 187), f"賣方：{s['sell_count']}筆 / {fmt_wan(s['sell_amount'])}", font=F(18, True), fill=GREEN)

    inactive_box_bottom = y + card_h2
    if inactive_brokers:
        if max_cards >= 4:
            ix, iy, iw, ih = 40, y + card_h2 + 16, 1120, 90
            inactive_box_bottom = iy + ih
        else:
            ix = 40 + max_cards * (card_w2 + card_gap)
            iy = y
            iw = 1120 - ix
            ih = card_h2
            inactive_box_bottom = y + card_h2

        rounded_rect(ix, iy, ix + iw, iy + ih, 16, fill=WHITE, outline=BORDER, width=2)
        title_y = iy + 20
        draw.text((ix + 24, title_y), "今日無動作分點", font=F(23, True), fill=NAVY)
        inactive_text = "、".join(inactive_brokers)
        draw.text((ix + 24, title_y + 42), inactive_text, font=F(20, True), fill=TEXT)

    table_y = inactive_box_bottom + 25
    table_x, table_w = 40, 1120
    sells_exist = len(sells) > 0
    buy_table_h = 430 if sells_exist else 560

    rounded_rect(table_x, table_y, table_x + table_w, table_y + buy_table_h, 16, fill=WHITE, outline=NAVY2, width=2)
    rect(table_x, table_y, table_x + table_w, table_y + 60, fill=NAVY, outline=NAVY)
    draw.text((table_x + 28, table_y + 12), f"{date_label} 今日買超明細", font=F(33, True), fill=WHITE)

    headers = ["排名", "分點", "事件", "標的 / 權證", "內容", "買超金額"]
    cols = [70, 210, 90, 250, 275, 225]
    hy = table_y + 60
    rect(table_x, hy, table_x + table_w, hy + 48, fill="#F3F7FC", outline=BORDER)
    cx = table_x
    for h, wid in zip(headers, cols):
        center_text(h, cx, hy, cx + wid, hy + 48, F(20, True), NAVY)
        cx += wid
        draw.line([cx, hy, cx, table_y + buy_table_h], fill=BORDER, width=1)

    max_buy_rows = 6 if sells_exist else 8
    row_h = 56
    ry = hy + 48
    for idx, r in enumerate(buys[:max_buy_rows], 1):
        rect(table_x, ry, table_x + table_w, ry + row_h, fill=WHITE if idx % 2 else "#FAFCFF", outline=BORDER)
        values = [
            str(idx),
            r["broker"],
            r["event"],
            fit_text(r["target"], 12),
            fit_text(r["content"], 16),
            fmt_wan(r["amount"]),
        ]
        cx = table_x
        for j, (txt, wid) in enumerate(zip(values, cols)):
            if j in [0, 2]:
                center_text(txt, cx, ry, cx + wid, ry + row_h, F(20, True), RED if j == 2 else TEXT)
            elif j == 5:
                center_text(txt, cx, ry, cx + wid, ry + row_h, F(22, True), RED)
            else:
                draw.text((cx + 12, ry + 14), txt, font=F(19, True if j in [1, 3] else False), fill=TEXT)
            cx += wid
        ry += row_h

    sell_y = table_y + buy_table_h + 25
    if sells_exist:
        sell_h = 255
        rounded_rect(table_x, sell_y, table_x + table_w, sell_y + sell_h, 16, fill=WHITE, outline=GREEN, width=2)
        rect(table_x, sell_y, table_x + table_w, sell_y + 58, fill=GREEN, outline=GREEN)
        draw.text((table_x + 28, sell_y + 12), f"{date_label} 賣方提醒", font=F(31, True), fill=WHITE)

        headers2 = ["分點", "狀態", "標的 / 權證", "內容", "賣方金額"]
        cols2 = [220, 120, 330, 280, 170]
        hy2 = sell_y + 58
        rect(table_x, hy2, table_x + table_w, hy2 + 42, fill="#F3F7FC", outline=BORDER)
        cx = table_x
        for h, wid in zip(headers2, cols2):
            center_text(h, cx, hy2, cx + wid, hy2 + 42, F(19, True), NAVY)
            cx += wid
            draw.line([cx, hy2, cx, sell_y + sell_h], fill=BORDER, width=1)

        ry = hy2 + 42
        for r in sells[:3]:
            rect(table_x, ry, table_x + table_w, ry + 49, fill=WHITE, outline=BORDER)
            values = [r["broker"], r["status"], fit_text(r["target"], 18), fit_text(r["content"], 18), fmt_wan(r["amount"])]
            cx = table_x
            for j, (txt, wid) in enumerate(zip(values, cols2)):
                if j == 1:
                    center_text(txt, cx, ry, cx + wid, ry + 49, F(20, True), GREEN)
                elif j == 4:
                    center_text(txt, cx, ry, cx + wid, ry + 49, F(21, True), GREEN)
                else:
                    draw.text((cx + 12, ry + 13), txt, font=F(18, True if j == 0 else False), fill=TEXT)
                cx += wid
            ry += 49

    by = 1390
    rounded_rect(40, by, 1160, 1460, 16, fill=WHITE, outline=NAVY2, width=2)
    draw.text((65, by + 16), "今日重點", font=F(24, True), fill=NAVY)

    top_broker = max(TRACKED_BROKERS, key=lambda b: broker_summary[b]["buy_amount"]) if buys else "無"
    note = f"{top_broker} 為當日主要買超分點；無動作分點縮小顯示，主圖只保留主要買賣超。"
    draw.text((205, by + 16), note, font=F(22), fill=TEXT)
    draw.text((455, 1470), "本圖為籌碼追蹤整理，不構成投資建議。", font=F(18), fill=MUTED)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=95)


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
