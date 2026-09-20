"""盤中族群資金流向雷達（轉強／轉弱）。

做法：盤中每隔幾分鐘把「全族群漲幅排名」存一張快照，查詢時只讀本地快照算名次移動，
所以比較的是**同一天盤中對盤中**（現在 vs 30 分鐘前 vs 今天第一張），不拿昨天的收盤湊數。

- 背景抓取：09:00～13:35 每 SNAPSHOT_MINUTES 分鐘一次，每次 2 個 HTTP 請求、0 次 Gemini。
- 轉強：名次往前；連續兩張快照都往前＝「持續轉強」，只動一次＝「剛發動」。
- 轉弱：名次往後，判斷方式相同。
- 抓不到即時資料就回沒有資料，不用昨天的頂替。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import warrant_ai_tools as tools
import cmoney_sector_catalog as cmoney_catalog
import local_market_cache

SNAPSHOT_MINUTES = max(3, tools._env_int("DISCORD_AI_RADAR_SNAPSHOT_MINUTES", 10))
COMPARE_MINUTES = max(10, tools._env_int("DISCORD_AI_RADAR_COMPARE_MINUTES", 30))
MIN_MEMBERS = max(3, tools._env_int("DISCORD_AI_RADAR_MIN_MEMBERS", 5))
OPEN_GRACE_MINUTES = max(0, tools._env_int("DISCORD_AI_RADAR_OPEN_GRACE", 15))
TOP_N = max(3, tools._env_int("DISCORD_AI_RADAR_TOP", 5))
EXCLUDE_NAMES = {"其他", "傳產-其他"}
_STATE_PREFIX = "radar_snap:"


def _now():
    return tools.taipei_now()


def _minutes(now=None) -> int:
    now = now or _now()
    return now.hour * 60 + now.minute


def session_open(now=None) -> bool:
    now = now or _now()
    return now.weekday() < 5 and 9 * 60 <= _minutes(now) <= 13 * 60 + 35


def ready_for_query(now=None) -> bool:
    """開盤前 15 分鐘雜訊太大，不給排名。"""
    now = now or _now()
    return session_open(now) and _minutes(now) >= 9 * 60 + OPEN_GRACE_MINUTES


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
    try:
        radar = cmoney_catalog.get_live_radar(refresh=True)
    except Exception as exc:
        print(f"⚠️ 族群雷達快照失敗｜{type(exc).__name__}", flush=True)
        return {"saved": False, "reason": "抓取失敗"}
    rows = [r for r in (radar.get("rows") or []) if str(r.get("name") or "") not in EXCLUDE_NAMES]
    if not rows:
        return {"saved": False, "reason": "沒有可用資料"}
    rows.sort(key=lambda r: -float(r.get("change_pct") or 0.0))
    snapshot = {
        "time": now.strftime("%H:%M"),
        "rows": [{"name": str(r.get("name") or ""), "rank": index,
                  "change_pct": round(float(r.get("change_pct") or 0.0), 2)}
                 for index, r in enumerate(rows, 1)],
    }
    snaps.append(snapshot)
    local_market_cache.set_state(_key(day), snaps[-60:])
    return {"saved": True, "time": snapshot["time"], "groups": len(snapshot["rows"])}


def _rank_map(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["name"]: row for row in snapshot.get("rows") or []}


def report(top: int = TOP_N) -> Dict[str, Any]:
    """回傳今日盤中的轉強／轉弱族群（名次移動）。"""
    now = _now()
    day = now.strftime("%Y-%m-%d")
    snaps = _load_day(day)
    if len(snaps) < 2:
        return {"available": False,
                "reason": "今天的盤中快照還不夠（至少要兩張），請等幾分鐘再試。" if session_open(now)
                          else "現在不是盤中，沒有即時的族群資金流向資料。"}
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
