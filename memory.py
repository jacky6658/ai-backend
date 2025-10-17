import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional

class MemoryManager:
    def __init__(self, db_path: str = "db.sqlite3"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
    
    def add_message(self, user_id: str, role: str, content: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
    
    def get_recent_messages(self, user_id: str, limit: int = 20) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT role, content FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            )
            messages = [{"role": row[0], "content": row[1]} for row in cursor.fetchall()]
            return list(reversed(messages))
    
    def get_summary(self, user_id: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT content FROM summaries WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    
    def update_summary(self, user_id: str, content: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO summaries (user_id, content) VALUES (?, ?)",
                (user_id, content)
            )
    
    def should_summarize(self, user_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                (user_id,)
            )
            count = cursor.fetchone()[0]
            return count % 20 == 0 and count > 0
