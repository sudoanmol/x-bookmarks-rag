"""Parser tests. These run offline against the synthetic fixture."""

from __future__ import annotations

import pytest

from x_bookmarks_rag import parse

from . import fixtures


@pytest.fixture
def records() -> dict[str, dict]:
    page = fixtures.page()
    parsed = [r for e in parse.iter_entries(page) if (r := parse.parse_entry(e))]
    return {r["bookmark"]["tweet_id"]: r for r in parsed}


def test_cursors_and_ads_are_skipped(records):
    assert set(records) == {"1001", "1002", "1003"}


def test_bottom_cursor_is_found():
    assert parse.bottom_cursor(fixtures.page()) == "NEXT"
    assert parse.bottom_cursor(fixtures.empty_page()) is None


def test_author_reads_the_new_field_location(records):
    author = records["1001"]["author"]
    assert author["screen_name"] == "alice"
    assert author["verified"] == 1


def test_author_falls_back_to_legacy_fields(records):
    author = records["1002"]["author"]
    assert author["screen_name"] == "bob"
    assert author["verified"] == 0


def test_tco_links_are_expanded_and_media_links_dropped(records):
    text = records["1001"]["bookmark"]["text"]
    assert "https://example.com/post" in text
    assert "t.co" not in text


def test_note_tweet_beats_truncated_full_text(records):
    bookmark = records["1003"]["bookmark"]
    assert bookmark["is_long"] == 1
    assert "well past the old limit" in bookmark["text"]
    assert "https://arxiv.org/abs/2401.00001" in bookmark["text"]


def test_visibility_wrapper_is_unwrapped(records):
    assert records["1003"]["bookmark"]["tweet_id"] == "1003"


def test_quoted_post_text_is_carried(records):
    bookmark = records["1003"]["bookmark"]
    assert bookmark["quoted_tweet_id"] == "999"
    assert "@dave: The original claim" == bookmark["quoted_text"]


def test_highest_bitrate_mp4_wins_over_hls(records):
    video = records["1002"]["media"][0]
    assert video["url"] == "https://video.twimg.com/high.mp4"
    assert video["bitrate"] == 2_176_000
    assert video["duration_ms"] == 92_000
    assert video["thumb_url"] == "https://pbs.twimg.com/poster.jpg"


def test_photo_keeps_alt_text_and_size(records):
    photo = records["1001"]["media"][0]
    assert photo["kind"] == "photo"
    assert photo["alt_text"] == "a line chart"
    assert (photo["width"], photo["height"]) == (1200, 800)


def test_card_enriches_the_matching_link(records):
    links = {link["url"]: link for link in records["1003"]["links"]}
    card = links["https://arxiv.org/abs/2401.00001"]
    assert card["title"] == "A Paper"
    assert card["domain"] == "arxiv.org"
    assert card["from_card"] == 1


def test_language_is_captured_for_translation(records):
    assert records["1002"]["bookmark"]["lang"] == "ja"


def test_created_at_becomes_iso(records):
    assert records["1001"]["bookmark"]["created_at"].startswith("2018-10-10T20:19:24")


def test_url_is_built_from_the_author_handle(records):
    assert records["1001"]["bookmark"]["url"] == "https://x.com/alice/status/1001"


# --------------------------------------------------------------------------
# Cards name their target by t.co. Guessing which link a card describes was a
# real bug; these lock the resolution behaviour.
# --------------------------------------------------------------------------


def _tweet_with_card(card_url: str, url_entities: list[dict]) -> dict:
    return {
        "__typename": "Tweet",
        "rest_id": "2001",
        "core": {"user_results": {"result": {"rest_id": "u9", "core": {"screen_name": "eve", "name": "Eve"}}}},
        "legacy": {"full_text": "two links", "lang": "en", "entities": {"urls": url_entities}},
        "card": {
            "legacy": {
                "url": card_url,
                "binding_values": [{"key": "title", "value": {"string_value": "Card Title"}}],
            }
        },
    }


def test_card_resolves_through_the_tco_short_link():
    tweet = _tweet_with_card(
        "https://t.co/second",
        [
            {"url": "https://t.co/first", "expanded_url": "https://first.example/a"},
            {"url": "https://t.co/second", "expanded_url": "https://second.example/b"},
        ],
    )
    links = {link["url"]: link for link in parse.extract_links(tweet)}
    assert len(links) == 2
    assert links["https://second.example/b"]["title"] == "Card Title"
    assert links["https://first.example/a"]["title"] is None


def test_unmatched_card_becomes_its_own_link_instead_of_hijacking_one():
    tweet = _tweet_with_card(
        "https://elsewhere.example/c",
        [{"url": "https://t.co/first", "expanded_url": "https://first.example/a"}],
    )
    links = {link["url"]: link for link in parse.extract_links(tweet)}
    assert links["https://first.example/a"]["from_card"] == 0
    assert links["https://elsewhere.example/c"]["title"] == "Card Title"


def test_self_referencing_x_links_are_ignored():
    tweet = _tweet_with_card(
        "card://internal",
        [{"url": "https://t.co/q", "expanded_url": "https://x.com/alice/status/5"}],
    )
    assert parse.extract_links(tweet) == []
