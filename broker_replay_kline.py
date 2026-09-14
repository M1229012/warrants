# -*- coding: utf-8 -*-
"""
分點權證波段複盤 K 線圖｜broker_replay_kline
=============================================

用途：
挑一個分點＋一檔（或多檔）股票，把該分點「大額買進權證 → 賣出出清」的波段畫成一頁式 K 線長圖，
並算出：
  - 同一段期間「單純買現股」的報酬率（進場日收盤 → 出場日收盤）
  - 分點實際「買權證」的報酬率（沿用回測程式的 FIFO 已實現報酬）
圖片底部附「此分點在這檔股票的歷次波段」表格與習性統計，方便大家複盤。
另外輸出波段總表 CSV 與可直接用瀏覽器打開的 index.html。

兩種出圖方式（REPLAY_CHART_MODE）：
- 分段：選到的每一次買賣各一張，K 線只畫那一段（前後加少量留白），圖不會拉太長。
- 整段：選到的波段全部畫在同一張，從第一段進場畫到最後一段出場，看分點來回操作的節奏。
- 兩種：兩種都出。
第幾次買賣由 REPLAY_ROUNDS 指定（最新／全部／3／1,3／2-4／近3／-2）。
編號在 log、GitHub Actions 的 Summary 頁與圖內表格都看得到；第一次可先跑「全部」看清單。

資料來源（全部沿用既有程式，判斷規則不重寫）：
1. 分點歷史：warrant_backtest_moneydj 的本機歷史快取
   CACHE_DIR/broker_warrant_history_cache.csv(.parquet)，保留最近 HISTORY_RETENTION_TRADING_DAYS 個交易日。
2. 大額買進與出場：同一支程式的 build_amount_class_events()
   ——A~E 金額強度分類＋跨事件 FIFO 扣減，與 Google Sheet ABCDE 表是同一套定義。
3. K 線：同一支程式的 _pattern_moneydj_price_dataframe()（MoneyDJ czkc1 日 K，免 token）。
   這是回測模組的內部函式，回測那邊改名時這裡要跟著改。
4. 版型：kline_core.py（從 K_function 週報抽出的一頁式 K 線），放在本檔同一個資料夾。

波段定義：
- 起點：ABCDE 事件日——分點對該股任一權證單日買進 ≥ 100 萬，且同日同標的累積達 A 級以上。
- 終點：該事件買進的權證依 FIFO 全部賣完那天（出清日）。
- 上一段還沒出清前又出現大額買進，視為加碼，併成同一段。
- 還沒出清的波段畫到快取最新交易日，權證報酬只算已賣出的部分。

執行位置：
回測程式的歷史快取只存在跑過回測的機器（GitHub Actions 的 warrant_cache）。
GitHub Actions 用 broker-replay-kline.yml，會以唯讀方式還原回測 workflow 存下的 warrant_cache；
本機要跑的話，把 CACHE_DIR 指到下載回來的快取資料夾。
本程式只讀快取，不會寫回快取，也不會動 Google Sheet。

主要環境變數：
- REPLAY_BROKER：分點標籤／顯示名稱／券商代號，例如「元大南屯」「元大-南屯」「9853」（必填）
- REPLAY_STOCKS：股票代號，逗號分隔；空白＝該分點最近有大額買進的前 REPLAY_MAX_STOCKS 檔
- REPLAY_CHART_MODE：分段／整段／兩種（預設分段）
- REPLAY_ROUNDS：第幾次買賣（預設最新）
- REPLAY_BACKTEST_PATH：回測程式路徑；空白時在本檔資料夾找最新的 warrant_backtest_moneydj*.py
- REPLAY_OUTPUT_DIR：輸出資料夾；空白時用回測程式的 OUTPUT_DIR/broker_replay
- REPLAY_DISCORD_ENABLE：1 才推 Discord（預設關閉）

GitHub Actions 建議 secrets：
- REPLAY_DISCORD_WEBHOOK_URL（或沿用 DISCORD_WEBHOOK_URL_TEST／DISCORD_WEBHOOK_URL）

必要套件：
pip install requests pandas numpy matplotlib pillow pyarrow
"""

import bisect
import glob
import html
import importlib.util
import os
import re
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

# kline_core.py 與本檔放在同一個資料夾（從 onepage-kline skill 複製過來）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from kline_core import (  # noqa: E402
    GRID,
    GREEN,
    MUTED,
    NAVY,
    RED,
    BLUE,
    TEXT,
    adjust_stacked_ylim,
    calculate_indicators,
    fig_to_png_buffer,
    normalize_ohlcv,
    plot_onepage_kline,
)


# ============================================================
# 設定區
# ============================================================

# 分點可填 FALLBACK 的標籤（元大南屯）、顯示名稱（元大-南屯）或券商代號（9853）。
# 券商代號大小寫敏感：9A9g 是永豐金內湖、9A9G 是永豐金天母，照原樣填，不要轉大小寫。
REPLAY_BROKER = os.getenv("REPLAY_BROKER", "").strip()
REPLAY_STOCKS = os.getenv("REPLAY_STOCKS", os.getenv("REPLAY_STOCK", "")).strip()
# Downloads 常同時存在 (17)、(18) 多個版本，沒指定時取最後修改的那一份。
REPLAY_BACKTEST_PATH = os.getenv("REPLAY_BACKTEST_PATH", "").strip()
REPLAY_OUTPUT_DIR = os.getenv("REPLAY_OUTPUT_DIR", "").strip()

# 出圖方式：分段＝每次買賣一張；整段＝選到的波段畫在同一張；兩種＝都出。
# 同一檔股票分點可能來回操作十幾次，整段 K 線會拉很長，所以預設只出分段。
REPLAY_CHART_MODE = os.getenv("REPLAY_CHART_MODE", "分段").strip()
# 第幾次買賣：最新／全部／3／1,3／2-4／近3（最近三段）／-2（倒數第二段）。
# 分段模式每個編號一張；整段模式把選到的波段從第一段畫到最後一段。
REPLAY_ROUNDS = os.getenv("REPLAY_ROUNDS", "最新").strip()

# 沒指定股票時最多畫幾檔（依最近一次大額買進日排序），避免大分點一次產出上百張圖。
REPLAY_MAX_STOCKS = max(1, int(os.getenv("REPLAY_MAX_STOCKS", "10")))
# 只保留「最大級距」達到這一級以上的波段（A 最小、E 最大）。波段編號是篩選後重新編的。
REPLAY_MIN_CLASS = os.getenv("REPLAY_MIN_CLASS", "A").strip().upper()[:1] or "A"
REPLAY_INCLUDE_OPEN = os.getenv("REPLAY_INCLUDE_OPEN", "1").strip().lower() not in ("0", "false", "no", "off")

# 波段前後各多畫幾根 K 棒：前面要看得到「分點進場前股價在哪」，
# 後面要看得到「出場後有沒有續漲」，這才是複盤真正要比對的地方。
REPLAY_PAD_BEFORE_BARS = max(0, int(os.getenv("REPLAY_PAD_BEFORE_BARS", "20")))
REPLAY_PAD_AFTER_BARS = max(0, int(os.getenv("REPLAY_PAD_AFTER_BARS", "10")))
# 隔日沖這種一兩天的波段也至少畫這麼多根，否則 K 棒寬到失真、均線也看不出方向。
REPLAY_MIN_WINDOW_BARS = max(10, int(os.getenv("REPLAY_MIN_WINDOW_BARS", "45")))

# MoneyDJ czkc1 的 c 參數＝回傳最近幾根 K 棒。分點歷史保留 200 個交易日，
# 再加上 MA60 暖身 60 根與前後留白，回測預設的 240 根不夠用，這裡拉到 400。
# 回測模組是在 import 時讀這個值，所以必須在載入回測模組「之前」寫進環境變數；
# 使用者自己設定過 PATTERN_MONEYDJ_KLINE_LIMIT 時以使用者的為準。
REPLAY_KLINE_LIMIT = max(260, int(os.getenv("REPLAY_KLINE_LIMIT", "400")))
os.environ.setdefault("PATTERN_MONEYDJ_KLINE_LIMIT", str(REPLAY_KLINE_LIMIT))

# 現股報酬用還原股價（含除權息）計算，K 線仍畫原始股價，才會和當時看盤的價位一致。
# 還原價抓不到時自動退回原始股價，表格註腳會標明基準。
REPLAY_STOCK_RETURN_ADJUSTED = os.getenv("REPLAY_STOCK_RETURN_ADJUSTED", "1").strip().lower() not in ("0", "false", "no", "off")
# 現股漲跌幅絕對值小於這個 % 時不算「權證／現股倍數」：分母太小，倍數會失真到幾百倍。
REPLAY_LEVERAGE_MIN_STOCK_PCT = max(0.1, float(os.getenv("REPLAY_LEVERAGE_MIN_STOCK_PCT", "1.0")))

# 賣出天數很多時（分批出場），只有金額最大的前幾天加文字標籤，其餘只畫三角形，避免字疊成一團。
# 整段圖一次有好幾段，每段只標更少的賣出日。出清日一律標。
REPLAY_SELL_LABEL_MAX = max(1, int(os.getenv("REPLAY_SELL_LABEL_MAX", "6")))
REPLAY_FULL_SELL_LABEL_MAX = max(0, int(os.getenv("REPLAY_FULL_SELL_LABEL_MAX", "1")))
# 歷次波段表格最多列幾段；超過時以本段為中心取前後各半。
REPLAY_TABLE_MAX_ROWS = max(3, int(os.getenv("REPLAY_TABLE_MAX_ROWS", "12")))
# 長區間的加權價量分布是逐列迴圈，趕時間可以關掉。
REPLAY_SHOW_VOLUME_PROFILE = os.getenv("REPLAY_SHOW_VOLUME_PROFILE", "1").strip().lower() not in ("0", "false", "no", "off")
REPLAY_HTML_INDEX_ENABLE = os.getenv("REPLAY_HTML_INDEX_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")

# 日 K 抓取允許失敗檔數。超過就中止，不產出缺一半的複盤。
MAX_ALLOWED_PRICE_ERRORS = max(0, int(os.getenv("REPLAY_MAX_ALLOWED_PRICE_ERRORS", "3")))

# 推 Discord 是對外公開的動作，預設關閉；確認圖沒問題再打開。
REPLAY_DISCORD_ENABLE = os.getenv("REPLAY_DISCORD_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
DISCORD_WEBHOOK_URL = (
    os.getenv("REPLAY_DISCORD_WEBHOOK_URL")
    or os.getenv("DISCORD_WEBHOOK_URL_TEST")
    or os.getenv("DISCORD_WEBHOOK_URL")
    or ""
)
REPLAY_DISCORD_SLEEP_SECONDS = max(0.0, float(os.getenv("REPLAY_DISCORD_SLEEP_SECONDS", "1.5")))

AMOUNT_CLASS_ORDER = "ABCDE"
CANDLE_TITLE = "股價趨勢｜K線、均線、布林｜▲ 大額買進權證　▼ 賣出權證"
# (欄名, x 位置, 對齊)。x 用 transAxes 0~1，寬 28 吋長圖、25pt 字實測不會互相壓到。
TABLE_COLUMNS = [
    ("#", 0.012, "left"),
    ("進場日", 0.050, "left"),
    ("出場日", 0.160, "left"),
    ("持有", 0.300, "right"),
    ("最大級距", 0.325, "left"),
    ("權證買進", 0.505, "right"),
    ("權證報酬", 0.615, "right"),
    ("現股報酬", 0.725, "right"),
    ("期間最高", 0.835, "right"),
    ("狀態", 0.870, "left"),
]


# ============================================================
# 基本工具
# ============================================================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def to_float(x, default=0.0):
    try:
        value = float(str(x).replace(",", "").replace("%", "").strip())
    except Exception:
        return default
    return value if np.isfinite(value) else default


def _date_key(value) -> str:
    """任何日期格式 → 回測程式使用的 YYYY/MM/DD 字串；無法解析回空字串。"""
    s = str(value if value is not None else "").strip().split(" ")[0].replace("/", "-")
    if not s:
        return ""
    ts = pd.to_datetime(s, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y/%m/%d")


def _stock_key(value) -> str:
    """保留台股／ETF 英數代號（2330、00981A），與回測程式 _normalize_pattern_stock_code 同規則。"""
    s = str(value or "").strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    s = re.sub(r"[^0-9A-Z]", "", s.split()[0]) if s else ""
    return s if 4 <= len(s) <= 8 else ""


def _warrant_key(value) -> str:
    # lots 與 daily_records 的權證代號都出自回測同一條正規化流程，這裡只需去空白。
    return str(value or "").strip().upper()


def _safe_name(value) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(value or "").strip()) or "unknown"


def fmt_wan(value) -> str:
    v = to_float(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f}億"
    return f"{v / 1e4:,.0f}萬"


def fmt_wan_signed(value) -> str:
    v = to_float(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:+,.2f}億"
    return f"{v / 1e4:+,.0f}萬"


def fmt_pct(value) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{value:+.1f}%"


def pct_color(value):
    if value is None or not np.isfinite(value) or abs(value) < 1e-9:
        return NAVY
    return RED if value > 0 else GREEN


def class_rank(code) -> int:
    code = str(code or "").strip().upper()
    return AMOUNT_CLASS_ORDER.index(code) if code and code in AMOUNT_CLASS_ORDER else -1


def _mean(values):
    values = [float(v) for v in values if v is not None and np.isfinite(v)]
    return sum(values) / len(values) if values else None


def _round_or_blank(value, digits=2):
    return "" if value is None or not np.isfinite(value) else round(float(value), digits)


def parse_chart_modes(raw) -> set:
    """REPLAY_CHART_MODE → {"segment"} / {"full"} / 兩者。workflow 選單的中文長標籤也吃。"""
    s = str(raw or "").strip().lower()
    if ("整段" in s and "分段" in s) or "兩種" in s or s == "both":
        return {"segment", "full"}
    if "整段" in s or s in ("full", "whole"):
        return {"full"}
    if s and "分段" not in s and s not in ("segment", "seg"):
        log(f"⚠️ 看不懂 REPLAY_CHART_MODE={raw}，改用分段")
    return {"segment"}


def parse_round_selection(spec, total) -> list:
    """REPLAY_ROUNDS → 1 起算的波段編號（排序、去重）。

    支援：最新／全部／3／1,3／2-4／近3（最近三段）／-2（倒數第二段），可混用：「1,近2」。
    """
    if total <= 0:
        return []
    s = str(spec or "").strip().lower()
    for old, new in (("，", ","), ("、", ","), ("～", "-"), ("~", "-"), ("－", "-")):
        s = s.replace(old, new)
    if s in ("", "全部", "all"):
        return list(range(1, total + 1))

    picked = set()
    for token in re.split(r"[,;\s]+", s):
        if not token:
            continue
        if token in ("最新", "latest", "last"):
            picked.add(total)
            continue
        if token in ("全部", "all"):
            picked.update(range(1, total + 1))
            continue
        m = re.fullmatch(r"(?:近|最近|last)(\d+)", token)
        if m:
            picked.update(range(max(1, total - int(m.group(1)) + 1), total + 1))
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            a, b = sorted(int(x) for x in m.groups())
            picked.update(range(a, b + 1))
            continue
        m = re.fullmatch(r"-(\d+)", token)
        if m:
            picked.add(total - int(m.group(1)) + 1)
            continue
        if token.isdigit():
            picked.add(int(token))
            continue
        log(f"⚠️ 看不懂的波段編號「{token}」，略過")

    dropped = sorted(n for n in picked if not 1 <= n <= total)
    if dropped:
        log(f"⚠️ 波段編號超出範圍（本檔共 {total} 段）：{dropped}")
    return sorted(n for n in picked if 1 <= n <= total)


# ============================================================
# 資料層：載入回測模組、分點歷史、ABCDE 事件、日 K
# ============================================================

def _find_backtest_path() -> str:
    if REPLAY_BACKTEST_PATH:
        if not os.path.isfile(REPLAY_BACKTEST_PATH):
            raise RuntimeError(f"REPLAY_BACKTEST_PATH 不存在：{REPLAY_BACKTEST_PATH}")
        return REPLAY_BACKTEST_PATH
    candidates = glob.glob(os.path.join(_HERE, "warrant_backtest_moneydj*.py"))
    if not candidates:
        raise RuntimeError("找不到 warrant_backtest_moneydj*.py，請設定 REPLAY_BACKTEST_PATH")
    return max(candidates, key=os.path.getmtime)


def load_backtest_module():
    """用 importlib 直接載入回測程式（檔名可能含空白與括號，不能一般 import）。

    回測程式刻意維持單一檔案，模組層有大量 global 快取；整支當成一個模組載入，
    它的函式讀寫的都是同一份 global，不會有拆模組後讀到舊物件的問題。
    模組層只有 os.makedirs 與 TZ 設定，不會在 import 時連網或跑主流程。
    """
    path = _find_backtest_path()
    spec = importlib.util.spec_from_file_location("warrant_backtest_for_replay", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    log(f"📦 載入回測模組：{os.path.basename(path)}｜{getattr(module, 'PROGRAM_BUILD_ID', '-')}")
    return module


def resolve_broker(bt, raw):
    """分點標籤／顯示名稱／券商代號 → (標籤, 券商代號, 顯示名稱)。"""
    raw = str(raw or "").strip()
    if not raw:
        raise RuntimeError("請設定 REPLAY_BROKER（分點標籤或券商代號，例如 元大南屯／9853）")
    for label, pair in bt.FULL_FALLBACK.items():
        name, code = str(pair[0] or "").strip(), str(pair[1] or "").strip()
        if raw in (label, name, code):
            return label, code, name or label
    # 歷史快取載入時會把不在分點清單的券商整批清掉，不在清單的代號一定查不到，直接擋下。
    raise RuntimeError(
        f"REPLAY_BROKER={raw} 不在回測程式的分點清單內。可用分點：{'、'.join(bt.FULL_FALLBACK.keys())}"
    )


def load_broker_history(bt, broker_code):
    """讀回測歷史快取，只留指定分點。回傳 (分點歷史 DataFrame, 快取最新交易日)。"""
    history_df = bt.load_history_cache()
    if history_df is None or history_df.empty:
        raise RuntimeError(
            f"讀不到分點歷史快取：{bt.HISTORY_CACHE_PATH}.parquet。"
            "請確認 CACHE_DIR 指向回測程式的 warrant_cache 資料夾，且 USE_CACHE 沒有關掉。"
        )
    # load_history_cache 已把日期正規化成 YYYY/MM/DD，字串最大值就是最新交易日；
    # 不要逐列 _date_key，全分點歷史動輒十幾萬列。
    latest_key = _date_key(history_df["日期"].astype(str).max())
    wanted = bt.normalize_broker_code_for_compare(broker_code)
    mask = history_df["券商代號"].map(bt.normalize_broker_code_for_compare) == wanted
    broker_df = history_df[mask].copy()
    if not broker_df.empty:
        log(
            f"✅ 分點歷史：{len(broker_df):,} 筆｜"
            f"{broker_df['日期'].min()} ~ {broker_df['日期'].max()}｜快取最新交易日 {latest_key}"
        )
    return broker_df, latest_key


def build_broker_events(bt, broker_history):
    """沿用回測主流程的 items → daily_records → ABCDE 事件（含 FIFO 出清）。

    FIFO 佇列的鍵是（券商代號, 權證代號），不同分點彼此獨立，
    所以只丟單一分點進去算，結果與回測全分點一起算完全相同。
    回測主流程會先載入權證日期主檔做身分防錯；這裡沒有載入，身分以快取裡已修正過的欄位為準。
    """
    items = bt.items_from_history_cache(broker_history)
    # 鍵的組法與回測主流程相同：(item 的券商代號, 權證代號)。FIFO 找賣出資料靠這個鍵。
    item_map = {(item["broker_code"], item["warrant_code"]): item for item in items}
    daily_records = bt.build_daily_records(items)
    grouped = bt.build_amount_class_events(daily_records, item_map)
    events = [ev for code in bt.AMOUNT_CLASS_CODES for ev in grouped.get(code, [])]

    flow_df = pd.DataFrame(daily_records)
    if flow_df.empty:
        flow_df = pd.DataFrame(columns=["標的股", "日期", "權證代號", "買進金額", "賣出金額"])
    else:
        flow_df = flow_df[["標的股", "日期", "權證代號", "買進金額", "賣出金額"]].copy()
        flow_df["標的股"] = flow_df["標的股"].map(_stock_key)
        flow_df["日期"] = (
            pd.to_datetime(flow_df["日期"].astype(str).str.replace("/", "-", regex=False), errors="coerce")
            .dt.strftime("%Y/%m/%d")
            .fillna("")
        )
        flow_df["權證代號"] = flow_df["權證代號"].map(_warrant_key)
        for col in ["買進金額", "賣出金額"]:
            flow_df[col] = pd.to_numeric(flow_df[col], errors="coerce").fillna(0.0)
    return events, flow_df


def pick_stock_codes(events):
    if REPLAY_STOCKS:
        codes = [_stock_key(x) for x in re.split(r"[,;；、\s]+", REPLAY_STOCKS)]
        return list(dict.fromkeys(c for c in codes if c))
    latest = {}
    for ev in events:
        code = _stock_key(ev.get("標的股"))
        day = _date_key(ev.get("事件日"))
        if code and day:
            latest[code] = max(latest.get(code, ""), day)
    return sorted(latest, key=lambda c: latest[c], reverse=True)[:REPLAY_MAX_STOCKS]


def stock_daily_flow(flow_df, stock_code) -> dict:
    """分點對該股「所有權證」的每日買賣金額（含未達門檻的小單），畫資金流面板用。"""
    sub = flow_df[flow_df["標的股"] == stock_code]
    if sub.empty:
        return {}
    grouped = sub.groupby("日期")[["買進金額", "賣出金額"]].sum()
    return {day: (float(row["買進金額"]), float(row["賣出金額"])) for day, row in grouped.iterrows()}


def fetch_kline_frames(bt, stock_code):
    """原始日 K（畫圖用）＋還原收盤對照（算現股報酬用）。"""
    raw, source = bt._pattern_moneydj_price_dataframe(stock_code, "1990/01/01", "2999/12/31", adjusted=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"MoneyDJ 日 K 無資料：{stock_code}（來源 {source}）")
    # 指標用完整序列先算好再切窗，窗口前段的 MA60／布林才不會是空的。
    kdf = calculate_indicators(normalize_ohlcv(raw))

    adj_close = None
    if REPLAY_STOCK_RETURN_ADJUSTED:
        try:
            adj, _ = bt._pattern_moneydj_price_dataframe(stock_code, "1990/01/01", "2999/12/31", adjusted=True)
            adj_df = normalize_ohlcv(adj)
            adj_close = dict(zip(adj_df.index.strftime("%Y/%m/%d"), adj_df["Close"].astype(float)))
        except Exception as exc:
            log(f"⚠️ {stock_code}｜還原股價抓取失敗，現股報酬改用原始股價：{exc}")

    log(
        f"✅ {stock_code}｜日 K {len(kdf):,} 根｜"
        f"{kdf.index.min():%Y/%m/%d} ~ {kdf.index.max():%Y/%m/%d}｜來源 {source}"
    )
    return kdf, adj_close


# ============================================================
# 運算層：波段合併、報酬率、習性統計
# ============================================================

def group_rounds(events, stock_code):
    """把同一檔股票的 ABCDE 事件依持有期間重疊合併成波段。"""
    evs = [ev for ev in events if _stock_key(ev.get("標的股")) == stock_code]
    evs.sort(key=lambda ev: _date_key(ev.get("事件日") or ev.get("起始日")))

    rounds, cur = [], None
    for ev in evs:
        start = _date_key(ev.get("事件日") or ev.get("起始日"))
        if not start:
            continue
        exit_key = _date_key(ev.get("出清日")) if str(ev.get("狀態", "")).strip() == "出清" else ""
        # 上一段還沒出清（或出清當天）又大額買進 → 視為加碼，併進同一段。
        if cur is not None and (cur["open"] or start <= cur["exit"]):
            cur["events"].append(ev)
            if exit_key:
                cur["exit"] = max(cur["exit"], exit_key)
            else:
                cur["open"] = True
            continue
        cur = {"events": [ev], "start": start, "exit": exit_key, "open": not exit_key}
        rounds.append(cur)
    return rounds


def summarize_round(rnd, latest_key, flow_df, stock_code):
    """彙總一段波段的權證部位：買進金額、FIFO 已實現報酬、買進日與賣出日。"""
    events = rnd["events"]
    lots = [lot for ev in events for lot in (ev.get("lots") or [])]
    total_cost = sum(to_float(lot.get("金額")) for lot in lots)
    orig_shares = sum(to_float(lot.get("股數")) for lot in lots)
    remain_shares = sum(to_float(lot.get("剩餘股數")) for lot in lots)
    realized_rev = sum(to_float(ev.get("已實現賣出金額")) for ev in events)
    realized_cost = sum(to_float(ev.get("已實現成本")) for ev in events)
    warrant_codes = sorted({_warrant_key(lot.get("權證代號")) for lot in lots} - {""})

    end = latest_key if rnd["open"] else rnd["exit"]
    buy_days = sorted(
        (
            {
                "date": _date_key(ev.get("事件日")),
                "class": str(ev.get("事件代碼", "")).strip().upper(),
                "amount": to_float(ev.get("單日累積買進金額")),
            }
            for ev in events
        ),
        key=lambda b: b["date"],
    )

    # FIFO 只留下「哪幾天的賣出動到這一段」，沒有記每天扣了多少；
    # 標籤金額取這一段用到的權證在那幾天的全部賣出金額，當作出場力道參考。
    exit_dates = sorted({
        _date_key(d) for ev in events for d in (ev.get("賣出影響日清單") or []) if _date_key(d)
    })
    sells = flow_df[
        (flow_df["標的股"] == stock_code)
        & flow_df["權證代號"].isin(warrant_codes)
        & flow_df["日期"].isin(exit_dates)
    ]
    sell_by_day = sells.groupby("日期")["賣出金額"].sum().to_dict() if not sells.empty else {}
    exit_days = [{"date": d, "amount": to_float(sell_by_day.get(d))} for d in exit_dates]

    if not rnd["open"]:
        status = "已出清"
    elif realized_cost > 0:
        status = "減碼中"
    else:
        status = "持有中"

    top_event = max(events, key=lambda ev: class_rank(ev.get("事件代碼")))
    max_class = str(top_event.get("事件代碼", "")).strip().upper()
    max_class_text = str(top_event.get("事件類型", "") or max_class).replace("-", " ")

    start_dt = pd.Timestamp(rnd["start"].replace("/", "-"))
    end_dt = pd.Timestamp(end.replace("/", "-"))
    rnd.update({
        "end": end,
        "status": status,
        "hold_days": max((end_dt - start_dt).days, 0),
        "max_class": max_class,
        "max_class_text": max_class_text,
        "total_cost": total_cost,
        "realized_rev": realized_rev,
        "realized_cost": realized_cost,
        "sold_ratio": 1 - remain_shares / orig_shares if orig_shares > 0 else 0.0,
        # 出清時已實現成本＝全部買進成本，與 ABCDE 表的「出清獲利%」同一個算法。
        "warrant_pct": (realized_rev - realized_cost) / realized_cost * 100 if realized_cost > 0 else None,
        "warrant_codes": warrant_codes,
        "buy_days": buy_days,
        "exit_days": exit_days,
        "stock_name": next(
            (str(ev.get("標的名稱") or "").strip() for ev in events if str(ev.get("標的名稱") or "").strip()),
            "",
        ),
        "image": "",
    })
    return rnd


def attach_stock_metrics(rnd, kdf, adj_close):
    """同期現股報酬：進場日收盤 → 出場日收盤（持有中則到最新交易日）。"""
    rnd.update({
        "entry_date": "", "exit_date": "", "entry_close": None, "exit_close": None,
        "stock_pct": None, "peak_pct": None, "leverage": None,
        "trading_days": 0, "return_basis": "原始",
    })
    keys = list(kdf.index.strftime("%Y/%m/%d"))
    s_pos = bisect.bisect_left(keys, rnd["start"])
    e_pos = bisect.bisect_right(keys, rnd["end"]) - 1
    if s_pos >= len(keys) or e_pos < s_pos:
        return rnd

    entry_raw = float(kdf["Close"].iloc[s_pos])
    exit_raw = float(kdf["Close"].iloc[e_pos])
    entry, exit_, basis = entry_raw, exit_raw, "原始"
    if adj_close:
        a0, a1 = to_float(adj_close.get(keys[s_pos])), to_float(adj_close.get(keys[e_pos]))
        if a0 > 0 and a1 > 0:
            entry, exit_, basis = a0, a1, "還原"

    stock_pct = (exit_ / entry - 1) * 100 if entry > 0 else None
    peak = float(kdf["High"].iloc[s_pos:e_pos + 1].max())
    leverage = None
    warrant_pct = rnd.get("warrant_pct")
    # 倍數只在出清後才有意義：減碼中的權證報酬只含已賣部分，和現股的整段報酬不能直接相除。
    if (
        not rnd["open"]
        and warrant_pct is not None
        and stock_pct is not None
        and abs(stock_pct) >= REPLAY_LEVERAGE_MIN_STOCK_PCT
    ):
        leverage = warrant_pct / stock_pct

    rnd.update({
        "entry_date": keys[s_pos],
        "exit_date": keys[e_pos],
        "entry_close": entry_raw,
        "exit_close": exit_raw,
        "stock_pct": stock_pct,
        "peak_pct": (peak / entry_raw - 1) * 100 if entry_raw > 0 else None,
        "leverage": leverage,
        "trading_days": e_pos - s_pos,
        "return_basis": basis,
    })
    return rnd


def summarize_habit(rounds) -> str:
    closed = [r for r in rounds if not r["open"]]
    open_n = len(rounds) - len(closed)
    if not closed:
        return f"分點習性｜尚無已出清波段｜持有中 {open_n} 段"
    wins = sum(1 for r in closed if (r["warrant_pct"] or 0) > 0)
    parts = [
        f"已出清 {len(closed)} 段",
        f"權證勝率 {wins / len(closed):.0%}",
        f"平均持有 {_mean([r['hold_days'] for r in closed]):.0f} 天",
    ]
    avg_w = _mean([r["warrant_pct"] for r in closed])
    avg_s = _mean([r["stock_pct"] for r in closed])
    if avg_w is not None:
        parts.append(f"平均權證 {avg_w:+.1f}%")
    if avg_s is not None:
        parts.append(f"平均同期現股 {avg_s:+.1f}%")
    if open_n:
        parts.append(f"持有中 {open_n} 段")
    return "分點習性｜" + "｜".join(parts)


def slice_window(kdf, start_key, end_key):
    """切出「start~end ± 留白」的 K 棒窗口；找不到對應日 K 回 None。"""
    keys = list(kdf.index.strftime("%Y/%m/%d"))
    s_pos = bisect.bisect_left(keys, start_key)
    e_pos = bisect.bisect_right(keys, end_key) - 1
    if s_pos >= len(keys) or e_pos < s_pos:
        return None

    lo = max(0, s_pos - REPLAY_PAD_BEFORE_BARS)
    hi = min(len(keys) - 1, e_pos + REPLAY_PAD_AFTER_BARS)
    need = REPLAY_MIN_WINDOW_BARS - (hi - lo + 1)
    if need > 0:
        # 短波段補根數時先往前補（看進場前的位置比較重要），前面不夠再往後補。
        extra_before = min(lo, need)
        lo -= extra_before
        hi = min(len(keys) - 1, hi + need - extra_before)
    return kdf.iloc[lo:hi + 1].copy()


def round_positions(plot_df, rounds):
    """每段波段在窗口內的 (round, 起點位置, 終點位置)；落在窗口外的略過。"""
    keys = list(plot_df.index.strftime("%Y/%m/%d"))
    out = []
    for r in rounds:
        s = bisect.bisect_left(keys, r["start"])
        e = bisect.bisect_right(keys, r["end"]) - 1
        if s < len(keys) and e >= s:
            out.append((r, s, e))
    return out


def pick_table_rounds(rounds, anchor):
    if len(rounds) <= REPLAY_TABLE_MAX_ROWS:
        return list(rounds)
    idx = next(i for i, r in enumerate(rounds) if r is anchor)
    start = max(0, min(idx - REPLAY_TABLE_MAX_ROWS // 2, len(rounds) - REPLAY_TABLE_MAX_ROWS))
    return rounds[start:start + REPLAY_TABLE_MAX_ROWS]


def format_round_line(r) -> str:
    return (
        f"#{r['no']:>2}  {r['start']} → {r['end']}（{r['status']}，{r['hold_days']} 天）"
        f"｜{r['max_class_text']}｜買進 {fmt_wan(r['total_cost'])}"
        f"｜權證 {fmt_pct(r['warrant_pct'])}｜同期現股 {fmt_pct(r['stock_pct'])}"
    )


# ============================================================
# 繪圖層
# ============================================================

def build_segment_cards(rnd):
    w = rnd["warrant_pct"]
    if rnd["status"] == "已出清":
        w_sub = f"已實現 {fmt_wan_signed(rnd['realized_rev'] - rnd['realized_cost'])}"
    elif rnd["status"] == "減碼中":
        w_sub = f"已賣 {rnd['sold_ratio']:.0%}，其餘未計"
    else:
        w_sub = "尚未賣出"

    s = rnd["stock_pct"]
    s_sub = f"收 {rnd['entry_close']:.2f} → {rnd['exit_close']:.2f}" if rnd["entry_close"] else "無股價資料"

    if rnd["leverage"] is not None:
        lev_value, lev_sub = f"{rnd['leverage']:.1f} 倍", "權證報酬 ÷ 現股報酬"
    elif rnd["open"]:
        lev_value, lev_sub = "-", "出清後才計算"
    else:
        lev_value, lev_sub = "-", f"現股漲跌 < {REPLAY_LEVERAGE_MIN_STOCK_PCT:g}% 不計"

    return [
        ("波段期間", f"{rnd['hold_days']} 天",
         f"{rnd['start'][5:]} → {rnd['end'][5:]}｜{rnd['trading_days']} 交易日", NAVY),
        ("權證買進", fmt_wan(rnd["total_cost"]),
         f"最大 {rnd['max_class_text']}｜{len(rnd['buy_days'])} 次", RED),
        ("分點權證報酬", fmt_pct(w), w_sub, pct_color(w)),
        ("同期現股報酬", fmt_pct(s), s_sub, pct_color(s)),
        ("權證／現股", lev_value, lev_sub, NAVY),
    ]


def build_full_cards(chart_rounds):
    closed = [r for r in chart_rounds if not r["open"]]
    wins = sum(1 for r in closed if (r["warrant_pct"] or 0) > 0)
    pnl = sum(r["realized_rev"] - r["realized_cost"] for r in chart_rounds)
    avg_w = _mean([r["warrant_pct"] for r in closed])
    avg_s = _mean([r["stock_pct"] for r in closed])
    top = max(chart_rounds, key=lambda r: class_rank(r["max_class"]))
    win_sub = f"勝率 {wins / len(closed):.0%}（已出清 {len(closed)} 段）" if closed else "尚無已出清波段"
    return [
        ("波段數", f"{len(chart_rounds)} 段",
         f"{chart_rounds[0]['start'][2:]} → {chart_rounds[-1]['end'][2:]}", NAVY),
        ("權證總買進", fmt_wan(sum(r["total_cost"] for r in chart_rounds)),
         f"最大 {top['max_class_text']}", RED),
        ("權證已實現損益", fmt_wan_signed(pnl), win_sub, pct_color(pnl)),
        ("平均權證報酬", fmt_pct(avg_w), "已出清波段平均", pct_color(avg_w)),
        ("平均同期現股", fmt_pct(avg_s), "已出清波段平均", pct_color(avg_s)),
    ]


def make_flow_drawer(stock_flow, spans):
    """分點權證每日買（紅，正值）賣（綠，負值）金額柱，右軸疊區間累計淨買折線。"""
    def draw(ax, plot_df, x):
        keys = list(plot_df.index.strftime("%Y/%m/%d"))
        buy = np.array([stock_flow.get(k, (0.0, 0.0))[0] for k in keys], dtype=float) / 1e4
        sell = np.array([stock_flow.get(k, (0.0, 0.0))[1] for k in keys], dtype=float) / 1e4

        for _r, s_idx, e_idx in spans:
            ax.axvspan(s_idx - 0.5, e_idx + 0.5, color=NAVY, alpha=0.06, zorder=0)
        ax.bar(x, buy, color=RED, width=0.72, alpha=0.85, label="權證買進", zorder=3)
        ax.bar(x, -sell, color=GREEN, width=0.72, alpha=0.85, label="權證賣出", zorder=3)
        ax.axhline(0, color=MUTED, linewidth=1.0, alpha=0.6)
        adjust_stacked_ylim(ax, [buy, -sell])
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:,.0f}"))

        cum = np.cumsum(buy - sell)
        ax2 = ax.twinx()
        ax2.plot(x, cum, color=BLUE, linewidth=2.6, label="區間累計淨買", zorder=4)
        lo, hi = min(float(cum.min()), 0.0), max(float(cum.max()), 0.0)
        span = max(hi - lo, 1.0)
        ax2.set_ylim(lo - span * 0.18, hi + span * 0.32)
        ax2.tick_params(colors=MUTED, labelsize=24)
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:,.0f}"))
        for spine in ax2.spines.values():
            spine.set_color(GRID)

        handles, labels = [], []
        for a in (ax, ax2):
            h, lab = a.get_legend_handles_labels()
            handles += h
            labels += lab
        # legend 掛在 ax2：畫在折線上層，才不會被累計線穿過。
        ax2.legend(handles, labels, loc="upper left", ncol=3, frameon=False, fontsize=24, labelcolor=TEXT)
    return draw


def make_table_drawer(table_rounds, highlight_ids, habit_text, footnote, ratio):
    """歷次波段表格。全部用 transAxes 座標，不受 kline_core 統一 set_xlim 影響。"""
    def draw(ax, plot_df, x):
        ax.grid(False)
        ax.set_xticks([])
        ax.set_yticks([])

        # 底部固定留約 1.1 個 ratio 單位（≈1.4 吋）放習性統計與註腳，不隨列數縮放。
        top = 0.95
        foot = min(0.45, 1.1 / max(ratio, 1e-6))
        row_h = (top - foot) / (len(table_rounds) + 1)
        y = top - row_h / 2

        for title, cx, ha in TABLE_COLUMNS:
            ax.text(cx, y, title, transform=ax.transAxes, ha=ha, va="center",
                    fontsize=24, color=MUTED, fontweight="bold")
        ax.plot([0.008, 0.992], [top - row_h, top - row_h], transform=ax.transAxes,
                color=GRID, linewidth=1.2)

        for r in table_rounds:
            y -= row_h
            marked = id(r) in highlight_ids
            if marked:
                ax.add_patch(Rectangle((0.006, y - row_h / 2), 0.988, row_h, transform=ax.transAxes,
                                       facecolor=NAVY, alpha=0.08, edgecolor="none", zorder=0))
            cells = [
                (f"▶{r['no']}" if marked else str(r["no"]), NAVY),
                (r["start"], TEXT),
                (r["end"], TEXT),
                (f"{r['hold_days']} 天", TEXT),
                (r["max_class_text"], NAVY),
                (fmt_wan(r["total_cost"]), TEXT),
                (fmt_pct(r["warrant_pct"]), pct_color(r["warrant_pct"])),
                (fmt_pct(r["stock_pct"]), pct_color(r["stock_pct"])),
                (fmt_pct(r["peak_pct"]), pct_color(r["peak_pct"])),
                (r["status"], MUTED if r["open"] else NAVY),
            ]
            for (text, color), (_title, cx, ha) in zip(cells, TABLE_COLUMNS):
                ax.text(cx, y, text, transform=ax.transAxes, ha=ha, va="center",
                        fontsize=25, color=color, fontweight="bold")

        ax.text(0.012, foot * 0.66, habit_text, transform=ax.transAxes, ha="left", va="center",
                fontsize=27, color=NAVY, fontweight="bold")
        ax.text(0.012, foot * 0.24, footnote, transform=ax.transAxes, ha="left", va="center",
                fontsize=21, color=MUTED)
    return draw


def _expand_candle_ylim(ax):
    """kline_core 的 Y 軸只留上 5%／下 11%，三角形與標籤會被裁掉，上下再多撐開一點。"""
    y0, y1 = ax.get_ylim()
    span = y1 - y0
    ax.set_ylim(y0 - span * 0.07, y1 + span * 0.09)
    return span


def draw_round_marks(ax, plot_df, span, rnd, s_idx, e_idx, sell_label_max, tag=""):
    """在 K 線上標出一段波段：區間底色、大額買進日（▲）、賣出日（▼）、進場價。"""
    keys = list(plot_df.index.strftime("%Y/%m/%d"))
    pos = {k: i for i, k in enumerate(keys)}
    lows = plot_df["Low"].astype(float).values
    highs = plot_df["High"].astype(float).values
    label_box = dict(facecolor="white", edgecolor="none", alpha=0.85, boxstyle="round,pad=0.15")

    ax.axvspan(s_idx - 0.5, e_idx + 0.5, color=NAVY, alpha=0.06, zorder=0)

    for b in rnd["buy_days"]:
        i = pos.get(b["date"])
        if i is None:
            continue
        y = lows[i] - span * 0.035
        ax.scatter([i], [y], marker="^", s=620, color=RED, edgecolors="white", linewidths=1.8, zorder=7)
        ax.text(i, y - span * 0.03, f"{b['class']} {fmt_wan(b['amount'])}", ha="center", va="top",
                fontsize=22, color=RED, fontweight="bold", zorder=7, bbox=label_box)

    exits = [e for e in rnd["exit_days"] if e["date"] in pos]
    final_exit = "" if rnd["open"] else rnd["end"]
    labeled = {e["date"] for e in sorted(exits, key=lambda e: e["amount"], reverse=True)[:sell_label_max]}
    for e in exits:
        i = pos[e["date"]]
        is_final = e["date"] == final_exit
        y = highs[i] + span * 0.035
        ax.scatter([i], [y], marker="v", s=760 if is_final else 520, color=GREEN,
                   edgecolors="white", linewidths=1.8, zorder=7)
        if is_final or e["date"] in labeled:
            text = f"出清 {fmt_wan(e['amount'])}" if is_final else f"賣 {fmt_wan(e['amount'])}"
            ax.text(i, y + span * 0.03, text, ha="center", va="bottom",
                    fontsize=22, color=GREEN, fontweight="bold", zorder=7, bbox=label_box)

    if rnd["entry_close"]:
        ax.hlines(rnd["entry_close"], s_idx - 0.5, e_idx + 0.5, colors=NAVY, linestyles="--",
                  linewidth=1.8, alpha=0.8, zorder=5)

    if tag:
        # 編號放在波段底部：頂部是均線 legend 與最新報價框，放上面會互相壓到。
        ax.text(s_idx - 0.4, ax.get_ylim()[0] + span * 0.015, tag, ha="left", va="bottom",
                fontsize=24, color=NAVY, fontweight="bold", zorder=8,
                bbox=dict(facecolor="white", edgecolor=NAVY, alpha=0.9, boxstyle="round,pad=0.2"))


def draw_entry_exit_box(ax, rnd):
    if not rnd["entry_close"]:
        return
    exit_label = "最新" if rnd["open"] else "出場"
    ax.text(0.988, 0.94,
            f"進場 {rnd['entry_date'][5:]} 收 {rnd['entry_close']:.2f} → "
            f"{exit_label} {rnd['exit_date'][5:]} 收 {rnd['exit_close']:.2f}",
            transform=ax.transAxes, ha="right", va="top", fontsize=27, color=NAVY, fontweight="bold",
            bbox=dict(facecolor="white", edgecolor=NAVY, boxstyle="round,pad=0.30", alpha=0.95))


def _find_axis_by_title(fig, title):
    for ax in fig.axes:
        if ax.get_title(loc="left") == title:
            return ax
    return None


def _move_date_labels(from_ax, to_ax, plot_df, max_xticks=12):
    """kline_core 只在最後一列畫日期；表格面板排在最後，要把日期搬回資金流面板。"""
    if from_ax is not None:
        from_ax.set_xticks([])
    if to_ax is None:
        return
    x = list(range(len(plot_df)))
    # 波段可能跨年，日期標籤帶兩位數年份。
    labels = [pd.Timestamp(d).strftime("%y/%m/%d") for d in plot_df.index]
    interval = max(1, len(x) // max(1, max_xticks))
    to_ax.set_xticks(x[::interval])
    to_ax.set_xticklabels(labels[::interval], rotation=30, ha="right", color=MUTED, fontsize=26)
    plt.setp(to_ax.get_xticklabels(), visible=True)


def render_replay_chart(*, stock_code, stock_name, broker_label, kdf, chart_rounds, rounds,
                        habit_text, stock_flow, title, subtitle_prefix, cards, highlight_ids,
                        full_mode):
    """分段與整段共用的出圖流程。chart_rounds 必須依時間排序。"""
    plot_df = slice_window(kdf, chart_rounds[0]["start"], chart_rounds[-1]["end"])
    if plot_df is None:
        log(f"⚠️ {stock_code}｜{title} 找不到對應的日 K，略過")
        return None
    spans = round_positions(plot_df, chart_rounds)

    table_rounds = pick_table_rounds(rounds, chart_rounds[0])
    table_ratio = 1.3 + 0.55 * (len(table_rounds) + 1)
    flow_title = f"{broker_label} 權證買賣｜每日買進（紅）／賣出（綠）與區間累計淨買（萬元）"
    table_title = f"{broker_label} 在 {stock_code} {stock_name} 的歷次波段"
    footnote = (
        f"現股報酬以{chart_rounds[0]['return_basis']}收盤計（進場日→出場日）｜權證報酬為 FIFO 已實現｜"
        "期間最高＝波段內最高價相對進場收盤｜持有中波段算到快取最新交易日"
    )

    fig = plot_onepage_kline(
        plot_df,
        title=title,
        subtitle=(
            f"{subtitle_prefix}｜K線區間：{plot_df.index[0]:%Y/%m/%d} - {plot_df.index[-1]:%Y/%m/%d}"
            "｜資訊分享非投資建議"
        ),
        cards=cards,
        candle_title=CANDLE_TITLE,
        extra_panels=[
            (5.0, flow_title, make_flow_drawer(stock_flow, spans)),
            (table_ratio, table_title,
             make_table_drawer(table_rounds, highlight_ids, habit_text, footnote, table_ratio)),
        ],
        show_volume_profile=REPLAY_SHOW_VOLUME_PROFILE,
    )

    candle_ax = _find_axis_by_title(fig, CANDLE_TITLE)
    if candle_ax is not None:
        span = _expand_candle_ylim(candle_ax)
        sell_label_max = REPLAY_FULL_SELL_LABEL_MAX if full_mode else REPLAY_SELL_LABEL_MAX
        for r, s_idx, e_idx in spans:
            draw_round_marks(candle_ax, plot_df, span, r, s_idx, e_idx, sell_label_max,
                             tag=f"#{r['no']}" if full_mode else "")
        if not full_mode:
            draw_entry_exit_box(candle_ax, chart_rounds[0])
    _move_date_labels(_find_axis_by_title(fig, table_title), _find_axis_by_title(fig, flow_title), plot_df)
    return fig_to_png_buffer(fig)


def plot_segment(stock_code, stock_name, broker_label, kdf, rnd, rounds, habit_text, stock_flow):
    return render_replay_chart(
        stock_code=stock_code, stock_name=stock_name, broker_label=broker_label, kdf=kdf,
        chart_rounds=[rnd], rounds=rounds, habit_text=habit_text, stock_flow=stock_flow,
        title=f"{stock_code} {stock_name}｜{broker_label} 波段複盤 #{rnd['no']}/{len(rounds)}",
        subtitle_prefix=f"波段：{rnd['start']} → {rnd['end']}（{rnd['status']}）",
        cards=build_segment_cards(rnd),
        highlight_ids={id(rnd)},
        full_mode=False,
    )


def plot_full(stock_code, stock_name, broker_label, kdf, chart_rounds, rounds, habit_text, stock_flow):
    first, last = chart_rounds[0], chart_rounds[-1]
    if len(chart_rounds) == len(rounds):
        scope = f"全部 {len(rounds)} 段"
        highlight_ids = set()  # 全部都選時整張表都亮等於沒亮，不標。
    else:
        scope = f"#{first['no']}～#{last['no']}（選 {len(chart_rounds)} 段）"
        highlight_ids = {id(r) for r in chart_rounds}
    return render_replay_chart(
        stock_code=stock_code, stock_name=stock_name, broker_label=broker_label, kdf=kdf,
        chart_rounds=chart_rounds, rounds=rounds, habit_text=habit_text, stock_flow=stock_flow,
        title=f"{stock_code} {stock_name}｜{broker_label} 整段複盤",
        subtitle_prefix=f"{scope}：{first['start']} → {last['end']}",
        cards=build_full_cards(chart_rounds),
        highlight_ids=highlight_ids,
        full_mode=True,
    )


# ============================================================
# 輸出層：PNG、波段總表 CSV、index.html、Step Summary、Discord
# ============================================================

def save_png(buf, out_dir, filename):
    with open(os.path.join(out_dir, filename), "wb") as fh:
        fh.write(buf.getvalue())


def round_to_record(broker_label, broker_code, stock_code, rnd):
    return {
        "分點": broker_label,
        "券商代號": broker_code,
        "股票代號": stock_code,
        "股票名稱": rnd["stock_name"],
        "波段序號": rnd["no"],
        "進場日": rnd["start"],
        "出場日": rnd["end"],
        "狀態": rnd["status"],
        "持有天數": rnd["hold_days"],
        "交易日數": rnd["trading_days"],
        "最大級距": rnd["max_class_text"],
        "大額買進次數": len(rnd["buy_days"]),
        "權證檔數": len(rnd["warrant_codes"]),
        "權證買進金額": round(rnd["total_cost"]),
        "已實現賣出金額": round(rnd["realized_rev"]),
        "已實現成本": round(rnd["realized_cost"]),
        "權證報酬%": _round_or_blank(rnd["warrant_pct"]),
        "同期現股報酬%": _round_or_blank(rnd["stock_pct"]),
        "期間最高%": _round_or_blank(rnd["peak_pct"]),
        "權證現股倍數": _round_or_blank(rnd["leverage"], 1),
        "現股計算基準": rnd["return_basis"],
        "圖檔": rnd["image"],
    }


def _html_pct(value):
    if value == "" or value is None:
        return '<td class="num">-</td>'
    cls = "up" if value > 0 else ("down" if value < 0 else "")
    return f'<td class="num {cls}">{value:+.1f}%</td>'


def write_html_index(path, broker_label, broker_code, records, stock_meta):
    """單一 HTML 檔、無外部資源，直接用瀏覽器打開，或整個資料夾丟到 GitHub Pages 即可。"""
    sections = []
    for stock_code in dict.fromkeys(r["股票代號"] for r in records):
        recs = [r for r in records if r["股票代號"] == stock_code]
        meta = stock_meta.get(stock_code, {})
        rows = []
        for r in recs:
            img = html.escape(r["圖檔"])
            thumb = (f'<a href="{img}" target="_blank"><img src="{img}" loading="lazy" alt=""></a>'
                     if img else "-")
            rows.append(
                "<tr>"
                f"<td>{r['波段序號']}</td><td>{html.escape(r['進場日'])}</td><td>{html.escape(r['出場日'])}</td>"
                f"<td class=\"num\">{r['持有天數']} 天</td><td>{html.escape(r['最大級距'])}</td>"
                f"<td class=\"num\">{fmt_wan(r['權證買進金額'])}</td>"
                f"{_html_pct(r['權證報酬%'])}{_html_pct(r['同期現股報酬%'])}{_html_pct(r['期間最高%'])}"
                f"<td>{html.escape(r['狀態'])}</td><td>{thumb}</td>"
                "</tr>"
            )
        full_link = ""
        for name in meta.get("full_images", []):
            full_link += f'<a class="full" href="{html.escape(name)}" target="_blank">整段 K 線圖</a> '
        sections.append(
            f"<section><h2>{html.escape(stock_code)} {html.escape(recs[0]['股票名稱'])}</h2>"
            f"<p class=\"habit\">{html.escape(meta.get('habit', ''))}</p>{full_link}"
            "<div class=\"scroll\"><table><thead><tr>"
            "<th>#</th><th>進場日</th><th>出場日</th><th>持有</th><th>最大級距</th><th>權證買進</th>"
            "<th>權證報酬</th><th>同期現股</th><th>期間最高</th><th>狀態</th><th>分段圖</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"
        )

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(broker_label)} 權證波段複盤</title>
<style>
body{{margin:0;background:#F5F5F7;color:#101828;font-family:"Noto Sans TC","Microsoft JhengHei","PingFang TC",sans-serif;}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 48px;}}
h1{{color:#1D2B44;font-size:1.7rem;margin:0 0 4px;}}
.sub{{color:#667085;margin:0 0 24px;}}
section{{background:#fff;border:1px solid #CAD3DF;border-radius:12px;padding:16px;margin-bottom:20px;}}
h2{{color:#1D2B44;font-size:1.25rem;margin:0 0 6px;}}
.habit{{color:#1D2B44;font-weight:700;margin:0 0 8px;}}
a.full{{display:inline-block;margin:0 8px 12px 0;padding:4px 12px;border:1px solid #1D2B44;border-radius:6px;color:#1D2B44;text-decoration:none;font-weight:700;}}
.scroll{{overflow-x:auto;}}
table{{border-collapse:collapse;width:100%;font-size:.92rem;white-space:nowrap;}}
th,td{{padding:6px 10px;border-bottom:1px solid #E4E9F0;text-align:left;}}
th{{color:#667085;background:#F6F8FB;}}
.num{{text-align:right;font-variant-numeric:tabular-nums;}}
.up{{color:#E85D5D;font-weight:700;}} .down{{color:#2CB39A;font-weight:700;}}
img{{width:72px;border:1px solid #CAD3DF;border-radius:4px;vertical-align:middle;}}
footer{{color:#667085;font-size:.85rem;}}
</style></head><body><main>
<h1>{html.escape(broker_label)}（{html.escape(broker_code)}）權證波段複盤</h1>
<p class="sub">產出時間 {datetime.now():%Y/%m/%d %H:%M}｜點縮圖看完整 K 線長圖｜資訊分享非投資建議</p>
{''.join(sections)}
<footer>波段＝ABCDE 大額買進日 → FIFO 出清日；權證報酬為已實現；同期現股為進場日收盤到出場日收盤。本次沒選到的波段不出圖，表格仍列出。</footer>
</main></body></html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)


def append_step_summary(broker_label, stock_code, stock_name, rounds, selected_ids):
    """把波段清單寫進 GitHub Actions 的 Summary 頁，下次要選第幾次買賣直接看這裡。"""
    path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    lines = [
        f"### {stock_code} {stock_name}｜{broker_label}",
        "",
        "| # | 進場日 | 出場日 | 狀態 | 持有 | 最大級距 | 權證買進 | 權證報酬 | 同期現股 | 本次出圖 |",
        "|---:|---|---|---|---:|---|---:|---:|---:|:---:|",
    ]
    for r in rounds:
        lines.append(
            f"| {r['no']} | {r['start']} | {r['end']} | {r['status']} | {r['hold_days']} 天 | "
            f"{r['max_class_text']} | {fmt_wan(r['total_cost'])} | {fmt_pct(r['warrant_pct'])} | "
            f"{fmt_pct(r['stock_pct'])} | {'✅' if id(r) in selected_ids else ''} |"
        )
    lines.append("")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        log(f"⚠️ 寫入 GitHub Step Summary 失敗：{exc}")


def send_discord_image(buf, filename, content):
    if not REPLAY_DISCORD_ENABLE:
        return
    if not DISCORD_WEBHOOK_URL:
        log("⚠️ 找不到 REPLAY_DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL_TEST / DISCORD_WEBHOOK_URL，略過 Discord 推播。")
        return
    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            data={"content": content},
            files={"file": (filename, buf.getvalue(), "image/png")},
            timeout=30,
        )
        if response.status_code in (200, 204):
            log(f"✅ Discord 圖片推播完成：{filename}")
        else:
            log(f"❌ Discord 圖片推播失敗: {response.status_code}，改用文字推播")
            requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=30)
    except Exception as exc:
        log(f"❌ Discord 圖片推播失敗：{exc}，改用文字推播")
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=30)
        except Exception as text_exc:
            log(f"❌ Discord 文字推播也失敗：{text_exc}")
    if REPLAY_DISCORD_SLEEP_SECONDS:
        time.sleep(REPLAY_DISCORD_SLEEP_SECONDS)


# ============================================================
# 主流程
# ============================================================

def run_broker_replay():
    started = time.time()
    chart_modes = parse_chart_modes(REPLAY_CHART_MODE)
    mode_label = "＋".join(label for key, label in (("segment", "分段"), ("full", "整段")) if key in chart_modes)
    log("=" * 100)
    log("啟動：分點權證波段複盤 K 線圖")
    log(f"出圖方式：{mode_label}｜波段選擇：{REPLAY_ROUNDS or '全部'}｜最低級距：{REPLAY_MIN_CLASS}")
    log("=" * 100)

    bt = load_backtest_module()
    broker_label, broker_code, broker_name = resolve_broker(bt, REPLAY_BROKER)
    log(f"🚀 分點：{broker_label}（{broker_name}｜{broker_code}）")

    broker_history, latest_key = load_broker_history(bt, broker_code)
    if broker_history.empty:
        raise RuntimeError(
            f"歷史快取裡沒有 {broker_label}（{broker_code}）的成交。"
            "回測 RUN_MODE=1 只追蹤精選分點，要看其他分點需先用 RUN_MODE=2 跑過回測。"
        )

    events, flow_df = build_broker_events(bt, broker_history)
    log(f"✅ 大額買進事件：{len(events):,} 筆（A~E）")
    stock_codes = pick_stock_codes(events)
    if not stock_codes:
        log(f"⚠️ {broker_label} 在快取期間內沒有任何大額買進事件，結束。")
        return

    out_dir = os.path.join(REPLAY_OUTPUT_DIR or os.path.join(bt.OUTPUT_DIR, "broker_replay"), _safe_name(broker_label))
    os.makedirs(out_dir, exist_ok=True)

    records, stock_meta = [], {}
    price_errors, plot_errors = [], []
    image_count = 0

    for stock_code in stock_codes:
        rounds = [summarize_round(r, latest_key, flow_df, stock_code) for r in group_rounds(events, stock_code)]
        rounds = [
            r for r in rounds
            if class_rank(r["max_class"]) >= class_rank(REPLAY_MIN_CLASS)
            and (REPLAY_INCLUDE_OPEN or not r["open"])
        ]
        if not rounds:
            log(f"⚠️ {stock_code}｜{broker_label} 在快取期間內沒有符合條件的波段")
            continue
        for no, r in enumerate(rounds, start=1):
            r["no"] = no

        try:
            kdf, adj_close = fetch_kline_frames(bt, stock_code)
        except Exception as exc:
            price_errors.append((stock_code, str(exc)))
            log(f"❌ {stock_code}｜日 K 抓取失敗：{exc}")
            if len(price_errors) > MAX_ALLOWED_PRICE_ERRORS:
                raise RuntimeError(
                    f"日 K 失敗 {len(price_errors)} 檔，超過上限 {MAX_ALLOWED_PRICE_ERRORS}，中止複盤"
                ) from exc
            continue

        for r in rounds:
            attach_stock_metrics(r, kdf, adj_close)
        habit = summarize_habit(rounds)
        stock_name = next((r["stock_name"] for r in rounds if r["stock_name"]), "")
        stock_flow = stock_daily_flow(flow_df, stock_code)
        selected = [rounds[n - 1] for n in parse_round_selection(REPLAY_ROUNDS, len(rounds))]
        selected_ids = {id(r) for r in selected}
        stock_meta[stock_code] = {"habit": habit, "full_images": []}

        log(f"📊 {stock_code} {stock_name}｜共 {len(rounds)} 段｜本次選 {len(selected)} 段｜{habit}")
        for r in rounds:
            log(f"   {'✅' if id(r) in selected_ids else '  '} {format_round_line(r)}")
        append_step_summary(broker_label, stock_code, stock_name, rounds, selected_ids)
        if not selected:
            log(f"⚠️ {stock_code}｜REPLAY_ROUNDS={REPLAY_ROUNDS} 沒選到任何波段，這檔不出圖")

        if "segment" in chart_modes:
            for r in selected:
                try:
                    buf = plot_segment(stock_code, stock_name, broker_label, kdf, r, rounds, habit, stock_flow)
                except Exception as exc:
                    plot_errors.append((stock_code, f"#{r['no']}", str(exc)))
                    log(f"❌ {stock_code}｜波段 #{r['no']} 繪圖失敗：{type(exc).__name__}: {exc}")
                    continue
                if buf is None:
                    continue
                r["image"] = (
                    f"{stock_code}_{r['no']:02d}_{r['start'].replace('/', '')}_{r['end'].replace('/', '')}.png"
                )
                save_png(buf, out_dir, r["image"])
                image_count += 1
                send_discord_image(
                    buf,
                    r["image"],
                    f"📊 {stock_code} {stock_name}｜{broker_label} 波段 #{r['no']}/{len(rounds)}\n"
                    f"{r['start']} → {r['end']}（{r['status']}）｜"
                    f"權證 {fmt_pct(r['warrant_pct'])}｜同期現股 {fmt_pct(r['stock_pct'])}",
                )

        if "full" in chart_modes and selected:
            try:
                buf = plot_full(stock_code, stock_name, broker_label, kdf, selected, rounds, habit, stock_flow)
            except Exception as exc:
                plot_errors.append((stock_code, "整段", str(exc)))
                log(f"❌ {stock_code}｜整段圖繪圖失敗：{type(exc).__name__}: {exc}")
                buf = None
            if buf is not None:
                first, last = selected[0], selected[-1]
                full_name = (
                    f"{stock_code}_full_{first['no']:02d}-{last['no']:02d}_"
                    f"{first['start'].replace('/', '')}_{last['end'].replace('/', '')}.png"
                )
                save_png(buf, out_dir, full_name)
                stock_meta[stock_code]["full_images"].append(full_name)
                image_count += 1
                send_discord_image(
                    buf,
                    full_name,
                    f"📊 {stock_code} {stock_name}｜{broker_label} 整段複盤 #{first['no']}～#{last['no']}\n"
                    f"{first['start']} → {last['end']}｜{habit}",
                )

        records.extend(round_to_record(broker_label, broker_code, stock_code, r) for r in rounds)

    if records:
        csv_path = os.path.join(out_dir, "rounds_summary.csv")
        pd.DataFrame(records).to_csv(csv_path, index=False, encoding="utf-8-sig")
        log(f"✅ 波段總表：{csv_path}")
        if REPLAY_HTML_INDEX_ENABLE:
            html_path = os.path.join(out_dir, "index.html")
            write_html_index(html_path, broker_label, broker_code, records, stock_meta)
            log(f"✅ 複盤網頁：{html_path}")

    log("=" * 100)
    log(f"完成：{broker_label} 權證波段複盤（{mode_label}）")
    log(f"股票數：{len(stock_meta)}｜波段數：{len(records)}｜圖片：{image_count} 張")
    log(f"日K失敗：{len(price_errors)}｜繪圖失敗：{len(plot_errors)}")
    log(f"輸出資料夾：{out_dir}")
    log(f"耗時：{time.time() - started:.2f} 秒")
    log("=" * 100)
    if plot_errors and image_count == 0:
        raise RuntimeError(f"全部繪圖失敗：{plot_errors[:3]}")


if __name__ == "__main__":
    run_broker_replay()
