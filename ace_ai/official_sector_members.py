"""官方類股指數 ↔ 成員名單 mapping（族群雷達規格 v1.1 第 3 點）。

- 對照固定寫死：MIS 類股 channel → 產業代碼，程式不靠名稱猜；MIS 名稱只拿來做一致性檢查。
- MIS 目前只抓 tse_t01～t31（上市類股指數），所以成員名單一律只取 market == twse，
  index universe 與 sample universe 才會一致。
- 成員來源優先用證交所上市公司基本資料的「產業別」（source=twse_official），和類股指數同一套分類；
  取不到才退回 FinMind 產業別（source=finmind_proxy）。FinMind 同一檔會有多個分類列，
  去重後常只留到非主產業那列（例：半導體只剩 26 檔），所以只當備援。
- 每次解析結果存進 SQLite；來源暫時失敗時沿用上次成功的版本（標 stale）。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Any, Dict, Optional

import warrant_ai_tools as tools
import local_market_cache
import sector_match

MAP_VERSION = "2026-09-21"      # 下面這張對照表改動時要一起改，basket 會跟著失效重算
MARKET = "twse"
_STATE_PREFIX = "sector_map:"

# MIS channel → (產業代碼, MIS 類股名稱可能的寫法)。
# None 代表總和型／無分析意義的指數，不提供成員（雷達端本來就排除或只列指數）。
SECTOR_MAP: Dict[str, Optional[tuple]] = {
    "t01": ("01", ("水泥",)), "t02": ("02", ("食品",)), "t03": ("03", ("塑膠",)),
    "t04": ("04", ("紡織纖維", "紡織")), "t05": ("05", ("電機機械", "電機")),
    "t06": ("06", ("電器電纜",)), "t07": None,            # 化學生技醫療：總和型
    "t08": ("08", ("玻璃陶瓷", "玻璃")), "t09": ("09", ("造紙",)),
    "t10": ("10", ("鋼鐵",)), "t11": ("11", ("橡膠",)), "t12": ("12", ("汽車",)),
    "t13": None,                                          # 電子工業：總和型
    "t14": ("14", ("建材營造", "營建")), "t15": ("15", ("航運",)),
    "t16": ("16", ("觀光餐旅", "觀光事業", "觀光")), "t17": ("17", ("金融保險", "金融")),
    "t18": ("18", ("貿易百貨",)), "t19": None, "t20": None,   # 綜合、其他
    "t21": ("21", ("化學",)), "t22": ("22", ("生技醫療",)), "t23": ("23", ("油電燃氣",)),
    "t24": ("24", ("半導體",)), "t25": ("25", ("電腦及週邊設備", "電腦週邊")),
    "t26": ("26", ("光電",)), "t27": ("27", ("通信網路",)), "t28": ("28", ("電子零組件",)),
    "t29": ("29", ("電子通路",)), "t30": ("30", ("資訊服務",)), "t31": None,   # 其他電子
    "t35": ("35", ("綠能環保",)), "t36": ("36", ("數位雲端",)),
    "t37": ("37", ("運動休閒",)), "t38": ("38", ("居家生活",)),
}
# 總和型／無分析意義：雷達 L1 直接依 sector_id 排除
AGGREGATE_IDS = frozenset(k for k, v in SECTOR_MAP.items() if v is None)
# sector_match 沒收錄的產業名稱在這裡補（FinMind industry_category 的寫法）
_EXTRA_INDUSTRY_NAMES = {"18": ("貿易百貨", "貿易百貨業")}


_REGISTRY_TTL = 12 * 3600
_REGISTRY: Dict[str, Any] = {"at": 0.0, "rows": []}
_REGISTRY_LOCK = threading.Lock()


_REFRESHING = [False]


def _fetch_registry() -> list:
    core = tools.core()
    label = "上市股票基本資料"
    rows, ok, error = core.fetch_openapi_json(
        core.TWSE_STOCK_REGISTRY_OPENAPI_URL, label,
        core._official_stock_registry_cache_name(label), core.WARRANT_STOCK_REGISTRY_STALE_MAX_DAYS)
    if not ok or not rows:
        raise tools.ToolDataError(error or "證交所上市公司基本資料為空")
    rows = [r for r in rows if isinstance(r, dict)]
    with _REGISTRY_LOCK:
        _REGISTRY.update({"at": time.time(), "rows": rows})
    return rows


def _refresh_background() -> None:
    with _REGISTRY_LOCK:
        if _REFRESHING[0]:
            return
        _REFRESHING[0] = True

    def worker() -> None:
        try:
            _fetch_registry()
        except Exception as exc:
            print(f"⚠️ 證交所上市公司基本資料背景更新失敗，沿用舊資料｜{type(exc).__name__}", flush=True)
        finally:
            with _REGISTRY_LOCK:
                _REFRESHING[0] = False

    threading.Thread(target=worker, name="ace-twse-registry", daemon=True).start()


def warm_registry() -> None:
    """開機背景預載；之後查詢一律先讀快取，不在使用者查詢時等官方端點。"""
    _refresh_background()


def _twse_registry() -> list:
    """證交所上市公司基本資料（t187ap03_L）；stale-while-revalidate：
    有資料就立刻回（過 12 小時才在背景更新），完全沒資料時才同步抓一次。"""
    with _REGISTRY_LOCK:
        rows, age = _REGISTRY["rows"], time.time() - _REGISTRY["at"]
    if rows:
        if age >= _REGISTRY_TTL:
            _refresh_background()
        return rows
    return _fetch_registry()


def _industry_names(code: str) -> tuple:
    return tuple(sector_match.OFFICIAL_INDUSTRIES.get(code) or ()) + _EXTRA_INDUSTRY_NAMES.get(code, ())


class OfficialSectorMemberResolver:
    """official_sector_id（MIS channel，如 t24）→ {member_codes, source, version, updated_at}。"""

    def __init__(self, market: str = MARKET):
        if market != MARKET:
            raise ValueError("官方類股雷達目前只有上市類股指數，成員只能取 twse")
        self.market = market

    @staticmethod
    def id_for_name(label: str) -> str:
        """舊快照沒存 sector_id 時用：只接受對照表裡 MIS 名稱的**完全相同**寫法，不做模糊比對。"""
        for sector_id, entry in SECTOR_MAP.items():
            if entry and label in entry[1]:
                return sector_id
        return ""

    def name_matches(self, sector_id: str, mis_name: str) -> bool:
        """MIS 回來的名稱是否符合對照表（防 channel 編號被官方調整時默默對錯）。"""
        entry = SECTOR_MAP.get(sector_id)
        if not entry:
            return False
        label = str(mis_name or "").replace("類指數", "").replace("指數", "").strip()
        expected = set(entry[1]) | set(_industry_names(entry[0]))
        return label in expected or any(len(n) >= 2 and (n in label or label in n) for n in expected)

    def resolve(self, sector_id: str, mis_name: str = "") -> Dict[str, Any]:
        entry = SECTOR_MAP.get(sector_id)
        if entry is None:
            return {"available": False, "sector_id": sector_id, "reason": "總和型或未對照的類股指數，不提供成員"}
        if mis_name and not self.name_matches(sector_id, mis_name):
            print(f"⚠️ 類股 mapping 名稱不一致｜{sector_id}｜MIS={mis_name}｜略過", flush=True)
            return {"available": False, "sector_id": sector_id, "reason": "MIS 類股名稱與對照表不一致"}
        industry = entry[0]
        key = _STATE_PREFIX + sector_id
        try:
            try:
                result = self._from_twse(sector_id, industry)
            except Exception as exc:
                print(f"⚠️ 證交所產業別取不到，改用 FinMind｜{sector_id}｜{type(exc).__name__}", flush=True)
                result = self._from_finmind(sector_id, industry)
            previous = local_market_cache.get_state(key, {}) or {}
            if previous.get("version") != result["version"]:   # 版本有變才寫入與印 log，避免每次查詢洗版
                local_market_cache.set_state(key, result)
                print(f"🗂️ 類股成員｜{sector_id}｜member_source={result['source']}｜"
                      f"{len(result['member_codes'])} 檔｜{result['version']}", flush=True)
            return result
        except Exception as exc:
            stored = local_market_cache.get_state(key, {}) or {}
            if stored.get("member_codes"):
                print(f"⚠️ 類股成員改用上次版本｜{sector_id}｜member_source={stored.get('source')}"
                      f"｜{type(exc).__name__}", flush=True)
                return dict(stored, stale=True)
            return {"available": False, "sector_id": sector_id, "reason": "成員名單取不到"}

    def _from_twse(self, sector_id: str, industry: str) -> Dict[str, Any]:
        """證交所上市公司基本資料的「產業別」代碼＝類股指數的編制分類，index／sample universe 同源。"""
        rows = _twse_registry()
        codes = sorted({str(r.get("公司代號") or "").strip() for r in rows
                        if str(r.get("產業別") or "").strip() == industry})
        codes = [c for c in codes if re.fullmatch(r"[1-9]\d{3}", c)]
        if not codes:
            raise tools.ToolDataError("證交所名冊沒有此產業別的普通股")
        digest = hashlib.sha1((MAP_VERSION + "|" + ",".join(codes)).encode("utf-8")).hexdigest()[:10]
        return {
            "available": True, "sector_id": sector_id, "industry": industry, "market": self.market,
            "member_codes": codes, "source": "twse_official",
            "version": f"{MAP_VERSION}:{digest}",
            "updated_at": time.strftime("%Y-%m-%d"), "resolved_at": time.time(),
        }

    def _from_finmind(self, sector_id: str, industry: str) -> Dict[str, Any]:
        import sector_analysis                    # 延後載入：共用它已快取的 FinMind 名冊
        frame = sector_analysis._finmind_catalog()
        names = set(_industry_names(industry))
        frame = frame[(frame["type"] == self.market)
                      & frame["industry_category"].astype(str).str.strip().isin(names)]
        codes = sorted({str(c) for c in frame["stock_id"]})
        if not codes:
            raise tools.ToolDataError("FinMind 名冊沒有此類股的上市成員")
        digest = hashlib.sha1((MAP_VERSION + "|" + ",".join(codes)).encode("utf-8")).hexdigest()[:10]
        return {
            "available": True, "sector_id": sector_id, "industry": industry, "market": self.market,
            "member_codes": codes, "source": "finmind_proxy",
            "version": f"{MAP_VERSION}:{digest}",
            "updated_at": frame["date"].max().strftime("%Y-%m-%d"), "resolved_at": time.time(),
        }
