"""艾斯 AI｜本週精選卡片式圖片（每張最多 3 檔）。

版面：頁首 → （第 1 張）TOP5 總覽 → 每檔一組：名次標題卡＋K 線（含分點買賣標註）＋分析卡 → 頁尾。
所有數字來自 Python 計算（weekly_pick.card_facts）；AI 只提供文字欄位。
配色、字型、K 線繪製沿用 answer_image，整體風格一致。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from answer_image import (
    ACCENT, BG, CONTENT, INK, LINE, MARGIN, MUTED, WIDTH,
    clean, draw_chart, encode_image, font, panel_height, text_at, wrap,
)

WEEKLY_PER_PAGE = 3
RANK_COLORS = {1: '#B8862B', 2: '#7A8699', 3: '#A0643C'}
GROUP_BG = '#ECEEF3'
FACT_BG = '#F6F7F9'
GOOD_BG, GOOD_INK = '#EAF6F2', '#1F7A64'
WARN_BG, WARN_INK = '#FDF3E7', '#B45309'
REASON_BG = '#F8F3EA'
NOTICE_BG, NOTICE_INK = '#FFF6E0', '#8A5A00'


def rank_color(rank: int) -> str:
    return RANK_COLORS.get(int(rank or 0), '#344054')


def paragraph(draw, x, y, text, size, width, fill=INK, bold=False, line=None, dry=False) -> int:
    """換行排版並回傳高度；dry=True 只量測不繪製（先量測再配置畫布，文字不截斷、不互疊）。"""
    line = line or int(size * 1.55)
    lines = wrap(clean(text), size, int(width), bold)
    if not dry:
        for i, row in enumerate(lines):
            text_at(draw, (x, y + i * line), row, size, fill, bold)
    return len(lines) * line


def page_header(draw, question: str, page_label: str, dry: bool) -> int:
    if not dry:
        draw.rectangle((MARGIN, 43, MARGIN + 48, 48), fill=ACCENT)
        text_at(draw, (MARGIN, 66), '艾斯 AI｜本週精選候選', 30, bold=True)
        text_at(draw, (WIDTH - 350, 73), 'ACE / RESEARCH', 20, ACCENT)
    y = 124
    y += paragraph(draw, MARGIN, y, question, 28, CONTENT, INK, True, 42, dry)
    if not dry:
        text_at(draw, (MARGIN, y + 8), page_label, 22, MUTED)
    return y + 56


def notice_box(draw, y: int, text: str, dry: bool) -> int:
    if not text:
        return 0
    body = paragraph(draw, 0, 0, text, 24, CONTENT - 56, dry=True, line=36)
    height = body + 36
    if not dry:
        draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + height), radius=16, fill=NOTICE_BG)
        paragraph(draw, MARGIN + 28, y + 18, text, 24, CONTENT - 56, NOTICE_INK, True, 36)
    return height + 24


def overview_card(draw, y: int, cards: list, weekly: dict, dry: bool) -> int:
    """第 1 張的 TOP5 總覽：一句話總結＋每檔一個名次小卡。"""
    x0, pad = MARGIN, 32
    h = 28
    if not dry:
        text_at(draw, (x0 + pad, y + h), '本週 TOP5 總覽', 30, bold=True)
    h += 52
    if weekly.get('overview'):
        h += paragraph(draw, x0 + pad, y + h, weekly['overview'], 25, CONTENT - pad * 2, INK, False, 38, dry) + 10
    filters = (weekly.get('meta') or {}).get('filters') or []
    if filters:
        h += paragraph(draw, x0 + pad, y + h, '篩選條件：' + '；'.join(filters), 22, CONTENT - pad * 2, MUTED, False, 34, dry) + 6
    n, gap = max(1, len(cards)), 14
    chip_w = (CONTENT - pad * 2 - gap * (n - 1)) / n
    name_lines = max([len(wrap(c.get('stock_name', ''), 22, int(chip_w) - 32)) for c in cards] + [1])
    chip_h = 132 + name_lines * 30
    if not dry:
        for i, card in enumerate(cards):
            cx, cy = x0 + pad + i * (chip_w + gap), y + h + 8
            color = rank_color(card['rank'])
            draw.rounded_rectangle((cx, cy, cx + chip_w, cy + chip_h), radius=14, fill=FACT_BG, outline=LINE)
            draw.rounded_rectangle((cx, cy + 14, cx + 6, cy + chip_h - 14), radius=3, fill=color)
            draw.ellipse((cx + 20, cy + 16, cx + 56, cy + 52), fill=color)
            draw.text((cx + 38, cy + 34), str(card['rank']), font=font(20, True), fill='white', anchor='mm')
            text_at(draw, (cx + 66, cy + 20), card['stock_code'], 26, bold=True)
            for k, row in enumerate(wrap(card.get('stock_name', ''), 22, int(chip_w) - 32)):
                text_at(draw, (cx + 20, cy + 64 + k * 30), row, 22, INK)
            sy = cy + 64 + name_lines * 30 + 8
            score = f"{float(card['score']):.1f}"
            text_at(draw, (cx + 20, sy), score, 34, color, bold=True)
            text_at(draw, (cx + 28 + font(34, True).getlength(score), sy + 14), '/ 100', 18, MUTED)
    h += chip_h + 8 + 30
    if not dry:
        draw.rounded_rectangle((x0, y, WIDTH - MARGIN, y + h), radius=20, outline=LINE, width=1)
    return h


def tag_rows(card: dict, width: int) -> list:
    tags = [
        ('型態', card.get('pattern_label', '')),
        ('均線', card.get('ma_alignment', '')),
        ('主力分點', card.get('lead_branch', '')),
        ('本次事件', '、'.join(card.get('triggered_events') or []) + ' 事件'),
        ('事件買進', card.get('lead_amount_text', '')),
    ]
    rows, row, used = [], [], 0.0
    for key, value in tags:
        w = font(22).getlength(f'{key}｜') + font(22, True).getlength(str(value)) + 28
        if row and used + w > width:
            rows.append(row)
            row, used = [], 0.0
        row.append((key, value, w))
        used += w + 10
    if row:
        rows.append(row)
    return rows


def hero_card(draw, y: int, card: dict, dry: bool) -> int:
    """名次徽章＋大標題＋一句話定位＋總分＋標籤＋五項分數條。"""
    x0, x1, pad = MARGIN, WIDTH - MARGIN, 32
    color = rank_color(card['rank'])
    title_x = x0 + pad + 108
    title_w = CONTENT - pad * 2 - 108 - 260
    title = f"{card['stock_code']}  {card.get('stock_name', '')}"
    title_h = paragraph(draw, title_x, y, title, 44, title_w, INK, True, 58, True)
    head_h = paragraph(draw, title_x, y, card.get('headline', ''), 26, title_w, MUTED, False, 38, True)
    top = max(112, title_h + 4 + head_h)
    rows = tag_rows(card, CONTENT - pad * 2)
    bars_h = 58
    height = 30 + top + 22 + len(rows) * 50 + 14 + bars_h + 28
    if dry:
        return height
    draw.rounded_rectangle((x0, y, x1, y + height), radius=20, fill='white', outline=LINE)
    draw.rounded_rectangle((x0, y, x0 + 10, y + height), radius=5, fill=color)
    cx, cy = x0 + pad + 48, y + 30 + 50
    draw.ellipse((cx - 48, cy - 48, cx + 48, cy + 48), fill=color)
    draw.text((cx, cy - 8), f"{int(card['rank']):02d}", font=font(38, True), fill='white', anchor='mm')
    draw.text((cx, cy + 26), '名次', font=font(16, True), fill='white', anchor='mm')
    paragraph(draw, title_x, y + 30, title, 44, title_w, INK, True, 58)
    paragraph(draw, title_x, y + 30 + title_h + 4, card.get('headline', ''), 26, title_w, MUTED, False, 38)
    draw.text((x1 - pad, y + 24), f"{float(card['score']):.1f}", font=font(68, True), fill=color, anchor='rt')
    draw.text((x1 - pad, y + 108), '綜合分數 / 100', font=font(22), fill=MUTED, anchor='rt')
    ty = y + 30 + top + 22
    for row in rows:
        tx = x0 + pad
        for key, value, w in row:
            draw.rounded_rectangle((tx, ty, tx + w, ty + 38), radius=19, fill=FACT_BG, outline=LINE)
            draw.text((tx + 14, ty + 19), f'{key}｜', font=font(22), fill=MUTED, anchor='lm')
            draw.text((tx + 14 + font(22).getlength(f'{key}｜'), ty + 19), str(value), font=font(22, True), fill=INK, anchor='lm')
            tx += w + 10
        ty += 50
    by = ty + 14
    parts = card.get('score_parts') or []
    gap = 24
    col_w = (CONTENT - pad * 2 - gap * (max(1, len(parts)) - 1)) / max(1, len(parts))
    for i, part in enumerate(parts):
        bx = x0 + pad + i * (col_w + gap)
        value, maximum = float(part.get('value') or 0), float(part.get('max') or 1)
        text_at(draw, (bx, by), part['label'], 21, MUTED)
        draw.text((bx + col_w, by), f"{value:.1f} / {maximum:g}", font=font(21, True), fill=INK, anchor='rt')
        draw.rounded_rectangle((bx, by + 38, bx + col_w, by + 50), radius=6, fill=LINE)
        filled = max(0.0, min(1.0, value / maximum)) * col_w
        if filled > 12:
            draw.rounded_rectangle((bx, by + 38, bx + filled, by + 50), radius=6, fill=color)
    return height


def section(draw, x, y, width, title, body, dry, facts=None) -> int:
    if not dry:
        draw.rectangle((x, y + 6, x + 5, y + 32), fill=ACCENT)
        text_at(draw, (x + 18, y), title, 27, bold=True)
    h = 46
    h += paragraph(draw, x, y + h, body, 26, width, INK, False, 40, dry)
    if facts:
        inner = width - 36
        fact_h = sum(paragraph(draw, 0, 0, f, 22, inner, dry=True, line=34) for f in facts) + 24
        if not dry:
            draw.rounded_rectangle((x, y + h + 10, x + width, y + h + 10 + fact_h), radius=12, fill=FACT_BG)
            fy = y + h + 22
            for f in facts:
                fy += paragraph(draw, x + 18, fy, f, 22, inner, '#344054', False, 34)
        h += fact_h + 10
    return h + 26


def bullet_box(draw, x, y, width, title, items, bg, ink, empty_text, dry, height=None) -> int:
    inner = width - 48
    items = list(items or []) or [empty_text]
    body = sum(paragraph(draw, 0, 0, '・' + t, 23, inner, dry=True, line=36) for t in items)
    need = 22 + 42 + body + 20
    if not dry:
        draw.rounded_rectangle((x, y, x + width, y + (height or need)), radius=14, fill=bg)
        text_at(draw, (x + 24, y + 22), title, 25, ink, bold=True)
        by = y + 22 + 42
        for t in items:
            by += paragraph(draw, x + 24, by, '・' + t, 23, inner, MUTED if t == empty_text else ink, False, 36)
    return need


def analysis_card(draw, y: int, card: dict, dry: bool) -> int:
    """判讀順序：型態 → 大量區與均線 → 布林 → 權證籌碼 → 分點近期操作 → 優點／注意 → 週報理由。"""
    x0, pad = MARGIN, 36
    width = CONTENT - pad * 2
    facts = list(card.get('event_lines') or []) + [card.get('overall_line', '')]
    if card.get('other_branches'):
        facts.append('其他高品質分點：' + '、'.join(card['other_branches']))
    sections = [
        ('① 型態', card.get('pattern', ''), None),
        ('② 大量區與均線', card.get('volume_and_ma', ''), None),
        ('③ 布林', card.get('bollinger', ''), None),
        ('④ 權證籌碼', card.get('warrant', ''), [f for f in facts if f]),
        ('⑤ 分點近期操作', card.get('branch_behavior', ''), None),
    ]
    half = (width - 20) / 2
    good_h = bullet_box(draw, 0, 0, half, '優點', card.get('strengths'), GOOD_BG, GOOD_INK, '沒有特別的加分標記', True)
    warn_h = bullet_box(draw, 0, 0, half, '注意', card.get('cautions'), WARN_BG, WARN_INK, '目前沒有特別警示', True)
    box_h = max(good_h, warn_h)
    reason_h = 22 + 42 + paragraph(draw, 0, 0, card.get('why_for_report', ''), 25, width - 48, dry=True, line=38) + 22
    body_h = 30 + sum(section(draw, 0, 0, width, t, b, True, f) for t, b, f in sections)
    height = body_h + box_h + 18 + reason_h + 32
    if dry:
        return height
    draw.rounded_rectangle((x0, y, WIDTH - MARGIN, y + height), radius=20, fill='white', outline=LINE)
    h = 30
    for title, body, fact_lines in sections:
        h += section(draw, x0 + pad, y + h, width, title, body, False, fact_lines)
    bullet_box(draw, x0 + pad, y + h, half, '優點', card.get('strengths'), GOOD_BG, GOOD_INK, '沒有特別的加分標記', False, box_h)
    bullet_box(draw, x0 + pad + half + 20, y + h, half, '注意', card.get('cautions'), WARN_BG, WARN_INK, '目前沒有特別警示', False, box_h)
    ry = y + h + box_h + 18
    draw.rounded_rectangle((x0 + pad, ry, x0 + pad + width, ry + reason_h), radius=14, fill=REASON_BG)
    text_at(draw, (x0 + pad + 24, ry + 22), '適合寫進週報的原因', 25, ACCENT, bold=True)
    paragraph(draw, x0 + pad + 24, ry + 22 + 42, card.get('why_for_report', ''), 25, width - 48, INK, False, 38)
    return height


def candidate_group(draw, y: int, card: dict, panel: dict | None, dry: bool) -> int:
    """一檔候選＝名次標題卡＋K 線（含分點買賣標註）＋分析卡，外層淡灰底框住，檔與檔清楚分隔。"""
    hero_h = hero_card(draw, 0, card, True)
    chart_h = panel_height(panel) if panel else 0
    analysis_h = analysis_card(draw, 0, card, True)
    gap = 16
    height = 20 + hero_h + gap + (chart_h + gap if panel else 0) + analysis_h + 20
    if dry:
        return height
    draw.rounded_rectangle((MARGIN - 20, y, WIDTH - MARGIN + 20, y + height), radius=28, fill=GROUP_BG)
    cy = y + 20
    hero_card(draw, cy, card, False)
    cy += hero_h + gap
    if panel:
        draw_chart(draw, cy, panel)
        cy += chart_h + gap
    analysis_card(draw, cy, card, False)
    return height


def page_footer(draw, y: int, meta: dict, last: bool, dry: bool) -> int:
    h = 0
    if last:
        for line in (meta.get('data_time', ''), meta.get('disclaimer', '')):
            if line:
                h += paragraph(draw, MARGIN, y + h, line, 22, CONTENT, MUTED, False, 34, dry)
        h += 16
    if not dry:
        draw.line((MARGIN, y + h + 10, WIDTH - MARGIN, y + h + 10), fill=LINE)
        text_at(draw, (MARGIN, y + h + 32), '股市艾斯  /  日 K 為收盤資料，非盤中即時行情', 20, MUTED)
    return h + 80


def render_weekly_pages(question: str, weekly: dict, panels: list | None = None) -> list:
    """本週精選 → 多張圖片，每張最多 WEEKLY_PER_PAGE 檔；第 1 張含 TOP5 總覽，最後一張含資料時間。"""
    cards = list(weekly.get('cards') or [])
    panel_by_code = {p.get('stock_code'): p for p in (panels or [])}
    pages = [cards[i:i + WEEKLY_PER_PAGE] for i in range(0, len(cards), WEEKLY_PER_PAGE)] or [[]]
    images = []
    for index, page_cards in enumerate(pages):
        first, last = index == 0, index == len(pages) - 1
        label = (
            f"第 {index + 1} / {len(pages)} 張｜第 {page_cards[0]['rank']}～{page_cards[-1]['rank']} 名"
            if page_cards else '目前沒有符合條件的候選股票'
        )
        blocks = [lambda d, yy, dry, label=label: page_header(d, question, label, dry)]
        if first:
            blocks.append(lambda d, yy, dry: notice_box(d, yy, weekly.get('notice', ''), dry))
            if cards:
                blocks.append(lambda d, yy, dry: overview_card(d, yy, cards, weekly, dry) + 36)
        for card in page_cards:
            blocks.append(lambda d, yy, dry, c=card: candidate_group(d, yy, c, panel_by_code.get(c['stock_code']), dry) + 40)
        blocks.append(lambda d, yy, dry, last=last: page_footer(d, yy, weekly.get('meta') or {}, last, dry))
        probe = ImageDraw.Draw(Image.new('RGB', (10, 10)))
        height = 0
        for block in blocks:
            height += block(probe, height, True)
        image = Image.new('RGB', (WIDTH, int(height) + 10), BG)
        draw = ImageDraw.Draw(image)
        y = 0
        for block in blocks:
            y += block(draw, y, False)
        images.append(image)
    return images


def make_weekly_attachments(question: str, weekly: dict, panels=None, *, max_bytes=7_500_000) -> list:
    return [encode_image(image, max_bytes) for image in render_weekly_pages(question, weekly, panels)]
