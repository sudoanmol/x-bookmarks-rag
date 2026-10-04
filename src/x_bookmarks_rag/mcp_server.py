"""MCP tools for searching and reading X bookmarks."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from . import chunk as chunk_mod
from . import db
from . import index as index_mod
from . import search as search_mod
from .extract import READABLE_SQL, normalize_url

PAGE_CHARS = 12_000
PASSAGE_LIMIT = 5

Source = Literal["post", "quote", "article", "link", "image", "video"]
SearchLimit = Annotated[int, Field(ge=1, le=50, description="Maximum number of bookmarks to return.")]
Offset = Annotated[int, Field(ge=0, description="Character offset for document paging.")]


class SearchHit(BaseModel):
    tweet_id: str
    url: str
    author: str
    created_at: str | None
    score: float
    best_source: str
    best_chunk: str
    best_ref: str | None
    chunk_count: int
    word_count: int
    media: list[str]
    lang: str | None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]


class Author(BaseModel):
    author_id: str | None
    screen_name: str | None
    name: str | None
    avatar_url: str | None
    verified: bool
    description: str | None


class QuotedPost(BaseModel):
    tweet_id: str | None
    text: str


class Link(BaseModel):
    url: str
    domain: str | None
    title: str | None
    description: str | None
    thumb_url: str | None
    from_card: bool


class Media(BaseModel):
    media_key: str
    kind: str
    url: str
    thumb_url: str | None
    alt_text: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    bitrate: int | None
    small_url: str | None
    small_bitrate: int | None
    position: int
    caption: str | None = None


class Bookmark(BaseModel):
    tweet_id: str
    url: str
    text: str
    lang: str | None
    created_at: str | None
    author: Author
    quoted_post: QuotedPost | None
    links: list[Link]
    media: list[Media]


class DocumentInfo(BaseModel):
    url: str
    kind: str
    title: str | None
    word_count: int


class DocumentPassage(BaseModel):
    source: str
    ref: str
    position: int
    score: float
    text: str


class DocumentRead(BaseModel):
    mode: Literal["page", "search"]
    documents: list[DocumentInfo]
    query: str | None = None
    passages: list[DocumentPassage] = Field(default_factory=list)
    offset: int | None = None
    next_offset: int | None = None
    has_more: bool | None = None
    text: str | None = None


server = MCPServer(
    "x-bookmarks",
    description="Search and read the owner's X bookmarks and extracted pages.",
    instructions=(
        "Use search_bookmarks first. Use get_bookmark for the full post and its attachments. "
        "Use read_document when a hit has more than one chunk or comes from an extracted page."
    ),
)


def _open_index():
    conn = index_mod.connect()
    if not conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]:
        conn.close()
        raise ValueError("The bookmark index is empty. Run `uv run xbm index` first.")
    return conn


@server.tool()
def search_bookmarks(
    query: Annotated[str, Field(min_length=1, description="Natural-language search query.")],
    limit: SearchLimit = 10,
    author: Annotated[str | None, Field(description="X handle, with or without @.")] = None,
    source: Annotated[Source | None, Field(description="Content type to search.")] = None,
) -> SearchResponse:
    """Search bookmarks. Each result includes its best matching passage and size."""
    conn = _open_index()
    try:
        hits = search_mod.search(conn, query, limit=limit, author=author, source=source)
    finally:
        conn.close()
    return SearchResponse(
        query=query,
        hits=[
            SearchHit(
                tweet_id=hit.tweet_id,
                url=hit.url,
                author=hit.author,
                created_at=hit.created_at,
                score=hit.score,
                best_source=hit.best_source,
                best_chunk=hit.best_chunk,
                best_ref=hit.best_ref,
                chunk_count=hit.chunk_count,
                word_count=hit.word_count,
                media=hit.media.split(",") if hit.media else [],
                lang=hit.lang,
            )
            for hit in hits
        ],
    )


@server.tool()
def get_bookmark(
    tweet_id: Annotated[str, Field(min_length=1, description="Numeric X post ID.")],
) -> Bookmark:
    """Get the full post, quoted post, author, links, and media. Excludes page bodies."""
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT b.tweet_id, b.url, b.text, b.lang, b.created_at,
                   b.quoted_tweet_id, b.quoted_text,
                   a.author_id, a.screen_name, a.name, a.avatar_url,
                   a.verified, a.description
            FROM bookmarks b
            LEFT JOIN authors a USING(author_id)
            WHERE b.tweet_id = ? AND b.removed_at IS NULL
            """,
            (tweet_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Bookmark {tweet_id!r} was not found.")

        links = conn.execute(
            """
            SELECT url, domain, title, description, thumb_url, from_card
            FROM links WHERE tweet_id = ? ORDER BY url
            """,
            (tweet_id,),
        ).fetchall()
        media = conn.execute(
            """
            SELECT m.media_key, m.kind, m.url, m.thumb_url, m.alt_text, m.width, m.height,
                   m.duration_ms, m.bitrate, m.small_url, m.small_bitrate, m.position,
                   CASE WHEN c.error IS NULL THEN c.text END AS caption
              FROM media m
              LEFT JOIN captions c ON c.media_key = m.media_key
             WHERE m.tweet_id = ?
             ORDER BY m.position, m.media_key
            """,
            (tweet_id,),
        ).fetchall()
    finally:
        conn.close()

    quoted_post = None
    if row["quoted_text"]:
        quoted_post = QuotedPost(tweet_id=row["quoted_tweet_id"], text=row["quoted_text"])
    return Bookmark(
        tweet_id=row["tweet_id"],
        url=row["url"],
        text=row["text"],
        lang=row["lang"],
        created_at=row["created_at"],
        author=Author(
            author_id=row["author_id"],
            screen_name=f"@{row['screen_name']}" if row["screen_name"] else None,
            name=row["name"],
            avatar_url=row["avatar_url"],
            verified=bool(row["verified"]),
            description=row["description"],
        ),
        quoted_post=quoted_post,
        links=[Link(**dict(item)) for item in links],
        media=[Media(**dict(item)) for item in media],
    )


def _find_documents(conn, target: str):
    if "://" in target:
        urls = {target, normalize_url(target)}
        placeholders = ",".join("?" for _ in urls)
        rows = conn.execute(
            f"""
            SELECT d.url, d.kind, d.title, d.body, d.word_count
            FROM documents d
            JOIN bookmarks b ON b.tweet_id = d.tweet_id
            WHERE d.url IN ({placeholders}) AND b.removed_at IS NULL
                  AND {READABLE_SQL}
            ORDER BY d.url
            """,
            sorted(urls),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT d.url, d.kind, d.title, d.body, d.word_count
            FROM documents d
            JOIN bookmarks b ON b.tweet_id = d.tweet_id
            WHERE d.tweet_id = ? AND b.removed_at IS NULL
                  AND {READABLE_SQL}
            ORDER BY d.kind = 'x_article' DESC, d.url
            """,
            (target,),
        ).fetchall()
    if not rows:
        raise ValueError(f"No readable document was found for {target!r}.")
    return rows


def _document_info(rows) -> list[DocumentInfo]:
    return [
        DocumentInfo(
            url=row["url"],
            kind=row["kind"],
            title=row["title"],
            word_count=row["word_count"],
        )
        for row in rows
    ]


@server.tool()
def read_document(
    tweet_id_or_url: Annotated[
        str,
        Field(min_length=1, description="A bookmark's numeric post ID or an extracted page URL."),
    ],
    query: Annotated[
        str | None,
        Field(description="Search only these pages and return their five best passages."),
    ] = None,
    offset: Offset = 0,
) -> DocumentRead:
    """Read extracted page text. Use query for passages, or offset for bounded paging."""
    conn = index_mod.connect()
    try:
        rows = _find_documents(conn, tweet_id_or_url)
        documents = _document_info(rows)
        urls = {row["url"] for row in rows}

        if query and query.strip():
            if not conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]:
                raise ValueError("The bookmark index is empty. Run `uv run xbm index` first.")
            placeholders = ",".join("?" for _ in urls)
            chunk_ids = {
                row["id"]
                for row in conn.execute(
                    f"""
                    SELECT id FROM chunks
                    WHERE ref IN ({placeholders}) AND source IN ('article', 'link', 'video')
                    """,
                    sorted(urls),
                )
            }
            if not chunk_ids:
                raise ValueError("The selected document is not indexed. Run `uv run xbm index`.")
            hits = search_mod.search_chunks(conn, query.strip(), chunk_ids, limit=PASSAGE_LIMIT)
            return DocumentRead(
                mode="search",
                documents=documents,
                query=query.strip(),
                passages=[
                    DocumentPassage(
                        source=hit.source,
                        ref=hit.ref or "",
                        position=hit.position,
                        score=hit.score,
                        text=hit.text,
                    )
                    for hit in hits
                ],
            )

        sections = []
        for row in rows:
            title = row["title"] or row["url"]
            sections.append(f"# {title}\n\nSource: {row['url']}\n\n{chunk_mod.to_text(row['body'])}")
        text = "\n\n".join(sections)
        page = text[offset : offset + PAGE_CHARS]
        next_offset = offset + len(page)
        has_more = next_offset < len(text)
        return DocumentRead(
            mode="page",
            documents=documents,
            offset=offset,
            next_offset=next_offset if has_more else None,
            has_more=has_more,
            text=page,
        )
    finally:
        conn.close()


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
