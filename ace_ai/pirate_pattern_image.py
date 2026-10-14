"""Test-only pirate pattern report. Native chart geometry and data are preserved.

No network, quote fetching, AI calls, database access, or changes to answer_image.
The artwork is the approved A template; all text and values come from this request.
"""
from __future__ import annotations

import importlib.util
import math
import re
import sys
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).with_name('assets') / 'pirate_pattern'
WIDTH = 1440
INK, MUTED, GOLD = '#3F2D22', '#735D49', '#946A3A'
UP, DOWN = '#AA493D', '#32715B'
_LOAD_LOCK = threading.Lock()
_NATIVE = None


def _chart_renderer():
    """Separate namespace: normal responses never see our palette or legend hook."""
    global _NATIVE
    with _LOAD_LOCK:
        if _NATIVE is None:
            path = Path(__file__).with_name('answer_image.py')
            spec = importlib.util.spec_from_file_location('_ace_pirate_native_chart', path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            original_text = module.text_at

            def paper_text(draw, xy, text, *args, **kwargs):
                return original_text(draw, xy, str(text).replace('藍：其他價位', '棕：其他價位'), *args, **kwargs)

            module.text_at = paper_text
            module.MUTED, module.LINE, module.GRID = MUTED, '#DCD0B9', '#E7DDCB'
            module.TILE_BG = '#FAF0DE'
            _NATIVE = module
    return _NATIVE


def font(size, kind='body'):
    # A new font instance per call avoids shared FreeType variation state in workers.
    if kind == 'pirate':
        # Pirata One (OFL) is Latin-only and used for the score gauge alone.
        pirate = ASSETS / 'PirataOne-Regular.ttf'
        if pirate.exists():
            return ImageFont.truetype(str(pirate), int(size))
        kind = 'classic'
    serif = kind in ('serif', 'classic')
    path = ASSETS / ('NotoSerifTC-VF.ttf' if serif else 'NotoSansTC-VF.ttf')
    f = ImageFont.truetype(str(path), int(size))
    f.set_variation_by_axes([700 if kind in ('serif', 'bold', 'num', 'classic') else 400])
    return f


def _clean(value):
    return _chart_renderer().clean(str(value or ''))


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _number(value):
    number = _finite(value)
    return f'{number:,.2f}' if number is not None else '—'


# Numbers / Latin words stay whole ("2,511.50" never splits); closing punctuation
# never starts a line.
_TOKEN_RE = re.compile(r'[A-Za-z0-9.,%+\-/>＜<＞]+|.')
_CLOSING = set('，。、；：！？）」』%')


def _wrap(text, width, size=25, kind='body'):
    f, lines = font(size, kind), []
    for paragraph in _clean(text).splitlines():
        line = ''
        for token in _TOKEN_RE.findall(paragraph):
            if line and f.getlength(line + token) > width and token not in _CLOSING:
                if f.getlength(token) > width:
                    # Over-long token: fall back to character breaks.
                    for char in token:
                        if line and f.getlength(line + char) > width:
                            lines.append(line.rstrip())
                            line = ''
                        line += char
                    continue
                lines.append(line.rstrip())
                line = token.lstrip()
            else:
                line += token
        if line.strip():
            lines.append(line.rstrip())
    return lines


def _fit(text, width, sizes, max_lines, kind='body'):
    """Largest size whose wrap fits max_lines; the last size ellipsizes."""
    for size in sizes:
        lines = _wrap(text, width, size, kind)
        if len(lines) <= max_lines:
            return lines, size
    f = font(size, kind)
    lines = lines[:max_lines]
    last = lines[-1]
    while last and f.getlength(last + '…') > width:
        last = last[:-1]
    lines[-1] = last.rstrip('，、；： ') + '…'
    return lines, size


@lru_cache(maxsize=2)
def _asset(name):
    with Image.open(ASSETS / name) as image:
        return image.convert('RGB')


def _tighten_blank_rows(pixels, longest=28, keep=.5, floor=18):
    """Halve long empty white bands (date axis→volume, below volume). Only rows
    with no drawing at all are removed, so no plotted pixel moves relative to
    its own axis; a minimum gap is always kept."""
    blank = np.all(pixels.min(axis=2) >= 250, axis=1)
    rows, start = [], None
    for y, empty in enumerate(list(blank) + [False]):
        if empty and start is None:
            start = y
        elif not empty and start is not None:
            length = y - start
            if length > longest:
                drop = length - max(floor, round(length * keep))
                mid = start + (length - drop)//2
                rows.extend(range(mid, mid + drop))
            start = None
    return np.delete(pixels, rows, axis=0) if rows else pixels


def chart_image(panel):
    """Native 1:1 chart, including every optional subplot and annotation.

    Only the duplicate native title/quote above y=136 is replaced by our Hero and
    four quote fields. The plot, date/price axes, MA/BB, volume and overlays retain
    their exact native coordinates. No resize, candle redraw or refit occurs.
    """
    native = _chart_renderer()
    height = native.panel_height(panel)
    image = Image.new('RGB', (native.WIDTH, height), 'white')
    native.draw_chart(ImageDraw.Draw(image), 0, panel)
    image = image.crop((96, 136, 1376, height))
    pixels = _tighten_blank_rows(np.array(image))
    image = Image.fromarray(pixels)
    paper = _asset('chart_mount.png').crop((80, 330, 1360, 1160)).resize(image.size, Image.Resampling.BILINEAR)
    paper = Image.blend(paper.filter(ImageFilter.GaussianBlur(20)), Image.new('RGB', image.size, '#FAF0DE'), .70)
    spread = np.ptp(pixels.astype(np.int16), axis=2)
    neutral = (spread <= 8) | ((spread <= 20) & (pixels.min(axis=2) >= 210))
    gray = pixels.astype(float) @ np.array([.2126, .7152, .0722]) / 255
    mix = np.where(gray > .9, 1., gray ** 1.5)
    ink = np.array([63., 45., 34.])
    tinted = np.round(ink + (np.array(paper) - ink) * mix[:, :, None]).astype(np.uint8)
    pixels[neutral] = tinted[neutral]
    # Reference VP's two largest areas retain red/orange; only the other bins warm up.
    other_volume = np.all(pixels == (225, 245, 254), axis=2)
    pixels[other_volume] = (229, 220, 201)
    return Image.fromarray(pixels)


def _levels(panel):
    card = panel.get('key_levels') or panel.get('scorecard') or {}
    close = _finite((panel.get('bars') or [{}])[-1].get('Close'))
    rows, seen = [], set()
    for key, kind in [('resistances_above_close', '壓力'), ('supports_below_close', '支撐')]:
        for item in card.get(key) or []:
            value = _finite(item.get('price'))
            name = _clean(item.get('label') or item.get('name'))
            if value is None or value <= 0 or (name, value) in seen:
                continue
            seen.add((name, value))
            rows.append(dict(kind=kind, name=name, price=value, source=item.get('source', '')))
    rows.sort(key=lambda row: abs(row['price'] / close - 1) if close else row['price'])
    return rows[:5]


def _facts(panel):
    """趨勢／動能 rows come from chart data, so the card stays short and exact."""
    card = panel.get('scorecard') or panel.get('key_levels') or {}
    last = panel['bars'][-1]
    groups = {}
    for key in ('MA5', 'MA10', 'MA20', 'MA60'):
        info = (card.get('ma_deduction') or {}).get(key)
        if isinstance(info, dict) and info.get('direction_now'):
            groups.setdefault(info['direction_now'], []).append(key[2:])
    moves = ['MA' + '／'.join(nums) + ' ' + direction for direction, nums in groups.items()]
    trend = '；'.join(v for v in [_clean(card.get('ma_alignment')), '，'.join(moves)] if v)
    close, ma5 = _finite(last.get('Close')), _finite(last.get('MA5'))
    volume, mv5 = _finite(last.get('Volume')), _finite(last.get('MV5'))
    momentum = []
    if close is not None and ma5:
        momentum.append(('站上' if close >= ma5 else '跌破') + ' MA5 ' + _number(ma5))
    if volume is not None and mv5:
        momentum.append('成交量' + ('高於' if volume >= mv5 else '低於') + ' 5 日均量')
    return [(label, text) for label, text in (('趨勢', trend), ('動能', '；'.join(momentum))) if text]


def _research(panel, answer, ai):
    """Bold conclusion + AI key points (one bullet per sentence) + A/B cards."""
    headline = _clean(ai.get('answer')) or _clean(answer).split('\n')[0]
    points = [s.strip() + '。' for s in re.split('[。\n]', _clean(ai.get('why'))) if s.strip()]
    summary = _clean(ai.get('summary'))
    if summary and summary not in headline:
        points.append(summary.rstrip('。') + '。')
    if not points:
        # AI pending: chart facts keep the card informative instead of blank.
        points = [label + '：' + text for label, text in _facts(panel)]
    if not headline:
        card = panel.get('scorecard') or panel.get('key_levels') or {}
        headline = _clean(card.get('pattern_label')) or '依本次 K 線、均線與價量位置觀察。'
    supplied = [s for s in ai.get('scenarios') or [] if _clean(s.get('text'))]
    if not supplied:
        last = panel['bars'][-1]
        month, short = _finite(last.get('MA20')), _finite(last.get('MA5'))
        supplied = [
            {'title': 'A｜守住月線再攻', 'text': ('月線 ' + _number(month) + ' 守穩，再觀察站回短均 ' + _number(short) + ' 與量能延續。') if month and short else '觀察支撐守穩、壓力突破與量能配合。', 'tone': 'good'},
            {'title': 'B｜跌破月線轉弱', 'text': ('若跌破月線 ' + _number(month) + ' 且量增，留意下一支撐與反彈能否站回。') if month else '觀察支撐失守與量增，留意下一支撐。', 'tone': 'warn'},
        ]
    return headline, points, supplied[:2]


# Scorecard reason「說明（小項 得分/滿分）」→ owning component (weekly_pick.score_pattern).
_REASON_RE = re.compile(r'（([^（）]+?)\s+([\d.]+)/([\d.]+)）\s*$')
_SUB_COMPONENT = {'均線排列': '均線趨勢', 'MA20 方向': '均線趨勢', 'MA60 方向': '均線趨勢',
                  'MA60': '價格位置', '距 MA20': '價格位置', '追高風險': '價格位置',
                  '上方量區壓力': '量區結構', '最大量區': '量區結構', '相對兩大量區': '量區結構',
                  '最近支撐': '下方支撐', '支撐密度': '下方支撐', '開布林': '布林', '通道位置': '布林'}


def _component_notes(card):
    """One short reason per score row: full marks → strongest item, else biggest loss."""
    items = {}
    for text in list(card.get('plus_reasons') or []) + list(card.get('minus_reasons') or []):
        text = _clean(text)
        match = _REASON_RE.search(text)
        if not match or match.group(1) not in _SUB_COMPONENT:
            continue
        points, maximum = float(match.group(2)), float(match.group(3))
        items.setdefault(_SUB_COMPONENT[match.group(1)], []).append((points, maximum, text[:match.start()].strip()))
    notes = {}
    for component in card.get('components') or []:
        rows = items.get(component.get('label'))
        if not rows:
            continue
        value, maximum = _finite(component.get('value')), _finite(component.get('max'))
        full = value is not None and maximum and value >= maximum * .9
        notes[component.get('label')] = (max(rows, key=lambda r: r[0]) if full else
                                         max(rows, key=lambda r: r[1] - r[0]))[2]
    return notes


def _short_note(text, width, size=21):
    """Key point only: drop（…）asides first, then later clauses, before any ellipsis."""
    text = re.sub(r'（[^（）]*）', '', text).strip()
    for candidate in (text, re.split('[，；]', text)[0]):
        for s in (size, size - 2):
            if font(s).getlength(candidate) <= width:
                return candidate, s
    lines, size = _fit(candidate, width, (size - 2,), 1)
    return lines[0], size


class Page:
    def __init__(self):
        self.parts = []
        self.overlays = []

    def band(self, a, b, height):
        image = _asset('template.png').crop((0, a, 793, b)).resize((WIDTH, height), Image.Resampling.LANCZOS)
        self.parts.append(image)
        return image

    @staticmethod
    def text(image, x, y, value, size=25, kind='body', color=INK, anchor='lt'):
        ImageDraw.Draw(image).text((x, y), str(value), font=font(size, kind), fill=color, anchor=anchor)

    @staticmethod
    def protect(image, box, opacity=135):
        mask = Image.new('L', image.size)
        ImageDraw.Draw(mask).rounded_rectangle(box, 12, fill=opacity)
        image.paste(Image.new('RGB', image.size, '#FFF7E9'), (0, 0), mask.filter(ImageFilter.GaussianBlur(12)))

    def lines(self, image, x, y, lines, size=25, kind='body', color=INK, spacing=36):
        for line in lines:
            self.text(image, x, y, line, size, kind, color)
            y += spacing
        return y

    def hero(self, panel):
        # Template rows 252–272 are only desk clutter under the text; trimmed (−36px).
        im = self.band(0, 252, 458)
        self.protect(im, (112, 62, 625, 433), 110)
        self.protect(im, (122, 375, 576, 429), 175)
        t, d = self.text, ImageDraw.Draw(im)
        t(im, 130, 75, '艾斯助手｜喬巴｜研究筆記', 20, 'bold', GOLD)
        t(im, 130, 118, 'ACE RESEARCH', 24, 'classic', GOLD)
        title = _clean(panel.get('stock_code')) + ' ' + _clean(panel.get('stock_name'))
        title_lines = _wrap(title, 472, 58, 'serif')
        self.lines(im, 125, 167, title_lines, 58, 'serif', spacing=62)
        d.line((130, 263, 488, 263), fill='#BFA073')
        d.polygon([(126, 278), (561, 278), (550, 308), (561, 338), (126, 338), (137, 308)], fill='#EDDCB9', outline='#BFA073')
        self.compass(im, 162, 308, 16)
        t(im, 194, 287, '現在型態怎麼看？', 27, 'serif')
        d.line((130, 371, 589, 371), fill='#C7AC86')
        d.line((130, 424, 589, 424), fill='#D4BF9F')
        # Shared baseline across Western date and CJK metadata, not shared top coordinate.
        last = panel['bars'][-1]
        date = str(last.get('date', ''))[:10].replace('-', '/')
        live = panel.get('intraday') or {}
        status = '盤中 ' + str(live.get('time', '')) if live.get('is_live') else '日K收盤'
        for x, value, kind in [(130, date, 'classic'), (341, status, 'body'), (485, '型態研究', 'body')]:
            t(im, x, 407, value, 20, kind, MUTED, 'ls')
        for x in (322, 465):
            d.line((x, 384, x, 413), fill='#CCB38E')

    @staticmethod
    def compass(im, cx, cy, r):
        d = ImageDraw.Draw(im)
        d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=GOLD)
        d.line((cx-r-3, cy, cx+r+3, cy), fill=GOLD)
        d.line((cx, cy-r-3, cx, cy+r+3), fill=GOLD)
        d.polygon([(cx, cy-r+3), (cx+4, cy), (cx, cy+r-3), (cx-4, cy)], fill=GOLD)

    def technical(self, panel):
        header = self.band(280, 465, 236)
        self.protect(header, (112, 38, 1328, 212), 128)
        d = ImageDraw.Draw(header)
        self.text(header, 140, 50, '01  K 線技術研究', 32, 'serif')
        d.line((455, 82, 1130, 82), fill='#BA9B6B')
        self.text(header, 1300, 55, '航海行情札記', 22, 'serif', GOLD, 'rt')
        boxes = [(105,112,398,242),(423,112,717,242),(739,112,1034,242),(1055,112,1342,242)]
        last, mount = panel['bars'][-1], _asset('chart_mount.png')
        for i, (label, key) in enumerate([('收盤價', 'Close'), ('開盤', 'Open'), ('最高', 'High'), ('最低', 'Low')]):
            # Four 280px tiles, 16px gaps: 136…1304 is centred on the 1440 page.
            x = 136 + 296*i
            header.paste(mount.crop(boxes[i]).resize((280,128), Image.Resampling.LANCZOS), (x,104))
            # Label (with compass) and value are each centred on the tile's axis.
            cx = x + 140
            # The close tile's change % rides on the label row so all four values
            # share one size and stay clear of the paper's curled corner.
            change = _finite(panel.get('change_pct')) if i == 0 else None
            tail = f'{change:+.2f}%' if change is not None else ''
            label_w = font(25, 'serif').getlength(label)
            tail_w = font(19, 'bold').getlength(tail) + 10 if tail else 0
            left = cx - (label_w + 30 + tail_w)/2
            self.compass(header, round(left+10), 138, 10)
            self.text(header, left+30, 138, label, 25, 'serif', MUTED, 'lm')
            if tail:
                self.text(header, left+30+label_w+10, 139, tail, 19, 'bold', UP if change>=0 else DOWN, 'lm')
            d.line((x+52,160,x+228,160), fill='#C7AA78')
            text = _number(last.get(key))
            size = 36
            while size > 26 and font(size, 'num').getlength(text) > 200:
                size -= 2
            self.text(header, cx, 210, text, size, 'num', INK, 'ms')
        chart = chart_image(panel)
        body_height = chart.height + 110
        board = self.band(465, 895, body_height)
        material = mount.crop((70, 246, 1374, 1250)).resize((1304, body_height-24), Image.Resampling.LANCZOS)
        mask = Image.new('L', material.size, 255)
        ImageDraw.Draw(mask).rectangle((0,0,material.width-1,material.height-1), outline=0, width=3)
        board.paste(material, (70, 4), mask.filter(ImageFilter.GaussianBlur(7)))
        board.paste(chart, (80, 64))

    def paper_card(self, image, box, color):
        x,y,x2,y2 = box
        paper = _asset('template.png').crop((180,620,560,815)).resize((x2-x,y2-y), Image.Resampling.LANCZOS)
        paper = Image.blend(paper, Image.new('RGB', paper.size, '#FFF5E2'), .35)
        image.paste(paper, (x,y))
        d = ImageDraw.Draw(image)
        d.rectangle(box, outline='#BDA077')
        d.line((x+19,y+25,x+19,y2-22), fill=color, width=4)
        d.ellipse(((x+x2)//2-4,y+9,(x+x2)//2+4,y+17), fill='#BA9663', outline='#88683E')

    # 02 keeps the template's own proportions (895–1255 ≈ 1.816×), so the Chopper
    # scene always ends exactly where 03 begins; text is fitted, never extends paper.
    READ_H = 654
    READ_L, READ_R = 640, 1392
    # A/B cards may hang into 03's empty top-right strip, stopping above its first row.
    READ_OVERLAP = 86

    def _card_plans(self, scenarios, width, bsize):
        inner = width - 74
        plans = [(_fit(s.get('title') or '觀察情境', inner, (24, 22), 1, 'serif'),
                  _fit(s.get('text') or '', inner, (bsize, bsize-1, bsize-2), 3)) for s in scenarios]
        content = max((t[1] + 30 + len(b[0])*(b[1]+10) for t, b in plans), default=0)
        return plans, content + 46

    def reading(self, headline, points, scenarios):
        H, L, R = self.READ_H, self.READ_L, self.READ_R
        im = self.band(895, 1255, H)
        band_y = sum(part.height for part in self.parts[:-1])
        d = ImageDraw.Draw(im)
        # Covers the template's blank green/pink slips; our A/B cards replace them.
        self.protect(im, (L-14, 18, R+14, H-8), 235)
        self.text(im, L+26, 40, '02  喬巴研究解讀', 32, 'serif')
        d.line((L+26, 96, R-26, 96), fill='#C7AC7F')
        tw = R - L - 52
        head, size = _fit(headline, tw, (28, 26), 2, 'bold')
        if head[-1].endswith('…'):
            head, size = _fit(headline, tw, (24,), 3, 'bold')
        head_h = len(head)*(size+12)
        gap = 22
        width = (R - L - gap) // 2 if len(scenarios) > 1 else R - L
        limit = H + self.READ_OVERLAP if scenarios else H - 30
        # Largest bullet size that fits; only if 20px still overflows, drop whole
        # trailing sentences (never cut one mid-way).
        for bsize in (23, 22, 21, 20):
            wrapped = [_wrap(p, tw - 26, bsize) for p in points]
            plans, card_h = self._card_plans(scenarios, width, min(bsize, 22)) if scenarios else ([], 0)
            def stack(items):
                text_h = head_h + (14 + sum(len(w)*(bsize+10) + 6 for w in items) if items else 0)
                return text_h + (24 + card_h if scenarios else 0)
            if 116 + stack(wrapped) <= limit:
                break
        while len(wrapped) > 1 and 116 + stack(wrapped) > limit:
            wrapped.pop()
        # Text is top-anchored; cards sit on the paper's lower edge unless pushed down.
        y = 116
        y = self.lines(im, L+26, y, head, size, 'bold', spacing=size+12)
        if wrapped:
            y += 14
            for lines in wrapped:
                dot = y + bsize*0.62
                d.ellipse((L+30, dot-4, L+38, dot+4), fill=GOLD)
                y = self.lines(im, L+52, y, lines, bsize, spacing=bsize+10) + 6
        if not scenarios:
            return
        top = band_y + max(y + 24 - 6, H - 24 - card_h)
        def draw_cards(page, top=top, plans=plans, card_h=card_h):
            pd = ImageDraw.Draw(page)
            for i, (scenario, ((titles, tsize), (body, bsize))) in enumerate(zip(scenarios, plans)):
                x = L + i*(width + gap)
                color = UP if scenario.get('tone') == 'good' else DOWN if scenario.get('tone') == 'warn' else GOLD
                self.paper_card(page, (x, top, x+width, top+card_h), color)
                cy = top + 26
                self.text(page, x+44, cy, titles[0], tsize, 'serif', color)
                cy += tsize + 12
                pd.line((x+43, cy, x+width-24, cy), fill='#CBB694')
                self.lines(page, x+44, cy+16, body, bsize, spacing=bsize+10)
        # Drawn after assembly so a card may overlap the start of 03.
        self.overlays.append(draw_cards)

    def score(self, panel):
        card = panel.get('scorecard') or {}
        components = list(card.get('components') or [])
        notes = _component_notes(card)
        row_h = 44
        rows_top = 108
        rows_h = max(5, len(components)) * row_h
        tag = '｜'.join(f'{label}：{_clean(card[key])}' for label, key in
                       (('型態', 'pattern_label'), ('均線', 'ma_alignment')) if _clean(card.get(key)))
        height = rows_top + rows_h + (44 if tag else 0) + 40
        im = self.band(1255,1578,height)
        self.protect(im,(112,25,1328,height-17),128)
        self.text(im,140,42,'03  完整型態評分依據',32,'serif')
        d = ImageDraw.Draw(im)
        y = rows_top + (rows_h - len(components)*row_h)//2
        for component in components:
            # Each row: label | bar | score | note share one text middle line.
            mid = y + row_h//2
            label, lsize = _fit(component.get('label',''), 170, (25, 22), 1, 'bold')
            self.text(im,140,mid,label[0],lsize,'bold',INK,'lm')
            value, maximum = _finite(component.get('value')), _finite(component.get('max'))
            ratio = max(0.,min(1.,value/maximum)) if value is not None and maximum and maximum>0 else 0
            d.rectangle((330,mid-4,610,mid+4),fill='#DFCCAD')
            if ratio:
                d.rectangle((330,mid-4,330+round(280*ratio),mid+4),fill=GOLD)
            for j in range(5):
                d.line((330+70*j,mid-7,330+70*j,mid+7),fill='#AE8F61')
            text = f'{value:g} / {maximum:g}' if value is not None and maximum is not None else '—'
            self.text(im,760,mid,text,25,'num',GOLD,'rm')
            note = notes.get(component.get('label'))
            if note:
                line, nsize = _short_note(note, 1100-790)
                self.text(im,790,mid,line,nsize,'body',MUTED,'lm')
            y += row_h
        if tag:
            line, tsize = _fit(tag, 1180-140, (21, 19), 1)
            self.text(im,140,rows_top+rows_h+22,line[0],tsize,'bold',MUTED,'lm')
        score = None if card.get('hide_score') else _finite(card.get('pattern_score'))
        cx,cy = 1222,rows_top+rows_h//2-14
        d.ellipse((cx-75,cy-75,cx+75,cy+75),fill='#F6E9CC',outline='#AF8E59',width=2)
        d.ellipse((cx-69,cy-69,cx+69,cy+69),outline='#D3BA91')
        d.arc((cx-59,cy-59,cx+59,cy+59),0,360,fill='#DCC9A6',width=8)
        if score is not None:
            d.arc((cx-59,cy-59,cx+59,cy+59),-90,-90+360*max(0,min(1,score/100)),fill=GOLD,width=8)
        for i in range(8):
            a=math.radians(i*45)
            x0,y0=cx+math.cos(a)*76,cy+math.sin(a)*76
            x1,y1=cx+math.cos(a)*84,cy+math.sin(a)*84
            d.line((x0,y0,x1,y1),fill='#A78A5B',width=3)
            d.ellipse((x1-2,y1-2,x1+2,y1+2),fill='#B79A68',outline='#8E6D3D')
        for i in range(20):
            a=math.radians(i*18-90)
            d.line((cx+math.cos(a)*68,cy+math.sin(a)*68,cx+math.cos(a)*72,cy+math.sin(a)*72),fill='#B79A68')
        self.text(im,cx,cy,f'{score:.1f}%' if score is not None else '—',40,'pirate',INK,'mm')
        grade = card.get('grade') if score is not None else '評分待取得'
        for i,line in enumerate(_wrap(grade or '',184,23,'bold')[:2]):
            self.text(im,cx,cy+112+i*30,line,23,'bold',UP if score is not None else MUTED,'mm')

    def prices(self, panel):
        rows, close = _levels(panel), _finite(panel['bars'][-1].get('Close'))
        plans = [(_wrap(row['name'],190,25),row) for row in rows]
        card = panel.get('scorecard') or panel.get('key_levels') or {}
        deduction = []
        for key in ('MA5', 'MA10', 'MA20', 'MA60'):
            info = (card.get('ma_deduction') or {}).get(key)
            if isinstance(info, dict):
                text = _chart_renderer().public_ma_condition(info)
                deduction.append((key, text[2:].strip() if text.startswith('明天') else text, info))
        above = [r for r in rows if r['kind'] == '壓力']
        below = [r for r in rows if r['kind'] == '支撐']
        span = (f'最近壓力與支撐相距 {(above[0]["price"]/below[0]["price"]-1)*100:.2f}%'
                if above and below else '')
        left_h = 96 + len(deduction)*40 + (46 if span else 0) + 26
        table_h = 116 + sum(max(45,len(lines)*32+12) for lines,row in plans) + 26
        height = max(380, left_h, table_h)
        im = self.band(1578,1840,height)
        # Stronger veil: the template's compass/book sits under the left column.
        self.protect(im,(112,20,604,height-20),215)
        self.protect(im,(628,20,1328,height-20),130)
        self.text(im,140,31,'04  關鍵價位與技術明細',32,'serif')
        d=ImageDraw.Draw(im)
        # Left: tomorrow's MA deduction conditions (price conditions only).
        y = 96
        if deduction:
            self.text(im,140,y-6,'明日扣抵條件',24,'serif',GOLD)
            d.line((140,y+30,576,y+30),fill='#C7AC86')
            y += 44
            for key, text, info in deduction:
                color = UP if info.get('direction_now') == '上揚' else MUTED
                self.text(im,140,y+16,key,23,'num',INK,'lm')
                line, size = _fit(text, 576-232, (23, 21), 1)
                self.text(im,232,y+16,line[0],size,'body',color,'lm')
                y += 40
        if span:
            d.line((140,y+8,576,y+8),fill='#D4BF9F')
            self.text(im,140,y+30,span,22,'body',MUTED,'lm')
        elif not deduction:
            self.text(im,140,y,'本次未取得扣抵條件。',23,'body',MUTED)
        # Right: nearest levels, columns anchored inside the 628–1328 paper.
        for x,label,anchor in [(664,'類型','lt'),(790,'名稱','lt'),(1120,'價位','rt'),(1296,'距現價','rt')]:
            self.text(im,x,74,label,25,'bold',MUTED,anchor)
        d.line((652,110,1304,110),fill='#BFA376')
        y=122
        for i,(lines,row) in enumerate(plans):
            rh=max(45,len(lines)*32+12)
            if i%2==0:
                d.rectangle((652,y-3,1304,y+rh-7),fill='#EFE2CB')
            color = UP if row['kind']=='壓力' else DOWN
            # Every column shares the row's vertical centre (mixed fonts, same line).
            mid = y + (rh-10)//2
            self.text(im,664,mid,row['kind'],25,'body',color,'lm')
            for k, line in enumerate(lines):
                self.text(im,790,mid+(k-(len(lines)-1)/2)*32,line,25,'body',INK,'lm')
            self.text(im,1120,mid,_number(row['price']),27,'num',INK,'rm')
            distance = f'{(row["price"]/close-1)*100:+.2f}%' if close else '—'
            self.text(im,1296,mid,distance,25,'num',color,'rm')
            y+=rh

    def finish(self):
        footer=self.band(1840,1983,105)
        self.text(footer,720,68,'股市艾斯',28,'serif',GOLD,'mm')
        image=Image.new('RGB',(WIDTH,sum(part.height for part in self.parts)))
        y=0
        for part in self.parts:
            image.paste(part,(0,y))
            y+=part.height
        for overlay in self.overlays:
            overlay(image)
        return image


def render_answer(question, answer, panels=None):
    panels = list(panels or [])
    charts = [p for p in panels if p.get('bars')]
    # Errors, comparisons and unrelated richer cards keep their complete existing output.
    unsupported = [p for p in panels if p not in charts and not p.get('ai_card')]
    if len(charts)!=1 or unsupported:
        import answer_image
        return answer_image.render_answer(question,answer,panels)
    panel=charts[0]
    ai_cards=[p['ai_card'] for p in panels if p.get('ai_card')]
    if len(ai_cards)>1:
        import answer_image
        return answer_image.render_answer(question,answer,panels)
    headline,points,scenarios=_research(panel,answer,ai_cards[0] if ai_cards else {})
    page=Page()
    page.hero(panel)
    page.technical(panel)
    page.reading(headline,points,scenarios)
    page.score(panel)
    page.prices(panel)
    return page.finish()


def make_attachment(question, answer, panels=None, *, max_bytes=7_500_000):
    # Same native attachment policy; compression never rescales native chart geometry.
    return _chart_renderer().encode_image(render_answer(question,answer,panels),max_bytes)
