"""抓證交所／櫃買全市場日收盤（不用 FinMind），存成 scratchpad/market_days.pkl，供型態分數離線比較。"""
import os, sys, time, pickle
from datetime import date, timedelta
sys.path.insert(0, r"C:\Users\chen1_ukw0m7r\Downloads\新增資料夾\ace_ai_test")
import market_data

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_days.pkl")
data = pickle.load(open(OUT, "rb")) if os.path.exists(OUT) else {}
day, start = date(2026, 10, 7), date(2026, 1, 20)
while day >= start:
    key = day.isoformat()
    if day.weekday() < 5 and key not in data:
        rows = []
        for m in ("twse", "tpex"):
            for attempt in range(3):
                status, got, detail = market_data.fetch_market(day, m)
                if status in ("complete", "empty"):
                    break
                time.sleep(5)
            rows += got if status == "complete" else []
            time.sleep(1.2)
        data[key] = rows
        print(key, len(rows), flush=True)
        pickle.dump(data, open(OUT, "wb"))
    day -= timedelta(days=1)
print("done", sum(1 for v in data.values() if v))
