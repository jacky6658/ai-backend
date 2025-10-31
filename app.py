import os
import json
import hashlib
import sqlite3
import secrets
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Iterable
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse, HTMLResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

import google.generativeai as genai

# PostgreSQL 支援
try:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("WARNING: psycopg2 未安裝，將使用 SQLite")


# 導入新的記憶系統模組
from memory import stm
from prompt_builder import build_enhanced_prompt, format_memory_for_display


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    message: str
    platform: Optional[str] = None
    profile: Optional[str] = None
    history: Optional[List[ChatMessage]] = None
    topic: Optional[str] = None
    style: Optional[str] = None
    duration: Optional[str] = "30"
    user_id: Optional[str] = None  # 新增用戶ID


class UserProfile(BaseModel):
    user_id: str
    preferred_platform: Optional[str] = None
    preferred_style: Optional[str] = None
    preferred_duration: Optional[str] = "30"
    content_preferences: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Generation(BaseModel):
    id: Optional[str] = None
    user_id: str
    content: str
    platform: Optional[str] = None
    topic: Optional[str] = None
    dedup_hash: Optional[str] = None  # 改為可選，後端自動生成
    created_at: Optional[datetime] = None


class ConversationSummary(BaseModel):
    user_id: str
    summary: str
    message_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class GoogleUser(BaseModel):
    id: str
    email: str
    name: str
    picture: Optional[str] = None
    verified_email: bool = False


class AuthToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: GoogleUser


class LongTermMemoryRequest(BaseModel):
    conversation_type: str
    session_id: str
    message_role: str
    message_content: str
    metadata: Optional[str] = None


# 載入環境變數
load_dotenv()

# OAuth 配置（從環境變數讀取）
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:5173/auth/callback")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://aivideonew.zeabur.app")
# 允許作為回跳前端的白名單（避免任意導向）
ALLOWED_FRONTENDS = {
    "https://aivideonew.zeabur.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
}

# 除錯資訊
print(f"DEBUG: Environment variables loaded:")
print(f"DEBUG: GOOGLE_CLIENT_ID: {GOOGLE_CLIENT_ID}")
print(f"DEBUG: GOOGLE_CLIENT_SECRET: {GOOGLE_CLIENT_SECRET}")
print(f"DEBUG: GOOGLE_REDIRECT_URI: {GOOGLE_REDIRECT_URI}")
print(f"DEBUG: FRONTEND_BASE_URL: {FRONTEND_BASE_URL}")

# JWT 密鑰（用於生成訪問令牌）
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "Missing JWT_SECRET in environment. Set a stable secret to keep tokens valid across restarts."
    )

# 安全認證
security = HTTPBearer()


# SQL 語法轉換輔助函數
def convert_sql_for_postgresql(sql: str) -> str:
    """將 SQLite 語法轉換為 PostgreSQL 語法"""
    # 轉換 AUTOINCREMENT
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("AUTOINCREMENT", "")
    
    # 轉換 TEXT 和 VARCHAR
    # 保留 TEXT 類型（PostgreSQL 也支援）
    # 但主鍵用 VARCHAR
    if "PRIMARY KEY" in sql:
        sql = sql.replace("TEXT PRIMARY KEY", "VARCHAR(255) PRIMARY KEY")
    
    # INTEGER -> INTEGER (PostgreSQL 也支援)
    # REAL -> REAL (PostgreSQL 也支援)
    
    return sql


# 數據庫初始化
def init_database():
    """初始化資料庫（支援 PostgreSQL 和 SQLite）"""
    database_url = os.getenv("DATABASE_URL")
    
    # 判斷使用哪種資料庫
    use_postgresql = False
    conn = None
    
    if database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE:
        use_postgresql = True
        print(f"INFO: 初始化 PostgreSQL 資料庫")
        conn = psycopg2.connect(database_url)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()
    else:
        # 使用 SQLite
        db_dir = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
        db_path = os.path.join(db_dir, "chatbot.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        print(f"INFO: 初始化 SQLite 資料庫: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
    
    # 輔助函數：執行 SQL 並自動轉換語法
    def execute_sql(sql: str):
        if use_postgresql:
            sql = convert_sql_for_postgresql(sql)
        cursor.execute(sql)
    
    # 創建用戶偏好表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            preferred_platform TEXT,
            preferred_style TEXT,
            preferred_duration TEXT,
            content_preferences TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 創建生成內容表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            content TEXT,
            platform TEXT,
            topic TEXT,
            dedup_hash TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)
    
    # 創建對話摘要表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            conversation_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)
    
    # 兼容舊表：補齊缺少欄位（message_count, updated_at）
    try:
        execute_sql("""
            ALTER TABLE conversation_summaries ADD COLUMN message_count INTEGER DEFAULT 0
        """)
    except Exception as e:
        # 欄位已存在則略過（SQLite/PG 不同錯誤訊息，這裡容錯）
        pass
    try:
        execute_sql("""
            ALTER TABLE conversation_summaries ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """)
    except Exception as e:
        pass
    
    # 創建用戶偏好追蹤表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            preference_type TEXT NOT NULL,
            preference_value TEXT NOT NULL,
            confidence_score REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id),
            UNIQUE(user_id, preference_type)
        )
    """)
    
    # 創建用戶行為記錄表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS user_behaviors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            behavior_type TEXT NOT NULL,
            behavior_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)
    
    # 創建用戶認證表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS user_auth (
            user_id TEXT PRIMARY KEY,
            google_id TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            picture TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at TIMESTAMP,
            is_subscribed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 為現有用戶添加 is_subscribed 欄位（如果不存在）
    try:
        cursor.execute("ALTER TABLE user_auth ADD COLUMN is_subscribed INTEGER DEFAULT 1")
        print("INFO: 已新增 is_subscribed 欄位到 user_auth 表")
    except (sqlite3.OperationalError, Exception) as e:
        # 兼容 SQLite 和 PostgreSQL 的錯誤
        error_str = str(e).lower()
        if "duplicate column" in error_str or "already exists" in error_str:
            print("INFO: 欄位 is_subscribed 已存在，跳過新增")
        else:
            print(f"WARNING: 無法新增 is_subscribed 欄位: {e}")
    
    # 將所有現有用戶的訂閱狀態設為 1（已訂閱）
    try:
        cursor.execute("UPDATE user_auth SET is_subscribed = 1 WHERE is_subscribed IS NULL OR is_subscribed = 0")
        updated_count = cursor.rowcount
        if updated_count > 0:
            print(f"INFO: 已將 {updated_count} 個用戶設為已訂閱")
    except Exception as e:
        print(f"INFO: 更新訂閱狀態時出現錯誤（可能是表格為空）: {e}")
    
    # 創建帳號定位記錄表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS positioning_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            record_number TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)
    
    # 創建腳本儲存表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS user_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            script_name TEXT,
            title TEXT,
            content TEXT NOT NULL,
            script_data TEXT,
            platform TEXT,
            topic TEXT,
            profile TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_profiles (user_id)
        )
    """)
    
    # 創建購買訂單表（orders）
    execute_sql("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            order_id TEXT UNIQUE NOT NULL,
            plan_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT DEFAULT 'TWD',
            payment_method TEXT,
            payment_status TEXT DEFAULT 'pending',
            paid_at TIMESTAMP,
            expires_at TIMESTAMP,
            invoice_number TEXT,
            invoice_type TEXT,
            vat_number TEXT,
            raw_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 創建長期記憶對話表（Long Term Memory）
    execute_sql("""
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            conversation_type TEXT NOT NULL,
            session_id TEXT,
            message_role TEXT NOT NULL,
            message_content TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_auth (user_id)
        )
    """)
    
    # 創建AI顧問對話記錄表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS ai_advisor_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_role TEXT NOT NULL,
            message_content TEXT NOT NULL,
            platform TEXT,
            topic TEXT,
            style TEXT,
            duration TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_auth (user_id)
        )
    """)
    
    # 創建IP人設規劃對話記錄表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS ip_planning_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_role TEXT NOT NULL,
            message_content TEXT NOT NULL,
            positioning_type TEXT,
            target_audience TEXT,
            content_style TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_auth (user_id)
        )
    """)
    
    # 創建LLM對話記錄表
    execute_sql("""
        CREATE TABLE IF NOT EXISTS llm_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_role TEXT NOT NULL,
            message_content TEXT NOT NULL,
            conversation_context TEXT,
            model_used TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_auth (user_id)
        )
    """)
    
    # 創建授權記錄表（licenses）
    execute_sql("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            order_id TEXT,
            tier TEXT DEFAULT 'personal',
            seats INTEGER DEFAULT 1,
            features_json TEXT,
            source TEXT,
            start_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # PostgreSQL 使用 AUTOCOMMIT，不需要 commit
    # SQLite 需要 commit
    if not use_postgresql:
        conn.commit()
        conn.close()
    
    if use_postgresql:
        conn.close()
        return "PostgreSQL"
    else:
        return db_path


def get_db_connection():
    """獲取數據庫連接（支援 PostgreSQL 和 SQLite）"""
    database_url = os.getenv("DATABASE_URL")
    
    # 如果有 DATABASE_URL 且包含 postgresql://，使用 PostgreSQL
    if database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE:
        try:
            print(f"INFO: 連接到 PostgreSQL 資料庫")
            conn = psycopg2.connect(database_url)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            return conn
        except Exception as e:
            print(f"ERROR: PostgreSQL 連接失敗: {e}")
            raise
    
    # 預設使用 SQLite
    db_dir = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    db_path = os.path.join(db_dir, "chatbot.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    print(f"INFO: 連接到 SQLite 資料庫: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def generate_dedup_hash(content: str, platform: str = None, topic: str = None) -> str:
    """生成去重哈希值"""
    # 清理內容，移除時間相關和隨機元素
    clean_content = content.lower().strip()
    # 移除常見的時間標記和隨機元素
    clean_content = clean_content.replace('\n', ' ').replace('\r', ' ')
    # 移除多餘空格
    clean_content = ' '.join(clean_content.split())
    
    hash_input = f"{clean_content}|{platform or ''}|{topic or ''}"
    return hashlib.md5(hash_input.encode('utf-8')).hexdigest()


def generate_user_id(email: str) -> str:
    """根據 email 生成用戶 ID"""
    return hashlib.md5(email.encode('utf-8')).hexdigest()[:12]


def generate_access_token(user_id: str) -> str:
    """生成訪問令牌"""
    payload = {
        "user_id": user_id,
        "exp": datetime.now().timestamp() + 3600  # 1小時過期
    }
    # 簡單的 JWT 實現（生產環境建議使用 PyJWT）
    import base64
    import json
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip('=')
    encoded_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    signature = hashlib.sha256(f"{encoded_header}.{encoded_payload}.{JWT_SECRET}".encode()).hexdigest()
    return f"{encoded_header}.{encoded_payload}.{signature}"


def verify_access_token(token: str, allow_expired: bool = False) -> Optional[str]:
    """
    驗證訪問令牌並返回用戶 ID
    - allow_expired=False：僅在過期時回傳 None；未過期則回傳 user_id
    - allow_expired=True：允許過期（給 refresh 用），仍回傳 user_id
    """
    try:
        import base64
        import json
        parts = token.split('.')
        if len(parts) != 3:
            print(f"[verify_access_token] format error, allow_expired={allow_expired}")
            return None

        # 簽名驗證
        expected_signature = hashlib.sha256(f"{parts[0]}.{parts[1]}.{JWT_SECRET}".encode()).hexdigest()
        if expected_signature != parts[2]:
            print(f"[verify_access_token] bad signature, allow_expired={allow_expired}")
            print(f"[verify_access_token] JWT_SECRET set: {bool(JWT_SECRET)}")
            return None

        # 解碼 payload（修正 padding）
        payload_b64 = parts[1]
        padding = '=' * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode())

        user_id = payload.get("user_id")
        if not user_id:
            return None

        exp = payload.get("exp", 0)
        now = datetime.now().timestamp()

        if not allow_expired:
            if exp < now:
                print(f"[verify_access_token] expired: exp={exp}, now={now}, allow_expired={allow_expired}")
                return None
            print(f"[verify_access_token] ok: user_id={user_id}, exp={exp}, now={now}, allow_expired={allow_expired}")
            return user_id

        # allow_expired=True：給 refresh 用
        is_expired = exp < now
        print(f"[verify_access_token] ok(refresh): user_id={user_id}, expired={is_expired}, allow_expired={allow_expired}")
        return user_id
    except Exception as e:
        print(f"[verify_access_token] error: {e}, allow_expired={allow_expired}")
        return None


async def get_google_user_info(access_token: str) -> Optional[GoogleUser]:
    """從 Google 獲取用戶資訊"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if response.status_code == 200:
                data = response.json()
                return GoogleUser(
                    id=data["id"],
                    email=data["email"],
                    name=data["name"],
                    picture=data.get("picture"),
                    verified_email=data.get("verified_email", False)
                )
    except Exception as e:
        print(f"Error getting Google user info: {e}")
    return None


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Optional[str]:
    """獲取當前用戶 ID（不允許過期）"""
    try:
        if not credentials:
            print("[get_current_user] no credentials")
            return None
        token_preview = (credentials.credentials or '')[:16]
        user_id = verify_access_token(credentials.credentials, allow_expired=False)
        print(f"[get_current_user] token[:16]={token_preview} user_id={user_id}")
        return user_id
    except Exception as e:
        print(f"[get_current_user] error: {e}")
        return None

async def get_current_user_for_refresh(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Optional[str]:
    """獲取當前用戶 ID（允許過期的 token，用於 refresh 場景）"""
    if not credentials:
        print("DEBUG: get_current_user_for_refresh - 沒有 credentials")
        return None
    token = credentials.credentials
    user_id = verify_access_token(token, allow_expired=True)
    if not user_id:
        print(f"DEBUG: get_current_user_for_refresh - token 驗證失敗，token 前10個字符: {token[:10] if token else 'None'}")
    else:
        print(f"DEBUG: get_current_user_for_refresh - 成功驗證，user_id: {user_id}")
    return user_id


def resolve_kb_path() -> Optional[str]:
    env_path = os.getenv("KB_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Try common relative locations
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.abspath(os.path.join(here, "data", "kb.txt")),  # 當前目錄下的 data/kb.txt
        os.path.abspath(os.path.join(here, "..", "AI短影音智能體重製版", "data", "kb.txt")),
        os.path.abspath(os.path.join(here, "..", "data", "kb.txt")),
        os.path.abspath(os.path.join(here, "..", "..", "AI短影音智能體重製版", "data", "kb.txt")),
        os.path.abspath(os.path.join(here, "..", "..", "data", "kb.txt")),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_kb_text() -> str:
    kb_path = resolve_kb_path()
    if not kb_path:
        return ""
    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def save_conversation_summary(user_id: str, user_message: str, ai_response: str) -> None:
    """保存智能對話摘要"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        database_url = os.getenv("DATABASE_URL")
        use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

        # 確保 user_profiles 存在該 user_id（修復外鍵約束錯誤）
        if use_postgresql:
            cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = %s", (user_id,))
        else:
            cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,))
        
        if not cursor.fetchone():
            # 如果不存在，自動創建
            if use_postgresql:
                cursor.execute("""
                    INSERT INTO user_profiles (user_id, created_at)
                    VALUES (%s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) DO NOTHING
                """, (user_id,))
            else:
                cursor.execute("""
                    INSERT OR IGNORE INTO user_profiles (user_id, created_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                """, (user_id,))

        # 智能摘要生成
        summary = generate_smart_summary(user_message, ai_response)
        conversation_type = classify_conversation(user_message, ai_response)

        if use_postgresql:
            cursor.execute("""
                INSERT INTO conversation_summaries (user_id, summary, conversation_type, created_at)
                VALUES (%s, %s, %s, %s)
            """, (user_id, summary, conversation_type, datetime.now()))
        else:
            cursor.execute("""
                INSERT INTO conversation_summaries (user_id, summary, conversation_type, created_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, summary, conversation_type, datetime.now()))

        # 追蹤用戶偏好
        track_user_preferences(user_id, user_message, ai_response, conversation_type)

        if not use_postgresql:
            conn.commit()
        conn.close()

    except Exception as e:
        print(f"保存對話摘要時出錯: {e}")

def track_user_preferences(user_id: str, user_message: str, ai_response: str, conversation_type: str) -> None:
    """追蹤用戶偏好"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        database_url = os.getenv("DATABASE_URL")
        use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
        
        # 提取偏好信息
        preferences = extract_user_preferences(user_message, ai_response, conversation_type)
        
        for pref_type, pref_value in preferences.items():
            # 檢查是否已存在
            if use_postgresql:
                cursor.execute("""
                    SELECT id, confidence_score FROM user_preferences 
                    WHERE user_id = %s AND preference_type = %s
                """, (user_id, pref_type))
            else:
                cursor.execute("""
                    SELECT id, confidence_score FROM user_preferences 
                    WHERE user_id = ? AND preference_type = ?
                """, (user_id, pref_type))
            
            existing = cursor.fetchone()
            
            if existing:
                # 更新現有偏好，增加信心分數
                new_confidence = min(existing[1] + 0.1, 1.0)
                if use_postgresql:
                    cursor.execute("""
                        UPDATE user_preferences 
                        SET preference_value = %s, confidence_score = %s, updated_at = %s
                        WHERE id = %s
                    """, (pref_value, new_confidence, datetime.now(), existing[0]))
                else:
                    cursor.execute("""
                        UPDATE user_preferences 
                        SET preference_value = ?, confidence_score = ?, updated_at = ?
                        WHERE id = ?
                    """, (pref_value, new_confidence, datetime.now(), existing[0]))
            else:
                # 創建新偏好
                if use_postgresql:
                    cursor.execute("""
                        INSERT INTO user_preferences (user_id, preference_type, preference_value, confidence_score)
                        VALUES (%s, %s, %s, %s)
                    """, (user_id, pref_type, pref_value, 0.5))
                else:
                    cursor.execute("""
                        INSERT INTO user_preferences (user_id, preference_type, preference_value, confidence_score)
                        VALUES (?, ?, ?, ?)
                    """, (user_id, pref_type, pref_value, 0.5))
        
        # 記錄行為
        if use_postgresql:
            cursor.execute("""
                INSERT INTO user_behaviors (user_id, behavior_type, behavior_data)
                VALUES (%s, %s, %s)
            """, (user_id, conversation_type, f"用戶輸入: {user_message[:100]}"))
        else:
            cursor.execute("""
                INSERT INTO user_behaviors (user_id, behavior_type, behavior_data)
                VALUES (?, ?, ?)
            """, (user_id, conversation_type, f"用戶輸入: {user_message[:100]}"))
        
        if not use_postgresql:
            conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"追蹤用戶偏好時出錯: {e}")

def extract_user_preferences(user_message: str, ai_response: str, conversation_type: str) -> dict:
    """提取用戶偏好"""
    preferences = {}
    text = user_message.lower()
    
    # 平台偏好
    platforms = ["抖音", "tiktok", "instagram", "youtube", "小紅書", "快手"]
    for platform in platforms:
        if platform in text:
            preferences["preferred_platform"] = platform
            break
    
    # 內容類型偏好
    content_types = ["美食", "旅遊", "時尚", "科技", "教育", "娛樂", "生活", "健身"]
    for content_type in content_types:
        if content_type in text:
            preferences["preferred_content_type"] = content_type
            break
    
    # 風格偏好
    if "搞笑" in text or "幽默" in text:
        preferences["preferred_style"] = "搞笑幽默"
    elif "專業" in text or "教學" in text:
        preferences["preferred_style"] = "專業教學"
    elif "情感" in text or "溫馨" in text:
        preferences["preferred_style"] = "情感溫馨"
    
    # 時長偏好
    if "30秒" in text or "30s" in text:
        preferences["preferred_duration"] = "30秒"
    elif "60秒" in text or "60s" in text:
        preferences["preferred_duration"] = "60秒"
    elif "15秒" in text or "15s" in text:
        preferences["preferred_duration"] = "15秒"
    
    return preferences

def generate_smart_summary(user_message: str, ai_response: str) -> str:
    """生成智能對話摘要"""
    # 提取關鍵信息
    user_keywords = extract_keywords(user_message)
    ai_keywords = extract_keywords(ai_response)
    
    # 判斷對話類型
    conversation_type = classify_conversation(user_message, ai_response)
    
    # 生成摘要
    if conversation_type == "account_positioning":
        return f"帳號定位討論：{user_keywords} → {ai_keywords}"
    elif conversation_type == "topic_selection":
        return f"選題討論：{user_keywords} → {ai_keywords}"
    elif conversation_type == "script_generation":
        return f"腳本生成：{user_keywords} → {ai_keywords}"
    elif conversation_type == "general_consultation":
        return f"一般諮詢：{user_keywords} → {ai_keywords}"
    else:
        return f"對話：{user_message[:30]}... → {ai_response[:50]}..."

def extract_keywords(text: str) -> str:
    """提取關鍵詞"""
    # 簡單的關鍵詞提取
    keywords = []
    important_words = ["短影音", "腳本", "帳號", "定位", "選題", "平台", "內容", "創意", "爆款", "流量"]
    
    for word in important_words:
        if word in text:
            keywords.append(word)
    
    return "、".join(keywords[:3]) if keywords else "一般討論"

def classify_conversation(user_message: str, ai_response: str) -> str:
    """分類對話類型"""
    text = (user_message + " " + ai_response).lower()
    
    if any(word in text for word in ["帳號定位", "定位", "目標受眾", "受眾"]):
        return "account_positioning"
    elif any(word in text for word in ["選題", "主題", "熱點", "趨勢"]):
        return "topic_selection"
    elif any(word in text for word in ["腳本", "生成", "寫腳本", "製作腳本"]):
        return "script_generation"
    else:
        return "general_consultation"

def get_user_memory(user_id: Optional[str]) -> str:
    """獲取用戶的增強長期記憶和個人化資訊"""
    if not user_id:
        return ""

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        database_url = os.getenv("DATABASE_URL")
        use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

        # 獲取用戶基本資料
        if use_postgresql:
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = %s", (user_id,))
        else:
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        profile = cursor.fetchone()

        # 獲取用戶偏好
        if use_postgresql:
            cursor.execute("""
                SELECT preference_type, preference_value, confidence_score 
                FROM user_preferences 
                WHERE user_id = %s AND confidence_score > 0.3
                ORDER BY confidence_score DESC
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT preference_type, preference_value, confidence_score 
                FROM user_preferences 
                WHERE user_id = ? AND confidence_score > 0.3
                ORDER BY confidence_score DESC
            """, (user_id,))
        preferences = cursor.fetchall()

        # 獲取最近的對話摘要（按類型分組）
        if use_postgresql:
            cursor.execute("""
                SELECT conversation_type, summary, created_at 
                FROM conversation_summaries
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 10
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT conversation_type, summary, created_at 
                FROM conversation_summaries
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 10
            """, (user_id,))
        summaries = cursor.fetchall()

        # 獲取最近的生成記錄
        if use_postgresql:
            cursor.execute("""
                SELECT platform, topic, content, created_at FROM generations
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 5
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT platform, topic, content, created_at FROM generations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 5
            """, (user_id,))
        generations = cursor.fetchall()

        # 獲取用戶行為統計
        if use_postgresql:
            cursor.execute("""
                SELECT behavior_type, COUNT(*) as count
                FROM user_behaviors
                WHERE user_id = %s
                GROUP BY behavior_type
                ORDER BY count DESC
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT behavior_type, COUNT(*) as count
                FROM user_behaviors
                WHERE user_id = ?
                GROUP BY behavior_type
                ORDER BY count DESC
            """, (user_id,))
        behaviors = cursor.fetchall()

        conn.close()

        # 構建增強記憶內容
        memory_parts = []

        # 用戶基本資料
        if profile:
            memory_parts.append(f"用戶基本資料：{profile[2] if len(profile) > 2 else '無'}")

        # 用戶偏好
        if preferences:
            memory_parts.append("用戶偏好分析：")
            for pref_type, pref_value, confidence in preferences:
                confidence_text = "高" if confidence > 0.7 else "中" if confidence > 0.4 else "低"
                memory_parts.append(f"- {pref_type}：{pref_value} (信心度：{confidence_text})")

        # 對話摘要（按類型分組）
        if summaries:
            memory_parts.append("最近對話記錄：")
            current_type = None
            for conv_type, summary, created_at in summaries:
                if conv_type != current_type:
                    type_name = {
                        "account_positioning": "帳號定位討論",
                        "topic_selection": "選題討論", 
                        "script_generation": "腳本生成",
                        "general_consultation": "一般諮詢"
                    }.get(conv_type, "其他討論")
                    memory_parts.append(f"  {type_name}：")
                    current_type = conv_type
                memory_parts.append(f"    - {summary}")

        # 生成記錄
        if generations:
            memory_parts.append("最近生成內容：")
            for gen in generations:
                memory_parts.append(f"- 平台：{gen[0]}, 主題：{gen[1]}, 時間：{gen[3]}")

        # 行為統計
        if behaviors:
            memory_parts.append("用戶行為統計：")
            for behavior_type, count in behaviors:
                type_name = {
                    "account_positioning": "帳號定位",
                    "topic_selection": "選題討論",
                    "script_generation": "腳本生成",
                    "general_consultation": "一般諮詢"
                }.get(behavior_type, behavior_type)
                memory_parts.append(f"- {type_name}：{count}次")

        return "\n".join(memory_parts) if memory_parts else ""

    except Exception as e:
        print(f"獲取用戶記憶時出錯: {e}")
        return ""

def build_system_prompt(kb_text: str, platform: Optional[str], profile: Optional[str], topic: Optional[str], style: Optional[str], duration: Optional[str], user_id: Optional[str] = None) -> str:
    # 檢查用戶是否真的設定了參數（不是預設值）
    platform_line = f"平台：{platform}" if platform else "平台：未設定"
    profile_line = f"帳號定位：{profile}" if profile else "帳號定位：未設定"
    topic_line = f"主題：{topic}" if topic else "主題：未設定"
    duration_line = f"腳本時長：{duration}秒" if duration else "腳本時長：未設定"
    # 獲取用戶記憶
    user_memory = get_user_memory(user_id)
    memory_header = "用戶記憶與個人化資訊：\n" if user_memory else ""
    kb_header = "短影音知識庫（節錄）：\n" if kb_text else ""
    rules = (
        "你是AIJob短影音顧問，專業協助用戶創作短影音內容。\n"
        "回答要口語化、簡潔有力，避免冗長問卷。\n"
        "優先依據知識庫回答，超出範圍可補充一般經驗並標示『[一般經驗]』。\n"
        "\n"
        "⚠️ 核心原則：\n"
        "1. 檢查對話歷史：用戶已經說過什麼？已經回答過什麼問題？\n"
        "2. 基於已有信息：如果用戶已經提供了受眾、產品、目標等信息，直接基於這些信息給建議，不要再問！\n"
        "3. 推進對話：每次回應都要讓對話往前進展，不要原地打轉或重複問題\n"
        "4. 記住流程位置：清楚知道現在是在帳號定位、選題還是腳本生成階段\n"
        "5. 避免問候語重複：如果不是對話開始，不要說「哈囉！很高興為您服務」之類的開場白\n"
        "\n"
        "專業顧問流程：\n"
        "1. 帳號定位階段：\n"
        "   - 收集：受眾是誰？產品/服務是什麼？目標是什麼？\n"
        "   - 當用戶已經說明這些，直接給出定位建議，不要再追問細節！\n"
        "   - 定位建議應包含：目標受眾分析、內容方向、風格調性\n"
        "\n"
        "2. 選題策略階段：\n"
        "   - 基於已確定的定位，推薦3-5個具體選題方向\n"
        "   - 不要再問定位相關問題\n"
        "\n"
        "3. 腳本生成階段：\n"
        "   - 只有在用戶明確要求時，才提供完整腳本\n"
        "\n"
        "對話記憶檢查清單：\n"
        "✅ 用戶是否已經說明受眾？→ 如果有，不要再問！\n"
        "✅ 用戶是否已經說明產品/目標？→ 如果有，不要再問！\n"
        "✅ 現在是對話開始還是中間？→ 如果是中間，不要用開場問候語！\n"
        "✅ 我已經收集到足夠信息了嗎？→ 如果有，給出具體建議，不要拖延！\n"
        "\n"
        "內容格式：\n"
        "• 使用數字標示（1. 2. 3.）或列點（•）組織內容\n"
        "• 用 emoji 分段強調（🚀 💡 ✅ 📌）\n"
        "• 絕對禁止使用 * 或 ** 等 Markdown 格式符號\n"
        "• 每段用換行分隔，保持清晰易讀\n"
        "• 所有內容都必須是純文字格式，沒有任何程式碼符號\n"
        "\n"
        "腳本結構：盡量對齊 Hook → Value → CTA 結構；Value 不超過三點，CTA 給一個明確動作。\n"
        "完整腳本應包含：\n"
        "1. 主題標題\n"
        "2. 腳本內容（只包含台詞、秒數、CTA，不包含畫面描述）\n"
        "3. 畫面感（鏡頭、音效建議）\n"
        "4. 發佈文案\n"
    )
    style_line = style or "格式要求：分段清楚，短句，每段換行，適度加入表情符號（如：✅✨🔥📌），避免口頭禪。使用數字標示（1. 2. 3.）或列點（•）來組織內容，不要使用 * 或 ** 等 Markdown 格式。"
    return f"{platform_line}\n{profile_line}\n{topic_line}\n{duration_line}\n{style_line}\n\n{rules}\n{memory_header}{user_memory}\n{kb_header}{kb_text}"


def create_app() -> FastAPI:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("WARNING: GEMINI_API_KEY not found in environment variables")
        # Delay failure to request time but keep app creatable
    else:
        print(f"INFO: GEMINI_API_KEY found, length: {len(api_key)}")

    genai.configure(api_key=api_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    print(f"INFO: Using model: {model_name}")

    # 初始化數據庫
    db_path = init_database()
    print(f"INFO: Database initialized at: {db_path}")

    app = FastAPI()

    # CORS for local file or dev servers
    frontend_url = os.getenv("FRONTEND_URL")
    cors_origins = [
        "http://localhost:5173",   # 本地前端
        "http://127.0.0.1:5173",  # 本地前端（備用）
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://aivideonew.zeabur.app",
        "http://aivideonew.zeabur.app",
        "https://reelmind.aijob.com.tw",
        "http://reelmind.aijob.com.tw",
        "https://backmanage.zeabur.app",
        "http://backmanage.zeabur.app"
    ]
    
    # 如果有設定前端 URL，加入 CORS 來源
    if frontend_url:
        cors_origins.append(frontend_url)
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    kb_text_cache = load_kb_text()

    @app.get("/")
    async def root():
        return {"message": "AI Video Backend is running"}
    
    @app.get("/api/debug/env")
    async def debug_env():
        """除錯環境變數"""
        return {
            "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": "***" if GOOGLE_CLIENT_SECRET else None,
            "GOOGLE_REDIRECT_URI": GOOGLE_REDIRECT_URI,
            "GEMINI_API_KEY": "***" if os.getenv("GEMINI_API_KEY") else None,
            "GEMINI_MODEL": os.getenv("GEMINI_MODEL"),
            "FRONTEND_URL": os.getenv("FRONTEND_URL")
        }

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        try:
            kb_status = "loaded" if kb_text_cache else "not_found"
            gemini_configured = bool(os.getenv("GEMINI_API_KEY"))
            
            # 測試 Gemini API 連線（如果已配置）
            gemini_test_result = "not_configured"
            if gemini_configured:
                try:
                    model = genai.GenerativeModel(model_name)
                    # 簡單測試呼叫
                    response = model.generate_content("test", request_options={"timeout": 5})
                    gemini_test_result = "working" if response else "failed"
                except Exception as e:
                    gemini_test_result = f"error: {str(e)}"
            
            return {
                "status": "ok",
                "kb_status": kb_status,
                "gemini_configured": gemini_configured,
                "gemini_test": gemini_test_result,
                "model_name": model_name,
                "timestamp": str(datetime.now())
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "timestamp": str(datetime.now())
            }

    @app.post("/api/generate/positioning")
    async def generate_positioning(body: ChatBody, request: Request):
        """一鍵生成帳號定位"""
        if not os.getenv("GEMINI_API_KEY"):
            return JSONResponse({"error": "Missing GEMINI_API_KEY in .env"}, status_code=500)

        # 專門的帳號定位提示詞
        positioning_prompt = f"""
你是AIJob短影音顧問，專門協助用戶進行帳號定位分析。

基於以下信息進行專業的帳號定位分析：
- 平台：{body.platform or '未設定'}
- 主題：{body.topic or '未設定'}
- 現有定位：{body.profile or '未設定'}

請提供：
1. 目標受眾分析
2. 內容定位建議
3. 風格調性建議
4. 競爭優勢分析
5. 具體執行建議

格式要求：分段清楚，短句，每段換行，適度加入表情符號，避免口頭禪。絕對不要使用 ** 或任何 Markdown 格式符號。
"""

        try:
            # 暫時使用原有的 stream_chat 端點
            user_id = getattr(body, 'user_id', None)
            system_text = build_system_prompt(kb_text_cache, body.platform, body.profile, body.topic, body.style, body.duration, user_id)
            
            user_history: List[Dict[str, Any]] = []
            for m in body.history or []:
                user_history.append({"role": m.get("role", "user"), "parts": [m.get("content", "")]})

            model_obj = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_text
            )
            chat = model_obj.start_chat(history=user_history)

            async def generate():
                try:
                    stream_resp = chat.send_message(positioning_prompt, stream=True)
                    for chunk in stream_resp:
                        if chunk.text:
                            yield f"data: {json.dumps({'type': 'token', 'content': chunk.text})}\n\n"
                    
                    # 保存對話摘要
                    if user_id:
                        save_conversation_summary(user_id, positioning_prompt, "".join([c.text for c in stream_resp]))
                    
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                except Exception as ex:
                    yield f"data: {json.dumps({'type': 'error', 'content': str(ex)})}\n\n"

            return StreamingResponse(generate(), media_type="text/plain")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/generate/topics")
    async def generate_topics(body: ChatBody, request: Request):
        """一鍵生成選題推薦"""
        if not os.getenv("GEMINI_API_KEY"):
            return JSONResponse({"error": "Missing GEMINI_API_KEY in .env"}, status_code=500)

        # 專門的選題推薦提示詞
        topics_prompt = f"""
你是AIJob短影音顧問，專門協助用戶進行選題推薦。

基於以下信息推薦熱門選題：
- 平台：{body.platform or '未設定'}
- 主題：{body.topic or '未設定'}
- 帳號定位：{body.profile or '未設定'}

請提供：
1. 熱門選題方向（3-5個）
2. 每個選題的具體建議
3. 選題策略和技巧
4. 內容規劃建議
5. 執行時程建議

格式要求：分段清楚，短句，每段換行，適度加入表情符號，避免口頭禪。絕對不要使用 ** 或任何 Markdown 格式符號。
"""

        try:
            user_id = getattr(body, 'user_id', None)
            system_text = build_system_prompt(kb_text_cache, body.platform, body.profile, body.topic, body.style, body.duration, user_id)
            
            user_history: List[Dict[str, Any]] = []
            for m in body.history or []:
                user_history.append({"role": m.get("role", "user"), "parts": [m.get("content", "")]})

            model_obj = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_text
            )
            chat = model_obj.start_chat(history=user_history)

            async def generate():
                try:
                    stream_resp = chat.send_message(topics_prompt, stream=True)
                    for chunk in stream_resp:
                        if chunk.text:
                            yield f"data: {json.dumps({'type': 'token', 'content': chunk.text})}\n\n"
                    
                    if user_id:
                        save_conversation_summary(user_id, topics_prompt, "".join([c.text for c in stream_resp]))
                    
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                except Exception as ex:
                    yield f"data: {json.dumps({'type': 'error', 'content': str(ex)})}\n\n"

            return StreamingResponse(generate(), media_type="text/plain")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/generate/script")
    async def generate_script(body: ChatBody, request: Request):
        """一鍵生成腳本"""
        if not os.getenv("GEMINI_API_KEY"):
            return JSONResponse({"error": "Missing GEMINI_API_KEY in .env"}, status_code=500)

        # 專門的腳本生成提示詞
        script_prompt = f"""
你是AIJob短影音顧問，專門協助用戶生成短影音腳本。

基於以下信息生成完整腳本：
- 平台：{body.platform or '未設定'}
- 主題：{body.topic or '未設定'}
- 帳號定位：{body.profile or '未設定'}
- 時長：{body.duration or '30'}秒

請生成包含以下結構的完整腳本：
1. 主題標題
2. Hook（開場鉤子）
3. Value（核心價值內容）
4. CTA（行動呼籲）
5. 畫面感描述
6. 發佈文案

格式要求：分段清楚，短句，每段換行，適度加入表情符號，避免口頭禪。絕對不要使用 ** 或任何 Markdown 格式符號。
"""

        try:
            user_id = getattr(body, 'user_id', None)
            system_text = build_system_prompt(kb_text_cache, body.platform, body.profile, body.topic, body.style, body.duration, user_id)
            
            user_history: List[Dict[str, Any]] = []
            for m in body.history or []:
                user_history.append({"role": m.get("role", "user"), "parts": [m.get("content", "")]})

            model_obj = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_text
            )
            chat = model_obj.start_chat(history=user_history)

            async def generate():
                try:
                    stream_resp = chat.send_message(script_prompt, stream=True)
                    for chunk in stream_resp:
                        if chunk.text:
                            yield f"data: {json.dumps({'type': 'token', 'content': chunk.text})}\n\n"
                    
                    if user_id:
                        save_conversation_summary(user_id, script_prompt, "".join([c.text for c in stream_resp]))
                    
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                except Exception as ex:
                    yield f"data: {json.dumps({'type': 'error', 'content': str(ex)})}\n\n"

            return StreamingResponse(generate(), media_type="text/plain")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/chat/stream")
    async def stream_chat(body: ChatBody, request: Request):
        if not os.getenv("GEMINI_API_KEY"):
            return JSONResponse({"error": "Missing GEMINI_API_KEY in .env"}, status_code=500)

        user_id = getattr(body, 'user_id', None)
        
        # === 整合記憶系統 ===
        # 1. 載入短期記憶（STM）- 最近對話上下文
        stm_context = ""
        stm_history = []
        if user_id:
            stm_context = stm.get_context_for_prompt(user_id)
            stm_history = stm.get_recent_turns_for_history(user_id, limit=5)
        
        # 2. 載入長期記憶（LTM）- 您現有的系統
        ltm_memory = get_user_memory(user_id) if user_id else ""
        
        # 3. 組合增強版 prompt
        system_text = build_enhanced_prompt(
            kb_text=kb_text_cache,
            stm_context=stm_context,
            ltm_memory=ltm_memory,
            platform=body.platform,
            profile=body.profile,
            topic=body.topic,
            style=body.style,
            duration=body.duration
        )
        
        # 4. 合併前端傳來的 history 和 STM history
        user_history: List[Dict[str, Any]] = []
        
        # 優先使用 STM 的歷史（更完整）
        if stm_history:
            user_history = stm_history
        else:
            # 如果沒有 STM，使用前端傳來的 history
            for m in body.history or []:
                if m.role == "user":
                    user_history.append({"role": "user", "parts": [m.content]})
                elif m.role in ("assistant", "model"):
                    user_history.append({"role": "model", "parts": [m.content]})

        model = genai.GenerativeModel(model_name)
        chat = model.start_chat(history=[
            {"role": "user", "parts": system_text},
            *user_history,
        ])

        def sse_events() -> Iterable[str]:
            yield f"data: {json.dumps({'type': 'start'})}\n\n"
            ai_response = ""
            try:
                stream = chat.send_message(body.message, stream=True)
                for chunk in stream:
                    try:
                        if chunk and getattr(chunk, "candidates", None):
                            parts = chunk.candidates[0].content.parts
                            if parts:
                                token = parts[0].text
                                if token:
                                    ai_response += token
                                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                    except Exception:
                        continue
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            finally:
                # === 保存記憶 ===
                if user_id and ai_response:
                    # 1. 保存到短期記憶（STM）- 新增
                    stm.add_turn(
                        user_id=user_id,
                        user_message=body.message,
                        ai_response=ai_response,
                        metadata={
                            "platform": body.platform,
                            "topic": body.topic,
                            "profile": body.profile
                        }
                    )
                    
                    # 2. 保存到長期記憶（LTM）- 您原有的系統
                    save_conversation_summary(user_id, body.message, ai_response)
                
                yield f"data: {json.dumps({'type': 'end'})}\n\n"

        return StreamingResponse(sse_events(), media_type="text/event-stream")

    # ===== 長期記憶功能 API =====
    
    @app.get("/api/user/memory/{user_id}")
    async def get_user_memory_api(user_id: str):
        """獲取用戶的長期記憶資訊"""
        try:
            memory = get_user_memory(user_id)
            return {"user_id": user_id, "memory": memory}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/user/conversations/{user_id}")
    async def get_user_conversations(user_id: str):
        """獲取用戶的對話記錄"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT id, conversation_type, summary, message_count, created_at FROM conversation_summaries 
                    WHERE user_id = %s 
                    ORDER BY created_at DESC 
                    LIMIT 100
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, conversation_type, summary, message_count, created_at FROM conversation_summaries 
                    WHERE user_id = ? 
                    ORDER BY created_at DESC 
                    LIMIT 100
                """, (user_id,))
            
            conversations = cursor.fetchall()
            
            conn.close()
            
            result = []
            for conv in conversations:
                conv_type_map = {
                    "account_positioning": "帳號定位",
                    "topic_selection": "選題討論",
                    "script_generation": "腳本生成",
                    "general_consultation": "AI顧問",
                    "ip_planning": "IP人設規劃"
                }
                result.append({
                    "id": conv[0],
                    "mode": conv_type_map.get(conv[1], conv[1]),
                    "summary": conv[2] or "",
                    "message_count": conv[3] or 0,
                    "created_at": conv[4]
                })
            
            return {
                "user_id": user_id,
                "conversations": result
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ===== 用戶歷史API端點 =====
    
    @app.get("/api/user/generations/{user_id}")
    async def get_user_generations(user_id: str):
        """獲取用戶的生成記錄"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT platform, topic, content, created_at FROM generations 
                    WHERE user_id = %s 
                    ORDER BY created_at DESC 
                    LIMIT 10
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT platform, topic, content, created_at FROM generations 
                    WHERE user_id = ? 
                    ORDER BY created_at DESC 
                    LIMIT 10
                """, (user_id,))
            generations = cursor.fetchall()
            
            conn.close()
            
            return {
                "user_id": user_id,
                "generations": [
                    {
                        "platform": gen[0], 
                        "topic": gen[1], 
                        "content": gen[2][:100] + "..." if len(gen[2]) > 100 else gen[2],
                        "created_at": gen[3]
                    } 
                    for gen in generations
                ]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/user/preferences/{user_id}")
    async def get_user_preferences(user_id: str):
        """獲取用戶的偏好設定"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT preference_type, preference_value, confidence_score, updated_at 
                FROM user_preferences 
                WHERE user_id = ? 
                ORDER BY confidence_score DESC, updated_at DESC
            """, (user_id,))
            preferences = cursor.fetchall()
            
            conn.close()
            
            return {
                "user_id": user_id,
                "preferences": [
                    {
                        "type": pref[0],
                        "value": pref[1],
                        "confidence": pref[2],
                        "updated_at": pref[3]
                    } 
                    for pref in preferences
                ]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    # ===== 短期記憶（STM）API =====
    
    @app.get("/api/user/stm/{user_id}")
    async def get_user_stm(user_id: str):
        """獲取用戶的短期記憶（當前會話記憶）"""
        try:
            memory = stm.load_memory(user_id)
            return {
                "user_id": user_id,
                "stm": {
                    "recent_turns": memory.get("recent_turns", []),
                    "last_summary": memory.get("last_summary", ""),
                    "turns_count": len(memory.get("recent_turns", [])),
                    "updated_at": memory.get("updated_at", 0)
                }
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.delete("/api/user/stm/{user_id}")
    async def clear_user_stm(user_id: str):
        """清除用戶的短期記憶"""
        try:
            stm.clear_memory(user_id)
            return {"message": "短期記憶已清除", "user_id": user_id}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/user/memory/full/{user_id}")
    async def get_full_memory(user_id: str):
        """獲取用戶的完整記憶（STM + LTM）"""
        try:
            # STM
            stm_data = stm.load_memory(user_id)
            
            # LTM
            ltm_data = get_user_memory(user_id)
            
            # 格式化顯示
            memory_summary = format_memory_for_display({
                "stm": stm_data,
                "ltm": {"memory_text": ltm_data}
            })
            
            return {
                "user_id": user_id,
                "stm": {
                    "recent_turns_count": len(stm_data.get("recent_turns", [])),
                    "has_summary": bool(stm_data.get("last_summary")),
                    "updated_at": stm_data.get("updated_at", 0)
                },
                "ltm": {
                    "memory_text": ltm_data[:200] + "..." if len(ltm_data) > 200 else ltm_data
                },
                "summary": memory_summary
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/user/positioning/save")
    async def save_positioning_record(request: Request):
        """儲存帳號定位記錄"""
        try:
            data = await request.json()
            user_id = data.get("user_id")
            content = data.get("content")
            
            if not user_id or not content:
                return JSONResponse({"error": "缺少必要參數"}, status_code=400)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 先檢查 user_profiles 是否存在該 user_id，若不存在則自動建立
            if use_postgresql:
                cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,))
            profile_exists = cursor.fetchone()
            
            if not profile_exists:
                # 自動建立 user_profiles 記錄
                if use_postgresql:
                    cursor.execute("""
                        INSERT INTO user_profiles (user_id, created_at)
                        VALUES (%s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) DO NOTHING
                    """, (user_id,))
                else:
                    cursor.execute("""
                        INSERT OR IGNORE INTO user_profiles (user_id, created_at)
                        VALUES (?, CURRENT_TIMESTAMP)
                    """, (user_id,))
                conn.commit()
            
            # 獲取該用戶的記錄數量來生成編號
            if use_postgresql:
                cursor.execute("SELECT COUNT(*) FROM positioning_records WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT COUNT(*) FROM positioning_records WHERE user_id = ?", (user_id,))
            count = cursor.fetchone()[0]
            record_number = f"{count + 1:02d}"
            
            # 插入記錄
            if use_postgresql:
                cursor.execute("""
                    INSERT INTO positioning_records (user_id, record_number, content)
                    VALUES (%s, %s, %s)
                    RETURNING id
                """, (user_id, record_number, content))
                record_id = cursor.fetchone()[0]
            else:
                cursor.execute("""
                    INSERT INTO positioning_records (user_id, record_number, content)
                    VALUES (?, ?, ?)
                """, (user_id, record_number, content))
                conn.commit()
                record_id = cursor.lastrowid
            
            conn.close()
            
            return {
                "success": True,
                "record_id": record_id,
                "record_number": record_number
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/user/positioning/{user_id}")
    async def get_positioning_records(user_id: str):
        """獲取用戶的所有帳號定位記錄"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT id, record_number, content, created_at
                    FROM positioning_records
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, record_number, content, created_at
                    FROM positioning_records
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            
            records = []
            for row in cursor.fetchall():
                records.append({
                    "id": row[0],
                    "record_number": row[1],
                    "content": row[2],
                    "created_at": row[3]
                })
            
            conn.close()
            return {"records": records}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.delete("/api/user/positioning/{record_id}")
    async def delete_positioning_record(record_id: int):
        """刪除帳號定位記錄"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("DELETE FROM positioning_records WHERE id = %s", (record_id,))
            else:
                cursor.execute("DELETE FROM positioning_records WHERE id = ?", (record_id,))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {"success": True}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ===== 腳本儲存功能 API =====
    
    @app.post("/api/scripts/save")
    async def save_script(request: Request):
        """儲存腳本"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                data = await request.json()
                user_id = data.get("user_id")
                content = data.get("content")
                script_data = data.get("script_data", {})
                platform = data.get("platform")
                topic = data.get("topic")
                profile = data.get("profile")
                
                if not user_id or not content:
                    return JSONResponse({"error": "缺少必要參數"}, status_code=400)
                
                conn = get_db_connection()
                cursor = conn.cursor()
                
                database_url = os.getenv("DATABASE_URL")
                use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
                
                # 提取腳本標題作為預設名稱
                script_name = script_data.get("title", "未命名腳本")
                
                # 插入腳本記錄
                if use_postgresql:
                    cursor.execute("""
                        INSERT INTO user_scripts (user_id, script_name, title, content, script_data, platform, topic, profile)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        user_id,
                        script_name,
                        script_data.get("title", ""),
                        content,
                        json.dumps(script_data),
                        platform,
                        topic,
                        profile
                    ))
                    script_id = cursor.fetchone()[0]
                else:
                    cursor.execute("""
                        INSERT INTO user_scripts (user_id, script_name, title, content, script_data, platform, topic, profile)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        user_id,
                        script_name,
                        script_data.get("title", ""),
                        content,
                        json.dumps(script_data),
                        platform,
                        topic,
                        profile
                    ))
                    conn.commit()
                    script_id = cursor.lastrowid
                
                conn.close()
                
                return {
                    "success": True,
                    "script_id": script_id,
                    "message": "腳本儲存成功"
                }
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1 * retry_count)  # 遞增延遲
                    continue
                else:
                    return JSONResponse({"error": f"資料庫錯誤: {str(e)}"}, status_code=500)
            except Exception as e:
                return JSONResponse({"error": f"儲存失敗: {str(e)}"}, status_code=500)
        
        return JSONResponse({"error": "儲存失敗，請稍後再試"}, status_code=500)
    
    @app.get("/api/scripts/my")
    async def get_my_scripts(current_user_id: Optional[str] = Depends(get_current_user)):
        """獲取用戶的腳本列表"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT id, script_name, title, content, script_data, platform, topic, profile, created_at, updated_at
                    FROM user_scripts
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (current_user_id,))
            else:
                cursor.execute("""
                    SELECT id, script_name, title, content, script_data, platform, topic, profile, created_at, updated_at
                    FROM user_scripts
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (current_user_id,))
            
            scripts = []
            for row in cursor.fetchall():
                script_data = json.loads(row[4]) if row[4] else {}
                scripts.append({
                    "id": row[0],
                    "name": row[1],
                    "title": row[2],
                    "content": row[3],
                    "script_data": script_data,
                    "platform": row[5],
                    "topic": row[6],
                    "profile": row[7],
                    "created_at": row[8],
                    "updated_at": row[9]
                })
            
            conn.close()
            return {"scripts": scripts}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    # 長期記憶相關API
    @app.post("/api/memory/long-term")
    async def save_long_term_memory(
        request_body: LongTermMemoryRequest,
        current_user_id: Optional[str] = Depends(get_current_user)
    ):
        """儲存長期記憶對話"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    INSERT INTO long_term_memory (user_id, conversation_type, session_id, message_role, message_content, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (current_user_id, request_body.conversation_type, request_body.session_id, request_body.message_role, request_body.message_content, request_body.metadata))
            else:
                cursor.execute("""
                    INSERT INTO long_term_memory (user_id, conversation_type, session_id, message_role, message_content, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (current_user_id, request_body.conversation_type, request_body.session_id, request_body.message_role, request_body.message_content, request_body.metadata))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            return {"success": True, "message": "長期記憶已儲存"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/memory/long-term")
    async def get_long_term_memory(
        conversation_type: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
        current_user_id: Optional[str] = Depends(get_current_user)
    ):
        """獲取長期記憶對話"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                if conversation_type and session_id:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = %s AND conversation_type = %s AND session_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (current_user_id, conversation_type, session_id, limit))
                elif conversation_type:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = %s AND conversation_type = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (current_user_id, conversation_type, limit))
                else:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (current_user_id, limit))
            else:
                if conversation_type and session_id:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = ? AND conversation_type = ? AND session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (current_user_id, conversation_type, session_id, limit))
                elif conversation_type:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = ? AND conversation_type = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (current_user_id, conversation_type, limit))
                else:
                    cursor.execute("""
                        SELECT id, conversation_type, session_id, message_role, message_content, metadata, created_at
                        FROM long_term_memory
                        WHERE user_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (current_user_id, limit))
            
            memories = []
            for row in cursor.fetchall():
                memories.append({
                    "id": row[0],
                    "conversation_type": row[1],
                    "session_id": row[2],
                    "message_role": row[3],
                    "message_content": row[4],
                    "metadata": row[5],
                    "created_at": row[6]
                })
            
            conn.close()
            return {"memories": memories}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    # 管理員長期記憶API
    @app.get("/api/admin/long-term-memory")
    async def get_all_long_term_memory(conversation_type: Optional[str] = None, limit: int = 100):
        """獲取所有長期記憶記錄（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                if conversation_type:
                    cursor.execute("""
                        SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id, 
                               ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                               ua.name, ua.email
                        FROM long_term_memory ltm
                        LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                        WHERE ltm.conversation_type = %s
                        ORDER BY ltm.created_at DESC
                        LIMIT %s
                    """, (conversation_type, limit))
                else:
                    cursor.execute("""
                        SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id, 
                               ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                               ua.name, ua.email
                        FROM long_term_memory ltm
                        LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                        ORDER BY ltm.created_at DESC
                        LIMIT %s
                    """, (limit,))
            else:
                if conversation_type:
                    cursor.execute("""
                        SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id, 
                               ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                               ua.name, ua.email
                        FROM long_term_memory ltm
                        LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                        WHERE ltm.conversation_type = ?
                        ORDER BY ltm.created_at DESC
                        LIMIT ?
                    """, (conversation_type, limit))
                else:
                    cursor.execute("""
                        SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id, 
                               ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                               ua.name, ua.email
                        FROM long_term_memory ltm
                        LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                        ORDER BY ltm.created_at DESC
                        LIMIT ?
                    """, (limit,))
            
            memories = []
            for row in cursor.fetchall():
                memories.append({
                    "id": row[0],
                    "user_id": row[1],
                    "conversation_type": row[2],
                    "session_id": row[3],
                    "message_role": row[4],
                    "message_content": row[5],
                    "metadata": row[6],
                    "created_at": row[7],
                    "user_name": row[8],
                    "user_email": row[9]
                })
            
            conn.close()
            return {"memories": memories}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # 取得單筆長期記憶（管理員用）
    @app.get("/api/admin/long-term-memory/{memory_id}")
    async def get_long_term_memory_by_id(memory_id: int):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

            if use_postgresql:
                cursor.execute(
                    """
                    SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id,
                           ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                           ua.name, ua.email
                    FROM long_term_memory ltm
                    LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                    WHERE ltm.id = %s
                    """,
                    (memory_id,)
                )
            else:
                cursor.execute(
                    """
                    SELECT ltm.id, ltm.user_id, ltm.conversation_type, ltm.session_id,
                           ltm.message_role, ltm.message_content, ltm.metadata, ltm.created_at,
                           ua.name, ua.email
                    FROM long_term_memory ltm
                    LEFT JOIN user_auth ua ON ltm.user_id = ua.user_id
                    WHERE ltm.id = ?
                    """,
                    (memory_id,)
                )

            row = cursor.fetchone()
            conn.close()
            if not row:
                return JSONResponse({"error": "記錄不存在"}, status_code=404)

            return {
                "id": row[0],
                "user_id": row[1],
                "conversation_type": row[2],
                "session_id": row[3],
                "message_role": row[4],
                "message_content": row[5],
                "metadata": row[6],
                "created_at": row[7],
                "user_name": row[8],
                "user_email": row[9]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # 刪除單筆長期記憶（管理員用）
    @app.delete("/api/admin/long-term-memory/{memory_id}")
    async def delete_long_term_memory(memory_id: int):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

            # 檢查存在
            if use_postgresql:
                cursor.execute("SELECT id FROM long_term_memory WHERE id = %s", (memory_id,))
            else:
                cursor.execute("SELECT id FROM long_term_memory WHERE id = ?", (memory_id,))
            if not cursor.fetchone():
                conn.close()
                return JSONResponse({"error": "記錄不存在"}, status_code=404)

            # 刪除
            if use_postgresql:
                cursor.execute("DELETE FROM long_term_memory WHERE id = %s", (memory_id,))
            else:
                cursor.execute("DELETE FROM long_term_memory WHERE id = ?", (memory_id,))
                conn.commit()

            conn.close()
            return {"success": True}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/memory-stats")
    async def get_memory_stats():
        """獲取長期記憶統計（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                # 總記憶數
                cursor.execute("SELECT COUNT(*) FROM long_term_memory")
                total_memories = cursor.fetchone()[0]
                
                # 活躍用戶數
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM long_term_memory")
                active_users = cursor.fetchone()[0]
                
                # 今日新增記憶數
                cursor.execute("""
                    SELECT COUNT(*) FROM long_term_memory 
                    WHERE DATE(created_at) = CURRENT_DATE
                """)
                today_memories = cursor.fetchone()[0]
                
                # 平均記憶/用戶
                avg_memories_per_user = total_memories / active_users if active_users > 0 else 0
                
            else:
                # SQLite 版本
                cursor.execute("SELECT COUNT(*) FROM long_term_memory")
                total_memories = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM long_term_memory")
                active_users = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM long_term_memory 
                    WHERE DATE(created_at) = DATE('now')
                """)
                today_memories = cursor.fetchone()[0]
                
                avg_memories_per_user = total_memories / active_users if active_users > 0 else 0
            
            conn.close()
            return {
                "total_memories": total_memories,
                "active_users": active_users,
                "today_memories": today_memories,
                "avg_memories_per_user": round(avg_memories_per_user, 2)
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    # 獲取用戶的長期記憶（支援會話篩選）
    @app.get("/api/memory/long-term")
    async def get_user_long_term_memory(
        conversation_type: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
        current_user_id: Optional[str] = Depends(get_current_user)
    ):
        """獲取用戶的長期記憶記錄"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 構建查詢條件
            where_conditions = ["user_id = ?" if not use_postgresql else "user_id = %s"]
            params = [current_user_id]
            
            if conversation_type:
                where_conditions.append("conversation_type = ?" if not use_postgresql else "conversation_type = %s")
                params.append(conversation_type)
            
            if session_id:
                where_conditions.append("session_id = ?" if not use_postgresql else "session_id = %s")
                params.append(session_id)
            
            where_clause = " AND ".join(where_conditions)
            
            if use_postgresql:
                cursor.execute(f"""
                    SELECT id, user_id, conversation_type, session_id, 
                           message_role, message_content, metadata, created_at
                    FROM long_term_memory
                    WHERE {where_clause}
                    ORDER BY created_at ASC
                    LIMIT %s
                """, params + [limit])
            else:
                cursor.execute(f"""
                    SELECT id, user_id, conversation_type, session_id, 
                           message_role, message_content, metadata, created_at
                    FROM long_term_memory
                    WHERE {where_clause}
                    ORDER BY created_at ASC
                    LIMIT ?
                """, params + [limit])
            
            memories = []
            for row in cursor.fetchall():
                memories.append({
                    "id": row[0],
                    "user_id": row[1],
                    "conversation_type": row[2],
                    "session_id": row[3],
                    "message_role": row[4],
                    "message_content": row[5],
                    "metadata": row[6],
                    "created_at": row[7]
                })
            
            conn.close()
            return {"memories": memories}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    # 獲取用戶的會話列表
    @app.get("/api/memory/sessions")
    async def get_user_sessions(
        conversation_type: Optional[str] = None,
        limit: int = 20,
        current_user_id: Optional[str] = Depends(get_current_user)
    ):
        """獲取用戶的會話列表"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            where_condition = "user_id = ?" if not use_postgresql else "user_id = %s"
            params = [current_user_id]
            
            if conversation_type:
                where_condition += " AND conversation_type = ?" if not use_postgresql else " AND conversation_type = %s"
                params.append(conversation_type)
            
            if use_postgresql:
                cursor.execute(f"""
                    SELECT session_id, 
                           MAX(created_at) as last_time,
                           COUNT(*) as message_count,
                           MAX(CASE WHEN message_role = 'user' THEN message_content END) as last_user_message,
                           MAX(CASE WHEN message_role = 'assistant' THEN message_content END) as last_ai_message
                    FROM long_term_memory
                    WHERE {where_condition}
                    GROUP BY session_id
                    ORDER BY last_time DESC
                    LIMIT %s
                """, params + [limit])
            else:
                cursor.execute(f"""
                    SELECT session_id, 
                           MAX(created_at) as last_time,
                           COUNT(*) as message_count,
                           MAX(CASE WHEN message_role = 'user' THEN message_content END) as last_user_message,
                           MAX(CASE WHEN message_role = 'assistant' THEN message_content END) as last_ai_message
                    FROM long_term_memory
                    WHERE {where_condition}
                    GROUP BY session_id
                    ORDER BY last_time DESC
                    LIMIT ?
                """, params + [limit])
            
            sessions = []
            for row in cursor.fetchall():
                sessions.append({
                    "session_id": row[0],
                    "last_time": row[1],
                    "message_count": row[2],
                    "last_user_message": row[3],
                    "last_ai_message": row[4]
                })
            
            conn.close()
            return {"sessions": sessions}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.put("/api/scripts/{script_id}/name")
    async def update_script_name(script_id: int, request: Request, current_user_id: Optional[str] = Depends(get_current_user)):
        """更新腳本名稱"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            data = await request.json()
            new_name = data.get("name")
            
            if not new_name:
                return JSONResponse({"error": "腳本名稱不能為空"}, status_code=400)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 檢查腳本是否屬於當前用戶
            if use_postgresql:
                cursor.execute("SELECT user_id FROM user_scripts WHERE id = %s", (script_id,))
            else:
                cursor.execute("SELECT user_id FROM user_scripts WHERE id = ?", (script_id,))
            result = cursor.fetchone()
            
            if not result:
                return JSONResponse({"error": "腳本不存在"}, status_code=404)
            
            if result[0] != current_user_id:
                return JSONResponse({"error": "無權限修改此腳本"}, status_code=403)
            
            # 更新腳本名稱
            if use_postgresql:
                cursor.execute("""
                    UPDATE user_scripts 
                    SET script_name = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (new_name, script_id))
            else:
                cursor.execute("""
                    UPDATE user_scripts 
                    SET script_name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (new_name, script_id))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {"success": True, "message": "腳本名稱更新成功"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.delete("/api/scripts/{script_id}")
    async def delete_script(script_id: int, current_user_id: Optional[str] = Depends(get_current_user)):
        """刪除腳本"""
        if not current_user_id:
            return JSONResponse({"error": "請先登入"}, status_code=401)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 檢查腳本是否屬於當前用戶
            if use_postgresql:
                cursor.execute("SELECT user_id FROM user_scripts WHERE id = %s", (script_id,))
            else:
                cursor.execute("SELECT user_id FROM user_scripts WHERE id = ?", (script_id,))
            result = cursor.fetchone()
            
            if not result:
                return JSONResponse({"error": "腳本不存在"}, status_code=404)
            
            if result[0] != current_user_id:
                return JSONResponse({"error": "無權限刪除此腳本"}, status_code=403)
            
            # 刪除腳本
            if use_postgresql:
                cursor.execute("DELETE FROM user_scripts WHERE id = %s", (script_id,))
            else:
                cursor.execute("DELETE FROM user_scripts WHERE id = ?", (script_id,))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {"success": True, "message": "腳本刪除成功"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/user/behaviors/{user_id}")
    async def get_user_behaviors(user_id: str):
        """獲取用戶的行為統計"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT behavior_type, COUNT(*) as count, MAX(created_at) as last_activity
                    FROM user_behaviors 
                    WHERE user_id = %s 
                    GROUP BY behavior_type
                    ORDER BY count DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT behavior_type, COUNT(*) as count, MAX(created_at) as last_activity
                    FROM user_behaviors 
                    WHERE user_id = ? 
                    GROUP BY behavior_type
                    ORDER BY count DESC
                """, (user_id,))
            behaviors = cursor.fetchall()
            
            conn.close()
            
            return {
                "user_id": user_id,
                "behaviors": [
                    {
                        "type": behavior[0],
                        "count": behavior[1],
                        "last_activity": behavior[2]
                    } 
                    for behavior in behaviors
                ]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ===== 管理員 API（用於後台管理系統） =====
    
    @app.get("/api/admin/users")
    async def get_all_users():
        """獲取所有用戶資料（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 獲取所有用戶基本資料（包含訂閱狀態和統計）
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT ua.user_id, ua.google_id, ua.email, ua.name, ua.picture, 
                           ua.created_at, ua.is_subscribed, up.preferred_platform, up.preferred_style, up.preferred_duration
                    FROM user_auth ua
                    LEFT JOIN user_profiles up ON ua.user_id = up.user_id
                    ORDER BY ua.created_at DESC
                """)
            else:
                cursor.execute("""
                    SELECT ua.user_id, ua.google_id, ua.email, ua.name, ua.picture, 
                           ua.created_at, ua.is_subscribed, up.preferred_platform, up.preferred_style, up.preferred_duration
                    FROM user_auth ua
                    LEFT JOIN user_profiles up ON ua.user_id = up.user_id
                    ORDER BY ua.created_at DESC
                """)
            
            users = []
            
            for row in cursor.fetchall():
                user_id = row[0]
                
                # 獲取對話數
                if use_postgresql:
                    cursor.execute("""
                        SELECT COUNT(*) FROM conversation_summaries WHERE user_id = %s
                    """, (user_id,))
                else:
                    cursor.execute("""
                        SELECT COUNT(*) FROM conversation_summaries WHERE user_id = ?
                    """, (user_id,))
                conversation_count = cursor.fetchone()[0]
                
                # 獲取腳本數
                if use_postgresql:
                    cursor.execute("""
                        SELECT COUNT(*) FROM user_scripts WHERE user_id = %s
                    """, (user_id,))
                else:
                    cursor.execute("""
                        SELECT COUNT(*) FROM user_scripts WHERE user_id = ?
                    """, (user_id,))
                script_count = cursor.fetchone()[0]
                
                # 格式化日期（台灣時區 UTC+8）
                created_at = row[5]
                if created_at:
                    try:
                        from datetime import timezone, timedelta
                        if isinstance(created_at, datetime):
                            dt = created_at
                        elif isinstance(created_at, str):
                            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        else:
                            dt = None
                        
                        if dt:
                            # 轉換為台灣時區 (UTC+8)
                            taiwan_tz = timezone(timedelta(hours=8))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            dt_taiwan = dt.astimezone(taiwan_tz)
                            created_at = dt_taiwan.strftime('%Y/%m/%d %H:%M')
                    except Exception as e:
                        print(f"格式化日期時出錯: {e}")
                        pass
                
                users.append({
                    "user_id": user_id,
                    "google_id": row[1],
                    "email": row[2],
                    "name": row[3],
                    "picture": row[4],
                    "created_at": created_at,
                    "is_subscribed": bool(row[6]) if row[6] is not None else True,  # 預設為已訂閱
                    "preferred_platform": row[7],
                    "preferred_style": row[8],
                    "preferred_duration": row[9],
                    "conversation_count": conversation_count,
                    "script_count": script_count
                })
            
            conn.close()
            return {"users": users}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.put("/api/admin/users/{user_id}/subscription")
    async def update_user_subscription(user_id: str, request: Request):
        """更新用戶訂閱狀態（管理員用）"""
        try:
            data = await request.json()
            is_subscribed = data.get("is_subscribed", 0)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 更新訂閱狀態
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    UPDATE user_auth 
                    SET is_subscribed = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                """, (1 if is_subscribed else 0, user_id))
            else:
                cursor.execute("""
                    UPDATE user_auth 
                    SET is_subscribed = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (1 if is_subscribed else 0, user_id))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {
                "success": True,
                "message": "訂閱狀態已更新",
                "user_id": user_id,
                "is_subscribed": bool(is_subscribed)
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/user/{user_id}/data")
    async def get_user_complete_data(user_id: str):
        """獲取指定用戶的完整資料（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 用戶基本資料
            if use_postgresql:
                cursor.execute("""
                    SELECT ua.google_id, ua.email, ua.name, ua.picture, ua.created_at,
                           up.preferred_platform, up.preferred_style, up.preferred_duration, up.content_preferences
                    FROM user_auth ua
                    LEFT JOIN user_profiles up ON ua.user_id = up.user_id
                    WHERE ua.user_id = %s
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT ua.google_id, ua.email, ua.name, ua.picture, ua.created_at,
                           up.preferred_platform, up.preferred_style, up.preferred_duration, up.content_preferences
                    FROM user_auth ua
                    LEFT JOIN user_profiles up ON ua.user_id = up.user_id
                    WHERE ua.user_id = ?
                """, (user_id,))
            
            user_data = cursor.fetchone()
            if not user_data:
                return JSONResponse({"error": "用戶不存在"}, status_code=404)
            
            # 帳號定位記錄
            if use_postgresql:
                cursor.execute("""
                    SELECT id, record_number, content, created_at
                    FROM positioning_records
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, record_number, content, created_at
                    FROM positioning_records
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            positioning_records = cursor.fetchall()
            
            # 腳本記錄
            if use_postgresql:
                cursor.execute("""
                    SELECT id, script_name, title, content, script_data, platform, topic, profile, created_at
                    FROM user_scripts
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, script_name, title, content, script_data, platform, topic, profile, created_at
                    FROM user_scripts
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            script_records = cursor.fetchall()
            
            # 生成記錄
            if use_postgresql:
                cursor.execute("""
                    SELECT id, content, platform, topic, created_at
                    FROM generations
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, content, platform, topic, created_at
                    FROM generations
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            generation_records = cursor.fetchall()
            
            # 對話摘要
            if use_postgresql:
                cursor.execute("""
                    SELECT id, summary, conversation_type, created_at
                    FROM conversation_summaries
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, summary, conversation_type, created_at
                    FROM conversation_summaries
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            conversation_summaries = cursor.fetchall()
            
            # 用戶偏好
            if use_postgresql:
                cursor.execute("""
                    SELECT preference_type, preference_value, confidence_score, created_at
                    FROM user_preferences
                    WHERE user_id = %s
                    ORDER BY confidence_score DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT preference_type, preference_value, confidence_score, created_at
                    FROM user_preferences
                    WHERE user_id = ?
                    ORDER BY confidence_score DESC
                """, (user_id,))
            user_preferences = cursor.fetchall()
            
            # 用戶行為
            if use_postgresql:
                cursor.execute("""
                    SELECT behavior_type, behavior_data, created_at
                    FROM user_behaviors
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT behavior_type, behavior_data, created_at
                    FROM user_behaviors
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            user_behaviors = cursor.fetchall()
            
            conn.close()
            
            return {
                "user_info": {
                    "user_id": user_id,
                    "google_id": user_data[0],
                    "email": user_data[1],
                    "name": user_data[2],
                    "picture": user_data[3],
                    "created_at": user_data[4],
                    "preferred_platform": user_data[5],
                    "preferred_style": user_data[6],
                    "preferred_duration": user_data[7],
                    "content_preferences": json.loads(user_data[8]) if user_data[8] else None
                },
                "positioning_records": [
                    {
                        "id": record[0],
                        "record_number": record[1],
                        "content": record[2],
                        "created_at": record[3]
                    } for record in positioning_records
                ],
                "script_records": [
                    {
                        "id": record[0],
                        "script_name": record[1],
                        "title": record[2],
                        "content": record[3],
                        "script_data": json.loads(record[4]) if record[4] else {},
                        "platform": record[5],
                        "topic": record[6],
                        "profile": record[7],
                        "created_at": record[8]
                    } for record in script_records
                ],
                "generation_records": [
                    {
                        "id": record[0],
                        "content": record[1],
                        "platform": record[2],
                        "topic": record[3],
                        "created_at": record[4]
                    } for record in generation_records
                ],
                "conversation_summaries": [
                    {
                        "id": record[0],
                        "summary": record[1],
                        "conversation_type": record[2],
                        "created_at": record[3]
                    } for record in conversation_summaries
                ],
                "user_preferences": [
                    {
                        "preference_type": record[0],
                        "preference_value": record[1],
                        "confidence_score": record[2],
                        "created_at": record[3]
                    } for record in user_preferences
                ],
                "user_behaviors": [
                    {
                        "behavior_type": record[0],
                        "behavior_data": record[1],
                        "created_at": record[2]
                    } for record in user_behaviors
                ]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/statistics")
    async def get_admin_statistics():
        """獲取系統統計資料（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 判斷資料庫類型
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 用戶總數
            cursor.execute("SELECT COUNT(*) FROM user_auth")
            total_users = cursor.fetchone()[0]
            
            # 今日新增用戶（兼容 SQLite 和 PostgreSQL）
            if use_postgresql:
                cursor.execute("""
                    SELECT COUNT(*) FROM user_auth 
                    WHERE created_at::date = CURRENT_DATE
                """)
            else:
                cursor.execute("""
                    SELECT COUNT(*) FROM user_auth 
                    WHERE DATE(created_at) = DATE('now')
                """)
            today_users = cursor.fetchone()[0]
            
            # 腳本總數
            cursor.execute("SELECT COUNT(*) FROM user_scripts")
            total_scripts = cursor.fetchone()[0]
            
            # 帳號定位總數
            cursor.execute("SELECT COUNT(*) FROM positioning_records")
            total_positioning = cursor.fetchone()[0]
            
            # 生成內容總數
            cursor.execute("SELECT COUNT(*) FROM generations")
            total_generations = cursor.fetchone()[0]
            
            # 對話摘要總數
            cursor.execute("SELECT COUNT(*) FROM conversation_summaries")
            total_conversations = cursor.fetchone()[0]
            
            # 平台使用統計
            cursor.execute("""
                SELECT platform, COUNT(*) as count
                FROM user_scripts
                WHERE platform IS NOT NULL
                GROUP BY platform
                ORDER BY count DESC
            """)
            platform_stats = cursor.fetchall()
            
            # 最近活躍用戶（7天內）（兼容 SQLite 和 PostgreSQL）
            if use_postgresql:
                cursor.execute("""
                    SELECT COUNT(DISTINCT user_id) 
                    FROM user_scripts 
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
                """)
            else:
                cursor.execute("""
                    SELECT COUNT(DISTINCT user_id) 
                    FROM user_scripts 
                    WHERE created_at >= datetime('now', '-7 days')
                """)
            active_users_7d = cursor.fetchone()[0]
            
            conn.close()
            
            return {
                "total_users": total_users,
                "today_users": today_users,
                "total_scripts": total_scripts,
                "total_positioning": total_positioning,
                "total_generations": total_generations,
                "total_conversations": total_conversations,
                "active_users_7d": active_users_7d,
                "platform_stats": [
                    {"platform": stat[0], "count": stat[1]} 
                    for stat in platform_stats
                ]
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/mode-statistics")
    async def get_mode_statistics():
        """獲取模式使用統計"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 獲取各模式的對話數
            cursor.execute("""
                SELECT conversation_type, COUNT(*) as count
                FROM conversation_summaries
                WHERE conversation_type IS NOT NULL
                GROUP BY conversation_type
            """)
            conversations = cursor.fetchall()
            
            # 計算各模式統計
            mode_stats = {
                "mode1_quick_generate": {"count": 0, "success_rate": 0},
                "mode2_ai_consultant": {"count": 0, "avg_turns": 0},
                "mode3_ip_planning": {"count": 0, "profiles_generated": 0}
            }
            
            # 根據對話類型分類
            for conv_type, count in conversations:
                if conv_type == "account_positioning":
                    mode_stats["mode1_quick_generate"]["count"] = count
                elif conv_type in ["topic_selection", "script_generation"]:
                    mode_stats["mode2_ai_consultant"]["count"] += count
                elif conv_type == "general_consultation":
                    mode_stats["mode2_ai_consultant"]["count"] += count
            
            # 獲取時間分布
            if use_postgresql:
                cursor.execute("""
                    SELECT DATE_TRUNC('hour', created_at) as hour, COUNT(*) as count
                    FROM conversation_summaries
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'
                    GROUP BY hour
                    ORDER BY hour
                """)
            else:
                cursor.execute("""
                    SELECT strftime('%H', created_at) as hour, COUNT(*) as count
                    FROM conversation_summaries
                    WHERE created_at >= datetime('now', '-30 days')
                    GROUP BY hour
                    ORDER BY hour
                """)
            
            time_stats = {"00:00-06:00": 0, "06:00-12:00": 0, "12:00-18:00": 0, "18:00-24:00": 0}
            for row in cursor.fetchall():
                try:
                    if use_postgresql:
                        # PostgreSQL 返回 datetime 對象
                        hour_str = row[0].strftime('%H')
                    else:
                        # SQLite 返回字符串 'HH' 格式
                        hour_str = str(row[0])[:2]
                    hour = int(hour_str)
                except:
                    hour = 0
                
                count = row[1]
                if 0 <= hour < 6:
                    time_stats["00:00-06:00"] += count
                elif 6 <= hour < 12:
                    time_stats["06:00-12:00"] += count
                elif 12 <= hour < 18:
                    time_stats["12:00-18:00"] += count
                else:
                    time_stats["18:00-24:00"] += count
            
            conn.close()
            
            return {
                "mode_stats": mode_stats,
                "time_distribution": time_stats
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/conversations")
    async def get_all_conversations():
        """獲取所有對話記錄（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT cs.id, cs.user_id, cs.conversation_type, cs.summary, cs.message_count, cs.created_at, 
                           ua.name, ua.email
                    FROM conversation_summaries cs
                    LEFT JOIN user_auth ua ON cs.user_id = ua.user_id
                    ORDER BY cs.created_at DESC
                    LIMIT 100
                """)
            else:
                cursor.execute("""
                    SELECT cs.id, cs.user_id, cs.conversation_type, cs.summary, cs.message_count, cs.created_at, 
                           ua.name, ua.email
                    FROM conversation_summaries cs
                    LEFT JOIN user_auth ua ON cs.user_id = ua.user_id
                    ORDER BY cs.created_at DESC
                    LIMIT 100
                """)
            
            conversations = []
            conv_type_map = {
                "account_positioning": "帳號定位",
                "topic_selection": "選題討論",
                "script_generation": "腳本生成",
                "general_consultation": "AI顧問",
                "ip_planning": "IP人設規劃"
            }
            
            for row in cursor.fetchall():
                conversations.append({
                    "id": row[0],
                    "user_id": row[1],
                    "mode": conv_type_map.get(row[2], row[2]),
                    "conversation_type": row[2],
                    "summary": row[3] or "",
                    "message_count": row[4] or 0,
                    "created_at": row[5],
                    "user_name": row[6] or "未知用戶",
                    "user_email": row[7] or ""
                })
            
            conn.close()
            
            return {"conversations": conversations}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/generations")
    async def get_all_generations():
        """獲取所有生成記錄"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT g.id, g.user_id, g.platform, g.topic, g.content, g.created_at, 
                           ua.name, ua.email
                    FROM generations g
                    LEFT JOIN user_auth ua ON g.user_id = ua.user_id
                    ORDER BY g.created_at DESC
                    LIMIT 100
                """)
            else:
                cursor.execute("""
                    SELECT g.id, g.user_id, g.platform, g.topic, g.content, g.created_at, 
                           ua.name, ua.email
                    FROM generations g
                    LEFT JOIN user_auth ua ON g.user_id = ua.user_id
                    ORDER BY g.created_at DESC
                    LIMIT 100
                """)
            
            generations = []
            for row in cursor.fetchall():
                generations.append({
                    "id": row[0],
                    "user_id": row[1],
                    "user_name": row[6] or "未知用戶",
                    "user_email": row[7] or "",
                    "platform": row[2] or "未設定",
                    "topic": row[3] or "未分類",
                    "type": "生成記錄",
                    "content": row[4][:100] if row[4] else "",
                    "created_at": row[5]
                })
            
            conn.close()
            
            return {"generations": generations}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/scripts")
    async def get_all_scripts():
        """獲取所有腳本記錄（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT us.id, us.user_id, us.script_name, us.title, us.platform, us.topic, 
                           us.created_at, ua.name, ua.email
                    FROM user_scripts us
                    LEFT JOIN user_auth ua ON us.user_id = ua.user_id
                    ORDER BY us.created_at DESC
                    LIMIT 100
                """)
            else:
                cursor.execute("""
                    SELECT us.id, us.user_id, us.script_name, us.title, us.platform, us.topic, 
                           us.created_at, ua.name, ua.email
                    FROM user_scripts us
                    LEFT JOIN user_auth ua ON us.user_id = ua.user_id
                    ORDER BY us.created_at DESC
                    LIMIT 100
                """)
            
            scripts = []
            for row in cursor.fetchall():
                scripts.append({
                    "id": row[0],
                    "user_id": row[1],
                    "name": row[2] or row[3] or "未命名腳本",
                    "title": row[3] or row[2] or "未命名腳本",
                    "platform": row[4] or "未設定",
                    "category": row[5] or "未分類",
                    "topic": row[5] or "未分類",
                    "created_at": row[6],
                    "user_name": row[7] or "未知用戶",
                    "user_email": row[8] or ""
                })
            
            conn.close()
            
            return {"scripts": scripts}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/platform-statistics")
    async def get_platform_statistics():
        """獲取平台使用統計"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            cursor.execute("""
                SELECT platform, COUNT(*) as count
                FROM user_scripts
                WHERE platform IS NOT NULL
                GROUP BY platform
                ORDER BY count DESC
            """)
            
            platform_stats = [{"platform": row[0], "count": row[1]} for row in cursor.fetchall()]
            
            conn.close()
            
            return {"platform_stats": platform_stats}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/user-activities")
    async def get_user_activities():
        """獲取最近用戶活動"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 獲取最近10個活動
            activities = []
            
            # 最近註冊的用戶
            cursor.execute("""
                SELECT user_id, name, created_at
                FROM user_auth
                ORDER BY created_at DESC
                LIMIT 3
            """)
            for row in cursor.fetchall():
                activities.append({
                    "type": "新用戶註冊",
                    "user_id": row[0],
                    "name": row[1] or "未知用戶",
                    "time": row[2],
                    "icon": "👤"
                })
            
            # 最近的腳本生成
            cursor.execute("""
                SELECT us.user_id, us.title, us.created_at, ua.name
                FROM user_scripts us
                LEFT JOIN user_auth ua ON us.user_id = ua.user_id
                ORDER BY us.created_at DESC
                LIMIT 3
            """)
            for row in cursor.fetchall():
                activities.append({
                    "type": "新腳本生成",
                    "user_id": row[0],
                    "name": row[3] or "未知用戶",
                    "title": row[1] or "未命名腳本",
                    "time": row[2],
                    "icon": "📝"
                })
            
            # 最近的對話
            cursor.execute("""
                SELECT cs.user_id, cs.conversation_type, cs.created_at, ua.name
                FROM conversation_summaries cs
                LEFT JOIN user_auth ua ON cs.user_id = ua.user_id
                ORDER BY cs.created_at DESC
                LIMIT 3
            """)
            for row in cursor.fetchall():
                mode_map = {
                    "account_positioning": "帳號定位",
                    "topic_selection": "選題討論",
                    "script_generation": "腳本生成",
                    "general_consultation": "AI顧問對話"
                }
                activities.append({
                    "type": f"{mode_map.get(row[1], '對話')}",
                    "user_id": row[0],
                    "name": row[3] or "未知用戶",
                    "time": row[2],
                    "icon": "💬"
                })
            
            # 按時間排序
            activities.sort(key=lambda x: x['time'], reverse=True)
            activities = activities[:10]
            
            conn.close()
            
            return {"activities": activities}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/analytics-data")
    async def get_analytics_data():
        """獲取分析頁面所需的所有數據"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 平台使用分布
            cursor.execute("""
                SELECT platform, COUNT(*) as count
                FROM user_scripts
                WHERE platform IS NOT NULL
                GROUP BY platform
                ORDER BY count DESC
            """)
            platform_stats = cursor.fetchall()
            platform_labels = [row[0] for row in platform_stats]
            platform_data = [row[1] for row in platform_stats]
            
            # 時間段使用分析（最近30天）
            if use_postgresql:
                cursor.execute("""
                    SELECT DATE_TRUNC('day', created_at) as date, COUNT(*) as count
                    FROM user_scripts
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'
                    GROUP BY date
                    ORDER BY date
                """)
            else:
                cursor.execute("""
                    SELECT DATE(created_at) as date, COUNT(*) as count
                    FROM user_scripts
                    WHERE created_at >= datetime('now', '-30 days')
                    GROUP BY date
                    ORDER BY date
                """)
            
            daily_usage = {}
            for row in cursor.fetchall():
                try:
                    if use_postgresql:
                        # PostgreSQL 返回 date 對象
                        day_name = row[0].strftime('%a')
                    else:
                        # SQLite 返回 'YYYY-MM-DD' 字符串
                        from datetime import datetime
                        date_str = str(row[0])
                        day_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        day_name = day_obj.strftime('%a')
                except:
                    day_name = 'Mon'
                
                daily_usage[day_name] = daily_usage.get(day_name, 0) + row[1]
            
            # 內容類型分布（根據 topic 分類）
            cursor.execute("""
                SELECT topic, COUNT(*) as count
                FROM user_scripts
                WHERE topic IS NOT NULL AND topic != ''
                GROUP BY topic
                ORDER BY count DESC
                LIMIT 5
            """)
            content_types = cursor.fetchall()
            content_labels = [row[0] for row in content_types]
            content_data = [row[1] for row in content_types]
            
            # 用戶活躍度（最近4週）
            weekly_activity = []
            for i in range(4):
                if use_postgresql:
                    cursor.execute(f"""
                        SELECT COUNT(DISTINCT user_id)
                        FROM user_scripts
                        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '{7 * (i + 1)} days'
                          AND created_at < CURRENT_TIMESTAMP - INTERVAL '{7 * i} days'
                    """)
                else:
                    cursor.execute(f"""
                        SELECT COUNT(DISTINCT user_id)
                        FROM user_scripts
                        WHERE created_at >= datetime('now', '-{7 * (i + 1)} days')
                          AND created_at < datetime('now', '-{7 * i} days')
                    """)
                count = cursor.fetchone()[0]
                weekly_activity.append(count)
            
            conn.close()
            
            return {
                "platform": {
                    "labels": platform_labels,
                    "data": platform_data
                },
                "time_usage": {
                    "labels": ['週一', '週二', '週三', '週四', '週五', '週六', '週日'],
                    "data": [
                        daily_usage.get('Mon', 0),
                        daily_usage.get('Tue', 0),
                        daily_usage.get('Wed', 0),
                        daily_usage.get('Thu', 0),
                        daily_usage.get('Fri', 0),
                        daily_usage.get('Sat', 0),
                        daily_usage.get('Sun', 0)
                    ]
                },
                "activity": {
                    "labels": ['第1週', '第2週', '第3週', '第4週'],
                    "data": weekly_activity
                },
                "content_type": {
                    "labels": content_labels,
                    "data": content_data
                }
            }
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    
    @app.get("/api/admin/export/{export_type}")
    async def export_csv(export_type: str):
        """匯出 CSV 檔案"""
        import csv
        import io
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 根據匯出類型選擇不同的數據
            if export_type == "users":
                cursor.execute("""
                    SELECT user_id, name, email, created_at, is_subscribed
                    FROM user_auth
                    ORDER BY created_at DESC
                """)
                
                # 創建 CSV
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['用戶ID', '姓名', 'Email', '註冊時間', '是否訂閱'])
                for row in cursor.fetchall():
                    writer.writerow(row)
                output.seek(0)
                
                return Response(
                    content=output.getvalue(),
                    media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=users.csv"}
                )
            
            elif export_type == "scripts":
                cursor.execute("""
                    SELECT us.id, ua.name, us.platform, us.topic, us.title, us.created_at
                    FROM user_scripts us
                    LEFT JOIN user_auth ua ON us.user_id = ua.user_id
                    ORDER BY us.created_at DESC
                """)
                
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['腳本ID', '用戶名稱', '平台', '主題', '標題', '創建時間'])
                for row in cursor.fetchall():
                    writer.writerow(row)
                output.seek(0)
                
                return Response(
                    content=output.getvalue(),
                    media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=scripts.csv"}
                )
            
            elif export_type == "conversations":
                cursor.execute("""
                    SELECT cs.id, ua.name, cs.conversation_type, cs.summary, cs.created_at
                    FROM conversation_summaries cs
                    LEFT JOIN user_auth ua ON cs.user_id = ua.user_id
                    ORDER BY cs.created_at DESC
                """)
                
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['對話ID', '用戶名稱', '對話類型', '摘要', '創建時間'])
                for row in cursor.fetchall():
                    writer.writerow(row)
                output.seek(0)
                
                return Response(
                    content=output.getvalue(),
                    media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=conversations.csv"}
                )
            
            elif export_type == "generations":
                cursor.execute("""
                    SELECT g.id, ua.name, g.platform, g.topic, g.content, g.created_at
                    FROM generations g
                    LEFT JOIN user_auth ua ON g.user_id = ua.user_id
                    ORDER BY g.created_at DESC
                """)
                
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['生成ID', '用戶名稱', '平台', '主題', '內容', '創建時間'])
                for row in cursor.fetchall():
                    writer.writerow(row)
                output.seek(0)
                
                return Response(
                    content=output.getvalue(),
                    media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=generations.csv"}
                )
            
            else:
                return JSONResponse({"error": "無效的匯出類型"}, status_code=400)
        
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ===== OAuth 認證功能 =====
    
    @app.get("/api/auth/google")
    async def google_auth(request: Request, fb: Optional[str] = None):
        """發起 Google OAuth 認證"""
        # 透過查詢參數 fb 覆寫回跳前端（必須在白名單內）
        chosen_frontend = fb if fb in ALLOWED_FRONTENDS else FRONTEND_BASE_URL
        # 以 state 帶回前端 base，callback 取回以決定最終導向
        from urllib.parse import quote
        state_val = quote(chosen_frontend)
        auth_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth?"
            f"client_id={GOOGLE_CLIENT_ID}&"
            f"redirect_uri={GOOGLE_REDIRECT_URI}&"
            f"response_type=code&"
            f"scope=openid email profile&"
            f"access_type=offline&"
            f"prompt=select_account&"
            f"state={state_val}"
        )
        
        # 除錯資訊
        print(f"DEBUG: Generated auth URL: {auth_url}")
        print(f"DEBUG: GOOGLE_CLIENT_ID: {GOOGLE_CLIENT_ID}")
        print(f"DEBUG: GOOGLE_REDIRECT_URI: {GOOGLE_REDIRECT_URI}")
        
        return {"auth_url": auth_url}

    @app.get("/api/auth/google/callback")
    async def google_callback_get(code: str = None, state: Optional[str] = None):
        """處理 Google OAuth 回調（GET 請求 - 來自 Google 重定向）"""
        try:
            # 除錯資訊
            print(f"DEBUG: OAuth callback received")
            print(f"DEBUG: Code: {code}")
            print(f"DEBUG: GOOGLE_CLIENT_ID: {GOOGLE_CLIENT_ID}")
            print(f"DEBUG: GOOGLE_CLIENT_SECRET: {GOOGLE_CLIENT_SECRET}")
            print(f"DEBUG: GOOGLE_REDIRECT_URI: {GOOGLE_REDIRECT_URI}")
            
            # 從 URL 參數獲取授權碼
            if not code:
                # 如果沒有 code，重定向到前端並顯示錯誤
                return RedirectResponse(url="https://aivideonew.zeabur.app/?error=missing_code")
            
            # 交換授權碼獲取訪問令牌
            async with httpx.AsyncClient() as client:
                token_response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": GOOGLE_REDIRECT_URI,
                    }
                )
                
                if token_response.status_code != 200:
                    raise HTTPException(status_code=400, detail="Failed to get access token")
                
                token_data = token_response.json()
                access_token = token_data["access_token"]
                
                # 獲取用戶資訊
                google_user = await get_google_user_info(access_token)
                if not google_user:
                    raise HTTPException(status_code=400, detail="Failed to get user info")
                
                # 生成用戶 ID
                user_id = generate_user_id(google_user.email)
                
                # 保存或更新用戶認證資訊
                conn = get_db_connection()
                cursor = conn.cursor()
                
                database_url = os.getenv("DATABASE_URL")
                use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
                
                if use_postgresql:
                    # PostgreSQL 語法
                    from datetime import timedelta
                    expires_at_value = datetime.now() + timedelta(seconds=token_data.get("expires_in", 3600))
                    
                    cursor.execute("""
                        INSERT INTO user_auth 
                        (user_id, google_id, email, name, picture, access_token, expires_at, is_subscribed, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) 
                        DO UPDATE SET 
                            google_id = EXCLUDED.google_id,
                            email = EXCLUDED.email,
                            name = EXCLUDED.name,
                            picture = EXCLUDED.picture,
                            access_token = EXCLUDED.access_token,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = CURRENT_TIMESTAMP
                    """, (
                        user_id,
                        google_user.id,
                        google_user.email,
                        google_user.name,
                        google_user.picture,
                        access_token,
                        expires_at_value,
                            0  # 新用戶預設為未訂閱
                    ))
                else:
                    # SQLite 語法
                    cursor.execute("""
                        INSERT OR REPLACE INTO user_auth 
                        (user_id, google_id, email, name, picture, access_token, expires_at, is_subscribed, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (
                        user_id,
                        google_user.id,
                        google_user.email,
                        google_user.name,
                        google_user.picture,
                        access_token,
                        datetime.now().timestamp() + token_data.get("expires_in", 3600),
                            0  # 新用戶預設為未訂閱
                    ))
                
                if not use_postgresql:
                    conn.commit()
                conn.close()
                
                # 生成應用程式訪問令牌
                app_access_token = generate_access_token(user_id)
                
                # 使用 URL 編碼確保參數安全
                from urllib.parse import quote, unquote
                safe_token = quote(app_access_token)
                safe_user_id = quote(user_id)
                safe_email = quote(google_user.email or '')
                safe_name = quote(google_user.name or '')
                safe_picture = quote(google_user.picture or '')
                # 取回 state 中的前端 base（若在白名單內）
                frontend_base = FRONTEND_BASE_URL
                try:
                    if state:
                        decoded = unquote(state)
                        if decoded in ALLOWED_FRONTENDS:
                            frontend_base = decoded
                except Exception:
                    pass
                # Redirect 到前端的 popup-callback.html 頁面
                # 該頁面會使用 postMessage 傳遞 token 給主視窗並自動關閉
                callback_url = (
                    f"{frontend_base}/auth/popup-callback.html"
                    f"?token={safe_token}"
                    f"&user_id={safe_user_id}"
                    f"&email={safe_email}"
                    f"&name={safe_name}"
                    f"&picture={safe_picture}"
                    f"&origin={quote(frontend_base)}"
                )
                
                print(f"DEBUG: Redirecting to callback URL: {callback_url}")
                
                # 設置適當的 HTTP Header 以支援 popup 通信
                response = RedirectResponse(url=callback_url)
                response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
                return response
                
        except Exception as e:
            # 處理錯誤訊息以安全地嵌入 JavaScript（先處理再放入 f-string）
            error_msg = str(e).replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
            
            # 返回錯誤頁面
            error_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>登入失敗</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    }}
                    .container {{
                        text-align: center;
                        background: white;
                        padding: 40px;
                        border-radius: 12px;
                        box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                    }}
                    h2 {{ color: #e74c3c; margin: 0 0 10px 0; }}
                    p {{ color: #7f8c8d; margin: 0; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h2>❌ 登入失敗</h2>
                    <p>{error_msg}</p>
                </div>
                <script>
                    (function() {{
                        try {{
                    if (window.opener) {{
                        window.opener.postMessage({{
                            type: 'GOOGLE_AUTH_ERROR',
                                    error: '{error_msg}'
                        }}, '*');
                                setTimeout(function() {{
                                    try {{
                                        window.close();
                                    }} catch (closeErr) {{
                                        console.log('Unable to close window:', closeErr);
                                    }}
                                }}, 3000);
                            }}
                        }} catch (postErr) {{
                            console.error('Error sending error message:', postErr);
                        }}
                    }})();
                </script>
            </body>
            </html>
            """
            
            # 設置適當的 HTTP Header 以支援 popup 通信
            error_response = HTMLResponse(content=error_html, status_code=500)
            error_response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
            error_response.headers["Access-Control-Allow-Origin"] = "https://aivideonew.zeabur.app"
            return error_response

    # ===== 金流回調（準備用，未啟用驗簽） =====
    @app.post("/api/payment/callback")
    async def payment_callback(payload: dict):
        """金流回調（測試/準備用）：更新用戶訂閱狀態與到期日。
        期待參數：
        - user_id: str
        - plan: 'monthly' | 'yearly'
        - transaction_id, amount, paid_at（可選，用於記錄）
        注意：正式上線需加入簽章驗證與來源白名單。
        """
        try:
            user_id = payload.get("user_id")
            plan = payload.get("plan")
            paid_at = payload.get("paid_at")
            transaction_id = payload.get("transaction_id")
            amount = payload.get("amount")

            if not user_id or plan not in ("monthly", "yearly"):
                raise HTTPException(status_code=400, detail="missing user_id or invalid plan")

            # 計算到期日
            days = 30 if plan == "monthly" else 365
            expires_dt = datetime.now() + timedelta(days=days)

            conn = get_db_connection()
            cursor = conn.cursor()

            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

            # 更新/建立 licenses 記錄，並設為 active
            if use_postgresql:
                try:
                    cursor.execute(
                        """
                        INSERT INTO licenses (user_id, tier, seats, expires_at, status, updated_at)
                        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id)
                        DO UPDATE SET
                            tier = EXCLUDED.tier,
                            expires_at = EXCLUDED.expires_at,
                            status = EXCLUDED.status,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (user_id, plan, 1, expires_dt, "active")
                    )
                except Exception as e:
                    # 若 licenses 不存在，忽略而不阻擋主流程
                    print("WARN: update licenses failed:", e)
            else:
                try:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO licenses
                        (user_id, tier, seats, expires_at, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (user_id, plan, 1, expires_dt.timestamp(), "active")
                    )
                except Exception as e:
                    print("WARN: update licenses failed:", e)

            # 將 user 設為已訂閱
            if use_postgresql:
                cursor.execute(
                    "UPDATE user_auth SET is_subscribed = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                    (user_id,)
                )
            else:
                cursor.execute(
                    "UPDATE user_auth SET is_subscribed = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (user_id,)
                )

            # 可選：記錄訂單（若有 orders 表）
            try:
                if use_postgresql:
                    cursor.execute(
                        """
                        INSERT INTO orders (user_id, plan_type, amount, payment_status, paid_at, invoice_number, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        """,
                        (user_id, plan, amount, "paid", paid_at, transaction_id)
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO orders (user_id, plan_type, amount, payment_status, paid_at, invoice_number, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (user_id, plan, amount, "paid", paid_at, transaction_id)
                    )
            except Exception as e:
                print("WARN: insert orders failed:", e)

            if not use_postgresql:
                conn.commit()
            conn.close()

            return {"ok": True, "user_id": user_id, "plan": plan, "expires_at": expires_dt.isoformat()}
        except HTTPException:
            raise
        except Exception as e:
            print("payment_callback error:", e)
            raise HTTPException(status_code=500, detail="payment callback failed")

    @app.post("/api/auth/google/callback")
    async def google_callback_post(request: dict):
        """處理 Google OAuth 回調（POST 請求 - 來自前端 JavaScript）"""
        try:
            # 從請求體獲取授權碼
            code = request.get("code")
            if not code:
                raise HTTPException(status_code=400, detail="Missing authorization code")
            
            # 交換授權碼獲取訪問令牌
            async with httpx.AsyncClient() as client:
                token_response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": GOOGLE_REDIRECT_URI,
                    }
                )
                
                if token_response.status_code != 200:
                    raise HTTPException(status_code=400, detail="Failed to get access token")
                
                token_data = token_response.json()
                access_token = token_data["access_token"]
                
                # 獲取用戶資訊
                google_user = await get_google_user_info(access_token)
                if not google_user:
                    raise HTTPException(status_code=400, detail="Failed to get user info")
                
                # 生成用戶 ID
                user_id = generate_user_id(google_user.email)
                
                # 保存或更新用戶認證資訊
                conn = get_db_connection()
                cursor = conn.cursor()
                
                database_url = os.getenv("DATABASE_URL")
                use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
                
                if use_postgresql:
                    # PostgreSQL 語法
                    from datetime import timedelta
                    expires_at_value = datetime.now() + timedelta(seconds=token_data.get("expires_in", 3600))
                    
                    cursor.execute("""
                        INSERT INTO user_auth 
                        (user_id, google_id, email, name, picture, access_token, expires_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) 
                        DO UPDATE SET 
                            google_id = EXCLUDED.google_id,
                            email = EXCLUDED.email,
                            name = EXCLUDED.name,
                            picture = EXCLUDED.picture,
                            access_token = EXCLUDED.access_token,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = CURRENT_TIMESTAMP
                    """, (
                        user_id,
                        google_user.id,
                        google_user.email,
                        google_user.name,
                        google_user.picture,
                        access_token,
                        expires_at_value
                    ))
                else:
                    # SQLite 語法
                    cursor.execute("""
                        INSERT OR REPLACE INTO user_auth 
                        (user_id, google_id, email, name, picture, access_token, expires_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (
                        user_id,
                        google_user.id,
                        google_user.email,
                        google_user.name,
                        google_user.picture,
                        access_token,
                        datetime.now().timestamp() + token_data.get("expires_in", 3600)
                    ))
                
                if not use_postgresql:
                    conn.commit()
                conn.close()
                
                # 生成應用程式訪問令牌
                app_access_token = generate_access_token(user_id)
                
                # 返回 JSON 格式（給前端 JavaScript 使用）
                return AuthToken(
                    access_token=app_access_token,
                    expires_in=3600,
                    user=google_user
                )
                
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/auth/refresh")
    async def refresh_token(
        current_user_id: Optional[str] = Depends(get_current_user_for_refresh)
    ):
        """刷新存取權杖（允許使用過期的 token）"""
        print(f"DEBUG: refresh_token - current_user_id={current_user_id}")
        if not current_user_id:
            print("DEBUG: refresh_token - current_user_id 為 None，返回 401")
            raise HTTPException(status_code=401, detail="未授權")
        print(f"DEBUG: refresh_token - 開始處理 refresh，user_id={current_user_id}")
        
        try:
            # 獲取資料庫連接
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 從資料庫獲取用戶的 refresh token（如果需要）
            # 但實際上我們直接生成新的 access token
            if use_postgresql:
                cursor.execute("SELECT user_id FROM user_auth WHERE user_id = %s", (current_user_id,))
            else:
                cursor.execute("SELECT user_id FROM user_auth WHERE user_id = ?", (current_user_id,))
            
            if not cursor.fetchone():
                conn.close()
                raise HTTPException(status_code=404, detail="用戶不存在")
            
            # 生成新的 access token
            new_access_token = generate_access_token(current_user_id)
            new_expires_at = datetime.now() + timedelta(hours=1)
            
            # 更新資料庫中的 token
            if use_postgresql:
                cursor.execute("""
                    UPDATE user_auth 
                    SET access_token = %s, expires_at = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                """, (new_access_token, new_expires_at, current_user_id))
            else:
                cursor.execute("""
                    UPDATE user_auth 
                    SET access_token = ?, expires_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (new_access_token, new_expires_at.isoformat(), current_user_id))
                conn.commit()
            
            conn.close()
            
            return {
                "access_token": new_access_token,
                "expires_at": new_expires_at.isoformat()
            }
                
        except HTTPException:
            raise
        except Exception as e:
            print(f"刷新 token 錯誤: {e}")
            raise HTTPException(status_code=500, detail="內部伺服器錯誤")

    @app.get("/api/auth/me")
    async def get_current_user_info(request: Request, current_user_id: Optional[str] = Depends(get_current_user)):
        """獲取當前用戶資訊"""
        if not current_user_id:
            # 兼容處理：若依賴鏈沒有取到 credentials，改從 Header 直接解析一次
            try:
                auth_header = request.headers.get("authorization", "") or ""
                auth_preview = auth_header[:40]
                print(f"[auth/me] missing user(dep), Authorization preview='{auth_preview}'")
                token = None
                if auth_header.lower().startswith("bearer "):
                    token = auth_header.split(" ", 1)[1].strip()
                if token:
                    uid = verify_access_token(token, allow_expired=False)
                    print(f"[auth/me] manual verify result user_id={uid}")
                    current_user_id = uid
            except Exception as _e:
                print(f"[auth/me] manual verify error: {_e}")
            if not current_user_id:
                raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT google_id, email, name, picture, is_subscribed, created_at 
                    FROM user_auth 
                    WHERE user_id = %s
                """, (current_user_id,))
            else:
                cursor.execute("""
                    SELECT google_id, email, name, picture, is_subscribed, created_at 
                    FROM user_auth 
                    WHERE user_id = ?
                """, (current_user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                # 格式化日期（台灣時區 UTC+8）
                created_at = row[5]
                if created_at:
                    try:
                        from datetime import timezone, timedelta
                        if isinstance(created_at, datetime):
                            # 如果是 datetime 對象，直接使用
                            dt = created_at
                        elif isinstance(created_at, str):
                            # 如果是字符串，解析它
                            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        else:
                            dt = None
                        
                        if dt:
                            # 轉換為台灣時區 (UTC+8)
                            taiwan_tz = timezone(timedelta(hours=8))
                            if dt.tzinfo is None:
                                # 如果沒有時區信息，假設是 UTC
                                dt = dt.replace(tzinfo=timezone.utc)
                            dt_taiwan = dt.astimezone(taiwan_tz)
                            created_at = dt_taiwan.strftime('%Y/%m/%d %H:%M')
                    except Exception as e:
                        print(f"格式化日期時出錯: {e}")
                        pass
                
                return {
                    "user_id": current_user_id,
                    "google_id": row[0],
                    "email": row[1],
                    "name": row[2],
                    "picture": row[3],
                    "is_subscribed": bool(row[4]) if row[4] is not None else True,  # 預設為已訂閱
                    "created_at": created_at
                }
            else:
                raise HTTPException(status_code=404, detail="User not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/auth/logout")
    async def logout(current_user_id: Optional[str] = Depends(get_current_user)):
        """登出用戶"""
        if not current_user_id:
            return {"message": "Already logged out"}
        
        # 這裡可以添加令牌黑名單邏輯
        return {"message": "Logged out successfully"}

    # ===== P0 功能：長期記憶＋個人化 =====
    
    @app.get("/api/profile/{user_id}")
    async def get_user_profile(user_id: str):
        """獲取用戶個人偏好"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("SELECT * FROM user_profiles WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    "user_id": row[0],
                    "preferred_platform": row[1],
                    "preferred_style": row[2],
                    "preferred_duration": row[3],
                    "content_preferences": json.loads(row[4]) if row[4] else None,
                    "created_at": row[5],
                    "updated_at": row[6]
                }
            else:
                return {"message": "Profile not found", "user_id": user_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/profile")
    async def create_or_update_profile(profile: UserProfile):
        """創建或更新用戶個人偏好"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 檢查是否已存在
            if use_postgresql:
                cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = %s", (profile.user_id,))
            else:
                cursor.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (profile.user_id,))
            exists = cursor.fetchone()
            
            if exists:
                # 更新現有記錄
                if use_postgresql:
                    cursor.execute("""
                        UPDATE user_profiles 
                        SET preferred_platform = %s, preferred_style = %s, preferred_duration = %s, 
                            content_preferences = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = %s
                    """, (
                        profile.preferred_platform,
                        profile.preferred_style,
                        profile.preferred_duration,
                        json.dumps(profile.content_preferences) if profile.content_preferences else None,
                        profile.user_id
                    ))
                else:
                    cursor.execute("""
                        UPDATE user_profiles 
                        SET preferred_platform = ?, preferred_style = ?, preferred_duration = ?, 
                            content_preferences = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ?
                    """, (
                        profile.preferred_platform,
                        profile.preferred_style,
                        profile.preferred_duration,
                        json.dumps(profile.content_preferences) if profile.content_preferences else None,
                        profile.user_id
                    ))
            else:
                # 創建新記錄
                if use_postgresql:
                    cursor.execute("""
                        INSERT INTO user_profiles 
                        (user_id, preferred_platform, preferred_style, preferred_duration, content_preferences)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        profile.user_id,
                        profile.preferred_platform,
                        profile.preferred_style,
                        profile.preferred_duration,
                        json.dumps(profile.content_preferences) if profile.content_preferences else None
                    ))
                else:
                    cursor.execute("""
                        INSERT INTO user_profiles 
                        (user_id, preferred_platform, preferred_style, preferred_duration, content_preferences)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        profile.user_id,
                        profile.preferred_platform,
                        profile.preferred_style,
                        profile.preferred_duration,
                        json.dumps(profile.content_preferences) if profile.content_preferences else None
                    ))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            return {"message": "Profile saved successfully", "user_id": profile.user_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/generations")
    async def save_generation(generation: Generation):
        """保存生成內容並檢查去重"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 生成去重哈希
            dedup_hash = generate_dedup_hash(
                generation.content, 
                generation.platform, 
                generation.topic
            )
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            # 檢查是否已存在相同內容
            if use_postgresql:
                cursor.execute("SELECT id FROM generations WHERE dedup_hash = %s", (dedup_hash,))
            else:
                cursor.execute("SELECT id FROM generations WHERE dedup_hash = ?", (dedup_hash,))
            existing = cursor.fetchone()
            
            if existing:
                return {
                    "message": "Similar content already exists",
                    "generation_id": existing[0],
                    "dedup_hash": dedup_hash,
                    "is_duplicate": True
                }
            
            # 生成新的 ID
            generation_id = hashlib.md5(f"{generation.user_id}_{datetime.now().isoformat()}".encode()).hexdigest()[:12]
            
            # 保存新生成內容
            if use_postgresql:
                cursor.execute("""
                    INSERT INTO generations (id, user_id, content, platform, topic, dedup_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    generation_id,
                    generation.user_id,
                    generation.content,
                    generation.platform,
                    generation.topic,
                    dedup_hash
                ))
            else:
                cursor.execute("""
                    INSERT INTO generations (id, user_id, content, platform, topic, dedup_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    generation_id,
                    generation.user_id,
                    generation.content,
                    generation.platform,
                    generation.topic,
                    dedup_hash
                ))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {
                "message": "Generation saved successfully",
                "generation_id": generation_id,
                "dedup_hash": dedup_hash,
                "is_duplicate": False
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/generations/{user_id}")
    async def get_user_generations(user_id: str, limit: int = 10):
        """獲取用戶的生成歷史"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT id, content, platform, topic, created_at 
                    FROM generations 
                    WHERE user_id = %s 
                    ORDER BY created_at DESC 
                    LIMIT %s
                """, (user_id, limit))
            else:
                cursor.execute("""
                    SELECT id, content, platform, topic, created_at 
                    FROM generations 
                    WHERE user_id = ? 
                    ORDER BY created_at DESC 
                    LIMIT ?
                """, (user_id, limit))
            
            rows = cursor.fetchall()
            conn.close()
            
            generations = []
            for row in rows:
                generations.append({
                    "id": row[0],
                    "content": row[1],
                    "platform": row[2],
                    "topic": row[3],
                    "created_at": row[4]
                })
            
            return {"generations": generations, "count": len(generations)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/conversation/summary")
    async def create_conversation_summary(user_id: str, messages: List[ChatMessage]):
        """創建對話摘要"""
        try:
            if not os.getenv("GEMINI_API_KEY"):
                return {"error": "Gemini API not configured"}
            
            # 準備對話內容
            conversation_text = "\n".join([f"{msg.role}: {msg.content}" for msg in messages])
            
            # 使用 Gemini 生成摘要
            model = genai.GenerativeModel(model_name)
            prompt = f"""
            請為以下對話生成一個簡潔的摘要（不超過100字），重點關注：
            1. 用戶的主要需求和偏好
            2. 討論的平台和主題
            3. 重要的風格要求
            
            對話內容：
            {conversation_text}
            """
            
            response = model.generate_content(prompt)
            summary = response.text if response else "無法生成摘要"
            
            # 保存到數據庫
            conn = get_db_connection()
            cursor = conn.cursor()

            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE

            message_cnt = len(messages)

            if use_postgresql:
                # PostgreSQL upsert：以 (user_id, created_at, summary) 近似去重，避免重複
                cursor.execute("""
                    INSERT INTO conversation_summaries (user_id, summary, conversation_type, created_at, message_count, updated_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """, (
                    user_id, summary, classify_conversation(user_message=messages[-1].content if messages else "", ai_response=summary), datetime.now(), message_cnt
                ))
            else:
                cursor.execute("""
                    INSERT OR REPLACE INTO conversation_summaries 
                    (user_id, summary, message_count, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (user_id, summary, message_cnt))
            
            if not use_postgresql:
                conn.commit()
            conn.close()
            
            return {
                "message": "Conversation summary created",
                "summary": summary,
                "message_count": message_cnt
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/conversation/summary/{user_id}")
    async def get_conversation_summary(user_id: str):
        """獲取用戶的對話摘要"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT summary, message_count, created_at, updated_at 
                FROM conversation_summaries 
                WHERE user_id = ?
            """, (user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    "user_id": user_id,
                    "summary": row[0],
                    "message_count": row[1],
                    "created_at": row[2],
                    "updated_at": row[3]
                }
            else:
                return {"message": "No conversation summary found", "user_id": user_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ============ 帳單資訊相關 API ============

    @app.get("/api/user/orders/{user_id}")
    async def get_user_orders(user_id: str, current_user_id: Optional[str] = Depends(get_current_user)):
        """獲取用戶的購買記錄"""
        if current_user_id != user_id:
            return JSONResponse({"error": "無權限訪問此用戶資料"}, status_code=403)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT id, order_id, plan_type, amount, currency, payment_method, 
                           payment_status, paid_at, expires_at, invoice_number, 
                           invoice_type, created_at
                    FROM orders 
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT id, order_id, plan_type, amount, currency, payment_method, 
                           payment_status, paid_at, expires_at, invoice_number, 
                           invoice_type, created_at
                    FROM orders 
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
            
            rows = cursor.fetchall()
            conn.close()
            
            orders = []
            for row in rows:
                orders.append({
                    "id": row[0],
                    "order_id": row[1],
                    "plan_type": row[2],
                    "amount": row[3],
                    "currency": row[4],
                    "payment_method": row[5],
                    "payment_status": row[6],
                    "paid_at": row[7],
                    "expires_at": row[8],
                    "invoice_number": row[9],
                    "invoice_type": row[10],
                    "created_at": row[11]
                })
            
            return {"orders": orders}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/user/license/{user_id}")
    async def get_user_license(user_id: str, current_user_id: Optional[str] = Depends(get_current_user)):
        """獲取用戶的授權資訊"""
        if current_user_id != user_id:
            return JSONResponse({"error": "無權限訪問此用戶資料"}, status_code=403)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT tier, seats, source, start_at, expires_at, status
                    FROM licenses 
                    WHERE user_id = %s AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT tier, seats, source, start_at, expires_at, status
                    FROM licenses 
                    WHERE user_id = ? AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    "user_id": user_id,
                    "tier": row[0],
                    "seats": row[1],
                    "source": row[2],
                    "start_at": str(row[3]),
                    "expires_at": str(row[4]),
                    "status": row[5]
                }
            else:
                return {"user_id": user_id, "tier": "none", "expires_at": None}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/admin/orders")
    async def get_all_orders():
        """獲取所有訂單記錄（管理員用）"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            database_url = os.getenv("DATABASE_URL")
            use_postgresql = database_url and "postgresql://" in database_url and PSYCOPG2_AVAILABLE
            
            if use_postgresql:
                cursor.execute("""
                    SELECT o.id, o.user_id, o.order_id, o.plan_type, o.amount, 
                           o.currency, o.payment_method, o.payment_status, 
                           o.paid_at, o.expires_at, o.invoice_number, o.created_at,
                           ua.name, ua.email
                    FROM orders o
                    LEFT JOIN user_auth ua ON o.user_id = ua.user_id
                    ORDER BY o.created_at DESC
                    LIMIT 100
                """)
            else:
                cursor.execute("""
                    SELECT o.id, o.user_id, o.order_id, o.plan_type, o.amount, 
                           o.currency, o.payment_method, o.payment_status, 
                           o.paid_at, o.expires_at, o.invoice_number, o.created_at,
                           ua.name, ua.email
                    FROM orders o
                    LEFT JOIN user_auth ua ON o.user_id = ua.user_id
                    ORDER BY o.created_at DESC
                    LIMIT 100
                """)
            
            orders = []
            for row in cursor.fetchall():
                orders.append({
                    "id": row[0],
                    "user_id": row[1],
                    "order_id": row[2],
                    "plan_type": row[3],
                    "amount": row[4],
                    "currency": row[5],
                    "payment_method": row[6],
                    "payment_status": row[7],
                    "paid_at": row[8],
                    "expires_at": row[9],
                    "invoice_number": row[10],
                    "created_at": row[11],
                    "user_name": row[12] or "未知用戶",
                    "user_email": row[13] or ""
                })
            
            conn.close()
            return {"orders": orders}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return app

app = create_app()

# 注意：在 Zeabur 部署時，使用 Dockerfile 中的 uvicorn 命令啟動
# 這個區塊主要用於本地開發
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"INFO: Starting Uvicorn locally on host=0.0.0.0, port={port}")
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="info",
        access_log=True,
        workers=1
    )


