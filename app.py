# app.py
import os
import json
import sqlite3
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, validator

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")  # 可不設；不設時走 fallback

# ========= App 與 CORS =========
app = FastAPI(title="AI Script Backend", version="2025-10-07")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ========= DB 工具（失敗不致命） =========
def _ensure_db_dir(path: str):
    db_dir = os.path.dirname(path) or "."
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception:
        pass

def get_conn() -> sqlite3.Connection:
    _ensure_db_dir(DB_PATH)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    try:
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
                payload_json TEXT,
                response_json TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                kind TEXT,
                note TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                prefs_json TEXT
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[BOOT] DB init failed:", e)

@app.on_event("startup")
def on_startup():
    init_db()
    print(f"[BOOT] SQLite path: {DB_PATH}")

# ========= 系統知識庫（讓模型一次吐完整腳本包） =========
SYSTEM_KB = """
你是一名「短影音技術顧問 & 腳本導演」。你的任務是：根據使用者輸入的行業、平台、目標、時長與主題，
直接產出一次可用的「完整創意腳本包」，而不是泛用建議。

【口吻與原則】
- 口吻：真實口語、節奏快、有情緒、有畫面。
- 內容要「可拍可演」：每段給出明確鏡位、對白（可口播台詞）、畫面感（B-roll/動作/道具）。
- 結構必須完整覆蓋全片時長（例如 0–60 秒），且每段區間不可重疊、不可缺口。
- 優先根據：行業爆款套路、常見鉤子（Hook）、反轉、強 CTA。

【內部知識庫（精簡）】
- 流量技巧：鉤子要強、情緒強、反轉、卡點、字幕節奏、對比/前後對照。
- 視頻策劃：受眾明確、場景貼近日常、賣點抓 1–2 個、畫面動起來（移動鏡頭/切 B-roll）。
- 視頻結構（參考）：開場鉤子(0–5s) → 價值鋪陳(5–25s) → 高潮/轉折(15–25s) → 收尾 CTA(25–30s/或 60s)。
- 文案結構：情緒 + 亮點 + 轉折 + CTA；字幕適度加 emoji，口語自然。

【輸出格式（只回傳 JSON，勿夾雜說明）】
{
  "assistant_message": "對使用者的 1 句簡短回覆（概括思路，非重複內容）",
  "segments": [
    {
      "start_sec": 0,
      "end_sec": 6,
      "type": "hook|value|demo|proof|cta|outro",
      "camera": "CU|MS|WS 等 + 是否移動/轉場",
      "dialog": "可直接口播/字幕的台詞（請自然口語）",
      "visual": "畫面感/B-roll/動作/道具/場景",
      "cta": "若本段需要 CTA，否則留空"
    }
  ],
  "creative_notes": {
    "alt_hooks": ["備選鉤子 A","備選鉤子 B","備選鉤子 C"],
    "shooting_tips": [
      "運鏡/燈光/場景佈置/BGM 等具體建議（可拍可執行）",
      "字卡與字幕節奏、表情/動作建議",
      "如果有產品：如何特寫/對比/前後效果"
    ]
  }
}

【嚴格要求】
- 必須是合法 JSON，不能有註解或多餘文字。
- 段數自動按時長分配（15/30/60 秒），常見 3–6 段；每段 5–12 秒左右。
- 若使用者只丟一個詞，也要主動補齊題材（先假設行業與平台、再生腳本），但語氣要禮貌地把假設寫進第一段對白。
"""

# ========= Pydantic 模型 =========
class Segment(BaseModel):
    type: str = Field(default="場景")
    start_sec: Optional[int] = None
    end_sec: Optional[int] = None
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

class ChatMessage(BaseModel):
    role: str
    content: str

    @validator("role")
    def _role_ok(cls, v):
        v = (v or "").strip().lower()
        if v not in {"user", "assistant", "system"}:
            raise ValueError("role must be user|assistant|system")
        return v

class ChatReq(BaseModel):
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    previous_segments: List[Segment] = Field(default_factory=list)
    remember: Optional[bool] = False

class ChatResp(BaseModel):
    session_id: str
    assistant_message: str
    segments: List[Segment] = Field(default_factory=list)
    creative_notes: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class UpdatePrefsReq(BaseModel):
    user_id: Optional[str] = None
    prefs: Dict[str, Any] = Field(default_factory=dict)

class FeedbackReq(BaseModel):
    user_id: Optional[str] = None
    kind: str  # thumbs_up | thumbs_down
    note: Optional[str] = None

# ========= 通用工具 =========
def _safe_db_log(endpoint: str, payload: dict, response: dict, user_input: str = ""):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO requests (endpoint, user_input, payload_json, response_json) VALUES (?, ?, ?, ?)",
            (endpoint, user_input, json.dumps(payload, ensure_ascii=False), json.dumps(response, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[DB] log failed:", e)

# ========= 錯誤處理 =========
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail or "http_error"})

@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    print("[UNHANDLED]", repr(exc))
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
    <html><body style="font-family: ui-sans-serif, system-ui">
      <h3>AI Script Backend OK</h3>
      <p>POST <code>/chat_generate</code> 或 <code>/generate_script</code> with JSON body.</p>
      <pre style="background:#f6f7fb;padding:10px;border-radius:8px">/chat_generate example:
{
  "user_id": "web-abc",
  "session_id": null,
  "messages": [{"role":"user","content":"行業: 電商｜平台: Reels｜時長: 30秒｜目標: 購買｜主題: 夏季新品開箱"}],
  "previous_segments": [],
  "remember": false
}</pre>
    </body></html>
    """

# ========= 生成（舊流程 /generate_script） =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
    step = len(req.previous_segments)
    pick_type = "片頭" if step == 0 else ("片尾" if step >= 2 else "場景")
    short = (req.user_input or "")[:30]
    seg = Segment(
        type=pick_type,
        camera=("CU 特寫眼神" if step == 0 else "MS 半身跟拍" if step == 1 else "WS 遠景拉遠"),
        dialog=(
            f"你是否也曾這樣想過？{short} —— 用 30 秒改變你的看法。"
            if step == 0 else
            f"把難的變簡單。{short}，現在就開始。"
            if step == 1 else
            "行動永遠比等待重要。現在，輪到你了。"
        ),
        visual=("彈入 #主題 標籤" if step == 0 else "快切 B-roll 與拍點" if step == 1 else "LOGO 收合 + CTA 卡片"),
        cta=("點我了解更多" if step >= 2 else "")
    )
    # 粗配秒數
    seg.start_sec = step * 6
    seg.end_sec = seg.start_sec + 6
    return [seg]

def _fallback_chat(req: ChatReq) -> Dict[str, Any]:
    # 產一份 0–60s 標準 6 段
    text = (req.messages[-1].content if req.messages else "")[:30]
    segs: List[Segment] = []
    labels = [("hook", "CU"), ("value", "MS"), ("value", "MS"), ("demo", "MS"), ("proof", "WS"), ("cta", "WS")]
    for i, (t, cam) in enumerate(labels):
        s = Segment(
            type=t,
            start_sec=i*10,
            end_sec=i*10+10,
            camera=cam,
            dialog=f"{'開場鉤子' if i==0 else '內容'}：{text}…",
            visual="B-roll + 字卡節奏",
            cta="點連結領取" if t == "cta" else ""
        )
        segs.append(s)
    return {
        "session_id": req.session_id or "fallback-session",
        "assistant_message": "這是模擬回覆：已依你的需求產生 0–60s 初稿。",
        "segments": [s.dict() for s in segs],
        "creative_notes": {
            "alt_hooks": ["不想再拖了嗎？", "30秒告訴你關鍵", "真相其實很簡單"],
            "shooting_tips": ["上強對比字卡", "節奏卡點剪輯", "多用移動鏡頭與手部特寫"]
        }
    }

def _gemini_chat(req: ChatReq) -> Dict[str, Any]:
    # 延遲載入，避免沒裝套件時直接崩
    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    history = [{"role": m.role, "content": m.content} for m in req.messages[-10:]]
    user_block = {
        "messages_tail": history,
        "previous_segments": [s.dict() for s in req.previous_segments],
    }
    prompt = SYSTEM_KB + "\n\n[使用者上下文]\n" + json.dumps(user_block, ensure_ascii=False) + "\n只回傳 JSON。"

    res = model.generate_content(prompt)
    text = (res.text or "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1 or j <= i:
        raise ValueError("model_return_not_json")
    data = json.loads(text[i:j+1])

    # 保險轉型（避免 key 缺漏或型別怪）
    session_id = req.session_id or "chat-"  # 不傳也給預設
    assistant_message = str(data.get("assistant_message", "")).strip() or "腳本已生成。"
    raw_segments = data.get("segments") or []
    segments: List[Segment] = []
    for item in raw_segments:
        segments.append(
            Segment(
                type=str(item.get("type", "場景") or "場景"),
                start_sec=int(item.get("start_sec") or 0),
                end_sec=int(item.get("end_sec") or 0),
                camera=str(item.get("camera", "") or ""),
                dialog=str(item.get("dialog", "") or ""),
                visual=str(item.get("visual", "") or ""),
                cta=str(item.get("cta", "") or ""),
            )
        )
    creative_notes = data.get("creative_notes") or {}

    return {
        "session_id": session_id,
        "assistant_message": assistant_message,
        "segments": [s.dict() for s in segments],
        "creative_notes": creative_notes,
    }

# ========= 路由：聊天式生成 =========
@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    try:
        if GOOGLE_API_KEY:
            try:
                out = _gemini_chat(req)
            except Exception as e:
                print("[Gemini] fail -> fallback:", repr(e))
                out = _fallback_chat(req)
        else:
            out = _fallback_chat(req)

        # DB log（不影響回應）
        _safe_db_log("/chat_generate", payload=req.dict(), response=out, user_input=(req.messages[-1].content if req.messages else ""))

        return ChatResp(
            session_id=out["session_id"] or (req.session_id or "chat-session"),
            assistant_message=out.get("assistant_message", "OK"),
            segments=[Segment(**s) for s in out.get("segments", [])],
            creative_notes=out.get("creative_notes"),
            error=None,
        )
    except HTTPException as exc:
        raise exc
    except Exception as e:
        print("[chat_generate] error:", repr(e))
        return JSONResponse(status_code=500, content={"session_id": req.session_id or "chat-session", "assistant_message": "", "segments": [], "creative_notes": None, "error": "internal_server_error"})

# ========= 路由：舊流程 =========
@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        if GOOGLE_API_KEY:
            # 這裡沿用 fallback 策略；若你想改用 Gemini 也可呼叫 _gemini_chat 的 segments
            try:
                # 直接包一份 chat 形式呼叫，方便維護
                chat_req = ChatReq(
                    user_id=None,
                    session_id=None,
                    messages=[ChatMessage(role="user", content=req.user_input or "請生成下一段 30 秒短影音腳本")],
                    previous_segments=req.previous_segments,
                    remember=False,
                )
                out = _gemini_chat(chat_req) if GOOGLE_API_KEY else _fallback_chat(chat_req)
                segs = [Segment(**s) for s in out.get("segments", [])]
                if not segs:
                    segs = _fallback_generate(req)
            except Exception as _:
                segs = _fallback_generate(req)
        else:
            segs = _fallback_generate(req)

        resp = GenerateResp(segments=segs, error=None)

        _safe_db_log("/generate_script", payload=req.dict(), response=resp.dict(), user_input=req.user_input)
        return resp
    except HTTPException as exc:
        raise exc
    except Exception as e:
        print("[generate_script] error:", repr(e))
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 路由：偏好 / 回饋 =========
@app.post("/update_prefs")
def update_prefs(req: UpdatePrefsReq):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO prefs (user_id, prefs_json) VALUES (?, ?)",
            (req.user_id or "", json.dumps(req.prefs, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[DB] prefs insert failed:", e)
    return {"ok": True}

@app.post("/feedback")
def feedback(req: FeedbackReq):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedback (user_id, kind, note) VALUES (?, ?, ?)",
            (req.user_id or "", req.kind, req.note or ""),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[DB] feedback insert failed:", e)
    return {"ok": True}
