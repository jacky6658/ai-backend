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
# 兼容舊變數名
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

# ========= App 與 CORS =========
app = FastAPI(title="AI Script + Copy Backend")

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
            mode TEXT,
            messages_json TEXT,
            previous_segments_json TEXT,
            response_json TEXT
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
      <p>POST <code>/chat_generate</code> (script/copy, 聊天式) 或 <code>/generate_script</code> (舊流程)。</p>
      <p>POST <code>/export/docx</code>, <code>/export/xlsx</code> 可匯出檔案。</p>
      <p>文案模式新增欄位：<code>copy.image_ideas: string[]</code>（圖片/視覺建議）。</p>
    </body></html>
    """

# ========= 內建「知識庫」 + 可擴充檔案 =========
BUILTIN_KB_SCRIPT = """
【短影音腳本原則（濃縮）】
1) 分段結構：Hook(0-5s) → Value(中段 5-25s / 延長可到40s) → CTA(收尾)。
2) 每段輸出欄位：type(片頭/場景/片尾或 hook/value/cta)、start_sec、end_sec、camera、dialog(口播/字幕台詞)、visual(畫面感/運鏡/畫面元素)、cta。
3) Hook：痛點 / 反差 / 數據鉤子 / 一句 punch line；快節奏 B-roll 導入。
4) Value：拆重點（3個以內），每個重點配「畫面元素」；節奏：切點明確。
5) CTA：動詞+利益，具體下一步（點連結 / 領取 / 私訊）；畫面配大字卡+Logo。
6) 語氣：口語、節奏感、短句、可搭 emoji；避免空話。
"""

BUILTIN_KB_COPY = """
【社群文案原則（濃縮）】
1) 結構：吸睛開頭（2-3行）→ 主體敘事/賣點 → CTA（動詞 + 指令）→ Hashtags。
2) 風格：對受眾說人話、短句、可搭 emoji、結尾有呼喚動作。
3) Hashtags：主關鍵字 1-3、延伸 5-8，避免太廣泛或無關。
4) 產出欄位：main_copy（主貼文）、alternates（3-4個短開頭）、hashtags（陣列）、cta（短句）、image_ideas（圖像/素材建議，依平台差異給方向）。
"""

def load_extra_kb(max_chars=2500) -> str:
    """
    讀取 /data/kb*.txt 或 /data/*.txt（可自備）並裁切。找不到則回空字串。
    """
    paths = glob.glob("/data/kb*.txt") + glob.glob("/data/*.kb.txt") + glob.glob("/data/knowledge*.txt")
    buf = []
    total = 0
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if not t:
                    continue
                remain = max_chars - total
                seg = t[:remain]
                if seg:
                    buf.append(f"\n[KB:{os.path.basename(p)}]\n{seg}")
                    total += len(seg)
                if total >= max_chars:
                    break
        except Exception:
            continue
    return "\n".join(buf)

EXTRA_KB = load_extra_kb()

# ========= Prompt 組裝 =========
SHORT_HINT_SCRIPT = "內容有點太短了 🙏 請提供：行業/平台/時長(秒)/目標/主題（例如：『電商｜Reels｜30秒｜購買｜夏季新品開箱』），我就能生成完整腳本。"
SHORT_HINT_COPY   = "內容有點太短了 🙏 請提供：平台/受眾/語氣/主題/CTA（例如：『IG｜男生視角｜活力回歸｜CTA：點連結』），我就能生成完整貼文。"

def _ensure_json_block(text: str) -> str:
    """
    嘗試從 LLM 回應裡把第一個 JSON 區塊拉出來。
    """
    if not text:
        raise ValueError("empty model output")
    t = text.strip()
    if t.startswith("```"):
        fence = "```"
        parts = t.split(fence)
        if len(parts) >= 3:
            t = parts[1]
    i1 = t.find("{")
    i2 = t.find("[")
    i = min([x for x in [i1, i2] if x >= 0], default=-1)
    if i < 0:
        return t
    j1 = t.rfind("}")
    j2 = t.rfind("]")
    j = max(j1, j2)
    if j > i:
        return t[i:j+1]
    return t

def detect_mode(messages: List[Dict[str, str]], explicit: Optional[str] = None) -> str:
    """
    回傳 'script' 或 'copy'
    """
    if explicit in ("script", "copy"):
        return explicit
    last = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last = (m.get("content") or "").lower()
            break
    copy_keys = ["文案", "hashtag", "貼文", "copy", "ig", "facebook", "小紅書", "抖音文案"]
    if any(k.lower() in last for k in copy_keys):
        return "copy"
    return "script"

def parse_segments(json_text: str) -> List[Dict[str, Any]]:
    data = json.loads(json_text)
    if isinstance(data, dict) and "segments" in data:
        data = data["segments"]
    if not isinstance(data, list):
        raise ValueError("segments must be a list")
    segs = []
    for item in data:
        segs.append({
            "type": item.get("type") or item.get("label") or "場景",
            "start_sec": item.get("start_sec", None),
            "end_sec": item.get("end_sec", None),
            "camera": item.get("camera", ""),
            "dialog": item.get("dialog", ""),
            "visual": item.get("visual", ""),
            "cta": item.get("cta", "")
        })
    return segs

def parse_copy(json_text: str) -> Dict[str, Any]:
    data = json.loads(json_text)
    if isinstance(data, list):
        data = data[0] if data else {}
    return {
        "main_copy":   data.get("main_copy", ""),
        "alternates":  data.get("alternates", []) or data.get("openers", []),
        "hashtags":    data.get("hashtags", []),
        "cta":         data.get("cta", ""),
        "image_ideas": data.get("image_ideas", [])   # ← 新增：圖片/視覺建議
    }

def build_script_prompt(user_input: str, previous_segments: List[Dict[str, Any]]) -> str:
    fewshot = """
【輸出格式（JSON）】
{
  "segments":[
    {"type":"hook","start_sec":0,"end_sec":5,"camera":"CU","dialog":"...","visual":"...","cta":""},
    {"type":"value","start_sec":5,"end_sec":25,"camera":"MS","dialog":"...","visual":"...","cta":""},
    {"type":"cta","start_sec":25,"end_sec":30,"camera":"WS","dialog":"...","visual":"...","cta":"..."}
  ]
}
"""
    prev = json.dumps(previous_segments or [], ensure_ascii=False)
    kb = (BUILTIN_KB_SCRIPT + "\n" + (EXTRA_KB or "")).strip()
    return f"""
你是短影音腳本顧問。請根據「使用者輸入」與「已接受段落」延續/或重寫，輸出 JSON（不要其他說明文字）。

{kb}

使用者輸入：
{user_input}

已接受段落（previous_segments）：
{prev}

請直接回傳 JSON（單一物件，不要 markdown code fence），範例如下：
{fewshot}
"""

def build_copy_prompt(user_input: str) -> str:
    fewshot = """
【輸出格式（JSON）】
{
  "main_copy": "主貼文（含換行與 emoji）",
  "alternates": ["備選開頭A","備選開頭B","備選開頭C"],
  "hashtags": ["#關鍵字1","#關鍵字2","#延伸3","#延伸4"],
  "cta": "行動呼籲一句話",
  "image_ideas": ["配圖/照片/示意圖建議1","建議2","建議3"]
}
"""
    kb = (BUILTIN_KB_COPY + "\n" + (EXTRA_KB or "")).strip()
    return f"""
你是社群文案顧問。請根據「使用者輸入」輸出 JSON（不要其他說明文字），涵蓋主貼文、備選開頭、Hashtags、CTA，
並加入 <image_ideas>（建議可用圖片/圖像風格/拍法/示意圖，並因應 IG/FB/小紅書/LinkedIn 差異給方向）。

{kb}

使用者輸入：
{user_input}

請直接回傳 JSON（單一物件，不要 markdown code fence），範例如下：
{fewshot}
"""

# ========= Gemini 產文 =========
def use_gemini() -> bool:
    return bool(GEMINI_API_KEY)

def gemini_generate_text(prompt: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    res = model.generate_content(prompt)
    return (res.text or "").strip()

# ========= Fallback（無 API Key 時） =========
def fallback_segments(user_input: str, prev_len: int) -> List[Dict[str, Any]]:
    step = prev_len
    return [
        {
            "type": "hook" if step == 0 else ("cta" if step >= 2 else "value"),
            "start_sec": 0 if step == 0 else 5 if step == 1 else 25,
            "end_sec":   5 if step == 0 else 25 if step == 1 else 30,
            "camera": "CU" if step == 0 else "MS" if step == 1 else "WS",
            "dialog": f"（模擬）{user_input[:36]}…",
            "visual": "（模擬）快切 B-roll / 大字卡",
            "cta": "點連結領取" if step >= 2 else ""
        }
    ]

def fallback_copy(user_input: str) -> Dict[str, Any]:
    return {
        "main_copy":  f"（模擬）IG 貼文：{user_input}\n關鍵賣點 + 故事 + CTA。",
        "alternates": ["開頭A：抓痛點","開頭B：丟數據","開頭C：小故事"],
        "hashtags":   ["#短影音","#行銷","#AI"],
        "cta":        "立即點連結",
        "image_ideas":["產品近拍 + 生活化情境","輕素材：手持使用前後對比","品牌色背景的俐落字卡"]
    }

# ========= /chat_generate（新流程，腳本/文案二合一）=========
@app.post("/chat_generate")
async def chat_generate(req: Request):
    """
    body: {
      user_id?: str,
      session_id?: str,
      messages: [{role, content}],
      previous_segments?: [segment...],
      remember?: bool,
      mode?: "script" | "copy"
    }
    """
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_json")

    user_id = (data.get("user_id") or "").strip() or "web"
    messages = data.get("messages") or []
    previous_segments = data.get("previous_segments") or []
    explicit_mode = (data.get("mode") or "").strip().lower() or None
    mode = detect_mode(messages, explicit=explicit_mode)

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
            prompt = build_copy_prompt(user_input)
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                copy = parse_copy(j)
            else:
                copy = fallback_copy(user_input)
            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整貼文（可再加要求，我會幫你改得更貼近風格）。",
                "segments": [],
                "copy": copy,
                "error": None
            }
        else:
            prompt = build_script_prompt(user_input, previous_segments)
            if use_gemini():
                out = gemini_generate_text(prompt)
                j = _ensure_json_block(out)
                segments = parse_segments(j)
            else:
                segments = fallback_segments(user_input, len(previous_segments or []))
            resp = {
                "session_id": data.get("session_id") or "s",
                "assistant_message": "我先給你第一版完整腳本（可再加要求，我會幫你改得更貼近風格）。",
                "segments": segments,
                "copy": None,
                "error": None
            }

        # DB 紀錄（失敗不影響回應）
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (
                    user_input,
                    mode,
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
    except HTTPException as exc:
        raise exc
    except Exception as e:
        print("[chat_generate] error:", e)
        return JSONResponse(
            status_code=500,
            content={
                "session_id": data.get("session_id") or "s",
                "assistant_message": "伺服器忙碌，稍後再試",
                "segments": [],
                "copy": None,
                "error": "internal_server_error"
            }
        )

# ========= 舊流程：/generate_script（保留） =========
@app.post("/generate_script")
async def generate_script(req: Request):
    """
    body: { "user_input": str, "previous_segments": [] }
    """
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid_json")

    user_input = (data.get("user_input") or "").strip()
    previous_segments = data.get("previous_segments") or []

    if len(user_input) < 6:
        return {"segments": [], "error": SHORT_HINT_SCRIPT}

    try:
        prompt = build_script_prompt(user_input, previous_segments)
        if use_gemini():
            out = gemini_generate_text(prompt)
            j = _ensure_json_block(out)
            segments = parse_segments(j)
        else:
            segments = fallback_segments(user_input, len(previous_segments or []))

        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO requests (user_input, mode, messages_json, previous_segments_json, response_json) VALUES (?, ?, ?, ?, ?)",
                (
                    user_input,
                    "legacy_generate_script",
                    json.dumps([], ensure_ascii=False),
                    json.dumps(previous_segments, ensure_ascii=False),
                    json.dumps({"segments": segments, "error": None}, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print("[DB] insert failed:", e)

        return {"segments": segments, "error": None}
    except Exception as e:
        print("[generate_script] error:", e)
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 偏好 & 回饋（簡易）=========
@app.post("/update_prefs")
async def update_prefs(req: Request):
    try:
        _ = await req.json()
        return {"ok": True}
    except Exception:
        return {"ok": False}

@app.post("/feedback")
async def feedback(req: Request):
    try:
        data = await req.json()
        print("[feedback]", data)
        return {"ok": True}
    except Exception:
        return {"ok": False}

# ========= 匯出（DOCX / XLSX）=========
def _ensure_docx():
    try:
        import docx  # noqa
        return True
    except Exception:
        return False

def _ensure_xlsx():
    try:
        import openpyxl  # noqa
        return True
    except Exception:
        return False

@app.post("/export/docx")
async def export_docx(req: Request):
    """
    body: { messages_script?, messages_copy?, segments?, copy? }
    直接回傳 docx 檔案串流
    """
    if not _ensure_docx():
        return JSONResponse(status_code=501, content={"error": "docx_not_available"})
    from docx import Document
    from docx.shared import Pt

    data = await req.json()
    messages_script = data.get("messages_script") or []
    messages_copy = data.get("messages_copy") or []
    segments = data.get("segments") or []
    copy = data.get("copy") or None

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft JhengHei"
    style.font.size = Pt(11)

    doc.add_heading("短影音顧問 AI 專案匯出", level=1)

    # 對話（腳本）
    doc.add_heading("一、對話紀錄（腳本）", level=2)
    for m in messages_script:
        doc.add_paragraph(f"{m.get('role')}: {m.get('content') or ''}")

    # 對話（文案）
    doc.add_heading("二、對話紀錄（文案）", level=2)
    for m in messages_copy:
        doc.add_paragraph(f"{m.get('role')}: {m.get('content') or ''}")

    # 腳本分段
    doc.add_heading("三、腳本分段", level=2)
    if segments:
        for i, s in enumerate(segments, 1):
            doc.add_paragraph(f"#{i} {s.get('type')} ({s.get('start_sec')}s–{s.get('end_sec')}s) camera:{s.get('camera')}")
            if s.get("dialog"): doc.add_paragraph("台詞：" + s.get("dialog"))
            if s.get("visual"): doc.add_paragraph("畫面：" + s.get("visual"))
            if s.get("cta"):    doc.add_paragraph("CTA：" + s.get("cta"))
    else:
        doc.add_paragraph("（無片段）")

    # 文案
    doc.add_heading("四、文案模組", level=2)
    if copy:
        doc.add_paragraph("【主貼文】")
        doc.add_paragraph(copy.get("main_copy") or "")
        doc.add_paragraph("【備選開頭】")
        for i, a in enumerate(copy.get("alternates") or [], 1):
            doc.add_paragraph(f"{i}. {a}")
        doc.add_paragraph("【Hashtags】")
        doc.add_paragraph(" ".join(copy.get("hashtags") or []))
        doc.add_paragraph("【CTA】")
        doc.add_paragraph(copy.get("cta") or "")

        # 新增：圖片建議
        ideas = copy.get("image_ideas") or []
        if ideas:
            doc.add_paragraph("【圖片建議】")
            for i, idea in enumerate(ideas, 1):
                doc.add_paragraph(f"{i}. {idea}")
    else:
        doc.add_paragraph("（無文案）")

    from io import BytesIO
    bio = BytesIO()
    doc.save(bio)
    bio.seek(0)
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="export.docx"'}
    )

@app.post("/export/xlsx")
async def export_xlsx(req: Request):
    """
    body: { segments?, copy? }
    """
    if not _ensure_xlsx():
        return JSONResponse(status_code=501, content={"error": "xlsx_not_available"})
    import openpyxl
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    data = await req.json()
    segments = data.get("segments") or []
    copy = data.get("copy") or None

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "腳本分段"
    ws1.append(["#","type","start_sec","end_sec","camera","dialog","visual","cta"])
    for i, s in enumerate(segments, 1):
        ws1.append([i, s.get("type"), s.get("start_sec"), s.get("end_sec"), s.get("camera"), s.get("dialog"), s.get("visual"), s.get("cta")])

    ws2 = wb.create_sheet("文案")
    ws2.append(["主貼文"])
    ws2.append([copy.get("main_copy") if copy else ""])
    ws2.append([])
    ws2.append(["備選開頭"])
    for a in (copy.get("alternates") if copy else []) or []:
        ws2.append([a])
    ws2.append([])
    ws2.append(["Hashtags"])
    ws2.append([" ".join(copy.get("hashtags") if copy else [])])
    ws2.append([])
    ws2.append(["CTA"])
    ws2.append([copy.get("cta") if copy else ""])
    ws2.append([])
    ws2.append(["圖片建議"])
    for idea in (copy.get("image_ideas") if copy else []) or []:
        ws2.append([idea])

    for ws in (ws1, ws2):
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value else 0 for c in col) + 2
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(width, 80)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="export.xlsx"'}
    )
