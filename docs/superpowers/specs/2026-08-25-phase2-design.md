# Phase 2 — enrich, index, search

Date: 2026-08-25
Status: approved; Layer 1 in progress

## What the gate measured

Phase 1 captured 1,211 bookmarks across 62 pages, spanning May 2024 to
August 2026, from 779 authors. These numbers, not estimates, drive the design.

| Fact | Number | Consequence |
|---|---:|---|
| Total text | 443,191 chars | Embedding is trivial. Minutes, locally. |
| Long posts (`note_tweet`) | 280 | Full bodies already captured. |
| **X Articles** | **153** | 12.6% of the collection. Bodies missing. |
| Photos | 541 | Only 10 items have alt text anywhere. |
| Videos | 294 clips, 42 h | See the split below. |
| Links | 566 across 254 domains | GitHub alone is 136. |
| Needs translation | ~11 posts | Small. Applies to enrichment output too. |

### The video split decides the audio design

| Length | Clips | Runtime |
|---|---:|---:|
| Under 10 min | 249 | 4.1 h |
| Over 10 min | 45 | 37.9 h |

The short 249 fit in half of one day's free Groq quota. The long 45 hold 90% of
all runtime. So: short clips to Groq, long clips to local `mlx-whisper`
overnight, which has no quota and no rate limit. Nothing is dropped. Download is
4.5 GB using the smallest MP4 variant; ffmpeg extracts audio and discards the
video, so peak disk stays small.

## Decisions

### Extraction uses Defuddle inside Playwright

`defuddle` is bundled to a single IIFE with `bun build` and injected into a live
Playwright page. The page is fully rendered, so JavaScript sites work where a
plain fetch returns an empty shell.

The library carries site-specific extractors, which removed two custom paths
from an earlier draft of this design:

- **`x-article`** reads `[data-testid="twitterArticleRichTextView"]` and returns
  the title, author, code blocks, embedded tweets, and the article's images.
  Intercepting the Draft.js `content_state` is unnecessary.
- **`github`** detects issues and pull requests and pulls their comments. A
  hand-rolled raw-README fetch is unnecessary.

Also covered: `youtube`, `hackernews`, `substack`, `medium`, `reddit`,
`wikipedia`, `chatgpt`, `claude`, `gemini`, `grok`, `linkedin`, `arxiv` by
heuristic.

Two failures found by spiking against real bookmark URLs:

- `arxiv.org/pdf/...` starts a download instead of rendering. Rewrite `/pdf/` to
  `/abs/`, and route non-HTML content types away from the browser.
- JavaScript apps can return a near-empty result on `domcontentloaded`. Retry
  once on `networkidle` before accepting a thin extraction.

External pages get a context with **no storage state**, so they never see the X
session, and `bypass_csp=True`, because sites such as GitHub otherwise block
script injection.

### Depth-1 costs a column, not a recursion engine

Images and embedded tweets inside an article are part of the article, not a
second level. Defuddle already returns them and they join the image queue.

Links inside an article are a real second level. The `documents` table carries a
`depth` column. Article links enqueue at depth 1, deduplicated by URL against
the 566 already known. Depth is capped at 1. How many there are is unknown until
the articles are extracted, so that count becomes the next gate.

### Translation is a step, not a layer

Roughly 11 posts are non-English, but captions and transcripts will contain
languages the post's `lang` field never shows. So translation runs on chunk text
before embedding. `chunks.text` holds English and is indexed;
`chunks.source_text` holds the original for display. Groq Whisper's
`translations` endpoint returns English directly from non-English audio, so
transcription and translation collapse into one call there.

### One vector space

Every modality becomes text before indexing. Decided in the phase 1 spec; the
gate numbers reinforce it, since captions and transcripts are the only way the
843 media items become searchable at all.

## Layers

Each layer leaves a working product behind. Re-indexing after a layer is a local
operation over local data.

| Layer | Adds | Network |
|---|---|---|
| **1** | Chunk, embed, and search everything already captured | Local Ollama only |
| **2** | 153 article bodies, 566 link bodies, then the depth-1 gate | Playwright |
| **3** | 541 image captions and OCR | Groq vision |
| **4** | 294 transcripts, split Groq / local | Groq + `mlx-whisper` |
| **5** | Web UI and MCP server over the same search function | None |

## Schema additions

```sql
documents   -- extracted long-form: url, kind, depth, source_tweet_id,
            -- title, author, site, published, body, word_count, lang, error
enrichment  -- job queue: kind, ref, state, attempts, provider, error
            -- state in (pending, running, done, failed); UNIQUE(kind, ref)
media_text  -- caption, ocr, transcript, provider, lang per media_key
chunks      -- tweet_id, source, ref, position, text, source_text, lang
chunk_vec   -- vec0 virtual table, FLOAT[768]
chunk_fts   -- fts5 over chunks.text
```

`enrichment` is what makes the Groq quotas survivable. A stage that runs out of
quota leaves its rows `pending`, and tomorrow's run drains them. Pacing comes
from Groq's `x-ratelimit-remaining-*` response headers, not from limits copied
out of a docs page.

## Model routing

| Job | First choice | Fallback |
|---|---|---|
| Captions, OCR | Groq `qwen/qwen3.6-27b` | local `qwen3-vl` |
| Short transcripts | Groq `whisper-large-v3-turbo` | local `mlx-whisper` |
| Long transcripts | local `mlx-whisper` | — |
| Translation | Groq `openai/gpt-oss-20b` | local `gemma4` |
| **Embeddings** | **local `embeddinggemma`** | — |

Embeddings never leave the machine. An index tied to a remote model that can
change underneath it is an index that silently rots. The Groq vision model is
preview and can be retired, which is why every remote stage has a local twin.

## Retrieval

Vector search over `chunk_vec` and BM25 over `chunk_fts`, fused by reciprocal
rank. Chunks resolve to bookmarks, and a bookmark scores by its best chunk.
Filters on author, date, language, media kind, and domain come from the phase 1
tables. Search is one function; CLI, web UI, and MCP are three thin callers.
