"""JSONL + plain-text logging for discourse runs.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import json
from pathlib import Path


def append_run(log_dir: Path, run_record: dict) -> None:
    """Append one run as a JSONL line plus a one-line human-readable summary.

    Creates log_dir if needed. Never overwrites -- append only, so an aborted
    run can lose at most its own last line, never the history.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / "runs.jsonl"
    human_path = log_dir / "runs.log"

    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run_record, ensure_ascii=False) + "\n")

    verdict = run_record.get("judge_verdict") or {}
    tps = run_record.get("total_tokens_per_second", 0.0)
    summary = (
        f"[{run_record.get('ended_at')}] run={run_record.get('run_id')} "
        f"iter={run_record.get('iteration')} task={run_record.get('task', '')[:60]!r} "
        f"total_tokens={run_record.get('total_tokens')} "
        f"total_tokens_per_second={tps:.2f} "
        f"verdict={verdict.get('verdict', '<unparsed>')} "
        f"confidence={verdict.get('confidence', '?')}\n"
    )
    with human_path.open("a", encoding="utf-8") as f:
        f.write(summary)
