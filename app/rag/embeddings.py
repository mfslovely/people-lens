
"""Local embedding and reranking models (fastembed / ONNX). They load lazily and are reused for the life of the process."""

from __future__ import annotations

import threading
from functools import lru_cache

from app.config import get_settings

_lock = threading.Lock()


@lru_cache
def _embedder():
    from fastembed import TextEmbedding

    with _lock:
        return TextEmbedding(get_settings().embed_model)


@lru_cache
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _lock:
        return TextCrossEncoder(get_settings().rerank_model)


def embed_passages(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _embedder().passage_embed(texts)]


def embed_query(text: str) -> list[float]:
    return next(iter(_embedder().query_embed(text))).tolist()


def rerank(query: str, docs: list[str]) -> list[float]:
    if not docs:
        return []
    return [float(s) for s in _reranker().rerank(query, docs)]
