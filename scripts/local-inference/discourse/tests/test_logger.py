import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.logger import append_run


class AppendRunTests(unittest.TestCase):
    def test_appends_valid_jsonl_line_and_human_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "local-discourse"
            record = {
                "run_id": "abc123",
                "ended_at": "2026-09-19T20:00:00+00:00",
                "iteration": 1,
                "task": "Test task",
                "total_tokens": 100,
                "total_tokens_per_second": 13.5,
                "judge_verdict": {"verdict": "revise", "confidence": 0.6},
            }
            append_run(log_dir, record)
            append_run(log_dir, record)

            jsonl_lines = (log_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(jsonl_lines), 2)
            parsed = json.loads(jsonl_lines[0])
            self.assertEqual(parsed["run_id"], "abc123")
            self.assertEqual(parsed["judge_verdict"]["verdict"], "revise")

            human_lines = (log_dir / "runs.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(human_lines), 2)
            self.assertIn("verdict=revise", human_lines[0])
            self.assertIn("confidence=0.6", human_lines[0])


if __name__ == "__main__":
    unittest.main()
