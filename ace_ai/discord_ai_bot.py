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
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import warrant_ai_tools as tools
import weekly_pick
import answer_image
import weekly_image
import sector_analysis
import sys
import traceback

import numpy as np

import sector_match
import sector_radar

# 主程式每算一檔股票都會印「📅 週報統計區間」，Bot 一題會印上百行，把真正的訊息洗掉。
# 這裡只在 Bot 行程過濾，不動主程式，週報與回測的輸出完全不受影響。
_LOG_MUTE_PATTERNS = tuple(x for x in os.getenv("DISCORD_AI_LOG_MUTE", "週報統計區間").split("|") if x)


class _FilteredStdout:
    """過濾指定關鍵字的 stdout。

    print() 會分成「訊息」與「換行」兩次 write，所以擋掉訊息之後要把緊接著的換行一起吃掉，
    否則 log 會留下一堆只有時間戳的空行，真正要看的訊息反而被洗掉。
    """

    def __init__(self, stream):
        self._stream = stream
        self._drop_newline = False
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            if self._drop_newline and text in (chr(10), chr(13) + chr(10), ""):
                self._drop_newline = False
                return len(text)
            if _LOG_MUTE_PATTERNS and any(pattern in text for pattern in _LOG_MUTE_PATTERNS):
                self._drop_newline = not text.endswith(chr(10))
                return len(text)
            self._drop_newline = False
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


if _LOG_MUTE_PATTERNS and not isinstance(sys.stdout, _FilteredStdout):
    sys.stdout = _FilteredStdout(sys.stdout)

import market_data
import market_scan
import sector_roster
import local_market_cache
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
    admin_command_name: str = "ace"
    guild_ids: Set[int] = field(default_factory=set)
    ephemeral: bool = False
    allow_all_users: bool = False
    # 本週精選限定使用者（Discord 使用者 ID，逗號分隔）；沒設定時任何人都不能用。
    weekly_pick_user_ids: Set[int] = field(default_factory=set)
    weekly_pick_allow_admins: bool = True
    # !ace 文字指令（預設關閉，只用 /ask）
    prefix_command_enabled: bool = False

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
            admin_command_name=(os.getenv("DISCORD_AI_ADMIN_COMMAND", "ace").strip().lower() or "ace"),
            guild_ids=_parse_id_set(os.getenv("DISCORD_AI_GUILD_IDS", "")),
            ephemeral=_env_flag("DISCORD_AI_EPHEMERAL"),
            allow_all_users=_is_allow_all(os.getenv("DISCORD_AI_ALLOWED_USER_IDS", "")),
            weekly_pick_user_ids=_parse_id_set(os.getenv("DISCORD_AI_WEEKLY_PICK_USER_IDS", "")),
            weekly_pick_allow_admins=_env_flag("DISCORD_AI_WEEKLY_PICK_ALLOW_ADMINS", "1"),
            prefix_command_enabled=_env_flag("DISCORD_AI_PREFIX_COMMAND_ENABLE", "0"),
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
    "volume": ("爆量", "量能", "均量", "放量", "量縮", "量增", "帶量", "窒息量"),
    "futures": ("台指期", "期貨", "未平倉", "空單", "多單", "三大法人期貨", "外資期貨"),
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
    sector: Optional[Dict[str, str]] = None

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
            "sector": self.sector,
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
        sector = sector_analysis.detect_request(question)   # 大盤層級的問題會在解析器裡就被排除
        if sector is not None:
            return ParsedQuestion(original=question, intents={"sector"}, sector=sector)
        kf = tools.core()
        text_upper = question.upper()
        parsed = ParsedQuestion(original=question, intents=self.detect_intents(text_upper))
        index_code = tools.resolve_index_code(question)
        if index_code:
            # 大盤／櫃買走和個股完全相同的型態流程（K 線＋評分卡＋AI），資料來自指數日K。
            parsed.stocks = [(index_code, tools.INDEX_CODES[index_code])]
            parsed.intents.add("index")
            return parsed

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
    "我可以幫你查股票、族群與權證分點資料，請用 `/ask 問題`，例如：\n"
    "• `/ask 2330現在型態好嗎`\n"
    "• `/ask 半導體族群哪檔型態比較好`\n"
    "• `/ask 現在所有族群誰最強`\n"
    "• `/ask 航運股今天誰漲最多`\n"
    "• `/ask 金融股有哪些`\n"
    "• `/ask 我2330成本2000可以怎麼觀察`\n"
    "• `/ask 2330現在技術面怎麼樣`\n"
    "• `/ask 2330現在在大量區哪裡`\n"
    "• `/ask 2330最近有哪些分點在加碼`\n"
    "• `/ask 2330有哪些高勝率分點最近在加碼`\n"
    "• `/ask 永豐金內湖勝率多少`\n"
    "• `/ask 永豐金內湖D事件勝率`\n"
    "• `/ask 永豐金內湖最近在買什麼`\n"
    "• `/ask 2330最近有什麼新聞，偏利多還是利空`\n"
    "• `/ask 目前權證買超金額最大的是誰？技術面如何`\n"
    "可以接著追問，例如先問「幫我分析2330」，再問「那它的壓力在哪」「跟聯發科比呢」；輸入「重新開始」可清除上一題。"
)

PLANNER_TOOLS = (
    "get_stock_overview", "get_technical_analysis", "get_volume_profile", "get_warrant_branch",
    "get_high_winrate_branches_buying", "get_branch_performance", "get_branch_recent_trades",
    "get_branch_stock_history", "get_branch_winrate_rank", "get_recent_news", "query_google_sheet",
    "get_branch_event_performance", "get_branch_recent_behavior", "detect_current_branch_events",
    "get_sheet_stock_chips", "get_branch_stock_position", "get_cost_position_context",
)


_TOP_WORDS_RE = re.compile(r"最大|最多|最高|排行|排名|前\s*\d*\s*名|前幾|第一名|哪一?檔|哪些股票|誰")


def is_top_warrant_question(parsed: "ParsedQuestion") -> bool:
    """沒有指定股票、問權證買超／買進金額排行（例如「目前權證買超金額最大的是誰」）。勝率排行走原本路由。"""
    text = parsed.original
    return ("warrant" in parsed.intents or "權證" in text) and bool(_TOP_WORDS_RE.search(text)) and "win_rate" not in parsed.intents


class QueryRouter:
    """規則判斷優先；只有規則無法決定要用哪些 Tool 時才呼叫 Gemini Planner。"""

    def __init__(self, gateway: "GeminiGateway", config: BotConfig, log: DebugLog) -> None:
        self.gateway = gateway
        self.config = config
        self.log = log

    def plan(self, parsed: ParsedQuestion, stats: "AnswerStats") -> QueryPlan:
        if parsed.sector is not None:
            return QueryPlan(route="rule_sector", need_final_llm=parsed.sector["mode"] in ("technical", "momentum"))
        if not parsed.branches and _BREADTH_RE.search(parsed.original) and not (parsed.stocks and "index" not in parsed.intents):
            # 盤面廣度：指數漲跌 vs 多數個股，不需要任何個股資料。
            plan = QueryPlan(route="rule_breadth", need_final_llm=True)
            plan.add("get_index_contribution")
            plan.add("get_market_breadth")
            if "futures" in parsed.intents:
                plan.add("get_futures_positions")
            return plan
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
        # 有股票的問題一律交給 AI 客觀回答：型態／成本／K 棒／漲跌看法／技術面／沒有特定類別的問題走型態路由
        # （K 線＋型態評分卡＋AI 回答）；新聞、權證、勝率等指定類別才走各自的資料組合，只問股價才直接排版。
        other_categories = categories - {"price", "technical", "volume_profile"}
        if parsed.stocks and ("cost" in intents or not other_categories):
            if categories == {"price"} and not analysis and "cost" not in intents:
                return self._stock_plan(parsed, categories, analysis)
            return self._pattern_plan(parsed)
        if parsed.stocks:
            return self._stock_plan(parsed, categories, analysis)
        if "futures" in intents and not parsed.branches:
            plan = QueryPlan(route="rule_futures", need_final_llm=True)
            plan.add("get_futures_positions")
            for code, _ in parsed.stocks[:1]:
                plan.add("get_stock_overview", stock_code=code)
                plan.add("get_technical_analysis", stock_code=code)
            return plan
        if not parsed.stocks and is_top_warrant_question(parsed):
            # 「權證買超金額最大的是誰」：先讀 TOP15 共識淨買超排行，引擎再對第一名補型態資料與 K 線（仍只呼叫 1 次 Gemini）。
            plan = QueryPlan(route="rule_top_warrant", need_final_llm=True)
            plan.add("get_top_warrant_buy_stocks")
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
                # 所有一般權證問題只讀 Google Sheet；MoneyDJ 僅限管理員明確啟用的備援圖片。
                # 天數和 K 線圖一致（近 70 個交易日）；使用者明講「近 10 日」才用指定天數。
                plan.add("get_sheet_stock_chips", stock_code=code,
                         days=parsed.days if parsed.days_specified else tools.CHIPS_DAYS)
                # 沒有分點事件時，AI 至少要能用量能與位置把話講完整，不要只寫「沒有偵測到」。
                plan.add("get_stock_overview", stock_code=code)
                plan.add("get_technical_analysis", stock_code=code)
                plan.add("get_volume_profile", stock_code=code)
            if "news" in categories:
                plan.add("get_recent_news", stock_code=code)
                # 新聞統整時附上當日收盤與漲跌，讓 AI 能說明股價當下的反應（快取資料，幾乎不增加時間）。
                plan.add("get_stock_overview", stock_code=code)
        # 只問股價（例如「2344股價多少」）才直接排版數字，其餘都交給 AI 回答。
        plan.need_final_llm = analysis or bool(categories - {"price"}) or len(parsed.stocks) > 1
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
            if code in tools.INDEX_CODES:
                # 大盤／櫃買固定附上三大法人台指期未平倉（僅供參考，不做多空判斷）。
                plan.add("get_futures_positions")
            # 純型態問題只算技術結構，不讀權證分點，避免圖片過長與不必要的 Sheet 呼叫。
        return plan

    def _default_bundle(self, parsed: ParsedQuestion) -> QueryPlan:
        plan = QueryPlan(route="rule_stock_bundle", need_final_llm=True)
        for code, _ in parsed.stocks:
            plan.add("get_stock_overview", stock_code=code)
            plan.add("get_technical_analysis", stock_code=code)
            plan.add("get_volume_profile", stock_code=code)
            plan.add("get_sheet_stock_chips", stock_code=code,
                     days=parsed.days if parsed.days_specified else tools.CHIPS_DAYS)
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
                # Planner 即使選到 MoneyDJ 類工具，一般會員路由也強制落回 Google Sheet。
                for code in stocks[:2]:
                    plan.add("get_sheet_stock_chips", stock_code=code, days=days)
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
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_source: str = "none"


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
    - 同一時間最多 DISCORD_AI_GEMINI_CONCURRENCY（預設 2）個 Gemini 呼叫，避免被併發打爆額度。
    """

    DAILY_LIMIT = max(0, tools._env_int("DISCORD_AI_GEMINI_DAILY_LIMIT", 0))   # 0＝不限

    def __init__(self, log: DebugLog) -> None:
        self.log = log
        self._lock = threading.BoundedSemaphore(max(1, tools._env_int("DISCORD_AI_GEMINI_CONCURRENCY", 2)))
        self._day = ""
        self._day_calls = 0
        self._count_lock = threading.Lock()

    def _quota_left(self) -> bool:
        """免費方案有每日請求上限；超過軟上限就只出規則式內容，不再呼叫 AI。"""
        if not self.DAILY_LIMIT:
            return True
        today = tools.taipei_now().strftime("%Y-%m-%d")
        with self._count_lock:
            if self._day != today:
                self._day, self._day_calls = today, 0
            if self._day_calls >= self.DAILY_LIMIT:
                return False
            self._day_calls += 1
            return True

    def generate(self, prompt: str, purpose: str, schema: Optional[Dict[str, Any]] = None, temperature: float = 0.3) -> GeminiResult:
        if not self._quota_left():
            self.log(f"Gemini 今日次數已達上限 {self.DAILY_LIMIT}，改用規則式輸出：{purpose}")
            return GeminiResult(ok=False, text="", error="daily_limit")
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
        # 目前主程式的 _call_gemini_with_retry 只回傳文字，不暴露 usage_metadata；
        # 因此這裡誠實標記為 estimated。之後若主程式改成回傳官方 usage，可直接替換本段。
        output_text = str(text).strip() if text else ""
        input_tokens = max(1, round(len(prompt) / 4)) if prompt else 0
        output_tokens = max(1, round(len(output_text) / 4)) if output_text else 0
        total_tokens = input_tokens + output_tokens
        self.log(
            f"Gemini 呼叫｜用途={purpose}｜model={kf.GEMINI_MODEL}｜latency={latency:.2f}s｜"
            f"prompt={len(prompt):,} 字｜tokens≈{input_tokens}+{output_tokens}={total_tokens}（estimated）｜"
            f"結果={'成功' if text else '失敗'}"
        )
        tools.record_api_event("Gemini", status=200 if text else 500, latency=latency, detail=purpose)
        if text:
            return GeminiResult(ok=True, text=output_text, latency=latency, purpose=purpose,
                                input_tokens=input_tokens, output_tokens=output_tokens,
                                total_tokens=total_tokens, token_source="estimated")
        rate_limited = any(k.lower() in last_error.lower() for k in _RATE_LIMIT_KEYWORDS)
        return GeminiResult(
            ok=False,
            error=last_error or "Gemini 沒有回傳內容",
            rate_limited=rate_limited,
            latency=latency,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=0,
            total_tokens=input_tokens,
            token_source="estimated",
        )


# ============================================================
# 最終回答 Prompt 與數字核對
# ============================================================

FINAL_BASE_PROMPT = """你是「艾斯 AI 台股數據研究助手」。你的回答要像熟悉台股技術面、價量、大量區與權證分點的研究者：先理解使用者真正想問什麼，再從 tool_results 挑最有判別力的證據回答，不要像模板客服，也不要把所有欄位逐項朗讀。

規則：
1. 只能依 tool_results 的事實與數字回答，不可自創資料。使用者自己提供的成本或假設價格必須明確標成「你的成本／假設價格」，不可當成現價。
2. 保持客觀。可以直接說目前結構偏強、偏弱、轉強、承壓、支撐較明確等，但每個判斷都要緊接數據或型態依據；同時點出最重要的不利條件。買超、高勝率都不是未來保證。
3. 不給目標價或報酬保證，不替使用者做最後買賣決定。
4. 回答以問題為中心：通常先用 1～2 句直接回答，再補 2～3 個最重要證據，最後視需要說後續最值得觀察什麼。不要固定套【回答】【觀察重點】；除非資訊很多，否則自然分段即可。
5. 圖片本身已顯示 K 線、均線、布林、大量區與分點標記，文字不要再逐項報數；只引用真正影響判斷的 1～3 個數據。沒有資料的欄位直接略過，不要在回答中列一串系統缺漏原因。
6. 不要提資料供應商、Google Sheet、工作表或內部系統名稱。需要時說「日K資料」「權證分點統計」「歷史事件統計」。
7. 問未來漲跌或機率時，不自行預測；改說目前有哪些偏多／偏空條件，以及哪個條件變化最值得追蹤。K 棒型態則依實體、影線、量能與所處位置說明是否符合常見定義，不斷言後續。
8. 全部使用繁體中文，語氣自然、精簡、有分析感；同一件事不要重複。"""

FINAL_TECH_RULES = """技術面規則：
- 訊號狀態分三種，不可混用：「收盤確認」＝signal_status、型態評分、布林與均線訊號（都用最後一根已收盤 K 棒）；「盤中暫時」＝intraday_observation.changes，一定要寫「盤中暫時…，尚待收盤確認」，不可說成已突破、已站穩；「資料不足」＝欄位為 null 或寫資料不足，要說「目前無法確認」，不可當成沒有訊號。
- 盤中量能看 intraday_volume_estimate：以 vs_mv20_pct（對 20 日均量）為主要判讀，再補 vs_mv5_pct 與 vs_prev_day_pct；昨日本身可能是異常量，不可只跟昨日比。一律講明是估算、收盤前會變動，並照 confidence（初步／估算／可靠）調整語氣：「初步」時不要給肯定結論。沒有這個欄位就不拿盤中累計量和日均量比較；volume_suspect=true 不解讀量能。
- 布林依 bollinger 的 position、signals、width_trend、squeeze、band_walk、breakout 欄位判讀。影線穿越不等於收盤突破，壓縮不預測方向，觸軌不代表反轉。均線扣抵推算是「收盤維持不變」的條件推算，不是預測。"""

FINAL_NEWS_RULES = """新聞規則：只能用 get_recent_news 的 title、summary、content、summary_points（「公司名:本公司…」是公司重大訊息，屬事實）。
- 同一事件的多篇報導合併，整理成 2～4 點：發生什麼事、關鍵數字（只用 content／summary 出現過的）、來源與日期；不要逐條重列標題。
- 聳動字眼不是事實；法人目標價、獲利預估要寫「某機構估計」。content_source 為「RSS 摘要」或「僅標題」時只描述標題寫到的事實。
- 【可能利多】【可能利空／風險】分開寫，只寫新聞提到的因素；沒有利空就寫「新聞內容未提及明顯利空，但資訊有限」。【綜合觀察】1～2 句說明份量與待確認資訊，不下漲跌結論。"""

FINAL_PATTERN_RULES = """型態／成本／操作問題（有 get_pattern_scorecard）：
- 先直接回答使用者真正問的問題，再挑影響最大的型態、大量區／支撐、均線或權證分點證據。不要把評分卡五大項逐一念完。
- 問成本／操作：可說成本相對現價與帳面損益，再用「若守住／若跌破／若重新站回」的條件式框架說明，不替使用者下買賣決定。
- 問型態：可以直接說目前結構偏強、偏弱或中性，並引用型態分數及最關鍵的一個加分、一個壓力。
- 比較兩檔：描述兩者技術結構差異與各自風險，不提供投資選擇或推薦。
- 分點資料有價值時，優先說「在哪個型態／大量區附近布局、目前是否仍持有、對應事件歷史表現」；不要只報總勝率。
- 沒有明確訊號或沒有資料的項目直接省略，不要硬湊固定段落。"""

FINAL_CHIPS_RULES = """權證分點問題（有 get_sheet_stock_chips）：
- 第一句直接回答有沒有追蹤分點的 A～E 事件：有就點名分點、事件別、買進金額、目前部位與勝率；沒有就一句話帶過「近 N 個交易日沒有追蹤分點的大額買進紀錄」，不要解釋系統怎麼定義，也不要寫「我們系統」「缺乏訊號」「先行指標」這類空話。
- 沒有分點事件時，改用技術面把話講完整，像技術分析者在看盤：先用 volume_trend 說量能（近 5 日均量是前 20 日的幾倍、連續放大幾天、是不是 20 日最大量），再說價格位置（站上哪些均線、相對兩大量區、布林狀態）。
- volume_trend.level 是「明顯放大」或「溫和放大」時，不可以寫成量能平穩或「市場自然供需」，也不要叫使用者「觀察量能是否放大」——資料已經放大就直接說出來，並說明配合的價格位置代表什麼。
- 沒有分點資料不等於利多或利空，但也不可以因此省略技術面說明。"""

FINAL_RANK_RULES = """權證共識淨買超排行（有 get_top_warrant_buy_stocks）：
【回答】先寫排行名稱與統計期間（照 source 與 period 寫，不要自己改名，也不要用「共識」「全分點」等字眼），並註明統計範圍是追蹤的分點、不是全市場；接著列出前 3 名（名次、股票、net_buy_cost_text、主要分點，分點是高勝率或精選五分點要點出）。
unrealized_return_text 是這些分點目前部位的估計未實現損益，要說明是估計值、不是已實現。
接著針對第一名，依型態評分卡回答技術面（型態分數、grade 與最主要的一個得分與一個失分原因）。
【觀察重點】照型態／成本／操作問題的規則，針對第一名撰寫。"""

FINAL_FORMAT_GENERAL = """依問題自然組織 2～4 個短段落；只有新聞或多主題真的需要分組時才使用小標題，不要每題固定套同一組標題。"""
FINAL_FORMAT_NEWS = """區塊依序使用：【回答】（1～2 句直接說整體偏利多、偏利空或好壞參半）、【新聞重點】、【可能利多】、【可能利空／風險】、【綜合觀察】。"""
FINAL_FORMAT_PATTERN = """用自然短段落回答，不強制固定標題；先回答，再給最重要證據與後續觀察條件。"""


def _is_empty_value(value: Any) -> bool:
    """判斷是不是空值。

    不能用 `value not in (None, "", [], {})`：numpy 陣列會逐元素比較、回傳陣列，
    Python 判斷真假時就會丟出「The truth value of an empty array is ambiguous」。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, np.ndarray):
        return value.size == 0
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _prune_empty(value: Any) -> Any:
    """移除空值，縮小送給 Gemini 的 JSON。numpy 型別一併換成 Python 原生型別，
    否則後面 json.dumps 會直接失敗。"""
    if isinstance(value, dict):
        for key, raw in value.items():
            if isinstance(raw, np.ndarray):   # 哪個工具回傳陣列要留紀錄，之後可以在來源就改成清單
                print(f"⚠️ tool_results 欄位是 numpy 陣列，已自動轉換：{key}｜長度 {raw.size}", flush=True)
        pruned = {k: _prune_empty(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if not _is_empty_value(v)}
    if isinstance(value, list):
        return [v for v in (_prune_empty(i) for i in value) if not _is_empty_value(v)]
    if isinstance(value, np.ndarray):
        return [_prune_empty(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value


# 送進 Gemini 前拿掉的欄位：說明文字（規則已寫在 prompt）、圖片專用或重複的欄位。
_PAYLOAD_DROP_KEYS = {
    "data_source", "indicator_definition", "volume_unit_note", "definition_note", "note", "rules", "method", "worksheet",
    "source_type", "summary_points_source", "url", "squeeze_reference_count", "squeeze_threshold_pct",
    "mid_change_10d_pct", "close_range_10d_pct", "volume_ratio_prior20",
}
_BOLLINGER_KEEP = {"upper", "mid", "lower", "percent_b", "width_pct_of_mid", "position", "signals", "width_trend",
                   "width_change_5d_pct", "squeeze", "sideways", "band_walk", "breakout_up", "breakout_down",
                   "return_inside", "squeeze_breakout"}


def _drop_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_keys(v) for k, v in value.items() if k not in _PAYLOAD_DROP_KEYS}
    if isinstance(value, list):
        return [_drop_keys(v) for v in value]
    return value


def _candle_shape(d: Dict[str, Any]) -> Dict[str, Any]:
    """今天 K 棒結構（Python 計算）：實體、上影線、下影線占前日收盤 %，給 AI 判斷仙人指路、長上影等型態。"""
    o, h, l, c, prev = (d.get(k) for k in ("open", "high", "low", "close", "prev_close"))
    if None in (o, h, l, c) or not prev:
        return {}

    def pct(value: float) -> float:
        return round(value / prev * 100, 2)

    return {
        "color": "紅K" if c > o else "黑K" if c < o else "十字／平盤",
        "body_pct": pct(abs(c - o)),
        "upper_shadow_pct": pct(h - max(o, c)),
        "lower_shadow_pct": pct(min(o, c) - l),
        "range_pct": pct(h - l),
        "body_share_of_range_pct": round(abs(c - o) / (h - l) * 100, 1) if h > l else None,
        "upper_shadow_share_of_range_pct": round((h - max(o, c)) / (h - l) * 100, 1) if h > l else None,
        "close_position_in_range_pct": round((c - l) / (h - l) * 100, 1) if h > l else None,
    }


def _compact_tool_data(name: str, data: Dict[str, Any], has_scorecard: bool) -> Optional[Dict[str, Any]]:
    """依問題類型精簡 Tool 資料；回傳 None 表示這份資料已被型態評分卡涵蓋，不必再送。"""
    if has_scorecard and name in ("get_cost_position_context", "get_sheet_stock_chips", "get_volume_profile"):
        return None  # 成本位置、支撐壓力、追蹤分點、量區型態都已整理在評分卡
    data = dict(data)
    if name == "get_technical_analysis":
        data["bollinger"] = {k: v for k, v in (data.get("bollinger") or {}).items() if k in _BOLLINGER_KEEP}
        if has_scorecard:
            # 均線值、排列、扣抵都在評分卡；這裡只留評分卡沒有的 KD／MACD 訊號與布林狀態。
            data = {k: data.get(k) for k in ("stock_code", "data_date", "signal_status", "intraday_observation", "kd", "macd", "bollinger", "ma20_cross_recent_3_days", "ma_kline_signals")}
            data["kd"] = {"signals": (data.get("kd") or {}).get("signals")}
            data["macd"] = {"signals": (data.get("macd") or {}).get("signals"), "osc_trend": (data.get("macd") or {}).get("osc_trend")}
        else:
            data["ma_deduction"] = {k: {f: v.get(f) for f in ("direction_now", "turn_text", "tomorrow_close_needed_to_rise")}
                                    for k, v in (data.get("ma_deduction") or {}).items() if k in ("MA20", "MA60")}
    elif name == "get_pattern_scorecard":
        data["ma_deduction"] = {k: {f: v.get(f) for f in ("direction_now", "turn_text", "tomorrow_close_needed_to_rise")}
                                for k, v in (data.get("ma_deduction") or {}).items()}
        data["plus_reasons"] = (data.get("plus_reasons") or [])[:4]
        data["minus_reasons"] = (data.get("minus_reasons") or [])[:4]
    elif name == "get_stock_overview":
        candle = _candle_shape(data)
        data = {k: data.get(k) for k in ("stock_code", "stock_name", "data_date", "data_source", "intraday", "close", "change_pct", "volume_status", "volume_trend", "volume_ratio_vs_mv5", "volume_ratio_vs_mv20")}
        data["candle"] = candle
    elif name == "get_recent_news":
        data["articles"] = [{k: v for k, v in a.items() if k not in ("event_key",) and not (k == "summary" and a.get("content"))}
                            for a in data.get("articles") or []]
    return _drop_keys(data)


def build_final_payload(question: str, results: Sequence[tools.ToolResult]) -> Dict[str, Any]:
    has_scorecard = any(r.ok and r.name == "get_pattern_scorecard" for r in results)
    tool_results: Dict[str, Any] = {}
    for result in results:
        if result.ok:
            data = _compact_tool_data(result.name, result.data, has_scorecard)
            if data is None:
                continue
        else:
            data = result.to_payload()
        key = result.name
        suffix = result.data.get("stock_code") or result.data.get("branch") or ""
        if key in tool_results or suffix:
            key = f"{key}:{suffix}" if suffix else f"{key}:{len(tool_results)}"
        tool_results[key] = data
    return _prune_empty({"question": question, "tool_results": tool_results})


FINAL_BREADTH_RULES = ("【盤面結構】沒有 get_index_contribution 時，第一句就要說「今天的指數貢獻榜要 15:00 後才有」，不可以拿漲幅排名或前一個交易日的資料當成今天的貢獻榜。回答『今天是不是都在拉權值股／誰在拉大盤／誰拖累指數』時，必須先看 get_index_contribution，不可只看漲跌幅。先直接回答結論，再分別列加權與櫃買的拉升 TOP5、拖累 TOP5；每檔優先引用 points（貢獻點數）、weight_pct（指數權重）與 change_pct（漲跌幅）。top5_positive_share_pct／top5_negative_share_pct 是前五大貢獻占已涵蓋正／負貢獻的比例，可用來說明集中度；concentration 是規則式集中度結論。盤中一定說明 basis=盤中估算、market_cap_coverage_pct（市值涵蓋率）與收盤前仍會變動；若 top5_positive_certified／top5_negative_certified 為 false，不可把榜單講成交易所最終完整排名。get_market_breadth 只用來補充加權與櫃買差異、上漲比率與中小型股是否跟上，不可取代貢獻點數。不要預測未來指數點位，也不要給買賣建議。")


FINAL_FUTURES_RULES = ("【台指期未平倉】只陳述口數與前一日變化，並說明未平倉含現貨避險部位、不能單獨當多空訊號；不可用它推論明天漲跌，也不可給買賣建議。")


def build_final_prompt(payload: Dict[str, Any]) -> str:
    """只放這題用得到的規則：技術面、新聞、型態評分卡各自一段，避免每題都送全部規則。"""
    names = {key.split(":", 1)[0] for key in (payload.get("tool_results") or {})}
    sections = [FINAL_BASE_PROMPT]
    if names & {"get_technical_analysis", "get_pattern_scorecard", "get_volume_profile"}:
        sections.append(FINAL_TECH_RULES)
    if "get_recent_news" in names:
        sections.append(FINAL_NEWS_RULES)
    if "get_sheet_stock_chips" in names:
        sections.append(FINAL_CHIPS_RULES)
    if "get_market_breadth" in names:
        sections.append(FINAL_BREADTH_RULES)
    if "get_futures_positions" in names:
        sections.append(FINAL_FUTURES_RULES)
    if "get_top_warrant_buy_stocks" in names:
        sections.append(FINAL_RANK_RULES)
    if "get_pattern_scorecard" in names:
        sections += [FINAL_PATTERN_RULES, FINAL_FORMAT_PATTERN]
    elif names == {"get_recent_news"} or names == {"get_recent_news", "get_stock_overview"}:
        sections.append(FINAL_FORMAT_NEWS)
    else:
        sections.append(FINAL_FORMAT_GENERAL)
    payload_json = json.dumps(payload.get("tool_results") or {}, ensure_ascii=False, separators=(",", ":"), default=tools.json_safe)
    return "\n\n".join(sections) + f"\n\n使用者問題：{payload['question']}\n\ntool_results（JSON）：\n{payload_json}\n"


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


def _unit_variants(token: str) -> Set[str]:
    """大金額換算成「萬／億」的寫法：1,120,000,000 → 11.2（億）、112000（萬），不是四捨五入的約數。"""
    try:
        value = float(token.replace(",", "").lstrip("+"))
    except ValueError:
        return set()
    variants: Set[str] = set()
    for unit in (1e4, 1e8):
        if abs(value) >= unit:
            scaled = value / unit
            for digits in (0, 1, 2):
                if abs(round(scaled, digits) - scaled) < 1e-9:  # 只接受換算後剛好整除到該位數，避免把四捨五入當成對得上
                    variants |= _number_variants(f"{round(scaled, digits):.{digits}f}")
    return variants


def find_ungrounded_numbers(answer: str, payload: Dict[str, Any]) -> List[str]:
    """找出回答中不存在於 tool_results 的數字（小於等於 10 的整數視為一般敘述用字；大金額允許精確換算成萬／億）。"""
    source = json.dumps(payload, ensure_ascii=False)
    allowed: Set[str] = set()
    for token in _NUMBER_RE.findall(source):
        allowed |= _number_variants(token)
        allowed |= _unit_variants(token)
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


_SENTENCE_RE = re.compile(r"[^。！？；\n]*[。！？；]?")


# ============================================================
# 事實核對：數字之外，再核對「股票歸屬、均線標籤與數值、站上／跌破方向、題目假設價」
# 全部用 Python 規則比對 Tool 原始結果，不另外呼叫 Gemini；只刪有問題的句子。
# ============================================================

_USER_INPUT_KEYS = {"cost_price"}          # 這些欄位的數字來自使用者輸入，不是市場資料
_MA_ALIAS = {"週線": "MA5", "周線": "MA5", "雙週線": "MA10", "月線": "MA20", "季線": "MA60", "半年線": "MA120", "年線": "MA240"}
_MA_ALL_WORDS = ("所有均線", "全部均線", "各均線", "各條均線")
_MA_NAME = r"(?:(?<![A-Za-z])MA\s?\d{1,3}|雙週線|週線|周線|月線|季線|半年線|年線)"
_MA_LABEL = r"(?:" + _MA_NAME + r"|所有均線|全部均線|各均線|各條均線)"
# 「月線 31.2 元」「MA20（31.2）」「季線約 45」：標籤後面緊接的價格；後面接 %／日／張等單位的是距離或天數，不核對。
# 指數（加權、櫃買）動輒五位數，寫法會有千分位逗號；不吃逗號的話「46,543」會被讀成「46」，
# 事實核對就會把正確的句子當成數字錯誤刪掉。
_MA_VALUE_RE = re.compile(r"(" + _MA_NAME + r")[\s（(：:為在約於是]{0,4}(\d[\d,]*(?:\.\d+)?)(?![\d.%％日天個張億萬倍檔次週年])")
_DIRECTION_RE = re.compile(
    r"(站上|站穩|站回|突破|守住|守穩|跌破|失守|跌落|摜破)\s*((?:" + _MA_LABEL + r")(?:\s*[、與和及/／]\s*(?:" + _MA_LABEL + r"))*)")
_UP_WORDS = {"站上", "站穩", "站回", "突破", "守住", "守穩"}
# 條件、否定、未來、過去的句子不是在陳述「現在的位置」，不核對方向，避免誤刪。
_DIRECTION_SKIP_RE = re.compile(
    r"若|如果|一旦|假如|倘若|假設|需|須|必須|要|能否|是否|未|沒|不|等待|等|觀察|才|可能|恐|會|將|可望|機會|留意|注意|關注|避免|"
    r"之前|前一|先前|日前|前天|前幾|之後|以後|後續|隨後|明天|明日|後天|再|否|曾|過去|昨|試圖|嘗試|挑戰|接近|逼近|測試|回測")
_PRICE_SUBJECT_RE = re.compile(r"股價|價格|收盤|現價|盤中|K\s?棒|報價|今日|今天|目前")
_NON_PRICE_SUBJECT_RE = re.compile(r"成本|均價|買點|目標|扣抵")
_INTRADAY_WORD_RE = re.compile(r"盤中|暫時|即時|目前|現在|今日|今天|此刻|最新成交")
_ASSUME_RE = re.compile(r"成本|假設|假如|如果|若|倘若|買在|買進|買入|進場|均價|持有|持股|部位|停損|套在|套牢|攤平|帳面|損益|虧損|獲利|報酬|你|您|題目|設定")
_CLAUSE_SPLIT_RE = re.compile(r"[，,：:]")
_PAREN_RE = re.compile(r"（[^（）]*）|\([^()]*\)")
_HEADING_RE = re.compile(r"^(?:\*\*|【|#)")


def _strip_keys(value: Any, keys: Set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _strip_keys(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_strip_keys(v, keys) for v in value]
    return value


def _find_values(value: Any, key: str) -> List[Any]:
    found: List[Any] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and v is not None:
                found.append(v)
            found += _find_values(v, key)
    elif isinstance(value, list):
        for v in value:
            found += _find_values(v, key)
    return found


def _variants_of(text: str) -> Set[str]:
    variants: Set[str] = set()
    for token in set(_NUMBER_RE.findall(text)):
        variants |= _number_variants(token)
        variants |= _unit_variants(token)
    return variants


def _ma_key(label: str) -> str:
    label = re.sub(r"\s+", "", label)
    return _MA_ALIAS.get(label, label.upper())


def _numeric_values(value: Any) -> List[float]:
    numbers: List[float] = []
    if isinstance(value, bool):
        return numbers
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        for v in value.values():
            numbers += _numeric_values(v)
    elif isinstance(value, list):
        for v in value:
            numbers += _numeric_values(v)
    return numbers


class FactSheet:
    """從 Tool 原始結果整理可核對的事實：每檔股票的數字、均線數值、收盤確認與盤中的均線位置。"""

    def __init__(self, question: str, results: Sequence[tools.ToolResult], payload: Dict[str, Any]) -> None:
        self.stocks: Dict[str, str] = {}
        self.stock_numbers: Dict[str, Set[str]] = {}
        self.shared_numbers: Set[str] = set()
        self.ma_values: Dict[str, Dict[str, List[float]]] = {}
        self.closed_pos: Dict[str, Dict[str, str]] = {}
        self.live_pos: Dict[str, Dict[str, str]] = {}
        tool_results = payload.get("tool_results") or {}
        self.market = _variants_of(json.dumps(_strip_keys(tool_results, _USER_INPUT_KEYS), ensure_ascii=False, default=str))
        user_text = " ".join([question] + [str(v) for v in _find_values(tool_results, "cost_price")])
        self.user_only = _variants_of(user_text) - self.market
        for result in results:
            if result.ok and isinstance(result.data, dict):
                self._add_result(result.name, result.data)

    def _add_result(self, name: str, data: Dict[str, Any]) -> None:
        code = str(data.get("stock_code") or "")
        numbers = _variants_of(json.dumps(_strip_keys(data, _USER_INPUT_KEYS), ensure_ascii=False, default=str))
        if not code:
            self.shared_numbers |= numbers
            return
        self.stock_numbers.setdefault(code, set()).update(numbers)
        if data.get("stock_name"):
            self.stocks[code] = str(data["stock_name"])
        else:
            self.stocks.setdefault(code, "")
        values = self.ma_values.setdefault(code, {})
        closed = self.closed_pos.setdefault(code, {})
        for key, info in (data.get("moving_averages") or {}).items():
            if isinstance(info, dict):
                if info.get("value") is not None:
                    values.setdefault(key, []).append(float(info["value"]))
                if info.get("position") in ("站上", "跌破", "持平"):
                    closed[key] = info["position"]
        for key, info in (data.get("ma_deduction") or {}).items():
            values.setdefault(key, []).extend(_numeric_values(info))  # 扣抵價、明天需收在多少才上彎等
        # 型態評分卡沒有 moving_averages：用關鍵價位表補（現價下方＝站上、上方＝跌破）。
        for field_name, position in (("supports_below_close", "站上"), ("resistances_above_close", "跌破")):
            for level in data.get(field_name) or []:
                label = str((level or {}).get("label") or "")
                if re.fullmatch(r"MA\d+", label) and level.get("price") is not None:
                    values.setdefault(label, []).append(float(level["price"]))
                    closed.setdefault(label, position)
        live = (data.get("intraday_observation") or {}).get("ma_positions") or {}
        if live:
            self.live_pos.setdefault(code, {}).update({k: v for k, v in live.items() if v in ("站上", "跌破", "持平")})

    # ---------- 股票辨識 ----------
    def mentioned(self, text: str) -> List[str]:
        found = []
        for code, name in self.stocks.items():
            if (name and name in text) or re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text):
                found.append(code)
        return found

    def heading_stock(self, line: str, current: str) -> str:
        """「**友達（2409）**」「【華邦電】」這類段落標題：之後沒寫股票名的句子都算這檔。"""
        stripped = line.strip()
        if _HEADING_RE.match(stripped) or stripped.endswith(("：", ":")):
            codes = self.mentioned(stripped)
            if len(codes) == 1:
                return codes[0]
        return current

    # ---------- 單句核對 ----------
    def sentence_issues(self, sentence: str, current: str = "") -> List[str]:
        issues: List[str] = []
        codes = self.mentioned(sentence)
        subject = codes[0] if len(codes) == 1 else (current if not codes else "")
        if not subject and len(self.stocks) == 1 and not codes:
            subject = next(iter(self.stocks))
        issues += self._number_issues(sentence, codes)
        if subject:
            issues += self._ma_value_issues(sentence, subject)
            issues += self._direction_issues(sentence, subject)
        return issues

    def _number_issues(self, sentence: str, codes: List[str]) -> List[str]:
        text = sentence
        for pattern in _EXEMPT_PATTERNS:
            text = pattern.sub(" ", text)
        issues = []
        for token in _NUMBER_RE.findall(text):
            cleaned = token.replace(",", "").lstrip("+")
            try:
                value = float(cleaned)
            except ValueError:
                continue
            if value.is_integer() and abs(value) <= 10:
                continue
            forms = {cleaned, cleaned.lstrip("-")}
            if forms & self.market:
                # 句子只講一檔股票時，數字不可以只屬於另一檔股票。
                if len(codes) == 1 and len(self.stock_numbers) >= 2:
                    own = self.stock_numbers.get(codes[0], set()) | self.shared_numbers
                    if not forms & own and any(forms & nums for c, nums in self.stock_numbers.items() if c != codes[0]):
                        issues.append(f"數字 {token} 屬於另一檔股票")
                continue
            if forms & self.user_only:
                if not _ASSUME_RE.search(sentence):
                    issues.append(f"題目提供的數字 {token} 沒標明是成本／假設，不能當成實際報價")
                continue
            issues.append(f"對不上的數字 {token}")
        return issues

    def _ma_value_issues(self, sentence: str, code: str) -> List[str]:
        issues = []
        values = self.ma_values.get(code) or {}
        for label, number in _MA_VALUE_RE.findall(sentence):
            key = _ma_key(label)
            known = values.get(key)
            if not known:
                continue
            written = float(str(number).replace(",", ""))
            if not any(abs(written - v) <= max(abs(v) * 0.006, 0.011) for v in known):
                issues.append(f"{key} 數值不符（寫 {number}，資料為 {known[0]:g}）")
        return issues

    def _direction_issues(self, sentence: str, code: str) -> List[str]:
        closed = self.closed_pos.get(code) or {}
        live = self.live_pos.get(code) or {}
        if not closed:
            return []
        issues = []
        plain = _PAREN_RE.sub("", sentence)
        for clause in _CLAUSE_SPLIT_RE.split(plain):
            for match in _DIRECTION_RE.finditer(clause):
                prefix = clause[:match.start()]
                if _DIRECTION_SKIP_RE.search(clause):
                    continue
                if _NON_PRICE_SUBJECT_RE.search(prefix):
                    continue  # 成本／扣抵價與均線的比較，不是股價位置
                if re.search(_MA_LABEL, prefix) and not _PRICE_SUBJECT_RE.search(prefix):
                    continue  # 「MA5 跌破 MA20」是均線彼此交叉，不是股價位置
                claimed = "站上" if match.group(1) in _UP_WORDS else "跌破"
                labels = re.findall(_MA_LABEL, match.group(2))
                keys: List[str] = []
                for label in labels:
                    keys += [k for k in ("MA5", "MA10", "MA20", "MA60") if k in closed] if label in _MA_ALL_WORDS else [_ma_key(label)]
                intraday_ok = bool(live) and bool(_INTRADAY_WORD_RE.search(sentence))
                for key in keys:
                    actual = closed.get(key)
                    if actual not in ("站上", "跌破") or actual == claimed:
                        continue
                    if intraday_ok and live.get(key) == claimed:
                        continue
                    if live.get(key) == claimed:
                        issues.append(f"盤中{claimed} {key} 被寫成已確認（收盤為{actual}）")
                    else:
                        issues.append(f"方向不符：寫{claimed} {key}，收盤實際為{actual}")
        return issues

    def check(self, answer: str) -> List[Tuple[str, List[str]]]:
        _, removed = _scan_sentences(answer, self.sentence_issues, self.heading_stock)
        return removed


def _scan_sentences(answer: str, checker: Callable[[str, str], List[str]],
                    heading: Optional[Callable[[str, str], str]] = None) -> Tuple[str, List[Tuple[str, List[str]]]]:
    """逐行逐句核對；標題行（【…】）保留。回傳（刪減後文字, [(被刪的句子, 原因)]）。"""
    kept_lines: List[str] = []
    removed: List[Tuple[str, List[str]]] = []
    current = ""
    for line in answer.split("\n"):
        stripped = line.strip()
        if heading:
            current = heading(stripped, current)
        if not stripped or re.fullmatch(r"【[^】]+】", stripped):
            kept_lines.append(line)
            continue
        kept = []
        for sentence in _SENTENCE_RE.findall(line):
            if not sentence.strip():
                continue
            reasons = checker(sentence, current)
            if reasons:
                removed.append((sentence.strip(), reasons))
            else:
                kept.append(sentence)
        text = "".join(kept).strip()
        if text and text not in ("・", "•", "-"):
            kept_lines.append(text)
    # 內容被刪光的區塊標題一併移除，避免留下空標題。
    cleaned: List[str] = []
    for i, line in enumerate(kept_lines):
        is_heading = bool(re.fullmatch(r"【[^】]+】", line.strip()))
        next_content = next((l for l in kept_lines[i + 1:] if l.strip()), "")
        if is_heading and (not next_content or re.fullmatch(r"【[^】]+】", next_content.strip())):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip(), removed


def prune_ungrounded_sentences(answer: str, payload: Dict[str, Any], facts: Optional[FactSheet] = None) -> Tuple[str, List[str]]:
    """刪除有問題的句子：有 facts 時做完整事實核對，否則只核對數字。回傳（刪減後文字, 被刪的句子）。"""
    if facts is not None:
        text, removed = _scan_sentences(answer, facts.sentence_issues, facts.heading_stock)
    else:
        text, removed = _scan_sentences(answer, lambda sentence, _current: find_ungrounded_numbers(sentence, payload))
    return text, [sentence for sentence, _ in removed]


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


def _deduction_line(deduction: Dict[str, Any]) -> str:
    """均線扣抵合併成一行：MA5 續揚、MA20 第 2 日轉下彎…（細節數字在圖片的評分卡）。"""
    words = {"上揚": "續揚", "下彎": "續彎"}
    parts = [f"{key} {info.get('turn_text') or tools.turn_phrase(info['turn'], info.get('turn_day'))}" if info.get("turn")
             else f"{key} {words.get(info.get('direction_now'), '走平')}" for key, info in deduction.items()]
    return ("\n扣抵（收盤不變推算）：" + "、".join(parts)) if parts else ""


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
        + _deduction_line(d.get("ma_deduction") or {})
        + f"\nKD：K {_v(kd.get('K9'))}／D {_v(kd.get('D9'))}（{kd.get('signals') or '無特殊訊號'}）\n"
        f"MACD：DIF {_v(macd.get('DIF'))}／MACD {_v(macd.get('MACD'))}／OSC {_v(macd.get('OSC'))}"
        f"（{macd.get('osc_trend')}{'；' + macd.get('signals') if macd.get('signals') else ''}）\n"
        f"布林：上軌 {_v(bb.get('upper'))}／中軌 {_v(bb.get('mid'))}／下軌 {_v(bb.get('lower'))}，"
        f"{bb.get('position')}（%B {_v(bb.get('percent_b'))}）"
        + f"\n【布林觀察】{'；'.join(bb.get('signals') or ['資料不足'])}"
        + f"\n帶寬 {_v(bb.get('width_pct_of_mid'), '%')}｜相對5日前變化 {_v(bb.get('width_change_5d_pct'), '%', signed=True)}"
        + "\n※ 壓縮與橫盤依本專案規則判定；觸軌不代表反轉，壓縮不代表突破方向。"
    )


def _breadth_panel(data: Dict[str, Any]) -> Dict[str, Any]:
    """盤面結構卡片：主體列「權值股漲幅排行」（問的就是權值股）；抓不到權值股報價時才退回類股。"""
    heavy = list(data.get("heavyweights") or [])

    live = str(data.get("basis") or "") == "盤中即時"
    intraday = {"is_live": True, "time": str(data.get("time") or "")} if live else {}

    def stock_rows(items, start=1):
        return [{"rank": i, "stock_code": str(x.get("code") or ""), "stock_name": str(x.get("name") or ""),
                 "market": "", "pattern_score": None, "close": x.get("close"),
                 "change_pct": float(x.get("change_pct") or 0.0), "intraday": intraday, "quote_date": ""}
                for i, x in enumerate(items, start)]

    def group_rows(items, start=1):
        return [{"rank": i, "stock_code": "", "stock_name": str(x.get("name") or ""), "market": "",
                 "row_kind": "sector_group", "pattern_score": None,
                 "change_pct": float(x.get("change_pct") or 0.0),
                 "coverage_text": "", "ratio_text": "", "leader_text": ""}
                for i, x in enumerate(items, start)]

    parts = []
    if data.get("taiex_change_pct") is not None:
        parts.append("加權 {:+.2f}%".format(data["taiex_change_pct"]))
    if data.get("tpex_change_pct") is not None:
        parts.append("櫃買 {:+.2f}%".format(data["tpex_change_pct"]))
    if data.get("heavyweight_median_pct") is not None:
        parts.append("權值股中位 {:+.2f}%".format(data["heavyweight_median_pct"]))
    note = "｜".join(parts)
    if data.get("advance_ratio_pct") is not None:
        note += f"｜上漲類股 {data.get('sector_advancing', 0)}/{data.get('sector_total', 0)}"
    if heavy:
        rows, others = stock_rows(heavy[:3]), stock_rows(heavy[3:8], start=4)
        title = "盤面結構｜權值股"
    else:
        rows, others = group_rows(data.get("strongest") or [])[:3], group_rows(data.get("weakest") or [], start=4)[:3]
        title = "盤面結構｜類股"
    return {"sector": {"name": title, "mode": "market_momentum",
                       "comparison_date": "", "rows": rows, "others": others,
                       "coverage_note": "", "liquidity_note": note,
                       "live_time": str(data.get("time") or "")}}


def format_index_contribution(data: Dict[str, Any]) -> str:
    lines = [f"**指數貢獻點數｜{data.get('basis', '')}**"]
    if data.get("structure_summary"):
        lines.append(str(data["structure_summary"]))
    for market in data.get("markets") or []:
        points = market.get("index_points")
        head = f"{market.get('index_name', '')}"
        if points is not None:
            head += f" {points:+.2f} 點"
        coverage = market.get("market_cap_coverage_pct")
        if coverage is not None and str(market.get("basis") or "").startswith("盤中"):
            head += f"｜市值涵蓋 {coverage:.1f}%"
        lines.append(f"**{head}**")
        pos_share = market.get("top5_positive_share_pct")
        if pos_share is not None:
            lines.append(f"拉升集中度：TOP5 占已涵蓋正貢獻 {pos_share:.1f}%｜{market.get('concentration', '')}")
        for label, key in (("拉升", "top"), ("拖累", "bottom")):
            items = list(market.get(key) or [])
            if items:
                lines.append(label + "：" + "、".join(
                    f"{x['stock_name']}({x['stock_code']}) {x['points']:+.2f}點 / 權重{x.get('weight_pct', 0):.2f}% / {x.get('change_pct', 0):+.2f}%"
                    for x in items[:5]))
            else:
                lines.append(label + "：目前沒有可列出的成分股")
    lines.append("※ 盤中為官方即時報價估算；收盤後改用交易所全市場收盤快照完整計算。")
    return chr(10).join(lines)


def format_market_breadth(data: Dict[str, Any]) -> str:
    """AI 失敗時的規則式輸出；欄位與工具回傳一致。"""
    stamp = f" {data.get('time')}" if data.get("time") else ""
    lines = [f"**盤面結構｜{data.get('basis', '')}{stamp}**"]
    index_bits = []
    if data.get("taiex_change_pct") is not None:
        index_bits.append(f"加權指數 {data['taiex_change_pct']:+.2f}%")
    if data.get("tpex_change_pct") is not None:
        index_bits.append(f"櫃買指數 {data['tpex_change_pct']:+.2f}%")
    if index_bits:
        lines.append("｜".join(index_bits))
    if data.get("heavyweight_median_pct") is not None:
        lines.append(f"權值股 {data.get('heavyweight_sample', 0)} 檔中位 {data['heavyweight_median_pct']:+.2f}%"
                     + (f"｜對照：{data.get('others_label', '')} {data['others_change_pct']:+.2f}%"
                        if data.get("others_change_pct") is not None else ""))
        top = data.get("heavyweight_top") or []
        if top:
            lines.append("權值股表現：" + "、".join(f"{x['name']} {x['change_pct']:+.2f}%" for x in top))
    if data.get("sector_total"):
        lines.append(f"類股 {data['sector_total']} 個｜上漲 {data.get('sector_advancing', 0)}"
                     f"（上漲比率 {data.get('advance_ratio_pct', 0)}%）"
                     + (f"｜中位 {data['sector_median_pct']:+.2f}%" if data.get("sector_median_pct") is not None else ""))
    if data.get("strongest"):
        lines.append("最強類股：" + "、".join(f"{x['name']} {x['change_pct']:+.2f}%" for x in data["strongest"]))
    if data.get("weakest"):
        lines.append("最弱類股：" + "、".join(f"{x['name']} {x['change_pct']:+.2f}%" for x in data["weakest"]))
    lines.append(f"判讀：{data.get('verdict', '')}")
    lines.append("※ 以上為當下盤面結構描述，不代表未來表現。")
    return chr(10).join(lines)


def format_futures(data: Dict[str, Any]) -> str:
    rows = data.get("investors") or []
    lines = [f"**三大法人台指期未平倉｜{data.get('data_date', '')}**"]
    for row in rows:
        change = row.get("change_vs_prev")
        delta = ""
        if change is not None:
            delta = f"（較前一日{'增加' if change > 0 else '減少'} {abs(int(change)):,} 口）" if change else "（與前一日相同）"
        lines.append(f"・{row['investor']}：{row['net_text']} 口{delta}")
    lines.append("※ 未平倉含現貨避險部位，僅供參考，不作多空判斷。")
    return chr(10).join(lines)


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
        date = f"{item['date']}｜" if item.get("date") else ""
        lines.append(f"• {date}{item.get('title')}（{item.get('source')}）")
        # 有原文段落就列前 120 字；RSS 摘要常常只是標題＋媒體名，跟標題重複就不列。
        detail = item.get("content") if "原文" in item.get("content_source", "") else item.get("summary")
        detail = re.sub(r"\s+", " ", str(detail or "")).strip()
        title = str(item.get("title") or "")
        if detail and not detail.startswith(title[:20]):
            lines.append(f"　{tools._truncate_sentences(detail, 160)}")
    return "\n".join(lines)


def format_sheet_query(d: Dict[str, Any]) -> str:
    label = tools.SHEET_REGISTRY.get(d.get("worksheet"), "查詢結果")
    lines = [f"📌 {label}：符合 {d.get('matched_rows')} 筆，顯示 {d.get('returned_rows')} 筆"]
    for row in (d.get("rows") or [])[:8]:
        lines.append("• " + "｜".join(f"{v}" for v in list(row.values())[:6] if v))
    return "\n".join(lines)


FORMATTERS = {
    "get_stock_overview": format_overview,
    "get_technical_analysis": format_technical,
    "get_futures_positions": format_futures,
    "get_market_breadth": format_market_breadth,
    "get_index_contribution": format_index_contribution,
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


def format_top_warrant(d: Dict[str, Any]) -> str:
    if not d.get("available"):
        return f"【權證淨買超排行】{d.get('reason', '目前沒有取得足夠資料')}"
    lines = [f"【{d.get('source') or '權證淨買超排行'}】{d.get('period')}（統計至 {d.get('stat_date')}）"]
    for row in d.get("stocks") or []:
        branches = "；".join(b.get("detail", "") for b in row.get("top_branches") or [])
        lines.append(
            f"{row['rank']}. {row.get('stock_name', '')}（{row['stock_code']}）淨買超 {row.get('net_buy_cost_text')}"
            f"｜分點 {_v(row.get('branch_count'))} 家｜事件 {row.get('events') or '-'}｜權證 {_v(row.get('warrant_count'))} 檔"
            + (f"｜估計報酬 {row['unrealized_return_text']}" if row.get("unrealized_return_text") else "")
        )
        if branches:
            lines.append(f"　{branches}")
    lines.append("※ 統計範圍為追蹤的分點，不是全市場；估計報酬為未實現估值。")
    return "\n".join(lines)


def format_pattern_scorecard(d: Dict[str, Any]) -> str:
    """評分細節已畫在圖片的型態評分卡；文字只留一行總結，避免重複。"""
    return (
        f"【型態評分】型態分數 {_v(d.get('pattern_score'))} / 100（{d.get('grade')}）｜"
        + "｜".join(f"{c['label']} {_v(c['value'])} / {c['max']}" for c in d.get("components") or [])
        + "\n※ 只評技術結構，不含籌碼，不是買賣建議。"
    )


FORMATTERS.update({
    "get_pattern_scorecard": format_pattern_scorecard,
    "get_top_warrant_buy_stocks": format_top_warrant,
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
            intraday = d.get("intraday") or {}
            if intraday.get("is_live"):
                add(f"股價為 {intraday['date']} {intraday['time']} 盤中即時報價（尚未收盤）")
            elif intraday:
                add(f"股價為 {intraday['date']} 今日收盤")
            else:
                add(f"股價截至 {d['data_date']}（日K收盤）")
        elif r.name in ("get_warrant_branch", "get_high_winrate_branches_buying") and d.get("period_start"):
            add(f"權證分點 {d['period_start']}～{d['period_end']}（{d.get('actual_trading_days')} 個交易日）")
        elif r.name == "get_branch_performance" and d.get("found"):
            add(f"勝率統計（更新 {d.get('sheet_updated_at') or '時間未知'}）")
        elif r.name in ("get_branch_recent_trades", "get_branch_winrate_rank") and (d.get("period") or d.get("snapshot_date")):
            add(f"近10日分點統計 {d.get('period') or d.get('snapshot_date')}")
        elif r.name == "get_branch_stock_history" and d.get("found"):
            add(f"ABCDE 事件（更新 {d.get('sheet_updated_at') or '時間未知'}）")
        elif r.name == "get_recent_news" and d.get("available"):
            add("新聞為近期報導整理")
        elif r.name in ("get_sheet_stock_chips", "get_branch_stock_position") and d.get("data_latest_event_date"):
            add(f"追蹤分點 A～E 事件截至 {d['data_latest_event_date']}")
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
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_source: str = "none"

    def record_gemini(self, result: GeminiResult) -> None:
        self.gemini_calls += 1
        self.gemini_latency += result.latency
        self.input_tokens += int(result.input_tokens or 0)
        self.output_tokens += int(result.output_tokens or 0)
        self.total_tokens += int(result.total_tokens or 0)
        if result.token_source and result.token_source != "none":
            self.token_source = result.token_source


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
    context_note: str = ""
    as_text: bool = False          # True＝用純文字訊息回覆（草稿要能直接複製，不能只給圖片）
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_source: str = "none"
    api_usage: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# 短期追問記憶（依 伺服器＋頻道＋使用者 隔離，只存股票／成本／分點，不存對話原文）
# ============================================================

MEMORY_MINUTES = tools._env_int("DISCORD_AI_MEMORY_MINUTES", 30)
# 週精選草稿編輯 session 比一般追問長很多：管理員常常改一改、去看盤、再回來改。
WEEKLY_DRAFT_MINUTES = max(10, tools._env_int("DISCORD_AI_WEEKLY_DRAFT_MINUTES", 120))
MEMORY_MAX_ENTRIES = tools._env_int("DISCORD_AI_MEMORY_MAX_ENTRIES", 5000)
# 使用者把「/ace 族群資金流向」整串打進 /ask 的輸入框時，前綴不能被當成族群名稱。
_SLASH_PREFIX_RE = re.compile(r"^\s*/(ask|ace)[:：,，]?\s*", re.IGNORECASE)

# /ask 的權證 K 線標註版型：event＝編號＋分點明細表（預設），flow＝分點配色圖例（週精選用）。
# 「是不是只有權值股在動」這類盤面結構問題。
# 句子同時出現「大盤層級的對象」與「廣度／貢獻的問法」時，一律走盤面結構，不進族群解析。
_MARKET_SCOPE_RE = re.compile(r"大盤|加權|櫃買|指數|盤面|盤感|權值|市場")
_BREADTH_RE = re.compile(r"盤感|盤面|市場廣度|廣度|權值股|權值|只有大型股|大盤漲.{0,6}個股|個股沒跟上|普漲|齊漲|拉指數|撐盤|貢獻|拉抬|誰在拉|誰拉|誰讓大盤|加.{0,4}點|扣.{0,4}點|拉升|拖累|漲的都是|指數失真|多數個股|中小型股|內資|盤勢結構")
ASK_MARK_MODE = (os.getenv("DISCORD_AI_ASK_MARK_MODE", "event").strip().lower() or "event")
INTENT_FALLBACK_ENABLE = tools._env_int("DISCORD_AI_INTENT_FALLBACK", 1)
MEMORY_RESET_WORDS = ("重新開始", "清除記憶", "換個話題", "忘記上一題")
_FOLLOWUP_HINT_RE = re.compile(r"它|他|這檔|那檔|這支|那支|該股|這家|那家|呢|同一檔")
# 打招呼、閒聊這類不該接上一題的句子。
_SMALLTALK_RE = re.compile(r"^(你好|哈囉|hi|hello|在嗎|嗨|謝謝|感謝|早安|午安|晚安|測試)")
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十1-9])\s*名?|冠軍|榜首|龍頭|亞軍|季軍")
_ORDINAL_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SECTOR_ROWS_TTL = 1800
_COMPARE_RE = re.compile(r"比較|相比|對比|比呢|跟.{1,8}比|和.{1,8}比|與.{1,8}比")


@dataclass
class MemoryEntry:
    stocks: List[Tuple[str, str]]
    cost_price: Optional[float]
    branches: List[str]
    updated_at: float
    sector: Optional[Dict[str, str]] = None      # 上一題問的族群，供「那誰型態最好」接續


class ConversationMemory:
    """同一位使用者在同一頻道的最近股票與成本；30 分鐘沒追問就忘記，最多保留 5,000 筆（最舊的先刪）。"""

    def __init__(self, minutes: int = MEMORY_MINUTES, max_entries: int = MEMORY_MAX_ENTRIES) -> None:
        self.ttl = max(1, minutes) * 60
        self.max_entries = max(100, max_entries)
        self._data: "OrderedDict[str, MemoryEntry]" = OrderedDict()
        self._sector_rows: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
        self._lock = threading.Lock()

    def remember_sector_rows(self, industry: str, rows: List[Tuple[str, str]]) -> None:
        """記住某族群排行的前幾名，讓「第一名的壓力在哪」接得起來（只存代號與名稱）。"""
        if not industry or not rows:
            return
        with self._lock:
            self._sector_rows[str(industry)] = (time.time(), list(rows)[:5])
            for key in [k for k, (stamp, _) in self._sector_rows.items() if time.time() - stamp > _SECTOR_ROWS_TTL]:
                self._sector_rows.pop(key, None)

    def sector_rows(self, industry: str) -> List[Tuple[str, str]]:
        with self._lock:
            stamp, rows = self._sector_rows.get(str(industry), (0.0, []))
        return list(rows) if stamp and time.time() - stamp <= _SECTOR_ROWS_TTL else []

    def _ordinal_stocks(self, text: str, entry: "MemoryEntry") -> List[Tuple[str, str]]:
        """把「第一名」「跟第二名比呢」換成上一題族群排行的個股（最多 2 檔）。"""
        rows = self.sector_rows(str((entry.sector or {}).get("industry") or ""))
        if not rows:
            return []
        picks: List[Tuple[str, str]] = []
        for match in _ORDINAL_RE.finditer(text):
            token, digit = match.group(0), match.group(1)
            if digit:
                index = int(digit) if digit.isdigit() else _ORDINAL_WORDS.get(digit, 0)
            else:
                index = {"冠軍": 1, "榜首": 1, "龍頭": 1, "亞軍": 2, "季軍": 3}.get(token, 0)
            if 1 <= index <= len(rows) and rows[index - 1] not in picks:
                picks.append(rows[index - 1])
        return picks[:2]

    def get(self, key: str) -> Optional[MemoryEntry]:
        if not key:
            return None
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.time() - entry.updated_at > self.ttl:
                self._data.pop(key, None)
                return None
            return entry

    def clear(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def stats(self) -> Dict[str, int]:
        now = time.time()
        with self._lock:
            expired = [key for key, entry in self._data.items() if now - entry.updated_at > self.ttl]
            for key in expired:
                self._data.pop(key, None)
            return {"entries": len(self._data), "max_entries": self.max_entries, "ttl_minutes": int(self.ttl / 60)}

    def update(self, key: str, parsed: ParsedQuestion) -> None:
        if parsed.sector is not None:
            # 族群問題：記住這個族群，後續「那誰型態最好」才接得起來；個股記憶則清掉。
            sector = parsed.sector
            if not key or sector.get("mode") in ("catalog", "unsupported", "belongs") or not sector.get("industry"):
                self.clear(key)
                return
            with self._lock:
                self._data[key] = MemoryEntry([], None, [], time.time(),
                                              {"industry": sector["industry"], "name": sector.get("name", ""),
                                               "mode": sector.get("mode", "technical")})
                self._data.move_to_end(key)
                while len(self._data) > self.max_entries:
                    self._data.popitem(last=False)
            return
        if not key or not parsed.stocks:
            return
        previous = self.get(key)
        cost = parsed.cost_price
        if cost is None and previous and previous.cost_price and previous.stocks[:1] == parsed.stocks[:1]:
            cost = previous.cost_price  # 同一檔股票沿用之前說過的成本
        with self._lock:
            self._data[key] = MemoryEntry(list(parsed.stocks[:2]), cost, list(parsed.branches[:1]), time.time())
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def resolve(self, key: str, parsed: ParsedQuestion) -> str:
        """問題沒寫股票、但看得出是追問時，補上上一題的股票（就地修改 parsed），回傳顯示給使用者的說明。"""
        if parsed.sector is not None:
            return ""
        entry = self.get(key)
        if entry is None:
            return ""
        text = parsed.original
        if entry.sector and not parsed.stocks and not parsed.branches:
            # 「第一名的壓力在哪」「跟第二名比呢」：把名次換成上一題排行的個股。
            picked = self._ordinal_stocks(text, entry)
            if picked:
                parsed.stocks = picked
                names = "、".join(f"{n or c}（{c}）" for c, n in picked)
                return f"延續上一題的族群排行：{names}"
            if sector_analysis.is_sector_follow_up(text):
                follow_up = sector_analysis.follow_up_request(text, entry.sector)
                if follow_up:
                    parsed.sector = follow_up
                    parsed.intents = set(parsed.intents) | {"sector"}
                    return f"延續上一題的族群：{entry.sector.get('name', '')}"
            return ""
        if not entry.stocks:
            return ""
        names = "、".join(f"{name or code}（{code}）" for code, name in entry.stocks)
        if parsed.stocks:
            if len(parsed.stocks) == 1 and _COMPARE_RE.search(text) and parsed.stocks[0][0] != entry.stocks[0][0]:
                parsed.stocks = [entry.stocks[0], parsed.stocks[0]]
                return f"延續上一題：{entry.stocks[0][1] or entry.stocks[0][0]} 與 {parsed.stocks[1][1] or parsed.stocks[1][0]} 比較"
            if (parsed.cost_price is None and entry.cost_price and parsed.stocks[0][0] == entry.stocks[0][0]
                    and ("cost" in parsed.intents or "成本" in text)):
                parsed.cost_price = entry.cost_price
                parsed.intents.add("cost")
                return f"沿用上一題的成本 {entry.cost_price:g}"
            return ""
        if is_top_warrant_question(parsed) or ("win_rate" in parsed.intents and parsed.branches):
            return ""  # 排行、分點勝率這類問題本來就不針對單一股票
        if parsed.branches and "position" not in parsed.intents and not _FOLLOWUP_HINT_RE.search(text):
            return ""  # 例如「永豐金內湖最近在買什麼」：問的是分點本身
        # 預設沿用上一題的股票；只有打招呼、或句子裡出現「新主題」（別的族群／專有名詞）時才不接。
        if _SMALLTALK_RE.match(text.strip()):
            return ""
        if sector_match.has_new_topic(text) and not _FOLLOWUP_HINT_RE.search(text):
            return ""
        parsed.stocks = list(entry.stocks)
        if parsed.cost_price is None and entry.cost_price and ("cost" in parsed.intents or "成本" in text):
            parsed.cost_price = entry.cost_price
        return f"延續上一題：{names}"


# ============================================================
# 排隊與並行（取代原本「整個流程一把鎖」）
# ============================================================

ANSWER_CONCURRENCY = max(1, tools._env_int("DISCORD_AI_ANSWER_CONCURRENCY", 3))
ANSWER_QUEUE_LIMIT = max(1, tools._env_int("DISCORD_AI_QUEUE_LIMIT", 20))
QUEUE_FULL_MESSAGE = "目前使用人數較多，排隊已滿，請過一兩分鐘再問一次。"


class AceQueryEngine:
    """與 Discord 無關的問答核心；CLI 與 Bot 共用。"""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.log = DebugLog(config.debug)
        self.parser = QuestionParser()
        self.gateway = GeminiGateway(self.log)
        self.router = QueryRouter(self.gateway, config, self.log)
        # 同時處理多題時資料工具也要夠用（每題約 4～6 個工具）。
        self.executor = ThreadPoolExecutor(max_workers=max(4, tools._env_int("DISCORD_AI_TOOL_WORKERS", 10)), thread_name_prefix="ace-tool")
        self._answer_cache = tools.TTLCache("discord_ai_answer")
        self.memory = ConversationMemory()
        self._slots = threading.BoundedSemaphore(ANSWER_CONCURRENCY)   # 一般問答最多同時 N 題
        self._weekly_lock = threading.Lock()                           # 本週精選獨立排隊、一次一個
        self._weekly_draft_lock = threading.Lock()
        self._weekly_drafts: Dict[str, Dict[str, Any]] = {}            # context_key → 正在編輯的週精選草稿
        self._queue_lock = threading.Lock()
        self._inflight: Dict[str, Future] = {}                          # 同一個問題同時進來共用一次計算
        self._pending = 0                                               # 排隊中＋處理中的題數
        self._request_local = threading.local()                          # 單次問答 request_id，供 API 計量

    def queue_size(self) -> int:
        with self._queue_lock:
            return self._pending

    def _answer_impl(self, question: str, context_key: str = "", on_queue: Optional[Callable[[int], None]] = None,
                     is_admin: bool = False, admin_mode: bool = False) -> AnswerResult:
        """context_key＝伺服器:頻道:使用者，用來記住追問；on_queue(前面還有幾題) 在需要排隊時呼叫一次。

        admin_mode=True 代表這題來自管理員專用指令（本週精選、草稿、維護）；
        一般 /ask 永遠不會進到那些流程，避免草稿編輯把正常問題吃掉。
        """
        started = time.perf_counter()
        # 有人會把「/ace 族群資金流向」整串貼進輸入框；前綴要拿掉，否則會被當成族群名稱去查。
        prefix = _SLASH_PREFIX_RE.match(question)
        if prefix:
            question = _SLASH_PREFIX_RE.sub("", question, count=1)
            if prefix.group(1).lower() == str(self.config.admin_command_name).lower() and is_admin:
                admin_mode = True
        compact = re.sub(r"\s+", "", question)
        if any(word in compact for word in MEMORY_RESET_WORDS):
            self.memory.clear(context_key)
            self._clear_draft_session(context_key)
            return AnswerResult(text="好的，已清除上一題的內容，接下來請直接輸入想問的股票。", route="memory_reset", gemini_calls=0, elapsed=0.0)
        # MoneyDJ 只允許管理員明確要求備援圖片；一般問答／週精選不會自動碰 MoneyDJ。
        if weekly_pick.is_admin_moneydj_image_question(question):
            if not is_admin:
                return AnswerResult(text="MoneyDJ 備援圖片僅限管理員使用。", route="admin_moneydj_denied", gemini_calls=0, elapsed=time.perf_counter()-started)
            return self._answer_admin_moneydj_image(question, started)

        if not admin_mode:
            # /ask 只做一般問答；精選相關的字眼直接導向管理員指令，不進草稿流程。
            if is_weekly_pick_question(question) or weekly_pick.is_weekly_admin_feature_question(question):
                hint = f"本週精選與草稿請改用 /{self.config.admin_command_name}（管理員專用）。"
                return AnswerResult(text=hint if is_admin else "「本週精選」目前只開放管理員使用；一般個股、族群、權證分點問題可以照常詢問。",
                                    route="weekly_pick_hint", gemini_calls=0, elapsed=time.perf_counter() - started)
            return self._answer_general(question, context_key, on_queue, started, compact)
        admin_reply = self._answer_admin_command(question, started, context_key)
        if admin_reply is not None:
            return admin_reply
        # 管理員自己貼文字：直接當成目前草稿，不經過 AI，也不做數字核對（文字是人寫的）。
        if weekly_pick.is_manual_draft_question(question):
            return self._answer_weekly_manual_draft(question, context_key, started)
        # 週精選草稿是獨立的編輯 session，只有管理員指令會進到這裡。
        if weekly_pick.is_weekly_draft_question(question):
            with self._weekly_lock:
                return self._answer_weekly_draft(question, context_key, started)
        draft_session = self._load_draft_session(context_key)
        if draft_session and time.time() - float(draft_session.get("updated_at", 0) or 0) > WEEKLY_DRAFT_MINUTES * 60:
            self._clear_draft_session(context_key)
            draft_session = {}
        if draft_session and weekly_pick.is_weekly_image_question(question):
            return self._answer_weekly_article_image(context_key, started)
        if draft_session and re.sub(r"\s+", "", question) in ("還原上一版", "回到上一版", "復原上一版", "undo"):
            previous = str(draft_session.get("previous_draft") or "")
            if not previous:
                return AnswerResult(text="沒有可還原的上一版草稿。", route="weekly_draft_revision", gemini_calls=0, elapsed=time.perf_counter()-started)
            draft_session.update(draft=previous, previous_draft="", updated_at=time.time())
            self._save_draft_session(context_key, draft_session)
            return AnswerResult(text=previous + "\n\n※ 已還原上一版。", route="weekly_draft_revision", gemini_calls=0,
                                elapsed=time.perf_counter()-started, cacheable=False)
        if draft_session and weekly_pick.is_weekly_session_followup(question, draft_session.get("stock_code", "")):
            return self._answer_weekly_revision(question, context_key, started)
        if is_weekly_pick_question(question):
            hit, cached = self._answer_cache.get(compact)
            if hit:
                return replace(cached, route="answer_cache", gemini_calls=0, elapsed=time.perf_counter() - started, cache_hit=True)
            if self._weekly_lock.locked() and on_queue:
                on_queue(1)
            with self._weekly_lock:
                return self._answer_weekly_pick(question, started)
        # 管理員指令裡的一般問題（例如先看排名再問個股）仍然走一般問答。
        return self._answer_general(question, context_key, on_queue, started, compact)

    def _answer_general(self, question: str, context_key: str, on_queue: Optional[Callable[[int], None]],
                        started: float, compact: str) -> AnswerResult:
        """一般問答：解析 → 追問記憶 → 快取 → 排隊 → 計算。"""
        # 先判斷是不是族群雷達（「哪些族群正在轉強」），不是才解析族群名稱，
        # 否則「正在轉強」會被當成族群名稱去查。
        radar = sector_radar.detect_intent(compact)
        if radar:
            return self._answer_radar(radar["direction"], started, route="rule_radar")
        try:
            parsed = self.parser.parse(question)
        except tools.ToolDataError as exc:
            self.log(f"問題解析失敗：{exc}")
            return AnswerResult(text="目前無法解析問題所需的基本資料，請稍後再試。", route="error", gemini_calls=0, elapsed=time.perf_counter() - started)
        note = self.memory.resolve(context_key, parsed)
        if note:
            self.log(f"追問記憶：{note}")
        # 快取鍵值用「補完股票之後」的問題，避免 A 使用者的「那它的壓力在哪」拿到 B 使用者的答案。
        key = "|".join([compact, ",".join(c for c, _ in parsed.stocks), str(parsed.cost_price or ""), ",".join(parsed.branches)])
        hit, cached = self._answer_cache.get(key)
        if hit:
            self.log(f"回答快取命中：{question}")
            self.memory.update(context_key, parsed)
            return replace(cached, route="answer_cache", gemini_calls=0, elapsed=time.perf_counter() - started,
                           cache_hit=True, context_note=note)

        with self._queue_lock:
            shared = self._inflight.get(key)
            if shared is None:
                if self._pending >= ANSWER_CONCURRENCY + ANSWER_QUEUE_LIMIT:
                    return AnswerResult(text=QUEUE_FULL_MESSAGE, route="queue_full", gemini_calls=0, elapsed=0.0)
                ahead = max(0, self._pending - ANSWER_CONCURRENCY + 1)
                self._pending += 1
                future: Future = Future()
                self._inflight[key] = future
        if shared is not None:
            self.log(f"相同問題正在計算，共用結果：{question}")
            result = shared.result(timeout=self.config.tool_timeout_seconds + 120)
            self.memory.update(context_key, parsed)
            return replace(result, context_note=note)

        try:
            if ahead and on_queue:
                on_queue(ahead)
            with self._slots:
                effective = f"{question}（{note}）" if note else question
                result = self._answer_uncached(effective, started, parsed)
            future.set_result(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._queue_lock:
                self._pending -= 1
                self._inflight.pop(key, None)
        # 只快取「資料全部成功、且 Gemini 沒有失敗」的回答，避免限流或逾時訊息被重複送出。
        if result.cacheable:
            # 盤中股價每分鐘在變，回答快取跟著縮短，避免同一題拿到幾分鐘前的價格。
            seconds = self.config.answer_cache_seconds
            if tools.INTRADAY_ENABLE and tools.intraday_session_now():
                seconds = min(seconds, tools.TTL_INTRADAY_SECONDS)
            self._answer_cache.set(key, result, seconds)
        self.memory.update(context_key, parsed)
        return replace(result, context_note=note)

    # 草稿相關與維護指令回純文字：管理員要能直接複製、貼回去，也方便自己留檔。
    TEXT_ROUTES = {"weekly_draft", "weekly_draft_revision", "weekly_manual_draft", "weekly_draft_show",
                   "admin_help", "admin_status", "admin_market_sync", "admin_roster_build", "weekly_pick_hint",
                   "admin_usage"}

    def answer(self, question: str, context_key: str = "", on_queue: Optional[Callable[[int], None]] = None,
               is_admin: bool = False, admin_mode: bool = False) -> AnswerResult:
        """公開入口：替每一題建立 request_id，蒐集這一題實際 API 使用量。"""
        request_id = uuid.uuid4().hex[:10]
        self._request_local.request_id = request_id
        try:
            with tools.api_request_scope(request_id):
                result = self._answer_impl(question, context_key, on_queue, is_admin=is_admin, admin_mode=admin_mode)
            usage = tools.request_api_usage(request_id, clear=True)
            # 每題的用量寫進本地 SQLite（保留 30 天），「/ace 用量」與負載評估都讀這張表。
            try:
                local_market_cache.log_usage(result.route, result.gemini_calls, result.input_tokens,
                                             result.output_tokens, result.token_source, usage,
                                             result.elapsed, result.cache_hit)
            except Exception:
                pass
            print(f"📊 usage｜route={result.route}｜cache={'hit' if result.cache_hit else 'miss'}｜"
                  f"{result.elapsed:.1f}s｜Gemini {result.gemini_calls} 次"
                  f"（in {result.input_tokens:,} / out {result.output_tokens:,} / {result.token_source}）｜"
                  f"API {usage}", flush=True)
            return replace(result, request_id=request_id, api_usage=usage,
                           as_text=result.as_text or result.route in self.TEXT_ROUTES)
        finally:
            self._request_local.request_id = ""

    def _answer_weekly_pick(self, question: str, started: float) -> AnswerResult:
        """本週精選排名：Python 公平計算 Top10，排名階段不呼叫 Gemini。"""
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
            # 排名階段只顯示 Top10，不產十張 K 線；選定個股後再生成週精選文字／圖片。
            panels = []
            if answer.cards:
                weekly = {"cards": answer.cards, "overview": answer.overview, "meta": answer.meta, "notice": answer.notice}
        except tools.ToolDataError as exc:
            self.log(f"本週精選無法計算：{exc}")
            detail = tools._public_detail(exc)
            text, cache_hit = "本週精選目前無法計算" + (f"：{detail}" if detail else "，資料暫時無法取得，請稍後再試。"), False
        except Exception as exc:  # 計算流程任何例外都不可讓 Bot 中斷
            print(f"❌ 本週精選計算失敗：{type(exc).__name__}: {exc}", flush=True)
            text, cache_hit = "本週精選計算時發生錯誤，請稍後再試（詳細原因已記錄在 console）。", False
        elapsed = time.perf_counter() - started
        self.log(f"本週精選完成｜Gemini 呼叫 {stats.gemini_calls} 次｜快取={cache_hit}｜總耗時 {elapsed:.1f}s")
        return AnswerResult(
            text=text, route="weekly_pick", gemini_calls=stats.gemini_calls, elapsed=elapsed, cache_hit=cache_hit,
            panels=panels, layout="weekly_pick" if weekly else "text", weekly=weekly,
            input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
            total_tokens=stats.total_tokens, token_source=stats.token_source,
        )

    def _answer_radar(self, direction: str, started: float, route: str) -> AnswerResult:
        """族群雷達（新 L1：Δ30m＋分位數門檻）；/ace 與 /ask 共用。"""
        result = sector_radar.answer(direction)
        panels = result.get("panels") or []
        return AnswerResult(text=result["text"], route=route, gemini_calls=0,
                            elapsed=time.perf_counter()-started, cacheable=False, panels=panels,
                            as_text=not panels)

    def _answer_admin_command(self, question: str, started: float, context_key: str = "") -> Optional[AnswerResult]:
        """管理員維護指令；找不到對應指令時回 None（交給後面的精選／草稿流程）。"""
        return self._admin_command(question, started, context_key)

    def _admin_command(self, question: str, started: float, context_key: str = "") -> Optional[AnswerResult]:
        """管理員維護指令：更新市場底庫／更新族群名冊／系統狀態。找不到對應指令時回 None。"""
        compact = re.sub(r"\s+", "", question)
        if compact in ("說明", "help", "HELP", "指令", "使用說明"):
            return AnswerResult(text=ADMIN_HELP_MESSAGE, route="admin_help", gemini_calls=0,
                                elapsed=time.perf_counter()-started, cacheable=False)
        if compact in ("目前草稿", "現在草稿", "看草稿"):
            session = self._load_draft_session(context_key)
            text = str((session or {}).get("draft") or "")
            return AnswerResult(text=text or "目前沒有編輯中的草稿。", route="weekly_draft_show",
                                gemini_calls=0, elapsed=time.perf_counter()-started, cacheable=False)
        if compact in ("系統狀態", "狀態", "底庫狀態", "資料狀態"):
            info = market_scan.status()
            lines = [
                "**系統狀態**",
                f"族群名冊：{info['roster_groups']} 類｜建立於 {info['roster_built_at'] or '尚未建立'}",
                f"日K底庫：{info['bars_stocks']:,} 檔 × {info['bars_days']} 日｜最新 {info['last_day'] or '-'}",
                f"型態分數：{info['scored_stocks']:,} 檔｜{(info['score_scan'] or {}).get('at', '尚未計算')}",
                f"永久磁碟：{'已掛載 /data' if info['persistent'] else '未掛載（重新部署會清空）'}",
            ]
            return AnswerResult(text="\n".join(lines), route="admin_status", gemini_calls=0,
                                elapsed=time.perf_counter()-started, cacheable=False)
        if compact in ("更新市場底庫", "重建市場底庫", "更新底庫"):
            def job() -> None:
                result = market_data.sync(log=self.log)
                self.log(f"市場底庫更新完成：{result}")
                market_scan.score_pending(log=self.log)
            threading.Thread(target=job, name="ace-market-sync", daemon=True).start()
            return AnswerResult(text="已開始在背景更新全市場日K底庫（每個交易日 2 個請求）。完成後可用「系統狀態」查看。",
                                route="admin_market_sync", gemini_calls=0, elapsed=time.perf_counter()-started, cacheable=False)
        if compact in ("用量", "使用量", "今日用量", "usage"):
            info = local_market_cache.usage_summary()
            api_text = "、".join(f"{k} {v}" for k, v in (info.get("api_counts") or {}).items()) or "-"
            text = (f"**用量｜{info['day']}**" + chr(10) +
                    f"題數 {info['questions']}｜快取命中 {info['cache_hits']}（{info['cache_hit_rate']}%）" + chr(10) +
                    f"Gemini {info['gemini_calls']} 次｜tokens in {info['input_tokens']:,} / out {info['output_tokens']:,}" + chr(10) +
                    f"API：{api_text}" + chr(10) +
                    f"平均耗時 {info['avg_elapsed']}s｜最慢 {info['slowest']}s｜尖峰 {info.get('busiest_hour') or '-'}")
            return AnswerResult(text=text, route="admin_usage", gemini_calls=0,
                                elapsed=time.perf_counter()-started, cacheable=False)
        radar = sector_radar.detect_intent(compact)
        if radar:
            return self._answer_radar(radar["direction"], started, route="admin_radar")
        if compact in ("更新族群名冊", "重建族群名冊", "更新名冊"):
            def job() -> None:
                try:
                    sector_roster.build(log=self.log)
                except Exception as exc:
                    self.log(f"族群名冊建立失敗：{type(exc).__name__}: {exc}")
            threading.Thread(target=job, name="ace-roster-build", daemon=True).start()
            return AnswerResult(text="已開始在背景重建族群名冊（掃描全市場約 10～20 分鐘）。完成後可用「系統狀態」查看。",
                                route="admin_roster_build", gemini_calls=0, elapsed=time.perf_counter()-started, cacheable=False)
        return None

    def _answer_weekly_draft(self, question: str, context_key: str, started: float) -> AnswerResult:
        """管理員：從 Top10 選一檔，先產生可人工修改的週精選純文字。"""
        stats = AnswerStats()
        code = weekly_pick.extract_stock_code(question)
        if not code:
            return AnswerResult(text="請指定股票代號，例如：3034 幫我生成週精選文字。", route="weekly_draft", gemini_calls=0, elapsed=time.perf_counter()-started)

        def generate(prompt: str, schema: Optional[Dict[str, Any]] = None) -> GeminiResult:
            result = self.gateway.generate(prompt, purpose="weekly_draft", schema=schema, temperature=0.35)
            stats.record_gemini(result)
            return result

        data = weekly_pick.generate_weekly_draft(
            code, generate=generate, find_ungrounded=find_ungrounded_numbers, log=self.log
        )
        if not data.get("ok"):
            return AnswerResult(text=str(data.get("reason") or "週精選文字目前無法生成。"), route="weekly_draft",
                                gemini_calls=stats.gemini_calls, elapsed=time.perf_counter()-started, cacheable=False)
        session = {
            "stock_code": data["stock_code"], "stock_name": data.get("stock_name", ""),
            "draft": data["draft"], "candidate": data["candidate"], "facts": data["facts"],
            "mark_branches": data.get("mark_branches") or [],
            "admin_notes": [], "updated_at": time.time(),
        }
        self._save_draft_session(context_key, session)
        text = data["draft"] + "\n\n※ 這是草稿（純文字，可直接複製保存）。你可以直接說「權證部分短一點／大量區再強調／不要寫某段」；確認後再說「這版確認，生成圖片」。"
        return AnswerResult(text=text, route="weekly_draft", gemini_calls=stats.gemini_calls, elapsed=time.perf_counter()-started, cacheable=False,
                            input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
                            total_tokens=stats.total_tokens, token_source=stats.token_source)

    def _answer_weekly_manual_draft(self, question: str, context_key: str, started: float) -> AnswerResult:
        """管理員直接貼上文章：原文照收，成為目前草稿，可以接著修改或直接產圖。"""
        code, draft = weekly_pick.extract_manual_draft(question)
        if not code:
            return AnswerResult(text="請附上股票代號，例如：3006 套用文字 <貼上整篇>。", route="weekly_manual_draft",
                                gemini_calls=0, elapsed=time.perf_counter()-started)
        if len(draft) < 30:
            return AnswerResult(text="沒有讀到文章內容。用法：`3006 套用文字` 後面直接貼上整篇；"
                                     "slash 指令打不出換行時，可以用 // 代表分段。",
                                route="weekly_manual_draft", gemini_calls=0, elapsed=time.perf_counter()-started)
        try:
            candidate, _ = weekly_pick.find_weekly_candidate(code, log=self.log)
        except Exception as exc:
            self.log(f"套用文字時取得候選失敗：{type(exc).__name__}: {exc}")
            candidate = None
        if not candidate:
            return AnswerResult(text=f"{code} 不在當期本週精選 Top 10，沒有對應的權證分點資料可以畫圖。"
                                     "可以先用「本週精選排名」確認候選股。",
                                route="weekly_manual_draft", gemini_calls=0, elapsed=time.perf_counter()-started)
        session = {
            "stock_code": candidate["stock_code"], "stock_name": candidate.get("stock_name", ""),
            "draft": draft, "candidate": candidate, "facts": {},
            "mark_branches": weekly_pick.mark_branches(candidate),
            "admin_notes": [], "previous_draft": "", "updated_at": time.time(),
        }
        self._save_draft_session(context_key, session)
        branches = weekly_pick.mentioned_branches(draft, candidate)
        note = f"（文章提到的分點：{'、'.join(branches)}）" if branches else "（文章沒有提到追蹤分點，圖上不會畫買賣標註）"
        return AnswerResult(
            text=draft + f"\n\n※ 已套用為目前草稿{note}。可以接著修改，或說「這版確認，生成圖片」。",
            route="weekly_manual_draft", gemini_calls=0, elapsed=time.perf_counter()-started, cacheable=False)

    def _answer_weekly_revision(self, question: str, context_key: str, started: float) -> AnswerResult:
        """管理員：延續同一檔週精選草稿做文字修改。

        修訂用專屬 prompt（前一版擺最前面、未指示處逐字保留），不再重送風格範例；
        數字核對沒過時先重試一次，再不行才只刪有問題的句子，不整篇丟掉修改。
        """
        session = self._load_draft_session(context_key, with_candidate=True)
        if not session:
            return AnswerResult(text="目前沒有正在編輯的週精選草稿。請先輸入「股票代號＋幫我生成週精選文字」。", route="weekly_draft_revision", gemini_calls=0, elapsed=time.perf_counter()-started)
        stats = AnswerStats()
        # 管理員自己補充的事實（外資、法人、現股籌碼…）留在本篇 session，後續修改可自然融入。
        admin_notes = list(session.get("admin_notes") or [])
        if any(k in question for k in ("外資", "投信", "法人", "現股籌碼", "成本", "買超", "賣超")) or re.search(r"\d", question):
            admin_notes.append(question.strip())
            admin_notes = admin_notes[-12:]

        previous = str(session.get("draft") or "")
        draft, changed, facts = "", "", {}
        for attempt in range(2):
            prompt, facts = weekly_pick.build_weekly_revision_prompt(
                session["candidate"], previous_draft=previous, instruction=question,
                admin_notes=admin_notes, strict=attempt > 0)
            response = self.gateway.generate(prompt, purpose="weekly_draft_revision",
                                             schema=weekly_pick.WEEKLY_REVISION_SCHEMA, temperature=0.15)
            stats.record_gemini(response)
            if not response.ok:
                return AnswerResult(text=RATE_LIMIT_MESSAGE if response.rate_limited else "週精選文字修改失敗，原草稿已保留。",
                                    route="weekly_draft_revision", gemini_calls=stats.gemini_calls,
                                    elapsed=time.perf_counter()-started, cacheable=False)
            data = tools.core()._extract_json_from_text(response.text)
            candidate_draft = str((data or {}).get("draft") or "").strip() if isinstance(data, dict) else ""
            changed = str((data or {}).get("changed") or "").strip() if isinstance(data, dict) else ""
            if not candidate_draft:
                continue
            issues = find_ungrounded_numbers(candidate_draft, facts)
            if not issues:
                draft = candidate_draft
                break
            self.log(f"週精選草稿修改數字核對未通過（第 {attempt+1} 次）：{issues[:10]}")
            if attempt:
                pruned, removed = prune_ungrounded_sentences(candidate_draft, facts)
                if pruned and len(pruned) >= len(candidate_draft) * 0.6:
                    draft = pruned
                    changed = (changed + "；" if changed else "") + f"另刪掉 {len(removed)} 句對不上資料的內容"
        if not draft:
            return AnswerResult(text="這次修改沒有取得可用的文字（可能是出現對不上原始資料的數字），原草稿已保留，可以換個說法再試一次。",
                                route="weekly_draft_revision", gemini_calls=stats.gemini_calls,
                                elapsed=time.perf_counter()-started, cacheable=False)
        if re.sub(r"\s+", "", draft) == re.sub(r"\s+", "", previous):
            changed = "這次沒有任何變更（模型認為原文已符合指示）"
        session.update(draft=draft, facts=facts, admin_notes=admin_notes,
                       previous_draft=previous, updated_at=time.time())
        self._save_draft_session(context_key, session)
        footer = f"\n\n※ 本次修改：{changed}" if changed else ""
        return AnswerResult(text=draft + footer + "\n※ 可繼續修改、說「還原上一版」，或說「這版確認，生成圖片」。",
                            route="weekly_draft_revision", gemini_calls=stats.gemini_calls,
                            elapsed=time.perf_counter()-started, cacheable=False,
                            input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
                            total_tokens=stats.total_tokens, token_source=stats.token_source)

    # ---------- 草稿 session：記憶體＋SQLite，重新部署不會不見 ----------
    def _draft_state_key(self, context_key: str) -> str:
        return f"weekly_draft:{context_key}"

    def _load_draft_session(self, context_key: str, with_candidate: bool = False) -> Dict[str, Any]:
        """草稿 session：記憶體優先，沒有就讀本機儲存。

        候選資料（排名結果）只有真的要改稿／產圖時才補回來，
        免得一般問答也被還原流程拖慢。
        """
        with self._weekly_draft_lock:
            session = dict(self._weekly_drafts.get(context_key) or {})
        if not session:
            stored = local_market_cache.get_state(self._draft_state_key(context_key), {}) or {}
            if not stored or time.time() - float(stored.get("updated_at") or 0) > WEEKLY_DRAFT_MINUTES * 60:
                return {}
            session = dict(stored)
        if not with_candidate or session.get("candidate"):
            return session
        try:
            candidate, _ = weekly_pick.find_weekly_candidate(str(session.get("stock_code") or ""), log=self.log)
        except Exception as exc:
            self.log(f"草稿還原失敗：{type(exc).__name__}: {exc}")
            return {}
        if not candidate:
            return {}
        session = {**session, "candidate": candidate, "mark_branches": weekly_pick.mark_branches(candidate)}
        with self._weekly_draft_lock:
            self._weekly_drafts[context_key] = session
        self.log(f"草稿已從本地還原：{session.get('stock_code')}")
        return session

    def _save_draft_session(self, context_key: str, session: Dict[str, Any]) -> None:
        with self._weekly_draft_lock:
            self._weekly_drafts[context_key] = session
        local_market_cache.set_state(self._draft_state_key(context_key), {
            k: session.get(k) for k in ("stock_code", "stock_name", "draft", "previous_draft", "admin_notes", "updated_at")
        })

    def _clear_draft_session(self, context_key: str) -> None:
        with self._weekly_draft_lock:
            self._weekly_drafts.pop(context_key, None)
        local_market_cache.delete_state(self._draft_state_key(context_key))

    def _answer_weekly_article_image(self, context_key: str, started: float) -> AnswerResult:
        """管理員確認草稿後，沿用一般個股（3034）版型產生週精選圖片。

        結構固定為：K 線＋精簡權證買賣超標註 → 型態評分（含關鍵價位＋文章提及分點動向） → 已確認週精選文字。
        權證逐日流水只畫在 K 線，不再展開長明細表；分點範圍完全以最終文字實際提到者為準。
        """
        session = self._load_draft_session(context_key, with_candidate=True)
        if not session:
            return AnswerResult(text="目前沒有已確認的週精選草稿。請先生成並修改文字。", route="weekly_article_image", gemini_calls=0, elapsed=time.perf_counter()-started)
        code = session["stock_code"]
        name = session.get("stock_name", "")
        candidate = session.get("candidate") or {}
        # 最終圖片只顯示「管理員最後確認文字中實際提到」的分點。
        # 不因其他分點金額大、勝率高或屬精選五分點而自動補入。
        branches = weekly_pick.mentioned_branches(session.get("draft", ""), candidate)
        if not branches:
            self.log(f"週精選圖片：{code} 最終文字未提到候選分點，K 線不畫權證流水標記，追蹤分點區塊隱藏")
        # branch_name 未指定時 flow 模式會自動挑 Top 分點，因此空清單時用 sentinel 強制得到 0 筆標記。
        chart_branches = branches or ["__NO_WEEKLY_BRANCH__"]
        panels = self._get_chart_panels([code], {code: chart_branches}, mark_mode="flow", flow_source="sheet")

        # 週精選候選在排名階段已取得同一套技術資料，直接沿用來建 3034 版型的型態評分卡。
        # 「關鍵價位」照一般 3034 卡保留；「追蹤分點動向」只顯示文章提到的分點。
        if panels:
            tech = candidate.get("technical") or {}
            vp = candidate.get("volume_profile") or {}
            extras = candidate.get("technical_extras") or {}
            chips = None
            try:
                chip_results = self._run_tools([ToolCall("get_sheet_stock_chips", {"stock_code": code, "days": 20})])
                if chip_results and chip_results[0].ok:
                    chips = chip_results[0].data
            except Exception as exc:
                self.log(f"週精選圖片分點評分列略過：{code}｜{type(exc).__name__}: {exc}")
            if tech and vp:
                try:
                    panels[0]["scorecard"] = weekly_pick.build_pattern_scorecard(
                        tech, vp, extras, chips, tracked_branch_names=branches
                    )
                except Exception as exc:
                    self.log(f"週精選圖片型態評分卡略過：{code}｜{type(exc).__name__}: {exc}")

        title = f"權證分點觀察｜週精選｜{code} {name}"
        image_text = weekly_pick.weekly_image_text(session["draft"], code, name)
        article = weekly_pick.weekly_article_parts(session["draft"], code, name)
        article["subtitle"] = f"資料日 {(candidate.get('technical') or {}).get('data_date', '')}｜僅為個人投資筆記，非買賣建議".strip("｜")
        panels = list(panels) + [{"article": article}]
        return AnswerResult(
            text=image_text, route="weekly_article_image", gemini_calls=0, elapsed=time.perf_counter()-started,
            cacheable=False, panels=panels, layout="weekly_article", weekly={"image_title": title},
        )

    def _answer_admin_moneydj_image(self, question: str, started: float) -> AnswerResult:
        """管理員限定：Sheet 沒有指定分點時，明確要求 MoneyDJ 備援權證點位圖。"""
        code = weekly_pick.extract_stock_code(question)
        branch = weekly_pick.extract_admin_moneydj_branch(question)
        if not code or not branch:
            return AnswerResult(text="請指定股票代號與分點，例如：3006 新光 MoneyDJ備援產圖。",
                                route="admin_moneydj_image", gemini_calls=0, elapsed=time.perf_counter()-started)
        kf = tools.core()
        normalized = kf.normalize_branch_name(branch)
        known = {kf.normalize_branch_name(x): x for x in tools.get_known_branches()}
        if normalized in known:
            canonical = known[normalized]
            self.log(f"管理員備援請求：{canonical} 已存在 Google Sheet，直接使用 Sheet，不啟動 MoneyDJ")
            panels = self._get_chart_panels([code], {code: [canonical]}, mark_mode=ASK_MARK_MODE, flow_source="sheet")
            text = f"{code}｜{canonical} 權證分點圖"
        else:
            self.log(f"管理員 MoneyDJ 備援啟動｜stock={code}｜branch={branch}")
            panels = self._get_chart_panels([code], {code: [branch]}, mark_mode="flow", flow_source="moneydj")
            text = f"{code}｜{branch} 權證分點備援圖"
        return AnswerResult(text=text, route="admin_moneydj_image",
                            gemini_calls=0, elapsed=time.perf_counter()-started, panels=panels, cacheable=False)

    def _get_chart_panels(self, codes: List[str], branches: Optional[Dict[str, List[str]]] = None,
                          mark_mode: str = "event", flow_source: str = "sheet",
                          allow_moneydj_fallback: bool = False) -> List[Dict[str, Any]]:
        codes = list(dict.fromkeys(codes))
        calls = []
        for code in codes:
            names = (branches or {}).get(code) or []
            kwargs = {"stock_code": code, "mark_mode": mark_mode, "flow_source": flow_source,
                      "allow_moneydj_fallback": allow_moneydj_fallback}
            if names:
                kwargs["branch_name"] = ",".join(names)
            calls.append(ToolCall("get_chart_panel", kwargs))
        results = self._run_tools(calls)
        panels: List[Dict[str, Any]] = []
        for code, result in zip(codes, results):
            if result.ok:
                panels.append(result.data)
                continue
            # 權證標註失敗不能拖垮整張圖；立即重試純 K 線。
            retry = self._run_tools([ToolCall("get_chart_panel", {"stock_code": code, "with_marks": False})])[0]
            if retry.ok:
                self.log(f"K 線重試成功：{code}｜權證標註略過")
                panels.append(retry.data)
            else:
                panels.append({"stock_code": code, "error": "K 線資料暫時無法取得；以下保留已取得的分析。"})
        return panels

    def plan_only(self, question: str) -> Tuple[ParsedQuestion, QueryPlan, AnswerStats]:
        stats = AnswerStats()
        if is_weekly_pick_question(question):
            filters = weekly_pick.parse_weekly_pick_filters(question)
            parsed = ParsedQuestion(original=question, intents={"weekly_pick"}, notes=filters.describe() + (["refresh"] if filters.refresh else []))
            return parsed, QueryPlan(route="weekly_pick", need_final_llm=True), stats
        parsed = self.parser.parse(question)
        return parsed, self.router.plan(parsed, stats), stats

    def _classify_fallback(self, question: str, parsed: ParsedQuestion, stats: "AnswerStats") -> Optional[QueryPlan]:
        """規則認不出來時，花 1 次 Gemini 只做「主題／動作」分類（不產生任何數字）。"""
        if not INTENT_FALLBACK_ENABLE:
            return None
        schema = {"type": "object", "properties": {
            "subject": {"type": "string"}, "action": {"type": "string"}, "target": {"type": "string"}},
            "required": ["subject", "action", "target"]}
        prompt = ("你是台股問句分類器，只輸出 JSON，不要解釋、不要回答問題本身。\n"
                  'subject 從 ["stock","sector","market","branch","none"] 擇一；'
                  'action 從 ["pattern","members","rank","compare","chips","news","price","none"] 擇一；'
                  "target 寫問題裡提到的股票名稱或代號、或族群名稱，沒有就填空字串。\n問題：" + question)
        result = self.gateway.generate(prompt, purpose="intent", schema=schema, temperature=0.0)
        stats.record_gemini(result)
        if not result.ok:
            return None
        try:
            payload = json.loads(result.text)
        except (ValueError, TypeError):
            return None
        subject = str(payload.get("subject") or "").strip()
        action = str(payload.get("action") or "").strip()
        target = str(payload.get("target") or "").strip()
        self.log(f"🧭 AI 分類：subject={subject}｜action={action}｜target={target}")
        if subject == "sector" and target:
            hit = sector_match.match(target)
            if hit:
                mode = {"members": "members", "rank": "technical", "pattern": "technical"}.get(action, "technical")
                parsed.sector = {"mode": mode, "industry": hit["industry"], "name": hit["name"]}
                parsed.intents = set(parsed.intents) | {"sector"}
                return QueryPlan(route="rule_sector", need_final_llm=mode in ("technical", "momentum"))
        if subject == "stock" and target:
            try:
                retry = self.parser.parse(target)
            except Exception:
                retry = None
            if retry is not None and retry.stocks:
                parsed.stocks = list(retry.stocks[:2])
                parsed.intents = set(parsed.intents) | set(retry.intents)
                return self.router.plan(parsed, stats)
        near = sector_match.suggest(target or question)
        if near:
            options = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(near[:3]))
            return QueryPlan(route="clarify",
                             clarification=f"不太確定你要問的是哪一個，是不是這幾個族群之一？\n{options}\n"
                                           "可以直接輸入族群名稱，或用股票代號問我。")
        return None

    def _answer_uncached(self, question: str, started: float, parsed: Optional[ParsedQuestion] = None) -> AnswerResult:
        stats = AnswerStats()
        self.log(f"使用者問題：{question}")
        if parsed is None:
            try:
                parsed = self.parser.parse(question)
            except tools.ToolDataError as exc:
                self.log(f"問題解析失敗：{exc}")
                return AnswerResult(text="目前無法解析問題所需的基本資料，請稍後再試。", route="error", gemini_calls=0, elapsed=time.perf_counter() - started)
        self.log(f"解析結果：{json.dumps(parsed.summary(), ensure_ascii=False)}")
        plan = self.router.plan(parsed, stats)
        if plan.route == "help":
            plan = self._classify_fallback(question, parsed, stats) or plan
        self.log(
            f"🧭 主題={'族群:' + str((parsed.sector or {}).get('name', '')) if parsed.sector else ('個股:' + ','.join(c for c, _ in parsed.stocks) if parsed.stocks else ('分點:' + ','.join(parsed.branches) if parsed.branches else '無'))}"
            f"｜動作={(parsed.sector or {}).get('mode', '') or ','.join(sorted(parsed.intents)) or '-'}"
        )
        self.log(
            f"路由={plan.route}｜planner={plan.planner_used}｜final_llm={plan.need_final_llm}｜"
            f"tools={[c.name + json.dumps(c.kwargs, ensure_ascii=False) for c in plan.tool_calls]}"
        )
        if plan.clarification:
            return AnswerResult(text=plan.clarification, route=plan.route, gemini_calls=stats.gemini_calls, elapsed=time.perf_counter() - started)

        if plan.route == "rule_sector":
            return self._answer_sector(parsed.sector, started)

        pre_results: List[tools.ToolResult] = []
        if plan.route == "rule_top_warrant":
            pre_results = self._run_tools(plan.tool_calls)
            ranking = pre_results[0] if pre_results else None
            stocks = (ranking.data.get("stocks") or []) if ranking is not None and ranking.ok else []
            plan = QueryPlan(route="rule_top_warrant", need_final_llm=True)
            if stocks:
                top = stocks[0]
                pattern = self.router._pattern_plan(ParsedQuestion(original=question, intents=set(), stocks=[(top["stock_code"], top.get("stock_name", ""))]))
                plan.tool_calls = list(pattern.tool_calls)
                self.log(f"權證買進排行第一名：{top['stock_code']} {top.get('stock_name', '')}｜{top.get('event_buy_amount_text')}")

        codes = list(dict.fromkeys([code for code, _ in parsed.stocks] +
                     [c.kwargs["stock_code"] for c in plan.tool_calls if c.kwargs.get("stock_code")]))
        chart_branch = parsed.branches[0] if parsed.branches else ""
        light = plan.route == "rule_stock" and not plan.need_final_llm   # 只問股價：走輕量流程
        warrant_visual_query = bool(parsed.branches) or any(k in question for k in ("權證", "分點", "籌碼", "買賣超")) or plan.route == "rule_top_warrant"
        # 一般問答的權證圖一律用同一種版型（編號＋分點明細表），不再依問法在兩種版型之間跳。
        mark_mode = ASK_MARK_MODE
        chart_calls = []
        for c in codes:
            kwargs = {"stock_code": c, "mark_mode": mark_mode, "flow_source": "sheet"}
            if chart_branch:
                kwargs["branch_name"] = chart_branch
            if light or plan.route == "rule_pattern" or not warrant_visual_query:
                kwargs["with_marks"] = False
            chart_calls.append(ToolCall("get_chart_panel", kwargs))
        combined = pre_results + self._run_tools(plan.tool_calls + chart_calls)
        results = [r for r in combined if r.name != "get_chart_panel"]
        chart_results = [r for r in combined if r.name == "get_chart_panel"]
        panels = []
        for code, chart in zip(codes, chart_results):
            panel = dict(chart.data) if chart.ok else {"stock_code": code, "error": "K 線資料暫時無法取得；以下保留已取得的分析。"}
            panels.append(panel)
        if plan.route in ("rule_pattern", "rule_top_warrant"):
            for panel in panels:
                card = self._pattern_scorecard(panel["stock_code"], results, parsed.cost_price)
                if card:
                    panel["scorecard"] = card
                    results.append(tools.ToolResult("get_pattern_scorecard", True, card))
        contribution = next((r.data for r in results if r.name == "get_index_contribution" and r.ok), None)
        markets = list((contribution or {}).get("markets") or [])
        # 問大盤就只給加權、問櫃買就只給櫃買；沒指定才兩個都給，不要拿另一個市場充數。
        want = "tpex" if re.search(r"櫃買|上櫃|OTC", question, re.I) else (
            "twse" if re.search(r"大盤|加權|台股", question) else "")
        if want:
            markets = [m for m in markets if m.get("market") == want]
        for market in markets:
            panels.append({"contribution": market})
        breadth = next((r.data for r in results if r.name == "get_market_breadth" and r.ok), None)
        # 問「誰在拉／拖累指數」時，貢獻點數卡片已直接回答問題；
        # breadth 仍提供給 AI 做市場廣度補充，但不要再塞一張「權值股漲跌幅排行」混淆主題。
        # 問「加幾點／貢獻」時，貢獻榜出不來就不要用漲幅排行卡充數（那不是答案）。
        points_question = bool(re.search(r"點|貢獻|拉抬|拉指數|撐盤", question))
        if breadth and not markets and not points_question and (breadth.get("strongest") or breadth.get("weakest")):
            panels.append(_breadth_panel(breadth))
        text, llm_ok = self._compose(question, plan, results, stats)
        if plan.route == "rule_breadth" and not any(r.name == "get_index_contribution" and r.ok for r in results):
            # 貢獻榜算不出來時要講清楚，不能讓 AI 拿漲幅或舊資料當答案。
            text = ("※ 今天的指數貢獻榜要等交易所收盤檔發布（約 15:00）後才有；"
                    "以下只是權值股漲幅與盤面廣度，不是貢獻點數排名。" + chr(10) + chr(10) + text)
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
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            total_tokens=stats.total_tokens,
            token_source=stats.token_source,
        )

    def _answer_sector(self, request: Dict[str, str], started: float) -> AnswerResult:
        def validate(explanation: str, row: Dict[str, Any]) -> bool:
            result = tools.ToolResult("get_sector_candidate", True, row)
            payload = {"tool_results": {"get_sector_candidate": row}}
            facts = FactSheet("", [result], payload)
            # 一次只核對這檔股票，防止其他成分股的數字通過核對。
            return not facts.check(f"**{row['stock_name']}（{row['stock_code']}）**\n{explanation}")

        result = sector_analysis.answer(request, self.gateway, validate)
        # 記住排行前幾名，讓「第一名的壓力在哪」「跟第二名比呢」接得起來。
        try:
            sector_panel = ((result.get("panels") or [{}])[0] or {}).get("sector") or {}
            rows = [(str(r.get("stock_code") or ""), str(r.get("stock_name") or ""))
                    for r in (list(sector_panel.get("rows") or []) + list(sector_panel.get("others") or []))
                    if r.get("stock_code")]
            self.memory.remember_sector_rows(str(request.get("industry") or ""), rows)
        except Exception:
            pass
        # 會員看到的是 panels 畫出的族群卡片；text 保留給 Log 與 --ask。
        return AnswerResult(text=result["text"], route="rule_sector", gemini_calls=result["calls"],
                            elapsed=time.perf_counter() - started, cacheable=result["cacheable"],
                            panels=result.get("panels") or [],
                            input_tokens=int(result.get("input_tokens") or 0),
                            output_tokens=int(result.get("output_tokens") or 0),
                            total_tokens=int(result.get("total_tokens") or 0),
                            token_source=str(result.get("token_source") or "none"))

    def _pattern_scorecard(self, code: str, results: Sequence[tools.ToolResult], cost_price: Optional[float]) -> Dict[str, Any]:
        """型態評分卡：與本週精選同一套 100 分制型態評分（純 Python，0 次 Gemini）；資料不足時回傳空 dict。"""
        found = {r.name: r.data for r in results if r.ok and r.data.get("stock_code") == code}
        tech, vp = found.get("get_technical_analysis"), found.get("get_volume_profile")
        if not tech or not vp:
            return {}
        try:
            extras = weekly_pick._technical_extras(code)
            return weekly_pick.build_pattern_scorecard(tech, vp, extras, found.get("get_sheet_stock_chips"), cost_price)
        except Exception as exc:  # 評分失敗只少一張卡，不影響回答
            self.log(f"型態評分卡略過：{code}｜{type(exc).__name__}: {exc}")
            return {}

    def _run_tools(self, calls: Sequence[ToolCall]) -> List[tools.ToolResult]:
        cancel_event = threading.Event()
        request_id = str(getattr(self._request_local, "request_id", "") or "")
        futures = [(call, self.executor.submit(tools.run_tool_scoped, call.name, call.kwargs, cancel_event, request_id)) for call in calls]
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
        facts = FactSheet(question, results, payload)
        issues = facts.check(answer)
        if issues:
            # 只刪掉有問題的句子（數字對不上、張冠李戴、均線數值或站上／跌破方向寫錯、把題目假設價當報價）；
            # 刪太多（剩不到六成）才整篇改用系統整理的資料。
            pruned, removed = prune_ungrounded_sentences(answer, payload, facts)
            detail = "；".join(f"{sentence[:40]}（{'、'.join(reasons[:2])}）" for sentence, reasons in issues[:6])
            self.log(f"事實核對：刪除 {len(removed)} 句｜{detail}")
            if not pruned or len(pruned) < len(answer) * 0.6 or facts.check(pruned):
                self.log("事實核對未通過，改用規則式回答")
                return f"（AI 文字中有內容無法對應到原始資料，改顯示系統整理的資料）\n\n{rule_answer}", False
            answer = pruned
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

    def check_weekly_pick(self, user_id: int, is_admin: bool = False) -> str:
        """本週精選開放給伺服器管理員（管理員或管理伺服器權限）與 DISCORD_AI_WEEKLY_PICK_USER_IDS 內的使用者；
        回傳拒絕訊息，允許時回傳空字串。"""
        if user_id in self.config.weekly_pick_user_ids or (is_admin and self.config.weekly_pick_allow_admins):
            return ""
        return "「本週精選」目前只開放管理員使用；一般個股、權證分點問題可以照常詢問。"

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
        catalog = sector_analysis.cmoney_catalog.get_catalog()
        print(f"🔥 預熱：CMoney 細產業／概念 {len(catalog.get('groups') or {}):,} 類", flush=True)
    except Exception as exc:
        print(f"⚠️ 預熱：CMoney 族群目錄失敗（既有族群名冊仍可用）｜{type(exc).__name__}: {exc}", flush=True)
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
    if tools.FUGLE_API_KEY:
        try:
            print(f"📏 富果成交量單位校正：{tools.calibrate_fugle_volume_unit()}", flush=True)
        except Exception as exc:  # 校正失敗不影響上線
            print(f"⚠️ 富果成交量單位校正略過：{type(exc).__name__}: {exc}", flush=True)
    print(f"🔥 預熱完成｜{time.perf_counter() - started:.1f} 秒", flush=True)


def with_context_note(result: "AnswerResult") -> str:
    """延續上一題時，在回答最上面用小字標出 Bot 是怎麼理解這題的（例如「延續上一題：華邦電（2344）」）。"""
    if not result.context_note:
        return result.text
    return f"※ {result.context_note}（輸入「重新開始」可清除）\n{result.text}"


def _is_guild_admin(member) -> bool:
    """Discord 伺服器管理員：有「管理員」或「管理伺服器」權限（私訊或取不到權限時為 False）。"""
    perms = getattr(member, "guild_permissions", None)
    return bool(perms is not None and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)))


ADMIN_HELP_MESSAGE = """**管理員指令**（一般會員看不到，也不能使用）

【本週精選】
• `本週精選排名`：算出當期 Top 10
• `3006 幫我生成週精選文字`：挑一檔生成草稿
• 接著直接說修改需求：`權證部分短一點`、`不要提到均線`、`把新光的勝率補上`
• `還原上一版`：回到修改前
• `這版確認，生成圖片`：產出精選圖片
• `3006 套用文字 <貼上整篇>`：直接沿用自己寫好的文章（不經過 AI；slash 打不出換行時用 // 分段）
• `目前草稿`：看現在編輯中的文章

【資料維護】
• `系統狀態`：名冊、日K底庫、型態分數、是否永久保存
• `更新市場底庫`：補齊全市場日K（每個交易日 2 個請求）
• `更新族群名冊`：重新掃描族群成分股（約 10～20 分鐘）
• `族群雷達`／`轉強族群`／`轉弱族群`：盤中類股指數近 30 分鐘變化（Δ ppt），/ask 也可問「哪些族群正在轉強」
• `用量`：今日 Gemini 與各 API 使用量

一般個股、族群、權證分點問題請照常用 /ask。"""


WEEKLY_PICK_ACK = "📊 本週精選候選計算中，正在整理事件、股價與分點資料。首次查詢可能需要數分鐘；完成後這張圖會更新為結果。"

USAGE_LOG_SECONDS = max(60, tools._env_int("DISCORD_AI_USAGE_LOG_SECONDS", 300))
BACKGROUND_TICK_SECONDS = max(30, tools._env_int("DISCORD_AI_BACKGROUND_TICK_SECONDS", 60))
CMONEY_MEMBER_WARMUP_PER_TICK = max(0, tools._env_int("DISCORD_AI_CMONEY_MEMBER_WARMUP_PER_TICK", 1))
# 全市場日K底庫：證交所／櫃買官方資料，每個交易日 2 個請求，不吃 FinMind 額度。
MARKET_SYNC_ENABLE = _env_flag("DISCORD_AI_MARKET_SYNC_ENABLE", "1")
MARKET_SYNC_BUDGET = max(60.0, tools._env_float("DISCORD_AI_MARKET_SYNC_BUDGET", 600.0))
MARKET_SCORE_BUDGET = max(30.0, tools._env_float("DISCORD_AI_MARKET_SCORE_BUDGET", 240.0))

def _process_resource_snapshot(previous_cpu: Optional[Tuple[float, float]] = None) -> Tuple[Dict[str, Any], Tuple[float, float]]:
    """不用額外套件，從 Linux /proc 與磁碟統計目前 Bot 資源；非 Linux 時安全退化。"""
    wall = time.monotonic(); cpu = time.process_time()
    cpu_pct = None
    if previous_cpu:
        wall_delta = max(1e-6, wall - previous_cpu[0])
        cpu_pct = max(0.0, (cpu - previous_cpu[1]) / wall_delta * 100.0)
    rss = 0
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        rss = pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        pass
    disk_path = "/data" if os.path.exists("/data") else str(Path(__file__).parent)
    try:
        disk = shutil.disk_usage(disk_path)
        disk_info = {"used": disk.used, "total": disk.total, "free": disk.free, "path": disk_path}
    except Exception:
        disk_info = {"used": 0, "total": 0, "free": 0, "path": disk_path}
    return {"rss_bytes": rss, "cpu_pct": cpu_pct, "disk": disk_info}, (wall, cpu)

def _fmt_mb(value: Any) -> str:
    try:
        return f"{float(value) / 1024 / 1024:.1f}MB"
    except Exception:
        return "-"

_volume_curve_day = [""]


def _market_maintenance_loop(stop: threading.Event) -> None:
    """背景維護全市場底庫：開機補齊歷史、每個交易日收盤後補當天，再把型態分數算完。

    全部只用官方每日行情（2 個請求／交易日）與本地 CPU，不會為了單一問題掃市場。
    """
    last_attempt = ""
    while not stop.is_set():
        try:
            info = market_data.coverage()
            now = tools.taipei_now()
            # 盤中每隔幾分鐘存一張族群漲幅快照（2 個請求、0 次 Gemini），供轉強／轉弱雷達比較。
            if sector_radar.session_open(now):
                try:
                    sector_radar.tick()
                except Exception as exc:
                    print(f"⚠️ 族群雷達快照略過｜{type(exc).__name__}", flush=True)
                try:
                    import intraday_volume
                    intraday_volume.sample_basket()      # 基準籃子取樣（走背景額度）
                except Exception as exc:
                    print(f"⚠️ 盤中量能取樣略過｜{type(exc).__name__}", flush=True)
            today = now.strftime("%Y-%m-%d")
            # 收盤後用當日實際成交量回算時段係數，讓盤中量能估算越用越準（每天一次）。
            if _volume_curve_day[0] != today and now.hour * 60 + now.minute >= 15 * 60:
                _volume_curve_day[0] = today
                try:
                    import intraday_volume
                    codes = local_market_cache.codes_with_history(2)
                    lots = {c: (v.get("volume") or 0) / 1000
                            for c, v in local_market_cache.latest_changes(codes).items()}
                    avg20 = {c: float((v or {}).get("avg_lots") or 0)
                             for c, v in local_market_cache.liquidity_map(20).items()}
                    intraday_volume.calibrate(lots, avg20, day=today)
                except Exception as exc:
                    print(f"⚠️ 盤中量能曲線校正略過｜{type(exc).__name__}", flush=True)
            minutes = now.hour * 60 + now.minute
            after_close = now.weekday() < 5 and minutes >= 14 * 60 + 5
            need_history = int(info.get("days") or 0) < 60
            need_today = after_close and str(info.get("last_day") or "") < today and last_attempt != today
            if need_history or need_today:
                last_attempt = today if need_today else last_attempt
                result = market_data.sync(budget_seconds=MARKET_SYNC_BUDGET, log=lambda m: print(f"🗂️ {m}", flush=True))
                print(f"🗂️ 市場底庫：{result['days']} 個交易日 × {result['stocks']:,} 檔｜最新 {result['last_day']}｜"
                      f"本輪 {result['requests']} 個請求、{result['elapsed']:.0f} 秒", flush=True)
            if int(market_data.coverage().get("days") or 0) >= 20:
                market_scan.score_pending(budget_seconds=MARKET_SCORE_BUDGET)
        except Exception as exc:
            print(f"⚠️ 市場底庫背景維護失敗｜{type(exc).__name__}: {exc}", flush=True)
        if stop.wait(300):
            break


def _usage_monitor_loop(engine: "AceQueryEngine", stop: threading.Event) -> None:
    """Railway 背景監控：API 用量、記憶體、快取與延遲收盤重查。

    只做低頻 CMoney 雷達與「已被問過且仍暫定收盤」的 Fugle 重查；
    不做 Fugle 全市場預抓。
    """
    previous_cpu = None
    last_log = 0.0
    last_cmoney = 0.0
    while not stop.wait(BACKGROUND_TICK_SECONDS):
        now = tools.taipei_now()
        # 盤中 CMoney 雷達先在背景暖好，會員問「現在族群誰最強」時直接讀快取。
        minutes = now.hour * 60 + now.minute
        if now.weekday() < 5 and 8 * 60 + 50 <= minutes <= 13 * 60 + 45:
            if time.time() - last_cmoney >= max(60, getattr(sector_analysis.cmoney_catalog, "RADAR_TTL", 300)):
                try:
                    sector_analysis.cmoney_catalog.get_live_radar(refresh=True)
                except Exception as exc:
                    print(f"⚠️ CMoney 盤中族群雷達背景更新失敗｜{type(exc).__name__}", flush=True)
                last_cmoney = time.time()
        # CMoney 分類屬低頻靜態資料：每個 tick 只補少量尚未存到 Persistent Volume 的族群，
        # 逐步把細產業／概念成分股抓齊，不影響 Fugle 額度。
        # 自建名冊在的時候，CMoney 成分股只是舊備援，不需要在背景一直補（也少打它的網站）。
        if CMONEY_MEMBER_WARMUP_PER_TICK > 0 and not sector_roster.available():
            try:
                sector_analysis.cmoney_catalog.warm_member_catalog_batch(CMONEY_MEMBER_WARMUP_PER_TICK)
            except Exception as exc:
                print(f"⚠️ CMoney 成分股名冊背景補齊失敗｜{type(exc).__name__}", flush=True)
        # 只重查曾被問過、13:30 後仍未正式收盤的股票。
        try:
            recheck = tools.recheck_provisional_closes(max_items=5)
            if recheck.get("checked"):
                print(f"🔁 盤後收盤查核｜checked={recheck['checked']}｜confirmed={recheck['confirmed']}｜pending={recheck['pending']}", flush=True)
        except Exception as exc:
            print(f"⚠️ 盤後收盤查核失敗｜{type(exc).__name__}", flush=True)
        if time.time() - last_log < USAGE_LOG_SECONDS:
            continue
        resources, previous_cpu = _process_resource_snapshot(previous_cpu)
        usage = tools.api_usage_snapshot()
        fm = tools.finmind_usage()
        mem = engine.memory.stats()
        market_cache = local_market_cache.stats()
        fugle = usage.get("Fugle", {})
        cm_stats = sector_analysis.cmoney_catalog.cache_stats()
        lines = [
            "📊 ACE USAGE",
            f"  Railway process｜RAM {_fmt_mb(resources['rss_bytes'])}｜CPU {resources['cpu_pct']:.1f}%" if resources['cpu_pct'] is not None else f"  Railway process｜RAM {_fmt_mb(resources['rss_bytes'])}｜CPU warming",
            f"  Railway disk｜{_fmt_mb(resources['disk']['used'])} / {_fmt_mb(resources['disk']['total'])}｜path={resources['disk']['path']}",
            f"  Memory sessions｜{mem['entries']} / {mem['max_entries']}｜TTL={mem['ttl_minutes']}m｜queue={engine.queue_size()} / {ANSWER_QUEUE_LIMIT}",
            f"  Local market cache｜stocks={market_cache['stocks']}｜bars={market_cache['bars']}｜scores={market_cache['scores']}｜size={_fmt_mb(market_cache['bytes'])}",
            f"  CMoney catalog cache｜groups={cm_stats.get('groups', 0)}｜member_groups={cm_stats.get('member_groups', 0)}",
            f"  Sector roster｜groups={len(sector_roster.catalog())}｜built={sector_roster.built_at() or '尚未建立'}",
            f"  Market base｜{market_cache.get('stocks', 0):,} 檔 × {market_cache.get('days', 0)} 日｜"
            f"最新 {market_cache.get('last_day', '-') or '-'}｜型態分數 {market_cache.get('scores', 0):,} 檔｜"
            f"{'persistent /data' if market_cache.get('persistent') else 'EPHEMERAL（未掛 Volume，重新部署會清空）'}",
            f"  Fugle intraday｜{fugle.get('last_60s', 0)} / 60 requests/min｜10m={fugle.get('last_10m', 0)}｜errors={fugle.get('errors', 0)}｜background cap={tools.FUGLE_BACKGROUND_LIMIT_PER_MIN}/min｜hard={tools.FUGLE_HARD_LIMIT_PER_MIN}/min",
            f"  Fugle provisional close｜{tools.provisional_close_stats().get('count', 0)} pending",
        ]
        if fm.get("available"):
            lines.append(f"  FinMind official｜{fm.get('used', 0)} / {fm.get('limit', 0)}｜remaining={fm.get('remaining', '-')}")
        else:
            lines.append(f"  FinMind official｜usage unavailable ({fm.get('reason', 'unknown')})")
        for provider in ("FinMindData", "CMoney", "MoneyDJFlow", "GoogleSheet", "Gemini"):
            row = usage.get(provider, {})
            lines.append(f"  {provider}｜60s={row.get('last_60s', 0)}｜10m={row.get('last_10m', 0)}｜process={row.get('process_total', 0)}｜errors={row.get('errors', 0)}｜avg={row.get('avg_latency', 0):.2f}s")
        print("\n".join(lines), flush=True)
        last_log = time.time()


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
    admins = "伺服器管理員（管理員／管理伺服器權限）" if config.weekly_pick_allow_admins else ""
    users = f"指定使用者 {sorted(config.weekly_pick_user_ids)}" if config.weekly_pick_user_ids else ""
    if admins or users:
        print(f"🔒 本週精選開放：{'＋'.join(x for x in (admins, users) if x)}")
    else:
        print("⚠️ 本週精選：未開放管理員、也沒設定 DISCORD_AI_WEEKLY_PICK_USER_IDS，任何人都不能使用")

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
        render_started = asyncio.get_running_loop().time()
        try:
            if weekly:
                images = await asyncio.to_thread(weekly_image.make_weekly_attachments, question, weekly, panels, max_bytes=limit)
            else:
                images = [await asyncio.to_thread(answer_image.make_attachment, question, text, panels, max_bytes=limit)]
        except Exception as exc:
            print(f"⚠️ 圖片產生失敗：{type(exc).__name__}: {exc}", flush=True)
            images = [await asyncio.to_thread(answer_image.make_attachment, "暫時無法產生回答",
                      "圖片產生失敗或內容超過附件容量，請縮小查詢範圍後再試。", max_bytes=limit)]
        render_elapsed = asyncio.get_running_loop().time() - render_started
        total_bytes = sum(len(data) for data, _ in images)
        print(f"🖼️ 圖片產生完成｜張數={len(images)}｜render={render_elapsed:.2f}s｜大小={total_bytes/1024:.1f}KB", flush=True)
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

    async def interaction_text(interaction, text: str, *, ephemeral=False):
        """草稿與維護指令用純文字回覆；太長時自動分段，方便直接複製。"""
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        chunks = split_discord_message(text or "（沒有內容）", 1900)
        await interaction.edit_original_response(content=chunks[0], attachments=[], allowed_mentions=no_mentions)
        for chunk in chunks[1:]:
            await interaction.followup.send(content=chunk, ephemeral=ephemeral, allowed_mentions=no_mentions)

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
    usage_stop = threading.Event()
    usage_monitor_started = threading.Event()

    @client.event
    async def on_ready() -> None:
        if not usage_monitor_started.is_set():
            usage_monitor_started.set()
            threading.Thread(target=_usage_monitor_loop, args=(engine, usage_stop), name="ace-usage-monitor", daemon=True).start()
            if MARKET_SYNC_ENABLE:
                threading.Thread(target=_market_maintenance_loop, args=(usage_stop,), name="ace-market-base", daemon=True).start()
        print(
            f"✅ 艾斯 AI 已上線：{client.user}｜指令 /{config.slash_command_name}（一般）＋/{config.admin_command_name}（管理員）" + (f" 與 {config.command_prefix}" if config.prefix_command_enabled else "") + "｜"
            f"允許使用者 {'不限' if config.allow_all_users else str(len(config.allowed_user_ids)) + ' 人'}｜限制頻道 {len(config.allowed_channel_ids) or '不限'}｜"
            f"debug={config.debug}",
            flush=True,
        )

    async def handle_question(interaction: "discord.Interaction", question: str, admin_mode: bool) -> None:
        request_started = asyncio.get_running_loop().time()
        user_id, channel_id = interaction.user.id, interaction.channel_id or 0
        is_admin = _is_guild_admin(interaction.user)
        denied = guard.check_permission(user_id, channel_id, interaction.guild_id)
        if denied:
            engine.log(f"拒絕使用者 {user_id}｜頻道 {channel_id}｜{denied}")
            await interaction_image(interaction, "使用權限", denied, ephemeral=True)
            return
        if admin_mode:
            admin_denied = guard.check_weekly_pick(user_id, is_admin)
            if admin_denied:
                engine.log(f"管理員指令拒絕使用者 {user_id}")
                await interaction_image(interaction, "使用權限", admin_denied, ephemeral=True)
                return
        busy = guard.acquire(user_id)
        if busy:
            await interaction_image(interaction, "請稍候", busy, ephemeral=True)
            return
        try:
            await interaction.response.defer(thinking=True, ephemeral=config.ephemeral)
            if admin_mode and is_weekly_pick_question(question) and not weekly_pick.is_weekly_draft_question(question):
                await interaction_image(interaction, question, WEEKLY_PICK_ACK, ephemeral=config.ephemeral)
            loop = asyncio.get_running_loop()

            def on_queue(ahead: int) -> None:
                # 在工作執行緒被呼叫：把「排隊中」圖片丟回 Discord 事件迴圈送出，不等待結果。
                message = f"目前前面還有 {ahead} 個問題在處理，輪到你時會自動更新這則回覆。"
                asyncio.run_coroutine_threadsafe(
                    interaction_image(interaction, question, message, ephemeral=config.ephemeral), loop)

            context_key = f"{interaction.guild_id or 0}:{channel_id}:{user_id}"
            result = await asyncio.to_thread(engine.answer, question, context_key, on_queue, is_admin, admin_mode)
            image_question = (result.weekly or {}).get("image_title", question) if result.layout == "weekly_article" else question
            upload_started = asyncio.get_running_loop().time()
            if result.as_text:
                await interaction_text(interaction, with_context_note(result), ephemeral=config.ephemeral)
            else:
                await interaction_image(interaction, image_question, with_context_note(result), result.panels, ephemeral=config.ephemeral,
                                        weekly=result.weekly if result.layout == "weekly_pick" else None)
            upload_elapsed = asyncio.get_running_loop().time() - upload_started
            total_elapsed = asyncio.get_running_loop().time() - request_started
            print(
                f"📊 REQUEST METRICS｜id={result.request_id}｜route={result.route}｜compute={result.elapsed:.2f}s｜"
                f"render+upload={upload_elapsed:.2f}s｜end_to_end={total_elapsed:.2f}s｜cache={result.cache_hit}｜"
                f"Gemini={result.gemini_calls}｜tokens={result.input_tokens}+{result.output_tokens}={result.total_tokens}({result.token_source})｜"
                f"API={result.api_usage}", flush=True)
        except discord.HTTPException as exc:
            print(f"⚠️ Discord /{config.slash_command_name} 回覆失敗：{exc}", flush=True)
        except Exception as exc:  # 單題失敗不可讓 Bot 中斷
            print(f"❌ 艾斯 AI /{config.slash_command_name} 處理失敗：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()   # 印出檔名與行號，numpy/pandas 這類例外沒有堆疊就無法定位
            try:
                await interaction_image(interaction, "暫時無法完成", "處理問題時發生錯誤，請稍後再試。", ephemeral=config.ephemeral)
            except discord.HTTPException as send_exc:
                print(f"⚠️ 錯誤訊息送出失敗：{send_exc}", flush=True)
        finally:
            guard.release(user_id)

    @client.tree.command(name=config.slash_command_name, description="艾斯 AI：問股票型態、技術面、權證分點與新聞")
    @app_commands.describe(question="例如：2330現在型態好嗎／記憶體族群誰型態最好／2330最近有什麼新聞")
    async def ask_command(interaction: "discord.Interaction", question: str) -> None:
        await handle_question(interaction, question, admin_mode=False)

    @client.tree.command(name=config.admin_command_name, description="艾斯 AI 管理員：本週精選、草稿編輯與資料維護")
    @app_commands.describe(question="例如：本週精選排名／3006 幫我生成週精選文字／系統狀態／說明")
    async def admin_command(interaction: "discord.Interaction", question: str) -> None:
        await handle_question(interaction, question, admin_mode=True)

    @client.event
    async def on_message(message: "discord.Message") -> None:
        request_started = asyncio.get_running_loop().time()
        # 指令統一用 /ask；!ace 文字指令預設關閉（DISCORD_AI_PREFIX_COMMAND_ENABLE=1 才開）。
        if not config.prefix_command_enabled or message.author.bot:
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
        if weekly_pick.is_weekly_admin_feature_question(question):
            weekly_denied = guard.check_weekly_pick(user_id, _is_guild_admin(message.author))
            if weekly_denied:
                engine.log(f"本週精選拒絕使用者 {user_id}")
                await reply_image(message, "使用權限", weekly_denied)
                return
        busy = guard.acquire(user_id)
        if busy:
            await reply_image(message, "請稍候", busy)
            return
        pending = None
        try:
            if is_weekly_pick_question(question) and not weekly_pick.is_weekly_draft_question(question):
                pending = await reply_image(message, question, WEEKLY_PICK_ACK)
            async with message.channel.typing():
                context_key = f"{message.guild.id if message.guild else 0}:{channel_id}:{user_id}"
                result = await asyncio.to_thread(engine.answer, question, context_key, None, _is_guild_admin(message.author))
            image_question = (result.weekly or {}).get("image_title", question) if result.layout == "weekly_article" else question
            upload_started = asyncio.get_running_loop().time()
            await reply_image(message, image_question, with_context_note(result), result.panels, pending=pending,
                              weekly=result.weekly if result.layout == "weekly_pick" else None)
            upload_elapsed = asyncio.get_running_loop().time() - upload_started
            total_elapsed = asyncio.get_running_loop().time() - request_started
            print(
                f"📊 REQUEST METRICS｜id={result.request_id}｜route={result.route}｜compute={result.elapsed:.2f}s｜"
                f"render+upload={upload_elapsed:.2f}s｜end_to_end={total_elapsed:.2f}s｜cache={result.cache_hit}｜"
                f"Gemini={result.gemini_calls}｜tokens={result.input_tokens}+{result.output_tokens}={result.total_tokens}({result.token_source})｜"
                f"API={result.api_usage}", flush=True)
        except discord.HTTPException as exc:
            print(f"⚠️ Discord 訊息送出失敗：{exc}", flush=True)
        except Exception as exc:  # 單題失敗不可讓 Bot 中斷
            print(f"❌ 艾斯 AI 處理問題失敗：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            try:
                await reply_image(message, "暫時無法完成", "處理問題時發生錯誤，請稍後再試。", pending=pending)
            except discord.HTTPException as send_exc:
                print(f"⚠️ 錯誤訊息送出失敗：{send_exc}", flush=True)
        finally:
            guard.release(user_id)

    try:
        client.run(config.token)
    finally:
        usage_stop.set()


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
