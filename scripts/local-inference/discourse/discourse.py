#!/usr/bin/env python3
"""Sequential critique -> defense -> judge discourse against a local llama-server.

A small model reviews a draft/task in three isolated roles:
  critique  sees only the draft and lists concrete flaws,
  defense   sees only the draft (NOT the critique) and assesses it independently,
  judge     sees the draft plus both independent outputs and returns a JSON
            verdict: {"verdict": pass|revise|reject, "confidence", "reasoning",
            "improvements"}.

Every run is appended to <log-dir>/runs.jsonl (full prompts, outputs, token
counts, timings) plus a one-line summary in <log-dir>/runs.log.

Usage:
  discourse.py run --task "Draft: ..." [--roles-file roles.json] [--log-dir DIR]
  discourse.py run --task-file draft.md
  discourse.py stats [--log-dir DIR]

Environment:
  DISCOURSE_LOG_DIR  default log directory (default: ./discourse-logs)
  LLM_BASE_URL       overrides "base_url" from the roles file
                     (e.g. http://127.0.0.1:8080/v1)

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.llm_client import chat_completion
from lib.logger import append_run
from lib.stats import compute_stats, format_stats, load_runs

DEFAULT_LOG_DIR = Path(os.environ.get("DISCOURSE_LOG_DIR", "discourse-logs"))
DEFAULT_ROLES_FILE = Path(__file__).resolve().parent / "roles.json"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _parse_judge_verdict(text: str) -> dict:
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return {"parse_error": "no JSON object found", "raw": text}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"parse_error": str(exc), "raw": text}


def _role_record(role: str, cfg: dict, messages: list[dict], result, model: str, seed: int) -> dict:
    return {
        "role": role,
        "system_prompt": cfg["system_prompt"],
        "prompt_version": _prompt_hash(cfg["system_prompt"]),
        "context": messages,
        "output": result.content,
        "raw_output": result.raw_content,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "tokens_per_second": result.tokens_per_second,
        "duration_ms": result.duration_ms,
        "finish_reason": result.finish_reason,
        "model": model,
        "temperature": cfg["temperature"],
        "seed": seed,
        "max_tokens": cfg["max_tokens"],
        "error": result.error,
    }


def run_discourse(task: str, roles_config: dict, *, iteration: int) -> dict:
    model = roles_config["model"]
    base_url = os.environ.get("LLM_BASE_URL") or roles_config["base_url"]
    seed = roles_config["seed"]
    roles = roles_config["roles"]

    started_at = datetime.now(timezone.utc).isoformat()
    role_records = []

    # Critique: sees only the task/draft.
    critique_cfg = roles["critique"]
    critique_messages = [
        {"role": "system", "content": critique_cfg["system_prompt"]},
        {"role": "user", "content": task},
    ]
    critique_result = chat_completion(
        base_url, critique_messages, model=model,
        temperature=critique_cfg["temperature"], seed=seed, max_tokens=critique_cfg["max_tokens"],
    )
    role_records.append(_role_record("critique", critique_cfg, critique_messages, critique_result, model, seed))

    # Defense: sees only the task/draft, INDEPENDENTLY -- NOT the critique
    # output (context isolation, so it cannot just mirror the critic).
    defense_cfg = roles["defense"]
    defense_messages = [
        {"role": "system", "content": defense_cfg["system_prompt"]},
        {"role": "user", "content": task},
    ]
    defense_result = chat_completion(
        base_url, defense_messages, model=model,
        temperature=defense_cfg["temperature"], seed=seed, max_tokens=defense_cfg["max_tokens"],
    )
    role_records.append(_role_record("defense", defense_cfg, defense_messages, defense_result, model, seed))

    # Judge: sees the task plus both independent outputs.
    judge_cfg = roles["judge"]
    judge_user_content = (
        f"Draft/task:\n{task}\n\n"
        f"Critique (independent):\n{critique_result.content}\n\n"
        f"Assessment (independent):\n{defense_result.content}"
    )
    judge_messages = [
        {"role": "system", "content": judge_cfg["system_prompt"]},
        {"role": "user", "content": judge_user_content},
    ]
    judge_result = chat_completion(
        base_url, judge_messages, model=model,
        temperature=judge_cfg["temperature"], seed=seed, max_tokens=judge_cfg["max_tokens"],
    )
    role_records.append(_role_record("judge", judge_cfg, judge_messages, judge_result, model, seed))

    ended_at = datetime.now(timezone.utc).isoformat()
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in role_records)
    total_duration_ms = sum(r["duration_ms"] for r in role_records)
    total_completion_tokens = sum(r["completion_tokens"] for r in role_records)
    total_tokens_per_second = (
        total_completion_tokens / (total_duration_ms / 1000) if total_duration_ms > 0 else 0.0
    )

    return {
        "run_id": uuid.uuid4().hex,
        "started_at": started_at,
        "ended_at": ended_at,
        "total_duration_ms": total_duration_ms,
        "task": task,
        "iteration": iteration,
        "roles": role_records,
        "judge_verdict": _parse_judge_verdict(judge_result.content),
        "total_tokens": total_tokens,
        "total_tokens_per_second": total_tokens_per_second,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one critique->defense->judge pass")
    task_group = run_p.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", help="Task/draft text inline")
    task_group.add_argument("--task-file", type=Path, help="File containing the task/draft text")
    run_p.add_argument("--iteration", type=int, default=1)
    run_p.add_argument("--roles-file", type=Path, default=DEFAULT_ROLES_FILE)
    run_p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)

    stats_p = sub.add_parser("stats", help="Summarise metrics from runs.jsonl")
    stats_p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)

    args = parser.parse_args()

    if args.command == "run":
        task = args.task if args.task else args.task_file.read_text(encoding="utf-8").strip()
        roles_config = json.loads(args.roles_file.read_text(encoding="utf-8"))
        record = run_discourse(task, roles_config, iteration=args.iteration)
        append_run(args.log_dir, record)
        print(json.dumps(record["judge_verdict"], ensure_ascii=False, indent=2))
        print(f"\nLogged to: {args.log_dir / 'runs.jsonl'}")
        return 0

    if args.command == "stats":
        runs = load_runs(args.log_dir / "runs.jsonl")
        print(format_stats(compute_stats(runs)))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
