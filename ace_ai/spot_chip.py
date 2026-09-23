"""現股券商分點籌碼（SPOT_CHIP）：富邦 eBroker 分點頁 → 本地 SQLite（spot_branch_daily）→ 分析與圖卡資料。

- 資料來源沿用 C_function.py 的富邦 eBroker zco 分點頁（指定股票、指定日期的買超／賣超券商）。
- 每交易日 × 每股票 × 每分點存一列（net = 買進 − 賣出；正＝買超、負＝賣超）；已抓過的日期不重抓。
- 預設用 HTTP 抓頁面；DISCORD_AI_SPOT_FETCHER=selenium 時改用 Selenium（需要 Chromium，同一輪只開一個瀏覽器）。
- 來源每天只列「買超前段」與「賣超前段」分點，不是全部分點；區間統計是每日前段加總，屬近似值。
- 券商分類（例如外資券商）只是券商類別，不是官方外資法人買賣超。
Router 只呼叫 build_report / branch_report，不知道抓取細節。
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

import local_market_cache
import warrant_ai_tools as tools

SOURCE_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco.djhtm"
SOURCE_NAME = "fubon_zco"
REQUESTED_DAYS = 70
PERIODS = (3, 5, 10, 20, 70)
FETCHER = os.getenv("DISCORD_AI_SPOT_FETCHER", "http").strip().lower() or "http"
MAX_CONCURRENCY = max(1, int(os.getenv("DISCORD_AI_SPOT_CONCURRENCY", "1") or 1))
# 每頁間隔：原本 0.6 秒 × 71 頁光等待就 43 秒（實際抓取只要約 17 秒），改成 0.2 秒。
REQUEST_GAP = float(os.getenv("DISCORD_AI_SPOT_REQUEST_GAP", "0.2") or 0.2)
# 會員這一題最多等幾秒補歷史；沒補完的日期在背景繼續補，下一題就是完整資料。
BACKFILL_BUDGET = float(os.getenv("DISCORD_AI_SPOT_BACKFILL_BUDGET", "25") or 25)
BACKGROUND_BUDGET = float(os.getenv("DISCORD_AI_SPOT_BACKGROUND_BUDGET", "600") or 600)
TODAY_READY = os.getenv("DISCORD_AI_SPOT_TODAY_READY", "15:30").strip() or "15:30"
# 狀態：complete（有可信分點）／pending_update（最新交易日富邦尚未更新）／market_closed（全市場休市）
# ／stock_no_trade（市場有開、個股沒成交）／source_error（抓取失敗）／retry（歷史日有成交但來源仍無資料）。
CONFIRMED_STATUSES = frozenset({"complete", "market_closed", "stock_no_trade"})   # 已確認、不再重抓
RETRY_AFTER_SECONDS = {"pending_update": 15 * 60, "retry": 60 * 60, "source_error": 60 * 60}
SOURCE_LIMITATION = "來源每日只列買超／賣超前段分點，區間統計為每日前段加總（近似值）"
BROKER_TAG_NOTE = "券商分類只代表券商類別；外資券商分點合計不是官方外資法人買賣超"

# 沿用 C_function.py 的券商分類（只做標籤顯示）。
BROKER_TAGS = {
    "美商高盛": "外資", "台灣摩根士丹利": "外資", "美林": "外資", "新加坡商瑞銀": "外資",
    "摩根大通": "外資", "野村": "外資", "麥格理": "外資", "香港上海匯豐": "外資", "花旗環球": "外資",
    "法興": "外資", "大和國泰": "外資",
    "合庫": "官股", "華南永昌": "官股", "第一金": "官股", "臺銀": "官股", "兆豐": "官股",
    "土銀": "官股", "台企銀": "官股", "彰銀": "官股",
}

_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENCY)
_BACKGROUND: set = set()
_BACKGROUND_GUARD = threading.Lock()
_STOCK_LOCKS: Dict[str, threading.Lock] = {}
_STOCK_LOCKS_GUARD = threading.Lock()


def _iso(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def broker_tag(name: str) -> str:
    for key, tag in BROKER_TAGS.items():
        if key in str(name or ""):
            return tag
    return ""


# ============================================================
# 解析：zco 分點頁（不依賴 lxml／bs4，用標準庫 HTMLParser）
# ============================================================

class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _number(text: str) -> Optional[float]:
    value = str(text or "").replace(",", "").replace("+", "").strip()
    try:
        return float(value)
    except ValueError:
        return None


def parse_zco_html(html: str) -> Tuple[str, List[Dict[str, Any]]]:
    """回傳 (status, rows)。status：complete（有分點）、no_data（頁面正常但當日沒有資料）、source_error（找不到表格）。
    買超側與賣超側都完整保留（不做 Top50 截斷）；net 一律用 買進−賣出 重算，不沿用來源的正負號或絕對值。"""
    parser = _TableRows()
    parser.feed(str(html or ""))
    header_index = buy_col = sell_col = -1
    for index, row in enumerate(parser.rows):
        if any("買超券商" in c for c in row) and any("賣超券商" in c for c in row):
            header_index = index
            buy_col = next(i for i, c in enumerate(row) if "買超券商" in c)
            sell_col = next(i for i, c in enumerate(row) if "賣超券商" in c)
            break
    if header_index < 0:
        return ("no_data" if "查無" in str(html or "") or "無資料" in str(html or "") else "source_error"), []
    merged: Dict[str, Dict[str, Any]] = {}
    for row in parser.rows[header_index + 1:]:
        for start in (buy_col, sell_col):
            if len(row) < start + 4:
                continue
            name = row[start].strip()
            if not name or any(word in name for word in ("合計", "平均", "買超券商", "賣超券商")):
                continue
            buy, sell = _number(row[start + 1]), _number(row[start + 2])
            if buy is None or sell is None:
                continue
            merged[name] = {"branch_name": name, "buy": buy, "sell": sell, "net": buy - sell}
    return ("complete" if merged else "no_data"), list(merged.values())


# ============================================================
# 抓取：同一輪 backfill 共用一個 session／瀏覽器
# ============================================================

def _source_url(stock_code: str, date: str) -> str:
    return f"{SOURCE_URL}?a={stock_code}&e={date}&f={date}"


@contextmanager
def open_source() -> Iterator[Callable[[str, str], str]]:
    """yield fetch(stock_code, date) -> html。HTTP 預設；selenium 模式整輪只開一個 Chrome。"""
    if FETCHER == "selenium":
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        options = Options()
        for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                     "--blink-settings=imagesEnabled=false", "--window-size=960,720"):
            options.add_argument(flag)
        if os.getenv("CHROME_BIN", "").strip():
            options.binary_location = os.getenv("CHROME_BIN", "").strip()
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(int(os.getenv("PAGE_LOAD_TIMEOUT", "25")))

        def fetch(stock_code: str, date: str) -> str:
            driver.get(_source_url(stock_code, date))
            WebDriverWait(driver, int(os.getenv("WAIT_TABLE_SEC", "10"))).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table.t01")))
            return driver.page_source
        try:
            yield fetch
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return
    import requests
    session = requests.Session()
    session.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/122 Safari/537.36")

    def fetch(stock_code: str, date: str) -> str:
        response = session.get(_source_url(stock_code, date), timeout=(5, 20))
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "big5"
        return response.text
    try:
        yield fetch
    finally:
        session.close()


def _stock_lock(stock_code: str) -> threading.Lock:
    with _STOCK_LOCKS_GUARD:
        return _STOCK_LOCKS.setdefault(str(stock_code), threading.Lock())


# ============================================================
# 交易日與要抓的日期
# ============================================================

def market_closed_dates(official: Sequence[str]) -> List[str]:
    """行事曆上是交易日、但全市場日 K 底庫在它的涵蓋範圍內沒有任何行情的日子（颱風假等臨時休市）。
    底庫涵蓋不完整時（例如剛部署、還在補歷史）不判定，避免把還沒同步的日子誤當休市。"""
    base = set(local_market_cache.known_dates(400))
    if not base:
        return []
    lo, hi = min(base), max(base)
    inside = [d for d in official if lo <= d <= hi]
    if not inside or len([d for d in inside if d in base]) / len(inside) < 0.9:
        return []
    return [d for d in inside if d not in base]


def trading_dates(end: datetime, count: int) -> List[str]:
    """實際開市日（不含週末、國定假日、臨時休市）；官方行事曆失敗時改用本地日 K 底庫已有的交易日。"""
    start = end - timedelta(days=int(count * 1.7) + 20)
    try:
        days = [_iso(d) for d in tools.core()._get_official_trading_dates(start, end)]
    except Exception:
        days = []
    if not days:
        days = [d for d in sorted(local_market_cache.known_dates(400)) if d <= end.strftime("%Y-%m-%d")]
    days = sorted({_iso(d) for d in days})
    closed = set(market_closed_dates(days))
    return [d for d in days if d not in closed][-count:]


def candidate_dates(now: Optional[datetime] = None) -> Tuple[List[str], str]:
    """最近的實際交易日（舊→新，最多 71 天：今天若還沒更新，70 日窗口仍可以昨天為終點）＋今天的狀態：
    today_ready（時間到了可以「嘗試」抓今天，不代表一定完成）／intraday（盤中或未到時間，不抓今天）／closed_day。"""
    now = now or tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    days = trading_dates(now, REQUESTED_DAYS + 1)
    if today not in days:
        return days[-REQUESTED_DAYS:], "closed_day"
    if now.strftime("%H:%M") < TODAY_READY:
        return [d for d in days if d != today][-REQUESTED_DAYS:], "intraday"
    return days[-(REQUESTED_DAYS + 1):], "today_ready"


def _needs_fetch(info: Optional[Dict[str, Any]], date: str, today: str, now: datetime) -> bool:
    if not info:
        return True
    status = info.get("status")
    if status in CONFIRMED_STATUSES:
        return False   # 已完成、全市場休市、個股沒成交：不再重抓
    # pending_update／retry／source_error：過了間隔才再問來源，避免每個會員查詢都打富邦
    wait = RETRY_AFTER_SECONDS.get(status, RETRY_AFTER_SECONDS["retry"])
    try:
        checked = datetime.fromisoformat(str(info.get("checked_at")))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    # 重試間隔用實際經過的時間（checked_at 是寫入當下的 UTC 時間）
    return (datetime.now(timezone.utc) - checked).total_seconds() >= wait


def _no_data_status(date: str, latest_date: str, bar_dates: Sequence[str]) -> str:
    """來源查不到分點時：最新交易日＝富邦可能還沒更新（pending_update）；
    歷史日且日 K 底庫確認個股當天沒成交＝stock_no_trade；歷史日但個股有成交（或無法確認）＝retry，之後再試。"""
    if date >= latest_date:
        return "pending_update"
    bars = set(bar_dates or ())
    if bars and min(bars) <= date <= max(bars) and date not in bars:
        return "stock_no_trade"
    return "retry"


def ensure_days(stock_code: str, dates: Sequence[str], budget: float = BACKFILL_BUDGET,
                now: Optional[datetime] = None, fetch_source=open_source, latest_date: str = "",
                bar_dates: Optional[Sequence[str]] = None, lock_wait: Optional[float] = None) -> Dict[str, int]:
    """只補缺少的日期（新→舊）；每抓完一天立刻寫 SQLite；同股票同時只有一個執行緒在補，全域同時最多 MAX_CONCURRENCY。
    latest_date＝目前最新交易日（查不到資料時判 pending_update）；bar_dates＝個股日 K 日期（判斷停牌）。"""
    now = now or tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    latest_date = latest_date or (max(dates) if dates else today)
    bar_dates = list(bar_dates) if bar_dates is not None else list(_bars(stock_code).keys())
    lock = _stock_lock(stock_code)
    # 背景正在補同一檔時，會員這一題不排隊等它：直接用資料庫已有的資料回答。
    if not lock.acquire(timeout=max(0.0, budget if lock_wait is None else lock_wait)):
        return {"fetched": 0, "remaining": len(dates), "busy": 1}
    try:
        status = local_market_cache.spot_day_status(stock_code, dates)
        missing = sorted((d for d in dates if _needs_fetch(status.get(d), d, today, now)), reverse=True)
        bars = set(bar_dates)
        for date in list(missing):
            # 市場有開、個股日 K 確認當天沒成交（停牌）：不必問富邦，直接確認
            if date < latest_date and bars and min(bars) <= date <= max(bars) and date not in bars:
                local_market_cache.save_spot_day(stock_code, date, [], "stock_no_trade", "daily_bars")
                missing.remove(date)
        if not missing:
            return {"fetched": 0, "remaining": 0}
        deadline = time.monotonic() + budget
        fetched = errors = 0
        if not _SEMAPHORE.acquire(timeout=max(1.0, budget)):
            return {"fetched": 0, "remaining": len(missing), "busy": 1}
        try:
            with fetch_source() as fetch:
                for date in missing:
                    if time.monotonic() >= deadline:
                        break
                    started = time.perf_counter()
                    try:
                        state, rows = parse_zco_html(fetch(stock_code, date))
                        if state == "no_data":
                            state = _no_data_status(date, latest_date, bar_dates)
                        local_market_cache.save_spot_day(stock_code, date, rows, state, SOURCE_NAME)
                        tools.record_api_event("SpotBranch", status=200, latency=time.perf_counter() - started)
                        fetched += 1
                    except Exception as exc:   # 失敗不當成 0：記 source_error，已完成的日期保留
                        errors += 1
                        local_market_cache.save_spot_day(stock_code, date, [], "source_error", SOURCE_NAME,
                                                         f"{type(exc).__name__}: {exc}")
                        tools.record_api_event("SpotBranch", status=500, latency=time.perf_counter() - started)
                        if errors >= 3:
                            break   # 來源異常時不要一路打完 70 天
                    time.sleep(REQUEST_GAP)
        finally:
            _SEMAPHORE.release()
        return {"fetched": fetched, "remaining": max(0, len(missing) - fetched), "errors": errors}
    finally:
        lock.release()


def continue_in_background(stock_code: str, dates: Sequence[str], latest_date: str,
                           bar_dates: Sequence[str], fetch_source=open_source) -> bool:
    """這一題的時間用完但還有日期沒補：背景繼續補（同一檔只開一條），不擋住 Discord 回覆。"""
    code = str(stock_code)
    with _BACKGROUND_GUARD:
        if code in _BACKGROUND:
            return False
        _BACKGROUND.add(code)

    def run() -> None:
        try:
            result = ensure_days(code, dates, budget=BACKGROUND_BUDGET, fetch_source=fetch_source,
                                 latest_date=latest_date, bar_dates=bar_dates, lock_wait=BACKGROUND_BUDGET)
            print(f"📚 現股分點背景補資料｜{code}｜新增 {result.get('fetched', 0)} 日｜尚缺 {result.get('remaining', 0)}", flush=True)
        except Exception as exc:
            print(f"⚠️ 現股分點背景補資料失敗｜{code}｜{type(exc).__name__}: {exc}", flush=True)
        finally:
            with _BACKGROUND_GUARD:
                _BACKGROUND.discard(code)

    threading.Thread(target=run, name=f"spot-backfill-{code}", daemon=True).start()
    return True


# ============================================================
# 分析
# ============================================================

def _bars(stock_code: str) -> Dict[str, Tuple[float, float]]:
    """{日期: (收盤, 成交張數)}，來自本地日 K 底庫。"""
    data = local_market_cache.load_bars(stock_code, limit=local_market_cache.KEEP_DAYS) or {}
    frame = data.get("df")
    if frame is None or frame.empty:
        return {}
    out = {}
    for index, row in frame.iterrows():
        close, volume = float(row.get("Close") or 0), float(row.get("Volume") or 0)
        out[_iso(index)] = (close, volume / 1000)
    return out


def _top(net_by_branch: Dict[str, float], positive: bool, n: int) -> List[Tuple[str, float]]:
    items = [(b, v) for b, v in net_by_branch.items() if (v > 0 if positive else v < 0)]
    items.sort(key=lambda kv: (-kv[1], kv[0]) if positive else (kv[1], kv[0]))
    return items[:n]


def _sum_by_branch(rows: Sequence[Dict[str, Any]], dates: Sequence[str]) -> Dict[str, float]:
    wanted, total = set(dates), defaultdict(float)
    for row in rows:
        if row["date"] in wanted:
            total[row["branch_name"]] += row["net"]
    return dict(total)


def scenario(buy_ratio: float, sell_ratio: float, net_conc: float) -> str:
    """沿用 C_function.py 的 Top15 情境門檻。"""
    high = float(os.getenv("TOP15_RATIO_HIGH", "8"))
    low = float(os.getenv("TOP15_RATIO_LOW", "3"))
    if buy_ratio >= high and sell_ratio <= low and net_conc > 0:
        return "偏吸籌"
    if buy_ratio >= high and sell_ratio >= high:
        return "對敲／換手"
    if sell_ratio >= high and net_conc <= 1.0:
        return "賣壓偏高"
    return "中性"


def analyze(stock_code: str, dates: Sequence[str], statuses: Dict[str, Dict[str, Any]],
            rows: Sequence[Dict[str, Any]], bars: Dict[str, Tuple[float, float]]) -> Dict[str, Any]:
    complete = [d for d in dates if (statuses.get(d) or {}).get("status") == "complete"]
    latest = complete[-1] if complete else ""
    # 70 日窗口一律以「最新完整日」為終點：今天還沒更新時不把今天塞進窗口（不會變成 69/70）
    dates = [d for d in dates if not latest or d <= latest][-REQUESTED_DAYS:]
    complete = [d for d in complete if d in dates]
    confirmed = [d for d in dates if (statuses.get(d) or {}).get("status") in ("complete", "stock_no_trade")]
    by_date: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in rows:
        by_date[row["date"]][row["branch_name"]] = row["net"]
    report: Dict[str, Any] = {"requested_days": min(REQUESTED_DAYS, len(dates)) if dates else REQUESTED_DAYS,
                              "available_days": len(confirmed), "latest_complete_date": latest, "periods": [],
                              "source_limitation": SOURCE_LIMITATION}
    if not latest:
        return report
    report["latest_top_buy"] = [{"branch": b, "net": v, "tag": broker_tag(b)} for b, v in _top(by_date[latest], True, 5)]
    report["latest_top_sell"] = [{"branch": b, "net": v, "tag": broker_tag(b)} for b, v in _top(by_date[latest], False, 5)]
    end = dates.index(latest)
    for n in PERIODS:
        window = list(dates[max(0, end - n + 1):end + 1])
        have = [d for d in window if d in confirmed]
        item: Dict[str, Any] = {"days": n, "available": len(have)}
        if len(window) < n or len(have) < n:
            item["insufficient"] = True
            report["periods"].append(item)
            continue
        net = _sum_by_branch(rows, window)
        buy15 = sum(v for _, v in _top(net, True, 15))
        sell15 = -sum(v for _, v in _top(net, False, 15))
        volume = sum((bars.get(d) or (0, 0))[1] for d in window)
        item.update(top15_buy=buy15, top15_sell=sell15)
        if volume > 0:
            buy_ratio, sell_ratio = buy15 / volume * 100, sell15 / volume * 100
            net_conc = (buy15 - sell15) / volume * 100
            item.update(buy_ratio=round(buy_ratio, 2), sell_ratio=round(sell_ratio, 2), net_concentration=round(net_conc, 2),
                        scenario=scenario(buy_ratio, sell_ratio, net_conc))
        report["periods"].append(item)
    span = [d for d in dates[max(0, end - 19):end + 1] if d in complete]
    if span:
        cumulative = _sum_by_branch(rows, span)
        report["cumulative_days"] = len(span)
        report["cumulative_buy"] = [{"branch": b, "net": v, "tag": broker_tag(b)} for b, v in _top(cumulative, True, 5)]
        report["cumulative_sell"] = [{"branch": b, "net": v, "tag": broker_tag(b)} for b, v in _top(cumulative, False, 5)]
        last5 = span[-5:]
        continuity = []
        for item in report["cumulative_buy"]:
            branch = item["branch"]
            daily = [by_date[d].get(branch, 0.0) for d in span]
            recent = [by_date[d].get(branch, 0.0) for d in last5]
            state = ("持續加碼" if sum(1 for v in recent if v > 0) >= 3 and recent[-1] > 0
                     else "近期減碼" if sum(recent[-3:]) < 0 else "")
            buys = [(bars[d][0], by_date[d].get(branch, 0.0)) for d in span if by_date[d].get(branch, 0.0) > 0 and d in bars]
            cost = sum(p * v for p, v in buys) / sum(v for _, v in buys) if buys else None
            continuity.append({"branch": branch, "appear_20": sum(1 for v in daily if v > 0),
                               "appear_5": sum(1 for v in recent if v > 0), "state": state,
                               "est_cost": round(cost, 2) if cost else None})
        report["continuity"] = continuity
    priced = [d for d in span if d in bars and bars[d][1] > 0]
    if priced:
        vwap = sum(bars[d][0] * bars[d][1] for d in priced) / sum(bars[d][1] for d in priced)
        close = bars[priced[-1]][0]
        report["vwap"] = {"days": len(priced), "vwap": round(vwap, 2), "close": close,
                          "gap_pct": round((close / vwap - 1) * 100, 2) if vwap else None}
    report["history"] = branch_history(report.get("cumulative_buy") or [], dates, complete, by_date, bars)
    return report


def branch_history(buyers: Sequence[Dict[str, Any]], dates: Sequence[str], complete: Sequence[str],
                   by_date: Dict[str, Dict[str, float]], bars: Dict[str, Tuple[float, float]], horizon: int = 5) -> List[Dict[str, Any]]:
    """主要買超分點在本檔的歷史表現：分點進入當日買超前 20 名的日子，之後 horizon 個交易日的報酬（本地資料計算，不重爬）。"""
    ordered = [d for d in dates if d in bars]
    out = []
    for item in buyers[:3]:
        branch, signals, wins, total = item["branch"], 0, 0, 0.0
        for d in complete:
            ranked = [b for b, _ in _top(by_date.get(d, {}), True, 20)]
            if branch not in ranked or d not in ordered:
                continue
            i = ordered.index(d)
            if i + horizon >= len(ordered):
                continue
            base, future = bars[ordered[i]][0], bars[ordered[i + horizon]][0]
            if base <= 0:
                continue
            ret = (future / base - 1) * 100
            signals, wins, total = signals + 1, wins + (ret > 0), total + ret
        if signals:
            out.append({"branch": branch, "signals": signals, "win_rate": round(wins / signals * 100, 1),
                        "avg_return": round(total / signals, 2), "horizon": horizon})
    return out


def build_report(stock_code: str, mode: str = "full", now: Optional[datetime] = None,
                 budget: float = BACKFILL_BUDGET, fetch_source=open_source) -> Dict[str, Any]:
    """mode=full：補齊最近 70 個交易日後分析；mode=latest：只確保最近一個完整交易日（不 backfill 70 日）。"""
    now = now or tools.taipei_now()
    dates, today_state = candidate_dates(now)
    if not dates:
        return {"error": "trading_calendar", "requested_days": REQUESTED_DAYS, "available_days": 0}
    bar_dates = list(_bars(stock_code).keys())
    if mode == "latest":
        progress = {"fetched": 0, "remaining": 0}
        for date in reversed(dates[-4:]):   # 最新一天還沒更新（pending_update）才往前找，最多 4 天
            step = ensure_days(stock_code, [date], budget=min(budget, 30), now=now, fetch_source=fetch_source,
                               latest_date=dates[-1], bar_dates=bar_dates)
            progress["fetched"] += step.get("fetched", 0)
            if (local_market_cache.spot_day_status(stock_code, [date]).get(date) or {}).get("status") == "complete":
                break
    else:
        progress = ensure_days(stock_code, dates, budget=budget, now=now, fetch_source=fetch_source,
                               latest_date=dates[-1], bar_dates=bar_dates, lock_wait=2.0)
        if progress.get("remaining") and not progress.get("errors"):
            # 時間用完（或背景正在補）：剩下的日期背景繼續，這一題先用已有的資料回答
            progress["background"] = continue_in_background(stock_code, dates, dates[-1], bar_dates, fetch_source)
    statuses = local_market_cache.spot_day_status(stock_code, dates)
    complete = [d for d in dates if (statuses.get(d) or {}).get("status") == "complete"]
    rows = local_market_cache.load_spot_rows(stock_code, complete)
    report = analyze(stock_code, dates, statuses, rows, _bars(stock_code))
    today = now.strftime("%Y-%m-%d")
    report["pending_update"] = [d for d in dates if (statuses.get(d) or {}).get("status") == "pending_update"]
    report.update(mode=mode, today_state=today_state, progress=progress,
                  source_errors=sum(1 for d in dates if (statuses.get(d) or {}).get("status") == "source_error"))
    latest = report.get("latest_complete_date")
    if today_state == "intraday":
        report["date_note"] = "盤中查詢・顯示最近完整交易日"
    elif today_state == "today_ready" and latest and latest != today:
        report["date_note"] = "今日分點資料尚未更新・目前顯示最近完整交易日"
    return report


_BRANCH_NAMES_CACHE: Dict[str, Any] = {"at": 0.0, "names": []}


def _branch_key(text: str) -> str:
    return re.sub(r"[\s\-－‐—_・·]+", "", str(text or "")).upper()


def known_branch_names() -> List[str]:
    """現股資料庫裡出現過的券商分點名稱（60 秒快取）；和權證分點名單分開。"""
    if time.monotonic() - _BRANCH_NAMES_CACHE["at"] > 60:
        _BRANCH_NAMES_CACHE.update(at=time.monotonic(), names=local_market_cache.spot_branch_names())
    return list(_BRANCH_NAMES_CACHE["names"])


def match_branch(question: str, names: Optional[Sequence[str]] = None) -> str:
    """從問句找現股分點名稱（忽略空白與「-」，取最長的符合）；找不到回空字串。"""
    key = _branch_key(question)
    best = ""
    for name in (names if names is not None else known_branch_names()):
        k = _branch_key(name)
        if len(k) >= 3 and k in key and len(k) > len(_branch_key(best)):
            best = name
    return best


def branch_report(branch_name: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """「某分點現股最近買什麼」：只查本地已建置過的股票（不會去爬全市場）；查不到就是查不到，不改用權證資料。"""
    now = now or tools.taipei_now()
    branch_name = match_branch(branch_name) or branch_name
    dates = trading_dates(now, 20)
    rows = local_market_cache.spot_branch_history(branch_name, dates[0] if dates else "")
    total: Dict[str, float] = defaultdict(float)
    for row in rows:
        total[row["stock_code"]] += row["net"]
    return {"branch": branch_name, "days": len(dates), "dates": [dates[0], dates[-1]] if dates else [],
            "buy": _top(dict(total), True, 8), "sell": _top(dict(total), False, 8), "found": bool(rows)}


# ============================================================
# 圖卡資料（沿用 answer_image.branch_card 的區塊：heading／tiles／lists／table／badge／note）
# ============================================================

def _lots(value: float) -> str:
    return f"{value:+,.0f} 張"


def _slash(date: str) -> str:
    return str(date or "").replace("-", "/")


def _branch_label(item: Dict[str, Any]) -> str:
    return f"{item['branch']}（{item['tag']}券商）" if item.get("tag") else item["branch"]


def history_footer(report: Dict[str, Any]) -> str:
    have, want = int(report.get("available_days") or 0), int(report.get("requested_days") or REQUESTED_DAYS)
    state = f"歷史資料｜{have} / {want} 個交易日" if have >= want else f"歷史資料建置中｜{have} / {want} 個交易日"
    until = report.get("latest_complete_date")
    return f"股市艾斯  /  現股券商分點資料｜{state}" + (f"｜資料截至 {_slash(until)}" if until else "")


def _ratio(period: Dict[str, Any], key: str, signed: bool = False) -> str:
    value = period.get(key)
    if value is None:
        return "-"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def report_card(report: Dict[str, Any], stock_code: str, stock_name: str) -> Dict[str, Any]:
    """現股分點籌碼圖卡（不畫 K 線分點標記）；沒有資料的區塊直接省略。"""
    latest = report.get("latest_complete_date") or ""
    want = int(report.get("requested_days") or REQUESTED_DAYS)
    sections: List[Dict[str, Any]] = [{"type": "badge", "text": f"資料日期｜{_slash(latest)}"}]
    if report.get("date_note"):
        sections.append({"type": "note", "text": report["date_note"]})
    if report.get("mode") != "latest" and int(report.get("available_days") or 0) < want:
        sections.append({"type": "note", "text": f"歷史資料建置中｜{report.get('available_days', 0)} / {want} 個交易日"
                                                 "（下次查詢會從中斷處繼續補齊）"})
    sections.append({"type": "heading", "text": f"最新現股分點動向｜{_slash(latest)}"})
    sections.append({"type": "lists", "items": [
        {"title": "TOP5 買超", "tone": "up", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                     for x in report.get("latest_top_buy") or []]},
        {"title": "TOP5 賣超", "tone": "down", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                       for x in report.get("latest_top_sell") or []]}]})
    if report.get("mode") != "latest":
        periods = report.get("periods") or []
        if any(not p.get("insufficient") for p in periods):
            rows = []
            for p in periods:
                if p.get("insufficient"):
                    rows.append([f"{p['days']}日", f"資料不足 {p['available']}/{p['days']}", "-", "-", "-", "-", "-"])
                    continue
                rows.append([f"{p['days']}日", _lots(p["top15_buy"]), _lots(-p["top15_sell"]), _ratio(p, "buy_ratio"),
                             _ratio(p, "sell_ratio"), _ratio(p, "net_concentration", True), p.get("scenario") or "-"])
            sections.append({"type": "heading", "text": "籌碼集中度（Top15 分點）"})
            sections.append({"type": "table", "columns": ["期間", "Top15買超", "Top15賣超", "買超比", "賣超比", "淨集中度", "判讀"],
                             "rows": rows, "signed": ("Top15買超", "Top15賣超", "淨集中度"), "accent": ()})
        if report.get("cumulative_buy") or report.get("cumulative_sell"):
            n = report.get("cumulative_days", 20)
            sections.append({"type": "heading", "text": f"主要累積分點（近{n}日）"})
            sections.append({"type": "lists", "items": [
                {"title": "累積買超", "tone": "up", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                           for x in report.get("cumulative_buy") or []]},
                {"title": "累積賣超", "tone": "down", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                             for x in report.get("cumulative_sell") or []]}]})
        if report.get("continuity"):
            sections.append({"type": "heading", "text": "分點延續性（主要累積買超）"})
            sections.append({"type": "table", "columns": ["分點", "近20日買超天數", "近5日買超天數", "狀態", "估算成本"],
                             "rows": [[c["branch"], f"{c['appear_20']} 天", f"{c['appear_5']} 天", c["state"] or "-",
                                       f"{c['est_cost']:,.2f}" if c.get("est_cost") else "-"] for c in report["continuity"]],
                             "signed": (), "accent": ("狀態",)})
        vwap = report.get("vwap")
        if vwap and vwap.get("vwap"):
            gap = vwap.get("gap_pct")
            sections.append({"type": "heading", "text": "成本"})
            sections.append({"type": "tiles", "items": [
                {"label": f"近{vwap['days']}日均價（VWAP）", "value": f"{vwap['vwap']:,.2f}", "tone": "ink"},
                {"label": "最新收盤", "value": f"{vwap['close']:,.2f}", "tone": "ink"},
                {"label": "與均價差距", "value": f"{gap:+.2f}%" if gap is not None else "-", "tone": "signed"}]})
        if report.get("history"):
            sections.append({"type": "heading", "text": "主要分點歷史表現（本檔，本地資料）"})
            sections.append({"type": "table", "columns": ["分點", "進買超前20名次數", "5日後上漲比例", "5日平均報酬"],
                             "rows": [[h["branch"], f"{h['signals']} 次", f"{h['win_rate']:.1f}%", f"{h['avg_return']:+.2f}%"]
                                      for h in report["history"]],
                             "signed": ("5日平均報酬",), "accent": ("5日後上漲比例",)})
    sections.append({"type": "note", "text": f"※ {SOURCE_LIMITATION}；{BROKER_TAG_NOTE}。"})
    return {"branch": f"{stock_code} {stock_name}".strip(), "tags": ["現股分點籌碼"], "label": "現股分點籌碼",
            "sections": sections, "footer_text": history_footer(report)}


def progress_card(stock_code: str, stock_name: str, report: Dict[str, Any]) -> Dict[str, Any]:
    have, want = int(report.get("available_days") or 0), int(report.get("requested_days") or REQUESTED_DAYS)
    note = ("資料來源暫時無法連線，已完成的日期都已保存，稍後再問一次會從中斷處繼續。" if report.get("source_errors")
            else "第一次查詢需要逐日建立歷史資料，稍後再問一次會從中斷處繼續，已建好的日期不會重抓。")
    return {"branch": f"{stock_code} {stock_name}".strip(), "tags": ["現股分點籌碼"], "label": "現股分點籌碼",
            "sections": [{"type": "heading", "text": "現股分點資料建置中"},
                         {"type": "tiles", "items": [{"label": "已建立交易日", "value": f"{have} / {want}", "tone": "accent"}]},
                         {"type": "note", "text": note}],
            "footer_text": history_footer(report)}


def branch_card(report: Dict[str, Any], names: Dict[str, str]) -> Dict[str, Any]:
    """「某分點現股最近買什麼」：只含本地已建置的股票；查不到就顯示查無資料（不改走權證）。"""
    def label(code: str) -> str:
        return f"{names.get(code, '')}（{code}）" if names.get(code) else code
    if not report.get("found"):
        return message_card(report.get("branch") or "現股分點", "查無該現股分點資料",
                            "本地只保存查詢過的股票；可以先查「股票代號＋現股籌碼」建立該股資料。")
    start, end = (report.get("dates") or ["", ""])[:2]
    return {"branch": report["branch"], "tags": ["現股分點籌碼"], "label": "現股分點籌碼", "sections": [
        {"type": "heading", "text": f"近{report.get('days', 20)}日現股買賣（{_slash(start)}～{_slash(end)}）"},
        {"type": "lists", "items": [
            {"title": "淨買超", "tone": "up", "rows": [{"name": label(c), "value": _lots(v), "extra": ""} for c, v in report["buy"]]},
            {"title": "淨賣超", "tone": "down", "rows": [{"name": label(c), "value": _lots(v), "extra": ""} for c, v in report["sell"]]}]},
        {"type": "note", "text": "※ 只含本地已建置過的股票，不是該分點全市場進出。"}]}


def summary_payload(report: Dict[str, Any]) -> Dict[str, Any]:
    """給 AI 解讀用的精簡現股籌碼（最新 Top5、5／20 日傾向、主要累積買超、VWAP）。"""
    periods = {p["days"]: p for p in report.get("periods") or [] if not p.get("insufficient")}
    return {
        "type": "現股券商分點籌碼（不是三大法人）",
        "data_date": report.get("latest_complete_date"), "date_note": report.get("date_note", ""),
        "history_days": f"{report.get('available_days', 0)}/{report.get('requested_days', REQUESTED_DAYS)}",
        "latest_top_buy": [{"branch": x["branch"], "net_lots": x["net"]} for x in report.get("latest_top_buy") or []],
        "latest_top_sell": [{"branch": x["branch"], "net_lots": x["net"]} for x in report.get("latest_top_sell") or []],
        "tendency": {f"{n}d": {k: periods[n].get(k) for k in ("buy_ratio", "sell_ratio", "net_concentration", "scenario")}
                     for n in (5, 20) if n in periods},
        "cumulative_buy_top3": [{"branch": x["branch"], "net_lots": x["net"]} for x in (report.get("cumulative_buy") or [])[:3]],
        "vwap": report.get("vwap"),
        "note": SOURCE_LIMITATION,
    }


def summary_card(report: Dict[str, Any]) -> Dict[str, Any]:
    """型態分析頁裡的「籌碼重點」：兩欄 Top3＋一列傾向／成本（精簡版；完整資料在現股籌碼頁）。"""
    latest = report.get("latest_complete_date") or ""
    sections: List[Dict[str, Any]] = []
    if report.get("date_note"):
        sections.append({"type": "note", "text": report["date_note"]})
    sections.append({"type": "lists", "items": [
        {"title": "最新 TOP3 買超", "tone": "up", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                        for x in (report.get("latest_top_buy") or [])[:3]]},
        {"title": "最新 TOP3 賣超", "tone": "down", "rows": [{"name": _branch_label(x), "value": _lots(x["net"]), "extra": ""}
                                                          for x in (report.get("latest_top_sell") or [])[:3]]}]})
    periods = {p["days"]: p for p in report.get("periods") or [] if not p.get("insufficient")}
    tiles = []
    for n in (5, 20):
        p = periods.get(n)
        if p and p.get("net_concentration") is not None:
            tiles.append({"label": f"近{n}日｜{p.get('scenario') or '中性'}", "value": f"{p['net_concentration']:+.2f}%",
                          "tone": "signed"})
        else:
            tiles.append({"label": f"近{n}日", "value": "資料不足", "tone": "ink"})
    vwap = report.get("vwap") or {}
    if vwap.get("vwap"):
        tiles.append({"label": f"近{vwap['days']}日均價（VWAP）", "value": f"{vwap['vwap']:,.2f}", "tone": "ink"})
        if vwap.get("gap_pct") is not None:
            tiles.append({"label": "現價與均價差距", "value": f"{vwap['gap_pct']:+.2f}%", "tone": "signed"})
    sections.append({"type": "tiles", "items": tiles})
    return {"branch": "籌碼重點", "tags": [f"現股分點｜{_slash(latest)}"] if latest else ["現股分點"],
            "label": f"歷史 {report.get('available_days', 0)} / {report.get('requested_days', REQUESTED_DAYS)} 個交易日",
            "sections": sections}


def message_card(title: str, badge: str, note: str = "") -> Dict[str, Any]:
    """查無資料／來源錯誤／今日未更新等狀態卡（一律圖片）。"""
    sections: List[Dict[str, Any]] = [{"type": "badge", "text": badge}]
    if note:
        sections.append({"type": "note", "text": note})
    return {"branch": title, "tags": ["現股分點籌碼"], "label": "現股分點籌碼", "sections": sections}
