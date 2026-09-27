#!/usr/bin/env python3
"""phone_notes_pull.py — pull new Markdown notes from an Android notes app into a local vault.

Purpose:
    Notes written on the phone (e.g. with a Markdown editor such as Markor) land
    in a folder on the device. This script finds new .md files there via adb,
    pulls them, files a copy into the topic folders via note_router.classify()
    (pure keyword rules, no LLM) and keeps an unmodified raw copy as well.

    Safety properties:
      * Notes whose file name hints at credentials (pw, password, token, ...) are
        never routed into a topic folder - they only get the raw copy.
      * With --delete-after-verify the original on the phone is removed ONLY after
        both local copies were written and verified via SHA-256. Without that flag
        (the default) the phone is never modified.
      * Only successfully processed files are remembered in the state file, so a
        transient failure (cable pulled mid-transfer) is retried on the next run.

    Routed copies are named YYYY-MM-DD-HHMM-<slug>.md (source mtime, note title as
    slug) - the same naming as note_router.route(), shared via target_path().

Usage:
    phone_notes_pull.py [--dry-run] [--serial SERIAL] [--remote-dir DIR]
                        [--raw-dir DIR] [--state-dir DIR] [--delete-after-verify]
    Notes root for routed copies: NOTE_ROUTER_ROOT (see note_router.py).

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import note_router  # noqa: E402  shared router, not duplicated

ADB = os.environ.get("ANDROID_ADB") or shutil.which("adb") or "adb"
SERIAL = os.environ.get("ANDROID_SERIAL", "")

# File names with these hints are NEVER routed into a topic folder (credential
# risk) - raw copy only.
CREDENTIAL_NAME_HINTS = ("pw", "passwort", "password", "secret", "geheim",
                         "token", "keepass", "credential", "zugangsdat")


def looks_like_credentials(name: str) -> bool:
    stem = Path(name).stem.lower()
    return any(hint in stem for hint in CREDENTIAL_NAME_HINTS)


def adb_cmd(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    base = [ADB] + (["-s", SERIAL] if SERIAL else [])
    return subprocess.run(base + args, capture_output=True,
                          text=True, timeout=timeout, errors="replace")


def device_present() -> bool:
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    ready = [line.split()[0] for line in out.splitlines()[1:]
             if line.strip().endswith("device")]
    return (SERIAL in ready) if SERIAL else len(ready) == 1


def shlex_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def list_remote_md(remote_dir: str) -> list[tuple[str, int, int]]:
    cmd = (f"find {shlex_quote(remote_dir)} -maxdepth 1 -name '*.md' "
           f"-type f -exec stat -c '%s|%Y|%n' {{}} +")
    res = adb_cmd(["shell", cmd])
    files = []
    for line in res.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            size, mtime = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        files.append((parts[2].strip(), size, mtime))
    return files


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def unique_target(dirpath: Path, name: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    target = dirpath / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = dirpath / f"{stem}-{n}{suffix}"
        n += 1
    return target


def process_file(remote: str, size: int, mtime: int, args) -> dict:
    name = remote.rsplit("/", 1)[-1]
    raw_title = Path(name).stem  # note title = file name without .md
    stage = args.state_dir / "staging"
    stage.mkdir(parents=True, exist_ok=True)
    local_tmp = stage / name

    res = adb_cmd(["pull", "-a", remote, str(local_tmp)], timeout=120)
    if res.returncode != 0 or not local_tmp.is_file():
        return {"remote": remote, "status": "pull_failed", "detail": res.stderr[-300:]}

    if size >= 0 and local_tmp.stat().st_size != size:
        local_tmp.unlink(missing_ok=True)
        return {"remote": remote, "status": "size_mismatch"}

    content = local_tmp.read_text(encoding="utf-8", errors="replace")
    credential_hint = looks_like_credentials(name)
    slug, kw = (None, "") if credential_hint else note_router.classify(name + "\n" + content[:2000])
    digest = sha256_file(local_tmp)

    if args.dry_run:
        local_tmp.unlink(missing_ok=True)
        if credential_hint:
            return {"remote": remote, "status": "dry_run", "target_category": "raw-only (credential hint)"}
        return {"remote": remote, "status": "dry_run", "target_category": slug, "keyword": kw,
                "target_name": note_router.target_name(raw_title, mtime)}

    raw_target = unique_target(args.raw_dir, name)
    raw_target.write_bytes(local_tmp.read_bytes())
    routed_target = None
    if not credential_hint:
        routed_target = note_router.target_path(note_router.ROOT / slug, raw_title, mtime)
        routed_target.write_bytes(local_tmp.read_bytes())
    local_tmp.unlink(missing_ok=True)

    ok = raw_target.is_file() and sha256_file(raw_target) == digest
    if not credential_hint:
        ok = ok and routed_target.is_file() and sha256_file(routed_target) == digest
    if not ok:
        return {"remote": remote, "status": "verify_failed",
                "raw": str(raw_target), "routed": str(routed_target) if routed_target else None}

    if args.delete_after_verify:
        adb_cmd(["shell", f"rm -f -- {shlex_quote(remote)}"])
        check = adb_cmd(["shell", f"test -e {shlex_quote(remote)} && echo EXISTS || echo GONE"])
        if "GONE" not in check.stdout:
            return {"remote": remote, "status": "delete_failed",
                    "raw": str(raw_target), "routed": str(routed_target)}

    return {"remote": remote, "status": "ok",
            "category": "raw-only (credential hint)" if credential_hint else slug,
            "keyword": kw, "raw": str(raw_target),
            "routed": str(routed_target) if routed_target else None, "sha256": digest}


def main() -> int:
    global SERIAL
    ap = argparse.ArgumentParser(description="Pull new Markdown notes from an Android device via adb.")
    ap.add_argument("--dry-run", action="store_true", help="only show where notes would be filed")
    ap.add_argument("--serial", default=SERIAL, help="adb serial (default: $ANDROID_SERIAL or the only device)")
    ap.add_argument("--remote-dir", default="/storage/emulated/0/Documents/markor",
                    help="notes folder on the device")
    ap.add_argument("--raw-dir", type=Path, default=Path("phone-raw"),
                    help="local folder for unmodified raw copies")
    ap.add_argument("--state-dir", type=Path, default=Path(".phone_notes_state"),
                    help="local folder for state + staging")
    ap.add_argument("--delete-after-verify", action="store_true",
                    help="remove the note on the phone after both copies are verified (off by default)")
    args = ap.parse_args()
    SERIAL = args.serial

    if not device_present():
        print("No (unique) adb device connected - nothing to do.")
        return 0

    state_path = args.state_dir / "seen.json"
    state = load_state(state_path)
    results = []
    for remote, size, mtime in list_remote_md(args.remote_dir):
        seen = state.get(remote)
        if seen and seen.get("size") == size and seen.get("mtime") == mtime and seen.get("status") == "ok":
            continue
        result = process_file(remote, size, mtime, args)
        results.append(result)
        if not args.dry_run and result["status"] == "ok":
            state[remote] = {"size": size, "mtime": mtime, "status": "ok",
                             "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        # Other states are deliberately NOT stored -> the next run retries
        # (self-healing on transient errors).

    if not args.dry_run and results:
        save_state(state_path, state)

    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    if not results:
        print("No new .md files on the device.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
