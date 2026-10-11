"""三角開發：7 檔使用者手繪標準答案，印出程式選的上下緣錨點日期。資料快取在 scratchpad（只抓一次）。"""
import json, os, sys, importlib
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r"C:\Users\chen1_ukw0m7r\Downloads\新增資料夾\ace_ai_test")
import kline_patterns as kp

CODES = (sys.argv[3].split(",") if len(sys.argv) > 3 else ["2344", "1608", "2421", "2467", "3715", "4576", "4977"])


def load(code):
    path = os.path.join(HERE, f"px_{code}.json")
    if not os.path.exists(path):
        r = requests.get("https://api.finmindtrade.com/api/v4/data", timeout=30,
                         params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": "2025-09-01", "end_date": "2026-10-05"})
        json.dump(r.json()["data"], open(path, "w"))
    d = pd.DataFrame(json.load(open(path)))
    d = d[d["Trading_Volume"] > 0]
    df = pd.DataFrame({"Open": d["open"].values, "High": d["max"].values, "Low": d["min"].values,
                       "Close": d["close"].values, "Volume": d["Trading_Volume"].values},
                      index=pd.to_datetime(d["date"]))
    return df


bars = int(sys.argv[1]) if len(sys.argv) > 1 else 250
for code in CODES:
    df = load(code).iloc[-bars:]
    importlib.reload(kp)
    end = len(df) - 1
    atr = kp._atr_prev(df["High"].to_numpy(float), df["Low"].to_numpy(float), df["Close"].to_numpy(float))
    t = kp.user_triangle(df, atr, end)
    if not t:
        print(code, "無三角")
        continue
    day = lambda i: df.index[i].strftime("%m-%d")
    print(code, t["kind"], "上緣", [day(i) for i in t["anchors"]["upper"]], f"斜率{t['upper'][0]:.3f}",
          "下緣", [day(i) for i in t["anchors"]["lower"]], f"斜率{t['lower'][0]:.3f}", t.get("gap", ""))

if len(sys.argv) == 3:   # 除錯：python tri_dev.py 250 1608 → 列出候選上緣與下緣
    import numpy as np
    df = load(sys.argv[2]).iloc[-bars:]; end = len(df) - 1
    H, L, O, C = (df[k].to_numpy(float) for k in ("High", "Low", "Open", "Close"))
    A = kp._atr_prev(H, L, C); A = np.where(np.isnan(A), np.nanmedian(A), A)
    last = end - 1; lo = max(14, last - kp.TRI_DAYS); ref = float(A[last]); day = lambda i: df.index[i].strftime("%m-%d")
    top, bot = np.maximum(O, C), np.minimum(O, C)
    for name, args in (("上緣候選", (H, top, L, C, A, lo, last, 1, ref)), ("下緣候選", (L, bot, H, C, A, lo, last, -1, ref))):
        print(name, [([day(i) for i in r["a"]], round(r["s"], 3), r["score"], r["density"]) for r in kp._tri_lines(*args)][:10])
