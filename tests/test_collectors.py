import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import openalex_counts as oa
import scopus_counts as sc


class OpenAlexTests(unittest.TestCase):
    def test_parse_years(self):
        self.assertEqual(oa.parse_years("2020-2024"), [2020, 2021, 2022, 2023, 2024])
        self.assertEqual(oa.parse_years("2022, 2020, 2021"), [2020, 2021, 2022])
        with self.assertRaises(ValueError):
            oa.parse_years("2020,2022")

    def test_missing_groups_are_zero(self):
        payload = {
            "meta": {"count": 3},
            "group_by": [
                {"key": "2020", "count": 1},
                {"key": "2024", "count": 2},
            ],
        }
        self.assertEqual(
            oa.parse_group_counts(payload, [2020, 2021, 2022, 2023, 2024]),
            {2020: 1, 2021: 0, 2022: 0, 2023: 0, 2024: 2},
        )

    def test_group_total_is_validated(self):
        with self.assertRaises(ValueError):
            oa.parse_group_counts(
                {"meta": {"count": 2}, "group_by": [{"key": "2020", "count": 1}]},
                [2020],
            )


class ScopusTests(unittest.TestCase):
    def test_key_rotation_never_prints_key(self):
        manager = sc.KeyManager(["first-secret", "second-secret"])
        output = StringIO()
        with redirect_stdout(output):
            manager.mark_exhausted(0)
        index, value = manager.current()
        self.assertEqual(index, 1)
        self.assertEqual(value, "second-secret")
        self.assertNotIn("first-secret", output.getvalue())
        self.assertNotIn("second-secret", output.getvalue())

    def test_zero_precheck_skips_annual_calls(self):
        client = object.__new__(sc.ScopusClient)
        calls = []

        def total(query, date_range=None):
            calls.append((query, date_range))
            return 0

        client.total = total
        counts = sc.ScopusClient.counts(client, "AF-ID(1)", "2", [2020, 2021])
        self.assertEqual(counts, {2020: 0, 2021: 0})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "2020-2021")

    def test_positive_precheck_runs_annual_calls(self):
        client = object.__new__(sc.ScopusClient)
        results = iter([3, 1, 2])
        client.total = lambda query, date_range=None: next(results)
        counts = sc.ScopusClient.counts(client, "AF-ID(1)", "2", [2020, 2021])
        self.assertEqual(counts, {2020: 1, 2021: 2})


class MatrixTests(unittest.TestCase):
    def test_checkpoint_roundtrip_and_resume(self):
        orgs = pd.DataFrame({"openalex_id": ["https://openalex.org/I1"]})
        journals = pd.DataFrame({"openalex_source_id": ["https://openalex.org/S1"]})
        matrix = oa.empty_matrix(orgs, journals, [2020, 2021])
        matrix.loc[(2020, "https://openalex.org/I1"), "https://openalex.org/S1"] = 4
        matrix.loc[(2021, "https://openalex.org/I1"), "https://openalex.org/S1"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counts.tsv"
            oa.save_matrix(matrix, path)
            restored = oa.load_resume(path, oa.empty_matrix(orgs, journals, [2020, 2021]))
        self.assertEqual(restored.iloc[:, 0].tolist(), [4, 0])
        self.assertEqual(list(oa.pending_pairs(restored, orgs, journals, [2020, 2021])), [])


if __name__ == "__main__":
    unittest.main()
