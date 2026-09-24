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
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
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
# 會員這一題補歷史時同一檔並行幾條連線（全行程同時抓取的頁數上限也是這個數）；1＝逐頁
PARALLEL = max(1, int(os.getenv("DISCORD_AI_SPOT_PARALLEL", "4") or 4))
# 每頁間隔：原本 0.6 秒 × 71 頁光等待就 43 秒（實際抓取只要約 17 秒），改成 0.2 秒。
REQUEST_GAP = float(os.getenv("DISCORD_AI_SPOT_REQUEST_GAP", "0.2") or 0.2)
# 會員這一題最多等幾秒補歷史；沒補完的日期在背景繼續補，下一題就是完整資料。
BACKFILL_BUDGET = float(os.getenv("DISCORD_AI_SPOT_BACKFILL_BUDGET", "25") or 25)
BACKGROUND_BUDGET = float(os.getenv("DISCORD_AI_SPOT_BACKGROUND_BUDGET", "600") or 600)
# 型態＋籌碼整合頁：只等最近完整日這幾秒，歷史全部背景補
QUICK_BUDGET = float(os.getenv("DISCORD_AI_SPOT_QUICK_BUDGET", "8") or 8)
# 一般現股籌碼題（full／latest）同步階段只確保最近完整日，總時間上限；70 日歷史一律交給背景
SYNC_BUDGET = float(os.getenv("DISCORD_AI_SPOT_SYNC_BUDGET", "10") or 10)
# 單頁讀取逾時（秒）：來源很慢時不要一頁等 20 秒
HTTP_READ_TIMEOUT = float(os.getenv("DISCORD_AI_SPOT_HTTP_TIMEOUT", "10") or 10)
# 背景補資料排隊上限（排隊中＋執行中的股票數）；滿了這次就不排，下次有人查再試
MAX_PENDING_BACKFILLS = max(1, int(os.getenv("DISCORD_AI_SPOT_MAX_PENDING_BACKFILLS", "24") or 24))
# 來源連續失敗幾次就停止本輪，並讓新的抓取暫停 SOURCE_COOLDOWN 秒（已在 DB 的資料照常回答）
SOURCE_FAIL_LIMIT = max(1, int(os.getenv("DISCORD_AI_SPOT_SOURCE_FAIL_LIMIT", "3") or 3))
SOURCE_COOLDOWN = float(os.getenv("DISCORD_AI_SPOT_SOURCE_COOLDOWN", "45") or 45)
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

_SEMAPHORE = threading.BoundedSemaphore(max(MAX_CONCURRENCY, PARALLEL))
_BACKGROUND: set = set()
_BACKGROUND_GUARD = threading.Lock()
_STOCK_LOCKS: Dict[str, threading.Lock] = {}
_STOCK_LOCKS_GUARD = threading.Lock()
_SOURCE_STATE = {"down_until": 0.0, "fails": 0}
_FETCH_DEADLINE = threading.local()   # 這一頁抓取的截止時間（HTTP 逾時依剩餘時間縮短）
_SOURCE_GUARD = threading.Lock()


def source_cooling() -> bool:
    """富邦來源剛連續失敗：冷卻期間不再發新的抓取（會員仍用 DB 已有資料回答）。"""
    with _SOURCE_GUARD:
        return time.monotonic() < _SOURCE_STATE["down_until"]


_FOREGROUND_WAITING = [0]   # 正在等來源名額的會員題數；背景看到 >0 就先讓


def _acquire_source(deadline: float, background: bool) -> bool:
    """逐頁取得來源名額（全域 MAX_CONCURRENCY）。會員優先：背景在有會員等待時先讓出，不和會員搶。"""
    if background:
        while True:
            with _SOURCE_GUARD:
                waiting = _FOREGROUND_WAITING[0]
            if not waiting and _SEMAPHORE.acquire(timeout=0.2):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05 if waiting else 0)
    with _SOURCE_GUARD:
        _FOREGROUND_WAITING[0] += 1
    try:
        return _SEMAPHORE.acquire(timeout=max(0.5, deadline - time.monotonic()))
    finally:
        with _SOURCE_GUARD:
            _FOREGROUND_WAITING[0] -= 1


def _note_source(ok: bool) -> bool:
    """全程式累計富邦連續失敗次數（不同會員、不同請求都算）；達 SOURCE_FAIL_LIMIT 就冷卻，成功就歸零。回傳是否剛觸發冷卻。"""
    with _SOURCE_GUARD:
        _SOURCE_STATE["fails"] = 0 if ok else _SOURCE_STATE.get("fails", 0) + 1
        tripped = _SOURCE_STATE["fails"] >= SOURCE_FAIL_LIMIT
        if tripped:
            _SOURCE_STATE["fails"] = 0
    if tripped:
        _trip_source()
    return tripped


def _trip_source() -> None:
    with _SOURCE_GUARD:
        _SOURCE_STATE["down_until"] = time.monotonic() + SOURCE_COOLDOWN
    print(f"⚠️ 現股分點來源連續失敗 {SOURCE_FAIL_LIMIT} 次，暫停新的抓取 {SOURCE_COOLDOWN:.0f} 秒", flush=True)


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


# 被擋／錯誤頁的特徵（不用「登入」：正常頁首也有登入連結）
_BLOCK_MARKERS = ("captcha", "驗證碼", "access denied", "request rejected", "403 forbidden", "too many requests",
                  "請輸入驗證", "系統忙碌", "service unavailable")


_TITLE_CODE_RE = re.compile(r"<title>\s*主力賣買超-([0-9A-Za-z]+)\s*</title>")
_QUERY_DATE_RE = re.compile(r"getYMD([12])\s*=\s*'(\d{4}/\d{2}/\d{2})'")
_UPDATE_DATE_RE = re.compile(r"最後更新日：(\d{4}/\d{2}/\d{2})")


def validate_spot_snapshot(html: str, stock_code: str = "", date: str = "") -> Tuple[str, List[Dict[str, Any]], str]:
    """富邦 zco 頁完整性驗證 → (status, rows, detail)。
    complete：有買超／賣超表頭、兩側都有分點、每列買進／賣出都能解析、頁面沒被截斷、（有給代號時）頁面是這檔股票。
    no_data：頁面正常但當天沒有任何分點（富邦尚未更新或個股沒成交，由呼叫端依日期與市場資料判斷）。
    partial：只有一側、欄位解析異常或 HTML 被截斷；blocked：被擋／錯誤頁；source_error：找不到分點表格。
    買超側與賣超側都完整保留（不做 Top50 截斷）；net 一律用 買進−賣出 重算，不沿用來源的正負號或絕對值。"""
    text = str(html or "")
    low = text.lower()
    if not text.strip():
        return "source_error", [], "空白頁"
    if any(marker in low for marker in _BLOCK_MARKERS):
        return "blocked", [], "疑似被擋或錯誤頁"
    parser = _TableRows()
    parser.feed(text)
    header_index = buy_col = sell_col = -1
    for index, row in enumerate(parser.rows):
        if any("買超券商" in c for c in row) and any("賣超券商" in c for c in row):
            header_index = index
            buy_col = next(i for i, c in enumerate(row) if "買超券商" in c)
            sell_col = next(i for i, c in enumerate(row) if "賣超券商" in c)
            break
    if header_index < 0:
        if "查無" in text or "無資料" in text:
            return "no_data", [], "來源回覆查無資料"
        return "source_error", [], "找不到分點表格"
    if "</table>" not in low[low.find("買超券商".lower()):] or "</html>" not in low:
        return "partial", [], "HTML 被截斷"
    merged: Dict[str, Dict[str, Any]] = {}
    sides = {buy_col: 0, sell_col: 0}
    bad = 0
    for row in parser.rows[header_index + 1:]:
        for start in (buy_col, sell_col):
            if len(row) <= start:
                continue
            name = row[start].strip()
            if not name or any(word in name for word in ("合計", "平均", "買超券商", "賣超券商", "查無")):   # 「查無(台積電2330)…」列＝沒資料
                continue
            buy = _number(row[start + 1]) if len(row) > start + 1 else None
            sell = _number(row[start + 2]) if len(row) > start + 2 else None
            if buy is None or sell is None:
                bad += 1
                continue
            sides[start] += 1
            merged[name] = {"branch_name": name, "buy": buy, "sell": sell, "net": buy - sell}
    if bad:
        return "partial", [], f"{bad} 列買進／賣出欄位無法解析"
    if not merged:
        return "no_data", [], "頁面正常但沒有分點"
    if not sides[buy_col] or not sides[sell_col]:
        return "partial", [], "只有買超側" if sides[buy_col] else "只有賣超側"
    if stock_code:
        # 用頁面明確欄位驗證（<title>主力賣買超-代號</title>），不在全文找代號；找不到欄位就不能當完整
        found = _TITLE_CODE_RE.search(text)
        if not found:
            return "partial", [], "頁面沒有股票代號欄位"
        if found.group(1) != str(stock_code):
            return "source_error", [], f"頁面是 {found.group(1)}，不是這檔股票"
    if date:
        want = str(date).replace("-", "/")
        queried = dict(_QUERY_DATE_RE.findall(text))
        if not queried:
            return "partial", [], "頁面沒有查詢日期欄位"
        updated = _UPDATE_DATE_RE.search(text)
        if set(queried.values()) != {want} or (updated and updated.group(1) != want):
            return "source_error", [], f"頁面日期不是 {want}"
    return "complete", list(merged.values()), ""


def parse_zco_html(html: str) -> Tuple[str, List[Dict[str, Any]]]:
    """相容舊介面：回傳 (status, rows)；status 同 validate_spot_snapshot。"""
    status, rows, _ = validate_spot_snapshot(html)
    return status, rows


# ============================================================
# 抓取：同一輪 backfill 共用一個 session／瀏覽器
# ============================================================

def _source_url(stock_code: str, date: str) -> str:
    return f"{SOURCE_URL}?a={stock_code}&e={date}&f={date}"


@contextmanager
def open_source() -> Iterator[Callable[[str, str], str]]:
    """yield fetch(stock_code, date) -> html。HTTP 預設；selenium 模式整輪只開一個 Chrome。
    正式 Docker 沒有 Chromium／selenium：設成 selenium 但環境不支援時記 Log 並改用 HTTP，不讓整個功能崩潰。"""
    driver = None
    if FETCHER == "selenium":
        try:
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
        except Exception as exc:
            print(f"⚠️ DISCORD_AI_SPOT_FETCHER=selenium 但環境沒有可用的 selenium／Chromium，改用 HTTP｜"
                  f"{type(exc).__name__}: {exc}", flush=True)
            driver = None
    if driver is not None:
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
        # 逾時依這一題剩餘時間縮短：一頁很慢時不會把會員那一題拖過時限
        left = getattr(_FETCH_DEADLINE, "at", 0.0) - time.monotonic() if getattr(_FETCH_DEADLINE, "at", 0.0) else HTTP_READ_TIMEOUT
        read = max(1.0, min(HTTP_READ_TIMEOUT, left))
        response = session.get(_source_url(stock_code, date), timeout=(min(5.0, read), read))
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
    """預定交易日中「實際全市場休市」的日子（颱風假、臨時停止交易）：必須上市與上櫃都有交易所明確回覆
    當天無交易（local_market_cache.market_days = closed）。本地底庫沒資料不是休市證據，不會自行推論。"""
    try:
        return local_market_cache.market_closed_days(official)
    except local_market_cache.DBError:
        return []


def trading_dates(end: datetime, count: int) -> List[str]:
    """實際開市日（預定交易日扣掉確認休市日）；官方行事曆失敗時改用本地日 K 底庫已有的交易日。"""
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


# 最新日還沒更新時，70 日窗口以最近完整日為終點往前數，所以多準備幾天舊日期
WINDOW_BUFFER = 3


def candidate_dates(now: Optional[datetime] = None) -> Tuple[List[str], str]:
    """最近的實際交易日（舊→新，70＋緩衝天數）＋今天的狀態：
    today_ready（時間到了可以「嘗試」抓今天，不代表一定完成）／intraday（盤中或未到時間，不抓今天）／closed_day。"""
    now = now or tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    want = REQUESTED_DAYS + WINDOW_BUFFER
    days = trading_dates(now, want + 1)
    if today not in days:
        return days[-want:], "closed_day"
    if now.strftime("%H:%M") < TODAY_READY:
        return [d for d in days if d != today][-want:], "intraday"
    return days[-(want + 1):], "today_ready"


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


def _no_data_status(stock_code: str, date: str, latest_date: str) -> str:
    """富邦頁面正常但沒有分點時：
    - 最新交易日＝富邦可能還沒更新 → pending_update（不是停牌、不是 0 張）
    - 歷史日：該股所屬市場當天收盤快照 complete、快照裡確實沒有這檔（交易所＋富邦兩個來源都沒有）→ stock_no_trade
    - 其他（市場快照不完整、不知道所屬市場）→ retry，之後再確認"""
    if date >= latest_date:
        return "pending_update"
    if date in local_market_cache.stock_absent_confirmed(stock_code, [date]):
        return "stock_no_trade"
    return "retry"


def _fetch_status(state: str, stock_code: str, date: str, latest_date: str) -> str:
    """validate_spot_snapshot 的結果 → spot_branch_days 狀態。"""
    if state == "complete":
        return "complete"
    if state == "no_data":
        return _no_data_status(stock_code, date, latest_date)
    if state == "partial":
        return "pending_update" if date >= latest_date else "retry"   # 疑似不完整：不存 complete
    return "source_error"                                             # blocked／錯誤頁／找不到表格


def ensure_days(stock_code: str, dates: Sequence[str], budget: float = BACKFILL_BUDGET,
                now: Optional[datetime] = None, fetch_source=open_source, latest_date: str = "",
                bar_dates: Optional[Sequence[str]] = None, lock_wait: Optional[float] = None,
                background: bool = False) -> Dict[str, int]:
    """只補還沒確認的日期（新→舊）；每抓完一天立刻寫 SQLite；同股票同時只有一個執行緒在補（拿到鎖後重讀狀態，
    同一 stock＋date 不會重抓），全域同時最多 MAX_CONCURRENCY。latest_date＝目前最新交易日（查不到資料時判 pending_update）。
    回傳的 remaining 是「重新讀 DB 後仍未確認」的天數（嘗試過≠完成）。bar_dates 保留相容，不再用來判斷停牌。
    DB 讀取失敗（DBError）直接往上丟，不會因此整批重抓富邦。"""
    now = now or tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    latest_date = latest_date or (max(dates) if dates else today)
    lock = _stock_lock(stock_code)
    # 背景正在補同一檔時，會員這一題不排隊等它：直接用資料庫已有的資料回答。
    if not lock.acquire(timeout=max(0.0, budget if lock_wait is None else lock_wait)):
        return {"fetched": 0, "remaining": len(dates), "busy": 1}
    try:
        status = local_market_cache.spot_day_status(stock_code, dates)
        missing = sorted((d for d in dates if _needs_fetch(status.get(d), d, today, now)), reverse=True)
        fetched = errors = streak = 0
        if missing and source_cooling():
            return {"fetched": 0, "remaining": _unresolved(stock_code, dates, status), "cooldown": 1, "statuses": status}
        busy = 0
        if missing:
            deadline = time.monotonic() + budget
            # 會員這一題用 PARALLEL 條連線並行抓（每條各自的 session）；背景與 selenium 模式維持 1 條
            workers = 1 if background or FETCHER == "selenium" else max(1, min(PARALLEL, len(missing)))
            queue, guard, failure = deque(missing), threading.Lock(), []
            state = {"fetched": 0, "errors": 0, "streak": 0, "busy": 0, "stop": False}
            request_id = str(getattr(tools._API_REQUEST_LOCAL, "request_id", "") or "")

            def work() -> None:
                _FETCH_DEADLINE.at = deadline
                try:
                    _work()
                finally:
                    _FETCH_DEADLINE.at = 0.0   # 單一連線時在呼叫端執行緒跑，不能把截止時間留給下一題

            def _work() -> None:
                with tools.api_request_scope(request_id), fetch_source() as fetch:
                    while True:
                        with guard:
                            if state["stop"] or not queue:
                                return
                            date = queue.popleft()
                        if time.monotonic() >= deadline:
                            return
                        # 來源名額逐頁取得：背景一次補 70 天時，會員的抓取最多等一頁，不會被整批卡住
                        if not _acquire_source(deadline, background):
                            with guard:
                                state["busy"], state["stop"] = 1, True
                            return
                        if time.monotonic() >= deadline:   # 等名額時時間已到：不再開始新的抓取
                            _SEMAPHORE.release()
                            return
                        try:
                            failed = _fetch_one(fetch, stock_code, date, latest_date)
                        except local_market_cache.DBError as exc:
                            with guard:
                                failure.append(exc)
                                state["stop"] = True
                            return
                        finally:
                            _SEMAPHORE.release()
                        tripped = _note_source(not failed)   # 跨請求累計：不同會員各失敗一次也會觸發冷卻
                        with guard:
                            state["fetched"] += 1
                            state["errors"] += int(failed)
                            state["streak"] = state["streak"] + 1 if failed else 0
                            if (tripped or state["streak"] >= SOURCE_FAIL_LIMIT) and not state["stop"]:
                                state["stop"] = True
                                if not tripped:
                                    _trip_source()   # 來源異常（被擋、連線失敗）時不要一路打完 70 天
                            elif state["errors"] >= 3:
                                state["stop"] = True
                        time.sleep(REQUEST_GAP)

            if workers == 1:
                work()
            else:
                threads = [threading.Thread(target=work, name=f"spot-fetch-{i}", daemon=True) for i in range(workers)]
                for thread in threads:
                    thread.start()
                wait_until = deadline + 1.0   # 所有連線共用同一個等待截止（逾時已依剩餘時間縮短，這裡再給 1 秒緩衝）
                for thread in threads:
                    thread.join(max(0.0, wait_until - time.monotonic()))
            if failure:
                raise failure[0]
            fetched, errors, busy = state["fetched"], state["errors"], state["busy"]
        if fetched:
            status = local_market_cache.spot_day_status(stock_code, dates)   # 有寫入才重讀
        result = {"fetched": fetched, "remaining": _unresolved(stock_code, dates, status), "errors": errors,
                  "statuses": status}
        if busy and not fetched:
            result["busy"] = 1
        return result
    finally:
        lock.release()


def _fetch_one(fetch, stock_code: str, date: str, latest_date: str) -> bool:
    """抓一天、驗證、寫入（同一個 transaction）；回傳 True＝來源失敗。DBError 往上丟。"""
    started = time.perf_counter()
    try:
        state, rows, detail = validate_spot_snapshot(fetch(stock_code, date), stock_code, date)
        final = _fetch_status(state, stock_code, date, latest_date)
        local_market_cache.save_spot_day(stock_code, date, rows if final == "complete" else [],
                                         final, SOURCE_NAME, detail)
        tools.record_api_event("SpotBranch", status=200, latency=time.perf_counter() - started)
        return state in ("blocked", "source_error")
    except local_market_cache.DBError:
        raise
    except Exception as exc:   # 失敗不當成 0：記 source_error，已完成的日期保留
        local_market_cache.save_spot_day(stock_code, date, [], "source_error", SOURCE_NAME,
                                         f"{type(exc).__name__}: {exc}")
        tools.record_api_event("SpotBranch", status=500, latency=time.perf_counter() - started)
        return True


def _unresolved(stock_code: str, dates: Sequence[str], status: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    """還沒 complete／stock_no_trade／market_closed 的天數；status 沒給就重新讀 DB。"""
    if status is None:
        status = local_market_cache.spot_day_status(stock_code, dates)
    return sum(1 for d in dates if (status.get(d) or {}).get("status") not in CONFIRMED_STATUSES)


# 背景補資料：固定 worker 數的執行緒池（不再每檔開一條新 Thread），同一檔排隊中／執行中只算一次。
BACKGROUND_WORKERS = max(1, int(os.getenv("DISCORD_AI_SPOT_BACKGROUND_WORKERS", "1") or 1))
_BACKGROUND_POOL: Optional[ThreadPoolExecutor] = None


def _background_pool() -> ThreadPoolExecutor:
    global _BACKGROUND_POOL
    with _BACKGROUND_GUARD:
        if _BACKGROUND_POOL is None:
            _BACKGROUND_POOL = ThreadPoolExecutor(max_workers=BACKGROUND_WORKERS, thread_name_prefix="spot-backfill")
        return _BACKGROUND_POOL


def continue_in_background(stock_code: str, dates: Sequence[str], latest_date: str,
                           bar_dates: Sequence[str] = (), fetch_source=open_source) -> bool:
    """這一題的時間用完但還有日期沒補：交給背景 worker 繼續補（同一檔同時只排一次），不擋住 Discord 回覆。"""
    code = str(stock_code)
    if source_cooling():
        return False   # 來源冷卻中：不排背景，下次有人查再試
    with _BACKGROUND_GUARD:
        if code in _BACKGROUND or len(_BACKGROUND) >= MAX_PENDING_BACKFILLS:
            return False
        _BACKGROUND.add(code)

    def run() -> None:
        try:
            result = ensure_days(code, dates, budget=BACKGROUND_BUDGET, fetch_source=fetch_source,
                                 latest_date=latest_date, lock_wait=BACKGROUND_BUDGET, background=True)
            print(f"📚 現股分點背景補資料｜{code}｜嘗試 {result.get('fetched', 0)} 日｜仍未確認 {result.get('remaining', 0)}", flush=True)
        except Exception as exc:
            print(f"⚠️ 現股分點背景補資料失敗｜{code}｜{type(exc).__name__}: {exc}", flush=True)
        finally:
            with _BACKGROUND_GUARD:
                _BACKGROUND.discard(code)

    try:
        _background_pool().submit(run)
    except RuntimeError:   # 行程關閉中
        with _BACKGROUND_GUARD:
            _BACKGROUND.discard(code)
        return False
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


def listing_days(stock_code: str, dates: Sequence[str], bars: Dict[str, Tuple[float, float]]) -> Optional[int]:
    """新上市股票：有可靠證據（第一根日K 之前，所屬市場收盤快照 complete 卻沒有這檔）才回傳上市後的交易日數；
    否則回 None（requested_days 固定 70，不因行事曆暫時不足而縮短）。"""
    if not bars:
        return None
    first = min(bars)
    before = [d for d in dates if d < first]
    if not before:
        return None
    try:
        absent = local_market_cache.stock_absent_confirmed(stock_code, before[-3:])
    except local_market_cache.DBError:
        return None
    if len(absent) < min(3, len(before[-3:])):
        return None
    return sum(1 for d in dates if d >= first)


def analyze(stock_code: str, dates: Sequence[str], statuses: Dict[str, Dict[str, Any]],
            rows: Sequence[Dict[str, Any]], bars: Dict[str, Tuple[float, float]],
            requested: Optional[int] = None) -> Dict[str, Any]:
    complete = [d for d in dates if (statuses.get(d) or {}).get("status") == "complete"]
    latest = complete[-1] if complete else ""
    requested = int(requested or REQUESTED_DAYS)
    # 70 日窗口一律以「最近完整日」為終點往前數 70 個實際交易日：今天 pending_update 時不把今天塞進窗口（不會變成 69/70）
    dates = [d for d in dates if not latest or d <= latest][-requested:]
    complete = [d for d in complete if d in dates]
    confirmed = [d for d in dates if (statuses.get(d) or {}).get("status") in ("complete", "stock_no_trade")]
    by_date: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in rows:
        by_date[row["date"]][row["branch_name"]] = row["net"]
    # requested＝應有天數（行事曆暫時不足時不縮短）；resolved＝complete＋確認沒成交；data＝有分點資料；
    # unresolved＝pending_update／source_error／retry／還沒抓（含行事曆缺的天數）。market_closed 根本不在窗口裡。
    report: Dict[str, Any] = {"requested_days": requested, "resolved_days": len(confirmed), "data_days": len(complete),
                              "unresolved_days": max(0, requested - len(confirmed)),
                              "available_days": len(confirmed), "latest_complete_date": latest, "periods": [],
                              "source_limitation": SOURCE_LIMITATION,
                              "window_dates": list(dates), "complete_dates": list(complete)}   # 單一分點明細用
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
        # 分母完整性：窗口內每個有分點資料的日子都要有成交量，否則比例會被高估，不計算集中度
        volume_complete = all((bars.get(d) or (0, 0))[1] > 0 for d in window if d in complete)
        volume = sum((bars.get(d) or (0, 0))[1] for d in window)
        item.update(top15_buy=buy15, top15_sell=sell15)
        if not volume_complete:
            item["ratio_unavailable"] = True
        elif volume > 0:
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
                 budget: float = BACKFILL_BUDGET, fetch_source=open_source,
                 calendar: Optional[Tuple[List[str], str]] = None) -> Dict[str, Any]:
    """先回答、再背景補歷史：
    mode=full：先確保最近完整日，剩下的同步時間（總上限 SYNC_BUDGET）用 PARALLEL 條連線並行補 70 日，補不完的交給背景；
    mode=quick：只確保最近完整日（總上限 QUICK_BUDGET），歷史全部交給背景；
    兩者都先用本地已有資料回答（沒補完時圖上顯示「歷史 x / 70」）；
    mode=latest：只確保最近完整日，不排背景、不掃 70 日。
    calendar＝呼叫端已算好的 candidate_dates() 結果（同一題重用，不重查行事曆）。
    本地資料庫讀取失敗時丟 local_market_cache.DBError（不當成沒資料去整批重抓）。"""
    now = now or tools.taipei_now()
    t0 = time.perf_counter()
    dates, today_state = calendar if calendar is not None else candidate_dates(now)
    if not dates:
        return {"error": "trading_calendar", "requested_days": REQUESTED_DAYS, "available_days": 0}
    t_prepared = time.perf_counter()
    progress: Dict[str, Any] = {"fetched": 0, "remaining": 0, "errors": 0}
    total = min(budget, QUICK_BUDGET if mode == "quick" else SYNC_BUDGET)
    deadline = time.monotonic() + total
    statuses: Optional[Dict[str, Dict[str, Any]]] = None
    for date in reversed(dates[-4:]):   # 最新一天還沒更新（pending_update）才往前找，最多 4 天
        left = deadline - time.monotonic()
        if left <= 0:
            break
        step = ensure_days(stock_code, [date], budget=left, now=now, fetch_source=fetch_source,
                           latest_date=dates[-1], lock_wait=0.5)
        progress["fetched"] += step.get("fetched", 0)
        progress["errors"] = progress.get("errors", 0) + step.get("errors", 0)
        for key in ("cooldown", "busy"):
            if step.get(key):
                progress[key] = 1
        state = ((step.get("statuses") or local_market_cache.spot_day_status(stock_code, [date])).get(date) or {}).get("status")
        if state == "complete" or step.get("cooldown"):
            break
    left = deadline - time.monotonic()
    if mode == "full" and left > 1 and not progress.get("cooldown") and not progress.get("errors"):
        step = ensure_days(stock_code, dates, budget=left, now=now, fetch_source=fetch_source,
                           latest_date=dates[-1], lock_wait=0.5)
        progress["fetched"] += step.get("fetched", 0)
        progress["errors"] += step.get("errors", 0)
    t_fetched = time.perf_counter()
    statuses = local_market_cache.spot_day_status(stock_code, dates)   # 這一題只讀一次全窗口狀態，之後重用
    if mode != "latest":
        progress["remaining"] = _unresolved(stock_code, dates, statuses)
        if progress["remaining"] and not progress.get("errors"):
            # 來源這一題已經出錯就不排背景（避免一路撞壞掉的來源）；缺的歷史交給背景（同一檔只排一次、有佇列上限、來源冷卻中不排），這一題先用已有的資料回答
            progress["background"] = continue_in_background(stock_code, dates, dates[-1], (), fetch_source)
    complete = [d for d in dates if (statuses.get(d) or {}).get("status") == "complete"]
    rows = local_market_cache.load_spot_rows(stock_code, complete)
    bars = _bars(stock_code)
    report = analyze(stock_code, dates, statuses, rows, bars,
                     requested=min(REQUESTED_DAYS, listing_days(stock_code, dates, bars) or REQUESTED_DAYS))
    today = now.strftime("%Y-%m-%d")
    report["pending_update"] = [d for d in dates if (statuses.get(d) or {}).get("status") == "pending_update"]
    report.update(mode=mode, today_state=today_state, progress=progress,
                  source_errors=sum(1 for d in dates if (statuses.get(d) or {}).get("status") == "source_error"))
    latest = report.get("latest_complete_date")
    if today_state == "intraday":
        report["date_note"] = "盤中查詢・顯示最近完整交易日"
    elif today_state == "today_ready" and latest and latest != today:
        report["date_note"] = "今日分點資料尚未更新・目前顯示最近完整交易日"
    report["complete_all"] = complete   # 整個候選窗口的完整日（呼叫端算快取鍵用，不必再查 DB）
    if today_state in ("intraday", "today_ready") and latest != today:
        remember_for_today(stock_code, now)   # 今天資料還沒出來時被查過：資料一出來就先抓
    report["timing"] = {"spot_prepare": round(t_prepared - t0, 3), "spot_fetch": round(t_fetched - t_prepared, 3),
                        "spot_db": round(time.perf_counter() - t_fetched, 3)}
    return report


# ============================================================
# 今日待抓：當天資料出來前被查過的股票，富邦一更新就先抓當天那一頁
# （只用富邦分點頁，不用 FinMind／富果／Gemini）
# ============================================================

TODAY_PREFETCH_ENABLE = os.getenv("DISCORD_AI_SPOT_TODAY_PREFETCH_ENABLE", "1").strip() != "0"
TODAY_QUEUE_MAX = max(1, int(os.getenv("DISCORD_AI_SPOT_TODAY_QUEUE_MAX", "300") or 300))
TODAY_PREFETCH_BUDGET = float(os.getenv("DISCORD_AI_SPOT_TODAY_PREFETCH_BUDGET", "120") or 120)
_QUEUE_KEY = "spot_today_queue"
_QUEUE_GUARD = threading.Lock()


def remember_for_today(stock_code: str, now: Optional[datetime] = None) -> None:
    """記進今日待抓清單（存 SQLite kv，重啟不會不見；隔天自動換新清單）。失敗只略過，不影響回答。"""
    if not TODAY_PREFETCH_ENABLE:
        return
    today = (now or tools.taipei_now()).strftime("%Y-%m-%d")
    code = str(stock_code)
    try:
        with _QUEUE_GUARD:
            state = local_market_cache.get_state(_QUEUE_KEY, {}) or {}
            codes = list(state.get("codes") or []) if state.get("day") == today else []
            if code in codes or len(codes) >= TODAY_QUEUE_MAX:
                return
            local_market_cache.set_state(_QUEUE_KEY, {"day": today, "codes": codes + [code]})
    except Exception as exc:
        print(f"⚠️ 今日待抓清單寫入略過｜{code}｜{type(exc).__name__}: {exc}", flush=True)


def prefetch_today(now: Optional[datetime] = None, fetch_source=open_source,
                   log: Callable[[str], None] = print) -> Dict[str, Any]:
    """背景維護每輪呼叫：TODAY_READY 後，依序抓待抓清單上每檔的「今天」一頁。
    第一檔還是 pending_update（富邦還沒更新）就停，等 ensure_days 的 15 分鐘重試間隔到了再試；
    1 條連線、會員優先、會員正在查的那檔跳過、來源冷卻或出錯就停。"""
    now = now or tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    if not TODAY_PREFETCH_ENABLE or now.strftime("%H:%M") < TODAY_READY or source_cooling():
        return {"skipped": True}
    state = local_market_cache.get_state(_QUEUE_KEY, {}) or {}
    if state.get("day") != today or not state.get("codes"):
        return {"skipped": True}
    dates, today_state = candidate_dates(now)
    if today_state != "today_ready" or not dates or dates[-1] != today:
        return {"skipped": True}   # 今天不是交易日
    deadline, fetched, done, waiting = time.monotonic() + TODAY_PREFETCH_BUDGET, 0, 0, False
    for code in state["codes"]:
        left = deadline - time.monotonic()
        if left <= 1 or source_cooling():
            break
        result = ensure_days(code, [today], budget=left, now=now, fetch_source=fetch_source, latest_date=today,
                             lock_wait=0, background=True)
        fetched += result.get("fetched", 0)
        status = ((result.get("statuses") or {}).get(today) or {}).get("status")
        if status in CONFIRMED_STATUSES:
            done += 1
        elif status == "pending_update":
            waiting = True
            break   # 富邦今天還沒更新：其他股票也不用試
        if result.get("errors"):
            break
    if fetched:
        log(f"⚡ 現股分點今日待抓｜今日已完成 {done}/{len(state['codes'])} 檔｜本輪抓 {fetched} 頁"
            + ("｜富邦尚未更新，稍後再試" if waiting else ""))
    return {"fetched": fetched, "done": done, "waiting": waiting, "queued": len(state["codes"])}


_BRANCH_NAMES_CACHE: Dict[str, Any] = {"at": 0.0, "names": []}


def _branch_key(text: str) -> str:
    return re.sub(r"[\s\-－‐—_・·]+", "", str(text or "")).upper()


def _refresh_branch_names() -> None:
    try:
        _BRANCH_NAMES_CACHE.update(at=time.monotonic(), names=local_market_cache.spot_branch_names())
    finally:
        _BRANCH_NAMES_CACHE["refreshing"] = False


def known_branch_names() -> List[str]:
    """現股資料庫裡出現過的券商分點名稱；和權證分點名單分開。
    第一次同步載入；之後超過 60 秒改在背景重新整理，這一題先用現有名單（不讓每一題都查 SELECT DISTINCT）。"""
    if not _BRANCH_NAMES_CACHE["at"]:
        _refresh_branch_names()
    elif time.monotonic() - _BRANCH_NAMES_CACHE["at"] > 60 and not _BRANCH_NAMES_CACHE.get("refreshing"):
        _BRANCH_NAMES_CACHE["refreshing"] = True
        threading.Thread(target=_refresh_branch_names, name="spot-branch-names", daemon=True).start()
    return list(_BRANCH_NAMES_CACHE["names"])


def match_branch(question: str, names: Optional[Sequence[str]] = None, min_len: int = 3) -> str:
    """從問句找現股分點名稱（忽略空白與「-」，取最長的符合）；找不到回空字串。
    min_len=2 只給「已拿掉股票名稱」的問句用（美林、野村這類兩字分點）。"""
    key = _branch_key(question)
    best = ""
    for name in (names if names is not None else known_branch_names()):
        k = _branch_key(name)
        if len(k) >= min_len and k in key and len(k) > len(_branch_key(best)):
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


def branch_flow(stock_code: str, branch_name: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """指定分點在本檔 70 日窗口（build_report 的窗口）的每日買賣超＋統計；K 線副圖與 AI 解讀共用。
    沒上榜的日子不是 0 張而是「不在前段」：柱狀圖不畫，累積與區間合計以 0 計（近似值）。"""
    dates = list(report.get("window_dates") or [])
    window = set(dates)
    complete = [d for d in report.get("complete_dates") or [] if d in window]
    key = _branch_key(branch_name)
    rows = [r for r in local_market_cache.load_spot_rows(stock_code, complete) if _branch_key(r["branch_name"]) == key]
    daily = {r["date"]: r["net"] for r in rows}
    cumulative, running = {}, 0.0
    for d in dates:
        running += daily.get(d, 0.0)
        cumulative[d] = running

    def total(n: int) -> float:
        return sum(daily.get(d, 0.0) for d in dates[-n:])

    recent = [daily.get(d, 0.0) for d in dates[-20:]]
    streak = sign = 0
    for d in reversed(dates):   # 從最新一天往回數：連續買超為正、連續賣超為負，沒上榜就中斷
        now = 1 if daily.get(d, 0.0) > 0 else -1 if daily.get(d, 0.0) < 0 else 0
        if now == 0 or (sign and now != sign):
            break
        sign, streak = now, streak + now
    bars = _bars(stock_code)
    buys = [(bars[d][0], daily[d]) for d in dates[-20:] if daily.get(d, 0.0) > 0 and d in bars]
    cost = sum(p * v for p, v in buys) / sum(v for _, v in buys) if buys else None
    top_buy = max(daily.items(), key=lambda kv: kv[1]) if daily else None
    top_sell = min(daily.items(), key=lambda kv: kv[1]) if daily else None
    name = rows[0]["branch_name"] if rows else branch_name
    return {"found": bool(rows), "branch": name, "tag": broker_tag(name),
            "latest_date": report.get("latest_complete_date") or "", "date_note": report.get("date_note", ""),
            "latest_net": daily.get(report.get("latest_complete_date") or ""),
            "daily": daily, "cumulative": cumulative, "window_days": len(dates), "listed_days": len(daily),
            "available_days": report.get("available_days", 0), "requested_days": report.get("requested_days", REQUESTED_DAYS),
            "total_5d": total(5), "total_20d": total(20), "total_window": total(len(dates)),
            "buy_days_20": sum(1 for v in recent if v > 0), "sell_days_20": sum(1 for v in recent if v < 0),
            "streak": streak, "est_cost_20d": round(cost, 2) if cost else None,
            "largest_buy": top_buy if top_buy and top_buy[1] > 0 else None,
            "largest_sell": top_sell if top_sell and top_sell[1] < 0 else None,
            "cumulative_peak": max(cumulative.items(), key=lambda kv: kv[1]) if cumulative else None,
            "cumulative_trough": min(cumulative.items(), key=lambda kv: kv[1]) if cumulative else None}


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
    data = int(report.get("data_days", have) or 0)
    # 已確認＝有分點資料＋確認當天沒成交；例如 69 天有資料＋1 天停牌＝70/70 已確認、有效分點 69 日
    state = (f"歷史日期｜{have} / {want} 已確認｜有效分點資料｜{data} 日" if have >= want
             else f"歷史資料建置中｜{have} / {want} 個交易日")
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
                {"label": f"近{vwap['days']}日量價加權均價（估）", "value": f"{vwap['vwap']:,.2f}", "tone": "ink"},
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
    """給 AI 解讀用的精簡現股籌碼（最新 Top5、5／20 日傾向、主要累積買超、量價加權均價（估））。"""
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


def _int(value: float) -> int:
    return int(round(float(value or 0)))


def branch_flow_payload(flow: Dict[str, Any], stock_code: str, stock_name: str, bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """給 AI 解讀用：分點每日買賣超（只列上榜日）、轉折點、區間合計＋同期股價路徑與均線位置（bars＝K 線面板）。"""
    closes = {str(b.get("date", "")).replace("/", "-"): float(b["Close"]) for b in bars or [] if b.get("Close") is not None}
    last = dict((bars or [{}])[-1])
    close = last.get("Close")
    moving = {k: {"value": round(float(last[k]), 2), "position": "站上" if close >= last[k] else "跌破"}
              for k in ("MA5", "MA10", "MA20", "MA60") if last.get(k) and close}

    def at(item) -> Optional[Dict[str, Any]]:
        if not item:
            return None
        date, lots = item
        out = {"date": date, "lots": _int(lots)}
        if date in closes:
            out["close"] = round(closes[date], 2)
        return out

    window = [d for d in flow.get("cumulative") or {} if d in closes]
    path = {}
    if window:
        high, low = max(window, key=closes.get), min(window, key=closes.get)
        path = {"window_start": {"date": window[0], "close": round(closes[window[0]], 2)},
                "window_high": {"date": high, "close": round(closes[high], 2)},
                "window_low": {"date": low, "close": round(closes[low], 2)}}
    streak = int(flow.get("streak") or 0)
    return {
        "type": "單一現股券商分點在本檔的每日買賣超（張），不是三大法人、也不是權證分點",
        "stock_code": stock_code, "stock_name": stock_name, "branch": flow.get("branch"),
        "broker_type": f"{flow['tag']}券商" if flow.get("tag") else "",
        "data_date": flow.get("latest_date"), "date_note": flow.get("date_note", ""),
        "history_days": f"{flow.get('available_days', 0)}/{flow.get('requested_days', REQUESTED_DAYS)}",
        "window_days": flow.get("window_days"), "listed_days": flow.get("listed_days"),
        "latest_net_lots": _int(flow["latest_net"]) if flow.get("latest_net") is not None else "最新一日未上榜",
        "total_5d_lots": _int(flow.get("total_5d")), "total_20d_lots": _int(flow.get("total_20d")),
        "total_window_lots": _int(flow.get("total_window")),
        "buy_days_20": flow.get("buy_days_20"), "sell_days_20": flow.get("sell_days_20"),
        "streak": f"連續買超 {streak} 天" if streak > 0 else f"連續賣超 {-streak} 天" if streak < 0 else "",
        "largest_buy": at(flow.get("largest_buy")), "largest_sell": at(flow.get("largest_sell")),
        "cumulative_peak": at(flow.get("cumulative_peak")), "cumulative_trough": at(flow.get("cumulative_trough")),
        "est_cost_20d": flow.get("est_cost_20d"),
        "daily_net_lots": [{"date": d, "net": _int(v)} for d, v in sorted((flow.get("daily") or {}).items())],
        "price_date": str(last.get("date", "")).replace("/", "-"), "close": close,
        "moving_averages": moving, "price_path": path,
    }


def branch_flow_panel(flow: Dict[str, Any]) -> Dict[str, Any]:
    """K 線面板的 branch_flow 欄位（answer_image.draw_branch_flow 畫）。"""
    n = int(flow.get("window_days") or 0)
    latest = flow.get("latest_date") or ""
    parts = [f"近5日 {_lots(flow.get('total_5d') or 0)}",
             f"近20日 {_lots(flow.get('total_20d') or 0)}（買超 {flow.get('buy_days_20', 0)} 天／賣超 {flow.get('sell_days_20', 0)} 天）",
             f"上榜 {flow.get('listed_days', 0)}/{n} 天"]
    if int(flow.get("available_days") or 0) < int(flow.get("requested_days") or REQUESTED_DAYS):
        parts.append(f"歷史建置中 {flow.get('available_days', 0)}/{flow.get('requested_days', REQUESTED_DAYS)}")
    net = flow.get("latest_net")
    return {"branch": _branch_label(flow), "daily": dict(flow.get("daily") or {}), "cumulative": dict(flow.get("cumulative") or {}),
            "latest_label": f"最新 {_slash(latest)[5:]} " + (_lots(net) if net is not None else "未上榜"),
            "total_label": f"{n}日累積 {_lots(flow.get('total_window') or 0)}",
            "stats": "　".join(parts),
            "note": "※ 來源每日只列買賣超前段分點；未上榜的日子不畫柱、累積以 0 計（近似值）"
                    + ("；外資券商不等於外資法人" if flow.get("tag") == "外資" else "")}


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
        tiles.append({"label": f"近{vwap['days']}日量價加權均價（估）", "value": f"{vwap['vwap']:,.2f}", "tone": "ink"})
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
