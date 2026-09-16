"""
權證參考資料抓取（靜態屬性 ＋ 全市場 OHLCV）
================================================
為什麼要獨立抓這兩份：

【1】權證靜態屬性 —— 沒有它，模型學到的是 theta 不是選股能力
  權證的漲跌有很大一塊來自「價內外程度」與「剩餘天數」，跟分點的選股能力無關。
  少了履約價和到期日這兩個變數，模型很容易只學會
  「深價內、剩餘天數長的權證比較會漲」，然後你以為它讀懂了分點的操作習性。
  主程式的 warrants_cache 只留 6 個欄位（代號／名稱／標的／上市日／最後交易日），
  履約價、行使比例、認購售、上下限價格全部沒有留。

  而且這份資料有時效性：TWSE t187ap37_L 只列「現行有效」權證，
  已到期權證的履約價事後補不回來 —— 跟籌碼一樣，不現在抓就沒了。

【2】全市場 OHLCV —— 主程式已經在打這兩個端點，卻只留收盤價
  TWSE MI_INDEX?type=ALL 一次回 34,852 列，含開高低收＋成交股數／筆數／金額；
  TPEx dailyQuotes 回 11,358 列，另有均價與發行股數。
  主程式抓價格時全部丟掉只留收盤價。把它們留下來是「零額外請求」，
  而且順帶拿到權證自己的 OHLCV —— 買進日可以用均價估成本，
  比用收盤價合理得多，也才能算 MFE／MAE 這類路徑型標籤。

刻意不做的：分點對「標的現股」的買賣。
  同一分點的權證買方與現股買方無法確認是同一人（一個分點可能有上百個客戶），
  當特徵的訊噪比太差，不值得為它多抓一份資料。

用法：
    python warrant_reference_harvest.py meta
    python warrant_reference_harvest.py ohlcv --start 2023/09/11 --end 2026/09/16
    python warrant_reference_harvest.py ohlcv --recent 5      # 每日增量用
    python warrant_reference_harvest.py report

依賴：requests、pandas、pyarrow、warrant_history_store.py（同資料夾）
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

# append-only 的合併與守衛只有一份實作，從累積庫那支共用過來。
from warrant_history_store import (
    STORE_DIR,
    guarded_write,
    merge_frames,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError, ValueError):
        pass


META_STORE_PATH = os.path.join(STORE_DIR, "warrant_meta_store.parquet")
OHLCV_DIR = os.path.join(STORE_DIR, "ohlcv")
REQUEST_SLEEP = float(os.getenv("REFERENCE_SLEEP_SECONDS", "0.6"))
REQUEST_TIMEOUT = (8, 60)
MAX_ATTEMPTS = int(os.getenv("REFERENCE_MAX_ATTEMPTS", "4"))

SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=4))


def _get_json(url, params=None, referer=""):
    """官方端點很常間歇性斷線（TPEx 的 ChunkedEncodingError 尤其頻繁），重試不足會整天缺資料。"""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, */*",
    }
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = SESSION.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            return response.json(), ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(2 ** attempt, 8))
    return None, last


# ══════════════════════════════════════════════════════════════════════
# 日期：官方兩邊混用民國與西元
# ══════════════════════════════════════════════════════════════════════

def roc_to_ad(value):
    """'1150915' → '2026/09/15'；已經是西元的 '20250924' 原樣轉換。"""
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) == 8:
        return f"{text[0:4]}/{text[4:6]}/{text[6:8]}"
    if len(text) == 7:
        return f"{int(text[0:3]) + 1911}/{text[3:5]}/{text[5:7]}"
    return ""


def _num(value):
    text = str(value or "").replace(",", "").strip()
    if not text or text in ("--", "-", "null"):
        return ""
    try:
        return float(text)
    except ValueError:
        return ""


NUMERIC_META_COLUMNS = [
    "履約價_原始", "履約價_最新", "行使比例_最新",
    "上限價_最新", "下限價_最新", "發行單位數量_仟", "已註銷單位_仟",
]
NUMERIC_OHLCV_COLUMNS = [
    "開盤價", "最高價", "最低價", "收盤價", "均價",
    "成交股數", "成交金額", "成交筆數",
]


def coerce_numeric(df, columns):
    """數值欄位一律轉成 float。留著空字串會讓整欄變成 float／str 混合的
    object 欄，parquet 直接拒寫。缺值用 NaN，不要用空字串。"""
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


# ══════════════════════════════════════════════════════════════════════
# 1. 權證靜態屬性
# ══════════════════════════════════════════════════════════════════════

META_COLUMNS = [
    "權證代號", "權證簡稱", "市場", "權證類型", "類別",
    "標的代號", "標的名稱", "上市日", "最後交易日", "履約截止日",
    "履約價_原始", "履約價_最新", "行使比例_最新",
    "上限價_最新", "下限價_最新",
    "發行單位數量_仟", "已註銷單位_仟", "快照日",
]


def fetch_meta_twse():
    payload, error = _get_json("https://openapi.twse.com.tw/v1/opendata/t187ap37_L")
    if not isinstance(payload, list):
        print(f"    ⚠️ TWSE t187ap37_L 失敗：{error}")
        return pd.DataFrame(columns=META_COLUMNS)

    rows = []
    for item in payload:
        rows.append({
            "權證代號": str(item.get("權證代號", "")).strip(),
            "權證簡稱": str(item.get("權證簡稱", "")).strip(),
            "市場": "TWSE",
            "權證類型": str(item.get("權證類型", "")).strip(),
            "類別": str(item.get("類別", "")).strip(),
            # TWSE 只給標的名稱不給代號；代號從分點累積庫的「標的股」欄位 join 回來。
            "標的代號": "",
            "標的名稱": str(item.get("標的證券/指數", "")).strip(),
            "上市日": "",
            "最後交易日": roc_to_ad(item.get("最後交易日", "")),
            "履約截止日": roc_to_ad(item.get("履約截止日", "")),
            "履約價_原始": _num(item.get("原始履約價格(元)/履約指數", "")),
            "履約價_最新": _num(item.get("最新履約價格(元)/履約指數", "")),
            "行使比例_最新": _num(item.get("最新標的履約配發數量(每仟單位權證)", "")),
            "上限價_最新": _num(item.get("最新上限價格(元)/上限指數", "")),
            "下限價_最新": _num(item.get("最新下限價格(元)/下限指數", "")),
            "發行單位數量_仟": _num(item.get("發行單位數量(仟單位)", "")),
            "已註銷單位_仟": "",
            "快照日": roc_to_ad(item.get("出表日期", "")),
        })
    return coerce_numeric(pd.DataFrame(rows, columns=META_COLUMNS), NUMERIC_META_COLUMNS)


def fetch_meta_tpex():
    payload, error = _get_json(
        "https://www.tpex.org.tw/openapi/v1/tpex_warrant_issue",
        referer="https://www.tpex.org.tw/",
    )
    if not isinstance(payload, list):
        print(f"    ⚠️ TPEx tpex_warrant_issue 失敗：{error}")
        return pd.DataFrame(columns=META_COLUMNS)

    rows = []
    for item in payload:
        rows.append({
            "權證代號": str(item.get("Code", "")).strip(),
            "權證簡稱": str(item.get("Name", "")).strip(),
            "市場": "TPEx",
            "權證類型": str(item.get("Type", "")).strip(),
            "類別": str(item.get("American/European", "")).strip(),
            "標的代號": str(item.get("UnderlyingStockCode", "")).strip(),
            "標的名稱": str(item.get("UnderlyingStock", "")).strip(),
            "上市日": roc_to_ad(item.get("ListedDate", "")),
            "最後交易日": roc_to_ad(item.get("ExpiryDate", "")),
            "履約截止日": roc_to_ad(item.get("ExpiryDate", "")),
            "履約價_原始": "",
            "履約價_最新": _num(item.get("LatestExercisePrice", "")),
            "行使比例_最新": _num(item.get("Latest ExerciseRatio", "")),
            "上限價_最新": _num(item.get("CapPrice/Index", "")),
            "下限價_最新": _num(item.get("FloorPrice/Index", "")),
            "發行單位數量_仟": _num(item.get("InitialIssuance", "")),
            "已註銷單位_仟": _num(item.get("Accum.CanceledWarrant", "")),
            "快照日": roc_to_ad(item.get("Date", "")),
        })
    return coerce_numeric(pd.DataFrame(rows, columns=META_COLUMNS), NUMERIC_META_COLUMNS)


def cmd_meta(args):
    print("=" * 74)
    print("📋 權證靜態屬性")
    print("=" * 74)

    frames = []
    for label, fetcher in (("TWSE", fetch_meta_twse), ("TPEx", fetch_meta_tpex)):
        df = fetcher()
        print(f"  {label}：{len(df):,} 筆")
        if not df.empty:
            frames.append(df)
        time.sleep(REQUEST_SLEEP)

    if not frames:
        print("  ⛔ 兩個官方來源都沒有回應，不寫入。")
        return 1

    incoming = pd.concat(frames, ignore_index=True)
    incoming = incoming[incoming["權證代號"].astype(str).str.strip() != ""]

    existing = pd.DataFrame()
    if os.path.exists(META_STORE_PATH):
        existing = pd.read_parquet(META_STORE_PATH).fillna("")
    previous_rows = len(existing)

    # 鍵含履約價與行使比例：除息調整會產生新的一列，而不是把舊的蓋掉。
    # 配合 首次入庫／最後更新，就能還原「事件當天的履約價是多少」。
    merged, stats = merge_frames(
        existing,
        incoming,
        keys=["權證代號", "最後交易日", "履約價_最新", "行使比例_最新"],
        date_column="最後交易日",
    )
    if stats["略過"]:
        print(f"  ⏭ {stats['略過']}")
        return 0

    if guarded_write(merged, META_STORE_PATH, previous_rows):
        print(
            f"  ✅ 既有 {stats['既有']:,} → {stats['合併後']:,} 列"
            f"（新增 {stats['新增']:,}、更新 {stats['更新']:,}）"
        )
    return 0


# ══════════════════════════════════════════════════════════════════════
# 2. 全市場 OHLCV
# ══════════════════════════════════════════════════════════════════════

OHLCV_COLUMNS = [
    "代號", "名稱", "日期", "市場",
    "開盤價", "最高價", "最低價", "收盤價", "均價",
    "成交股數", "成交金額", "成交筆數",
]


def _ohlcv_path(year):
    return os.path.join(OHLCV_DIR, f"ohlcv_store_{year}.parquet")


def fetch_ohlcv_twse(target_dt):
    payload, error = _get_json(
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
        params={"response": "json", "date": target_dt.strftime("%Y%m%d"), "type": "ALL"},
        referer="https://www.twse.com.tw/",
    )
    if not isinstance(payload, dict):
        return pd.DataFrame(columns=OHLCV_COLUMNS), error
    if str(payload.get("stat", "")).upper() not in ("OK", ""):
        return pd.DataFrame(columns=OHLCV_COLUMNS), ""

    date_text = target_dt.strftime("%Y/%m/%d")
    rows = []
    for table in payload.get("tables") or []:
        fields = [str(f) for f in (table.get("fields") or [])]
        if "證券代號" not in fields:
            continue
        idx = {name: i for i, name in enumerate(fields)}
        for row in table.get("data") or []:
            if not isinstance(row, (list, tuple)) or len(row) < len(fields):
                continue
            code = str(row[idx["證券代號"]]).strip().replace('"', "").replace("=", "")
            if not code:
                continue
            rows.append({
                "代號": code,
                "名稱": str(row[idx.get("證券名稱", 1)]).strip(),
                "日期": date_text,
                "市場": "TWSE",
                "開盤價": _num(row[idx["開盤價"]]) if "開盤價" in idx else "",
                "最高價": _num(row[idx["最高價"]]) if "最高價" in idx else "",
                "最低價": _num(row[idx["最低價"]]) if "最低價" in idx else "",
                "收盤價": _num(row[idx["收盤價"]]) if "收盤價" in idx else "",
                "均價": "",
                "成交股數": _num(row[idx["成交股數"]]) if "成交股數" in idx else "",
                "成交金額": _num(row[idx["成交金額"]]) if "成交金額" in idx else "",
                "成交筆數": _num(row[idx["成交筆數"]]) if "成交筆數" in idx else "",
            })
        if rows:
            break
    return coerce_numeric(pd.DataFrame(rows, columns=OHLCV_COLUMNS), NUMERIC_OHLCV_COLUMNS), ""


def fetch_ohlcv_tpex(target_dt):
    payload, error = _get_json(
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
        params={"date": target_dt.strftime("%Y/%m/%d"), "id": "", "response": "json"},
        referer="https://www.tpex.org.tw/",
    )
    if not isinstance(payload, dict):
        return pd.DataFrame(columns=OHLCV_COLUMNS), error

    date_text = target_dt.strftime("%Y/%m/%d")
    rows = []
    for table in payload.get("tables") or []:
        fields = [str(f) for f in (table.get("fields") or [])]
        if "代號" not in fields:
            continue
        idx = {name: i for i, name in enumerate(fields)}
        for row in table.get("data") or []:
            if not isinstance(row, (list, tuple)) or len(row) < len(fields):
                continue
            code = str(row[idx["代號"]]).strip()
            if not code:
                continue
            rows.append({
                "代號": code,
                "名稱": str(row[idx.get("名稱", 1)]).strip(),
                "日期": date_text,
                "市場": "TPEx",
                "開盤價": _num(row[idx["開盤"]]) if "開盤" in idx else "",
                "最高價": _num(row[idx["最高"]]) if "最高" in idx else "",
                "最低價": _num(row[idx["最低"]]) if "最低" in idx else "",
                "收盤價": _num(row[idx["收盤"]]) if "收盤" in idx else "",
                "均價": _num(row[idx["均價"]]) if "均價" in idx else "",
                "成交股數": _num(row[idx["成交股數"]]) if "成交股數" in idx else "",
                "成交金額": _num(row[idx["成交金額(元)"]]) if "成交金額(元)" in idx else "",
                "成交筆數": _num(row[idx["成交筆數"]]) if "成交筆數" in idx else "",
            })
        if rows:
            break
    return coerce_numeric(pd.DataFrame(rows, columns=OHLCV_COLUMNS), NUMERIC_OHLCV_COLUMNS), ""


def _existing_dates(year):
    path = _ohlcv_path(year)
    if not os.path.exists(path):
        return set(), 0
    df = pd.read_parquet(path, columns=["日期"])
    return set(df["日期"].astype(str)), len(df)


def cmd_ohlcv(args):
    if args.recent:
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=int(args.recent) * 2)
    else:
        start_dt = datetime.strptime(args.start, "%Y/%m/%d")
        end_dt = datetime.strptime(args.end, "%Y/%m/%d")

    print("=" * 74)
    print(f"📈 全市場 OHLCV｜{start_dt:%Y/%m/%d} ~ {end_dt:%Y/%m/%d}")
    print("   已抓過的日期會自動跳過，中斷後直接重跑即可續抓。")
    print("=" * 74)

    # 一次處理一年，避免把好幾年的資料同時攤在記憶體裡，
    # 也讓每個 parquet 檔維持在可以當 Release asset 上傳的大小。
    for year in range(start_dt.year, end_dt.year + 1):
        year_start = max(start_dt, datetime(year, 1, 1))
        year_end = min(end_dt, datetime(year, 12, 31))
        done, previous_rows = _existing_dates(year)

        pending = []
        cursor = year_start
        while cursor <= year_end:
            if cursor.weekday() < 5 and cursor.strftime("%Y/%m/%d") not in done:
                pending.append(cursor)
            cursor += timedelta(days=1)

        print(f"\n  ── {year} ──  既有 {previous_rows:,} 列／{len(done)} 天"
              f"｜待抓 {len(pending)} 天")
        if not pending:
            continue

        collected = []
        failures = []
        for i, day in enumerate(pending, start=1):
            frames = []
            for label, fetcher in (("TWSE", fetch_ohlcv_twse), ("TPEx", fetch_ohlcv_tpex)):
                df, error = fetcher(day)
                if error:
                    failures.append(f"{day:%Y/%m/%d} {label} {error}")
                elif not df.empty:
                    frames.append(df)
                time.sleep(REQUEST_SLEEP)
            if frames:
                collected.append(pd.concat(frames, ignore_index=True))
            if i % 20 == 0 or i == len(pending):
                total = sum(len(f) for f in collected)
                print(f"    {i}/{len(pending)} 天｜已收集 {total:,} 列", flush=True)

        if not collected:
            print("    ⏭ 這一年沒有收到任何資料（可能全是休市日）")
            continue

        incoming = pd.concat(collected, ignore_index=True)
        existing = pd.DataFrame()
        if os.path.exists(_ohlcv_path(year)):
            existing = pd.read_parquet(_ohlcv_path(year)).fillna("")

        merged, stats = merge_frames(
            existing, incoming, keys=["代號", "日期", "市場"], date_column="日期"
        )
        if guarded_write(merged, _ohlcv_path(year), previous_rows, f"{year} "):
            print(f"    ✅ {previous_rows:,} → {stats['合併後']:,} 列"
                  f"（新增 {stats['新增']:,}）")
        if failures:
            print(f"    ⚠️ {len(failures)} 次抓取失敗（重跑會自動補）：{failures[:3]}")

    return 0


def cmd_report(args):
    print("=" * 74)
    print("📊 參考資料累積庫")
    print("=" * 74)

    if os.path.exists(META_STORE_PATH):
        meta = pd.read_parquet(META_STORE_PATH)
        size = os.path.getsize(META_STORE_PATH) / 1024 / 1024
        print(f"\n  權證靜態屬性｜{len(meta):,} 列｜{size:,.1f} MB")
        print(f"    相異權證 {meta['權證代號'].nunique():,}"
              f"｜市場 {dict(meta['市場'].value_counts())}")
        if "權證類型" in meta.columns:
            print(f"    類型 {dict(meta['權證類型'].value_counts())}")
        if "首次入庫" in meta.columns:
            print(f"    入庫批次 {meta['首次入庫'].nunique()} 批"
                  f"，最早 {meta['首次入庫'].min()}")
    else:
        print("\n  權證靜態屬性：尚未建立")

    if os.path.isdir(OHLCV_DIR):
        print("\n  全市場 OHLCV：")
        for name in sorted(os.listdir(OHLCV_DIR)):
            if not name.endswith(".parquet"):
                continue
            path = os.path.join(OHLCV_DIR, name)
            df = pd.read_parquet(path, columns=["日期", "代號"])
            size = os.path.getsize(path) / 1024 / 1024
            print(f"    {name}｜{len(df):,} 列｜{df['日期'].nunique()} 個交易日"
                  f"｜{df['代號'].nunique():,} 檔｜{size:,.1f} MB")
    else:
        print("\n  全市場 OHLCV：尚未建立")
    print("=" * 74)
    return 0


def main():
    parser = argparse.ArgumentParser(description="權證參考資料抓取")
    sub = parser.add_subparsers(dest="command", required=True)

    p_meta = sub.add_parser("meta", help="權證靜態屬性（履約價、行使比例、到期日…）")
    p_meta.set_defaults(func=cmd_meta)

    p_ohlcv = sub.add_parser("ohlcv", help="全市場 OHLCV")
    p_ohlcv.add_argument("--start", default="2023/09/11")
    p_ohlcv.add_argument("--end", default=datetime.today().strftime("%Y/%m/%d"))
    p_ohlcv.add_argument("--recent", type=int, help="只補最近 N 個日曆日")
    p_ohlcv.set_defaults(func=cmd_ohlcv)

    p_report = sub.add_parser("report", help="看累積狀況")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
