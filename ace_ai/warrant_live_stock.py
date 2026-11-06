"""11-06 使用者（測試版，/testace 管理員）：問「某檔股票的權證今天買賣超」→ 列出這檔股票今天有成交的權證、估計買賣超。

- 權證清單＝權證基本資料（warrant_store.warrant_meta，回測 Actions 每天更新）裡標的是這檔、還沒過最後交易日的；
  不受 TOP10 候選池（前一日前 2,500 檔）限制。11-06 實測金山電有 89 檔。
- 盤中行情＝奇摩批次報價（yahoo_quotes.raw_items）；欄位解析和內外盤判斷與關注分點權證共用（branch_warrant_live）。
- 估計買賣超＝外盤張數－內盤張數（只算分得出內外盤的成交）。權證的對手幾乎都是發行商造市：
  外盤多≈投資人向發行商買進、內盤多≈投資人賣回。認售（含熊證）買超是看空，所以認購、認售分開加總。
- 盤中看不到分點；是誰在買賣要等收盤分點資料。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

import branch_warrant_live as bwl
import warrant_ai_tools as tools
import yahoo_quotes

TABLE_MAX = 20
QUOTE_DEADLINE = 30.0


def resolve(text: str) -> Tuple[str, str]:
    """問句裡的股票（代號或名稱）→（代號, 名稱）；認不出回（"", ""）。名稱先用權證基本資料的標的名冊（有權證的股票）。"""
    import warrant_store
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    try:
        names, codes = warrant_store.stock_names()
    except Exception:
        names, codes = {}, {}
    if raw in names:
        return raw, names[raw]
    if raw in codes:
        return codes[raw], raw
    code = tools.core()._normalize_stock_name_code_key(raw)
    if code and code.isdigit():
        try:
            return code, tools.resolve_stock_name(code)
        except Exception:
            return code, names.get(code, "")
    return "", ""


def warrants_of(code: str, today: str) -> List[Dict[str, str]]:
    import warrant_store
    rows = warrant_store._query("SELECT warrant, name, market, type FROM warrant_meta WHERE stock=? "
                                "AND (last_trade='' OR last_trade IS NULL OR last_trade>=?)", (code, today))
    return [{"code": r[0], "name": r[1] or "", "market": r[2] or "", "type": r[3] or ""} for r in rows]


def side_of(kind: str, name: str) -> str:
    """認購（含牛證）＝call；認售（含熊證）＝put。"""
    text = f"{kind}{name}"
    return "put" if ("售" in text or "熊" in text) else "call"


def build(code: str, name: str, log: Callable[[str], None] = print) -> Dict[str, Any]:
    now = tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    listed = warrants_of(code, today)
    symbols = {bwl.symbol(w["code"], w["market"]): w["code"] for w in listed}
    items = yahoo_quotes.raw_items(symbols, QUOTE_DEADLINE) if symbols else {}
    meta = {w["code"]: w for w in listed}
    rows = []
    for wcode, item in items.items():
        q = bwl.parse(item, today)
        if not q["traded"]:
            continue
        w = meta.get(wcode) or {}
        net = q["outside"] - q["inside"]
        rows.append(dict(q, code=wcode, name=q["name"] or w.get("name") or wcode, side=side_of(w.get("type", ""), q["name"] or w.get("name", "")),
                         net=net, net_value=net * 1000 * (q["price"] or 0)))
    rows.sort(key=lambda r: (-r["value"], -r["lots"]))
    sums = {side: {"count": 0, "value": 0.0, "lots": 0.0, "net": 0.0, "net_value": 0.0} for side in ("call", "put")}
    for r in rows:
        s = sums[r["side"]]
        s["count"] += 1
        for key in ("value", "lots", "net", "net_value"):
            s[key] += r[key]
    return {"code": code, "name": name, "time": now.strftime("%H:%M"), "after_close": (now.hour, now.minute) >= (13, 35),
            "listed": len(listed), "quoted": len(items), "rows": rows, "sums": sums}


def _net_text(lots: float) -> str:
    return f"{lots:+,.0f}張" if abs(lots) >= 0.5 else "0張"


def card(res: Dict[str, Any]) -> Dict[str, Any]:
    rows, sums = res["rows"], res["sums"]
    tiles = [{"label": "今天有成交", "value": f"{len(rows)} / {res['listed']} 檔"}]
    for side, label in (("call", "認購 估計買賣超"), ("put", "認售 估計買賣超")):
        s = sums[side]
        value = f"{_net_text(s['net'])} {tools._plain_money(abs(s['net_value']))}" if s["count"] else "無成交"
        tiles.append({"label": label, "value": value, "tone": "signed"})
    sections: List[Dict[str, Any]] = [{"type": "tiles", "items": tiles}]
    if rows:
        table = []
        for r in rows[:TABLE_MAX]:
            change = "-" if r["change_pct"] is None else f"{r['change_pct']:+.1f}%"
            table.append([f"{r['name']} {r['code']}", f"{r['lots']:,.0f}張 {tools._plain_money(r['value'])}",
                          f"{r['outside']:,.0f}", f"{r['inside']:,.0f}", _net_text(r["net"]), change])
        sections += [{"type": "heading", "text": "今天有成交的權證（成交金額、張數大的在前）"},
                     {"type": "table", "columns": ["權證", "今日成交", "外盤", "內盤", "估計買賣超", "漲跌"], "rows": table,
                      "widths": [3.2, 2.4, 1.1, 1.1, 1.5, 1.1], "signed": ("估計買賣超", "漲跌"), "accent": (),
                      "row_badges": [bwl.surge_text(r) for r in rows[:TABLE_MAX]]}]
        if len(rows) > TABLE_MAX:
            sections.append({"type": "note", "keep": True, "text": f"另有 {len(rows) - TABLE_MAX} 檔金額較小，未列出"})
    elif not res["listed"]:
        sections.append({"type": "paragraph", "text": "權證基本資料裡找不到這檔股票還在交易的權證。"})
    else:
        sections.append({"type": "paragraph", "text": f"這檔股票的 {res['listed']} 檔權證，今天到 {res['time']} 都還沒有成交。"})
    sections.append({"type": "note", "keep": True,
                     "text": "估計買賣超＝外盤張數－內盤張數（只算分得出內外盤的成交）；權證多和發行商造市成交，外盤多大致是投資人買進、"
                             "內盤多大致是投資人賣出；認售買超是看空。盤中看不到分點，是誰在買賣要等收盤分點資料。"})
    when = "今日" if res["after_close"] else "盤中"
    return {"branch": f"{res['name'] or res['code']}（{res['code']}）｜權證{when}買賣超", "label": f"{when} {res['time']}",
            "sections": sections, "clean_display": False, "footer_text": "股市艾斯  /  權證成交為奇摩即時報價，收盤前會變動"}


def log_text(res: Dict[str, Any]) -> str:
    sums = res["sums"]
    lines = [f"個股權證買賣超｜{res['name']}（{res['code']}）｜{res['time']}｜掛牌 {res['listed']} 檔｜有報價 {res['quoted']} 檔"
             f"｜有成交 {len(res['rows'])} 檔｜認購 {_net_text(sums['call']['net'])}｜認售 {_net_text(sums['put']['net'])}"]
    for r in res["rows"]:
        lines.append(f"- {r['name']}（{r['code']}）｜{r['lots']:,.0f}張 {tools._plain_money(r['value'])}｜外 {r['outside']:,.0f}"
                     f"／內 {r['inside']:,.0f}｜估計 {_net_text(r['net'])}｜{r['time']}")
    return "\n".join(lines)


def ensure_store() -> Optional[str]:
    """權證基本資料還沒同步時回提示文字（呼叫端直接回覆）；可以用回 None。"""
    import warrant_store
    try:
        return None if warrant_store.available() else "權證基本資料還沒同步，暫時查不到這檔股票的權證清單，請稍後再試。"
    except Exception as exc:
        return f"權證基本資料讀取失敗：{tools.err_text(exc)}"
