"""Offline regressions for the public release's portability and secret handling."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect as c
import journal_institution_coverage as coverage


class PublicRelease(unittest.TestCase):
    def test_openalex_does_not_require_scopus_credentials(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "ROOT", Path(tmp)):
            with patch.dict(os.environ, {"OPENALEX_API_KEY": "example-openalex"}, clear=True):
                self.assertEqual(c.credentials("openalex"), ("example-openalex", []))
                with self.assertRaises(c.Halt):
                    c.credentials("scopus")

    def test_scopus_does_not_require_openalex_credentials(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "ROOT", Path(tmp)):
            with patch.dict(os.environ, {"SCOPUS_API_KEYS": "example-scopus"}, clear=True):
                self.assertEqual(c.credentials("scopus"), (None, ["example-scopus"]))

    def test_prepare_is_self_contained_and_rejects_changed_inputs(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "HERE", Path(tmp)):
            orgs, journals = c.prepare()
            self.assertEqual((len(orgs), len(journals)), (220, 639))
            manifest = json.loads((c.HERE / "input_manifest.json").read_text())
            self.assertEqual(set(manifest["input_sha256"]), {"orgs.csv", "journals.csv"})
            for name, expected in manifest["input_sha256"].items():
                self.assertEqual(hashlib.sha256((c.HERE / name).read_bytes()).hexdigest(), expected)
            c.prepare()
            manifest["input_sha256"]["journals.csv"] = "changed"
            c.save(c.HERE / "input_manifest.json", manifest)
            with self.assertRaises(c.Halt):
                c.prepare()

    def test_error_response_log_redacts_credentials(self):
        with tempfile.TemporaryDirectory(dir=c.HERE) as tmp, patch.object(c, "HERE", Path(tmp)):
            client = c.Client("openalex", ["example-private-key"])
            client.log({"status": 403, "payload": {"error": "example-private-key"}})
            logged = client.path.read_text()
            self.assertNotIn("example-private-key", logged)
            self.assertIn("[REDACTED]", logged)

    def test_unlinked_query_is_global_and_uses_primary_source(self):
        filters = coverage.year_filters(2024, ["S1"]) + [coverage.unlinked_filter()]
        query = coverage.query(filters, coverage.SOURCE_FIELD)
        self.assertEqual(query["corpus"], "core")
        self.assertEqual(filters[-1], {"column_id": "institutions_distinct_count", "value": 0})
        self.assertNotIn("lineage", json.dumps(query))
        self.assertIn("primary_location.source.id", json.dumps(query))

    def test_unlinked_grouping_rejects_incomplete_or_duplicate_buckets(self):
        from unittest.mock import Mock
        client = Mock()
        client.request.return_value = {"meta": {"count": 2}, "group_by": [{"key": "S1", "count": 1}]}
        with self.assertRaises(ValueError):
            coverage.grouped(client, [], coverage.SOURCE_FIELD, {"S1"})
        client.request.return_value = {"meta": {"count": 2}, "group_by": [
            {"key": "S1", "count": 1}, {"key": "S1", "count": 1}]}
        with self.assertRaises(ValueError):
            coverage.grouped(client, [], coverage.SOURCE_FIELD, {"S1"})


if __name__ == "__main__":
    unittest.main()
