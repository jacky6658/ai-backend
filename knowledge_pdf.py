# knowledge_pdf.py
# 功能：讀取 PDF（本地或 URL），抽取純文字，根據用戶提問擷取最相關的段落，供 Prompt 使用。
import os, io, time, re, math
from typing import List, Tuple
import requests
from pypdf import PdfReader

# 可在環境變數設定：KNOWLEDGE_PDF_URL=/app/static/短视频.pdf 或 https://your.domain/static/短视频.pdf
KNOWLEDGE_PDF_URL = os.getenv("KNOWLEDGE_PDF_URL", "").strip()

_CACHE_TTL = int(os.getenv("KNOWLEDGE_CACHE_TTL", "600"))  # 預設 10 分鐘
_cache_text: Tuple[str, float] = ("", 0.0)

def _now() -> float:
    return time.time()

def _fetch_bytes() -> bytes:
    if not KNOWLEDGE_PDF_URL:
        raise RuntimeError("KNOWLEDGE_PDF_URL not set")
    if KNOWLEDGE_PDF_URL.startswith("http"):
        r = requests.get(KNOWLEDGE_PDF_URL, timeout=10)
        r.raise_for_status()
        return r.content
    # 當成本地檔案路徑
    with open(KNOWLEDGE_PDF_URL, "rb") as f:
        return f.read()

def _pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n".join(pages)
    # 乾淨化
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text

def load_knowledge_text(force: bool=False) -> str:
    """載入並快取整份 PDF 的純文字。"""
    global _cache_text
    text, exp = _cache_text
    if (not force) and text and _now() < exp:
        return text
    raw = _fetch_bytes()
    text = _pdf_to_text(raw)
    _cache_text = (text, _now() + _CACHE_TTL)
    return text

def _split_paragraphs(text: str) -> List[str]:
    # 以「空行」粗略切段
    paras = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paras if len(p.strip()) >= 10]

def _tokenish_len(s: str) -> int:
    # 粗估 token（英中混排）
    return max(1, math.ceil(len(s) / 1.6))

def _score(query: str, para: str) -> float:
    # 超輕量關鍵詞重疊打分（避免引入向量庫）
    q = re.findall(r"[\w\u4e00-\u9fa5]+", (query or "").lower())
    p = (para or "").lower()
    if not q: 
        return 0.0
    hit = 0.0
    for w in set(q):
        if len(w) <= 1:
            continue
        c = p.count(w)
        if c:
            hit += 1.0 + 0.25 * (c - 1)
    # 偏好與短影音結構/文案詞彙
    if re.search(r"(视频|短影音|標題|标题|文案|结构|節奏|钩子|鉤子|开头|結尾|对比|反差|流量|变现|變現|Hook|CTA)", para, flags=re.I):
        hit *= 1.15
    return hit

def retrieve_context(query: str, max_token: int = 900) -> str:
    """依據用戶提問，回傳 PDF 中最相關的多段落（總長控制在 max_token 粗估值內）。"""
    try:
        text = load_knowledge_text()
    except Exception:
        return ""  # 若 PDF 未設好，不阻斷主流程

    paras = _split_paragraphs(text)
    scored = sorted(((p, _score(query, p)) for p in paras), key=lambda x: x[1], reverse=True)

    picked, total = [], 0
    for p, s in scored[:100]:
        if s <= 0:
            break
        tl = _tokenish_len(p)
        if total + tl > max_token:
            break
        picked.append(p.strip())
        total += tl

    if not picked:
        # 沒命中時，取前幾段作為兜底簡要
        for p in paras[:8]:
            tl = _tokenish_len(p)
            if total + tl > max_token:
                break
            picked.append(p)
            total += tl

    return "\n\n".join(picked)
