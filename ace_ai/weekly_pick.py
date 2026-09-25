"""艾斯 AI｜本週精選候選股（私人研究用）。

定位：先從 Google Sheet 權證事件中找出近 60 個交易日仍有未出清權證大戶部位的普通股，
所有分點以同一套規則比較；精選五分點只標記、不加分。排除 ETF／非普通股與預設 2330。

排名：技術面 50 ＋ 權證面 50。權證面＝本次事件／精確複合事件歷史績效 20
＋有效權證部位金額 15 ＋ 持倉／操作狀態 15。0～60 日只決定資格，不做新鮮度扣分。
勝率優先使用 Sheet 已存在的精確複合事件（如 A+C）；找不到時才退回單事件資料，
總勝率只保留為背景資訊。

操作流程：先以「本週精選排名」取得 Top10；管理員再指定「3034 幫我生成週精選文字」，
人工修改確認後輸入「這版確認，生成圖片」。排名階段 0 次 Gemini，AI 只負責文字解讀與改寫。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter
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

    event_window_trading_days: int = tools._env_int("WEEKLY_PICK_EVENT_WINDOW_DAYS", 60)
    min_event_win_rate: float = tools._env_float("WEEKLY_PICK_MIN_EVENT_WIN_RATE", 60.0)
    min_event_sample: int = tools._env_int("WEEKLY_PICK_MIN_EVENT_SAMPLE", 10)
    shortlist_size: int = tools._env_int("WEEKLY_PICK_SHORTLIST_SIZE", 0)  # 0＝所有符合資格股票公平進入完整評分
    # 本週精選固定排除的股票代號（逗號分隔），預設排除 2330。
    exclude_codes: Tuple[str, ...] = tuple(c.strip() for c in os.getenv("WEEKLY_PICK_EXCLUDE_CODES", "2330").split(",") if c.strip())
    top_n: int = tools._env_int("WEEKLY_PICK_TOP_N", 10)
    recent_behavior_days: int = tools._env_int("WEEKLY_PICK_RECENT_BEHAVIOR_DAYS", 30)
    recent_case_count: int = tools._env_int("WEEKLY_PICK_RECENT_CASE_COUNT", 10)
    # 預設關閉：每檔候選即時抓 MoneyDJ 近20日流水很吃記憶體，Railway 會 out of memory。
    live_flow_enable: bool = os.getenv("WEEKLY_PICK_LIVE_FLOW_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
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


WEEKLY_PICK_TRIGGERS = ("本週精選", "本周精選", "本州精選", "這週精選", "這周精選", "每週精選", "每周精選",
                        "精選股票", "精選個股", "精選候選")
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
    """整理「分點 × 股票」目前仍有效的這一輪事件與歷史績效。

    排名優先使用 Sheet 已存在的「精確複合事件」統計，例如目前未出清事件為 A、C，
    且勝率統計存在 A+C，就直接採 A+C；只有找不到精確組合時，才退回單事件加權。
    已出清的舊事件不拿來湊目前事件組合，避免把不同輪布局錯組成 A+C。
    """
    records = perf["branches"].get(branch, {})
    active = pair_events[pair_events.get("status", "") != "已出清"].copy() if "status" in pair_events else pair_events.copy()
    # 每週精選的定位是找「權證大戶仍在場」的股票；已全數出清的 pair 留作紀錄但不進候選。
    active_pair = not active.empty
    basis = active if active_pair else pair_events.sort_values("event_date").tail(1)
    amount_by_code = basis.groupby("event_code")["buy_amount"].sum() if not basis.empty else pd.Series(dtype=float)
    total_amount = float(amount_by_code.sum()) if len(amount_by_code) else 0.0
    triggered = [c for c in tools.EVENT_CODES if c in amount_by_code.index]
    combo_key = "+".join(triggered)

    matched: Dict[str, Dict[str, Any]] = {}
    for code in triggered:
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

    exact = records.get(combo_key) if combo_key and "+" in combo_key else None

    def weighted(key: str) -> Optional[float]:
        pairs = [(m["amount_share"], _f(m.get(key))) for m in matched.values()]
        pairs = [(w, v) for w, v in pairs if v is not None and w > 0]
        weight = sum(w for w, _ in pairs)
        return sum(w * v for w, v in pairs) / weight if weight > 0 else None

    if exact:
        raw = _f(exact.get("raw_win_rate"))
        adjusted = _f(exact.get("adjusted_win_rate"))
        included = _f(exact.get("included_count"))
        weighted_return = _f(exact.get("weighted_return"))
        avg_holding_days = _f(exact.get("avg_holding_days"))
        unresolved_ratio = _f(exact.get("unresolved_ratio"))
        performance_source = "exact_combo"
        performance_key = combo_key
        exact_payload = {
            "raw_win_rate": exact.get("raw_win_rate"),
            "adjusted_win_rate": exact.get("adjusted_win_rate"),
            "included_count": tools._num(exact.get("included_count"), 0),
            "event_count": tools._num(exact.get("event_count"), 0),
            "unresolved_count": tools._num(exact.get("unresolved_count"), 0),
            "weighted_return": exact.get("weighted_return"),
            "avg_holding_days": exact.get("avg_holding_days"),
            "unresolved_ratio": exact.get("unresolved_ratio"),
        }
    else:
        raw = weighted("raw_win_rate")
        adjusted = weighted("adjusted_win_rate")
        included = weighted("included_count")
        weighted_return = weighted("weighted_return")
        avg_holding_days = weighted("avg_holding_days")
        unresolved_ratio = weighted("unresolved_ratio")
        performance_source = "single_event_fallback" if len(triggered) > 1 else "single_event"
        performance_key = combo_key or (triggered[0] if triggered else "")
        exact_payload = {}

    overall = records.get("overall") or {}
    dominant = max(matched, key=lambda c: matched[c]["amount_share"]) if matched else ""
    return {
        "branch": branch,
        "stock_code": stock_code,
        "active_pair": active_pair,
        "triggered_events": triggered,
        "event_combo_key": combo_key,
        "performance_key": performance_key,
        "performance_source": performance_source,
        "exact_combo_performance": exact_payload,
        "dominant_event": dominant,
        "event_dates": sorted({tools._fmt_date(d) for d in basis["event_date"]}) if not basis.empty else [],
        "event_buy_amount": total_amount,
        "event_buy_amount_text": tools._money_text(total_amount),
        "event_performance": matched,
        "matched_raw_win_rate": _round(raw, 2),
        "matched_adjusted_win_rate": _round(adjusted, 2),
        "matched_included_count": _round(included, 1),
        "matched_weighted_return": _round(weighted_return, 2),
        "matched_avg_holding_days": _round(avg_holding_days, 2),
        "matched_unresolved_ratio": _round(unresolved_ratio, 3),
        "overall": {
            "raw_win_rate": overall.get("raw_win_rate"),
            "adjusted_win_rate": overall.get("adjusted_win_rate"),
            "included_count": tools._num(overall.get("included_count"), 0),
            "event_count": tools._num(overall.get("event_count"), 0),
            "unresolved_count": tools._num(overall.get("unresolved_count"), 0),
            "weighted_return": overall.get("weighted_return"),
        },
        "primary": bool(raw is not None and raw >= min_win_rate),
        "has_history": bool(exact or any(not m.get("missing_history") for m in matched.values())),
        "is_selected_five": branch in set(tools.selected_five_branches()),
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
    return round(min(25.0, score), 2), reasons


def score_warrant_amount(stock: Dict[str, Any], lead_live: Dict[str, Any]) -> Tuple[float, List[str]]:
    """權證買超／持有金額（原始 25 分）：金額 20＋多分點共振 5。

    不因最後買超距今 41～60 日而扣分；時間只作為資訊，不是排名因子。
    事件勝率已在另一個分項計算，這裡不再用「高勝率分點金額」重複加權。
    """
    total = float(stock.get("event_buy_amount_total") or 0.0)
    branch_count = int(stock.get("active_branch_count") or len(stock.get("pairs") or []))
    amount_part = 20.0 * _clip01(math.log10(max(total, 1.0) / 1e6) / math.log10(50.0))
    resonance = 0.0 if branch_count <= 1 else 3.0 if branch_count == 2 else 5.0
    score = amount_part + resonance
    reasons = [
        f"目前有效事件買進合計 {tools._money_text(total)} → {amount_part:.1f}",
        f"仍在場分點 {branch_count} 家 → {resonance:.1f}",
    ]
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
    """從既有 calculate_indicators 結果取出評分序列；盤中開啟時使用今日暫定 K。"""
    bundle = tools._load_price_bundle(stock_code)
    df = bundle["df"] if tools.LIVE_PATTERN_SCORE and bundle.get("intraday") else tools.closed_frame(bundle)
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


# ============================================================
# 型態評分（100 分制；本週精選與一般問答共用）
# 五大項各自從 0 分算到滿分，全部滿分＝100；同一件事只在一個項目計分。
#   換算權重：量區結構 25｜下方支撐 25｜均線趨勢 25｜價格位置 15｜布林 10（量區＋支撐合計 50）
# ============================================================

PATTERN_GRADES = ((75.0, "結構偏強"), (60.0, "中性偏多"), (45.0, "結構中性"), (30.0, "中性偏弱"), (0.0, "結構偏弱"))
PATTERN_WEIGHT = 50.0  # 技術面合計 50；不讓型態或權證任何單一面向極端主導
EVENT_WEIGHT, AMOUNT_WEIGHT, RECENT_WEIGHT = 20.0, 15.0, 15.0  # 權證面合計 50：事件績效／部位金額／持倉操作狀態
# 各項原始滿分（細項加總用）與換算後權重（型態分數 100 分）：以型態與大量區支撐為主要依據。
PATTERN_COMPONENTS = (("均線趨勢", 30), ("價格位置", 20), ("量區結構", 25), ("下方支撐", 15), ("布林", 10))
PATTERN_COMPONENT_WEIGHTS = {"均線趨勢": 25, "價格位置": 15, "量區結構": 25, "下方支撐": 25, "布林": 10}


def weekly_technical_score(pattern_score_100: Any) -> float:
    """把唯一的 100 分型態評分等比例換算成週精選技術面 50 分。

    重要：週精選不得再維護第二套技術評分。一般個股的型態評分與本週精選
    必須共用 score_pattern()；此函式只做 100 -> 50 的線性換算。
    """
    value = _f(pattern_score_100)
    if value is None:
        return 0.0
    value = max(0.0, min(100.0, value))
    return round(value * PATTERN_WEIGHT / 100.0, 2)


def _direction_points(d: Dict[str, Any], full: float) -> Tuple[float, str]:
    """均線方向＋扣抵推算：上揚且扣抵後不轉彎＝滿分；上揚但將轉下彎＝一半以下；下彎＝0。"""
    now, turn, day = d.get("direction_now"), d.get("turn"), d.get("turn_day")
    if not now:
        return full / 2, "資料不足，給一半"
    if now == "上揚":
        if turn == "轉下彎":
            return round(full * 0.4, 1), f"上揚，但扣抵價偏高，收盤不變{tools.turn_phrase(turn, day)}"
        return full, "上揚，扣抵後仍續揚"
    if now == "下彎":
        if turn == "轉上揚":
            return round(full * 0.5, 1), f"下彎，但扣抵價偏低，收盤不變{tools.turn_phrase(turn, day)}"
        return 0.0, "下彎" + ("，且在股價上方形成壓力" if d.get("ma_above_close") else "")
    if turn == "轉上揚":
        return round(full * 0.7, 1), f"走平，收盤不變{tools.turn_phrase(turn, day)}"
    if turn == "轉下彎":
        return round(full * 0.2, 1), f"走平，收盤不變{tools.turn_phrase(turn, day)}"
    return full / 2, "走平"


def score_pattern(tech: Dict[str, Any], vp: Dict[str, Any], extras: Dict[str, Any], config: WeeklyPickConfig) -> Dict[str, Any]:
    """回傳 {score, components, items, marks}；items 每筆＝（項目, 小項, 得分, 滿分, 說明）。"""
    marks = {"extended": False, "overhead": False}
    items: List[Dict[str, Any]] = []

    def add(component: str, label: str, points: float, maximum: float, note: str) -> None:
        items.append({"component": component, "label": label, "points": round(max(0.0, min(maximum, points)), 1),
                      "max": maximum, "note": note})

    # ---------- 均線趨勢 30：排列 12＋MA20 方向 10＋MA60 方向 8 ----------
    mas = tech.get("moving_averages") or {}
    values = {k: _f((mas.get(k) or {}).get("value")) for k in ("MA5", "MA10", "MA20", "MA60")}
    alignment = tech.get("ma_alignment", "")
    if alignment == "多頭排列":
        add("均線趨勢", "均線排列", 12, 12, "多頭排列 MA5>MA10>MA20>MA60")
    elif alignment == "空頭排列":
        add("均線趨勢", "均線排列", 0, 12, "空頭排列 MA5<MA10<MA20<MA60")
    elif None not in (values["MA5"], values["MA10"], values["MA20"]) and values["MA5"] > values["MA10"] > values["MA20"]:
        add("均線趨勢", "均線排列", 8, 12, "短期多頭 MA5>MA10>MA20，MA60 尚未排好")
    elif None not in (values["MA5"], values["MA10"], values["MA20"]) and values["MA5"] < values["MA10"] < values["MA20"]:
        add("均線趨勢", "均線排列", 2, 12, "短期空頭 MA5<MA10<MA20")
    elif alignment == "資料不足":
        add("均線趨勢", "均線排列", 6, 12, "均線資料不足，給一半")
    elif all((mas.get(k) or {}).get("position") == "站上" for k in ("MA5", "MA10", "MA20", "MA60")) and values["MA20"] > values["MA60"]:
        # 短均線只差一點沒排好，但股價站上所有均線且月線在季線之上，結構接近多頭，不該跟真正糾結同分。
        add("均線趨勢", "均線排列", 8, 12, "站上所有均線，MA20 在 MA60 之上，短均線尚未排齊")
    else:
        add("均線趨勢", "均線排列", 4, 12, "均線糾結")
    deduction = tech.get("ma_deduction") or {}
    for key, full in (("MA20", 10), ("MA60", 8)):
        points, note = _direction_points(deduction.get(key) or {}, full)
        add("均線趨勢", f"{key} 方向", points, full, f"{key} {note}")

    # ---------- 價格位置 20：距 MA20 12＋站上 MA60 4＋追高風險 4 ----------
    dist20 = _f((mas.get("MA20") or {}).get("distance_pct"))
    dist60 = _f((mas.get("MA60") or {}).get("distance_pct"))
    if dist20 is None:
        add("價格位置", "距 MA20", 6, 12, "MA20 資料不足，給一半")
    elif 0 <= dist20 <= config.near_ma20_pct:
        add("價格位置", "距 MA20", 12, 12, f"在 MA20 上方 {dist20:.1f}%，貼近月線")
    elif config.near_ma20_pct < dist20 <= config.extended_ma20_pct:
        add("價格位置", "距 MA20", 8, 12, f"在 MA20 上方 {dist20:.1f}%，稍有乖離")
    elif dist20 > config.extended_ma20_pct:
        marks["extended"] = True
        add("價格位置", "距 MA20", 3, 12, f"在 MA20 上方 {dist20:.1f}%，乖離過大")
    elif dist20 >= -3:
        add("價格位置", "距 MA20", 5, 12, f"跌破 MA20 {abs(dist20):.1f}%，仍在月線附近")
    else:
        add("價格位置", "距 MA20", 0, 12, f"跌破 MA20 {abs(dist20):.1f}%")
    if dist60 is None:
        add("價格位置", "MA60", 2, 4, "MA60 資料不足，給一半")
    else:
        add("價格位置", "MA60", 4 if dist60 >= 0 else 0, 4, f"{'站上' if dist60 >= 0 else '跌破'} MA60（{dist60:+.1f}%）")
    chase, notes = 4.0, []
    bb = tech.get("bollinger") or {}
    percent_b = _f(bb.get("percent_b"))
    ret5 = _f(extras.get("return_5d_pct"))
    if ret5 is not None and ret5 > config.surge_5d_pct:
        chase -= 2
        marks["extended"] = True
        notes.append(f"近5日已漲 {ret5:.1f}%")
    if percent_b is not None and percent_b > 105:
        chase -= 2
        marks["extended"] = True
        notes.append(f"布林 %B {percent_b:.0f} 衝出上軌")
    if extras.get("heavy_volume_long_black"):
        chase = 0
        notes.append("爆量長黑")
    add("價格位置", "追高風險", chase, 4, "、".join(notes) if notes else "沒有急漲、衝出上軌或爆量長黑")

    # ---------- 量區結構 25：相對兩大量區 10＋最大量區事件 9＋上方量區壓力 6 ----------
    # 依「離量區多遠」給分，不再全有全無：貼著量區下緣測試，和深跌在量區下方不同分。
    close = _f(vp.get("close")) or _f(tech.get("close"))
    overhead = None
    for key in ("maximum_volume_zone", "second_volume_zone"):
        zone = vp.get(key) or {}
        low = _f(zone.get("price_low"))
        if close and low and low > close:
            gap = (low / close - 1) * 100
            if overhead is None or gap < overhead[0]:
                overhead = (gap, zone.get("label") or "大量區", low)
    position = str(vp.get("position_vs_two_zones", ""))
    if "之上" in position:
        add("量區結構", "相對兩大量區", 10, 10, "收盤在兩大量區之上")
    elif "之間" in position:
        add("量區結構", "相對兩大量區", 6, 10, "收盤在兩大量區之間")
    elif "之下" in position and overhead and overhead[0] <= 3:
        add("量區結構", "相對兩大量區", 4, 10, f"收盤在兩大量區之下，但距{overhead[1]}下緣只有 {overhead[0]:.1f}%，正在測試量區")
    elif "之下" in position and overhead and overhead[0] <= config.support_zone_pct:
        add("量區結構", "相對兩大量區", 2, 10, f"收盤在兩大量區之下，距{overhead[1]}下緣 {overhead[0]:.1f}%")
    elif "之下" in position:
        add("量區結構", "相對兩大量區", 0, 10, "收盤在兩大量區之下且距離較遠")
    else:
        add("量區結構", "相對兩大量區", 5, 10, "量區位置資料不足，給一半")
    relation = str((vp.get("maximum_volume_zone") or {}).get("close_relation", ""))
    max_low = _f((vp.get("maximum_volume_zone") or {}).get("price_low"))
    max_gap = (max_low / close - 1) * 100 if close and max_low and max_low > close else None
    if "上方" in relation:
        points, note = 5, "站在最大量區上方"
        if vp.get("recent_breakout"):
            points, note = 7, "近期突破最大量區並站穩"
            if vp.get("retest_after_breakout"):
                points, note = 9, "近期突破最大量區，回踩未破"
        add("量區結構", "最大量區", points, 9, note)
    elif "量區內" in relation:
        add("量區結構", "最大量區", 4, 9, "在最大量區內整理")
    elif "下方" in relation and (vp.get("rebound_test_after_breakdown") or (max_gap is not None and max_gap <= 3)):
        add("量區結構", "最大量區", 3, 9, f"在最大量區下方，已反彈到量區附近（距下緣 {max_gap:.1f}%）" if max_gap is not None else "跌破最大量區後反彈測試中")
    elif "下方" in relation:
        add("量區結構", "最大量區", 0, 9, "近期跌破最大量區且未站回" if vp.get("recent_breakdown") else "在最大量區下方")
    else:
        add("量區結構", "最大量區", 4.5, 9, "最大量區資料不足，給一半")
    # 上方壓力只看「壓力有多近」，和上面兩項是不同面向；貼近量區仍給少量分數，不重複歸零。
    if overhead is None:
        add("量區結構", "上方量區壓力", 6, 6, "上方沒有大量區")
    elif overhead[0] <= config.overhead_zone_pct:
        marks["overhead"] = True
        add("量區結構", "上方量區壓力", 2, 6, f"上方{overhead[1]} {overhead[2]:g} 只差 {overhead[0]:.1f}%，有壓力")
    else:
        add("量區結構", "上方量區壓力", 4, 6, f"上方{overhead[1]} {overhead[2]:g} 距離 {overhead[0]:.1f}%")

    # ---------- 下方支撐 15：最近支撐距離 11＋8% 內支撐數 4 ----------
    supports = []
    if close:
        for label, level in (("MA10", values["MA10"]), ("MA20", values["MA20"]), ("MA60", values["MA60"])):
            if level and level <= close:
                supports.append(((close / level - 1) * 100, label))
        for key in ("maximum_volume_zone", "second_volume_zone"):
            zone = vp.get(key) or {}
            low, high = _f(zone.get("price_low")), _f(zone.get("price_high"))
            if low is None or high is None:
                continue
            if high <= close:
                supports.append(((close / high - 1) * 100, f"{zone.get('label') or '大量區'}上緣"))
            elif low <= close:
                supports.append((0.0, f"{zone.get('label') or '大量區'}（股價在區內）"))
    if supports:
        gap, label = min(supports)
        points = 11 if gap <= 3 else 9 if gap <= 5 else 6 if gap <= config.support_zone_pct else 3 if gap <= 12 else 1
        add("下方支撐", "最近支撐", points, 11, f"最近支撐 {label} 在下方 {gap:.1f}%")
        near = sorted({lb for g, lb in supports if g <= config.support_zone_pct})
        count_points = 4 if len(near) >= 3 else 2 if len(near) == 2 else 0
        add("下方支撐", "支撐密度", count_points, 4,
            f"下方 {config.support_zone_pct:g}% 內有 {len(near)} 道支撐" + (f"（{'、'.join(near)}）" if near else ""))
    else:
        add("下方支撐", "最近支撐", 0, 11, "下方沒有均線或大量區支撐")
        add("下方支撐", "支撐密度", 0, 4, "下方沒有支撐")

    # ---------- 布林 10：開布林 7＋位置 3（中軌方向已算在 MA20，不重複） ----------
    # 開布林＝帶寬擴張（5 日帶寬增加 ≥10%，本專案規則）；向上開口時股價延續性較強，向下開口為 0。
    bb_position = str(bb.get("position", ""))
    walk, squeeze_break, width_trend = bb.get("band_walk"), bb.get("squeeze_breakout"), bb.get("width_trend")
    above_mid = bb_position in ("位於中軌與上軌之間", "收盤位於上軌外", "突破上軌")
    width_change = _f(bb.get("width_change_5d_pct"))
    width_text = f"（5 日帶寬 {width_change:+.1f}%）" if width_change is not None else ""
    if squeeze_break == "壓縮後向上突破":
        add("布林", "開布林", 7, 7, "壓縮後向上突破，布林向上開口")
    elif width_trend == "擴張" and above_mid and (walk == "沿上軌" or (_f(bb.get("percent_b")) or 0) >= 80):
        add("布林", "開布林", 7, 7, f"布林向上開口且股價貼著上軌{width_text}")
    elif width_trend == "擴張" and above_mid:
        add("布林", "開布林", 5, 7, f"布林開口，股價在中軌上方{width_text}")
    elif walk == "沿下軌" or squeeze_break == "壓縮後向下跌破" or (width_trend == "擴張" and not above_mid):
        add("布林", "開布林", 0, 7, f"布林向下開口{width_text}")
    elif width_trend in ("收窄", "持平") or bb.get("squeeze"):
        add("布林", "開布林", 3, 7, f"布林未開口（帶寬{'壓縮' if bb.get('squeeze') else width_trend}）{width_text}")
    else:
        add("布林", "開布林", 3.5, 7, "布林資料不足，給一半")
    bb_points = {"位於中軌與上軌之間": 3, "收盤位於上軌外": 3, "突破上軌": 3, "位於下軌與中軌之間": 1}.get(bb_position, 0)
    add("布林", "通道位置", bb_points if bb_position not in ("", "資料不足") else 1.5, 3, bb_position or "資料不足")

    components = []
    for name, maximum in PATTERN_COMPONENTS:
        raw = sum(i["points"] for i in items if i["component"] == name)
        weight = PATTERN_COMPONENT_WEIGHTS[name]
        components.append({"label": name, "value": round(raw / maximum * weight, 1), "max": weight})
    return {
        "score": round(sum(c["value"] for c in components), 1),
        "components": components,
        "items": items,
        "marks": marks,
    }



def pattern_reason_lists(items: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """得分依據＝拿到一半以上的小項；失分原因＝拿不到一半的小項（每個小項只出現一次）。"""
    good, bad = [], []
    for item in items:
        text = f"{item['note']}（{item['label']} {item['points']:g}/{item['max']:g}）"
        (good if item["points"] >= item["max"] / 2 else bad).append(text)
    return good, bad


def pattern_grade(score: float) -> str:
    return next(label for floor, label in PATTERN_GRADES if score >= floor)


def _branch_status(row: Dict[str, Any]) -> str:
    sells = row.get("reduce_or_exit_lookback") or []
    holding = int(row.get("open_event_count") or 0)
    total = int(row.get("event_count_lookback") or 0)
    pct = row.get("remaining_pct_estimate")
    if holding and total:
        text = f"持有中 {holding}/{total} 筆"
        if pct is not None and sells:
            text += f"・估剩約 {pct:.0f}%"
        return text
    if holding:
        return "持有中・近期有賣出" if sells else "持有中"
    if total or row.get("events_recent") or row.get("event_buy_amount_lookback_text") not in (None, "", "-"):
        return "已全部出清"
    return "只有賣出紀錄"


def build_pattern_scorecard(
    tech: Dict[str, Any],
    vp: Dict[str, Any],
    extras: Dict[str, Any],
    chips: Optional[Dict[str, Any]] = None,
    cost_price: Optional[float] = None,
    config: Optional[WeeklyPickConfig] = None,
    tracked_branch_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """一般問答的型態評分卡：分數規則與本週精選相同（score_pattern）；只評技術結構，不含權證籌碼、不是買賣建議。"""
    config = config or WeeklyPickConfig()
    pattern = score_pattern(tech, vp, extras, config)
    try:
        import local_market_cache
        local_market_cache.save_pattern_score(
            str(tech.get("stock_code") or vp.get("stock_code") or ""),
            str(tech.get("data_date") or vp.get("data_date") or ""),
            pattern["score"], pattern_grade(pattern["score"]), pattern.get("components"),
            str(tech.get("signal_status") or ""),
        )
    except Exception:
        pass
    good, bad = pattern_reason_lists(pattern["items"])
    levels = tools.key_price_levels(tech, vp)
    close = levels["close"]
    branches = []
    chip_rows = list((chips or {}).get("branches") or [])
    if tracked_branch_names is None:
        # 一般個股分析維持原邏輯：顯示高勝率追蹤分點與精選五分點。
        high, selected = tools.display_branch_set()
        shown = [row for row in chip_rows if row.get("branch") in high | selected]
    else:
        # 週精選最終圖片：只顯示管理員最後確認文字中「實際提到」的分點。
        # 不因金額、勝率、精選標記自動補其他分點，避免圖文不一致。
        wanted = list(dict.fromkeys(str(x).strip() for x in tracked_branch_names if str(x).strip()))
        by_name = {str(row.get("branch", "")).strip(): row for row in chip_rows}
        shown = [by_name[name] for name in wanted if name in by_name]
    for row in shown[:5]:
        events = row.get("events_recent") or []
        last = events[-1] if events else {}
        sells = row.get("reduce_or_exit_lookback") or []
        branches.append({
            "branch": row.get("branch", ""),
            "is_high_win_rate": bool(row.get("is_high_win_rate")),
            "overall_win_rate": row.get("overall_win_rate_background"),
            "latest_event": (f"{last.get('event')} {str(last.get('event_date', ''))[5:]} 買 {last.get('buy_amount_text', '')}"
                             if last else "區間內無 A～E 事件"),
            "event_buy_amount_text": row.get("event_buy_amount_lookback_text", "-"),
            "status": _branch_status(row),
            "latest_sell": (f"{sells[-1]['date'][5:]} {sells[-1]['action']} {sells[-1]['sell_amount_text']}" if sells else ""),
        })
    card = {
        "stock_code": tech.get("stock_code") or vp.get("stock_code", ""),
        "data_date": tech.get("data_date"),
        "pattern_score": pattern["score"],
        "grade": pattern_grade(pattern["score"]),
        "components": pattern["components"],
        "plus_reasons": good,
        "minus_reasons": bad,
        "pattern_label": vp.get("pattern_label") or "型態資料不足",
        "ma_alignment": tech.get("ma_alignment", ""),
        "ma_deduction": tech.get("ma_deduction") or {},
        "score_basis": tech.get("signal_status") or "收盤確認",
        "intraday_changes": ((tech.get("intraday_observation") or {}).get("changes") or [])[:4],
        "close": close,
        "resistances_above_close": levels["resistances"],
        "supports_below_close": levels["supports"],
        "tracked_branches": branches,
        "tracked_branches_period": (chips or {}).get("period_lookback", ""),
        # 有分點資料才顯示；沒有就整段隱藏，原因只寫 Log，不在圖片補註解。
        "show_tracked_branches": bool(branches),
        "method": "型態分數 100 分＝量區結構 25＋下方支撐 25＋均線趨勢 25＋價格位置 15＋布林 10（量區與支撐合計 50），規則與本週精選相同；只評技術結構，不含籌碼，不是買賣建議",
    }
    if cost_price:
        card["cost_price"] = tools._num(cost_price)
        card["unrealized_pct"] = tools._pct(close, cost_price)
    return card


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
        common = window["stock_code"].astype(str).str.fullmatch(r"[1-9]\d{3}")
        if (~common).any():
            self.log(f"排除 ETF／非普通股：{'、'.join(sorted(window.loc[~common, 'stock_code'].astype(str).unique())[:10])}"
                     f"｜{window.loc[~common, 'stock_code'].nunique()} 檔")
            window = window[common]
        if config.exclude_codes:
            window = window[~window["stock_code"].isin(config.exclude_codes)]
            self.log(f"固定排除：{'、'.join(config.exclude_codes)}｜剩 {window['stock_code'].nunique():,} 檔")
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
            if not pair.get("active_pair"):
                continue  # 已完全出清的舊布局不算「權證大戶仍在場」
            stock = stocks.setdefault(pair["stock_code"], {"stock_code": pair["stock_code"], "pairs": []})
            stock["pairs"].append(pair)
        for stock in stocks.values():
            ranked = sorted(stock["pairs"], key=lambda p: (p["event_score"], p["event_buy_amount"]), reverse=True)
            stock["pairs"] = ranked
            high_quality = [
                p for p in ranked
                if p["primary"] and (p["matched_included_count"] or 0) >= config.min_event_sample
            ]
            stock["lead"] = ranked[0]
            stock["high_quality_pairs"] = high_quality
            stock["event_buy_amount_total"] = float(sum(p["event_buy_amount"] for p in ranked))
            stock["active_branch_count"] = len(ranked)
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
        """所有符合資格股票原則上都進完整評分，避免先用權證條件預排序造成候選偏誤。

        WEEKLY_PICK_SHORTLIST_SIZE=0（預設）代表不截斷；若部署資源有限，可由環境變數設定安全上限。
        """
        if self.config.shortlist_size and self.config.shortlist_size > 0:
            for stock in pool:
                stock["pre_amount_score"], _ = score_warrant_amount(stock, {})
                stock["pre_score"] = stock["lead"]["event_score"] + stock["pre_amount_score"]
            ranked = sorted(pool, key=lambda s: s["pre_score"], reverse=True)
            chosen = ranked[: max(self.config.top_n, self.config.shortlist_size)]
            self.log(f"部署安全上限啟用：{len(pool)} → {len(chosen)} 檔進完整評分")
            return chosen
        self.log(f"公平比較模式：{len(pool)} 檔全部進入完整技術＋權證評分")
        return list(pool)

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
        # 各項原始分數照舊計算，再換算成新權重（技術面 50＋事件績效 20＋權證金額 15＋持倉操作 15＝100）。
        recent_raw, reasons["recent_branch_behavior"] = score_recent_behavior(behavior, config)
        amount_raw, reasons["warrant_amount"] = score_warrant_amount(stock, lead_live)
        reasons["event_performance"] = lead["event_score_reasons"]
        breakdown["event_performance_score"] = round(lead["event_score"] * EVENT_WEIGHT / 25, 2)
        breakdown["recent_branch_behavior_score"] = round(recent_raw * RECENT_WEIGHT / 10, 2)
        breakdown["warrant_amount_score"] = round(amount_raw * AMOUNT_WEIGHT / 25, 2)
        # 型態評分（100 分制，與一般問答共用）換算成 50 分計入綜合分數。
        pattern: Dict[str, Any] = {}
        if tech and vp:
            pattern = score_pattern(tech, vp, extras, config)
            breakdown["pattern_score"] = weekly_technical_score(pattern["score"])
            good, bad = pattern_reason_lists(pattern["items"])
            reasons["pattern"] = good + bad
            marks = pattern["marks"]
        else:
            breakdown["pattern_score"], reasons["pattern"], marks = 0.0, ["技術資料取得失敗"], {"extended": False, "overhead": False}
        total = round(sum(breakdown.values()), 1)
        pattern_score_100 = round(float(pattern.get("score") or 0.0), 1)
        technical_score_50 = round(float(breakdown.get("pattern_score") or 0.0), 2)
        warrant_score_50 = round(
            float(breakdown.get("event_performance_score") or 0.0)
            + float(breakdown.get("warrant_amount_score") or 0.0)
            + float(breakdown.get("recent_branch_behavior_score") or 0.0),
            2,
        )
        support_points = next((c["value"] for c in pattern.get("components", []) if c["label"] == "下方支撐"), 0.0)

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
        if (lead["event_score"] >= 18 and pattern.get("score", 0) < 40) or (
            lead_live.get("has_trades") and (_f(lead_live.get("net_buy_5d")) or 0) <= 0
        ):
            flags.add("conflicting_signals")
        if support_points >= 11:
            flags.add("strong_support")
        if stock["high_quality_branch_count"] >= 2:
            flags.add("multi_branch_confirmation")

        stock.update({
            "score": total,
            "score_breakdown": breakdown,
            # 單一來源：型態 100 分與週精選技術 50 分是同一套規則，只做等比例換算。
            "pattern_score_100": pattern_score_100,
            "technical_score_50": technical_score_50,
            "warrant_score_50": warrant_score_50,
            "score_reasons": reasons,
            "pattern": pattern,
            "quality_flags": sorted(flags),
            "behavior": behavior,
            "technical": tech,
            "volume_profile": vp,
            "technical_extras": extras,
            "selected_five_branches": [p["branch"] for p in stock.get("pairs", []) if p.get("is_selected_five")],
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
            f"   event_performance_score = {b['event_performance_score']} / {EVENT_WEIGHT:g}｜recent_branch_behavior_score = {b['recent_branch_behavior_score']} / {RECENT_WEIGHT:g}｜"
            f"warrant_amount_score = {b['warrant_amount_score']} / {AMOUNT_WEIGHT:g}｜pattern_score = {b['pattern_score']} / {PATTERN_WEIGHT:g}"
            f"（型態 {(stock.get('pattern') or {}).get('score')} / 100）｜total = {stock['score']} / 100"
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
        "score_summary": {
            "pattern_score_100": stock.get("pattern_score_100", (stock.get("pattern") or {}).get("score")),
            "technical_score_50": stock.get("technical_score_50", stock["score_breakdown"].get("pattern_score", 0)),
            "warrant_score_50": stock.get("warrant_score_50", round(
                float(stock["score_breakdown"].get("event_performance_score", 0) or 0)
                + float(stock["score_breakdown"].get("warrant_amount_score", 0) or 0)
                + float(stock["score_breakdown"].get("recent_branch_behavior_score", 0) or 0), 2)),
        },
        "branch": {
            "name": lead["branch"],
            "event_buy_amount_in_window_text": lead["event_buy_amount_text"],
            "net_buy_5d_text": live.get("net_buy_5d_text") if live.get("has_trades") else None,
            "event_dates": lead["event_dates"],
            "triggered_events": lead["triggered_events"],
            "performance_key": lead.get("performance_key"),
            "performance_source": lead.get("performance_source"),
            "exact_combo_performance": lead.get("exact_combo_performance") or {},
            "event_performance": {c: _perf_brief(m) for c, m in lead["event_performance"].items()},
            "matched_weighted": {
                "raw_win_rate": lead["matched_raw_win_rate"],
                "adjusted_win_rate": lead["matched_adjusted_win_rate"],
                "sample_included": lead["matched_included_count"],
                "weighted_return": lead["matched_weighted_return"],
                "avg_holding_days": lead.get("matched_avg_holding_days"),
            },
            "overall_background": lead["overall"],
            "is_selected_five": lead.get("is_selected_five", False),
        },
        "other_high_quality_branches": [
            {
                "name": p["branch"],
                "triggered_events": p["triggered_events"],
                "performance_key": p.get("performance_key"),
                "performance_source": p.get("performance_source"),
                "exact_combo_performance": p.get("exact_combo_performance") or {},
                "event_performance": {c: _perf_brief(m) for c, m in (p.get("event_performance") or {}).items()},
                "matched_raw_win_rate": p.get("matched_raw_win_rate"),
                "matched_adjusted_win_rate": p.get("matched_adjusted_win_rate"),
                "sample_included": p.get("matched_included_count"),
                "weighted_return": p.get("matched_weighted_return"),
                "avg_holding_days": p.get("matched_avg_holding_days"),
                "overall_background": p.get("overall") or {},
                "event_buy_amount_text": p.get("event_buy_amount_text"),
                "event_dates": p.get("event_dates") or [],
                "active_pair": p.get("active_pair"),
                "is_selected_five": p.get("is_selected_five", False),
            }
            for p in (stock.get("pairs") or stock.get("high_quality_pairs") or []) if p.get("branch") != lead["branch"]
        ],
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
            "bollinger": tech.get("bollinger"),
            "return_5d_pct": (stock.get("technical_extras") or {}).get("return_5d_pct"),
        },
        "volume_profile": {
            "maximum_volume_zone": {k: (vp.get("maximum_volume_zone") or {}).get(k) for k in ("price_low", "price_high", "close_relation")},
            "second_volume_zone": {k: (vp.get("second_volume_zone") or {}).get(k) for k in ("price_low", "price_high", "close_relation")},
            "position_vs_two_zones": vp.get("position_vs_two_zones"),
            "pattern_label": vp.get("pattern_label"),
            "recent_maximum_zone_event": vp.get("recent_maximum_zone_event"),
        },
        "pattern_score_100": (stock.get("pattern") or {}).get("score"),
        "pattern_grade": pattern_grade((stock.get("pattern") or {}).get("score") or 0),
        "pattern_components": (stock.get("pattern") or {}).get("components"),
        "pattern_reasons": (stock.get("score_reasons") or {}).get("pattern"),
        "quality_notes": [_FLAG_TEXT.get(f, f) for f in stock["quality_flags"]],
    }


WEEKLY_PICK_SYSTEM_PROMPT = """你是我的私人台股研究助理。
你的工作不是推薦我買股票，而是協助我找出「本週最值得進一步研究、最適合撰寫 Discord 本週精選週報的候選股票」。
Top 10 已由 Python 依分數排好（型態＋大量區支撐占 50 分為主要依據，事件績效、權證金額、近期操作為輔），你只負責解釋，不得更改排名、不得新增或刪除股票。
why_for_report 先說型態與大量區支撐的理由，再補充籌碼。
只能根據 tool_results 提供的資料回答，不得自行補充不存在的數據（股價、勝率、分點、金額、均線、大量區、新聞都一樣）。

每檔股票的判讀順序（一定照這個順序思考與撰寫）：
1. 型態：先用 volume_profile.pattern_label 與 recent_maximum_zone_event 判斷目前價格型態好壞。
2. 大量區與均線：現價相對第一／第二大量區的位置、是否有支撐或壓力；均線排列、MA20／MA60 位置與距離。
3. 布林：technical.bollinger 的位置、帶寬變化、是否沿軌（壓縮不預測方向，影線穿越不等於收盤突破）。
4. 權證籌碼：本次事件的修正勝率與樣本數優先，其次加權報酬；總勝率（overall_background）只能當背景。
5. 分點近期操作：只能當參考；提到近期案例時必須同時寫出已有結果筆數、勝敗與仍未完成筆數。

寫作規則：
- 全部使用繁體中文。嚴禁輸出任何英文欄位名稱或程式代碼（例如 adjusted_win_rate、quality_flags、unresolved 等），需要時改用中文說法（修正勝率、樣本數、未完成案例）。
- quality_notes 是已翻成中文的提醒，負面項目寫進 cautions，正面項目可寫進 strengths。
- 總勝率高但本次事件勝率偏弱、或總勝率普通但本次事件表現佳時，要在 warrant 中點出。
- 未完成案例不能算成功；不要把買超等同看多必漲；不要把高歷史勝率說成這次一定成功；不提供目標價或報酬預測。
- 金額沿用資料中的「萬／億」文字，數字只能使用資料中出現過的數值。
- 每個欄位 1～2 句、口語清楚；strengths 與 cautions 各 1～3 點、每點 25 字以內；headline 18 字以內。

只輸出符合 JSON Schema 的 JSON：
overview：一句話總結這份 Top 10 的共同特徵（40 字以內）
candidates：依 TOP5 順序，每檔包含 stock_code、headline、pattern、volume_and_ma、bollinger、warrant、branch_behavior、strengths、cautions、why_for_report。"""


WEEKLY_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "stock_code": {"type": "string"},
                    "headline": {"type": "string"},
                    "pattern": {"type": "string"},
                    "volume_and_ma": {"type": "string"},
                    "bollinger": {"type": "string"},
                    "warrant": {"type": "string"},
                    "branch_behavior": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "cautions": {"type": "array", "items": {"type": "string"}},
                    "why_for_report": {"type": "string"},
                },
                "required": ["stock_code", "headline", "pattern", "volume_and_ma", "bollinger", "warrant",
                             "branch_behavior", "strengths", "cautions", "why_for_report"],
            },
        },
    },
    "required": ["overview", "candidates"],
}

CARD_TEXT_FIELDS = ("headline", "pattern", "volume_and_ma", "bollinger", "warrant", "branch_behavior", "why_for_report")
_ENGLISH_CODE_RE = re.compile(r"[（(]?\s*\b[a-z]+(?:_[a-z0-9]+)+\b\s*[）)]?")


def strip_english_codes(text: str) -> str:
    """保險：移除 AI 誤抄的英文欄位代碼（例如「（unresolved_cases_high）」）。"""
    cleaned = _ENGLISH_CODE_RE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


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


_KEEP_EMPTY_KEYS = {"quality_flags", "quality_notes", "triggered_events"}


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
        raw_pattern = stock.get("pattern_score_100", (stock.get("pattern") or {}).get("score", 0))
        technical_50 = stock.get("technical_score_50", b.get("pattern_score", 0))
        warrant_50 = stock.get("warrant_score_50", round(
            float(b.get("event_performance_score", 0) or 0)
            + float(b.get("warrant_amount_score", 0) or 0)
            + float(b.get("recent_branch_behavior_score", 0) or 0), 2))
        lines.append(
            f"綜合分數：{stock['score']} / 100（技術 {technical_50:g}/50＝型態 {float(raw_pattern or 0):g}/100 × 0.5｜權證 {warrant_50:g}/50）"
        )
        lines.append(
            f"　權證細項：事件 {b['event_performance_score']}/{EVENT_WEIGHT:g}｜部位 {b['warrant_amount_score']}/{AMOUNT_WEIGHT:g}｜"
            f"持倉操作 {b['recent_branch_behavior_score']}/{RECENT_WEIGHT:g}"
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
            lines.append("【布林觀察】" + "；".join((tech.get("bollinger") or {}).get("signals") or ["資料不足"]))
        good = [_FLAG_TEXT[f] for f in stock["quality_flags"] if f in _POSITIVE_FLAGS]
        warn = [_FLAG_TEXT.get(f, f) for f in stock["quality_flags"] if f not in _POSITIVE_FLAGS]
        if good:
            lines.append("【優點】" + "、".join(good))
        if warn:
            lines.append("【注意】" + "、".join(warn))
    lines.append("")
    lines.append(
        f"資料時間：近 60 個交易日權證事件 {result['window_start']}～{result['window_end']}｜"
        f"勝率統計更新 {result['perf_sheet_updated_at'] or '時間未知'}｜股價為日K收盤資料"
    )
    lines.append("※ 本週精選是研究候選清單，不是買賣建議；最後由你自行判斷。")
    return "\n".join(lines)


# ============================================================
# 卡片資料（圖片版面用）
# ============================================================

SCORE_PARTS = (
    ("pattern_score", "技術面", PATTERN_WEIGHT),
    ("event_performance_score", "事件績效", EVENT_WEIGHT),
    ("warrant_amount_score", "權證部位", AMOUNT_WEIGHT),
    ("recent_branch_behavior_score", "持倉操作", RECENT_WEIGHT),
)


def mark_branches(stock: Dict[str, Any]) -> List[str]:
    """候選股可用的權證分點清單；正式週精選圖片仍會再依最終文章內容過濾。"""
    names = [stock["lead"]["branch"]] + [p["branch"] for p in stock.get("pairs") or []]
    return list(dict.fromkeys(names))[:4]


def mentioned_branches(draft: str, stock: Dict[str, Any]) -> List[str]:
    """回傳「最終週精選文字中實際提到」的候選分點，保持候選資料原順序。

    週精選圖片的 K 線流水標記與「追蹤分點動向」都只能使用這份清單；
    不會因其他分點買超更大、勝率更高或屬精選五分點而自動補入。
    """
    text = str(draft or "")
    candidates = [stock.get("lead", {}).get("branch", "")] + [p.get("branch", "") for p in stock.get("pairs") or []]
    candidates = list(dict.fromkeys(name for name in candidates if name))
    return [name for name in candidates if name in text]


def card_facts(stock: Dict[str, Any]) -> Dict[str, Any]:
    """排名卡固定數據；精選五分點只標記、不加分。"""
    lead = stock["lead"]
    tech = stock.get("technical") or {}
    vp = stock.get("volume_profile") or {}
    ma20 = (tech.get("moving_averages") or {}).get("MA20") or {}
    event_lines = []
    exact = lead.get("exact_combo_performance") or {}
    if exact:
        event_lines.append(
            f"{lead.get('performance_key')} 組合｜勝率 {exact.get('raw_win_rate')}%（修正 {exact.get('adjusted_win_rate')}%）｜樣本 {_count_text(exact.get('included_count'))}"
        )
    else:
        for code, m in lead["event_performance"].items():
            if m.get("raw_win_rate") is None and m.get("adjusted_win_rate") is None:
                continue
            event_lines.append(f"{code} 事件｜勝率 {m.get('raw_win_rate')}%（修正 {m.get('adjusted_win_rate')}%）｜樣本 {_count_text(m.get('included_count'))}")
    selected = bool(lead.get("is_selected_five"))
    return {
        "rank": stock["rank"],
        "stock_code": stock["stock_code"],
        "stock_name": stock.get("stock_name", ""),
        "score": stock["score"],
        "pattern_score_100": stock.get("pattern_score_100", (stock.get("pattern") or {}).get("score")),
        "technical_score_50": stock.get("technical_score_50", stock["score_breakdown"].get("pattern_score", 0)),
        "warrant_score_50": stock.get("warrant_score_50", round(
            float(stock["score_breakdown"].get("event_performance_score", 0) or 0)
            + float(stock["score_breakdown"].get("warrant_amount_score", 0) or 0)
            + float(stock["score_breakdown"].get("recent_branch_behavior_score", 0) or 0), 2)),
        "score_parts": [
            {"label": label, "value": stock["score_breakdown"].get(key, 0), "max": maximum}
            for key, label, maximum in SCORE_PARTS
        ],
        "pattern_label": vp.get("pattern_label") or "型態資料不足",
        "ma_alignment": tech.get("ma_alignment") or "-",
        "ma20_text": f"MA20 {ma20.get('position', '-')} {ma20.get('distance_pct')}%" if ma20.get("distance_pct") is not None else "",
        "lead_branch": lead["branch"],
        "lead_branch_selected": selected,
        "lead_branch_label": ("⭐ " if selected else "") + lead["branch"],
        "lead_amount_text": lead["event_buy_amount_text"],
        "event_combo_key": lead.get("performance_key") or "+".join(lead.get("triggered_events") or []),
        "event_performance_source": lead.get("performance_source"),
        "event_win_rate": lead.get("matched_raw_win_rate"),
        "event_adjusted_win_rate": lead.get("matched_adjusted_win_rate"),
        "event_sample": lead.get("matched_included_count"),
        "triggered_events": lead["triggered_events"],
        "event_lines": event_lines,
        "other_branches": [(("⭐ " if p.get("is_selected_five") else "") + p["branch"]) for p in stock.get("pairs") or [] if p["branch"] != lead["branch"]][:3],
        "mark_branches": mark_branches(stock),
        "data_date": tech.get("data_date", ""),
    }


def rule_card_text(stock: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini 不可用時的中文卡片內容（只整理數據，不做 AI 解讀）。"""
    lead = stock["lead"]
    tech = stock.get("technical") or {}
    vp = stock.get("volume_profile") or {}
    mas = tech.get("moving_averages") or {}
    ma20, ma60 = mas.get("MA20") or {}, mas.get("MA60") or {}
    mz = vp.get("maximum_volume_zone") or {}
    behavior = stock.get("behavior") or {}
    recent = behavior.get("branch_recent") or {}
    flags = stock.get("quality_flags") or []
    events = "、".join(lead["triggered_events"])
    return {
        "headline": f"{vp.get('pattern_label') or '型態待確認'}｜{events} 事件",
        "pattern": f"目前型態為「{vp.get('pattern_label') or '資料不足'}」；{vp.get('recent_maximum_zone_event') or '近期沒有明確穿越最大量區'}。",
        "volume_and_ma": (
            f"{vp.get('position_vs_two_zones') or '大量區位置資料不足'}；最大量區 {mz.get('price_low', '-')}～{mz.get('price_high', '-')}。"
            f"均線{tech.get('ma_alignment', '資料不足')}，MA20 {ma20.get('position', '-')}（{ma20.get('distance_pct', '-')}%），"
            f"MA60 {ma60.get('position', '-')}（{ma60.get('distance_pct', '-')}%）。"
        ),
        "bollinger": "；".join((tech.get("bollinger") or {}).get("signals") or ["布林資料不足"]) + "。",
        "warrant": f"{lead['branch']} 本次事件買進 {lead['event_buy_amount_text']}，符合 {events} 事件。",
        "branch_behavior": (recent.get("recent_cases_sentence") or "近期操作資料不足。") + "（近期案例僅供參考）",
        "strengths": [_FLAG_TEXT[f] for f in flags if f in _POSITIVE_FLAGS],
        "cautions": [_FLAG_TEXT.get(f, f) for f in flags if f not in _POSITIVE_FLAGS],
        "why_for_report": f"綜合分數 {stock['score']}，型態、支撐與權證事件都有可追蹤的內容。",
    }


def build_cards(result: Dict[str, Any], ai: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """合併 Python 事實與 AI 文字；AI 少給或給錯股票時該檔改用系統整理內容。"""
    ai_by_code = {}
    for item in (ai or {}).get("candidates") or []:
        if isinstance(item, dict) and item.get("stock_code"):
            ai_by_code[str(item["stock_code"]).strip()] = item
    cards = []
    for stock in result["top"]:
        facts = card_facts(stock)
        text = rule_card_text(stock)
        item = ai_by_code.get(stock["stock_code"])
        source = "rule"
        if item:
            source = "ai"
            for key in CARD_TEXT_FIELDS:
                if str(item.get(key) or "").strip():
                    text[key] = strip_english_codes(item[key])
            for key in ("strengths", "cautions"):
                values = [strip_english_codes(v) for v in (item.get(key) or []) if str(v).strip()]
                if values:
                    text[key] = values[:3]
        cards.append({**facts, **text, "text_source": source})
    overview = strip_english_codes((ai or {}).get("overview") or "")
    return cards, overview


def cards_ai_text(cards: List[Dict[str, Any]], overview: str) -> str:
    """把 AI 寫的文字串起來做數字核對。"""
    parts = [overview]
    for card in cards:
        if card.get("text_source") != "ai":
            continue
        parts += [str(card.get(k, "")) for k in CARD_TEXT_FIELDS] + list(card.get("strengths") or []) + list(card.get("cautions") or [])
    return "\n".join(p for p in parts if p)


def cards_to_text(cards: List[Dict[str, Any]], overview: str, meta: Dict[str, Any]) -> str:
    """文字版（CLI／圖片失敗時備用），順序與圖片一致。"""
    lines = ["艾斯 AI｜本週精選候選"]
    if overview:
        lines.append(overview)
    for card in cards:
        lines += [
            "",
            f"#{card['rank']} {card['stock_code']} {card['stock_name']}｜綜合分數 {card['score']} / 100",
            f"【型態】{card['pattern']}",
            f"【大量區與均線】{card['volume_and_ma']}",
            f"【布林】{card['bollinger']}",
            f"【權證籌碼】{card['warrant']}",
            *[f"　{line}" for line in card["event_lines"]],
            f"　{card['overall_line']}",
            f"【分點近期操作】{card['branch_behavior']}",
        ]
        if card.get("strengths"):
            lines.append("【優點】" + "；".join(card["strengths"]))
        if card.get("cautions"):
            lines.append("【注意】" + "；".join(card["cautions"]))
        lines.append(f"【適合週報的原因】{card['why_for_report']}")
    lines += ["", meta.get("data_time", ""), meta.get("disclaimer", "")]
    return "\n".join(line for line in lines if line is not None)


def ranking_to_text(cards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    lines = ["📊 權證分點觀察｜本週精選 Top 10"]
    for card in cards:
        star = "⭐" if card.get("lead_branch_selected") else ""
        event = card.get("event_combo_key") or "-"
        win = card.get("event_win_rate")
        sample = card.get("event_sample")
        win_text = f"{win}% / n={_count_text(sample)}" if win is not None else "事件勝率資料不足"
        parts = {p["label"]: p["value"] for p in card.get("score_parts") or []}
        lines.append(
            f"{card['rank']}. {card['stock_code']} {card.get('stock_name','')}｜{card['score']:.1f}分｜"
            f"技術 {parts.get('技術面',0):.1f}/50｜權證 {max(0.0, float(card.get('score') or 0)-parts.get('技術面',0)):.1f}/50｜"
            f"{star}{card.get('lead_branch','')}｜{event} {win_text}｜{card.get('lead_amount_text','')}"
        )
    if meta.get("data_time"):
        lines += ["", meta["data_time"]]
    return "\n".join(lines)


def ranking_card(cards: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    """本週精選排名圖卡（和其他研究筆記同一套）：Top 10 表格＋一行資料時間。精選五分點用 ★（字型有這個字形，不用 emoji）。"""
    rows = []
    for card in cards:
        parts = {p["label"]: p["value"] for p in card.get("score_parts") or []}
        tech = float(parts.get("技術面", 0) or 0)
        score = float(card.get("score") or 0)
        win = card.get("event_win_rate")
        event = card.get("event_combo_key") or "-"
        win_text = f"{event} {win}%（n={_count_text(card.get('event_sample'))}）" if win is not None else "資料不足"
        rows.append([str(card["rank"]), f"{card['stock_code']} {card.get('stock_name', '')}", f"{score:.1f}", f"{tech:.1f}",
                     f"{max(0.0, score - tech):.1f}", ("★" if card.get("lead_branch_selected") else "") + str(card.get("lead_branch", "")),
                     win_text, str(card.get("lead_amount_text", ""))])
    return {"branch": "本週精選 Top 10", "tags": ["權證分點觀察"], "label": "排名", "sections": [
        {"type": "table", "columns": ["#", "股票", "總分", "技術/50", "權證/50", "主力分點", "事件勝率", "權證金額"], "rows": rows,
         "widths": (0.05, 0.15, 0.08, 0.09, 0.09, 0.17, 0.24, 0.13), "accent": ("總分",), "signed": ("權證金額",)},
        {"type": "note", "text": "★＝精選五分點｜" + str(meta.get("data_time", ""))}]}


def weekly_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filters": result.get("filters") or [],
        "data_time": (
            f"資料時間：近 60 個交易日權證事件 {result['window_start']}～{result['window_end']}｜"
            f"勝率統計更新 {result['perf_sheet_updated_at'] or '時間未知'}｜股價為日K收盤資料"
        ),
        "disclaimer": "※ 本週精選是研究候選清單，不是買賣建議；最後由你自行判斷。",
    }


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
    stock_codes: List[str] = field(default_factory=list)
    cards: List[Dict[str, Any]] = field(default_factory=list)
    overview: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    notice: str = ""


def rank_cache_key(config: "WeeklyPickConfig", filters: "WeeklyPickFilters") -> str:
    """排名快取鍵值：事件表、勝率統計、行情日期都沒變且條件相同時，草稿與排名共用同一份結果。
    行情日期（本地底庫最新交易日＋今天）納入鍵值：收盤行情更新後技術分數會變，不能沿用舊排名。"""
    bundle = tools.load_abcde_event_rows()
    perf = tools.read_branch_event_performance()
    try:
        import local_market_cache
        bar_day = (local_market_cache.known_dates(1) or [""])[0]
    except Exception:
        bar_day = ""
    return "|".join([
        "weekly_pick_rank_v9", tools._fmt_date(bundle["latest_event_date"]), str(perf.get("sheet_updated_at", "")),
        bar_day, tools.taipei_now().strftime("%Y-%m-%d"),
        filters.signature(), str(config.event_window_trading_days), str(config.top_n),
    ])


def run_weekly_pick(
    question: str,
    generate: Callable[..., Any],
    find_ungrounded: Callable[[str, Dict[str, Any]], List[str]],
    rate_limit_message: str,
    log: Callable[[str], None] = print,
    config: Optional[WeeklyPickConfig] = None,
) -> WeeklyPickAnswer:
    """Discord 排名入口：純 Python 排出 Top 10，不呼叫 Gemini、不自動生成長篇週報。"""
    started = time.perf_counter()
    config = config or WeeklyPickConfig()
    stage_log = StageLog(log)
    filters = parse_weekly_pick_filters(question)
    if filters.refresh:
        _clear_tool_caches()
        stage_log("refresh：清除事件表、勝率統計與本週精選快取")
    cache_key = rank_cache_key(config, filters)
    cached = None if filters.refresh else _load_cache(config, cache_key)
    if cached and cached.get("result"):
        result = cached["result"]
        stage_log("排名快取命中，不重新計算")
        cache_hit = True
    else:
        result = WeeklyPickEngine(config, log).run(filters)
        _save_cache(config, cache_key, {"result": result, "ai_ok": True})
        cache_hit = False
    cards, _ = build_cards(result, None)
    meta = weekly_meta(result)
    text = ranking_to_text(cards, meta)
    return WeeklyPickAnswer(
        text=text, gemini_calls=0, cache_hit=cache_hit, elapsed=time.perf_counter()-started,
        stock_codes=[c["stock_code"] for c in cards], cards=cards, overview="", meta=meta, notice="",
    )


# ============================================================
# 管理員週精選文字編輯流程
# ============================================================

_WEEKLY_STYLE_FILE = os.path.join(os.path.dirname(__file__), "weekly_style_examples.txt")


def load_weekly_style_examples(max_chars: int = 9000) -> str:
    """載入使用者過往週精選文案，僅作語氣／結構參考。"""
    try:
        with open(_WEEKLY_STYLE_FILE, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return ""
    return text[:max(1000, int(max_chars))]


WEEKLY_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}},
    "required": ["draft"],
}


WEEKLY_DRAFT_SYSTEM_PROMPT = """你是「權證分點觀察｜週精選」文字編輯助理。
你的任務是根據 fact_data 寫出一篇可直接貼到 Discord 的週精選草稿，文風要貼近 style_examples，但所有客觀數字與權證分點資料只能來自 fact_data；admin_notes 是管理員自己補充的事實，可自然融入文章。

寫作原則：
1. 使用繁體中文、自然口語，像長期觀察台股與權證分點的人在寫自己的投資筆記；不要像制式研究報告、客服或把欄位逐項念完。
2. 開頭固定兩行：
   權證分點觀察｜週精選 📌
   📌 股票代號 股票名稱
3. 不使用固定模板硬塞所有指標。先從資料中挑出「這檔目前最值得講的 3～5 件事」再寫；沒有辨識度的資訊可以完全省略。
4. 技術面要有選擇性：
   - 股價接近大量區、剛突破/回踩大量區時，大量區應成為文章重點並寫出相關價位。
   - 均線排列、扣抵、布林、量能若沒有特別訊號，不必每篇都寫。
   - 若某一項才是這檔最重要的技術特徵，可以多寫一點，不必維持固定順序。
5. 權證分點也是依重要性選材。文章提到幾個分點，就可以分別寫各自的事件勝率、樣本、平均持有天數、加權報酬或目前部位，但只挑對這篇有意義的數據，不需要每個欄位全部列出。
6. 若有精確複合事件統計（例如 A+C），優先使用該組合。若只有單事件資料可參考，直接用自然文字說明是哪幾個事件的歷史表現，不要寫「依單一事件資料綜合觀察」這種系統語句，也不能假裝有不存在的複合事件統計。
7. 總勝率只作背景；若本次事件勝率更有代表性，就以本次事件為主。
8. 精選五分點只代表標記，不代表比較高分，不要寫成因為是精選分點所以更好。
9. 只有 fact_data 或 admin_notes 真的有資料時，才能提到外資、投信、法人、現股分點、族群或週 K；沒有就不要補。
10. 主文通常 1 個緊湊段落、約 4～7 句，可依資料增減。常見但非固定的結構是「最重要的技術位置 → 權證分點 → 歷史績效 → 後續觀察」。
11. 語氣可使用「後續可以觀察／持續留意／短線不必急著追價」等筆記式說法，但不要保證漲跌、寫目標價或把歷史勝率當成未來機率。
12. 結尾維持過往寫法：
   ⚠️ 僅為個人投資筆記
   🧡 非任何買賣建議
13. 若 instruction 是修改要求，必須延續 previous_draft 的內容與語氣，只改使用者指定的地方；例如「把華南永昌台中的勝率與平均持有補上」就真的補進去，不要把整篇重寫成另一套模板。
14. admin_notes 若有管理員補充的外資、法人或現股籌碼資訊，可以自然放進最適合的位置；不得延伸出 admin_notes 沒說的數字。

只輸出 JSON：{"draft":"完整草稿"}。"""


WEEKLY_REVISION_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}, "changed": {"type": "string"}},
    "required": ["draft"],
}

WEEKLY_REVISION_SYSTEM_PROMPT = """你是「權證分點觀察｜週精選」文字編輯。現在是**修改既有草稿**，不是重寫一篇新文章。

最重要的三條：
1. 以 current_draft 為準，**沒有被指示到的句子必須一字不改**照抄回來（包含開頭兩行、結尾兩行、標點與換行）。
2. instruction 說什麼就改什麼，而且一定要真的改到；例如「不要提到均線」就把均線那句整句刪掉、「短一點」就刪掉次要句子、「補上某分點的勝率」就從 fact_data 找該分點的數字寫進去。
3. 數字只能來自 fact_data 或 admin_notes；找不到資料就在 changed 說明「資料沒有這項」，不要自己編，也不要改動其他數字。

其他規則：
- 不要因為這次修改就把文章改回制式模板、也不要重新排列段落順序。
- 不新增 instruction 沒要求的內容；不寫目標價、不保證漲跌。
- 結尾固定保留：⚠️ 僅為個人投資筆記 / 🧡 非任何買賣建議。

輸出 JSON：{"draft":"修改後的完整文章","changed":"一句話說明這次改了什麼"}。"""


def build_weekly_revision_prompt(candidate: Dict[str, Any], previous_draft: str, instruction: str,
                                 admin_notes: Optional[List[str]] = None, strict: bool = False) -> Tuple[str, Dict[str, Any]]:
    """修改既有草稿用：不送風格範例（前一版本身就是風格），只送事實與指令，token 約為重寫的三分之一。"""
    facts = candidate_payload(candidate)
    notes = [str(x).strip() for x in (admin_notes or []) if str(x).strip()]
    payload = {
        "current_draft": str(previous_draft or "").strip(),
        "instruction": str(instruction or "").strip(),
        "admin_notes": notes,
        "fact_data": tools_prune(facts),
    }
    extra = ("\n\n上一次的修改因為出現對不上原始資料的數字而被退回，"
             "這次除了 instruction 指定的地方以外，其他數字請原封不動照抄 current_draft。") if strict else ""
    prompt = (WEEKLY_REVISION_SYSTEM_PROMPT + extra + "\n\ninput_data（JSON）：\n"
              + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return prompt, {"fact_data": facts, "admin_notes": notes, "instruction": str(instruction or "").strip(),
                    "previous_draft": str(previous_draft or "").strip()}


def build_weekly_draft_prompt(candidate: Dict[str, Any], instruction: str = "", previous_draft: str = "", admin_notes: Optional[List[str]] = None) -> Tuple[str, Dict[str, Any]]:
    facts = candidate_payload(candidate)
    style = load_weekly_style_examples()
    notes = [str(x).strip() for x in (admin_notes or []) if str(x).strip()]
    payload = {
        "fact_data": tools_prune(facts),
        "admin_notes": notes,
        "instruction": str(instruction or "").strip(),
        "previous_draft": str(previous_draft or "").strip(),
    }
    prompt = (
        WEEKLY_DRAFT_SYSTEM_PROMPT
        + "\n\nstyle_examples（只學語氣與結構，不可抄其中舊數字）：\n"
        + style
        + "\n\ninput_data（JSON）：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return prompt, {"fact_data": facts, "admin_notes": notes, "instruction": str(instruction or "").strip()}


def find_weekly_candidate(stock_code: str, log: Callable[[str], None] = print, config: Optional[WeeklyPickConfig] = None) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """依目前 Top10 規則找指定股票；只允許對當期排名候選生成正式週精選草稿。

    排名很貴（近百檔完整評分），所以先吃當期快取；快取沒有才重算。
    """
    config = config or WeeklyPickConfig()
    filters = WeeklyPickFilters()
    cache_key = rank_cache_key(config, filters)
    cached = _load_cache(config, cache_key)
    result = (cached or {}).get("result") if isinstance(cached, dict) else None
    if not isinstance(result, dict) or not result.get("top"):
        result = WeeklyPickEngine(config, log).run(filters)
        _save_cache(config, cache_key, {"result": result, "ai_ok": True})
    else:
        log("週精選草稿：沿用當期排名快取，未重新計算")
    code = tools.core()._normalize_stock_name_code_key(stock_code)
    stock = next((s for s in result.get("top") or [] if s.get("stock_code") == code), None)
    return stock, result


def weekly_article_parts(draft: str, stock_code: str, stock_name: str = "") -> Dict[str, Any]:
    """把草稿拆成圖片版面要用的三段：標題、內文、免責。

    圖片上方已經有「權證分點觀察｜週精選」標題，所以這裡不重複；
    免責兩行移到卡片底部，內文只留正文段落。
    """
    lines = [line.rstrip() for line in str(draft or "").splitlines()]
    code = str(stock_code or "").strip()
    body, disclaimers = [], []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            body.append("")
            continue
        if "權證分點觀察" in stripped and ("週精選" in stripped or "周精選" in stripped):
            continue
        if code and code in stripped and len(stripped) <= 24:
            continue                      # 「📌 3006 晶豪科」這種標頭
        if stripped in ("⚠️ 僅為個人投資筆記", "🧡 非任何買賣建議") or stripped.startswith(("⚠️", "🧡")):
            disclaimers.append(stripped)
            continue
        body.append(stripped)
    text = "\n".join(body).strip()
    title = f"{stock_name}（{code}）" if stock_name else code
    return {"title": title, "body": text, "disclaimers": disclaimers or ["⚠️ 僅為個人投資筆記", "🧡 非任何買賣建議"]}


# ============================================================
# 人工文章自動排版：Gemini 只重新分段、加固定小標、把數字整理成資料列；程式逐字核對，不通過就用原文排版
# ============================================================

LAYOUT_HEADINGS = ("技術面", "籌碼面", "操作觀察")
LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "heading": {"type": "string", "enum": list(LAYOUT_HEADINGS)},
            "paragraphs": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
        },
        "required": ["heading", "paragraphs"],
    }}},
    "required": ["sections"],
}
_LAYOUT_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LAYOUT_STRIP_RE = re.compile(r"[\s*`，。、；：:,.!?！？（）()「」『』【】《》〈〉…\-—～~｜|/／%％]+")
LAYOUT_LEFTOVER_RATIO = 0.12     # 扣掉段落與資料列後原文剩下的字（刪掉的開頭語、資料列改寫的名稱）最多占全文比例
LAYOUT_MAX_ROWS = 4
LAYOUT_MAX_LABEL = 14


def layout_prompt(body: str, retry_reason: str = "") -> str:
    retry = f"\n※ 上一次排版沒通過核對：{retry_reason}。請修正後重新輸出，仍然只能照抄原文。\n" if retry_reason else ""
    return (retry +
        "你是排版編輯。下面是一篇已經寫好的個股觀察文章，請只做排版，輸出 JSON。\n"
        f"1. 分成最多三段，小標只能用：{'、'.join(LAYOUT_HEADINGS)}（沒有內容的段落不要輸出），依原文順序。\n"
        "2. 段落內的句子必須逐字照抄原文，不可改寫、增減字詞、換同義詞或改數字；只能重新分段（太長拆開、零碎的合併）。\n"
        "3. 句首和小標意思重複的開頭語要刪掉，例如「技術面來看，」「權證籌碼方面，」「操作上，」。\n"
        f"4. 籌碼面裡的金額、勝率、持有天數這類數字，最多 {LAYOUT_MAX_ROWS} 個可以整理成 rows：label 是簡短名稱，"
        "value 只放數字和單位，例如 {\"label\":\"近70日4筆事件買進\",\"value\":\"約2,120萬元\"}、"
        "{\"label\":\"調整後勝率\",\"value\":\"約67%\"}；整理進 rows 的那一句要從段落刪掉，不要重複。\n"
        "5. 去掉 **、emoji 等格式符號；不要加任何原文沒有的內容。\n\n"
        f"原文：\n{body}"
    )


def _layout_norm(text: str) -> str:
    return _LAYOUT_STRIP_RE.sub("", _LAYOUT_NUMBER_RE.sub(lambda m: m.group().replace(",", ""), str(text or "")))


def _layout_numbers(texts) -> Counter:
    return Counter(n.replace(",", "") for t in texts for n in _LAYOUT_NUMBER_RE.findall(str(t or "")))


def parse_layout(data: Any) -> Optional[Dict[str, Any]]:
    """Gemini 回傳的排版結構正規化；格式不對回 None。"""
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        return None
    sections = []
    for sec in data["sections"]:
        if not isinstance(sec, dict) or sec.get("heading") not in LAYOUT_HEADINGS:
            return None
        paragraphs = [str(p).replace("**", "").strip() for p in sec.get("paragraphs") or [] if str(p).strip()]
        rows = [{"label": str(r.get("label") or "").strip(), "value": str(r.get("value") or "").replace("**", "").strip()}
                for r in sec.get("rows") or [] if isinstance(r, dict)]
        rows = [r for r in rows if r["label"] and r["value"]]
        if paragraphs or rows:
            sections.append({"heading": sec["heading"], "paragraphs": paragraphs, "rows": rows})
    return {"sections": sections} if sections else None


def verify_layout(body: str, layout: Dict[str, Any]) -> Tuple[bool, str]:
    """排版結果逐句核對：
    - 段落拆成子句（，；。），每個子句都要是原文逐字的片段（中間抽走一句到資料列可以，改寫、新增字不行）
    - 原文每個數字都要出現、不能有原文沒有的數字（重複可以，由 tidy_layout 去重）
    - 扣掉段落子句與資料列內容後，原文剩下沒被呈現的字不超過一定比例（不能整句丟掉）"""
    paragraphs = [p for s in layout["sections"] for p in s["paragraphs"]]
    rows = [r for s in layout["sections"] for r in s["rows"]]
    if len(rows) > LAYOUT_MAX_ROWS:
        return False, f"資料列 {len(rows)} 列，最多 {LAYOUT_MAX_ROWS} 列"
    long_label = next((r["label"] for r in rows if len(r["label"]) > LAYOUT_MAX_LABEL), "")
    if long_label:
        return False, f"資料列名稱太長：{long_label}"
    expected = set(_layout_numbers([body]))
    actual = set(_layout_numbers(paragraphs + [r["label"] for r in rows] + [r["value"] for r in rows]))
    if expected != actual:
        missing = sorted(expected - actual)[:3]
        extra = sorted(actual - expected)[:3]
        return False, f"數字和原文不一致（少了 {missing or '-'}，多了 {extra or '-'}）"
    original = _layout_norm(body)
    leftover = original
    for paragraph in paragraphs:
        for clause in _CLAUSE_RE.findall(paragraph):
            piece = _layout_norm(clause)
            if not piece:
                continue
            if piece not in original:
                return False, f"句子不是原文：{clause[:24]}"
            leftover = leftover.replace(piece, "", 1)
    for row in rows:                      # 搬到資料列的內容也算有呈現
        for part in (row["value"], row["label"]):
            piece = _layout_norm(part)
            if piece and piece in leftover:
                leftover = leftover.replace(piece, "", 1)
    if len(leftover) > max(30, len(original) * LAYOUT_LEFTOVER_RATIO):
        return False, f"有 {len(leftover)} 字原文沒有出現在排版結果：{leftover[:20]}"
    return True, ""


# 子句：以，；。或逗號分隔，但「2,120」這種數字中間的逗號不切
_CLAUSE_RE = re.compile(r"(?:[^，,；;。]|(?<=\d),(?=\d))+[，,；;。]?")


# 資料列內容結尾的「數字＋單位」：「調整後勝率約67%」→ 名稱「調整後勝率」、數值「約67%」
_ROW_TAIL_RE = re.compile(r"^(.*?)((?:約|近|達|共|逾)?[-+]?\d[\d,]*(?:\.\d+)?\s*(?:萬元|億元|元|萬張|萬|億|%|％|天|日|張|筆|倍|點|檔)?)$")


def _tidy_row(row: Dict[str, str]) -> Dict[str, str]:
    match = _ROW_TAIL_RE.match(row["value"].strip("，,；;。 "))
    if match and match.group(1).strip() and len(match.group(1).strip()) <= 14:
        return {"label": match.group(1).strip("，,；;的 "), "value": match.group(2).strip()}
    return row


def tidy_layout(layout: Dict[str, Any]) -> Dict[str, Any]:
    """去掉重複，但不遺失資訊：
    - 段落子句的數字都在某一列資料列上：子句文字完全被那一列涵蓋→刪子句；子句還有別的內容（例如「A、B、E事件的」）→保留子句、刪那一列
    - 最後資料列內容前面的文字改當名稱、只留數字＋單位"""
    rows = [(i, r) for i, sec in enumerate(layout["sections"]) for r in sec["rows"]]
    dropped_rows: Set[int] = set()
    sections = []
    for sec in layout["sections"]:
        paragraphs = []
        for paragraph in sec["paragraphs"]:
            kept = []
            for clause in _CLAUSE_RE.findall(paragraph):
                numbers = set(_layout_numbers([clause]))
                match = next((k for k, (_, r) in enumerate(rows) if numbers
                              and numbers <= set(_layout_numbers([r["label"], r["value"]]))), None)
                if match is None:
                    kept.append(clause)
                    continue
                row = rows[match][1]
                if _layout_norm(clause) in _layout_norm(row["label"] + row["value"]) or \
                        _layout_norm(clause) in _layout_norm(row["value"]):
                    continue                  # 資料列已完整呈現這一句
                kept.append(clause)
                dropped_rows.add(match)       # 子句資訊比資料列多：留子句、不重複列資料列
            text = "".join(kept).strip()
            if text and text[-1] in "，,；;":
                text = text[:-1] + "。"
            if text:
                paragraphs.append(text)
        sections.append(dict(sec, paragraphs=paragraphs, rows=[]))
    for k, (i, row) in enumerate(rows):
        if k not in dropped_rows:
            sections[i]["rows"].append(_tidy_row(row))
    return {"sections": [s for s in sections if s["paragraphs"] or s["rows"]]}


_LEAD_IN_RE = re.compile(r"^(?:權證籌碼方面|權證籌碼上|權證籌碼|權證方面|籌碼方面|籌碼面|籌碼上|技術面上|技術面|技術上|"
                         r"型態上|型態面|操作建議|操作方面|操作面|操作上)(?:來看|而言|方面|部分|上)?[，,：:、\s]*")
# 小標關鍵字與權重：「突破」「整理」「操作」這類各段都會出現的通用詞權重低
_HEADING_WORDS = {
    "技術面": {"均線": 1, "型態": 1, "布林": 1, "季線": 1, "月線": 1, "年線": 1, "K線": 1, "扣抵": 1, "趨勢": 1,
            "盤整": 1, "震盪": 1, "量能": 1, "大量區": 1, "翻揚": 1, "下彎": 1, "上揚": 1, "多頭": 1, "空頭": 1,
            "長紅": 1, "長黑": 1, "突破": 0.5, "整理": 0.5},
    "籌碼面": {"分點": 1, "權證": 1, "籌碼": 1, "買進": 1, "買超": 1, "賣超": 1, "勝率": 1, "持有": 1, "事件": 1,
            "部位": 1, "主力": 1, "外資": 1, "投信": 1, "法人": 1, "加碼": 1, "減碼": 1, "追打": 1, "布局": 1, "佈局": 1},
    "操作觀察": {"追價": 1.5, "追高": 1.5, "拉回": 1.5, "回檔": 1.5, "觀察": 1.5, "停損": 1.5, "跌破": 1.5, "進場": 1.5,
             "出場": 1.5, "等待": 1.5, "可等": 1.5, "不急": 1.5, "失效": 1.5, "失敗": 1.5, "風險": 1.5, "留意": 1.5,
             "注意": 1.5, "操作": 0.5},
}
_ROW_WORDS = ("買進", "買超", "賣超", "賣出", "勝率", "持有", "報酬", "金額", "張數")
_ROW_LABEL_TRIM = re.compile(r"^(?:目前|該分點|其中|共|而|並)+|的$")


def _heading_scores(text: str) -> Dict[str, float]:
    return {h: sum(text.count(w) * weight for w, weight in words.items()) for h, words in _HEADING_WORDS.items()}


def _heading_of(text: str) -> str:
    scores = _heading_scores(text)
    best = max(LAYOUT_HEADINGS, key=lambda h: (scores[h], -LAYOUT_HEADINGS.index(h)))
    return best if scores[best] > 0 else ""


def _ordered_headings(blocks: List[str]) -> List[str]:
    """整篇沒分段時逐句判斷：小標只往下走（技術面→籌碼面→操作觀察），往回跳的句子歸到下一句的小標（或目前的小標）。"""
    raw = [_heading_of(b) for b in blocks]
    out: List[str] = []
    current = 0
    for i, heading in enumerate(raw):
        idx = LAYOUT_HEADINGS.index(heading) if heading else current
        if idx < current:
            following = next((LAYOUT_HEADINGS.index(h) for h in raw[i + 1:] if h), current)
            idx = max(current, following)
        current = idx
        out.append(LAYOUT_HEADINGS[idx])
    return out


def _extract_rows(paragraph: str, room: int) -> Tuple[str, List[Dict[str, str]]]:
    rows, kept = [], []
    for clause in _CLAUSE_RE.findall(paragraph):
        core = clause.strip("，,；;。 ")
        match = _ROW_TAIL_RE.match(core)
        label = _ROW_LABEL_TRIM.sub("", match.group(1).strip()) if match else ""
        if (match and len(rows) < room and label and len(label) <= LAYOUT_MAX_LABEL
                and any(w in core for w in _ROW_WORDS) and _LAYOUT_NUMBER_RE.search(match.group(2))):
            rows.append({"label": label, "value": match.group(2).strip()})
            continue
        kept.append(clause)
    text = "".join(kept).strip()
    if text and text[-1] in "，,；;":
        text = text[:-1] + "。"
    return text, rows


def rule_layout(body: str) -> Optional[Dict[str, Any]]:
    """不靠 AI 的排版：依關鍵字判斷小標、刪掉和小標重複的開頭語、籌碼數字抽成資料列。只刪減與搬動原句。"""
    blocks = [re.sub(r"\*\*|`", "", b).strip() for b in re.split(r"\n+", str(body or "")) if b.strip()]
    single = len(blocks) == 1
    if single:                            # 整篇只有一段：逐句判斷，連續同小標的句子合成一段
        blocks = [x.strip() for x in re.findall(r"[^。]+。?", blocks[0]) if x.strip()]
    sections: List[Dict[str, Any]] = []
    headings = _ordered_headings(blocks) if single else [
        _heading_of(b) or "" for b in blocks]
    for block, heading in zip(blocks, headings):
        heading = heading or (sections[-1]["heading"] if sections else LAYOUT_HEADINGS[0])
        text = _LEAD_IN_RE.sub("", block).strip()
        if not text:
            continue
        if sections and sections[-1]["heading"] == heading:
            # 原本就分段的文章保留段落；整篇一段時逐句判斷，同小標的句子接回同一段
            if single:
                sections[-1]["paragraphs"][-1] += text
            else:
                sections[-1]["paragraphs"].append(text)
        else:
            sections.append({"heading": heading, "paragraphs": [text], "rows": []})
    if not sections:
        return None
    for sec in sections:
        if sec["heading"] == "籌碼面":
            paragraphs = []
            for paragraph in sec["paragraphs"]:
                text, rows = _extract_rows(paragraph, LAYOUT_MAX_ROWS - len(sec["rows"]))
                sec["rows"] += rows
                if text:
                    paragraphs.append(text)
            sec["paragraphs"] = paragraphs
    return {"sections": [s for s in sections if s["paragraphs"] or s["rows"]]}


def layout_card(layout: Dict[str, Any], title: str, label: str, disclaimers: List[str]) -> Dict[str, Any]:
    """排版結果 → 和其他研究筆記同一套卡片（金色小標、段落、淺底資料列、註解）。"""
    sections: List[Dict[str, Any]] = []
    for sec in layout["sections"]:
        sections.append({"type": "heading", "text": sec["heading"]})
        # 每個小標底下一段完整文字：句子接在一起自然換行，不在句號處拆成好幾段（看起來零碎）
        merged = "".join(p.strip() for p in sec["paragraphs"] if p.strip())
        if merged:
            sections.append({"type": "paragraph", "text": merged})
        if sec["rows"]:
            sections.append({"type": "rows", "items": [{"lead": r["label"], "parts": [r["value"]]} for r in sec["rows"]]})
    notes = [re.sub(r"^[^\w一-鿿]+", "", str(d)).strip() for d in disclaimers]
    sections.append({"type": "note", "text": "※ " + "｜".join(n for n in notes if n)})
    return {"branch": title, "tags": ["權證分點觀察", "週精選"], "label": label, "sections": sections}


def weekly_image_text(draft: str, stock_code: str, stock_name: str = "") -> str:
    """週精選圖片專用文字。

    Discord 草稿保留「權證分點觀察｜週精選」與 📌 股票標頭，方便直接貼文；
    圖片上方本來就已經有週精選標題，因此圖片內移除重複的前兩行，
    改成和一般 3034 分析圖相同的「股票名稱（代號）＋正文」結構。
    """
    lines = [line.rstrip() for line in str(draft or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and "權證分點觀察" in lines[0] and ("週精選" in lines[0] or "周精選" in lines[0]):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    code = str(stock_code or "").strip()
    if lines and code and code in lines[0]:
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    # 圖片版把免責兩行從正文移到同一個小字 note，保留原本措辭，只調整版面。
    disclaimers = []
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("⚠️ 僅為個人投資筆記", "🧡 非任何買賣建議"):
            disclaimers.append(stripped)
        else:
            kept.append(line)
    body = "\n".join(kept).strip()
    if disclaimers:
        body = (body + "\n\n" if body else "") + "　｜　".join(disclaimers)
    label = f"{stock_name}（{code}）" if stock_name else code
    return (label + ("\n\n" + body if body else "")).strip()


def generate_weekly_draft(
    stock_code: str,
    generate: Callable[..., Any],
    find_ungrounded: Callable[[str, Dict[str, Any]], List[str]],
    instruction: str = "",
    previous_draft: str = "",
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """產生／修改單檔週精選文字。回傳 draft、candidate 與畫圖要用的分點。"""
    stock, ranking = find_weekly_candidate(stock_code, log=log)
    if not stock:
        return {
            "ok": False,
            "reason": f"{stock_code} 目前不在本週 Top10 候選內；請先查看本週精選排名，再選擇候選股。",
            "ranking": ranking,
        }
    prompt, facts = build_weekly_draft_prompt(stock, instruction=instruction, previous_draft=previous_draft, admin_notes=[])
    response = generate(prompt, WEEKLY_DRAFT_SCHEMA)
    if not getattr(response, "ok", False):
        return {"ok": False, "reason": getattr(response, "error", "AI 文字生成失敗") or "AI 文字生成失敗"}
    data = tools.core()._extract_json_from_text(response.text)
    draft = str((data or {}).get("draft") or "").strip() if isinstance(data, dict) else ""
    if not draft:
        return {"ok": False, "reason": "AI 回傳格式不完整，沒有取得週精選文字。"}
    ungrounded = find_ungrounded(draft, facts)
    if ungrounded:
        return {"ok": False, "reason": "AI 草稿中有數字無法對應原始資料，已阻止送出。", "issues": ungrounded[:10]}
    return {
        "ok": True,
        "draft": draft,
        "stock_code": stock["stock_code"],
        "stock_name": stock.get("stock_name", ""),
        "candidate": stock,
        "facts": facts,
        "mark_branches": mark_branches(stock),
    }

_WEEKLY_DRAFT_PATTERNS = (
    "週精選文字", "周精選文字", "精選文字", "週精選文案", "周精選文案", "生成精選文字", "產生精選文字"
)
_WEEKLY_IMAGE_PATTERNS = ("生成週精選圖片", "生成周精選圖片", "週精選圖片", "周精選圖片", "轉成圖片", "做成圖片", "生成圖片")
_WEEKLY_REVISION_WORDS = (
    "改", "修改", "短一點", "長一點", "口語", "刪", "刪掉", "拿掉", "省略", "補", "補上", "補充",
    "加上", "加入", "加進去", "寫上", "寫進去", "強調", "不要寫", "換成", "重寫", "重整", "順一下",
    "這版", "上一版", "這段", "第二段", "第一段", "也要", "也寫", "再加", "再補",
)
_WEEKLY_EDIT_FIELDS = ("勝率", "平均持有", "持有天數", "加權報酬", "大量區", "均線", "布林", "權證", "分點", "外資", "投信", "法人", "現股籌碼", "技術面")


def extract_stock_code(text: str) -> str:
    match = re.search(r"(?<!\d)([1-9]\d{3})(?!\d)", str(text or ""))
    return match.group(1) if match else ""


# 管理員自己寫好（或想沿用舊版）的文字：直接套用，不經過 AI 重寫。
_MANUAL_DRAFT_PATTERNS = ("套用文字", "套用這段", "套用草稿", "用這段文字", "使用這段文字",
                          "直接用這段", "貼上文字", "沿用文字", "手動文字")
# Discord slash 指令的輸入框打不出換行，所以也接受這些符號當段落分隔。
_PARAGRAPH_MARKS = ("\\n", "//", "｜｜", "||")


def normalize_draft_text(text: str) -> str:
    """把手動貼上的文字整理成段落：支援真正的換行，或用 \\n、//、｜｜ 當分段符號。"""
    value = str(text or "").strip()
    for mark in _PARAGRAPH_MARKS:
        value = value.replace(mark, "\n")
    value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def is_manual_draft_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not extract_stock_code(compact):
        return False
    return any(k in compact for k in _MANUAL_DRAFT_PATTERNS) or "權證分點觀察" in compact


def extract_manual_draft(text: str) -> Tuple[str, str]:
    """回傳（股票代號, 文章內容）；抓不到內容時回傳空字串。"""
    raw = str(text or "")
    code = extract_stock_code(re.sub(r"\s+", "", raw))
    if not code:
        return "", ""
    body = raw
    for keyword in _MANUAL_DRAFT_PATTERNS:
        position = body.find(keyword)
        if position >= 0:
            body = body[position + len(keyword):]
            break
    else:
        # 直接貼整篇（開頭就是「權證分點觀察｜週精選」）：把指令前綴切掉
        position = body.find("權證分點觀察")
        if position > 0:
            body = body[position:]
    body = re.sub(r"^[\s:：，,、。\-–—]+", "", body)
    return code, normalize_draft_text(body)


def is_weekly_draft_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return bool(extract_stock_code(compact) and any(k in compact for k in _WEEKLY_DRAFT_PATTERNS))


def is_weekly_image_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(k in compact for k in _WEEKLY_IMAGE_PATTERNS)


def is_weekly_revision_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(k in compact for k in _WEEKLY_REVISION_WORDS)


# 草稿編輯中，出現這些字通常代表「使用者換題目了」……
_OTHER_TOPIC_PATTERNS = (
    "族群", "類股", "概念股", "排行", "排名", "本週精選", "本周精選", "精選股票", "精選個股",
    "勝率", "分點", "新聞", "股價多少", "收盤價", "有哪些", "誰最強", "誰型態", "怎麼用", "使用說明",
    "系統狀態", "更新名冊", "更新底庫",
)
# ……但同一句若帶有修改語氣（「把新光的勝率補上」），仍然是在改這篇草稿。
_EDIT_CUE_PATTERNS = (
    "改", "修", "短", "長", "刪", "拿掉", "去掉", "省略", "補", "加", "寫", "強調", "不要", "不用", "別",
    "換", "口語", "語氣", "段", "句", "標題", "開頭", "結尾", "重寫", "重整", "順", "簡單", "詳細",
    "這版", "上一版", "多一點", "少一點", "也要", "再",
)


# 草稿開著時的判斷順序：別檔股票→離開；明確改稿語氣→改稿；問句／查明細→一般問答；帶代號查資料→一般問答；其餘→改稿
_STRONG_EDIT_RE = re.compile(
    r"改成|改為|改一下|修改|刪掉|刪除|拿掉|去掉|省略|不要寫|不要提|不用寫|不用提|別寫|別提|補上|補進|寫進|加上|加入|寫成|"
    r"強調|換個|口語|語氣|重寫|太長|太短|短一點|長一點|精簡|簡短|詳細一點|多一點|少一點|多寫|少寫|"
    r"這段|那段|這句|這篇|第[一二三四五1-5]段|第[一二三四五1-5]句|開頭|結尾|標題|結論|上一版|草稿|文章")
_QUESTION_RE = re.compile(r"[？?]|嗎|多少|哪些|哪幾|哪一|誰|如何|怎麼看|怎麼樣|是否|有沒有|明細|查詢|查一下|列出|給我看")
_DATA_TOPIC_WORDS = ("買賣超", "明細", "籌碼", "現股", "外資", "投信", "法人", "K線", "技術面", "型態", "新聞",
                     "權證", "分點", "勝率", "大量區", "支撐", "壓力")
_QUANTITY_AFTER_NUMBER = re.compile(r"(?<!\d)([1-9]\d{3})(?=\s*(?:張|股|元|塊|萬|億|％|%|點|筆|口|人))")


def _code_in_edit_text(text: str) -> str:
    """改稿句子裡的 4 位數：後面接張／元／萬等單位的是數量，不是股票代號。"""
    return extract_stock_code(_QUANTITY_AFTER_NUMBER.sub(" ", str(text or "")))


def is_weekly_session_followup(text: str, current_stock_code: str = "") -> bool:
    """已有週精選草稿時，**預設把訊息當成改稿指令**；只有明顯是別的題目才放行給一般問答。

    舊版用關鍵字白名單判斷「這句是不是在改稿」，像「不要提到均線」「這樣太長了」「換個說法」
    都會漏接，草稿完全沒被修改，使用者會以為 Bot 不理人。
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return False
    code = _code_in_edit_text(compact)
    if code and current_stock_code and code != str(current_stock_code):
        return False          # 明確問另一檔股票
    if _STRONG_EDIT_RE.search(compact):
        return True           # 明確的改稿語氣（「把新光的勝率補上」「權證那段短一點」）
    if _QUESTION_RE.search(compact):
        return False          # 在問問題／查明細（「3042 買賣超明細」「永豐金內湖勝率多少」）
    if code and any(w in compact for w in _DATA_TOPIC_WORDS):
        return False          # 帶代號查資料（「3042 現股籌碼」）
    if any(k in compact for k in _OTHER_TOPIC_PATTERNS) and not any(k in compact for k in _EDIT_CUE_PATTERNS):
        return False          # 換成族群／分點／排名等其他功能（沒有任何修改語氣）
    return True


def is_admin_moneydj_image_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).upper()
    return bool(extract_stock_code(compact) and ("MONEYDJ" in compact or "備援" in compact) and any(k in compact for k in ("圖片", "產圖", "K線", "分點")))


def extract_admin_moneydj_branch(text: str) -> str:
    """管理員備援圖片的原始分點名稱；允許該分點不在 Google Sheet 名冊。"""
    value = str(text or "")
    value = re.sub(r"(?<!\d)[1-9]\d{3}(?!\d)", " ", value)
    value = re.sub(r"(?i)moneydj", " ", value)
    for word in ("用", "使用", "備援", "生成", "產生", "產圖", "圖片", "K線圖", "K線", "權證", "分點", "幫我", "請"):
        value = value.replace(word, " ")
    return re.sub(r"[，,、：:\s]+", " ", value).strip()


def is_weekly_admin_feature_question(text: str) -> bool:
    return is_weekly_pick_question(text) or is_weekly_draft_question(text) or is_weekly_image_question(text) or is_admin_moneydj_image_question(text)
