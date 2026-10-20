"""Offline checks for count semantics, complete paging and quota handling."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect as c


class Counts(unittest.TestCase):
    def test_scopus_pages_and_years(self):
        records = [{"eid": f"e{i}", "source": "123", "date": f"{2020+i%6}-01-01"} for i in range(27)]
        client = Mock()
        client.request.side_effect = [{"total": 27, "records": records[:25]}, {"total": 27, "records": records[25:]}]
        r = c.sc_partition(client, {"scopus_lineage_query": "AF-ID(1) OR AF-ID(2)"}, ["123"])
        self.assertEqual(sum(x["count"] for x in r["counts"]), 27)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args.args[0]["start"], 25)
        self.assertIn("(AF-ID(1) OR AF-ID(2))", client.request.call_args.args[0]["query"])
        self.assertIn("SOURCE-ID(123)", client.request.call_args.args[0]["query"])

    def test_scopus_bad_metadata_and_duplicates(self):
        valid = {"eid": "a", "source": "123", "date": "2020-01-01"}
        for records in [[valid, valid], [dict(valid, date=None)], [dict(valid, source="999")], [dict(valid, date="2019-01-01")]]:
            with self.subTest(records=records), self.assertRaises(c.Halt):
                c.tally(records, ["123"], c.YEARS)

    def test_zero_is_complete(self):
        client = Mock()
        client.request.return_value = {"total": 0, "records": []}
        r = c.sc_partition(client, {"scopus_lineage_query": "AF-ID(1)"}, ["123"])
        self.assertEqual(r["counts"], [])
        self.assertEqual(client.request.call_count, 1)

    def test_scopus_changed_total_rejected(self):
        client = Mock()
        client.request.side_effect = [{"total": 26, "records": []}, {"total": 27, "records": []}]
        with self.assertRaises(c.Halt):
            c.sc_partition(client, {"scopus_lineage_query": "AF-ID(1)"}, ["123"])

    def test_direct_fallback_checks_source_and_batch_totals(self):
        client = Mock()
        # Source 123 has two works in 2020; source 456 has none.
        client.request.side_effect = [{"total": n} for n in [2,2,0,0,0,0,0,0,2]]
        result = c.sc_direct_fallback(client,{"scopus_lineage_query":"AF-ID(1)"},["123","456"],"duplicate EID",1)
        self.assertEqual(result["total"],2)
        self.assertEqual(result["counts"],[{"source":"123","year":2020,"count":2}])
        self.assertEqual(result["count_method"],"direct_fallback")

    def test_openalex_complete_sum_avoids_empty_page(self):
        client = Mock()
        client.request.side_effect = [
            {"meta": {"count": 3, "next_cursor": "next"}, "group_by": [{"key": "https://openalex.org/S1", "count": 2}]},
            {"meta": {"count": 3, "next_cursor": "still_non_null"}, "group_by": [{"key": "https://openalex.org/S2", "count": 1}]}]
        r = c.oa_partition(client, {"openalex_id": "https://openalex.org/I1"}, ["S1", "S2", "S3"], 2024)
        self.assertEqual(r["total"], 3)
        self.assertEqual(client.request.call_count, 2)

    def test_openalex_incomplete_not_zero_filled(self):
        client = Mock()
        client.request.return_value = {"meta": {"count": 3, "next_cursor": None}, "group_by": []}
        with self.assertRaises(c.Halt):
            c.oa_partition(client, {"openalex_id": "I1"}, ["S1"], 2024)

    def test_first_429_stops_without_retry(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "HERE", Path(tmp)):
            client = c.Client("scopus", ["test-secret-1", "test-secret-2"])
            response = Mock(status_code=429, headers={"X-RateLimit-Remaining": "0"})
            response.elapsed.total_seconds.return_value = 0.1
            response.json.return_value = {"error": "quota"}
            session = Mock()
            session.get.return_value = response
            client.local.session = session
            with self.assertRaises(c.Halt):client.request({"query": "q"})
            with self.assertRaises(c.Halt):client.request({"query": "q2"})
            self.assertEqual(session.get.call_count, 1)
            self.assertTrue(client.stop_path.exists())

    def test_reserve_switch_and_cached_success(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "HERE", Path(tmp)):
            client = c.Client("scopus", ["test-secret-1", "test-secret-2"])
            client.quotas = {1: {"x-ratelimit-remaining": "250"}}
            response = Mock(status_code=200, headers={"X-RateLimit-Remaining": "19999"})
            response.elapsed.total_seconds.return_value = 0.1
            response.json.return_value = {"search-results": {"opensearch:totalResults": "0", "entry": []}}
            session = Mock()
            session.get.return_value = response
            client.local.session = session
            self.assertEqual(client.request({"query": "q"})["total"], 0)
            self.assertEqual(session.get.call_args.kwargs["headers"]["X-ELS-APIKey"], "test-secret-2")
            self.assertEqual(client.request({"query": "q"})["total"], 0)
            self.assertEqual(session.get.call_count, 1)
            resumed = c.Client("scopus", ["test-secret-1", "test-secret-2"])
            self.assertEqual(resumed.request({"query": "q"})["total"], 0)


if __name__ == "__main__":
    unittest.main()
