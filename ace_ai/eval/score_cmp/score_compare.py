"""型態分數支撐修正：離線整批比較（舊版／A：近價量區＋合併＋文字／B：A＋條件式 MA5）。

資料：scratchpad/market_days.pkl（證交所／櫃買全市場日收盤，未還原權息；各版本吃同一份資料，只比差異）。
"""
import dataclasses, os, pickle, sys, time, json
import numpy as np
import pandas as pd

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import warrant_ai_tools as tools
import weekly_pick as wp

HERE = os.path.dirname(os.path.abspath(__file__))
N_STOCKS = int(sys.argv[1]) if len(sys.argv) > 1 else 500
N_DATES = int(sys.argv[2]) if len(sys.argv) > 2 else 8
STEP = 5
BARS = 120

days = pickle.load(open(os.path.join(HERE, "market_days.pkl"), "rb"))
recs = [dict(r, date=d) for d, rows in days.items() for r in rows]
raw = pd.DataFrame(recs)
raw["date"] = pd.to_datetime(raw["date"])
panel = {c: g.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
         .rename(columns=str.capitalize) for c, g in raw.groupby("stock_code")}
all_dates = sorted(raw["date"].unique())
as_of = [all_dates[-1 - i * STEP] for i in range(N_DATES)]
first = all_dates.index(as_of[-1])
ok = [c for c, df in panel.items() if len(df.loc[:as_of[-1]]) >= BARS and len(df) >= first + 1 + N_DATES * STEP - STEP]
value = {c: float((panel[c]["Close"] * panel[c]["Volume"]).iloc[-20:].mean()) for c in ok}
codes = sorted(ok, key=lambda c: -value[c])[:N_STOCKS]
if "3406" in panel and "3406" not in codes:
    codes.append("3406")
print(f"{len(codes)} 檔 × {len(as_of)} 日：{[pd.Timestamp(d).strftime('%m/%d') for d in as_of]}", flush=True)

kf = tools.core()
CUR = {}


def bundle(code, *a, **k):
    df = CUR[code]
    closed = kf.calculate_indicators(df.copy())
    closed["Close_prev"] = closed["Close"].shift(1)
    return {"df": closed, "closed_df": closed, "market": "", "intraday": {}, "daily_source": "offline",
            "data_gaps": [], "raw_closed_df": df, "corporate_actions": {},
            "input_fp": tools.frame_fingerprint(df), "source_fp": tools.analysis_fingerprint(tools.frame_fingerprint(df), {})}


tools._load_price_bundle = bundle
tools._stock_identity = lambda c: (str(c), "")
tools._kline_patterns = lambda *a, **k: {}

base = wp.WeeklyPickConfig()
OFF = dataclasses.replace(base, near_zone_support=False, support_merge_pct=0.0, ma5_limit_support=False, support_card_distance=False, deduction_path_score=False)
CFG = {   # 分項開關比較；A＝四項全開（MA5 關）、B＝A＋MA5
    "OLD": OFF,
    "NEAR": dataclasses.replace(OFF, near_zone_support=True),
    "MERGE": dataclasses.replace(OFF, support_merge_pct=1.0),
    "DIST": dataclasses.replace(OFF, support_card_distance=True),
    "PATH": dataclasses.replace(OFF, deduction_path_score=True),
    "A": dataclasses.replace(OFF, near_zone_support=True, support_merge_pct=1.0, support_card_distance=True, deduction_path_score=True),
    "B": dataclasses.replace(OFF, near_zone_support=True, support_merge_pct=1.0, support_card_distance=True, deduction_path_score=True, ma5_limit_support=True),
}

rows, fails, t0 = [], 0, time.time()
for d in as_of:
    day_codes = sorted([c for c in panel if len(panel[c].loc[:d]) >= BARS and panel[c].index[panel[c].index <= d][-1:].equals(pd.DatetimeIndex([d]))],   # 各截止日各自判資格
                       key=lambda c: -float((panel[c]['Close'] * panel[c]['Volume']).loc[d]))[:N_STOCKS]
    for code in day_codes:
        df = panel[code].loc[:d].iloc[-BARS:]
        if len(df) < BARS or df.index[-1] != d:
            continue
        CUR[code] = df
        tools.CACHE._data.clear()
        try:
            tech, vp, ex = tools.get_technical_analysis(code), tools.get_volume_profile(code), wp._technical_extras(code)
        except Exception as exc:
            fails += 1
            if fails <= 5: print(f"{code}: {type(exc).__name__}: {exc}", flush=True)
            continue
        row = {"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "code": code}
        for name, cfg in CFG.items():
            s = wp.score_pattern(tech, vp, ex, cfg)
            row[name] = s["score"]
            row[name + "_sup"] = " / ".join(i["note"] for i in s["items"] if i["component"] == "下方支撐")
        ma5 = (tech.get("ma_deduction") or {}).get("MA5") or {}
        row["ma5_limit"] = ma5.get("limit_proof") == "跌停也續揚"
        row["near_zone_below"] = any((z.get("price_low") or 1e18) <= (vp.get("close") or 0) for z in vp.get("near_volume_zones") or [])
        row["ma20_text"] = wp._direction_points(tech["ma_deduction"].get("MA20") or {}, 10)[1]
        rows.append(row)
    print(pd.Timestamp(d).strftime("%m/%d"), len(rows), f"{time.time()-t0:.0f}s", flush=True)

res = pd.DataFrame(rows)
res.to_pickle(os.path.join(HERE, "score_compare.pkl"))
print("失敗", fails)
