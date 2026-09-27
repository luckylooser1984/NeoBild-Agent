#!/usr/bin/env python3
"""hardware_collector.py — hardware health collector: pure data collection, no LLM, no network calls.

Collects user-journal errors since the last run, lm-sensors / thermal-zone
readings, Intel RAPL energy counters, battery status (upower) and network
interfaces. Appends one JSONL line per run to <out>/sensors/YYYYMMDD.jsonl and
a one-line summary to <out>/logs/collector.log. Only when something looks
abnormal (journal errors, high temperature, low battery while discharging)
it optionally takes a screenshot into <out>/screenshots/.

Meant for a cron job or a systemd oneshot timer. Complements telemetry.py:
telemetry.py samples load/rates every minute, this collector records the raw
hardware picture plus context (journal errors) at a lower frequency.

Usage:
    python3 hardware_collector.py                      # one run
    python3 hardware_collector.py --out-dir DIR        # or HARDWARE_COLLECTOR_DIR=DIR
    python3 hardware_collector.py --force-screenshot   # screenshot even without anomaly
    python3 hardware_collector.py --no-screenshot      # never take screenshots
    python3 hardware_collector.py --screenshot-cmd "grim {path}"

Optional tools (each is skipped gracefully if missing): journalctl, sensors
(lm-sensors), upower, ip, and a screenshot tool (default: KDE spectacle).
Note: the JSONL contains interface addresses from `ip -brief addr`; keep the
output directory private.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUT = Path(os.environ.get(
    "HARDWARE_COLLECTOR_DIR",
    Path.home() / ".local" / "state" / "hardware-collector"))
DEFAULT_SCREENSHOT_CMD = "spectacle -b -n -f -o {path}"

# Thresholds for "abnormal" -> screenshot
TEMP_WARN_C = 85.0
BATTERY_WARN_PCT = 15.0

# Known background noise (e.g. interactive sudo prompts) - not a hardware
# finding; otherwise every run would take a screenshot.
BENIGN_JOURNAL_RE = ("pam_unix(sudo:auth): conversation failed",
                     "pam_unix(sudo:auth): auth could not identify password")


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"__ERR__{type(e).__name__}:{e}"


def journal_errors(state_file):
    since = "-1h"
    if state_file.exists():
        try:
            since = state_file.read_text().strip()
        except Exception:
            pass
    out = sh(["journalctl", "--user", "-p", "3", "--since", since,
              "--no-pager", "-o", "short-iso"])
    if out.startswith("__ERR__"):
        return []
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("--")]
    relevant = [l for l in lines if not any(b in l for b in BENIGN_JOURNAL_RE)]
    return relevant[-50:]


def sensors_readings():
    out = sh(["sensors", "-j"])
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out}


def thermal_zones():
    zones = {}
    base = Path("/sys/class/thermal")
    if not base.exists():
        return zones
    for tz in sorted(base.glob("thermal_zone*")):
        try:
            typ = (tz / "type").read_text().strip()
            temp = int((tz / "temp").read_text().strip()) / 1000.0
            zones[tz.name] = {"type": typ, "temp_c": temp}
        except Exception:
            continue
    return zones


def rapl_energy():
    out = {}
    base = Path("/sys/class/powercap")
    if not base.exists():
        return out
    for zone in sorted(base.glob("intel-rapl:*")):
        if ":" in zone.name[len("intel-rapl:"):]:
            continue  # top-level zones only, no sub-zones
        try:
            name = (zone / "name").read_text().strip()
            uj = int((zone / "energy_uj").read_text().strip())
            out[zone.name] = {"name": name, "energy_uj": uj}
        except Exception:
            continue
    return out


def battery():
    dev = sh(["upower", "-e"])
    bat_dev = next((l for l in dev.splitlines() if "battery" in l.lower()), None)
    if not bat_dev:
        return {}
    info = sh(["upower", "-i", bat_dev])
    d = {}
    for line in info.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        d[k.strip()] = v.strip()
    return d


def net_interfaces():
    out = sh(["ip", "-brief", "addr"])
    return out.splitlines()


def max_temp(zones):
    vals = [z["temp_c"] for z in zones.values() if isinstance(z.get("temp_c"), (int, float))]
    return max(vals) if vals else None


def take_screenshot(shot_dir, reason, cmd_template):
    shot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = shot_dir / f"{ts}-{reason}.png"
    cmd = [part.replace("{path}", str(path)) for part in shlex.split(cmd_template)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        err = r.stderr.strip()
    except Exception as e:
        err = f"{type(e).__name__}:{e}"
    return str(path) if path.exists() else f"__FAILED__{err}"


def main():
    ap = argparse.ArgumentParser(description="Hardware health collector (JSONL, no LLM, no network).")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT,
                    help=f"output directory (default: $HARDWARE_COLLECTOR_DIR or {DEFAULT_OUT})")
    ap.add_argument("--force-screenshot", action="store_true",
                    help="take a screenshot even if nothing looks abnormal")
    ap.add_argument("--no-screenshot", action="store_true", help="never take screenshots")
    ap.add_argument("--screenshot-cmd", default=DEFAULT_SCREENSHOT_CMD,
                    help="screenshot command, {path} is replaced by the target file "
                         f"(default: '{DEFAULT_SCREENSHOT_CMD}')")
    args = ap.parse_args()

    out = args.out_dir.expanduser()
    sensor_dir, log_dir, shot_dir = out / "sensors", out / "logs", out / "screenshots"
    state_file = out / ".last_run"
    now = datetime.now(timezone.utc).isoformat()

    zones = thermal_zones()
    errors_since_last = journal_errors(state_file)
    sample = {
        "t": now,
        "journal_errors_since_last_run": errors_since_last,
        "sensors": sensors_readings(),
        "thermal_zones": zones,
        "rapl": rapl_energy(),
        "battery": battery(),
        "net": net_interfaces(),
    }

    mtemp = max_temp(zones)
    bat_pct = None
    try:
        bat_pct = float(sample["battery"].get("percentage", "").rstrip("%"))
    except Exception:
        pass

    anomaly = bool(errors_since_last)
    if mtemp is not None and mtemp >= TEMP_WARN_C:
        anomaly = True
    if bat_pct is not None and bat_pct <= BATTERY_WARN_PCT and \
       sample["battery"].get("state") != "charging":
        anomaly = True

    sample["anomaly"] = anomaly
    if not args.no_screenshot and (anomaly or args.force_screenshot):
        sample["screenshot"] = take_screenshot(
            shot_dir, "anomaly" if anomaly else "manual", args.screenshot_cmd)

    sensor_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    with (sensor_dir / f"{day}.jsonl").open("a") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "collector.log").open("a") as f:
        flag = "ANOMALY" if anomaly else "ok"
        f.write(f"{now} {flag} max_temp={mtemp} bat={bat_pct}\n")

    state_file.write_text(now)

    if anomaly:
        print(f"[hardware-collector] anomaly detected (max_temp={mtemp}, "
              f"bat={bat_pct}) -> {sample.get('screenshot', 'no screenshot')}")


if __name__ == "__main__":
    main()
