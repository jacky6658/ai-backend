# knowledge_text_loader.py
import os, re

# 讀取路徑：優先用環境變數，其次用預設檔名（繁中/簡中都兼容），最後退回 /data/kb.txt
DEFAULT_PATHS = [
    os.getenv("KNOWLEDGE_TXT_PATH", "").strip() or "/data/短視頻_知識庫.txt",
    "/data/短视频_知識庫.txt",
    "/data/kb.txt",
]

_CACHE = {"text": None, "path": None, "parts": None}

def _pick_path() -> str:
    for p in DEFAULT_PATHS:
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        except Exception:
            pass
    return DEFAULT_PATHS[0]

def load_knowledge_text(force: bool = False) -> str:
    """載入並快取整份知識庫文字。"""
    if _CACHE["text"] is not None and not force:
        return _CACHE["text"]
    path = _pick_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        # 用長破折號分隔章節（你的 txt 已使用 '--------------------------------'）
        sep = re.compile(r"\n-{10,}\n")
        parts = [p.strip() for p in sep.split(txt) if p.strip()]
        _CACHE.update({"text": txt, "path": path, "parts": parts or [txt]})
        print(f"[KB] loaded {len(_CACHE['parts'])} sections from: {path}")
        return txt
    except Exception as e:
        print(f"[KB] load error from {path}: {e}")
        _CACHE.update({"text": "", "path": path, "parts": [""]})
        return ""

def retrieve_context(query: str, max_chars: int = 1200) -> str:
    """
    超輕量檢索：依 query 關鍵字對各段落簡單打分，取最相關的若干段並裁到 max_chars。
    之後要升級向量檢索只需改這個函式。
    """
    if _CACHE["text"] is None:
        load_knowledge_text()
    parts = _CACHE["parts"] or [""]
    q = (query or "").lower()
    if not q:
        return "\n\n".join(parts)[:max_chars]

    words = [w for w in re.split(r"[\s、，,。.!?;；:/]+", q) if w]
    def score(p: str) -> int:
        pl = p.lower()
        return sum(1 for w in words if w in pl)

    ranked = sorted(parts, key=score, reverse=True)
    out = ""
    for p in ranked:
        if len(out) + len(p) + 2 > max_chars:
            break
        out += p + "\n\n"
    return out.strip()

