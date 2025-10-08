# knowledge_pdf.py
import os, io, time, re, math
import requests
from functools import lru_cache
from typing import List, Tuple
from pypdf import PdfReader

# 支援：URL 或 本地路徑（例如 Zeabur 的 /app/static/shortvideo.pdf）
KNOWLEDGE_PDF_URL = os.getenv("KNOWLEDGE_PDF_URL", "")
# 例如：KNOWLEDGE_PDF_URL=https://your-domain/static/短视频.pdf
# 或   KNOWLEDGE_PDF_URL=/app/static/短视频.pdf

_CACHE_TTL = int(os.getenv("KNOWLEDGE_CACHE_TTL", "600"))  # 10min
_cache_text: Tuple[str, float] = ("", 0.0)

def _now() -> float:
    return time.time()

def _fetch_bytes() -> bytes:
    if KNOWLEDGE_PDF_URL.startswith("http"):
        r = requests.get(KNOWLEDGE_PDF_URL, timeout=10)
        r.raise_for_status()
        return r.content
    # 當作本地檔案
    with open(KNOWLEDGE_PDF_URL, "rb") as f:
        return f.read()

def _pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for i, p in enumerate(reader.pages):
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n".join(pages)
    # 清一下多餘空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text

def load_knowledge_text(force: bool=False) -> str:
    global _cache_text
    text, exp = _cache_text
    if (not force) and text and _now() < exp:
        return text
    raw = _fetch_bytes()
    text = _pdf_to_text(raw)
    _cache_text = (text, _now() + _CACHE_TTL)
    return text

def _tokenish_len(s: str) -> int:
    # 粗略估 token，英中混排抓字數/1.6
    return max(1, math.ceil(len(s)/1.6))

def _split_paragraphs(text: str) -> List[str]:
    # 依空行或章節分段
    paras = re.split(r"\n\s*\n", text)
    # 去除過短/純符號
    return [p.strip() for p in paras if len(p.strip()) >= 10]

def _score(query: str, para: str) -> float:
    # 超輕量「關鍵詞重疊 + 位置」打分（避免上 RAG 向量庫）
    q = re.findall(r"[\w\u4e00-\u9fa5]+", query.lower())
    p = para.lower()
    if not q: return 0.0
    hit = 0
    for w in set(q):
        if len(w) <= 1: 
            continue
        c = p.count(w)
        if c:
            hit += 1.0 + 0.25*(c-1)
    # 偏好含有標題/結構詞的段落
    if re.search(r"(视频|標題|标题|文案|结构|節奏|钩子|开头|结尾|对比|揭秘|流量|变现)", para):
        hit *= 1.15
    return hit

def retrieve_context(query: str, max_token: int = 900) -> str:
    text = load_knowledge_text()
    paras = _split_paragraphs(text)
    scored = sorted(
        [(p, _score(query, p)) for p in paras],
        key=lambda x: x[1],
        reverse=True
    )
    picked, total = [], 0
    for p, s in scored[:80]:  # 掃前 80 段
        if s <= 0: break
        tl = _tokenish_len(p)
        if total + tl > max_token: 
            break
        picked.append(p.strip())
        total += tl
    # 若完全沒命中，回傳前幾段「綱要性內容」
    if not picked:
        base = []
        for p in paras[:8]:
            tl = _tokenish_len(p)
            if total + tl > max_token: break
            base.append(p)
            total += tl
        picked = base
    return "\n\n".join(picked)
