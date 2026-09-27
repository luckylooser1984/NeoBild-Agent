#!/usr/bin/env python3
"""phone_ui.py -- minimal adb UI helper for driving an Android screen (no uiautomator2 server needed).

Purpose:
    Gives an agent (or a human in a terminal) a compact, text-only view of the
    current screen and simple actions on it. It uses only `uiautomator dump` and
    `input` over adb, so nothing has to be installed on the device.

Commands:
    ui [filter]           filtered, numbered element tree of the current screen
    tap <nr|text>         tap an element (number from the last `ui` output, or text/description)
    type <text>           type ASCII text (spaces ok)
    key <back|home|enter|recents|wakeup|menu>
    swipe <up|down|left|right>
    wait <text> [sec]     wait until text appears in the tree (default 6 s)
    state                 short status (focused window, lock screen)

    Device: $ANDROID_SERIAL (adb picks the only device otherwise).
    Note: swipe coordinates assume a ~720 px wide portrait screen; adjust SWIPE_BOX.

Author: Lukas Weißmann
License: MIT
"""
import os
import re
import subprocess
import sys
import time

ADB = os.environ.get("ANDROID_ADB", "adb")
DUMP = "/sdcard/.phone_ui.xml"


def adb(*args, binary=False, timeout=30):
    r = subprocess.run([ADB, *args], capture_output=True, text=not binary, timeout=timeout)
    return r.stdout


def dump(require=None):
    """UI dump; with require=<package name> retry until exactly that app is in the
    tree (after app switches uiautomator sometimes returns a stale window)."""
    x = ""
    for _ in range(6):
        adb("shell", "uiautomator", "dump", DUMP)
        x = adb("exec-out", "cat", DUMP)
        if isinstance(x, bytes):
            x = x.decode("utf-8", "replace")
        if "<hierarchy" in x and len(x) > 400 and (not require or require in x):
            return x
        time.sleep(1.0)
    return x


def nodes(x):
    out = []
    for m in re.finditer(r"<node[^>]*?>", x):
        t = m.group(0)

        def g(k):
            mm = re.search(k + r'="([^"]*)"', t)
            return mm.group(1) if mm else ""

        mb = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", g("bounds"))
        if not mb:
            continue
        x1, y1, x2, y2 = map(int, mb.groups())
        out.append({
            "text": g("text"), "desc": g("content-desc"), "cls": g("class").split(".")[-1],
            "pkg": g("package"), "rid": g("resource-id"), "clk": g("clickable") == "true",
            "en": g("enabled") == "true", "chk": g("checked"), "chkbl": g("checkable"),
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "w": x2 - x1, "h": y2 - y1, "b": g("bounds"),
            "scroll": g("scrollable") == "true",
        })
    return out


def label(n):
    return n["text"] or n["desc"] or ""


def cmd_ui(args):
    ns = nodes(dump())
    filt = args[0].lower() if args else None
    i = 0
    for n in ns:
        lab = label(n)
        if filt and filt not in (lab + " " + n["cls"] + " " + n["pkg"] + " " + n["rid"]).lower():
            continue
        if not (lab or n["clk"] or n["scroll"]):
            continue
        i += 1
        n["_i"] = i
        flags = []
        if n["clk"]:
            flags.append("TAP")
        if n["scroll"]:
            flags.append("SCROLL")
        if not n["en"]:
            flags.append("disabled")
        print(f'{i:>3} ({n["cx"]:>4},{n["cy"]:>4}) {n["cls"]:<14} {" ".join(flags):<12} '
              f'{n["pkg"].split(".")[-1]:<12} {lab[:60]!r} {n["rid"][:40]}')
    if not i:
        print("(no elements)")
    # Context lines without a number (static text)
    print("--- text ---")
    seen = set()
    for n in ns:
        lab = label(n)
        if lab and not n.get("_i") and lab not in seen:
            seen.add(lab)
            print(f'    ({n["cx"]:>4},{n["cy"]:>4}) {n["cls"]:<14} {lab[:70]!r}')


def find(ns, needle, exact=False):
    n_low = needle.lower()
    hits = []
    for n in ns:
        lab = label(n)
        if not lab:
            continue
        if (lab == needle) if exact else (n_low in lab.lower()):
            hits.append(n)
    return hits


def cmd_tap(args):
    if not args:
        print("tap: missing argument")
        return 1
    target = args[0]
    ns = nodes(dump())
    if target.isdigit():
        idx = int(target)
        numbered = [n for n in ns if label(n) or n["clk"] or n["scroll"]]
        if idx > len(numbered):
            print(f"tap: number {idx} > {len(numbered)}")
            return 1
        n = numbered[idx - 1]
    else:
        hits = find(ns, target, exact="--exact" in args)
        if not hits:
            print(f"tap: '{target}' not found")
            return 1
        if len(hits) > 1:
            print(f"tap: {len(hits)} hits for '{target}': "
                  + "; ".join(sorted({label(h)[:30] for h in hits})))
        n = hits[0]
    adb("shell", "input", "tap", str(n["cx"]), str(n["cy"]))
    print(f'tap {n["cx"]},{n["cy"]} -> {label(n)[:50]!r}')
    return 0


def cmd_type(args):
    txt = " ".join(args)
    if not txt:
        print("type: missing text")
        return 1
    if not txt.isascii():
        print("type: ASCII only")
        return 1
    adb("shell", "input", "text", txt.replace(" ", "%s"))
    print(f'type: "{txt}"')
    return 0


KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
        "recents": "KEYCODE_APP_SWITCH", "wakeup": "KEYCODE_WAKEUP", "menu": "KEYCODE_MENU"}

X, Y = 360, 800
SWIPE_BOX = {"up": (X, Y + 400, X, Y - 400), "down": (X, Y - 400, X, Y + 400),
             "left": (560, Y, 120, Y), "right": (120, Y, 560, Y)}


def cmd_key(args):
    k = (args[0] if args else "").lower()
    if k not in KEYS:
        print(f"key: unknown ({k}); allowed: {', '.join(KEYS)}")
        return 1
    adb("shell", "input", "keyevent", KEYS[k])
    print(f"key {k}")
    return 0


def cmd_swipe(args):
    d = (args[0] if args else "up").lower()
    if d not in SWIPE_BOX:
        print("swipe: up|down|left|right")
        return 1
    adb("shell", "input", "swipe", *map(str, SWIPE_BOX[d]), "250")
    print(f"swipe {d}")
    return 0


def cmd_wait(args):
    if not args:
        print("wait: missing text")
        return 1
    needle = args[0]
    tmax = float(args[1]) if len(args) > 1 else 6.0
    t0 = time.time()
    while time.time() - t0 < tmax:
        if find(nodes(dump()), needle):
            print(f"OK: '{needle}' present after {time.time()-t0:.1f}s")
            return 0
        time.sleep(0.8)
    print(f"TIMEOUT: '{needle}' not present after {tmax}s")
    return 1


def cmd_state(args):
    w = adb("shell", "dumpsys", "window")
    for line in w.splitlines():
        if "mCurrentFocus" in line or "mDreamingLockscreen" in line:
            print(line.strip())
    return 0


CMDS = {"ui": cmd_ui, "tap": cmd_tap, "type": cmd_type, "key": cmd_key,
        "swipe": cmd_swipe, "wait": cmd_wait, "state": cmd_state}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    sys.exit(CMDS[sys.argv[1]](sys.argv[2:]) or 0)
