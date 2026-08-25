# x-bookmarks-rag — design

Date: 2026-08-25
Status: phase 1 built; phase 2 blocked on the inspect report

## Goal

Search a personal X bookmark collection in natural language, across text,
images, video, and linked articles. No X API. No paid services.

## Constraints

- Roughly 500 to 2,000 bookmarks.
- Full enrichment: vision captions, OCR, audio transcripts, article extraction.
- Non-English posts must be detected and translated to English.
- Inference runs on Groq's free tier, with local fallback.
- Interfaces: CLI, local web UI, and an MCP server.
- Runs on an Apple M3 Pro with 18 GB of memory.

## Decision: one vector space, not several

Every modality is converted to text before indexing: post text, image caption,
OCR output, video transcript, article body. One embedding model, one vector
index, one BM25 index beside it.

Rejected alternatives:

- **A vector space per modality** (CLIP or SigLIP for images beside a text
  model). Buys visual similarity search. Costs a second model, a second index,
  and score calibration between spaces that are not comparable. The query type
  it enables is rare in this corpus.
- **Late interaction over rendered screenshots** (ColPali or ColQwen). Strong on
  visually dense pages, weak on links and plain text, heavy on disk. Wrong shape
  for a corpus that is mostly text and links.

Choosing one space does not close the door. Adding an image vector column to the
chunk table later is additive, not a rewrite.

## Decision: capture is separated from everything downstream

Capture is the only stage that touches X, the only stage with account risk, and
the only stage that cannot be repeated cheaply. It writes raw GraphQL responses
to disk untouched. Normalize, enrich, and index all replay from local files.

A parser fix, a chunking change, or a better embedding model must never cost
another scroll through the account.

## Decision: drive the UI, do not replay cursors

X signs each API call with an `x-client-transaction-id` header computed by
obfuscated page JavaScript. Replaying cursors directly would be faster but
requires reproducing that signature, which changes without notice.

Capture is a one-time job followed by small updates, so speed is worth little
and correctness is worth a lot. The browser pages for itself; this tool listens.

The GraphQL query ID in `/i/api/graphql/{queryId}/Bookmarks` changes with every
frontend deploy. Only the trailing operation name is matched.

## Phase 1 — built

### Modules

| Module | Responsibility |
|---|---|
| `config` | Paths, URLs, pacing, environment |
| `session` | Interactive login, saved storage state, expiry detection |
| `capture` | Scroll loop, response interception, raw page writes |
| `parse` | GraphQL entry to normalized records; no network |
| `db` | Schema and idempotent upserts |
| `normalize` | Raw pages to SQLite |
| `inspect` | The measurement report |
| `cli` | `login`, `sync`, `normalize`, `inspect`, `status` |

### Schema

`authors`, `bookmarks`, `media`, `links`, `raw_pages`, `sync_state`.

`bookmarks.lang` carries X's language code and drives the phase 2 translation
stage. `media.duration_ms` and `media.bitrate` come down inside the GraphQL
payload, so video runtime and download size are known before any download.

### Incremental sync

`sortIndex` orders by bookmark time, not post time. The highest value from a
clean run is the watermark. The next run stops at it and reads one extra page.

The watermark advances only on `stop_reason` of `end_of_list` or `watermark`.
A stall leaves it in place.

`sync --full` re-walks and soft-deletes vanished posts by setting `removed_at`.
Enrichment output survives, so un-bookmarking does not discard paid work.

### Parser cases covered by tests

- `TweetWithVisibilityResults` wrapping the real post one level down.
- Author fields in both the pre-2025 `legacy` location and the current one.
- `note_tweet` long-form text beating truncated `full_text`.
- t.co expansion, and dropping media t.co links that carry no information.
- Highest-bitrate MP4 chosen over HLS variants.
- Link cards resolved through their t.co short link rather than guessed.
- Cursor and non-post entries skipped.

## Phase 2 — designed after the report

The inspect report decides these open questions:

1. **Video.** Runtime, clip count, and download size against Groq's free 28,800
   audio-seconds per day. Transcribe everything, transcribe on demand, or keep
   keyframe captions only.
2. **Links.** Domain spread decides how much article extraction is worth and how
   many domains will simply refuse.
3. **Translation.** The non-English count sizes the stage. Groq Whisper's
   `translations` endpoint returns English directly from non-English audio, and
   the vision model can be told to caption in English regardless of source.

Planned shape, subject to the numbers:

- Enrichment as a SQLite job queue with `pending`, `running`, `done`, `failed`
  states and an attempt count, paced by Groq's `x-ratelimit-remaining-*`
  response headers rather than hardcoded limits.
- Vision and transcription behind an interface with two implementations. Groq
  first (`qwen/qwen3.6-27b` is preview and can be retired), local `qwen3-vl` and
  `mlx-whisper` as fallback.
- Embeddings always local through Ollama, so the index never depends on a remote
  model that can change underneath it.
- Hybrid retrieval: vector search and BM25 fused by reciprocal rank, then
  reranked.
