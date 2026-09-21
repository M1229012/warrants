"""全市場底庫、族群名冊與全市場排行的離線測試（不連網、不用金鑰）。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.mkdtemp(prefix="ace-test-")
os.environ.setdefault("DISCORD_AI_MARKET_CACHE_DB", os.path.join(TEMP, "market.sqlite3"))
os.environ.setdefault("DISCORD_AI_SECTOR_ROSTER", os.path.join(TEMP, "roster.json"))

import local_market_cache          # noqa: E402
import market_data                 # noqa: E402
import market_scan                 # noqa: E402
import sector_roster               # noqa: E402

# 其他測試可能已經先 import 過 sector_roster，環境變數就來不及生效；直接指定模組內的路徑。
ROSTER_FILE = Path(os.environ["DISCORD_AI_SECTOR_ROSTER"])
sector_roster.ROSTER_PATH = ROSTER_FILE
sector_roster.REPO_PATH = ROSTER_FILE
sector_roster.DATA_PATH = ROSTER_FILE

TWSE_PAYLOAD = {
    "stat": "OK",
    "tables": [{
        "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
        "data": [
            ["2330", "台積電", "40,892,688", "1", "1", "2,430.00", "2,465.00", "2,420.00", "2,460.00"],
            ["0050", "元大台灣50", "1,000", "1", "1", "50.00", "51.00", "49.00", "50.50"],   # ETF 不收
            ["2344", "華邦電", "120,000,000", "1", "1", "170.00", "180.00", "169.00", "179.50"],
            ["9999", "壞資料", "--", "1", "1", "--", "--", "--", "--"],
        ],
    }],
}
TPEX_PAYLOAD = {
    "tables": [{
        "fields": ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  "],
        "data": [["6531", "愛普*", "968.00", "+48.00", "930.00", "970.00", "925.00", "3,000,000"]],
    }],
}


class MarketDataTests(unittest.TestCase):
    def test_parses_official_payloads_and_skips_etf(self):
        rows = market_data._rows_from_twse(TWSE_PAYLOAD)
        self.assertEqual([r["stock_code"] for r in rows], ["2330", "2344"])
        self.assertEqual(rows[0]["close"], 2460.0)
        self.assertEqual(rows[0]["volume"], 40892688.0)
        self.assertEqual(rows[0]["market"], "twse")
        otc = market_data._rows_from_tpex(TPEX_PAYLOAD)
        self.assertEqual(otc[0]["stock_code"], "6531")
        self.assertEqual((otc[0]["close"], otc[0]["market"]), (968.0, "tpex"))

    def test_holiday_returns_nothing(self):
        self.assertEqual(market_data._rows_from_twse({"stat": "很抱歉，沒有符合條件的資料!"}), [])


class LocalCacheTests(unittest.TestCase):
    def setUp(self):
        with local_market_cache._db() as conn:
            conn.execute("DELETE FROM daily_bars")
            conn.execute("DELETE FROM pattern_scores")
            conn.commit()

    def _seed(self):
        for index, day in enumerate(["2026-09-16", "2026-09-17", "2026-09-18"]):
            local_market_cache.save_market_day([
                {"stock_code": "2330", "market": "twse", "open": 2400 + index, "high": 2500, "low": 2300,
                 "close": 2400 + index * 30, "volume": 40_000_000},
                {"stock_code": "1101", "market": "twse", "open": 30, "high": 31, "low": 29,
                 "close": 30 + index * 0.1, "volume": 100_000},
            ], day, source="test")

    def test_bulk_save_changes_and_liquidity(self):
        self._seed()
        self.assertEqual(local_market_cache.known_dates()[0], "2026-09-18")
        changes = local_market_cache.latest_changes(["2330", "1101"])
        self.assertAlmostEqual(changes["2330"]["close"], 2460.0)
        self.assertAlmostEqual(changes["2330"]["change_pct"], (2460 / 2430 - 1) * 100, places=6)
        liquidity = local_market_cache.liquidity_map(20)
        self.assertAlmostEqual(liquidity["2330"]["avg_lots"], 40000.0)
        self.assertGreater(liquidity["2330"]["avg_value"], 9e10)
        self.assertLess(liquidity["1101"]["avg_value"], 5e7)      # 冷門股不會通過門檻

    def test_state_roundtrip(self):
        local_market_cache.set_state("unit-test", {"a": 1})
        self.assertEqual(local_market_cache.get_state("unit-test"), {"a": 1})
        local_market_cache.delete_state("unit-test")
        self.assertIsNone(local_market_cache.get_state("unit-test"))


ROSTER = {
    "built_at": "2026-09-20 12:00",
    "groups": {
        "C23020": {"name": "半導體", "kind": "industry", "stocks": ["2330", "2344", "2303", "6531", "3006"]},
        "C50877": {"name": "資產股", "kind": "concept", "stocks": ["1101", "1103", "1110"]},
    },
}


class RosterTests(unittest.TestCase):
    def setUp(self):
        ROSTER_FILE.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
        sector_roster.reload()

    def test_match_and_members(self):
        self.assertEqual(sector_roster.match_group("半導體族群誰型態最好")["code"], "C23020")
        self.assertEqual(sector_roster.match_group("資產股")["code"], "C50877")
        self.assertIsNone(sector_roster.match_group("完全不存在的族群"))
        with patch.object(sector_roster, "_name_map", return_value={"2330": "台積電", "2344": "華邦電"}):
            data = sector_roster.get_members("C23020")
        self.assertEqual(len(data["stocks"]), 5)
        self.assertTrue(data["complete"])            # 名冊是掃全市場建的，不會只有首屏 8 檔
        self.assertEqual(data["stocks"][0]["stock_code"], "2303")

    def test_short_names_and_question_sentences(self):
        # 「散熱」是簡稱、「…族群誰最強」是問句，兩種都要找得到族群
        roster = {"built_at": "2026-09-20", "groups": {
            "C30024": {"name": "散熱零組件", "kind": "industry", "stocks": ["2421", "3017", "3338"]},
            "C50001": {"name": "散熱模組", "kind": "concept", "stocks": ["2421", "3017"]},   # 奇鋐只在這一類
            "C50913": {"name": "記憶體", "kind": "concept", "stocks": ["2344", "2408", "8299"]}}}
        ROSTER_FILE.write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
        sector_roster.reload()
        merged = sector_roster.match_group("散熱")                 # 簡稱 → 合併兩個散熱族群
        self.assertEqual(merged["code"], "multi:C30024,C50001")
        self.assertEqual(sector_roster.match_group("散熱族群誰最強")["code"], "multi:C30024,C50001")
        self.assertEqual(sector_roster.match_group("散熱模組誰型態最好")["name"], "散熱模組")  # 完整名稱優先
        self.assertEqual(sector_roster.match_group("請問記憶體產業 現在誰的形態最強")["name"], "記憶體")
        with patch.object(sector_roster, "_name_map", return_value={}):
            members = sector_roster.get_members(merged["code"], display_name="散熱")
        self.assertEqual(members["name"], "散熱")
        self.assertEqual([s["stock_code"] for s in members["stocks"]], ["2421", "3017", "3338"])

    def test_missing_group(self):
        with self.assertRaises(Exception):
            sector_roster.get_members("C99999")


class MarketRankTests(unittest.TestCase):
    def setUp(self):
        ROSTER_FILE.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
        sector_roster.reload()
        with local_market_cache._db() as conn:
            conn.execute("DELETE FROM daily_bars")
            conn.execute("DELETE FROM pattern_scores")
            conn.commit()

    def test_ranking_needs_enough_liquid_members(self):
        # 只有 3 檔有量：低於 MIN_MEMBERS=5，不應該排出「半導體」
        for day in ("2026-09-17", "2026-09-18"):
            local_market_cache.save_market_day(
                [{"stock_code": code, "market": "twse", "open": 100, "high": 100, "low": 100,
                  "close": 100, "volume": 5_000_000} for code in ("2330", "2344", "2303")], day)
        data = market_scan.rank_groups("market_momentum")
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["reason"], "low_coverage")

    def test_ranking_uses_median_and_coverage(self):
        codes = ["2330", "2344", "2303", "6531", "3006"]
        for index, day in enumerate(("2026-09-17", "2026-09-18")):
            local_market_cache.save_market_day(
                [{"stock_code": code, "market": "twse", "open": 100, "high": 110, "low": 90,
                  "close": 100 + index * (i + 1), "volume": 5_000_000} for i, code in enumerate(codes)], day)
        data = market_scan.rank_groups("market_momentum")
        self.assertEqual([r["name"] for r in data["rows"]], ["半導體"])
        row = data["rows"][0]
        self.assertEqual((row["coverage"], row["members"]), (5, 5))
        self.assertAlmostEqual(row["median"], 3.0)          # 漲幅 1,2,3,4,5% 的中位數
        self.assertEqual(row["strong_ratio"], 100)
        self.assertEqual(row["leader_code"], "3006")
        for code in codes:
            local_market_cache.save_pattern_score(code, "2026-09-18", 60 + codes.index(code) * 5, "中性偏多", [], "收盤確認")
        # 測試資料只有 5 檔：放寬「≥20 檔、型態有效 80%」門檻，只驗中位數與綜合分數算法
        with patch.object(market_scan, "TECH_BIG_MIN", 1), patch.object(market_scan, "TECH_MIN_COVERAGE", 0.0):
            technical = market_scan.rank_groups("market_technical")
        self.assertAlmostEqual(technical["rows"][0]["median"], 70.0)
        top = technical["rows"][0]
        # 分數 60/65/70/75/80 → 75 分以上 2/5＝40% → 綜合 0.7×70 + 0.3×40 = 61.0
        self.assertEqual(top["strong_count"], 2)
        self.assertAlmostEqual(top["composite"], 61.0)
        self.assertEqual([s["score"] for s in top["top_stocks"]][:2], [80.0, 75.0])

    def test_technical_ranking_skips_small_groups(self):
        # 預設門檻（≥20 檔、型態有效 80%）：只有 5 檔有分數的族群不能進型態排行
        codes = ["2330", "2344", "2303", "6531", "3006"]
        for code in codes:
            local_market_cache.save_pattern_score(code, "2026-09-18", 90, "強勢", [], "收盤確認")
        for day in ("2026-09-17", "2026-09-18"):
            local_market_cache.save_market_day(
                [{"stock_code": code, "market": "twse", "open": 100, "high": 100, "low": 100,
                  "close": 100, "volume": 5_000_000} for code in codes], day)
        names = [r["name"] for r in market_scan.rank_groups("market_technical")["rows"]]
        self.assertNotIn("半導體", names)

    def test_no_roster_reports_reason(self):
        # 名冊完全沒有時要講清楚原因，不能給看起來像全市場的排行
        with patch.object(sector_roster, "catalog", return_value={}):
            self.assertEqual(market_scan.rank_groups("market_technical")["reason"], "roster_missing")

    def test_empty_override_falls_back_to_bundled_roster(self):
        # 指定的名冊檔壞掉／是空的時，退回程式附帶的名冊，不是直接不能用
        bundled = Path(__file__).with_name("sector_roster.json")
        if not bundled.exists():
            self.skipTest("沒有附帶名冊可測")
        ROSTER_FILE.write_text(json.dumps({"groups": {}}), encoding="utf-8")
        with patch.object(sector_roster, "REPO_PATH", bundled):
            sector_roster.reload()
            self.assertGreater(len(sector_roster.catalog()), 0)
        sector_roster.reload()


if __name__ == "__main__":
    unittest.main()
