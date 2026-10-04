"""Transcript helpers. The Modal transcriber is stubbed so these stay offline."""

from __future__ import annotations

import json

import pytest

from x_bookmarks_rag import config, db
from x_bookmarks_rag import normalize as normalize_mod
from x_bookmarks_rag import transcribe as transcribe_mod

from . import fixtures

VIDEO_URL = "https://x.com/bob/status/1002/video/1"


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


def stub(result):
    def transcriber(jobs):
        for job in jobs:
            yield {"url": job.url, **result}
    return transcriber


@pytest.mark.parametrize("seconds, text", [(0, "0:00"), (75.9, "1:15"), (3725, "1:02:05")])
def test_clock(seconds, text):
    assert transcribe_mod.clock(seconds) == text


def test_paragraphs_break_each_minute_and_lead_with_the_time():
    segments = [(0.0, 5.0, "Hello"), (30.0, 35.0, "there."), (61.0, 70.0, "A <b> tag"), (80.0, 82.0, "")]
    assert transcribe_mod.to_html(segments) == (
        "<p>[0:00] Hello there.</p>\n<p>[1:01] A &lt;b&gt; tag</p>"
    )


def test_pending_keys_the_video_by_its_x_url_and_uses_the_small_mp4(workspace):
    conn = db.connect()
    jobs = transcribe_mod.pending(conn)
    conn.close()
    assert [(j.url, j.mp4, j.tweet_id) for j in jobs] == [
        (VIDEO_URL, "https://video.twimg.com/low.mp4", "1002")
    ]


def test_run_stores_the_transcript_as_a_video_document(workspace):
    conn = db.connect()
    segments = [(0.0, 4.0, "Short clip, few words.")]
    tally = transcribe_mod.run(
        conn, transcribe_mod.pending(conn), transcriber=stub({"segments": segments, "lang": "en"})
    )
    row = conn.execute("SELECT * FROM documents WHERE url = ?", (VIDEO_URL,)).fetchone()
    assert tally == {"ok": 1, "silent": 0, "failed": 0}
    assert row["kind"] == "video"
    assert row["title"] == "Video transcript (1:32)"
    assert row["body"] == "<p>[0:00] Short clip, few words.</p>"
    assert row["word_count"] == 4
    assert row["lang"] == "en"
    assert transcribe_mod.pending(conn) == []
    conn.close()


def test_a_silent_clip_is_done_and_not_retried(workspace):
    conn = db.connect()
    tally = transcribe_mod.run(
        conn, transcribe_mod.pending(conn), transcriber=stub({"segments": [], "lang": None})
    )
    assert tally == {"ok": 0, "silent": 1, "failed": 0}
    assert transcribe_mod.pending(conn) == []
    conn.close()


def test_a_failure_retries_only_when_asked(workspace):
    conn = db.connect()
    failing = stub({"error": "HTTPStatusError: 404"})
    transcribe_mod.run(conn, transcribe_mod.pending(conn), transcriber=failing)
    assert transcribe_mod.pending(conn) == []
    assert len(transcribe_mod.pending(conn, retry_failed=True)) == 1
    conn.close()
