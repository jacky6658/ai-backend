#!/usr/bin/env python
# coding: utf-8

from flask import Flask, request, jsonify, session
from flask_cors import CORS
import google.generativeai as genai
import os, uuid, sqlite3
from contextlib import contextmanager
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ── Gemini 設定 ────────────────────────────────────────────────────────────────
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("⚠️ 請設定 GEMINI_API_KEY")
genai.configure(api_key=api_key)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── SQLite ────────────────────────────────────────────────────────────────────
DB_PATH = "ai_assistant.db"

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

# ── API ───────────────────────────────────────────────────────────────────────
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

        prompt = (
            f"以短影音文案教練身分，針對主題「{topic}」與行為「{action}」，"
            f"用{user_level}等級的風格生成內容。回覆請使用繁體中文。"
        )

        model = genai.GenerativeModel(GEMINI_MODEL)
        chat = model.start_chat(history=[])
        resp = chat.send_message(prompt)
        result = getattr(resp, "text", None) or ""

        if not result:
            return jsonify({"error": "模型沒有回覆內容"}), 502

        save_conversation(user_id, topic, action, user_level, prompt, result)

        return jsonify({
            "message": {"content": result},
            "user_id": user_id,
            "user_level": user_level,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        # 統一錯誤格式，前端可讀
        return jsonify({"error": f"server_error: {str(e)}"}), 500

@app.get("/health")
def health():
    return jsonify({"status": "ok", "model": GEMINI_MODEL, "time": datetime.now().isoformat()})

init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
