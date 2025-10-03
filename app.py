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

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here')
CORS(app, supports_credentials=True)  # 啟用 CORS 並支援 credentials

# 從環境變數讀取 API 金鑰
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is not set in the environment variables")
genai.configure(api_key=api_key)

# 用戶等級定義
USER_LEVELS = {
    "beginner": "初學者",
    "intermediate": "中級用戶", 
    "advanced": "進階用戶"
}

# 資料庫初始化
def init_db():
    """初始化資料庫"""
    with sqlite3.connect('ai_assistant.db') as conn:
        cursor = conn.cursor()
        
        # 用戶表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                level TEXT DEFAULT 'beginner',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 對話歷史表
        cursor.execute('''
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
        ''')
        
        # 用戶偏好表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                preferred_structures TEXT,
                feedback_data TEXT,
                learning_progress TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        # 訓練數據表
        cursor.execute('''
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
        ''')
        
        conn.commit()

@contextmanager
def get_db():
    """資料庫連接上下文管理器"""
    conn = sqlite3.connect('ai_assistant.db')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_or_create_user(user_id=None):
    """獲取或創建用戶"""
    if not user_id:
        user_id = str(uuid.uuid4())
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
        
        if not user:
            cursor.execute(
                'INSERT INTO users (id, level) VALUES (?, ?)',
                (user_id, 'beginner')
            )
            conn.commit()
            return {'id': user_id, 'level': 'beginner'}
        else:
            # 更新最後活動時間
            cursor.execute(
                'UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?',
                (user_id,)
            )
            conn.commit()
            return dict(user)

def save_conversation(user_id, topic, action, user_level, prompt, response):
    """保存對話記錄"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO conversations (user_id, topic, action, user_level, prompt, response)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, topic, action, user_level, prompt, response))
        conn.commit()

def get_user_history(user_id, limit=10):
    """獲取用戶歷史對話"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM conversations 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]

def analyze_user_patterns(user_id):
    """分析用戶使用模式"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 獲取用戶最常用的功能
        cursor.execute('''
            SELECT action, COUNT(*) as count 
            FROM conversations 
            WHERE user_id = ? 
            GROUP BY action 
            ORDER BY count DESC
        ''', (user_id,))
        actions = cursor.fetchall()
        
        # 獲取用戶最常用的主題類型
        cursor.execute('''
            SELECT topic, COUNT(*) as count 
            FROM conversations 
            WHERE user_id = ? 
            GROUP BY topic 
            ORDER BY count DESC 
            LIMIT 5
        ''', (user_id,))
        topics = cursor.fetchall()
        
        return {
            'preferred_actions': [dict(row) for row in actions],
            'common_topics': [dict(row) for row in topics]
        }

def get_personalized_prompt(user_id, level, action, topic):
    """根據用戶歷史生成個性化提示詞"""
    
    # 獲取用戶歷史
    history = get_user_history(user_id, 5)
    patterns = analyze_user_patterns(user_id)
    
    # 基礎知識庫
    base_knowledge = """
    # 短影音文案結構知識庫

    ## 結構一：總分總結構
    這是一種經典的論述結構，邏輯清晰，容易讓觀眾快速抓住重點。
    - 節奏: 開頭 -> 總結 -> 分述 -> 總結
    - 開頭 (金句/問句/懸念): 用一句話抓住用戶注意力
    - 中間 (分述): 條列式說明，通常用數字引導，如 1, 2, 3
    - 結尾 (變現關鍵): 總結金句，並給出明確的行動指令

    ## 結構二：解題式結構
    直接拋出用戶痛點，並給出解決方案，信任感強，適合知識型、技能型內容。
    - 節奏: 拋出痛點 -> 給出方案 -> 實操演練 -> 輸出價值觀

    ## 結構三：對比式結構
    透過前後的巨大反差來製造衝擊力，適用於美業、改造、技能提升等領域。
    - 節奏: 黃金開場 -> 溝通細節 -> 大改變

    ## 結構四：敘事式結構 (故事結構)
    透過講故事的方式引發情感共鳴，是黏性最強的文案結構。
    - 節奏: 事件 -> 阻礙 -> 轉折意外 -> 感悟升華
    """
    
    # 個性化元素
    personalization = ""
    if history:
        recent_topics = [h['topic'] for h in history[:3]]
        personalization += f"\n\n# 個性化建議\n根據您最近的創作主題：{', '.join(recent_topics)}，"
        
        if patterns['preferred_actions']:
            most_used = patterns['preferred_actions'][0]['action']
            if most_used == 'copywriting':
                personalization += "您似乎更偏好文案創作，建議融入更多情感元素。"
            elif most_used == 'scriptwriting':
                personalization += "您經常創作腳本，建議注重視覺呈現和節奏控制。"
    
    # 根據等級和動作生成提示詞
    if action == 'copywriting':
        if level == 'beginner':
            return f"""
            你是一個專業的文案創作助手。請根據以下主題為初學者用戶生成一段簡單易懂、吸引人的行銷文案。

            主題：{topic}

            請使用「總分總結構」來創作：
            1. 開頭：用一句吸引人的話或問句開始
            2. 中間：用3個要點來說明
            3. 結尾：用一句總結並呼籲行動

            要求：
            - 語言簡潔明瞭，避免過於複雜的詞彙
            - 內容要有吸引力但不誇大
            - 適合社群媒體分享
            - 字數控制在150-200字之間

            {base_knowledge}
            {personalization}
            """
        elif level == 'intermediate':
            return f"""
            你是一個專業的文案創作助手。請根據以下主題為中級用戶生成一段專業的行銷文案。

            主題：{topic}

            請從以下結構中選擇最適合的一種來創作：
            1. 總分總結構：適合邏輯性強的內容
            2. 解題式結構：適合解決問題的內容
            3. 對比式結構：適合展示變化的內容

            要求：
            - 內容要有深度和說服力
            - 包含情感元素和理性分析
            - 適合多平台使用
            - 字數控制在200-300字之間
            - 請在文案後說明使用了哪種結構及原因

            {base_knowledge}
            {personalization}
            """
        else:  # advanced
            return f"""
            你是一個頂級的文案創作專家。請根據以下主題為進階用戶生成一段高水準的行銷文案。

            主題：{topic}

            請綜合運用所有四種結構的精華，創作一段具有以下特質的文案：
            1. 強烈的情感共鳴
            2. 清晰的邏輯脈絡
            3. 獨特的創意角度
            4. 精準的目標受眾定位
            5. 有效的行動呼籲

            要求：
            - 展現專業的文案技巧和創意思維
            - 考慮不同平台的特性和受眾
            - 包含心理學和行銷學原理的運用
            - 字數控制在300-400字之間
            - 請詳細說明創作思路、使用的技巧和預期效果

            {base_knowledge}
            {personalization}
            """
    
    elif action == 'scriptwriting':
        if level == 'beginner':
            return f"""
            你是一個專業的短影音腳本創作助手。請根據以下主題為初學者用戶生成一個簡單易拍的短影音腳本。

            主題：{topic}

            請創作一個30-60秒的短影音腳本，包含：
            1. 場景設定（簡單易實現）
            2. 開場白（3-5秒抓住注意力）
            3. 主要內容（20-40秒）
            4. 結尾呼籲（5-10秒）

            要求：
            - 拍攝難度低，適合新手
            - 道具需求簡單
            - 語言自然流暢
            - 包含具體的拍攝提示

            {base_knowledge}
            {personalization}
            """
        elif level == 'intermediate':
            return f"""
            你是一個專業的短影音腳本創作助手。請根據以下主題為中級用戶生成一個專業的短影音腳本。

            主題：{topic}

            請創作一個60-90秒的短影音腳本，包含：
            1. 詳細場景設定和道具清單
            2. 分鏡頭設計（至少3-5個鏡頭）
            3. 對白和旁白
            4. 音效和配樂建議
            5. 剪輯要點

            要求：
            - 運用專業的影片製作技巧
            - 考慮視覺效果和節奏感
            - 適合中等製作預算
            - 包含拍攝和後製建議

            {base_knowledge}
            {personalization}
            """
        else:  # advanced
            return f"""
            你是一個頂級的短影音腳本創作專家。請根據以下主題為進階用戶生成一個高品質的短影音腳本。

            主題：{topic}

            請創作一個90-120秒的專業級短影音腳本，包含：
            1. 完整的創意概念和故事架構
            2. 詳細的分鏡頭腳本（包含鏡頭語言）
            3. 角色設定和表演指導
            4. 專業的視聽設計
            5. 後製特效和調色建議
            6. 平台優化策略

            要求：
            - 展現電影級的製作思維
            - 融合最新的短影音趨勢
            - 考慮商業價值和藝術性
            - 提供完整的製作流程指南
            - 包含數據分析和優化建議

            {base_knowledge}
            {personalization}
            """

@app.route('/generate', methods=['POST'])
def generate_content():
    data = request.get_json()
    if not data or 'topic' not in data or 'action' not in data:
        return jsonify({"error": "請提供主題和操作類型"}), 400

    topic = data['topic']
    action = data['action']
    user_level = data.get('user_level', 'beginner')
    user_id = data.get('user_id') or session.get('user_id')
    
    # 獲取或創建用戶
    if not user_id:
        user = get_or_create_user()
        user_id = user['id']
        session['user_id'] = user_id
    else:
        user = get_or_create_user(user_id)
    
    # 驗證用戶等級
    if user_level not in USER_LEVELS:
        user_level = 'beginner'

    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # 根據用戶歷史生成個性化提示詞
        prompt = get_personalized_prompt(user_id, user_level, action, topic)
        
        response = model.generate_content(prompt)
        result = response.text
        
        # 保存對話記錄
        save_conversation(user_id, topic, action, user_level, prompt, result)

        return jsonify({
            "result": result,
            "user_id": user_id,
            "user_level": user_level,
            "user_level_name": USER_LEVELS[user_level],
            "timestamp": datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/feedback', methods=['POST'])
def submit_feedback():
    """提交用戶反饋"""
    data = request.get_json()
    user_id = data.get('user_id') or session.get('user_id')
    
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400
    
    rating = data.get('rating')  # 1-5 星評分
    feedback_text = data.get('feedback_text', '')
    topic = data.get('topic')
    generated_content = data.get('generated_content')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO training_data (user_id, input_topic, generated_content, user_feedback, feedback_text)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, topic, generated_content, rating, feedback_text))
        conn.commit()
    
    return jsonify({"message": "感謝您的反饋！"})

@app.route('/history', methods=['GET'])
def get_history():
    """獲取用戶歷史"""
    user_id = request.args.get('user_id') or session.get('user_id')
    
    if not user_id:
        return jsonify({"error": "用戶ID不存在"}), 400
    
    limit = int(request.args.get('limit', 10))
    history = get_user_history(user_id, limit)
    patterns = analyze_user_patterns(user_id)
    
    return jsonify({
        "history": history,
        "patterns": patterns
    })

@app.route('/user-levels', methods=['GET'])
def get_user_levels():
    """獲取所有用戶等級"""
    return jsonify(USER_LEVELS)

@app.route('/health', methods=['GET'])
def health_check():
    """健康檢查端點"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "3.0.0",
        "features": ["user_levels", "conversation_memory", "personalization", "feedback_system"]
    })

@app.route('/analytics', methods=['GET'])
def get_analytics():
    """獲取系統分析數據"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 總用戶數
        cursor.execute('SELECT COUNT(*) as total_users FROM users')
        total_users = cursor.fetchone()['total_users']
        
        # 總對話數
        cursor.execute('SELECT COUNT(*) as total_conversations FROM conversations')
        total_conversations = cursor.fetchone()['total_conversations']
        
        # 用戶等級分布
        cursor.execute('SELECT level, COUNT(*) as count FROM users GROUP BY level')
        level_distribution = [dict(row) for row in cursor.fetchall()]
        
        # 最受歡迎的功能
        cursor.execute('SELECT action, COUNT(*) as count FROM conversations GROUP BY action ORDER BY count DESC')
        popular_actions = [dict(row) for row in cursor.fetchall()]
        
        return jsonify({
            "total_users": total_users,
            "total_conversations": total_conversations,
            "level_distribution": level_distribution,
            "popular_actions": popular_actions
        })

# 初始化資料庫
init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
