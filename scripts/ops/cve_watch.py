#!/usr/bin/env python3
"""cve_watch.py — daily CVE briefing as a plain script instead of an LLM turn.

Runs cve_daily.py (NVD, last 24h), filters the result down to *your* stack
(keyword list, overridable) plus CVSS >= 7.0, de-duplicates against a state
file and prints ONLY new hits. Empty output = silent (watchdog convention for
cron jobs whose stdout is forwarded as a notification).

Noise control built in:
  - WordPress plugin/theme advisories are dropped (they dominate the critical
    hits and are rarely relevant for a static-site setup)
  - upstream Linux kernel CVEs (hundreds per day) are only counted; they are
    listed individually only from CVSS 9.0 — a rolling-release distro ships
    the fixes anyway

Usage:
    python3 cve_watch.py
    python3 cve_watch.py --keywords my_stack.txt     # one keyword per line
Environment:
    CVE_WATCH_STATE   state file (default ~/.local/state/cve_watch_seen.json)

Exit: always 0; findings are reported via stdout content, not the exit code
(otherwise every finding would look like a failed run in the scheduler).

Author: Lukas Weißmann
License: MIT
"""
import argparse, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPT = os.path.join(HERE, "cve_daily.py")
STATE = os.environ.get(
    "CVE_WATCH_STATE",
    os.path.join(os.path.expanduser("~"), ".local", "state", "cve_watch_seen.json"),
)
MAX_LINES = 20

# Default stack: a Linux workstation running local AI tooling.
STACK = ("arch linux", "glibc", "systemd", "util-linux", "openssl", "openssh", "sudo", "polkit",
         "pkexec", "qemu", "kvm", "libvirt", "docker", "containerd", "podman", "python", "flask",
         "fastapi", "django", "golang", "go stdlib", "rust", "cargo", "hugo", "llama.cpp", "ggml",
         "node.js", "npm", "chromium", "firefox", "kde", "plasma", "kwin", "wayland", "xorg",
         "tor", "monero", "sqlite", "mariadb", "mysql", "nginx", "wireshark", "nmap", "android",
         "adb", "curl", "libcurl", "git", "zstd", "xz", "ffmpeg", "imagemagick", "vulkan", "mesa",
         "pipewire", "wordpress")
RE_WPNOISE = re.compile(r"wordpress (plugin|theme)", re.I)
RE_KERNEL = re.compile(r"in the linux kernel", re.I)
CRITICAL = 9.0


def build_stack_re(keywords):
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in keywords) + r")(?![a-z0-9])", re.I)


def load_seen():
    try:
        with open(STATE) as fh:
            d = json.load(fh)
        return set(d.get("ids", [])), d.get("ts", 0)
    except (OSError, json.JSONDecodeError):
        return set(), 0


def main():
    ap = argparse.ArgumentParser(description="Filtered, de-duplicated daily CVE briefing.")
    ap.add_argument("--keywords", help="file with one stack keyword per line (replaces the default list)")
    args = ap.parse_args()

    keywords = STACK
    if args.keywords:
        with open(args.keywords, encoding="utf-8") as fh:
            keywords = tuple(l.strip().lower() for l in fh if l.strip() and not l.startswith("#"))
    re_stack = build_stack_re(keywords)

    if not os.path.exists(SCRIPT):
        print("cve_watch: %s missing" % SCRIPT, file=sys.stderr)
        return 0
    try:
        r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, timeout=180)
    except subprocess.SubprocessError as e:
        print("cve_watch: cve_daily.py not runnable: %s" % e, file=sys.stderr)
        return 0
    if r.returncode != 0 or not r.stdout.strip():
        print("cve_watch: no NVD response (rc=%s) — staying silent" % r.returncode, file=sys.stderr)
        return 0

    seen, _ = load_seen()
    hits, new_ids = [], []
    kernel_count = 0
    for line in r.stdout.splitlines():
        if "|" not in line or not line.startswith("CVE-"):
            continue
        cid = line.split("|")[0].strip()
        m = re.search(r"CVSS\s+([0-9.]+)", line)
        score = float(m.group(1)) if m else 0.0
        low = line.lower()
        is_kernel = bool(RE_KERNEL.search(low))
        stack_hit = bool(re_stack.search(low)) and not RE_WPNOISE.search(low)
        # Individual listing only for: critical CVSS OR stack hit (minus the kernel flood).
        # Kernel CVEs go into the counter.
        if is_kernel and not stack_hit:
            kernel_count += 1
            if score < CRITICAL:
                continue
        elif not stack_hit and (score < CRITICAL or RE_WPNOISE.search(low)):
            continue
        elif not (score >= 7.0 or score == 0.0 or score >= CRITICAL):
            continue
        if cid in seen:
            continue
        new_ids.append(cid)
        hits.append((score, line.strip()))

    # Persist state (even if nothing is reported -> no repeated noise)
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump({"ts": int(time.time()), "ids": sorted(seen | set(new_ids))[-4000:]}, fh)
    except OSError:
        pass

    if not hits:
        return 0
    hits.sort(key=lambda x: -x[0])
    print("CVE watch %s — %d relevant advisory(ies); %d kernel CVE(s) today not listed individually:"
          % (time.strftime("%Y-%m-%d"), len(hits), kernel_count))
    for score, line in hits[:MAX_LINES]:
        print("  " + line[:220])
    if len(hits) > MAX_LINES:
        print("  ... %d more (full run: python3 %s)" % (len(hits) - MAX_LINES, SCRIPT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
