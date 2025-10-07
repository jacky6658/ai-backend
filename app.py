# app.py
import os
import json
import sqlite3
import uuid
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Request
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
    allow_origins=["*"],            # 若有固定前端網域可收斂此清單
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id TEXT,
            user_id TEXT,
            messages_json TEXT,
            segments_json TEXT,
            copy_json TEXT
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
        print("[BOOT] DB init failed:", e)

# ========= Pydantic 模型（共用） =========
class Segment(BaseModel):
    type: str = Field(default="場景")
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    # 供聊天模式右側時間軸顯示（如果模型有產）
    start_sec: Optional[int] = None
    end_sec: Optional[int] = None
    cta: Optional[str] = ""

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# ========= 聊天模式的模型 =========
class ChatMessage(BaseModel):
    role: str
    content: str

class CopyOut(BaseModel):
    main_copy: str = ""
    alternates: List[str] = Field(default_factory=list)
    hashtags: List[str] = Field(default_factory=list)
    cta: str = ""

class ChatReq(BaseModel):
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    previous_segments: List[Segment] = Field(default_factory=list)
    remember: Optional[bool] = False

class ChatResp(BaseModel):
    session_id: str
    assistant_message: str
    segments: Optional[List[Segment]] = None
    copy: Optional[CopyOut] = None
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
      <p>POST <code>/generate_script</code> 或 <code>/chat_generate</code> with JSON body.</p>
      <pre>{
  "user_input": "hi",
  "previous_segments": []
}</pre>
    </body></html>
    """

# ========= 產生段落（舊流程） =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
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
            start_sec=0, end_sec=6, cta="點連結了解更多"
        )
    ]

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    import google.generativeai as genai  # 延遲載入
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    system_prompt = (
        "你是短影音腳本助手。輸出 JSON 陣列，每個元素含 type(片頭|場景|片尾)、"
        "camera、dialog、visual、(可選)start_sec、end_sec、cta，不要加註解或多餘文字。"
    )
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成一段 30 秒短影音腳本的下一個分段。"

    prompt = (
        f"{system_prompt}\n"
        f"使用者輸入: {user}\n"
        f"已接受段落(previous_segments): {json.dumps(prev, ensure_ascii=False)}\n"
        f"請僅回傳 JSON 陣列"
    )

    res = model.generate_content(prompt)
    text = (res.text or "").strip()
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        text = text[first_bracket:last_bracket + 1]
    data = json.loads(text)
    segments: List[Segment] = []
    for item in data:
        segments.append(
            Segment(
                type=item.get("type", "場景"),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
                start_sec=item.get("start_sec"),
                end_sec=item.get("end_sec"),
                cta=item.get("cta", ""),
            )
        )
    return segments

@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        if GOOGLE_API_KEY:
            try:
                segments = _gemini_generate(req)
            except Exception:
                segments = _fallback_generate(req)
        else:
            segments = _fallback_generate(req)

        # 記錄（失敗不影響回應）
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
        raise exc
    except Exception:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 新增：聊天模式主流程 =========
def _assistant_reply_stub(user_text: str) -> str:
    """沒有外部模型時的簡易助理回覆，避免前端卡住。"""
    user_text = (user_text or "").strip()
    return (
        "我已讀取你的需求，先給你一版初稿：\n"
        "1) 依平台與時長拆成片頭/主體/收尾。\n"
        "2) 每段附上鏡位與畫面感，最後提供一個 CTA。\n"
        "若需更口語/更具體的場景，直接回覆告訴我。"
        + (f"\n\n（摘要：{user_text[:60]}…）" if user_text else "")
    )

@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    """
    前端期望：
    { session_id, assistant_message, segments?, copy? }
    """
    # 取最後一則 user 訊息作為本地生成依據
    last_user = ""
    for m in reversed(req.messages or []):
        if m.role.lower() == "user":
            last_user = m.content or ""
            break

    # 產段落（優先走本地 fallback；你之後可換成更完整的 LLM 流程）
    gen_req = GenerateReq(user_input=last_user, previous_segments=req.previous_segments or [])
    if GOOGLE_API_KEY:
        try:
            segments = _gemini_generate(gen_req)
        except Exception:
            segments = _fallback_generate(gen_req)
    else:
        segments = _fallback_generate(gen_req)

    assistant_message = _assistant_reply_stub(last_user)
    session_id = req.session_id or f"s-{uuid.uuid4().hex[:8]}"

    # 可選：同時給一份文案草稿（讓文案頁可即刻顯示）
    copy = CopyOut(
        main_copy=f"主貼文草稿：{(last_user or '你的主題')}，提出痛點→亮點→承諾→行動呼籲。",
        alternates=["開頭A：拋問題吸引注意","開頭B：拎數據建立信任","開頭C：講一個極短故事"],
        hashtags=["#短影音","#行銷","#AI"],
        cta="立即私訊/點連結了解詳情"
    )

    # 寫入簡易聊天紀錄（失敗不擋回應）
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chats (session_id, user_id, messages_json, segments_json, copy_json) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                req.user_id or "",
                json.dumps([m.model_dump() for m in req.messages], ensure_ascii=False),
                json.dumps([s.model_dump() for s in segments], ensure_ascii=False),
                json.dumps(copy.model_dump(), ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[DB] chat insert failed:", e)

    return ChatResp(
        session_id=session_id,
        assistant_message=assistant_message,
        segments=segments,
        copy=copy,
        error=None,
    )

# ========= 偏好/回饋（前端已在使用） =========
class UpdatePrefsReq(BaseModel):
    user_id: Optional[str] = None
    prefs: dict = Field(default_factory=dict)

@app.post("/update_prefs")
def update_prefs(req: UpdatePrefsReq):
    # Demo：接受即回，後續你可把 prefs 存 DB
    return {"ok": True}

class FeedbackReq(BaseModel):
    user_id: Optional[str] = None
    kind: str
    note: Optional[str] = None

@app.post("/feedback")
def feedback(req: FeedbackReq):
    # Demo：接受即回，後續你可把回饋存 DB
    return {"ok": True}
