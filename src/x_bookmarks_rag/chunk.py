"""Turn stored bookmarks into embeddable chunks.

Capture holds post text, quoted posts, the X Article stub, and link cards.
Extraction adds the full text behind articles and links. When a full document
exists, it replaces the stub rather than joining it, so the same words are not
indexed twice.
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlparse
from dataclasses import dataclass

from markdownify import markdownify


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
            room = MAX_CHARS - len(buffer) - 2
            if room > 0:
                parts.append(f"{buffer}\n\n{paragraph[:room]}")
                paragraph = paragraph[max(0, room - OVERLAP_CHARS) :]
            else:
                parts.append(buffer)
        while len(paragraph) > MAX_CHARS:
            parts.append(paragraph[:MAX_CHARS])
            paragraph = paragraph[MAX_CHARS - OVERLAP_CHARS :]
        buffer = paragraph
    if buffer:
        parts.append(buffer)
    return parts


def chunks_for_row(row: sqlite3.Row, *, skip_article: bool = False) -> list[Chunk]:
    """Build every chunk a single bookmark contributes.

    Set skip_article once the full article text has been extracted: the stub
    is the first paragraph of it, and indexing both buries the real thing.
    """
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

    if row["article_title"] and not skip_article:
        preview = row["article_preview"] or ""
        add("article", f"{row['article_title']}\n\n{preview}".strip())

    return out


def chunks_for_links(rows: list[sqlite3.Row], *, covered: set[str] | None = None) -> list[Chunk]:
    """Link cards carry a title and description worth indexing on their own.

    A card whose page was extracted is dropped: the card text is a summary of
    the page, and the page is already in the index.
    """
    out: list[Chunk] = []
    for row in rows:
        if covered and row["url"] in covered:
            continue
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


# Extracted bodies are HTML, because the links and images inside them are what
# the later layers need. Embeddings want prose, so the markup comes off here.
BLANK_RUN = re.compile(r"\n{3,}")

# A markdown table row: | a | b | c |
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# What is left of a row once the pipes, digits, signs, and rules come off.
TABLE_NOISE = re.compile(r"[\d\s|:.,%+\-—–]+")


def to_text(html: str) -> str:
    """Flatten extracted HTML into prose.

    Anchors and images are stripped to their text: a raw URL adds no meaning to
    an embedding and crowds out the words around it.
    """
    if not html:
        return ""
    text = markdownify(html, strip=["a", "img"], heading_style="ATX")
    kept = [line.rstrip() for line in text.splitlines() if not is_number_table(line)]
    return BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()


def is_number_table(line: str) -> bool:
    """True for a table row carrying no words.

    An article that shows a matrix renders as rows of floats. Embedding those
    matches nothing and still wins the ranking for its bookmark, because the
    row is short and dense.
    """
    if not TABLE_ROW.match(line):
        return False
    return not TABLE_NOISE.sub("", line).strip()


def chunks_for_documents(rows: list[sqlite3.Row]) -> list[Chunk]:
    """Chunk the full text behind articles and links."""
    out: list[Chunk] = []
    for row in rows:
        body = to_text(row["body"])
        if not body:
            continue

        if row["kind"] in ("x_article", "video"):
            source = "article" if row["kind"] == "x_article" else "video"
            label = f"@{row['screen_name']}" if row["screen_name"] else "@unknown"
        else:
            source = "link"
            label = row["site"] or urlparse(row["url"]).netloc or "link"

        head = f"{label}: {row['title']}" if row["title"] else f"{label}:"
        for position, piece in enumerate(split_long(f"{head}\n\n{body}")):
            out.append(
                Chunk(
                    tweet_id=row["tweet_id"],
                    source=source,
                    ref=row["url"],
                    position=position,
                    text=piece,
                    lang=row["lang"],
                )
            )
    return out


def chunks_for_images(rows: list[sqlite3.Row]) -> list[Chunk]:
    """Index a photo caption. The handle rides along as it does on posts."""
    out: list[Chunk] = []
    for row in rows:
        caption = (row["text"] or "").strip()
        if not caption:
            continue
        handle = f"@{row['screen_name']}" if row["screen_name"] else "@unknown"
        for position, piece in enumerate(split_long(caption)):
            out.append(
                Chunk(
                    tweet_id=row["tweet_id"],
                    source="image",
                    ref=row["media_key"],
                    position=position,
                    text=f"{handle}: {piece}",
                    lang=row["lang"],
                )
            )
    return out
