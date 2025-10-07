# app.py
import os
import json
import sqlite3
from typing import List, Optional, Any, Dict, Union
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

# ========= Env =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
# 同時相容兩種命名
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# ========= App / CORS =========
app = FastAPI(title="AI Script/Copy Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ========= DB Tools =========
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
            response_json TEXT,
            task TEXT
        )
        """
    )
    conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    try:
        init_db()
        print(f"[BOOT] SQLite OK: {DB_PATH}")
    except Exception as e:
        print("[BOOT] DB init failed:", e)

# ========= Schemas =========
class Segment(BaseModel):
    type: Optional[str] = Field(default=None, description="片頭/場景/片尾 or hook/value/cta")
    start_sec: Optional[int] = None
    end_sec: Optional[int] = None
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    cta: Optional[str] = ""

class CopyProduct(BaseModel):
    main_copy: Optional[str] = ""
    alternates: Optional[List[str]] = Field(default_factory=list)
    hashtags: Optional[List[str]] = Field(default_factory=list)
    cta: Optional[str] = ""

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatGenerateReq(BaseModel):
    # 通用
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    messages: Optional[List[ChatMessage]] = Field(default_factory=list)
    previous_segments: Optional[List[Segment]] = Field(default_factory=list)
    remember: Optional[bool] = False
    # 明確指定任務（不帶也可，自動判斷）
    task: Optional[str] = Field(default=None, description="script | copy")

class ChatGenerateResp(BaseModel):
    session_id: Optional[str] = None
    assistant_message: Optional[str] = ""
    segments: Optional[List[Segment]] = Field(default_factory=list)
    copy: Optional[CopyProduct] = None
    error: Optional[str] = None

class GenerateReq(BaseModel):
    user_input: Optional[str] = ""
    previous_segments: Optional[List[Segment]] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# ========= Errors =========
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail or "http_error"})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": "internal_server_error"})

# ========= Health / Static =========
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
      <p>POST <code>/chat_generate</code> or <code>/generate_script</code></p>
    </body></html>
    """

# ========= Helpers =========
def _too_short(text: str) -> bool:
    if not text:
        return True
    # 粗估：中文字兩倍長度
    t = text.strip()
    return len(t) < 36  # 大約 18 全形字

def _detect_task(messages: List[ChatMessage], explicit: Optional[str]) -> str:
    if explicit in ("copy", "script"):
        return explicit
    last = (messages or [])[-1].content.lower() if messages else ""
    # 簡單偵測：提到 linkedin/ig/facebook/hashtag/貼文 視為文案
    if any(k in last for k in ["linkedin", "ig", "instagram", "facebook", "小紅書", "貼文", "hashtags", "#"]):
        return "copy"
    return "script"

def _fewshot_rules_script() -> str:
    return (
        "你是短影音腳本顧問，請輸出 JSON 陣列，每個元素代表一段："
        "字段：type(hook|value|cta)、camera、dialog(口白/字幕稿)、visual(畫面描述)、cta。"
        "請以 0-60秒的節奏拆成 3-6 段，每段約 6-12 秒，語氣口語、節奏感。"
    )

def _fewshot_rules_copy() -> str:
    return (
        "你是社群文案顧問，請輸出 JSON 物件："
        "{ main_copy: string, alternates: string[], hashtags: string[], cta: string }。"
        "要求：結構化、可直接貼上平台；hashtags 不少於 8 個；語氣貼近平台。"
    )

def _run_gemini(prompt: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(prompt)
    return (res.text or "").strip()

# ========= Generators =========
def _fallback_script(req_text: str, previous_segments: List[Segment]) -> List[Segment]:
    step = len(previous_segments)
    labels = [("hook","CU"), ("value","MS"), ("cta","WS")]
    i = min(step, len(labels)-1)
    t, cam = labels[i]
    return [Segment(
        type=t, camera=cam,
        dialog=f"（示例口白）依你需求的 {req_text[:24]} …",
        visual="示例畫面：快切 B-roll + 重點字卡",
        cta="點連結瞭解更多"
    )]

def _gemini_script(req_text: str, previous_segments: List[Segment]) -> List[Segment]:
    base = _fewshot_rules_script()
    prev = [s.model_dump() for s in previous_segments]
    prompt = (
        f"{base}\n"
        f"使用者需求：{req_text}\n"
        f"已加入時間軸：{json.dumps(prev, ensure_ascii=False)}\n"
        "請僅回傳 JSON 陣列，形如："
        '[{"type":"hook","camera":"CU","dialog":"...","visual":"...","cta":"..."}]'
    )
    text = _run_gemini(prompt)
    first, last = text.find("["), text.rfind("]")
    if first != -1 and last > first:
        text = text[first:last+1]
    data = json.loads(text)
    out: List[Segment] = []
    # 自動補秒數
    base_start = 0
    for idx, item in enumerate(data):
        d = Segment(
            type=item.get("type") or "value",
            camera=item.get("camera",""),
            dialog=item.get("dialog",""),
            visual=item.get("visual",""),
            cta=item.get("cta",""),
            start_sec=item.get("start_sec") or base_start,
            end_sec=item.get("end_sec") or base_start + 8
        )
        base_start = d.end_sec or (base_start+8)
        out.append(d)
    return out

def _fallback_copy(req_text: str) -> CopyProduct:
    return CopyProduct(
        main_copy=f"（示例文案）{req_text} 的社群貼文初稿：以痛點引入、亮點說服、CTA 收束。",
        alternates=["開頭A：抓痛點","開頭B：拋數據","開頭C：口語反問"],
        hashtags=["#行銷","#品牌","#短影音","#社群","#成長","#案例","#技巧","#CTA"],
        cta="點擊連結了解更多"
    )

def _gemini_copy(req_text: str) -> CopyProduct:
    base = _fewshot_rules_copy()
    prompt = (
        f"{base}\n"
        f"使用者需求/主題：{req_text}\n"
        "請只回傳 JSON 物件，形如："
        '{"main_copy":"...","alternates":["...","..."],"hashtags":["#a","#b"],"cta":"..."}'
    )
    text = _run_gemini(prompt)
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        text = text[first:last+1]
    data = json.loads(text)
    return CopyProduct(
        main_copy=data.get("main_copy",""),
        alternates=data.get("alternates") or [],
        hashtags=data.get("hashtags") or [],
        cta=data.get("cta","")
    )

# ========= Endpoints =========
@app.post("/chat_generate", response_model=ChatGenerateResp)
def chat_generate(req: ChatGenerateReq):
    # 取最後一句作為 user_input
    last_text = ""
    if req.messages:
        last_text = req.messages[-1].content or ""
    task = _detect_task(req.messages, req.task)

    # 太短先友善引導
    if _too_short(last_text):
        tip = "內容有點太短了 🙏 請補充：行業/平台/目標/主題/受眾（例如：「電商｜IG｜購買｜新品開箱｜目標顧客大學生」）。"
        return ChatGenerateResp(
            session_id=req.session_id or "sess",
            assistant_message=tip,
            segments=[],
            copy=None,
            error=None
        )

    try:
        if GEMINI_API_KEY:
            if task == "copy":
                copy_out = _gemini_copy(last_text)
                assistant = "我先給你第一版完整文案（可再加要求，我會幫你改得更貼近風格）。"
                resp = ChatGenerateResp(
                    session_id=req.session_id or "sess",
                    assistant_message=assistant,
                    segments=[],
                    copy=copy_out
                )
            else:
                segs = _gemini_script(last_text, req.previous_segments or [])
                assistant = "我先補一個段落作為延續，附鏡位、台詞與畫面感。若你要一次看到 0–60 秒完整腳本，直接跟我說「給我完整腳本」。"
                resp = ChatGenerateResp(
                    session_id=req.session_id or "sess",
                    assistant_message=assistant,
                    segments=segs,
                    copy=None
                )
        else:
            # 沒有 API Key，使用 fallback
            if task == "copy":
                copy_out = _fallback_copy(last_text)
                assistant = "目前未提供 API Key，先用規則產生文案草稿供你微調。"
                resp = ChatGenerateResp(
                    session_id=req.session_id or "sess",
                    assistant_message=assistant,
                    copy=copy_out
                )
            else:
                segs = _fallback_script(last_text, req.previous_segments or [])
                assistant = "目前未提供 API Key，先用規則生成一版草稿供你微調。"
                resp = ChatGenerateResp(
                    session_id=req.session_id or "sess",
                    assistant_message=assistant,
                    segments=segs
                )

        # DB 記錄（非必要）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, previous_segments_json, response_json, task) VALUES (?, ?, ?, ?)",
                (
                    last_text,
                    json.dumps([s.model_dump() for s in (req.previous_segments or [])], ensure_ascii=False),
                    json.dumps(resp.model_dump(), ensure_ascii=False),
                    task
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return resp
    except HTTPException:
        raise
    except Exception as e:
        return ChatGenerateResp(
            session_id=req.session_id or "sess",
            error="internal_server_error",
            assistant_message="系統忙碌，稍後再試或補充更具體的需求。",
        )

@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    # 舊流程保留
    user_text = req.user_input or ""
    if _too_short(user_text):
        tip_seg = Segment(
            type="提示",
            dialog="內容太短，請補：行業/平台/時長/目標/主題（例如：電商｜Reels｜30秒｜購買｜夏季新品開箱）。"
        )
        return GenerateResp(segments=[tip_seg])

    try:
        if GEMINI_API_KEY:
            segs = _gemini_script(user_text, req.previous_segments or [])
        else:
            segs = _fallback_script(user_text, req.previous_segments or [])
        # 記錄
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, previous_segments_json, response_json, task) VALUES (?, ?, ?, ?)",
                (
                    user_text,
                    json.dumps([s.model_dump() for s in (req.previous_segments or [])], ensure_ascii=False),
                    json.dumps([s.model_dump() for s in segs], ensure_ascii=False),
                    "script"
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)
        return GenerateResp(segments=segs, error=None)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})
