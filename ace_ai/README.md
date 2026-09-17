# 艾斯 AI｜Discord 私人問答助理

本版本為「全圖片回答版」。`/ask`、`!ace` 的回答、使用說明、等待、權限、冷卻、釐清問題及錯誤提示，都以圖片附件送出；文字全部在圖片內。

- 指定個股：最近 70 根日 K（跟隨原週報 `WARRANT_CHART_LOOKBACK` 設定）、MA5／10／20／60、布林三軌、成交量、完整分析文字，以及完整的橫向價量分布。
- 沒有指定股票：同樣風格的純文字資訊圖卡。
- 比較股票或本週精選：把相關股票的圖表和文字放在同一張圖。依內容自動增加高度，長回答是一張可放大的長圖，不固定 A4 高度、不裁掉內文。
- 圖表使用 Python 取得的 OHLC；Gemini 不產生價格、K 棒或均線。原有數字核對、權限及 Sheet 唯讀設定仍保留。
- 股價無法取得時，圖內標示缺資料，保留已取得的分析。
- PNG 超過附件預算時改用 JPEG；若仍超過容量，回覆縮小查詢範圍的圖卡。

### 價量分布更新

每張個股圖都直接呼叫原週報 `_calculate_weighted_volume_profile_stats(..., n_bins=40)`，以影線各 20%、實體 60% 的原始規則分配成交量，保留完整 40 格分布。橫條從圖表左側開始，長度為該格量／最大格量 × 圖表寬度／1.08；最大量區紅色、第二大量區橘色、其他價位淺藍色，透明度和原週報一致。K 棒與均線畫在分布上方。

這項繪圖資料不交給 Gemini，也不依賴提問是否包含「大量區」。缺少有效成交量時顯示「價量分布暫無有效資料」，不產生推測色帶。

已使用合成行情逐格比對使用者提供的原始函式，確認 40 格邊界、累積量及最大／第二大量區索引完全一致。`demo_profile.json` 是同一參考函式對示範行情的計算結果，只供 `demo_images.py` 離線預覽，正式查詢使用即時計算結果。

### 布林軌道與判讀

圖表上／中／下軌全部使用原週報 `calculate_indicators` 的 BB_UPPER／BB_MID／BB_LOWER，設定為 20 日均線 ± 2 倍標準差；中軌等同 MA20，以虛線標示。軌道尚無有效值時留空，不補造資料。

`bollinger_analysis.py` 以同一批日 K 計算下列觀察，送入一般技術面、Gemini 分析、本週精選與規則式備援回答。以下是本專案採用的明確判讀門檻，不是聲稱所有分析工具都採用同一套標準。

| 觀察 | 本版規則 |
|---|---|
| 向上／向下突破 | 前一日收盤在相應軌內，本日收盤跨越當日上／下軌；連續在軌外另標為持續軌外 |
| 影線穿越 | 最高／最低價穿越軌道，但收盤回到通道內，不當成收盤突破 |
| 重返通道 | 前一日收盤在軌外，本日收盤回到上下軌之間 |
| 壓縮 | 本日帶寬百分比不高於前 60 個有效帶寬的第 20 百分位；排除本日，不足 60 日則未知 |
| 收窄／擴張 | 帶寬相對 5 個交易日前減少／增加至少 10%；其餘標示持平 |
| 橫盤 | 近 10 日收盤皆在各自當日軌內、中軌變化絕對值 ≤1%、收盤高低差／平均收盤 ≤6%，且帶寬沒有擴張 |
| 沿上軌／沿下軌 | 連續 3 日 %b ≥80% 且中軌 5 日上升／%b ≤20% 且中軌下降 |
| 壓縮後突破 | 前 5 個交易日曾達壓縮條件，且本日收盤首次跨越上／下軌 |

帶寬百分比＝（上軌－下軌）／中軌 ×100；%b＝（收盤－下軌）／（上軌－下軌）×100。同時提供當日量對前 20 日均量的比值作量能背景。缺資料用 null 表示，不等同未觸發。

原作者說明觸軌本身不是買賣訊號，BandWidth 可用於辨識波動收斂；本版也不讓 AI 由壓縮自行預測方向。參考：[Bollinger Band Rules](https://www.bollingerbands.com/bollinger-band-rules)。

共 24 項離線測試通過，涵蓋完整價量分布、布林突破／影線／重返通道、壓縮、擴張、橫盤、沿軌、資料不足、查詢路由及圖片傳送。原有精選分數權重未新增布林分數；新增狀態提供給解讀。本週精選快取鍵已更新，避免沿用缺少布林欄位的舊結果。

## 安裝這份更新

這是原 GitHub 專案中 `ace_ai/` 資料夾的替換包，不是獨立完整 repo。將包內 `ace_ai/` 覆蓋到原 repo 同名資料夾，再提交部署。保留根目錄的 `requirements.txt`、週報主程式、種子資料與其他原始檔案。

Railway 的 Root Directory 留空，Config File Path 使用 `/ace_ai/railway.toml`。本版 Dockerfile 會安裝 `fonts-noto-cjk` 中文字型，並透過 `ace_ai/requirements.txt` 安裝 Pillow。

若根目錄的週報檔案實際名為 `K_function_warrant_report_20260530 (19).py`，請將它在 repo 內命名為 `K_function_warrant_report_20260530.py`，或把 `WARRANT_CORE_SCRIPT` 設成容器中的實際路徑。檔案須已提交到 repo；Windows Downloads 路徑無法供 Railway 使用。

Discord Bot 須有 View Channel、Send Messages、Read Message History、Attach Files 權限。`!ace` 需要在 Developer Portal → Bot 開啟 Message Content Intent；程式已設定 `intents.message_content = True`。詳見 [discord.py intents 說明](https://discordpy.readthedocs.io/en/stable/intents.html#message-content)。

這份交付已通過本機離線測試與圖片目視檢查；沒有使用真實 API 金鑰，未做 Railway 建置、Google Sheet／行情連線或 Discord 傳送實測。

## 圖片預覽與測試

```bash
python ace_ai/demo_images.py --output-dir preview
python -m unittest discover -s ace_ai -p "test_*.py" -v
python ace_ai/discord_ai_bot.py --ask "2344現在技術面怎麼樣" --image-output answer.png
```

前兩項可離線執行；範例圖片全部使用明確標示的合成行情。第三項會使用你部署設定中的實際資料來源及 Gemini。

本機需有中文字型；Windows 預設使用微軟正黑體，Linux 使用 Noto Sans CJK TC，亦可設定 `DISCORD_AI_FONT_PATH` 指定字型。

## !ace 功能

`!ace 問題` 和 `/ask 問題` 使用同一套查詢引擎，不需要兩次設定。直接輸入 `!ace` 可取得使用說明圖卡。

| 用途 | 範例 |
|---|---|
| 股價與量能 | `!ace 2344股價` |
| 技術指標、均線、KD、MACD、布林 | `!ace 2344現在技術面怎麼樣` |
| 布林突破、壓縮、橫盤 | `!ace 2344布林有突破嗎？目前壓縮還是橫盤？` |
| 大量區、支撐壓力 | `!ace 華邦電現在在大量區哪裡` |
| 權證分點買賣與高勝率分點 | `!ace 2344有哪些高勝率分點最近在加碼` |
| A～E 事件歷史績效 | `!ace 永豐金內湖D事件勝率` |
| 分點近期交易與操作習性 | `!ace 永豐金內湖最近在買什麼` |
| 分點勝率排行 | `!ace 分點勝率排行` |
| 新聞與綜合分析 | `!ace 分析2344目前權證籌碼、技術面、大量區與近期新聞` |
| 本週精選與條件篩選 | `!ace 本週精選 只看D事件 勝率70%以上 排除漲太多` |

資料為既有來源的日 K、權證與統計；不是盤中即時報價，也不會下單。一般比較一次最多 2 檔，本週精選走獨立的候選排序流程。

在 Discord 輸入 `/ask 問題`（或 `!ace 問題`），Bot 會抓取相關的股價、權證分點、A～E 事件績效、大量區與新聞資料，整理後交給 Gemini 解釋。

- 所有數字都由 Python 計算：直接重用 repo 根目錄 `K_function_warrant_report_20260530.py` 的函式，以及回測輸出到 Google Sheet 的結果，Gemini 只負責文字說明。
- Bot 對 Google Sheet **只讀不寫**。
- 這個資料夾不會影響週報、回測或任何 GitHub Actions。

## 檔案

| 檔案 | 用途 |
|---|---|
| `discord_ai_bot.py` | 入口：Discord `/ask`、`!ace`、權限、問題解析、路由、Gemini、數字核對 |
| `warrant_ai_tools.py` | 資料 Tool 層：股價、技術面、大量區、權證分點、A～E 事件績效、分點近期操作、新聞、Sheet 唯讀查詢 |
| `weekly_pick.py` | 本週精選候選股：篩選 → 評分 → TOP5 |
| `requirements.txt` | 根目錄 requirements.txt ＋ discord.py |
| `Dockerfile`、`railway.toml` | Railway 部署設定 |
| `env.example` | 環境變數清單（沒有任何真實 Secret） |

## 使用範例

```text
/ask 2344現在技術面怎麼樣
/ask 華邦電現在在大量區哪裡
/ask 2344有哪些高勝率分點最近在加碼
/ask 永豐金內湖D事件勝率
/ask 永豐金內湖最近的操作習性
/ask 分析2344目前權證籌碼、技術面、大量區與近期新聞
/ask 本週精選
/ask 本週精選 只看D事件 勝率70%以上 排除漲太多
/ask 本週精選 refresh
```

## 本機測試（不連 Discord）

需要先設定好 `env.example` 列出的 Secret：

```bash
python ace_ai/discord_ai_bot.py --plan "2344現在技術面怎麼樣"
python ace_ai/discord_ai_bot.py --ask "本週精選" --debug
```

## 部署

Railway 新建 Service 並連接這個 GitHub repo：

- **Root Directory**：留空（使用 repo 根目錄）
- **Config File Path**：`/ace_ai/railway.toml`
- **Variables**：依照 `env.example` 填入

Discord Bot 請使用**新的 Discord Application**，不要設定 Interactions Endpoint URL（現有 Cloudflare Worker 的 `/w` 使用另一個 Application）。
