"""Command line entry point."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from playwright.sync_api import sync_playwright
from rich.console import Console

from . import capture as capture_mod
from . import extract as extract_mod
from . import config, db, inspect as inspect_mod, normalize as normalize_mod, session
from . import index as index_mod
from . import search as search_mod

app = typer.Typer(
    add_completion=False,
    help="Search your X bookmarks in natural language.",
    no_args_is_help=True,
)
console = Console()


@app.command(rich_help_panel="Setup")
def login() -> None:
    """Open a browser, sign in to X by hand, and save the session."""
    console.print("[cyan]Opening a browser. Sign in to X, then leave the window alone.[/cyan]")
    with sync_playwright() as pw:
        try:
            session.login(pw)
        except TimeoutError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]Session saved to {config.STATE_PATH} (mode 0600).[/green]")


def _capture(full: bool, max_pages: int, headed: bool) -> None:
    """Capture new bookmarks, then load them into the database."""
    if not session.session_exists():
        console.print("[red]No saved session. Run `xbm login` first.[/red]")
        raise typer.Exit(1)

    conn = db.connect()
    watermark = db.get_watermark(conn)
    conn.close()

    mode = "full walk" if full else ("incremental" if watermark else "first run")
    console.print(f"[cyan]Capturing bookmarks ({mode}).[/cyan]")

    with console.status("Loading the bookmarks timeline...") as status:
        def on_page(page_no: int, new: int, total: int) -> None:
            status.update(f"Page {page_no}: +{new} posts, {total} captured")

        try:
            result = capture_mod.capture(
                full=full,
                max_pages=max_pages or None,
                headless=not headed,
                on_page=on_page,
            )
        except session.SessionExpired as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    console.print(
        f"[green]Captured {result.entries} posts across {result.pages} pages[/green] "
        f"[dim](stopped: {result.stop_reason})[/dim]"
    )

    if not result.pages:
        raise typer.Exit(1)

    loaded = normalize_mod.normalize(result.paths)
    console.print(
        f"[green]Stored {loaded.bookmarks} posts, {loaded.media} media items, "
        f"{loaded.links} links.[/green]"
    )

    conn = db.connect()
    if full:
        removed = db.mark_removed(conn, loaded.seen_ids)
        if removed:
            console.print(f"[yellow]Marked {removed} bookmarks as removed.[/yellow]")

    if result.clean and result.max_sort_index:
        db.set_watermark(conn, result.max_sort_index)
        db.set_state(conn, "last_sync_at", db.now_iso())
        console.print("[dim]Watermark advanced. The next sync fetches only new bookmarks.[/dim]")
    else:
        console.print(
            "[yellow]The run did not reach a clean end, so the watermark was not moved. "
            "Run sync again to continue.[/yellow]"
        )
    conn.close()


# Each flag skips its stage and keeps that content out of the index, on top
# of what config.toml already excludes. A flag holds for one run.
NoArticles = typer.Option(False, "--no-articles", help="Leave out X Articles.")
NoLinks = typer.Option(False, "--no-links", help="Leave out linked pages and link cards.")
NoImages = typer.Option(False, "--no-images", help="Leave out photo captions and OCR.")
NoVideos = typer.Option(False, "--no-videos", help="Leave out video transcripts.")
NoQuotes = typer.Option(False, "--no-quotes", help="Leave out quoted posts.")
NoTranslate = typer.Option(False, "--no-translate", help="Index foreign text untranslated.")


def _resolve(
    articles: bool, links: bool, images: bool, videos: bool, quotes: bool, no_translate: bool
) -> tuple[frozenset[str], bool]:
    """Merge config.toml with this run's flags into (exclude, translate)."""
    try:
        saved = config.settings()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    flags = {"article": articles, "link": links, "image": images, "video": videos, "quote": quotes}
    exclude = saved.exclude | {source for source, skip in flags.items() if skip}
    translate = saved.translate and not no_translate
    if exclude or not translate:
        left_out = sorted(exclude) + ([] if translate else ["translation"])
        console.print(f"[dim]Leaving out: {', '.join(left_out)}[/dim]")
    return frozenset(exclude), translate


@app.command(rich_help_panel="Daily")
def sync(
    full: bool = typer.Option(False, "--full", help="Walk every bookmark and detect removals."),
    max_pages: int = typer.Option(0, "--max-pages", help="Stop after N pages. 0 means no limit."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    no_articles: bool = NoArticles,
    no_links: bool = NoLinks,
    no_images: bool = NoImages,
    no_videos: bool = NoVideos,
    no_quotes: bool = NoQuotes,
    no_translate: bool = NoTranslate,
) -> None:
    """Capture new bookmarks, enrich them, and update the index.

    Runs capture, extract, caption, transcribe, translate, and index. Every
    stage handles only what is new. A failed stage does not stop the rest, so
    new posts still reach the index; the run exits non-zero at the end.
    """
    exclude, translate = _resolve(
        no_articles, no_links, no_images, no_videos, no_quotes, no_translate
    )
    kinds = {k for k, source in (("x_article", "article"), ("link", "link")) if source not in exclude}
    stages = [
        ("capture", lambda: _capture(full, max_pages, headed)),
        ("extract", lambda: _extract(kinds), bool(kinds)),
        ("caption", lambda: _caption(wait_for_batch=True), "image" not in exclude),
        ("transcribe", _transcribe, "video" not in exclude),
        ("translate", lambda: _translate(exclude), translate),
        ("index", lambda: _index(exclude=exclude, translated=translate)),
    ]
    failed = []
    for name, run, *enabled in stages:
        if enabled and not enabled[0]:
            continue
        console.rule(f"[bold]{name}", align="left")
        try:
            run()
        except Exception as exc:  # typer.Exit included: one stage must not sink the rest
            if not isinstance(exc, typer.Exit):
                console.print(f"[red]{name} failed: {type(exc).__name__}: {exc}[/red]")
            failed.append(name)
    if failed:
        console.print(f"[red]Failed stages: {', '.join(failed)}[/red]")
        raise typer.Exit(1)


@app.command(rich_help_panel="Pipeline stages")
def capture(
    full: bool = typer.Option(False, "--full", help="Walk every bookmark and detect removals."),
    max_pages: int = typer.Option(0, "--max-pages", help="Stop after N pages. 0 means no limit."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
) -> None:
    """Capture new bookmarks only. No enrichment, no index."""
    _capture(full, max_pages, headed)


@app.command(rich_help_panel="Maintenance")
def normalize() -> None:
    """Rebuild the database from every raw page already on disk."""
    result = normalize_mod.normalize()
    console.print(
        f"[green]Read {result.pages} pages: {result.bookmarks} posts, "
        f"{result.media} media items, {result.links} links.[/green]"
    )
    for path in result.failed_pages:
        console.print(f"[red]Unreadable: {path}[/red]")


@app.command(rich_help_panel="Maintenance")
def inspect() -> None:
    """Report what the captured data actually contains."""
    conn = db.connect()
    stats = inspect_mod.gather(conn)
    conn.close()
    if not stats["total"]:
        console.print("[yellow]No bookmarks stored yet. Run `xbm sync` first.[/yellow]")
        raise typer.Exit(1)
    inspect_mod.render(stats, console)


@app.command(rich_help_panel="Daily")
def status() -> None:
    """Show what is captured, enriched, and indexed, and what each stage needs."""
    import httpx

    def ok(flag: bool, yes: str = "ready", no: str = "missing") -> str:
        return f"[green]{yes}[/green]" if flag else f"[red]{no}[/red]"

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    conn = index_mod.connect()
    live = "JOIN bookmarks b USING(tweet_id) WHERE b.removed_at IS NULL"
    bookmarks = count("SELECT COUNT(*) FROM bookmarks WHERE removed_at IS NULL")
    photos = count(f"SELECT COUNT(*) FROM media m {live} AND m.kind = 'photo'")
    videos = count(f"SELECT COUNT(*) FROM media m {live} AND m.kind = 'video'")
    captioned = count("SELECT COUNT(*) FROM captions WHERE error IS NULL AND text != ''")
    transcribed = count("SELECT COUNT(*) FROM documents WHERE kind = 'video' AND error IS NULL")
    speech = count("SELECT COUNT(*) FROM documents WHERE kind = 'video' AND word_count > 0")
    pages = count(f"SELECT COUNT(*) FROM documents d WHERE kind != 'video' AND {extract_mod.READABLE_SQL}")
    failed_pages = count("SELECT COUNT(*) FROM documents WHERE kind != 'video' AND error IS NOT NULL")
    translated = count("SELECT COUNT(*) FROM translations WHERE text IS NOT NULL")
    chunks = count("SELECT COUNT(*) FROM chunks")
    embedded = count("SELECT COUNT(*) FROM chunk_vec")
    last_sync = db.get_state(conn, "last_sync_at")
    conn.close()

    try:
        saved = config.settings()
        excluded = ", ".join(sorted(saved.exclude)) or "nothing"
        if not saved.translate:
            excluded += " (translation off)"
    except ValueError as exc:
        excluded = f"[red]{exc}[/red]"
    try:
        tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2).json()["models"]
        ollama = ok(any(m["name"].startswith("embeddinggemma") for m in tags), no="embeddinggemma not pulled")
    except Exception:
        ollama = ok(False, no="not running")
    modal_ready = (Path.home() / ".modal.toml").exists() or bool(os.environ.get("MODAL_TOKEN_ID"))

    rows = [
        ("Bookmarks", f"{bookmarks:,}  [dim]last sync {last_sync or 'never'}[/dim]"),
        ("Pages", f"{pages:,} readable  [dim]{failed_pages:,} failed[/dim]"),
        ("Captions", f"{captioned:,} / {photos:,} photos"),
        ("Transcripts", f"{transcribed:,} / {videos:,} videos  [dim]{speech:,} with speech[/dim]"),
        ("Translations", f"{translated:,} chunks"),
        ("Index", f"{embedded:,} / {chunks:,} chunks embedded"),
        ("Excluded", f"{excluded}  [dim]{config.SETTINGS_PATH}[/dim]"),
        ("", ""),
        ("X session", ok(session.session_exists(), "present") + f"  [dim]{config.STATE_PATH}[/dim]"),
        ("Ollama", ollama + "  [dim]index, search[/dim]"),
        ("Modal", ok(modal_ready, no="run `modal setup`") + "  [dim]caption, transcribe[/dim]"),
        ("Groq key", ok(bool(config.GROQ_API_KEY)) + "  [dim]translate[/dim]"),
    ]
    for label, value in rows:
        console.print(f"{label:<13}{value}" if label else "")


def _extract(kinds: set[str], limit: int | None = None, retry: bool = False) -> None:
    conn = extract_mod.connect()
    jobs = [j for j in extract_mod.pending(conn, retry_failed=retry) if j.kind in kinds]
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        console.print("[green]Everything is extracted.[/green]")
        return

    articles = sum(1 for j in jobs if j.kind == "x_article")
    console.print(f"[dim]{articles} articles, {len(jobs) - articles} links[/dim]")

    with console.status("Extracting...") as status:
        def on_progress(done: int, total: int, doc) -> None:
            mark = "[red]x[/red]" if doc.error and not doc.body else "[green]ok[/green]"
            status.update(f"{done}/{total} {mark} {doc.url[:70]}")

        tally = extract_mod.run(conn, jobs, on_progress=on_progress)
    conn.close()
    console.print(f"[green]{tally['ok']:,} extracted[/green] [dim]{tally['failed']:,} failed[/dim]")


def _caption(limit: int | None = None, retry: bool = False, wait_for_batch: bool = False) -> None:
    from . import caption as caption_mod

    conn = db.connect()
    jobs = caption_mod.pending(conn, retry_failed=retry)
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        console.print("[green]Every photo is captioned.[/green]")
        return
    if wait_for_batch and len(jobs) < caption_mod.SYNC_MIN_PHOTOS:
        console.print(
            f"[dim]{len(jobs)} new photos wait for {caption_mod.SYNC_MIN_PHOTOS} before a GPU starts. "
            "Run `xbm caption` to caption them now.[/dim]"
        )
        return

    console.print(f"[dim]{len(jobs)} photos[/dim]")

    # Plain lines, not a status spinner: Modal draws its own live output.
    def on_progress(done: int, total: int, cap) -> None:
        if cap.error:
            console.print(f"[red]x[/red] {cap.media_key}: {cap.error}")
        if done % caption_mod.BATCH == 0 or done == total:
            console.print(f"{done}/{total}")

    tally = caption_mod.run(conn, jobs, on_progress=on_progress)
    conn.close()
    console.print(f"[green]{tally['ok']:,} captioned[/green] [dim]{tally['failed']:,} failed[/dim]")


def _transcribe(limit: int | None = None, retry: bool = False) -> None:
    from . import transcribe as transcribe_mod

    conn = db.connect()
    jobs = transcribe_mod.pending(conn, retry_failed=retry)
    if limit:
        jobs = jobs[-limit:]  # the shortest, so a trial run is cheap
    if not jobs:
        console.print("[green]Every video is transcribed.[/green]")
        return

    hours = sum(j.duration_ms for j in jobs) / 3_600_000
    console.print(f"[dim]{len(jobs)} videos, {hours:.1f} hours[/dim]")

    # Plain lines, not a status spinner: Modal draws its own live output.
    def on_progress(done: int, total: int, doc) -> None:
        mark = "[red]x[/red]" if doc.error else "ok" if doc.word_count else "silent"
        detail = doc.error or f"{doc.word_count:,} words"
        console.print(f"{done}/{total} {mark} {doc.url} {detail}")

    tally = transcribe_mod.run(conn, jobs, on_progress=on_progress)
    conn.close()
    console.print(
        f"[green]{tally['ok']:,} transcribed[/green] [dim]{tally['silent']:,} silent, "
        f"{tally['failed']:,} failed[/dim]"
    )


def _translate(exclude: frozenset[str] = frozenset()) -> None:
    from . import translate as translate_mod

    if not config.GROQ_API_KEY:
        console.print("[yellow]GROQ_API_KEY is missing from .env, so nothing was translated.[/yellow]")
        return

    conn = db.connect()
    jobs = translate_mod.pending(conn, exclude=exclude)
    if not jobs:
        console.print("[green]Nothing new to translate.[/green]")
        return

    console.print(f"[dim]{len(jobs)} candidate chunks[/dim]")

    def on_progress(done: int, total: int, piece, error) -> None:
        mark = f"[red]x {error}[/red]" if error else "ok"
        console.print(f"{done}/{total} {mark} {piece.source} {piece.tweet_id} {piece.lang}")

    tally = translate_mod.run(conn, jobs, on_progress=on_progress)
    conn.close()
    console.print(
        f"[green]{tally['translated']:,} translated[/green] [dim]{tally['english']:,} already "
        f"English, {tally['failed']:,} failed[/dim]"
    )


def _index(rebuild: bool = False, exclude: frozenset[str] = frozenset(), translated: bool = True) -> None:
    conn = index_mod.connect()
    with console.status("Indexing...") as status:
        def on_progress(stage: str, done: int, total: int) -> None:
            status.update(f"{stage}: {done}/{total}")

        stats = index_mod.build(
            conn, rebuild=rebuild, exclude=exclude, translated=translated, on_progress=on_progress
        )
    conn.close()
    console.print(
        f"[green]{stats['chunks']:,} chunks, {stats['embedded']:,} embedded[/green] "
        f"[dim](+{stats['added']} new, -{stats['removed']} stale)[/dim]"
    )


Limit = typer.Option(None, "--limit", "-n", help="Stop after this many items.")
Retry = typer.Option(False, "--retry", help="Try the failed items again.")


@app.command(rich_help_panel="Pipeline stages")
def extract(limit: int = Limit, retry: bool = Retry) -> None:
    """Fetch the full text behind articles and links."""
    _extract({"x_article", "link"}, limit, retry)


@app.command(rich_help_panel="Pipeline stages")
def caption(limit: int = Limit, retry: bool = Retry) -> None:
    """Caption bookmark photos with a vision model on a Modal GPU."""
    _caption(limit, retry)


@app.command(rich_help_panel="Pipeline stages")
def transcribe(limit: int = Limit, retry: bool = Retry) -> None:
    """Transcribe bookmark videos with Whisper on Modal GPUs."""
    _transcribe(limit, retry)


@app.command(rich_help_panel="Pipeline stages")
def translate() -> None:
    """Translate foreign chunks into English with Groq. Run `xbm index` after."""
    _translate(_resolve(False, False, False, False, False, False)[0])


@app.command(rich_help_panel="Pipeline stages")
def index(
    rebuild: bool = typer.Option(False, "--rebuild", help="Discard chunks and embed everything again."),
    no_articles: bool = NoArticles,
    no_links: bool = NoLinks,
    no_images: bool = NoImages,
    no_videos: bool = NoVideos,
    no_quotes: bool = NoQuotes,
    no_translate: bool = NoTranslate,
) -> None:
    """Chunk and embed the captured bookmarks."""
    exclude, translate = _resolve(
        no_articles, no_links, no_images, no_videos, no_quotes, no_translate
    )
    _index(rebuild, exclude, translated=translate)


@app.command(rich_help_panel="Daily")
def search(
    query: list[str] = typer.Argument(..., help="What you are looking for."),
    limit: int = typer.Option(10, "--limit", "-n"),
    author: str = typer.Option(None, "--author", "-a", help="Restrict to one handle."),
    source: str = typer.Option(None, "--source", help="post, quote, article, link, image, or video."),
) -> None:
    """Search your bookmarks in natural language."""
    text = " ".join(query)
    conn = index_mod.connect()
    if not conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]:
        console.print("[yellow]Nothing indexed yet. Run `xbm index` first.[/yellow]")
        raise typer.Exit(1)
    hits = search_mod.search(conn, text, limit=limit, author=author, source=source)
    conn.close()

    if not hits:
        console.print("[yellow]No matches.[/yellow]")
        raise typer.Exit(1)

    for rank, hit in enumerate(hits, 1):
        when = (hit.created_at or "")[:10]
        tags = [hit.best_source]
        if hit.media:
            tags.append(hit.media)
        if hit.lang and hit.lang != "en":
            tags.append(hit.lang)
        console.print(
            f"[bold cyan]{rank:2}.[/bold cyan] [bold]{hit.author}[/bold] "
            f"[dim]{when} · {' · '.join(tags)} · {hit.score:.4f}[/dim]"
        )
        # Show the passage that actually matched. Printing the post instead
        # hides why a hit ranked, which is the whole question being asked.
        passage = " ".join(hit.best_chunk.split())
        console.print(f"    {passage[:280]}{'...' if len(passage) > 280 else ''}")
        if hit.best_ref:
            console.print(f"    [dim]from {hit.best_ref}[/dim]")
        console.print(f"    [blue]{hit.url}[/blue]\n")


if __name__ == "__main__":
    app()
