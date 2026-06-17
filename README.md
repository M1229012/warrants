# 權證分點籌碼分析系統

本專案主要用於自動化抓取、整理與分析權證分點籌碼資料，並將結果輸出到 Excel、Google Sheet 與 Discord 圖卡。

系統可用於觀察權證分點買賣超、分點共識買賣超、近一個月分點排名、近 10 個交易日明細，以及指定條件下的股票買賣超排行。

---

## 專案功能

- 權證分點買賣超資料抓取
- 權證對應標的股整理
- 權證價格與標的股價格快取
- Google Sheet 快取同步
- Excel 報表輸出
- Discord 圖卡產生與推播
- 精選分點每日買賣超分析
- 全分點近一個月共識買賣超統計
- 單一分點近一個月買賣超 TOP15
- 近 10 個交易日分點買賣明細
- 今日買賣超大於指定金額股票篩選
- GitHub Actions 自動化執行

---

## 專案結構

```text
.
├── .github/workflows
│   └── GitHub Actions 自動化流程
│
├── Spot stock
│   └── stock_branch_backtest.py
│       現股分點相關回測與分析程式
│
├── cloudflare_worker
│   └── register-command.js
│       Discord 指令註冊與 Cloudflare Worker 相關程式
│
├── K_function_warrant_report_20260530.py
│   單一股票權證週報產生程式
│
├── discord_warrant_report_gsheet.py
│   從 Google Sheet 讀取資料並產生 Discord 圖卡
│
├── public_preview_masked.py
│   公開預覽或遮罩版本相關程式
│
├── requirements.txt
│   Python 套件需求
│
├── run_report.py
│   報告產生入口程式
│
└── warrant_backtest.py
    權證分點籌碼主程式
```

---

## 主要程式說明

### `warrant_backtest.py`

權證分點籌碼主程式。

主要負責：

- 抓取權證分點資料
- 整理權證買賣超
- 整理權證對應標的股
- 補抓權證價格
- 補抓標的股價格
- 建立 Excel 報表
- 同步 Google Sheet
- 建立與更新快取資料
- 產生近一個月與近 10 個交易日統計資料

---

### `discord_warrant_report_gsheet.py`

Discord 圖卡產生程式。

主要負責：

- 從 Google Sheet 讀取已整理好的資料
- 產生 Discord 圖片報告
- 推播指定圖卡到 Discord 頻道

常見圖卡包含：

- 精選五分點每日圖
- 近一個月共識淨買超 TOP15
- 本週權證共識買賣超 TOP15
- 近 10 日分點買賣明細圖
- 今日買賣超大於指定金額股票
- 單一分點近一個月買賣超 TOP15

---

### `run_report.py`

報告產生入口程式。

通常用於統一管理不同報告模式，方便搭配 GitHub Actions 或手動執行。

---

### `K_function_warrant_report_20260530.py`

單一股票權證週報程式。

主要用於產生指定股票的權證週報，可能包含技術面、籌碼面、新聞整理與 Discord 推播內容。

---

### `Spot stock/stock_branch_backtest.py`

現股分點相關分析程式。

主要用於現股分點買賣超資料整理、回測或觀察。

---

## 安裝方式

### 1. Clone 專案

```bash
git clone <你的 GitHub Repository URL>
cd <你的專案資料夾>
```

---

### 2. 建立 Python 虛擬環境

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

### 3. 安裝套件

```bash
pip install -r requirements.txt
```

---

## 環境變數設定

本專案會透過環境變數控制 Google Sheet、Discord、快取與執行模式。

可以在本機建立 `.env` 檔案，或是在 GitHub Actions 的 Secrets / Variables 中設定。

範例：

```env
GOOGLE_SHEET_NAME=權證分點籌碼
GOOGLE_SHEET_ID=

WARRANT_GSHEET_ENABLE=1
WARRANT_GSHEET_CACHE_ENABLE=1
WARRANT_CACHE_FORCE_REFRESH=0
WARRANT_ALWAYS_REFRESH_WARRANT_FLOW=0

WARRANT_NEWS_FAST_MODE=1

WARRANT_API4_WORKERS=50
WARRANT_API5_WORKERS=70
WARRANT_API4_SECOND_PASS_WAIT=2

DISCORD_WEBHOOK_URL=
DISCORD_BOT_TOKEN=
DISCORD_APPLICATION_ID=
DISCORD_GUILD_ID=
```

---

## 常用環境變數說明

| 變數名稱 | 說明 |
|---|---|
| `GOOGLE_SHEET_NAME` | Google Sheet 名稱 |
| `GOOGLE_SHEET_ID` | Google Sheet ID，建議使用 ID 會比名稱穩定 |
| `WARRANT_GSHEET_ENABLE` | 是否啟用 Google Sheet 輸出 |
| `WARRANT_GSHEET_CACHE_ENABLE` | 是否啟用 Google Sheet 快取 |
| `WARRANT_CACHE_FORCE_REFRESH` | 是否強制刷新快取 |
| `WARRANT_ALWAYS_REFRESH_WARRANT_FLOW` | 是否每次都重新抓權證分點資料 |
| `WARRANT_NEWS_FAST_MODE` | 是否啟用新聞快速模式 |
| `WARRANT_API4_WORKERS` | API4 多工數量 |
| `WARRANT_API5_WORKERS` | API5 多工數量 |
| `WARRANT_API4_SECOND_PASS_WAIT` | API4 第二輪等待秒數 |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL |
| `DISCORD_BOT_TOKEN` | Discord Bot Token |
| `DISCORD_APPLICATION_ID` | Discord Application ID |
| `DISCORD_GUILD_ID` | Discord 伺服器 ID |

---

## 執行方式

### 執行權證分點主程式

```bash
python warrant_backtest.py
```

此程式會依照目前設定抓取資料、更新快取，並輸出 Excel 或同步 Google Sheet。

---

### 產生 Discord 圖卡

```bash
python discord_warrant_report_gsheet.py
```

此程式會從 Google Sheet 讀取整理後的資料，並產生指定圖卡。

---

### 執行報告入口程式

```bash
python run_report.py
```

此程式通常用於統一管理不同報告產生流程。

---

## GitHub Actions 自動化

本專案可透過 `.github/workflows` 內的 workflow 自動執行。

常見用途包含：

- 每日盤後更新權證分點資料
- 自動同步 Google Sheet
- 自動產生 Discord 圖卡
- 手動指定日期重新產圖
- 手動選擇不同報告模式

GitHub Actions 執行前，請先到 Repository Settings 設定必要的 Secrets。

常見 Secrets：

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
DISCORD_WEBHOOK_URL
DISCORD_BOT_TOKEN
DISCORD_APPLICATION_ID
DISCORD_GUILD_ID
```

實際需要的 Secrets 會依照 workflow 內容而定。

---

## 常見報告模式

系統可依照不同模式產生不同報告。

常見模式包含：

```text
精選五分點每日圖
近一個月共識淨買超TOP15
本週權證共識買賣超TOP15
近10日分點買賣明細圖
全部圖片
```

實際可用選項會依照 GitHub Actions workflow 或程式內設定而定。

---

## 快取說明

本專案會使用快取資料來加速執行，避免每次都重新抓取所有資料。

常見快取包含：

- 權證分點資料快取
- 權證價格快取
- 標的股價格快取
- 股票名稱對照快取
- 近 10 日分點買賣明細快取
- Google Sheet 快取工作表

使用快取可以大幅降低 API 請求量，並縮短每日執行時間。

---

## 強制刷新快取

若資料異常、排名錯誤、快取污染，或需要完整重跑，可以設定：

```env
WARRANT_CACHE_FORCE_REFRESH=1
```

強制刷新會重新整理資料，執行時間會比一般模式更久。

一般每日執行建議使用：

```env
WARRANT_CACHE_FORCE_REFRESH=0
WARRANT_GSHEET_CACHE_ENABLE=1
```

---

## 資料時間邏輯

本系統中的「近一個月」與「近 10 日」應以交易日計算，而不是單純日曆天。

因此週末、國定假日或休市日不應被納入交易日資料。

---

## 分點統計注意事項

分點統計結果可能受到以下條件影響：

- 是否使用全分點模式
- 是否只統計精選分點
- 是否有過濾總公司分點
- 是否有保留指定例外分點
- 是否使用正確交易日區間
- 是否受到舊快取資料影響
- 權證對應標的是否正確
- 價格資料是否完整

若排名結果與預期不同，建議先檢查快取與執行模式。

---

## 輸出資料

系統可能產生以下類型檔案：

```text
output/
├── warrant_report_xxxx.png
├── warrant_branch_report_xxxx.png
├── warrant_backtest_result.xlsx
└── ...
```

實際檔名會依照日期、股票代號與報告模式自動產生。

---

## 常見問題

### Q1：為什麼今天資料沒有更新？

可能原因：

- 快取判斷已有今日資料
- Google Sheet 尚未同步成功
- API 抓取失敗
- 當日非交易日
- GitHub Actions 環境變數未設定完整
- 權限或金鑰設定錯誤

可嘗試強制刷新：

```env
WARRANT_CACHE_FORCE_REFRESH=1
```

---

### Q2：為什麼排名結果跟預期不同？

請檢查：

- 是否使用全分點模式
- 是否只統計精選五分點
- 是否使用近一個月交易日
- 是否有過濾總公司分點
- 是否有舊快取影響
- 權證對應標的是否正確
- 排序欄位是股數、金額、淨買超或買賣超方向

---

### Q3：為什麼 Discord 圖卡沒有資料？

請檢查：

- Google Sheet 是否已有最新資料
- 圖卡程式讀取的工作表名稱是否正確
- 指定日期是否有資料
- Discord Webhook 是否正確
- Discord Bot Token 是否正確
- GitHub Actions 是否成功執行主程式

---

### Q4：為什麼程式執行很久？

可能原因：

- 第一次建立快取
- 強制刷新所有資料
- 全分點模式資料量較大
- 近 10 日明細需要補抓大量價格
- API 請求量過大導致等待或重試

一般情況下，使用快取後每日執行會比第一次快很多。

---

## 開發注意事項

修改程式時請特別注意：

- 不要破壞既有快取格式
- 不要任意更改 Google Sheet 工作表名稱
- 不要任意更動已穩定的欄位順序
- 不要讓五分點模式與全分點模式共用錯誤資料
- 價格快取、分點快取、股票名稱快取需分開檢查
- 修改排名邏輯時，需確認排序依據是股數、金額、淨買超或買賣超方向
- 近 10 日與近一個月資料應以交易日為主
- 若資料異常，請先確認快取是否被舊資料污染

---

## License

本專案為私人研究與自動化分析用途，未經授權請勿任意轉載、販售或公開使用。
