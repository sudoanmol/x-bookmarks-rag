"""The measurement gate.

Phase 2 is designed against these numbers, not against guesses. In particular
the video totals decide whether transcribing every clip is sensible.
"""

from __future__ import annotations

import sqlite3
from statistics import median

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config, db

# Groq free tier, whisper-large-v3-turbo: audio seconds per day.
GROQ_AUDIO_SECONDS_PER_DAY = 28_800

ALIVE = "removed_at IS NULL"


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def gather(conn: sqlite3.Connection) -> dict:
    stats: dict = {}

    stats["total"] = _scalar(conn, f"SELECT COUNT(*) FROM bookmarks WHERE {ALIVE}")
    stats["removed"] = _scalar(conn, "SELECT COUNT(*) FROM bookmarks WHERE removed_at IS NOT NULL")
    stats["authors"] = _scalar(conn, "SELECT COUNT(*) FROM authors")
    stats["raw_pages"] = _scalar(conn, "SELECT COUNT(*) FROM raw_pages")
    stats["watermark"] = db.get_watermark(conn)
    stats["last_sync"] = db.get_state(conn, "last_sync_at")

    span = conn.execute(
        f"SELECT MIN(created_at), MAX(created_at) FROM bookmarks WHERE {ALIVE} AND created_at IS NOT NULL"
    ).fetchone()
    stats["oldest_post"], stats["newest_post"] = (span[0], span[1]) if span else (None, None)

    # Text
    lengths = [
        r[0]
        for r in _rows(conn, f"SELECT LENGTH(text) FROM bookmarks WHERE {ALIVE} AND text != ''")
    ]
    stats["text_median"] = int(median(lengths)) if lengths else 0
    stats["text_total_chars"] = sum(lengths)
    stats["long_posts"] = _scalar(conn, f"SELECT COUNT(*) FROM bookmarks WHERE {ALIVE} AND is_long = 1")
    stats["empty_text"] = _scalar(conn, f"SELECT COUNT(*) FROM bookmarks WHERE {ALIVE} AND text = ''")
    stats["with_quote"] = _scalar(
        conn, f"SELECT COUNT(*) FROM bookmarks WHERE {ALIVE} AND quoted_tweet_id IS NOT NULL"
    )

    # Language, which drives the translation stage
    stats["languages"] = _rows(
        conn,
        f"""SELECT COALESCE(lang, 'unknown') AS lang, COUNT(*) AS n
            FROM bookmarks WHERE {ALIVE} GROUP BY lang ORDER BY n DESC""",
    )
    stats["non_english"] = _scalar(
        conn,
        f"""SELECT COUNT(*) FROM bookmarks
            WHERE {ALIVE} AND lang IS NOT NULL AND lang NOT IN ('en', 'und', 'qme', 'qst', 'zxx')""",
    )

    # Media
    stats["media_by_kind"] = _rows(
        conn,
        f"""SELECT m.kind, COUNT(*) AS n FROM media m
            JOIN bookmarks b USING(tweet_id) WHERE b.{ALIVE}
            GROUP BY m.kind ORDER BY n DESC""",
    )
    stats["with_media"] = _scalar(
        conn,
        f"""SELECT COUNT(DISTINCT m.tweet_id) FROM media m
            JOIN bookmarks b USING(tweet_id) WHERE b.{ALIVE}""",
    )
    stats["with_alt"] = _scalar(
        conn,
        f"""SELECT COUNT(*) FROM media m JOIN bookmarks b USING(tweet_id)
            WHERE b.{ALIVE} AND m.alt_text IS NOT NULL AND m.alt_text != ''""",
    )
    stats["photos"] = next(
        (r["n"] for r in stats["media_by_kind"] if r["kind"] == "photo"), 0
    )
    stats["caption_ok"] = _scalar(
        conn, "SELECT COUNT(*) FROM captions WHERE error IS NULL AND text != ''"
    )
    stats["caption_failed"] = _scalar(
        conn, "SELECT COUNT(*) FROM captions WHERE error IS NOT NULL"
    )

    videos = _rows(
        conn,
        f"""SELECT m.duration_ms, m.bitrate, m.small_bitrate
            FROM media m JOIN bookmarks b USING(tweet_id)
            WHERE b.{ALIVE} AND m.kind IN ('video', 'animated_gif') AND m.duration_ms > 0""",
    )
    durations = [r[0] / 1000 for r in videos]
    stats["video_count"] = len(durations)
    stats["video_seconds"] = sum(durations)
    stats["video_median_s"] = median(durations) if durations else 0
    stats["video_max_s"] = max(durations) if durations else 0
    stats["video_bytes_best"] = sum((r[1] or 0) * (r[0] / 1000) / 8 for r in videos)
    stats["video_bytes"] = sum((r[2] or r[1] or 0) * (r[0] / 1000) / 8 for r in videos)
    stats["long_videos"] = sum(1 for d in durations if d > 600)

    # Articles carry only a title and a short preview here. The body needs a
    # second visit to each post.
    stats["articles"] = _scalar(
        conn, f"SELECT COUNT(*) FROM bookmarks WHERE {ALIVE} AND article_id IS NOT NULL"
    )

    # Links
    stats["link_total"] = _scalar(
        conn, f"SELECT COUNT(*) FROM links l JOIN bookmarks b USING(tweet_id) WHERE b.{ALIVE}"
    )
    stats["link_domains"] = _scalar(
        conn,
        f"SELECT COUNT(DISTINCT domain) FROM links l JOIN bookmarks b USING(tweet_id) WHERE b.{ALIVE}",
    )
    stats["top_domains"] = _rows(
        conn,
        f"""SELECT l.domain, COUNT(*) AS n FROM links l JOIN bookmarks b USING(tweet_id)
            WHERE b.{ALIVE} AND l.domain IS NOT NULL
            GROUP BY l.domain ORDER BY n DESC LIMIT 15""",
    )
    stats["top_authors"] = _rows(
        conn,
        f"""SELECT a.screen_name, COUNT(*) AS n FROM bookmarks b
            JOIN authors a USING(author_id) WHERE b.{ALIVE}
            GROUP BY a.screen_name ORDER BY n DESC LIMIT 15""",
    )
    return stats


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def render(stats: dict, console: Console | None = None) -> None:
    console = console or Console()
    photos = stats["photos"]

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column()
    overview.add_row("Bookmarks", f"{stats['total']:,}")
    overview.add_row("Removed (soft)", f"{stats['removed']:,}")
    overview.add_row("Distinct authors", f"{stats['authors']:,}")
    overview.add_row("Raw pages on disk", f"{stats['raw_pages']:,}")
    overview.add_row("Post dates", f"{(stats['oldest_post'] or '?')[:10]} to {(stats['newest_post'] or '?')[:10]}")
    overview.add_row("Last sync", stats["last_sync"] or "never")
    console.print(Panel(overview, title="Overview", border_style="cyan"))

    text = Table.grid(padding=(0, 2))
    text.add_column(style="bold")
    text.add_column()
    text.add_row("Median length", f"{stats['text_median']:,} chars")
    text.add_row("Total text", f"{stats['text_total_chars']:,} chars")
    text.add_row("Long posts (note_tweet)", f"{stats['long_posts']:,}")
    text.add_row("With a quoted post", f"{stats['with_quote']:,}")
    text.add_row("Empty text", f"{stats['empty_text']:,}")
    text.add_row("X Articles (body not in payload)", f"{stats['articles']:,}")
    console.print(Panel(text, title="Text", border_style="cyan"))

    langs = Table(show_header=True, header_style="bold")
    langs.add_column("Language")
    langs.add_column("Posts", justify="right")
    for row in stats["languages"][:12]:
        langs.add_row(row["lang"], f"{row['n']:,}")
    console.print(
        Panel(
            langs,
            title=f"Languages — {stats['non_english']:,} posts need translation",
            border_style="cyan",
        )
    )

    media = Table(show_header=True, header_style="bold")
    media.add_column("Kind")
    media.add_column("Items", justify="right")
    for row in stats["media_by_kind"]:
        media.add_row(row["kind"], f"{row['n']:,}")
    media.add_row("[dim]posts with media[/dim]", f"[dim]{stats['with_media']:,}[/dim]")
    media.add_row("[dim]items with alt text[/dim]", f"[dim]{stats['with_alt']:,}[/dim]")
    console.print(Panel(media, title="Media", border_style="cyan"))

    if stats["video_count"]:
        quota_days = stats["video_seconds"] / GROQ_AUDIO_SECONDS_PER_DAY
        video = Table.grid(padding=(0, 2))
        video.add_column(style="bold")
        video.add_column()
        video.add_row("Clips", f"{stats['video_count']:,}")
        video.add_row("Total runtime", _fmt_duration(stats["video_seconds"]))
        video.add_row("Median clip", _fmt_duration(stats["video_median_s"]))
        video.add_row("Longest clip", _fmt_duration(stats["video_max_s"]))
        video.add_row("Clips over 10 min", f"{stats['long_videos']:,}")
        video.add_row("Download, smallest variant", _fmt_bytes(stats["video_bytes"]))
        video.add_row(
            "[dim]Download, best variant[/dim]",
            f"[dim]{_fmt_bytes(stats['video_bytes_best'])} — never needed for speech[/dim]",
        )
        video.add_row(
            "Groq free transcription",
            f"{quota_days:.2f} of one day's quota ({GROQ_AUDIO_SECONDS_PER_DAY:,}s/day)",
        )
        console.print(Panel(video, title="Video — the phase 2 decision", border_style="yellow"))

    links = Table(show_header=True, header_style="bold")
    links.add_column("Domain")
    links.add_column("Links", justify="right")
    for row in stats["top_domains"]:
        links.add_row(row["domain"], f"{row['n']:,}")
    console.print(
        Panel(
            links,
            title=f"Links — {stats['link_total']:,} total across {stats['link_domains']:,} domains",
            border_style="cyan",
        )
    )

    authors = Table(show_header=True, header_style="bold")
    authors.add_column("Author")
    authors.add_column("Bookmarks", justify="right")
    for row in stats["top_authors"]:
        authors.add_row(f"@{row['screen_name']}", f"{row['n']:,}")
    console.print(Panel(authors, title="Top authors", border_style="cyan"))

    work = Table.grid(padding=(0, 2))
    work.add_column(style="bold")
    work.add_column()
    work.add_row(
        "Image captions",
        f"{stats['caption_ok']:,} of {photos:,} photos"
        + (f"  ({stats['caption_failed']} failed)" if stats["caption_failed"] else ""),
    )
    work.add_row("Transcription", f"{_fmt_duration(stats['video_seconds'])} of audio")
    work.add_row("Article fetches", f"{stats['link_total']:,} external URLs")
    work.add_row("X Article bodies", f"{stats['articles']:,} second-pass visits")
    work.add_row("Translations", f"{stats['non_english']:,} posts")
    work.add_row("Groq key", "present" if config.GROQ_API_KEY else "[red]missing from .env[/red]")
    console.print(Panel(work, title="Phase 2 workload estimate", border_style="magenta"))
