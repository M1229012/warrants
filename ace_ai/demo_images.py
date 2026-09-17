"""Offline layout examples. All market prices are synthetic, never live data."""
from pathlib import Path
import math
import pandas as pd
import answer_image


def sample_panel():
    bars = []
    dates = pd.bdate_range('2026-06-10', periods=70)
    for i, date in enumerate(dates):
        opening = 88 + i * .27 + math.sin(i * .4) * 2.5
        close = opening + math.sin(i * 1.7) * 1.2
        bars.append({'date': date.strftime('%Y-%m-%d'), 'Open': opening,
                     'High': max(opening, close) + 1.1, 'Low': min(opening, close) - .8,
                     'Close': close, 'Volume': 1000 + (i * 317) % 2800})
    for i, bar in enumerate(bars):
        for days in (5, 10, 20, 60):
            bar[f'MA{days}'] = sum(x['Close'] for x in bars[max(0, i-days+1):i+1]) / min(i+1, days)
    return {'stock_code': 'DEMO', 'stock_name': '示範股票', 'bars': bars,
            'change_pct': (bars[-1]['Close'] / bars[-2]['Close'] - 1) * 100,
            'zones': [{'price_low': 99.0, 'price_high': 101.0}]}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default='demo-output')
    args = parser.parse_args()
    dest = Path(args.output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    text = '''【走勢觀察】
    股價沿短期均線震盪向上；觀察回檔時，能否持續守住均線附近。
    【量價重點】
    淡紅色帶表示示範大量區 99～101 元，對照價格與成交量觀察。
    【後續留意】
    上攻時留意成交量是否同步增加；若跌破大量區，再檢視趨勢是否改變。
    ※ 本圖數據全部為合成示範，僅供確認排版，並非真實股票分析。'''
    answer_image.render_answer('這檔股票目前技術面怎麼樣？', text, [sample_panel()], demo=True).save(dest / 'stock-preview.png')
    answer_image.render_answer('!ace 可以做什麼？', '''【個股研究】
    !ace 2344股價
    !ace 2344現在技術面怎麼樣
    !ace 華邦電現在在大量區哪裡
    【權證與分點】
    !ace 2344有哪些高勝率分點最近在加碼
    !ace 永豐金內湖D事件勝率
    !ace 永豐金內湖最近在買什麼
    【新聞與本週精選】
    !ace 2344最近有什麼新聞
    !ace 本週精選
    !ace 本週精選 只看D事件 勝率70%以上 排除漲太多
    【使用方式】
    /ask 與 !ace 共用分析功能。所有回覆皆以圖片呈現。''').save(dest / 'help-preview.png')
    print(f'Created previews in {dest}')


if __name__ == '__main__':
    main()
