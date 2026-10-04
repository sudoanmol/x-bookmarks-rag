# x-bookmarks-rag

Natural-language search over the owner's X (Twitter) bookmarks. It captures
bookmarks with a real browser, extracts the full text behind articles and
links, embeds everything locally, and searches with hybrid retrieval.

No X API. Everything runs on this machine, except two remote services:
Groq on its free tier, and Modal (owner's free credits) for GPU captioning.

**This file is the handoff.** Read all of it before you change anything. Many
rules below cost hours to learn. The reasons are given so you can tell when a
rule stops applying.

---

## 1. Rules you must not break

These come from the owner. They override your defaults.

| Rule | Why |
| --- | --- |
| Use `uv` for everything: `uv run`, `uv add`, `uv venv`. | Project standard. |
| Add dependencies with `uv add`. Never hand-edit `pyproject.toml`. | Keeps the lock file correct. |
| Use `trash`, never `rm`. | A hook blocks `rm`. |
| Never pass `-c user.email` or `-c user.name` to git. | The global git config is already correct. The owner had to clean up commits once because of this. |
| Never commit secrets. `.env` holds `GROQ_API_KEY`. | |
| Push only when the owner asks. The repo is local and has never been pushed. | |
| Do not add backward compatibility, fallbacks, or migrations. Remove the old path. | Owner's rule. See §7 for how schema changes are done here. |
| Do not build speculative abstractions. Build the simplest thing that fully meets the requirement. | Owner's rule: "refuse to solve problems we don't have". |
| Measure before you design. Look at the real data first. | Every good decision in this repo came from doing this. Every mistake came from skipping it. |

The owner writes and expects **ASD-STE100 Simplified Technical English**: short
sentences, active voice, one meaning per word.

### The session file

`~/.config/x-bookmarks/state.json` (mode 0600) holds a **live X login**. It sits
outside the repo on purpose.

- Never copy it into the repo.
- Never print its contents.
- Only X Article pages may use it. Every external page gets a browser context
  with **no** storage state, so a third-party site never sees the login.

---

## 2. Commands

```bash
uv run xbm login      # opens a browser; the owner signs in by hand
uv run xbm sync       # capture new bookmarks since the watermark
uv run xbm status     # session and sync state
uv run xbm inspect    # report what the captured data contains
uv run xbm normalize  # rebuild the DB from raw pages on disk
uv run xbm extract    # fetch full text behind articles and links
uv run xbm caption    # vision model on a Modal GPU (--limit, --retry)
uv run xbm index      # chunk and embed  (--rebuild to redo everything)
uv run xbm search "..."  # hybrid search  (-n, --author, --source)

uv run pytest -q      # 103 tests, all offline, ~1s
```

Ollama must be running, with `embeddinggemma` pulled. That is the only model
the project needs.

---

## 3. Architecture

Data flows one way. Each stage only reads what the stage before it wrote.

```
X GraphQL  ->  capture  ->  raw pages (JSON on disk)
                              |
                           parse + normalize
                              |
                           bookmarks / authors / media / links
                              |
                    extract (Playwright + Defuddle)  ->  documents
                    caption (Modal + vLLM)           ->  captions
                              |
                           chunk  ->  embed  ->  chunks + chunk_vec + chunk_fts
                              |
                           search (RRF over vector + BM25)
```

| Module | Job |
| --- | --- |
| `config.py` | Paths, URLs, pacing. Loads `.env` by **explicit path** (see §8). |
| `session.py` | Saves and loads the browser storage state. |
| `capture.py` | Drives the bookmarks page and intercepts GraphQL responses. |
| `parse.py` | Turns a GraphQL page into rows. Network-free, so it is fully testable. |
| `normalize.py` | Rebuilds the DB from raw pages. |
| `db.py` | Schema and idempotent upserts. Owns **all** tables. |
| `extract.py` | Renders pages and pulls readable text with Defuddle. |
| `caption.py` | Modal app (vLLM on one H100) plus the local job and save logic. |
| `chunk.py` | Turns rows, documents, and captions into embeddable pieces. |
| `embed.py` | Ollama calls. Asymmetric prefixes (see §8). |
| `index.py` | Builds `chunks`, `chunk_vec`, `chunk_fts`. Owns the vec0 connection. |
| `search.py` | Hybrid retrieval with reciprocal rank fusion. |
| `mcp_server.py` | Stdio MCP tools for search, bookmark details, and document reading. |
| `inspect.py` | The gate report that drives build decisions. |
| `cli.py` | Typer entry point. |

### Two connection functions. Do not mix them.

- `db.connect()` — plain SQLite. Use for everything normal.
- `index.connect()` — **loads the sqlite-vec extension.** Any query touching
  `chunk_vec` fails with `no such module: vec0` without it.

---

## 4. Data model

All tables live in `db.py`. `data/bookmarks.db`, WAL mode, `busy_timeout=30000`.

| Table | Notes |
| --- | --- |
| `authors` | `author_id` PK, `screen_name`, `name`, `avatar_url`, `verified`, `description`. Quoted-post authors are deliberately **not** stored. |
| `bookmarks` | `tweet_id` PK, `sort_index`, `text`, `is_long`, `lang`, `quoted_text`, `article_id`, `article_title`, `article_preview`, `removed_at`. |
| `media` | `media_key` PK, `kind` (photo/video/animated_gif), `url`, `alt_text`, `duration_ms`, `bitrate`, **`small_url`/`small_bitrate`**. |
| `links` | `(tweet_id, url)` PK, `domain`, `title`, `description`, `from_card`. |
| `documents` | `url` PK, `kind` (x_article/link), `tweet_id`, `title`, `body` (**HTML**), `word_count`, `attempts`, `error`. |
| `captions` | `media_key` PK, `text`, `model`, `attempts`, `error`. No FK to `media`: `replace_media` deletes and reinserts on every normalize. |
| `chunks` | `id`, `tweet_id`, `source` (post/quote/article/link/image), `ref`, `position`, `text`, `source_text`, `lang`, `hash`. |
| `chunk_vec` | vec0 virtual table, `FLOAT[768]`. |
| `chunk_fts` | fts5 external-content table over `chunks`. |
| `sync_state` | `watermark` = highest `sort_index` seen. |
| `raw_pages` | Every captured GraphQL page. **The DB can always be rebuilt from these.** |

Two design points worth keeping:

- **Soft delete.** Un-bookmarking sets `removed_at`, so captions survive.
- **`sort_index`, not post time.** It orders by *bookmark* time, which is what
  incremental sync needs.

---

## 5. Current state (all verified, not estimated)

| Thing | Count |
| --- | --- |
Counted on 2026-10-03.

| Thing | Count |
| --- | --- |
| Bookmarks | 1,350 |
| Authors | 856 |
| Media | 949 — 606 photo, 335 video, 8 gif |
| Media with alt text | **18** |
| Links (unique) | 626 |
| Documents | 789 stored, **683 usable**. Every link has a row. |
| Extracted words | **1,217,416** |
| Chunks / vectors | 4,297 / 4,297 (608 of them image chunks) |
| Captions | **605 of 606** photos. The one failure is a 404 at X. |
| Video | 51.2 hours: 279 clips under 10 min, 56 over |

Layers 1 and 2 and image captions are done and working. Search returns real
passages from extracted pages and OCR text from images, not just post text.

The unusable documents are mostly "thin extraction" (under the word floor),
plus 10 binary targets (PDFs). Links are stored under `normalize_url()`, so
compare against `documents.url` through that function, not raw `links.url`.

---

## 6. Decisions already made. Do not re-open these.

**Embedding model: `embeddinggemma`.** Benchmarked against 4 alternatives on 55
generated queries over the real corpus.

| Model | Dims | R@1 | R@5 | MRR |
| --- | --: | --: | --: | --: |
| **embeddinggemma** | 768 | **0.564** | 0.727 | **0.642** |
| qwen3-embedding:0.6b | 1024 | 0.509 | 0.818 | 0.626 |
| bge-m3 | 1024 | 0.455 | 0.727 | 0.583 |
| nomic-embed-text | 768 | 0.436 | 0.618 | 0.536 |

At 55 queries the 95% band is about ±0.13, so **no model separates**. The
decision was made on speed: 3,437 chunks embed in about 2 minutes, where a 4B
model needs 30 to 60. The benchmark measured dense retrieval alone; the real
system also runs BM25, which narrows the gap further. The rejected models were
deleted to free 17.4 GB.

**Depth-1 crawling: none.** The owner chose this after seeing the numbers:
5,342 outbound URLs, about 2.2 hours, and roughly 7x index growth, dominated by
GitHub repository navigation and documentation sidebars. Nothing is lost —
the outbound links are still inside `documents.body`, so this can be turned on
later without re-fetching.

**No LLM answer layer.** The owner will use this mostly through MCP, where the
agent is already the model. Pre-summarizing would compress the evidence, and
the agent would then summarize the summary. Retrieval returns passages; the
caller reasons.

**One vector space** for all content types, rather than separate indexes.

**Groq for enrichment, not embeddings.** Groq serves no embedding models.
Available and relevant: `whisper-large-v3-turbo` (28,800 audio seconds per
day), `openai/gpt-oss-120b` and `-20b`.

**Captions run on Modal, not Groq.** The Groq free tier (8,000 tokens per
minute, serial) needed days for 600 photos. Modal runs the same model,
`Qwen/Qwen3.8-27B-FP8`, under vLLM on one H100: 583 photos in one run, about
15 minutes of GPU and roughly $1. The app is ephemeral (`app.run()`), so it
stops when `xbm caption` exits. **The owner pays for every GPU second: after
any Modal run, confirm `modal container list --json` prints `[]`.**

---

## 7. Hard-won lessons

Each of these cost real time. Do not rediscover them.

### Playwright and X

1. **The GraphQL query ID changes on every X frontend deploy.** Match only the
   trailing operation name (`Bookmarks`), never the full path.
2. **Never make blocking calls inside a Playwright event handler** in the sync
   API. Collect `Response` objects in the handler; read bodies in the main flow.
3. **X throttles one session across concurrent browsers.** The first parallel
   run returned 61 of 153 articles empty. Every one succeeded on a serial
   retry. Articles now use `ARTICLE_WORKERS = 1`; links keep `WORKERS = 4`,
   because they spread over hundreds of hosts.
4. **Never scroll an X Article page.** The view mounts and unmounts surrounding
   elements as you move, so scrolling makes extraction *worse* — measured at
   542 words after two scrolls and 449 after four, against 478 with no scroll.
   The GraphQL `content_state` is captured in parallel as the authoritative
   copy and proves the render was complete.
5. **`bypass_csp=True` is required.** GitHub otherwise blocks script injection.
6. **Playwright's sync API is bound to its creating thread.** Each worker
   thread must build its own `sync_playwright()`, browser, and DB connection.
7. **Rewrite arXiv `/pdf/` to `/abs/`.** A PDF URL makes the browser start a
   download instead of rendering. `looks_like_a_file()` guards the rest.
8. **JavaScript apps can be empty at `domcontentloaded`.** Retry once on
   `networkidle` before calling an extraction thin.

### Everything else

9. **`load_dotenv()` must take an explicit path.** `find_dotenv()` walks up
   from the calling frame, which breaks for scripts run from stdin.
10. **Defuddle returns HTML, not markdown**, even with `markdown: true`. That
    is fine and deliberate: `documents.body` keeps HTML because the `<a href>`
    and `<img src>` inside are what later layers need. Markup comes off at
    chunk time in `chunk.to_text()`.
11. **Schema changes: drop and rebuild, never migrate.** `CREATE TABLE IF NOT
    EXISTS` will not add a column to an existing table, and the owner forbids
    migrations. `raw_pages` makes rebuilding safe.
12. **SQLite: `LIMIT` goes after `UNION ALL`.** Wrap each side in a subquery.
13. **embeddinggemma uses asymmetric prefixes.** Documents get
    `title: none | text: `, queries get `task: search result | query: `.
    Using the wrong one is a real handicap. See `embed.py`.
14. **Verify test expectations against real output before trusting them.** Two
    early test failures were wrong expectations, not wrong code.
15. **Cards name their target by t.co**, so resolve the short link. Guessing
    which link a card belongs to attached cards to the wrong URL.
16. **Store the smallest MP4, not the best.** Transcription wants speech, not
    pixels. A 44.5-minute clip is 3.3 GB at top bitrate and 81 MB at 256 kbps.
    Across 40 bookmarks this was 10.73 GB versus 0.27 GB, a 40x saving.
17. **Do not FK `captions` to `media`.** `replace_media` deletes and reinserts
    on every normalize. `ON DELETE CASCADE` would wipe every caption on
    `xbm sync`. `documents` has no FK for the same reason.
18. **vLLM FP8 needs a CUDA devel image.** DeepGEMM compiles kernels at
    startup and asserts on a missing CUDA toolkit. `debian_slim` has none.
    Use `nvidia/cuda:<ver>-devel`, matched to the vLLM wheel (0.30 → CUDA 13).
19. **Qwen3.8 is a hybrid Mamba model.** vLLM's default `max_num_seqs=1024`
    exceeds its Mamba cache blocks on an H100 (796). Set it to the batch size.
20. **A raise in Modal `@enter` crash-loops on the GPU** while the client
    waits. `caption.Model.load` keeps the error and fails the first call
    instead, so the run ends and the app stops.
21. **Finish the Modal `.map()` generator.** Leaving it unfinished closes it
    inside `app.run()` and raises `aclose(): asynchronous generator is already
    running`. In `zip`, put the generator first.
22. **Incremental sync stops one page after the watermark hit.** An earlier
    version only stopped when a scroll returned no pages, so it walked all 69.

---

## 8. MCP server

This is done. `mcp_server.py` uses MCP 2.0 and stdio transport. Codex has the
server registered as `x-bookmarks`.

### Why three tools, not one

Search returns **one passage per bookmark**, so a long thread cannot crowd out
a sharp short post. That hides scale:

| Chunks per bookmark | Bookmarks |
| --- | --: |
| 1 | 536 |
| 2–3 | 434 |
| 4–10 | 210 |
| 11+ | 31 |

The worst case is one bookmark whose linked Claude Code CHANGELOG is **78,729
words in 155 chunks**. A caller sees 1 of 155. It needs a way to go deeper, and
a way to know that deeper exists.

### The tools

1. **`search_bookmarks(query, limit=10, author=None, source=None)`**
   Wraps `search.search()`. Return per hit: `tweet_id`, `url`, `author`,
   `created_at`, `score`, `best_source`, `best_chunk` (the passage that
   matched), `best_ref` (the page URL it came from), `chunk_count`,
   `word_count`, `media`, `lang`.
   `chunk_count` and `word_count` are what make tool 3 discoverable.

2. **`get_bookmark(tweet_id)`**
   Full post text, quoted post, author, date, every link, every media item.
   Each media item includes `caption` when one exists. Cheap and bounded.
   Do **not** include document bodies here.

3. **`read_document(tweet_id_or_url, query=None, offset=0)`**
   The extracted page text. **This one is dangerous if done naively**:
   returning the CHANGELOG whole is roughly 100,000 tokens and would destroy
   the caller's context. So:
   - with `query`: run the same hybrid search **restricted to that document**
     and return the best passages;
   - without: page through with `offset` and return `has_more`.

Use stdio transport. Add a `xbm-mcp` entry point in `[project.scripts]`.

### Registration

Codex uses this global registration:

```bash
codex mcp add x-bookmarks -- uv run --project ~/Developer/x-bookmarks-rag xbm-mcp
```

The server was tested through an MCP stdio client. All three tools returned
structured results against the live corpus.

---

## 9. Backlog after MCP

In the owner's chosen order. Each layer must leave a working product.

1. **Image captions and OCR.** Done. `uv run xbm caption` after each sync
   captions only new photos, then `uv run xbm index`.
2. **Video transcripts.** 51.2 hours total. Split by length: 279 clips under 10
   minutes go to Groq `whisper-large-v3-turbo` (free tier gives 28,800 audio
   seconds per day, so the short set is about half of one day). The 56 long
   clips run locally with `mlx-whisper`. Use `media.small_url`, never `url`.
3. **Foreign language translation.** The owner asked for this explicitly.
   Design: `chunks.text` holds English, `chunks.source_text` holds the
   original. Both columns already exist. Note that the `zxx` language bucket is
   mostly X Articles, not foreign text — genuine translation work is about 11
   posts.
4. **Web UI.** Over the same `search.search()` function.

---

## 10. How to verify your work

- `uv run pytest -q` — 103 tests, all offline, about one second. Keep it that way.
  Tests use synthetic GraphQL fixtures in `tests/fixtures.py` and stub
  `embed.embed_documents` / `embed.embed_query` with a deterministic vector.
- Run a real query and read the passages:
  `uv run xbm search "what was the article about kv caching"`.
  A good result shows the matching passage and a `from <url>` line, not the
  post text.
- Never report a step as done without running it. The owner checks.
