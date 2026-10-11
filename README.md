# DSE News

A minimal local workspace for downloading Dhaka Stock Exchange news as CSV.
Every collection writes **one combined CSV** with `title`, `headline`,
`description`, and `date`. Files use UTF-8-SIG so Bengali opens correctly in Excel.
Rows are sorted by title from A to Z, ignoring case.

## Setup

Python 3.10 or newer:

```powershell
python -m pip install playwright
python -m playwright install chromium
```

## Web interface

```powershell
python app.py
```

Open **http://127.0.0.1:8000**. Choose your dates and select **Generate & download
CSV**. A complete export downloads automatically when ready. Previously generated
files are available under **Saved CSVs**. Partial exports are labeled and can be
downloaded manually. The interface shows dates and progress only.
The collection continues if you refresh the page.

```powershell
python app.py --port 8080 --output ./dse_news
```

The app runs locally and uses the Python scraper; it does not need a hosted service
or any Python web framework. Stop the server with Ctrl+C.

## Command line

```powershell
python scrape.py --from 2026-10-11 --to 2026-10-11
python scrape.py --from 2026-10-01 --to 2026-10-11 --output ./news
```

The default output is `./dse_news/dse_news_<from>_<to>.csv`. Individual CSV export
is no longer supported. Use `--headed` to see the browser, `--delay` to change
the pause between fallback clicks, or `--timeout` to change the wait in milliseconds.
Incomplete runs retain the collected rows and return a nonzero exit code.

## Faster collection

The scraper captures the public news response used by the DSE page through
Playwright and exports records in bulk. Truncated date ranges are split into
smaller ranges, and duplicate records are skipped. If the feed is unavailable or
its structure changes, the scraper falls back to clicking the news panels.
Use `--click-only` to force this fallback for troubleshooting.
