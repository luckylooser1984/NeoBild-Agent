#!/usr/bin/env python3
"""system_inventory.py — non-invasive hardware/software inventory (read-only).

Audit baseline for: fingerprint sensor, P2P services/protocols, open ports,
firewall, running services, secrets exposure (file NAMES only, NEVER contents).

Principles: no sudo, no installs, no writes except --output.
Every check is isolated — missing tools yield null instead of a crash.
Output: JSON on stdout or into --output.

Usage:
    python3 system_inventory.py --pretty
    python3 system_inventory.py -o inventory.json --secret-root ~/projects --secret-root ~/.config

Tested on Arch Linux (fingerprint package check uses pacman when available).

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys

FPC_VENDORS = {  # known fingerprint sensor USB vendor IDs
    "138a": "Validity Sensors (ThinkPad default)",
    "27c6": "Goodix",
    "06cb": "Synaptics",
    "1c7a": "LighTuning (older ThinkPads)",
    "0483": "STMicroelectronics (partly FPC)",
    "10a5": "FPC (Fingerprint Cards)",
}
P2P_PORTS = {  # well-known P2P/privacy services (port -> protocol)
    6881: "BitTorrent (DHT/tracker)", 6882: "BitTorrent", 6883: "BitTorrent",
    6884: "BitTorrent", 6885: "BitTorrent", 6886: "BitTorrent",
    6887: "BitTorrent", 6888: "BitTorrent", 6889: "BitTorrent",
    51413: "Transmission",
    4661: "eMule", 4662: "eMule", 4672: "eMule (KAD)",
    6257: "DC++/KAD", 6699: "WinMX", 2710: "XBT Tracker",
    33445: "Tox", 33446: "Tox (UDP)",
    8333: "Bitcoin", 9735: "Lightning",
    18080: "Monero P2P", 18081: "Monero RPC", 55555: "Monero P2P (alt)",
    9000: "ZeroNet/Tor?", 43110: "ZeroNet",
    22011: "WOT?", 4444: "I2P", 7656: "I2P (SAM)", 6667: "IRC (possibly Tor)",
}


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Safe subprocess wrapper: (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {cmd[0]}"
    except Exception as e:  # noqa: BLE001 — the inventory must always complete
        return 1, "", str(e)


def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def read_sys(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def collect_system() -> dict:
    out = {"hostname": read_sys("/proc/sys/kernel/hostname")}
    rc, u, _ = run(["uname", "-srm"])
    out["kernel"] = u if rc == 0 else None
    os_release = read_sys("/etc/os-release")
    m = re.search(r'^PRETTY_NAME="?(.+?)"?$', os_release, re.M)
    out["os"] = m.group(1) if m else None
    out["uptime_s"] = read_sys("/proc/uptime").split()[0] or None
    out["arch"] = os.uname().machine
    return out


def collect_hardware() -> dict:
    hw: dict = {}
    # CPU
    cpuinfo = read_sys("/proc/cpuinfo")
    hw["cpu_model"] = (re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
                       or [None, None])[1]
    hw["cpu_cores"] = len(re.findall(r"^processor\s*:", cpuinfo, re.M))
    # RAM
    meminfo = read_sys("/proc/meminfo")
    kb = re.search(r"^MemTotal:\s*(\d+)", meminfo, re.M)
    hw["ram_kb"] = int(kb.group(1)) if kb else None
    # GPU (VGA/3D)
    if has("lspci"):
        _, out, _ = run(["lspci"])
        gpus = [ln.split(":", 2)[-1].strip() for ln in out.splitlines()
                if re.search(r"VGA compatible|3D controller|Display controller", ln)]
        hw["gpus"] = gpus
    # USB
    if has("lsusb"):
        _, out, _ = run(["lsusb"])
        hw["usb_devices"] = out.splitlines()
        fpc = []
        for ln in out.splitlines():
            m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})\s+(.*)", ln, re.I)
            if m:
                vid = m.group(1).lower()
                if vid in FPC_VENDORS:
                    fpc.append({"vid": vid, "pid": m.group(2),
                                "desc": m.group(3),
                                "known": FPC_VENDORS[vid]})
        hw["fingerprint_sensors"] = fpc
    # Disk
    if has("lsblk"):
        _, out, _ = run(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINTS", "-J"])
        try:
            hw["block_devices"] = json.loads(out)
        except json.JSONDecodeError:
            hw["block_devices"] = out
    return hw


def collect_fingerprint() -> dict:
    """fprintd driver/service status + enrolled fingers.

    Arch packages: fprintd (daemon), fprintd-clients (fprintd-list/enroll),
    libfprint (driver backend, the actual sensor support).
    """
    fp: dict = {"sensor_usb": None, "packages": {}, "daemon_present": None,
                "service_active": None, "service_enabled": None,
                "enrolled_users": [], "notes": []}
    if has("pacman"):
        rc, out, _ = run(["pacman", "-Q", "fprintd", "fprintd-clients",
                          "libfprint", "libfprint-2"])
        # rc!=0 is normal when single packages are missing — always parse the output
        for ln in out.splitlines():
            m_missing = re.search(r"package '([^']+)' was not found", ln)
            if m_missing:
                fp["packages"][m_missing.group(1)] = None
                continue
            p = ln.split()
            if len(p) == 2:
                fp["packages"][p[0]] = p[1]
    # fprintd >= 1.94: clients (/usr/bin/fprintd-*) ship in the daemon package,
    # there is no separate /usr/bin/fprintd; /usr/lib/fprintd is a directory.
    fp["daemon_present"] = (os.path.isdir("/usr/lib/fprintd")
                            or shutil.which("fprintd-list") is not None)
    if has("systemctl"):
        rc, out, _ = run(["systemctl", "is-active", "fprintd.service"])
        fp["service_active"] = out if rc in (0, 3) else None
        rc2, out2, _ = run(["systemctl", "is-enabled", "fprintd.service"])
        fp["service_enabled"] = out2 if rc2 in (0, 1, 3) else None
    if has("fprintd-list"):
        rc, out, _ = run(["fprintd-list"])
        fp["enrolled_users"] = out.splitlines() if rc == 0 else []
    if not fp["daemon_present"]:
        fp["notes"].append("fprintd daemon missing (/usr/lib/fprintd/fprintd)")
    return fp


def collect_network() -> dict:
    net: dict = {"interfaces": [], "listening": [], "p2p_ports": []}
    if has("ip"):
        _, out, _ = run(["ip", "-br", "addr"])
        net["interfaces"] = out.splitlines()
    if has("ss"):
        # -n forces numeric ports; the parser uses regex instead of columns
        # because the ss format varies by version/protocol (UDP: UNCONN).
        rc, out, _ = run(["ss", "-tulpn", "-n"], timeout=15)
        if rc == 0:
            for ln in out.splitlines()[1:]:
                m_proto = re.match(r"^(tcp|udp|tcp6|udp6)", ln)
                if not m_proto:
                    continue
                proto = m_proto.group(1)
                m_port = re.search(r":(\d+)\s+", ln)
                port = int(m_port.group(1)) if m_port else None
                m_local = re.search(r"\s((?:[0-9a-fA-F.:]+|\[[^\]]+\]|\*):\S+)\s", ln)
                local = m_local.group(1) if m_local else None
                m_proc = re.search(r'users:\(\("([^"]+)', ln)
                proc = m_proc.group(1) if m_proc else None
                entry = {"proto": proto, "local": local,
                         "process": proc}
                if port is not None:
                    entry["port"] = port
                    if port in P2P_PORTS:
                        entry["p2p"] = P2P_PORTS[port]
                        net["p2p_ports"].append(entry)
                net["listening"].append(entry)
    return net


def collect_services() -> list[dict]:
    if not has("systemctl"):
        return []
    _, out, _ = run(["systemctl", "list-units", "--type=service",
                     "--state=running", "--no-pager", "--no-legend"])
    svcs = []
    for ln in out.splitlines():
        p = ln.split()
        if len(p) >= 4 and p[0].endswith(".service"):
            svcs.append({"unit": p[0], "load": p[1], "active": p[2],
                         "sub": p[3]})
    return svcs


def collect_security(secret_roots: list[str]) -> dict:
    sec: dict = {"firewall": {}, "ssh": {}, "secrets_files": [], "warnings": []}
    # Firewall
    if has("ufw"):
        rc, out, _ = run(["ufw", "status"])
        sec["firewall"]["ufw"] = out if rc == 0 else None
    if has("nft"):
        rc, out, _ = run(["nft", "list", "ruleset"], timeout=15)
        sec["firewall"]["nftables"] = out if rc == 0 else None
        if rc != 0:
            sec["warnings"].append("nft ruleset not readable without sudo")
    # SSH
    rc, out, _ = run(["systemctl", "is-active", "sshd"])
    sec["ssh"]["sshd_active"] = out if rc in (0, 3) else None
    if has("ss"):
        _, out, _ = run(["ss", "-tlnp"])
        sec["ssh"]["sshd_listening"] = any(":22" in ln for ln in out.splitlines())
    # Secrets: scan file NAMES/paths only, never read or print contents
    patterns = (r".*\.env$", r".*secret.*", r".*credential.*",
                r".*\.pem$", r".*id_rsa$", r".*id_ed25519$")
    # Real secrets only: hide package/venv/archive noise
    ignore_dirs = {".git", "node_modules", ".cache", "__pycache__",
                   ".venv", "venv", "site-packages", "node_modules",
                   "testdata", "fixtures", "tests", "examples",
                   "pkg/mod", "Archive", "dist-packages", ".smart-env"}
    roots = [os.path.expanduser(r) for r in secret_roots]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ignore_dirs
                           and not d.endswith((".venv", "-venv"))]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if any(re.match(p, fn, re.I) for p in patterns):
                    sec["secrets_files"].append(os.path.relpath(full,
                                                                os.path.expanduser("~")))
    return sec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", "-o", help="write JSON to this file (optional)")
    ap.add_argument("--pretty", action="store_true", help="indent JSON")
    ap.add_argument("--secret-root", action="append",
                    help="directory to scan for secret-looking file names "
                         "(repeatable; default: ~/.config and ~/.ssh)")
    args = ap.parse_args()

    report = {
        "tool": "system_inventory",
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "read-only (no sudo, no installs)",
        "system": collect_system(),
        "hardware": collect_hardware(),
        "fingerprint": collect_fingerprint(),
        "network": collect_network(),
        "services_running": collect_services(),
        "security": collect_security(args.secret_root or ["~/.config", "~/.ssh"]),
    }
    data = json.dumps(report, indent=2 if args.pretty else None,
                      default=str, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(data + "\n")
        print(f"written: {args.output}")
    else:
        print(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
