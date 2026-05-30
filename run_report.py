import os
import time
import traceback
from pathlib import Path

import requests

from K_function_warrant_report_20260530 import generate_warrant_report


def _parse_stock_codes() -> list[str]:
    """
    從環境變數 STOCK_CODES 讀取要產生報告的股票代號。
    支援格式：
    - 2408
    - 2408,2330,2317
    - 2408，2330，2317
    """
    raw = os.getenv("STOCK_CODES", "2408")
    raw = raw.replace("，", ",").replace(" ", ",")
    codes = [x.strip() for x in raw.split(",") if x.strip()]
    return codes or ["2408"]


def _get_discord_webhook_url() -> str:
    """
    Discord Webhook 讀取順序：
    1. DISCORD_WEBHOOK_URL_TEST：測試頻道使用，符合你目前 GitHub Secret 名稱
    2. DISCORD_WEBHOOK_URL：正式頻道備用
    """
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
        f"📊 {stock_code} 權證技術報告\n"
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


def main() -> int:
    output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    stock_codes = _parse_stock_codes()
    print(f"🚀 開始產生權證報告：{stock_codes}")

    failed = []
    for stock_code in stock_codes:
        try:
            print(f"\n===== 產生 {stock_code} 權證報告 =====")

            buf = generate_warrant_report(stock_code)
            if buf is None:
                print(f"❌ {stock_code} 產圖失敗：回傳 None")
                failed.append(stock_code)
                continue

            today = time.strftime("%Y%m%d")
            image_path = output_dir / f"warrant_report_{stock_code}_{today}.png"
            image_path.write_bytes(buf.getvalue())
            print(f"✅ 圖片已輸出：{image_path}")

            _send_to_discord(image_path, stock_code)

        except Exception as exc:
            print(f"❌ {stock_code} 發生錯誤：{exc}")
            traceback.print_exc()
            failed.append(stock_code)

    if failed:
        print(f"⚠️ 以下代號失敗：{failed}")
        return 1

    print("✅ 全部權證報告產生完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
