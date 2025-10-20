# AI 短影音顧問智能系統 - 後端

## 🚀 系統概述

這是一個基於 FastAPI 的 AI 短影音顧問智能系統後端，提供三種核心 AI 功能：
- 🎯 AI 短影音定位顧問
- 💡 AI 選題小助手  
- 📝 AI 腳本生成大師

## 📊 當前系統狀態 (2024-10-20)

### ✅ 已完成功能
- **用戶認證系統**：Email 註冊/登入 + Google OAuth
- **推薦碼系統**：邀請朋友註冊，雙方各得 500 點數
- **點數管理**：新用戶 500 點，推薦獎勵 500 點，使用扣點
- **SSE 串流**：即時逐字輸出，提供 GPT 般體驗
- **RAG 整合**：結合知識庫進行智能回答
- **後台管理**：用戶管理、點數充值、推薦碼管理
- **資料庫修復**：自動修復缺失欄位，確保數據完整性

### 🔧 技術架構狀態
- **FastAPI 服務**：運行在端口 8080
- **SQLite 資料庫**：包含 users, users_auth, credit_transactions 等表
- **CORS 配置**：支援跨域請求
- **錯誤處理**：完整的異常處理和日誌記錄

## ✨ 主要功能

### 用戶管理
- **註冊/登入系統**：支持 Email 註冊和 Google OAuth 登入
- **推薦碼系統**：邀請朋友註冊，雙方各得 500 點數
- **點數管理**：新用戶註冊獲得 500 點數，使用推薦碼額外獲得 500 點數
- **用戶檔案**：個人資料管理和使用統計
- **會話管理**：安全的 Cookie 會話和登出功能

### AI 功能
- **定位顧問**：基於知識庫的短影音定位分析
- **選題助手**：智能選題建議和內容規劃
- **腳本生成**：支持多種模板（A-E）和平台（Reels/TikTok/小紅書/YouTube Shorts）
- **SSE 串流**：即時逐字輸出，提供 GPT 般的體驗
- **RAG 整合**：結合知識庫進行智能回答

### 管理系統
- **後台管理**：用戶管理、點數管理、推薦碼管理
- **數據分析**：使用統計和系統監控
- **點數交易記錄**：完整的點數變動歷史
- **管理員認證**：安全的後台登入系統
- **自動修復**：資料庫結構自動檢測和修復

## 🛠 技術架構

### 後端技術棧
- **FastAPI**：現代化的 Python Web 框架
- **SQLite**：輕量級資料庫
- **Uvicorn**：ASGI 服務器
- **Authlib**：OAuth 認證
- **Pydantic**：數據驗證

### 核心模組
```
backend/
├── app.py                 # 主應用程式
├── chat_stream.py         # SSE 串流聊天
├── memory.py             # 用戶記憶管理
├── rag.py                # 檢索增強生成
├── knowledge_loader.py   # 知識庫載入
├── providers.py          # LLM 提供商介面
├── admin/
│   └── admin.html        # 後台管理介面
└── data/
    ├── kb_positioning.txt
    ├── kb_topic_selection.txt
    └── kb_script_generation.txt
```

## 🚀 快速開始

### 環境要求
- Python 3.11+
- pip 或 conda

### 安裝步驟

1. **克隆專案**
```bash
git clone <repository-url>
cd ai_web_app/對話式/原始/backend
```

2. **創建虛擬環境**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate     # Windows
```

3. **安裝依賴**
```bash
pip install -r requirements.txt
```

4. **設置環境變數**
```bash
# 複製環境變數範例
cp .env.example .env

# 編輯 .env 文件
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
OAUTH_REDIRECT_URI=https://your-domain.com/auth/google/callback
ADMIN_TOKEN=your_admin_token
```

5. **初始化資料庫**
```bash
python -c "from app import init_db; init_db()"
```

6. **啟動服務**
```bash
python app.py
```

服務將在 `http://localhost:8080` 啟動

## 📚 API 文檔

### 認證相關
- `POST /auth/signup` - 用戶註冊
- `POST /auth/login` - 用戶登入
- `POST /auth/logout` - 用戶登出
- `GET /me` - 獲取當前用戶資訊
- `GET /auth/google/start` - Google OAuth 開始
- `GET /auth/google/callback` - Google OAuth 回調
- `GET /auth/google/success` - Google 登入成功頁面

### AI 功能
- `POST /api/chat` - 非串流聊天
- `GET /api/stream` - SSE 串流聊天
- `POST /api/positioning` - 定位分析
- `POST /api/topics` - 選題建議
- `POST /api/script` - 腳本生成

### 用戶管理
- `GET /api/user/profile` - 用戶檔案
- `GET /api/user/knowledge` - 知識庫內容
- `POST /api/user/profile` - 更新用戶檔案

### 管理後台
- `GET /admin/users` - 用戶列表
- `POST /admin/charge` - 充值點數
- `GET /admin/referrals` - 推薦碼管理

## 🔧 配置說明

### 環境變數
```bash
# Google OAuth
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
OAUTH_REDIRECT_URI=https://your-domain.com/auth/google/callback

# 管理員
ADMIN_TOKEN=your_secret_admin_token

# 資料庫
DB_PATH=three_agents_system.db

# CORS
ALLOWED_ORIGINS=https://your-frontend-domain.com,http://localhost:3000
```

### 資料庫結構
- **users**：用戶基本資訊
- **users_auth**：認證資訊
- **user_profiles**：用戶檔案
- **messages**：聊天記錄
- **summaries**：對話摘要
- **credit_transactions**：點數交易記錄

## 🚀 部署

### Docker 部署
```bash
# 構建映像
docker build -t ai-video-backend .

# 運行容器
docker run -p 8080:8080 \
  -e GOOGLE_CLIENT_ID=your_id \
  -e GOOGLE_CLIENT_SECRET=your_secret \
  -e ADMIN_TOKEN=your_token \
  ai-video-backend
```

### 雲端部署
支援部署到：
- **Zeabur**：推薦使用
- **Railway**
- **Heroku**
- **AWS/GCP/Azure**

## 🔍 監控與日誌

### 健康檢查
```bash
curl http://localhost:8080/healthz
```

### 日誌查看
```bash
# 查看應用程式日誌
tail -f logs/app.log

# 查看錯誤日誌
tail -f logs/error.log
```

## 🛠 開發指南

### 添加新功能
1. 在 `app.py` 中添加路由
2. 實現對應的業務邏輯
3. 更新 API 文檔
4. 添加測試用例

### 資料庫遷移
```bash
# 創建遷移腳本
python scripts/migrate.py

# 執行遷移
python scripts/migrate.py --up
```

## 📞 支援

如有問題，請聯繫：
- 技術支援：support@example.com
- 文檔：https://docs.example.com
- GitHub Issues：https://github.com/your-repo/issues

## 📄 授權

MIT License - 詳見 [LICENSE](LICENSE) 文件