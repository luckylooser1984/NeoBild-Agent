#!/usr/bin/env python3
"""
Prompt test bench -- scores role outputs by objective, text-derived criteria.

Purpose: compare prompt variants measurably. Without scoring, "prompt
optimisation" is gut feeling; here every criterion is computed from the
output text itself (no LLM involved).

Criteria per role (0..1, higher is better unless noted):

  point_fidelity  The prompt asks for n points -- do n points arrive?
                  Counted via numbered / bullet markers.
  completion      No truncated points. A point counts as finished if it
                  ends with punctuation instead of breaking off mid-word.
  conformity      Judge: valid JSON with all required fields.
                  Critic/defense: no filler phrases.
  independence    Defense must NOT mirror the critique (measured via word
                  overlap -- needs both texts).
  concrete        Share of concrete references (numbers, file names,
                  technical terms) versus generic language.
  filler          Filler-phrase rate (lower is better).
  echo            Share of points that merely retell the draft (lower is
                  better).
  spread          Across several runs of the same task: how stable is the
                  judge (see the `spread` command).

The heuristics (filler list, evaluative words) are tuned for English
outputs; adapt the word lists if your prompts use another language.

Usage:
  prompt_bench.py single <runs.jsonl>        # averages per role
  prompt_bench.py spread <battery.jsonl>     # judge stability per task
  prompt_bench.py ab <runs.jsonl> [--a VER] [--b VER]
                                             # compare prompt versions

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# --------------------------------------------------------------- text analysis

# Numbered points: "1.", "1)", "- ", "* ", "•"
POINT_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.MULTILINE)
SENT_END_RE = re.compile(r"[.!?:]\s*$")
WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]{4,}")

# Phrases that indicate filler instead of substance.
FILLERS = [
    "in summary", "in conclusion", "overall", "it is important to",
    "one could say", "generally speaking", "in general", "as mentioned",
    "to summarize", "summary", "i hope",
]

# Concreteness markers: numbers, percentages, file extensions, code words.
CONCRETE_RE = re.compile(
    r"(\d+\s*%|\d+[.,]?\d*\s*(?:s|ms|gb|mb|kb|tok)|"
    r"\b\w+\.(py|md|json|jsonl|sh|yaml|sql|db)\b|"
    r"\b(SQLite|JSON|API|Cron|Timeout|Backup|Hash|Token|Schema|Migration)\b)",
    re.IGNORECASE,
)

# Evaluative words: a point containing one of these is a statement, not an echo.
EVALUATIVE_RE = re.compile(
    r"\b(missing|lacks|unclear|barely|risky|problematic|incomplete|"
    r"contradicts|fails|breaks|unusable|unnecessary|"
    r"insufficient|flawed|dangerous)\b",
    re.IGNORECASE)


def count_points(text: str) -> int:
    return len(POINT_RE.findall(text or ""))


def completion_rate(text: str) -> float:
    """Share of points that end cleanly."""
    if not text:
        return 0.0
    blocks = POINT_RE.split("\n" + text)
    blocks = [b.strip() for b in blocks if b.strip()]
    if not blocks:
        return 0.0
    finished = sum(1 for b in blocks if SENT_END_RE.search(b))
    return finished / len(blocks)


def filler_rate(text: str) -> float:
    """Filler hits relative to sentence count (lower is better)."""
    if not text:
        return 0.0
    low = text.lower()
    hits = sum(low.count(f) for f in FILLERS)
    sentences = max(1, len(re.findall(r"[.!?]", text)))
    return min(1.0, hits / sentences)


def concreteness(text: str) -> float:
    """Concrete references per 100 words (5 hits / 100 words = 1.0)."""
    words = max(1, len(WORD_RE.findall(text or "")))
    hits = len(CONCRETE_RE.findall(text or ""))
    return min(1.0, (hits / words) * 100 / 5)


def overlap(a: str, b: str, stop: set[str] | None = None) -> float:
    """Word overlap of two texts (Jaccard on word level).

    stop: words taken from the task itself -- they necessarily appear in both
    texts and only show that both address the same question, not that one
    copies the other.
    """
    wa = {w.lower() for w in WORD_RE.findall(a or "")}
    wb = {w.lower() for w in WORD_RE.findall(b or "")}
    if stop:
        wa -= stop
        wb -= stop
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# ------------------------------------------------------- judge JSON scoring

JUDGE_REQUIRED = {"verdict", "confidence", "reasoning", "improvements"}
JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def judge_conformity(text: str) -> tuple[float, str]:
    """1.0 = valid JSON with all required fields. Otherwise partial score + reason."""
    if not text:
        return 0.0, "empty"
    m = JSON_OBJ_RE.search(text)
    if not m:
        return 0.0, "no JSON object"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return 0.0, f"broken JSON: {exc}"
    missing = JUDGE_REQUIRED - set(obj)
    if missing:
        return 0.3, f"missing fields: {sorted(missing)}"
    if obj.get("verdict") not in ("pass", "revise", "reject"):
        return 0.6, f"invalid verdict: {obj.get('verdict')!r}"
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return 0.7, f"invalid confidence: {conf!r}"
    if not isinstance(obj.get("improvements"), list) or not obj["improvements"]:
        return 0.8, "improvements empty / not a list"
    # Text before/after the JSON?
    rest = (text[:m.start()] + text[m.end():]).strip()
    if rest:
        return 0.9, f"text outside JSON ({len(rest)} chars)"
    return 1.0, "clean"


# ------------------------------------------------------------------ scoring

def echo_rate(text: str, task: str) -> float:
    """Share of points that parrot the draft instead of judging it.

    Heuristic: a point counts as an echo if more than half of its content
    words appear literally in the draft AND it contains no evaluative word.
    Deliberately strict: a bare "not" is NOT enough (an observed echo case
    contained "not" and was still a pure retelling).
    """
    if not text or not task:
        return 0.0
    draft_words = {w.lower() for w in WORD_RE.findall(task)}
    echoes = 0
    total = 0
    for block in POINT_RE.split("\n" + text)[1:]:
        block = block.strip()
        if not block:
            continue
        total += 1
        words = [w.lower() for w in WORD_RE.findall(block)]
        if not words:
            continue
        inside = sum(1 for w in words if w in draft_words)
        evaluative = bool(EVALUATIVE_RE.search(block))
        if inside / len(words) > 0.5 and not evaluative:
            echoes += 1
    return echoes / total if total else 0.0


def score_role(role_rec: dict, *others: str, task: str = "") -> dict:
    role = role_rec["role"]
    out = role_rec.get("output") or ""
    s: dict = {"role": role, "len": len(out), "points": count_points(out)}
    s["completion"] = round(completion_rate(out), 3)
    s["concrete"] = round(concreteness(out), 3)
    s["filler"] = round(filler_rate(out), 3)
    s["echo"] = round(echo_rate(out, task), 3)

    if role == "judge":
        conf, reason = judge_conformity(out)
        s["conformity"] = round(conf, 3)
        s["conformity_reason"] = reason
    else:
        # Read the expected point count from the system prompt
        # ("3 to 5 points" -> 3, "EXACTLY 4" -> 4, default 3).
        prompt = role_rec.get("system_prompt", "")
        m = re.search(r"(\d+)\s*to\s*(\d+)", prompt) or re.search(r"EXACTLY\s+(\d+)", prompt)
        expected = int(m.group(1)) if m else 3
        s["point_fidelity"] = round(
            min(1.0, s["points"] / expected) if expected else 0.0, 3)
        s["conformity"] = round(1.0 - s["filler"], 3)

    if role == "defense" and others:
        # Defense must NOT mirror the critique. Words from the task are
        # subtracted -- they only show that both address the same question.
        stop = {w.lower() for w in WORD_RE.findall(task)} if task else set()
        ov = overlap(out, others[0], stop)
        s["critique_overlap"] = round(ov, 3)
        s["independence"] = round(max(0.0, 1.0 - ov * 4), 3)
    return s


def score_run(run: dict) -> dict:
    recs = run.get("roles", [])
    crit = next((r for r in recs if r["role"] == "critique"), None)
    task = run.get("task", "")
    out = {"run_id": run["run_id"][:8], "task": task[:50]}
    out["roles"] = []
    for r in recs:
        if r["role"] == "defense" and crit:
            out["roles"].append(score_role(r, crit.get("output", ""), task=task))
        else:
            out["roles"].append(score_role(r, task=task))
    out["finished"] = all(r.get("finish_reason") == "stop" for r in recs)
    return out


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def cmd_single(path: Path) -> int:
    runs = load(path)
    if not runs:
        print(f"no runs in {path}")
        return 0
    print(f"File: {path} ({len(runs)} runs)\n")
    agg: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        sc = score_run(run)
        for r in sc["roles"]:
            a = agg.setdefault(r["role"], {})
            for k, v in r.items():
                if isinstance(v, (int, float)):
                    a.setdefault(k, []).append(float(v))
    for role, values in agg.items():
        print(f"[{role}]")
        for k, vals in sorted(values.items()):
            print(f"   {k:20s} avg {mean(vals):6.3f}   n={len(vals)}")
        print()
    finished = sum(1 for r in runs if score_run(r)["finished"])
    print(f"Runs with finish_reason=stop in all roles: {finished}/{len(runs)}")
    return 0


def cmd_spread(path: Path) -> int:
    """How stable is the judge per task?"""
    rows = load(path)
    if not rows:
        print(f"no data in {path}")
        return 0
    by: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("verdict"):
            by.setdefault(r["task_name"], []).append(
                (r["verdict"], r.get("confidence")))
    for name, values in sorted(by.items()):
        verd = {v for v, _ in values}
        confs = [c for _, c in values if c is not None]
        span = (max(confs) - min(confs)) if len(confs) > 1 else 0.0
        stable = "STABLE" if len(verd) == 1 and span <= 0.1 else "varies"
        print(f"  {name:12s} {len(values)} runs  verdicts={verd}  "
              f"conf-span={span:.2f}  -> {stable}")
    return 0


def cmd_ab(path: Path, ver_a: str, ver_b: str) -> int:
    """Evaluate and compare prompt variants separately.

    Runs are grouped by the critique prompt_version (first 8 chars) stored in
    the log, because discourse.py writes all runs into the same file.
    """
    runs = load(path)
    if not runs:
        print(f"no runs in {path}")
        return 0

    def versions_of(run: dict) -> dict[str, str]:
        return {r["role"]: r.get("prompt_version", "")[:8]
                for r in run.get("roles", [])}

    groups: dict[str, list[dict]] = {}
    for run in runs:
        crit = versions_of(run).get("critique", "")
        if not crit:
            continue
        groups.setdefault(crit, []).append(run)

    if not groups:
        print("no runs with prompt_version found")
        return 0

    print(f"Critique prompt versions found: "
          f"{ {k: len(v) for k, v in groups.items()} }\n")

    def agg(runs_: list[dict]) -> dict[str, dict[str, float]]:
        a: dict[str, dict[str, list[float]]] = {}
        for run in runs_:
            for sc in score_run(run)["roles"]:
                bucket = a.setdefault(sc["role"], {})
                for k, v in sc.items():
                    if isinstance(v, (int, float)):
                        bucket.setdefault(k, []).append(float(v))
        return {role: {k: mean(v) for k, v in w.items()}
                for role, w in a.items()}

    for ver, runs_ in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if ver in (ver_a, ver_b) or (not ver_a and not ver_b):
            stats = agg(runs_)
            finished = sum(1 for r in runs_ if score_run(r)["finished"])
            print(f"### Prompt version {ver}  ({len(runs_)} runs, "
                  f"{finished} complete)")
            for role in ("critique", "defense", "judge"):
                if role not in stats:
                    continue
                s = stats[role]
                keys = ["point_fidelity", "completion", "conformity", "concrete",
                        "filler", "echo", "independence",
                        "critique_overlap", "len"]
                parts = [f"{k}={s[k]:.3f}" for k in keys if k in s]
                print(f"   {role:9s} " + "  ".join(parts))
            verdicts = [r.get("judge_verdict", {}).get("verdict")
                        for r in runs_
                        if "parse_error" not in (r.get("judge_verdict") or {})]
            print(f"   verdicts={verdicts}")
            print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("single", help="average scores per role")
    p1.add_argument("path", type=Path)
    p2 = sub.add_parser("spread", help="judge stability per task (battery.jsonl)")
    p2.add_argument("path", type=Path)
    p3 = sub.add_parser("ab", help="compare prompt versions")
    p3.add_argument("path", type=Path)
    p3.add_argument("--a", default="")
    p3.add_argument("--b", default="")
    args = ap.parse_args()
    if args.cmd == "single":
        return cmd_single(args.path)
    if args.cmd == "spread":
        return cmd_spread(args.path)
    if args.cmd == "ab":
        return cmd_ab(args.path, args.a, args.b)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
