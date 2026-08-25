"""End-to-end test of normalize, watermark, soft delete, and the report.

Only capture touches the network, so everything below runs offline.
"""

from __future__ import annotations

import json

import pytest

from x_bookmarks_rag import config, db, inspect as inspect_mod, normalize as normalize_mod

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

    run_dir = config.RAW_DIR / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "page-0001.json").write_text(json.dumps(fixtures.page()))
    return tmp_path


def test_normalize_writes_every_table(workspace):
    result = normalize_mod.normalize()
    assert (result.pages, result.bookmarks) == (1, 4)
    assert result.media == 2
    assert result.seen_ids == {"1001", "1002", "1003", "1004"}

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM media WHERE kind='video'").fetchone()[0] == 1
    conn.close()


def test_normalize_is_idempotent(workspace):
    normalize_mod.normalize()
    normalize_mod.normalize()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
    conn.close()


def test_captured_at_survives_a_reload(workspace):
    normalize_mod.normalize()
    conn = db.connect()
    first = conn.execute("SELECT captured_at FROM bookmarks WHERE tweet_id='1001'").fetchone()[0]
    conn.close()

    normalize_mod.normalize()
    conn = db.connect()
    assert conn.execute("SELECT captured_at FROM bookmarks WHERE tweet_id='1001'").fetchone()[0] == first
    conn.close()


def test_watermark_round_trips(workspace):
    conn = db.connect()
    assert db.get_watermark(conn) is None
    db.set_watermark(conn, 1800000000000000003)
    assert db.get_watermark(conn) == 1800000000000000003
    conn.close()


def test_soft_delete_hides_but_keeps_the_row(workspace):
    normalize_mod.normalize()
    conn = db.connect()
    removed = db.mark_removed(conn, {"1001", "1002"})
    assert removed == 2
    assert conn.execute("SELECT COUNT(*) FROM bookmarks WHERE removed_at IS NULL").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0] == 4
    conn.close()


def test_re_bookmarking_clears_the_removal(workspace):
    normalize_mod.normalize()
    conn = db.connect()
    db.mark_removed(conn, set())
    conn.close()

    normalize_mod.normalize()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM bookmarks WHERE removed_at IS NULL").fetchone()[0] == 4
    conn.close()


def test_report_counts_the_things_phase_two_needs(workspace):
    normalize_mod.normalize()
    conn = db.connect()
    stats = inspect_mod.gather(conn)
    conn.close()

    assert stats["total"] == 4
    assert stats["long_posts"] == 1
    assert stats["with_quote"] == 1
    assert stats["video_count"] == 1
    assert stats["video_seconds"] == 92.0
    assert stats["non_english"] == 1          # the Japanese post
    assert stats["link_total"] == 2
    assert stats["with_alt"] == 1
    assert stats["articles"] == 1
    # the report estimates the download we would actually make: smallest variant
    assert round(stats["video_bytes"]) == round(832_000 * 92 / 8)
    assert round(stats["video_bytes_best"]) == round(2_176_000 * 92 / 8)


def test_report_renders_without_error(workspace):
    normalize_mod.normalize()
    conn = db.connect()
    stats = inspect_mod.gather(conn)
    conn.close()
    inspect_mod.render(stats)
