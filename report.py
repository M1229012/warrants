import pandas as pd
import requests
import Triangle5ma
import HighFly
import matplotlib.pyplot as plt
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
from X_function import get_institutional_stats_finmind
import io
import os
import json
import re


# 👉 印出目前系統中所有含 "Noto" 的字型名稱，幫助你 debug
print("✅ 可用的 Noto 字型如下：")
print([f.name for f in fm.fontManager.ttflist if "Noto" in f.name])

# 套用你原本的設定
plt.rcParams['font.family'] = 'Noto Sans CJK JP'
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['axes.spines.top'] = False
plt.rcParams['axes.spines.right'] = False
plt.rcParams['axes.spines.left'] = True
plt.rcParams['axes.spines.bottom'] = True

# === 引入你的原始功能（名稱取得 + 計算指標 + 畫圖） ===
# ==================== 名稱取得 ====================
#def get_tw_stock_name(stock_code: str) -> str:
# === 取得公司名稱（上市/上櫃共用） ===
def get_tw_stock_name(stock_code: str) -> str:
    # 嘗試從上市查詢
    try:
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
        resp = requests.get(url, timeout=5)
        for item in resp.json().get("data", []):
            if item[0].strip() == stock_code:
                return item[1]
    except:
        pass

    # 嘗試從上櫃查詢（TPEX）
    try:
        url = (
            "https://www.tpex.org.tw/web/stock/aftertrading/"
            "daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json"
        )
        resp = requests.get(url, timeout=5)
        tbl = resp.json().get("tables", [])
        if tbl:
            for item in tbl[0].get("data", []):
                if item[0].strip() == stock_code:
                    return item[1]
    except:
        pass

    # 若兩者皆失敗，再用 FinMind 查詢
    try:
        token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNS0wNi0xMSAxMTowODowMyIsInVzZXJfaWQiOiJBY2UiLCJpcCI6IjExNC4xMzYuMTEzLjkxIn0.UZTLVa7l0rA_CPI7hbkc_B5xadPWk75XaRZKxO6E31c"
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockInfo",
            "data_id": stock_code
        }
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        data = resp.json().get("data", [])
        if data:
            return data[0].get("stock_name", "未知公司")
    except:
        pass

    return "未知公司"


# ==================== 資料取得與計算 ====================

def is_twse_stock(code):
    try:
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        return any(row[0] == code for row in data.get("data", []))
    except:
        return False


def fetch_stock_data_yf(stock_code: str, period="120d"):
    import yfinance as yf
    import pandas as pd

    for suffix, market in [("TW", "上市"), ("TWO", "上櫃")]:
        full_code = f"{stock_code}.{suffix}"
        print(f"🔍 嘗試下載 {full_code} 歷史股價資料...")
        try:
            df = yf.download(full_code, period=period, interval="1d", progress=False, auto_adjust=False)
            if df is None or df.empty:
                print(f"⚠️ {full_code} 無資料，嘗試下一個市場")
                continue

            # 解決 MultiIndex 欄位問題
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            print(f"📊 原始欄位: {df.columns.tolist()}")
            expected_cols = {"Open", "High", "Low", "Close", "Volume"}
            if not expected_cols.issubset(df.columns):
                print(f"⚠️ 欄位不足: 缺少 {expected_cols - set(df.columns)}")
                continue

            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.dropna(inplace=True)
            df.index.name = "Date"
            print(f"✅ 成功下載 {market}（{full_code}）資料，共 {len(df)} 筆")
            return df, market, full_code

        except Exception as e:
            print(f"❌ {full_code} 錯誤: {e}")
            continue

    return None, None, None

# ==================== 三大法人資料 ====================
def fetch_inst_60d_from_x(stock_code: str, days: int = 60) -> pd.DataFrame:
    """
    從 X_function.get_institutional_stats_finmind 取回法人資料（單位：張）
    回傳欄位: Date, foreign, invest, dealer, total
    """
    # ✅ X_function 只能吃 (stock_code, n_days)
    # 為了拿到「近60個交易日」，用日曆日抓寬一點較保險（避免假日/空資料）
    inst = get_institutional_stats_finmind(stock_code, n_days=int(days * 2.2))

    if inst is None or inst.empty:
        return pd.DataFrame()

    # inst 欄位：date, 外資, 投信, 自營商（你程式裡就是這樣）
    need_cols = {"date", "外資", "投信", "自營商"}
    if not need_cols.issubset(inst.columns):
        print(f"⚠️ 法人資料欄位不符: {inst.columns.tolist()}")
        return pd.DataFrame()

    out = inst.copy()

    # ✅ Date 轉 datetime，方便跟 yfinance index join
    out["Date"] = pd.to_datetime(out["date"])

    # ✅ 映射成 K 線那套命名
    out["foreign"] = pd.to_numeric(out["外資"], errors="coerce").fillna(0)
    out["invest"]  = pd.to_numeric(out["投信"], errors="coerce").fillna(0)
    out["dealer"]  = pd.to_numeric(out["自營商"], errors="coerce").fillna(0)

    out["total"] = out["foreign"] + out["invest"] + out["dealer"]

    # 你的 X_function 最後是 date 降冪（最新在最上），這裡改成升冪再取尾巴
    out = out.sort_values("Date").tail(days).reset_index(drop=True)
    print(">>> [DEBUG] inst60 head:\n", out.head())
    print(">>> [DEBUG] inst60 columns:", out.columns.tolist())


    return out[["Date", "foreign", "invest", "dealer", "total"]]


# ==================== 權證分點 Google Sheet 資料 ====================
WARRANT_GSHEET_DEFAULT_NAME = "權證分點籌碼"
WARRANT_EVENT_MIN_AMOUNT = float(os.getenv("WARRANT_EVENT_MIN_AMOUNT", "0") or 0)
WARRANT_EVENT_MARK_TOP_N_PER_DAY = int(os.getenv("WARRANT_EVENT_MARK_TOP_N_PER_DAY", "3") or 3)
WARRANT_REPORT_MAX_ROWS = int(os.getenv("WARRANT_REPORT_MAX_ROWS", "8") or 8)


def _normalize_col_name(name) -> str:
    return re.sub(r"[\s　\(\)（）\[\]【】:：_\-\/\\\.]+", "", str(name).strip().lower())


def _pick_col(df: pd.DataFrame, aliases) -> str:
    """用多組可能欄名自動找欄位，讓 Google Sheet 欄名小改也不容易壞。"""
    if df is None or df.empty:
        return None

    normalized_map = {_normalize_col_name(c): c for c in df.columns}
    alias_keys = [_normalize_col_name(a) for a in aliases]

    # 先做完全相等，避免「標的」誤抓到「標的名稱」
    for key in alias_keys:
        if key in normalized_map:
            return normalized_map[key]

    # 再做包含比對，增加相容性
    for c in df.columns:
        nc = _normalize_col_name(c)
        for key in alias_keys:
            if key and (key in nc or nc in key):
                return c
    return None


def _clean_code(v) -> str:
    if pd.isna(v):
        return ""
    s = str(v).strip().replace("'", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s.strip()


def _parse_tw_date(v):
    """支援 2026/05/29、20260529、115/05/29 等日期格式。"""
    if pd.isna(v):
        return pd.NaT

    s = str(v).strip()
    if not s:
        return pd.NaT

    s = s.replace("年", "/").replace("月", "/").replace("日", "")
    s = s.replace(".", "/").replace("-", "/")
    s = re.sub(r"\s+", "", s)

    # 例如 20260529 或 1150529
    if re.fullmatch(r"\d{7,8}", s):
        if len(s) == 7:
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
        else:
            y = int(s[:4])
            m = int(s[4:6])
            d = int(s[6:8])
        return pd.Timestamp(year=y, month=m, day=d)

    m = re.match(r"^(\d{2,4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 1911:
            y += 1911
        return pd.Timestamp(year=y, month=mo, day=da)

    return pd.to_datetime(s, errors="coerce")


def _parse_amount(v) -> float:
    """將 Google Sheet 裡的金額字串轉數字，支援 1,200,000、120萬、0.12億、賣超120萬。"""
    if pd.isna(v):
        return 0.0

    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)

    raw = str(v).strip()
    if not raw:
        return 0.0

    sign = -1 if raw.startswith("(") and raw.endswith(")") else 1
    if ("賣超" in raw or raw.startswith("賣")) and "買賣超" not in raw and "-" not in raw:
        sign = -1

    multiplier = 1.0
    if "億" in raw:
        multiplier = 100000000.0
    elif "萬" in raw:
        multiplier = 10000.0

    cleaned = raw.replace(",", "").replace("+", "")
    cleaned = cleaned.replace("元", "").replace("張", "")
    cleaned = cleaned.replace("萬", "").replace("億", "")
    cleaned = cleaned.replace("(", "-").replace(")", "")
    nums = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
    if not nums:
        return 0.0

    val = float(nums[0]) * multiplier
    if val < 0:
        return val
    return sign * val


def _format_money_zh(v: float) -> str:
    try:
        v = float(v)
    except Exception:
        return "+0"

    abs_v = abs(v)
    sign = "+" if v > 0 else "-" if v < 0 else ""
    if abs_v >= 100000000:
        return f"{sign}{abs_v / 100000000:.2f}億"
    if abs_v >= 10000:
        return f"{sign}{abs_v / 10000:.0f}萬"
    return f"{v:+,.0f}"


def _build_gspread_client():
    """
    建立 Google Sheet client。
    支援：
    1. GCP_SERVICE_KEY：GitHub Secret 內直接放 service account JSON
    2. GOOGLE_APPLICATION_CREDENTIALS / SERVICE_ACCOUNT_FILE：放 JSON 檔案路徑
    """
    try:
        import gspread
    except Exception as e:
        print(f"⚠️ 尚未安裝 gspread，略過權證分點 Google Sheet：{e}")
        return None

    raw_key = os.getenv("GCP_SERVICE_KEY", "").strip()
    if raw_key:
        try:
            if raw_key.startswith("{"):
                info = json.loads(raw_key)
            elif os.path.exists(raw_key):
                with open(raw_key, "r", encoding="utf-8") as f:
                    info = json.load(f)
            else:
                info = json.loads(raw_key.replace("\\n", "\\n"))
            return gspread.service_account_from_dict(info)
        except Exception as e:
            print(f"⚠️ GCP_SERVICE_KEY 解析失敗，略過權證分點 Google Sheet：{e}")
            return None

    key_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("SERVICE_ACCOUNT_FILE")
    if key_file and os.path.exists(key_file):
        try:
            return gspread.service_account(filename=key_file)
        except Exception as e:
            print(f"⚠️ service account 檔案讀取失敗，略過權證分點 Google Sheet：{e}")
            return None

    print("⚠️ 找不到 GCP_SERVICE_KEY / GOOGLE_APPLICATION_CREDENTIALS，略過權證分點 Google Sheet")
    return None


def _normalize_warrant_events(raw_df: pd.DataFrame, source_sheet: str = "") -> pd.DataFrame:
    """把不同欄名的 Google Sheet 資料統一成權證分點事件表。"""
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    date_col = _pick_col(df, ["日期", "交易日期", "資料日期", "date", "Date"])
    branch_col = _pick_col(df, ["分點", "券商分點", "券商", "分公司", "broker", "branch"])
    underlying_col = _pick_col(df, ["標的代號", "標的股票代號", "股票代號", "個股代號", "underlying_code", "stock_code", "data_id"])
    underlying_name_col = _pick_col(df, ["標的名稱", "個股名稱", "股票名稱", "公司名稱", "underlying_name", "stock_name"])
    warrant_code_col = _pick_col(df, ["權證代號", "權證", "商品代號", "warrant_code", "權證證號"])
    warrant_name_col = _pick_col(df, ["權證名稱", "商品名稱", "warrant_name"])
    category_col = _pick_col(df, ["類別", "事件", "訊號", "分類", "type", "category"])
    buy_col = _pick_col(df, ["買超金額", "買進金額", "買方金額", "買超", "買進", "buy_amount", "buy"])
    sell_col = _pick_col(df, ["賣超金額", "賣出金額", "賣方金額", "賣超", "賣出", "sell_amount", "sell"])
    net_col = _pick_col(df, ["買賣超金額", "淨買超", "淨買賣超", "淨額", "買賣超", "net_amount", "net"])

    if date_col is None:
        print(f"⚠️ {source_sheet} 找不到日期欄位，略過")
        return pd.DataFrame()

    if underlying_col is None and warrant_code_col is None and warrant_name_col is None:
        print(f"⚠️ {source_sheet} 找不到標的/權證欄位，略過")
        return pd.DataFrame()

    rows = []
    for _, row in df.iterrows():
        dt = _parse_tw_date(row.get(date_col, ""))
        if pd.isna(dt):
            continue

        buy_amount = _parse_amount(row.get(buy_col, 0)) if buy_col else 0.0
        sell_amount = abs(_parse_amount(row.get(sell_col, 0))) if sell_col else 0.0

        if net_col:
            net_amount = _parse_amount(row.get(net_col, 0))
        elif buy_col or sell_col:
            net_amount = buy_amount - sell_amount
        else:
            net_amount = 0.0

        # 若只有淨額欄，補出買/賣欄方便後面顯示
        if not buy_col and net_amount > 0:
            buy_amount = net_amount
        if not sell_col and net_amount < 0:
            sell_amount = abs(net_amount)

        rows.append({
            "Date": pd.Timestamp(dt).normalize(),
            "branch": str(row.get(branch_col, "") if branch_col else "").strip(),
            "underlying_code": _clean_code(row.get(underlying_col, "")) if underlying_col else "",
            "underlying_name": str(row.get(underlying_name_col, "") if underlying_name_col else "").strip(),
            "warrant_code": _clean_code(row.get(warrant_code_col, "")) if warrant_code_col else "",
            "warrant_name": str(row.get(warrant_name_col, "") if warrant_name_col else "").strip(),
            "category": str(row.get(category_col, "") if category_col else "").strip(),
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "net_amount": net_amount,
            "source_sheet": source_sheet,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    group_cols = ["Date", "branch", "underlying_code", "underlying_name", "warrant_code", "warrant_name", "category", "source_sheet"]
    out = (
        out.groupby(group_cols, dropna=False, as_index=False)
           .agg({"buy_amount": "sum", "sell_amount": "sum", "net_amount": "sum"})
    )
    out = out[out["net_amount"].abs() > 0].copy()
    out["side"] = np.where(out["net_amount"] >= 0, "買超", "賣超")
    return out.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)


def fetch_warrant_branch_events_from_gsheet(stock_code: str,
                                            stock_name: str = "",
                                            start_date=None,
                                            end_date=None,
                                            min_abs_amount: float = None) -> pd.DataFrame:
    """
    從 Google Sheet 讀取權證分點買賣超資料，並篩選指定標的代號。
    若 Google Sheet 尚未設定，會回傳空表，不影響原本 K 線圖產生。
    """
    if str(os.getenv("WARRANT_GSHEET_ENABLE", "1")).strip() == "0":
        return pd.DataFrame()

    stock_code = _clean_code(stock_code)
    stock_name = str(stock_name or "").strip()
    min_abs_amount = WARRANT_EVENT_MIN_AMOUNT if min_abs_amount is None else float(min_abs_amount or 0)

    gc = _build_gspread_client()
    if gc is None:
        return pd.DataFrame()

    try:
        spreadsheet_id = os.getenv("WARRANT_GSHEET_ID") or os.getenv("GSHEET_ID") or os.getenv("SPREADSHEET_ID")
        spreadsheet_name = os.getenv("WARRANT_GSHEET_NAME") or os.getenv("GSHEET_NAME") or WARRANT_GSHEET_DEFAULT_NAME
        sh = gc.open_by_key(spreadsheet_id) if spreadsheet_id else gc.open(spreadsheet_name)

        worksheet_env = os.getenv("WARRANT_GSHEET_WORKSHEET", "").strip()
        if worksheet_env:
            worksheets = [sh.worksheet(name.strip()) for name in worksheet_env.split(",") if name.strip()]
        else:
            worksheets = sh.worksheets()

        all_events = []
        for ws in worksheets:
            try:
                records = ws.get_all_records(empty2zero=False, head=1)
                if not records:
                    continue
                raw_df = pd.DataFrame(records)
                events = _normalize_warrant_events(raw_df, source_sheet=ws.title)
                if not events.empty:
                    all_events.append(events)
            except Exception as e:
                print(f"⚠️ 讀取工作表 {ws.title} 失敗，略過：{e}")

        if not all_events:
            return pd.DataFrame()

        events = pd.concat(all_events, ignore_index=True)

        # 以標的代號為主；若使用者直接查權證代號，也支援 warrant_code；標的名稱也做輔助比對。
        mask = pd.Series(False, index=events.index)
        if stock_code:
            mask = mask | (events["underlying_code"].astype(str) == stock_code)
            mask = mask | (events["warrant_code"].astype(str) == stock_code)
        if stock_name:
            mask = mask | events["underlying_name"].astype(str).str.contains(stock_name, na=False)
            mask = mask | events["warrant_name"].astype(str).str.contains(stock_name, na=False)

        events = events[mask].copy()
        if events.empty:
            return events

        if start_date is not None:
            start_ts = pd.Timestamp(start_date).normalize()
            events = events[events["Date"] >= start_ts]
        if end_date is not None:
            end_ts = pd.Timestamp(end_date).normalize()
            events = events[events["Date"] <= end_ts]
        if min_abs_amount > 0:
            events = events[events["net_amount"].abs() >= min_abs_amount]

        return events.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)

    except Exception as e:
        print(f"⚠️ 權證分點 Google Sheet 讀取失敗，略過：{e}")
        return pd.DataFrame()


def _filter_warrant_events_for_plot(warrant_events: pd.DataFrame, plot_index) -> pd.DataFrame:
    if warrant_events is None or warrant_events.empty:
        return pd.DataFrame()
    start = pd.Timestamp(plot_index.min()).normalize()
    end = pd.Timestamp(plot_index.max()).normalize()
    out = warrant_events.copy()
    out["Date"] = pd.to_datetime(out["Date"]).dt.normalize()
    out = out[(out["Date"] >= start) & (out["Date"] <= end)].copy()
    return out.sort_values(["Date", "net_amount"], ascending=[True, False]).reset_index(drop=True)


def _build_warrant_report_text(warrant_events: pd.DataFrame, max_rows: int = None) -> str:
    max_rows = WARRANT_REPORT_MAX_ROWS if max_rows is None else max_rows
    title = "權證分點買賣超"

    if warrant_events is None or warrant_events.empty:
        return f"{title}\n圖中區間：無資料"

    events = warrant_events.copy()
    events["abs_amount"] = events["net_amount"].abs()
    events = events.sort_values(["Date", "abs_amount"], ascending=[False, False]).head(max_rows)

    lines = [title]
    for _, r in events.iterrows():
        dt = pd.Timestamp(r["Date"]).strftime("%m/%d")
        branch = str(r.get("branch", "")).strip() or "未知分點"
        side = "買" if float(r.get("net_amount", 0)) > 0 else "賣"
        amount = _format_money_zh(float(r.get("net_amount", 0)))
        warrant_code = str(r.get("warrant_code", "")).strip()
        warrant_name = str(r.get("warrant_name", "")).strip()
        warrant_label = warrant_code or warrant_name or "權證"
        if warrant_code and warrant_name:
            warrant_label = f"{warrant_code} {warrant_name[:5]}"
        category = str(r.get("category", "")).strip()
        cat = f"[{category}]" if category else ""
        lines.append(f"{dt} {branch[:6]} {side} {amount} {warrant_label}{cat}")

    if len(warrant_events) > len(events):
        lines.append(f"...共 {len(warrant_events)} 筆")

    return "\n".join(lines)


def _plot_warrant_event_markers(candle_ax, plot_df: pd.DataFrame, x: list, warrant_events: pd.DataFrame,
                                buy_y: float, sell_y: float):
    if warrant_events is None or warrant_events.empty:
        return

    date_to_x = {pd.Timestamp(dt).normalize(): i for i, dt in enumerate(plot_df.index)}
    grouped = warrant_events.groupby("Date")

    for dt, g in grouped:
        dt = pd.Timestamp(dt).normalize()
        if dt not in date_to_x:
            continue

        i = date_to_x[dt]
        net = float(g["net_amount"].sum())
        if abs(net) <= 0:
            continue

        top = g.reindex(g["net_amount"].abs().sort_values(ascending=False).index).head(WARRANT_EVENT_MARK_TOP_N_PER_DAY)
        top_branch = str(top.iloc[0].get("branch", "")).strip() if not top.empty else ""
        count_txt = f"{len(g)}筆" if len(g) > 1 else (top_branch[:4] if top_branch else "")

        if net > 0:
            color = "#D81B60"
            candle_ax.scatter(i, buy_y, marker="D", s=90, color=color, edgecolor="white", linewidth=0.7, zorder=24)
            candle_ax.text(i, buy_y, f"權買\n{count_txt}", ha="center", va="top", fontsize=8,
                           color=color, fontweight="bold", zorder=25)
        else:
            color = "#00897B"
            candle_ax.scatter(i, sell_y, marker="D", s=90, color=color, edgecolor="white", linewidth=0.7, zorder=24)
            candle_ax.text(i, sell_y, f"權賣\n{count_txt}", ha="center", va="bottom", fontsize=8,
                           color=color, fontweight="bold", zorder=25)

# ==================== 價量累積疊圖 ====================

def add_weighted_volume_profile_overlay(ax, df: pd.DataFrame, n_bins: int = 40, side: str = 'left',
                                        color='skyblue', alpha=0.4, scale=5):
    """
    在給定的 K 線圖 ax 上繪製進階 OHLC + Volume 分布的模擬價量累積圖。

    每根 K 線的成交量依以下邏輯進行分配：
    - 60% 分配在實體部分（Open ~ Close）
    - 40% 分配在影線部分（Low ~ High 但排除實體）

    將每個價位區間的累積量視覺化為橫向柱狀圖，與 K 線圖重疊。
    """
    
    volumes = df["Volume"]
    lows = df["Low"]
    highs = df["High"]
    opens = df["Open"]
    closes = df["Close"]

    price_min, price_max = lows.min(), highs.max()
    bins = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    height = bins[1] - bins[0]

    volume_profile = np.zeros(n_bins)

    for i in range(len(df)):
        vol = volumes.iloc[i]
        low = lows.iloc[i]
        high = highs.iloc[i]
        open_ = opens.iloc[i]
        close = closes.iloc[i]

        body_min = min(open_, close)
        body_max = max(open_, close)

        # 將價格分成三段：下影線、實體、上影線
        shadow_bottom = (low, body_min)
        body = (body_min, body_max)
        shadow_top = (body_max, high)

        def assign_volume_to_range(price_range, weight):
            start, end = price_range
            if end - start < 1e-6:
                return
            segment_bins = np.where((bin_centers >= start) & (bin_centers <= end))[0]
            if len(segment_bins) == 0:
                return
            vol_per_bin = (vol * weight) / len(segment_bins)
            for idx in segment_bins:
                volume_profile[idx] += vol_per_bin

        assign_volume_to_range(shadow_bottom, 0.2)
        assign_volume_to_range(body, 0.6)
        assign_volume_to_range(shadow_top, 0.2)

    volume_profile_scaled = volume_profile / volume_profile.max()
    x_min, x_max = ax.get_xlim()
    width_max = (x_max - x_min) / scale
    max_index = np.argmax(volume_profile)

    for i in range(n_bins):
        width = volume_profile_scaled[i] * width_max
        if side == 'left':
            x_start = x_min
        else:
            x_start = x_max - width

        rect_color = "#FF7E39" if i == max_index else color  # 🔥 最大值為橘色

        rect = plt.Rectangle(
            (x_start, bin_centers[i] - height / 2),
            width,
            height,
            color=rect_color,
            alpha=alpha,
            zorder=0,
            transform=ax.transData,
            clip_on=True,
        )
        ax.add_patch(rect)

    ax.set_xlim(x_min, x_max)
# ==================== 超級趨勢SuperTrend指標 ===================
def add_supertrend(df: pd.DataFrame, period=10, multiplier=2.5, use_atr=True) -> pd.DataFrame:
    """
    計算 Supertrend 指標，並加入買賣訊號（根據 TradingView v4 Pine Script 模型）
    """
    df = df.copy()

    hl2 = (df["High"] + df["Low"]) / 2

    # 計算 ATR
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)

    if use_atr:
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
    else:
        atr = tr.rolling(window=period).mean()

    # 計算上下帶
    upper_basic = hl2 - multiplier * atr
    lower_basic = hl2 + multiplier * atr

    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    trend = [1]
    supertrend = [np.nan]
    buy_signal = [False]
    sell_signal = [False]

    for i in range(1, len(df)):
        if df["Close"].iloc[i - 1] > upper_band.iloc[i - 1]:
            upper_band.iloc[i] = max(upper_basic.iloc[i], upper_band.iloc[i - 1])
        else:
            upper_band.iloc[i] = upper_basic.iloc[i]

        if df["Close"].iloc[i - 1] < lower_band.iloc[i - 1]:
            lower_band.iloc[i] = min(lower_basic.iloc[i], lower_band.iloc[i - 1])
        else:
            lower_band.iloc[i] = lower_basic.iloc[i]

        prev_trend = trend[-1]
        if prev_trend == -1 and df["Close"].iloc[i] > lower_band.iloc[i - 1]:
            trend.append(1)
        elif prev_trend == 1 and df["Close"].iloc[i] < upper_band.iloc[i - 1]:
            trend.append(-1)
        else:
            trend.append(prev_trend)

        if trend[-1] == 1 and trend[-2] == -1:
            buy_signal.append(True)
        else:
            buy_signal.append(False)

        if trend[-1] == -1 and trend[-2] == 1:
            sell_signal.append(True)
        else:
            sell_signal.append(False)

        supertrend.append(upper_band.iloc[i] if trend[-1] == 1 else lower_band.iloc[i])

    df["Supertrend"] = supertrend
    df["Supertrend_Trend"] = trend
    df["Supertrend_Buy"] = buy_signal
    df["Supertrend_Sell"] = sell_signal

    return df[["Supertrend", "Supertrend_Trend", "Supertrend_Buy", "Supertrend_Sell"]]

# ==================== 指標計算 ===================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()

    df["MV5"] = df["Volume"].rolling(5).mean()
    df["MV20"] = df["Volume"].rolling(20).mean()

    low_min = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K9"] = rsv.ewm(com=2).mean()
    df["D9"] = df["K9"].ewm(com=2).mean()
    df["J9"] = 3 * df["K9"] - 2 * df["D9"]
    
    # MACD
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["OSC"] = df["DIF"] - df["MACD"]
    
    # === ✅ 平滑版 DMI ===
    up_move = df["High"].diff()
    down_move = df["Low"].diff().abs()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0).ravel(), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0).ravel(), index=df.index)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    smoothed_tr = tr.ewm(alpha=1/14, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1/14, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1/14, adjust=False).mean()

    df["+DI"] = 100 * smoothed_plus_dm / smoothed_tr
    df["-DI"] = 100 * smoothed_minus_dm / smoothed_tr
    dx = 100 * (df["+DI"] - df["-DI"]).abs() / (df["+DI"] + df["-DI"])
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # RSI
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["RSI14"] = 100 - (100 / (1 + rs))

    #布林
    df["BB_MID"] = df["Close"].rolling(20).mean()
    df["BB_STD"] = df["Close"].rolling(20).std()
    df["BB_UPPER"] = df["BB_MID"] + 2 * df["BB_STD"]
    df["BB_LOWER"] = df["BB_MID"] - 2 * df["BB_STD"]
    df["BB_WIDTH"] = df["BB_UPPER"] - df["BB_LOWER"]

    df[["Supertrend", "Supertrend_Trend", "Supertrend_Buy", "Supertrend_Sell"]] = add_supertrend(df, period=10, multiplier=2.5, use_atr=True)

    return df

# ==================== 備註條件 ====================

def get_kd_signals(df):
    remark = []
    k = df["K9"].iloc[-1]
    d = df["D9"].iloc[-1]
    j = df["J9"].iloc[-1]
    k_prev = df["K9"].iloc[-2]
    d_prev = df["D9"].iloc[-2]
    j_prev = df["J9"].iloc[-2]

    if k_prev < d_prev and k > d:
        remark.append("KD黃金交叉")
    if k_prev > d_prev and k < d:
        remark.append("KD死亡交叉")
    if k < 20 and k > k_prev:
        remark.append("K值低檔翻揚")
    if k > 80 and k < k_prev:
        remark.append("K值高檔鈍化")
        # 新增 J 線訊號
    if j_prev < k_prev and j > k:
        remark.append("J上穿K(動能轉強)")
    if j_prev > k_prev and j < k:
        remark.append("J下穿K(動能轉弱)")

    if j >= 100:
        remark.append("J過熱(>100)")
    if j <= 0:
        remark.append("J過冷(<0)")
    return "\n".join(remark)


def get_macd_signals(df):
    remark = []
    dif = df["DIF"]
    macd = df["MACD"]
    osc = df["OSC"]
    close = df["Close"]
    n = 6  # 背離判斷時間區間

    # 原有交叉條件
    if dif.iloc[-2] < macd.iloc[-2] and dif.iloc[-1] > macd.iloc[-1]:
        remark.append("MACD黃叉")
    if dif.iloc[-2] > macd.iloc[-2] and dif.iloc[-1] < macd.iloc[-1]:
        remark.append("MACD死叉")
    if osc.iloc[-2] < 0 and osc.iloc[-1] > 0:
        remark.append("OSC動能翻多")
    if osc.iloc[-2] > 0 and osc.iloc[-1] < 0:
        remark.append("OSC動能翻空")

    # 新增背離條件
    if len(df) >= n + 1:
        # 多頭背離：價格創新低但 DIF 上升
        if close.iloc[-1] < close.iloc[-n] and dif.iloc[-1] > dif.iloc[-n] and osc.iloc[-1] < 0:
            remark.append("✔多頭背離")

        # 空頭背離：價格創新高但 DIF 下降
        if close.iloc[-1] > close.iloc[-n] and dif.iloc[-1] < dif.iloc[-n] and osc.iloc[-1] > 0:
            remark.append("⚠空頭背離")

    return "\n".join(remark)

# ==================== 三大法人備註條件 ====================
def _to_lots(series: pd.Series) -> pd.Series:
    """
    將數值轉成「張」(1張=1000股)。
    - 若資料看起來是「股數」(數值很大)，就 /1000
    - 若本來就是「張」，就原樣
    """
    s = series.astype(float).copy()
    # 這個閾值是務實防呆：通常「股數」會動輒數十萬/數百萬以上
    if s.abs().median(skipna=True) > 50000:
        return s / 1000.0
    return s


def build_chip_priority_signal(df: pd.DataFrame, window: int = 60, cooldown_days: int = 3) -> pd.DataFrame:
    """
    依照「相對(60日分位數) + 持續性 + 量能/價格確認」產生籌碼箭頭訊號（一天最多一種）
    需要欄位：foreign, invest, dealer, Volume
    (可選加分欄位：MV20, Close, Open, MA20)
    回傳新增欄位：
      - chip_signal: str
      - chip_dir: +1 / -1
      - chip_color: str
    """
    df = df.copy()

    need = {"foreign", "invest", "dealer", "Volume"}
    if not need.issubset(df.columns):
        df["chip_signal"] = ""
        df["chip_dir"] = 0
        df["chip_color"] = ""
        return df

    # ===== 單位統一：張 =====
    f = _to_lots(df["foreign"])
    i = _to_lots(df["invest"])
    d = _to_lots(df["dealer"])
    v_lots = df["Volume"].astype(float) / 1000.0  # yfinance Volume => 股數，轉成張
    total = f + i + d

    # ===== 佔量比例（ratio）：跨權值股/小股本一致判斷 =====
    v_safe = v_lots.replace(0, np.nan)
    f_ratio = f / v_safe
    i_ratio = i / v_safe
    t_ratio = total / v_safe

    # 3日合計 ratio（更像趨勢：3日總買賣 / 3日總量）
    v3 = v_lots.rolling(3, min_periods=3).sum().replace(0, np.nan)
    f_3ratio = f.rolling(3, min_periods=3).sum() / v3
    i_3ratio = i.rolling(3, min_periods=3).sum() / v3
    t_3ratio = total.rolling(3, min_periods=3).sum() / v3

    # ===== 量能確認：Volume > MV20（若沒 MV20 就自己算）=====
    if "MV20" in df.columns:
        mv20_lots = df["MV20"].astype(float) / 1000.0
    else:
        mv20_lots = v_lots.rolling(20, min_periods=5).mean()
    vol_ok = v_lots > mv20_lots

    # ===== 價格/結構確認（可用就用，沒有就放寬）=====
    if {"Close", "Open"}.issubset(df.columns):
        bull_candle = df["Close"] > df["Open"]
        bear_candle = df["Close"] < df["Open"]
    else:
        bull_candle = pd.Series(True, index=df.index)
        bear_candle = pd.Series(True, index=df.index)

    if "MA20" in df.columns and "Close" in df.columns:
        above_ma20 = df["Close"] >= df["MA20"]
    else:
        above_ma20 = pd.Series(True, index=df.index)

    # ===== 60日「相對門檻」：用 abs 的 rolling quantile =====
    def rquant_abs(s: pd.Series, q: float = 0.90) -> pd.Series:
        return s.abs().rolling(window, min_periods=max(20, window // 3)).quantile(q)

    # ✅ 外資/投信/三大法人：用 ratio 的歷史分布當基準
    f_abs_q90 = rquant_abs(f_ratio, 0.90)
    i_abs_q90 = rquant_abs(i_ratio, 0.90)
    t_abs_q90 = rquant_abs(t_ratio, 0.90)

    # ✅ 自營商：你說暫時不改，仍用「張數」門檻（較嚴格）
    d_abs_q95 = rquant_abs(d, 0.95)

    # ===== 持續性：用 streak（連續買/賣天數）=====
    def streak_len(mask: pd.Series) -> pd.Series:
        out = np.zeros(len(mask), dtype=int)
        run = 0
        for k, ok in enumerate(mask.fillna(False).to_list()):
            run = run + 1 if ok else 0
            out[k] = run
        return pd.Series(out, index=mask.index)

    f_buy_streak = streak_len(f > 0)
    f_sell_streak = streak_len(f < 0)
    i_buy_streak = streak_len(i > 0)
    i_sell_streak = streak_len(i < 0)

    # ===== 3日合計強度門檻（改用 ratio 的 60日分位數）=====
    f_3sum_q90 = rquant_abs(f_3ratio, 0.90)
    i_3sum_q90 = rquant_abs(i_3ratio, 0.90)
    t_3sum_q90 = rquant_abs(t_3ratio, 0.90)

    # ===== 訊號條件（更嚴格、較不會亂亮）=====
    # 1) 三大法人共振買 / 賣（強度用 total_ratio + 量能 + K棒方向）
    cond_all_buy = (
        (f > 0) & (i > 0) & (d > 0) &
        (t_ratio > t_abs_q90) &
        vol_ok & bull_candle
    )
    cond_all_sell = (
        (f < 0) & (i < 0) & (d < 0) &
        (t_ratio < -t_abs_q90) &
        vol_ok & bear_candle
    )

    # 2) 投信趨勢（連買≥3 + 3日ratio強度>P90 + 結構確認 + 量能）
    cond_invest_trend_buy = (
        (i_buy_streak >= 3) &
        (i_3ratio > i_3sum_q90) &
        above_ma20 &
        vol_ok
    )
    cond_invest_trend_sell = (
        (i_sell_streak >= 3) &
        (i_3ratio < -i_3sum_q90) &
        (~above_ma20) &
        vol_ok
    )

    # 3) 外資趨勢（連買≥3 + 3日ratio強度>P90 + 結構確認 + 量能）
    cond_foreign_trend_buy = (
        (f_buy_streak >= 3) &
        (f_3ratio > f_3sum_q90) &
        above_ma20 &
        vol_ok
    )
    cond_foreign_trend_sell = (
        (f_sell_streak >= 3) &
        (f_3ratio < -f_3sum_q90) &
        (~above_ma20) &
        vol_ok
    )

    # 4) 自營商（噪音最多，改成「極端日」才提示，而且需跟 total 同向）
    #    你目前只有自營商用絕對張數，這段依你的要求不改。
    cond_dealer_extreme_buy = (
        (d > d_abs_q95) &
        (total > 1000) &
        vol_ok
    )
    cond_dealer_extreme_sell = (
        (d < -d_abs_q95) &
        (total < -1000) &
        vol_ok
    )

    # ===== 優先序（一天只取一種）=====
    rules = [
        ("三大法人買",   +1, "red",      cond_all_buy),
        ("三大法人賣",   -1, "red",      cond_all_sell),

        ("投信趨勢買",   +1, "orange",   cond_invest_trend_buy),
        ("投信趨勢賣",   -1, "orange",   cond_invest_trend_sell),

        ("外資趨勢買",   +1, "#1F3A8A",  cond_foreign_trend_buy),
        ("外資趨勢賣",   -1, "#1F3A8A",  cond_foreign_trend_sell),

        ("自營商極端買", +1, "gray",     cond_dealer_extreme_buy),
        ("自營商極端賣", -1, "gray",     cond_dealer_extreme_sell),
    ]

    # ===== Cooldown（同一種訊號觸發後 3 天內不重複）=====
    df["chip_signal"] = ""
    df["chip_dir"] = 0
    df["chip_color"] = ""

    last_fire = {}  # signal_name -> last index position fired

    # 逐日掃描（確保 cooldown 真正生效）
    idx_list = list(df.index)
    for pos, idx in enumerate(idx_list):
        picked = None

        for name, direction, color, mask in rules:
            if not bool(mask.loc[idx]):
                continue

            last_pos = last_fire.get(name, None)
            if last_pos is not None and (pos - last_pos) <= cooldown_days:
                continue

            picked = (name, direction, color)
            break

        if picked is None:
            continue

        name, direction, color = picked
        df.at[idx, "chip_signal"] = name
        df.at[idx, "chip_dir"] = direction
        df.at[idx, "chip_color"] = color
        last_fire[name] = pos

    return df




def get_dmi_signals(df):
    remark = []
    pdi = df["+DI"]
    mdi = df["-DI"]
    adx = df["ADX"]
    if pdi.iloc[-2] < mdi.iloc[-2] and pdi.iloc[-1] > mdi.iloc[-1] and adx.iloc[-1] > 20:
        remark.append("多頭趨勢成形")
    if mdi.iloc[-2] < pdi.iloc[-2] and mdi.iloc[-1] > pdi.iloc[-1] and adx.iloc[-1] > 20:
        remark.append("空頭趨勢成形")
    if adx.iloc[-1] > 25:
        remark.append("趨勢明確")
    elif adx.iloc[-1] < 20:
        remark.append("趨勢盤整")
    return "\n".join(remark)


def get_rsi_signals(df):
    remark = []
    rsi = df["RSI14"]
    if rsi.iloc[-1] > 70:
        remark.append("RSI過熱")
    if rsi.iloc[-1] < 30:
        remark.append("RSI過冷")
    if rsi.iloc[-2] < 50 and rsi.iloc[-1] >= 50:
        remark.append("RSI上穿50")
    if rsi.iloc[-2] > 50 and rsi.iloc[-1] <= 50:
        remark.append("RSI下穿50")
    return "\n".join(remark)


def get_volume_signals(df):
    remark = []
    last = df.iloc[-1]
    mv5 = df["MV5"].iloc[-1]
    mv20 = df["MV20"].iloc[-1]
    close = last["Close"]
    open_ = last["Open"]
    volume = last["Volume"]
    if volume > mv5 and df["MV5"].iloc[-1] > df["MV5"].iloc[-2]:
        remark.append("量能放大")
    if volume < mv5 and df["MV5"].iloc[-1] < df["MV5"].iloc[-2]:
        remark.append("量能縮小")
    if close > open_ and volume > mv20:
        remark.append("放量長紅")
    if close < open_ and volume > mv20:
        remark.append("爆量黑K")
    return "\n".join(remark)

def get_ma_kline_signals(df: pd.DataFrame) -> str:
    """
    分析 K 線與均線訊號，根據 10 種常見條件回傳備註，已加入優先權重排序。
    """
    if len(df) < 2:
        return ""

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    notes = []

    # 條件優先順序處理（先加入最高優先的）
    if latest["MA5"] > latest["MA10"] > latest["MA20"] > latest["MA60"]:
        notes.append("均線多頭排列")
    elif latest["MA5"] < latest["MA10"] < latest["MA20"] < latest["MA60"]:
        notes.append("均線空頭排列")

    # 黃金交叉與死亡交叉
    if prev["MA5"] < prev["MA20"] and latest["MA5"] > latest["MA20"]:
        notes.append("均線黃金交叉")
    elif prev["MA5"] > prev["MA20"] and latest["MA5"] < latest["MA20"]:
        notes.append("均線死亡交叉")

    # 強勢站上 or 全面跌破
    if all(latest["Close"] > latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("強勢站上均線")
    elif all(latest["Close"] < latest[ma] for ma in ["MA5", "MA10", "MA20", "MA60"]):
        notes.append("全面跌破均線")

    # 帶量突破 or 帶量長黑
    if (
        latest["Close"] > latest["MA60"]
        and latest["Close"] > latest["Open"]
        and latest["Volume"] > prev["Volume"]
    ):
        notes.append("帶量突破年線")
    if (
        latest["Close"] < latest["MA20"]
        and latest["Close"] < latest["Open"]
        and latest["Volume"] > prev["Volume"]
    ):
        notes.append("帶量長黑跌破月線")

    # 連三紅 / 黑
    last_3 = df.iloc[-3:]
    if all(row["Close"] > row["Open"] for _, row in last_3.iterrows()) and all(
        df[f"MA{i}"].iloc[-1] > df[f"MA{i}"].iloc[-2] for i in [5, 10, 20]
    ):
        notes.append("連三紅突破均線")
    if all(row["Close"] < row["Open"] for _, row in last_3.iterrows()) and all(
        df[f"MA{i}"].iloc[-1] < df[f"MA{i}"].iloc[-2] for i in [5, 10, 20]
    ):
        notes.append("連三黑轉弱訊號")

    return "．".join(notes) if notes else ""

# ==================== 三大法人圖 ====================
def plot_institutional_stacked_bars(ax, plot_df: pd.DataFrame, x: list):
    """
    三大法人買賣超（正負）疊合柱狀圖
    - 0 上方：正值堆疊（外資 -> 投信 -> 自營商）
    - 0 下方：負值堆疊（外資 -> 投信 -> 自營商）
    顏色固定：
      外資：淺藍、投信：橘、自營商：灰
    """
    if not {"foreign", "invest", "dealer"}.issubset(plot_df.columns):
        ax.text(0.5, 0.5, "No institutional data", transform=ax.transAxes,
                ha="center", va="center", fontsize=14, alpha=0.6)
        return

    # ✅ 固定顏色（你指定的）
    C_FOREIGN = "#7CB5EC"  # 淺藍
    C_INVEST  = "#FFA500"  # 橘
    C_DEALER  = "#9E9E9E"  # 灰

    f = plot_df["foreign"].astype(float).values
    i = plot_df["invest"].astype(float).values
    d = plot_df["dealer"].astype(float).values

    f_pos, i_pos, d_pos = np.clip(f, 0, None), np.clip(i, 0, None), np.clip(d, 0, None)
    f_neg, i_neg, d_neg = np.clip(f, None, 0), np.clip(i, None, 0), np.clip(d, None, 0)

    width = 0.7
    alpha = 0.70

    # === 正值堆疊（0 上方）===
    ax.bar(x, f_pos, width=width, bottom=0,
           color=C_FOREIGN, alpha=alpha, label="外資")
    ax.bar(x, i_pos, width=width, bottom=f_pos,
           color=C_INVEST,  alpha=alpha, label="投信")
    ax.bar(x, d_pos, width=width, bottom=f_pos + i_pos,
           color=C_DEALER,  alpha=alpha, label="自營商")

    # === 負值堆疊（0 下方）===（✅ label=None，避免 legend 重複）
    ax.bar(x, f_neg, width=width, bottom=0,
           color=C_FOREIGN, alpha=alpha, label=None)
    ax.bar(x, i_neg, width=width, bottom=f_neg,
           color=C_INVEST,  alpha=alpha, label=None)
    ax.bar(x, d_neg, width=width, bottom=f_neg + i_neg,
           color=C_DEALER,  alpha=alpha, label=None)

    ax.axhline(0, color="black", linewidth=1, linestyle="--", alpha=0.6)

    # ✅ y軸對稱
    max_abs = np.nanmax(np.abs(np.concatenate([f, i, d]))) if len(f) else 1
    max_abs = 1 if max_abs == 0 else max_abs
    ax.set_ylim(-max_abs * 1.25, max_abs * 1.25)


def format_lots(v: float) -> str:
    # 顯示「張」：千分位，含正負號
    try:
        return f"{v:+,.0f}"
    except:
        return "+0"

from matplotlib.patches import Patch

def draw_inst_header_like_legend(inst_ax, plot_df):
    C_FOREIGN = "#A7D3F5"  # 外資 淺藍
    C_INVEST  = "#F5A623"  # 投信 橘
    C_DEALER  = "#B0B0B0"  # 自營商 灰
    C_TOTAL   = "#333333"  # 合計 深灰

    last = plot_df.iloc[-1]
    f = float(last.get("foreign", 0) or 0)
    i = float(last.get("invest", 0) or 0)
    d = float(last.get("dealer", 0) or 0)
    t = f + i + d

    def fmt(v):  # + / - 格式
        return f"{v:+,.0f}"

    # 左邊標題：不粗體
    y = 1.25
    inst_ax.text(
        0.01, y, "三大法人",
        transform=inst_ax.transAxes,
        ha="left", va="center",
        fontsize=15, color="#111111",
        clip_on=False
    )

    # 用「圖例色塊」呈現顏色，但文字保持預設黑色（像圖例）
    handles = [
        Patch(facecolor=C_FOREIGN, edgecolor=C_FOREIGN, label=f"外資 {fmt(f)} 張"),
        Patch(facecolor=C_INVEST,  edgecolor=C_INVEST,  label=f"投信 {fmt(i)} 張"),
        Patch(facecolor=C_DEALER,  edgecolor=C_DEALER,  label=f"自營商 {fmt(d)} 張"),
        Patch(facecolor=C_TOTAL,   edgecolor=C_TOTAL,   label=f"合計 {fmt(t)} 張"),
    ]

    # 這裡是關鍵：ncol=4 強制同一行；columnspacing/handlelength 控制「不要分太開」
    inst_ax.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(0.085, 1.4),  # 從「三大法人」右邊開始
        ncol=4,
        frameon=False,
        fontsize=15,
        handlelength=1.0,
        handletextpad=0.4,
        columnspacing=1.0,
        borderaxespad=0.0,
        labelspacing=0.2
    )


# ==================== 圖表繪製 ====================


def _is_triangle5ma_signal(window: pd.DataFrame) -> bool:
    if len(window) < 50:
        return False

    hist = window[window['Volume'] != 0].copy()
    if len(hist) < 50:
        return False

    today_close = hist['Close'].iloc[-1]
    avg_close_5d = hist['Close'].iloc[-6:-1].mean()
    avg_close_30d = hist['Close'].iloc[-30:].mean()
    avg_close_31_50d = hist['Close'].iloc[-50:-30].mean()
    today_volume = hist['Volume'].iloc[-1]
    yesterday_volume = hist['Volume'].iloc[-2]

    high_price_30d_date = hist['High'].iloc[-30:].idxmax()
    if high_price_30d_date >= hist.index[-6]:
        return False

    hist_1_31d = hist.iloc[-31:-1]
    low_price = hist_1_31d['Low'].min()
    low_price_date = hist_1_31d['Low'].idxmin()
    low_price_loc = hist.index.get_loc(low_price_date)
    filtered_hist_right = hist.iloc[low_price_loc + 4:] if low_price_loc + 4 < len(hist) else hist.iloc[low_price_loc + 1:]
    if filtered_hist_right.empty:
        return False

    second_low_price = filtered_hist_right['Low'].min()
    second_low_price_date = filtered_hist_right['Low'].idxmin()
    low_date_range = hist.loc[low_price_date:second_low_price_date]
    low_non_zero_volume_count = len(low_date_range[low_date_range['Volume'] != 0]) - 1
    low_slope = (second_low_price - low_price) / low_non_zero_volume_count if low_non_zero_volume_count != 0 else 0

    hist_1_41d = hist.iloc[-41:-1]
    high_price = hist_1_41d['High'].max()
    high_price_date = hist_1_41d['High'].idxmax()
    if high_price_date in hist.iloc[-11:-1].index:
        return False

    high_price_loc = hist.index.get_loc(high_price_date)
    filtered_high_hist = hist.iloc[high_price_loc + 4:] if high_price_loc + 4 < len(hist) else hist.iloc[high_price_loc + 1:]
    if filtered_high_hist.empty:
        return False

    second_high_price = filtered_high_hist['High'].max()
    second_high_price_date = filtered_high_hist['High'].idxmin()
    high_date_range = hist.loc[high_price_date:second_high_price_date]
    high_non_zero_volume_count = len(high_date_range[high_date_range['Volume'] != 0]) - 1
    high_slope = (second_high_price - high_price) / high_non_zero_volume_count if high_non_zero_volume_count != 0 else 0

    three_day_gain = (today_close - hist['Close'].iloc[-4:-1].iloc[0]) / hist['Close'].iloc[-4:-1].iloc[0]
    today_date = hist_1_41d.index[-1]
    high_to_today_non_zero_volume_count = len(hist.loc[high_price_date:today_date][hist['Volume'] != 0])
    expected_price_today = high_price + high_slope * high_to_today_non_zero_volume_count
    if today_close >= expected_price_today:
        return False

    return (
        today_volume > 0.8 * yesterday_volume
        and today_close > avg_close_5d
        and avg_close_30d > avg_close_31_50d * 1.07
        and abs(high_slope) < abs(low_slope)
        and low_slope > 0
        and three_day_gain < 0.08
        and today_volume > 1000000
        and round(high_slope, 5) > -1
    )


def _is_highfly_signal(window: pd.DataFrame) -> bool:
    if len(window) < 30:
        return False
    hist = window.copy()
    hist['5d_ma'] = hist['Close'].rolling(window=5).mean()
    hist['10d_ma'] = hist['Close'].rolling(window=10).mean()
    hist['20d_ma'] = hist['Close'].rolling(window=20).mean()

    close_today = round(hist['Close'].iloc[-1], 2)
    open_today = hist['Open'].iloc[-1]
    high_today = hist['High'].iloc[-1]
    low_today = hist['Low'].iloc[-1]
    volume_today = hist['Volume'].iloc[-1]

    if volume_today <= 1000000:
        return False
    if hist['High'].iloc[-2] != hist['High'][-31:-1].max():
        return False
    if hist['Open'].iloc[-2] <= hist['Close'].iloc[-2] or (hist['High'].iloc[-2] - hist['Low'].iloc[-2]) / hist['Low'].iloc[-2] < 0.02:
        return False
    if close_today < hist['5d_ma'].iloc[-1] or (high_today - low_today) / low_today < 0.02:
        return False
    if hist['10d_ma'].iloc[-1] <= hist['10d_ma'].iloc[-2]:
        return False
    if hist['20d_ma'].iloc[-1] <= hist['20d_ma'].iloc[-2]:
        return False
    if (close_today - open_today) / open_today > 0.03:
        return False
    return True


def _build_strategy_signal_map(df: pd.DataFrame, back_days: int = 30):
    df = df.sort_index().copy()
    signal_map = {}
    start_idx = max(0, len(df) - back_days)
    for i in range(start_idx, len(df)):
        day = df.index[i]
        labels = []
        if _is_triangle5ma_signal(df.iloc[: i + 1]):
            labels.append("三角")
        if _is_highfly_signal(df.iloc[: i + 1]):
            labels.append("高檔")
        if labels:
            signal_map[day.normalize()] = labels
    return signal_map

def plot_stock_chart(df: pd.DataFrame, stock_code: str = "", lookback: int = 60, warrant_events: pd.DataFrame = None):
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
    df["Close_prev"] = df["Close"].shift(1)

    plot_df = df.tail(lookback).copy()
    x = list(range(len(plot_df)))
    warrant_events_in_plot = _filter_warrant_events_for_plot(warrant_events, plot_df.index)

    # 策略標記失敗時不影響原本 !k 流程
    try:
        strategy_signal_map = _build_strategy_signal_map(df, back_days=30)
    except Exception as e:
        print(f"⚠️ 策略標記建立失敗，略過標記: {e}")
        strategy_signal_map = {}
    date_labels = [d.strftime("%m-%d") for d in plot_df.index]
    up = plot_df["Close"] >= plot_df["Open"]
    down = ~up

    fig, axes = plt.subplots(6, 1, figsize=(14, 20),
                             gridspec_kw={"height_ratios": [4, 1, 1, 1, 1, 1]},
                             sharex=True)
    
    # ✅（補回你原本的）整張圖的大色外框
    fig.patch.set_edgecolor("#FFD580")   # 外框顏色
    fig.patch.set_linewidth(10)          # 外框粗細（pt）
    
    # ✅（補回你原本的）標題：黃底圓角框（用 fig.text，不用 suptitle）
    fig.text(
        0.5, 0.973, f"{stock_code}權證技術報告",
        ha="center", va="bottom",
        fontsize=28, fontweight="bold",
        bbox=dict(
            facecolor="#FFE066",
            edgecolor="#FFE066",
            boxstyle="round,pad=0.6",
            alpha=1.0
        )
    )


    def adjust_right_axis(ax):
        ax.yaxis.set_label_position("right")
        ax.yaxis.tick_right()
        ax.yaxis.set_ticks_position("right")
        ax.yaxis.set_tick_params(pad=1)
        ax.spines["right"].set_position(("axes", 0.995))
        ax.spines["right"].set_visible(True)

    # ✅ 先定義 K 線主圖軸
    candle_ax = axes[0]
    candle_width = 0.82  # 加寬 K 棒實體，減少 K 與 K 之間視覺間距

    # ✅ 先畫 K 線（只畫一次）
    for i in x:
        color = '#FF6666' if up.iloc[i] else '#66CC66'
        open_price = plot_df["Open"].iloc[i]
        close_price = plot_df["Close"].iloc[i]
        body_low = min(open_price, close_price)
        body_height = abs(close_price - open_price)
        candle_ax.plot([i, i], [plot_df["Low"].iloc[i], plot_df["High"].iloc[i]], color=color)
        if np.isclose(open_price, close_price, atol=max(1e-3, abs(close_price) * 1e-4)):
            # 平盤十字K：用短橫線畫出實體，避免只剩一束直線
            candle_ax.plot([i - candle_width / 2, i + candle_width / 2], [close_price, close_price], color=color, linewidth=3)
        else:
            candle_ax.bar(i, body_height, width=candle_width, bottom=body_low, color=color, align="center")

    # ✅ 再畫均線 / 布林 / 價量分布
    candle_ax.plot(x, plot_df["MA5"],  label=f"5MA {plot_df['MA5'].iloc[-1]:.2f}",  color="red")
    candle_ax.plot(x, plot_df["MA10"], label=f"10MA {plot_df['MA10'].iloc[-1]:.2f}", color="orange")
    candle_ax.plot(x, plot_df["MA20"], label=f"20MA {plot_df['MA20'].iloc[-1]:.2f}", color="green")
    candle_ax.plot(x, plot_df["MA60"], label=f"60MA {plot_df['MA60'].iloc[-1]:.2f}", color="blue")
    candle_ax.plot(x, plot_df["BB_UPPER"], linestyle="--", color="gray", linewidth=1.2)
    candle_ax.plot(x, plot_df["BB_LOWER"], linestyle="--", color="gray", linewidth=1.2)
    add_weighted_volume_profile_overlay(candle_ax, plot_df, n_bins=40, side='left', color='skyblue', alpha=0.3, scale=1.01)

    # ✅ 補回主圖圖例（去重）
    handles, labels = candle_ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    candle_ax.legend(
        unique.values(), unique.keys(),
        loc="upper left", bbox_to_anchor=(-0.01, 1.15),
        ncol=5, fontsize=15, frameon=False
    )

    # ✅ 扣抵值面板（只放在 K 線主圖右上角）
    def _safe_float(v):
        try:
            if pd.isna(v):
                return np.nan
            return float(v)
        except Exception:
            return np.nan

    def _analyze_pressure(r0, r1, r2, r3):
        if all(pd.notna([r0, r1, r2, r3])):
            if r1 > r0 and r2 > r0 and r3 > r0:
                return "↗ 增", "#E53935"
            if r1 < r0 and r2 < r0 and r3 < r0:
                return "↘ 減", "#43A047"
        return "→ 震", "#607D8B"

    def _get_fail_val(est, today, ref, close_now):
        if any(pd.isna([est, today, ref, close_now])):
            return "--"
        limit_up = close_now * 1.1
        limit_down = close_now * 0.9
        is_impossible = False
        if est > today and limit_down > ref:
            is_impossible = True
        elif est < today and limit_up < ref:
            is_impossible = True
        return "X" if is_impossible else f"{ref:.1f}"

    close_now = _safe_float(plot_df["Close"].iloc[-1]) if len(plot_df) else np.nan
    panel_rows = []
    panel_row_colors = []
    for n, ma_col, period_color in [(5, "MA5", "#2196F3"), (10, "MA10", "#FF9800"), (20, "MA20", "#4CAF50")]:
        if len(plot_df) < n + 3:
            panel_rows.append([f"{n}日", "--", "--", "--", "--"])
            panel_row_colors.append((period_color, "black", "#607D8B", "#D4AC0D", "#607D8B"))
            continue

        ma_today = _safe_float(plot_df[ma_col].iloc[-1])
        r0 = _safe_float(plot_df["Close"].iloc[-n])
        # 對齊 TradingView: r0=close[n-1], r1=close[n-2], r2=close[n-3], r3=close[n-4]
        r1 = _safe_float(plot_df["Close"].iloc[-(n - 1)])
        r2 = _safe_float(plot_df["Close"].iloc[-(n - 2)])
        r3 = _safe_float(plot_df["Close"].iloc[-(n - 3)])

        est = (ma_today * n - r0 + close_now) / n if all(pd.notna([ma_today, r0, close_now])) else np.nan
        trend_up = pd.notna(est) and pd.notna(ma_today) and est > ma_today
        trend_txt = "上" if trend_up else "下"
        trend_color = "#66BB6A" if trend_up else "#EF5350"
        pressure_txt, pressure_color = _analyze_pressure(r0, r1, r2, r3)
        fail_val = _get_fail_val(est, ma_today, r0, close_now)

        panel_rows.append([
            f"{n}日",
            f"{est:.1f}" if pd.notna(est) else "--",
            trend_txt if pd.notna(est) and pd.notna(ma_today) else "--",
            fail_val,
            pressure_txt
        ])
        panel_row_colors.append((period_color, "black", trend_color, "#D4AC0D", pressure_color))

    panel = candle_ax.table(
        cellText=panel_rows,
        colLabels=["週期", "預估值", "預估向", "失敗值", "壓力"],
        cellLoc="center",
        colLoc="center",
        bbox=[0.18, 0.74, 0.16, 0.19]
    )
    panel.auto_set_font_size(False)
    panel.set_fontsize(9)
    panel.set_zorder(30)
    for (r, c), cell in panel.get_celld().items():
        cell.set_zorder(31)
        cell.set_edgecolor("#808080")
        cell.set_linewidth(0.8)
        cell.set_facecolor((0, 0, 0, 0))
        cell.get_text().set_fontweight("bold")
        if r == 0:
            cell.get_text().set_color("black")
        else:
            if 1 <= r <= len(panel_row_colors):
                cell.get_text().set_color(panel_row_colors[r - 1][c])

    # ✅ 籌碼箭頭
    ARROW_SIZE = 110      # 原本 220 → 減半
    ARROW_ALPHA = 0.6     # 半透明
    ARROW_Z = 9  
# ✅ 籌碼箭頭（縮小 + 半透明）
    if {"chip_dir", "chip_color"}.issubset(plot_df.columns):
        for ii in range(len(plot_df)):
            direction = int(plot_df["chip_dir"].iloc[ii]) if not pd.isna(plot_df["chip_dir"].iloc[ii]) else 0
            if direction == 0:
                continue
    
            c = plot_df["chip_color"].iloc[ii]
    
            if direction > 0:
                candle_ax.scatter(
                    x[ii],
                    plot_df["Low"].iloc[ii] * 0.985,
                    marker="^",
                    s=ARROW_SIZE,
                    color=c,
                    alpha=ARROW_ALPHA,
                    zorder=ARROW_Z
                )
            else:
                candle_ax.scatter(
                    x[ii],
                    plot_df["High"].iloc[ii] * 1.015,
                    marker="v",
                    s=ARROW_SIZE,
                    color=c,
                    alpha=ARROW_ALPHA,
                    zorder=ARROW_Z
                )


    # 依 !tra5ma / !highfly 條件標記近 30 天符合訊號
    y_min = plot_df["Low"].min()
    y_max = plot_df["High"].max()
    y_span = max(y_max - y_min, 1e-6)
    marker_y = y_min - y_span * 0.09

    has_warrant_events = warrant_events_in_plot is not None and not warrant_events_in_plot.empty
    has_warrant_buy = has_warrant_events and (warrant_events_in_plot["net_amount"].sum() > 0 or (warrant_events_in_plot["net_amount"] > 0).any())
    has_warrant_sell = has_warrant_events and (warrant_events_in_plot["net_amount"].sum() < 0 or (warrant_events_in_plot["net_amount"] < 0).any())
    warrant_buy_y = y_min - y_span * 0.18
    warrant_sell_y = y_max + y_span * 0.08

    bottom_pad = y_span * (0.24 if has_warrant_buy else 0.11)
    top_pad = y_span * (0.16 if has_warrant_sell else 0.05)
    candle_ax.set_ylim(y_min - bottom_pad, y_max + top_pad)

    # ✅ 權證分點買賣超標記：圖中範圍有事件才標在 K 線主圖
    _plot_warrant_event_markers(candle_ax, plot_df, x, warrant_events_in_plot, warrant_buy_y, warrant_sell_y)

    for i, dt in enumerate(plot_df.index):
        labels = strategy_signal_map.get(pd.Timestamp(dt).normalize(), [])
        if not labels:
            continue

        if "三角" in labels:
            candle_ax.scatter(
                i - 0.12, marker_y, marker="o", s=48,
                color="#2EBD59", edgecolor="white", linewidth=0.6, zorder=21
            )

        if "高檔" in labels:
            candle_ax.scatter(
                i + 0.12, marker_y, marker="o", s=48,
                color="#7EDCFF", edgecolor="white", linewidth=0.6, zorder=21
            )

    adjust_right_axis(candle_ax)

    # ✅ MA備註（不影響箭頭）
    ma_note = get_ma_kline_signals(plot_df)
    if ma_note:
        candle_ax.text(0.6, 0.17, ma_note, transform=candle_ax.transAxes,
                       ha="center", va="top", fontsize=19, fontweight="bold",
                       bbox=dict(facecolor="#F3FF46", edgecolor="#F3FF46",
                                 boxstyle="round,pad=0.4", alpha=0.9),
                       color="#D31F1F")

    # === 籌碼箭頭圖例定義（集中管理） ===
    chip_legend_items = [
        ("^", "red",   "三大法人"),
        ("^", "orange","投信"),
        ("^", "#1F3A8A", "外資"),
        ("^", "gray",  "自營商"),
        ("o", "#2EBD59", "三角收斂"),
        ("o", "#7EDCFF", "高檔飛舞"),
        ("D", "#D81B60", "權證買超"),
        ("D", "#00897B", "權證賣超"),
    ]
    # === 籌碼箭頭圖例（左下角，懸浮） ===
    legend_x = 0.02     # 左右位置（axes fraction）
    legend_y = 0.05     # 底部起始位置
    line_gap = 0.045    # 每一行的間距
    
    for i, (marker, color, label) in enumerate(chip_legend_items):
        y = legend_y + i * line_gap
    
        # 箭頭
        candle_ax.scatter(
            legend_x, y,
            transform=candle_ax.transAxes,
            marker=marker,
            s=80,
            color=color,
            zorder=20
        )
    
        # 文字
        candle_ax.text(
            legend_x + 0.03, y,
            label,
            transform=candle_ax.transAxes,
            ha="left", va="center",
            fontsize=11,
            color="#333333",
            zorder=20
        )




    # ✅ 加入右上角最近一日資訊區塊
    latest = plot_df.iloc[-1]
    prev_close = latest["Close_prev"] if not pd.isna(latest["Close_prev"]) else latest["Close"]
    diff = latest["Close"] - prev_close
    pct = (diff / prev_close * 100) if prev_close != 0 else 0
    bw_pct = latest["BB_WIDTH"] / latest["Close"] * 100  # 計算 BB 寬佔股價比
    info = (
        f"{plot_df.index[-1].strftime('%Y/%m/%d')}\n"
        f"開 {latest['Open']:.2f}\n"
        f"高 {latest['High']:.2f}\n"
        f"低 {latest['Low']:.2f}\n"
        f"收 {latest['Close']:.2f}\n"
        f"漲跌 {diff:+.2f}\n"
        f"幅度 {pct:+.2f}%\n"
        f"量 {latest['Volume'] / 1000:,.0f} 張\n"
        f"布林寬比 {bw_pct:.2f}%"
    )
    candle_ax.text(0.03, 0.92, info, transform=candle_ax.transAxes,
                   fontsize=15, verticalalignment='top', horizontalalignment='left',
                   linespacing=1.4,
                   bbox=dict(facecolor='white', alpha=0.8, boxstyle='round,pad=0.5'))

    # ✅ 權證分點買賣超報告區塊
    warrant_report_text = _build_warrant_report_text(warrant_events_in_plot)
    candle_ax.text(0.985, 0.92, warrant_report_text, transform=candle_ax.transAxes,
                   fontsize=10.5, verticalalignment='top', horizontalalignment='right',
                   linespacing=1.35, color="#222222",
                   bbox=dict(facecolor='white', edgecolor="#D81B60", alpha=0.88, boxstyle='round,pad=0.45'))

    

    # 成交量
    plot_df = df.tail(lookback).copy()
    plot_df["Volume"] = plot_df["Volume"] / 1000
    plot_df["MV5"] = plot_df["MV5"] / 1000
    plot_df["MV20"] = plot_df["MV20"] / 1000
    vol_ax = axes[1]
    vol_ax.bar([i for i in x if up.iloc[i]], plot_df["Volume"][up], color="#F18B8B", width=0.7)
    vol_ax.bar([i for i in x if down.iloc[i]], plot_df["Volume"][down], color="#9AEC9A", width=0.7)
    vol_ax.plot(x, label=f"成交量 {plot_df['Volume'].iloc[-1]:,.0f} 張", color="none")
    vol_ax.plot(x, plot_df["MV5"], label=f"MA5 {plot_df['MV5'].iloc[-1]:,.0f} 張", color="blue")
    vol_ax.plot(x, plot_df["MV20"], label=f"MA20 {plot_df['MV20'].iloc[-1]:,.0f} 張", color="magenta")
    vol_ax.legend(loc="upper left", bbox_to_anchor=(-0.05, 1.5), ncol=3, fontsize=16, frameon=False)
    adjust_right_axis(vol_ax)
    vol_note = get_volume_signals(plot_df)
    if vol_note:
        vol_ax.text(
            0.93, 1.30, vol_note, transform=vol_ax.transAxes,
            ha="right", va="top",
            fontsize=18, fontweight="bold",
            bbox=dict(facecolor="#F3FF46", edgecolor="#F3FF46", boxstyle="round,pad=0.4", alpha=0.9),
            color="#D31F1F"
        )


    # KD
    kd_ax = axes[2]
    kd_ax.plot(x, label=f"KDJ", color="none")
    kd_ax.plot(x, plot_df["K9"], label=f"K9 {plot_df['K9'].iloc[-1]:.2f}", color="blue")
    kd_ax.plot(x, plot_df["D9"], label=f"D9 {plot_df['D9'].iloc[-1]:.2f}", color="orange")
    kd_ax.plot(x, plot_df["J9"], label=f"J9 {plot_df['J9'].iloc[-1]:.2f}", color="green")
    #kd_ax.plot(x, label=f"K值>80為紅點     K值<20為綠點", color="none")

    for i in x:
        k_val = plot_df["K9"].iloc[i]
        if k_val > 80:
            kd_ax.scatter(i, k_val, color='red', s=30)
        elif k_val < 20:
            kd_ax.scatter(i, k_val, color='mediumseagreen', s=30)
    kd_ax.legend(loc="upper left", bbox_to_anchor=(-0.05, 1.50, 0.55, 0.12), mode="expand", ncol=4, fontsize=16, frameon=False)
    adjust_right_axis(kd_ax)

    # KD 備註
    kd_note = get_kd_signals(plot_df)
    if kd_note:
        kd_note = get_kd_signals(plot_df)
    if kd_note:
        kd_ax.text(
            0.93, 1.30, kd_note, transform=kd_ax.transAxes,
            ha="right", va="top",
            fontsize=18, fontweight="bold",
            bbox=dict(facecolor="#F3FF46", edgecolor="#F3FF46", boxstyle="round,pad=0.4", alpha=0.9),
            color="#D31F1F"
        )

    kd_ax.text(
        0.99, 1.50,
        "K值>80為紅點   K值<20為綠點",
        transform=kd_ax.transAxes,
        ha="right", va="center",
        fontsize=10,
        color="#333333"
    )

    # MACD
    macd_ax = axes[3]
    dif = plot_df["DIF"]
    macd = plot_df["MACD"]
    osc = plot_df["OSC"]
    macd_ax.bar(x, osc, color=["#F6682BF8" if v >= 0 else "#2AFC8F" for v in osc], width=0.7, alpha=0.5, label=f"OSC {osc.iloc[-1]:.2f}")
    macd_ax.plot(x, label=f"MACD", color="none")
    macd_ax.plot(x, dif, label=f"DIF {dif.iloc[-1]:.2f}", color="blue")
    macd_ax.plot(x, macd, label=f"MACD {macd.iloc[-1]:.2f}", color="purple")
# 取得正負最大值，設為對稱的 y 軸上下限
    max_val = max(abs(osc.min()), abs(osc.max()))
    macd_ax.set_ylim(-max_val * 1.1, max_val * 1.1)  # 留點緩衝
    macd_ax.axhline(0, color="black", linewidth=1, linestyle="--", alpha=0.6)
    macd_ax.legend(loc="upper left", bbox_to_anchor=(-0.05, 1.5), ncol=4, fontsize=16, frameon=False)
    adjust_right_axis(macd_ax)
    macd_note = get_macd_signals(plot_df)
    if macd_note:
        macd_ax.text(
            0.93, 1.30, macd_note, transform=macd_ax.transAxes,
            ha="right", va="top",
            fontsize=18, fontweight="bold",
            bbox=dict(facecolor="#F3FF46", edgecolor="#F3FF46", boxstyle="round,pad=0.4", alpha=0.9),
            color="#D31F1F"
        )

    

    # 三大法人（取代 DMI）
    
    inst_ax = axes[4]
    
    plot_institutional_stacked_bars(inst_ax, plot_df, x)
    
    # ✅ 不用 legend，改成自己畫一行
    draw_inst_header_like_legend(inst_ax, plot_df)
    
    adjust_right_axis(inst_ax)


    # RSI
    rsi_ax = axes[5]
    rsi_ax.plot(x, label=f"RSI", color="none")
    rsi_ax.plot(x, plot_df["RSI14"], label=f"RSI14 {plot_df['RSI14'].iloc[-1]:.2f}", color="purple")
    rsi_ax.axhline(70, color="red", linestyle="--")
    rsi_ax.axhline(30, color="green", linestyle="--")
    rsi_ax.legend(loc="upper left", bbox_to_anchor=(-0.05, 1.5), ncol=2, fontsize=16, frameon=False)
    adjust_right_axis(rsi_ax)
    rsi_note = get_rsi_signals(plot_df)
    if rsi_note:
        rsi_ax.text(
            0.93, 1.30, rsi_note, transform=rsi_ax.transAxes,
            ha="right", va="top",
            fontsize=18, fontweight="bold",
            bbox=dict(facecolor="#F3FF46", edgecolor="#F3FF46", boxstyle="round,pad=0.4", alpha=0.9),
            color="#D31F1F"
        )


    # 日期
    # 控制日期標籤間距
    interval = max(1, len(x) // 15)  # 最多顯示 15 個標籤
    plt.xticks(ticks=x[::interval], labels=[date_labels[i] for i in range(0, len(date_labels), interval)], rotation=30, fontsize=8)

    # ✅ 加入註記文字（右上角）
    fig.text(0.98, 0.99, "By 股市艾斯出品-轉傳請註明\n資訊僅供教育參考 非買賣建議用途", ha="right", va="top",fontsize=14, alpha=0.6, fontweight="bold", color="#413F3F")
    # Layout spacing 調整
    # ✅ 整張圖外框（不受 axes spines 影響）

    plt.subplots_adjust(left=0.03, right=0.96, top=0.91, bottom=0.06, hspace=0.45)
    return fig


# ✅ 以下為原封不動搬過來的核心函數：
# ✅ 我們只補一個主函數 generate_k_chart(stock_code) 供 !k 呼叫



def generate_k_chart(stock_code: str) -> io.BytesIO:
    try:
        stock_name = get_tw_stock_name(stock_code)
        df, market, yf_code = fetch_stock_data_yf(stock_code)
        if df is None or df.empty:
            return None

        df = calculate_indicators(df)

        # === 只做一次：抓法人 → 對齊 index → join ===
        inst60 = fetch_inst_60d_from_x(stock_code, days=70)

        if inst60 is None or inst60.empty:
            print("⚠️ inst60 為空，略過籌碼箭頭 join")
            # 沒有法人也沒關係，後面 build_chip_priority_signal 會走防呆
            df[["foreign", "invest", "dealer", "total"]] = 0
        else:
            # 確保 Date 欄位存在且為 datetime
            if "Date" not in inst60.columns:
                print(f"⚠️ inst60 欄位異常：{inst60.columns.tolist()}，略過 join")
                df[["foreign", "invest", "dealer", "total"]] = 0
            else:
                inst60 = inst60.copy()
                inst60["Date"] = pd.to_datetime(inst60["Date"])
                inst60 = inst60.set_index("Date").sort_index()

                # yfinance index 也是 datetime，確保一致（去掉時區）
                df = df.copy()
                df.index = pd.to_datetime(df.index).tz_localize(None)

                keep_cols = ["foreign", "invest", "dealer", "total"]
                for c in keep_cols:
                    if c not in inst60.columns:
                        inst60[c] = 0

                df = df.join(inst60[keep_cols], how="left")
                df[keep_cols] = df[keep_cols].fillna(0)

        # === 產生籌碼箭頭 ===
        df = build_chip_priority_signal(df)

        # === 權證分點資料：只抓 K 線顯示範圍附近的 Google Sheet 資料 ===
        lookback = 70
        plot_start = pd.Timestamp(df.tail(lookback).index.min()).normalize()
        plot_end = pd.Timestamp(df.tail(lookback).index.max()).normalize()
        warrant_events = fetch_warrant_branch_events_from_gsheet(
            stock_code=stock_code,
            stock_name=stock_name,
            start_date=plot_start,
            end_date=plot_end,
            min_abs_amount=WARRANT_EVENT_MIN_AMOUNT
        )

        chart_title = f"{stock_name} ({stock_code})"
        fig = plot_stock_chart(df, stock_code=chart_title, lookback=lookback, warrant_events=warrant_events)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=300, bbox_inches="tight", pad_inches=0.25)
        plt.close(fig)
        buf.seek(0)
        return buf

    except Exception as e:
        print(f"❌ 產生 K 線圖錯誤: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_warrant_report(stock_code: str) -> io.BytesIO:
    """權證報告入口；保留 generate_k_chart 供原本指令相容。"""
    return generate_k_chart(stock_code)

