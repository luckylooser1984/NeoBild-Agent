#!/usr/bin/env python3
"""User profile extraction and persistent storage (local only).

Asks the local model to pull structured facts about the user out of a
dialog snippet (interests, projects, preferred language, devices, technical
level, writing style, ...) and upserts them into the `user_profile` table.
profile_as_text() renders the profile for a system prompt.

Privacy note: this is designed for a single-user agent whose memory DB
never leaves the machine. Do not point it at a shared or synced database.

Usage (module):
  from user_profile import update_from_dialog, profile_as_text
Usage (CLI):
  user_profile.py --show [--db memory.db]
  user_profile.py --dialog-file chat.txt [--db memory.db]

Environment: MEMORY_DB, LLM_BASE_URL, LLM_MODEL (see memdb.py).

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from memdb import chat, connect

_EXTRACT_PROMPT = """Analyse the following dialog excerpt and extract structured information about the user.
Return a JSON object (NO Markdown, no ```json). Only fields that are actually recognisable.
Possible keys: name, occupation, projects, interests, preferred_language, location, devices, technical_level, writing_style.
If nothing is recognisable, return {{}}.

Dialog:
{text}
"""


def extract_profile(dialog_text: str) -> dict:
    """Ask the local model to extract user facts from a dialog snippet."""
    prompt = _EXTRACT_PROMPT.format(text=dialog_text[:2000])
    try:
        raw = chat(prompt, max_tokens=256, temperature=0.2, timeout=45)
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception:
        return {}


def update_profile(facts: dict, db: Path | None = None) -> None:
    """Upsert profile key-value pairs into the user_profile table."""
    if not facts:
        return
    con = connect(db)
    now = datetime.now().isoformat()
    for key, value in facts.items():
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        con.execute(
            "INSERT INTO user_profile(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, val_str, now),
        )
    con.commit()
    con.close()


def get_profile(db: Path | None = None) -> dict:
    """Load the full user profile as a dict."""
    con = connect(db)
    rows = con.execute("SELECT key, value FROM user_profile").fetchall()
    con.close()
    profile = {}
    for key, value in rows:
        try:
            profile[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            profile[key] = value
    return profile


def profile_as_text(db: Path | None = None) -> str:
    """Return the profile formatted for use in a system prompt."""
    p = get_profile(db)
    if not p:
        return ""
    lines = ["[User profile]"]
    for k, v in p.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def update_from_dialog(dialog_text: str, db: Path | None = None) -> dict:
    """Extract and persist profile info from a dialog. Returns extracted facts."""
    facts = extract_profile(dialog_text)
    update_profile(facts, db)
    return facts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print the stored profile")
    g.add_argument("--dialog-file", type=Path, help="extract facts from this dialog text")
    a = ap.parse_args()
    if a.show:
        print(profile_as_text(a.db) or "(empty profile)")
    else:
        facts = update_from_dialog(a.dialog_file.read_text(encoding="utf-8"), a.db)
        print(json.dumps(facts, ensure_ascii=False, indent=2))
