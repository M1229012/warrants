"""盤中量能估算 ＋ 時段曲線自我校正（TWSE／TPEX 分開訓練）。

    估今日全日量 = 目前累積量 ÷ 該時點係數
    輸出同時比較：昨日全日量、5 日均量、20 日均量（以 20 日均量為主要判讀基準）

曲線訓練流程（每日收盤後）：
  基準籃子（每月自動重挑）＋會員查詢股票（同股票同時點去重）
  → 收盤取得實際全日量 → 排除不適合訓練的股票／日期
  → 每日每市場每時點取「跨股票中位數」→ 保留最近 20 個交易日
  → 取跨日中位數當目標值 → 單日最多 ±2ppt 靠近 → 強制單調遞增、13:30=1.0
  → 同時更新各時點的歷史預測誤差，誤差決定輸出語氣

資料量：55 時點 × 20 日 × 2 市場 ≈ 2,200 筆；逐檔原始樣本收盤校正完即刪。
"""
from __future__ import annotations

import re
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

import warrant_ai_tools as tools
import local_market_cache

OPEN_MINUTES = 9 * 60
CLOSE_MINUTES = 13 * 60 + 30
BUCKET = 5
KEEP_DAYS = max(5, tools._env_int("DISCORD_AI_VOLUME_CURVE_DAYS", 20))
MIN_DAYS_BLEND = 8          # 少於這個日數完全用預設曲線
MIN_DAYS_FULL = 15          # 達到這個日數才完全採用學習曲線
MAX_STEP = 0.02             # 單日每個時點最多調整 2 個百分點
ABNORMAL_VOLUME_X = 3.0     # 當日量 > 20 日均量的幾倍就不拿來訓練曲線
MIN_TRAIN_LOTS = 500        # 成交量過低的股票不訓練曲線
BASKET_PER_MARKET = 10
SAMPLES_KEY = "ivol_samples"        # 舊版：當日原始樣本整包 JSON（改存 ivol_samples 資料表，只剩部署當天相容讀取）
DAILY_KEY = "ivol_daily_medians"    # 每日×市場×時點中位數（保留 KEEP_DAYS 日）
CURVE_KEY = "ivol_curves"           # 目前使用的曲線
ERROR_KEY = "ivol_errors"           # 各時點歷史預測誤差
BASKET_KEY = "ivol_basket"          # 基準籃子（每月重挑）

# 預設經驗曲線（到該時點為止的累積量占全日比例）；上櫃尾盤占比略高。
_DEFAULT_POINTS = {
    9 * 60 + 5: 0.10, 9 * 60 + 15: 0.17, 9 * 60 + 30: 0.25, 9 * 60 + 45: 0.31,
    10 * 60: 0.36, 10 * 60 + 30: 0.44, 11 * 60: 0.50, 11 * 60 + 30: 0.56,
    12 * 60: 0.62, 12 * 60 + 30: 0.68, 13 * 60: 0.76, 13 * 60 + 15: 0.83,
    13 * 60 + 25: 0.88, CLOSE_MINUTES: 1.0,
}
DEFAULT_CURVES = {
    "twse": dict(_DEFAULT_POINTS),
    "tpex": {k: (round(v * 0.97, 4) if k < 13 * 60 + 20 else v) for k, v in _DEFAULT_POINTS.items()},
}


# ============================================================
# 共用
# ============================================================

def _bucket(minutes: int) -> int:
    return max(OPEN_MINUTES, min(CLOSE_MINUTES, int(minutes) // BUCKET * BUCKET))


def _state(key: str, default):
    value = local_market_cache.get_state(key, default)
    return value if isinstance(value, type(default)) else default


def _interpolate(points: Dict[int, float], minutes: int) -> Optional[float]:
    if minutes >= CLOSE_MINUTES:
        return 1.0
    keys = sorted(points)
    if not keys:
        return None
    if minutes <= keys[0]:
        return points[keys[0]]
    for left, right in zip(keys, keys[1:]):
        if left <= minutes <= right:
            span = right - left
            if span <= 0:
                return points[right]
            return points[left] + (points[right] - points[left]) * ((minutes - left) / span)
    return points[keys[-1]]


def curve_for(market: str) -> Tuple[Dict[int, float], str, int]:
    """(係數表, 來源說明, 有效日數)；日數不足就混合或退回預設曲線。"""
    market = market if market in DEFAULT_CURVES else "twse"
    default = DEFAULT_CURVES[market]
    stored = _state(CURVE_KEY, {}).get(market) or {}
    learned = {int(k): float(v) for k, v in (stored.get("points") or {}).items() if 0 < float(v) <= 1}
    days = int(stored.get("days") or 0)
    if not learned or days < MIN_DAYS_BLEND:
        return dict(default), "預設曲線", days
    if days >= MIN_DAYS_FULL:
        merged = dict(default)
        merged.update(learned)
        return merged, f"學習曲線（{days} 日）", days
    weight = (days - MIN_DAYS_BLEND) / max(1, MIN_DAYS_FULL - MIN_DAYS_BLEND)
    merged = dict(default)
    for minute, value in learned.items():
        base = _interpolate(default, minute) or value
        merged[minute] = round(base * (1 - weight) + value * weight, 4)
    return merged, f"混合曲線（{days} 日，權重 {weight:.0%}）", days


def _error_pct(market: str, minutes: int) -> Optional[float]:
    errors = _state(ERROR_KEY, {}).get(market) or {}
    value = errors.get(str(_bucket(minutes)))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(error_pct: Optional[float]) -> Tuple[str, str]:
    """用實際誤差決定語氣，不寫死開盤時間規則。"""
    if error_pct is None:
        return "初步", "此時點尚無誤差統計，僅供粗略參考"
    if error_pct > 15:
        return "初步", f"此時點近期估算誤差中位數約 {error_pct:.0f}%，僅供粗略參考"
    if error_pct >= 5:
        return "估算", f"此時點近期估算誤差中位數約 {error_pct:.0f}%"
    return "可靠", f"此時點近期估算誤差中位數約 {error_pct:.0f}%"


# ============================================================
# 盤中記錄與估算
# ============================================================

def record_sample(code: str, market: str, cumulative_lots: float, now=None,
                  pred: Optional[float] = None) -> None:
    """同股票×同日×同 5 分鐘只存一筆（覆蓋更新）。"""
    if not code or not cumulative_lots or cumulative_lots <= 0:
        return
    now = now or tools.taipei_now()
    minutes = now.hour * 60 + now.minute
    if not (OPEN_MINUTES <= minutes <= CLOSE_MINUTES):
        return
    # 一檔×一個時點一列（ivol_samples 表），不再每次把「當天所有股票的樣本」整包 JSON 重寫
    local_market_cache.ivol_record(now.strftime("%Y-%m-%d"), str(code),
                                   market if market in DEFAULT_CURVES else "twse",
                                   _bucket(minutes), round(float(cumulative_lots), 1),
                                   pred=round(float(pred), 1) if pred is not None else None)


def estimate(code: str, market: str, cumulative_lots: Optional[float],
             prev_day_lots: Optional[float] = None, mv5_lots: Optional[float] = None,
             mv20_lots: Optional[float] = None, now=None) -> Dict[str, Any]:
    """盤中估算今日全日量，並同時比較昨日、5 日均量、20 日均量。"""
    now = now or tools.taipei_now()
    minutes = now.hour * 60 + now.minute
    if not cumulative_lots or cumulative_lots <= 0 or minutes < OPEN_MINUTES:
        return {}
    market = market if market in DEFAULT_CURVES else "twse"
    points, source, _days = curve_for(market)
    factor = _interpolate(points, minutes)
    if not factor or factor <= 0:
        return {}
    estimated = float(cumulative_lots) / factor
    error_pct = _error_pct(market, minutes)
    level_label, confidence_note = _confidence(error_pct)
    result: Dict[str, Any] = {
        "time": now.strftime("%H:%M"),
        "cumulative_lots": round(float(cumulative_lots)),
        "elapsed_pct_of_day": round(factor * 100, 1),
        "estimated_full_day_lots": round(estimated),
        "curve_source": source,
        "confidence": level_label,
        "confidence_note": confidence_note,
        "expected_error_pct": error_pct,
        "note": "依時段量能分布推估全日量，收盤前為估算值",
    }
    for label, key, base in (("昨日", "vs_prev_day_pct", prev_day_lots),
                             ("5日均量", "vs_mv5_pct", mv5_lots),
                             ("20日均量", "vs_mv20_pct", mv20_lots)):
        if base and float(base) > 0:
            result[key] = round((estimated / float(base) - 1) * 100, 1)
            result[key.replace("_pct", "_lots")] = round(float(base))
    main = result.get("vs_mv20_pct")
    if main is not None:
        result["level"] = ("明顯放大" if main >= 30 else "溫和放大" if main >= 10
                           else "明顯量縮" if main <= -30 else "溫和量縮" if main <= -10 else "與常態相當")
        result["baseline"] = "20日均量"
    try:
        record_sample(code, market, float(cumulative_lots), now, pred=estimated)
    except Exception:
        pass
    return result


# ============================================================
# 基準籃子（每月重挑）
# ============================================================

def _pick_basket() -> Dict[str, List[str]]:
    """依近 20 日平均成交金額分層，各層挑波動相對穩定者，當市場成交節奏的感測器。"""
    liquidity = local_market_cache.liquidity_map(20)
    markets: Dict[str, List[Tuple[str, float]]] = {"twse": [], "tpex": []}
    for code, info in liquidity.items():
        if not re.fullmatch(r"[1-9]\d{3}", str(code)):
            continue                                     # 只留普通股，排除 ETF／權證
        value = float(info.get("avg_value") or 0)
        lots = float(info.get("avg_lots") or 0)
        if value <= 0 or lots < MIN_TRAIN_LOTS:
            continue
        bars = local_market_cache.load_bars(code, limit=21)
        if not bars or bars.get("count", 0) < 21:
            continue                                     # 新上市／資料不足不選
        frame = bars["df"]
        changes = frame["Close"].pct_change().dropna() * 100
        volatility = float(changes.std()) if len(changes) else 999.0
        market = str(bars.get("market") or "twse")
        if market in markets:
            markets[market].append((code, value, volatility))
    basket: Dict[str, List[str]] = {}
    for market, rows in markets.items():
        if len(rows) < BASKET_PER_MARKET:
            continue
        rows.sort(key=lambda r: -r[1])                   # 依平均成交金額排序
        size = len(rows)
        layers = ((0, int(size * 0.1), 3),               # 大型／高流動
                  (int(size * 0.1), int(size * 0.4), 3),  # 中型
                  (int(size * 0.4), size, 4))             # 一般流動性
        picked: List[str] = []
        for start, end, count in layers:
            pool = sorted(rows[start:end], key=lambda r: r[2])   # 同層取波動較穩定者
            picked += [code for code, *_ in pool[:count]]
        basket[market] = picked[:BASKET_PER_MARKET]
    return basket


def basket(refresh: bool = False) -> Dict[str, List[str]]:
    """每月第一個交易日重挑一次；名單固定，避免樣本結構每天變動。"""
    stored = _state(BASKET_KEY, {})
    month = tools.taipei_now().strftime("%Y-%m")
    if not refresh and stored.get("month") == month and stored.get("basket"):
        return dict(stored["basket"])
    picked = _pick_basket()
    if picked:
        local_market_cache.set_state(BASKET_KEY, {"month": month, "basket": picked, "at": time.time()})
        print(f"🧺 盤中量能基準籃子已更新（{month}）：" +
              "｜".join(f"{m} {len(c)} 檔" for m, c in picked.items()), flush=True)
        return picked
    return dict(stored.get("basket") or {})


def sample_basket() -> Dict[str, int]:
    """背景每 5 分鐘取一次基準股的累積量；走背景額度，忙碌時自動讓給使用者。"""
    now = tools.taipei_now()
    minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5 or not (OPEN_MINUTES <= minutes <= CLOSE_MINUTES):
        return {"sampled": 0, "reason": "非盤中"}
    taken = 0
    for market, codes in (basket() or {}).items():
        for code in codes:
            if not tools.fugle_background_allowed():
                return {"sampled": taken, "reason": "背景額度保留給使用者"}
            try:
                with tools.api_priority("background"):
                    quote = tools.fetch_fugle_quote(code)
            except Exception:
                continue
            lots = tools._num(quote.get("trade_volume"))
            if not lots:
                continue
            record_sample(code, market, float(tools._quote_volume_shares(float(lots))) / 1000, now)
            taken += 1
    return {"sampled": taken}


# ============================================================
# 收盤校正
# ============================================================

def calibrate(full_day_lots: Dict[str, float], avg20_lots: Dict[str, float],
              day: str = "", session_ok: bool = True) -> Dict[str, Any]:
    """收盤後回算真實係數並更新曲線與誤差統計。"""
    day = day or tools.taipei_now().strftime("%Y-%m-%d")
    today = _state(SAMPLES_KEY, {}).get(day) or {}
    for code, entry in local_market_cache.ivol_load_day(day).items():
        legacy = today.get(code) or {}
        today[code] = {**legacy, **entry,
                       "points": {**(legacy.get("points") or {}), **(entry.get("points") or {})},
                       "pred": {**(legacy.get("pred") or {}), **(entry.get("pred") or {})}}
    if not today:
        return {"updated": 0, "reason": "no_samples"}
    if not session_ok:
        local_market_cache.ivol_delete_day(day)
        return {"updated": 0, "reason": "非正常交易日，不納入訓練"}
    if not any(float(full_day_lots.get(code) or 0) > 0 for code in today):
        return {"updated": 0, "reason": "尚無當日正式收盤量，保留樣本待重試"}

    ratios: Dict[str, Dict[int, List[float]]] = {"twse": {}, "tpex": {}}
    errors: Dict[str, Dict[int, List[float]]] = {"twse": {}, "tpex": {}}
    trained = skipped = 0
    for code, entry in today.items():
        market = str(entry.get("market") or "twse")
        actual = float(full_day_lots.get(code) or 0)
        average = float(avg20_lots.get(code) or 0)
        if actual <= 0 or actual < MIN_TRAIN_LOTS or (average > 0 and actual > average * ABNORMAL_VOLUME_X):
            skipped += 1                                  # 異常量或量太小：不訓練曲線
            continue
        trained += 1
        for bucket, cumulative in (entry.get("points") or {}).items():
            try:
                ratio = float(cumulative) / actual
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if 0 < ratio <= 1.02:
                ratios.setdefault(market, {}).setdefault(int(bucket), []).append(min(ratio, 1.0))
        for bucket, predicted in (entry.get("pred") or {}).items():
            try:
                errors.setdefault(market, {}).setdefault(int(bucket), []).append(
                    abs(float(predicted) / actual - 1) * 100)
            except (TypeError, ValueError, ZeroDivisionError):
                continue

    # 每日每市場每時點先取跨股票中位數，保留最近 KEEP_DAYS 日
    daily = _state(DAILY_KEY, {})
    for market, buckets in ratios.items():
        if not buckets:
            continue
        per_day = daily.setdefault(market, {})
        per_day[day] = {str(b): round(statistics.median(v), 4) for b, v in buckets.items() if len(v) >= 3}
        for old in sorted(per_day)[:-KEEP_DAYS]:
            per_day.pop(old, None)

    curves = _state(CURVE_KEY, {})
    summary = {}
    for market in ("twse", "tpex"):
        per_day = daily.get(market) or {}
        if len(per_day) < MIN_DAYS_BLEND:
            summary[market] = f"樣本 {len(per_day)} 日（未達 {MIN_DAYS_BLEND} 日，仍用預設曲線）"
            curves.setdefault(market, {}).update({"days": len(per_day)})
            continue
        targets: Dict[int, float] = {}
        for values in per_day.values():
            for bucket, ratio in values.items():
                targets.setdefault(int(bucket), []).append(float(ratio))
        current = {int(k): float(v) for k, v in ((curves.get(market) or {}).get("points") or {}).items()}
        points: Dict[int, float] = {}
        for bucket, values in targets.items():
            if len(values) < MIN_DAYS_BLEND:
                continue
            target = statistics.median(values)
            old = current.get(bucket, _interpolate(DEFAULT_CURVES[market], bucket) or target)
            step = max(-MAX_STEP, min(MAX_STEP, target - old))
            points[bucket] = round(min(0.999, max(0.001, old + step)), 4)
        if not points:
            summary[market] = "有效時點不足"
            continue
        # 單調性修正：後面的時點不可低於前面（各 bucket 獨立取中位數可能倒退）
        running = 0.0
        for bucket in sorted(points):
            running = max(running, points[bucket])
            points[bucket] = round(running, 4)
        points[CLOSE_MINUTES] = 1.0
        curves[market] = {"points": {str(k): v for k, v in points.items()},
                          "days": len(per_day), "at": time.time()}
        summary[market] = f"{len(points)} 個時點｜樣本 {len(per_day)} 日"

    # 誤差統計（近 KEEP_DAYS 日中位數），用來決定輸出語氣
    error_state = _state(ERROR_KEY, {})
    error_hist = _state(ERROR_KEY + "_hist", {})
    for market, buckets in errors.items():
        per_day = error_hist.setdefault(market, {})
        per_day[day] = {str(b): round(statistics.median(v), 2) for b, v in buckets.items() if v}
        for old in sorted(per_day)[:-KEEP_DAYS]:
            per_day.pop(old, None)
        merged: Dict[str, List[float]] = {}
        for values in per_day.values():
            for bucket, err in values.items():
                merged.setdefault(bucket, []).append(float(err))
        error_state[market] = {b: round(statistics.median(v), 2) for b, v in merged.items() if v}
    # 四份學習結果與新舊樣本刪除一起提交；寫入或清除失敗時全部回滾。
    local_market_cache.save_ivol_learning_state(day, daily, curves, error_state, error_hist)
    print(f"📏 盤中量能曲線校正｜{day}｜訓練 {trained} 檔／排除 {skipped} 檔｜" +
          "｜".join(f"{m}：{t}" for m, t in summary.items()), flush=True)
    return {"updated": len(summary), "trained": trained, "skipped": skipped, "summary": summary}


def error_report() -> str:
    """各時點的近期估算誤差，供 /ace 系統狀態或 log 檢視。"""
    errors = _state(ERROR_KEY, {})
    lines = []
    for market in ("twse", "tpex"):
        points = errors.get(market) or {}
        if not points:
            continue
        _curve, source, days = curve_for(market)
        lines.append(f"{market.upper()}｜{source}")
        for bucket in sorted(points, key=lambda b: int(b))[:: max(1, len(points) // 6)]:
            minute = int(bucket)
            lines.append(f"  {minute // 60:02d}:{minute % 60:02d}｜誤差中位數 {points[bucket]:.1f}%")
    return "\n".join(lines) or "尚無估算誤差統計"
