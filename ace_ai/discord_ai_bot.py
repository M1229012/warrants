"""艾斯 AI Discord 問答機器人（私人測試版）。

流程：
    Discord `!ace 問題`
    → 權限／頻道／冷卻檢查
    → 規則式實體與意圖解析（必要時才用 Gemini Planner）
    → warrant_ai_tools 取得並篩選資料（重用週報主程式函式）
    → 精簡 JSON → Gemini 最終回答（簡單查詢直接由 Python 排版，不呼叫 Gemini）
    → 數字核對 → 一頁式圖片回覆 Discord

本機測試（不連 Discord）：
    python discord_ai_bot.py --ask "2344現在技術面怎麼樣"
    python discord_ai_bot.py --plan "2344現在技術面怎麼樣"     # 只看路由，不抓資料

Railway 正式測試：
    python discord_ai_bot.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import warrant_ai_tools as tools
import weekly_pick
import answer_image
import weekly_image
from weekly_pick import is_weekly_pick_question


# ============================================================
# 設定
# ============================================================

def _parse_id_set(raw: str) -> Set[int]:
    """解析逗號分隔的 Discord ID；非數字項目直接略過並提示。"""
    ids: Set[int] = set()
    for part in re.split(r"[,，;\s]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
        else:
            print(f"⚠️ 忽略無效的 Discord ID：{part!r}")
    return ids


def _is_allow_all(raw: str) -> bool:
    """DISCORD_AI_ALLOWED_USER_IDS 設為 * 或 all 時，不限制使用者。"""
    return str(raw or "").strip().lower() in ("*", "all")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BotConfig:
    """Discord AI Bot 設定；全部來自環境變數，不在程式內保存任何 Secret。"""

    token: str
    allowed_user_ids: Set[int]
    allowed_channel_ids: Set[int]
    debug: bool
    command_prefix: str
    user_cooldown_seconds: float
    answer_cache_seconds: int
    tool_timeout_seconds: float
    max_message_chars: int
    planner_enabled: bool
    slash_command_name: str = "ask"
    guild_ids: Set[int] = field(default_factory=set)
    ephemeral: bool = False
    allow_all_users: bool = False

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
            allowed_user_ids=set() if _is_allow_all(os.getenv("DISCORD_AI_ALLOWED_USER_IDS", "")) else _parse_id_set(os.getenv("DISCORD_AI_ALLOWED_USER_IDS", "")),
            allowed_channel_ids=_parse_id_set(os.getenv("DISCORD_AI_ALLOWED_CHANNEL_IDS", "")),
            debug=_env_flag("DISCORD_AI_DEBUG"),
            command_prefix=os.getenv("DISCORD_AI_COMMAND_PREFIX", "!ace").strip() or "!ace",
            user_cooldown_seconds=tools._env_float("DISCORD_AI_USER_COOLDOWN_SECONDS", 8.0),
            answer_cache_seconds=tools._env_int("DISCORD_AI_ANSWER_CACHE_SECONDS", 300),
            tool_timeout_seconds=tools._env_float("DISCORD_AI_TOOL_TIMEOUT_SECONDS", 180.0),
            max_message_chars=max(500, min(1950, tools._env_int("DISCORD_AI_MAX_MESSAGE_CHARS", 1900))),
            planner_enabled=_env_flag("DISCORD_AI_PLANNER_ENABLE", "1"),
            slash_command_name=(os.getenv("DISCORD_AI_SLASH_COMMAND", "ask").strip().lower() or "ask"),
            guild_ids=_parse_id_set(os.getenv("DISCORD_AI_GUILD_IDS", "")),
            ephemeral=_env_flag("DISCORD_AI_EPHEMERAL"),
            allow_all_users=_is_allow_all(os.getenv("DISCORD_AI_ALLOWED_USER_IDS", "")),
        )


NOT_OPEN_MESSAGE = "目前 AI 分析功能尚未開放。"
RATE_LIMIT_MESSAGE = "AI 分析目前暫時達到 API 使用限制，請稍後再試。"
DISCLAIMER = "※ 以上為歷史資料與統計整理，不構成投資建議。"


class DebugLog:
    """Debug 資訊只輸出到 console，不送到 Discord。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, message: str) -> None:
        if self.enabled:
            print(f"🧪 [艾斯AI] {message}", flush=True)


# ============================================================
# 問題解析
# ============================================================

INTENT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "price": ("股價", "價格", "收盤", "多少錢", "漲跌", "漲幅", "跌幅", "成交量", "量比", "報價"),
    "technical": ("技術面", "技術", "均線", "MA5", "MA10", "MA20", "MA60", "月線", "季線", "週線",
                  "KD", "MACD", "OSC", "布林", "指標", "黃金交叉", "死亡交叉", "乖離",
                  "BOLL", "壓縮", "收窄", "擴張", "橫盤", "上軌", "下軌", "中軌", "沿軌"),
    "volume_profile": ("大量區", "量區", "成本區", "籌碼密集", "支撐", "壓力", "套牢", "價量",
                       "型態", "線型", "K線", "走勢", "趨勢", "盤整", "整理"),
    "cost": ("成本", "均價", "買在", "被套", "停損", "停利", "要不要賣", "該賣", "續抱", "抱著"),
    "warrant": ("權證", "分點", "籌碼", "主力", "加碼", "買超", "賣超", "大戶", "進場"),
    "win_rate": ("勝率", "績效", "歷史表現", "表現", "報酬率", "準不準", "準確"),
    "rank": ("排行", "排名", "前幾", "最準"),
    "news": ("新聞", "消息", "題材", "公告", "營收", "法說", "重訊", "利多", "利空"),
    "history": ("過去", "歷史", "以前", "之前", "相比", "比較", "對比"),
    "recent_trades": ("買什麼", "在買", "買了", "最近買", "賣什麼", "在賣", "操作", "進出", "布局", "佈局"),
    "behavior": ("習性", "節奏", "風格", "操作模式", "近況", "動態", "最近怎樣", "最近如何", "最近在做什麼"),
    "position": ("部位", "還在", "出清", "出場", "賣掉", "賣了", "持有", "抱著", "庫存", "留倉", "還有沒有"),
    "analysis": ("分析", "怎麼樣", "怎樣", "怎麼看", "如何", "看法", "觀察", "解讀", "評估", "綜合",
                 "好嗎", "好不好", "可以買", "能買", "該不該", "操作", "建議", "怎麼辦",
                 "整體", "呼應", "合理", "注意", "意義", "健康", "強不強", "弱不弱"),
}

CATEGORY_INTENTS = ("price", "technical", "volume_profile", "warrant", "win_rate", "news", "recent_trades")

FILLER_WORDS = (
    "請問", "幫我", "幫忙", "一下", "現在", "目前", "最近", "近期", "今天", "今日", "這支", "這檔",
    "股票", "個股", "有沒有", "有哪些", "哪些", "什麼", "甚麼", "多少", "怎麼", "哪裡", "位置",
    "狀況", "情況", "資料", "給我", "列出", "告訴我", "還有", "是否", "可以", "有誰", "誰", "的",
    "了", "在", "跟", "與", "和", "及", "嗎", "呢", "吧", "是", "有", "會", "要", "對", "看", "近",
    "日", "天", "個", "交易", "區", "面", "高", "低", "相關", "目前的",
)

BROKER_PREFIXES = (
    "群益金鼎", "華南永昌", "永豐金", "第一金", "元大", "富邦", "凱基", "永豐", "群益", "國泰", "統一",
    "華南", "兆豐", "玉山", "元富", "台新", "中信", "國票", "日盛", "新光", "康和", "福邦", "大昌",
    "宏遠", "亞東", "合庫", "土銀", "台銀", "彰銀", "摩根", "美林", "德信", "犇亞", "光和", "致和",
    "安泰", "大展", "口袋", "高橋", "北城", "元富",
)

STOCK_CODE_RE = re.compile(r"(?<![0-9A-Za-z/.\-])(\d{4,6}[A-Z]?)(?![0-9A-Za-z%/.\-年月日])")
COST_RE = re.compile(r"(?:成本價?|均價|買在|買進價|進場價)\s*(?:在|是|為|約|大約)?\s*(\d+(?:\.\d+)?)\s*(?:元|塊)?")
DAYS_RE = re.compile(r"(?:近|最近)?\s*(\d{1,2})\s*(?:個)?\s*(?:交易)?\s*(?:日|天)")
EVENT_RE = re.compile(r"(?<![A-Z])([A-E])\s*(?:類|事件|級|型)|事件\s*([A-E])(?![A-Z])")
EVENT_NAME_MAP = {"基礎買超": "A", "明顯買超": "B", "強勢買超": "C", "大額布局": "D", "超大額布局": "E"}


@dataclass
class ParsedQuestion:
    """問題解析結果。"""

    original: str
    intents: Set[str]
    stocks: List[Tuple[str, str]] = field(default_factory=list)
    branches: List[str] = field(default_factory=list)
    stock_candidates: List[Tuple[str, str]] = field(default_factory=list)
    branch_candidates: List[str] = field(default_factory=list)
    days: int = 5
    days_specified: bool = False
    cost_price: Optional[float] = None
    event_type: str = ""
    notes: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "intents": sorted(self.intents),
            "stocks": self.stocks,
            "branches": self.branches,
            "stock_candidates": self.stock_candidates,
            "branch_candidates": self.branch_candidates,
            "days": self.days,
            "event_type": self.event_type,
            "notes": self.notes,
        }


def _blank_out(text: str, token: str) -> str:
    return text.replace(token, "｜") if token else text


class QuestionParser:
    """規則式實體辨識：股票代號／名稱、分點名稱、天數、事件類型與意圖。"""

    def detect_intents(self, text_upper: str) -> Set[str]:
        return {
            intent for intent, words in INTENT_KEYWORDS.items()
            if any(word.upper() in text_upper for word in words)
        }

    def parse(self, question: str) -> ParsedQuestion:
        kf = tools.core()
        text_upper = question.upper()
        parsed = ParsedQuestion(original=question, intents=self.detect_intents(text_upper))

        cost_match = COST_RE.search(question)
        if cost_match:
            parsed.cost_price = float(cost_match.group(1))
            parsed.intents.add("cost")
            question = question.replace(cost_match.group(0), " ")
        days_match = DAYS_RE.search(question)
        if days_match:
            parsed.days = max(1, min(int(days_match.group(1)), 20))
            parsed.days_specified = True
        event_match = EVENT_RE.search(text_upper)
        if event_match:
            parsed.event_type = event_match.group(1) or event_match.group(2) or ""
        else:
            for label, letter in sorted(EVENT_NAME_MAP.items(), key=lambda x: -len(x[0])):
                if label in question:
                    parsed.event_type = letter
                    break

        work = kf.normalize_branch_name(question).upper()
        work = self._extract_branches(work, parsed)
        work = self._extract_stock_codes(question, work, parsed)
        work = self._remove_keywords(work)
        work = self._extract_stock_names(work, parsed)
        self._fuzzy_branch(work, parsed)
        if not parsed.stocks and not parsed.stock_candidates and not parsed.branches:
            self._fuzzy_stock(work, parsed)
        return parsed

    def _extract_branches(self, work: str, parsed: ParsedQuestion) -> str:
        try:
            known = tools.get_known_branches()
        except Exception as exc:  # Google Sheet 失敗時仍可處理股票類問題
            parsed.notes.append(f"分點清單暫時無法取得：{type(exc).__name__}")
            return work
        for alias in sorted(known, key=len, reverse=True):
            if len(alias) >= 3 and alias.upper() in work:
                canonical = known[alias]
                if canonical not in parsed.branches:
                    parsed.branches.append(canonical)
                work = _blank_out(work, alias.upper())
        return work

    def _extract_stock_codes(self, question: str, work: str, parsed: ParsedQuestion) -> str:
        try:
            name_map = tools.get_stock_name_map()
        except Exception as exc:
            name_map = {}
            parsed.notes.append(f"股票名冊暫時無法取得：{type(exc).__name__}")
        for code in STOCK_CODE_RE.findall(question.upper()):
            normalized = tools.core()._normalize_stock_name_code_key(code)
            if name_map and normalized not in name_map:
                parsed.notes.append(f"名冊查無代號 {normalized}")
                continue
            entry = (normalized, name_map.get(normalized, ""))
            if entry not in parsed.stocks:
                parsed.stocks.append(entry)
            work = _blank_out(work, code)
        return work

    def _remove_keywords(self, work: str) -> str:
        words = sorted({w.upper() for group in INTENT_KEYWORDS.values() for w in group if len(w) >= 2}, key=len, reverse=True)
        for word in words:
            work = _blank_out(work, word)
        return work

    def _extract_stock_names(self, work: str, parsed: ParsedQuestion) -> str:
        try:
            name_map = tools.get_stock_name_map()
        except Exception:
            return work
        kf = tools.core()
        by_name: Dict[str, str] = {}
        for code, name in name_map.items():
            key = kf.normalize_branch_name(name).upper()
            if len(key) >= 2:
                by_name.setdefault(key, code)
        for key in sorted(by_name, key=len, reverse=True):
            if key in work:
                code = by_name[key]
                entry = (code, name_map.get(code, key))
                if entry not in parsed.stocks:
                    parsed.stocks.append(entry)
                work = _blank_out(work, key)
        return work

    def _leftover_chunks(self, work: str) -> List[str]:
        for word in sorted(FILLER_WORDS, key=len, reverse=True):
            work = _blank_out(work, word)
        return [c for c in re.findall(r"[一-鿿A-Z]{2,8}", work)]

    def _fuzzy_branch(self, work: str, parsed: ParsedQuestion) -> None:
        wants_branch = bool(parsed.intents & {"win_rate", "recent_trades", "warrant", "history", "behavior"})
        if parsed.branches or not wants_branch:
            return
        for chunk in self._leftover_chunks(work):
            prefix = next((p for p in BROKER_PREFIXES if chunk.startswith(p)), "")
            if not prefix or len(chunk) <= len(prefix):
                continue
            try:
                canonical, candidates = tools.resolve_branch(chunk)
            except Exception as exc:
                parsed.notes.append(f"分點模糊比對失敗：{type(exc).__name__}")
                return
            if canonical:
                parsed.branches.append(canonical)
                parsed.notes.append(f"分點模糊比對：{chunk} → {canonical}")
                return
            if candidates:
                parsed.branch_candidates = candidates
                return

    def _fuzzy_stock(self, work: str, parsed: ParsedQuestion) -> None:
        try:
            name_map = tools.get_stock_name_map()
        except Exception:
            return
        for chunk in self._leftover_chunks(work):
            if any(chunk.startswith(p) for p in BROKER_PREFIXES) and parsed.branches:
                continue
            matches = sorted({(code, name) for code, name in name_map.items() if name.upper().startswith(chunk)})
            if len(matches) == 1:
                parsed.stocks.append(matches[0])
                parsed.notes.append(f"股票名稱模糊比對：{chunk} → {matches[0][1]}({matches[0][0]})")
                return
            if 1 < len(matches) <= 8:
                parsed.stock_candidates = matches
                return


# ============================================================
# 路由
# ============================================================

@dataclass
class ToolCall:
    name: str
    kwargs: Dict[str, Any]

    def key(self) -> str:
        return f"{self.name}:{json.dumps(self.kwargs, ensure_ascii=False, sort_keys=True)}"


@dataclass
class QueryPlan:
    """執行計畫：要跑哪些 Tool、是否需要 Gemini 最終回答。"""

    route: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    need_final_llm: bool = False
    clarification: str = ""
    planner_used: bool = False

    def add(self, name: str, **kwargs: Any) -> None:
        call = ToolCall(name, {k: v for k, v in kwargs.items() if v not in (None, "")})
        if call.key() not in {c.key() for c in self.tool_calls}:
            self.tool_calls.append(call)


HELP_MESSAGE = (
    "我可以幫你查股票與權證分點資料（`/ask 問題` 或 `!ace 問題` 都可以），例如：\n"
    "• `/ask 本週精選`（可加：勝率70%以上／只看D事件／永豐金內湖／買超1000萬以上／靠近月線／排除漲太多／refresh）\n"
    "• `/ask 永豐金內湖D事件勝率`\n"
    "• `!ace 2344股價`\n"
    "• `!ace 2344現在技術面怎麼樣`\n"
    "• `!ace 華邦電現在在大量區哪裡`\n"
    "• `!ace 2344最近有哪些分點在加碼`\n"
    "• `!ace 2344有哪些高勝率分點最近在加碼`\n"
    "• `!ace 永豐金內湖勝率多少`\n"
    "• `!ace 永豐金內湖最近在買什麼`\n"
    "• `!ace 2344最近有什麼新聞`\n"
    "• `!ace 分析2344目前權證籌碼、技術面、大量區與近期新聞`"
)

PLANNER_TOOLS = (
    "get_stock_overview", "get_technical_analysis", "get_volume_profile", "get_warrant_branch",
    "get_high_winrate_branches_buying", "get_branch_performance", "get_branch_recent_trades",
    "get_branch_stock_history", "get_branch_winrate_rank", "get_recent_news", "query_google_sheet",
    "get_branch_event_performance", "get_branch_recent_behavior", "detect_current_branch_events",
    "get_sheet_stock_chips", "get_branch_stock_position", "get_cost_position_context",
)


class QueryRouter:
    """規則判斷優先；只有規則無法決定要用哪些 Tool 時才呼叫 Gemini Planner。"""

    def __init__(self, gateway: "GeminiGateway", config: BotConfig, log: DebugLog) -> None:
        self.gateway = gateway
        self.config = config
        self.log = log

    def plan(self, parsed: ParsedQuestion, stats: "AnswerStats") -> QueryPlan:
        if parsed.stock_candidates and not parsed.stocks:
            options = "、".join(f"{name}（{code}）" for code, name in parsed.stock_candidates)
            return QueryPlan(route="clarify", clarification=f"找到多檔可能的股票：{options}\n請用股票代號重新詢問。")
        if parsed.branch_candidates and not parsed.branches:
            options = "、".join(parsed.branch_candidates)
            return QueryPlan(route="clarify", clarification=f"找到多個可能的分點：{options}\n請輸入完整分點名稱重新詢問。")
        if len(parsed.stocks) > 2:
            return QueryPlan(route="clarify", clarification="一次最多比較 2 檔股票，請縮小範圍後再問。")

        intents = parsed.intents
        categories = intents & set(CATEGORY_INTENTS)
        analysis = "analysis" in intents
        if parsed.branches:
            return self._branch_plan(parsed, categories, analysis)
        if parsed.stocks and ("cost" in intents or "volume_profile" in categories and analysis):
            return self._pattern_plan(parsed)
        if parsed.stocks:
            if categories:
                return self._stock_plan(parsed, categories, analysis)
            if not analysis:
                plan = QueryPlan(route="rule_stock_default")
                for code, _ in parsed.stocks:
                    plan.add("get_stock_overview", stock_code=code)
                    plan.add("get_technical_analysis", stock_code=code)
                return plan
        if not parsed.stocks and "win_rate" in intents and ("rank" in intents or "高" in parsed.original):
            plan = QueryPlan(route="rule_winrate_rank")
            plan.add("get_branch_winrate_rank")
            return plan
        # 沒有股票也沒有分點時，只有「可能是 Sheet 條件查詢」的問題才值得花一次 Planner。
        sheet_like = bool(parsed.event_type) or bool(intents & {"warrant", "win_rate", "history", "rank"})
        if self.config.planner_enabled and (parsed.stocks or sheet_like):
            planned = self._planner_plan(parsed, stats)
            if planned is not None:
                return planned
        if parsed.stocks:
            return self._default_bundle(parsed)
        return QueryPlan(route="help", clarification=HELP_MESSAGE)

    def _branch_plan(self, parsed: ParsedQuestion, categories: Set[str], analysis: bool) -> QueryPlan:
        branch = parsed.branches[0]
        if parsed.stocks:
            code = parsed.stocks[0][0]
            if "position" in parsed.intents:
                # 「部位還在嗎」：直接讀回測 FIFO 狀態，0 次 Gemini，不抓 MoneyDJ。
                plan = QueryPlan(route="rule_branch_position", need_final_llm=analysis)
                plan.add("get_branch_stock_position", branch_name=branch, stock_code=code)
                return plan
            plan = QueryPlan(route="rule_branch_stock", need_final_llm=True)
            plan.add("detect_current_branch_events", stock_code=code, branch_name=branch)
            plan.add("get_branch_stock_position", branch_name=branch, stock_code=code)
            plan.add("get_branch_event_performance", branch_name=branch)
            plan.add("get_branch_recent_behavior", branch_name=branch, stock_code=code)
            plan.add("get_branch_stock_history", branch_name=branch, stock_code=code)
            return plan
        wants_behavior = "behavior" in parsed.intents
        wants_perf = "win_rate" in categories or "history" in parsed.intents or bool(parsed.event_type)
        wants_trades = bool(categories & {"recent_trades", "warrant"}) and not wants_behavior
        plan = QueryPlan(route="rule_branch")
        if parsed.event_type:
            plan.add("get_branch_event_performance", branch_name=branch, event_type=parsed.event_type)
        elif wants_perf or not (wants_trades or wants_behavior):
            plan.add("get_branch_performance", branch_name=branch)
        if wants_behavior:
            plan.add("get_branch_recent_behavior", branch_name=branch)
        if wants_trades or wants_behavior or not wants_perf:
            plan.add("get_branch_recent_trades", branch_name=branch)
        plan.need_final_llm = analysis or not (wants_perf or wants_trades or wants_behavior)
        return plan

    def _stock_plan(self, parsed: ParsedQuestion, categories: Set[str], analysis: bool) -> QueryPlan:
        plan = QueryPlan(route="rule_stock")
        for code, _ in parsed.stocks:
            if "price" in categories:
                plan.add("get_stock_overview", stock_code=code)
            if "technical" in categories:
                plan.add("get_stock_overview", stock_code=code)
                plan.add("get_technical_analysis", stock_code=code)
                if analysis and "warrant" in categories:
                    plan.add("get_volume_profile", stock_code=code)
            if "volume_profile" in categories:
                plan.add("get_volume_profile", stock_code=code)
            if categories & {"warrant", "recent_trades", "win_rate"}:
                # 權證問題第一步一律看 Google Sheet 的追蹤分點（A～E 事件、賣出、勝率）。
                plan.add("get_sheet_stock_chips", stock_code=code, days=parsed.days)
                if tools.MONEYDJ_TOP_ENABLE:
                    plan.add("get_warrant_branch", stock_code=code, days=parsed.days)
            if "news" in categories:
                plan.add("get_recent_news", stock_code=code)
        # 權證分點與勝率 join 屬於同一類「籌碼」資料，不因此多呼叫 Gemini。
        groups = {
            "technical": bool(categories & {"technical"}),
            "volume_profile": bool(categories & {"volume_profile"}),
            "chips": bool(categories & {"warrant", "win_rate", "recent_trades"}),
            "news": bool(categories & {"news"}),
        }
        plan.need_final_llm = analysis or sum(groups.values()) >= 2 or len(parsed.stocks) > 1
        return plan

    def _pattern_plan(self, parsed: ParsedQuestion) -> QueryPlan:
        """型態／持股成本／操作類：型態＋大量區＋均線＋布林（有成本就加成本位置），交給 AI 寫客觀觀察重點。"""
        plan = QueryPlan(route="rule_pattern", need_final_llm=True)
        for code, _ in parsed.stocks:
            plan.add("get_stock_overview", stock_code=code)
            plan.add("get_technical_analysis", stock_code=code)
            plan.add("get_volume_profile", stock_code=code)
            if parsed.cost_price is not None:
                plan.add("get_cost_position_context", stock_code=code, cost_price=parsed.cost_price)
            if "warrant" in parsed.intents:
                plan.add("get_sheet_stock_chips", stock_code=code, days=parsed.days)
        return plan

    def _default_bundle(self, parsed: ParsedQuestion) -> QueryPlan:
        plan = QueryPlan(route="rule_stock_bundle", need_final_llm=True)
        for code, _ in parsed.stocks:
            plan.add("get_stock_overview", stock_code=code)
            plan.add("get_technical_analysis", stock_code=code)
            plan.add("get_volume_profile", stock_code=code)
            plan.add("get_sheet_stock_chips", stock_code=code, days=parsed.days)
            if tools.MONEYDJ_TOP_ENABLE:
                plan.add("get_warrant_branch", stock_code=code, days=parsed.days)
            plan.add("get_recent_news", stock_code=code)
        return plan

    def _planner_plan(self, parsed: ParsedQuestion, stats: "AnswerStats") -> Optional[QueryPlan]:
        try:
            metadata = tools.get_available_sheet_metadata()["sheets"]
        except Exception as exc:  # 沒有 Sheet 欄位資訊時，Planner 仍可選擇一般 Tool
            self.log(f"Planner 略過工作表欄位：{type(exc).__name__}: {exc}")
            metadata = [{"worksheet": k, "description": v, "columns": []} for k, v in tools.SHEET_REGISTRY.items()]
        prompt = build_planner_prompt(parsed, metadata)
        result = self.gateway.generate(prompt, purpose="planner", schema=PLANNER_SCHEMA, temperature=0.0)
        stats.record_gemini(result)
        if not result.ok:
            self.log(f"Planner 失敗，改用規則預設：{result.error}")
            return None
        data = tools_json_loads(result.text)
        if not isinstance(data, dict):
            self.log("Planner 回傳不是合法 JSON，改用規則預設")
            return None
        self.log(f"Planner 結果：{json.dumps(data, ensure_ascii=False)}")
        return self._validate_planner(parsed, data)

    def _validate_planner(self, parsed: ParsedQuestion, data: Dict[str, Any]) -> Optional[QueryPlan]:
        kf = tools.core()
        stocks = [code for code, _ in parsed.stocks]
        try:
            name_map = tools.get_stock_name_map()
        except Exception:
            name_map = {}
        for code in data.get("stock_codes", []) or []:
            normalized = kf._normalize_stock_name_code_key(code)
            if normalized and (not name_map or normalized in name_map) and normalized not in stocks:
                stocks.append(normalized)
        branches = list(parsed.branches)
        for name in data.get("branches", []) or []:
            try:
                canonical, _ = tools.resolve_branch(str(name))
            except Exception:
                canonical = ""
            if canonical and canonical not in branches:
                branches.append(canonical)
        days = int(data.get("days") or parsed.days or 5)
        days = max(1, min(days, 20))
        event_type = str(data.get("event_type") or parsed.event_type or "")

        plan = QueryPlan(route="planner", planner_used=True, need_final_llm=bool(data.get("need_final_llm", True)))
        for name in data.get("tools", []) or []:
            if name not in PLANNER_TOOLS:
                continue
            if name in ("get_stock_overview", "get_technical_analysis", "get_volume_profile", "get_recent_news"):
                for code in stocks[:2]:
                    plan.add(name, stock_code=code)
            elif name in ("get_warrant_branch", "get_high_winrate_branches_buying", "get_sheet_stock_chips"):
                for code in stocks[:2]:
                    plan.add("get_sheet_stock_chips", stock_code=code, days=days)
                    if name != "get_sheet_stock_chips" and tools.MONEYDJ_TOP_ENABLE:
                        plan.add(name, stock_code=code, days=days)
            elif name == "get_branch_stock_position" and branches and stocks:
                plan.add(name, branch_name=branches[0], stock_code=stocks[0])
            elif name in ("get_branch_performance",):
                for branch in branches[:2]:
                    plan.add(name, branch_name=branch, event_type=event_type)
            elif name == "get_branch_recent_trades":
                for branch in branches[:2]:
                    plan.add(name, branch_name=branch, stock_code=stocks[0] if stocks else "")
            elif name == "get_branch_stock_history" and branches and stocks:
                plan.add(name, branch_name=branches[0], stock_code=stocks[0])
            elif name == "get_branch_winrate_rank":
                plan.add(name)
            elif name == "get_branch_event_performance":
                for branch in branches[:2]:
                    plan.add(name, branch_name=branch, event_type=event_type)
            elif name in ("get_branch_recent_behavior", "detect_current_branch_events") and branches:
                if name == "detect_current_branch_events" and not stocks:
                    continue
                plan.add(name, branch_name=branches[0], stock_code=stocks[0] if stocks else "")
        for query in (data.get("sheet_queries", []) or [])[:2]:
            worksheet = str(query.get("worksheet", ""))
            if worksheet not in tools.SHEET_REGISTRY:
                continue
            filters = {
                "stock_code": kf._normalize_stock_name_code_key(query.get("stock_code", "")) if query.get("stock_code") else "",
                "branch": str(query.get("branch", "") or ""),
                "event_type": str(query.get("event_type", "") or ""),
                "date_start": str(query.get("date_start", "") or ""),
                "date_end": str(query.get("date_end", "") or ""),
            }
            if not any(filters.values()):
                continue
            plan.add("query_google_sheet", worksheet=worksheet, limit=min(int(query.get("limit") or 30), 50), **filters)
        if not plan.tool_calls:
            return None
        return plan


PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "stock_codes": {"type": "array", "items": {"type": "string"}},
        "branches": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string", "enum": list(PLANNER_TOOLS)}},
        "days": {"type": "integer"},
        "event_type": {"type": "string"},
        "sheet_queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "worksheet": {"type": "string"},
                    "stock_code": {"type": "string"},
                    "branch": {"type": "string"},
                    "event_type": {"type": "string"},
                    "date_start": {"type": "string"},
                    "date_end": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["worksheet"],
            },
        },
        "need_final_llm": {"type": "boolean"},
    },
    "required": ["stock_codes", "branches", "tools", "need_final_llm"],
}


def tools_json_loads(text: str) -> Any:
    """沿用主程式的 JSON 萃取（容忍 ```json 包裝）。"""
    return tools.core()._extract_json_from_text(text)


def build_planner_prompt(parsed: ParsedQuestion, sheet_metadata: List[Dict[str, Any]]) -> str:
    tool_lines = "\n".join(f"- {name}：{tools.TOOL_DESCRIPTIONS.get(name, name)}" for name in PLANNER_TOOLS)
    sheet_lines = "\n".join(
        f"- {s['worksheet']}：{s['description']}｜欄位：{'、'.join(s.get('columns', [])[:25])}"
        for s in sheet_metadata
    )
    known = {
        "已辨識股票": [f"{code} {name}" for code, name in parsed.stocks],
        "已辨識分點": parsed.branches,
        "天數": parsed.days,
        "事件類型": parsed.event_type,
    }
    return f"""你是台股問答系統的查詢規劃器，只負責決定要呼叫哪些資料工具，不回答問題、不產生任何數字。

可用工具：
{tool_lines}

可條件查詢的 Google Sheet 工作表（query_google_sheet 用；每個查詢至少要有 stock_code、branch、event_type 或日期其中一項）：
{sheet_lines}

規則：
1. 只選回答問題真正需要的工具，最多 5 個。
2. stock_codes 只放問題中明確提到的股票代號；branches 只放問題中明確提到的分點名稱。不得自行猜測。
3. 一般工具能回答時不要使用 query_google_sheet。
4. days 只能是 1～20，未提到就用 5。event_type 只能是 A、B、C、D、E 或空字串。
5. need_final_llm 在需要解讀或整合多種資料時為 true。

系統已辨識：{json.dumps(known, ensure_ascii=False)}
使用者問題：{parsed.original}
"""


# ============================================================
# Gemini 呼叫
# ============================================================

@dataclass
class GeminiResult:
    ok: bool
    text: str = ""
    error: str = ""
    rate_limited: bool = False
    latency: float = 0.0
    purpose: str = ""


_GEMINI_ERROR_STATE = threading.local()
_RATE_LIMIT_KEYWORDS = ("429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "exceeded")


def _install_gemini_error_recorder(kf: Any) -> None:
    """包裝主程式的 Gemini 錯誤判斷函式，讓 Bot 能分辨「限流」與其他失敗。

    只在 Bot 行程內包裝；原判斷邏輯完全保留（wrapper 直接回傳原函式結果）。
    """
    if getattr(kf, "_discord_ai_error_recorder_installed", False):
        return
    original_retryable = kf._is_retryable_gemini_error
    original_switch = kf._should_switch_gemini_key

    def retryable(err: Any) -> bool:
        _GEMINI_ERROR_STATE.last = str(err)
        return original_retryable(err)

    def switch(err: Any) -> bool:
        _GEMINI_ERROR_STATE.last = str(err)
        return original_switch(err)

    kf._is_retryable_gemini_error = retryable
    kf._should_switch_gemini_key = switch
    kf._discord_ai_error_recorder_installed = True


class GeminiGateway:
    """重用 _call_gemini_with_retry（多 Key fallback、retry、structured output）。

    - cache_task 留空、write_cache=False：不讀寫週報的 Google Sheet Gemini 快取，也不寫本機 prompt 快取。
    - 同一時間只允許一個 Gemini 呼叫，避免 Free Tier 被併發打爆。
    """

    def __init__(self, log: DebugLog) -> None:
        self.log = log
        self._lock = threading.Lock()

    def generate(self, prompt: str, purpose: str, schema: Optional[Dict[str, Any]] = None, temperature: float = 0.3) -> GeminiResult:
        kf = tools.core()
        _install_gemini_error_recorder(kf)
        if not kf.GEMINI_ENABLE:
            return GeminiResult(ok=False, error="WARRANT_GEMINI_ENABLE=0", purpose=purpose)
        if kf.genai is None:
            return GeminiResult(ok=False, error="google-genai 未安裝", purpose=purpose)
        if not kf._get_warrants_api_keys():
            return GeminiResult(ok=False, error="未設定 WARRANTS_API_KEY", purpose=purpose)
        _GEMINI_ERROR_STATE.last = ""
        started = time.perf_counter()
        with self._lock:
            try:
                text = kf._call_gemini_with_retry(
                    prompt,
                    cache_task="",
                    stock_code="",
                    stock_name="",
                    write_cache=False,
                    response_schema=schema,
                    temperature=temperature,
                )
            except Exception as exc:  # google-genai 例外型別眾多，統一轉成失敗結果
                text = None
                _GEMINI_ERROR_STATE.last = f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - started
        last_error = str(getattr(_GEMINI_ERROR_STATE, "last", "") or "")
        self.log(
            f"Gemini 呼叫｜用途={purpose}｜model={kf.GEMINI_MODEL}｜latency={latency:.2f}s｜"
            f"prompt={len(prompt):,} 字｜結果={'成功' if text else '失敗'}"
        )
        if text:
            return GeminiResult(ok=True, text=str(text).strip(), latency=latency, purpose=purpose)
        rate_limited = any(k.lower() in last_error.lower() for k in _RATE_LIMIT_KEYWORDS)
        return GeminiResult(
            ok=False,
            error=last_error or "Gemini 沒有回傳內容",
            rate_limited=rate_limited,
            latency=latency,
            purpose=purpose,
        )


# ============================================================
# 最終回答 Prompt 與數字核對
# ============================================================

FINAL_SYSTEM_PROMPT = """你是「艾斯 AI 台股資料分析助手」。
你只能依照系統提供的 tool_results 回答。

規則：
1. 不得自行創造任何數據（股價、勝率、分點、金額、均線、大量區、法人資料、新聞都一樣）。
2. 數字必須完全使用 tool_results 裡的數值或文字，金額沿用原本的「萬／億」寫法。
3. 如果資料缺失（available=false、found=false 或欄位為空），明確指出「目前沒有取得足夠資料」，不可猜測。
4. 分清楚：客觀數據、系統統計、AI 解讀；AI 解讀請加上「AI 解讀：」開頭。
5. 不要把「歷史勝率」描述成未來保證。
6. small_sample=true 或樣本數少時，要主動提醒樣本數偏少。
7. 資料日期要說清楚，並提醒股價為日K收盤資料、不是盤中即時。
8. 語氣自然、口語、不要過度艱深。
9. 回答將排進一頁式圖片，約 200～450 個中文字，必要時最多 800 字。每段 1～3 句。
10. 優先回答使用者真正問的問題，只放相關區塊。
11. 不提供目標價、報酬預測或「買進／賣出」這類直接指令；但使用者問型態、成本或操作時，不可拒答，必須依序用型態、大量區、均線（再來才是布林與籌碼）給出客觀觀察重點：目前位置、關鍵支撐與壓力價位（只能使用 tool_results 的價位）、以及「若守住／若跌破／若站回」各代表什麼，並寫在【觀察重點】。有成本價時要說明成本相對現價、均線與大量區的位置。
12. 涉及新聞時，只能引用 tool_results 裡的新聞標題與摘要。
13. 不要把「買超」直接等同「看多必漲」。
14. 不要把「高歷史勝率」直接說成這次一定成功。
15. 技術面必須參考 bollinger 的 position、signals、width_trend、squeeze、sideways、band_walk 與 breakout 旗標；問題提到布林時使用【布林觀察】區塊。
16. 布林判讀依 bollinger.rules 的本專案門檻；null 或資料不足不可判定有／沒有。影線穿越不等於收盤突破；持續軌外不等於本日首次突破。
17. 壓縮只代表波動收斂，不預測突破方向；觸軌不單獨推論反轉，未符合橫盤條件不可稱為橫盤。若資料不足60個有效帶寬，不可宣稱壓縮已成立。

輸出格式（圖片內文，不要用表格、不要用程式碼區塊）：
第一行：**股票名稱（代號）** 或 **分點名稱**
接著只放相關區塊。型態、成本、操作類問題依序使用：【型態】、【均線與大量區】、【布林】、【籌碼】、【觀察重點】；其他問題依序使用：【籌碼】、【技術面】、【大量區】、【新聞】、【綜合觀察】
最後一行：「資料時間：」列出各類資料的日期或統計期間。"""


def _prune_empty(value: Any) -> Any:
    """移除空值，縮小送給 Gemini 的 JSON。"""
    if isinstance(value, dict):
        pruned = {k: _prune_empty(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [v for v in (_prune_empty(i) for i in value) if v not in (None, "", [], {})]
    return value


def build_final_payload(question: str, results: Sequence[tools.ToolResult]) -> Dict[str, Any]:
    tool_results: Dict[str, Any] = {}
    for result in results:
        key = result.name
        suffix = result.data.get("stock_code") or result.data.get("branch") or ""
        if key in tool_results or suffix:
            key = f"{key}:{suffix}" if suffix else f"{key}:{len(tool_results)}"
        tool_results[key] = result.to_payload()
    return _prune_empty({"question": question, "tool_results": tool_results})


def build_final_prompt(payload: Dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{FINAL_SYSTEM_PROMPT}\n\n使用者問題：{payload['question']}\n\ntool_results（JSON）：\n{payload_json}\n"


_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?")
_EXEMPT_PATTERNS = (
    re.compile(r"\b(?:MA|MV|BB|K|D|J)\d+\b", re.IGNORECASE),
    re.compile(r"D\+\d+"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
    re.compile(r"\d{1,2}/\d{1,2}"),
    re.compile(r"12/26/9"),
    re.compile(r"/\s*(?:100|25|15|10)(?![\d.])"),
)


def _number_variants(token: str) -> Set[str]:
    cleaned = token.replace(",", "").lstrip("+")
    variants = {cleaned, cleaned.lstrip("-")}
    try:
        value = float(cleaned)
    except ValueError:
        return variants
    for number in (value, abs(value)):
        variants.add(f"{number:g}")
        for digits in (0, 1, 2):
            rounded = round(number, digits)
            variants.add(f"{rounded:g}")
            variants.add(f"{rounded:.{digits}f}")
    return variants


def find_ungrounded_numbers(answer: str, payload: Dict[str, Any]) -> List[str]:
    """找出回答中不存在於 tool_results 的數字（小於等於 10 的整數視為一般敘述用字）。"""
    source = json.dumps(payload, ensure_ascii=False)
    allowed: Set[str] = set()
    for token in _NUMBER_RE.findall(source):
        allowed |= _number_variants(token)
    text = answer
    for pattern in _EXEMPT_PATTERNS:
        text = pattern.sub(" ", text)
    ungrounded = []
    for token in _NUMBER_RE.findall(text):
        cleaned = token.replace(",", "").lstrip("+")
        if cleaned in allowed or cleaned.lstrip("-") in allowed:
            continue
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if value.is_integer() and abs(value) <= 10:
            continue
        ungrounded.append(token)
    return ungrounded


# ============================================================
# Python 規則式排版（0 次 Gemini 的回答）
# ============================================================

def _v(value: Any, suffix: str = "", signed: bool = False) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return f"{text}{suffix}"
    return f"{value}{suffix}"


def _title(data: Dict[str, Any]) -> str:
    name, code = data.get("stock_name", ""), data.get("stock_code", "")
    if code:
        return f"**{name}（{code}）**" if name else f"**{code}**"
    return f"**{data.get('branch', '')}**" if data.get("branch") else ""


def format_overview(d: Dict[str, Any]) -> str:
    return (
        f"💹 股價（{d.get('data_date')} 收盤）\n"
        f"收盤 {_v(d.get('close'))}，漲跌 {_v(d.get('change'), signed=True)}（{_v(d.get('change_pct'), '%', signed=True)}）\n"
        f"成交量 {_v(d.get('volume_lots'), ' 張')}｜5日均量 {_v(d.get('mv5_lots'), ' 張')}｜20日均量 {_v(d.get('mv20_lots'), ' 張')}\n"
        f"量比：對5日均量 {_v(d.get('volume_ratio_vs_mv5'), ' 倍')}｜對20日均量 {_v(d.get('volume_ratio_vs_mv20'), ' 倍')}"
    )


def format_technical(d: Dict[str, Any]) -> str:
    ma_parts = [
        f"{name} {_v(info.get('value'))}（{info.get('position')}）"
        for name, info in (d.get("moving_averages") or {}).items()
    ]
    cross = d.get("ma20_cross_recent_3_days") or {}
    if cross.get("just_broke_above"):
        cross_text = f"近3日剛突破 MA20（{cross.get('cross_date')}）"
    elif cross.get("just_broke_below"):
        cross_text = f"近3日剛跌破 MA20（{cross.get('cross_date')}）"
    else:
        cross_text = "近3日沒有穿越 MA20"
    kd, macd, bb = d.get("kd") or {}, d.get("macd") or {}, d.get("bollinger") or {}
    return (
        f"📈 技術面（{d.get('data_date')} 收盤 {_v(d.get('close'))}）\n"
        f"均線：{'｜'.join(ma_parts)}\n"
        f"排列：{d.get('ma_alignment')}；{cross_text}"
        + (f"；{d.get('ma_kline_signals')}" if d.get("ma_kline_signals") else "")
        + f"\nKD：K {_v(kd.get('K9'))}／D {_v(kd.get('D9'))}（{kd.get('signals') or '無特殊訊號'}）\n"
        f"MACD：DIF {_v(macd.get('DIF'))}／MACD {_v(macd.get('MACD'))}／OSC {_v(macd.get('OSC'))}"
        f"（{macd.get('osc_trend')}{'；' + macd.get('signals') if macd.get('signals') else ''}）\n"
        f"布林：上軌 {_v(bb.get('upper'))}／中軌 {_v(bb.get('mid'))}／下軌 {_v(bb.get('lower'))}，"
        f"{bb.get('position')}（%B {_v(bb.get('percent_b'))}）"
        + f"\n【布林觀察】{'；'.join(bb.get('signals') or ['資料不足'])}"
        + f"\n帶寬 {_v(bb.get('width_pct_of_mid'), '%')}｜相對5日前變化 {_v(bb.get('width_change_5d_pct'), '%', signed=True)}"
        + "\n※ 壓縮與橫盤依本專案規則判定；觸軌不代表反轉，壓縮不代表突破方向。"
    )


def format_volume_profile(d: Dict[str, Any]) -> str:
    window = d.get("analysis_window") or {}
    lines = [f"📊 大量區（{window.get('start')}～{window.get('end')}，{window.get('trading_days')} 根日K）"]
    for key in ("maximum_volume_zone", "second_volume_zone"):
        zone = d.get(key) or {}
        if zone:
            lines.append(
                f"{zone.get('label')}（{zone.get('chart_color')}）：{_v(zone.get('price_low'))}～{_v(zone.get('price_high'))}，"
                f"收盤{zone.get('close_relation')}"
            )
    lines.append(f"收盤 {_v(d.get('close'))}：{d.get('position_vs_two_zones')}")
    lines.append(f"近期：{d.get('recent_maximum_zone_event')}")
    lines.append(f"型態：{d.get('pattern_label')}（{d.get('crossing_volume_character')}）")
    return "\n".join(lines)


def _abcde_text(row: Dict[str, Any]) -> str:
    events = row.get("abcde_events") or []
    if not events:
        return ""
    parts = [f"{e.get('event')}事件 {str(e.get('event_date', ''))[5:]}" for e in events[:3]]
    return "｜" + "、".join(parts)


def format_warrant(d: Dict[str, Any]) -> str:
    if not d.get("available"):
        return f"📌 權證分點：{d.get('reason', '目前沒有取得足夠資料')}"
    lines = [
        f"📌 權證分點（{d.get('period_start')}～{d.get('period_end')}，{d.get('actual_trading_days')} 個交易日）",
        f"區間合計淨額 {d.get('total_net_amount_text')}（買 {d.get('total_buy_amount_text')}／賣 {d.get('total_sell_amount_text')}）",
        "買超：",
    ]
    for row in (d.get("top_buy_branches") or [])[:5]:
        lines.append(f"{row['rank']}. {row['branch']} {row['net_amount_text']}（主要：{row.get('main_warrant_name') or '-'}）{_abcde_text(row)}")
    lines.append("賣超：")
    for row in (d.get("top_sell_branches") or [])[:3]:
        lines.append(f"{row['rank']}. {row['branch']} {row['net_amount_text']}（主要：{row.get('main_warrant_name') or '-'}）")
    return "\n".join(lines)


def format_high_winrate(d: Dict[str, Any]) -> str:
    if not d.get("available"):
        return f"📌 高勝率分點：{d.get('reason', '目前沒有取得足夠資料')}"
    lines = [
        f"📌 近期買超分點 × 歷史勝率（{d.get('period_start')}～{d.get('period_end')}，高勝率門檻 {_v(d.get('high_win_rate_threshold_pct'), '%')}）"
    ]
    joined = d.get("joined_branches") or []
    if not joined:
        lines.append("近期買超分點在勝率統計中都沒有歷史資料。")
    for row in joined[:8]:
        flags = []
        if row.get("is_high_win_rate"):
            flags.append("高勝率")
        if row.get("small_sample"):
            flags.append("樣本少")
        lines.append(
            f"• {row['branch']}：買超 {row['net_amount_text']}｜歷史勝率 {_v(row.get('historical_win_rate_pct'), '%')}"
            f"｜加權報酬 {_v(row.get('historical_weighted_return_pct'), '%', signed=True)}"
            f"｜{_v(row.get('historical_event_count'), ' 筆')}"
            + (f"｜{'、'.join(flags)}" if flags else "")
            + _abcde_text(row)
        )
    lines.append("※ 歷史勝率不代表這次一定成功。")
    return "\n".join(lines)


def format_branch_performance(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        candidates = "、".join(d.get("candidates") or [])
        return f"📌 分點績效：{d.get('reason', '找不到分點')}" + (f"（候選：{candidates}）" if candidates else "")
    overall = d.get("overall_all_events") or {}
    lines = [f"📌 {d.get('branch')} 歷史績效（勝率統計）"]
    if overall:
        lines.append(
            f"全部事件：勝率 {_v(overall.get('win_rate_pct'), '%')}｜加權報酬 {_v(overall.get('weighted_return_pct'), '%', signed=True)}"
            f"｜{_v(overall.get('event_count'), ' 筆')}｜平均持有 {_v(overall.get('avg_holding_days'), ' 天')}"
            + (f"（{overall.get('holding_style')}）" if overall.get("holding_style") else "")
        )
    for row in d.get("by_event_type") or []:
        if str(row.get("event_type", "")).startswith("全部"):
            continue
        lines.append(
            f"• {row.get('event_type')}：勝率 {row.get('win_rate') or '-'}｜加權報酬 {row.get('weighted_return') or '-'}"
            f"｜{_v(row.get('event_count'), ' 筆')}｜平均持有 {row.get('avg_holding_days') or '-'} 天"
            + ("｜樣本少" if row.get("small_sample") else "")
        )
    lines.append("※ 勝率含實際出清與持有滿60日估值；歷史勝率不代表未來結果。")
    return "\n".join(lines)


def format_branch_recent(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        candidates = "、".join(d.get("candidates") or [])
        return f"📌 分點近期買賣：{d.get('reason', '找不到分點')}" + (f"（候選：{candidates}）" if candidates else "")
    lines = [f"📌 {d.get('branch')} 近10日權證買賣（{d.get('period') or d.get('snapshot_date')}）"]
    buys, sells = d.get("net_buy_stocks") or [], d.get("net_sell_stocks") or []
    if not buys and not sells:
        lines.append("近10日明細中沒有這個分點的買賣紀錄。")
    if buys:
        lines.append("淨買超：")
        lines.extend(
            f"{i}. {r['stock_name']}（{r['stock_code']}）{r['net_amount_text']}｜權證 {r.get('warrant_count') or '-'} 檔"
            for i, r in enumerate(buys[:8], 1)
        )
    if sells:
        lines.append("淨賣超：")
        lines.extend(f"{i}. {r['stock_name']}（{r['stock_code']}）{r['net_amount_text']}" for i, r in enumerate(sells[:5], 1))
    events = d.get("recent_abcde_events_30d") or []
    if events:
        lines.append("近30日 ABCDE 事件：" + "、".join(
            f"{e.get('標的名稱', '')}{e.get('事件代碼', '')}({str(e.get('事件日', ''))[5:]})" for e in events[:6]
        ))
    return "\n".join(lines)


def format_branch_stock_history(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        return f"📌 分點歷史事件：{d.get('reason', '找不到資料')}"
    counts = d.get("result_counts_from_sheet") or {}
    lines = [
        f"📌 {d.get('branch')} × {d.get('stock_name')}（{d.get('stock_code')}）ABCDE 歷史事件",
        f"共 {d.get('event_total')} 筆：勝 {counts.get('勝', 0)}｜敗 {counts.get('敗', 0)}｜平手 {counts.get('平手', 0)}｜未出清 {counts.get('未出清', 0)}"
        + ("｜樣本少" if d.get("small_sample") else ""),
    ]
    if d.get("closed_return_avg_pct") is not None:
        lines.append(f"已出清事件簡單平均報酬 {_v(d.get('closed_return_avg_pct'), '%', signed=True)}（{d.get('closed_return_samples')} 筆）")
    for e in (d.get("latest_events") or [])[:5]:
        lines.append(f"• {e.get('事件日')} {e.get('事件代碼')}事件｜{e.get('目前狀態')}｜{e.get('結果')}｜出清獲利 {e.get('出清獲利%') or '-'}")
    return "\n".join(lines)


def format_winrate_rank(d: Dict[str, Any]) -> str:
    lines = [f"📌 近10日分點勝率排行（{d.get('period') or d.get('snapshot_date')}）"]
    for row in (d.get("rows") or [])[:10]:
        lines.append(
            f"{row.get('排名')}. {row.get('分點')}：勝率 {row.get('近10日勝率')}（{row.get('近10日勝筆數')}勝{row.get('近10日敗筆數')}敗）"
            f"｜加權報酬 {row.get('近10日加權平均報酬%')}"
        )
    lines.append("※ 近10日勝率樣本少時波動大，不代表未來結果。")
    return "\n".join(lines)


def format_news(d: Dict[str, Any]) -> str:
    if not d.get("available"):
        return "📰 新聞：近期沒有取得與公司直接相關的合格新聞。"
    lines = ["📰 新聞"]
    for point in d.get("summary_points") or []:
        lines.append(f"• {point}")
    for item in (d.get("articles") or [])[:5]:
        lines.append(f"• {item.get('date')}｜{item.get('title')}（{item.get('source')}）")
    return "\n".join(lines)


def format_sheet_query(d: Dict[str, Any]) -> str:
    lines = [f"📌 {d.get('worksheet')}：符合 {d.get('matched_rows')} 筆，顯示 {d.get('returned_rows')} 筆"]
    for row in (d.get("rows") or [])[:8]:
        lines.append("• " + "｜".join(f"{v}" for v in list(row.values())[:6] if v))
    return "\n".join(lines)


FORMATTERS = {
    "get_stock_overview": format_overview,
    "get_technical_analysis": format_technical,
    "get_volume_profile": format_volume_profile,
    "get_warrant_branch": format_warrant,
    "get_high_winrate_branches_buying": format_high_winrate,
    "get_branch_performance": format_branch_performance,
    "get_branch_recent_trades": format_branch_recent,
    "get_branch_stock_history": format_branch_stock_history,
    "get_branch_winrate_rank": format_winrate_rank,
    "get_recent_news": format_news,
    "query_google_sheet": format_sheet_query,
    "get_branch_event_performance": None,
    "detect_current_branch_events": None,
    "get_branch_recent_behavior": None,
}


def _perf_line(label: str, p: Dict[str, Any]) -> str:
    if not p:
        return f"{label}：無資料"
    text = (
        f"{label}：勝率 {_v(p.get('raw_win_rate'), '%')}（修正 {_v(p.get('adjusted_win_rate'), '%')}）"
        f"｜納入 {_v(p.get('included_count'))} 筆"
    )
    if p.get("unresolved_count"):
        text += f"｜未完成 {_v(p.get('unresolved_count'))} 筆"
    text += f"｜加權報酬 {_v(p.get('weighted_return'), '%', signed=True)}｜平均持有 {_v(p.get('avg_holding_days'), ' 天')}"
    if p.get("small_sample"):
        text += "｜樣本少"
    return text


def format_branch_event_performance(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        candidates = "、".join(d.get("candidates") or [])
        return f"📌 事件別績效：{d.get('reason', '找不到資料')}" + (f"（候選：{candidates}）" if candidates else "")
    lines = [f"📌 {d.get('branch')} × A～E 事件歷史績效"]
    if d.get("event_type") and d.get("event_type") != "overall":
        lines.append(_perf_line(f"{d['event_type']}事件", d))
        lines.append(_perf_line("總勝率（背景）", d.get("overall_background") or {}))
    else:
        for code in tools.EVENT_CODES:
            if d.get(code):
                lines.append(_perf_line(f"{code}事件", d[code]))
        lines.append(_perf_line("全部合併（背景）", d.get("overall") or {}))
    lines.append(f"※ {d.get('adjusted_method', '')}；未完成事件不算勝。")
    return "\n".join(lines)


def format_current_events(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        return f"📌 本次事件：{d.get('reason', '找不到資料')}"
    if not d.get("triggered_events"):
        return f"📌 {d.get('branch')} 在 {d.get('window_start')}～{d.get('window_end')} 沒有觸發 A～E 事件"
    events = "、".join(f"{e['event']}({e['event_date'][5:]} {e['buy_amount_text']})" for e in d.get("events", [])[:6])
    return f"📌 本次事件（{d.get('window_start')}～{d.get('window_end')}）：{'、'.join(d['triggered_events'])}｜{events}"


def format_recent_behavior(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        return f"📌 分點近期操作：{d.get('reason', '找不到資料')}"
    recent = d.get("branch_recent") or {}
    lines = [f"📌 {d.get('branch')} 近期操作（{recent.get('window_start')}～{recent.get('window_end')}，僅供參考）"]
    same = d.get("same_stock") or {}
    live = same.get("live_flow") or {}
    if same:
        if live.get("has_trades"):
            status = [
                label for flag, label in (
                    ("continuous_buying", "持續加碼"), ("reducing_recently", "近期減碼"), ("direction_choppy", "方向反覆"),
                ) if live.get(flag)
            ]
            lines.append(
                f"同股票 {same.get('stock_name')}（{same.get('stock_code')}）：5日 {live.get('net_buy_5d_text')}｜10日 {live.get('net_buy_10d_text')}"
                f"｜20日 {live.get('net_buy_20d_text')}｜{'、'.join(status) or '無明顯加減碼'}"
            )
        elif live:
            lines.append(f"同股票逐日流水：{live.get('reason', '近20日沒有成交')}")
        if same.get("round_first_event_date"):
            lines.append(f"本輪第一筆未出清事件 {same['round_first_event_date']}，至今 {same.get('round_trading_days_since_start')} 個交易日")
        cases = same.get("stock_history_cases") or {}
        if cases.get("cases"):
            lines.append("同股票歷史：" + tools._outcome_sentence(cases, ""))
    if recent.get("top_buy_stocks"):
        lines.append("近期主要布局：" + "、".join(s.get("stock_name") or s["stock_code"] for s in recent["top_buy_stocks"][:5]))
    if recent.get("event_type_distribution"):
        lines.append("事件分布：" + "、".join(f"{k}×{v}" for k, v in recent["event_type_distribution"].items()))
    lines.append(f"{recent.get('recent_cases_sentence', '')}{recent.get('holding_style', '')}。")
    return "\n".join(lines)


def _event_short(e: Dict[str, Any]) -> str:
    return f"{e.get('event')} {str(e.get('event_date', ''))[5:]} {e.get('buy_amount_text', '')}（{e.get('status', '')}）"


def format_sheet_stock_chips(d: Dict[str, Any]) -> str:
    if not d.get("available"):
        return f"【追蹤分點籌碼】{d.get('reason', '目前沒有取得足夠資料')}"
    lines = [f"【追蹤分點籌碼】A～E 事件 {d.get('period_recent')}｜賣出與部位 {d.get('period_lookback')}"]
    for row in d.get("branches") or []:
        tag = "（高勝率）" if row.get("is_high_win_rate") else ""
        win = row.get("overall_win_rate_background")
        perf = "；".join(
            f"{code}事件 {_v(p.get('raw_win_rate'), '%')}｜n={_v(p.get('sample_included'))}"
            for code, p in (row.get("event_performance") or {}).items()
        )
        recent_events = row.get("events_recent") or []
        lines.append(
            f"• {row['branch']}{tag}："
            + (f"近期事件買進 {row.get('event_buy_amount_recent_text')}［{'、'.join(_event_short(e) for e in recent_events[:3])}］"
               if recent_events else ("近期無 A～E 事件" + (f"（觀察期間事件買進 {row.get('event_buy_amount_lookback_text')}）"
                                                           if row.get("event_buy_amount_lookback_text") not in (None, "", "-") else "")))
        )
        lines.append(
            f"　{row.get('position_status')}"
            + (f"｜總勝率（背景）{_v(win, '%')}" if win is not None else "")
            + (f"｜{perf}" if perf else "")
        )
        sells = row.get("reduce_or_exit_lookback") or []
        if sells:
            lines.append("　賣出：" + "、".join(f"{s['date'][5:]} {s['action']} {s['sell_amount_text']}" for s in sells[-3:]))
    lines.append("※ 只涵蓋回測追蹤分點；勝率為歷史統計，不代表這次一定成功。")
    return "\n".join(lines)


def format_branch_stock_position(d: Dict[str, Any]) -> str:
    if not d.get("found"):
        candidates = "、".join(d.get("candidates") or [])
        return f"【部位】{d.get('reason', '找不到資料')}" + (f"（候選：{candidates}）" if candidates else "")
    lines = [
        f"【部位】{d.get('branch')} × {d.get('stock_name')}（{d.get('stock_code')}）",
        d.get("position_status", ""),
    ]
    for e in d.get("open_events") or []:
        lines.append(f"• 未出清：{_event_short(e)}")
    for e in (d.get("recent_closed_events") or [])[-3:]:
        lines.append(f"• 已出清：{e.get('event')} {str(e.get('event_date', ''))[5:]} 買進 → {str(e.get('exit_date', ''))[5:]} 出清")
    sells = d.get("recent_sells") or []
    if sells:
        lines.append("【近期賣出】" + "、".join(f"{s['date'][5:]} {s['action']} {s['sell_amount_text']}" for s in sells[-5:]))
    perf = d.get("branch_performance") or {}
    if perf.get("overall_raw_win_rate") is not None:
        lines.append(f"總勝率（背景）{_v(perf.get('overall_raw_win_rate'), '%')}｜n={_v(perf.get('overall_sample_included'))}")
    lines.append(f"※ {d.get('definition_note', '')}")
    return "\n".join(lines)


FORMATTERS.update({
    "get_sheet_stock_chips": format_sheet_stock_chips,
    "get_branch_stock_position": format_branch_stock_position,
    "get_branch_event_performance": format_branch_event_performance,
    "detect_current_branch_events": format_current_events,
    "get_branch_recent_behavior": format_recent_behavior,
})


def build_data_time_line(results: Sequence[tools.ToolResult]) -> str:
    """整理每類資料的日期，避免使用者誤以為是盤中即時資料。"""
    parts: List[str] = []

    def add(text: str) -> None:
        if text and text not in parts:
            parts.append(text)

    for r in results:
        if not r.ok:
            continue
        d = r.data
        if r.name in ("get_stock_overview", "get_technical_analysis", "get_volume_profile") and d.get("data_date"):
            add(f"股價截至 {d['data_date']}（日K收盤）")
        elif r.name in ("get_warrant_branch", "get_high_winrate_branches_buying") and d.get("period_start"):
            add(f"權證分點 {d['period_start']}～{d['period_end']}（{d.get('actual_trading_days')} 個交易日）")
        elif r.name == "get_branch_performance" and d.get("found"):
            add(f"勝率統計（Sheet 更新 {d.get('sheet_updated_at') or '時間未知'}）")
        elif r.name in ("get_branch_recent_trades", "get_branch_winrate_rank") and (d.get("period") or d.get("snapshot_date")):
            add(f"近10日分點統計 {d.get('period') or d.get('snapshot_date')}")
        elif r.name == "get_branch_stock_history" and d.get("found"):
            add(f"ABCDE 事件（Sheet 更新 {d.get('sheet_updated_at') or '時間未知'}）")
        elif r.name == "get_recent_news" and d.get("available"):
            add("新聞為近期六來源抓取結果")
        elif r.name in ("get_sheet_stock_chips", "get_branch_stock_position") and d.get("data_latest_event_date"):
            add(f"追蹤分點 A～E 事件截至 {d['data_latest_event_date']}（Google Sheet）")
    return "資料時間：" + "｜".join(parts) if parts else ""


def build_rule_based_answer(results: Sequence[tools.ToolResult]) -> str:
    """0 次 Gemini：直接把 Tool 結果排版成 Discord 文字。"""
    sections: List[str] = []
    title = next((_title(r.data) for r in results if r.ok and _title(r.data)), "")
    if title:
        sections.append(title)
    for r in results:
        if r.ok:
            sections.append(FORMATTERS.get(r.name, format_sheet_query)(r.data))
        else:
            sections.append(f"⚠️ {r.user_message or '資料取得失敗'}，其他已取得資料如下。")
    time_line = build_data_time_line(results)
    if time_line:
        sections.append(time_line)
    sections.append(DISCLAIMER)
    return "\n\n".join(s for s in sections if s)


def split_discord_message(text: str, limit: int = 1900) -> List[str]:
    """依段落切成多則 Discord 訊息；單段過長時再硬切。"""
    chunks: List[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current.strip():
        chunks.append(current)
    return chunks or [""]


# ============================================================
# 問答引擎
# ============================================================

@dataclass
class AnswerStats:
    """單次問答的統計資訊（Debug 用）。"""

    gemini_calls: int = 0
    gemini_latency: float = 0.0
    prompt_chars: int = 0

    def record_gemini(self, result: GeminiResult) -> None:
        self.gemini_calls += 1
        self.gemini_latency += result.latency


@dataclass
class AnswerResult:
    text: str
    route: str
    gemini_calls: int
    elapsed: float
    cache_hit: bool = False
    cacheable: bool = False
    panels: List[Dict[str, Any]] = field(default_factory=list)
    layout: str = "text"
    weekly: Dict[str, Any] = field(default_factory=dict)


class AceQueryEngine:
    """與 Discord 無關的問答核心；CLI 與 Bot 共用。"""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.log = DebugLog(config.debug)
        self.parser = QuestionParser()
        self.gateway = GeminiGateway(self.log)
        self.router = QueryRouter(self.gateway, config, self.log)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ace-tool")
        self._answer_cache = tools.TTLCache("discord_ai_answer")
        self._engine_lock = threading.Lock()

    def answer(self, question: str) -> AnswerResult:
        started = time.perf_counter()
        normalized = re.sub(r"\s+", "", question)
        hit, cached = self._answer_cache.get(normalized)
        if hit:
            self.log(f"回答快取命中：{question}")
            return replace(cached, route="answer_cache", gemini_calls=0, elapsed=time.perf_counter() - started, cache_hit=True)
        if is_weekly_pick_question(question):
            with self._engine_lock:
                return self._answer_weekly_pick(question, started)
        with self._engine_lock:
            result = self._answer_uncached(question, started)
        # 只快取「資料全部成功、且 Gemini 沒有失敗」的回答，避免限流或逾時訊息被重複送出。
        if result.cacheable:
            self._answer_cache.set(normalized, result, self.config.answer_cache_seconds)
        return result

    def _answer_weekly_pick(self, question: str, started: float) -> AnswerResult:
        """本週精選：Python 算 TOP5，正常只呼叫 Gemini 一次；有自己的 weekly_pick 快取。"""
        stats = AnswerStats()
        self.log(f"本週精選問題：{question}")
        panels = []
        weekly: Dict[str, Any] = {}

        def generate(prompt: str, schema: Optional[Dict[str, Any]] = None) -> GeminiResult:
            result = self.gateway.generate(prompt, purpose="weekly_pick", schema=schema, temperature=0.3)
            stats.record_gemini(result)
            return result

        try:
            answer = weekly_pick.run_weekly_pick(
                question,
                generate=generate,
                find_ungrounded=find_ungrounded_numbers,
                rate_limit_message=RATE_LIMIT_MESSAGE,
                log=self.log,
            )
            text, cache_hit = answer.text, answer.cache_hit
            branches = {card["stock_code"]: card.get("mark_branches") or [] for card in answer.cards}
            panels = self._get_chart_panels(answer.stock_codes, branches)
            if answer.cards:
                weekly = {"cards": answer.cards, "overview": answer.overview, "meta": answer.meta, "notice": answer.notice}
        except tools.ToolDataError as exc:
            text, cache_hit = f"本週精選目前無法計算：{exc}", False
        except Exception as exc:  # 計算流程任何例外都不可讓 Bot 中斷
            print(f"❌ 本週精選計算失敗：{type(exc).__name__}: {exc}", flush=True)
            text, cache_hit = "本週精選計算時發生錯誤，請稍後再試（詳細原因已記錄在 console）。", False
        elapsed = time.perf_counter() - started
        self.log(f"本週精選完成｜Gemini 呼叫 {stats.gemini_calls} 次｜快取={cache_hit}｜總耗時 {elapsed:.1f}s")
        return AnswerResult(
            text=text, route="weekly_pick", gemini_calls=stats.gemini_calls, elapsed=elapsed, cache_hit=cache_hit,
            panels=panels, layout="weekly_pick" if weekly else "text", weekly=weekly,
        )

    def _get_chart_panels(self, codes: List[str], branches: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
        codes = list(dict.fromkeys(codes))
        calls = []
        for code in codes:
            names = (branches or {}).get(code) or []
            calls.append(ToolCall("get_chart_panel", {"stock_code": code, **({"branch_name": ",".join(names)} if names else {})}))
        results = self._run_tools(calls)
        return [r.data if r.ok else {"stock_code": code, "error": "K 線資料暫時無法取得；以下保留已取得的分析。"}
                for code, r in zip(codes, results)]

    def plan_only(self, question: str) -> Tuple[ParsedQuestion, QueryPlan, AnswerStats]:
        stats = AnswerStats()
        if is_weekly_pick_question(question):
            filters = weekly_pick.parse_weekly_pick_filters(question)
            parsed = ParsedQuestion(original=question, intents={"weekly_pick"}, notes=filters.describe() + (["refresh"] if filters.refresh else []))
            return parsed, QueryPlan(route="weekly_pick", need_final_llm=True), stats
        parsed = self.parser.parse(question)
        return parsed, self.router.plan(parsed, stats), stats

    def _answer_uncached(self, question: str, started: float) -> AnswerResult:
        stats = AnswerStats()
        self.log(f"使用者問題：{question}")
        try:
            parsed = self.parser.parse(question)
        except tools.ToolDataError as exc:
            return AnswerResult(text=f"目前無法解析問題所需的基本資料（{exc}），請稍後再試。", route="error", gemini_calls=0, elapsed=time.perf_counter() - started)
        self.log(f"解析結果：{json.dumps(parsed.summary(), ensure_ascii=False)}")
        plan = self.router.plan(parsed, stats)
        self.log(
            f"路由={plan.route}｜planner={plan.planner_used}｜final_llm={plan.need_final_llm}｜"
            f"tools={[c.name + json.dumps(c.kwargs, ensure_ascii=False) for c in plan.tool_calls]}"
        )
        if plan.clarification:
            return AnswerResult(text=plan.clarification, route=plan.route, gemini_calls=stats.gemini_calls, elapsed=time.perf_counter() - started)

        codes = list(dict.fromkeys([code for code, _ in parsed.stocks] +
                     [c.kwargs["stock_code"] for c in plan.tool_calls if c.kwargs.get("stock_code")]))
        chart_branch = parsed.branches[0] if parsed.branches else ""
        chart_calls = [ToolCall("get_chart_panel", {"stock_code": c, **({"branch_name": chart_branch} if chart_branch else {})}) for c in codes]
        combined = self._run_tools(plan.tool_calls + chart_calls)
        results = [r for r in combined if r.name != "get_chart_panel"]
        chart_results = [r for r in combined if r.name == "get_chart_panel"]
        panels = []
        for code, chart in zip(codes, chart_results):
            panel = dict(chart.data) if chart.ok else {"stock_code": code, "error": "K 線資料暫時無法取得；以下保留已取得的分析。"}
            panels.append(panel)
        text, llm_ok = self._compose(question, plan, results, stats)
        elapsed = time.perf_counter() - started
        self.log(
            f"完成｜Gemini 呼叫 {stats.gemini_calls} 次（{stats.gemini_latency:.2f}s）｜"
            f"最終 prompt {stats.prompt_chars:,} 字｜回答 {len(text):,} 字｜總耗時 {elapsed:.2f}s"
        )
        return AnswerResult(
            text=text,
            route=plan.route,
            gemini_calls=stats.gemini_calls,
            elapsed=elapsed,
            cacheable=llm_ok and all(r.ok for r in combined),
            panels=panels,
        )

    def _run_tools(self, calls: Sequence[ToolCall]) -> List[tools.ToolResult]:
        cancel_event = threading.Event()
        futures = [(call, self.executor.submit(tools.run_tool, call.name, call.kwargs, cancel_event)) for call in calls]
        done, _ = wait([f for _, f in futures], timeout=self.config.tool_timeout_seconds)
        results: List[tools.ToolResult] = []
        for call, future in futures:
            if future in done:
                result = future.result()
            else:
                cancel_event.set()
                result = tools.ToolResult(
                    name=call.name,
                    ok=False,
                    error="timeout",
                    user_message=f"{tools._TOOL_FAILURE_MESSAGES.get(call.name, '資料取得')}（超過 {self.config.tool_timeout_seconds:.0f} 秒逾時）",
                    elapsed=self.config.tool_timeout_seconds,
                )
            self.log(
                f"Tool {call.name}｜{'成功' if result.ok else '失敗'}｜{result.elapsed:.2f}s｜"
                f"cache hit={result.cache_hits} miss={result.cache_misses}"
                + (f"｜錯誤={result.error}" if result.error else "")
            )
            results.append(result)
        return results

    def _compose(self, question: str, plan: QueryPlan, results: List[tools.ToolResult], stats: AnswerStats) -> Tuple[str, bool]:
        """回傳 (回答文字, 是否可快取)。"""
        rule_answer = build_rule_based_answer(results)
        if not plan.need_final_llm or not any(r.ok for r in results):
            return rule_answer, True
        payload = build_final_payload(question, results)
        prompt = build_final_prompt(payload)
        stats.prompt_chars = len(prompt)
        result = self.gateway.generate(prompt, purpose="final_answer", temperature=0.3)
        stats.record_gemini(result)
        if not result.ok:
            self.log(f"最終回答 Gemini 失敗：{result.error}")
            prefix = RATE_LIMIT_MESSAGE if result.rate_limited else "AI 分析暫時無法使用，以下先提供系統整理的資料。"
            return f"{prefix}\n\n{rule_answer}", False
        answer = result.text
        ungrounded = find_ungrounded_numbers(answer, payload)
        if ungrounded:
            self.log(f"數字核對未通過，改用規則式回答：{ungrounded[:10]}")
            return f"（AI 文字中有數字無法對應到原始資料，改顯示系統整理的資料）\n\n{rule_answer}", False
        if "資料時間" not in answer:
            time_line = build_data_time_line(results)
            if time_line:
                answer = f"{answer}\n\n{time_line}"
        return f"{answer}\n\n{DISCLAIMER}", True


# ============================================================
# Discord Bot
# ============================================================

class AccessGuard:
    """私人測試權限、頻道限制與使用者冷卻。"""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._last_request: Dict[int, float] = {}
        self._running: Set[int] = set()
        self._lock = threading.Lock()

    def check_permission(self, user_id: int, channel_id: int, guild_id: Optional[int] = None) -> str:
        """回傳拒絕訊息；允許時回傳空字串。

        DISCORD_AI_ALLOWED_USER_IDS=* 時不限使用者，但只接受伺服器內的訊息（不接受私訊），
        且有設定 DISCORD_AI_GUILD_IDS 時只限那些伺服器，避免 Bot 被加到其他伺服器後被陌生人使用。
        """
        if self.config.allow_all_users:
            if guild_id is None:
                return "請在伺服器頻道內使用艾斯 AI。"
            if self.config.guild_ids and guild_id not in self.config.guild_ids:
                return NOT_OPEN_MESSAGE
        elif user_id not in self.config.allowed_user_ids:
            return NOT_OPEN_MESSAGE
        if self.config.allowed_channel_ids and channel_id not in self.config.allowed_channel_ids:
            return "請在指定的 AI 測試頻道使用艾斯 AI。"
        return ""

    def acquire(self, user_id: int) -> str:
        """冷卻與重複執行檢查；允許時回傳空字串並登記執行中。"""
        now = time.time()
        with self._lock:
            if user_id in self._running:
                return "上一個問題還在處理中，請稍候。"
            elapsed = now - self._last_request.get(user_id, 0.0)
            if elapsed < self.config.user_cooldown_seconds:
                return f"請稍等 {self.config.user_cooldown_seconds - elapsed:.0f} 秒再問下一題。"
            self._last_request[user_id] = now
            self._running.add(user_id)
            return ""

    def release(self, user_id: int) -> None:
        with self._lock:
            self._running.discard(user_id)


def _startup_warmup() -> None:
    """背景預熱並自我檢查：股票名冊、分點清單（含 Google Sheet 連線）、官方權證發行商資料。

    結果只寫在 console，方便部署後直接從 Railway Logs 確認資料來源是否正常。
    """
    started = time.perf_counter()
    try:
        print(f"🔥 預熱：股票名冊 {len(tools.get_stock_name_map()):,} 檔", flush=True)
    except Exception as exc:  # 預熱失敗不影響 Bot 上線
        print(f"⚠️ 預熱：股票名冊失敗｜{type(exc).__name__}: {exc}", flush=True)
    try:
        tools.get_known_branches()
    except Exception as exc:
        print(f"⚠️ 預熱：分點清單失敗｜{type(exc).__name__}: {exc}", flush=True)
    try:
        table = tools.read_sheet_table("勝率統計")
        print(f"✅ 自我檢查：Google Sheet 可讀取（勝率統計 {len(table['df']):,} 列，Sheet 更新 {table['sheet_updated_at'] or '時間未知'}）", flush=True)
    except Exception as exc:
        print(f"❌ 自我檢查：Google Sheet 讀取失敗，分點勝率／A～E 事件／本週精選將無法使用｜{type(exc).__name__}: {exc}", flush=True)
    try:
        tools.core()._finmind_start_official_warrant_issuer_prefetch()
    except Exception as exc:
        print(f"⚠️ 預熱：官方權證發行商資料失敗｜{type(exc).__name__}: {exc}", flush=True)
    # 權證分點查詢一定要用官方權證名冊；TPEx 海外連線常回 520 並觸發長時間重試，
    # 啟動時先抓好（成功後主程式會留本機副本），避免第一個問權證的人等到逾時。
    try:
        registry = tools.core()._load_official_warrant_registry()
        print(f"🔥 預熱：官方權證名冊 {len(registry):,} 筆", flush=True)
    except Exception as exc:
        print(f"⚠️ 預熱：官方權證名冊失敗（權證分點查詢可能較慢）｜{type(exc).__name__}: {exc}", flush=True)
    print(f"🔥 預熱完成｜{time.perf_counter() - started:.1f} 秒", flush=True)


WEEKLY_PICK_ACK = "📊 本週精選候選計算中，正在整理事件、股價與分點資料。首次查詢可能需要數分鐘；完成後這張圖會更新為結果。"


def run_discord_bot(config: BotConfig) -> None:
    """啟動 Discord Bot（長時間運行）；Token 只從 DISCORD_BOT_TOKEN 讀取。

    同時支援：
    - Slash 指令 `/ask question:<問題>`（預設名稱 ask，可用 DISCORD_AI_SLASH_COMMAND 改名）
    - 文字指令 `!ace <問題>`
    注意：Slash 指令走 Gateway。這個 Bot 所屬的 Discord Application 不可設定
    Interactions Endpoint URL（現有 Cloudflare Worker 的 /w、/ww 用的是另一個 Application）。
    """
    import discord
    from discord import app_commands

    if not config.token:
        raise SystemExit("❌ 未設定 DISCORD_BOT_TOKEN，無法啟動 Discord Bot")
    if config.allow_all_users:
        scope = f"伺服器 {sorted(config.guild_ids)}" if config.guild_ids else "Bot 所在的所有伺服器"
        print(f"ℹ️ DISCORD_AI_ALLOWED_USER_IDS=*：不限使用者，只限 {scope} 內使用（不接受私訊）")
    elif not config.allowed_user_ids:
        print("⚠️ DISCORD_AI_ALLOWED_USER_IDS 未設定：所有人呼叫都會收到「尚未開放」")

    answer_image.font(29)  # Fail early if CJK fonts were not installed.
    tools.core()
    threading.Thread(target=_startup_warmup, name="ace-warmup", daemon=True).start()
    engine = AceQueryEngine(config)
    guard = AccessGuard(config)
    intents = discord.Intents.default()
    intents.message_content = True
    no_mentions = discord.AllowedMentions.none()
    prefix = config.command_prefix.lower()

    async def image_files(question: str, text: str, panels=None, guild=None, weekly=None):
        """回傳 discord.File 清單；本週精選會拆成多張（每張最多 3 檔股票）。"""
        limit = min(7_500_000, getattr(guild, "filesize_limit", 7_500_000))
        try:
            if weekly:
                images = await asyncio.to_thread(weekly_image.make_weekly_attachments, question, weekly, panels, max_bytes=limit)
            else:
                images = [await asyncio.to_thread(answer_image.make_attachment, question, text, panels, max_bytes=limit)]
        except Exception as exc:
            print(f"⚠️ 圖片產生失敗：{type(exc).__name__}: {exc}", flush=True)
            images = [await asyncio.to_thread(answer_image.make_attachment, "暫時無法產生回答",
                      "圖片產生失敗或內容超過附件容量，請縮小查詢範圍後再試。", max_bytes=limit)]
        return [
            discord.File(io.BytesIO(data), filename=f"ace-answer-{i}.{extension}" if len(images) > 1 else f"ace-answer.{extension}")
            for i, (data, extension) in enumerate(images, 1)
        ]

    async def reply_image(message, question: str, text: str, panels=None, *, pending=None, weekly=None):
        files = await image_files(question, text, panels, message.guild, weekly)
        try:
            if pending is not None:
                try:
                    # Replace all old attachments, including the waiting image.
                    return await pending.edit(content=None, attachments=files, allowed_mentions=no_mentions)
                except discord.NotFound as exc:
                    if exc.code != 10008:  # Only recreate a manually deleted message.
                        raise
                    for file in files:
                        file.reset()
            return await message.reply(files=files, mention_author=False, allowed_mentions=no_mentions)
        finally:
            for file in files:
                file.close()

    async def interaction_image(interaction, question: str, text: str, panels=None, *, ephemeral=False, weekly=None):
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        files = await image_files(question, text, panels, interaction.guild, weekly)
        try:
            try:
                # The deferred original reply is the single message for this query.
                # This also supports ephemeral replies without exposing them publicly.
                return await interaction.edit_original_response(content=None, attachments=files, allowed_mentions=no_mentions)
            except discord.NotFound as exc:
                if exc.code != 10008 or interaction.is_expired():
                    raise
                for file in files:
                    file.reset()
                return await interaction.followup.send(files=files, ephemeral=ephemeral, allowed_mentions=no_mentions, wait=True)
        finally:
            for file in files:
                file.close()

    class AceClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self) -> None:
            try:
                if config.guild_ids:
                    for guild_id in config.guild_ids:
                        guild = discord.Object(id=guild_id)
                        self.tree.copy_global_to(guild=guild)
                        synced = await self.tree.sync(guild=guild)
                        print(f"✅ Slash 指令已同步到伺服器 {guild_id}：{[c.name for c in synced]}", flush=True)
                else:
                    synced = await self.tree.sync()
                    print(f"✅ Slash 指令已全域同步（最久約 1 小時生效）：{[c.name for c in synced]}", flush=True)
            except discord.HTTPException as exc:
                print(f"⚠️ Slash 指令同步失敗：{exc}", flush=True)

    client = AceClient()

    @client.event
    async def on_ready() -> None:
        print(
            f"✅ 艾斯 AI 已上線：{client.user}｜指令 /{config.slash_command_name} 與 {config.command_prefix}｜"
            f"允許使用者 {'不限' if config.allow_all_users else str(len(config.allowed_user_ids)) + ' 人'}｜限制頻道 {len(config.allowed_channel_ids) or '不限'}｜"
            f"debug={config.debug}",
            flush=True,
        )

    @client.tree.command(name=config.slash_command_name, description="艾斯 AI：問股票、權證分點，或輸入「本週精選」")
    @app_commands.describe(question="例如：2344現在技術面怎麼樣／永豐金內湖D事件勝率／本週精選")
    async def ask_command(interaction: "discord.Interaction", question: str) -> None:
        user_id, channel_id = interaction.user.id, interaction.channel_id or 0
        denied = guard.check_permission(user_id, channel_id, interaction.guild_id)
        if denied:
            engine.log(f"拒絕使用者 {user_id}｜頻道 {channel_id}｜{denied}")
            await interaction_image(interaction, "使用權限", denied, ephemeral=True)
            return
        busy = guard.acquire(user_id)
        if busy:
            await interaction_image(interaction, "請稍候", busy, ephemeral=True)
            return
        try:
            await interaction.response.defer(thinking=True, ephemeral=config.ephemeral)
            if is_weekly_pick_question(question):
                await interaction_image(interaction, question, WEEKLY_PICK_ACK, ephemeral=config.ephemeral)
            result = await asyncio.to_thread(engine.answer, question)
            await interaction_image(interaction, question, result.text, result.panels, ephemeral=config.ephemeral,
                                    weekly=result.weekly if result.layout == "weekly_pick" else None)
            print(f"✅ Discord /{config.slash_command_name} 回覆圖片已更新｜route={result.route}｜計算 {result.elapsed:.1f}s｜快取={result.cache_hit}", flush=True)
        except discord.HTTPException as exc:
            print(f"⚠️ Discord /{config.slash_command_name} 回覆失敗：{exc}", flush=True)
        except Exception as exc:  # 單題失敗不可讓 Bot 中斷
            print(f"❌ 艾斯 AI /{config.slash_command_name} 處理失敗：{type(exc).__name__}: {exc}", flush=True)
            try:
                await interaction_image(interaction, "暫時無法完成", "處理問題時發生錯誤，請稍後再試。", ephemeral=config.ephemeral)
            except discord.HTTPException as send_exc:
                print(f"⚠️ 錯誤訊息送出失敗：{send_exc}", flush=True)
        finally:
            guard.release(user_id)

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if message.author.bot:
            return
        content = (message.content or "").strip()
        lowered = content.lower()
        if not (lowered == prefix or lowered.startswith(prefix + " ")):
            return
        question = content[len(prefix):].strip()
        user_id, channel_id = message.author.id, message.channel.id

        denied = guard.check_permission(user_id, channel_id, message.guild.id if message.guild else None)
        if denied:
            engine.log(f"拒絕使用者 {user_id}｜頻道 {channel_id}｜{denied}")
            await reply_image(message, "使用權限", denied)
            return
        if not question:
            await reply_image(message, "!ace 使用說明", HELP_MESSAGE)
            return
        busy = guard.acquire(user_id)
        if busy:
            await reply_image(message, "請稍候", busy)
            return
        pending = None
        try:
            if is_weekly_pick_question(question):
                pending = await reply_image(message, question, WEEKLY_PICK_ACK)
            async with message.channel.typing():
                result = await asyncio.to_thread(engine.answer, question)
            await reply_image(message, question, result.text, result.panels, pending=pending,
                              weekly=result.weekly if result.layout == "weekly_pick" else None)
            print(f"✅ Discord {config.command_prefix} 回覆圖片已送出／更新｜route={result.route}｜計算 {result.elapsed:.1f}s｜快取={result.cache_hit}", flush=True)
        except discord.HTTPException as exc:
            print(f"⚠️ Discord 訊息送出失敗：{exc}", flush=True)
        except Exception as exc:  # 單題失敗不可讓 Bot 中斷
            print(f"❌ 艾斯 AI 處理問題失敗：{type(exc).__name__}: {exc}", flush=True)
            try:
                await reply_image(message, "暫時無法完成", "處理問題時發生錯誤，請稍後再試。", pending=pending)
            except discord.HTTPException as send_exc:
                print(f"⚠️ 錯誤訊息送出失敗：{send_exc}", flush=True)
        finally:
            guard.release(user_id)

    client.run(config.token)


# ============================================================
# 入口
# ============================================================

def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="艾斯 AI Discord 問答機器人")
    parser.add_argument("--ask", help="不連 Discord，直接在 console 回答一個問題")
    parser.add_argument("--plan", help="只顯示問題解析與路由，不抓資料、不呼叫 Gemini（Planner 路由除外）")
    parser.add_argument("--image-output", help="搭配 --ask 將完整回答另存 PNG 圖片")
    parser.add_argument("--debug", action="store_true", help="等同 DISCORD_AI_DEBUG=1")
    args = parser.parse_args(argv)

    if args.debug:
        os.environ["DISCORD_AI_DEBUG"] = "1"
    config = BotConfig.from_env()

    if args.plan:
        engine = AceQueryEngine(config)
        parsed, plan, stats = engine.plan_only(args.plan)
        print(json.dumps({
            "parsed": parsed.summary(),
            "route": plan.route,
            "need_final_llm": plan.need_final_llm,
            "planner_used": plan.planner_used,
            "gemini_calls_for_planning": stats.gemini_calls,
            "tools": [{"name": c.name, "kwargs": c.kwargs} for c in plan.tool_calls],
            "clarification": plan.clarification,
        }, ensure_ascii=False, indent=2))
        return
    if args.ask:
        engine = AceQueryEngine(config)
        result = engine.answer(args.ask)
        if args.image_output:
            if result.layout == "weekly_pick":
                base, ext = os.path.splitext(args.image_output)
                for i, image in enumerate(weekly_image.render_weekly_pages(args.ask, result.weekly, result.panels), 1):
                    path = f"{base}-{i}{ext or '.png'}"
                    image.save(path)
                    print(f"圖片已儲存：{path}")
            else:
                answer_image.render_answer(args.ask, result.text, result.panels).save(args.image_output)
                print(f"圖片已儲存：{args.image_output}")
        print("=" * 60)
        for index, chunk in enumerate(split_discord_message(result.text, config.max_message_chars), 1):
            print(f"--- Discord 訊息 {index} ---")
            print(chunk)
        print("=" * 60)
        print(f"route={result.route}｜Gemini 呼叫 {result.gemini_calls} 次｜{result.elapsed:.2f} 秒")
        return
    run_discord_bot(config)


if __name__ == "__main__":
    main()
