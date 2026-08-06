"""驗證 MoneyDJ ＋ MOPS 種子檔能不能取代 FinMind 的權證分點。

主程式要的是這十三欄：

    Date, branch, broker_code, warrant_code, warrant_name,
    underlying_code, underlying_name, buy_amount, sell_amount,
    net_amount, buy_shares, sell_shares, side

分別從三個地方湊出來：

    MoneyDJ API4   哪些分點碰過這檔權證（代號、名稱）
    MoneyDJ API5   每個 (權證, 分點) 的每日買賣股數與金額
    MOPS 種子檔     權證名稱、對應正股 → warrant_name / underlying_*

⚠️ 權證代號會被回收重複使用。03882T 在 2023-10 是「臺股指群益39售06」，
2026-07 重新掛牌變成「力積電中信61售01」，正股完全不同。所以查種子檔
一定要用「代號 ＋ 交易日落在 list_date~end_date 區間」，只用代號會對到錯的正股。

用法：

    python 分點工具/verify_moneydj_vs_finmind.py 2330            # 自動取最近交易日
    python 分點工具/verify_moneydj_vs_finmind.py 2330 2026-08-05

需要 FINMIND_API_TOKEN 才能對照；沒有 token 時只輸出 MoneyDJ 的結果供人工檢查。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "mops_warrant_identity_seed.csv.gz"

sys.path.insert(0, str(REPO_ROOT / "主程式"))
from warrant_sources import fetch_warrant_branch_daily  # noqa: E402

OUTPUT_COLUMNS = [
    "Date", "branch", "broker_code", "warrant_code", "warrant_name",
    "underlying_code", "underlying_name", "buy_amount", "sell_amount",
    "net_amount", "buy_shares", "sell_shares", "side",
]


def load_seed() -> pd.DataFrame:
    seed = pd.read_csv(
        SEED_PATH,
        compression="gzip",
        dtype={"code": str, "target_code": str},
    )
    for column in ("list_date", "end_date"):
        seed[column] = pd.to_datetime(seed[column], errors="coerce")
    return seed


def warrants_of(seed: pd.DataFrame, underlying: str, day: pd.Timestamp) -> pd.DataFrame:
    """某檔正股在指定日期「當時仍有效」的權證。

    end_date 缺值的視為仍在市，寧可多帶也不要漏。
    """
    rows = seed[seed["target_code"].astype(str) == str(underlying)]
    in_window = (rows["list_date"].isna() | (rows["list_date"] <= day)) & (
        rows["end_date"].isna() | (rows["end_date"] >= day)
    )
    return rows[in_window].drop_duplicates("code")


def identify(seed: pd.DataFrame, code: str, day: pd.Timestamp) -> dict:
    """反查權證屬於哪一期，回名稱與正股。"""
    rows = seed[seed["code"].astype(str) == str(code)]
    in_window = (rows["list_date"].isna() | (rows["list_date"] <= day)) & (
        rows["end_date"].isna() | (rows["end_date"] >= day)
    )
    hit = rows[in_window]
    if hit.empty:
        return {"warrant_name": "", "underlying_code": "", "underlying_name": ""}
    row = hit.iloc[-1]
    return {
        "warrant_name": str(row.get("name") or ""),
        "underlying_code": str(row.get("target_code") or ""),
        "underlying_name": str(row.get("target_name") or ""),
    }


def to_output(frame: pd.DataFrame, seed: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """把逐筆分點資料補上識別欄位，湊成主程式要的十三欄。"""
    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    records = []
    for row in frame.to_dict("records"):
        info = identify(seed, row["stock_id"], day)
        buy_shares = int(row.get("buy") or 0)
        sell_shares = int(row.get("sell") or 0)
        buy_amount = float(row.get("buy_amount", 0) or 0)
        sell_amount = float(row.get("sell_amount", 0) or 0)
        records.append(
            {
                "Date": row["date"],
                "branch": row.get("securities_trader", ""),
                "broker_code": row.get("securities_trader_id", ""),
                "warrant_code": row["stock_id"],
                **info,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "net_amount": buy_amount - sell_amount,
                "buy_shares": buy_shares,
                "sell_shares": sell_shares,
                # 主程式用 side 分買賣方；零股數的列在上游已經濾掉。
                "side": "buy" if buy_shares >= sell_shares else "sell",
            }
        )
    return pd.DataFrame(records, columns=OUTPUT_COLUMNS)


def finmind_branch_day(warrant_code: str, day: str) -> pd.DataFrame:
    token = (os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_TOKEN") or "").strip()
    if not token:
        return pd.DataFrame()
    response = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params={
            "dataset": "TaiwanStockWarrantTradingDailyReport",
            "data_id": warrant_code,
            # 這個資料集一次只給一天，帶 end_date 會被拒絕。
            "start_date": day,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=(8, 60),
    )
    rows = (response.json() or {}).get("data") or []
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    # FinMind 只給單價，金額要自己乘；MoneyDJ 則是直接給金額。
    frame["buy_amount"] = frame["buy"] * frame["price"]
    frame["sell_amount"] = frame["sell"] * frame["price"]
    frame = frame.rename(columns={"date": "date"})
    return frame


def main() -> int:
    underlying = (sys.argv[1] if len(sys.argv) > 1 else "2330").strip()
    seed = load_seed()

    day_arg = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    day = pd.Timestamp(day_arg) if day_arg else pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None).normalize()

    candidates = warrants_of(seed, underlying, day)
    if candidates.empty:
        print(f"❌ 種子檔查不到 {underlying} 在 {day.date()} 的有效權證")
        return 1
    codes = candidates["code"].tolist()
    print(f"🎯 正股 {underlying}｜{day.date()}｜種子檔有效權證 {len(codes)} 檔")

    # MoneyDJ 一次可取五日，之後再篩出目標日。
    dj_raw = fetch_warrant_branch_daily(codes, days=5)
    if dj_raw.empty:
        print("❌ MoneyDJ 完全沒有資料")
        return 1

    target = day_arg or dj_raw["date"].max()
    dj_day = dj_raw[dj_raw["date"] == target]
    print(f"📅 比對日：{target}")

    # MoneyDJ 的 buy/sell 是股數，金額另外算回來給 to_output 用。
    dj_day = dj_day.assign(
        buy_amount=dj_day["buy"] * dj_day["price"],
        sell_amount=dj_day["sell"] * dj_day["price"],
    )
    mine = to_output(dj_day, seed, day)

    print(f"\n=== MoneyDJ ＋ 種子檔 產出 {len(mine)} 筆 ===")
    if not mine.empty:
        print(mine.head(5).to_string(index=False))
        missing = mine[mine["underlying_code"] == ""]
        print(f"\n識別欄位補齊率：{(len(mine) - len(missing))}/{len(mine)}")
        wrong = mine[(mine["underlying_code"] != "") & (mine["underlying_code"] != underlying)]
        if not wrong.empty:
            print(f"⚠️ 有 {len(wrong)} 筆對到別的正股（代號回收沒處理好）：")
            print(wrong[["warrant_code", "warrant_name", "underlying_code"]].drop_duplicates().to_string(index=False))
        else:
            print(f"✅ 所有權證都正確對到正股 {underlying}")

    if not (os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_TOKEN")):
        print("\n⚠️ 未設 FINMIND_API_TOKEN，略過與 FinMind 的逐筆對照")
        return 0

    print("\n=== 與 FinMind 逐筆對照 ===")
    total_fin = total_dj = both = mismatch = 0
    checked = 0
    for code in codes:
        fin = finmind_branch_day(code, target)
        got = mine[mine["warrant_code"] == code]
        if fin.empty and got.empty:
            continue
        checked += 1
        fin_key = (
            fin.groupby("securities_trader_id")[["buy", "sell"]].sum().sort_index()
            if not fin.empty else pd.DataFrame(columns=["buy", "sell"])
        )
        dj_key = (
            got.groupby("broker_code")[["buy_shares", "sell_shares"]].sum().sort_index()
            if not got.empty else pd.DataFrame(columns=["buy_shares", "sell_shares"])
        )
        total_fin += len(fin_key)
        total_dj += len(dj_key)
        shared = sorted(set(fin_key.index) & set(dj_key.index))
        both += len(shared)
        only_fin = sorted(set(fin_key.index) - set(dj_key.index))
        only_dj = sorted(set(dj_key.index) - set(fin_key.index))

        bad = [
            b for b in shared
            if int(fin_key.loc[b, "buy"]) != int(dj_key.loc[b, "buy_shares"])
            or int(fin_key.loc[b, "sell"]) != int(dj_key.loc[b, "sell_shares"])
        ]
        mismatch += len(bad)
        if only_fin or only_dj or bad:
            print(f"  {code}: FinMind {len(fin_key)}／MoneyDJ {len(dj_key)}", end="")
            if only_fin:
                print(f"｜只有 FinMind: {only_fin}", end="")
            if only_dj:
                print(f"｜只有 MoneyDJ: {only_dj}", end="")
            if bad:
                print(f"｜數量不符: {bad}", end="")
            print()

    print(
        f"\n檢查 {checked} 檔權證｜FinMind 分點 {total_fin} 組｜MoneyDJ {total_dj} 組｜"
        f"共同 {both} 組｜買賣量不符 {mismatch} 組"
    )
    if total_fin == total_dj == both and mismatch == 0:
        print("✅ 完全一致，MoneyDJ ＋ 種子檔可以取代 FinMind 這份資料")
    else:
        print("⚠️ 有落差，切換前要先確認差在哪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
