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
      "元大彰化民生",
      "元大南屯",
      "元大虎尾",
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
      "永豐金市政",
      "永豐金信義",
      "永豐金敦南",
    ],
  },
  {
    key: "huanan",
    label: "華南永昌",
    brokers: [
      "華南永昌台中",
      "華南永昌岡山",
    ],
  },
  {
    key: "fubon",
    label: "富邦",
    brokers: [
      "富邦公益",
      "富邦仁愛",
      "富邦敦南",
    ],
  },
  {
    key: "capital",
    label: "群益金鼎",
    brokers: [
      "群益金鼎東大",
      "群益金鼎中壢",
      "群益金鼎新竹",
      "群益金鼎古亭",
    ],
  },
  {
    key: "first",
    label: "凱基",
    brokers: [
      "凱基士林",
      "凱基中山",
      "凱基竹科",
    ],
  },
  {
    key: "other",
    label: "其他",
    brokers: [
      "新光",
      "福邦證券",
      "兆豐板橋",
      "國票中正",
      "國票敦北法人",
      "統一三多",
      "第一金中壢",
    ],
  },
];

const DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES = [
  "華南永昌台中",
  "元大南屯",
  "新光",
  "永豐金內湖",
  "富邦敦南",
];

const ALL_BRANCH_FLOW_BROKERS = [];
const BRANCH_FLOW_BROKER_INDEX = new Map();

for (const category of BROKER_CATEGORIES) {
  for (const broker of category.brokers || []) {
    if (!BRANCH_FLOW_BROKER_INDEX.has(broker)) {
      BRANCH_FLOW_BROKER_INDEX.set(broker, ALL_BRANCH_FLOW_BROKERS.length);
      ALL_BRANCH_FLOW_BROKERS.push(broker);
    }
  }
}

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

async function triggerWorkflow(
  env,
  stockCode,
  refreshNewsSummary,
  selectedBranchFlowMode = "five",
  selectedBranchFlowBranches = "五分點"
) {
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW_FILE);

  const inputs = {
    stock_codes: stockCode,
  };

  if (String(env.WORKFLOW_HAS_REFRESH_INPUT || "1") !== "0") {
    inputs.refresh_news_summary = refreshNewsSummary ? "1" : "0";
  }

  if (String(env.WORKFLOW_HAS_SELECTED_BRANCH_INPUT || "1") !== "0") {
    inputs.selected_branch_flow_mode = selectedBranchFlowMode || "five";
    inputs.selected_branch_flow_branches = selectedBranchFlowBranches || "五分點";
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

function getInteractionUserId(interaction) {
  return (
    interaction?.member?.user?.id ||
    interaction?.user?.id ||
    ""
  );
}

function parseBoolFlag(value) {
  return String(value || "0") === "1";
}

function selectedKeysFromBrokerNames(brokers) {
  const keys = [];
  const seen = new Set();

  for (const broker of brokers || []) {
    const idx = BRANCH_FLOW_BROKER_INDEX.get(broker);

    if (idx === undefined) {
      continue;
    }

    const key = idx.toString(36);

    if (!seen.has(key)) {
      seen.add(key);
      keys.push(key);
    }
  }

  return keys;
}

function selectedKeysToBrokerNames(keys) {
  const names = [];
  const seen = new Set();

  for (const key of keys || []) {
    const idx = parseInt(String(key || ""), 36);

    if (!Number.isInteger(idx) || idx < 0 || idx >= ALL_BRANCH_FLOW_BROKERS.length) {
      continue;
    }

    const broker = ALL_BRANCH_FLOW_BROKERS[idx];

    if (broker && !seen.has(broker)) {
      seen.add(broker);
      names.push(broker);
    }
  }

  return names;
}

function encodeSelectedKeys(keys) {
  return selectedKeysFromBrokerNames(selectedKeysToBrokerNames(keys)).join(".");
}

function decodeSelectedKeys(value) {
  const raw = String(value || "").trim();

  if (!raw) {
    return [];
  }

  return encodeSelectedKeys(raw.split(".").filter(Boolean)).split(".").filter(Boolean);
}

function makeWCustomId(prefix, userId, stockCode, refreshFlag, ...parts) {
  return [prefix, userId, stockCode, refreshFlag ? "1" : "0", ...parts].join("|");
}

function parseWCustomId(customId) {
  const parts = String(customId || "").split("|");

  return {
    prefix: parts[0] || "",
    userId: parts[1] || "",
    stockCode: parts[2] || "",
    refreshFlag: parts[3] === "1",
    parts: parts.slice(4),
  };
}

function isComponentOwner(interaction, parsed) {
  const actualUserId = getInteractionUserId(interaction);
  return Boolean(parsed?.userId && actualUserId && parsed.userId === actualUserId);
}

function selectedBranchSummary(selectedKeys) {
  const names = selectedKeysToBrokerNames(selectedKeys).map(displayBrokerName);

  if (names.length === 0) {
    return "尚未選擇自訂分點";
  }

  return names.join("、");
}

function buildWarrantBranchModeData(stockCode, stockName, refreshNewsSummary, userId) {
  const refreshText = refreshNewsSummary
    ? "\n🔄 本次會重新抓新聞並重新產生本週重點。"
    : "\n📦 本次會優先使用當日快取；沒有快取才重新產生。";

  return {
    content:
      `✅ 已確認：\`${stockCode} ${stockName}\`\n` +
      "請選擇本次週報的精選分點資金流模式：\n" +
      `預設五分點：${DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES.join("、")}${refreshText}`,
    components: [
      {
        type: COMPONENT.ACTION_ROW,
        components: [
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SUCCESS,
            label: "產生預設五分點週報",
            custom_id: makeWCustomId("w5", userId, stockCode, refreshNewsSummary),
          },
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.PRIMARY,
            label: "改選分點",
            custom_id: makeWCustomId("wc", userId, stockCode, refreshNewsSummary),
          },
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SECONDARY,
            label: "取消",
            custom_id: makeWCustomId("wx", userId, stockCode, refreshNewsSummary),
          },
        ],
      },
    ],
  };
}

function buildWarrantBranchCategoryData(stockCode, refreshNewsSummary, userId, selectedKeys = []) {
  const state = encodeSelectedKeys(selectedKeys);

  return {
    content:
      `股票：\`${stockCode}\`\n` +
      "請選擇分點分類，再複選要納入「精選分點資金流」的分點。\n" +
      `目前選擇：${selectedBranchSummary(selectedKeys)}`,
    components: [
      {
        type: COMPONENT.ACTION_ROW,
        components: [
          {
            type: COMPONENT.STRING_SELECT,
            custom_id: makeWCustomId("wcat", userId, stockCode, refreshNewsSummary, state),
            placeholder: "選擇分點分類",
            min_values: 1,
            max_values: 1,
            options: BROKER_CATEGORIES.map((cat) => ({
              label: cat.label,
              value: cat.key,
              description: `查看${cat.label}分點`,
            })),
          },
        ],
      },
      {
        type: COMPONENT.ACTION_ROW,
        components: [
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SUCCESS,
            label: "確認產生週報",
            custom_id: makeWCustomId("wgo", userId, stockCode, refreshNewsSummary, state),
            disabled: selectedKeysToBrokerNames(selectedKeys).length === 0,
          },
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SECONDARY,
            label: "返回模式選擇",
            custom_id: makeWCustomId("wback", userId, stockCode, refreshNewsSummary),
          },
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SECONDARY,
            label: "清空選擇",
            custom_id: makeWCustomId("wclear", userId, stockCode, refreshNewsSummary),
            disabled: selectedKeysToBrokerNames(selectedKeys).length === 0,
          },
          {
            type: COMPONENT.BUTTON,
            style: BUTTON_STYLE.SECONDARY,
            label: "取消",
            custom_id: makeWCustomId("wx", userId, stockCode, refreshNewsSummary),
          },
        ],
      },
    ],
  };
}

function buildWarrantBranchSelectData(categoryKey, page, stockCode, refreshNewsSummary, userId, selectedKeys = []) {
  const category = getBrokerCategory(categoryKey);

  if (!category) {
    return buildWarrantBranchCategoryData(stockCode, refreshNewsSummary, userId, selectedKeys);
  }

  const brokers = category.brokers || [];
  const totalPages = Math.max(
    Math.ceil(brokers.length / BROKER_SELECT_PAGE_SIZE),
    1
  );

  const currentPage = clampPage(page, totalPages);
  const start = currentPage * BROKER_SELECT_PAGE_SIZE;
  const pageBrokers = brokers.slice(start, start + BROKER_SELECT_PAGE_SIZE);
  const selectedNames = new Set(selectedKeysToBrokerNames(selectedKeys));
  const state = encodeSelectedKeys(selectedKeys);

  const components = [];

  if (pageBrokers.length > 0) {
    components.push({
      type: COMPONENT.ACTION_ROW,
      components: [
        {
          type: COMPONENT.STRING_SELECT,
          custom_id: makeWCustomId("wbr", userId, stockCode, refreshNewsSummary, category.key, String(currentPage), state),
          placeholder: `複選${category.label}分點`,
          min_values: 0,
          max_values: Math.min(pageBrokers.length, 25),
          options: pageBrokers.map((broker) => ({
            label: displayBrokerName(broker),
            value: broker,
            description: "加入精選分點資金流",
            default: selectedNames.has(broker),
          })),
        },
      ],
    });
  }

  const navButtons = [
    {
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "返回分類",
      custom_id: makeWCustomId("wcats", userId, stockCode, refreshNewsSummary, state),
    },
  ];

  if (totalPages > 1) {
    navButtons.push({
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "上一頁",
      custom_id: makeWCustomId("wpage", userId, stockCode, refreshNewsSummary, category.key, String(currentPage - 1), state),
      disabled: currentPage <= 0,
    });

    navButtons.push({
      type: COMPONENT.BUTTON,
      style: BUTTON_STYLE.SECONDARY,
      label: "下一頁",
      custom_id: makeWCustomId("wpage", userId, stockCode, refreshNewsSummary, category.key, String(currentPage + 1), state),
      disabled: currentPage >= totalPages - 1,
    });
  }

  components.push({
    type: COMPONENT.ACTION_ROW,
    components: navButtons,
  });

  components.push({
    type: COMPONENT.ACTION_ROW,
    components: [
      {
        type: COMPONENT.BUTTON,
        style: BUTTON_STYLE.SUCCESS,
        label: "確認產生週報",
        custom_id: makeWCustomId("wgo", userId, stockCode, refreshNewsSummary, state),
        disabled: selectedKeysToBrokerNames(selectedKeys).length === 0,
      },
      {
        type: COMPONENT.BUTTON,
        style: BUTTON_STYLE.SECONDARY,
        label: "清空選擇",
        custom_id: makeWCustomId("wclear", userId, stockCode, refreshNewsSummary),
        disabled: selectedKeysToBrokerNames(selectedKeys).length === 0,
      },
      {
        type: COMPONENT.BUTTON,
        style: BUTTON_STYLE.SECONDARY,
        label: "取消",
        custom_id: makeWCustomId("wx", userId, stockCode, refreshNewsSummary),
      },
    ],
  });

  return {
    content:
      `股票：\`${stockCode}\`\n` +
      `分類：${category.label}｜第 ${currentPage + 1}/${totalPages} 頁\n` +
      `目前選擇：${selectedBranchSummary(selectedKeys)}`,
    components,
  };
}

function isWarrantRunCustomId(customId) {
  const prefix = String(customId || "").split("|")[0] || "";
  return prefix === "w5" || prefix === "wgo";
}

function buildBrokerCategoryButtonsData() {
  return {
    content: "請選擇要查詢的分點分類：",
    components: [
      {
        type: COMPONENT.ACTION_ROW,
        components: [
          {
            type: COMPONENT.STRING_SELECT,
            custom_id: "ww_category",
            placeholder: "選擇分點分類",
            min_values: 1,
            max_values: 1,
            options: BROKER_CATEGORIES.map((cat) => ({
              label: cat.label,
              value: cat.key,
              description: `查看${cat.label}分點`,
            })),
          },
        ],
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

  const userId = getInteractionUserId(interaction);

  if (!userId) {
    return reply("❌ 無法確認操作使用者，請重新輸入 `/w`。", true);
  }

  return replyData(
    buildWarrantBranchModeData(stockCode, stockName, refreshNewsSummary, userId),
    false
  );
}

async function handleComponent(interaction, env) {
  const customId = String(interaction?.data?.custom_id || "");

  if (customId === "ww_back") {
    return updateMessageData(buildBrokerCategoryButtonsData());
  }

  if (customId === "ww_category") {
    const categoryKey = String(interaction?.data?.values?.[0] || "").trim();

    return updateMessageData(buildBrokerSelectData(categoryKey, 0));
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

  const parsed = parseWCustomId(customId);

  if (["w5", "wc", "wcat", "wbr", "wgo", "wback", "wcats", "wpage", "wclear", "wx"].includes(parsed.prefix)) {
    if (!isComponentOwner(interaction, parsed)) {
      return reply("❌ 這個週報選單不是你建立的，請重新輸入 `/w`。", true);
    }
  }

  if (parsed.prefix === "w5" || parsed.prefix === "wgo") {
    return deferUpdateMessage();
  }

  if (parsed.prefix === "wc") {
    return updateMessageData(
      buildWarrantBranchCategoryData(parsed.stockCode, parsed.refreshFlag, parsed.userId, [])
    );
  }

  if (parsed.prefix === "wcat") {
    const selectedKeys = decodeSelectedKeys(parsed.parts[0] || "");
    const categoryKey = String(interaction?.data?.values?.[0] || "").trim();

    return updateMessageData(
      buildWarrantBranchSelectData(
        categoryKey,
        0,
        parsed.stockCode,
        parsed.refreshFlag,
        parsed.userId,
        selectedKeys
      )
    );
  }

  if (parsed.prefix === "wbr") {
    const categoryKey = parsed.parts[0] || "";
    const page = parsed.parts[1] || "0";
    const oldSelectedKeys = decodeSelectedKeys(parsed.parts[2] || "");
    const selectedValues = Array.isArray(interaction?.data?.values)
      ? interaction.data.values.map((x) => String(x || "").trim()).filter(Boolean)
      : [];
    const category = getBrokerCategory(categoryKey);

    if (!category) {
      return updateMessageData(
        buildWarrantBranchCategoryData(parsed.stockCode, parsed.refreshFlag, parsed.userId, oldSelectedKeys)
      );
    }

    const brokers = category.brokers || [];
    const totalPages = Math.max(
      Math.ceil(brokers.length / BROKER_SELECT_PAGE_SIZE),
      1
    );
    const currentPage = clampPage(page, totalPages);
    const start = currentPage * BROKER_SELECT_PAGE_SIZE;
    const pageBrokers = brokers.slice(start, start + BROKER_SELECT_PAGE_SIZE);
    const pageBrokerSet = new Set(pageBrokers);
    const oldSelectedNames = selectedKeysToBrokerNames(oldSelectedKeys);
    const nextSelectedNames = oldSelectedNames.filter((name) => !pageBrokerSet.has(name));

    for (const name of selectedValues) {
      if (!nextSelectedNames.includes(name)) {
        nextSelectedNames.push(name);
      }
    }

    const nextSelectedKeys = selectedKeysFromBrokerNames(nextSelectedNames);

    return updateMessageData(
      buildWarrantBranchSelectData(
        categoryKey,
        currentPage,
        parsed.stockCode,
        parsed.refreshFlag,
        parsed.userId,
        nextSelectedKeys
      )
    );
  }

  if (parsed.prefix === "wback") {
    const stockMap = await loadStockMap(env);
    const stockName = stockMap.get(parsed.stockCode) || parsed.stockCode;

    return updateMessageData(
      buildWarrantBranchModeData(parsed.stockCode, stockName, parsed.refreshFlag, parsed.userId)
    );
  }

  if (parsed.prefix === "wcats") {
    const selectedKeys = decodeSelectedKeys(parsed.parts[0] || "");

    return updateMessageData(
      buildWarrantBranchCategoryData(
        parsed.stockCode,
        parsed.refreshFlag,
        parsed.userId,
        selectedKeys
      )
    );
  }

  if (parsed.prefix === "wpage") {
    const categoryKey = parsed.parts[0] || "";
    const page = parsed.parts[1] || "0";
    const selectedKeys = decodeSelectedKeys(parsed.parts[2] || "");

    return updateMessageData(
      buildWarrantBranchSelectData(
        categoryKey,
        page,
        parsed.stockCode,
        parsed.refreshFlag,
        parsed.userId,
        selectedKeys
      )
    );
  }

  if (parsed.prefix === "wclear") {
    return updateMessageData(
      buildWarrantBranchCategoryData(parsed.stockCode, parsed.refreshFlag, parsed.userId, [])
    );
  }

  if (parsed.prefix === "wx") {
    return updateMessageData({
      content: "已取消產生權證週報。",
      components: [],
    });
  }

  return reply("❌ 不支援的互動元件。", true);
}

async function handleWarrantFlowSelectionAndEdit(interaction, env) {
  try {
    const parsed = parseWCustomId(interaction?.data?.custom_id || "");

    if (!isComponentOwner(interaction, parsed)) {
      await editOriginalResponseData(interaction, {
        content: "❌ 這個週報選單不是你建立的，請重新輸入 `/w`。",
        components: [],
      });
      return;
    }

    const stockCode = normalizeStockCode(parsed.stockCode);

    if (!isSingleStockCode(stockCode)) {
      await editOriginalResponseData(interaction, {
        content: "❌ 股票代號格式錯誤，請重新輸入 `/w`。",
        components: [],
      });
      return;
    }

    const stockMap = await loadStockMap(env);
    const stockName = stockMap.get(stockCode);

    if (!stockName) {
      await editOriginalResponseData(interaction, {
        content: `❌ 查無股票代號 \`${stockCode}\`，未執行週報。請確認 Google Sheet「快取_股票名稱」是否有此代號。`,
        components: [],
      });
      return;
    }

    const active = await hasActiveWorkflowRun(env);

    if (active.active) {
      await editOriginalResponseData(interaction, {
        content: "⏳ 目前已有週報產生中，請等上一檔完成後再查下一檔。",
        components: [],
      });
      return;
    }

    let selectedBranchFlowMode = "five";
    let selectedBranchFlowBranches = "五分點";
    let selectedText = DEFAULT_SELECTED_BRANCH_FLOW_BRANCHES.join("、");

    if (parsed.prefix === "wgo") {
      const selectedKeys = decodeSelectedKeys(parsed.parts[0] || "");
      const selectedNames = selectedKeysToBrokerNames(selectedKeys);

      if (selectedNames.length === 0) {
        await editOriginalResponseData(interaction, {
          content: "❌ 尚未選擇自訂分點，請重新輸入 `/w`。",
          components: [],
        });
        return;
      }

      selectedBranchFlowMode = "custom";
      selectedBranchFlowBranches = selectedNames.join(",");
      selectedText = selectedNames.map(displayBrokerName).join("、");
    }

    await triggerWorkflow(
      env,
      stockCode,
      parsed.refreshFlag,
      selectedBranchFlowMode,
      selectedBranchFlowBranches
    );

    const refreshText = parsed.refreshFlag
      ? "\n🔄 本次會重新抓新聞並重新產生本週重點。"
      : "\n📦 本次會優先使用當日快取；沒有快取才重新產生。";

    await editOriginalResponseData(interaction, {
      content:
        `✅ 已確認：\`${stockCode} ${stockName}\`\n` +
        `📌 精選分點資金流：${selectedText}\n` +
        `🔎 已成功送出查詢，權證週報正在產生中。${refreshText}`,
      components: [],
    });
  } catch (err) {
    await editOriginalResponseData(interaction, {
      content: `❌ 執行失敗：${err.message || err}`,
      components: [],
    });
  }
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
    const responseData = data?.data || {
      content: "✅ 指令已處理完成。",
      components: [],
    };
    const { flags, ...editableData } = responseData;

    await editOriginalResponseData(interaction, editableData);
  } catch (err) {
    await editOriginalResponseData(interaction, {
      content: `❌ 執行失敗：${err.message || err}`,
      components: [],
    });
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

      if (isWarrantRunCustomId(customId)) {
        const parsed = parseWCustomId(customId);

        if (isComponentOwner(interaction, parsed)) {
          ctx.waitUntil(handleWarrantFlowSelectionAndEdit(interaction, env));
        }

        return handleComponent(interaction, env);
      }

      if (customId.startsWith("ww_broker:")) {
        ctx.waitUntil(handleBrokerSelectionAndEdit(interaction, env));
        return handleComponent(interaction, env);
      }

      return handleComponent(interaction, env);
    }

    return reply("❌ 不支援的 Discord interaction type。", true);
  },
};
