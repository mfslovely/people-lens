"""Section-aware Markdown chunking.

Policies are short and strongly structured, so each chunk is one heading section with its heading path kept, for example
"Leave and Holiday Policy > 4. Earned Leave > 4.4 Carry forward". The heading path makes citations precise and gives
lexical search useful terms. Sections longer than MAX_WORDS are split on paragraph boundaries with a one-paragraph overlap.
Tables are never split.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

MAX_WORDS = 280

_INJECTION = re.compile(
    r"[^.\n]*\b(ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(system|previous)|"
    r"system prompt|note to (ai|llm|assistants?)|you are now|approve all)\b[^.\n]*[.\n]?",
    re.I,
)


@dataclass
class Chunk:
    ordinal: int
    section_path: str
    content: str
    flagged_injection: bool

    @property
    def token_count(self) -> int:
        return int(len(self.content.split()) * 1.3)


def parse_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("policy document is missing YAML front matter")
    return yaml.safe_load(m.group(1)), m.group(2)


def sanitize(text: str) -> tuple[str, bool]:
    """Neutralise instruction-like text inside documents. Retrieved content is data, never instructions."""
    cleaned, n = _INJECTION.subn("[removed: instruction-like text detected during ingestion] ", text)
    return cleaned, n > 0


def chunk_markdown(body: str, title: str) -> list[Chunk]:
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = [title]
    buf: list[str] = []

    def flush():
        if any(line.strip() for line in buf):
            sections.append((list(path), list(buf)))
        buf.clear()

    for line in body.splitlines():
        h = re.match(r"^(#{1,4})\s+(.*)", line)
        if h:
            flush()
            level = len(h.group(1))
            heading = h.group(2).strip()
            if level == 1:
                path = [title]
            else:
                path = path[: level - 1] + [heading]
            continue
        buf.append(line)
    flush()

    chunks: list[Chunk] = []
    for spath, lines in sections:
        text = "\n".join(lines).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        groups: list[list[str]] = [[]]
        for p in paras:
            if sum(len(x.split()) for x in groups[-1]) + len(p.split()) > MAX_WORDS and groups[-1]:
                groups.append([groups[-1][-1]] if not groups[-1][-1].startswith("|") else [])
            groups[-1].append(p)
        for g in groups:
            content, flagged = sanitize("\n\n".join(g))
            chunks.append(Chunk(len(chunks), " > ".join(spath), content, flagged))
    return chunks
