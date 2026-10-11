#!/usr/bin/env python3
"""Local web interface for the DSE scraper (no additional Python packages).

Setup: python -m pip install playwright
       python -m playwright install chromium
Run:   python app.py
Open:  http://127.0.0.1:8000

Optional: python app.py --port 8080 --output ./dse_news
The server binds to loopback and runs the existing scraper in a background
process so progress requests stay responsive during long collections.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scrape import FIELDS, sort_by_title

ROOT = Path(__file__).resolve().parent
EXPORT_NAME = re.compile(r"dse_news_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv\Z")
ASSETS = {"/": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"),
          "/app.js": ("app.js", "text/javascript")}
LOG = logging.getLogger("dse_app")


def valid_range(start, end):
    for value in (start, end):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Choose valid start and end dates.")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Choose valid start and end dates.") from exc
    if start > end:
        raise ValueError("The start date must be on or before the end date.")


class Workspace:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None
        self.process = None

    def export_path(self, name):
        # Only known combined CSV names can be read; no arbitrary file paths.
        if not EXPORT_NAME.fullmatch(name):
            raise FileNotFoundError("Export not found.")
        path = (self.output / name).resolve()
        if path.parent != self.output or not path.is_file():
            raise FileNotFoundError("Export not found.")
        return path

    def read_export(self, name):
        path = self.export_path(name)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [{key: row.get(key, "") for key in FIELDS} for row in csv.DictReader(handle)]
        return sort_by_title(rows)

    def exports(self):
        items = []
        for path in self.output.glob("dse_news_*.csv"):
            match = EXPORT_NAME.fullmatch(path.name)
            if match:
                items.append({"filename": path.name, "start": match[1], "end": match[2],
                              "modified": path.stat().st_mtime})
        return sorted(items, key=lambda item: item["modified"], reverse=True)

    def start(self, start, end):
        valid_range(start, end)
        with self.lock:
            if self.active:
                raise RuntimeError("A collection is already running. Please wait for it to finish.")
            # Keep a small bounded history for polling after a run finishes.
            if len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "state": "running", "start": start, "end": end,
                   "current": 0, "total": 0, "count": 0,
                   "message": "Connecting to DSE…", "filename": None}
            self.jobs[job_id] = job
            self.active = job_id
        threading.Thread(target=self.run, args=(job_id,), daemon=True).start()
        return dict(job)

    def status(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise FileNotFoundError("Collection not found.")
            return dict(self.jobs[job_id])

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def run(self, job_id):
        job = self.status(job_id)
        filename = f"dse_news_{job['start']}_{job['end']}.csv"
        written = False
        last_error = "The collection could not finish. Please try again."
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scrape.py"), "--from", job["start"],
                 "--to", job["end"], "--output", str(self.output)],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            with self.lock:
                self.process = process
            for line in process.stdout:
                LOG.info("%s", line.strip())
                progress = re.search(r"Reading item (\d+)/(\d+); (\d+) unique", line)
                saved = re.search(r"Wrote (\d+) unique items to", line)
                bulk = re.search(r"Bulk data: (\d+) announcements collected", line)
                if progress:
                    current, total, count = map(int, progress.groups())
                    self.update(job_id, current=current, total=total, count=count,
                                message=f"Reading announcement {current} of {total}…")
                if saved:
                    written = True
                    self.update(job_id, count=int(saved[1]))
                if bulk:
                    self.update(job_id, count=int(bulk[1]),
                                message="Preparing your CSV download…")
                if " ERROR " in line:
                    last_error = line.split(" ERROR ", 1)[1].strip()
            return_code = process.wait()
            process.stdout.close()
            count = len(self.read_export(filename)) if written else 0
            if return_code == 0 and written:
                self.update(job_id, state="complete", count=count, filename=filename,
                            message=f"Collected {count} announcements. Your CSV is ready.")
            elif written and count:
                self.update(job_id, state="partial", count=count, filename=filename,
                            message=f"Collected {count} announcements; some items could not be read.")
            else:
                self.update(job_id, state="failed", message=last_error)
        except (OSError, ValueError, csv.Error) as exc:
            LOG.exception("Collection failed")
            self.update(job_id, state="failed", message=str(exc))
        finally:
            with self.lock:
                self.active = None
                self.process = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        LOG.debug(format, *args)

    @property
    def workspace(self):
        return self.server.workspace

    def send_bytes(self, data, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; "
                         "script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, payload, status=200):
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                self.send_bytes((ROOT / "web" / filename).read_bytes(), content_type + "; charset=utf-8")
            elif path == "/api/exports":
                today = datetime.now(timezone(timedelta(hours=6))).date().isoformat()
                self.json({"exports": self.workspace.exports(), "today": today,
                           "active": self.workspace.active})
            elif path.startswith("/api/exports/"):
                filename = path.removeprefix("/api/exports/")
                self.json({"filename": filename, "rows": self.workspace.read_export(filename)})
            elif path.startswith("/api/jobs/"):
                self.json(self.workspace.status(path.removeprefix("/api/jobs/")))
            elif path.startswith("/download/"):
                filename = path.removeprefix("/download/")
                # Project legacy files onto the four public fields as well.
                import io
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(self.workspace.read_export(filename))
                self.send_bytes(stream.getvalue().encode("utf-8-sig"),
                                "text/csv; charset=utf-8", filename=filename)
            else:
                self.json({"error": "Not found."}, 404)
        except FileNotFoundError:
            self.json({"error": "Not found."}, 404)
        except (OSError, csv.Error) as exc:
            LOG.warning("Could not read export: %s", exc)
            self.json({"error": "Could not read this export. Please try again."}, 500)

    def do_POST(self):
        if self.path != "/api/scrape":
            self.json({"error": "Not found."}, 404)
            return
        # A custom header and same-origin check prevent other sites starting jobs.
        origin = self.headers.get("Origin")
        if (self.headers.get("X-Requested-With") != "DSEWorkspace"
                or (origin and origin != f"http://{self.headers.get('Host')}")):
            self.json({"error": "Open the app in your local browser to collect news."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            job = self.workspace.start(data.get("start"), data.get("end"))
            self.json(job, 202)
        except (ValueError, UnicodeDecodeError) as exc:
            self.json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self.json({"error": str(exc)}, 409)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, default=ROOT / "dse_news")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.workspace = Workspace(args.output)
    print(f"DSE News · http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with server.workspace.lock:
            process = server.workspace.process
        if process and process.poll() is None:
            process.terminate()
        server.server_close()


if __name__ == "__main__":
    main()
