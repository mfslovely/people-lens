"""Ingest policy Markdown into kb.* tables. Idempotent and version-aware, with no model retraining.

Run: python -m app.rag.ingest [paths...]   (default: data/policies/*.md)
- Re-ingesting an unchanged file is a no-op (sha256 match).
- A new version of an existing doc_key is added alongside the old one. `is_current` is recomputed from the effective
  dates and APP_TODAY, so retrieval prefers the version in force, while `as_of` queries can still reach older versions.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

from pgvector.psycopg import register_vector  # type: ignore[import-not-found]

from app.config import ROOT, get_settings
from app.core.observability import log, setup_logging
from app.db import tx
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embeddings import embed_passages


def ingest_file(path: Path) -> dict:
    raw = path.read_text()
    meta, body = parse_front_matter(raw)
    sha = hashlib.sha256(raw.encode()).hexdigest()
    with tx() as conn:
        register_vector(conn)
        doc = conn.execute(
            """INSERT INTO kb.documents (doc_key, title, category) VALUES (%s,%s,%s)
               ON CONFLICT (doc_key) DO UPDATE SET title = EXCLUDED.title, category = EXCLUDED.category RETURNING id""",
            (meta["doc_key"], meta["title"], meta.get("category", "general")),
        ).fetchone()
        existing = conn.execute(
            "SELECT id FROM kb.document_versions WHERE document_id=%s AND sha256=%s", (doc["id"], sha)
        ).fetchone()
        if existing:
            return {"file": path.name, "status": "unchanged"}
        conn.execute("DELETE FROM kb.document_versions WHERE document_id=%s AND version=%s", (doc["id"], str(meta["version"])))
        ver = conn.execute(
            """INSERT INTO kb.document_versions (document_id, version, effective_from, effective_to, source_path, sha256)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (doc["id"], str(meta["version"]), meta["effective_from"], meta.get("effective_to"),
             str(path.relative_to(ROOT)), sha),
        ).fetchone()
        chunks = chunk_markdown(body, meta["title"])
        vectors = embed_passages([f"{c.section_path}\n{c.content}" for c in chunks])
        for c, v in zip(chunks, vectors):
            conn.execute(
                """INSERT INTO kb.chunks (version_id, ordinal, section_path, content, token_count, embedding,
                       metadata, flagged_injection) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ver["id"], c.ordinal, c.section_path, c.content, c.token_count, v,
                 f'{{"doc_key": "{meta["doc_key"]}", "version": "{meta["version"]}"}}', c.flagged_injection),
            )
        flagged = sum(c.flagged_injection for c in chunks)
        if flagged:
            log.warning("ingest_injection_flagged", extra={"data": {"file": path.name, "chunks": flagged}})
        return {"file": path.name, "status": "ingested", "version": str(meta["version"]), "chunks": len(chunks),
                "flagged": flagged}


def refresh_current_versions(today: date | None = None) -> None:
    today = today or get_settings().today
    with tx() as conn:
        conn.execute("UPDATE kb.document_versions SET is_current = false")
        conn.execute(
            """UPDATE kb.document_versions v SET is_current = true
                FROM (SELECT DISTINCT ON (document_id) id FROM kb.document_versions
                       WHERE effective_from <= %(t)s AND (effective_to IS NULL OR effective_to >= %(t)s)
                       ORDER BY document_id, effective_from DESC) cur
               WHERE v.id = cur.id""",
            {"t": today},
        )


def main(paths: list[str]) -> None:
    setup_logging()
    files = [Path(p).resolve() for p in paths] or sorted((ROOT / "data/policies").glob("*.md"))
    for f in files:
        print(ingest_file(f))
    refresh_current_versions()
    print("current versions refreshed")


if __name__ == "__main__":
    main(sys.argv[1:])
