"""今日族群排行盤中報價：奇摩股市批次報價（10-30，測試版）。

為什麼不用 MIS 當主來源：10-30 Log，證交所 MIS 盤中 96% 的股票 z（成交價）是「-」，
750 檔只拿到 23 檔成交價（加委託盤估值也只有 257 檔），165 個族群只排得出 8 個。
奇摩股市批次介面（tw.stock.yahoo.com）10-30 實測：
- 即時，不是延遲：regularMarketTime 跟抓取時間只差約 2 秒，回應 exchangeDataDelayedBy＝0
  （延遲 20 分鐘的是國際版 Yahoo Finance，不同服務）。
- 一次最多 50 檔（100 檔回 400）；750 檔分 15 次、約 12 秒抓完，720 檔有今天的成交價。
- price＝最新成交價，regularMarketTime＝這檔最後一筆成交時間（冷門股可能是幾分鐘前，那就是它的最新成交價）。

使用風險由使用者自行評估（10-30）：Yahoo 服務條款限制自動化存取與商業使用；非公開介面，可能改版或被擋。
所以：
- 只有族群排行快照用，請求間隔 0.3 秒、單執行緒；被擋（403／429）或連續失敗就冷卻 5 分鐘，期間全部改走 MIS。
- 抓不到的股票由 MIS 補（member_sector_tape._fetch）。
- 環境變數 DISCORD_AI_MEMBER_QUOTE_SOURCE=mis 可以整個關掉，不用改程式。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

import local_market_cache
import warrant_ai_tools as tools

SOURCE = os.getenv("DISCORD_AI_MEMBER_QUOTE_SOURCE", "yahoo").strip().lower()
URL = "https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.stockList;fields=avgPrice;symbols="
BATCH = 50                       # 10-30 實測：50 檔可以、100 檔回 400
GAP_SECONDS = 0.3                # 每批之間的間隔
TIMEOUT_SECONDS = 10.0
DEFAULT_DEADLINE = 20.0          # 查詢當下補拍的整次時限（和 MIS 的查詢補拍相同）
COOLDOWN_SECONDS = 300.0         # 被擋或連續失敗：5 分鐘內不再打，全部改走 MIS
MAX_ERRORS = 3
OPEN_MINUTE = 9 * 60             # 09:00 前的價格是試撮，不是成交
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_COOL_UNTIL = [0.0]
_LOCK = threading.Lock()


def enabled() -> bool:
    return SOURCE == "yahoo" and time.monotonic() >= _COOL_UNTIL[0]


def _cool_down(reason: str) -> None:
    _COOL_UNTIL[0] = time.monotonic() + COOLDOWN_SECONDS
    print(f"⚠️ Yahoo 成員報價暫停 {COOLDOWN_SECONDS / 60:.0f} 分鐘，改用 MIS｜{reason}", flush=True)


def _raw(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse(item: Dict[str, Any], today: str) -> Optional[Dict[str, Any]]:
    """一筆奇摩報價 → 和 sector_radar MIS 報價相同的欄位。不是今天的成交、09:00 前的試撮價、缺昨收都不收。"""
    price, prev = _raw(item.get("price")), _raw(item.get("regularMarketPreviousClose"))
    stamp = str(item.get("regularMarketTime") or "")
    if price <= 0 or prev <= 0 or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tools.TAIPEI_TZ)
    except ValueError:
        return None
    if moment.strftime("%Y-%m-%d") != today or moment.hour * 60 + moment.minute < OPEN_MINUTE:
        return None
    lots = _raw(item.get("volume")) / 1000           # volume 是股數
    turnover = _raw(item.get("turnoverM")) * 1e6     # turnoverM 單位百萬元（10-30 台積電 28965.12＝289.7 億）
    return {"name": str(item.get("symbolName") or item.get("symbol") or ""), "price": price, "prev": prev,
            "change_pct": round((price / prev - 1) * 100, 2),
            "value": turnover if turnover > 0 else lots * 1000 * price,
            "high": _raw(item.get("regularMarketDayHigh")), "low": _raw(item.get("regularMarketDayLow")),
            "open": _raw(item.get("regularMarketOpen")),
            "limit_up": _raw(item.get("limitUpPrice")), "limit_down": _raw(item.get("limitDownPrice")),
            "traded": True, "lots": lots,
            "trade_price": price,                        # 奇摩的 price 是最新成交價（不是五檔中價）
            "pz_price": 0.0, "bid": _raw(item.get("bid")), "ask": _raw(item.get("ask")),
            "price_source": "yahoo", "trade_time": moment.strftime("%H:%M:%S"),
            "exchange_date": moment.strftime("%Y%m%d")}


def fetch(codes: List[str], deadline_seconds: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """批次抓奇摩報價；回 {代號: 報價}。冷卻中、市場別不明、超過時限的股票不回傳（呼叫端用 MIS 補）。"""
    if not codes or not enabled() or tools.closed_quotes_only():
        return {}
    try:
        markets = local_market_cache.stock_markets(codes)
    except Exception:
        markets = {}
    symbols = {f"{c}.{'TW' if markets.get(c) == 'twse' else 'TWO'}": c
               for c in codes if markets.get(c) in ("twse", "tpex")}
    today = tools.taipei_now().strftime("%Y-%m-%d")
    names = list(symbols)
    out: Dict[str, Dict[str, Any]] = {}
    errors = calls = 0
    began = time.perf_counter()
    deadline = began + (deadline_seconds or DEFAULT_DEADLINE)
    with _LOCK:                                      # 單執行緒，不並行打
        for start in range(0, len(names), BATCH):
            if time.perf_counter() > deadline or not enabled():
                break
            batch = names[start:start + BATCH]
            calls += 1
            try:
                resp = requests.get(URL + urllib.parse.quote(",".join(batch)), headers=HEADERS,
                                    timeout=TIMEOUT_SECONDS)
                if resp.status_code in (403, 429):
                    _cool_down(f"HTTP {resp.status_code}")
                    break
                resp.raise_for_status()
                for item in resp.json() or []:
                    code = symbols.get(str(item.get("symbol") or ""))
                    quote = parse(item, today) if code else None
                    if quote:
                        out[code] = quote
            except Exception as exc:
                errors += 1
                print(f"⚠️ Yahoo 成員報價批次失敗｜{len(batch)} 檔｜{tools.err_text(exc)}", flush=True)
                if errors >= MAX_ERRORS:
                    _cool_down(f"連續 {errors} 批失敗")
                    break
            if start + BATCH < len(names):
                time.sleep(GAP_SECONDS)
    print(f"📡 Yahoo 成員報價｜requested={len(codes)}｜returned={len(out)}｜市場別不明={len(codes) - len(symbols)}"
          f"｜http_calls={calls}｜失敗 {errors} 批｜{time.perf_counter() - began:.1f}s", flush=True)
    return out
