"""Chunking, indexing, and search. Embeddings are stubbed so these stay offline."""

from __future__ import annotations

import hashlib
import json

import pytest

from x_bookmarks_rag import chunk as chunk_mod
from x_bookmarks_rag import config, embed
from x_bookmarks_rag import index as index_mod
from x_bookmarks_rag import normalize as normalize_mod
from x_bookmarks_rag import search as search_mod

from . import fixtures


def fake_vector(text: str) -> list[float]:
    """Deterministic pseudo-embedding: same text always gives the same vector."""
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] + i) % 255) / 255.0 for i in range(embed.DIMENSIONS)]


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "data" / "media")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "bookmarks.db")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "cfg" / "state.json")
    monkeypatch.setattr(embed, "embed_documents", lambda texts, batch=32: [fake_vector(t) for t in texts])
    monkeypatch.setattr(embed, "embed_query", lambda text: fake_vector(text))
    monkeypatch.setattr(index_mod.embed, "embed_documents", lambda texts, batch=32: [fake_vector(t) for t in texts])
    monkeypatch.setattr(search_mod.embed, "embed_query", lambda text: fake_vector(text))
    config.ensure_dirs()

    run = config.RAW_DIR / "run1"
    run.mkdir(parents=True)
    (run / "page-0001.json").write_text(json.dumps(fixtures.page()))
    normalize_mod.normalize()
    return index_mod.connect()


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_short_text_stays_one_chunk():
    assert chunk_mod.split_long("hello") == ["hello"]


def test_long_text_splits_and_every_piece_fits():
    text = "\n\n".join("paragraph " + "x" * 400 for _ in range(40))
    pieces = chunk_mod.split_long(text)
    assert len(pieces) > 1
    assert all(len(p) <= chunk_mod.MAX_CHARS for p in pieces)


def test_a_single_unbroken_paragraph_is_still_split():
    pieces = chunk_mod.split_long("y" * (chunk_mod.MAX_CHARS * 3))
    assert len(pieces) >= 3
    assert all(len(p) <= chunk_mod.MAX_CHARS for p in pieces)


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------


def test_every_source_kind_is_chunked(indexed):
    index_mod.build(indexed)
    sources = {r["source"] for r in indexed.execute("SELECT DISTINCT source FROM chunks")}
    assert {"post", "quote", "article", "link"} <= sources


def test_author_handle_rides_along_for_who_said_what(indexed):
    index_mod.build(indexed)
    row = indexed.execute("SELECT text FROM chunks WHERE tweet_id='1001' AND source='post'").fetchone()
    assert row["text"].startswith("@alice:")


def test_every_chunk_gets_embedded(indexed):
    stats = index_mod.build(indexed)
    assert stats["chunks"] == stats["embedded"] > 0


def test_reindexing_embeds_nothing_new(indexed):
    first = index_mod.build(indexed)
    second = index_mod.build(indexed)
    assert second["added"] == 0 and second["removed"] == 0
    assert second["chunks"] == first["chunks"]


def test_removed_bookmarks_lose_their_chunks(indexed):
    index_mod.build(indexed)
    before = indexed.execute("SELECT COUNT(*) FROM chunks WHERE tweet_id='1001'").fetchone()[0]
    assert before > 0
    indexed.execute("UPDATE bookmarks SET removed_at='now' WHERE tweet_id='1001'")
    indexed.commit()
    index_mod.build(indexed)
    assert indexed.execute("SELECT COUNT(*) FROM chunks WHERE tweet_id='1001'").fetchone()[0] == 0


def test_edited_text_replaces_its_old_chunks(indexed):
    index_mod.build(indexed)
    indexed.execute("UPDATE bookmarks SET text='something completely different' WHERE tweet_id='1001'")
    indexed.commit()
    index_mod.build(indexed)
    texts = [r["text"] for r in indexed.execute("SELECT text FROM chunks WHERE tweet_id='1001' AND source='post'")]
    assert texts == ["@alice: something completely different"]


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ['NOT AND OR', 'quote " unbalanced', 'parens ( ) *', 'a-b c:d e^f', '', '   '],
)
def test_fts_query_never_produces_broken_syntax(indexed, raw):
    """User input must not reach FTS5 raw; these all used to be crashes."""
    index_mod.build(indexed)
    search_mod.search(indexed, raw)  # must not raise


def test_search_finds_a_post_by_its_own_words(indexed):
    index_mod.build(indexed)
    hits = search_mod.search(indexed, "chart worth keeping", limit=5)
    assert "1001" in {h.tweet_id for h in hits}


def test_author_filter_restricts_results(indexed):
    index_mod.build(indexed)
    hits = search_mod.search(indexed, "chart", limit=10, author="carol")
    assert all(h.author == "@carol" for h in hits)


def test_source_filter_restricts_results(indexed):
    index_mod.build(indexed)
    hits = search_mod.search(indexed, "paper abstract", limit=10, source="link")
    assert all(h.best_source == "link" for h in hits)


def test_a_bookmark_appears_once_however_many_chunks_match(indexed):
    index_mod.build(indexed)
    hits = search_mod.search(indexed, "the", limit=20)
    ids = [h.tweet_id for h in hits]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# Extracted documents replace the stubs they came from.
# --------------------------------------------------------------------------


def test_html_becomes_prose_without_urls():
    from x_bookmarks_rag import chunk as chunk_mod

    html = '<main><p>Read <a href="https://example.com/x">the guide</a> now.</p>'
    text = chunk_mod.to_text(html)
    assert "the guide" in text
    assert "example.com" not in text


def test_blank_runs_collapse():
    from x_bookmarks_rag import chunk as chunk_mod

    assert "\n\n\n" not in chunk_mod.to_text("<p>a</p><br><br><br><p>b</p>")


def test_article_stub_is_dropped_once_the_body_exists(indexed):
    from x_bookmarks_rag import chunk as chunk_mod

    row = indexed.execute(
        "SELECT b.tweet_id, b.text, b.quoted_text, b.lang, b.article_title,"
        " b.article_preview, a.screen_name FROM bookmarks b"
        " LEFT JOIN authors a USING(author_id) WHERE b.article_title IS NOT NULL"
    ).fetchone()
    assert row is not None
    with_stub = chunk_mod.chunks_for_row(row)
    without = chunk_mod.chunks_for_row(row, skip_article=True)
    assert any(c.source == "article" for c in with_stub)
    assert not any(c.source == "article" for c in without)


def test_a_card_is_dropped_when_its_page_was_extracted():
    from x_bookmarks_rag import chunk as chunk_mod

    rows = [
        {"tweet_id": "1", "url": "https://www.example.com/a?utm_source=x",
         "domain": "example.com", "title": "T", "description": "D"}
    ]
    assert chunk_mod.chunks_for_links(rows) != []
    # The document was stored under the normalized URL.
    assert chunk_mod.chunks_for_links(rows, covered={"https://example.com/a"}) == []


@pytest.mark.parametrize(
    "line, dropped",
    [
        ("| 0.13 | -0.51 | -0.63 |", True),
        ("|  |  |  |", True),
        ("| --- | --- | --- |", True),
        ("| 12% | +3 | -0.4 |", True),
        ("| Model | R@1 | MRR |", False),
        ("| gpt | 0.5 | 0.6 |", False),
        ("Just prose, 0.13 and -0.51.", False),
    ],
)
def test_only_wordless_table_rows_are_dropped(line, dropped):
    from x_bookmarks_rag import chunk as chunk_mod

    assert chunk_mod.is_number_table(line) is dropped


def test_a_matrix_leaves_the_prose_around_it_intact():
    from x_bookmarks_rag import chunk as chunk_mod

    html = "<p>We multiply:</p><table><tr><td>0.13</td><td>-0.51</td></tr></table><p>But then</p>"
    text = chunk_mod.to_text(html)
    assert "We multiply" in text and "But then" in text
    assert "0.13" not in text
