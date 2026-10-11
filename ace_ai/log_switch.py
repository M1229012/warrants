"""10-11：可開關的 Log 類別。平常關閉以節省 Log，需要時用 /ace 指令（可白話）打開。

錯誤、警告與會員問答紀錄不受影響，永遠會印出。開關存在本機資料庫，重新部署後沿用。
"""
from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional, Tuple

# 類別：(顯示名稱, 預設是否開啟, 說明, 白話關鍵字)
CATEGORIES: Dict[str, Tuple[str, bool, str, Tuple[str, ...]]] = {
    "tri": ("三角核對", False, "每次算三角印出：主型態來源、鎖定日、越線紀錄、上下緣", ("三角", "型態")),
    "usage": ("用量報表", False, "每 5 分鐘的 ACE USAGE（RAM、額度、API 次數）", ("用量", "額度", "資源", "usage")),
    "market": ("底庫進度", False, "市場底庫與型態分數底庫的背景更新進度", ("底庫", "市場", "分數", "掃描")),
    "datawarn": ("休市提示", False, "個股日K缺日（休市／無成交）的提示", ("休市", "缺日", "資料提示")),
}
_STATE_KEY = "log_switch_v1"
_LOCK = threading.Lock()
_CACHE: Optional[Dict[str, bool]] = None


def _load() -> Dict[str, bool]:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            saved = {}
            try:
                import local_market_cache
                saved = local_market_cache.get_state(_STATE_KEY, {}) or {}
            except Exception:
                saved = {}
            _CACHE = {k: bool(saved.get(k, v[1])) for k, v in CATEGORIES.items()}
        return dict(_CACHE)


def enabled(category: str) -> bool:
    return bool(_load().get(category, False))


def set_many(categories: List[str], on: bool) -> Dict[str, bool]:
    global _CACHE
    state = _load()
    for c in categories:
        state[c] = on
    with _LOCK:
        _CACHE = dict(state)
    try:
        import local_market_cache
        local_market_cache.set_state(_STATE_KEY, state)
    except Exception as exc:
        print(f"⚠️ Log 開關存檔失敗（本次仍生效）：{type(exc).__name__}: {exc}", flush=True)
    return state


def status_text() -> str:
    state = _load()
    rows = [f"{'🟢' if state[k] else '⚪'} {v[0]}：{v[2]}" for k, v in CATEGORIES.items()]
    return "Log 開關（錯誤與問答紀錄永遠會印）\n" + "\n".join(rows)


_LOG_WORD = re.compile(r"(?:log|LOG|Log|日誌|紀錄|記錄|訊息)")
_ON = re.compile(r"(?:打開|開啟|啟用|顯示|開)")
_OFF = re.compile(r"(?:關掉|關閉|停用|隱藏|不要|關)")


def parse_command(text: str) -> Optional[Tuple[str, List[str]]]:
    """白話也可以：「開啟三角紀錄」「三角log打開」「關掉用量訊息」「全部log關掉」「log狀態」。
    回傳 (動作, 類別清單)；動作＝on／off／status。不是 Log 指令回傳 None。"""
    s = re.sub(r"\s+", "", str(text or ""))
    if not _LOG_WORD.search(s):
        return None
    if re.search(r"(?:狀態|開關|有哪些|清單)$", s) and not re.search(r"(?:打開|開啟|關掉|關閉)", s):
        return "status", []
    picked = [k for k, v in CATEGORIES.items() if any(w.lower() in s.lower() for w in v[3])]
    if re.search(r"(?:全部|所有|全)", s):
        picked = list(CATEGORIES)
    if not picked:
        return None
    if _OFF.search(s):
        return "off", picked
    if _ON.search(s):
        return "on", picked
    return None
