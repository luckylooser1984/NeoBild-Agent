#!/usr/bin/env python3
"""
Majority verdict -- catches outliers of a small local model.

Observation: a local 1.7B model once judged a risky draft "pass" with
confidence 0.9 while it otherwise said "revise". A single run is therefore
not a reliable verdict -- but over several runs the majority was right every
time.

This tool runs the same draft n times through discourse.py and decides by
majority vote. It also averages the majority's confidence and reports
disagreement as a warning signal.

Usage:
  majority_vote.py --task "..." --runs 3
  majority_vote.py --task-file draft.md --runs 5 --roles-file roles.json

Exit codes:
  0 = clear majority
  3 = undecided (no verdict with an absolute majority) -> escalate

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

DISC = Path(__file__).resolve().parent


def single_run(task: str, roles_file: Path, iteration: int,
               log_dir: Path | None) -> dict | None:
    """Run one discourse pass and return the judge verdict."""
    cmd = [sys.executable, str(DISC / "discourse.py"), "run",
           "--task", task, "--iteration", str(iteration),
           "--roles-file", str(roles_file)]
    if log_dir is not None:
        cmd += ["--log-dir", str(log_dir)]
    try:
        proc = subprocess.run(cmd, cwd=str(DISC), capture_output=True,
                              text=True, timeout=400)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--task-file", type=Path)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--roles-file", type=Path, default=DISC / "roles.json")
    ap.add_argument("--log-dir", type=Path, default=None,
                    help="passed through to discourse.py (default: its own default)")
    args = ap.parse_args()

    task = args.task if args.task else args.task_file.read_text(
        encoding="utf-8").strip()

    verdicts: list[dict] = []
    for i in range(args.runs):
        v = single_run(task, args.roles_file, 3000 + i, args.log_dir)
        if v is None:
            print(f"  run {i+1}/{args.runs}: failed", file=sys.stderr)
            continue
        verdicts.append(v)
        print(f"  run {i+1}/{args.runs}: {v.get('verdict')} "
              f"(conf {v.get('confidence')})", file=sys.stderr)

    if not verdicts:
        print(json.dumps({"error": "no valid run"}))
        return 3

    counter = Counter(v.get("verdict") for v in verdicts)
    top, count = counter.most_common(1)[0]
    confs = [c for v in verdicts
             if v.get("verdict") == top
             for c in [v.get("confidence")]
             if isinstance(c, (int, float))]
    result = {
        "verdict": top,
        "majority": f"{count}/{len(verdicts)}",
        "clear": count > len(verdicts) / 2,
        "distribution": dict(counter),
        "confidence_mean": round(sum(confs) / len(confs), 3) if confs else None,
        "samples": len(verdicts),
    }
    # Disagreement is a signal in itself: with a split vote the verdict is
    # uncertain no matter which side wins.
    if not result["clear"]:
        result["warning"] = ("no absolute majority -- verdict uncertain, "
                             "escalation to a stronger model recommended")
    elif len(counter) > 1:
        result["warning"] = (f"{len(verdicts)-count} outlier(s) in "
                             f"{len(verdicts)} runs -- treat the output as a "
                             f"suggestion, not as a verdict")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["clear"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
