# app.py
import os
import json
import sqlite3
import time
import re
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
    # 既有：請求記錄表（保留）
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
    # 聊天會話（保留）
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
    # 新增：使用者長期記憶（key-value）與片段記憶（自由文本）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profile(
            user_id TEXT PRIMARY KEY,
            prefs_json TEXT,           -- 個人偏好（tone, language, writing_style, max_length等）
            counters_json TEXT,        -- 回饋計數（thumbs_up/down等）
            updated_at INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_memory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            content TEXT,              -- 記住的一句話/事實/偏好
            importance INTEGER,        -- 1~5
            tags TEXT,                 -- 逗號分隔
            created_at INTEGER
        )
        """
    )
    # 新增：內部「思考草稿」存檔（不回傳給前端）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_thought(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            user_id TEXT,
            scratchpad TEXT,
            created_at INTEGER
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

# ========= 共用：回覆錯誤處理 =========
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
      <p>POST <code>/generate_script</code> with JSON body.</p>
      <pre>{
  "user_input": "hi",
  "previous_segments": []
}</pre>
      <p>Chat API: <code>/chat_generate</code>（支援 user_id / 記憶 / 學習）</p>
    </body></html>
    """

# ========= 產生段落主流程（沿用） =========
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
        )
    ]

def _gemini_generate(req: GenerateReq) -> List[Segment]:
    import google.generativeai as genai
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

        # 記錄
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

# ========= 聊天 + 記憶 + 學習 =========
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    user_id: Optional[str] = None                 # ★ 新增：用來綁定記憶
    messages: List[ChatMessage] = Field(default_factory=list)
    previous_segments: List[Dict[str, Any]] = Field(default_factory=list)
    remember: Optional[bool] = False              # ★ 勾選後，會嘗試把本輪重點寫入記憶

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

# ----- 記憶與偏好 -----
DEFAULT_PREFS = {
    "tone": "friendly",          # 語氣
    "language": "zh-TW",         # 回覆語系
    "writing_style": "concise",  # 文風：concise/balanced/rich
    "max_length": 1200,          # 回覆最大字數
}

def _get_or_create_profile(user_id: str) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT prefs_json, counters_json FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    if row:
        prefs = json.loads(row[0]) if row[0] else {}
        counters = json.loads(row[1]) if row[1] else {}
    else:
        prefs = DEFAULT_PREFS.copy()
        counters = {"thumbs_up": 0, "thumbs_down": 0}
        cur.execute(
            "INSERT INTO user_profile(user_id, prefs_json, counters_json, updated_at) VALUES(?,?,?,?)",
            (user_id, json.dumps(prefs, ensure_ascii=False), json.dumps(counters, ensure_ascii=False), int(time.time()))
        )
        conn.commit()
    conn.close()
    return {"prefs": prefs, "counters": counters}

def _update_profile(user_id: str, prefs: Dict[str, Any] = None, counters: Dict[str, Any] = None):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT prefs_json, counters_json FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    old_prefs = json.loads(row[0]) if row and row[0] else {}
    old_counters = json.loads(row[1]) if row and row[1] else {}
    if prefs:
        old_prefs.update(prefs)
    if counters:
        for k, v in counters.items():
            old_counters[k] = old_counters.get(k, 0) + int(v)
    cur.execute(
        "REPLACE INTO user_profile(user_id, prefs_json, counters_json, updated_at) VALUES(?,?,?,?)",
        (user_id, json.dumps(old_prefs, ensure_ascii=False), json.dumps(old_counters, ensure_ascii=False), int(time.time()))
    )
    conn.commit()
    conn.close()

def _save_memory(user_id: str, content: str, importance: int = 2, tags: List[str] = None):
    if not content:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_memory(user_id, content, importance, tags, created_at) VALUES(?,?,?,?,?)",
        (user_id, content, max(1, min(importance, 5)), ",".join(tags or []), int(time.time()))
    )
    conn.commit()
    conn.close()

def _recent_memories(user_id: str, limit: int = 10) -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT content FROM user_memory WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]

def _extract_memories_from_text(text: str) -> List[str]:
    """
    超輕量規則：從使用者文字找可持久化的事實/偏好。
    真正上線可換成 NER/分類器或 LLM 兩階段抽取。
    """
    memories = []
    # 個人稱呼 / 名字
    m = re.search(r"(我叫|我的名字是|可以叫我)([^，。,！!?\s]{1,12})", text)
    if m:
        memories.append(f"使用者偏好稱呼：{m.group(2)}")
    # 偏好語氣/語言
    if "用台灣繁體" in text or "用繁體中文" in text:
        memories.append("偏好語言：zh-TW")
    if "幽默" in text:
        memories.append("偏好語氣：humorous")
    if "專業" in text or "正式" in text:
        memories.append("偏好語氣：professional")
    # 類別興趣
    if "短影音" in text:
        memories.append("興趣：短影音創作")
    return memories

def _compose_system_preamble(user_prefs: Dict[str, Any], recent_memory: List[str]) -> str:
    tone = user_prefs.get("tone", "friendly")
    lang = user_prefs.get("language", "zh-TW")
    style = user_prefs.get("writing_style", "concise")
    limit = int(user_prefs.get("max_length", 1200))

    mem = "；".join(recent_memory[:8]) if recent_memory else "（無）"
    return (
        f"你是短影音腳本與文案助理，請用 {lang} 回覆；語氣 {tone}；文風 {style}；"
        f"請限制在 {limit} 字內。綜合考慮使用者的長期記憶：{mem}。"
        f"輸出結果時，先完成最終回覆給使用者，並額外提供一份 segments JSON（type/camera/dialog/visual）。"
        f"不要在回覆中包含你的思考過程或草稿。"
    )

# ----- 內部思考草稿（不回傳，僅存 DB）-----
def _save_thought(session_id: str, user_id: Optional[str], scratch: str):
    if not scratch:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO assistant_thought(session_id, user_id, scratchpad, created_at) VALUES(?,?,?,?)",
        (session_id, user_id or "", scratch, int(time.time()))
    )
    conn.commit()
    conn.close()

# ----- 會話工具 -----
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

# ----- 核心：由聊天生成分段（沿用你的生成器）-----
def _generate_segments_with_context(user_input: str, prev_segments: List[Dict[str, Any]]) -> List[Segment]:
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

# ----- Chat API -----
@chat_router.post("/chat_generate", response_model=ChatResponse)
def chat_generate(req: ChatRequest):
    # 1) session & user
    sid = _ensure_session(req.session_id)
    uid = req.user_id or "anon"

    # 2) 讀/建使用者偏好與最近記憶
    profile = _get_or_create_profile(uid)
    prefs = profile["prefs"]
    recent_mem = _recent_memories(uid, limit=10)

    # 3) 寫入本輪 user 訊息
    new_user_msgs = [m for m in req.messages if m.role == "user"]
    if new_user_msgs:
        _save_messages(sid, new_user_msgs)

    # 4) 組合「系統前言 + 會話」成一段 user_input，送進既有生成器
    history = _load_history(sid)
    preamble = _compose_system_preamble(prefs, recent_mem)
    chat_txt = "\n".join([f"{'使用者' if m.role=='user' else '助理'}: {m.content}" for m in history])
    user_input_for_gen = (
        f"{preamble}\n\n"
        f"以下是雙方對話紀錄：\n{chat_txt}\n\n"
        f"請先決定整體腳本策略（只在內部思考，勿回給使用者），"
        f"接著輸出最終回覆（中文）；並產生 segments JSON 陣列。"
    )

    # 5) （可選）做一份「內部思考草稿」文字——我們這裡用簡單規劃字串示意並存檔
    scratch = (
        "【內部草稿】規劃本輪回覆步驟：\n"
        "1) 先重述使用者目標\n2) 產出短影音主線與風格\n3) 給 1~2 段分段\n4) 給文案CTA\n"
        "（此草稿不會回傳給前端，只存DB便於日後分析品質）"
    )
    _save_thought(sid, uid, scratch)

    # 6) 生成 segments（沿用你的生成器）
    segments_objs = _generate_segments_with_context(user_input_for_gen, req.previous_segments)
    segments = [s.model_dump() for s in segments_objs]

    # 7) 產出可讀回覆（assistant_message）
    #    這裡用非常簡化的模板。若有 Google Key，你也可以再丟一次 LLM 讓口吻更貼偏好。
    tone_hint = {
        "friendly": "親切口語",
        "humorous": "帶點幽默",
        "professional": "專業清楚"
    }.get(prefs.get("tone", "friendly"), "親切口語")

    assistant_message = (
        f"好的，這輪我會用「{tone_hint}」風格來協助你。\n"
        f"我已先規劃一本短影音的主線，並產出新的分段，"
        f"你可以直接說「調快節奏」「更口語」或提供主題方向，我會接著優化。"
    )

    # 8) 記憶抽取（顯式 remember=true 或自動從最新 user 訊息抓）
    if uid:
        if req.remember:
            # 顯式記憶：把最後一則 user 訊息整句存起來（importance=3）
            if new_user_msgs:
                _save_memory(uid, new_user_msgs[-1].content, importance=3, tags=["explicit"])
        else:
            # 自動抽取：從使用者訊息中撈偏好/事實（importance=2）
            if new_user_msgs:
                autos = []
                for m in new_user_msgs:
                    autos.extend(_extract_memories_from_text(m.content))
                for item in autos[:5]:
                    _save_memory(uid, item, importance=2, tags=["auto"])

    # 9) 紀錄助理回覆
    _save_messages(sid, [ChatMessage(role="assistant", content=assistant_message)])

    return ChatResponse(
        session_id=sid,
        assistant_message=assistant_message,
        segments=segments,
        error=None
    )

# 回饋 API：讓前端送 thumbs_up/down，做「學習」
class FeedbackReq(BaseModel):
    user_id: Optional[str] = None
    kind: Literal["thumbs_up", "thumbs_down"]
    note: Optional[str] = None

@app.post("/feedback")
def feedback(req: FeedbackReq):
    uid = req.user_id or "anon"
    if req.kind not in ("thumbs_up", "thumbs_down"):
        raise HTTPException(status_code=400, detail="invalid kind")
    _update_profile(uid, counters={req.kind: 1})
    # 策略：若連續多次 thumbs_down，可把 writing_style 改為 'concise' 或 tone 改 'professional'
    profile = _get_or_create_profile(uid)
    downs = profile["counters"].get("thumbs_down", 0)
    ups = profile["counters"].get("thumbs_up", 0)
    patched = {}
    if downs >= 3 and profile["prefs"].get("writing_style") != "concise":
        patched["writing_style"] = "concise"
    if ups >= 5 and profile["prefs"].get("tone") != "friendly":
        patched["tone"] = "friendly"
    if patched:
        _update_profile(uid, prefs=patched)
    return {"ok": True, "prefs": _get_or_create_profile(uid)["prefs"]}

# 偏好 API：讓前端直接更新偏好（語言、語氣…）
class PrefsReq(BaseModel):
    user_id: Optional[str] = None
    prefs: Dict[str, Any]

@app.post("/update_prefs")
def update_prefs(req: PrefsReq):
    uid = req.user_id or "anon"
    _update_profile(uid, prefs=req.prefs or {})
    return {"ok": True, "prefs": _get_or_create_profile(uid)["prefs"]}

# 掛上聊天路由
app.include_router(chat_router)


