# 📊 權證分點籌碼分析系統

本專案用於自動化抓取、整理與分析權證分點籌碼資料，並輸出 Google Sheet、Excel 與 Discord 圖卡報告。

系統主要用來觀察權證分點買賣超、分點共識、近一個月排名、近 10 個交易日明細，以及指定條件下的股票買賣超排行。

---

## ✨ 主要功能

- 📈 權證分點買賣超資料整理
- 🏦 權證對應標的股統計
- 🗓️ 近一個月分點買賣超排名
- 🔍 近 10 個交易日分點買賣明細
- 💰 今日買賣超大於指定金額股票篩選
- ☁️ Google Sheet 快取同步
- 🖼️ Discord 圖卡產生與推播
- ⚙️ GitHub Actions 自動化執行

---

## 📁 專案結構

```text
.
├── .github/workflows
│   └── GitHub Actions 自動化流程
│
├── Spot stock
│   └── stock_branch_backtest.py
│
├── cloudflare_worker
│   └── register-command.js
│
├── K_function_warrant_report_20260530.py
├── discord_warrant_report_gsheet.py
├── public_preview_masked.py
├── requirements.txt
├── run_report.py
└── warrant_backtest.py
```

---

## 🧩 主要檔案說明

### 🧠 `warrant_backtest.py`

權證分點籌碼主程式。

負責抓取權證分點資料、整理買賣超統計、更新快取、輸出 Excel，並同步 Google Sheet。

---

### 🖼️ `discord_warrant_report_gsheet.py`

Discord 圖卡產生程式。

負責從 Google Sheet 讀取資料，產生 Discord 圖卡並推播。

---

### 🚀 `run_report.py`

報告產生入口程式。

用於執行不同圖卡模式，方便搭配 GitHub Actions 或手動執行。

---

### 📌 `K_function_warrant_report_20260530.py`

單一股票權證週報產生程式。

主要用於產生指定股票的權證週報。

---

### 📊 `Spot stock/stock_branch_backtest.py`

現股分點相關分析程式。

主要用於現股分點買賣超資料整理、回測或觀察。

---

## 🛠️ 安裝方式

### 1️⃣ Clone 專案

```bash
git clone <你的 GitHub Repository URL>
cd <你的專案資料夾>
```

---

### 2️⃣ 建立 Python 虛擬環境

建議使用 Python 3.10 以上版本。

```bash
python -m venv venv
```

Windows：

```bash
venv\Scripts\activate
```

macOS / Linux：

```bash
source venv/bin/activate
```

---

### 3️⃣ 安裝套件

```bash
pip install -r requirements.txt
```

---

## 🔐 環境變數

本專案需要設定 Google Sheet、Discord 與快取相關環境變數。

可以在本機建立 `.env` 檔案，或是在 GitHub Actions 的 Secrets / Variables 中設定。

```env
GOOGLE_SHEET_NAME=權證分點籌碼
GOOGLE_SHEET_ID=

WARRANT_GSHEET_ENABLE=1
WARRANT_GSHEET_CACHE_ENABLE=1
WARRANT_CACHE_FORCE_REFRESH=0
WARRANT_ALWAYS_REFRESH_WARRANT_FLOW=0

DISCORD_WEBHOOK_URL=
DISCORD_BOT_TOKEN=
DISCORD_APPLICATION_ID=
DISCORD_GUILD_ID=
```

---

## ▶️ 執行方式

### 執行權證分點主程式

```bash
python warrant_backtest.py
```

---

### 產生 Discord 圖卡

```bash
python discord_warrant_report_gsheet.py
```

---

### 執行報告入口

```bash
python run_report.py
```

---

## 🤖 GitHub Actions

本專案可透過 `.github/workflows` 內的 workflow 自動執行。

常見用途：

- 🕒 每日盤後更新資料
- ☁️ 自動同步 Google Sheet
- 🖼️ 自動產生 Discord 圖卡
- 📅 手動指定日期重新產圖
- 🔁 手動選擇不同報告模式

請記得在 GitHub Repository Settings 中設定 Secrets。

常見 Secrets：

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
DISCORD_WEBHOOK_URL
DISCORD_BOT_TOKEN
DISCORD_APPLICATION_ID
DISCORD_GUILD_ID
```

---

## 🧠 快取說明

系統會使用快取資料來加速執行，避免每次都重新抓取所有資料。

常見快取包含：

- 📌 權證分點資料快取
- 💵 權證價格快取
- 📈 標的股價格快取
- 🏷️ 股票名稱對照快取
- 🗓️ 近 10 日分點買賣明細快取
- ☁️ Google Sheet 快取工作表

若資料異常或快取污染，可以設定：

```env
WARRANT_CACHE_FORCE_REFRESH=1
```

重新整理資料。

---

## ⚠️ 注意事項

請勿將以下資料直接上傳到 GitHub：

- `.env`
- Google Service Account JSON
- Discord Bot Token
- Discord Webhook URL
- 任何 API 金鑰或帳號密碼

修改程式時請注意：

- 不要任意更改 Google Sheet 工作表名稱
- 不要任意更動既有欄位順序
- 不要破壞快取格式
- 五分點模式與全分點模式資料需分開確認
- 近一個月與近 10 日資料應以交易日為主

---

## 📌 License

本專案為私人研究與自動化分析用途，未經授權請勿任意轉載、販售或公開使用。
