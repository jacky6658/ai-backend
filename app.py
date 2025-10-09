# app.py
import os
import json
import glob
import sqlite3
from typing import List, Optional, Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response, StreamingResponse

# ========= 環境變數 =========
DB_PATH = os.getenv("DB_PATH", "/data/script_generation.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
KNOWLEDGE_TXT_PATH = os.getenv("KNOWLEDGE_TXT_PATH", "/data/kb.txt")
GLOBAL_KB_TEXT = ""

# ========= App 與 CORS =========
app = FastAPI(title="AI Script + Copy Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

# ========= 偏好與引導狀態 =========
USER_PREFS: Dict[str, Dict[str, Any]] = {}     # key: user_id -> {"template_type": "A"~"F", "duration": 30/60}
QA_SESSIONS: Dict[str, Dict[str, Any]] = {}    # key: session_id
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
    if not st:
        return None
    step = st["step"]
    if step < len(QA_QUESTIONS):
        return QA_QUESTIONS[step]["q"]
    return None

def qa_record_answer(session_id: str, user_text: str):
    st = QA_SESSIONS.get(session_id)
    if not st:
        return
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
    lines=[]
    for it in QA_QUESTIONS:
        k=it["key"]
        if k in ans:
            lines.append(f"{labels.get(k,k)}：{ans[k]}")
    return "；".join(lines)

# ========= 簡易 KB =========
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
    scored=[]
    for i,line in enumerate(lines):
        score = sum(1 for t in toks if t and t in line)
        if score>0:
            scored.append((score,i,line))
    scored.sort(key=lambda x:(-x[0], x[1]))
    selected=[]; total=0
    for _,_,ln in scored:
        if not ln.strip(): continue
        take=ln.strip()
        if total+len(take)+1>max_chars: break
        selected.append(take); total+=len(take)+1
    if not selected:
        return text[:max_chars]
    return "\n".join(selected)

# ========= DB =========
def _ensure_db_dir(path:str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

def get_conn() -> sqlite3.Connection:
    _ensure_db_dir(DB_PATH)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    _ensure_db_dir(DB_PATH)
    conn=get_conn(); cur=conn.cursor()
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
    conn.commit(); conn.close()

@app.on_event("startup")
def on_startup():
    try:
        init_db()
        global GLOBAL_KB_TEXT
        GLOBAL_KB_TEXT = load_kb_text()
        print(f"[BOOT] KB loaded len={len(GLOBAL_KB_TEXT)}")
        print(f"[BOOT] DB ready at {DB_PATH}")
    except Exception as e:
        print("[BOOT] init failed:", e)

@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><body>
      <h3>AI Backend OK</h3>
      <p>POST <code>/chat_generate</code>（腳本/文案二合一）</p>
      <p>POST <code>/generate_script</code>（舊流程保留）</p>
      <p>POST <code>/export/xlsx</code> 匯出 Excel；🧠 引導式問答：<code>/chat_qa</code></p>
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
4) 欄位：main_copy / alternates / hashtags / cta / image_ideas（圖片/視覺建議）。
"""

def load_extra_kb(max_chars=2500) -> str:
    chunks=[]; total=0
    if GLOBAL_KB_TEXT:
        seg=GLOBAL_KB_TEXT[:max_chars]
        chunks.append(f"\n[KB:global]\n{seg}"); total+=len(seg)
    else:
        paths = glob.glob("/data/kb*.txt")+glob.glob("/data/*.kb.txt")+glob.glob("/data/knowledge*.txt")
        for p in paths:
            try:
                t=open(p,"r",encoding="utf-8").read().strip()
                if not t: continue
                take = (max_chars-total)
                seg=t[:take]
                if seg:
                    chunks.append(f"\n[KB:{os.path.basename(p)}]\n{seg}")
                    total+=len(seg)
                if total>=max_chars: break
            except Exception:
                pass
    return "\n".join(chunks)

EXTRA_KB = load_extra_kb()

# ========= 工具 =========
SHORT_HINT_SCRIPT = "內容有點太短了 🙏 請提供：行業/平台/時長(秒)/目標/主題（例如：『電商｜Reels｜60秒｜購買｜夏季新品開箱』），我就能生成完整腳本。"
SHORT_HINT_COPY   = "內容有點太短了 🙏 請提供：平台/受眾/語氣/主題/CTA（例如：『IG｜男生視角｜活力回歸｜CTA：點連結』），我就能生成完整貼文。"

def _ensure_json_block(text: str) -> str:
    if not text: raise ValueError("empty model output")
    t = text.strip()
    if t.startswith("```"):
        parts=t.split("```")
        if len(parts)>=3: t=parts[1]
    i=min([x for x in (t.find("{"), t.find("[")) if x>=0], default=-1)
    if i<0: return t
    j=max(t.rfind("}"), t.rfind("]"))
    if j>i: return t[i:j+1]
    return t

def detect_mode(messages: List[Dict[str, str]], explicit: Optional[str]) -> str:
    if explicit in ("script","copy"):
        return explicit
    last=""
    for m in reversed(messages or []):
        if m.get("role")=="user":
            last=(m.get("content") or "").lower(); break
    copy_keys=["文案","貼文","copy","hashtag","hashtags","ig","facebook","fb","linkedin","小紅書","x（twitter）","x/twitter","抖音文案"]
    if any(k in last for k in copy_keys):
        return "copy"
    return "script"

def parse_segments(json_text: str) -> List[Dict[str, Any]]:
    data=json.loads(json_text)
    if isinstance(data, dict) and "segments" in data: data=data["segments"]
    if not isinstance(data, list): raise ValueError("segments must be a list")
    segs=[]
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
    data=json.loads(json_text)
    if isinstance(data, list): data=data[0] if data else {}
    return {
        "main_copy":   data.get("main_copy",""),
        "alternates":  data.get("alternates",[]) or data.get("openers",[]),
        "hashtags":    data.get("hashtags",[]),
        "cta":         data.get("cta",""),
        "image_ideas": data.get("image_ideas",[])
    }

TEMPLATE_GUIDE = {
    "A": "三段式：Hook → Value → CTA。重點清楚、節奏明快，適合廣泛情境。",
    "B": "問題解決：痛點 → 解法 → 證據/示例 → CTA。適合教育與導購。",
    "C": "Before-After：改變前後對比，強調差異與收益 → CTA。適合案例/見證。",
    "D": "教學：步驟化教學（1-2-3）+ 注意事項 → CTA。適合技巧分享。",
    "E": "敘事：小故事鋪陳 → 轉折亮點 → CTA。適合品牌情緒/人物敘事。",
    "F": "爆點連發：連續強 Hook/金句/反差點，最後收斂 → CTA。適合抓注意力。"
}

def _duration_plan(duration: Optional[int]) -> Dict[str, Any]:
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
    fewshot = """
{"segments":[
  {"type":"hook","start_sec":0,"end_sec":5,"camera":"CU","dialog":"...","visual":"...","cta":""},
  {"type":"value","start_sec":5,"end_sec":25,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"cta","start_sec":25,"end_sec":30,"camera":"WS","dialog":"...","visual":"...","cta":"..."}
]}
"""
    return {"fewshot": fewshot, "note": "請以 30 秒 3 段輸出，Hook 要強、CTA 明確。"}

def build_script_prompt(user_input: str, previous_segments: List[Dict[str, Any]],
                        template_type: Optional[str]=None, duration: Optional[int]=None,
                        dialogue_mode: Optional[str]=None, knowledge_hint: Optional[str]=None) -> str:
    plan=_duration_plan(duration)
    fewshot=plan["fewshot"]; duration_note=plan["note"]
    tmpl=(template_type or "").strip().upper()
    tmpl_text=TEMPLATE_GUIDE.get(tmpl, "未指定模板時由你判斷最合適的結構。")
    kb=(BUILTIN_KB_SCRIPT+"\n"+(EXTRA_KB or "")).strip()
    q = f"{knowledge_hint}\n{user_input}" if knowledge_hint else user_input
    try:
        kb_ctx_dynamic = retrieve_context(q)
    except Exception:
        kb_ctx_dynamic = ""
    prev=json.dumps(previous_segments or [], ensure_ascii=False)
    mode_line=""
    if (dialogue_mode or "").lower()=="free":
        mode_line="語氣更自由、可主動提出精煉建議與反問以完善腳本；"
    elif (dialogue_mode or "").lower()=="guide":
        mode_line="語氣偏引導，逐步釐清要素後直接給出完整分段；"
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
    kb=(BUILTIN_KB_COPY+"\n"+(EXTRA_KB or "")).strip()
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
    d=int(duration or 30)
    if d>=60:
        labels=["hook","value1","value2","value3","value4","cta"]
        segs=[]; start=0
        for i,l in enumerate(labels):
            end=10*(i+1)
            if i==len(labels)-1: end=60
            cam="CU" if i==0 else ("WS" if i==len(labels)-1 else "MS")
            segs.append({
                "type":l,"start_sec":start,"end_sec":end,"camera":cam,
                "dialog":f"（模擬）{user_input[:36]}…",
                "visual":"（模擬）快切 B-roll / 大字卡",
                "cta":"點連結領取 🔗" if l=="cta" else ""
            })
            start=end
        return segs
    return [{
        "type": "hook" if prev_len == 0 else ("cta" if prev_len >= 2 else "value"),
        "start_sec": 0 if prev_len == 0 else 5 if prev_len == 1 else 25,
        "end_sec":   5 if prev_len == 0 else 25 if prev_len == 1 else 30,
        "camera": "CU" if prev_len == 0 else "MS" if prev_len == 1 else "WS",
        "dialog": f"（模擬）{user_input[:36]}…",
        "visual": "（模擬）快切 B-roll / 大字卡",
        "cta": "點連結領取 🔗" if prev_len >= 2 else ""
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

# ========= 偏好設定 API：只記，**不回訊息** =========
@app.post("/set_prefs")
async def set_prefs(req: Request):
    data = await req.json()
    user_id = (data.get("user_id") or "").strip() or "web"
    template_type = (data.get("template_type") or "").strip().upper() or None
    duration = data.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except Exception:
        duration = None
    USER_PREFS[user_id] = {"template_type": template_type, "duration": duration}
    return {"ok": True, "saved": USER_PREFS[user_id]}

# ========= 引導式問答 API =========
@app.post("/chat_qa")
async def chat_qa(req: Request):
    data = await req.json()
    session_id = (data.get("session_id") or "qa").strip() or "qa"
    user_msg = (data.get("message") or "").strip()
    user_id = (data.get("user_id") or "").strip() or "web"

    # 初次進入：建立 session
    if session_id not in QA_SESSIONS:
        qa_reset(session_id)
        # 如果有偏好（已用 /set_prefs 設過），**預填** structure/duration 並跳過 Q1/Q2
        prefs = USER_PREFS.get(user_id) or {}
        if prefs.get("template_type"): 
            QA_SESSIONS[session_id]["answers"]["structure"] = prefs["template_type"]
            QA_SESSIONS[session_id]["step"] = max(QA_SESSIONS[session_id]["step"], 1)
        if prefs.get("duration"):
            QA_SESSIONS[session_id]["answers"]["duration"] = str(prefs["duration"])
            QA_SESSIONS[session_id]["step"] = max(QA_SESSIONS[session_id]["step"], 2)
        # 只回「歡迎」——不問 Q1/Q2
        return {
            "session_id": session_id,
            "assistant_message": "👋 歡迎使用腳本模式！— 新手沒想法 → 請用【引導模式】，先選結構＋時長，我會一步步問答並給建議。\n— 已有想法 → 切到【自由模式】直接聊，你說想做什麼，我來補齊腳本與畫面建議。",
            "segments": [],
            "done": False,
            "error": None
        }

    # 若不是第一次：進入問答流程
    if user_msg:
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

    # 問答完成 → 生成腳本
    ans = QA_SESSIONS.get(session_id, {}).get("answers", {})
    brief = compose_brief_from_answers(ans)
    kb_ctx = retrieve_context(brief) or ""
    template_type = (ans.get("structure") or "").strip()[:1].upper() or None
    try:
        duration = int((ans.get("duration") or "").strip())
    except Exception:
        duration = 30

    user_input = f"{brief}\n\n【KB輔助摘錄】\n{kb_ctx}"
    previous_segments=[]
    prompt = build_script_prompt(user_input, previous_segments, template_type=template_type, duration=duration, dialogue_mode="guide")
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

    QA_SESSIONS.pop(session_id, None)
    return {
        "session_id": session_id,
        "assistant_message": "我已根據你的回答生成第一版腳本（可再調整）。",
        "segments": segments,
        "done": True,
        "error": None
    }

# ========= /chat_generate：加入「自由模式」意圖判斷 =========
def detect_intent_free(text: str) -> str:
    """
    回傳：
      - 'ask_structure'：詢問 A~F 結構/怎麼選
      - 'ask_meta'     ：一般分析/建議/流程/如何做
      - 'generate'     ：明確要腳本/文案
    """
    t = (text or "").strip().lower()
    if any(k in t for k in ["哪一種結構","哪種結構","a~f","a-f","a至f","結構推薦","用哪個結構","哪個結構","before-after","問題解決","爆點連發","三段式","教學","敘事"]):
        return "ask_structure"
    if any(k in t for k in ["幫我寫","生成腳本","出腳本","做60秒腳本","60 秒腳本","30 秒腳本","請產生腳本","write a script","script for"]):
        return "generate"
    # 問句/為什麼/怎麼做 → 視為分析
    if "？" in t or "?" in t or any(k in t for k in ["怎麼","如何","為什麼","可不可以","適不適合"]):
        return "ask_meta"
    # 預設：若包含「腳本/分段」等字眼也算生成
    if any(k in t for k in ["腳本","分段","hook","cta"]):
        return "generate"
    return "ask_meta"

def answer_structure_explained(user_input: str, prefs: Dict[str,Any]) -> str:
    guide = "\n".join([f"{k}. {v}" for k,v in TEMPLATE_GUIDE.items()])
    hint = f"\n\n我的建議：若主題是「{user_input[:40]}」"
    # 簡單啟發：含「教學/步驟」→ D、含「案例/前後對比」→ C、含「痛點/解法」→ B，否則 A。
    low = user_input.lower()
    if any(k in low for k in ["教學","步驟","教程","how to","教你","做法"]): sug="D 教學"
    elif any(k in low for k in ["前後","before","after","改變前後","對比"]): sug="C Before-After"
    elif any(k in low for k in ["問題","痛點","解決","煩惱","卡關","困擾"]): sug="B 問題解決"
    else: sug="A 三段式"
    if prefs.get("duration")==60:
        hint += f"、你偏好 60 秒，可拆 5~6 段；結構建議：{sug}。"
    elif prefs.get("duration")==30:
        hint += f"、你偏好 30 秒，走 3 段精煉；結構建議：{sug}。"
    else:
        hint += f"；結構建議：{sug}。"
    return f"以下是 A~F 結構說明：\n{guide}{hint}\n\n需要我直接用這個結構產第一版腳本嗎？回覆「產生腳本」即可。"

@app.post("/chat_generate")
async def chat_generate(req: Request):
    """
    body: {
      user_id?: str, session_id?: str,
      messages: [{role, content}],
      previous_segments?: [segment...],
      remember?: bool,
      mode?: "script" | "copy",
      topic?: str,
      dialogue_mode?: "guide" | "free",
      template_type?: "A"|"B"|"C"|"D"|"E"|"F",
      duration?: 30|60,
      knowledge_hint?: str
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

    dialogue_mode = (data.get("dialogue_mode") or "").strip().lower() or None
    template_type = (data.get("template_type") or "").strip().upper() or None
    try:
        duration = int(data.get("duration")) if data.get("duration") is not None else None
    except Exception:
        duration = None
    knowledge_hint = (data.get("knowledge_hint") or "").strip() or None

    # 合併偏好（若前端先用 /set_prefs）
    prefs = USER_PREFS.get(user_id) or {}
    template_type = template_type or prefs.get("template_type")
    duration = duration or prefs.get("duration")

    user_input = ""
    for m in reversed(messages):
        if m.get("role")=="user":
            user_input=(m.get("content") or "").strip()
            break

    hint = SHORT_HINT_COPY if mode=="copy" else SHORT_HINT_SCRIPT
    if len(user_input) < 6:
        return {
            "session_id": data.get("session_id") or "s",
            "assistant_message": hint,
            "segments": [],
            "copy": None,
            "error": None
        }

    try:
        # ========= 文案 =========
        if mode == "copy":
            prompt = build_copy_prompt(user_input, topic)
            if use_gemini():
                out=gemini_generate_text(prompt)
                j=_ensure_json_block(out)
                copy=parse_copy(j)
            else:
                copy=fallback_copy(user_input, topic)
            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整貼文（可再加要求，我會幫你改得更貼近風格）。",
                "segments": [],
                "copy": copy,
                "error": None
            }

        # ========= 腳本 =========
        else:
            if dialogue_mode == "free":
                intent = detect_intent_free(user_input)
                if intent == "ask_structure":
                    msg = answer_structure_explained(user_input, {"duration": duration})
                    resp = {
                        "session_id": data.get("session_id") or "s",
                        "assistant_message": msg,
                        "segments": [],
                        "copy": None,
                        "error": None
                    }
                    # 記錄就好，不產腳本
                    conn=get_conn(); cur=conn.cursor()
                    cur.execute(
                        "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                        (user_input, "script-qa", json.dumps(messages, ensure_ascii=False),
                         json.dumps(previous_segments, ensure_ascii=False), json.dumps(resp, ensure_ascii=False))
                    )
                    conn.commit(); conn.close()
                    return resp
                elif intent == "ask_meta":
                    # 使用 KB 回答建議，不產腳本
                    kb_ctx = retrieve_context(user_input) or ""
                    msg = f"這題我先給分析與建議：\n\n{kb_ctx[:800] or '（已讀取你的知識庫，但沒有精準片段；建議補充更多前情/受眾/平台。）'}\n\n若要我直接產出腳本，回覆「產生腳本」或告訴我秒數/結構。"
                    resp = {
                        "session_id": data.get("session_id") or "s",
                        "assistant_message": msg,
                        "segments": [],
                        "copy": None,
                        "error": None
                    }
                    conn=get_conn(); cur=conn.cursor()
                    cur.execute(
                        "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                        (user_input, "script-qa", json.dumps(messages, ensure_ascii=False),
                         json.dumps(previous_segments, ensure_ascii=False), json.dumps(resp, ensure_ascii=False))
                    )
                    conn.commit(); conn.close()
                    return resp
                # intent == 'generate' → 落到下面生成

            prompt = build_script_prompt(
                user_input, previous_segments,
                template_type=template_type, duration=duration,
                dialogue_mode=dialogue_mode, knowledge_hint=knowledge_hint
            )
            if use_gemini():
                out=gemini_generate_text(prompt)
                j=_ensure_json_block(out)
                segments=parse_segments(j)
            else:
                segments=fallback_segments(user_input, len(previous_segments or []), duration=duration)

            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整腳本（可再加要求，我會幫你改得更貼近風格）。",
                "segments": segments,
                "copy": None,
                "error": None
            }

        # 紀錄
        try:
            conn=get_conn(); cur=conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (user_input, mode, json.dumps(messages, ensure_ascii=False),
                 json.dumps(previous_segments, ensure_ascii=False),
                 json.dumps(resp, ensure_ascii=False))
            )
            conn.commit(); conn.close()
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

# ========= 舊流程（保留） =========
@app.post("/generate_script")
async def generate_script(req: Request):
    data = await req.json()
    user_input = (data.get("user_input") or "").strip()
    previous_segments = data.get("previous_segments") or []
    template_type = (data.get("template_type") or "").strip().upper() or None
    try:
        duration = int(data.get("duration")) if data.get("duration") is not None else None
    except Exception:
        duration = None

    if len(user_input) < 6:
        return {"segments": [], "error": SHORT_HINT_SCRIPT}

    try:
        prompt = build_script_prompt(user_input, previous_segments, template_type=template_type, duration=duration)
        if use_gemini():
            out=gemini_generate_text(prompt)
            j=_ensure_json_block(out)
            segments=parse_segments(j)
        else:
            segments=fallback_segments(user_input, len(previous_segments or []), duration=duration)
        return {"segments": segments, "error": None}
    except Exception as e:
        print("[generate_script] error:", e)
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 匯出（保留） =========
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
