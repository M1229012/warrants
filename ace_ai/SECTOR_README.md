# 族群查詢與比較

## 用法

- `/ask 有哪些族群`
- `/ask 記憶體族群現在誰形態最好`
- `/ask 散熱族群盤中誰最強`
- `/ask PCB族群有哪些`
- `/ask 記憶體IC族群名冊`
- `/ask 半導體族群哪檔型態比較好`
- `/ask 航運股今天誰漲最多`
- `/ask 金融股有哪些`

「比較好」預設依既有技術型態分數排名；「盤中／漲幅／誰最強」依最新漲跌幅排名；明確問「形態／型態」時仍採技術評分。每次回覆前三名及實際比較範圍，AI 最多呼叫一次作解讀。上市與上櫃普通股都包含，不含興櫃、創櫃、ETF 或權證。

## 細分名冊（本次更新）

新增 30 個細分類對照，股票代號與名稱由證交所／櫃買中心「產業價值鏈資訊平台」公開網頁取得，不使用 AI 猜名單、不逐檔手寫成分股，也不需要付費 FinMind API。

- 記憶體、記憶體 IC、記憶體控制 IC、DRAM 製造。
- 散熱、PCB 製造、銅箔基板、PCB 設備、玻纖布、銅箔。
- 機器人、工業型機器人、AGV／AMR、服務型及人型機器人、感測器。
- IC 設計、IP 設計／IC 設計代工、IC／晶圓製造、封裝測試、半導體設備、導線架。
- 伺服器、工業電腦、電源供應器、機殼、主機板、顯示卡、光學鏡片／鏡頭、網路設備、光通訊設備。

同時解析本國／外國上市與上櫃公司，合併後依代號去重，並核對網頁宣告的公司數。名冊查詢分列兩個市場，排名也顯示上市／上櫃數量。記憶體合併記憶體 IC、控制 IC、DRAM 製造及電腦產業的記憶體名冊；本次公開來源實測為上市 10 檔、上櫃 12 檔，後續隨來源更新。

分類範圍以來源原始定義為準。例如 PCB 製造含硬板、軟板及 IC 載板；DRAM 為製造分類；光通訊為設備分類，不能直接當作 CPO。AI 伺服器、HBM、ABF、CPO 等尚無精確對照的題材不會套用較大的分類。來源只列興櫃／創櫃而沒有上市櫃股票時，會顯示 0 檔，不把興櫃混入，也不代表市場上沒有相關題材公司。

每個產業鏈網頁共享快取，預設查詢時若資料超過一天即更新。網路失敗時使用最近成功的快取或隨程式附上的 `fine_sector_seed.json` 公開名冊快照；清楚標記備援與原取得日期，五分鐘後可重試。這是取得日期，並非官方分類修訂日期。部分分類無資料則標記不完整，不能宣稱涵蓋所有市場題材股。

## 名冊來源（2026-09-20 起）

族群成分股改成**自建快照** `sector_roster.json`：離線掃一次全市場，用 CMoney 個股頁反查每檔屬於哪些產業／概念族群，一次得到所有族群的完整成員（含概念股、集團股）。

- 平常查詢只讀這個檔案，不連 CMoney，也沒有「首屏只有 8 檔」的問題。
- 重建：`python ace_ai/sector_roster.py build`（約 10～20 分鐘），或在 Discord 由管理員說「更新族群名冊」。
- 名冊沒建立時，才退回舊的 CMoney 即時查詢流程。

## 資料流程

1. 名冊優先重用 `core()._finmind_load_stock_info()` 的 `TaiwanStockInfo`；相同代號先取日期最新列，再篩選上市櫃市場與產業。
2. 主要名冊失敗、缺欄位或找不到分類時，使用既有 `FUGLE_API_KEY`，分別查 `intraday/tickers` 的 `TSE`、`OTC` 市場。備援只取得一個市場時，明確標示名冊不完整。
   上述 FinMind 優先、富果備援保留於大產業分類；細族群另由公開產業鏈補足，兩者不互相冒充。
3. 行情、技術面、大量區及型態分數沿用現有工具，不修改其來源或計算規則。這裡的「FinMind 優先、富果備援」是族群名冊；現有盤中行情仍依原本設定取得。
4. 技術排名只比較同一日期的已收盤評分，最新報價另外標示。盤中漲幅排行排除昨收備援和超過 15 分鐘的報價。
5. 不設定任意的前 N 檔候選清單；嘗試掃描整個族群，但有時間上限。抓取失敗、日期不符、超時未完成的數量均會列出；部分排行不宣稱為全族群前三名。
6. 排名及數字由 Python 固定輸出，AI 僅補充解讀並使用既有 FactSheet 逐檔核對；AI 失敗時保留程式排名。

## 可選環境變數

無須新增金鑰或套件，沿用既有設定。以下不設定也能使用：

| 變數 | 預設 | 說明 |
|---|---:|---|
| `DISCORD_AI_SECTOR_MEMBERS_TTL` | 86400 | 主要名冊快取秒數；備援名冊最多快取 300 秒後重試主要來源 |
| `DISCORD_AI_SECTOR_RESULT_TTL` | 300 | 個股比較資料及完整族群結果快取秒數 |
| `DISCORD_AI_SECTOR_TIMEOUT` | 90 | 一輪族群股票掃描時間上限，名冊取得和 AI 解讀時間另計 |
| `DISCORD_AI_SECTOR_REQUEST_GAP` | 1.5 | 未命中族群個股快取時的最小啟動間隔秒數，不可低於 1.5 |
| `DISCORD_AI_SECTOR_LIQUIDITY_DAYS` | 20 | 流動性門檻的計算天數（已收盤交易日） |
| `DISCORD_AI_SECTOR_MIN_AVG_VALUE` | 50000000 | 排行門檻：平均成交金額（元）；未達者不列入排行 |
| `DISCORD_AI_SECTOR_MIN_AVG_LOTS` | 500 | 排行門檻：平均成交量（張）；與成交金額兩個條件都要達到 |
| `DISCORD_AI_FINE_MEMBERS_TTL` | 86400 | 細分類網頁名冊更新間隔秒數 |
| `DISCORD_AI_FINE_MEMBERS_TIMEOUT` | 12 | 每次公開名冊 HTTP 請求逾時秒數 |
| `DISCORD_AI_FINE_CATALOG_CACHE` | `.cache/fine_sector_catalog.json` | 細分名冊快取檔；預設相對於程式資料夾，可改為可寫入的絕對路徑 |

族群掃描使用獨立的兩個工作執行緒，同時間只掃描一個族群，避免占滿個股工具工作池。成功的個股資料會共用快取；部分排行最多快取 30 秒，重查時可繼續利用已取得資料補齊。節流僅涵蓋此新增流程，不取代供應商對整個帳號的限制。

族群查詢不沿用上一題的個股／成本；回答後清除該使用者舊個股記憶，避免下一題誤接回上一檔。一般個股與分點查詢維持原本流程。

## 檔案與測試

上一版已在 `discord_ai_bot.py` 加入族群入口。本次僅更新 `sector_analysis.py`、族群測試及本說明，新增 `fine_sector_catalog.py`、`fine_sector_seed.json`、`test_fine_sector_catalog.py`，其餘既有功能不修改。將這些檔案放在既有 `ace_ai` 同一資料夾後，重新啟動 Discord Bot。若正式 Bot 在另一台主機，須同步這些檔案並重啟該主機的 Bot。

```sh
cd ace_ai
python -m unittest test_fine_sector_catalog test_sector_analysis test_bollinger test_waiting_reply -v
```

新增測試使用模擬資料驗證 FinMind 優先、富果雙市場備援、重複／轉板處理、名冊快取、日期與時效檢查、排序、超時、AI 失敗備援及路由隔離，不會讀取金鑰或呼叫外部服務。

細分類測試另驗證雙市場解析、外國上市公司、排除興櫃、網頁重複區塊、細分類子表、來源數量不符、快取跨程序讀取、網站失敗備援、分類合併去重，以及「記憶體族群現在誰形態最好」完整路由。這四組合計 50 項測試通過；公開來源 5 個頁面的 30 個分類另已實際連線驗證。行情與 AI 端到端測試使用模擬資料，並非實際盤中排名結果。

提供的檔案未包含週報核心 `K_function_warrant_report_20260530.py`，因此無法在這份獨立資料夾完成真實行情與 Gemini 整合測試。既有圖片測試另缺少 `demo_images.py`，本次未修改或補造該測試依賴。

官方規格：
- https://finmind.github.io/tutor/TaiwanMarket/Technical/#taiwanstockinfo
- https://developer.fugle.tw/docs/data/http-api/intraday/tickers/
- https://ic.tpex.org.tw/introduce.php?ic=D000
- https://ic.tpex.org.tw/introduce.php?ic=F000
- https://ic.tpex.org.tw/introduce.php?ic=L000
- https://ic.tpex.org.tw/introduce.php?ic=6000
- https://ic.tpex.org.tw/introduce.php?ic=I000
