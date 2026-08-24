from __future__ import annotations

import io
import math
import re
import sqlite3
import urllib3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch
import numpy as np
import pandas as pd
import requests
from PIL import Image

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== 視覺：沿用 K_function 風格 =====
BG = "#F5F5F7"
PANEL = "#FFFFFF"
TEXT = "#101828"
MUTED = "#667085"
NAVY = "#1D2B44"
RED = "#E85D5D"       # 台股：加碼 / 增加
GREEN = "#2CB39A"     # 台股：減碼 / 減少
BLUE = "#315F95"
ORANGE = "#F59E0B"
GRID = "#CAD3DF"

REPORT_WIDTH = 18
DPI = 150
MAX_OUTPUT_WIDTH = 2400

TWSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


@dataclass
class ETFMeta:
    code: str
    name: str
    issuer: str
    aum: float = 0.0
    aum_date: str = ""


def setup_font():
    preferred = [
        "Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans TC",
        "Microsoft JhengHei", "PingFang TC", "Arial Unicode MS"
    ]
    names = {f.name for f in fm.fontManager.ttflist}
    for name in preferred:
        if name in names:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def clean_code(v) -> str:
    s = str(v or "").strip().upper().replace("'", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def to_num(v):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return 0.0
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return 0.0


def fmt_aum(v: float) -> str:
    if not v or not np.isfinite(v):
        return "規模待補"
    return f"{v / 1e8:,.1f} 億"


def fmt_lots(v: float) -> str:
    if not np.isfinite(v):
        return "-"
    sign = "+" if v > 0 else ""
    av = abs(v)
    if av >= 10000:
        return f"{sign}{v/10000:,.1f}萬張"
    if av >= 1000:
        return f"{sign}{v:,.0f}張"
    return f"{sign}{v:,.1f}張"


def fetch_twse_prices() -> dict:
    try:
        r = requests.get(TWSE_DAY_ALL, timeout=30, verify=False, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        r.raise_for_status()
        rows = r.json()
        out = {}
        for x in rows:
            code = clean_code(x.get("Code"))
            if not code:
                continue
            close = to_num(x.get("ClosingPrice"))
            out[code] = {
                "name": str(x.get("Name", "")).strip(),
                "close": close,
                "trade_value": to_num(x.get("TradeValue")),
            }
        return out
    except Exception as exc:
        print(f"⚠️ TWSE 行情取得失敗：{exc}")
        return {}


def fetch_yahoo_aum(code: str) -> tuple[float, str]:
    """從 Yahoo 台股 ETF profile 讀 totalAssets，失敗回 0。"""
    url = f"https://tw.stock.yahoo.com/quote/{code}/profile"
    try:
        r = requests.get(url, timeout=20, verify=False, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        r.raise_for_status()
        html = r.text
        m = re.search(r'"totalAssets":"([^"]+)"', html)
        md = re.search(r'"totalAssetsDate":"([^"]+)"', html)
        aum = float(m.group(1)) if m else 0.0
        d = md.group(1).replace("\\u002F", "/") if md else ""
        return aum, d
    except Exception:
        return 0.0, ""


def read_tables(db_path: Path):
    con = sqlite3.connect(db_path)
    try:
        etfs = pd.read_sql_query("SELECT * FROM etf_list", con)
        holdings = pd.read_sql_query("SELECT * FROM holdings", con)
    finally:
        con.close()

    etfs.columns = [str(c).strip() for c in etfs.columns]
    holdings.columns = [str(c).strip() for c in holdings.columns]

    # 欄位相容
    rename = {}
    for c in holdings.columns:
        k = c.lower()
        if k in {"code", "stockid", "stock_id"}:
            rename[c] = "stock_code"
        elif k in {"name", "stockname", "stock_name"}:
            rename[c] = "stock_name"
        elif k in {"etf", "etfcode", "etf_code"}:
            rename[c] = "etf_code"
    holdings = holdings.rename(columns=rename)

    required = {"etf_code", "stock_code", "stock_name", "shares", "weight", "date"}
    missing = required - set(holdings.columns)
    if missing:
        raise RuntimeError(f"holdings 缺少欄位：{sorted(missing)}")

    holdings["etf_code"] = holdings["etf_code"].map(clean_code)
    holdings["stock_code"] = holdings["stock_code"].map(clean_code)
    holdings["shares"] = pd.to_numeric(holdings["shares"], errors="coerce").fillna(0.0)
    holdings["weight"] = pd.to_numeric(holdings["weight"], errors="coerce").fillna(0.0)
    holdings["date"] = pd.to_datetime(holdings["date"], errors="coerce")
    holdings = holdings.dropna(subset=["date"])
    holdings["date"] = holdings["date"].dt.normalize()
    holdings["lots"] = holdings["shares"] / 1000.0

    if "etf_code" in etfs.columns:
        etfs["etf_code"] = etfs["etf_code"].map(clean_code)
    return etfs, holdings


def latest_two_dates(holdings: pd.DataFrame):
    ds = sorted(pd.unique(holdings["date"]))
    if len(ds) < 2:
        raise RuntimeError("至少需要兩個不同日期的持股資料才能計算每日異動")
    return pd.Timestamp(ds[-1]), pd.Timestamp(ds[-2])


def build_meta(etfs: pd.DataFrame, holdings: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(set(holdings["etf_code"].dropna().astype(str)))
    rows = []
    price_map = fetch_twse_prices()

    etf_lookup = {}
    if "etf_code" in etfs.columns:
        for _, r in etfs.iterrows():
            etf_lookup[clean_code(r.get("etf_code"))] = r.to_dict()

    for code in codes:
        r = etf_lookup.get(code, {})
        name = str(r.get("etf_name") or r.get("name") or price_map.get(code, {}).get("name", "")).strip()
        issuer = str(r.get("issuer") or r.get("fund_company") or "").strip()
        aum, aum_date = fetch_yahoo_aum(code)
        rows.append({
            "etf_code": code,
            "etf_name": name,
            "issuer": issuer,
            "aum": aum,
            "aum_date": aum_date,
        })
    out = pd.DataFrame(rows)
    return out.sort_values(["aum", "etf_code"], ascending=[False, True]).reset_index(drop=True)


def build_changes(holdings: pd.DataFrame, today: pd.Timestamp, prev: pd.Timestamp) -> pd.DataFrame:
    cols = ["etf_code", "stock_code", "stock_name", "shares", "lots", "weight"]
    t = holdings[holdings["date"] == today][cols].copy()
    p = holdings[holdings["date"] == prev][cols].copy()

    # 同 ETF 同股票若來源重複，先合併，避免重複計數
    agg = {"stock_name": "last", "shares": "sum", "lots": "sum", "weight": "sum"}
    t = t.groupby(["etf_code", "stock_code"], as_index=False).agg(agg)
    p = p.groupby(["etf_code", "stock_code"], as_index=False).agg(agg)

    m = t.merge(
        p,
        on=["etf_code", "stock_code"],
        how="outer",
        suffixes=("_today", "_prev"),
    )
    for c in ["shares_today", "shares_prev", "lots_today", "lots_prev", "weight_today", "weight_prev"]:
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)

    m["stock_name"] = m["stock_name_today"].fillna(m["stock_name_prev"]).fillna("")
    m["change_shares"] = m["shares_today"] - m["shares_prev"]
    m["change_lots"] = m["change_shares"] / 1000.0
    m["change_weight"] = m["weight_today"] - m["weight_prev"]

    def classify(r):
        a, b = r["shares_prev"], r["shares_today"]
        if a <= 0 < b:
            return "新增"
        if a > 0 and b <= 0:
            return "出清"
        if b > a:
            return "加碼"
        if b < a:
            return "減碼"
        return "不變"

    m["action"] = m.apply(classify, axis=1)
    return m


def build_consensus(holdings: pd.DataFrame, changes: pd.DataFrame, meta: pd.DataFrame, today: pd.Timestamp):
    h = holdings[holdings["date"] == today].copy()
    h = h[h["shares"] > 0]

    # 只把可辨識成台股證券代號的持股納入「重複選股」
    # 排除現金、期貨、外幣等非股票項目。
    h = h[h["stock_code"].astype(str).str.match(r"^[0-9A-Z]{4,6}$", na=False)]

    aum_map = meta.set_index("etf_code")["aum"].to_dict()
    h["aum"] = h["etf_code"].map(aum_map).fillna(0.0)
    h["weighted_score"] = h["aum"] * (h["weight"] / 100.0)

    c = h.groupby(["stock_code", "stock_name"], as_index=False).agg(
        etf_count=("etf_code", "nunique"),
        consensus_aum=("aum", "sum"),
        weighted_score=("weighted_score", "sum"),
        avg_weight=("weight", "mean"),
    )

    active_count = max(1, h["etf_code"].nunique())
    c["etf_ratio"] = c["etf_count"] / active_count * 100

    ch = changes[changes["action"] != "不變"].copy()
    add = ch[ch["action"].isin(["新增", "加碼"])].groupby(
        ["stock_code", "stock_name"], as_index=False
    ).agg(add_etfs=("etf_code", "nunique"), add_lots=("change_lots", "sum"))

    reduce = ch[ch["action"].isin(["減碼", "出清"])].groupby(
        ["stock_code", "stock_name"], as_index=False
    ).agg(reduce_etfs=("etf_code", "nunique"), reduce_lots=("change_lots", "sum"))

    c = c.merge(add, on=["stock_code", "stock_name"], how="left")
    c = c.merge(reduce, on=["stock_code", "stock_name"], how="left")
    for col in ["add_etfs", "reduce_etfs", "add_lots", "reduce_lots"]:
        c[col] = c[col].fillna(0)
    return c.sort_values(["etf_count", "weighted_score"], ascending=[False, False]).reset_index(drop=True)


def rounded_panel(ax, x, y, w, h, radius=0.02, face=PANEL, edge="#E4E7EC"):
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0.008,rounding_size={radius}",
        transform=ax.transAxes,
        linewidth=1.0,
        edgecolor=edge,
        facecolor=face,
        clip_on=False,
    )
    ax.add_patch(patch)
    return patch


def save_screenshot_like(fig, path: Path):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)

    img = Image.open(buf).convert("RGB")
    if img.width > MAX_OUTPUT_WIDTH:
        scale = MAX_OUTPUT_WIDTH / img.width
        img = img.resize((MAX_OUTPUT_WIDTH, int(img.height * scale)), Image.Resampling.LANCZOS)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=False, compress_level=6)
    print(f"🖼️ {path.name}：{img.width}x{img.height}")


def _text(ax, x, y, s, size=12, color=TEXT, weight="normal", ha="left"):
    ax.text(x, y, s, transform=ax.transAxes, fontsize=size, color=color,
            fontweight=weight, ha=ha, va="top")


def generate_daily_report(meta, changes, today, prev, out_path: Path):
    show = meta.copy()
    n_etf = len(show)
    # 每檔 ETF 卡片約 0.95 吋高，ETF 多時自動拉長
    height = max(10, 3.6 + n_etf * 1.15)
    fig = plt.figure(figsize=(REPORT_WIDTH, height), facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    _text(ax, 0.04, 0.972, "主動式 ETF｜每日持股異動", 26, NAVY, "bold")
    _text(ax, 0.04, 0.935, f"{today:%Y/%m/%d} vs {prev:%Y/%m/%d}｜依 ETF 資產規模排序", 12, MUTED)

    meaningful = changes[changes["action"] != "不變"]
    stats = [
        ("主動 ETF", f"{n_etf} 檔"),
        ("新增", f"{(meaningful.action == '新增').sum():,} 筆"),
        ("加碼", f"{(meaningful.action == '加碼').sum():,} 筆"),
        ("減碼 / 出清", f"{meaningful.action.isin(['減碼','出清']).sum():,} 筆"),
    ]
    x0, card_w, gap = 0.04, 0.215, 0.018
    for i, (label, val) in enumerate(stats):
        x = x0 + i * (card_w + gap)
        rounded_panel(ax, x, 0.855, card_w, 0.06)
        _text(ax, x + 0.016, 0.900, label, 10, MUTED)
        _text(ax, x + 0.016, 0.873, val, 18, NAVY, "bold")

    y = 0.82
    card_h = 0.82 / max(n_etf, 1)
    card_h = min(card_h, 0.085)

    for idx, row in show.iterrows():
        if y - card_h < 0.025:
            break

        code = row["etf_code"]
        sub = changes[(changes["etf_code"] == code) & (changes["action"] != "不變")].copy()
        sub["abs_change"] = sub["change_lots"].abs()
        sub = sub.sort_values(["abs_change", "change_weight"], ascending=[False, False])

        rounded_panel(ax, 0.04, y - card_h + 0.005, 0.92, card_h - 0.008)
        rank = idx + 1
        _text(ax, 0.057, y - 0.007, f"{rank:02d}", 11, MUTED, "bold")
        title = f"{code}  {row['etf_name'] or ''}"
        _text(ax, 0.095, y - 0.005, title, 14, NAVY, "bold")
        aum_line = f"{fmt_aum(row['aum'])}"
        if row.get("aum_date"):
            aum_line += f"｜規模日 {row['aum_date']}"
        if row.get("issuer"):
            aum_line += f"｜{row['issuer']}"
        _text(ax, 0.095, y - 0.034, aum_line, 9.5, MUTED)

        if sub.empty:
            _text(ax, 0.46, y - 0.019, "今日持股無變動", 11, MUTED)
        else:
            top = sub.head(6)
            chunks = []
            for _, r in top.iterrows():
                action = r["action"]
                sign = "▲" if action in {"新增", "加碼"} else "▼"
                name = str(r["stock_name"] or r["stock_code"])
                chunks.append(f"{sign}{name} {fmt_lots(r['change_lots'])}")
            # 分兩行，每行 3 個
            line1 = "    ".join(chunks[:3])
            line2 = "    ".join(chunks[3:6])
            _text(ax, 0.46, y - 0.008, line1, 10.5, TEXT, "bold")
            if line2:
                _text(ax, 0.46, y - 0.035, line2, 10, TEXT)

        y -= card_h

    _text(ax, 0.04, 0.012, "註：紅色語意＝新增/加碼；綠色語意＝減碼/出清。資料日期以各投信實際揭露為準。", 9, MUTED)
    save_screenshot_like(fig, out_path)


def _draw_rank_table(ax, title, df, mode, top_n=15, y_start=0.92):
    _text(ax, 0.05, y_start, title, 17, NAVY, "bold")
    y = y_start - 0.05

    if df.empty:
        _text(ax, 0.05, y, "無資料", 11, MUTED)
        return

    show = df.head(top_n).copy()
    maxv = 1.0
    if mode == "count":
        maxv = max(1.0, float(show["etf_count"].max()))
    elif mode == "add":
        maxv = max(1.0, float(show["add_etfs"].max()))
    elif mode == "reduce":
        maxv = max(1.0, float(show["reduce_etfs"].max()))

    row_h = 0.045
    for i, (_, r) in enumerate(show.iterrows(), 1):
        yy = y - (i - 1) * row_h
        if yy < 0.04:
            break
        name = f"{r['stock_code']} {r['stock_name']}"
        _text(ax, 0.05, yy, f"{i:02d}", 9.5, MUTED, "bold")
        _text(ax, 0.095, yy, name, 10.5, TEXT, "bold")

        if mode == "count":
            val = float(r["etf_count"])
            label = f"{int(val)} 檔｜{r['etf_ratio']:.0f}%"
            color = BLUE
        elif mode == "add":
            val = float(r["add_etfs"])
            label = f"{int(val)} 檔同步加碼｜{fmt_lots(float(r['add_lots']))}"
            color = RED
        else:
            val = float(r["reduce_etfs"])
            label = f"{int(val)} 檔同步減碼｜{fmt_lots(float(r['reduce_lots']))}"
            color = GREEN

        bar_x, bar_y, bar_w, bar_h = 0.47, yy - 0.002, 0.31, 0.016
        rounded_panel(ax, bar_x, bar_y, bar_w, bar_h, radius=0.008, face="#EEF2F6", edge="#EEF2F6")
        fill_w = bar_w * (val / maxv)
        rounded_panel(ax, bar_x, bar_y, max(fill_w, 0.005), bar_h, radius=0.008, face=color, edge=color)
        _text(ax, 0.81, yy, label, 9.5, color, "bold")


def generate_consensus_report(meta, consensus, today, out_path: Path, top_n=20):
    fig = plt.figure(figsize=(REPORT_WIDTH, 18), facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    _text(ax, 0.04, 0.974, "主動式 ETF｜共識選股排行", 27, NAVY, "bold")
    _text(ax, 0.04, 0.94, f"{today:%Y/%m/%d}｜重複持股、同步加碼、同步減碼", 12, MUTED)

    n_etf = len(meta)
    n_stock = len(consensus)
    top_consensus = int(consensus["etf_count"].max()) if not consensus.empty else 0
    stats = [
        ("主動 ETF", f"{n_etf} 檔"),
        ("共識股票", f"{n_stock} 檔"),
        ("最高重複", f"{top_consensus} 檔 ETF"),
    ]
    for i, (label, val) in enumerate(stats):
        x = 0.04 + i * 0.30
        rounded_panel(ax, x, 0.865, 0.27, 0.055)
        _text(ax, x + 0.016, 0.903, label, 10, MUTED)
        _text(ax, x + 0.016, 0.88, val, 17, NAVY, "bold")

    # 三個獨立區塊
    left = fig.add_axes([0.055, 0.48, 0.89, 0.35])
    left.axis("off")
    rank1 = consensus.sort_values(["etf_count", "weighted_score"], ascending=[False, False])
    _draw_rank_table(left, f"① 重複持股 TOP {top_n}", rank1, "count", top_n=min(top_n, 15))

    lower_left = fig.add_axes([0.055, 0.07, 0.43, 0.36])
    lower_left.axis("off")
    add = consensus[consensus["add_etfs"] > 0].sort_values(
        ["add_etfs", "add_lots"], ascending=[False, False]
    )
    _draw_rank_table(lower_left, "② 同步加碼排行", add, "add", top_n=10)

    lower_right = fig.add_axes([0.52, 0.07, 0.43, 0.36])
    lower_right.axis("off")
    reduce = consensus[consensus["reduce_etfs"] > 0].copy()
    reduce["reduce_abs"] = reduce["reduce_lots"].abs()
    reduce = reduce.sort_values(["reduce_etfs", "reduce_abs"], ascending=[False, False])
    _draw_rank_table(lower_right, "③ 同步減碼排行", reduce, "reduce", top_n=10)

    _text(ax, 0.04, 0.025,
          "規模加權共識＝Σ(ETF資產規模 × 個股權重)。重複持股先以 ETF 檔數排序，同檔數再以規模加權共識排序。",
          9, MUTED)
    save_screenshot_like(fig, out_path)


def export_csv(meta, changes, consensus, today, out_dir):
    meta.to_csv(out_dir / f"active_etf_list_{today:%Y%m%d}.csv", index=False, encoding="utf-8-sig")
    changes.to_csv(out_dir / f"active_etf_changes_{today:%Y%m%d}.csv", index=False, encoding="utf-8-sig")
    consensus.to_csv(out_dir / f"active_etf_consensus_{today:%Y%m%d}.csv", index=False, encoding="utf-8-sig")


def generate_reports(db_path: Path, out_dir: Path, top_n: int = 20):
    setup_font()
    out_dir.mkdir(parents=True, exist_ok=True)

    etfs, holdings = read_tables(db_path)
    today, prev = latest_two_dates(holdings)
    print(f"📅 比較日期：{today:%Y-%m-%d} vs {prev:%Y-%m-%d}")

    meta = build_meta(etfs, holdings)
    changes = build_changes(holdings, today, prev)
    consensus = build_consensus(holdings, changes, meta, today)

    export_csv(meta, changes, consensus, today, out_dir)

    p1 = out_dir / f"active_etf_daily_{today:%Y%m%d}.png"
    p2 = out_dir / f"active_etf_consensus_{today:%Y%m%d}.png"

    generate_daily_report(meta, changes, today, prev, p1)
    generate_consensus_report(meta, consensus, today, p2, top_n=top_n)
    return [p1, p2]
