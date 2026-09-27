#!/usr/bin/env python3
"""connection_log.py -- append-only connection log (USB/Wi-Fi connect/disconnect) for an Android device.

Purpose:
    Records one JSONL line per connect/disconnect event. Session durations are
    derived later from consecutive connect/disconnect pairs per transport, not
    at write time. Meant to be called from udev / NetworkManager hooks via
    on_device_event.py.

Usage:
    connection_log.py <usb|wifi> <connect|disconnect>
Environment:
    CONNECTION_LOG=/path/connection-log.jsonl   (default: ./connection-logs/connection-log.jsonl)
    CONNECTION_LOG_SSID=1   also record the phone's current Wi-Fi SSID on connect
                            (off by default - an SSID can reveal locations)
    ANDROID_SERIAL          device to query for the SSID

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ADB = os.environ.get("ANDROID_ADB", "adb")
SERIAL = os.environ.get("ANDROID_SERIAL", "")
LOG_PATH = Path(os.environ.get("CONNECTION_LOG", "connection-logs/connection-log.jsonl")).expanduser()


def current_ssid() -> str | None:
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + ["shell", "dumpsys wifi"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'SSID:\s*"([^"]*)"', res.stdout)
    return m.group(1) if m else None


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(sys.argv) != 3 or sys.argv[1] not in ("usb", "wifi") or sys.argv[2] not in ("connect", "disconnect"):
        print("Usage: connection_log.py <usb|wifi> <connect|disconnect>", file=sys.stderr)
        return 2
    transport, event = sys.argv[1], sys.argv[2]
    entry = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "transport": transport,
        "event": event,
    }
    if event == "connect" and os.environ.get("CONNECTION_LOG_SSID") == "1":
        entry["ssid"] = current_ssid()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
