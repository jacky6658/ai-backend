# 串流聊天 API 使用說明

## 環境變數設定

請確保以下環境變數已正確設定：

```bash
# 必要環境變數
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash  # 可選，預設為 gemini-2.5-flash
KNOWLEDGE_TXT_PATH=/data/data/kb.txt  # 可選，預設為 /data/data/kb.txt

# 資料庫設定
DB_PATH=/data/three_agents_system.db  # 可選，預設為 /data/three_agents_system.db
```

## API 端點

### 1. SSE 串流端點

**GET** `/api/stream?session_id={session_id}&q={question}`

**回應格式：** `text/event-stream`

**JavaScript 使用範例：**

```javascript
// 使用 EventSource 連接 SSE 串流
function connectSSEStream(sessionId, question) {
    const url = `/api/stream?session_id=${encodeURIComponent(sessionId)}&q=${encodeURIComponent(question)}`;
    const eventSource = new EventSource(url);
    
    let fullResponse = '';
    
    eventSource.onmessage = function(event) {
        if (event.data === '[DONE]') {
            eventSource.close();
            console.log('串流完成，完整回應：', fullResponse);
            return;
        }
        
        // 累積回應內容
        fullResponse += event.data;
        
        // 即時顯示到 UI
        displayMessage(event.data);
    };
    
    eventSource.onerror = function(event) {
        console.error('SSE 連接錯誤：', event);
        eventSource.close();
    };
    
    return eventSource;
}

// 使用範例
const sessionId = 'user123_session456';
const question = '如何製作短影音？';
connectSSEStream(sessionId, question);
```

### 2. WebSocket 端點

**WebSocket** `/api/ws`

**JavaScript 使用範例：**

```javascript
// 使用 WebSocket 連接
function connectWebSocket(sessionId, question) {
    const ws = new WebSocket(`ws://localhost:8080/api/ws`);
    
    ws.onopen = function() {
        console.log('WebSocket 連接已建立');
        
        // 發送訊息
        const message = {
            session_id: sessionId,
            q: question
        };
        ws.send(JSON.stringify(message));
    };
    
    ws.onmessage = function(event) {
        if (event.data === '[DONE]') {
            console.log('WebSocket 回應完成');
            ws.close();
            return;
        }
        
        // 即時顯示到 UI
        displayMessage(event.data);
    };
    
    ws.onerror = function(error) {
        console.error('WebSocket 錯誤：', error);
    };
    
    ws.onclose = function() {
        console.log('WebSocket 連接已關閉');
    };
    
    return ws;
}

// 使用範例
const sessionId = 'user123_session456';
const question = '如何製作短影音？';
connectWebSocket(sessionId, question);
```

### 3. 非串流端點

**POST** `/api/chat`

**請求格式：**
```json
{
    "session_id": "user123_session456",
    "q": "如何製作短影音？"
}
```

**回應格式：**
```json
{
    "answer": "製作短影音的完整回應...",
    "sources": "基於知識庫檢索和歷史對話",
    "turn_summary": "本輪對話摘要（≤120字）"
}
```

**JavaScript 使用範例：**

```javascript
// 使用 fetch 發送 POST 請求
async function sendChatMessage(sessionId, question) {
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: sessionId,
                q: question
            })
        });
        
        if (!response.ok) {
            throw new Error(`HTTP 錯誤：${response.status}`);
        }
        
        const data = await response.json();
        console.log('完整回應：', data.answer);
        console.log('來源：', data.sources);
        console.log('本輪摘要：', data.turn_summary);
        
        return data;
    } catch (error) {
        console.error('發送訊息時發生錯誤：', error);
        throw error;
    }
}

// 使用範例
const sessionId = 'user123_session456';
const question = '如何製作短影音？';
sendChatMessage(sessionId, question);
```

## 功能特色

### 1. 知識庫整合
- 自動檢索相關知識片段（Top-K=5，最大1200字元）
- 整合到系統提示中，提供更準確的回應

### 2. 歷史對話管理
- 自動載入會話的歷史摘要（`sessions.context_summary`）
- 載入最近20筆對話記錄
- 控制 token 窗口約6k字元

### 3. 自動總結
- 背景任務自動總結對話（≤400字）
- 更新 `sessions.context_summary` 欄位
- 生成本輪對話摘要（≤120字）

### 4. 系統提示優化
- 先結論、後步驟的回應格式
- 明確標示不確定之處
- 自動添加來源和建議

## 錯誤處理

所有端點都會處理以下錯誤情況：
- 未設定 `GEMINI_API_KEY`
- 資料庫連接錯誤
- Gemini API 調用失敗
- 會話不存在

錯誤訊息會透過相應的串流或回應格式返回。

## 注意事項

1. **CORS 設定**：已沿用現有的 CORS 設定，支援跨域請求
2. **資料庫**：使用現有的 `sessions` 和 `messages` 表，不修改 schema
3. **知識庫**：使用現有的 `knowledge_text_loader.py` 模組
4. **背景任務**：總結功能使用 FastAPI 的 BackgroundTasks，不會阻塞回應
5. **串流支援**：SSE 和 WebSocket 都支援即時回應，適合聊天介面
