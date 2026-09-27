#!/usr/bin/env python3
"""task_list.py — build a work list from a phone note inbox.

Purpose:
    Tasks and ideas are jotted down as Markdown on the phone; phone_intake.sh
    copies them into a local inbox folder. This script turns that inbox into a
    work list (TASKS.md) plus a register (tasks.json), so every item is handled
    exactly once and it is always visible where it was delegated.

    It decides NOTHING and executes NOTHING: it reads the notes, suggests a
    destination based on simple keyword features (German + English) and keeps
    the status. The actual assignment is done by a human or an agent, which edits
    the "status" field in tasks.json.

    For MHTML page captures, the readable version produced by
    mhtml_to_markdown.py (in the notes folder) is scored instead of the raw file,
    so MIME headers don't distort the classification.

Usage:
    task_list.py              build/update the list
    task_list.py --status     print a short overview only
Environment:
    INTAKE_INBOX=/path   inbox folder (default: ./phone-inbox)
    INTAKE_NOTES=/path   folder with converted notes (default: ./notes-converted)

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

INBOX = Path(os.environ.get("INTAKE_INBOX", "phone-inbox")).expanduser()
NOTES = Path(os.environ.get("INTAKE_NOTES", "notes-converted")).expanduser()
REGISTER = INBOX / "tasks.json"
LISTING = INBOX / "TASKS.md"

FEATURES = [
    ("question/research", r"\?|\bwie\b|\bwarum\b|\bwas ist\b|\bwelche[rs]?\b|\brecherch|\bquelle"
                          r"|\bhow\b|\bwhy\b|\bwhat is\b|\bwhich\b|\bresearch\b|\bsource\b"),
    ("code/system", r"```|\bscript\b|\bpython\b|\bbash\b|\bsystemd\b|\bunit\b|\bfehler\b|\berror\b"
                    r"|\bbug\b|\bconfig\b|\bkonfig|\bpatch\b|\bterminal\b|\bgrub\b|\bkernel\b"),
    ("idea/hypothesis", r"\bidee\b|\bidea\b|\bhypothese\b|\bhypothesis\b|\bvielleicht\b|\bmaybe\b"
                        r"|\bkoennte\b|\bcould\b|\büberleg|\bueberleg|\bkonzept\b|\bconcept\b"),
    ("content/channel", r"\bmarketing\b|\bcontent\b|\bartikel\b|\barticle\b|\bpost\b|\bkanal\b"
                        r"|\bchannel\b|\bnewsletter\b|\bvideo\b|\bmusik\b|\bmusic\b"),
    ("money/offer", r"\bpreis\b|\bprice\b|\bförder|\bfoerder|\bgrant\b|\bcredit\b|\bangebot\b"
                    r"|\boffer\b|\bumsatz\b|\brevenue\b|\bkunde\b|\bcustomer\b|\brechnung\b|\binvoice\b"),
]

DESTINATION = {
    "question/research": "question list -> research channel, store the answer locally",
    "code/system": "handle locally; larger scope -> coding agent / sub-agents",
    "idea/hypothesis": "ideas folder or decision queue (options, no recommendation)",
    "content/channel": "editorial / content backlog",
    "money/offer": "research task + check primary source, then decision queue",
    "unclear": "let a human classify it (no automation)",
}

INTERNAL_FILES = {"manifest.tsv", "TASKS.md", "tasks.json"}


def read_note(path: Path, max_chars: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def title(path: Path, text: str) -> str:
    for line in text.splitlines():
        z = line.strip()
        if z.startswith("#"):
            return z.lstrip("# ").strip()[:80]
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return path.stem


def classify(text: str) -> str:
    hits = [(name, len(re.findall(pattern, text, re.IGNORECASE))) for name, pattern in FEATURES]
    hits = [(n, c) for n, c in hits if c]
    if not hits:
        return "unclear"
    hits.sort(key=lambda x: -x[1])
    return hits[0][0]


def converted_note(inbox_file: Path) -> Path | None:
    """For an MHTML capture, find the readable note produced from it."""
    try:
        head = inbox_file.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        return None
    if "MultipartBoundary" not in head and "Saved by Blink" not in head:
        return None
    stem = inbox_file.stem.lower().replace(" ", "_")[:60]
    # Tolerant matching: all meaningful words of the inbox name must appear in the
    # converted note's name (names may differ, e.g. word order).
    noise = {"copy", "of", "the", "und", "and", "eine", "ein"}
    marks = {w for w in re.split(r"[^a-z0-9]+", stem) if len(w) > 3 and w not in noise}
    if not NOTES.exists():
        return None
    for cand in sorted(NOTES.glob("*.md")):
        cstem = cand.stem.lower()
        if marks and all(m in cstem for m in marks):
            return cand
        if cand.stem.endswith(stem):
            return cand
    return None


def main() -> int:
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return 0
    if not INBOX.exists():
        print(f"Inbox missing: {INBOX}")
        return 2
    register: dict = {}
    if REGISTER.exists():
        try:
            register = json.loads(REGISTER.read_text())
        except Exception:
            register = {}

    files = sorted(p for p in INBOX.iterdir()
                   if p.is_file() and not p.name.startswith(".") and p.name not in INTERNAL_FILES)
    for p in files:
        src = converted_note(p) or p
        text = read_note(src)
        kind = classify(text)
        entry = register.get(p.name, {})
        entry.update({
            "file": p.name,
            "title": title(src, text),
            "bytes": p.stat().st_size,
            "kind": kind,
            "destination": DESTINATION.get(kind, DESTINATION["unclear"]),
            "status": entry.get("status", "open"),
            "captured": entry.get("captured", datetime.now().isoformat(timespec="seconds")),
            "readable_version": str(src) if src is not p else "",
        })
        entry.setdefault("preview", "\n".join(
            z for z in text.splitlines()[:14] if z.strip())[:900])
        register[p.name] = entry

    REGISTER.write_text(json.dumps(register, ensure_ascii=False, indent=2))

    if "--status" in sys.argv:
        for e in register.values():
            print(f"{e['status']:12} {e['kind']:18} {e['file']}")
        return 0

    open_items = [e for e in register.values() if e["status"] == "open"]
    lines = ["# Tasks from the phone inbox", "",
             f"As of: {datetime.now().isoformat(timespec='seconds')}  |  open: {len(open_items)} of {len(register)}",
             "", "This file is generated; change the status in `tasks.json`.", ""]
    for e in sorted(register.values(), key=lambda x: x["captured"]):
        lines += [f"## {e['title']}", "",
                  f"- File: `{e['file']}` ({e['bytes']} bytes)",
                  f"- Captured: {e['captured']}",
                  f"- Detected as: **{e['kind']}**",
                  f"- Suggested destination: {e['destination']}",
                  f"- Status: **{e['status']}**", "",
                  "<details><summary>Preview</summary>", "", "```",
                  e.get("preview", "")[:900], "```", "", "</details>", ""]
    LISTING.write_text("\n".join(lines))
    print(f"Work list: {LISTING}  |  open: {len(open_items)} / total: {len(register)}")
    for e in open_items:
        print(f"  open: {e['kind']:18} {e['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
