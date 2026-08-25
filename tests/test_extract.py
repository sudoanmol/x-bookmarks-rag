"""Extraction helpers. Pure functions, no browser, no network."""

from __future__ import annotations

import pytest

from x_bookmarks_rag import extract


# --------------------------------------------------------------------------
# URL normalization. arXiv PDF links made the browser start a download instead
# of rendering, which is how this rule was found.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://arxiv.org/pdf/2401.00001", "https://arxiv.org/abs/2401.00001"),
        ("https://arxiv.org/pdf/2401.00001.pdf", "https://arxiv.org/abs/2401.00001"),
        ("https://arxiv.org/abs/2401.00001", "https://arxiv.org/abs/2401.00001"),
        ("https://www.example.com/post", "https://example.com/post"),
    ],
)
def test_normalize_url(raw, expected):
    assert extract.normalize_url(raw) == expected


def test_tracking_parameters_are_dropped_so_duplicates_collapse():
    a = extract.normalize_url("https://example.com/a?utm_source=x&utm_medium=y")
    b = extract.normalize_url("https://example.com/a")
    assert a == b


def test_meaningful_query_parameters_survive():
    url = extract.normalize_url("https://example.com/search?q=rust&page=2")
    assert "q=rust" in url and "page=2" in url


def test_fragments_are_dropped():
    assert "#" not in extract.normalize_url("https://example.com/a#section")


@pytest.mark.parametrize(
    "url, is_file",
    [
        ("https://example.com/paper.pdf", True),
        ("https://example.com/clip.mp4", True),
        ("https://example.com/photo.JPEG", True),
        ("https://example.com/article", False),
        ("https://example.com/a.pdf/not-really", False),
    ],
)
def test_binary_targets_are_recognized(url, is_file):
    assert extract.looks_like_a_file(url) is is_file


# --------------------------------------------------------------------------
# X Article text. content_state is the authoritative copy and proves a rendered
# extraction was not truncated.
# --------------------------------------------------------------------------


def _payload(blocks):
    return {"data": {"tweetResult": {"result": {"article": {"article_results": {
        "result": {"content_state": {"blocks": blocks}}}}}}}}


def test_content_state_is_found_however_deeply_nested():
    payload = _payload([{"text": "first"}, {"text": "second"}])
    assert extract.article_text(payload) == "first\nsecond"


def test_missing_content_state_returns_empty_not_an_error():
    assert extract.article_text({"data": {"nothing": 1}}) == ""
    assert extract.find_content_state({"a": [1, 2, {"b": None}]}) is None


def test_blocks_without_text_are_tolerated():
    assert extract.article_text(_payload([{"text": "a"}, {}, {"text": "b"}])) == "a\n\nb"


def test_content_state_survives_a_list_in_the_path():
    payload = {"data": {"items": [{"x": 1}, _payload([{"text": "deep"}])]}}
    assert extract.article_text(payload) == "deep"
