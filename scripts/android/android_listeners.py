#!/usr/bin/env python3
"""android_listeners.py -- read-only: map open TCP listeners on an Android device to their owning UID/package.

Purpose:
    A monitor that only says "something listens on 0.0.0.0:xxxx" is not very
    actionable. This tool closes that gap: it reads /proc/net/tcp{,6} (uid column)
    via adb, maps the UID to packages with `pm list packages -U` and classifies
    the bind address against the device's own address list (`ip -o addr`).

    Classes per listener:
      loopback   127.0.0.0/8 or ::1, reachable locally only
      wildcard   0.0.0.0 / ::, reachable on ALL interfaces (worth an alarm)
      own        one of the device's own addresses (wlan0/rndis0/ap0/...),
                 reachable from the same network segment
      foreign    an address not assigned to the device (worth an alarm)

Usage:
    android_listeners.py                 table
    android_listeners.py --json          machine-readable
    android_listeners.py --nonlocal      only own/wildcard/foreign
    android_listeners.py --watch 300     sample every 15 s for 5 min, report new ports at the end

    Read-only (cat /proc/net/tcp*, ip, pm list packages -U). No writes, no root.
    Exit 0 = ok, 1 = adb/device not usable. Device: --serial or $ANDROID_SERIAL.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

ADB = os.environ.get("ANDROID_ADB", "adb")
ADB_TIMEOUT = 40
LOOPBACK6 = {"::1", "0:0:0:0:0:0:0:1"}


def adb(serial, args, timeout=ADB_TIMEOUT):
    cmd = [ADB] + (["-s", serial] if serial else []) + ["shell"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout


def pick_serial(explicit=None):
    if explicit:
        return explicit
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=ADB_TIMEOUT).stdout
    online = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]
    return online[0] if online else None


def hex_to_ipv4(h):
    return socket.inet_ntoa(struct.pack("<I", int(h, 16)))


def hex_to_ipv6(h):
    """Hex from /proc/net/tcp6 -> canonical notation (4-byte words, little-endian)."""
    raw = b"".join(struct.pack("<I", int(h[i:i + 8], 16)) for i in range(0, 32, 8))
    return socket.inet_ntop(socket.AF_INET6, raw)


def parse_proc_tcp(text, v6=False):
    """LISTEN rows from /proc/net/tcp{,6}: (addr, port, uid)."""
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        if f[3].upper() != "0A":  # TCP_LISTEN
            continue
        local, lport = f[1].rsplit(":", 1)
        try:
            addr = hex_to_ipv6(local) if v6 else hex_to_ipv4(local)
            port = int(lport, 16)
            uid = int(f[7])
        except Exception:
            continue
        out.append((addr, port, uid))
    return out


def uid_map(serial):
    """UID -> package name(s) from `pm list packages -U`."""
    m = {}
    for line in adb(serial, ["pm", "list", "packages", "-U"]).splitlines():
        pkg = re.search(r"package:(\S+)", line)
        uid = re.search(r"uid:(\d+)", line)
        if pkg and uid:
            m.setdefault(int(uid.group(1)), []).append(pkg.group(1))
    return m


def own_addresses(serial):
    addrs = set()
    for line in adb(serial, ["ip", "-o", "addr"]).splitlines():
        m = re.search(r"inet6?\s+(\S+?)/\d+", line)
        if m:
            addrs.add(m.group(1).split("%")[0])
    return addrs


def classify(addr, own):
    mapped = re.match(r"^::ffff:(\d+\.\d+\.\d+\.\d+)$", addr)
    plain = mapped.group(1) if mapped else addr
    if plain.startswith("127.") or plain in LOOPBACK6:
        return "loopback"
    if plain in ("0.0.0.0", "::"):
        return "wildcard"
    return "own" if plain in own else "foreign"


def snapshot(serial, umap=None, own=None):
    umap = uid_map(serial) if umap is None else umap
    own = own_addresses(serial) if own is None else own
    rows = []
    for v6 in (False, True):
        text = adb(serial, ["cat", "/proc/net/tcp6" if v6 else "/proc/net/tcp"])
        for addr, port, uid in parse_proc_tcp(text, v6=v6):
            rows.append({
                "class": classify(addr, own),
                "addr": addr,
                "port": port,
                "uid": uid,
                "packages": umap.get(uid, []),
            })
    rows.sort(key=lambda r: (r["class"], r["port"]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only: map open TCP listeners on an Android device to UID/package.")
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--non-loopback", "--nonlocal", dest="non_loopback", action="store_true",
                    help="only own/wildcard/foreign (not loopback)")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="sample for this many seconds and report new non-loopback listeners")
    ap.add_argument("--interval", type=int, default=15)
    args = ap.parse_args(argv)

    serial = pick_serial(args.serial)
    if not serial:
        print("adb: no device online", file=sys.stderr)
        return 1

    rows = snapshot(serial)
    if not rows:
        print("no listeners read (adb/permissions?)", file=sys.stderr)
        return 1

    if args.watch:
        seen = {(r["addr"], r["port"]) for r in rows}
        new = []
        end = time.time() + args.watch
        while time.time() < end:
            time.sleep(min(args.interval, max(1, int(end - time.time()))))
            for r in snapshot(serial):
                key = (r["addr"], r["port"])
                if key not in seen:
                    seen.add(key)
                    if r["class"] != "loopback":
                        r["detected"] = time.strftime("%H:%M:%S")
                        new.append(r)
        if args.json:
            print(json.dumps(new, ensure_ascii=False, indent=1))
        else:
            print(f"Watch {args.watch}s: {len(new)} new non-loopback listeners")
            for r in new:
                pk = ",".join(r["packages"]) or "-"
                print(f"  {r['detected']} {r['class']:8} {r['addr']}:{r['port']} uid={r['uid']} {pk}")
        return 0

    rows = [r for r in rows if not args.non_loopback or r["class"] != "loopback"]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(f"{len(rows)} listeners")
        for r in rows:
            pk = ",".join(r["packages"]) or "-"
            print(f"  {r['class']:8} {r['addr']}:{r['port']:<6} uid={r['uid']:<6} {pk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
