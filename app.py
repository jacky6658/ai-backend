# app.py
import os
import json
import sqlite3
from typing import List, Optional, Any, Dict, Tuple
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
    # 舊接口紀錄
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
    # 對話記憶
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            mode TEXT DEFAULT 'auto'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    camera: Optional[str] = ""
    dialog: Optional[str] = ""
    visual: Optional[str] = ""

class GenerateReq(BaseModel):
    user_input: str = ""
    previous_segments: List[Segment] = Field(default_factory=list)

class GenerateResp(BaseModel):
    segments: List[Segment] = Field(default_factory=list)
    error: Optional[str] = None

class ChatMessage(BaseModel):
    role: str  # "system" / "user" / "assistant"
    content: str

class ChatReq(BaseModel):
    session_id: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    mode: str = "auto"  # "script" / "copy" / "auto"

class ChatResp(BaseModel):
    assistant_message: str
    metadata: Dict[str, Any] = {}

# ========= 內建知識庫（由你心智圖濃縮，可再擴充） =========
KB: Dict[str, Dict[str, List[str]]] = {
    "流量技巧": {
        "開頭鉤子": [
            "反常識/數字衝擊：『你以為…其實…』、『3秒看懂…』",
            "人設/場景切入：人物近景＋場景音效（鍵盤、電梯、鬧鐘）",
            "痛點質問：『為什麼你每天…卻…？』",
        ],
        "加速節奏": [
            "每 0.7–1.0 秒輕微鏡位變化（推、拉、移、晃），B-roll 快切",
            "關鍵詞上字幕，節奏點做呼吸位（0.1–0.2s 停頓）",
        ],
    },
    "視覺策略": {
        "鏡位模板": [
            "片頭：特寫主角臉部/手部 + 定向光，聚焦情緒",
            "場景：半身跟拍→桌面近距→產品特寫（推近）",
            "片尾：遠景拉遠 + LOGO/CTA 入場（左下或居中落版）",
        ],
        "畫面語言": [
            "動作與字幕對拍點；B-roll：鍵盤/計時器/走路/開門/城市轉場",
            "色彩：冷暖對比作情緒（冷→問題、暖→解法/成果）",
        ],
    },
    "腳本結構": {
        "短視頻三段式": [
            "開頭（0–3s）：強鉤子 + 提出矛盾/問題",
            "中段（3–20s）：解法步驟/案例演示/前後對比",
            "結尾（20–30s）：總結 + CTA（私訊/連結/收藏/關注）",
        ],
        "常見套路": [
            "故事式：人物→衝突→轉折→解決→結果",
            "清單式：3 步驟/5 招技巧，搭配序號字幕",
            "問答式：連環提問→揭示真相→一招帶走",
        ],
    },
    "CTA": {
        "類型": [
            "行動：『私訊我拿清單』、『點連結領模板』",
            "社交：『收藏這支，回頭照著拍』、『關注拿後續 Part2』",
            "轉化：『限時 48h，填表體驗』",
        ]
    },
    "時長節拍": {
        "通用 30s": [
            "0–3s 鉤子、3–12s 核心內容、高頻畫面變化",
            "12–24s 案例/步驟/對比、24–30s 收束 + 明確 CTA",
        ]
    }
}

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
      <p>POST <code>/chat</code> (新聊天模式) 或 <code>/generate_script</code> (舊分段模式)</p>
    </body></html>
    """

# ========= 小工具：知識檢索 / 提示詞生成 =========
def _simple_retrieve(topic: str, mode: str = "auto") -> Dict[str, List[str]]:
    """超輕量檢索：根據關鍵字挑選 KB 片段（無向量庫）。"""
    topic_lc = (topic or "").lower()
    picked: Dict[str, List[str]] = {}

    def add(cat: str, key: str, items: List[str]):
        picked.setdefault(f"{cat}/{key}", [])
        picked[f"{cat}/{key}"].extend(items)

    # 通用必帶
    add("流量技巧", "開頭鉤子", KB["流量技巧"]["開頭鉤子"])
    add("時長節拍", "通用 30s", KB["時長節拍"]["通用 30s"])

    # 行業/意圖簡單匹配（可再擴）
    if any(w in topic_lc for w in ["電商", "賣", "商品", "團購", "開箱", "優惠"]):
        add("視覺策略", "鏡位模板", KB["視覺策略"]["鏡位模板"])
        add("CTA", "類型", [KB["CTA"]["類型"][0], KB["CTA"]["類型"][2]])
    if any(w in topic_lc for w in ["個人品牌", "創作者", "學習", "自媒體", "經驗"]):
        add("腳本結構", "常見套路", KB["腳本結構"]["常見套路"])
        add("CTA", "類型", [KB["CTA"]["類型"][1]])

    # 模式導向
    if mode == "copy":
        add("腳本結構", "常見套路", ["清單式：3-5 點，句子短、口語、有表情符號可讀性"])
    else:
        add("視覺策略", "畫面語言", KB["視覺策略"]["畫面語言"])

    return picked

def _knowledge_block(picked: Dict[str, List[str]]) -> str:
    lines = []
    for k, arr in picked.items():
        lines.append(f"- {k}：")
        for it in arr:
            lines.append(f"  • {it}")
    return "\n".join(lines)

def _ensure_session(session_id: Optional[str], mode: str = "auto") -> str:
    """建立或回傳 session_id；並在 DB 建立紀錄。"""
    sid = session_id or os.urandom(8).hex()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sessions (session_id, mode) VALUES (?, ?)", (sid, mode))
    conn.commit()
    conn.close()
    return sid

def _save_messages(session_id: str, messages: List[ChatMessage]):
    if not messages:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        [(session_id, m.role, m.content) for m in messages],
    )
    conn.commit()
    conn.close()

def _lm_generate_text(prompt_text: str) -> str:
    """包一層模型呼叫；沒有 Key 就回本地預設。"""
    if not GOOGLE_API_KEY:
        return "（本地回覆）我已讀取你的主題，將以短影音顧問的方式提供腳本/文案建議。"

    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(prompt_text)
    return (res.text or "").strip()

# ========= 新接口：聊天模式 =========
@app.post("/chat", response_model=ChatResp)
def chat(req: ChatReq):
    sid = _ensure_session(req.session_id, req.mode)
    _save_messages(sid, req.messages)

    # 取最後一則 user 當「主題/需求」
    last_user = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_user = m.content.strip()
            break

    picked = _simple_retrieve(last_user, req.mode)
    kb_txt = _knowledge_block(picked)

    # 兩種模式的產出規範
    script_spec = (
        "若需求是『腳本』，請輸出：\n"
        "1)『分析』：受眾/痛點/策略（條列）\n"
        "2)『時間軸規劃』：0–3s、3–10s、10–20s、20–30s 各段要點\n"
        "3)『分段 JSON』：僅輸出 JSON 陣列（type/camera/dialog/visual），不要多餘文字\n"
    )
    copy_spec = (
        "若需求是『文案』，請輸出：\n"
        "1)『貼文骨架』：開頭鉤子→問題→解法→行動\n"
        "2)『平台變體』：IG Reels/抖音/小紅書（各 1 版簡短）\n"
        "3)『CTA 選項』：行動型/社交型各 2 條\n"
    )

    system = (
        "你是專業短影音顧問，精通腳本拆解、畫面語言與社群文案。"
        "必須語氣專業、可執行、條列清晰。"
        "先簡短確認需求，再給出具體方案。"
    )

    prompt = []
    prompt.append(f"[系統]\n{system}")
    prompt.append("[內建知識]\n" + kb_txt)
    prompt.append("[產出規範]\n" + script_spec + "\n" + copy_spec)
    prompt.append("[對話]")
    for m in req.messages:
        tag = "使用者" if m.role == "user" else ("助理" if m.role == "assistant" else "系統")
        prompt.append(f"{tag}：{m.content}")
    if req.mode == "script":
        prompt.append("請優先走『腳本』規範；若資訊不足，先以提問澄清後仍要給暫定方案。")
    elif req.mode == "copy":
        prompt.append("請優先走『文案』規範；若資訊不足，先以提問澄清後仍要給暫定方案。")
    else:
        prompt.append("請根據語意自動判斷更適合腳本或文案，並說明原因。")

    text = _lm_generate_text("\n\n".join(prompt))

    # 存助理回覆
    _save_messages(sid, [ChatMessage(role="assistant", content=text)])

    return ChatResp(
        assistant_message=text,
        metadata={"session_id": sid, "picked_knowledge": picked, "mode": req.mode},
    )

# ========= 舊接口：分段生成（保留且增強：帶知識庫提示） =========
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

def _gemini_generate_with_kb(req: GenerateReq) -> List[Segment]:
    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    picked = _simple_retrieve(req.user_input, "script")
    kb_txt = _knowledge_block(picked)

    system_prompt = (
        "你是短影音腳本助手。只輸出 JSON 陣列，每個元素含："
        "type(片頭|場景|片尾)、camera、dialog、visual。"
        "不要加註解或額外說明。"
    )
    prev = [s.model_dump() for s in req.previous_segments]
    user = req.user_input or "請生成一段 30 秒短影音腳本的下一個分段。"

    prompt = (
        f"{system_prompt}\n\n"
        f"[內建知識]\n{kb_txt}\n\n"
        f"[主題]\n{user}\n"
        f"[已接受段落 previous_segments]\n{json.dumps(prev, ensure_ascii=False)}\n\n"
        f"僅回傳 JSON 陣列，例如："
        f'[{{"type":"片頭","camera":"...","dialog":"...","visual":"..."}}]'
    )

    res = model.generate_content(prompt)
    text = (res.text or "").strip()
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j != -1 and j > i:
        text = text[i:j+1]
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
                segments = _gemini_generate_with_kb(req)
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
        raise exc
    except Exception:
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})
