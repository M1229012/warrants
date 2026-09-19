"""艾斯 AI｜本週精選 Top 10 排名圖。

排名階段只顯示候選清單，不自動產生十檔長篇分析或 K 線圖。
精選五分點只以 ★ 標記，完全不影響分數。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from answer_image import (
    ACCENT, BG, CONTENT, INK, LINE, MARGIN, MUTED, WIDTH,
    add_center_watermarks, encode_image, font, header_brand, text_at,
)

ROW_H = 82
HEAD_H = 52
CARD_PAD = 28


def _score_part(card: dict, label: str) -> float:
    for item in card.get("score_parts") or []:
        if item.get("label") == label:
            return float(item.get("value") or 0)
    return 0.0


def _event_text(card: dict) -> str:
    key = str(card.get("event_combo_key") or "-")
    win = card.get("event_win_rate")
    sample = card.get("event_sample")
    if win is None:
        return f"{key}｜勝率 -"
    n = "-" if sample is None else f"{float(sample):g}"
    return f"{key}｜{float(win):g}%｜n={n}"


def render_weekly_pages(question: str, weekly: dict, panels: list | None = None) -> list:
    cards = list(weekly.get("cards") or [])[:10]
    meta = weekly.get("meta") or {}
    top = 124
    title_h = 114
    table_y = top + title_h + 24
    table_h = HEAD_H + max(1, len(cards)) * ROW_H
    footer_h = 105
    height = int(table_y + table_h + footer_h)
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.rectangle((MARGIN, 43, MARGIN + 48, 48), fill=ACCENT)
    header_brand(draw, "權證分點觀察｜本週精選 Top 10", 30)
    text_at(draw, (MARGIN, top), "近 60 個交易日仍有權證大戶部位的候選股，所有分點採相同規則評分。", 22, MUTED)
    text_at(draw, (MARGIN, top + 34), "技術 50＝一般個股型態評分 100 × 0.5；週精選不使用第二套技術評分。", 20, MUTED)
    text_at(draw, (MARGIN, top + 64), "★＝精選五分點（僅標記、不加分）", 20, MUTED)
    text_at(draw, (MARGIN, top + 94), "※ 排名僅供研究與觀察參考，不代表未來表現，亦非買賣建議。", 18, MUTED)

    x0, x1 = MARGIN, WIDTH - MARGIN
    draw.rounded_rectangle((x0, table_y, x1, table_y + table_h), radius=18, fill="white", outline=LINE)
    cols = [
        ("排名", 75), ("股票", 210), ("總分", 90), ("技術", 90),
        ("權證", 90), ("主要分點", 220), ("事件績效", 210), ("有效買進", 155),
    ]
    total = sum(w for _, w in cols)
    cols = [(name, CONTENT * w / total) for name, w in cols]
    draw.rounded_rectangle((x0, table_y, x1, table_y + HEAD_H), radius=18, fill="#F4F5F7")
    cx = x0
    for name, w in cols:
        draw.text((cx + w / 2, table_y + HEAD_H / 2), name, font=font(18, True), fill=MUTED, anchor="mm")
        cx += w

    if not cards:
        draw.text((WIDTH / 2, table_y + HEAD_H + ROW_H / 2), "目前沒有符合條件的候選股票", font=font(24), fill=MUTED, anchor="mm")
    for i, card in enumerate(cards):
        y = table_y + HEAD_H + i * ROW_H
        if i:
            draw.line((x0 + 12, y, x1 - 12, y), fill=LINE)
        tech = float(card.get("technical_score_50") if card.get("technical_score_50") is not None else _score_part(card, "技術面"))
        warrant = float(card.get("warrant_score_50") if card.get("warrant_score_50") is not None else max(0.0, float(card.get("score") or 0) - tech))
        branch = ("★ " if card.get("lead_branch_selected") else "") + str(card.get("lead_branch") or "")
        values = [
            str(card.get("rank") or i + 1),
            f"{card.get('stock_code','')} {card.get('stock_name','')}",
            f"{float(card.get('score') or 0):.1f}",
            f"{tech:.1f}/50",
            f"{warrant:.1f}/50",
            branch,
            _event_text(card),
            str(card.get("lead_amount_text") or "-"),
        ]
        cx = x0
        for (name, w), value in zip(cols, values):
            size = 20 if name not in ("股票", "主要分點", "事件績效") else 18
            bold = name in ("排名", "股票", "總分", "主要分點")
            # 長字串略縮小，避免互疊。
            fnt = font(size, bold)
            while size > 14 and fnt.getlength(value) > w - 14:
                size -= 1
                fnt = font(size, bold)
            draw.text((cx + (10 if name in ("股票", "主要分點", "事件績效") else w / 2), y + ROW_H / 2),
                      value, font=fnt, fill=INK if name != "總分" else ACCENT,
                      anchor="lm" if name in ("股票", "主要分點", "事件績效") else "mm")
            cx += w

    footer_y = table_y + table_h + 28
    if meta.get("data_time"):
        text_at(draw, (MARGIN, footer_y), meta["data_time"], 19, MUTED)
    if meta.get("disclaimer"):
        text_at(draw, (MARGIN, footer_y + 32), meta["disclaimer"], 19, MUTED)
    return [add_center_watermarks(image)]


def make_weekly_attachments(question: str, weekly: dict, panels=None, *, max_bytes=7_500_000) -> list:
    return [encode_image(image, max_bytes) for image in render_weekly_pages(question, weekly, panels)]
