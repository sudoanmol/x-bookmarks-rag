"""Build the chunk, vector, and full-text indexes.

Re-running is cheap. Chunks are compared by content hash, so unchanged
bookmarks are skipped and only genuinely new text is embedded.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Callable

import sqlite_vec

from . import chunk as chunk_mod
from . import db, embed

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    tweet_id    TEXT NOT NULL REFERENCES bookmarks(tweet_id),
    source      TEXT NOT NULL,
    ref         TEXT,
    position    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    source_text TEXT,
    lang        TEXT,
    hash        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_tweet  ON chunks(tweet_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
    chunk_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{embed.DIMENSIONS}]
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text, content='chunks', content_rowid='id'
);
"""

BOOKMARK_SQL = """
SELECT b.tweet_id, b.text, b.quoted_text, b.lang, b.article_title, b.article_preview,
       a.screen_name
FROM bookmarks b LEFT JOIN authors a USING(author_id)
WHERE b.removed_at IS NULL
"""

DOCUMENT_SQL = """
SELECT d.url, d.kind, d.tweet_id, d.title, d.body, d.lang, d.site,
       a.screen_name
FROM documents d
JOIN bookmarks b ON b.tweet_id = d.tweet_id
LEFT JOIN authors a USING(author_id)
WHERE b.removed_at IS NULL AND d.body != '' AND d.word_count > 0
"""

LINK_SQL = """
SELECT l.tweet_id, l.url, l.domain, l.title, l.description
FROM links l JOIN bookmarks b USING(tweet_id)
WHERE b.removed_at IS NULL AND (l.title IS NOT NULL OR l.description IS NOT NULL)
"""


def connect() -> sqlite3.Connection:
    """A database connection with the vector extension loaded."""
    conn = db.connect()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA)
    return conn


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _collect(conn: sqlite3.Connection) -> dict[str, list[chunk_mod.Chunk]]:
    by_tweet: dict[str, list[chunk_mod.Chunk]] = {}
    documents = conn.execute(DOCUMENT_SQL).fetchall()
    articled = {r["tweet_id"] for r in documents if r["kind"] == "x_article"}
    covered = {r["url"] for r in documents if r["kind"] != "x_article"}

    for row in conn.execute(BOOKMARK_SQL):
        pieces = chunk_mod.chunks_for_row(
            row, skip_article=row["tweet_id"] in articled
        )
        if pieces:
            by_tweet.setdefault(row["tweet_id"], []).extend(pieces)
    for piece in chunk_mod.chunks_for_links(
        conn.execute(LINK_SQL).fetchall(), covered=covered
    ):
        by_tweet.setdefault(piece.tweet_id, []).append(piece)
    for piece in chunk_mod.chunks_for_documents(documents):
        by_tweet.setdefault(piece.tweet_id, []).append(piece)
    return by_tweet


def build(
    conn: sqlite3.Connection,
    *,
    rebuild: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    """Sync chunks to the current bookmarks, then embed whatever is missing."""
    if rebuild:
        conn.executescript(
            "DELETE FROM chunk_vec; DELETE FROM chunks; "
            "INSERT INTO chunk_fts(chunk_fts) VALUES('delete-all');"
        )
        conn.commit()

    desired = _collect(conn)
    existing: dict[str, set[str]] = {}
    for row in conn.execute("SELECT tweet_id, hash FROM chunks"):
        existing.setdefault(row["tweet_id"], set()).add(row["hash"])

    added = removed = 0
    for index, (tweet_id, pieces) in enumerate(desired.items(), 1):
        want = {_hash(p.text): p for p in pieces}
        have = existing.get(tweet_id, set())
        if want.keys() == have:
            continue

        cur = conn.execute("SELECT id FROM chunks WHERE tweet_id = ?", (tweet_id,))
        stale = [r["id"] for r in cur.fetchall()]
        if stale:
            conn.executemany("DELETE FROM chunk_vec WHERE chunk_id = ?", [(i,) for i in stale])
            conn.execute("DELETE FROM chunks WHERE tweet_id = ?", (tweet_id,))
            removed += len(stale)

        conn.executemany(
            "INSERT INTO chunks(tweet_id, source, ref, position, text, lang, hash) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            [(p.tweet_id, p.source, p.ref, p.position, p.text, p.lang, h) for h, p in want.items()],
        )
        added += len(want)
        if on_progress and index % 50 == 0:
            on_progress("chunking", index, len(desired))

    # Bookmarks that vanished take their chunks with them.
    conn.execute(
        "DELETE FROM chunks WHERE tweet_id IN "
        "(SELECT tweet_id FROM bookmarks WHERE removed_at IS NOT NULL)"
    )
    conn.commit()

    pending = conn.execute(
        "SELECT id, text FROM chunks WHERE id NOT IN (SELECT chunk_id FROM chunk_vec)"
    ).fetchall()

    for start in range(0, len(pending), 32):
        window = pending[start : start + 32]
        vectors = embed.embed_documents([r["text"] for r in window])
        conn.executemany(
            "INSERT INTO chunk_vec(chunk_id, embedding) VALUES(?, ?)",
            [(r["id"], sqlite_vec.serialize_float32(v)) for r, v in zip(window, vectors)],
        )
        conn.commit()
        if on_progress:
            on_progress("embedding", min(start + 32, len(pending)), len(pending))

    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
    conn.commit()

    return {
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embedded": conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0],
        "added": added,
        "removed": removed,
    }
