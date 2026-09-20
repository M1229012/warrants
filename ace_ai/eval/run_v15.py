"""族群辨識迴歸測試（離線、不連外、0 次 Gemini）。

用法：
    python ace_ai/eval/run_v15.py

只測 sector_analysis.detect_request 的判斷結果（模式與對到哪個族群），
不呼叫任何行情 API，也不需要 FinMind／Google Sheet 金鑰。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sector_analysis  # noqa: E402
import sector_match     # noqa: E402

QUESTIONS = Path(__file__).with_name("questions_v15.txt")


def load_cases():
    cases = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            cases.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return cases


def run() -> int:
    cases = load_cases()
    failed = []
    for question, want_mode, want_target in cases:
        request = sector_analysis.detect_request(question)
        mode = (request or {}).get("mode", "none")
        name = str((request or {}).get("name", ""))
        industry = str((request or {}).get("industry", ""))
        ok = mode == want_mode
        if ok and want_target:
            ok = want_target in name or want_target in industry or want_target in str(
                (request or {}).get("merged_names", ""))
        if not ok:
            failed.append((question, want_mode, want_target, mode, name))
    print(f"族群辨識：{len(cases) - len(failed)}/{len(cases)} 通過")
    for question, want_mode, want_target, mode, name in failed:
        print(f"  ✗ {question}｜期望 {want_mode} {want_target}｜實際 {mode} {name}")
    if not sector_match._index():
        print("⚠️ 讀不到族群名冊（sector_roster.json），族群題目一定會失敗。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
