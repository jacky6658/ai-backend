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

# ========= App 與 CORS =========
app = FastAPI(title="AI Script + Copy Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)

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
    conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    try:
        init_db()
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
      <h3>AI Backend OK</h3>
      <p>POST <code>/chat_generate</code>（腳本/文案二合一）</p>
      <p>POST <code>/generate_script</code>（舊流程保留）</p>
      <p>POST <code>/export/xlsx</code> 匯出 Excel；<code>/export/docx</code> 暫停（501）。</p>
      <p>文案模式：回傳物件含 <code>image_ideas</code>（圖片/視覺建議）。</p>
    </body></html>
    """

# ========= 內建知識庫 =========
BUILTIN_KB_SCRIPT = """
【短影音腳本原則（濃縮）】
1) Hook(0-5s) → Value(5-25s 可延伸) → CTA。
2) 每段輸出：type/start_sec/end_sec/camera/dialog/visual/cta。
3) Hook 用痛點/反差/數據鉤子 + 快節奏 B-roll；Value 拆 3 點以內；CTA 動詞+利益+下一步。
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
    paths = glob.glob("/data/kb*.txt") + glob.glob("/data/*.kb.txt") + glob.glob("/data/knowledge*.txt")
    chunks, total = [], 0
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
SHORT_HINT_SCRIPT = "內容有點太短了 🙏 請提供：行業/平台/時長(秒)/目標/主題（例如：『電商｜Reels｜30秒｜購買｜夏季新品開箱』），我就能生成完整腳本。"
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

def build_script_prompt(user_input: str, previous_segments: List[Dict[str, Any]]) -> str:
    fewshot = """
{"segments":[
  {"type":"hook","start_sec":0,"end_sec":5,"camera":"CU","dialog":"...","visual":"...","cta":""},
  {"type":"value","start_sec":5,"end_sec":25,"camera":"MS","dialog":"...","visual":"...","cta":""},
  {"type":"cta","start_sec":25,"end_sec":30,"camera":"WS","dialog":"...","visual":"...","cta":"..."}
]}
"""
    prev = json.dumps(previous_segments or [], ensure_ascii=False)
    kb = (BUILTIN_KB_SCRIPT + "\n" + (EXTRA_KB or "")).strip()
    return f"""
你是短影音腳本顧問。請根據「使用者輸入」與「已接受段落」延續或重寫，輸出 JSON（禁止額外說明文字）。

{kb}

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
def fallback_segments(user_input: str, prev_len: int) -> List[Dict[str, Any]]:
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
      mode?: "script" | "copy",    # ← 前端強制帶入避免誤判
      topic?: str                  # ← 文案主題（可選）
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

    user_input = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_input = (m.get("content") or "").strip()
            break

    # 針對 copy 與 script 分流短字提示
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

        # DB 紀錄
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
        return {"segments": segments, "error": None}
    except Exception as e:
        print("[generate_script] error:", e)
        return JSONResponse(status_code=500, content={"segments": [], "error": "internal_server_error"})

# ========= 匯出：Word 暫停 / Excel 保留 =========
@app.post("/export/docx")
async def export_docx_disabled():
    # 先停用：避免前端誤按導致錯誤；之後要開再實作
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
from fastapi.responses import FileResponse

@app.get("/download/requests_export.csv")
def download_requests_csv():
    """匯出資料庫 requests 表為 CSV 檔，方便手動下載或備份"""
    export_path = "/data/requests_export.csv"
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM requests ORDER BY id DESC")
    rows = cursor.fetchall()
    headers = [desc[0] for desc in cursor.description]
    conn.close()

    with open(export_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return FileResponse(
        export_path,
        media_type="text/csv",
        filename="requests_export.csv"
    )


@app.get("/export/google-sheet")
def export_for_google_sheet(limit: int = 100):
    """
    給 Google Sheet 用的簡化匯出。
    可以在 Google Sheet 裡用：
      =IMPORTDATA("https://你的網域/export/google-sheet?limit=50")
    """
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, created_at, user_input, mode FROM requests ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()

    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "user_input", "mode"])
    for row in rows:
        writer.writerow(row)
    return Response(content=output.getvalue(), media_type="text/csv")
@app.get("/export/google-sheet-flat")
def export_google_sheet_flat(limit: int = 200):
    """
    扁平版 CSV：把常用欄位攤平，Google Sheet 直接讀就乾淨。
    例：=IMPORTDATA("https://aijobvideobackend.zeabur.app/export/google-sheet-flat?limit=200")
    """
    import csv
    from io import StringIO

    # 1) 安全處理 limit
    try:
        limit = int(limit)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))  # 1~2000

    # 2) 讀資料（避免 LIMIT ? 綁定問題，這裡用字面量）
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, created_at, user_input, mode, response_json "
        f"FROM requests ORDER BY id DESC LIMIT {limit}"
    )
    rows = cur.fetchall()
    conn.close()

    # 3) 準備輸出
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "id","created_at","mode","user_input",
        "assistant_message",
        "segments_count",
        "hook_dialog","value_dialog","cta_dialog",
        "copy_main_copy","copy_hashtags"
    ])

    for _id, created_at, user_input, mode, resp_json in rows:
        assistant_message = ""
        segments_count = ""
        hook_dialog = value_dialog = cta_dialog = ""
        copy_main = ""
        copy_hashtags = ""

        try:
            data = json.loads(resp_json or "{}")
            assistant_message = (data.get("assistant_message") or "")[:500]

            segs = data.get("segments") or []
            if isinstance(segs, list):
                segments_count = len(segs)

                def find_dialog(t):
                    tl = str(t).lower()
                    for s in segs:
                        if str(s.get("type","")).lower() == tl:
                            return s.get("dialog","")
                    return ""

                hook_dialog  = find_dialog("hook")
                value_dialog = find_dialog("value")
                cta_dialog   = find_dialog("cta")

            copy = data.get("copy") or {}
            if isinstance(copy, dict):
                copy_main = copy.get("main_copy") or ""
                tags = copy.get("hashtags") or []
                if isinstance(tags, list):
                    copy_hashtags = " ".join(map(str, tags))
        except Exception:
            # 解析失敗就保持空字串，避免整支掛掉
            pass

        writer.writerow([
            _id, created_at, mode, user_input,
            assistant_message, segments_count,
            hook_dialog, value_dialog, cta_dialog,
            copy_main, copy_hashtags
        ])

    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "inline; filename=export_flat.csv"}
    )
