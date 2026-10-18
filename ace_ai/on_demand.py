"""背景工作關閉時（DISCORD_AI_MARKET_SYNC_ENABLE=0，測試版省錢用）：被問到才準備資料。

背景開著時（正式版）這裡全部不做事，行為和原本一樣。
- 全市場日K底庫＋型態分數：本週精選排名、族群排行被問到時補到最新收盤再算（底庫每天 2 個官方請求；分數純本地 CPU）。
- 權證分點歷史庫：權證明細被問到時同步一次（warrant_store 自己限制檢查頻率）。
- 上櫃三大法人：法人圖被問到時補缺的天數。
- 族群雷達／盤中量能：盤中快照補不回來 → 被問到就把盤中取樣打開 RADAR_AWAKE_SECONDS 秒，沒人再問就自動關。
- 資料庫每日整理：當天第一個問題（15:30 後）在背景做一次。
"""
import threading
import time
from typing import Any, Callable, Dict, Optional

RADAR_AWAKE_SECONDS = 2 * 3600
_ENABLED = [False]
_RADAR_UNTIL = [0.0]
_MARKET_LOCK = threading.Lock()
_DONE_TODAY: Dict[str, str] = {}          # 工作名稱 → 今天已做過的日期
_BACKGROUND: Dict[str, threading.Thread] = {}


def enable(on: bool) -> None:
    """開機時由主程式設定：背景工作關閉＝按需模式。"""
    _ENABLED[0] = bool(on)


def enabled() -> bool:
    return _ENABLED[0]


def _today() -> str:
    import warrant_ai_tools as tools
    return tools.taipei_now().strftime("%Y-%m-%d")


def _once_per_day(name: str) -> bool:
    """今天第一次呼叫回 True 並記下來；之後回 False。"""
    today = _today()
    if _DONE_TODAY.get(name) == today:
        return False
    _DONE_TODAY[name] = today
    return True


def _run_background(name: str, job: Callable[[], Any]) -> bool:
    """同名工作同時只跑一個；已在跑就不重複開。回傳這次有沒有新開。"""
    running = _BACKGROUND.get(name)
    if running is not None and running.is_alive():
        return False
    thread = threading.Thread(target=job, name=f"on-demand-{name}", daemon=True)
    _BACKGROUND[name] = thread
    thread.start()
    return True


def market_ready(budget_seconds: float, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """全市場日K補到最新收盤、型態分數算完；budget 內做不完就交給背景繼續，回 ready=False。
    回傳 {"ready": bool, "scored": 已算檔數, "pending": 還沒算的檔數, "skipped": 背景開著不用做}。"""
    if not enabled():
        return {"ready": True, "skipped": True}
    import market_data
    import market_scan
    started = time.monotonic()

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    if not _MARKET_LOCK.acquire(timeout=max(0.0, min(5.0, left()))):
        return {"ready": False, "busy": True, **_score_progress()}
    try:
        info = market_data.coverage()
        if _market_stale(info):
            log(f"🗂️ 按需補全市場日K｜目前最新 {info.get('last_day') or '-'}")
            market_data.sync(budget_seconds=max(10.0, left() * 0.6), log=lambda m: None)
        pending = 1
        if left() > 5:
            result = market_scan.score_pending(budget_seconds=max(5.0, left() - 3))
            pending = int(result.get("pending", 1)) if "skipped" not in result else 1
        stale = _market_stale(market_data.coverage())
    finally:
        _MARKET_LOCK.release()
    if pending or stale:
        _run_background("market", lambda: _finish_market(log))
    return {"ready": not pending and not stale, **_score_progress()}


def _finish_market(log: Callable[[str], None]) -> None:
    """排名當下沒算完的分數，背景接著算完（每輪 240 秒，最多 10 輪）。"""
    import market_data
    import market_scan
    with _MARKET_LOCK:
        for _ in range(10):
            if _market_stale(market_data.coverage()):
                market_data.sync(budget_seconds=300, log=lambda m: None)
            result = market_scan.score_pending(budget_seconds=240)
            if "skipped" not in result and not result.get("pending") and not _market_stale(market_data.coverage()):
                log("🗂️ 按需全市場資料準備完成，可以再問一次排名")
                return


def _market_stale(info: Dict[str, Any]) -> bool:
    """底庫天數不足，或最新一天早於應有的最近收盤日（官方行事曆判斷；判斷不出來就當作不用補）。"""
    import market_data
    import warrant_ai_tools as tools
    if int(info.get("days") or 0) < market_data.HISTORY_DAYS:
        return True
    try:
        expected = tools.expected_last_close()
        expected_day = expected.strftime("%Y-%m-%d") if expected is not None and str(expected) != "NaT" else ""
    except Exception:
        expected_day = ""
    return bool(expected_day) and str(info.get("last_day") or "") < expected_day


def _score_progress() -> Dict[str, int]:
    import market_scan
    info = market_scan.status()
    scored = int(info.get("scored_stocks") or 0)
    pending = int((info.get("score_scan") or {}).get("pending") or 0)
    return {"scored": scored, "pending": pending}


def warrant_store_ready(log: Callable[[str], None] = print) -> None:
    """權證明細（剩餘天數、槓桿）用的歷史庫：被問到時同步一次（warrant_store 自己限制檢查頻率）。"""
    if not enabled():
        return
    try:
        import warrant_store
        warrant_store.maybe_sync(log=lambda m: log(f"🗃️ {m}"))
    except Exception as exc:
        log(f"⚠️ 按需權證歷史庫同步略過｜{type(exc).__name__}: {exc}")


def tpex_institutional_ready(budget_seconds: float = 20.0, log: Callable[[str], None] = print) -> None:
    """上櫃法人圖：補缺的天數（一輪最多 40 天，budget 內補到滿或沒有缺為止）。"""
    if not enabled():
        return
    import warrant_ai_tools as tools
    started = time.monotonic()
    try:
        while time.monotonic() - started < budget_seconds:
            if tools.tpex_inst_days_available() >= tools.INST_CHART_DAYS:
                return
            if not tools.sync_tpex_institutional(max_requests=40, log=lambda m: None):
                return
    except Exception as exc:
        log(f"⚠️ 按需上櫃法人補資料略過｜{type(exc).__name__}: {exc}")


def wake_radar() -> None:
    """族群雷達／盤中量能被問到：盤中取樣打開 RADAR_AWAKE_SECONDS 秒（快照補不回來，只能從現在開始拍）。"""
    if enabled():
        _RADAR_UNTIL[0] = time.monotonic() + RADAR_AWAKE_SECONDS


def radar_awake() -> bool:
    """盤中取樣該不該跑：背景開著（正式版）一律跑；按需模式只在被問到後的 2 小時內跑。"""
    return not enabled() or time.monotonic() < _RADAR_UNTIL[0]


def daily_maintenance(log: Callable[[str], None] = print) -> None:
    """資料庫每日整理：15:30 後當天第一個問題在背景做一次（不讓會員等）。"""
    if not enabled():
        return
    import warrant_ai_tools as tools
    now = tools.taipei_now()
    if now.hour * 60 + now.minute < 15 * 60 + 30 or not _once_per_day("db_maintenance"):
        return

    def job() -> None:
        try:
            import local_market_cache
            local_market_cache.daily_maintenance(now.strftime("%Y-%m-%d"))
        except Exception as exc:
            log(f"⚠️ 按需資料庫整理略過｜{type(exc).__name__}: {exc}")

    _run_background("db_maintenance", job)
