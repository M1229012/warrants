# 艾斯 AI｜Discord 私人問答助理

本版本為「全圖片回答版」。`/ask`、`!ace` 的回答、使用說明、等待、權限、冷卻、釐清問題及錯誤提示，都以圖片附件送出；文字全部在圖片內。

- 指定個股：最近 70 根日 K、MA5／10／20／60、成交量、完整分析文字；查詢取得大量區資料時加入區帶。
- 沒有指定股票：同樣風格的純文字資訊圖卡。
- 比較股票或本週精選：把相關股票的圖表和文字放在同一張圖。依內容自動增加高度，長回答是一張可放大的長圖，不固定 A4 高度、不裁掉內文。
- 圖表使用 Python 取得的 OHLC；Gemini 不產生價格、K 棒或均線。原有數字核對、權限及 Sheet 唯讀設定仍保留。
- 股價無法取得時，圖內標示缺資料，保留已取得的分析。
- PNG 超過附件預算時改用 JPEG；若仍超過容量，回覆縮小查詢範圍的圖卡。

## 安裝這份更新

這是原 GitHub 專案中 `ace_ai/` 資料夾的替換包，不是獨立完整 repo。將包內 `ace_ai/` 覆蓋到原 repo 同名資料夾，再提交部署。保留根目錄的 `requirements.txt`、週報主程式、種子資料與其他原始檔案。

Railway 的 Root Directory 留空，Config File Path 使用 `/ace_ai/railway.toml`。本版 Dockerfile 會安裝 `fonts-noto-cjk` 中文字型，並透過 `ace_ai/requirements.txt` 安裝 Pillow。

若根目錄的週報檔案實際名為 `K_function_warrant_report_20260530 (19).py`，請將它在 repo 內命名為 `K_function_warrant_report_20260530.py`，或把 `WARRANT_CORE_SCRIPT` 設成容器中的實際路徑。檔案須已提交到 repo；Windows Downloads 路徑無法供 Railway 使用。

Discord Bot 須有 View Channel、Send Messages、Read Message History、Attach Files 權限。`!ace` 需要在 Developer Portal → Bot 開啟 Message Content Intent；程式已設定 `intents.message_content = True`。詳見 [discord.py intents 說明](https://discordpy.readthedocs.io/en/stable/intents.html#message-content)。

這份交付已通過本機離線測試與圖片目視檢查；沒有使用真實 API 金鑰，未做 Railway 建置、Google Sheet／行情連線或 Discord 傳送實測。

## 圖片預覽與測試

```bash
python ace_ai/demo_images.py --output-dir preview
python -m unittest discover -s ace_ai -p test_image_answers.py -v
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
