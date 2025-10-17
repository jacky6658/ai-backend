# 💰 AIJob 帳號充值功能使用指南

## 🎯 功能概述

已為您完善了完整的帳號充值功能，包括：
- ✅ 管理後台充值功能
- ✅ 前端快速充值功能
- ✅ 後端API充值功能
- ✅ 充值腳本工具

## 🚀 快速開始

### 方法一：使用充值腳本（推薦）

1. **設置環境變數**
   ```bash
   # 在Zeabur後台設置環境變數
   ADMIN_TOKEN=your_secret_token
   ```

2. **運行充值腳本**
   ```bash
   python charge_my_account.py
   ```

3. **選擇操作**
   - 選擇 `1` 為您的帳號充值 5000 點數
   - 選擇 `2` 查看當前點數餘額

### 方法二：使用管理後台

1. **登入管理後台**
   - 訪問：`https://aijobvideobackend.zeabur.app/admin`
   - 使用管理員帳號密碼登入

2. **使用充值功能**
   - 在「💰 用戶充值管理」區塊
   - 輸入您的email：`aiagentg888@gmail.com`
   - 輸入要充值的點數
   - 點擊「💳 立即充值」

### 方法三：使用前端快速充值

1. **登入前端**
   - 使用Google登入您的帳號

2. **開啟快速充值**
   - 點擊右上角用戶頭像
   - 選擇「帳戶」
   - 點擊「⚡ 快速充值」按鈕

3. **選擇充值金額**
   - 選擇預設點數包：100、500、1000、5000
   - 或輸入自定義金額

## 🔧 技術細節

### 後端API

#### 充值API
```http
POST /admin/user/add_credits
Content-Type: application/json
x-admin-token: your_admin_token

{
  "identifier": "user@example.com",  // 用戶email或username
  "credits": 1000,                   // 充值點數
  "reason": "管理員充值"              // 充值原因
}
```

#### 查詢餘額API
```http
GET /admin/user/{user_id}/credits
x-admin-token: your_admin_token
```

### 資料庫結構

#### user_credits 表
```sql
CREATE TABLE user_credits (
    user_id TEXT PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### orders 表
```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    order_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## 📋 功能特色

### 管理後台功能
- ✅ 用戶列表查看
- ✅ 點數餘額查詢
- ✅ 批量充值操作
- ✅ 充值記錄追蹤
- ✅ 稽核日誌記錄

### 前端功能
- ✅ 快速充值視窗
- ✅ 預設點數包選擇
- ✅ 自定義金額輸入
- ✅ 即時餘額更新
- ✅ 充值結果提示

### 安全特性
- ✅ 管理員權限驗證
- ✅ 操作稽核記錄
- ✅ 輸入驗證
- ✅ 錯誤處理

## 🛠️ 故障排除

### 常見問題

1. **充值失敗：用戶不存在**
   - 解決：請先在前端使用Google登入一次
   - 確保email地址正確

2. **充值失敗：權限不足**
   - 解決：檢查ADMIN_TOKEN是否正確設置
   - 確認管理員權限

3. **充值失敗：網路錯誤**
   - 解決：檢查網路連接
   - 確認後端服務正常運行

### 檢查步驟

1. **檢查後端狀態**
   ```bash
   curl https://aijobvideobackend.zeabur.app/admin/healthz
   ```

2. **檢查管理員權限**
   ```bash
   curl -H "x-admin-token: your_token" https://aijobvideobackend.zeabur.app/admin/users_auth
   ```

3. **檢查用戶資料**
   - 登入前端確認用戶資料已創建
   - 檢查資料庫中是否有對應記錄

## 📊 使用統計

### 充值記錄查看
- 管理後台：查看「Orders」區塊
- 資料庫：查詢 `orders` 表
- 稽核日誌：查看 `admin_audit_logs` 表

### 點數使用追蹤
- 用戶每次使用AI功能會扣除對應點數
- 可在管理後台查看使用統計
- 支援CSV匯出功能

## 🔄 自動化建議

### 定期充值
可以設置定時任務自動為您的帳號充值：

```python
# 每日自動充值腳本
import schedule
import time

def daily_charge():
    # 執行充值邏輯
    pass

schedule.every().day.at("09:00").do(daily_charge)

while True:
    schedule.run_pending()
    time.sleep(60)
```

### 監控腳本
```python
# 點數餘額監控
def check_balance():
    if balance < 100:
        # 發送提醒或自動充值
        pass
```

## 📞 支援

如有問題，請檢查：
1. 環境變數設置
2. 網路連接狀態
3. 後端服務狀態
4. 用戶登入狀態

---

**注意**：所有充值操作都會記錄在稽核日誌中，請妥善保管管理員權限。
