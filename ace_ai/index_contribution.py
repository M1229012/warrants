"""指數貢獻點數：誰把加權／櫃買指數拉上去、誰把它拖下來。

核心不是單看漲跌幅，而是把個股在指數中的權重一起算進來：

    某股貢獻點數 = 昨日指數 × (今日漲跌價 × 發行股數) ÷ 昨日總市值

資料與執行策略：
- 發行股數：TWSE／TPEx 公開資料，寫入本地快取，每週更新即可。
- 收盤後：使用本地全市場日 K，直接計算完整上市／上櫃普通股貢獻，0 個盤中行情請求。
- 盤中：使用 TWSE MIS 官方即時報價「批次」抓取，依昨日市值由大到小逐批補價；
  一旦未抓股票依一般 ±10% 漲跌幅上限也不可能擠進正／負貢獻 TOP5，就提前停止。
  同時設總時間預算，連線慢時直接回傳目前已取得結果，不讓 Discord 問答卡死。
- 若 MIS 完全失敗，才退回原本少量 Fugle 單股即時報價；不逐檔掃全市場。

盤中結果一律標示「估算」與市值涵蓋率；收盤後才稱完整收盤計算。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import local_market_cache
import warrant_ai_tools as tools

TWSE_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INFO_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "application/json"}
MIS_HEADERS = {
    "User-Agent": "Mozilla/5.0 AceAI/1.0",
    "Accept": "application/json",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}
SHARES_STATE_KEY = "index_shares"
SHARES_TTL_DAYS = max(1, tools._env_int("DISCORD_AI_SHARES_TTL_DAYS", 7))
SHARES_HTTP_TIMEOUT = max(3.0, tools._env_float("DISCORD_AI_SHARES_HTTP_TIMEOUT", 7.0))
_SHARES_REFRESH_LOCK = threading.Lock()
_SHARES_REFRESHING = [False]

# 舊設定保留作 MIS 完全失敗時的少量 Fugle 備援，不拿它當全市場 TOP5。
LIVE_TOP_TWSE = max(3, tools._env_int("DISCORD_AI_CONTRIB_LIVE_TWSE", 12))
LIVE_TOP_TPEX = max(3, tools._env_int("DISCORD_AI_CONTRIB_LIVE_TPEX", 6))

# 盤中批次抓取：總時間預算是「加權＋櫃買合計」，避免網路慢時 Discord 逾時。
LIVE_BUDGET_SECONDS = max(3.0, tools._env_float("DISCORD_AI_CONTRIB_BUDGET", 9.0))
LIVE_BATCH_SIZE = max(20, tools._env_int("DISCORD_AI_CONTRIB_BATCH_SIZE", 70))
LIVE_MAX_TWSE = max(LIVE_BATCH_SIZE, tools._env_int("DISCORD_AI_CONTRIB_MAX_TWSE", 350))
LIVE_MAX_TPEX = max(LIVE_BATCH_SIZE, tools._env_int("DISCORD_AI_CONTRIB_MAX_TPEX", 280))
LIVE_MIS_TIMEOUT = max(2.0, tools._env_float("DISCORD_AI_CONTRIB_MIS_TIMEOUT", 4.5))
LIVE_CACHE_SECONDS = max(5, tools._env_int("DISCORD_AI_CONTRIB_CACHE_SECONDS", 20))
# 一般上市櫃普通股每日 ±10%；只拿來判斷「未抓股票理論上是否仍可能擠進 TOP5」。
DAILY_LIMIT_PCT = max(10.0, tools._env_float("DISCORD_AI_CONTRIB_DAILY_LIMIT_PCT", 10.0))

INDEX_OF = {"twse": ("TAIEX", "加權指數"), "tpex": ("TPEX", "櫃買指數")}
MIS_PREFIX = {"twse": "tse", "tpex": "otc"}
MIS_BENCHMARK = {"twse": "tse_t00.tw", "tpex": "otc_o00.tw"}

# 同一時間很多會員問同一題時，共享 20 秒批次報價快取，避免把官方端點打爆。
_MIS_LOCK = threading.Lock()
_MIS_CACHE: Dict[str, Dict[str, Any]] = {
    "twse": {"at": 0.0, "quotes": {}, "benchmark": {}},
    "tpex": {"at": 0.0, "quotes": {}, "benchmark": {}},
}


def _number(value: Any) -> float:
    try:
        text = str(value).replace(",", "").replace("+", "").strip()
        if text in ("", "-", "--", "---", "null", "None"):
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _fetch_shares() -> Dict[str, List[Any]]:
    """{代號: [市場, 發行股數]}；兩個公開端點，各一個請求。"""
    session = tools.core().get_thread_session()
    shares: Dict[str, List[Any]] = {}
    started = time.perf_counter()
    response = session.get(TWSE_INFO_URL, headers=HEADERS, timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
    response.raise_for_status()
    for row in response.json() or []:
        code = str(row.get("公司代號") or "").strip()
        count = _number(row.get("已發行普通股數或TDR原股發行股數"))
        if code and count > 0:
            shares[code] = ["twse", count]
    tools.record_api_event("TWSE-OpenAPI", status=200, latency=time.perf_counter() - started)

    started = time.perf_counter()
    response = session.get(TPEX_INFO_URL, headers=HEADERS, timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
    response.raise_for_status()
    for row in response.json() or []:
        code = str(row.get("SecuritiesCompanyCode") or "").strip()
        capital = _number(row.get("Paidin.Capital.NTDollars"))
        par = _number(row.get("ParValueOfCommonStock")) or 10.0
        if code and capital > 0:
            shares[code] = ["tpex", capital / par]
    tools.record_api_event("TPEx-OpenAPI", status=200, latency=time.perf_counter() - started)
    return shares


def _refresh_shares_background() -> None:
    """舊快取過期時背景更新；會員查詢先用舊資料，不被兩個公開端點卡住。"""
    with _SHARES_REFRESH_LOCK:
        if _SHARES_REFRESHING[0]:
            return
        _SHARES_REFRESHING[0] = True

    def worker() -> None:
        try:
            shares = _fetch_shares()
            if shares:
                local_market_cache.set_state(SHARES_STATE_KEY, {"at": time.time(), "shares": shares})
                print(
                    f"📐 發行股數背景更新完成：上市 {sum(1 for v in shares.values() if v[0] == 'twse'):,} 檔｜"
                    f"上櫃 {sum(1 for v in shares.values() if v[0] == 'tpex'):,} 檔",
                    flush=True,
                )
        except Exception as exc:
            print(f"⚠️ 發行股數背景更新失敗，繼續沿用舊資料｜{type(exc).__name__}", flush=True)
        finally:
            with _SHARES_REFRESH_LOCK:
                _SHARES_REFRESHING[0] = False

    threading.Thread(target=worker, name="ace-index-shares-refresh", daemon=True).start()


def share_counts(refresh: bool = False) -> Dict[str, List[Any]]:
    """發行股數（含市場別）。有舊快取時採 stale-while-revalidate，避免會員查詢連線逾時。"""
    state = local_market_cache.get_state(SHARES_STATE_KEY, {}) or {}
    fresh = False
    try:
        fresh = (time.time() - float(state.get("at", 0))) < SHARES_TTL_DAYS * 86400
    except (TypeError, ValueError):
        fresh = False
    old = dict(state.get("shares") or {})
    if old and fresh and not refresh:
        return old
    if old and not refresh:
        _refresh_shares_background()
        return old
    try:
        shares = _fetch_shares()
    except Exception as exc:
        print(f"⚠️ 發行股數更新失敗，沿用舊資料｜{type(exc).__name__}", flush=True)
        return old
    if shares:
        local_market_cache.set_state(SHARES_STATE_KEY, {"at": time.time(), "shares": shares})
        print(
            f"📐 發行股數已更新：上市 {sum(1 for v in shares.values() if v[0] == 'twse'):,} 檔｜"
            f"上櫃 {sum(1 for v in shares.values() if v[0] == 'tpex'):,} 檔",
            flush=True,
        )
    return shares or old


def _index_prev_close(index_code: str) -> Tuple[Optional[float], Optional[float], str]:
    """備援：(昨收指數, 最新指數, 資料日)。盤中優先使用 MIS benchmark。"""
    try:
        bundle = tools._load_price_bundle(index_code)
    except Exception:
        return None, None, ""
    frame = bundle.get("df")
    if frame is None or len(frame) < 2:
        return None, None, ""
    closed = tools.closed_frame(bundle)
    prev = float(closed["Close"].iloc[-1]) if bundle.get("intraday") else float(closed["Close"].iloc[-2])
    now = float(frame["Close"].iloc[-1])
    return prev, now, str(frame.index[-1].date())


def _cache_bucket(market: str) -> Dict[str, Any]:
    bucket = _MIS_CACHE[market]
    if time.time() - float(bucket.get("at") or 0.0) > LIVE_CACHE_SECONDS:
        bucket = {"at": time.time(), "quotes": {}, "benchmark": {}}
        _MIS_CACHE[market] = bucket
    return bucket


def _fetch_mis_batch(market: str, codes: List[str], deadline: float) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """一次 MIS GET 抓一批股票；同時帶 benchmark，回傳 quotes + 指數昨收/現值。"""
    if not codes or time.monotonic() >= deadline:
        return {}, {}

    # 先看 20 秒共享快取。網路呼叫放在 lock 內，併發會員會等第一個人抓完後直接共用結果。
    wait = max(0.05, deadline - time.monotonic())
    acquired = _MIS_LOCK.acquire(timeout=wait)
    if not acquired:
        bucket = _cache_bucket(market)
        return {c: bucket["quotes"][c] for c in codes if c in bucket["quotes"]}, dict(bucket.get("benchmark") or {})
    try:
        bucket = _cache_bucket(market)
        missing = [c for c in codes if c not in bucket["quotes"]]
        if not missing:
            return {c: bucket["quotes"][c] for c in codes if c in bucket["quotes"]}, dict(bucket.get("benchmark") or {})
        remaining = deadline - time.monotonic()
        if remaining <= 0.15:
            return {c: bucket["quotes"][c] for c in codes if c in bucket["quotes"]}, dict(bucket.get("benchmark") or {})

        prefix = MIS_PREFIX[market]
        channels = [f"{prefix}_{code}.tw" for code in missing]
        # benchmark 只多十幾個字，順手一起拿，避免另外打一個指數 API。
        channels.append(MIS_BENCHMARK[market])
        timeout = min(LIVE_MIS_TIMEOUT, max(0.5, remaining))
        session = tools.core().get_thread_session()
        started = time.perf_counter()
        status = 0
        try:
            response = session.get(
                MIS_URL,
                params={"ex_ch": "|".join(channels), "json": "1", "delay": "0"},
                headers=MIS_HEADERS,
                timeout=(min(2.5, timeout), timeout),
            )
            status = int(response.status_code)
            response.raise_for_status()
            payload = response.json() or {}
        finally:
            try:
                tools.record_api_event("TWSE-MIS", status=status, latency=time.perf_counter() - started)
            except Exception:
                pass

        for item in payload.get("msgArray") or []:
            channel = str(item.get("ch") or "")
            if channel == MIS_BENCHMARK[market]:
                prev = _number(item.get("y"))
                current = _number(item.get("z")) or _number(item.get("o"))
                if prev > 0:
                    bucket["benchmark"] = {
                        "prev": prev,
                        "current": current if current > 0 else prev,
                        "time": str(item.get("t") or "")[:5],
                    }
                continue
            code = str(item.get("c") or "").strip()
            if not code:
                # 某些回應 c 為空，從 channel 回推代號。
                try:
                    code = channel.split("_", 1)[1].split(".", 1)[0]
                except Exception:
                    code = ""
            if not code:
                continue
            current = _number(item.get("z"))
            previous = _number(item.get("y"))
            if previous <= 0:
                continue
            if current <= 0:
                # 尚未成交時維持昨收，不拿開盤價硬當最新價。
                current = previous
            bucket["quotes"][code] = {
                "close": current,
                "prev_close": previous,
                "change_pct": (current / previous - 1.0) * 100.0,
                "time": str(item.get("t") or "")[:5],
            }
        bucket["at"] = time.time()
        return {c: bucket["quotes"][c] for c in codes if c in bucket["quotes"]}, dict(bucket.get("benchmark") or {})
    finally:
        _MIS_LOCK.release()


def _fugle_fallback(rows: List[Dict[str, Any]], market: str, deadline: float) -> int:
    """MIS 完全失敗才用舊的少量 Fugle 單股報價；硬時間上限，不逐檔掃市場。"""
    limit = LIVE_TOP_TWSE if market == "twse" else LIVE_TOP_TPEX
    ranked = sorted(rows, key=lambda r: -r["prev_value"])[:limit]
    used = 0
    for row in ranked:
        if time.monotonic() >= deadline:
            break
        try:
            overview = tools.get_stock_overview(row["stock_code"])
        except Exception:
            continue
        close = tools._num(overview.get("close"))
        if close and close > 0:
            row["close"] = float(close)
            row["change_pct"] = (float(close) / row["prev_close"] - 1.0) * 100.0
            row["live_quote"] = True
            used += 1
    return used


def _rank_safety(rows: List[Dict[str, Any]], prev_index: float, top_n: int) -> Tuple[bool, bool, float]:
    """依未抓成分股的最大理論貢獻，判斷正／負 TOP N 是否已不可能被插隊。"""
    quoted = [r for r in rows if r.get("live_quote")]
    unquoted = [r for r in rows if not r.get("live_quote")]
    positives = sorted((r["points"] for r in quoted if r["points"] > 0), reverse=True)
    negatives = sorted((abs(r["points"]) for r in quoted if r["points"] < 0), reverse=True)
    if not unquoted:
        return True, True, 0.0
    max_weight = max((r["weight_pct"] for r in unquoted), default=0.0) / 100.0
    unseen_bound = prev_index * max_weight * (DAILY_LIMIT_PCT / 100.0)
    top_safe = len(positives) >= top_n and positives[top_n - 1] > unseen_bound
    bottom_safe = len(negatives) >= top_n and negatives[top_n - 1] > unseen_bound
    return top_safe, bottom_safe, unseen_bound


def _concentration_label(share: Optional[float]) -> str:
    if share is None:
        return "資料不足"
    if share >= 70:
        return "高度集中"
    if share >= 50:
        return "偏集中"
    return "較分散"


def contribution(market: str, live: bool, top: int = 5, *, deadline: Optional[float] = None) -> Dict[str, Any]:
    """單一市場的指數貢獻點數。"""
    if market not in INDEX_OF:
        raise ValueError(f"unsupported market: {market}")
    index_code, index_name = INDEX_OF[market]
    shares = share_counts()
    codes = [code for code, info in shares.items() if info and info[0] == market]
    if not codes:
        raise tools.ToolDataError("沒有發行股數資料，無法計算指數貢獻")

    changes = local_market_cache.latest_changes(codes)
    rows: List[Dict[str, Any]] = []
    total_prev_value = 0.0
    for code in codes:
        info = changes.get(code) or {}
        last_close = tools._num(info.get("close"))
        pct = tools._num(info.get("change_pct"))
        if not last_close or pct is None:
            continue
        if live:
            # 盤中的基準必須是「上一個已收盤交易日」，不能再往前推一天。
            prev_close = float(last_close)
            close = float(last_close)
            change_pct = 0.0
        else:
            if float(pct) == -100:
                continue
            prev_close = float(last_close) / (1.0 + float(pct) / 100.0)
            close = float(last_close)
            change_pct = float(pct)
        if prev_close <= 0:
            continue
        count = float(shares[code][1])
        prev_value = prev_close * count
        total_prev_value += prev_value
        rows.append({
            "stock_code": code,
            "prev_close": prev_close,
            "close": close,
            "change_pct": change_pct,
            "shares": count,
            "prev_value": prev_value,
            "live_quote": False,
        })
    if not rows or total_prev_value <= 0:
        raise tools.ToolDataError("本地底庫資料不足，無法計算指數貢獻")

    prev_index: Optional[float] = None
    now_index: Optional[float] = None
    data_date = ""
    live_used = 0
    top_safe = bottom_safe = not live
    unseen_bound = 0.0

    if live:
        deadline = deadline or (time.monotonic() + LIVE_BUDGET_SECONDS)
        ranked = sorted(rows, key=lambda r: -r["prev_value"])
        max_candidates = LIVE_MAX_TWSE if market == "twse" else LIVE_MAX_TPEX
        ranked = ranked[:max_candidates]
        benchmark: Dict[str, Any] = {}

        for start in range(0, len(ranked), LIVE_BATCH_SIZE):
            if time.monotonic() >= deadline:
                break
            batch = ranked[start:start + LIVE_BATCH_SIZE]
            try:
                quotes, batch_benchmark = _fetch_mis_batch(
                    market, [r["stock_code"] for r in batch], deadline
                )
            except Exception as exc:
                print(f"⚠️ {index_name} MIS 批次報價失敗｜{type(exc).__name__}", flush=True)
                quotes, batch_benchmark = {}, {}
            if batch_benchmark:
                benchmark = batch_benchmark
            for row in batch:
                quote = quotes.get(row["stock_code"])
                if not quote:
                    continue
                current = tools._num(quote.get("close"))
                if not current or current <= 0:
                    continue
                row["close"] = float(current)
                row["change_pct"] = (float(current) / row["prev_close"] - 1.0) * 100.0
                row["live_quote"] = True
                live_used += 1

            if benchmark.get("prev"):
                prev_index = float(benchmark["prev"])
                now_index = float(benchmark.get("current") or benchmark["prev"])
                data_date = tools.taipei_now().strftime("%Y-%m-%d")
            elif prev_index is None:
                prev_index, now_index, data_date = _index_prev_close(index_code)

            if prev_index:
                factor = prev_index / total_prev_value
                for row in rows:
                    row["weight_pct"] = row["prev_value"] / total_prev_value * 100.0
                    row["points"] = (row["close"] - row["prev_close"]) * row["shares"] * factor
                top_safe, bottom_safe, unseen_bound = _rank_safety(rows, prev_index, top)
                if top_safe and bottom_safe:
                    break

        # MIS 一檔都沒拿到才退回舊的少量 Fugle；仍受同一個總 deadline 控制。
        if live_used == 0 and time.monotonic() < deadline:
            live_used = _fugle_fallback(rows, market, deadline)
        if prev_index is None:
            prev_index, now_index, data_date = _index_prev_close(index_code)
    else:
        prev_index, now_index, data_date = _index_prev_close(index_code)

    if not prev_index:
        raise tools.ToolDataError("指數基準資料不足，無法計算指數貢獻")

    factor = prev_index / total_prev_value
    names: Dict[str, str] = {}
    try:
        names = tools.get_stock_name_map() or {}
    except Exception:
        names = {}
    for row in rows:
        row["points"] = round((row["close"] - row["prev_close"]) * row["shares"] * factor, 2)
        row["stock_name"] = names.get(row["stock_code"], row["stock_code"])
        row["weight_pct"] = round(row["prev_value"] / total_prev_value * 100.0, 2)
        row["change_pct"] = round(float(row["change_pct"]), 2)
        row["close"] = round(float(row["close"]), 2)

    evaluated = [r for r in rows if r.get("live_quote")] if live else list(rows)
    positives = sorted((r for r in evaluated if r["points"] > 0), key=lambda r: -r["points"])
    negatives = sorted((r for r in evaluated if r["points"] < 0), key=lambda r: r["points"])
    top_rows = positives[:top]
    bottom_rows = negatives[:top]
    keep = ("stock_code", "stock_name", "points", "change_pct", "close", "weight_pct")

    positive_total = sum(r["points"] for r in positives)
    negative_total = abs(sum(r["points"] for r in negatives))
    top_lift = sum(r["points"] for r in top_rows)
    top_drag = sum(r["points"] for r in bottom_rows)  # 負值
    positive_share = (top_lift / positive_total * 100.0) if positive_total > 0 else None
    negative_share = (abs(top_drag) / negative_total * 100.0) if negative_total > 0 else None
    covered_prev_value = sum(r["prev_value"] for r in evaluated)
    market_cap_coverage = covered_prev_value / total_prev_value * 100.0 if total_prev_value > 0 else 0.0
    estimated_points = round(sum(r["points"] for r in evaluated), 2)
    index_points = round((now_index - prev_index), 2) if now_index is not None else None

    # 指數上漲時看「拉升集中度」，下跌時看「拖累集中度」。
    focus_share = positive_share if (index_points or 0) >= 0 else negative_share
    concentration = _concentration_label(focus_share)
    if live and not (top_safe and bottom_safe):
        concentration += "（盤中估算）"

    return {
        "market": market,
        "index_name": index_name,
        "index_prev_close": round(prev_index, 2),
        "index_now": round(now_index, 2) if now_index is not None else None,
        "index_points": index_points,
        "estimated_points": estimated_points,
        "residual_points": round(index_points - estimated_points, 2) if index_points is not None else None,
        "basis": "盤中估算" if live else "收盤精算",
        "data_date": data_date,
        "components": len(rows),
        "components_evaluated": len(evaluated),
        "live_quotes": live_used,
        "market_cap_coverage_pct": round(market_cap_coverage, 1),
        "top5_lift_points": round(top_lift, 2),
        "top5_drag_points": round(top_drag, 2),
        "top5_positive_share_pct": round(positive_share, 1) if positive_share is not None else None,
        "top5_negative_share_pct": round(negative_share, 1) if negative_share is not None else None,
        "concentration": concentration,
        "top5_positive_certified": bool(top_safe),
        "top5_negative_certified": bool(bottom_safe),
        "unseen_max_points_bound": round(unseen_bound, 2),
        "top": [{k: r[k] for k in keep} for r in top_rows],
        "bottom": [{k: r[k] for k in keep} for r in bottom_rows],
    }


def report(live: Optional[bool] = None, top: int = 5) -> Dict[str, Any]:
    """加權與櫃買一起計算；盤中共享一個總時間預算，避免連線慢時拖垮問答。"""
    if live is None:
        now = tools.taipei_now()
        live = now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute <= 13 * 60 + 35
    out: Dict[str, Any] = {"basis": "盤中估算" if live else "收盤精算", "markets": []}
    overall_deadline = time.monotonic() + LIVE_BUDGET_SECONDS if live else None
    markets_order = ("twse", "tpex")
    for idx, market in enumerate(markets_order):
        # 平分「剩餘」時間，保證加權不會把整個 budget 吃光而讓櫃買沒有即時榜單。
        if live and overall_deadline is not None:
            now_mono = time.monotonic()
            remaining = max(0.2, overall_deadline - now_mono)
            markets_left = len(markets_order) - idx
            market_deadline = now_mono + remaining / max(1, markets_left)
        else:
            market_deadline = None
        try:
            out["markets"].append(contribution(market, live=bool(live), top=top, deadline=market_deadline))
        except Exception as exc:
            print(f"⚠️ {INDEX_OF[market][1]} 貢獻點數略過：{type(exc).__name__}: {exc}", flush=True)
    if not out["markets"]:
        raise tools.ToolDataError("目前無法計算指數貢獻點數")

    twse = next((m for m in out["markets"] if m.get("market") == "twse"), None)
    tpex = next((m for m in out["markets"] if m.get("market") == "tpex"), None)
    if twse:
        direction = "拉升" if (twse.get("index_points") or 0) >= 0 else "拖累"
        share = twse.get("top5_positive_share_pct") if direction == "拉升" else twse.get("top5_negative_share_pct")
        if share is not None:
            out["structure_summary"] = (
                f"加權指數前五大{direction}股占已涵蓋{'正' if direction == '拉升' else '負'}貢獻約 {share:.1f}%"
                f"，屬{twse.get('concentration', '')}。"
            )
        if tpex and twse.get("index_points") is not None and tpex.get("index_points") is not None:
            out["structure_summary"] = (out.get("structure_summary") or "") + (
                f" 加權 {twse['index_points']:+.2f} 點；櫃買 {tpex['index_points']:+.2f} 點。"
            )

    out["method_note"] = (
        "貢獻點數＝昨日指數 ×（漲跌價 × 發行股數）÷ 昨日總市值；"
        "收盤後用全市場日K完整計算。盤中用官方 MIS 批次報價，依市值逐批補價並設時間上限，"
        "畫面會標示市值涵蓋率與是否仍屬估算。"
    )
    return out
