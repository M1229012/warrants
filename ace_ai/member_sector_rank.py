"""今日族群排行 v1（10-24）：只用 member_sectors.json 的族群。

規格（使用者與外部審核確認）：
1. 盤前固定名單：前 20 個完整交易日平均成交額 ≥ 2,000 萬，或前一交易日成交額 ≥ 2,000 萬
   （只看今天以前的日 K，盤中／收盤後重算名單都一樣）。今天才第一次爆量的股票抓不到，屬已知限制。
2. 族群指標都用同一批「有效報價」：等權平均漲幅、漲幅中位數、上漲家數比例（漲幅 > 0；有成交且持平的算進分母）。
   缺報價、停牌、尚無成交不當成 0%，只計入「缺報價」。
3. 相對強度＝族群等權平均 − 追蹤股票池基準（名單去重、每檔一次的等權平均，只用有效報價）。
4. 可排名：有效報價 ≥ 3 檔，且有效報價 ÷ 通過流動性檔數 ≥ 80%；3～4 檔附註小樣本。
5. 依等權平均漲幅排序；同家族只在顯示時取一個，向後遞補；合格不足三個就只顯示實際數量。
6. 標籤依序判定：強勢 → 漲勢不均 → 一般上漲 → 抗跌 → 偏弱 → 持平。
7. 試行族群：另算「只用 confirmed」版本（同一批報價、同一份名單、同一個基準）寫進 Log，樣本不足就標樣本不足。

報價：允許即時行情且在盤中時用 MIS（sector_radar 5 分鐘共用快取）；其餘用本地日 K 的最新收盤。
會員預設 closed_quotes_only()，所以會員查詢通常是「最近收盤」排行；即時排行只在允許即時行情時出現。
成交額只有「成交價 × 累積成交量」的估計值，對外一律寫「估計」。
已知限制：今天才首次爆量者不在名單；上櫃除權息與減資恢復交易的參考價尚未核對／校正（只處理上市 TWT49U）。
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import local_market_cache
import sector_match
import warrant_ai_tools as tools

LIQUIDITY_DAYS = max(5, tools._env_int("DISCORD_AI_MEMBER_RANK_LIQUIDITY_DAYS", 20))
MIN_VALUE = max(0.0, tools._env_float("DISCORD_AI_MEMBER_RANK_MIN_VALUE", 20_000_000.0))       # 元
UP_RATIO = min(1.0, max(0.0, tools._env_float("DISCORD_AI_MEMBER_RANK_UP_RATIO", 0.60)))
MIN_COVERAGE = min(1.0, max(0.0, tools._env_float("DISCORD_AI_MEMBER_RANK_MIN_COVERAGE", 0.80)))
MIN_VALID = max(1, tools._env_int("DISCORD_AI_MEMBER_RANK_MIN_VALID", 3))
SMALL_MAX = max(MIN_VALID, tools._env_int("DISCORD_AI_MEMBER_RANK_SMALL_MAX", 4))              # ≤ 此數附註小樣本
TOP_N = max(1, tools._env_int("DISCORD_AI_MEMBER_RANK_TOP", 3))
MOVERS = 3
FLAT_EPS = 1e-9

_POOL_LOCK = threading.Lock()
_POOL_CACHE: Dict[str, Any] = {}
_VERIFY_DONE: Dict[str, bool] = {}


# ============================================================
# 名單
# ============================================================

def _today() -> str:
    return tools.taipei_now().strftime("%Y-%m-%d")


def universe() -> List[str]:
    """所有會員族群成分股（去重、去掉 excluded）。"""
    codes = set()
    for name in sector_match.member_groups():
        codes.update(sector_match.member_codes(name))
    return sorted(codes)


def liquid_pool(day: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    """當日固定的流動性名單：{代號: {avg_value, last_value}}；同一天、同一版 JSON 只算一次。"""
    day = day or _today()
    key = f"{day}|{sector_match.member_version()}|{MIN_VALUE:.0f}"
    with _POOL_LOCK:
        if _POOL_CACHE.get("key") == key:
            return _POOL_CACHE["pool"]
    stats = local_market_cache.liquidity_before(day, LIQUIDITY_DAYS)
    pool = {}
    for code in universe():
        row = stats.get(code) or {}
        full = int(row.get("days") or 0) >= LIQUIDITY_DAYS
        avg_ok = full and float(row.get("avg_value") or 0) >= MIN_VALUE
        last_ok = float(row.get("last_value") or 0) >= MIN_VALUE
        if avg_ok or last_ok:
            pool[code] = {"avg_value": float(row.get("avg_value") or 0), "last_value": float(row.get("last_value") or 0)}
    with _POOL_LOCK:
        _POOL_CACHE.update(key=key, pool=pool)
    print(f"📋 今日族群排行名單｜{day}｜成分股 {len(universe())} 檔｜通過流動性 {len(pool)} 檔"
          f"｜門檻 {MIN_VALUE / 1e4:,.0f} 萬（{LIQUIDITY_DAYS} 日均或前一日）｜JSON {sector_match.member_version()}", flush=True)
    return pool


# ============================================================
# 報價
# ============================================================

def _live_allowed() -> bool:
    return (not tools.closed_quotes_only()) and tools.intraday_session_now()


def quotes(codes: List[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """回傳 ({代號: {change_pct, price, est_value}}, 資訊)。只收有成交、有參考價的報價。"""
    if _live_allowed():
        import sector_radar
        raw = sector_radar._quotes_for(codes)
        # 漲跌幅用價格重算（sector_radar 的 change_pct 已四捨五入到 2 位，上漲判斷要用未四捨五入值）
        out = {c: {"change_pct": (float(q["price"]) / float(q["prev"]) - 1) * 100, "price": float(q["price"]),
                   "est_value": float(q.get("value") or 0), "prev": float(q["prev"]), "name": q.get("name", "")}
               for c, q in raw.items() if float(q.get("value") or 0) > 0 and float(q.get("prev") or 0) > 0}
        _verify_reference(out)
        now = tools.taipei_now()
        closed = now.hour * 60 + now.minute >= 13 * 60 + 30
        return out, {"live": not closed, "basis": "收盤" if closed else "盤中",
                     "time": now.strftime("%H:%M"), "date": now.strftime("%Y-%m-%d")}
    changes = local_market_cache.latest_changes(codes)
    latest = max((str(v.get("date") or "") for v in changes.values()), default="")
    out = {c: {"change_pct": float(v["change_pct"]), "price": float(v["close"]),
               "est_value": float(v["close"]) * float(v.get("volume") or 0)}
           for c, v in changes.items()
           if str(v.get("date") or "") == latest and float(v.get("volume") or 0) > 0}
    # 日 K 的漲跌幅是對「未調整昨收」；當天除權息的上市股改用證交所公告的參考價（上櫃尚未涵蓋）
    refs = _reference_prices(latest)
    fixed = [c for c in out if refs.get(c)]
    for c in fixed:
        out[c]["change_pct"] = (out[c]["price"] / refs[c] - 1) * 100
    if fixed:
        print(f"🔎 今日族群排行｜{latest} 除權息 {len(fixed)} 檔改用公告參考價計算漲跌", flush=True)
    return out, {"live": False, "basis": "收盤", "time": "", "date": latest}


_REF_CACHE: Dict[str, Dict[str, float]] = {}


def _reference_prices(day: str) -> Dict[str, float]:
    """同一天只讀一次 TWT49U；讀不到就回空（照原本未調整漲跌，並寫 Log）。"""
    if not day:
        return {}
    if day not in _REF_CACHE:
        try:
            _REF_CACHE[day] = _twse_reference_prices(day)
        except Exception as exc:
            print(f"⚠️ 除權息參考價讀取失敗，{day} 漲跌幅未調整｜{tools.err_text(exc)}", flush=True)
            return {}
    return _REF_CACHE[day]


def _twse_reference_prices(day: str) -> Dict[str, float]:
    """證交所 TWT49U 當日除權息參考價（權、息、權息都收）：盤中拿來核對 MIS，收盤模式拿來校正漲跌幅。
    （corporate_action_sources.parse_twse 只收「權」「權息」做股數調整，不能沿用。）"""
    query = urlencode({"startDate": day.replace("-", ""), "endDate": day.replace("-", ""), "response": "json"})
    request = Request("https://www.twse.com.tw/rwd/zh/exRight/TWT49U?" + query, headers={"User-Agent": "AceResearch/1.0"})
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    fields = payload.get("fields") or []
    if payload.get("stat") != "OK" or "股票代號" not in fields or "除權息參考價" not in fields:
        return {}
    out = {}
    for values in payload.get("data") or []:
        row = dict(zip(fields, values))
        try:
            out[str(row["股票代號"]).strip()] = float(str(row["除權息參考價"]).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return out


def _verify_reference(live: Dict[str, Dict[str, Any]]) -> None:
    """每天一次：當天除權息的上市股，核對 MIS 參考價（y）和證交所公告的除權息參考價是否一致，結果寫 Log。
    上櫃除權息與減資恢復交易的參考價另有來源，v1 尚未核對（已知限制）。"""
    day = _today()
    if _VERIFY_DONE.get(day):
        return
    _VERIFY_DONE[day] = True
    official = _reference_prices(day)
    checked = [(c, live[c]["prev"], ref) for c, ref in official.items() if c in live and live[c].get("prev")]
    bad = [f"{c}:MIS {p:g}／公告 {r:g}" for c, p, r in checked if abs(p - r) > 0.011]
    print(f"🔎 除權息參考價核對｜{day}｜公告 {len(official)} 檔｜可比對 {len(checked)} 檔｜不符 {len(bad)} 檔"
          + (f"｜{'、'.join(bad[:10])}" if bad else ""), flush=True)


# ============================================================
# 計算
# ============================================================

def _stats(codes: List[str], pool: Dict[str, Any], quote: Dict[str, Dict[str, Any]], bench: Optional[float]) -> Dict[str, Any]:
    liquid = [c for c in codes if c in pool]
    valid = [c for c in liquid if c in quote]
    changes = [quote[c]["change_pct"] for c in valid]
    out: Dict[str, Any] = {"members": len(codes), "liquid": len(liquid), "valid": len(valid),
                           "missing": [c for c in liquid if c not in quote]}
    coverage = len(valid) / len(liquid) if liquid else 0.0
    out["coverage"] = coverage
    if len(valid) < MIN_VALID:
        out["status"] = "樣本不足"
        return out
    if coverage < MIN_COVERAGE:
        out["status"] = "資料不足"
        return out
    avg = sum(changes) / len(changes)
    med = statistics.median(changes)
    up = sum(1 for v in changes if v > 0) / len(changes)
    rel = (avg - bench) if bench is not None else None
    out.update(status="ok", avg=avg, median=med, up_ratio=up, rel=rel, small=len(valid) <= SMALL_MAX,
               label=label_of(avg, med, up, rel),
               movers=[{"code": c, "change_pct": quote[c]["change_pct"]}
                       for c in sorted(valid, key=lambda c: (-quote[c]["change_pct"], c))[:MOVERS]],
               est_value=sum(quote[c].get("est_value") or 0 for c in valid),
               avg_value=sum(float(pool[c].get("avg_value") or 0) for c in valid))
    return out


def label_of(avg: float, median: float, up_ratio: float, rel: Optional[float]) -> str:
    """依序判定，符合就停止。基準資料不足（rel=None）時，需要比較基準的標籤改標「基準不足」。"""
    if avg > FLAT_EPS:
        if rel is None and median > 0 and up_ratio >= UP_RATIO:
            return "基準不足"
        if median > 0 and up_ratio >= UP_RATIO and rel is not None and rel > 0:
            return "強勢"
        if up_ratio < UP_RATIO:
            return "漲勢不均"
        return "一般上漲"
    if avg < -FLAT_EPS:
        if rel is None:
            return "基準不足"
        return "抗跌" if rel > 0 else "偏弱"
    return "持平"


def rank(day: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """所有會員族群的今日排行（完整結果）；名單或報價都拿不到時回 None。"""
    groups = sector_match.member_groups()
    if not groups:
        return None
    pool = liquid_pool(day)
    if not pool:
        return None
    began = time.perf_counter()
    quote, info = quotes(sorted(pool))
    if not quote:
        return None
    valid_pool = [c for c in pool if c in quote]
    bench_cov = len(valid_pool) / len(pool)
    bench = (sum(quote[c]["change_pct"] for c in valid_pool) / len(valid_pool)
             if valid_pool and bench_cov >= MIN_COVERAGE else None)
    rows, skipped, trial_logs = [], [], []
    for name, group in groups.items():
        stat = _stats(sector_match.member_codes(name), pool, quote, bench)
        stat.update(name=name, family=str(group.get("family") or name), trial=bool(group.get("trial")))
        if stat["status"] != "ok":
            skipped.append(f"{name}:{stat['status']}({stat['valid']}/{stat['liquid']}/{stat['members']})")
            continue
        if stat["trial"]:
            confirmed = _stats(sector_match.member_codes(name, confirmed_only=True), pool, quote, bench)
            stat["confirmed_only"] = confirmed
            trial_logs.append(f"{name}｜全部 {stat['avg']:+.2f}%（{stat['valid']} 檔）"
                              + (f"｜只算已查證 {confirmed['avg']:+.2f}%（{confirmed['valid']} 檔）"
                                 if confirmed["status"] == "ok" else f"｜只算已查證：{confirmed['status']}（{confirmed['valid']} 檔）"))
        rows.append(stat)
    rows.sort(key=lambda r: (-r["avg"], r["name"]))
    for index, row in enumerate(rows, 1):
        row["raw_rank"] = index
    shown, families = [], set()
    for row in rows:                      # 同家族只在顯示時取一個，向後遞補
        if row["family"] in families:
            continue
        families.add(row["family"])
        shown.append(row)
        if len(shown) >= TOP_N:
            break
    for index, row in enumerate(shown, 1):
        row["rank"] = index
    print(f"📊 今日族群排行｜{info['basis']} {info['date']} {info['time']}｜可排名 {len(rows)}／{len(groups)} 族群"
          f"｜基準 {('%+.2f%%' % bench) if bench is not None else '不足'}（有效 {len(valid_pool)}/{len(pool)}）"
          f"｜{time.perf_counter() - began:.1f}s", flush=True)
    print("📊 今日族群排行前十（含同家族）｜" + "｜".join(
        f"{r['raw_rank']}.{r['name']} {r['avg']:+.2f}% {r['label']}" for r in rows[:10]), flush=True)
    if skipped:
        print(f"📊 今日族群排行未排名 {len(skipped)} 族群（有效/通過流動性/成分）｜" + "、".join(skipped[:40]), flush=True)
    for line in trial_logs:
        print("🧪 試行族群比較｜" + line, flush=True)
    return {"rows": shown, "all_rows": rows, "info": info, "bench": bench, "bench_valid": len(valid_pool),
            "pool": len(pool), "groups": len(groups), "skipped": len(skipped)}


# ============================================================
# 輸出
# ============================================================

def _names() -> Dict[str, str]:
    try:
        return tools.get_stock_name_map() or {}
    except Exception:
        return {}


def _label_text(row: Dict[str, Any]) -> str:
    parts = [row["label"]]
    if row.get("small"):
        parts.append("小樣本")
    if row.get("trial"):
        parts.append("試行")
    return "・".join(parts)


def _movers_text(row: Dict[str, Any], names: Dict[str, str]) -> str:
    return "・".join(f"{names.get(m['code'], m['code'])} {m['change_pct']:+.2f}%" for m in row.get("movers") or [])


def answer() -> Optional[Dict[str, Any]]:
    """「哪個族群最強」：文字＋排行圖卡；資料不足回 None 讓上層說明。"""
    data = rank()
    if not data or not data["rows"]:
        return None
    names = _names()
    info = data["info"]
    when = f"盤中 {info['time']}" if info["live"] else f"{info['date']} 收盤"
    bench = data["bench"]
    lines = [f"**今日族群排行｜{when}**",
             f"追蹤股票池基準 {('%+.2f%%' % bench) if bench is not None else '資料不足'}"
             f"（{data['bench_valid']} 檔有量股票等權平均）"]
    for row in data["rows"]:
        rel = f"｜比基準 {row['rel']:+.2f}" if row.get("rel") is not None else ""
        heat = ""
        if info["live"] and row.get("avg_value"):
            heat = f"｜估計成交額已達日均 {row['est_value'] / row['avg_value'] * 100:.0f}%"
        lines.append(f"{row['rank']}. {row['name']}【{_label_text(row)}】平均 {row['avg']:+.2f}%｜中位 {row['median']:+.2f}%"
                     f"｜上漲 {row['up_ratio'] * 100:.0f}%{rel}｜有效 {row['valid']}／通過流動性 {row['liquid']}／成分 {row['members']} 檔"
                     f"{heat}｜領漲 {_movers_text(row, names)}")
    if any(r.get("trial") for r in data["rows"]):
        lines.append("※ 標「試行」的族群，成分股查證尚未完成。")
    lines.append(f"※ 只計近 {LIQUIDITY_DAYS} 日均成交額或前一日成交額 ≥{MIN_VALUE / 1e4:,.0f} 萬的成分股；"
                 "族群漲幅為等權平均，排名僅供研究與觀察參考，不代表未來表現，亦非買賣建議。")
    return {"text": "\n".join(lines), "calls": 0, "cacheable": False, "panels": [panel(data, names)]}


_LABEL_TONE = {"強勢": "up", "漲勢不均": "up", "一般上漲": "up", "抗跌": "accent", "偏弱": "down"}


def panel(data: Dict[str, Any], names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """沿用既有族群排行卡（sector panel, market_momentum）。"""
    names = names if names is not None else _names()
    info = data["info"]
    rows = [{
        "rank": row["rank"], "stock_code": "", "stock_name": row["name"], "market": "", "row_kind": "sector_group",
        "pattern_score": None, "change_pct": round(row["avg"], 2),
        "coverage_text": f"有效 {row['valid']}／通過流動性 {row['liquid']}／成分 {row['members']} 檔",
        "ratio_text": (f"中位 {row['median']:+.2f}%｜上漲 {row['up_ratio'] * 100:.0f}%"
                       + (f"｜比基準 {row['rel']:+.2f}" if row.get("rel") is not None else "")),
        "leader_text": "",
        "extra_labels": [("狀態", _LABEL_TONE.get(row["label"], "neutral"), _label_text(row))]
                        + ([("領漲股", "accent", _movers_text(row, names))] if row.get("movers") else []),
    } for row in data["rows"]]
    bench = data["bench"]
    return {"sector": {
        "name": "今日族群", "mode": "market_momentum", "comparison_date": info["date"], "rows": rows, "others": [],
        "coverage_note": "",
        "liquidity_note": (("盤中估算｜" if info["live"] else "")
                           + f"等權平均｜基準 {('%+.2f%%' % bench) if bench is not None else '資料不足'}"),
        "live_time": info["time"] if info["live"] else "",
    }}
