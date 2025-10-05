FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/
# 預設環境變數（可被平台覆蓋）
ENV GEMINI_MODEL=gemini-1.5-flash
ENV DB_PATH=/app/script_generation.db

EXPOSE 8000
# 注意：此行需與模組名稱一致 -> app:app
CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8000"]
