"""Offline tests for dream.py -- the LLM call is replaced by a fake."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dream  # noqa: E402
import memdb  # noqa: E402


class DreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        memdb.init_db(self.db)
        con = sqlite3.connect(self.db)
        # 1: will be forgotten (2.0 * 0.95 < 2.0)
        # 2-4: high importance, connected -> one cluster of 3
        # 5: medium, stays
        rows = [(1, "trivial chatter", 2.0), (2, "prefers local models", 9.0),
                (3, "runs a CPU-only laptop", 9.0), (4, "cares about privacy", 9.0),
                (5, "asked about cron once", 5.0)]
        con.executemany(
            "INSERT INTO embeddings(id, role, text, importance) VALUES (?, 'user', ?, ?)", rows)
        con.executemany(
            "INSERT INTO edges(src_id, dst_id, distance) VALUES (?, ?, 0.1)", [(2, 3), (3, 4)])
        con.commit()
        con.close()
        self._orig_chat = dream.chat
        dream.chat = lambda *a, **k: "summary of the cluster"

    def tearDown(self):
        dream.chat = self._orig_chat
        self.tmp.cleanup()

    def test_dry_run_changes_nothing(self):
        stats = dream.run_decay(self.db, dry_run=True)
        self.assertEqual(stats["deleted"], 1)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 5)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM reflections").fetchone()[0], 0)

    def test_real_run_forgets_decays_and_summarises(self):
        stats = dream.run_decay(self.db, dry_run=False)
        self.assertEqual(stats, {"decayed": 4, "boosted": 0, "deleted": 1, "summarized": 3})
        con = sqlite3.connect(self.db)
        ids = [r[0] for r in con.execute("SELECT id FROM embeddings ORDER BY id")]
        self.assertEqual(ids, [2, 3, 4, 5])
        imp = con.execute("SELECT importance FROM embeddings WHERE id=2").fetchone()[0]
        self.assertAlmostEqual(imp, 8.55)
        kind, = con.execute("SELECT kind FROM reflections").fetchone()
        self.assertEqual(kind, "dream_summary")
        self.assertTrue(list(Path(self.tmp.name).glob("memory.db.bak.*")))


if __name__ == "__main__":
    unittest.main()
