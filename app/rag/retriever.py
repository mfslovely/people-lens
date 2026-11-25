"""Hybrid retrieval: pgvector cosine + Postgres full-text, fused with RRF, then reranked by a cross-encoder.

Abstention: if the best reranked score is below `rag_min_rerank_score`, no evidence is returned and the agent must say
the policies don't cover the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pgvector.psycopg import register_vector  # type: ignore[import-not-found]

from app.agent.models import Citation
from app.config import get_settings
from app.db import tx
from app.rag.embeddings import embed_query, rerank

RRF_K = 60
CANDIDATES = 20
_STOP = {"what", "is", "the", "a", "an", "of", "to", "i", "my", "am", "can", "do", "how", "many", "who", "if", "for",
         "and", "or", "in", "on", "me", "about", "does", "are", "be", "will", "it", "this", "that", "with", "policy"}


@dataclass
class Retrieved:
    citation: Citation
    content: str
    flagged: bool


def _version_filter(as_of: date | None) -> tuple[str, dict]:
    if as_of:
        return ("v.effective_from <= %(as_of)s AND (v.effective_to IS NULL OR v.effective_to >= %(as_of)s)",
                {"as_of": as_of})
    return "v.is_current", {}


def search(query: str, as_of: date | None = None, k: int | None = None) -> list[Retrieved]:
    s = get_settings()
    k = k or s.rag_top_k
    vf, params = _version_filter(as_of)
    qvec = embed_query(query)
    terms = [t for t in re.findall(r"[a-z]+", query.lower()) if t not in _STOP and len(t) > 2]
    tsq = " | ".join(terms) if terms else "policy"
    base = f"""SELECT c.id, c.content, c.section_path, c.flagged_injection, d.doc_key, d.title, v.version
                 FROM kb.chunks c JOIN kb.document_versions v ON v.id = c.version_id
                 JOIN kb.documents d ON d.id = v.document_id WHERE {vf}"""
    with tx() as conn:
        register_vector(conn)
        import numpy as np

        params = {**params, "q": np.array(qvec), "tsq": tsq, "n": CANDIDATES}
        vec_rows = conn.execute(base + " ORDER BY c.embedding <=> %(q)s LIMIT %(n)s", params).fetchall()
        fts_rows = conn.execute(
            base + " AND c.tsv @@ to_tsquery('english', %(tsq)s) "
                   "ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', %(tsq)s)) DESC LIMIT %(n)s", params
        ).fetchall()

    fused: dict[int, float] = {}
    rows: dict[int, dict] = {}
    for ranking in (vec_rows, fts_rows):
        for rank, r in enumerate(ranking):
            fused[r["id"]] = fused.get(r["id"], 0) + 1 / (RRF_K + rank + 1)
            rows[r["id"]] = r
    top = sorted(fused, key=fused.get, reverse=True)[:10]
    if not top:
        return []
    scores = rerank(query, [f"{rows[i]['section_path']}\n{rows[i]['content']}" for i in top])
    ranked = sorted(zip(top, scores), key=lambda x: x[1], reverse=True)
    out = []
    for cid, score in ranked[:k]:
        if score < s.rag_min_rerank_score:
            continue
        r = rows[cid]
        out.append(Retrieved(
            citation=Citation(chunk_id=cid, doc_key=r["doc_key"], version=r["version"], section_path=r["section_path"],
                              snippet=r["content"][:240], score=round(score, 3)),
            content=r["content"], flagged=r["flagged_injection"]))
    return out
