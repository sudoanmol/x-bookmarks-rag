"""MCP tool behavior with a synthetic offline index."""

from __future__ import annotations

import hashlib
import json

import anyio
import pytest

from x_bookmarks_rag import config, embed
from x_bookmarks_rag import index as index_mod
from x_bookmarks_rag import mcp_server
from x_bookmarks_rag import normalize as normalize_mod
from x_bookmarks_rag import search as search_mod

from . import fixtures


def fake_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] + i) % 255) / 255.0 for i in range(embed.DIMENSIONS)]


@pytest.fixture
def mcp_index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "data" / "media")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "bookmarks.db")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "cfg" / "state.json")
    monkeypatch.setattr(
        index_mod.embed,
        "embed_documents",
        lambda texts, batch=32: [fake_vector(text) for text in texts],
    )
    monkeypatch.setattr(search_mod.embed, "embed_query", fake_vector)
    config.ensure_dirs()

    run = config.RAW_DIR / "run1"
    run.mkdir(parents=True)
    (run / "page-0001.json").write_text(json.dumps(fixtures.page()))
    normalize_mod.normalize()

    conn = index_mod.connect()
    long_body = "<p>Needle cache method. " + "filler " * 2_500 + "</p>"
    conn.execute(
        """
        INSERT INTO documents(
            url, kind, tweet_id, title, site, body, word_count, attempts
        ) VALUES(?, 'link', '1001', 'Cache Notes', 'Example', ?, 2503, 1)
        """,
        ("https://example.com/post", long_body),
    )
    conn.commit()
    index_mod.build(conn)
    conn.close()
    return config.DB_PATH


def test_server_lists_the_three_tools():
    async def list_names():
        return {tool.name for tool in await mcp_server.server.list_tools()}

    assert anyio.run(list_names) == {
        "search_bookmarks",
        "get_bookmark",
        "read_document",
    }


def test_search_returns_the_passage_and_size(mcp_index):
    result = mcp_server.search_bookmarks("needle cache", limit=4)
    hit = next(hit for hit in result.hits if hit.tweet_id == "1001")

    assert hit.best_ref == "https://example.com/post"
    assert hit.best_source == "link"
    assert hit.chunk_count > 1
    assert hit.word_count == 2503
    assert hit.media == ["photo"]


def test_get_bookmark_returns_attachments_but_not_document_body(mcp_index):
    result = mcp_server.get_bookmark("1001")

    assert result.author.screen_name == "@alice"
    assert result.links[0].url == "https://example.com/post"
    assert result.media[0].alt_text == "a line chart"
    assert result.media[0].caption is None
    assert "Needle cache method" not in result.model_dump_json()


def test_get_bookmark_includes_the_caption(mcp_index):
    from x_bookmarks_rag import db

    conn = db.connect()
    conn.execute(
        "INSERT INTO captions(media_key, text, attempts) VALUES('m1', 'Image: a line chart', 1)"
    )
    conn.commit()
    conn.close()
    result = mcp_server.get_bookmark("1001")
    assert result.media[0].caption == "Image: a line chart"


def test_read_document_pages_plain_text(mcp_index):
    first = mcp_server.read_document("1001")

    assert first.mode == "page"
    assert first.has_more is True
    assert first.next_offset == mcp_server.PAGE_CHARS
    assert "Needle cache method" in first.text
    assert "<p>" not in first.text

    second = mcp_server.read_document("1001", offset=first.next_offset)
    assert second.mode == "page"
    assert second.offset == mcp_server.PAGE_CHARS
    assert second.has_more is False


def test_read_document_search_stays_inside_the_selected_page(mcp_index):
    result = mcp_server.read_document(
        "https://www.example.com/post?utm_source=x",
        query="needle cache",
    )

    assert result.mode == "search"
    assert result.passages
    assert {passage.ref for passage in result.passages} == {"https://example.com/post"}
    assert "Needle cache method" in result.passages[0].text


def test_mcp_call_returns_structured_content(mcp_index):
    async def call():
        return await mcp_server.server.call_tool("get_bookmark", {"tweet_id": "1001"})

    result = anyio.run(call)
    assert result.structured_content["tweet_id"] == "1001"
    assert result.structured_content["author"]["screen_name"] == "@alice"
