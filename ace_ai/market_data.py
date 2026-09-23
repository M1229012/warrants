"""全市場日 K 底庫：證交所／櫃買「依日期查全市場」官方 OpenAPI。

一個交易日只要 2 個請求（上市＋上櫃）就能拿到全市場收盤，因此：
- 回補 70 個交易日＝約 140 個請求，完全不用 FinMind 額度、不需要任何金鑰。
- 每天收盤後只要 2 個請求就能把當天補上。

資料寫進 local_market_cache（SQLite），族群排行、全市場排行與個股查詢都直接讀本地，
不再為了單一問題逐檔打行情 API。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date as _date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import warrant_ai_tools as tools
import local_market_cache

TWSE_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=ALLBUT0999&response=json"
TPEX_DAY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date}&type=EW&id=&response=json"
HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "application/json,text/plain,*/*"}
TIMEOUT = max(4.0, tools._env_float("DISCORD_AI_MARKET_HTTP_TIMEOUT", 20.0))
REQUEST_GAP = max(0.3, tools._env_float("DISCORD_AI_MARKET_REQUEST_GAP", 1.2))
HISTORY_DAYS = max(70, tools._env_int("DISCORD_AI_MARKET_HISTORY_DAYS", 140))
_COMMON_STOCK = re.compile(r"^[1-9]\d{3}$")
_LAST_REQUEST = [0.0]


def _sleep_between_requests() -> None:
    wait = REQUEST_GAP - (time.monotonic() - _LAST_REQUEST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST[0] = time.monotonic()


def _get_json(url: str, provider: str) -> Dict[str, Any]:
    _sleep_between_requests()
    started = time.perf_counter()
    status = 0
    try:
        session = tools.core().get_thread_session()
        response = session.get(url, headers=HEADERS, timeout=(4, TIMEOUT))
        status = int(response.status_code)
        response.raise_for_status()
        return json.loads(response.text)
    finally:
        try:
            tools.record_api_event(provider, status=status, latency=time.perf_counter() - started)
        except Exception:
            pass


def _number(value: Any) -> Optional[float]:
    text = str(value or "").replace(",", "").replace("+", "").strip()
    if not text or text in ("--", "---", "N/A", "null"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number


def _rows_from_twse(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if str(payload.get("stat") or "").upper() != "OK":
        return []
    rows: List[Dict[str, Any]] = []
    for table in payload.get("tables") or []:
        fields = list(table.get("fields") or [])
        if "證券代號" not in fields:
            continue
        index = {name: i for i, name in enumerate(fields)}
        for raw in table.get("data") or []:
            try:
                code = str(raw[index["證券代號"]]).strip()
            except (IndexError, KeyError, TypeError):
                continue
            if not _COMMON_STOCK.match(code):
                continue   # ETF／權證／特別股不進底庫
            values = {key: _number(raw[index[column]]) for key, column in
                      (("open", "開盤價"), ("high", "最高價"), ("low", "最低價"),
                       ("close", "收盤價"), ("volume", "成交股數")) if column in index}
            if not all(values.get(k) for k in ("open", "high", "low", "close")):
                continue
            rows.append({"stock_code": code, "market": "twse", "volume": values.get("volume") or 0.0,
                         **{k: values[k] for k in ("open", "high", "low", "close")}})
        break
    return rows


def _rows_from_tpex(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for table in payload.get("tables") or []:
        fields = [str(f).strip() for f in (table.get("fields") or [])]
        if not fields or "代號" not in fields:
            continue
        index = {name: i for i, name in enumerate(fields)}

        def pick(raw: List[Any], *names: str) -> Optional[float]:
            for name in names:
                if name in index:
                    value = _number(raw[index[name]])
                    if value is not None:
                        return value
            return None

        for raw in table.get("data") or []:
            try:
                code = str(raw[index["代號"]]).strip()
            except (IndexError, KeyError, TypeError):
                continue
            if not _COMMON_STOCK.match(code):
                continue
            close = pick(raw, "收盤", "收盤價")
            open_ = pick(raw, "開盤", "開盤價")
            high = pick(raw, "最高", "最高價")
            low = pick(raw, "最低", "最低價")
            volume = pick(raw, "成交股數", "成交量") or 0.0
            if not all(v for v in (close, open_, high, low)):
                continue
            rows.append({"stock_code": code, "market": "tpex", "open": open_, "high": high,
                         "low": low, "close": close, "volume": volume})
        break
    return rows


# 一個市場當天普通股少於這個數量，視為回應不完整（正常上市約 1,000 檔、上櫃約 800 檔）。
MIN_ROWS = {"twse": max(1, tools._env_int("DISCORD_AI_MARKET_MIN_ROWS_TWSE", 500)),
            "tpex": max(1, tools._env_int("DISCORD_AI_MARKET_MIN_ROWS_TPEX", 300))}


def _twse_empty(payload: Dict[str, Any]) -> bool:
    """證交所明確回覆「沒有符合條件的資料」（休市日）；其他非 OK 狀態不算。"""
    return "沒有符合條件" in str(payload.get("stat") or "")


def _tpex_empty(payload: Dict[str, Any]) -> bool:
    tables = payload.get("tables")
    if str(payload.get("stat") or "ok").lower() not in ("ok", ""):
        return False
    return isinstance(tables, list) and all(not (t.get("data") or []) for t in tables)


def fetch_market(day: _date, market: str) -> Tuple[str, List[Dict[str, Any]], str]:
    """單一市場單日收盤 → (status, rows, detail)。
    complete＝列數達門檻；empty＝交易所明確回覆當天沒有資料；source_error＝連線失敗、格式不符或列數不足。"""
    try:
        if market == "twse":
            payload = _get_json(TWSE_DAY_URL.format(date=day.strftime("%Y%m%d")), "TWSE")
            rows, empty = _rows_from_twse(payload), _twse_empty(payload)
        else:
            payload = _get_json(TPEX_DAY_URL.format(date=day.strftime("%Y/%m/%d")), "TPEx")
            rows, empty = _rows_from_tpex(payload), _tpex_empty(payload)
    except Exception as exc:
        print(f"⚠️ 市場底庫：{'上市' if market == 'twse' else '上櫃'} {day} 取得失敗｜{type(exc).__name__}", flush=True)
        return "source_error", [], f"{type(exc).__name__}: {exc}"
    if len(rows) >= MIN_ROWS[market]:
        return "complete", rows, ""
    if not rows and empty:
        return "empty", [], "交易所回覆當天無資料"
    return "source_error", rows, f"只有 {len(rows)} 檔（門檻 {MIN_ROWS[market]}），視為回應不完整"


def fetch_day(day: _date) -> List[Dict[str, Any]]:
    """單一交易日的全市場收盤（上市＋上櫃普通股，只含 complete 的市場）；非交易日回傳空清單。"""
    rows: List[Dict[str, Any]] = []
    for market in local_market_cache.MARKETS:
        status, got, _ = fetch_market(day, market)
        if status == "complete":
            rows += got
    return rows


def _now_date() -> _date:
    return tools.taipei_now().date()


def sync_day(day: _date, markets: Tuple[str, ...] = ("twse", "tpex")) -> int:
    """抓指定市場並記錄各自的完整性。兩邊都明確回覆無資料、且日期已過＝closed（颱風假等全市場休市）；
    只有一邊無資料＝那一邊 source_error（不能拿一邊的成功代表全市場）；今天還沒資料＝pending。"""
    key = day.strftime("%Y-%m-%d")
    results = {m: fetch_market(day, m) for m in markets}
    past = day < _now_date()
    known = local_market_cache.market_status([key])[key]
    saved = 0
    for market, (status, rows, detail) in results.items():
        if status == "complete":
            saved += local_market_cache.save_market_day(rows, key, source="TWSE" if market == "twse" else "TPEx")
            local_market_cache.save_market_status(key, market, "complete", len(rows), "TWSE/TPEx")
            continue
        if status == "empty":
            others = [results[m][0] if m in results else ("empty" if known[m] == "closed" else known[m])
                      for m in local_market_cache.MARKETS if m != market]
            if not past:
                final = "pending"
            elif all(o == "empty" for o in others):
                final = "closed"
            else:
                final = "source_error"
                detail = "另一個市場當天有資料，這一邊卻回覆無資料"
        else:
            final = "source_error"
        local_market_cache.save_market_status(key, market, final, len(rows), "TWSE/TPEx", detail)
    return saved


def _candidate_days(count: int, end: Optional[_date] = None) -> List[_date]:
    """往回列出可能的交易日（只跳過週末；遇到假日 API 會回空，由呼叫端處理）。"""
    day = end or tools.taipei_now().date()
    days: List[_date] = []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def sync(target_days: int = HISTORY_DAYS, budget_seconds: float = 600.0,
         log: Callable[[str], None] = print, force: bool = False) -> Dict[str, Any]:
    """把底庫補到 target_days 個交易日。上市、上櫃分開判斷：兩邊都 complete（或都確認休市）才跳過，
    只有一邊成功的日子下一輪只重抓失敗的那一邊。"""
    started = time.monotonic()
    candidates = _candidate_days(int(target_days * 1.5))
    keys = [d.strftime("%Y-%m-%d") for d in candidates]
    status = local_market_cache.market_status(keys)
    done ={k for k in keys if all(status[k][m] == "complete" for m in local_market_cache.MARKETS)}
    fetched = saved = holidays = 0
    for day, key in zip(candidates, keys):
        if time.monotonic() - started > budget_seconds:
            log(f"市場底庫：達到本輪時間上限（{budget_seconds:.0f} 秒），下一輪續補")
            break
        if len(done) >= target_days:
            break
        if not force and (key in done or all(status[key][m] == "closed" for m in local_market_cache.MARKETS)):
            continue
        # 只重抓還沒 complete／closed 的那一邊（舊版底庫沒有完整性紀錄的日子會兩邊各重抓一次）
        need = tuple(m for m in local_market_cache.MARKETS if force or status[key][m] not in ("complete", "closed"))
        count = sync_day(day, need)
        fetched += len(need)
        status[key] = local_market_cache.market_status([key])[key]
        if all(status[key][m] == "complete" for m in local_market_cache.MARKETS):
            done.add(key)
        if count:
            saved += count
            log(f"市場底庫：{key} 收盤 {count:,} 檔（上市 {status[key]['twse']}／上櫃 {status[key]['tpex']}）")
        elif all(status[key][m] == "closed" for m in local_market_cache.MARKETS):
            holidays += 1
    local_market_cache.trim_history(local_market_cache.KEEP_DAYS)
    stats = local_market_cache.stats()
    local_market_cache.set_state("market_sync", {
        "at": tools.taipei_now().strftime("%Y-%m-%d %H:%M"), "days": stats.get("days", 0),
        "stocks": stats.get("stocks", 0), "last_day": stats.get("last_day", ""),
    })
    return {"requests": fetched, "days_saved": len(done), "rows_saved": saved,
            "non_trading_days": holidays, "days": stats.get("days", 0), "stocks": stats.get("stocks", 0),
            "last_day": stats.get("last_day", ""), "elapsed": time.monotonic() - started}


def coverage() -> Dict[str, Any]:
    stats = local_market_cache.stats()
    state = local_market_cache.get_state("market_sync", {}) or {}
    return {"days": stats.get("days", 0), "stocks": stats.get("stocks", 0),
            "scored_stocks": stats.get("scores", 0), "last_day": stats.get("last_day", ""),
            "synced_at": state.get("at", ""), "persistent": stats.get("persistent", False),
            "ready": int(stats.get("days", 0)) >= 60 and int(stats.get("stocks", 0)) >= 800}


def main(argv: Optional[List[str]] = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="建立／更新全市場日K底庫（證交所＋櫃買官方資料）")
    parser.add_argument("--days", type=int, default=HISTORY_DAYS, help="要補到幾個交易日（預設 80）")
    parser.add_argument("--budget", type=float, default=1800.0, help="本次最長秒數")
    parser.add_argument("--force", action="store_true", help="已有的日期也重抓")
    args = parser.parse_args(argv)
    result = sync(target_days=args.days, budget_seconds=args.budget, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
