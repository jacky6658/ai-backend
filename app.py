#!/usr/bin/env python
# coding: utf-8
"""
FastAPI 後端服務（相容 + 雲端寫入保險版）
- 核心 API（不可變）：POST /generate_script
  Request:  {"user_input": "...", "previous_segments": [...]}
  Response: {"segments":[{"type":"片頭","camera":"...","dialog":"...","visual":"..."}], "error": null}
- 功能：
  * Gemini 生成（分段生成 + 聚合）
  * 退避重試（429/502/503/504/timeout/網路錯誤）
  * SQLite 永久化每次生成結果（自動挑選可寫目錄）
- 運維：
  * GET /healthz（健康檢查）
  * GET / 與 /favicon.ico（避免瀏覽器 404 噪音）
"""

import os
import json
import time
import uuid
import sqlite3
from datetime import datetime
from typing import List, Optional, Literal, Dict, Any, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

import google.generativeai as genai

# ----------------------------
# App 先宣告（避免裝飾器時找不到 app）
# ----------------------------
app = FastAPI(title="Script Generator API", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 視需求鎖定網域
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# 環境變數與 Gemini 設定（不改 API）
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

# DB 預設（可被環境變數覆蓋）；若不可寫，後續會自動 fallback
DB_PATH_ENV = os.getenv("DB_PATH", "/app/script_generation.db").strip()

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_available = True
else:
    gemini_available = False  # /generate_script 會回 502；/healthz OK

# ----------------------------
# SQLite：挑選可寫路徑（解決雲端唯讀目錄）
# ----------------------------
def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def _touch(path: str) -> None:
    # 預先建立檔案，避免部分平台首次連線報錯
    with open(path, "a", encoding="utf-8"):
        pass

def pick_writable_db_path() -> str:
    """
    依序嘗試：
    1) DB_PATH_ENV（例如你手動設成 /data/script_generation.db）
    2) /data/script_generation.db
    3) /tmp/appdata/script_generation.db
    成功即可返回；若都失敗則拋出例外。
    """
    candidates = [
        DB_PATH_ENV,
        "/data/script_generation.db",
        "/tmp/appdata/script_generation.db",
    ]
    last_err = None
    for p in candidates:
        try:
            _ensure_parent_dir(p)
            _touch(p)
            # 測試能否開啟資料庫（建立檔案 + pragma）
            conn = sqlite3.connect(p)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.close()
            print(f"[BOOT] SQLite path OK: {p}")
            return p
        except Exception as e:
            last_err = e
            print(f"[BOOT] SQLite path failed: {p} -> {e}")
            continue
    raise sqlite3.OperationalError(f"No writable DB path. last_err={last_err}")

DB_PATH = pick_writable_db_path()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            user_input TEXT,
            previous_segments_json TEXT,
            response_json TEXT,
            error TEXT,
            model TEXT,
            request_id TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id TEXT NOT NULL,
            type TEXT NOT NULL,
            camera TEXT,
            dialog TEXT,
            visual TEXT,
            FOREIGN KEY (generation_id) REFERENCES generations(id)
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

# ----------------------------
# Pydantic 結構（不可變）
# ----------------------------
SegmentType = Literal["片頭", "場景", "片尾"]

class Segment(BaseModel):
    type: SegmentType
    camera: str
    dialog: str
    visual: str

class GenerateRequest(BaseModel):
    user_input: str = Field(..., description="使用者對影片主題或需求的描述")
    previous_segments: Optional[List[Segment]] = Field(
        default=None, description="已有的段落（用於分段生成）"
    )

class GenerateResponse(BaseModel):
    segments: List[Segment]
    error: Optional[str] = None

# ----------------------------
# 分段判定
# ----------------------------
ALL_TYPES: List[SegmentType] = ["片頭", "場景", "片尾"]

def segments_missing(previous: Optional[List[Segment]]) -> List[SegmentType]:
    if not previous:
        return ALL_TYPES[:]  # 全部生成
    have = {s.type for s in previous}
    return [t for t in ALL_TYPES if t not in have]

# ----------------------------
# Gemini 呼叫 + 重試
# ----------------------------
class GeminiError(Exception):
    pass

def build_prompt(user_input: str, need_types: List[SegmentType], previous_segments: Optional[List[Segment]]) -> str:
    prev_text = json.dumps([s.dict() for s in (previous_segments or [])], ensure_ascii=False, indent=2)
    need_text = ", ".join(need_types) if need_types else "(無)"
    return f"""
你是資深短影音腳本編劇。請根據使用者需求生成「短影片腳本」的分段內容。
要求：
1) 僅輸出 JSON（不要任何說明文字），格式：
{{
  "segments": [
    {{"type":"片頭","camera":"...","dialog":"...","visual":"..."}},
    {{"type":"場景","camera":"...","dialog":"...","visual":"..."}},
    {{"type":"片尾","camera":"...","dialog":"...","visual":"..."}}
  ]
}}
2) 你「只生成」以下缺少的段落：{need_text}
3) 每個欄位務必是字串，不可為空；dialog 要口語且符合短影音節奏（5-12秒/段）。
4) camera：鏡位與運鏡；visual：畫面元素與轉場。
5) 確保 JSON 可被 parse（無多餘註解/文字/markdown）。

使用者需求：
{user_input}

已存在的段落（請勿重複生成）：
{prev_text}
""".strip()

def parse_model_json(txt: str) -> Dict[str, Any]:
    try:
        return json.loads(txt)
    except Exception:
        pass
    s, e = txt.find("{"), txt.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(txt[s:e+1])
        except Exception:
            pass
    if "```" in txt:
        for p in txt.split("```"):
            p = p.strip()
            if p.startswith("{") and p.endswith("}"):
                try:
                    return json.loads(p)
                except Exception:
                    continue
    raise GeminiError("模型輸出非合法 JSON")

def call_gemini_with_retry(
    prompt: str,
    model_name: str,
    max_retries: int = 3,
    base_delay: float = 1.0,
    timeout_sec: float = 90.0,
) -> Tuple[Dict[str, Any], Optional[str]]:
    if not gemini_available:
        raise GeminiError("GEMINI_API_KEY 未設定")

    last_err: Optional[Exception] = None
    request_id: Optional[str] = None

    generation_config = {
        "temperature": 0.8,
        "top_p": 0.95,
        "response_mime_type": "application/json",
    }

    for attempt in range(1, max_retries + 1):
        try:
            model = genai.GenerativeModel(model_name)
            start = time.time()
            resp = model.generate_content(prompt, generation_config=generation_config)
            if (time.time() - start) > timeout_sec:
                raise GeminiError(f"Timeout > {timeout_sec}s")

            try:
                request_id = getattr(resp, "request_id", None)
            except Exception:
                request_id = None

            text = resp.text or ""
            parsed = parse_model_json(text)
            return parsed, request_id

        except Exception as e:
            last_err = e
            msg = str(e).lower()
            retryable = any(k in msg for k in ["429", "502", "503", "504", "timeout", "temporarily", "unavailable", "rate"])
            if attempt < max_retries and retryable:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            break

    raise GeminiError(f"Gemini 呼叫失敗：{last_err}")

# ----------------------------
# DB 持久化
# ----------------------------
def persist_generation(
    user_input: str,
    previous_segments: Optional[List[Segment]],
    response_json: Optional[Dict[str, Any]],
    error: Optional[str],
    model: str,
    request_id: Optional[str],
) -> str:
    generation_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO generations (id, created_at, user_input, previous_segments_json, response_json, error, model, request_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            created_at,
            user_input,
            json.dumps([s.dict() for s in (previous_segments or [])], ensure_ascii=False),
            json.dumps(response_json, ensure_ascii=False) if response_json else None,
            error,
            model,
            request_id,
        ),
    )
    if response_json and "segments" in response_json:
        for seg in response_json["segments"]:
            cur.execute(
                """
                INSERT INTO segments (generation_id, type, camera, dialog, visual)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    seg.get("type", ""),
                    seg.get("camera", ""),
                    seg.get("dialog", ""),
                    seg.get("visual", ""),
                ),
            )
    conn.commit()
    conn.close()
    return generation_id

# ----------------------------
# 核心 API（不可變）：POST /generate_script
# ----------------------------
@app.post("/generate_script", response_model=GenerateResponse)
def generate_script(payload: GenerateRequest):
    need = segments_missing(payload.previous_segments)
    if not need:
        return GenerateResponse(segments=payload.previous_segments or [], error=None)

    prompt = build_prompt(payload.user_input, need, payload.previous_segments)

    try:
        parsed_json, request_id = call_gemini_with_retry(
            prompt=prompt,
            model_name=GEMINI_MODEL,
            max_retries=3,
            base_delay=1.0,
            timeout_sec=90.0,
        )
        if "segments" not in parsed_json or not isinstance(parsed_json["segments"], list):
            raise GeminiError("模型回應缺少 'segments' 陣列")

        # 合併（只補缺的）
        merged: List[Segment] = list(payload.previous_segments or [])
        for item in parsed_json["segments"]:
            seg = Segment(
                type=item.get("type"),
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
            )
            if seg.type in need:
                merged.append(seg)

        # 補齊檢查
        merged_types = {s.type for s in merged}
        missing_after = [t for t in ALL_TYPES if t not in merged_types]
        err_msg = None if not missing_after else f"仍缺少段落：{', '.join(missing_after)}"

        # 入庫
        persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json={"segments": [s.dict() for s in merged]},
            error=err_msg,
            model=GEMINI_MODEL,
            request_id=request_id,
        )
        return GenerateResponse(segments=merged, error=err_msg)

    except GeminiError as ge:
        persist_generation(payload.user_input, payload.previous_segments, None, str(ge), GEMINI_MODEL, None)
        return JSONResponse(status_code=502, content=GenerateResponse(segments=[], error=str(ge)).dict())
    except Exception as e:
        persist_generation(payload.user_input, payload.previous_segments, None, f"Unhandled: {e}", GEMINI_MODEL, None)
        return JSONResponse(status_code=500, content=GenerateResponse(segments=[], error=f"Unhandled: {e}").dict())

# ----------------------------
# 運維：健康檢查與友善首頁（不影響核心 API）
# ----------------------------
@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "model": GEMINI_MODEL,
        "db": DB_PATH,
        "gemini": gemini_available
    }

@app.get("/")
def index():
    return {
        "service": "AI Script Backend",
        "endpoints": {
            "POST /generate_script": "主 API，請以 JSON 請求",
            "GET /healthz": "健康檢查"
        }
    }

@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)
