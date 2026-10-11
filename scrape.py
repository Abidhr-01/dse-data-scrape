#!/usr/bin/env python3
"""Scrape JavaScript-rendered DSE news into Excel-friendly CSV files.

Requires Python 3.10+ and Playwright with Chromium:
    python -m pip install playwright
    python -m playwright install chromium

Usage:
    python scrape.py --from 2026-10-01 --to 2026-10-11
    python scrape.py --from 2026-10-01 --to 2026-10-11 --output ./news
    python scrape.py --from 2026-10-01 --to 2026-10-11 --headed
    python app.py  # Open the web interface at http://127.0.0.1:8000

The title column retains the complete title/date/company metadata line. Dates
are normalized when recognized; otherwise their displayed text is retained.
Every run writes one combined CSV with title, headline, description, and date.
Rows are sorted alphabetically by title (case-insensitive).
PDF controls are never clicked. Selectors are grouped below for easy upkeep.

Exit codes: 0 = completed (including an explicitly empty range), 1 = failed or
partial scrape, 2 = invalid arguments. Collected rows survive later failures.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightError = RuntimeError
    PlaywrightTimeout = TimeoutError


LOG = logging.getLogger("dse_news")
BASE_URL = "https://www.dse.com.bd/news"
CONTAINER = '.site-container[class*="grid"][class*="gap-6"]'
LEFT = '[class~="rounded-2xl"][class~="overflow-hidden"]'
RIGHT = '[class~="rounded-2xl"][class~="p-7"]'
ITEM = 'button[class*="text-left"][class*="px-5"][class*="py-4"]'
TITLE = '[class~="group"][class*="inline-flex"][class*="items-baseline"]'
HEADLINE = '[class*="text-[18px]"][class*="font-semibold"]'
DESCRIPTION = '[class*="text-[13.5px]"][class*="leading-"]'
FIELDS = ("title", "headline", "description", "date")
NEWS_API = "https://www.dse.com.bd/api/live/news"
EMPTY_TEXT = re.compile(
    r"\b(?:no (?:news|records|results|data|items)(?:\s+(?:items|found|available))?"
    r"|nothing found|no matches)\b", re.I
)
MORE_TEXT = re.compile(
    r"^(?:load\s+more(?:\s+news)?|show\s+more(?:\s+news)?|more\s+news"
    r"|next(?:\s+page)?|older(?:\s+(?:news|posts))?)\s*[>\u203a\u00bb\u2192]*$", re.I
)

# Read all detail fields together so they come from the same DOM snapshot.
READ_DETAIL = """(panel, s) => {
    const header = panel.querySelector(s.title);
    const headline = panel.querySelector(s.headline);
    const description = panel.querySelector(s.description);
    const time = panel.querySelector('time');
    return {
        title: header?.innerText.trim() || '',
        headline: headline?.innerText.trim() || '',
        description: description?.innerText.trim() || '',
        date: time?.getAttribute('datetime') || time?.innerText.trim() || ''
    };
}"""
DETAIL_SELECTORS = {"title": TITLE, "headline": HEADLINE, "description": DESCRIPTION}

# A changed panel must settle before extraction. An already-selected item can
# keep the same panel; in that case verify its headline against the clicked row.
# This avoids blindly sleeping and accidentally saving the preceding article.
WAIT_DETAIL = """a => {
    const panel = document.querySelector(a.panel);
    if (!panel) return false;
    const get = selector => panel.querySelector(selector)?.innerText.trim() || '';
    const value = [get(a.title), get(a.headline), get(a.description)];
    if (!value[1]) return false;
    const normalize = s => s.replace(/\\s+/g, ' ').trim().toLowerCase();
    const row = normalize(a.row);
    const matchesRow = row.includes(normalize(value[1]))
        && (!value[0] || normalize(value[0]).split(' ').filter(Boolean)
            .every(word => row.includes(word)));
    const signature = JSON.stringify(value);
    const changed = signature !== a.before;
    if (!changed && !matchesRow) return false;
    // State is per click and reset from Python before every attempt.
    const state = window.__dseDetailWait;
    if (!state || state.signature !== signature) {
        window.__dseDetailWait = {signature, since: performance.now()};
        return false;
    }
    const loading = panel.matches('[aria-busy="true"]')
        || panel.querySelector('[aria-busy="true"], [role="progressbar"]');
    return !loading && performance.now() - state.since >= a.settle;
}"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=Path("./dse_news"))
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window")
    parser.add_argument("--delay", type=float, default=0.05,
                        help="Seconds between fallback news clicks (default: 0.05)")
    parser.add_argument("--click-only", action="store_true",
                        help="Use the slower detail-panel fallback instead of bulk page data")
    parser.add_argument("--timeout", type=int, default=30000,
                        help="Maximum wait per operation in milliseconds (default: 30000)")
    parser.add_argument("--max-pages", type=int, default=1000,
                        help="Safety limit for pagination/load-more batches (default: 1000)")
    args = parser.parse_args(argv)
    for name in ("start", "end"):
        value = getattr(args, name)
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
                raise ValueError
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            parser.error(f"--{'from' if name == 'start' else 'to'} must be a valid YYYY-MM-DD date")
    if args.start > args.end:
        parser.error("--from must be on or before --to")
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay must be a finite nonnegative number")
    if args.timeout <= 0 or args.max_pages <= 0:
        parser.error("--timeout and --max-pages must be positive")
    return args


def extract_date(text):
    """Recognize ISO and common English/Bengali-digit dates without guessing MDY."""
    text = text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    patterns = (
        (r"\b\d{4}-\d{2}-\d{2}(?=$|[T\s|,])", ("%Y-%m-%d",)),
        (r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b", ("%d/%m/%Y", "%d-%m-%Y")),
        (r"\b\d{1,2}[\s-]+[A-Za-z]{3,9}[\s,-]+\d{4}\b", ("%d %b %Y", "%d %B %Y")),
        (r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b", ("%b %d %Y", "%B %d %Y")),
    )
    for pattern, formats in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group()
        for fmt in formats:
            candidate = re.sub(r"[,-]+", " ", raw) if "%b" in fmt or "%B" in fmt else raw
            try:
                return datetime.strptime(" ".join(candidate.split()), fmt).date().isoformat()
            except ValueError:
                pass
        return raw
    return ""


def api_records(payload, start, end):
    """Validate the public news feed before using it instead of clicking rows.

    Keep the publication date supplied by DSE; a company name may contain a
    bond maturity date, which is not the date of the announcement.
    """
    if (not isinstance(payload, dict) or not isinstance(payload.get("rows"), list)
            or type(payload.get("truncated")) is not bool):
        raise ValueError("Unrecognized DSE news feed")
    rows = []
    for item in payload["rows"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("code", "name", "summary", "body")
        ):
            raise ValueError("Incomplete DSE news record")
        raw_date = item.get("filedAt", "") or item.get("date", "")
        if not isinstance(raw_date, str):
            raise ValueError("Missing DSE publication date")
        published = extract_date(raw_date)
        if not published or not start <= published <= end or not item["summary"].strip():
            raise ValueError("DSE news record does not match the requested range")
        rows.append({"title": "\n".join(p.strip() for p in (item["code"], item["name"]) if p.strip()),
                     "headline": item["summary"].strip(), "description": item["body"].strip(),
                     "date": published})
    return rows


def collect_api(context, payload, args, rows):
    """Export complete feed batches; split truncated date ranges adaptively.

    A single-day truncation is reported as partial instead of silently claiming
    a complete export. The request limit also protects against endless batching.
    """
    pending = [(args.start, args.end, payload)]
    seen = set()
    failures = 0
    requests = 0
    while pending:
        if requests >= args.max_pages:
            LOG.error("Reached --max-pages with more date batches remaining")
            return failures + 1
        start, end, data = pending.pop()
        requests += 1
        try:
            if data is None:
                url = NEWS_API + "?" + urlencode({"from": start, "to": end})
                response = context.request.get(url, timeout=args.timeout)
                try:
                    if not response.ok:
                        raise ValueError(f"DSE news feed returned HTTP {response.status}")
                    data = response.json()
                finally:
                    response.dispose()
            batch = api_records(data, start, end)
        except (PlaywrightError, ValueError) as exc:
            failures += 1
            LOG.error("Could not read date batch %s to %s: %s", start, end, exc)
            continue
        if data["truncated"] and start < end:
            first = datetime.strptime(start, "%Y-%m-%d").date()
            last = datetime.strptime(end, "%Y-%m-%d").date()
            middle = first + (last - first) // 2
            pending.extend([(str(middle + timedelta(days=1)), end, None),
                            (start, str(middle), None)])
            LOG.info("Splitting a truncated date range: %s to %s", start, end)
            continue
        if data["truncated"]:
            failures += 1
            LOG.error("DSE truncated the single-day feed for %s; exporting available rows", start)
        for row in batch:
            key = tuple(" ".join(row[k].split()) for k in FIELDS)
            if key not in seen:
                rows.append(row)
                seen.add(key)
        LOG.info("Bulk data: %d announcements collected", len(rows))
    return failures


def read_detail(panel):
    return panel.evaluate(READ_DETAIL, DETAIL_SELECTORS) if panel.count() else {
        "title": "", "headline": "", "description": "", "date": ""
    }


def list_snapshot(buttons):
    """Include IDs when exposed, plus row text; keep order for load-more detection."""
    return buttons.evaluate_all("""nodes => nodes.map(n => [
        n.getAttribute('data-news-id') || n.getAttribute('data-id') || n.id,
        n.innerText.trim()
    ])""")


def scrape_item(page, button, panel, args):
    row_text = button.inner_text().strip()
    row_date = button.locator("time").first
    date_hint = ""
    if row_date.count():
        date_hint = row_date.get_attribute("datetime") or row_date.inner_text()
    before = read_detail(panel)
    # JSON.stringify on the browser side ensures identical Unicode serialization.
    signature = page.evaluate("v => JSON.stringify(v)",
                              [before[k] for k in ("title", "headline", "description")])
    last_error = None
    for attempt in range(2):
        try:
            page.evaluate("() => { window.__dseDetailWait = null; }")
            button.click()
            page.wait_for_function(WAIT_DETAIL, arg={
                "panel": f"{CONTAINER} {RIGHT}", **DETAIL_SELECTORS,
                "before": signature, "row": row_text, "settle": 500,
            }, timeout=args.timeout, polling=100)
            detail = read_detail(panel)
            if not detail["headline"]:
                raise ValueError("The selected item has no headline")
            for field in ("title", "description"):
                if not detail[field]:
                    LOG.warning("Missing %s for %s", field, detail["headline"][:80])
            raw_date = detail["date"] or date_hint
            item_date = extract_date(raw_date) or extract_date(row_text) or extract_date(detail["title"])
            item_date = item_date or raw_date
            return {k: detail[k] for k in ("title", "headline", "description")} | {
                "date": item_date
            }
        except (PlaywrightError, ValueError) as exc:
            last_error = exc
            LOG.warning("Item attempt %d/2 failed: %s", attempt + 1, str(exc).splitlines()[0])
    raise ValueError(f"Could not read item {row_text[:100]!r}: {last_error}")


def enabled_control(locator):
    for index in range(locator.count()):
        control = locator.nth(index)
        if (control.is_visible() and control.is_enabled()
                and control.get_attribute("aria-disabled") != "true"
                and not control.evaluate("e => !!e.closest('[inert], [aria-disabled=\"true\"]')")):
            return control
    return None


def find_next(container):
    """Prefer semantic controls; never click an arbitrary button or a PDF control."""
    candidates = [
        container.locator('a[rel~="next"], button[rel~="next"]'),
        container.get_by_role("button", name=MORE_TEXT),
        container.get_by_role("link", name=MORE_TEXT),
        container.locator('button[aria-label*="next" i], a[aria-label*="next" i]'),
        container.get_by_role("button", name=re.compile(r"^[>\u203a\u00bb\u2192]$")),
        container.get_by_role("link", name=re.compile(r"^[>\u203a\u00bb\u2192]$")),
    ]
    for candidate in candidates:
        control = enabled_control(candidate)
        if control is not None:
            return control
    # Numbered pagination occasionally has no explicit Next button.
    current = container.locator('[aria-current="page"]')
    if current.count() == 1 and current.inner_text().strip().isdigit():
        number = str(int(current.inner_text().strip()) + 1)
        pager = current.locator("..")
        for role in ("button", "link"):
            control = enabled_control(pager.get_by_role(role, name=number, exact=True))
            if control is not None:
                return control
    return None


def advance(page, container, buttons, previous, args):
    control = find_next(container)
    if control is None:
        return False
    control.click()
    # Pagination can replace the rows, while load-more appends to them. Do not
    # use networkidle: unrelated background requests may never become idle.
    page.wait_for_function("""a => {
        const nodes = [...document.querySelectorAll(a.selector)];
        const snapshot = nodes.map(n => [
            n.getAttribute('data-news-id') || n.getAttribute('data-id') || n.id,
            n.innerText.trim()
        ]);
        const changed = snapshot.length && JSON.stringify(snapshot) !== JSON.stringify(a.before);
        return changed;
    }""", arg={"selector": f"{CONTAINER} {LEFT} {ITEM}", "before": previous},
        timeout=args.timeout, polling=100)
    # Wait for incremental rendering to settle before taking the next snapshot.
    page.wait_for_function("""selector => {
        const nodes = [...document.querySelectorAll(selector)];
        const value = JSON.stringify(nodes.map(n => n.innerText));
        const old = window.__dseListWait;
        if (!old || old.value !== value) {
            window.__dseListWait = {value, since: performance.now()};
            return false;
        }
        return nodes.length > 0 && performance.now() - old.since >= 500;
    }""", arg=f"{CONTAINER} {LEFT} {ITEM}", timeout=args.timeout, polling=100)
    return True


def scrape(args, rows):
    """Mutate rows as we go so partial results are retained on errors/interrupts."""
    url = BASE_URL + "?" + urlencode({"from": args.start, "to": args.end})
    seen_rows = set()
    seen_lists = set()
    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        try:
            context = browser.new_context(accept_downloads=False,
                                          viewport={"width": 1440, "height": 1000})
            context.route("**/*", lambda route: route.abort() if route.request.resource_type
                          in ("image", "media", "font") else route.continue_())
            page = context.new_page()
            page.set_default_timeout(args.timeout)
            LOG.info("Opening %s", url)
            response = None
            if not args.click_only:
                # Capture the very same public response used by DSE's news page.
                # This avoids N clicks, N panel waits, and N throttling delays.
                def is_news_response(result):
                    parsed = urlsplit(result.url)
                    query = parse_qs(parsed.query)
                    return (parsed.netloc == "www.dse.com.bd" and parsed.path == "/api/live/news"
                            and query.get("from") == [args.start] and query.get("to") == [args.end])
                try:
                    with page.expect_response(is_news_response, timeout=min(args.timeout, 10000)) as captured:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
                    feed = captured.value
                    if not feed.ok:
                        raise ValueError(f"DSE news feed returned HTTP {feed.status}")
                    payload = feed.json()
                    api_records(payload, args.start, args.end)
                    LOG.info("Using bulk news data from the DSE page")
                except (PlaywrightError, ValueError) as exc:
                    LOG.warning("Bulk news unavailable; using detail-panel fallback: %s",
                                str(exc).splitlines()[0])
                else:
                    return collect_api(context, payload, args, rows)
            if response is None:
                response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
            if response and response.status >= 400:
                raise ValueError(f"DSE returned HTTP {response.status}")
            container = page.locator(CONTAINER).first
            container.wait_for(state="visible")
            buttons = container.locator(f"{LEFT} {ITEM}")
            panel = container.locator(RIGHT).first
            try:
                buttons.first.wait_for(state="visible")
            except PlaywrightTimeout:
                if EMPTY_TEXT.search(container.inner_text()):
                    LOG.warning("No news found for the requested range")
                    return 0
                raise ValueError("No news list appeared; check selectors, site access, or --headed")

            start_index = 0
            for batch in range(1, args.max_pages + 1):
                snapshot = list_snapshot(buttons)
                list_key = repr(snapshot)
                if list_key in seen_lists:
                    LOG.error("Pagination repeated a previously visited list; stopping")
                    return failures + 1
                seen_lists.add(list_key)
                LOG.info("Batch %d: %d news buttons (%d new)",
                         batch, len(snapshot), len(snapshot) - start_index)
                for index in range(start_index, len(snapshot)):
                    LOG.info("Reading item %d/%d; %d unique rows saved so far",
                             index + 1, len(snapshot), len(rows))
                    try:
                        row = scrape_item(page, buttons.nth(index), panel, args)
                        key = tuple(" ".join(row[k].split()) for k in FIELDS)
                        if key not in seen_rows:
                            rows.append(row)
                            seen_rows.add(key)
                        else:
                            LOG.info("Skipping duplicate: %s", row["headline"][:80])
                    except (PlaywrightError, ValueError) as exc:
                        failures += 1
                        LOG.error("Skipping item %d: %s", index + 1, str(exc).splitlines()[0])
                    # Intentional throttling, separate from readiness waits.
                    page.wait_for_timeout(args.delay * 1000)
                page.evaluate("() => { window.__dseListWait = null; }")
                if batch == args.max_pages:
                    if find_next(container) is not None:
                        LOG.error("Reached --max-pages with more news available")
                        failures += 1
                    break
                if not advance(page, container, buttons, snapshot, args):
                    break
                updated = list_snapshot(buttons)
                # Process only appended rows for cumulative load-more lists.
                start_index = len(snapshot) if (
                    len(updated) > len(snapshot) and updated[:len(snapshot)] == snapshot
                ) else 0
            return failures
        finally:
            browser.close()


def sort_by_title(rows):
    """Sort whole records A–Z, preserving the order of matching titles."""
    return sorted(rows, key=lambda row: " ".join((row.get("title") or "").split()).casefold())


def write_csv(path, rows):
    # newline='' prevents blank rows on Windows; the BOM helps Excel read Bengali.
    # Atomic replacement prevents an interrupted write from corrupting an old CSV.
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sort_by_title(rows))
    temporary.replace(path)


def save_results(args, rows):
    args.output.mkdir(parents=True, exist_ok=True)
    stem = f"dse_news_{args.start}_{args.end}"
    combined = args.output / f"{stem}.csv"
    write_csv(combined, rows)
    LOG.info("Wrote %d unique items to %s", len(rows), combined.resolve())


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sync_playwright is None:
        LOG.error("Install Playwright: python -m pip install playwright; "
                  "then python -m playwright install chromium")
        return 1
    rows = []
    status = 0
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        status = 1 if scrape(args, rows) else 0
    except KeyboardInterrupt:
        LOG.warning("Interrupted; preserving collected rows")
        status = 1
    except (PlaywrightError, OSError, ValueError) as exc:
        LOG.error("Scrape stopped: %s", exc)
        status = 1
    try:
        save_results(args, rows)
    except OSError as exc:
        LOG.error("Could not save CSV files: %s", exc)
        return 1
    if status:
        LOG.warning("Scrape incomplete; exported %d collected rows", len(rows))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
