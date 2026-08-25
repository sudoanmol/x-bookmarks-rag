"""Extract readable documents with Defuddle inside a real browser.

A rendered page beats a fetched one: JavaScript sites return an empty shell to
plain HTTP, and Defuddle's site-specific extractors read the DOM. The library
covers X Articles, GitHub issues and pull requests, YouTube, Hacker News,
Substack, Reddit, and Wikipedia, so this module adds no per-site code.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import db

BUNDLE = Path(__file__).parent / "vendor" / "defuddle.bundle.js"

# Below this, an extraction is treated as failed rather than stored as content.
THIN_WORDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    tweet_id    TEXT,
    title       TEXT,
    author      TEXT,
    site        TEXT,
    published   TEXT,
    body        TEXT,
    word_count  INTEGER NOT NULL DEFAULT 0,
    lang        TEXT,
    fetched_at  TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_tweet ON documents(tweet_id);
CREATE INDEX IF NOT EXISTS idx_documents_depth ON documents(depth);

CREATE TABLE IF NOT EXISTS enrichment (
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    provider   TEXT,
    error      TEXT,
    updated_at TEXT,
    PRIMARY KEY (kind, ref)
);

CREATE INDEX IF NOT EXISTS idx_enrichment_state ON enrichment(kind, state);
"""

# Defuddle runs against `document`, so the page must already be rendered.
PARSE_JS = """(url) => {
    const p = new Defuddle(document, url, { markdown: true }).parse();
    return {
        title: p.title || null,
        author: p.author || null,
        site: p.site || null,
        published: p.published || null,
        language: p.language || null,
        wordCount: p.wordCount || 0,
        content: p.content || '',
    };
}"""

TRACKING = re.compile(r"^(utm_|ref_?$|ref_src|ref_url|s|t|si|feature|__twitter)", re.I)

# X serves an article's real text inside these GraphQL operations. It is the
# authoritative copy, and cheap to capture while the page loads anyway.
ARTICLE_OPS = ("TweetResultByRestId", "TweetDetail")

# An extraction below this share of the authoritative word count is truncated.
TRUNCATION_RATIO = 0.7


@dataclass
class Document:
    url: str
    kind: str
    depth: int = 0
    tweet_id: str | None = None
    title: str | None = None
    author: str | None = None
    site: str | None = None
    published: str | None = None
    body: str = ""
    word_count: int = 0
    lang: str | None = None
    error: str | None = None


def normalize_url(url: str) -> str:
    """Make a URL fetchable and comparable.

    arXiv PDF links start a download instead of rendering, so they are pointed
    at the abstract page. Tracking parameters are dropped so the same article
    saved twice is one document.
    """
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path

    if host == "arxiv.org" and path.startswith("/pdf/"):
        path = "/abs/" + path.removeprefix("/pdf/").removesuffix(".pdf")

    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and not TRACKING.match(pair.split("=", 1)[0])
    ]
    return urlunparse((parts.scheme or "https", host, path, "", "&".join(kept), ""))


def looks_like_a_file(url: str) -> bool:
    """Binary targets make the browser download instead of render."""
    return bool(re.search(r"\.(pdf|zip|tar|gz|mp4|mp3|png|jpe?g|gif|webp|csv|xlsx?)$", urlparse(url).path, re.I))


def find_content_state(node) -> dict | None:
    """Locate `content_state` anywhere in a GraphQL payload."""
    if isinstance(node, dict):
        state = node.get("content_state")
        if isinstance(state, dict):
            return state
        for value in node.values():
            if (found := find_content_state(value)) is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            if (found := find_content_state(value)) is not None:
                return found
    return None


def article_text(payload: dict) -> str:
    state = find_content_state(payload)
    blocks = (state or {}).get("blocks") or []
    return "\n".join(block.get("text", "") for block in blocks).strip()


def connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.executescript(SCHEMA)
    return conn


def extract_one(page, url: str, *, kind: str, tweet_id: str | None = None, depth: int = 0) -> Document:
    """Render a URL and pull the readable document out of it."""
    doc = Document(url=url, kind=kind, depth=depth, tweet_id=tweet_id)

    if looks_like_a_file(url):
        doc.error = "binary target, not a page"
        return doc

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_200)
        page.add_script_tag(path=str(BUNDLE))
        result = page.evaluate(PARSE_JS, url)

        # A JavaScript app can still be empty at domcontentloaded. Give it one
        # more chance to settle before calling the extraction a failure.
        if (result.get("wordCount") or 0) < THIN_WORDS:
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            page.add_script_tag(path=str(BUNDLE))
            retry = page.evaluate(PARSE_JS, url)
            if (retry.get("wordCount") or 0) > (result.get("wordCount") or 0):
                result = retry

        doc.title = result.get("title")
        doc.author = result.get("author")
        doc.site = result.get("site")
        doc.published = result.get("published")
        doc.lang = result.get("language")
        doc.body = result.get("content") or ""
        doc.word_count = result.get("wordCount") or 0

        if doc.word_count < THIN_WORDS:
            doc.error = f"thin extraction ({doc.word_count} words)"
    except Exception as exc:
        doc.error = str(exc).splitlines()[0][:200]

    return doc


def extract_x_article(page, url: str, tweet_id: str) -> Document:
    """Extract an X Article.

    Never scroll. The article view mounts and unmounts surrounding page
    elements as you move, so scrolling makes the extraction worse rather than
    fuller. The GraphQL copy of the text is captured in parallel and used to
    prove the rendered extraction was not truncated.
    """
    import json

    doc = Document(url=url, kind="x_article", tweet_id=tweet_id)
    responses: list = []
    handler = lambda r: (
        responses.append(r)
        if "/i/api/graphql/" in r.url
        and r.url.split("?")[0].rstrip("/").split("/")[-1] in ARTICLE_OPS
        else None
    )
    page.on("response", handler)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3_500)
        page.add_script_tag(path=str(BUNDLE))
        result = page.evaluate(PARSE_JS, url)

        doc.title = result.get("title")
        doc.author = result.get("author")
        doc.site = result.get("site") or "X (Twitter)"
        doc.body = result.get("content") or ""
        doc.word_count = result.get("wordCount") or 0

        truth = ""
        for response in responses:
            try:
                candidate = article_text(json.loads(response.text()))
            except Exception:
                continue
            if len(candidate) > len(truth):
                truth = candidate

        if truth:
            expected = len(truth.split())
            if doc.word_count < expected * TRUNCATION_RATIO:
                # Keep the authoritative text rather than a partial render.
                doc.body = truth
                doc.word_count = expected
                doc.error = f"rendered extraction was short; used GraphQL text ({expected} words)"
        elif doc.word_count < THIN_WORDS:
            doc.error = f"thin extraction ({doc.word_count} words)"
    except Exception as exc:
        doc.error = str(exc).splitlines()[0][:200]
    finally:
        page.remove_listener("response", handler)

    return doc


def save(conn: sqlite3.Connection, doc: Document) -> None:
    conn.execute(
        """
        INSERT INTO documents(url, kind, depth, tweet_id, title, author, site,
                              published, body, word_count, lang, fetched_at, error)
        VALUES(:url, :kind, :depth, :tweet_id, :title, :author, :site,
               :published, :body, :word_count, :lang, :now, :error)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title, author = excluded.author, site = excluded.site,
            published = excluded.published, body = excluded.body,
            word_count = excluded.word_count, lang = excluded.lang,
            fetched_at = excluded.fetched_at, error = excluded.error
        """,
        {**doc.__dict__, "now": db.now_iso()},
    )
    conn.commit()
