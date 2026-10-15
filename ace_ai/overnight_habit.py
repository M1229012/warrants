"""隔日沖（測試版 10-15）：分點慣性看全市場，買超當天就判斷，隔天資料進來再驗證。

只讀本機現股分點資料庫（spot_branch_daily／spot_branch_days／spot_history_bars），不連網；慣性表快取 6 小時。
大量買超＝當天淨買占該股當日成交量 ≥ BIG_SHARE（5%），重押 ≥ HEAVY_SHARE（10%），且 ≥ MIN_LOTS 張（排除極冷門股）；
門檻依 10-15 實測：上榜買超分點中位數 0.75%、P90 4.4%；當日第 1 大買超中位數 6.4%、P75 11.5%。
沒有當日成交量的股票日不列入統計（不拿張數代替）。
隔日反向＝下一個完整交易日同分點淨賣 ≥ 前一天淨買 × REVERSE（隔天沒上榜＝沒有大量賣出）。
慣性分點＝全市場核對 ≥ MIN_CHECKED 次、跨 ≥ MIN_STOCKS 檔、反向比例 ≥ MIN_RATE。
"""
from __future__ import annotations

import os
import threading
import time
from statistics import median

import local_market_cache as store

BIG_SHARE = float(os.getenv("TEST_OVERNIGHT_BIG_SHARE", ".05"))
HEAVY_SHARE = float(os.getenv("TEST_OVERNIGHT_HEAVY_SHARE", ".10"))
MIN_LOTS = float(os.getenv("TEST_OVERNIGHT_MIN_LOTS", "20"))
REVERSE = float(os.getenv("TEST_OVERNIGHT_REVERSE", ".5"))
MIN_CHECKED, MIN_STOCKS, MIN_RATE = 10, 3, 60.0
TTL = 6 * 3600
_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "table": None, "stocks": 0}


def _volumes(conn, code=None):
    """{(股票, 日期): 成交張數}；spot_history_bars 的 volume 是股數。"""
    sql = "SELECT stock_code, date, volume FROM spot_history_bars WHERE confirmed=1 AND volume>0"
    try:
        rows = conn.execute(sql + (" AND stock_code=?" if code else ""), (code,) if code else ()).fetchall()
    except Exception:          # 舊資料庫還沒有價格表
        return {}
    return {(c, d): float(v) / 1000 for c, d, v in rows}


def _load():
    with store._connect() as conn:
        days = conn.execute("SELECT stock_code, date FROM spot_branch_days WHERE status='complete' ORDER BY stock_code, date").fetchall()
        rows = conn.execute("SELECT stock_code, date, branch_name, net FROM spot_branch_daily").fetchall()
        return days, rows, _volumes(conn)


def habits(force=False):
    """{分點: {checked, reverse, rate, heavy_checked, heavy_reverse, stocks, median_reverse_pct}}；只回傳達到慣性門檻的分點。"""
    with _LOCK:
        if not force and _CACHE["table"] is not None and time.time() - _CACHE["at"] < TTL:
            return _CACHE["table"], _CACHE["stocks"]
        days, rows, volume = _load()
        next_day, by_day, prev = {}, {}, {}
        for code, date in days:
            if code in prev:
                next_day[(code, prev[code])] = date
            prev[code] = date
        for code, date, branch, net in rows:
            by_day.setdefault((code, date), {})[branch] = float(net or 0)
        stats = {}
        for (code, date), branches in by_day.items():
            nxt, vol = next_day.get((code, date)), volume.get((code, date))
            if nxt is None or (code, nxt) not in by_day or not vol:
                continue
            for branch, net in branches.items():
                if net < MIN_LOTS or net / vol < BIG_SHARE:
                    continue
                s = stats.setdefault(branch, {"checked": 0, "reverse": 0, "heavy_checked": 0, "heavy_reverse": 0,
                                              "stocks": set(), "pcts": []})
                heavy = net / vol >= HEAVY_SHARE
                reversed_ = -by_day[(code, nxt)].get(branch, 0.0) >= net * REVERSE
                s["checked"] += 1
                s["heavy_checked"] += heavy
                s["stocks"].add(code)
                if reversed_:
                    s["reverse"] += 1
                    s["heavy_reverse"] += heavy
                    s["pcts"].append(round(-by_day[(code, nxt)][branch] / net * 100))
        table = {}
        for branch, s in stats.items():
            rate = s["reverse"] / s["checked"] * 100
            if s["checked"] >= MIN_CHECKED and len(s["stocks"]) >= MIN_STOCKS and rate >= MIN_RATE:
                table[branch] = {"checked": s["checked"], "reverse": s["reverse"], "rate": round(rate, 1),
                                 "heavy_checked": s["heavy_checked"], "heavy_reverse": s["heavy_reverse"],
                                 "stocks": len(s["stocks"]), "median_reverse_pct": median(s["pcts"]) if s["pcts"] else None}
        _CACHE.update(at=time.time(), table=table, stocks=len({c for c, _ in volume}))
        return table, _CACHE["stocks"]


def _tier(share):
    return "重押" if share is not None and share >= HEAVY_SHARE * 100 else "大量" if share is not None and share >= BIG_SHARE * 100 else ""


def check(stock_code, report):
    """最新完整日的主要買盤中，慣性分點佔多少（當天判斷）＋前一日慣性買盤今天有沒有賣（驗證）。"""
    table, universe = habits()
    latest = report.get("latest_complete_date")
    with store._connect() as conn:
        volume = _volumes(conn, stock_code)
    vol = volume.get((stock_code, latest))
    top = [x for x in report.get("latest_top_buy") or [] if float(x.get("net") or 0) > 0]
    total = sum(float(x["net"]) for x in top)
    buyers = []
    for x in top:
        share = round(float(x["net"]) / vol * 100, 2) if vol else None
        buyers.append(dict(branch=x["branch"], net_lots=x["net"], volume_pct=share, tier=_tier(share),
                           habit=table.get(x["branch"])))
    flagged = [b for b in buyers if b["habit"]]
    out = {"data_date": latest, "day_volume_lots": vol, "universe_stocks": universe, "habit_branches_known": len(table),
           "top_buyers": buyers,
           "habit_share_of_top_buy_pct": round(sum(float(b["net_lots"]) for b in flagged) / total * 100, 1) if total else None,
           "habit_share_of_volume_pct": round(sum(float(b["net_lots"]) for b in flagged) / vol * 100, 1) if vol and flagged else None}
    try:
        with store._connect() as conn:
            dates = [d for (d,) in conn.execute(
                "SELECT date FROM spot_branch_days WHERE stock_code=? AND status='complete' AND date<=? ORDER BY date DESC LIMIT 2",
                (stock_code, latest)).fetchall()]
            if len(dates) == 2:
                prev = dict(conn.execute("SELECT branch_name, net FROM spot_branch_daily WHERE stock_code=? AND date=?",
                                         (stock_code, dates[1])).fetchall())
                today = dict(conn.execute("SELECT branch_name, net FROM spot_branch_daily WHERE stock_code=? AND date=?",
                                          (stock_code, dates[0])).fetchall())
                pv = volume.get((stock_code, dates[1]))
                out["verify_previous_day"] = [
                    {"branch": b, "buy_date": dates[1], "buy_lots": n, "volume_pct": round(float(n) / pv * 100, 2),
                     "next_day_net_lots": today.get(b, 0), "reversed": -float(today.get(b, 0)) >= float(n) * REVERSE}
                    for b, n in prev.items() if b in table and pv and float(n) >= MIN_LOTS and float(n) / pv >= BIG_SHARE]
    except Exception:
        pass
    return out
