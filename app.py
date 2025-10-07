# app.py
import os
import json
import sqlite3
import time
from typing import List, Optional, Any, Dict, Literal
from fastapi import FastAPI, HTTPException, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")  # 可不設；不設時用本地 fallback

# ========= App 與 CORS =========
app = FastAPI(title="AI Script Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # 若有固定前端網域，建議改成清單
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ========= DB 工具 =========
def _ensure_db_dir(path: str):
    db_dir = os.path.dirname(path) or "."
    os.makedirs(db_dir, exist_ok=True)

def get_conn() -> sqlite3.Connection:
    _ensure_db_dir(DB_PATH)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

def init_db():
    _ensure_db_dir(DB_PATH)
    conn = get_conn()
    cur = conn.cursor()
    # 原本的請求記錄表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_input TEXT,
            previous_segments_json TEXT,
            response_json TEXT
        )
        """
    )
    # 新增：聊天會話 & 訊息表
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_session(
            id TEXT PRIMARY KEY,
            created_at INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_message(
            session_id TEXT,
            role TEXT,     -- 'user'|'assistant'|'system'
            content TEXT,
            ts INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

# ========= 啟動時初始化 =========
@app.on_event("startup")
def on_startup():
    try:
        init_db()
        print(f"[BOOT] SQLite path OK: {DB_PATH}")
    except Exception as e:
        # 只印出，不讓服務掛掉
        print("[BOOT] DB init failed:", e)

# ========= Pydantic 模型（產生段落） =========
class Segment(BaseModel):
    type: str = Field(default="場景")
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# ========= 錯誤處理（不要手動 Content-Length） =========
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if exc.detail else "http_error"},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 可在此加 logger，但不要手動設定 Content-Length
    return JSONResponse(status_code=500, content={"error": "internal_server_error"})

# ========= 健康檢查 & 靜態 =========
@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><body>
      <h3>AI Backend OK</h3>
      <p>POST <code>/generate_script</code> with JSON body.</p>
      <pre>{
  "user_input": "hi",
  "previous_segments": []
}</pre>
    </body></html>
    """

# ========= 產生段落主流程 =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
    """沒有 GOOGLE_API_KEY 時，給一份可用的本地段落，讓前端不會卡住。"""
    step = len(req.previous_segments)
    pick_type = "片頭" if step == 0 else ("片尾" if step >= 2 else "場景")
    short = (req.user_input or "")[:30]
    return [
        Segment(
            type=pick_type,
            camera=(
                "特寫主角臉部，燈光從右側打入，聚焦眼神。"
                if step == 0 else
                "半身跟拍，移至桌面，快速推近產品。" if step == 1
                else "遠景收尾，主角背對夜景，鏡頭緩慢拉遠。"
            ),
            dialog=(
                f"你是否也曾這樣想過？{short} —— 用 30 秒改變你的看法。"
                if step == 0 else
                f"把難的變簡單。{short}，現在就開始。" if step == 1
                else "行動永遠比等待重要。現在，輪到你了。"
            ),
            visual=(
                "字幕彈入：#加班也能健身；LOGO 淡入。"
                if step == 0 else
                "快切 B-roll：鍵盤、定時器、杯中冰塊；節奏對齊拍點。" if step == 1
                else "LOGO 收合、CTA 卡片滑入（左下）。"
            ),
        )
    ]

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    """使用 Google Generative AI 產生；失敗就丟回 fallback。"""
    import google.generativeai as genai  # 延遲載入

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    system_prompt = (
        "你是短影音腳本助手。輸出 JSON 陣列，每個元素含 type(片頭|場景|片尾)、"
        "camera、dialog、visual 三欄，不要加註解或多餘文字。"
    )
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成一段 30 秒短影音腳本的下一個分段。"

    prompt = (
        f"{system_prompt}\n"
        f"使用者輸入: {user}\n"
        f"已接受段落(previous_segments): {json.dumps(prev, ensure_ascii=False)}\n"
        f"請僅回傳 JSON 陣列，如: "
        f'[{{"type":"片頭","camera":"...","dialog":"...","visual":"..."}}]'
    )

    res = model.generate_content(prompt)
    text = (res.text or "").strip()

    # 嘗試只提取第一個 JSON 陣列
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        text = text[first_bracket:last_bracket + 1]

    data = json.loads(text)  # 可能丟出例外讓上層接住
    segments: List[Segment] = []
    for item in data:
        segments.append(
            Segment(
                type=item.get("type", "場景"),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
            )
        )
    return segments

@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        # 先嘗試 Gemini，沒有 key 或失敗都 fallback
        if GOOGLE_API_KEY:
            try:
                segments = _gemini_generate(req)
            except Exception as _:
                segments = _fallback_generate(req)
        else:
            segments = _fallback_generate(req)

        # 記錄到 DB（最小審計；失敗不影響回應）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, previous_segments_json, response_json) VALUES (?, ?, ?)",
                (
                    req.user_input,
                    json.dumps([s.model_dump() for s in req.previous_segments], ensure_ascii=False),
                    json.dumps([s.model_dump() for s in segments], ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return GenerateResp(segments=segments, error=None)
    except HTTPException as exc:
        # 讓 FastAPI 的 handler 處理
        raise exc
    except Exception as e:
        # 統一以 JSON 回傳
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 下面開始：聊天式 API（新增） =========

# --- Chat 資料模型 ---
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)     # 本輪新訊息（至少 1 則 user）
    previous_segments: List[Dict[str, Any]] = Field(default_factory=list)

class ChatResponse(BaseModel):
    session_id: str
    assistant_message: str
    segments: List[Dict[str, Any]]
    error: Optional[str] = None

chat_router = APIRouter()

def _ensure_session(session_id: Optional[str]) -> str:
    sid = session_id or f"s_{int(time.time()*1000)}"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM chat_session WHERE id=?", (sid,))
    if not cur.fetchone():
        cur.execute("INSERT INTO chat_session(id, created_at) VALUES(?,?)", (sid, int(time.time())))
        conn.commit()
    conn.close()
    return sid

def _save_messages(session_id: str, msgs: List[ChatMessage]) -> None:
    if not msgs:
        return
    conn = get_conn()
    cur = conn.cursor()
    for m in msgs:
        cur.execute(
            "INSERT INTO chat_message(session_id, role, content, ts) VALUES(?,?,?,?)",
            (session_id, m.role, m.content, int(time.time()))
        )
    conn.commit()
    conn.close()

def _load_history(session_id: str) -> List[ChatMessage]:
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT role, content FROM chat_message WHERE session_id=? ORDER BY ts ASC",
        (session_id,)
    ).fetchall()
    conn.close()
    return [ChatMessage(role=r, content=c) for (r, c) in rows]

# 把聊天內容壓成一條 user_input，仍然沿用你現有的產生器
def _generate_segments_from_chat(history: List[ChatMessage], prev_segments: List[Dict[str, Any]]) -> List[Segment]:
    header = "你是短影音腳本與文案助理，輸出 JSON segments：type/camera/dialog/visual。"
    chat_txt = "\n".join([f"{'使用者' if m.role=='user' else '助理'}: {m.content}" for m in history])
    prev_txt = json.dumps(prev_segments, ensure_ascii=False)
    user_input = f"{header}\n\n對話紀錄：\n{chat_txt}\n\n已接受的前段：{prev_txt}\n請產生新的 segments（鍵名與格式保持不變）。"

    req = GenerateReq(
        user_input=user_input,
        previous_segments=[Segment(**s) for s in prev_segments] if prev_segments else []
    )

    if GOOGLE_API_KEY:
        try:
            return _gemini_generate(req)
        except Exception:
            return _fallback_generate(req)
    else:
        return _fallback_generate(req)

@chat_router.post("/chat_generate", response_model=ChatResponse)
def chat_generate(req: ChatRequest):
    # 1) session 與儲存本輪 user 訊息
    sid = _ensure_session(req.session_id)
    new_user_msgs = [m for m in req.messages if m.role == "user"]
    if new_user_msgs:
        _save_messages(sid, new_user_msgs)

    # 2) 讀完整歷史（可在這裡做摘要/裁切以控長度）
    history = _load_history(sid)

    # 3) 產生 segments（沿用原本引擎）
    segments_objs = _generate_segments_from_chat(history, req.previous_segments)
    segments = [s.model_dump() for s in segments_objs]

    # 4) 準備助手回覆並存檔
    assistant_message = "好的，我已根據你的最新指示產出新分段，若想換風格或節奏直接跟我說。"
    _save_messages(sid, [ChatMessage(role="assistant", content=assistant_message)])

    return ChatResponse(
        session_id=sid,
        assistant_message=assistant_message,
        segments=segments,
        error=None
    )

# 掛上聊天路由
app.include_router(chat_router)

