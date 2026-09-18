# 艾斯 AI Discord Bot（Railway 獨立 Service）
# Build context 必須是 repo 根目錄：Bot 要載入根目錄的週報主程式與種子資料（*.csv.gz）。
# 只跑 ace_ai/discord_ai_bot.py，產生回答圖片、不啟動 API，也不影響 GitHub Actions。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/app/.cache/matplotlib

RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY ace_ai/requirements.txt /app/ace_ai/requirements.txt
# 在 ace_ai/ 目錄內安裝：requirements.txt 裡的 -r ../requirements.txt 不論 pip 以「檔案位置」或
# 「目前目錄」解析都會指到 /app/requirements.txt（pip 26.2 以目前目錄解析，曾找成 /requirements.txt）。
# 不再每次升級 pip，避免新版 pip 行為改變讓部署失敗。
WORKDIR /app/ace_ai
RUN pip install --no-cache-dir -r requirements.txt
WORKDIR /app

COPY . /app
RUN mkdir -p /app/.cache/matplotlib

CMD ["python", "-u", "ace_ai/discord_ai_bot.py"]
