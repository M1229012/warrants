import os
import time
import traceback
from pathlib import Path

import requests

from K_function_warrant_report_20260530 import generate_warrant_report


def _parse_stock_codes() -> list[str]:
    """
    從環境變數 STOCK_CODES 讀取要產生報告的股票代號。
    支援：
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
    先確認報告格式時可以不用設定 Discord。
    若有設定 DISCORD_WEBHOOK_URL_TEST，才會順便推到測試頻道。
    """
    return (
        os.getenv("DISCORD_WEBHOOK_URL_TEST", "").strip()
        or os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    )


def _send_to_discord(image_path: Path, stock_code: str) -> None:
    webhook_url = _get_discord_webhook_url()
    if not webhook_url:
        print("ℹ️ 未設定 DISCORD_WEBHOOK_URL_TEST，略過 Discord 推播，只上傳 GitHub Artifact")
        return

    content = (
        f""
    )

    with image_path.open("rb") as f:
        resp = requests.post(
            webhook_url,
            data={"content": content},
            files={"file": (image_path.name, f, "image/png")},
            timeout=60,
        )

    resp.raise_for_status()
    print(f"✅ 已推播到 Discord 測試頻道：{image_path.name}")


def main() -> int:
    output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    stock_codes = _parse_stock_codes()
    print(f"🚀 本次要產生的報告代號：{stock_codes}")

    failed = []

    for stock_code in stock_codes:
        try:
            print(f"\n========== 開始產生 {stock_code} 權證報告 ==========")

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
            print(f"❌ {stock_code} 發生錯誤：{exc}")
            traceback.print_exc()
            failed.append(stock_code)

    if failed:
        print(f"⚠️ 以下代號產生失敗：{failed}")
        return 1

    print("✅ 全部報告產生完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
