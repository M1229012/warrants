#!/usr/bin/env python3
"""下載 FinMind 融資維持率與上市／櫃買指數，輸出 CSV 與 SVG 折線圖。

資料口徑：
1. TaiwanTotalExchangeMarginMaintenance 只有「全市場大盤融資維持率」一欄，
   FinMind 並未提供可直接拆分的上市、上櫃融資維持率。
2. 上市與櫃買走勢使用 TaiwanStockTotalReturnIndex 的 TAIEX、TPEx，
   並各自正規化為起始日 = 100，方便比較。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Iterable, Sequence


API_URL = "https://api.finmindtrade.com/api/v4/data"
DEFAULT_START = "2025-01-01"
DEFAULT_END = "2026-07-28"
MARGIN_DATASET = "TaiwanTotalExchangeMarginMaintenance"
INDEX_DATASET = "TaiwanStockTotalReturnIndex"


class FinMindError(RuntimeError):
    """FinMind API 回傳錯誤或資料格式不符。"""


@dataclass(frozen=True)
class Point:
    day: date
    value: float


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期格式必須是 YYYY-MM-DD，收到：{value}"
        ) from exc


def fetch_dataset(
    dataset: str,
    start_date: date,
    end_date: date,
    token: str,
    data_id: str | None = None,
    timeout: int = 45,
) -> list[dict]:
    params = {
        "dataset": dataset,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    if data_id:
        params["data_id"] = data_id

    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "finmind-margin-chart/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("msg", body)
        except json.JSONDecodeError:
            detail = body
        raise FinMindError(f"FinMind HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FinMindError(f"無法連線至 FinMind：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise FinMindError("FinMind 回傳的內容不是有效 JSON。") from exc

    if payload.get("status") not in (None, 200):
        raise FinMindError(str(payload.get("msg") or payload))
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FinMindError("FinMind 回傳缺少 data 陣列。")
    return rows


def rows_to_points(
    rows: Iterable[dict],
    value_field: str,
    start_date: date,
    end_date: date,
) -> list[Point]:
    by_date: dict[date, float] = {}
    for row in rows:
        try:
            day = parse_iso_date(str(row["date"]))
            value = float(row[value_field])
        except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError) as exc:
            raise FinMindError(
                f"資料缺少 date 或 {value_field}，範例列：{row}"
            ) from exc
        if start_date <= day <= end_date and math.isfinite(value):
            by_date[day] = value
    return [Point(day, by_date[day]) for day in sorted(by_date)]


def normalize(points: Sequence[Point]) -> list[Point]:
    if not points:
        return []
    base = points[0].value
    if base == 0:
        raise FinMindError("指數起始值為 0，無法正規化。")
    return [Point(point.day, point.value / base * 100.0) for point in points]


def merge_for_csv(
    margin: Sequence[Point],
    taiex: Sequence[Point],
    tpex: Sequence[Point],
) -> list[dict[str, str]]:
    margin_map = {p.day: p.value for p in margin}
    taiex_map = {p.day: p.value for p in taiex}
    tpex_map = {p.day: p.value for p in tpex}
    taiex_norm = {p.day: p.value for p in normalize(taiex)}
    tpex_norm = {p.day: p.value for p in normalize(tpex)}
    all_days = sorted(set(margin_map) | set(taiex_map) | set(tpex_map))

    def fmt(mapping: dict[date, float], day: date) -> str:
        value = mapping.get(day)
        return "" if value is None else f"{value:.6f}".rstrip("0").rstrip(".")

    return [
        {
            "date": day.isoformat(),
            "margin_maintenance_pct": fmt(margin_map, day),
            "taiex_total_return_index": fmt(taiex_map, day),
            "tpex_total_return_index": fmt(tpex_map, day),
            "taiex_normalized": fmt(taiex_norm, day),
            "tpex_normalized": fmt(tpex_norm, day),
        }
        for day in all_days
    ]


def write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "margin_maintenance_pct",
        "taiex_total_return_index",
        "tpex_total_return_index",
        "taiex_normalized",
        "tpex_normalized",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _ticks(low: float, high: float, count: int = 5) -> list[float]:
    if low == high:
        return [low]
    step = (high - low) / (count - 1)
    return [low + step * i for i in range(count)]


def _range(points: Sequence[Point], include: float | None = None) -> tuple[float, float]:
    values = [point.value for point in points]
    if include is not None:
        values.append(include)
    if not values:
        return (0.0, 1.0)
    low, high = min(values), max(values)
    if low == high:
        pad = max(abs(low) * 0.05, 1.0)
    else:
        pad = (high - low) * 0.08
    return low - pad, high + pad


def render_svg(
    path: Path,
    margin: Sequence[Point],
    taiex: Sequence[Point],
    tpex: Sequence[Point],
    start_date: date,
    end_date: date,
) -> None:
    taiex_norm = normalize(taiex)
    tpex_norm = normalize(tpex)

    width, height = 1400, 900
    left, right = 105, 55
    plot_width = width - left - right
    top_y, panel_height = 150, 245
    bottom_y = 535
    top_low, top_high = _range([*taiex_norm, *tpex_norm], include=100.0)
    bottom_low, bottom_high = _range(margin, include=130.0)
    span_days = max((end_date - start_date).days, 1)

    def x(day: date) -> float:
        return left + (day - start_date).days / span_days * plot_width

    def y(value: float, low: float, high: float, origin: float) -> float:
        return origin + panel_height - (value - low) / (high - low) * panel_height

    def path_data(points: Sequence[Point], low: float, high: float, origin: float) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'} {x(point.day):.2f} "
            f"{y(point.value, low, high, origin):.2f}"
            for i, point in enumerate(points)
        )

    def month_ticks() -> list[date]:
        candidates: list[date] = [start_date]
        cursor = date(start_date.year, start_date.month, 1)
        while cursor <= end_date:
            if cursor.month in (1, 4, 7, 10) and cursor >= start_date:
                candidates.append(cursor)
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        candidates.append(end_date)
        return sorted(set(candidates))

    colors = {
        "bg": "#ffffff",
        "text": "#172033",
        "muted": "#667085",
        "grid": "#d9dee8",
        "taiex": "#2563eb",
        "tpex": "#f59e0b",
        "margin": "#0f9f7f",
        "threshold": "#dc2626",
    }
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="title desc">',
        f'<title id="title">{escape("FinMind 大盤融資維持率與上市、櫃買走勢")}</title>',
        f'<desc id="desc">{escape("上圖比較上市與櫃買報酬指數，起始日正規化為 100；下圖顯示 FinMind 全市場大盤融資維持率。")}</desc>',
        f'<rect width="{width}" height="{height}" fill="{colors["bg"]}"/>',
        '<style>'
        'text{font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif;}'
        '.axis{font-size:18px;fill:#667085;}'
        '.label{font-size:20px;font-weight:600;fill:#172033;}'
        '.legend{font-size:18px;fill:#172033;}'
        '</style>',
        f'<text x="{left}" y="48" font-size="30" font-weight="700" fill="{colors["text"]}">'
        f'{escape("FinMind 大盤融資維持率與上市／櫃買走勢")}</text>',
        f'<text x="{left}" y="82" font-size="18" fill="{colors["muted"]}">'
        f'{start_date.isoformat()} ～ {end_date.isoformat()}｜指數起始日 = 100</text>',
        f'<text class="label" x="{left}" y="{top_y - 28}">上市與櫃買報酬指數</text>',
        f'<text class="label" x="{left}" y="{bottom_y - 28}">全市場大盤融資維持率（%）</text>',
    ]

    for value in _ticks(top_low, top_high):
        yy = y(value, top_low, top_high, top_y)
        parts.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" '
            f'stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="axis" x="{left-14}" y="{yy+6:.2f}" text-anchor="end">'
            f"{value:.1f}</text>"
        )

    for value in _ticks(bottom_low, bottom_high):
        yy = y(value, bottom_low, bottom_high, bottom_y)
        parts.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" '
            f'stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="axis" x="{left-14}" y="{yy+6:.2f}" text-anchor="end">'
            f"{value:.1f}%</text>"
        )

    for tick in month_ticks():
        xx = x(tick)
        parts.append(
            f'<line x1="{xx:.2f}" y1="{top_y}" x2="{xx:.2f}" '
            f'y2="{top_y+panel_height}" stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{xx:.2f}" y1="{bottom_y}" x2="{xx:.2f}" '
            f'y2="{bottom_y+panel_height}" stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="axis" x="{xx:.2f}" y="{bottom_y+panel_height+34}" '
            f'text-anchor="middle">{tick.strftime("%Y-%m")}</text>'
        )

    threshold_y = y(130.0, bottom_low, bottom_high, bottom_y)
    parts.extend(
        [
            f'<line x1="{left}" y1="{threshold_y:.2f}" x2="{width-right}" '
            f'y2="{threshold_y:.2f}" stroke="{colors["threshold"]}" '
            'stroke-width="2" stroke-dasharray="10 8"/>',
            f'<text x="{width-right}" y="{threshold_y-10:.2f}" text-anchor="end" '
            f'font-size="17" fill="{colors["threshold"]}">130% 追繳參考線</text>',
        ]
    )

    series = [
        (taiex_norm, top_low, top_high, top_y, colors["taiex"]),
        (tpex_norm, top_low, top_high, top_y, colors["tpex"]),
        (margin, bottom_low, bottom_high, bottom_y, colors["margin"]),
    ]
    for points, low, high, origin, color in series:
        if points:
            parts.append(
                f'<path d="{path_data(points, low, high, origin)}" fill="none" '
                f'stroke="{color}" stroke-width="4" stroke-linejoin="round" '
                'stroke-linecap="round"/>'
            )

    parts.extend(
        [
            f'<line x1="{left+650}" y1="117" x2="{left+695}" y2="117" '
            f'stroke="{colors["taiex"]}" stroke-width="4"/>',
            f'<text class="legend" x="{left+707}" y="123">上市 TAIEX</text>',
            f'<line x1="{left+840}" y1="117" x2="{left+885}" y2="117" '
            f'stroke="{colors["tpex"]}" stroke-width="4"/>',
            f'<text class="legend" x="{left+897}" y="123">櫃買 TPEx</text>',
            f'<line x1="{left+650}" y1="{bottom_y-34}" x2="{left+695}" '
            f'y2="{bottom_y-34}" stroke="{colors["margin"]}" stroke-width="4"/>',
            f'<text class="legend" x="{left+707}" y="{bottom_y-28}">'
            "FinMind 全市場融資維持率</text>",
            f'<text x="{left}" y="{height-28}" font-size="16" fill="{colors["muted"]}">'
            "來源：FinMind API｜注意：FinMind 官方融資維持率資料未拆分上市與上櫃。</text>",
            "</svg>",
        ]
    )
    path.write_text("\n".join(parts), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下載 FinMind 融資維持率與上市／櫃買報酬指數並畫圖。"
    )
    parser.add_argument("--start", type=parse_iso_date, default=parse_iso_date(DEFAULT_START))
    parser.add_argument("--end", type=parse_iso_date, default=parse_iso_date(DEFAULT_END))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("finmind_output"),
        help="輸出目錄（預設：./finmind_output）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start > args.end:
        print("錯誤：--start 不可晚於 --end。", file=sys.stderr)
        return 2

    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        print(
            "錯誤：請先設定 FINMIND_TOKEN。此融資維持率資料集需要 "
            "FinMind Backer／Sponsor 權限。",
            file=sys.stderr,
        )
        return 2

    try:
        margin_rows = fetch_dataset(
            MARGIN_DATASET, args.start, args.end, token
        )
        taiex_rows = fetch_dataset(
            INDEX_DATASET, args.start, args.end, token, data_id="TAIEX"
        )
        tpex_rows = fetch_dataset(
            INDEX_DATASET, args.start, args.end, token, data_id="TPEx"
        )
        margin = rows_to_points(
            margin_rows,
            "TotalExchangeMarginMaintenance",
            args.start,
            args.end,
        )
        taiex = rows_to_points(taiex_rows, "price", args.start, args.end)
        tpex = rows_to_points(tpex_rows, "price", args.start, args.end)
        if not margin:
            raise FinMindError("指定期間沒有融資維持率資料。")
        if not taiex or not tpex:
            raise FinMindError("指定期間缺少 TAIEX 或 TPEx 指數資料。")

        args.output_dir.mkdir(parents=True, exist_ok=True)
        range_slug = f"{args.start.isoformat()}_to_{args.end.isoformat()}"
        csv_path = args.output_dir / f"finmind_margin_maintenance_{range_slug}.csv"
        svg_path = args.output_dir / f"finmind_margin_maintenance_{range_slug}.svg"
        write_csv(csv_path, merge_for_csv(margin, taiex, tpex))
        render_svg(svg_path, margin, taiex, tpex, args.start, args.end)
    except (FinMindError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print(f"完成：{csv_path.resolve()}")
    print(f"完成：{svg_path.resolve()}")
    print(
        "資料口徑：融資維持率為 FinMind 全市場單一序列；"
        "上市／櫃買線為報酬指數（起始日=100）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
