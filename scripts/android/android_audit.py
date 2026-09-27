#!/usr/bin/env python3
"""android_audit.py -- local, read-only hardening audit + anomaly monitor for an Android device over adb.

Purpose:
    Takes a deterministic snapshot of the security-relevant state of your own
    Android device (build/boot properties, security settings, AppOps special
    rights, listening ports, device admins, /data/local/tmp, installed packages
    and their granted "risky" permissions) and compares it against a baseline.
    New accessibility services, a freshly enabled HTTP proxy, an app suddenly
    granted RECORD_AUDIO, a new externally reachable listener ... show up as
    ranked findings.

    stdlib only. No network (except the optional `deep --resolve`, which calls a
    local whois). No writing adb commands: this script NEVER changes the
    device, it only finds and reports.

Subcommands:
    snapshot [--out PATH]      current state as JSON (deterministically sorted)
    diff [--baseline PATH]     current state vs. baseline -> findings (exit 10 if any)
    report                     Markdown report of the current state
    watch [--baseline PATH]    cron helper: diff with rate-limited offline notices;
                               stdout stays empty while everything is quiet
    deep [--resolve]           weekly deep check: established connections, never-seen
                               peers, foreground services, SELinux denials, suspicious log lines

Configuration:
    ANDROID_SERIAL             device to use (default: first online device)
    ANDROID_ADB                adb binary (default: adb from PATH)
    ANDROID_AUDIT_DIR          state folder (default: <script dir>/baseline)
    ANDROID_AUDIT_SYSTEM_PREFIXES  extra comma-separated package prefixes to treat as
                               system/OEM noise (e.g. your vendor's "com.vendor.")

Author: Lukas Weißmann
License: MIT
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

# Absolute adb path: PATH is not reliable in cron/subshell contexts,
# so try env first, then which, then /usr/bin/adb.
ADB_BIN = os.environ.get("ANDROID_ADB") or shutil.which("adb") or "/usr/bin/adb"

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("ANDROID_AUDIT_DIR") or os.path.join(HERE, "baseline")
DEFAULT_BASELINE = os.path.join(BASE_DIR, "baseline.json")
STATE_FILE = os.path.join(BASE_DIR, "watch_state.json")

ADB_TIMEOUT = 40

# --- Risky permissions (camera/mic/location/communication/files) -------------
RISKY_PERMS = [
    "CAMERA", "RECORD_AUDIO", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
    "ACCESS_BACKGROUND_LOCATION", "READ_SMS", "RECEIVE_SMS", "SEND_SMS",
    "READ_CALL_LOG", "WRITE_CALL_LOG", "READ_CONTACTS", "WRITE_CONTACTS",
    "PROCESS_OUTGOING_CALLS", "ANSWER_PHONE_CALLS", "CALL_PHONE",
    "READ_PHONE_NUMBERS", "READ_PHONE_STATE", "MANAGE_EXTERNAL_STORAGE",
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES",
    "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO", "BODY_SENSORS",
    "ACTIVITY_RECOGNITION", "SYSTEM_ALERT_WINDOW", "REQUEST_INSTALL_PACKAGES",
    "PACKAGE_USAGE_STATS", "GET_ACCOUNTS", "READ_CALENDAR", "WRITE_CALENDAR",
    "QUERY_ALL_PACKAGES", "ACCESS_MEDIA_LOCATION", "SCHEDULE_EXACT_ALARM",
    "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN", "NEARBY_WIFI_DEVICES",
    "WRITE_SECURE_SETTINGS", "READ_LOGS", "DUMP", "BIND_ACCESSIBILITY_SERVICE",
    "BIND_NOTIFICATION_LISTENER_SERVICE", "BIND_DEVICE_ADMIN",
]

# --- AppOps that represent special rights ------------------------------------
WATCHED_APP_OPS = [
    "SYSTEM_ALERT_WINDOW", "REQUEST_INSTALL_PACKAGES", "GET_USAGE_STATS",
    "ACCESS_RESTRICTED_SETTINGS", "MANAGE_EXTERNAL_STORAGE", "RECORD_AUDIO",
    "CAMERA", "FINE_LOCATION", "COARSE_LOCATION", "BACKGROUND_LOCATION",
    "READ_CLIPBOARD", "WRITE_CLIPBOARD", "PROJECT_MEDIA", "MOCK_LOCATION",
    "ACTIVATE_VPN", "READ_NOTIFICATION", "ACCESS_NOTIFICATIONS",
    "BIND_ACCESSIBILITY_SERVICE", "SCHEDULE_EXACT_ALARM", "SYSTEM_EXEMPT_FROM_POWER_RESTRICTIONS",
    "RUN_IN_BACKGROUND", "START_FOREGROUND", "TOAST_WINDOW",
]

SETTINGS_GLOBAL = [
    "adb_enabled", "adb_wifi_enabled", "development_settings_enabled",
    "private_dns_mode", "private_dns_specifier", "http_proxy",
    "package_verifier_enable", "package_verifier_user_consent",
    "upload_apk_enable", "stay_on_while_plugged_in", "auto_time",
]
SETTINGS_SECURE = [
    "enabled_accessibility_services", "accessibility_enabled",
    "enabled_notification_listeners", "install_non_market_apps",
    "default_input_method", "enabled_input_methods", "always_on_vpn_app",
    "always_on_vpn_lockdown", "user_setup_complete", "lockscreen.disabled",
    "lock_screen_lock_after_timeout", "mount_play_not_snd", "adb_enabled",
]
SETTINGS_SYSTEM = [
    "lockscreen.disabled", "screen_brightness_mode",
]

PROPS = [
    "ro.product.model", "ro.product.brand", "ro.build.version.release",
    "ro.build.version.sdk", "ro.build.version.security_patch",
    "ro.build.fingerprint", "ro.build.type", "ro.build.tags",
    "ro.debuggable", "ro.secure", "ro.adb.secure", "ro.crypto.state",
    "ro.crypto.type", "ro.boot.verifiedbootstate", "ro.boot.flash.locked",
    "ro.oem_unlock_supported", "ro.kernel.qemu", "service.adb.tcp.port",
    "ro.boot.veritymode", "ro.boot.vbmeta.device_state", "vendor.security.patch",
    "ro.system.build.version.security_patch", "ro.vendor.build.version.security_patch",
]

# System packages kept quiet in reports (noise, no insight). Add your OEM's
# prefix via ANDROID_AUDIT_SYSTEM_PREFIXES.
SYSTEM_PREFIXES = (
    "android", "com.android.", "com.google.android.", "com.android", "com.mediatek",
    "com.qualcomm", "com.google.android", "com.google.", "org.codeaurora",
) + tuple(p.strip() for p in os.environ.get("ANDROID_AUDIT_SYSTEM_PREFIXES", "").split(",") if p.strip())


def adb(args, serial=None, timeout=ADB_TIMEOUT):
    cmd = [ADB_BIN]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 124


def pick_serial():
    env = os.environ.get("ANDROID_SERIAL")
    candidates = []
    if env:
        candidates.append(env)
    out, _, _ = adb(["devices"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            candidates.append(parts[0])
    for cand in candidates:
        state, _, _ = adb(["get-state"], serial=cand)
        if state.strip() == "device":
            return cand
    return None


def sh(serial, command, timeout=ADB_TIMEOUT):
    out, err, rc = adb(["shell", command], serial=serial, timeout=timeout)
    return out


def parse_dump_packages(text):
    """dumpsys package -> {pkg: {...}} (only the factual fields we evaluate)."""
    out = {}
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"\n  Package \[([^\]]+)\] \(", text)]
    starts.append((len(text), None))
    for i in range(len(starts) - 1):
        pos, name = starts[i]
        body = text[pos:starts[i + 1][0]]
        d = {}
        for key, pat in (
            ("versionName", r"versionName=(\S+)"),
            ("versionCode", r"versionCode=(\d+)"),
            ("targetSdk", r"targetSdk=(\d+)"),
            ("installer", r"installerPackageName=(\S+)"),
            ("codePath", r"codePath=(\S+)"),
            ("firstInstallTime", r"firstInstallTime=([^\n]+)"),
            ("lastUpdateTime", r"lastUpdateTime=([^\n]+)"),
            ("flags", r"\n    flags=\[([^\]]*)\]"),
        ):
            m = re.search(pat, body)
            d[key] = m.group(1).strip() if m else None
        m = re.search(r"User 0:[^\n]*enabled=(\d+)", body)
        d["enabledUser0"] = m.group(1) if m else None
        perms = {}
        for sect in ("runtime permissions:", "install permissions:"):
            if sect in body:
                seg = body.split(sect, 1)[1]
                seg = re.split(r"\n    \S|\n  Package \[", seg)[0]
                for pm in re.finditer(r"^\s+(android\.permission\.[A-Z0-9_]+|[a-zA-Z0-9_.]+): granted=(\w+)", seg, re.M):
                    if pm.group(2) == "true":
                        perms[pm.group(1)] = True
        d["granted"] = sorted(perms)
        out[name] = d
    return out


def collect_packages(serial):
    pkgs = {}
    out = sh(serial, "pm list packages -u -f -i", timeout=60)
    for line in out.splitlines():
        m = re.match(r"package:(.*?)=([\w.]+)\s*(?:installer=(\S+))?", line.strip())
        if not m:
            continue
        path, name, installer = m.group(1), m.group(2), m.group(3)
        pkgs[name] = {"apkPath": path, "installer": installer}
    # Mark packages disabled for the user
    disabled = sh(serial, "pm list packages -d --user 0", timeout=40)
    state = {}
    for line in disabled.splitlines():
        if line.startswith("package:"):
            state[line.strip().split(":", 1)[1]] = "disabled"
    for name in pkgs:
        pkgs[name]["userState"] = state.get(name, "enabled")
    return pkgs


def collect():
    serial = pick_serial()
    if not serial:
        return None, "no device reachable via adb (USB cable / wireless debugging?)"
    state = sh(serial, "getprop")
    props = {}
    for key in PROPS:
        m = re.search(r"\[%s\]: \[(.*?)\]" % re.escape(key), state)
        props[key] = m.group(1) if m else None
    # Sanity guard: without the model prop the collection is broken (adb hiccup,
    # offline, unauthorized) -- report an error, NOT a device change.
    if not props.get("ro.product.model") or not props.get("ro.build.version.sdk"):
        return None, "adb returned no usable device props (getprop empty) -- device offline/unauthorized?"
    settings = {"global": {}, "secure": {}, "system": {}}
    for scope, keys in (("global", SETTINGS_GLOBAL), ("secure", SETTINGS_SECURE), ("system", SETTINGS_SYSTEM)):
        for key in keys:
            val = sh(serial, "settings get %s %s" % (scope, key)).strip()
            settings[scope][key] = None if val in ("null", "") else val
    appops = {}
    for op in WATCHED_APP_OPS:
        for mode in ("allow", "ignore", "deny", "foreground"):
            out = sh(serial, "cmd appops query-op %s %s" % (op, mode))
            if "No operations." in out or "No, operations" in out:
                continue
            names = sorted(x.strip() for x in out.split() if x.strip() and not x.startswith("No,"))
            if names:
                appops[op] = {"mode": mode, "packages": names}
                break
    listeners = []
    out = sh(serial, "ss -tln")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "LISTEN":
            listeners.append({"addr": parts[3]})
    listeners.sort(key=lambda x: x["addr"])
    tmp = sorted(x.strip() for x in sh(serial, "ls -a /data/local/tmp").splitlines() if x.strip() not in (".", ".."))
    policy = sh(serial, "dumpsys device_policy")
    admins = []
    m = re.search(r"Enabled Device Admins \(User 0[^)]*\):(.*?)(?:\n  \n|\n    mPasswordOwner)", policy, re.S)
    if m:
        admins = sorted(x.strip() for x in m.group(1).splitlines() if x.strip())
    admin_active = bool(admins)
    users = [l.strip() for l in sh(serial, "pm list users").splitlines() if l.strip().startswith("UserInfo")]
    dumpsys = sh(serial, "dumpsys package", timeout=120)
    pkgs = collect_packages(serial)
    details = parse_dump_packages(dumpsys) if dumpsys else {}
    for name, d in pkgs.items():
        det = details.get(name, {})
        d.update({k: det.get(k) for k in ("versionName", "versionCode", "targetSdk", "firstInstallTime", "lastUpdateTime", "granted", "enabledUser0")})
        d["granted"] = det.get("granted", [])
    snap = {
        "schema": 2,
        "serial": serial,
        "props": props,
        "settings": settings,
        "appops": appops,
        "special": {
            "listeners": listeners,
            "deviceAdminActive": admin_active,
            "users": users,
            "localTmp": tmp,
        },
        "packages": pkgs,
    }
    return snap, None


def is_system(name):
    return name.startswith(SYSTEM_PREFIXES) or name in ("android",)


def risky_in(pkg):
    return sorted({p.rsplit(".", 1)[-1] for p in pkg.get("granted", []) if any(k in p for k in RISKY_PERMS)})


# --------------------------------------------------------------------------- #
# Diff / anomaly rating
# --------------------------------------------------------------------------- #

def diff(old, new):
    findings = []  # (severity, text)

    def add(sev, text):
        findings.append((sev, text))

    op, np_ = old["props"], new["props"]
    # Device identity / integrity
    for key, sev, label in (
        ("ro.boot.verifiedbootstate", "critical", "Verified boot state"),
        ("ro.boot.flash.locked", "critical", "Bootloader lock"),
        ("ro.debuggable", "critical", "ro.debuggable"),
        ("ro.secure", "critical", "ro.secure"),
        ("ro.adb.secure", "critical", "ro.adb.secure"),
        ("ro.build.type", "high", "Build type"),
        ("ro.crypto.state", "high", "Encryption"),
        ("ro.kernel.qemu", "high", "Emulator flag"),
        ("service.adb.tcp.port", "high", "ADB TCP port"),
    ):
        if op.get(key) != np_.get(key):
            add(sev, "%s changed: %r -> %r" % (label, op.get(key), np_.get(key)))
    if op.get("ro.build.version.security_patch") != np_.get("ro.build.version.security_patch"):
        add("info", "Security patch level: %s -> %s" % (op.get("ro.build.version.security_patch"), np_.get("ro.build.version.security_patch")))

    # Settings
    sev_map = {
        ("global", "adb_enabled"): ("high", "USB debugging"),
        ("global", "adb_wifi_enabled"): ("critical", "Wireless debugging"),
        ("global", "development_settings_enabled"): ("high", "Developer options"),
        ("global", "private_dns_mode"): ("high", "Private DNS mode"),
        ("global", "private_dns_specifier"): ("high", "Private DNS server"),
        ("global", "http_proxy"): ("critical", "HTTP proxy"),
        ("global", "package_verifier_enable"): ("critical", "Play Protect verifier"),
        ("secure", "always_on_vpn_app"): ("high", "Always-on VPN"),
        ("secure", "always_on_vpn_lockdown"): ("high", "VPN lockdown"),
        ("secure", "install_non_market_apps"): ("critical", "Unknown sources (global)"),
        ("secure", "accessibility_enabled"): ("critical", "Accessibility enabled"),
        ("secure", "lockscreen.disabled"): ("critical", "Lock screen disabled"),
        ("secure", "lock_screen_lock_after_timeout"): ("high", "Lock screen timeout"),
        ("secure", "default_input_method"): ("high", "Default keyboard"),
    }
    for (scope, key), (sev, label) in sev_map.items():
        o = old.get("settings", {}).get(scope, {}).get(key)
        n = new.get("settings", {}).get(scope, {}).get(key)
        if o != n:
            add(sev, "%s (%s/%s): %r -> %r" % (label, scope, key, o, n))

    # Accessibility services / notification listeners / IMEs: new = report immediately
    for scope, key, sev, label in (
        ("secure", "enabled_accessibility_services", "critical", "accessibility service"),
        ("secure", "enabled_notification_listeners", "high", "notification listener"),
        ("secure", "enabled_input_methods", "high", "keyboard (IME)"),
    ):
        o = set((old.get("settings", {}).get(scope, {}).get(key) or "").split(":")) - {""}
        n = set((new.get("settings", {}).get(scope, {}).get(key) or "").split(":")) - {""}
        for x in sorted(n - o):
            add(sev, "NEW %s: %s" % (label, x))
        for x in sorted(o - n):
            add("info", "%s removed: %s" % (label, x))

    # AppOps: newly granted special rights
    for op, val in new.get("appops", {}).items():
        oldval = old.get("appops", {}).get(op) or {}
        oldpk = set(oldval.get("packages", []))
        for p in sorted(set(val["packages"]) - oldpk):
            if is_system(p) and op not in ("SYSTEM_ALERT_WINDOW",):
                continue
            add("high" if op in ("SYSTEM_ALERT_WINDOW", "REQUEST_INSTALL_PACKAGES", "GET_USAGE_STATS", "MANAGE_EXTERNAL_STORAGE", "ACTIVATE_VPN") else "medium",
                "AppOp %s=%s NEW for %s" % (op, val.get("mode"), p))

    # Device admin
    if not old["special"]["deviceAdminActive"] and new["special"]["deviceAdminActive"]:
        add("critical", "Device admin newly active")
    if old["special"]["deviceAdminActive"] and not new["special"]["deviceAdminActive"]:
        add("high", "Device admin no longer active")

    # Network listeners
    oldl = {l["addr"] for l in old["special"]["listeners"]}
    newl = {l["addr"] for l in new["special"]["listeners"]}
    for a in sorted(newl - oldl):
        host = a.rsplit(":", 1)[0]
        ext = not (host.startswith("127.") or host in ("[::1]", "::1"))
        add("critical" if ext else "medium", "NEW listening port: %s%s" % (a, "  (reachable from outside!)" if ext else ""))

    # /data/local/tmp
    for f in sorted(set(new["special"]["localTmp"]) - set(old["special"]["localTmp"])):
        add("high", "New file in /data/local/tmp: %s" % f)
    for f in sorted(set(old["special"]["localTmp"]) - set(new["special"]["localTmp"])):
        add("info", "/data/local/tmp cleaned up: %s" % f)

    # Packages
    opk, npk = old["packages"], new["packages"]
    for name in sorted(set(npk) - set(opk)):
        inst = npk[name].get("installer") or "?"
        sev = "critical" if inst == "com.google.android.packageinstaller" else "high"
        add(sev, "NEWLY INSTALLED: %s (version %s, installer %s) -- rights: %s" % (
            name, npk[name].get("versionName"), inst, ",".join(risky_in(npk[name])) or "-"))
    for name in sorted(set(opk) - set(npk)):
        add("info", "UNINSTALLED: %s" % name)
    for name in sorted(set(opk) & set(npk)):
        o, n = opk[name], npk[name]
        if o.get("versionName") != n.get("versionName"):
            add("info", "Update %s: %s -> %s" % (name, o.get("versionName"), n.get("versionName")))
        newperms = set(risky_in(n)) - set(risky_in(o))
        if newperms:
            add("high", "NEW risky permissions for %s: %s" % (name, ",".join(sorted(newperms))))
        if o.get("userState") != n.get("userState"):
            add("medium", "%s state: %s -> %s" % (name, o.get("userState"), n.get("userState")))
        if o.get("installer") != n.get("installer") and n.get("installer"):
            add("medium", "%s installer changed: %s -> %s" % (name, o.get("installer"), n.get("installer")))
    return findings


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def report(snap):
    p, s = snap["props"], snap["settings"]
    L = []
    A = L.append
    A("# Android hardening report")
    A("")
    A("Device: %s (%s)" % (p.get("ro.product.model"), p.get("ro.product.brand")))
    A("")
    A("| Fact | Value | Rating |")
    A("|---|---|---|")
    patch = p.get("ro.build.version.security_patch") or "?"
    A("| Android / SDK | %s / %s | |" % (p.get("ro.build.version.release"), p.get("ro.build.version.sdk")))
    A("| Security patch | %s | %s |" % (patch, patch_age_note(patch)))
    A("| Build type / tags | %s / %s | %s |" % (p.get("ro.build.type"), p.get("ro.build.tags"),
      "ok" if p.get("ro.build.type") == "user" else "CHECK: not a user build"))
    A("| Verified boot | %s | %s |" % (p.get("ro.boot.verifiedbootstate"), "ok" if p.get("ro.boot.verifiedbootstate") == "green" else "CHECK"))
    A("| Bootloader locked | %s | %s |" % (p.get("ro.boot.flash.locked"), "ok" if p.get("ro.boot.flash.locked") == "1" else "CHECK"))
    A("| Encryption | %s | %s |" % (p.get("ro.crypto.state"), "ok" if p.get("ro.crypto.state") == "encrypted" else "CHECK"))
    A("| ro.debuggable / ro.secure | %s / %s | %s |" % (p.get("ro.debuggable"), p.get("ro.secure"),
      "ok" if (p.get("ro.debuggable") == "0" and p.get("ro.secure") == "1") else "CHECK: root/debug risk"))
    A("")
    A("## Settings")
    A("")
    for scope, keys in (("global", SETTINGS_GLOBAL), ("secure", SETTINGS_SECURE)):
        for k in keys:
            v = s.get(scope, {}).get(k)
            if v is not None:
                A("- %s/%s = %s" % (scope, k, v))
    A("")
    A("## Special access")
    A("")
    A("- Device admin active: %s" % snap["special"]["deviceAdminActive"])
    for l in snap["special"]["listeners"]:
        A("- Listener: %s" % l["addr"])
    A("- /data/local/tmp: %s" % (", ".join(snap["special"]["localTmp"]) or "-"))
    A("")
    A("## Third-party apps (%d) with risky permissions" % len([n for n in snap["packages"] if not is_system(n)]))
    A("")
    A("| App | Version | Installer | Risky permissions |")
    A("|---|---|---|---|")
    for name in sorted(snap["packages"]):
        if is_system(name):
            continue
        d = snap["packages"][name]
        A("| %s | %s | %s | %s |" % (name, d.get("versionName"), d.get("installer") or "-", ", ".join(risky_in(d)) or "-"))
    return "\n".join(L) + "\n"


def patch_age_note(patch):
    try:
        y, m, d = (int(x) for x in patch.split("-"))
    except Exception:
        return "?"
    months = (time.gmtime().tm_year - y) * 12 + (time.gmtime().tm_mon - m)
    if months <= 1:
        return "current"
    return "OUTDATED (%d months)" % months


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def cmd_snapshot(args):
    snap, err = collect()
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    out = DEFAULT_BASELINE
    if "--out" in args:
        out = args[args.index("--out") + 1]
    save(out, snap)
    print("Snapshot written: %s (%d packages)" % (out, len(snap["packages"])))
    return 0


def cmd_diff(args):
    new, err = collect()
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    path = DEFAULT_BASELINE
    if "--baseline" in args:
        path = args[args.index("--baseline") + 1]
    if not os.path.exists(path):
        print("ERROR: no baseline at %s" % path, file=sys.stderr)
        return 2
    findings = diff(load(path), new)
    order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    findings.sort(key=lambda f: order.get(f[0], 9))
    if not findings:
        print("No deviations from the baseline.")
        return 0
    for sev, text in findings:
        print("[%s] %s" % (sev.upper(), text))
    return 10


def cmd_report(args):
    snap, err = collect()
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    print(report(snap))
    return 0


def cmd_watch(args):
    """Cron watchdog: stdout only on real changes (-> notification);
    offline state is reported rate-limited."""
    new, err = collect()
    st = load(STATE_FILE) if os.path.exists(STATE_FILE) else {"offline_runs": 0, "last_offline_alert": 0}
    if err:
        st["offline_runs"] = st.get("offline_runs", 0) + 1
        save(STATE_FILE, st)
        n = st["offline_runs"]
        if n == 3 or (n > 3 and (n - 3) % 24 == 0):
            print("Android monitor: device not reachable via adb for %d runs." % n)
        return 0
    if st.get("offline_runs"):
        st["offline_runs"] = 0
        save(STATE_FILE, st)
    path = DEFAULT_BASELINE
    if "--baseline" in args:
        path = args[args.index("--baseline") + 1]
    if not os.path.exists(path):
        print("Android monitor: no baseline (%s) -- take a snapshot first." % path)
        return 0
    findings = diff(load(path), new)
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    findings.sort(key=lambda f: sev_rank.get(f[0], 9))
    loud = [f for f in findings if f[0] in ("critical", "high")]
    quiet = [f for f in findings if f[0] not in ("critical", "high")]
    if not loud:
        # Cosmetic changes only (updates/uninstalls): silently roll the baseline
        # forward, no alarm -- otherwise the watchdog would fire every day.
        if quiet:
            save(path, new)
        return 0
    print("Android hardening watchdog: %d relevant change(s) on the device" % len(loud))
    print("")
    for sev, text in loud:
        print("[%s] %s" % (sev.upper(), text))
    if quiet:
        print("")
        print("(-- %d non-critical changes: %s)" % (len(quiet), "; ".join(t for _, t in quiet[:6])))
    print("")
    print("Baseline NOT rolled forward automatically -- review the change, then: android_audit.py snapshot")
    return 0


def norm_peer(addr):
    """Normalise a peer address: IPv4 in full, IPv6 truncated to /64 (address rotation)."""
    host, _, port = addr.rpartition(":")
    host = host.strip("[]")
    if ":" in host:
        groups = host.split(":")[:4]
        return ":".join(groups) + "::/64:" + port
    return host + ":" + port


def whois_label(addr):
    """Optional: who operates a peer? Only on request (--resolve); the cron run
    deliberately stays offline. Uses the local whois client."""
    host = addr.rsplit(":", 1)[0].strip("[]")
    if ":" in host:
        query = ":".join(host.split(":")[:3]) + "::"
    else:
        query = ".".join(host.split(".")[:3]) + ".0"
    whois = shutil.which("whois")
    if not whois:
        return None
    try:
        p = subprocess.run([whois, query], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    pref = {"org-name": 0, "orgname": 0, "netname": 1, "descr": 2}
    best = None
    for line in p.stdout.splitlines():
        key = line.split(":", 1)[0].strip().lower()
        if key in pref and ":" in line:
            val = line.split(":", 1)[1].strip()
            if best is None or pref[key] < best[0]:
                best = (pref[key], val)
    return best[1] if best else None


def cmd_deep(args):
    """Weekly deep check: established connections, foreground services,
    SELinux denials + suspicious log lines. Read-only."""
    serial = pick_serial()
    if not serial:
        print("ERROR: no device reachable via adb", file=sys.stderr)
        return 2
    print("# Android deep check (read-only) -- %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print()
    print("## Established TCP connections")
    peers = {}
    for l in sh(serial, "ss -tn state established").splitlines():
        if "Local" in l or "Cannot" in l:
            continue
        toks = [t for t in l.split() if ":" in t]
        if len(toks) >= 2:
            key = toks[0] + " -> " + toks[1]
            peers[key] = peers.get(key, 0) + 1
    if peers:
        for c in sorted(peers):
            print("- %s (%dx)" % (c, peers[c]))
    else:
        print("- (none visible)")
    if peers:
        kp_path = os.path.join(BASE_DIR, "known_peers.json")
        known = load(kp_path) if os.path.exists(kp_path) else {}
        new, cur = [], {}
        for c in peers:
            norm = norm_peer(c.split(" -> ")[1])
            cur[norm] = True
            if norm not in known:
                new.append(c)
        known.update(cur)
        save(kp_path, known)
        print()
        print("## Peers never seen before: %d" % len(new))
        if "--resolve" in args:
            cache = {}
            for c in sorted(new):
                peer = c.split(" -> ")[1]
                if peer not in cache:
                    cache[peer] = whois_label(peer) or "whois: no match"
                print("- %s  [%s]" % (c, cache[peer]))
        else:
            for c in sorted(new):
                print("- %s" % c)
            if new:
                print("  (resolve operators with 'deep --resolve' - uses whois; the cron run does not)")
    print()
    print("## Running foreground services")
    svc = sh(serial, "dumpsys activity services", timeout=60)
    fg = []
    for blk in svc.split("ServiceRecord{")[1:]:
        if "isForeground=true" not in blk:
            continue
        m = re.search(r"\s([\w.]+)/([\w.$]+)\s", blk[:300])
        if m:
            fg.append("%s/%s" % (m.group(1), m.group(2).lstrip(".")))
    for x in sorted(set(fg)) or ["(none)"]:
        print("- %s" % x)
    print()
    print("## SELinux denials / suspicious log lines (last 4000 logcat lines)")
    log = sh(serial, "logcat -d -t 4000", timeout=90)
    denied = [l for l in log.splitlines() if "avc: denied" in l]
    susp = [l for l in log.splitlines() if re.search(r"(?i)(malware|trojan|exploit|rooted device|signature mismatch|INSTALL_FAILED_VERIFICATION|dropped.*permission)", l)]
    print("- SELinux 'avc: denied': %d lines" % len(denied))
    for l in denied[:5]:
        print("    %s" % l.strip()[:200])
    print("- suspicious hits: %d" % len(susp))
    for l in susp[:10]:
        print("    %s" % l.strip()[:200])
    return 0


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 1
    cmd, rest = args[0], args[1:]
    return {
        "snapshot": cmd_snapshot,
        "diff": cmd_diff,
        "report": cmd_report,
        "watch": cmd_watch,
        "deep": cmd_deep,
    }.get(cmd, lambda a: (print("unknown command: %s" % cmd), 1)[1])(rest)


if __name__ == "__main__":
    sys.exit(main())
