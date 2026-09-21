"""CMoney industry/concept catalog + intraday sector radar.

The module is deliberately defensive: CMoney is treated as an external HTML source, not an
assumed stable API. Data is cached to disk; any parse/network failure falls back to the last
validated cache and never blocks ordinary stock queries.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit

import pandas as pd

import warrant_ai_tools as tools

BASE = "https://www.cmoney.tw"
INDUSTRY_INDEX = BASE + "/finance/f00072.aspx"
# CMoney 新版公開頁亦有「類股總覽／概念股總覽」；保留舊 finance 頁作第一來源，
# 新版 forum 頁作備援。實際使用時只要其中一個可解析即可。
INDUSTRY_INDEX_URLS = [INDUSTRY_INDEX, BASE + "/forum/category"]
CONCEPT_INDEX_URLS = [BASE + "/forum/concept", BASE + "/finance/concept.aspx"]
GROUP_URLS_BY_KIND = {
    "concept": [BASE + "/forum/concept/{code}"],
    "industry": [BASE + "/forum/category/{code}", BASE + "/finance/f00072.aspx?b=1&t={code}&o=1"],
}
CACHE_SCHEMA_VERSION = 3
TTL = max(300, tools._env_int("DISCORD_AI_CMONEY_CATALOG_TTL", 43200))
RADAR_TTL = max(60, tools._env_int("DISCORD_AI_CMONEY_RADAR_TTL", 300))
TIMEOUT = max(2.0, tools._env_float("DISCORD_AI_CMONEY_TIMEOUT", 8.0))
EXPAND_WORKERS = max(1, min(12, tools._env_int("DISCORD_AI_CMONEY_EXPAND_WORKERS", 6)))
EXPAND_MAX_CANDIDATES = max(8, tools._env_int("DISCORD_AI_CMONEY_EXPAND_MAX_CANDIDATES", 60))
DEFAULT_PATH = "/data/cmoney_sector_catalog.json" if Path("/data").exists() else str(Path(__file__).parent / ".cache" / "cmoney_sector_catalog.json")
CACHE_PATH = Path(os.getenv("DISCORD_AI_CMONEY_CACHE", DEFAULT_PATH))
_LOCK = threading.RLock()
_MEM: Dict[str, Tuple[float, Any]] = {}
# 背景補齊成分股時連續失敗的族群（CMoney 頁面格式變動、族群已下架等），超過次數就跳過。
_MEMBER_WARM_FAILED: Dict[str, int] = {}
MEMBER_WARM_MAX_RETRY = max(1, tools._env_int("DISCORD_AI_CMONEY_WARM_MAX_RETRY", 3))

# Only spelling/market-language aliases. They do not silently broaden stock membership.
TEXT_NORMALIZE = {
    "那些族群": "哪些族群", "有那些": "有哪些", "形態": "型態", "形势": "型態", "型态": "型態",
    "記意體": "記憶體", "記億體": "記憶體", "記憶提": "記憶體",
}
# CPO is a market synonym/sub-theme frequently spoken alongside optical communication. We prefer
# a direct CPO group if CMoney exposes one; otherwise this is explicitly marked as a fallback.
FALLBACK_PARENT = {"CPO": "光通訊", "共同封裝光學": "光通訊", "CO-PACKAGEDOPTICS": "光通訊"}


def normalize_text(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "")).upper()
    for old, new in TEXT_NORMALIZE.items():
        value = value.replace(old.upper(), new.upper())
    return value


def _get(url: str) -> str:
    started = time.perf_counter()
    status = 0
    try:
        session = tools.core().get_thread_session()
        response = session.get(url, headers={"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "text/html,application/xhtml+xml"}, timeout=(4, TIMEOUT))
        status = int(response.status_code)
        response.raise_for_status()
        # CMoney 頁面一律 UTF-8；apparent_encoding 在部分環境會猜成 cp1252，族群名稱就會變亂碼。
        declared = (response.headers.get("Content-Type") or "").lower()
        response.encoding = "utf-8" if "charset=" not in declared else response.encoding
        return response.text
    finally:
        try:
            tools.record_api_event("CMoney", status=status, latency=time.perf_counter() - started)
        except Exception:
            pass


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self.options: List[Tuple[str, str]] = []
        self._href = ""
        self._parts: List[str] = []
        self._option = ""
        self._option_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._href, self._parts = attrs.get("href", ""), []
        elif tag == "option":
            self._option, self._option_parts = attrs.get("value", ""), []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)
        if self._option:
            self._option_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = re.sub(r"\s+", " ", "".join(self._parts)).strip()
            if text:
                self.links.append((self._href, text))
            self._href, self._parts = "", []
        elif tag == "option" and self._option:
            text = re.sub(r"\s+", " ", "".join(self._option_parts)).strip()
            if text:
                self.options.append((self._option, text))
            self._option, self._option_parts = "", []


def _code_from_url(value: str) -> str:
    try:
        parsed = urlsplit(urljoin(BASE, value))
        query = parse_qs(parsed.query)
        code = str((query.get("t") or [""])[0]).strip().upper()
        if re.fullmatch(r"C\d{4,8}", code):
            return code
        match = re.search(r"/(?:category|concept)/(C\d{4,8})(?:/|$)", parsed.path, re.I)
        return match.group(1).upper() if match else ""
    except Exception:
        return ""


def parse_catalog_html(html: str, kind: str) -> Dict[str, Dict[str, str]]:
    parser = LinkParser(); parser.feed(html)
    groups: Dict[str, Dict[str, str]] = {}
    candidates = list(parser.links) + list(parser.options)
    for raw, label in candidates:
        code = _code_from_url(raw) or (str(raw).strip().upper() if re.fullmatch(r"C\d{4,8}", str(raw).strip().upper()) else "")
        if not code or not re.fullmatch(r"C\d{4,8}", code):
            continue
        name = re.sub(r"^[\-–—\s]+|[\-–—\s]+$", "", re.sub(r"\([^)]*\)$", "", label)).strip()
        if len(name) < 2 or name in {"個別產業", "詳細", "更多"}:
            continue
        groups[code] = {"code": code, "name": name, "kind": kind}
    return groups


def _read_disk() -> Dict[str, Any]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        # v8 的成分股 parser 可能把頁面其他股票誤當成族群成分股。
        # 升級 schema 時保留族群目錄，但清掉舊 members，避免錯誤名冊延續。
        if int(data.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
            data = dict(data)
            data.pop("members", None)
            data["schema_version"] = CACHE_SCHEMA_VERSION
        return data
    except Exception:
        return {}


def _write_disk(data: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _age_seconds(data: Dict[str, Any]) -> float:
    try:
        stamp = datetime.fromisoformat(str(data.get("updated_at")))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except Exception:
        return float("inf")


def get_catalog(refresh: bool = False) -> Dict[str, Any]:
    key = "catalog"
    with _LOCK:
        cached = _MEM.get(key)
        if cached and not refresh and time.time() - cached[0] < TTL:
            return cached[1]
    disk = _read_disk()
    if not refresh and disk.get("groups") and _age_seconds(disk) < TTL:
        with _LOCK: _MEM[key] = (time.time(), disk)
        return disk
    groups: Dict[str, Dict[str, str]] = {}
    errors = []
    for kind, urls in (("industry", INDUSTRY_INDEX_URLS), ("concept", CONCEPT_INDEX_URLS)):
        kind_groups: Dict[str, Dict[str, str]] = {}
        for url in urls:
            try:
                parsed = parse_catalog_html(_get(url), kind)
                if parsed:
                    kind_groups.update(parsed)
                    # 一個來源已取得足夠分類就不再多打一個頁面。
                    if len(kind_groups) >= 10:
                        break
            except Exception as exc:
                errors.append(f"{kind}:{type(exc).__name__}")
        groups.update(kind_groups)
    if not groups:
        if disk.get("groups"):
            result = dict(disk, stale=True, errors=errors)
            with _LOCK: _MEM[key] = (time.time(), result)
            return result
        raise ValueError("CMoney 族群目錄無法解析")
    result = {"schema_version": CACHE_SCHEMA_VERSION, "updated_at": datetime.now(timezone.utc).isoformat(), "groups": groups, "stale": bool(errors), "errors": errors}
    _write_disk(result)
    with _LOCK: _MEM[key] = (time.time(), result)
    print(f"✅ CMoney 族群目錄：{len(groups)} 類｜errors={errors or '-'}", flush=True)
    return result


def _stock_from_text(text: str) -> Optional[Tuple[str, str]]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.search(r"(?<!\d)([1-9]\d{3})(?!\d)", text)
    if not m:
        return None
    code = m.group(1)
    name = re.sub(r"(?<!\d)" + re.escape(code) + r"(?!\d)", "", text)
    name = re.sub(r"[()（）|｜\-–—\s]+", " ", name).strip().split(" ")[0] if name else ""
    return code, name


def parse_members_html(html: str, expected_group_name: str = "") -> List[Dict[str, str]]:
    """只從真正的成分股表格解析股票；不再掃整頁任意連結。

    CMoney 頁面包含討論區、熱門股票與導覽連結，直接掃所有 <a> 會把無關股票
    （例如 2330）誤認成族群成分。這裡要求表格欄位明確包含「個股名稱／股票名稱」。
    """
    found: Dict[str, Dict[str, str]] = {}
    try:
        frames = pd.read_html(StringIO(html))
    except Exception:
        frames = []
    for frame in frames:
        columns = [str(c).strip() for c in frame.columns]
        name_col = next((c for c in frame.columns if any(k in str(c) for k in ("個股名稱", "股票名稱", "個股", "股票"))), None)
        if name_col is None:
            continue
        # 避免誤吃討論區或排行表：成分股表通常同時含股價/漲跌/成交量其中至少一欄。
        if not any(any(k in col for k in ("股價", "漲跌", "成交", "本益比")) for col in columns):
            continue
        for value in frame[name_col].tolist():
            row = _stock_from_text(str(value))
            if not row:
                continue
            code, name = row
            found.setdefault(code, {"stock_code": code, "stock_name": name or code, "market": ""})
    return sorted(found.values(), key=lambda r: r["stock_code"])



def _hidden_member_count(html: str) -> int:
    """CMoney 首屏通常只顯示 8 檔，若出現「查看其他 N 檔股票」就代表目前表格不是完整名冊。"""
    match = re.search(r"查看其他\s*([0-9,]+)\s*檔股票", str(html or ""), re.I)
    if not match:
        return 0
    try:
        return max(0, int(match.group(1).replace(",", "")))
    except ValueError:
        return 0


def _seed_candidates_for_group(group_name: str) -> List[Dict[str, str]]:
    """用既有細分名冊只當『候選池』，最後仍會逐檔回 CMoney 個股頁確認該 group code。

    這不是直接把大分類塞進來；候選只用來找 CMoney 首屏未展開的股票。
    """
    try:
        import fine_sector_catalog as fine_catalog
    except Exception:
        return []
    target = normalize_text(group_name)
    if not target:
        return []
    candidates: List[Dict[str, str]] = []
    for key, item in getattr(fine_catalog, "GROUPS", {}).items():
        if not item:
            continue
        name = str(item[0] or "")
        clean = normalize_text(name)
        # 允許「光通訊」↔「光通訊設備」這種非常接近的名稱，但不做大產業模糊擴張。
        close = clean == target or (min(len(clean), len(target)) >= 3 and (clean in target or target in clean) and abs(len(clean)-len(target)) <= 4)
        if not close:
            continue
        try:
            data = fine_catalog.get_members(key)
        except Exception:
            continue
        for stock in data.get("stocks") or []:
            code = str(stock.get("stock_code") or "").strip()
            if re.fullmatch(r"[1-9]\d{3}", code):
                candidates.append({"stock_code": code, "stock_name": str(stock.get("stock_name") or code), "market": str(stock.get("market") or "")})
    by_code = {s["stock_code"]: s for s in candidates}
    return [by_code[k] for k in sorted(by_code)]


def _stock_has_group_link(stock_code: str, group_code: str, kind: str) -> bool:
    """逐檔用 CMoney 個股頁的分類連結確認成分，避免討論區文字造成誤判。"""
    url = f"https://api.cmoney.tw/forum/stock/{stock_code}"
    html = _get(url)
    parser = LinkParser(); parser.feed(html)
    target = str(group_code or "").upper()
    for href, _label in parser.links:
        code = _code_from_url(href)
        if code == target:
            path = urlsplit(urljoin(BASE, href)).path.lower()
            if kind == "concept" and "/concept/" not in path:
                continue
            if kind == "industry" and "/category/" not in path and "f00072" not in path:
                continue
            return True
    return False


def _expand_incomplete_members(group: Dict[str, Any], visible: List[Dict[str, str]], expected_total: int) -> Tuple[List[Dict[str, str]], bool, str]:
    """CMoney 首屏不完整時，以本地細分名冊作候選，再逐檔回 CMoney 個股頁驗證 exact group code。

    若候選池不足或網路失敗，寧可標記 complete=False，也不假裝只抓到的 8 檔就是完整名冊。
    """
    code = str(group.get("code") or "").upper()
    kind = str(group.get("kind") or "industry")
    seed = _seed_candidates_for_group(str(group.get("name") or ""))[:EXPAND_MAX_CANDIDATES]
    known = {s["stock_code"]: dict(s) for s in visible}
    todo = [s for s in seed if s["stock_code"] not in known]
    if not todo:
        return list(known.values()), len(known) >= expected_total, "首屏未完整，沒有可用的本地候選池"
    matched: Dict[str, Dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=EXPAND_WORKERS, thread_name_prefix="cmoney-expand") as pool:
        futures = {pool.submit(_stock_has_group_link, s["stock_code"], code, kind): s for s in todo}
        for future in as_completed(futures):
            stock = futures[future]
            try:
                if future.result():
                    matched[stock["stock_code"]] = stock
            except Exception:
                continue
    known.update(matched)
    stocks = [known[k] for k in sorted(known)]
    complete = len(stocks) >= expected_total
    note = f"首屏 {len(visible)} 檔＋驗證補回 {len(matched)} 檔；頁面預期 {expected_total} 檔"
    return stocks, complete, note


def get_members(code: str, refresh: bool = False) -> Dict[str, Any]:
    code = str(code).upper().strip()
    catalog = get_catalog()
    group = (catalog.get("groups") or {}).get(code)
    if not group:
        raise ValueError(f"CMoney 找不到族群代碼 {code}")
    key = f"members:{code}"
    with _LOCK:
        cached = _MEM.get(key)
        if cached and not refresh and time.time() - cached[0] < TTL:
            return cached[1]
    disk = _read_disk()
    disk_members = ((disk.get("members") or {}).get(code) or {}) if isinstance(disk.get("members"), dict) else {}
    try:
        stocks: List[Dict[str, str]] = []
        last_error = None
        used_url = ""
        kind = str(group.get("kind") or "industry")
        templates = GROUP_URLS_BY_KIND.get(kind) or GROUP_URLS_BY_KIND["industry"]
        parse_complete = False
        expected_total = 0
        catalog_note = ""
        for template in templates:
            url = template.format(code=code)
            try:
                html = _get(url)
                candidate = parse_members_html(html, group.get("name", ""))
                if len(candidate) < 2:
                    continue
                hidden = _hidden_member_count(html)
                candidate_expected = len(candidate) + hidden
                if len(candidate) > len(stocks):
                    stocks, used_url, expected_total = candidate, url, candidate_expected
                if hidden == 0:
                    stocks, used_url, expected_total = candidate, url, len(candidate)
                    parse_complete = True
                    break
            except Exception as exc:
                last_error = exc
        if len(stocks) < 2:
            raise ValueError("CMoney 成分股頁面沒有解析到有效成分表") from last_error
        if not parse_complete and expected_total > len(stocks):
            expanded, parse_complete, note = _expand_incomplete_members(
                {**group, "code": code}, stocks, expected_total)
            if len(expanded) > len(stocks):
                stocks = expanded
            catalog_note = note
        result = {
            "industry": "cmoney:" + code,
            "name": group["name"],
            "stocks": stocks,
            "source": "CMoney",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "complete": bool(parse_complete),
            "expected_total": expected_total or len(stocks),
            "missing_count": max(0, (expected_total or len(stocks)) - len(stocks)),
            "missing_markets": [],
            "scope": group.get("kind", ""),
            "catalog_note": catalog_note,
            "source_url": used_url,
        }
        disk = disk if isinstance(disk, dict) else {}
        disk["schema_version"] = CACHE_SCHEMA_VERSION
        disk.setdefault("groups", catalog.get("groups") or {})
        disk.setdefault("members", {})[code] = result
        disk["updated_at"] = catalog.get("updated_at")
        _write_disk(disk)
        print(
            f"✅ 族群成分名冊：{code} {group['name']}｜kind={kind}｜stocks={len(stocks)}"
            f"｜expected={result['expected_total']}｜complete={result['complete']}｜url={used_url}"
            + (f"｜{catalog_note}" if catalog_note else ""),
            flush=True,
        )
    except Exception as exc:
        if disk_members.get("stocks"):
            result = dict(disk_members, complete=False, stale=True, catalog_note="")
            print(f"⚠️ CMoney 成分股更新失敗，使用快取：{code}｜{type(exc).__name__}", flush=True)
        else:
            raise
    with _LOCK:
        _MEM[key] = (time.time(), result)
    return result


def _aliases(groups: Dict[str, Dict[str, str]]) -> List[Tuple[str, str, str]]:
    rows = []
    for code, group in groups.items():
        name = normalize_text(group.get("name", ""))
        if name:
            rows.append((name, code, group.get("name", "")))
    return rows


def match_group(text: str) -> Optional[Dict[str, Any]]:
    """Return an exact/clear fuzzy CMoney group. Stock membership is never broadened silently."""
    clean = normalize_text(text)
    try:
        catalog = get_catalog()
    except Exception:
        return None
    groups = catalog.get("groups") or {}
    aliases = sorted(_aliases(groups), key=lambda row: -len(row[0]))
    exact = [row for row in aliases if row[0] and row[0] in clean]
    if exact:
        alias, code, name = exact[0]
        return {"code": code, "name": name, "match": "exact", "fallback": False}
    # Common market shorthand fallback only if CMoney does not expose the narrower label itself.
    for shorthand, parent in FALLBACK_PARENT.items():
        if shorthand in clean:
            direct = next((row for row in aliases if shorthand in row[0]), None)
            if direct:
                return {"code": direct[1], "name": direct[2], "match": "exact", "fallback": False}
            parent_match = next((row for row in aliases if normalize_text(parent) in row[0] or row[0] in normalize_text(parent)), None)
            if parent_match:
                return {"code": parent_match[1], "name": shorthand, "parent_name": parent_match[2], "match": "parent_fallback", "fallback": True}
    # Fuzzy only against plausible question chunks; require a unique margin.
    chunks = [c for c in re.split(r"族群|概念股|產業|類股|誰|哪|最|型態|排行|排名|今天|現在|比較|好|強", clean) if len(c) >= 2]
    if not chunks:
        return None
    scored = []
    for chunk in chunks:
        for alias, code, name in aliases:
            ratio = difflib.SequenceMatcher(None, chunk, alias).ratio()
            if ratio >= 0.72:
                scored.append((ratio, code, name, alias, chunk))
    scored.sort(reverse=True)
    if not scored:
        return None
    best = scored[0]
    second = scored[1][0] if len(scored) > 1 and scored[1][1] != best[1] else 0.0
    if best[0] < 0.76 or best[0] - second < 0.06:
        return None
    return {"code": best[1], "name": best[2], "match": "fuzzy", "fallback": False, "matched_text": best[4], "similarity": round(best[0], 3)}


def _parse_ranking_table(html: str, kind: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return out
    for df in tables:
        cols = [str(c).strip() for c in df.columns]
        if not any("分類" in c or "產業" in c or "概念" in c for c in cols):
            continue
        name_col = next((c for c in df.columns if any(k in str(c) for k in ("分類","產業","概念"))), df.columns[0])
        pct_col = next((c for c in df.columns if "一日" in str(c) or "漲幅" in str(c)), None)
        if pct_col is None:
            continue
        for _, row in df.iterrows():
            name = str(row.get(name_col, "")).strip()
            raw = str(row.get(pct_col, "")).replace("%", "").replace(",", "").strip()
            try: pct = float(raw)
            except Exception: continue
            if name and name.lower() != "nan":
                out.append({"name": name, "change_pct": pct, "kind": kind})
    return out


def get_live_radar(refresh: bool = False) -> Dict[str, Any]:
    key = "radar"
    with _LOCK:
        cached = _MEM.get(key)
        if cached and not refresh and time.time() - cached[0] < RADAR_TTL:
            return cached[1]
    rows: List[Dict[str, Any]] = []
    errors = []
    for kind, urls in (("industry", INDUSTRY_INDEX_URLS), ("concept", CONCEPT_INDEX_URLS)):
        kind_rows = []
        for url in urls:
            try:
                kind_rows = _parse_ranking_table(_get(url), kind)
                if kind_rows:
                    break
            except Exception as exc:
                errors.append(f"{kind}:{type(exc).__name__}")
        rows.extend(kind_rows)
    # Same label can appear in both pages; keep the freshest/first value and sort numerically.
    by_name = {}
    for row in rows:
        by_name.setdefault(row["name"], row)
    rows = sorted(by_name.values(), key=lambda r: (-r["change_pct"], r["name"]))
    result = {"rows": rows, "updated_at": datetime.now(timezone.utc).astimezone(tools.TAIPEI_TZ).strftime("%Y-%m-%d %H:%M"),
              "errors": errors, "complete": bool(rows)}
    with _LOCK: _MEM[key] = (time.time(), result)
    print(f"📡 CMoney 族群雷達：{len(rows)} 類｜errors={errors or '-'}", flush=True)
    return result


def get_cached_members(code: str) -> Optional[Dict[str, Any]]:
    """只讀已存在的成分股快取，不觸發網路。跨族群排行必須用這個，避免一次爬幾百頁。"""
    code = str(code).upper().strip()
    key = f"members:{code}"
    with _LOCK:
        cached = _MEM.get(key)
        if cached and (cached[1] or {}).get("stocks"):
            return cached[1]
    disk = _read_disk()
    row = ((disk.get("members") or {}).get(code) or {}) if isinstance(disk.get("members"), dict) else {}
    if row.get("stocks"):
        with _LOCK:
            _MEM[key] = (time.time(), row)
        return row
    return None


def cache_stats() -> Dict[str, int]:
    disk = _read_disk()
    groups = disk.get("groups") or {}
    members = disk.get("members") or {}
    return {
        "groups": len(groups) if isinstance(groups, dict) else 0,
        "member_groups": sum(1 for v in members.values() if isinstance(v, dict) and v.get("stocks")) if isinstance(members, dict) else 0,
    }


def warm_member_catalog_batch(limit: int = 1) -> Dict[str, int]:
    """低頻補齊 CMoney 細族群成分股名冊；每次只抓少量 group，不阻塞使用者查詢。"""
    try:
        catalog = get_catalog()
    except Exception:
        stats = cache_stats()
        return {"attempted": 0, "loaded": 0, **stats}
    groups = catalog.get("groups") or {}
    disk = _read_disk()
    existing = set((disk.get("members") or {}).keys()) if isinstance(disk.get("members"), dict) else set()
    # 連續失敗的族群要跳過，否則每一輪都卡在同一個代碼，後面的族群永遠補不到。
    missing = [code for code in groups if code not in existing and _MEMBER_WARM_FAILED.get(code, 0) < MEMBER_WARM_MAX_RETRY]
    attempted = loaded = 0
    for code in missing[:max(0, int(limit))]:
        attempted += 1
        try:
            if get_members(code, refresh=True).get("stocks"):
                loaded += 1
            _MEMBER_WARM_FAILED.pop(code, None)
        except Exception as exc:
            fails = _MEMBER_WARM_FAILED.get(code, 0) + 1
            _MEMBER_WARM_FAILED[code] = fails
            name = str((groups.get(code) or {}).get("name") or "")
            stop = "，不再重試" if fails >= MEMBER_WARM_MAX_RETRY else ""
            print(f"⚠️ CMoney 成分股背景補齊失敗：{code} {name}｜{type(exc).__name__}: {exc}｜第 {fails} 次{stop}", flush=True)
    stats = cache_stats()
    return {"attempted": attempted, "loaded": loaded, **stats}
