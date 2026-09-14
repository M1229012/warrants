# -*- coding: utf-8 -*-
"""
一頁式 K 線長圖核心模組
========================

從 K_function 權證週報抽出來的可重用版本。給任何「一張圖看完一檔股票」的需求用：
標題列 → 摘要卡片 → K線（均線＋布林＋價量分布）→ 成交量 → 任意數量的自訂面板。

最短用法：
    import yfinance as yf
    from kline_core import plot_onepage_kline, fig_to_png_buffer

    df = yf.download("2330.TW", period="160d")
    fig = plot_onepage_kline(df, title="2330 台積電｜個股技術面一頁式")
    buf = fig_to_png_buffer(fig)

加卡片與自訂面板：
    def draw_inst(ax, df, x):
        ax.bar(x, df["foreign"], color=RED, width=0.72)

    fig = plot_onepage_kline(
        df,
        title="2330 台積電｜週報",
        subtitle="區間：2026/08/18 - 2026/08/22｜資訊僅供參考",
        cards=[("本週股價", "+3.25%", "", RED), ("本週量能", "-8.10%", "", GREEN)],
        extra_panels=[(5.0, "三大法人買賣超", draw_inst)],
    )

必要套件：
pip install pandas numpy matplotlib
（fig_to_png_buffer 的縮圖另需 pillow）
"""

import os
from io import BytesIO

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle

try:
    from PIL import Image
except Exception:
    Image = None


# ============================================================
# 視覺風格：淺背景 + 藏青色元素（與 tw-chart-visual-style 一致）
# ============================================================

BG     = "#F5F5F7"
PANEL  = "#FFFFFF"
PANEL2 = "#FFFFFF"
GRID   = "#CAD3DF"
TEXT   = "#101828"
MUTED  = "#667085"
NAVY   = "#1D2B44"
GOLD   = NAVY
RED    = "#E85D5D"
GREEN  = "#2CB39A"
BLUE   = "#315F95"
ORANGE = "#F59E0B"
LIME   = "#2E8B57"
PURPLE = "#6F5BD8"

CENTER_WATERMARK_TEXT      = "股市艾斯\n台股DC討論群"
CENTER_WATERMARK_ALPHA     = 0.06
CENTER_WATERMARK_FONT_SIZE = 200
CENTER_WATERMARK_ROTATION  = 18

BRAND_NOTE_TEXT = "By 股市艾斯出品  請勿轉傳"

# 版面基準：原版 figsize 寬 28、height_ratios 總和 50.45 對應圖高 62.3，
# 比值 1.234。改 row 數量時沿用同一比值，字級才不會相對變形。
FIG_WIDTH_DEFAULT = 28.0
ROW_HEIGHT_SCALE = 1.234

# 各區塊預設 height_ratio。K 線區塊刻意給到 13.1，是實際放大 K 棒，
# 而不是靠壓縮 Y 軸留白做出「假性放大」。
ROW_RATIO_HEADER = 1.45
ROW_RATIO_CARDS  = 2.05
ROW_RATIO_CANDLE = 13.1
ROW_RATIO_VOLUME = 2.45

REPORT_OUTPUT_DPI = int(os.getenv("REPORT_OUTPUT_DPI", "110"))
SCREENSHOT_OUTPUT_MAX_WIDTH = int(os.getenv("SCREENSHOT_OUTPUT_MAX_WIDTH", "2400"))


# ============================================================
# 字型
# ============================================================

REPORT_FONT_DIR = os.getenv("REPORT_FONT_DIR", ".cache/report-fonts").strip() or ".cache/report-fonts"

_registered_fonts = []
for _font_path in [
    os.path.join(REPORT_FONT_DIR, "NotoSansCJKtc-Regular.otf"),
    os.path.join(REPORT_FONT_DIR, "NotoSansCJKtc-Bold.otf"),
]:
    if not os.path.isfile(_font_path):
        continue
    try:
        fm.fontManager.addfont(_font_path)
        _name = fm.FontProperties(fname=_font_path).get_name()
        if _name:
            _registered_fonts.append(_name)
    except Exception as exc:
        print(f"⚠️ 報表字型註冊失敗：{_font_path}｜{exc}")

_available = {f.name for f in fm.fontManager.ttflist}
for _cand in _registered_fonts + [
    "Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans TC",
    "Microsoft JhengHei", "PingFang TC", "SimHei",
]:
    if _cand and _cand in _available:
        plt.rcParams["font.family"] = _cand
        break
else:
    plt.rcParams["font.family"] = "DejaVu Sans"
    print("⚠️ 找不到中文字型，暫時使用 DejaVu Sans")
plt.rcParams["axes.unicode_minus"] = False


# ============================================================
# 資料準備
# ============================================================

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """把常見來源（yfinance / FinMind / 自建表）整理成標準 OHLCV + DatetimeIndex。

    yfinance 單檔下載會回 MultiIndex 欄位，這裡先攤平；FinMind 的中文/小寫欄名也一併對應。
    """
    if df is None or len(df) == 0:
        raise ValueError("空的 DataFrame，無法繪圖")

    out = df.copy()

    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [c[0] for c in out.columns]

    alias = {
        "open": "Open", "high": "High", "low": "Low", "close": "Close",
        "volume": "Volume", "max": "High", "min": "Low",
        "Trading_Volume": "Volume", "開盤價": "Open", "最高價": "High",
        "最低價": "Low", "收盤價": "Close", "成交股數": "Volume",
    }
    out = out.rename(columns={c: alias.get(c, alias.get(str(c).lower(), c)) for c in out.columns})

    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"缺少必要欄位：{missing}")

    if not isinstance(out.index, pd.DatetimeIndex):
        for c in ["date", "Date", "日期"]:
            if c in out.columns:
                out = out.set_index(pd.to_datetime(out[c], errors="coerce"))
                break
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out[~out.index.isna()].sort_index()

    for c in OHLCV:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """均線、量均、KD、MACD、布林。K 線圖只用到 MA / MV / BB，其餘留給文字判讀。"""
    df = df.copy()
    for n in [5, 10, 20, 60]:
        df[f"MA{n}"] = df["Close"].rolling(n).mean()
    df["MV5"] = df["Volume"].rolling(5).mean()
    df["MV20"] = df["Volume"].rolling(20).mean()

    low_min = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K9"] = rsv.ewm(com=2).mean()
    df["D9"] = df["K9"].ewm(com=2).mean()
    df["J9"] = 3 * df["K9"] - 2 * df["D9"]

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["OSC"] = df["DIF"] - df["MACD"]

    df["BB_MID"] = df["Close"].rolling(20).mean()
    df["BB_STD"] = df["Close"].rolling(20).std()
    df["BB_UPPER"] = df["BB_MID"] + 2 * df["BB_STD"]
    df["BB_LOWER"] = df["BB_MID"] - 2 * df["BB_STD"]
    df["BB_WIDTH"] = df["BB_UPPER"] - df["BB_LOWER"]
    return df


def get_ma_kline_signals(df: pd.DataFrame) -> str:
    """K 線區塊中央那行金句：均線排列 / 交叉 / 帶量突破。沒有訊號就回空字串不畫。"""
    if df is None or len(df) < 3:
        return ""
    latest, prev = df.iloc[-1], df.iloc[-2]
    notes = []
    if latest["MA5"] > latest["MA10"] > latest["MA20"] > latest["MA60"]:
        notes.append("均線多頭排列")
    elif latest["MA5"] < latest["MA10"] < latest["MA20"] < latest["MA60"]:
        notes.append("均線空頭排列")
    if prev["MA5"] < prev["MA20"] and latest["MA5"] > latest["MA20"]:
        notes.append("均線黃金交叉")
    elif prev["MA5"] > prev["MA20"] and latest["MA5"] < latest["MA20"]:
        notes.append("均線死亡交叉")
    if all(latest["Close"] > latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("強勢站上均線")
    elif all(latest["Close"] < latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("全面跌破均線")
    if latest["Close"] > latest["MA60"] and latest["Close"] > latest["Open"] and latest["Volume"] > prev["Volume"]:
        notes.append("帶量突破年線")
    if latest["Close"] < latest["MA20"] and latest["Close"] < latest["Open"] and latest["Volume"] > prev["Volume"]:
        notes.append("帶量長黑跌破月線")
    return "．".join(notes)


# ============================================================
# 繪圖工具
# ============================================================

def style_ax(ax, title=None, title_color=NAVY):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=28)
    for spine in ax.spines.values():
        spine.set_color(GRID)
        spine.set_linewidth(1.1)
    ax.grid(True, color=GRID, alpha=0.35, linewidth=0.7)
    if title:
        ax.set_title(title, loc="left", fontsize=38, color=title_color, fontweight="bold", pad=14)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_color(MUTED)


def plot_candles(ax, plot_df: pd.DataFrame, x: list):
    """紅漲綠跌 K 棒。十字線（開收幾乎相同）改畫一條粗橫線，否則會消失。"""
    up = plot_df["Close"] >= plot_df["Open"]
    width = 0.82
    for i in x:
        color = RED if up.iloc[i] else GREEN
        op, cl = float(plot_df["Open"].iloc[i]), float(plot_df["Close"].iloc[i])
        hi, lo = float(plot_df["High"].iloc[i]), float(plot_df["Low"].iloc[i])
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.65, zorder=3)
        body_low = min(op, cl)
        body_h = abs(cl - op)
        if body_h < max(0.01, cl * 0.0005):
            ax.plot([i - width / 2, i + width / 2], [cl, cl], color=color, linewidth=3.0, zorder=4)
        else:
            ax.bar(i, body_h, bottom=body_low, width=width, color=color, edgecolor=color,
                   linewidth=0.8, align="center", zorder=4)


def weighted_volume_profile_stats(df: pd.DataFrame, n_bins: int = 40) -> dict:
    """加權價量分布：把每日成交量依 下影線 0.2 / 實體 0.6 / 上影線 0.2 分配到價格區間。

    比單純用收盤價分箱更接近真實成交價位；最大量區與第二大量區之後會被標成紅／橘。
    """
    required = {"Low", "High", "Open", "Close", "Volume"}
    if df is None or df.empty or not required.issubset(df.columns):
        return {}

    work = df[["Low", "High", "Open", "Close", "Volume"]].copy()
    for col in work.columns:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna()
    if work.empty:
        return {}

    price_min, price_max = float(work["Low"].min()), float(work["High"].max())
    if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
        return {}

    n_bins = max(5, int(n_bins or 40))
    bins = np.linspace(price_min, price_max, n_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2
    height = float(bins[1] - bins[0])
    profile = np.zeros(n_bins, dtype=float)

    for _, row in work.iterrows():
        vol = float(row["Volume"])
        low, high = float(row["Low"]), float(row["High"])
        body_min, body_max = min(float(row["Open"]), float(row["Close"])), max(float(row["Open"]), float(row["Close"]))
        for (start, end), weight in [((low, body_min), 0.2), ((body_min, body_max), 0.6), ((body_max, high), 0.2)]:
            if end - start < 1e-6:
                continue
            idxs = np.where((centers >= start) & (centers <= end))[0]
            if len(idxs):
                profile[idxs] += vol * weight / len(idxs)

    if float(profile.max()) <= 0:
        return {}

    order = np.argsort(profile)[::-1]
    return {
        "centers": centers,
        "height": height,
        "profile": profile,
        "max_idx": int(order[0]),
        "second_idx": int(order[1]) if len(order) > 1 else -1,
    }


def add_weighted_volume_profile_overlay(ax, df: pd.DataFrame, n_bins: int = 40,
                                        color="#38BDF8", alpha=0.15, scale=1.08):
    """在 K 線圖左側疊加水平價量分布。第一大量紅色、第二大量橘色、其餘淺藍。"""
    stats = weighted_volume_profile_stats(df, n_bins=n_bins)
    if not stats:
        return
    centers, height, profile = stats["centers"], float(stats["height"]), stats["profile"]
    max_idx, second_idx = stats["max_idx"], stats["second_idx"]

    scaled = profile / profile.max()
    x_min, x_max = ax.get_xlim()
    width_max = (x_max - x_min) / scale
    for i in range(len(profile)):
        if i == max_idx:
            rect_color, rect_alpha = "#DC2626", 0.2
        elif i == second_idx:
            rect_color, rect_alpha = "#F59E0B", 0.2
        else:
            rect_color, rect_alpha = color, alpha
        ax.add_patch(Rectangle((x_min, centers[i] - height / 2), scaled[i] * width_max, height,
                               color=rect_color, alpha=rect_alpha, zorder=0, clip_on=True))
    ax.set_xlim(x_min, x_max)


def adjust_candle_price_ylim(ax, plot_df: pd.DataFrame):
    """Y 軸只依 K 棒 High / Low 決定。

    刻意不把 MA60 與 BB_LOWER 納入下緣，否則早期均線偏低會把整個 K 棒區壓扁。
    早期 MA60 / 布林下軌因此會被底部自然裁切，這是預期行為。
    """
    if plot_df is None or plot_df.empty:
        return
    low_s = pd.to_numeric(plot_df["Low"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    high_s = pd.to_numeric(plot_df["High"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if low_s.empty or high_s.empty:
        return
    y_min, y_max = float(low_s.min()), float(high_s.max())
    if not np.isfinite(y_min) or not np.isfinite(y_max):
        return
    y_span = max(y_max - y_min, 1e-6)
    ax.set_ylim(y_min - y_span * 0.11, y_max + y_span * 0.05)


def adjust_volume_ylim(ax, plot_df: pd.DataFrame):
    """成交量上方多留白，避免最高量柱或均量線貼到 legend。"""
    if plot_df is None or plot_df.empty:
        return
    values = []
    for col in ["Volume", "MV5", "MV20"]:
        if col in plot_df.columns:
            s = pd.to_numeric(plot_df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna() / 1000
            if not s.empty:
                values.append(s)
    if not values:
        return
    y_max = float(pd.concat(values, ignore_index=True).max())
    if not np.isfinite(y_max) or y_max <= 0:
        return
    ax.set_ylim(0, y_max * 1.45)


def adjust_stacked_ylim(ax, series_list, upper_pad_ratio=0.32, lower_pad_ratio=0.18):
    """正負堆疊柱（三大法人、資金流）用：上下都留白，legend 不會壓到柱子。"""
    arrays = [pd.to_numeric(pd.Series(s), errors="coerce").fillna(0).astype(float).values for s in series_list]
    if not arrays or len(arrays[0]) == 0:
        return
    pos_stack = sum(np.clip(a, 0, None) for a in arrays)
    neg_stack = sum(np.clip(a, None, 0) for a in arrays)
    y_min = min(float(np.nanmin(neg_stack)), 0.0)
    y_max = max(float(np.nanmax(pos_stack)), 0.0)
    if not np.isfinite(y_min) or not np.isfinite(y_max):
        return
    span = y_max - y_min
    if span <= 0:
        span = max(abs(y_max), abs(y_min), 1.0)
    ax.set_ylim(y_min - span * lower_pad_ratio, y_max + span * upper_pad_ratio)


def draw_card(ax, x, y, w, h, label, value, sub="", value_color=NAVY):
    """摘要卡片：白底圓角 + 上方藏青 band。

    band 一定要 set_clip_path 裁到外框圓角，否則左右會露出方角或看起來縮短。
    數值固定畫在同一水平線，一整排卡片才不會看起來歪掉。
    """
    rounding, band_h = 0.026, 0.078

    box = FancyBboxPatch((x, y), w, h, transform=ax.transAxes,
                         boxstyle=f"round,pad=0.000,rounding_size={rounding}",
                         facecolor=PANEL2, edgecolor=NAVY, linewidth=1.25, zorder=1)
    ax.add_patch(box)

    band = Rectangle((x, y + h - band_h), w, band_h, transform=ax.transAxes,
                     facecolor=NAVY, edgecolor=NAVY, linewidth=0, alpha=0.96, zorder=2)
    band.set_clip_path(box)
    ax.add_patch(band)

    ax.text(x + w / 2, y + h - 0.15, label, transform=ax.transAxes, color=MUTED,
            fontsize=29, fontweight="bold", ha="center", va="top", zorder=4)
    ax.text(x + w / 2, y + 0.30, value, transform=ax.transAxes, color=value_color,
            fontsize=42, fontweight="bold", ha="center", va="center", zorder=4)
    if sub:
        ax.text(x + w / 2, y + 0.10, sub, transform=ax.transAxes, color=MUTED,
                fontsize=22, fontweight="bold", ha="center", va="bottom", zorder=4)


def add_center_watermarks(fig, text=CENTER_WATERMARK_TEXT, ys=(0.66, 0.31)):
    """長圖偏高，上下各放一個淡浮水印，捲到哪一段都看得到。"""
    if not text:
        return
    try:
        for y in ys:
            fig.text(0.5, y, text, ha="center", va="center",
                     fontsize=CENTER_WATERMARK_FONT_SIZE, fontweight="bold",
                     color=NAVY, alpha=CENTER_WATERMARK_ALPHA,
                     rotation=CENTER_WATERMARK_ROTATION, linespacing=1.12, zorder=1000)
    except Exception:
        pass


# ============================================================
# 一頁式主版型
# ============================================================

def plot_onepage_kline(
    df: pd.DataFrame,
    title: str,
    subtitle: str = "",
    brand_note: str = BRAND_NOTE_TEXT,
    cards=None,
    candle_title: str = "股價趨勢｜K線、均線、布林與價量分布",
    show_volume: bool = True,
    extra_panels=None,
    watermark_text: str = CENTER_WATERMARK_TEXT,
    fig_width: float = FIG_WIDTH_DEFAULT,
    date_fmt: str = "%m-%d",
    max_xticks: int = 12,
    show_volume_profile: bool = True,
):
    """組出一頁式 K 線長圖並回傳 fig。

    參數：
    - df           : OHLCV DataFrame，會自動 normalize 與補指標。
    - title        : 主標題，慣例是 "2330 台積電｜個股技術面一頁式"。
    - subtitle     : 副標題，慣例是 "區間：... ｜資訊僅供參考"。
    - cards        : [(label, value, sub, color), ...]，摘要卡片，最多 6 張。
    - extra_panels : [(height_ratio, title, draw_fn), ...]
                     draw_fn(ax, plot_df, x) 自己畫內容，style_ax 已先套好。
    - show_volume_profile : 關掉可省下逐列分箱運算（長區間會慢）。

    區塊高度用 height_ratio 控制，整張圖高度＝總 ratio × ROW_HEIGHT_SCALE，
    這樣加減面板時字級比例不會跑掉。
    """
    plot_df = normalize_ohlcv(df)
    if "MA5" not in plot_df.columns or "BB_UPPER" not in plot_df.columns:
        plot_df = calculate_indicators(plot_df)
    x = list(range(len(plot_df)))
    date_labels = [pd.Timestamp(d).strftime(date_fmt) for d in plot_df.index]

    cards = list(cards or [])
    extra_panels = list(extra_panels or [])

    rows = [("header", ROW_RATIO_HEADER, None, None)]
    if cards:
        rows.append(("cards", ROW_RATIO_CARDS, None, None))
    rows.append(("candle", ROW_RATIO_CANDLE, candle_title, None))
    if show_volume:
        rows.append(("volume", ROW_RATIO_VOLUME, None, None))
    for panel in extra_panels:
        ratio, panel_title, draw_fn = panel
        rows.append(("custom", float(ratio), panel_title, draw_fn))

    height_ratios = [r[1] for r in rows]
    fig_height = sum(height_ratios) * ROW_HEIGHT_SCALE
    fig = plt.figure(figsize=(fig_width, fig_height), facecolor=BG)
    gs = GridSpec(len(rows), 12, figure=fig, height_ratios=height_ratios,
                  hspace=0.20, wspace=0.25)

    data_axes = []
    candle_ax = None

    for i, (kind, _ratio, row_title, draw_fn) in enumerate(rows):
        if kind == "header":
            ax = fig.add_subplot(gs[i, :])
            ax.set_axis_off()
            ax.text(0.01, 0.50, title, color=NAVY, fontsize=68, fontweight="bold",
                    ha="left", va="center")
            if subtitle:
                ax.text(0.01, -0.10, subtitle, color=MUTED, fontsize=32, ha="left", va="center")
            if brand_note:
                ax.text(1.03, 0.62, brand_note, color=NAVY, fontsize=30, fontweight="bold",
                        ha="right", va="center")

        elif kind == "cards":
            ax = fig.add_subplot(gs[i, :])
            ax.set_axis_off()
            gap = 0.01
            card_w = min(0.183, (0.96 - (len(cards) - 1) * gap) / max(len(cards), 1))
            start_x = (1 - (len(cards) * card_w + (len(cards) - 1) * gap)) / 2
            for j, card in enumerate(cards):
                label, value = card[0], card[1]
                sub = card[2] if len(card) > 2 else ""
                col = card[3] if len(card) > 3 else NAVY
                draw_card(ax, start_x + j * (card_w + gap), 0.06, card_w, 0.88, label, value, sub, col)

        elif kind == "candle":
            ax = fig.add_subplot(gs[i, :])
            candle_ax = ax
            style_ax(ax, row_title)
            plot_candles(ax, plot_df, x)
            for col, color, lab in [
                ("MA5", RED, "5MA"), ("MA10", ORANGE, "10MA"),
                ("MA20", LIME, "20MA"), ("MA60", BLUE, "60MA"),
            ]:
                if col in plot_df.columns:
                    ax.plot(x, plot_df[col], color=color, linewidth=2.1,
                            label=f"{lab} {plot_df[col].iloc[-1]:.2f}")
            for col in ["BB_UPPER", "BB_LOWER"]:
                if col in plot_df.columns:
                    ax.plot(x, plot_df[col], linestyle="--", color=MUTED, linewidth=1.4, alpha=0.9)

            if show_volume_profile:
                add_weighted_volume_profile_overlay(ax, plot_df)
            adjust_candle_price_ylim(ax, plot_df)

            ax.legend(loc="upper left", ncol=4, frameon=False, fontsize=26, labelcolor=TEXT)
            ax.yaxis.tick_right()
            for lab in ax.get_yticklabels():
                lab.set_fontweight("bold")

            latest = plot_df.iloc[-1]
            prev_close = plot_df["Close"].iloc[-2] if len(plot_df) >= 2 else latest["Close"]
            diff = latest["Close"] - prev_close
            pct = diff / prev_close * 100 if prev_close else np.nan
            latest_info = (
                f"{plot_df.index[-1].strftime('%Y/%m/%d')}  "
                f"開 {latest['Open']:.2f}  高 {latest['High']:.2f}  "
                f"低 {latest['Low']:.2f}  收 {latest['Close']:.2f}  "
                f"{diff:+.2f} ({pct:+.2f}%)"
            )
            ax.text(0.012, 0.94, latest_info, transform=ax.transAxes, color=TEXT, fontsize=27,
                    ha="left", va="top",
                    bbox=dict(facecolor=PANEL2, edgecolor=GRID, boxstyle="round,pad=0.30", alpha=0.95))

            ma_note = get_ma_kline_signals(plot_df)
            if ma_note:
                ax.text(0.5, 0.08, ma_note, transform=ax.transAxes, color=NAVY, fontsize=34,
                        fontweight="bold", ha="center", va="center",
                        bbox=dict(facecolor="#F6F8FB", edgecolor=NAVY,
                                  boxstyle="round,pad=0.28", alpha=0.95))
            data_axes.append(ax)

        elif kind == "volume":
            ax = fig.add_subplot(gs[i, :], sharex=candle_ax)
            style_ax(ax)
            up = plot_df["Close"] >= plot_df["Open"]
            vol_lots = plot_df["Volume"] / 1000
            ax.bar([j for j in x if up.iloc[j]], vol_lots[up], color=RED, width=0.72, alpha=0.72)
            ax.bar([j for j in x if not up.iloc[j]], vol_lots[~up], color=GREEN, width=0.72, alpha=0.72)
            if "MV5" in plot_df.columns:
                ax.plot(x, plot_df["MV5"] / 1000, color=BLUE, linewidth=2.1, label="5日均量")
            if "MV20" in plot_df.columns:
                ax.plot(x, plot_df["MV20"] / 1000, color=PURPLE, linewidth=2.1, label="20日均量")
            adjust_volume_ylim(ax, plot_df)
            ax.text(0.001, 1.14, "成交量（張）", transform=ax.transAxes, color=NAVY,
                    fontsize=34, fontweight="bold", ha="left", va="center", clip_on=False)
            ax.legend(loc="upper right", ncol=2, frameon=False, fontsize=24, labelcolor=TEXT)
            data_axes.append(ax)

        else:  # custom
            ax = fig.add_subplot(gs[i, :])
            style_ax(ax, row_title)
            if callable(draw_fn):
                draw_fn(ax, plot_df, x)
            data_axes.append(ax)

    # x 軸：全部對齊同一範圍，只有最後一列顯示日期標籤。
    for ax in data_axes:
        ax.set_xlim(-1, len(x))
        plt.setp(ax.get_xticklabels(), visible=False)

    if data_axes:
        last_ax = data_axes[-1]
        interval = max(1, len(x) // max(1, max_xticks))
        last_ax.set_xticks(x[::interval])
        last_ax.set_xticklabels([date_labels[j] for j in range(0, len(date_labels), interval)],
                                rotation=30, ha="right", color=MUTED, fontsize=26)
        plt.setp(last_ax.get_xticklabels(), visible=True)

    add_center_watermarks(fig, watermark_text)
    fig.subplots_adjust(left=0.035, right=0.965, top=0.975, bottom=0.03)
    return fig


# ============================================================
# 輸出
# ============================================================

def fig_to_png_buffer(fig, dpi=None, max_width=None) -> BytesIO:
    """輸出 PNG 到 BytesIO，並做一次「截圖式」縮圖。

    長圖 dpi 高、檔案大，直接推 Discord 容易超過限制。這裡保留原始排版，
    只把像素等比例縮到接近螢幕寬度再重壓。文字多，維持 PNG 避免 JPEG 雜訊。
    """
    dpi = int(dpi or REPORT_OUTPUT_DPI)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0.18,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)

    if Image is None:
        print("⚠️ Pillow 未安裝，略過截圖式二次輸出")
        return buf

    max_width = int(max_width or SCREENSHOT_OUTPUT_MAX_WIDTH)
    try:
        img = Image.open(buf).convert("RGB")
        old_w, old_h = img.size
        if max_width > 0 and old_w > max_width:
            scale = max_width / max(old_w, 1)
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((max(1, int(old_w * scale)), max(1, int(old_h * scale))), resample)
        out = BytesIO()
        img.save(out, format="PNG", optimize=False, compress_level=6)
        out.seek(0)
        return out
    except Exception as e:
        print(f"⚠️ 截圖式二次輸出失敗，改用原圖：{e}")
        buf.seek(0)
        return buf
