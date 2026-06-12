import { verifyKey } from "discord-interactions";

const TYPE = {
  PING: 1,
  APPLICATION_COMMAND: 2,
  MESSAGE_COMPONENT: 3,
};

const RESP = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
  DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5,
  DEFERRED_UPDATE_MESSAGE: 6,
  UPDATE_MESSAGE: 7,
};

const COMPONENT = {
  ACTION_ROW: 1,
  BUTTON: 2,
  STRING_SELECT: 3,
};

const BUTTON_STYLE = {
  PRIMARY: 1,
  SECONDARY: 2,
  SUCCESS: 3,
  DANGER: 4,
};

const BROKER_SELECT_PAGE_SIZE = 25;

const BROKER_DISPLAY_NAMES = {
  群益東大: "群益金鼎東大",
};

const BROKER_CATEGORIES = [
  {
    key: "yuanta",
    label: "元大",
    brokers: [
      "元大內湖民權",
      "元大南屯",
      "元大善化",
      "元大敦化",
      "元大雙和",
    ],
  },
  {
    key: "sinopac",
    label: "永豐金",
    brokers: [
      "永豐金內湖",
      "永豐金竹北",
      "永豐金竹科",
      "永豐金萬盛",
      "永豐金潮州",
    ],
  },
  {
    key: "huanan",
    label: "華南永昌",
    brokers: [
      "華南永昌世貿",
      "華南永昌台中",
      "華南永昌岡山",
    ],
  },
  {
    key: "fubon",
    label: "富邦",
    brokers: [
      "富邦公益",
      "富邦北高雄",
      "富邦台北",
      "富邦敦南",
    ],
  },
  {
    key: "capital",
    label: "群益金鼎",
    brokers: [
      "群益東大",
      "群益金鼎中壢",
      "群益金鼎北高雄",
      "群益金鼎古亭",
    ],
  },
  {
    key: "other",
    label: "其他",
    brokers: [
      "新光",
      "福邦",
      "第一金",
      "第一金中壢",
      "第一金安和",
      "兆豐小港",
      "凱基士林",
      "凱基科園",
      "國票中正",
      "國票敦北法人",
    ],
  },
];

function displayBrokerName(brokerName) {
  const name = String(brokerName || "").trim();
  return BROKER_DISPLAY_NAMES[name] || name;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
    },
  });
}

function text(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
    },
  });
}

function reply(content, ephemeral = false) {
  return json({
    type: RESP.CHANNEL_MESSAGE_WITH_SOURCE,
    data: {
      content,
      flags: ephemeral ? 64 : 0,
    },
  });
}

function replyData(data, ephemeral = false) {
  return json({
    type: RESP.CHANNEL_MESSAGE_WITH_SOURCE,
    data: {
      ...data,
      flags: ephemeral ? 64 : data?.flags || 0,
    },
  });
}

function updateMessageData(data) {
  return json({
    type: RESP.UPDATE_MESSAGE,
    data,
  });
}

function deferReply(ephemeral = false) {
  return json({
    type: RESP.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
    data: {
      flags: ephemeral ? 64 : 0,
    },
  });
}

function deferUpdateMessage() {
  return json({
    type: RESP.DEFERRED_UPDATE_MESSAGE,
  });
}

async function editOriginalResponse(interaction, content) {
  const url = `https://discord.com/api/v10/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;

  const resp = await fetch(url, {
    method: "PATCH",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify({
      content,
    }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    console.error(`編輯 Discord 原始回應失敗：HTTP ${resp.status}｜${body}`);
  }
}

async function editOriginalResponseData(interaction, data) {
  const url = `https://discord.com/api/v10/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;

  const resp = await fetch(url, {
    method: "PATCH",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(data),
  });

  if (!resp.ok) {
    const body = await resp.text();
    console.error(`編輯 Discord 原始回應失敗：HTTP ${resp.status}｜${body}`);
  }
}

function normalizeStockCode(value) {
  let s = String(value || "").trim().toUpperCase().replaceAll("'", "");
  s = s.replace(/\s+/g, "");

  if (/^\d+\.0$/.test(s)) {
    s = s.slice(0, -2);
  }

  if (/^\d+$/.test(s) && s.length < 4) {
    s = s.padStart(4, "0");
  }

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

    if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
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

  return rows.filter((r) =>
    r.some((c) => String(c || "").trim() !== "")
  );
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

  if (!resp.ok) {
    throw new Error(`讀取股票名稱 CSV 失敗：HTTP ${resp.status}`);
  }

  const rows = parseCsv(await resp.text());

  if (rows.length < 2) {
    throw new Error("股票名稱 CSV 沒有資料。");
  }

  const headers = rows[0].map((x) => String(x || "").trim());

  const codeIdx = headerIndex(headers, [
    "代號",
    "股票代號",
    "證券代號",
    "有價證券代號",
    "公司代號",
  ]);

  const nameIdx = headerIndex(headers, [
    "名稱",
    "股票名稱",
    "證券名稱",
    "有價證券名稱",
    "公司名稱",
    "公司簡稱",
  ]);

  if (codeIdx < 0 || nameIdx < 0) {
    throw new Error("股票名稱 CSV 缺少「代號」或「名稱」欄位。");
  }

  const map = new Map();

  for (const row of rows.slice(1)) {
    if (row.length <= Math.max(codeIdx, nameIdx)) continue;

    const code = normalizeStockCode(row[codeIdx]);
    const name = String(row[nameIdx] || "").trim();

    if (code && name && name !== "未知公司") {
      map.set(code, name);
    }
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
  try {
    data = body ? JSON.parse(body) : null;
  } catch {
    data = body;
  }

  if (!resp.ok) {
    throw new Error(
      `GitHub API 失敗：HTTP ${resp.status}｜${String(body).slice(0, 500)}`
    );
  }

  return data;
}

async function hasActiveWorkflowRunByFile(env, workflowFile) {
  const workflow = encodeURIComponent(workflowFile);
  const branch = encodeURIComponent(env.GITHUB_BRANCH || "main");

  for (const status of ["queued", "in_progress"]) {
    const data = await githubFetch(
      env,
      `/actions/workflows/${workflow}/runs?branch=${branch}&status=${status}&per_page=10`,
      {
        method: "GET",
      }
    );

    const runs = Array.isArray(data?.workflow_runs)
      ? data.workflow_runs
      : [];

    if (runs.length > 0) {
      return {
        active: true,
        run: runs[0],
      };
    }
  }

  return {
    active: false,
    run: null,
  };
}

async function hasActiveWorkflowRun(env) {
  return hasActiveWorkflowRunByFile(env, env.GITHUB_WORKFLOW_FILE);
}

function getBroker10DWorkflowFile(env) {
  return (
    env.BROKER_10D_WORKFLOW_FILE ||
    env.DISCORD_REPORT_WORKFLOW_FILE ||
    "discord_warrant_report.yml"
  );
}

async function triggerWorkflow(env, stockCode, refreshNewsSummary) {
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW_FILE);

  const inputs = {
    stock_codes: stockCode,
  };

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

async function triggerBroker10DWorkflow(env, brokerName) {
  const workflowFile = getBroker10DWorkflowFile(env);
  const workflow = encodeURIComponent(workflowFile);

  const inputs = {
    run_plan: env.BROKER_10D_RUN_PLAN || "近10日分點買賣明細圖",
    broker_name: brokerName,
  };

  if (env.BROKER_10D_TARGET_DATE) {
    inputs.target_date = env.BROKER_10D_TARGET_DATE;
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

function getBrokerCategory(categoryKey) {
  return BROKER_CATEGORIES.find((cat) => cat.key === categoryKey) || null;
}

function buildBrokerCategoryButtonsData() {
  return {
    content: "請選擇要查詢的分點分類：",
    components: [
      {
        type: COMPONENT.ACTION_ROW,
        components: BROKER_CATEGORIES.map((cat) => ({
          type: COMPONENT.BUTTON,
          style: BUTTON_STYLE.PRIMARY,
          label: cat.label,
          custom_id: `ww_cat:${cat.key}:0`,
        })),
      },
    ],
  };
}

function clampPage(page, totalPages) {
  const n = Number(page);

  if (!Number.isFinite(n)) {
    return 0;
  }

  if (n < 0) {
    return 0;
  }

  if (n >= totalPages) {
    return Math.max(totalPages - 1, 0);
  }

  return Math.floor(n);
}

function buildBrokerSelectData(categoryKey, page = 0) {
  const category = getBrokerCategory(categoryKey);

  if (!category) {
    return {
      content: "❌ 找不到這個分點分類，請重新輸入 `/ww`。",
      components: [],
    };
  }

  const brokers = category.brokers || [];
  const totalPages = Math.max(
    Math.ceil(brokers.length / BROKER_SELECT_PAGE_SIZE),
    1
  );

  const currentPage = clampPage(page, totalPages);
  const start = currentPage * BROKER_SELECT_PAGE_SIZE;
  const pageBrokers = brokers.slice(start, start + BROKER_SELECT_PAGE_SIZE);

  const components = [];

  if (pageBrokers.length > 0) {
    components.push({
      type: COMPONENT.ACTION_ROW,
      components: [
        {
          type: COMPONENT.STRING_SELECT,
          custom_id: `ww_broker:${category.key}:${currentPage}`,
          placeholder: `選擇${category.label}分點`,
          min_values: 1,
          max_values: 1,
          options: pageBrokers.map((broker) => ({
            label: displayBrokerName(broker),
            value: broker,
            description: "產生近10日分點買賣明細圖",
          })),
        },
      ],
    });
  }

  const buttons = [
    {
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "返回分類",
      custom_id: "ww_back",
    },
  ];

  if (totalPages > 1) {
    buttons.push({
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "上一頁",
      custom_id: `ww_page:${category.key}:${currentPage - 1}`,
      disabled: currentPage <= 0,
    });

    buttons.push({
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "下一頁",
      custom_id: `ww_page:${category.key}:${currentPage + 1}`,
      disabled: currentPage >= totalPages - 1,
    });
  }

  components.push({
    type: COMPONENT.ACTION_ROW,
    components: buttons,
  });

  return {
    content: `請選擇要產生近10日買賣明細圖的分點：\n分類：${category.label}｜第 ${currentPage + 1}/${totalPages} 頁`,
    components,
  };
}

async function handleCommand(interaction, env) {
  const commandName = interaction?.data?.name;

  if (!["w", "ww"].includes(commandName)) {
    return reply("❌ 不支援的指令。", true);
  }

  if (commandName === "ww") {
    return replyData(buildBrokerCategoryButtonsData(), true);
  }

  const stockRaw = option(interaction, "stock");
  const refreshRaw = option(interaction, "refresh");

  // /w：新聞與本週重點優先使用當日快取；refresh:true 時重新抓新聞並重新產生本週重點
  const refreshNewsSummary =
    refreshRaw === true || String(refreshRaw || "").toLowerCase() === "true";

  if (!stockRaw) {
    return reply(
      `❌ 請輸入股票代號，例如 \`/${commandName} stock:2408\`。`,
      true
    );
  }

  if (containsSeparator(stockRaw)) {
    return reply(
      "❌ 一次只能查詢一檔股票，請只輸入單一代號，例如 `2408`。",
      true
    );
  }

  const stockCode = normalizeStockCode(stockRaw);

  if (!isSingleStockCode(stockCode)) {
    return reply(
      "❌ 股票代號格式錯誤，請輸入單一股票代號，例如 `2408`。",
      true
    );
  }

  const stockMap = await loadStockMap(env);
  const stockName = stockMap.get(stockCode);

  if (!stockName) {
    return reply(
      `❌ 查無股票代號 \`${stockCode}\`，未執行週報。請確認 Google Sheet「快取_股票名稱」是否有此代號。`,
      true
    );
  }

  const active = await hasActiveWorkflowRun(env);

  if (active.active) {
    return reply(
      "⏳ 目前已有週報產生中，請等上一檔完成後再查下一檔。",
      true
    );
  }

  await triggerWorkflow(env, stockCode, refreshNewsSummary);

  const refreshText = refreshNewsSummary
    ? "\n🔄 本次會重新抓新聞並重新產生本週重點。"
    : "\n📦 本次會優先使用當日快取；沒有快取才重新產生。";

  return reply(
    `✅ 已確認：\`${stockCode} ${stockName}\`\n🔎 已成功送出查詢，權證週報正在產生中。${refreshText}`,
    false
  );
}

async function handleComponent(interaction, env) {
  const customId = String(interaction?.data?.custom_id || "");

  if (customId === "ww_back") {
    return updateMessageData(buildBrokerCategoryButtonsData());
  }

  if (customId.startsWith("ww_cat:")) {
    const parts = customId.split(":");
    const categoryKey = parts[1] || "";
    const page = parts[2] || "0";

    return updateMessageData(buildBrokerSelectData(categoryKey, page));
  }

  if (customId.startsWith("ww_page:")) {
    const parts = customId.split(":");
    const categoryKey = parts[1] || "";
    const page = parts[2] || "0";

    return updateMessageData(buildBrokerSelectData(categoryKey, page));
  }

  if (customId.startsWith("ww_broker:")) {
    return deferUpdateMessage();
  }

  return reply("❌ 不支援的互動元件。", true);
}

async function handleBrokerSelectionAndEdit(interaction, env) {
  try {
    const customId = String(interaction?.data?.custom_id || "");

    if (!customId.startsWith("ww_broker:")) {
      await editOriginalResponseData(interaction, {
        content: "❌ 不支援的分點選單。",
        components: [],
      });
      return;
    }

    const brokerName = String(interaction?.data?.values?.[0] || "").trim();

    if (!brokerName) {
      await editOriginalResponseData(interaction, {
        content: "❌ 沒有選到分點，請重新輸入 `/ww`。",
        components: [],
      });
      return;
    }

    const workflowFile = getBroker10DWorkflowFile(env);
    const active = await hasActiveWorkflowRunByFile(env, workflowFile);

    if (active.active) {
      await editOriginalResponseData(interaction, {
        content: "⏳ 目前已有分點圖產生中，請等上一個完成後再查下一個分點。",
        components: [],
      });
      return;
    }

    await triggerBroker10DWorkflow(env, brokerName);

    await editOriginalResponseData(interaction, {
      content:
        `✅ 已確認分點：\`${displayBrokerName(brokerName)}\`\n` +
        "📊 已成功送出查詢，「近10日分點買賣明細圖」正在產生中。",
      components: [],
    });
  } catch (err) {
    await editOriginalResponseData(interaction, {
      content: `❌ 執行失敗：${err.message || err}`,
      components: [],
    });
  }
}

async function runCommandAndEdit(interaction, env) {
  try {
    const response = await handleCommand(interaction, env);
    const data = await response.json();
    const content = data?.data?.content || "✅ 指令已處理完成。";

    await editOriginalResponse(interaction, content);
  } catch (err) {
    await editOriginalResponse(
      interaction,
      `❌ 執行失敗：${err.message || err}`
    );
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return text("Discord interactions endpoint is running.");
    }

    const signature = request.headers.get("x-signature-ed25519");
    const timestamp = request.headers.get("x-signature-timestamp");
    const body = await request.text();

    if (!signature || !timestamp || !env.DISCORD_PUBLIC_KEY) {
      return text(
        "Missing Discord signature headers or DISCORD_PUBLIC_KEY.",
        401
      );
    }

    const valid = await verifyKey(
      body,
      signature,
      timestamp,
      env.DISCORD_PUBLIC_KEY
    );

    if (!valid) {
      return text("Bad request signature.", 401);
    }

    let interaction;

    try {
      interaction = JSON.parse(body);
    } catch {
      return text("Invalid JSON.", 400);
    }

    if (interaction.type === TYPE.PING) {
      return json({
        type: RESP.PONG,
      });
    }

    if (interaction.type === TYPE.APPLICATION_COMMAND) {
      const commandName = interaction?.data?.name;

      if (commandName === "ww") {
        return handleCommand(interaction, env);
      }

      ctx.waitUntil(runCommandAndEdit(interaction, env));
      return deferReply(false);
    }

    if (interaction.type === TYPE.MESSAGE_COMPONENT) {
      const customId = String(interaction?.data?.custom_id || "");

      if (customId.startsWith("ww_broker:")) {
        ctx.waitUntil(handleBrokerSelectionAndEdit(interaction, env));
        return handleComponent(interaction, env);
      }

      return handleComponent(interaction, env);
    }

    return reply("❌ 不支援的 Discord interaction type。", true);
  },
};
