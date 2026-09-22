"""全市場族群排行：完全用本地底庫計算，不為了單一問題打行情 API。

流程：
1. `market_data.sync()` 每天把全市場收盤寫進 SQLite（2 個請求／天）。
2. `score_pending()` 在背景把每一檔的型態分數算好（純 CPU，沿用同一套 score_pattern）。
3. `rank_groups()` 用名冊 + 本地資料排族群，秒回，而且會誠實回報涵蓋率。
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import warrant_ai_tools as tools
import local_market_cache
import sector_roster
import weekly_pick

# 漲幅排行仍保留流動性與涵蓋率門檻；純型態排行改用完整族群名冊，
# 只排除真的沒有型態分數的股票，避免把「族群型態」變成「高流動性個股型態」。
MIN_MEMBERS = max(3, tools._env_int("DISCORD_AI_MARKET_MIN_MEMBERS", 5))
MIN_COVERAGE = min(1.0, max(0.2, tools._env_float("DISCORD_AI_MARKET_MIN_COVERAGE", 0.6)))
TECH_MIN_VALID = max(1, tools._env_int("DISCORD_AI_MARKET_TECH_MIN_VALID", 3))
LIQUIDITY_DAYS = max(5, tools._env_int("DISCORD_AI_SECTOR_LIQUIDITY_DAYS", 20))
MIN_AVG_VALUE = max(0.0, tools._env_float("DISCORD_AI_SECTOR_MIN_AVG_VALUE", 50_000_000.0))
MIN_AVG_LOTS = max(0.0, tools._env_float("DISCORD_AI_SECTOR_MIN_AVG_LOTS", 500.0))
SCORE_BUDGET = max(30.0, tools._env_float("DISCORD_AI_MARKET_SCORE_BUDGET", 480.0))
_SCORE_LOCK = threading.Lock()

# 全市場型態排行 v2：只比大族群（細產業／概念 ≥20 檔），綜合分數＝70% 中位＋30% 高分股占比，
# 再去掉高度重疊的族群（重疊率＝交集 ÷ 較小族群檔數 > 70% 保留分數高者）。
TECH_BIG_MIN = max(1, tools._env_int("DISCORD_AI_MARKET_TECH_BIG_MIN", 20))
TECH_MIN_COVERAGE = min(1.0, max(0.0, tools._env_float("DISCORD_AI_MARKET_TECH_MIN_COVERAGE", 0.8)))
TECH_STRONG_SCORE = 75
TECH_MEDIAN_WEIGHT = 0.7
TECH_OVERLAP_MAX = min(1.0, max(0.1, tools._env_float("DISCORD_AI_MARKET_TECH_OVERLAP", 0.7)))
TECH_TOP_STOCKS = 5


def _liquid_codes() -> set:
    """近 N 日平均成交金額／成交量達標的股票（一次 SQL，不打 API）。"""
    stats = local_market_cache.liquidity_map(LIQUIDITY_DAYS)
    return {code for code, row in stats.items()
            if (row.get("avg_value") or 0) >= MIN_AVG_VALUE and (row.get("avg_lots") or 0) >= MIN_AVG_LOTS}


def _member_codes() -> Dict[str, List[str]]:
    groups = sector_roster.catalog()
    out: Dict[str, List[str]] = {}
    for code in groups:
        try:
            out[code] = [s["stock_code"] for s in sector_roster.get_members(code)["stocks"]]
        except Exception:
            continue
    return out


def _dedupe_overlap(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """依綜合分數由高到低，和已保留族群重疊率 > 70% 的就去掉（保留分數較高者）；去重名單只寫 log。"""
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for row in rows:
        codes = row.pop("_codes", set())
        clash = next((k for k in kept
                      if codes and k["_member_set"]
                      and len(codes & k["_member_set"]) / min(len(codes), len(k["_member_set"])) > TECH_OVERLAP_MAX), None)
        if clash:
            dropped.append(f"{row['name']}→{clash['name']}")
            continue
        row["_member_set"] = codes
        kept.append(row)
    for row in kept:
        row.pop("_member_set", None)
    if dropped:
        print(f"📚 型態排行去重（重疊率 >{TECH_OVERLAP_MAX:.0%}）：{'、'.join(dropped[:30])}"
              + (f"…共 {len(dropped)} 個" if len(dropped) > 30 else ""), flush=True)
    return kept


def rank_groups(mode: str, limit: int = 10) -> Dict[str, Any]:
    """mode＝market_momentum（中位漲幅）或 market_technical（中位型態分數）。"""
    catalog = sector_roster.catalog()
    if not catalog:
        return {"mode": mode, "rows": [], "reason": "roster_missing", "groups_total": 0, "groups_ranked": 0}
    members = _member_codes()
    universe = sorted({code for codes in members.values() for code in codes})
    liquid = _liquid_codes()
    if mode == "market_technical":
        values = local_market_cache.pattern_scores_for(universe)
        pick: Callable[[Dict[str, Any]], Optional[float]] = lambda row: row.get("score")
    else:
        values = local_market_cache.latest_changes(universe)
        pick = lambda row: row.get("change_pct")
    names = sector_roster._name_map()
    rows: List[Dict[str, Any]] = []
    for group_code, info in catalog.items():
        all_codes = list(dict.fromkeys(members.get(group_code, [])))
        if not all_codes:
            continue

        if mode == "market_technical":
            # 純型態排行：完整族群名冊都參加，不先套成交量／成交金額門檻。
            # v2：只比 ≥20 檔的大族群，且型態有效檔數要達 80%，小族群不會因 3 檔全高分衝到第一。
            codes = all_codes
            if len(codes) < TECH_BIG_MIN:
                continue
            pairs = [(c, pick(values[c])) for c in codes if c in values and pick(values[c]) is not None]
            required = max(min(TECH_MIN_VALID, len(codes)), math.ceil(len(codes) * TECH_MIN_COVERAGE))
            if len(pairs) < required:
                continue
        else:
            # 漲幅／強勢排行保留原本流動性規則，避免冷門股對短線強勢排名造成過度影響。
            codes = [c for c in all_codes if c in liquid]
            if len(codes) < MIN_MEMBERS:
                continue
            pairs = [(c, pick(values[c])) for c in codes if c in values and pick(values[c]) is not None]
            if len(pairs) < MIN_MEMBERS or len(pairs) / len(codes) < MIN_COVERAGE:
                continue

        numbers = [v for _, v in pairs]
        leader_code, leader_value = max(pairs, key=lambda x: x[1])
        row = {
            "group_code": group_code, "name": info["name"], "kind": info["kind"],
            "median": round(statistics.median(numbers), 2),
            "coverage": len(pairs), "members": len(codes), "total_members": len(all_codes),
            "strong_ratio": round(sum(1 for v in numbers if v >= 75) / len(numbers) * 100, 0) if mode == "market_technical"
            else round(sum(1 for v in numbers if v > 0) / len(numbers) * 100, 0),
            "leader_code": leader_code, "leader_name": names.get(leader_code, ""),
            "leader_value": round(leader_value, 2),
        }
        if mode == "market_technical":
            strong_count = sum(1 for v in numbers if v >= TECH_STRONG_SCORE)
            strong_pct = strong_count / len(numbers) * 100
            top = sorted(pairs, key=lambda x: -x[1])[:TECH_TOP_STOCKS]
            row.update({
                "strong_count": strong_count,
                "composite": round(TECH_MEDIAN_WEIGHT * row["median"] + (1 - TECH_MEDIAN_WEIGHT) * strong_pct, 1),
                "top_stocks": [{"code": c, "name": names.get(c, c), "score": round(v, 1)} for c, v in top],
                "_codes": set(all_codes),
            })
        rows.append(row)
    if mode == "market_technical":
        rows.sort(key=lambda r: (-r["composite"], -r["median"], r["name"]))
        rows = _dedupe_overlap(rows)
    else:
        rows.sort(key=lambda r: (-r["median"], r["name"]))
    for index, row in enumerate(rows[:limit], 1):
        row["rank"] = index
    dates = local_market_cache.known_dates(limit=1)
    return {"mode": mode, "rows": rows[:limit], "groups_total": len(catalog), "groups_ranked": len(rows),
            "as_of": dates[0] if dates else "", "scored_stocks": len(values),
            "liquidity_rule": ("" if mode == "market_technical" else
                               f"近 {LIQUIDITY_DAYS} 日平均成交金額 {MIN_AVG_VALUE/1e4:,.0f} 萬元、平均成交量 {MIN_AVG_LOTS:,.0f} 張以上"),
            "reason": "" if rows else ("no_scores" if mode == "market_technical" and not values else "low_coverage")}


def score_pending(budget_seconds: float = SCORE_BUDGET, log: Callable[[str], None] = print,
                  codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """補算分數日期與個股最後收盤 K 棒不一致的股票，可分多輪執行。"""
    if not _SCORE_LOCK.acquire(blocking=False):
        return {"skipped": "already_running"}
    started = time.monotonic()
    done = failed = 0
    try:
        last_bars = local_market_cache.last_bar_dates()
        if not last_bars:
            return {"done": 0, "failed": 0, "pending": 0, "reason": "no_bars"}
        latest = max(last_bars.values())  # 僅供狀態顯示，不參與個股是否重算的判斷。
        universe = codes or sorted({c for codes_ in _member_codes().values() for c in codes_}) or \
            local_market_cache.codes_with_history(69)
        scored = local_market_cache.latest_pattern_score_dates(universe)
        todo = [c for c in universe
                if not last_bars.get(c) or scored.get(c) != last_bars[c]]
        for code in todo:
            if time.monotonic() - started > budget_seconds:
                break
            try:
                # 背景優先權：不搶使用者的即時行情額度，也不接盤中報價（分數只用收盤 K 棒）。
                with tools.api_priority("background"):
                    tech = tools.get_technical_analysis(code)
                    vp = tools.get_volume_profile(code)
                    extras = weekly_pick._technical_extras(code)
                score = weekly_pick.score_pattern(tech, vp, extras, weekly_pick.WeeklyPickConfig())
                if not math.isfinite(float(score["score"])):
                    raise ValueError("score not finite")
                score_date = tech.get("data_date") or last_bars.get(code)
                if not score_date:
                    raise ValueError("missing stock bar date")
                local_market_cache.save_pattern_score(
                    code, str(score_date).replace("/", "-"), float(score["score"]),
                    weekly_pick.pattern_grade(score["score"]), score.get("components"),
                    str(tech.get("signal_status") or ""))
                done += 1
            except Exception:
                failed += 1
        pending = max(0, len(todo) - done - failed)
        if done or failed:
            log(f"📈 型態分數底庫：新增 {done} 檔｜失敗 {failed}｜尚待 {pending}｜{time.monotonic()-started:.0f} 秒")
        local_market_cache.set_state("score_scan", {
            "at": tools.taipei_now().strftime("%Y-%m-%d %H:%M"), "latest": latest,
            "done": done, "failed": failed, "pending": pending})
        return {"done": done, "failed": failed, "pending": pending, "latest": latest,
                "elapsed": time.monotonic() - started}
    finally:
        _SCORE_LOCK.release()


def status() -> Dict[str, Any]:
    stats = local_market_cache.stats()
    return {
        "roster_built_at": sector_roster.built_at(), "roster_groups": len(sector_roster.catalog()),
        "bars_days": stats.get("days", 0), "bars_stocks": stats.get("stocks", 0),
        "scored_stocks": stats.get("scores", 0), "last_day": stats.get("last_day", ""),
        "persistent": stats.get("persistent", False),
        "market_sync": local_market_cache.get_state("market_sync", {}) or {},
        "score_scan": local_market_cache.get_state("score_scan", {}) or {},
    }
