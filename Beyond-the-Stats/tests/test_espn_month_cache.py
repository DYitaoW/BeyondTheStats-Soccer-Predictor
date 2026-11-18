"""ESPN month scoreboards: cached, tiered by distance, used by range crawls.

Cups and friendlies used to walk ESPN day by day (cups ~1,700 requests a
night, friendlies 732). ``fetch_scoreboard_range`` now reads one cached month
payload per month; far months are re-fetched rarely (friendlies weekly).
Network is mocked; dates are relative to today.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

import espn_api_cache as eac  # noqa: E402


def _month(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


class MonthTtlTests(unittest.TestCase):
    def test_tiers(self):
        today = date.today()
        cur = _month(today)
        far = _next_month(_next_month(_next_month(cur)))
        self.assertEqual(eac.month_ttl_seconds("eng.fa", cur, today), eac.MONTH_TTL_CURRENT_SECONDS)
        self.assertEqual(eac.month_ttl_seconds("eng.fa", far, today), eac.MONTH_TTL_FAR_SECONDS)
        self.assertEqual(eac.month_ttl_seconds("fifa.friendly", far, today), eac.MONTH_TTL_SPARSE_FAR_SECONDS)
        old = _month(today - timedelta(days=120))
        self.assertEqual(eac.month_ttl_seconds("eng.fa", old, today), eac.MONTH_TTL_PAST_SECONDS)
        # A month that ended 1 day ago still refreshes like the current month.
        first = _month(today)
        prev = _month(first - timedelta(days=1))
        self.assertEqual(eac.month_ttl_seconds("eng.fa", prev, first + timedelta(days=1)), eac.MONTH_TTL_CURRENT_SECONDS)
        nxt = _next_month(cur)
        self.assertEqual(eac.month_ttl_seconds("eng.fa", nxt, nxt - timedelta(days=5)), eac.MONTH_TTL_NEAR_SECONDS)


class RangeUsesMonthsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(eac, "CACHE_ROOT", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_one_cached_request_per_month_and_window_filter(self):
        start = date.today() + timedelta(days=2)
        end = start + timedelta(days=70)
        inside = (start + timedelta(days=3)).strftime("%Y-%m-%dT19:00Z")
        outside = (start - timedelta(days=1)).strftime("%Y-%m-%dT19:00Z")
        calls = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            self.assertIn("&limit=1000", req.full_url)
            code = req.full_url.split("dates=")[1][:6]
            events = [{"id": f"{code}-in", "date": inside}, {"id": f"{code}-out", "date": outside}]
            return Resp(json.dumps({"events": events}).encode())

        with mock.patch.object(eac, "_urlopen", side_effect=fake_urlopen):
            first = eac.fetch_scoreboard_range("eng.fa", start, end)
            n_months = len(eac._months_between(start, end))
            self.assertEqual(len(calls), n_months)
            again = eac.fetch_scoreboard_range("eng.fa", start, end)
            self.assertEqual(len(calls), n_months)  # served from the month cache
        ids = {e["id"] for e in first}
        self.assertTrue(all(i.endswith("-in") for i in ids))
        self.assertEqual(len(first), len(again))

    def test_month_failure_falls_back_to_day_walk(self):
        start = date.today() + timedelta(days=1)
        end = start + timedelta(days=1)
        with mock.patch.object(eac, "fetch_scoreboard_month", side_effect=OSError("boom")), \
                mock.patch.object(eac, "_urlopen", side_effect=OSError("no range")), \
                mock.patch.object(eac, "fetch_scoreboard_default", return_value={"events": []}), \
                mock.patch.object(eac, "fetch_scoreboard", return_value={"events": [{"id": "d1", "date": ""}]}) as day:
            events = eac.fetch_scoreboard_range("eng.fa", start, end)
        self.assertEqual(day.call_count, 2)
        self.assertEqual([e["id"] for e in events], ["d1"])


if __name__ == "__main__":
    unittest.main()
