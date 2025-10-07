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
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# ========= App 與 CORS =========
app = FastAPI(title="AI Script Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.on_event("startup")
def on_startup():
    try:
        init_db()
        print(f"[BOOT] SQLite path OK: {DB_PATH}")
    except Exception as e:
        print("[BOOT] DB init failed:", e)

# ========= 模型 =========
class Segment(BaseModel):
    type: str = Field(default="場景")
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    start_sec: Optional[int] = None
    end_sec: Optional[int] = None
    cta: Optional[str] = ""

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

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

# ========= 錯誤處理 =========
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail or "http_error"})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": "internal_server_error"})

# ========= 健康檢查 & 靜態 =========
@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/favicon.ico")
def favicon(): return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><body>
      <h3>AI Backend OK</h3>
      <p>POST <code>/generate_script</code> 或 <code>/chat_generate</code> with JSON body.</p>
    </body></html>
    """

# ========= 基礎產生器 =========
def _fallback_generate_single(req: GenerateReq) -> List[Segment]:
    """單段（續寫用）"""
    step = len(req.previous_segments)
    pick_type = "片頭" if step == 0 else ("片尾" if step >= 2 else "場景")
    short = (req.user_input or "")[:30]
    return [
        Segment(
            type=pick_type,
            camera=("特寫主角臉部，燈光從右側打入，聚焦眼神。" if step == 0
                    else "半身跟拍，移至桌面，快速推近產品。" if step == 1
                    else "遠景收尾，主角背對夜景，鏡頭緩慢拉遠。"),
            dialog=("你是否也曾這樣想過？" + short + " —— 用 30 秒改變你的看法。" if step == 0
                    else "把難的變簡單。" + short + "，現在就開始。" if step == 1
                    else "行動永遠比等待重要。現在，輪到你了。"),
            visual=("字幕彈入：#加班也能健身；LOGO 淡入。" if step == 0
                    else "快切 B-roll：鍵盤、定時器、杯中冰塊；節奏對齊拍點。" if step == 1
                    else "LOGO 收合、CTA 卡片滑入（左下）。"),
            start_sec=0, end_sec=6, cta="點連結了解更多"
        )
    ]

def _fallback_generate_full(user_text: str) -> List[Segment]:
    """一次給完整 0–60s（6 段）"""
    topic = (user_text or "你的主題").strip()[:40]
    # 時長切段（可依需求調）
    timeline = [
        ("片頭",   0,  6,  "特寫 / 快速吸睛"),
        ("場景",   6, 16,  "半身 / 跟拍"),
        ("場景",  16, 28,  "手部 / 產品特寫"),
        ("場景",  28, 40,  "對焦 / 情境切換"),
        ("場景",  40, 52,  "遠近交替 / 節奏加速"),
        ("片尾",  52, 60,  "遠景收尾 / LOGO / CTA"),
    ]
    segments: List[Segment] = []
    for i, (t, a, b, cam_hint) in enumerate(timeline, 1):
        segments.append(
            Segment(
                type=t,
                start_sec=a, end_sec=b,
                camera=f"{cam_hint}。主題：{topic}",
                dialog=(
                    "開場丟問題，建立共鳴。"
                    if t == "片頭" else
                    "說明痛點→亮點→轉折，口語且有節奏。"
                    if i in (2,3,4,5) else
                    "總結價值，給清楚行動呼籲。"
                ),
                visual=(
                    "大字卡 + 快速跳切 + 音效點。"
                    if t == "片頭" else
                    "B-roll 穿插：操作畫面、數據、使用前後對比。"
                    if i in (2,3,4,5) else
                    "LOGO 收合，CTA 卡片滑入；結尾音效。"
                ),
                cta="點連結了解更多 / 預約體驗"
            )
        )
    return segments

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    """（可選）用 Gemini；此處仍保留單段，完整稿可擴寫 prompt 再切段"""
    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    system_prompt = (
        "你是短影音腳本助手。輸出 JSON 陣列，每個元素含 "
        "type(片頭|場景|片尾)、camera、dialog、visual、"
        "start_sec、end_sec、cta。不要加註解或多餘文字。"
    )
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成下一個分段。"
    prompt = (
        f"{system_prompt}\n"
        f"使用者輸入: {user}\n"
        f"previous_segments: {json.dumps(prev, ensure_ascii=False)}\n"
        f"請僅回傳 JSON 陣列。"
    )
    res = model.generate_content(prompt)
    text = (res.text or "").strip()
    first_bracket = text.find("["); last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        text = text[first_bracket:last_bracket + 1]
    data = json.loads(text)
    out: List[Segment] = []
    for item in data:
        out.append(
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
    return out

# ========= 舊流程（保留） =========
@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        if GOOGLE_API_KEY:
            try:
                segments = _gemini_generate(req)
            except Exception:
                segments = _fallback_generate_single(req)
        else:
            segments = _fallback_generate_single(req)

        # 記錄（非阻塞）
        try:
            conn = get_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, previous_segments_json, response_json) VALUES (?, ?, ?)",
                (
                    req.user_input,
                    json.dumps([s.model_dump() for s in req.previous_segments], ensure_ascii=False),
                    json.dumps([s.model_dump() for s in segments], ensure_ascii=False),
                ),
            )
            conn.commit(); conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return GenerateResp(segments=segments, error=None)
    except HTTPException as exc:
        raise exc
    except Exception:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 聊天模式 =========
def _needs_full_script(text: str) -> bool:
    if not text: return False
    kw = ["完整", "0-60", "0～60", "0到60", "全段", "全流程", "完整腳本", "完整版", "整支", "完整的腳本"]
    t = text.lower()
    return any(k in text or k in t for k in kw)

def _assistant_reply_for(full: bool) -> str:
    if full:
        return ("我先依你的主題輸出一版 0–60 秒完整腳本：片頭→主體 4 段→片尾，"
                "每段附鏡位、台詞與畫面感，並標註秒數與 CTA。你可點選卡片加入右側時間軸，或直接要求我微調。")
    return ("我先補一個段落作為延續，附鏡位、台詞與畫面感。"
            "若你要一次看到 0–60 秒完整腳本，直接跟我說「給我完整腳本」。")

@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    # 取最後一則 user 訊息
    last_user = ""
    for m in reversed(req.messages or []):
        if m.role.lower() == "user":
            last_user = m.content or ""
            break

    # 判斷是否一次給完整 0–60s
    want_full = _needs_full_script(last_user) or len(req.previous_segments or []) == 0

    if want_full:
        segments = _fallback_generate_full(last_user)
    else:
        # 單段續寫：可換成 _gemini_generate(req2)
        gen_req = GenerateReq(user_input=last_user, previous_segments=req.previous_segments or [])
        if GOOGLE_API_KEY:
            try:
                segments = _gemini_generate(gen_req)
            except Exception:
                segments = _fallback_generate_single(gen_req)
        else:
            segments = _fallback_generate_single(gen_req)

    session_id = req.session_id or f"s-{uuid.uuid4().hex[:8]}"
    assistant_message = _assistant_reply_for(want_full)

    # 也給文案草稿（文案頁可直接用）
    copy = CopyOut(
        main_copy=f"主貼文草稿：{(last_user or '你的主題')} → 痛點→亮點→承諾→CTA。",
        alternates=["開頭A：拋問題吸睛","開頭B：引用數據建立信任","開頭C：極短故事帶入"],
        hashtags=["#短影音","#腳本","#行銷"],
        cta="點連結了解更多 / 私訊領取方案"
    )

    # 紀錄（非阻塞）
    try:
        conn = get_conn(); cur = conn.cursor()
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
        conn.commit(); conn.close()
    except Exception as e:
        print("[DB] chat insert failed:", e)

    return ChatResp(
        session_id=session_id,
        assistant_message=assistant_message,
        segments=segments,
        copy=copy,
        error=None,
    )

# ========= 偏好/回饋 =========
class UpdatePrefsReq(BaseModel):
    user_id: Optional[str] = None
    prefs: dict = Field(default_factory=dict)

@app.post("/update_prefs")
def update_prefs(req: UpdatePrefsReq):
    return {"ok": True}

class FeedbackReq(BaseModel):
    user_id: Optional[str] = None
    kind: str
    note: Optional[str] = None

@app.post("/feedback")
def feedback(req: FeedbackReq):
    return {"ok": True}
