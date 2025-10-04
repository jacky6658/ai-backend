#!/usr/bin/env python
# coding: utf-8

from flask import Flask, request, jsonify, session
from flask_cors import CORS
import google.generativeai as genai
import os
import json
from datetime import datetime, timedelta
import uuid
import sqlite3
from contextlib import contextmanager

# -------------------------------
# App & CORS
# -------------------------------
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here')

# 寬鬆 CORS 設定：前端才能直接呼叫
CORS(app, resources={r"/*": {"origins": "*"}})

# -------------------------------
# Gemini 初始化
# -------------------------------
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is not set in the environment variables")
genai.configure(api_key=api_key)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# -------------------------------
# 使用者等級
# -------------------------------
USER_LEVELS = {
    "beginner": "初學者",
    "intermediate": "中級用戶",
    "advanced": "進階用戶"
}

# -------------------------------
# SQLite 資料庫
# -------------------------------
DB_PATH = os.getenv("DB_PATH", "ai_assistant.db")

def init_db():
    """初始化資料庫（若表不存在則建立）"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                level TEXT DEFAULT 'beginner',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                preferred_structures TEXT,
                feedback_data TEXT,
                learning_progress TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)

        cursor.execute("""
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
    """取得或建立用戶；並更新 last_active"""
    if not user_id:
        user_id = str(uuid.uuid4())

    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = c.fetchone()
        if not user:
            c.execute('INSERT INTO users (id, level) VALUES (?, ?)', (user_id, 'beginner'))
            conn.commit()
            return {'id': user_id, 'level': 'beginner'}
        else:
            c.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?', (user_id,))
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

        return {
            "preferred_actions": actions,
            "common_topics": topics
        }

# -------------------------------
# 問候與基礎知識（摘要自短視頻.pdf）
# -------------------------------
def get_personalized_prompt(user_id, level, action, topic):
    """根據等級/歷史建立 system 規則與基礎結構說明（供模型遵循）"""
    history = get_user_history(user_id, 5)
    patterns = analyze_user_patterns(user_id)

    base_knowledge = """
# 短影音文案結構知識庫（節選）
- 總分總：開頭鉤子 → 條列重點(1/2/3) → 收束金句＋行動呼籲
- 解題式：痛點 → 解法 → 實操 → 價值觀收束（適合變現導向）
- 對比式：黃金開場 → 差異化賣點（≤5點）→ 為什麼要買
- 敘事式：事件 → 阻礙 → 轉折 → 感悟升華；語氣要堅定，少口頭禪
"""

    personalization = ""
    if history:
        recent_topics = [h['topic'] for h in history[:3]]
        personalization += f"\n\n# 個性化\n最近主題：{', '.join(recent_topics)}。"
        if patterns['preferred_actions']:
            most_used = patterns['preferred_actions'][0]['action']
            if most_used == 'copywriting':
                personalization += "你偏好多做文案，加入更多情緒與金句有助轉換。"
            elif most_used == 'scriptwriting':
                personalization += "你常做腳本，請強化分鏡與節奏（3~5鏡頭）。"

    # 依等級給予不同的指示口吻
    if action == 'copywriting':
        if level == 'beginner':
            guide = "使用「總分總」創作；150~200字；語言簡潔，結尾給 CTA。"
        elif level == 'intermediate':
            guide = "在總分總/解題式/對比式中擇一；200~300字；附上使用結構與理由。"
        else:
            guide = "綜合四種結構；300~400字；說明創作思路、技巧與預期效果。"
    else:  # scriptwriting
        if level == 'beginner':
            guide = "30~60秒；場景簡單；開場3~5秒抓注意；附拍攝提示。"
        elif level == 'intermediate':
            guide = "60~90秒；3~5鏡頭；附对白/旁白、音效配樂與剪輯要點。"
        else:
            guide = "90~120秒；完整分鏡與角色指導；視聽設計與後製策略；平台優化。"

    system_rules = f"""
你是短影音的文案/腳本教練。以堅定、清楚、可落地為準則，不要冗長口頭禪。
主題：{topic}
等級：{level}（{USER_LEVELS.get(level,'')}）
任務：{action}
請遵循：{guide}
{base_knowledge}
{personalization}
"""
    return system_rules

# -------------------------------
# 產生內容（支援多輪對話）
# -------------------------------
@app.route('/generate', methods=['POST'])
def generate_content():
    try:
        data = request.get_json(force=True) or {}
        topic = (data.get('topic') or '').strip()
        action = (data.get('action') or 'copywriting').strip()  # copywriting | scriptwriting
        user_level = (data.get('user_level') or 'beginner').strip()
        notes = (data.get('notes') or '').strip()
        msgs = data.get('messages') or []  # [{role:"user"|"assistant", content:"..."}]

        if not topic or not action:
            return jsonify({"error": "請提供主題(topic)與操作類型(action)"}), 400

        # user
        user_id = data.get('user_id') or session.get('user_id')
        if not user_id:
            user = get_or_create_user()
            user_id = user['id']
            session['user_id'] = user_id
        else:
            get_or_create_user(user_id)

        if user_level not in USER_LEVELS:
            user_level = 'beginner'

        # 模型與對話歷史
        model = genai.GenerativeModel(GEMINI_MODEL)

        system_prompt = get_personalized_prompt(user_id, user_level, action, topic)

        # 轉換前端 messages 為 Gemini 歷史
        history = [{"role": "user", "parts": [system_prompt]}]
        for m in msgs:
            role = "user" if m.get("role") == "user" else "model"
            history.append({"role": role, "parts": [m.get("content", "")]})

        chat = model.start_chat(history=history)

        # 本輪提示：延續上下文
        turn_prompt = f"[{action}] 主題：{topic}\n備註：{notes}\n請延續上下文產出最佳回應。"
        resp = chat.send_message(turn_prompt)

        # 兼容擷取文字
        content = getattr(resp, "text", None)
        if not content and getattr(resp, "candidates", None):
            try:
                content = resp.candidates[0].content.parts[0].text
            except Exception:
                content = ""

        content = content or "（AI沒有回覆內容）"

        # 存歷史
        save_conversation(
            user_id=user_id,
            topic=topic,
            action=action,
            user_level=user_level,
            prompt=turn_prompt,
            response=content
        )

        # **前端預期格式**
        return jsonify({
            "message": {"content": content},
            "user_id": user_id,
            "user_level": user_level,
            "user_level_name": USER_LEVELS[user_level],
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        return jsonify({"error": str(e), "message": {"content": ""}}), 500

# -------------------------------
# 回饋、歷史、等級、健康、分析
# -------------------------------
@app.route('/feedback', methods=['POST'])
def submit_feedback():
    data = request.get_json(force=True) or {}
    user_id = data.get('user_id') or session.get('user_id')
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400

    rating = data.get('rating')          # 1~5
    feedback_text = data.get('feedback_text', '')
    topic = data.get('topic')
    generated_content = data.get('generated_content')

    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO training_data (user_id, input_topic, generated_content, user_feedback, feedback_text)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, topic, generated_content, rating, feedback_text))
        conn.commit()

    return jsonify({"message": "感謝您的反饋！"}), 200

@app.route('/history', methods=['GET'])
def get_history():
    user_id = request.args.get('user_id') or session.get('user_id')
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400

    limit = int(request.args.get('limit', 10))
    history = get_user_history(user_id, limit)
    patterns = analyze_user_patterns(user_id)

    return jsonify({"history": history, "patterns": patterns}), 200

@app.route('/user-levels', methods=['GET'])
def get_user_levels():
    return jsonify(USER_LEVELS), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "3.1.0",
        "features": [
            "user_levels",
            "conversation_memory",
            "personalization",
            "feedback_system",
            "multi_turn_chat"
        ]
    }), 200

@app.route('/analytics', methods=['GET'])
def get_analytics():
    with get_db() as conn:
        c = conn.cursor()

        c.execute('SELECT COUNT(*) AS total_users FROM users')
        total_users = c.fetchone()['total_users']

        c.execute('SELECT COUNT(*) AS total_conversations FROM conversations')
        total_conversations = c.fetchone()['total_conversations']

        c.execute('SELECT level, COUNT(*) AS count FROM users GROUP BY level')
        level_distribution = [dict(r) for r in c.fetchall()]

        c.execute('SELECT action, COUNT(*) AS count FROM conversations GROUP BY action ORDER BY count DESC')
        popular_actions = [dict(r) for r in c.fetchall()]

        return jsonify({
            "total_users": total_users,
            "total_conversations": total_conversations,
            "level_distribution": level_distribution,
            "popular_actions": popular_actions
        }), 200

# -------------------------------
# 啟動
# -------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
