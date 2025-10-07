# app.py
import os
import json
import sqlite3
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

# ================== 環境變數 ==================
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")

# 同時支援兩種名稱（你在 Zeabur 設的是 GEMINI_API_KEY）
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# ================== FastAPI & CORS ==================
app = FastAPI(title="AI Script Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ================== SQLite ==================
def _ensure_db_dir(path: str):
    db_dir = os.path.dirname(path) or "."
    os.makedirs(db_dir, exist_ok=True)

def get_conn() -> sqlite3.Connection:
    _ensure_db_dir(DB_PATH)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

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
    conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    try:
        init_db()
        print(f"[BOOT] DB OK @ {DB_PATH}")
    except Exception as e:
        print("[BOOT] DB init failed:", e)

# ================== Pydantic Models ==================
class Segment(BaseModel):
    type: str = Field(default="場景")      # 片頭/場景/片尾…等
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""
    cta: Optional[str] = ""
    start_sec: Optional[int] = None
    end_sec: Optional[int] = None

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

# 聊天模式（前端的 /chat_generate）
class ChatMessage(BaseModel):
    role: str
    content: str

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
    copy: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

# ================== 錯誤處理 ==================
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail or "http_error"})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": "internal_server_error"})

# ================== 健康檢查/首頁 ==================
@app.get("/healthz")
def healthz():
    return {"ok": True, "model": GEMINI_MODEL, "has_key": bool(GEMINI_API_KEY)}

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
def root():
    return """
    <html><body>
      <h3>AI Backend OK</h3>
      <p>Endpoints:</p>
      <ul>
        <li>POST <code>/chat_generate</code></li>
        <li>POST <code>/generate_script</code></li>
      </ul>
    </body></html>
    """

# ================== 內建知識庫（精簡提要） ==================
KNOWLEDGE_BULLETS = """
你是【短影音顧問 AI】。輸出務必遵循「Hook→中段→收尾 CTA」的節奏，口語自然、有 punch line。
每一段同時提供：對白（給人念的台詞）、畫面感（鏡頭/動作）、重點（導演備忘）。
"""

FEW_SHOT_STYLE = """
【格式範例（請嚴格套用）】
[Hook 0~5s]
🎤 對白：先投放關聯台詞（抓注意力）
🎬 畫面：切快鏡/字幕動態；主角半身或 CU
🔥 重點：開場 punch line + 亮點標註

[中段 5~25s]
🎤 對白：…（逐步鋪陳 2~3 個賣點）
🎬 畫面：…（示範/數據/觀眾反應）
🔥 重點：…（每小段 5~8s，有節奏）

[收尾 25~30s]
🎤 對白：…（總結利益點）
🎬 畫面：LOGO + CTA 卡片；微拉遠
📣 CTA：…（明確行動）

——
請把使用者主題融進「對白/畫面/重點」，不要回模板字樣。
語言：依使用者指定（預設繁體中文）；語氣：口語、節奏感。
"""

# ================== 產生（無 Key 時 fallback） ==================
def _fallback_segments(user_input: str, step_base: int = 0) -> List[Segment]:
    return [
        Segment(
            type="hook",
            camera="CU",
            dialog=f"開場鉤子：{(user_input or '這個主題').strip()}，你一定要看！",
            visual="快切 B-roll + 大字卡",
            cta="",
            start_sec=0,
            end_sec=5,
        ),
        Segment(
            type="value",
            camera="MS",
            dialog="三個重點快速講清楚，口語 punch line。",
            visual="對焦產品/厲害畫面/使用對比",
            cta="",
            start_sec=5,
            end_sec=12,
        ),
        Segment(
            type="cta",
            camera="WS",
            dialog="行動呼籲口播，收束。",
            visual="大字卡 + Logo",
            cta="點連結領取 / 立即私訊",
            start_sec=12,
            end_sec=20,
        ),
    ]

def _build_structured_prompt(messages: List[ChatMessage], language: str = "zh-TW") -> str:
    user_last = ""
    for m in reversed(messages):
        if m.role.lower() == "user":
            user_last = m.content.strip()
            break

    system = KNOWLEDGE_BULLETS + "\n" + FEW_SHOT_STYLE
    guide = f"語言：{language}。請直接輸出腳本文本（不要額外解說），依上方格式。"
    prompt = f"{system}\n\n使用者主題：{user_last}\n\n{guide}"
    return prompt

def _ensure_len_or_hint(messages: List[ChatMessage]) -> Optional[str]:
    """輸入太短時，回傳友善引導訊息；正常則 None。"""
    user_last = ""
    for m in reversed(messages):
        if m.role.lower() == "user":
            user_last = (m.content or "").strip()
            break
    if len(user_last) < 12:  # 自由調整閾值
        return "內容有點太短了 🙏 請告訴我：行業/平台/時長（秒）/目標/主題（例如：『電商｜Reels｜30秒｜購買｜夏季新品開箱』），我就能生成完整腳本。"
    return None

# ================== Gemini 生成 ==================
def _gemini_generate_text(prompt: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(prompt)
    return (res.text or "").strip()

def _parse_script_to_segments(text: str) -> List[Segment]:
    """
    盡力從結構化文本解析成 segments。
    支援你要求的三段（Hook / 中段 / 收尾），並補上預設秒數。
    """
    if not text:
        return []

    # 粗略切段
    blocks = []
    curr = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[Hook") or line.startswith("[中段") or line.startswith("[收尾") or line.lower().startswith("[hook"):
            if curr:
                blocks.append(curr)
                curr = []
        curr.append(line)
    if curr:
        blocks.append(curr)

    segs: List[Segment] = []
    default_ranges = [(0, 5), (5, 25), (25, 30)]
    for idx, b in enumerate(blocks[:3]):
        label = "scene"
        if "Hook" in b[0] or "hook" in b[0]:
            label = "hook"
        elif "收尾" in b[0]:
            label = "cta"
        else:
            label = "value"

        dialog = []
        visual = []
        cta = ""

        for ln in b:
            if "對白" in ln:
                dialog.append(ln.split("對白：", 1)[-1].strip())
            elif "畫面" in ln:
                visual.append(ln.split("畫面：", 1)[-1].strip())
            elif "CTA" in ln or "cta" in ln.lower():
                cta = ln.split("：", 1)[-1].strip()

        start, end = default_ranges[min(idx, len(default_ranges)-1)]
        segs.append(
            Segment(
                type=label,
                camera="CU" if label == "hook" else ("WS" if label == "cta" else "MS"),
                dialog="\n".join(dialog).strip(),
                visual="\n".join(visual).strip(),
                cta=cta,
                start_sec=start,
                end_sec=end,
            )
        )

    return segs

# ================== /chat_generate ==================
@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    # 1) 輸入太短 → 直接友善訊息（200）
    hint = _ensure_len_or_hint(req.messages)
    if hint:
        return ChatResp(
            session_id=req.session_id or "session-" "local",
            assistant_message=hint,
            segments=[],
            copy=None,
            error=None,
        )

    language = "zh-TW"
    # 前端偏好可能有另外一路送 /update_prefs，但我們可以從對話中推個預設
    try:
        if GEMINI_API_KEY:
            prompt = _build_structured_prompt(req.messages, language=language)
            text = _gemini_generate_text(prompt)
            if not text:
                raise RuntimeError("empty_model_output")
            segs = _parse_script_to_segments(text)
            # 如果模型沒照格式，仍提供 fallback 片段避免 UI 空白
            if not segs:
                segs = _fallback_segments(req.messages[-1].content if req.messages else "")

            return ChatResp(
                session_id=req.session_id or "session-model",
                assistant_message="我先給你第一版完整腳本（可再加要求，我會幫你改得更貼近風格）。",
                segments=segs,
                copy=None,
            )
        else:
            # 沒有 API key → 友善回覆 + fallback 片段
            segs = _fallback_segments(req.messages[-1].content if req.messages else "")
            return ChatResp(
                session_id=req.session_id or "session-fallback",
                assistant_message="目前未提供 API Key；先用規則產出第一版草稿給你微調。",
                segments=segs,
                copy=None,
            )
    except Exception as e:
        print("[chat_generate] error:", e)
        # 不丟 422/500，回 200 + 提示，避免前端一直跳 ❌
        return ChatResp(
            session_id=req.session_id or "session-error",
            assistant_message="系統忙碌或輸入格式較特殊，我已切換為保底草稿。你也可以補充行業/平台/時長/目標，我會升級成完整版本。",
            segments=_fallback_segments(req.messages[-1].content if req.messages else ""),
            copy=None,
            error=None,
        )

# ================== 舊流程：/generate_script ==================
def _gemini_generate_segments_via_prompt(user_input: str, previous_segments: List[Segment]) -> List[Segment]:
    prompt = f"""{KNOWLEDGE_BULLETS}

{FEW_SHOT_STYLE}

使用者主題：{user_input}
已接受段落（previous）：{json.dumps([s.model_dump() for s in previous_segments], ensure_ascii=False)}

請只輸出腳本文本（不要多餘說明）。
"""
    text = _gemini_generate_text(prompt)
    segs = _parse_script_to_segments(text)
    if not segs:
        segs = _fallback_segments(user_input)
    return segs

@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        if not req.user_input or len(req.user_input.strip()) < 6:
            # 友善指引，而不是 422
            return GenerateResp(
                segments=[],
                error="內容太短。請補充『行業/平台/時長(秒)/目標/主題』，例：電商｜Reels｜30秒｜購買｜夏季新品開箱。",
            )

        if GEMINI_API_KEY:
            try:
                segs = _gemini_generate_segments_via_prompt(req.user_input, req.previous_segments)
            except Exception:
                segs = _fallback_segments(req.user_input)
        else:
            segs = _fallback_segments(req.user_input)

        # 寫 DB（不影響回應）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, previous_segments_json, response_json) VALUES (?, ?, ?)",
                (
                    req.user_input,
                    json.dumps([s.model_dump() for s in req.previous_segments], ensure_ascii=False),
                    json.dumps([s.model_dump() for s in segs], ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return GenerateResp(segments=segs, error=None)
    except HTTPException as exc:
        raise exc
    except Exception as e:
        print("[/generate_script] error:", e)
        # 統一 JSON 200 + error 字串，避免前端拋 Exception
        return GenerateResp(segments=[], error="internal_server_error")
