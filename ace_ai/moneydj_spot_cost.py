"""MoneyDJ 現股分點買賣均價（測試版 10-15）。

資料：MoneyDJ jsondata，與 warrant_backtest_moneydj 同一套網址 hash 與限流規則（endpoint 換成 twStockData）。
  - stock-chip0002-4：最新交易日全部分點（日期, 券商代碼, 名稱, 買股, 賣股, LotSize, 當日總股數）→ 名稱對代碼。
  - stock-chip0002-6：分點對個股明細 by 近日，c＝最近 N 筆（日期, 買股, 賣股, 買金額仟元, 賣金額仟元, 1000）。
限流（照權證回測）：全域 token bucket、單一工作執行緒依序請求；回應不是 JSON（「請稍候」／錯誤頁）或請求失敗
  → 全域降速 ×0.5 並冷卻（60 秒起、每次加倍、上限 10 分鐘），冷卻中完全不送請求；連續成功才緩慢回升 ×1.15。
呼叫端不等待網路：有快取就用；沒有就排背景工作並回傳 Future，Bot 先出圖、補完再整題重算覆蓋原圖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor

HOST = "https://pscnetsecrwd.moneydj.com"
DOMAIN = "/b2brwdCommon/jsondata/"
REVISION = os.getenv("MONEYDJ_URL_REVISION", "2018_07_31_1").strip()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept": "*/*", "Referer": "https://pscnetsecrwd.moneydj.com/"}
ENABLED = os.getenv("TEST_MONEYDJ_SPOT_COST", "1").strip().lower() not in ("0", "false", "no")
HISTORY = max(1, int(os.getenv("TEST_MONEYDJ_SPOT_DAYS", "20")))       # 近 N 筆，算近 20 日買進均價
RATE_START = float(os.getenv("TEST_MONEYDJ_SPOT_RATE", "1.0"))          # 每秒請求數（Bot 比回測保守）
RATE_MIN, RATE_MAX = 0.2, max(1.0, float(os.getenv("TEST_MONEYDJ_SPOT_RATE_MAX", "2.0")))
BACKOFF, RECOVER, RECOVER_AFTER = 0.5, 1.15, 20
COOL_START, COOL_MAX = 60.0, 600.0
TIMEOUT = (5, 10)
CACHE_SECONDS = 8 * 3600

_LOCK = threading.Lock()
_rate = RATE_START
_tokens = 1.0
_updated = time.monotonic()
_cool_until = 0.0
_cool_next = COOL_START
_streak = 0
_CACHE = {}        # (code, date) → {"codes": {name: code}, "branches": {name: data}, "at": time}
_JOBS = {}         # (code, date) → Future
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moneydj-spot")   # 單執行緒：絕不並發打 MoneyDJ


class Throttled(RuntimeError):
    pass


def build_url(endpoint, params):
    """同 warrant_backtest_moneydj.moneydj_build_url：md5 前 6 碼當三層路徑；params 順序影響 hash。"""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    digest = hashlib.md5(f"{DOMAIN}{params.get('x', '')}/{endpoint}.xdjjson?{query}".encode("utf-8")).hexdigest()
    return f"{HOST}{DOMAIN}{digest[0:2]}/{digest[2:4]}/{digest[4:6]}/{endpoint}.xdjjson?{query}&revision={REVISION}"


def cooling():
    return time.monotonic() < _cool_until


def _wait_token():
    global _tokens, _updated
    while True:
        with _LOCK:
            if time.monotonic() < _cool_until:
                raise Throttled("MoneyDJ 冷卻中")
            now = time.monotonic()
            _tokens = min(1.0, _tokens + (now - _updated) * _rate)    # 桶容量 1：不累積、不爆衝
            _updated = now
            if _tokens >= 1.0:
                _tokens -= 1.0
                return
            need = (1.0 - _tokens) / _rate
        time.sleep(min(max(need, 0.01), 2.0))


def _backoff(reason):
    global _rate, _cool_until, _cool_next, _streak
    with _LOCK:
        _rate = max(RATE_MIN, _rate * BACKOFF)
        _cool_until = time.monotonic() + _cool_next
        print(f"🐌 MoneyDJ 現股成本降速冷卻｜{reason}｜{_rate:.2f} req/s｜冷卻 {_cool_next:.0f}s", flush=True)
        _cool_next = min(COOL_MAX, _cool_next * 2)
        _streak = 0


def _success():
    global _rate, _cool_next, _streak
    with _LOCK:
        _streak += 1
        if _streak >= RECOVER_AFTER:
            _rate, _streak = min(RATE_MAX, _rate * RECOVER), 0
            _cool_next = COOL_START


def _get(params):
    _wait_token()
    try:
        req = urllib.request.Request(build_url("twStockData", params), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT[1]) as resp:
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            body = resp.read()
        if "json" not in ctype and body[:1] not in (b"{", b"["):
            raise Throttled(f"非 JSON 回應 {ctype or '-'} {len(body)} bytes")
        data = json.loads(body.decode("utf-8", "replace"))
    except Throttled as exc:
        _backoff(str(exc))
        raise
    except Exception as exc:
        _backoff(f"{type(exc).__name__}")
        raise Throttled(str(exc)) from exc
    _success()
    sets = data if isinstance(data, list) else [data]
    return [row for s in sets for row in ((s or {}).get("ResultSet") or {}).get("Result") or []]


def _norm(name):
    return re.sub(r"證券|[-－\s　]", "", str(name or ""))


def _num(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _fetch(code, date, names):
    """背景：先拿名稱→代碼（1 請求），再逐家抓近 N 筆明細（每家 1 請求）。中途被擋就保留已拿到的部分。"""
    key = (code, date)
    entry = _CACHE.setdefault(key, {"codes": {}, "branches": {}, "at": time.time()})
    try:
        if not entry["codes"]:
            for r in _get({"a": code, "x": "stock-chip0002-4", "c": "1", "e": "200"}):   # 只回最新交易日（買、賣各一組）
                entry["codes"].setdefault(_norm(r.get("V3")), str(r.get("V2", "")).strip())
        for name in names:
            if name in entry["branches"]:
                continue
            bcode = entry["codes"].get(_norm(name))
            if not bcode:
                entry["branches"][name] = None             # 最新一天沒有交易紀錄：查不到代碼，不顯示價格
                continue
            rows = _get({"x": "stock-chip0002-6", "c": HISTORY, "a": code, "b": bcode})
            entry["branches"][name] = summarize(rows, date)
    finally:
        entry["at"] = time.time()
    return entry


def summarize(rows, date):
    """date 當天的買／賣均價，以及截至 date 近 HISTORY 筆的買進均價（金額仟元÷股數）。"""
    days = []
    for r in rows:
        d = str(r.get("V1", "")).replace("/", "-")
        if d and d <= date:
            days.append((d, _num(r.get("V2")), _num(r.get("V3")), _num(r.get("V4")) * 1000, _num(r.get("V5")) * 1000))
    days.sort()
    today = next((x for x in days if x[0] == date), None)
    buy_sh, buy_amt = sum(x[1] for x in days), sum(x[3] for x in days)
    avg = lambda amt, sh: round(amt / sh, 2) if sh > 0 else None
    return {"date": date, "days": len(days),
            "buy_avg_day": avg(today[3], today[1]) if today else None,
            "sell_avg_day": avg(today[4], today[2]) if today else None,
            "buy_avg_period": avg(buy_amt, buy_sh), "period_buy_lots": round(buy_sh / 1000)}


def costs(code, date, names):
    """回傳 ({分點: 均價資料或 None}, 背景 Future 或 None)。完全不等網路；冷卻中或停用時不排工作。"""
    key = (str(code), str(date))
    entry = _CACHE.get(key)
    if entry and time.time() - entry["at"] > CACHE_SECONDS:
        _CACHE.pop(key, None)
        entry = None
    have = {n: entry["branches"].get(n) for n in names if entry and n in entry["branches"]}
    missing = [n for n in names if n not in have]
    if not missing or not ENABLED or cooling():
        return have, None
    with _LOCK:
        job = _JOBS.get(key)
        if job is None or job.done():
            job = _POOL.submit(_fetch, key[0], key[1], list(missing))
            _JOBS[key] = job
    return have, job
