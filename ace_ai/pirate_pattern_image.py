"""Test-only pirate pattern report. Native chart geometry and data are preserved.

No network, quote fetching, AI calls, database access, or changes to answer_image.
The artwork is the approved A template; all text and values come from this request.
"""
from __future__ import annotations

import importlib.util
import math
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


def _wrap(text, width, size=25, kind='body'):
    f, lines = font(size, kind), []
    for paragraph in _clean(text).splitlines():
        line = ''
        for char in paragraph:
            if line and f.getlength(line + char) > width:
                lines.append(line.rstrip())
                line = ''
            line += char
        if line.strip():
            lines.append(line.rstrip())
    return lines


@lru_cache(maxsize=2)
def _asset(name):
    with Image.open(ASSETS / name) as image:
        return image.convert('RGB')


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
    pixels = np.array(image)
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


def _research(panel, answer, ai):
    """Preserve supplied interpretation; use observable facts while AI is pending."""
    paragraphs = []
    for key in ('answer', 'summary', 'why', 'context', 'notice', 'cost_basis', 'footer'):
        value = _clean(ai.get(key))
        if value and value not in paragraphs:
            paragraphs.append(value)
    if not paragraphs:
        paragraphs = [_clean(answer)] if _clean(answer) else []
    if not paragraphs:
        card = panel.get('scorecard') or panel.get('key_levels') or {}
        paragraphs = [card.get('pattern_label') or '依本次 K 線、均線與價量位置觀察。']
        if card.get('ma_alignment'):
            paragraphs.append('均線：' + str(card['ma_alignment']))
    supplied = list(ai.get('scenarios') or [])
    # Retain additional scenarios as prose rather than silently drop them.
    for scenario in supplied[2:]:
        paragraphs.append(str(scenario.get('title', '')) + '：' + str(scenario.get('text', '')))
    if not supplied:
        last = panel['bars'][-1]
        month, short = _finite(last.get('MA20')), _finite(last.get('MA5'))
        supplied = [
            {'title': 'A｜守住月線再攻', 'text': ('月線 ' + _number(month) + ' 守穩，再觀察站回短均 ' + _number(short) + ' 與量能延續。') if month and short else '觀察支撐守穩、壓力突破與量能配合。', 'tone': 'good'},
            {'title': 'B｜跌破月線轉弱', 'text': ('若跌破月線 ' + _number(month) + ' 且量增，留意下一支撐與反彈能否站回。') if month else '觀察支撐失守與量增，留意下一支撐。', 'tone': 'warn'},
        ]
    return paragraphs, supplied


class Page:
    def __init__(self):
        self.parts = []

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
        im = self.band(0, 272, 494)
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
        self.protect(header, (113, 38, 1338, 212), 128)
        d = ImageDraw.Draw(header)
        self.text(header, 140, 50, '01  K 線技術研究', 32, 'serif')
        d.line((455, 82, 1081, 82), fill='#BA9B6B')
        self.text(header, 1122, 55, '航海行情札記', 22, 'serif', GOLD)
        boxes = [(105,112,398,242),(423,112,717,242),(739,112,1034,242),(1055,112,1342,242)]
        last, mount = panel['bars'][-1], _asset('chart_mount.png')
        for i, (label, key) in enumerate([('收盤價', 'Close'), ('開盤', 'Open'), ('最高', 'High'), ('最低', 'Low')]):
            x = 128 + 296*i
            header.paste(mount.crop(boxes[i]).resize((280,128), Image.Resampling.LANCZOS), (x,104))
            self.compass(header, x+36, 140, 10)
            self.text(header, x+64, 115, label, 25, 'serif', MUTED)
            d.line((x+64,157,x+209,157), fill='#C7AA78')
            size = 40 if i == 0 else 36
            text = _number(last.get(key))
            if font(size, 'num').getlength(text) > 164:
                size = 30
            self.text(header, x+25, 212, text, size, 'num', INK, 'ls')
            change = _finite(panel.get('change_pct'))
            if i == 0 and change is not None:
                self.text(header, x+249, 184, f'{change:+.2f}%', 21, 'bold', UP if change>=0 else DOWN, 'rs')
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

    def reading(self, paragraphs, scenarios):
        main_lines = []
        for paragraph in paragraphs:
            main_lines.extend(_wrap(paragraph, 640, 25))
        scenario_lines = [(_wrap(s.get('title') or '觀察情境', 236, 26, 'serif'), _wrap(s.get('text') or '', 236, 25)) for s in scenarios]
        top_height = max(310, 182 + len(main_lines)*36)
        card_height = max(235, max((len(a)*34 + len(b)*36 + 73 for a,b in scenario_lines), default=0))
        height = max(625, top_height + card_height + 35)
        # Keep the body character at the approved size/pose when text requires more paper.
        im = self.band(895, 1195, 625)
        if height > 625:
            extension = _asset('template.png').crop((25,1350,765,1520)).resize((WIDTH, height), Image.Resampling.LANCZOS)
            extension.paste(im, (0,0))
            self.parts[-1] = im = extension
        self.protect(im, (652,92,1338,height-25), 145)
        self.text(im, 675, 108, '02  喬巴研究解讀', 32, 'serif')
        ImageDraw.Draw(im).line((675,166,1308,166), fill='#C7AC7F')
        self.lines(im, 675, 185, main_lines)
        for i, (scenario, (titles, body)) in enumerate(zip(scenarios, scenario_lines)):
            x = 665 + 335*i
            color = UP if scenario.get('tone') == 'good' else DOWN if scenario.get('tone') == 'warn' else GOLD
            self.paper_card(im, (x,top_height,x+310,top_height+card_height), color)
            cursor = self.lines(im, x+42, top_height+27, titles, 26, 'serif', color, 34)
            ImageDraw.Draw(im).line((x+41,cursor+7,x+288,cursor+7), fill='#CBB694')
            self.lines(im, x+42, cursor+22, body)

    def score(self, panel):
        card = panel.get('scorecard') or {}
        components = list(card.get('components') or [])
        details = []
        for label, key in [('加分', 'plus_reasons'), ('扣分', 'minus_reasons')]:
            reasons = [_clean(v) for v in card.get(key) or [] if _clean(v)]
            if reasons:
                details.extend(_wrap(label+'：'+'；'.join(reasons), 1180, 22))
        for label, key in [('型態', 'pattern_label'), ('均線', 'ma_alignment'), ('評分基準', 'score_basis')]:
            if card.get(key):
                details.extend(_wrap(label+'：'+str(card[key]), 1180, 22))
        deductions = card.get('ma_deduction') or {}
        for key, info in deductions.items():
            if isinstance(info, dict):
                details.extend(_wrap(str(key)+'：'+_chart_renderer().public_ma_condition(info), 1180, 22))
        if card.get('show_tracked_branches') and card.get('tracked_branches'):
            tracked = card['tracked_branches']
            details.extend(_wrap('追蹤分點：'+str(tracked), 1180, 22))
        component_height = max(188, sum(max(37, len(_wrap(c.get('label',''),190,25,'bold'))*32) for c in components))
        height = max(370, 110+component_height+len(details)*32+35)
        im = self.band(1260,1580,height)
        self.protect(im,(114,25,1338,height-17),128)
        self.text(im,140,45,'03  完整型態評分依據',32,'serif')
        d, y = ImageDraw.Draw(im), 112
        for component in components:
            labels = _wrap(component.get('label',''),190,25,'bold')
            self.lines(im,140,y,labels,25,'bold',spacing=32)
            value, maximum = _finite(component.get('value')), _finite(component.get('max'))
            ratio = max(0.,min(1.,value/maximum)) if value is not None and maximum and maximum>0 else 0
            d.rectangle((350,y+10,650,y+18),fill='#DFCCAD')
            if ratio:
                d.rectangle((350,y+10,350+round(300*ratio),y+18),fill=GOLD)
            for j in range(5):
                d.line((350+75*j,y+7,350+75*j,y+21),fill='#AE8F61')
            text = f'{value:g} / {maximum:g}' if value is not None and maximum is not None else '—'
            self.text(im,805,y+26,text,25,'num',GOLD,'rs')
            y += max(37,len(labels)*32)
        score = None if card.get('hide_score') else _finite(card.get('pattern_score'))
        cx,cy = 1247,150
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
        self.text(im,cx,cy,f'{score:.1f}%' if score is not None else '—',27,'classic',INK,'mm')
        grade = card.get('grade') if score is not None else '評分待取得'
        for i,line in enumerate(_wrap(grade or '',184,23,'bold')):
            self.text(im,cx,265+i*32,line,23,'bold',UP if score is not None else MUTED,'mm')
        self.lines(im,140,110+component_height,details,22,color=MUTED,spacing=32)

    def prices(self, panel):
        rows, close = _levels(panel), _finite(panel['bars'][-1].get('Close'))
        plans = [(_wrap(row['name'],190,25),row) for row in rows]
        height = max(380,100+sum(max(45,len(lines)*32+12) for lines,row in plans)+30)
        im = self.band(1580,1840,height)
        self.protect(im,(116,20,593,260),120)
        self.protect(im,(635,25,1340,height-20),130)
        self.text(im,140,31,'04  關鍵價位與技術明細',32,'serif')
        sources = {'ma':'均線','volume_zone':'量區','bollinger':'布林','triangle':'三角邊界'}
        kinds = list(dict.fromkeys(sources.get(row['source'],'技術位置') for row in rows))
        guide = (f'本表取自「{"、".join(kinds)}」。\n' if rows else '本次未取得可列示的關鍵價位。\n')
        guide += f'列出距現價最近的 {len(rows)} 筆；\n正／負值代表現價上／下方，\n先看近價，再確認站回或跌破。'
        self.lines(im,140,98,_wrap(guide,430,23),23,color=MUTED,spacing=35)
        d=ImageDraw.Draw(im)
        for x,label,anchor in [(660,'類型','lt'),(802,'名稱','lt'),(1120,'價位','rt'),(1310,'距現價','rt')]:
            self.text(im,x,74,label,25,'bold',MUTED,anchor)
        d.line((650,110,1320,110),fill='#BFA376')
        y=122
        for i,(lines,row) in enumerate(plans):
            rh=max(45,len(lines)*32+12)
            if i%2==0:
                d.rectangle((650,y-3,1320,y+rh-7),fill='#EFE2CB')
            color = UP if row['kind']=='壓力' else DOWN
            self.text(im,660,y,row['kind'],25,color=color)
            self.lines(im,802,y,lines,25,spacing=32)
            self.text(im,1120,y,_number(row['price']),27,'num',INK,'rt')
            distance = f'{(row["price"]/close-1)*100:+.2f}%' if close else '—'
            self.text(im,1310,y,distance,25,'num',color,'rt')
            y+=rh

    def finish(self):
        footer=self.band(1840,1983,105)
        self.text(footer,720,68,'股市艾斯',28,'serif',GOLD,'mm')
        image=Image.new('RGB',(WIDTH,sum(part.height for part in self.parts)))
        y=0
        for part in self.parts:
            image.paste(part,(0,y))
            y+=part.height
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
    paragraphs,scenarios=_research(panel,answer,ai_cards[0] if ai_cards else {})
    page=Page()
    page.hero(panel)
    page.technical(panel)
    page.reading(paragraphs,scenarios[:2])
    page.score(panel)
    page.prices(panel)
    return page.finish()


def make_attachment(question, answer, panels=None, *, max_bytes=7_500_000):
    # Same native attachment policy; compression never rescales native chart geometry.
    return _chart_renderer().encode_image(render_answer(question,answer,panels),max_bytes)
