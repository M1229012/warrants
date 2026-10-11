"""三角畫線對答案（離線）：程式的上下緣 vs 使用者截圖的線，在截圖最後一天比較，單位＝ATR。

用法：python eval/tri_truth/score.py [模式...]     模式：baseline／envelope／balanced-v1（預設 baseline envelope）
奇數列＝調整組、偶數列＝驗收組；驗收組不拿來調參數，用來確認規則不是只對特定股票有效。
"""
import csv, json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PASS_ATR = 1.0          # 上下緣都在 1 ATR 內＝線畫對


def run_mode(mode):
    env = dict(os.environ, DISCORD_AI_KLINE_TRI_SELECTION_MODE=mode, DISCORD_AI_TRI_STATE_DB="0",
               DISCORD_AI_MARKET_CACHE_DB=os.path.join(tempfile.mkdtemp(), "m.sqlite3"), PYTHONUTF8="1")
    out = subprocess.run([sys.executable, "-X", "utf8", __file__, "--worker"], env=env, cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    rows = [json.loads(l[4:]) for l in out.stdout.splitlines() if l.startswith("ROW ")]
    if not rows:
        print(out.stderr[-2000:])
    return rows


def worker():
    sys.path.insert(0, ROOT)
    import pandas as pd, kline_patterns as kp
    for i, row in enumerate(csv.DictReader(open(os.path.join(HERE, "truth.csv"), encoding="utf-8"))):
        raw = pd.DataFrame(json.load(open(os.path.join(HERE, f"px_{row['code']}_{row['date']}.json"), encoding="utf-8")))
        raw = raw[raw.Trading_Volume > 0]
        df = pd.DataFrame({k: raw[v].to_numpy(float) for k, v in {"Open": "open", "High": "max", "Low": "min",
                           "Close": "close", "Volume": "Trading_Volume"}.items()}, index=pd.to_datetime(raw.date)).iloc[-144:]
        res = kp.detect(df, {"status": "ok", "items": []})
        tri = res.get("triangle")
        atr = float(kp._atr_prev(df["High"].to_numpy(float), df["Low"].to_numpy(float), df["Close"].to_numpy(float))[-1])
        end = len(df) - 1
        out = {"i": i, "code": row["code"], "name": row["name"], "date": row["date"], "atr": round(atr, 3),
               "truth": [float(row["upper_end"]), float(row["lower_end"]), row["status"]],
               "edge": round(min(abs(df["Close"].iloc[-1] - float(row["upper_end"])), abs(df["Close"].iloc[-1] - float(row["lower_end"]))) / atr, 2)}
        if tri:
            u = tri["upper"][0] * end + tri["upper"][1]
            d = tri["lower"][0] * end + tri["lower"][1]
            pos = tri.get("current_position")
            st = "break_up" if pos == "above" else "break_down" if pos == "below" else "inside"
            out.update(prog=[round(u, 2), round(d, 2), st], cand=bool(tri.get("candidate")),
                       err=[round(abs(u - out["truth"][0]) / atr, 2), round(abs(d - out["truth"][1]) / atr, 2)],
                       start=str(df.index[min(tri["anchors"]["upper"] + tri["anchors"]["lower"])].date()))
        print("ROW " + json.dumps(out, ensure_ascii=False), flush=True)


def main(modes):
    results = {m: run_mode(m) for m in modes}
    print(f"{'#':>2} {'股票':<10} {'日期':<10} {'答案上/下/狀態':<22}" + "".join(f"| {m:<34}" for m in modes))
    summary = {m: {"調整組": [0, 0], "驗收組": [0, 0]} for m in modes}
    for idx in range(len(results[modes[0]])):
        r0 = results[modes[0]][idx]
        line = f"{r0['i']:>2} {r0['code']+r0['name']:<10} {r0['date']:<10} {str(r0['truth']):<22}"
        for m in modes:
            r = results[m][idx]
            group = "調整組" if r["i"] % 2 == 0 else "驗收組"
            # 收盤貼線（離答案的線 0.3 ATR 內）時，型態內／越線都算對，避免只差一點點就判錯
            ok = "prog" in r and max(r["err"]) <= PASS_ATR and (r["prog"][2] == r["truth"][2] or r["edge"] <= 0.3)
            summary[m][group][0] += ok
            summary[m][group][1] += 1
            cell = (f"{'✅' if ok else '❌'} 誤差{r['err']} {r['prog'][2]}{'(候選)' if r['cand'] else ''}"
                    if "prog" in r else "❌ 無三角")
            line += f"| {cell:<34}"
        print(line)
    for m in modes:
        s = summary[m]
        print(f"【{m}】調整組 {s['調整組'][0]}/{s['調整組'][1]}｜驗收組 {s['驗收組'][0]}/{s['驗收組'][1]}"
              f"｜合計 {s['調整組'][0]+s['驗收組'][0]}/{s['調整組'][1]+s['驗收組'][1]}（上下緣都在 {PASS_ATR} ATR 內且狀態相同）")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()
    else:
        main(sys.argv[1:] or ["baseline", "envelope"])
