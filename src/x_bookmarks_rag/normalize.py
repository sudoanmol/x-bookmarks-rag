"""Load raw GraphQL pages into SQLite.

Capture is the only stage that touches X. Everything here reads local files, so
a parser fix costs a re-run rather than another scroll through your bookmarks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import config, db, parse


@dataclass
class NormalizeResult:
    pages: int = 0
    bookmarks: int = 0
    media: int = 0
    links: int = 0
    failed_pages: list[str] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)


def all_raw_pages() -> list[Path]:
    """Every captured page, oldest run first."""
    return sorted(config.RAW_DIR.glob("*/page-*.json"))


def normalize(paths: Iterable[Path] | None = None) -> NormalizeResult:
    pages = list(paths) if paths is not None else all_raw_pages()
    result = NormalizeResult()
    conn = db.connect()

    try:
        for path in pages:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result.failed_pages.append(str(path))
                continue

            result.pages += 1
            for entry in parse.iter_entries(payload):
                record = parse.parse_entry(entry)
                if not record:
                    continue

                tweet_id = record["bookmark"]["tweet_id"]
                db.upsert_author(conn, record["author"])
                db.upsert_bookmark(conn, record["bookmark"])
                db.replace_media(conn, tweet_id, record["media"])
                db.replace_links(conn, tweet_id, record["links"])

                result.seen_ids.add(tweet_id)
                result.bookmarks += 1
                result.media += len(record["media"])
                result.links += len(record["links"])

            conn.commit()
    finally:
        conn.close()

    return result
