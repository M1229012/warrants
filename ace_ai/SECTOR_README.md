# 族群查詢與比較

## 用法

- `/ask 有哪些族群`
- `/ask 半導體族群哪檔型態比較好`
- `/ask 航運股今天誰漲最多`
- `/ask 金融股有哪些`

「比較好」預設依既有技術型態分數排名；「盤中／漲幅／誰最強」依最新漲跌幅排名。每次回覆前三名及實際比較範圍，AI 最多呼叫一次作解讀。產業分類為上市櫃普通股，不含興櫃、ETF 或權證。記憶體、散熱、PCB 等細分概念股不在免費名冊分類中，會提示不支援，不以整個半導體／電子零組件業替代。

## 資料流程

1. 名冊優先重用 `core()._finmind_load_stock_info()` 的 `TaiwanStockInfo`；相同代號先取日期最新列，再篩選上市櫃市場與產業。
2. 主要名冊失敗、缺欄位或找不到分類時，使用既有 `FUGLE_API_KEY`，分別查 `intraday/tickers` 的 `TSE`、`OTC` 市場。備援只取得一個市場時，明確標示名冊不完整。
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

族群掃描使用獨立的兩個工作執行緒，同時間只掃描一個族群，避免占滿個股工具工作池。成功的個股資料會共用快取；部分排行最多快取 30 秒，重查時可繼續利用已取得資料補齊。節流僅涵蓋此新增流程，不取代供應商對整個帳號的限制。

族群查詢不沿用上一題的個股／成本；回答後清除該使用者舊個股記憶，避免下一題誤接回上一檔。一般個股與分點查詢維持原本流程。

## 檔案與測試

既有檔案只在 `discord_ai_bot.py` 加入族群入口、路由、記憶隔離及回答橋接。新增 `sector_analysis.py`、`test_sector_analysis.py` 和本說明。

```sh
cd ace_ai
python -m unittest test_sector_analysis test_bollinger test_waiting_reply -v
```

新增測試使用模擬資料驗證 FinMind 優先、富果雙市場備援、重複／轉板處理、名冊快取、日期與時效檢查、排序、超時、AI 失敗備援及路由隔離，不會讀取金鑰或呼叫外部服務。

提供的檔案未包含週報核心 `K_function_warrant_report_20260530.py`，因此無法在這份獨立資料夾完成真實行情與 Gemini 整合測試。既有圖片測試另缺少 `demo_images.py`，本次未修改或補造該測試依賴。

官方規格：
- https://finmind.github.io/tutor/TaiwanMarket/Technical/#taiwanstockinfo
- https://developer.fugle.tw/docs/data/http-api/intraday/tickers/
