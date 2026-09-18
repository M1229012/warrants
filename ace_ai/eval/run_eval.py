"""艾斯 AI 固定題評測。

快速模式（預設，0 次 Gemini、不抓股價）：只測問題解析、路由、追問記憶，改完程式隨時可跑。
    python ace_ai/eval/run_eval.py

完整模式（真的抓資料、呼叫 Gemini，需要和 Railway 相同的環境變數）：另外檢查回答內容與耗時。
    python ace_ai/eval/run_eval.py --full
    python ace_ai/eval/run_eval.py --full --only P01,F01     # 只跑指定題目

結果寫到 ace_ai/eval/results/eval_YYYYMMDD_HHMM.csv，最後印出通過率與失敗原因。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import discord_ai_bot as bot  # noqa: E402

# 回答圖片不可出現的字：資料供應商與內部工作表名稱（新聞媒體名稱可以）。
FORBIDDEN_WORDS = ("FinMind", "富果", "Fugle", "Google Sheet", "GoogleSheet", "工作表", "快取_", "gspread")
FORBIDDEN_PATTERNS = (
    (re.compile(r"第\s*\d+\s*(?:日|個交易日)"), "出現「第 N 日」寫法"),
    (re.compile(r"盤中[^。\n]{0,20}?(?:量縮|量增|爆量|放量|縮量|量能放大|量能萎縮)"), "用盤中累計量判斷量縮／量增"),
)
FALLBACK_PHRASE = "改顯示系統整理的資料"
PATTERN_ROUTES = {"rule_pattern", "rule_top_warrant"}


def load_cases(only: str = "") -> List[Dict[str, Any]]:
    cases = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["cases"]
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    return cases


# ---------------- 快速模式 ----------------

def fast_turn(engine: "bot.AceQueryEngine", key: str, question: str) -> Dict[str, Any]:
    """重現 engine.answer 的前半段（記憶、解析、路由），但不抓資料、不呼叫 Gemini。"""
    compact = re.sub(r"\s+", "", question)
    if any(word in compact for word in bot.MEMORY_RESET_WORDS):
        engine.memory.clear(key)
        return {"route": "memory_reset", "stocks": [], "cost": None, "note": "", "llm": False}
    if bot.is_weekly_pick_question(question):
        return {"route": "weekly_pick", "stocks": [], "cost": None, "note": "", "llm": True}
    parsed = engine.parser.parse(question)
    note = engine.memory.resolve(key, parsed)
    plan = engine.router.plan(parsed, bot.AnswerStats())
    if not plan.clarification:
        engine.memory.update(key, parsed)
    return {"route": plan.route, "stocks": [c for c, _ in parsed.stocks], "cost": parsed.cost_price,
            "note": note, "llm": plan.need_final_llm}


# ---------------- 完整模式 ----------------

def full_turn(engine: "bot.AceQueryEngine", key: str, question: str) -> Dict[str, Any]:
    started = time.perf_counter()
    result = engine.answer(question, key)
    elapsed = time.perf_counter() - started
    text = result.text or ""
    return {
        "route": result.route,
        "stocks": [p.get("stock_code", "") for p in result.panels or []],
        "cost": None,
        "note": result.context_note,
        "llm": result.gemini_calls > 0,
        "text": text,
        "elapsed": elapsed,
        "gemini_calls": result.gemini_calls,
    }


def check_turn(expect: Dict[str, Any], got: Dict[str, Any], full: bool) -> List[str]:
    problems: List[str] = []
    route = got["route"]
    if "route" in expect and route not in expect["route"] and not (full and route == "answer_cache"):
        problems.append(f"路由 {route}，預期 {'/'.join(expect['route'])}")
    if "stocks" in expect and not (full and route not in PATTERN_ROUTES | {"rule_stock"}):
        want = expect["stocks"]
        have = got["stocks"]
        # 完整模式從 K 線圖取得股票（排行題會多畫第一名），只檢查預期的股票都有出現。
        ok = have == want if not full else all(c in have for c in want)
        if not ok:
            problems.append(f"股票 {have}，預期 {want}")
    if not full and "cost" in expect and got["cost"] != expect["cost"]:
        problems.append(f"成本 {got['cost']}，預期 {expect['cost']}")
    if "note" in expect:
        if expect["note"] and expect["note"] not in (got["note"] or ""):
            problems.append(f"追問說明「{got['note']}」未包含「{expect['note']}」")
        if not expect["note"] and got["note"]:
            problems.append(f"不該延續上一題，卻出現「{got['note']}」")
    if not full and "llm" in expect and bool(got["llm"]) != bool(expect["llm"]):
        problems.append(f"need_final_llm={got['llm']}，預期 {expect['llm']}")
    if full:
        text = got.get("text", "")
        for word in FORBIDDEN_WORDS:
            if word in text:
                problems.append(f"出現資料來源字眼「{word}」")
        for pattern, label in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                problems.append(f"{label}：{match.group(0)}")
        if FALLBACK_PHRASE in text:
            problems.append("事實核對未通過，整篇改用系統整理資料")
        if expect.get("llm") and route in PATTERN_ROUTES and "【回答】" not in text:
            problems.append("缺少【回答】段落")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="艾斯 AI 固定題評測")
    parser.add_argument("--full", action="store_true", help="真的抓資料並呼叫 Gemini（需要環境變數）")
    parser.add_argument("--only", default="", help="只跑指定題號，例如 P01,F01")
    args = parser.parse_args()

    config = bot.BotConfig.from_env()
    engine = bot.AceQueryEngine(config)
    if not args.full:
        # 快速模式不花 Gemini：原本會交給 Planner 的題目，直接記成 planner 路由。
        engine.router._planner_plan = lambda parsed, stats: bot.QueryPlan(route="planner", planner_used=True)

    cases = [c for c in load_cases(args.only) if not (args.full and c.get("fast_only"))]
    rows: List[Dict[str, Any]] = []
    passed = total = 0
    failures: List[Tuple[str, str, List[str]]] = []
    for case in cases:
        key = f"eval:{case['id']}"
        engine.memory.clear(key)
        for index, turn in enumerate(case["turns"], 1):
            label = f"{case['id']}-{index}"
            try:
                got = full_turn(engine, key, turn["q"]) if args.full else fast_turn(engine, key, turn["q"])
                problems = check_turn(turn, got, args.full)
            except Exception as exc:  # 單題錯誤記錄下來，繼續跑下一題
                got = {"route": "exception", "stocks": [], "cost": None, "note": "", "llm": False}
                problems = [f"例外 {type(exc).__name__}: {exc}"]
            total += 1
            passed += not problems
            if problems:
                failures.append((label, turn["q"], problems))
            print(f"{'✅' if not problems else '❌'} {label:<6} {turn['q']:<24} route={got['route']:<20}"
                  + (f" {got.get('elapsed', 0):.1f}s Gemini×{got.get('gemini_calls', 0)}" if args.full else "")
                  + ("" if not problems else "｜" + "；".join(problems)))
            rows.append({
                "id": label, "group": case.get("group", ""), "question": turn["q"], "passed": not problems,
                "route": got["route"], "stocks": " ".join(got["stocks"]), "cost": got.get("cost") or "",
                "context_note": got.get("note") or "", "elapsed_s": round(got.get("elapsed", 0), 2),
                "gemini_calls": got.get("gemini_calls", ""), "problems": "；".join(problems),
                "answer": (got.get("text") or "")[:1500],
            })

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"eval_{'full' if args.full else 'fast'}_{datetime.now():%Y%m%d_%H%M}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
    print("=" * 60)
    print(f"通過 {passed}／{total}（{passed / max(1, total):.0%}）｜結果：{out}")
    if args.full and rows:
        times = sorted(r["elapsed_s"] for r in rows if r["elapsed_s"])
        if times:
            print(f"耗時：中位數 {times[len(times) // 2]:.1f}s｜最慢 {times[-1]:.1f}s")
    for label, question, problems in failures:
        print(f"  {label} {question}：{'；'.join(problems)}")
    engine.executor.shutdown(wait=False)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
