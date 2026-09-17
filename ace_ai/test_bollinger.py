import unittest
from unittest.mock import Mock, patch
import pandas as pd
import numpy as np
from bollinger_analysis import analyze_bollinger
import discord_ai_bot as bot
import warrant_ai_tools as tools


def frame(n=90):
    return pd.DataFrame({'Close': [100.]*n, 'High': [101.]*n, 'Low': [99.]*n,
                         'BB_UPPER': [110.]*n, 'BB_MID': [100.]*n, 'BB_LOWER': [90.]*n,
                         'Volume': [1000.]*n}, index=pd.bdate_range('2026-01-01', periods=n))


class BollingerTests(unittest.TestCase):
    def test_close_breakout_differs_from_wick_touch(self):
        df = frame()
        df.loc[df.index[-1], ['Close', 'High']] = [111, 112]
        self.assertTrue(analyze_bollinger(df)['breakout_up'])
        df.loc[df.index[-1], 'Close'] = 109
        result = analyze_bollinger(df)
        self.assertFalse(result['breakout_up'])
        self.assertIn('盤中穿越上軌，收盤未站上', result['signals'])

    def test_down_breakout_and_reentry(self):
        df = frame()
        df.loc[df.index[-1], ['Close', 'Low']] = [89, 88]
        self.assertTrue(analyze_bollinger(df)['breakout_down'])
        df.loc[df.index[-2], 'Close'] = 89
        df.loc[df.index[-1], 'Close'] = 95
        self.assertTrue(analyze_bollinger(df)['return_inside'])

    def test_already_outside_is_not_new_breakout(self):
        df = frame()
        df.loc[df.index[-2:], 'Close'] = 111
        result = analyze_bollinger(df)
        self.assertFalse(result['breakout_up'])
        self.assertIn('收盤持續位於上軌外', result['signals'])

    def test_squeeze_excludes_today_from_reference(self):
        df = frame()
        df.loc[df.index[-1], ['BB_UPPER', 'BB_LOWER']] = [102, 98]
        result = analyze_bollinger(df)
        self.assertTrue(result['squeeze'])
        self.assertEqual(result['squeeze_threshold_pct'], 20)
        self.assertEqual(result['width_pct_of_mid'], 4)
        self.assertEqual(result['width_trend'], '收窄')

    def test_not_enough_history_stays_unknown(self):
        result = analyze_bollinger(frame(30))
        self.assertIsNone(result['squeeze'])
        self.assertEqual(result['squeeze_reference_count'], 29)

    def test_compression_followed_by_breakout(self):
        df = frame()
        df.loc[df.index[-5:], ['BB_UPPER', 'BB_LOWER']] = [105, 95]
        df.loc[df.index[-1], 'Close'] = 106
        result = analyze_bollinger(df)
        self.assertEqual(result['squeeze_breakout'], '壓縮後向上突破')
        df.loc[df.index[-1], 'Close'] = 94
        self.assertEqual(analyze_bollinger(df)['squeeze_breakout'], '壓縮後向下跌破')

    def test_flat_market_and_trending_market_are_distinct(self):
        df = frame()
        self.assertTrue(analyze_bollinger(df)['sideways'])
        df.loc[df.index[-10:], 'BB_MID'] = np.linspace(97, 100, 10)
        self.assertFalse(analyze_bollinger(df)['sideways'])

    def test_expanding_bands_not_classified_sideways(self):
        df = frame()
        df.loc[df.index[-1], ['BB_UPPER', 'BB_LOWER']] = [120, 80]
        result = analyze_bollinger(df)
        self.assertEqual(result['width_trend'], '擴張')
        self.assertFalse(result['sideways'])

    def test_band_walk_requires_direction(self):
        df = frame()
        df.loc[df.index[-3:], 'Close'] = 109
        self.assertEqual(analyze_bollinger(df)['band_walk'], '未符合沿軌條件')
        df.loc[df.index[-6:], 'BB_MID'] = np.linspace(99, 100, 6)
        self.assertEqual(analyze_bollinger(df)['band_walk'], '沿上軌')
        df.loc[df.index[-3:], 'Close'] = 91
        df.loc[df.index[-6:], 'BB_MID'] = np.linspace(101, 100, 6)
        self.assertEqual(analyze_bollinger(df)['band_walk'], '沿下軌')

    def test_missing_bands_and_zero_width(self):
        df = frame()
        df.loc[df.index[-1], 'BB_UPPER'] = float('nan')
        self.assertIsNone(analyze_bollinger(df)['breakout_up'])
        self.assertEqual(analyze_bollinger(df)['position'], '資料不足')
        df[['BB_UPPER', 'BB_MID', 'BB_LOWER']] = 100
        result = analyze_bollinger(df)
        self.assertIsNone(result['percent_b'])
        self.assertTrue(result['squeeze'])

    def test_technical_payload_and_rule_text_include_observations(self):
        df = frame()
        core = Mock()
        core.get_kd_signals.return_value = ''
        core.get_macd_signals.return_value = ''
        core.get_ma_kline_signals.return_value = ''
        with patch.object(tools, 'core', return_value=core), patch.object(tools, '_stock_identity', return_value=('1234', '測試')), patch.object(tools, '_load_price_bundle', return_value={'df': df}):
            result = tools.get_technical_analysis('1234')
        self.assertEqual(result['bollinger'], analyze_bollinger(df))
        text = bot.format_technical(result)
        self.assertIn('【布林觀察】', text)
        self.assertIn('符合橫盤整理條件', text)

    def test_bollinger_questions_route_to_technical_tool(self):
        parser = bot.QuestionParser()
        router = bot.QueryRouter(Mock(), bot.BotConfig.from_env(), Mock())
        for text in ('2344布林有沒有突破', '2344壓縮了嗎', '2344橫盤嗎', '2344上軌在哪裡'):
            parsed = bot.ParsedQuestion(text, parser.detect_intents(text), stocks=[('2344', '測試')])
            plan = router.plan(parsed, bot.AnswerStats())
            self.assertIn('get_technical_analysis', [call.name for call in plan.tool_calls])


if __name__ == '__main__':
    unittest.main()
