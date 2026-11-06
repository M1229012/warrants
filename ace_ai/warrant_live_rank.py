"""11-06 使用者（測試版，/testace 管理員）：盤中權證成交（爆量）TOP10——全市場，和「關注分點權證」分開。

- 全市場約 5.2 萬檔權證，奇摩一次 50 檔，全掃要上千次請求，不適合問一次掃一次。
  → 候選池＝前一個交易日成交金額前 POOL_SIZE 檔（證交所 MI_INDEX 權證類別＋櫃買 otc type=WW，每天抓一次），
    問的時候用奇摩批次抓這批的盤中成交，依成交金額、再依張數排。昨天沒什麼量、今天才突然放量的可能不在名單內（圖上寫明）。
- 11-06 實測：前一日上市權證 0999／0999P／0999C／0999B／0999X 共約 3.97 萬檔（有成交 1.67 萬），上櫃 type=WW 約 1.27 萬檔。
- 不跑背景工作（使用者在意費用）：問了才抓；同一份排行 RANK_CACHE_SECONDS 內重問沿用。
- 欄位解析、內外盤判斷、爆量判斷和關注分點權證共用（branch_warrant_live.parse／lean／surge_text）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import branch_warrant_live as bwl
import warrant_ai_tools as tools
import yahoo_quotes

POOL_SIZE = max(100, int(os.getenv("DISCORD_AI_WARRANT_RANK_POOL", "2500") or 2500))
TOP = 10
RANK_DEADLINE = 45.0             # 奇摩整次時限（2,500 檔＝50 批，11-06 實測約 0.32 秒一批）
RANK_CACHE_SECONDS = 60.0
TWSE_TYPES = ("0999", "0999P", "0999C", "0999B", "0999X")   # 認購、認售、牛、熊、可展延
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type={type}&response=json"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date}&type=WW&response=json"
TWSE_GAP = 1.0                   # 證交所連打會被擋
# 11-06 使用者：「另外列排除 ETF、台積電、正二等權證的一般股票權證 TOP10」。
# 一般股票權證＝標的是 4 碼、不是 00 開頭（ETF／正2／反1）的個股，且不在排除名單；指數權證（臺股指）奇摩沒有標的 → 不算。
GENERAL_EXCLUDE = {c.strip() for c in os.getenv("DISCORD_AI_WARRANT_RANK_EXCLUDE", "2330").split(",") if c.strip()}
_STOCK_CODE = re.compile(r"[1-9]\d{3}")

_LOCK = threading.Lock()
_POOL: Dict[str, Dict[str, Any]] = {}          # {今天日期: 候選池}
_RANK: Dict[str, Any] = {}                     # {"at": monotonic, "date": 今天, "res": 排行}


def _num(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _rows(table: Dict[str, Any], code_name: str, name_name: str, lots_name: str, value_name: str,
          market: str) -> List[Dict[str, Any]]:
    fields = [str(f).strip() for f in table.get("fields") or []]
    if not all(n in fields for n in (code_name, name_name, lots_name, value_name)):
        return []
    ci, ni, li, vi = (fields.index(n) for n in (code_name, name_name, lots_name, value_name))
    out = []
    for row in table.get("data") or []:
        if len(row) <= max(ci, ni, li, vi):
            continue
        code = str(row[ci]).strip()
        value = _num(row[vi])
        if len(code) == 6 and value > 0:           # 權證代號 6 碼；只留昨天有成交的
            out.append({"code": code, "name": str(row[ni]).strip(), "prev_lots": _num(row[li]) / 1000,
                        "prev_value": value, "market": market})
    return out


def _twse_day(day: datetime) -> Optional[List[Dict[str, Any]]]:
    """證交所某天的權證行情；那天沒有資料（休市、還沒公布）回 None。"""
    out: List[Dict[str, Any]] = []
    for i, kind in enumerate(TWSE_TYPES):
        if i:
            time.sleep(TWSE_GAP)
        payload = tools._official_json(TWSE_URL.format(date=day.strftime("%Y%m%d"), type=kind), "TWSE")
        if payload.get("stat") != "OK":
            if i == 0:
                return None
            continue
        for table in payload.get("tables") or []:
            out += _rows(table, "證券代號", "證券名稱", "成交股數", "成交金額", "上市")
    return out


def _tpex_day(day: datetime) -> List[Dict[str, Any]]:
    payload = tools._official_json(TPEX_URL.format(date=day.strftime("%Y/%m/%d")), "TPEx")
    out: List[Dict[str, Any]] = []
    for table in payload.get("tables") or []:
        out += _rows(table, "代號", "名稱", "成交股數", "成交金額(元)", "上櫃")
    return out


def previous_pool(now: datetime, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """前一個交易日成交金額前 POOL_SIZE 檔權證（每天抓一次）。回 {date, rows}；抓不到回 rows 空。"""
    key = now.strftime("%Y-%m-%d")
    with _LOCK:
        if key in _POOL:
            return _POOL[key]
    pool: Dict[str, Any] = {"date": "", "rows": []}
    for back in range(1, 9):
        day = now - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            twse = _twse_day(day)
        except Exception as exc:
            log(f"權證排行｜證交所 {day:%m/%d} 權證行情略過｜{tools.err_text(exc)}")
            break
        if twse is None:
            continue
        tpex, tpex_ok = [], False
        for attempt in range(2):              # 11-06 實測櫃買偶爾連線中斷（ChunkedEncodingError）：重試一次
            try:
                tpex, tpex_ok = _tpex_day(day), True
                break
            except Exception as exc:
                log(f"權證排行｜櫃買 {day:%m/%d} 權證行情失敗（第 {attempt + 1} 次）｜{tools.err_text(exc)}")
        rows = sorted(twse + tpex, key=lambda r: -r["prev_value"])[:POOL_SIZE]
        pool = {"date": day.strftime("%Y-%m-%d"), "rows": rows, "twse": len(twse), "tpex": len(tpex), "tpex_ok": tpex_ok}
        break
    if pool["rows"] and pool.get("tpex_ok"):  # 櫃買沒抓到：這次只用上市照出，但不快取，下次問再重抓
        with _LOCK:
            _POOL.clear()
            _POOL[key] = pool
    return pool


def is_general(quote: Dict[str, Any]) -> bool:
    """一般股票權證：標的是個股（4 碼、非 00 開頭）且不在排除名單（預設台積電）。"""
    code = str(quote.get("underlying") or "")
    return bool(_STOCK_CODE.fullmatch(code)) and code not in GENERAL_EXCLUDE


def by_stock(quotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """11-06 使用者（「一隻股票的權證容易霸榜」）：一般股票權證改依標的股票彙整——同一檔股票的權證成交張數、金額、
    內外盤、昨量加總，依金額、再依張數排。"""
    groups: Dict[str, Dict[str, Any]] = {}
    for q in quotes:
        code = str(q.get("underlying") or "")
        g = groups.setdefault(code, {"underlying": code, "underlying_name": q.get("underlying_name") or "", "count": 0,
                                     "lots": 0.0, "prev_lots": 0.0, "value": 0.0, "inside": 0.0, "outside": 0.0, "top": q,
                                     "real_lots": 0.0, "cross": 0.0,
                                     "call_net": 0.0, "calls": 0})
        g["count"] += 1
        for key in ("lots", "prev_lots", "value", "inside", "outside", "cross"):
            g[key] += float(q.get(key) or 0)
        g["real_lots"] += float(q.get("real_lots", q.get("lots")) or 0)
        # 認購、認售混在一起算內外盤會互相抵銷（買認售是看空）→ 表上只放認購的估計買賣超（外盤－內盤）
        if not any(mark in str(q.get("name") or "") for mark in ("售", "熊")):
            g["calls"] += 1
            g["call_net"] += float(q.get("outside") or 0) - float(q.get("inside") or 0)
        if q["value"] > g["top"]["value"]:
            g["top"] = q
    return sorted(groups.values(), key=lambda g: (-g["value"], -g["lots"]))


def build(log: Callable[[str], None] = print) -> Dict[str, Any]:
    now = tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    hit = _RANK.get("res")
    if hit and _RANK.get("date") == today and time.monotonic() - _RANK.get("at", 0) <= RANK_CACHE_SECONDS:
        return hit
    pool = previous_pool(now, log)
    rows = pool["rows"]
    symbols = {bwl.symbol(r["code"], r["market"]): r["code"] for r in rows}
    items = yahoo_quotes.raw_items(symbols, RANK_DEADLINE) if symbols else {}
    traded = []
    for code, item in items.items():
        quote = bwl.parse(item, today)
        if quote["traded"]:
            traded.append(dict(quote, code=code))
    traded.sort(key=lambda q: (-q["value"], -q["lots"]))          # 金額、張數高的優先
    res = {"time": now.strftime("%H:%M"), "after_close": (now.hour, now.minute) >= (13, 35), "tpex_ok": pool.get("tpex_ok", True),
           "pool_date": pool["date"], "pool": len(rows), "quoted": len(items), "traded": len(traded),
           "rows": traded[:TOP], "all": traded, "general": by_stock([q for q in traded if is_general(q)])[:TOP]}
    if rows and items:
        _RANK.update(at=time.monotonic(), date=today, res=res)
    return res


def _times(quote: Dict[str, Any]) -> str:
    prev = float(quote.get("prev_lots") or 0)      # 11-06：疑似對轉的量不算進比昨天
    return f"{float(quote.get('real_lots', quote['lots'])) / prev:.1f}倍" if prev > 0 else "昨無量"


def _table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    table = []
    for i, q in enumerate(rows, 1):
        change = "-" if q["change_pct"] is None else f"{q['change_pct']:+.1f}%"
        table.append([f"{i}. {q['name'] or q['code']} {q['code']}", f"{q['lots']:,.0f}張 {tools._plain_money(q['value'])}",
                      _times(q), change, bwl.lean(q)[0]])
    return {"type": "table", "columns": ["權證", "今日成交", "比昨天", "漲跌", "內外盤"], "rows": table,
            "widths": [3.4, 2.6, 1.3, 1.2, 1.6], "signed": ("漲跌",), "accent": (),
            "row_badges": [bwl.quote_badge(q) for q in rows]}


def _stock_table(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    table = []
    for i, g in enumerate(groups, 1):
        net = f"{g['call_net']:+,.0f}張" if g["calls"] else "-"
        table.append([f"{i}. {g['underlying_name'] or g['underlying']} {g['underlying']}",
                      f"{g['lots']:,.0f}張 {tools._plain_money(g['value'])}", f"{g['count']}檔", _times(g), net])
    return {"type": "table", "columns": ["標的股", "權證成交", "檔數", "比昨天", "認購買賣超"], "rows": table,
            "widths": [2.9, 2.7, 1.0, 1.2, 1.8], "accent": (), "signed": ("認購買賣超",),
            "row_badges": [bwl.surge_text(g) for g in groups]}      # 彙整列只標爆量（已扣掉疑似對轉的量）


def card(res: Dict[str, Any]) -> Dict[str, Any]:
    rows = res["rows"]
    total = sum(q["value"] for q in rows)
    sections: List[Dict[str, Any]] = [{"type": "tiles", "items": [
        {"label": "掃描權證", "value": f"{res['pool']:,} 檔"},
        {"label": "今天有成交", "value": f"{res['traded']:,} 檔"},
        {"label": f"TOP{TOP} 成交金額", "value": tools._plain_money(total) if total else "0"}]}]
    if rows:
        excluded = "、".join({"2330": "台積電"}.get(c, c) for c in sorted(GENERAL_EXCLUDE))
        sections += [{"type": "heading", "text": "全部權證（成交金額、張數大的在前）"}, _table(rows)]
        general = res.get("general") or []
        sections.append({"type": "heading", "text": f"一般股票權證 TOP{TOP}（依標的股票；排除指數、ETF{'、' + excluded if excluded else ''}）"})
        sections.append(_stock_table(general) if general else {"type": "paragraph", "text": "今天還沒有一般股票權證成交。"})
    elif not res["pool"]:
        sections.append({"type": "paragraph", "text": "抓不到前一個交易日的權證行情，暫時排不出來，請稍後再試。"})
    else:
        sections.append({"type": "paragraph", "text": f"候選的 {res['pool']:,} 檔權證，今天到 {res['time']} 都還沒有成交。"})
    pool_day = res["pool_date"][5:].replace("-", "/") if res["pool_date"] else "前一個交易日"
    if res["pool"] and not res.get("tpex_ok", True):
        sections.append({"type": "note", "keep": True, "text": "這次櫃買權證行情沒抓到，名單只含上市權證；再問一次會重抓。"})
    sections.append({"type": "note", "keep": True,
                     "text": f"候選＝{pool_day} 成交金額前 {res['pool']:,} 檔權證，昨天沒什麼量、今天才突然放量的可能不在名單內；"
                             "比昨天＝今日張數÷昨天全天張數；爆量＝已達昨天 2 倍；內外盤只能參考（權證多和發行商造市成交）；"
                             "認購買賣超＝這檔股票認購權證的外盤張數－內盤張數（只算候選名單裡的權證）；"
                             "疑似對轉＝大量分不出內外盤的成交（多半是券商之間對轉），不算進比昨天、爆量。"})
    return {"branch": f"權證{'今日' if res['after_close'] else '盤中'}成交 TOP{TOP}",
            "label": f"{'今日' if res['after_close'] else '盤中'} {res['time']}", "sections": sections,
            "clean_display": False, "footer_text": "股市艾斯  /  權證成交為奇摩即時報價，收盤前會變動"}


def log_text(res: Dict[str, Any]) -> str:
    lines = [f"權證成交排行｜{res['time']}｜候選 {res['pool']} 檔（{res['pool_date']} 成交金額前段）"
             f"｜有報價 {res['quoted']} 檔｜今天有成交 {res['traded']} 檔"]
    lines.append("【全部權證】")
    for i, q in enumerate(res["all"][:30], 1):
        lines.append(f"{i}. {q['name']}（{q['code']}，標的 {q.get('underlying') or '指數'}）｜{q['lots']:,.0f}張 "
                     f"{tools._plain_money(q['value'])}｜昨 {q['prev_lots']:,.0f}張｜內 {q['inside']:,.0f}／外 {q['outside']:,.0f}"
                     f"｜{bwl.lean(q)[0]}｜{q['time']}")
    lines.append("【一般股票權證（依標的）】")
    for i, g in enumerate(res.get("general") or [], 1):
        lines.append(f"{i}. {g['underlying_name']}（{g['underlying']}）｜{g['count']} 檔｜{g['lots']:,.0f}張 "
                     f"{tools._plain_money(g['value'])}｜昨 {g['prev_lots']:,.0f}張｜內 {g['inside']:,.0f}／外 {g['outside']:,.0f}"
                     f"｜認購估計買賣超 {g['call_net']:+,.0f}張｜最大 {g['top']['name']}")
    return "\n".join(lines)
