# AIJob 短影音智能體 - 技術總覽（前端 / 後端）

本文件整理目前專案的技術現況與重點設定，方便快速掌握整體進度、維運與除錯。

---

## 後端（Backend）

- 技術棧
  - Python 3.11、FastAPI、Uvicorn
  - SQLite（預設：`/data/three_agents_system.db`）
  - Google Gemini（`gemini-2.5-flash`）
  - Authlib（Google OAuth）、Starlette SessionMiddleware
  - 部署：Zeabur
- 主程式與啟動
  - 主檔：`backend/app.py`
  - Start：`uvicorn backend.app:app --host 0.0.0.0 --port 8080`
- 功能模組
  - 三智能體（具長期記憶）
    - 定位 positioning：產出定位、語氣建議；自動寫回 `user_profiles`
    - 選題 topics：每日選題；寫入 `topic_suggestions`
    - 腳本文案 script/copy：分段腳本或社群文案（hashtags/CTA）
  - 聊天端點：`POST /chat`、`POST /chat_stream`（舊版 `chat_generate`/`generate_script` 保留）
  - 匯出：`/export/xlsx`、`/export/google-sheet-flat(-v2)`
- 管理後台（/admin）
  - 內嵌於 `app.py`，無需獨立前端
  - 儀表板：總用戶、總請求、近 7 日趨勢、mode/agent 分佈
  - 帳號管理：查詢 `users`/`users_auth`、重設密碼（二次確認，寫入 `admin_audit_logs`）
  - 訊息/請求檢視：依 user_id/session_id/日期/模式/agent 篩選
  - 匯出：Users/Usage CSV、`/admin/requests_full.csv`，提供 Google Sheet `IMPORTDATA` 範例
  - 安全：
    - Admin 登入 `/admin/login`、登出 `/admin/logout`、健康檢查 `/admin/healthz`
    - `admin_session` Cookie 5 小時自動失效；頁面每 5 分鐘自檢，失效導回登入
    - 所有 fetch 皆 `credentials:'include'`，避免跨站 Cookie 被擋
- 主要資料表（節錄）
  - `users`、`users_auth`（SHA-256 密碼雜湊）
  - `user_profiles`（定位檔）、`sessions`、`messages`、`agent_memories`
  - `topic_suggestions`（每日選題）、`requests`（請求日誌）
  - `user_credits`、`orders`（點數/訂單，簡化）
  - `admin_audit_logs`（敏感操作稽核）
- 環境變數（重點）
  - Admin：`ADMIN_USER`、`ADMIN_PASSWORD`、（選）`ADMIN_TOKEN`
  - 基礎：`SESSION_SECRET`、`DB_PATH`、`KNOWLEDGE_TXT_PATH`、`ALLOWED_ORIGINS`
  - AI：`GEMINI_API_KEY`（或 `GOOGLE_API_KEY`）、`GEMINI_MODEL`
  - OAuth：`GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`OAUTH_REDIRECT_URI`
- OAuth 注意
  - 已加 `prompt=consent select_account`；回呼缺 `id_token` 時以 `userinfo`/OIDC 端點補齊
  - Cookie 設 `SameSite=None; Secure`，務必走 HTTPS；跨域請求需 `credentials:'include'`

---

## 前端（Frontend）

- 檔案與技術
  - 原生 HTML/CSS/JS；主檔：`front/index.html`
  - 舊版 `front/admin_dashboard.html` 已由後端 `/admin` 取代（檔案保留）
- 行為重點
  - `API_BASE` 指向後端（目前：`https://aijobvideobackend.zeabur.app`）
  - Email 登入/註冊：`/auth/login`、`/auth/signup`；成功後 `/me` 驗證狀態
  - Google 登入：彈窗啟動 `/auth/google/start?next=/`，輪詢 `/me` 完成同步；Toast 置中
  - 帳號抽屜 / 點數概覽（可對接帳務摘要 API）
- 後台使用方式
  - 直接開 `https://aijobvideobackend.zeabur.app/admin`（同網域）
  - 內嵌頁已為所有管理 API 加上 `credentials:'include'`

---

## 快速驗證

- 健康檢查：`GET /healthz` → `{ "ok": true }`
- 後台健康：`GET /admin/healthz` → 應見 `admin_ready`、`oauth_ready`、`has_admin_session`
- 後台登入後可載入儀表板；重設密碼會寫入稽核
- Google 登入會彈出帳號選擇並返回已登入的 `/me`

---

## 部署備忘（Zeabur）

- Build：`pip install -r backend/requirements.txt`
- Start：`uvicorn backend.app:app --host 0.0.0.0 --port 8080`
- 若專案根目錄非含 `backend/`，請設定 Working Directory；或於 Start 加 `--app-dir`
- `ALLOWED_ORIGINS` 需包含實際前端網域（例：`https://video.aijob.com.tw`）

---

## 目前狀態摘要

- 後端：服務與管理後台可用；Google OAuth、Admin Session、CSV/Sheet 匯出皆上線
- 前端：主頁可登入/呼叫 AI；後台以 `/admin` 為主（舊檔保留）
