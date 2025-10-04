#!/usr/bin/env python
# coding: utf-8

"""
AI Backend (Gemini) — v3.2 (fixed)
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
            elif most_used == "scriptwriting":  # ← 修正：完整字串
                personalization += "你常做腳本，建議強化分鏡與節奏（3~5 鏡頭）。"

    if action == "copywriting":
        if level == "beginner":
            guide = "用『總分總』；150~200 字；語言簡潔，結尾給 CTA。"
        elif level == "intermediate":
            guide = "在 總分總/解題式/對比式 中擇一；200~300 字；附使用結構與理由。"
        else:
            guide = "綜合四種結構；300~400 字；說明創作思路、技巧與預期效果。"
    else:
        if level == "beginner":
            guide = "30~60 秒；場景簡單；開場 3~5 秒抓注意；附拍攝提示。"
        elif level == "intermediate":
            guide = "60~90 秒；3~5 鏡頭；附对白/旁白、配樂與剪輯要點。"
        else:
            guide = "90~120 秒；完整分鏡與角色指導；視聽設計/後製策略；平台優化。"

    system_rules = f"""
你是短影音的文案/腳本教練。語氣清晰、堅定、可落地，避免冗長口頭禪。
主題：{topic}
等級：{level}（{USER_LEVELS.get(level,'')}）
任務：{action}
請遵循：{guide}
{base_knowledge}
{personalization}
"""
    return system_rules


def _gemini_text_from_response(resp):
    text = getattr(resp, "text", None)
    if text:
        return text
    if getattr(resp, "candidates", None):
        try:
            return resp.candidates[0].content.parts[0].text
        except Exception:
            return ""
    return ""


# -------------------------------
# 產出（JSON，一次性）
# -------------------------------
@app.post("/generate")
def generate_content():
    try:
        data = request.get_json(force=True) or {}
        topic = (data.get("topic") or "").strip()
        action = (data.get("action") or "copywriting").strip()
        user_level = (data.get("user_level") or "beginner").strip()
        notes = (data.get("notes") or "").strip()
        msgs = data.get("messages") or []  # [{role:"user"|"assistant", content:"..."}]

        if not topic or not action:
            return jsonify({"error": "請提供主題(topic)與操作類型(action)"}), 400

        user_id = data.get("user_id") or session.get("user_id")
        if not user_id:
            user = get_or_create_user()
            user_id = user["id"]
            session["user_id"] = user_id
        else:
            get_or_create_user(user_id)

        if user_level not in USER_LEVELS:
            user_level = "beginner"

        model = genai.GenerativeModel(GEMINI_MODEL)

        system_prompt = get_personalized_prompt(user_id, user_level, action, topic)
        history = [{"role": "user", "parts": [system_prompt]}]
        for m in msgs[-8:]:
            role = "user" if m.get("role") == "user" else "model"
            history.append({"role": role, "parts": [m.get("content", "")]})

        chat = model.start_chat(history=history)
        turn_prompt = f"[{action}] 主題：{topic}\n備註：{notes}\n請延續上下文產出最佳回應。"
        resp = chat.send_message(turn_prompt)

        content = _gemini_text_from_response(resp) or "（AI沒有回覆內容）"

        save_conversation(
            user_id=user_id,
            topic=topic,
            action=action,
            user_level=user_level,
            prompt=turn_prompt,
            response=content,
        )

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
# 產出（SSE 串流，可選）
# -------------------------------
@app.get("/generate_stream")
def generate_stream():
    try:
        topic = (request.args.get("topic") or "").strip()
        action = (request.args.get("action") or "copywriting").strip()
        user_level = (request.args.get("user_level") or "beginner").strip()
        notes = (request.args.get("notes") or "").strip()

        user_id = request.args.get("user_id") or session.get("user_id")
        if not user_id:
            user = get_or_create_user()
            user_id = user["id"]
            session["user_id"] = user_id
        else:
            get_or_create_user(user_id)

        model = genai.GenerativeModel(GEMINI_MODEL)
        system_prompt = get_personalized_prompt(user_id, user_level, action, topic)

        chat = model.start_chat(history=[{"role": "user", "parts": [system_prompt]}])
        turn_prompt = f"[{action}] 主題：{topic}\n備註：{notes}\n請延續上下文產出最佳回應。"

        def event_stream():
            try:
                resp = chat.send_message(turn_prompt, stream=True)
                buffer = []
                for chunk in resp:
                    t = getattr(chunk, "text", "") or ""
                    if not t:
                        continue
                    buffer.append(t)
                    yield f'data: {{"delta": {repr(t)}}}\n\n'
                final = "".join(buffer)
                save_conversation(
                    user_id=user_id,
                    topic=topic,
                    action=action,
                    user_level=user_level,
                    prompt=turn_prompt,
                    response=final
                )
                yield 'data: {"done": true}\n\n'
            except Exception as e:
                yield f'data: {{"error": {repr(str(e))}}}\n\n'

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
        }
        return Response(event_stream(), headers=headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------
# 回饋、歷史、等級、健康、分析
# -------------------------------
@app.post("/feedback")
def submit_feedback():
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400

    rating = data.get("rating")
    feedback_text = data.get("feedback_text", "")
    topic = data.get("topic")
    generated_content = data.get("generated_content")

    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO training_data (user_id, input_topic, generated_content, user_feedback, feedback_text)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, topic, generated_content, rating, feedback_text))
        conn.commit()

    return jsonify({"message": "感謝您的反饋！"}), 200


@app.get("/history")
def get_history():
    user_id = request.args.get("user_id") or session.get("user_id")
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400

    limit = int(request.args.get("limit", 10))
    history = get_user_history(user_id, limit)
    patterns = analyze_user_patterns(user_id)

    return jsonify({"history": history, "patterns": patterns}), 200


@app.get("/user-levels")
def get_user_levels():
    return jsonify(USER_LEVELS), 200


@app.get("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "3.2.0-fixed",
        "features": [
            "user_levels",
            "conversation_memory",
            "personalization",
            "feedback_system",
            "multi_turn_chat",
            "sse_streaming"
        ]
    }), 200


@app.get("/analytics")
def get_analytics():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS total_users FROM users")
        total_users = c.fetchone()["total_users"]
        c.execute("SELECT COUNT(*) AS total_conversations FROM conversations")
        total_conversations = c.fetchone()["total_conversations"]
        c.execute("SELECT level, COUNT(*) AS count FROM users GROUP BY level")
        level_distribution = [dict(r) for r in c.fetchall()]
        c.execute("SELECT action, COUNT(*) AS count FROM conversations GROUP BY action ORDER BY count DESC")
        popular_actions = [dict(r) for r in c.fetchall()]
        return jsonify({
            "total_users": total_users,
            "total_conversations": total_conversations,
            "level_distribution": level_distribution,
            "popular_actions": popular_actions
        }), 200


# -------------------------------
# （可選）真正的 .docx 匯出（需 python-docx）
# -------------------------------
@app.post("/export-docx")
def export_docx():
    try:
        from docx import Document
        from docx.shared import Pt

        data = request.get_json(force=True) or {}
        title = data.get("title", "短影片文案草稿")
        topic = data.get("topic", "未命名")
        content = data.get("content", "")

        doc = Document()
        h = doc.add_heading(title, level=1)
        h.runs[0].font.size = Pt(22)
        p = doc.add_paragraph()
        r = p.add_run(f"主題：{topic}")
        r.font.size = Pt(12)
        doc.add_paragraph("")  # 空行
        for line in str(content).split("\n"):
            doc.add_paragraph(line)

        from io import BytesIO
        buf = BytesIO()
        doc.save(buf); buf.seek(0)

        return app.response_class(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="短影片文案_{topic}.docx"',
                "Access-Control-Expose-Headers": "Content-Disposition"
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------
# 啟動
# -------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

