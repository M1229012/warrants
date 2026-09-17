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
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

# append-only 的合併與守衛只有一份實作，從累積庫那支共用過來。
from warrant_history_store import (
    STORE_DIR,
    fill_object_na,
    guarded_write,
    merge_frames,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError, ValueError):
        pass


# 三支抓取程式與 workflow 共用同一個版本號，workflow 開跑前會比對。
# 2026-09-17 發生過只更新了一部分檔案、新舊混跑，log 完全看不出來。
# 改任何一支都要一起升版號。
HARVEST_BUILD = "2026-09-18.1"

META_STORE_PATH = os.path.join(STORE_DIR, "warrant_meta_store.parquet")
OHLCV_DIR = os.path.join(STORE_DIR, "ohlcv")
# 已確認休市的平日（春節、國定假日等）。放在 ohlcv/ 外面：
# 回補與分析程式會把 ohlcv/ 裡的 parquet 都當成行情檔讀。
NO_TRADING_DATES_PATH = os.path.join(STORE_DIR, "ohlcv_no_trading_dates.parquet")
# 兩個市場都回空、而且至少是這麼多天以前的日子，才記成休市。
# 最近幾天回空可能只是還沒發布，不能永久跳過。
NO_TRADING_CONFIRM_DAYS = int(os.getenv("NO_TRADING_CONFIRM_DAYS", "7"))
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


def load_numeric_store(path, numeric_columns):
    """
    讀既有累積庫：文字欄補空字串、數值欄維持 float。

    順便把數值欄重新轉型一次 —— 如果哪一版程式曾經寫進混型別的檔案，
    讀進來就修正，不讓錯誤一路傳下去。
    """
    return coerce_numeric(fill_object_na(pd.read_parquet(path)), numeric_columns)


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
    print(f"📋 權證靜態屬性｜程式版本 {HARVEST_BUILD}")
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
        # 不可以 .fillna("")：數值欄的 NaN 會變成空字串，整欄變成 float／str 混合，
        # 只要有任何一列舊資料沒被本次批次取代（權證下市、履約價調整），parquet 就拒寫。
        # 2026-09-17 在 Actions 上實際炸過（已註銷單位_仟）。
        existing = load_numeric_store(META_STORE_PATH, NUMERIC_META_COLUMNS)
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

    merged = coerce_numeric(merged, NUMERIC_META_COLUMNS)
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
    """只讀日期這一欄，而且用 Arrow 讀：745 萬列的字串欄轉成 pandas 物件很吃記憶體。"""
    path = _ohlcv_path(year)
    if not os.path.exists(path):
        return set(), 0
    table = pq.read_table(path, columns=["日期"])
    return set(pc.unique(table["日期"]).to_pylist()), table.num_rows


def append_ohlcv_days(path, incoming, run_stamp=None):
    """
    把新的交易日附加到年檔，不做逐列鍵比對。

    為什麼不用 merge_frames：OHLCV 的一列是「某代號某日的成交」，事後不會變。
    cmd_ohlcv 的待抓清單本來就排除了既有日期，新資料與既有資料不可能撞鍵，
    合併在這裡等於白做。實測（2026-09-17，真實資料 293 萬列）merge_frames 峰值多用
    2.57 GB、51 秒，外推到 2026 年檔 745 萬列約 6.5 GB、2.2 分鐘 ——
    每天只補 4.6 萬列卻整年重讀重寫，到 12 月會逼近 10 GB。

    這裡整段留在 Arrow 裡：既有檔讀成 Arrow Table，新資料接在後面，一次寫出。
    回傳 (附加前列數, 附加後列數)。
    """
    run_stamp = run_stamp or datetime.today().strftime("%Y/%m/%d")
    incoming = coerce_numeric(incoming.copy(), NUMERIC_OHLCV_COLUMNS)
    incoming = incoming.drop_duplicates(subset=["代號", "日期", "市場"], keep="last")
    incoming["最後更新"] = run_stamp
    incoming["首次入庫"] = run_stamp

    if not os.path.exists(path):
        table = pa.Table.from_pandas(incoming, preserve_index=False)
        _atomic_write_table(table, path)
        return 0, table.num_rows

    existing = pq.read_table(path)
    # 安全閥：萬一上游日期判斷出錯，已經存在的日期一律不重複附加。
    existing_dates = set(pc.unique(existing["日期"]).to_pylist())
    incoming = incoming[~incoming["日期"].isin(existing_dates)]
    if incoming.empty:
        return existing.num_rows, existing.num_rows

    # 欄位順序與型別對齊既有檔；既有檔有、新資料沒有的欄位補空值。
    for name in existing.schema.names:
        if name not in incoming.columns:
            incoming[name] = None
    new_table = pa.Table.from_pandas(
        incoming[existing.schema.names], preserve_index=False
    ).cast(existing.schema.remove_metadata()).replace_schema_metadata(existing.schema.metadata)

    combined = pa.concat_tables([existing, new_table])
    if combined.num_rows < existing.num_rows:
        raise RuntimeError("附加後列數變少，已中止寫入（不應該發生）")
    _atomic_write_table(combined, path)
    return existing.num_rows, combined.num_rows


def _atomic_write_table(table, path):
    """先寫暫存再置換：中途被砍掉不會留下半份壞掉的年檔。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def cmd_ohlcv(args):
    if args.recent:
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=int(args.recent) * 2)
    else:
        start_dt = datetime.strptime(args.start, "%Y/%m/%d")
        end_dt = datetime.strptime(args.end, "%Y/%m/%d")

    print("=" * 74)
    print(f"📈 全市場 OHLCV｜{start_dt:%Y/%m/%d} ~ {end_dt:%Y/%m/%d}｜程式版本 {HARVEST_BUILD}")
    print("   已抓過的日期會自動跳過，中斷後直接重跑即可續抓。")
    print("=" * 74)

    known_closed = load_no_trading_dates()
    newly_closed = set()
    confirm_before = (datetime.today() - timedelta(days=NO_TRADING_CONFIRM_DAYS)).date()

    # 一次處理一年，避免把好幾年的資料同時攤在記憶體裡，
    # 也讓每個 parquet 檔維持在可以當 Release asset 上傳的大小。
    for year in range(start_dt.year, end_dt.year + 1):
        year_start = max(start_dt, datetime(year, 1, 1))
        year_end = min(end_dt, datetime(year, 12, 31))
        done, previous_rows = _existing_dates(year)

        pending = []
        skipped_closed = 0
        cursor = year_start
        while cursor <= year_end:
            key = cursor.strftime("%Y/%m/%d")
            if cursor.weekday() < 5 and key not in done:
                if key in known_closed:
                    skipped_closed += 1
                else:
                    pending.append(cursor)
            cursor += timedelta(days=1)

        closed_note = f"｜已知休市略過 {skipped_closed} 天" if skipped_closed else ""
        print(f"\n  ── {year} ──  既有 {previous_rows:,} 列／{len(done)} 天"
              f"｜待抓 {len(pending)} 天{closed_note}")
        if not pending:
            continue

        collected = []
        failures = []
        for i, day in enumerate(pending, start=1):
            frames = []
            had_error = False
            for label, fetcher in (("TWSE", fetch_ohlcv_twse), ("TPEx", fetch_ohlcv_tpex)):
                df, error = fetcher(day)
                if error:
                    had_error = True
                    failures.append(f"{day:%Y/%m/%d} {label} {error}")
                elif not df.empty:
                    frames.append(df)
                time.sleep(REQUEST_SLEEP)
            if frames:
                collected.append(pd.concat(frames, ignore_index=True))
            elif not had_error and day.date() <= confirm_before:
                # 兩個市場都正常回應、都沒有資料、而且不是最近幾天 → 休市
                newly_closed.add(day.strftime("%Y/%m/%d"))
            if i % 20 == 0 or i == len(pending):
                total = sum(len(f) for f in collected)
                print(f"    {i}/{len(pending)} 天｜已收集 {total:,} 列", flush=True)

        if not collected:
            print("    ⏭ 這一年沒有收到任何資料（可能全是休市日）")
            continue

        incoming = pd.concat(collected, ignore_index=True)
        before, after = append_ohlcv_days(_ohlcv_path(year), incoming)
        print(f"    ✅ {before:,} → {after:,} 列（新增 {after - before:,}）")
        if failures:
            print(f"    ⚠️ {len(failures)} 次抓取失敗（重跑會自動補）：{failures[:3]}")

    if newly_closed:
        save_no_trading_dates(known_closed | newly_closed)
        print(f"\n  📅 新確認休市 {len(newly_closed)} 天，之後不再重查"
              f"（累計 {len(known_closed | newly_closed)} 天）")
    return 0


def load_no_trading_dates():
    if not os.path.exists(NO_TRADING_DATES_PATH):
        return set()
    return set(pq.read_table(NO_TRADING_DATES_PATH, columns=["日期"])["日期"].to_pylist())


def save_no_trading_dates(dates):
    """
    休市日清單。只會變多不會變少。
    萬一哪天被誤判成休市（例如官方那天暫時回空），刪掉這個檔案重跑就會重新確認。
    """
    _atomic_write_table(pa.table({"日期": sorted(dates)}), NO_TRADING_DATES_PATH)


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
            # 用 Arrow 算：2024 年檔 1,184 萬列，兩個字串欄讀進 pandas 要好幾 GB。
            table = pq.read_table(path, columns=["日期", "代號"])
            size = os.path.getsize(path) / 1024 / 1024
            print(f"    {name}｜{table.num_rows:,} 列"
                  f"｜{pc.count_distinct(table['日期']).as_py()} 個交易日"
                  f"｜{pc.count_distinct(table['代號']).as_py():,} 檔｜{size:,.1f} MB")
            del table
        closed = load_no_trading_dates()
        if closed:
            print(f"    已確認休市平日：{len(closed)} 天")
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
