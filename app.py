# app.py
import os
import json
import sqlite3
from typing import List, Optional, Any, Dict, Tuple
from datetime import datetime, timedelta

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
    allow_origins=["*"],            # 若上線建議鎖網域白名單
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
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_input TEXT,
            previous_segments_json TEXT,
            response_json TEXT
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT,
            message TEXT,
            assistant TEXT,
            segments_json TEXT
        );
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

# ========= Pydantic 模型 =========
class Segment(BaseModel):
    type: str = Field(default="場景")
    # 0–6s 這種秒數區間，強制要求
    time: Optional[str] = ""       # ex: "0–6s"
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    cta: Optional[str] = ""

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# 聊天模式
class ChatReq(BaseModel):
    user_id: Optional[str] = "web-user"
    message: str
    tone: Optional[str] = "neutral"
    style: Optional[str] = "concise"
    language: Optional[str] = "zh-TW"
    max_len: Optional[int] = 800

class ChatResp(BaseModel):
    assistant: str
    segments: List[Segment] = Field(default_factory=list)

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
      <p>POST <code>/generate_script</code> or <code>/chat_generate</code> with JSON body.</p>
      <pre>{
  "user_input": "hi",
  "previous_segments": []
}</pre>
    </body></html>
    """

# ========= 內建知識庫（濃縮） =========
KB_FLOW = """
【流量技巧】
1) 開場3秒要有鉤⼦、動態視覺或反差；2) 人設鮮明；3) 節奏快、每 3–5 秒一個信息點；
4) 鏡位切換配合情緒（特寫=情緒、半身=操作、遠景=收尾）；5) 口語+押節奏，句長 <= 18字。
"""

KB_FRAME = """
【常用橋段】
A) 開場：問題/反差/金句；B) 鋪陳：痛點→場景→轉折；C) 解法：步驟/演示/對比；
D) 證據：數據/見證/前後對照；E) CTA：下一步與誘因。
"""

KB_SCRIPT_RULES = """
【腳本輸出規範】
- 產出 0–60 秒完整腳本，分 6–8 段，每段必含：time(秒數區間)、type、camera、dialog、visual、cta。
- dialog 必須是「真實可朗讀台詞」，**禁止**出現「說明痛點→亮點→轉折」這種抽象指令或概述字樣。
- dialog 每句不超過 18 個中文字；可多行；允許口語、擬聲詞。
- camera 寫鏡位/運鏡（例：特寫/半身/全景、推/拉/搖/跟）。
- visual 寫畫面元素/置景/道具（可列點）。
- CTA 必須具體（例：點連結了解更多/免費試用/私訊領清單）。
- 僅輸出 JSON 陣列（不要任何多餘文字）。
"""

def build_system_prompt() -> str:
    return (
        "你是專業的短影音腳本顧問，熟悉 IG Reels/YouTube Shorts/TikTok。"
        "請依據輸入，直接產出可拍攝的腳本。"
        + KB_FLOW + KB_FRAME + KB_SCRIPT_RULES
    )

# ========= 產生段落（舊 API，保留） =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
    step = len(req.previous_segments)
    pick_type = "片頭" if step == 0 else ("片尾" if step >= 2 else "場景")
    short = (req.user_input or "")[:30]
    return [
        Segment(
            type=pick_type,
            time="0–6s" if step == 0 else ("52–60s" if step >= 2 else "6–16s"),
            camera=(
                "特寫主角臉部，燈光從右側打入，聚焦眼神。"
                if step == 0 else
                "半身跟拍，移至桌面，快速推近產品。" if step == 1
                else "遠景收尾，主角背對夜景，鏡頭緩慢拉遠。"
            ),
            dialog=(
                f"你是否也曾這樣想過？{short}。"
                if step == 0 else
                f"把難的變簡單。{short}，現在就開始。" if step == 1
                else "行動永遠比等待重要。現在，輪到你了。"
            ),
            visual=(
                "字幕彈入：#關鍵主題；LOGO 淡入。"
                if step == 0 else
                "快切 B-roll：鍵盤、定時器、杯中冰塊；節奏對齊拍點。" if step == 1
                else "LOGO 收合、CTA 卡片滑入（左下）。"
            ),
            cta=("點連結了解更多" if step < 2 else "立即私訊領清單"),
        )
    ]

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    import google.generativeai as genai

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    sys = build_system_prompt()
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成完整 0–60 秒短影音腳本。"

    prompt = (
        f"{sys}\n"
        f"使用者輸入: {user}\n"
        f"已接受段落(previous_segments): {json.dumps(prev, ensure_ascii=False)}\n"
        f"輸出格式範例（JSON 陣列）：\n"
        f'[{{"type":"片頭","time":"0–6s","camera":"特寫/推近","dialog":"一句一句可朗讀台詞","visual":"置景/道具","cta":"點連結了解更多"}},'
        f'{{"type":"場景","time":"6–12s","camera":"半身/跟拍","dialog":"...","visual":"...","cta":""}}]'
    )

    res = model.generate_content(prompt)
    text = (res.text or "").strip()

    # 擷取最外層 JSON 陣列
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last != -1 and last > first:
        text = text[first:last+1]

    data = json.loads(text)
    segments: List[Segment] = []
    for item in data:
        segments.append(
            Segment(
                type=item.get("type", "場景"),
                time=item.get("time", ""),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
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
            except Exception as _:
                segments = _fallback_generate(req)
        else:
            segments = _fallback_generate(req)

        # 紀錄
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
    except HTTPException:
        raise
    except Exception:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 聊天模式（新） =========

def _is_keyword_mode(msg: str) -> bool:
    s = (msg or "").strip()
    # 非標點、字數很短 → 視為關鍵詞/單字
    return len(s) <= 4

def _chat_with_gemini(system: str, user: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(f"{system}\n{user}")
    return (getattr(res, "text", None) or "").strip()

def _extract_json_array(text: str) -> Optional[list]:
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(text[first:last+1])
        except Exception:
            return None
    return None

def _parse_segments_from_text(text: str) -> List[Segment]:
    data = _extract_json_array(text) or []
    segs: List[Segment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        segs.append(
            Segment(
                type=item.get("type", "場景"),
                time=item.get("time", ""),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
                cta=item.get("cta", ""),
            )
        )
    return segs

@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    """
    前端以 /chat_generate 對話：
      request: { user_id, message, tone, style, language, max_len }
      response: { assistant: str, segments: [ ... ] }
    """
    if not req.message.strip():
        raise HTTPException(400, "empty_message")

    # 去重：同 user_id 1 分鐘內相同訊息避免重覆
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT message FROM chats WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (req.user_id or "web-user",),
        )
        row = cur.fetchone()
        if row and (row[0] or "").strip() == req.message.strip():
            # 直接回覆一個輕量提示，避免 UI 出現連發重覆
            return ChatResp(assistant="我收到了，正在思考更好的版本…你也可以補充：行業/平台/時長/目標/風格。", segments=[])
    except Exception:
        pass

    # 核心 prompt：兩種模式
    segments: List[Segment] = []
    if not GOOGLE_API_KEY:
        # 沒 key：回本地模板 + 提示
        draft = _fallback_generate(GenerateReq(user_input=req.message, previous_segments=[]))
        assistant = (
            "我先給你一版 0–60 秒草稿（示意）。若要更精準，請補充：行業、平台、時長、要推產品/主題、"
            "對白口吻與 CTA 偏好。\n你也可以說「生成完整腳本」。"
        )
        segments = draft
    else:
        sys = build_system_prompt()

        if _is_keyword_mode(req.message):
            # 關鍵詞模式：先方向+澄清，再給完整草稿（JSON）
            user = (
                f"關鍵詞：{req.message}\n"
                "請先輸出：\n"
                "1) 三個創意方向（每個一句話標題+一句話策略），2) 三個澄清問題（幫我更快確定風格），"
                "3) 然後直接給 0–60 秒完整腳本（遵守【腳本輸出規範】，只用 JSON 陣列，不要多餘文字）。"
            )
            raw = _chat_with_gemini(sys, user)
            # 嘗試抽出 JSON 陣列做右側時間軸
            segments = _parse_segments_from_text(raw)
            # assistant 留下「方向+問題」文字（去掉 JSON 部分）
            json_part = raw.find("[")
            assistant = raw[:json_part].strip() if json_part != -1 else raw
        else:
            # 直接腳本模式：生成完整 0–60s 腳本（只 JSON）
            user = (
                f"使用者需求：{req.message}\n"
                "請直接產出 0–60 秒完整腳本（6–8 段），僅輸出 JSON 陣列，禁止多餘文字。"
            )
            raw = _chat_with_gemini(sys, user)
            segments = _parse_segments_from_text(raw)
            assistant = (
                "我依據你的描述，已產出 0–60 秒完整腳本在右側。"
                "需要我改『口吻、場景、平台規格或 CTA』，直接告訴我。"
            )
            if not segments:
                # 若模型沒產 JSON，就降級出一版草稿
                segments = _fallback_generate(GenerateReq(user_input=req.message, previous_segments=[]))
                assistant = "我先給你一版草稿，你可以補充行業/平台/時長/CTA，我會優化成完整 0–60 秒腳本。"

    # 存聊天紀錄
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chats (user_id, message, assistant, segments_json) VALUES (?, ?, ?, ?)",
            (
                req.user_id or "web-user",
                req.message,
                assistant,
                json.dumps([s.model_dump() for s in segments], ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[DB] chat insert failed:", e)

    return ChatResp(assistant=assistant, segments=segments)
