"""Command line entry point."""

from __future__ import annotations

import typer
from playwright.sync_api import sync_playwright
from rich.console import Console

from . import capture as capture_mod
from . import config, db, inspect as inspect_mod, normalize as normalize_mod, session

app = typer.Typer(
    add_completion=False,
    help="Capture, normalize, and inspect your X bookmarks.",
    no_args_is_help=True,
)
console = Console()


@app.command()
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


@app.command()
def sync(
    full: bool = typer.Option(False, "--full", help="Walk every bookmark and detect removals."),
    max_pages: int = typer.Option(0, "--max-pages", help="Stop after N pages. 0 means no limit."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
) -> None:
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


@app.command()
def normalize() -> None:
    """Rebuild the database from every raw page already on disk."""
    result = normalize_mod.normalize()
    console.print(
        f"[green]Read {result.pages} pages: {result.bookmarks} posts, "
        f"{result.media} media items, {result.links} links.[/green]"
    )
    for path in result.failed_pages:
        console.print(f"[red]Unreadable: {path}[/red]")


@app.command()
def inspect() -> None:
    """Report what the captured data actually contains."""
    conn = db.connect()
    stats = inspect_mod.gather(conn)
    conn.close()
    if not stats["total"]:
        console.print("[yellow]No bookmarks stored yet. Run `xbm sync` first.[/yellow]")
        raise typer.Exit(1)
    inspect_mod.render(stats, console)


@app.command()
def status() -> None:
    """Show session and sync state."""
    conn = db.connect()
    console.print(f"Session file : {config.STATE_PATH} "
                  f"{'[green]present[/green]' if session.session_exists() else '[red]missing[/red]'}")
    console.print(f"Database     : {config.DB_PATH}")
    console.print(f"Raw pages    : {config.RAW_DIR}")
    alive = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE removed_at IS NULL").fetchone()[0]
    console.print(f"Bookmarks    : {alive}")
    console.print(f"Watermark    : {db.get_watermark(conn) or 'not set'}")
    console.print(f"Last sync    : {db.get_state(conn, 'last_sync_at') or 'never'}")
    console.print(f"Groq key     : {'[green]set[/green]' if config.GROQ_API_KEY else '[red]missing[/red]'}")
    conn.close()


if __name__ == "__main__":
    app()
