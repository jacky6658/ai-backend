# app.py
import os
import json
import glob
import sqlite3
from typing import List, Optional, Any, Dict
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response, StreamingResponse

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/three_agents_system.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
KNOWLEDGE_TXT_PATH = os.getenv("KNOWLEDGE_TXT_PATH", "/data/kb.txt")
GLOBAL_KB_TEXT = ""

# ========= App 與 CORS =========
app = FastAPI(title="Three AI Agents System with Long-term Memory")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ========= 引導式問答狀態（記憶體暫存） =========
QA_SESSIONS: Dict[str, Dict[str, Any]] = {}  # key: session_id
QA_QUESTIONS = [
    {"key":"structure","q":"【Q1】請選擇腳本結構（A 三段式 / B 問題解決 / C Before-After / D 教學 / E 敘事 / F 爆點連發）"},
    {"key":"duration","q":"【Q2】影片時長（30 或 60 秒）"},
    {"key":"topic","q":"【Q3】請輸入主題或產品名稱"},
    {"key":"goal","q":"【Q4】主要目標（吸流量 / 教育 / 轉單 / 品牌）"},
    {"key":"audience","q":"【Q5】目標受眾（年齡/性別/特質/痛點）"},
    {"key":"hook","q":"【Q6】開場鉤子類型（問句/反差/同理/數字）＋想放的關鍵詞"},
    {"key":"cta","q":"【Q7】CTA（關注/收藏 / 留言/私訊 / 購買連結）"}
]

def qa_reset(session_id: str):
    QA_SESSIONS[session_id] = {"step": 0, "answers": {}}

def qa_next_question(session_id: str) -> Optional[str]:
    st = QA_SESSIONS.get(session_id)
    if not st: return None
    step = st["step"]
    if step < len(QA_QUESTIONS):
        return QA_QUESTIONS[step]["q"]
    return None

def qa_record_answer(session_id: str, user_text: str):
    st = QA_SESSIONS.get(session_id)
    if not st: return
    step = st["step"]
    if step < len(QA_QUESTIONS):
        key = QA_QUESTIONS[step]["key"]
        st["answers"][key] = user_text
        st["step"] = step + 1

def compose_brief_from_answers(ans: Dict[str,str]) -> str:
    labels = {
        "structure":"結構","duration":"時長","topic":"主題","goal":"目標","audience":"受眾",
        "hook":"鉤子","cta":"CTA"
    }
    lines = []
    for it in QA_QUESTIONS:
        k = it["key"]
        if k in ans:
            lines.append(f"{labels.get(k,k)}：{ans[k]}")
    return "；".join(lines)

# ========= 簡易 KB 檢索 =========
def load_kb_text() -> str:
    path = KNOWLEDGE_TXT_PATH
    try:
        if os.path.exists(path):
            with open(path,"r",encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def retrieve_context(query: str, max_chars: int = 1200) -> str:
    text = GLOBAL_KB_TEXT or ""
    if not text: 
        return ""
    import re
    toks = [t for t in re.split(r'[\s，。；、,.:?!\-\/\[\]()]+', (query or "")) if len(t)>=1]
    toks = list(dict.fromkeys(toks))
    lines = text.splitlines()
    scored = []
    for i, line in enumerate(lines):
        score = sum(1 for t in toks if t and t in line)
        if score>0:
            scored.append((score, i, line))
    scored.sort(key=lambda x:(-x[0], x[1]))
    selected=[]
    total=0
    for _, _, ln in scored:
        if not ln.strip(): 
            continue
        take = ln.strip()
        if total + len(take) + 1 > max_chars:
            break
        selected.append(take)
        total += len(take) + 1
    if not selected:
        return text[:max_chars]
    return "\n".join(selected)

# ========= DB =========
def _ensure_db_dir(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

def get_conn() -> sqlite3.Connection:
    _ensure_db_dir(DB_PATH)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    _ensure_db_dir(DB_PATH)
    conn = get_conn()
    cur = conn.cursor()
    
    # 原有表格
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_input TEXT,
            mode TEXT,
            messages_json TEXT,
            previous_segments_json TEXT,
            response_json TEXT
        )
    """)
    
    # 新增：三智能體系統表格
    # 1. 用戶基本資訊表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            email TEXT,
            name TEXT,
            platform_preferences TEXT,
            language_preference TEXT DEFAULT 'zh-TW',
            timezone TEXT DEFAULT 'Asia/Taipei',
            status TEXT DEFAULT 'active'
        )
    """)
    
    # 2. 用戶定位檔案表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            business_type TEXT,
            target_audience TEXT,
            brand_voice TEXT,
            content_goals TEXT,
            primary_platform TEXT,
            secondary_platforms TEXT,
            posting_frequency TEXT,
            preferred_topics TEXT,
            content_styles TEXT,
            video_duration_preference TEXT,
            competitors TEXT,
            unique_value_proposition TEXT,
            current_followers INTEGER DEFAULT 0,
            engagement_rate REAL DEFAULT 0.0,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            UNIQUE(user_id)
        )
    """)
    
    # 3. 會話記錄表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_type TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active',
            context_summary TEXT,
            key_insights TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # 4. 對話記錄表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    
    # 5. 智能體記憶表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            agent_type TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            content TEXT NOT NULL,
            importance_score INTEGER DEFAULT 5,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_accessed DATETIME DEFAULT CURRENT_TIMESTAMP,
            access_count INTEGER DEFAULT 1,
            tags TEXT,
            related_memories TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # 6. 選題建議表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topic_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            suggested_date DATE NOT NULL,
            topics TEXT NOT NULL,
            reasoning TEXT,
            user_feedback TEXT,
            used_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            UNIQUE(user_id, suggested_date)
        )
    """)
    
    # 建立索引
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_agent ON sessions(user_id, agent_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_agent ON agent_memories(user_id, agent_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON agent_memories(importance_score DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_suggestions_user_date ON topic_suggestions(user_id, suggested_date)")
    
    conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    try:
        init_db()
        global GLOBAL_KB_TEXT
        GLOBAL_KB_TEXT = load_kb_text()
        print(f"[BOOT] KB loaded from {KNOWLEDGE_TXT_PATH} len={len(GLOBAL_KB_TEXT)}")
        print(f"[BOOT] DB ready at {DB_PATH}")
    except Exception as e:
        print("[BOOT] DB init failed:", e)

@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/favicon.ico")
def favicon(): return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><body>
      <h3>三智能體長期記憶系統</h3>
      <h4>原有功能：</h4>
      <ul>
        <li>POST <code>/chat_generate</code>（腳本/文案二合一）</li>
        <li>POST <code>/generate_script</code>（舊流程保留）</li>
        <li>POST <code>/export/xlsx</code> 匯出 Excel</li>
        <li>POST <code>/chat_qa</code> 引導式問答</li>
      </ul>
      
      <h4>新增三智能體功能：</h4>
      <ul>
        <li><strong>定位智能體</strong></li>
        <li>POST <code>/agent/positioning/analyze</code> - 分析用戶定位</li>
        <li>PUT <code>/agent/positioning/profile</code> - 更新定位檔案</li>
        <li><strong>選題智能體</strong></li>
        <li>POST <code>/agent/topics/suggest</code> - 獲取選題建議</li>
        <li>GET <code>/agent/topics/history</code> - 選題歷史</li>
        <li><strong>腳本文案智能體</strong></li>
        <li>POST <code>/agent/content/generate</code> - 生成腳本/文案</li>
        <li><strong>記憶系統</strong></li>
        <li>GET <code>/memory/user/{user_id}</code> - 獲取用戶記憶</li>
        <li>POST <code>/memory/add</code> - 添加記憶</li>
      </ul>
    </body></html>
    """

# ========= 內建知識庫 =========
BUILTIN_KB_SCRIPT = """
【短影音腳本原則（濃縮）】
1) Hook(0-5s) → Value → CTA。60s 版可拆 5~6 段，節奏清楚。
2) 每段輸出：type/start_sec/end_sec/camera/dialog/visual/cta。
3) Hook 用痛點/反差/數據鉤子 + 快節奏 B-roll；Value 拆重點；CTA 動詞+利益+下一步。
4) 語氣口語、短句、有節奏，避免空話。
"""

BUILTIN_KB_COPY = """
【社群文案原則（濃縮）】
1) 結構：吸睛開頭 → 主體賣點/故事 → CTA → Hashtags。
2) 風格：貼近受眾、短句、可搭 emoji、結尾有動作。
3) Hashtags：主關鍵字 1-3、延伸 5-8。
4) 欄位：main_copy / alternates / hashtags / cta / image_ideas（平台化圖片建議）。
"""

def load_extra_kb(max_chars=2500) -> str:
    chunks, total = [], 0
    if GLOBAL_KB_TEXT:
        seg = GLOBAL_KB_TEXT[:max_chars]
        chunks.append(f"\n[KB:global]\n{seg}")
        total += len(seg)
    else:
        paths = glob.glob("/data/kb*.txt") + glob.glob("/data/*.kb.txt") + glob.glob("/data/knowledge*.txt")
        for p in paths:
            try:
                t = open(p, "r", encoding="utf-8").read().strip()
                if not t: continue
                take = (max_chars - total)
                seg = t[:take]
                if seg:
                    chunks.append(f"\n[KB:{os.path.basename(p)}]\n{seg}")
                    total += len(seg)
                if total >= max_chars: break
            except Exception:
                pass
    return "\n".join(chunks)

EXTRA_KB = load_extra_kb()

# ========= 提示字 & 工具 =========
SHORT_HINT_SCRIPT = "內容有點太短了 🙏 請提供：行業/平台/時長(秒)/目標/主題（例如：『電商｜Reels｜60秒｜購買｜夏季新品開箱』），我就能生成完整腳本。"
SHORT_HINT_COPY   = "內容有點太短了 🙏 請提供：平台/受眾/語氣/主題/CTA（例如：『IG｜男生視角｜活力回歸｜CTA：點連結』），我就能生成完整貼文。"

def _ensure_json_block(text: str) -> str:
    if not text: raise ValueError("empty model output")
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3: t = parts[1]
    i = min([x for x in (t.find("{"), t.find("[")) if x >= 0], default=-1)
    if i < 0: return t
    j = max(t.rfind("}"), t.rfind("]"))
    if j > i: return t[i:j+1]
    return t

def detect_mode(messages: List[Dict[str, str]], explicit: Optional[str]) -> str:
    """優先使用 explicit；否則用關鍵字判斷。"""
    if explicit in ("script", "copy"):
        return explicit
    last = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last = (m.get("content") or "").lower()
            break
    copy_keys = [
        "文案","貼文","copy","hashtag","hashtags",
        "ig","facebook","fb","linkedin","小紅書","x（twitter）","x/twitter","抖音文案"
    ]
    if any(k in last for k in copy_keys):
        return "copy"
    return "script"

def parse_segments(json_text: str) -> List[Dict[str, Any]]:
    data = json.loads(json_text)
    if isinstance(data, dict) and "segments" in data: data = data["segments"]
    if not isinstance(data, list): raise ValueError("segments must be a list")
    segs = []
    for it in data:
        segs.append({
            "type": it.get("type") or it.get("label") or "場景",
            "start_sec": it.get("start_sec", None),
            "end_sec": it.get("end_sec", None),
            "camera": it.get("camera", ""),
            "dialog": it.get("dialog", ""),
            "visual": it.get("visual", ""),
            "cta": it.get("cta", "")
        })
    return segs

def parse_copy(json_text: str) -> Dict[str, Any]:
    data = json.loads(json_text)
    if isinstance(data, list): data = data[0] if data else {}
    return {
        "main_copy":   data.get("main_copy", ""),
        "alternates":  data.get("alternates", []) or data.get("openers", []),
        "hashtags":    data.get("hashtags", []),
        "cta":         data.get("cta", ""),
        "image_ideas": data.get("image_ideas", [])
    }

# === NEW: 模板/時長/模式說明 ===
TEMPLATE_GUIDE = {
    "A": "三段式：Hook → Value → CTA。重點清楚、節奏明快，適合廣泛情境。",
    "B": "問題解決：痛點 → 解法 → 證據/示例 → CTA。適合教育與導購。",
    "C": "Before-After：改變前後對比，強調差異與收益 → CTA。適合案例/見證。",
    "D": "教學：步驟化教學（1-2-3）+ 注意事項 → CTA。適合技巧分享。",
    "E": "敘事：小故事鋪陳 → 轉折亮點 → CTA。適合品牌情緒/人物敘事。",
    "F": "爆點連發：連續強 Hook/金句/反差點，最後收斂 → CTA。適合抓注意力。"
}

def _duration_plan(duration: Optional[int]) -> Dict[str, Any]:
    """
    回傳分段建議與 fewshot JSON。30s 走 3 段；60s 走 6 段（每段~10s）。
    """
    if int(duration or 0) == 60:
        fewshot = """
{"segments":[
  {"type":"hook","start_sec":0,"end_sec":10,"camera":"CU","dialog":"...","visual":"...","cta":""},
  {"type":"value1","start_sec":10,"end_sec":20,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"value2","start_sec":20,"end_sec":30,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"value3","start_sec":30,"end_sec":40,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"value4","start_sec":40,"end_sec":50,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"cta","start_sec":50,"end_sec":60,"camera":"WS","dialog":"...","visual":"...","cta":"..."}
]}
"""
        return {"fewshot": fewshot, "note": "請以 60 秒約 6 段輸出，段與段間節奏分明。"}
    # default 30s
    fewshot = """
{"segments":[
  {"type":"hook","start_sec":0,"end_sec":5,"camera":"CU","dialog":"...","visual":"...","cta":""},
  {"type":"value","start_sec":5,"end_sec":25,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"cta","start_sec":25,"end_sec":30,"camera":"WS","dialog":"...","visual":"...","cta":"..."}
]}
"""
    return {"fewshot": fewshot, "note": "請以 30 秒 3 段輸出，Hook 要強、CTA 明確。"}

def build_script_prompt(
    user_input: str,
    previous_segments: List[Dict[str, Any]],
    template_type: Optional[str] = None,
    duration: Optional[int] = None,
    dialogue_mode: Optional[str] = None,
    knowledge_hint: Optional[str] = None,
) -> str:
    plan = _duration_plan(duration)
    fewshot = plan["fewshot"]
    duration_note = plan["note"]
    tmpl = (template_type or "").strip().upper()
    tmpl_text = TEMPLATE_GUIDE.get(tmpl, "未指定模板時由你判斷最合適的結構。")

    kb = (BUILTIN_KB_SCRIPT + "\n" + (EXTRA_KB or "")).strip()
    # 動態 KB 擷取：合併使用者輸入與可選提示
    q = user_input
    if knowledge_hint:
        q = f"{knowledge_hint}\n{user_input}"
    try:
        kb_ctx_dynamic = retrieve_context(q)
    except Exception:
        kb_ctx_dynamic = ""

    prev = json.dumps(previous_segments or [], ensure_ascii=False)

    mode_line = ""
    if (dialogue_mode or "").lower() == "free":
        mode_line = "語氣更自由、可主動提出精煉建議與反問以完善腳本；"
    elif (dialogue_mode or "").lower() == "guide":
        mode_line = "語氣偏引導，逐步釐清要素後直接給出完整分段；"

    return f"""
你是短影音腳本顧問。{mode_line}請根據「使用者輸入」與「已接受段落」延續或重寫，輸出 JSON（禁止額外說明文字）。

【選擇的模板】{tmpl or "（未指定）"}：{tmpl_text}
【時長要求】{int(duration) if duration else "（未指定，預設 30）"} 秒。{duration_note}

{kb}

【KB輔助摘錄】（若空白代表無）
{kb_ctx_dynamic[:1000]}

使用者輸入：
{user_input}

已接受段落：
{prev}

只回傳 JSON：
{fewshot}
"""

def build_copy_prompt(user_input: str, topic: Optional[str]) -> str:
    topic_line = f"\n【主題】{topic}" if topic else ""
    fewshot = """
{
  "main_copy":"主貼文（含換行與 emoji）",
  "alternates":["備選開頭A","備選開頭B","備選開頭C"],
  "hashtags":["#關鍵字1","#關鍵字2","#延伸3","#延伸4"],
  "cta":"行動呼籲一句話",
  "image_ideas":["配圖/照片/示意圖建議1","建議2","建議3"]
}
"""
    kb = (BUILTIN_KB_COPY + "\n" + (EXTRA_KB or "")).strip()
    return f"""
你是社群文案顧問。請依「使用者輸入」與可選的主題輸出**JSON**，包含主貼文、備選開頭、Hashtags、CTA，並加入 image_ideas（平台導向的圖片/拍法/視覺建議）。語氣可口語並適度使用 emoji。

{kb}

使用者輸入：
{user_input}{topic_line}

只回傳 JSON（單一物件，不要 markdown fence）：
{fewshot}
"""

# ========= Gemini =========
def use_gemini() -> bool: return bool(GEMINI_API_KEY)

def gemini_generate_text(prompt: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(prompt)
    return (res.text or "").strip()

# ========= Fallback =========
def fallback_segments(user_input: str, prev_len: int, duration: Optional[int]=None) -> List[Dict[str, Any]]:
    d = int(duration or 30)
    if d >= 60:
        # 粗略 60s 六段
        labels = ["hook","value1","value2","value3","value4","cta"]
        segs=[]
        start=0
        for i,l in enumerate(labels):
            end = 10*(i+1)
            if i==len(labels)-1: end = 60
            cam = "CU" if i==0 else ("WS" if i==len(labels)-1 else "MS")
            segs.append({
                "type": l, "start_sec": start, "end_sec": end, "camera": cam,
                "dialog": f"（模擬）{user_input[:36]}…",
                "visual": "（模擬）快切 B-roll / 大字卡",
                "cta": "點連結領取 🔗" if l=="cta" else ""
            })
            start = end
        return segs
    # 預設 30s 三段
    step = prev_len
    return [{
        "type": "hook" if step == 0 else ("cta" if step >= 2 else "value"),
        "start_sec": 0 if step == 0 else 5 if step == 1 else 25,
        "end_sec":   5 if step == 0 else 25 if step == 1 else 30,
        "camera": "CU" if step == 0 else "MS" if step == 1 else "WS",
        "dialog": f"（模擬）{user_input[:36]}…",
        "visual": "（模擬）快切 B-roll / 大字卡",
        "cta": "點連結領取 🔗" if step >= 2 else ""
    }]

def fallback_copy(user_input: str, topic: Optional[str]) -> Dict[str, Any]:
    t = f"（主題：{topic}）" if topic else ""
    return {
        "main_copy":  f"（模擬）IG 貼文：{user_input} {t}\n精神回歸、效率回升！⚡️\n今天就行動吧！",
        "alternates": ["🔥 今天就開始","💡 其實只要這樣做","👉 你也可以"],
        "hashtags":   ["#行銷","#AI","#文案","#社群經營"],
        "cta":        "立即點連結 🔗",
        "image_ideas":["產品近拍 + 生活情境","品牌色背景大字卡","步驟流程示意圖"]
    }

# ========= 三智能體系統核心功能 =========

# 用戶管理
def create_or_get_user(user_id: str, email: str = None, name: str = None) -> Dict:
    """創建或獲取用戶"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    
    user = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    
    if not user:
        conn.execute(
            "INSERT INTO users (user_id, email, name) VALUES (?, ?, ?)",
            (user_id, email, name)
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    
    conn.close()
    return dict(user) if user else None

def get_user_profile(user_id: str) -> Optional[Dict]:
    """獲取用戶定位檔案"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    profile = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(profile) if profile else None

def update_user_profile(user_id: str, profile_data: Dict) -> bool:
    """更新用戶定位檔案"""
    conn = get_conn()
    
    existing = conn.execute(
        "SELECT id FROM user_profiles WHERE user_id = ?", (user_id,)
    ).fetchone()
    
    if existing:
        update_fields = []
        values = []
        for key, value in profile_data.items():
            if key != 'user_id' and value is not None:
                update_fields.append(f"{key} = ?")
                values.append(json.dumps(value) if isinstance(value, (list, dict)) else str(value))
        
        if update_fields:
            values.append(user_id)
            sql = f"UPDATE user_profiles SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
            conn.execute(sql, values)
    else:
        profile_data['user_id'] = user_id
        fields = list(profile_data.keys())
        placeholders = ['?' for _ in fields]
        values = [json.dumps(v) if isinstance(v, (list, dict)) else str(v) for v in profile_data.values()]
        
        sql = f"INSERT INTO user_profiles ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
        conn.execute(sql, values)
    
    conn.commit()
    conn.close()
    return True

# 會話管理
def create_session(user_id: str, agent_type: str) -> str:
    """創建新會話"""
    session_id = f"{user_id}_{agent_type}_{int(datetime.now().timestamp())}"
    conn = get_conn()
    
    conn.execute(
        "INSERT INTO sessions (session_id, user_id, agent_type) VALUES (?, ?, ?)",
        (session_id, user_id, agent_type)
    )
    conn.commit()
    conn.close()
    
    return session_id

def add_message(session_id: str, role: str, content: str, metadata: Dict = None):
    """添加對話記錄"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, metadata) VALUES (?, ?, ?, ?)",
        (session_id, role, content, json.dumps(metadata) if metadata else None)
    )
    conn.commit()
    conn.close()

# 記憶系統
def add_memory(user_id: str, agent_type: str, memory_type: str, content: str, 
               importance_score: int = 5, tags: List[str] = None) -> int:
    """添加記憶"""
    conn = get_conn()
    
    cursor = conn.execute(
        """INSERT INTO agent_memories 
           (user_id, agent_type, memory_type, content, importance_score, tags) 
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, agent_type, memory_type, content, importance_score, 
         json.dumps(tags) if tags else None)
    )
    memory_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return memory_id

def get_user_memories(user_id: str, agent_type: str = None, memory_type: str = None, 
                     limit: int = 20) -> List[Dict]:
    """獲取用戶記憶"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    
    conditions = ["user_id = ?"]
    params = [user_id]
    
    if agent_type:
        conditions.append("agent_type = ?")
        params.append(agent_type)
    
    if memory_type:
        conditions.append("memory_type = ?")
        params.append(memory_type)
    
    params.append(limit)
    
    sql = f"""SELECT * FROM agent_memories 
              WHERE {' AND '.join(conditions)} 
              ORDER BY importance_score DESC, last_accessed DESC 
              LIMIT ?"""
    
    memories = conn.execute(sql, params).fetchall()
    conn.close()
    
    return [dict(memory) for memory in memories]

# 定位智能體
def positioning_agent_analyze(user_input: str, user_profile: Dict = None, memories: List[Dict] = None) -> str:
    """定位智能體分析"""
    context = "你是專業的品牌定位顧問，幫助用戶釐清帳號方向和定位。\n\n"
    
    if user_profile:
        context += f"用戶現有檔案：{json.dumps(user_profile, ensure_ascii=False)}\n\n"
    
    if memories:
        context += f"相關記憶：\n"
        for memory in memories[:5]:
            context += f"- {memory['content']}\n"
        context += "\n"
    
    context += f"用戶輸入：{user_input}\n\n"
    context += """請分析並提供：
1. 業務類型判斷
2. 目標受眾分析  
3. 品牌語氣建議
4. 內容策略方向
5. 平台選擇建議
6. 競爭優勢識別

請以結構化的方式回應，並提出具體可行的建議。"""
    
    return context

# 選題智能體
def topic_selection_agent_generate(user_profile: Dict, memories: List[Dict] = None) -> str:
    """選題智能體生成建議"""
    context = f"你是專業的內容選題顧問，為用戶提供每日靈感建議。\n\n"
    
    if user_profile:
        context += f"用戶檔案：\n"
        context += f"- 業務類型：{user_profile.get('business_type', '未設定')}\n"
        context += f"- 目標受眾：{user_profile.get('target_audience', '未設定')}\n"
        context += f"- 品牌語氣：{user_profile.get('brand_voice', '未設定')}\n"
        context += f"- 主要平台：{user_profile.get('primary_platform', '未設定')}\n\n"
    
    if memories:
        context += f"相關洞察：\n"
        for memory in memories[:3]:
            context += f"- {memory['content']}\n"
        context += "\n"
    
    context += """請提供5個具體的內容選題建議，每個選題包含：
1. 標題/主題
2. 為什麼適合這個用戶
3. 預期效果
4. 創作建議
5. 相關熱門標籤

考慮當前熱點、季節性、用戶興趣和平台特性。"""
    
    return context

def extract_key_insights(text: str, agent_type: str) -> List[str]:
    """從AI回應中提取關鍵洞察"""
    insights = []
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if len(line) > 20 and any(keyword in line for keyword in ['建議', '應該', '可以', '重點', '關鍵']):
            insights.append(line)
    
    return insights[:3]

# ========= 引導式問答 API =========
@app.post("/chat_qa")
async def chat_qa(req: Request):
    data = await req.json()
    session_id = (data.get("session_id") or "qa").strip() or "qa"
    user_msg = (data.get("message") or "").strip()

    # 初次進入：建立 session 並送歡迎 + Q1
    if session_id not in QA_SESSIONS:
        qa_reset(session_id)
        q = qa_next_question(session_id)
        return {
            "session_id": session_id,
            "assistant_message": "嗨👋 讓我們一步步生成你的短影音腳本！\n" + (q or ""),
            "segments": [],
            "done": False,
            "error": None
        }

    # 正常流程：記錄上一題的回答
    qa_record_answer(session_id, user_msg)
    next_q = qa_next_question(session_id)
    if next_q:
        return {
            "session_id": session_id,
            "assistant_message": next_q,
            "segments": [],
            "done": False,
            "error": None
        }

    # 問答完成 → 組合描述 + 取 KB context → 走原有 build_script_prompt
    ans = QA_SESSIONS.get(session_id, {}).get("answers", {})
    brief = compose_brief_from_answers(ans)
    kb_ctx = retrieve_context(brief) or ""
    # 將 QA 選到的 structure/duration 帶入
    template_type = (ans.get("structure") or "").strip()[:1].upper() or None
    try:
        duration = int((ans.get("duration") or "").strip())
    except Exception:
        duration = 30

    user_input = f"{brief}\n\n【KB輔助摘錄】\n{kb_ctx}"

    previous_segments = []
    prompt = build_script_prompt(
        user_input,
        previous_segments,
        template_type=template_type,
        duration=duration,
        dialogue_mode="guide",
    )
    try:
        if use_gemini():
            out = gemini_generate_text(prompt)
            j = _ensure_json_block(out)
            segments = parse_segments(j)
        else:
            segments = fallback_segments(user_input, 0, duration=duration)
    except Exception as e:
        print("[chat_qa] error:", e)
        segments = []

    # 清除 session
    QA_SESSIONS.pop(session_id, None)

    return {
        "session_id": session_id,
        "assistant_message": "我已根據你的回答生成第一版腳本（可再調整）。",
        "segments": segments,
        "done": True,
        "error": None
    }

# ========= /chat_generate =========
@app.post("/chat_generate")
async def chat_generate(req: Request):
    """
    body: {
      user_id?: str,
      session_id?: str,
      messages: [{role, content}],
      previous_segments?: [segment...],
      remember?: bool,
      mode?: "script" | "copy",          # ← 保留既有：腳本/文案
      topic?: str,                        # ← 文案主題（可選）
      dialogue_mode?: "guide" | "free",   # ← 新增：引導/自由 對話風格（可選）
      template_type?: "A"|"B"|"C"|"D"|"E"|"F",  # ← 新增
      duration?: 30|60,                   # ← 新增：腳本時長
      knowledge_hint?: str                # ← 新增：檢索提示詞（可選）
    }
    """
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_json")

    user_id = (data.get("user_id") or "").strip() or "web"
    messages = data.get("messages") or []
    previous_segments = data.get("previous_segments") or []
    topic = (data.get("topic") or "").strip() or None

    explicit_mode = (data.get("mode") or "").strip().lower() or None
    mode = detect_mode(messages, explicit=explicit_mode)

    # NEW: 讀取新參數（後端若沒收到也不影響舊行為）
    dialogue_mode = (data.get("dialogue_mode") or "").strip().lower() or None
    template_type = (data.get("template_type") or "").strip().upper() or None
    try:
        duration = int(data.get("duration")) if data.get("duration") is not None else None
    except Exception:
        duration = None
    knowledge_hint = (data.get("knowledge_hint") or "").strip() or None

    user_input = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_input = (m.get("content") or "").strip()
            break

    hint = SHORT_HINT_COPY if mode == "copy" else SHORT_HINT_SCRIPT
    if len(user_input) < 6:
        return {
            "session_id": data.get("session_id") or "s",
            "assistant_message": hint,
            "segments": [],
            "copy": None,
            "error": None
        }

    try:
        if mode == "copy":
            prompt = build_copy_prompt(user_input, topic)
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                copy = parse_copy(j)
            else:
                copy = fallback_copy(user_input, topic)

            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整貼文（可再加要求，我會幫你改得更貼近風格）。",
                "segments": [],
                "copy": copy,
                "error": None
            }

        else:  # script
            prompt = build_script_prompt(
                user_input,
                previous_segments,
                template_type=template_type,
                duration=duration,
                dialogue_mode=dialogue_mode,
                knowledge_hint=knowledge_hint,
            )
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                segments = parse_segments(j)
            else:
                segments = fallback_segments(user_input, len(previous_segments or []), duration=duration)

            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整腳本（可再加要求，我會幫你改得更貼近風格）。",
                "segments": segments,
                "copy": None,
                "error": None
            }

        # DB 紀錄（保留原行為）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (
                    user_input, mode,
                    json.dumps(messages, ensure_ascii=False),
                    json.dumps(previous_segments, ensure_ascii=False),
                    json.dumps(resp, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return resp

    except Exception as e:
        print("[chat_generate] error:", e)
        return JSONResponse(status_code=500, content={
            "session_id": data.get("session_id") or "s",
            "assistant_message": "伺服器忙碌，稍後再試",
            "segments": [],
            "copy": None,
            "error": "internal_server_error"
        })

# ========= 舊流程：/generate_script =========
@app.post("/generate_script")
async def generate_script(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_json")

    user_input = (data.get("user_input") or "").strip()
    previous_segments = data.get("previous_segments") or []

    # 向下相容：舊端點若想支援 60s/模板，也可帶入這兩個欄位（可選）
    template_type = (data.get("template_type") or "").strip().upper() or None
    try:
        duration = int(data.get("duration")) if data.get("duration") is not None else None
    except Exception:
        duration = None

    if len(user_input) < 6:
        return {"segments": [], "error": SHORT_HINT_SCRIPT}

    try:
        prompt = build_script_prompt(
            user_input,
            previous_segments,
            template_type=template_type,
            duration=duration
        )
        if use_gemini():
            out = gemini_generate_text(prompt)
            j = _ensure_json_block(out)
            segments = parse_segments(j)
        else:
            segments = fallback_segments(user_input, len(previous_segments or []), duration=duration)
        return {"segments": segments, "error": None}
    except Exception as e:
        print("[generate_script] error:", e)
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 匯出：Word 暫停 / Excel 保留 =========
@app.post("/export/docx")
async def export_docx_disabled():
    return JSONResponse(status_code=501, content={"error": "docx_export_disabled"})

def _ensure_xlsx():
    try:
        import openpyxl  # noqa
        return True
    except Exception:
        return False

@app.post("/export/xlsx")
async def export_xlsx(req: Request):
    if not _ensure_xlsx():
        return JSONResponse(status_code=501, content={"error": "xlsx_not_available"})
    import openpyxl
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    data = await req.json()
    segments = data.get("segments") or []
    copy = data.get("copy") or None

    wb = openpyxl.Workbook()
    ws1 = wb.active; ws1.title = "腳本分段"
    ws1.append(["#","type","start_sec","end_sec","camera","dialog","visual","cta"])
    for i, s in enumerate(segments, 1):
        ws1.append([i, s.get("type"), s.get("start_sec"), s.get("end_sec"),
                    s.get("camera"), s.get("dialog"), s.get("visual"), s.get("cta")])

    ws2 = wb.create_sheet("文案")
    ws2.append(["主貼文"]); ws2.append([copy.get("main_copy") if copy else ""])
    ws2.append([]); ws2.append(["備選開頭"])
    for a in (copy.get("alternates") if copy else []) or []: ws2.append([a])
    ws2.append([]); ws2.append(["Hashtags"])
    ws2.append([" ".join(copy.get("hashtags") if copy else [])])
    ws2.append([]); ws2.append(["CTA"])
    ws2.append([copy.get("cta") if copy else ""])
    ws2.append([]); ws2.append(["圖片建議"])
    for idea in (copy.get("image_ideas") if copy else []) or []: ws2.append([idea])

    for ws in (ws1, ws2):
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value else 0 for c in col) + 2
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(width, 80)

    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="export.xlsx"'}
    )

# ========= CSV 下載 & Google Sheet 連動 =========
import csv
import json
from fastapi.responses import FileResponse, Response
from io import StringIO

@app.get("/download/requests_export.csv")
def download_requests_csv():
    export_path = "/data/requests_export.csv"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM requests ORDER BY id DESC")
    rows = cur.fetchall()
    headers = [desc[0] for desc in cur.description]
    conn.close()

    with open(export_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return FileResponse(
        export_path,
        media_type="text/csv",
        filename="requests_export.csv",
    )


@app.get("/export/google-sheet")
def export_for_google_sheet(limit: int = 100):
    try:
        limit = int(limit)
    except Exception:
        limit = 100
    limit = max(1, min(limit, 2000))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, created_at, user_input, mode FROM requests ORDER BY id DESC LIMIT {limit}"
    )
    rows = cur.fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "user_input", "mode"])
    for row in rows:
        writer.writerow(row)

    return Response(content=output.getvalue(), media_type="text/csv")


@app.get("/export/google-sheet-flat")
def export_google_sheet_flat(limit: int = 200):
    try:
        limit = int(limit)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, created_at, user_input, mode, response_json
        FROM requests
        ORDER BY id DESC
        LIMIT {limit}
        """
    )
    rows = cur.fetchall()
    conn.close()

    out = StringIO()
    writer = csv.writer(out)

    headers = [
        "id", "created_at", "mode", "user_input",
        "assistant_message",
        "copy_main_copy",
        "copy_cta",
        "copy_hashtags",
        "copy_alternates_joined",
        "segments_count",
        "seg1_type", "seg1_start_sec", "seg1_end_sec", "seg1_dialog", "seg1_visual", "seg1_cta",
        "seg2_type", "seg2_start_sec", "seg2_end_sec", "seg2_dialog", "seg2_visual", "seg2_cta",
        "seg3_type", "seg3_start_sec", "seg3_end_sec", "seg3_dialog", "seg3_visual", "seg3_cta",
    ]
    writer.writerow(headers)

    for _id, created_at, user_input, mode, resp_json in rows:
        assistant_message = ""
        copy_main = ""
        copy_cta = ""
        copy_hashtags = ""
        copy_alternates_joined = ""
        segments_count = 0

        def empty_seg():
            return ["", "", "", "", "", ""]
        seg1 = empty_seg()
        seg2 = empty_seg()
        seg3 = empty_seg()

        try:
            data = json.loads(resp_json or "{}")
            assistant_message = (data.get("assistant_message") or "")[:500]

            c = data.get("copy") or {}
            if isinstance(c, dict):
                copy_main = c.get("main_copy") or ""
                copy_cta = c.get("cta") or ""
                tags = c.get("hashtags") or []
                if isinstance(tags, list):
                    copy_hashtags = " ".join(map(str, tags))
                alts = c.get("alternates") or c.get("openers") or []
                if isinstance(alts, list):
                    copy_alternates_joined = " | ".join(map(str, alts))

            segs = data.get("segments") or []
            if isinstance(segs, list):
                segments_count = len(segs)

                def to_seg(s):
                    return [
                        s.get("type", ""),
                        s.get("start_sec", ""),
                        s.get("end_sec", ""),
                        s.get("dialog", ""),
                        s.get("visual", ""),
                        s.get("cta", ""),
                    ]

                if len(segs) >= 1: seg1 = to_seg(segs[0])
                if len(segs) >= 2: seg2 = to_seg(segs[1])
                if len(segs) >= 3: seg3 = to_seg(segs[2])

        except Exception as e:
            assistant_message = f"[JSON parse error] {str(e)}"

        writer.writerow([
            _id, created_at, mode, user_input,
            assistant_message,
            copy_main,
            copy_cta,
            copy_hashtags,
            copy_alternates_joined,
            segments_count,
            *seg1, *seg2, *seg3,
        ])

    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "inline; filename=export_flat.csv"},
    )

# ========= Google Sheet 扁平化（v2） =========
import csv
import json
from io import StringIO
from fastapi.responses import Response

@app.get("/export/google-sheet-flat-v2")
def export_google_sheet_flat_v2(limit: int = 200):
    """
    扁平化 CSV（含 copy 與前 3 個 segments），禁用快取。
    在 Google Sheets 使用：
      =IMPORTDATA("https://aijobvideobackend.zeabur.app/export/google-sheet-flat-v2?limit=500")
    """
    try:
        limit = int(limit)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, created_at, user_input, mode, response_json
        FROM requests
        ORDER BY id DESC
        LIMIT {limit}
        """
    )
    rows = cur.fetchall()
    conn.close()

    out = StringIO()
    writer = csv.writer(out)

    headers = [
        "id", "created_at", "mode", "user_input",
        "assistant_message",
        "copy_main_copy", "copy_cta", "copy_hashtags", "copy_alternates_joined",
        "segments_count",
        "seg1_type", "seg1_start_sec", "seg1_end_sec", "seg1_dialog", "seg1_visual", "seg1_cta",
        "seg2_type", "seg2_start_sec", "seg2_end_sec", "seg2_dialog", "seg2_visual", "seg2_cta",
        "seg3_type", "seg3_start_sec", "seg3_end_sec", "seg3_dialog", "seg3_visual", "seg3_cta",
    ]
    writer.writerow(headers)

    def empty_seg():
        return ["", "", "", "", "", ""]

    for _id, created_at, user_input, mode, resp_json in rows:
        assistant_message = ""
        copy_main = ""
        copy_cta = ""
        copy_hashtags = ""
        copy_alternates = ""
        segments_count = 0
        seg1 = empty_seg()
        seg2 = empty_seg()
        seg3 = empty_seg()

        try:
            data = json.loads(resp_json or "{}")
            assistant_message = (data.get("assistant_message") or "")[:500]

            c = data.get("copy") or {}
            if isinstance(c, dict):
                copy_main = c.get("main_copy") or ""
                copy_cta = c.get("cta") or ""
                tags = c.get("hashtags") or []
                if isinstance(tags, list):
                    copy_hashtags = " ".join(map(str, tags))
                alts = c.get("alternates") or c.get("openers") or []
                if isinstance(alts, list):
                    copy_alternates = " | ".join(map(str, alts))

            segs = data.get("segments") or []
            if isinstance(segs, list):
                segments_count = len(segs)

                def to_seg(s):
                    return [
                        s.get("type", ""),
                        s.get("start_sec", ""),
                        s.get("end_sec", ""),
                        s.get("dialog", ""),
                        s.get("visual", ""),
                        s.get("cta", ""),
                    ]

                if len(segs) >= 1: seg1 = to_seg(segs[0])
                if len(segs) >= 2: seg2 = to_seg(segs[1])
                if len(segs) >= 3: seg3 = to_seg(segs[2])

        except Exception as e:
            assistant_message = f"[JSON parse error] {str(e)}"

        writer.writerow([
            _id, created_at, mode, user_input,
            assistant_message,
            copy_main, copy_cta, copy_hashtags, copy_alternates,
            segments_count,
            *seg1, *seg2, *seg3,
        ])

    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": "inline; filename=export_flat_v2.csv",
            "Cache-Control": "no-store, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

# ========= 三智能體 API 端點 =========

# 定位智能體
@app.post("/agent/positioning/analyze")
async def positioning_analyze(req: Request):
    """定位智能體分析用戶定位"""
    try:
        data = await req.json()
        user_id = data.get("user_id")
        user_input = data.get("user_input", "")
        
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        # 確保用戶存在
        create_or_get_user(user_id)
        
        # 獲取用戶檔案和相關記憶
        user_profile = get_user_profile(user_id)
        memories = get_user_memories(user_id, agent_type="positioning", limit=10)
        
        # 創建會話
        session_id = create_session(user_id, "positioning")
        add_message(session_id, "user", user_input)
        
        # 生成分析
        analysis_context = positioning_agent_analyze(user_input, user_profile, memories)
        
        # 調用 AI 生成回應
        if use_gemini():
            ai_response = gemini_generate_text(analysis_context)
        else:
            ai_response = "AI服務暫時不可用，請稍後再試。"
        
        add_message(session_id, "assistant", ai_response)
        
        # 提取關鍵洞察並保存為記憶
        if ai_response and len(ai_response) > 50:
            key_insights = extract_key_insights(ai_response, "positioning")
            for insight in key_insights:
                add_memory(user_id, "positioning", "insight", insight, importance_score=7)
        
        return {
            "session_id": session_id,
            "response": ai_response,
            "user_profile": user_profile,
            "error": None
        }
        
    except Exception as e:
        print(f"[Positioning Agent Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

@app.put("/agent/positioning/profile")
async def update_positioning_profile(req: Request):
    """更新用戶定位檔案"""
    try:
        data = await req.json()
        user_id = data.get("user_id")
        profile_data = data.get("profile_data", {})
        
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        # 確保用戶存在
        create_or_get_user(user_id)
        
        # 更新檔案
        success = update_user_profile(user_id, profile_data)
        
        if success:
            # 保存檔案更新為記憶
            add_memory(user_id, "positioning", "profile_update", 
                      f"用戶檔案已更新：{json.dumps(profile_data, ensure_ascii=False)}", 
                      importance_score=8)
        
        return {
            "success": success,
            "message": "檔案更新成功" if success else "檔案更新失敗",
            "error": None
        }
        
    except Exception as e:
        print(f"[Profile Update Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

# 選題智能體
@app.post("/agent/topics/suggest")
async def topic_suggest(req: Request):
    """獲取選題建議"""
    try:
        data = await req.json()
        user_id = data.get("user_id")
        target_date = data.get("target_date")  # YYYY-MM-DD 格式
        
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        # 確保用戶存在
        create_or_get_user(user_id)
        
        # 解析日期
        if target_date:
            try:
                from datetime import datetime
                target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
            except ValueError:
                from datetime import date
                target_date = date.today()
        else:
            from datetime import date
            target_date = date.today()
        
        # 獲取用戶檔案和相關記憶
        user_profile = get_user_profile(user_id)
        memories = get_user_memories(user_id, agent_type="topic_selection", limit=5)
        
        # 生成選題建議
        suggestion_context = topic_selection_agent_generate(user_profile, memories)
        
        # 調用 AI 生成選題
        if use_gemini():
            ai_response = gemini_generate_text(suggestion_context)
        else:
            ai_response = "AI服務暫時不可用，請稍後再試。"
        
        # 保存選題建議
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO topic_suggestions 
               (user_id, suggested_date, topics, reasoning) 
               VALUES (?, ?, ?, ?)""",
            (user_id, target_date.isoformat(), json.dumps({"suggestions": ai_response}), ai_response)
        )
        conn.commit()
        conn.close()
        
        return {
            "user_id": user_id,
            "suggested_date": target_date.isoformat(),
            "suggestions": ai_response,
            "reasoning": ai_response,
            "error": None
        }
        
    except Exception as e:
        print(f"[Topic Selection Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

@app.get("/agent/topics/history")
async def topic_history(user_id: str, limit: int = 10):
    """獲取選題歷史"""
    try:
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        suggestions = conn.execute(
            "SELECT * FROM topic_suggestions WHERE user_id = ? ORDER BY suggested_date DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        conn.close()
        
        return {
            "user_id": user_id,
            "suggestions": [dict(s) for s in suggestions],
            "error": None
        }
        
    except Exception as e:
        print(f"[Topic History Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

# 腳本文案智能體（增強版）
@app.post("/agent/content/generate")
async def content_generate(req: Request):
    """生成腳本或文案（增強版，整合記憶系統）"""
    try:
        data = await req.json()
        user_id = data.get("user_id")
        user_input = data.get("user_input", "")
        mode = data.get("mode", "script")  # "script" 或 "copy"
        template_type = data.get("template_type")
        duration = data.get("duration")
        
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        # 確保用戶存在
        create_or_get_user(user_id)
        
        # 獲取用戶檔案和相關記憶
        user_profile = get_user_profile(user_id)
        memories = get_user_memories(user_id, agent_type="script_copy", limit=10)
        
        # 創建會話
        session_id = create_session(user_id, "script_copy")
        add_message(session_id, "user", user_input)
        
        # 構建增強的提示詞（整合用戶檔案和記憶）
        enhanced_input = user_input
        
        if user_profile:
            profile_context = f"""
【用戶定位檔案】
- 業務類型：{user_profile.get('business_type', '未設定')}
- 目標受眾：{user_profile.get('target_audience', '未設定')}
- 品牌語氣：{user_profile.get('brand_voice', '未設定')}
- 主要平台：{user_profile.get('primary_platform', '未設定')}
"""
            enhanced_input = f"{profile_context}\n\n用戶需求：{user_input}"
        
        if memories:
            memory_context = "\n【相關記憶】\n"
            for memory in memories[:3]:
                memory_context += f"- {memory['content']}\n"
            enhanced_input = f"{enhanced_input}\n\n{memory_context}"
        
        # 使用現有的 chat_generate 邏輯，但傳入增強後的輸入
        enhanced_data = {
            "user_id": user_id,
            "session_id": session_id,
            "messages": [{"role": "user", "content": enhanced_input}],
            "mode": mode,
            "template_type": template_type,
            "duration": duration
        }
        
        # 調用現有的生成邏輯
        result = await chat_generate_internal(enhanced_data)
        
        # 添加記憶
        if result.get("assistant_message"):
            add_memory(user_id, "script_copy", "generation", 
                      f"生成{mode}：{user_input[:100]}...", 
                      importance_score=6)
        
        return result
        
    except Exception as e:
        print(f"[Content Generation Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

# 記憶系統 API
@app.get("/memory/user/{user_id}")
async def get_user_memory(user_id: str, agent_type: str = None, memory_type: str = None, limit: int = 20):
    """獲取用戶記憶"""
    try:
        memories = get_user_memories(user_id, agent_type, memory_type, limit)
        
        return {
            "user_id": user_id,
            "memories": memories,
            "count": len(memories),
            "error": None
        }
        
    except Exception as e:
        print(f"[Memory Retrieval Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

@app.post("/memory/add")
async def add_memory_endpoint(req: Request):
    """添加記憶"""
    try:
        data = await req.json()
        user_id = data.get("user_id")
        agent_type = data.get("agent_type")
        memory_type = data.get("memory_type")
        content = data.get("content")
        importance_score = data.get("importance_score", 5)
        tags = data.get("tags", [])
        
        if not all([user_id, agent_type, memory_type, content]):
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        memory_id = add_memory(user_id, agent_type, memory_type, content, importance_score, tags)
        
        return {
            "memory_id": memory_id,
            "message": "記憶添加成功",
            "error": None
        }
        
    except Exception as e:
        print(f"[Memory Addition Error] {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(e)}
        )

# 內部函數：chat_generate 的內部邏輯（供 content_generate 調用）
async def chat_generate_internal(data: dict):
    """chat_generate 的內部邏輯，供其他函數調用"""
    user_id = (data.get("user_id") or "").strip() or "web"
    messages = data.get("messages") or []
    previous_segments = data.get("previous_segments") or []
    topic = (data.get("topic") or "").strip() or None

    explicit_mode = (data.get("mode") or "").strip().lower() or None
    mode = detect_mode(messages, explicit=explicit_mode)

    dialogue_mode = (data.get("dialogue_mode") or "").strip().lower() or None
    template_type = (data.get("template_type") or "").strip().upper() or None
    try:
        duration = int(data.get("duration")) if data.get("duration") is not None else None
    except Exception:
        duration = None
    knowledge_hint = (data.get("knowledge_hint") or "").strip() or None

    user_input = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_input = (m.get("content") or "").strip()
            break

    hint = SHORT_HINT_COPY if mode == "copy" else SHORT_HINT_SCRIPT
    if len(user_input) < 6:
        return {
            "session_id": data.get("session_id") or "s",
            "assistant_message": hint,
            "segments": [],
            "copy": None,
            "error": None
        }

    try:
        if mode == "copy":
            prompt = build_copy_prompt(user_input, topic)
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                copy = parse_copy(j)
            else:
                copy = fallback_copy(user_input, topic)

            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整貼文（可再加要求，我會幫你改得更貼近風格）。",
                "segments": [],
                "copy": copy,
                "error": None
            }

        else:  # script
            prompt = build_script_prompt(
                user_input,
                previous_segments,
                template_type=template_type,
                duration=duration,
                dialogue_mode=dialogue_mode,
                knowledge_hint=knowledge_hint,
            )
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                segments = parse_segments(j)
            else:
                segments = fallback_segments(user_input, len(previous_segments or []), duration=duration)

            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整腳本（可再加要求，我會幫你改得更貼近風格）。",
                "segments": segments,
                "copy": None,
                "error": None
            }

        return resp

    except Exception as e:
        print("[chat_generate_internal] error:", e)
        return {
            "session_id": data.get("session_id") or "s",
            "assistant_message": "伺服器忙碌，稍後再試",
            "segments": [],
            "copy": None,
            "error": "internal_server_error"
        }
