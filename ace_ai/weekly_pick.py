"""艾斯 AI｜本週精選候選股（私人研究用）。

目的不是替使用者選股，而是每週從權證資料找出最適合撰寫 Discord「本週精選」週報的
3～5 檔候選，最後由使用者自己決定。

流程（效能由粗到細）：
    Stage 1  回測官方 A～E 事件表 → 最近 N 個交易日有事件的「分點 × 股票」
    Stage 2  分點 × 本次事件的歷史績效（Bayesian 修正勝率）＋ 事件買進金額 → 預排序，縮到 10～20 檔
    Stage 3  只對候選抓技術面、大量區、分點近期操作（含 MoneyDJ 近20日流水）
    Score    事件績效 25 ＋ 近期操作 10 ＋ 權證金額 25 ＋ 技術型態 25 ＋ 支撐 15 ＝ 100
    TOP5     由 Python 決定，Gemini 只負責解釋（正常 1 次呼叫）

新聞不列入分數；只有 Discord AI 已有新聞快取時才附上補充。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

import warrant_ai_tools as tools


# ============================================================
# 設定
# ============================================================

@dataclass
class WeeklyPickConfig:
    """本週精選所有門檻與權重參數集中在這裡，全部可用環境變數覆寫。"""

    event_window_trading_days: int = tools._env_int("WEEKLY_PICK_EVENT_WINDOW_DAYS", 5)
    min_event_win_rate: float = tools._env_float("WEEKLY_PICK_MIN_EVENT_WIN_RATE", 60.0)
    min_event_sample: int = tools._env_int("WEEKLY_PICK_MIN_EVENT_SAMPLE", 10)
    shortlist_size: int = tools._env_int("WEEKLY_PICK_SHORTLIST_SIZE", 12)
    top_n: int = tools._env_int("WEEKLY_PICK_TOP_N", 5)
    recent_behavior_days: int = tools._env_int("WEEKLY_PICK_RECENT_BEHAVIOR_DAYS", 30)
    recent_case_count: int = tools._env_int("WEEKLY_PICK_RECENT_CASE_COUNT", 10)
    live_flow_enable: bool = os.getenv("WEEKLY_PICK_LIVE_FLOW_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
    workers: int = max(1, tools._env_int("WEEKLY_PICK_WORKERS", 3))
    near_ma20_pct: float = tools._env_float("WEEKLY_PICK_NEAR_MA20_PCT", 5.0)
    extended_ma20_pct: float = tools._env_float("WEEKLY_PICK_EXTENDED_MA20_PCT", 12.0)
    surge_5d_pct: float = tools._env_float("WEEKLY_PICK_SURGE_5D_PCT", 15.0)
    overhead_zone_pct: float = tools._env_float("WEEKLY_PICK_OVERHEAD_ZONE_PCT", 5.0)
    support_zone_pct: float = tools._env_float("WEEKLY_PICK_SUPPORT_ZONE_PCT", 8.0)
    high_confidence_win_rate: float = tools._env_float("WEEKLY_PICK_HIGH_CONFIDENCE_WIN_RATE", 65.0)
    high_confidence_sample: int = tools._env_int("WEEKLY_PICK_HIGH_CONFIDENCE_SAMPLE", 30)
    unresolved_high_ratio: float = tools._env_float("WEEKLY_PICK_UNRESOLVED_HIGH_RATIO", 0.30)
    cache_dir: str = os.getenv("WEEKLY_PICK_CACHE_DIR", os.path.join("discord_ai_cache", "weekly_pick"))
    cache_seconds: int = tools._env_int("WEEKLY_PICK_CACHE_SECONDS", 43200)


# ============================================================
# 條件式查詢（之後要加新 filter，只需擴充這個 dataclass 與 parse／apply 兩處）
# ============================================================

@dataclass
class WeeklyPickFilters:
    min_win_rate: Optional[float] = None
    event_types: Set[str] = field(default_factory=set)
    branch: str = ""
    min_amount: Optional[float] = None
    near_ma20: bool = False
    exclude_extended: bool = False
    refresh: bool = False

    def signature(self) -> str:
        data = {
            "min_win_rate": self.min_win_rate,
            "event_types": sorted(self.event_types),
            "branch": self.branch,
            "min_amount": self.min_amount,
            "near_ma20": self.near_ma20,
            "exclude_extended": self.exclude_extended,
        }
        return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    def describe(self) -> List[str]:
        parts = []
        if self.min_win_rate is not None:
            parts.append(f"本次事件勝率 ≥ {self.min_win_rate:g}%")
        if self.event_types:
            parts.append(f"只看 {'、'.join(sorted(self.event_types))} 事件")
        if self.branch:
            parts.append(f"分點：{self.branch}")
        if self.min_amount is not None:
            parts.append(f"事件買進 ≥ {tools._money_text(self.min_amount).lstrip('+')}")
        if self.near_ma20:
            parts.append("靠近月線")
        if self.exclude_extended:
            parts.append("排除漲太多")
        return parts


WEEKLY_PICK_TRIGGERS = ("本週精選", "本周精選", "這週精選", "這周精選", "精選候選")
_WEEKLY_REPORT_WORDS = ("週報", "周報")


def is_weekly_pick_question(text: str) -> bool:
    s = re.sub(r"\s+", "", str(text or ""))
    if any(t in s for t in WEEKLY_PICK_TRIGGERS):
        return True
    return any(w in s for w in _WEEKLY_REPORT_WORDS) and any(k in s for k in ("適合", "候選", "找", "挑"))


def parse_weekly_pick_filters(text: str) -> WeeklyPickFilters:
    s = re.sub(r"\s+", "", str(text or "")).upper()
    filters = WeeklyPickFilters()
    match = re.search(r"勝率(\d{2,3}(?:\.\d+)?)%?以上", s)
    if match:
        filters.min_win_rate = float(match.group(1))
    filters.event_types = set(re.findall(r"([A-E])(?:類)?事件", s))
    match = re.search(r"買超(\d+(?:\.\d+)?)(萬|億)以上", s)
    if match:
        filters.min_amount = float(match.group(1)) * (10_000 if match.group(2) == "萬" else 100_000_000)
    filters.near_ma20 = any(k in s for k in ("靠近月線", "近月線", "月線附近", "MA20附近", "靠近MA20"))
    filters.exclude_extended = any(k in s for k in ("排除漲太多", "不要漲太多", "排除追高", "不追高"))
    filters.refresh = any(k in s for k in ("REFRESH", "重新計算", "強制更新"))
    try:
        normalized = tools.core().normalize_branch_name(text).upper()
        known = tools.get_known_branches()
        for alias in sorted(known, key=len, reverse=True):
            if len(alias) >= 3 and alias.upper() in normalized:
                filters.branch = known[alias]
                break
    except Exception as exc:  # 分點清單失敗時略過分點條件
        print(f"⚠️ 本週精選分點條件解析略過：{type(exc).__name__}: {exc}")
    return filters


# ============================================================
# 小工具
# ============================================================

def _clip01(value: float) -> float:
    return float(min(1.0, max(0.0, value)))


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return round(value, digits) if value is not None else None


class StageLog:
    """Debug 模式才輸出到 console。"""

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log

    def __call__(self, message: str) -> None:
        self.log(f"[本週精選] {message}")


# ============================================================
# Stage 2：分點 × 本次事件
# ============================================================

def evaluate_pair(
    branch: str,
    stock_code: str,
    pair_events: pd.DataFrame,
    perf: Dict[str, Any],
    config: WeeklyPickConfig,
    min_win_rate: float,
) -> Dict[str, Any]:
    """整理「分點 × 股票」本次觸發的事件與事件別歷史績效。

    避免 double counting：同一筆「分點 × 股票」只算一次事件績效。
    若區間內觸發多個等級（不同交易日的多筆事件），以各等級「本次買進金額占比」加權平均，
    權重合計為 1，不會把 A、C、D 的勝率各加一次分。
    """
    records = perf["branches"].get(branch, {})
    amount_by_code = pair_events.groupby("event_code")["buy_amount"].sum()
    total_amount = float(amount_by_code.sum())
    matched: Dict[str, Dict[str, Any]] = {}
    for code in tools.EVENT_CODES:
        if code not in amount_by_code.index:
            continue
        rec = records.get(code)
        share = float(amount_by_code[code]) / total_amount if total_amount > 0 else 0.0
        entry = {"amount_share": round(share, 3), "event_buy_amount_text": tools._money_text(amount_by_code[code])}
        if rec:
            entry.update({
                "raw_win_rate": rec.get("raw_win_rate"),
                "adjusted_win_rate": rec.get("adjusted_win_rate"),
                "included_count": tools._num(rec.get("included_count"), 0),
                "event_count": tools._num(rec.get("event_count"), 0),
                "unresolved_count": tools._num(rec.get("unresolved_count"), 0),
                "weighted_return": rec.get("weighted_return"),
                "avg_holding_days": rec.get("avg_holding_days"),
                "unresolved_ratio": rec.get("unresolved_ratio"),
            })
        else:
            entry["missing_history"] = True
        matched[code] = entry

    def weighted(key: str) -> Optional[float]:
        pairs = [(m["amount_share"], _f(m.get(key))) for m in matched.values()]
        pairs = [(w, v) for w, v in pairs if v is not None and w > 0]
        weight = sum(w for w, _ in pairs)
        return sum(w * v for w, v in pairs) / weight if weight > 0 else None

    overall = records.get("overall") or {}
    raw = weighted("raw_win_rate")
    dominant = max(matched, key=lambda c: matched[c]["amount_share"]) if matched else ""
    return {
        "branch": branch,
        "stock_code": stock_code,
        "triggered_events": list(matched.keys()),
        "dominant_event": dominant,
        "event_dates": sorted({tools._fmt_date(d) for d in pair_events["event_date"]}),
        "event_buy_amount": total_amount,
        "event_buy_amount_text": tools._money_text(total_amount),
        "event_performance": matched,
        "matched_raw_win_rate": _round(raw, 2),
        "matched_adjusted_win_rate": _round(weighted("adjusted_win_rate"), 2),
        "matched_included_count": _round(weighted("included_count"), 1),
        "matched_weighted_return": _round(weighted("weighted_return"), 2),
        "matched_unresolved_ratio": _round(weighted("unresolved_ratio"), 3),
        "overall": {
            "raw_win_rate": overall.get("raw_win_rate"),
            "adjusted_win_rate": overall.get("adjusted_win_rate"),
            "included_count": tools._num(overall.get("included_count"), 0),
            "event_count": tools._num(overall.get("event_count"), 0),
            "unresolved_count": tools._num(overall.get("unresolved_count"), 0),
            "weighted_return": overall.get("weighted_return"),
        },
        "primary": bool(raw is not None and raw >= min_win_rate),
        "has_history": any(not m.get("missing_history") for m in matched.values()),
    }


def score_event_performance(pair: Dict[str, Any], config: WeeklyPickConfig, min_win_rate: float) -> Tuple[float, List[str]]:
    """A. 本次事件歷史績效（25）：修正勝率 15 ＋ 樣本數 5 ＋ 加權報酬 5；overall 不計分。"""
    adj = _f(pair.get("matched_adjusted_win_rate"))
    raw = _f(pair.get("matched_raw_win_rate"))
    n = _f(pair.get("matched_included_count")) or 0.0
    ret = _f(pair.get("matched_weighted_return"))
    if adj is None:
        return 0.0, ["勝率統計沒有本次事件的歷史資料"]
    win_part = 15.0 * _clip01((adj - 50.0) / 30.0)
    sample_part = 5.0 * _clip01(math.log1p(n) / math.log1p(50.0))
    return_part = 5.0 * _clip01(((ret if ret is not None else 5.0) + 10.0) / 40.0)
    score = win_part + sample_part + return_part
    reasons = [f"修正勝率 {adj:.1f}% → {win_part:.1f}", f"樣本 {n:.0f} 筆 → {sample_part:.1f}", f"加權報酬 {ret if ret is not None else '-'}% → {return_part:.1f}"]
    if raw is not None and raw < min_win_rate:
        score *= 0.7
        reasons.append(f"本次事件原始勝率 {raw:.1f}% 低於門檻 {min_win_rate:g}%，×0.7")
    return round(min(25.0, score), 2), reasons


def score_warrant_amount(stock: Dict[str, Any], lead_live: Dict[str, Any]) -> Tuple[float, List[str]]:
    """C. 權證買超金額（25）：總事件買進 12 ＋ 高品質分點買進 8 ＋ 多分點共振 5（log scaling、上限封頂）。"""
    total = stock["event_buy_amount_total"]
    hq_amount = stock["high_quality_amount"]
    hq_count = stock["high_quality_branch_count"]
    total_part = 12.0 * _clip01(math.log10(max(total, 1.0) / 1e6) / math.log10(50.0))
    hq_part = 8.0 * _clip01(math.log10(max(hq_amount, 1.0) / 1e6) / math.log10(30.0))
    resonance = 0.0 if hq_count <= 1 else 3.0 if hq_count == 2 else 5.0
    score = total_part + hq_part + resonance
    reasons = [
        f"事件買進合計 {tools._money_text(total)} → {total_part:.1f}",
        f"高品質分點買進 {tools._money_text(hq_amount)} → {hq_part:.1f}",
        f"高品質分點 {hq_count} 家 → {resonance:.1f}",
    ]
    net5 = _f(lead_live.get("net_buy_5d")) if lead_live.get("has_trades") else None
    if net5 is not None and net5 <= 0:
        score -= 3.0
        reasons.append(f"主力分點近5日淨額 {lead_live.get('net_buy_5d_text')}（非淨買）→ -3")
    return round(min(25.0, max(0.0, score)), 2), reasons


def score_recent_behavior(behavior: Dict[str, Any], config: WeeklyPickConfig) -> Tuple[float, List[str]]:
    """B. 同分點近期操作（10）：基準 5 分，依加碼／減碼／反覆與已完成案例微調。"""
    if not behavior.get("found"):
        return 5.0, ["近期操作資料不足，給中性 5 分"]
    score, reasons = 5.0, ["基準 5"]
    same = behavior.get("same_stock") or {}
    live = same.get("live_flow") or {}
    if live.get("has_trades"):
        if live.get("continuous_buying"):
            score += 2.0
            reasons.append("近5日持續加碼 +2")
        if (_f(live.get("net_buy_5d")) or 0) > 0 and (_f(live.get("net_buy_20d")) or 0) > 0:
            score += 1.0
            reasons.append("5日與20日皆淨買，部位增加 +1")
        if live.get("reducing_recently"):
            score -= 3.0
            reasons.append("近期開始減碼 -3")
        if live.get("direction_choppy"):
            score -= 2.0
            reasons.append("近20日買賣方向反覆 -2")
    else:
        if len(same.get("round_open_events") or []) >= 2:
            score += 1.0
            reasons.append("本輪仍有 2 筆以上未出清事件 +1")
        if same.get("sells_in_window"):
            score -= 1.5
            reasons.append("觀察期內有減碼／出清紀錄 -1.5")
    history = same.get("stock_history_cases") or {}
    if (history.get("completed_cases") or 0) >= 3:
        edge = (history["wins"] - history["losses"]) / history["completed_cases"]
        score += 1.5 * max(-1.0, min(1.0, edge))
        reasons.append(f"同股票已完成 {history['completed_cases']} 筆（{history['wins']}勝{history['losses']}敗）{1.5 * edge:+.1f}")
    recent = (behavior.get("branch_recent") or {}).get("recent_cases") or {}
    if (recent.get("completed_cases") or 0) >= 5:
        edge = (recent["wins"] - recent["losses"]) / recent["completed_cases"]
        score += 1.0 * max(-1.0, min(1.0, edge))
        reasons.append(f"近期已完成 {recent['completed_cases']} 筆 {edge:+.2f}")
    elif recent:
        reasons.append("近期已完成案例少於 5 筆，不加減分")
    if (recent.get("unresolved_cases") or 0) >= 6:
        score -= 0.5
        reasons.append("近期未完成案例偏多 -0.5")
    return round(min(10.0, max(0.0, score)), 2), reasons


def _technical_extras(stock_code: str) -> Dict[str, Any]:
    """從既有 calculate_indicators 結果取出評分需要的序列資訊（不另算指標）。"""
    df = tools._load_price_bundle(stock_code)["df"]
    latest = df.iloc[-1]
    close = _f(latest.get("Close"))
    close_5 = _f(df["Close"].iloc[-6]) if len(df) >= 6 else None
    mid_now = _f(latest.get("BB_MID"))
    mid_5 = _f(df["BB_MID"].iloc[-6]) if len(df) >= 6 else None
    open_ = _f(latest.get("Open"))
    volume = _f(latest.get("Volume"))
    mv20 = _f(latest.get("MV20"))
    long_black = bool(
        None not in (open_, close, volume, mv20)
        and mv20 > 0 and volume > 2 * mv20 and close < open_ and (open_ - close) / open_ >= 0.04
    )
    return {
        "return_5d_pct": _round((close / close_5 - 1) * 100 if close and close_5 else None, 2),
        "bb_mid_rising": bool(mid_now is not None and mid_5 is not None and mid_now > mid_5),
        "heavy_volume_long_black": long_black,
    }


def score_technical(tech: Dict[str, Any], vp: Dict[str, Any], extras: Dict[str, Any], config: WeeklyPickConfig) -> Tuple[float, List[str], Dict[str, bool]]:
    """D. 技術型態（25）：基準 12.5，依均線、MA20 位置、大量區型態、布林與追高風險加減。"""
    score, reasons, marks = 12.5, ["基準 12.5"], {"extended": False, "overhead": False}
    mas = tech.get("moving_averages") or {}
    values = {k: _f((mas.get(k) or {}).get("value")) for k in ("MA5", "MA10", "MA20", "MA60")}
    alignment = tech.get("ma_alignment", "")
    if alignment == "多頭排列":
        score += 5
        reasons.append("MA5>MA10>MA20>MA60 +5")
    elif None not in (values["MA5"], values["MA10"], values["MA20"]) and values["MA5"] > values["MA10"] > values["MA20"]:
        score += 3
        reasons.append("MA5>MA10>MA20 +3")
    elif alignment == "空頭排列":
        score -= 6
        reasons.append("空頭排列 -6")
    positions = [(mas.get(k) or {}).get("position") for k in ("MA5", "MA10", "MA20", "MA60")]
    if positions and all(p == "跌破" for p in positions):
        score -= 5
        reasons.append("全面跌破 MA5/10/20/60 -5")
    dist20 = _f((mas.get("MA20") or {}).get("distance_pct"))
    cross = tech.get("ma20_cross_recent_3_days") or {}
    if cross.get("just_broke_above"):
        score += 3
        reasons.append("近3日剛站上 MA20 +3")
    elif dist20 is not None and 0 <= dist20 <= config.near_ma20_pct:
        score += 3
        reasons.append(f"位於 MA20 上方 {dist20:.1f}%（不遠）+3")
    elif dist20 is not None and config.near_ma20_pct < dist20 <= config.extended_ma20_pct:
        score += 1
        reasons.append(f"位於 MA20 上方 {dist20:.1f}% +1")
    if dist20 is not None and dist20 > config.extended_ma20_pct:
        score -= 4
        marks["extended"] = True
        reasons.append(f"距 MA20 {dist20:.1f}% 過遠 -4")
    elif dist20 is not None and dist20 < 0 and not all(p == "跌破" for p in positions):
        score -= 3
        reasons.append(f"跌破 MA20（{dist20:.1f}%）-3")

    max_zone = vp.get("maximum_volume_zone") or {}
    position = str(vp.get("position_vs_two_zones", ""))
    above_max = "上方" in str(max_zone.get("close_relation", ""))
    if vp.get("recent_breakout") and above_max:
        score += 3
        reasons.append("近期突破最大量區且站穩 +3")
        if vp.get("retest_after_breakout"):
            score += 2
            reasons.append("突破後回踩未破 +2")
    if "之上" in position:
        score += 2
        reasons.append("股價在兩個大量區之上 +2")
    elif "之下" in position:
        score -= 3
        reasons.append("股價在兩個大量區之下 -3")
    if vp.get("recent_breakdown") and not above_max:
        score -= 5
        reasons.append("近期跌破最大量區且未站回 -5")

    bb = tech.get("bollinger") or {}
    percent_b = _f(bb.get("percent_b"))
    if bb.get("position") == "位於中軌與上軌之間":
        score += 1
        reasons.append("布林中軌與上軌之間 +1")
    if extras.get("bb_mid_rising") and bb.get("position") in ("位於中軌與上軌之間", "突破上軌"):
        score += 1
        reasons.append("布林中軌向上 +1")
    if percent_b is not None and percent_b > 105:
        score -= 2
        marks["extended"] = True
        reasons.append(f"布林 %B {percent_b:.0f} 上軌乖離過高 -2")
    ret5 = _f(extras.get("return_5d_pct"))
    if ret5 is not None and ret5 > config.surge_5d_pct:
        score -= 3
        marks["extended"] = True
        reasons.append(f"近5日已漲 {ret5:.1f}% -3")
    if extras.get("heavy_volume_long_black"):
        score -= 4
        reasons.append("爆量長黑 -4")
    close = _f(vp.get("close"))
    for key in ("maximum_volume_zone", "second_volume_zone"):
        zone = vp.get(key) or {}
        low = _f(zone.get("price_low"))
        if close and low and low > close and (low / close - 1) * 100 <= config.overhead_zone_pct:
            score -= 2
            marks["overhead"] = True
            reasons.append(f"上方 {zone.get('label')} {low:g} 距離 {((low / close - 1) * 100):.1f}% 形成壓力 -2")
            break
    return round(min(25.0, max(0.0, score)), 2), reasons, marks


def score_support(tech: Dict[str, Any], vp: Dict[str, Any], config: WeeklyPickConfig) -> Tuple[float, List[str]]:
    """E. 支撐品質（15）：MA20 5 ＋ MA60 3 ＋ 最大量區 4 ＋ 第二大量區 3。"""
    score, reasons = 0.0, []
    mas = tech.get("moving_averages") or {}
    dist20 = _f((mas.get("MA20") or {}).get("distance_pct"))
    dist60 = _f((mas.get("MA60") or {}).get("distance_pct"))
    if dist20 is not None and 0 <= dist20 <= 3:
        score += 5
        reasons.append(f"MA20 在下方 {dist20:.1f}% +5")
    elif dist20 is not None and 3 < dist20 <= 6:
        score += 3
        reasons.append(f"MA20 在下方 {dist20:.1f}% +3")
    elif dist20 is not None and 6 < dist20 <= 10:
        score += 1
        reasons.append(f"MA20 在下方 {dist20:.1f}% +1")
    if dist60 is not None and 0 <= dist60 <= 8:
        score += 3
        reasons.append(f"MA60 在下方 {dist60:.1f}% +3")
    elif dist60 is not None and dist60 > 8:
        score += 1
        reasons.append(f"MA60 在下方較遠（{dist60:.1f}%）+1")
    close = _f(vp.get("close"))
    for key, near_points, far_points, inside_points in (("maximum_volume_zone", 4, 2, 2), ("second_volume_zone", 3, 1, 1)):
        zone = vp.get(key) or {}
        low, high = _f(zone.get("price_low")), _f(zone.get("price_high"))
        if not close or low is None or high is None:
            continue
        if high <= close:
            gap = (close / high - 1) * 100
            points = near_points if gap <= config.support_zone_pct else far_points
            score += points
            reasons.append(f"{zone.get('label')} {low:g}～{high:g} 在下方 {gap:.1f}% +{points}")
        elif low <= close <= high:
            score += inside_points
            reasons.append(f"股價位於{zone.get('label')}內 +{inside_points}")
    if not reasons:
        reasons.append("下方沒有明確支撐")
    return round(min(15.0, score), 2), reasons


# ============================================================
# 主流程
# ============================================================

class WeeklyPickEngine:
    """本週精選計算（純 Python）；Gemini 說明由呼叫端決定是否執行。"""

    def __init__(self, config: Optional[WeeklyPickConfig] = None, log: Callable[[str], None] = print) -> None:
        self.config = config or WeeklyPickConfig()
        self.log = StageLog(log)

    # ---------------- Stage 1 + 2 ----------------
    def build_stock_pool(self, filters: WeeklyPickFilters) -> Dict[str, Any]:
        config = self.config
        min_win_rate = filters.min_win_rate if filters.min_win_rate is not None else config.min_event_win_rate
        bundle = tools.load_abcde_event_rows()
        events, latest = bundle["events"], bundle["latest_event_date"]
        if latest is None:
            raise tools.SheetUnavailableError("A～E 事件表沒有資料")
        start, end = tools._recent_event_dates(latest, config.event_window_trading_days, events)
        window = events[(events["event_date"] >= start) & (events["event_date"] <= end)].copy()
        self.log(f"事件視窗 {tools._fmt_date(start)}～{tools._fmt_date(end)}｜初始事件 {len(window):,} 筆｜股票 {window['stock_code'].nunique():,} 檔")
        if filters.branch:
            window = window[window["branch"] == filters.branch]
        if filters.event_types:
            window = window[window["event_code"].isin(filters.event_types)]
        window = window[window["buy_amount"].fillna(0) > 0]
        self.log(f"套用條件後有買進事件的股票 {window['stock_code'].nunique():,} 檔｜條件：{filters.describe() or '無'}")

        perf = tools.read_branch_event_performance()
        pairs = [
            evaluate_pair(branch, code, g, perf, config, min_win_rate)
            for (branch, code), g in window.groupby(["branch", "stock_code"])
        ]
        stocks: Dict[str, Dict[str, Any]] = {}
        for pair in pairs:
            pair["event_score"], pair["event_score_reasons"] = score_event_performance(pair, config, min_win_rate)
            stock = stocks.setdefault(pair["stock_code"], {"stock_code": pair["stock_code"], "pairs": []})
            stock["pairs"].append(pair)
        for stock in stocks.values():
            ranked = sorted(stock["pairs"], key=lambda p: (p["primary"], p["event_score"], p["event_buy_amount"]), reverse=True)
            stock["pairs"] = ranked
            high_quality = [
                p for p in ranked
                if p["primary"] and (p["matched_included_count"] or 0) >= config.min_event_sample
            ]
            stock["lead"] = ranked[0]
            stock["high_quality_pairs"] = high_quality
            stock["event_buy_amount_total"] = float(sum(p["event_buy_amount"] for p in ranked))
            stock["high_quality_amount"] = float(sum(p["event_buy_amount"] for p in high_quality))
            stock["high_quality_branch_count"] = len(high_quality)
            stock["max_high_quality_single"] = max((p["event_buy_amount"] for p in high_quality), default=0.0)
            stock["has_primary"] = any(p["primary"] for p in ranked)
        pool = list(stocks.values())
        if filters.min_amount is not None:
            pool = [s for s in pool if s["event_buy_amount_total"] >= filters.min_amount]
        if filters.min_win_rate is not None:
            pool = [s for s in pool if s["has_primary"]]
        primary_count = sum(1 for s in pool if s["has_primary"])
        adjusted_ok = sum(1 for s in pool if (s["lead"]["matched_adjusted_win_rate"] or 0) >= min_win_rate)
        self.log(
            f"matched event 原始勝率 ≥{min_win_rate:g}% 的股票 {primary_count:,} 檔｜"
            f"修正勝率仍 ≥{min_win_rate:g}% 的股票 {adjusted_ok:,} 檔"
        )
        return {
            "pool": pool,
            "min_win_rate": min_win_rate,
            "window_start": tools._fmt_date(start),
            "window_end": tools._fmt_date(end),
            "latest_event_date": tools._fmt_date(latest),
            "perf_sheet_updated_at": perf.get("sheet_updated_at", ""),
            "perf_missing_columns": perf.get("missing_columns", []),
            "priors": perf.get("priors", {}),
            "prior_strength": perf.get("prior_strength"),
        }

    def shortlist(self, pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """用不需要抓價的分數（事件績效＋金額）預排序，主要候選優先，縮到 shortlist_size。"""
        for stock in pool:
            stock["pre_amount_score"], _ = score_warrant_amount(stock, {})
            stock["pre_score"] = stock["lead"]["event_score"] + stock["pre_amount_score"]
        ranked = sorted(pool, key=lambda s: (s["has_primary"], s["pre_score"]), reverse=True)
        chosen = ranked[: max(self.config.top_n, self.config.shortlist_size)]
        self.log(f"預排序後進入技術面計算：{len(chosen)} 檔 → {', '.join(s['stock_code'] for s in chosen)}")
        return chosen

    # ---------------- Stage 3 ----------------
    def enrich_and_score(self, stock: Dict[str, Any], min_win_rate: float) -> Dict[str, Any]:
        config = self.config
        code = stock["stock_code"]
        lead = stock["lead"]
        flags: Set[str] = set()
        try:
            stock["stock_name"] = tools.resolve_stock_name(code)
        except tools.ToolDataError:
            stock["stock_name"] = ""

        behavior: Dict[str, Any] = {"found": False}
        try:
            behavior = tools.get_branch_recent_behavior(
                lead["branch"], code,
                lookback_days=config.recent_behavior_days,
                recent_case_count=config.recent_case_count,
                include_live_flow=config.live_flow_enable,
            )
        except Exception as exc:  # 近期行為失敗只影響 B 分數
            behavior = {"found": False, "reason": f"{type(exc).__name__}: {exc}"}
            flags.add("recent_behavior_unavailable")
        lead_live = ((behavior.get("same_stock") or {}).get("live_flow") or {}) if behavior.get("found") else {}
        if config.live_flow_enable and not lead_live.get("available"):
            flags.add("recent_flow_unavailable")

        for pair in stock["high_quality_pairs"][:4]:
            if pair["branch"] == lead["branch"] or not config.live_flow_enable:
                continue
            try:
                live = tools._live_same_stock_flow(code, pair["branch"])
                pair["net_buy_5d_text"] = live.get("net_buy_5d_text", "") if live.get("has_trades") else "近20日無成交"
            except Exception as exc:
                pair["net_buy_5d_text"] = ""
                print(f"⚠️ 本週精選次要分點流水略過：{code} {pair['branch']}｜{type(exc).__name__}: {exc}")

        tech: Dict[str, Any] = {}
        vp: Dict[str, Any] = {}
        extras: Dict[str, Any] = {}
        try:
            tech = tools.get_technical_analysis(code)
            vp = tools.get_volume_profile(code)
            extras = _technical_extras(code)
        except Exception as exc:  # 股價失敗時技術與支撐給 0，並加旗標
            flags.add("technical_unavailable")
            stock["technical_error"] = f"{type(exc).__name__}: {exc}"

        breakdown: Dict[str, float] = {}
        reasons: Dict[str, List[str]] = {}
        breakdown["event_performance_score"], reasons["event_performance"] = lead["event_score"], lead["event_score_reasons"]
        breakdown["recent_branch_behavior_score"], reasons["recent_branch_behavior"] = score_recent_behavior(behavior, config)
        breakdown["warrant_amount_score"], reasons["warrant_amount"] = score_warrant_amount(stock, lead_live)
        if tech and vp:
            breakdown["technical_score"], reasons["technical"], marks = score_technical(tech, vp, extras, config)
            breakdown["support_score"], reasons["support"] = score_support(tech, vp, config)
        else:
            breakdown["technical_score"], reasons["technical"], marks = 0.0, ["技術資料取得失敗"], {"extended": False, "overhead": False}
            breakdown["support_score"], reasons["support"] = 0.0, ["技術資料取得失敗"]
        total = round(sum(breakdown.values()), 1)

        n = lead["matched_included_count"] or 0
        adj = lead["matched_adjusted_win_rate"] or 0
        raw = lead["matched_raw_win_rate"]
        overall_raw = _f(lead["overall"].get("raw_win_rate"))
        if n < config.min_event_sample:
            flags.add("low_sample")
        if adj >= config.high_confidence_win_rate and n >= config.high_confidence_sample:
            flags.add("high_event_confidence")
        if not lead["primary"]:
            flags.add("below_event_win_rate_threshold")
        # 只看「本次事件歷史」的未納入勝率比例：比例高代表歷史勝率可能被高估。
        # 近期案例本來就多半尚未結束，只揭露數量，不拿來觸發這個旗標。
        if (lead["matched_unresolved_ratio"] or 0) >= config.unresolved_high_ratio:
            flags.add("unresolved_cases_high")
        if overall_raw is not None and raw is not None and overall_raw >= 70 and raw < min_win_rate:
            flags.add("overall_high_but_event_weak")
        if overall_raw is not None and raw is not None and raw >= 75 and overall_raw < 65:
            flags.add("event_strong_overall_average")
        if marks.get("extended"):
            flags.add("extended_price")
        if marks.get("overhead"):
            flags.add("overhead_resistance")
        if lead_live.get("reducing_recently"):
            flags.add("branch_recently_reducing")
        if (breakdown["event_performance_score"] >= 18 and breakdown["technical_score"] <= 8) or (
            lead_live.get("has_trades") and (_f(lead_live.get("net_buy_5d")) or 0) <= 0
        ):
            flags.add("conflicting_signals")
        if breakdown["support_score"] >= 11:
            flags.add("strong_support")
        if stock["high_quality_branch_count"] >= 2:
            flags.add("multi_branch_confirmation")

        stock.update({
            "score": total,
            "score_breakdown": breakdown,
            "score_reasons": reasons,
            "quality_flags": sorted(flags),
            "behavior": behavior,
            "technical": tech,
            "volume_profile": vp,
            "technical_extras": extras,
        })
        return stock

    def run(self, filters: WeeklyPickFilters) -> Dict[str, Any]:
        started = time.perf_counter()
        base = self.build_stock_pool(filters)
        shortlisted = self.shortlist(base["pool"])
        with ThreadPoolExecutor(max_workers=self.config.workers, thread_name_prefix="weekly-pick") as executor:
            scored = list(executor.map(lambda s: self.enrich_and_score(s, base["min_win_rate"]), shortlisted))
        self.log(f"技術面計算完成：{len(scored)} 檔")
        if filters.near_ma20:
            scored = [
                s for s in scored
                if s.get("technical") and 0 <= (_f(((s["technical"].get("moving_averages") or {}).get("MA20") or {}).get("distance_pct")) or -1) <= self.config.near_ma20_pct
            ]
        if filters.exclude_extended:
            scored = [s for s in scored if "extended_price" not in s["quality_flags"]]
        ranked = sorted(scored, key=lambda s: (s["score"], s["event_buy_amount_total"]), reverse=True)
        for label, size in (("TOP20", 20), ("TOP10", 10)):
            self.log(f"{label}：" + "、".join(f"{s['stock_code']}({s['score']})" for s in ranked[:size]))
        top = ranked[: self.config.top_n]
        for rank, stock in enumerate(top, 1):
            stock["rank"] = rank
            self._debug_candidate(stock)
        return {
            **{k: v for k, v in base.items() if k != "pool"},
            "filters": filters.describe(),
            "pool_size": len(base["pool"]),
            "scored_size": len(scored),
            "top": top,
            "elapsed": round(time.perf_counter() - started, 1),
        }

    def _debug_candidate(self, stock: Dict[str, Any]) -> None:
        lead = stock["lead"]
        b = stock["score_breakdown"]
        behavior = stock.get("behavior") or {}
        live = ((behavior.get("same_stock") or {}).get("live_flow") or {})
        recent = ((behavior.get("branch_recent") or {}).get("recent_cases") or {})
        self.log(
            f"#{stock['rank']} {stock['stock_code']} {stock.get('stock_name', '')}｜主力 {lead['branch']}｜"
            f"事件 {lead['triggered_events']}｜"
            + "；".join(
                f"{c}: raw={m.get('raw_win_rate')} adj={m.get('adjusted_win_rate')} n={m.get('included_count')} unresolved={m.get('unresolved_count')}"
                for c, m in lead["event_performance"].items()
            )
            + f"｜overall raw={lead['overall'].get('raw_win_rate')} n={lead['overall'].get('included_count')}"
        )
        self.log(
            f"   recent：5日 {live.get('net_buy_5d_text', '-')}｜10日 {live.get('net_buy_10d_text', '-')}｜"
            f"持續加碼={live.get('continuous_buying')}｜減碼={live.get('reducing_recently')}｜"
            f"completed={recent.get('completed_cases')} (W{recent.get('wins')}/L{recent.get('losses')}) unresolved={recent.get('unresolved_cases')}"
        )
        self.log(
            f"   event_performance_score = {b['event_performance_score']} / 25｜recent_branch_behavior_score = {b['recent_branch_behavior_score']} / 10｜"
            f"warrant_amount_score = {b['warrant_amount_score']} / 25｜technical_score = {b['technical_score']} / 25｜"
            f"support_score = {b['support_score']} / 15｜total = {stock['score']} / 100"
        )
        self.log(f"   flags={stock['quality_flags']}")


# ============================================================
# 輸出：Gemini payload／規則式排版
# ============================================================

def _perf_brief(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "raw_win_rate": m.get("raw_win_rate"),
        "adjusted_win_rate": m.get("adjusted_win_rate"),
        "sample_included": m.get("included_count"),
        "unresolved_count": m.get("unresolved_count"),
        "weighted_return": m.get("weighted_return"),
        "avg_holding_days": m.get("avg_holding_days"),
        "amount_share": m.get("amount_share"),
    }


def candidate_payload(stock: Dict[str, Any]) -> Dict[str, Any]:
    """TOP5 單檔交給 Gemini 的精簡 JSON。"""
    lead = stock["lead"]
    behavior = stock.get("behavior") or {}
    same = behavior.get("same_stock") or {}
    live = same.get("live_flow") or {}
    branch_recent = behavior.get("branch_recent") or {}
    tech = stock.get("technical") or {}
    vp = stock.get("volume_profile") or {}
    mas = tech.get("moving_averages") or {}
    return {
        "rank": stock["rank"],
        "stock_code": stock["stock_code"],
        "stock_name": stock.get("stock_name", ""),
        "score": stock["score"],
        "score_breakdown": stock["score_breakdown"],
        "branch": {
            "name": lead["branch"],
            "event_buy_amount_in_window_text": lead["event_buy_amount_text"],
            "net_buy_5d_text": live.get("net_buy_5d_text") if live.get("has_trades") else None,
            "event_dates": lead["event_dates"],
            "triggered_events": lead["triggered_events"],
            "event_performance": {c: _perf_brief(m) for c, m in lead["event_performance"].items()},
            "matched_weighted": {
                "raw_win_rate": lead["matched_raw_win_rate"],
                "adjusted_win_rate": lead["matched_adjusted_win_rate"],
                "sample_included": lead["matched_included_count"],
                "weighted_return": lead["matched_weighted_return"],
            },
            "overall_background": lead["overall"],
        },
        "other_high_quality_branches": [
            {
                "name": p["branch"],
                "triggered_events": p["triggered_events"],
                "matched_raw_win_rate": p["matched_raw_win_rate"],
                "matched_adjusted_win_rate": p["matched_adjusted_win_rate"],
                "sample_included": p["matched_included_count"],
                "event_buy_amount_text": p["event_buy_amount_text"],
                "net_buy_5d_text": p.get("net_buy_5d_text"),
            }
            for p in stock["high_quality_pairs"] if p["branch"] != lead["branch"]
        ][:3],
        "warrant": {
            "event_buy_amount_total_text": tools._money_text(stock["event_buy_amount_total"]),
            "high_quality_amount_text": tools._money_text(stock["high_quality_amount"]),
            "max_high_quality_single_text": tools._money_text(stock["max_high_quality_single"]),
            "high_quality_branch_count": stock["high_quality_branch_count"],
        },
        "recent_behavior": {
            "same_stock": {
                "net_buy_5d_text": live.get("net_buy_5d_text"),
                "net_buy_10d_text": live.get("net_buy_10d_text"),
                "net_buy_20d_text": live.get("net_buy_20d_text"),
                "continuous_buying": live.get("continuous_buying"),
                "reducing_recently": live.get("reducing_recently"),
                "direction_choppy": live.get("direction_choppy"),
                "latest_add_date": live.get("latest_add_date"),
                "main_warrants": live.get("main_warrants"),
                "rotation_note": live.get("rotation_note"),
                "round_first_event_date": same.get("round_first_event_date"),
                "round_trading_days_since_start": same.get("round_trading_days_since_start"),
                "round_event_buy_amount_text": same.get("round_event_buy_amount_text"),
                "sells_in_window": same.get("sells_in_window"),
                "stock_history_cases": same.get("stock_history_cases"),
                "live_flow_available": live.get("available"),
            },
            "branch_recent": {
                "recent_cases": branch_recent.get("recent_cases"),
                "recent_cases_sentence": branch_recent.get("recent_cases_sentence"),
                "holding_style": branch_recent.get("holding_style"),
                "event_type_distribution": branch_recent.get("event_type_distribution"),
                "top_buy_stocks": [s.get("stock_name") or s.get("stock_code") for s in branch_recent.get("top_buy_stocks", [])[:3]],
            },
        },
        "technical": {
            "data_date": tech.get("data_date"),
            "close": tech.get("close"),
            "ma_alignment": tech.get("ma_alignment"),
            "MA20": mas.get("MA20"),
            "MA60": mas.get("MA60"),
            "ma20_cross_recent_3_days": tech.get("ma20_cross_recent_3_days"),
            "kd_signals": (tech.get("kd") or {}).get("signals"),
            "macd_osc_trend": (tech.get("macd") or {}).get("osc_trend"),
            "bollinger_position": (tech.get("bollinger") or {}).get("position"),
            "return_5d_pct": (stock.get("technical_extras") or {}).get("return_5d_pct"),
        },
        "volume_profile": {
            "maximum_volume_zone": {k: (vp.get("maximum_volume_zone") or {}).get(k) for k in ("price_low", "price_high", "close_relation")},
            "second_volume_zone": {k: (vp.get("second_volume_zone") or {}).get(k) for k in ("price_low", "price_high", "close_relation")},
            "position_vs_two_zones": vp.get("position_vs_two_zones"),
            "pattern_label": vp.get("pattern_label"),
            "recent_maximum_zone_event": vp.get("recent_maximum_zone_event"),
        },
        "support_reasons": (stock.get("score_reasons") or {}).get("support"),
        "technical_reasons": (stock.get("score_reasons") or {}).get("technical"),
        "quality_flags": stock["quality_flags"],
    }


WEEKLY_PICK_SYSTEM_PROMPT = """你是我的私人台股研究助理。
你的工作不是推薦我買股票，而是協助我找出「本週最值得進一步研究、最適合撰寫 Discord 本週精選週報的候選股票」。
TOP5 已由 Python 依分數排好，你只負責解釋，不得更改排名、不得新增或刪除股票。
只能根據 tool_results 提供的資料回答，不得自行補充不存在的數據（股價、勝率、分點、金額、均線、大量區、新聞都一樣）。

分析權證分點時的優先順序：
1. 本次 matched A/B/C/D/E 事件的 adjusted_win_rate（修正勝率）
2. 本次事件 raw_win_rate 與樣本數 sample_included
3. weighted_return（加權報酬）
4. recent_behavior（近期操作）
5. overall_background（總勝率，只能當背景）
規則：
- overall 很高但本次事件勝率差（flag overall_high_but_event_weak），必須提醒。
- overall 普通但本次事件歷史表現很好（flag event_strong_overall_average），也要指出。
- 近期操作只能當 context，不得因最近幾筆成功就說分點勝率很高。提到近期案例時必須同時寫出已有結果筆數、勝敗與仍未完成筆數，例如「最近10筆案例中，6筆已有結果，其中4勝2敗；另4筆仍未完成」。
- 未完成案例不能算成功。
- 有 low_sample、unresolved_cases_high、extended_price、overhead_resistance、branch_recently_reducing、conflicting_signals 等 flag 時要寫進【注意】。
- 不要把買超等同看多必漲，不要把高歷史勝率說成這次一定成功，不提供目標價或報酬預測。
- 金額沿用資料中的「萬／億」文字。

輸出格式（Discord 訊息，不要表格、不要程式碼區塊）：
📊 艾斯 AI｜本週精選候選
第一名用完整格式：
🥇 代號 名稱
綜合分數：xx / 100
【權證】主力分點與本次買進、本次符合的事件、各事件歷史（勝率｜n=樣本）、總勝率（背景）、本次主要加分來源
【分點近期操作】是否持續加碼／減碼、近期案例 completed 與 unresolved
【技術面】3～4 點
【優點】2～4 點
【注意】1～3 點
【適合週報的原因】1～2 句
第二～五名（🥈🥉4️⃣5️⃣）用精簡格式：分數、權證一句、事件勝率一句、技術一句、注意一句。
全文控制在 2500 字以內。"""


def build_gemini_prompt(result: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    payload = {
        "task": "本週精選候選說明",
        "data_time": {
            "abcde_event_window": f"{result['window_start']}～{result['window_end']}",
            "latest_event_date": result["latest_event_date"],
            "win_rate_sheet_updated_at": result["perf_sheet_updated_at"],
        },
        "filters": result["filters"],
        "min_event_win_rate": result["min_win_rate"],
        "candidates": [candidate_payload(s) for s in result["top"]],
    }
    text = json.dumps(tools_prune(payload), ensure_ascii=False, separators=(",", ":"))
    return f"{WEEKLY_PICK_SYSTEM_PROMPT}\n\ntool_results（JSON）：\n{text}\n", payload


_KEEP_EMPTY_KEYS = {"quality_flags", "triggered_events"}


def tools_prune(value: Any) -> Any:
    """移除空值縮小 prompt；quality_flags 空清單代表「沒有警示」，必須保留。"""
    if isinstance(value, dict):
        pruned = {k: tools_prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if k in _KEEP_EMPTY_KEYS or v not in (None, "", [], {})}
    if isinstance(value, list):
        return [v for v in (tools_prune(i) for i in value) if v not in (None, "", [], {})]
    return value


_FLAG_TEXT = {
    "low_sample": "本次事件樣本數偏少",
    "unresolved_cases_high": "未完成案例比例偏高，勝率可能被高估",
    "extended_price": "股價離支撐偏遠／短線漲多",
    "overhead_resistance": "上方有大量區壓力",
    "branch_recently_reducing": "主力分點近期開始減碼",
    "conflicting_signals": "籌碼與技術訊號不一致",
    "strong_support": "下方支撐明確",
    "multi_branch_confirmation": "多個高品質分點同時布局",
    "high_event_confidence": "本次事件歷史表現可信度高",
    "below_event_win_rate_threshold": "本次事件勝率未達門檻",
    "overall_high_but_event_weak": "總勝率高但本次事件勝率偏弱",
    "event_strong_overall_average": "總勝率普通但本次事件表現佳",
    "recent_flow_unavailable": "近期逐日流水取得失敗",
    "recent_behavior_unavailable": "分點近期操作資料取得失敗",
    "technical_unavailable": "技術資料取得失敗",
}
_POSITIVE_FLAGS = {"strong_support", "multi_branch_confirmation", "high_event_confidence", "event_strong_overall_average"}
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def _count_text(value: Any) -> str:
    number = _f(value)
    return f"{number:g}" if number is not None else "-"


def _event_line(code: str, m: Dict[str, Any]) -> str:
    raw = m.get("raw_win_rate")
    adj = m.get("adjusted_win_rate")
    n = m.get("included_count")
    if raw is None and adj is None:
        return f"{code}事件：勝率統計無資料"
    text = f"{code}事件：{raw if raw is not None else '-'}%（修正 {adj if adj is not None else '-'}%）｜n={_count_text(n)}"
    if m.get("unresolved_count"):
        text += f"｜未完成 {m['unresolved_count']:g}"
    return text


def format_rule_based(result: Dict[str, Any]) -> str:
    """Gemini 不可用或核對未通過時的系統排版（不含 AI 解讀）。"""
    lines = ["📊 艾斯 AI｜本週精選候選"]
    if result["filters"]:
        lines.append("條件：" + "；".join(result["filters"]))
    if not result["top"]:
        lines.append("目前沒有符合條件的候選股票。")
    for stock in result["top"]:
        lead = stock["lead"]
        behavior = stock.get("behavior") or {}
        live = ((behavior.get("same_stock") or {}).get("live_flow") or {})
        recent = (behavior.get("branch_recent") or {})
        tech = stock.get("technical") or {}
        vp = stock.get("volume_profile") or {}
        b = stock["score_breakdown"]
        medal = _MEDALS[stock["rank"] - 1] if stock["rank"] <= len(_MEDALS) else f"{stock['rank']}."
        lines.append("")
        lines.append(f"{medal} {stock['stock_code']} {stock.get('stock_name', '')}")
        lines.append(
            f"綜合分數：{stock['score']} / 100（事件 {b['event_performance_score']}｜近期 {b['recent_branch_behavior_score']}｜"
            f"金額 {b['warrant_amount_score']}｜技術 {b['technical_score']}｜支撐 {b['support_score']}）"
        )
        lines.append(
            f"【權證】{lead['branch']} 本次事件買進 {lead['event_buy_amount_text']}"
            + (f"，近5日淨額 {live.get('net_buy_5d_text')}" if live.get("has_trades") else "")
            + f"；本次符合 {'、'.join(lead['triggered_events'])} 事件"
        )
        for code, m in lead["event_performance"].items():
            lines.append("　" + _event_line(code, m))
        overall = lead["overall"]
        lines.append(f"　總勝率（背景）：{overall.get('raw_win_rate', '-')}%｜n={_count_text(overall.get('included_count'))}")
        if stock["high_quality_branch_count"] >= 2:
            others = [p["branch"] for p in stock["high_quality_pairs"] if p["branch"] != lead["branch"]][:3]
            lines.append(f"　其他高品質分點：{'、'.join(others)}")
        if behavior.get("found"):
            status = []
            if live.get("continuous_buying"):
                status.append("近5日持續加碼")
            if live.get("reducing_recently"):
                status.append("近期開始減碼")
            if live.get("direction_choppy"):
                status.append("買賣方向反覆")
            lines.append(f"【分點近期操作】{'、'.join(status) or '近5日沒有明顯加碼或減碼'}；{recent.get('recent_cases_sentence', '')}（僅供參考，不作為長期勝率）")
        if tech:
            ma20 = (tech.get("moving_averages") or {}).get("MA20") or {}
            lines.append(
                f"【技術面】{tech.get('data_date')} 收盤 {tech.get('close')}｜{tech.get('ma_alignment')}｜"
                f"MA20 {ma20.get('position')}（{ma20.get('distance_pct')}%）｜大量區型態：{vp.get('pattern_label', '-')}"
            )
        good = [_FLAG_TEXT[f] for f in stock["quality_flags"] if f in _POSITIVE_FLAGS]
        warn = [_FLAG_TEXT.get(f, f) for f in stock["quality_flags"] if f not in _POSITIVE_FLAGS]
        if good:
            lines.append("【優點】" + "、".join(good))
        if warn:
            lines.append("【注意】" + "、".join(warn))
    lines.append("")
    lines.append(
        f"資料時間：A～E 事件 {result['window_start']}～{result['window_end']}｜"
        f"勝率統計更新 {result['perf_sheet_updated_at'] or '時間未知'}｜股價為日K收盤資料"
    )
    lines.append("※ 本週精選是研究候選清單，不是買賣建議；最後由你自行判斷。")
    return "\n".join(lines)


# ============================================================
# 快取與對外入口
# ============================================================

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: Dict[str, Dict[str, Any]] = {}
_REFRESH_TOOL_CACHE_KEYS = (
    "abcde_event_rows", "known_branches", "sheet_勝率統計", "sheet_每日賣出明細",
    *(f"sheet_{title}" for title in tools.AMOUNT_CLASS_SHEETS.values()),
)


def _cache_path(config: WeeklyPickConfig, key: str) -> str:
    return os.path.join(config.cache_dir, f"weekly_pick_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}.json")


def _load_cache(config: WeeklyPickConfig, key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        item = _MEMORY_CACHE.get(key)
    if item is None:
        path = _cache_path(config, key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                item = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            print(f"⚠️ 本週精選快取讀取失敗：{path}｜{exc}")
            return None
    if time.time() - float(item.get("saved_at", 0)) > config.cache_seconds:
        return None
    return item


def _save_cache(config: WeeklyPickConfig, key: str, item: Dict[str, Any]) -> None:
    item = {**item, "saved_at": time.time()}
    with _CACHE_LOCK:
        _MEMORY_CACHE[key] = item
    path = _cache_path(config, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"⚠️ 本週精選快取寫入失敗：{path}｜{exc}")


def _clear_tool_caches() -> None:
    for key in _REFRESH_TOOL_CACHE_KEYS:
        tools.CACHE._data.pop(f"{tools.CACHE.namespace}_{key}", None)
    for full_key in list(tools.CACHE._data):
        if full_key.startswith(f"{tools.CACHE.namespace}_branch_event_perf_"):
            tools.CACHE._data.pop(full_key, None)


@dataclass
class WeeklyPickAnswer:
    text: str
    gemini_calls: int
    cache_hit: bool
    elapsed: float


def run_weekly_pick(
    question: str,
    generate: Callable[[str], Any],
    find_ungrounded: Callable[[str, Dict[str, Any]], List[str]],
    rate_limit_message: str,
    log: Callable[[str], None] = print,
    config: Optional[WeeklyPickConfig] = None,
) -> WeeklyPickAnswer:
    """Discord 入口：Python 算 TOP5 → 一次 Gemini 說明 → 數字核對；結果依資料日期快取。

    generate(prompt) 必須回傳具有 ok / text / rate_limited / error 屬性的物件。
    """
    started = time.perf_counter()
    config = config or WeeklyPickConfig()
    stage_log = StageLog(log)
    filters = parse_weekly_pick_filters(question)
    if filters.refresh:
        _clear_tool_caches()
        stage_log("refresh：清除事件表、勝率統計與本週精選快取")

    bundle = tools.load_abcde_event_rows()
    perf = tools.read_branch_event_performance()
    cache_key = "|".join([
        "weekly_pick",
        tools._fmt_date(bundle["latest_event_date"]),
        str(perf.get("sheet_updated_at", "")),
        filters.signature(),
        str(config.min_event_win_rate),
        str(config.top_n),
    ])
    cached = None if filters.refresh else _load_cache(config, cache_key)
    if cached and cached.get("text"):
        stage_log("快取命中（同一份事件資料與條件），不重新計算、不呼叫 Gemini")
        return WeeklyPickAnswer(cached["text"], 0, True, time.perf_counter() - started)

    engine = WeeklyPickEngine(config, log)
    result = cached["result"] if cached and cached.get("result") else engine.run(filters)
    rule_text = format_rule_based(result)
    if not result["top"]:
        _save_cache(config, cache_key, {"result": result, "text": rule_text})
        return WeeklyPickAnswer(rule_text, 0, False, time.perf_counter() - started)

    prompt, payload = build_gemini_prompt(result)
    stage_log(f"Gemini prompt {len(prompt):,} 字（TOP{len(result['top'])}，1 次呼叫）")
    response = generate(prompt)
    calls = 1
    if not getattr(response, "ok", False):
        stage_log(f"Gemini 失敗：{getattr(response, 'error', '')}")
        prefix = rate_limit_message if getattr(response, "rate_limited", False) else "AI 說明暫時無法使用，以下先提供系統計算結果。"
        _save_cache(config, cache_key, {"result": result, "text": ""})
        return WeeklyPickAnswer(f"{prefix}\n\n{rule_text}", calls, False, time.perf_counter() - started)
    text = str(response.text or "").strip()
    ungrounded = find_ungrounded(text, payload)
    if ungrounded:
        stage_log(f"數字核對未通過：{ungrounded[:10]}")
        _save_cache(config, cache_key, {"result": result, "text": ""})
        return WeeklyPickAnswer(f"（AI 說明中有數字無法對應到原始資料，改顯示系統計算結果）\n\n{rule_text}", calls, False, time.perf_counter() - started)
    text += (
        f"\n\n資料時間：A～E 事件 {result['window_start']}～{result['window_end']}｜"
        f"勝率統計更新 {result['perf_sheet_updated_at'] or '時間未知'}｜股價為日K收盤資料"
        "\n※ 本週精選是研究候選清單，不是買賣建議；最後由你自行判斷。"
    )
    _save_cache(config, cache_key, {"result": result, "text": text})
    stage_log(f"完成｜Gemini {calls} 次｜總耗時 {time.perf_counter() - started:.1f}s")
    return WeeklyPickAnswer(text, calls, False, time.perf_counter() - started)
