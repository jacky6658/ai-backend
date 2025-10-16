"""
AI Points System - 點數訂閱制獨立模組
不修改既有系統，僅新增 /points/* 和 /plans/* 命名空間
"""

import sqlite3
import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

# 點數系統配置
POINTS_CONFIG = {
    "FREE_QUOTA_PER_MODULE": 10,  # 每個模組每月免費額度
    "POINTS_PER_ONE_CLICK": 2,    # 一鍵生成每次扣點
    "POINTS_PER_CHAT": 1,         # 聊天每次扣點
    "AUTO_TOPUP_THRESHOLD": 20,   # 自動補點閾值
    "CARRYOVER_RATE": 0.3,        # 結轉比例
}

class PointReason(Enum):
    PURCHASE = "purchase"
    DEDUCT = "deduct"
    REFUND = "refund"
    EXPIRE = "expire"
    GIFT = "gift"
    MONTHLY_GRANT = "monthly_grant"
    CARRYOVER = "carryover"

class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"

@dataclass
class PointPack:
    pack_id: int
    name: str
    points: int
    price_ntd: int
    valid_days: int
    is_active: bool

@dataclass
class Plan:
    plan_id: int
    name: str
    monthly_points: int
    batch_limit: int
    roles_limit: int
    is_active: bool

class PointsSystem:
    def __init__(self, db_path: str = "ai_points.db"):
        self.db_path = db_path
        self.init_database()
    
    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """初始化點數系統資料表"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 點數包表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS point_packs (
                pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL,
                price_ntd INTEGER NOT NULL,
                valid_days INTEGER DEFAULT 180,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 點數訂單表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS point_orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                pack_id INTEGER NOT NULL,
                price_paid INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                provider TEXT DEFAULT 'manual',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                paid_at DATETIME,
                FOREIGN KEY (pack_id) REFERENCES point_packs (pack_id)
            )
        """)
        
        # 點數錢包表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS point_wallets (
                user_id TEXT PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                auto_topup_enabled BOOLEAN DEFAULT 0,
                auto_topup_pack_id INTEGER,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (auto_topup_pack_id) REFERENCES point_packs (pack_id)
            )
        """)
        
        # 點數帳本表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS point_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ref_id TEXT,
                expire_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 訂閱方案表（只讀查詢來源）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                monthly_points INTEGER DEFAULT 0,
                batch_limit INTEGER DEFAULT 10,
                roles_limit INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 訂閱記錄表（只讀查詢來源）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                plan_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                renew_at DATETIME,
                status TEXT DEFAULT 'active',
                metadata TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (plan_id, user_id),
                FOREIGN KEY (plan_id) REFERENCES plans (plan_id)
            )
        """)
        
        # 免費額度使用記錄
        cur.execute("""
            CREATE TABLE IF NOT EXISTS free_quota_usage (
                user_id TEXT NOT NULL,
                module TEXT NOT NULL,
                usage_date DATE NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, module, usage_date)
            )
        """)
        
        # 建立索引
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user_expire ON point_ledger(user_id, expire_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user_reason ON point_ledger(user_id, reason)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_status ON point_orders(user_id, status)")
        
        # 插入預設點數包
        self._insert_default_packs(cur)
        
        # 插入預設方案
        self._insert_default_plans(cur)
        
        conn.commit()
        conn.close()
    
    def _insert_default_packs(self, cur):
        """插入預設點數包"""
        default_packs = [
            ("小額包", 300, 399, 180),
            ("標準包", 1000, 1099, 180),
            ("大額包", 3000, 3399, 180),
        ]
        
        for name, points, price, valid_days in default_packs:
            cur.execute("""
                INSERT OR IGNORE INTO point_packs (name, points, price_ntd, valid_days)
                VALUES (?, ?, ?, ?)
            """, (name, points, price, valid_days))
    
    def _insert_default_plans(self, cur):
        """插入預設訂閱方案"""
        default_plans = [
            ("基礎方案", 100, 10, 1),
            ("專業方案", 500, 50, 3),
            ("企業方案", 2000, 200, 10),
        ]
        
        for name, monthly_points, batch_limit, roles_limit in default_plans:
            cur.execute("""
                INSERT OR IGNORE INTO plans (name, monthly_points, batch_limit, roles_limit)
                VALUES (?, ?, ?, ?)
            """, (name, monthly_points, batch_limit, roles_limit))
    
    def get_wallet_info(self, user_id: str) -> Dict:
        """獲取錢包資訊"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 獲取餘額
        wallet = cur.execute(
            "SELECT balance, auto_topup_enabled FROM point_wallets WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        
        if not wallet:
            # 初始化錢包
            cur.execute(
                "INSERT INTO point_wallets (user_id, balance) VALUES (?, 0)",
                (user_id,)
            )
            balance = 0
            auto_topup_enabled = False
        else:
            balance = wallet["balance"]
            auto_topup_enabled = wallet["auto_topup_enabled"]
        
        # 獲取即將到期的點數
        expiring_soon = cur.execute("""
            SELECT SUM(delta) as expiring_points
            FROM point_ledger 
            WHERE user_id = ? AND delta > 0 AND expire_at BETWEEN ? AND ?
        """, (user_id, datetime.now(), datetime.now() + timedelta(days=7))).fetchone()
        
        conn.close()
        
        return {
            "balance": balance,
            "auto_topup_enabled": auto_topup_enabled,
            "expiring_soon": expiring_soon["expiring_points"] or 0
        }
    
    def get_point_packs(self) -> List[PointPack]:
        """獲取可用的點數包"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        packs = cur.execute(
            "SELECT * FROM point_packs WHERE is_active = 1 ORDER BY points ASC"
        ).fetchall()
        
        conn.close()
        
        return [PointPack(**dict(pack)) for pack in packs]
    
    def get_plans(self) -> List[Plan]:
        """獲取訂閱方案（只讀）"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        plans = cur.execute(
            "SELECT * FROM plans WHERE is_active = 1 ORDER BY monthly_points ASC"
        ).fetchall()
        
        conn.close()
        
        return [Plan(**dict(plan)) for plan in plans]
    
    def authorize_usage(self, user_id: str, module: str, mode: str, count: int) -> Dict:
        """授權使用（不扣點，只判斷）"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 1. 檢查免費額度
        today = datetime.now().date()
        free_usage = cur.execute("""
            SELECT count FROM free_quota_usage 
            WHERE user_id = ? AND module = ? AND usage_date = ?
        """, (user_id, module, today)).fetchone()
        
        used_free = free_usage["count"] if free_usage else 0
        remaining_free = max(0, POINTS_CONFIG["FREE_QUOTA_PER_MODULE"] - used_free)
        
        if remaining_free >= count:
            conn.close()
            return {
                "authorized": True,
                "cost": 0,
                "reason": "OK",
                "needTopup": False,
                "suggestPackIds": []
            }
        
        # 2. 計算需要扣的點數
        if mode == "oneclick":
            points_needed = POINTS_CONFIG["POINTS_PER_ONE_CLICK"] * count
        else:  # chat
            points_needed = POINTS_CONFIG["POINTS_PER_CHAT"] * count
        
        # 3. 檢查訂閱方案限制
        subscription = cur.execute("""
            SELECT s.*, p.batch_limit, p.roles_limit
            FROM subscriptions s
            JOIN plans p ON s.plan_id = p.plan_id
            WHERE s.user_id = ? AND s.status = 'active'
        """, (user_id,)).fetchone()
        
        if subscription and count > subscription["batch_limit"]:
            conn.close()
            return {
                "authorized": False,
                "cost": points_needed,
                "reason": "UPGRADE_REQUIRED",
                "needTopup": False,
                "suggestPackIds": []
            }
        
        # 4. 檢查點數餘額
        wallet = cur.execute(
            "SELECT balance FROM point_wallets WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        
        balance = wallet["balance"] if wallet else 0
        
        if balance >= points_needed:
            conn.close()
            return {
                "authorized": True,
                "cost": points_needed,
                "reason": "OK",
                "needTopup": False,
                "suggestPackIds": []
            }
        
        # 5. 需要補點
        suggest_packs = cur.execute("""
            SELECT pack_id FROM point_packs 
            WHERE is_active = 1 AND points >= ? 
            ORDER BY points ASC LIMIT 3
        """, (points_needed,)).fetchall()
        
        conn.close()
        
        return {
            "authorized": False,
            "cost": points_needed,
            "reason": "INSUFFICIENT_POINTS",
            "needTopup": True,
            "suggestPackIds": [pack["pack_id"] for pack in suggest_packs]
        }
    
    def create_checkout(self, user_id: str, pack_id: int) -> Dict:
        """創建點數包訂單"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 獲取點數包資訊
        pack = cur.execute(
            "SELECT * FROM point_packs WHERE pack_id = ? AND is_active = 1",
            (pack_id,)
        ).fetchone()
        
        if not pack:
            conn.close()
            return {"error": "點數包不存在"}
        
        # 創建訂單
        order_id = cur.execute("""
            INSERT INTO point_orders (user_id, pack_id, price_paid, status)
            VALUES (?, ?, ?, ?)
        """, (user_id, pack_id, pack["price_ntd"], OrderStatus.PENDING.value)).lastrowid
        
        conn.commit()
        conn.close()
        
        return {
            "order_id": order_id,
            "checkout_url": f"/points/checkout/{order_id}",
            "amount": pack["price_ntd"],
            "points": pack["points"]
        }
    
    def process_payment(self, order_id: int, provider: str = "manual") -> bool:
        """處理付款成功（金流回調）"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 獲取訂單資訊
        order = cur.execute("""
            SELECT o.*, p.points, p.valid_days
            FROM point_orders o
            JOIN point_packs p ON o.pack_id = p.pack_id
            WHERE o.order_id = ? AND o.status = 'pending'
        """, (order_id,)).fetchone()
        
        if not order:
            conn.close()
            return False
        
        # 更新訂單狀態
        cur.execute("""
            UPDATE point_orders 
            SET status = ?, provider = ?, paid_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
        """, (OrderStatus.PAID.value, provider, order_id))
        
        # 添加點數到帳本
        expire_at = datetime.now() + timedelta(days=order["valid_days"])
        cur.execute("""
            INSERT INTO point_ledger (user_id, delta, reason, ref_id, expire_at)
            VALUES (?, ?, ?, ?, ?)
        """, (order["user_id"], order["points"], PointReason.PURCHASE.value, str(order_id), expire_at))
        
        # 更新錢包餘額
        self._update_wallet_balance(cur, order["user_id"])
        
        conn.commit()
        conn.close()
        return True
    
    def consume_points(self, user_id: str, module: str, mode: str, count: int) -> bool:
        """實際扣點（在既有流程完成後調用）"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 1. 先使用免費額度
        today = datetime.now().date()
        free_usage = cur.execute("""
            SELECT count FROM free_quota_usage 
            WHERE user_id = ? AND module = ? AND usage_date = ?
        """, (user_id, module, today)).fetchone()
        
        used_free = free_usage["count"] if free_usage else 0
        remaining_free = max(0, POINTS_CONFIG["FREE_QUOTA_PER_MODULE"] - used_free)
        
        if remaining_free > 0:
            # 更新免費額度使用記錄
            free_count = min(remaining_free, count)
            cur.execute("""
                INSERT OR REPLACE INTO free_quota_usage (user_id, module, usage_date, count)
                VALUES (?, ?, ?, ?)
            """, (user_id, module, today, used_free + free_count))
            
            count -= free_count
            if count == 0:
                conn.commit()
                conn.close()
                return True
        
        # 2. 計算需要扣的點數
        if mode == "oneclick":
            points_needed = POINTS_CONFIG["POINTS_PER_ONE_CLICK"] * count
        else:  # chat
            points_needed = POINTS_CONFIG["POINTS_PER_CHAT"] * count
        
        # 3. 扣點
        success = self._deduct_points(cur, user_id, points_needed)
        
        if success:
            conn.commit()
        
        conn.close()
        return success
    
    def _deduct_points(self, cur, user_id: str, points_needed: int) -> bool:
        """扣點邏輯（最早到期優先）"""
        # 獲取可用的正數分錄（按到期時間排序）
        available_ledgers = cur.execute("""
            SELECT id, delta FROM point_ledger 
            WHERE user_id = ? AND delta > 0 AND expire_at > CURRENT_TIMESTAMP
            ORDER BY expire_at ASC
        """, (user_id,)).fetchall()
        
        remaining = points_needed
        
        for ledger in available_ledgers:
            if remaining <= 0:
                break
            
            deduct_amount = min(remaining, ledger["delta"])
            
            # 添加負數分錄
            cur.execute("""
                INSERT INTO point_ledger (user_id, delta, reason, ref_id)
                VALUES (?, ?, ?, ?)
            """, (user_id, -deduct_amount, PointReason.DEDUCT.value, str(ledger["id"])))
            
            # 更新原分錄
            cur.execute("""
                UPDATE point_ledger 
                SET delta = delta - ? 
                WHERE id = ?
            """, (deduct_amount, ledger["id"]))
            
            remaining -= deduct_amount
        
        if remaining > 0:
            return False  # 餘額不足
        
        # 更新錢包餘額
        self._update_wallet_balance(cur, user_id)
        return True
    
    def _update_wallet_balance(self, cur, user_id: str):
        """更新錢包餘額"""
        # 計算當前餘額
        balance = cur.execute("""
            SELECT COALESCE(SUM(delta), 0) as balance
            FROM point_ledger 
            WHERE user_id = ? AND expire_at > CURRENT_TIMESTAMP
        """, (user_id,)).fetchone()["balance"]
        
        # 更新錢包
        cur.execute("""
            INSERT OR REPLACE INTO point_wallets (user_id, balance, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (user_id, balance))
    
    def add_points(self, user_id: str, points: int, reason: PointReason, ref_id: str = None, expire_days: int = 180):
        """添加點數"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        expire_at = datetime.now() + timedelta(days=expire_days)
        
        cur.execute("""
            INSERT INTO point_ledger (user_id, delta, reason, ref_id, expire_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, points, reason.value, ref_id, expire_at))
        
        self._update_wallet_balance(cur, user_id)
        conn.commit()
        conn.close()
    
    def expire_sweep(self):
        """到期清理（每日排程）"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # 處理到期的點數
        expired_ledgers = cur.execute("""
            SELECT id, user_id, delta FROM point_ledger 
            WHERE delta > 0 AND expire_at <= CURRENT_TIMESTAMP
        """).fetchall()
        
        for ledger in expired_ledgers:
            # 添加到期分錄
            cur.execute("""
                INSERT INTO point_ledger (user_id, delta, reason, ref_id)
                VALUES (?, ?, ?, ?)
            """, (ledger["user_id"], -ledger["delta"], PointReason.EXPIRE.value, str(ledger["id"])))
            
            # 更新錢包餘額
            self._update_wallet_balance(cur, ledger["user_id"])
        
        conn.commit()
        conn.close()
    
    def toggle_auto_topup(self, user_id: str, enabled: bool, pack_id: int = None):
        """切換自動補點"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        cur.execute("""
            INSERT OR REPLACE INTO point_wallets (user_id, auto_topup_enabled, auto_topup_pack_id)
            VALUES (?, ?, ?)
        """, (user_id, enabled, pack_id))
        
        conn.commit()
        conn.close()

# 全域實例
points_system = PointsSystem()
