"""Turn raw Bookmarks GraphQL pages into normalized records.

This module never touches the network. It reads the JSON that capture wrote to
disk, so it can be re-run after any parser fix without scrolling X again.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlparse, urlunparse

TWEET_TYPES = {"Tweet", "TweetWithVisibilityResults"}

TRACKING = re.compile(r"^(utm_|ref_?$|ref_src|ref_url|s|t|si|feature|__twitter)", re.I)


def normalize_url(url: str) -> str:
    """Make a URL fetchable and comparable.

    arXiv PDF links start a download instead of rendering, so they are pointed
    at the abstract page. Tracking parameters are dropped so the same article
    saved twice is one document.
    """
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path

    if host == "arxiv.org" and path.startswith("/pdf/"):
        path = "/abs/" + path.removeprefix("/pdf/").removesuffix(".pdf")

    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and not TRACKING.match(pair.split("=", 1)[0])
    ]
    return urlunparse((parts.scheme or "https", host, path, "", "&".join(kept), ""))


# --------------------------------------------------------------------------
# navigation helpers
# --------------------------------------------------------------------------


def dig(obj: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts without raising on a missing key."""
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
        if obj is None:
            return default
    return obj


def iter_entries(page: dict) -> Iterator[dict]:
    """Yield every timeline entry in a Bookmarks response."""
    instructions = dig(page, "data", "bookmark_timeline_v2", "timeline", "instructions")
    if not isinstance(instructions, list):
        return
    for instruction in instructions:
        for entry in instruction.get("entries") or []:
            yield entry


def bottom_cursor(page: dict) -> str | None:
    """The cursor X wants for the next page, or None at the end."""
    for entry in iter_entries(page):
        content = entry.get("content") or {}
        if content.get("cursorType") == "Bottom":
            return content.get("value")
    return None


def unwrap_tweet(result: dict | None) -> dict | None:
    """TweetWithVisibilityResults hides the real tweet one level down."""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    return result if result.get("__typename") in TWEET_TYPES or "legacy" in result else None


# --------------------------------------------------------------------------
# field extraction
# --------------------------------------------------------------------------


def parse_created_at(value: str | None) -> str | None:
    """X sends 'Wed Oct 10 20:19:24 +0000 2018'. Store ISO 8601 instead."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return None


def extract_author(tweet: dict) -> dict:
    """Read the author. X moved these fields out of `legacy` during 2025, so
    check the new location first and fall back to the old one."""
    user = dig(tweet, "core", "user_results", "result") or {}
    legacy = user.get("legacy") or {}
    core = user.get("core") or {}
    return {
        "author_id": user.get("rest_id"),
        "screen_name": core.get("screen_name") or legacy.get("screen_name"),
        "name": core.get("name") or legacy.get("name"),
        "avatar_url": dig(user, "avatar", "image_url")
        or legacy.get("profile_image_url_https"),
        "verified": int(bool(user.get("is_blue_verified") or legacy.get("verified"))),
        "description": legacy.get("description"),
    }


def extract_text(tweet: dict) -> tuple[str, bool]:
    """Return the fullest available text and whether it came from note_tweet.

    note_tweet holds long-form posts. legacy.full_text truncates them.
    """
    note = dig(tweet, "note_tweet", "note_tweet_results", "result")
    legacy = tweet.get("legacy") or {}

    if note and note.get("text"):
        text = note["text"]
        entities = note.get("entity_set") or {}
        is_long = True
    else:
        text = legacy.get("full_text") or ""
        entities = legacy.get("entities") or {}
        is_long = False

    # Swap t.co shorteners for the real destination.
    for url_entity in entities.get("urls") or []:
        short = url_entity.get("url")
        expanded = url_entity.get("expanded_url")
        if short and expanded:
            text = text.replace(short, expanded)

    # Media t.co links carry no information. Drop them.
    for media in (legacy.get("extended_entities") or {}).get("media") or []:
        short = media.get("url")
        if short:
            text = text.replace(short, "").rstrip()

    return text.strip(), is_long


def video_variants(video_info: dict) -> tuple[str | None, int | None, str | None, int | None]:
    """Return the best and the smallest MP4, as (url, bitrate) pairs.

    Enrichment wants speech, not pixels. A 44-minute clip is 3.3 GB at the top
    bitrate and 81 MB at the bottom one, and the audio track is the same. HLS
    variants carry no bitrate and need a player, so they are ignored.
    """
    variants = [
        v
        for v in (video_info.get("variants") or [])
        if v.get("content_type") == "video/mp4" and v.get("url")
    ]
    if not variants:
        return None, None, None, None
    ranked = sorted(variants, key=lambda v: v.get("bitrate") or 0)
    best, small = ranked[-1], ranked[0]
    return best["url"], best.get("bitrate"), small["url"], small.get("bitrate")


def extract_media(tweet: dict) -> list[dict]:
    tweet_id = tweet.get("rest_id")
    legacy = tweet.get("legacy") or {}
    items: list[dict] = []

    for position, media in enumerate((legacy.get("extended_entities") or {}).get("media") or []):
        kind = media.get("type")
        thumb = media.get("media_url_https")
        url, bitrate, duration = thumb, None, None

        small_url, small_bitrate = None, None
        if kind in ("video", "animated_gif"):
            info = media.get("video_info") or {}
            url, bitrate, small_url, small_bitrate = video_variants(info)
            duration = info.get("duration_millis")
            if not url:
                continue

        items.append(
            {
                "media_key": media.get("media_key") or f"{tweet_id}-{position}",
                "tweet_id": tweet_id,
                "kind": kind,
                "url": url,
                "thumb_url": thumb,
                "alt_text": media.get("ext_alt_text"),
                "width": dig(media, "original_info", "width"),
                "height": dig(media, "original_info", "height"),
                "duration_ms": duration,
                "bitrate": bitrate,
                "small_url": small_url,
                "small_bitrate": small_bitrate,
                "position": position,
            }
        )
    return items


def _card_values(tweet: dict) -> dict[str, Any]:
    """Flatten a link preview card into a plain dict."""
    values = {}
    for binding in dig(tweet, "card", "legacy", "binding_values", default=[]) or []:
        key, value = binding.get("key"), binding.get("value") or {}
        if not key:
            continue
        values[key] = value.get("string_value") or dig(value, "image_value", "url")
    return values


def _blank_link(tweet_id: str, url: str) -> dict:
    # Stored under the same key as its documents row, so the two join directly.
    url = normalize_url(url)
    return {
        "tweet_id": tweet_id,
        "url": url,
        "domain": urlparse(url).netloc.lower().removeprefix("www."),
        "title": None,
        "description": None,
        "thumb_url": None,
        "from_card": 0,
    }


def extract_links(tweet: dict) -> list[dict]:
    tweet_id = tweet.get("rest_id")
    legacy = tweet.get("legacy") or {}
    note_entities = dig(tweet, "note_tweet", "note_tweet_results", "result", "entity_set") or {}

    links: dict[str, dict] = {}
    # A card names its target by the t.co short link, so keep the mapping back
    # to the real destination instead of guessing which link the card describes.
    resolve: dict[str, str] = {}

    for entities in (legacy.get("entities") or {}, note_entities):
        for url_entity in entities.get("urls") or []:
            expanded = url_entity.get("expanded_url")
            if not expanded or "//x.com/" in expanded or "//twitter.com/" in expanded:
                continue
            links[expanded] = _blank_link(tweet_id, expanded)
            short = url_entity.get("url")
            if short:
                resolve[short] = expanded

    card = _card_values(tweet)
    card_url = card.get("card_url") or dig(tweet, "card", "legacy", "url")
    if card and card_url and not card_url.startswith("card://"):
        target = resolve.get(card_url, card_url)
        entry = links.setdefault(target, _blank_link(tweet_id, target))
        entry["title"] = card.get("title") or entry["title"]
        entry["description"] = card.get("description") or entry["description"]
        entry["thumb_url"] = (
            card.get("thumbnail_image_large")
            or card.get("thumbnail_image")
            or card.get("photo_image_full_size_large")
            or entry["thumb_url"]
        )
        entry["from_card"] = 1

    return list(links.values())


# --------------------------------------------------------------------------
# entry -> records
# --------------------------------------------------------------------------


def extract_article(tweet: dict) -> dict:
    """Read the X Article stub.

    The Bookmarks timeline carries the title, a short preview, and an id, but
    not `content_state`, which holds the body. Fetching the body needs a second
    visit to the post; `article_body` stays NULL until then.
    """
    article = dig(tweet, "article", "article_results", "result")
    if not article:
        return {"article_id": None, "article_title": None, "article_preview": None}
    return {
        "article_id": article.get("rest_id") or article.get("id"),
        "article_title": article.get("title"),
        "article_preview": article.get("preview_text"),
    }


def parse_entry(entry: dict) -> dict | None:
    """Convert one timeline entry into author / bookmark / media / link records.

    Returns None for cursors, ads, and anything that is not a readable post.
    """
    if dig(entry, "content", "itemContent", "itemType") != "TimelineTweet":
        return None

    tweet = unwrap_tweet(dig(entry, "content", "itemContent", "tweet_results", "result"))
    if not tweet or not tweet.get("rest_id"):
        return None

    legacy = tweet.get("legacy") or {}
    author = extract_author(tweet)
    text, is_long = extract_text(tweet)

    quoted = unwrap_tweet(dig(tweet, "quoted_status_result", "result"))
    quoted_text = None
    if quoted:
        quoted_body, _ = extract_text(quoted)
        quoted_author = extract_author(quoted).get("screen_name") or "unknown"
        quoted_text = f"@{quoted_author}: {quoted_body}".strip()

    screen_name = author.get("screen_name") or "i"
    tweet_id = tweet["rest_id"]

    bookmark = {
        "tweet_id": tweet_id,
        "sort_index": entry.get("sortIndex") or "0",
        "author_id": author.get("author_id"),
        "text": text,
        "is_long": int(is_long),
        "lang": legacy.get("lang"),
        "created_at": parse_created_at(legacy.get("created_at")),
        "favorite_count": legacy.get("favorite_count") or 0,
        "retweet_count": legacy.get("retweet_count") or 0,
        "reply_count": legacy.get("reply_count") or 0,
        "quote_count": legacy.get("quote_count") or 0,
        "bookmark_count": legacy.get("bookmark_count") or 0,
        "quoted_tweet_id": quoted.get("rest_id") if quoted else None,
        "quoted_text": quoted_text,
        **extract_article(tweet),
        "conversation_id": legacy.get("conversation_id_str"),
        "url": f"https://x.com/{screen_name}/status/{tweet_id}",
    }

    media = extract_media(tweet)
    links = extract_links(tweet)
    if quoted:
        media.extend(extract_media(quoted))
        links.extend(extract_links(quoted))
    for item in media:
        item["tweet_id"] = tweet_id
    for item in links:
        item["tweet_id"] = tweet_id

    return {"author": author, "bookmark": bookmark, "media": media, "links": links}


def count_tweet_entries(page: dict) -> int:
    return sum(1 for entry in iter_entries(page) if parse_entry(entry))
