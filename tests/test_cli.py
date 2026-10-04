"""The sync pipeline and its settings. Every stage is stubbed."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from x_bookmarks_rag import cli, config


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    return path


@pytest.fixture
def stages(monkeypatch, settings_file):
    calls = []

    def stub(name):
        def run(*args, **kwargs):
            calls.append((name, kwargs))
        return run

    for name in ("capture", "extract", "caption", "transcribe", "translate", "index"):
        monkeypatch.setattr(cli, f"_{name}", stub(name))
    return calls


def test_a_missing_settings_file_indexes_everything(settings_file):
    assert config.settings() == config.Settings()


def test_settings_reject_an_unknown_source(settings_file):
    settings_file.write_text('exclude = ["vidoes"]\n')
    with pytest.raises(ValueError, match="vidoes"):
        config.settings()


def test_sync_runs_every_stage_in_order(stages):
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert [name for name, _ in stages] == [
        "capture", "extract", "caption", "transcribe", "translate", "index"
    ]


def test_config_and_flags_both_exclude(stages, settings_file):
    settings_file.write_text('exclude = ["video"]\n')
    result = CliRunner().invoke(cli.app, ["sync", "--no-images", "--no-translate"])
    assert result.exit_code == 0, result.output
    assert [name for name, _ in stages] == ["capture", "extract", "index"]
    assert stages[-1][1] == {"exclude": frozenset({"video", "image"}), "translated": False}


def test_a_failed_stage_does_not_stop_the_index(stages, monkeypatch):
    def boom():
        raise RuntimeError("modal is not logged in")

    monkeypatch.setattr(cli, "_caption", boom)
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    assert "caption failed" in result.output
    assert [name for name, _ in stages][-1] == "index"


def test_sync_waits_for_a_batch_of_photos(monkeypatch, tmp_path):
    from x_bookmarks_rag import caption as caption_mod

    ran = []
    monkeypatch.setattr(cli.db, "connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(caption_mod, "run", lambda *a, **k: ran.append(1) or {"ok": 0, "failed": 0})
    jobs = [caption_mod.Job(str(i), "u", "t") for i in range(caption_mod.SYNC_MIN_PHOTOS - 1)]
    monkeypatch.setattr(caption_mod, "pending", lambda conn, retry_failed=False: jobs)

    cli._caption(wait_for_batch=True)
    assert ran == []
    cli._caption()
    assert ran == [1]
