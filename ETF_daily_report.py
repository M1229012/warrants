from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from report_generator import generate_reports

ROOT = Path(__file__).resolve().parent
UPSTREAM_DIR = ROOT / ".cache" / "TWActiveETFCrawler"
UPSTREAM_REPO = os.getenv(
    "ACTIVE_ETF_CRAWLER_REPO",
    "https://github.com/SheenArtem/TWActiveETFCrawler.git",
)
LOCAL_DB = ROOT / "data" / "etf_holdings.db"


def run(cmd, cwd=None):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, check=True)


def ensure_upstream():
    UPSTREAM_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not UPSTREAM_DIR.exists():
        run(["git", "clone", "--depth", "1", UPSTREAM_REPO, str(UPSTREAM_DIR)])
    else:
        run(["git", "fetch", "--depth", "1", "origin", "main"], cwd=UPSTREAM_DIR)
        run(["git", "reset", "--hard", "origin/main"], cwd=UPSTREAM_DIR)


def find_upstream_db() -> Path:
    candidates = [
        UPSTREAM_DIR / "data" / "etf_holdings.db",
        UPSTREAM_DIR / "etf_holdings.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    found = list(UPSTREAM_DIR.rglob("*.db"))
    if not found:
        raise FileNotFoundError("抓取完成後找不到 SQLite DB")
    return found[0]


def validate_db(db_path: Path):
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        need = {"etf_list", "holdings"}
        missing = need - tables
        if missing:
            raise RuntimeError(f"資料庫缺少必要資料表：{sorted(missing)}")
    finally:
        con.close()


def update_data():
    ensure_upstream()
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=UPSTREAM_DIR)

    # 某些投信來源需要 Playwright；若環境已安裝就補 Chromium。
    try:
        run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=UPSTREAM_DIR)
    except Exception as exc:
        print(f"⚠️ Playwright Chromium 安裝失敗，繼續嘗試其他來源：{exc}")

    run([sys.executable, "main.py", "--all"], cwd=UPSTREAM_DIR)

    source_db = find_upstream_db()
    validate_db(source_db)
    LOCAL_DB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, LOCAL_DB)
    print(f"✅ 最新 ETF DB：{LOCAL_DB}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true", help="只用現有 data/etf_holdings.db 產圖")
    ap.add_argument("--top", type=int, default=20, help="共識排行顯示筆數")
    args = ap.parse_args()

    if not args.skip_fetch:
        update_data()

    if not LOCAL_DB.exists():
        raise FileNotFoundError("找不到 data/etf_holdings.db")

    paths = generate_reports(LOCAL_DB, ROOT / "output", top_n=args.top)
    print("✅ 完成：")
    for p in paths:
        print("  -", p)


if __name__ == "__main__":
    main()
