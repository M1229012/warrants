#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日精選分點買賣超追蹤圖卡
- 買超：紅色
- 賣方提醒：綠色
- 只追蹤指定 5 家分點
- 買超門檻預設 100 萬
- 賣方顯示門檻預設為買超門檻的 20%，也就是 20 萬
- 圖卡不公開內部門檻，只呈現主要買賣動作
- 產生 PNG 並送到 DISCORD_WEBHOOK_URL_TEST
"""

from __future__ import annotations

import os
import math
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date

import requests
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


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

SHEET_A = "A_單檔大買"
SHEET_B = "B_同標的單日合計"
SHEET_C = "C_同標的3日累積"
SHEET_D = "D_近10日累積淨買進"
SHEET_STAT = "勝率統計"

NTD_PER_WARRANT_POINT = float(os.getenv("NTD_PER_WARRANT_POINT", "1000"))


def parse_date_value(v) -> date | None:
    if pd.isna(v):
        return None
    try:
        t = pd.to_datetime(v, errors="coerce")
        if pd.isna(t):
            return None
        return t.date()
    except Exception:
        return None


def money_to_wan(v: float) -> float:
    return round(float(v or 0) / 10000, 1)


def fmt_wan(v: float) -> str:
    return f"{money_to_wan(v):.1f} 萬"


def safe_float(v, default=0.0) -> float:
    try:
        if pd.isna(v) or v == "-":
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v, default=0) -> int:
    try:
        if pd.isna(v) or v == "-":
            return default
        return int(float(v))
    except Exception:
        return default


def short_event_name(sheet_name: str) -> str:
    if sheet_name == SHEET_A:
        return "A"
    if sheet_name == SHEET_B:
        return "B"
    if sheet_name == SHEET_C:
        return "C"
    if sheet_name == SHEET_D:
        return "D"
    return ""


def infer_latest_date(excel_path: Path) -> date:
    candidates = []

    read_plan = [
        (SHEET_A, ["買進日", "減碼日", "出清日"]),
        (SHEET_B, ["事件日", "減碼日", "出清日"]),
        (SHEET_C, ["結束日", "減碼日", "出清日"]),
        (SHEET_D, ["結束日", "減碼日", "出清日"]),
    ]

    for sheet, cols in read_plan:
        try:
            df = pd.read_excel(excel_path, sheet_name=sheet, usecols=lambda c: c in cols)
        except Exception:
            continue
        for c in cols:
            if c in df.columns:
                for v in df[c].dropna().tolist():
                    d = parse_date_value(v)
                    if d:
                        candidates.append(d)

    if not candidates:
        raise RuntimeError("無法從 Excel 推斷日期，請用 TARGET_DATE=YYYY-MM-DD 指定。")
    return max(candidates)


def read_history_stats(excel_path: Path) -> dict:
    """
    勝率統計表格式較特殊，因此用 header=None 抓「全部-A+B+C+D合併」列。
    """
    result = {}
    try:
        stat = pd.read_excel(excel_path, sheet_name=SHEET_STAT, header=None)
    except Exception:
        return result

    for broker in TRACKED_BROKERS:
        rows = stat[(stat[0] == broker) & (stat[1] == "全部-A+B+C+D合併")]
        if not rows.empty:
            r = rows.iloc[0]
            result[broker] = {
                "total_events": safe_int(r[2]),
                "win_rate": safe_float(r[8]),
                "avg_hold_days": safe_float(r[9]),
            }
        else:
            result[broker] = {
                "total_events": 0,
                "win_rate": 0.0,
                "avg_hold_days": 0.0,
            }
    return result


def load_sheet(excel_path: Path, sheet_name: str, needed_cols: list[str]) -> pd.DataFrame:
    try:
        df = pd.read_excel(excel_path, sheet_name=sheet_name, usecols=lambda c: c in needed_cols)
    except ValueError:
        # 部分欄位不存在時 fallback 全讀，再取交集
        df = pd.read_excel(excel_path, sheet_name=sheet_name)
        df = df[[c for c in needed_cols if c in df.columns]]
    if "分點" in df.columns:
        df = df[df["分點"].isin(TRACKED_BROKERS)].copy()
    return df


def append_buy(buys: list[dict], broker: str, event: str, underlying, warrant_name: str,
               amount: float, qty: int, sheet_name: str):
    if amount < BUY_THRESHOLD:
        return
    buys.append({
        "broker": broker,
        "event": event,
        "underlying": "" if pd.isna(underlying) else str(int(float(underlying))) if isinstance(underlying, (int, float)) and not pd.isna(underlying) else str(underlying),
        "warrant": str(warrant_name) if pd.notna(warrant_name) else "",
        "amount": amount,
        "qty": qty,
        "sheet": sheet_name,
    })


def append_sell(sells: list[dict], broker: str, status: str, event: str, underlying, warrant_name: str,
                amount: float, qty: int, sheet_name: str):
    if amount < SELL_THRESHOLD and not (status == "出清" and DISPLAY_EXIT_ALWAYS):
        return
    sells.append({
        "broker": broker,
        "status": status,
        "event": event,
        "underlying": "" if pd.isna(underlying) else str(int(float(underlying))) if isinstance(underlying, (int, float)) and not pd.isna(underlying) else str(underlying),
        "warrant": str(warrant_name) if pd.notna(warrant_name) else "",
        "amount": amount,
        "qty": qty,
        "sheet": sheet_name,
    })


def extract_actions(excel_path: Path, target: date) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []

    # A：單檔權證大買
    a_cols = [
        "事件類型", "分點", "權證名稱", "標的股", "買進日", "買進張數", "買進金額",
        "減碼日", "減碼均價", "減碼獲利%", "出清日", "出清均價", "出清獲利%"
    ]
    A = load_sheet(excel_path, SHEET_A, a_cols)

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
        df = load_sheet(excel_path, sheet_name, common_cols)

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

        # 若同標的多筆，主欄顯示標的股；若單筆，顯示權證
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


def draw_report_image(
    target: date,
    buys_raw: list[dict],
    sells_raw: list[dict],
    history: dict,
    output_path: Path,
):
    buys = compress_actions(buys_raw, "buy")
    sells = compress_actions(sells_raw, "sell")

    buy_total = sum(x["amount"] for x in buys)
    sell_total = sum(x["amount"] for x in sells)
    net = buy_total - sell_total

    # 分點 summary
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

    # Header
    date_label = f"{target.month}/{target.day}"
    draw.text((50, 28), f"{date_label} 精選分點買賣超追蹤", font=F(58, True), fill=NAVY)
    draw.text((55, 104), f"精選 5 家分點｜只看 {target:%Y/%m/%d} 當日動作", font=F(28, True), fill=NAVY2)
    draw.text((55, 148), "紅色＝買超　綠色＝賣方提醒　單位：萬元", font=F(22, True), fill=TEXT)

    # KPI cards
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

    # Active broker cards
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

    if inactive_brokers:
        ix = 40 + max_cards * (card_w2 + card_gap) if max_cards < 4 else 40
        iy = y + card_h2 + 16 if max_cards >= 4 else y
        iw = 1120 if max_cards >= 4 else 1120 - ix
        ih = 90 if max_cards >= 4 else card_h2
        rounded_rect(ix, iy, ix + iw, iy + ih, 16, fill=WHITE, outline=BORDER, width=2)
        title_y = iy + 20
        draw.text((ix + 24, title_y), "今日無動作分點", font=F(23, True), fill=NAVY)
        inactive_text = "、".join(inactive_brokers)
        draw.text((ix + 24, title_y + 42), inactive_text, font=F(20, True), fill=TEXT)

    # Tables
    table_y = 625 if not inactive_brokers or max_cards < 4 else 715
    buy_table_h = 430 if sells else 560
    table_x, table_w = 40, 1120

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

    max_buy_rows = 6 if sells else 8
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

    # 賣方提醒
    sell_y = table_y + buy_table_h + 25
    sell_h = 255
    if sells:
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
    else:
        sell_y -= 20

    # Bottom note
    by = 1390
    rounded_rect(40, by, 1160, 1460, 16, fill=WHITE, outline=NAVY2, width=2)
    draw.text((65, by + 16), "今日重點", font=F(24, True), fill=NAVY)

    top_broker = max(TRACKED_BROKERS, key=lambda b: broker_summary[b]["buy_amount"]) if buys else "無"
    note = f"{top_broker} 為當日主要買超分點；無動作分點縮小顯示，主圖只保留主要買賣超。"
    draw.text((205, by + 16), note, font=F(22), fill=TEXT)
    draw.text((455, 1470), "本圖為籌碼追蹤整理，不構成投資建議。", font=F(18), fill=MUTED)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=95)


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
    parser.add_argument("--excel", default=os.getenv("EXCEL_PATH", "權證分點籌碼.xlsx"))
    parser.add_argument("--date", default=os.getenv("TARGET_DATE", ""))
    parser.add_argument("--output", default=os.getenv("OUTPUT_IMAGE", "output/精選分點買賣超追蹤.png"))
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    excel_path = Path(args.excel)
    if not excel_path.exists():
        raise FileNotFoundError(f"找不到 Excel 檔案：{excel_path}")

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = infer_latest_date(excel_path)

    output_path = Path(args.output)

    history = read_history_stats(excel_path)
    buys, sells = extract_actions(excel_path, target)

    make_msg = (
        f"目標日期：{target:%Y-%m-%d}\n"
        f"買超原始筆數：{len(buys)}，賣方提醒原始筆數：{len(sells)}\n"
        f"買超門檻：{BUY_THRESHOLD:.0f}，賣方門檻：{SELL_THRESHOLD:.0f}\n"
        f"輸出圖檔：{output_path}"
    )
    print(make_msg)

    draw_report_image(target, buys, sells, history, output_path)

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "")
    if args.no_discord:
        print("已設定 --no-discord，只輸出圖片，不發送 Discord。")
        return

    if not webhook_url:
        raise RuntimeError("找不到 DISCORD_WEBHOOK_URL_TEST，請先在 GitHub Secrets 設定。")

    send_to_discord(webhook_url, output_path, target)
    print("Discord 已發送。")


if __name__ == "__main__":
    main()
