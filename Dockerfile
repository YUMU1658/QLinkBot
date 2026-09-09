FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # 抖音浏览器直取所需的 Chromium（含系统依赖）
    && python -m playwright install --with-deps chromium

COPY qlinkbot ./qlinkbot

RUN useradd --create-home bot \
    && mkdir -p downloads \
    && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "qlinkbot.main"]
