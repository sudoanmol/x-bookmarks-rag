"""Turn stored bookmarks into embeddable chunks.

Layer 1 chunks only what capture already holds: post text, quoted posts, the
X Article stub, and link cards. Later layers add article bodies, captions, and
transcripts through the same `chunks` table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# EmbeddingGemma holds 2048 tokens. Roughly four characters per token, kept well
# under the ceiling so the author prefix and framing always fit.
MAX_CHARS = 4_800
OVERLAP_CHARS = 200


@dataclass
class Chunk:
    tweet_id: str
    source: str
    ref: str | None
    position: int
    text: str
    lang: str | None


def split_long(text: str) -> list[str]:
    """Split on paragraph breaks, then hard-wrap anything still too long."""
    if len(text) <= MAX_CHARS:
        return [text]

    parts: list[str] = []
    buffer = ""
    for paragraph in text.split("\n\n"):
        if len(buffer) + len(paragraph) + 2 <= MAX_CHARS:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            continue
        if buffer:
            parts.append(buffer)
        while len(paragraph) > MAX_CHARS:
            parts.append(paragraph[:MAX_CHARS])
            paragraph = paragraph[MAX_CHARS - OVERLAP_CHARS :]
        buffer = paragraph
    if buffer:
        parts.append(buffer)
    return parts


def chunks_for_row(row: sqlite3.Row) -> list[Chunk]:
    """Build every chunk a single bookmark contributes."""
    handle = f"@{row['screen_name']}" if row["screen_name"] else "@unknown"
    out: list[Chunk] = []

    def add(source: str, text: str, ref: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        for piece in split_long(text):
            out.append(
                Chunk(
                    tweet_id=row["tweet_id"],
                    source=source,
                    ref=ref,
                    position=len(out),
                    # The handle rides along so "what did X say about Y" works.
                    text=f"{handle}: {piece}",
                    lang=row["lang"],
                )
            )

    add("post", row["text"])
    add("quote", row["quoted_text"])

    if row["article_title"]:
        preview = row["article_preview"] or ""
        add("article", f"{row['article_title']}\n\n{preview}".strip())

    return out


def chunks_for_links(rows: list[sqlite3.Row]) -> list[Chunk]:
    """Link cards carry a title and description worth indexing on their own."""
    out: list[Chunk] = []
    for row in rows:
        body = " — ".join(p for p in (row["title"], row["description"]) if p)
        if not body:
            continue
        out.append(
            Chunk(
                tweet_id=row["tweet_id"],
                source="link",
                ref=row["url"],
                position=0,
                text=f"{row['domain']}: {body}",
                lang=None,
            )
        )
    return out
