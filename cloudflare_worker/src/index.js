import { verifyKey } from "discord-interactions";

const TYPE = { PING: 1, APPLICATION_COMMAND: 2 };
const RESP = { PONG: 1, CHANNEL_MESSAGE_WITH_SOURCE: 4 };

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function text(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function reply(content, ephemeral = false) {
  return json({
    type: RESP.CHANNEL_MESSAGE_WITH_SOURCE,
    data: { content, flags: ephemeral ? 64 : 0 },
  });
}

function normalizeStockCode(value) {
  let s = String(value || "").trim().toUpperCase().replaceAll("'", "");
  s = s.replace(/\s+/g, "");
  if (/^\d+\.0$/.test(s)) s = s.slice(0, -2);
  if (/^\d+$/.test(s) && s.length < 4) s = s.padStart(4, "0");
  return s;
}

function isSingleStockCode(code) {
  return /^(?:\d{4,6}|\d{5}[A-Z])$/.test(code);
}

function containsSeparator(value) {
  return /[,，\s　\/\\|、;；]/.test(String(value || ""));
}

function parseCsv(textValue) {
  const rows = [];
  let row = [];
  let cell = "";
  let inQuotes = false;
  const s = String(textValue || "").replace(/^\uFEFF/, "");

  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const next = s[i + 1];

    if (inQuotes) {
      if (ch === '"' && next === '"') {
        cell += '"';
        i++;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        cell += ch;
      }
      continue;
    }

    if (ch === '"') inQuotes = true;
    else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (ch !== "\r") {
      cell += ch;
    }
  }

  row.push(cell);
  rows.push(row);
  return rows.filter((r) => r.some((c) => String(c || "").trim() !== ""));
}

function headerIndex(headers, candidates) {
  const h = headers.map((x) => String(x || "").trim());
  for (const cand of candidates) {
    const idx = h.indexOf(cand);
    if (idx >= 0) return idx;
  }
  return -1;
}

async function loadStockMap(env) {
  if (!env.STOCK_CSV_URL) {
    throw new Error("缺少 STOCK_CSV_URL，無法讀取股票名稱快取 CSV。");
  }

  const resp = await fetch(env.STOCK_CSV_URL, {
    cf: {
      cacheTtl: Number(env.STOCK_CSV_CACHE_TTL_SECONDS || 300),
      cacheEverything: true,
    },
  });

  if (!resp.ok) throw new Error(`讀取股票名稱 CSV 失敗：HTTP ${resp.status}`);

  const rows = parseCsv(await resp.text());
  if (rows.length < 2) throw new Error("股票名稱 CSV 沒有資料。");

  const headers = rows[0].map((x) => String(x || "").trim());
  const codeIdx = headerIndex(headers, ["代號", "股票代號", "證券代號", "有價證券代號", "公司代號"]);
  const nameIdx = headerIndex(headers, ["名稱", "股票名稱", "證券名稱", "有價證券名稱", "公司名稱", "公司簡稱"]);

  if (codeIdx < 0 || nameIdx < 0) {
    throw new Error("股票名稱 CSV 缺少「代號」或「名稱」欄位。");
  }

  const map = new Map();
  for (const row of rows.slice(1)) {
    if (row.length <= Math.max(codeIdx, nameIdx)) continue;
    const code = normalizeStockCode(row[codeIdx]);
    const name = String(row[nameIdx] || "").trim();
    if (code && name && name !== "未知公司") map.set(code, name);
  }
  return map;
}

async function githubFetch(env, path, options = {}) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}${path}`;
  const resp = await fetch(url, {
    ...options,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "cloudflare-discord-warrant-worker",
      ...(options.headers || {}),
    },
  });

  const body = await resp.text();
  let data = null;
  try { data = body ? JSON.parse(body) : null; } catch { data = body; }

  if (!resp.ok) {
    throw new Error(`GitHub API 失敗：HTTP ${resp.status}｜${String(body).slice(0, 500)}`);
  }
  return data;
}

async function hasActiveWorkflowRun(env) {
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW_FILE);
  const branch = encodeURIComponent(env.GITHUB_BRANCH || "main");

  for (const status of ["queued", "in_progress"]) {
    const data = await githubFetch(
      env,
      `/actions/workflows/${workflow}/runs?branch=${branch}&status=${status}&per_page=10`,
      { method: "GET" },
    );

    const runs = Array.isArray(data?.workflow_runs) ? data.workflow_runs : [];
    if (runs.length > 0) return { active: true, run: runs[0] };
  }
  return { active: false, run: null };
}

async function triggerWorkflow(env, stockCode, refreshNewsSummary) {
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW_FILE);
  const inputs = { stock_codes: stockCode };

  if (String(env.WORKFLOW_HAS_REFRESH_INPUT || "1") !== "0") {
    inputs.refresh_news_summary = refreshNewsSummary ? "1" : "0";
  }

  await githubFetch(env, `/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({
      ref: env.GITHUB_BRANCH || "main",
      inputs,
    }),
  });
}

function option(interaction, name) {
  const options = interaction?.data?.options || [];
  const found = options.find((o) => o.name === name);
  return found ? found.value : undefined;
}

async function handleCommand(interaction, env) {
  if (interaction?.data?.name !== "w") {
    return reply("❌ 不支援的指令。", true);
  }

  const stockRaw = option(interaction, "stock");
  const refreshNewsSummary = option(interaction, "refresh") === true;

  if (!stockRaw) return reply("❌ 請輸入股票代號，例如 `/w stock:2408`。", true);
  if (containsSeparator(stockRaw)) {
    return reply("❌ 一次只能查詢一檔股票，請只輸入單一代號，例如 `2408`。", true);
  }

  const stockCode = normalizeStockCode(stockRaw);
  if (!isSingleStockCode(stockCode)) {
    return reply("❌ 股票代號格式錯誤，請輸入單一股票代號，例如 `2408`。", true);
  }

  const stockMap = await loadStockMap(env);
  const stockName = stockMap.get(stockCode);

  if (!stockName) {
    return reply(`❌ 查無股票代號 \`${stockCode}\`，未執行週報。請確認 Google Sheet「快取_股票名稱」是否有此代號。`, true);
  }

  const active = await hasActiveWorkflowRun(env);
  if (active.active) {
    const url = active.run?.html_url || "";
    return reply(`⏳ 目前已有週報產生中，請等上一檔完成後再查下一檔。${url ? `\n${url}` : ""}`, true);
  }

  await triggerWorkflow(env, stockCode, refreshNewsSummary);

  const refreshText = refreshNewsSummary
    ? "\n🔄 本次會重新抓新聞並重新產生本週重點。"
    : "\n📦 本次會優先使用當日快取；沒有快取才重新產生。";

  return reply(`✅ 已確認：\`${stockCode} ${stockName}\`\n🚀 已觸發 GitHub Actions 產生權證週報。${refreshText}`);
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return text("Discord interactions endpoint is running.");

    const signature = request.headers.get("x-signature-ed25519");
    const timestamp = request.headers.get("x-signature-timestamp");
    const body = await request.text();

    if (!signature || !timestamp || !env.DISCORD_PUBLIC_KEY) {
      return text("Missing Discord signature headers or DISCORD_PUBLIC_KEY.", 401);
    }

    const valid = await verifyKey(body, signature, timestamp, env.DISCORD_PUBLIC_KEY);
    if (!valid) return text("Bad request signature.", 401);

    let interaction;
    try { interaction = JSON.parse(body); }
    catch { return text("Invalid JSON.", 400); }

    if (interaction.type === TYPE.PING) return json({ type: RESP.PONG });

    if (interaction.type === TYPE.APPLICATION_COMMAND) {
      try { return await handleCommand(interaction, env); }
      catch (err) { return reply(`❌ 執行失敗：${err.message || err}`, true); }
    }

    return reply("❌ 不支援的 Discord interaction type。", true);
  },
};
