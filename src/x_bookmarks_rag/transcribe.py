"""Transcribe bookmark videos with Whisper on Modal GPUs.

A transcript is stored as a `documents` row with kind 'video', so chunking,
search, and read_document treat it like any other extracted page.
"""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

import modal

from .extract import Document, save

MODEL = "large-v3-turbo"
MAX_ATTEMPTS = 2
# Start a new paragraph once the current one spans this many seconds.
PARAGRAPH_S = 60.0


@dataclass
class Job:
    url: str  # the X video URL, which keys the documents row
    mp4: str  # media.small_url: speech needs no pixels
    tweet_id: str
    duration_ms: int


# Results arrive as {"url", "segments": [(start, end, text)], "lang"} or
# {"url", "error"}. A clip with no audio track has empty segments.
Transcriber = Callable[[list[Job]], Iterator[dict]]


# --------------------------------------------------------------------------
# Remote side. Runs inside the Modal container.
# --------------------------------------------------------------------------

app = modal.App("xbm-transcribe")
hf_cache = modal.Volume.from_name("xbm-hf-cache", create_if_missing=True)
# CTranslate2 4.x needs CUDA 12 and cuDNN 9 at runtime. faster-whisper 1.2.1
# passes av.open(metadata_errors=...), which PyAV 19 removed.
image = (
    modal.Image.from_registry("nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04", add_python="3.12")
    .uv_pip_install("faster-whisper==1.2.1", "av<19", "httpx", "python-dotenv")
    .add_local_python_source("x_bookmarks_rag")
)


@app.cls(
    image=image,
    gpu="L4",
    volumes={"/root/.cache/huggingface": hf_cache},
    timeout=3600,
    scaledown_window=60,
    max_containers=8,
)
class Model:
    @modal.enter()
    def load(self) -> None:
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        # A raise here makes Modal restart the container forever, with the
        # client still waiting. Keep the error and fail the first call.
        self.load_error: str | None = None
        try:
            model = WhisperModel(MODEL, device="cuda", compute_type="float16")
            self.pipeline = BatchedInferencePipeline(model)
        except Exception as exc:
            self.load_error = repr(exc)
        hf_cache.commit()

    @modal.method()
    def transcribe(self, url: str, mp4: str) -> dict:
        import tempfile

        import av
        import httpx

        if self.load_error:
            raise RuntimeError(f"model failed to load: {self.load_error}")
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
                with httpx.stream("GET", mp4, timeout=120, follow_redirects=True) as r:
                    r.raise_for_status()
                    expected = int(r.headers.get("content-length", 0))
                    for block in r.iter_bytes():
                        f.write(block)
                f.flush()
                if expected and f.tell() != expected:
                    raise IOError(f"short download: {f.tell()} of {expected} bytes")
                with av.open(f.name) as container:
                    if not container.streams.audio:
                        return {"url": url, "segments": [], "lang": None}
                # The pipeline runs VAD first, which keeps Whisper from
                # inventing words over silence and music.
                segments, info = self.pipeline.transcribe(f.name, batch_size=16)
                spans = [(s.start, s.end, s.text.strip()) for s in segments]
                # decode_audio drops invalid data without a word, so a damaged
                # file decodes to nothing and looks silent. Real audio always
                # gets a language, even when it holds no speech.
                if not info.language:
                    raise ValueError("audio track decoded to nothing")
                return {"url": url, "segments": spans, "lang": info.language}
        except Exception as exc:
            return {"url": url, "error": f"{type(exc).__name__}: {exc}"[:500]}


def modal_transcriber(jobs: list[Job]) -> Iterator[dict]:
    with modal.enable_output(), app.run():
        yield from Model().transcribe.map(
            [j.url for j in jobs], [j.mp4 for j in jobs], order_outputs=False
        )


# --------------------------------------------------------------------------
# Local side.
# --------------------------------------------------------------------------


def clock(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def to_html(segments: Iterable[tuple[float, float, str]]) -> str:
    """One <p> per minute or so, led by its start time."""
    paragraphs: list[tuple[float, list[str]]] = []
    for start, _end, text in segments:
        if not text:
            continue
        if not paragraphs or start - paragraphs[-1][0] >= PARAGRAPH_S:
            paragraphs.append((start, []))
        paragraphs[-1][1].append(text)
    return "\n".join(
        f"<p>[{clock(start)}] {html.escape(' '.join(texts))}</p>"
        for start, texts in paragraphs
    )


def pending(conn: sqlite3.Connection, *, retry_failed: bool = False) -> list[Job]:
    """Videos still worth transcribing, longest first so containers finish together."""
    done: dict[str, int] = {}
    for row in conn.execute("SELECT url, error, attempts FROM documents WHERE kind = 'video'"):
        done[row["url"]] = -1 if not row["error"] else row["attempts"]

    jobs: list[Job] = []
    for row in conn.execute(
        """
        SELECT m.tweet_id, m.position, m.small_url, m.duration_ms,
               COALESCE(a.screen_name, 'i') AS handle
          FROM media m
          JOIN bookmarks b USING(tweet_id)
          LEFT JOIN authors a USING(author_id)
         WHERE b.removed_at IS NULL AND m.kind = 'video' AND m.small_url IS NOT NULL
         ORDER BY m.duration_ms DESC
        """
    ):
        url = f"https://x.com/{row['handle']}/status/{row['tweet_id']}/video/{row['position'] + 1}"
        attempts = done.get(url)
        if attempts is None or (attempts >= 0 and (retry_failed or attempts < MAX_ATTEMPTS)):
            jobs.append(Job(url, row["small_url"], row["tweet_id"], row["duration_ms"] or 0))
    return jobs


def to_document(job: Job, result: dict) -> Document:
    doc = Document(url=job.url, kind="video", tweet_id=job.tweet_id, site="x.com")
    if result.get("error"):
        doc.error = result["error"]
        return doc
    doc.title = f"Video transcript ({clock(job.duration_ms / 1000)})"
    doc.body = to_html(result["segments"])
    doc.word_count = sum(len(text.split()) for _s, _e, text in result["segments"])
    doc.lang = result.get("lang")
    return doc


def run(
    conn: sqlite3.Connection,
    jobs: list[Job],
    *,
    transcriber: Transcriber = modal_transcriber,
    on_progress=None,
) -> dict[str, int]:
    by_url = {j.url: j for j in jobs}
    tally = {"ok": 0, "silent": 0, "failed": 0}
    for done, result in enumerate(transcriber(jobs), 1):
        doc = to_document(by_url[result["url"]], result)
        save(conn, doc)
        tally["failed" if doc.error else "ok" if doc.word_count else "silent"] += 1
        if on_progress:
            on_progress(done, len(jobs), doc)
    return tally
