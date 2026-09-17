"""艾斯 AI｜本週精選候選股（私人研究用）。

目的不是替使用者選股，而是每週從權證資料找出最適合撰寫 Discord「本週精選」週報的
3～5 檔候選，最後由使用者自己決定。

流程（效能由粗到細）：
    Stage 1  回測官方 A～E 事件表 → 最近 N 個交易日有事件的「分點 × 股票」
    Stage 2  分點 × 本次事件的歷史績效（Bayesian 修正勝率）＋ 事件買進金額 → 預排序，縮到 10～20 檔
    Stage 3  只對候選抓技術面、大量區、分點近期操作（含 MoneyDJ 近20日流水）
    Score    事件績效 25 ＋ 近期操作 10 ＋ 權證金額 25 ＋ 型態評分 40（100 分制型態分數 × 0.4）＝ 100
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


# ============================================================
# 型態評分（100 分制；本週精選與一般問答共用）
# 五大項各自從 0 分算到滿分，全部滿分＝100；同一件事只在一個項目計分。
#   均線趨勢 30｜價格位置 20｜量區結構 25｜下方支撐 15｜布林 10
# ============================================================

PATTERN_GRADES = ((70.0, "結構偏強"), (40.0, "結構中性"), (0.0, "結構偏弱"))
PATTERN_WEIGHT = 40.0  # 本週精選綜合分數中型態評分的權重
PATTERN_COMPONENTS = (("均線趨勢", 30), ("價格位置", 20), ("量區結構", 25), ("下方支撐", 15), ("布林", 10))


def _direction_points(d: Dict[str, Any], full: float) -> Tuple[float, str]:
    """均線方向＋扣抵推算：上揚且扣抵後不轉彎＝滿分；上揚但將轉下彎＝一半以下；下彎＝0。"""
    now, turn, day = d.get("direction_now"), d.get("turn"), d.get("turn_day")
    if not now:
        return full / 2, "資料不足，給一半"
    if now == "上揚":
        if turn == "轉下彎":
            return round(full * 0.4, 1), f"上揚，但扣抵價偏高，收盤不變第 {day} 日起轉下彎"
        return full, "上揚，扣抵後仍續揚"
    if now == "下彎":
        if turn == "轉上揚":
            return round(full * 0.5, 1), f"下彎，但扣抵價偏低，收盤不變第 {day} 日起轉上揚"
        return 0.0, "下彎" + ("，且在股價上方形成壓力" if d.get("ma_above_close") else "")
    if turn == "轉上揚":
        return round(full * 0.7, 1), f"走平，收盤不變第 {day} 日起轉上揚"
    if turn == "轉下彎":
        return round(full * 0.2, 1), f"走平，收盤不變第 {day} 日起轉下彎"
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
    close = _f(vp.get("close")) or _f(tech.get("close"))
    position = str(vp.get("position_vs_two_zones", ""))
    if "之上" in position:
        add("量區結構", "相對兩大量區", 10, 10, "收盤在兩大量區之上")
    elif "之下" in position:
        add("量區結構", "相對兩大量區", 0, 10, "收盤在兩大量區之下")
    elif "之間" in position:
        add("量區結構", "相對兩大量區", 5, 10, "收盤在兩大量區之間")
    else:
        add("量區結構", "相對兩大量區", 5, 10, "量區位置資料不足，給一半")
    relation = str((vp.get("maximum_volume_zone") or {}).get("close_relation", ""))
    if "上方" in relation:
        points, note = 5, "站在最大量區上方"
        if vp.get("recent_breakout"):
            points, note = 7, "近期突破最大量區並站穩"
            if vp.get("retest_after_breakout"):
                points, note = 9, "近期突破最大量區，回踩未破"
        add("量區結構", "最大量區", points, 9, note)
    elif "量區內" in relation:
        add("量區結構", "最大量區", 3, 9, "在最大量區內整理")
    elif "下方" in relation:
        add("量區結構", "最大量區", 0, 9, "近期跌破最大量區且未站回" if vp.get("recent_breakdown") else "在最大量區下方")
    else:
        add("量區結構", "最大量區", 4.5, 9, "最大量區資料不足，給一半")
    overhead = None
    for key in ("maximum_volume_zone", "second_volume_zone"):
        zone = vp.get(key) or {}
        low = _f(zone.get("price_low"))
        if close and low and low > close:
            gap = (low / close - 1) * 100
            if overhead is None or gap < overhead[0]:
                overhead = (gap, zone.get("label") or "大量區", low)
    if overhead is None:
        add("量區結構", "上方量區壓力", 6, 6, "上方沒有大量區")
    elif overhead[0] <= config.overhead_zone_pct:
        marks["overhead"] = True
        add("量區結構", "上方量區壓力", 0, 6, f"上方{overhead[1]} {overhead[2]:g} 只差 {overhead[0]:.1f}%，形成壓力")
    else:
        add("量區結構", "上方量區壓力", 3, 6, f"上方{overhead[1]} {overhead[2]:g} 距離 {overhead[0]:.1f}%")

    # ---------- 下方支撐 15：最近支撐距離 11＋8% 內支撐數 4 ----------
    supports = []
    if close:
        for label, level in (("MA20", values["MA20"]), ("MA60", values["MA60"])):
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

    # ---------- 布林 10：位置 5＋通道狀態 5（中軌方向已算在 MA20，不重複） ----------
    bb_position = str(bb.get("position", ""))
    bb_points = {"位於中軌與上軌之間": 5, "收盤位於上軌外": 3, "突破上軌": 3, "位於下軌與中軌之間": 2}.get(bb_position, 0)
    add("布林", "通道位置", bb_points if bb_position != "資料不足" else 2.5, 5, bb_position or "資料不足")
    walk, squeeze_break, width_trend = bb.get("band_walk"), bb.get("squeeze_breakout"), bb.get("width_trend")
    above_mid = bb_position in ("位於中軌與上軌之間", "收盤位於上軌外", "突破上軌")
    if walk == "沿上軌" or squeeze_break == "壓縮後向上突破":
        add("布林", "通道狀態", 5, 5, "沿上軌" if walk == "沿上軌" else "壓縮後向上突破")
    elif walk == "沿下軌" or squeeze_break == "壓縮後向下跌破":
        add("布林", "通道狀態", 0, 5, "沿下軌" if walk == "沿下軌" else "壓縮後向下跌破")
    elif width_trend == "擴張":
        add("布林", "通道狀態", 4 if above_mid else 1, 5, f"帶寬擴張，股價在中軌{'上方' if above_mid else '下方'}")
    elif width_trend in ("收窄", "持平") or bb.get("squeeze"):
        add("布林", "通道狀態", 3, 5, "帶寬收斂／持平，方向未定")
    else:
        add("布林", "通道狀態", 2.5, 5, "布林資料不足，給一半")

    components = []
    for name, maximum in PATTERN_COMPONENTS:
        value = round(sum(i["points"] for i in items if i["component"] == name), 1)
        components.append({"label": name, "value": value, "max": maximum})
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
    if row.get("open_event_count"):
        return "持有中・近期有賣出" if sells else "持有中"
    if row.get("events_recent") or row.get("event_buy_amount_lookback_text") not in (None, "", "-"):
        return "已出清"
    return "只有賣出紀錄"


def build_pattern_scorecard(
    tech: Dict[str, Any],
    vp: Dict[str, Any],
    extras: Dict[str, Any],
    chips: Optional[Dict[str, Any]] = None,
    cost_price: Optional[float] = None,
    config: Optional[WeeklyPickConfig] = None,
) -> Dict[str, Any]:
    """一般問答的型態評分卡：分數規則與本週精選相同（score_pattern）；只評技術結構，不含權證籌碼、不是買賣建議。"""
    config = config or WeeklyPickConfig()
    pattern = score_pattern(tech, vp, extras, config)
    good, bad = pattern_reason_lists(pattern["items"])
    levels = tools.key_price_levels(tech, vp)
    close = levels["close"]
    branches = []
    for row in ((chips or {}).get("branches") or [])[:5]:
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
        "close": close,
        "resistances_above_close": levels["resistances"],
        "supports_below_close": levels["supports"],
        "tracked_branches": branches,
        "tracked_branches_period": (chips or {}).get("period_lookback", ""),
        "method": "型態分數 100 分＝均線趨勢 30＋價格位置 20＋量區結構 25＋下方支撐 15＋布林 10，每項從 0 分算起，規則與本週精選相同；只評技術結構，不含籌碼，不是買賣建議",
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
        # 型態評分（100 分制，與一般問答共用）換算成 40 分計入綜合分數。
        pattern: Dict[str, Any] = {}
        if tech and vp:
            pattern = score_pattern(tech, vp, extras, config)
            breakdown["pattern_score"] = round(pattern["score"] * PATTERN_WEIGHT / 100, 2)
            good, bad = pattern_reason_lists(pattern["items"])
            reasons["pattern"] = good + bad
            marks = pattern["marks"]
        else:
            breakdown["pattern_score"], reasons["pattern"], marks = 0.0, ["技術資料取得失敗"], {"extended": False, "overhead": False}
        total = round(sum(breakdown.values()), 1)
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
        if (breakdown["event_performance_score"] >= 18 and pattern.get("score", 0) < 40) or (
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
            "score_reasons": reasons,
            "pattern": pattern,
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
            f"warrant_amount_score = {b['warrant_amount_score']} / 25｜pattern_score = {b['pattern_score']} / {PATTERN_WEIGHT:g}"
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
TOP5 已由 Python 依分數排好，你只負責解釋，不得更改排名、不得新增或刪除股票。
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
overview：一句話總結這份 TOP5 的共同特徵（40 字以內）
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
        lines.append(
            f"綜合分數：{stock['score']} / 100（事件 {b['event_performance_score']}｜近期 {b['recent_branch_behavior_score']}｜"
            f"金額 {b['warrant_amount_score']}｜型態 {b['pattern_score']}）"
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
        f"資料時間：A～E 事件 {result['window_start']}～{result['window_end']}｜"
        f"勝率統計更新 {result['perf_sheet_updated_at'] or '時間未知'}｜股價為日K收盤資料"
    )
    lines.append("※ 本週精選是研究候選清單，不是買賣建議；最後由你自行判斷。")
    return "\n".join(lines)


# ============================================================
# 卡片資料（圖片版面用）
# ============================================================

SCORE_PARTS = (
    ("event_performance_score", "事件績效", 25),
    ("recent_branch_behavior_score", "近期操作", 10),
    ("warrant_amount_score", "權證金額", 25),
    ("pattern_score", "型態評分", 40),
)


def mark_branches(stock: Dict[str, Any]) -> List[str]:
    """K 線要標註的分點＝這檔候選實際用來評分的分點（主力＋高品質分點）。"""
    names = [stock["lead"]["branch"]] + [p["branch"] for p in stock.get("high_quality_pairs") or []]
    return list(dict.fromkeys(names))[:4]


def card_facts(stock: Dict[str, Any]) -> Dict[str, Any]:
    """卡片上的固定數據（全部來自 Python 計算，不經過 AI）。"""
    lead = stock["lead"]
    tech = stock.get("technical") or {}
    vp = stock.get("volume_profile") or {}
    ma20 = (tech.get("moving_averages") or {}).get("MA20") or {}
    overall = lead.get("overall") or {}
    event_lines = []
    for code, m in lead["event_performance"].items():
        if m.get("raw_win_rate") is None and m.get("adjusted_win_rate") is None:
            event_lines.append(f"{code} 事件｜勝率統計無資料")
            continue
        line = f"{code} 事件｜勝率 {m.get('raw_win_rate')}%（修正 {m.get('adjusted_win_rate')}%）｜樣本 {_count_text(m.get('included_count'))}"
        if m.get("unresolved_count"):
            line += f"｜未完成 {_count_text(m.get('unresolved_count'))}"
        event_lines.append(line)
    return {
        "rank": stock["rank"],
        "stock_code": stock["stock_code"],
        "stock_name": stock.get("stock_name", ""),
        "score": stock["score"],
        "score_parts": [
            {"label": label, "value": stock["score_breakdown"].get(key, 0), "max": maximum}
            for key, label, maximum in SCORE_PARTS
        ],
        "pattern_label": vp.get("pattern_label") or "型態資料不足",
        "ma_alignment": tech.get("ma_alignment") or "-",
        "ma20_text": f"MA20 {ma20.get('position', '-')} {ma20.get('distance_pct')}%" if ma20.get("distance_pct") is not None else "",
        "lead_branch": lead["branch"],
        "lead_amount_text": lead["event_buy_amount_text"],
        "triggered_events": lead["triggered_events"],
        "event_lines": event_lines,
        "overall_line": f"總勝率（背景）{overall.get('raw_win_rate', '-')}%｜樣本 {_count_text(overall.get('included_count'))}",
        "other_branches": [p["branch"] for p in stock.get("high_quality_pairs") or [] if p["branch"] != lead["branch"]][:3],
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


def weekly_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filters": result.get("filters") or [],
        "data_time": (
            f"資料時間：A～E 事件 {result['window_start']}～{result['window_end']}｜"
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


def run_weekly_pick(
    question: str,
    generate: Callable[..., Any],
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
        "weekly_pick_cards_v3",  # v3：型態評分改 100 分制，舊快取的分數欄位不同
        tools._fmt_date(bundle["latest_event_date"]),
        str(perf.get("sheet_updated_at", "")),
        filters.signature(),
        str(config.min_event_win_rate),
        str(config.top_n),
    ])

    def answer(item: Dict[str, Any], calls: int, cache_hit: bool, notice: str = "") -> WeeklyPickAnswer:
        result = item["result"]
        cards, overview, meta = item.get("cards") or [], item.get("overview", ""), weekly_meta(result)
        text = cards_to_text(cards, overview, meta) if cards else format_rule_based(result)
        if notice:
            text = f"{notice}\n\n{text}"
        return WeeklyPickAnswer(
            text, calls, cache_hit, time.perf_counter() - started,
            [c["stock_code"] for c in cards], cards, overview, meta, notice,
        )

    cached = None if filters.refresh else _load_cache(config, cache_key)
    if cached and cached.get("ai_ok") and cached.get("cards"):
        stage_log("快取命中（同一份事件資料與條件），不重新計算、不呼叫 Gemini")
        return answer(cached, 0, True)

    engine = WeeklyPickEngine(config, log)
    result = cached["result"] if cached and cached.get("result") else engine.run(filters)
    if not result["top"]:
        item = {"result": result, "cards": [], "overview": "", "ai_ok": True}
        _save_cache(config, cache_key, item)
        return answer(item, 0, False)

    prompt, payload = build_gemini_prompt(result)
    stage_log(f"Gemini prompt {len(prompt):,} 字（TOP{len(result['top'])}，1 次呼叫，結構化卡片）")
    response = generate(prompt, WEEKLY_CARD_SCHEMA)
    calls = 1
    rule_cards, _ = build_cards(result, None)
    if not getattr(response, "ok", False):
        stage_log(f"Gemini 失敗：{getattr(response, 'error', '')}")
        notice = rate_limit_message if getattr(response, "rate_limited", False) else "AI 說明暫時無法使用，以下為系統計算結果。"
        item = {"result": result, "cards": rule_cards, "overview": "", "ai_ok": False}
        _save_cache(config, cache_key, item)
        return answer(item, calls, False, notice)
    data = tools.core()._extract_json_from_text(response.text)
    if not isinstance(data, dict):
        stage_log("Gemini 回傳不是合法 JSON，改用系統整理內容")
        item = {"result": result, "cards": rule_cards, "overview": "", "ai_ok": False}
        _save_cache(config, cache_key, item)
        return answer(item, calls, False, "AI 說明格式錯誤，以下為系統計算結果。")
    cards, overview = build_cards(result, data)
    ungrounded = find_ungrounded(cards_ai_text(cards, overview), payload)
    if ungrounded:
        stage_log(f"數字核對未通過：{ungrounded[:10]}")
        item = {"result": result, "cards": rule_cards, "overview": "", "ai_ok": False}
        _save_cache(config, cache_key, item)
        return answer(item, calls, False, "AI 說明中有數字無法對應到原始資料，以下為系統計算結果。")
    item = {"result": result, "cards": cards, "overview": overview, "ai_ok": True}
    _save_cache(config, cache_key, item)
    stage_log(f"完成｜Gemini {calls} 次｜總耗時 {time.perf_counter() - started:.1f}s")
    return answer(item, calls, False)
