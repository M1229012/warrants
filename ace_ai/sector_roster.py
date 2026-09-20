"""族群成分名冊快照：一次建好、平常不連外。

族群成員變動很慢，所以名冊用「離線建一次、存成檔案」的方式維護：

    python ace_ai/sector_roster.py build          # 掃全市場，產生 sector_roster.json
    python ace_ai/sector_roster.py show 記憶體     # 檢查某個族群的成員

建立方式是「反查」：CMoney 沒有一次列出整個族群的端點，但每一檔股票的頁面會列出它屬於哪些
產業／概念族群。掃過全市場一次，就能同時得到所有族群的完整名單（含概念股、集團股）。

Bot 執行時只讀這個檔案，不再為了查族群去爬 CMoney；只有盤中族群漲幅雷達，
或管理員明確要求「更新族群名冊」時才會連外。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import warrant_ai_tools as tools

REPO_PATH = Path(__file__).parent / "sector_roster.json"
DATA_PATH = Path("/data/sector_roster.json")
ROSTER_PATH = Path(os.getenv("DISCORD_AI_SECTOR_ROSTER", str(DATA_PATH if Path("/data").exists() else REPO_PATH)))
STOCK_PAGE = "https://api.cmoney.tw/forum/stock/{code}"
HEADERS = {"User-Agent": "Mozilla/5.0 AceAI/1.0", "Accept": "text/html,application/xhtml+xml"}
BUILD_WORKERS = max(1, min(8, tools._env_int("DISCORD_AI_ROSTER_WORKERS", 6)))
BUILD_TIMEOUT = max(4.0, tools._env_float("DISCORD_AI_ROSTER_TIMEOUT", 15.0))
MIN_GROUP_SIZE = max(2, tools._env_int("DISCORD_AI_ROSTER_MIN_GROUP", 3))
_GROUP_LINK = re.compile(r"/forum/(category|concept)/(C\d{4,6})")
_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {}


# ============================================================
# 讀取（Bot 執行時只會用到這一段）
# ============================================================

def _load() -> Dict[str, Any]:
    with _LOCK:
        if _CACHE.get("data") is not None and _CACHE.get("path") == str(ROSTER_PATH):
            return _CACHE["data"]
    data: Dict[str, Any] = {}
    for path in (ROSTER_PATH, DATA_PATH, REPO_PATH):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("groups"):
                    break
        except Exception as exc:
            print(f"⚠️ 族群名冊讀取失敗：{path}｜{type(exc).__name__}", flush=True)
            data = {}
    with _LOCK:
        _CACHE["data"] = data if isinstance(data, dict) else {}
        _CACHE["path"] = str(ROSTER_PATH)
    return _CACHE["data"]


def reload() -> Dict[str, Any]:
    with _LOCK:
        _CACHE.pop("data", None)
    return _load()


def available() -> bool:
    return bool((_load().get("groups") or {}))


def built_at() -> str:
    return str(_load().get("built_at") or "")


def catalog() -> Dict[str, Dict[str, Any]]:
    """{code: {name, kind, size}}，不含成分股明細。"""
    groups = _load().get("groups") or {}
    return {code: {"name": g.get("name", code), "kind": g.get("kind", "industry"), "size": len(g.get("stocks") or [])}
            for code, g in groups.items()}


def group_names(kind: str = "") -> List[str]:
    return sorted({g["name"] for g in catalog().values() if not kind or g["kind"] == kind})


_NAME_TTL = 600.0


def _name_map() -> Dict[str, str]:
    """股票名冊：成功或失敗都記住 10 分鐘，避免整份名冊每個族群都重打一次 API。"""
    with _LOCK:
        cached = _CACHE.get("names")
        if cached and time.time() - cached[0] < _NAME_TTL:
            return cached[1]
    try:
        names = tools.get_stock_name_map() or {}
    except Exception:
        names = {}
    with _LOCK:
        _CACHE["names"] = (time.time(), names)
    return names


def _market_map() -> Dict[str, str]:
    """代號 → twse／tpex；先用本地底庫（含市場別），沒有就留空。"""
    with _LOCK:
        cached = _CACHE.get("markets")
    if cached is not None:
        return cached
    markets: Dict[str, str] = {}
    try:
        import local_market_cache
        for code in local_market_cache.codes_with_history(1):
            bars = local_market_cache.load_bars(code, limit=1)
            if bars and bars.get("market"):
                markets[code] = str(bars["market"])
    except Exception:
        markets = {}
    with _LOCK:
        _CACHE["markets"] = markets
    return markets


def get_members(code: str, display_name: str = "") -> Dict[str, Any]:
    groups = _load().get("groups") or {}
    key = str(code)
    if key.lower().startswith("multi:"):
        parts = [c.strip().upper() for c in key.split(":", 1)[1].split(",") if c.strip()]
        merged = [groups.get(c) for c in parts if groups.get(c)]
        if not merged:
            raise tools.ToolDataError(f"族群名冊沒有 {code}")
        group = {
            "name": display_name or "＋".join(str(g.get("name") or "") for g in merged),
            "kind": "merged",
            "stocks": sorted({s for g in merged for s in (g.get("stocks") or [])}),
        }
    else:
        group = groups.get(key.upper())
    if not group:
        raise tools.ToolDataError(f"族群名冊沒有 {code}")
    names, markets = _name_map(), _market_map()
    stocks = [{"stock_code": c, "stock_name": names.get(c, ""), "market": markets.get(c, "")}
              for c in group.get("stocks") or []]
    stocks = [s for s in stocks if re.fullmatch(r"[1-9]\d{3}", s["stock_code"])]
    return {
        "industry": "roster:" + (key if key.lower().startswith("multi:") else key.upper()),
        "name": group.get("name", code),
        "stocks": sorted(stocks, key=lambda s: s["stock_code"]),
        "source": "roster",
        "updated_at": str(_load().get("built_at") or "")[:10],
        "complete": True,          # 名冊是掃全市場建的，沒有「首屏只有 8 檔」的問題
        "missing_markets": [],
        "scope": group.get("kind", ""),
        "market_counts": {"twse": sum(s["market"] == "twse" for s in stocks),
                          "tpex": sum(s["market"] == "tpex" for s in stocks)},
    }


def _normalize(text: str) -> str:
    try:
        import cmoney_sector_catalog as cmoney_catalog
        return cmoney_catalog.normalize_text(text)
    except Exception:
        return re.sub(r"\s+", "", str(text or "")).upper()


_TOPIC_SUFFIX = re.compile(r"(族群|類股|概念股|概念|產業|個股|股票|股)+$")
# 問句裡的疑問／修飾用字：拿掉之後剩下的就是族群名（「散熱族群誰最強」→「散熱」）。
_QUESTION_WORDS = re.compile(
    r"(請問|幫我|目前|現在|今天|今日|盤中|收盤|最近|近期|哪一個|哪個|哪些|那些|誰|排行|排名|比較|最強|最好|最弱|"
    r"型態|形態|技術面|技術|漲幅|漲跌|漲最多|強勢|成分股|成分|名單|名冊|有哪些|有那些|是什麼|如何|怎樣|怎麼樣|"
    r"族群|類股|概念股|概念|產業|個股|股票|的|嗎|呢|吧|喔|啊)")


def _core_topic(value: str) -> str:
    return _TOPIC_SUFFIX.sub("", _QUESTION_WORDS.sub("", value)).strip()


def match_group(text: str) -> Optional[Dict[str, str]]:
    """從問題文字找族群。

    1. 完全相同（記憶體 → 記憶體）
    2. 族群名整個出現在問題裡，取最長的（「散熱零組件誰最強」→ 散熱零組件）
    3. 問題是族群名的簡稱（「散熱」→ 散熱零組件）：優先產業別、成分股多的
    """
    value = _normalize(text)
    if not value:
        return None
    entries = [(code, info["name"], _normalize(info["name"]), info["kind"], info["size"])
               for code, info in catalog().items()]
    short = _TOPIC_SUFFIX.sub("", value)
    core = _core_topic(value)
    for candidate in (value, short, core):
        if not candidate:
            continue
        for code, name, key, kind, _ in entries:
            if key and key == candidate:
                return {"code": code, "name": name, "kind": kind, "match": "exact"}
    hits = [(len(key), size, code, name, kind) for code, name, key, kind, size in entries
            if key and len(key) >= 2 and key in value]
    if hits:
        hits.sort(reverse=True)
        _, _, code, name, kind = hits[0]
        return {"code": code, "name": name, "kind": kind, "match": "contains"}
    for candidate in (short, core):
        if len(candidate) < 2:
            continue
        partial = [(not key.startswith(candidate), kind != "industry", -size, code, name, kind)
                   for code, name, key, kind, size in entries if key and candidate in key]
        if not partial:
            continue
        partial.sort()
        prefixed = [row for row in partial if not row[0]]
        # 「散熱」同時對到散熱零組件與散熱模組時，合併成一個族群一起比較，
        # 否則像奇鋐（在散熱模組）這種代表股會被排除在外。
        if len(prefixed) > 1:
            codes = [row[3] for row in prefixed]
            names = "＋".join(row[4] for row in prefixed)
            return {"code": "multi:" + ",".join(codes), "name": candidate, "kind": "merged",
                    "match": "merged", "merged_names": names}
        *_, code, name, kind = partial[0]
        return {"code": code, "name": name, "kind": kind, "match": "partial"}
    return None


# ============================================================
# 建立（離線維護工具；Bot 平常不會執行這一段）
# ============================================================

def _fetch_groups_for_stock(code: str) -> List[Tuple[str, str]]:
    session = tools.core().get_thread_session()
    response = session.get(STOCK_PAGE.format(code=code), headers=HEADERS, timeout=(4, BUILD_TIMEOUT))
    response.raise_for_status()
    found: List[Tuple[str, str]] = []
    for kind, group_code in _GROUP_LINK.findall(response.text):
        entry = ("concept" if kind == "concept" else "industry", group_code.upper())
        if entry not in found:
            found.append(entry)
    return found


def _group_names_from_cmoney() -> Dict[str, str]:
    try:
        import cmoney_sector_catalog as cmoney_catalog
        groups = (cmoney_catalog.get_catalog() or {}).get("groups") or {}
        return {str(code).upper(): str(info.get("name") or code) for code, info in groups.items()}
    except Exception as exc:
        print(f"⚠️ 取得 CMoney 族群名稱失敗，改用代碼當名稱｜{type(exc).__name__}", flush=True)
        return {}


def stock_universe() -> List[str]:
    """全市場普通股代號：先用官方股票名冊，取不到就用本地日K底庫。"""
    codes = [c for c in _name_map() if re.fullmatch(r"[1-9]\d{3}", str(c))]
    if codes:
        return sorted(codes)
    try:
        import local_market_cache
        return sorted(c for c in local_market_cache.codes_with_history(1) if re.fullmatch(r"[1-9]\d{3}", c))
    except Exception:
        return []


def build(codes: Optional[List[str]] = None, workers: int = BUILD_WORKERS,
          log: Callable[[str], None] = print, save: bool = True,
          partial_path: Optional[Path] = None) -> Dict[str, Any]:
    """掃描全市場，反查每一檔屬於哪些族群，組出完整名冊。"""
    started = time.monotonic()
    universe = [str(c) for c in (codes or stock_universe()) if re.fullmatch(r"[1-9]\d{3}", str(c))]
    if not universe:
        raise tools.ToolDataError("沒有可用的股票名冊，無法建立族群名冊")
    names = _group_names_from_cmoney()
    partial_path = partial_path or (ROSTER_PATH.parent / "sector_roster.partial.json")
    done: Dict[str, List[List[str]]] = {}
    if partial_path.exists():
        try:
            done = json.loads(partial_path.read_text(encoding="utf-8")).get("stocks") or {}
            log(f"沿用上次進度：已完成 {len(done):,} 檔")
        except Exception:
            done = {}
    todo = [c for c in universe if c not in done]
    log(f"開始建立族群名冊：全市場 {len(universe):,} 檔，待掃描 {len(todo):,} 檔，{workers} 條連線")
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="roster") as pool:
        futures = {pool.submit(_fetch_groups_for_stock, code): code for code in todo}
        for index, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                done[code] = [[kind, group] for kind, group in future.result()]
            except Exception as exc:
                failed += 1
                if failed <= 5:
                    log(f"  略過 {code}｜{type(exc).__name__}")
            if index % 100 == 0 or index == len(todo):
                log(f"  進度 {index:,}/{len(todo):,}｜失敗 {failed}｜已用 {time.monotonic()-started:.0f} 秒")
                try:
                    partial_path.parent.mkdir(parents=True, exist_ok=True)
                    partial_path.write_text(json.dumps({"stocks": done}, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
    groups: Dict[str, Dict[str, Any]] = {}
    for code, entries in done.items():
        for kind, group_code in entries:
            group = groups.setdefault(group_code, {"name": names.get(group_code, group_code), "kind": kind, "stocks": []})
            group["stocks"].append(code)
    groups = {code: {**g, "stocks": sorted(set(g["stocks"]))} for code, g in groups.items()
              if len(set(g["stocks"])) >= MIN_GROUP_SIZE}
    roster = {
        "built_at": datetime.now(timezone.utc).astimezone(tools.TAIPEI_TZ).strftime("%Y-%m-%d %H:%M"),
        "universe": len(universe), "scanned": len(done), "failed": failed,
        "groups": dict(sorted(groups.items())),
    }
    if save:
        ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROSTER_PATH.write_text(json.dumps(roster, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"已寫入 {ROSTER_PATH}")
        try:
            partial_path.unlink()
        except OSError:
            pass
    reload()
    log(f"完成：族群 {len(groups):,} 類｜掃描 {len(done):,} 檔｜失敗 {failed}｜{time.monotonic()-started:.0f} 秒")
    return roster


def main(argv: Optional[List[str]] = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="族群成分名冊（離線建立／查詢）")
    sub = parser.add_subparsers(dest="command")
    builder = sub.add_parser("build", help="掃描全市場建立名冊")
    builder.add_argument("--limit", type=int, default=0, help="只掃前 N 檔（試跑用）")
    builder.add_argument("--workers", type=int, default=BUILD_WORKERS)
    shower = sub.add_parser("show", help="檢查某個族群目前的成員")
    shower.add_argument("name")
    sub.add_parser("stats", help="顯示名冊統計")
    args = parser.parse_args(argv)

    if args.command == "build":
        codes = stock_universe()
        build(codes=codes[: args.limit] if args.limit else None, workers=args.workers)
        return
    if args.command == "show":
        found = match_group(args.name)
        if not found:
            print(f"找不到族群：{args.name}")
            return
        data = get_members(found["code"])
        print(f"{data['name']}（{found['code']}｜{data['scope']}）共 {len(data['stocks'])} 檔")
        print("、".join(f"{s['stock_name']}{s['stock_code']}" for s in data["stocks"]))
        return
    info = catalog()
    print(f"名冊建立時間：{built_at() or '尚未建立'}｜族群 {len(info)} 類")
    for kind in ("industry", "concept"):
        rows = [v for v in info.values() if v["kind"] == kind]
        print(f"  {kind}：{len(rows)} 類｜成分股中位數 {sorted(r['size'] for r in rows)[len(rows)//2] if rows else 0} 檔")


if __name__ == "__main__":
    main(sys.argv[1:])
