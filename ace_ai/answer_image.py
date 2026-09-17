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
CHART_HEIGHT = 790
# 分點買賣標註：K 線上下各留一條標籤帶（▲／▼＋最多 3 列編號圓圈），不和 K 棒重疊。
MARK_LANE = 104
MARK_BADGE_R = 11
MARK_BADGE_ROW = 26
MARK_MAX_ROWS = 3
MARK_LEGEND_SIZE = 20
MARK_LEGEND_LINE = 30


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


def volume_profile_rectangles(profile: dict, left, right, py, low, high):
    """Reference overlay geometry: width = relative volume * axis width / 1.08.

    Blend the reference alpha against the white card; draw behind grid/candles.
    No replacement bands are fabricated when the profile is unavailable.
    """
    bins = profile.get('bins') or []
    values = profile.get('profile') or []
    if not values or len(bins) != len(values) + 1:
        return []
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bins + values):
        return []
    if any(b <= a for a, b in zip(bins, bins[1:])) or max(values) <= 0:
        return []
    maximum = max(values)
    rectangles = []
    for i, value in enumerate(values):
        if value <= 0 or bins[i + 1] <= low or bins[i] >= high:
            continue
        if i == profile.get('max_idx'):
            rgb, alpha = (220, 38, 38), .20
        elif i == profile.get('second_idx'):
            rgb, alpha = (245, 158, 11), .20
        else:
            rgb, alpha = (56, 189, 248), .15
        fill = tuple(round(255 * (1-alpha) + c * alpha) for c in rgb)
        width = value / maximum * (right - left) / 1.08
        rectangles.append(((left, py(min(high, bins[i+1])), left + width, py(max(low, bins[i]))), fill))
    return rectangles


def _mark_events(panel: dict) -> list[dict]:
    return list(((panel or {}).get('marks') or {}).get('events') or [])


def _has_mark_section(panel: dict) -> bool:
    return bool((panel or {}).get('bars')) and bool((panel or {}).get('marks'))


def _single_branch(panel: dict) -> str:
    branches = {e['branch'] for e in _mark_events(panel)}
    return next(iter(branches)) if len(branches) == 1 else ''


def _mark_legend_entries(panel: dict) -> list[str]:
    entries = []
    single = _single_branch(panel)
    for e in _mark_events(panel):
        who = '' if single else f"{e['branch']}｜"
        text = f"{e['no']}  {who}{e['event']} {e['buy_date'][5:]} 買 {e.get('buy_amount_text', '')}"
        if e.get('exit_date'):
            text += f" → {e['exit_date'][5:]} 出清"
        elif e.get('reduce_date'):
            text += f" → {e['reduce_date'][5:]} 減碼未出清"
        else:
            text += f"｜{e.get('status', '')}"
        entries.append(text)
    return entries


def _mark_legend_layout(panel: dict) -> tuple[list[str], list[list[str]], int]:
    """回傳（標題行、每筆換行後的文字、總高度）。先量測再配置畫布，文字不截斷、不互疊。"""
    if not _has_mark_section(panel):
        return [], [], 0
    marks = panel.get('marks') or {}
    rule = marks.get('rule', '')
    if _mark_events(panel):
        title = f"分點買賣標註｜{rule}：紅圈 N＝A～E 事件買進日　綠圈 N＝該筆出清日　▼ 無數字＝減碼日（不含報酬率）"
    else:
        title = f"分點買賣標註｜{rule}：圖表區間內沒有 A～E 事件"
    title_lines = wrap(title, MARK_LEGEND_SIZE, CONTENT - 80)
    column = (CONTENT - 80 - 24) // 2
    wrapped = [wrap(entry, MARK_LEGEND_SIZE, column) for entry in _mark_legend_entries(panel)]
    rows_height = 0
    for i in range(0, len(wrapped), 2):
        rows_height += max(len(item) for item in wrapped[i:i + 2]) * MARK_LEGEND_LINE
    height = 16 + len(title_lines) * MARK_LEGEND_LINE + rows_height + 8
    return title_lines, wrapped, height


def panel_height(panel: dict) -> int:
    extra = 2 * MARK_LANE if _mark_events(panel) else 0
    return CHART_HEIGHT + extra + _mark_legend_layout(panel)[2]


def _assign_rows(badges: list[dict]) -> None:
    """同一側的編號圓圈依 x 排列，擠在一起時往下一列放（最多 3 列）。"""
    last = [-1e9] * MARK_MAX_ROWS
    gap = MARK_BADGE_R * 2 + 4
    for badge in sorted(badges, key=lambda b: b['cx']):
        row = next((r for r in range(MARK_MAX_ROWS) if badge['cx'] - last[r] >= gap), None)
        if row is None:
            # 三列都擠滿：放進最空的一列並往右挪到不重疊的位置，細線仍連回自己的三角形。
            row = min(range(MARK_MAX_ROWS), key=lambda r: last[r])
            badge['cx'] = last[row] + gap
        badge['row'] = row
        last[row] = badge['cx']


def _draw_badge(draw, cx, cy, number_text, color):
    draw.ellipse((cx - MARK_BADGE_R, cy - MARK_BADGE_R, cx + MARK_BADGE_R, cy + MARK_BADGE_R),
                 fill=color, outline='white', width=2)
    size = 14 if len(str(number_text)) < 2 else 12
    draw.text((cx, cy), str(number_text), font=font(size, True), fill='white', anchor='mm')


def _dotted(draw, x, y_from, y_to, color):
    step = 5 if y_to >= y_from else -5
    for yy in range(int(y_from), int(y_to), step * 2):
        draw.line((x, yy, x, yy + step), fill=color, width=1)


def draw_marks(draw, panel: dict, px, py, step: float, price_top: float, price_bottom: float) -> None:
    """broker_replay_kline 同款標註：▲＋紅圈 N 在買進日、▼＋綠圈 N 在出清日、▼ 無數字＝減碼日。不寫報酬率。"""
    bars = panel.get('bars') or []
    index = {bar['date']: i for i, bar in enumerate(bars)}
    events = _mark_events(panel)
    if not events:
        return
    half = max(5, min(9, step * 0.45))
    buy_badges, sell_badges = [], []
    sell_days: dict[int, list[int]] = {}
    reduce_days: set[int] = set()
    buy_days: set[int] = set()
    for e in events:
        i = index.get(e['buy_date'])
        if i is not None:
            buy_days.add(i)
            buy_badges.append({'x': px(i), 'cx': px(i), 'no': e['no']})
        j = index.get(e.get('exit_date') or '')
        if j is not None:
            sell_days.setdefault(j, []).append(e['no'])
        k = index.get(e.get('reduce_date') or '')
        if k is not None and k != j:
            reduce_days.add(k)
    for j, numbers in sell_days.items():
        for n, no in enumerate(sorted(numbers)):
            offset = (n - (len(numbers) - 1) / 2) * (MARK_BADGE_R * 2 + 3)
            sell_badges.append({'x': px(j), 'cx': px(j) + offset, 'no': no})
    _assign_rows(buy_badges)
    _assign_rows(sell_badges)

    tri_bottom = price_bottom + 14
    for i in sorted(buy_days):
        x = px(i)
        _dotted(draw, x, py(bars[i]['Low']) + 4, tri_bottom - half, UP)
        draw.polygon([(x, tri_bottom - half), (x - half, tri_bottom + half), (x + half, tri_bottom + half)],
                     fill=UP, outline='white')
    tri_top = price_top - 14
    for j in sorted(set(sell_days) | reduce_days):
        x = px(j)
        _dotted(draw, x, py(bars[j]['High']) - 4, tri_top + half, DOWN)
        draw.polygon([(x - half, tri_top - half), (x + half, tri_top - half), (x, tri_top + half)],
                     fill=DOWN, outline='white')
    for badge in buy_badges:
        cy = tri_bottom + half + 6 + MARK_BADGE_R + badge['row'] * MARK_BADGE_ROW
        if abs(badge['cx'] - badge['x']) > 1 or badge['row']:
            draw.line((badge['x'], tri_bottom + half, badge['cx'], cy - MARK_BADGE_R), fill=UP, width=1)
        _draw_badge(draw, badge['cx'], cy, badge['no'], UP)
    for badge in sell_badges:
        cy = tri_top - half - 6 - MARK_BADGE_R - badge['row'] * MARK_BADGE_ROW
        if abs(badge['cx'] - badge['x']) > 1 or badge['row']:
            draw.line((badge['x'], tri_top - half, badge['cx'], cy + MARK_BADGE_R), fill=DOWN, width=1)
        _draw_badge(draw, badge['cx'], cy, badge['no'], DOWN)


def draw_mark_legend(draw, panel: dict, top: int) -> None:
    title_lines, wrapped, _height = _mark_legend_layout(panel)
    if not title_lines:
        return
    x0 = MARGIN + 36
    y = top + 16
    for line in title_lines:
        text_at(draw, (x0, y), line, MARK_LEGEND_SIZE, INK, bold=True)
        y += MARK_LEGEND_LINE
    column = (CONTENT - 80 - 24) // 2
    for i in range(0, len(wrapped), 2):
        pair = wrapped[i:i + 2]
        for c, lines in enumerate(pair):
            for k, line in enumerate(lines):
                text_at(draw, (x0 + c * (column + 24), y + k * MARK_LEGEND_LINE), line, MARK_LEGEND_SIZE, INK)
        y += max(len(lines) for lines in pair) * MARK_LEGEND_LINE


def draw_chart(draw, y: int, panel: dict) -> None:
    x0, x1 = MARGIN, WIDTH - MARGIN
    draw.rounded_rectangle((x0, y, x1, y + panel_height(panel)), radius=20, fill='white', outline=LINE)
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
    band_colors = {'BB_UPPER': '#667085', 'BB_MID': '#76879A', 'BB_LOWER': '#667085'}
    for i, (key, label) in enumerate((('BB_UPPER', '布林上軌'), ('BB_MID', '中軌 = MA20'), ('BB_LOWER', '布林下軌'))):
        text_at(draw, (x0 + 34 + i * 400, y + 168), f'{label}  {number(last.get(key))}', 22, band_colors[key])
    extra = 2 * MARK_LANE if _mark_events(panel) else 0
    left, right, top, bottom = x0 + 36, x1 - 118, y + 225, y + 531 + extra
    # 有標註時價格只畫在中間，上下標籤帶放 ▼／▲ 與編號圓圈。
    price_top, price_bottom = (top + MARK_LANE, bottom - MARK_LANE) if extra else (top, bottom)
    lows = [b['Low'] for b in bars]
    highs = [b['High'] for b in bars]
    for b in bars:
        for key in list(colors) + list(band_colors):
            if b.get(key) is not None:
                lows.append(b[key]); highs.append(b[key])
    low, high = min(lows), max(highs)
    padding = max((high - low) * .08, abs(high) * .005, .01)
    low -= padding; high += padding
    py = lambda value: price_bottom - (value - low) / (high - low) * (price_bottom - price_top)
    step = (right - left) / len(bars)
    px = lambda i: left + (i + .5) * step
    profile_rectangles = volume_profile_rectangles(panel.get('volume_profile') or {}, left, right, py, low, high)
    for rectangle, fill in profile_rectangles:
        draw.rectangle(rectangle, fill=fill)
    for i in range(5):
        value = low + (high - low) * i / 4
        gy = py(value)
        draw.line((left, gy, right, gy), fill=LINE, width=1)
        text_at(draw, (right + 14, gy - 10), number(value), 20, MUTED)
    draw_marks(draw, panel, px, py, step, price_top, price_bottom)
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
    # Dashed bands, including the middle band, use report-supplied values.
    # A missing rolling value breaks the line instead of joining across a gap.
    for key, color in band_colors.items():
        for i in range(1, len(bars)):
            a, b = bars[i-1].get(key), bars[i].get(key)
            if a is None or b is None:
                continue
            xa, ya, xb, yb = px(i-1), py(a), px(i), py(b)
            distance = math.hypot(xb-xa, yb-ya)
            for start in range(0, max(1, math.ceil(distance)), 12):
                t0, t1 = min(start/max(distance, 1), 1), min((start+7)/max(distance, 1), 1)
                draw.line((xa+(xb-xa)*t0, ya+(yb-ya)*t0, xa+(xb-xa)*t1, ya+(yb-ya)*t1), fill=color, width=2)
    vtop, vbottom = y + 580 + extra, y + 640 + extra
    maximum = max([b.get('Volume') or 0 for b in bars] + [1])
    for i, bar in enumerate(bars):
        height = max(0, (bar.get('Volume') or 0) / maximum * (vbottom - vtop))
        draw.rectangle((px(i) - step * .3, vbottom - height, px(i) + step * .3, vbottom),
                       fill=UP if bar['Close'] >= bar['Open'] else DOWN)
    text_at(draw, (left, y + 549 + extra), '成交量', 20, MUTED)
    for i in sorted({0, len(bars) // 3, 2 * len(bars) // 3, len(bars) - 1}):
        text_at(draw, (max(left, min(px(i) - 32, right - 66)), y + 655 + extra), bars[i]['date'][5:], 20, MUTED)
    legend = '價量分布｜紅：最大量區  /  橘：第二大量區  /  藍：其他價位' if profile_rectangles else '價量分布暫無有效資料'
    text_at(draw, (left, y + 691 + extra), legend + '  /  虛線：布林軌道', 18, MUTED)
    state = '布林｜' + '；'.join((panel.get('bollinger') or {}).get('signals', ['資料不足'])[:3])
    for i, line in enumerate(wrap(state, 20, CONTENT - 80)[:2]):
        text_at(draw, (left, y + 729 + extra + i * 28), line, 20, INK)
    draw_mark_legend(draw, panel, y + CHART_HEIGHT + extra - 4)


def render_answer(question: str, answer: str, panels: list[dict] | None = None,
                  *, title: str = '艾斯 AI｜研究筆記', demo: bool = False) -> Image.Image:
    panels = panels or []
    question_lines = wrap(clean(question), 31, CONTENT - 12, True)
    header_height = 155 + len(question_lines) * 47
    blocks = body_blocks(answer)
    body_height = sum(b.height for b in blocks) + 68
    height = header_height + sum(panel_height(p) + 24 for p in panels) + body_height + 112
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
        y += panel_height(panel) + 24
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
