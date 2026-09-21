"""盤中族群資金流向雷達（轉強／轉弱）。

做法：盤中每隔幾分鐘存一張「類股即時漲跌排名」快照，查詢時只讀本地快照算名次移動，
比較的是**同一天盤中對盤中**（現在 vs 約 30 分鐘前 vs 今天第一張），不拿昨天的收盤湊數。

- 資料來源：證交所 MIS 即時行情，一個請求就拿到全部類股指數＋加權＋櫃買（官方、免金鑰）。
- 轉強：名次往前；相鄰兩張快照都往前＝「持續」，只動一次＝「剛發動」。轉弱同理。
- 抓不到即時資料就說沒有，不用昨天的資料頂替。
- 這條序列只收 MIS 官方類股指數；CMoney 概念族群屬另一套 universe，不得混入（規格全域規則 1）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import warrant_ai_tools as tools
import local_market_cache

SNAPSHOT_MINUTES = max(2, tools._env_int("DISCORD_AI_RADAR_SNAPSHOT_MINUTES", 5))
COMPARE_MINUTES = max(5, tools._env_int("DISCORD_AI_RADAR_COMPARE_MINUTES", 30))
TOP_N = max(3, tools._env_int("DISCORD_AI_RADAR_TOP", 5))
# 「電子工業」是半導體＋光電＋電腦週邊…等類股的總和，放進排名等於重複計算，也查不到成分股；
# 「其他」「綜合」則沒有分析意義。
EXCLUDE_NAMES = {"其他", "其他電子", "綜合", "電子工業"}
# 證交所 MIS 即時行情：一個請求就能拿到全部類股指數＋加權＋櫃買，官方來源、免金鑰。
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "application/json",
               "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
MIS_CHANNELS = ["tse_t%02d.tw" % i for i in range(1, 32)]
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
    for item in payload.get("msgArray") or []:
        channel, name = str(item.get("ch") or ""), str(item.get("n") or "")
        if not channel or not name:
            continue
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
        if label in EXCLUDE_NAMES:
            continue
        # sector_id＝MIS channel（如 t24），給 OfficialSectorMemberResolver 對照成員用，不靠名稱
        rows.append({"sector_id": channel.split(".")[0], "name": label, "change_pct": pct})
    return {"rows": rows, "benchmarks": benchmarks, "time": stamp}


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
    try:
        quotes = fetch_sector_quotes()
        rows = list(quotes.get("rows") or [])
        benchmarks = dict(quotes.get("benchmarks") or {})
    except Exception as exc:
        print(f"⚠️ 官方類股指數取得失敗，本輪不存快照｜{type(exc).__name__}", flush=True)
    if not rows:   # 不用 CMoney 補：概念族群不能替代官方類股指數（兩套 universe 不混用）
        return {"saved": False, "reason": "官方類股指數取得失敗"}
    rows.sort(key=lambda r: -float(r.get("change_pct") or 0.0))
    snapshot = {
        "time": now.strftime("%H:%M"),
        "benchmarks": benchmarks,
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
        return {"available": False, "reason": "今天還沒有盤中快照", "phase": phase(now)}
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
    for row in latest.get("rows") or []:
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
        "base_time": (base or {}).get("time", ""), "snapshots": len(snaps),
        "phase": phase(now), "groups": total, "rows": graded,
        "delta_minutes": DELTA_MINUTES, "min_ppt": DELTA_MIN_PPT,
        "percentile_cut": round(DELTA_PERCENTILE * 100, 1),
    }


def candidates(direction: str = "up", limit: int = CANDIDATE_LIMIT, now=None) -> Dict[str, Any]:
    """轉強／轉弱候選：Δ 分位數 ＋ 最低絕對變化量，兩者同時滿足。"""
    data = deltas(now)
    if not data.get("available"):
        return {"available": False, "reason": data.get("reason"), "phase": data.get("phase")}
    rows = list(data["rows"])
    total = len(rows)
    cut = max(1, int(round(total * DELTA_PERCENTILE)))
    if direction == "down":
        pool = rows[-cut:]
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
        "reason": "" if picked else f"目前沒有族群同時滿足前後 {data['percentile_cut']}% 與 {DELTA_MIN_PPT} ppt 門檻",
    }


def ensure_snapshot() -> Dict[str, Any]:
    """查詢時順手補一張快照：今天還沒有任何快照就立刻抓一張，其餘照原本的間隔。"""
    return tick(force=not _load_day(_now().strftime("%Y-%m-%d")))


def _rank_map(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["name"]: row for row in snapshot.get("rows") or []}


def report(top: int = TOP_N) -> Dict[str, Any]:
    """回傳今日盤中的轉強／轉弱族群（名次移動）。"""
    now = _now()
    day = now.strftime("%Y-%m-%d")
    snaps = _load_day(day)
    if len(snaps) < 2:
        if session_open(now):
            reason = (f"已經記錄今天第 {len(snaps)} 張盤中快照，要有兩張才能比較名次變化；"
                      f"約 {SNAPSHOT_MINUTES} 分鐘後再問一次就會有結果。")
        else:
            reason = "現在不是盤中（09:00～13:35），沒有即時的類股資金流向資料。"
        return {"available": False, "reason": reason, "snapshots": len(snaps)}
    latest = snaps[-1]
    steps = max(1, round(COMPARE_MINUTES / SNAPSHOT_MINUTES))
    base = snaps[-(steps + 1)] if len(snaps) > steps else snaps[0]
    first = snaps[0]
    now_map, base_map, first_map = _rank_map(latest), _rank_map(base), _rank_map(first)
    previous = _rank_map(snaps[-2])
    total = max(1, len(latest.get("rows") or []))
    changes = [float(r.get("change_pct") or 0.0) for r in latest.get("rows") or []]
    market_median = sorted(changes)[len(changes) // 2] if changes else 0.0

    rows: List[Dict[str, Any]] = []
    for name, row in now_map.items():
        before = base_map.get(name)
        if not before:
            continue
        move = int(before["rank"]) - int(row["rank"])            # 正＝名次往前
        last_move = int(previous.get(name, {}).get("rank", row["rank"])) - int(row["rank"])
        open_move = int(first_map.get(name, {}).get("rank", row["rank"])) - int(row["rank"])
        rows.append({
            "name": name, "rank": int(row["rank"]), "rank_before": int(before["rank"]),
            "move": move, "last_move": last_move, "open_move": open_move,
            "change_pct": float(row.get("change_pct") or 0.0),
            "total_groups": total,
            "continuous": (move > 0 and last_move > 0) or (move < 0 and last_move < 0),
        })

    strong = [r for r in rows if r["move"] > 0 and r["change_pct"] > market_median]
    weak = [r for r in rows if r["move"] < 0 and r["change_pct"] < market_median]
    strong.sort(key=lambda r: (-r["move"], -r["change_pct"]))
    weak.sort(key=lambda r: (r["move"], r["change_pct"]))
    return {
        "available": True, "time": latest.get("time", ""), "base_time": base.get("time", ""),
        "open_time": first.get("time", ""), "groups": total,
        "market_median": round(market_median, 2),
        "strong": strong[:top], "weak": weak[:top],
        "snapshots": len(snaps),
    }


def _panel_rows(rows: List[Dict[str, Any]], rising: bool) -> List[Dict[str, Any]]:
    out = []
    for index, row in enumerate(rows, 1):
        arrow = "↑" if row["move"] > 0 else "↓"
        tag = "持續" if row.get("continuous") else "剛發動"
        out.append({
            "rank": index, "stock_code": "", "stock_name": row["name"], "market": "",
            "row_kind": "sector_group", "pattern_score": None,
            "change_pct": row["change_pct"],
            "coverage_text": f"第{row['rank_before']}名 → 第{row['rank']}名",
            "ratio_text": f"{arrow}{abs(row['move'])} 名・{tag}",
            "leader_text": f"開盤以來 {'↑' if row['open_move'] > 0 else '↓'}{abs(row['open_move'])} 名"
                           if row.get("open_move") else "",
        })
    return out


def panels(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """做成和族群排行一樣的卡片（轉強一張、轉弱一張），圖上不寫資料來源與方法。"""
    result = []
    for rows, title, rising in ((data.get("strong") or [], "族群資金流向｜轉強", True),
                                (data.get("weak") or [], "族群資金流向｜轉弱", False)):
        if not rows:
            continue
        panel_rows = _panel_rows(rows, rising)
        result.append({"sector": {
            "name": title, "mode": "market_momentum", "comparison_date": "",
            "rows": panel_rows[:3], "others": panel_rows[3:5],
            "coverage_note": "", "liquidity_note": f"與 {data.get('base_time', '')} 相比的名次變化",
            "live_time": str(data.get("time", "")),
        }})
    return result


def format_report(data: Dict[str, Any]) -> str:
    if not data.get("available"):
        return str(data.get("reason") or "目前沒有可用的族群資金流向資料。")
    lines = [f"**族群資金流向｜{data.get('time', '')}（與 {data.get('base_time', '')} 相比）**"]
    for label, key in (("轉強", "strong"), ("轉弱", "weak")):
        rows = data.get(key) or []
        lines.append(f"【{label}】" + ("" if rows else "無明顯變化"))
        for index, row in enumerate(rows, 1):
            arrow = "↑" if row["move"] > 0 else "↓"
            tag = "持續" if row.get("continuous") else "剛發動"
            lines.append(f"{index}. {row['name']}｜第{row['rank_before']}名 → 第{row['rank']}名"
                         f"（{arrow}{abs(row['move'])}・{tag}）｜族群漲幅 {row['change_pct']:+.2f}%")
    lines.append("※ 盤中名次會持續變動，僅供當下觀察，不代表未來表現。")
    return "\n".join(lines)
