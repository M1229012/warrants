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
              "kind=branch 時如果還指定某檔股票（例：永豐金內湖的雙鴻權證今天有沒有加碼或賣出、元大南屯手上的 2454 權證今天有動嗎），"
              "stock 寫那檔股票的代號或名稱，照原文寫；沒指定留空字串。\n"
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
    stock = str(out.get("stock") or "").strip()      # 11-06 使用者：「永豐金內湖的雙鴻」＝分點＋股票
    if out.get("group") == "selected_five":          # 11-06 使用者：「另外可以問精選五分點的權證」
        return {"kind": "branch", "group": "selected_five", "branches": [], "unknown": [], "stock": stock}
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
    return {"kind": "branch", "group": "named" if picks or unknown else "all", "branches": picks, "unknown": unknown,
            "stock": stock}


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


def holdings(branches: List[str], log: Callable[[str], None] = print,
             min_cost: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """{權證代號: {名稱、標的、holders=[(分點, 張數)]、lots＝合計張數、cost＝合計成本}}；min_cost 預設 MIN_HOLD_COST。"""
    min_cost = MIN_HOLD_COST if min_cost is None else min_cost
    key = (tools.taipei_now().strftime("%Y-%m-%d"), tuple(sorted(branches)), min_cost)
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
    merged = {code: item for code, item in merged.items() if item["cost"] >= min_cost}   # 零頭不算有單
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


FOCUS_MIN_COST = 100_000         # 指定股票時（永豐金內湖的雙鴻）門檻放低：小一點的部位也要看到有沒有動作
# 11-06 使用者（「推估他買的量占這支權證發行量幾 %，初步判斷是不是他賣的」）：三個條件都成立標「疑似賣出」（待收盤確認）
SUSPECT_ISSUE_SHARE = float(os.getenv("DISCORD_AI_BRANCH_LIVE_SUSPECT_ISSUE", "0.2") or 0.2)   # 分點持有 ≥ 發行量 20%
SUSPECT_INSIDE_SHARE = float(os.getenv("DISCORD_AI_BRANCH_LIVE_SUSPECT_INSIDE", "0.3") or 0.3)  # 今日內盤 ≥ 分點持有 30%


def suspect_sell(row: Dict[str, Any]) -> bool:
    """疑似分點賣出：分點是主要持有人（持有 ≥ 發行量 SUSPECT_ISSUE_SHARE）、今天內盤張數 ≥ 分點持有 SUSPECT_INSIDE_SHARE、
    估計買賣超為負。只是線索，是不是他賣要等收盤分點資料。"""
    q = row.get("quote") or {}
    if not q.get("traded") or not row.get("lots") or row.get("issue_share") is None:
        return False
    return (row["issue_share"] >= SUSPECT_ISSUE_SHARE and float(q.get("inside") or 0) >= SUSPECT_INSIDE_SHARE * row["lots"]
            and net_lots(q) < 0)


def build(branches: List[str], focus: str = "", log: Callable[[str], None] = print,
          stock: str = "", stock_name: str = "") -> Dict[str, Any]:
    """stock＝只看這檔股票的權證（11-06 使用者：「單獨問分點的權證今日有沒有動作，例如永豐金內湖的雙鴻」）。"""
    hold = holdings(branches, log, min(MIN_HOLD_COST, FOCUS_MIN_COST) if stock else None)
    if stock:
        hold = {code: item for code, item in hold.items() if str(item.get("stock") or "") == stock}
    symbols = {symbol(code, item.get("market", "")): code for code, item in hold.items()}
    items = yahoo_quotes.raw_items(symbols, QUOTE_DEADLINE) if symbols else {}
    now = tools.taipei_now()
    today = now.strftime("%Y-%m-%d")
    try:
        import warrant_issue
        issue = warrant_issue.issued(list(hold), log) if hold else {}
    except Exception as exc:                 # 發行量讀不到：照常出圖，只是不算占發行、不標疑似
        log(f"分點權證盤中｜發行量略過｜{tools.err_text(exc)}")
        issue = {}
    rows = []
    for code, item in hold.items():
        quote = parse(items[code], today) if code in items else None
        row = dict(item, quote=quote, share=0.0, issue=issue.get(code),
                   issue_share=(item["lots"] / issue[code]) if issue.get(code) else None)
        if quote and quote["traded"] and item["lots"] > 0:
            row["share"] = quote["lots"] / item["lots"]
        row["suspect"] = suspect_sell(row)
        rows.append(row)
    traded = [r for r in rows if r["quote"] and r["quote"]["traded"]]
    traded.sort(key=lambda r: (-r["quote"]["value"], -r["quote"]["lots"]))   # 11-06 使用者：金額、張數高的優先
    return {"branches": branches, "focus": focus, "time": now.strftime("%H:%M"), "date": today,
            "stock": stock, "stock_name": stock_name,
            "holding_count": len(hold), "quoted": len(items), "rows": rows, "traded": traded}


def net_lots(quote: Dict[str, Any]) -> float:
    """估計買賣超（張）＝外盤－內盤（只算分得出內外盤的成交）。"""
    return float(quote.get("outside") or 0) - float(quote.get("inside") or 0)


def _badge(row: Dict[str, Any]) -> str:
    """一列只放一個標籤：疑似賣出優先，其次爆量。"""
    if row.get("suspect"):
        return "疑似賣出"
    q = row.get("quote") or {}
    return surge_text(q) if q.get("traded") else ""


def _holder_text(holders: List[Tuple[str, float]]) -> str:
    return holders[0][0] if len(holders) == 1 else f"{holders[0][0]} 等{len(holders)}家"


def card(res: Dict[str, Any]) -> Dict[str, Any]:
    """branch_card 格式（answer_image.branch_card）：摘要三格＋有成交的權證表＋一行聲明。"""
    traded = res["traded"]
    total = sum(r["quote"]["value"] for r in traded)
    stock_label = f"{res.get('stock_name') or res.get('stock')}" if res.get("stock") else ""
    if stock_label:
        title = f"{res['focus'] or '關注分點'}｜{stock_label} 權證今日動向"
    else:
        title = f"{res['focus']}｜權證盤中量能" if res["focus"] else "關注分點權證｜盤中量能"
    suspects = sum(1 for r in traded if r.get("suspect"))
    sections: List[Dict[str, Any]] = [{"type": "tiles", "items": [
        {"label": "今天有成交", "value": f"{len(traded)} / {res['holding_count']} 檔"},
        {"label": "成交金額合計", "value": tools._plain_money(total) if total else "0"},
        # 11-06：第三格改成「疑似分點賣出」檔數（偏買／偏賣檔數寫 Log）
        {"label": "疑似分點賣出", "value": f"{suspects} 檔", "tone": "down" if suspects else "ink"}]}]
    single = len(res["branches"]) == 1                 # 只看一家分點：「持有分點」欄不用列
    # 指定股票：沒成交的持有權證也列（「今天沒成交」也是答案）；不指定：只列有成交的
    listed = traded + ([r for r in res["rows"] if r not in traded] if stock_label else [])
    if listed:
        rows = []
        for r in listed[:TABLE_MAX]:
            q = r["quote"] if r["quote"] and r["quote"]["traded"] else None
            name = f"{r['name'] or (r['quote'] or {}).get('name') or r['warrant']} {r['warrant']}"
            if q:
                change = "-" if q["change_pct"] is None else f"{q['change_pct']:+.1f}%"
                net = net_lots(q)
                cells = [f"{q['lots']:,.0f}張 {tools._plain_money(q['value'])}", f"{r['share']:.0%}", change,
                         f"{net:+,.0f}張" if q["inside"] + q["outside"] else "-"]
            else:
                cells = ["今天沒成交", "-", "-", "-"]
            held = f"{r['lots']:,.0f}張" + (f" {r['issue_share']:.0%}" if r.get("issue_share") is not None else "")
            rows.append([name] + ([] if single else [_holder_text(r["holders"])]) + [held] + cells)
        columns = ["權證"] + ([] if single else ["持有分點"]) + ["分點持有（占發行）", "今日成交", "占持有", "漲跌", "估計買賣超"]
        widths = [2.9] + ([] if single else [1.8]) + [1.8, 2.1, 1.0, 1.1, 1.4]
        heading = (f"{stock_label} 持有中的權證（有成交的在前）" if stock_label else "有成交的權證（成交金額、張數大的在前）")
        sections += [{"type": "heading", "text": heading},
                     {"type": "table", "columns": columns, "rows": rows, "widths": widths,
                      "signed": ("漲跌", "估計買賣超"), "accent": (),
                      "row_badges": [_badge(r) for r in listed[:TABLE_MAX]]}]
        if len(listed) > TABLE_MAX:
            sections.append({"type": "note", "text": f"另有 {len(listed) - TABLE_MAX} 檔量較小，未列出", "keep": True})
    else:
        sections.append({"type": "paragraph", "text": f"關注分點持有的 {res['holding_count']} 檔權證，今天到 {res['time']} 都還沒有成交。"})
    sections.append({"type": "note", "keep": True,
                     "text": "占發行＝分點持有÷權證發行量（發行量含發行商沒賣出的部分，實際占投資人手上的比例更高）；"
                             "今日成交＝全市場成交；占持有＝今日成交張數÷分點持有張數；爆量＝盤中量已達昨天全天 2 倍；"
                             "估計買賣超＝外盤張數－內盤張數（權證多和發行商造市成交，外盤多大致是有人買進、內盤多大致是有人賣出）；"
                             f"疑似賣出＝分點持有占發行 {SUSPECT_ISSUE_SHARE:.0%} 以上、今天內盤達分點持有 {SUSPECT_INSIDE_SHARE:.0%}、"
                             "估計買賣超為負。盤中看不到分點，是不是這個分點在加碼或賣出，要等收盤分點資料確認。"})
    return {"branch": title, "label": f"盤中 {res['time']}", "sections": sections, "clean_display": False,
            "footer_text": "股市艾斯  /  權證盤中量能為奇摩即時報價，收盤前會變動"}


def log_text(res: Dict[str, Any]) -> str:
    leans = [lean(r["quote"])[1] for r in res["traded"]]
    buys = sum(1 for x in leans if x is not None and x >= LEAN_BUY)
    sells = sum(1 for x in leans if x is not None and x <= 1 - LEAN_BUY)
    lines = [f"分點權證盤中｜{res['time']}｜分點 {len(res['branches'])} 家｜持有權證 {res['holding_count']} 檔"
             f"｜有報價 {res['quoted']} 檔｜今天有成交 {len(res['traded'])} 檔｜偏買 {buys}／偏賣 {sells}"
             f"｜疑似賣出 {sum(1 for r in res['traded'] if r.get('suspect'))} 檔"]
    for r in res["traded"]:
        q = r["quote"]
        holders = "、".join(f"{b} {lots:,.0f}張" for b, lots in r["holders"])
        issue = (f"｜發行 {r['issue']:,.0f}張、占 {r['issue_share']:.0%}" if r.get("issue_share") is not None else "｜發行量不明")
        lines.append(f"- {r['name'] or q['name']}（{r['warrant']}）｜{holders}{issue}｜今日 {q['lots']:,.0f}張"
                     f"（昨 {q['prev_lots']:,.0f}張）{tools._plain_money(q['value'])}｜占持有 {r['share']:.0%}"
                     f"｜內 {q['inside']:,.0f}／外 {q['outside']:,.0f}｜{lean(q)[0]}｜最後成交 {q['time']}"
                     + ("｜疑似賣出" if r.get("suspect") else ""))
    return "\n".join(lines)
