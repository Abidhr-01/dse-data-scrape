"""Run with: python -m unittest discover -s tests -v."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import app
import scrape


ROWS = [{"title": "AAA\nCompany A", "headline": "Company announcement",
         "description": 'বাংলা, "news"\nSecond line', "date": "2026-10-11"}]
NAME = "dse_news_2026-10-11_2026-10-11.csv"


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.ROOT)
        self.output = Path(self.temp.name)
        scrape.write_csv(self.output / NAME, ROWS)
        self.workspace = app.Workspace(self.output)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.server.workspace = self.workspace
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        request = Request(self.base + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read()

    def test_export_unicode_schema_and_download(self):
        status, _, body = self.request("/api/exports/" + NAME)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"], ROWS)
        status, headers, body = self.request("/download/" + NAME)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        self.assertEqual(reader.fieldnames, [scrape.CSV_HEADERS[k] for k in scrape.FIELDS])
        self.assertEqual(list(reader), [scrape.to_csv_row(r) for r in ROWS])
        self.assertIn(NAME, headers["Content-Disposition"])

    def test_validation_and_origin_protection(self):
        headers = {"Content-Type": "application/json", "X-Requested-With": "DSEWorkspace"}
        for start, end in (("2026-10-12", "2026-10-11"), ("2026-02-30", "2026-03-01")):
            status, _, _ = self.request("/api/scrape", {"start": start, "end": end}, headers)
            self.assertEqual(status, 400)
        payload = {"start": "2026-10-11", "end": "2026-10-11"}
        self.assertEqual(self.request("/api/scrape", payload)[0], 403)
        headers["Origin"] = "https://unrelated.example"
        self.assertEqual(self.request("/api/scrape", payload, headers)[0], 403)

    def test_missing_export_and_path_traversal(self):
        self.assertEqual(self.request("/download/%2e%2e%2fapp.py")[0], 404)
        self.assertEqual(self.request("/api/jobs/not-a-job")[0], 404)
        self.assertEqual(self.request("/app.py")[0], 404)

    def test_combined_export_creates_no_item_directory(self):
        args = scrape.parse_args(["--from", "2026-10-11", "--to", "2026-10-11",
                                  "--output", str(self.output / "fresh")])
        scrape.save_results(args, ROWS)
        self.assertEqual([p.name for p in args.output.iterdir()], [NAME])

    def test_background_job_progress_concurrency_and_completion(self):
        entered = threading.Event()
        release = threading.Event()
        output = self.output

        class ProgressStream:
            def __iter__(self):
                yield "INFO Reading item 1/1; 0 unique rows saved so far\n"
                entered.set()
                release.wait(5)
                scrape.write_csv(output / NAME, ROWS)
                yield "INFO Wrote 1 unique items to file\n"
            def close(self):
                pass

        class Process:
            stdout = ProgressStream()
            def wait(self):
                return 0

        with patch.object(app.subprocess, "Popen", return_value=Process()) as launch:
            job = self.workspace.start("2026-10-11", "2026-10-11")
            self.assertTrue(entered.wait(3))
            state = self.workspace.status(job["id"])
            self.assertEqual((state["state"], state["current"], state["total"]), ("running", 1, 1))
            with self.assertRaises(RuntimeError):
                self.workspace.start("2026-10-11", "2026-10-11")
            release.set()
            self.wait_for_job(job["id"])
            state = self.workspace.status(job["id"])
            self.assertEqual((state["state"], state["filename"], state["count"]), ("complete", NAME, 1))
            self.assertNotIn("--split", launch.call_args.args[0])

    def test_failed_run_does_not_offer_an_old_export_as_new(self):
        class Process:
            stdout = io.StringIO("2026-10-11 ERROR Install Playwright first\n")
            def wait(self):
                return 1
        with patch.object(app.subprocess, "Popen", return_value=Process()):
            job = self.workspace.start("2026-10-11", "2026-10-11")
            self.wait_for_job(job["id"])
            state = self.workspace.status(job["id"])
            self.assertEqual(state["state"], "failed")
            self.assertIsNone(state["filename"])
            self.assertIn("Playwright", state["message"])

    def test_partial_run_offers_preserved_rows(self):
        class Process:
            stdout = io.StringIO("INFO Wrote 1 unique items to file\n")
            def wait(self):
                return 1
        with patch.object(app.subprocess, "Popen", return_value=Process()):
            job = self.workspace.start("2026-10-11", "2026-10-11")
            self.wait_for_job(job["id"])
            state = self.workspace.status(job["id"])
            self.assertEqual((state["state"], state["count"], state["filename"]), ("partial", 1, NAME))

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if self.workspace.status(job_id)["state"] != "running" and self.workspace.active is None:
                return
            time.sleep(0.01)
        self.fail("Background job did not finish")


if __name__ == "__main__":
    unittest.main()
