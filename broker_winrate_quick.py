# -*- coding: utf-8 -*-
"""
分點勝率速查｜任何分點（含還沒加進回測清單的新分點）的 全部＋A/B/C/D/E 勝率
=====================================================

部署重點：
1. 不重寫任何計算：用 importlib 載入同資料夾最新的 warrant_backtest_moneydj*.py，
   直接呼叫它的 build_amount_class_events／FIFO／collect_stat_records／make_summary_map，
   數字定義與 Google Sheet「勝率統計」工作表完全相同（含 repair 的滿 60 日市價估值）。
2. 資料來源依序 fallback：
   - 回測歷史快取裡已經有這個券商代號 → 直接算，秒出
   - 本工具自己的快取 warrant_cache/quick_winrate/<代號>_<統計日>.csv → 同一天重跑秒出
   - 都沒有 → MoneyDJ API4 掃窗口內所有權證找候選，再 API5 取回逐日明細
3. 唯讀：不寫回測的歷史快取、分點代號快取、Google Sheet。
   只會寫本工具自己的快取與結果 CSV（以及補抓到的權證價格，那本來就是共用價格快取）。

用法（環境變數，多個分點用逗號分隔；同一次掃描可以一起查，API4 成本不變）：
  QUICK_BROKERS="元大-台南=9851,凱基-松山"
  - 「名稱=券商代號」：代號大小寫敏感（9A9g 永豐金內湖 ≠ 9A9G 永豐金天母），知道就填
  - 只給名稱：先用近 5 日 300 檔權證探索代號，找不到會請你補代號
  - 已在回測清單的分點可直接給標籤或代號，例如 元大南屯、9853
  也可以直接帶參數：python broker_winrate_quick.py 元大-台南=9851 凱基-松山

其他 env：
- QUICK_DAYS：回溯交易日數，預設沿用回測的 HISTORY_RETENTION_TRADING_DAYS（200）
- QUICK_MARK_TO_MARKET：滿 60 日未出清按權證市價估值，預設 1（與 repair 勝率一致）
- QUICK_FORCE_REFETCH：忽略所有快取重抓，預設 0
- QUICK_BACKTEST_PATH：回測程式路徑；空白時在本檔資料夾找最新的 warrant_backtest_moneydj*.py

必要套件：與回測程式相同
"""

import glob
import importlib.util
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd


# ============================================================
# 設定區
# ============================================================

_HERE = os.path.dirname(os.path.abspath(__file__))

QUICK_BROKERS = os.getenv("QUICK_BROKERS", "").strip() or ",".join(sys.argv[1:])
QUICK_DAYS = int(os.getenv("QUICK_DAYS", "0"))  # 0＝沿用回測設定
QUICK_MARK_TO_MARKET = os.getenv("QUICK_MARK_TO_MARKET", "1").strip().lower() not in ("0", "false", "no", "off")
QUICK_FORCE_REFETCH = os.getenv("QUICK_FORCE_REFETCH", "0").strip().lower() in ("1", "true", "yes", "on")
QUICK_BACKTEST_PATH = os.getenv("QUICK_BACKTEST_PATH", "").strip()

# MoneyDJ 分點資料實測只回溯到 2023/09/11，再往前掃只是浪費請求。
MONEYDJ_HISTORY_FLOOR = datetime(2023, 9, 11)


# ============================================================
# 基本工具
# ============================================================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _fmt_pct(value, signed=False):
    if value is None:
        return "-"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


# ============================================================
# 資料層
# ============================================================

def load_backtest_module():
    """用 importlib 載入回測程式（檔名含空白與括號，不能一般 import）。

    回測程式刻意維持單一檔案，模組層有大量 global 快取；整支當成一個模組載入，
    它的函式讀寫的都是同一份 global。模組層不會在 import 時連網或跑主流程。
    """
    path = QUICK_BACKTEST_PATH
    if not path:
        candidates = glob.glob(os.path.join(_HERE, "warrant_backtest_moneydj*.py"))
        if not candidates:
            raise RuntimeError("找不到 warrant_backtest_moneydj*.py，請設定 QUICK_BACKTEST_PATH")
        path = max(candidates, key=os.path.getmtime)
    spec = importlib.util.spec_from_file_location("warrant_backtest_for_quick", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    log(f"📦 載入回測模組：{os.path.basename(path)}｜{getattr(module, 'PROGRAM_BUILD_ID', '-')}")
    return module


def parse_broker_specs(bt, raw):
    """「名稱[=代號]」→ {標籤: (正則, 顯示名稱, 代號或空字串)}。"""
    specs = {}
    for token in [x.strip() for x in re.split(r"[,;；、\s]+", raw or "") if x.strip()]:
        name, _, code = token.partition("=")
        name, code = name.strip(), code.strip()
        # 已在回測清單內：直接沿用原本的正則與代號，避免兩邊定義不一致。
        known = next(
            (label for label, (n, c) in bt.FULL_FALLBACK.items() if name in (label, n, c)),
            None,
        )
        if known:
            specs[known] = (bt.FULL_TARGET_PATTERNS[known], bt.FULL_FALLBACK[known][0], code or bt.FULL_FALLBACK[known][1])
            continue
        # 新分點：「元大-台南」→ 標籤 元大台南、正則 元大.*台南
        parts = [p for p in re.split(r"[-－\s]+", name) if p]
        label = "".join(parts)
        specs[label] = (".*".join(re.escape(p) for p in parts), name, code)
    return specs


def register_brokers(bt, specs):
    """把分點注入回測模組的分點清單，並把本次範圍縮成只有這幾間。

    load_history_cache／prune 會把不在清單內的券商整批清掉，所以必須先注入再讀快取。
    """
    for label, (pattern, name, code) in specs.items():
        bt.FULL_TARGET_PATTERNS[label] = pattern
        if code:
            bt.FULL_FALLBACK[label] = (name, code)
    bt.TARGET_PATTERNS = {label: specs[label][0] for label in specs}
    bt.FALLBACK = {label: bt.FULL_FALLBACK[label] for label in specs if label in bt.FULL_FALLBACK}


def resolve_broker_map(bt, warrants, specs):
    if all(code for _p, _n, code in specs.values()):
        return {label: (name, code) for label, (_p, name, code) in specs.items()}
    # 探索代號沿用回測的 find_broker_codes_moneydj，但它最後會用「只有這幾間」
    # 覆寫共用的 broker_map_cache.csv，下次 daily 就得整批重探，所以這裡關掉寫入。
    bt.save_broker_map_cache = lambda *_args, **_kwargs: None
    found = bt.find_broker_codes_moneydj(warrants)
    missing = [label for label in specs if label not in found]
    if missing:
        raise RuntimeError(
            f"近 5 日樣本權證找不到這些分點的代號：{'、'.join(missing)}；"
            "請用「名稱=券商代號」指定，例如 元大-台南=9851"
        )
    return {label: found[label] for label in specs}


def _quick_cache_path(bt, broker_code, target_date):
    return os.path.join(
        bt.CACHE_DIR, "quick_winrate",
        f"{broker_code}_{target_date.replace('/', '')}.csv",
    )


def fetch_history_from_moneydj(bt, warrants, broker_map, target_date, days):
    """API4 掃窗口內所有權證找候選 → API5 取回逐日明細。

    API4 以權證為單位，一次回傳所有分點，所以「新分點」省不掉全市場掃描；
    但同一輪多查幾間分點不會多花請求。
    """
    target_dt = bt.parse_date(target_date)
    # 交易日換算日曆日再加緩衝，與 repair 重建相同。
    scan_days = max(int(days * 1.6) + 20, 90)
    window_start_dt = max(target_dt - timedelta(days=scan_days), MONEYDJ_HISTORY_FLOOR)
    scan_days = (target_dt - window_start_dt).days
    log(f"🚀 MoneyDJ API4 掃描 {window_start_dt:%Y/%m/%d}～{target_date}（全市場權證，最花時間的一段）")
    candidates, _ = bt._moneydj_scan_candidates(
        warrants, broker_map, target_date,
        history_empty=True,
        scan_days_override=scan_days,
        window_start_date=window_start_dt.strftime("%Y/%m/%d"),
    )
    if bt.MONEYDJ_PRESCAN_FAILED_CODES:
        log(f"⚠️ API4 有 {len(bt.MONEYDJ_PRESCAN_FAILED_CODES):,} 檔權證取不到，這些權證的交易會缺漏")
    if not candidates:
        return pd.DataFrame(), 0
    log(f"✅ 候選 {len(candidates):,} 組（權證×分點），開始 API5 取回明細")

    fetched, pending = [], list(candidates)
    for round_no, workers in enumerate((bt.MONEYDJ_HISTORY_WORKERS, bt.MONEYDJ_API5_RECOVERY_WORKERS), start=1):
        if not pending:
            break
        failed = []
        with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as ex:
            futures = {
                ex.submit(bt._moneydj_candidate_to_item, c, target_date, history_limit=days): c
                for c in pending
            }
            for fut in as_completed(futures):
                try:
                    item, ok = fut.result()
                except Exception:
                    item, ok = None, False
                if not ok:
                    failed.append(futures[fut])
                elif item is not None:
                    fetched.append(item)
        log(f"{'✅' if not failed else '⚠️'} API5 第 {round_no} 輪：有效 {len(fetched):,}｜失敗 {len(failed):,}")
        pending = failed

    history_df = bt._moneydj_items_to_history_df(fetched)
    if history_df is not None and not history_df.empty:
        history_df, _ = bt.repair_history_metadata_from_warrants(history_df, warrants)
    return history_df, len(pending)


def load_broker_history(bt, warrants, broker_map, target_date, days):
    codes = {bt.normalize_broker_code_for_compare(code) for _n, code in broker_map.values()}
    main_cache = bt.load_history_cache()

    if not QUICK_FORCE_REFETCH and not main_cache.empty:
        in_main = main_cache[main_cache["券商代號"].map(bt.normalize_broker_code_for_compare).isin(codes)]
        if set(in_main["券商代號"].map(bt.normalize_broker_code_for_compare)) >= codes:
            log(f"♻️ 回測歷史快取已有這些分點：{len(in_main):,} 列，不打 MoneyDJ")
            return in_main, main_cache, 0

    paths = {code: _quick_cache_path(bt, code, target_date) for _n, code in broker_map.values()}
    if not QUICK_FORCE_REFETCH and all(os.path.exists(p) for p in paths.values()):
        log("♻️ 沿用本工具今日快取，不打 MoneyDJ")
        cached = pd.concat(
            [pd.read_csv(p, dtype=str, encoding=bt.CACHE_ENCODING).fillna("") for p in paths.values()],
            ignore_index=True,
        )
        return cached, main_cache, 0

    history_df, failed_pairs = fetch_history_from_moneydj(bt, warrants, broker_map, target_date, days)
    # API5 還有失敗組合時不落地快取：下次重跑會重抓，不會把缺漏資料當成完整結果重用。
    if failed_pairs == 0 and history_df is not None and not history_df.empty:
        for _n, code in broker_map.values():
            part = history_df[history_df["券商代號"].map(bt.normalize_broker_code_for_compare)
                              == bt.normalize_broker_code_for_compare(code)]
            os.makedirs(os.path.dirname(paths[code]), exist_ok=True)
            part.to_csv(paths[code], index=False, encoding=bt.CACHE_ENCODING)
    return history_df, main_cache, failed_pairs


# ============================================================
# 運算層
# ============================================================

def compute_winrate_summary(bt, history_df, main_cache, target_date, days):
    target_dt = bt.parse_date(target_date)
    dates = history_df["日期"].map(bt.parse_date)
    history_df = history_df[dates.notna() & (dates <= target_dt)].copy()

    # 回溯窗口要用「真實交易日」切。單一分點自己的交易日很稀疏，拿它數 200 天會回溯過頭，
    # 所以併入回測歷史快取（31 間分點幾乎涵蓋每個交易日）的日期來算截止日。
    # 用聯集而不是只用快取：QUICK_DAYS 大於快取保留天數時，只看快取會把截止日切得太近。
    ref_dates = pd.concat([main_cache.get("日期", pd.Series(dtype=str)), history_df["日期"]], ignore_index=True)
    cutoff = bt.recent_trading_date_cutoff_from_series(ref_dates, days)
    if cutoff is not None:
        history_df = history_df[history_df["日期"].map(bt.parse_date).map(lambda d: d.date() >= cutoff)]

    items = bt.items_from_history_cache(history_df)
    item_map = {(item["broker_code"], item["warrant_code"]): item for item in items}
    amount_events = bt.build_amount_class_events(bt.build_daily_records(items), item_map)
    groups = [amount_events.get(code, []) for code in bt.AMOUNT_CLASS_CODES]

    if QUICK_MARK_TO_MARKET:
        log(f"⏱️ 持有滿 {bt.WINRATE_MARK_TO_MARKET_DAYS} 日未出清事件按權證市價估值...")
        stats = bt.prepare_repair_winrate_mark_to_market(*groups, {}, target_date)
        if stats.get("unresolved", 0):
            log(f"⚠️ 仍有 {int(stats['unresolved']):,} 筆滿門檻事件缺價，未納入勝率（勝率可能偏高）")

    summary_map, _order = bt.make_summary_map(bt.collect_stat_records(*groups))
    return summary_map, cutoff


# ============================================================
# 輸出層
# ============================================================

def print_summary(bt, summary_map, broker_map, target_date, cutoff):
    rows = []
    for label, (name, code) in broker_map.items():
        per_class = summary_map.get(label, {})
        log("=" * 78)
        log(f"📊 {label}（{name}｜{code}）｜統計日 {target_date}｜起始日 {cutoff or '-'}")
        log(f"  {'類別':<10}{'事件':>5}{'納入':>5}{'勝/敗/平':>11}{'勝率':>8}{'平均報酬':>9}{'加權報酬':>9}{'均持有':>7}")
        for key in ["ALL"] + list(bt.AMOUNT_CLASS_CODES):
            s = per_class.get(key) or bt.calc_empty_summary(label, key)
            title = "全部ABCDE" if key == "ALL" else f"{key}-{bt.AMOUNT_CLASS_LABELS[key]}"
            wl = f"{s['勝筆數']}/{s['敗筆數']}/{s['平手筆數']}"
            hold = "-" if s["平均持有天數"] is None else f"{s['平均持有天數']:.0f}天"
            log(
                f"  {title:<10}{s['事件數']:>6}{s['納入勝率筆數']:>6}{wl:>12}"
                f"{_fmt_pct(s['勝率']):>9}{_fmt_pct(s['平均報酬%'], True):>10}"
                f"{_fmt_pct(s['加權報酬%'], True):>10}{hold:>8}"
            )
            rows.append({"統計日期": target_date, "分點名稱": name, "券商代號": code, **s, "事件類型": title})
        if 0 < per_class.get("ALL", {}).get("事件數", 0) < bt.WINRATE_STATS_MIN_TOTAL_EVENTS:
            log(f"  ⚠️ 事件數未滿 {bt.WINRATE_STATS_MIN_TOTAL_EVENTS} 筆，勝率參考性低（勝率統計表會排到最後）")
    return pd.DataFrame(rows)


# ============================================================
# 主流程
# ============================================================

def run_quick_winrate():
    start = time.time()
    log("=" * 78)
    log("啟動：分點勝率速查（全部＋ABCDE）")
    log("=" * 78)
    if not QUICK_BROKERS:
        log("❌ 請設定 QUICK_BROKERS，例如 QUICK_BROKERS=\"元大-台南=9851,凱基-松山\"")
        return

    bt = load_backtest_module()
    specs = parse_broker_specs(bt, QUICK_BROKERS)
    register_brokers(bt, specs)
    days = QUICK_DAYS or bt.HISTORY_RETENTION_TRADING_DAYS
    bt.HISTORY_RETENTION_TRADING_DAYS = days
    log(f"✅ 查詢分點：{'、'.join(specs)}｜回溯 {days} 個交易日")

    warrants = bt.get_all_call_warrants()
    if not warrants:
        log("❌ 權證清單無法取得，停止")
        return
    broker_map = resolve_broker_map(bt, warrants, specs)
    register_brokers(bt, {label: (specs[label][0], name, code) for label, (name, code) in broker_map.items()})

    market_date = bt.resolve_latest_trading_date_on_or_before(datetime.today())
    target_date, _ = bt.resolve_moneydj_daily_published_date(warrants, market_date)
    if not target_date:
        log(f"❌ MoneyDJ 無法確認 {market_date} 或之前的已發布交易日，停止")
        return

    history_df, main_cache, failed_pairs = load_broker_history(bt, warrants, broker_map, target_date, days)
    if history_df is None or history_df.empty:
        log("⚠️ 窗口內查無這些分點的任何權證交易")
        return

    summary_map, cutoff = compute_winrate_summary(bt, history_df, main_cache, target_date, days)
    result_df = print_summary(bt, summary_map, broker_map, target_date, cutoff)
    output_path = os.path.join(bt.OUTPUT_DIR, f"broker_winrate_quick_{datetime.now():%Y%m%d_%H%M%S}.csv")
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    log("=" * 78)
    if failed_pairs:
        log(f"⚠️ API5 仍有 {failed_pairs:,} 組取不到，結果不完整（未寫入快取，重跑會重抓）")
    log(f"📄 {output_path}")
    log(f"⏱️ 耗時：{time.time() - start:.2f} 秒")
    log("=" * 78)


if __name__ == "__main__":
    run_quick_winrate()
