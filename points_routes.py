"""
AI Points API Routes - 點數系統API路由
新增 /points/* 和 /plans/* 命名空間，不影響既有路由
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
import json
from .points_system import points_system, PointReason

# 創建路由器
points_router = APIRouter(prefix="/points", tags=["points"])
plans_router = APIRouter(prefix="/plans", tags=["plans"])

# 請求模型
class AuthorizeRequest(BaseModel):
    module: str  # '定位'|'選題'|'腳本'
    mode: str    # 'oneclick'|'chat'
    count: int

class CheckoutRequest(BaseModel):
    pack_id: int

class ConsumeRequest(BaseModel):
    usage_id: str
    module: str
    mode: str
    count: int
    points: int

class SettingsRequest(BaseModel):
    auto_topup_enabled: bool
    auto_topup_pack_id: Optional[int] = None

# 回應模型
class WalletResponse(BaseModel):
    balance: int
    auto_topup_enabled: bool
    expiring_soon: int

class PackResponse(BaseModel):
    pack_id: int
    name: str
    points: int
    price_ntd: int
    valid_days: int

class PlanResponse(BaseModel):
    plan_id: int
    name: str
    monthly_points: int
    batch_limit: int
    roles_limit: int

class AuthorizeResponse(BaseModel):
    authorized: bool
    cost: int
    reason: str  # 'INSUFFICIENT_POINTS'|'UPGRADE_REQUIRED'|'OK'
    needTopup: bool
    suggestPackIds: List[int]

class CheckoutResponse(BaseModel):
    order_id: int
    checkout_url: str
    amount: int
    points: int

# ========== 點數系統路由 ==========

@points_router.get("/wallet", response_model=WalletResponse)
async def get_wallet(req: Request):
    """獲取錢包資訊"""
    # 從請求中獲取用戶ID（需要根據您的認證系統調整）
    user_id = get_user_id_from_request(req)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登入")
    
    wallet_info = points_system.get_wallet_info(user_id)
    return WalletResponse(**wallet_info)

@points_router.get("/packs", response_model=List[PackResponse])
async def get_point_packs():
    """獲取點數包列表"""
    packs = points_system.get_point_packs()
    return [PackResponse(**pack.__dict__) for pack in packs]

@points_router.post("/authorize", response_model=AuthorizeResponse)
async def authorize_usage(req: Request, request: AuthorizeRequest):
    """授權使用（不扣點，只判斷）"""
    user_id = get_user_id_from_request(req)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登入")
    
    result = points_system.authorize_usage(
        user_id=user_id,
        module=request.module,
        mode=request.mode,
        count=request.count
    )
    
    return AuthorizeResponse(**result)

@points_router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(req: Request, request: CheckoutRequest):
    """創建點數包訂單"""
    user_id = get_user_id_from_request(req)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登入")
    
    result = points_system.create_checkout(user_id, request.pack_id)
    
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    
    return CheckoutResponse(**result)

@points_router.post("/consume")
async def consume_points(req: Request, request: ConsumeRequest):
    """實際扣點（在既有流程完成後調用）"""
    user_id = get_user_id_from_request(req)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登入")
    
    success = points_system.consume_points(
        user_id=user_id,
        module=request.module,
        mode=request.mode,
        count=request.count
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="餘額不足")
    
    return {"success": True}

@points_router.patch("/settings")
async def update_settings(req: Request, request: SettingsRequest):
    """更新自動補點設定"""
    user_id = get_user_id_from_request(req)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登入")
    
    points_system.toggle_auto_topup(
        user_id=user_id,
        enabled=request.auto_topup_enabled,
        pack_id=request.auto_topup_pack_id
    )
    
    return {"success": True}

@points_router.post("/webhooks/payment")
async def payment_webhook(req: Request):
    """金流回調（需要根據實際金流提供商調整）"""
    try:
        # 這裡需要根據實際金流提供商實現驗簽和冪等性檢查
        body = await req.json()
        
        # 假設金流回調包含 order_id
        order_id = body.get("order_id")
        if not order_id:
            raise HTTPException(status_code=400, detail="缺少訂單ID")
        
        # 處理付款
        success = points_system.process_payment(order_id, "webhook")
        
        if not success:
            raise HTTPException(status_code=400, detail="處理付款失敗")
        
        return {"success": True}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== 方案系統路由 ==========

@plans_router.get("/list", response_model=List[PlanResponse])
async def get_plans():
    """獲取訂閱方案列表（只讀）"""
    plans = points_system.get_plans()
    return [PlanResponse(**plan.__dict__) for plan in plans]

# ========== 管理員路由 ==========

@points_router.post("/admin/add-points")
async def admin_add_points(req: Request, user_id: str, points: int, reason: str = "gift"):
    """管理員添加點數"""
    # 這裡需要檢查管理員權限
    if not check_admin_permission(req):
        raise HTTPException(status_code=403, detail="權限不足")
    
    points_system.add_points(
        user_id=user_id,
        points=points,
        reason=PointReason(reason),
        ref_id="admin_gift"
    )
    
    return {"success": True}

@points_router.post("/admin/expire-sweep")
async def admin_expire_sweep(req: Request):
    """管理員觸發到期清理"""
    if not check_admin_permission(req):
        raise HTTPException(status_code=403, detail="權限不足")
    
    points_system.expire_sweep()
    return {"success": True}

# ========== 輔助函數 ==========

def get_user_id_from_request(req: Request) -> Optional[str]:
    """從請求中獲取用戶ID（需要根據您的認證系統調整）"""
    # 方法1: 從session cookie
    session_cookie = req.cookies.get("session")
    if session_cookie:
        try:
            from .app import session_signer  # 假設您有這個
            data = session_signer.loads(session_cookie)
            return data.get("user_id")
        except:
            pass
    
    # 方法2: 從Authorization header
    auth_header = req.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        # 這裡需要根據您的JWT實現解析token
        # return parse_jwt_token(token)
        pass
    
    # 方法3: 從查詢參數（臨時方案）
    return req.query_params.get("user_id")

def check_admin_permission(req: Request) -> bool:
    """檢查管理員權限（需要根據您的管理員系統調整）"""
    # 這裡需要根據您的管理員認證系統實現
    admin_token = req.headers.get("x-admin-token")
    return admin_token == "your_admin_token"  # 替換為實際的token檢查

# ========== 定時任務 ==========

async def daily_tasks():
    """每日定時任務"""
    # 到期清理
    points_system.expire_sweep()
    
    # 月贈點（如果需要）
    # await grant_monthly_points()
    
    # 發送到期提醒（如果需要）
    # await send_expiration_notifications()

# ========== 整合到主應用 ==========

def register_points_routes(app):
    """註冊點數系統路由到主應用"""
    app.include_router(points_router)
    app.include_router(plans_router)
    
    # 註冊定時任務（需要根據您的定時任務系統調整）
    # app.add_event_handler("startup", start_scheduler)
    # app.add_event_handler("shutdown", stop_scheduler)
