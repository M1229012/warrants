# 艾斯 AI｜Discord 私人問答助理

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
