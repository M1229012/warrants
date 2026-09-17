"""
權證分點歷史深度回補（可分段、可續跑、不會整輪作廢）
==========================================================
為什麼不直接用主程式的 repair：

  repair 是為「200 天、一次跑完」設計的，拿來回補 3 年會撞上四個問題（2026-09-17 實測）：

  1. 查詢太重被限流。掃描區間公式是 int(保留天數 × 1.6) + 20，
     800 天 → 每個 API4 請求都查 1,300 天（2023/02/24～今天），而且所有權證共用同一段。
  2. 沒有續跑。檔頭 AUDIT8 註解寫了「候選快取」「API5 跨次續跑」，
     但 MONEYDJ_CANDIDATE_CACHE_ENABLED／MONEYDJ_API5_CHECKPOINT_ENABLED
     只有定義、整支程式沒有任何地方呼叫。撞到 6 小時上限就全部歸零。
  3. fail-closed。只要有一組 API5 最終失敗，整輪丟棄。
  4. 母體不完整。get_all_call_warrants() ＝官方「現行」清單 ∪ 本機快取，
     2023–2025 年已到期、不在現行清單的權證根本不會被掃。

這支程式的對應做法：

  1. 每檔權證只查它自己的生命週期 ∩ [2023/09/11, 今天]。
  2. API4 與 API6 兩階段都有 checkpoint，鍵不綁日期；有自訂時間預算，時間到自己收手落盤。
  3. 失敗的組合記錄下來、下次重試；連續失敗太多次就放棄，不阻擋其他資料寫出。
  4. 母體 ＝ 官方現行清單 ∪ OHLCV 累積庫裡「曾經有成交的認購權證」。

第一輪實跑（2026-09-17，220 分鐘）後的修正：

  * API4 沒掃完不再擋住 API6。已經掃完的生命週期，它的候選就是完整的，
    沒有理由等其他 20 萬檔。現在預算前段給 API4、後段給 API6，每一輪都會有明細進庫。
  * 由舊到新處理。MoneyDJ 的保留底線是滾動的，最舊的資料每天都在消失，
    所以最舊的生命週期與候選最先抓。
  * 速率自動回升。主程式只在「冷卻等級從 >0 往下降」時才回升速率，
    等級一旦回到 0 就不再回升 —— 實測一次 9.3 秒的慢回應把速率從 8 砍到 4.6，
    之後近 3 小時都卡在 4.6。這裡每次落盤時，若期間沒有新的限流事件就緩慢調回起始速率。
  * OHLCV 生命週期合併。同一代號、間隔 14 天內的成交段落視為同一檔權證
    （跨年分檔、名稱格式差異都會把一檔權證拆成好幾段）。代號被回收時兩段之間一定有明顯空窗。
  * 連續失敗 BACKFILL_MAX_ATTEMPTS 輪就放棄。快速 500 是 MoneyDJ 確定沒有這筆資料，
    主程式每次已經內部重試 6 次，每一輪再重試只是浪費時間。
  * 每輪結束寫 last_run.json，workflow 據此決定要不要自動觸發下一段。

MoneyDJ 的呼叫、限流、退避全部重用主程式已驗證過的實作（api4_get_with_status、
_moneydj_candidate_to_item），這裡只負責排程、checkpoint 與母體。

用法：
    python warrant_backfill.py                      # 跑到時間預算用完或全部完成
    python warrant_backfill.py --max-minutes 240
    python warrant_backfill.py --status             # 只看進度，不打 MoneyDJ

依賴：warrant_backtest_nosheet_history.py、warrant_history_store.py（同資料夾）
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta

# ── 必須在 import 主程式之前設定：主程式在模組載入時就讀環境變數 ──
# 起始速率放低、上限封頂。實測 25 req/s 起跑會在幾千個請求後被限流，
# 一路退到冷卻等級 6，之後每次全體暫停 45 秒 —— 從一開始就穩定跑反而比較快。
os.environ.setdefault("MONEYDJ_RATE_START_PER_SECOND", "8")
os.environ.setdefault("MONEYDJ_RATE_MAX_PER_SECOND", "12")
os.environ.setdefault("GSHEET_RESULT_ENABLED", "0")
os.environ.setdefault("GSHEET_CACHE_ENABLED", "0")
os.environ.setdefault("SHEETLESS_MODE", "1")
os.environ.setdefault("RUN_MODE", "2")

import pandas as pd  # noqa: E402

import warrant_backtest_nosheet_history as M  # noqa: E402
from warrant_history_store import STORE_DIR, atomic_write_parquet  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError, ValueError):
        pass


# 實測 MoneyDJ 分點資料最早只到這一天（2026-09-16，多檔不同權證的 API6 序列同時從這天開始）。
MONEYDJ_FLOOR = os.getenv("BACKFILL_FLOOR_DATE", "2023/09/11")
MAX_ATTEMPTS = max(int(os.getenv("BACKFILL_MAX_ATTEMPTS", "3")), 1)
# API4 還沒掃完時，這一輪預算給 API4 的比例；剩下的給 API6。
API4_SHARE = min(max(float(os.getenv("BACKFILL_API4_SHARE", "0.6")), 0.1), 1.0)
# 同一代號兩段成交之間的空窗在這個天數內，視為同一檔權證。
LIFECYCLE_MERGE_GAP_DAYS = int(os.getenv("BACKFILL_LIFECYCLE_MERGE_GAP_DAYS", "14"))

BACKFILL_DIR = os.path.join(M.CACHE_DIR, "backfill")
API4_STATE_PATH = os.path.join(BACKFILL_DIR, "api4_state.parquet")
CANDIDATES_PATH = os.path.join(BACKFILL_DIR, "candidates.parquet")
API6_STATE_PATH = os.path.join(BACKFILL_DIR, "api6_state.parquet")
LAST_RUN_PATH = os.path.join(BACKFILL_DIR, "last_run.json")
ROWS_PART_GLOB = os.path.join(BACKFILL_DIR, "rows_part_*.parquet")
HISTORY_CACHE_PARQUET = f"{M.HISTORY_CACHE_PATH}.parquet"
OHLCV_DIR = os.path.join(STORE_DIR, "ohlcv")

# 認購＋牛證。與主程式 get_all_call_warrants 的範圍一致（排除認售／熊證）。
CALL_WARRANT_NAME_RE = re.compile(r"(?:購|牛)\d{1,2}$")

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_GAVE_UP = "gave_up"


def _fmt(dt):
    return dt.strftime("%Y/%m/%d")


def _read_parquet_or_empty(path):
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_parquet(path)


def _next_status(ok, previous_attempts):
    """成功就是 ok；失敗時累加嘗試次數，到上限就放棄，不再每輪重試。"""
    if ok:
        return STATUS_OK, previous_attempts + 1
    attempts = previous_attempts + 1
    return (STATUS_GAVE_UP if attempts >= MAX_ATTEMPTS else STATUS_FAILED), attempts


class RateNudger:
    """
    主程式只在冷卻等級從 >0 往下降時才回升速率；等級回到 0 之後就不會再升。
    每次落盤時檢查：這段期間沒有新的限流事件、冷卻等級是 0、速率低於起始值，
    就往回調一級。上限設在起始速率 —— 那是實測過安全的區間，不往上試探。
    """

    def __init__(self):
        self.last_hits = M._MONEYDJ_THROTTLE_HITS

    def __call__(self):
        hits = M._MONEYDJ_THROTTLE_HITS
        if (
            hits == self.last_hits
            and M._MONEYDJ_THROTTLE_LEVEL == 0
            and M._MONEYDJ_RATE_PER_SECOND < M.MONEYDJ_RATE_START_PER_SECOND
        ):
            M._moneydj_rate_recover()
        self.last_hits = hits


# ══════════════════════════════════════════════════════════════════════
# 母體：官方現行清單 ∪ OHLCV 裡曾經有成交的認購權證
# ══════════════════════════════════════════════════════════════════════

def _collect_ohlcv_segments(floor_dt, target_dt):
    """
    先把所有年份的成交段落收齊，再依代號合併。

    逐年處理會把跨年的同一檔權證切成兩段（12 月一段、1 月一段），
    第一輪實跑的 OHLCV 歷史生命週期 247,894 個明顯偏多，這是原因之一。
    """
    frames = []
    for path in sorted(glob.glob(os.path.join(OHLCV_DIR, "ohlcv_store_*.parquet"))):
        traded = pd.read_parquet(path, columns=["代號", "名稱", "日期"])
        names = traded["名稱"].astype(str).str.strip()
        traded = traded[names.str.contains(CALL_WARRANT_NAME_RE)].copy()
        if traded.empty:
            continue
        traded["_dt"] = pd.to_datetime(
            traded["日期"].astype(str).str.replace("/", "-"), errors="coerce"
        )
        traded = traded[
            traded["_dt"].notna()
            & (traded["_dt"] >= pd.Timestamp(floor_dt))
            & (traded["_dt"] <= pd.Timestamp(target_dt))
        ]
        frames.append(
            traded.groupby(["代號", "名稱"])["_dt"].agg(["min", "max"]).reset_index()
        )
    if not frames:
        return []

    spans = pd.concat(frames, ignore_index=True)
    spans["_code"] = spans["代號"].map(M._normalize_warrant_code_for_identity)
    spans = spans[spans["_code"] != ""].sort_values(["_code", "min"])

    merged = []
    gap = timedelta(days=LIFECYCLE_MERGE_GAP_DAYS)
    for code, group in spans.groupby("_code", sort=False):
        current = None
        for row in group.itertuples(index=False):
            start, end = row.min.to_pydatetime(), row.max.to_pydatetime()
            name = str(row.名稱).strip()
            if current and start <= current["end"] + gap:
                # 同一檔權證：延長區間，名稱取較新的那段
                if end > current["end"]:
                    current["end"] = end
                    current["name"] = name
                continue
            if current:
                merged.append(current)
            current = {"code": code, "name": name, "start": start, "end": end}
        if current:
            merged.append(current)
    return merged


def build_universe(warrants, floor_dt, target_dt):
    """
    回傳生命週期清單，每筆是一個「代號 × 查詢區間」，由舊到新排序。

    官方清單有正確的上市日／最後交易日與標的，優先採用；
    OHLCV 只補「官方清單沒有、且區間不重疊」的生命週期。
    """
    lifecycles = {}
    official_ranges = defaultdict(list)

    for record in warrants or []:
        code = M._normalize_warrant_code_for_identity(record.get("代號", ""))
        if not code:
            continue
        start = M.parse_date(record.get("上市日", "")) or floor_dt
        end = M.parse_date(record.get("最後交易日", "")) or target_dt
        start, end = max(start, floor_dt), min(end, target_dt)
        if start > end:
            continue
        key = (code, _fmt(start), _fmt(end))
        lifecycles[key] = {
            "代號": code,
            "名稱": str(record.get("名稱", "")).strip(),
            "標的股": str(record.get("標的股", "")).strip(),
            "標的名稱": str(record.get("標的名稱", "")).strip(),
            "起日": _fmt(start),
            "迄日": _fmt(end),
            "來源": "官方",
        }
        official_ranges[code].append((start, end))
    official_count = len(lifecycles)

    segments = _collect_ohlcv_segments(floor_dt, target_dt)
    if not segments:
        print("  ⚠️ 找不到 OHLCV 累積庫 —— 母體只有官方現行清單，"
              "2023–2025 年已到期的權證會漏掉。先跑 warrant_reference_harvest.py ohlcv。")

    ohlcv_added = 0
    unresolved_underlying = 0
    for seg in segments:
        code = seg["code"]
        # 第一筆成交前可能已經有人在買，往前留一週緩衝。
        start = max(seg["start"] - timedelta(days=7), floor_dt)
        end = min(seg["end"], target_dt)
        if any(s <= end and start <= e for s, e in official_ranges.get(code, [])):
            continue
        key = (code, _fmt(start), _fmt(end))
        if key in lifecycles:
            continue

        resolved = M.resolve_underlying_from_warrant_name(seg["name"]) or {}
        underlying = M.normalize_security_code_text(resolved.get("stock_code", ""))
        if not underlying:
            unresolved_underlying += 1
        lifecycles[key] = {
            "代號": code,
            "名稱": seg["name"],
            "標的股": underlying,
            "標的名稱": str(resolved.get("stock_name", "")).strip(),
            "起日": _fmt(start),
            "迄日": _fmt(end),
            "來源": "OHLCV",
        }
        ohlcv_added += 1

    universe = sorted(lifecycles.values(), key=lambda r: (r["起日"], r["代號"]))
    print(f"  ✅ 回補母體：官方 {official_count:,} ＋ OHLCV 歷史 {ohlcv_added:,}"
          f" ＝ {len(universe):,} 個生命週期（已合併同代號連續段落）"
          f"｜查詢區間已裁到 {_fmt(floor_dt)} 之後｜由舊到新處理")
    if unresolved_underlying:
        print(f"  ℹ️ OHLCV 歷史權證中 {unresolved_underlying:,} 檔無法由名稱解析標的"
              "（仍會抓籌碼，標的欄位留空）")
    return universe


# ══════════════════════════════════════════════════════════════════════
# 排程：有上限的提交 ＋ 時間預算 ＋ 定期落盤
# ══════════════════════════════════════════════════════════════════════

def run_bounded(items, worker, workers, on_result, deadline, flush_every, flush):
    """
    不一次把幾萬個 future 全部丟進 executor：那樣時間到了也收不回來。
    只保持 2×workers 個在跑，每完成一個檢查一次時間，時間到就停止補新的，
    等手上這批做完、落盤，然後正常返回 —— 讓後面的合併與上傳步驟有時間跑。
    """
    source = iter(items)
    completed = 0
    stopped_by_deadline = False

    def safe(item):
        try:
            return item, worker(item), None
        except Exception as exc:
            return item, None, exc

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = set()

        def refill():
            while len(pending) < workers * 2:
                try:
                    item = next(source)
                except StopIteration:
                    return
                pending.add(executor.submit(safe, item))

        if time.monotonic() < deadline:
            refill()
        else:
            stopped_by_deadline = True
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                pending.discard(future)
                on_result(*future.result())
                completed += 1
                if completed % flush_every == 0:
                    flush()
            if not stopped_by_deadline and time.monotonic() >= deadline:
                stopped_by_deadline = True
                print(f"  ⏰ 這個階段的時間預算用完，停止派發新工作，"
                      f"等手上 {len(pending)} 個完成後落盤")
            if not stopped_by_deadline:
                refill()

    flush()
    return completed, stopped_by_deadline


# ══════════════════════════════════════════════════════════════════════
# Phase 1：API4 找出每個生命週期有哪些追蹤分點
# ══════════════════════════════════════════════════════════════════════

def load_api4_state():
    state = {}
    df = _read_parquet_or_empty(API4_STATE_PATH)
    for row in df.to_dict("records"):
        row.setdefault("嘗試次數", 1)
        state[(row["代號"], row["起日"], row["迄日"])] = row
    return state


def load_candidates():
    candidates = {}
    df = _read_parquet_or_empty(CANDIDATES_PATH)
    for row in df.to_dict("records"):
        candidates[(row["代號"], row["起日"], row["迄日"], row["券商代號"])] = row
    return candidates


def api4_todo(universe, state):
    return [
        rec for rec in universe
        if state.get((rec["代號"], rec["起日"], rec["迄日"]), {}).get("狀態")
        not in (STATUS_OK, STATUS_GAVE_UP)
    ]


def phase_api4(universe, broker_map, workers, deadline):
    code_map = {
        M.normalize_broker_code_for_compare(code): (label, name, code)
        for label, (name, code) in broker_map.items()
    }
    state = load_api4_state()
    candidates = load_candidates()
    todo = api4_todo(universe, state)

    print(f"\n【Phase 1】API4 分點預篩｜已完成 {len(universe) - len(todo):,}"
          f"／待做 {len(todo):,}｜workers={workers}")
    if not todo:
        return candidates, 0

    def worker(rec):
        rows, ok = M.api4_get_with_status(rec["代號"], rec["起日"], rec["迄日"])
        found = []
        if ok:
            for row in rows:
                info = code_map.get(M.normalize_broker_code_for_compare(row.get("V2", "")))
                if not info:
                    continue
                label, broker_name, canonical = info
                found.append({
                    "代號": rec["代號"], "名稱": rec["名稱"],
                    "標的股": rec["標的股"], "標的名稱": rec["標的名稱"],
                    "分點": label, "分點名稱": broker_name,
                    "券商代號": canonical,
                    # API5／API6 的券商代號大小寫敏感（9A9g≠9A9G），查詢要用原始值。
                    "API券商代號": str(row.get("V2", "")).strip() or canonical,
                    "起日": rec["起日"], "迄日": rec["迄日"],
                })
        return ok, found

    stamp = datetime.today().strftime("%Y/%m/%d %H:%M")
    progress = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_GAVE_UP: 0}
    nudge = RateNudger()

    def on_result(rec, result, exc):
        ok, found = result if result else (False, [])
        key = (rec["代號"], rec["起日"], rec["迄日"])
        status, attempts = _next_status(
            ok and exc is None, int(state.get(key, {}).get("嘗試次數", 0) or 0)
        )
        progress[status] += 1
        state[key] = {
            "代號": rec["代號"], "起日": rec["起日"], "迄日": rec["迄日"],
            "狀態": status, "嘗試次數": attempts, "候選數": len(found), "時間": stamp,
        }
        for cand in found:
            candidates[(cand["代號"], cand["起日"], cand["迄日"], cand["券商代號"])] = cand

    def flush():
        atomic_write_parquet(pd.DataFrame(list(state.values())), API4_STATE_PATH)
        if candidates:
            atomic_write_parquet(pd.DataFrame(list(candidates.values())), CANDIDATES_PATH)
        nudge()
        done = sum(progress.values())
        print(f"  [{done:,}/{len(todo):,}] API4｜成功 {progress[STATUS_OK]:,}"
              f"｜失敗待重試 {progress[STATUS_FAILED]:,}｜放棄 {progress[STATUS_GAVE_UP]:,}"
              f"｜累計候選 {len(candidates):,} 組"
              f"｜速率 {M._MONEYDJ_RATE_PER_SECOND:.1f} req/s")

    completed, _ = run_bounded(
        todo, worker, workers, on_result, deadline, flush_every=500, flush=flush
    )
    return candidates, completed


# ══════════════════════════════════════════════════════════════════════
# Phase 2：API6 取回每組 (權證 × 分點) 在生命週期內的逐日明細
# ══════════════════════════════════════════════════════════════════════

def load_api6_state():
    state = {}
    df = _read_parquet_or_empty(API6_STATE_PATH)
    for row in df.to_dict("records"):
        row.setdefault("嘗試次數", 1)
        state[(row["代號"], row["起日"], row["迄日"], row["券商代號"])] = row
    return state


def api6_todo(candidates, state):
    todo = [
        cand for key, cand in candidates.items()
        if state.get(key, {}).get("狀態") not in (STATUS_OK, STATUS_GAVE_UP)
    ]
    # 由舊到新：最舊的資料最接近 MoneyDJ 的滾動底線，最先消失。
    todo.sort(key=lambda c: (c["起日"], c["代號"], c["券商代號"]))
    return todo


def phase_api6(candidates, target_key, workers, deadline):
    state = load_api6_state()
    todo = api6_todo(candidates, state)
    print(f"\n【Phase 2】API6 逐日明細｜已完成 {len(candidates) - len(todo):,}"
          f"／待做 {len(todo):,}｜workers={workers}")
    if not todo:
        return 0
    if time.monotonic() >= deadline:
        print("  ⏭ 沒有剩餘時間，下一輪再抓")
        return 0

    part_counter = {"n": len(glob.glob(ROWS_PART_GLOB))}
    buffer = []
    stamp = datetime.today().strftime("%Y/%m/%d %H:%M")
    progress = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_GAVE_UP: 0, "rows": 0}
    nudge = RateNudger()

    def worker(cand):
        candidate_tuple = (
            cand["代號"], cand["名稱"], cand["標的股"], cand["標的名稱"],
            cand["分點"], cand["分點名稱"], cand["券商代號"], cand["API券商代號"],
        )
        return M._moneydj_candidate_to_item(
            candidate_tuple, target_key, date_range=(cand["起日"], cand["迄日"])
        )

    def on_result(cand, result, exc):
        item, ok = result if result else (None, False)
        key = (cand["代號"], cand["起日"], cand["迄日"], cand["券商代號"])
        # item 為 None 但 ok=True 代表「查詢成功、區間內沒有交易」。
        # repair 把這種情況當成失敗，是它會整輪作廢的原因之一；這裡視為完成。
        status, attempts = _next_status(
            ok and exc is None, int(state.get(key, {}).get("嘗試次數", 0) or 0)
        )
        progress[status] += 1
        row_count = 0
        if item is not None:
            row_count = len(item.get("df", []))
            buffer.append(item)
        progress["rows"] += row_count
        state[key] = {
            "代號": cand["代號"], "起日": cand["起日"], "迄日": cand["迄日"],
            "券商代號": cand["券商代號"], "狀態": status, "嘗試次數": attempts,
            "列數": row_count, "時間": stamp,
        }

    def flush():
        # 明細用「分片檔」只增不改：每次落盤寫一個新檔，
        # 不必把越來越大的明細整份重寫一遍。
        if buffer:
            part_counter["n"] += 1
            part_path = os.path.join(BACKFILL_DIR, f"rows_part_{part_counter['n']:05d}.parquet")
            atomic_write_parquet(M._moneydj_items_to_history_df(list(buffer)), part_path)
            buffer.clear()
        atomic_write_parquet(pd.DataFrame(list(state.values())), API6_STATE_PATH)
        nudge()
        done = progress[STATUS_OK] + progress[STATUS_FAILED] + progress[STATUS_GAVE_UP]
        print(f"  [{done:,}/{len(todo):,}] API6｜成功 {progress[STATUS_OK]:,}"
              f"｜失敗待重試 {progress[STATUS_FAILED]:,}｜放棄 {progress[STATUS_GAVE_UP]:,}"
              f"｜本輪取回 {progress['rows']:,} 列"
              f"｜速率 {M._MONEYDJ_RATE_PER_SECOND:.1f} req/s")

    completed, _ = run_bounded(
        todo, worker, workers, on_result, deadline, flush_every=1000, flush=flush
    )
    return completed


# ══════════════════════════════════════════════════════════════════════
# 輸出：把所有分片併進主程式的歷史快取，讓 warrant_history_store merge 接手
# ══════════════════════════════════════════════════════════════════════

def publish_to_history_cache():
    parts = sorted(glob.glob(ROWS_PART_GLOB))
    if not parts:
        print("\n  ℹ️ 還沒有任何明細分片可以輸出")
        return 0

    frames = [pd.read_parquet(p) for p in parts]
    if os.path.exists(HISTORY_CACHE_PARQUET):
        frames.insert(0, pd.read_parquet(HISTORY_CACHE_PARQUET))
    combined = pd.concat(frames, ignore_index=True, sort=False)
    before = len(combined)
    combined = combined.drop_duplicates(
        subset=["權證代號", "券商代號", "日期"], keep="last"
    ).reset_index(drop=True)
    atomic_write_parquet(combined, HISTORY_CACHE_PARQUET)
    print(f"\n💾 已輸出到歷史快取：{HISTORY_CACHE_PARQUET}"
          f"｜{len(combined):,} 列（去重前 {before:,}）")
    return len(combined)


def _count(state, status):
    return sum(1 for row in state.values() if row.get("狀態") == status)


def summarize_progress(universe=None):
    api4_state = load_api4_state()
    candidates = load_candidates()
    api6_state = load_api6_state()

    info = {
        "api4_ok": _count(api4_state, STATUS_OK),
        "api4_failed": _count(api4_state, STATUS_FAILED),
        "api4_gave_up": _count(api4_state, STATUS_GAVE_UP),
        "candidates": len(candidates),
        "api6_ok": _count(api6_state, STATUS_OK),
        "api6_failed": _count(api6_state, STATUS_FAILED),
        "api6_gave_up": _count(api6_state, STATUS_GAVE_UP),
        "api6_rows": int(sum(int(r.get("列數", 0) or 0) for r in api6_state.values())),
        "parts": len(glob.glob(ROWS_PART_GLOB)),
    }
    if universe is not None:
        info["universe"] = len(universe)
        info["api4_remaining"] = len(api4_todo(universe, api4_state))
        info["api6_remaining"] = len(api6_todo(candidates, api6_state))
        info["complete"] = info["api4_remaining"] == 0 and info["api6_remaining"] == 0
    return info


def print_status(info):
    print(f"\n{'=' * 74}")
    print("📋 回補進度")
    print("-" * 74)
    universe = info.get("universe")
    universe_text = f"／母體 {universe:,}" if universe else ""
    print(f"  API4 生命週期：成功 {info['api4_ok']:,}｜失敗待重試 {info['api4_failed']:,}"
          f"｜放棄 {info['api4_gave_up']:,}{universe_text}")
    if "api4_remaining" in info:
        print(f"    剩餘 {info['api4_remaining']:,}")
    print(f"  候選 (權證×分點)：{info['candidates']:,} 組")
    print(f"  API6 明細：成功 {info['api6_ok']:,}｜失敗待重試 {info['api6_failed']:,}"
          f"｜放棄 {info['api6_gave_up']:,}｜累計 {info['api6_rows']:,} 列")
    if "api6_remaining" in info:
        print(f"    剩餘 {info['api6_remaining']:,}（API4 還沒掃完的話，候選還會再增加）")
    print(f"  明細分片：{info['parts']} 個")

    if "complete" not in info:
        print("\n  ℹ️ 是否全部完成，以「深度回補」步驟最後印出的判定為準。")
    elif info["complete"]:
        print("\n  ✅ 回補完成。之後改用 daily 模式每天增量即可。")
    else:
        print("\n  ⏳ 尚未完成 —— 下一段會從這裡接續。")
    print("=" * 74)


def main():
    parser = argparse.ArgumentParser(description="權證分點歷史深度回補")
    parser.add_argument("--max-minutes", type=float,
                        default=float(os.getenv("BACKFILL_MAX_MINUTES", "240")),
                        help="這次最多跑幾分鐘（要留時間給後面的合併與上傳）")
    parser.add_argument("--api4-workers", type=int,
                        default=int(os.getenv("BACKFILL_API4_WORKERS", "12")))
    parser.add_argument("--api6-workers", type=int,
                        default=int(os.getenv("BACKFILL_API6_WORKERS", "16")))
    parser.add_argument("--status", action="store_true", help="只看進度")
    args = parser.parse_args()

    os.makedirs(BACKFILL_DIR, exist_ok=True)
    if args.status:
        print_status(summarize_progress())
        return 0

    started = time.monotonic()
    deadline = started + args.max_minutes * 60

    print("=" * 74)
    print("🗄️ 權證分點歷史深度回補")
    print(f"   時間預算 {args.max_minutes:.0f} 分鐘｜MoneyDJ 底線 {MONEYDJ_FLOOR}"
          f"｜起始速率 {M.MONEYDJ_RATE_START_PER_SECOND:.0f}"
          f"／上限 {M.MONEYDJ_RATE_MAX_PER_SECOND:.0f} req/s"
          f"｜連續失敗 {MAX_ATTEMPTS} 輪放棄")
    print("=" * 74)

    print("\n【Step 1】權證母體（沿用主程式）")
    warrants = M.get_all_call_warrants()
    if not warrants:
        print("  ⛔ 權證清單無法取得")
        return 1

    print("\n【Step 2】分點代號（沿用主程式）")
    broker_map = M.filter_broker_map_for_active_targets(M.find_broker_codes_moneydj(warrants))
    if not broker_map:
        print("  ⛔ 分點代號無法取得")
        return 1

    target_dt = M.resolve_latest_trading_date_on_or_before(datetime.today())
    target_dt = target_dt if isinstance(target_dt, datetime) else M.parse_date(target_dt)
    target_dt = target_dt or datetime.today()
    floor_dt = datetime.strptime(MONEYDJ_FLOOR, "%Y/%m/%d")

    print(f"\n【Step 3】回補母體｜{_fmt(floor_dt)} ～ {_fmt(target_dt)}")
    universe = build_universe(warrants, floor_dt, target_dt)

    # API4 沒掃完時只給它前段預算，後段一定留給 API6：
    # 已經掃完的生命週期，候選就是完整的，沒理由等全部掃完才開始抓明細。
    remaining_seconds = max(deadline - time.monotonic(), 0)
    api4_deadline = time.monotonic() + remaining_seconds * API4_SHARE
    candidates, api4_done = phase_api4(
        universe, broker_map, args.api4_workers, api4_deadline
    )
    api6_done = phase_api6(candidates, _fmt(target_dt), args.api6_workers, deadline)

    publish_to_history_cache()
    M.print_moneydj_health_report()

    info = summarize_progress(universe)
    print_status(info)

    info.update({
        "api4_done_this_run": api4_done,
        "api6_done_this_run": api6_done,
        "finished_at": datetime.today().strftime("%Y/%m/%d %H:%M:%S"),
    })
    with open(LAST_RUN_PATH, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)

    elapsed = (time.monotonic() - started) / 60
    print(f"\n⏱️ 本次耗時 {elapsed:.1f} 分鐘")
    # 一律正常結束：沒跑完不是錯誤，是預期中的分段。
    return 0


if __name__ == "__main__":
    sys.exit(main())
