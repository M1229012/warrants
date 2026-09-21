"""艾斯 AI 問答機器人的資料工具層（read-only）。

設計原則：
1. 所有數值都由既有週報主程式（K_function_warrant_report_20260530.py）的函式計算，
   這裡只負責「呼叫、篩選、聚合、整理成精簡 JSON」，不重寫第二套演算法。
2. 主程式以 importlib 載入一次；載入前強制設定唯讀旗標，
   避免 Discord 查詢的短區間權證資料被寫回 Google Sheet 快照。
3. 每個 Tool 只回傳回答問題需要的少量欄位，禁止把整張工作表或完整歷史交給 Gemini。
4. Discord AI 自己的快取一律使用 ``discord_ai_*`` 命名空間，
   不寫入、也不覆蓋週報的 weekly_keypoints / news_points 快取。
"""

from __future__ import annotations

import difflib
import importlib.util
import html
import json
import math
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from bollinger_analysis import analyze_bollinger
import local_market_cache


# ============================================================
# 環境設定
# ============================================================

def _env_int(name: str, default: int) -> int:
    """讀取整數環境變數；格式錯誤時回傳預設值。"""
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """讀取浮點數環境變數；格式錯誤時回傳預設值。"""
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except ValueError:
        return default


CORE_SCRIPT_ENV = "WARRANT_CORE_SCRIPT"
DEFAULT_CORE_SCRIPT = "K_function_warrant_report_20260530.py"
CORE_MODULE_NAME = "warrant_report_core"

# 週報產圖使用 fetch_stock_data_yf(stock_code, period="180d") 後取最後 70 根 K 棒；
# Discord AI 必須使用同一個抓取區間，大量區才會與圖卡完全一致。
PRICE_FETCH_PERIOD = "180d"

TTL_PRICE_SECONDS = _env_int("DISCORD_AI_TTL_PRICE_SECONDS", 600)
TTL_WARRANT_SECONDS = _env_int("DISCORD_AI_TTL_WARRANT_SECONDS", 1200)
TTL_NEWS_SECONDS = _env_int("DISCORD_AI_TTL_NEWS_SECONDS", 2700)
TTL_SHEET_SECONDS = _env_int("DISCORD_AI_TTL_SHEET_SECONDS", 1800)
TTL_BRANCH_PERF_SECONDS = _env_int("DISCORD_AI_TTL_BRANCH_PERF_SECONDS", 21600)
TTL_REFERENCE_SECONDS = _env_int("DISCORD_AI_TTL_REFERENCE_SECONDS", 43200)

WARRANT_TOPN = _env_int("DISCORD_AI_WARRANT_TOPN", 8)
WARRANT_JOIN_TOPN = _env_int("DISCORD_AI_WARRANT_JOIN_TOPN", 15)
HIGH_WIN_RATE_PCT = _env_float("DISCORD_AI_HIGH_WIN_RATE_PCT", 60.0)
# 分點相關問題一律先讀 Google Sheet（回測追蹤分點、A～E 事件、每日賣出明細）。
# MoneyDJ 即時逐權證抓取很吃記憶體與時間，只在明確開啟時才用。
LIVE_FLOW_ENABLE = os.getenv("DISCORD_AI_LIVE_FLOW_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
MONEYDJ_TOP_ENABLE = os.getenv("DISCORD_AI_MONEYDJ_TOP_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
CHART_MARK_MAX_EVENTS = _env_int("DISCORD_AI_CHART_MARK_MAX_EVENTS", 12)
# 隔日衝：買進後 N 個交易日內就「全部出清」的 A～E 事件，不在 K 線標註與分點動向表顯示（只影響顯示，不改回測勝率與評分）。
DAYTRADE_MAX_HOLD_DAYS = _env_int("DISCORD_AI_DAYTRADE_MAX_HOLD_DAYS", 2)
SHEET_CHIPS_MIN_SELL_AMOUNT = _env_float("DISCORD_AI_SHEET_CHIPS_MIN_SELL_AMOUNT", 100_000.0)
SMALL_SAMPLE_EVENTS = _env_int("DISCORD_AI_SMALL_SAMPLE_EVENTS", 10)
SHEET_QUERY_MAX_ROWS = 100
NEWS_MAX_ITEMS = _env_int("DISCORD_AI_NEWS_MAX_ITEMS", 8)
NEWS_SUMMARY_MAX_CHARS = 160
# 新聞：鉅亨網新聞搜尋 API（用股票代號查，含內文）優先，再補既有六來源標題。
# 不再抓一般新聞網站原文：Google News 連結解碼會卡住，大量解析網頁也曾讓 Discord 心跳逾時斷線。
NEWS_CNYES_ENABLE = os.getenv("DISCORD_AI_NEWS_CNYES_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
CNYES_SEARCH_URL = "https://ess.api.cnyes.com/ess/api/v1/news/keyword"
CNYES_NEWS_PAGE_URL = "https://news.cnyes.com/news/id/{}"
NEWS_LOOKBACK_DAYS = _env_int("DISCORD_AI_NEWS_LOOKBACK_DAYS", 14)
NEWS_CNYES_MAX_ITEMS = _env_int("DISCORD_AI_NEWS_CNYES_MAX_ITEMS", 8)
NEWS_CNYES_BODY_ITEMS = _env_int("DISCORD_AI_NEWS_CNYES_BODY_ITEMS", 5)
NEWS_ARTICLE_TIMEOUT = _env_float("DISCORD_AI_NEWS_ARTICLE_TIMEOUT", 6.0)
NEWS_BODY_BATCH_TIMEOUT = _env_float("DISCORD_AI_NEWS_BODY_BATCH_TIMEOUT", 8.0)
NEWS_CONTENT_MAX_CHARS = _env_int("DISCORD_AI_NEWS_CONTENT_MAX_CHARS", 1500)
TAIPEI_TZ = timezone(timedelta(hours=8))
TEXT_CELL_MAX_CHARS = 120

# ============================================================
# API 使用量 / 背景額度保護
# ============================================================

_API_LOCK = threading.RLock()
_API_EVENTS = defaultdict(lambda: deque(maxlen=5000))
_API_TOTALS = defaultdict(int)
_API_ERRORS = defaultdict(int)
_API_LATENCY = defaultdict(lambda: [0.0, 0])
_API_PRIORITY = threading.local()
_API_REQUEST_LOCAL = threading.local()
_API_REQUEST_COUNTS = defaultdict(lambda: defaultdict(int))
_API_REQUEST_LATENCY = defaultdict(lambda: defaultdict(float))
_API_REQUEST_LOCK = threading.RLock()
FUGLE_BACKGROUND_LIMIT_PER_MIN = max(1, _env_int("DISCORD_AI_FUGLE_BACKGROUND_LIMIT_PER_MIN", 15))
FUGLE_USER_RESERVE_PER_MIN = max(1, _env_int("DISCORD_AI_FUGLE_USER_RESERVE_PER_MIN", 20))
FUGLE_HARD_LIMIT_PER_MIN = min(59, max(10, _env_int("DISCORD_AI_FUGLE_HARD_LIMIT_PER_MIN", 55)))
FINMIND_USAGE_TTL = max(60, _env_int("DISCORD_AI_FINMIND_USAGE_TTL", 300))
_FINMIND_USAGE_CACHE = {"at": 0.0, "data": {}}


def record_api_event(provider: str, *, status: int = 200, latency: float = 0.0, detail: str = "") -> None:
    now = time.time()
    key = str(provider or "unknown")
    with _API_LOCK:
        _API_TOTALS[key] += 1
        _API_EVENTS[key].append(now)
        if status and int(status) >= 400:
            _API_ERRORS[key] += 1
        bucket = _API_LATENCY[key]
        bucket[0] += max(0.0, float(latency or 0.0)); bucket[1] += 1
    request_id = str(getattr(_API_REQUEST_LOCAL, "request_id", "") or "")
    if request_id:
        with _API_REQUEST_LOCK:
            _API_REQUEST_COUNTS[request_id][key] += 1
            _API_REQUEST_LATENCY[request_id][key] += max(0.0, float(latency or 0.0))




@contextmanager
def api_request_scope(request_id: str):
    """把 API 呼叫歸到單次 Discord 問答；worker thread 也可顯式帶入同一 request_id。"""
    old = str(getattr(_API_REQUEST_LOCAL, "request_id", "") or "")
    _API_REQUEST_LOCAL.request_id = str(request_id or "")
    try:
        yield
    finally:
        _API_REQUEST_LOCAL.request_id = old


def request_api_usage(request_id: str, *, clear: bool = False) -> Dict[str, Any]:
    rid = str(request_id or "")
    if not rid:
        return {}
    with _API_REQUEST_LOCK:
        counts = dict(_API_REQUEST_COUNTS.get(rid) or {})
        latency = dict(_API_REQUEST_LATENCY.get(rid) or {})
        result = {k: {"calls": int(v), "latency": round(float(latency.get(k, 0.0)), 3)} for k, v in counts.items()}
        if clear:
            _API_REQUEST_COUNTS.pop(rid, None)
            _API_REQUEST_LATENCY.pop(rid, None)
        return result


def run_tool_scoped(name: str, kwargs: Dict[str, Any], cancel_event: threading.Event, request_id: str = "") -> "ToolResult":
    with api_request_scope(request_id):
        return run_tool(name, kwargs, cancel_event)


def api_usage_snapshot() -> Dict[str, Any]:
    now = time.time()
    with _API_LOCK:
        result = {}
        for key in set(_API_TOTALS) | set(_API_EVENTS):
            events = _API_EVENTS[key]
            per_min = sum(1 for ts in events if now - ts <= 60)
            per_10m = sum(1 for ts in events if now - ts <= 600)
            total_latency, count = _API_LATENCY[key]
            result[key] = {"last_60s": per_min, "last_10m": per_10m, "process_total": _API_TOTALS[key],
                           "errors": _API_ERRORS[key], "avg_latency": round(total_latency / count, 3) if count else 0.0}
        return result


@contextmanager
def api_priority(priority: str):
    old = getattr(_API_PRIORITY, "value", "user")
    _API_PRIORITY.value = priority
    try:
        yield
    finally:
        _API_PRIORITY.value = old


def current_api_priority() -> str:
    return getattr(_API_PRIORITY, "value", "user")


def fugle_background_allowed() -> bool:
    snap = api_usage_snapshot().get("Fugle", {})
    used = int(snap.get("last_60s", 0))
    return used < min(FUGLE_BACKGROUND_LIMIT_PER_MIN, max(1, 60 - FUGLE_USER_RESERVE_PER_MIN))


def finmind_usage(force: bool = False) -> Dict[str, Any]:
    token = os.getenv("FINMIND_API_TOKEN", "").strip()
    if not token:
        return {"available": False, "reason": "no_token"}
    now = time.time()
    with _API_LOCK:
        if not force and now - float(_FINMIND_USAGE_CACHE.get("at", 0)) < FINMIND_USAGE_TTL:
            return dict(_FINMIND_USAGE_CACHE.get("data") or {})
    try:
        kf = core()
        started = time.perf_counter()
        response = kf.get_thread_session().get(
            "https://api.web.finmindtrade.com/v2/user_info",
            headers={"Authorization": f"Bearer {token}"}, timeout=(4, 6))
        status = int(response.status_code)
        record_api_event("FinMindUsage", status=status, latency=time.perf_counter()-started)
        response.raise_for_status()
        payload = response.json() or {}
        data = {"available": True, "used": int(payload.get("user_count") or 0),
                "limit": int(payload.get("api_request_limit") or 0)}
        data["remaining"] = max(0, data["limit"] - data["used"]) if data["limit"] else None
    except Exception as exc:
        data = {"available": False, "reason": type(exc).__name__}
    with _API_LOCK:
        _FINMIND_USAGE_CACHE["at"] = now; _FINMIND_USAGE_CACHE["data"] = dict(data)
    return data


# Bot 行程強制唯讀。這些值只影響 Discord Bot 自己的 process，不影響 GitHub Actions。
# - 關閉 Action 主控並啟用純 Live：fetch_warrant_events_full_market 不讀也不寫 Google Sheet 權證快照。
# - 關閉強制刷新：避免 WARRANT_CACHE_FORCE_REFRESH 讓快照寫入條件成立。
# - 關閉 Gemini Google Sheet 快取寫入與快取試算表自動建立。
_READ_ONLY_FORCED_ENV = {
    "WARRANT_ACTION_REFRESH_CONTROLS_REPORT_DATA": "0",
    "WARRANT_REPORT_LIVE_ONLY": "1",
    "WARRANT_ALWAYS_REFRESH_WARRANT_FLOW": "0",
    "WARRANT_CACHE_FORCE_REFRESH": "0",
    "WARRANT_LOCAL_CACHE_FORCE_REFRESH": "0",
    "WARRANT_LLM_CACHE_FORCE_REFRESH": "0",
    "WARRANT_ACTION_CACHE_ONLY_MODE": "0",
    "WARRANT_GSHEET_LLM_CACHE_WRITE_ENABLE": "0",
    "WARRANT_CACHE_GOOGLE_SHEET_AUTO_CREATE": "0",
}

# Bot 行程的預設值；使用者若在 Railway 另外設定，以使用者設定為準。
# Discord 問答不適合原本週報 5 次重試 × 遞增等待，預設縮短為 2 次。
_BOT_DEFAULT_ENV = {
    "MPLBACKEND": "Agg",
    "WARRANT_GEMINI_RETRY_TIMES": "2",
    "WARRANT_GEMINI_RETRY_BASE_WAIT": "2",
    "WARRANT_REPORT_TIMING_ENABLE": "0",
    # Railway 記憶體遠小於 GitHub Actions runner：MoneyDJ 併發從週報的 40／50 降到 12，
    # 並關閉「精選五分點 × 每支權證」補查（週報圖卡專用，單檔股票可多出上千組請求）。
    "WARRANT_MONEYDJ_RANGE_API4_WORKERS": "12",
    "WARRANT_MONEYDJ_RANGE_API5_WORKERS": "12",
    "WARRANT_HYBRID_MONEYDJ_API4_WORKERS": "12",
    "WARRANT_HYBRID_MONEYDJ_API5_WORKERS": "12",
    "WARRANT_SELECTED_BRANCH_FLOW_ENABLE": "0",
}


# ============================================================
# 主程式載入
# ============================================================

_CORE_MODULE = None
_CORE_LOCK = threading.Lock()


class ToolDataError(RuntimeError):
    """Tool 取得資料失敗；訊息會轉成 Discord 可讀的中文說明。"""


class SheetUnavailableError(ToolDataError):
    """Google Sheet 無法連線或工作表不存在。"""


def _resolve_core_script_path() -> str:
    """找出週報主程式路徑：優先環境變數，其次 repo 根目錄（本檔在 ace_ai/ 子資料夾），最後同資料夾。"""
    configured = os.getenv(CORE_SCRIPT_ENV, "").strip()
    if configured:
        return os.path.abspath(configured)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(here), DEFAULT_CORE_SCRIPT),
        os.path.join(here, DEFAULT_CORE_SCRIPT),
    ]
    return next((path for path in candidates if os.path.isfile(path)), candidates[0])


def normalize_gcp_service_key_env() -> None:
    """容錯整理 GCP_SERVICE_KEY：去掉誤貼的變數名稱、前後引號與三引號，驗證是合法 JSON 後壓成一行。

    Railway 等平台的變數值不適合多行，常見誤貼會讓主程式出現「GCP_SERVICE_KEY 解析失敗」。
    這裡只印出狀態與 client_email，絕不印出金鑰內容。
    """
    raw = os.environ.get("GCP_SERVICE_KEY", "")
    if not raw.strip():
        print("❌ GCP_SERVICE_KEY 未設定：Google Sheet 相關功能（分點勝率、A～E 事件、本週精選）無法使用")
        return
    text = raw.strip()
    text = re.sub(r"^\s*GCP_SERVICE_KEY\s*=\s*", "", text)
    # 整段被當成 JSON 字串存起來（外層雙引號＋內層 \"）時，先解一層字串。
    try:
        decoded = json.loads(text)
        if isinstance(decoded, str):
            text = decoded.strip()
    except json.JSONDecodeError:
        pass
    for _ in range(3):
        stripped = text.strip()
        for quote in ("'''", '"""', '"', "'"):
            if len(stripped) > 2 * len(quote) and stripped.startswith(quote) and stripped.endswith(quote):
                stripped = stripped[len(quote):-len(quote)].strip()
        if stripped == text:
            break
        text = stripped
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        try:
            info = json.loads(text.encode("utf-8").decode("unicode_escape"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(
                f"❌ GCP_SERVICE_KEY 不是完整 JSON（長度 {len(raw)} 字元，開頭 {raw.strip()[:3]!r}）｜{exc}｜"
                "請把 JSON 壓成單行後重新貼上"
            )
            return
    if not isinstance(info, dict) or info.get("type") != "service_account" or "private_key" not in info:
        print("❌ GCP_SERVICE_KEY 內容不是服務帳號金鑰（缺少 type=service_account 或 private_key）")
        return
    os.environ["GCP_SERVICE_KEY"] = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
    print(f"✅ GCP_SERVICE_KEY 格式正確｜服務帳號 {info.get('client_email', '-')}（請確認試算表已共用給這個 email）")


def apply_bot_process_env() -> Dict[str, str]:
    """在載入主程式前設定 Bot 行程專用環境變數，回傳被強制覆寫的項目。"""
    normalize_gcp_service_key_env()
    overridden = {}
    for key, value in _READ_ONLY_FORCED_ENV.items():
        previous = os.environ.get(key)
        if previous is not None and previous != value:
            overridden[key] = previous
        os.environ[key] = value
    for key, value in _BOT_DEFAULT_ENV.items():
        os.environ.setdefault(key, value)
    return overridden


def core():
    """載入並回傳週報主程式模組（整個行程只載入一次）。

    主程式的 main() 受 ``if __name__ == "__main__"`` 保護，import 時只會讀取設定與註冊字型，
    不會自動產生週報。
    """
    global _CORE_MODULE
    if _CORE_MODULE is not None:
        return _CORE_MODULE
    with _CORE_LOCK:
        if _CORE_MODULE is not None:
            return _CORE_MODULE
        path = _resolve_core_script_path()
        if not os.path.isfile(path):
            raise ToolDataError(
                f"找不到週報主程式：{path}｜請確認檔案存在或設定 {CORE_SCRIPT_ENV}"
            )
        overridden = apply_bot_process_env()
        for key, previous in overridden.items():
            print(f"🔒 Discord AI 唯讀保護：{key} 原值 {previous!r} 已改為 {_READ_ONLY_FORCED_ENV[key]!r}")
        started = time.perf_counter()
        spec = importlib.util.spec_from_file_location(CORE_MODULE_NAME, path)
        if spec is None or spec.loader is None:
            raise ToolDataError(f"無法載入週報主程式：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[CORE_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(CORE_MODULE_NAME, None)
            raise
        _CORE_MODULE = module
        print(
            f"✅ Discord AI 已載入週報主程式：{os.path.basename(path)}｜"
            f"{time.perf_counter() - started:.2f} 秒｜REPORT_LIVE_ONLY={module.REPORT_LIVE_ONLY}"
        )
        return module


# ============================================================
# 共用工具：TTL 快取、數值整理、日期
# ============================================================

class TTLCache:
    """Discord AI 專用的記憶體 TTL 快取；所有 key 自動加上 discord_ai_ 前綴。

    同一個 key 同時被多個 Tool 要求時，只會有一個執行緒真正去抓資料，其餘等待結果。
    """

    def __init__(self, namespace: str = "discord_ai") -> None:
        self.namespace = namespace
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._key_locks: Dict[str, threading.Lock] = {}

    def _full_key(self, key: str) -> str:
        return f"{self.namespace}_{key}"

    def get(self, key: str) -> Tuple[bool, Any]:
        """回傳 (是否命中, 值)。"""
        full_key = self._full_key(key)
        with self._lock:
            item = self._data.get(full_key)
            if item is None:
                return False, None
            expires_at, value = item
            if expires_at < time.time():
                self._data.pop(full_key, None)
                return False, None
            return True, value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._data[self._full_key(key)] = (time.time() + max(1.0, float(ttl_seconds)), value)

    def get_or_compute(self, key: str, ttl_seconds: float, compute: Callable[[], Any]) -> Tuple[Any, bool]:
        """命中就回傳快取；否則執行 compute 並寫入。回傳 (值, 是否命中)。"""
        hit, value = self.get(key)
        if hit:
            return value, True
        full_key = self._full_key(key)
        with self._lock:
            key_lock = self._key_locks.setdefault(full_key, threading.Lock())
        with key_lock:
            hit, value = self.get(key)
            if hit:
                return value, True
            value = compute()
            self.set(key, value, ttl_seconds)
            return value, False


CACHE = TTLCache("discord_ai")

_TOOL_CONTEXT = threading.local()


def _mark_cache(hit: bool) -> None:
    """記錄目前 Tool 是否命中快取，供 Debug 模式顯示。"""
    hits = getattr(_TOOL_CONTEXT, "cache_events", None)
    if hits is not None:
        hits.append(bool(hit))


def _cached(key: str, ttl_seconds: float, compute: Callable[[], Any]) -> Any:
    value, hit = CACHE.get_or_compute(key, ttl_seconds, compute)
    _mark_cache(hit)
    return value


def _num(value: Any, digits: int = 2) -> Optional[float]:
    """轉成 JSON 可序列化的數字；NaN / inf / 無法轉換回傳 None。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _pct(numerator: Any, denominator: Any, digits: int = 2) -> Optional[float]:
    num = _num(numerator, 10)
    den = _num(denominator, 10)
    if num is None or den is None or den == 0:
        return None
    return round((num / den - 1) * 100, digits)


def _fmt_date(value: Any) -> str:
    """統一日期輸出為 YYYY/MM/DD。"""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y/%m/%d")


def _taipei_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _clean_cell(value: Any) -> str:
    """Google Sheet 儲存格：去掉前導單引號與前後空白。"""
    text = str(value if value is not None else "").strip()
    if text.startswith("'"):
        text = text[1:].strip()
    return text


def _truncate(text: Any, limit: int = TEXT_CELL_MAX_CHARS) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _truncate_sentences(text: Any, limit: int) -> str:
    """長文截斷時停在句號、分號或換行，不把句子或數字切一半；找不到斷點才硬切並加「…」。"""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    cut = max(head.rfind(mark) for mark in ("。", "！", "？", "；", "\n"))
    if cut >= limit * 0.5:
        return head[: cut + 1].strip()
    return head.rstrip() + "…"


def turn_phrase(turn: str, day: Optional[int]) -> str:
    """均線轉向的時間用語：1＝明天、2＝後天、其餘「N 個交易日後」。"""
    if not turn or not day:
        return ""
    when = {1: "明天起", 2: "後天起"}.get(int(day), f"{int(day)} 個交易日後")
    return f"{when}{turn}"


def _money_text(value: Any) -> str:
    """沿用主程式 fmt_money 的「萬／億」金額格式。"""
    number = _num(value, 4)
    if number is None:
        return "-"
    return core().fmt_money(number)


def _normalize_branch(value: Any) -> str:
    return core().normalize_branch_name(str(value or ""))


# ============================================================
# Tool 執行結果
# ============================================================

@dataclass
class ToolResult:
    """單一 Tool 的執行結果；失敗時保留中文說明，不讓整個問答中斷。"""

    name: str
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    user_message: str = ""
    elapsed: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0

    def to_payload(self) -> Dict[str, Any]:
        """轉成交給 Gemini 的精簡結構。"""
        if self.ok:
            return self.data
        return {"available": False, "reason": self.user_message or "資料取得失敗"}


_TOOL_FAILURE_MESSAGES = {
    "get_stock_overview": "目前股價資料取得失敗",
    "get_technical_analysis": "目前股價／技術指標資料取得失敗",
    "get_volume_profile": "目前大量區資料取得失敗",
    "get_futures_positions": "台指期未平倉資料取得失敗",
    "get_warrant_branch": "目前權證分點資料取得失敗",
    "get_high_winrate_branches_buying": "目前高勝率分點資料取得失敗",
    "get_branch_performance": "歷史分點統計目前無法取得",
    "get_branch_recent_trades": "分點近期買賣資料目前無法取得",
    "get_branch_stock_history": "分點歷史事件資料目前無法取得",
    "get_recent_news": "目前新聞資料取得失敗",
    "get_branch_winrate_rank": "近10日分點勝率排行目前無法取得",
    "query_google_sheet": "資料查詢失敗",
}


_INTERNAL_WORDS = ("Sheet", "sheet", "工作表", "試算表", "GCP", "FinMind", "富果", "Fugle", "MoneyDJ", "gspread")


def _public_detail(exc: Exception) -> str:
    """錯誤原因可以給使用者看才附上（例如找不到分點）；含系統或資料供應商名稱的內部訊息一律不顯示。"""
    text = str(exc).strip()
    return "" if not text or any(word in text for word in _INTERNAL_WORDS) else text


def run_tool(name: str, kwargs: Dict[str, Any], cancel_event: Optional[threading.Event] = None) -> ToolResult:
    """執行指定 Tool，統一計時、快取統計與錯誤處理。"""
    function = TOOL_REGISTRY.get(name)
    if function is None:
        return ToolResult(name=name, ok=False, error="unknown tool", user_message=f"不支援的查詢：{name}")
    started = time.perf_counter()
    _TOOL_CONTEXT.cache_events = []
    call_kwargs = dict(kwargs or {})
    if name in _CANCELLABLE_TOOLS and cancel_event is not None:
        call_kwargs["cancel_event"] = cancel_event
    try:
        data = function(**call_kwargs)
        ok = True
        error = ""
        user_message = ""
    except SheetUnavailableError as exc:
        data, ok, error = {}, False, str(exc)
        user_message = _TOOL_FAILURE_MESSAGES.get(name, "資料取得失敗")
        print(f"⚠️ Discord AI Tool 失敗：{name}｜Google Sheet｜{exc}")
    except ToolDataError as exc:
        data, ok, error = {}, False, str(exc)
        detail = _public_detail(exc)
        user_message = _TOOL_FAILURE_MESSAGES.get(name, "資料取得失敗") + (f"（{detail}）" if detail else "")
        print(f"⚠️ Discord AI Tool 失敗：{name}｜{exc}")
    except Exception as exc:  # 既有函式可能丟出 requests / gspread / pandas 等各種例外
        data, ok = {}, False
        error = f"{type(exc).__name__}: {exc}"
        user_message = _TOOL_FAILURE_MESSAGES.get(name, "資料取得失敗")
        print(f"⚠️ Discord AI Tool 例外：{name}｜{error}")
    events = list(getattr(_TOOL_CONTEXT, "cache_events", []) or [])
    _TOOL_CONTEXT.cache_events = None
    return ToolResult(
        name=name,
        ok=ok,
        data=data if isinstance(data, dict) else {"value": data},
        error=error,
        user_message=user_message,
        elapsed=time.perf_counter() - started,
        cache_hits=sum(1 for hit in events if hit),
        cache_misses=sum(1 for hit in events if not hit),
    )


# ============================================================
# 股票代號／名稱與分點名稱辨識資料
# ============================================================

def get_stock_name_map() -> Dict[str, str]:
    """股票代號 → 名稱：TWSE／TPEx 官方基本資料為主，FinMind 股票清單補上 ETF 等其餘代號。"""

    def build() -> Dict[str, str]:
        kf = core()
        mapping: Dict[str, str] = {}
        try:
            mapping.update({str(k): str(v) for k, v in kf._official_stock_name_map().items() if k and v})
        except Exception as exc:  # 官方 OpenAPI 失敗時仍可使用 FinMind 清單
            print(f"⚠️ Discord AI 官方股票名冊讀取失敗：{type(exc).__name__}: {exc}")
        try:
            info = kf._finmind_load_stock_info()
            if info is not None and not info.empty and {"stock_id", "stock_name"}.issubset(info.columns):
                for code, name in zip(info["stock_id"].astype(str), info["stock_name"].astype(str)):
                    key = kf._normalize_stock_name_code_key(code)
                    if key and name and key not in mapping:
                        mapping[key] = name.strip()
        except Exception as exc:  # FinMind token 缺少或 API 失敗
            print(f"⚠️ Discord AI FinMind 股票清單讀取失敗：{type(exc).__name__}: {exc}")
        if not mapping:
            raise ToolDataError("股票名冊暫時無法取得")
        return mapping

    return _cached("stock_name_map", TTL_REFERENCE_SECONDS, build)


def resolve_stock_name(stock_code: str) -> str:
    """查股票名稱；名冊沒有時改用主程式單檔官方查詢。"""
    kf = core()
    code = kf._normalize_stock_name_code_key(stock_code)
    try:
        name = get_stock_name_map().get(code, "")
    except ToolDataError:
        name = ""
    if name:
        return name
    hit, cached = CACHE.get(f"stock_name_{code}")
    if hit:
        return cached
    try:
        name = str(kf.get_tw_stock_name(code) or "")
    except Exception as exc:
        raise ToolDataError(f"查不到股票代號 {code}") from exc
    CACHE.set(f"stock_name_{code}", name, TTL_REFERENCE_SECONDS)
    return name


def get_known_branches() -> Dict[str, str]:
    """已知分點別名 → 正式分點名稱（皆已 normalize_branch_name）。

    來源：週報使用的勝率統計、回測的近10日分點明細與股票ABCDE查詢資料。
    任一來源失敗只略過該來源；三個來源全部失敗時只快取 2 分鐘，避免 Sheet 暫時斷線後 6 小時都認不出分點。
    """
    hit, cached = CACHE.get("known_branches")
    _mark_cache(hit)
    if hit:
        return cached

    def build() -> Tuple[Dict[str, str], int]:
        kf = core()
        aliases: Dict[str, str] = {}

        def add(branch: Any, display: Any = "") -> None:
            canonical = kf.normalize_branch_name(str(branch or ""))
            if not canonical or canonical in ("未知分點",):
                return
            aliases.setdefault(canonical, canonical)
            display_norm = kf.normalize_branch_name(str(display or ""))
            if display_norm:
                aliases.setdefault(display_norm, canonical)

        sheet_sources_ok = 0
        try:
            perf = _read_branch_perf_df()
            for _, row in perf.iterrows():
                add(row.get("branch", ""), row.get("branch_display", ""))
            sheet_sources_ok += 1
        except Exception as exc:
            print(f"⚠️ Discord AI 分點清單（勝率統計）讀取失敗：{type(exc).__name__}: {exc}")
        for title in ("快取_近10日分點買賣明細", "股票ABCDE查詢資料"):
            try:
                table = read_sheet_table(title)["df"]
                if "分點" in table.columns:
                    names = table.get("分點名稱", pd.Series([""] * len(table)))
                    for branch, display in set(zip(table["分點"], names)):
                        add(branch, display)
                sheet_sources_ok += 1
            except Exception as exc:
                print(f"⚠️ Discord AI 分點清單（{title}）讀取失敗：{type(exc).__name__}: {exc}")
        # 官方證券商分點名冊（repo 內 securities_trader_seed.csv.gz，不依賴 Google Sheet）。
        # 只收「券商-分點」層級；總公司簡稱（例如「永豐金」）與股票名稱容易撞名，不列入。
        seed_count = 0
        try:
            for name in kf._load_trader_name_map().values():
                if "-" in str(name) and len(kf.normalize_branch_name(name)) >= 3:
                    add(name)
                    seed_count += 1
        except Exception as exc:
            print(f"⚠️ Discord AI 官方分點名冊讀取失敗：{type(exc).__name__}: {exc}")
        print(f"📋 Discord AI 分點清單：{len(set(aliases.values())):,} 個分點｜Google Sheet 來源成功 {sheet_sources_ok}/3｜官方名冊 {seed_count:,} 筆")
        return aliases, sheet_sources_ok

    aliases, sheet_sources_ok = build()
    # Sheet 全部讀取失敗時只快取 5 分鐘，之後重試；官方名冊部分仍可正常辨識分點。
    CACHE.set("known_branches", aliases, TTL_BRANCH_PERF_SECONDS if sheet_sources_ok else 300)
    return aliases


def suggest_branches(text: str, limit: int = 5) -> List[str]:
    """分點名稱模糊比對候選。"""
    target = _normalize_branch(text)
    if not target:
        return []
    known = get_known_branches()
    canonical_names = sorted(set(known.values()))
    contains = [name for name in canonical_names if target in name or name in target]
    close = difflib.get_close_matches(target, list(known.keys()), n=limit, cutoff=0.6)
    out: List[str] = []
    for name in contains + [known[c] for c in close]:
        if name not in out:
            out.append(name)
    return out[:limit]


def resolve_branch(text: str) -> Tuple[str, List[str]]:
    """回傳 (正式分點名稱, 候選清單)。唯一命中才回傳正式名稱。"""
    target = _normalize_branch(text)
    if not target:
        return "", []
    known = get_known_branches()
    if target in known:
        return known[target], []
    candidates = suggest_branches(target)
    if len(candidates) == 1:
        return candidates[0], []
    return "", candidates


# ============================================================
# Google Sheet 唯讀查詢層
# ============================================================

AMOUNT_CLASS_SHEETS = {
    "A": "A_基礎買超",
    "B": "B_明顯買超",
    "C": "C_強勢買超",
    "D": "D_大額布局",
    "E": "E_超大額布局",
}

EVENT_TYPE_LABELS = {
    "A": "A-基礎買超",
    "B": "B-明顯買超",
    "C": "C-強勢買超",
    "D": "D-大額布局",
    "E": "E-超大額布局",
}

# 允許 AI 查詢的工作表白名單。刻意排除大型工作表（例如 13,000 列以上的 TOP15 部位明細）
# 與純快取／狀態表，避免單次問答讀取過量資料。
SHEET_REGISTRY: Dict[str, str] = {
    "勝率統計": "每個分點的 A/B/C/D/E 事件與全部合併勝率、加權報酬、平均持有天數（分段式表格）",
    "股票ABCDE查詢資料": "每筆 ABCDE 大額買進事件（分點 × 標的股），含事件日、目前狀態、結果、出清獲利%、持有天數",
    "快取_近10日分點買賣明細": "追蹤分點近10日在各標的股的權證買賣超、淨額、報酬與判定",
    "快取_近10日分點勝率排行": "追蹤分點近10日勝率排行與主要交易標的",
    "券商查詢資料": "各分點累積淨買進標的排行與最近買進日",
    "近兩月買賣金額排行": "近兩月權證淨買進金額最高的標的與買進分點",
    "快取_近7日權證分點共識TOP15": "精選分點近7日共識買賣超權證 TOP15",
    "快取_TOP15共識淨買超": "近40日分點共識淨買超 TOP15 標的與型態",
    "每日賣出明細": "分點每日權證賣出（減碼／出清）明細與報酬率",
    **{title: f"{letter} 類事件明細（{EVENT_TYPE_LABELS[letter]}）" for letter, title in AMOUNT_CLASS_SHEETS.items()},
}

_BLOCK_TABLE_SHEETS = {"勝率統計"}
_HEADER_HINTS = ("分點", "標的股", "事件日", "排名", "統計日期", "事件類型", "日期", "權證代號")
_STOCK_COLUMNS = ("標的股", "股票代號")
_STOCK_NAME_COLUMNS = ("標的名稱",)
_BRANCH_COLUMNS = ("分點", "分點名稱")
_EVENT_COLUMNS = ("事件代碼", "事件類型", "事件")
_DATE_COLUMNS = ("事件日", "日期", "統計日期", "最近買進日", "最後筆日期")
_DROP_COLUMN_SUFFIXES = ("_JSON",)
_DROP_COLUMNS = {"run_id"}


def _open_main_spreadsheet():
    """開啟週報與回測共用的主試算表（沿用主程式 _open_gsheet 的授權與連線快取）。"""
    kf = core()
    sh = kf._open_gsheet()
    if sh is None:
        raise SheetUnavailableError("Google Sheet 無法連線，請確認 GCP_SERVICE_KEY 與試算表權限")
    return sh


def _spreadsheet_last_updated(sh) -> str:
    """盡量取得試算表最後更新時間；gspread 版本不支援時回傳空字串。"""
    for attr in ("get_lastUpdateTime", "lastUpdateTime"):
        try:
            value = getattr(sh, attr)
            value = value() if callable(value) else value
        except Exception as exc:  # Drive metadata 權限不足時不影響查詢
            print(f"ℹ️ Discord AI 無法取得試算表更新時間：{type(exc).__name__}: {exc}")
            return ""
        if value:
            try:
                ts = pd.Timestamp(value)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert("Asia/Taipei")
                return ts.strftime("%Y/%m/%d %H:%M")
            except (TypeError, ValueError):
                return str(value)
    return ""


def _detect_header_row(values: List[List[str]]) -> int:
    """前 6 列內命中最多已知欄位名稱的列視為表頭。"""
    best_idx, best_score = -1, 0
    for idx, row in enumerate(values[:6]):
        cells = {_clean_cell(c) for c in row}
        score = sum(1 for hint in _HEADER_HINTS if hint in cells)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_score >= 2 else -1


def _values_to_frame(values: List[List[str]], header_idx: int) -> pd.DataFrame:
    header = [_clean_cell(c) for c in values[header_idx]]
    columns: List[str] = []
    seen: Dict[str, int] = {}
    for idx, name in enumerate(header):
        base = name or f"欄{idx + 1}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        columns.append(base if count == 0 else f"{base}_{count + 1}")
    rows = []
    for raw in values[header_idx + 1:]:
        row = [_clean_cell(c) for c in raw]
        if not any(row):
            continue
        row = (row + [""] * len(columns))[: len(columns)]
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _block_table_to_frame(values: List[List[str]]) -> pd.DataFrame:
    """勝率統計這類「每個分點區塊重複表頭」的表格攤平成一張表。"""
    header: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    for raw in values:
        row = [_clean_cell(c) for c in raw]
        if not any(row):
            continue
        if "分點" in row and "事件類型" in row:
            header = row
            continue
        if header is None or row[0].startswith("分點："):
            continue
        record = {name: (row[i] if i < len(row) else "") for i, name in enumerate(header) if name}
        if record.get("分點"):
            rows.append(record)
    return pd.DataFrame(rows)


def read_sheet_table(title: str) -> Dict[str, Any]:
    """讀取白名單工作表並快取；回傳 {df, loaded_at, sheet_updated_at}。"""
    if title not in SHEET_REGISTRY:
        raise ToolDataError(f"工作表不在允許查詢清單：{title}")

    def build() -> Dict[str, Any]:
        sh = _open_main_spreadsheet()
        started = time.perf_counter()
        try:
            ws = sh.worksheet(title)
            values = ws.get_all_values()
            record_api_event("GoogleSheet", status=200, latency=time.perf_counter() - started)
        except Exception as exc:
            record_api_event("GoogleSheet", status=500, latency=time.perf_counter() - started)
            raise SheetUnavailableError(f"工作表「{title}」讀取失敗：{type(exc).__name__}") from exc
        if title in _BLOCK_TABLE_SHEETS:
            df = _block_table_to_frame(values)
        else:
            header_idx = _detect_header_row(values)
            df = _values_to_frame(values, header_idx) if header_idx >= 0 else pd.DataFrame()
        print(f"📥 Discord AI 讀取工作表：{title}｜{len(df):,} 列")
        return {
            "df": df,
            "loaded_at": _taipei_now().strftime("%Y/%m/%d %H:%M"),
            "sheet_updated_at": _spreadsheet_last_updated(sh),
        }

    return _cached(f"sheet_{title}", TTL_SHEET_SECONDS, build)


def _first_column(df: pd.DataFrame, candidates: Tuple[str, ...]) -> str:
    for name in candidates:
        if name in df.columns:
            return name
    return ""


def _event_letter(value: Any) -> str:
    """事件類型字串 → 單一 A~E 代碼；無法判斷時回傳空字串。"""
    text = _clean_cell(value).upper()
    match = re.match(r"^([A-E])(?:$|[-_\s])", text)
    return match.group(1) if match else ""


def _event_key(value: Any) -> str:
    """事件類型字串 → 正規化事件 key。

    支援單一事件（A～E）與 Sheet 內既有的複合事件（例如 A+C、A/C、A、C、A+C+E）。
    「全部」類列正規化為 ``overall``。這個函式只負責讀取 Sheet 已存在的統計名稱，
    不自行創造複合事件。
    """
    text = _clean_cell(value).upper().replace('＋', '+')
    if text.startswith('全部'):
        return 'overall'
    # 先抓獨立的 A～E 字母；避免把一般英文單字中的字母誤判成事件。
    letters = re.findall(r'(?<![A-Z])([A-E])(?![A-Z])', text)
    if not letters:
        single = _event_letter(text)
        return single
    # 複合事件一律依 A→E 排序，讓 Sheet 寫 C+A、A/C 或 A＋C 都能匹配同一個 A+C key。
    unique = set(letters)
    ordered = [code for code in EVENT_CODES if code in unique]
    return '+'.join(ordered)


def _parse_sheet_date(value: Any) -> Optional[pd.Timestamp]:
    parsed = core().parse_date(_clean_cell(value))
    return pd.Timestamp(parsed).normalize() if parsed else None


def _compact_row(row: pd.Series, columns: List[str]) -> Dict[str, str]:
    return {col: _truncate(row.get(col, "")) for col in columns}


def query_google_sheet(
    worksheet: str,
    stock_code: str = "",
    stock_name: str = "",
    branch: str = "",
    event_type: str = "",
    date_start: str = "",
    date_end: str = "",
    columns: Optional[List[str]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """唯讀、條件式查詢白名單工作表，只回傳篩選後的少量資料列。

    必須至少提供一個篩選條件（股票、分點、事件或日期），避免整張表被送出。
    日期欄存在時由新到舊排序；最多回傳 100 列。
    """
    kf = core()
    if not any([stock_code, stock_name, branch, event_type, date_start, date_end]):
        raise ToolDataError("Google Sheet 查詢必須指定至少一個篩選條件")
    table = read_sheet_table(worksheet)
    df = table["df"]
    if df.empty:
        return {
            "worksheet": worksheet,
            "matched_rows": 0,
            "returned_rows": 0,
            "rows": [],
            "sheet_updated_at": table["sheet_updated_at"],
        }

    mask = pd.Series(True, index=df.index)
    applied: Dict[str, str] = {}
    if stock_code:
        col = _first_column(df, _STOCK_COLUMNS)
        if not col:
            raise ToolDataError(f"工作表「{worksheet}」沒有股票欄位")
        code = kf._normalize_stock_name_code_key(stock_code)
        mask &= df[col].map(kf._normalize_stock_name_code_key) == code
        applied["stock_code"] = code
    if stock_name:
        col = _first_column(df, _STOCK_NAME_COLUMNS)
        if col:
            mask &= df[col].astype(str).str.strip() == str(stock_name).strip()
            applied["stock_name"] = str(stock_name).strip()
    if branch:
        cols = [c for c in _BRANCH_COLUMNS if c in df.columns]
        if not cols:
            raise ToolDataError(f"工作表「{worksheet}」沒有分點欄位")
        target = kf.normalize_branch_name(branch)
        branch_mask = pd.Series(False, index=df.index)
        for col in cols:
            branch_mask |= df[col].map(kf.normalize_branch_name) == target
        mask &= branch_mask
        applied["branch"] = target
    if event_type:
        letter = _event_letter(event_type)
        col = _first_column(df, _EVENT_COLUMNS)
        if letter and col:
            mask &= df[col].map(_event_letter) == letter
            applied["event_type"] = letter

    date_col = _first_column(df, _DATE_COLUMNS)
    work = df[mask].copy()
    if date_col and not work.empty:
        work["_date"] = work[date_col].map(_parse_sheet_date)
        start_ts = _parse_sheet_date(date_start) if date_start else None
        end_ts = _parse_sheet_date(date_end) if date_end else None
        if start_ts is not None:
            work = work[work["_date"].notna() & (work["_date"] >= start_ts)]
            applied["date_start"] = _fmt_date(start_ts)
        if end_ts is not None:
            work = work[work["_date"].notna() & (work["_date"] <= end_ts)]
            applied["date_end"] = _fmt_date(end_ts)
        work = work.sort_values("_date", ascending=False, na_position="last")

    usable_columns = [
        c for c in df.columns
        if c not in _DROP_COLUMNS and not c.endswith(_DROP_COLUMN_SUFFIXES)
    ]
    if columns:
        selected = [c for c in columns if c in usable_columns]
        usable_columns = selected or usable_columns
    limit = max(1, min(int(limit or 50), SHEET_QUERY_MAX_ROWS))
    rows = [_compact_row(row, usable_columns) for _, row in work.head(limit).iterrows()]
    return {
        "worksheet": worksheet,
        "filters": applied,
        "date_column": date_col,
        "matched_rows": int(len(work)),
        "returned_rows": len(rows),
        "truncated": bool(len(work) > limit),
        "rows": rows,
        "sheet_updated_at": table["sheet_updated_at"],
    }


def get_available_sheet_metadata() -> Dict[str, Any]:
    """列出可查詢的工作表、用途與欄位名稱（只讀前 6 列，不讀資料列）。"""

    def build() -> Dict[str, Any]:
        sh = _open_main_spreadsheet()
        try:
            existing = {ws.title: ws for ws in sh.worksheets()}
        except Exception as exc:
            raise SheetUnavailableError(f"工作表清單讀取失敗：{type(exc).__name__}") from exc
        sheets = []
        for title, description in SHEET_REGISTRY.items():
            ws = existing.get(title)
            if ws is None:
                continue
            columns: List[str] = []
            try:
                head = ws.get_values("1:6")
                if title in _BLOCK_TABLE_SHEETS:
                    header = next((r for r in head if "分點" in r and "事件類型" in r), [])
                else:
                    idx = _detect_header_row(head)
                    header = head[idx] if idx >= 0 else []
                columns = [
                    _clean_cell(c) for c in header
                    if _clean_cell(c) and _clean_cell(c) not in _DROP_COLUMNS
                    and not _clean_cell(c).endswith(_DROP_COLUMN_SUFFIXES)
                ]
            except Exception as exc:
                print(f"⚠️ Discord AI 工作表欄位讀取失敗：{title}｜{type(exc).__name__}: {exc}")
            sheets.append({"worksheet": title, "description": description, "columns": columns})
        return {"sheets": sheets, "sheet_updated_at": _spreadsheet_last_updated(sh)}

    return _cached("sheet_metadata", TTL_BRANCH_PERF_SECONDS, build)


# ============================================================
# 價格資料（Tool 1～3 共用，同一檔股票 10 分鐘內只抓一次）
# ============================================================

# ------------------------------------------------------------
# 富果（Fugle）行情：盤中用即時報價補上「今天這一根」；FinMind 日K抓不到時改用富果日K，K 線不留白。
# 需要 Railway 變數 FUGLE_API_KEY；沒設定就完全照舊只用 FinMind。
# ------------------------------------------------------------
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "").strip()
# 備援金鑰：主鑰額度用盡（429）或被拒（401/403）時自動換下一支，換完一輪才放棄。
FUGLE_API_KEYS = [k for k in (os.getenv(name, "").strip() for name in
                              ("FUGLE_API_KEY", "FUGLE_API_KEY_2", "FUGLE_API_KEY_3")) if k]
_FUGLE_KEY_INDEX = [0]


def _fugle_key() -> str:
    return FUGLE_API_KEYS[_FUGLE_KEY_INDEX[0] % len(FUGLE_API_KEYS)] if FUGLE_API_KEYS else FUGLE_API_KEY


def _fugle_rotate_key() -> bool:
    """換下一支金鑰；只剩一支就回 False。"""
    if len(FUGLE_API_KEYS) <= 1:
        return False
    _FUGLE_KEY_INDEX[0] = (_FUGLE_KEY_INDEX[0] + 1) % len(FUGLE_API_KEYS)
    print(f"🔁 富果改用備援金鑰 #{_FUGLE_KEY_INDEX[0] + 1}", flush=True)
    return True
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
FUGLE_TIMEOUT = _env_float("DISCORD_AI_FUGLE_TIMEOUT", 6.0)
INTRADAY_ENABLE = bool(FUGLE_API_KEYS or FUGLE_API_KEY) and os.getenv("DISCORD_AI_INTRADAY_ENABLE", "1").strip().lower() in ("1", "true", "yes", "on")
TTL_INTRADAY_SECONDS = _env_int("DISCORD_AI_TTL_INTRADAY_SECONDS", 60)
LIVE_PATTERN_SCORE = os.getenv("DISCORD_AI_LIVE_PATTERN_SCORE", "1").strip().lower() in ("1","true","yes","on")
MARKET_CLOSE_HHMM = (13, 30)


def taipei_now() -> datetime:
    return datetime.now(TAIPEI_TZ)


POST_CLOSE_RECHECK_MINUTES = max(15, _env_int("DISCORD_AI_POST_CLOSE_RECHECK_MINUTES", 15))
POST_CLOSE_QUERY_WINDOW_MINUTES = max(POST_CLOSE_RECHECK_MINUTES, _env_int("DISCORD_AI_POST_CLOSE_QUERY_WINDOW_MINUTES", 120))
_PROVISIONAL_CLOSE_LOCK = threading.RLock()
_PROVISIONAL_CLOSES: Dict[str, Dict[str, Any]] = {}

def intraday_session_now(now: Optional[datetime] = None) -> bool:
    """平日 08:55～收盤後查核視窗都維持短快取。

    13:30 後先視為「盤後暫定」。查核視窗預設延長到 15:30：
    - 使用者下一次再問同一股票時，短快取到期後會再向富果確認 isClose。
    - 已看過但仍未正式收盤的股票，背景每 15 分鐘最多再確認一次。
    這只針對曾被查詢的股票，不做全市場富果預抓。
    """
    now = now or taipei_now()
    if now.weekday() >= 5:
        return False
    end_minutes = 13 * 60 + 30 + POST_CLOSE_QUERY_WINDOW_MINUTES
    minutes = now.hour * 60 + now.minute
    return 8 * 60 + 55 <= minutes <= end_minutes


def _wait_for_fugle_user_slot(max_wait: float = 12.0) -> None:
    """避免直接使用者查詢把 Basic 60/min 撞到 429；背景工作不等待，直接讓位。"""
    started = time.monotonic()
    while True:
        now = time.time()
        with _API_LOCK:
            events = list(_API_EVENTS.get("Fugle", ()))
        recent = [ts for ts in events if now - ts <= 60]
        if len(recent) < FUGLE_HARD_LIMIT_PER_MIN:
            return
        if time.monotonic() - started >= max_wait:
            raise ToolDataError("富果即時行情額度暫時繁忙，已保留避免超過每分鐘限制")
        oldest = min(recent)
        wait_for = max(0.2, min(2.0, 60.05 - (now - oldest)))
        time.sleep(wait_for)


def _fugle_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if current_api_priority() == "background":
        if not fugle_background_allowed():
            raise ToolDataError("富果背景額度已保留給使用者查詢")
    else:
        _wait_for_fugle_user_slot()
    kf = core()
    attempts = max(1, len(FUGLE_API_KEYS) or 1)
    last_error: Optional[Exception] = None
    for _ in range(attempts):
        started = time.perf_counter(); status = 0
        try:
            response = kf.get_thread_session().get(
                f"{FUGLE_BASE_URL}/{path}", params=params or {},
                headers={"X-API-KEY": _fugle_key(), "Accept": "application/json"},
                timeout=(4, FUGLE_TIMEOUT),
            )
            status = int(response.status_code)
            if status in (401, 403, 429):
                last_error = ToolDataError(f"富果 API 回應 {status}")
                if _fugle_rotate_key():
                    continue
                raise ToolDataError("富果 API 超過每分鐘呼叫上限" if status == 429 else f"富果 API 金鑰被拒（{status}）")
            response.raise_for_status()
            return response.json() or {}
        finally:
            record_api_event("Fugle", status=status, latency=time.perf_counter()-started)
    raise last_error or ToolDataError("富果 API 無可用金鑰")


def _fugle_timestamp(value: Any) -> Optional[datetime]:
    """lastUpdated 可能是毫秒或微秒的 Unix 時間。"""
    number = _num(value, 0)
    if not number:
        return None
    seconds = number / 1e6 if number > 1e14 else number / 1e3
    return datetime.fromtimestamp(seconds, TAIPEI_TZ)


def fetch_fugle_quote(stock_code: str) -> Dict[str, Any]:
    """富果盤中即時報價（intraday/quote）；還沒有成交（開盤前、試撮中）或查不到時回傳空 dict。"""
    data = _fugle_get(f"intraday/quote/{stock_code}")
    if data.get("isTrial"):
        return {}
    close = _num(data.get("closePrice")) or _num(data.get("lastPrice"))
    day = _parse_sheet_date(data.get("date"))
    if not close or day is None:
        return {}
    open_ = _num(data.get("openPrice")) or close
    high = max(_num(data.get("highPrice")) or close, open_, close)
    low = min(_num(data.get("lowPrice")) or close, open_, close)
    updated = _fugle_timestamp(data.get("lastUpdated"))
    close_confirmed = bool(data.get("isClose"))
    # 不再用 13:30 時鐘硬判收盤；延遲收盤時只要 isClose 尚未成立，就仍屬暫定資料。
    live = not close_confirmed
    return {
        "date": pd.Timestamp(day).normalize(),
        "time": updated.strftime("%H:%M") if updated else "",
        "open": open_, "high": high, "low": low, "close": close,
        "trade_volume": _num((data.get("total") or {}).get("tradeVolume"), 0) or 0.0,
        "is_live": bool(live), "is_close_confirmed": close_confirmed,
    }


# 富果盤中報價 total.tradeVolume 的單位（固定換算，不靠成交量大小猜）：lots＝張（×1000 換成股）、shares＝股。
# 上線後看啟動 Log「富果成交量單位校正」確認；結果和預設不同時用 Railway 變數 FUGLE_QUOTE_VOLUME_UNIT 改。
FUGLE_QUOTE_VOLUME_UNIT = os.getenv("FUGLE_QUOTE_VOLUME_UNIT", "lots").strip().lower()
VOLUME_SUSPECT_HIGH_RATIO = _env_float("DISCORD_AI_VOLUME_SUSPECT_HIGH", 30.0)
VOLUME_SUSPECT_LOW_RATIO = _env_float("DISCORD_AI_VOLUME_SUSPECT_LOW", 0.001)


def _quote_volume_shares(trade_volume: float) -> float:
    """盤中累計量依設定的固定單位換成「股」。"""
    return float(trade_volume or 0) * (1 if FUGLE_QUOTE_VOLUME_UNIT in ("shares", "share", "股") else 1000)


def _volume_suspect(volume_shares: float, stock_df: pd.DataFrame) -> bool:
    """換算後和近 20 日均量差距離譜（例如 30 倍以上或千分之一以下）只標記為可疑，不拿來切換單位。"""
    recent = pd.to_numeric(stock_df["Volume"], errors="coerce").tail(20).mean()
    if not volume_shares or not recent or not math.isfinite(recent) or recent <= 0:
        return False
    ratio = volume_shares / recent
    return ratio > VOLUME_SUSPECT_HIGH_RATIO or ratio < VOLUME_SUSPECT_LOW_RATIO


def calibrate_fugle_volume_unit(stock_code: str = "2330") -> str:
    """收盤後比對同一天「盤中報價累計量」與「日K成交量（股）」，確認盤中量的單位；只寫 Log，不改設定。"""
    if not (FUGLE_API_KEYS or FUGLE_API_KEY):
        return "未設定 FUGLE_API_KEY，略過"
    quote = _fugle_get(f"intraday/quote/{stock_code}")
    if not quote.get("isClose"):
        return "今天尚未收盤（或非交易日），收盤後重新部署才能校正"
    day = str(quote.get("date") or "")
    candles = _fugle_get(f"historical/candles/{stock_code}", {"from": day, "to": day, "timeframe": "D", "fields": "volume"})
    rows = candles.get("data") or []
    trade_volume = _num((quote.get("total") or {}).get("tradeVolume"), 0)
    daily_volume = _num(rows[0].get("volume"), 0) if rows else None
    if not trade_volume or not daily_volume:
        return f"{day} 資料不足，無法校正"
    ratio = daily_volume / trade_volume
    unit = "lots" if 500 <= ratio <= 2000 else "shares" if 0.5 <= ratio <= 2 else "unknown"
    verdict = "與設定一致" if unit == FUGLE_QUOTE_VOLUME_UNIT else f"與設定不同，請把 FUGLE_QUOTE_VOLUME_UNIT 設為 {unit}"
    return f"{stock_code} {day}：盤中累計量 {trade_volume:,.0f}、日K成交量 {daily_volume:,.0f} 股，比例 {ratio:,.1f} → 單位 {unit}（目前設定 {FUGLE_QUOTE_VOLUME_UNIT}，{verdict}）"


def fetch_fugle_daily(stock_code: str, calendar_days: int) -> pd.DataFrame:
    """富果日K（historical/candles，免費方案可用；日K成交量單位為股，和 FinMind 相同）。FinMind 失敗時的備援。"""
    end = taipei_now().date()
    start = end - timedelta(days=min(int(calendar_days), 360))
    data = _fugle_get(f"historical/candles/{stock_code}", {
        "from": start.isoformat(), "to": end.isoformat(), "timeframe": "D",
        "fields": "open,high,low,close,volume", "sort": "asc",
    })
    rows = data.get("data") or []
    if not rows:
        raise ToolDataError(f"{stock_code} 富果日K沒有資料")
    df = pd.DataFrame(rows).rename(columns={"date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        df[column] = pd.to_numeric(df.get(column), errors="coerce")
    return (df.dropna(subset=["Date", "Open", "High", "Low", "Close"])
            .drop_duplicates(subset=["Date"], keep="last").sort_values("Date").set_index("Date")
            [["Open", "High", "Low", "Close", "Volume"]])


def _append_intraday_bar(code: str, stock_df: pd.DataFrame, market: str = "") -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """日K還沒有今天這一根時，按需接上富果「今天」的 OHLCV。

    富果只抓使用者正在問／背景允許的股票，不做全市場預抓。13:30 後若 isClose 尚未確認，
    仍保留為盤後暫定並在短快取失效後重新確認。只有 isClose=True 才寫入持久化日K。
    """
    now = taipei_now()
    if not INTRADAY_ENABLE or now.weekday() >= 5 or (now.hour, now.minute) < (9, 0):
        return stock_df, {}
    if current_api_priority() == "background":
        # 背景掃描（型態分數、底庫維護）一律只用已收盤 K 棒，
        # 否則全市場逐檔抓即時報價會把每分鐘額度吃光，使用者問的那一檔反而拿不到。
        return stock_df, {}
    last_date = pd.Timestamp(stock_df.index.max()).normalize()
    if last_date >= pd.Timestamp(now.date()):
        return stock_df, {}
    try:
        quote = fetch_fugle_quote(code)
    except Exception as exc:  # 富果失敗不影響既有日K
        print(f"⚠️ {code} 富果即時報價略過：{type(exc).__name__}: {exc}", flush=True)
        return stock_df, {}
    if not quote or quote["date"] <= last_date:
        return stock_df, {}
    volume_shares = _quote_volume_shares(quote["trade_volume"])
    bar = pd.DataFrame(
        [[quote["open"], quote["high"], quote["low"], quote["close"], volume_shares]],
        index=pd.DatetimeIndex([quote["date"]]), columns=["Open", "High", "Low", "Close", "Volume"],
    )
    merged = pd.concat([stock_df[["Open", "High", "Low", "Close", "Volume"]], bar])
    confirmed = bool(quote.get("is_close_confirmed"))
    info = {"date": _fmt_date(quote["date"]), "time": quote["time"], "is_live": quote["is_live"],
            "is_close_confirmed": confirmed,
            "post_close_provisional": bool(not confirmed and (now.hour, now.minute) >= MARKET_CLOSE_HHMM),
            "cumulative_volume_lots": _num(volume_shares / 1000, 0),
            "volume_suspect": _volume_suspect(volume_shares, stock_df)}
    if confirmed:
        local_market_cache.save_confirmed_bar(code, quote["date"], quote["open"], quote["high"], quote["low"],
                                              quote["close"], volume_shares, market=market, source="Fugle-close")
        with _PROVISIONAL_CLOSE_LOCK:
            _PROVISIONAL_CLOSES.pop(code, None)
    elif info["post_close_provisional"]:
        with _PROVISIONAL_CLOSE_LOCK:
            _PROVISIONAL_CLOSES[code] = {"last_checked": time.time(), "market": market, "date": quote.get("date", "")}
    if info["volume_suspect"]:
        print(f"⚠️ {code} 盤中累計量和近 20 日均量差距異常（{volume_shares:,.0f} 股），已標記為可疑", flush=True)
    state = "正式收盤" if confirmed else ("盤後暫定" if info["post_close_provisional"] else "盤中暫定")
    print(f"⏱️ {code} 接上富果即時報價：{info['date']} {info['time']}｜{quote['close']}｜{state}", flush=True)
    return merged, info


def provisional_close_stats() -> Dict[str, Any]:
    with _PROVISIONAL_CLOSE_LOCK:
        return {"count": len(_PROVISIONAL_CLOSES), "codes": list(_PROVISIONAL_CLOSES)[:20]}

def recheck_provisional_closes(max_items: int = 5) -> Dict[str, int]:
    """背景只重查「今天已被使用者問過、13:30 後仍未確認收盤」的股票。

    每檔至少隔 POST_CLOSE_RECHECK_MINUTES 才再查一次，且走 background API budget；
    不掃全市場，因此不會用這個功能把 Fugle 60/min 吃滿。
    """
    now = taipei_now()
    if now.weekday() >= 5 or (now.hour * 60 + now.minute) < 13 * 60 + 30:
        return {"checked": 0, "confirmed": 0, "pending": provisional_close_stats()["count"]}
    due_before = time.time() - POST_CLOSE_RECHECK_MINUTES * 60
    with _PROVISIONAL_CLOSE_LOCK:
        due = [(code, dict(info)) for code, info in _PROVISIONAL_CLOSES.items() if float(info.get("last_checked", 0)) <= due_before]
    checked = confirmed_count = 0
    for code, info in due[:max(1, int(max_items))]:
        if not fugle_background_allowed():
            break
        try:
            with api_priority("background"):
                quote = fetch_fugle_quote(code)
            checked += 1
            if quote.get("is_close_confirmed"):
                volume_shares = _quote_volume_shares(float(quote.get("trade_volume") or 0))
                local_market_cache.save_confirmed_bar(
                    code, quote["date"], quote["open"], quote["high"], quote["low"], quote["close"],
                    volume_shares, market=str(info.get("market") or ""), source="Fugle-close-recheck")
                with _PROVISIONAL_CLOSE_LOCK:
                    _PROVISIONAL_CLOSES.pop(code, None)
                confirmed_count += 1
                print(f"✅ {code} 延遲收盤重新確認完成｜{quote.get('date')} {quote.get('time')}", flush=True)
            else:
                with _PROVISIONAL_CLOSE_LOCK:
                    if code in _PROVISIONAL_CLOSES:
                        _PROVISIONAL_CLOSES[code]["last_checked"] = time.time()
                print(f"⏳ {code} 延遲收盤仍未確認｜{quote.get('date')} {quote.get('time')}", flush=True)
        except Exception as exc:
            with _PROVISIONAL_CLOSE_LOCK:
                if code in _PROVISIONAL_CLOSES:
                    _PROVISIONAL_CLOSES[code]["last_checked"] = time.time()
            print(f"⚠️ {code} 延遲收盤重新確認失敗｜{type(exc).__name__}", flush=True)
    return {"checked": checked, "confirmed": confirmed_count, "pending": provisional_close_stats()["count"]}


def price_source_note(bundle: Dict[str, Any]) -> str:
    """給 AI 與圖片看的資料說明；盤中／盤後暫定都明確標示。"""
    info = bundle.get("intraday") or {}
    if not info:
        return "日K收盤資料（非盤中即時）"
    if info.get("post_close_provisional"):
        return f"日K＋今日盤後暫定報價（{info['date']} {info['time']}；將再次確認是否完成收盤）"
    if info.get("is_live"):
        return f"日K＋盤中即時報價（{info['date']} {info['time']}，今天的 K 棒、均線與型態分數仍會變動；最終以收盤為準）"
    return f"日K＋今日正式收盤報價（{info['date']}）"



# ============================================================
# 大盤／櫃買指數（FinMind TaiwanStockPrice 的 TAIEX／TPEx，與個股同一條資料線）
# ============================================================

INDEX_CODES = {"TAIEX": "加權指數", "TPEX": "櫃買指數"}
INDEX_FINMIND_ID = {"TAIEX": "TAIEX", "TPEX": "TPEx"}
_INDEX_ALIASES = (
    ("TAIEX", ("大盤", "加權指數", "加權", "台股指數", "台股大盤", "集中市場", "TAIEX", "台積電權值")),
    ("TPEX", ("櫃買", "櫃買指數", "上櫃指數", "OTC指數", "櫃檯買賣", "TPEX", "OTC")),
)


def resolve_index_code(text: str) -> str:
    """問句裡有沒有指到大盤或櫃買；兩個都提到時以先出現的為準。"""
    value = str(text or "").upper()
    found = []
    for code, names in _INDEX_ALIASES:
        for name in names:
            position = value.find(str(name).upper())
            if position >= 0:
                found.append((position, code))
                break
    return min(found)[1] if found else ""


def _fetch_index_daily(code: str) -> pd.DataFrame:
    """指數日K：欄位與個股完全相同，後面所有指標、型態評分都能直接沿用。"""
    kf = core()
    end = taipei_now()
    start = end - timedelta(days=400)
    started = time.perf_counter()
    try:
        raw = kf._finmind_get_data(
            "TaiwanStockPrice", data_id=INDEX_FINMIND_ID.get(code, code),
            start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"), allow_empty=False,
        ).fillna(0)
        record_api_event("FinMindData", status=200, latency=time.perf_counter() - started)
    except Exception:
        record_api_event("FinMindData", status=500, latency=time.perf_counter() - started)
        raise
    frame = raw.rename(columns={"date": "Date", "open": "Open", "max": "High", "min": "Low",
                                "close": "Close", "Trading_Volume": "Volume"})
    missing = {"Date", "Open", "High", "Low", "Close"} - set(frame.columns)
    if missing:
        raise ToolDataError("指數資料欄位不足：" + str(sorted(missing)))
    if "Volume" not in frame.columns:
        frame["Volume"] = 0.0
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (frame.dropna(subset=["Date", "Open", "High", "Low", "Close"])
             .drop_duplicates(subset=["Date"], keep="last").sort_values("Date").set_index("Date"))
    if frame.empty:
        raise ToolDataError(INDEX_CODES.get(code, code) + " 沒有可用的日K資料")
    return frame


def _load_index_bundle(code: str) -> Dict[str, Any]:
    kf = core()

    def build() -> Dict[str, Any]:
        frame = _cached("index_daily_" + code, TTL_PRICE_SECONDS, lambda: _fetch_index_daily(code))
        closed = kf.calculate_indicators(frame)
        closed["Close_prev"] = closed["Close"].shift(1)
        return {"df": closed, "closed_df": closed, "market": "index", "intraday": {},
                "daily_source": "指數日K收盤資料"}

    return _cached("price_" + code, TTL_PRICE_SECONDS, build)


FUTURES_ID = os.getenv("DISCORD_AI_FUTURES_ID", "TX").strip() or "TX"


def get_futures_positions(days: int = 10) -> Dict[str, Any]:
    """三大法人台指期未平倉（FinMind）。只陳述口數與變化，不作多空判斷。"""
    kf = core()
    end = taipei_now()
    start = end - timedelta(days=max(5, int(days or 10)) * 2 + 10)
    started = time.perf_counter()
    try:
        raw = kf._finmind_get_data(
            "TaiwanFuturesInstitutionalInvestors", data_id=FUTURES_ID,
            start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"), allow_empty=False)
        record_api_event("FinMindData", status=200, latency=time.perf_counter() - started)
    except Exception as exc:
        record_api_event("FinMindData", status=500, latency=time.perf_counter() - started)
        raise ToolDataError("台指期未平倉資料暫時無法取得") from exc
    if raw is None or raw.empty:
        raise ToolDataError("台指期未平倉沒有資料")
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    dates = sorted({d for d in frame["date"]})
    if not dates:
        raise ToolDataError("台指期未平倉沒有有效日期")
    latest, previous = dates[-1], (dates[-2] if len(dates) > 1 else None)

    def rows_for(day) -> Dict[str, Dict[str, float]]:
        out = {}
        subset = frame[frame["date"] == day]
        for _, row in subset.iterrows():
            name = str(row.get("institutional_investors") or "")
            long_oi = float(row.get("long_open_interest_balance_volume") or 0.0)
            short_oi = float(row.get("short_open_interest_balance_volume") or 0.0)
            out[name] = {"long": long_oi, "short": short_oi, "net": long_oi - short_oi}
        return out

    today_rows = rows_for(latest)
    prev_rows = rows_for(previous) if previous is not None else {}
    investors = []
    for name in ("外資", "外資及陸資", "投信", "自營商"):
        info = today_rows.get(name)
        if not info:
            continue
        before = (prev_rows.get(name) or {}).get("net")
        investors.append({
            "investor": name,
            "long_oi": int(info["long"]), "short_oi": int(info["short"]), "net_oi": int(info["net"]),
            "net_text": ("淨多 " if info["net"] >= 0 else "淨空 ") + format(int(abs(info["net"])), ","),
            "change_vs_prev": int(info["net"] - before) if before is not None else None,
        })
    return {
        "futures_id": FUTURES_ID, "data_date": latest.strftime("%Y/%m/%d"),
        "previous_date": previous.strftime("%Y/%m/%d") if previous is not None else "",
        "investors": investors,
        "definition_note": "未平倉口數含現貨避險部位，不能單獨當作多空訊號；僅供參考",
    }


def _load_price_bundle(stock_code: str) -> Dict[str, Any]:
    """日K：FinMind 為主、失敗改富果日K；盤中再接上富果即時報價。指標沿用 calculate_indicators。"""
    kf = core()
    if str(stock_code or "").strip().upper() in INDEX_CODES:
        return _load_index_bundle(str(stock_code).strip().upper())
    code = kf._normalize_stock_name_code_key(stock_code)

    def daily() -> Tuple[pd.DataFrame, str, str]:
        # 先讀 Persistent Volume / 本機 SQLite 的最近 70 日。只要資料仍夠新，就不再打 FinMind。
        persistent = local_market_cache.load_bars(code, limit=max(70, local_market_cache.KEEP_DAYS))
        if persistent and persistent.get("count", 0) >= 69:
            gap = (pd.Timestamp(taipei_now().date()) - persistent["last_date"]).days
            if gap <= 5:
                return persistent["df"], str(persistent.get("market") or ""), "本地69日歷史快取"
        try:
            started = time.perf_counter()
            stock_df, market, _ = kf.fetch_stock_data_yf(code, period=PRICE_FETCH_PERIOD)
            record_api_event("FinMindData", status=200, latency=time.perf_counter()-started)
            if stock_df is not None and not stock_df.empty:
                local_market_cache.save_bars(code, stock_df, market=str(market or ""), source="FinMind", confirmed=True)
                return stock_df, str(market or ""), "FinMind 日K"
            error: Exception = ToolDataError(f"{code} FinMind 沒有股價資料")
        except Exception as exc:  # FinMind 失敗時才以富果歷史日K備援；正常盤中不拿富果做歷史預抓。
            record_api_event("FinMindData", status=500)
            error = exc
        if not (FUGLE_API_KEYS or FUGLE_API_KEY):
            raise ToolDataError(f"{code} 沒有股價資料：{type(error).__name__}: {error}")
        print(f"⚠️ {code} FinMind 股價失敗，才改用富果歷史日K備援：{type(error).__name__}: {error}", flush=True)
        days = int(re.search(r"\d+", PRICE_FETCH_PERIOD).group(0)) if re.search(r"\d+", PRICE_FETCH_PERIOD) else 180
        frame = fetch_fugle_daily(code, days)
        local_market_cache.save_bars(code, frame, market="", source="Fugle-history-fallback", confirmed=True)
        return frame, "", "富果日K備援"

    def build() -> Dict[str, Any]:
        daily_df, market, daily_source = _cached(f"price_daily_{code}", TTL_PRICE_SECONDS, daily)
        merged, intraday = _append_intraday_bar(code, daily_df, market)
        closed = kf.calculate_indicators(daily_df)
        closed["Close_prev"] = closed["Close"].shift(1)
        if not intraday:
            return {"df": closed, "closed_df": closed, "market": market, "intraday": {}, "daily_source": daily_source}
        df = kf.calculate_indicators(merged)
        df["Close_prev"] = df["Close"].shift(1)
        # 均量只用已收盤的日子：盤中累計量不算進 MV5／MV20（早盤會把均量拉低、量比失真）。
        for column in ("MV5", "MV20"):
            if column in df.columns and len(df) > 1:
                df.iloc[-1, df.columns.get_loc(column)] = df[column].iloc[-2]
        return {"df": df, "closed_df": closed, "market": market, "intraday": intraday, "daily_source": daily_source}

    ttl = TTL_INTRADAY_SECONDS if INTRADAY_ENABLE and intraday_session_now() else TTL_PRICE_SECONDS
    # 背景掃描的結果不含盤中 K 棒，另存一個 key，免得使用者接著問同一檔時
    # 拿到背景剛寫進去、沒有即時價的版本。
    prefix = "price_closed_" if current_api_priority() == "background" else "price_"
    return _cached(f"{prefix}{code}", ttl, build)


def closed_frame(bundle: Dict[str, Any]) -> pd.DataFrame:
    """評分、訊號、大量區用：只含已收盤 K 棒（盤中暫定 K 棒另外當作「盤中觀察」）。"""
    return bundle.get("closed_df") if bundle.get("closed_df") is not None else bundle["df"]


def intraday_observation(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """盤中狀態和前一日收盤確認狀態的差異：只描述「盤中暫時」，一律標註尚待收盤確認。"""
    info = bundle.get("intraday") or {}
    if not info:
        return {}
    df, closed = bundle["df"], closed_frame(bundle)
    now, last = df.iloc[-1], closed.iloc[-1]
    price, closed_close = _num(now.get("Close")), _num(last.get("Close"))
    changes: List[str] = []
    positions: Dict[str, str] = {}
    for n in (5, 10, 20, 60):
        key = f"MA{n}"
        live_ma, closed_ma = _num(now.get(key)), _num(last.get(key))
        if price is None or live_ma is None:
            continue
        live_pos = _ma_position(price, live_ma)["position"]
        positions[key] = live_pos
        closed_pos = _ma_position(closed_close, closed_ma)["position"] if closed_close is not None and closed_ma is not None else "資料不足"
        if live_pos != closed_pos and live_pos in ("站上", "跌破"):
            changes.append(f"盤中暫時{live_pos} {key}（前一日收盤為{closed_pos}），尚待收盤確認")
    upper, lower = _num(now.get("BB_UPPER")), _num(now.get("BB_LOWER"))
    if price is not None and upper is not None and price > upper:
        changes.append("盤中暫時站上布林上軌，尚待收盤確認")
    elif price is not None and lower is not None and price < lower:
        changes.append("盤中暫時跌破布林下軌，尚待收盤確認")
    return {
        "status": "盤中暫時" if info.get("is_live") else "今日收盤（日K尚未更新）",
        "time": info.get("time", ""),
        "price": price,
        "change_pct": _pct(price, closed_close),
        "ma_positions": positions,
        "changes": changes,
        "cumulative_volume_lots": info.get("cumulative_volume_lots"),
        "volume_suspect": bool(info.get("volume_suspect")),
        "volume_note": "盤中累計量不和日均量比較，也不據此判斷量縮或量增" if info.get("is_live") else "",
    }


def _stock_identity(stock_code: str) -> Tuple[str, str]:
    raw = str(stock_code or "").strip().upper()
    if raw in INDEX_CODES:
        return raw, INDEX_CODES[raw]
    code = core()._normalize_stock_name_code_key(stock_code)
    if not code:
        raise ToolDataError("股票代號不可為空")
    try:
        name = resolve_stock_name(code)
    except ToolDataError:
        name = ""
    return code, name


PRICE_SOURCE_NOTE = "日K收盤資料（非盤中即時）"


# ============================================================
# Tool 1：股價概況
# ============================================================

def _volume_trend(df: pd.DataFrame) -> Dict[str, Any]:
    """量能趨勢（只用已收盤日）：近 5 日均量相對前 20 日、連續放大天數、20 日內最大量。

    讓 AI 不必自己從 K 線猜「量有沒有放大」；這些都是 Python 算好的事實。
    """
    try:
        volume = pd.to_numeric(df["Volume"], errors="coerce").dropna()
    except (KeyError, TypeError):
        return {}
    if len(volume) < 25:
        return {}
    recent5 = float(volume.iloc[-5:].mean())
    prior20 = float(volume.iloc[-25:-5].mean())
    today = float(volume.iloc[-1])
    rising = 0
    for i in range(len(volume) - 1, 0, -1):
        if volume.iloc[i] > volume.iloc[i - 1]:
            rising += 1
        else:
            break
    ratio = recent5 / prior20 if prior20 else None
    if ratio is None:
        level = "資料不足"
    elif ratio >= 2.0:
        level = "明顯放大"
    elif ratio >= 1.3:
        level = "溫和放大"
    elif ratio <= 0.7:
        level = "明顯萎縮"
    else:
        level = "持平"
    return {
        "today_lots": _num(today / 1000, 0),
        "avg5_lots": _num(recent5 / 1000, 0),
        "prior20_avg_lots": _num(prior20 / 1000, 0),
        "recent5_vs_prior20": _num(ratio, 2),
        "level": level,
        "rising_streak_days": rising,
        "is_20d_high_volume": bool(today >= float(volume.iloc[-20:].max())),
    }


def get_stock_overview(stock_code: str) -> Dict[str, Any]:
    """最新一根日K的收盤、漲跌幅、成交量（張）、5／20日均量與量比。"""
    code, name = _stock_identity(stock_code)
    bundle = _load_price_bundle(code)
    df = bundle["df"]
    latest = df.iloc[-1]
    close = _num(latest.get("Close"))
    prev_close = _num(latest.get("Close_prev"))
    change = _num(close - prev_close) if close is not None and prev_close is not None else None
    volume = _num(latest.get("Volume"), 4)
    mv5 = _num(latest.get("MV5"), 4)
    mv20 = _num(latest.get("MV20"), 4)
    live = bool((bundle.get("intraday") or {}).get("is_live"))
    suspect = bool((bundle.get("intraday") or {}).get("volume_suspect"))
    if live or suspect:
        # 盤中累計量不和全日均量比較；可疑量能也不計算量比。
        ratio5 = ratio20 = None
    else:
        ratio5 = _num(volume / mv5) if volume is not None and mv5 else None
        ratio20 = _num(volume / mv20) if volume is not None and mv20 else None
    return {
        "stock_code": code,
        "stock_name": name,
        "market": bundle["market"],
        "data_date": _fmt_date(df.index[-1]),
        "data_source": price_source_note(bundle),
        "intraday": bundle.get("intraday") or {},
        "open": _num(latest.get("Open")),
        "high": _num(latest.get("High")),
        "low": _num(latest.get("Low")),
        "close": close,
        "prev_close": prev_close,
        "change": change,
        "change_pct": _pct(close, prev_close),
        "volume_lots": _num(volume / 1000, 0) if volume is not None else None,
        "mv5_lots": _num(mv5 / 1000, 0) if mv5 is not None else None,
        "mv20_lots": _num(mv20 / 1000, 0) if mv20 is not None else None,
        "volume_ratio_vs_mv5": ratio5,
        "volume_ratio_vs_mv20": ratio20,
        "volume_trend": _volume_trend(closed_frame(bundle)) if not suspect else {},
        "volume_status": "盤中累計量（尚未收盤）" if live else ("量能資料可疑，不判讀" if suspect else "收盤成交量"),
        "volume_unit_note": "成交量與均量單位為張；量比＝當日量 ÷ 均量；盤中不計算量比",
    }


# ============================================================
# Tool 2：技術指標
# ============================================================

def _ma_position(close: Optional[float], ma: Optional[float]) -> Dict[str, Any]:
    if close is None or ma is None:
        return {"value": ma, "position": "資料不足", "distance_pct": None}
    if close > ma:
        position = "站上"
    elif close < ma:
        position = "跌破"
    else:
        position = "持平"
    return {"value": ma, "position": position, "distance_pct": _pct(close, ma)}


def _recent_ma_cross(df: pd.DataFrame, column: str, lookback: int = 3) -> Dict[str, Any]:
    """最近 lookback 根 K 棒內是否真正穿越均線（前一日在另一側才算）。"""
    result = {"just_broke_above": False, "just_broke_below": False, "cross_date": ""}
    if column not in df.columns or len(df) < 2:
        return result
    for idx in range(len(df) - 1, max(0, len(df) - lookback) - 1, -1):
        if idx < 1:
            break
        prev, curr = df.iloc[idx - 1], df.iloc[idx]
        if any(pd.isna(v) for v in (prev["Close"], prev[column], curr["Close"], curr[column])):
            continue
        if prev["Close"] <= prev[column] and curr["Close"] > curr[column]:
            result.update(just_broke_above=True, cross_date=_fmt_date(df.index[idx]))
            break
        if prev["Close"] >= prev[column] and curr["Close"] < curr[column]:
            result.update(just_broke_below=True, cross_date=_fmt_date(df.index[idx]))
            break
    return result


def _ma_alignment(values: Dict[int, Optional[float]]) -> str:
    ma5, ma10, ma20, ma60 = (values.get(n) for n in (5, 10, 20, 60))
    if None in (ma5, ma10, ma20, ma60):
        return "資料不足"
    if ma5 > ma10 > ma20 > ma60:
        return "多頭排列"
    if ma5 < ma10 < ma20 < ma60:
        return "空頭排列"
    return "均線糾結（未形成多空排列）"


def _bollinger_position(close: Optional[float], upper: Optional[float], mid: Optional[float], lower: Optional[float]) -> str:
    if None in (close, upper, mid, lower):
        return "資料不足"
    if close > upper:
        return "突破上軌"
    if close >= mid:
        return "位於中軌與上軌之間"
    if close >= lower:
        return "位於下軌與中軌之間"
    return "跌破下軌"


MA_DEDUCTION_DAYS = _env_int("DISCORD_AI_MA_DEDUCTION_DAYS", 5)


def analyze_ma_deduction(df: pd.DataFrame, periods: Sequence[int] = (5, 10, 20, 60), days: int = MA_DEDUCTION_DAYS) -> Dict[str, Any]:
    """均線扣抵：明天的 MA_n 會移除 n 個交易日前那根收盤（扣抵價）。

    明日均線變化 ＝（明日收盤 − 扣抵價）÷ n，所以收盤高於扣抵價均線才會上揚。
    推算假設「收盤維持今天價位」，逐日扣掉未來 days 根扣抵價，找出均線是否會轉向；只是條件推算，不是預測。
    """
    closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if closes.empty:
        return {}
    close = float(closes.iloc[-1])
    result: Dict[str, Any] = {}
    for n in periods:
        if len(closes) < n + 1:
            continue
        start = len(closes) - n
        ma = float(closes.iloc[start:].mean())
        flat = abs(ma) * 0.0002
        k = max(1, min(days, n))
        deductions = [float(v) for v in closes.iloc[start:start + k]]
        dates = [_fmt_date(d) for d in closes.index[start:start + k]]
        changes = [(close - d) / n for d in deductions]

        def direction(change: float) -> str:
            return "上揚" if change > flat else "下彎" if change < -flat else "走平"

        now = direction((close - float(closes.iloc[start - 1])) / n)
        turn, turn_day = "", None
        for day, change in enumerate(changes, 1):
            future = direction(change)
            if future != "走平" and future != now:
                turn, turn_day = ("轉下彎" if future == "下彎" else "轉上揚"), day
                break
        projected = ma + sum(changes)
        high, low = max(deductions), min(deductions)
        above_close = ma > close
        if turn == "轉下彎":
            signal = (f"MA{n} 目前{now}，但未來 {k} 日扣抵價最高 {high:,.2f} 高於現價；收盤若維持 {close:,.2f}，"
                      f"{turn_phrase(turn, turn_day)}" + ("，且均線在股價上方，下彎後容易形成壓力" if above_close else ""))
        elif turn == "轉上揚":
            signal = (f"MA{n} 目前{now}，未來 {k} 日扣抵價最低 {low:,.2f} 低於現價；收盤若維持 {close:,.2f}，"
                      f"{turn_phrase(turn, turn_day)}")
        elif now == "上揚":
            signal = f"MA{n} 上揚，未來 {k} 日扣抵價（{low:,.2f}～{high:,.2f}）不高於現價，收盤維持不變時仍續揚"
        elif now == "下彎":
            signal = (f"MA{n} 下彎，未來 {k} 日扣抵價（{low:,.2f}～{high:,.2f}）不低於現價，收盤維持不變時仍續彎"
                      + ("，均線在股價上方，壓力未解除" if above_close else ""))
        else:
            signal = f"MA{n} 走平，未來 {k} 日扣抵價 {low:,.2f}～{high:,.2f}"
        result[f"MA{n}"] = {
            "value": _num(ma),
            "direction_now": now,
            "next_deduction_price": _num(deductions[0]),
            "next_deduction_date": dates[0],
            "tomorrow_close_needed_to_rise": _num(deductions[0]),
            "deduction_days": k,
            "deduction_prices": [_num(v) for v in deductions],
            "deduction_high": _num(high),
            "deduction_low": _num(low),
            "projected_value_if_close_unchanged": _num(projected),
            "turn": turn,
            "turn_day": turn_day,
            "turn_text": turn_phrase(turn, turn_day),
            "ma_above_close": above_close,
            "signal": signal,
        }
    return result


def get_technical_analysis(stock_code: str) -> Dict[str, Any]:
    """重用既有指標；若已取得今日即時 K，型態相關數值採當下暫定 K 重新計算。

    盤中分數的用途是「現在此刻的技術結構」，不是正式收盤訊號；回傳欄位會明確標示盤中/盤後暫定。
    """
    kf = core()
    code, name = _stock_identity(stock_code)
    bundle = _load_price_bundle(code)
    intraday = bundle.get("intraday") or {}
    use_live = bool(LIVE_PATTERN_SCORE and intraday)
    df = bundle["df"] if use_live else closed_frame(bundle)
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest
    close = _num(latest.get("Close"))
    ma_values = {n: _num(latest.get(f"MA{n}")) for n in (5, 10, 20, 60)}
    osc, prev_osc = _num(latest.get("OSC"), 4), _num(prev.get("OSC"), 4)
    if osc is None or prev_osc is None:
        osc_trend = "資料不足"
    elif abs(osc) > abs(prev_osc):
        osc_trend = "柱狀體擴大"
    elif abs(osc) < abs(prev_osc):
        osc_trend = "柱狀體縮小"
    else:
        osc_trend = "柱狀體持平"
    k9, d9 = _num(latest.get("K9")), _num(latest.get("D9"))

    def safe_signal(function: Callable[[pd.DataFrame], str]) -> str:
        try:
            return str(function(df) or "")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"⚠️ Discord AI 技術訊號計算略過：{function.__name__}｜{exc}")
            return ""

    return {
        "stock_code": code,
        "stock_name": name,
        "data_date": _fmt_date(df.index[-1]),
        "data_source": price_source_note(bundle),
        "intraday": intraday,
        "signal_status": (f"盤後暫定（{_fmt_date(df.index[-1])}；將再次確認收盤）" if intraday.get("post_close_provisional")
                          else f"盤中暫定（{_fmt_date(df.index[-1])} {intraday.get('time','')}；最終以收盤為準）" if use_live and intraday.get("is_live")
                          else f"收盤確認（{_fmt_date(df.index[-1])} 收盤）"),
        "intraday_observation": intraday_observation(bundle),
        "close": close,
        "moving_averages": {f"MA{n}": _ma_position(close, v) for n, v in ma_values.items()},
        "ma_alignment": _ma_alignment(ma_values),
        "ma20_cross_recent_3_days": _recent_ma_cross(df, "MA20"),
        "ma_deduction": analyze_ma_deduction(df),
        "kd": {
            "K9": k9,
            "D9": d9,
            "J9": _num(latest.get("J9")),
            "k_above_d": (k9 > d9) if k9 is not None and d9 is not None else None,
            "signals": safe_signal(kf.get_kd_signals),
        },
        "macd": {
            "DIF": _num(latest.get("DIF"), 3),
            "MACD": _num(latest.get("MACD"), 3),
            "OSC": _num(osc, 3),
            "osc_trend": osc_trend,
            "signals": safe_signal(kf.get_macd_signals),
        },
        "bollinger": analyze_bollinger(df),
        "ma_kline_signals": safe_signal(kf.get_ma_kline_signals),
        "indicator_definition": "MA＝收盤簡單均線；KD＝9日RSV；MACD＝12/26/9；布林＝20日±2倍標準差（與週報相同）",
    }


# ============================================================
# Tool 3：大量區（與週報圖卡同一套演算法）
# ============================================================

def _event_flags(recent_event: str) -> Dict[str, bool]:
    """把既有型態文字轉成布林旗標；文字本身由 _build_price_volume_pattern_payload 產生。"""
    text = str(recent_event or "")
    return {
        "recent_breakout": "突破最大量區" in text,
        "recent_breakdown": "跌破最大量區" in text,
        "retest_after_breakout": "回踩" in text,
        "rebound_test_after_breakdown": "反彈測試" in text,
    }


def get_volume_profile(stock_code: str) -> Dict[str, Any]:
    """重用 build_weekly_context、_calculate_weighted_volume_profile_stats、_build_price_volume_pattern_payload。

    週報圖卡的價量累積圖使用 ctx["plot_df"]（近 CHART_LOOKBACK 根日K）與 n_bins=40，
    這裡傳入完全相同的資料與參數，因此最大量區／第二大量區價格與圖卡一致。
    """
    kf = core()
    code, name = _stock_identity(stock_code)
    bundle = _load_price_bundle(code)
    # 大量區的「成交量分布」仍只用已收盤日，避免早盤累計量扭曲成本區；
    # 但股價相對量區的位置改用今日盤中/盤後暫定價格，讓型態分數能即時變動。
    df = closed_frame(bundle)
    ctx = kf.build_weekly_context(df, pd.DataFrame(), kf.WEEK_TRADING_DAYS)
    plot_df = ctx["plot_df"]
    stats = kf._calculate_weighted_volume_profile_stats(plot_df, n_bins=40)
    if not stats:
        raise ToolDataError(f"{code} 近期K線資料不足，無法計算大量區")
    pattern = kf._build_price_volume_pattern_payload(ctx, n_bins=40)

    bins, centers, profile = stats["bins"], stats["centers"], stats["profile"]
    max_idx, second_idx = int(stats["max_idx"]), int(stats["second_idx"])
    closed_close = float(stats["work"]["Close"].iloc[-1])
    live_df = bundle.get("df")
    close = float(live_df["Close"].iloc[-1]) if LIVE_PATTERN_SCORE and bundle.get("intraday") and live_df is not None and not live_df.empty else closed_close

    def zone(idx: int, label: str, color: str) -> Dict[str, Any]:
        if idx < 0 or idx >= len(centers):
            return {}
        low, high, center = float(bins[idx]), float(bins[idx + 1]), float(centers[idx])
        return {
            "label": label,
            "chart_color": color,
            "price_low": _num(low),
            "price_high": _num(high),
            "center_price": _num(center),
            "close_relation": kf._price_zone_relation(close, low, high),
            "close_distance_from_center_pct": _pct(close, center),
            "relative_strength_pct": _num(profile[idx] / profile[max_idx] * 100) if profile[max_idx] > 0 else None,
        }

    def current_position() -> str:
        zones = []
        for idx in (max_idx, second_idx):
            if 0 <= idx < len(centers):
                zones.append((float(bins[idx]), float(bins[idx + 1])))
        if len(zones) < 2:
            return str(pattern.get("latest_position_relative_to_two_zones", "") or "")
        if close > max(z[1] for z in zones):
            return "收盤在兩大量區之上" if not bundle.get("intraday") else "現價在兩大量區之上"
        if close < min(z[0] for z in zones):
            return "收盤在兩大量區之下" if not bundle.get("intraday") else "現價在兩大量區之下"
        return "收盤在兩大量區之間" if not bundle.get("intraday") else "現價在兩大量區之間"

    recent_event = str(pattern.get("recent_maximum_zone_pattern", "") or "")
    return {
        "stock_code": code,
        "stock_name": name,
        "data_date": _fmt_date(bundle["df"].index[-1] if LIVE_PATTERN_SCORE and bundle.get("intraday") else plot_df.index[-1]),
        "analysis_window": {
            "start": _fmt_date(plot_df.index[0]),
            "end": _fmt_date(plot_df.index[-1]),
            "trading_days": int(len(plot_df)),
            "price_bins": 40,
        },
        "close": _num(close),
        "maximum_volume_zone": zone(max_idx, "最大量區", "紅色"),
        "second_volume_zone": zone(second_idx, "第二大量區", "橘色"),
        "position_vs_two_zones": current_position(),
        "recent_maximum_zone_event": recent_event,
        **_event_flags(recent_event),
        "pattern_label": pattern.get("current_pattern_label", ""),
        "pattern_evidence": pattern.get("pattern_evidence", ""),
        "crossing_volume_character": pattern.get("crossing_volume_character", ""),
        "recent_swing_structure": pattern.get("recent_swing_structure", ""),
        "weekly_price_volume_relationship": pattern.get("weekly_price_volume_relationship", ""),
        "system_interpretation": pattern.get("neutral_zone_interpretation", ""),
        "algorithm_note": "大量區分布仍用近70根已收盤日K；盤中只更新現價相對量區位置，避免未收盤累計量扭曲成本區",
    }


# ============================================================
# Tool 4：個股權證分點（與週報 TOP5 同口徑）
# ============================================================

def _warrant_flow_frame(
    stock_code: str,
    days: int,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """抓取指定股票最近 N 個交易日的權證分點事件，並套用週報 TOP5 的排除規則。"""
    kf = core()
    code, name = _stock_identity(stock_code)
    days = max(1, min(int(days or 5), _env_int("WARRANT_FLOW_MAX_DAYS", 60)))

    def build() -> Dict[str, Any]:
        today = kf.get_taipei_today_ts()
        trading_dates = [
            pd.Timestamp(d).normalize()
            for d in kf._get_official_trading_dates(today - pd.Timedelta(days=days * 2 + 20), today)
        ]
        trading_dates = [d for d in trading_dates if d <= today]
        if not trading_dates:
            raise ToolDataError("交易日曆暫時無法取得")
        # 多抓一個交易日：盤中或盤後資料尚未發布時，仍能湊滿 N 個有資料的交易日。
        request_dates = trading_dates[-(days + 1):]
        started = time.perf_counter()
        try:
            events = kf.fetch_warrant_events_full_market(
                code, name, request_dates[0], request_dates[-1], cancel_event
            )
            record_api_event("MoneyDJFlow", status=200, latency=time.perf_counter() - started)
        except Exception:
            record_api_event("MoneyDJFlow", status=500, latency=time.perf_counter() - started)
            raise
        if events is None or events.empty:
            return {"flow": pd.DataFrame(), "window_dates": [], "latest_date": None}
        e = events.copy()
        e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.normalize()
        e = e.dropna(subset=["Date"])
        latest = e["Date"].max()
        window_dates = [d for d in trading_dates if d <= latest][-days:]
        e = e[e["Date"].isin(window_dates)].copy()
        e["branch"] = e["branch"].map(kf.normalize_branch_name)
        if kf.HEDGE_FILTER_ENABLE:
            e, _ = kf.filter_out_market_maker_hedges(
                e, hedge_threshold=kf.HEDGE_FILTER_THRESHOLD, do_filter=True
            )
        flow = kf.filter_warrant_flow_excluding_issuer_market_makers(e)
        return {"flow": flow, "window_dates": window_dates, "latest_date": latest}

    bundle = _cached(f"warrant_{code}_{days}", TTL_WARRANT_SECONDS, build)
    return {"code": code, "name": name, "days": days, **bundle}


def _abcde_events_by_branch(stock_code: str, date_start: str, date_end: str) -> Tuple[Dict[str, List[Dict[str, str]]], str]:
    """回測 Sheet 的 ABCDE 事件（只涵蓋回測追蹤的分點）；失敗時回傳原因。"""
    try:
        result = query_google_sheet(
            "股票ABCDE查詢資料",
            stock_code=stock_code,
            date_start=date_start,
            date_end=date_end,
            columns=["事件代碼", "分點", "事件日", "目前狀態", "結果", "單日累積買進金額"],
            limit=SHEET_QUERY_MAX_ROWS,
        )
    except ToolDataError as exc:
        return {}, str(exc)
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in result["rows"]:
        key = _normalize_branch(row.get("分點", ""))
        grouped.setdefault(key, []).append({
            "event": row.get("事件代碼", ""),
            "event_date": row.get("事件日", ""),
            "buy_amount": _money_text(core()._parse_number_like_value(row.get("單日累積買進金額", ""))),
            "status": row.get("目前狀態", ""),
            "result": row.get("結果", ""),
        })
    return grouped, ""


def _branch_rows(table: pd.DataFrame, side: str, flow: pd.DataFrame) -> List[Dict[str, Any]]:
    active_days = (
        flow.groupby("branch")["Date"].nunique().to_dict()
        if flow is not None and not flow.empty and "branch" in flow.columns
        else {}
    )
    rows = []
    for rank, (_, r) in enumerate(table.iterrows(), 1):
        branch = str(r.get("branch", "") or "")
        rows.append({
            "rank": rank,
            "side": side,
            "branch": branch,
            "net_amount": _num(r.get("net_amount"), 0),
            "net_amount_text": _money_text(r.get("net_amount")),
            "main_warrant_code": str(r.get("max_warrant_code", "") or ""),
            "main_warrant_name": str(r.get("max_warrant_name", "") or ""),
            "main_warrant_net_text": _money_text(r.get("max_warrant_amount")),
            "active_days": int(active_days.get(branch, 0)),
        })
    return rows


def get_warrant_branch(
    stock_code: str,
    days: int = 5,
    topn: int = WARRANT_TOPN,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """指定股票最近 N 日權證分點買賣超排行（預設 5 日，可 5／10／20）。

    重用 fetch_warrant_events_full_market（MoneyDJ 主來源，單一股票短區間），
    再套用 filter_warrant_flow_excluding_issuer_market_makers 與 top_branch_tables，
    與週報 TOP5 使用相同的排除規則。
    """
    kf = core()
    bundle = _warrant_flow_frame(stock_code, days, cancel_event)
    flow, window_dates = bundle["flow"], bundle["window_dates"]
    base = {
        "stock_code": bundle["code"],
        "stock_name": bundle["name"],
        "requested_trading_days": bundle["days"],
        "data_source": "MoneyDJ 權證分點（已排除發行商造市端、疑似對手單與券商總公司型分點，與週報TOP5同口徑）",
    }
    if flow is None or flow.empty or not window_dates:
        return {**base, "available": False, "reason": "查詢區間內沒有權證分點成交資料"}

    buy_top, sell_top = kf.top_branch_tables(flow, topn=max(1, int(topn)))
    period_start, period_end = _fmt_date(window_dates[0]), _fmt_date(window_dates[-1])
    abcde, abcde_error = _abcde_events_by_branch(bundle["code"], period_start, period_end)
    buy_rows = _branch_rows(buy_top, "買超", flow)
    sell_rows = _branch_rows(sell_top, "賣超", flow)
    for row in buy_rows + sell_rows:
        events = abcde.get(row["branch"])
        if events:
            row["abcde_events"] = events[:5]
    return {
        **base,
        "available": True,
        "period_start": period_start,
        "period_end": period_end,
        "actual_trading_days": len(window_dates),
        "latest_data_date": _fmt_date(bundle["latest_date"]),
        "total_buy_amount_text": _money_text(flow["buy_amount"].sum()),
        "total_sell_amount_text": _money_text(flow["sell_amount"].sum()),
        "total_net_amount": _num(flow["net_amount"].sum(), 0),
        "total_net_amount_text": _money_text(flow["net_amount"].sum()),
        "branch_count": int(flow["branch"].nunique()),
        "top_buy_branches": buy_rows,
        "top_sell_branches": sell_rows,
        "abcde_note": (
            "ABCDE 事件只涵蓋回測追蹤的分點（事件資料暫時無法取得）"
            if abcde_error
            else "ABCDE 事件只涵蓋回測追蹤的分點"
        ),
    }


# ============================================================
# Tool 5：分點歷史績效（勝率統計）
# ============================================================

_BRANCH_PERF_LOCK = threading.Lock()
_BRANCH_PERF_LOADED_AT = 0.0


def _read_branch_perf_df() -> pd.DataFrame:
    """重用 read_gsheet_branch_perf_df；超過 TTL 才強制重讀，避免長時間運行的 Bot 永遠用舊資料。"""
    global _BRANCH_PERF_LOADED_AT
    kf = core()
    with _BRANCH_PERF_LOCK:
        stale = time.time() - _BRANCH_PERF_LOADED_AT > TTL_BRANCH_PERF_SECONDS
        df = kf.read_gsheet_branch_perf_df(force_refresh=stale)
        _mark_cache(not stale)
        if df is None or df.empty:
            raise SheetUnavailableError("勝率統計讀取失敗或沒有資料")
        if stale:
            _BRANCH_PERF_LOADED_AT = time.time()
        return df


def _event_row_payload(row: pd.Series) -> Dict[str, Any]:
    kf = core()
    count = kf._parse_number_like_value(row.get("事件數", ""))
    return {
        "event_type": row.get("事件類型", ""),
        "event_count": _num(count, 0),
        "closed_count": _num(kf._parse_number_like_value(row.get("已出清筆數", "")), 0),
        "open_count": _num(kf._parse_number_like_value(row.get("未出清筆數", "")), 0),
        "win_count": _num(kf._parse_number_like_value(row.get("勝筆數", "")), 0),
        "loss_count": _num(kf._parse_number_like_value(row.get("敗筆數", "")), 0),
        "win_rate": row.get("勝率", ""),
        "avg_holding_days": row.get("平均持有天數", ""),
        "avg_return": row.get("平均報酬%", ""),
        "weighted_return": row.get("加權報酬%", ""),
        "total_buy_amount_text": _money_text(kf._parse_number_like_value(row.get("總買進金額", ""))),
        "small_sample": bool(count is not None and np.isfinite(count) and count < SMALL_SAMPLE_EVENTS),
    }


def get_branch_performance(branch_name: str, event_type: str = "") -> Dict[str, Any]:
    """分點歷史勝率、加權報酬率、事件數與平均持有天數；可指定 A~E 事件類型。"""
    kf = core()
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {
            "found": False,
            "query": branch_name,
            "candidates": candidates,
            "reason": "勝率統計找不到這個分點" if not candidates else "分點名稱有多個可能，請指定",
        }
    perf = _read_branch_perf_df()
    hit = perf[perf["branch"] == canonical]
    overall: Dict[str, Any] = {}
    if not hit.empty:
        r = hit.iloc[0]
        count = _num(r.get("event_count"), 0)
        overall = {
            "win_rate_pct": _num(r.get("win_rate")),
            "weighted_return_pct": _num(r.get("weighted_return")),
            "event_count": count,
            "avg_holding_days": _num(r.get("avg_holding_days"), 1),
            "holding_style": kf._describe_branch_holding_style(r.get("avg_holding_days")),
            "small_sample": bool(count is not None and count < SMALL_SAMPLE_EVENTS),
        }

    by_event: List[Dict[str, Any]] = []
    sheet_updated_at = ""
    try:
        table = read_sheet_table("勝率統計")
        sheet_updated_at = table["sheet_updated_at"]
        df = table["df"]
        if not df.empty and "分點" in df.columns:
            rows = df[df["分點"].map(kf.normalize_branch_name) == canonical]
            letter = _event_letter(event_type)
            for _, row in rows.iterrows():
                if letter and _event_letter(row.get("事件類型", "")) != letter:
                    continue
                by_event.append(_event_row_payload(row))
    except ToolDataError as exc:
        print(f"⚠️ Discord AI 勝率統計事件明細略過：{exc}")

    if not overall and not by_event:
        return {"found": False, "query": branch_name, "branch": canonical, "reason": "勝率統計沒有這個分點的資料"}
    return {
        "found": True,
        "branch": canonical,
        "requested_event_type": EVENT_TYPE_LABELS.get(_event_letter(event_type), ""),
        "overall_all_events": overall,
        "by_event_type": by_event,
        "data_source": "回測勝率統計",
        "sheet_updated_at": sheet_updated_at,
        "definition_note": "勝率含實際出清與持有滿60日後估值的事件；歷史勝率不代表未來結果",
        "small_sample_threshold": SMALL_SAMPLE_EVENTS,
    }


# ============================================================
# 高勝率分點 × 近期加碼（Python join）
# ============================================================

def get_high_winrate_branches_buying(
    stock_code: str,
    days: int = 5,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """近期買超分點與勝率統計 join，依勝率排序後只留下重要結果。"""
    kf = core()
    warrant = get_warrant_branch(stock_code, days=days, topn=WARRANT_JOIN_TOPN, cancel_event=cancel_event)
    if not warrant.get("available"):
        return {**warrant, "joined_branches": []}
    try:
        perf = _read_branch_perf_df()
    except ToolDataError as exc:
        return {
            **{k: warrant[k] for k in ("stock_code", "stock_name", "period_start", "period_end", "latest_data_date")},
            "available": False,
            "reason": f"近期分點資料已取得，但{exc}",
            "top_buy_branches": warrant["top_buy_branches"][:WARRANT_TOPN],
        }
    perf_map = {str(r["branch"]): r for _, r in perf.iterrows()}
    joined, unmatched = [], []
    for row in warrant["top_buy_branches"]:
        p = perf_map.get(row["branch"])
        if p is None:
            unmatched.append(row["branch"])
            continue
        count = _num(p.get("event_count"), 0)
        win_rate = _num(p.get("win_rate"))
        joined.append({
            **{k: row[k] for k in ("rank", "branch", "net_amount_text", "main_warrant_name", "active_days")},
            **({"abcde_events": row["abcde_events"]} if row.get("abcde_events") else {}),
            "historical_win_rate_pct": win_rate,
            "historical_weighted_return_pct": _num(p.get("weighted_return")),
            "historical_event_count": count,
            "avg_holding_days": _num(p.get("avg_holding_days"), 1),
            "holding_style": kf._describe_branch_holding_style(p.get("avg_holding_days")),
            "is_high_win_rate": bool(win_rate is not None and win_rate >= HIGH_WIN_RATE_PCT),
            "small_sample": bool(count is not None and count < SMALL_SAMPLE_EVENTS),
        })
    joined.sort(key=lambda x: (-(x["historical_win_rate_pct"] or 0), x["rank"]))
    return {
        "stock_code": warrant["stock_code"],
        "stock_name": warrant["stock_name"],
        "available": True,
        "period_start": warrant["period_start"],
        "period_end": warrant["period_end"],
        "actual_trading_days": warrant["actual_trading_days"],
        "latest_data_date": warrant["latest_data_date"],
        "high_win_rate_threshold_pct": HIGH_WIN_RATE_PCT,
        "small_sample_threshold": SMALL_SAMPLE_EVENTS,
        "buy_branches_checked": len(warrant["top_buy_branches"]),
        "joined_branches": joined[:10],
        "high_win_rate_count": sum(1 for x in joined if x["is_high_win_rate"]),
        "branches_without_history": unmatched[:10],
        "data_source": "近期買超：權證分點（週報同口徑）；歷史勝率：回測勝率統計",
        "definition_note": "「加碼」指查詢區間內權證淨買超；高勝率門檻為歷史勝率 ≥ 門檻值，不代表這次一定成功",
    }


# ============================================================
# 分點近期買賣（回測 Sheet：近10日分點買賣明細）
# ============================================================

def _latest_snapshot(df: pd.DataFrame, date_column: str = "統計日期") -> Tuple[pd.DataFrame, str]:
    """同一張表若累積多個統計日，只取最新一期。"""
    if df.empty or date_column not in df.columns:
        return df, ""
    dates = df[date_column].map(_parse_sheet_date)
    latest = dates.dropna().max() if dates.notna().any() else None
    if latest is None:
        return df, ""
    return df[dates == latest], _fmt_date(latest)


def get_branch_recent_trades(branch_name: str, stock_code: str = "", limit: int = 10) -> Dict[str, Any]:
    """指定分點近10日在各標的的權證買賣（讀回測 Sheet，不重抓全市場）。"""
    kf = core()
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    table = read_sheet_table("快取_近10日分點買賣明細")
    df = table["df"]
    if df.empty or "分點" not in df.columns:
        raise SheetUnavailableError("近10日分點買賣明細沒有資料")
    rows = df[(df["分點"].map(kf.normalize_branch_name) == canonical)
              | (df.get("分點名稱", pd.Series([""] * len(df))).map(kf.normalize_branch_name) == canonical)]
    rows, snapshot_date = _latest_snapshot(rows)
    if stock_code:
        code = kf._normalize_stock_name_code_key(stock_code)
        rows = rows[rows["標的股"].map(kf._normalize_stock_name_code_key) == code]
    period = rows["統計期間"].iloc[0] if not rows.empty and "統計期間" in rows.columns else ""

    def amount(row: pd.Series, column: str) -> float:
        value = kf._parse_number_like_value(row.get(column, ""))
        return float(value) if np.isfinite(value) else 0.0

    def item(row: pd.Series) -> Dict[str, Any]:
        return {
            "stock_code": row.get("標的股", ""),
            "stock_name": row.get("標的名稱", ""),
            "direction": row.get("買賣方向", ""),
            "net_amount_text": _money_text(row["_net"]),
            "buy_amount_text": _money_text(amount(row, "近10日買進金額")),
            "sell_amount_text": _money_text(amount(row, "近10日賣出金額")),
            "warrant_count": row.get("涉及權證檔數", ""),
            "stock_10d_change": row.get("標的10日漲跌幅%", ""),
            "warrant_return": row.get("用於勝率報酬%", ""),
            "judgement": row.get("判定", ""),
        }

    # 「近10日淨賣超金額」只是淨買超金額取負號，買賣方向一律以淨買超金額正負判斷。
    buy_items: List[Dict[str, Any]] = []
    sell_items: List[Dict[str, Any]] = []
    if not rows.empty:
        rows = rows.assign(_net=rows.apply(lambda r: amount(r, "近10日淨買超金額"), axis=1))
        buy_items = [item(r) for _, r in rows[rows["_net"] > 0].sort_values("_net", ascending=False).head(limit).iterrows()]
        sell_items = [item(r) for _, r in rows[rows["_net"] < 0].sort_values("_net").head(limit).iterrows()]

    branch_stats: Dict[str, Any] = {}
    if not rows.empty:
        first = rows.iloc[0]
        branch_stats = {
            "win_rate_10d": first.get("分點近10日勝率", ""),
            "win_count_10d": first.get("分點近10日勝筆數", ""),
            "loss_count_10d": first.get("分點近10日敗筆數", ""),
            "weighted_return_10d": first.get("分點近10日加權平均報酬%", ""),
        }
    recent_events: List[Dict[str, str]] = []
    try:
        start = (_taipei_now() - timedelta(days=30)).strftime("%Y/%m/%d")
        events = query_google_sheet(
            "股票ABCDE查詢資料",
            branch=canonical,
            stock_code=stock_code,
            date_start=start,
            columns=["事件代碼", "標的股", "標的名稱", "事件日", "目前狀態", "結果", "單日累積買進金額"],
            limit=10,
        )
        recent_events = events["rows"]
    except ToolDataError as exc:
        print(f"⚠️ Discord AI 分點近期 ABCDE 事件略過：{exc}")
    return {
        "found": True,
        "branch": canonical,
        "snapshot_date": snapshot_date,
        "period": period,
        "stock_filter": stock_code,
        "net_buy_stocks": buy_items,
        "net_sell_stocks": sell_items,
        "branch_10d_stats": branch_stats,
        "recent_abcde_events_30d": recent_events,
        "data_source": "近10日分點買賣明細與 ABCDE 事件（只涵蓋回測追蹤的分點）",
        "definition_note": "近10日勝率為10日窗口定義，與勝率統計的歷史勝率不同",
        "sheet_updated_at": table["sheet_updated_at"],
    }


# ============================================================
# 分點 × 股票歷史事件
# ============================================================

def get_branch_stock_history(branch_name: str, stock_code: str, limit: int = 15) -> Dict[str, Any]:
    """分點在指定股票的 ABCDE 歷史事件，並依回測「結果」欄統計勝敗筆數。"""
    kf = core()
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    code, name = _stock_identity(stock_code)
    table = read_sheet_table("股票ABCDE查詢資料")
    df = table["df"]
    if df.empty:
        raise SheetUnavailableError("股票ABCDE查詢資料沒有資料")
    rows = df[(df["分點"].map(kf.normalize_branch_name) == canonical)
              & (df["標的股"].map(kf._normalize_stock_name_code_key) == code)].copy()
    rows["_date"] = rows["事件日"].map(_parse_sheet_date)
    rows = rows.sort_values("_date", ascending=False, na_position="last")

    results = rows["結果"].astype(str).str.strip() if "結果" in rows.columns else pd.Series(dtype=str)
    closed_returns = [
        kf._parse_percent_like_value(v, ratio_if_small=False)
        for v in rows.get("出清獲利%", pd.Series(dtype=str))
    ]
    closed_returns = [v for v in closed_returns if np.isfinite(v)]
    holding = [kf._parse_number_like_value(v) for v in rows.get("持有天數", pd.Series(dtype=str))]
    holding = [v for v in holding if np.isfinite(v)]
    columns = ["事件代碼", "事件日", "目前狀態", "結果", "單日累積買進金額", "出清日", "出清獲利%", "持有天數"]
    events = []
    for _, row in rows.head(limit).iterrows():
        record = _compact_row(row, [c for c in columns if c in rows.columns])
        if "單日累積買進金額" in record:
            record["單日累積買進金額"] = _money_text(kf._parse_number_like_value(record["單日累積買進金額"]))
        events.append(record)
    total = int(len(rows))
    return {
        "found": True,
        "branch": canonical,
        "stock_code": code,
        "stock_name": name,
        "event_total": total,
        "result_counts_from_sheet": {
            "勝": int((results == "勝").sum()),
            "敗": int((results == "敗").sum()),
            "平手": int((results == "平手").sum()),
            "未出清": int((results == "未出清").sum()),
        },
        "closed_return_samples": len(closed_returns),
        "closed_return_avg_pct": _num(float(np.mean(closed_returns))) if closed_returns else None,
        "avg_holding_days": _num(float(np.mean(holding)), 1) if holding else None,
        "small_sample": total < SMALL_SAMPLE_EVENTS,
        "latest_events": events,
        "data_source": "回測 ABCDE 事件",
        "definition_note": "勝敗筆數直接採用回測結果欄；出清獲利平均為已出清事件的簡單平均，非加權報酬",
        "sheet_updated_at": table["sheet_updated_at"],
    }


# ============================================================
# 近10日分點勝率排行
# ============================================================

def get_branch_winrate_rank(limit: int = 10) -> Dict[str, Any]:
    """回測 Sheet 的近10日分點勝率排行（最新一期）。"""
    table = read_sheet_table("快取_近10日分點勝率排行")
    df, snapshot_date = _latest_snapshot(table["df"])
    if df.empty:
        raise SheetUnavailableError("近10日分點勝率排行沒有資料")
    columns = ["排名", "分點", "近10日勝率", "近10日勝筆數", "近10日敗筆數", "近10日統計筆數", "近10日加權平均報酬%", "主要交易標的"]
    rank = pd.to_numeric(df.get("排名", pd.Series(dtype=str)), errors="coerce")
    df = df.assign(_rank=rank).sort_values("_rank", na_position="last")
    rows = [_compact_row(r, [c for c in columns if c in df.columns]) for _, r in df.head(max(1, min(limit, 30))).iterrows()]
    return {
        "snapshot_date": snapshot_date,
        "period": df["統計期間"].iloc[0] if "統計期間" in df.columns else "",
        "rows": rows,
        "data_source": "近10日分點勝率排行（只涵蓋回測追蹤的分點）",
        "definition_note": "近10日勝率為10日窗口定義，樣本少時波動大",
        "sheet_updated_at": table["sheet_updated_at"],
    }


# ============================================================
# Tool 6：近期新聞
# ============================================================

def _news_date(value: Any) -> str:
    try:
        ts = pd.Timestamp(pd.to_datetime(str(value), utc=True))
    except (TypeError, ValueError):
        return str(value or "")
    if pd.isna(ts):
        return str(value or "")
    return ts.tz_convert("Asia/Taipei").strftime("%Y/%m/%d")


def _cnyes_html_to_text(value: str) -> str:
    """鉅亨文章內容是一小段 HTML：只對這段做簡單去標籤（不掃整個網頁，避免長時間佔住 GIL 卡住 Discord 心跳）。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h\d>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t　\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


_RSC_PUSH_MARK = 'self.__next_f.push([1,'
_RSC_TEXT_ROW_RE = re.compile(r'(?:^|\n)([0-9a-z]+):T([0-9a-f]+),')


def _cnyes_rsc_article_html(page: str) -> str:
    """鉅亨文章頁（Next.js App Router）：內文在 self.__next_f.push 的文字列（id:T長度,內容）。

    只用 str.find＋json raw_decode 解出字串（C 實作，單頁約數毫秒），不對整頁跑複雜正規式。
    同頁另有 JSON-LD 文字列（以 { 開頭），略過；取剩下最長的一段。長度單位是 UTF-8 位元組。
    """
    decoder = json.JSONDecoder()
    chunks, pos = [], 0
    while True:
        index = page.find(_RSC_PUSH_MARK, pos)
        if index < 0:
            break
        try:
            value, pos = decoder.raw_decode(page, index + len(_RSC_PUSH_MARK))
        except ValueError:
            break
        if isinstance(value, str):
            chunks.append(value)
    joined = "".join(chunks)
    best = ""
    for match in _RSC_TEXT_ROW_RE.finditer(joined):
        size = int(match.group(2), 16)
        segment = joined[match.end(): match.end() + size].encode("utf-8")[:size].decode("utf-8", errors="ignore")
        if segment.lstrip().startswith("{"):
            continue
        if len(segment) > len(best):
            best = segment
    return html.unescape(best)


def fetch_cnyes_article_text(news_id: str) -> str:
    """鉅亨文章內文；找不到時退回頁面 meta description。"""
    kf = core()
    response = kf.get_thread_session().get(
        CNYES_NEWS_PAGE_URL.format(news_id),
        headers={"User-Agent": kf.HDR["User-Agent"], "Accept": "text/html"},
        timeout=(4, NEWS_ARTICLE_TIMEOUT),
    )
    response.raise_for_status()
    page = response.text
    content = _cnyes_html_to_text(_cnyes_rsc_article_html(page))
    match = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', page[:30000])
    description = html.unescape(match.group(1)).strip() if match else ""
    # 內文是表格時（例如 FactSet 預估），導言只在 description，補在前面。
    if description and description[:20] not in content:
        content = (description + "\n" + content).strip()
    return content


_FILING_META_RE = re.compile(r"^(第\d+款|公司代號|公司名稱|發言日期|發言時間|發言人|發言人職稱|發言人電話|符合條款)")
_FILING_EMPTY_VALUES = {"不適用", "無", "NA", "N/A", "none", "None"}


def _compact_filing_text(text: str) -> str:
    """公司重大訊息公告：拿掉制式抬頭（公司代號、發言人…），並把「1.欄位名稱:值」逐項分段，
    值是空的或「不適用／無」的整項拿掉，讓金額、交易對象等重點不被截掉，也不留下沒有內容的欄位名稱。"""
    head: List[str] = []
    items: List[List[str]] = []
    for raw in str(text or "").split("\n"):
        line = raw.strip()
        if not line or _FILING_META_RE.match(line):
            continue
        if re.match(r"^\d{1,2}[.．、]", line):
            items.append([line])
        elif items:
            items[-1].append(line)
        else:
            head.append(line)
    kept = list(head)
    for item in items:
        block = "\n".join(item)
        parts = re.split(r"[:：]", block, maxsplit=1)
        value = parts[1].strip(" \n。") if len(parts) == 2 else ""
        if len(parts) == 2 and (not value or value in _FILING_EMPTY_VALUES):
            continue
        kept.append(block)
    return "\n".join(kept)


def fetch_cnyes_news(code: str, name: str) -> List[Dict[str, Any]]:
    """鉅亨網新聞搜尋 API（用股票代號查），只留標題／標籤明確提到本公司、且在回看天數內的新聞，前幾篇抓內文。"""
    kf = core()
    response = kf.get_thread_session().get(
        CNYES_SEARCH_URL,
        params={"q": code, "limit": 30, "page": 1},
        headers={"User-Agent": kf.HDR["User-Agent"], "Accept": "application/json"},
        timeout=(4, 8),
    )
    response.raise_for_status()
    raw_items = ((response.json() or {}).get("data") or {}).get("items") or []
    cutoff = time.time() - NEWS_LOOKBACK_DAYS * 86400
    picked = []
    for item in raw_items:
        # 搜尋 API 的標題會帶 <mark> 高亮標籤，先去掉。
        title = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("title", "") or ""))).strip()
        item["title"] = title
        published = float(item.get("publishAt") or 0)
        tags = " ".join(str(t) for t in item.get("keywordForTag") or [])
        related = name in title or code in title or name in tags or f"TWS:{code}:STOCK" in json.dumps(item, ensure_ascii=False)
        if not title or not item.get("newsId") or published < cutoff or not related:
            continue
        picked.append(item)
        if len(picked) >= NEWS_CNYES_MAX_ITEMS:
            break
    # 內文並行抓取；整批有總時限，逾時的直接放棄（不等待），不拖住回答。
    from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
    pool = ThreadPoolExecutor(max_workers=max(1, NEWS_CNYES_BODY_ITEMS), thread_name_prefix="ace-cnyes")
    futures = {str(item["newsId"]): pool.submit(fetch_cnyes_article_text, str(item["newsId"]))
               for item in picked[:NEWS_CNYES_BODY_ITEMS]}
    futures_wait(list(futures.values()), timeout=NEWS_BODY_BATCH_TIMEOUT)
    pool.shutdown(wait=False, cancel_futures=True)
    articles = []
    for item in picked:
        news_id = str(item["newsId"])
        future = futures.get(news_id)
        content = ""
        if future is not None and future.done():
            try:
                content = future.result()
            except Exception as exc:  # 單篇失敗只少內文
                print(f"⚠️ 鉅亨新聞內文略過：{news_id}｜{type(exc).__name__}: {exc}", flush=True)
        summary = _cnyes_html_to_text(item.get("summary") or "")
        if content and re.match(r"^[^:：]{1,12}[:：]本公司", item["title"]):
            content = _compact_filing_text(content)
        articles.append({
            "date": datetime.fromtimestamp(float(item["publishAt"]), TAIPEI_TZ).strftime("%Y/%m/%d"),
            "title": item["title"].strip(),
            "source": "鉅亨網",
            "summary": _truncate_sentences(summary, NEWS_SUMMARY_MAX_CHARS),
            "content": _truncate_sentences(content, NEWS_CONTENT_MAX_CHARS) if content else "",
            "content_source": "鉅亨網原文" if content else "僅標題",
            "url": CNYES_NEWS_PAGE_URL.format(news_id),
            "event_key": f"cnyes_{news_id}",
        })
    return articles


def get_recent_news(stock_code: str, limit: int = NEWS_MAX_ITEMS) -> Dict[str, Any]:
    """近期公司新聞：鉅亨網（有內文）優先，再補既有六來源新聞標題；不抓一般網站原文（曾卡住 Discord 心跳）。"""
    kf = core()
    code, name = _stock_identity(stock_code)
    if not name:
        raise ToolDataError(f"查不到 {code} 的公司名稱，無法安全比對新聞")

    def build() -> Dict[str, Any]:
        cached_points = kf._load_gsheet_news_points_cache_for_display(code, name, allow_stale=False)
        cnyes: List[Dict[str, Any]] = []
        if NEWS_CNYES_ENABLE:
            try:
                cnyes = fetch_cnyes_news(code, name)
            except Exception as exc:  # 鉅亨失敗時仍有六來源標題
                print(f"⚠️ {code} 鉅亨新聞略過：{type(exc).__name__}: {exc}", flush=True)
        others = []
        try:
            articles = kf.fetch_multi_source_news_articles(code, name, max_items=kf.NEWS_GOOGLE_MAX_ITEMS)
            articles = kf._dedupe_news_articles_by_event(list(articles or []), code, name, log_label="Discord AI 新聞")
        except Exception as exc:
            print(f"⚠️ {code} 六來源新聞略過：{type(exc).__name__}: {exc}", flush=True)
            articles = []
        seen = [a["title"][:14] for a in cnyes]
        for article in articles:
            title = kf._clean_news_title(article.get("title", ""))
            description = kf._normalize_news_text(article.get("description", ""))
            if not title or kf._is_price_only_news_without_fundamentals(f"{title} {description}"):
                continue
            if any(prefix and prefix in title for prefix in seen):
                continue
            seen.append(title[:14])
            # RSS 摘要常常只是「標題＋媒體名」，跟標題重複時不當內容。
            detail = "" if not description or description.startswith(title[:14]) else description
            others.append({
                "date": _news_date(article.get("published", "")),
                "title": title,
                "source": str(article.get("source", "") or ""),
                "summary": _truncate_sentences(detail, NEWS_SUMMARY_MAX_CHARS),
                "content": _truncate_sentences(detail, NEWS_CONTENT_MAX_CHARS),
                "content_source": "RSS 摘要" if detail else "僅標題",
                "url": str(article.get("url", "") or ""),
                "event_key": kf._news_article_event_key(article, code, name),
            })
        with_body = [a for a in cnyes if a["content"]]
        items = (with_body + [a for a in cnyes if not a["content"]] + others)[: max(1, int(limit))]
        print(f"📰 {code} 新聞｜鉅亨 {len(cnyes)} 篇（有內文 {len(with_body)} 篇）｜六來源標題 {len(others)} 篇｜採用 {len(items)} 篇", flush=True)
        return {
            "stock_code": code,
            "stock_name": name,
            "source_type": "鉅亨網新聞搜尋（含內文）＋既有六來源新聞標題（公司主體過濾＋事件去重）",
            "summary_points": list(cached_points or [])[:3],
            "summary_points_source": "週報當日新聞重點快取（已通過既有 grounding 驗證）" if cached_points else "",
            "articles": items,
        }

    data = _cached(f"news_{code}", TTL_NEWS_SECONDS, build)
    return {**data, "available": bool(data["summary_points"] or data["articles"])}


# ============================================================
# A/B/C/D/E 事件別績效（本週精選／AI 專用；週報仍只讀全部合併列）
# ============================================================
#
# 事件定義的唯一來源是回測程式 warrant_backtest_moneydj.py：
#   AMOUNT_CLASS_SPECS + classify_amount_class() + build_amount_class_events()
#   事件單位＝同分點 × 同標的 × 同一天；依當日權證買進金額合計分級，
#   A 100～160萬、B 160～250萬、C 250～500萬、D 500～1000萬、E ≥1000萬（區間互斥）。
# 這裡不重新判斷事件，只讀回測輸出的 A_～E_ 工作表與勝率統計。

EVENT_CODES: Tuple[str, ...] = ("A", "B", "C", "D", "E")
EVENT_PRIOR_STRENGTH = _env_float("DISCORD_AI_EVENT_PRIOR_STRENGTH", 20.0)
WINRATE_FLAT_BAND_PCT = max(_env_float("WINRATE_FLAT_BAND_PCT", 0.0), 0.0)
_SCOPE_PRIORITY = {"全分點": 0, "未標記舊資料": 1, "": 1, "精選五分點": 2}


def _pct_value(value: Any) -> Optional[float]:
    """「68.75%」「+3.04%」→ 68.75 / 3.04；「-」回傳 None。Sheet 的百分比欄一律帶 %，不做小數放大。"""
    number = core()._parse_percent_like_value(_clean_cell(value), ratio_if_small=False)
    return float(number) if np.isfinite(number) else None


def _count_value(value: Any) -> Optional[float]:
    number = core()._parse_number_like_value(_clean_cell(value))
    return float(number) if np.isfinite(number) else None


def _find_column(columns: List[str], exact: str = "", prefix: str = "", suffix: str = "") -> str:
    if exact and exact in columns:
        return exact
    for column in columns:
        if prefix and suffix and column.startswith(prefix) and column.endswith(suffix):
            return column
    return ""


_EVENT_PERF_REQUIRED_COLUMNS = ("分點", "事件類型", "事件數", "納入勝率筆數", "未納入勝率筆數", "勝筆數", "敗筆數", "勝率", "加權報酬%", "平均持有天數")


def read_branch_event_performance(prior_strength: float = EVENT_PRIOR_STRENGTH) -> Dict[str, Any]:
    """讀取「勝率統計」每個分點 × A～E 事件與全部合併列，並計算小樣本修正勝率。

    adjusted_win_rate 使用 Bayesian smoothing：
        (勝筆數 + 同事件全分點平均勝率 × prior_strength) / (納入勝率筆數 + prior_strength)
    - 勝筆數、納入勝率筆數直接取自回測輸出，不是估算。
    - 只有勝筆數欄缺少時，才用「勝率 × 納入勝率筆數」估算，並標記 win_count_estimated。
    - 未納入勝率（未出清且未滿估值天數）的事件不會被當成勝，另外保留 unresolved_count
      與 win_rate_if_unresolved_lost（把未納入事件全部視為敗的保守參考值）。
    """

    def build() -> Dict[str, Any]:
        kf = core()
        table = read_sheet_table("勝率統計")
        df = table["df"]
        columns = list(df.columns)
        missing = [c for c in _EVENT_PERF_REQUIRED_COLUMNS if c not in columns]
        if df.empty or "分點" in missing or "事件類型" in missing:
            raise SheetUnavailableError("勝率統計沒有可解析的分點／事件類型資料")
        forced_col = _find_column(columns, prefix="滿", suffix="估值筆數")
        revised_col = _find_column(columns, exact="修正勝率")

        branches: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # pooled 需保留 Sheet 內既有的複合事件 key（例如 A+C），不能只預建 A～E。
        pooled: Dict[str, List[float]] = {}
        for _, row in df.iterrows():
            event_label = _clean_cell(row.get("事件類型", ""))
            code = _event_key(event_label)
            branch = kf.normalize_branch_name(row.get("分點", ""))
            if not code or not branch:
                continue
            included = _count_value(row.get("納入勝率筆數", "")) or 0.0
            raw = _pct_value(row.get("勝率", ""))
            wins = _count_value(row.get("勝筆數", ""))
            estimated = False
            if wins is None and raw is not None:
                wins = round(raw / 100.0 * included)  # 估算：Sheet 缺勝筆數欄時才使用
                estimated = True
            record = {
                "event_type": event_label,
                "event_count": _count_value(row.get("事件數", "")) or 0.0,
                "closed_count": _count_value(row.get("已出清筆數", "")),
                "open_count": _count_value(row.get("未出清筆數", "")),
                "forced_valuation_count": _count_value(row.get(forced_col, "")) if forced_col else None,
                "included_count": included,
                "unresolved_count": _count_value(row.get("未納入勝率筆數", "")) or 0.0,
                "win_count": wins,
                "loss_count": _count_value(row.get("敗筆數", "")),
                "flat_count": _count_value(row.get("平手筆數", "")),
                "raw_win_rate": raw,
                "win_count_estimated": estimated,
                "avg_return": _pct_value(row.get("平均報酬%", "")),
                "weighted_return": _pct_value(row.get("加權報酬%", "")),
                "avg_holding_days": _count_value(row.get("平均持有天數", "")),
                "total_buy_amount": _count_value(row.get("總買進金額", "")),
                "sheet_revised_win_rate": _pct_value(row.get(revised_col, "")) if revised_col else None,
            }
            branches.setdefault(branch, {})[code] = record
            if wins is not None and included > 0:
                bucket = pooled.setdefault(code, [0.0, 0.0])
                bucket[0] += wins
                bucket[1] += included

        priors = {
            code: round(w / n * 100.0, 2) if n > 0 else 50.0
            for code, (w, n) in pooled.items()
        }
        k = max(0.0, float(prior_strength))
        for records in branches.values():
            for code, rec in records.items():
                prior = priors.get(code, 50.0)
                wins, included = rec["win_count"], rec["included_count"]
                if wins is None:
                    rec["adjusted_win_rate"] = None
                else:
                    rec["adjusted_win_rate"] = round((wins + prior / 100.0 * k) / (included + k) * 100.0, 2) if included + k > 0 else None
                denominator = included + rec["unresolved_count"]
                rec["win_rate_if_unresolved_lost"] = round(wins / denominator * 100.0, 2) if wins is not None and denominator > 0 else None
                rec["unresolved_ratio"] = round(rec["unresolved_count"] / rec["event_count"], 4) if rec["event_count"] else None
                rec["prior_win_rate"] = prior
        return {
            "branches": branches,
            "priors": priors,
            "prior_strength": k,
            "missing_columns": missing + ([] if forced_col else ["滿N日估值筆數"]),
            "sheet_updated_at": table["sheet_updated_at"],
        }

    return _cached(f"branch_event_perf_{prior_strength:g}", TTL_BRANCH_PERF_SECONDS, build)


def _event_perf_payload(branch: str, code: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "branch": branch,
        "event_type": code,
        "event_label": rec.get("event_type", ""),
        "raw_win_rate": rec.get("raw_win_rate"),
        "adjusted_win_rate": rec.get("adjusted_win_rate"),
        "prior_win_rate": rec.get("prior_win_rate"),
        "event_count": _num(rec.get("event_count"), 0),
        "included_count": _num(rec.get("included_count"), 0),
        "unresolved_count": _num(rec.get("unresolved_count"), 0),
        "win_count": _num(rec.get("win_count"), 0),
        "loss_count": _num(rec.get("loss_count"), 0),
        "win_count_estimated": rec.get("win_count_estimated", False),
        "win_rate_if_unresolved_lost": rec.get("win_rate_if_unresolved_lost"),
        "weighted_return": rec.get("weighted_return"),
        "avg_holding_days": rec.get("avg_holding_days"),
        "small_sample": bool((rec.get("included_count") or 0) < SMALL_SAMPLE_EVENTS),
    }


def get_branch_event_performance(branch_name: str, event_type: str = "") -> Dict[str, Any]:
    """分點 × 指定事件（或 A～E 全部）的原始勝率、修正勝率、樣本數與加權報酬。"""
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    data = read_branch_event_performance()
    records = data["branches"].get(canonical)
    if not records:
        return {"found": False, "branch": canonical, "reason": "勝率統計沒有這個分點"}
    meta = {
        "adjusted_method": f"Bayesian smoothing：(勝筆數 + 同事件全分點平均勝率 × {data['prior_strength']:g}) ÷ (納入勝率筆數 + {data['prior_strength']:g})",
        "definition_note": "勝率分母＝已出清＋持有滿估值天數事件；未納入勝率事件另列 unresolved_count，不計入勝",
        "missing_columns": data["missing_columns"],
        "sheet_updated_at": data["sheet_updated_at"],
    }
    event_key = _event_key(event_type)
    overall = records.get("overall")
    if event_key and event_key != "overall":
        rec = records.get(event_key)
        if not rec:
            return {"found": False, "branch": canonical, "event_type": event_key, "reason": f"勝率統計沒有 {event_key} 事件資料"}
        return {
            "found": True,
            **_event_perf_payload(canonical, event_key, rec),
            "overall_background": _event_perf_payload(canonical, "overall", overall) if overall else {},
            **meta,
        }
    event_keys = [k for k in records.keys() if k != "overall"]
    return {
        "found": True,
        "branch": canonical,
        **{code: _event_perf_payload(canonical, code, records[code]) for code in event_keys},
        "overall": _event_perf_payload(canonical, "overall", overall) if overall else {},
        **meta,
    }


def load_abcde_event_rows() -> Dict[str, Any]:
    """讀取回測輸出的 A_～E_ 事件表，整理成單一 DataFrame（同事件跨資料範圍去重，全分點優先）。

    completed / unresolved 判定：
    - 出清獲利% 有值 → completed（實際出清）
    - 否則勝率結算報酬% 有值 → completed（持有滿估值天數的估值結果）
    - 兩者皆無 → unresolved，不算勝、也不刪除
    """

    def build() -> Dict[str, Any]:
        kf = core()
        frames, errors = [], []
        for code, title in AMOUNT_CLASS_SHEETS.items():
            try:
                df = read_sheet_table(title)["df"]
            except ToolDataError as exc:
                errors.append(f"{title}：{exc}")
                continue
            if df.empty:
                continue
            frame = pd.DataFrame({
                "scope": df.get("資料範圍", pd.Series([""] * len(df))).map(_clean_cell),
                "event_code": df.get("事件類型", pd.Series([code] * len(df))).map(lambda v, c=code: _event_letter(v) or c),
                "branch": df["分點"].map(kf.normalize_branch_name),
                "stock_code": df["標的股"].map(kf._normalize_stock_name_code_key),
                "event_date": df["事件日"].map(_parse_sheet_date),
                "buy_amount": df.get("單日累積買進金額", pd.Series([""] * len(df))).map(_count_value),
                "lots": df.get("買超張數", pd.Series([""] * len(df))).map(_count_value),
                "warrant_count": df.get("涵蓋權證數", pd.Series([""] * len(df))).map(_count_value),
                "warrant_list": df.get("權證清單", pd.Series([""] * len(df))).map(_clean_cell),
                "max_single_warrant": df.get("最大單筆權證", pd.Series([""] * len(df))).map(_clean_cell),
                "reduce_date": df.get("減碼日", pd.Series([""] * len(df))).map(_parse_sheet_date),
                "exit_date": df.get("出清日", pd.Series([""] * len(df))).map(_parse_sheet_date),
                "exit_return": df.get("出清獲利%", pd.Series([""] * len(df))).map(_pct_value),
                "settle_method": df.get("勝率結算方式", pd.Series([""] * len(df))).map(_clean_cell),
                "settle_return": df.get("勝率結算報酬%", pd.Series([""] * len(df))).map(_pct_value),
                "holding_days": df.get("持有天數", pd.Series([""] * len(df))).map(_count_value),
            })
            frames.append(frame)
        if not frames:
            raise SheetUnavailableError("A～E 事件表都無法讀取" + (f"（{'；'.join(errors)}）" if errors else ""))
        events = pd.concat(frames, ignore_index=True)
        events = events.dropna(subset=["event_date"])
        events = events[(events["branch"] != "") & (events["stock_code"] != "")]
        events["_scope_rank"] = events["scope"].map(lambda s: _SCOPE_PRIORITY.get(s, 1))
        events = (
            events.sort_values("_scope_rank")
            .drop_duplicates(subset=["branch", "stock_code", "event_date", "event_code"], keep="first")
            .drop(columns=["_scope_rank"])
        )

        def resolve(row: pd.Series) -> Tuple[str, Optional[float], str]:
            if row["exit_return"] is not None and np.isfinite(row["exit_return"]):
                return "completed", float(row["exit_return"]), "實際出清"
            if row["settle_return"] is not None and np.isfinite(row["settle_return"]):
                return "completed", float(row["settle_return"]), row["settle_method"] or "滿期估值"
            return "unresolved", None, ""

        if events.empty:
            raise SheetUnavailableError("A～E 事件表沒有有效事件列")
        resolved = [resolve(row) for _, row in events.iterrows()]
        events["resolution"] = [r[0] for r in resolved]
        events["result_return"] = [r[1] for r in resolved]
        events["result_basis"] = [r[2] for r in resolved]
        # 與回測 calc_result_tag 相同的平手區間；Railway 需設定與回測 workflow 相同的 WINRATE_FLAT_BAND_PCT。
        band = WINRATE_FLAT_BAND_PCT
        events["result"] = events["result_return"].map(
            lambda r: "未完成" if r is None or not np.isfinite(r) else ("勝" if r > band else "敗" if r < -band else "平手")
        )
        events["status"] = np.where(
            events["exit_date"].notna(), "已出清",
            np.where(events["reduce_date"].notna(), "已減碼未出清", "目前持有"),
        )
        events = events.sort_values("event_date").reset_index(drop=True)
        return {
            "events": events,
            "latest_event_date": events["event_date"].max() if not events.empty else None,
            "errors": errors,
        }

    return _cached("abcde_event_rows", TTL_SHEET_SECONDS, build)


def _recent_event_dates(latest: pd.Timestamp, trading_days: int, events: pd.DataFrame) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """以官方交易日曆回推 N 個交易日；日曆取不到時退回事件表實際出現過的日期。"""
    kf = core()
    try:
        dates = [
            pd.Timestamp(d).normalize()
            for d in kf._get_official_trading_dates(latest - pd.Timedelta(days=trading_days * 2 + 20), latest)
        ]
    except Exception as exc:  # 官方休市表失敗時仍可運作
        print(f"⚠️ Discord AI 交易日曆讀取失敗，改用事件表日期：{type(exc).__name__}: {exc}")
        dates = sorted(set(events["event_date"].dropna()))
    dates = [d for d in dates if d <= latest]
    window = dates[-max(1, int(trading_days)):] if dates else [latest]
    return window[0], window[-1]


def _trading_days_between(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """含頭含尾的官方交易日數；日曆失敗時退回工作日數。"""
    try:
        return len(core()._get_official_trading_dates(start, end))
    except Exception as exc:  # 官方休市表失敗時仍回傳近似值
        print(f"⚠️ Discord AI 交易日數改用工作日估算：{type(exc).__name__}: {exc}")
        return len(pd.bdate_range(start, end))


def _event_record(row: pd.Series) -> Dict[str, Any]:
    return {
        "event": row["event_code"],
        "event_date": _fmt_date(row["event_date"]),
        "buy_amount": _num(row["buy_amount"], 0),
        "buy_amount_text": _money_text(row["buy_amount"]),
        "warrant_count": _num(row["warrant_count"], 0),
        "max_single_warrant": row["max_single_warrant"],
        "status": row["status"],
        "resolution": row["resolution"],
        "result": row["result"],
        "result_return_pct": _num(row["result_return"]),
    }


def detect_current_branch_events(stock_code: str, branch_name: str, lookback_trading_days: int = 5) -> Dict[str, Any]:
    """「這一次」分點在該股票觸發了哪些 A～E 事件（直接讀回測官方輸出，不重判）。"""
    kf = core()
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    code = kf._normalize_stock_name_code_key(stock_code)
    bundle = load_abcde_event_rows()
    events, latest = bundle["events"], bundle["latest_event_date"]
    if latest is None:
        return {"found": False, "branch": canonical, "stock_code": code, "reason": "事件表沒有資料"}
    start, end = _recent_event_dates(latest, lookback_trading_days, events)
    hit = events[
        (events["branch"] == canonical) & (events["stock_code"] == code)
        & (events["event_date"] >= start) & (events["event_date"] <= end)
    ]
    triggered = [c for c in EVENT_CODES if c in set(hit["event_code"])]
    return {
        "found": True,
        "stock_code": code,
        "branch": canonical,
        "window_start": _fmt_date(start),
        "window_end": _fmt_date(end),
        "triggered_events": triggered,
        "events": [_event_record(r) for _, r in hit.iterrows()],
        "event_buy_amount_total": _num(hit["buy_amount"].sum(), 0),
        "overlap_note": "A～E 以單日買進金額分級、區間互斥；同一天只會有一個等級，多個等級代表不同交易日的多筆事件",
        "data_latest_event_date": _fmt_date(latest),
    }


def _outcome_summary(rows: pd.DataFrame) -> Dict[str, Any]:
    """completed／unresolved 分開統計，不把未完成算成功、也不刪除。"""
    completed = rows[rows["resolution"] == "completed"]
    return {
        "cases": int(len(rows)),
        "completed_cases": int(len(completed)),
        "wins": int((completed["result"] == "勝").sum()),
        "losses": int((completed["result"] == "敗").sum()),
        "flats": int((completed["result"] == "平手").sum()),
        "unresolved_cases": int((rows["resolution"] == "unresolved").sum()),
    }


def _outcome_sentence(summary: Dict[str, Any], label: str) -> str:
    if not summary["cases"]:
        return f"{label}沒有案例。"
    text = f"{label}{summary['cases']}筆案例中，{summary['completed_cases']}筆已有結果，其中{summary['wins']}勝{summary['losses']}敗"
    if summary["flats"]:
        text += f"{summary['flats']}平手"
    if summary["unresolved_cases"]:
        text += f"；另{summary['unresolved_cases']}筆仍未完成"
    return text + "。"


def _live_same_stock_flow(stock_code: str, branch: str) -> Dict[str, Any]:
    """用 MoneyDJ 近20交易日分點流水（週報同口徑）整理同分點 × 同股票的逐日操作。"""
    bundle = _warrant_flow_frame(stock_code, 20)
    flow = bundle["flow"]
    dates = list(bundle["window_dates"])
    if flow is None or flow.empty or not dates:
        return {"available": False, "reason": "近20日沒有權證分點流水"}
    sub = flow[flow["branch"] == branch]
    if sub.empty:
        return {"available": True, "has_trades": False, "period_start": _fmt_date(dates[0]), "period_end": _fmt_date(dates[-1])}
    daily = sub.groupby("Date")["net_amount"].sum().reindex(dates, fill_value=0.0)

    def net_last(n: int) -> float:
        return float(daily.tail(n).sum())

    buy_days = [d for d, v in daily.items() if v > 0]
    sell_days = [d for d, v in daily.items() if v < 0]
    signs = [1 if v > 0 else -1 for v in daily.values if v != 0]
    flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    last5 = daily.tail(5)
    by_warrant = sub.groupby(["warrant_code", "warrant_name"])["net_amount"].sum().sort_values(ascending=False)
    rotation_days = []
    for day, g in sub.groupby("Date"):
        per_warrant = g.groupby("warrant_code")["net_amount"].sum()
        if (per_warrant >= 500_000).any() and (per_warrant <= -500_000).any():
            rotation_days.append(_fmt_date(day))
    return {
        "available": True,
        "has_trades": True,
        "period_start": _fmt_date(dates[0]),
        "period_end": _fmt_date(dates[-1]),
        "net_buy_5d": _num(net_last(5), 0),
        "net_buy_10d": _num(net_last(10), 0),
        "net_buy_20d": _num(net_last(20), 0),
        "net_buy_5d_text": _money_text(net_last(5)),
        "net_buy_10d_text": _money_text(net_last(10)),
        "net_buy_20d_text": _money_text(net_last(20)),
        "first_buy_date_20d": _fmt_date(buy_days[0]) if buy_days else "",
        "latest_add_date": _fmt_date(buy_days[-1]) if buy_days else "",
        "latest_sell_date": _fmt_date(sell_days[-1]) if sell_days else "",
        "buy_days_last5": int((last5 > 0).sum()),
        "sell_days_last5": int((last5 < 0).sum()),
        "continuous_buying": bool((last5 > 0).sum() >= 3 and (last5 < 0).sum() == 0),
        "reducing_recently": bool(net_last(5) < 0 or (last5 < 0).sum() >= 2),
        "direction_flips_20d": int(flips),
        "direction_choppy": bool(flips >= 3),
        "warrant_count_20d": int(sub["warrant_code"].nunique()),
        "main_warrants": [
            {"warrant": f"{wc} {wn}".strip(), "net_amount_text": _money_text(v)}
            for (wc, wn), v in by_warrant.head(3).items()
        ],
        "rotation_evidence_days": rotation_days,
        "rotation_note": (
            "同日有一檔權證淨賣≥50萬且另一檔淨買≥50萬，可能是換倉，仍需人工確認"
            if rotation_days else "近20日沒有同日一賣一買的換倉證據（資料不足以判定換倉時不下結論）"
        ),
    }


def get_branch_recent_behavior(
    branch_name: str,
    stock_code: str = "",
    lookback_days: int = 30,
    recent_case_count: int = 10,
    include_live_flow: Optional[bool] = None,
) -> Dict[str, Any]:
    """分點近期操作習性（Recent Behavior Context，不是長期統計證據）。

    A. 同分點 × 同股票：本輪 ABCDE 事件、減碼／出清紀錄，以及 MoneyDJ 近 5／10／20 日淨買超。
    B. 同分點近期所有股票：主要買超與賣超標的、事件分布、頻率、平均布局金額、持有風格、
       最近 N 筆案例的 completed／unresolved。
    """
    kf = core()
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    bundle = load_abcde_event_rows()
    events, latest = bundle["events"], bundle["latest_event_date"]
    if latest is None:
        raise SheetUnavailableError("A～E 事件表沒有資料")
    start, end = _recent_event_dates(latest, lookback_days, events)
    branch_events = events[events["branch"] == canonical]
    window = branch_events[(branch_events["event_date"] >= start) & (branch_events["event_date"] <= end)]

    sells = pd.DataFrame()
    try:
        sell_df = read_sheet_table("每日賣出明細")["df"]
        if not sell_df.empty:
            sells = sell_df[sell_df["分點"].map(kf.normalize_branch_name) == canonical].copy()
            sells["_date"] = sells["日期"].map(_parse_sheet_date)
            sells["_amount"] = sells["賣出金額"].map(_count_value).fillna(0.0)
            sells["_code"] = sells["標的股"].map(kf._normalize_stock_name_code_key)
            sells = sells[(sells["_date"] >= start) & (sells["_date"] <= end)]
    except (ToolDataError, KeyError) as exc:
        print(f"⚠️ Discord AI 每日賣出明細略過：{type(exc).__name__}: {exc}")

    recent_cases = branch_events.sort_values("event_date").tail(max(1, int(recent_case_count)))
    completed_recent = recent_cases[recent_cases["resolution"] == "completed"]
    top_buys = (
        window.groupby("stock_code")["buy_amount"].sum().sort_values(ascending=False).head(5)
        if not window.empty else pd.Series(dtype=float)
    )
    top_sells = (
        sells.groupby("_code")["_amount"].sum().sort_values(ascending=False).head(5)
        if not sells.empty else pd.Series(dtype=float)
    )
    name_map: Dict[str, str] = {}
    try:
        name_map = get_stock_name_map()
    except ToolDataError:
        pass
    holding = completed_recent["holding_days"].dropna()
    avg_holding = float(holding.mean()) if len(holding) else None
    if avg_holding is None:
        style = "近期已完成案例不足，無法判斷持有風格"
    elif avg_holding <= 5:
        style = "近期偏向短線進出"
    elif avg_holding <= 20:
        style = "近期偏向波段"
    else:
        style = "近期偏向長抱"
    recent_summary = _outcome_summary(recent_cases)
    branch_recent = {
        "window_start": _fmt_date(start),
        "window_end": _fmt_date(end),
        "event_count": int(len(window)),
        "active_event_days": int(window["event_date"].nunique()) if not window.empty else 0,
        "event_type_distribution": {c: int((window["event_code"] == c).sum()) for c in EVENT_CODES if (window["event_code"] == c).any()},
        "avg_event_buy_amount_text": _money_text(window["buy_amount"].mean()) if not window.empty else "-",
        "top_buy_stocks": [
            {"stock_code": c, "stock_name": name_map.get(c, ""), "event_buy_amount_text": _money_text(v)}
            for c, v in top_buys.items()
        ],
        "top_sell_stocks": [
            {"stock_code": c, "stock_name": name_map.get(c, ""), "sell_amount_text": _money_text(v)}
            for c, v in top_sells.items()
        ],
        "largest_recent_events": [
            {**_event_record(r), "stock_code": r["stock_code"], "stock_name": name_map.get(r["stock_code"], "")}
            for _, r in window.sort_values("buy_amount", ascending=False).head(3).iterrows()
        ],
        "recent_cases": recent_summary,
        "recent_cases_sentence": _outcome_sentence(recent_summary, "最近"),
        "recent_completed_avg_holding_days": _num(avg_holding, 1),
        "holding_style": style,
    }

    same_stock: Dict[str, Any] = {}
    if stock_code:
        code = kf._normalize_stock_name_code_key(stock_code)
        stock_events = branch_events[branch_events["stock_code"] == code]
        open_events = stock_events[stock_events["status"] != "已出清"]
        round_start = open_events["event_date"].min() if not open_events.empty else None
        stock_sells = sells[sells["_code"] == code] if not sells.empty else pd.DataFrame()
        same_stock = {
            "stock_code": code,
            "stock_name": name_map.get(code, ""),
            "round_first_event_date": _fmt_date(round_start) if round_start is not None else "",
            "round_open_events": [_event_record(r) for _, r in open_events.tail(8).iterrows()],
            "round_event_buy_amount_text": _money_text(open_events["buy_amount"].sum()) if not open_events.empty else "-",
            "round_trading_days_since_start": _trading_days_between(round_start, latest) if round_start is not None else 0,
            "latest_event_date": _fmt_date(stock_events["event_date"].max()) if not stock_events.empty else "",
            "events_in_window": [_event_record(r) for _, r in stock_events[stock_events["event_date"] >= start].iterrows()],
            "sells_in_window": [
                {"date": _fmt_date(r["_date"]), "action": _clean_cell(r.get("狀態", "")), "sell_amount_text": _money_text(r["_amount"]),
                 "return_pct": _clean_cell(r.get("報酬率", ""))}
                for _, r in stock_sells.sort_values("_date").tail(8).iterrows()
            ] if not stock_sells.empty else [],
            "stock_history_cases": _outcome_summary(stock_events),
        }
        if LIVE_FLOW_ENABLE if include_live_flow is None else include_live_flow:
            try:
                same_stock["live_flow"] = _live_same_stock_flow(code, canonical)
            except Exception as exc:  # MoneyDJ 失敗時仍保留 Sheet 事件資料
                same_stock["live_flow"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "found": True,
        "branch": canonical,
        "context_note": "近期操作只作為 Recent Behavior Context，不是長期統計證據；未完成案例不計入勝敗",
        "same_stock": same_stock,
        "branch_recent": branch_recent,
        "data_latest_event_date": _fmt_date(latest),
    }



# ============================================================
# Google Sheet 優先：個股追蹤分點籌碼、分點部位、K 線標註
# ============================================================

def _branch_perf_brief(perf: Dict[str, Any], branch: str, codes: List[str]) -> Dict[str, Any]:
    """分點總勝率（背景）＋本次事件別勝率；perf 讀取失敗時回傳空結構。"""
    records = (perf.get("branches") or {}).get(branch) or {}
    overall = records.get("overall") or {}
    events = {}
    for code in codes:
        rec = records.get(code)
        if rec:
            events[code] = {
                "raw_win_rate": rec.get("raw_win_rate"),
                "adjusted_win_rate": rec.get("adjusted_win_rate"),
                "sample_included": _num(rec.get("included_count"), 0),
                "unresolved_count": _num(rec.get("unresolved_count"), 0),
                "weighted_return": rec.get("weighted_return"),
            }
    return {
        "overall_raw_win_rate": overall.get("raw_win_rate"),
        "overall_adjusted_win_rate": overall.get("adjusted_win_rate"),
        "overall_sample_included": _num(overall.get("included_count"), 0),
        "event_performance": events,
    }


def _high_win_rate_branches(perf: Dict[str, Any]) -> List[str]:
    return sorted(
        branch for branch, records in (perf.get("branches") or {}).items()
        if ((records.get("overall") or {}).get("raw_win_rate") or 0) >= HIGH_WIN_RATE_PCT
    )


def selected_five_branches() -> List[str]:
    """週報主程式的精選五分點（WARRANT_SELECTED_BRANCH_FLOW_BRANCHES，預設 華南永昌台中、元大南屯、新光、永豐金內湖、富邦敦南）。"""
    kf = core()
    raw = str(getattr(kf, "SELECTED_BRANCH_FLOW_BRANCHES", "") or "")
    return list(dict.fromkeys(kf.normalize_branch_name(x) for x in re.split(r"[,，、]", raw) if x.strip()))


def display_branch_set() -> Tuple[set, set]:
    """圖片上要顯示的分點（高勝率分點, 精選五分點）；K 線標註與分點動向表共用，兩邊一致。"""
    try:
        high = set(_high_win_rate_branches(read_branch_event_performance()))
    except Exception as exc:  # Sheet 讀不到時不標高勝率分點，K 線照畫
        print(f"⚠️ 高勝率分點清單略過：{type(exc).__name__}: {exc}", flush=True)
        high = set()
    try:
        selected = set(selected_five_branches())
    except Exception:  # 主程式沒有這個設定時只用高勝率分點
        selected = set()
    return high, selected


def _sell_rows(stock_code: str = "", branch: str = "") -> pd.DataFrame:
    """每日賣出明細（減碼／出清）；讀不到時回傳空表，不讓主要回答失敗。"""
    kf = core()
    try:
        df = read_sheet_table("每日賣出明細")["df"]
    except ToolDataError as exc:
        print(f"⚠️ Discord AI 每日賣出明細略過：{exc}")
        return pd.DataFrame()
    if df.empty or not {"分點", "標的股", "日期"}.issubset(df.columns):
        return pd.DataFrame()
    work = df.copy()
    work["_branch"] = work["分點"].map(kf.normalize_branch_name)
    work["_code"] = work["標的股"].map(kf._normalize_stock_name_code_key)
    if stock_code:
        work = work[work["_code"] == kf._normalize_stock_name_code_key(stock_code)]
    if branch:
        work = work[work["_branch"] == branch]
    work["_date"] = work["日期"].map(_parse_sheet_date)
    amounts = work["賣出金額"] if "賣出金額" in work.columns else pd.Series([""] * len(work), index=work.index)
    work["_amount"] = amounts.map(_count_value).fillna(0.0)
    return work.dropna(subset=["_date"]).sort_values("_date")


CHIPS_DAYS = max(5, _env_int("DISCORD_AI_CHIPS_DAYS", 70))          # 和 K 線圖一樣看近 70 個交易日
# K 線上的賣出標記門檻：低於這個金額的減碼／零星賣出不畫（事件出清不受限制，再小也要標）。
MIN_SELL_MARK_AMOUNT = max(0.0, _env_float("DISCORD_AI_CHART_MIN_SELL_AMOUNT", 200_000.0))


def get_sheet_stock_chips(stock_code: str, days: int = CHIPS_DAYS, lookback_days: int = CHIPS_DAYS) -> Dict[str, Any]:
    """個股的「回測追蹤分點」權證籌碼（全部讀 Google Sheet，不即時抓 MoneyDJ）。

    - 最近 N 個交易日的 A～E 大額買進事件（事件定義＝回測程式）
    - 同一視窗內的減碼／出清（每日賣出明細）與未出清事件（出清需與賣出明細對得上）
    - 每個分點的總勝率（背景）與本次事件別勝率
    """
    code, name = _stock_identity(stock_code)
    days = max(1, int(days or CHIPS_DAYS))
    lookback_days = max(int(lookback_days or CHIPS_DAYS), days)
    bundle = load_abcde_event_rows()
    events, latest = bundle["events"], bundle["latest_event_date"]
    if latest is None:
        raise SheetUnavailableError("A～E 事件表沒有資料")
    start_n, end = _recent_event_dates(latest, days, events)
    start_long, _ = _recent_event_dates(latest, lookback_days, events)
    stock_events = events[events["stock_code"] == code]
    # 隔日衝事件連同它當天的減碼／出清賣出紀錄一起排除，避免分點動向表出現「只有賣出紀錄」的隔日衝分點。
    day_trades = stock_events[_day_trade_mask(stock_events)]
    day_trade_sell_keys = {
        (r["branch"], pd.Timestamp(d).normalize())
        for _, r in day_trades.iterrows() for d in (r.get("exit_date"), r.get("reduce_date"))
        if d is not None and not pd.isna(d)
    }
    stock_events = stock_events.drop(day_trades.index)
    try:
        perf = read_branch_event_performance()
    except ToolDataError as exc:
        print(f"⚠️ Discord AI 勝率統計略過：{exc}")
        perf = {}
    high_set = set(_high_win_rate_branches(perf))
    sells = _sell_rows(code)
    if day_trade_sell_keys and not sells.empty:
        keys = list(zip(sells["_branch"], pd.to_datetime(sells["_date"]).dt.normalize()))
        sells = sells[[key not in day_trade_sell_keys for key in keys]]
    sells_long = sells[sells["_date"] >= start_long] if not sells.empty else sells
    # 出清核對用的原始賣出（不套金額門檻），與 K 線標註同一套規則，避免圖文不一致。
    verified_sell_keys = set()
    if not sells_long.empty:
        for _, srow in sells_long.iterrows():
            if float(srow.get("_amount") or 0.0) > 0:
                verified_sell_keys.add((str(srow["_branch"]), pd.Timestamp(srow["_date"]).normalize()))
    # 顯示用的賣出門檻與 K 線標註一致（低於門檻的零星賣出不列）。
    if not sells_long.empty:
        sells_long = sells_long[sells_long["_amount"] >= MIN_SELL_MARK_AMOUNT]

    branches = set(stock_events.loc[stock_events["event_date"] >= start_long, "branch"])
    if not sells_long.empty:
        branches |= set(sells_long["_branch"])
    rows: List[Dict[str, Any]] = []
    for branch in sorted(branches):
        b_events = stock_events[stock_events["branch"] == branch]
        recent = b_events[b_events["event_date"] >= start_n]
        long_events = b_events[b_events["event_date"] >= start_long]
        open_events = b_events[b_events["status"] != "已出清"]
        b_sells = sells_long[sells_long["_branch"] == branch] if not sells_long.empty else sells_long
        codes = [c for c in EVENT_CODES if c in set(recent["event_code"])] or [
            c for c in EVENT_CODES if c in set(long_events["event_code"])
        ]
        brief = _branch_perf_brief(perf, branch, codes)
        # 視窗內的事件才算數，而且「出清」要和每日賣出明細對得上（與 K 線標註同一套規則）。
        window_events = long_events
        exited = 0
        for _, erow in window_events.iterrows():
            exit_date = erow.get("exit_date")
            if exit_date is None or pd.isna(exit_date):
                continue
            if (branch, pd.Timestamp(exit_date).normalize()) in verified_sell_keys:
                exited += 1
        total_events = int(len(window_events))
        holding = max(0, total_events - exited)
        buy_sum = float(window_events["buy_amount"].sum()) if total_events else 0.0
        sell_sum = float(b_sells["_amount"].sum()) if not b_sells.empty else 0.0
        remain_pct = None
        if buy_sum > 0:
            remain_pct = max(0.0, min(100.0, (1.0 - sell_sum / buy_sum) * 100.0))
        if total_events == 0:
            position = "只有賣出紀錄，沒有 A～E 買進事件"
        elif holding <= 0:
            position = f"{total_events} 筆 A～E 事件已全部出清"
        else:
            position = f"持有中 {holding}/{total_events} 筆"
            if remain_pct is not None and sell_sum > 0:
                position += f"・估剩約 {remain_pct:.0f}%（僅供參考）"
        rows.append({
            "branch": branch,
            "is_high_win_rate": branch in high_set,
            "events_recent": [_event_record(r) for _, r in recent.iterrows()],
            "event_buy_amount_recent_text": _money_text(recent["buy_amount"].sum()) if not recent.empty else "-",
            "event_buy_amount_lookback_text": _money_text(long_events["buy_amount"].sum()) if not long_events.empty else "-",
            "_sort_recent": float(recent["buy_amount"].sum()) if not recent.empty else 0.0,
            "_sort_long": float(long_events["buy_amount"].sum()) if not long_events.empty else 0.0,
            "open_event_count": int(holding),
            "event_count_lookback": int(total_events),
            "exited_count_lookback": int(exited),
            "remaining_pct_estimate": _num(remain_pct) if remain_pct is not None else None,
            "position_status": position,
            "reduce_or_exit_lookback": [
                {"date": _fmt_date(r["_date"]), "action": _clean_cell(r.get("狀態", "")), "sell_amount_text": _money_text(r["_amount"])}
                for _, r in b_sells.tail(5).iterrows()
            ] if not b_sells.empty else [],
            "overall_win_rate_background": brief.get("overall_raw_win_rate"),
            "overall_sample_included": brief.get("overall_sample_included"),
            "event_performance": brief.get("event_performance"),
        })
    # 只留「近期有 A～E 事件」或「區間內有事件／有效賣出」的分點；高勝率與近期金額優先。
    rows = [r for r in rows if r["_sort_recent"] > 0 or r["_sort_long"] > 0 or r["reduce_or_exit_lookback"]]
    rows.sort(key=lambda r: (r["_sort_recent"] > 0, r["is_high_win_rate"], r["_sort_recent"], r["_sort_long"]), reverse=True)
    for row in rows:
        row.pop("_sort_recent", None)
        row.pop("_sort_long", None)
    return {
        "stock_code": code,
        "stock_name": name,
        "available": bool(rows),
        "reason": "" if rows else f"近 {lookback_days} 個交易日，回測追蹤分點在這檔股票沒有 A～E 事件或賣出紀錄",
        "period_recent": f"{_fmt_date(start_n)}～{_fmt_date(end)}（{days} 個交易日）",
        "period_lookback": f"{_fmt_date(start_long)}～{_fmt_date(end)}（{lookback_days} 個交易日）",
        "data_latest_event_date": _fmt_date(latest),
        "high_win_rate_threshold_pct": HIGH_WIN_RATE_PCT,
        "branches": rows[:8],
        "data_source": "回測追蹤分點的 A～E 事件（單日權證買進≥100萬）、每日賣出明細、勝率統計",
        "definition_note": "只涵蓋回測追蹤的分點；未達 A～E 門檻的小額買進不在事件表內；勝率為歷史統計，不代表未來結果",
    }


TOP_WARRANT_LIMIT = _env_int("DISCORD_AI_TOP_WARRANT_LIMIT", 5)
TOP15_SHEET = "快取_TOP15共識淨買超"
TOP15_SCOPE = os.getenv("DISCORD_AI_TOP15_SCOPE", "全分點").strip() or "全分點"


def _sheet_number(value: Any) -> Optional[float]:
    text = _clean_cell(value).replace(",", "").replace("%", "").replace("*", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def top15_title(trading_days: Optional[float]) -> str:
    """給使用者看的排行名稱：不用「共識／全分點」等內部術語，改用統計期間描述。"""
    prefix = "權證淨買超排行" if TOP15_SCOPE == "全分點" else "精選分點權證淨買超排行"
    return f"{prefix}（近 {int(trading_days)} 個交易日）" if trading_days else prefix


def _top15_return_text(row: Any) -> str:
    """報酬率欄位有時是「-15.65%*」文字、有時是 -0.0404 這種比例，統一成百分比文字（*＝含備援估價）。"""
    text = _clean_cell(row.get("報酬率文字", ""))
    if "%" in text:
        return text
    number = _sheet_number(row.get("報酬率文字")) if text else None
    if number is None:
        number = _sheet_number(row.get("報酬率"))
    if number is None:
        return ""
    if abs(number) <= 1:  # 比例（-0.0404）轉成百分比
        number *= 100
    return f"{number:+.2f}%"


def get_top_warrant_buy_stocks(limit: int = TOP_WARRANT_LIMIT) -> Dict[str, Any]:
    """權證共識淨買超排行：直接讀使用者統計好的「快取_TOP15共識淨買超」，取統計日期最新的一批（預設資料範圍＝全分點）。"""
    kf = core()
    limit = max(1, min(int(limit or TOP_WARRANT_LIMIT), 15))
    df = read_sheet_table(TOP15_SHEET)["df"].copy()
    required = {"資料範圍", "統計日期", "排名", "標的股"}
    if df.empty or not required.issubset(df.columns):
        raise SheetUnavailableError(f"{TOP15_SHEET} 沒有資料或欄位不足")
    df["_date"] = df["統計日期"].map(_parse_sheet_date)
    df["_rank"] = df["排名"].map(_sheet_number)
    scoped = df[(df["資料範圍"].map(_clean_cell) == TOP15_SCOPE) & df["_date"].notna() & df["_rank"].notna()]
    if scoped.empty:
        raise SheetUnavailableError(f"{TOP15_SHEET} 沒有「{TOP15_SCOPE}」的資料")
    latest_date = scoped["_date"].max()
    latest = scoped[scoped["_date"] == latest_date]
    if "更新時間" in latest.columns and "run_id" in latest.columns:
        # 同一統計日期若重跑多次，只取最後一次 run 的結果。
        last_run = latest.sort_values("更新時間")["run_id"].iloc[-1]
        latest = latest[latest["run_id"] == last_run]
    latest = latest.sort_values("_rank").drop_duplicates(subset=["標的股"], keep="first").head(limit)
    high, selected = display_branch_set()
    rows = []
    for _, r in latest.iterrows():
        branches = []
        for line in [x.strip() for x in _clean_cell(r.get("參與分點明細", "")).split("\n") if x.strip()][:3]:
            name = line.split(" ", 1)[0]
            normalized = kf.normalize_branch_name(name)
            branches.append({"detail": line, "is_high_win_rate": normalized in high, "is_selected_five": normalized in selected})
        net = _sheet_number(r.get("淨買超成本"))
        rows.append({
            "rank": int(r["_rank"]),
            "stock_code": kf._normalize_stock_name_code_key(_clean_cell(r["標的股"])),
            "stock_name": _clean_cell(r.get("標的名稱", "")),
            "net_buy_cost_text": _money_text(net) if net is not None else _clean_cell(r.get("淨買超成本", "")),
            "branch_count": _sheet_number(r.get("參與分點數")),
            "top_branches": branches,
            "events": _clean_cell(r.get("事件", "")),
            "warrant_count": _sheet_number(r.get("權證檔數")),
            "unrealized_return_text": _top15_return_text(r),
            "sheet_pattern": _clean_cell(r.get("型態", "")),
        })
    first = latest.iloc[0] if not latest.empty else {}
    return {
        "available": bool(rows),
        "reason": "" if rows else f"{TOP15_SHEET} 最新一批沒有資料",
        "source": top15_title(_sheet_number(first.get("有效交易日數")) if len(rows) else None),
        "stat_date": _fmt_date(latest_date),
        "period": _clean_cell(first.get("統計期間", "")) if len(rows) else "",
        "valid_trading_days": _sheet_number(first.get("有效交易日數")) if len(rows) else None,
        "stocks": rows,
        "definition_note": "追蹤分點在統計期間內的權證共識淨買超（淨買超成本＝買進成本扣除賣出），報酬率為這些部位目前的估計未實現損益；不是全市場權證買超",
    }


def get_branch_stock_position(branch_name: str, stock_code: str) -> Dict[str, Any]:
    """分點在某股票的部位是否還在：依回測 FIFO 的 A～E 事件狀態與每日賣出明細判斷（只讀 Sheet）。"""
    canonical, candidates = resolve_branch(branch_name)
    if not canonical:
        return {"found": False, "query": branch_name, "candidates": candidates, "reason": "找不到唯一符合的分點"}
    code, name = _stock_identity(stock_code)
    bundle = load_abcde_event_rows()
    events, latest = bundle["events"], bundle["latest_event_date"]
    if latest is None:
        raise SheetUnavailableError("A～E 事件表沒有資料")
    rows = events[(events["branch"] == canonical) & (events["stock_code"] == code)].sort_values("event_date")
    open_rows = rows[rows["status"] != "已出清"]
    closed_rows = rows[rows["status"] == "已出清"]
    sells = _sell_rows(code, canonical)
    if not open_rows.empty:
        reduced = int((open_rows["status"] == "已減碼未出清").sum())
        status = (
            f"部位仍在：{len(open_rows)} 筆 A～E 事件尚未出清（最早 {_fmt_date(open_rows['event_date'].min())}"
            + (f"，其中 {reduced} 筆已減碼" if reduced else "") + "）"
        )
    elif not rows.empty:
        last_exit = closed_rows["exit_date"].dropna().max() if not closed_rows.empty else None
        status = f"A～E 事件部位已全部出清（最後出清 {_fmt_date(last_exit)}）"
    else:
        status = "回測事件表沒有這個分點在這檔股票的 A～E 事件"
    try:
        perf = read_branch_event_performance()
    except ToolDataError:
        perf = {}
    return {
        "found": True,
        "branch": canonical,
        "stock_code": code,
        "stock_name": name,
        "position_status": status,
        "open_events": [_event_record(r) for _, r in open_rows.tail(10).iterrows()],
        "recent_closed_events": [
            {**_event_record(r), "exit_date": _fmt_date(r["exit_date"])} for _, r in closed_rows.tail(5).iterrows()
        ],
        "recent_sells": [
            {"date": _fmt_date(r["_date"]), "action": _clean_cell(r.get("狀態", "")), "event": _clean_cell(r.get("事件", "")),
             "warrant": _clean_cell(r.get("權證名稱", "")), "sell_amount_text": _money_text(r["_amount"])}
            for _, r in sells.tail(8).iterrows()
        ] if not sells.empty else [],
        "branch_performance": _branch_perf_brief(perf, canonical, [c for c in EVENT_CODES if c in set(rows["event_code"])]),
        "data_latest_event_date": _fmt_date(latest),
        "data_source": "A～E 事件（回測 FIFO 狀態）與每日賣出明細",
        "definition_note": "部位依回測 FIFO 追蹤「達 A～E 門檻的事件」；未達門檻的小額買進不在內，因此不是券商實際庫存",
    }


def _day_trade_mask(rows: pd.DataFrame) -> pd.Series:
    """隔日衝事件：已出清，且出清日距買進日 ≤ DAYTRADE_MAX_HOLD_DAYS 個交易日（以平日計，1＝隔一個交易日就出清）。"""
    if rows.empty or DAYTRADE_MAX_HOLD_DAYS <= 0:
        return pd.Series(False, index=rows.index)

    def is_day_trade(row: pd.Series) -> bool:
        buy, exit_ = row.get("event_date"), row.get("exit_date")
        if row.get("status") != "已出清" or buy is None or exit_ is None or pd.isna(buy) or pd.isna(exit_):
            return False
        held = int(np.busday_count(pd.Timestamp(buy).date(), pd.Timestamp(exit_).date()))
        return 0 <= held <= DAYTRADE_MAX_HOLD_DAYS

    return rows.apply(is_day_trade, axis=1).astype(bool)


def chart_marks_for_stock(stock_code: str, dates: List[str], branch_name: str = "") -> Dict[str, Any]:
    """K 線標註用的分點買賣點（只讀 Sheet，不含報酬率）。

    指定分點時只標那些分點（可用逗號傳多個，例如本週精選的主力＋高品質分點）；
    否則只標「勝率統計總勝率 ≥ 高勝率門檻」的追蹤分點與精選五分點（本週精選也一樣）。
    買進＝A～E 事件日；出清／減碼＝該事件的 FIFO 出清日、減碼日（落在圖表區間內才標）。
    """
    if str(stock_code or "").strip().upper() in INDEX_CODES:
        return {"mode": "flow", "source": "index", "events": []}
    kf = core()
    if not dates:
        return {}
    code = kf._normalize_stock_name_code_key(stock_code)
    start, end = _parse_sheet_date(dates[0]), _parse_sheet_date(dates[-1])
    bundle = load_abcde_event_rows()
    events = bundle["events"]
    stock_rows = events[events["stock_code"] == code]
    rows = stock_rows[(stock_rows["event_date"] >= start) & (stock_rows["event_date"] <= end)]
    window_count = len(rows)
    if branch_name:
        targets = []
        for raw in [x for x in str(branch_name).split(",") if x.strip()]:
            canonical, _ = resolve_branch(raw)
            targets.append(canonical or kf.normalize_branch_name(raw))
        targets = list(dict.fromkeys(targets))
        rows = rows[rows["branch"].isin(targets)]
        rule = f"分點：{'、'.join(targets)}"
    else:
        high, selected = display_branch_set()
        rows = rows[rows["branch"].isin(high | selected)]
        rule = f"總勝率 {HIGH_WIN_RATE_PCT:g}% 以上的追蹤分點" + ("＋精選五分點" if selected else "")

    def in_window(value: Any) -> bool:
        return value is not None and not pd.isna(value) and start <= value <= end

    branch_count = len(rows)
    day_trades = _day_trade_mask(rows)
    if day_trades.any():
        rule += f"（已排除 {DAYTRADE_MAX_HOLD_DAYS} 個交易日內出清的隔日衝 {int(day_trades.sum())} 筆）"
    rows = rows[~day_trades]
    rows = rows.sort_values("event_date").tail(max(1, CHART_MARK_MAX_EVENTS))
    marks = []
    for no, (_, r) in enumerate(rows.iterrows(), 1):
        marks.append({
            "no": no,
            "branch": r["branch"],
            "event": r["event_code"],
            "buy_date": _fmt_date(r["event_date"]),
            "buy_amount_text": _money_text(r["buy_amount"]),
            "reduce_date": _fmt_date(r["reduce_date"]) if in_window(r["reduce_date"]) else "",
            "exit_date": _fmt_date(r["exit_date"]) if in_window(r["exit_date"]) else "",
            "status": r["status"],
        })
    reason = ""
    if not marks:
        if stock_rows.empty:
            reason = "事件資料內沒有這檔股票的 A～E 買進紀錄；不代表沒有其他權證交易。"
        elif not window_count:
            reason = f"這檔股票有 {len(stock_rows)} 筆 A～E 事件，但買進日都不在本圖日期範圍；目前標記以區間內買進事件為準。"
        elif not branch_count:
            reason = (f"圖表區間有 {window_count} 筆 A～E 事件，但沒有符合指定分點的事件。" if branch_name else
                      f"圖表區間有 {window_count} 筆 A～E 事件，但所屬分點未列入總勝率 {HIGH_WIN_RATE_PCT:g}% 以上或精選分點名單。"
                      "單一事件的調整後勝率不等於分點總勝率；勝率資料未取得也可能影響名單。")
        else:
            reason = f"符合分點條件的 {branch_count} 筆 A～E 事件皆屬 {DAYTRADE_MAX_HOLD_DAYS} 個交易日內出清，已依隔日衝排除規則略過。"
    return {"rule": rule, "events": marks, "data_latest_event_date": _fmt_date(bundle["latest_event_date"]),
            "empty_reason": reason,
            "filter_counts": {"stock_events": len(stock_rows), "in_window": window_count,
                              "eligible_branch": branch_count, "excluded_day_trades": int(day_trades.sum()),
                              "displayed": len(marks)}}


def sheet_flow_marks_for_stock(stock_code: str, dates: List[str], branch_name: str = "") -> Dict[str, Any]:
    """K 線權證分點買賣標註（事件配對版，只讀 Google Sheet）。

    規則：
    - **有編號的一定是 A～E 事件**：事件買進日標編號，同一筆事件的出清日標「同一個編號」。
    - 不是事件出清的賣出（小幅減碼、零星賣出）只畫三角形、不給編號，避免看起來像事件出清。
    - 金額用每日賣出明細的當日賣出金額；買進用事件的單日累積買進金額。
    """
    kf = core()
    empty = {"mode": "flow", "source": "sheet", "events": []}
    if not dates:
        return empty
    if str(branch_name or "").strip() == "__NO_WEEKLY_BRANCH__":
        return empty
    code = kf._normalize_stock_name_code_key(stock_code)
    start, end = _parse_sheet_date(dates[0]), _parse_sheet_date(dates[-1])
    if start is None or end is None:
        return empty
    visible = {str(d) for d in dates}

    bundle = load_abcde_event_rows()
    events = bundle["events"]
    rows = events[(events["stock_code"] == code) & (events["event_date"] >= start) & (events["event_date"] <= end)].copy()

    targets: List[str] = []
    if branch_name:
        for raw in [x for x in re.split(r"[,，、]", str(branch_name)) if x.strip()]:
            canonical, _ = resolve_branch(raw)
            if canonical:
                targets.append(canonical)
            else:
                normalized = kf.normalize_branch_name(raw)
                if normalized:
                    targets.append(normalized)
        targets = list(dict.fromkeys(targets))
        if targets:
            rows = rows[rows["branch"].isin(targets)]

    sells = _sell_rows(code)
    sell_amounts: Dict[Tuple[str, pd.Timestamp], float] = {}
    if not sells.empty:
        sells = sells[(sells["_date"] >= start) & (sells["_date"] <= end)]
        if targets:
            sells = sells[sells["_branch"].isin(targets)]
        for _, row in sells.iterrows():
            key = (str(row["_branch"]), pd.Timestamp(row["_date"]).normalize())
            sell_amounts[key] = sell_amounts.get(key, 0.0) + float(row.get("_amount") or 0.0)

    def in_chart(day: Any) -> str:
        """回傳圖上對應的日期字串；不在這 70 根 K 棒內就回空字串。"""
        if day is None or pd.isna(day):
            return ""
        text = _fmt_date(day)
        return text if text in visible else ""

    marks: List[Dict[str, Any]] = []
    paired_sells: set = set()
    unverified: List[str] = []
    exit_groups: Dict[Tuple[str, pd.Timestamp], Dict[str, Any]] = {}
    ordered = list(rows.sort_values(["event_date", "branch"]).itertuples())
    # A～E 事件的買進一律給編號；同一筆事件的出清沿用同一個編號。
    for no, row in enumerate(ordered, 1):
        branch = str(row.branch)
        event_code = str(getattr(row, "event_code", "") or "")
        buy_day = in_chart(row.event_date)
        if buy_day:
            amount = float(getattr(row, "buy_amount", 0.0) or 0.0)
            marks.append({"no": no, "branch": branch, "event_codes": event_code, "kind": "event_buy",
                          "action": "buy", "action_text": "事件買進", "action_date": buy_day,
                          "net_amount": _num(amount, 0), "net_amount_text": _money_text(amount)})
        exit_date = getattr(row, "exit_date", None)
        exit_day = in_chart(exit_date)
        if exit_day:
            key = (branch, pd.Timestamp(exit_date).normalize())
            amount = sell_amounts.get(key, 0.0)
            # 出清要「兩張表都對得上」：A～E 事件表有出清日，每日賣出明細也真的有當天的賣出，
            # 否則只是事件表的欄位，圖上不畫，避免和追蹤分點動向的「持有中」互相矛盾。
            if amount <= 0:
                unverified.append(f"{branch} {exit_day}")
                continue
            paired_sells.add(key)
            # 同一天同一個分點可能一次清掉好幾筆事件：合併成一個標記，編號列出被清掉的那幾筆，
            # 金額只算一次（當日該分點的賣出合計），否則同一筆賣出會被重複計算。
            group = exit_groups.setdefault(key, {"nos": [], "codes": [], "day": exit_day, "amount": amount})
            group["nos"].append(no)
            if event_code and event_code not in group["codes"]:
                group["codes"].append(event_code)
        reduce_date = getattr(row, "reduce_date", None)
        reduce_day = in_chart(reduce_date)
        if reduce_day and reduce_day != exit_day:
            key = (branch, pd.Timestamp(reduce_date).normalize())
            amount = sell_amounts.get(key, 0.0)
            paired_sells.add(key)
            # 減碼不是出清 → 不給編號；金額太小的也不畫，避免圖面被零股級賣單塞滿。
            if amount >= MIN_SELL_MARK_AMOUNT:
                marks.append({"no": "", "branch": branch, "event_codes": event_code, "kind": "reduce",
                              "action": "sell", "action_text": "減碼", "action_date": reduce_day,
                              "net_amount": _num(-amount, 0), "net_amount_text": _money_text(-amount)})

    # 出清標記：一個分點、同一天只畫一個 ▼，編號＝當天被清掉的那幾筆買進的號碼。
    for (branch, _day), group in exit_groups.items():
        nos = sorted(group["nos"])
        label = "、".join(str(n) for n in nos[:3]) + (f"…共{len(nos)}筆" if len(nos) > 3 else "")
        amount = float(group["amount"] or 0.0)
        marks.append({"no": label, "no_list": nos, "branch": branch, "event_codes": "＋".join(group["codes"]),
                      "kind": "event_exit", "action": "sell", "action_text": "事件出清",
                      "action_date": group["day"], "net_amount": _num(-amount, 0),
                      "net_amount_text": _money_text(-amount)})

    known_branches = {str(b) for b in rows["branch"].unique()} if not rows.empty else set()
    for (branch, day), amount in sorted(sell_amounts.items(), key=lambda item: -item[1]):
        if (branch, day) in paired_sells or amount < MIN_SELL_MARK_AMOUNT:
            continue
        if targets and branch not in targets:
            continue
        if not targets and branch not in known_branches:
            continue
        day_text = in_chart(day)
        if not day_text:
            continue
        marks.append({"no": "", "branch": branch, "event_codes": "", "kind": "reduce",
                      "action": "sell", "action_text": "賣出（非事件）", "action_date": day_text,
                      "net_amount": _num(-amount, 0), "net_amount_text": _money_text(-amount)})

    if not marks:
        print(f"ℹ️ {code} Google Sheet 權證標註：指定分點/期間無事件", flush=True)
        return empty

    if not targets:
        # 沒指定分點時只留累積金額最大的幾個分點，避免圖面過度擁擠。
        topn = max(1, _env_int("CHART_FLOW_TOP_BRANCHES", 4))
        totals: Dict[str, float] = {}
        for mark in marks:
            totals[mark["branch"]] = totals.get(mark["branch"], 0.0) + abs(float(mark.get("net_amount") or 0.0))
        keep = {name for name, _ in sorted(totals.items(), key=lambda item: -item[1])[:topn]}
        marks = [m for m in marks if m["branch"] in keep]

    max_marks = max(1, _env_int("CHART_FLOW_MAX_MARKS", 14))
    if len(marks) > max_marks:
        # 超量時先砍沒有編號的零星賣出，事件買賣一定保留。
        numbered = [m for m in marks if m["no"] != ""]
        extras = [m for m in marks if m["no"] == ""]
        extras.sort(key=lambda m: -abs(float(m.get("net_amount") or 0.0)))
        marks = numbered[:max_marks] + extras[: max(0, max_marks - len(numbered))]
    marks.sort(key=lambda m: (m["action_date"], str(m["no"])))
    if unverified:
        print(f"⚠️ {code} 事件表有出清日、但每日賣出明細沒有對應賣出，圖上不標：{'、'.join(unverified[:6])}", flush=True)
    print(f"✅ {code} Google Sheet 權證標註｜branches={targets or 'auto'}｜"
          f"事件買進 {sum(1 for m in marks if m['kind'] == 'event_buy')} 個｜"
          f"已核對出清 {sum(1 for m in marks if m['kind'] == 'event_exit')} 個｜"
          f"減碼／零星賣出 {sum(1 for m in marks if m['kind'] == 'reduce')} 個", flush=True)
    return {"mode": "flow", "source": "sheet", "events": marks,
            "data_latest_event_date": _fmt_date(bundle.get("latest_event_date"))}


def moneydj_flow_marks_for_stock(stock_code: str, dates: List[str], branch_name: str = "") -> Dict[str, Any]:
    """K 線用「實際權證流水」買賣超點位。

    與 A～E 事件回放分開：先以「分點 × 股票 × 日期」聚合 MoneyDJ 淨額，
    同日不同權證的一買一賣會先互抵，降低換倉造成的假雙訊號。
    未指定分點時，只取區間累積絕對淨額最大的前幾個分點，避免圖面過度擁擠。
    """
    kf = core()
    if not dates:
        return {"mode": "flow", "events": []}
    code = kf._normalize_stock_name_code_key(stock_code)
    start, end = _parse_sheet_date(dates[0]), _parse_sheet_date(dates[-1])
    lookback = min(max(1, len(dates)), _env_int("WARRANT_FLOW_MAX_DAYS", 60))
    try:
        bundle = _warrant_flow_frame(code, lookback)
        flow = bundle.get("flow")
    except Exception as exc:
        print(f"⚠️ {code} 權證流水標註取得失敗：{type(exc).__name__}: {exc}", flush=True)
        return {"mode": "flow", "events": []}
    if flow is None or flow.empty:
        print(f"ℹ️ {code} 權證流水標註：區間內無資料", flush=True)
        return {"mode": "flow", "events": []}
    rows = flow.copy()
    rows["Date"] = pd.to_datetime(rows["Date"], errors="coerce").dt.normalize()
    rows = rows[rows["Date"].notna() & (rows["Date"] >= start) & (rows["Date"] <= end)]
    if branch_name:
        # MoneyDJ 備援是處理 Sheet 名冊外分點，不能用 Sheet 的 fuzzy resolve；直接正規化後精確篩選。
        targets = [kf.normalize_branch_name(raw) for raw in re.split(r"[,，、]", str(branch_name)) if raw.strip()]
        targets = list(dict.fromkeys(x for x in targets if x))
        rows = rows[rows["branch"].isin(targets)]
    else:
        topn = max(1, _env_int("CHART_FLOW_TOP_BRANCHES", 4))
        totals = rows.groupby("branch")["net_amount"].sum().abs().sort_values(ascending=False)
        rows = rows[rows["branch"].isin(list(totals.head(topn).index))]
    if rows.empty:
        print(f"ℹ️ {code} 權證流水標註：指定分點／篩選後無資料", flush=True)
        return {"mode": "flow", "events": []}
    daily = rows.groupby(["branch", "Date"], as_index=False)["net_amount"].sum()
    min_abs = max(0.0, _env_float("CHART_FLOW_MIN_ABS_AMOUNT", 100_000.0))
    daily = daily[daily["net_amount"].abs() >= min_abs]
    if daily.empty:
        print(f"ℹ️ {code} 權證流水標註：全部低於顯示門檻 {min_abs:g}", flush=True)
        return {"mode": "flow", "events": []}
    max_marks = max(1, _env_int("CHART_FLOW_MAX_MARKS", 14))
    # 優先保留大額點位；最後再依日期排序，圖面閱讀較自然。
    daily = daily.assign(_abs=daily["net_amount"].abs()).nlargest(max_marks, "_abs").sort_values("Date")
    marks = []
    for no, (_, row) in enumerate(daily.iterrows(), 1):
        amount = float(row["net_amount"])
        marks.append({
            "no": no,
            "branch": row["branch"],
            "action": "buy" if amount > 0 else "sell",
            "action_text": "買超" if amount > 0 else "賣超",
            "action_date": _fmt_date(row["Date"]),
            "net_amount": _num(amount, 0),
            "net_amount_text": _money_text(amount),
        })
    return {"mode": "flow", "events": marks}



def chart_flow_marks_for_stock(
    stock_code: str,
    dates: List[str],
    branch_name: str = "",
    *,
    source: str = "sheet",
    allow_moneydj_fallback: bool = False,
) -> Dict[str, Any]:
    """權證買賣點位入口。一般會員/週精選固定 Sheet；MoneyDJ 只給管理員明確備援。"""
    requested = str(source or "sheet").lower()
    if requested == "moneydj":
        result = moneydj_flow_marks_for_stock(stock_code, dates, branch_name)
        result["source"] = "moneydj_admin_fallback"
        return result
    result = sheet_flow_marks_for_stock(stock_code, dates, branch_name)
    if result.get("events") or not allow_moneydj_fallback:
        return result
    print(f"⚠️ {stock_code} Sheet 無權證標註，管理員備援改用 MoneyDJ｜branch={branch_name}", flush=True)
    fallback = moneydj_flow_marks_for_stock(stock_code, dates, branch_name)
    fallback["source"] = "moneydj_admin_fallback"
    return fallback


# ============================================================
# 持股成本位置（型態／操作類問題用；只整理價位，不下買賣指令）
# ============================================================

def key_price_levels(tech: Dict[str, Any], vp: Dict[str, Any]) -> Dict[str, Any]:
    """現價上下方的關鍵價位（均線、附近大量區、布林三軌），附距現價 %；全部來自既有計算結果。

    關鍵價位要保留大量區，但版面也要精簡，因此：
    1. 大量區太遠（預設超過 12%）就省略。
    2. 若股價在大量區上方，只保留該大量區「上緣」作為支撐參考。
    3. 若股價在大量區下方，只保留該大量區「下緣」作為壓力參考。
    4. 只有當股價落在大量區內時，才同時保留上下緣。
    5. 量區若符合條件，即使不在最近幾個均線價位內，也會額外保留。
    """
    close = _num(tech.get("close"))
    levels: List[Dict[str, Any]] = []
    zone_keep_pct = max(0.0, float(_env_float("KEY_LEVEL_ZONE_MAX_DISTANCE_PCT", 12.0)))

    def add(label: str, value: Any, source: str) -> None:
        price = _num(value)
        if price is not None and price > 0:
            levels.append({"label": label, "price": price, "source": source})

    def add_zone(label: str, low: Any, high: Any) -> None:
        low_p = _num(low)
        high_p = _num(high)
        if low_p is None or high_p is None or low_p <= 0 or high_p <= 0:
            return
        # 沒有 close 時保守保留兩端；有 close 時依相對位置精簡。
        if close is None:
            add(f"{label}下緣", low_p, "volume_zone")
            add(f"{label}上緣", high_p, "volume_zone")
        elif close > high_p:
            add(f"{label}上緣", high_p, "volume_zone")
        elif close < low_p:
            add(f"{label}下緣", low_p, "volume_zone")
        else:
            add(f"{label}下緣", low_p, "volume_zone")
            add(f"{label}上緣", high_p, "volume_zone")

    for key, info in (tech.get("moving_averages") or {}).items():
        add(key, (info or {}).get("value"), "ma")
    for key, label in (("maximum_volume_zone", "最大量區"), ("second_volume_zone", "第二大量區")):
        zone = vp.get(key) or {}
        add_zone(label, zone.get("price_low"), zone.get("price_high"))
    bb = tech.get("bollinger") or {}
    add("布林上軌", bb.get("upper"), "bollinger")
    add("布林下軌", bb.get("lower"), "bollinger")  # 布林中軌就是 MA20，不重複列

    if close is not None:
        for lv in levels:
            lv["distance_from_close_pct"] = _pct(lv["price"], close)

    def _keep_volume_zone(lv: Dict[str, Any]) -> bool:
        if lv.get("source") != "volume_zone":
            return True
        dist = lv.get("distance_from_close_pct")
        return dist is not None and abs(float(dist)) <= zone_keep_pct

    def _finalize(items: List[Dict[str, Any]], *, support: bool, base_limit: int) -> List[Dict[str, Any]]:
        ordered = sorted(items, key=(lambda lv: -lv["price"]) if support else (lambda lv: lv["price"]))
        base = list(ordered[:base_limit])
        seen = {(str(lv.get("label")), float(lv.get("price"))) for lv in base}
        extras = []
        for lv in ordered[base_limit:]:
            key = (str(lv.get("label")), float(lv.get("price")))
            if lv.get("source") == "volume_zone" and key not in seen:
                extras.append(lv)
                seen.add(key)
        return base + extras

    supports_pool = [lv for lv in levels if close is not None and lv["price"] <= close and _keep_volume_zone(lv)]
    resistances_pool = [lv for lv in levels if close is not None and lv["price"] > close and _keep_volume_zone(lv)]
    supports = _finalize(supports_pool, support=True, base_limit=3)
    resistances = _finalize(resistances_pool, support=False, base_limit=2)
    return {"close": close, "levels": levels, "supports": supports, "resistances": resistances,
            "zone_keep_pct": zone_keep_pct}


def get_cost_position_context(stock_code: str, cost_price: float) -> Dict[str, Any]:
    """成本價相對現價、均線、大量區、布林的位置，以及現價上下方的關鍵價位（全部 Python 計算）。"""
    code, name = _stock_identity(stock_code)
    cost = float(cost_price)
    if cost <= 0:
        raise ToolDataError("成本價必須大於 0")
    tech = get_technical_analysis(code)
    vp = get_volume_profile(code)
    key_levels = key_price_levels(tech, vp)
    close, levels = key_levels["close"], key_levels["levels"]

    def relation(price: float) -> str:
        return "高於" if cost > price else "低於" if cost < price else "等於"

    supports, resistances = key_levels["supports"], key_levels["resistances"]
    return {
        "stock_code": code,
        "stock_name": name,
        "data_date": tech.get("data_date"),
        "cost_price": _num(cost),
        "close": close,
        "unrealized_pct": _pct(close, cost),
        "cost_vs_levels": [{"label": lv["label"], "price": lv["price"], "cost_is": relation(lv["price"])} for lv in levels],
        "pattern_label": vp.get("pattern_label"),
        "ma_alignment": tech.get("ma_alignment"),
        "supports_below_close": supports,
        "resistances_above_close": resistances,
        "note": "價位全部來自日K收盤計算的均線、大量區與布林；僅供觀察，不是買賣指令或目標價",
    }


# ============================================================
# Tool 註冊表
# ============================================================

def get_chart_panel(stock_code: str, branch_name: str = "", with_marks: bool = True, mark_mode: str = "event", flow_source: str = "sheet", allow_moneydj_fallback: bool = False) -> Dict[str, Any]:
    """Only Python OHLC data enters the chart; never parse prices from AI text."""
    kf = core()
    code = kf._normalize_stock_name_code_key(stock_code)
    bundle = _load_price_bundle(code)
    df = bundle["df"].copy().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    required = ["Open", "High", "Low", "Close"]
    chart_columns = required + ["Volume", "MV5", "MV20", "MA5", "MA10", "MA20", "MA60", "BB_UPPER", "BB_MID", "BB_LOWER"]
    for column in chart_columns:
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce").replace([float("inf"), -float("inf")], float("nan"))
    df = df.dropna(subset=required)
    df = df[(df["High"] >= df[["Open", "Close", "Low"]].max(axis=1)) &
            (df["Low"] <= df[["Open", "Close", "High"]].min(axis=1))]
    if df.empty:
        raise ToolDataError("沒有有效的 OHLC 資料")
    # Same window as the reference report (70 by default).
    plot_df = df.tail(max(1, int(getattr(kf, "CHART_LOOKBACK", 70))))
    bars = []
    for date, row in plot_df.iterrows():
        bars.append({"date": _fmt_date(date), **{
            key: _num(row.get(key), 6) for key in chart_columns
        }})
    try:
        name = resolve_stock_name(code)
    except Exception:
        name = ""
    previous = df["Close"].iloc[-2] if len(df) > 1 else None
    profile = {}
    try:
        # Same 20% lower wick / 60% body / 20% upper wick allocation as the
        # reference report. Keep every bin and the original ranking indices.
        closed_plot = closed_frame(bundle).tail(len(plot_df) - (1 if bundle.get("intraday") else 0))
        stats = kf._calculate_weighted_volume_profile_stats(closed_plot if not closed_plot.empty else plot_df, n_bins=40)
        if stats:
            profile = {
                "bins": [float(v) for v in stats["bins"]],
                "profile": [float(v) for v in stats["profile"]],
                "max_idx": int(stats["max_idx"]),
                "second_idx": int(stats["second_idx"]),
            }
    except Exception as exc:
        print(f"⚠️ {code} 價量分布取得失敗：{type(exc).__name__}", flush=True)
    marks: Dict[str, Any] = {}
    if with_marks:
        try:
            marks = (chart_flow_marks_for_stock(code, [bar["date"] for bar in bars], branch_name, source=flow_source, allow_moneydj_fallback=allow_moneydj_fallback) if str(mark_mode).lower() == "flow" else chart_marks_for_stock(code, [bar["date"] for bar in bars], branch_name))
        except Exception as exc:  # Sheet 失敗時 K 線照畫，只是沒有分點標註
            print(f"⚠️ {code} 分點買賣標註略過：{type(exc).__name__}: {exc}", flush=True)
            marks = {"mode": str(mark_mode).lower(), "events": []}
    return {"stock_code": code, "stock_name": name, "bars": bars,
            "volume_profile": profile, "marks": marks, "intraday": bundle.get("intraday") or {},
            "bollinger": analyze_bollinger(closed_frame(bundle)),
            "change_pct": float((df["Close"].iloc[-1] / previous - 1) * 100) if previous else None}


TOOL_REGISTRY: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_chart_panel": get_chart_panel,
    "get_sheet_stock_chips": get_sheet_stock_chips,
    "get_top_warrant_buy_stocks": get_top_warrant_buy_stocks,
    "get_cost_position_context": get_cost_position_context,
    "get_branch_stock_position": get_branch_stock_position,
    "get_stock_overview": get_stock_overview,
    "get_technical_analysis": get_technical_analysis,
    "get_volume_profile": get_volume_profile,
    "get_futures_positions": get_futures_positions,
    "get_warrant_branch": get_warrant_branch,
    "get_high_winrate_branches_buying": get_high_winrate_branches_buying,
    "get_branch_performance": get_branch_performance,
    "get_branch_recent_trades": get_branch_recent_trades,
    "get_branch_stock_history": get_branch_stock_history,
    "get_branch_winrate_rank": get_branch_winrate_rank,
    "get_recent_news": get_recent_news,
    "query_google_sheet": query_google_sheet,
    "get_branch_event_performance": get_branch_event_performance,
    "detect_current_branch_events": detect_current_branch_events,
    "get_branch_recent_behavior": get_branch_recent_behavior,
}

_CANCELLABLE_TOOLS = {"get_warrant_branch", "get_high_winrate_branches_buying"}

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "get_top_warrant_buy_stocks": "權證共識淨買超排行：讀「快取_TOP15共識淨買超」最新統計日期的名次（參數 limit）",
    "get_cost_position_context": "持股成本相對現價、均線、大量區、布林的位置與上下關鍵價位（參數 stock_code, cost_price）",
    "get_sheet_stock_chips": "個股權證籌碼（Google Sheet 優先）：回測追蹤分點近期 A~E 事件、部位狀態、減碼出清、勝率（參數 stock_code, days）",
    "get_branch_stock_position": "分點在某股票的部位是否還在（回測 FIFO 狀態＋每日賣出明細）（參數 branch_name, stock_code）",
    "get_stock_overview": "股價概況：收盤、漲跌幅、成交量、均量、量比（參數 stock_code）",
    "get_technical_analysis": "技術面：MA5/10/20/60、均線排列、MA20突破跌破、KD、MACD、OSC、布林（參數 stock_code）",
    "get_futures_positions": "三大法人台指期未平倉口數與前一日變化（不需參數）；含避險部位，僅供參考",
    "get_volume_profile": "大量區：最大／第二大量區價格、現價位置、突破跌破回踩、價量型態（參數 stock_code）",
    "get_warrant_branch": "個股近N日權證分點買賣超排行與ABCDE事件（參數 stock_code, days=5/10/20）",
    "get_high_winrate_branches_buying": "個股近期買超分點 join 歷史勝率（參數 stock_code, days）",
    "get_branch_performance": "分點歷史勝率／加權報酬／事件數／持有天數，可指定A~E（參數 branch_name, event_type）",
    "get_branch_recent_trades": "分點近10日買賣哪些股票（參數 branch_name, 可選 stock_code）",
    "get_branch_stock_history": "分點在某股票的ABCDE歷史事件與勝敗（參數 branch_name, stock_code）",
    "get_branch_winrate_rank": "近10日分點勝率排行（無參數）",
    "get_recent_news": "個股近期新聞（參數 stock_code）",
    "query_google_sheet": "條件式查詢回測 Google Sheet 白名單工作表（需 worksheet 與至少一個篩選條件）",
    "get_branch_event_performance": "分點 × A~E 事件的原始勝率、小樣本修正勝率、樣本數、未完成數（參數 branch_name, event_type）",
    "detect_current_branch_events": "分點近N交易日在某股票觸發哪些 A~E 事件（參數 stock_code, branch_name）",
    "get_branch_recent_behavior": "分點近期操作習性：同股票本輪操作與近期所有股票（參數 branch_name, 可選 stock_code）",
}

_TOOL_FAILURE_MESSAGES.update({
    "get_top_warrant_buy_stocks": "權證淨買超排行目前無法取得",
    "get_cost_position_context": "成本位置資料目前無法取得",
    "get_sheet_stock_chips": "分點籌碼資料目前無法取得",
    "get_branch_stock_position": "分點部位資料目前無法取得",
    "get_branch_event_performance": "分點事件別績效目前無法取得",
    "detect_current_branch_events": "分點目前事件資料無法取得",
    "get_branch_recent_behavior": "分點近期操作資料無法取得",
})
