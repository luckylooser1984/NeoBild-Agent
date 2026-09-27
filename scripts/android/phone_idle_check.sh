#!/usr/bin/env bash
# phone_idle_check.sh — is the phone currently free to be used by automation?
#
# Purpose:
#   Turns the rule "automation may use the phone (e.g. its on-device models)
#   as long as I'm not using it" into a measurable check.
#   Criterion: screen off (mWakefulness=Asleep) counts as free. Screen on,
#   locked or unlocked, counts as busy - when in doubt, hands off.
#   Read-only: only issues queries, changes nothing on the device.
#
# Output: a short verdict + details.
# Exit codes: 0 = free (IDLE), 1 = busy (BUSY), 2 = device not reachable.
#
# Usage:
#   phone_idle_check.sh            (ANDROID_SERIAL=... to pick a device)
#
# Author: Lukas Weißmann
# License: MIT

case "${1:-}" in -h|--help) sed -n '2,17p' "$0"; exit 0;; esac

ADB="${ANDROID_ADB:-adb}"
SERIAL="${ANDROID_SERIAL:-}"

if [ -n "$SERIAL" ]; then
  ADB_ARGS=(-s "$SERIAL")
else
  ADB_ARGS=()
fi

if ! "$ADB" "${ADB_ARGS[@]}" get-state >/dev/null 2>&1; then
  echo "PHONE: not reachable (no adb device) -> DO NOT USE"
  exit 2
fi

POWER=$("$ADB" "${ADB_ARGS[@]}" shell dumpsys power 2>/dev/null)
WAKE=$(printf '%s\n' "$POWER" | grep -m1 -o 'mWakefulness=[A-Za-z]*' | cut -d= -f2)
SCREEN=$(printf '%s\n' "$POWER" | grep -m1 -o 'mScreenOn=[a-z]*' | cut -d= -f2)
LASTWAKE=$(printf '%s\n' "$POWER" | grep -m1 -o 'mLastWakeTime=[0-9]*' | cut -d= -f2)
LOCKED=$("$ADB" "${ADB_ARGS[@]}" shell dumpsys deviceidle 2>/dev/null | grep -m1 -o 'mScreenLocked=[a-z]*' | cut -d= -f2)
FOCUS=$("$ADB" "${ADB_ARGS[@]}" shell dumpsys window 2>/dev/null | grep -m1 'mCurrentFocus' | sed 's/.*mCurrentFocus=//')

echo "State: wakefulness=${WAKE:-?} screen=${SCREEN:-?} locked=${LOCKED:-?}"
echo "Foreground: ${FOCUS:-unknown}"
[ -n "$LASTWAKE" ] && echo "Last wake (ms since boot): ${LASTWAKE}"

if [ "${WAKE:-}" = "Asleep" ] || [ "${SCREEN:-}" = "false" ]; then
  echo "VERDICT: IDLE - screen off, nobody is using the phone."
  exit 0
fi

echo "VERDICT: BUSY - screen on. Hands off, check again later."
exit 1
