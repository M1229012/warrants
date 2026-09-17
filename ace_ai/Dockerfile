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
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/ace_ai/requirements.txt

COPY . /app
RUN mkdir -p /app/.cache/matplotlib

CMD ["python", "-u", "ace_ai/discord_ai_bot.py"]
