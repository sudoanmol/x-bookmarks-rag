"""Capture Bookmarks GraphQL responses from a real browser session.

The browser makes its own requests and this module only listens. X signs each
API call with an `x-client-transaction-id` header computed by obfuscated page
JavaScript, so replaying cursors by hand is a moving target. Letting the app
page for itself means nothing here ever needs signing.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from playwright.sync_api import Response, sync_playwright

from . import config, db, parse, session

# The query ID in /i/api/graphql/{queryId}/Bookmarks changes with every X
# frontend deploy. The operation name does not, so match only on that.
OPERATION = "Bookmarks"


def is_bookmarks_response(response: Response) -> bool:
    url = response.url
    if "/i/api/graphql/" not in url:
        return False
    return url.split("?")[0].rstrip("/").endswith(f"/{OPERATION}")


@dataclass
class CaptureResult:
    run_id: str
    raw_dir: Path
    pages: int = 0
    entries: int = 0
    max_sort_index: int | None = None
    stop_reason: str = "unknown"
    paths: list[Path] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Only a run that reached a real end may advance the watermark."""
        return self.stop_reason in ("end_of_list", "watermark")


def _scroll(page) -> None:
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.mouse.wheel(0, 1200)


def _pace(page) -> None:
    page.wait_for_timeout(
        random.randint(config.SCROLL_DELAY_MIN_MS, config.SCROLL_DELAY_MAX_MS)
    )


def capture(
    *,
    full: bool = False,
    max_pages: int | None = None,
    headless: bool = True,
    on_page: Callable[[int, int, int], None] | None = None,
) -> CaptureResult:
    """Scroll the bookmarks timeline and write every raw response to disk.

    `full` ignores the watermark and walks the whole list.
    `on_page(page_number, entries_this_page, entries_total)` reports progress.
    """
    config.ensure_dirs()
    conn = db.connect()
    watermark = None if full else db.get_watermark(conn)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = config.RAW_DIR / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    result = CaptureResult(run_id=run_id, raw_dir=raw_dir)

    with sync_playwright() as pw:
        browser, context = session.open_session(pw, headless=headless)
        page = context.new_page()

        # Collect Response objects in the handler and read their bodies in the
        # main flow. Sync Playwright forbids blocking calls inside handlers.
        collected: list[Response] = []
        page.on(
            "response",
            lambda r: collected.append(r) if is_bookmarks_response(r) else None,
        )

        def wait_for_new(seen: int, timeout_ms: int) -> bool:
            waited = 0
            while len(collected) <= seen and waited < timeout_ms:
                page.wait_for_timeout(250)
                waited += 250
            return len(collected) > seen

        try:
            page.goto(config.BOOKMARKS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            session.assert_logged_in(page)

            if not wait_for_new(0, config.RESPONSE_TIMEOUT_MS):
                session.assert_logged_in(page)
                result.stop_reason = "no_first_page"
                return result

            processed = 0
            last_cursor: str | None = None
            stalls = 0

            while True:
                new_pages = []
                while processed < len(collected):
                    response = collected[processed]
                    processed += 1
                    try:
                        new_pages.append(response.json())
                    except Exception:
                        continue

                hit_before = result.stop_reason == "watermark"
                for payload in new_pages:
                    result.pages += 1
                    path = raw_dir / f"page-{result.pages:04d}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    result.paths.append(path)

                    records = [
                        record
                        for entry in parse.iter_entries(payload)
                        if (record := parse.parse_entry(entry))
                    ]
                    result.entries += len(records)
                    db.record_page(conn, str(path), run_id, len(records))
                    conn.commit()

                    for record in records:
                        sort_index = int(record["bookmark"]["sort_index"])
                        if result.max_sort_index is None or sort_index > result.max_sort_index:
                            result.max_sort_index = sort_index
                        if watermark is not None and sort_index <= watermark:
                            result.stop_reason = "watermark"

                    if on_page:
                        on_page(result.pages, len(records), result.entries)

                    if not records:
                        result.stop_reason = "end_of_list"

                    cursor = parse.bottom_cursor(payload)
                    if cursor and cursor == last_cursor:
                        result.stop_reason = "end_of_list"
                    last_cursor = cursor or last_cursor

                # A watermark hit reads one extra page for safety, then stops.
                if result.stop_reason == "watermark" and (hit_before or not new_pages):
                    break
                if result.stop_reason == "end_of_list":
                    break
                if max_pages and result.pages >= max_pages:
                    result.stop_reason = "max_pages"
                    break

                seen = len(collected)
                _pace(page)
                _scroll(page)

                if not wait_for_new(seen, config.RESPONSE_TIMEOUT_MS):
                    stalls += 1
                    if stalls >= 3:
                        # X stopped fetching. Either the list ended or the page
                        # is stuck; the cursor state above decides which.
                        result.stop_reason = (
                            "watermark" if result.stop_reason == "watermark" else "end_of_list"
                        )
                        break
                else:
                    stalls = 0
        finally:
            browser.close()
            conn.close()

    return result
