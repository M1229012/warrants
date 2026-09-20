"""族群名稱解析：全系統唯一入口。

設計原則（v15）：
- 只有一個地方決定「這句話指的是哪個族群」，不再讓多個模組各自猜。
- 名冊（sector_roster）是唯一的成分來源；同義詞表只負責把口語對到名冊名稱。
- 對不到就誠實回 None，由上層回「查不到，相近的是 …」，不做低信心的模糊猜測。
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sector_roster

ALIAS_PATH = Path(__file__).with_name("sector_aliases.json")
CUSTOM_PATH = Path(__file__).with_name("custom_sectors.json")
FUZZY_MIN = 0.85          # 編輯距離門檻；低於這個就當作查不到
FUZZY_MARGIN = 0.10       # 第一名與第二名的差距，避免兩個族群都像
_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {}

# 疑問詞／修飾詞：拿掉之後剩下的就是主題字（「散熱族群誰最強」→「散熱」）
_QUESTION_WORDS = re.compile(
    r"(請問|幫我|麻煩|目前|現在|今天|今日|盤中|收盤|最近|近期|這邊|哪一個|哪個|哪些|那些|哪幾|幾檔|"
    r"誰|排行|排名|前三名|前五名|前幾名|比較|相比|最強|最好|最弱|最差|強勢|弱勢|"
    r"型態|形態|技術面|技術|結構|均線|支撐|壓力|布林|漲幅|漲跌|漲最多|量能|爆量|"
    r"成分股|成分|名單|名冊|有哪些|有那些|有什麼|有啥|有誰|包含|屬於|是什麼|如何|怎樣|怎麼樣|好嗎|強嗎|"
    r"大量區|在哪裡|在哪|哪裡|多少|還是|還有|還|會不會|可不可以|能不能|要不要|注意|看|問|說|算|是|有|沒有|不|很|太|比|跟|和|與|"
    r"族群|類股|概念股|概念|產業|個股|股票|股|的|了|嗎|呢|吧|喔|啊|欸|一下)")
_PRONOUN = re.compile(r"^(它|他|她|這|那|這檔|那檔|這支|那支|該股|裡面|其中|第[一二三四五六七八九十\d]+名?)?$")
_TOPIC_TAIL = re.compile(r"(族群|類股|概念股|概念|產業|個股|股票|股)+$")


def _data() -> Dict[str, Any]:
    with _LOCK:
        if _CACHE.get("ready"):
            return _CACHE
    alias_raw: Dict[str, Any] = {}
    custom_raw: Dict[str, Any] = {}
    for path, target in ((ALIAS_PATH, "alias"), (CUSTOM_PATH, "custom")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:
            print(f"⚠️ 族群設定讀取失敗：{path.name}｜{type(exc).__name__}", flush=True)
            payload = {}
        if target == "alias":
            alias_raw = payload
        else:
            custom_raw = payload
    with _LOCK:
        _CACHE.update({
            "ready": True,
            "aliases_raw": dict(alias_raw.get("aliases") or {}),
            "typos": dict(alias_raw.get("typos") or {}),
            "exclude": {str(x) for x in (alias_raw.get("exclude_groups") or [])},
            "custom": dict(custom_raw.get("groups") or {}),
        })
    return _CACHE


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
    sector_roster.reload()


def normalize(text: str) -> str:
    """全半形、空白、分隔符號與常見錯字一次處理掉。"""
    value = str(text or "")
    value = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in value)
    for wrong, right in (_data().get("typos") or {}).items():
        value = value.replace(wrong, right)
    value = re.sub(r"[\s/\\\-_·・、,，.。+＋&]", "", value)
    return value.upper()


def _alias_index() -> Dict[str, List[str]]:
    """同義詞表的鍵先正規化；normalize 需要 typos，所以這一步和讀檔分開，避免互相呼叫。"""
    with _LOCK:
        cached = _CACHE.get("aliases")
    if cached is not None:
        return cached
    index = {normalize(k): list(v) for k, v in (_data().get("aliases_raw") or {}).items()}
    with _LOCK:
        _CACHE["aliases"] = index
    return index


def core_topic(text: str) -> str:
    """去掉疑問詞與「族群／股票」尾綴之後剩下的主題字。"""
    return _TOPIC_TAIL.sub("", _QUESTION_WORDS.sub("", normalize(text))).strip()


def has_new_topic(text: str) -> bool:
    """句子裡是否出現「新主題」（不是純指代句）；追問記憶用這個守門。"""
    rest = core_topic(text)
    return len(rest) >= 2 and not _PRONOUN.match(rest)


# ============================================================
# 名冊索引
# ============================================================

def _index() -> List[Tuple[str, str, str, str, int]]:
    """(正規化名稱, 族群代碼, 顯示名稱, 類別, 成分股數)。"""
    exclude = _data().get("exclude") or set()
    rows = []
    for code, info in sector_roster.catalog().items():
        name = str(info.get("name") or "")
        if not name or name in exclude:
            continue
        rows.append((normalize(name), code, name, info.get("kind", ""), int(info.get("size") or 0)))
    return rows


def _codes_for_names(names: List[str]) -> Tuple[List[str], List[str]]:
    """把同義詞表寫的名稱清單換成名冊代碼；找不到的名稱直接略過。"""
    index = {key: code for key, code, *_ in _index()}
    codes, hit_names = [], []
    for name in names:
        key = normalize(name)
        code = index.get(key)
        if code and code not in codes:
            codes.append(code)
            hit_names.append(name)
    return codes, hit_names


def _pack(codes: List[str], display: str, confidence: str, merged: str = "") -> Dict[str, Any]:
    if len(codes) == 1:
        industry = "roster:" + codes[0]
    else:
        industry = "roster:multi:" + ",".join(codes)
    result = {"industry": industry, "name": display, "confidence": confidence}
    if merged:
        result["merged_names"] = merged
    return result


def _custom(name: str, confidence: str) -> Dict[str, Any]:
    return {"industry": "custom:" + name, "name": name, "confidence": confidence}


# ============================================================
# 主要入口
# ============================================================

def match(text: str) -> Optional[Dict[str, Any]]:
    """從問句找族群；對不到回 None（上層要誠實說查不到）。"""
    value = normalize(text)
    if not value:
        return None
    core = core_topic(text) or value
    aliases = _alias_index()
    customs = _data().get("custom") or {}

    # 1. 自訂族群（名冊沒有的：AI伺服器／重電／儲能）
    for name, info in customs.items():
        keys = [normalize(name)] + [normalize(a) for a in (info.get("aliases") or [])]
        if core in keys:
            return _custom(name, "exact")
        if any(k and k in value for k in keys):
            return _custom(name, "alias")

    # 2. 同義詞表：先比主題字，再比整句
    for candidate, confidence in ((core, "exact"), (value, "alias")):
        target = aliases.get(candidate)
        if target is None and confidence == "alias":
            hits = sorted((k for k in aliases if len(k) >= 2 and k in value), key=len, reverse=True)
            target = aliases[hits[0]] if hits else None
        if not target:
            continue
        if any(str(t).startswith("__custom__") for t in target):
            return _custom(str(target[0]).replace("__custom__", ""), confidence)
        codes, names = _codes_for_names([str(t) for t in target])
        if codes:
            merged = "＋".join(names) if len(codes) > 1 else ""
            display = names[0] if len(codes) == 1 else (core or value)
            return _pack(codes, display, "merged" if len(codes) > 1 else confidence, merged)

    index = _index()
    # 3. 名冊名稱完全相同
    for key, code, name, _, _ in index:
        if key == core or key == value:
            return _pack([code], name, "exact")
    # 4. 名冊名稱整個出現在問句裡（取最長的）
    contains = sorted(((len(key), size, code, name) for key, code, name, _, size in index
                       if len(key) >= 2 and key in value), reverse=True)
    if contains:
        same = [row for row in contains if row[0] == contains[0][0]]
        codes = [row[2] for row in same]
        if len(codes) > 1:  # 同名兩類（電源供應器＝產業＋概念）取聯集
            return _pack(codes, same[0][3], "merged", "＋".join(dict.fromkeys(r[3] for r in same)))
        return _pack([contains[0][2]], contains[0][3], "alias")
    # 5. 主題字是族群名的一部分（散熱 → 散熱模組＋散熱零組件）
    if len(core) >= 2:
        partial = [(code, name) for key, code, name, _, _ in index if core in key]
        if partial:
            codes = [c for c, _ in partial]
            if len(codes) > 1:
                return _pack(codes, core, "merged", "＋".join(n for _, n in partial))
            return _pack(codes, partial[0][1], "alias")
    # 6. 最後才用編輯距離，且要求夠像、夠唯一
    if len(core) >= 2:
        scored = sorted(((difflib.SequenceMatcher(None, core, key).ratio(), code, name)
                         for key, code, name, _, _ in index), reverse=True)
        if scored and scored[0][0] >= FUZZY_MIN:
            second = scored[1][0] if len(scored) > 1 else 0.0
            if scored[0][0] - second >= FUZZY_MARGIN:
                return _pack([scored[0][1]], scored[0][2], "fuzzy")
    return None


def suggest(text: str, limit: int = 3) -> List[str]:
    """查不到時，給幾個最像的族群名稱當提示。"""
    core = core_topic(text)
    if len(core) < 2:
        return []
    scored = sorted(((difflib.SequenceMatcher(None, core, key).ratio(), name)
                     for key, _, name, _, _ in _index()), reverse=True)
    return [name for ratio, name in scored[:limit] if ratio >= 0.4]


def custom_members(name: str) -> Dict[str, Any]:
    """自訂族群的成分股（格式與 sector_roster.get_members 相同）。"""
    info = (_data().get("custom") or {}).get(name)
    if not info:
        raise KeyError(name)
    codes = [str(c) for c in (info.get("stocks") or []) if re.fullmatch(r"[1-9]\d{3}", str(c))]
    names = sector_roster._name_map()
    markets = sector_roster._market_map()
    stocks = [{"stock_code": c, "stock_name": names.get(c, ""), "market": markets.get(c, "")} for c in codes]
    return {
        "industry": "custom:" + name, "name": name, "stocks": sorted(stocks, key=lambda s: s["stock_code"]),
        "source": "custom", "updated_at": "", "complete": True, "missing_markets": [], "scope": "custom",
        "market_counts": {"twse": sum(s["market"] == "twse" for s in stocks),
                          "tpex": sum(s["market"] == "tpex" for s in stocks)},
    }


def groups_of(stock_code: str) -> List[str]:
    """反查：這一檔屬於哪些族群（含自訂族群）。"""
    code = str(stock_code).strip()
    exclude = _data().get("exclude") or set()
    found = [str(info.get("name") or "") for info in (sector_roster._load().get("groups") or {}).values()
             if code in (info.get("stocks") or [])]
    found += [name for name, info in (_data().get("custom") or {}).items()
              if code in [str(c) for c in (info.get("stocks") or [])]]
    return sorted({n for n in found if n and n not in exclude})


# ============================================================
# 動作判斷（要名單、要排行、還是要看型態）
# ============================================================

_RANK_RE = re.compile(r"最強|最好|最弱|最差|排行|排名|前[一二三四五六七八九十\d]+名|誰比較|哪一?檔比較|強勢股")
_LIST_RE = re.compile(r"成分|名單|名冊|有哪些|有那些|有什麼|有啥|有誰|哪些股|哪幾[檔支]|包含|組成|誰在裡面|(?:族群|類股|概念股|產業)(?:股票|個股|有誰)")
_BELONG_RE = re.compile(r"屬於|是不是.*(族群|類股)|算不算|歸類|哪些族群|什麼族群|哪個族群")
_BLOCK_RE = re.compile(r"分點|籌碼|買超|賣超|新聞|營收|基本面|estimate|便宜|估值|勝率|權證")


def action_of(text: str, default: str = "technical") -> str:
    """members（要名單）／technical（型態排行）／momentum（漲幅）／belongs（反查）／blocked（不支援）。"""
    value = normalize(text)
    if _BELONG_RE.search(value):
        return "belongs"
    if _BLOCK_RE.search(value):
        return "blocked"
    if _RANK_RE.search(value):
        return "momentum" if re.search(r"漲幅|漲跌|漲最|今天最強|盤中", value) and not re.search(r"型態|結構|技術", value) else "technical"
    if _LIST_RE.search(value):
        return "members"
    if re.search(r"型態|結構|技術|均線|支撐|壓力|布林|好嗎|強嗎|怎樣|如何|怎麼看", value):
        return "technical"
    if re.search(r"漲幅|漲跌|漲最|強勢|盤中", value):
        return "momentum"
    return default
