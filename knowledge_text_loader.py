# -*- coding: utf-8 -*-
"""
Simple knowledge text loader + retriever
- Reads from env KNOWLEDGE_TXT_PATH (default: /data/kb.txt)
- If not found, will try to concatenate /data/kb*.txt and /data/*.txt
- Provides:
    load_knowledge_text(force: bool=False) -> str
    retrieve_context(query: str, k: int=3, max_chars: int=1200) -> str
Designed to be dependency-free (pure Python).
"""

import os
import glob
import re
import math
from typing import List, Tuple

_KB_CACHE_TEXT: str = ""
_KB_CACHE_CHUNKS: List[str] = []
_KB_CACHE_TFIDF: dict = {}
_KB_CACHE_DF: dict = {}
_KB_CACHE_READY: bool = False

# ---------- File loading ----------

def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return f.read()
        except Exception:
            return ""

def _gather_all_text() -> str:
    """Try the env path first; if not found, concatenate candidates under /data."""
    env_path = os.getenv("KNOWLEDGE_TXT_PATH", "/data/kb.txt")
    if os.path.exists(env_path):
        t = _read_file(env_path)
        if t.strip():
            return t

    buf = []
    # prefer kb*.txt first
    for pattern in ("/data/kb*.txt", "/data/*.txt"):
        for p in sorted(glob.glob(pattern)):
            try:
                # skip obviously non-knowledge files
                base = os.path.basename(p).lower()
                if base.endswith(".csv"):
                    continue
                buf.append(_read_file(p))
            except Exception:
                pass
    return "\n\n".join(x for x in buf if x)

# ---------- Text chunking ----------

_SPLIT_RULE = re.compile(
    r"(?:\n[-=]{6,}\n|^\s*[-=]{6,}\s*$|^\s*#{1,6}\s+.+?$|^\s*第[一二三四五六七八九十]+\s*[、.．])",
    re.M | re.U
)

def _chunk_text(t: str, target_size: int = 350, max_size: int = 600) -> List[str]:
    """Split by headers/separators then re-pack into ~350-600 char chunks."""
    if not t:
        return []

    # first split by “大段落”標記
    rough = re.split(_SPLIT_RULE, t)
    parts = []
    for seg in rough:
        seg = (seg or "").strip()
        if seg:
            parts.append(seg)

    # merge to target_size
    chunks: List[str] = []
    buf = ""
    for seg in parts:
        if not buf:
            buf = seg
            continue
        if len(buf) + 1 + len(seg) <= max_size:
            buf = f"{buf}\n{seg}"
        else:
            chunks.append(buf.strip())
            buf = seg
    if buf.strip():
        chunks.append(buf.strip())

    # if still too long, hard split
    packed: List[str] = []
    for c in chunks:
        if len(c) <= max_size:
            packed.append(c)
        else:
            s = 0
            while s < len(c):
                packed.append(c[s:s+max_size])
                s += max_size
    return packed

# ---------- Tokenization & TF-IDF ----------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    tokens = []
    # english/number words
    tokens.extend(_WORD_RE.findall(text))
    # cjk characters (single char granularity)
    tokens.extend([ch for ch in text if _CJK_RE.match(ch)])
    # remove trivial tokens
    return [t for t in tokens if len(t) >= 1]

def _build_tfidf(chunks: List[str]):
    global _KB_CACHE_TFIDF, _KB_CACHE_DF
    _KB_CACHE_TFIDF = {}
    _KB_CACHE_DF = {}
    N = len(chunks) or 1

    # document frequency
    for idx, c in enumerate(chunks):
        seen = set(_tokenize(c))
        for tok in seen:
            _KB_CACHE_DF[tok] = _KB_CACHE_DF.get(tok, 0) + 1

    # per-chunk tf
    for idx, c in enumerate(chunks):
        tf = {}
        for tok in _tokenize(c):
            tf[tok] = tf.get(tok, 0) + 1
        _KB_CACHE_TFIDF[idx] = tf

    # store idf inside DF map (just reuse structure)
    for tok, df in _KB_CACHE_DF.items():
        _KB_CACHE_DF[tok] = math.log((1 + N) / (1 + df)) + 1.0

def _score_chunk(idx: int, query_tokens: List[str]) -> float:
    tf = _KB_CACHE_TFIDF.get(idx, {})
    score = 0.0
    for tok in query_tokens:
        if tok in tf:
            idf = _KB_CACHE_DF.get(tok, 1.0)
            score += tf[tok] * idf
    return score

# ---------- Public APIs ----------

def load_knowledge_text(force: bool = False) -> str:
    """Load raw text and build chunk/tfidf cache on first call or when force=True."""
    global _KB_CACHE_TEXT, _KB_CACHE_CHUNKS, _KB_CACHE_READY
    if _KB_CACHE_READY and not force:
        return _KB_CACHE_TEXT

    text = _gather_all_text()
    _KB_CACHE_TEXT = text
    _KB_CACHE_CHUNKS = _chunk_text(text)
    _build_tfidf(_KB_CACHE_CHUNKS)
    _KB_CACHE_READY = True
    return _KB_CACHE_TEXT

def retrieve_context(query: str, k: int = 3, max_chars: int = 1200) -> str:
    """Return top-k relevant chunks (TF-IDF) concatenated, capped by max_chars."""
    if not _KB_CACHE_READY:
        load_knowledge_text(force=False)

    if not query:
        return ""

    q_tokens = _tokenize(query)
    if not q_tokens:
        return ""

    scores: List[Tuple[float, int]] = []
    for idx in range(len(_KB_CACHE_CHUNKS)):
        s = _score_chunk(idx, q_tokens)
        if s > 0:
            scores.append((s, idx))

    # sort by score desc
    scores.sort(key=lambda x: x[0], reverse=True)
    picked = []
    used = 0
    for _, idx in scores[: max(1, k)]:
        chunk = _KB_CACHE_CHUNKS[idx]
        if used + len(chunk) + 2 > max_chars:
            remain = max_chars - used
            if remain > 0:
                picked.append(chunk[:remain])
            break
        picked.append(chunk)
        used += len(chunk) + 2

    return "\n---\n".join(picked)

