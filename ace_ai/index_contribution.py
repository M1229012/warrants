"""指數貢獻點數：誰把加權／櫃買指數拉上去、誰把它拖下來。

核心不是單看漲跌幅，而是依交易所的發行量加權公式，把所有指數成分股納入後計算個股對指數點數的影響。

官方公式：指數 = 成分股總發行市值 ÷ 當日基值 × 100。
因此收盤後以官方收盤指數與完整成分母體的收盤總市值反推當日換算係數，再計算每檔：

    某股貢獻點數 = (今日計算價格 - 前一交易日價格) × 當日發行股數 × 當日換算係數

資料與執行策略：
- TPEx：優先直接使用櫃買中心公開的「櫃買指數成分股」名冊與發行股數。
- TAIEX：依證交所正式編製要點，以所有符合納入規則的上市普通股與官方發行股數重建；
  證交所每日精確成分權重檔屬 Data E-Shop 商品，未取得該授權檔時不宣稱為官方公布權重。
- 收盤後：使用本地全市場官方日 K，所有可辨識成分股都參與計算，不做前 N 大權值股截斷。
- 盤中：使用 TWSE MIS 官方即時報價「批次」抓取，依昨日市值由大到小逐批補價；
  一旦未抓股票依一般 ±10% 漲跌幅上限也不可能擠進正／負貢獻 TOP5，就提前停止。
  同時設總時間預算，連線慢時直接回傳目前已取得結果，不讓 Discord 問答卡死。
- 若 MIS 完全失敗，才退回原本少量 Fugle 單股即時報價；不逐檔掃全市場。

盤中結果一律標示「估算」與市值涵蓋率；收盤後依完整母體計算，並在 log 留下成分數、價格覆蓋率與指數殘差供對帳。
"""
from __future__ import annotations

import csv
import io
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import local_market_cache
import warrant_ai_tools as tools

TWSE_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INFO_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TPEX_CONSTITUENTS_URL = "https://www.tpex.org.tw/openapi/v1/tpex_index_consti"
TWSE_NEWLIST_CSV_URL = "https://www.twse.com.tw/company/newlisting?response=open_data"
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "application/json"}
MIS_HEADERS = {
    "User-Agent": "Mozilla/5.0 AceAI/1.0",
    "Accept": "application/json",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}
SHARES_STATE_KEY = "index_components_v16"
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


def _parse_yyyymmdd(value: Any) -> Optional[date]:
    text = str(value or "").strip().replace("/", "").replace("-", "")
    if not text:
        return None
    try:
        if len(text) == 7 and text.isdigit():  # ROC yyyMMdd
            return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None
    return None


def _twse_normal_inclusion_date(listed: date) -> date:
    """官方規則：新上市滿一個完整日曆月後，於次月第一個交易日納入。

    這裡先算該月曆月的月初日期；實際第一交易日由『查詢日 >= 此日期』判斷。
    例：7/23 上市 -> 9/1 起具備一般納入資格；8/11 上市 -> 10/1 起。
    """
    year, month = listed.year, listed.month + 2
    while month > 12:
        year += 1
        month -= 12
    return date(year, month, 1)


def _fetch_twse_newlisting_notes(session) -> Dict[str, str]:
    """最近上市公司備註，用來辨識『櫃轉市等上市當日即納入』的官方例外。"""
    try:
        response = session.get(TWSE_NEWLIST_CSV_URL, headers=HEADERS,
                               timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
        response.raise_for_status()
        text = response.content.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        out: Dict[str, str] = {}
        for row in rows:
            code = str(row.get("公司代號") or "").strip()
            if code:
                out[code] = str(row.get("備註") or "").strip()
        return out
    except Exception:
        return {}


def _twse_include_now(code: str, listed: Optional[date], note: str, as_of: date) -> bool:
    if listed is None:
        return True
    # 編製要點明定的上市當日納入例外；近期上市清單能明確辨識者直接採用。
    immediate_words = ("櫃轉市", "金融控股", "投資控股", "分割", "轉換股份", "新設公司")
    if any(word in str(note or "") for word in immediate_words):
        return as_of >= listed
    return as_of >= _twse_normal_inclusion_date(listed)


def _find_key(row: Dict[str, Any], needles: Tuple[str, ...]) -> str:
    for key in row.keys():
        low = str(key).lower().replace(" ", "")
        if all(n.lower().replace(" ", "") in low for n in needles):
            return str(key)
    return ""


def _parse_tpex_constituents(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """解析 TPEx 官方『櫃買指數成分股』OpenAPI；欄位名稱改版時仍盡量自動辨識。"""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        code = str(
            row.get("SecuritiesCompanyCode") or row.get("Code") or row.get("代號")
            or row.get("股票代號") or row.get("證券代號") or ""
        ).strip()
        if not (len(code) == 4 and code.isdigit()):
            continue
        name = str(
            row.get("CompanyAbbreviation") or row.get("Name") or row.get("名稱")
            or row.get("股票名稱") or row.get("證券名稱") or code
        ).strip()
        share_key = ""
        for key in row.keys():
            low = str(key).lower().replace(" ", "")
            if (("發行" in low and "股" in low) or "issueshares" in low
                    or ("numberofshares" in low and "issue" in low)):
                share_key = str(key)
                break
        shares = _number(row.get(share_key)) if share_key else 0.0
        if shares <= 0:
            continue
        # 官網表格常以『發行仟股數』呈現；OpenAPI 若欄名明示千/仟股就換算為股。
        if share_key and any(word in share_key for word in ("仟", "千")):
            shares *= 1000.0
        out[code] = {"market": "tpex", "shares": shares, "name": name,
                     "source": "TPEx官方櫃買指數成分股OpenAPI", "exact_membership": True}
    return out


def _fetch_components() -> Dict[str, Dict[str, Any]]:
    """官方資料建立指數母體。

    TPEx：直接使用官方『櫃買指數成分股』OpenAPI（成分股＋發行股數）。
    TWSE：免費 OpenAPI 沒有提供每日 TAIEX 官方成分權重檔；依官方編製要點，用
          上市公司基本資料的普通股發行股數＋上市日期規則重建母體。官方每日成分權重檔
          TWTANU_TAI/TWT73U1 為 Data E-Shop 付費資料，程式不偽裝成已取得該檔。
    """
    session = tools.core().get_thread_session()
    today = tools.taipei_now().date()
    components: Dict[str, Dict[str, Any]] = {}

    started = time.perf_counter()
    response = session.get(TWSE_INFO_URL, headers=HEADERS,
                           timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
    response.raise_for_status()
    twse_rows = response.json() or []
    tools.record_api_event("TWSE-OpenAPI", status=200, latency=time.perf_counter() - started)
    newlisting_notes = _fetch_twse_newlisting_notes(session)
    for row in twse_rows:
        code = str(row.get("公司代號") or "").strip()
        shares = _number(row.get("已發行普通股數或TDR原股發行股數"))
        if not (len(code) == 4 and code.isdigit() and shares > 0):
            continue
        listed = _parse_yyyymmdd(row.get("上市日期"))
        if not _twse_include_now(code, listed, newlisting_notes.get(code, ""), today):
            continue
        components[code] = {
            "market": "twse", "shares": shares,
            "name": str(row.get("公司簡稱") or code).strip(),
            "listed_date": listed.isoformat() if listed else "",
            "source": "TWSE官方編製規則＋上市公司基本資料", "exact_membership": False,
        }

    # TPEx 先拿『指數成分股』官方端點；若端點暫時不可用才用官方公司基本資料備援。
    tpex_exact: Dict[str, Dict[str, Any]] = {}
    try:
        started = time.perf_counter()
        response = session.get(TPEX_CONSTITUENTS_URL, headers=HEADERS,
                               timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
        response.raise_for_status()
        tpex_exact = _parse_tpex_constituents(response.json() or [])
        tools.record_api_event("TPEx-OpenAPI", status=200, latency=time.perf_counter() - started)
        # 正常櫃買指數是數百檔；若官方端點只回到不合理的小片段，不把它當完整母體。
        if len(tpex_exact) < 100:
            print(f"⚠️ TPEx 指數成分股回傳僅 {len(tpex_exact)} 檔，視為不完整，改用官方公司基本資料備援", flush=True)
            tpex_exact = {}
    except Exception as exc:
        print(f"⚠️ TPEx 指數成分股端點失敗，改用官方公司基本資料備援｜{type(exc).__name__}", flush=True)

    if tpex_exact:
        components.update(tpex_exact)
    else:
        started = time.perf_counter()
        response = session.get(TPEX_INFO_URL, headers=HEADERS,
                               timeout=(min(3.0, SHARES_HTTP_TIMEOUT), SHARES_HTTP_TIMEOUT))
        response.raise_for_status()
        for row in response.json() or []:
            code = str(row.get("SecuritiesCompanyCode") or "").strip()
            shares = _number(row.get("IssueShares"))
            if not (len(code) == 4 and code.isdigit() and shares > 0):
                continue
            components[code] = {
                "market": "tpex", "shares": shares,
                "name": str(row.get("CompanyAbbreviation") or code).strip(),
                "source": "TPEx官方公司基本資料備援", "exact_membership": False,
            }
        tools.record_api_event("TPEx-OpenAPI", status=200, latency=time.perf_counter() - started)
    return components


def _refresh_shares_background() -> None:
    """舊快取過期時背景更新；會員查詢先用舊資料，不被官方端點卡住。"""
    with _SHARES_REFRESH_LOCK:
        if _SHARES_REFRESHING[0]:
            return
        _SHARES_REFRESHING[0] = True

    def worker() -> None:
        try:
            components = _fetch_components()
            if components:
                local_market_cache.set_state(SHARES_STATE_KEY, {"at": time.time(), "components": components})
                print(
                    f"📐 指數母體背景更新完成：上市 {sum(1 for v in components.values() if v.get('market') == 'twse'):,} 檔｜"
                    f"上櫃 {sum(1 for v in components.values() if v.get('market') == 'tpex'):,} 檔",
                    flush=True,
                )
        except Exception as exc:
            print(f"⚠️ 指數母體背景更新失敗，繼續沿用舊資料｜{type(exc).__name__}", flush=True)
        finally:
            with _SHARES_REFRESH_LOCK:
                _SHARES_REFRESHING[0] = False

    threading.Thread(target=worker, name="ace-index-components-refresh", daemon=True).start()


def component_universe(refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """指數成分母體（官方資料）；有舊快取時 stale-while-revalidate。"""
    state = local_market_cache.get_state(SHARES_STATE_KEY, {}) or {}
    fresh = False
    try:
        fresh = (time.time() - float(state.get("at", 0))) < SHARES_TTL_DAYS * 86400
    except (TypeError, ValueError):
        fresh = False
    old = dict(state.get("components") or {})
    if old and fresh and not refresh:
        return old
    if old and not refresh:
        _refresh_shares_background()
        return old
    try:
        components = _fetch_components()
    except Exception as exc:
        print(f"⚠️ 指數母體更新失敗，沿用舊資料｜{type(exc).__name__}", flush=True)
        return old
    if components:
        local_market_cache.set_state(SHARES_STATE_KEY, {"at": time.time(), "components": components})
        print(
            f"📐 指數母體已更新：上市 {sum(1 for v in components.values() if v.get('market') == 'twse'):,} 檔｜"
            f"上櫃 {sum(1 for v in components.values() if v.get('market') == 'tpex'):,} 檔",
            flush=True,
        )
    return components or old


def share_counts(refresh: bool = False) -> Dict[str, List[Any]]:
    """向後相容：{代號: [市場, 發行股數]}。"""
    return {code: [info.get("market"), info.get("shares")]
            for code, info in component_universe(refresh=refresh).items() if info.get("shares")}

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
    """單一市場的指數貢獻點數。

    收盤後一定掃完整官方母體，不再只看前幾大權值股。
    - TPEx：優先用官方櫃買指數成分股 OpenAPI。
    - TWSE：依 TAIEX 官方編製要點，用上市普通股官方基本資料＋上市時間規則重建；
      官方每日 TAIEX 成分權重檔屬 Data E-Shop 付費資料，因此不把重建值冒充官方公布權重。
    """
    if market not in INDEX_OF:
        raise ValueError(f"unsupported market: {market}")
    index_code, index_name = INDEX_OF[market]
    components = component_universe()
    market_components = {code: info for code, info in components.items() if info.get("market") == market}
    codes = list(market_components)
    if not codes:
        raise tools.ToolDataError("沒有指數成分母體，無法計算指數貢獻")

    # 先取得官方/既有指數日K的昨收與收盤；收盤模式用日期來避免把停牌前的舊漲跌誤算到今天。
    prev_index, now_index, index_date = _index_prev_close(index_code)
    target_date = index_date or str((local_market_cache.stats() or {}).get("last_day") or "")
    changes = local_market_cache.latest_changes(codes)

    rows: List[Dict[str, Any]] = []
    total_prev_value = 0.0
    for code in codes:
        quote = changes.get(code) or {}
        last_close = tools._num(quote.get("close"))
        pct = tools._num(quote.get("change_pct"))
        quote_date = str(quote.get("date") or "")
        if not last_close:
            continue
        comp = market_components[code]
        count = float(comp.get("shares") or 0.0)
        if count <= 0:
            continue

        if live:
            # 盤中本地底庫最後一根就是上一個已收盤交易日。
            prev_close = float(last_close)
            close = float(last_close)
            change_pct = 0.0
        else:
            # 今天沒有收盤資料（停牌/無成交/底庫缺口）時，不把歷史上一個交易日的漲跌硬算成今天。
            if target_date and quote_date and quote_date != target_date:
                prev_close = float(last_close)
                close = float(last_close)
                change_pct = 0.0
            else:
                if pct is None or float(pct) == -100:
                    continue
                prev_close = float(last_close) / (1.0 + float(pct) / 100.0)
                close = float(last_close)
                change_pct = float(pct)
        if prev_close <= 0:
            continue
        prev_value = prev_close * count
        rows.append({
            "stock_code": code,
            "stock_name": str(comp.get("name") or code),
            "prev_close": prev_close,
            "close": close,
            "change_pct": change_pct,
            "shares": count,
            "prev_value": prev_value,
            "quote_date": quote_date,
            "live_quote": False,
        })
        total_prev_value += prev_value

    if not rows or total_prev_value <= 0:
        raise tools.ToolDataError("本地底庫資料不足，無法計算指數貢獻")

    live_used = 0
    top_safe = bottom_safe = not live
    unseen_bound = 0.0
    data_date = target_date

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
                item = quotes.get(row["stock_code"])
                if not item:
                    continue
                current = tools._num(item.get("close"))
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

        # MIS 一檔都沒拿到才退回少量 Fugle；仍受同一個 deadline 控制。
        if live_used == 0 and time.monotonic() < deadline:
            live_used = _fugle_fallback(rows, market, deadline)
        if prev_index is None:
            prev_index, now_index, data_date = _index_prev_close(index_code)

    if not prev_index:
        raise tools.ToolDataError("指數基準資料不足，無法計算指數貢獻")

    # 官方公式：指數 = 總發行市值 / 當日基值 × 100。
    # 收盤後以「當日完整母體的收盤總市值 + 官方收盤指數」反推當日基值，
    # 再用同一基值計算每一檔的點數貢獻；這比沿用昨日總市值更符合基值調整規則。
    current_total_value = sum(float(r["close"]) * float(r["shares"]) for r in rows)
    if not live and now_index and current_total_value > 0:
        factor = float(now_index) / current_total_value
        weight_base = current_total_value
        weight_mode = "close"
    else:
        factor = float(prev_index) / total_prev_value
        weight_base = total_prev_value
        weight_mode = "prev"

    names: Dict[str, str] = {}
    try:
        names = tools.get_stock_name_map() or {}
    except Exception:
        names = {}
    for row in rows:
        row["points"] = round((row["close"] - row["prev_close"]) * row["shares"] * factor, 2)
        if not row.get("stock_name") or row.get("stock_name") == row["stock_code"]:
            row["stock_name"] = names.get(row["stock_code"], row["stock_code"])
        market_value = (row["close"] * row["shares"]) if weight_mode == "close" else row["prev_value"]
        row["weight_pct"] = round(market_value / weight_base * 100.0, 2) if weight_base > 0 else 0.0
        row["change_pct"] = round(float(row["change_pct"]), 2)
        row["close"] = round(float(row["close"]), 2)

    # 收盤後全部成分股都參與排行；盤中只對已取得即時報價者排名，避免未取價股票被當 0% 混入。
    evaluated = [r for r in rows if r.get("live_quote")] if live else list(rows)
    positives = sorted((r for r in evaluated if r["points"] > 0), key=lambda r: -r["points"])
    negatives = sorted((r for r in evaluated if r["points"] < 0), key=lambda r: r["points"])
    top_rows = positives[:top]
    bottom_rows = negatives[:top]
    keep = ("stock_code", "stock_name", "points", "change_pct", "close", "weight_pct")

    positive_total = sum(r["points"] for r in positives)
    negative_total = abs(sum(r["points"] for r in negatives))
    top_lift = sum(r["points"] for r in top_rows)
    top_drag = sum(r["points"] for r in bottom_rows)
    positive_share = (top_lift / positive_total * 100.0) if positive_total > 0 else None
    negative_share = (abs(top_drag) / negative_total * 100.0) if negative_total > 0 else None
    covered_value = sum((r["prev_value"] for r in evaluated))
    market_cap_coverage = covered_value / total_prev_value * 100.0 if total_prev_value > 0 else 0.0
    estimated_points = round(sum(r["points"] for r in evaluated), 2)
    index_points = round((now_index - prev_index), 2) if now_index is not None else None
    residual = round(index_points - estimated_points, 2) if index_points is not None else None

    focus_share = positive_share if (index_points or 0) >= 0 else negative_share
    concentration = _concentration_label(focus_share)
    if live and not (top_safe and bottom_safe):
        concentration += "（盤中估算）"

    exact_membership = all(bool(market_components[c].get("exact_membership")) for c in codes if c in market_components)
    if market == "tpex" and exact_membership:
        universe_source = "TPEx官方櫃買指數成分股"
        basis = "盤中估算" if live else "收盤"
    elif market == "twse":
        universe_source = "TAIEX官方編製規則重建（TWSE免費公開資料）"
        basis = "盤中估算" if live else "收盤"
    else:
        universe_source = "官方公司基本資料備援"
        basis = "盤中估算" if live else "收盤"

    # 收盤時把『完整度』寫進 log，方便和 CMoney 對帳；不把診斷字塞到圖片裡。
    if not live:
        print(
            f"📊 {index_name} 貢獻點數｜母體={len(codes)}｜有價格={len(rows)}｜"
            f"指數={index_points:+.2f}｜個股合計={estimated_points:+.2f}｜殘差={residual:+.2f}｜{universe_source}",
            flush=True,
        )

    return {
        "market": market,
        "index_name": index_name,
        "index_prev_close": round(prev_index, 2),
        "index_now": round(now_index, 2) if now_index is not None else None,
        "index_points": index_points,
        "estimated_points": estimated_points,
        "residual_points": residual,
        "basis": basis,
        "data_date": data_date,
        "components": len(codes),
        "components_evaluated": len(evaluated),
        "component_price_coverage_pct": round(len(rows) / len(codes) * 100.0, 1) if codes else 0.0,
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
        "universe_source": universe_source,
        "exact_official_membership": bool(exact_membership),
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
        "依交易所發行量加權公式計算：收盤後以完整成分母體的總發行市值與官方收盤指數換算每檔貢獻點數；"
        "櫃買優先使用官方公開成分股名冊，TAIEX 依官方編製要點與免費公開資料重建母體。"
        "盤中使用官方 MIS 批次報價並設時間上限，畫面會標示市值涵蓋率與是否仍屬估算。"
    )
    return out
