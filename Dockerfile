FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 基本系統套件（憑證/時區常見）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# 先安裝依賴
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 建立「可寫」資料目錄，給 SQLite 用
RUN mkdir -p /data && chmod -R 777 /data

# 放進後端程式
COPY app.py /app/

# 預設模型，可在 Zeabur 環境變數覆蓋
ENV GEMINI_MODEL=gemini-1.5-flash
# 關鍵：把 DB_PATH 指到可寫的 /data
ENV DB_PATH=/data/script_generation.db

EXPOSE 8000

# 讓雲平台 PORT 環境變數生效（Zeabur 會注入）
CMD ["sh", "-c", "uvicorn app:app --host=0.0.0.0 --port=${PORT:-8000}"]
