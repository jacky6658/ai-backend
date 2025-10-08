FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 基本套件
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# 建立 DB 目錄（給 SQLite 用）
RUN mkdir -p /data && chmod 777 /data

# 先安裝 requirements
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 放入後端程式
COPY app.py /app/
COPY knowledge_text_loader.py /app/

# 放知識庫（目前你的檔案在 data/data/kb.txt）
RUN mkdir -p /data
COPY data/data/kb.txt /data/kb.txt
ENV KNOWLEDGE_TXT_PATH=/data/kb.txt

# 你的環境變數（保持和 app.py 預設一致）
ENV GEMINI_MODEL=gemini-1.5-flash
ENV DB_PATH=/data/script_generation.db

EXPOSE 8080

# 啟動服務（app.py 內的 FastAPI 物件叫 app）
CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8080"]
