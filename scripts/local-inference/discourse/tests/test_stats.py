import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.stats import compute_stats, format_stats


class StatsTests(unittest.TestCase):
    def test_empty_runs_returns_zeroed_structure(self):
        stats = compute_stats([])
        self.assertEqual(stats["run_count"], 0)
        self.assertEqual(format_stats(stats), "Runs: 0\n(no runs logged)")

    def test_aggregates_two_runs(self):
        runs = [
            {
                "total_duration_ms": 1000.0,
                "total_tokens_per_second": 10.0,
                "judge_verdict": {"verdict": "pass", "confidence": 0.9},
                "roles": [
                    {"role": "critique", "tokens_per_second": 12.0, "duration_ms": 400.0, "error": None},
                    {"role": "defense", "tokens_per_second": 11.0, "duration_ms": 300.0, "error": None},
                    {"role": "judge", "tokens_per_second": 9.0, "duration_ms": 300.0, "error": None},
                ],
            },
            {
                "total_duration_ms": 2000.0,
                "total_tokens_per_second": 8.0,
                "judge_verdict": {"parse_error": "invalid json", "raw": "..."},
                "roles": [
                    {"role": "critique", "tokens_per_second": 14.0, "duration_ms": 500.0, "error": None},
                    {"role": "defense", "tokens_per_second": 13.0, "duration_ms": 400.0, "error": "timeout"},
                    {"role": "judge", "tokens_per_second": 7.0, "duration_ms": 600.0, "error": None},
                ],
            },
        ]
        stats = compute_stats(runs)
        self.assertEqual(stats["run_count"], 2)
        self.assertAlmostEqual(stats["avg_total_duration_ms"], 1500.0)
        self.assertEqual(stats["judge_parse_error_count"], 1)
        self.assertEqual(stats["judge_verdict_counts"], {"pass": 1})
        self.assertAlmostEqual(stats["by_role"]["critique"]["avg_tokens_per_second"], 13.0)
        self.assertEqual(stats["by_role"]["defense"]["error_count"], 1)


if __name__ == "__main__":
    unittest.main()
