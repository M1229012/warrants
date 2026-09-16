"""
權證分點歷史深度回補（可分段、可續跑、不會整輪作廢）
==========================================================
為什麼不直接用主程式的 repair：

  repair 是為「200 天、一次跑完」設計的，拿來回補 3 年會撞上四個問題（2026-09-17 實測）：

  1. 查詢太重被限流。掃描區間公式是 int(保留天數 × 1.6) + 20，
     800 天 → 每個 API4 請求都查 1,300 天（2023/02/24～今天），而且所有權證共用同一段。
     一檔上個月才上市的權證也被拿去查三年半。MoneyDJ 回「請稍候」，
     掃到第 5,000 檔就降到冷卻等級 6/6、2 req/s。

  2. 沒有續跑。檔頭 AUDIT8 註解寫了「候選快取」「API5 跨次續跑」，
     但 MONEYDJ_CANDIDATE_CACHE_ENABLED／MONEYDJ_API5_CHECKPOINT_ENABLED
     只有定義、整支程式沒有任何地方呼叫。撞到 6 小時上限就全部歸零。

  3. fail-closed。只要有一組 API5 最終失敗，整輪丟棄。
     這對 200 天是正確的保護，對要分好幾次跑的 3 年回補是致命的。

  4. 母體不完整。get_all_call_warrants() ＝官方「現行」清單 ∪ 本機快取。
     第一次回補沒有快取，2023–2025 年已到期、不在現行清單的權證根本不會被掃。

這支程式的對應做法：

  1. 每檔權證只查它自己的生命週期 ∩ [2023/09/11, 今天]。
     2023/09/11 是實測的 MoneyDJ 保留底線，更早的查了也是空的。
  2. API4 與 API6 兩階段都有 checkpoint，而且鍵不綁日期。
     另外有自訂的時間預算，時間到就收手並落盤，不讓 GitHub 在 350 分鐘時硬砍。
  3. 失敗的組合記錄下來、下次重試，但不阻擋已抓到的資料寫出去。
     累積庫是只進不出的聯集，部分資料寫進去不會污染任何東西。
  4. 母體 ＝ 官方現行清單 ∪ OHLCV 累積庫裡「曾經有成交的認購權證」。
     TWSE／TPEx 的每日行情記錄了每天所有有成交的權證與名稱，那才是完整的歷史母體。

  逐組明細改走 API6（指定區間），不用 API5 的「最近 800 筆」。
  API6 只回生命週期內的列，請求更輕，也不會因為筆數上限截斷。

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

BACKFILL_DIR = os.path.join(M.CACHE_DIR, "backfill")
API4_STATE_PATH = os.path.join(BACKFILL_DIR, "api4_state.parquet")
CANDIDATES_PATH = os.path.join(BACKFILL_DIR, "candidates.parquet")
API6_STATE_PATH = os.path.join(BACKFILL_DIR, "api6_state.parquet")
ROWS_PART_GLOB = os.path.join(BACKFILL_DIR, "rows_part_*.parquet")
HISTORY_CACHE_PARQUET = f"{M.HISTORY_CACHE_PATH}.parquet"
OHLCV_DIR = os.path.join(STORE_DIR, "ohlcv")

# 認購＋牛證。與主程式 get_all_call_warrants 的範圍一致（排除認售／熊證）。
CALL_WARRANT_NAME_RE = re.compile(r"(?:購|牛)\d{1,2}$")


def _fmt(dt):
    return dt.strftime("%Y/%m/%d")


def _read_parquet_or_empty(path, columns=None):
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns or [])
    return pd.read_parquet(path)


# ══════════════════════════════════════════════════════════════════════
# 母體：官方現行清單 ∪ OHLCV 裡曾經有成交的認購權證
# ══════════════════════════════════════════════════════════════════════

def build_universe(warrants, floor_dt, target_dt):
    """
    回傳生命週期清單，每筆是一個「代號 × 查詢區間」。

    官方清單有正確的上市日／最後交易日與標的，優先採用；
    OHLCV 只補「官方清單沒有、且區間不重疊」的生命週期 ——
    同一代號在不同年份可能是不同權證（代號會回收），所以用（代號, 名稱）分組，
    不同名稱就是不同的生命週期。
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

    ohlcv_added = 0
    unresolved_underlying = 0
    parts = sorted(glob.glob(os.path.join(OHLCV_DIR, "ohlcv_store_*.parquet")))
    if not parts:
        print("  ⚠️ 找不到 OHLCV 累積庫 —— 母體只有官方現行清單，"
              "2023–2025 年已到期的權證會漏掉。先跑 warrant_reference_harvest.py ohlcv。")
    for path in parts:
        traded = pd.read_parquet(path, columns=["代號", "名稱", "日期"])
        traded = traded[
            traded["名稱"].astype(str).str.strip().str.contains(CALL_WARRANT_NAME_RE)
        ]
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
        spans = traded.groupby(["代號", "名稱"])["_dt"].agg(["min", "max"]).reset_index()

        for row in spans.itertuples(index=False):
            code = M._normalize_warrant_code_for_identity(row.代號)
            if not code:
                continue
            # 第一筆成交前可能已經有人在買，往前留一週緩衝。
            start = max(row.min.to_pydatetime() - timedelta(days=7), floor_dt)
            end = min(row.max.to_pydatetime(), target_dt)
            if any(s <= end and start <= e for s, e in official_ranges.get(code, [])):
                continue
            key = (code, _fmt(start), _fmt(end))
            if key in lifecycles:
                continue

            name = str(row.名稱).strip()
            resolved = M.resolve_underlying_from_warrant_name(name) or {}
            underlying = M.normalize_security_code_text(resolved.get("stock_code", ""))
            if not underlying:
                unresolved_underlying += 1
            lifecycles[key] = {
                "代號": code,
                "名稱": name,
                "標的股": underlying,
                "標的名稱": str(resolved.get("stock_name", "")).strip(),
                "起日": _fmt(start),
                "迄日": _fmt(end),
                "來源": "OHLCV",
            }
            ohlcv_added += 1

    print(f"  ✅ 回補母體：官方 {official_count:,} ＋ OHLCV 歷史 {ohlcv_added:,}"
          f" ＝ {len(lifecycles):,} 個生命週期"
          f"｜查詢區間已裁到 {_fmt(floor_dt)} 之後")
    if unresolved_underlying:
        print(f"  ℹ️ OHLCV 歷史權證中 {unresolved_underlying:,} 檔無法由名稱解析標的"
              "（仍會抓籌碼，標的欄位留空）")
    return list(lifecycles.values())


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

        refill()
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
                print(f"  ⏰ 時間預算用完，停止派發新工作，等手上 {len(pending)} 個完成後落盤")
            if not stopped_by_deadline:
                refill()

    flush()
    return completed, stopped_by_deadline


# ══════════════════════════════════════════════════════════════════════
# Phase 1：API4 找出每個生命週期有哪些追蹤分點
# ══════════════════════════════════════════════════════════════════════

def phase_api4(universe, broker_map, workers, deadline):
    code_map = {
        M.normalize_broker_code_for_compare(code): (label, name, code)
        for label, (name, code) in broker_map.items()
    }

    state_df = _read_parquet_or_empty(API4_STATE_PATH)
    state = {}
    if not state_df.empty:
        for row in state_df.itertuples(index=False):
            state[(row.代號, row.起日, row.迄日)] = row._asdict()

    candidates_df = _read_parquet_or_empty(CANDIDATES_PATH)
    candidates = {}
    if not candidates_df.empty:
        for row in candidates_df.to_dict("records"):
            candidates[(row["代號"], row["起日"], row["迄日"], row["券商代號"])] = row

    todo = [
        rec for rec in universe
        if state.get((rec["代號"], rec["起日"], rec["迄日"]), {}).get("狀態") != "ok"
    ]
    print(f"\n【Phase 1】API4 分點預篩｜已完成 {len(universe) - len(todo):,}"
          f"／待做 {len(todo):,}｜workers={workers}")
    if not todo:
        return candidates, 0, False

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
    progress = {"ok": 0, "failed": 0}

    def on_result(rec, result, exc):
        ok, found = result if result else (False, [])
        status = "ok" if ok and exc is None else "failed"
        progress[status] += 1
        state[(rec["代號"], rec["起日"], rec["迄日"])] = {
            "代號": rec["代號"], "起日": rec["起日"], "迄日": rec["迄日"],
            "狀態": status, "候選數": len(found), "時間": stamp,
        }
        for cand in found:
            candidates[(cand["代號"], cand["起日"], cand["迄日"], cand["券商代號"])] = cand

    def flush():
        atomic_write_parquet(pd.DataFrame(list(state.values())), API4_STATE_PATH)
        if candidates:
            atomic_write_parquet(pd.DataFrame(list(candidates.values())), CANDIDATES_PATH)
        done = progress["ok"] + progress["failed"]
        print(f"  [{done:,}/{len(todo):,}] API4｜成功 {progress['ok']:,}"
              f"｜失敗 {progress['failed']:,}｜累計候選 {len(candidates):,} 組"
              f"｜目前速率 {M._MONEYDJ_RATE_PER_SECOND:.1f} req/s")

    completed, stopped = run_bounded(
        todo, worker, workers, on_result, deadline, flush_every=500, flush=flush
    )
    return candidates, completed, stopped


# ══════════════════════════════════════════════════════════════════════
# Phase 2：API6 取回每組 (權證 × 分點) 在生命週期內的逐日明細
# ══════════════════════════════════════════════════════════════════════

def phase_api6(candidates, target_key, workers, deadline):
    state_df = _read_parquet_or_empty(API6_STATE_PATH)
    state = {}
    if not state_df.empty:
        for row in state_df.to_dict("records"):
            state[(row["代號"], row["起日"], row["迄日"], row["券商代號"])] = row

    todo = [c for k, c in candidates.items() if state.get(k, {}).get("狀態") != "ok"]
    print(f"\n【Phase 2】API6 逐日明細｜已完成 {len(candidates) - len(todo):,}"
          f"／待做 {len(todo):,}｜workers={workers}")
    if not todo:
        return 0, False

    existing_parts = sorted(glob.glob(ROWS_PART_GLOB))
    part_counter = {"n": len(existing_parts)}
    buffer = []
    stamp = datetime.today().strftime("%Y/%m/%d %H:%M")
    progress = {"ok": 0, "failed": 0, "rows": 0}

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
        # item 為 None 但 ok=True 代表「查詢成功、區間內沒有交易」。
        # repair 把這種情況當成失敗，是它會整輪作廢的原因之一；這裡視為完成。
        status = "ok" if ok and exc is None else "failed"
        progress[status] += 1
        row_count = 0
        if item is not None:
            row_count = len(item.get("df", []))
            buffer.append(item)
        progress["rows"] += row_count
        key = (cand["代號"], cand["起日"], cand["迄日"], cand["券商代號"])
        state[key] = {
            "代號": cand["代號"], "起日": cand["起日"], "迄日": cand["迄日"],
            "券商代號": cand["券商代號"], "狀態": status, "列數": row_count, "時間": stamp,
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
        done = progress["ok"] + progress["failed"]
        print(f"  [{done:,}/{len(todo):,}] API6｜成功 {progress['ok']:,}"
              f"｜失敗 {progress['failed']:,}｜本次取回 {progress['rows']:,} 列"
              f"｜目前速率 {M._MONEYDJ_RATE_PER_SECOND:.1f} req/s")

    return run_bounded(
        todo, worker, workers, on_result, deadline, flush_every=1000, flush=flush
    )


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


def print_status(universe_size=None):
    api4 = _read_parquet_or_empty(API4_STATE_PATH)
    cands = _read_parquet_or_empty(CANDIDATES_PATH)
    api6 = _read_parquet_or_empty(API6_STATE_PATH)
    parts = glob.glob(ROWS_PART_GLOB)

    print(f"\n{'=' * 74}")
    print("📋 回補進度")
    print("-" * 74)
    if not api4.empty:
        counts = api4["狀態"].value_counts().to_dict()
        total = f"／母體 {universe_size:,}" if universe_size else ""
        print(f"  API4 生命週期：成功 {counts.get('ok', 0):,}"
              f"｜失敗待重試 {counts.get('failed', 0):,}{total}")
    else:
        print("  API4：尚未開始")
    print(f"  候選 (權證×分點)：{len(cands):,} 組")
    if not api6.empty:
        counts = api6["狀態"].value_counts().to_dict()
        print(f"  API6 明細：成功 {counts.get('ok', 0):,}"
              f"｜失敗待重試 {counts.get('failed', 0):,}"
              f"｜未開始 {max(len(cands) - len(api6), 0):,}"
              f"｜累計 {int(api6['列數'].sum()):,} 列")
    else:
        print("  API6：尚未開始")
    print(f"  明細分片：{len(parts)} 個")

    if universe_size is None:
        # --status 單獨執行時不建母體（那要打官方 API），無法判斷是否全部完成。
        # 與其猜一個「尚未完成」誤導人，不如明講以哪裡為準。
        print("\n  ℹ️ 是否全部完成，以「深度回補」步驟最後印出的判定為準。")
        print("=" * 74)
        return

    finished = (
        not api4.empty
        and (api4["狀態"] == "ok").sum() >= universe_size
        and not api6.empty
        and len(api6) >= len(cands)
        and (api6["狀態"] == "ok").all()
    )
    if finished:
        print("\n  ✅ 回補完成。之後改用 daily 模式每天增量即可。")
    else:
        print("\n  ⏳ 尚未完成 —— 再觸發一次 backfill 會從這裡接續。")
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
        print_status()
        return 0

    started = time.monotonic()
    deadline = started + args.max_minutes * 60

    print("=" * 74)
    print("🗄️ 權證分點歷史深度回補")
    print(f"   時間預算 {args.max_minutes:.0f} 分鐘｜MoneyDJ 底線 {MONEYDJ_FLOOR}"
          f"｜起始速率 {M.MONEYDJ_RATE_START_PER_SECOND:.0f}"
          f"／上限 {M.MONEYDJ_RATE_MAX_PER_SECOND:.0f} req/s")
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
    target_dt = M.parse_date(target_dt) if not isinstance(target_dt, datetime) else target_dt
    target_dt = target_dt or datetime.today()
    floor_dt = datetime.strptime(MONEYDJ_FLOOR, "%Y/%m/%d")

    print(f"\n【Step 3】回補母體｜{_fmt(floor_dt)} ～ {_fmt(target_dt)}")
    universe = build_universe(warrants, floor_dt, target_dt)

    candidates, _, stopped = phase_api4(universe, broker_map, args.api4_workers, deadline)

    if not stopped:
        phase_api6(candidates, _fmt(target_dt), args.api6_workers, deadline)
    else:
        print("\n  ⏭ API4 還沒掃完，這次不進 API6（候選不完整，先把預篩做完）")

    publish_to_history_cache()
    M.print_moneydj_health_report()
    print_status(universe_size=len(universe))

    elapsed = (time.monotonic() - started) / 60
    print(f"\n⏱️ 本次耗時 {elapsed:.1f} 分鐘")
    # 一律正常結束：沒跑完不是錯誤，是預期中的分段。
    # 回傳非 0 會讓 workflow 把後面的合併與上傳當成失敗步驟。
    return 0


if __name__ == "__main__":
    sys.exit(main())
