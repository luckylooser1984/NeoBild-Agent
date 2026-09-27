#!/usr/bin/env python3
"""phone_transfer.py -- file transfer between a PC and a USB-connected Android phone via adb, SHA-256 verified.

Purpose:
    On some devices USB tethering (RNDIS) and MTP exclude each other, so you
    either have internet over the cable or file transfer - not both. adb is
    available in both modes, so this tool moves files over adb and verifies
    every single-file transfer with SHA-256 on both sides.

Examples:
    phone_transfer.py devices
    phone_transfer.py ls /sdcard/Download
    phone_transfer.py pull /sdcard/Download/photo.jpg
    phone_transfer.py pull /sdcard/Music/Album --dest-dir ~/Music
    phone_transfer.py push song.mp3 /sdcard/Music/

Exit codes: 0 = ok, 1 = error, 2 = usage/device error

Author: Lukas Weißmann
License: MIT
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys

ADB = os.environ.get("ANDROID_ADB") or shutil.which("adb") or "adb"
DEFAULT_DEST = os.path.expanduser("~/Downloads")
DEFAULT_REMOTE_ROOT = "/sdcard"


def run(args: list[str], serial: str | None = None) -> subprocess.CompletedProcess:
    cmd = [ADB] + (["-s", serial] if serial else []) + args
    return subprocess.run(cmd, capture_output=True, text=True)


def shell(cmdline: str, serial: str | None = None) -> subprocess.CompletedProcess:
    return run(["shell", cmdline], serial)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_device(requested: str | None) -> str:
    res = run(["devices"])
    lines = [l for l in res.stdout.splitlines()[1:] if l.strip()]
    ready = [l.split()[0] for l in lines if "\tdevice" in l]
    if requested:
        if requested not in ready:
            print(f"ERROR: device {requested} not connected/authorized.", file=sys.stderr)
            sys.exit(2)
        return requested
    if not ready:
        print("ERROR: no phone connected. Check the USB cable and that adb is\n"
              "  offered in the current USB mode. See: adb devices", file=sys.stderr)
        sys.exit(2)
    if len(ready) > 1:
        print(f"ERROR: {len(ready)} devices connected -> use --serial.", file=sys.stderr)
        sys.exit(2)
    return ready[0]


def remote_is_dir(path: str, serial: str | None) -> bool:
    return shell(f"test -d {shlex.quote(path)}", serial).returncode == 0


def remote_sha256(path: str, serial: str | None) -> str | None:
    res = shell(f"sha256sum {shlex.quote(path)}", serial)
    if res.returncode != 0:
        return None
    parts = res.stdout.split()
    return parts[0] if parts else None


def cmd_devices(serial: str | None, _args) -> int:
    print(run(["devices", "-l"]).stdout.strip())
    return 0


def cmd_ls(serial: str | None, args) -> int:
    res = shell(f"ls -la {shlex.quote(args.remote)}", serial)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return 0 if res.returncode == 0 else 1


def cmd_pull(serial: str | None, args) -> int:
    remote = args.remote
    if shell(f"test -e {shlex.quote(remote)}", serial).returncode != 0:
        print(f"ERROR: not present on the phone: {remote}", file=sys.stderr)
        return 1
    os.makedirs(args.dest_dir, exist_ok=True)
    res = run(["pull", "-a", remote, args.dest_dir], serial)
    print(res.stdout.strip())
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return 1
    name = os.path.basename(remote.rstrip("/")) or os.path.basename(remote)
    local = os.path.join(args.dest_dir, name)
    if os.path.isdir(local):
        print(f"OK (directory): {local}")
        return 0
    rs, ls_ = remote_sha256(remote, serial), sha256_file(local)
    if rs and rs == ls_:
        print(f"OK sha256 matches: {local}  ({ls_[:16]}...)")
        return 0
    print(f"WARNING: sha256 not confirmed (remote={rs} local={ls_})", file=sys.stderr)
    return 1


def cmd_push(serial: str | None, args) -> int:
    local = args.local
    if not os.path.exists(local):
        print(f"ERROR: not present locally: {local}", file=sys.stderr)
        return 1
    is_dir = os.path.isdir(local)
    base = os.path.basename(local.rstrip("/"))
    if not args.remote:
        target = f"{DEFAULT_REMOTE_ROOT}/Download/{base}"
    elif args.remote.endswith("/") or remote_is_dir(args.remote, serial):
        target = args.remote.rstrip("/")
        if not is_dir:
            target = f"{target}/{base}"
    else:
        target = args.remote
    res = run(["push", local, target], serial)
    print(res.stdout.strip())
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return 1
    if is_dir:
        print(f"OK (directory) -> {target}")
        return 0
    rs, ls_ = remote_sha256(target, serial), sha256_file(local)
    if rs and rs == ls_:
        print(f"OK sha256 matches on the phone: {target}")
        return 0
    print(f"WARNING: sha256 not confirmed (remote={rs} local={ls_})", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="File transfer PC <-> Android phone over adb with SHA-256 verification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"),
                    help="adb serial (only needed with several devices)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("devices", help="connected devices")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("ls", help="list a directory on the phone")
    p.add_argument("remote")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("pull", help="phone -> PC")
    p.add_argument("remote")
    p.add_argument("--dest-dir", default=DEFAULT_DEST, help=f"target (default: {DEFAULT_DEST})")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("push", help="PC -> phone")
    p.add_argument("local")
    p.add_argument("remote", nargs="?", help="default: /sdcard/Download/<name>")
    p.set_defaults(func=cmd_push)

    args = ap.parse_args()
    args.serial = pick_device(args.serial)
    return args.func(args.serial, args)


if __name__ == "__main__":
    sys.exit(main())
