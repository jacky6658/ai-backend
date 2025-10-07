# app.py
import os
import json
import sqlite3
import re
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, ConfigDict

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")  # 可不設；不設時用本地 fallback

# ========= App 與 CORS =========
app = FastAPI(title="AI Script Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],         # 允許所有方法，避免 OPTIONS/POST 被擋
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
            endpoint TEXT,
            user_input TEXT,
            meta_json TEXT,
            previous_segments_json TEXT,
            response_json TEXT
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

# ========= Pydantic 模型（舊流程）=========
class Segment(BaseModel):
    type: str = Field(default="場景")
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    # 新增欄位給右側時間軸更好用（可選）
    start_s: Optional[int] = None
    end_s: Optional[int] = None
    cta: Optional[str] = None

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# ========= Chat 專用（寬鬆，避免 422）=========
class ChatReq(BaseModel):
    # 全部可選，extra=allow 讓前端多送欄位也不會 422
    model_config = ConfigDict(extra="allow")
    user_id: Optional[str] = None
    text: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = "zh-TW"
    style: Optional[str] = None
    max_len: Optional[int] = 800
    previous_segments: Optional[List[Dict[str, Any]]] = None

class ChatResp(BaseModel):
    reply: str
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# ========= 錯誤處理 =========
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
  "text": "給我完整腳本",
  "previous_segments": []
}</pre>
    </body></html>
    """

# ========= 產生段落（舊流程） =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
    step = len(req.previous_segments)
    pick_type = "片頭" if step == 0 else ("片尾" if step >= 2 else "場景")
    short = (req.user_input or "")[:30]
    base = Segment(
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
    return [base]

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    import google.generativeai as genai  # 延遲載入
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    system_prompt = (
        "你是短影音腳本助手。輸出 JSON 陣列，每個元素含 type(片頭|場景|片尾)、"
        "camera、dialog、visual 三欄；可選 start_s/end_s/cta；不要多餘文字。"
    )
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成一段 30 秒短影音腳本的下一個分段。"

    prompt = (
        f"{system_prompt}\n"
        f"使用者輸入: {user}\n"
        f"已接受段落(previous_segments): {json.dumps(prev, ensure_ascii=False)}\n"
        f"請僅回傳 JSON 陣列，如: "
        f'[{{"type":"場景","camera":"...","dialog":"...","visual":"...","start_s":0,"end_s":5}}]'
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
                start_s=item.get("start_s"),
                end_s=item.get("end_s"),
                cta=item.get("cta"),
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

        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (endpoint, user_input, meta_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "/generate_script",
                    req.user_input,
                    json.dumps({}, ensure_ascii=False),
                    json.dumps([s.model_dump() for s in req.previous_segments], ensure_ascii=False),
                    json.dumps([s.model_dump() for s in segments], ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return GenerateResp(segments=segments, error=None)
    except HTTPException:
        raise
    except Exception:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= Chat（新：對話 + 一次輸出 0–60s）=========
FULL_SCRIPT_HINTS = [
    "完整腳本", "0-60", "0～60", "0至60", "完整分鏡", "全段", "全套腳本", "一整段腳本"
]

def _asks_full_script(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    for kw in FULL_SCRIPT_HINTS:
        if kw in t:
            return True
    # 很短而且像「給我完整腳本」「馬卡」「要腳本」
    if len(t) <= 8 and re.search(r"(腳本|script|分鏡)", t):
        return True
    return False

def _make_full_0_60_segments(topic: str = "品牌曝光/轉換", platform: str = "Reels") -> List[Segment]:
    # 一次回傳 6 段 0~60s
    return [
        Segment(type="片頭", start_s=0,  end_s=6,  camera="鏡位: 半身/跟拍 | 移至畫面 | 快速進近產品",
                dialog="把難的變簡單。現在開始。", visual="快切 B-roll：鍵盤、定時器、杯中冰塊；節奏對拍點。", cta="點連結了解更多"),
        Segment(type="場景", start_s=6,  end_s=16, camera="鏡位: 操作畫面/數據 | 使用前/後對比",
                dialog="痛點→亮點，口語節奏。", visual="圖表/字幕節奏化呈現", cta="預約體驗"),
        Segment(type="場景", start_s=16, end_s=28, camera="鏡位: 操作畫面/數據 | 使用前/後對比",
                dialog="再補一個關鍵利益，仍然口語節奏。", visual="產品/服務關鍵畫面", cta="了解更多"),
        Segment(type="場景", start_s=28, end_s=40, camera="鏡位: 情境/人物帶入 | 情境切換",
                dialog="案例/情境簡述，爽感片段。", visual="亮點畫面串接", cta="立即試試"),
        Segment(type="場景", start_s=40, end_s=52, camera="鏡位: 產品重點/Logo/CTA",
                dialog="總結價值，呼應痛點，輕鬆一句 punch line。", visual="Logo + CTA 卡片滑入；結尾高效", cta="點此開始"),
        Segment(type="片尾", start_s=52, end_s=60, camera="鏡位: 收尾/Logo | 穩定 BGM",
                dialog="行動呼籲 + 一句收尾話術。", visual="CTA 留白、結尾場景", cta="限時優惠 · 立即行動"),
    ]

def _chat_fallback_reply(text: str) -> str:
    # 自然回覆（避免一成不變模板）
    return (
        "收到！我先幫你釐清一下：\n"
        "1) 想做哪個平台（IG Reels / TikTok / Shorts）？\n"
        "2) 片長與口吻（15s/30s、中性/活潑/專業）？\n"
        "3) 目標是曝光、互動還是轉換？\n"
        "回我以上 3 點，我就能直接給你可拍的腳本段落；若要一次看完 0–60 秒腳本，也可以直接跟我說「給我完整腳本」。"
    )

@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    try:
        text = (req.text or "").strip()
        prev = req.previous_segments or []

        segments: List[Segment] = []
        reply: str = ""

        # 使用者想要完整 0–60 秒腳本
        if _asks_full_script(text):
            segments = _make_full_0_60_segments()
            reply = "這是 0–60 秒的一鏡到底腳本，右側已顯示 6 段時間軸，你可逐段加入或一次建置。"
        else:
            # 一般對話：若有 key 可走 Gemini，否則走 fallback 自然問句
            if GOOGLE_API_KEY:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=GOOGLE_API_KEY)
                    model = genai.GenerativeModel(GEMINI_MODEL)
                    system = (
                        "你是短影音顧問，請用自然口語提供具體建議、可以一步步逼近腳本。避免重覆句型。"
                    )
                    prompt = f"{system}\n使用者: {text}\n已選段落: {json.dumps(prev, ensure_ascii=False)}"
                    res = model.generate_content(prompt)
                    reply = (res.text or "").strip() or _chat_fallback_reply(text)
                except Exception:
                    reply = _chat_fallback_reply(text)
            else:
                reply = _chat_fallback_reply(text)

        # 寫 DB（不影響回應）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (endpoint, user_input, meta_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "/chat_generate",
                    text,
                    json.dumps({"tone": req.tone, "style": req.style, "language": req.language, "max_len": req.max_len}, ensure_ascii=False),
                    json.dumps(prev, ensure_ascii=False),
                    json.dumps({"reply": reply, "segments": [s.model_dump() for s in segments]}, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return ChatResp(reply=reply, segments=segments, error=None)

    except HTTPException:
        raise
    except Exception:
        return JSONResponse(status_code=500, content={"reply": "", "segments": [], "error": "internal_server_error"})
