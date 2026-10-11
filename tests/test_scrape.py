"""Regression tests for bulk exports and truncated date ranges."""
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import scrape


def item(published="2026-10-11", body="Company disclosure"):
    return {"code": "TB5Y0428", "name": "5Y BGTB 12/04/2028", "summary": "Announcement",
            "body": body, "filedAt": published, "date": "11 Oct 2026"}


class BulkTests(unittest.TestCase):
    def args(self, start="2026-10-11", end="2026-10-11"):
        return scrape.parse_args(["--from", start, "--to", end])

    def test_publication_date_is_not_bond_maturity(self):
        rows = scrape.api_records({"rows": [item()], "truncated": False}, "2026-10-11", "2026-10-11")
        self.assertEqual(rows[0]["date"], "2026-10-11")
        self.assertEqual(set(rows[0]), set(scrape.FIELDS))

    def test_invalid_feed_triggers_fallback_instead_of_silent_data_loss(self):
        for payload in ({}, {"rows": [], "truncated": "false"},
                        {"rows": [{"summary": "Incomplete"}], "truncated": False},
                        {"rows": [item("2026-10-12")], "truncated": False}):
            with self.assertRaises(ValueError):
                scrape.api_records(payload, "2026-10-11", "2026-10-11")

    def test_duplicate_content_is_skipped_but_different_descriptions_remain(self):
        rows = []
        payload = {"rows": [item(), item(), item(body="Different disclosure")], "truncated": False}
        failures = scrape.collect_api(None, payload, self.args(), rows)
        self.assertEqual(failures, 0)
        self.assertEqual(len(rows), 2)

    def test_truncated_ranges_are_split_into_complete_date_batches(self):
        calls = []
        disposed = []
        def get(url, timeout):
            query = parse_qs(urlsplit(url).query)
            start, end = query["from"][0], query["to"][0]
            calls.append((start, end))
            data = {"rows": [item(start)], "truncated": start != end}
            return SimpleNamespace(ok=True, status=200, json=lambda: data,
                                   dispose=lambda: disposed.append(url))
        context = SimpleNamespace(request=SimpleNamespace(get=get))
        rows = []
        failures = scrape.collect_api(context, {"rows": [], "truncated": True},
                                      self.args("2026-10-09", "2026-10-11"), rows)
        self.assertEqual(failures, 0)
        self.assertEqual({row["date"] for row in rows}, {"2026-10-09", "2026-10-10", "2026-10-11"})
        self.assertEqual(len(calls), len(disposed))

    def test_single_day_truncation_is_reported_as_partial(self):
        rows = []
        failures = scrape.collect_api(None, {"rows": [item()], "truncated": True}, self.args(), rows)
        self.assertEqual(failures, 1)
        self.assertEqual(len(rows), 1)

    def test_empty_feed_finishes_without_waiting_for_a_list(self):
        rows = []
        self.assertEqual(scrape.collect_api(None, {"rows": [], "truncated": False}, self.args(), rows), 0)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
