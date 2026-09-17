"""
權證分點歷史累積庫（只進不出）
=====================================
為什麼需要這個東西：
  實測（2026-09-16）MoneyDJ 的分點資料最早只到 2023/09/11，而且每個代號只保留
  「最近一輪佔用該代號的權證」。也就是說那是一個會往前滾的視窗 ——
  今天多一天，最舊的那一天就可能永遠消失。
  主程式又預設 HISTORY_RETENTION_TRADING_DAYS=200，跑完還會主動把超過 200 個
  交易日的資料剪掉。兩件事加起來，等於一邊抓一邊丟。

這支程式就是那個「丟不掉」的地方：
  * 本體是 Parquet，不是 Excel。3 年 × 全分點約 500–800 萬列，
    Excel 單一工作表上限 1,048,576 列，裝不下。Excel 是匯出，不是儲存。
  * 合併一律是聯集（union）。舊資料只會被「同一把鑰匙的新版本」覆寫，
    永遠不會因為新批次沒有它就被刪掉。
  * 每列記 `首次入庫` 與 `最後更新`，之後要做倖存者偏誤分析時，
    「這列是什麼時候才進來的」本身就是特徵。
  * 寫入前有守衛：合併結果列數少於現有庫，直接中止不寫。
    append-only 要用程式擋住，不能只靠約定。

用法：
    python warrant_history_store.py merge     # 把本次跑出來的快取併進累積庫
    python warrant_history_store.py report    # 看累積庫涵蓋範圍
    python warrant_history_store.py export    # 匯出 Excel
    python warrant_history_store.py export --scope full     # 全量，按年分頁
    python warrant_history_store.py export --scope recent   # 只匯近 N 個交易日

環境變數：
    CACHE_DIR            主程式的快取目錄（預設沿用主程式規則）
    HISTORY_STORE_DIR    累積庫位置（預設 <OUTPUT_DIR>/warrant_history_store）
    EXPORT_RECENT_DAYS   recent 模式匯出幾個交易日（預設 60）

依賴：pandas、pyarrow、openpyxl
"""

import argparse
import os
import sys
from datetime import datetime

import pandas as pd

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError, ValueError):
        pass


# ══════════════════════════════════════════════════════════════════════
# 路徑：與主程式的規則保持一致，避免兩邊各自為政
# ══════════════════════════════════════════════════════════════════════

DEFAULT_OUTPUT_DIR = (
    "output"
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    else r"C:\Users\chen1_ukw0m7r\Downloads"
)
OUTPUT_DIR = os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(OUTPUT_DIR, "warrant_cache"))
STORE_DIR = os.getenv("HISTORY_STORE_DIR", os.path.join(OUTPUT_DIR, "warrant_history_store"))
EXPORT_RECENT_DAYS = max(int(os.getenv("EXPORT_RECENT_DAYS", "60")), 1)

# Excel 單一工作表硬上限；留一列給標題。
EXCEL_MAX_ROWS = 1_048_576 - 1

# 累積庫的兩份資料。key 是合併時的唯一鍵，少一個欄位就會把不同列壓成同一列。
STORES = {
    "history": {
        "來源": os.path.join(CACHE_DIR, "broker_warrant_history_cache.csv"),
        "檔名": "broker_warrant_history_store.parquet",
        "鍵": ["權證代號", "券商代號", "日期"],
        "日期欄": "日期",
        "說明": "分點權證買賣明細",
    },
    "price": {
        "來源": os.path.join(CACHE_DIR, "price_cache.csv"),
        "檔名": "price_store.parquet",
        "鍵": ["代號", "日期"],
        "日期欄": "日期",
        "說明": "權證／標的收盤價",
    },
    # 權證主檔必須一起累積：代號會被回收，只留今天的主檔就無法還原
    # 「這筆 2024 年的事件當時買的是哪一檔權證」。鍵含生命週期就是為了這個。
    "warrants": {
        "來源": os.path.join(CACHE_DIR, "warrants_cache.csv"),
        "檔名": "warrants_store.parquet",
        "鍵": ["代號", "上市日", "最後交易日"],
        "日期欄": "最後交易日",
        "說明": "權證主檔（代號×生命週期）",
    },
}

PROVENANCE_COLUMNS = ["首次入庫", "最後更新"]


def store_path(kind):
    return os.path.join(STORE_DIR, STORES[kind]["檔名"])


# ══════════════════════════════════════════════════════════════════════
# 讀取
# ══════════════════════════════════════════════════════════════════════

def fill_object_na(df):
    """
    只把文字欄位的缺值補成空字串，數值欄位維持 NaN。

    一律 fillna("") 會讓數值欄變成「float 和 str 混在一起」的 object 欄，
    parquet 直接拒寫（ArrowInvalid）。踩過一次，別再改回去。
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    object_columns = [c for c in out.columns if out[c].dtype == object]
    if object_columns:
        out[object_columns] = out[object_columns].fillna("")
    return out


def _normalize_dates(df, date_column):
    """日期統一成 YYYY/MM/DD 字串；解析不掉的列直接丟棄並回報。"""
    if date_column not in df.columns:
        return df, 0
    parsed = pd.to_datetime(
        df[date_column].astype(str).str.replace("/", "-", regex=False),
        errors="coerce",
    )
    dropped = int(parsed.isna().sum())
    out = df[parsed.notna()].copy()
    out[date_column] = parsed[parsed.notna()].dt.strftime("%Y/%m/%d")
    return out, dropped


def read_source_cache(kind):
    """讀主程式這一輪跑出來的快取。與主程式同樣是 parquet 優先、CSV 相容。"""
    source = STORES[kind]["來源"]
    parquet_path = f"{source}.parquet"
    for path, reader in (
        (parquet_path, lambda p: pd.read_parquet(p)),
        (source, lambda p: pd.read_csv(p, dtype=str, encoding="utf-8-sig")),
    ):
        if not os.path.exists(path):
            continue
        try:
            return fill_object_na(reader(path)), path
        except Exception as exc:
            print(f"  ⚠️ 讀取失敗：{path}｜{type(exc).__name__}: {exc}")
    return pd.DataFrame(), ""


def load_store(kind):
    path = store_path(kind)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return fill_object_na(pd.read_parquet(path))
    except Exception as exc:
        # 累積庫讀不出來時絕不能當成空的往下走，否則下一步就把它整個覆蓋掉。
        raise RuntimeError(
            f"累積庫讀取失敗，為避免覆蓋既有資料已中止：{path}｜{type(exc).__name__}: {exc}"
        ) from exc


# ══════════════════════════════════════════════════════════════════════
# 合併：聯集，只進不出
# ══════════════════════════════════════════════════════════════════════

def merge_frames(existing, incoming, keys, date_column, run_stamp=None):
    """
    append-only 合併的本體。抽出來讓參考資料那支程式共用 ——
    這段邏輯只能有一份，兩邊各抄一份的話，其中一份修了 bug 另一份不會跟著修。

    回傳 (合併後 DataFrame, stats)。
    """
    run_stamp = run_stamp or datetime.today().strftime("%Y/%m/%d")
    stats = {
        "既有": 0, "本次": 0, "新增": 0, "更新": 0, "合併後": 0,
        "丟棄壞日期": 0, "略過": "",
    }

    existing = pd.DataFrame() if existing is None else existing
    stats["既有"] = len(existing)

    if incoming is None or incoming.empty:
        stats["略過"] = "本次快取是空的，累積庫原封不動"
        return existing, stats

    incoming = fill_object_na(incoming.copy())
    missing = [k for k in keys if k not in incoming.columns]
    if missing:
        stats["略過"] = f"本次快取缺少鍵欄位 {missing}，不併入"
        return existing, stats

    incoming, dropped = _normalize_dates(incoming, date_column)
    stats["丟棄壞日期"] = dropped
    incoming = incoming.drop_duplicates(subset=keys, keep="last")
    stats["本次"] = len(incoming)

    # 本次批次不該帶著上一輪的入庫戳記進來。
    incoming = incoming.drop(columns=PROVENANCE_COLUMNS, errors="ignore")
    incoming["最後更新"] = run_stamp

    if existing.empty:
        merged = incoming.copy()
        merged["首次入庫"] = run_stamp
        stats["新增"] = len(merged)
        stats["合併後"] = len(merged)
        return merged, stats

    existing, _ = _normalize_dates(existing, date_column)

    # 用鍵做左右比對，判斷哪些是全新的、哪些是既有列的新版本。
    existing_keys = existing[keys].astype(str).agg("\u0001".join, axis=1)
    incoming_keys = incoming[keys].astype(str).agg("\u0001".join, axis=1)
    known = set(existing_keys)
    is_new = ~incoming_keys.isin(known)
    stats["新增"] = int(is_new.sum())
    stats["更新"] = int((~is_new).sum())

    # 既有列的首次入庫要保留；新列才蓋今天。
    first_seen = dict(zip(existing_keys, existing.get("首次入庫", pd.Series(dtype=str))))
    incoming["首次入庫"] = [
        first_seen.get(k) or run_stamp for k in incoming_keys
    ]

    # concat 後 keep="last"：同一把鑰匙以本次為準（MoneyDJ 可能事後修正），
    # 但沒有出現在本次批次的舊列會原封不動留著 —— 這就是 append-only。
    merged = pd.concat([existing, incoming], ignore_index=True, sort=False)
    merged = merged.drop_duplicates(subset=keys, keep="last")
    merged = merged.sort_values(keys).reset_index(drop=True)
    stats["合併後"] = len(merged)

    return merged, stats


def merge_into_store(kind, incoming, run_stamp=None):
    spec = STORES[kind]
    return merge_frames(
        load_store(kind), incoming, spec["鍵"], spec["日期欄"], run_stamp
    )


def atomic_write_parquet(df, path):
    """先寫暫存再置換：中途失敗不會留下半份壞掉的累積庫。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def guarded_write(df, path, previous_rows, label=""):
    """只進不出的守衛：合併結果比既有少就拒寫。給兩支程式共用。"""
    if df is None or df.empty:
        print(f"  ⛔ {label}合併結果是空的，拒絕寫入。")
        return False
    if len(df) < previous_rows:
        print(
            f"  ⛔ {label}合併後 {len(df):,} 列少於既有 {previous_rows:,} 列，"
            "這代表合併邏輯有問題。已中止寫入，累積庫保持原狀。"
        )
        return False
    atomic_write_parquet(df, path)
    return True


def save_store(kind, merged, previous_rows):
    """寫入前的守衛：只進不出必須由程式擋住，不能只靠約定。"""
    return guarded_write(merged, store_path(kind), previous_rows)


def cmd_merge(args):
    run_stamp = datetime.today().strftime("%Y/%m/%d")
    print("=" * 74)
    print(f"📥 併入累積庫｜{run_stamp}")
    print(f"   快取來源 {CACHE_DIR}")
    print(f"   累積庫   {STORE_DIR}")
    print("=" * 74)

    any_written = False
    for kind in (args.kind or list(STORES.keys())):
        spec = STORES[kind]
        print(f"\n  ── {kind}（{spec['說明']}）──")
        incoming, source_path = read_source_cache(kind)
        if source_path:
            print(f"    來源：{source_path}｜{len(incoming):,} 列")
        else:
            print("    來源：找不到本次快取")

        previous_rows = len(load_store(kind))
        merged, stats = merge_into_store(kind, incoming, run_stamp)

        if stats["略過"]:
            print(f"    ⏭ {stats['略過']}")
            continue
        if stats["丟棄壞日期"]:
            print(f"    ⚠️ 丟棄無法解析日期的列：{stats['丟棄壞日期']:,}")

        if save_store(kind, merged, previous_rows):
            any_written = True
            print(
                f"    ✅ 既有 {stats['既有']:,} → 合併後 {stats['合併後']:,} 列"
                f"（新增 {stats['新增']:,}、更新 {stats['更新']:,}）"
            )

    if any_written:
        print()
        cmd_report(args)
    return 0


# ══════════════════════════════════════════════════════════════════════
# 涵蓋率報告：回補到哪、每年多少、哪些分點有資料
# ══════════════════════════════════════════════════════════════════════

def cmd_report(args):
    print("=" * 74)
    print("📊 累積庫涵蓋範圍")
    print("=" * 74)
    for kind in (args.kind or list(STORES.keys())):
        spec = STORES[kind]
        path = store_path(kind)
        if not os.path.exists(path):
            print(f"\n  ── {kind}：尚未建立（{path}）")
            continue

        df = load_store(kind)
        date_column = spec["日期欄"]
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"\n  ── {kind}（{spec['說明']}）｜{len(df):,} 列｜{size_mb:,.1f} MB")
        if df.empty or date_column not in df.columns:
            continue

        dates = df[date_column].astype(str)
        print(f"     日期範圍 {dates.min()} ~ {dates.max()}｜"
              f"相異交易日 {dates.nunique():,}")

        by_year = dates.str[:4].value_counts().sort_index()
        print("     逐年列數：" + "｜".join(
            f"{year} {count:,}" for year, count in by_year.items()
        ))

        if "分點" in df.columns:
            brokers = df["分點"].astype(str).str.strip()
            brokers = brokers[brokers != ""]
            print(f"     分點數 {brokers.nunique()}")

        if "首次入庫" in df.columns:
            intake = df["首次入庫"].astype(str).value_counts().sort_index()
            recent = list(intake.items())[-3:]
            print("     最近入庫批次：" + "｜".join(
                f"{day} +{count:,}" for day, count in recent
            ))
    print("=" * 74)
    return 0


# ══════════════════════════════════════════════════════════════════════
# Excel 匯出
# ══════════════════════════════════════════════════════════════════════

def _write_sheets(writer, df, base_name, date_column):
    """按年分頁；單年超過 Excel 上限就再往下切。回傳實際寫出的分頁數。"""
    written = 0
    years = sorted(df[date_column].astype(str).str[:4].unique())
    for year in years:
        chunk = df[df[date_column].astype(str).str[:4] == year]
        if chunk.empty:
            continue
        parts = [chunk]
        if len(chunk) > EXCEL_MAX_ROWS:
            parts = [
                chunk.iloc[i:i + EXCEL_MAX_ROWS]
                for i in range(0, len(chunk), EXCEL_MAX_ROWS)
            ]
            print(f"    ⚠️ {year} 年 {len(chunk):,} 列超過 Excel 上限，"
                  f"切成 {len(parts)} 個分頁")
        for idx, part in enumerate(parts, start=1):
            suffix = "" if len(parts) == 1 else f"_{idx}"
            part.to_excel(writer, sheet_name=f"{base_name}{year}{suffix}", index=False)
            written += 1
    return written


def cmd_export(args):
    scope = args.scope
    stamp = datetime.today().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"warrant_history_store_{scope}_{stamp}.xlsx")

    print("=" * 74)
    print(f"📤 匯出 Excel｜模式 {scope}")
    print("=" * 74)

    history = load_store("history")
    if history.empty:
        # 回補初期（API6 還沒抓到任何明細）累積庫本來就是空的，這不是錯誤。
        # 回傳 1 會讓 workflow 標紅，看起來像整輪失敗。
        print("  ℹ️ 分點累積庫目前是空的，這次沒有 Excel 可以匯出。")
        return 0

    date_column = STORES["history"]["日期欄"]

    if scope == "recent":
        trading_days = sorted(history[date_column].astype(str).unique())
        keep = set(trading_days[-EXPORT_RECENT_DAYS:])
        history = history[history[date_column].astype(str).isin(keep)]
        print(f"  近 {EXPORT_RECENT_DAYS} 個交易日：{len(history):,} 列")
    elif scope == "full":
        print(f"  全量 {len(history):,} 列 —— 按年分頁；"
              "列數大時 openpyxl 會很慢，請耐心等。")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        if scope == "summary":
            # 給人看的彙總：訓練請直接讀 parquet，不要走 Excel。
            by_year_broker = (
                history.assign(年=history[date_column].astype(str).str[:4])
                .groupby(["年", "分點"], dropna=False)
                .size()
                .reset_index(name="列數")
                .sort_values(["年", "列數"], ascending=[True, False])
            )
            by_year_broker.to_excel(writer, sheet_name="逐年分點列數", index=False)

            by_day = (
                history.groupby(date_column).size().reset_index(name="列數")
            )
            by_day.to_excel(writer, sheet_name="逐日列數", index=False)
            print("  ✅ 彙總 2 個分頁")
        else:
            sheets = _write_sheets(writer, history, "分點明細_", date_column)
            print(f"  ✅ 明細 {sheets} 個分頁")

            price = load_store("price")
            if not price.empty and scope == "recent":
                keep_codes = set(history["權證代號"].astype(str))
                price_slice = price[price["代號"].astype(str).isin(keep_codes)]
                if len(price_slice) <= EXCEL_MAX_ROWS:
                    price_slice.to_excel(writer, sheet_name="價格", index=False)
                    print(f"  ✅ 價格 {len(price_slice):,} 列")

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"\n💾 {out_path}｜{size_mb:,.1f} MB")
    print("=" * 74)
    return 0


def main():
    parser = argparse.ArgumentParser(description="權證分點歷史累積庫（只進不出）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_merge = sub.add_parser("merge", help="把本次快取併進累積庫")
    p_merge.add_argument("--kind", nargs="*", choices=list(STORES.keys()))
    p_merge.set_defaults(func=cmd_merge)

    p_report = sub.add_parser("report", help="看累積庫涵蓋範圍")
    p_report.add_argument("--kind", nargs="*", choices=list(STORES.keys()))
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser("export", help="匯出 Excel")
    p_export.add_argument(
        "--scope", choices=["recent", "full", "summary"], default="recent"
    )
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
