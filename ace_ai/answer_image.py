"""Deterministic, image-only answers. No network calls or AI-generated price graphics."""
from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = '#F5F5F7'
INK = '#101828'
MUTED = '#667085'
LINE = '#E4E7EC'
ACCENT = '#A17936'
UP = '#E85D5D'
DOWN = '#2CB39A'
WIDTH = 1440
MARGIN = 64
CONTENT = WIDTH - MARGIN * 2


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False):
    candidates = [os.getenv('DISCORD_AI_FONT_PATH', '')]
    candidates += [
        'C:/Windows/Fonts/msjhbd.ttc' if bold else 'C:/Windows/Fonts/msjh.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold else
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]
    for path in candidates:
        if path and Path(path).is_file():
            # Debian Noto CJK collection index 3 is Traditional Chinese.
            return ImageFont.truetype(path, size, index=3 if 'NotoSansCJK' in path else 0)
    raise RuntimeError('找不到中文字型；請安裝 fonts-noto-cjk 或設定 DISCORD_AI_FONT_PATH')


def clean(text: str) -> str:
    text = str(text).replace('🥇', '01 ').replace('🥈', '02 ').replace('🥉', '03 ')
    text = re.sub(r'[\U0001F000-\U0001FAFF\uFE0F\u20E3\u200D]', '', text)
    text = re.sub(r'[❓✅⚠❌📊]', '', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1（\2）', text)
    return text.replace('**', '').replace('`', '').strip()


def wrap(text: str, size: int, width: int, bold: bool = False) -> list[str]:
    face = font(size, bold)
    lines, current = [], ''
    for char in text:
        if current and face.getlength(current + char) > width:
            lines.append(current)
            current = ''
        current += char
    if current:
        lines.append(current)
    return lines or ['']


def number(value, digits=2):
    try:
        value = float(value)
        return f'{value:,.{digits}f}' if math.isfinite(value) else '—'
    except (TypeError, ValueError):
        return '—'


@dataclass
class Block:
    kind: str
    lines: list[str]
    height: int


def body_blocks(text: str) -> list[Block]:
    # Measure before allocating the canvas; no truncation or font shrinking.
    # Existing rule answers use emoji at the start of section headings.
    text = re.sub(r'(?m)^\s*[💹📈📊📌📰🔎]\s*([^\n]+)', r'【\1】', text)
    text = clean(text)
    text = re.sub(r'(?<!\n)(【[^】]+】)', r'\n\1', text)
    blocks = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            if blocks and blocks[-1].kind != 'space':
                blocks.append(Block('space', [], 14))
            continue
        heading = re.match(r'^(?:【([^】]+)】|#{1,6}\s+(.+)$)(.*)', raw)
        if heading:
            title = heading.group(1) or heading.group(2)
            lines = wrap(title, 29, CONTENT - 80, True)
            blocks.append(Block('heading', lines, len(lines) * 42 + 22))
            raw = heading.group(3).strip()
        if raw:
            small = raw.startswith(('資料時間', '※', '資料來源'))
            size, line_height = (23, 36) if small else (29, 45)
            lines = wrap(raw, size, CONTENT - 80)
            blocks.append(Block('note' if small else 'text', lines, len(lines) * line_height + 10))
    return blocks


def text_at(draw, xy, text, size=29, fill=INK, bold=False):
    draw.text(xy, str(text), font=font(size, bold), fill=fill, anchor='lt')


def draw_chart(draw, y: int, panel: dict) -> None:
    x0, x1 = MARGIN, WIDTH - MARGIN
    draw.rounded_rectangle((x0, y, x1, y + 670), radius=20, fill='white', outline=LINE)
    code, name = panel.get('stock_code', ''), panel.get('stock_name', '')
    text_at(draw, (x0 + 32, y + 26), f'{code} {name}｜日 K', 31, bold=True)
    bars = panel.get('bars') or []
    if not bars:
        for i, line in enumerate(wrap(panel.get('error', 'K 線資料暫時無法取得'), 29, CONTENT - 80)):
            text_at(draw, (x0 + 36, y + 110 + i * 45), line, fill=MUTED)
        return
    last = bars[-1]
    change = panel.get('change_pct')
    info = f"收盤 {number(last['Close'])}"
    if change is not None:
        info += f"   {change:+.2f}%"
    text_at(draw, (x0 + 32, y + 76), info, 28, UP if (change or 0) >= 0 else DOWN)
    text_at(draw, (x1 - 320, y + 35), f"資料至 {last['date']}", 22, MUTED)
    colors = {'MA5': UP, 'MA10': '#D99836', 'MA20': '#6C8B46', 'MA60': '#777AC4'}
    for i, (key, color) in enumerate(colors.items()):
        text_at(draw, (x0 + 34 + i * 292, y + 125), f'{key}  {number(last.get(key))}', 23, color)
    left, right, top, bottom = x0 + 36, x1 - 118, y + 180, y + 486
    lows = [b['Low'] for b in bars]
    highs = [b['High'] for b in bars]
    for b in bars:
        for key in colors:
            if b.get(key) is not None:
                lows.append(b[key]); highs.append(b[key])
    low, high = min(lows), max(highs)
    padding = max((high - low) * .08, abs(high) * .005, .01)
    low -= padding; high += padding
    py = lambda value: bottom - (value - low) / (high - low) * (bottom - top)
    step = (right - left) / len(bars)
    px = lambda i: left + (i + .5) * step
    for i in range(5):
        value = low + (high - low) * i / 4
        gy = py(value)
        draw.line((left, gy, right, gy), fill=LINE, width=1)
        text_at(draw, (right + 14, gy - 10), number(value), 20, MUTED)
    zones = panel.get('zones') or []
    for zone_index, zone in enumerate(zones):
        a, b = zone.get('price_low'), zone.get('price_high')
        if a is not None and b is not None and a <= high and b >= low:
            draw.rectangle((left, py(min(b, high)), right, py(max(a, low))), fill='#FCEAEA' if zone_index == 0 else '#FFF1DC')
    for i, bar in enumerate(bars):
        color = UP if bar['Close'] >= bar['Open'] else DOWN
        center = px(i)
        draw.line((center, py(bar['High']), center, py(bar['Low'])), fill=color, width=2)
        a, b = sorted((py(bar['Open']), py(bar['Close'])))
        half = max(1, step * .30)
        draw.rectangle((center - half, a, center + half, max(a + 2, b)), fill=color)
    for key, color in colors.items():
        segment = []
        for i, bar in enumerate(bars):
            if bar.get(key) is None:
                if len(segment) > 1:
                    draw.line(segment, fill=color, width=2)
                segment = []
            else:
                segment.append((px(i), py(bar[key])))
        if len(segment) > 1:
            draw.line(segment, fill=color, width=2)
    vtop, vbottom = y + 535, y + 595
    maximum = max([b.get('Volume') or 0 for b in bars] + [1])
    for i, bar in enumerate(bars):
        height = max(0, (bar.get('Volume') or 0) / maximum * (vbottom - vtop))
        draw.rectangle((px(i) - step * .3, vbottom - height, px(i) + step * .3, vbottom),
                       fill=UP if bar['Close'] >= bar['Open'] else DOWN)
    text_at(draw, (left, y + 504), '成交量', 20, MUTED)
    for i in sorted({0, len(bars) // 3, 2 * len(bars) // 3, len(bars) - 1}):
        text_at(draw, (max(left, min(px(i) - 32, right - 66)), y + 610), bars[i]['date'][5:], 20, MUTED)
    if zones:
        text_at(draw, (right - 620, y + 646), '淡紅：最大量區  /  淡橘：第二大量區', 18, MUTED)


def render_answer(question: str, answer: str, panels: list[dict] | None = None,
                  *, title: str = '艾斯 AI｜研究筆記', demo: bool = False) -> Image.Image:
    panels = panels or []
    question_lines = wrap(clean(question), 31, CONTENT - 12, True)
    header_height = 155 + len(question_lines) * 47
    blocks = body_blocks(answer)
    body_height = sum(b.height for b in blocks) + 68
    height = header_height + len(panels) * 694 + body_height + 112
    image = Image.new('RGB', (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((MARGIN, 43, MARGIN + 48, 48), fill=ACCENT)
    text_at(draw, (MARGIN, 66), title, 28, bold=True)
    text_at(draw, (WIDTH - 350, 73), '示範資料・非真實行情' if demo else 'ACE / RESEARCH', 20, ACCENT)
    for i, line in enumerate(question_lines):
        text_at(draw, (MARGIN, 124 + i * 47), line, 31, bold=True)
    y = header_height
    for panel in panels:
        draw_chart(draw, y, panel)
        y += 694
    draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + body_height), radius=20, fill='white', outline=LINE)
    cursor = y + 30
    for block in blocks:
        if block.kind == 'heading':
            draw.rectangle((MARGIN + 30, cursor + 4, MARGIN + 34, cursor + 29), fill=ACCENT)
            for i, line in enumerate(block.lines):
                text_at(draw, (MARGIN + 48, cursor + i * 42), line, 29, bold=True)
        else:
            for i, line in enumerate(block.lines):
                small = block.kind == 'note'
                text_at(draw, (MARGIN + 36, cursor + i * (36 if small else 45)), line,
                        23 if small else 29, MUTED if small else INK)
        cursor += block.height
    draw.line((MARGIN, height - 71, WIDTH - MARGIN, height - 71), fill=LINE)
    text_at(draw, (MARGIN, height - 49), '股市艾斯  /  日 K 為收盤資料，非盤中即時行情' if panels else '股市艾斯  /  AI 資料整理', 20, MUTED)
    return image


def encode_image(image: Image.Image, max_bytes: int = 7_500_000) -> tuple[bytes, str]:
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    if output.tell() <= max_bytes:
        return output.getvalue(), 'png'
    for quality in (92, 85, 75, 65):
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=quality, optimize=True)
        if output.tell() <= max_bytes:
            return output.getvalue(), 'jpg'
    raise ValueError('回答圖片超過附件容量，請縮小查詢範圍')


def make_attachment(question: str, answer: str, panels=None, *, max_bytes=7_500_000):
    return encode_image(render_answer(question, answer, panels), max_bytes)
