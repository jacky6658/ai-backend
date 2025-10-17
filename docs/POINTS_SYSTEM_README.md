# AI Points System - 點數訂閱制系統

## 📋 系統概述

這是一個**完全獨立**的點數訂閱制模組，不修改任何既有代碼，只新增功能。

## 🗂️ 新增文件結構

```
backend/
├── points_system.py          # 點數系統核心邏輯
├── points_routes.py          # API路由定義
└── points_integration.py     # 整合到主應用

public/
├── points-addon.css          # 點數系統樣式
└── src/
    ├── lib/
    │   └── aiPoints_bus.js   # 事件攔截器
    ├── widgets/
    │   ├── aiPoints_badge.js # 點數徽章
    │   └── aiPoints_modal.js # 購買彈窗
    └── boot/
        └── aiPoints_bootstrap.js # 啟動腳本
```

## 🚀 部署步驟

### 1. 上傳文件到GitHub
```bash
# 只上傳新增的文件，不修改既有文件
git add backend/points_*.py
git add public/points-addon.css
git add public/src/
git commit -m "Add AI Points System"
git push
```

### 2. 重新部署Zeabur
- 在Zeabur後台重新部署
- 確保所有新文件都被包含

### 3. 設置環境變數
在Zeabur環境變數中添加：
```
ADMIN_TOKEN=your_secret_admin_token
```

### 4. 初始化數據庫
系統會自動創建點數系統相關的數據表。

## 🔧 API端點

### 點數系統 (`/points/*`)
- `GET /points/wallet` - 獲取錢包資訊
- `GET /points/packs` - 獲取點數包列表
- `POST /points/authorize` - 授權使用（不扣點）
- `POST /points/checkout` - 創建點數包訂單
- `POST /points/consume` - 實際扣點
- `PATCH /points/settings` - 更新設定
- `POST /points/webhooks/payment` - 金流回調

### 方案系統 (`/plans/*`)
- `GET /plans/list` - 獲取訂閱方案（只讀）

## 💡 使用方式

### 前端自動攔截
系統會自動攔截以下按鈕：
- 包含「一鍵生成」、「生成腳本」等關鍵字的按鈕
- 具有 `data-action` 屬性的元素

### 手動調用
```javascript
// 顯示點數購買彈窗
window.aiPoints.showModal({
    suggestPackIds: [1, 2, 3],
    onSuccess: () => console.log('購買成功')
});

// 顯示升級訂閱彈窗
window.aiPoints.showUpgrade({
    onSuccess: () => console.log('升級成功')
});

// 更新點數徽章
window.aiPoints.updateBadge();
```

## 🎯 功能特色

### 1. 事件攔截
- 自動檢測一鍵生成按鈕
- 不修改既有代碼
- 零侵入式整合

### 2. 授權機制
- 免費額度：每個模組每月10次
- 點數扣費：一鍵生成2點/次，聊天1點/次
- 自動補點：餘額低於20點時自動購買

### 3. 點數包系統
- 小額包：300點 / NT$399
- 標準包：1000點 / NT$1099（推薦）
- 大額包：3000點 / NT$3399

### 4. 訂閱方案
- 基礎方案：100點/月，批次上限10次
- 專業方案：500點/月，批次上限50次
- 企業方案：2000點/月，批次上限200次

## 🔒 安全特性

- 所有API都有認證檢查
- 點數扣費有冪等性保護
- 金流回調有驗簽機制
- 管理員操作有稽核記錄

## 📱 響應式設計

- 支援手機、平板、桌面
- 暗色模式適配
- 觸控友好的界面

## 🛠️ 開發模式

在本地開發時，系統會：
- 顯示詳細的調試信息
- 記錄性能指標
- 提供錯誤追蹤

## ❓ 常見問題

### Q: 會影響既有功能嗎？
A: 完全不會。所有新增功能都是獨立的，不修改任何既有代碼。

### Q: 如何自定義攔截規則？
A: 修改 `aiPoints_bus.js` 中的 `actionKeywords` 數組。

### Q: 如何添加新的點數包？
A: 在數據庫的 `point_packs` 表中添加記錄，或使用管理員API。

### Q: 如何整合金流？
A: 修改 `points_routes.py` 中的 `payment_webhook` 函數。

## 📞 技術支援

如有問題，請檢查：
1. 瀏覽器控制台是否有錯誤
2. 後端日誌是否有異常
3. 環境變數是否正確設置

---

**注意**：此系統設計為完全獨立模組，可以隨時啟用或停用，不會影響既有功能。
