"""族群功能的離線回歸測試：不使用 API Key、不呼叫 Gemini。"""
import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import discord_ai_bot as bot
import sector_analysis as sector
import warrant_ai_tools as tools


def stock(code="2330", name="台積電"):
    return {"stock_code": code, "stock_name": name, "market": "twse"}


def members(stocks=None):
    return {"industry": "24", "name": "半導體業", "stocks": stocks or [stock()],
            "source": "FinMind", "updated_at": "2026-09-18", "complete": True, "missing_markets": []}


def quote(code="2330", change=1.0, date="2026-09-18", minute="10:30"):
    return {**stock(code), "close": 100.0, "change_pct": change, "quote_date": date,
            "intraday": {"date": date, "time": minute, "is_live": True}}


class SectorTests(unittest.TestCase):
    def setUp(self):
        self.cache_patch = patch.object(sector, "CACHE", tools.TTLCache("test_sector"))
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.now_patch = patch.object(tools, "taipei_now", return_value=datetime(2026, 9, 18, 10, 35, tzinfo=tools.TAIPEI_TZ))
        self.now_patch.start()
        self.addCleanup(self.now_patch.stop)

    def test_routes_and_fine_groups_without_loading_core(self):
        cases = {"半導體族群哪支股票現在比較好": ("24", "technical"),
                 "航運股今天誰漲最多": ("15", "momentum"),
                 "金融股有哪些": ("17", "members"),
                 "半導體族群有哪些比較好的股票": ("24", "technical"),
                 "半導體族群盤中型態哪檔好": ("24", "technical"),
                 "有哪些族群": ("", "catalog"),
                 "記憶體族群哪檔好": ("fine:memory", "technical"),
                 "半導體記憶體族群哪檔好": ("fine:memory", "technical"),
                 "PCB族群": ("fine:pcb", "technical"),
                 "金融族群有哪些分點買超": ("17", "unsupported")}
        with patch.object(tools, "core", side_effect=AssertionError("must not load core")):
            for question, expected in cases.items():
                parsed = bot.QuestionParser().parse(question)
                self.assertEqual((parsed.sector["industry"], parsed.sector["mode"]), expected, question)
                router = bot.QueryRouter(Mock(), bot.BotConfig.from_env(), Mock())
                self.assertEqual(router.plan(parsed, bot.AnswerStats()).route, "rule_sector")

    def test_existing_questions_not_intercepted(self):
        for question in ("2344現在型態好嗎", "台積電現在技術面怎麼樣", "永豐金內湖勝率多少",
                         "那它的壓力在哪", "跟旺宏比呢", "2330是半導體嗎", "本週精選"):
            self.assertIsNone(sector.detect_request(question), question)

    def test_multiple_industries_request_clarification(self):
        self.assertEqual(sector.detect_request("半導體和航運族群比較")["mode"], "unsupported")
        self.assertEqual(sector.detect_request("其他電子業哪檔好")["industry"], "31")

    def test_memory_does_not_inject_previous_stock(self):
        memory = bot.ConversationMemory()
        memory.update("g:c:u", bot.ParsedQuestion("台積電", set(), stocks=[("2330", "台積電")], cost_price=100))
        parsed = bot.QuestionParser().parse("航運族群哪支比較好")
        self.assertEqual(memory.resolve("g:c:u", parsed), "")
        self.assertEqual(parsed.stocks, [])
        memory.update("g:c:u", parsed)
        self.assertIsNone(memory.get("g:c:u"))

    def test_finmind_primary_latest_row_before_filter_and_dedup(self):
        frame = pd.DataFrame([
            ["2330", "台積電", "半導體業", "twse", "2026-09-18"],
            ["1111", "已轉板", "半導體業", "emerging", "2025-01-01"],
            ["1111", "已轉板", "半導體業", "tpex", "2026-09-18"],
            ["2222", "已轉分類", "半導體業", "twse", "2025-01-01"],
            ["2222", "已轉分類", "光電業", "twse", "2026-09-18"],
            ["3333", "興櫃", "半導體業", "emerging", "2026-09-18"],
            ["0050", "ETF", "半導體業", "twse", "2026-09-18"],
        ], columns=["stock_id", "stock_name", "industry_category", "type", "date"])
        core = SimpleNamespace(_finmind_load_stock_info=Mock(return_value=frame))
        with patch.object(tools, "core", return_value=core), patch.object(tools, "_fugle_get") as fallback:
            result = sector.get_members("24")
            self.assertEqual([s["stock_code"] for s in result["stocks"]], ["1111", "2330"])
            self.assertEqual(result["source"], "FinMind")
            sector.get_members("24")
            core._finmind_load_stock_info.assert_called_once()
            fallback.assert_not_called()

    def test_fugle_fallback_uses_both_markets_and_filters_etfs(self):
        responses = [{"date": "2026-09-18", "data": [{"symbol": "2330", "name": "台積電"}, {"symbol": "0050"}]},
                     {"date": "2026-09-18", "data": [{"symbol": "5347", "name": "世界"}]}]
        with patch.object(sector, "_finmind_catalog", side_effect=RuntimeError()), patch.object(tools, "FUGLE_API_KEY", "test"), \
                patch.object(tools, "_fugle_get", side_effect=responses) as fallback:
            result = sector.get_members("24")
            self.assertEqual(result["source"], "Fugle")
            self.assertTrue(result["complete"])
            self.assertEqual([s["stock_code"] for s in result["stocks"]], ["2330", "5347"])
            self.assertEqual([c.args[1]["market"] for c in fallback.call_args_list], ["TSE", "OTC"])
            self.assertTrue(all(c.args[1]["industry"] == "24" for c in fallback.call_args_list))

    def test_fallback_partial_roster_is_explicit(self):
        with patch.object(sector, "_finmind_catalog", side_effect=RuntimeError()), patch.object(tools, "FUGLE_API_KEY", "test"), \
                patch.object(tools, "_fugle_get", side_effect=[{"data": [{"symbol": "2330"}]}, RuntimeError()]):
            data = sector.get_members("24")
            self.assertFalse(data["complete"])
            self.assertEqual(data["missing_markets"], ["OTC"])

    def test_no_providers_returns_honest_failure_no_llm(self):
        gateway = Mock()
        with patch.object(sector, "_finmind_catalog", side_effect=RuntimeError()), patch.object(tools, "FUGLE_API_KEY", ""):
            result = sector.answer(sector.detect_request("半導體族群誰最好"), gateway, Mock())
        self.assertFalse(result["cacheable"])
        self.assertIn("暫時無法取得", result["text"])
        gateway.generate.assert_not_called()

    def test_momentum_excludes_yesterday_and_old_intraday(self):
        rows = [quote("2330", 1), quote("2344", 9, "2026-09-17"), quote("2303", 8, minute="09:00")]
        with patch.object(tools, "intraday_session_now", return_value=True):
            valid, excluded, date = sector._eligible(rows, "momentum")
        self.assertEqual([r["stock_code"] for r in valid], ["2330"])
        self.assertEqual(excluded, 2)
        self.assertEqual(date, "2026-09-18")

    def test_technical_uses_same_closed_date(self):
        rows = [dict(quote("2330"), score_date="2026-09-17", pattern_score=60),
                dict(quote("2303"), score_date="2026-09-16", pattern_score=99)]
        valid, excluded, date = sector._eligible(rows, "technical")
        self.assertEqual(valid[0]["stock_code"], "2330")
        self.assertEqual((excluded, date), (1, "2026-09-17"))

    def test_slash_dates_from_existing_tools_are_not_excluded(self):
        # 既有工具的 data_date 是 YYYY/MM/DD；曾因直接和 YYYY-MM-DD 比字串而把整個族群排除。
        ma = {f"MA{n}": {"value": 100} for n in (5, 10, 20, 60)}
        overview = {"close": 100, "change_pct": 1, "data_date": "2026/09/18",
                    "intraday": {"date": "2026/09/18", "time": "10:30", "is_live": True}}
        with patch.object(sector, "_wait_turn"), patch.object(tools, "get_stock_overview", return_value=overview),                 patch.object(tools, "get_technical_analysis", return_value={"data_date": "2026/09/17", "moving_averages": ma}),                 patch.object(tools, "get_volume_profile", return_value={"maximum_volume_zone": {"price_low": 90}}),                 patch.object(sector.weekly_pick, "_technical_extras", return_value={}),                 patch.object(sector.weekly_pick, "score_pattern", return_value={"score": 75, "items": []}):
            row = sector._stock_row(stock(), "technical", time.monotonic() + 5, threading.Event())
        self.assertEqual((row["score_date"], row["quote_date"], row["intraday"]["date"]), ("2026-09-17", "2026-09-18", "2026-09-18"))
        valid, excluded, date = sector._eligible([row, dict(row, stock_code="2303", score_date="2026/09/17")], "technical")
        self.assertEqual((len(valid), excluded, date), (2, 0, "2026-09-17"))
        with patch.object(tools, "intraday_session_now", return_value=True):
            valid, excluded, _ = sector._eligible([dict(quote("2330"), quote_date="2026/09/18",
                                                         intraday={"date": "2026/09/18", "time": "10:30", "is_live": True})], "momentum")
        self.assertEqual((len(valid), excluded), (1, 0))

    def test_ranking_sorts_numbers_and_reuses_cache(self):
        roster = members([stock("2330"), stock("2303"), stock("2344")])
        quotes = {"2330": quote("2330", -1), "2303": quote("2303", -3), "2344": quote("2344", -2)}
        with patch.object(sector, "get_members", return_value=roster), \
                patch.object(sector, "_stock_row", side_effect=lambda s, *args: quotes[s["stock_code"]]) as fetch, \
                patch.object(tools, "intraday_session_now", return_value=True):
            result = sector.get_ranking("24", "momentum")
            self.assertEqual([r["stock_code"] for r in result["rows"]], ["2330", "2344", "2303"])
            self.assertEqual(result["compared_count"], 3)
            self.assertEqual(sector.get_ranking("24", "momentum"), result)
            self.assertEqual(fetch.call_count, 3)
            self.assertIn("-1%", sector.format_ranking(result))

    def test_scan_failure_not_silently_claiming_entire_sector(self):
        def fetch(s, *args):
            if s["stock_code"] == "2303":
                raise RuntimeError("provider unavailable")
            return quote()
        with patch.object(sector, "get_members", return_value=members([stock(), stock("2303")])), \
                patch.object(sector, "_stock_row", side_effect=fetch), patch.object(tools, "intraday_session_now", return_value=True):
            data = sector.get_ranking("24", "momentum")
        self.assertEqual(data["failed_count"], 1)
        self.assertIn("不能視為整個族群前三名", sector.format_ranking(data))

    def test_timeout_does_not_wait_for_all_stocks(self):
        def slow(s, mode, deadline, cancel):
            cancel.wait(2)
            raise TimeoutError()
        started = time.monotonic()
        with patch.object(sector, "get_members", return_value=members([stock(), stock("2303"), stock("2344")])), \
                patch.object(sector, "_stock_row", side_effect=slow), patch.object(sector, "SCAN_TIMEOUT", 0.05):
            data = sector.get_ranking("24", "momentum")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(data["unprocessed_count"], 3)
        self.assertEqual(data["rows"], [])

    def test_reuses_existing_score_and_no_chips(self):
        ma = {f"MA{n}": {"value": 100} for n in (5, 10, 20, 60)}
        tech = {"data_date": "2026-09-17", "moving_averages": ma}
        overview = {"close": 100, "change_pct": 1, "data_date": "2026-09-18"}
        with patch.object(sector, "_wait_turn"), patch.object(tools, "get_stock_overview", return_value=overview), \
                patch.object(tools, "get_technical_analysis", return_value=tech), \
                patch.object(tools, "get_volume_profile", return_value={"maximum_volume_zone": {"price_low": 90}}), \
                patch.object(sector.weekly_pick, "_technical_extras", return_value={}), \
                patch.object(sector.weekly_pick, "score_pattern", return_value={"score": 75, "items": []}) as score, \
                patch.object(tools, "get_sheet_stock_chips") as chips:
            row = sector._stock_row(stock(), "technical", time.monotonic() + 5, threading.Event())
            self.assertEqual(row["pattern_score"], 75)
            self.assertEqual(row["score_date"], "2026-09-17")
            score.assert_called_once()
            chips.assert_not_called()

    def test_engine_end_to_end_ai_failure_keeps_ranking(self):
        engine = bot.AceQueryEngine(bot.BotConfig.from_env())
        self.addCleanup(engine.executor.shutdown, wait=True)
        engine.gateway.generate = Mock(return_value=SimpleNamespace(ok=False, text=""))
        with patch.object(sector, "get_members", return_value=members()), \
                patch.object(sector, "_stock_row", return_value=quote()), patch.object(tools, "intraday_session_now", return_value=True):
            result = engine.answer("半導體族群盤中誰最強")
        self.assertEqual(result.route, "rule_sector")
        self.assertEqual(result.gemini_calls, 1)
        self.assertIn("2330", result.text)
        self.assertIn("程式計算結果", result.text)
        # 會員看到的是族群卡片：只有排行資料，不含名冊來源等執行細節。
        self.assertEqual(len(result.panels), 1)
        card = result.panels[0]["sector"]
        self.assertEqual([r["stock_code"] for r in card["rows"]], ["2330"])
        self.assertNotIn("source", card)

    def test_ranking_card_renders_without_roster_details(self):
        import answer_image
        data = {"name": "半導體業", "mode": "technical", "comparison_date": "2026-09-17", "total_count": 3, "compared_count": 2,
                "members_complete": True, "rows": [dict(quote("2330"), rank=1, pattern_score=80.5, grade="結構偏強",
                                                         plus_reasons=["站上所有均線（均線排列 8/12）"], minus_reasons=[])],
                "others": [dict(quote("2303"), rank=4, pattern_score=40, grade="中性偏弱")]}
        panel = sector.ranking_panel(data, {"2330": "均線結構完整。"})
        self.assertEqual(panel["sector"]["coverage_note"], "1 檔暫無同日資料，未列入排行")
        self.assertEqual(panel["sector"]["rows"][0]["observation"], "均線結構完整。")
        image = answer_image.render_answer("半導體族群誰型態最好", "文字版", [panel])
        self.assertGreater(image.height, 600)
        members = sector.members_panel({"name": "PCB製造", "stocks": [stock("3037", "欣興"), dict(stock("5439", "高技"), market="tpex")],
                                        "complete": True, "updated_at": "2026-09-18"})
        self.assertEqual((len(members["sector_members"]["twse"]), len(members["sector_members"]["tpex"])), (1, 1))
        self.assertGreater(answer_image.render_answer("PCB族群有哪些", "文字版", [members]).height, 400)

    def test_engine_structured_ai_validates_each_stock(self):
        engine = bot.AceQueryEngine(bot.BotConfig.from_env())
        self.addCleanup(engine.executor.shutdown, wait=True)
        engine.gateway.generate = Mock(return_value=SimpleNamespace(ok=True, text='{"observations":[{"stock_code":"2330","text":"本次漲幅為 1%，仍需留意盤中變動。"},{"stock_code":"9999","text":"推薦買進。"}]}'))
        with patch.object(sector, "get_members", return_value=members()), \
                patch.object(sector, "_stock_row", return_value=quote()), patch.object(tools, "intraday_session_now", return_value=True):
            result = engine.answer("半導體族群盤中谁最強")
        self.assertIn("【AI 解讀】", result.text)
        self.assertNotIn("9999", result.text)
        self.assertEqual(engine.gateway.generate.call_count, 1)

    def test_wrong_ai_price_rejected_without_losing_ranking(self):
        engine = bot.AceQueryEngine(bot.BotConfig.from_env())
        self.addCleanup(engine.executor.shutdown, wait=True)
        engine.gateway.generate = Mock(return_value=SimpleNamespace(ok=True, text='{"observations":[{"stock_code":"2330","text":"目前股價 9999 元。"}]}'))
        with patch.object(sector, "get_members", return_value=members()), \
                patch.object(sector, "_stock_row", return_value=quote()), patch.object(tools, "intraday_session_now", return_value=True):
            result = engine.answer("半導體族群盤中誰最強")
        self.assertIn("報價 100 元", result.text)
        self.assertNotIn("9999", result.text)
        self.assertNotIn("【AI 解讀】", result.text)

    def test_ranking_renders_with_existing_image_pipeline(self):
        import io
        from PIL import Image
        import answer_image
        with patch.object(sector, "get_members", return_value=members()), \
                patch.object(sector, "_stock_row", return_value=quote()), patch.object(tools, "intraday_session_now", return_value=True):
            data = sector.get_ranking("24", "momentum")
        picture, extension = answer_image.make_attachment("半導體族群盤中誰最強", sector.format_ranking(data), [])
        self.assertIn(extension, ("png", "jpg", "jpeg"))
        self.assertGreater(Image.open(io.BytesIO(picture)).height, 500)


if __name__ == "__main__":
    unittest.main()
