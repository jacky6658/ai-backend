FROM python:3.11-slim

# 設定環境變數
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        gcc \
        python3-dev && \
    rm -rf /var/lib/apt/lists/*

# 升級 pip
RUN python -m pip install --upgrade pip

# 複製並安裝 Python 依賴
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 驗證安裝
RUN python -c "import fastapi; print('FastAPI version:', fastapi.__version__)"

# 複製測試應用
COPY app_test.py /app/app.py

# 建立必要目錄
RUN mkdir -p /data && chmod 777 /data

# 設定環境變數
ENV DB_PATH=/data/test.db
ENV GEMINI_MODEL=gemini-2.5-flash

EXPOSE 8080

# 啟動服務
CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8080"]
