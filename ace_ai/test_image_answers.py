"""Offline regressions: layout, numeric chart source, cache, image-only transport."""
import ast
import io
from pathlib import Path
import time
import unittest
from unittest.mock import patch, Mock

import pandas as pd
from PIL import Image
import answer_image as image
import discord_ai_bot as bot
import warrant_ai_tools as tools
from demo_images import sample_panel


class ImageTests(unittest.TestCase):
    def test_long_text_wrap_preserves_content(self):
        text = '資料來源與注意事項：' + '股票分析AB123網址/' * 150
        lines = image.wrap(text, 29, 1100)
        self.assertEqual(''.join(lines), text)
        self.assertTrue(all(image.font(29).getlength(line) <= 1100 for line in lines))

    def test_all_sections_and_final_disclaimer_fit(self):
        text = '\n'.join(f'【觀察{i}】' + '長內容測試' * 18 for i in range(25)) + '\n※ END'
        blocks = image.body_blocks(text)
        self.assertEqual(blocks[-1].lines, ['※ END'])
        picture = image.render_answer('長問題' * 40, text, [sample_panel()] * 2)
        self.assertGreater(picture.height, 6000)
        data, extension = image.encode_image(picture)
        self.assertEqual(Image.open(io.BytesIO(data)).size, picture.size)
        self.assertLess(len(data), 7_500_000)

    def test_no_stock_and_failed_stock_still_render(self):
        for panels in ([], [{'stock_code': '2344', 'error': '資料暫時無法取得'}]):
            data, ext = image.make_attachment('使用說明', bot.HELP_MESSAGE, panels)
            self.assertGreater(Image.open(io.BytesIO(data)).height, 500)

    def test_flat_single_candle_and_missing_volume(self):
        panel = {'bars': [{'date': '2026-01-01', 'Open': 10, 'High': 10, 'Low': 10, 'Close': 10, 'Volume': None}]}
        self.assertEqual(image.render_answer('單筆資料', '說明', [panel]).width, 1440)

    def test_attachment_cap_is_enforced(self):
        with self.assertRaises(ValueError):
            image.encode_image(image.render_answer('題目', '回答'), max_bytes=10)

    def test_chart_source_filters_bad_bars_and_sorts(self):
        df = pd.DataFrame([
            {'Open': 12, 'High': 14, 'Low': 11, 'Close': 13},
            {'Open': 10, 'High': 12, 'Low': 9, 'Close': 11},
            {'Open': 10, 'High': 8, 'Low': 9, 'Close': 11},
        ], index=pd.to_datetime(['2026-01-02', '2026-01-01', '2026-01-03']))
        core = Mock()
        core._normalize_stock_name_code_key.side_effect = str
        core.CHART_LOOKBACK = 70
        core._calculate_weighted_volume_profile_stats.return_value = {}
        with patch.object(tools, 'core', return_value=core), patch.object(tools, '_load_price_bundle', return_value={'df': df}), patch.object(tools, 'resolve_stock_name', return_value='測試'):
            panel = tools.get_chart_panel('1234')
        self.assertEqual([b['Close'] for b in panel['bars']], [11, 13])
        self.assertAlmostEqual(panel['change_pct'], (13/11-1)*100)

    def test_full_volume_profile_uses_reference_function(self):
        sample = sample_panel()
        df = pd.DataFrame(sample['bars']).set_index('date')
        df.index = pd.to_datetime(df.index)
        stats = sample['volume_profile']
        core = Mock()
        core._normalize_stock_name_code_key.side_effect = str
        core.CHART_LOOKBACK = 70
        core._calculate_weighted_volume_profile_stats.return_value = stats
        with patch.object(tools, 'core', return_value=core), patch.object(tools, '_load_price_bundle', return_value={'df': df}), patch.object(tools, 'resolve_stock_name', return_value='示範'):
            panel = tools.get_chart_panel('1234')
        self.assertEqual(panel['volume_profile'], stats)
        args, kwargs = core._calculate_weighted_volume_profile_stats.call_args
        self.assertEqual(kwargs, {'n_bins': 40})
        self.assertEqual(len(args[0]), 70)
        self.assertEqual(len(panel['volume_profile']['profile']), 40)
        for actual, original in zip(panel['bars'], sample['bars']):
            for key in ('BB_UPPER', 'BB_MID', 'BB_LOWER'):
                self.assertEqual(actual[key], original[key])
        self.assertEqual(panel['bollinger']['upper'], round(sample['bars'][-1]['BB_UPPER'], 4))

    def test_profile_bar_width_colors_and_price_bounds(self):
        profile = {'bins': [10, 20, 30, 40], 'profile': [25, 100, 50], 'max_idx': 1, 'second_idx': 2}
        rectangles = image.volume_profile_rectangles(profile, 10, 1090, lambda x: 500-x*10, 10, 40)
        self.assertEqual([round(rect[2]-rect[0]) for rect, _ in rectangles], [250, 1000, 500])
        self.assertEqual([color for _, color in rectangles], [(225, 245, 254), (248, 212, 212), (253, 236, 206)])
        self.assertEqual(rectangles[0][0][1::2], (300, 400))

    def test_missing_or_invalid_profile_never_fabricates_bands(self):
        for profile in ({}, {'bins': [1, 2], 'profile': [0]}, {'bins': [2, 1], 'profile': [5]}, {'bins': [1, 2], 'profile': [float('nan')]}):
            self.assertEqual(image.volume_profile_rectangles(profile, 0, 100, lambda x: x, 0, 10), [])

    def test_cache_preserves_panels_without_network(self):
        engine = bot.AceQueryEngine(bot.BotConfig.from_env())
        try:
            expected = bot.AnswerResult('答案', 'test', 1, 1, cacheable=True, panels=[sample_panel()])
            with patch.object(engine, '_answer_uncached', return_value=expected) as call:
                first = engine.answer('2344技術面')
                second = engine.answer('2344 技術面')
            self.assertEqual(call.call_count, 1)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.panels, first.panels)
            self.assertEqual(second.gemini_calls, 0)
        finally:
            engine.executor.shutdown()

    def test_chart_data_not_sent_to_gemini(self):
        engine = bot.AceQueryEngine(bot.BotConfig.from_env())
        parsed = bot.ParsedQuestion('2344', set(), stocks=[('2344', '測試')])
        plan = bot.QueryPlan('test', [bot.ToolCall('get_stock_overview', {'stock_code': '2344'})])
        results = [tools.ToolResult('get_stock_overview', True, {'stock_code': '2344'}),
                   tools.ToolResult('get_chart_panel', True, sample_panel())]
        try:
            with patch.object(engine.parser, 'parse', return_value=parsed), patch.object(engine.router, 'plan', return_value=plan), patch.object(engine, '_run_tools', return_value=results), patch.object(engine, '_compose', return_value=('答案', True)) as compose:
                result = engine._answer_uncached('2344', time.perf_counter())
            self.assertEqual(len(result.panels), 1)
            self.assertEqual([r.name for r in compose.call_args.args[2]], ['get_stock_overview'])
        finally:
            engine.executor.shutdown()

    def test_all_discord_sends_use_only_file(self):
        tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_discord_bot')
        sends = [n for n in ast.walk(run) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ('send', 'reply', 'send_message')]
        self.assertEqual(len(sends), 2)
        for call in sends:
            self.assertFalse(call.args)
            self.assertIn('file', [k.arg for k in call.keywords])
            self.assertNotIn('content', [k.arg for k in call.keywords])


if __name__ == '__main__':
    unittest.main()
