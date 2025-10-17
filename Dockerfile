FROM python:3.11-slim

WORKDIR /app

# 安裝系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata && \
    rm -rf /var/lib/apt/lists/*

# 建立必要目錄
RUN mkdir -p /data && chmod 777 /data

# 複製並安裝 Python 依賴
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 驗證安裝
RUN python -c "import fastapi; print('FastAPI installed successfully')"

# 複製應用程式碼
COPY app.py /app/
COPY memory.py /app/
COPY rag.py /app/
COPY knowledge_loader.py /app/
COPY providers.py /app/
COPY chat_stream.py /app/
COPY knowledge_text_loader.py /app/
COPY points_system.py /app/
COPY points_routes.py /app/
COPY points_integration.py /app/

# 複製 admin 資料夾
COPY admin/ /app/admin/

# 放知識庫進容器
COPY data/ /data/

# 設定環境變數
ENV DB_PATH=/data/three_agents_system.db
ENV GEMINI_MODEL=gemini-2.5-flash
ENV KNOWLEDGE_TXT_PATH=/data/kb.txt

EXPOSE 8080

# 啟動服務
CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8080"]
