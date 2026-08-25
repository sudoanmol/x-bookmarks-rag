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
    attempts    INTEGER NOT NULL DEFAULT 0,
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
                              published, body, word_count, lang, fetched_at,
                              attempts, error)
        VALUES(:url, :kind, :depth, :tweet_id, :title, :author, :site,
               :published, :body, :word_count, :lang, :now, 1, :error)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title, author = excluded.author, site = excluded.site,
            published = excluded.published, body = excluded.body,
            word_count = excluded.word_count, lang = excluded.lang,
            fetched_at = excluded.fetched_at, error = excluded.error,
            attempts = documents.attempts + 1
        """,
        {**doc.__dict__, "now": db.now_iso()},
    )
    conn.commit()


# --------------------------------------------------------------------------
# Batch run
# --------------------------------------------------------------------------

# Two failures are enough to call a URL dead. Most are 404s and paywalls, and
# a third attempt costs 30 seconds to learn the same thing.
MAX_ATTEMPTS = 2

ARTICLE_SQL = """
SELECT b.tweet_id, a.screen_name AS handle
  FROM bookmarks b JOIN authors a ON a.author_id = b.author_id
 WHERE b.article_id IS NOT NULL AND b.removed_at IS NULL
 ORDER BY b.sort_index DESC
"""

LINK_SQL = """
SELECT l.url, MIN(l.tweet_id) AS tweet_id
  FROM links l JOIN bookmarks b ON b.tweet_id = l.tweet_id
 WHERE b.removed_at IS NULL
 GROUP BY l.url
"""


@dataclass
class Job:
    url: str
    kind: str
    tweet_id: str | None = None


def pending(conn: sqlite3.Connection, *, retry_failed: bool = False) -> list[Job]:
    """List the documents still worth fetching.

    A URL that succeeded is never refetched. A URL that failed is retried until
    it runs out of attempts, so an interrupted run resumes where it stopped.
    """
    done: dict[str, int] = {}
    for row in conn.execute("SELECT url, error, attempts FROM documents"):
        done[row["url"]] = -1 if not row["error"] else row["attempts"]

    def wanted(url: str) -> bool:
        attempts = done.get(url)
        if attempts is None:
            return True
        if attempts < 0:
            return False
        return retry_failed or attempts < MAX_ATTEMPTS

    jobs: list[Job] = []
    for row in conn.execute(ARTICLE_SQL):
        url = f"https://x.com/{row['handle']}/status/{row['tweet_id']}"
        if wanted(url):
            jobs.append(Job(url, "x_article", row["tweet_id"]))

    seen: set[str] = set()
    for row in conn.execute(LINK_SQL):
        url = normalize_url(row["url"])
        if url in seen:
            continue
        seen.add(url)
        if wanted(url):
            jobs.append(Job(url, "link", row["tweet_id"]))

    return jobs


# Fetching is almost all waiting on the network, so several browsers pay off.
# Four is where the gain flattened on this machine.
WORKERS = 4


def run(conn: sqlite3.Connection, jobs: list[Job], *, on_progress=None) -> dict[str, int]:
    """Fetch every job across a few browsers.

    Articles use the X session. Links get a context with no storage state, so
    an external site never sees the login.
    """
    import threading

    lock = threading.Lock()
    tally = {"ok": 0, "failed": 0}
    done = [0]
    total = len(jobs)

    # Articles first: they are the reason the session exists, and they fail
    # fast if it has expired.
    jobs = sorted(jobs, key=lambda j: j.kind != "x_article")
    lanes = [jobs[i::WORKERS] for i in range(WORKERS)]

    def report(doc: Document) -> None:
        with lock:
            tally["failed" if doc.error and not doc.body else "ok"] += 1
            done[0] += 1
            if on_progress:
                on_progress(done[0], total, doc)

    threads = [
        threading.Thread(target=_worker, args=(lane, report), daemon=True)
        for lane in lanes
        if lane
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return tally


def _worker(jobs: list[Job], report) -> None:
    """One browser, one database connection, one slice of the work.

    Playwright's sync API is bound to the thread that created it, so each
    worker builds its own from scratch.
    """
    from playwright.sync_api import sync_playwright

    from . import config

    conn = connect()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        contexts: dict[bool, object] = {}
        try:
            for job in jobs:
                needs_session = job.kind == "x_article"
                if needs_session not in contexts:
                    contexts[needs_session] = browser.new_context(
                        bypass_csp=True,
                        **(
                            {"storage_state": str(config.STATE_PATH)}
                            if needs_session
                            else {}
                        ),
                    )
                page = contexts[needs_session].new_page()
                try:
                    if needs_session:
                        doc = extract_x_article(page, job.url, job.tweet_id)
                    else:
                        doc = extract_one(
                            page, job.url, kind=job.kind, tweet_id=job.tweet_id
                        )
                finally:
                    page.close()

                save(conn, doc)
                report(doc)
        finally:
            browser.close()
    conn.close()
