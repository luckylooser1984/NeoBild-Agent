import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prompt_bench as pb


class PromptBenchTests(unittest.TestCase):
    def test_judge_conformity_clean(self):
        text = '{"verdict": "revise", "confidence": 0.7, "reasoning": "x", "improvements": ["y"]}'
        self.assertEqual(pb.judge_conformity(text), (1.0, "clean"))

    def test_judge_conformity_missing_fields(self):
        score, reason = pb.judge_conformity('{"verdict": "pass"}')
        self.assertEqual(score, 0.3)
        self.assertIn("missing fields", reason)

    def test_expected_points_from_prompt(self):
        rec = {"role": "critique", "system_prompt": "Name EXACTLY 4 flaws.",
               "output": "1. First flaw.\n2. Second flaw."}
        self.assertEqual(pb.score_role(rec)["point_fidelity"], 0.5)

    def test_echo_detected_without_evaluative_word(self):
        task = "Draft: we delete all logs older than seven days via cron"
        out = "1. We delete all logs older than seven days via cron."
        self.assertEqual(pb.echo_rate(out, task), 1.0)
        out2 = "1. Deleting logs older than seven days is risky without backup."
        self.assertEqual(pb.echo_rate(out2, task), 0.0)


if __name__ == "__main__":
    unittest.main()
