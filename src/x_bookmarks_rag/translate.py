"""Translate foreign chunks into English with Groq.

Translations are cached by the hash of the original chunk text. `index.build`
then stores the English in chunks.text and the original in chunks.source_text,
so BM25 and the embedding both see English.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable

import httpx

from . import config, db
from .chunk import Chunk

MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# The free tier allows 8,000 tokens per minute, prompt and reply together.
# Dense CJK runs about one token per character, so long text goes in pieces.
PIECE_CHARS = 1_500
ENGLISH = "ENGLISH"

SYSTEM = (
    "You translate text into English. Reply with the English translation only: "
    "no preface, no notes, no quotes. Keep the leading label (the text before the "
    "first colon), @handles, URLs, code, numbers, [m:ss] timestamps, and line "
    f"breaks as they are. If the text is already English, reply with exactly {ENGLISH}."
)

# X tags that do not name a language: no text, media only, unknown, art.
NOT_LANGUAGES = {"zxx", "qme", "und", "art"}
LETTER = re.compile(r"[^\W\d_]")
LATIN = re.compile(r"[A-Za-zÀ-ɏ]")

Translator = Callable[[str], str | None]


def is_candidate(piece: Chunk) -> bool:
    """Worth asking the model. A false positive costs one call, nothing more.

    X's tag is wrong on many short posts, and untagged pages can hold foreign
    text, so either signal is enough: a foreign tag, or mostly non-Latin letters.
    """
    lang = (piece.lang or "").lower()
    if lang and not lang.startswith("en") and lang not in NOT_LANGUAGES:
        return True
    letters = LETTER.findall(piece.text)
    if len(letters) < 5:
        return False
    return len(LATIN.findall(piece.text)) / len(letters) < 0.7


def pieces(text: str) -> list[str]:
    """Paragraph-aligned pieces of at most PIECE_CHARS, hard-cut if one is longer."""
    out: list[str] = []
    for para in text.split("\n\n"):
        while len(para) > PIECE_CHARS:
            out.append(para[:PIECE_CHARS])
            para = para[PIECE_CHARS:]
        if out and len(out[-1]) + len(para) + 2 <= PIECE_CHARS:
            out[-1] += "\n\n" + para
        else:
            out.append(para)
    return out


def groq_translator(client: httpx.Client, sleep=time.sleep) -> Translator:
    """One call per piece. Returns None when every piece is already English."""

    def ask(text: str) -> str:
        while True:
            r = client.post(
                GROQ_URL,
                json={
                    "model": MODEL,
                    "temperature": 0,
                    "reasoning_effort": "low",
                    "max_completion_tokens": 3_000,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": text},
                    ],
                },
            )
            if r.status_code == 429:
                sleep(float(r.headers.get("retry-after") or 15))
                continue
            r.raise_for_status()
            return (r.json()["choices"][0]["message"].get("content") or "").strip()

    def translate(text: str) -> str | None:
        parts = [(p, ask(p)) for p in pieces(text)]
        if all(reply == ENGLISH for _p, reply in parts):
            return None
        return "\n\n".join(p if reply == ENGLISH else reply for p, reply in parts)

    return translate


def pending(
    conn: sqlite3.Connection, *, exclude: frozenset[str] = frozenset()
) -> list[tuple[str, Chunk]]:
    """(hash, chunk) for every candidate the cache has not seen, from the very
    chunks `xbm index` would build."""
    from .index import _collect, _hash  # index imports this module

    known = lookup(conn)
    out: dict[str, Chunk] = {}
    for chunks in _collect(conn).values():
        for piece in chunks:
            key = _hash(piece.text)
            if piece.source not in exclude and key not in known and is_candidate(piece):
                out[key] = piece
    return list(out.items())


def lookup(conn: sqlite3.Connection) -> dict[str, str | None]:
    """hash -> English, or None for text the model called English already."""
    return {r["hash"]: r["text"] for r in conn.execute("SELECT hash, text FROM translations")}


def save(conn: sqlite3.Connection, key: str, source_text: str, text: str | None) -> None:
    conn.execute(
        """
        INSERT INTO translations(hash, source_text, text, model, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(hash) DO UPDATE SET
            text = excluded.text, model = excluded.model, updated_at = excluded.updated_at
        """,
        (key, source_text, text, MODEL, db.now_iso()),
    )
    conn.commit()


def run(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, Chunk]],
    *,
    translator: Translator | None = None,
    on_progress=None,
) -> dict[str, int]:
    own = translator is None
    client = None
    if own:
        if not config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is missing from .env")
        client = httpx.Client(
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}, timeout=120
        )
        translator = groq_translator(client)

    tally = {"translated": 0, "english": 0, "failed": 0}
    try:
        for done, (key, piece) in enumerate(candidates, 1):
            try:
                english = translator(piece.text)
            except Exception as exc:
                tally["failed"] += 1
                if on_progress:
                    on_progress(done, len(candidates), piece, f"{type(exc).__name__}: {exc}"[:200])
                continue
            save(conn, key, piece.text, english)
            tally["english" if english is None else "translated"] += 1
            if on_progress:
                on_progress(done, len(candidates), piece, None)
    finally:
        if client:
            client.close()
    return tally
