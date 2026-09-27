#!/usr/bin/env python3
"""sort_inbox.py — sort a synced download/inbox folder into notes and media folders.

Purpose:
    A folder that is filled from the phone (e.g. via Syncthing) is emptied by
    simple rules: text/Markdown is classified with note_router.classify() and
    filed as <notes-root>/<category>/YYYY-MM-DD-HHMM-<slug>.md (source mtime as
    timestamp, same naming as note_router.route()). Screenshots, other media,
    documents and everything else go into sub-folders of a device folder.
    Pure rule logic by file extension / name, no LLM.

Usage:
    sort_inbox.py --inbox DIR --device-dir DIR [--dry-run]
    Notes root: NOTE_ROUTER_ROOT (see note_router.py).

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import note_router  # noqa: E402  shared router, not duplicated

TEXT_EXT = {".md", ".txt", ".markdown"}
SCREENSHOT_HINTS = ("screenshot", "screen-", "scrshot")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".3gp"}
DOC_EXT = {".pdf", ".doc", ".docx", ".odt", ".xlsx", ".csv"}


def unique_target(dirpath: Path, name: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    target = dirpath / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = dirpath / f"{stem}-{n}{suffix}"
        n += 1
    return target


def classify_target(path: Path, device_dir: Path) -> Path:
    ext = path.suffix.lower()
    lower_name = path.name.lower()
    if ext in TEXT_EXT:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            content = ""
        slug, _kw = note_router.classify(path.name + "\n" + content)
        return note_router.target_path(note_router.ROOT / slug, path.stem, path.stat().st_mtime)
    if ext in IMAGE_EXT and any(h in lower_name for h in SCREENSHOT_HINTS):
        return unique_target(device_dir / "screenshots", path.name)
    if ext in IMAGE_EXT or ext in VIDEO_EXT:
        return unique_target(device_dir / "media", path.name)
    if ext in DOC_EXT:
        return unique_target(device_dir / "documents", path.name)
    return unique_target(device_dir / "other", path.name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sort a synced inbox folder by simple rules.")
    ap.add_argument("--inbox", type=Path, required=True, help="folder filled by the sync tool")
    ap.add_argument("--device-dir", type=Path, required=True,
                    help="target for media/documents/other (sub-folders are created)")
    ap.add_argument("--dry-run", action="store_true", help="print targets, move nothing")
    args = ap.parse_args()

    if not args.inbox.is_dir():
        print(f"{args.inbox} does not exist.")
        return 0
    moved = 0
    for entry in sorted(args.inbox.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if args.dry_run:
            ext = entry.suffix.lower()
            what = note_router.classify(entry.name)[0] if ext in TEXT_EXT else ext or "other"
            print(f"{entry.name} -> ({what})")
            continue
        target = classify_target(entry, args.device_dir)
        shutil.move(str(entry), str(target))
        print(f"{entry.name} -> {target}")
        moved += 1
    if not moved and not args.dry_run:
        print("Nothing new in the inbox.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
