"""一次性：抓 truth.csv 每檔到截圖日的日K（FinMind 公開資料），存成離線 fixture。"""
import csv, json, os, time, urllib.request
from datetime import date, timedelta
here = os.path.dirname(os.path.abspath(__file__))
for row in csv.DictReader(open(os.path.join(here, "truth.csv"), encoding="utf-8")):
    out = os.path.join(here, f"px_{row['code']}_{row['date']}.json")
    if os.path.exists(out):
        continue
    end = date.fromisoformat(row["date"]); start = end - timedelta(days=330)
    url = (f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={row['code']}"
           f"&start_date={start}&end_date={end}")
    data = json.load(urllib.request.urlopen(url, timeout=60)).get("data", [])
    json.dump(data, open(out, "w", encoding="utf-8"))
    print(row["code"], row["date"], len(data), data[-1]["date"] if data else "-", data[-1]["close"] if data else "-")
    time.sleep(1)
