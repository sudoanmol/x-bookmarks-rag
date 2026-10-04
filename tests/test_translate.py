"""Translation. Groq is stubbed so these stay offline."""

from __future__ import annotations

import json

import httpx
import pytest

from x_bookmarks_rag import index as index_mod
from x_bookmarks_rag import search as search_mod
from x_bookmarks_rag import translate as translate_mod
from x_bookmarks_rag.chunk import Chunk

from .test_index import indexed  # noqa: F401  (pytest fixture)


def piece(text: str, lang: str | None) -> Chunk:
    return Chunk(tweet_id="1", source="post", ref=None, position=0, text=text, lang=lang)


@pytest.mark.parametrize(
    "text, lang, expected",
    [
        ("@a: 俺的最強技術スタックこれ", "ja", True),
        ("@a: simplicity", "es", True),  # a wrong tag still asks; the model says English
        ("@a: 终于知道 Codex 5.6 跑长任务时，额度为什么可能掉得特别快了", "en", True),
        ("@a: base ui + shadcn/ui for terminals", "en", False),
        ("@a: base ui + shadcn/ui for terminals", "en-GB", False),
        ("@a: 👀", "zxx", False),
        ("@a: hi", None, False),
    ],
)
def test_candidates(text, lang, expected):
    assert translate_mod.is_candidate(piece(text, lang)) is expected


def test_long_text_splits_on_paragraphs_within_the_limit():
    paras = ["字" * 900, "字" * 900, "x" * 4000]
    parts = translate_mod.pieces("\n\n".join(paras))
    assert all(len(p) <= translate_mod.PIECE_CHARS for p in parts)
    assert parts[0] == "字" * 900
    assert "".join(parts).replace("\n\n", "") == "".join(paras)


def groq(replies: list[httpx.Response]):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return replies.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_english_text_translates_to_none():
    client, calls = groq([reply("ENGLISH")])
    assert translate_mod.groq_translator(client)("simplicity") is None
    assert calls[0]["model"] == translate_mod.MODEL


def test_429_waits_then_translates():
    client, _ = groq([httpx.Response(429, headers={"retry-after": "2"}), reply("My stack.")])
    slept = []
    assert translate_mod.groq_translator(client, sleep=slept.append)("俺のスタック") == "My stack."
    assert slept == [2.0]


def test_pieces_join_and_english_pieces_keep_their_text():
    text = "字" * 1000 + "\n\n" + "plain English here " * 60
    client, calls = groq([reply("Character"), reply("ENGLISH")])
    out = translate_mod.groq_translator(client)(text)
    assert len(calls) == 2
    assert out == "Character\n\n" + ("plain English here " * 60)


def test_index_stores_english_and_keeps_the_original(indexed):  # noqa: F811
    conn = indexed
    jobs = dict(translate_mod.pending(conn))
    japanese = next(p for p in jobs.values() if p.tweet_id == "1002" and p.source == "post")
    tally = translate_mod.run(
        conn,
        list(jobs.items()),
        translator=lambda text: "@bob: A demo clip, in English" if text == japanese.text else None,
    )
    assert tally["translated"] == 1
    assert translate_mod.pending(conn) == []

    index_mod.build(conn)
    row = conn.execute(
        "SELECT text, source_text FROM chunks WHERE tweet_id = '1002' AND source = 'post'"
    ).fetchone()
    assert row["text"] == "@bob: A demo clip, in English"
    assert row["source_text"] == japanese.text

    hits = search_mod.search(conn, "demo clip english", limit=3)
    assert hits and hits[0].tweet_id == "1002"
