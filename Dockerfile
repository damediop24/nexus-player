FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \

    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir playwright
RUN playwright install --with-deps chromium

COPY . .

WORKDIR /app/backend

RUN mkdir -p /data
ENV DATABASE_PATH=/data/nexus.db
ENV PORT=8899
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/status" || exit 1

CMD ["python", "app.py"]