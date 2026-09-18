"""櫃買中心／證交所公開產業價值鏈細分名冊；包含上市、上櫃。

API 大產業分類無法提供細族群，因此僅對細族群補充公開產業鏈網頁。
股票名單由網頁取得；此處只維護分類／別名對照，不手寫成分股。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import warrant_ai_tools as tools


BASE_URL = "https://ic.tpex.org.tw/introduce.php?ic="
# (顯示名稱, 別名, (產業鏈頁碼, 子分類碼)...)，沒有任何手寫股票名單。
GROUPS = {
    "memory": ("記憶體", ("記憶體", "存儲器"), (("D000", "D150"), ("D000", "D160"), ("D000", "D320"), ("F000", "F500"))),
    "memory_ic": ("記憶體IC", ("記憶體IC",), (("D000", "D150"),)),
    "memory_controller": ("記憶體控制IC", ("記憶體控制IC", "記憶體控制器"), (("D000", "D160"),)),
    "dram": ("DRAM製造", ("DRAM", "DRAM製造"), (("D000", "D320"),)),
    "cooling": ("散熱", ("散熱", "散熱模組"), (("F000", "FB00"),)),
    "pcb": ("PCB製造", ("PCB", "印刷電路板", "PCB製造"), (("L000", "L610"),)),
    "ccl": ("銅箔基板", ("銅箔基板", "CCL"), (("L000", "L630"),)),
    "pcb_equipment": ("PCB設備", ("PCB設備", "PCB檢測設備"), (("L000", "L700"),)),
    "glass_cloth": ("玻纖布", ("玻纖布", "玻璃纖維"), (("L000", "L100"),)),
    "copper_foil": ("銅箔", ("銅箔",), (("L000", "L400"),)),
    "robot": ("機器人", ("機器人",), (("6000", "6310"), ("6000", "6320"), ("6000", "6330"))),
    "industrial_robot": ("工業型機器人", ("工業型機器人", "工業機器人"), (("6000", "6310"),)),
    "mobile_robot": ("AGV及AMR", ("AGV", "AMR", "移動機器人"), (("6000", "6320"),)),
    "service_robot": ("服務型及人型機器人", ("服務型機器人", "服務機器人", "人型機器人", "人形機器人"), (("6000", "6330"),)),
    "sensor": ("感測器", ("感測器",), (("6000", "6110"),)),
    "ic_design": ("IC設計", ("IC設計",), (("D000", "D100"),)),
    "ip_asic": ("IP設計／IC設計代工", ("IP設計", "ASIC", "矽智財"), (("D000", "DC00"),)),
    "foundry": ("IC／晶圓製造", ("晶圓製造", "晶圓代工", "IC製造"), (("D000", "D300"),)),
    "ic_test": ("IC封裝測試", ("IC封裝測試", "封裝測試", "封測"), (("D000", "D900"),)),
    "semi_equipment": ("半導體設備", ("半導體設備",), (("D000", "D400"), ("D000", "D600"))),
    "lead_frame": ("導線架", ("導線架",), (("D000", "D800"),)),
    "server": ("伺服器", ("伺服器",), (("F000", "FM00"),)),
    "ipc": ("工業電腦", ("工業電腦", "IPC"), (("F000", "FK00"),)),
    "power": ("電源供應器", ("電源供應器",), (("F000", "F800"),)),
    "case": ("機殼", ("機殼",), (("F000", "F700"),)),
    "motherboard": ("主機板", ("主機板",), (("F000", "F600"),)),
    "graphics": ("顯示卡", ("顯示卡",), (("F000", "FD00"),)),
    "optical": ("光學鏡片／鏡頭", ("光學鏡片", "光學鏡頭", "鏡頭"), (("F000", "FQ00"),)),
    "network": ("網路設備", ("網通", "網路設備"), (("I000", "I900"),)),
    "optical_comm": ("光通訊設備", ("光通訊",), (("I000", "IA00"),)),
}
# 這些名稱比來源子分類更窄，不自動冒充同義詞。
UNMAPPED = ("AI伺服器", "NAND", "DDR5", "DDR4", "HBM", "ABF", "載板", "軟板", "硬板",
            "CPO", "矽光子", "COWOS", "先進封裝", "低軌衛星", "金控", "AI概念", "人工智慧")
TTL = max(60, tools._env_int("DISCORD_AI_FINE_MEMBERS_TTL", 86400))
TIMEOUT = max(1.0, tools._env_float("DISCORD_AI_FINE_MEMBERS_TIMEOUT", 12.0))
SEED_PATH = Path(__file__).with_name("fine_sector_seed.json")
CACHE_PATH = Path(os.getenv("DISCORD_AI_FINE_CATALOG_CACHE", str(Path(__file__).parent / ".cache" / "fine_sector_catalog.json")))
_CACHE = tools.TTLCache("discord_ai_fine_catalog")
_DISK_LOCK = threading.Lock()


class CatalogError(ValueError):
    pass


class ChainParser(HTMLParser):
    """按 companyList 區塊與市場標題解析，不收錄正文／興櫃／知名外國企業。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.groups = {}
        self.depth = 0
        self.current = None
        self.market = ""
        self.bold = None
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            ident = attrs.get("id", "")
            if ident.startswith("companyList_") and self.current is None:
                self.current = {"id": ident[12:], "name": attrs.get("title", ""), "depth": self.depth,
                                "stocks": [], "expected": {"twse": 0, "tpex": 0}, "observed": {"twse": 0, "tpex": 0},
                                "declared": False}
                self.market = ""
        if self.current is None:
            return
        if tag == "b":
            self.bold = []
        if tag == "a" and self.market:
            url = urlsplit(attrs.get("href", ""))
            code = parse_qs(url.query).get("stk_code", [""])[0]
            if url.path.rsplit("/", 1)[-1] == "company_basic.php" and re.fullmatch(r"[1-9]\d{3}", code):
                self.anchor = {"stock_code": code, "market": self.market, "parts": [], "title": attrs.get("title", "")}

    def handle_data(self, data):
        if self.bold is not None:
            self.bold.append(data)
        if self.anchor is not None:
            self.anchor["parts"].append(data)

    def handle_endtag(self, tag):
        if self.current is not None:
            if tag == "b" and self.bold is not None:
                text = "".join(self.bold)
                match = re.search(r"(上市|上櫃)公司\s*[（(]\s*(\d+)\s*家\s*[）)]", text)
                if re.search(r"(?:公司|知名外國企業)\s*[（(]\s*\d+\s*家\s*[）)]", text):
                    self.current["declared"] = True
                self.market = {"上市": "twse", "上櫃": "tpex"}.get(match[1], "") if match else ""
                if match:
                    self.current["expected"][self.market] += int(match[2])
                    self.current["declared"] = True
                self.bold = None
            if tag == "a" and self.anchor is not None:
                row = self.anchor
                name = "".join(row["parts"]).strip() or row["title"].strip()
                if name:
                    self.current["stocks"].append({"stock_code": row["stock_code"], "stock_name": name, "market": row["market"]})
                    self.current["observed"][row["market"]] += 1
                self.anchor = None
            if tag == "div" and self.depth == self.current["depth"]:
                group = self.current
                group["valid"] = group["declared"] and group["expected"] == group["observed"]
                # 網頁後段 noscript 會重複名冊，不可重複計數。
                self.groups.setdefault(group["id"], group)
                self.current = None
                self.bold = self.anchor = None
                self.market = ""
        if tag == "div":
            self.depth -= 1


def parse_page(html):
    parser = ChainParser()
    parser.feed(html)
    # 半導體頁將記憶體IC、DRAM等更細分類放在獨立 table 中。
    names = dict(re.findall(r'id="sc_link_([A-Z0-9]+)"[^>]*><span>.*?</span>&nbsp;(.*?)&nbsp;', html))
    for markup, code in re.findall(r'(<table\s+id="sc_company_([A-Z0-9]+)"[^>]*>.*?</table>)', html, re.S):
        if code not in names or code in parser.groups:
            continue
        child = ChainParser()
        child.feed(f'<div id="companyList_{code}" title="{escape(unescape(names[code]), quote=True)}">{markup}</div>')
        parser.groups.update(child.groups)
    if not parser.groups:
        raise CatalogError("產業鏈網頁沒有可辨識的公司名冊")
    return {key: {field: value for field, value in group.items() if field not in ("depth", "id", "declared")}
            for key, group in parser.groups.items()}


def fetch_page(page):
    if not re.fullmatch(r"[A-Z0-9]{4}", page):
        raise CatalogError("無效的產業鏈頁碼")
    request = Request(BASE_URL + page, headers={"User-Agent": "AceAI-sector-catalog/1.0", "Accept": "text/html"})
    with urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(3_000_001)
    if len(raw) > 3_000_000:
        raise CatalogError("產業鏈網頁超過大小限制")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "source_url": BASE_URL + page,
            "groups": parse_page(raw.decode("utf-8-sig"))}


def _read_pages(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pages = data.get("pages", {}) if isinstance(data, dict) else {}
        return pages if isinstance(pages, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_page(page, data):
    # 快取寫入失敗不影響名冊查詢；備援種子資料永不覆寫。
    try:
        with _DISK_LOCK:
            pages = _read_pages(CACHE_PATH)
            pages[page] = data
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = CACHE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8")
            temp.replace(CACHE_PATH)
    except OSError:
        pass


def _age(data):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"])).total_seconds()
        return age if age >= 0 else float("inf")
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _load_page(page):
    def build():
        disk = _read_pages(CACHE_PATH).get(page)
        if disk and 0 <= _age(disk) < TTL:
            return dict(disk, stale=False)
        try:
            data = fetch_page(page)
            required = {block for _, _, refs in GROUPS.values() for p, block in refs if p == page}
            if any(not data["groups"].get(block, {}).get("valid") for block in required):
                raise CatalogError("上市櫃公司數量核對失敗")
            _save_page(page, data)
            return dict(data, stale=False)
        except Exception as exc:
            print(f"細分名冊網頁更新失敗：{page}｜{type(exc).__name__}", flush=True)
            options = [item for item in (disk, _read_pages(SEED_PATH).get(page)) if item]
            for fallback in sorted(options, key=_age):
                if fallback.get("groups"):
                    return dict(fallback, stale=True)
            raise CatalogError("細分名冊暫時無法取得，且沒有可用的已驗證名冊") from exc
    hit, data = _CACHE.get(page)
    if hit:
        return data
    # 共用同頁下載，失敗備援五分鐘後重試，不把網路失敗快取一天。
    data, _ = _CACHE.get_or_compute(page, 300, build)
    if not data.get("stale"):
        _CACHE.set(page, data, TTL)
    return data


def match_group(text):
    """優先長名稱；較窄但無對應分類的名稱不能套成較大細族群。"""
    text = re.sub(r"\s+", "", text).upper()
    if any(word.upper() in text for word in UNMAPPED):
        return None
    remaining = text
    matched = []
    aliases = sorted(((alias.upper(), key) for key, (_, names, _) in GROUPS.items() for alias in names), key=lambda item: -len(item[0]))
    for alias, key in aliases:
        pattern = r"(?<![A-Z])" + re.escape(alias) + r"(?![A-Z])" if alias.isascii() else re.escape(alias)
        if re.search(pattern, remaining):
            if key not in matched:
                matched.append(key)
            remaining = re.sub(pattern, "", remaining)
    return matched


def get_members(key):
    if key not in GROUPS:
        raise CatalogError("不支援的細分分類")
    title, _, refs = GROUPS[key]
    stocks, sources, categories, errors = {}, {}, [], []
    stale = False
    for page, block in refs:
        try:
            data = _load_page(page)
            group = data["groups"].get(block, {})
            if not group.get("valid"):
                raise CatalogError("細分類名冊缺少上市櫃市場或數量不符")
            for row in group["stocks"]:
                stocks[row["stock_code"]] = dict(row)
            sources[page] = {"url": data["source_url"], "fetched_at": data["fetched_at"]}
            categories.append(group["name"])
            stale = stale or data.get("stale", False)
        except Exception as exc:
            errors.append(f"{page}/{block}")
            print(f"細分類名冊略過：{page}/{block}｜{type(exc).__name__}", flush=True)
    if not sources:
        raise CatalogError("細分名冊沒有可用的上市櫃股票")
    rows = sorted(stocks.values(), key=lambda row: row["stock_code"])
    count = {market: sum(row["market"] == market for row in rows) for market in ("twse", "tpex")}
    return {"industry": "fine:" + key, "name": title, "stocks": rows, "source": "TPExIndustryChain",
            "updated_at": min(source["fetched_at"][:10] for source in sources.values()),
            "complete": not errors, "missing_markets": [], "missing_categories": errors,
            "market_counts": count, "stale": bool(stale), "source_urls": [s["url"] for s in sources.values()],
            "scope": "、".join(dict.fromkeys(categories)),
            "catalog_note": "依公開產業鏈分類合併上市及上櫃名單；涵蓋來源列出的製造、模組或供應鏈公司，不等同所有市場題材股。"
                            + (" 網頁暫時無法更新，目前使用已驗證的備援名冊，日期為取得日。" if stale else " 名冊日期為取得日。")}
