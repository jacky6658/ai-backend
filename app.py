#!/usr/bin/env python
# coding: utf-8

import os
import uuid
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from flask import Flask, request, jsonify, session
from flask_cors import CORS
import google.generativeai as genai

# ------------------------------------------------------------------------------
# 基礎設定：先宣告 app，再定義所有路由（避免 NameError）
# ------------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ------------------------------------------------------------------------------
# Gemini 設定
# ------------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("⚠️ 請設定環境變數 GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ------------------------------------------------------------------------------
# SQLite
# ------------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "ai_assistant.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            topic TEXT,
            action TEXT,
            user_level TEXT,
            prompt TEXT,
            response TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def save_conversation(user_id, topic, action, user_level, prompt, response):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO conversations
                     (user_id, topic, action, user_level, prompt, response)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (user_id, topic, action, user_level, prompt, response))
        conn.commit()

# ------------------------------------------------------------------------------
# 健康檢查
# ------------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "model": GEMINI_MODEL,
        "time": datetime.now().isoformat()
    })

# ------------------------------------------------------------------------------
# 主要 API
# ------------------------------------------------------------------------------
@app.post("/generate")
def generate():
    try:
        data = request.get_json(force=True) or {}
        topic = (data.get("topic") or "").strip()
        action = (data.get("action") or "copywriting").strip()
        user_level = (data.get("user_level") or "beginner").strip()

        if not topic:
            return jsonify({"error": "請提供 topic"}), 400
        if len(topic) > 300:
            return jsonify({"error": "topic 過長，請精簡至 300 字內"}), 400

        user_id = session.get("user_id") or ("u_" + uuid.uuid4().hex[:10])
        session["user_id"] = user_id

        # 控制輸出長度，避免回覆過長拖太久
        max_tok = 800 if user_level == "advanced" else (700 if user_level == "intermediate" else 600)

        system_hint = (
            "你是短影音文案/腳本教練，輸出精煉、結構清晰，避免冗語。"
            "請直接給可用段落與小標。"
        )
        user_prompt = (
            f"主題：{topic}\n"
            f"任務：{('腳本設計' if action=='scriptwriting' else '文案撰寫')}\n"
            f"對象等級：{user_level}\n"
            "請用繁體中文輸出。"
        )

        model = genai.GenerativeModel(
            GEMINI_MODEL,
            generation_config={
                "max_output_tokens": max_tok,
                "candidate_count": 1,
                "temperature": 0.8
            }
        )

        def run_once():
            chat = model.start_chat(history=[])
            return chat.send_message(f"{system_hint}\n\n{user_prompt}")

        try:
            resp = run_once()
        except Exception:
            # 偶發錯誤再試一次
            resp = run_once()

        result = getattr(resp, "text", None) or ""
        if not result:
            return jsonify({"error": "模型沒有回覆內容"}), 502

        save_conversation(user_id, topic, action, user_level, user_prompt, result)
        return jsonify({
            "message": {"content": result},
            "user_id": user_id,
            "user_level": user_level,
            "timestamp": datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({"error": f"server_error: {str(e)}"}), 500

# ------------------------------------------------------------------------------
# 啟動（Zeabur/Heroku 類平臺用 gunicorn 載入：app:app）
# ------------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
