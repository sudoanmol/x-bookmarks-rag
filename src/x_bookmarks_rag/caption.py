"""Caption bookmark photos with a vision model on a Modal GPU.

The container fetches each photo, and vLLM captions a batch at a time.
Results come back batch by batch, so an interrupted run keeps what it got.
"""

from __future__ import annotations

import base64
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

import modal

from . import db

MODEL = "Qwen/Qwen3.8-27B-FP8"
MAX_TOKENS = 2048
BATCH = 64
MAX_ATTEMPTS = 2

PROMPT = (
    "Transcribe every word visible in the image, preserving line breaks "
    "and layout. Then write one line that starts with Image: and describes "
    "the picture. If the image has no text, write only the Image: line."
)

THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass
class Job:
    media_key: str
    url: str
    tweet_id: str


@dataclass
class Caption:
    media_key: str
    text: str = ""
    model: str | None = None
    error: str | None = None


# One result per URL: {"text": str} or {"error": str}.
Captioner = Callable[[Iterable[list[str]]], Iterator[list[dict]]]


# --------------------------------------------------------------------------
# Remote side. Runs inside the Modal container.
# --------------------------------------------------------------------------

app = modal.App("xbm-caption")
hf_cache = modal.Volume.from_name("xbm-hf-cache", create_if_missing=True)
# A devel image, not debian_slim: vLLM's FP8 kernels (DeepGEMM) compile at
# startup and need the CUDA toolkit. vLLM 0.30 wheels target CUDA 13.
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.3-devel-ubuntu24.04", add_python="3.12")
    .uv_pip_install("vllm==0.30.0", "httpx", "python-dotenv")
    .add_local_python_source("x_bookmarks_rag")
)


@app.cls(
    image=image,
    gpu="H100",
    volumes={"/root/.cache/huggingface": hf_cache},
    timeout=3600,
    scaledown_window=60,
    max_containers=1,
)
class Model:
    @modal.enter()
    def load(self) -> None:
        from vllm import LLM

        # A raise here makes Modal restart the container on the GPU forever,
        # with the client still waiting. Keep the error and fail the first
        # call instead, so the run ends and the app stops.
        self.load_error: str | None = None
        try:
            # pbs ?name=large caps the long side at 2048 px, about 4k image tokens.
            self.llm = LLM(
                MODEL,
                max_model_len=16_384,
                max_num_seqs=BATCH,
                limit_mm_per_prompt={"image": 1},
            )
        except Exception as exc:
            self.load_error = repr(exc)
        hf_cache.commit()

    @modal.method()
    def caption(self, urls: list[str]) -> list[dict]:
        import httpx
        from vllm import SamplingParams

        if self.load_error:
            raise RuntimeError(f"model failed to load: {self.load_error}")
        results: list[dict] = [{} for _ in urls]
        prompts, slots = [], []
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            for i, url in enumerate(urls):
                try:
                    r = client.get(url, params={"name": "large"})
                    r.raise_for_status()
                except Exception as exc:
                    results[i] = {"error": f"fetch: {exc}"[:500]}
                    continue
                kind = r.headers.get("content-type", "image/jpeg").split(";")[0]
                data = base64.b64encode(r.content).decode()
                prompts.append([{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{data}"}},
                        {"type": "text", "text": PROMPT},
                    ],
                }])
                slots.append(i)

        if prompts:
            outputs = self.llm.chat(
                prompts,
                SamplingParams(temperature=0, max_tokens=MAX_TOKENS),
                chat_template_kwargs={"enable_thinking": False},
            )
            for i, out in zip(slots, outputs):
                results[i] = {"text": out.outputs[0].text}
        return results


def modal_captioner(batches: Iterable[list[str]]) -> Iterator[list[dict]]:
    with modal.enable_output(), app.run():
        yield from Model().caption.map(batches)


# --------------------------------------------------------------------------
# Local side.
# --------------------------------------------------------------------------


def parse_caption(raw: str) -> str:
    text = THINK.sub("", raw or "").strip()
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        if kept and line == kept[-1]:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def pending(conn: sqlite3.Connection, *, retry_failed: bool = False) -> list[Job]:
    """Photos still worth captioning. Videos and gifs are a later layer."""
    done: dict[str, int] = {}
    for row in conn.execute("SELECT media_key, error, attempts FROM captions"):
        done[row["media_key"]] = -1 if not row["error"] else row["attempts"]

    def wanted(key: str) -> bool:
        attempts = done.get(key)
        if attempts is None:
            return True
        if attempts < 0:
            return False
        return retry_failed or attempts < MAX_ATTEMPTS

    jobs: list[Job] = []
    for row in conn.execute(
        """
        SELECT m.media_key, m.url, m.tweet_id
          FROM media m JOIN bookmarks b USING(tweet_id)
         WHERE b.removed_at IS NULL AND m.kind = 'photo'
         ORDER BY b.sort_index DESC, m.position
        """
    ):
        if wanted(row["media_key"]):
            jobs.append(Job(row["media_key"], row["url"], row["tweet_id"]))
    return jobs


def save(conn: sqlite3.Connection, cap: Caption) -> None:
    conn.execute(
        """
        INSERT INTO captions(media_key, text, model, attempts, error, updated_at)
        VALUES(:media_key, :text, :model, 1, :error, :now)
        ON CONFLICT(media_key) DO UPDATE SET
            text       = excluded.text,
            model      = excluded.model,
            error      = excluded.error,
            updated_at = excluded.updated_at,
            attempts   = captions.attempts + 1
        """,
        {**cap.__dict__, "now": db.now_iso()},
    )
    conn.commit()


def to_caption(job: Job, result: dict) -> Caption:
    if result.get("error"):
        return Caption(media_key=job.media_key, error=result["error"])
    text = parse_caption(result.get("text", ""))
    if not text:
        return Caption(media_key=job.media_key, model=MODEL, error="empty caption")
    return Caption(media_key=job.media_key, text=text, model=MODEL)


def run(
    conn: sqlite3.Connection,
    jobs: list[Job],
    *,
    captioner: Captioner = modal_captioner,
    on_progress=None,
) -> dict[str, int]:
    batches = [jobs[i : i + BATCH] for i in range(0, len(jobs), BATCH)]
    tally = {"ok": 0, "failed": 0}
    done = 0
    # The captioner goes first in zip so it runs to its end. Leaving the
    # Modal generator unfinished closes it inside app.run(), which raises.
    for results, batch in zip(captioner([j.url for j in b] for b in batches), batches):
        for job, result in zip(batch, results):
            cap = to_caption(job, result)
            save(conn, cap)
            tally["failed" if cap.error else "ok"] += 1
            done += 1
            if on_progress:
                on_progress(done, len(jobs), cap)
    return tally
