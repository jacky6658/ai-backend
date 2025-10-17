FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 安裝基本套件
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# 建立 DB 目錄（給 SQLite 用）
RUN mkdir -p /data && chmod 777 /data

# 安裝 Python 套件
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY app.py /app/
COPY chat_stream.py /app/
COPY knowledge_text_loader.py /app/
COPY points_system.py /app/
COPY points_routes.py /app/
COPY points_integration.py /app/

# 放知識庫進容器
COPY data/kb.txt /data/kb.txt

# 設定環境變數
ENV GEMINI_MODEL=gemini-2.5-flash
ENV DB_PATH=/data/three_agents_system.db
ENV KNOWLEDGE_TXT_PATH=/data/kb.txt

EXPOSE 8080

# 啟動服務
CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8080"]
