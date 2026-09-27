#!/data/data/com.termux/files/usr/bin/bash
# termux_background_job.sh — run a long-lived Python job in Termux, CPU-throttled and protected from Android's process killers.
#
# Purpose:
#   Running background work on a phone is different from Linux:
#     * No systemd, no cgroups -> no CPUQuota. The hard limit comes from cpulimit,
#       the priority from nice (often ineffective on Android).
#     * Android freezes/kills background processes. termux-wake-lock is required,
#       but it is NOT a CPU brake.
#     * The "phantom process killer" (Android 12+) kills child processes via
#       max_phantom_processes; this can only be relaxed via adb from a PC, not
#       from inside Termux (commands are printed below).
#
# Usage:
#   termux_background_job.sh <script.py> [cpu-limit-percent=35] [cpus=0-3] [-- script args...]
#   Logs go to $JOB_DIR/logs/termux.log (JOB_DIR default: ~/.bgjob).
#
# Author: Lukas Weißmann
# License: MIT

set -u
case "${1:-}" in ""|-h|--help) sed -n '2,17p' "$0"; exit 0;; esac

SCRIPT="$1"; shift
LIMIT="${1:-35}"; [ $# -gt 0 ] && shift
CPUS="${1:-0-3}"; [ $# -gt 0 ] && shift
[ "${1:-}" = "--" ] && shift
BASE="${JOB_DIR:-$HOME/.bgjob}"

echo "[1/6] Ensure packages"
# cpulimit is the only real CPU limit without root on Android.
# termux-api provides termux-wake-lock.
pkg install -y cpulimit termux-api python coreutils 2>&1 | tail -3

echo "[2/6] Acquire wake lock (prevents Doze freeze)"
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock && echo "  wake lock active (until termux-wake-unlock)"
else
  echo "  WARNING: termux-wake-lock missing (install the Termux:API app!)"
fi

echo "[3/6] Doze/battery optimisation: Termux must be 'Unrestricted'"
echo "  Manually: Settings > Apps > Termux > Battery > Unrestricted"

echo "[4/6] Relax the phantom process killer (needs adb from a PC)"
cat <<'EOF'
  Run from a PC over USB (NOT possible inside Termux):
    adb shell settings put global settings_enable_monitor_phantom_procs false
    adb shell device_config put activity_manager max_phantom_processes 2147483647
    adb shell device_config set_sync_disabled_for_tests persistent
  Undo:
    adb shell device_config set_sync_disabled_for_tests none
    adb shell settings put global settings_enable_monitor_phantom_procs true
EOF

echo "[5/6] Create job directory"
mkdir -p "$BASE/logs"

echo "[6/6] Start (limit ${LIMIT}% CPU, cores ${CPUS})"
if ! command -v cpulimit >/dev/null 2>&1; then
  echo "  ERROR: cpulimit missing -> 'pkg install cpulimit'"
  exit 1
fi

# Background: first taskset (core choice), then nice, then cpulimit as hard limit.
# cpulimit alternates SIGSTOP/SIGCONT -> safe alongside other running agents.
nohup taskset -c "$CPUS" \
      nice -n 19 \
      cpulimit --limit="$LIMIT" --background \
      -- python3 "$SCRIPT" "$@" \
      >> "$BASE/logs/termux.log" 2>&1 &

echo "  started (PID $!). Log: $BASE/logs/termux.log"
echo "  Stop: pkill -f $(basename "$SCRIPT") ; termux-wake-unlock"
