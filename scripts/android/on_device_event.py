#!/usr/bin/env python3
"""on_device_event.py -- orchestrates actions when an Android device connects/disconnects (USB or Wi-Fi).

Purpose:
    Single entry point for device events. Intended wiring: a root-side udev rule
    (USB) or a NetworkManager dispatcher script (Wi-Fi) starts a templated
    systemd user unit, e.g. android-device-trigger@<event>.service, which calls
    this script. It then
      1. logs the event (connection_log.py),
      2. on every connect pulls new phone notes (../second-brain/phone_notes_pull.py),
      3. on Wi-Fi connect optionally runs an extra sync command
         ($DEVICE_EVENT_WIFI_CMD), so Wi-Fi contact reuses an existing sync
         instead of building a second pipeline.

Usage:
    on_device_event.py <usb-add|usb-remove|wifi-up|wifi-down>

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable or "python3"
NOTES_PULL = HERE.parent / "second-brain" / "phone_notes_pull.py"

EVENT_MAP = {
    "usb-add": ("usb", "connect"),
    "usb-remove": ("usb", "disconnect"),
    "wifi-up": ("wifi", "connect"),
    "wifi-down": ("wifi", "disconnect"),
}


def run(args: list[str]) -> None:
    subprocess.run(args, timeout=600)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in EVENT_MAP:
        print("Usage: on_device_event.py <usb-add|usb-remove|wifi-up|wifi-down>", file=sys.stderr)
        return 2
    transport, event = EVENT_MAP[sys.argv[1]]

    run([PY, str(HERE / "connection_log.py"), transport, event])

    if event != "connect":
        return 0

    # Pull new phone notes on every connect (USB or Wi-Fi).
    if NOTES_PULL.is_file():
        run([PY, str(NOTES_PULL)])

    extra = os.environ.get("DEVICE_EVENT_WIFI_CMD")
    if transport == "wifi" and extra:
        run(shlex.split(extra))

    return 0


if __name__ == "__main__":
    sys.exit(main())
