"""費波逐日重播驗證（測試版 10-28）：過去每隔 STEP 個交易日，只用「當天以前」的日 K 呼叫 fib_levels.compute，
再往後看 HORIZON 根有沒有碰到價位、碰到後是反應還是突破；同時用同方向、相近距離的隨機價位當對照組。

用法（會連 FinMind 抓日 K，抓過的存在 eval/fib_replay/，之後可加 --offline 只用快取）：
    python fib_replay.py
    python fib_replay.py --codes 2330 2308 --step 5 --horizon 20

定義（ATR＝as-of 當天 fib_levels 用的前一日 ATR20）：
  碰到：之後 HORIZON 根內，上方價位 High ≥ 價位 − TOUCH_ATR×ATR；下方價位 Low ≤ 價位 ＋ TOUCH_ATR×ATR。
  碰到後 REACT_DAYS 根內，先發生哪一個：
    突破＝收盤越過價位 ≥ BREAK_ATR×ATR；
    反應＝從價位往回走 ≥ REACT_ATR×ATR（碰到當天看收盤，之後看最低／最高）；
    都沒有＝未定。
  對照組：每個費波價位配一個同方向、距離×0.6～1.4（避開 0.85～1.15）的隨機價位，用同一套規則評估。
  「費波比對照組好」＝碰到後反應率明顯高於對照組；兩者差不多代表這類價位沒有額外資訊。
資料：FinMind TaiwanStockPrice（未還原），再用 TaiwanStockDividendResult 的除權息前後參考價往前還原；
減資、分割不在這份資料裡，遇到會讓價格斷層，報告會列出單日跳空超過 25% 的檔數提醒。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import zlib
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import fib_levels

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "eval", "fib_replay")
DEFAULT_CODES = ["2330", "2308", "2317", "2454", "2382", "2303", "2357", "2379", "3231", "3711", "3037", "6669",
                 "1519", "1301", "2603", "2881", "2412", "3008", "3034", "4566", "6179", "1608", "2344", "2421",
                 "2467", "2474", "3715", "4576", "4977", "6173"]
STEP = 5            # 每隔幾個交易日重播一次
HORIZON = 20        # 之後看幾根有沒有碰到
REACT_DAYS = 5      # 碰到後看幾根判斷反應／突破
TOUCH_ATR = 0.1
REACT_ATR = 1.0
BREAK_ATR = 0.5
DIST_BUCKETS = ((0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, math.inf))
FINMIND = "https://api.finmindtrade.com/api/v4/data"


# ---------- 資料 ----------

def _token() -> str:
    for name in ("FINMIND_API_TOKEN", "FINMIND_API_TOKEN_2"):
        for part in str(os.getenv(name) or "").replace(";", ",").replace("\n", ",").split(","):
            part = part.strip().removeprefix("Bearer ").strip()
            if part:
                return part
    return ""


def _fetch(dataset: str, code: str, start: str, end: str) -> List[Dict[str, Any]]:
    import requests
    token = _token()
    r = requests.get(FINMIND, timeout=60, params={"dataset": dataset, "data_id": code, "start_date": start, "end_date": end},
                     headers={"Authorization": f"Bearer {token}"} if token else {})
    body = r.json()
    if body.get("status") not in (200, None):
        raise RuntimeError(f"{dataset} {code}：{body.get('msg') or body.get('status')}")
    return body.get("data") or []


def adjust_dividends(df: pd.DataFrame, rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """除權息日之前的價格乘上（除權息參考價／除權息前收盤），由近到遠累乘；成交量不動。"""
    out = df.copy()
    cols = [c for c in ("Open", "High", "Low", "Close") if c in out]
    for row in sorted(rows, key=lambda x: x.get("date", ""), reverse=True):
        before, after = float(row.get("before_price") or 0), float(row.get("after_price") or 0)
        if before <= 0 or after <= 0 or after >= before:
            continue
        mask = out.index < pd.Timestamp(row["date"])
        out.loc[mask, cols] = out.loc[mask, cols] * (after / before)
    return out


def load_prices(code: str, days: int, offline: bool = False) -> pd.DataFrame:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"px_{code}.json")
    if os.path.exists(path):
        cached = json.load(open(path, encoding="utf-8"))
    elif offline:
        raise FileNotFoundError(f"{code} 沒有快取（拿掉 --offline 先抓一次）")
    else:
        end = date.today()
        start = (end - timedelta(days=days)).isoformat()
        cached = {"price": _fetch("TaiwanStockPrice", code, start, end.isoformat()),
                  "dividend": _fetch("TaiwanStockDividendResult", code, start, end.isoformat())}
        json.dump(cached, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        time.sleep(1)
    d = pd.DataFrame(cached["price"])
    if d.empty:
        raise ValueError(f"{code} 沒有日 K")
    d = d[d["Trading_Volume"] > 0]
    df = pd.DataFrame({"Open": d["open"].astype(float).values, "High": d["max"].astype(float).values,
                       "Low": d["min"].astype(float).values, "Close": d["close"].astype(float).values,
                       "Volume": d["Trading_Volume"].astype(float).values}, index=pd.to_datetime(d["date"]))
    return adjust_dividends(df.sort_index(), cached.get("dividend") or [])


# ---------- 單一價位的後續結果 ----------

def outcome(h, l, c, t: int, price: float, side: int, atr: float,
            horizon: int = HORIZON, react_days: int = REACT_DAYS) -> Dict[str, Any]:
    """side＝+1 上方壓力、−1 下方支撐。只看 t 之後的 K 棒。"""
    last = len(c) - 1
    tol = TOUCH_ATR * atr
    touch = next((k for k in range(t + 1, min(t + horizon, last) + 1)
                  if (h[k] >= price - tol if side > 0 else l[k] <= price + tol)), None)
    if touch is None:
        return {"touched": False, "touch_days": None, "result": ""}
    if touch + react_days > last:
        return {"touched": True, "touch_days": touch - t, "result": "資料不足"}
    result = "未定"
    for j in range(touch, touch + react_days + 1):
        if (c[j] - price) * side >= BREAK_ATR * atr:
            result = "突破"
            break
        far = c[j] if j == touch else (l[j] if side > 0 else h[j])   # 碰到當天高低點的先後不知道，只看收盤
        if (price - far) * side >= REACT_ATR * atr:
            result = "反應"
            break
    return {"touched": True, "touch_days": touch - t, "result": result}


def _control_factor(key: str) -> float:
    rng = np.random.default_rng(zlib.crc32(key.encode("utf-8")))
    while True:
        f = float(rng.uniform(0.6, 1.4))
        if abs(f - 1.0) >= 0.15:
            return f


# ---------- 重播 ----------

def replay_code(code: str, df: pd.DataFrame, step: int = STEP, horizon: int = HORIZON,
                react_days: int = REACT_DAYS) -> List[Dict[str, Any]]:
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    rows: List[Dict[str, Any]] = []
    first = fib_levels.HISTORY - 1               # 前面要有完整一年，結果才跟 Bot 實際算法一致
    stop = len(df) - 1 - horizon - react_days    # 後面要留足觀察期，避免把「還沒發生」算成沒碰到
    for t in range(first, stop + 1, step):
        try:
            res = fib_levels.compute(df.iloc[:t + 1])   # 只給當天以前的資料
        except ValueError:
            continue
        atr, close, day = res["atr"], res["close"], df.index[t].strftime("%Y-%m-%d")
        for n, lv in enumerate(res["levels"]):
            side = 1 if lv["above"] else -1
            dist = abs(lv["price"] - close) / atr
            area = res["areas"][lv["area"]] if lv.get("area") is not None else None
            base = {"code": code, "date": day, "mode": res["mode"], "widened": res["widened"],
                    "label": lv["label"], "kind": lv["kind"], "side": side, "ai": lv["ai"],
                    "tentative": lv["tentative"], "conditional": lv["conditional"], "watch": lv["watch"], "tight": bool(lv.get("tight")),
                    "confluence": bool(lv["confluence"]), "area_size": len(area["labels"]) if area else 1,
                    "atr": atr, "close": close}
            rows.append(dict(base, control=False, price=lv["price"], dist_atr=dist,
                             **outcome(h, l, c, t, lv["price"], side, atr, horizon, react_days)))
            if lv["watch"]:
                continue   # 現價附近不配對照組（幾乎必碰，比不出差異）
            f = _control_factor(f"{code}|{day}|{n}")
            price = close + side * dist * f * atr
            if price > 0:
                rows.append(dict(base, control=True, price=price, dist_atr=dist * f,
                                 **outcome(h, l, c, t, price, side, atr, horizon, react_days)))
    return rows


def dedup(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一檔、同名稱、同價位的費波價位會連續好幾個重播日重複出現，只留第一次（對照組跟著它的母價位）。"""
    seen, out, keep = set(), [], False
    for r in rows:
        if not r["control"]:
            key = (r["code"], r["label"], round(r["price"], 2))
            keep = key not in seen
            seen.add(key)
        if keep:
            out.append(r)
    return out


# ---------- 統計 ----------

def _wilson(k: int, n: int) -> str:
    if n == 0:
        return "—"
    p, z = k / n, 1.96
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return f"{max(0, mid - half):.0%}～{min(1, mid + half):.0%}"


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    touched = [r for r in rows if r["touched"]]
    judged = [r for r in touched if r["result"] != "資料不足"]
    react = sum(r["result"] == "反應" for r in judged)
    brk = sum(r["result"] == "突破" for r in judged)
    return {"n": len(rows), "touch": len(touched) / len(rows) if rows else float("nan"),
            "judged": len(judged), "react": react / len(judged) if judged else float("nan"),
            "break": brk / len(judged) if judged else float("nan"), "react_ci": _wilson(react, len(judged)),
            "days": float(np.median([r["touch_days"] for r in touched])) if touched else float("nan")}


def _bucket(d: float) -> str:
    for lo, hi in DIST_BUCKETS:
        if lo <= d < hi:
            return f"{lo:g}～{hi:g} ATR" if hi != math.inf else f"{lo:g} ATR 以上"
    return "0.5 ATR 內"


GROUPS = [
    ("全部（不含現價附近）", lambda r: not r["watch"]),
    ("交給 AI", lambda r: r["ai"]),
    ("不交給 AI", lambda r: not r["ai"] and not r["watch"]),
    ("上方壓力", lambda r: r["side"] > 0 and not r["watch"]),
    ("下方支撐", lambda r: r["side"] < 0 and not r["watch"]),
    ("回撤／反彈", lambda r: r["kind"] == "retrace" and not r["watch"]),
    ("前波高低點", lambda r: r["kind"] == "prior" and not r["watch"]),
    ("延伸", lambda r: r["kind"] == "extend" and not r["watch"]),
    ("整理區等幅", lambda r: r["kind"] == "box" and not r["watch"]),
    ("整理區上下緣", lambda r: r["kind"] == "box_edge" and not r["watch"]),
    ("下跌支撐（大段／前低／等幅）", lambda r: r["kind"] == "support" and not r["watch"]),
    ("暫定（未確認錨點）", lambda r: r["tentative"] and not r["watch"]),
    ("錨點已確認", lambda r: not r["tentative"] and not r["watch"]),
    ("突破後條件目標", lambda r: r["conditional"] and not r["watch"]),
    ("交給 AI｜上方", lambda r: r["ai"] and r["side"] > 0),
    ("交給 AI｜下方", lambda r: r["ai"] and r["side"] < 0),
    ("有緊密共振", lambda r: r["tight"] and not r["watch"]),
    ("只有附近共振", lambda r: r["confluence"] and not r["tight"] and not r["watch"]),
    ("沒有共振", lambda r: not r["confluence"] and not r["watch"]),
    ("一區多個依據", lambda r: r["area_size"] >= 2 and not r["watch"]),
    ("模式：創高延伸", lambda r: r["mode"] == "extend" and not r["watch"]),
    ("模式：拉回", lambda r: r["mode"] == "pullback" and not r["watch"]),
    ("模式：反彈", lambda r: r["mode"] == "rebound" and not r["watch"]),
    ("模式：下跌低點未確認", lambda r: r["mode"] == "falling" and not r["watch"]),
    ("放寬到一年才找到", lambda r: r["widened"] and not r["watch"]),
] + [(f"距離 {_bucket(lo)}", (lambda lo, hi: lambda r: not r["watch"] and lo <= r["dist_atr"] < hi)(lo, hi))
     for lo, hi in DIST_BUCKETS]


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """每一組：費波價位 vs 它們各自配對的對照組（對照組沿用母價位的分組屬性，距離沿用母價位的分組）。"""
    out = []
    for name, keep in GROUPS:
        fib, ctrl = [], []
        parent = None
        for r in rows:   # 對照組緊跟在母價位後面
            if not r["control"]:
                parent = r
                if keep(r):
                    fib.append(r)
            elif parent is not None and keep(parent):
                ctrl.append(r)
        out.append({"group": name, "fib": _stats(fib), "control": _stats(ctrl)})
    watch = [r for r in rows if r["watch"] and not r["control"]]
    out.append({"group": "現價附近觀察位（無對照組）", "fib": _stats(watch), "control": _stats([])})
    return out


def _pct(x: float) -> str:
    return "—" if x != x else f"{x:.0%}"


def _num(x: float) -> str:
    return "—" if x != x else f"{x:.0f}"


def report(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    lines = [f"# 費波逐日重播驗證（{meta['generated']}）", "",
             f"- 股票：{meta['codes_ok']} 檔（失敗 {len(meta['failed'])} 檔：{'、'.join(meta['failed']) or '無'}）",
             f"- 重播區間：{meta['first_date']} ～ {meta['last_date']}，每 {meta['step']} 個交易日一次，共 {meta['asof']} 個重播日",
             f"- 碰到：之後 {meta['horizon']} 根內碰到價位（容差 {TOUCH_ATR:g} ATR）",
             f"- 碰到後 {meta['react_days']} 根內：收盤越過 {BREAK_ATR:g} ATR＝突破；往回走 {REACT_ATR:g} ATR＝反應；都沒有＝未定",
             "- 對照組：同方向、距離 ×0.6～1.4 的隨機價位，用同一套規則評估",
             "- 樣本已去重：同一檔、同名稱、同價位只算第一次出現",
             f"- 可能有減資／分割斷層（單日跳空 > 25%）的檔：{'、'.join(meta['jumps']) or '無'}", ""]

    def table(title, data):
        lines.extend([f"## {title}", "",
                      "| 分組 | 價位數 | 碰到率 | 碰到後反應率（95% 區間） | 突破率 | 碰到中位天數 | 對照組碰到率 | 對照組反應率 | 反應率差 |",
                      "|---|---|---|---|---|---|---|---|---|"])
        for g in data:
            f, k = g["fib"], g["control"]
            if f["n"] == 0:
                continue
            diff = f["react"] - k["react"] if k["judged"] and f["judged"] else float("nan")
            lines.append(f"| {g['group']} | {f['n']} | {_pct(f['touch'])} | {_pct(f['react'])}（{f['react_ci']}，n={f['judged']}） | "
                         f"{_pct(f['break'])} | {_num(f['days'])} | {_pct(k['touch'])} | {_pct(k['react'])} | "
                         f"{'—' if diff != diff else f'{diff:+.0%}'} |")
        lines.append("")

    table("全部重播日", summarize(rows))
    dates = sorted({r["date"] for r in rows})
    if len(dates) >= 4:   # 前後兩半各自算，結果方向一致才比較可信
        mid = dates[len(dates) // 2]
        key = {"全部（不含現價附近）", "交給 AI", "不交給 AI", "上方壓力", "下方支撐", "有緊密共振", "暫定（未確認錨點）"}
        table(f"前半段（{dates[0]}～{mid} 之前）", [g for g in summarize([r for r in rows if r["date"] < mid]) if g["group"] in key])
        table(f"後半段（{mid}～{dates[-1]}）", [g for g in summarize([r for r in rows if r["date"] >= mid]) if g["group"] in key])
    lines += ["## 怎麼看", "",
              "- 反應率差為正、而且前後兩半方向一致，才代表這類費波價位比隨機價位更容易出現反應。",
              "- 95% 區間很寬（樣本少）的分組先不要下結論。",
              "- 碰到率主要由距離決定，不能拿來比較好壞；比較時看「碰到後反應率」與對照組的差距。",
              "- 這是技術參考的歷史統計，不代表未來一定會有同樣的反應。"]
    return "\n".join(lines) + "\n"


def run(codes: List[str], days: int, step: int, horizon: int, react_days: int, offline: bool) -> str:
    all_rows, failed, jumps = [], [], []
    for code in codes:
        try:
            df = load_prices(code, days, offline)
            if (df["Close"].pct_change().abs() > 0.25).any():
                jumps.append(code)
            rows = replay_code(code, df, step, horizon, react_days)
            all_rows += rows
            print(f"{code}：{sum(not r['control'] for r in rows)} 個價位", flush=True)
        except Exception as exc:   # 單檔失敗不擋整批
            failed.append(code)
            print(f"{code} 失敗：{exc}", flush=True)
    rows = dedup(all_rows)
    dates = sorted({r["date"] for r in rows})
    meta = {"generated": date.today().isoformat(), "codes_ok": len(codes) - len(failed), "failed": failed,
            "first_date": dates[0] if dates else "-", "last_date": dates[-1] if dates else "-",
            "step": step, "asof": len(dates), "horizon": horizon, "react_days": react_days, "jumps": jumps}
    text = report(rows, meta)
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    with open(os.path.join(OUT_DIR, f"report_{stamp}.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, f"rows_{stamp}.csv"), index=False, encoding="utf-8-sig")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="費波逐日重播驗證")
    parser.add_argument("--codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--days", type=int, default=1100, help="抓幾個日曆天的日 K（預設約三年，前一年只當計算暖機）")
    parser.add_argument("--step", type=int, default=STEP)
    parser.add_argument("--horizon", type=int, default=HORIZON)
    parser.add_argument("--react-days", type=int, default=REACT_DAYS)
    parser.add_argument("--offline", action="store_true", help="只用 eval/fib_replay 的快取，不連網")
    args = parser.parse_args()
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(run(args.codes, args.days, args.step, args.horizon, args.react_days, args.offline))
    print(f"報告與明細已存到 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
