"""
AI Points Integration - 點數系統整合到主應用
不修改既有代碼，只添加新的路由和功能
"""

from fastapi import FastAPI
from .points_routes import register_points_routes
from .points_system import points_system
import asyncio
from datetime import datetime, timedelta

def integrate_points_system(app: FastAPI):
    """整合點數系統到主應用"""
    
    # 註冊路由
    register_points_routes(app)
    
    # 添加定時任務
    app.add_event_handler("startup", start_points_scheduler)
    app.add_event_handler("shutdown", stop_points_scheduler)
    
    print("AI Points System integrated successfully")

# 定時任務相關
scheduler_task = None

async def start_points_scheduler():
    """啟動點數系統定時任務"""
    global scheduler_task
    scheduler_task = asyncio.create_task(daily_points_tasks())
    print("Points system scheduler started")

async def stop_points_scheduler():
    """停止點數系統定時任務"""
    global scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    print("Points system scheduler stopped")

async def daily_points_tasks():
    """每日定時任務"""
    while True:
        try:
            # 等待到凌晨3點
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            
            wait_seconds = (next_run - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            
            # 執行每日任務
            await execute_daily_tasks()
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Daily points task error: {e}")
            # 出錯時等待1小時再重試
            await asyncio.sleep(3600)

async def execute_daily_tasks():
    """執行每日任務"""
    try:
        print(f"Executing daily points tasks at {datetime.now()}")
        
        # 1. 到期清理
        points_system.expire_sweep()
        
        # 2. 月贈點（如果需要）
        await grant_monthly_points()
        
        # 3. 發送到期提醒（如果需要）
        await send_expiration_notifications()
        
        print("Daily points tasks completed")
        
    except Exception as e:
        print(f"Error executing daily tasks: {e}")

async def grant_monthly_points():
    """發放月贈點"""
    try:
        # 這裡需要根據您的訂閱系統實現
        # 檢查有效訂閱用戶並發放月贈點
        pass
    except Exception as e:
        print(f"Error granting monthly points: {e}")

async def send_expiration_notifications():
    """發送到期提醒"""
    try:
        # 這裡可以實現郵件或推播通知
        # 提醒用戶點數即將到期
        pass
    except Exception as e:
        print(f"Error sending expiration notifications: {e}")

# 中間件：自動扣點
async def points_middleware(request, call_next):
    """點數系統中間件（可選）"""
    response = await call_next(request)
    
    # 這裡可以添加自動扣點邏輯
    # 例如：在特定API調用後自動扣點
    
    return response

# 工具函數
def add_points_to_user(user_id: str, points: int, reason: str = "gift"):
    """為用戶添加點數（管理員用）"""
    from .points_system import PointReason
    points_system.add_points(
        user_id=user_id,
        points=points,
        reason=PointReason(reason),
        ref_id="admin_gift"
    )

def get_user_points(user_id: str) -> int:
    """獲取用戶點數餘額"""
    wallet_info = points_system.get_wallet_info(user_id)
    return wallet_info["balance"]

def consume_user_points(user_id: str, module: str, mode: str, count: int) -> bool:
    """消費用戶點數"""
    return points_system.consume_points(user_id, module, mode, count)
