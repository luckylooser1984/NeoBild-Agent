"""Aggregate metrics from runs.jsonl for `discourse.py stats`.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def load_runs(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    runs = []
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs


def compute_stats(runs: list[dict]) -> dict:
    """Aggregate per role and per run. Empty input -> zeroed structure; never
    raises, so a fresh install without any runs still prints cleanly."""
    stats: dict[str, Any] = {
        "run_count": len(runs),
        "by_role": {},
        "judge_parse_error_count": 0,
        "judge_verdict_counts": {},
        "avg_total_duration_ms": 0.0,
        "avg_total_tokens_per_second": 0.0,
    }
    if not runs:
        return stats

    total_durations, total_tps = [], []
    role_tps: dict[str, list[float]] = {}
    role_durations: dict[str, list[float]] = {}
    role_errors: dict[str, int] = {}

    for run in runs:
        total_durations.append(run.get("total_duration_ms", 0.0))
        total_tps.append(run.get("total_tokens_per_second", 0.0))

        verdict = run.get("judge_verdict") or {}
        if "parse_error" in verdict:
            stats["judge_parse_error_count"] += 1
        else:
            v = verdict.get("verdict", "<missing>")
            stats["judge_verdict_counts"][v] = stats["judge_verdict_counts"].get(v, 0) + 1

        for role_record in run.get("roles", []):
            role = role_record.get("role", "<unknown>")
            role_tps.setdefault(role, []).append(role_record.get("tokens_per_second", 0.0))
            role_durations.setdefault(role, []).append(role_record.get("duration_ms", 0.0))
            if role_record.get("error"):
                role_errors[role] = role_errors.get(role, 0) + 1

    stats["avg_total_duration_ms"] = statistics.mean(total_durations)
    stats["avg_total_tokens_per_second"] = statistics.mean(total_tps)
    for role in role_tps:
        stats["by_role"][role] = {
            "avg_tokens_per_second": statistics.mean(role_tps[role]),
            "avg_duration_ms": statistics.mean(role_durations[role]),
            "error_count": role_errors.get(role, 0),
            "sample_count": len(role_tps[role]),
        }
    return stats


def format_stats(stats: dict) -> str:
    lines = [f"Runs: {stats['run_count']}"]
    if stats["run_count"] == 0:
        lines.append("(no runs logged)")
        return "\n".join(lines)
    lines.append(f"Avg total duration: {stats['avg_total_duration_ms']:.0f} ms")
    lines.append(f"Avg tokens/s overall: {stats['avg_total_tokens_per_second']:.2f}")
    lines.append(f"Judge parse errors: {stats['judge_parse_error_count']}")
    lines.append(f"Judge verdicts: {stats['judge_verdict_counts']}")
    for role, values in stats["by_role"].items():
        lines.append(
            f"  {role}: avg {values['avg_tokens_per_second']:.2f} tok/s, "
            f"avg {values['avg_duration_ms']:.0f} ms, "
            f"{values['error_count']}/{values['sample_count']} errors"
        )
    return "\n".join(lines)
