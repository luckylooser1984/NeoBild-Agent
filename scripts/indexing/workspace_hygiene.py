#!/usr/bin/env python3
"""workspace_hygiene.py — read-only hygiene scan for working directories.

Checks each root for:
  1. Log growth without rotation (>200 MB, >7 days old, no .1/.gz siblings)
  2. Backup accumulation (>10 files of the same kind, >500 MB in total)
  3. Exact duplicates (size prefilter + MD5; skips .git/.venv/node_modules/__pycache__)
  4. Large untracked binaries in git repos (>100 MB, `git status` "??")
  5. __pycache__ sprawl

Writes ONLY a Markdown report to --out (plus <out>.json). Deletes nothing,
needs no root.

Usage:
    python3 workspace_hygiene.py --root ~/projects --root ~/notes --out report.md

Author: Lukas Weißmann
License: MIT
"""
import argparse, hashlib, os, subprocess, sys, time, json
from collections import defaultdict

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".cache",
             ".mozilla", ".var", "site-packages", ".smart-env", ".stfolder"}
LOG_SUFFIX = (".log",)
LOG_HINTS = ("embed.log", "desktop.log", "crash.log", "error.log")
ROT_MARK = (".1", ".2", ".gz", ".old", ".1.gz")
MB = 1024 * 1024
NOW = time.time()


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def fsize(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def check_logs(root, min_mb=200, min_days=7):
    out = []
    for p in walk(root):
        low = os.path.basename(p).lower()
        if not (low.endswith(LOG_SUFFIX) or low in LOG_HINTS):
            continue
        s = fsize(p)
        if s < min_mb * MB:
            continue
        try:
            age = (NOW - os.path.getmtime(p)) / 86400
        except OSError:
            continue
        if age < min_days:
            continue
        if any(os.path.exists(p + m) for m in ROT_MARK):
            continue
        out.append({"path": p, "mb": round(s / MB, 1), "days": round(age, 1)})
    return sorted(out, key=lambda x: -x["mb"])


def check_backups(root, min_files=10, min_total_mb=500):
    groups = defaultdict(list)
    for p in walk(root):
        b = os.path.basename(p)
        for marker in (".bak", "_backup", ".backup", "-bak"):
            if marker in b.lower():
                key = os.path.join(os.path.dirname(p), marker)
                groups[key].append(p)
                break
        else:
            # Date pattern at the end: name-YYYY-MM-DD.ext
            stem, ext = os.path.splitext(b)
            parts = stem.rsplit("-", 3)
            if len(parts) == 4 and parts[1].isdigit() and len(parts[1]) == 4:
                key = os.path.join(os.path.dirname(p), "date-" + ext)
                groups[key].append(p)
    out = []
    for key, files in groups.items():
        if len(files) < min_files:
            continue
        total = sum(fsize(f) for f in files)
        if total < min_total_mb * MB:
            continue
        out.append({"group": key, "count": len(files),
                    "total_mb": round(total / MB, 1),
                    "newest": max(files, key=lambda f: fsize(f) and os.path.getmtime(f))})
    return sorted(out, key=lambda x: -x["total_mb"])


def md5(p, chunk=1 << 20):
    h = hashlib.md5()
    try:
        with open(p, "rb") as fh:
            while True:
                d = fh.read(chunk)
                if not d:
                    break
                h.update(d)
    except OSError:
        return None
    return h.hexdigest()


def check_duplicates(root, min_mb=1):
    by_size = defaultdict(list)
    for p in walk(root):
        s = fsize(p)
        if s < min_mb * MB:
            continue
        by_size[s].append(p)
    dupes = []
    for s, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash = defaultdict(list)
        for f in files:
            d = md5(f)
            if d:
                by_hash[d].append(f)
        for d, fs in by_hash.items():
            if len(fs) > 1:
                dupes.append({"mb": round(s / MB, 1), "count": len(fs),
                              "waste_mb": round(s * (len(fs) - 1) / MB, 1),
                              "files": fs})
    return sorted(dupes, key=lambda x: -x["waste_mb"])


def check_untracked_binaries(root, min_mb=100):
    try:
        r = subprocess.run(["git", "-C", root, "status", "--porcelain",
                            "--untracked-files=all"],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        p = os.path.join(root, rel)
        s = fsize(p)
        if s >= min_mb * MB:
            out.append({"path": p, "mb": round(s / MB, 1)})
    return sorted(out, key=lambda x: -x["mb"])


def check_pycache(root):
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if os.path.basename(dirpath) == "__pycache__":
            count += 1
            for fn in filenames:
                total += fsize(os.path.join(dirpath, fn))
            dirnames[:] = []
    return {"count": count, "mb": round(total / MB, 1)}


def main():
    ap = argparse.ArgumentParser(description="Read-only workspace hygiene scan.")
    ap.add_argument("--root", action="append", required=True, help="directory to scan (repeatable)")
    ap.add_argument("--out", required=True, help="Markdown report path (JSON goes to <out>.json)")
    ap.add_argument("--min-dupe-mb", type=int, default=1, help="ignore duplicates smaller than this")
    a = ap.parse_args()

    lines = ["# Workspace hygiene report", "",
             f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Roots: {', '.join(a.root)}", ""]
    data = {}

    for root in a.root:
        if not os.path.isdir(root):
            lines.append(f"## {root}\n\nMISSING (not a directory)\n")
            continue
        lines.append(f"## {root}\n")

        logs = check_logs(root)
        lines.append(f"### 1. Logs without rotation ({len(logs)})")
        for x in logs[:25]:
            lines.append(f"- {x['mb']} MB, {x['days']} days old: {x['path']}")
        if not logs:
            lines.append("- none")
        lines.append("")

        baks = check_backups(root)
        lines.append(f"### 2. Backup accumulation ({len(baks)})")
        for x in baks[:25]:
            lines.append(f"- {x['count']} files, {x['total_mb']} MB: {x['group']}")
        if not baks:
            lines.append("- none")
        lines.append("")

        dupes = check_duplicates(root, a.min_dupe_mb)
        waste = round(sum(d["waste_mb"] for d in dupes), 1)
        lines.append(f"### 3. Exact duplicates ({len(dupes)} groups, {waste} MB wasted)")
        for d in dupes[:30]:
            lines.append(f"- {d['count']}x {d['mb']} MB (waste {d['waste_mb']} MB): {d['files'][0]}")
        if not dupes:
            lines.append("- none")
        lines.append("")

        bins = check_untracked_binaries(root)
        lines.append(f"### 4. Large untracked binaries ({len(bins)})")
        for x in bins[:25]:
            lines.append(f"- {x['mb']} MB: {x['path']}")
        if not bins:
            lines.append("- none")
        lines.append("")

        pc = check_pycache(root)
        lines.append(f"### 5. __pycache__ sprawl\n- {pc['count']} directories, {pc['mb']} MB\n")

        data[root] = {"logs": logs, "backups": baks, "dupes": dupes,
                      "untracked": bins, "pycache": pc}

    with open(a.out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(a.out + ".json", "w") as fh:
        json.dump(data, fh, indent=2)
    print("OK", a.out)
    for root, d in data.items():
        print(f"{root}: logs={len(d['logs'])} baks={len(d['backups'])} "
              f"dupes={len(d['dupes'])} untracked={len(d['untracked'])} "
              f"waste={round(sum(x['waste_mb'] for x in d['dupes']),1)}MB "
              f"pycache={d['pycache']['count']}/{d['pycache']['mb']}MB")


if __name__ == "__main__":
    main()
