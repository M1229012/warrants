"""11-08 使用者（「自己算成本」）：逐檔融資成本引擎——大盤／櫃買融資維持率改成「Σ個股融資市值 ÷ Σ個股估算本金」。

為什麼：玩股網等網站的大盤融資維持率不是用交易所公布的融資金額當分母，而是每一檔股票用融資成本估出本金再加總
（11/06 用它的個股維持率反推：上市 Σ市值 7,612.48 億 ÷ Σ本金 5,870.15 億 ＝ 129.68%，和它的大盤數字一致）。
我們不拿它的數字，自己從交易所每天公布的逐檔資料累計成本：

每檔每天（只用公開資料）：
  舊部位剩餘張數 = 前日融資餘額 − 今日融資賣出 − 今日現金償還
  成本總額      = 前日成本總額 × 剩餘張數 ÷ 前日融資餘額 ＋ 今日融資買進張數 × 1000 × 今日成交均價
  （成交均價＝成交金額 ÷ 成交股數；11-08 驗證上市 369 筆跨日觀測，平均誤差 0.0014 個百分點，用收盤價 0.11、只看淨張數 0.95）
  今日融資餘額和「剩餘＋買進」對不上（除權、資券互抵等）→ 成本總額等比例調整
  估算本金      = 成本總額 × 融資成數
  市值          = 今日融資餘額 × 1000 × 今日收盤價（沒成交用最近一次收盤）
大盤維持率 = Σ市值 ÷ Σ估算本金（扣除 ETF：代號 0 開頭；存託憑證 91 開頭也不算，網站不提供它們的個股維持率）。

限制（照實標示）：
- 起點成本拿不到 → 從很久以前開始重播（WARM_DAYS 個交易日後才當作可用），起點用當天收盤當成本，誤差隨換手遞減。
- 上櫃：11-08 研究時用同一套規則對不上玩股網的上櫃個股（誤差約 14 個百分點，原因未明），融資成數也未確認 → 上櫃結果標「未驗證」。
本模組只做計算；抓資料用 warrant_ai_tools._official_json（證交所／櫃買官方 JSON），存檔由呼叫端處理。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

FIN_RATIO = {"twse": 0.6, "tpex": 0.6}     # 估算本金用的融資成數（上櫃未驗證）
WARM_DAYS = 120                             # 重播滿這麼多個交易日才當作成本已收斂

TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=ALL&response=json"
TWSE_QUOTE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=ALLBUT0999&response=json"
TPEX_MARGIN = "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={date}&response=json"
TPEX_QUOTE = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date}&type=EW&response=json"


def _num(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def is_fund(code: str) -> bool:
    return str(code).strip()[:1] == "0"


def is_tdr(code: str) -> bool:
    return str(code).strip()[:2] == "91"


def _quotes(payload: Dict[str, Any], code_f: str, shares_f: str, amount_f: str, close_f: str,
            bid_f: str, ask_f: str) -> Dict[str, Dict[str, float]]:
    """{代號: {close, vwap}}：close 沒成交時用買賣中價（可能 0）；vwap＝成交金額 ÷ 成交股數（沒成交 0）。"""
    out: Dict[str, Dict[str, float]] = {}
    for table in payload.get("tables") or []:
        fields = [str(f).strip() for f in table.get("fields") or []]
        if code_f not in fields or close_f not in fields:
            continue
        ix = {name: fields.index(name) for name in (code_f, shares_f, amount_f, close_f, bid_f, ask_f) if name in fields}
        for row in table.get("data") or []:
            get = lambda name: _num(row[ix[name]]) if name in ix and len(row) > ix[name] else 0.0
            close = get(close_f)
            if close <= 0:
                bid, ask = get(bid_f), get(ask_f)
                close = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)
            shares, amount = get(shares_f), get(amount_f)
            out[str(row[ix[code_f]]).strip()] = {"close": close, "vwap": amount / shares if shares > 0 else 0.0}
    return out


def fetch_day(market: str, day: datetime, get_json) -> Optional[Dict[str, Dict[str, float]]]:
    """一天的逐檔資料 {代號: {prev, buy, sell, repay, lots, close, vwap}}；休市或還沒公布回 None。
    get_json(url, source) → dict（呼叫端傳 warrant_ai_tools._official_json，含櫃買中繼憑證處理）。"""
    if market == "tpex":
        date = day.strftime("%Y/%m/%d")
        table = ((get_json(TPEX_MARGIN.format(date=date), "TPEx").get("tables") or [{}])[0] or {})
        data = table.get("data") or []
        if not data:
            return None
        # 櫃買：代號、名稱、前資餘額、資買、資賣、現償、資餘額…
        margin = {str(r[0]).strip(): (_num(r[2]), _num(r[3]), _num(r[4]), _num(r[5]), _num(r[6])) for r in data if len(r) > 6}
        quotes = _quotes(get_json(TPEX_QUOTE.format(date=date), "TPEx"), "代號", "成交股數", "成交金額(元)", "收盤",
                         "最後買價", "最後賣價")
    else:
        date = day.strftime("%Y%m%d")
        payload = get_json(TWSE_MARGIN.format(date=date), "TWSE")
        tables = payload.get("tables") or []
        if payload.get("stat") != "OK" or len(tables) < 2:
            return None
        # 證交所：代號、名稱、融資 買進、賣出、現金償還、前日餘額、今日餘額…
        margin = {str(r[0]).strip(): (_num(r[5]), _num(r[2]), _num(r[3]), _num(r[4]), _num(r[6]))
                  for r in tables[1].get("data") or [] if len(r) > 6}
        quotes = _quotes(get_json(TWSE_QUOTE.format(date=date), "TWSE"), "證券代號", "成交股數", "成交金額", "收盤價",
                         "最後揭示買價", "最後揭示賣價")
    out: Dict[str, Dict[str, float]] = {}
    for code, (prev, buy, sell, repay, lots) in margin.items():
        q = quotes.get(code) or {}
        out[code] = {"prev": prev, "buy": buy, "sell": sell, "repay": repay, "lots": lots,
                     "close": q.get("close", 0.0), "vwap": q.get("vwap", 0.0)}
    return out


def step(state: Dict[str, list], rows: Dict[str, Dict[str, float]]) -> Dict[str, int]:
    """用一天的資料更新 state {代號: [融資張數, 成本總額（元）, 最近價格, 已累計天數]}；回傳統計（新開、重設）。"""
    stats = {"new": 0, "rebased": 0, "adjusted": 0}
    for code, r in rows.items():
        price = r["close"] or (state.get(code) or [0, 0, 0, 0])[2]
        vwap = r["vwap"] or price
        lots_prev, lots = r["prev"], r["lots"]
        s = state.get(code)
        if s is None:
            if lots <= 0 and lots_prev <= 0:
                continue
            # 第一次看到：前日部位的成本拿不到，先用今天的價格當成本（起點誤差，靠重播天數遞減）
            s = [lots_prev, lots_prev * 1000 * (price or vwap), price, 0]
            stats["new"] += 1
        held, cost = s[0], s[1]
        if held != lots_prev:
            # 和官方前日餘額對不上＝分割／減資／配股（11-08 實測 6949 面額變更 1 拆 20：5,315 張 → 106,072 張）：
            # 張數照官方，成本總額不變（每張成本跟著變）。原本保留每張成本，6949 的本金被放大 20 倍（多算 330 億）
            if held <= 0:
                cost = lots_prev * 1000 * (price or vwap)
            stats["rebased"] += 1
        remaining = max(0.0, lots_prev - r["sell"] - r["repay"])
        cost = (cost * remaining / lots_prev if lots_prev > 0 else 0.0) + r["buy"] * 1000 * vwap
        expected = remaining + r["buy"]
        if lots > 0 and expected > 0 and abs(lots - expected) > 0.5:
            cost *= lots / expected                  # 除權、資券互抵等：張數照官方，平均成本不變
            stats["adjusted"] += 1
        elif lots > 0 and expected <= 0:
            cost = lots * 1000 * (price or vwap)
        state[code] = [lots, cost if lots > 0 else 0.0, price or s[2], s[3] + 1]
    return stats


def aggregate(state: Dict[str, list], market: str) -> Dict[str, Any]:
    """Σ市值 ÷ Σ估算本金：全部（含 ETF、不含存託憑證）與扣除 ETF 兩種。"""
    f = FIN_RATIO.get(market, 0.6)
    acc = {"all": [0.0, 0.0, 0], "ex": [0.0, 0.0, 0]}
    for code, (lots, cost, price, _) in state.items():
        if lots <= 0 or cost <= 0 or price <= 0 or is_tdr(code):
            continue
        value, principal = lots * 1000 * price, cost * f
        for key in ("all",) + (() if is_fund(code) else ("ex",)):
            acc[key][0] += value
            acc[key][1] += principal
            acc[key][2] += 1
    out: Dict[str, Any] = {}
    for key, (value, principal, count) in acc.items():
        out[f"maint_{key}"] = round(value / principal * 100, 2) if principal > 0 else None
        out[f"value_{key}_yi"] = round(value / 1e8, 2)
        out[f"principal_{key}_yi"] = round(principal / 1e8, 2)
        out[f"count_{key}"] = count
    return out
