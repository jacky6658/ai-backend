# app.py
import os
import json
import re
import sqlite3
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")  # 可不設；不設時用本地 fallback

# ========= 知識庫（輕量內建，可擴充） =========
# 這裡放你腦圖裡的共通「流量技巧 / 視覺焦點 / 節奏套路 / 文案結構」等摘要，作為模型的 domain hint
SYSTEM_KB = """
你是短影音顧問與拍攝腳本專家。請依下列原則輸出：
- 流量技巧：5秒內吸睛鉤子、對比、懸念、數字/清單、轉場節奏、口語 punch line。
- 視覺焦點：主體清晰、畫面層次（前中後景/景別切換）、關鍵道具、B-roll 補畫。
- 節奏與段落：Hook(0~5s) → Value(5~25s，可2~3小段) → CTA(25~30s)；若時長非30s，等比縮放。
- 文案調性：口語、具畫面感、避免制式敘述，重視『對白、畫面、重點』的對齊。
- CTA 結尾清楚：追蹤/私訊/點連結/預約/領取等。

輸出時不要贅詞，不要『說明痛點→亮點→轉折』這類模板語。務必給出可直接拍攝的腳本。
"""

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
    # 也順手放聊天記錄（非必要）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT,
            session_id TEXT,
            messages_json TEXT,
            assistant_json TEXT
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

# v2 對話模式
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatReq(BaseModel):
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    messages: List[ChatMessage]
    previous_segments: Optional[List[Segment]] = Field(default_factory=list)
    remember: Optional[bool] = False

class ChatResp(BaseModel):
    session_id: Optional[str] = None
    assistant_message: str = ""
    segments: Optional[List[Segment]] = None
    copy: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class PrefsReq(BaseModel):
    user_id: Optional[str] = None
    prefs: Dict[str, Any] = Field(default_factory=dict)

class FeedbackReq(BaseModel):
    user_id: Optional[str] = None
    kind: str
    note: Optional[str] = None

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
      <p>POST <code>/generate_script</code>（舊流程）與 <code>/chat_generate</code>（新對話）</p>
    </body></html>
    """

# ========= 工具 =========
def short_text(msg: str) -> bool:
    """判斷是否過短（中文約 6~10 字以下 或 英文 < 3 個詞）"""
    s = (msg or "").strip()
    if not s:
        return True
    # 粗略偵測：中文字數
    zh_chars = re.findall(r"[\u4e00-\u9fff]", s)
    if len(zh_chars) <= 6:
        # 英文詞數
        words = re.findall(r"[A-Za-z0-9]+", s)
        if len(words) < 3:
            return True
    return False

def make_guidance() -> str:
    return (
        "我需要更多資訊才能幫你生成可拍攝的完整腳本。\n"
        "請補充：行業、平台（Reels/Shorts/TikTok…）、時長（15/30/60秒）、目標（導流/購買/品牌）、主題與受眾痛點/賣點。\n"
        "例如：行業: 電商｜平台: Reels｜時長: 30秒｜目標: 購買｜主題: 夏季新品開箱｜受眾: 上班族｜賣點: 輕薄速乾。"
    )

def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None

def _rule_fallback(user_text: str, total_sec: int = 30) -> List[Segment]:
    """無模型或解析失敗時的本地規則稿，保證前端不會空白"""
    hook_end = min(5, total_sec // 6)
    mid_end = max(total_sec - 5, 10)
    if mid_end <= hook_end:
        mid_end = hook_end + 10
    return [
        Segment(
            type="hook",
            start_sec=0, end_sec=hook_end,
            camera="CU",
            dialog=f"開場鉤子：{user_text[:18]}…",
            visual="快切 B-roll + 動態字幕",
            cta=""
        ),
        Segment(
            type="value",
            start_sec=hook_end, end_sec=min(mid_end, total_sec-5),
            camera="MS",
            dialog="三個重點講清楚，口語 punch line。",
            visual="對焦產品/數據圖/使用對比",
            cta=""
        ),
        Segment(
            type="cta",
            start_sec=min(total_sec-5, mid_end), end_sec=total_sec,
            camera="WS",
            dialog="行動呼籲口播，收束。",
            visual="大字卡 + Logo",
            cta="點連結領取 / 立即私訊"
        ),
    ]

# ========= Gemini 生成（結構化 Prompt + few-shot） =========
def _build_struct_prompt(user_text: str, prev: List[dict]) -> str:
    """
    產生一個『明確格式』的提示，要求模型輸出 JSON 陣列，每個元素含：
    type(start with: hook/value/cta)、start_sec、end_sec、camera、dialog、visual、cta
    """
    exemplar = {
        "type": "hook",
        "start_sec": 0,
        "end_sec": 5,
        "camera": "CU",
        "dialog": "鉤子對白…",
        "visual": "鏡頭與畫面描述…",
        "cta": ""
    }
    prompt = f"""
{SYSTEM_KB}

你必須根據使用者主題，直接輸出「JSON 陣列」並且每一段對應拍攝用的欄位。
不要輸出多餘說明，不要包在 code block 裡，更不要加「解釋」。
每個元素欄位必須包含：type (必為 hook/value/cta 三類之一)、start_sec、end_sec、camera、dialog、visual、cta。

- 時長：若使用者未指定，預設 30 秒（0~30s），Hook 約 0~5s，Value 5~25s（可分 2 段 value），CTA 25~30s。
- 語言：沿用使用者語言（優先繁體中文）。
- 口氣：口語、畫面感強烈、可直接拍。

已接受段落 previous_segments（可作上下文延續）：
{json.dumps(prev, ensure_ascii=False)}

使用者輸入：
{user_text}

✅ 僅回傳 JSON 陣列，例如：
[
  {json.dumps(exemplar, ensure_ascii=False)},
  {{
    "type": "value",
    "start_sec": 5,
    "end_sec": 22,
    "camera": "MS",
    "dialog": "…",
    "visual": "…",
    "cta": ""
  }},
  {{
    "type": "cta",
    "start_sec": 22,
    "end_sec": 30,
    "camera": "WS",
    "dialog": "…",
    "visual": "…",
    "cta": "點連結…"
  }}
]
"""
    return prompt.strip()

def _gemini_generate_segments(user_text: str, prev_segments: List[Segment]) -> List[Segment]:
    import google.generativeai as genai  # 延遲載入
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prev = [s.model_dump() for s in prev_segments]
    prompt = _build_struct_prompt(user_text, prev)

    res = model.generate_content(prompt)
    text = (res.text or "").strip()

    # 嘗試只提取第一個 JSON 陣列
    lb = text.find("["); rb = text.rfind("]")
    if lb != -1 and rb != -1 and rb > lb:
        text = text[lb:rb+1]

    data = safe_json_loads(text)
    if not isinstance(data, list):
        raise ValueError("model_return_not_json")

    segments: List[Segment] = []
    for item in data:
        segments.append(
            Segment(
                type=item.get("type", "value"),
                start_sec=item.get("start_sec"),
                end_sec=item.get("end_sec"),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
                cta=item.get("cta", ""),
            )
        )
    # 確保類型合理且時間不重疊（簡單修正）
    for i, s in enumerate(segments):
        if s.start_sec is None or s.end_sec is None or s.end_sec <= s.start_sec:
            s.start_sec = i * 6
            s.end_sec = s.start_sec + 6
        if s.type not in ("hook", "value", "cta"):
            s.type = "value"
    return segments

# ========= 舊流程：/generate_script =========
def _fallback_generate(req: GenerateReq) -> List[Segment]:
    step = len(req.previous_segments)
    pick_type = "hook" if step == 0 else ("cta" if step >= 2 else "value")
    short = (req.user_input or "")[:30]
    base = _rule_fallback(short or "你的主題", 30)
    # 調整第一段 type 與台詞以符合舊習慣
    base[0].type = pick_type
    return base

@app.post("/generate_script", response_model=GenerateResp)
def generate_script(req: GenerateReq):
    try:
        # 若輸入太短，直接回友善引導（保持 200，segments 空，讓前端顯示文字）
        if short_text(req.user_input):
            return GenerateResp(segments=[], error=None)  # 前端顯示 assistant 提示由 /chat_generate，舊流程僅給空列表

        if GOOGLE_API_KEY:
            try:
                segs = _gemini_generate_segments(req.user_input, req.previous_segments)
            except Exception as _:
                segs = _fallback_generate(req)
        else:
            segs = _fallback_generate(req)

        # 記錄 DB（忽略失敗）
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
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 新：/chat_generate（對話模式） =========
@app.post("/chat_generate", response_model=ChatResp)
def chat_generate(req: ChatReq):
    """
    - 保留你前端契約：回傳 assistant_message、segments（可選）、copy（可選）
    - 若用戶輸入過短：不丟 422，改回 200 + 引導文（assistant_message），segments 空
    - 一律嘗試輸出「可拍攝」的分段（Hook/Value/CTA），格式固定，便於右欄時間軸
    """
    try:
        # 1) 取得最後一則使用者訊息
        last_user = ""
        for m in reversed(req.messages):
            if m.role == "user":
                last_user = (m.content or "").strip()
                break

        if not last_user:
            return ChatResp(
                session_id=req.session_id or "s0",
                assistant_message="我需要一段你的主題描述，才能開始協作喔。",
                segments=[],
                copy=None,
                error=None
            )

        # 2) 過短則回友善引導（200）
        if short_text(last_user):
            return ChatResp(
                session_id=req.session_id or "s0",
                assistant_message=make_guidance(),
                segments=[],
                copy=None,
                error=None
            )

        # 3) 正常生成
        if GOOGLE_API_KEY:
            try:
                segs = _gemini_generate_segments(last_user, req.previous_segments or [])
                assistant = "我根據你的需求，已產生 0~30s 可直接拍攝的分段腳本。"
            except Exception as _:
                segs = _rule_fallback(last_user, 30)
                assistant = "模型解析失敗，我先用規則生成一版草稿供你微調。"
        else:
            segs = _rule_fallback(last_user, 30)
            assistant = "目前未提供 API Key，先用規則生成一版草稿供你微調。"

        # 4) 記錄 DB（忽略失敗）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chats (user_id, session_id, messages_json, assistant_json) VALUES (?, ?, ?, ?)",
                (
                    req.user_id or "",
                    req.session_id or "",
                    json.dumps([m.model_dump() for m in req.messages], ensure_ascii=False),
                    json.dumps({"assistant_message": assistant, "segments": [s.model_dump() for s in segs]}, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] chat insert failed:", e)

        return ChatResp(
            session_id=req.session_id or "s1",
            assistant_message=assistant,
            segments=segs,
            copy=None,
            error=None
        )
    except HTTPException as exc:
        raise exc
    except Exception as e:
        return JSONResponse(status_code=500, content={"assistant_message":"", "segments":[], "error":"internal_server_error"})

# ========= 偏好 & 回饋（維持契約） =========
@app.post("/update_prefs")
def update_prefs(req: PrefsReq):
    # 這裡簡單回 OK，若你要存 DB 可自行擴充
    return {"ok": True}

@app.post("/feedback")
def feedback(req: FeedbackReq):
    # 這裡簡單回 OK，若你要存 DB 可自行擴充
    return {"ok": True}
