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
from datetime import date

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
    # The passage as written, when best_chunk is its English translation.
    best_source_text: str | None
    # Where the matched passage came from: the page URL for a link or article,
    # None when the post itself matched. A caller citing a passage needs this.
    best_ref: str | None
    lang: str | None
    media: str | None
    # How much of this bookmark the returned passage represents. A caller that
    # cannot see this has no way to tell an answer from a fragment.
    chunk_count: int
    word_count: int


@dataclass
class ChunkHit:
    chunk_id: int
    source: str
    ref: str | None
    position: int
    text: str
    source_text: str | None
    score: float


def fts_query(text: str) -> str:
    """FTS5 has its own syntax; user input must not reach it raw.

    Tokens are quoted and joined with OR so a long question still matches on
    its rarest words rather than requiring every one.
    """
    tokens = [t for t in TOKEN.findall(text) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in tokens)


def _vector_ranks(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    chunk_ids: set[int] | None = None,
) -> dict[int, int]:
    if chunk_ids is not None and not chunk_ids:
        return {}
    # SQLite turns a one-item IN into `=`, and vec0 returns no rows for an
    # equality inside a KNN query. A lone candidate ranks first anyway.
    if chunk_ids is not None and len(chunk_ids) == 1:
        return {next(iter(chunk_ids)): 0}

    vector = sqlite_vec.serialize_float32(embed.embed_query(query))
    params: list[object] = [vector, min(k, len(chunk_ids)) if chunk_ids is not None else k]
    restriction = ""
    if chunk_ids is not None:
        placeholders = ",".join("?" for _ in chunk_ids)
        restriction = f" AND chunk_id IN ({placeholders})"
        params.extend(sorted(chunk_ids))
    rows = conn.execute(
        "SELECT chunk_id FROM chunk_vec WHERE embedding MATCH ? AND k = ?"
        f"{restriction} ORDER BY distance",
        params,
    ).fetchall()
    return {row["chunk_id"]: rank for rank, row in enumerate(rows)}


def _lexical_ranks(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    chunk_ids: set[int] | None = None,
) -> dict[int, int]:
    match = fts_query(query)
    if not match or (chunk_ids is not None and not chunk_ids):
        return {}

    params: list[object] = [match]
    restriction = ""
    if chunk_ids is not None:
        placeholders = ",".join("?" for _ in chunk_ids)
        restriction = f" AND rowid IN ({placeholders})"
        params.extend(sorted(chunk_ids))
    params.append(min(k, len(chunk_ids)) if chunk_ids is not None else k)
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?"
            f"{restriction} ORDER BY rank LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["rowid"]: rank for rank, row in enumerate(rows)}


def _fuse(dense: dict[int, int], lexical: dict[int, int]) -> dict[int, float]:
    fused: dict[int, float] = {}
    for ranks in (dense, lexical):
        for chunk_id, rank in ranks.items():
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


def allowed_chunks(
    conn: sqlite3.Connection,
    *,
    author: str | None = None,
    source: str | None = None,
    after: date | None = None,
    before: date | None = None,
) -> set[int] | None:
    """The chunks a filtered search may rank, or None when nothing filters.

    Filters apply before ranking, not after: a filter applied to the top
    candidates sees only what happened to rank there, and returns too little.
    """
    clauses, params = [], []
    if author:
        clauses.append("LOWER(a.screen_name) = ?")
        params.append(author.lstrip("@").lower())
    if source:
        clauses.append("c.source = ?")
        params.append(source)
    if after:
        clauses.append("b.created_at >= ?")
        params.append(after.isoformat())
    if before:
        clauses.append("b.created_at < ?")
        params.append(before.isoformat())
    if not clauses:
        return None
    rows = conn.execute(
        f"""
        SELECT c.id FROM chunks c
        JOIN bookmarks b USING(tweet_id)
        LEFT JOIN authors a ON a.author_id = b.author_id
        WHERE b.removed_at IS NULL AND {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    return {row["id"] for row in rows}


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    author: str | None = None,
    source: str | None = None,
    after: date | None = None,
    before: date | None = None,
) -> list[Hit]:
    """`after` is inclusive and `before` exclusive, both on the post date."""
    allowed = allowed_chunks(conn, author=author, source=source, after=after, before=before)
    dense = _vector_ranks(conn, query, CANDIDATES, allowed)
    lexical = _lexical_ranks(conn, query, CANDIDATES, allowed)

    fused = _fuse(dense, lexical)
    if not fused:
        return []

    placeholders = ",".join("?" * len(fused))
    rows = conn.execute(
        f"""
        SELECT c.id, c.tweet_id, c.source, c.text AS chunk_text, c.source_text, c.ref,
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
            best_source_text=row["source_text"],
            best_ref=row["ref"],
            lang=row["lang"],
            media=row["media"],
            chunk_count=row["chunk_count"],
            word_count=row["doc_words"] or len((row["text"] or "").split()),
        )
        for score, row in ordered
    ]


def search_chunks(
    conn: sqlite3.Connection,
    query: str,
    chunk_ids: set[int],
    *,
    limit: int = 5,
) -> list[ChunkHit]:
    """Run hybrid search only over the selected chunks."""
    dense = _vector_ranks(conn, query, CANDIDATES, chunk_ids)
    lexical = _lexical_ranks(conn, query, CANDIDATES, chunk_ids)
    fused = _fuse(dense, lexical)
    if not fused:
        return []

    selected = sorted(fused, key=fused.get, reverse=True)[:limit]
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        f"SELECT id, source, ref, position, text, source_text FROM chunks WHERE id IN ({placeholders})",
        selected,
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [
        ChunkHit(
            chunk_id=chunk_id,
            source=by_id[chunk_id]["source"],
            ref=by_id[chunk_id]["ref"],
            position=by_id[chunk_id]["position"],
            text=by_id[chunk_id]["text"],
            source_text=by_id[chunk_id]["source_text"],
            score=fused[chunk_id],
        )
        for chunk_id in selected
    ]
