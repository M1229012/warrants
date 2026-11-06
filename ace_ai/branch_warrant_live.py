"""11-06 使用者（測試版，/testace 管理員）：盤中看「關注分點有部位的權證」今天有沒有量、量多大、偏買還是偏賣。

- 關注分點＝圖片上標註的分點：高勝率分點＋精選五分點（tools.display_branch_set）；問句點名分點就只看那幾家。
- 分點部位＝權證分點歷史庫 FIFO 剩餘張數（剩餘成本 ≥ MIN_HOLD_COST 才算有單）（tools._branch_positions；release 之後的日子用 Sheet 補），收盤後才會變，
  同一天 30 分鐘內重問不重算。
- 盤中行情＝奇摩批次報價（yahoo_quotes.raw_items，一次 50 檔，和族群報價共用鎖、間隔、被擋冷卻）。
  11-06 實測權證有 volume（股）、previousVolume（昨量）、turnoverM（百萬元）、inMarket／outMarket（內／外盤張數）。
- 偏買／偏賣：能分辨內外盤的成交裡，外盤（打在委賣價）≥ 60% 寫偏買、≤ 40% 寫偏賣。權證多半是和發行商造市成交，
  只能參考；盤中看不到分點，是不是這些分點在買賣要等收盤分點資料。
- 問法任何白話都可以：先看有沒有提到分點／追蹤／關注／主力或已知分點名稱，再由 AI 讀整句判斷是不是在問今天盤中的量。
- 11-06 使用者：「關注分點」和「盤中權證爆量 TOP10」（全市場，warrant_live_rank）是分開的兩張；同一次 AI 讀題分出 branch／rank。
- 11-06 使用者：「優先列出金額、張數高的」→ 表格依成交金額、再依張數排。
- 關注分點清單是追蹤規則的一部分（機密），只給管理員。
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import warrant_ai_tools as tools
import yahoo_quotes

HOLD_CACHE_SECONDS = 1800        # 分點部位收盤後才會變：同一天 30 分鐘內重問不重算
QUOTE_DEADLINE = 25.0            # 奇摩報價整次時限
TABLE_MAX = 20                   # 圖上最多列幾檔（其餘寫 Log）
LEAN_BUY = 0.6                   # 外盤比 ≥ 60%＝偏買；≤ 40%＝偏賣
MIN_SIDE_LOTS = 10               # 能分辨內外盤的成交太少（< 10 張）不判斷
SURGE_MULT = 2.0                 # 盤中量已經 ≥ 昨天全天 2 倍（或昨天沒成交）＝爆量（圖上標出）
# 「有單」門檻：剩餘部位成本（關注分點合計）。11-06 實測五個分點 FIFO 剩餘 > 0 的有 5,641 檔，多是 1～5 張的零頭
# （小額買賣的殘量），≥ 50 萬剩 474 檔、≥ 100 萬剩 284 檔 → 預設 50 萬（部分減碼後的部位也看得到）
MIN_HOLD_COST = float(os.getenv("DISCORD_AI_BRANCH_LIVE_MIN_COST", "500000") or 500000)

_HOLD: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_HOLD_LOCK = threading.Lock()

# 只是「要不要請 AI 讀題」的門檻，不是判斷：沒提到這些字、也沒點名已知分點的問句，不多花一次 AI
_MENTION_RE = re.compile(r"分點|追蹤|關注|主力|券商|大戶")
_RANK_WORDS = r"(?:排行|排名|top|前\s*\d+|前[十五三]|最|爆|量|熱)"
_RANK_RE = re.compile(rf"(?:權證|牛熊證|窩輪).*{_RANK_WORDS}|{_RANK_WORDS}.*(?:權證|牛熊證|窩輪)", re.IGNORECASE)
_TODAY_WORDS = r"(?:今天|今日|盤中|現在|目前|買超|賣超|買賣超|內外盤|外盤|內盤)"
_TODAY_RE = re.compile(rf"(?:權證|牛熊證|窩輪).*{_TODAY_WORDS}|{_TODAY_WORDS}.*(?:權證|牛熊證|窩輪)")
_SCHEMA = {"type": "object",
           "properties": {"kind": {"type": "string", "enum": ["branch", "rank", "stock", "other"]},
                          "stock": {"type": "string"},
                          "group": {"type": "string", "enum": ["all", "selected_five", "named"]},
                          "branches": {"type": "array", "items": {"type": "string"}}},
           "required": ["kind", "branches"]}


def read_request(question: str, ask_ai: Optional[Callable[[str, dict], Optional[dict]]],
                 known: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """回 {kind: branch（關注分點有單的權證今天盤中的量／買賣）｜rank（全市場權證今天盤中成交／爆量排行），
    branches: [正式分點名], unknown: [認不出的名稱]}；branches 空＝全部關注分點。不是這兩種或讀題失敗回 None（照一般流程）。"""
    q = str(question or "")
    named = any(len(alias) >= 2 and alias in q for alias in known)
    if ask_ai is None or not (_MENTION_RE.search(q) or named or _RANK_RE.search(q) or _TODAY_RE.search(q)):
        return None
    prompt = ("你是台股問句解析器，只輸出 JSON，不要回答問題。判斷使用者要看哪一種「今天盤中的權證成交量」：\n"
              "kind=branch：分點（關注分點、追蹤分點、主力、某家券商分點）手上持有的權證，今天盤中有沒有量、在買或賣、有沒有爆量／出貨"
              "（例：關注分點的權證今天有量嗎、追蹤分點有單的權證誰在爆量、元大南屯的權證今天有在賣嗎）。\n"
              "kind=rank：全市場的權證今天盤中成交排行，不限分點（例：今天哪些權證爆量、權證成交金額前十、盤中最熱的權證、權證量大排行）。\n"
              "kind=stock：某一檔股票的權證今天盤中的成交、買賣超、內外盤，不問分點（例：金山電的權證今天買賣超、2454 權證盤中有人在買嗎、"
              "台積電權證今天是買超還賣超）；stock＝那檔股票的代號或名稱，照原文寫。\n"
              "kind=other：其他（分點過去的買賣紀錄、勝率、持有部位明細、某檔股票的權證分點籌碼（哪些分點買）、型態、權證怎麼選、"
              "收盤後的歷史排行等）。\n"
              "group（kind=branch 才看）：selected_five＝指定「精選五分點／五大分點／精選分點」這一組；named＝點名特定分點；"
              "all＝其他（我關注的、追蹤分點、主力，沒有特別指定）。\n"
              "branches：group=named 時句子裡點名的券商分點名稱，照原文寫；其他情況留空陣列。\n"
              "問題：" + q)
    try:
        out = ask_ai(prompt, _SCHEMA) or {}
    except Exception:
        return None
    kind = out.get("kind")
    if kind == "rank":
        return {"kind": "rank", "group": "all", "branches": [], "unknown": []}
    if kind == "stock":                              # 11-06 使用者：某檔股票的權證今天買賣超
        return {"kind": "stock", "group": "all", "branches": [], "unknown": [], "stock": str(out.get("stock") or "").strip()}
    if kind != "branch":
        return None
    if out.get("group") == "selected_five":          # 11-06 使用者：「另外可以問精選五分點的權證」
        return {"kind": "branch", "group": "selected_five", "branches": [], "unknown": []}
    kf = tools.core()
    picks: List[str] = []
    unknown: List[str] = []
    for name in out.get("branches") or []:
        name = str(name or "").strip()
        if not name:
            continue
        canon = known.get(kf.normalize_branch_name(name)) or known.get(name)
        if canon:
            picks.append(canon)
        else:
            unknown.append(name)
    picks = list(dict.fromkeys(picks))
    return {"kind": "branch", "group": "named" if picks or unknown else "all", "branches": picks, "unknown": unknown}


def watched_branches() -> List[str]:
    high, selected = tools.display_branch_set()
    return sorted(set(high) | set(selected))


def _branch_holdings(branch: str) -> List[Dict[str, Any]]:
    pos = tools._branch_positions(branch)
    if not pos:
        return []
    out = []
    for p in pos["positions"].values():
        code = str(p["warrant"])
        if code.startswith("~") or p["remaining"] <= 0 or p["expired"]:
            continue                 # 「~」＝多檔權證事件張數未拆分，沒有單一代號可以查行情
        out.append({"warrant": code, "name": p.get("name") or "", "stock": p.get("stock") or "",
                    "stock_name": p.get("stock_name") or "", "lots": p["remaining"] / 1000,
                    "cost": float(p.get("remaining_cost") or 0.0), "market": (p.get("meta") or {}).get("market") or ""})
    return out


def holdings(branches: List[str], log: Callable[[str], None] = print) -> Dict[str, Dict[str, Any]]:
    """{權證代號: {名稱、標的、holders=[(分點, 張數)]、lots＝合計張數、cost＝合計成本}}。"""
    key = (tools.taipei_now().strftime("%Y-%m-%d"), tuple(sorted(branches)))
    with _HOLD_LOCK:
        hit = _HOLD.get(key)
        if hit and time.monotonic() - hit[0] <= HOLD_CACHE_SECONDS:
            return hit[1]
    merged: Dict[str, Dict[str, Any]] = {}
    for branch in branches:
        try:
            rows = _branch_holdings(branch)
        except Exception as exc:     # 單一分點讀不到只略過，不讓整張失敗
            log(f"分點權證盤中｜{branch} 部位略過｜{tools.err_text(exc)}")
            continue
        for row in rows:
            item = merged.setdefault(row["warrant"], dict(row, holders=[], lots=0.0, cost=0.0))
            item["holders"].append((branch, row["lots"]))
            item["lots"] += row["lots"]
            item["cost"] += row["cost"]
            item["name"] = item["name"] or row["name"]
    merged = {code: item for code, item in merged.items() if item["cost"] >= MIN_HOLD_COST}   # 零頭不算有單
    for item in merged.values():
        item["holders"].sort(key=lambda h: -h[1])
    with _HOLD_LOCK:
        if len(_HOLD) > 16:
            _HOLD.clear()
        _HOLD[key] = (time.monotonic(), merged)
    return merged


def symbol(code: str, market: str = "") -> str:
    """奇摩代號：上櫃 .TWO、上市 .TW；基本資料沒有市場別時看代號（上櫃權證 7 開頭）。"""
    m = str(market or "").lower()
    if "櫃" in m or m in ("tpex", "otc"):
        return f"{code}.TWO"
    if "市" in m or m == "twse":
        return f"{code}.TW"
    return f"{code}.TWO" if str(code).startswith("7") else f"{code}.TW"


def parse(item: Dict[str, Any], today: str) -> Dict[str, Any]:
    """一筆奇摩權證報價 → 今天的量能。最後成交不是今天＝今天還沒成交（昨收、昨量照填）。"""
    raw = yahoo_quotes._raw
    stamp = str(item.get("regularMarketTime") or "")
    moment = None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tools.TAIPEI_TZ)
    except ValueError:
        pass
    today_trade = bool(moment and moment.strftime("%Y-%m-%d") == today)
    lots = raw(item.get("volume")) / 1000 if today_trade else 0.0
    price = raw(item.get("price")) if today_trade else 0.0
    prev = raw(item.get("regularMarketPreviousClose"))
    value = raw(item.get("turnoverM")) * 1e6 if today_trade else 0.0
    if today_trade and value <= 0:
        value = lots * 1000 * (raw(item.get("avgPrice")) or price)
    inside, outside = (raw(item.get("inMarket")), raw(item.get("outMarket"))) if today_trade else (0.0, 0.0)
    related = str(item.get("relatedSymbol") or "")       # 標的：2330.TW、00632R.TW；指數權證（臺股指）是空的
    return {"name": str(item.get("symbolName") or ""), "traded": today_trade and lots > 0, "lots": lots,
            "underlying": related.split(".")[0], "underlying_name": str(item.get("relatedSymbolName") or ""),
            "prev_lots": raw(item.get("previousVolume")) / 1000, "value": value, "price": price, "prev": prev,
            "change_pct": (price / prev - 1) * 100 if price > 0 and prev > 0 else None,
            "inside": inside, "outside": outside, "time": moment.strftime("%H:%M") if today_trade else ""}


def lean(quote: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """（文字, 外盤比）；能分辨內外盤的成交太少回（"-", None）。"""
    side = float(quote.get("inside") or 0) + float(quote.get("outside") or 0)
    if side < MIN_SIDE_LOTS:
        return "-", None
    ratio = float(quote.get("outside") or 0) / side
    if ratio >= LEAN_BUY:
        return f"偏買 {ratio:.0%}", ratio
    if ratio <= 1 - LEAN_BUY:
        return f"偏賣 {1 - ratio:.0%}", ratio
    return "買賣均衡", ratio


def surge_text(quote: Dict[str, Any]) -> str:
    """盤中量已經 ≥ 昨天全天 SURGE_MULT 倍（或昨天沒成交）→「爆量」。"""
    lots, prev = float(quote.get("lots") or 0), float(quote.get("prev_lots") or 0)
    return "爆量" if lots > 0 and (prev <= 0 or lots >= SURGE_MULT * prev) else ""


def build(branches: List[str], focus: str = "", log: Callable[[str], None] = print) -> Dict[str, Any]:
    hold = holdings(branches, log)
    symbols = {symbol(code, item.get("market", "")): code for code, item in hold.items()}
    items = yahoo_quotes.raw_items(symbols, QUOTE_DEADLINE) if symbols else {}
    now = tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    rows = []
    for code, item in hold.items():
        quote = parse(items[code], today) if code in items else None
        row = dict(item, quote=quote, share=0.0)
        if quote and quote["traded"] and item["lots"] > 0:
            row["share"] = quote["lots"] / item["lots"]
        rows.append(row)
    traded = [r for r in rows if r["quote"] and r["quote"]["traded"]]
    traded.sort(key=lambda r: (-r["quote"]["value"], -r["quote"]["lots"]))   # 11-06 使用者：金額、張數高的優先
    return {"branches": branches, "focus": focus, "time": now.strftime("%H:%M"), "date": today,
            "holding_count": len(hold), "quoted": len(items), "rows": rows, "traded": traded}


def _holder_text(holders: List[Tuple[str, float]]) -> str:
    return holders[0][0] if len(holders) == 1 else f"{holders[0][0]} 等{len(holders)}家"


def card(res: Dict[str, Any]) -> Dict[str, Any]:
    """branch_card 格式（answer_image.branch_card）：摘要三格＋有成交的權證表＋一行聲明。"""
    traded = res["traded"]
    leans = [lean(r["quote"])[1] for r in traded]
    buys = sum(1 for x in leans if x is not None and x >= LEAN_BUY)
    sells = sum(1 for x in leans if x is not None and x <= 1 - LEAN_BUY)
    total = sum(r["quote"]["value"] for r in traded)
    title = f"{res['focus']}｜權證盤中量能" if res["focus"] else "關注分點權證｜盤中量能"
    sections: List[Dict[str, Any]] = [{"type": "tiles", "items": [
        {"label": "今天有成交", "value": f"{len(traded)} / {res['holding_count']} 檔"},
        {"label": "成交金額合計", "value": tools._plain_money(total) if total else "0"},
        {"label": "偏買／偏賣", "value": f"{buys} / {sells} 檔"}]}]
    if traded:
        rows = []
        for r in traded[:TABLE_MAX]:
            q = r["quote"]
            change = "-" if q["change_pct"] is None else f"{q['change_pct']:+.1f}%"
            rows.append([f"{r['name'] or q['name'] or r['warrant']} {r['warrant']}", _holder_text(r["holders"]),
                         f"{r['lots']:,.0f}張", f"{q['lots']:,.0f}張 {tools._plain_money(q['value'])}",
                         f"{r['share']:.0%}", change, lean(q)[0]])
        sections += [{"type": "heading", "text": "有成交的權證（成交金額、張數大的在前）"},
                     {"type": "table", "columns": ["權證", "持有分點", "分點持有", "今日成交", "占持有", "漲跌", "內外盤"],
                      "rows": rows, "widths": [3.0, 1.9, 1.3, 2.2, 1.1, 1.1, 1.5], "signed": ("漲跌",), "accent": (),
                      "row_badges": [surge_text(r["quote"]) for r in traded[:TABLE_MAX]]}]
        if len(traded) > TABLE_MAX:
            sections.append({"type": "note", "text": f"另有 {len(traded) - TABLE_MAX} 檔量較小，未列出", "keep": True})
    else:
        sections.append({"type": "paragraph", "text": f"關注分點持有的 {res['holding_count']} 檔權證，今天到 {res['time']} 都還沒有成交。"})
    sections.append({"type": "note", "keep": True,
                     "text": "今日成交＝全市場成交；占持有＝今日成交張數÷關注分點持有張數；爆量＝盤中量已達昨天全天 2 倍；內外盤＝可分辨的成交裡打在委賣（偏買）或委買（偏賣）的比例，"
                             "權證多和發行商造市成交，只能參考。盤中看不到分點，是不是這些分點在買賣要等收盤分點資料確認。"})
    return {"branch": title, "label": f"盤中 {res['time']}", "sections": sections, "clean_display": False,
            "footer_text": "股市艾斯  /  權證盤中量能為奇摩即時報價，收盤前會變動"}


def log_text(res: Dict[str, Any]) -> str:
    lines = [f"分點權證盤中｜{res['time']}｜分點 {len(res['branches'])} 家｜持有權證 {res['holding_count']} 檔"
             f"｜有報價 {res['quoted']} 檔｜今天有成交 {len(res['traded'])} 檔"]
    for r in res["traded"]:
        q = r["quote"]
        holders = "、".join(f"{b} {lots:,.0f}張" for b, lots in r["holders"])
        lines.append(f"- {r['name'] or q['name']}（{r['warrant']}）｜{holders}｜今日 {q['lots']:,.0f}張"
                     f"（昨 {q['prev_lots']:,.0f}張）{tools._plain_money(q['value'])}｜占持有 {r['share']:.0%}"
                     f"｜內 {q['inside']:,.0f}／外 {q['outside']:,.0f}｜{lean(q)[0]}｜最後成交 {q['time']}")
    return "\n".join(lines)
