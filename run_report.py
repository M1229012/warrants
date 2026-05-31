import os
import time
import traceback
from pathlib import Path

import requests

from K_function_warrant_report_20260530 import generate_warrant_report, update_full_market_warrant_cache


def _parse_stock_codes() -> list[str]:
    """
    從環境變數 STOCK_CODES 讀取股票代號。
    支援：2408 或 2408,2330 或 2408，2330。
    """
    raw = os.getenv("STOCK_CODES", "2408")
    raw = raw.replace("，", ",").replace(" ", ",")
    codes = [x.strip() for x in raw.split(",") if x.strip()]
    return codes or ["2408"]


def _get_action_mode() -> str:
    """
    動作模式：
    - report：產生權證週報圖片
    - cache：抓取該股票全市場權證分點買賣超，寫入 Google Sheet 快取
    """
    mode = os.getenv("WARRANT_ACTION", os.getenv("ACTION_MODE", "report")).strip().lower()
    if mode in ("generate_report", "report", "image", "plot"):
        return "report"
    if mode in ("cache", "update_cache", "full_market_cache", "fetch_cache"):
        return "cache"
    print(f"⚠️ 未知 WARRANT_ACTION={mode}，改用 report")
    return "report"


def _get_discord_webhook_url() -> str:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL_TEST", "").strip()
    if webhook_url:
        print("✅ 使用 DISCORD_WEBHOOK_URL_TEST 推播 Discord")
        return webhook_url

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if webhook_url:
        print("✅ 使用 DISCORD_WEBHOOK_URL 推播 Discord")
        return webhook_url

    return ""


def _send_to_discord(image_path: Path, stock_code: str) -> None:
    webhook_url = _get_discord_webhook_url()
    if not webhook_url:
        print("ℹ️ 未設定 DISCORD_WEBHOOK_URL_TEST 或 DISCORD_WEBHOOK_URL，略過 Discord 推播")
        return

    content = (
        f"📊 {stock_code} 權證資金流週報\n"
        f"資訊僅供教育參考，非投資建議用途。"
    )

    with image_path.open("rb") as f:
        resp = requests.post(
            webhook_url,
            data={"content": content},
            files={"file": (image_path.name, f, "image/png")},
            timeout=60,
        )

    resp.raise_for_status()
    print(f"✅ 已推播到 Discord：{image_path.name}")


def _run_report(stock_codes: list[str], output_dir: Path) -> list[str]:
    failed = []
    print(f"🚀 開始產生權證週報：{stock_codes}")

    for stock_code in stock_codes:
        try:
            print(f"\n===== 產生 {stock_code} 權證週報 =====")
            buf = generate_warrant_report(stock_code)
            if buf is None:
                print(f"❌ {stock_code} 產圖失敗：generate_warrant_report 回傳 None")
                failed.append(stock_code)
                continue

            today = time.strftime("%Y%m%d_%H%M%S")
            image_path = output_dir / f"warrant_report_{stock_code}_{today}.png"
            image_path.write_bytes(buf.getvalue())
            print(f"✅ 圖片已輸出：{image_path}")

            _send_to_discord(image_path, stock_code)

        except Exception as exc:
            print(f"❌ {stock_code} 產生週報錯誤：{exc}")
            traceback.print_exc()
            failed.append(stock_code)

    return failed


def _run_cache(stock_codes: list[str], output_dir: Path) -> list[str]:
    failed = []
    print(f"🚀 開始抓取全市場權證分點快取：{stock_codes}")

    for stock_code in stock_codes:
        try:
            print(f"\n===== 更新 {stock_code} 全市場權證分點快取 =====")
            df = update_full_market_warrant_cache(stock_code)
            if df is None or df.empty:
                print(f"❌ {stock_code} 快取更新失敗或沒有資料")
                failed.append(stock_code)
                continue

            summary_path = output_dir / f"full_market_cache_{stock_code}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            summary_path.write_text(
                "\n".join([
                    f"股票代號：{stock_code}",
                    f"抓取筆數：{len(df):,}",
                    f"日期範圍：{df['Date'].min()} ~ {df['Date'].max()}",
                    "狀態：已寫入 Google Sheet 全市場分點快取",
                ]),
                encoding="utf-8",
            )
            print(f"✅ 快取摘要已輸出：{summary_path}")

        except Exception as exc:
            print(f"❌ {stock_code} 更新快取錯誤：{exc}")
            traceback.print_exc()
            failed.append(stock_code)

    return failed


def main() -> int:
    output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    stock_codes = _parse_stock_codes()
    action_mode = _get_action_mode()
    print(f"✅ 動作模式：{action_mode}")

    if action_mode == "cache":
        failed = _run_cache(stock_codes, output_dir)
    else:
        failed = _run_report(stock_codes, output_dir)

    if failed:
        print(f"⚠️ 以下代號執行失敗：{failed}")
        return 1

    print("✅ 全部任務完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
