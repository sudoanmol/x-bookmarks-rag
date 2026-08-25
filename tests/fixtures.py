"""A synthetic Bookmarks page shaped like the real GraphQL response.

It covers the cases that break naive parsers: the TweetWithVisibilityResults
wrapper, long-form note_tweet text, the 2025 move of author fields out of
`legacy`, video variants, quoted posts, link cards, and cursor entries.
"""

from __future__ import annotations


def _user(rest_id: str, screen_name: str, *, new_shape: bool = True) -> dict:
    user: dict = {"rest_id": rest_id, "legacy": {"description": "bio"}}
    if new_shape:
        user["core"] = {"screen_name": screen_name, "name": screen_name.title()}
        user["avatar"] = {"image_url": f"https://pbs.twimg.com/{screen_name}.jpg"}
        user["is_blue_verified"] = True
    else:
        user["legacy"].update(
            {
                "screen_name": screen_name,
                "name": screen_name.title(),
                "profile_image_url_https": f"https://pbs.twimg.com/{screen_name}.jpg",
                "verified": False,
            }
        )
    return user


def _tweet(rest_id: str, user: dict, legacy: dict, **extra) -> dict:
    return {"__typename": "Tweet", "rest_id": rest_id, "core": {"user_results": {"result": user}}, "legacy": legacy, **extra}


def _entry(sort_index: str, tweet_result: dict) -> dict:
    return {
        "entryId": f"tweet-{tweet_result.get('rest_id') or tweet_result['tweet']['rest_id']}",
        "sortIndex": sort_index,
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": tweet_result}},
        },
    }


PHOTO_TWEET = _tweet(
    "1001",
    _user("u1", "alice"),
    {
        "full_text": "A chart worth keeping https://t.co/abc https://t.co/pic",
        "created_at": "Wed Oct 10 20:19:24 +0000 2018",
        "lang": "en",
        "favorite_count": 120,
        "retweet_count": 5,
        "reply_count": 2,
        "quote_count": 1,
        "bookmark_count": 9,
        "conversation_id_str": "1001",
        "entities": {"urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.com/post"}]},
        "extended_entities": {
            "media": [
                {
                    "media_key": "m1",
                    "type": "photo",
                    "url": "https://t.co/pic",
                    "media_url_https": "https://pbs.twimg.com/media/one.jpg",
                    "ext_alt_text": "a line chart",
                    "original_info": {"width": 1200, "height": 800},
                }
            ]
        },
    },
)

VIDEO_TWEET = _tweet(
    "1002",
    _user("u2", "bob", new_shape=False),
    {
        "full_text": "Demo clip",
        "created_at": "Thu Mar 06 09:00:00 +0000 2025",
        "lang": "ja",
        "conversation_id_str": "1002",
        "entities": {},
        "extended_entities": {
            "media": [
                {
                    "media_key": "m2",
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/poster.jpg",
                    "original_info": {"width": 1280, "height": 720},
                    "video_info": {
                        "duration_millis": 92_000,
                        "variants": [
                            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
                            {"content_type": "video/mp4", "bitrate": 832_000, "url": "https://video.twimg.com/low.mp4"},
                            {"content_type": "video/mp4", "bitrate": 2_176_000, "url": "https://video.twimg.com/high.mp4"},
                        ],
                    },
                }
            ]
        },
    },
)

LONG_TWEET = {
    "__typename": "TweetWithVisibilityResults",
    "tweet": _tweet(
        "1003",
        _user("u3", "carol"),
        {
            "full_text": "Truncated version of the thread...",
            "created_at": "Fri Jan 03 12:00:00 +0000 2025",
            "lang": "en",
            "conversation_id_str": "1003",
            "entities": {},
        },
        note_tweet={
            "note_tweet_results": {
                "result": {
                    "text": "The full long-form body, well past the old limit. See https://t.co/long",
                    "entity_set": {"urls": [{"url": "https://t.co/long", "expanded_url": "https://arxiv.org/abs/2401.00001"}]},
                }
            }
        },
        quoted_status_result={
            "result": _tweet(
                "999",
                _user("u4", "dave"),
                {"full_text": "The original claim", "created_at": "Fri Jan 03 10:00:00 +0000 2025", "lang": "en", "entities": {}},
            )
        },
        card={
            "legacy": {
                "url": "https://arxiv.org/abs/2401.00001",
                "binding_values": [
                    {"key": "title", "value": {"string_value": "A Paper"}},
                    {"key": "description", "value": {"string_value": "An abstract."}},
                    {"key": "thumbnail_image_large", "value": {"image_value": {"url": "https://pbs.twimg.com/card.jpg"}}},
                ],
            }
        },
    ),
}


ARTICLE_TWEET = _tweet(
    "1004",
    _user("u5", "frank"),
    {
        "full_text": "New piece https://t.co/art",
        "created_at": "Mon Feb 03 08:00:00 +0000 2025",
        "lang": "en",
        "conversation_id_str": "1004",
        "entities": {"urls": [{"url": "https://t.co/art", "expanded_url": "https://x.com/i/article/77"}]},
    },
    article={
        "article_results": {
            "result": {
                "rest_id": "77",
                "title": "Step-By-Step LLM Engineering",
                "preview_text": "At some point, reading about LLMs stops being enough.",
                "metadata": {"first_published_at_secs": 1738569600},
            }
        }
    },
)


def page() -> dict:
    return {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [
                                _entry("1800000000000000003", PHOTO_TWEET),
                                _entry("1800000000000000002", VIDEO_TWEET),
                                _entry("1800000000000000001", LONG_TWEET),
                                _entry("1800000000000000004", ARTICLE_TWEET),
                                {"entryId": "cursor-top-0", "sortIndex": "1800000000000000009",
                                 "content": {"entryType": "TimelineTimelineCursor", "cursorType": "Top", "value": "TOP"}},
                                {"entryId": "cursor-bottom-0", "sortIndex": "1800000000000000000",
                                 "content": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "NEXT"}},
                            ],
                        }
                    ]
                }
            }
        }
    }


def empty_page() -> dict:
    return {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": []}]}}}}
