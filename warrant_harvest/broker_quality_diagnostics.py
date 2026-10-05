"""
分點品質診斷：把「勝率高」拆成「真的有能力」與「只是押對一波」
==================================================================
要解決的問題：
  有些分點勝率很高，但三年只買台積電，或只買辛耘、萬潤兩檔。
  帳面上 20 筆、勝率 75%，實際可能只是「三週內在同一波行情裡加碼五次」＝
  一次判斷，而且那檔股票本來就在漲 —— 買什麼認購權證都會贏。
  現行的「分點勝率排行」把這種情況跟真正的選股能力混成同一個數字。

這支程式不對集中度做人工扣分（那只是把一個主觀係數換成另一個），
而是把勝率拆成五個彼此獨立的診斷：

  1. 有效樣本數    同一標的、相隔 < K 個交易日的事件併成一個 episode。
                   同一波行情裡的加碼是一次判斷，不是 N 筆樣本。
  2. Wilson 下界   用 episode 數算 95% 信賴區間下界，排序用下界而不是點估計。
                   樣本少的分點區間寬，自己就會沉下去，不需要訂懲罰係數。
  3. 集中度        相異標的數、HHI、前三大標的佔比。這是描述，不是扣分項。
  4. 超額勝率      基準＝同標的、同時間窗、其他分點的報酬中位數。
                   辛耘漲兩倍時大家都贏，扣掉基準才看得出誰真的比較強。
  5. 跨標的一致性  在幾檔不同標的上都贏？專精型分點這一項會很低 ——
                   它不是沒價值，是「不能外推到沒做過的標的」。

刻意的設計取捨，先講清楚：

  * 標籤用「前瞻報酬」而不是主程式的 FIFO 出清損益。
    FIFO 的持有期由賣出時機決定，每筆事件長短不一，沒辦法公平比較；
    而且這裡要問的是「他買的東西有沒有漲」，不是「他賣得好不好」。
    兩個問題都值得回答，但診斷集中度問題要用前者。

  * 出場價遇到當天沒成交，用最後有效收盤價往前填，並記錄填補比例。
    台股三萬多檔權證每天只有一部分有交易，不填會系統性丟掉冷門權證。

用法：
    python broker_quality_diagnostics.py
    python broker_quality_diagnostics.py --horizon 20 --episode-gap 5
    python broker_quality_diagnostics.py --excel

依賴：pandas、pyarrow、openpyxl
"""

import argparse
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, OSError, ValueError):
        pass


# 與 warrant_harvest/ 三支程式、workflow 共用同一個版本號，workflow 開跑前會比對。
HARVEST_BUILD = "2026-09-18.2"

DEFAULT_OUTPUT_DIR = (
    "output"
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    else r"C:\Users\chen1_ukw0m7r\Downloads"
)
OUTPUT_DIR = os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
STORE_DIR = os.getenv(
    "HISTORY_STORE_DIR", os.path.join(OUTPUT_DIR, "warrant_history_store")
)
HISTORY_PATH = os.path.join(STORE_DIR, "broker_warrant_history_store.parquet")
OHLCV_DIR = os.path.join(STORE_DIR, "ohlcv")

# ABCDE 分級門檻（元）。與主程式檔頭記載的規則一致：
# 進入條件＝事件內至少 1 檔權證單日買進金額 >= 100 萬；
# 分級依據＝同一分點＋同一標的＋同一天的買進金額合計。
SINGLE_WARRANT_ENTRY_AMOUNT = float(os.getenv("ENTRY_AMOUNT", "1000000"))
GRADE_BOUNDS = [
    ("A", 1_000_000, 1_600_000),
    ("B", 1_600_000, 2_500_000),
    ("C", 2_500_000, 5_000_000),
    ("D", 5_000_000, 10_000_000),
    ("E", 10_000_000, float("inf")),
]

Z_95 = 1.959963985
EXCEL_MAX_ROWS = 1_048_576 - 1


def grade_breakdown(events, return_column, horizons):
    """分點 × ABCDE 級距的次數與勝率。大單（D、E）和小單（A、B）的準度常常差很多。"""
    scored = events[events[return_column].notna()]
    if scored.empty:
        return pd.DataFrame()
    rows = []
    for (broker, grade), group in scored.groupby(["分點", "級距"]):
        row = {"分點": broker, "級距": grade, "事件數": len(group),
               "中位報酬": float(group[return_column].median())}
        for h in horizons:
            column = f"報酬_{h}"
            if column in group and group[column].notna().any():
                row[f"勝率_{h}日"] = float((group[column].dropna() > 0).mean())
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["分點", "級距"]).reset_index(drop=True)


def ohlcv_market_gaps():
    """
    找出只有一個市場有行情的交易日。

    2026-09-18 實際發生過：2025/06～12 有 136 天只抓到 TPEx、TWSE 整段回空。
    那些天的上市權證沒有價格，算報酬時會拿更早的價格往前填，勝率會失真而且看不出來。
    分析前先檢查，有缺就明講。
    """
    gaps = []
    if not os.path.isdir(OHLCV_DIR):
        return gaps
    for name in sorted(os.listdir(OHLCV_DIR)):
        if not name.endswith(".parquet"):
            continue
        table = pq.read_table(os.path.join(OHLCV_DIR, name), columns=["日期", "市場"])
        pairs = table.group_by(["日期", "市場"]).aggregate([]).to_pylist()
        markets = {}
        for row in pairs:
            markets.setdefault(row["日期"], set()).add(row["市場"])
        gaps.extend(
            (date, sorted({"TWSE", "TPEx"} - have))
            for date, have in markets.items() if len(have) < 2
        )
    return sorted(gaps)


def explanation(args, events, trading_days, gaps=()):
    """Excel 第一頁：這份報表的勝率是怎麼算的。沒有這頁，數字很容易被誤讀。"""
    gap_text = "無（每個交易日兩個市場都有行情）"
    if gaps:
        gap_text = (f"⚠️ 有 {len(gaps)} 天只有單一市場的行情"
                    f"（{gaps[0][0]} ～ {gaps[-1][0]}），這些日子的價格是往前填的，"
                    "涉及的勝率可能失真。抓取流程下一輪會自動補齊。")
    lines = [
        ("資料完整性", gap_text),
        ("", ""),
        ("程式版本", HARVEST_BUILD),
        ("產出時間", datetime.today().strftime("%Y/%m/%d %H:%M")),
        ("資料期間", f"{trading_days[0]} ～ {trading_days[-1]}（{len(trading_days)} 個交易日）"),
        ("事件數", f"{len(events):,}"),
        ("", ""),
        ("事件", "同一分點＋同一標的＋同一天＝ 1 筆；進場條件是單一權證買進 ≥ 100 萬，"
                 "級距看當天該標的所有權證買進合計（A 100–159 萬 … E ≥ 1000 萬）"),
        ("勝率", f"事件當天收盤買進、持有 N 個交易日後收盤賣出，報酬 > 0 算贏。"
                 f"排序用的是 {args.horizon} 日。多檔權證以買進金額加權"),
        ("注意", "這不是主程式的 FIFO 出清勝率。FIFO 看的是『他賣得好不好』，"
                 "這裡看的是『他買的東西有沒有漲』，兩者都有意義但不能直接比"),
        ("原始勝率", "每筆事件各算一次"),
        ("有效勝率", f"同標的相隔 < {args.episode_gap} 個交易日的事件併成一次判斷後的勝率"),
        ("Wilson下界", "有效勝率的 95% 信賴區間下界。樣本少的分點會被拉低，排序用這個"),
        ("超額勝率", f"扣掉同標的、前後 {args.benchmark_window} 日內其他分點的報酬中位數後，"
                     "仍然贏的比例。接近 50% 代表贏的是行情不是選股"),
        ("參考性", "高／中／專精／低，判讀規則見『判讀』欄"),
    ]
    return pd.DataFrame(lines, columns=["項目", "說明"])


# ══════════════════════════════════════════════════════════════════════
# 讀取
# ══════════════════════════════════════════════════════════════════════

def load_history():
    if not os.path.exists(HISTORY_PATH):
        raise SystemExit(
            f"⛔ 找不到分點累積庫：{HISTORY_PATH}\n"
            "   先跑 warrant_history_store.py merge。"
        )
    df = pd.read_parquet(HISTORY_PATH)
    needed = {"分點", "標的股", "權證代號", "日期", "買進金額"}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(f"⛔ 累積庫缺少欄位：{sorted(missing)}")

    df["買進金額"] = pd.to_numeric(df["買進金額"], errors="coerce").fillna(0.0)
    for column in ("分點", "標的股", "權證代號", "日期"):
        df[column] = df[column].astype(str).str.strip()
    df = df[(df["分點"] != "") & (df["日期"] != "")]
    return df


def load_ohlcv(wanted_codes):
    """
    只載入用得到的代號。

    在 Arrow 裡先過濾再轉 pandas：四個年檔合計約 3,300 萬列，
    整份讀進 pandas 光字串欄就要好幾 GB。
    """
    if not os.path.isdir(OHLCV_DIR):
        raise SystemExit(
            f"⛔ 找不到 OHLCV：{OHLCV_DIR}" + chr(10)
            + "   先跑 warrant_reference_harvest.py ohlcv。"
        )
    wanted = pa.array(sorted({str(c).strip() for c in wanted_codes if str(c).strip()}))
    frames = []
    for name in sorted(os.listdir(OHLCV_DIR)):
        if not name.endswith(".parquet"):
            continue
        table = pq.read_table(
            os.path.join(OHLCV_DIR, name),
            columns=["代號", "日期", "收盤價", "成交股數"],
        )
        codes = pc.utf8_trim_whitespace(table["代號"])
        table = table.set_column(0, "代號", codes).filter(pc.is_in(codes, value_set=wanted))
        frames.append(table.to_pandas())
        del table
    if not frames:
        raise SystemExit("⛔ OHLCV 目錄裡沒有 parquet")

    ohlcv = pd.concat(frames, ignore_index=True)
    ohlcv["日期"] = ohlcv["日期"].astype(str)
    ohlcv["收盤價"] = pd.to_numeric(ohlcv["收盤價"], errors="coerce")
    return ohlcv.sort_values(["代號", "日期"])


# ══════════════════════════════════════════════════════════════════════
# Step 1：還原 ABCDE 事件
# ══════════════════════════════════════════════════════════════════════

def build_events(history):
    """
    同一分點＋同一標的＋同一天＝ 1 筆事件。

    進入條件看的是「事件內單一權證的最大買進金額」，
    分級看的是「事件內所有權證的買進金額合計」—— 這兩個是不同的數字，
    用錯會讓一堆小額分散買進被誤判成進場。
    """
    buys = history[history["買進金額"] > 0].copy()
    grouped = buys.groupby(["分點", "標的股", "日期"], dropna=False)

    events = grouped.agg(
        買進金額合計=("買進金額", "sum"),
        單檔最大買進=("買進金額", "max"),
        權證檔數=("權證代號", "nunique"),
    ).reset_index()

    events = events[events["單檔最大買進"] >= SINGLE_WARRANT_ENTRY_AMOUNT].copy()

    def grade(amount):
        for name, low, high in GRADE_BOUNDS:
            if low <= amount < high:
                return name
        return ""

    events["級距"] = events["買進金額合計"].map(grade)
    events = events[events["級距"] != ""]
    return events.sort_values(["分點", "標的股", "日期"]).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════
# Step 2：標籤 —— 前瞻報酬
# ══════════════════════════════════════════════════════════════════════

def build_price_lookup(ohlcv, trading_days):
    """
    每個代號攤平成「交易日 × 收盤價」並往前填。

    沒成交的日子官方不給收盤價（實測無成交列 100% 缺值）。
    不往前填的話，冷門權證會系統性拿不到出場價，
    而冷門權證正好是小分點在買的 —— 那會直接扭曲比較結果。
    """
    wide = ohlcv.pivot_table(
        index="日期", columns="代號", values="收盤價", aggfunc="last"
    )
    wide = wide.reindex(trading_days).ffill()
    return wide


def attach_returns(history, events, price_wide, trading_days, horizons):
    """事件報酬＝事件內各權證前瞻報酬，以買進金額加權。"""
    day_index = {day: i for i, day in enumerate(trading_days)}

    legs = history[history["買進金額"] > 0][
        ["分點", "標的股", "日期", "權證代號", "買進金額"]
    ].copy()
    keys = events[["分點", "標的股", "日期"]]
    legs = legs.merge(keys, on=["分點", "標的股", "日期"], how="inner")

    legs["_i"] = legs["日期"].map(day_index)
    legs = legs[legs["_i"].notna()].copy()
    legs["_i"] = legs["_i"].astype(int)

    entry = []
    for code, i in zip(legs["權證代號"], legs["_i"]):
        entry.append(
            price_wide.iat[i, price_wide.columns.get_loc(code)]
            if code in price_wide.columns else float("nan")
        )
    legs["進場價"] = entry

    for horizon in horizons:
        exits = []
        for code, i in zip(legs["權證代號"], legs["_i"]):
            j = min(i + horizon, len(trading_days) - 1)
            exits.append(
                price_wide.iat[j, price_wide.columns.get_loc(code)]
                if code in price_wide.columns else float("nan")
            )
        legs[f"出場價_{horizon}"] = exits
        legs[f"報酬_{horizon}"] = (
            legs[f"出場價_{horizon}"] / legs["進場價"] - 1.0
        )

    # 加權平均：金額大的權證對事件結果影響大，等權平均會被小單稀釋。
    out = events.copy()
    for horizon in horizons:
        column = f"報酬_{horizon}"
        valid = legs[legs[column].notna() & (legs["進場價"] > 0)]
        weighted = (
            valid.assign(_w=valid["買進金額"] * valid[column])
            .groupby(["分點", "標的股", "日期"])
            .agg(_num=("_w", "sum"), _den=("買進金額", "sum"))
            .reset_index()
        )
        weighted[column] = weighted["_num"] / weighted["_den"]
        out = out.merge(
            weighted[["分點", "標的股", "日期", column]],
            on=["分點", "標的股", "日期"],
            how="left",
        )
    return out


# ══════════════════════════════════════════════════════════════════════
# Step 3：Episode 合併 —— 真正的獨立樣本數
# ══════════════════════════════════════════════════════════════════════

def build_episodes(events, trading_days, gap, return_column):
    """
    同一分點、同一標的，與前一筆相隔 < gap 個交易日就併成同一個 episode。

    這是整支程式最關鍵的一步。三週內在辛耘加碼五次是一次判斷，
    當成五筆獨立樣本會讓信賴區間縮到原本的一半以下，
    高集中度分點的勝率因此被系統性高估。
    """
    day_index = {day: i for i, day in enumerate(trading_days)}
    work = events.copy()
    work["_i"] = work["日期"].map(day_index)
    work = work[work["_i"].notna()].copy()
    work["_i"] = work["_i"].astype(int)
    work = work.sort_values(["分點", "標的股", "_i"])

    previous = work.groupby(["分點", "標的股"])["_i"].shift(1)
    is_new = previous.isna() | ((work["_i"] - previous) >= gap)
    work["_episode"] = is_new.groupby(
        [work["分點"], work["標的股"]]
    ).cumsum()

    valid = work[work[return_column].notna()]
    episodes = (
        valid.assign(_w=valid["買進金額合計"] * valid[return_column])
        .groupby(["分點", "標的股", "_episode"])
        .agg(
            事件數=("日期", "size"),
            起日=("日期", "min"),
            迄日=("日期", "max"),
            金額=("買進金額合計", "sum"),
            _num=("_w", "sum"),
        )
        .reset_index()
    )
    episodes["報酬"] = episodes["_num"] / episodes["金額"]
    return episodes.drop(columns=["_num"])


# ══════════════════════════════════════════════════════════════════════
# Step 5：超額報酬 —— 扣掉行情
# ══════════════════════════════════════════════════════════════════════

def attach_excess(events, trading_days, window, return_column):
    """
    基準＝同一標的、同一時間窗、「其他分點」的事件報酬中位數。

    辛耘漲兩倍的期間，買辛耘認購的分點幾乎都會贏。
    扣掉同期同標的其他分點的表現，才看得出誰真的比較強。
    同期沒有其他分點做同一檔的事件時，這一筆不計入超額統計
    （寧可少算，不要拿一個假基準去比）。

    舊版有兩個問題（2026-09-18 修正）：
      1. 依「標的股」分組順序算出結果，卻依事件表原本的列順序塞回去 ——
         事件表是按分點排序的，超額報酬會對到錯的事件。現在一律用索引對齊。
      2. 每筆事件都掃過同標的的全部事件（平方成長），台積電這種上千筆的標的很慢。
         現在先按日期排序，用二分搜尋只取時間窗內的同儕。
    """
    day_index = {day: i for i, day in enumerate(trading_days)}
    work = events[events[return_column].notna()][
        ["分點", "標的股", "日期", return_column]
    ].copy()
    work["_i"] = work["日期"].map(day_index)
    work = work[work["_i"].notna()].copy()
    work["_i"] = work["_i"].astype(int)
    work["超額"] = np.nan

    for _, group in work.groupby("標的股", sort=False):
        ordered = group.sort_values("_i")
        days = ordered["_i"].to_numpy()
        values = ordered[return_column].to_numpy(dtype=float)
        brokers = ordered["分點"].to_numpy()
        lo = np.searchsorted(days, days - window, side="left")
        hi = np.searchsorted(days, days + window, side="right")
        result = np.full(len(ordered), np.nan)
        for k in range(len(ordered)):
            window_values = values[lo[k]:hi[k]]
            peers = window_values[brokers[lo[k]:hi[k]] != brokers[k]]
            if peers.size:
                result[k] = values[k] - np.median(peers)
        work.loc[ordered.index, "超額"] = result
    return work[["分點", "標的股", "日期", "超額"]]


# ══════════════════════════════════════════════════════════════════════
# Step 2/4/6：統計
# ══════════════════════════════════════════════════════════════════════

def wilson_lower(wins, total, z=Z_95):
    """
    Wilson 95% 信賴區間下界。

    用它取代原始勝率當排序依據：樣本少的分點區間寬、下界自然低，
    不需要另外訂一個主觀的集中度懲罰係數。
    3 戰 3 勝的下界約 0.44，20 戰 15 勝的下界約 0.54 —— 後者才排得上去。
    """
    if total <= 0:
        return float("nan")
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z / denominator * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    )
    return max(centre - margin, 0.0)


def concentration(group, name_map):
    """HHI＝各標的買進金額佔比的平方和。1＝全押一檔；0.1＝大約分散在 10 檔。"""
    amounts = group.groupby("標的股")["買進金額合計"].sum().sort_values(ascending=False)
    total = amounts.sum()
    if total <= 0:
        return 0, float("nan"), float("nan"), "", 0.0, ""
    share = amounts / total
    hhi = float((share ** 2).sum())
    top3 = float(share.head(3).sum())

    main_code = str(amounts.index[0])
    main_name = name_map.get(main_code, "") or main_code
    main_share = float(share.iloc[0])
    top3_text = "、".join(
        f"{name_map.get(str(code), '') or code} {value:.0%}"
        for code, value in share.head(3).items()
    )
    return int(len(amounts)), hhi, top3, main_name, main_share, top3_text


def underlying_breakdown(group, return_column, name_map, top_n=5):
    """
    逐標的的次數與勝率，例如「台積電 12次 67%、辛耘 8次 75%」。

    專精型分點不該只被貼一個「參考性偏低」的評斷就算了 ——
    他只做那幾檔這件事本身就是最重要的資訊，把哪幾檔、各自幾次、各自勝率
    直接攤出來，看的人自己就能判斷能不能用。
    """
    parts = []
    stats = (
        group.groupby("標的股")
        .agg(
            次數=(return_column, "size"),
            勝率=(return_column, lambda s: (s > 0).mean()),
            金額=("買進金額合計", "sum"),
        )
        .sort_values("金額", ascending=False)
    )
    for code, row in stats.head(top_n).iterrows():
        label = name_map.get(str(code), "") or str(code)
        parts.append(f"{label} {int(row['次數'])}次 {row['勝率']:.0%}")
    if len(stats) > top_n:
        parts.append(f"…另 {len(stats) - top_n} 檔")
    return "、".join(parts)


def verdict(row):
    """
    把數字翻成一句人看得懂的判讀。

    「專精」不是扣分，是分類 —— 只做辛耘三年都對，很可能是產業鏈資訊優勢，
    那對那幾檔是最值錢的訊號，只是不能拿去買別的標的。
    所以專精型不給「參考性偏低」的評斷，直接攤開他做哪幾檔、各自勝率多少。

    真正該降級的只有兩種：數字不能信（樣本太少）、
    數字是真的但不是他的功勞（行情帶動）。這兩個優先於專精判定。
    """
    notes = []

    if row["有效episode數"] < 10:
        level = "低"
        notes.append(
            f"有效樣本僅 {row['有效episode數']} 次獨立判斷"
            f"（{row['事件數']} 筆事件多為同一波加碼），勝率不可信"
        )
    elif pd.notna(row["超額勝率"]) and row["超額勝率"] < 0.45:
        level = "低"
        notes.append(
            f"超額勝率僅 {row['超額勝率']:.0%} —— 同期做同一批標的的其他分點也在贏，"
            "贏的是行情不是選股"
        )
    elif row["相異標的數"] <= 3 or row["HHI"] >= 0.5 or row["前三大標的佔比"] >= 0.85:
        level = "專精"
        notes.append(
            f"只做 {row['相異標的數']} 檔標的，{row['主力標的']} 佔 "
            f"{row['主力標的佔比']:.0%}"
        )
        notes.append(f"逐標的勝率：{row['逐標的勝率']}")
        notes.append("對這幾檔可用，不要外推到他沒做過的標的")
        return level, "；".join(notes)
    elif pd.notna(row["跨標的一致性"]) and row["跨標的一致性"] < 0.5:
        level = "中"
        notes.append(
            f"分散在 {row['相異標的數']} 檔標的，但只有 "
            f"{row['跨標的一致性']:.0%} 的標的勝率過半，表現不穩定"
        )
    elif pd.isna(row["跨標的一致性"]):
        # 分散得夠開，但每檔標的的事件數都不足以單獨判斷勝率。
        # 這種情況不能當成「表現好」，也不是「集中度問題」，要分開講。
        level = "中"
        notes.append(
            f"分散在 {row['相異標的數']} 檔標的，但每檔的事件數都太少，"
            "無法逐標的驗證一致性"
        )
    else:
        level = "高"
        notes.append(
            f"分散在 {row['相異標的數']} 檔標的，"
            f"{row['跨標的一致性']:.0%} 的標的勝率過半"
        )
        if pd.notna(row["超額勝率"]):
            notes.append(f"扣掉同期同儕後超額勝率仍有 {row['超額勝率']:.0%}")

    notes.append(f"主要標的：{row['前三大標的']}")
    return level, "；".join(notes)


def summarize(events, episodes, excess, return_column, min_underlying_events,
              name_map, horizons=()):
    rows = []
    excess_map = excess.set_index(["分點", "標的股", "日期"])["超額"]
    events = events.join(
        excess_map, on=["分點", "標的股", "日期"], how="left"
    )

    for broker, group in events.groupby("分點"):
        scored = group[group[return_column].notna()]
        if scored.empty:
            continue

        episode_group = episodes[episodes["分點"] == broker]
        episode_wins = int((episode_group["報酬"] > 0).sum())
        episode_total = int(len(episode_group))

        underlying_count, hhi, top3, main_name, main_share, top3_text = (
            concentration(scored, name_map)
        )

        # 跨標的一致性：只看樣本夠的標的，否則 1 戰 1 勝也會算成 100%
        consistency_hits, consistency_base = 0, 0
        for _, sub in scored.groupby("標的股"):
            if len(sub) < min_underlying_events:
                continue
            consistency_base += 1
            if (sub[return_column] > 0).mean() > 0.5:
                consistency_hits += 1

        has_excess = scored[scored["超額"].notna()]

        rows.append({
            "分點": broker,
            "事件數": len(scored),
            "有效episode數": episode_total,
            "原始勝率": float((scored[return_column] > 0).mean()),
            "有效勝率": episode_wins / episode_total if episode_total else float("nan"),
            "Wilson下界": wilson_lower(episode_wins, episode_total),
            "相異標的數": underlying_count,
            "HHI": hhi,
            "前三大標的佔比": top3,
            "主力標的": main_name,
            "主力標的佔比": main_share,
            "前三大標的": top3_text,
            "逐標的勝率": underlying_breakdown(scored, return_column, name_map),
            "跨標的一致性": (
                consistency_hits / consistency_base if consistency_base else float("nan")
            ),
            "一致性樣本標的數": consistency_base,
            "超額勝率": (
                float((has_excess["超額"] > 0).mean()) if len(has_excess) else float("nan")
            ),
            "有基準事件數": len(has_excess),
            "中位報酬": float(scored[return_column].median()),
            # 不同持有天數的勝率一起列：權證有時間價值耗損，抱越久越吃虧，
            # 20 日勝率高但 60 日掉很多，代表這個分點的訊號只適合短打。
            **{
                f"勝率_{h}日": (
                    float((group[f"報酬_{h}"].dropna() > 0).mean())
                    if group[f"報酬_{h}"].notna().any() else float("nan")
                )
                for h in horizons if f"報酬_{h}" in group.columns
            },
            "買進金額合計": float(scored["買進金額合計"].sum()),
        })

    summary = pd.DataFrame(rows)
    if summary.empty:
        return summary

    verdicts = summary.apply(verdict, axis=1, result_type="expand")
    summary["參考性"] = verdicts[0]
    summary["判讀"] = verdicts[1]

    # 排序：先按參考性分層，同層內才比 Wilson 下界。
    # 否則一個「3 戰 3 勝、只做台積電」的分點還是會靠點估計爬到前面。
    # 專精排在中之後，但它是「另一種用途」不是「比較差」——
    # 輸出時會另外獨立成一張表，不跟通用型混在同一個排行裡比高下。
    order = {"高": 0, "中": 1, "專精": 2, "低": 3}
    summary["_order"] = summary["參考性"].map(order)
    summary = summary.sort_values(
        ["_order", "Wilson下界"], ascending=[True, False]
    ).drop(columns=["_order"])
    return summary.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="分點品質診斷")
    parser.add_argument("--horizon", type=int, default=20,
                        help="主要前瞻報酬天數（交易日）")
    parser.add_argument("--extra-horizons", default="5,10,60",
                        help="另外一併計算的天數，逗號分隔")
    parser.add_argument("--episode-gap", type=int, default=5,
                        help="同標的相隔幾個交易日內併成同一 episode")
    parser.add_argument("--benchmark-window", type=int, default=10,
                        help="超額報酬的基準取前後幾個交易日")
    parser.add_argument("--min-events", type=int, default=10,
                        help="分點至少要有幾筆事件才列入排行")
    parser.add_argument("--min-underlying-events", type=int, default=3,
                        help="計算跨標的一致性時，單一標的至少要幾筆事件")
    parser.add_argument("--excel", action="store_true", help="另外輸出 Excel")
    args = parser.parse_args()

    horizons = sorted({args.horizon} | {
        int(x) for x in args.extra_horizons.split(",") if x.strip()
    })
    return_column = f"報酬_{args.horizon}"

    print("=" * 78)
    print("🔬 分點品質診斷")
    print(f"   前瞻 {args.horizon} 個交易日｜episode 間隔 {args.episode_gap} 日"
          f"｜基準窗 ±{args.benchmark_window} 日")
    print("=" * 78)

    history = load_history()
    print(f"\n  分點累積庫：{len(history):,} 列"
          f"｜{history['日期'].min()} ~ {history['日期'].max()}")

    events = build_events(history)
    print(f"  ABCDE 事件：{len(events):,} 筆"
          f"｜分點 {events['分點'].nunique()}｜標的 {events['標的股'].nunique():,}")
    print("    級距分布：" + "｜".join(
        f"{k} {v:,}" for k, v in events["級距"].value_counts().sort_index().items()
    ))

    wanted = set(history["權證代號"]) | set(history["標的股"])
    gaps = ohlcv_market_gaps()
    if gaps:
        print(f"\n  ⚠️ OHLCV 有 {len(gaps)} 天只有單一市場的行情（{gaps[0][0]} ～ {gaps[-1][0]}）")
        print("     這些日子的價格會往前填，涉及的勝率可能失真。抓取流程下一輪會自動補齊。")
    ohlcv = load_ohlcv(wanted)
    trading_days = sorted(ohlcv["日期"].unique())
    print(f"  OHLCV：{len(ohlcv):,} 列｜交易日 {len(trading_days):,}")

    price_wide = build_price_lookup(ohlcv, trading_days)
    events = attach_returns(history, events, price_wide, trading_days, horizons)

    scored = events[events[return_column].notna()]
    print(f"  可評分事件：{len(scored):,} / {len(events):,}"
          f"（{len(scored) / max(len(events), 1):.1%}）")

    episodes = build_episodes(events, trading_days, args.episode_gap, return_column)
    print(f"  合併後 episode：{len(episodes):,}"
          f"（壓縮 {1 - len(episodes) / max(len(scored), 1):.1%}）")

    excess = attach_excess(events, trading_days, args.benchmark_window, return_column)

    # 標的代號 → 名稱：判讀要印「專做台積電」而不是「專做 2330」。
    name_map = {}
    if "標的名稱" in history.columns:
        pairs = history[["標的股", "標的名稱"]].astype(str)
        pairs = pairs[(pairs["標的股"] != "") & (pairs["標的名稱"] != "")]
        name_map = dict(zip(pairs["標的股"], pairs["標的名稱"]))

    summary = summarize(
        events, episodes, excess, return_column, args.min_underlying_events, name_map,
        horizons,
    )
    summary = summary[summary["事件數"] >= args.min_events]

    if summary.empty:
        print("\n  ⛔ 沒有分點達到最低事件數門檻。")
        return 1

    def pct(value, width=6, digits=0):
        return f"{value:>{width}.{digits}%}" if pd.notna(value) else f"{'-':>{width}}"

    general = summary[summary["參考性"].isin(["高", "中"])]
    focused = summary[summary["參考性"] == "專精"]
    weak = summary[summary["參考性"] == "低"]

    print(f"\n{'=' * 78}")
    print("📊 通用型分點（做得夠分散，訊號可以外推到新標的）")
    print("-" * 78)
    if general.empty:
        print("  （無）")
    else:
        print(f"  {'分點':<13}{'事件':>5}{'有效N':>6}{'原始':>7}"
              f"{'下界':>7}{'超額':>7}{'標的':>5}{'一致':>6}")
        for row in general.itertuples():
            print(
                f"  {row.分點:<13}{row.事件數:>5}{row.有效episode數:>6}"
                f"{pct(row.原始勝率, 7, 1)}{pct(row.Wilson下界, 7, 1)}"
                f"{pct(row.超額勝率, 7, 1)}{row.相異標的數:>5}"
                f"{pct(row.跨標的一致性, 6)}"
            )

    print(f"\n{'=' * 78}")
    print("🎯 專精型分點（只做特定幾檔 —— 不是比較差，是只能用在那幾檔）")
    print("-" * 78)
    if focused.empty:
        print("  （無）")
    else:
        for row in focused.itertuples():
            print(f"  {row.分點}｜{row.事件數} 筆事件／{row.有效episode數} 次獨立判斷"
                  f"｜整體勝率 {row.原始勝率:.0%}（下界 {row.Wilson下界:.0%}）")
            print(f"    做哪幾檔：{row.逐標的勝率}")
            print()

    print(f"{'=' * 78}")
    print("⚠️ 數字不可信的分點")
    print("-" * 78)
    if weak.empty:
        print("  （無）")
    else:
        for row in weak.itertuples():
            print(f"  {row.分點}：{row.判讀}")

    print(f"\n{'-' * 78}")
    print("  分層依據（優先序由上而下）：")
    print("    低    有效 episode < 10  → 獨立判斷次數太少，勝率不可信")
    print("    低    超額勝率 < 45%     → 同期同儕也在贏，贏的是行情不是選股")
    print("    專精  標的 ≤3／HHI ≥0.5  → 只做那幾檔，對那幾檔可用、不可外推")
    print("    中    跨標的一致性 < 50% → 有分散但表現不穩")
    print("    高    以上皆非")
    print("=" * 78)

    stamp = datetime.today().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"broker_quality_{stamp}.csv")
    summary.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n💾 {csv_path}")

    if args.excel:
        xlsx_path = os.path.join(OUTPUT_DIR, f"broker_quality_{stamp}.xlsx")
        by_grade = grade_breakdown(events, return_column, horizons)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            explanation(args, events, trading_days, gaps).to_excel(
                writer, sheet_name="說明", index=False
            )
            summary.to_excel(writer, sheet_name="分點排行", index=False)
            by_grade.to_excel(writer, sheet_name="分點×級距勝率", index=False)
            for sheet, frame in (("事件明細", events), ("Episode明細", episodes)):
                if len(frame) <= EXCEL_MAX_ROWS:
                    frame.to_excel(writer, sheet_name=sheet, index=False)
                else:
                    print(f"  ⚠️ {sheet} {len(frame):,} 列超過 Excel 上限，只輸出最近的部分")
                    frame.sort_values("日期" if "日期" in frame else frame.columns[0]).tail(
                        EXCEL_MAX_ROWS
                    ).to_excel(writer, sheet_name=sheet, index=False)
        print(f"💾 {xlsx_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
