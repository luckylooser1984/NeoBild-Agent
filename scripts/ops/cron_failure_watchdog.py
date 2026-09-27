#!/usr/bin/env python3
"""cron_failure_watchdog.py — make failing scheduled agent jobs visible.

Context: in an agent setup, scheduled jobs often deliver their results only
locally, and LLM-driven jobs die silently when the model provider is
unreachable. This script surfaces failures without any LLM, so it also works
offline.

Input: a JSON jobs file — either a list of jobs or {"jobs": [...]} — where
each job may carry: name/job_id, enabled, state ("paused"), last_status,
next_run_at (ISO 8601). This matches the cron store of the open-source
Hermes Agent, but any scheduler that writes such a file works.

Behaviour: prints ONLY when the state CHANGES (empty stdout = nothing new),
so it can run as a quiet cron job whose stdout is forwarded as a message.

Detects:
  - enabled jobs whose last_status is not ok
  - overdue jobs (next_run_at more than --tolerance-h hours in the past)
  - number of paused jobs (only when that number changes)

Usage:
    python3 cron_failure_watchdog.py --jobs path/to/jobs.json [--state path/to/state.json]

Read-only on the jobs file, no root, no secrets.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import datetime
import json
import os
import sys

OK_STATUS = {"ok", "success", "completed", ""}
DEFAULT_STATE = os.path.join(os.path.expanduser("~"), ".local", "state", "cron_watchdog.json")


def load_jobs(path):
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    return d if isinstance(d, list) else d.get("jobs", [])


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Report failing/overdue scheduled jobs on change only.")
    ap.add_argument("--jobs", default=os.environ.get("CRON_WATCHDOG_JOBS"),
                    help="jobs JSON file (or $CRON_WATCHDOG_JOBS)")
    ap.add_argument("--state", default=os.environ.get("CRON_WATCHDOG_STATE", DEFAULT_STATE),
                    help=f"state file (default: {DEFAULT_STATE})")
    ap.add_argument("--tolerance-h", type=float, default=3, help="overdue tolerance in hours")
    args = ap.parse_args()
    if not args.jobs:
        ap.error("--jobs or $CRON_WATCHDOG_JOBS is required")

    try:
        jobs = load_jobs(args.jobs)
    except Exception as exc:  # a missing/broken jobs file is itself a finding
        print(f"Cron watchdog: jobs file not readable ({exc.__class__.__name__}: {exc})")
        return 0

    now = datetime.datetime.now(datetime.timezone.utc)
    failed, overdue, paused = [], [], 0

    for j in jobs:
        name = j.get("name") or j.get("job_id") or "?"
        if not j.get("enabled", True) or j.get("state") == "paused":
            paused += 1
            continue
        status = (j.get("last_status") or "").strip().lower()
        if status not in OK_STATUS:
            failed.append(f"{name}: {status[:90]}")
        nxt = parse_ts(j.get("next_run_at"))
        if nxt is not None and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=datetime.timezone.utc)
        if nxt is not None and (now - nxt) > datetime.timedelta(hours=args.tolerance_h):
            overdue.append(f"{name} (due {nxt:%Y-%m-%d %H:%M})")

    new = {
        "failed": sorted(failed),
        "overdue": sorted(overdue),
        "paused": paused,
    }

    try:
        with open(args.state, "r", encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:
        old = None

    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(new, fh, ensure_ascii=False, indent=1)
    except Exception as exc:
        print(f"Cron watchdog: state not writable ({exc})")

    if old == new:
        return 0
    if not failed and not overdue and (old or {}).get("paused") == paused:
        return 0

    newly_failed = [f for f in failed if f not in (old or {}).get("failed", [])]
    lines = ["Cron failure watchdog:"]
    if failed:
        lines.append(f"  jobs with error status: {len(failed)}")
        for f in failed:
            mark = " (NEW)" if f in newly_failed else ""
            lines.append(f"    - {f}{mark}")
    if overdue:
        lines.append(f"  overdue: {len(overdue)}")
        for u in overdue:
            lines.append(f"    - {u}")
    if paused and paused != (old or {}).get("paused"):
        lines.append(f"  paused jobs: {paused}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
