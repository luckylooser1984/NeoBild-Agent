#!/usr/bin/env python3
"""app_inventory.py -- read-only app inventory (size, granted permissions) + a deterministic
list of OEM packages you might want to disable.

Purpose:
    Lists all third-party apps plus system packages matching known OEM-bloat
    prefixes that YOU supply (--oem-prefix). Suggestions are based only on those
    prefixes - there is no heuristic that labels third-party apps (e.g. privacy
    tools) as "removable". The full inventory is listed so a human decides.
    Nothing is uninstalled or disabled.

Usage:
    app_inventory.py --oem-prefix com.vendor. [--oem-prefix com.other.] [--out-dir DIR]
    Writes <out-dir>/app-inventory-<date>.md. Device: --serial or $ANDROID_SERIAL.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ADB = os.environ.get("ANDROID_ADB") or shutil.which("adb") or "adb"
SERIAL = ""


def adb_base() -> list[str]:
    return [ADB] + (["-s", SERIAL] if SERIAL else [])


def adb_shell(cmd: str, timeout: int = 120) -> str:
    res = subprocess.run(adb_base() + ["shell", cmd], capture_output=True,
                         text=True, timeout=timeout, errors="replace")
    return res.stdout


def device_present() -> bool:
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=20).stdout
    ready = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]
    return (SERIAL in ready) if SERIAL else len(ready) == 1


def shlex_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def list_packages(flag: str) -> dict[str, str]:
    out = adb_shell(f"pm list packages {flag} -f")
    pkgs = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("package:") or "=" not in line:
            continue
        apk_path, pkg = line[len("package:"):].rsplit("=", 1)
        pkgs[pkg] = apk_path
    return pkgs


def apk_size(apk_path: str) -> int:
    out = adb_shell(f"stat -c %s {shlex_quote(apk_path)}")
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return -1


def package_detail(pkg: str) -> dict:
    out = adb_shell(f"dumpsys package {pkg}")
    perms = sorted(set(re.findall(r"^\s+(android\.permission\.\S+):\s+granted=true", out, re.MULTILINE)))
    persistent = re.search(r"flags=\[[^\]]*\bPERSISTENT\b", out) is not None
    m = re.search(r"versionName=(\S+)", out)
    return {"permissions": perms, "persistent": persistent, "version": m.group(1) if m else ""}


def main() -> int:
    global SERIAL
    ap = argparse.ArgumentParser(description="Read-only Android app inventory with OEM-bloat suggestions.")
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", ""))
    ap.add_argument("--oem-prefix", action="append", default=[],
                    help="package prefix of known OEM bloat (repeatable), e.g. com.vendor.")
    ap.add_argument("--out-dir", type=Path, default=Path("app-audit"))
    args = ap.parse_args()
    SERIAL = args.serial
    prefixes = tuple(args.oem_prefix)

    if not device_present():
        print("No (unique) adb device connected - nothing to do.")
        return 0

    third_party = list_packages("-3")
    system_all = list_packages("-s")
    oem_bloat = {p: path for p, path in system_all.items() if prefixes and p.startswith(prefixes)}
    merged = {**third_party, **oem_bloat}

    rows = []
    for pkg, apk_path in sorted(merged.items()):
        rows.append({"pkg": pkg, "size": apk_size(apk_path),
                     "kind": "OEM system" if pkg in oem_bloat else "third-party",
                     **package_detail(pkg)})

    suggest = [r for r in rows if r["kind"] == "OEM system"]
    stamp = dt.date.today().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"app-inventory-{stamp}.md"

    lines = [
        f"# App inventory — {stamp}",
        "",
        "Collected read-only via adb (`pm list packages`, `dumpsys package`). "
        "No automatic uninstall, nothing changed - report only.",
        "",
        f"## Candidates to disable ({len(suggest)} OEM system packages matching the given prefixes)",
        "",
    ]
    if suggest:
        lines.append("| Package | Version | Size (bytes) | PERSISTENT | Note |")
        lines.append("|---|---|---|---|---|")
        for r in sorted(suggest, key=lambda r: -r["size"]):
            note = ("resists `pm disable-user` - needs root or a debloat tool"
                    if r["persistent"] else "can be disabled via `pm disable-user --user 0 <package>`")
            lines.append(f"| `{r['pkg']}` | {r['version']} | {r['size']} | "
                         f"{'yes' if r['persistent'] else 'no'} | {note} |")
    else:
        lines.append("No OEM packages found for prefixes: " + (", ".join(prefixes) or "(none given)") + ".")

    lines += ["", "## Full inventory (third-party + OEM system)", "",
              "| Package | Kind | Version | Size (bytes) | Permissions (granted) |",
              "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['pkg']}` | {r['kind']} | {r['version']} | {r['size']} | "
                     f"{', '.join(r['permissions']) or '-'} |")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {out_path} ({len(rows)} packages, {len(suggest)} suggestions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
