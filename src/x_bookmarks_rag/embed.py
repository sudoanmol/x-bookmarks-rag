"""Local embeddings through Ollama.

EmbeddingGemma is trained with asymmetric prefixes: documents and queries are
framed differently. Ollama applies no template of its own, so this module adds
them exactly as the model card documents.
"""

from __future__ import annotations

import ollama

MODEL = "embeddinggemma"
DIMENSIONS = 768
CONTEXT_TOKENS = 2048


def as_document(text: str, title: str = "none") -> str:
    return f"title: {title} | text: {text}"


def as_query(text: str) -> str:
    return f"task: search result | query: {text}"


def embed_documents(texts: list[str], batch: int = 32) -> list[list[float]]:
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        window = [as_document(t) for t in texts[start : start + batch]]
        out.extend(ollama.embed(model=MODEL, input=window)["embeddings"])
    return out


def embed_query(text: str) -> list[float]:
    return ollama.embed(model=MODEL, input=as_query(text))["embeddings"][0]
