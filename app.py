#!/usr/bin/env python
# coding: utf-8

from flask import Flask, request, jsonify, session, Response
from flask_cors import CORS
import google.generativeai as genai
import os, uuid, sqlite3
from contextlib import contextmanager
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ✅ 設定 Gemini
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("⚠️ 請設定 GEMINI_API_KEY")
genai.configure(api_key=api_key)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ✅ SQLite 初始化
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
        )""")
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

# ✅ Gemini 產文
@app.post("/generate")
def generate_content():
    try:
        data = request.get_json(force=True)
        topic = data.get("topic", "").strip()
        action = data.get("action", "copywriting")
        user_level = data.get("user_level", "beginner")
        user_id = session.get("user_id") or str(uuid.uuid4())
        session["user_id"] = user_id

        model = genai.GenerativeModel(GEMINI_MODEL)
        chat = model.start_chat(history=[])
        prompt = f"以短影音文案教練身分，針對主題「{topic}」與行為「{action}」，用{user_level}等級的風格生成內容。"
        resp = chat.send_message(prompt)
        result = getattr(resp, "text", "")

        save_conversation(user_id, topic, action, user_level, prompt, result)

        return jsonify({
            "message": {"content": result},
            "user_id": user_id,
            "user_level": user_level,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ 匯出 Word
@app.post("/export-docx")
def export_docx():
    try:
        from docx import Document
        from io import BytesIO

        data = request.get_json(force=True)
        content = data.get("content", "")
        title = data.get("title", "AI文案")

        doc = Document()
        doc.add_heading(title, 0)
        for line in content.split("\n"):
            doc.add_paragraph(line)

        buf = BytesIO()
        doc.save(buf)
        buf.seek(0)

        return app.response_class(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename={title}.docx"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ 健康檢查
@app.get("/health")
def health_check():
    return jsonify({"status": "ok", "model": GEMINI_MODEL, "time": datetime.now().isoformat()})

init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
