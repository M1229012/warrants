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
COMPACT_PRICE_EXTRA = 40      # 兩檔比較時 K 線價格區縮短
TILE_BANDS = ('BB_UPPER', 'BB_LOWER')   # 布林中軌就是 MA20，數值卡與線條都不重複
TILE_H = 98
MA_COLORS = {'MA5': UP, 'MA10': '#D99836', 'MA20': '#6C8B46', 'MA60': '#777AC4'}
BAND_COLORS = {'BB_UPPER': '#667085', 'BB_MID': '#76879A', 'BB_LOWER': '#667085'}
BAND_LABELS = {'BB_UPPER': '布林上軌', 'BB_MID': '中軌 MA20', 'BB_LOWER': '布林下軌'}
# 均量線：避開紅綠量柱的顏色。
MV_COLORS = {'MV5': '#E0A030', 'MV20': '#5B6BBF'}
# 分點買賣標註：K 線上下各留一條標籤帶（▲／▼＋最多 3 列編號圓圈），不和 K 棒重疊。
# 每個分點一個顏色：K 線上的三角形與編號都用這個顏色，圖例才看得出誰是誰。
BRANCH_COLORS = ('#C2410C', '#1D4ED8', '#047857', '#7C3AED', '#B45309', '#BE185D')
MARK_LANE = 104
MARK_BADGE_R = 11
MARK_BADGE_ROW = 26
MARK_MAX_ROWS = 3
# 標註明細表：事件多於這個數量時左右兩欄並排。
MARK_TABLE_SINGLE_MAX = 3
MARK_ROW_H = 40
TABLE_HEAD_H = 34
# 與週報 add_center_watermarks 相同的文字、藏青色、透明度與角度。
CENTER_WATERMARK_TEXT = '股市艾斯\n台股DC討論群'
CENTER_WATERMARK_COLOR = '#1D2B44'
CENTER_WATERMARK_ALPHA = 0.06
CENTER_WATERMARK_FONT_SIZE = 200
CENTER_WATERMARK_ROTATION = 18


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
            small = raw.startswith(('資料時間', '※', '資料來源', '⚠️', '🧡'))
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

def _branch_palette(events: list[dict]) -> dict:
    """依出現順序配色；分點多於配色數時循環使用。"""
    names, palette = [], {}
    for event in events or []:
        name = str(event.get('branch') or '').strip()
        if name and name not in names:
            names.append(name)
    for index, name in enumerate(names):
        palette[name] = BRANCH_COLORS[index % len(BRANCH_COLORS)]
    return palette


def _mark_events(panel: dict) -> list[dict]:
    return list(((panel or {}).get('marks') or {}).get('events') or [])


def _has_mark_section(panel: dict) -> bool:
    return bool((panel or {}).get('bars')) and bool(_mark_events(panel)) and not (panel or {}).get('compact')


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
    """K 線卡片底部的分點標註說明。

    無資料時整段不畫。flow（實際買賣超點位）採 3034 版型的精簡圖例，
    不再把逐日流水展開成長表；詳細資料留在 LOG／文字分析，避免週精選圖片被表格撐長。
    event 模式仍保留既有事件明細表。
    """
    if not _has_mark_section(panel):
        return 0
    marks = panel.get('marks') or {}
    events = _mark_events(panel)
    mode = str(marks.get('mode') or 'event')
    x0, width = MARGIN + 36, CONTENT - 72
    h = 22
    if not dry:
        draw.line((MARGIN + 32, top + 4, WIDTH - MARGIN - 32, top + 4), fill=LINE)
        draw.rectangle((x0, top + h + 4, x0 + 4, top + h + 28), fill=ACCENT)
        text_at(draw, (x0 + 14, top + h), '分點權證買賣標註', 24, INK, True)
    h += 44

    if mode == 'flow':
        # 週精選／明確詢問買賣超點位：一個分點一個顏色，圖例直接對應 K 線上的編號。
        palette = _branch_palette(events)
        rows = []
        for name, color in palette.items():
            items = [e for e in events if str(e.get('branch') or '').strip() == name]
            numbered = sorted({int(e['no']) for e in items if str(e.get('no', '')) != ''})
            buys = [e for e in items if e.get('action') == 'buy']
            exits = [e for e in items if e.get('action') != 'buy' and str(e.get('no', '')) != '']
            plain = [e for e in items if e.get('action') != 'buy' and str(e.get('no', '')) == '']
            exited = {int(e['no']) for e in exits if str(e.get('no', '')) != ''}
            holding = [e for e in buys if str(e.get('no', '')) != '' and int(e['no']) not in exited]
            detail = "　".join(x for x in (
                f"事件買進 {len(buys)} 筆" if buys else "",
                f"已出清 {len(exits)} 筆" if exits else "",
                f"持有中 {len(holding)} 筆" if holding else "",
                f"減碼 {len(plain)} 筆" if plain else "") if x)
            prefix = "編號 " + "、".join(str(n) for n in numbered) if numbered else "本期無 A～E 事件"
            rows.append((name, color, f"{prefix}｜{detail}" if detail else prefix))
        if not dry:
            sy = top + h + 7
            half = 7
            draw.polygon([(x0 + half, sy - half), (x0, sy + half), (x0 + 2 * half, sy + half)], fill=MUTED)
            draw.text((x0 + 22, sy), '買超', font=font(18), fill=INK, anchor='lm')
            sx = x0 + 110
            draw.polygon([(sx, sy - half), (sx + 2 * half, sy - half), (sx + half, sy + half)], fill=MUTED)
            draw.text((sx + 22, sy), '賣超', font=font(18), fill=INK, anchor='lm')
            draw.text((x0 + 232, sy), '編號＝A～E 事件（出清與買進同號）；無編號＝減碼或零星賣出',
                      font=font(18), fill=MUTED, anchor='lm')
            ly = sy + 30
            for name, color, detail in rows:
                draw.ellipse((x0, ly - 7, x0 + 14, ly + 7), fill=color)
                draw.text((x0 + 24, ly), name, font=font(19, True), fill=INK, anchor='lm')
                draw.text((x0 + 24 + font(19, True).getlength(name) + 16, ly), detail,
                          font=font(18), fill=MUTED, anchor='lm')
                ly += 30
        return int(h + 38 + len(rows) * 30)

    table_top = top + h
    split = len(events) > MARK_TABLE_SINGLE_MAX
    groups = [events[:math.ceil(len(events) / 2)], events[math.ceil(len(events) / 2):]] if split else [events]
    gap = 32
    table_w = (width - gap) / 2 if split else width
    if not dry:
        for g, group in enumerate(groups):
            tx = x0 + g * (table_w + gap)
            _draw_mark_table(draw, tx, table_top, table_w, group)
    h += TABLE_HEAD_H + len(groups[0]) * MARK_ROW_H + 18
    return int(h)


def _draw_flow_mark_table(draw, x: float, y: float, width: float, events: list[dict], head_h: int = 38, row_h: int = 44) -> None:
    columns = [('編號', 70), ('分點', 230), ('日期', 110), ('方向', 100), ('淨買賣超', 170)]
    total = sum(w for _, w in columns)
    columns = [(label, w * width / total) for label, w in columns]
    draw.rounded_rectangle((x, y, x + width, y + head_h), radius=8, fill=TILE_BG)
    cx = x
    for label, w in columns:
        draw.text((cx + (w / 2 if label in ('編號', '方向') else 10), y + head_h / 2), label, font=font(17), fill=MUTED, anchor='mm' if label in ('編號', '方向') else 'lm')
        cx += w
    ry = y + head_h
    for e in events:
        mid = ry + row_h / 2
        action = str(e.get('action') or '')
        color = UP if action == 'buy' else DOWN
        values = {
            '分點': str(e.get('branch') or ''),
            '日期': str(e.get('action_date') or '')[5:],
            '方向': str(e.get('action_text') or ('買超' if action == 'buy' else '賣超')),
            '淨買賣超': str(e.get('net_amount_text') or ''),
        }
        cx = x
        for label, w in columns:
            if label == '編號':
                _draw_badge(draw, cx + w / 2, mid, e.get('no', ''), color)
            else:
                anchor = 'mm' if label == '方向' else 'lm'
                tx = cx + (w / 2 if anchor == 'mm' else 10)
                draw.text((tx, mid), values[label], font=font(18, label in ('分點','淨買賣超')), fill=color if label in ('方向','淨買賣超') else INK, anchor=anchor)
            cx += w
        draw.line((x, ry + row_h, x + width, ry + row_h), fill=LINE)
        ry += row_h


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
    """（上方標籤帶, 下方標籤帶）高度。

    event 模式：出清／減碼在上、買進在下。
    flow 模式：賣超在上、買超在下。兩種模式都必須預留標記空間，
    避免三角形與編號圓圈壓到均線數值卡或 K 棒。
    """
    if (panel or {}).get('compact'):
        return 0, 0
    dates = {bar['date'] for bar in (panel or {}).get('bars') or []}
    events = _mark_events(panel)
    mode = str(((panel or {}).get('marks') or {}).get('mode') or 'event')
    if mode == 'flow':
        top = any(e.get('action') == 'sell' and e.get('action_date') in dates for e in events)
        bottom = any(e.get('action') == 'buy' and e.get('action_date') in dates for e in events)
    else:
        top = any(e.get('exit_date') in dates or e.get('reduce_date') in dates for e in events if e.get('exit_date') or e.get('reduce_date'))
        bottom = any(e.get('buy_date') in dates for e in events)
    return (MARK_LANE if top else 0), (MARK_LANE if bottom else 0)


def _price_extra(panel: dict) -> int:
    return COMPACT_PRICE_EXTRA if (panel or {}).get('compact') else CHART_PRICE_EXTRA


def panel_height(panel: dict) -> int:
    return CHART_HEIGHT + _price_extra(panel) + sum(mark_lanes(panel)) + mark_legend(None, panel, 0, True)


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
    """K 線分點標註。event 模式畫事件回放；flow 模式畫每日分點淨買／淨賣。"""
    bars = panel.get('bars') or []
    index = {bar['date']: i for i, bar in enumerate(bars)}
    events = _mark_events(panel)
    if not events:
        return
    mode = str(((panel or {}).get('marks') or {}).get('mode') or 'event')
    half = max(5, min(9, step * 0.45))
    if mode == 'flow':
        palette = _branch_palette(events)
        buy_badges, sell_badges = [], []
        for e in events:
            i = index.get(e.get('action_date') or '')
            if i is None:
                continue
            item = {'x': px(i), 'cx': px(i), 'no': e.get('no', ''),
                    'color': palette.get(str(e.get('branch') or '').strip())}
            if e.get('action') == 'buy':
                buy_badges.append(item)
            else:
                sell_badges.append(item)
        _assign_rows(buy_badges); _assign_rows(sell_badges)
        tri_bottom, tri_top = price_bottom + 14, price_top - 14
        for badge in buy_badges:
            color = badge.get('color') or UP
            i = min(range(len(bars)), key=lambda k: abs(px(k) - badge['x']))
            _dotted(draw, badge['x'], py(bars[i]['Low']) + 4, tri_bottom - half, color)
            draw.polygon([(badge['x'], tri_bottom-half),(badge['x']-half,tri_bottom+half),(badge['x']+half,tri_bottom+half)], fill=color, outline='white')
            if str(badge.get('no', '')) != '':
                cy = tri_bottom + half + 6 + MARK_BADGE_R + badge['row'] * MARK_BADGE_ROW
                _draw_badge(draw, badge['cx'], cy, badge['no'], color)
        for badge in sell_badges:
            color = badge.get('color') or DOWN
            i = min(range(len(bars)), key=lambda k: abs(px(k) - badge['x']))
            _dotted(draw, badge['x'], py(bars[i]['High']) - 4, tri_top + half, color)
            draw.polygon([(badge['x']-half,tri_top-half),(badge['x']+half,tri_top-half),(badge['x'],tri_top+half)], fill=color, outline='white')
            # 有編號＝A～E 事件（出清沿用買進編號）；沒編號＝小幅減碼／零星賣出。
            if str(badge.get('no', '')) != '':
                cy = tri_top - half - 6 - MARK_BADGE_R - badge['row'] * MARK_BADGE_ROW
                _draw_badge(draw, badge['cx'], cy, badge['no'], color)
        return

    buy_badges, sell_badges = [], []
    sell_days: dict[int, list[int]] = {}
    reduce_days: set[int] = set()
    buy_days: set[int] = set()
    for e in events:
        i = index.get(e.get('buy_date') or '')
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
    _assign_rows(buy_badges); _assign_rows(sell_badges)
    tri_bottom = price_bottom + 14
    for i in sorted(buy_days):
        x = px(i); _dotted(draw, x, py(bars[i]['Low']) + 4, tri_bottom - half, UP)
        draw.polygon([(x, tri_bottom-half),(x-half,tri_bottom+half),(x+half,tri_bottom+half)], fill=UP, outline='white')
    tri_top = price_top - 14
    for j in sorted(set(sell_days) | reduce_days):
        x = px(j); _dotted(draw, x, py(bars[j]['High']) - 4, tri_top + half, DOWN)
        draw.polygon([(x-half,tri_top-half),(x+half,tri_top-half),(x,tri_top+half)], fill=DOWN, outline='white')
    for badge in buy_badges:
        cy = tri_bottom + half + 6 + MARK_BADGE_R + badge['row'] * MARK_BADGE_ROW
        _draw_badge(draw, badge['cx'], cy, badge['no'], UP)
    for badge in sell_badges:
        cy = tri_top - half - 6 - MARK_BADGE_R - badge['row'] * MARK_BADGE_ROW
        _draw_badge(draw, badge['cx'], cy, badge['no'], DOWN)


# ============================================================
# K 線卡片
# ============================================================

def draw_value_tiles(draw, x: float, y: float, width: float, last: dict) -> None:
    """均線 4 格＋布林上下軌 2 格數值卡：色線圖例、數值、收盤相對位置（站上／跌破、高於／低於）。"""
    tiles = ([(k, 'MA20／中軌' if k == 'MA20' else k, c, False) for k, c in MA_COLORS.items()]
             + [(k, BAND_LABELS[k], BAND_COLORS[k], True) for k in TILE_BANDS])
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
    intraday = panel.get('intraday') or {}
    live = bool(intraday.get('is_live'))
    if live:
        # 盤中：右上角改成醒目的「盤中 HH:MM」膠囊，價格標籤改「成交」。
        stamp = f"盤中 {intraday.get('time', '')}｜收盤前會變動"
        stamp_w = font(20, True).getlength(stamp) + 28
        draw.rounded_rectangle((x1 - 32 - stamp_w, y + 26, x1 - 32, y + 60), radius=17, fill=WARN_BG)
        draw.text((x1 - 32 - stamp_w / 2, y + 43), stamp, font=font(20, True), fill=WARN_INK, anchor='mm')
        draw.text((x1 - 32, y + 70), f"資料至 {last['date']} {intraday.get('time', '')}", font=font(18), fill=MUTED, anchor='rt')
    else:
        draw.text((x1 - 32, y + 34), f"資料至 {last['date']}", font=font(22), fill=MUTED, anchor='rt')
    color = UP if (change or 0) >= 0 else DOWN
    price_label = '成交' if live else '收盤'
    draw.text((x0 + 32, y + 112), price_label, font=font(22), fill=MUTED, anchor='ls')
    price_x = x0 + 32 + font(22).getlength(price_label) + 12
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
    extra = lane_top + lane_bottom + _price_extra(panel)
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
    if not panel.get('compact'):
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
    # Dashed upper/lower bands (the middle band is the MA20 line) use report-supplied values.
    # A missing rolling value breaks the line instead of joining across a gap.
    for key in TILE_BANDS:
        band_color = BAND_COLORS[key]
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

GRADE_STYLE = {'結構偏強': (GOOD_BG, GOOD_INK), '中性偏多': ('#F1F8F5', GOOD_INK), '結構中性': (TILE_BG, '#344054'),
               '中性偏弱': ('#FEF8F0', WARN_INK), '結構偏弱': (WARN_BG, WARN_INK)}
LEVEL_STYLE = {'壓力': (UP_BG, UP), '現價': (ACCENT_BG, ACCENT), '成本': (COST_BG, COST_INK), '支撐': (DOWN_BG, DOWN)}
LEVEL_ROW_H = 38
LEVEL_MAX_RESISTANCES, LEVEL_MAX_SUPPORTS, BRANCH_MAX_ROWS, REASON_MAX_ITEMS = 1, 2, 4, 3
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
    def _pick(levels, base_limit):
        picked = list((levels or [])[:base_limit])
        seen = {(str(lv.get('label', '')), float(lv.get('price'))) for lv in picked if _finite(lv.get('price')) is not None}
        # 大量區若距現價不遠，工具層已先保留；這裡再確保不要因表格列數上限被截掉。
        for lv in (levels or [])[base_limit:]:
            label = str(lv.get('label', ''))
            price = _finite(lv.get('price'))
            if price is None:
                continue
            key = (label, float(price))
            if '量區' in label and key not in seen:
                picked.append(lv)
                seen.add(key)
        return picked

    rows = []
    for lv in _pick(card.get('resistances_above_close') or [], LEVEL_MAX_RESISTANCES):
        rows.append(('壓力', lv.get('label', ''), lv.get('price'), lv.get('distance_from_close_pct')))
    for lv in _pick(card.get('supports_below_close') or [], LEVEL_MAX_SUPPORTS):
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
        text, size = fit('MA20（布林中軌）' if label == 'MA20' else label, 21, 210, kind == '現價')
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
    basis = str(card.get('score_basis') or '收盤確認')
    h += _sub_heading(draw, px, y + h, '型態評分', f'{basis}｜只評技術結構，不含籌碼，不是買賣建議', width, dry) + 10

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

    # 盤中觀察：分數固定以收盤計算，盤中和收盤不同的地方另外列出，標明尚待收盤確認。
    live_changes = [str(t) for t in card.get('intraday_changes') or []]
    if live_changes:
        box_h = _reason_column(None, 0, 0, width - 48, '盤中觀察（尚待收盤確認，不計入分數）', live_changes, WARN_INK, '', True) + 32
        if not dry:
            draw.rounded_rectangle((px, y + h, px + width, y + h + box_h), radius=14, fill=WARN_BG)
            _reason_column(draw, px + 24, y + h + 16, width - 48, '盤中觀察（尚待收盤確認，不計入分數）', live_changes, WARN_INK, '', False)
        h += box_h + 20

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
    h += _sub_heading(draw, px, y + h, '關鍵價位', '均線、附近大量區與布林上下軌中，離收盤最近的壓力與支撐（大量區太遠時省略）', width, dry)
    if levels:
        if not dry:
            _draw_level_table(draw, px, y + h, width, levels, card)
        h += TABLE_HEAD_H + len(levels) * LEVEL_ROW_H + 26
    else:
        if not dry:
            text_at(draw, (px, y + h), '目前沒有可用的價位資料', 21, MUTED)
        h += 34 + 34

    branches = (card.get('tracked_branches') or [])[:BRANCH_MAX_ROWS]
    show_tracked = bool(card.get('show_tracked_branches', True))
    if show_tracked:
        period = card.get('tracked_branches_period') or ''
        # 不在圖片上寫「高勝率優先」等系統篩選說明；週精選只呈現文章實際提到的分點。
        note = (f'{period}' if period else '')
        h += _sub_heading(draw, px, y + h, '追蹤分點動向', note, width, dry)
        if branches:
            if not dry:
                _draw_branch_table(draw, px, y + h, width, branches)
            h += TABLE_HEAD_H + len(branches) * BRANCH_ROW_H + 30
        else:
            # 一般個股分析保留區塊高度；缺資料原因不顯示在圖片上，只由上游寫 LOG。
            h += 16
    return int(h)


COMPARE_LABEL_W = 210
COMPARE_ROW_H = 42
COMPARE_SCORE_H = 76


def _branch_summary(card: dict) -> str:
    rows = card.get('tracked_branches') or []
    if not rows:
        return '近 20 個交易日無 A～E 事件'
    holding = [r for r in rows if str(r.get('status', '')).startswith('持有中')]
    high = [r for r in holding if r.get('is_high_win_rate')]
    text = f"{len(rows)} 家有事件｜持有中 {len(holding)} 家"
    return text + (f"（高勝率 {len(high)}）" if high else '')


def _nearest_level(card: dict, key: str) -> str:
    levels = card.get(key) or []
    if not levels:
        return '—'
    lv = levels[0]
    label = 'MA20（中軌）' if lv.get('label') == 'MA20' else str(lv.get('label', ''))
    pct = _finite(lv.get('distance_from_close_pct'))
    return f"{label} {number(lv.get('price'))}" + (f"（{pct:+.1f}%）" if pct is not None else '')


def compare_card(draw, y: float, panels: list[dict], dry: bool) -> int:
    """兩檔比較：分數、五大項、型態、均線、最近壓力／支撐、追蹤分點並排成一張表，取代兩張完整評分卡。"""
    cards = [p['scorecard'] for p in panels]
    x0, x1, pad = MARGIN, WIDTH - MARGIN, 36
    px, width = x0 + pad, CONTENT - pad * 2
    col_w = (width - COMPARE_LABEL_W) / len(cards)
    labels = [str(c.get('label', '')) for c in cards[0].get('components') or []]
    rows = [('分數', None)] + [(label, 'component') for label in labels] + [
        ('型態', lambda c: str(c.get('pattern_label') or '—')),
        ('均線', lambda c: str(c.get('ma_alignment') or '—')),
        ('最近壓力', lambda c: _nearest_level(c, 'resistances_above_close')),
        ('最近支撐', lambda c: _nearest_level(c, 'supports_below_close')),
        ('追蹤分點', _branch_summary),
    ]
    if any(c.get('intraday_changes') for c in cards):
        rows.append(('盤中觀察', lambda c: '；'.join(str(t).replace('，尚待收盤確認', '') for t in (c.get('intraday_changes') or [])[:2]) or '—'))
    basis = str(cards[0].get('score_basis') or '收盤確認')
    head_h = _sub_heading(None, px, 0, '型態比較', f'{basis}｜只評技術結構，不含籌碼，不是買賣建議', width, True)
    body_h = TABLE_HEAD_H + COMPARE_SCORE_H + (len(rows) - 1) * COMPARE_ROW_H
    total = int(30 + head_h + 6 + body_h + 30)
    if dry:
        return total
    draw.rounded_rectangle((x0, y, x1, y + total), radius=20, fill='white', outline=LINE)
    h = 30
    h += _sub_heading(draw, px, y + h, '型態比較', f'{basis}｜只評技術結構，不含籌碼，不是買賣建議', width, False) + 6
    ty = y + h
    scores = [_finite(c.get('pattern_score')) or 0.0 for c in cards]
    best = max(range(len(cards)), key=lambda i: scores[i]) if len(set(scores)) > 1 else None
    draw.rounded_rectangle((px, ty, px + width, ty + TABLE_HEAD_H), radius=8, fill=TILE_BG)
    draw.text((px + 12, ty + TABLE_HEAD_H / 2), '項目', font=font(18), fill=MUTED, anchor='lm')
    for i, panel in enumerate(panels):
        cx = px + COMPARE_LABEL_W + i * col_w
        text, size = fit(f"{panel.get('stock_code', '')} {panel.get('stock_name', '')}", 21, col_w - 24, True)
        draw.text((cx + 12, ty + TABLE_HEAD_H / 2), text, font=font(size, True), fill=INK, anchor='lm')
    ry = ty + TABLE_HEAD_H
    for label, getter in rows:
        row_h = COMPARE_SCORE_H if getter is None else COMPARE_ROW_H
        mid = ry + row_h / 2
        draw.text((px + 12, mid), label, font=font(20, getter is None), fill=MUTED if getter else INK, anchor='lm')
        for i, card in enumerate(cards):
            cx = px + COMPARE_LABEL_W + i * col_w + 12
            cw = col_w - 24
            if getter is None:
                score_text = f'{scores[i]:.1f}'
                color = ACCENT if best is None or best == i else MUTED
                draw.text((cx, mid + 18), score_text, font=font(46, True), fill=color, anchor='ls')
                sx = cx + font(46, True).getlength(score_text) + 8
                draw.text((sx, mid + 18), '/ 100', font=font(19), fill=MUTED, anchor='ls')
                grade = str(card.get('grade', ''))
                bg, ink = GRADE_STYLE.get(grade, (TILE_BG, INK))
                gx = sx + font(19).getlength('/ 100') + 16
                gw = font(19, True).getlength(grade) + 28
                draw.rounded_rectangle((gx, mid - 3, gx + gw, mid + 29), radius=16, fill=bg)
                draw.text((gx + gw / 2, mid + 13), grade, font=font(19, True), fill=ink, anchor='mm')
            elif getter == 'component':
                comp = next((c for c in card.get('components') or [] if c.get('label') == label), {})
                value, maximum = _finite(comp.get('value')) or 0.0, float(comp.get('max') or 1)
                bar_right = cx + cw - 110
                draw.rounded_rectangle((cx, mid - 6, bar_right, mid + 6), radius=6, fill=LINE)
                filled = max(0.0, min(1.0, value / maximum)) * (bar_right - cx)
                if filled > 12:
                    draw.rounded_rectangle((cx, mid - 6, cx + filled, mid + 6), radius=6, fill=ACCENT)
                draw.text((cx + cw, mid), f'{value:g} / {maximum:g}', font=font(19, True), fill=INK, anchor='rm')
            else:
                text, size = fit(getter(card), 20, cw, False)
                draw.text((cx, mid), text, font=font(size), fill=INK, anchor='lm')
        draw.line((px, ry + row_h, px + width, ry + row_h), fill=LINE)
        ry += row_h
    for i in range(1, len(cards)):
        lx = px + COMPARE_LABEL_W + i * col_w
        draw.line((lx, ty + TABLE_HEAD_H, lx, ry), fill=LINE)
    return total


def _compare_mode(panels: list[dict]) -> bool:
    return len(panels) >= 2 and all(p.get('scorecard') and p.get('bars') for p in panels)


# ============================================================
# 族群排行／成分股卡片（會員看的版面：不放名冊來源、檔數統計等執行細節）
# ============================================================

RANK_COLORS = {1: '#A17936', 2: '#7C8698', 3: '#B0784A'}
SECTOR_PAD = 36
SECTOR_INNER = 26
SECTOR_REASON_SIZE = 22
SECTOR_REASON_LINE = 33
SECTOR_OTHER_ROW_H = 46


def _display_name(name) -> str:
    return str(name or '').rstrip('*＊').strip()


def _market_label(market) -> str:
    return '上市' if str(market) in ('twse', 'TSE') else '上櫃' if str(market) in ('tpex', 'OTC') else ''


def _plain_reason(text: str) -> str:
    """「站上所有均線（均線排列 8/12）」→「站上所有均線」：會員版不顯示小項配分。"""
    return _REASON_POINTS_RE.sub('', str(text or '')).strip()


def _sector_date(date: str) -> str:
    return str(date or '')[5:].replace('-', '/') if date else ''


def _labeled_lines(label_w: float, width: float, items: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, list[str]]]:
    """（標籤, 標籤底色, 文字色, 文字）→ 依寬度換行後的結果。"""
    return [(label, bg, ink, wrap(text, SECTOR_REASON_SIZE, width - label_w - 14))
            for label, bg, ink, text in items if text]


def _rank_card_items(row: dict, technical: bool) -> list[tuple[str, str, str, str]]:
    items = []
    if technical:
        plus = [_plain_reason(t) for t in row.get('plus_reasons') or []][:1]
        minus = [_plain_reason(t) for t in row.get('minus_reasons') or []][:1]
        items += [('優勢', GOOD_BG, GOOD_INK, t) for t in plus]
        items += [('留意', WARN_BG, WARN_INK, t) for t in minus]
    if row.get('observation'):
        items.append(('解讀', ACCENT_BG, INK, str(row['observation'])))
    return items


def _rank_card(draw, x: float, y: float, width: float, row: dict, technical: bool, dry: bool) -> int:
    """前三名卡片：名次徽章＋股名／代號／市場＋分數（或漲幅）＋分數條＋優勢／留意／AI 解讀。"""
    rank = int(row.get('rank') or 0)
    group_row = str(row.get('row_kind') or '') == 'sector_group'
    label_w = 64
    lines = _labeled_lines(label_w, width - SECTOR_INNER * 2, _rank_card_items(row, technical and not group_row))
    body_h = sum(max(1, len(ls)) * SECTOR_REASON_LINE + 10 for *_, ls in lines)
    head_h = 132 if group_row else (178 if technical else 150)   # 名稱列＋第二行（＋分數條）
    height = int(head_h + body_h + SECTOR_INNER - 10)
    if dry:
        return height
    first = rank == 1
    draw.rounded_rectangle((x, y, x + width, y + height), radius=18,
                           fill=ACCENT_BG if first else TILE_BG, outline=ACCENT if first else LINE, width=2 if first else 1)
    ix, right = x + SECTOR_INNER, x + width - SECTOR_INNER
    # 名次徽章
    color = RANK_COLORS.get(rank, MUTED)
    cx, cy = ix + 28, y + SECTOR_INNER + 30
    draw.ellipse((cx - 28, cy - 28, cx + 28, cy + 28), fill=color)
    draw.text((cx, cy), str(rank), font=font(30, True), fill='white', anchor='mm')
    # 股名（族群名）＋代號＋市場
    nx = ix + 74
    name = _display_name(row.get('stock_name'))
    draw.text((nx, cy + 12), name, font=font(34, True), fill=INK, anchor='ls')
    tx = nx + font(34, True).getlength(name) + 14
    code = '' if group_row else str(row.get('stock_code', ''))
    if code:
        draw.text((tx, cy + 12), code, font=font(24), fill=MUTED, anchor='ls')
        tx += font(24).getlength(code) + 12
    market = '' if group_row else _market_label(row.get('market'))
    if market:
        mw = font(17, True).getlength(market) + 18
        draw.rounded_rectangle((tx, cy - 9, tx + mw, cy + 15), radius=12, fill='white', outline=LINE)
        draw.text((tx + mw / 2, cy + 3), market, font=font(17, True), fill=MUTED, anchor='mm')
    # 右側：分數＋分級（型態排行）或漲跌幅（漲幅排行）
    change = _finite(row.get('change_pct'))
    change_color = UP if (change or 0) > 0 else DOWN if (change or 0) < 0 else MUTED
    if group_row:
        # 族群列：型態排行顯示中位型態分數，漲幅排行顯示中位漲幅。
        score = _finite(row.get('pattern_score'))
        if score is not None:
            draw.text((right, cy + 18), '/ 100', font=font(20), fill=MUTED, anchor='rs')
            draw.text((right - font(20).getlength('/ 100') - 8, cy + 18), f'{score:.1f}',
                      font=font(48, True), fill=ACCENT if first else INK, anchor='rs')
        else:
            draw.text((right, cy + 18), f'{change:+.2f}%' if change is not None else '—',
                      font=font(48, True), fill=change_color, anchor='rs')
    elif technical:
        grade = str(row.get('grade', ''))
        bg, ink = GRADE_STYLE.get(grade, (TILE_BG, INK))
        gw = font(20, True).getlength(grade) + 30
        draw.rounded_rectangle((right - gw, cy - 16, right, cy + 20), radius=18, fill=bg)
        draw.text((right - gw / 2, cy + 2), grade, font=font(20, True), fill=ink, anchor='mm')
        sx = right - gw - 16
        draw.text((sx, cy + 18), '/ 100', font=font(20), fill=MUTED, anchor='rs')
        sx -= font(20).getlength('/ 100') + 8
        score = _finite(row.get('pattern_score')) or 0.0
        draw.text((sx, cy + 18), f'{score:.1f}', font=font(48, True), fill=ACCENT if first else INK, anchor='rs')
    else:
        text = f'{change:+.2f}%' if change is not None else '—'
        draw.text((right, cy + 18), text, font=font(48, True), fill=change_color, anchor='rs')
    # 第二行：族群列放涵蓋與代表股，個股列放股價與漲跌
    if group_row:
        gy = y + SECTOR_INNER + 92
        parts = [str(row.get('coverage_text') or ''), str(row.get('ratio_text') or ''), str(row.get('leader_text') or '')]
        text, size = fit('　｜　'.join(p for p in parts if p), 22, width - SECTOR_INNER * 2 - 74)
        draw.text((nx, gy), text, font=font(size), fill=MUTED, anchor='ls')
        return height
    live = (row.get('intraday') or {}).get('is_live')
    price_label = '成交' if live else '收盤'
    ly = y + SECTOR_INNER + 92
    close = _finite(row.get('close'))
    draw.text((nx, ly), f'{price_label} {number(close) if close is not None else "—"}', font=font(22), fill=INK, anchor='ls')
    px_ = nx + font(22).getlength(f'{price_label} {number(close) if close is not None else "—"}') + 18
    if technical and change is not None:
        draw.text((px_, ly), f'{change:+.2f}%', font=font(22, True), fill=change_color, anchor='ls')
    elif not technical:
        info = row.get('intraday') or {}
        when = f"盤中 {info.get('time', '')}" if live else f"{_sector_date(row.get('quote_date'))} 收盤"
        draw.text((px_, ly), when, font=font(20), fill=MUTED, anchor='ls')
    # 分數條
    if technical:
        by = y + SECTOR_INNER + 122
        score = _finite(row.get('pattern_score')) or 0.0
        draw.rounded_rectangle((nx, by, right, by + 10), radius=5, fill=LINE)
        filled = max(0.0, min(1.0, score / 100)) * (right - nx)
        if filled > 10:
            draw.rounded_rectangle((nx, by, nx + filled, by + 10), radius=5, fill=color if not first else ACCENT)
    # 優勢／留意／AI 解讀
    ry = y + head_h
    for label, bg, ink, ls in lines:
        # 顏色只放在標籤，說明文字一律深色，比較好讀。
        draw.rounded_rectangle((ix, ry - 1, ix + label_w, ry + 27), radius=14, fill=bg if bg != ACCENT_BG or not first else 'white')
        draw.text((ix + label_w / 2, ry + 13), label, font=font(18, True), fill=ink if label != '解讀' else ACCENT, anchor='mm')
        for i, line in enumerate(ls):
            text_at(draw, (ix + label_w + 14, ry + i * SECTOR_REASON_LINE), line, SECTOR_REASON_SIZE, INK)
        ry += max(1, len(ls)) * SECTOR_REASON_LINE + 10
    return height


def _other_rows_table(draw, x: float, y: float, width: float, rows: list[dict], technical: bool, dry: bool) -> int:
    height = TABLE_HEAD_H + len(rows) * SECTOR_OTHER_ROW_H
    if dry:
        return height
    draw.rounded_rectangle((x, y, x + width, y + TABLE_HEAD_H), radius=8, fill=TILE_BG)
    group_rows = any(str(r.get('row_kind') or '') == 'sector_group' for r in rows)
    heads = (('名次', x + 16, 'lm'), ('族群' if group_rows else '個股', x + 96, 'lm'))
    if group_rows:
        heads += ((('中位型態' if technical else '中位漲幅'), x + width - 330, 'rm'), ('納入檔數', x + width - 16, 'rm'))
    elif technical:
        heads += (('型態分數', x + 470, 'lm'), ('分級', x + width - 330, 'lm'), ('漲跌', x + width - 16, 'rm'))
    else:
        heads += (('價格', x + width - 200, 'rm'), ('漲跌', x + width - 16, 'rm'))
    for label, lx, anchor in heads:
        draw.text((lx, y + TABLE_HEAD_H / 2), label, font=font(17), fill=MUTED, anchor=anchor)
    ry = y + TABLE_HEAD_H
    for row in rows:
        mid = ry + SECTOR_OTHER_ROW_H / 2
        draw.text((x + 34, mid), str(row.get('rank', '')), font=font(21, True), fill=MUTED, anchor='mm')
        name = (_display_name(row.get('stock_name')) if str(row.get('row_kind') or '') == 'sector_group'
                else f"{_display_name(row.get('stock_name'))}  {row.get('stock_code', '')}")
        text, size = fit(name, 22, 360, False)
        draw.text((x + 96, mid), text, font=font(size), fill=INK, anchor='lm')
        change = _finite(row.get('change_pct'))
        change_text = f'{change:+.2f}%' if change is not None else '—'
        change_color = UP if (change or 0) > 0 else DOWN if (change or 0) < 0 else MUTED
        if group_rows:
            median = _finite(row.get('pattern_score'))
            value = f'{median:.1f}' if median is not None else change_text
            draw.text((x + width - 330, mid), value, font=font(21, True),
                      fill=INK if median is not None else change_color, anchor='rm')
            draw.text((x + width - 16, mid), str(row.get('coverage_text') or '—'), font=font(20), fill=MUTED, anchor='rm')
            draw.line((x, ry + SECTOR_OTHER_ROW_H, x + width, ry + SECTOR_OTHER_ROW_H), fill=LINE)
            ry += SECTOR_OTHER_ROW_H
            continue
        if technical:
            score = _finite(row.get('pattern_score')) or 0.0
            bar_l, bar_r = x + 470, x + width - 430
            draw.rounded_rectangle((bar_l, mid - 5, bar_r, mid + 5), radius=5, fill=LINE)
            filled = max(0.0, min(1.0, score / 100)) * (bar_r - bar_l)
            if filled > 10:
                draw.rounded_rectangle((bar_l, mid - 5, bar_l + filled, mid + 5), radius=5, fill='#C9B48E')
            draw.text((bar_r + 70, mid), f'{score:.1f}', font=font(21, True), fill=INK, anchor='rm')
            grade = str(row.get('grade', ''))
            bg, ink = GRADE_STYLE.get(grade, (TILE_BG, INK))
            gw = font(17, True).getlength(grade) + 22
            gx = x + width - 330
            draw.rounded_rectangle((gx, mid - 14, gx + gw, mid + 14), radius=14, fill=bg)
            draw.text((gx + gw / 2, mid), grade, font=font(17, True), fill=ink, anchor='mm')
        else:
            close = _finite(row.get('close'))
            draw.text((x + width - 200, mid), number(close) if close is not None else '—', font=font(21), fill=INK, anchor='rm')
        draw.text((x + width - 16, mid), change_text, font=font(21, True), fill=change_color, anchor='rm')
        draw.line((x, ry + SECTOR_OTHER_ROW_H, x + width, ry + SECTOR_OTHER_ROW_H), fill=LINE)
        ry += SECTOR_OTHER_ROW_H
    return height


def sector_card(draw, y: float, data: dict, dry: bool) -> int:
    x0, x1 = MARGIN, WIDTH - MARGIN
    px, width = x0 + SECTOR_PAD, CONTENT - SECTOR_PAD * 2
    mode = str(data.get('mode') or '')
    technical = mode.endswith('technical')
    rows = data.get('rows') or []
    h = 34
    title = f"{data.get('name', '')}｜{'型態排行' if technical else '漲幅排行'}"
    if data.get('live_time') and not technical:
        stamp = f"盤中 {data['live_time']}｜收盤前會變動"
        stamp_bg, stamp_ink = WARN_BG, WARN_INK
    else:
        stamp = f"{_sector_date(data.get('comparison_date'))} 收盤" if data.get('comparison_date') else ''
        stamp_bg, stamp_ink = TILE_BG, MUTED
    disclaimer = '※ 排名僅供研究與觀察參考，不代表未來表現，亦非買賣建議。'
    note = str(data.get('liquidity_note') or '')
    if note:
        disclaimer = f"{note}　｜　{disclaimer}"
    if not dry:
        draw.rectangle((px, y + h + 6, px + 5, y + h + 38), fill=ACCENT)
        text_at(draw, (px + 18, y + h), title, 32, INK, True)
        if stamp:
            sw = font(19, True).getlength(stamp) + 30
            draw.rounded_rectangle((x1 - SECTOR_PAD - sw, y + h + 2, x1 - SECTOR_PAD, y + h + 38), radius=18, fill=stamp_bg)
            draw.text((x1 - SECTOR_PAD - sw / 2, y + h + 20), stamp, font=font(19, True), fill=stamp_ink, anchor='mm')
        text_at(draw, (px, y + h + 58), disclaimer, 18, MUTED)
    h += 56 + 38
    if not rows:
        if not dry:
            text_at(draw, (px, y + h), '目前沒有足夠的同日資料可以排名，請稍後再試。', 24, MUTED)
        h += 60
    for row in rows:
        h += _rank_card(draw, px, y + h, width, row, technical, dry) + 18
    others = data.get('others') or []
    if others:
        h += 10
        h += _sub_heading(draw, px, y + h, '其他排名', '', width, dry)
        h += _other_rows_table(draw, px, y + h, width, others, technical, dry) + 18
    h += 22
    return int(h)


def members_card(draw, y: float, data: dict, dry: bool) -> int:
    x0, x1 = MARGIN, WIDTH - MARGIN
    px, width = x0 + SECTOR_PAD, CONTENT - SECTOR_PAD * 2
    twse, tpex = data.get('twse') or [], data.get('tpex') or []
    h = 34
    if not dry:
        draw.rectangle((px, y + h + 6, px + 5, y + h + 38), fill=ACCENT)
        text_at(draw, (px + 18, y + h), f"{data.get('name', '')}｜成分股", 32, INK, True)
        text_at(draw, (px, y + h + 56), f'共 {len(twse) + len(tpex)} 檔：上市 {len(twse)} 檔、上櫃 {len(tpex)} 檔', 20, MUTED)
    h += 56 + 50
    chip_h, gap = 42, 10
    for label, stocks in (('上市', twse), ('上櫃', tpex)):
        if not dry:
            text_at(draw, (px, y + h), f'{label}  {len(stocks)} 檔', 23, INK, True)
        h += 42
        if not stocks:
            if not dry:
                text_at(draw, (px, y + h), '此分類沒有這個市場的個股', 21, MUTED)
            h += 44
            continue
        widths = [font(21, True).getlength(_display_name(s['stock_name'])) + font(18).getlength(s['stock_code']) + 44 for s in stocks]
        cx, cy = px, y + h
        for s, w in zip(stocks, widths):
            if cx > px and cx + w > px + width:
                cx, cy = px, cy + chip_h + gap
            if not dry:
                draw.rounded_rectangle((cx, cy, cx + w, cy + chip_h), radius=21, fill=TILE_BG, outline=LINE)
                name = _display_name(s['stock_name'])
                draw.text((cx + 16, cy + chip_h / 2), name, font=font(21, True), fill=INK, anchor='lm')
                draw.text((cx + 16 + font(21, True).getlength(name) + 10, cy + chip_h / 2), s['stock_code'],
                          font=font(18), fill=MUTED, anchor='lm')
            cx += w + gap
        h += _flow_rows(widths, width, gap) * (chip_h + gap) + 18
    h += 18
    return int(h)


def catalog_card(draw, y: float, data: dict, dry: bool) -> int:
    """族群清單：用多欄 chip 排版，不顯示資料來源、抓取規則或 fallback 說明。"""
    x0, x1 = MARGIN, WIDTH - MARGIN
    px, width = x0 + SECTOR_PAD, CONTENT - SECTOR_PAD * 2
    h = 34
    if not dry:
        draw.rectangle((px, y + h + 6, px + 5, y + h + 38), fill=ACCENT)
        text_at(draw, (px + 18, y + h), str(data.get('title') or '細產業名單'), 32, INK, True)
    h += 58
    chip_h, gap_x, gap_y = 34, 8, 8
    cols = 6
    col_w = (width - gap_x * (cols - 1)) / cols
    for section in data.get('sections') or []:
        items = [str(v).strip() for v in (section.get('items') or []) if str(v).strip()]
        if not items:
            continue
        if not dry:
            draw.rectangle((px, y + h + 5, px + 4, y + h + 31), fill=ACCENT)
            text_at(draw, (px + 15, y + h), str(section.get('title') or ''), 24, INK, True)
        h += 42
        rows = (len(items) + cols - 1) // cols
        if not dry:
            for idx, item in enumerate(items):
                r, c = divmod(idx, cols)
                cx = px + c * (col_w + gap_x)
                cy = y + h + r * (chip_h + gap_y)
                draw.rounded_rectangle((cx, cy, cx + col_w, cy + chip_h), radius=12, fill=TILE_BG, outline=LINE)
                label, size = fit(item, 18, col_w - 22, True)
                draw.text((cx + 11, cy + chip_h / 2), label, font=font(size, True), fill=INK, anchor='lm')
        h += rows * (chip_h + gap_y) + 20
    return int(h + 8)

def _sector_block(draw, y: float, panel: dict, dry: bool) -> int:
    """先量高度、畫白底卡片，再畫內容（避免底色蓋掉文字）。"""
    if panel.get('sector'):
        fn, data = sector_card, panel['sector']
    elif panel.get('sector_members'):
        fn, data = members_card, panel['sector_members']
    else:
        fn, data = catalog_card, panel['sector_catalog']
    height = fn(None, 0, data, True)
    if not dry:
        draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + height), radius=20, fill='white', outline=LINE)
        fn(draw, y, data, False)
    return height


ARTICLE_PAD = 44
ARTICLE_BODY_SIZE = 30      # 精選文章內文比一般回答再大一級，手機上也好讀
ARTICLE_LINE = 50
ARTICLE_PARAGRAPH_GAP = 22


def _article_paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{1,}", str(body or "")) if p.strip()]


def article_card(draw, y: float, data: dict, dry: bool) -> int:
    """週精選文章版面：標題列＋分段內文＋底部免責，行距與留白比一般回答寬。"""
    x0, x1 = MARGIN, WIDTH - MARGIN
    px, width = x0 + ARTICLE_PAD, CONTENT - ARTICLE_PAD * 2
    title = str(data.get('title') or '')
    subtitle = str(data.get('subtitle') or '')
    paragraphs = [wrap(p, ARTICLE_BODY_SIZE, width) for p in _article_paragraphs(data.get('body'))]
    disclaimers = [str(x) for x in (data.get('disclaimers') or []) if str(x).strip()]
    h = 38
    if title:
        h += 58
    if subtitle:
        h += 36
    h += 20
    for lines in paragraphs:
        h += len(lines) * ARTICLE_LINE + ARTICLE_PARAGRAPH_GAP
    if disclaimers:
        h += 34
    h += 36
    if dry:
        return int(h)

    draw.rounded_rectangle((x0, y, x1, y + h), radius=20, fill='white', outline=LINE)
    cursor = y + 38
    if title:
        draw.rectangle((px, cursor + 8, px + 6, cursor + 44), fill=ACCENT)
        text_at(draw, (px + 20, cursor), title, 34, INK, True)
        cursor += 58
    if subtitle:
        text_at(draw, (px + 20 if title else px, cursor), subtitle, 21, MUTED)
        cursor += 36
    cursor += 20
    for lines in paragraphs:
        for line in lines:
            text_at(draw, (px, cursor), line, ARTICLE_BODY_SIZE, INK)
            cursor += ARTICLE_LINE
        cursor += ARTICLE_PARAGRAPH_GAP
    if disclaimers:
        cursor += 2
        draw.line((px, cursor, px + width, cursor), fill=LINE)
        text_at(draw, (px, cursor + 10), "　｜　".join(clean(d) for d in disclaimers), 21, MUTED)
    return int(h)


def _is_article_panel(panel: dict) -> bool:
    return bool((panel or {}).get('article'))


def _is_sector_panel(panel: dict) -> bool:
    return bool((panel or {}).get('sector') or (panel or {}).get('sector_members') or (panel or {}).get('sector_catalog'))


def header_brand(draw, title: str, size: int, brand: str = 'ACE / RESEARCH') -> None:
    """頁首：左邊標題、右邊品牌字，右緣對齊下方卡片（WIDTH - MARGIN），兩者同一條文字基線。"""
    baseline = 66 + font(size, True).getmetrics()[0]
    draw.text((MARGIN, baseline), title, font=font(size, True), fill=INK, anchor='ls')
    draw.text((WIDTH - MARGIN, baseline), brand, font=font(20), fill=ACCENT, anchor='rs')


def price_footer(panels: list[dict] | None) -> str:
    """頁尾資料說明：有任何一檔接上盤中即時報價就改寫，避免圖上寫「收盤資料」卻是盤中價格。"""
    infos = [(p or {}).get('intraday') or {} for p in panels or []]
    if any(i.get('is_live') for i in infos):
        return '股市艾斯  /  最新一根 K 棒為盤中即時報價，收盤前會變動'
    if any(infos):
        return '股市艾斯  /  最新一根 K 棒為今日收盤報價，其餘為日 K 收盤資料'
    return '股市艾斯  /  日 K 為收盤資料，非盤中即時行情'


def panel_block_height(panel: dict) -> int:
    card = panel.get('scorecard')
    return panel_height(panel) + 24 + (scorecard(None, 0, card, True) + 24 if card else 0)


def add_center_watermarks(image: Image.Image) -> Image.Image:
    """合成到成品上方：長圖上下各一枚，短圖一枚；僅縮放字樣、不更動內容。"""
    if not CENTER_WATERMARK_TEXT:
        return image
    # 週報 fig 座標原點在左下；Pillow 左上，所以 0.66/0.31 對應 0.34/0.69。
    centers = (0.34, 0.69) if image.height >= image.width * 0.85 else (0.55,)
    face = font(CENTER_WATERMARK_FONT_SIZE, True)
    probe = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    spacing = round(CENTER_WATERMARK_FONT_SIZE * 0.12)
    bounds = probe.multiline_textbbox((0, 0), CENTER_WATERMARK_TEXT, font=face, spacing=spacing, align='center')
    stamp = Image.new('RGBA', (math.ceil(bounds[2] - bounds[0]) + 16, math.ceil(bounds[3] - bounds[1]) + 16))
    ImageDraw.Draw(stamp).multiline_text((8 - bounds[0], 8 - bounds[1]), CENTER_WATERMARK_TEXT,
                                       font=face, spacing=spacing, align='center', fill=CENTER_WATERMARK_COLOR)
    stamp = stamp.rotate(CENTER_WATERMARK_ROTATION, resample=Image.Resampling.BICUBIC, expand=True)
    max_height = image.height * (0.29 if len(centers) == 2 else 0.42)
    scale = min(1.0, image.width * 0.82 / stamp.width, max_height / stamp.height)
    if scale < 1:
        stamp = stamp.resize((max(1, round(stamp.width * scale)), max(1, round(stamp.height * scale))), Image.Resampling.LANCZOS)
    stamp.putalpha(stamp.getchannel('A').point(lambda alpha: round(alpha * CENTER_WATERMARK_ALPHA)))
    # 小區塊合成，避免為長圖另外配置整張 RGBA 浮水印圖層。
    for center in centers:
        xy = ((image.width - stamp.width) // 2, round(image.height * center - stamp.height / 2))
        image.paste(stamp, xy, stamp)
    return image


def render_answer(question: str, answer: str, panels: list[dict] | None = None,
                  *, title: str = '艾斯 AI｜研究筆記', demo: bool = False) -> Image.Image:
    panels = panels or []
    # 族群排行／成分股：整張用卡片呈現，不再另外排文字區塊（文字版只留給 Log）。
    sector_panels = [p for p in panels if _is_sector_panel(p)]
    article_panels = [p for p in panels if _is_article_panel(p)]
    panels = [p for p in panels if not _is_sector_panel(p) and not _is_article_panel(p)]
    compare = _compare_mode(panels)
    if compare:
        # 兩檔比較：K 線縮短、不畫分點標註，兩張評分卡合併成一張並排比較表，圖片長度約減半。
        panels = [{**p, 'compact': True} for p in panels]
    question_lines = wrap(clean(question), 31, CONTENT - 12, True)
    header_height = 155 + len(question_lines) * 47
    blocks = [] if (sector_panels or article_panels) else body_blocks(answer)
    body_height = sum(b.height for b in blocks) + 68 if blocks else 0
    if compare:
        panels_height = sum(panel_height(p) + 24 for p in panels) + compare_card(None, 0, panels, True) + 24
    else:
        panels_height = sum(panel_block_height(p) for p in panels)
    panels_height += sum(_sector_block(None, 0, p, True) + 24 for p in sector_panels)
    panels_height += sum(article_card(None, 0, p['article'], True) + 24 for p in article_panels)
    height = header_height + panels_height + body_height + 112
    image = Image.new('RGB', (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((MARGIN, 43, MARGIN + 48, 48), fill=ACCENT)
    header_brand(draw, title, 28, '示範資料・非真實行情' if demo else 'ACE / RESEARCH')
    for i, line in enumerate(question_lines):
        text_at(draw, (MARGIN, 124 + i * 47), line, 31, bold=True)
    y = header_height
    for panel in panels:
        draw_chart(draw, y, panel)
        y += panel_height(panel) + 24
        if panel.get('scorecard') and not compare:
            scorecard(draw, y, panel['scorecard'], False)
            y += scorecard(None, 0, panel['scorecard'], True) + 24
    if compare:
        y += compare_card(draw, y, panels, False) + 24
    for panel in sector_panels:
        y += _sector_block(draw, y, panel, False) + 24
    for panel in article_panels:
        y += article_card(draw, y, panel['article'], False) + 24
    if blocks:
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
    if article_panels:
        footer = '股市艾斯  /  日 K 為收盤資料，非盤中即時行情'
    elif sector_panels:
        live = any((p.get('sector') or {}).get('live_time') for p in sector_panels)
        footer = '股市艾斯  /  盤中漲幅為暫定值，收盤前會變動' if live else '股市艾斯  /  族群排行依日 K 收盤資料計算'
    else:
        footer = price_footer(panels) if panels else '股市艾斯  /  AI 資料整理'
    text_at(draw, (MARGIN, height - 49), footer, 20, MUTED)
    return add_center_watermarks(image)


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
