# Cloudflare Discord Warrant Worker

功能：
- Discord 使用 `/w stock:2408`
- Cloudflare Worker 先確認股票代號格式
- 從 Google Sheet「快取_股票名稱」CSV 檢查代號是否存在
- 檢查 GitHub Actions 是否已經有週報在跑
- 沒有執行中才觸發 GitHub Actions `workflow_dispatch`

## 安裝

```bash
npm install
```

## 設定 wrangler.toml

```bash
cp wrangler.toml.example wrangler.toml
```

修改：
- `GITHUB_REPO`
- `GITHUB_WORKFLOW_FILE`
- `STOCK_CSV_URL`

## 設定 Cloudflare Secrets

```bash
npx wrangler secret put DISCORD_PUBLIC_KEY
npx wrangler secret put GITHUB_TOKEN
```

## 部署 Worker

```bash
npx wrangler deploy
```

部署後會得到 Worker URL，例如：

```text
https://warrant-discord-worker.xxx.workers.dev
```

## Discord Interactions Endpoint URL

到 Discord Developer Portal：

```text
Application → General Information → Interactions Endpoint URL
```

填入 Worker URL。

## 註冊 Slash Command

建立本機環境檔：

```bash
cp .dev.vars.example .dev.vars
```

填入：
- `DISCORD_APPLICATION_ID`
- `DISCORD_BOT_TOKEN`
- `DISCORD_GUILD_ID`

然後執行：

```bash
npm run register
```

## 使用

```text
/w stock:2408
```

重新抓新聞與本週重點：

```text
/w stock:2408 refresh:true
```
