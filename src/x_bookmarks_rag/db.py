"""SQLite schema and write helpers.

Every write is an upsert keyed on a stable X identifier, so re-normalizing the
raw pages any number of times gives the same database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    author_id   TEXT PRIMARY KEY,
    screen_name TEXT,
    name        TEXT,
    avatar_url  TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    description TEXT
);

CREATE TABLE IF NOT EXISTS bookmarks (
    tweet_id        TEXT PRIMARY KEY,
    sort_index      TEXT NOT NULL,
    author_id       TEXT REFERENCES authors(author_id),
    text            TEXT NOT NULL,
    is_long         INTEGER NOT NULL DEFAULT 0,
    lang            TEXT,
    created_at      TEXT,
    favorite_count  INTEGER NOT NULL DEFAULT 0,
    retweet_count   INTEGER NOT NULL DEFAULT 0,
    reply_count     INTEGER NOT NULL DEFAULT 0,
    quote_count     INTEGER NOT NULL DEFAULT 0,
    bookmark_count  INTEGER NOT NULL DEFAULT 0,
    quoted_tweet_id TEXT,
    quoted_text     TEXT,
    conversation_id TEXT,
    url             TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    removed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_sort  ON bookmarks(sort_index DESC);
CREATE INDEX IF NOT EXISTS idx_bookmarks_alive ON bookmarks(removed_at);

CREATE TABLE IF NOT EXISTS media (
    media_key   TEXT PRIMARY KEY,
    tweet_id    TEXT NOT NULL REFERENCES bookmarks(tweet_id),
    kind        TEXT NOT NULL,
    url         TEXT NOT NULL,
    thumb_url   TEXT,
    alt_text    TEXT,
    width       INTEGER,
    height      INTEGER,
    duration_ms INTEGER,
    bitrate     INTEGER,
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_media_tweet ON media(tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_kind  ON media(kind);

CREATE TABLE IF NOT EXISTS links (
    tweet_id    TEXT NOT NULL REFERENCES bookmarks(tweet_id),
    url         TEXT NOT NULL,
    domain      TEXT,
    title       TEXT,
    description TEXT,
    thumb_url   TEXT,
    from_card   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tweet_id, url)
);

CREATE INDEX IF NOT EXISTS idx_links_domain ON links(domain);

CREATE TABLE IF NOT EXISTS raw_pages (
    path        TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    entry_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# sync state
# --------------------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection) -> int | None:
    """Highest sortIndex from the last clean run, or None on a first run."""
    raw = get_state(conn, "watermark")
    return int(raw) if raw else None


def set_watermark(conn: sqlite3.Connection, value: int) -> None:
    set_state(conn, "watermark", str(value))


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def upsert_author(conn: sqlite3.Connection, author: dict) -> None:
    if not author.get("author_id"):
        return
    conn.execute(
        """
        INSERT INTO authors(author_id, screen_name, name, avatar_url, verified, description)
        VALUES(:author_id, :screen_name, :name, :avatar_url, :verified, :description)
        ON CONFLICT(author_id) DO UPDATE SET
            screen_name = excluded.screen_name,
            name        = excluded.name,
            avatar_url  = excluded.avatar_url,
            verified    = excluded.verified,
            description = excluded.description
        """,
        author,
    )


def upsert_bookmark(conn: sqlite3.Connection, bm: dict) -> None:
    """Insert or refresh a bookmark. captured_at is preserved on update."""
    conn.execute(
        """
        INSERT INTO bookmarks(
            tweet_id, sort_index, author_id, text, is_long, lang, created_at,
            favorite_count, retweet_count, reply_count, quote_count, bookmark_count,
            quoted_tweet_id, quoted_text, conversation_id, url,
            captured_at, last_seen_at, removed_at
        ) VALUES(
            :tweet_id, :sort_index, :author_id, :text, :is_long, :lang, :created_at,
            :favorite_count, :retweet_count, :reply_count, :quote_count, :bookmark_count,
            :quoted_tweet_id, :quoted_text, :conversation_id, :url,
            :now, :now, NULL
        )
        ON CONFLICT(tweet_id) DO UPDATE SET
            sort_index      = excluded.sort_index,
            author_id       = excluded.author_id,
            text            = excluded.text,
            is_long         = excluded.is_long,
            lang            = excluded.lang,
            created_at      = excluded.created_at,
            favorite_count  = excluded.favorite_count,
            retweet_count   = excluded.retweet_count,
            reply_count     = excluded.reply_count,
            quote_count     = excluded.quote_count,
            bookmark_count  = excluded.bookmark_count,
            quoted_tweet_id = excluded.quoted_tweet_id,
            quoted_text     = excluded.quoted_text,
            conversation_id = excluded.conversation_id,
            url             = excluded.url,
            last_seen_at    = excluded.last_seen_at,
            removed_at      = NULL
        """,
        {**bm, "now": now_iso()},
    )


def replace_media(conn: sqlite3.Connection, tweet_id: str, items: Iterable[dict]) -> None:
    conn.execute("DELETE FROM media WHERE tweet_id = ?", (tweet_id,))
    conn.executemany(
        """
        INSERT INTO media(media_key, tweet_id, kind, url, thumb_url, alt_text,
                          width, height, duration_ms, bitrate, position)
        VALUES(:media_key, :tweet_id, :kind, :url, :thumb_url, :alt_text,
               :width, :height, :duration_ms, :bitrate, :position)
        ON CONFLICT(media_key) DO NOTHING
        """,
        list(items),
    )


def replace_links(conn: sqlite3.Connection, tweet_id: str, items: Iterable[dict]) -> None:
    conn.execute("DELETE FROM links WHERE tweet_id = ?", (tweet_id,))
    conn.executemany(
        """
        INSERT INTO links(tweet_id, url, domain, title, description, thumb_url, from_card)
        VALUES(:tweet_id, :url, :domain, :title, :description, :thumb_url, :from_card)
        ON CONFLICT(tweet_id, url) DO UPDATE SET
            title       = COALESCE(excluded.title, links.title),
            description = COALESCE(excluded.description, links.description),
            thumb_url   = COALESCE(excluded.thumb_url, links.thumb_url),
            from_card   = MAX(links.from_card, excluded.from_card)
        """,
        list(items),
    )


def record_page(conn: sqlite3.Connection, path: str, run_id: str, entry_count: int) -> None:
    conn.execute(
        "INSERT INTO raw_pages(path, run_id, fetched_at, entry_count) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET entry_count = excluded.entry_count",
        (path, run_id, now_iso(), entry_count),
    )


def mark_removed(conn: sqlite3.Connection, seen_ids: set[str]) -> int:
    """After a full walk, soft-delete bookmarks that X no longer returns.

    Enrichment output is kept, so un-bookmarking never throws away paid work.
    """
    stamp = now_iso()
    cur = conn.execute("SELECT tweet_id FROM bookmarks WHERE removed_at IS NULL")
    missing = [r["tweet_id"] for r in cur.fetchall() if r["tweet_id"] not in seen_ids]
    conn.executemany(
        "UPDATE bookmarks SET removed_at = ? WHERE tweet_id = ?",
        [(stamp, tid) for tid in missing],
    )
    conn.commit()
    return len(missing)
