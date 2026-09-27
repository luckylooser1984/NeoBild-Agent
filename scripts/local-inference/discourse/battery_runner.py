#!/usr/bin/env python3
"""
Long-running test battery for a small local model (discourse workflow).

Runs a fixed battery of drafts repeatedly through the
critique -> defense -> judge tool (discourse.py), records per-run metrics and
writes them to a separate evaluation log.

Purpose: reliable data over time instead of single snapshots -- which role
breaks off, how often the judge parser fails, which task types are stable,
how much the verdict scatters with the same seed.

Output (append-only, nothing is overwritten):
  <out-dir>/battery.jsonl   one record per run
  <out-dir>/battery.log     short line for humans

Usage:
  battery_runner.py              # one round over all tasks
  battery_runner.py --rounds 3   # three rounds
  battery_runner.py --stats      # summarise battery.jsonl

Deliberately runs WITHOUT a daemon: an external scheduler (cron, systemd
timer) starts it at a fixed interval, and every run has a hard timeout, so a
hanging run cannot block the machine.

Environment:
  DISCOURSE_LOG_DIR  default output directory (default: ./discourse-logs)

Author: Lukas Weißmann
License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DISC = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = Path(os.environ.get("DISCOURSE_LOG_DIR", "discourse-logs"))

# Fixed task battery: deliberately different types and severities.
TASKS: list[tuple[str, str, str]] = [
    ("ethics", "medium",
     "Draft: We introduce a weekly status email for all users. "
     "Every week a report with usage numbers is sent automatically. "
     "Users cannot opt out, because it keeps the system simpler."),
    ("architecture", "high",
     "Architecture draft: The token index will be stored in SQLite instead of JSONL. "
     "Rationale: faster queries. Migration happens in one step with no way back. "
     "All agents write directly into the same table, with a lock."),
    ("trivial", "low",
     "Draft: We rename the file README.md to INFO.md. "
     "Reason: the name is shorter and looks more modern."),
    ("security", "high",
     "Draft: The local agent may execute commands without confirmation "
     "as long as they do not start with rm. All other commands run directly. "
     "Rationale: confirmations cost time."),
    ("data", "medium",
     "Draft: We automatically delete all logs older than 7 days via cron. "
     "Rationale: saves space. No backup exists."),
]

RUN_TIMEOUT_S = 300


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def run_one(name: str, level: str, task: str, iteration: int,
            roles_file: Path, log_dir: Path) -> dict:
    """One discourse run. Returns a metadata record."""
    t0 = time.monotonic()
    cmd = [sys.executable, "discourse.py", "run",
           "--task", task, "--iteration", str(iteration),
           "--roles-file", str(roles_file), "--log-dir", str(log_dir)]
    rec: dict = {
        "ts": now(),
        "task_name": name,
        "level": level,
        "iteration": iteration,
        "ok": False,
        "timeout": False,
        "verdict": None,
        "confidence": None,
        "parse_error": None,
        "duration_ms": 0,
    }
    try:
        proc = subprocess.run(
            cmd, cwd=str(DISC), capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
        )
        rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if proc.returncode != 0:
            rec["error"] = (proc.stderr or "").strip()[-500:]
            return rec
        # discourse.py prints the judge verdict as JSON on stdout
        out = proc.stdout.strip()
        start = out.find("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            try:
                verdict = json.loads(out[start:end + 1])
                rec["verdict"] = verdict.get("verdict")
                rec["confidence"] = verdict.get("confidence")
                rec["parse_error"] = verdict.get("parse_error")
                rec["improvements"] = len(verdict.get("improvements", []) or [])
            except json.JSONDecodeError as exc:
                rec["error"] = f"stdout is not JSON: {exc}"
                return rec
        rec["ok"] = rec["parse_error"] is None
    except subprocess.TimeoutExpired:
        rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
        rec["timeout"] = True
        rec["error"] = f"timeout after {RUN_TIMEOUT_S}s"
    return rec


def append(rec: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "battery.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    line = (f"{rec['ts']} {rec['task_name']:12s} "
            f"{'OK   ' if rec['ok'] else 'ERROR'} "
            f"verdict={rec.get('verdict')} conf={rec.get('confidence')} "
            f"{rec['duration_ms']}ms")
    with (out_dir / "battery.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def load(out_dir: Path) -> list[dict]:
    battery = out_dir / "battery.jsonl"
    if not battery.exists():
        return []
    out = []
    with battery.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def cmd_stats(out_dir: Path) -> int:
    rows = load(out_dir)
    if not rows:
        print("no runs in battery.jsonl")
        return 0
    ok = [r for r in rows if r["ok"]]
    fail = [r for r in rows if not r["ok"]]
    to = [r for r in rows if r.get("timeout")]
    durs = [r["duration_ms"] for r in ok]
    print(f"Runs total         : {len(rows)}")
    print(f"  successful       : {len(ok)}")
    print(f"  failed/parse err : {len(fail)}")
    print(f"  timeouts         : {len(to)}")
    if durs:
        print(f"  duration avg/max : {sum(durs)/len(durs):.0f} ms / {max(durs)} ms")
    verd: dict[str, int] = {}
    for r in ok:
        verd[r["verdict"]] = verd.get(r["verdict"], 0) + 1
    print(f"  verdicts         : {verd}")
    print()
    print("per task:")
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["task_name"], []).append(r)
    for name, items in sorted(by.items()):
        o = [r for r in items if r["ok"]]
        v = [r["verdict"] for r in o]
        print(f"  {name:12s} {len(items):3d} runs, {len(o)} ok, verdicts={v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--roles-file", type=Path, default=DISC / "roles.json")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="where battery.jsonl/.log and the discourse runs go")
    args = ap.parse_args()
    out_dir = args.out_dir.resolve()

    if args.stats:
        return cmd_stats(out_dir)

    base_it = int(datetime.now().strftime("%H%M"))
    total = 0
    for _ in range(args.rounds):
        for name, level, task in TASKS:
            rec = run_one(name, level, task, base_it + total,
                          args.roles_file.resolve(), out_dir)
            append(rec, out_dir)
            total += 1
    print(f"\nRound finished: {total} runs, logged in {out_dir / 'battery.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
