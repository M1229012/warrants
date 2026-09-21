"""官方類股指數 ↔ 成員名單 mapping（族群雷達規格 v1.1 第 3 點）。

- 對照固定寫死：MIS 類股 channel → 產業代碼，程式不靠名稱猜；MIS 名稱只拿來做一致性檢查。
- MIS 目前只抓 tse_t01～t31（上市類股指數），所以成員名單一律只取 market == twse，
  index universe 與 sample universe 才會一致。
- 目前成員來源是 FinMind 產業別（source=finmind_proxy），log 會註明，卡片不顯示。
- 每次解析結果存進 SQLite；FinMind 暫時失敗時沿用上次成功的版本（標 stale）。
"""
from __future__ import annotations

import hashlib
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
}
# sector_match 沒收錄的產業名稱在這裡補（FinMind industry_category 的寫法）
_EXTRA_INDUSTRY_NAMES = {"18": ("貿易百貨", "貿易百貨業")}


def _industry_names(code: str) -> tuple:
    return tuple(sector_match.OFFICIAL_INDUSTRIES.get(code) or ()) + _EXTRA_INDUSTRY_NAMES.get(code, ())


class OfficialSectorMemberResolver:
    """official_sector_id（MIS channel，如 t24）→ {member_codes, source, version, updated_at}。"""

    def __init__(self, market: str = MARKET):
        if market != MARKET:
            raise ValueError("官方類股雷達目前只有上市類股指數，成員只能取 twse")
        self.market = market

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
            result = self._from_finmind(sector_id, industry)
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
