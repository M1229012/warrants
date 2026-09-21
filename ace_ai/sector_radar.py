"""盤中族群雷達（轉強／轉弱），規格見 SECTOR_RADAR_SPEC.md。

做法：盤中每 5 分鐘存一張「類股即時漲跌」快照，查詢時只讀本地快照算 Δ30m（ppt），
比較的是**同一天盤中對盤中**，不拿昨天的收盤湊數。名次只當輔助小字。

- 資料來源：證交所 MIS 即時行情，一個請求就拿到全部類股指數＋加權＋櫃買（官方、免金鑰）。
- 候選：Δ 在全類股前／後 10% 且 |Δ| ≥ 0.20 ppt（兩者同時滿足）。
- 目前只到 L1（指數層級），卡片標「僅指數層級，未驗證個股」；L2 代表股驗證之後接上。
- 抓不到即時資料就說沒有，不用昨天的資料頂替。
- 這條序列只收 MIS 官方類股指數；CMoney 概念族群屬另一套 universe，不得混入（規格全域規則 1）。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import warrant_ai_tools as tools
import local_market_cache
from official_sector_members import AGGREGATE_IDS

SNAPSHOT_MINUTES = max(2, tools._env_int("DISCORD_AI_RADAR_SNAPSHOT_MINUTES", 5))
_TICK_LOCK = threading.Lock()     # 背景 tick 與查詢時補抓不要同時各存一張
# 總和型（電子工業、化學生技醫療）會和子類股重複計算，「其他」「綜合」沒有分析意義。
# 主要依 sector_id（AGGREGATE_IDS）排除；名稱只給沒有 sector_id 的舊快照用。
EXCLUDE_NAMES = {"其他", "其他電子", "綜合", "電子工業", "電子", "化學生技醫療"}
# 證交所 MIS 即時行情：一個請求就能拿到全部類股指數＋加權＋櫃買，官方來源、免金鑰。
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "application/json",
               "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
# t35～t38＝綠能環保／數位雲端／運動休閒／居家生活；MIS 沒有的 channel 只會不回傳，不影響其他類股。
MIS_CHANNELS = ["tse_t%02d.tw" % i for i in range(1, 39)]
MIS_BENCHMARKS = {"t00.tw": "加權指數", "o00.tw": "櫃買指數"}
MIS_TIMEOUT = max(3.0, tools._env_float("DISCORD_AI_RADAR_TIMEOUT", 8.0))
_STATE_PREFIX = "radar_snap:"


def fetch_sector_quotes() -> Dict[str, Any]:
    """證交所 MIS 即時類股指數：一次請求拿完，回傳 {rows, benchmarks, time}。"""
    channels = "|".join(MIS_CHANNELS + ["tse_t00.tw", "otc_o00.tw"])
    started = time.perf_counter()
    status = 0
    try:
        try:
            session = tools.core().get_thread_session()
        except Exception:   # 主程式還沒載入時（離線測試）就用一般連線
            import requests
            session = requests
        response = session.get(MIS_URL, params={"ex_ch": channels, "json": "1", "delay": "0"},
                               headers=MIS_HEADERS, timeout=(4, MIS_TIMEOUT))
        status = int(response.status_code)
        response.raise_for_status()
        payload = response.json() or {}
    finally:
        try:
            tools.record_api_event("TWSE-MIS", status=status, latency=time.perf_counter() - started)
        except Exception:
            pass
    rows, benchmarks, stamp = [], {}, ""
    market_turnover: Optional[float] = None
    for item in payload.get("msgArray") or []:
        channel, name = str(item.get("ch") or ""), str(item.get("n") or "")
        if not channel or not name:
            continue
        if channel == "t00.tw":
            _log_t00_payload(item)
            market_turnover = _market_turnover(item)
        try:
            close = float(item.get("z") or item.get("o") or 0)
            previous = float(item.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0 or previous <= 0:
            continue
        pct = round((close / previous - 1) * 100, 2)
        stamp = str(item.get("t") or stamp)[:5]
        label = name.replace("類指數", "").replace("指數", "")
        if channel in MIS_BENCHMARKS:
            benchmarks[MIS_BENCHMARKS[channel]] = pct
            continue
        if label in EXCLUDE_NAMES or channel.split(".")[0] in AGGREGATE_IDS:
            continue
        # sector_id＝MIS channel（如 t24），給 OfficialSectorMemberResolver 對照成員用，不靠名稱
        rows.append({"sector_id": channel.split(".")[0], "name": label, "change_pct": pct})
    return {"rows": rows, "benchmarks": benchmarks, "time": stamp, "market_turnover": market_turnover}


# 上市全市場累計成交金額（成交占比的分母）：欄位與單位必須先看過 MIS 原始 payload 再用環境變數指定，
# 程式不猜欄位。未設定＝market_turnover unavailable，成交占比／熱度一律顯示「—」。
MARKET_TURNOVER_FIELD = os.getenv("DISCORD_AI_RADAR_MARKET_TURNOVER_FIELD", "").strip()
MARKET_TURNOVER_UNIT = tools._env_float("DISCORD_AI_RADAR_MARKET_TURNOVER_UNIT", 1.0)   # 乘上後要是「元」
_T00_LOGGED = [""]


def _log_t00_payload(item: Dict[str, Any]) -> None:
    """每天印一次 tse_t00.tw 原始欄位，供確認哪個欄位是累計成交金額、單位為何。"""
    day = _now().strftime("%Y-%m-%d")
    if _T00_LOGGED[0] == day:
        return
    _T00_LOGGED[0] = day
    print("🧪 MIS tse_t00.tw 原始欄位｜" + json.dumps(item, ensure_ascii=False), flush=True)


def _market_turnover(item: Dict[str, Any]) -> Optional[float]:
    if not MARKET_TURNOVER_FIELD:
        return None
    try:
        value = float(str(item.get(MARKET_TURNOVER_FIELD) or "").replace(",", "")) * MARKET_TURNOVER_UNIT
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _now():
    return tools.taipei_now()


def _minutes(now=None) -> int:
    now = now or _now()
    return now.hour * 60 + now.minute


def session_open(now=None) -> bool:
    now = now or _now()
    return now.weekday() < 5 and 9 * 60 <= _minutes(now) <= 13 * 60 + 35


def ready_for_query(now=None) -> bool:
    """盤中 09:00 起即可查詢；能否下「明顯轉強／轉弱」由 phase() 控制（v1.1）。"""
    return session_open(now)


def _key(day: str) -> str:
    return _STATE_PREFIX + day


def _load_day(day: str) -> List[Dict[str, Any]]:
    data = local_market_cache.get_state(_key(day), []) or []
    return data if isinstance(data, list) else []


def tick(force: bool = False) -> Dict[str, Any]:
    """存一張快照；還沒到間隔時間就跳過。回傳這次的狀態摘要。"""
    with _TICK_LOCK:
        return _tick(force)


def _tick(force: bool) -> Dict[str, Any]:
    now = _now()
    if not force and not session_open(now):
        return {"saved": False, "reason": "非盤中"}
    day = now.strftime("%Y-%m-%d")
    snaps = _load_day(day)
    if snaps and not force:
        last = snaps[-1].get("time", "")
        try:
            last_minutes = int(last[:2]) * 60 + int(last[3:5])
        except (ValueError, IndexError):
            last_minutes = -999
        if _minutes(now) - last_minutes < SNAPSHOT_MINUTES:
            return {"saved": False, "reason": "間隔未到"}
    rows: List[Dict[str, Any]] = []
    benchmarks: Dict[str, Any] = {}
    market_turnover = None
    try:
        quotes = fetch_sector_quotes()
        rows = list(quotes.get("rows") or [])
        benchmarks = dict(quotes.get("benchmarks") or {})
        market_turnover = quotes.get("market_turnover")
    except Exception as exc:
        print(f"⚠️ 官方類股指數取得失敗，本輪不存快照｜{type(exc).__name__}", flush=True)
    if not rows:   # 不用 CMoney 補：概念族群不能替代官方類股指數（兩套 universe 不混用）
        return {"saved": False, "reason": "官方類股指數取得失敗"}
    rows.sort(key=lambda r: -float(r.get("change_pct") or 0.0))
    snapshot = {
        "time": now.strftime("%H:%M"),
        "benchmarks": benchmarks,
        "market_turnover": market_turnover,
        "rows": [{"sector_id": str(r.get("sector_id") or ""), "name": str(r.get("name") or ""), "rank": index,
                  "change_pct": round(float(r.get("change_pct") or 0.0), 2)}
                 for index, r in enumerate(rows, 1)],
    }
    snaps.append(snapshot)
    # 保留當日第一張（Δ 的退路基準），其餘只留最近 60 張；重啟後仍讀得到。
    kept = snaps[-60:]
    if snaps and snaps[0] not in kept:
        kept = [snaps[0]] + kept[1:]
    local_market_cache.set_state(_key(day), kept)
    if market_turnover:
        # 分母可用時才做全類股成員掃描並存同時點成交占比歷史（成交熱度的基準）；
        # 同一 5 分鐘桶的報價查詢時直接共用，不會再打一次。
        try:
            save_turnover_history(day, _bucket_of(snapshot["time"]), market_turnover)
        except Exception as exc:
            print(f"⚠️ 族群成交占比歷史略過｜{type(exc).__name__}: {exc}", flush=True)
    else:
        _log_once("turnover_tick", "📡 sector radar｜market_turnover unavailable（未設定或欄位解析失敗），不存成交占比歷史")
    return {"saved": True, "time": snapshot["time"], "groups": len(snapshot["rows"])}



# ============================================================
# L1：Δ30m、開盤模式、候選族群（規格書第 2 章、v1.1 第 4 點）
# ============================================================

DELTA_MINUTES = max(10, tools._env_int("DISCORD_AI_RADAR_DELTA_MINUTES", 30))
DELTA_TOLERANCE = max(3, tools._env_int("DISCORD_AI_RADAR_DELTA_TOLERANCE", 8))
DELTA_MIN_PPT = max(0.05, tools._env_float("DISCORD_AI_RADAR_DELTA_MIN_PPT", 0.20))
DELTA_PERCENTILE = min(0.4, max(0.02, tools._env_float("DISCORD_AI_RADAR_DELTA_PCT", 0.10)))
CANDIDATE_LIMIT = max(1, tools._env_int("DISCORD_AI_RADAR_CANDIDATES", 3))
BASE_SNAPSHOT_CUTOFF = 9 * 60 + 5     # 基準快照晚於這個時間就不能說「開盤以來」


def _minutes_of(stamp: str) -> Optional[int]:
    try:
        return int(str(stamp)[:2]) * 60 + int(str(stamp)[3:5])
    except (ValueError, IndexError, TypeError):
        return None


def phase(now=None) -> Dict[str, Any]:
    """開盤模式：09:00–09:14 只能觀察、09:15–09:29 初步、09:30 起正式。"""
    now = now or _now()
    minutes = _minutes(now)
    if not session_open(now):
        return {"phase": "closed", "label": "非盤中", "allow_strong": False, "prefix": ""}
    if minutes < 9 * 60 + 15:
        return {"phase": "opening", "label": "開盤觀察", "allow_strong": False, "prefix": "開盤觀察"}
    if minutes < 9 * 60 + 30:
        return {"phase": "early", "label": "初步", "allow_strong": False, "prefix": "初步"}
    return {"phase": "normal", "label": "正式", "allow_strong": True, "prefix": ""}


def _baseline(snaps: List[Dict[str, Any]], latest_minutes: int) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """取 Δ 的基準快照 → (快照, 文案, basis_kind)。

    時間窗以「最新快照時間」為準（不是現在時間），MIS 中途斷線時 Δ 仍是真正的 30 分鐘。
    優先「最接近 latest−30 分鐘、誤差 ≤8 分鐘」且早於 latest 的快照；找不到才退回當日第一張。
    basis_kind："30m" / "open"（首張 ≤09:05）/ "first_observation"。
    """
    if not snaps:
        return None, "", ""
    target = latest_minutes - DELTA_MINUTES
    best, best_gap = None, None
    for snap in snaps:
        stamp = _minutes_of(snap.get("time", ""))
        if stamp is None or stamp >= latest_minutes:
            continue
        gap = abs(stamp - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = snap, gap
    if best is not None and best_gap is not None and best_gap <= DELTA_TOLERANCE:
        return best, f"近{DELTA_MINUTES}分", "30m"
    first = snaps[0]
    first_minutes = _minutes_of(first.get("time", ""))
    if first_minutes is not None and first_minutes <= BASE_SNAPSHOT_CUTOFF:
        return first, "開盤以來", "open"
    return first, f"自首次觀測（{first.get('time', '')}）以來", "first_observation"


def deltas(now=None) -> Dict[str, Any]:
    """L1 主結果：每個類股的現在漲幅、基準漲幅、Δ30m、名次與分位數。"""
    now = now or _now()
    day = now.strftime("%Y-%m-%d")
    snaps = _load_day(day)
    if not snaps:
        reason = ("今天還沒有盤中快照，約 5 分鐘後再問一次。" if session_open(now)
                  else "現在不是盤中（09:00～13:35），今天也沒有盤中快照可比較。")
        return {"available": False, "reason": reason, "phase": phase(now)}
    if len(snaps) < 2:   # 只有一張時不自己比自己，免得假裝有「開盤以來 Δ=0」
        return {"available": False, "reason": "已記錄今日第一張快照，等待下一張快照後才能計算變化",
                "snapshots": len(snaps), "phase": phase(now)}
    latest = snaps[-1]
    latest_minutes = _minutes_of(latest.get("time", ""))
    if latest_minutes is None:
        return {"available": False, "reason": "最新快照時間格式錯誤", "phase": phase(now)}
    base, basis_label, basis_kind = _baseline(snaps[:-1], latest_minutes)
    base_minutes = _minutes_of((base or {}).get("time", ""))
    actual_minutes = latest_minutes - base_minutes if base_minutes is not None else None
    base_map = _rank_map(base or {})
    rows: List[Dict[str, Any]] = []
    for row in _rank_map(latest).values():
        name = row.get("name")
        before = base_map.get(name) or {}
        change = float(row.get("change_pct") or 0.0)
        base_change = float(before.get("change_pct")) if before.get("change_pct") is not None else None
        rows.append({
            "sector_id": row.get("sector_id") or "",      # 舊快照沒有這欄，L2 會跳過
            "name": name,
            "change_pct": change,
            "base_change_pct": base_change,
            "delta_ppt": round(change - base_change, 2) if base_change is not None else None,
            "rank": int(row.get("rank") or 0),
            "rank_before": int(before.get("rank") or 0) or None,
        })
    graded = [r for r in rows if r["delta_ppt"] is not None]
    graded.sort(key=lambda r: -r["delta_ppt"])
    total = len(graded)
    for index, row in enumerate(graded):
        row["delta_percentile"] = round((index + 1) / total * 100, 1) if total else None
    return {
        "available": True, "time": latest.get("time", ""), "basis_label": basis_label,
        "basis_kind": basis_kind, "actual_delta_minutes": actual_minutes,
        "market_turnover": latest.get("market_turnover"),
        "base_time": (base or {}).get("time", ""), "snapshots": len(snaps),
        "phase": phase(now), "groups": total, "rows": graded,
        "delta_minutes": DELTA_MINUTES, "min_ppt": DELTA_MIN_PPT,
        "percentile_cut": round(DELTA_PERCENTILE * 100, 1),
    }


def candidates(direction: str = "up", limit: int = CANDIDATE_LIMIT, now=None,
               data: Optional[Dict[str, Any]] = None, rows: Optional[List[Dict[str, Any]]] = None,
               use_percentile: bool = True) -> Dict[str, Any]:
    """轉強／轉弱候選：Δ 分位數 ＋ 最低絕對變化量，兩者同時滿足。

    data＝已算好的 deltas()；rows＝只在這一組族群內比較（v1.3 規模分組）；
    use_percentile=False＝小型族群只看 ±0.20 ppt 絕對門檻（組內家數太少，10% 分位數沒有統計意義）。
    """
    data = data if data is not None else deltas(now)
    if not data.get("available"):
        return {"available": False, "reason": data.get("reason"), "phase": data.get("phase")}
    rows = sorted(rows if rows is not None else data["rows"], key=lambda r: -r["delta_ppt"])
    total = len(rows)
    cut = max(1, int(round(total * DELTA_PERCENTILE))) if use_percentile else total
    if direction == "down":
        pool = rows[-cut:] if cut else []
        picked = [r for r in pool if (r["delta_ppt"] or 0) <= -DELTA_MIN_PPT]
        picked.sort(key=lambda r: r["delta_ppt"])
    else:
        pool = rows[:cut]
        picked = [r for r in pool if (r["delta_ppt"] or 0) >= DELTA_MIN_PPT]
        picked.sort(key=lambda r: -r["delta_ppt"])
    return {
        "available": True, "direction": direction, "time": data["time"],
        "basis_label": data["basis_label"], "basis_kind": data["basis_kind"],
        "actual_delta_minutes": data["actual_delta_minutes"], "base_time": data["base_time"],
        "phase": data["phase"],
        "groups": total, "cut_size": cut, "min_ppt": DELTA_MIN_PPT,
        "candidates": picked[:limit],
        "reason": "" if picked else (
            f"目前沒有族群同時滿足前後 {data['percentile_cut']}% 與 {DELTA_MIN_PPT} ppt 門檻" if use_percentile
            else f"目前沒有族群近期變化達 ±{DELTA_MIN_PPT} ppt"),
    }


def ensure_snapshot() -> Dict[str, Any]:
    """查詢時順手補一張快照（只在盤中）：今天還沒有任何快照就立刻抓，其餘照原本的間隔。"""
    now = _now()
    if not session_open(now):
        return {"saved": False, "reason": "非盤中"}
    return tick(force=not _load_day(now.strftime("%Y-%m-%d")))


def _rank_map(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """排除總和型指數後依漲幅重新排名（舊快照可能還存著化學生技醫療，名次要重算才不會錯位）。"""
    kept = [dict(row) for row in snapshot.get("rows") or []
            if row.get("name") not in EXCLUDE_NAMES and row.get("sector_id") not in AGGREGATE_IDS]
    kept.sort(key=lambda r: -float(r.get("change_pct") or 0.0))
    for index, row in enumerate(kept, 1):
        row["rank"] = index
    return {row["name"]: row for row in kept}


# ============================================================
# 意圖判斷：先判斷是不是「雷達」，不是才交給族群名稱解析
# ============================================================

_RADAR_WORD_RE = re.compile(r"轉強|轉弱|資金流向|雷達|強勢|弱勢")
_RADAR_SCOPE_RE = re.compile(r"族群|類股|產業|資金流向|雷達|TOP|排行|排名", re.IGNORECASE)
# 拿掉這些通用字後還有殘字（例如「半導體」「記憶體」），就代表在問特定族群，不是雷達。
_RADAR_GENERIC_RE = re.compile(
    r"有哪些|哪幾個|哪一個|哪些|哪個|什麼|有沒有|目前|現在|今天|今日|盤中|正在|開始|主要|大型|小型|"
    r"族群|類股|產業|轉強|轉弱|強勢|弱勢|資金流向|資金|流向|雷達|比較|列出|一下|看看|看|查|"
    r"TOP\d*|前[一二三四五1-5]名?|排行|排名|近30分鐘?|近三十分鐘?|近期|最近|"
    r"的|是|嗎|呢|了|在|有|誰|和|與|跟|及|、|[?？!！。,，\s]", re.IGNORECASE)


def detect_intent(text: str) -> Optional[Dict[str, str]]:
    """「哪些族群正在轉強」→ {direction: up}；「記憶體族群有誰」→ None（交給族群解析）。"""
    value = str(text or "").strip()
    if not _RADAR_WORD_RE.search(value) or not _RADAR_SCOPE_RE.search(value):
        return None
    if _RADAR_GENERIC_RE.sub("", value):
        return None
    up, down = "轉強" in value, "轉弱" in value
    # v1.3：「小型族群雷達」只看小型組；「主要／大型族群雷達」只看主要組；其餘兩組都出
    scope = "small" if "小型" in value else ("main" if re.search(r"主要|大型", value) else "all")
    # 單獨問「目前弱勢／強勢」＝現在跌（漲）最多，不是「近30分轉弱（轉強）」，只回那一張
    view = "all"
    if not (up or down):
        if "弱勢" in value and "強勢" not in value:
            view = "weak"
        elif "強勢" in value and "弱勢" not in value:
            view = "strong"
    return {"direction": "up" if up and not down else "down" if down and not up else "both",
            "scope": scope, "view": view}


# ============================================================
# 成員層（v1.2）：擴散度、集中度、主要帶動股、成交占比／熱度、領漲領跌
# 成員＝OfficialSectorMemberResolver（twse_official 優先）→ MIS 批次報價（全部成員，不篩流動性）。
# 一個 MIS 請求帶 50 檔，不佔富果額度；每批驗 requested／returned，缺的小批重試一次。
# ============================================================

LEADER_TOP = 5                      # v1.4：每族群領漲／領跌 TOP5（不硬湊反方向）
MEMBER_WORKERS = 4                  # MIS 批次並行數（循序打 13 批要 10 秒以上）
MEMBER_BATCH = 50                   # 每個 MIS 請求帶幾檔
MEMBER_RETRY_BATCH = 10             # 缺漏代號的重試批量
MEMBER_DEADLINE = 12.0              # 整次成員報價的時間上限（秒），不能拖住 Discord 回覆
MIN_COVERAGE = 0.80                 # 報價完整率低於此值不下判讀
SMALL_SECTOR = 10                   # 成分股少於此數，卡片註明檔數
BROAD_MIN, SPLIT_MIN, GAP_MIN = 0.60, 0.50, 0.80
HEAT_HOT, HEAT_COLD = 1.2, 1.0
HEAT_MIN_DAYS, HEAT_MAX_DAYS = 5, 20
TURNOVER_KEEP_DAYS = 45             # 日曆天；成交占比歷史保留
_TURNOVER_PREFIX = "radar_turnover:"
_QUOTE_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}   # {5 分鐘桶: {代號: 報價}}
_QUOTE_LOCK = threading.Lock()
_ONCE: Dict[str, str] = {}


def _log_once(key: str, text: str) -> None:
    day = _now().strftime("%Y-%m-%d")
    if _ONCE.get(key) != day:
        _ONCE[key] = day
        print(text, flush=True)


def _bucket_of(stamp: str) -> str:
    minutes = _minutes_of(stamp)
    return "%02d%02d" % divmod(minutes // 5 * 5, 60) if minutes is not None else ""


def _quote_bucket(now=None) -> str:
    now = now or _now()
    return now.strftime("%Y%m%d") + ("close" if not session_open(now) else _bucket_of(now.strftime("%H:%M")))


def _mis_price(item: Dict[str, Any]) -> float:
    """z＝最新成交；盤中瞬間沒有成交時是 "-"，退回最佳買價第一檔。"""
    for raw in (item.get("z"), str(item.get("b") or "").split("_")[0]):
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 0.0


def _num_field(item: Dict[str, Any], key: str) -> float:
    try:
        return float(str(item.get(key) or "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _thread_session():
    try:
        return tools.core().get_thread_session()      # 每條執行緒各自的連線
    except Exception:
        import requests
        return requests


_HTTP_LOCK = threading.Lock()
_HTTP_SENT = [0]                     # 成員報價實際送出的 HTTP 次數（全域、thread-safe）


def _fetch_batch_scoped(request_id: str, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """工作執行緒沒有主執行緒的 thread-local request_id，要顯式帶入，這一題的 API 用量才不會少計。"""
    with tools.api_request_scope(request_id):
        return _fetch_batch(None, codes)


def _fetch_batch(session, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    session = session or _thread_session()               # 並行時傳 None，各執行緒用自己的 session
    with _HTTP_LOCK:
        _HTTP_SENT[0] += 1
    began, status, items = time.perf_counter(), 0, []
    try:
        response = session.get(MIS_URL, params={"ex_ch": "|".join(f"tse_{c}.tw" for c in codes),
                                                "json": "1", "delay": "0"},
                               headers=MIS_HEADERS, timeout=(4, MIS_TIMEOUT))
        status = int(response.status_code)
        response.raise_for_status()
        items = (response.json() or {}).get("msgArray") or []
    except Exception as exc:
        print(f"⚠️ MIS 成員報價批次失敗｜{len(codes)} 檔｜{type(exc).__name__}", flush=True)
    finally:
        try:
            tools.record_api_event("TWSE-MIS", status=status, latency=time.perf_counter() - began)
        except Exception:
            pass
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        code = str(item.get("c") or "")
        price, previous = _mis_price(item), _num_field(item, "y")
        if code and price > 0 and previous > 0:
            lots = _num_field(item, "v")                  # MIS 累計成交量（張）
            out[code] = {"name": str(item.get("n") or code), "price": price, "prev": previous,
                         "change_pct": round((price / previous - 1) * 100, 2),
                         "value": lots * 1000 * price}     # 成交金額近似值（MIS 沒有逐檔成交金額）
    return out


def _fetch_stock_quotes(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批次報價＋完整率驗證：缺的用小批重試一次，仍缺就記下來（不猜、不補）。"""
    try:
        session = tools.core().get_thread_session()
    except Exception:
        import requests
        session = requests
    out: Dict[str, Dict[str, Any]] = {}
    began = time.perf_counter()
    deadline = began + MEMBER_DEADLINE
    with _HTTP_LOCK:
        sent_before = _HTTP_SENT[0]
    request_id = str(getattr(tools._API_REQUEST_LOCAL, "request_id", "") or "")
    batches = [codes[start:start + MEMBER_BATCH] for start in range(0, len(codes), MEMBER_BATCH)]
    # 有限並行（4 條）＋整體時限；逾時的批次直接放棄，缺的代號交給下面的小批重試
    from concurrent.futures import ThreadPoolExecutor, wait
    pool = ThreadPoolExecutor(max_workers=MEMBER_WORKERS, thread_name_prefix="ace-radar-mis")
    try:
        futures = [pool.submit(_fetch_batch_scoped, request_id, batch) for batch in batches]
        done, pending = wait(futures, timeout=max(1.0, deadline - time.perf_counter()))
        for future in done:
            try:
                out.update(future.result())
            except Exception:
                pass
        if pending:
            print(f"⚠️ MIS 成員報價逾時，{len(pending)} 批略過", flush=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    retry = [c for c in codes if c not in out]
    for start in range(0, len(retry), MEMBER_RETRY_BATCH):
        if time.perf_counter() > deadline:
            break
        out.update(_fetch_batch(session, retry[start:start + MEMBER_RETRY_BATCH]))
    missing = [c for c in codes if c not in out]
    with _HTTP_LOCK:
        sent = _HTTP_SENT[0] - sent_before     # 同時有其他查詢在抓時會一起算進來，屬上限值
    print(f"📡 MIS 成員報價｜requested={len(codes)}｜returned={len(out)}｜missing={len(missing)}"
          f"｜http_calls={sent}（{len(batches)} 批＋重試）｜{time.perf_counter() - began:.1f}s"
          + (f"｜{','.join(missing[:15])}" if missing else ""), flush=True)
    return out


def _quotes_for(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """同一個 5 分鐘桶內共用報價（背景 tick 與查詢都走這裡）；只補抓還沒有的代號。"""
    bucket = _quote_bucket()
    with _QUOTE_LOCK:                                    # 同時查詢時等同一份結果（single-flight）
        for key in [k for k in _QUOTE_CACHE if k != bucket]:
            _QUOTE_CACHE.pop(key, None)
        cached = _QUOTE_CACHE.setdefault(bucket, {})
        missing = [c for c in codes if c not in cached]
        if missing:
            cached.update(_fetch_stock_quotes(missing))
        return {c: cached[c] for c in codes if c in cached}


def _resolver():
    from official_sector_members import OfficialSectorMemberResolver
    return OfficialSectorMemberResolver()


def _all_sector_members() -> Dict[str, List[str]]:
    from official_sector_members import SECTOR_MAP
    resolver = _resolver()
    out = {}
    for sid, entry in SECTOR_MAP.items():
        if entry:
            codes = resolver.resolve(sid).get("member_codes") or []
            if codes:
                out[sid] = list(codes)
    return out


# ------------------------------------------------------------
# 成交占比歷史（同一 5 分鐘桶）：{day: {bucket: {"market": 元, "sectors": {sid: 元}}}}
# ------------------------------------------------------------

def save_turnover_history(day: str, bucket: str, market_turnover: float) -> None:
    """背景 tick：全類股成員掃描一次，存這個時點各族群成交金額（完整率 < 80% 的族群不存）。"""
    members = _all_sector_members()
    quotes = _quotes_for(sorted({c for codes in members.values() for c in codes}))
    sectors = {}
    for sid, codes in members.items():
        priced = [c for c in codes if c in quotes]
        if priced and len(priced) / len(codes) >= MIN_COVERAGE:
            sectors[sid] = round(sum(quotes[c]["value"] for c in priced))
    key = _TURNOVER_PREFIX + day
    data = local_market_cache.get_state(key, {}) or {}
    data[bucket] = {"market": market_turnover, "sectors": sectors}
    local_market_cache.set_state(key, data)
    from datetime import timedelta
    old = (_now() - timedelta(days=TURNOVER_KEEP_DAYS)).strftime("%Y-%m-%d")
    local_market_cache.delete_state(_TURNOVER_PREFIX + old)


def _turnover_history(today: str) -> List[Dict[str, Any]]:
    """今天以前的成交占比歷史（新到舊，最多往回 40 個日曆天）。"""
    from datetime import datetime, timedelta
    base = datetime.strptime(today, "%Y-%m-%d")
    out = []
    for back in range(1, 41):
        data = local_market_cache.get_state(_TURNOVER_PREFIX + (base - timedelta(days=back)).strftime("%Y-%m-%d"))
        if data:
            out.append(data)
    return out


def _turnover_heat(sid: str, bucket: str, share_now: Optional[float],
                   history: List[Dict[str, Any]]) -> Tuple[Optional[float], int]:
    """今日同時點占比 ÷ 過去同一桶占比中位數；有效日 < 5 不給倍數。"""
    if share_now is None:
        return None, 0
    shares = []
    for data in history:
        entry = data.get(bucket) or {}
        market, value = entry.get("market"), (entry.get("sectors") or {}).get(sid)
        if market and value:
            shares.append(value / market)
        if len(shares) >= HEAT_MAX_DAYS:
            break
    if len(shares) < HEAT_MIN_DAYS:
        return None, len(shares)
    median = statistics.median(shares)
    return (round(share_now / median, 2) if median > 0 else None), len(shares)


# ------------------------------------------------------------
# 族群統計與判讀
# ------------------------------------------------------------

def sector_stats(row: Dict[str, Any], codes: List[str], quotes: Dict[str, Dict[str, Any]],
                 liquid: set, shares: Dict[str, float]) -> Dict[str, Any]:
    priced = [(c, quotes[c]) for c in codes if c in quotes]
    if not codes or not priced:
        return {"available": False, "members": len(codes)}
    changes = [q["change_pct"] for _, q in priced]
    median = statistics.median(changes)
    index_change = float(row["change_pct"])
    sign = 1 if index_change >= 0 else -1
    # 主要帶動股：昨收市值 × 漲跌幅（對指數的貢獻）；股數缺才退回成交金額最大的同向股
    contrib = [(shares[c] * q["prev"] * q["change_pct"], q["name"]) for c, q in priced if shares.get(c)]
    driver, driver_basis = "", ""
    if contrib:
        best = max(contrib, key=lambda t: t[0] * sign)
        if best[0] * sign > 0:
            driver, driver_basis = best[1], "contribution"
    if not driver:
        same = [q for _, q in priced if q["change_pct"] * sign > 0]
        if same:
            driver, driver_basis = max(same, key=lambda q: q["value"])["name"], "turnover_fallback"
    ups = sorted((q for c, q in priced if c in liquid and q["change_pct"] > 0),
                 key=lambda q: -q["change_pct"])[:LEADER_TOP]
    downs = sorted((q for c, q in priced if c in liquid and q["change_pct"] < 0),
                   key=lambda q: q["change_pct"])[:LEADER_TOP]
    return {
        "available": True, "members": len(codes), "priced": len(priced),
        "coverage": len(priced) / len(codes),
        "up": sum(ch > 0 for ch in changes), "down": sum(ch < 0 for ch in changes),
        "median": round(median, 2), "gap": round(index_change - median, 2),
        "turnover": sum(q["value"] for _, q in priced),
        "driver": driver, "driver_basis": driver_basis,
        "leaders_up": [{"name": q["name"], "change_pct": q["change_pct"]} for q in ups],
        "leaders_down": [{"name": q["name"], "change_pct": q["change_pct"]} for q in downs],
    }


def judge(stat: Dict[str, Any], change: float, heat: Optional[float], stage: str) -> Tuple[str, str]:
    """回 (判讀, 一句白話)。優先序：集中 > 全面 > 無量 > 廣泛；熱度缺不否決擴散，只是不標「全面」。"""
    if not stat.get("available"):
        return "", ""
    if stat["coverage"] < MIN_COVERAGE:
        return "", f"成分股報價僅 {stat['priced']}/{stat['members']}，不足以判讀"
    if stage == "opening":
        return "", "開盤初期波動大，僅先列入觀察"
    pre = "初步" if stage == "early" else ""
    n, med, gap = stat["priced"], stat["median"], stat["gap"]
    driver = stat.get("driver") or "少數權值股"
    if change > 0:
        breadth = stat["up"] / n
        if gap >= GAP_MIN and breadth < SPLIT_MIN:
            return pre + "集中拉抬", f"指數走強主要由{driver}帶動，多數成分股未跟上"
        if breadth >= BROAD_MIN and med > 0 and heat is not None and heat >= HEAT_HOT:
            return pre + "全面走強", "多數成分股同步走強，且成交熱度升溫"
        if breadth >= SPLIT_MIN and med > 0 and heat is not None and heat < HEAT_COLD:
            return pre + "有價無量", "成分股普遍上漲，但成交熱度未跟上"
        if breadth >= BROAD_MIN and med > 0:
            return pre + "廣泛走強", "多數成分股同步走強" + ("，成交熱度暫無資料" if heat is None else "")
    elif change < 0:
        breadth = stat["down"] / n
        if gap <= -GAP_MIN and breadth < SPLIT_MIN:
            return pre + "集中下殺", f"指數走弱主要由{driver}拖累，多數成分股未同步下跌"
        if breadth >= BROAD_MIN and med < 0 and heat is not None and heat >= HEAT_HOT:
            return pre + "全面走弱", "多數成分股同步走弱，且成交熱度升溫"
        if breadth >= SPLIT_MIN and med < 0 and heat is not None and heat < HEAT_COLD:
            return pre + "無量下跌", "成分股普遍下跌，但成交熱度未放大"
        if breadth >= BROAD_MIN and med < 0:
            return pre + "廣泛走弱", "多數成分股同步走弱" + ("，成交熱度暫無資料" if heat is None else "")
    return "", ""


def enrich(rows: List[Dict[str, Any]], data: Dict[str, Any]) -> bool:
    """替每個族群列補 stat／heat／share；成功取得成員報價回 True。任何失敗只降級，不拋例外。"""
    try:
        from representative_basket import MIN_AVG_LOTS, MIN_AVG_VALUE
        resolver = _resolver()
        liquidity = local_market_cache.liquidity_map(20)
        liquid = {c for c, v in liquidity.items()
                  if float(v.get("avg_value") or 0) >= MIN_AVG_VALUE and float(v.get("avg_lots") or 0) >= MIN_AVG_LOTS}
        try:
            import index_contribution
            shares = {c: float(v.get("shares") or 0) for c, v in index_contribution.component_universe().items()
                      if v.get("market") == "twse"}
        except Exception as exc:
            print(f"⚠️ 發行股數取不到，主要帶動股改用成交金額｜{type(exc).__name__}", flush=True)
            shares = {}
        members: Dict[str, List[str]] = {}
        for row in rows:
            sid = row.get("sector_id") or resolver.id_for_name(row["name"])
            row["sector_id"] = sid
            if sid and sid not in members:
                info = resolver.resolve(sid, row["name"])
                members[sid] = list(info.get("member_codes") or [])
                row["member_source"] = info.get("source", "-")
        quotes = _quotes_for(sorted({c for codes in members.values() for c in codes}))
        market = data.get("market_turnover")
        bucket = _bucket_of(data.get("time", ""))
        history = _turnover_history(_now().strftime("%Y-%m-%d")) if market else []
        for row in rows:
            stat = sector_stats(row, members.get(row["sector_id"], []), quotes, liquid, shares)
            share = stat["turnover"] / market if stat.get("available") and market else None
            heat, days = _turnover_heat(row["sector_id"], bucket, share, history)
            row.update({"stat": stat, "share": share, "heat": heat, "heat_days": days})
            if stat.get("available"):
                print(f"   {row['name']}｜{row['sector_id']}｜member_source={row.get('member_source', '-')}"
                      f"｜coverage={stat['priced']}/{stat['members']}｜上漲 {stat['up']}｜下跌 {stat['down']}"
                      f"｜中位 {stat['median']:+.2f}%｜gap {stat['gap']:+.2f}"
                      f"｜share={'-' if share is None else f'{share * 100:.2f}%'}"
                      f"｜heat={'-' if heat is None else heat}（{days} 日）"
                      f"｜driver={stat['driver'] or '-'}（{stat['driver_basis'] or '-'}）", flush=True)
        if not market:
            _log_once("turnover", "📡 sector radar｜market_turnover unavailable，成交占比／熱度顯示「—」")
        return bool(quotes)
    except Exception as exc:
        print(f"⚠️ 族群成員層略過，只列指數層級｜{type(exc).__name__}: {exc}", flush=True)
        return False


# ============================================================
# L1 輸出（L2 代表股驗證接上前的暫時版：只列指數層級）
# ============================================================

_MODE_NAMES = {"strong": "strongest", "weak": "weakest", "up": "turning_up", "down": "turning_down",
               "moves": "moves"}
RADAR_NOTE = "只比較官方成分股 ≥20 檔的大族群｜領漲／領跌取流動性達標成分股"
L1_ONLY_NOTE = "未取得成分股資料，僅指數層級"
STRONG_TOP = 3
STRONG_POOL = 8          # 強勢區先看類股漲幅前 8 名，剔除集中拉抬後取 3
CONCENTRATED_POOL = 5    # 集中型異動：只從類股漲幅前 5 名挑
CONCENTRATED_MAX = 2

# 規模分組：依官方完整成員檔數（不看報價成功數），整個交易日固定。
# v1.4：初期只看大族群（≥20 檔）；小族群被單一個股左右的程度太大，先不上畫面。
BIG_MIN = 20             # ≥20 檔＝大族群（main）
SMALL_MAX = BIG_MIN - 1  # 其餘為小族群，v1.4 不顯示
TINY_MAX = 2
TINY_SHOW = 2
TIER_NAMES = {"main": "族群雷達", "small": "小型族群"}
_SIZE_PREFIX = "radar_size:"


def size_groups(rows: List[Dict[str, Any]]) -> Dict[str, Tuple[str, int]]:
    """sector_id → (main/small/tiny, 官方成員檔數)。當天第一次算完就存起來，盤中不換組。"""
    day = _now().strftime("%Y-%m-%d")
    key = _SIZE_PREFIX + day
    counts = dict(local_market_cache.get_state(key, {}) or {})
    missing = [r for r in rows if r.get("sector_id") and r["sector_id"] not in counts]
    if missing:
        resolver = _resolver()
        for row in missing:
            count = len(resolver.resolve(row["sector_id"], row["name"]).get("member_codes") or [])
            if count:                                   # 取不到成員的不寫入，下次再試
                counts[row["sector_id"]] = count
        local_market_cache.set_state(key, counts)
    groups = {sid: ("main" if n >= BIG_MIN else "small" if n > TINY_MAX else "tiny", n)
              for sid, n in counts.items()}
    by_group: Dict[str, List[str]] = {}
    for row in rows:
        group = groups.get(row.get("sector_id") or "", ("unknown", 0))
        by_group.setdefault(group[0], []).append(f"{row['name']}({group[1]})")
    _log_once("size_groups", "📡 sector radar size groups｜" + "｜".join(
        f"{g}={len(v)}：{'、'.join(v) if g != 'main' else len(v)}" for g, v in sorted(by_group.items())))
    return groups


def _tier_ranks(rows: List[Dict[str, Any]]) -> None:
    """組內名次（現在／基準），「排名 7 → 5」只跟同一組比。"""
    for key, field in (("tier_rank", "change_pct"), ("tier_rank_before", "base_change_pct")):
        for index, row in enumerate(sorted(rows, key=lambda r: -float(r.get(field) or 0.0)), 1):
            row[key] = index


def _title(section: str, data: Dict[str, Any], tier: str = "main") -> str:
    """強勢＝依目前漲幅；轉強／轉弱＝依 Δ（不是漲幅排行，標題要講清楚）；異動＝依漲跌幅絕對值。"""
    prefix = TIER_NAMES.get(tier, "族群雷達")
    if section == "strong":
        return f"{prefix}｜目前強勢 TOP3"
    if section == "weak":
        return f"{prefix}｜目前弱勢 TOP3"
    if section == "moves":
        return f"{prefix}｜異動 TOP3"
    word = "轉強" if section == "up" else "轉弱"
    stage = data["phase"].get("phase")
    if stage == "opening":
        return f"{prefix}｜開盤觀察・{data['basis_label']}{word}"
    if stage == "early":
        return f"{prefix}｜初步{word}・{data['basis_label']}"
    return f"{prefix}｜{data['basis_label']}{word} TOP3"


def _stamp(data: Dict[str, Any]) -> Tuple[str, bool]:
    """右上角：盤中寫「收盤前會變動」；收盤後改寫「收盤｜最後快照」。"""
    if data["phase"].get("phase") == "closed":
        return f"收盤｜最後快照 {data['time']}", False
    return f"盤中 {data['time']}｜收盤前會變動", True


def _rank_text(row: Dict[str, Any]) -> str:
    """有組內名次就用組內名次（v1.3），沒有才退回全類股名次。"""
    now_rank = row.get("tier_rank") or row["rank"]
    before = row.get("tier_rank_before") if row.get("tier_rank") else row.get("rank_before")
    return f"排名 {before} → {now_rank}" if before else f"排名 {now_rank}"


def _delta_text(row: Dict[str, Any], data: Dict[str, Any]) -> str:
    return f"{data['basis_label']} {row['delta_ppt']:+.2f} ppt"


def _log_l1(section: str, data: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    if not data.get("available"):
        print(f"📡 sector radar L1｜mode={_MODE_NAMES[section]}｜unavailable｜{data.get('reason')}", flush=True)
        return
    print(f"📡 sector radar L1｜mode={_MODE_NAMES[section]}｜snapshot={data['time']}｜base={data['base_time']}"
          f"｜basis_kind={data['basis_kind']}｜actual_delta_minutes={data['actual_delta_minutes']}"
          f"｜phase={data['phase'].get('phase')}｜groups={data['groups']}｜candidates={len(rows)}", flush=True)
    for row in rows:   # 逐筆印出基準與現在漲幅，方便對照證交所歷史快照驗算
        print(f"   {row['name']}｜{data['base_time']} {row['base_change_pct']:+.2f}% → "
              f"{data['time']} {row['change_pct']:+.2f}%｜Δ {row['delta_ppt']:+.2f} ppt｜{_rank_text(row)}",
              flush=True)


def _heat_text(row: Dict[str, Any], stage: str) -> str:
    """有 ≥5 日同時點歷史才給倍數；不足只給占比；分母不可用就「—」。"""
    if row.get("heat") is not None:
        return f"成交熱度{'（初步）' if stage in ('opening', 'early') else ''} {row['heat']:.2f}×"
    if row.get("share") is not None:
        return f"成交占比 {row['share'] * 100:.1f}%"
    return "成交熱度 —"


def _breadth_text(row: Dict[str, Any], section: str, stage: str, with_heat: bool = False) -> str:
    """上漲（或下跌）家數＋中位漲幅；成交熱度 v1.4 延後，預設不顯示。"""
    stat = row.get("stat") or {}
    if not stat.get("available"):
        return ""
    count = (f"下跌 {stat['down']}/{stat['priced']}" if section == "down"
             else f"上漲 {stat['up']}/{stat['priced']}")
    parts = [count, f"中位 {stat['median']:+.2f}%"]
    if with_heat:
        parts.append(_heat_text(row, stage))
    if stat["priced"] < stat["members"]:
        parts.append(f"報價 {stat['priced']}/{stat['members']}")
    return "｜".join(parts)


def _tag(row: Dict[str, Any], section: str, stage: str) -> str:
    """集中度小標籤：集中／廣泛（取代整句判讀與「集中型異動」區塊）。"""
    verdict, sentence = _verdict(row, section, stage)
    if "集中" in verdict:
        return "集中"
    if "廣泛" in verdict or "全面" in verdict:
        return "廣泛"
    if not verdict and sentence.startswith("成分股報價"):
        return "報價不足"
    return ""


def _leaders_text(row: Dict[str, Any], section: str) -> str:
    stat = row.get("stat") or {}
    picks = stat.get("leaders_down" if section == "down" else "leaders_up") or []
    return "・".join(f"{s['name']} {s['change_pct']:+.2f}%" for s in picks)


def _leaders_label(section: str) -> str:
    return "領跌" if section == "down" else "領漲"


def _verdict(row: Dict[str, Any], section: str, stage: str) -> Tuple[str, str]:
    """轉強區只判上漲中的族群、轉弱區只判下跌中的族群，方向相反時不標（免得轉強卡寫「廣泛走弱」）。"""
    change = float(row["change_pct"])
    if (section == "up" and change <= 0) or (section == "down" and change >= 0):
        return "", ""
    return judge(row.get("stat") or {}, change, row.get("heat"), stage)


def _side(row: Dict[str, Any], section: str) -> str:
    """領漲或領跌：轉弱區固定領跌；異動區依族群自己的漲跌方向。"""
    if section in ("down", "weak") or (section == "moves" and float(row["change_pct"]) < 0):
        return "down"
    return "up"


def _card_row(index: int, row: Dict[str, Any], section: str, data: Dict[str, Any],
              tier: str = "main") -> Dict[str, Any]:
    """圖片卡（v1.4）：
    強勢卡　第二行＝上漲 n/N｜中位｜集中／廣泛
    轉強／弱＝近30分 Δ｜上漲（下跌）n/N｜中位｜排名變化｜集中／廣泛（依 Δ 排序，Δ 一定要看得到）
    下面一列領漲／領跌 TOP5。"""
    stage = data["phase"].get("phase")
    side = _side(row, section)
    labels = []
    leaders = _leaders_text(row, side)
    if leaders:
        labels.append((f"{_leaders_label(side)}", side, leaders))
    breadth = _breadth_text(row, side, stage)
    if section in ("up", "down"):
        breadth = "｜".join(x for x in (_delta_text(row, data), breadth) if x)
    return {
        "rank": index, "stock_code": "", "stock_name": row["name"], "market": "",
        "row_kind": "sector_group", "pattern_score": None,
        "change_pct": row["change_pct"],
        "coverage_text": breadth,
        # 名次移動只在轉強／轉弱卡當輔助（組內名次）
        "ratio_text": _rank_text(row) if section in ("up", "down") else "",
        "leader_text": _tag(row, section, stage),
        "extra_labels": labels,
    }


def _concentrated_row(row: Dict[str, Any], label: str = "") -> Dict[str, Any]:
    stat = row.get("stat") or {}
    driver = (stat.get("driver") or "少數權值股")[:5]
    return {"rank": "", "stock_code": "", "stock_name": row["name"], "market": "",
            "row_kind": "sector_group", "pattern_score": None, "change_pct": row["change_pct"],
            "coverage_text": label or f"{driver}主導"}


def _panel(section: str, data: Dict[str, Any], rows: List[Dict[str, Any]], member_ok: bool,
           concentrated: Optional[List[Dict[str, Any]]] = None, tier: str = "main",
           tiny: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    items = [_card_row(index, row, section, data, tier) for index, row in enumerate(rows, 1)]
    note = RADAR_NOTE if member_ok else L1_ONLY_NOTE
    stamp, live = _stamp(data)
    panel = {
        "name": _title(section, data, tier), "mode": "market_momentum", "comparison_date": "",
        "title_suffix": "",                 # 不加「漲幅排行」：轉強／轉弱卡是依 Δ 排序
        "stamp_text": stamp, "stamp_live": live,
        "footer_text": ("股市艾斯  /  盤中漲幅為暫定值，收盤前會變動" if live
                        else "股市艾斯  /  類股指數為當日最後一張盤中快照"),
        "rows": items[:3], "others": [],
        "coverage_note": "", "liquidity_note": note,
        "live_time": str(data.get("time", "")),
    }
    if concentrated:
        panel.update({"others": [_concentrated_row(r) for r in concentrated],
                      "others_title": "集中型異動", "others_heads": ("類股漲幅", "主導")})
    elif tiny:
        panel.update({"others": [_concentrated_row(r, f"成分股 {r.get('member_count', 0)} 檔") for r in tiny],
                      "others_title": "極小型異動（容易受單一成分股影響）", "others_heads": ("類股漲幅", "檔數")})
    return {"sector": panel}


def _is_concentrated(row: Dict[str, Any], stage: str) -> bool:
    return judge(row.get("stat") or {}, float(row["change_pct"]), row.get("heat"), stage)[0].endswith("集中拉抬")


def _plan(data: Dict[str, Any], scope: str, main: List[Dict[str, Any]], small: List[Dict[str, Any]],
          tiny: List[Dict[str, Any]], view: str = "all") -> Tuple[List[Tuple[str, str, Dict[str, Any], str]], List[Dict[str, Any]]]:
    """決定要出哪些區塊 → ([(tier, section, 候選結果, 理由)], 需要成員資料的族群)。

    view＝weak：只回「目前弱勢 TOP3」（現在跌最多，≠ 近30分轉弱）；view＝strong：只回目前強勢。
    """
    if view in ("weak", "strong"):
        ordered = sorted(main, key=lambda r: r["change_pct"] if view == "weak" else -r["change_pct"])[:STRONG_TOP]
        return [("main", view, {"rows": ordered}, "")], ordered
    plan, targets = [], []
    if scope in ("all", "main"):
        by_change = sorted(main, key=lambda r: -r["change_pct"])
        ups = candidates("up", data=data, rows=main)
        downs = candidates("down", data=data, rows=main)
        # v1.4：強勢區直接依類股漲幅取 TOP3，不再剔除集中拉抬（改用「集中」小標籤）
        plan += [("main", "strong", {"rows": by_change[:STRONG_TOP]}, ""),
                 ("main", "up", {"rows": ups["candidates"]}, ups["reason"]),
                 ("main", "down", {"rows": downs["candidates"]}, downs["reason"])]
        targets += by_change[:STRONG_TOP] + ups["candidates"] + downs["candidates"]
    if scope == "small":   # v1.4 不開放（answer 會把 scope 改成 main），保留給之後
        ups = candidates("up", data=data, rows=small, use_percentile=False)
        downs = candidates("down", data=data, rows=small, use_percentile=False)
        strong = sorted(small, key=lambda r: -r["change_pct"])[:STRONG_TOP]
        plan += [("small", "strong", {"rows": strong}, ""),
                 ("small", "up", {"rows": ups["candidates"]}, ups["reason"]),
                 ("small", "down", {"rows": downs["candidates"]}, downs["reason"])]
        targets += strong + ups["candidates"] + downs["candidates"]
    return plan, targets


def answer(direction: str = "both", scope: str = "all", now=None, view: str = "all") -> Dict[str, Any]:
    """Discord 入口：回 {text, panels, title}。

    v1.3 先依規模分組，再在組內比較：
    - all  ：主要族群 強勢／轉強／轉弱 ＋ 小型族群 異動 TOP3（依漲跌幅絕對值）
    - main ：只有主要族群三張
    - small：小型族群 強勢／轉強／轉弱（轉強轉弱只看 ±0.20 ppt，不用分位數）
    主要族群的強勢區剔除集中拉抬（另列「集中型異動」）；小型族群不剔除，直接標出來。
    direction 目前只保留介面。
    """
    # v1.4：初期只看大族群（≥20 檔），「小型／主要族群雷達」都回同一份；問小型／主要時文字先講清楚
    notice = "目前族群雷達暫以官方成分股 ≥20 檔的大族群為主。" if scope in ("small", "main") else ""
    scope = "main"
    title = "族群雷達"
    ensure_snapshot()
    data = deltas(now)
    if not data.get("available"):
        _log_l1("strong", data, [])
        return {"text": str(data.get("reason") or "目前沒有可用的族群雷達資料。"), "panels": [], "title": title}
    stage = data["phase"].get("phase")
    try:
        resolver = _resolver()
        for row in data["rows"]:
            row["sector_id"] = row.get("sector_id") or resolver.id_for_name(row["name"])
        groups = size_groups(data["rows"])
    except Exception as exc:
        print(f"⚠️ 族群規模分組失敗，全部當主要族群｜{type(exc).__name__}: {exc}", flush=True)
        groups = {}
    tiers: Dict[str, List[Dict[str, Any]]] = {"main": [], "small": [], "tiny": []}
    for row in data["rows"]:
        group, count = groups.get(row.get("sector_id") or "", ("main" if not groups else "", 0))
        if group in tiers:
            row.update({"size_group": group, "member_count": count})
            tiers[group].append(row)
    _tier_ranks(tiers["main"])
    _tier_ranks(tiers["small"])
    plan, targets = _plan(data, scope, tiers["main"], tiers["small"], tiers["tiny"], view)
    tiny = sorted(tiers["tiny"], key=lambda r: -abs(r["change_pct"]))[:TINY_SHOW] if scope != "main" else []
    unique = {id(r): r for r in targets + tiny}
    member_ok = enrich(list(unique.values()), data)

    stamp, _ = _stamp(data)
    lines = ([notice] if notice else []) + [f"**{title}｜{stamp}（{data['basis_label']}，基準 {data['base_time']}）**"]
    panel_list: List[Dict[str, Any]] = []
    for index_plan, (tier, section, picked, reason) in enumerate(plan):
        concentrated: List[Dict[str, Any]] = []      # v1.4 不再另列「集中型異動」
        rows = picked["rows"]
        # 極小型族群掛在最後一張小型卡下面（不參與正式排名）
        tiny_here = tiny if tier == "small" and index_plan == len(plan) - 1 else []
        _log_l1(section, dict(data, groups=len(tiers[tier])), rows)
        lines.append(f"【{_title(section, data, tier)}】")
        if not rows and not concentrated and not tiny_here:
            lines.append(reason or "目前沒有符合的族群")
            continue
        for index, row in enumerate(rows, 1):
            side = _side(row, section)
            tail = f"｜{_rank_text(row)}" if section in ("up", "down") else ""
            breadth = _breadth_text(row, "down" if side == "down" else "up", stage)
            leaders = _leaders_text(row, side)
            tag = _tag(row, section, stage)
            tail += f"｜{breadth}" if breadth else ""
            tail += f"｜{tag}" if tag else ""
            tail += f"｜{_leaders_label(side)} {leaders}" if leaders else ""
            lines.append(f"{index}. {row['name']} {row['change_pct']:+.2f}%｜{_delta_text(row, data)}{tail}")
        if concentrated:
            lines.append("集中型異動：" + "；".join(
                f"{r['name']} {r['change_pct']:+.2f}%（{_concentrated_row(r)['coverage_text']}）" for r in concentrated))
        if tiny_here:
            lines.append("極小型異動（容易受單一成分股影響）：" + "；".join(
                f"{r['name']} {r['change_pct']:+.2f}%（成分股 {r.get('member_count', 0)} 檔）" for r in tiny_here))
        panel_list.append(_panel(section, data, rows, member_ok, concentrated, tier, tiny_here))
    lines.append(f"※ {RADAR_NOTE if member_ok else L1_ONLY_NOTE}；盤中變化快，僅供當下觀察，不代表未來表現。")
    return {"text": "\n".join(lines), "panels": panel_list, "title": title}
