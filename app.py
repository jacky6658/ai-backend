#!/usr/bin/env python
# coding: utf-8

"""
AI Backend (Gemini) — v3.2
- /generate：多輪對話（JSON）
- /generate_stream：SSE 串流（可選）
- /export-docx：真正 .docx 下載（可選，需 python-docx）
- /feedback, /history, /analytics, /health
"""

from flask import Flask, request, jsonify, session, Response
from flask_cors import CORS
import google.generativeai as genai
import os
import uuid
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# -------------------------------
# App & CORS
# -------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key-here")
# 開放所有來源，避免前端被 CORS 擋
CORS(app, resources={r"/*": {"origins": "*"}})

# -------------------------------
# Gemini 初始化
# -------------------------------
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is not set")
genai.configure(api_key=api_key)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# -------------------------------
# 常數與 SQLite
# -------------------------------
USER_LEVELS = {
    "beginner": "初學者",
    "intermediate": "中級用戶",
    "advanced": "進階用戶",
}
DB_PATH = os.getenv("DB_PATH", "ai_assistant.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                level TEXT DEFAULT 'beginner',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                topic TEXT,
                action TEXT,
                user_level TEXT,
                prompt TEXT,
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                preferred_structures TEXT,
                feedback_data TEXT,
                learning_progress TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS training_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                input_topic TEXT,
                generated_content TEXT,
                user_feedback INTEGER,
                feedback_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
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


def get_or_create_user(user_id=None):
    """取得或建立用戶；更新最後活動時間"""
    if not user_id:
        user_id = str(uuid.uuid4())
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = c.fetchone()
        if not user:
            c.execute("INSERT INTO users (id, level) VALUES (?, ?)", (user_id, "beginner"))
            conn.commit()
            return {"id": user_id, "level": "beginner"}
        else:
            c.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
            conn.commit()
            return dict(user)


def save_conversation(user_id, topic, action, user_level, prompt, response):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO conversations (user_id, topic, action, user_level, prompt, response)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, topic, action, user_level, prompt, response))
        conn.commit()


def get_user_history(user_id, limit=10):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM conversations
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in c.fetchall()]


def analyze_user_patterns(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT action, COUNT(*) as count
            FROM conversations
            WHERE user_id = ?
            GROUP BY action
            ORDER BY count DESC
        """, (user_id,))
        actions = [dict(r) for r in c.fetchall()]
        c.execute("""
            SELECT topic, COUNT(*) as count
            FROM conversations
            WHERE user_id = ?
            GROUP BY topic
            ORDER BY count DESC
            LIMIT 5
        """, (user_id,))
        topics = [dict(r) for r in c.fetchall()]
        return {"preferred_actions": actions, "common_topics": topics}

# -------------------------------
# Prompt（以你的短視頻知識為基底）
# -------------------------------
def get_personalized_prompt(user_id, level, action, topic):
    history = get_user_history(user_id, 5)
    patterns = analyze_user_patterns(user_id)

    base_knowledge = """
# 短影音文案結構（摘要）
- 總分總：開頭鉤子 → 條列重點(1/2/3) → 金句收束＋CTA
- 解題式：痛點 → 解法 → 實操 → 價值觀收束（偏變現）
- 對比式：黃金開場 → 差異化賣點 → 為什麼要買
- 敘事式：事件 → 阻礙 → 轉折 → 感悟升華（少口頭禪、語氣要堅定）
"""

    personalization = ""
    if history:
        recent_topics = ", ".join([h["topic"] for h in history[:3]])
        personalization += f"\n\n# 個人化\n最近主題：{recent_topics}。"
        if patterns["preferred_actions"]:
            most_used = patterns["preferred_actions"][0]["action"]
            if most_used == "copywriting":
                personalization += "你偏好多做文案，建議加強情緒與金句。"
            elif most_used == "scriptwrit_
