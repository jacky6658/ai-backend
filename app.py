#!/usr/bin/env python
# coding: utf-8
"""
FastAPI 後端服務
- POST /generate_script
- 使用 Gemini 生成短影片腳本（片頭/場景/片尾）
- 分段生成（根據 previous_segments 決定補哪些段落）
- 自動重試（429/502/timeout/網路錯誤）
- SQLite 永久化每次生成結果
"""

import os
import json
import time
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional, Literal, Any, Dict, Tuple

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

# ----------------------------
# 環境變數與 Gemini 設定
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
# 若你想使用你提到的 "gemini-2.5-flash"，只要設定環境變數 GEMINI_MODEL="gemini-2.5-flash"

if not GEMINI_API_KEY:
    raise RuntimeError("環境變數 GEMINI_API_KEY 未設定。請先 export GEMINI_API_KEY=...")

genai.configure(api_key=GEMINI_API_KEY)

# ----------------------------
# FastAPI 初始化 & CORS
# ----------------------------
app = FastAPI(title="Script Generator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 視情況鎖定網域
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# SQLite 初始化
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "script_generation.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # 儲存每次請求與回應
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
    # 可選：把 segments 正規化到子表（若將來要查詢更方便）
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
# Pydantic Schema
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
# 工具：決定需要生成的段落
# ----------------------------
ALL_TYPES: List[SegmentType] = ["片頭", "場景", "片尾"]


def segments_missing(previous: Optional[List[Segment]]) -> List[SegmentType]:
    if not previous:
        return ALL_TYPES[:]  # 全部生成
    have = {s.type for s in previous}
    return [t for t in ALL_TYPES if t not in have]


# ----------------------------
# Gemini 呼叫與重試
# ----------------------------
class GeminiError(Exception):
    pass


RetryableStatus = {429, 502, 503, 504}


def build_prompt(user_input: str, need_types: List[SegmentType], previous_segments: Optional[List[Segment]]) -> str:
    """
    為 Gemini 構造提示，要求嚴格輸出 JSON（僅含 segments 陣列）
    """
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
5) 請確保 JSON 合法可 parse。

使用者需求：
{user_input}

已存在的段落（請勿重複生成）：
{prev_text}
    """.strip()


def parse_model_json(txt: str) -> Dict[str, Any]:
    """
    嘗試從模型輸出解析 JSON；若包裹在程式碼區塊或前後雜訊，試著抽離。
    """
    # 直接嘗試
    try:
        return json.loads(txt)
    except Exception:
        pass

    # 嘗試抽取第一個 { ... } 區塊
    start = txt.find("{")
    end = txt.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = txt[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 最後嘗試移除 Markdown code fence
    if "```" in txt:
        parts = txt.split("```")
        for p in parts:
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
    request_timeout_sec: float = 60.0,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    呼叫 Gemini，具退避重試。
    回傳 (parsed_json, request_id)
    """
    last_err: Optional[Exception] = None
    request_id: Optional[str] = None

    # 設定回應為純 JSON（較新 SDK 支援 response_mime_type）
    generation_config = {
        "temperature": 0.8,
        "top_p": 0.95,
        "response_mime_type": "application/json",
    }

    for attempt in range(1, max_retries + 1):
        try:
            model = genai.GenerativeModel(model_name)
            start_ts = time.time()
            response = model.generate_content(
                prompt,
                generation_config=generation_config,
                # safety_settings 可依需求調整
            )
            elapsed = time.time() - start_ts
            if elapsed > request_timeout_sec:
                raise GeminiError(f"Timeout > {request_timeout_sec}s")

            # 有些版本把 request_id 放在 response 的 meta 裡；不同 SDK 可能差異
            try:
                request_id = getattr(response, "request_id", None)
            except Exception:
                request_id = None

            # 取得文字並解析 JSON
            text = response.text or ""
            parsed = parse_model_json(text)
            return parsed, request_id

        except Exception as e:
            last_err = e

            # 嘗試辨識是否需要重試（簡化處理）
            msg = str(e).lower()
            is_retryable = (
                "429" in msg
                or "502" in msg
                or "503" in msg
                or "504" in msg
                or "timeout" in msg
                or "temporarily" in msg
                or "unavailable" in msg
                or "rate" in msg
            )
            if attempt < max_retries and is_retryable:
                time.sleep(base_delay * (2 ** (attempt - 1)))  # 指數退避
                continue
            break

    raise GeminiError(f"Gemini 呼叫失敗：{last_err}")


# ----------------------------
# 永久化：寫入 DB
# ----------------------------
def persist_generation(
    user_input: str,
    previous_segments: Optional[List[Segment]],
    response_json: Optional[Dict[str, Any]],
    error: Optional[str],
    model: str,
    request_id: Optional[str],
) -> str:
    """將一次請求/回應紀錄到 SQLite，並把 segments 寫入子表。"""
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
# API：POST /generate_script
# ----------------------------
@app.post("/generate_script", response_model=GenerateResponse)
def generate_script(payload: GenerateRequest):
    need = segments_missing(payload.previous_segments)
    if not need:
        # 都有了，直接回傳既有內容即可
        return GenerateResponse(segments=payload.previous_segments or [], error=None)

    prompt = build_prompt(payload.user_input, need, payload.previous_segments)

    try:
        parsed_json, request_id = call_gemini_with_retry(
            prompt=prompt,
            model_name=GEMINI_MODEL,
            max_retries=3,
            base_delay=1.0,
            request_timeout_sec=90.0,
        )
        # 基本結構/欄位驗證
        if "segments" not in parsed_json or not isinstance(parsed_json["segments"], list):
            raise GeminiError("模型回應缺少 'segments' 陣列")

        # 合併 previous + 新生成
        merged = list(payload.previous_segments or [])
        for item in parsed_json["segments"]:
            # 轉成 Pydantic Segment（同時做欄位校驗/補預設）
            seg = Segment(
                type=item.get("type"),  # 若模型不小心產出英文，會在此處報錯
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
            )
            # 僅加入缺少類型（避免重複）
            if seg.type in need:
                merged.append(seg)

        # 檢查是否真的補齊
        merged_types = {s.type for s in merged}
        missing_after_merge = [t for t in ALL_TYPES if t not in merged_types]
        error_msg = None
        if missing_after_merge:
            error_msg = f"仍缺少段落：{', '.join(missing_after_merge)}"

        # 落庫
        persisted_id = persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json={"segments": [s.dict() for s in merged]},
            error=error_msg,
            model=GEMINI_MODEL,
            request_id=request_id,
        )

        # 回傳
        return GenerateResponse(segments=merged, error=error_msg)

    except GeminiError as ge:
        # 落庫錯誤案例
        persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json=None,
            error=str(ge),
            model=GEMINI_MODEL,
            request_id=None,
        )
        # 維持固定回應格式
        return JSONResponse(
            status_code=502,
            content=GenerateResponse(segments=[], error=str(ge)).dict(),
        )
    except Exception as e:
        persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json=None,
            error=f"Unhandled: {e}",
            model=GEMINI_MODEL,
            request_id=None,
        )
        return JSONResponse(
            status_code=500,
            content=GenerateResponse(segments=[], error=f"Unhandled: {e}").dict(),
        )


# ----------------------------
# Health Check
# ----------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "model": GEMINI_MODEL, "db": DB_PATH}
#!/usr/bin/env python
# coding: utf-8
"""
FastAPI 後端服務
- POST /generate_script
- 使用 Gemini 生成短影片腳本（片頭/場景/片尾）
- 分段生成（根據 previous_segments 決定補哪些段落）
- 自動重試（429/502/timeout/網路錯誤）
- SQLite 永久化每次生成結果
"""

import os
import json
import time
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional, Literal, Any, Dict, Tuple

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

# ----------------------------
# 環境變數與 Gemini 設定
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
# 若你想使用你提到的 "gemini-2.5-flash"，只要設定環境變數 GEMINI_MODEL="gemini-2.5-flash"

if not GEMINI_API_KEY:
    raise RuntimeError("環境變數 GEMINI_API_KEY 未設定。請先 export GEMINI_API_KEY=...")

genai.configure(api_key=GEMINI_API_KEY)

# ----------------------------
# FastAPI 初始化 & CORS
# ----------------------------
app = FastAPI(title="Script Generator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 視情況鎖定網域
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# SQLite 初始化
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "script_generation.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # 儲存每次請求與回應
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
    # 可選：把 segments 正規化到子表（若將來要查詢更方便）
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
# Pydantic Schema
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
# 工具：決定需要生成的段落
# ----------------------------
ALL_TYPES: List[SegmentType] = ["片頭", "場景", "片尾"]


def segments_missing(previous: Optional[List[Segment]]) -> List[SegmentType]:
    if not previous:
        return ALL_TYPES[:]  # 全部生成
    have = {s.type for s in previous}
    return [t for t in ALL_TYPES if t not in have]


# ----------------------------
# Gemini 呼叫與重試
# ----------------------------
class GeminiError(Exception):
    pass


RetryableStatus = {429, 502, 503, 504}


def build_prompt(user_input: str, need_types: List[SegmentType], previous_segments: Optional[List[Segment]]) -> str:
    """
    為 Gemini 構造提示，要求嚴格輸出 JSON（僅含 segments 陣列）
    """
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
5) 請確保 JSON 合法可 parse。

使用者需求：
{user_input}

已存在的段落（請勿重複生成）：
{prev_text}
    """.strip()


def parse_model_json(txt: str) -> Dict[str, Any]:
    """
    嘗試從模型輸出解析 JSON；若包裹在程式碼區塊或前後雜訊，試著抽離。
    """
    # 直接嘗試
    try:
        return json.loads(txt)
    except Exception:
        pass

    # 嘗試抽取第一個 { ... } 區塊
    start = txt.find("{")
    end = txt.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = txt[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 最後嘗試移除 Markdown code fence
    if "```" in txt:
        parts = txt.split("```")
        for p in parts:
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
    request_timeout_sec: float = 60.0,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    呼叫 Gemini，具退避重試。
    回傳 (parsed_json, request_id)
    """
    last_err: Optional[Exception] = None
    request_id: Optional[str] = None

    # 設定回應為純 JSON（較新 SDK 支援 response_mime_type）
    generation_config = {
        "temperature": 0.8,
        "top_p": 0.95,
        "response_mime_type": "application/json",
    }

    for attempt in range(1, max_retries + 1):
        try:
            model = genai.GenerativeModel(model_name)
            start_ts = time.time()
            response = model.generate_content(
                prompt,
                generation_config=generation_config,
                # safety_settings 可依需求調整
            )
            elapsed = time.time() - start_ts
            if elapsed > request_timeout_sec:
                raise GeminiError(f"Timeout > {request_timeout_sec}s")

            # 有些版本把 request_id 放在 response 的 meta 裡；不同 SDK 可能差異
            try:
                request_id = getattr(response, "request_id", None)
            except Exception:
                request_id = None

            # 取得文字並解析 JSON
            text = response.text or ""
            parsed = parse_model_json(text)
            return parsed, request_id

        except Exception as e:
            last_err = e

            # 嘗試辨識是否需要重試（簡化處理）
            msg = str(e).lower()
            is_retryable = (
                "429" in msg
                or "502" in msg
                or "503" in msg
                or "504" in msg
                or "timeout" in msg
                or "temporarily" in msg
                or "unavailable" in msg
                or "rate" in msg
            )
            if attempt < max_retries and is_retryable:
                time.sleep(base_delay * (2 ** (attempt - 1)))  # 指數退避
                continue
            break

    raise GeminiError(f"Gemini 呼叫失敗：{last_err}")


# ----------------------------
# 永久化：寫入 DB
# ----------------------------
def persist_generation(
    user_input: str,
    previous_segments: Optional[List[Segment]],
    response_json: Optional[Dict[str, Any]],
    error: Optional[str],
    model: str,
    request_id: Optional[str],
) -> str:
    """將一次請求/回應紀錄到 SQLite，並把 segments 寫入子表。"""
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
# API：POST /generate_script
# ----------------------------
@app.post("/generate_script", response_model=GenerateResponse)
def generate_script(payload: GenerateRequest):
    need = segments_missing(payload.previous_segments)
    if not need:
        # 都有了，直接回傳既有內容即可
        return GenerateResponse(segments=payload.previous_segments or [], error=None)

    prompt = build_prompt(payload.user_input, need, payload.previous_segments)

    try:
        parsed_json, request_id = call_gemini_with_retry(
            prompt=prompt,
            model_name=GEMINI_MODEL,
            max_retries=3,
            base_delay=1.0,
            request_timeout_sec=90.0,
        )
        # 基本結構/欄位驗證
        if "segments" not in parsed_json or not isinstance(parsed_json["segments"], list):
            raise GeminiError("模型回應缺少 'segments' 陣列")

        # 合併 previous + 新生成
        merged = list(payload.previous_segments or [])
        for item in parsed_json["segments"]:
            # 轉成 Pydantic Segment（同時做欄位校驗/補預設）
            seg = Segment(
                type=item.get("type"),  # 若模型不小心產出英文，會在此處報錯
                camera=item.get("camera", ""),
                dialog=item.get("dialog", ""),
                visual=item.get("visual", ""),
            )
            # 僅加入缺少類型（避免重複）
            if seg.type in need:
                merged.append(seg)

        # 檢查是否真的補齊
        merged_types = {s.type for s in merged}
        missing_after_merge = [t for t in ALL_TYPES if t not in merged_types]
        error_msg = None
        if missing_after_merge:
            error_msg = f"仍缺少段落：{', '.join(missing_after_merge)}"

        # 落庫
        persisted_id = persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json={"segments": [s.dict() for s in merged]},
            error=error_msg,
            model=GEMINI_MODEL,
            request_id=request_id,
        )

        # 回傳
        return GenerateResponse(segments=merged, error=error_msg)

    except GeminiError as ge:
        # 落庫錯誤案例
        persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json=None,
            error=str(ge),
            model=GEMINI_MODEL,
            request_id=None,
        )
        # 維持固定回應格式
        return JSONResponse(
            status_code=502,
            content=GenerateResponse(segments=[], error=str(ge)).dict(),
        )
    except Exception as e:
        persist_generation(
            user_input=payload.user_input,
            previous_segments=payload.previous_segments,
            response_json=None,
            error=f"Unhandled: {e}",
            model=GEMINI_MODEL,
            request_id=None,
        )
        return JSONResponse(
            status_code=500,
            content=GenerateResponse(segments=[], error=f"Unhandled: {e}").dict(),
        )


# ----------------------------
# Health Check
# ----------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "model": GEMINI_MODEL, "db": DB_PATH}

