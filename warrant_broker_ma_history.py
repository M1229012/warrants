"""
FinMind 完整歷史權證分點均線勝率統計
======================================

固定流程：
1. 逐日下載 FinMind 全市場權證分點 Parquet。
2. 驗證、日期感知權證身分配對、過濾 26 個目標分點並逐日保存。
3. 僅由 compact_daily 重建 ABCDE 事件與跨事件 FIFO 出清結果。
4. 以 TaiwanStockPriceAdj 建立事件日 MA5/MA10/MA20 條件。
5. 輸出 Parquet 與獨立 Excel，不使用 MoneyDJ 或 Google Sheet。

必要環境變數：
    FINMIND_API_0714=<FinMind token>

必要套件：
    pip install requests pandas openpyxl pyarrow

可選維護參數：
    FORCE_RECALCULATE_STATS=1
    FORCE_REDOWNLOAD_DATES=2023-06-21,2023-06-26
    MA20_FLAT_TOLERANCE_PCT=0.001
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # 允許 --self-test 在未安裝網路套件的環境執行。
    requests = None
    HTTPAdapter = None


PROGRAM_VERSION = "1.0.1"
SCHEMA_VERSION = "ma-history-v1"
WARRANT_DATASET = "TaiwanStockWarrantTradingDailyReport"
PRICE_DATASET = "TaiwanStockPriceAdj"
AMOUNT_THRESH = 1_000_000
FINMIND_DOCUMENTED_START = "2023-06-21"

BASE_DIR = Path(os.getenv("MA_HISTORY_BASE_DIR", ".")).resolve()
CACHE_ROOT = Path(
    os.getenv("MA_HISTORY_CACHE_DIR", str(BASE_DIR / "warrant_cache" / "ma_history"))
).resolve()
RAW_DAILY_DIR = CACHE_ROOT / "raw_daily"
COMPACT_DAILY_DIR = CACHE_ROOT / "compact_daily"
STOCK_PRICE_DIR = CACHE_ROOT / "stock_price"
RESULTS_DIR = CACHE_ROOT / "results"
METADATA_DIR = CACHE_ROOT / "metadata"
STATE_PATH = CACHE_ROOT / "state.json"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()

FINMIND_TOKEN = (
    os.getenv("FINMIND_API_0714", "").strip()
    or os.getenv("FINMIND_TOKEN", "").strip()
)
FINMIND_API_BASE = os.getenv(
    "FINMIND_API_BASE", "https://api.finmindtrade.com/api/v4"
).rstrip("/")
FINMIND_DATA_URL = f"{FINMIND_API_BASE}/data"
FINMIND_DATALIST_URL = f"{FINMIND_API_BASE}/datalist"
FINMIND_STORAGE_OBJECTS_URL = f"{FINMIND_API_BASE}/storage_objects"
REQUEST_CONNECT_TIMEOUT = float(os.getenv("FINMIND_CONNECT_TIMEOUT", "10"))
REQUEST_READ_TIMEOUT = float(os.getenv("FINMIND_READ_TIMEOUT", "180"))
MAX_RETRIES = max(int(os.getenv("FINMIND_MAX_RETRIES", "4")), 1)
RETRY_BASE_SECONDS = max(float(os.getenv("FINMIND_RETRY_BASE_SECONDS", "2")), 0.1)
METADATA_CACHE_HOURS = max(float(os.getenv("FINMIND_METADATA_CACHE_HOURS", "12")), 0)
PROGRESS_EVERY_DAYS = max(int(os.getenv("PROGRESS_EVERY_DAYS", "20")), 1)
LOCAL_ERROR_CIRCUIT_BREAKER = max(
    int(os.getenv("LOCAL_ERROR_CIRCUIT_BREAKER", "5")), 2
)
MA20_FLAT_TOLERANCE_PCT = float(
    os.getenv("MA20_FLAT_TOLERANCE_PCT", "0.001")
)
FORCE_RECALCULATE_STATS = os.getenv(
    "FORCE_RECALCULATE_STATS", "0"
).strip().lower() in ("1", "true", "yes")
FORCE_REDOWNLOAD_DATES = os.getenv(
    "FORCE_REDOWNLOAD_DATES", ""
).strip()
KEEP_RAW_DAILY = os.getenv(
    "KEEP_RAW_DAILY", "1"
).strip().lower() not in ("0", "false", "no")

AMOUNT_CLASS_SPECS = [
    ("A", "100萬至未滿160萬", 1_000_000, 1_600_000),
    ("B", "160萬至未滿250萬", 1_600_000, 2_500_000),
    ("C", "250萬至未滿500萬", 2_500_000, 5_000_000),
    ("D", "500萬至未滿1000萬", 5_000_000, 10_000_000),
    ("E", "1000萬以上", 10_000_000, None),
]
AMOUNT_CLASS_LABELS = {code: label for code, label, _, _ in AMOUNT_CLASS_SPECS}

TARGET_PATTERNS = {
    "富邦公益": r"富邦.*公益",
    "富邦敦南": r"富邦.*敦南",
    "富邦仁愛": r"富邦.*仁愛",
    "新光": r"^新光$",
    "永豐金內湖": r"永豐.*內湖",
    "永豐金竹北": r"永豐.*竹北",
    "永豐金竹科": r"永豐.*竹科",
    "永豐金市政": r"永豐.*市政",
    "永豐金信義": r"永豐.*信義",
    "華南永昌台中": r"華南.*台中",
    "華南永昌淡水": r"華南.*淡水",
    "福邦證券": r"^福邦證券",
    "群益東大": r"群益.*東大",
    "群益金鼎古亭": r"群益.*古亭",
    "群益金鼎新竹": r"群益.*新竹",
    "元大內湖民權": r"元大.*(內湖.*民權|民權)",
    "元大南屯": r"元大.*南屯",
    "元大汐止": r"元大.*汐止",
    "元大虎尾": r"元大.*虎尾",
    "元大彰化民生": r"元大.*彰化民生",
    "兆豐板橋": r"兆豐.*板橋",
    "凱基士林": r"凱基.*士林",
    "凱基中山": r"凱基.*中山",
    "國票敦北法人": r"國票.*(敦北|法人)",
    "統一三多": r"統一.*三多",
    "第一金中壢": r"第一金.*中壢",
}

FALLBACK_BROKERS = {
    "富邦公益": ("富邦-公益", "961F"),
    "富邦敦南": ("富邦-敦南", "9663"),
    "富邦仁愛": ("富邦-仁愛", "9676"),
    "新光": ("新光", "8560"),
    "永豐金內湖": ("永豐金-內湖", "9A9g"),
    "永豐金竹北": ("永豐金-竹北", "9A9P"),
    "永豐金竹科": ("永豐金-竹科", "9A9X"),
    "永豐金市政": ("永豐金-市政", "9A9W"),
    "永豐金信義": ("永豐金-信義", "9A9R"),
    "華南永昌台中": ("華南永昌-台中", "9302"),
    "華南永昌淡水": ("華南永昌-淡水", "9316"),
    "福邦證券": ("福邦證券", "6480"),
    "群益東大": ("群益金鼎-東大", "9135"),
    "群益金鼎古亭": ("群益金鼎-古亭", "918C"),
    "群益金鼎新竹": ("群益金鼎-新竹", "9186"),
    "元大內湖民權": ("元大-內湖民權", "9867"),
    "元大南屯": ("元大-南屯", "9853"),
    "元大汐止": ("元大-汐止", "989Q"),
    "元大虎尾": ("元大-虎尾", "980l"),
    "元大彰化民生": ("元大-彰化民生", "989J"),
    "兆豐板橋": ("兆豐-板橋", "700B"),
    "凱基士林": ("凱基-士林", "9238"),
    "凱基中山": ("凱基-中山", "9229"),
    "國票敦北法人": ("國票-敦北法人", "779c"),
    "統一三多": ("統一-三多", "585Q"),
    "第一金中壢": ("第一金-中壢", "538Y"),
}

RAW_REQUIRED_COLUMNS = {
    "securities_trader",
    "price",
    "buy",
    "sell",
    "securities_trader_id",
    "stock_id",
    "date",
}
COMPACT_COLUMNS = [
    "權證代號",
    "權證名稱",
    "標的股",
    "標的名稱",
    "分點",
    "分點名稱",
    "券商代號",
    "日期",
    "買進股數",
    "賣出股數",
    "買進金額",
    "賣出金額",
    "買超股數",
    "買超金額",
    "身分配對狀態",
    "身分配對錯誤原因",
]

MA_POSITION_NAMES = {
    "000": "低於全部均線",
    "001": "僅高於MA20",
    "010": "僅高於MA10",
    "011": "高於MA10與MA20",
    "100": "僅高於MA5",
    "101": "高於MA5與MA20",
    "110": "高於MA5與MA10",
    "111": "高於全部均線",
}

CONDITION_SPECS = [
    ("高於MA5", lambda d: d["高於MA5"] == True),
    ("低於MA5", lambda d: d["高於MA5"] == False),
    ("高於MA10", lambda d: d["高於MA10"] == True),
    ("低於MA10", lambda d: d["高於MA10"] == False),
    ("高於MA20", lambda d: d["高於MA20"] == True),
    ("低於MA20", lambda d: d["高於MA20"] == False),
    ("剛站上MA5", lambda d: d["剛站上MA5"] == True),
    ("剛站上MA10", lambda d: d["剛站上MA10"] == True),
    ("剛站上MA20", lambda d: d["剛站上MA20"] == True),
    ("剛跌破MA5", lambda d: d["剛跌破MA5"] == True),
    ("剛跌破MA10", lambda d: d["剛跌破MA10"] == True),
    ("剛跌破MA20", lambda d: d["剛跌破MA20"] == True),
    ("高於全部均線", lambda d: d["MA位置代碼"] == "111"),
    ("低於全部均線", lambda d: d["MA位置代碼"] == "000"),
    ("均線多頭排列", lambda d: d["均線多頭排列"] == True),
    ("均線空頭排列", lambda d: d["均線空頭排列"] == True),
    ("MA20上彎", lambda d: d["MA20方向"] == "上彎"),
    ("MA20走平", lambda d: d["MA20方向"] == "走平"),
    ("MA20下彎", lambda d: d["MA20方向"] == "下彎"),
]

_thread_local = threading.local()


def ensure_directories() -> None:
    for path in (
        RAW_DAILY_DIR,
        COMPACT_DAILY_DIR,
        STOCK_PRICE_DIR,
        RESULTS_DIR,
        METADATA_DIR,
        OUTPUT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0") and text[:-2].isalnum():
        text = text[:-2]
    return re.sub(r"\s+", "", text)


def normalize_broker_code(value: Any) -> str:
    return normalize_code(value)


def normalize_name(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").replace("　", "")


def parse_date(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def atomic_write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        with temp_path.open("r", encoding="utf-8") as handle:
            json.load(handle)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        frame.to_parquet(temp_path, index=False)
        check = pd.read_parquet(temp_path)
        if list(check.columns) != list(frame.columns):
            raise RuntimeError(f"Parquet 寫入驗證欄位不一致：{path}")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= 0:
        raise FileNotFoundError(str(path))
    return pd.read_parquet(path)


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "first_available_date": "",
        "latest_available_date": "",
        "successful_dates": [],
        "failed_dates": [],
        "confirmed_empty_dates": [],
        "last_completed_date": "",
        "last_statistics_time": "",
        "raw_rows_by_date": {},
        "compact_rows_by_date": {},
        "deduplicated_rows_by_date": {},
        "failure_reasons": {},
        "availability_discovery_source": "",
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
        except Exception as exc:
            damaged = STATE_PATH.with_suffix(
                f".damaged-{datetime.now():%Y%m%d%H%M%S}.json"
            )
            os.replace(STATE_PATH, damaged)
            print(f"⚠️ state.json 損壞，已移至 {damaged.name}：{exc}")
    for key in ("successful_dates", "failed_dates", "confirmed_empty_dates"):
        state[key] = sorted({iso_date(x) for x in state.get(key, []) if iso_date(x)})
    for key in ("raw_rows_by_date", "compact_rows_by_date", "deduplicated_rows_by_date"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    if not isinstance(state.get("failure_reasons"), dict):
        state["failure_reasons"] = {}
    state["schema_version"] = SCHEMA_VERSION
    state["program_version"] = PROGRAM_VERSION
    return state


def save_state(state: dict[str, Any]) -> None:
    for key in ("successful_dates", "failed_dates", "confirmed_empty_dates"):
        state[key] = sorted(set(state.get(key, [])))
    atomic_write_json(state, STATE_PATH)


def get_session() -> Any:
    if requests is None or HTTPAdapter is None:
        raise RuntimeError("缺少 requests；請執行 pip install requests")
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def finmind_headers() -> dict[str, str]:
    if not FINMIND_TOKEN:
        raise RuntimeError("缺少 FINMIND_API_0714（或 FINMIND_TOKEN）環境變數")
    return {
        "Authorization": f"Bearer {FINMIND_TOKEN}",
        "User-Agent": "warrant-broker-ma-history/1.0",
        "Accept": "application/json, application/octet-stream, */*",
    }


def request_with_retries(
    url: str,
    *,
    params: dict[str, Any],
    description: str,
    attempts: Optional[int] = None,
) -> Any:
    total_attempts = max(int(attempts or MAX_RETRIES), 1)
    last_error: Optional[BaseException] = None
    for attempt in range(1, total_attempts + 1):
        try:
            response = get_session().get(
                url,
                params=params,
                headers=finmind_headers(),
                timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
            )
            if response.status_code in (401, 402, 403):
                raise RuntimeError(
                    f"FinMind 權限或額度錯誤（HTTP {response.status_code}）"
                )
            if response.status_code >= 500 or response.status_code in (408, 429):
                response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < total_attempts:
                delay = min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 30)
                print(
                    f"  ⚠️ {description} 第 {attempt}/{total_attempts} 次失敗："
                    f"{type(exc).__name__}: {exc}；{delay:.1f} 秒後重試"
                )
                time.sleep(delay)
    raise RuntimeError(
        f"{description} 重試 {total_attempts} 次仍失敗："
        f"{type(last_error).__name__}: {last_error}"
    )


def metadata_cache_path(dataset: str) -> Path:
    return METADATA_DIR / f"{dataset}.parquet"


def fetch_dataset(
    dataset: str,
    *,
    params: Optional[dict[str, Any]] = None,
    cache: bool = False,
    force: bool = False,
) -> pd.DataFrame:
    cache_path = metadata_cache_path(dataset)
    if cache and cache_path.exists() and not force:
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours <= METADATA_CACHE_HOURS:
            try:
                cached = read_parquet(cache_path)
                if not cached.empty:
                    return cached
            except Exception:
                pass
    query: dict[str, Any] = {"dataset": dataset}
    if params:
        query.update({key: value for key, value in params.items() if value not in (None, "")})
    try:
        response = request_with_retries(
            FINMIND_DATA_URL, params=query, description=f"FinMind {dataset}"
        )
        payload = response.json()
        status = int(payload.get("status", response.status_code) or response.status_code)
        if status != 200:
            raise RuntimeError(payload.get("msg") or f"status={status}")
        frame = pd.DataFrame(payload.get("data") or [])
        if frame.empty:
            raise RuntimeError("回傳空資料")
        if cache:
            atomic_write_parquet(frame, cache_path)
        return frame
    except Exception:
        if cache and cache_path.exists():
            cached = read_parquet(cache_path)
            if not cached.empty:
                print(f"  ⚠️ {dataset} 更新失敗，沿用既有中繼資料快取")
                return cached
        raise


def trading_dates() -> list[str]:
    frame = fetch_dataset("TaiwanStockTradingDate", cache=True)
    if "date" not in frame.columns:
        raise RuntimeError("TaiwanStockTradingDate 缺少 date 欄位")
    dates = sorted({iso_date(value) for value in frame["date"] if iso_date(value)})
    today = datetime.now().date()
    return [value for value in dates if parse_date(value).date() <= today]


def discover_dataset_start_from_metadata() -> tuple[str, str]:
    """
    優先由 FinMind datalist metadata 取得資料起始日。

    datalist 在不同 FinMind 版本的巢狀欄位略有差異，因此以資料集名稱定位
    record，再從 start/min/first/begin 類欄位抽日期。若 metadata 未提供日期，
    才退回官方文件所載的資料集起始日；實際全市場日檔仍逐日由
    storage_objects 回應確認。
    """

    def walk(value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    try:
        response = request_with_retries(
            FINMIND_DATALIST_URL,
            params={"dataset": WARRANT_DATASET},
            description="FinMind datalist metadata",
        )
        if response.status_code == 200:
            payload = response.json()
            candidates: list[str] = []
            for record in walk(payload):
                if WARRANT_DATASET not in {
                    str(item) for item in record.values() if isinstance(item, str)
                }:
                    continue
                for key, value in record.items():
                    key_lower = str(key).lower()
                    if any(
                        token in key_lower
                        for token in ("start", "first", "begin", "minimum", "min_date")
                    ):
                        parsed = iso_date(value)
                        if parsed:
                            candidates.append(parsed)
            if candidates:
                return min(candidates), "FinMind datalist metadata"
    except Exception as exc:
        print(
            f"  ⚠️ datalist 未提供可用起始日，改用官方資料集範圍："
            f"{type(exc).__name__}: {exc}"
        )
    return FINMIND_DOCUMENTED_START, "FinMind 官方資料集範圍備援"


def _response_message(response: Any) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("msg") or payload.get("message") or payload)
    except Exception:
        pass
    return (response.text or "")[:500]


def response_is_parquet(response: Any) -> bool:
    content = response.content or b""
    content_type = str(response.headers.get("Content-Type", "")).lower()
    return (
        "parquet" in content_type
        or (len(content) >= 8 and content[:4] == b"PAR1" and content[-4:] == b"PAR1")
    )


@dataclass
class DownloadResult:
    status: str
    frame: pd.DataFrame
    reason: str = ""


def download_warrant_day(day: str) -> DownloadResult:
    last_reason = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = request_with_retries(
                FINMIND_STORAGE_OBJECTS_URL,
                params={"dataset": WARRANT_DATASET, "date": day},
                description=f"全市場權證分點 {day}",
                attempts=1,
            )
            if response_is_parquet(response):
                frame = pd.read_parquet(io.BytesIO(response.content))
                missing = RAW_REQUIRED_COLUMNS - set(frame.columns)
                if missing:
                    raise RuntimeError(f"schema 缺少欄位：{sorted(missing)}")
                if frame.empty:
                    raise RuntimeError("Parquet 為空，無法確認為有效完整日檔")
                actual_dates = {
                    iso_date(value) for value in frame["date"] if iso_date(value)
                }
                if actual_dates != {day}:
                    raise RuntimeError(
                        f"日檔日期不符：預期 {day}，實際 {sorted(actual_dates)}"
                    )
                return DownloadResult("ok", frame)

            message = _response_message(response)
            message_lower = message.lower()
            explicit_no_data = (
                response.status_code in (400, 404)
                and any(
                    token in message_lower
                    for token in (
                        "not found",
                        "no data",
                        "does not exist",
                        "尚未",
                        "無資料",
                        "不存在",
                    )
                )
            )
            if explicit_no_data:
                return DownloadResult("empty", pd.DataFrame(), message)
            raise RuntimeError(
                f"HTTP {response.status_code}，內容不是 Parquet：{message}"
            )
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                delay = min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 30)
                print(
                    f"  ⚠️ 全市場權證分點 {day} 第 {attempt}/{MAX_RETRIES} 次失敗："
                    f"{last_reason}；{delay:.1f} 秒後重試"
                )
                time.sleep(delay)
    return DownloadResult("error", pd.DataFrame(), last_reason)


def match_target_broker(name: str) -> str:
    for label, pattern in TARGET_PATTERNS.items():
        if re.search(pattern, str(name or "").strip()):
            return label
    return ""


def build_broker_map() -> dict[str, tuple[str, str]]:
    frame = fetch_dataset("TaiwanSecuritiesTraderInfo", cache=True)
    by_code: dict[str, tuple[str, str]] = {}
    if {"securities_trader_id", "securities_trader"}.issubset(frame.columns):
        for code, name in frame[
            ["securities_trader_id", "securities_trader"]
        ].itertuples(index=False, name=None):
            normalized = normalize_broker_code(code)
            if normalized:
                by_code[normalized] = (str(name or "").strip(), str(code or "").strip())
    result: dict[str, tuple[str, str]] = {}
    for label in TARGET_PATTERNS:
        fallback_name, fallback_code = FALLBACK_BROKERS[label]
        result[label] = by_code.get(
            normalize_broker_code(fallback_code), (fallback_name, fallback_code)
        )
    for _, (name, raw_code) in by_code.items():
        label = match_target_broker(name)
        if label:
            canonical = FALLBACK_BROKERS[label][1]
            result[label] = (name, canonical or raw_code)
    return result


def build_stock_master(frame: pd.DataFrame) -> dict[str, str]:
    if not {"stock_id", "stock_name"}.issubset(frame.columns):
        return {}
    work = frame.copy()
    work["_date"] = pd.to_datetime(
        work["date"] if "date" in work.columns else "", errors="coerce"
    )
    work["_row"] = range(len(work))
    work["stock_id"] = work["stock_id"].map(normalize_code)
    work["stock_name"] = work["stock_name"].fillna("").astype(str).str.strip()
    work = work[(work["stock_id"] != "") & (work["stock_name"] != "")]
    work = work.sort_values(["stock_id", "_date", "_row"]).drop_duplicates(
        "stock_id", keep="last"
    )
    return dict(zip(work["stock_id"], work["stock_name"]))


def stock_alias_resolver(stock_master: dict[str, str]) -> list[tuple[str, str, str, bool]]:
    exact_names = {normalize_name(name) for name in stock_master.values() if normalize_name(name)}
    suffixes = (
        "半導體", "科技", "電子", "光電", "精密", "材料", "生技", "醫療",
        "資訊", "電腦", "通信", "通訊", "電機", "機械", "工業", "實業",
        "企業", "國際", "控股", "投控", "建設", "營造", "食品", "鋼鐵",
    )
    candidates: dict[tuple[str, str], tuple[str, str, str, bool]] = {}
    for code, name in stock_master.items():
        normalized = normalize_name(name)
        aliases = {normalized}
        stripped = normalized
        for suffix in suffixes:
            if stripped.endswith(suffix) and len(stripped) > len(suffix) + 1:
                aliases.add(stripped[: -len(suffix)])
        for length in range(min(4, len(normalized)), 1, -1):
            prefix = normalized[:length]
            if prefix not in exact_names or prefix == normalized:
                aliases.add(prefix)
        for alias in aliases:
            if len(alias) >= 2 and alias not in {"台灣", "臺灣"}:
                record = (alias, code, name, alias == normalized)
                candidates[(alias, code)] = record
    candidates[("台灣50", "0050")] = ("台灣50", "0050", "元大台灣50", True)
    candidates[("臺灣50", "0050")] = ("臺灣50", "0050", "元大台灣50", True)
    return sorted(
        candidates.values(), key=lambda item: (len(item[0]), item[3]), reverse=True
    )


def resolve_underlying_from_name(
    warrant_name: str, resolver: list[tuple[str, str, str, bool]]
) -> Optional[tuple[str, str, int, bool]]:
    name = normalize_name(warrant_name)
    if name.startswith(("台灣50正", "臺灣50正", "台灣50反", "臺灣50反")):
        pass
    elif name.startswith(("台灣50", "臺灣50", "元大台灣50", "元大臺灣50")):
        return "0050", "元大台灣50", 4, True
    for prefix, code, stock_name, exact in resolver:
        if name.startswith(prefix):
            return code, stock_name, len(prefix), exact
    return None


@dataclass(frozen=True)
class WarrantIdentity:
    code: str
    name: str
    target_code: str
    target_name: str
    list_date: str
    end_date: str
    status: str
    reason: str


class WarrantIdentityIndex:
    def __init__(self, records: Iterable[WarrantIdentity]):
        self.by_code: dict[str, list[WarrantIdentity]] = defaultdict(list)
        for record in records:
            self.by_code[record.code].append(record)
        for code in self.by_code:
            self.by_code[code].sort(key=lambda rec: (rec.list_date, rec.end_date))

    def resolve(self, warrant_code: str, trade_date: str) -> WarrantIdentity:
        code = normalize_code(warrant_code)
        matches = [
            rec
            for rec in self.by_code.get(code, [])
            if rec.list_date <= trade_date <= rec.end_date
        ]
        if not matches:
            return WarrantIdentity(
                code, "", "", "", "", "", "Unmatched", "交易日找不到有效權證上市區間"
            )
        targets = {(rec.target_code, rec.target_name) for rec in matches if rec.target_code}
        if len(targets) > 1:
            return WarrantIdentity(
                code,
                matches[-1].name,
                "",
                "",
                matches[-1].list_date,
                matches[-1].end_date,
                "Ambiguous",
                f"同一交易日命中多個標的：{sorted(targets)}",
            )
        chosen = max(matches, key=lambda rec: rec.list_date)
        return chosen


def build_warrant_identity_index() -> WarrantIdentityIndex:
    info = fetch_dataset("TaiwanStockInfoWithWarrant", cache=True)
    summary = fetch_dataset("TaiwanStockInfoWithWarrantSummary", cache=True)
    stock_info = fetch_dataset("TaiwanStockInfo", cache=True)
    required = {"stock_id", "target_stock_id", "type", "date", "end_date"}
    missing = required - set(summary.columns)
    if missing:
        raise RuntimeError(f"權證標的歷史對照缺少欄位：{sorted(missing)}")

    stock_parts = [stock_info]
    if {"stock_id", "stock_name"}.issubset(info.columns):
        stock_parts.append(info)
    stock_master = build_stock_master(pd.concat(stock_parts, ignore_index=True))
    resolver = stock_alias_resolver(stock_master)
    names_by_code: dict[str, list[str]] = defaultdict(list)
    if {"stock_id", "stock_name"}.issubset(info.columns):
        for code, name in info[["stock_id", "stock_name"]].itertuples(
            index=False, name=None
        ):
            code_text = normalize_code(code)
            name_text = str(name or "").strip()
            if code_text and name_text and name_text not in names_by_code[code_text]:
                names_by_code[code_text].append(name_text)

    records: list[WarrantIdentity] = []
    work = summary.copy().fillna("")
    work = work[work["type"].astype(str).str.contains("認購", na=False)]
    for raw_code, raw_target, raw_listed, raw_ended in work[
        ["stock_id", "target_stock_id", "date", "end_date"]
    ].itertuples(index=False, name=None):
        code = normalize_code(raw_code)
        target = normalize_code(raw_target)
        listed = iso_date(raw_listed)
        ended = iso_date(raw_ended)
        if not code or not listed or not ended:
            continue
        names = names_by_code.get(code, [])
        name = names[0] if names else code
        resolved_candidates = [
            (candidate, resolve_underlying_from_name(candidate, resolver))
            for candidate in names
        ]
        resolved_candidates = [item for item in resolved_candidates if item[1]]
        matching_names = [
            candidate
            for candidate, resolved in resolved_candidates
            if resolved and resolved[0] == target
        ]
        if matching_names:
            name = max(matching_names, key=len)
        resolved = resolve_underlying_from_name(name, resolver)
        target_name = stock_master.get(target, "")
        status = "Matched"
        reason = "FinMind target_stock_id＋上市區間"
        if resolved:
            resolved_code, resolved_name, prefix_len, exact = resolved
            high_confidence = exact or prefix_len >= 3
            if not target and high_confidence:
                target, target_name = resolved_code, resolved_name
                reason = "權證名稱解析補足標的"
            elif target and resolved_code != target and high_confidence:
                summary_name = normalize_name(target_name)
                if not summary_name or not normalize_name(name).startswith(summary_name):
                    target, target_name = resolved_code, resolved_name
                    reason = "權證名稱高信心修正 FinMind target_stock_id"
        if not target:
            status = "Unmatched"
            reason = "缺少 target_stock_id 且權證名稱無法可靠解析"
        records.append(
            WarrantIdentity(
                code, name, target, target_name, listed, ended, status, reason
            )
        )

    deduplicated: dict[tuple[str, str, str], WarrantIdentity] = {}
    for record in records:
        key = (record.code, record.list_date, record.end_date)
        previous = deduplicated.get(key)
        if previous is None or (
            record.status == "Matched",
            bool(record.target_code),
            len(record.name),
        ) >= (
            previous.status == "Matched",
            bool(previous.target_code),
            len(previous.name),
        ):
            deduplicated[key] = record
    frame = pd.DataFrame([record.__dict__ for record in deduplicated.values()])
    atomic_write_parquet(frame, METADATA_DIR / "warrant_identity_intervals.parquet")
    return WarrantIdentityIndex(deduplicated.values())


def normalize_warrant_day(
    raw: pd.DataFrame,
    day: str,
    identity_index: WarrantIdentityIndex,
    broker_map: dict[str, tuple[str, str]],
) -> tuple[pd.DataFrame, int]:
    broker_by_code: dict[str, tuple[str, str, str]] = {}
    for label, (name, code) in broker_map.items():
        broker_by_code[normalize_broker_code(code)] = (label, name, code)
    work = raw.copy()
    work["stock_id"] = work["stock_id"].map(normalize_code)
    work["securities_trader_id"] = work["securities_trader_id"].map(
        normalize_broker_code
    )
    work = work[work["securities_trader_id"].isin(broker_by_code)].copy()
    if work.empty:
        return pd.DataFrame(columns=COMPACT_COLUMNS), 0

    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work["buy"] = pd.to_numeric(work["buy"], errors="coerce")
    work["sell"] = pd.to_numeric(work["sell"], errors="coerce")
    if work[["price", "buy", "sell"]].isna().any().any():
        raise RuntimeError("價格／買進／賣出欄位含無法解析的數值")
    work["buy"] = work["buy"].astype("int64")
    work["sell"] = work["sell"].astype("int64")
    work = work[(work["buy"] != 0) | (work["sell"] != 0)].copy()
    work["_buy_amount"] = (work["price"] * work["buy"]).round().astype("int64")
    work["_sell_amount"] = (work["price"] * work["sell"]).round().astype("int64")
    work["_date"] = work["date"].map(iso_date)
    work["_broker_name"] = (
        work["securities_trader"].fillna("").astype(str).str.strip()
    )
    grouped = (
        work.groupby(
            ["stock_id", "securities_trader_id", "_date"],
            as_index=False,
            sort=False,
        )
        .agg(
            買進股數=("buy", "sum"),
            賣出股數=("sell", "sum"),
            買進金額=("_buy_amount", "sum"),
            賣出金額=("_sell_amount", "sum"),
            FinMind分點名稱=("_broker_name", "last"),
        )
    )
    before_dedup = len(grouped)
    records: list[dict[str, Any]] = []
    # name=None 很重要：Pandas namedtuple 會把前導底線欄位 `_date`
    # 自動改名成 `_2`，再用 row._asdict() 讀 `_date` 會造成 KeyError。
    for (
        warrant_code,
        broker_code,
        trade_day,
        buy_qty,
        sell_qty,
        buy_amount,
        sell_amount,
        finmind_broker_name,
    ) in grouped.itertuples(index=False, name=None):
        trade_day = trade_day or day
        identity = identity_index.resolve(warrant_code, trade_day)
        label, configured_name, canonical_code = broker_by_code[broker_code]
        broker_name = finmind_broker_name or configured_name
        buy_qty = int(buy_qty)
        sell_qty = int(sell_qty)
        buy_amount = int(buy_amount)
        sell_amount = int(sell_amount)
        records.append(
            {
                "權證代號": warrant_code,
                "權證名稱": identity.name,
                "標的股": identity.target_code,
                "標的名稱": identity.target_name,
                "分點": label,
                "分點名稱": broker_name,
                "券商代號": canonical_code,
                "日期": trade_day,
                "買進股數": buy_qty,
                "賣出股數": sell_qty,
                "買進金額": buy_amount,
                "賣出金額": sell_amount,
                "買超股數": buy_qty - sell_qty,
                "買超金額": buy_amount - sell_amount,
                "身分配對狀態": identity.status,
                "身分配對錯誤原因": identity.reason,
            }
        )
    result = pd.DataFrame(records, columns=COMPACT_COLUMNS)
    duplicate_key = ["權證代號", "券商代號", "日期"]
    duplicates = result.duplicated(duplicate_key, keep=False)
    if duplicates.any():
        sample = result.loc[duplicates, duplicate_key].head(10).to_dict("records")
        raise RuntimeError(f"精簡資料出現重複權證＋券商＋日期：{sample}")
    result = result.sort_values(
        ["日期", "分點", "標的股", "權證代號"]
    ).reset_index(drop=True)
    return result, before_dedup


def raw_path(day: str) -> Path:
    return RAW_DAILY_DIR / f"{day}.parquet"


def compact_path(day: str) -> Path:
    return COMPACT_DAILY_DIR / f"{day}.parquet"


def load_valid_raw_day(day: str) -> Optional[pd.DataFrame]:
    """讀取精簡失敗後留下的 raw，讓下次執行不必重新呼叫 FinMind。"""
    path = raw_path(day)
    if not path.exists() or path.stat().st_size <= 0:
        return None
    try:
        raw = read_parquet(path)
        missing = RAW_REQUIRED_COLUMNS - set(raw.columns)
        actual_dates = {iso_date(value) for value in raw["date"] if iso_date(value)}
        if raw.empty or missing or actual_dates != {day}:
            print(
                f"  ⚠️ {day} 暫存 raw 驗證失敗，將重新下載："
                f"missing={sorted(missing)} dates={sorted(actual_dates)}"
            )
            return None
        return raw
    except Exception as exc:
        print(
            f"  ⚠️ {day} 暫存 raw 無法讀取，將重新下載："
            f"{type(exc).__name__}: {exc}"
        )
        return None


def validate_saved_day(day: str) -> tuple[bool, str, int, int]:
    try:
        raw_rows = 0
        if KEEP_RAW_DAILY:
            raw = read_parquet(raw_path(day))
            missing_raw = RAW_REQUIRED_COLUMNS - set(raw.columns)
            if missing_raw:
                return False, f"raw 缺少欄位 {sorted(missing_raw)}", 0, 0
            raw_dates = {iso_date(value) for value in raw["date"] if iso_date(value)}
            if raw.empty or raw_dates != {day}:
                return False, f"raw 日期不符或為空：{sorted(raw_dates)}", 0, 0
            raw_rows = len(raw)
        compact = read_parquet(compact_path(day))
        missing_compact = set(COMPACT_COLUMNS) - set(compact.columns)
        if missing_compact:
            return False, f"compact 缺少欄位 {sorted(missing_compact)}", 0, 0
        compact_dates = {
            iso_date(value) for value in compact["日期"] if iso_date(value)
        }
        if compact_dates and compact_dates != {day}:
            return False, f"compact 日期不符：{sorted(compact_dates)}", 0, 0
        duplicate_key = ["權證代號", "券商代號", "日期"]
        if compact.duplicated(duplicate_key).any():
            return False, "compact 出現重複權證＋券商＋日期", 0, 0
        return True, "", raw_rows, len(compact)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", 0, 0


def audit_successful_files(state: dict[str, Any]) -> list[str]:
    valid: list[str] = []
    invalid: list[str] = []
    for day in list(state["successful_dates"]):
        ok, reason, raw_rows, compact_rows = validate_saved_day(day)
        if ok:
            valid.append(day)
            if KEEP_RAW_DAILY:
                state["raw_rows_by_date"][day] = raw_rows
            state["compact_rows_by_date"][day] = compact_rows
            state["deduplicated_rows_by_date"][day] = compact_rows
        else:
            invalid.append(day)
            state["failure_reasons"][day] = f"啟動檢查失敗：{reason}"
            print(f"  ⚠️ {day} 成功檔案損壞，排入重新下載：{reason}")
    state["successful_dates"] = sorted(valid)
    state["failed_dates"] = sorted(set(state["failed_dates"]) | set(invalid))
    save_state(state)
    return invalid


def parse_force_dates(text: str) -> list[str]:
    values = [
        iso_date(part.strip())
        for part in re.split(r"[,;；、\s]+", text or "")
        if part.strip()
    ]
    invalid = [part for part in values if not part]
    if invalid:
        raise ValueError(f"FORCE_REDOWNLOAD_DATES 含無效日期：{invalid}")
    return sorted(set(value for value in values if value))


def discover_expected_dates(state: dict[str, Any]) -> list[str]:
    dates = trading_dates()
    override = iso_date(os.getenv("FINMIND_WARRANT_DATA_START_DATE", ""))
    metadata_start, source = discover_dataset_start_from_metadata()
    start = override or metadata_start
    state["availability_discovery_source"] = (
        "FINMIND_WARRANT_DATA_START_DATE" if override else source
    )
    expected = [day for day in dates if day >= start]
    if not expected:
        raise RuntimeError(f"交易日清單沒有 {start} 以後的日期")
    # 實際最早／最新「可用全市場日檔」在下載後以成功日期更新；這裡只建立候選交易日。
    state["first_available_date"] = min(state["successful_dates"], default="")
    state["latest_available_date"] = max(state["successful_dates"], default="")
    return expected


def mark_day_status(
    state: dict[str, Any],
    day: str,
    status: str,
    reason: str = "",
) -> None:
    for key in ("successful_dates", "failed_dates", "confirmed_empty_dates"):
        if day in state[key]:
            state[key].remove(day)
    if status == "ok":
        state["successful_dates"].append(day)
        state["failure_reasons"].pop(day, None)
        state["last_completed_date"] = day
    elif status == "empty":
        state["confirmed_empty_dates"].append(day)
        state["failure_reasons"].pop(day, None)
    else:
        state["failed_dates"].append(day)
        state["failure_reasons"][day] = reason
    state["first_available_date"] = min(state["successful_dates"], default="")
    state["latest_available_date"] = max(state["successful_dates"], default="")
    save_state(state)


def print_download_progress(
    state: dict[str, Any], new_compact_rows: int, processed: int
) -> None:
    successful = state["successful_dates"]
    print(
        "\n【下載進度摘要】\n"
        f"  本次已處理日數：{processed:,}\n"
        f"  目前成功日數：{len(successful):,}\n"
        f"  失敗日數：{len(state['failed_dates']):,}\n"
        f"  確認無資料日數：{len(state['confirmed_empty_dates']):,}\n"
        f"  最早成功日：{min(successful, default='-')}\n"
        f"  最新成功日：{max(successful, default='-')}\n"
        f"  本次新增列數：{new_compact_rows:,}\n"
        f"  累積精簡列數："
        f"{sum(int(v or 0) for v in state['compact_rows_by_date'].values()):,}\n"
    )


def download_and_compact_history(
    state: dict[str, Any],
    expected_dates: list[str],
    identity_index: WarrantIdentityIndex,
    broker_map: dict[str, tuple[str, str]],
) -> None:
    if FORCE_RECALCULATE_STATS:
        print("ℹ️ FORCE_RECALCULATE_STATS=1：跳過全市場日檔網路下載。")
        return

    force_dates = parse_force_dates(FORCE_REDOWNLOAD_DATES)
    # 最新交易日可能在日檔發布前回覆「無資料」。最近 5 個交易日的 empty
    # 每次都重新探測，避免把「尚未發布」永久當成歷史缺檔。
    recent_probe_dates = set(expected_dates[-5:])
    for day in list(state["confirmed_empty_dates"]):
        if day in recent_probe_dates:
            state["confirmed_empty_dates"].remove(day)
    expected_set = set(expected_dates)
    out_of_range = [day for day in force_dates if day not in expected_set]
    if out_of_range:
        print(f"  ⚠️ 強制日期不在候選交易日清單，仍會嘗試：{out_of_range}")
        expected_dates = sorted(set(expected_dates) | set(out_of_range))

    for day in force_dates:
        for key in ("successful_dates", "failed_dates", "confirmed_empty_dates"):
            if day in state[key]:
                state[key].remove(day)
        state["raw_rows_by_date"].pop(day, None)
        state["compact_rows_by_date"].pop(day, None)
        state["deduplicated_rows_by_date"].pop(day, None)
    save_state(state)

    completed = set(state["successful_dates"]) | set(state["confirmed_empty_dates"])
    failed_priority = [day for day in state["failed_dates"] if day in expected_dates]
    forced_priority = [day for day in force_dates if day in expected_dates]
    remaining = [day for day in expected_dates if day not in completed]
    queue = list(dict.fromkeys(forced_priority + failed_priority + remaining))

    if not queue:
        print("✅ 所有候選交易日已有明確狀態；沒有缺少的全市場日檔。")
        return

    print(
        f"【階段一】待處理 {len(queue):,} 個交易日；"
        f"失敗日優先 {len(failed_priority):,} 日。"
    )
    processed = 0
    new_compact_rows = 0
    last_local_error_signature = ""
    consecutive_local_errors = 0
    for day in queue:
        try:
            cached_raw = load_valid_raw_day(day)
            result = (
                DownloadResult("ok", cached_raw, "沿用精簡失敗後保存的 raw")
                if cached_raw is not None
                else download_warrant_day(day)
            )
            if result.status == "empty":
                mark_day_status(state, day, "empty", result.reason)
                print(f"  ↪️ {day}：FinMind 明確回覆無全市場日檔")
            elif result.status != "ok":
                mark_day_status(state, day, "error", result.reason)
                print(f"  ❌ {day}：{result.reason}")
            else:
                raw = result.frame
                # 先保存 raw，再進行精簡。精簡或身分配對失敗時 raw 會留下，
                # 下次直接重用；compact 驗證成功後才依 KEEP_RAW_DAILY 決定刪除。
                if cached_raw is None:
                    atomic_write_parquet(raw, raw_path(day))
                compact, pre_dedup_rows = normalize_warrant_day(
                    raw, day, identity_index, broker_map
                )
                atomic_write_parquet(compact, compact_path(day))
                ok, reason, raw_rows, compact_rows = validate_saved_day(day)
                if not ok:
                    raise RuntimeError(f"落盤後驗證失敗：{reason}")
                state["raw_rows_by_date"][day] = len(raw)
                state["compact_rows_by_date"][day] = compact_rows
                state["deduplicated_rows_by_date"][day] = compact_rows
                mark_day_status(state, day, "ok")
                if not KEEP_RAW_DAILY:
                    try:
                        raw_path(day).unlink(missing_ok=True)
                    except Exception as exc:
                        print(
                            f"  ⚠️ {day} compact 已成功，但暫存 raw 刪除失敗："
                            f"{type(exc).__name__}: {exc}"
                        )
                new_compact_rows += compact_rows
                unmatched = int(
                    (compact["身分配對狀態"] != "Matched").sum()
                ) if not compact.empty else 0
                print(
                    f"  ✅ {day}：原始 {raw_rows:,}｜26分點 {compact_rows:,}｜"
                    f"身分待確認 {unmatched:,}"
                )
            last_local_error_signature = ""
            consecutive_local_errors = 0
        except KeyboardInterrupt:
            print("\n⏹️ 使用者中止；已完成日期均已保存。")
            raise
        except Exception as exc:
            signature = f"{type(exc).__name__}: {exc}"
            mark_day_status(
                state, day, "error", signature
            )
            print(f"  ❌ {day}：{signature}")
            if signature == last_local_error_signature:
                consecutive_local_errors += 1
            else:
                last_local_error_signature = signature
                consecutive_local_errors = 1
            if consecutive_local_errors >= LOCAL_ERROR_CIRCUIT_BREAKER:
                raise RuntimeError(
                    f"連續 {consecutive_local_errors} 個交易日發生相同本機處理錯誤，"
                    f"已啟動斷路器避免持續浪費 runner 時間：{signature}"
                ) from exc
        processed += 1
        if processed % PROGRESS_EVERY_DAYS == 0:
            print_download_progress(state, new_compact_rows, processed)
    print_download_progress(state, new_compact_rows, processed)


def classify_amount_class(total_amount: Any) -> tuple[str, str]:
    try:
        amount = int(total_amount or 0)
    except Exception:
        amount = 0
    for code, label, lower, upper in AMOUNT_CLASS_SPECS:
        if amount >= lower and (upper is None or amount < upper):
            return code, label
    return "", ""


def event_unique_id(broker_code: str, underlying: str, event_day: str) -> str:
    return f"{normalize_code(broker_code)}|{normalize_code(underlying)}|{event_day}"


def build_events_for_day(frame: pd.DataFrame, day: str) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    valid = frame[
        (frame["身分配對狀態"] == "Matched")
        & (frame["標的股"].astype(str).str.strip() != "")
        & (pd.to_numeric(frame["買進金額"], errors="coerce").fillna(0) > 0)
        & (pd.to_numeric(frame["買進股數"], errors="coerce").fillna(0) > 0)
    ].copy()
    if valid.empty:
        return []
    events: list[dict[str, Any]] = []
    group_columns = [
        "分點",
        "分點名稱",
        "券商代號",
        "標的股",
        "標的名稱",
        "日期",
    ]
    for key, group in valid.groupby(group_columns, dropna=False, sort=False):
        broker, broker_name, broker_code, underlying, underlying_name, event_day = key
        event_day = iso_date(event_day) or day
        warrants = (
            group.groupby(["權證代號", "權證名稱"], as_index=False)
            .agg(買進金額=("買進金額", "sum"), 買進股數=("買進股數", "sum"))
        )
        lots: list[dict[str, Any]] = []
        max_single = 0
        for warrant_code, warrant_name, raw_amount, raw_quantity in warrants[
            ["權證代號", "權證名稱", "買進金額", "買進股數"]
        ].itertuples(index=False, name=None):
            amount = int(raw_amount or 0)
            quantity = int(raw_quantity or 0)
            if amount <= 0 or quantity <= 0:
                continue
            max_single = max(max_single, amount)
            lots.append(
                {
                    "買進日": event_day,
                    "權證代號": normalize_code(warrant_code),
                    "權證名稱": str(warrant_name or ""),
                    "金額": amount,
                    "股數": quantity,
                }
            )
        if not lots or max_single < AMOUNT_THRESH:
            continue
        total_amount = sum(lot["金額"] for lot in lots)
        total_quantity = sum(lot["股數"] for lot in lots)
        code, label = classify_amount_class(total_amount)
        if not code:
            continue
        events.append(
            {
                "事件唯一ID": event_unique_id(broker_code, underlying, event_day),
                "分點": broker,
                "分點名稱": broker_name,
                "券商代號": broker_code,
                "標的股": underlying,
                "標的名稱": underlying_name,
                "事件日": event_day,
                "事件代碼": code,
                "事件類型": f"{code}-{label}",
                "單日累積買進金額": int(total_amount),
                "買超股數": int(total_quantity),
                "涵蓋權證數": len({lot["權證代號"] for lot in lots}),
                "權證清單": "；".join(
                    f"{lot['權證代號']} {lot['權證名稱']}" for lot in lots
                ),
                "最大單檔買進金額": int(max_single),
                "lots": lots,
            }
        )
    return events


def checkpoint_frame(
    events: list[dict[str, Any]], last_day: str, checkpoint_type: str
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for event in events:
        row = {
            key: value
            for key, value in event.items()
            if key not in ("lots", "_fifo_allocations")
        }
        lots = event.get("lots", [])
        row["FIFO部位JSON"] = json.dumps(lots, ensure_ascii=False)
        row["尚未出清FIFO部位JSON"] = json.dumps(
            [lot for lot in lots if int(lot.get("剩餘股數", lot.get("股數", 0)) or 0) > 0],
            ensure_ascii=False,
        )
        row["已完成出清資料JSON"] = json.dumps(
            event.get("_fifo_allocations", []), ensure_ascii=False
        )
        row["最後處理日期"] = last_day
        row["checkpoint類型"] = checkpoint_type
        row["程式版本"] = PROGRAM_VERSION
        row["schema版本"] = SCHEMA_VERSION
        rows.append(row)
    return pd.DataFrame(rows)


def load_compact_frames(
    state: dict[str, Any],
) -> tuple[list[pd.DataFrame], list[dict[str, Any]]]:
    frames: list[pd.DataFrame] = []
    events: list[dict[str, Any]] = []
    successful = sorted(state["successful_dates"])
    for index, day in enumerate(successful, start=1):
        frame = read_parquet(compact_path(day))
        frames.append(frame)
        events.extend(build_events_for_day(frame, day))
        if index % PROGRESS_EVERY_DAYS == 0:
            checkpoint = checkpoint_frame(events, day, "event-build")
            atomic_write_parquet(
                checkpoint, RESULTS_DIR / "event_checkpoint.parquet"
            )
            print(
                f"  💾 事件 checkpoint：{day}｜累積事件 {len(events):,}"
            )
    if successful:
        checkpoint = checkpoint_frame(events, successful[-1], "event-build")
        atomic_write_parquet(checkpoint, RESULTS_DIR / "event_checkpoint.parquet")
    ids = [event["事件唯一ID"] for event in events]
    duplicates = [key for key, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise AssertionError(f"事件唯一ID重複：{duplicates[:10]}")
    return frames, events


def build_sale_rows(
    compact_frames: Iterable[pd.DataFrame],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    sales: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for frame in compact_frames:
        if frame.empty:
            continue
        work = frame[
            pd.to_numeric(frame["賣出股數"], errors="coerce").fillna(0) > 0
        ]
        for row in work[
            ["券商代號", "權證代號", "日期", "賣出股數", "賣出金額"]
        ].itertuples(index=False, name=None):
            broker_code, warrant_code, day, quantity, amount = row
            key = (normalize_code(broker_code), normalize_code(warrant_code))
            sales[key].append(
                {
                    "日期": iso_date(day),
                    "賣出股數": int(quantity or 0),
                    "賣出金額": float(amount or 0),
                }
            )
    for key in sales:
        sales[key].sort(key=lambda row: row["日期"])
    return sales


def simulate_group_outcomes_fifo(
    events: list[dict[str, Any]],
    sale_rows: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """沿用主程式的「券商代號＋權證代號、跨事件、舊事件優先」FIFO。"""
    queues: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event_sequence, event in enumerate(events):
        event_day = iso_date(event["事件日"])
        event.update(
            {
                "目前狀態": "未出清",
                "減碼日": None,
                "出清日": None,
                "出清報酬%": None,
                "持有天數": None,
                "原始股數": 0,
                "剩餘股數": 0,
                "累計賣出股數": 0,
                "已實現賣出金額": 0.0,
                "已實現成本": 0.0,
                "_fifo_allocations": [],
            }
        )
        valid_lots = []
        for lot_sequence, lot in enumerate(event.get("lots", [])):
            quantity = int(lot.get("股數", 0) or 0)
            amount = float(lot.get("金額", 0) or 0)
            warrant_code = normalize_code(lot.get("權證代號"))
            broker_code = normalize_code(event.get("券商代號"))
            if quantity <= 0 or amount <= 0 or not warrant_code or not broker_code:
                continue
            lot.update(
                {
                    "均價": amount / quantity,
                    "剩餘股數": quantity,
                    "累計賣出股數": 0,
                    "已實現賣出金額": 0.0,
                    "已實現成本": 0.0,
                }
            )
            valid_lots.append(lot)
            queues[(broker_code, warrant_code)].append(
                {
                    "event": event,
                    "event_sequence": event_sequence,
                    "lot": lot,
                    "lot_sequence": lot_sequence,
                    "event_day": event_day,
                    "buy_day": iso_date(lot.get("買進日")) or event_day,
                }
            )
        event["lots"] = valid_lots
        event["原始股數"] = sum(int(lot["股數"]) for lot in valid_lots)
        event["剩餘股數"] = event["原始股數"]

    for key, queue in queues.items():
        queue.sort(
            key=lambda item: (
                item["event_day"],
                item["buy_day"],
                item["event_sequence"],
                item["lot_sequence"],
            )
        )
        for sale in sale_rows.get(key, []):
            sell_day = iso_date(sale["日期"])
            sell_quantity = int(sale["賣出股數"] or 0)
            sell_amount = float(sale["賣出金額"] or 0)
            if not sell_day or sell_quantity <= 0:
                continue
            sell_price = sell_amount / sell_quantity
            remaining_sale = sell_quantity
            for reference in queue:
                if remaining_sale <= 0:
                    break
                # 與現行主程式相同：事件日當日買賣不互扣。
                if sell_day <= reference["event_day"]:
                    continue
                lot = reference["lot"]
                remaining_lot = int(lot.get("剩餘股數", 0) or 0)
                if remaining_lot <= 0:
                    continue
                allocated = min(remaining_sale, remaining_lot)
                revenue = allocated * sell_price
                cost = allocated * float(lot["均價"])
                lot["剩餘股數"] -= allocated
                lot["累計賣出股數"] += allocated
                lot["已實現賣出金額"] += revenue
                lot["已實現成本"] += cost
                reference["event"]["_fifo_allocations"].append(
                    {
                        "日期": sell_day,
                        "股數": allocated,
                        "賣出金額": revenue,
                        "成本": cost,
                    }
                )
                remaining_sale -= allocated

    for event in events:
        original = sum(int(lot["股數"]) for lot in event["lots"])
        remaining = sum(int(lot["剩餘股數"]) for lot in event["lots"])
        sold = max(original - remaining, 0)
        by_day: dict[str, dict[str, float]] = defaultdict(
            lambda: {"股數": 0, "賣出金額": 0.0, "成本": 0.0}
        )
        for allocation in event["_fifo_allocations"]:
            values = by_day[allocation["日期"]]
            values["股數"] += int(allocation["股數"])
            values["賣出金額"] += float(allocation["賣出金額"])
            values["成本"] += float(allocation["成本"])
        event["原始股數"] = original
        event["剩餘股數"] = remaining
        event["累計賣出股數"] = sold
        event["已實現賣出金額"] = sum(v["賣出金額"] for v in by_day.values())
        event["已實現成本"] = sum(v["成本"] for v in by_day.values())
        if sold <= 0:
            event["目前狀態"] = "未出清"
            continue
        running = original
        cumulative_revenue = 0.0
        for sell_day in sorted(by_day):
            values = by_day[sell_day]
            running = max(running - int(values["股數"]), 0)
            cumulative_revenue += values["賣出金額"]
            if running > 0 and event["減碼日"] is None:
                event["減碼日"] = sell_day
            if running <= 0:
                total_cost = float(
                    sum(float(lot.get("金額", 0) or 0) for lot in event["lots"])
                )
                event["出清日"] = sell_day
                event["出清報酬%"] = (
                    round((cumulative_revenue - total_cost) / total_cost * 100, 2)
                    if total_cost
                    else None
                )
                event["持有天數"] = (
                    parse_date(sell_day) - parse_date(event["事件日"])
                ).days
                break
        event["目前狀態"] = "已出清" if remaining <= 0 else "減碼"
    return events


def price_cache_path(stock_id: str) -> Path:
    safe_code = re.sub(r"[^0-9A-Z_-]", "_", normalize_code(stock_id))
    return STOCK_PRICE_DIR / f"{safe_code}.parquet"


def normalize_price_frame(frame: pd.DataFrame, stock_id: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "stock_id", "close"])
    date_column = next(
        (column for column in ("date", "Date", "日期") if column in frame.columns),
        None,
    )
    close_column = next(
        (
            column
            for column in ("close", "Close", "close_price", "收盤價")
            if column in frame.columns
        ),
        None,
    )
    if not date_column or not close_column:
        raise RuntimeError(
            f"{PRICE_DATASET} schema 缺少日期或還原收盤價：{list(frame.columns)}"
        )
    work = pd.DataFrame(
        {
            "date": frame[date_column].map(iso_date),
            "stock_id": normalize_code(stock_id),
            "close": pd.to_numeric(frame[close_column], errors="coerce"),
        }
    )
    work = work[
        (work["date"] != "")
        & work["close"].notna()
        & (work["close"] > 0)
    ].copy()
    return (
        work.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def contiguous_missing_ranges(
    expected_dates: list[str], cached_dates: set[str]
) -> list[tuple[str, str]]:
    missing = [day for day in expected_dates if day not in cached_dates]
    if not missing:
        return []
    position = {day: index for index, day in enumerate(expected_dates)}
    ranges: list[tuple[str, str]] = []
    start = previous = missing[0]
    for day in missing[1:]:
        if position[day] != position[previous] + 1:
            ranges.append((start, previous))
            start = day
        previous = day
    ranges.append((start, previous))
    return ranges


def ensure_stock_price(
    stock_id: str,
    start_day: str,
    end_day: str,
    all_trading_dates: list[str],
) -> pd.DataFrame:
    path = price_cache_path(stock_id)
    try:
        cached = normalize_price_frame(read_parquet(path), stock_id)
    except Exception:
        cached = pd.DataFrame(columns=["date", "stock_id", "close"])
    expected = [
        day for day in all_trading_dates if start_day <= day <= end_day
    ]
    cached_dates = set(cached["date"]) if not cached.empty else set()
    ranges = contiguous_missing_ranges(expected, cached_dates)
    pieces = [cached]
    for range_start, range_end in ranges:
        try:
            fetched = fetch_dataset(
                PRICE_DATASET,
                params={
                    "data_id": stock_id,
                    "start_date": range_start,
                    "end_date": range_end,
                },
                cache=False,
            )
            normalized = normalize_price_frame(fetched, stock_id)
            pieces.append(normalized)
        except Exception as exc:
            print(
                f"  ⚠️ {stock_id} 還原股價缺口 {range_start}~{range_end}："
                f"{type(exc).__name__}: {exc}"
            )
    merged = (
        pd.concat(pieces, ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    if not merged.empty:
        atomic_write_parquet(merged, path)
    return merged


def find_lookback_start(
    event_day: str, all_trading_dates: list[str], trading_days: int = 80
) -> str:
    prior = [day for day in all_trading_dates if day < event_day]
    return prior[max(len(prior) - trading_days, 0)] if prior else event_day


def prepare_ma_table(price_frame: pd.DataFrame) -> pd.DataFrame:
    work = normalize_price_frame(
        price_frame,
        str(price_frame["stock_id"].iloc[0]) if not price_frame.empty else "",
    )
    if work.empty:
        return work
    work = work.sort_values("date").reset_index(drop=True)
    for window in (5, 10, 20):
        work[f"ma{window}"] = work["close"].rolling(
            window=window, min_periods=window
        ).mean()
    work["prev_close"] = work["close"].shift(1)
    work["prev_ma5"] = work["ma5"].shift(1)
    work["prev_ma10"] = work["ma10"].shift(1)
    work["prev_ma20"] = work["ma20"].shift(1)
    work["ma20_5d_ago"] = work["ma20"].shift(5)
    return work


def missing_ma_values(reason: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "事件日收盤價": None,
        "MA5": None,
        "MA10": None,
        "MA20": None,
        "昨日收盤價": None,
        "昨日MA5": None,
        "昨日MA10": None,
        "昨日MA20": None,
        "5個交易日前MA20": None,
        "高於MA5": None,
        "高於MA10": None,
        "高於MA20": None,
        "剛站上MA5": None,
        "剛站上MA10": None,
        "剛站上MA20": None,
        "剛跌破MA5": None,
        "剛跌破MA10": None,
        "剛跌破MA20": None,
        "MA位置代碼": "",
        "MA位置名稱": "",
        "MA20方向": "",
        "均線多頭排列": None,
        "均線空頭排列": None,
        "均線資料狀態": "Missing",
        "均線錯誤原因": reason,
    }
    return values


def calculate_ma_values(
    ma_table: pd.DataFrame,
    event_day: str,
    tolerance: float = MA20_FLAT_TOLERANCE_PCT,
) -> dict[str, Any]:
    if ma_table.empty:
        return missing_ma_values("無還原股價資料")
    rows = ma_table[ma_table["date"] == event_day]
    if rows.empty:
        return missing_ma_values("事件日沒有還原收盤價；禁止使用事件日後價格補值")
    row = rows.iloc[-1]
    required = [
        "close",
        "ma5",
        "ma10",
        "ma20",
        "prev_close",
        "prev_ma5",
        "prev_ma10",
        "prev_ma20",
        "ma20_5d_ago",
    ]
    missing = [column for column in required if pd.isna(row[column])]
    if missing:
        return missing_ma_values(
            f"事件日前歷史不足，無法計算：{','.join(missing)}"
        )
    close = float(row["close"])
    ma5 = float(row["ma5"])
    ma10 = float(row["ma10"])
    ma20 = float(row["ma20"])
    prev_close = float(row["prev_close"])
    prev_ma5 = float(row["prev_ma5"])
    prev_ma10 = float(row["prev_ma10"])
    prev_ma20 = float(row["prev_ma20"])
    old_ma20 = float(row["ma20_5d_ago"])
    above5 = close > ma5
    above10 = close > ma10
    above20 = close > ma20
    position = f"{int(above5)}{int(above10)}{int(above20)}"
    base = abs(old_ma20)
    relative_difference = (
        (ma20 - old_ma20) / base if base > 0 else ma20 - old_ma20
    )
    if abs(relative_difference) <= tolerance:
        direction = "走平"
    elif relative_difference > 0:
        direction = "上彎"
    else:
        direction = "下彎"
    return {
        "事件日收盤價": close,
        "MA5": ma5,
        "MA10": ma10,
        "MA20": ma20,
        "昨日收盤價": prev_close,
        "昨日MA5": prev_ma5,
        "昨日MA10": prev_ma10,
        "昨日MA20": prev_ma20,
        "5個交易日前MA20": old_ma20,
        "高於MA5": above5,
        "高於MA10": above10,
        "高於MA20": above20,
        "剛站上MA5": close > ma5 and prev_close <= prev_ma5,
        "剛站上MA10": close > ma10 and prev_close <= prev_ma10,
        "剛站上MA20": close > ma20 and prev_close <= prev_ma20,
        "剛跌破MA5": close < ma5 and prev_close >= prev_ma5,
        "剛跌破MA10": close < ma10 and prev_close >= prev_ma10,
        "剛跌破MA20": close < ma20 and prev_close >= prev_ma20,
        "MA位置代碼": position,
        "MA位置名稱": MA_POSITION_NAMES[position],
        "MA20方向": direction,
        "均線多頭排列": ma5 > ma10 > ma20,
        "均線空頭排列": ma5 < ma10 < ma20,
        "均線資料狀態": "OK",
        "均線錯誤原因": "",
    }


def attach_ma_to_events(
    events: list[dict[str, Any]],
    all_trading_dates: list[str],
    statistics_day: str = "",
) -> list[dict[str, Any]]:
    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_stock[normalize_code(event["標的股"])].append(event)
    latest_event_day = max(
        (event["事件日"] for event in events),
        default="",
    )
    latest_day = max(
        value
        for value in (
            iso_date(statistics_day),
            latest_event_day,
            max(all_trading_dates, default=iso_date(datetime.now())),
        )
        if value
    )
    for index, (stock_id, stock_events) in enumerate(sorted(by_stock.items()), start=1):
        earliest_event = min(event["事件日"] for event in stock_events)
        start_day = find_lookback_start(earliest_event, all_trading_dates, 80)
        price_frame = ensure_stock_price(
            stock_id, start_day, latest_day, all_trading_dates
        )
        ma_table = prepare_ma_table(price_frame)
        for event in stock_events:
            event.update(calculate_ma_values(ma_table, event["事件日"]))
        if index % 20 == 0 or index == len(by_stock):
            print(f"  📈 還原股價／均線進度：{index:,}/{len(by_stock):,} 個標的")
    return events


EVENT_OUTPUT_COLUMNS = [
    "事件唯一ID",
    "分點",
    "分點名稱",
    "券商代號",
    "標的股",
    "標的名稱",
    "事件日",
    "事件代碼",
    "事件類型",
    "單日累積買進金額",
    "買超股數",
    "涵蓋權證數",
    "權證清單",
    "目前狀態",
    "出清日",
    "出清報酬%",
    "持有天數",
    "事件日收盤價",
    "MA5",
    "MA10",
    "MA20",
    "昨日收盤價",
    "昨日MA5",
    "昨日MA10",
    "昨日MA20",
    "5個交易日前MA20",
    "高於MA5",
    "高於MA10",
    "高於MA20",
    "剛站上MA5",
    "剛站上MA10",
    "剛站上MA20",
    "剛跌破MA5",
    "剛跌破MA10",
    "剛跌破MA20",
    "MA位置代碼",
    "MA位置名稱",
    "MA20方向",
    "均線多頭排列",
    "均線空頭排列",
    "均線資料狀態",
    "均線錯誤原因",
]


def events_to_frame(events: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for event in events:
        rows.append({column: event.get(column) for column in EVENT_OUTPUT_COLUMNS})
    frame = pd.DataFrame(rows, columns=EVENT_OUTPUT_COLUMNS)
    if frame.empty:
        return frame
    if frame["事件唯一ID"].duplicated().any():
        raise AssertionError("同一券商、標的、事件日產生重複事件")
    invalid_codes = set(frame["事件代碼"]) - set("ABCDE")
    if invalid_codes:
        raise AssertionError(f"事件代碼不在 A~E：{invalid_codes}")
    if len(frame) != int((frame["目前狀態"] == "已出清").sum()) + int(
        (frame["目前狀態"] != "已出清").sum()
    ):
        raise AssertionError("事件總數不等於已出清加未出清")
    valid_ma = frame["均線資料狀態"] == "OK"
    calculated_codes = (
        frame.loc[valid_ma, ["高於MA5", "高於MA10", "高於MA20"]]
        .astype(int)
        .astype(str)
        .agg("".join, axis=1)
    )
    if not calculated_codes.equals(frame.loc[valid_ma, "MA位置代碼"]):
        raise AssertionError("MA位置代碼與高於MA5／10／20不一致")
    return frame


def sample_grade(closed_count: int) -> str:
    if closed_count < 15:
        return "樣本不足"
    if closed_count < 30:
        return "初步觀察"
    if closed_count < 60:
        return "具參考性"
    return "相對穩定"


def result_tag(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    numeric = float(value)
    if numeric > 0:
        return "勝"
    if numeric < 0:
        return "敗"
    return "平手"


def summarize_event_group(
    group: pd.DataFrame,
    *,
    broker: str,
    condition: str = "",
    position_code: str = "",
    event_code: str = "ALL",
) -> dict[str, Any]:
    closed = group[group["目前狀態"] == "已出清"].copy()
    returns = pd.to_numeric(closed["出清報酬%"], errors="coerce").dropna()
    holding = pd.to_numeric(closed["持有天數"], errors="coerce").dropna()
    closed = closed.loc[returns.index] if len(returns) else closed.iloc[0:0]
    result_tags = returns.map(result_tag)
    total_amount = int(
        pd.to_numeric(group["單日累積買進金額"], errors="coerce")
        .fillna(0)
        .sum()
    )
    closed_amount_series = pd.to_numeric(
        closed["單日累積買進金額"], errors="coerce"
    ).fillna(0)
    closed_amount = int(closed_amount_series.sum())
    pnl = float((closed_amount_series * returns / 100).sum()) if len(returns) else 0.0
    event_type = (
        "全部-A+B+C+D+E合併"
        if event_code == "ALL"
        else f"{event_code}-{AMOUNT_CLASS_LABELS[event_code]}"
    )
    closed_count = len(closed)
    row = {
        "分點": broker,
        "均線條件": condition,
        "MA位置代碼": position_code,
        "事件代碼": event_code,
        "事件類型": event_type,
        "事件總數": len(group),
        "已出清筆數": closed_count,
        "未出清筆數": len(group) - closed_count,
        "勝筆數": int((result_tags == "勝").sum()),
        "敗筆數": int((result_tags == "敗").sum()),
        "平手筆數": int((result_tags == "平手").sum()),
        "勝率": round(float((result_tags == "勝").sum()) / closed_count * 100, 2)
        if closed_count
        else None,
        "平均持有天數": round(float(holding.mean()), 2) if len(holding) else None,
        "中位數持有天數": round(float(holding.median()), 2) if len(holding) else None,
        "平均報酬%": round(float(returns.mean()), 2) if len(returns) else None,
        "中位數報酬%": round(float(returns.median()), 2) if len(returns) else None,
        "加權報酬%": round(pnl / closed_amount * 100, 2)
        if closed_amount
        else None,
        "總買進金額": total_amount,
        "已出清買進金額": closed_amount,
        "估算損益金額": int(round(pnl)),
        "最高報酬%": round(float(returns.max()), 2) if len(returns) else None,
        "最低報酬%": round(float(returns.min()), 2) if len(returns) else None,
        "樣本等級": sample_grade(closed_count),
        "資料起始日": group["事件日"].min() if len(group) else None,
        "資料結束日": group["事件日"].max() if len(group) else None,
    }
    if row["勝筆數"] + row["敗筆數"] + row["平手筆數"] != closed_count:
        raise AssertionError("勝＋敗＋平手不等於已出清")
    return row


def build_condition_statistics(events: pd.DataFrame) -> pd.DataFrame:
    valid = events[events["均線資料狀態"] == "OK"].copy()
    brokers = sorted(set(events["分點"].dropna())) if not events.empty else []
    rows: list[dict[str, Any]] = []
    for broker in brokers:
        broker_group = valid[valid["分點"] == broker]
        for condition_name, predicate in CONDITION_SPECS:
            condition_group = broker_group[predicate(broker_group)]
            for code in ["ALL", "A", "B", "C", "D", "E"]:
                group = (
                    condition_group
                    if code == "ALL"
                    else condition_group[condition_group["事件代碼"] == code]
                )
                rows.append(
                    summarize_event_group(
                        group,
                        broker=broker,
                        condition=condition_name,
                        event_code=code,
                    )
                )
    return pd.DataFrame(rows)


def build_position_statistics(events: pd.DataFrame) -> pd.DataFrame:
    valid = events[events["均線資料狀態"] == "OK"].copy()
    brokers = sorted(set(events["分點"].dropna())) if not events.empty else []
    rows: list[dict[str, Any]] = []
    for broker in brokers:
        broker_group = valid[valid["分點"] == broker]
        for position_code in MA_POSITION_NAMES:
            position_group = broker_group[
                broker_group["MA位置代碼"] == position_code
            ]
            for code in ["ALL", "A", "B", "C", "D", "E"]:
                group = (
                    position_group
                    if code == "ALL"
                    else position_group[position_group["事件代碼"] == code]
                )
                rows.append(
                    summarize_event_group(
                        group,
                        broker=broker,
                        position_code=position_code,
                        event_code=code,
                    )
                )
    return pd.DataFrame(rows)


def best_qualified(
    stats: pd.DataFrame, label_column: str
) -> str:
    if stats.empty:
        return "樣本不足，暫不判定"
    eligible = stats[
        (stats["事件代碼"] == "ALL")
        & (stats["已出清筆數"] >= 15)
        & stats["勝率"].notna()
    ]
    if eligible.empty:
        return "樣本不足，暫不判定"
    best = eligible.sort_values(
        ["勝率", "已出清筆數", "事件總數"],
        ascending=[False, False, False],
    ).iloc[0]
    return f"{best[label_column]}（勝率{best['勝率']:.2f}%，已出清{int(best['已出清筆數'])}筆）"


def build_broker_profiles(
    events: pd.DataFrame,
    condition_stats: pd.DataFrame,
    position_stats: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for broker, group in events.groupby("分點", sort=True):
        closed = group[group["目前狀態"] == "已出清"].copy()
        returns = pd.to_numeric(closed["出清報酬%"], errors="coerce")
        amounts = pd.to_numeric(
            closed["單日累積買進金額"], errors="coerce"
        ).fillna(0)
        holding = pd.to_numeric(closed["持有天數"], errors="coerce").dropna()
        valid_positions = group.loc[
            group["均線資料狀態"] == "OK", "MA位置代碼"
        ]
        most_position = (
            valid_positions.value_counts().index[0]
            if not valid_positions.empty
            else "Missing"
        )
        pnl = float((amounts * returns.fillna(0) / 100).sum())
        total_closed_amount = float(amounts[returns.notna()].sum())
        rows.append(
            {
                "分點": broker,
                "資料起始日": group["事件日"].min(),
                "資料結束日": group["事件日"].max(),
                "事件總數": len(group),
                "已出清事件數": len(closed),
                "未出清事件數": len(group) - len(closed),
                "最常出現MA位置": most_position,
                "最高勝率均線條件": best_qualified(
                    condition_stats[condition_stats["分點"] == broker], "均線條件"
                ),
                "最高勝率MA位置": best_qualified(
                    position_stats[position_stats["分點"] == broker], "MA位置代碼"
                ),
                "平均持有天數": round(float(holding.mean()), 2)
                if len(holding)
                else None,
                "中位數持有天數": round(float(holding.median()), 2)
                if len(holding)
                else None,
                "平均報酬率": round(float(returns.mean()), 2)
                if returns.notna().any()
                else None,
                "中位數報酬率": round(float(returns.median()), 2)
                if returns.notna().any()
                else None,
                "加權報酬率": round(pnl / total_closed_amount * 100, 2)
                if total_closed_amount
                else None,
            }
        )
    return pd.DataFrame(rows)


def build_integrity_report(
    state: dict[str, Any],
    expected_dates: list[str],
    events: pd.DataFrame,
) -> pd.DataFrame:
    successful = set(state["successful_dates"])
    failed = set(state["failed_dates"])
    empty = set(state["confirmed_empty_dates"])
    unprocessed = sorted(set(expected_dates) - successful - failed - empty)
    missing_ma = (
        int((events["均線資料狀態"] == "Missing").sum())
        if not events.empty
        else 0
    )
    complete_ma = (
        int((events["均線資料狀態"] == "OK").sum())
        if not events.empty
        else 0
    )
    closed = (
        int((events["目前狀態"] == "已出清").sum())
        if not events.empty
        else 0
    )
    actual_first = min(successful, default="")
    actual_latest = max(successful, default="")
    complete = not failed and not unprocessed
    rows = [
        ("歷史資料狀態", "完整" if complete else "尚未完整"),
        ("FinMind最早可用日期", actual_first or "尚未確認"),
        ("FinMind最新可用日期", actual_latest or "尚未確認"),
        (
            "可用範圍探索來源",
            state.get("availability_discovery_source", ""),
        ),
        ("候選範圍起始日", min(expected_dates, default="")),
        ("候選範圍結束日", max(expected_dates, default="")),
        ("預期交易日數", len(expected_dates)),
        ("成功日數", len(successful)),
        ("失敗日數", len(failed)),
        ("確認無資料日數", len(empty)),
        ("尚未處理日數", len(unprocessed)),
        ("失敗日期清單", "、".join(sorted(failed)) or "-"),
        ("尚未處理日期清單", "、".join(unprocessed) or "-"),
        (
            "原始資料總列數",
            sum(int(v or 0) for v in state["raw_rows_by_date"].values()),
        ),
        (
            "過濾26分點後列數",
            sum(int(v or 0) for v in state["compact_rows_by_date"].values()),
        ),
        (
            "去重複後列數",
            sum(int(v or 0) for v in state["deduplicated_rows_by_date"].values()),
        ),
        ("事件總數", len(events)),
        ("有完整均線事件數", complete_ma),
        ("均線Missing事件數", missing_ma),
        ("已出清事件數", closed),
        ("未出清事件數", len(events) - closed),
        ("最早事件日", events["事件日"].min() if not events.empty else ""),
        ("最新事件日", events["事件日"].max() if not events.empty else ""),
        ("程式版本", PROGRAM_VERSION),
        ("schema版本", SCHEMA_VERSION),
        ("產生時間", datetime.now().isoformat(timespec="seconds")),
    ]
    if not events.empty:
        for broker, count in events["分點"].value_counts().sort_index().items():
            rows.append((f"各分點事件數｜{broker}", int(count)))
    return pd.DataFrame(rows, columns=["項目", "內容"])


def dataframe_for_excel(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in output.columns:
        if output[column].dtype == "object":
            output[column] = output[column].map(
                lambda value: ""
                if value is None
                else value
                if isinstance(value, (str, int, float, bool, datetime, date))
                else json.dumps(value, ensure_ascii=False)
            )
    return output


def style_excel(path: Path) -> None:
    workbook = load_workbook(path)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        if worksheet.max_row >= 1 and worksheet.max_column >= 1:
            worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sample_rows = min(worksheet.max_row, 300)
        for column_index in range(1, worksheet.max_column + 1):
            values = [
                str(worksheet.cell(row_index, column_index).value or "")
                for row_index in range(1, sample_rows + 1)
            ]
            width = min(max(max((len(value) for value in values), default=0) + 2, 10), 42)
            worksheet.column_dimensions[get_column_letter(column_index)].width = width
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=False)
    workbook.save(path)


def write_outputs(
    events: pd.DataFrame,
    condition_stats: pd.DataFrame,
    position_stats: pd.DataFrame,
    profiles: pd.DataFrame,
    integrity: pd.DataFrame,
) -> Path:
    atomic_write_parquet(events, RESULTS_DIR / "events.parquet")
    atomic_write_parquet(
        condition_stats, RESULTS_DIR / "ma_condition_stats.parquet"
    )
    atomic_write_parquet(
        position_stats, RESULTS_DIR / "ma_position_stats.parquet"
    )
    atomic_write_parquet(profiles, RESULTS_DIR / "broker_profiles.parquet")
    output_path = OUTPUT_DIR / f"warrant_broker_ma_history_{datetime.now():%Y%m%d}.xlsx"
    temporary = output_path.with_name(
        f"{output_path.name}.tmp.{os.getpid()}.xlsx"
    )
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            dataframe_for_excel(events).to_excel(
                writer, sheet_name="歷史均線事件明細", index=False
            )
            dataframe_for_excel(condition_stats).to_excel(
                writer, sheet_name="分點均線條件統計", index=False
            )
            dataframe_for_excel(position_stats).to_excel(
                writer, sheet_name="分點MA位置統計", index=False
            )
            dataframe_for_excel(profiles).to_excel(
                writer, sheet_name="分點操作指紋", index=False
            )
            dataframe_for_excel(integrity).to_excel(
                writer, sheet_name="歷史資料完整性", index=False
            )
        style_excel(temporary)
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return output_path


def validate_final_results(
    events: pd.DataFrame,
    condition_stats: pd.DataFrame,
    position_stats: pd.DataFrame,
) -> None:
    if not events.empty:
        if events["事件唯一ID"].duplicated().any():
            raise AssertionError("事件唯一ID不得重複")
        if not set(events["事件代碼"]).issubset(set("ABCDE")):
            raise AssertionError("每筆事件只能屬於 A~E 其中一類")
        closed = int((events["目前狀態"] == "已出清").sum())
        if len(events) != closed + int((events["目前狀態"] != "已出清").sum()):
            raise AssertionError("事件總數不等於已出清加未出清")
    for frame, label in (
        (condition_stats, "均線條件"),
        (position_stats, "MA位置"),
    ):
        if frame.empty:
            continue
        bad = frame[
            frame["勝筆數"] + frame["敗筆數"] + frame["平手筆數"]
            != frame["已出清筆數"]
        ]
        if not bad.empty:
            raise AssertionError(f"{label}統計勝敗平手驗證失敗")
        if (frame["事件總數"] != frame["已出清筆數"] + frame["未出清筆數"]).any():
            raise AssertionError(f"{label}統計事件數驗證失敗")
    valid_ids = set(events["事件唯一ID"]) if not events.empty else set()
    if len(valid_ids) != len(events):
        raise AssertionError("統計事件無法一對一追溯至事件明細")


def make_test_ma_row(**overrides: Any) -> pd.DataFrame:
    row = {
        "date": "2026-01-30",
        "stock_id": "2330",
        "close": 120.0,
        "ma5": 115.0,
        "ma10": 110.0,
        "ma20": 105.0,
        "prev_close": 100.0,
        "prev_ma5": 104.0,
        "prev_ma10": 103.0,
        "prev_ma20": 101.0,
        "ma20_5d_ago": 100.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def run_self_tests() -> dict[str, str]:
    """不連網的小型測試；失敗時直接拋出 AssertionError。"""
    raw_test = pd.DataFrame(
        [
            {
                "securities_trader": "測試分點",
                "price": 10.0,
                "buy": 100,
                "sell": 20,
                "securities_trader_id": "T001",
                "stock_id": "W1",
                "date": "2026-01-02",
            },
            {
                "securities_trader": "測試分點",
                "price": 11.0,
                "buy": 50,
                "sell": 0,
                "securities_trader_id": "T001",
                "stock_id": "W1",
                "date": "2026-01-02",
            },
        ]
    )
    identity_test = WarrantIdentityIndex(
        [
            WarrantIdentity(
                "W1",
                "測試購01",
                "2330",
                "台積電",
                "2025-01-01",
                "2026-12-31",
                "Matched",
                "測試身分",
            )
        ]
    )
    normalized_test, _ = normalize_warrant_day(
        raw_test,
        "2026-01-02",
        identity_test,
        {"測試分點": ("測試分點", "T001")},
    )
    assert len(normalized_test) == 1
    assert normalized_test.iloc[0]["日期"] == "2026-01-02"
    assert int(normalized_test.iloc[0]["買進股數"]) == 150

    high = calculate_ma_values(make_test_ma_row(), "2026-01-30")
    assert high["MA位置代碼"] == "111"
    assert high["均線多頭排列"] is True
    assert high["剛站上MA20"] is True

    low = calculate_ma_values(
        make_test_ma_row(
            close=80,
            ma5=90,
            ma10=100,
            ma20=110,
            prev_close=120,
            prev_ma5=115,
            prev_ma10=110,
            prev_ma20=105,
            ma20_5d_ago=109,
        ),
        "2026-01-30",
    )
    assert low["MA位置代碼"] == "000"
    assert low["均線空頭排列"] is True
    assert low["剛跌破MA10"] is True

    missing = calculate_ma_values(pd.DataFrame(), "2026-01-30")
    assert missing["均線資料狀態"] == "Missing"

    compact = pd.DataFrame(
        [
            {
                "權證代號": "W1",
                "權證名稱": "測試購01",
                "標的股": "2330",
                "標的名稱": "台積電",
                "分點": "測試分點",
                "分點名稱": "測試分點",
                "券商代號": "T001",
                "日期": "2026-01-02",
                "買進股數": 100,
                "賣出股數": 0,
                "買進金額": 1_100_000,
                "賣出金額": 0,
                "買超股數": 100,
                "買超金額": 1_100_000,
                "身分配對狀態": "Matched",
                "身分配對錯誤原因": "",
            },
            {
                "權證代號": "W2",
                "權證名稱": "測試購02",
                "標的股": "2330",
                "標的名稱": "台積電",
                "分點": "測試分點",
                "分點名稱": "測試分點",
                "券商代號": "T001",
                "日期": "2026-01-02",
                "買進股數": 50,
                "賣出股數": 0,
                "買進金額": 500_000,
                "賣出金額": 0,
                "買超股數": 50,
                "買超金額": 500_000,
                "身分配對狀態": "Matched",
                "身分配對錯誤原因": "",
            },
        ],
        columns=COMPACT_COLUMNS,
    )
    built = build_events_for_day(compact, "2026-01-02")
    assert len(built) == 1, "同券商＋標的＋日必須去重成一事件"
    assert built[0]["事件代碼"] == "B"
    built_again = build_events_for_day(compact, "2026-01-02")
    assert [item["事件唯一ID"] for item in built] == [
        item["事件唯一ID"] for item in built_again
    ], "相同精簡資料重算不得增加事件"
    sales = {
        ("T001", "W1"): [
            {"日期": "2026-01-03", "賣出股數": 100, "賣出金額": 1_210_000}
        ],
        ("T001", "W2"): [
            {"日期": "2026-01-03", "賣出股數": 50, "賣出金額": 550_000}
        ],
    }
    closed_event = simulate_group_outcomes_fifo(built, sales)[0]
    assert closed_event["目前狀態"] == "已出清"
    assert closed_event["出清報酬%"] == 10.0

    open_event = {
        **built[0],
        "事件唯一ID": "T001|2317|2026-01-04",
        "標的股": "2317",
        "事件日": "2026-01-04",
        "lots": [
            {
                "買進日": "2026-01-04",
                "權證代號": "W3",
                "權證名稱": "測試購03",
                "金額": 1_000_000,
                "股數": 100,
            }
        ],
    }
    open_result = simulate_group_outcomes_fifo([open_event], {})[0]
    assert open_result["目前狀態"] == "未出清"

    closed_event.update(high)
    event_frame = events_to_frame([closed_event])
    condition_frame = build_condition_statistics(event_frame)
    position_frame = build_position_statistics(event_frame)
    profile_frame = build_broker_profiles(
        event_frame, condition_frame, position_frame
    )
    validate_final_results(event_frame, condition_frame, position_frame)
    assert len(condition_frame) == len(CONDITION_SPECS) * 6
    assert len(position_frame) == len(MA_POSITION_NAMES) * 6
    assert len(profile_frame) == 1

    statistic_events = pd.DataFrame(
        [
            {
                "分點": "測試分點",
                "目前狀態": "已出清",
                "出清報酬%": 10,
                "持有天數": 1,
                "單日累積買進金額": 100,
                "事件日": "2026-01-01",
            },
            {
                "分點": "測試分點",
                "目前狀態": "已出清",
                "出清報酬%": -5,
                "持有天數": 3,
                "單日累積買進金額": 100,
                "事件日": "2026-01-02",
            },
            {
                "分點": "測試分點",
                "目前狀態": "已出清",
                "出清報酬%": 0,
                "持有天數": 100,
                "單日累積買進金額": 100,
                "事件日": "2026-01-03",
            },
        ]
    )
    summary = summarize_event_group(
        statistic_events, broker="測試分點", event_code="ALL"
    )
    assert summary["勝率"] == round(1 / 3 * 100, 2)
    assert summary["平均持有天數"] == round(104 / 3, 2)
    assert summary["中位數持有天數"] == 3.0
    assert summary["勝筆數"] == 1 and summary["敗筆數"] == 1
    assert summary["平手筆數"] == 1
    return {
        "FinMind日檔精簡與_date欄位": "PASS",
        "均線位置與排列": "PASS",
        "剛站上MA20": "PASS",
        "剛跌破MA10": "PASS",
        "不足歷史Missing": "PASS",
        "事件去重複": "PASS",
        "ABCDE分類": "PASS",
        "FIFO已出清": "PASS",
        "FIFO未出清": "PASS",
        "勝率": "PASS",
        "平均與中位持有天數": "PASS",
        "完整統計管線": "PASS",
    }


def rebuild_statistics(
    state: dict[str, Any], expected_dates: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    print("【階段二】只讀 compact_daily，重建事件、FIFO、均線與統計...")
    compact_frames, events = load_compact_frames(state)
    sales = build_sale_rows(compact_frames)
    events = simulate_group_outcomes_fifo(events, sales)
    last_day = max(state["successful_dates"], default="")
    atomic_write_parquet(
        checkpoint_frame(events, last_day, "fifo-complete"),
        RESULTS_DIR / "fifo_checkpoint.parquet",
    )
    dates = trading_dates()
    events = attach_ma_to_events(
        events, dates, statistics_day=max(state["successful_dates"], default="")
    )
    events_frame = events_to_frame(events)
    condition_stats = build_condition_statistics(events_frame)
    position_stats = build_position_statistics(events_frame)
    profiles = build_broker_profiles(
        events_frame, condition_stats, position_stats
    )
    validate_final_results(events_frame, condition_stats, position_stats)
    integrity = build_integrity_report(state, expected_dates, events_frame)
    return events_frame, condition_stats, position_stats, profiles, integrity


def main() -> int:
    ensure_directories()
    if "--self-test" in sys.argv:
        results = run_self_tests()
        for name, result in results.items():
            print(f"{name}: {result}")
        return 0
    print(
        "FinMind 完整歷史權證分點均線勝率統計\n"
        f"程式版本：{PROGRAM_VERSION}｜schema：{SCHEMA_VERSION}\n"
        f"快取目錄：{CACHE_ROOT}"
    )
    state = load_state()
    audit_successful_files(state)
    expected_dates = discover_expected_dates(state)
    if not FORCE_RECALCULATE_STATS:
        broker_map = build_broker_map()
        if len(broker_map) != len(TARGET_PATTERNS):
            raise RuntimeError(
                f"目標分點對照不完整：{len(broker_map)}/{len(TARGET_PATTERNS)}"
            )
        identity_index = build_warrant_identity_index()
        download_and_compact_history(
            state, expected_dates, identity_index, broker_map
        )
    elif not state["successful_dates"]:
        raise RuntimeError(
            "FORCE_RECALCULATE_STATS=1，但本機沒有任何成功 compact_daily"
        )
    (
        events,
        condition_stats,
        position_stats,
        profiles,
        integrity,
    ) = rebuild_statistics(state, expected_dates)
    output_path = write_outputs(
        events, condition_stats, position_stats, profiles, integrity
    )
    state["last_statistics_time"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    print(
        "\n✅ 完成\n"
        f"  事件：{len(events):,}\n"
        f"  均線條件統計：{len(condition_stats):,}\n"
        f"  MA位置統計：{len(position_stats):,}\n"
        f"  Excel：{output_path}"
    )
    if state["failed_dates"]:
        print(
            "  ⚠️ 歷史資料尚未完整；失敗日期："
            + "、".join(state["failed_dates"])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
