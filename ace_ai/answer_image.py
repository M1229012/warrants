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
TILE_BG = '#F6F7F9'
GRID = '#EEF0F3'
UP_BG, DOWN_BG = '#FDECEC', '#E3F5F1'
GOOD_BG, GOOD_INK = '#EAF6F2', '#1F7A64'
WARN_BG, WARN_INK = '#FDF3E7', '#B45309'
ACCENT_BG = '#F8F3EA'
COST_BG, COST_INK = '#EEF0FB', '#4F56A6'
WIDTH = 1440
MARGIN = 64
CONTENT = WIDTH - MARGIN * 2
# K 線卡片垂直配置：標題＋收盤＋均線／布林數值卡｜價格區｜日期軸＋成交量｜價量分布圖例＋布林狀態。
CHART_HEAD = 260
CHART_PRICE_BASE = 306
CHART_VOLUME_BLOCK = 156
CHART_FOOT = 114
CHART_HEIGHT = CHART_HEAD + CHART_PRICE_BASE + CHART_VOLUME_BLOCK + CHART_FOOT
# K 棒價格區加高（原本約 300px，加上標籤帶後 K 棒會被壓扁）。
CHART_PRICE_EXTRA = 240
TILE_H = 98
MA_COLORS = {'MA5': UP, 'MA10': '#D99836', 'MA20': '#6C8B46', 'MA60': '#777AC4'}
BAND_COLORS = {'BB_UPPER': '#667085', 'BB_MID': '#76879A', 'BB_LOWER': '#667085'}
BAND_LABELS = {'BB_UPPER': '布林上軌', 'BB_MID': '中軌 MA20', 'BB_LOWER': '布林下軌'}
# 均量線：避開紅綠量柱的顏色。
MV_COLORS = {'MV5': '#E0A030', 'MV20': '#5B6BBF'}
# 分點買賣標註：K 線上下各留一條標籤帶（▲／▼＋最多 3 列編號圓圈），不和 K 棒重疊。
MARK_LANE = 104
MARK_BADGE_R = 11
MARK_BADGE_ROW = 26
MARK_MAX_ROWS = 3
# 標註明細表：事件多於這個數量時左右兩欄並排。
MARK_TABLE_SINGLE_MAX = 3
MARK_ROW_H = 40
TABLE_HEAD_H = 34


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


_WRAP_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%+\-/_:~]*|\s+|.", re.S)
_NO_LINE_START = set('，。、；：！？）」』】〉》,.;:!?)]}%…～·')
_NO_LINE_END = set('（「『【〈《([{')


def wrap(text: str, size: int, width: int, bold: bool = False) -> list[str]:
    """換行：數字與英文單字整段不拆、標點不放在行首、左括號不留在行尾；單段比整行還寬才逐字切。"""
    face = font(size, bold)
    units: list[str] = []
    for token in _WRAP_TOKEN_RE.findall(text):
        if units and (token[0] in _NO_LINE_START or units[-1][-1] in _NO_LINE_END):
            units[-1] += token
        else:
            units.append(token)
    lines, current = [], ''
    for unit in units:
        if current and face.getlength(current + unit) > width:
            lines.append(current)
            current = ''
        if face.getlength(unit) <= width:
            current += unit
            continue
        for char in unit:
            if current and face.getlength(current + char) > width:
                lines.append(current)
                current = ''
            current += char
    if current:
        lines.append(current)
    return lines or ['']


def fit(text, size: int, width: float, bold: bool = False, minimum: int = 14) -> tuple[str, int]:
    """表格欄位用：先縮字到放得下，縮到最小仍放不下才截斷加「…」。"""
    text = str(text)
    while size > minimum and font(size, bold).getlength(text) > width:
        size -= 1
    if font(size, bold).getlength(text) > width:
        while text and font(size, bold).getlength(text + '…') > width:
            text = text[:-1]
        text += '…'
    return text, size


def number(value, digits=2):
    try:
        value = float(value)
        return f'{value:,.{digits}f}' if math.isfinite(value) else '—'
    except (TypeError, ValueError):
        return '—'


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


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


# ============================================================
# 分點買賣標註（K 線上的 ▲／▼＋編號圓圈，明細表在 K 線卡片底部）
# ============================================================

def _mark_events(panel: dict) -> list[dict]:
    return list(((panel or {}).get('marks') or {}).get('events') or [])


def _has_mark_section(panel: dict) -> bool:
    return bool((panel or {}).get('bars')) and bool((panel or {}).get('marks'))


def _mark_action(e: dict) -> tuple[str, str]:
    """明細表「後續動作」欄：出清日／減碼日落在圖表區間內就寫日期，否則寫目前狀態。"""
    status = str(e.get('status', ''))
    if e.get('exit_date'):
        return f"{e['exit_date'][5:]} 出清", DOWN
    if e.get('reduce_date'):
        return f"{e['reduce_date'][5:]} 減碼・持有", WARN_INK
    if status == '已出清':
        return '已出清', DOWN
    if '減碼' in status:
        return '已減碼・仍持有', WARN_INK
    return '持有中', INK


def _mark_columns(table_width: float) -> list[tuple[str, float]]:
    weights = (('K線編號', 68), ('分點', 136), ('事件', 44), ('買進日', 64), ('權證買進金額', 110), ('後續動作', 182))
    total = sum(w for _, w in weights)
    return [(label, w * table_width / total) for label, w in weights]


def _flow_rows(items: list[float], width: float, gap: float) -> int:
    rows, used = 1, 0.0
    for w in items:
        if used and used + gap + w > width:
            rows, used = rows + 1, 0.0
        used += (gap if used else 0) + w
    return rows


def _mark_symbol_items() -> list[tuple[str, str, float]]:
    """（圖示種類, 文字, 寬度）；圖示用畫的，不依賴字型有沒有 ▲▼ 字形。"""
    specs = (('buy', '買進日（A～E 事件）'), ('exit', '同編號出清日'), ('reduce', '減碼日'),
             ('note', 'K 線上的編號＝下表「K線編號」，同一個編號就是同一筆事件；金額＝當日買進權證金額，不含報酬率'))
    items = []
    for kind, text in specs:
        icon = {'buy': 44, 'exit': 44, 'reduce': 18, 'note': 0}[kind]
        items.append((kind, text, icon + (8 if icon else 0) + font(18).getlength(text)))
    return items


def mark_legend(draw, panel: dict, top: float, dry: bool) -> int:
    """K 線卡片底部的「分點權證買賣標註」：標題＋圖示說明＋明細表（先量測再配置，文字不互疊）。"""
    if not _has_mark_section(panel):
        return 0
    marks = panel.get('marks') or {}
    events = _mark_events(panel)
    x0, width = MARGIN + 36, CONTENT - 72
    h = 22
    title = '分點權證買賣標註'
    title_w = font(24, True).getlength(title)
    rule = str(marks.get('rule', ''))
    inline = font(18).getlength(rule) <= width - title_w - 20
    rule_lines = [] if inline else wrap(rule, 18, width)
    if not dry:
        draw.line((MARGIN + 32, top + 4, WIDTH - MARGIN - 32, top + 4), fill=LINE)
        draw.rectangle((x0, top + h + 4, x0 + 4, top + h + 28), fill=ACCENT)
        text_at(draw, (x0 + 14, top + h), title, 24, INK, True)
        if inline:
            draw.text((x0 + 14 + title_w + 14, top + h + 16), rule, font=font(18), fill=MUTED, anchor='lm')
    h += 40
    for line in rule_lines:
        if not dry:
            text_at(draw, (x0, top + h), line, 18, MUTED)
        h += 28
    items = _mark_symbol_items()
    rows = _flow_rows([w for _, _, w in items], width, 28)
    if not dry:
        sx, sy = x0, top + h + 14
        for kind, text, w in items:
            if sx > x0 and sx + w > x0 + width:
                sx, sy = x0, sy + 32
            cx = sx
            if kind in ('buy', 'exit', 'reduce'):
                color = UP if kind == 'buy' else DOWN
                half = 7
                if kind == 'buy':
                    draw.polygon([(cx + half, sy - half), (cx, sy + half), (cx + 2 * half, sy + half)], fill=color)
                else:
                    draw.polygon([(cx, sy - half), (cx + 2 * half, sy - half), (cx + half, sy + half)], fill=color)
                cx += 18
                if kind != 'reduce':
                    _draw_badge(draw, cx + 4 + MARK_BADGE_R, sy, 'N', color)
                    cx += 26
                cx += 8
            draw.text((cx, sy), text, font=font(18), fill=MUTED if kind == 'note' else INK, anchor='lm')
            sx += w + 28
    h += rows * 32 + 8
    if not events:
        if not dry:
            text_at(draw, (x0, top + h + 4), '圖表區間內沒有符合條件的 A～E 事件', 20, MUTED)
        return int(h + 36 + 18)
    split = len(events) > MARK_TABLE_SINGLE_MAX
    groups = [events[:math.ceil(len(events) / 2)], events[math.ceil(len(events) / 2):]] if split else [events]
    gap = 32
    table_w = (width - gap) / 2 if split else width
    table_top = top + h + 4
    if not dry:
        for g, group in enumerate(groups):
            tx = x0 + g * (table_w + gap)
            _draw_mark_table(draw, tx, table_top, table_w, group)
    h += 4 + TABLE_HEAD_H + len(groups[0]) * MARK_ROW_H + 18
    return int(h)


def _draw_mark_table(draw, x: float, y: float, width: float, events: list[dict]) -> None:
    columns = _mark_columns(width)
    draw.rounded_rectangle((x, y, x + width, y + TABLE_HEAD_H), radius=8, fill=TILE_BG)
    cx = x
    for label, w in columns:
        text, size = fit(label, 17, w - 12)
        if label == 'K線編號':
            draw.text((cx + w / 2, y + TABLE_HEAD_H / 2), text, font=font(size), fill=MUTED, anchor='mm')
        else:
            draw.text((cx + 8, y + TABLE_HEAD_H / 2), text, font=font(size), fill=MUTED, anchor='lm')
        cx += w
    ry = y + TABLE_HEAD_H
    for e in events:
        mid = ry + MARK_ROW_H / 2
        action, action_color = _mark_action(e)
        cells = {
            '分點': (e.get('branch', ''), 19, INK, True),
            '買進日': (e.get('buy_date', '')[5:], 19, INK, False),
            '權證買進金額': (e.get('buy_amount_text', ''), 19, UP, True),
            '後續動作': (action, 18, action_color, action_color != INK),
        }
        cx = x
        for label, w in columns:
            if label == 'K線編號':
                _draw_badge(draw, cx + w / 2, mid, e.get('no', ''), UP)
            elif label == '事件':
                code = str(e.get('event', ''))
                draw.rounded_rectangle((cx + 8, mid - 13, cx + 38, mid + 13), radius=6, fill=ACCENT_BG)
                draw.text((cx + 23, mid), code, font=font(17, True), fill=ACCENT, anchor='mm')
            elif label == '後續動作' and (e.get('exit_date') or e.get('reduce_date')):
                # 和 K 線上方同一個記號：出清＝綠圈同編號、減碼＝綠色 ▼，一眼對得起來。
                if e.get('exit_date'):
                    _draw_badge(draw, cx + 8 + MARK_BADGE_R, mid, e.get('no', ''), DOWN)
                else:
                    draw.polygon([(cx + 8, mid - 7), (cx + 8 + 2 * 11, mid - 7), (cx + 8 + 11, mid + 8)], fill=DOWN)
                value, size, color, bold = cells[label]
                text, size = fit(value, size, w - 20 - 2 * MARK_BADGE_R - 8, bold)
                draw.text((cx + 8 + 2 * MARK_BADGE_R + 8, mid), text, font=font(size, bold), fill=color, anchor='lm')
            else:
                value, size, color, bold = cells[label]
                text, size = fit(value, size, w - 14, bold)
                draw.text((cx + 8, mid), text, font=font(size, bold), fill=color, anchor='lm')
            cx += w
        draw.line((x, ry + MARK_ROW_H, x + width, ry + MARK_ROW_H), fill=LINE)
        ry += MARK_ROW_H


def mark_lanes(panel: dict) -> tuple[int, int]:
    """（上方標籤帶, 下方標籤帶）高度：有出清／減碼才留上方，有買進才留下方，避免空白。"""
    dates = {bar['date'] for bar in (panel or {}).get('bars') or []}
    events = _mark_events(panel)
    top = any(e.get('exit_date') in dates or e.get('reduce_date') in dates for e in events if e.get('exit_date') or e.get('reduce_date'))
    bottom = any(e.get('buy_date') in dates for e in events)
    return (MARK_LANE if top else 0), (MARK_LANE if bottom else 0)


def panel_height(panel: dict) -> int:
    return CHART_HEIGHT + CHART_PRICE_EXTRA + sum(mark_lanes(panel)) + mark_legend(None, panel, 0, True)


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


# ============================================================
# K 線卡片
# ============================================================

def draw_value_tiles(draw, x: float, y: float, width: float, last: dict) -> None:
    """均線 4 格＋布林 3 格數值卡：色線圖例、數值、收盤相對位置（站上／跌破、高於／低於）。"""
    tiles = [(k, k, c, False) for k, c in MA_COLORS.items()] + [(k, BAND_LABELS[k], c, True) for k, c in BAND_COLORS.items()]
    gap, group_gap = 10, 28
    w = (width - group_gap - gap * (len(tiles) - 2)) / len(tiles)
    close = _finite(last.get('Close'))
    tx = x
    for i, (key, label, color, dashed) in enumerate(tiles):
        if i == len(MA_COLORS):
            tx += group_gap - gap
        draw.rounded_rectangle((tx, y, tx + w, y + TILE_H), radius=12, fill=TILE_BG)
        sy = y + 23
        if dashed:
            for s in (14, 21, 28):
                draw.line((tx + s, sy, tx + s + 4, sy), fill=color, width=3)
        else:
            draw.line((tx + 14, sy, tx + 32, sy), fill=color, width=3)
        text, size = fit(label, 18, w - 54)
        draw.text((tx + 40, sy), text, font=font(size), fill=MUTED, anchor='lm')
        value = _finite(last.get(key))
        text, size = fit(number(value), 26, w - 28, True)
        text_at(draw, (tx + 14, y + 36), text, size, INK, True)
        if close is not None and value:
            pct = (close / value - 1) * 100
            word = ('站上' if pct >= 0 else '跌破') if not dashed else ('高於' if pct >= 0 else '低於')
            text, size = fit(f'{word} {pct:+.1f}%', 17, w - 28)
            text_at(draw, (tx + 14, y + 70), text, size, UP if pct >= 0 else DOWN)
        tx += w + gap


def _date_ticks(bars: list, step: float) -> list[int]:
    """每 5 根（間距太窄時 10、15…）標一個日期，從最後一根往回數，確保最新一天一定有日期。"""
    interval = 5 * max(1, math.ceil(math.ceil(64 / max(step, 1e-6)) / 5))
    return sorted(range(len(bars) - 1, -1, -interval))


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
    draw.text((x1 - 32, y + 34), f"資料至 {last['date']}", font=font(22), fill=MUTED, anchor='rt')
    color = UP if (change or 0) >= 0 else DOWN
    draw.text((x0 + 32, y + 112), '收盤', font=font(22), fill=MUTED, anchor='ls')
    price_x = x0 + 32 + font(22).getlength('收盤') + 12
    price = number(last['Close'])
    draw.text((price_x, y + 112), price, font=font(40, True), fill=color, anchor='ls')
    if change is not None:
        chip = f'{change:+.2f}%'
        chip_x = price_x + font(40, True).getlength(price) + 16
        chip_w = font(22, True).getlength(chip) + 28
        draw.rounded_rectangle((chip_x, y + 80, chip_x + chip_w, y + 116), radius=18, fill=UP_BG if change >= 0 else DOWN_BG)
        draw.text((chip_x + chip_w / 2, y + 98), chip, font=font(22, True), fill=color, anchor='mm')
    draw_value_tiles(draw, x0 + 32, y + 136, CONTENT - 64, last)

    lane_top, lane_bottom = mark_lanes(panel)
    extra = lane_top + lane_bottom + CHART_PRICE_EXTRA
    left, right = x0 + 36, x1 - 118
    top = y + CHART_HEAD
    bottom = top + CHART_PRICE_BASE + extra
    # 有標註時價格只畫在中間，上方標籤帶放 ▼／綠圈、下方標籤帶放 ▲／紅圈（沒有就不留白）。
    price_top, price_bottom = top + lane_top, bottom - lane_bottom
    lows = [b['Low'] for b in bars]
    highs = [b['High'] for b in bars]
    for b in bars:
        for key in list(MA_COLORS) + list(BAND_COLORS):
            if b.get(key) is not None:
                lows.append(b[key]); highs.append(b[key])
    low, high = min(lows), max(highs)
    padding = max((high - low) * .08, abs(high) * .005, .01)
    low -= padding; high += padding
    py = lambda value: price_bottom - (value - low) / (high - low) * (price_bottom - price_top)
    step = (right - left) / len(bars)
    px = lambda i: left + (i + .5) * step
    ticks = _date_ticks(bars, step)
    profile_rectangles = volume_profile_rectangles(panel.get('volume_profile') or {}, left, right, py, low, high)
    for rectangle, fill in profile_rectangles:
        draw.rectangle(rectangle, fill=fill)
    for i in ticks:
        draw.line((px(i), top, px(i), bottom), fill=GRID, width=1)
    for i in range(5):
        value = low + (high - low) * i / 4
        gy = py(value)
        draw.line((left, gy, right, gy), fill=LINE, width=1)
        text_at(draw, (right + 14, gy - 10), number(value), 20, MUTED)
    draw_marks(draw, panel, px, py, step, price_top, price_bottom)
    for i, bar in enumerate(bars):
        candle = UP if bar['Close'] >= bar['Open'] else DOWN
        center = px(i)
        draw.line((center, py(bar['High']), center, py(bar['Low'])), fill=candle, width=2)
        a, b = sorted((py(bar['Open']), py(bar['Close'])))
        half = max(1, step * .30)
        draw.rectangle((center - half, a, center + half, max(a + 2, b)), fill=candle)
    for key, line_color in MA_COLORS.items():
        segment = []
        for i, bar in enumerate(bars):
            if bar.get(key) is None:
                if len(segment) > 1:
                    draw.line(segment, fill=line_color, width=2)
                segment = []
            else:
                segment.append((px(i), py(bar[key])))
        if len(segment) > 1:
            draw.line(segment, fill=line_color, width=2)
    # Dashed bands, including the middle band, use report-supplied values.
    # A missing rolling value breaks the line instead of joining across a gap.
    for key, band_color in BAND_COLORS.items():
        for i in range(1, len(bars)):
            a, b = bars[i-1].get(key), bars[i].get(key)
            if a is None or b is None:
                continue
            xa, ya, xb, yb = px(i-1), py(a), px(i), py(b)
            distance = math.hypot(xb-xa, yb-ya)
            for start in range(0, max(1, math.ceil(distance)), 12):
                t0, t1 = min(start/max(distance, 1), 1), min((start+7)/max(distance, 1), 1)
                draw.line((xa+(xb-xa)*t0, ya+(yb-ya)*t0, xa+(xb-xa)*t1, ya+(yb-ya)*t1), fill=band_color, width=2)

    # 日期軸緊貼價格區下方；月份切換的日期用粗體，方便看出 K 棒落在哪個月。
    axis_y = bottom + 2
    draw.line((left, axis_y, right, axis_y), fill=LINE, width=1)
    previous_month = None
    for i in ticks:
        x = px(i)
        draw.line((x, axis_y, x, axis_y + 5), fill='#98A2B3', width=1)
        month = bars[i]['date'][5:7]
        new_month = month != previous_month
        previous_month = month
        label_x = max(left + 26, min(x, right - 26))
        draw.text((label_x, axis_y + 9), bars[i]['date'][5:], font=font(18, new_month),
                  fill=INK if new_month else MUTED, anchor='mt')

    # 成交量標題列：今日量＋均量線圖例（單位張，Volume 為股數）。
    label_y = bottom + 58
    draw.text((left, label_y), '成交量', font=font(20), fill=MUTED, anchor='lm')
    lx = left + font(20).getlength('成交量') + 18
    today_volume = _finite(last.get('Volume'))
    if today_volume is not None:
        text = f'今日 {today_volume / 1000:,.0f} 張'
        draw.text((lx, label_y), text, font=font(19, True), fill=INK, anchor='lm')
        lx += font(19, True).getlength(text) + 24
    for key, mv_color in MV_COLORS.items():
        value = _finite(last.get(key))
        draw.line((lx, label_y, lx + 20, label_y), fill=mv_color, width=3)
        text = f"{key} {value / 1000:,.0f} 張" if value is not None else f'{key} —'
        draw.text((lx + 28, label_y), text, font=font(19), fill=INK, anchor='lm')
        lx += 28 + font(19).getlength(text) + 24
    vtop, vbottom = bottom + 84, bottom + CHART_VOLUME_BLOCK
    for i in ticks:
        draw.line((px(i), vtop, px(i), vbottom), fill=GRID, width=1)
    maximum = max([b.get('Volume') or 0 for b in bars] + [b.get(k) or 0 for b in bars for k in MV_COLORS] + [1])
    vy = lambda value: vbottom - max(0, value) / maximum * (vbottom - vtop)
    for i, bar in enumerate(bars):
        draw.rectangle((px(i) - step * .3, vy(bar.get('Volume') or 0), px(i) + step * .3, vbottom),
                       fill=UP if bar['Close'] >= bar['Open'] else DOWN)
    for key, mv_color in MV_COLORS.items():
        segment = []
        for i, bar in enumerate(bars):
            value = _finite(bar.get(key))
            if value is None:
                if len(segment) > 1:
                    draw.line(segment, fill=mv_color, width=2)
                segment = []
            else:
                segment.append((px(i), vy(value)))
        if len(segment) > 1:
            draw.line(segment, fill=mv_color, width=2)
    legend = '價量分布｜紅：最大量區  /  橘：第二大量區  /  藍：其他價位' if profile_rectangles else '價量分布暫無有效資料'
    text_at(draw, (left, vbottom + 18), legend + '  /  虛線：布林軌道', 18, MUTED)
    state = '布林｜' + '；'.join((panel.get('bollinger') or {}).get('signals', ['資料不足'])[:3])
    for i, line in enumerate(wrap(state, 20, CONTENT - 80)[:2]):
        text_at(draw, (left, vbottom + 54 + i * 28), line, 20, INK)
    mark_legend(draw, panel, y + CHART_HEIGHT + extra, False)


# ============================================================
# 型態評分卡（型態／成本／操作類問題；數字全部來自 weekly_pick.build_pattern_scorecard）
# ============================================================

GRADE_STYLE = {'結構偏強': (GOOD_BG, GOOD_INK), '結構中性': (TILE_BG, '#344054'), '結構偏弱': (WARN_BG, WARN_INK)}
LEVEL_STYLE = {'壓力': (UP_BG, UP), '現價': (ACCENT_BG, ACCENT), '成本': (COST_BG, COST_INK), '支撐': (DOWN_BG, DOWN)}
LEVEL_ROW_H = 38
LEVEL_MAX_RESISTANCES, LEVEL_MAX_SUPPORTS, BRANCH_MAX_ROWS, REASON_MAX_ITEMS = 2, 3, 4, 3
SCORE_ROW_H = 30
BRANCH_ROW_H = 38


def _sub_heading(draw, x, y, title, note, width, dry) -> int:
    """小標題＋右側灰字說明；說明放不下時換到下一行。"""
    title_w = font(25, True).getlength(title)
    inline = font(18).getlength(note) <= width - title_w - 40
    lines = [] if inline or not note else wrap(note, 18, width)
    if not dry:
        draw.rectangle((x, y + 5, x + 4, y + 29), fill=ACCENT)
        text_at(draw, (x + 14, y), title, 25, INK, True)
        if inline and note:
            draw.text((x + 14 + title_w + 14, y + 17), note, font=font(18), fill=MUTED, anchor='lm')
        for i, line in enumerate(lines):
            text_at(draw, (x, y + 42 + i * 28), line, 18, MUTED)
    return 46 + len(lines) * 28


def _tag_rows(tags: list[tuple[str, str]], width: float) -> list[list[tuple[str, str, float]]]:
    rows, row, used = [], [], 0.0
    for key, value in tags:
        w = font(21).getlength(f'{key}｜') + font(21, True).getlength(value) + 28
        if row and used + w > width:
            rows.append(row)
            row, used = [], 0.0
        row.append((key, value, min(w, width)))
        used += w + 10
    if row:
        rows.append(row)
    return rows


def _level_rows(card: dict) -> list[tuple[str, str, float, float | None]]:
    rows = []
    for lv in (card.get('resistances_above_close') or [])[:LEVEL_MAX_RESISTANCES]:
        rows.append(('壓力', lv.get('label', ''), lv.get('price'), lv.get('distance_from_close_pct')))
    for lv in (card.get('supports_below_close') or [])[:LEVEL_MAX_SUPPORTS]:
        rows.append(('支撐', lv.get('label', ''), lv.get('price'), lv.get('distance_from_close_pct')))
    close = _finite(card.get('close'))
    if close:
        rows.append(('現價', '收盤', close, 0.0))
        cost = _finite(card.get('cost_price'))
        if cost:
            rows.append(('成本', '持股成本', cost, (cost / close - 1) * 100))
    order = {'壓力': 0, '現價': 1, '成本': 2, '支撐': 3}
    rows = [r for r in rows if _finite(r[2]) is not None]
    return sorted(rows, key=lambda r: (-float(r[2]), order[r[0]]))


def _deduction_outlook(info: dict) -> tuple[str, str]:
    """（推算文字, 顏色）：收盤維持不變時均線會不會轉向。"""
    if info.get('turn'):
        day = info.get('turn_day')
        text = info.get('turn_text') or f"{ {1: '明天起', 2: '後天起'}.get(day, f'{day} 個交易日後') }{info['turn']}"
        return text, WARN_INK if info['turn'] == '轉下彎' else GOOD_INK
    return {'上揚': ('續揚', INK), '下彎': ('續彎', INK)}.get(info.get('direction_now'), ('走平', MUTED))


def _level_note(label: str, card: dict) -> tuple[str, str]:
    """關鍵價位「說明」欄：均線寫方向與扣抵推算，量區／布林寫價位性質。"""
    info = (card.get('ma_deduction') or {}).get('MA20' if label == '布林中軌' else label)
    if info:
        outlook, color = _deduction_outlook(info)
        return f"{'MA20 ' if label == '布林中軌' else ''}目前{info.get('direction_now', '')}｜收盤不變：{outlook}", color
    if '量區' in label:
        return '成交密集區邊緣（籌碼成本區）', MUTED
    if '布林' in label:
        return '布林通道軌道（20 日 ±2 倍標準差）', MUTED
    if label == '持股成本':
        return '使用者輸入的成本價', MUTED
    return '', MUTED


def _draw_level_table(draw, x, y, width, rows, card) -> None:
    draw.rounded_rectangle((x, y, x + width, y + TABLE_HEAD_H), radius=8, fill=TILE_BG)
    mid_head = y + TABLE_HEAD_H / 2
    for label, lx, anchor in (('類型', x + 10, 'lm'), ('名稱', x + 114, 'lm'), ('價位', x + 470, 'rm'),
                              ('距現價', x + 590, 'rm'), ('說明（均線含扣抵推算）', x + 640, 'lm')):
        draw.text((lx, mid_head), label, font=font(17), fill=MUTED, anchor=anchor)
    ry = y + TABLE_HEAD_H
    for kind, label, price, pct in rows:
        mid = ry + LEVEL_ROW_H / 2
        bg, ink = LEVEL_STYLE[kind]
        if kind == '現價':
            draw.rectangle((x, ry, x + width, ry + LEVEL_ROW_H), fill='#FBF8F2')
        draw.rounded_rectangle((x + 10, mid - 15, x + 76, mid + 15), radius=15, fill=bg)
        draw.text((x + 43, mid), kind, font=font(18, True), fill=ink, anchor='mm')
        text, size = fit(label, 21, 210, kind == '現價')
        draw.text((x + 114, mid), text, font=font(size, kind == '現價'), fill=INK, anchor='lm')
        draw.text((x + 470, mid), number(price), font=font(22, True), fill=INK, anchor='rm')
        if kind != '現價' and pct is not None:
            pct = float(pct)
            draw.text((x + 590, mid), f'{pct:+.2f}%', font=font(20), fill=UP if pct > 0 else DOWN if pct < 0 else MUTED, anchor='rm')
        if kind != '現價':
            note, color = _level_note(label, card)
            if note:
                text, size = fit(note, 19, width - 660, color != MUTED)
                draw.text((x + 640, mid), text, font=font(size, color != MUTED), fill=color, anchor='lm')
        draw.line((x, ry + LEVEL_ROW_H, x + width, ry + LEVEL_ROW_H), fill=LINE)
        ry += LEVEL_ROW_H


_REASON_POINTS_RE = re.compile(r"（([^（）]*) ([\d.]+)/([\d.]+)）$")


def _short_reasons(items, lost: bool) -> list[str]:
    """「說明（小項 得分/滿分）」→「說明 得分/滿分」，依影響大小取前幾項（失分看少拿幾分、得分看拿到幾分）。"""
    parsed = []
    for text in items or []:
        match = _REASON_POINTS_RE.search(text)
        if match:
            points, maximum = float(match.group(2)), float(match.group(3))
            parsed.append((maximum - points if lost else points, f"{text[:match.start()]} {match.group(2)}/{match.group(3)}"))
        else:
            parsed.append((0.0, text))
    parsed.sort(key=lambda item: -item[0])
    return [text for _, text in parsed[:REASON_MAX_ITEMS]]


def _reason_column(draw, x, y, width, title, items, ink, empty, dry) -> int:
    lines = [wrap('・' + t, 20, width) for t in items] or [wrap(empty, 20, width)]
    if not dry:
        text_at(draw, (x, y), title, 21, ink, True)
        cy = y + 34
        for group in lines:
            for row in group:
                text_at(draw, (x, cy), row, 20, ink if items else MUTED)
                cy += 30
    return 34 + sum(len(g) for g in lines) * 30


def _deduction_chips(draw, x, y, width, card, dry) -> int:
    """均線扣抵濃縮成一列：MA5 續揚｜MA20 第 3 日轉下彎…（細節在關鍵價位說明欄與 AI 文字）。"""
    deduction = card.get('ma_deduction') or {}
    if not deduction:
        return 0
    label = '均線扣抵（收盤不變推算）'
    chips = []
    for key, info in deduction.items():
        outlook, color = _deduction_outlook(info)
        chips.append((f'{key} {outlook}', color))
    widths = [font(19, True).getlength(t) + 26 for t, _ in chips]
    start = x + font(20, True).getlength(label) + 16
    rows = _flow_rows(widths, width - (start - x), 10)
    if not dry:
        draw.text((x, y + 17), label, font=font(20, True), fill=INK, anchor='lm')
        cx, cy = start, y
        for (text, color), w in zip(chips, widths):
            if cx > start and cx + w > x + width:
                cx, cy = start, cy + 44
            draw.rounded_rectangle((cx, cy, cx + w, cy + 34), radius=17, fill=TILE_BG, outline=LINE)
            draw.text((cx + w / 2, cy + 17), text, font=font(19, True), fill=color if color != MUTED else INK, anchor='mm')
            cx += w + 10
    return rows * 44


def _draw_branch_table(draw, x, y, width, branches) -> None:
    columns = (('分點', 250), ('總勝率', 110), ('最近 A～E 事件', 290), ('區間事件買進', 160), ('部位狀態', 180), ('最近賣出', width - 990))
    draw.rounded_rectangle((x, y, x + width, y + TABLE_HEAD_H), radius=8, fill=TILE_BG)
    cx = x
    for label, w in columns:
        draw.text((cx + 10, y + TABLE_HEAD_H / 2), label, font=font(17), fill=MUTED, anchor='lm')
        cx += w
    ry = y + TABLE_HEAD_H
    for row in branches:
        mid = ry + BRANCH_ROW_H / 2
        status = str(row.get('status', ''))
        status_color = {'持有中': INK, '持有中・近期有賣出': WARN_INK, '已出清': DOWN}.get(status, MUTED)
        win = _finite(row.get('overall_win_rate'))
        cells = [
            None,
            (f'{win:.2f}%' if win is not None else '—', 20, INK, False),
            (row.get('latest_event', ''), 19, INK, False),
            (row.get('event_buy_amount_text', '-'), 20, UP, True),
            (status, 19, status_color, True),
            (row.get('latest_sell') or '—', 19, MUTED if not row.get('latest_sell') else INK, False),
        ]
        cx = x
        for (label, w), cell in zip(columns, cells):
            if cell is None:
                chip_w = font(15, True).getlength('高勝率') + 16 if row.get('is_high_win_rate') else 0
                text, size = fit(row.get('branch', ''), 21, w - 20 - (chip_w + 8 if chip_w else 0), True)
                draw.text((cx + 10, mid), text, font=font(size, True), fill=INK, anchor='lm')
                if chip_w:
                    chip_x = cx + 10 + font(size, True).getlength(text) + 8
                    draw.rounded_rectangle((chip_x, mid - 12, chip_x + chip_w, mid + 12), radius=12, fill=ACCENT_BG)
                    draw.text((chip_x + chip_w / 2, mid), '高勝率', font=font(15, True), fill=ACCENT, anchor='mm')
            else:
                value, size, color, bold = cell
                text, size = fit(value, size, w - 20, bold)
                draw.text((cx + 10, mid), text, font=font(size, bold), fill=color, anchor='lm')
            cx += w
        draw.line((x, ry + BRANCH_ROW_H, x + width, ry + BRANCH_ROW_H), fill=LINE)
        ry += BRANCH_ROW_H


def scorecard(draw, y: float, card: dict, dry: bool) -> int:
    """型態分數＋五大項＋主要得分／失分＋均線扣抵一列＋關鍵價位＋追蹤分點動向（精簡版）；dry=True 只量測高度。"""
    x0, x1, pad = MARGIN, WIDTH - MARGIN, 36
    px, width = x0 + pad, CONTENT - pad * 2
    if not dry:
        draw.rounded_rectangle((x0, y, x1, y + scorecard(None, 0, card, True)), radius=20, fill='white', outline=LINE)
    h = 30
    h += _sub_heading(draw, px, y + h, '型態評分', '規則同本週精選｜只評技術結構，不含籌碼，不是買賣建議', width, dry) + 10

    score = _finite(card.get('pattern_score')) or 0.0
    grade = str(card.get('grade', ''))
    tags = [('型態', str(card.get('pattern_label', ''))), ('均線', str(card.get('ma_alignment', '') or '—'))]
    cost = _finite(card.get('cost_price'))
    if cost:
        unrealized = _finite(card.get('unrealized_pct'))
        tags.append(('持股成本', f'{number(cost)}（現價相對成本 {unrealized:+.2f}%）' if unrealized is not None else number(cost)))
    tag_rows = _tag_rows(tags, width)
    score_block = max(146, 12 + len(card.get('components') or []) * SCORE_ROW_H + 8)
    if not dry:
        sy = y + h
        score_text = f'{score:.1f}'
        draw.text((px, sy + 76), score_text, font=font(72, True), fill=ACCENT, anchor='ls')
        draw.text((px + font(72, True).getlength(score_text) + 10, sy + 76), '/ 100', font=font(22), fill=MUTED, anchor='ls')
        bg, ink = GRADE_STYLE.get(grade, (TILE_BG, INK))
        grade_w = font(22, True).getlength(grade) + 36
        draw.rounded_rectangle((px, sy + 98, px + grade_w, sy + 138), radius=20, fill=bg)
        draw.text((px + grade_w / 2, sy + 118), grade, font=font(22, True), fill=ink, anchor='mm')
        rx, rw = px + 380, width - 380
        # 五大項：左側項目名、中間進度條、右側得分，一項一列。
        for k, component in enumerate(card.get('components') or []):
            cy = sy + 12 + k * SCORE_ROW_H
            value, maximum = _finite(component.get('value')) or 0.0, float(component.get('max') or 1)
            draw.text((rx, cy), str(component.get('label', '')), font=font(20), fill=MUTED, anchor='lm')
            bar_left, bar_right = rx + 110, rx + rw - 120
            draw.rounded_rectangle((bar_left, cy - 6, bar_right, cy + 6), radius=6, fill=LINE)
            filled = max(0.0, min(1.0, value / maximum)) * (bar_right - bar_left)
            if filled > 12:
                draw.rounded_rectangle((bar_left, cy - 6, bar_left + filled, cy + 6), radius=6, fill=ACCENT)
            draw.text((rx + rw, cy), f'{value:g} / {maximum:g}', font=font(20, True), fill=INK, anchor='rm')
        ty = sy + score_block
        for row in tag_rows:
            tx = px
            for key, value, w in row:
                draw.rounded_rectangle((tx, ty, tx + w, ty + 38), radius=19, fill=TILE_BG, outline=LINE)
                draw.text((tx + 14, ty + 19), f'{key}｜', font=font(21), fill=MUTED, anchor='lm')
                text, size = fit(value, 21, w - 28 - font(21).getlength(f'{key}｜'), True)
                draw.text((tx + 14 + font(21).getlength(f'{key}｜'), ty + 19), text, font=font(size, True), fill=INK, anchor='lm')
                tx += w + 10
            ty += 48
    h += score_block + len(tag_rows) * 48 + 14

    half = (width - 40) / 2
    plus = _short_reasons(card.get('plus_reasons'), lost=False)
    minus = _short_reasons(card.get('minus_reasons'), lost=True)
    reasons_h = 18 + max(_reason_column(None, 0, 0, half - 24, '主要得分', plus, GOOD_INK, '沒有拿到一半以上的項目', True),
                         _reason_column(None, 0, 0, half - 24, '主要失分', minus, WARN_INK, '各項都拿到一半以上', True)) + 16
    if not dry:
        draw.rounded_rectangle((px, y + h, px + half, y + h + reasons_h), radius=14, fill=GOOD_BG)
        draw.rounded_rectangle((px + half + 40, y + h, px + width, y + h + reasons_h), radius=14, fill=WARN_BG)
        _reason_column(draw, px + 20, y + h + 18, half - 24, '主要得分', plus, GOOD_INK, '沒有拿到一半以上的項目', False)
        _reason_column(draw, px + half + 60, y + h + 18, half - 24, '主要失分', minus, WARN_INK, '各項都拿到一半以上', False)
    h += reasons_h + 20
    h += _deduction_chips(draw, px, y + h, width, card, dry) + 18

    levels = _level_rows(card)
    h += _sub_heading(draw, px, y + h, '關鍵價位', '均線、兩大量區上下緣、布林三軌中，離收盤最近的壓力與支撐', width, dry)
    if levels:
        if not dry:
            _draw_level_table(draw, px, y + h, width, levels, card)
        h += TABLE_HEAD_H + len(levels) * LEVEL_ROW_H + 26
    else:
        if not dry:
            text_at(draw, (px, y + h), '目前沒有可用的價位資料', 21, MUTED)
        h += 34 + 34

    branches = (card.get('tracked_branches') or [])[:BRANCH_MAX_ROWS]
    period = card.get('tracked_branches_period') or ''
    note = '回測追蹤分點（高勝率優先）' + (f'｜{period}' if period else '')
    h += _sub_heading(draw, px, y + h, '追蹤分點動向', note, width, dry)
    if branches:
        if not dry:
            _draw_branch_table(draw, px, y + h, width, branches)
        h += TABLE_HEAD_H + len(branches) * BRANCH_ROW_H + 30
    else:
        if not dry:
            text_at(draw, (px, y + h), '近 20 個交易日追蹤分點在這檔股票沒有 A～E 事件或賣出紀錄', 21, MUTED)
        h += 34 + 30
    return int(h)


def panel_block_height(panel: dict) -> int:
    card = panel.get('scorecard')
    return panel_height(panel) + 24 + (scorecard(None, 0, card, True) + 24 if card else 0)


def render_answer(question: str, answer: str, panels: list[dict] | None = None,
                  *, title: str = '艾斯 AI｜研究筆記', demo: bool = False) -> Image.Image:
    panels = panels or []
    question_lines = wrap(clean(question), 31, CONTENT - 12, True)
    header_height = 155 + len(question_lines) * 47
    blocks = body_blocks(answer)
    body_height = sum(b.height for b in blocks) + 68
    height = header_height + sum(panel_block_height(p) for p in panels) + body_height + 112
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
        if panel.get('scorecard'):
            scorecard(draw, y, panel['scorecard'], False)
            y += scorecard(None, 0, panel['scorecard'], True) + 24
    draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + body_height), radius=20, fill='white', outline=LINE)
    cursor = y + 30
    for block in blocks:
        if block.kind == 'heading':
            # 利多／利空區塊用台股慣用紅／綠色條區分，其餘維持金色。
            heading = ''.join(block.lines)
            bar = DOWN if '利空' in heading else UP if '利多' in heading else ACCENT
            draw.rectangle((MARGIN + 30, cursor + 4, MARGIN + 34, cursor + 29), fill=bar)
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
