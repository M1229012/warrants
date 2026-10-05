"""富邦 zco 並行抓取壓力測試（只讀網頁，不寫資料庫）：連線數 × 每條間隔。"""
import os, sys, time, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, r"C:\Users\chen1_ukw0m7r\Downloads\新增資料夾\ace_ai_test")
os.environ["DISCORD_AI_SPOT_FETCHER"] = "http"
import spot_chip

ROUNDS = [(3, .5, "2317"), (6, .5, "2454"), (6, .2, "2303"), (12, .2, "3231"), (12, .5, "2382")]
if len(sys.argv) == 4:   # 指定單輪：python spot_rate_test.py 連線數 間隔秒 代號
    ROUNDS = [(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3])]
DAYS = [d for d in (date(2026, 10, 2) - timedelta(days=i) for i in range(365)) if d.weekday() < 5][:250]


def run(workers, gap, code):
    stats, lock, started = Counter(), threading.Lock(), time.time()
    with spot_chip.open_source() as fetch:
        def one(day):
            text = day.strftime("%Y-%m-%d")
            try:
                status = spot_chip.validate_spot_snapshot(fetch(code, text), code, text)[0]
            except Exception as exc:
                status = "exception:" + type(exc).__name__
            with lock:
                stats[status] += 1
            time.sleep(gap)
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, DAYS))
    spent = time.time() - started
    bad = stats["blocked"] + sum(v for k, v in stats.items() if k.startswith("exception"))
    print(f"連線{workers:>2}｜間隔{gap}｜{code}｜{spent:5.1f}秒｜{len(DAYS) / spent:4.1f} 次/秒｜{dict(stats)}", flush=True)
    return bad / len(DAYS)


for i, (workers, gap, code) in enumerate(ROUNDS):
    if i:
        time.sleep(60)
    if run(workers, gap, code) > 0.02:
        print("⚠️ 被擋或錯誤超過 2%，停止更高設定（冷卻 2 分鐘）")
        time.sleep(120)
        break
