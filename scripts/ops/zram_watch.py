#!/usr/bin/env python3
"""zram_watch.py — silent-by-default watchdog for a zram swap device.

The problem it makes visible: if your swap is /dev/zram0 (compressed swap
INSIDE RAM) rather than a disk device, its content never migrates back to a
disk on its own. Pages that were swapped out stay there even after RAM frees
up — the kernel only brings them back when their owner touches them again.
"Swap full" in such a setup means: part of your RAM is permanently occupied
by itself.

Metrics from /sys/block/zram0/mm_stat (order as documented by the kernel):
    orig_data_size   uncompressed bytes stored in swap
    compr_data_size  bytes those occupy compressed in RAM
    mem_used_total   total memory used including compression overhead

Threshold: compr_data_size relative to the zram disksize. That is the part
which physically eats RAM and therefore creates memory-pressure risk — not
the uncompressed payload share (which is always higher due to compression
and overstates scarcity).

Behaviour: prints to stdout ONLY on a state change (crosses a threshold,
recovers, or climbs to the next rung). Empty output = all quiet, so a cron
job that forwards stdout does not spam about a persistent condition.

Side effects: none. Reads /sys only and writes nothing but its own state
file. No root needed.

Usage:
    python3 zram_watch.py
Environment:
    ZRAM_WATCH_DIR    zram sysfs dir (default /sys/block/zram0)
    ZRAM_WATCH_STATE  state file (default ~/.local/state/zram_watch.json)

Author: Lukas Weißmann
License: MIT
"""
import json
import os
import sys
from datetime import datetime

ZRAM_DIR = os.environ.get("ZRAM_WATCH_DIR", "/sys/block/zram0")
MM_STAT = os.path.join(ZRAM_DIR, "mm_stat")
DISKSIZE = os.path.join(ZRAM_DIR, "disksize")
STATE_FILE = os.environ.get(
    "ZRAM_WATCH_STATE",
    os.path.join(os.path.expanduser("~"), ".local", "state", "zram_watch.json"),
)
STATE_DIR = os.path.dirname(STATE_FILE)

# Rungs: 0.80 = warning threshold, 0.92 = final warning before the ceiling.
LADDER = (0.80, 0.92)


def logger(*parts) -> None:
    sys.stderr.write("zram_watch: " + " ".join(str(p) for p in parts) + "\n")


def read_int(path: str):
    try:
        with open(path) as fh:
            return int(fh.read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def knobs() -> str:
    """Harmless configuration values to include — if the compression algorithm
    changes, it shows up in the report."""
    out = []
    try:
        with open(os.path.join(ZRAM_DIR, "comp_algorithm")) as fh:
            algo = fh.read().strip()
        active = ""
        for token in algo.split():
            if token.startswith("[") and token.endswith("]"):
                active = token.strip("[]")
        out.append(f"algorithm={active or algo.split()[0] if algo.split() else '?'}")
    except OSError:
        pass
    return " ".join(out)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def main() -> int:
    if not os.path.isdir(ZRAM_DIR):
        # No zram here — nothing to watch. Reporting it would be wrong
        # (it is a normal state), so stay silent.
        return 0

    disksize = read_int(DISKSIZE)
    try:
        with open(MM_STAT) as fh:
            fields = fh.read().split()
        orig, compr, mem_used = (int(fields[0]), int(fields[1]), int(fields[2]))
    except (OSError, ValueError, IndexError) as exc:
        logger(f"mm_stat unreadable ({type(exc).__name__}) — format changed?")
        return 1

    if not disksize:
        logger("disksize missing or 0")
        return 1

    ratio_ram = compr / disksize          # what zram physically costs in RAM
    ratio_data = orig / disksize          # how full the device is with payload
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def human(b: int) -> str:
        return f"{b / 1024 ** 3:.2f} GB"

    # Determine rung: the highest exceeded threshold wins.
    level = 0
    for i, limit in enumerate(LADDER, start=1):
        if ratio_ram >= limit:
            level = i

    state = load_state()
    before = int(state.get("level", 0))

    # Always update state so that "no change" is not treated as new next time.
    state.update({
        "level": level,
        "ratio_ram": round(ratio_ram, 4),
        "ratio_data": round(ratio_data, 4),
        "orig_bytes": orig,
        "compr_bytes": compr,
        "mem_used_bytes": mem_used,
        "disksize_bytes": disksize,
        "last_checked": now,
        "last_message": state.get("last_message"),
    })

    message = None
    if level > before:
        limit = LADDER[level - 1]
        message = [
            f"zram swap above threshold: {ratio_ram * 100:.1f}% of the "
            f"{human(disksize)} device ({human(compr)} compressed in RAM, "
            f"rung from {limit * 100:.0f}%)",
            f"Payload: {human(orig)} uncompressed = {ratio_data * 100:.1f}% of the device, "
            f"{human(mem_used)} total incl. compression overhead",
            "Swap content does not return by itself in this setup — it occupies "
            "RAM until the kernel pages it back in on access.",
            "Inspect with: zramctl; swapon --show; ps -eo pid,rss,comm --sort=-rss | head",
            f"As of: {now} ({knobs()})",
        ]
    elif level < before:
        message = [
            f"zram swap recovered: {ratio_ram * 100:.1f}% "
            f"({human(compr)} compressed in RAM, level {before} -> {level})",
            f"As of: {now}",
        ]

    if message:
        state["last_message"] = now
        print("\n".join(message))

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
