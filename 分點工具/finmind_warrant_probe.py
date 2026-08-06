from __future__ import annotations

import csv
import difflib
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


# ============================================================
# 基本設定
# ============================================================
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
FINMIND_WARRANT_URL = (
    "https://api.finmindtrade.com/api/v4/"
    "taiwan_stock_warrant_trading_daily_report"
)
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"

OUTPUT_ROOT = Path("data/finmind_probe")
STATE_DIR = OUTPUT_ROOT / "states"
COMPLETE_DIR = OUTPUT_ROOT / "complete"
SUMMARY_LOG_PATH = OUTPUT_ROOT / "probe_summary.csv"
BROKER_LOG_PATH = OUTPUT_ROOT / "probe_broker_detail.csv"
RESOLVED_BROKERS_PATH = OUTPUT_ROOT / "resolved_brokers.json"

# 使用者目前追蹤的 38 個分點。
# 若 GitHub Variables 有設定 FINMIND_BROKER_IDS 或 FINMIND_BROKER_NAMES，
# 會優先使用 GitHub Variables，不使用此預設清單。
# FinMind 官方分點名稱與專案慣用名稱不同時，在此建立明確別名。
# configured_name 仍保留原本顯示名稱；lookup_name 僅用於查找 FinMind 代碼。
BROKER_LOOKUP_ALIASES = {
    "群益東大": "群益金鼎-東大",
}

DEFAULT_BROKER_NAMES = [
    "富邦公益",
    "富邦敦南",
    "富邦仁愛",
    "新光",
    "永豐金內湖",
    "永豐金竹北",
    "永豐金竹科",
    "永豐金市政",
    "永豐金信義",
    "華南永昌台中",
    "華南永昌淡水",
    "華南永昌頭份",
    "華南永昌內壢",
    "福邦證券",
    "群益東大",
    "群益金鼎古亭",
    "群益金鼎新竹",
    "元大內湖民權",
    "元大南屯",
    "元大汐止",
    "元大虎尾",
    "元大東港",
    "元大苑裡",
    "元大彰化民生",
    "兆豐板橋",
    "凱基士林",
    "凱基敦北",
    "凱基中山",
    "凱基基隆",
    "凱基台南",
    "國票敦北法人",
    "統一三多",
    "統一土城",
    "統一中壢",
    "第一金中壢",
    "台中銀員林",
    "光和溪湖",
    "台灣企銀建成",
]

SUMMARY_FIELDS = [
    "probe_time",
    "target_date",
    "status",
    "configured_brokers",
    "successful_queries",
    "failed_queries",
    "brokers_with_data",
    "raw_rows",
    "unique_warrants",
    "buy_volume",
    "sell_volume",
    "net_volume",
    "buy_amount",
    "sell_amount",
    "net_amount",
    "snapshot_hash",
    "stable_count",
    "stable_required",
    "history_days",
    "history_reference_days",
    "history_min_days",
    "coverage_threshold",
    "coverage_enforced",
    "baseline_raw_rows",
    "baseline_unique_warrants",
    "baseline_brokers_with_data",
    "raw_rows_coverage",
    "unique_warrants_coverage",
    "brokers_with_data_coverage",
    "overall_coverage",
    "coverage_pass",
    "is_complete",
    "first_data_time",
    "last_changed_time",
    "complete_time",
    "error_summary",
]

BROKER_FIELDS = [
    "probe_time",
    "target_date",
    "securities_trader_id",
    "configured_name",
    "api_name",
    "status",
    "raw_rows",
    "unique_warrants",
    "buy_volume",
    "sell_volume",
    "net_volume",
    "buy_amount",
    "sell_amount",
    "net_amount",
    "snapshot_hash",
    "error",
]


@dataclass(frozen=True)
class BrokerTarget:
    securities_trader_id: str
    configured_name: str
    api_name: str = ""


@dataclass(frozen=True)
class Config:
    token: str
    stable_required: int
    history_reference_days: int
    history_min_days: int
    coverage_threshold: float
    request_interval_seconds: float
    request_timeout_seconds: int
    max_retries: int
    force_probe: bool
    explicit_target_date: str


class FinMindRequestError(RuntimeError):
    pass


# ============================================================
# 共用工具
# ============================================================
def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def split_env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return []
    return [item.strip() for item in re.split(r"[,;\n]+", raw) if item.strip()]


def load_config() -> Config:
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "缺少 FINMIND_TOKEN。請在 GitHub Repository Secrets 建立 FINMIND_TOKEN。"
        )

    stable_required = int(os.getenv("STABLE_PROBES_REQUIRED", "4"))
    if stable_required < 2:
        raise RuntimeError("STABLE_PROBES_REQUIRED 至少必須為 2。")

    history_reference_days = int(os.getenv("HISTORY_REFERENCE_DAYS", "20"))
    if history_reference_days < 1:
        raise RuntimeError("HISTORY_REFERENCE_DAYS 至少必須為 1。")

    history_min_days = int(os.getenv("HISTORY_MIN_DAYS", "5"))
    if history_min_days < 1:
        raise RuntimeError("HISTORY_MIN_DAYS 至少必須為 1。")
    if history_min_days > history_reference_days:
        raise RuntimeError(
            "HISTORY_MIN_DAYS 不可大於 HISTORY_REFERENCE_DAYS。"
        )

    coverage_threshold = float(os.getenv("COVERAGE_THRESHOLD", "0.90"))
    if not 0 < coverage_threshold <= 1:
        raise RuntimeError("COVERAGE_THRESHOLD 必須介於 0 與 1 之間。")

    return Config(
        token=token,
        stable_required=stable_required,
        history_reference_days=history_reference_days,
        history_min_days=history_min_days,
        coverage_threshold=coverage_threshold,
        request_interval_seconds=float(
            os.getenv("REQUEST_INTERVAL_SECONDS", "0.30")
        ),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        force_probe=env_bool("FORCE_PROBE", False),
        explicit_target_date=os.getenv("TARGET_DATE", "").strip(),
    )


def ensure_directories() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    COMPLETE_DIR.mkdir(parents=True, exist_ok=True)


def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def previous_weekday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def determine_target_date(now: datetime, explicit: str = "") -> str:
    """
    自動日期規則：
    - 台北時間平日 17:00 後：查當日。
    - 17:00 前：查上一個平日。
    - 週末：查上一個平日。

    國定休市日若需要手動指定，可從 workflow_dispatch 傳入 target_date。
    """
    if explicit:
        parsed = datetime.strptime(explicit, "%Y-%m-%d").date()
        return parsed.isoformat()

    current_day = now.date()
    if current_day.weekday() < 5 and now.hour >= 17:
        return current_day.isoformat()

    return previous_weekday(current_day).isoformat()


def normalize_broker_name(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("證券股份有限公司", "")
    text = text.replace("證券", "")
    text = re.sub(r"[\s\-－—_()（）·．.]+", "", text)
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dataframe_hash(df: pd.DataFrame) -> str:
    if df.empty:
        return ""

    preferred_columns = [
        "date",
        "securities_trader_id",
        "securities_trader",
        "stock_id",
        "price",
        "buy",
        "sell",
    ]
    columns = [column for column in preferred_columns if column in df.columns]
    normalized = df.loc[:, columns].copy()

    for column in normalized.columns:
        normalized[column] = normalized[column].fillna("").astype(str)

    normalized = normalized.sort_values(
        by=list(normalized.columns),
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)

    return sha256_text(normalized.to_csv(index=False, lineterminator="\n"))


def ensure_csv_schema(path: Path, fields: list[str]) -> None:
    """若欄位有新增，先安全遷移舊 CSV，避免追加資料時欄位錯位。"""
    if not path.exists() or path.stat().st_size == 0:
        return

    try:
        current = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except Exception as exc:
        raise RuntimeError(f"無法讀取既有紀錄檔 {path}: {exc}") from exc

    if list(current.columns) == fields:
        return

    for field in fields:
        if field not in current.columns:
            current[field] = ""

    current = current.reindex(columns=fields, fill_value="")
    temp_path = path.with_suffix(path.suffix + ".schema.tmp")
    current.to_csv(temp_path, index=False, encoding="utf-8-sig")
    temp_path.replace(path)


def append_csv(path: Path, row: dict[str, Any], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_csv_schema(path, fields)
    file_exists = path.exists() and path.stat().st_size > 0

    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def state_path_for(target_date: str) -> Path:
    return STATE_DIR / f"{target_date}.json"


def safe_int_sum(series: pd.Series) -> int:
    if series.empty:
        return 0
    return int(pd.to_numeric(series, errors="coerce").fillna(0).sum())


def safe_float_sum(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return round(float(pd.to_numeric(series, errors="coerce").fillna(0).sum()), 2)


def parse_bool_series(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.strip()
        .str.lower()
        .isin({"1", "true", "yes", "y", "on"})
    )


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def format_ratio(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def load_historical_baseline(
    target_date: str,
    configured_brokers: int,
    reference_days: int,
) -> dict[str, Any]:
    """
    取目前目標日前、已確認完整的交易日。

    每個交易日只保留最後一筆 COMPLETE 紀錄，再取最近 reference_days 日，
    並以中位數作為資料量基準，降低單日極端行情的影響。
    """
    empty = {
        "history_days": 0,
        "baseline_raw_rows": 0.0,
        "baseline_unique_warrants": 0.0,
        "baseline_brokers_with_data": 0.0,
        "history_dates": [],
    }

    if not SUMMARY_LOG_PATH.exists() or SUMMARY_LOG_PATH.stat().st_size == 0:
        return empty

    try:
        history = pd.read_csv(
            SUMMARY_LOG_PATH,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except Exception:
        return empty

    required = {
        "target_date",
        "configured_brokers",
        "successful_queries",
        "failed_queries",
        "brokers_with_data",
        "raw_rows",
        "unique_warrants",
        "is_complete",
    }
    if not required.issubset(history.columns):
        return empty

    history = history.copy()
    history = history.loc[parse_bool_series(history["is_complete"])].copy()
    history = history.loc[history["target_date"].astype(str) < target_date].copy()

    for column in [
        "configured_brokers",
        "successful_queries",
        "failed_queries",
        "brokers_with_data",
        "raw_rows",
        "unique_warrants",
    ]:
        history[column] = pd.to_numeric(history[column], errors="coerce")

    history = history.loc[
        history["configured_brokers"].eq(configured_brokers)
        & history["successful_queries"].eq(configured_brokers)
        & history["failed_queries"].eq(0)
        & history["raw_rows"].gt(0)
        & history["unique_warrants"].gt(0)
        & history["brokers_with_data"].gt(0)
    ].copy()

    if history.empty:
        return empty

    if "probe_time" in history.columns:
        history = history.sort_values(
            ["target_date", "probe_time"],
            kind="stable",
        )
    else:
        history = history.sort_values("target_date", kind="stable")

    history = history.drop_duplicates(subset=["target_date"], keep="last")
    history = history.sort_values("target_date", kind="stable").tail(reference_days)

    if history.empty:
        return empty

    return {
        "history_days": int(history["target_date"].nunique()),
        "baseline_raw_rows": float(history["raw_rows"].median()),
        "baseline_unique_warrants": float(
            history["unique_warrants"].median()
        ),
        "baseline_brokers_with_data": float(
            history["brokers_with_data"].median()
        ),
        "history_dates": history["target_date"].astype(str).tolist(),
    }


def evaluate_coverage(
    current_metrics: dict[str, Any],
    brokers_with_data: int,
    baseline: dict[str, Any],
    threshold: float,
    min_history_days: int,
) -> dict[str, Any]:
    raw_rows_coverage = safe_ratio(
        float(current_metrics["raw_rows"]),
        float(baseline["baseline_raw_rows"]),
    )
    unique_warrants_coverage = safe_ratio(
        float(current_metrics["unique_warrants"]),
        float(baseline["baseline_unique_warrants"]),
    )
    brokers_with_data_coverage = safe_ratio(
        float(brokers_with_data),
        float(baseline["baseline_brokers_with_data"]),
    )

    valid_ratios = [
        value
        for value in [
            raw_rows_coverage,
            unique_warrants_coverage,
            brokers_with_data_coverage,
        ]
        if value is not None
    ]
    overall_coverage = min(valid_ratios) if valid_ratios else None
    coverage_enforced = int(baseline["history_days"]) >= min_history_days

    # 歷史完整日尚不足時，先累積可靠基準；達門檻後才正式要求 90%。
    coverage_pass = bool(
        not coverage_enforced
        or (
            len(valid_ratios) == 3
            and all(value >= threshold for value in valid_ratios)
        )
    )

    return {
        "coverage_enforced": coverage_enforced,
        "raw_rows_coverage": raw_rows_coverage,
        "unique_warrants_coverage": unique_warrants_coverage,
        "brokers_with_data_coverage": brokers_with_data_coverage,
        "overall_coverage": overall_coverage,
        "coverage_pass": coverage_pass,
    }


# ============================================================
# FinMind API
# ============================================================
class FinMindClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
                "User-Agent": "github-finmind-warrant-update-probe/1.0",
            }
        )

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error = ""

        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.config.request_timeout_seconds,
                )

                if response.status_code == 402:
                    raise FinMindRequestError(
                        "FinMind API 使用次數已達上限（HTTP 402）。"
                    )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    wait_seconds = (
                        float(retry_after)
                        if retry_after.replace(".", "", 1).isdigit()
                        else min(2**attempt, 30)
                    )
                    last_error = f"HTTP 429，等待 {wait_seconds:.1f} 秒後重試"
                    time.sleep(wait_seconds)
                    continue

                response.raise_for_status()
                payload = response.json()

                if not isinstance(payload, dict):
                    raise FinMindRequestError("API 回傳格式不是 JSON object。")

                api_status = payload.get("status")
                if api_status not in (None, 200, "200"):
                    raise FinMindRequestError(
                        f"FinMind status={api_status}，msg={payload.get('msg', '')}"
                    )

                if "data" not in payload:
                    raise FinMindRequestError(
                        f"API 回傳缺少 data，msg={payload.get('msg', '')}"
                    )

                return payload

            except (
                requests.RequestException,
                ValueError,
                FinMindRequestError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 20))

        raise FinMindRequestError(last_error or "未知 API 錯誤")

    def get_securities_trader_info(self) -> pd.DataFrame:
        payload = self.get_json(
            FINMIND_DATA_URL,
            {"dataset": "TaiwanSecuritiesTraderInfo"},
        )
        return pd.DataFrame(payload.get("data", []))

    def get_warrant_branch_data(
        self,
        securities_trader_id: str,
        target_date: str,
    ) -> pd.DataFrame:
        payload = self.get_json(
            FINMIND_WARRANT_URL,
            {
                "securities_trader_id": securities_trader_id,
                "date": target_date,
            },
        )
        data = pd.DataFrame(payload.get("data", []))

        if data.empty:
            return pd.DataFrame(
                columns=[
                    "securities_trader",
                    "price",
                    "buy",
                    "sell",
                    "securities_trader_id",
                    "stock_id",
                    "date",
                ]
            )

        if "date" in data.columns:
            data["date"] = data["date"].astype(str)
            data = data.loc[data["date"] == target_date].copy()

        if "securities_trader_id" not in data.columns:
            data["securities_trader_id"] = securities_trader_id

        return data.reset_index(drop=True)


# ============================================================
# 分點設定解析
# ============================================================
def load_cached_brokers() -> list[BrokerTarget]:
    payload = load_json(RESOLVED_BROKERS_PATH, {})
    items = payload.get("brokers", []) if isinstance(payload, dict) else []
    brokers: list[BrokerTarget] = []

    for item in items:
        broker_id = str(item.get("securities_trader_id", "")).strip()
        configured_name = str(item.get("configured_name", "")).strip()
        api_name = str(item.get("api_name", "")).strip()
        if broker_id:
            brokers.append(
                BrokerTarget(
                    securities_trader_id=broker_id,
                    configured_name=configured_name or broker_id,
                    api_name=api_name,
                )
            )

    return brokers


def resolve_brokers(client: FinMindClient) -> list[BrokerTarget]:
    configured_ids = split_env_list("FINMIND_BROKER_IDS")
    configured_names = split_env_list("FINMIND_BROKER_NAMES")

    if configured_ids:
        name_by_index = configured_names if configured_names else []
        brokers = []
        for index, broker_id in enumerate(configured_ids):
            name = (
                name_by_index[index]
                if index < len(name_by_index)
                else broker_id
            )
            brokers.append(
                BrokerTarget(
                    securities_trader_id=broker_id,
                    configured_name=name,
                    api_name="",
                )
            )
        return brokers

    target_names = configured_names or DEFAULT_BROKER_NAMES

    cached = load_cached_brokers()
    cached_by_normalized = {
        normalize_broker_name(item.configured_name): item for item in cached
    }
    if target_names and all(
        normalize_broker_name(name) in cached_by_normalized
        for name in target_names
    ):
        return [
            cached_by_normalized[normalize_broker_name(name)]
            for name in target_names
        ]

    info = client.get_securities_trader_info()
    required_columns = {"securities_trader_id", "securities_trader"}
    missing = required_columns - set(info.columns)
    if missing:
        raise RuntimeError(
            "TaiwanSecuritiesTraderInfo 缺少欄位："
            + ", ".join(sorted(missing))
        )

    if "date" in info.columns:
        info = info.sort_values("date", kind="stable").drop_duplicates(
            subset=["securities_trader_id"],
            keep="last",
        )

    records = info.to_dict("records")
    by_normalized: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        normalized = normalize_broker_name(record.get("securities_trader", ""))
        if normalized:
            by_normalized.setdefault(normalized, []).append(record)

    all_normalized_names = list(by_normalized.keys())
    resolved: list[BrokerTarget] = []
    unresolved_messages: list[str] = []

    for configured_name in target_names:
        lookup_name = BROKER_LOOKUP_ALIASES.get(
            configured_name,
            configured_name,
        )
        key = normalize_broker_name(lookup_name)
        matches = by_normalized.get(key, [])

        if len(matches) == 1:
            match = matches[0]
            resolved.append(
                BrokerTarget(
                    securities_trader_id=str(
                        match["securities_trader_id"]
                    ).strip(),
                    configured_name=configured_name,
                    api_name=str(match["securities_trader"]).strip(),
                )
            )
            continue

        if len(matches) > 1:
            options = ", ".join(
                f"{item['securities_trader']}({item['securities_trader_id']})"
                for item in matches
            )
            unresolved_messages.append(
                f"{configured_name}（查找名稱：{lookup_name}）："
                f"匹配到多筆 [{options}]"
            )
            continue

        suggestions = difflib.get_close_matches(
            key,
            all_normalized_names,
            n=5,
            cutoff=0.45,
        )
        suggestion_text = ", ".join(
            f"{by_normalized[item][0]['securities_trader']}"
            f"({by_normalized[item][0]['securities_trader_id']})"
            for item in suggestions
        )
        unresolved_messages.append(
            f"{configured_name}（查找名稱：{lookup_name}）："
            f"找不到；可能為 [{suggestion_text}]"
        )

    if unresolved_messages:
        raise RuntimeError(
            "以下分點名稱無法唯一對應 FinMind 券商代碼。"
            "請在 GitHub Variables 直接設定 FINMIND_BROKER_IDS：\n- "
            + "\n- ".join(unresolved_messages)
        )

    save_json(
        RESOLVED_BROKERS_PATH,
        {
            "resolved_at": now_taipei().isoformat(timespec="seconds"),
            "brokers": [
                {
                    "securities_trader_id": item.securities_trader_id,
                    "configured_name": item.configured_name,
                    "api_name": item.api_name,
                }
                for item in resolved
            ],
        },
    )
    return resolved


# ============================================================
# 資料整理與統計
# ============================================================
def prepare_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    for column in ("price", "buy", "sell"):
        if column not in prepared.columns:
            prepared[column] = 0
        prepared[column] = pd.to_numeric(
            prepared[column], errors="coerce"
        ).fillna(0)

    prepared["buy_amount"] = prepared["price"] * prepared["buy"]
    prepared["sell_amount"] = prepared["price"] * prepared["sell"]
    prepared["net_volume"] = prepared["buy"] - prepared["sell"]
    prepared["net_amount"] = (
        prepared["buy_amount"] - prepared["sell_amount"]
    )
    return prepared


def calculate_metrics(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "raw_rows": 0,
            "unique_warrants": 0,
            "buy_volume": 0,
            "sell_volume": 0,
            "net_volume": 0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "net_amount": 0.0,
            "snapshot_hash": "",
        }

    prepared = prepare_numeric_columns(df)
    buy_volume = safe_int_sum(prepared["buy"])
    sell_volume = safe_int_sum(prepared["sell"])
    buy_amount = safe_float_sum(prepared["buy_amount"])
    sell_amount = safe_float_sum(prepared["sell_amount"])

    return {
        "raw_rows": int(len(prepared)),
        "unique_warrants": (
            int(prepared["stock_id"].astype(str).nunique())
            if "stock_id" in prepared.columns
            else 0
        ),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "net_volume": buy_volume - sell_volume,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "net_amount": round(buy_amount - sell_amount, 2),
        "snapshot_hash": dataframe_hash(prepared),
    }


def build_complete_buysell(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    prepared = prepare_numeric_columns(df)
    group_columns = [
        column
        for column in [
            "date",
            "securities_trader_id",
            "securities_trader",
            "stock_id",
        ]
        if column in prepared.columns
    ]

    result = (
        prepared.groupby(group_columns, as_index=False, dropna=False)
        .agg(
            buy_volume=("buy", "sum"),
            sell_volume=("sell", "sum"),
            buy_amount=("buy_amount", "sum"),
            sell_amount=("sell_amount", "sum"),
        )
        .reset_index(drop=True)
    )

    result["net_volume"] = result["buy_volume"] - result["sell_volume"]
    result["net_amount"] = result["buy_amount"] - result["sell_amount"]

    for column in ["buy_amount", "sell_amount", "net_amount"]:
        result[column] = result[column].round(2)

    sort_columns = [
        column
        for column in [
            "securities_trader_id",
            "stock_id",
        ]
        if column in result.columns
    ]
    if sort_columns:
        result = result.sort_values(sort_columns, kind="stable")

    return result.reset_index(drop=True)


# ============================================================
# GitHub Actions 顯示摘要
# ============================================================
def write_github_summary(lines: list[str]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return

    with Path(summary_path).open("a", encoding="utf-8") as file:
        file.write("\n".join(lines))
        file.write("\n")


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    ensure_directories()
    config = load_config()
    probe_time_dt = now_taipei()
    probe_time = probe_time_dt.isoformat(timespec="seconds")
    target_date = determine_target_date(
        probe_time_dt,
        config.explicit_target_date,
    )

    current_state_path = state_path_for(target_date)
    state = load_json(
        current_state_path,
        {
            "target_date": target_date,
            "completed": False,
            "stable_count": 0,
            "last_hash": "",
            "first_data_time": "",
            "last_changed_time": "",
            "complete_time": "",
        },
    )

    if state.get("completed") and not config.force_probe:
        message = (
            f"✅ {target_date} 已在 {state.get('complete_time', '')} "
            "判定完整，本次不再呼叫 FinMind API。"
        )
        print(message)
        write_github_summary(
            [
                "## FinMind 權證分點更新偵測",
                "",
                f"- 目標日期：`{target_date}`",
                "- 狀態：已完成，略過本次 API 呼叫",
                f"- 完成時間：`{state.get('complete_time', '')}`",
            ]
        )
        return 0

    client = FinMindClient(config)
    brokers = resolve_brokers(client)
    if not brokers:
        raise RuntimeError("沒有任何要偵測的券商分點。")

    print(
        f"🔍 FinMind 權證分點偵測｜目標日期 {target_date}｜"
        f"分點 {len(brokers)} 家｜時間 {probe_time}"
    )

    all_frames: list[pd.DataFrame] = []
    broker_rows: list[dict[str, Any]] = []
    error_messages: list[str] = []
    successful_queries = 0

    for index, broker in enumerate(brokers, start=1):
        try:
            frame = client.get_warrant_branch_data(
                securities_trader_id=broker.securities_trader_id,
                target_date=target_date,
            )
            successful_queries += 1

            if not frame.empty:
                frame = frame.copy()
                frame["_configured_name"] = broker.configured_name
                all_frames.append(frame)

            metrics = calculate_metrics(frame)
            api_name = (
                str(frame["securities_trader"].dropna().iloc[0])
                if not frame.empty and "securities_trader" in frame.columns
                else broker.api_name
            )

            broker_row = {
                "probe_time": probe_time,
                "target_date": target_date,
                "securities_trader_id": broker.securities_trader_id,
                "configured_name": broker.configured_name,
                "api_name": api_name,
                "status": "OK" if not frame.empty else "NO_DATA",
                **metrics,
                "error": "",
            }
            broker_rows.append(broker_row)

            print(
                f"  [{index:02d}/{len(brokers):02d}] "
                f"{broker.configured_name}({broker.securities_trader_id})｜"
                f"{metrics['raw_rows']:,} 列｜"
                f"{metrics['unique_warrants']:,} 檔權證"
            )

        except Exception as exc:  # 每一家分點都要留下失敗紀錄
            error_text = f"{type(exc).__name__}: {exc}"
            error_messages.append(
                f"{broker.configured_name}({broker.securities_trader_id}): "
                f"{error_text}"
            )
            broker_rows.append(
                {
                    "probe_time": probe_time,
                    "target_date": target_date,
                    "securities_trader_id": broker.securities_trader_id,
                    "configured_name": broker.configured_name,
                    "api_name": broker.api_name,
                    "status": "ERROR",
                    "raw_rows": 0,
                    "unique_warrants": 0,
                    "buy_volume": 0,
                    "sell_volume": 0,
                    "net_volume": 0,
                    "buy_amount": 0.0,
                    "sell_amount": 0.0,
                    "net_amount": 0.0,
                    "snapshot_hash": "",
                    "error": error_text,
                }
            )
            print(
                f"  [{index:02d}/{len(brokers):02d}] "
                f"❌ {broker.configured_name}｜{error_text}"
            )

        if (
            config.request_interval_seconds > 0
            and index < len(brokers)
        ):
            time.sleep(config.request_interval_seconds)

    combined = (
        pd.concat(all_frames, ignore_index=True, sort=False)
        if all_frames
        else pd.DataFrame()
    )
    total_metrics = calculate_metrics(combined)
    failed_queries = len(brokers) - successful_queries
    brokers_with_data = sum(
        1 for row in broker_rows if int(row.get("raw_rows", 0)) > 0
    )

    historical_baseline = load_historical_baseline(
        target_date=target_date,
        configured_brokers=len(brokers),
        reference_days=config.history_reference_days,
    )
    coverage = evaluate_coverage(
        current_metrics=total_metrics,
        brokers_with_data=brokers_with_data,
        baseline=historical_baseline,
        threshold=config.coverage_threshold,
        min_history_days=config.history_min_days,
    )

    previous_hash = str(state.get("last_hash", ""))
    current_hash = str(total_metrics["snapshot_hash"])
    has_data = total_metrics["raw_rows"] > 0
    all_queries_succeeded = failed_queries == 0

    if has_data and not state.get("first_data_time"):
        state["first_data_time"] = probe_time

    if all_queries_succeeded and has_data:
        if current_hash and current_hash == previous_hash:
            stable_count = int(state.get("stable_count", 0)) + 1
        else:
            stable_count = 1
            state["last_changed_time"] = probe_time
    else:
        stable_count = 0

    is_complete = bool(
        all_queries_succeeded
        and has_data
        and stable_count >= config.stable_required
        and coverage["coverage_pass"]
    )

    if failed_queries:
        status = "API_ERROR"
    elif not has_data:
        status = "NO_DATA"
    elif is_complete:
        status = "COMPLETE"
    elif (
        coverage["coverage_enforced"]
        and not coverage["coverage_pass"]
    ):
        status = "COVERAGE_TOO_LOW"
    elif stable_count >= 2:
        status = "STABLE_WAITING"
    else:
        status = "DATA_CHANGING"

    if is_complete and not state.get("complete_time"):
        state["complete_time"] = probe_time

    state.update(
        {
            "target_date": target_date,
            "last_probe_time": probe_time,
            "completed": is_complete,
            "stable_count": stable_count,
            "stable_required": config.stable_required,
            "last_hash": current_hash if all_queries_succeeded else previous_hash,
            "last_raw_rows": total_metrics["raw_rows"],
            "last_unique_warrants": total_metrics["unique_warrants"],
            "last_brokers_with_data": brokers_with_data,
            "last_successful_queries": successful_queries,
            "last_failed_queries": failed_queries,
            "configured_brokers": len(brokers),
            "history_days": historical_baseline["history_days"],
            "history_reference_days": config.history_reference_days,
            "history_min_days": config.history_min_days,
            "history_dates": historical_baseline["history_dates"],
            "coverage_threshold": config.coverage_threshold,
            "coverage_enforced": coverage["coverage_enforced"],
            "baseline_raw_rows": historical_baseline["baseline_raw_rows"],
            "baseline_unique_warrants": historical_baseline[
                "baseline_unique_warrants"
            ],
            "baseline_brokers_with_data": historical_baseline[
                "baseline_brokers_with_data"
            ],
            "raw_rows_coverage": coverage["raw_rows_coverage"],
            "unique_warrants_coverage": coverage[
                "unique_warrants_coverage"
            ],
            "brokers_with_data_coverage": coverage[
                "brokers_with_data_coverage"
            ],
            "overall_coverage": coverage["overall_coverage"],
            "coverage_pass": coverage["coverage_pass"],
        }
    )
    save_json(current_state_path, state)

    summary_row = {
        "probe_time": probe_time,
        "target_date": target_date,
        "status": status,
        "configured_brokers": len(brokers),
        "successful_queries": successful_queries,
        "failed_queries": failed_queries,
        "brokers_with_data": brokers_with_data,
        **total_metrics,
        "stable_count": stable_count,
        "stable_required": config.stable_required,
        "history_days": historical_baseline["history_days"],
        "history_reference_days": config.history_reference_days,
        "history_min_days": config.history_min_days,
        "coverage_threshold": config.coverage_threshold,
        "coverage_enforced": coverage["coverage_enforced"],
        "baseline_raw_rows": historical_baseline["baseline_raw_rows"],
        "baseline_unique_warrants": historical_baseline[
            "baseline_unique_warrants"
        ],
        "baseline_brokers_with_data": historical_baseline[
            "baseline_brokers_with_data"
        ],
        "raw_rows_coverage": coverage["raw_rows_coverage"],
        "unique_warrants_coverage": coverage[
            "unique_warrants_coverage"
        ],
        "brokers_with_data_coverage": coverage[
            "brokers_with_data_coverage"
        ],
        "overall_coverage": coverage["overall_coverage"],
        "coverage_pass": coverage["coverage_pass"],
        "is_complete": is_complete,
        "first_data_time": state.get("first_data_time", ""),
        "last_changed_time": state.get("last_changed_time", ""),
        "complete_time": state.get("complete_time", ""),
        "error_summary": " | ".join(error_messages),
    }
    append_csv(SUMMARY_LOG_PATH, summary_row, SUMMARY_FIELDS)

    for broker_row in broker_rows:
        append_csv(BROKER_LOG_PATH, broker_row, BROKER_FIELDS)

    complete_output_path = COMPLETE_DIR / (
        f"{target_date}_warrant_branch_buysell.csv"
    )
    if is_complete:
        complete_df = build_complete_buysell(combined)
        complete_df.to_csv(
            complete_output_path,
            index=False,
            encoding="utf-8-sig",
        )

    print("\n" + "=" * 72)
    print(f"目標日期：{target_date}")
    print(f"本次狀態：{status}")
    print(
        f"查詢成功：{successful_queries}/{len(brokers)}｜"
        f"有資料分點：{brokers_with_data}/{len(brokers)}"
    )
    print(
        f"原始資料量：{total_metrics['raw_rows']:,} 列｜"
        f"不同權證：{total_metrics['unique_warrants']:,} 檔"
    )
    print(
        f"買進量：{total_metrics['buy_volume']:,}｜"
        f"賣出量：{total_metrics['sell_volume']:,}｜"
        f"買賣超：{total_metrics['net_volume']:,}"
    )
    print(
        f"買進金額：{total_metrics['buy_amount']:,.2f}｜"
        f"賣出金額：{total_metrics['sell_amount']:,.2f}｜"
        f"淨買賣超金額：{total_metrics['net_amount']:,.2f}"
    )
    print(
        f"歷史完整日：{historical_baseline['history_days']}/"
        f"{config.history_min_days} 日啟用門檻｜"
        f"參考最近最多 {config.history_reference_days} 日"
    )
    if historical_baseline["history_days"] > 0:
        print(
            "歷史中位數基準："
            f"{historical_baseline['baseline_raw_rows']:,.1f} 列｜"
            f"{historical_baseline['baseline_unique_warrants']:,.1f} 檔權證｜"
            f"{historical_baseline['baseline_brokers_with_data']:,.1f} 家有資料分點"
        )
        print(
            "目前覆蓋率："
            f"列數 {format_ratio(coverage['raw_rows_coverage'])}｜"
            f"權證數 {format_ratio(coverage['unique_warrants_coverage'])}｜"
            f"有資料分點 {format_ratio(coverage['brokers_with_data_coverage'])}｜"
            f"最低值 {format_ratio(coverage['overall_coverage'])}"
        )
    else:
        print("歷史中位數基準：尚無已確認完整的歷史交易日。")

    print(
        f"90% 覆蓋率門檻："
        f"{'已啟用' if coverage['coverage_enforced'] else '尚未啟用'}｜"
        f"門檻 {config.coverage_threshold:.0%}｜"
        f"覆蓋率通過：{'是' if coverage['coverage_pass'] else '否'}"
    )
    print(
        f"穩定次數：{stable_count}/{config.stable_required}｜"
        f"判定完整：{'是' if is_complete else '否'}"
    )

    if is_complete:
        print(f"✅ 完整買賣超已輸出：{complete_output_path}")
    elif failed_queries:
        print("⚠️ 本次有 API 查詢失敗，不會累計穩定次數。")
    elif not has_data:
        print("⏳ 尚未取得目標日期資料，等待下次 10 分鐘探測。")
    elif coverage["coverage_enforced"] and not coverage["coverage_pass"]:
        print(
            "⏳ 資料量尚未達歷史中位數的 90%，"
            "即使內容暫時不變也不會判定完整。"
        )
    elif stable_count < config.stable_required:
        print("⏳ 資料量門檻已通過，等待連續穩定條件完成。")
    else:
        print("⏳ 資料尚未符合完整條件，等待下次探測。")
    print("=" * 72)

    summary_lines = [
        "## FinMind 權證分點更新偵測",
        "",
        f"- 探測時間：`{probe_time}`",
        f"- 目標日期：`{target_date}`",
        f"- 狀態：**{status}**",
        f"- 原始資料量：**{total_metrics['raw_rows']:,} 列**",
        f"- 不同權證：**{total_metrics['unique_warrants']:,} 檔**",
        f"- 成功查詢：**{successful_queries}/{len(brokers)}**",
        f"- 有資料分點：**{brokers_with_data}/{len(brokers)}**",
        f"- 買進量：`{total_metrics['buy_volume']:,}`",
        f"- 賣出量：`{total_metrics['sell_volume']:,}`",
        f"- 淨買賣超：`{total_metrics['net_volume']:,}`",
        f"- 淨買賣超金額：`{total_metrics['net_amount']:,.2f}`",
        (
            f"- 歷史完整日：**{historical_baseline['history_days']}** "
            f"（至少 {config.history_min_days} 日後啟用 90% 門檻）"
        ),
        f"- 90% 門檻：**{'已啟用' if coverage['coverage_enforced'] else '尚未啟用'}**",
        f"- 列數覆蓋率：**{format_ratio(coverage['raw_rows_coverage'])}**",
        f"- 權證數覆蓋率：**{format_ratio(coverage['unique_warrants_coverage'])}**",
        f"- 有資料分點覆蓋率：**{format_ratio(coverage['brokers_with_data_coverage'])}**",
        f"- 最低覆蓋率：**{format_ratio(coverage['overall_coverage'])}**",
        f"- 覆蓋率通過：**{'是' if coverage['coverage_pass'] else '否'}**",
        f"- 穩定次數：**{stable_count}/{config.stable_required}**",
        f"- 判定完整：**{'是' if is_complete else '否'}**",
    ]

    if historical_baseline["history_days"] > 0:
        summary_lines.extend(
            [
                (
                    "- 歷史列數中位數："
                    f"`{historical_baseline['baseline_raw_rows']:,.1f}`"
                ),
                (
                    "- 歷史權證數中位數："
                    f"`{historical_baseline['baseline_unique_warrants']:,.1f}`"
                ),
                (
                    "- 歷史有資料分點中位數："
                    f"`{historical_baseline['baseline_brokers_with_data']:,.1f}`"
                ),
            ]
        )

    write_github_summary(summary_lines)

    # API 短暫失敗已經寫入紀錄，不讓 workflow 中斷，下一個 10 分鐘會再試。
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        message = f"❌ 程式無法執行：{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        write_github_summary(
            [
                "## FinMind 權證分點更新偵測",
                "",
                f"- 狀態：**程式失敗**",
                f"- 錯誤：`{type(exc).__name__}: {exc}`",
            ]
        )
        sys.exit(1)
