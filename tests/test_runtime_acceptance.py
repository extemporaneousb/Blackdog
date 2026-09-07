from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_runtime.py"
SPEC = importlib.util.spec_from_file_location("acceptance_runtime", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class RuntimeAcceptanceReportTests(unittest.TestCase):
    def test_failed_publication_preserves_previous_report_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            target.write_text("previous complete report\n", encoding="utf-8")
            with patch.object(acceptance.os, "replace", side_effect=OSError("publication failed")):
                with self.assertRaises(OSError):
                    acceptance.publish_report(target, {"status": "passed"})
            self.assertEqual(target.read_text(), "previous complete report\n")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_known_percentiles_preserve_eligible_and_missing_denominators(self) -> None:
        result = acceptance.distribution([*range(1, 21), None, None])
        self.assertEqual(result["eligible_count"], 22)
        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["missing_count"], 2)
        self.assertEqual(result["p50_ms"], 10.5)
        self.assertEqual(result["p95_ms"], 19)
        self.assertFalse(result["small_sample"])

    def test_missing_observations_are_not_zero_duration(self) -> None:
        result = acceptance.distribution([None])
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["p50_ms"])
        self.assertIsNone(result["p95_ms"])
        self.assertTrue(result["small_sample"])

    def test_invalid_values_cannot_enter_comparable_duration_population(self) -> None:
        for value in (True, False, -1, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                acceptance.distribution([value])


if __name__ == "__main__":
    unittest.main()
