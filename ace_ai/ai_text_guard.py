"""AI 解讀不可出現資料欄位名稱。

11-02 使用者：圖上出現「扣除 top_stock 晉泰後」「follow.status 為 warming」，這些是程式給 AI 的欄位，
會員不該看到。兩道防線：Gateway 寫解讀時加上 PROMPT_RULE；畫 AI 解讀卡前用 clean_card 再掃一次
（圖卡都要經過這裡，新增的 AI 用途不用另外記得處理）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

PROMPT_RULE = ("\n輸出給會員看的文字一律用白話中文：不可寫出資料欄位名稱或程式用語"
               "（例如 top_stock、follow.status、warming、null），要改成它代表的意思。")

# 認得的欄位直接換成白話，其餘含欄位名稱的句子整句拿掉
PLAIN_WORDS = {"avg_without_top_stock_pct": "扣除領先個股後的平均", "top_stock": "領先個股",
               "avg_change_pct": "平均漲跌幅", "median_change_pct": "中位數", "rank_score_pct": "排序值",
               "change_5d_pct": "近 5 日漲跌", "up_count": "上漲家數", "limit_up_count": "漲停家數",
               "stock_pool_avg_pct": "股票池平均", "vs_stock_pool_pct": "相對股票池", "volume_vs_avg_times": "量能倍數",
               "behind_median_pct": "落後中位數"}
_FIELD_RE = re.compile(r"[A-Za-z0-9一-鿿]_[A-Za-z0-9一-鿿]"          # snake_case，含「下方支撐_由近到遠」
                       r"|(?<![A-Za-z])(?:follow\.[a-z_]+|warming|stale|laggards|picks|null|None|true|false|True|False)(?![A-Za-z])")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?|\n")


def field_names(text: Any) -> List[str]:
    """文字裡看起來像資料欄位的字（給 AI 重寫用）；沒有回空清單。"""
    return [m.group(0) for m in _FIELD_RE.finditer(str(text or ""))]


def clean_text(text: Any) -> str:
    """認得的欄位換白話；還是有欄位名稱的句子拿掉，其他句子原樣保留。"""
    value = str(text or "")
    if not field_names(value):
        return value
    for field in sorted(PLAIN_WORDS, key=len, reverse=True):
        value = value.replace(field, PLAIN_WORDS[field])
    kept = "".join(s for s in _SENTENCE_RE.findall(value) if not field_names(s)).strip()
    print(f"📝 AI 解讀有資料欄位名稱，已處理｜{str(text)[:200]}", flush=True)
    return kept


def clean_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """AI 解讀卡裡 AI 寫的欄位都掃過；程式寫的頁尾、標題不動。"""
    out = dict(card or {})
    for key in ("answer", "summary", "notice"):
        if out.get(key):
            out[key] = clean_text(out[key])
    if isinstance(out.get("why"), list):
        out["why"] = [t for t in (clean_text(w) for w in out["why"]) if t]
    elif out.get("why"):
        out["why"] = clean_text(out["why"])
    if isinstance(out.get("scenarios"), list):
        out["scenarios"] = [dict(s, text=clean_text(s.get("text"))) if isinstance(s, dict) else clean_text(s)
                            for s in out["scenarios"]]
    return out
