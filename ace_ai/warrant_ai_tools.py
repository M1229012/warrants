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
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


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
SMALL_SAMPLE_EVENTS = _env_int("DISCORD_AI_SMALL_SAMPLE_EVENTS", 10)
SHEET_QUERY_MAX_ROWS = 100
NEWS_MAX_ITEMS = _env_int("DISCORD_AI_NEWS_MAX_ITEMS", 5)
NEWS_SUMMARY_MAX_CHARS = 160
TEXT_CELL_MAX_CHARS = 120

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


def apply_bot_process_env() -> Dict[str, str]:
    """在載入主程式前設定 Bot 行程專用環境變數，回傳被強制覆寫的項目。"""
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
    "get_warrant_branch": "目前權證分點資料取得失敗",
    "get_high_winrate_branches_buying": "目前高勝率分點資料取得失敗",
    "get_branch_performance": "歷史分點統計目前無法取得",
    "get_branch_recent_trades": "分點近期買賣資料目前無法取得",
    "get_branch_stock_history": "分點歷史事件資料目前無法取得",
    "get_recent_news": "目前新聞資料取得失敗",
    "get_branch_winrate_rank": "近10日分點勝率排行目前無法取得",
    "query_google_sheet": "Google Sheet 查詢失敗",
}


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
        user_message = f"{_TOOL_FAILURE_MESSAGES.get(name, '資料取得失敗')}（{exc}）"
        print(f"⚠️ Discord AI Tool 失敗：{name}｜Google Sheet｜{exc}")
    except ToolDataError as exc:
        data, ok, error = {}, False, str(exc)
        user_message = f"{_TOOL_FAILURE_MESSAGES.get(name, '資料取得失敗')}（{exc}）"
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

    def build() -> Dict[str, str]:
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

        try:
            perf = _read_branch_perf_df()
            for _, row in perf.iterrows():
                add(row.get("branch", ""), row.get("branch_display", ""))
        except Exception as exc:
            print(f"⚠️ Discord AI 分點清單（勝率統計）讀取失敗：{type(exc).__name__}: {exc}")
        for title in ("快取_近10日分點買賣明細", "股票ABCDE查詢資料"):
            try:
                table = read_sheet_table(title)["df"]
                if "分點" in table.columns:
                    names = table.get("分點名稱", pd.Series([""] * len(table)))
                    for branch, display in set(zip(table["分點"], names)):
                        add(branch, display)
            except Exception as exc:
                print(f"⚠️ Discord AI 分點清單（{title}）讀取失敗：{type(exc).__name__}: {exc}")
        return aliases

    aliases = build()
    CACHE.set("known_branches", aliases, TTL_BRANCH_PERF_SECONDS if aliases else 120)
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
        try:
            ws = sh.worksheet(title)
            values = ws.get_all_values()
        except Exception as exc:
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
    """事件類型字串 → A~E 代碼；無法判斷時回傳空字串。"""
    text = _clean_cell(value).upper()
    match = re.match(r"^([A-E])(?:$|[-_\s])", text)
    return match.group(1) if match else ""


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

def _load_price_bundle(stock_code: str) -> Dict[str, Any]:
    """重用 fetch_stock_data_yf + calculate_indicators，與週報產圖相同的抓取區間。"""
    kf = core()
    code = kf._normalize_stock_name_code_key(stock_code)

    def build() -> Dict[str, Any]:
        stock_df, market, _ = kf.fetch_stock_data_yf(code, period=PRICE_FETCH_PERIOD)
        if stock_df is None or stock_df.empty:
            raise ToolDataError(f"{code} 沒有股價資料")
        df = kf.calculate_indicators(stock_df)
        df["Close_prev"] = df["Close"].shift(1)
        return {"df": df, "market": str(market or "")}

    return _cached(f"price_{code}", TTL_PRICE_SECONDS, build)


def _stock_identity(stock_code: str) -> Tuple[str, str]:
    code = core()._normalize_stock_name_code_key(stock_code)
    if not code:
        raise ToolDataError("股票代號不可為空")
    try:
        name = resolve_stock_name(code)
    except ToolDataError:
        name = ""
    return code, name


PRICE_SOURCE_NOTE = "FinMind 日K收盤資料（非盤中即時）"


# ============================================================
# Tool 1：股價概況
# ============================================================

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
    return {
        "stock_code": code,
        "stock_name": name,
        "market": bundle["market"],
        "data_date": _fmt_date(df.index[-1]),
        "data_source": PRICE_SOURCE_NOTE,
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
        "volume_ratio_vs_mv5": _num(volume / mv5) if volume is not None and mv5 else None,
        "volume_ratio_vs_mv20": _num(volume / mv20) if volume is not None and mv20 else None,
        "volume_unit_note": "成交量與均量單位為張（FinMind 股數 ÷ 1000）；量比＝當日量 ÷ 均量（均量含當日）",
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


def get_technical_analysis(stock_code: str) -> Dict[str, Any]:
    """重用 calculate_indicators 與既有 KD／MACD／均線訊號函式，整理最新技術面。"""
    kf = core()
    code, name = _stock_identity(stock_code)
    df = _load_price_bundle(code)["df"]
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest
    close = _num(latest.get("Close"))
    ma_values = {n: _num(latest.get(f"MA{n}")) for n in (5, 10, 20, 60)}
    upper, mid, lower = (_num(latest.get(c)) for c in ("BB_UPPER", "BB_MID", "BB_LOWER"))
    width = _num(latest.get("BB_WIDTH"))
    percent_b = (
        _num((close - lower) / (upper - lower) * 100)
        if None not in (close, upper, lower) and upper != lower
        else None
    )
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
        "data_source": PRICE_SOURCE_NOTE,
        "close": close,
        "moving_averages": {f"MA{n}": _ma_position(close, v) for n, v in ma_values.items()},
        "ma_alignment": _ma_alignment(ma_values),
        "ma20_cross_recent_3_days": _recent_ma_cross(df, "MA20"),
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
        "bollinger": {
            "upper": upper,
            "mid": mid,
            "lower": lower,
            "width": width,
            "width_pct_of_mid": _num(width / mid * 100) if width is not None and mid else None,
            "percent_b": percent_b,
            "position": _bollinger_position(close, upper, mid, lower),
        },
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
    df = _load_price_bundle(code)["df"]
    ctx = kf.build_weekly_context(df, pd.DataFrame(), kf.WEEK_TRADING_DAYS)
    plot_df = ctx["plot_df"]
    stats = kf._calculate_weighted_volume_profile_stats(plot_df, n_bins=40)
    if not stats:
        raise ToolDataError(f"{code} 近期K線資料不足，無法計算大量區")
    pattern = kf._build_price_volume_pattern_payload(ctx, n_bins=40)

    bins, centers, profile = stats["bins"], stats["centers"], stats["profile"]
    max_idx, second_idx = int(stats["max_idx"]), int(stats["second_idx"])
    close = float(stats["work"]["Close"].iloc[-1])

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

    recent_event = str(pattern.get("recent_maximum_zone_pattern", "") or "")
    return {
        "stock_code": code,
        "stock_name": name,
        "data_date": _fmt_date(plot_df.index[-1]),
        "analysis_window": {
            "start": _fmt_date(plot_df.index[0]),
            "end": _fmt_date(plot_df.index[-1]),
            "trading_days": int(len(plot_df)),
            "price_bins": 40,
        },
        "close": _num(close),
        "maximum_volume_zone": zone(max_idx, "最大量區", "紅色"),
        "second_volume_zone": zone(second_idx, "第二大量區", "橘色"),
        "position_vs_two_zones": pattern.get("latest_position_relative_to_two_zones", ""),
        "recent_maximum_zone_event": recent_event,
        **_event_flags(recent_event),
        "pattern_label": pattern.get("current_pattern_label", ""),
        "pattern_evidence": pattern.get("pattern_evidence", ""),
        "crossing_volume_character": pattern.get("crossing_volume_character", ""),
        "recent_swing_structure": pattern.get("recent_swing_structure", ""),
        "weekly_price_volume_relationship": pattern.get("weekly_price_volume_relationship", ""),
        "system_interpretation": pattern.get("neutral_zone_interpretation", ""),
        "algorithm_note": "與週報圖卡相同：近70根日K、40個價格區間，上下影線各分配20%、實體60%成交量",
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
    days = max(1, min(int(days or 5), 20))

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
        events = kf.fetch_warrant_events_full_market(
            code, name, request_dates[0], request_dates[-1], cancel_event
        )
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
            f"ABCDE 事件取自回測 Google Sheet，只涵蓋回測追蹤的分點（{abcde_error}）"
            if abcde_error
            else "ABCDE 事件取自回測 Google Sheet，只涵蓋回測追蹤的分點"
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
        "data_source": "Google Sheet「勝率統計」（回測程式產生）",
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
        "data_source": "近期買超：MoneyDJ 權證分點（週報TOP同口徑）；歷史勝率：Google Sheet「勝率統計」",
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
        "data_source": "Google Sheet「快取_近10日分點買賣明細」與「股票ABCDE查詢資料」（回測程式產生，只涵蓋回測追蹤的分點）",
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
        "data_source": "Google Sheet「股票ABCDE查詢資料」（回測程式產生）",
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
        "data_source": "Google Sheet「快取_近10日分點勝率排行」（回測程式產生，只涵蓋回測追蹤的分點）",
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


def get_recent_news(stock_code: str, limit: int = NEWS_MAX_ITEMS) -> Dict[str, Any]:
    """近期公司新聞：先用週報當日新聞摘要快取，沒有才走既有多來源新聞抓取（不呼叫 Gemini）。"""
    kf = core()
    code, name = _stock_identity(stock_code)
    if not name:
        raise ToolDataError(f"查不到 {code} 的公司名稱，無法安全比對新聞")

    def build() -> Dict[str, Any]:
        cached_points = kf._load_gsheet_news_points_cache_for_display(code, name, allow_stale=False)
        if cached_points:
            return {
                "stock_code": code,
                "stock_name": name,
                "source_type": "週報當日新聞重點快取（已通過既有 grounding 驗證）",
                "summary_points": list(cached_points)[:3],
                "articles": [],
            }
        articles = kf.fetch_multi_source_news_articles(code, name, max_items=kf.NEWS_GOOGLE_MAX_ITEMS)
        articles = kf._dedupe_news_articles_by_event(
            list(articles or []), code, name, log_label="Discord AI 新聞"
        )
        items = []
        for article in articles:
            title = kf._clean_news_title(article.get("title", ""))
            summary = kf._normalize_news_text(article.get("description", "") or article.get("content", ""))
            if not title or kf._is_price_only_news_without_fundamentals(f"{title} {summary}"):
                continue
            items.append({
                "date": _news_date(article.get("published", "")),
                "title": title,
                "source": str(article.get("source", "") or ""),
                "summary": _truncate(summary, NEWS_SUMMARY_MAX_CHARS),
                "url": str(article.get("url", "") or ""),
                "event_key": kf._news_article_event_key(article, code, name),
            })
            if len(items) >= max(1, int(limit)):
                break
        return {
            "stock_code": code,
            "stock_name": name,
            "source_type": "既有六來源新聞管線（公司主體過濾＋事件去重，未經 Gemini 摘要）",
            "summary_points": [],
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
        pooled: Dict[str, List[float]] = {code: [0.0, 0.0] for code in (*EVENT_CODES, "overall")}
        for _, row in df.iterrows():
            event_label = _clean_cell(row.get("事件類型", ""))
            code = "overall" if event_label.startswith("全部") else _event_letter(event_label)
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
                pooled[code][0] += wins
                pooled[code][1] += included

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
    letter = _event_letter(event_type)
    overall = records.get("overall")
    if letter:
        rec = records.get(letter)
        if not rec:
            return {"found": False, "branch": canonical, "event_type": letter, "reason": f"勝率統計沒有 {letter} 事件資料"}
        return {
            "found": True,
            **_event_perf_payload(canonical, letter, rec),
            "overall_background": _event_perf_payload(canonical, "overall", overall) if overall else {},
            **meta,
        }
    return {
        "found": True,
        "branch": canonical,
        **{code: _event_perf_payload(canonical, code, records[code]) for code in EVENT_CODES if code in records},
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
    include_live_flow: bool = True,
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
        if include_live_flow:
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
# Tool 註冊表
# ============================================================

TOOL_REGISTRY: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_stock_overview": get_stock_overview,
    "get_technical_analysis": get_technical_analysis,
    "get_volume_profile": get_volume_profile,
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
    "get_stock_overview": "股價概況：收盤、漲跌幅、成交量、均量、量比（參數 stock_code）",
    "get_technical_analysis": "技術面：MA5/10/20/60、均線排列、MA20突破跌破、KD、MACD、OSC、布林（參數 stock_code）",
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
    "get_branch_event_performance": "分點事件別績效目前無法取得",
    "detect_current_branch_events": "分點目前事件資料無法取得",
    "get_branch_recent_behavior": "分點近期操作資料無法取得",
})
