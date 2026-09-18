# 艾斯 AI｜Discord 台股問答助理

在 Discord 輸入 `/ask 問題`，Bot 抓取股價、技術面、大量區、權證分點、A～E 事件績效、新聞與族群名冊，整理成**一張圖片**回覆。

- **數字全部由 Python 計算**：重用 repo 根目錄 `K_function_warrant_report_20260530.py` 的函式，以及回測寫進 Google Sheet 的結果。Gemini 只負責文字解讀，而且每一句都會經過事實核對。
- **Bot 對 Google Sheet 只讀不寫**；不影響週報、回測或任何 GitHub Actions。
- **Bot 不會下單**；回答只描述技術結構與條件，不給買賣指令、目標價或上漲機率。

---

## 使用方式

指令只有 `/ask`。`!ace` 文字指令預設關閉（`DISCORD_AI_PREFIX_COMMAND_ENABLE=1` 才開）。

| 想問的事 | 範例 |
|---|---|
| 型態好不好（K 線＋型態評分卡） | `/ask 2409現在型態好嗎` |
| 持股成本怎麼看 | `/ask 我2409成本30元該怎麼看` |
| 兩檔比較 | `/ask 2344跟2408誰比較好` |
| 只問股價（不呼叫 AI） | `/ask 2330股價多少` |
| 新聞利多利空 | `/ask 2344最近有什麼新聞` |
| 權證分點與高勝率分點 | `/ask 2409權證分點買賣` |
| 權證淨買超排行 | `/ask 目前權證買超金額最大的是誰` |
| 分點 A～E 事件勝率 | `/ask 永豐金內湖D事件勝率` |
| 分點近期操作 | `/ask 永豐金內湖最近在買什麼` |
| 分點在某檔的部位 | `/ask 永豐金內湖2409部位還在嗎` |
| 分點勝率排行 | `/ask 勝率最高的分點有哪些` |
| 族群型態排行 | `/ask 記憶體族群現在誰形態最好` |
| 族群盤中漲幅 | `/ask 散熱族群盤中誰最強` |
| 族群成分股 | `/ask PCB族群有哪些` |
| 支援哪些族群 | `/ask 有哪些族群` |

**追問**：同一個人在同一個頻道 30 分鐘內可以接著問，不用重打股票。

```text
/ask 幫我分析華邦電
/ask 那它的壓力在哪        ← 自動接華邦電
/ask 跟南亞科比呢          ← 變成華邦電 vs 南亞科
/ask 重新開始              ← 清除上一題
```

回答最上面會用小字標出「※ 延續上一題：華邦電（2344）」，讓使用者知道 Bot 是怎麼理解這題的。

**本週精選**（`/ask 本週精選`、`/ask 本週精選 只看D事件 勝率70%以上`；也可以打「本周精選」「本州精選」「每週精選」「每周精選」「精選股票」「精選個股」）只開放給**伺服器管理員**（有「管理員」或「管理伺服器」權限的人）以及 `DISCORD_AI_WEEKLY_PICK_USER_IDS` 裡的使用者，使用說明不會顯示這項功能。候選只看 1～9 開頭的 4 碼普通股，**ETF（0 開頭，例如 0050、00878、00631L）一律排除**。

---

## 回答圖片

| 問題類型 | 圖片內容 |
|---|---|
| 單一個股 | K 線卡（MA5／10／20／60、布林上下軌、價量分布、成交量與均量線、分點買賣標註）＋型態評分卡（分數、五大項、主要得分／失分、均線扣抵、關鍵價位、追蹤分點動向）＋AI 回答 |
| 兩檔比較 | 兩張精簡 K 線（不畫分點標註）＋一張「型態比較」並排表（分數、五大項、型態、均線、最近壓力／支撐、追蹤分點、盤中觀察）＋AI 回答。長度約為單檔版的兩倍，而不是四倍 |
| 權證淨買超排行 | 排行＋第一名的 K 線與評分卡 |
| 族群排行 | 前三名卡片（名次、分數與分級或漲幅、股價、分數條、優勢／留意、AI 解讀）＋第 4～5 名排行表（最多顯示到第 5 名）；不顯示名冊來源與檔數統計，只在有個股缺資料時加一行小字說明 |
| 族群成分股 | 依上市／上櫃分組的股名標籤 |
| 本週精選 | TOP5 總覽＋每檔卡片，每張最多 3 檔，同一則訊息送多張 |

- **MA20 就是布林中軌**：數值卡寫成「MA20／中軌」，K 線上不另外畫中軌虛線，關鍵價位表也只列一次。
- **分點買賣標註**：▲＋紅圈 N＝A～E 買進日、▼＋綠圈 N＝同編號出清日、▼ 無數字＝減碼日；只標總勝率 60% 以上的追蹤分點與精選五分點，隔日沖（2 個交易日內出清）不標。不寫報酬率。
- **圖上不出現資料供應商名稱**（FinMind、富果、Google Sheet、工作表名稱），只寫「日K收盤資料」「盤中即時報價」「追蹤分點統計」；新聞媒體名稱照寫。
- **日期寫法**：扣抵推算寫「明天起／後天起／N 個交易日後」，不寫「第 N 日」。
- PNG 超過附件容量時改 JPEG；股價抓不到時圖內標示缺資料，保留其他分析。

---

## 型態評分（100 分）

只評技術結構，不含籌碼，不是買賣建議。與本週精選共用同一套規則（`weekly_pick.score_pattern`）。

| 項目 | 權重 | 看什麼 |
|---|---:|---|
| 均線趨勢 | 25 | 均線排列、MA20／MA60 方向與扣抵 |
| 價格位置 | 15 | 相對 MA20、近期漲幅是否過熱 |
| 量區結構 | 25 | 相對兩大量區的位置（漸進計分，接近量區下緣也給部分分數） |
| 下方支撐 | 25 | MA10／MA20／大量區等支撐的距離與品質 |
| 布林 | 10 | 開布林方向與位置（中軌方向已算在 MA20，不重複） |

分級：≥75 結構偏強、≥60 中性偏多、≥45 結構中性、≥30 中性偏弱、其餘結構偏弱。

本週精選綜合分數＝型態 50＋事件績效 22＋權證金額 16＋近期操作 12；預設排除 2330（`WEEKLY_PICK_EXCLUDE_CODES`）。

---

## 盤中與收盤

設定 `FUGLE_API_KEY` 後，平日盤中會在日 K 後面接上一根盤中 K 棒；沒設或抓不到就只用日 K，K 線不會留白。

訊號分三種狀態，AI 不可混用：

| 狀態 | 內容 |
|---|---|
| 收盤確認 | 型態評分、均線／布林訊號、大量區，一律用**最後一根已收盤 K 棒**計算 |
| 盤中暫時 | 盤中和收盤不同的地方另外列出，例如「盤中暫時站上 MA20（前一日收盤為跌破），尚待收盤確認」；評分卡用黃色小框標示，不計入分數 |
| 資料不足 | 欄位缺值時寫「目前無法確認」，不當成沒有訊號 |

- 盤中那根的 MV5／MV20 沿用前一日，**盤中累計量不和日均量比較**，也不判斷量縮量增。
- 盤中成交量單位固定換算（`FUGLE_QUOTE_VOLUME_UNIT`，預設 `lots`＝張）。和近 20 日均量相比超過 30 倍或低於 0.001 倍時標成可疑，不解讀量能。
- 啟動時 Log 會印 `📏 富果成交量單位校正：…`（收盤後比對盤中累計量與日 K 量），依結果確認單位設定。

---

## AI 回答與事實核對

- **Prompt 依問題組合**：基本規則＋技術面／新聞／型態／排行規則，只放這題用得到的段落；送出前刪掉說明欄位與圖上已有的數值，節省 token。
- **一定先寫【回答】**直接回應問題；型態題最多再加 3 行【觀察重點】。比較題第一句要下結論（「就技術結構來看，X 比 Y 好」，分數差不到 5 分寫「差不多」）。
- **事實核對（`FactSheet`，純 Python，不多花 Gemini）**：逐句檢查下列問題，有問題只刪那一句。
  1. 數字對不上原始資料（大金額可精確換算成萬／億）。
  2. 句子只講一檔股票，卻用了另一檔的數字。
  3. 「月線 31.2 元」這類均線數值寫錯（容許 0.6%，扣抵價也算對）。
  4. 「站上／跌破」和收盤確認的位置不符；句中有「盤中／暫時／目前」才可用盤中位置。
  5. 把使用者問題裡的價格（成本、假設跌到多少）當成現價。

  條件句、否定句、均線彼此交叉、成本與均線比較不核對方向，避免誤刪。刪到剩不到六成才整篇改用系統排版的資料。Log 會寫「事實核對：刪除 N 句｜句子（原因）」。
- **新聞**：鉅亨網個股新聞 API（含內文），AI 分【可能利多】【可能利空／風險】整理，聳動字眼與法人預估不當成事實。

---

## 多人使用

| 機制 | 預設 |
|---|---|
| 一般問答同時處理 | 3 題（`DISCORD_AI_ANSWER_CONCURRENCY`） |
| 排隊上限 | 20 題（`DISCORD_AI_QUEUE_LIMIT`），滿了回「排隊已滿，請過一兩分鐘再問」 |
| 排隊通知 | 需要排隊時先把回覆圖更新成「前面還有 N 個問題」，輪到時自動換成答案 |
| 相同問題 | 同時有人問同一題只算一次，大家共用結果 |
| 本週精選 | 獨立排隊，一次一個，不卡一般問答 |
| Gemini 同時呼叫 | 2 個（`DISCORD_AI_GEMINI_CONCURRENCY`） |
| 只問股價 | 走輕量流程：不呼叫 AI、K 線不查分點標註 |
| 同一人冷卻 | 8 秒；上一題還沒完成不能再問 |

**追問記憶**只存「股票代號、成本、分點、時間」，不存對話原文；鍵值是伺服器＋頻道＋使用者 ID。30 分鐘沒追問自動忘記，最多 5,000 筆（最舊的先刪），Bot 重啟即清空。排行、分點勝率、族群、打招呼這類問題不會接上一題的股票。

---

## 族群

大產業分類用 FinMind `TaiwanStockInfo`（富果 `TSE`／`OTC` 名冊備援）；30 個細分族群用證交所／櫃買中心公開產業價值鏈網頁，合併上市、上櫃並依代號去重，網站失敗時用 `fine_sector_seed.json` 快照。

- 「形態／型態／比較好」依型態分數排名；「盤中／漲幅／誰最強」依最新漲跌幅排名。圖上顯示前三名卡片與第 4～5 名；有個股缺資料時只加一行小字說明，不宣稱是全族群前三名。
- 技術排行只比較同一個收盤日期的評分；盤中漲幅排行排除非當日或超過 15 分鐘的報價。
- **排除成交清淡的個股**：近 20 個交易日平均成交金額 5,000 萬元以上、且平均成交量 500 張以上才列入排行（兩個條件都要達到），避免把沒什麼人交易的冷門股排到前面；門檻寫在圖卡副標題。可用 `DISCORD_AI_SECTOR_MIN_AVG_VALUE`、`DISCORD_AI_SECTOR_MIN_AVG_LOTS`、`DISCORD_AI_SECTOR_LIQUIDITY_DAYS` 調整。
- 排名、分數、價格與時間由 Python 輸出，AI 最多呼叫一次補充每檔解讀，且逐檔經過事實核對。
- HBM、CPO、AI 伺服器等沒有精確公開分類的題材，會提示未支援，不套用較大的分類。

分類清單、快取與可選變數詳見 [族群功能說明](SECTOR_README.md)。

---

## 技術細節

### 價量分布

直接呼叫週報 `_calculate_weighted_volume_profile_stats(..., n_bins=40)`（影線各 20%、實體 60%），保留完整 40 格：最大量區紅色、第二大量區橘色、其他淺藍色，透明度與週報一致。缺成交量時顯示「價量分布暫無有效資料」，不推測色帶。

### 布林判讀（`bollinger_analysis.py`）

軌道使用週報 `calculate_indicators` 的 BB_UPPER／BB_MID／BB_LOWER（20 日 ± 2 倍標準差）。以下是本專案採用的門檻，不代表所有工具都用同一套標準。

| 觀察 | 規則 |
|---|---|
| 向上／向下突破 | 前一日收盤在軌內、本日收盤跨越上／下軌；連續在軌外標為持續軌外 |
| 影線穿越 | 高／低價穿越軌道但收盤回到通道內，不當成突破 |
| 重返通道 | 前一日收盤在軌外，本日回到上下軌之間 |
| 壓縮 | 本日帶寬 ≤ 前 60 個有效帶寬的第 20 百分位（不足 60 日為未知） |
| 收窄／擴張 | 帶寬相對 5 個交易日前減少／增加至少 10% |
| 橫盤 | 近 10 日收盤都在軌內、中軌變化 ≤1%、收盤高低差 ≤6%，且帶寬沒有擴張 |
| 沿上軌／沿下軌 | 連續 3 日 %b ≥80% 且中軌上升／%b ≤20% 且中軌下降 |
| 壓縮後突破 | 前 5 個交易日曾壓縮，本日收盤首次跨越上／下軌 |

帶寬＝（上軌－下軌）／中軌 ×100；%b＝（收盤－下軌）／（上軌－下軌）×100。觸軌本身不是買賣訊號，AI 也不可由壓縮自行預測方向。參考：[Bollinger Band Rules](https://www.bollingerbands.com/bollinger-band-rules)。

### 等待圖更新

本週精選、排隊等候時，先送出「計算中／排隊中」圖片，完成後**在同一則訊息替換附件**，不保留舊圖；例外時換成錯誤圖片。等待訊息被手動刪除時才重新發送，私人回覆不會轉成公開訊息。Discord interaction token 有效 15 分鐘，超時或 Bot 重啟時無法保證更新。

---

## 檔案

| 檔案 | 用途 |
|---|---|
| `discord_ai_bot.py` | 入口：`/ask`、權限、問題解析、路由、追問記憶、排隊、Gemini、事實核對、圖片回覆、啟動自我檢查 |
| `warrant_ai_tools.py` | 資料工具層：股價（日 K＋盤中）、技術面、大量區、均線扣抵、權證分點、A～E 事件績效、分點部位、權證淨買超排行、新聞、Sheet 唯讀查詢 |
| `weekly_pick.py` | 型態評分（100 分）、型態評分卡、本週精選篩選與排序 |
| `answer_image.py` | 一般回答圖片：K 線卡、型態評分卡、兩檔比較表、文字區塊 |
| `weekly_image.py` | 本週精選卡片圖片 |
| `bollinger_analysis.py` | 布林軌道狀態判讀 |
| `sector_analysis.py` | 族群路由、名冊、技術／漲幅排行、AI 解讀與覆蓋率說明 |
| `fine_sector_catalog.py`、`fine_sector_seed.json` | 30 個細分族群的公開名冊解析、快取與備援快照 |
| `SECTOR_README.md` | 族群功能詳細說明 |
| `eval/questions.json`、`eval/run_eval.py` | 固定評測題（45 題、56 輪）與執行程式 |
| `test_*.py` | 離線單元測試 |
| `requirements.txt` | 根目錄 requirements.txt ＋ discord.py、Pillow |
| `Dockerfile`、`railway.toml` | Railway 部署設定（Dockerfile 安裝 Noto CJK 中文字型） |
| `env.example` | 環境變數清單（沒有真實 Secret） |

---

## 部署（Railway）

這是 GitHub repo 裡的 `ace_ai/` 資料夾，不是獨立 repo；需要 repo 根目錄的週報主程式與 `requirements.txt`。

- **Root Directory**：留空（repo 根目錄）
- **Config File Path**：`/ace_ai/railway.toml`
- **Start Command**：`python -u ace_ai/discord_ai_bot.py`
- 週報主程式在 repo 內須命名為 `K_function_warrant_report_20260530.py`，否則設定 `WARRANT_CORE_SCRIPT`。
- Discord 請用**另一個 Application**，不要設定 Interactions Endpoint URL（Cloudflare Worker 的 `/w` 用的是另一個）。Bot 需要 View Channel、Send Messages、Read Message History、Attach Files 權限。

部署成功時 Log 會出現：`✅ GCP_SERVICE_KEY 格式正確`、`✅ 自我檢查：Google Sheet 可讀取`、`📏 富果成交量單位校正`（有設 `FUGLE_API_KEY` 才有）、`🔥 預熱完成`、`✅ 艾斯 AI 已上線`。預熱在背景執行，順序可能不同。

### 環境變數

完整清單與預設值見 `env.example`。

| 必填 | 說明 |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot Token |
| `DISCORD_AI_ALLOWED_USER_IDS` | `*`＝不限使用者（只限伺服器內、不接受私訊） |
| `DISCORD_AI_GUILD_IDS` | 允許的伺服器 ID，逗號分隔；填了 `/ask` 會立即同步 |
| `WARRANTS_API_KEY`（`_2`、`_3`）、`GEMINI_MODEL` | Gemini |
| `FINMIND_API_TOKEN` | 日 K、股票名冊 |
| `GCP_SERVICE_KEY`、`GOOGLE_SHEET_ID`、`GSHEET_NAME` | 回測 Google Sheet（服務帳號 JSON 必須壓成單行） |

| 建議設定 | 說明 |
|---|---|
| `FUGLE_API_KEY` | 盤中即時報價；沒設就只用日 K |
| `DISCORD_AI_WEEKLY_PICK_USER_IDS` | 額外可使用本週精選的 User ID（伺服器管理員不用列）；逗號分隔 |
| `DISCORD_AI_WEEKLY_PICK_ALLOW_ADMINS` | 預設 1＝伺服器管理員都能用本週精選；0＝只限上面名單 |
| `FUGLE_QUOTE_VOLUME_UNIT` | `lots`（預設，張）或 `shares`（股），依 📏 校正 Log 決定 |

| 可選（預設值） | 說明 |
|---|---|
| `DISCORD_AI_ANSWER_CONCURRENCY`（3）、`DISCORD_AI_QUEUE_LIMIT`（20） | 同時處理題數、排隊上限 |
| `DISCORD_AI_GEMINI_CONCURRENCY`（2）、`DISCORD_AI_TOOL_WORKERS`（10） | Gemini 同時呼叫數、資料工具執行緒 |
| `DISCORD_AI_MEMORY_MINUTES`（30）、`DISCORD_AI_MEMORY_MAX_ENTRIES`（5000） | 追問記憶保留時間與筆數 |
| `DISCORD_AI_VOLUME_SUSPECT_HIGH`（30）、`DISCORD_AI_VOLUME_SUSPECT_LOW`（0.001） | 盤中量異常門檻（倍） |
| `DISCORD_AI_INTRADAY_ENABLE`（1）、`DISCORD_AI_TTL_INTRADAY_SECONDS`（60） | 盤中報價開關與快取秒數 |
| `DISCORD_AI_PREFIX_COMMAND_ENABLE`（0） | 1＝開啟 `!ace` 文字指令（需開 Message Content Intent） |
| `DISCORD_AI_TOP15_SCOPE`（全分點） | 權證淨買超排行的統計範圍，可改「精選五分點」 |
| `WEEKLY_PICK_EXCLUDE_CODES`（2330） | 本週精選排除的股票 |
| `DISCORD_AI_SECTOR_MIN_AVG_VALUE`（50000000）、`DISCORD_AI_SECTOR_MIN_AVG_LOTS`（500）、`DISCORD_AI_SECTOR_LIQUIDITY_DAYS`（20） | 族群排行的流動性門檻：平均成交金額（元）、平均成交量（張）、計算天數 |
| `DISCORD_AI_LIVE_FLOW_ENABLE`、`DISCORD_AI_MONEYDJ_TOP_ENABLE`、`WEEKLY_PICK_LIVE_FLOW_ENABLE`（0） | 即時抓 MoneyDJ；很吃記憶體，Railway 容易 out of memory，建議保持 0 |

---

## 測試

### 單元測試（離線，不需要金鑰）

```bash
cd ace_ai
python -m unittest test_fine_sector_catalog test_sector_analysis test_bollinger test_waiting_reply -v
```

涵蓋族群名冊與排行（含日期格式回歸測試）、布林判讀、等待圖替換、`/ask` 傳入追問記憶鍵值與排隊通知。`test_image_answers.py` 依賴本資料夾沒有的 `demo_images.py`，所以不在上面的清單。

### 固定評測題

```bash
python ace_ai/eval/run_eval.py                            # 快速模式：0 次 Gemini，只測解析、路由、追問記憶
python ace_ai/eval/run_eval.py --full                     # 完整模式：真的抓資料、呼叫 Gemini
python ace_ai/eval/run_eval.py --full --only P01,F01,G01  # 只跑指定題號
```

題目涵蓋型態、成本、只問股價、新聞、權證、分點、比較、追問序列、盤中、缺資料與族群。完整模式另外檢查：回答有沒有出現資料供應商名稱、「第 N 日」、用盤中累計量判斷量縮量增、事實核對整篇退回、缺少【回答】、比較題沒下結論，並記錄耗時與 Gemini 次數。結果寫到 `ace_ai/eval/results/*.csv`。

兩種模式都需要 `FINMIND_API_TOKEN` 與 `GCP_SERVICE_KEY`（股票名冊與分點名單），建議在 Railway Shell 或設好變數的本機執行。

### 不連 Discord 直接問

```bash
python ace_ai/discord_ai_bot.py --plan "2344現在技術面怎麼樣"      # 只看解析與路由
python ace_ai/discord_ai_bot.py --ask "2344跟2408誰比較好" --image-output answer.png
python ace_ai/discord_ai_bot.py --ask "記憶體族群現在誰形態最好" --image-output sector.png
```

本機需要中文字型：Windows 用微軟正黑體，Linux 用 Noto Sans CJK TC，或設定 `DISCORD_AI_FONT_PATH`。

---

## 已知限制

- 型態分數只看技術結構；30 個追蹤分點是手動挑選，勝率有樣本內偏誤。
- 富果盤中成交量單位以 📏 校正 Log 為準；真實盤中行情與 Gemini 端到端只能在 Railway 上驗證。
- 追問記憶與快取都在記憶體內，Bot 重啟就清空。
- 族群細分類依公開產業鏈定義，不等於市場上所有題材股。
