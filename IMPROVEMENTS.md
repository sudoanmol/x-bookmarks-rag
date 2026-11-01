# Improvements: agent UX and hit quality

Proposals only. Nothing here is built.

The biggest agent problem is response size: a default search costs about 9,600
tokens. The biggest quality problems are that 71% of document chunks have no
title, and keyword search has no stemming.

## How this was measured

Measured on 2026-10-31, on the index as it was before that day's sync finished:
5,099 chunks, with 132 of 1,483 live bookmarks not yet indexed. Token counts use
4 characters per token.

The 10 sample queries were ad hoc. They show cost and data shape, not relevance,
because there is no labelled test set yet.

## Build a test set first

Write about 50 real queries with known right answers, and score them by R@5 and
MRR, like the old embedding benchmark. Without this, the hit quality changes
below cannot be shown to help.

## Agent UX

| # | Change | Evidence | Effort |
| --- | --- | --- | --- |
| 1 | **Return short snippets.** Send about 800 characters around the match instead of the whole chunk. | Default search returns about 38,500 characters (max 48,300), and chunks run up to 4,800. Snippets could cut this to roughly 3,000 tokens (an estimate). | Small |
| 2 | **Let a passage lead to its neighbours.** Add a way to read chunks around a position. | Passages return a chunk `position`, but `read_document` pages by character `offset`, so an agent cannot use one to find the other. | Small |
| 3 | **Add browsing without a query**, such as `list_bookmarks` by author, date, or source. | `query` is required, so "what's new from this author" has to be faked with a search. | Small |
| 4 | **Store when each bookmark was saved.** | Date filters use the post date, so "what did I save last week" cannot be answered. `sort_index` gives the order, but it does not decode to a usable date: it gave 2025 for 2026 posts. | Medium |
| 5 | **Say how each hit matched:** keyword, meaning, or both. | `score` is a raw fusion number with no meaning to an agent, while an exact-match signal tells it how far to trust a hit. | Small |
| 6 | **Fix the stale error text.** | `mcp_server.py:161` and `:356` still say `uv run xbm index`. | Tiny |

## Hit quality

| # | Change | Evidence | Effort |
| --- | --- | --- | --- |
| 7 | **Add stemming to keyword search** with the FTS5 Porter tokenizer. | Searching "caching" misses 249 of the 357 chunks that contain "cache" or "caching". For "agents", it misses 589 of 1,697. Only the keyword index needs rebuilding. | Tiny |
| 8 | **Put the title on every chunk**, and use the EmbeddingGemma title slot. | 2,052 of 2,906 document chunks have no title, because only the first piece gets one. `embed.py` always sends `title: none`. Re-embedding takes about 3 minutes locally and is free. | Small |
| 9 | **Add the post's text to its linked-page chunks.** | The post says why the owner saved it and the page says what it is, but page chunks do not include the post. Test this separately from #8. | Small |
| 10 | **Try smaller chunks**, about 2,000 characters. | Long pages are cut at 4,800, and large chunks can blur the match. This would also shrink responses. Keep it only if the test set improves. | Small |
| 11 | **Fix the failing sources.** | 10 of 21 YouTube documents and 10 Google Docs failed extraction. YouTube captions and the Docs export URL could cover these. | Medium |
| 12 | **Caption video keyframes and GIFs.** | 194 clips have no speech, so nothing inside them is searchable. 27 of those bookmarks also have under 80 characters of post text, so they are nearly unfindable. This costs GPU time. | Medium |
| 13 | **Try a local reranker.** | It could help the top few results, but it adds a dependency and latency. Add it only if the test set shows a gap the other changes do not close. | Medium |

## New features

The owner picked these two. Neither is built.

### Scheduled sync

A `launchd` job runs `xbm sync` nightly, so the index stays fresh without anyone
thinking about it. It must notify the owner when the X session expires, because
otherwise the job fails with nobody watching. Before the 2026-10-31 sync, the
last sync was about four weeks old, and 132 new bookmarks had piled up.

### Thread capture

People often bookmark the first post of a thread, and the substance is further
down. Measure the gap first: how much `parse.py` picks up today has not been
checked.

## What to skip

- **Larger candidate pool.** The 80 + 80 candidates already give 58 to 98
  distinct bookmarks, and the default limit is 10.
- **Stopword removal.** On the one query tested, all 10 top keyword hits still
  contained a real search term, despite words like "what" and "the".
- **Boilerplate cleanup.** A rough pattern search flagged 70 chunks, but both
  samples read were real content.
- **Duplicate shared pages.** Only 25 pages are shared across bookmarks, so this
  is minor.
- **Query rewriting or a built-in answer layer.** The calling agent already does
  this job.

## Recommended order

1. **Quick fixes.** Do #6 and #7, which are nearly free.
2. **Test set.** Build it and record a baseline score.
3. **Chunk context.** Do #8 and #9, scoring each one separately.
4. **Agent UX.** Do #1, #2, and #5, which are the biggest day-to-day gain for
   agents.
5. **Later, if the data supports it.** Do #3 and #4 whenever browsing is wanted.
   Do #10 to #13 only where the test set shows they help.
