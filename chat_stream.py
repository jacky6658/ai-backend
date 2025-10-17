# -*- coding: utf-8 -*-
"""
串流聊天模組 - SSE 與 WebSocket 支援
提供 /api/stream、/api/ws、/api/chat 端點
整合知識庫檢索與自動總結功能
"""

import os
import json
import sqlite3
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import google.generativeai as genai
from knowledge_text_loader import load_knowledge_text, retrieve_context

# 環境變數
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
KNOWLEDGE_TXT_PATH = os.getenv("KNOWLEDGE_TXT_PATH", "/data/data/kb.txt")
DB_PATH = os.getenv("DB_PATH", "/data/three_agents_system.db")

# 系統提示模板
SYSTEM_PROMPT_TEMPLATE = """你的角色是 AI 短影音顧問。規則：
1) 先整合知識庫檢索片段，再結合歷史脈絡作答；以「先結論、後步驟」呈現。
2) 不足或不確定時要明講，並列出需要的補件或資料。
3) 回覆末尾加上：
   - 來源：列出使用到的文件/段落（若無命中則寫「無明確命中」）。
   - 建議：3 點以內、可執行的下一步。

[Top-K Retrieved Snippets]
{retrieved_context}

[歷史對話摘要]
{context_summary}

[最近對話]
{recent_messages}

用戶問題：{user_question}"""

# 初始化 Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

router = APIRouter()

# ========= 資料庫操作 =========

def get_conn() -> sqlite3.Connection:
    """取得資料庫連線"""
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def get_session_context(session_id: str) -> Dict[str, Any]:
    """取得會話上下文"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        # 取得會話資訊
        session_row = conn.execute(
            "SELECT user_id, agent_type, context_summary FROM sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        
        if not session_row:
            return {"user_id": None, "agent_type": None, "context_summary": None}
        
        # 取得最近 N 筆訊息（控制 token 窗口 ~6k）
        messages = conn.execute(
            """SELECT role, content, timestamp FROM messages 
               WHERE session_id = ? 
               ORDER BY timestamp DESC 
               LIMIT 20""",
            (session_id,)
        ).fetchall()
        
        return {
            "user_id": session_row["user_id"],
            "agent_type": session_row["agent_type"],
            "context_summary": session_row["context_summary"],
            "messages": [dict(msg) for msg in reversed(messages)]
        }
    finally:
        conn.close()

def save_message(session_id: str, role: str, content: str, metadata: Dict = None):
    """儲存訊息到資料庫"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, metadata) VALUES (?, ?, ?, ?)",
            (session_id, role, content, json.dumps(metadata) if metadata else None)
        )
        conn.commit()
    finally:
        conn.close()

def update_session_summary(session_id: str, summary: str):
    """更新會話摘要"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE sessions SET context_summary = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            (summary, session_id)
        )
        conn.commit()
    finally:
        conn.close()

# ========= 上下文建構 =========

def build_context(session_id: str, user_question: str) -> str:
    """建構完整的對話上下文"""
    # 載入知識庫
    load_knowledge_text()
    
    # 取得會話上下文
    context = get_session_context(session_id)
    
    # 檢索相關知識片段
    retrieved_context = retrieve_context(user_question, k=5, max_chars=1200)
    
    # 建構最近對話
    recent_messages = []
    for msg in context["messages"]:
        role = "用戶" if msg["role"] == "user" else "助手"
        recent_messages.append(f"{role}: {msg['content']}")
    
    recent_messages_text = "\n".join(recent_messages[-10:])  # 最近10輪對話
    
    # 建構完整提示
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        retrieved_context=retrieved_context or "無相關知識片段",
        context_summary=context["context_summary"] or "無歷史摘要",
        recent_messages=recent_messages_text or "無最近對話",
        user_question=user_question
    )
    
    return prompt

# ========= 串流生成 =========

async def generate_stream_response(prompt: str):
    """生成串流回應"""
    if not GEMINI_API_KEY:
        yield "data: 錯誤：未設定 GEMINI_API_KEY\n\n"
        return
    
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        # 使用 generate_content 的 stream 功能
        response = model.generate_content(prompt, stream=True)
        
        for chunk in response:
            if chunk.text:
                yield f"data: {chunk.text}\n\n"
        
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        yield f"data: 錯誤：{str(e)}\n\n"

async def generate_websocket_response(websocket: WebSocket, prompt: str):
    """生成 WebSocket 回應"""
    if not GEMINI_API_KEY:
        await websocket.send_text("錯誤：未設定 GEMINI_API_KEY")
        return
    
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt, stream=True)
        
        for chunk in response:
            if chunk.text:
                await websocket.send_text(chunk.text)
        
        await websocket.send_text("[DONE]")
        
    except Exception as e:
        await websocket.send_text(f"錯誤：{str(e)}")

# ========= 背景任務 =========

def summarize_conversation(session_id: str, user_question: str, assistant_response: str):
    """背景任務：總結對話"""
    try:
        # 取得會話上下文
        context = get_session_context(session_id)
        messages = context["messages"]
        
        # 建構總結提示
        summary_prompt = f"""請將以下對話總結成不超過400字的摘要，保留關鍵資訊：

歷史摘要：{context['context_summary'] or '無'}

最近對話：
{chr(10).join([f"{msg['role']}: {msg['content']}" for msg in messages[-6:]])}

最新一輪：
用戶: {user_question}
助手: {assistant_response}

請提供簡潔的對話摘要："""

        if GEMINI_API_KEY:
            model = genai.GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(summary_prompt)
            summary = response.text.strip()[:400]  # 限制長度
            
            # 更新會話摘要
            update_session_summary(session_id, summary)
            
    except Exception as e:
        print(f"總結對話時發生錯誤: {e}")

def create_turn_summary(user_question: str, assistant_response: str) -> str:
    """創建本輪對話摘要（≤120字）"""
    try:
        summary_prompt = f"""請為以下對話創建簡短摘要（不超過120字）：

用戶: {user_question}
助手: {assistant_response}

摘要："""

        if GEMINI_API_KEY:
            model = genai.GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(summary_prompt)
            return response.text.strip()[:120]
        
        return f"用戶詢問：{user_question[:50]}..."
        
    except Exception as e:
        return f"用戶詢問：{user_question[:50]}..."

# ========= API 端點 =========

@router.get("/api/stream")
async def stream_chat(
    session_id: str = Query(..., description="會話ID"),
    q: str = Query(..., description="用戶問題")
):
    """SSE 串流聊天端點"""
    
    # 儲存用戶訊息
    save_message(session_id, "user", q)
    
    # 建構上下文
    prompt = build_context(session_id, q)
    
    # 生成回應
    async def generate():
        full_response = ""
        async for chunk in generate_stream_response(prompt):
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                content = chunk[6:].strip()
                if content and not content.startswith("錯誤："):
                    full_response += content
            yield chunk
        
        # 儲存助手回應
        if full_response:
            save_message(session_id, "assistant", full_response)
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )

@router.websocket("/api/ws")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 聊天端點"""
    await websocket.accept()
    
    try:
        while True:
            # 接收訊息
            data = await websocket.receive_text()
            message_data = json.loads(data)
            
            session_id = message_data.get("session_id")
            user_question = message_data.get("q")
            
            if not session_id or not user_question:
                await websocket.send_text("錯誤：缺少必要參數")
                continue
            
            # 儲存用戶訊息
            save_message(session_id, "user", user_question)
            
            # 建構上下文
            prompt = build_context(session_id, user_question)
            
            # 生成回應
            full_response = ""
            async for chunk in generate_stream_response(prompt):
                if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                    content = chunk[6:].strip()
                    if content and not content.startswith("錯誤："):
                        full_response += content
                        await websocket.send_text(content)
            
            # 儲存助手回應
            if full_response:
                save_message(session_id, "assistant", full_response)
            
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_text(f"錯誤：{str(e)}")

class ChatRequest(BaseModel):
    session_id: str
    q: str

class ChatResponse(BaseModel):
    answer: str
    sources: str
    turn_summary: str

@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks
):
    """非串流聊天端點"""
    
    # 儲存用戶訊息
    save_message(request.session_id, "user", request.q)
    
    # 建構上下文
    prompt = build_context(request.session_id, request.q)
    
    # 生成回應
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="未設定 GEMINI_API_KEY")
    
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        answer = response.text.strip()
        
        # 儲存助手回應
        save_message(request.session_id, "assistant", answer)
        
        # 背景任務：總結對話
        background_tasks.add_task(
            summarize_conversation, 
            request.session_id, 
            request.q, 
            answer
        )
        
        # 創建本輪摘要
        turn_summary = create_turn_summary(request.q, answer)
        
        # 提取來源資訊（簡單實作）
        sources = "基於知識庫檢索和歷史對話"
        if "無明確命中" in answer:
            sources = "無明確命中"
        
        return ChatResponse(
            answer=answer,
            sources=sources,
            turn_summary=turn_summary
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成回應時發生錯誤：{str(e)}")
