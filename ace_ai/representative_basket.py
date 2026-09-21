"""代表股分層抽樣（族群雷達規格第 3 章，第 2 步定案版）。

- 只負責「拿 member_codes 挑代表股」；成員名單由 OfficialSectorMemberResolver 提供。
- 全部用本地底庫（20 日流動性、日 K），0 API。
- 名單每月重算一次；mapping_version 變了也重算（index／sample universe 必須同版本）。
- purpose 介面先保留；這一階段只有 sector_radar，盤中量能曲線之後再搬過來。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

import pandas as pd

import warrant_ai_tools as tools
import local_market_cache

MIN_BARS = 21                   # 20 個日報酬
MIN_AVG_LOTS = 500.0            # 張
MIN_AVG_VALUE = 50_000_000.0    # 元
MIN_USABLE = 6
# 合格檔數 → 高／中／一般 三層各取幾檔（固定表，結果可重現）
QUOTAS = {10: (4, 3, 3), 9: (3, 3, 3), 8: (3, 3, 2), 7: (3, 2, 2), 6: (2, 2, 2)}
LAYER_NAMES = ("高", "中", "一般")
PURPOSES = ("sector_radar",)    # 預留 volume_curve，遷移前不開放
_STATE_PREFIX = "rep_basket:"


def _split_three(n: int) -> List[int]:
    """族群內依成交金額平分三等分；除不盡時多出來的給較高層。"""
    return [n // 3 + (1 if i < n % 3 else 0) for i in range(3)]


class RepresentativeBasketManager:
    def __init__(self, market: str = "twse", purpose: str = "sector_radar", size: int = 10):
        if purpose not in PURPOSES:
            raise ValueError(f"purpose={purpose} 尚未遷移到 RepresentativeBasketManager")
        if size != 10:
            raise ValueError("目前只定案 10 檔（4/3/3）的配額表")
        self.market, self.purpose, self.size = market, purpose, size

    def _key(self, universe_id: str) -> str:
        return f"{_STATE_PREFIX}{self.purpose}:{self.market}:{universe_id}"

    def get(self, universe_id: str, member_codes: List[str], mapping_version: str,
            refresh: bool = False) -> Dict[str, Any]:
        """回傳 {usable, codes, layers, ...}；同月份＋同 mapping 版本就沿用，不每天變動。"""
        month = tools.taipei_now().strftime("%Y-%m")
        key = self._key(universe_id)
        stored = local_market_cache.get_state(key, {}) or {}
        if (not refresh and stored.get("month") == month
                and stored.get("mapping_version") == mapping_version and "usable" in stored):
            return dict(stored)
        built = self._build(member_codes)
        built.update({"universe_id": universe_id, "market": self.market, "purpose": self.purpose,
                      "month": month, "mapping_version": mapping_version, "built_at": time.time()})
        if built["eligible"] == 0 and stored.get("mapping_version") == mapping_version:
            # 本地底庫暫時沒資料（剛部署、還沒回補），不要用空結果蓋掉既有名單
            print(f"⚠️ 代表股重算失敗，沿用舊名單｜{universe_id}", flush=True)
            return dict(stored, stale=True)
        local_market_cache.set_state(key, built)
        print(f"🧺 代表股名單｜{self.purpose}｜{universe_id}｜合格 {built['eligible']}"
              f"／成員 {built['member_count']}｜取 {len(built['codes'])} 檔"
              f"｜{'可用' if built['usable'] else '不足，降級'}", flush=True)
        return built

    # ------------------------------------------------------------
    def _eligible(self, member_codes: List[str]) -> List[Dict[str, Any]]:
        dates = [pd.Timestamp(d).normalize() for d in local_market_cache.known_dates(limit=MIN_BARS)]
        if len(dates) < MIN_BARS:
            return []
        wanted = set(dates)
        liquidity = local_market_cache.liquidity_map(20)
        rows = []
        for code in sorted({str(c).strip() for c in member_codes}):
            if not re.fullmatch(r"[1-9]\d{3}", code):
                continue
            info = liquidity.get(code) or {}
            if float(info.get("avg_lots") or 0) < MIN_AVG_LOTS or float(info.get("avg_value") or 0) < MIN_AVG_VALUE:
                continue
            bars = local_market_cache.load_bars(code, limit=MIN_BARS)
            if not bars or bars.get("count", 0) < MIN_BARS:
                continue
            if bars.get("market") and str(bars["market"]) != self.market:
                continue                                  # 市場別不一致就不收，universe 要嚴格一致
            frame = bars["df"]
            if set(pd.DatetimeIndex(frame.index).normalize()) != wanted or frame["Close"].isna().any():
                continue                                  # 資料完整：最近 21 個交易日一天都不缺
            returns = frame["Close"].pct_change().dropna() * 100
            returns.index = pd.DatetimeIndex(returns.index).normalize()
            rows.append({"code": code, "avg_value": float(info["avg_value"]),
                         "avg_lots": float(info["avg_lots"]), "returns": returns})
        return rows

    def _build(self, member_codes: List[str]) -> Dict[str, Any]:
        pool = self._eligible(member_codes)
        base = {"member_count": len(set(member_codes)), "eligible": len(pool),
                "disposition_checked": False,             # 處置股名單尚未接入（規格全域規則 3）
                "criteria": {"min_avg_lots": MIN_AVG_LOTS, "min_avg_value": MIN_AVG_VALUE,
                             "min_bars": MIN_BARS, "pick": "layer_median_return_mad"}}
        if len(pool) < MIN_USABLE:
            return dict(base, usable=False, codes=[], layers=[],
                        reason=f"合格代表股不足 {MIN_USABLE} 檔（{len(pool)} 檔）")
        pool.sort(key=lambda r: (-r["avg_value"], r["code"]))
        quotas = QUOTAS[min(len(pool), self.size)]
        picked: List[Dict[str, Any]] = []
        start = 0
        for layer, (count, quota) in enumerate(zip(_split_three(len(pool)), quotas)):
            members = pool[start:start + count]
            start += count
            # 代表性誤差：個股 20 日報酬 與 該層每日報酬中位數 的 median absolute difference
            layer_median = pd.concat([r["returns"] for r in members], axis=1).median(axis=1)
            for row in members:
                row["rep_error"] = float((row["returns"] - layer_median).abs().median())
            members.sort(key=lambda r: (r["rep_error"], -r["avg_value"], r["code"]))
            picked += [{"code": r["code"], "layer": LAYER_NAMES[layer], "avg_value": round(r["avg_value"]),
                        "avg_lots": round(r["avg_lots"], 1), "rep_error": round(r["rep_error"], 4)}
                       for r in members[:quota]]
        return dict(base, usable=True, codes=[p["code"] for p in picked], layers=picked, reason="")
