"""Hybrid retrieval over the chunk index.

Vector search finds meaning, BM25 finds exact strings. Neither alone is enough:
embeddings miss handles, library names, and error codes, while BM25 misses
paraphrase. Reciprocal rank fusion combines them without needing the two score
scales to be comparable, which they are not.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

import sqlite_vec

from . import embed

# Standard reciprocal rank fusion constant. Damps the pull of any single
# ranker's top hit so one list cannot dominate the other.
RRF_K = 60
CANDIDATES = 80

TOKEN = re.compile(r"[\w']+", re.UNICODE)


@dataclass
class Hit:
    tweet_id: str
    url: str
    author: str
    created_at: str | None
    text: str
    score: float
    best_source: str
    best_chunk: str
    # Where the matched passage came from: the page URL for a link or article,
    # None when the post itself matched. A caller citing a passage needs this.
    best_ref: str | None
    lang: str | None
    media: str | None
    # How much of this bookmark the returned passage represents. A caller that
    # cannot see this has no way to tell an answer from a fragment.
    chunk_count: int
    word_count: int


def fts_query(text: str) -> str:
    """FTS5 has its own syntax; user input must not reach it raw.

    Tokens are quoted and joined with OR so a long question still matches on
    its rarest words rather than requiring every one.
    """
    tokens = [t for t in TOKEN.findall(text) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in tokens)


def _vector_ranks(conn: sqlite3.Connection, query: str, k: int) -> dict[int, int]:
    vector = sqlite_vec.serialize_float32(embed.embed_query(query))
    rows = conn.execute(
        "SELECT chunk_id FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (vector, k),
    ).fetchall()
    return {row["chunk_id"]: rank for rank, row in enumerate(rows)}


def _lexical_ranks(conn: sqlite3.Connection, query: str, k: int) -> dict[int, int]:
    match = fts_query(query)
    if not match:
        return {}
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["rowid"]: rank for rank, row in enumerate(rows)}


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    author: str | None = None,
    source: str | None = None,
) -> list[Hit]:
    dense = _vector_ranks(conn, query, CANDIDATES)
    lexical = _lexical_ranks(conn, query, CANDIDATES)

    fused: dict[int, float] = {}
    for ranks in (dense, lexical):
        for chunk_id, rank in ranks.items():
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not fused:
        return []

    placeholders = ",".join("?" * len(fused))
    rows = conn.execute(
        f"""
        SELECT c.id, c.tweet_id, c.source, c.text AS chunk_text, c.ref,
               b.url, b.text, b.created_at, b.lang,
               a.screen_name,
               (SELECT COUNT(*) FROM chunks WHERE tweet_id = b.tweet_id) AS chunk_count,
               (SELECT COALESCE(SUM(word_count), 0) FROM documents
                 WHERE tweet_id = b.tweet_id) AS doc_words,
               (SELECT GROUP_CONCAT(DISTINCT kind) FROM media WHERE tweet_id = b.tweet_id) AS media
        FROM chunks c
        JOIN bookmarks b USING(tweet_id)
        LEFT JOIN authors a ON a.author_id = b.author_id
        WHERE c.id IN ({placeholders}) AND b.removed_at IS NULL
        """,
        list(fused),
    ).fetchall()

    # A bookmark scores by its single best chunk, so a long thread does not
    # out-rank a sharper short post just by having more pieces.
    best: dict[str, tuple[float, sqlite3.Row]] = {}
    for row in rows:
        if author and (row["screen_name"] or "").lower() != author.lstrip("@").lower():
            continue
        if source and row["source"] != source:
            continue
        score = fused[row["id"]]
        if row["tweet_id"] not in best or score > best[row["tweet_id"]][0]:
            best[row["tweet_id"]] = (score, row)

    ordered = sorted(best.values(), key=lambda pair: -pair[0])[:limit]
    return [
        Hit(
            tweet_id=row["tweet_id"],
            url=row["url"],
            author=f"@{row['screen_name']}" if row["screen_name"] else "@unknown",
            created_at=row["created_at"],
            text=row["text"],
            score=score,
            best_source=row["source"],
            best_chunk=row["chunk_text"],
            best_ref=row["ref"],
            lang=row["lang"],
            media=row["media"],
            chunk_count=row["chunk_count"],
            word_count=row["doc_words"] or len((row["text"] or "").split()),
        )
        for score, row in ordered
    ]
