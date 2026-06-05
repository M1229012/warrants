import "dotenv/config";

const { DISCORD_APPLICATION_ID, DISCORD_BOT_TOKEN, DISCORD_GUILD_ID } = process.env;

if (!DISCORD_APPLICATION_ID || !DISCORD_BOT_TOKEN) {
  console.error("缺少 DISCORD_APPLICATION_ID 或 DISCORD_BOT_TOKEN。");
  process.exit(1);
}

const commands = [
  {
    name: "w",
    description: "產生單一股票的權證週報",
    options: [
      {
        type: 3,
        name: "stock",
        description: "股票代號，例如 2408",
        required: true,
      },
    ],
  },
  {
    name: "ww",
    description: "強制重新抓取資料並產生單一股票的權證週報",
    options: [
      {
        type: 3,
        name: "stock",
        description: "股票代號，例如 2408",
        required: true,
      },
    ],
  },
];

const endpoint = DISCORD_GUILD_ID
  ? `https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/guilds/${DISCORD_GUILD_ID}/commands`
  : `https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/commands`;

for (const command of commands) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: {
      Authorization: `Bot ${DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(command),
  });

  const text = await resp.text();

  if (!resp.ok) {
    console.error(`註冊 Slash Command 失敗：/${command.name}｜HTTP ${resp.status}`);
    console.error(text);
    process.exit(1);
  }

  console.log(`Slash Command 註冊成功：/${command.name}`);
  console.log(text);
}
