"""路由題庫用的離線走訪器：真實的解析器、路由、追問記憶、權限，資料工具與 Gemini 全部替換掉。

route_of(question) 回傳這題實際會走的路線（route、股票、分點、工具），不連網、不呼叫 Gemini。
test_routing_corpus.py 用它跑 eval/routing_corpus.txt；也可以直接執行本檔印出整份題庫的結果：
    python routing_harness.py
"""
from __future__ import annotations

import html
import re
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
from unittest.mock import Mock, patch

import discord_access as policy
import discord_ai_bot as bot

CORPUS = Path(__file__).with_name("eval") / "routing_corpus.txt"

# 題庫用到的股票（正式環境是官方＋FinMind 名冊，這裡只放題庫會出現的）
NAME_MAP = {
    "2330": "台積電", "2344": "華邦電", "2303": "聯電", "2454": "聯發科", "3006": "晶豪科", "2409": "友達",
    "6693": "廣閎科", "3042": "晶技", "7788": "松川精密", "1727": "中華化", "8103": "瀚荃", "3016": "嘉晶",
    "3167": "大量", "4576": "大銀微系統", "6683": "雍智科技", "6212": "理銘", "2301": "光寶科", "1301": "台塑",
    "1435": "中福", "3661": "世芯-KY", "2408": "南亞科", "3017": "奇鋐", "2317": "鴻海", "2382": "廣達",
    "00981A": "主動統一台股增長", "0050": "元大台灣50", "2603": "長榮", "2881": "富邦金", "2891": "中信金",
    "3037": "欣興", "8046": "南電", "3189": "景碩", "8299": "群聯", "2337": "旺宏", "2308": "台達電",
    "3231": "緯創", "2356": "英業達", "6669": "緯穎", "2002": "中鋼", "1216": "統一",
}
WARRANT_BRANCHES = ("永豐金內湖", "第一金中壢", "國票台南", "華南永昌台中", "元大南屯", "凱基台北", "第一金安和",
                    "富邦敦南", "新光", "元大土城永寧")
SPOT_BRANCHES = ("美林", "摩根大通", "凱基台北", "元大土城永寧", "富邦建國", "港商野村", "永豐金內湖")

ACCESS = {
    "both": policy.UserEntitlement(general=True, spot=True, warrant=True),         # 已訂閱＋權證N
    "spot": policy.UserEntitlement(general=True, spot=True),                       # 只有已訂閱
    "warrant": policy.UserEntitlement(general=True, warrant=True),                 # 只有權證N
}


def _normalize(name: str) -> str:
    s = html.unescape(str(name or "")).strip().replace("臺", "台")
    s = re.sub(r"[\s　\-_‐-―/\\|｜·．・•]+", "", s)
    return re.sub(r"[()（）［］\[\]{}｛｝]+", "", s)


def _code_key(code) -> str:
    s = re.sub(r"\s+", "", str(code or "").strip().upper())
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


class _Stop(Exception):
    """走到要抓資料那一步就停下來，帶回路由結果。"""

    def __init__(self, route: str, tools: List[str]):
        super().__init__(route)
        self.route, self.tools = route, tools


@dataclass
class Route:
    route: str
    stocks: List[str] = field(default_factory=list)
    branches: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    text: str = ""
    cost: Optional[float] = None

    def __str__(self) -> str:
        extra = [",".join(self.stocks), ",".join(self.branches)]
        return f"{self.route}｜{'｜'.join(x or '-' for x in extra)}｜{' '.join(sorted(set(self.tools)))}"


class Harness:
    def __init__(self) -> None:
        self._stack = ExitStack()
        core = SimpleNamespace(normalize_branch_name=_normalize, _normalize_stock_name_code_key=_code_key)
        branches = {b: b for b in WARRANT_BRANCHES}
        p = self._stack.enter_context
        p(patch.object(bot.tools, "core", return_value=core))
        p(patch.object(bot.tools, "get_stock_name_map", return_value=dict(NAME_MAP)))
        p(patch.object(bot.tools, "get_known_branches", return_value=branches))
        p(patch.object(bot.tools, "get_cached_known_branches", return_value=branches))
        p(patch.object(bot.tools, "resolve_branch", side_effect=self._resolve_branch))
        p(patch.object(bot.spot_chip, "known_branch_names", return_value=list(SPOT_BRANCHES)))
        p(patch.object(bot.local_market_cache, "log_usage"))
        p(patch.object(bot.local_market_cache, "get_state", side_effect=lambda k, d=None: d))
        p(patch.object(bot.local_market_cache, "set_state"))
        p(patch.object(bot, "INTENT_FALLBACK_ENABLE", False))
        p(patch.object(policy, "SECTOR_OPEN", True))        # 題庫測的是路由，不是族群鎖
        # 族群名冊固定用程式內附的 sector_roster.json（其他測試會把路徑改到暫存名冊）
        bundled = Path(bot.sector_roster.__file__).with_name("sector_roster.json")
        for attr in ("ROSTER_PATH", "DATA_PATH", "REPO_PATH"):
            p(patch.object(bot.sector_roster, attr, bundled))
        bot.sector_match.reload()
        self._stack.callback(bot.sector_match.reload)
        config = bot.BotConfig.from_env()
        config.planner_enabled = False
        self.engine = bot.AceQueryEngine.__new__(bot.AceQueryEngine)
        e = self.engine
        self._logged_route = ""

        def log(message, *a, **k):
            # 引擎自己印的「路由=xxx｜…」就是這題最後的路由（含不經過 router.plan 的法人題）
            found = re.match(r"路由=([^｜]+)｜", str(message))
            if found:
                self._logged_route = found.group(1)
        e.config, e.log = config, log
        e.parser, e.gateway = bot.QuestionParser(), Mock()
        e.gateway.generate.return_value = bot.GeminiResult(ok=False, text="", error="offline")
        e.router = bot.QueryRouter(e.gateway, config, e.log)
        e._answer_cache = bot.tools.TTLCache("routing_harness")
        e.memory = bot.ConversationMemory()
        e._slots = threading.BoundedSemaphore(8)
        e._weekly_lock, e._weekly_draft_lock = threading.Lock(), threading.Lock()
        e._weekly_drafts, e._queue_lock, e._inflight = {}, threading.Lock(), {}
        e._pending, e._quota_policy, e._request_local = 0, None, threading.local()
        e._load_draft_session = lambda *a, **k: {}
        e._run_tools = self._stop_at_tools
        e._answer_sector = lambda request, started: bot.AnswerResult("", "rule_sector", 0, 0.0)
        e._answer_radar = lambda direction, started, route, **k: bot.AnswerResult("", route, 0, 0.0)
        e._answer_chip = lambda chip, parsed, question, started: bot.AnswerResult("", f"chip_{chip}", 0, 0.0)
        e._answer_review = lambda *a, **k: bot.AnswerResult("", "trade_review", 0, 0.0)
        self._last_parsed: Optional[bot.ParsedQuestion] = None
        self._last_plan: Optional[bot.QueryPlan] = None
        original_parse, original_plan = e.parser.parse, e.router.plan

        def parse(*a, **k):
            self._last_parsed = original_parse(*a, **k)
            return self._last_parsed

        def plan(parsed, stats):
            self._last_plan = original_plan(parsed, stats)
            return self._last_plan
        e.parser.parse, e.router.plan = parse, plan

    @staticmethod
    def _resolve_branch(text: str) -> Tuple[str, List[str]]:
        key = _normalize(text)
        hits = [b for b in WARRANT_BRANCHES if b.startswith(key) or key.startswith(b)]
        return (hits[0], []) if len(hits) == 1 else ("", hits)

    def _stop_at_tools(self, calls):
        raise _Stop("", [c.name for c in calls])

    def close(self) -> None:
        self._stack.close()

    def route_of(self, question: str, user: str = "u1", access: str = "both", entry: str = "ask") -> Route:
        ent = ACCESS[access]
        ctx = policy.AccessContext(ent, entry)
        self._last_parsed = self._last_plan = None
        self._logged_route = ""
        try:
            result = self.engine.answer(question, f"g:c:{user}", access=ctx)
            route, tools, text = result.route, [], result.text
        except _Stop as stop:
            # 停在抓資料那一步：rule_institutional 不經過 router.plan，其餘看 plan
            route, tools, text = (self._logged_route or (self._last_plan.route if self._last_plan else "?")), stop.tools, ""
            if self._last_parsed is not None:     # 正式流程答完會記住這題（追問用）
                self.engine.memory.update(ctx.memory_key(f"g:c:{user}"), self._last_parsed)
        parsed = self._last_parsed
        return Route(route=route, text=text, tools=[t for t in tools if t != "get_chart_panel"],
                     stocks=[c for c, _ in parsed.stocks] if parsed else [],
                     branches=list(parsed.branches) if parsed else [],
                     cost=parsed.cost_price if parsed else None)


def load_corpus(path: Path = CORPUS) -> List[Dict[str, object]]:
    """每行：問題 | 期望路由 | 期望股票（逗號，可空）| 其他檢查（空白分隔：+工具 -工具 @權限 分點=名稱 成本=數字 >上一題）"""
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        question, route = parts[0], parts[1]
        stocks = [s for s in (parts[2] if len(parts) > 2 else "").split(",") if s]
        checks = (parts[3] if len(parts) > 3 else "").split()
        cases.append({"line": number, "question": question, "route": route, "stocks": stocks, "checks": checks})
    return cases


def check_case(harness: Harness, case: Dict[str, object], user: str) -> Tuple[Route, List[str]]:
    checks = list(case["checks"])
    access = next((c[1:] for c in checks if c.startswith("@")), "both")
    for prev in [c[1:] for c in checks if c.startswith(">")]:
        harness.route_of(prev.replace("_", " "), user=user, access=access)      # 先問上一題（追問記憶）
    got = harness.route_of(str(case["question"]), user=user, access=access)
    problems = []
    routes = str(case["route"]).split("/")
    if got.route not in routes:
        problems.append(f"路由 {got.route}（期望 {case['route']}）")
    if case["stocks"] != ["*"] and got.stocks != case["stocks"] and not (case["stocks"] == [] and got.route in ("help", "clarify")):
        problems.append(f"股票 {got.stocks}（期望 {case['stocks']}）")
    for c in checks:
        if c.startswith("+") and c[1:] not in got.tools:
            problems.append(f"缺工具 {c[1:]}")
        elif c.startswith("-") and c[1:] in got.tools:
            problems.append(f"不該有工具 {c[1:]}")
        elif c.startswith("分點=") and c[3:] not in got.branches:
            problems.append(f"分點 {got.branches}（期望 {c[3:]}）")
        elif c.startswith("成本=") and got.cost != float(c[3:]):
            problems.append(f"成本 {got.cost}（期望 {c[3:]}）")
    return got, problems


if __name__ == "__main__":
    h = Harness()
    try:
        failed = 0
        for i, case in enumerate(load_corpus()):
            got, problems = check_case(h, case, user=f"u{i}")
            mark = "✅" if not problems else "❌"
            failed += bool(problems)
            print(f"{mark} {case['question']}  →  {got}" + (f"  ⟵ {'；'.join(problems)}" if problems else ""))
        print(f"\n{len(load_corpus()) - failed}/{len(load_corpus())} 通過")
    finally:
        h.close()
