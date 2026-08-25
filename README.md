# x-bookmarks-rag

Search your X bookmarks in natural language. No X API, no paid services.

Phase 1 is built: capture every bookmark into SQLite and measure what is
actually in there. Phase 2 adds enrichment, embeddings, and search.

## Why it does not scrape HTML

The X web app fetches your bookmarks from an internal GraphQL endpoint. A real
browser runs your session, and this tool listens to the network responses. That
gives full post text, media URLs, video bitrate variants, link cards, quoted
posts, and language codes, in clean JSON.

Two problems disappear as a result:

- **Nothing is missed.** The timeline is virtualized, so a DOM scraper loses
  rows that scroll past before it reads them. Cursor paging cannot skip a page.
- **Nothing gets signed.** X signs each API call with an
  `x-client-transaction-id` header built by obfuscated page JavaScript. The
  browser makes its own requests, so this tool never forges a header.

## Install

```sh
uv sync
uv run playwright install chromium
cp .env.example .env      # phase 2 only; phase 1 needs no key
```

## Use

```sh
uv run xbm login          # opens a browser, you sign in once
uv run xbm sync           # capture new bookmarks, then load them
uv run xbm inspect        # report what was captured
```

Later runs fetch only what is new:

```sh
uv run xbm sync           # incremental, stops at the last watermark
uv run xbm sync --full    # full walk; also detects un-bookmarked posts
uv run xbm normalize      # rebuild the database from raw pages on disk
uv run xbm status         # session and sync state
```

## How a sync works

```
  x.com/i/bookmarks ──(headless Chrome, saved session)──┐
                                                        │ intercept
                          GraphQL Bookmarks responses ──┘
                                     │
   1. CAPTURE      data/raw/<run>/page-NNNN.json     append-only
                                     │
   2. NORMALIZE    SQLite: bookmarks · authors · media · links
                                     │
   3. INSPECT      the report that decides phase 2
```

Capture is the only stage that touches X. It writes raw responses to disk
untouched, so a parser fix, a schema change, or a better embedding model costs
a local re-run instead of another scroll through your account.

## Incremental sync

X orders bookmarks by `sortIndex`, which tracks **when you bookmarked**
something rather than when it was written. After a clean run the highest value
is stored as a watermark. The next run pages from the top, stops when it reaches
that value, and reads one extra page for safety.

The watermark advances only when a run reaches a real end. A crash or a stall
leaves it alone, and the raw pages are already saved, so nothing is lost.

An incremental run cannot see what you **un**bookmarked. `sync --full` re-walks
everything and sets `removed_at` on posts X no longer returns. That is a soft
delete: enrichment output stays, so un-bookmarking never throws away paid work.

## Session

`xbm login` opens a visible browser once and saves a Playwright storage state to
`~/.config/x-bookmarks/state.json` with mode `0600`. It lives outside the
repository on purpose, because a `.gitignore` line is one `git add -f` away from
committing a live login.

Chrome's own cookie database is deliberately not read. Those cookies sit behind
Keychain encryption, the format shifts between Chrome releases, and a running
Chrome locks the profile.

When the session expires, X redirects to the login flow and the tool stops with
an error. It does not capture zero bookmarks and report success.

## Terms of service

X forbids automated collection. This reads your own bookmarks, from your own
session, on your own machine, and paces itself between scrolls. The account risk
is real and yours to weigh.

## Status

| Stage | State |
|---|---|
| Capture, normalize, inspect | Built |
| Enrich: captions, OCR, transcripts, articles, translation | Phase 2 |
| Embed and index | Phase 2 |
| Search: CLI, web UI, MCP server | Phase 2 |

Phase 2 gets designed against the numbers `xbm inspect` reports, not guesses.
