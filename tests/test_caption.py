"""Caption helpers. The Modal captioner is stubbed so these stay offline."""

from __future__ import annotations

import json

import pytest

from x_bookmarks_rag import caption as caption_mod
from x_bookmarks_rag import chunk as chunk_mod
from x_bookmarks_rag import config, db
from x_bookmarks_rag import normalize as normalize_mod

from . import fixtures


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "data" / "media")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "bookmarks.db")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "cfg" / "state.json")
    config.ensure_dirs()
    run = config.RAW_DIR / "run1"
    run.mkdir(parents=True)
    (run / "page-0001.json").write_text(json.dumps(fixtures.page()))
    normalize_mod.normalize()
    return tmp_path


# --------------------------------------------------------------------------
# Caption text
# --------------------------------------------------------------------------


def test_ocr_and_image_line_are_kept():
    raw = (
        "Every npm package release\n"
        "> npx @better-npm/cli\n"
        "Image: a terminal screenshot of an .npmrc diff"
    )
    assert caption_mod.parse_caption(raw) == raw


def test_an_image_with_no_text_keeps_only_the_image_line():
    assert caption_mod.parse_caption("Image: a cat on a windowsill") == (
        "Image: a cat on a windowsill"
    )


def test_thinking_tags_are_stripped():
    raw = "<think>looks like a chart</think>\nImage: a line chart"
    assert caption_mod.parse_caption(raw) == "Image: a line chart"


def test_blank_output_stays_empty():
    assert caption_mod.parse_caption("  \n  ") == ""


def test_consecutive_duplicate_lines_collapse():
    raw = "EVGA\n" * 80 + "Image: a wall of GPU boxes"
    assert caption_mod.parse_caption(raw) == "EVGA\nImage: a wall of GPU boxes"


# --------------------------------------------------------------------------
# Jobs, save, run
# --------------------------------------------------------------------------


def test_pending_is_photos_only(workspace):
    conn = db.connect()
    jobs = caption_mod.pending(conn)
    conn.close()
    assert [j.media_key for j in jobs] == ["m1"]
    assert jobs[0].url == "https://pbs.twimg.com/media/one.jpg"


def test_a_successful_caption_is_not_pending(workspace):
    conn = db.connect()
    caption_mod.save(
        conn,
        caption_mod.Caption(media_key="m1", text="Image: a chart", model="x"),
    )
    assert caption_mod.pending(conn) == []
    conn.close()


def test_a_failed_caption_retries_only_when_asked(workspace):
    conn = db.connect()
    caption_mod.save(
        conn,
        caption_mod.Caption(media_key="m1", error="429"),
    )
    assert [j.media_key for j in caption_mod.pending(conn)] == ["m1"]
    caption_mod.save(
        conn,
        caption_mod.Caption(media_key="m1", error="429"),
    )
    assert caption_mod.pending(conn) == []
    assert [j.media_key for j in caption_mod.pending(conn, retry_failed=True)] == ["m1"]
    conn.close()


def stub(results):
    """A captioner that answers every batch from a fixed list."""
    def captioner(batches):
        for urls in batches:
            assert urls == ["https://pbs.twimg.com/media/one.jpg"]
            yield results
    return captioner


def test_run_commits_one_caption(workspace):
    conn = db.connect()
    body = "OCR line\nImage: a chart"
    tally = caption_mod.run(
        conn, caption_mod.pending(conn), captioner=stub([{"text": body}])
    )
    row = conn.execute("SELECT text, model, error, attempts FROM captions").fetchone()
    conn.close()
    assert tally == {"ok": 1, "failed": 0}
    assert row["text"] == body
    assert row["model"] == caption_mod.MODEL
    assert row["error"] is None
    assert row["attempts"] == 1


def test_run_records_a_fetch_error(workspace):
    conn = db.connect()
    tally = caption_mod.run(
        conn, caption_mod.pending(conn), captioner=stub([{"error": "fetch: 404"}])
    )
    row = conn.execute("SELECT text, error FROM captions").fetchone()
    conn.close()
    assert tally == {"ok": 0, "failed": 1}
    assert row["error"] == "fetch: 404"


def test_run_marks_an_empty_caption_failed(workspace):
    conn = db.connect()
    caption_mod.run(
        conn, caption_mod.pending(conn), captioner=stub([{"text": "<think>x</think>"}])
    )
    row = conn.execute("SELECT error FROM captions").fetchone()
    conn.close()
    assert row["error"] == "empty caption"


def test_normalize_does_not_wipe_captions(workspace):
    conn = db.connect()
    caption_mod.save(
        conn,
        caption_mod.Caption(media_key="m1", text="keep me", model="x"),
    )
    conn.close()
    normalize_mod.normalize()
    conn = db.connect()
    assert conn.execute("SELECT text FROM captions WHERE media_key='m1'").fetchone()[0] == (
        "keep me"
    )
    conn.close()


def test_enrichment_is_gone_and_captions_exist(workspace):
    conn = db.connect()
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "captions" in names
    assert "enrichment" not in names


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_image_chunk_carries_the_handle_and_the_caption():
    rows = [
        {
            "tweet_id": "1001",
            "media_key": "m1",
            "text": "Image: a line chart of loss",
            "screen_name": "alice",
            "lang": "en",
        }
    ]
    pieces = chunk_mod.chunks_for_images(rows)
    assert len(pieces) == 1
    assert pieces[0].source == "image"
    assert pieces[0].ref == "m1"
    assert pieces[0].text == "@alice: Image: a line chart of loss"


def test_empty_captions_are_not_chunked():
    rows = [
        {
            "tweet_id": "1",
            "media_key": "m",
            "text": "",
            "screen_name": "alice",
            "lang": "en",
        }
    ]
    assert chunk_mod.chunks_for_images(rows) == []


def test_a_long_caption_is_split():
    rows = [
        {
            "tweet_id": "1",
            "media_key": "m",
            "text": "word " * (chunk_mod.MAX_CHARS),
            "screen_name": "bob",
            "lang": "en",
        }
    ]
    pieces = chunk_mod.chunks_for_images(rows)
    assert len(pieces) > 1
    assert all(p.source == "image" and p.ref == "m" for p in pieces)
    assert all(p.text.startswith("@bob:") for p in pieces)
