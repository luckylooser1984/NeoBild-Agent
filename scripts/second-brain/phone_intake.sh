#!/usr/bin/env bash
# phone_intake.sh — read-only note intake from an Android phone via adb.
#
# Purpose:
#   Copies new text files from one or more folders on the phone (e.g. the folder
#   of a Markdown notes app) into a local inbox, recognises already-seen files by
#   SHA-256 and converts MHTML page captures into readable Markdown via
#   mhtml_to_markdown.py. Every intake is logged in manifest.tsv.
#   NOTHING on the phone is changed or deleted - it only reads (adb pull).
#   Tip: run phone_idle_check.sh (android/) first so you don't disturb the user.
#
# Usage:
#   phone_intake.sh
# Environment:
#   INTAKE_SOURCES="/sdcard/Documents/markor /sdcard/Download"  source folders (space-separated)
#   INTAKE_INBOX=/path    local inbox           (default: ./phone-inbox)
#   INTAKE_NOTES=/path    converted notes       (default: ./notes-converted)
#   ANDROID_SERIAL=...    pick a device when several are connected
#
# Pitfalls learned the hard way:
#  * `adb shell find /sdcard ...` silently returns nothing on some devices (FUSE) -> use `ls -1`.
#  * `read` in a loop would read the script's stdin -> feed the list via here-string.
#  * adb commands inside the loop swallow stdin -> always `< /dev/null`.
#  * sha256sum on the phone fails for some permissions -> hash locally.
#
# Author: Lukas Weißmann
# License: MIT

set -u
case "${1:-}" in -h|--help) sed -n '2,24p' "$0"; exit 0;; esac

ADB="${ANDROID_ADB:-$(command -v adb || echo adb)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SOURCES_STR="${INTAKE_SOURCES:-/sdcard/Documents/markor /sdcard/Download}"
INBOX="${INTAKE_INBOX:-$PWD/phone-inbox}"
NOTES="${INTAKE_NOTES:-$PWD/notes-converted}"
PY="$(command -v python3 || echo python3)"

mkdir -p "$INBOX" "$NOTES"
HASHES="$INBOX/.hashes"
MANIFEST="$INBOX/manifest.tsv"
[ -f "$HASHES" ] || : > "$HASHES"
[ -f "$MANIFEST" ] || printf "time\tsource\tlocal\tbytes\tsha256\tconverted\n" > "$MANIFEST"

is_text() {  # $1 = path on the phone
  case "$1" in
    *.apk|*.pdf|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.zip|*.gz|*.xz|*.mp3|*.mp4|*.mkv|*.ogg|\
    *.properties|*.xml|*.db|*.kdbx|*.key|*.so|*.dex|*.ttf|*.otf) return 1;;
    *) return 0;;
  esac
}

fetched=0
converted=0
for d in $SOURCES_STR; do
  listing=$("$ADB" shell "ls -1 '$d' 2>/dev/null" < /dev/null | tr -d '\r')
  [ -z "$listing" ] && continue
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    remote="$d/$name"
    is_text "$remote" || continue
    tmp=$(mktemp "${TMPDIR:-/tmp}/intake.XXXXXX")
    if ! "$ADB" pull "$remote" "$tmp" >/dev/null 2>&1 < /dev/null; then
      rm -f "$tmp"; continue
    fi
    hash=$(sha256sum "$tmp" | awk '{print $1}')
    if grep -q "^$hash$" "$HASHES" 2>/dev/null; then
      rm -f "$tmp"; continue   # already fetched
    fi
    safe=$(echo "$name" | tr '/ ' '__')
    mv "$tmp" "$INBOX/$safe"
    echo "$hash" >> "$HASHES"
    # MHTML page capture? -> produce readable text
    conv="-"
    if grep -q -m1 -a "Saved by Blink\|MultipartBoundary" "$INBOX/$safe" 2>/dev/null; then
      target="$NOTES/$(date +%Y-%m-%d)-$(echo "${safe%.md}" | tr 'A-Z ' 'a-z_' | cut -c1-60).md"
      if "$PY" "$HERE/mhtml_to_markdown.py" "$INBOX/$safe" "$target" \
           "$name" "fetched from phone $(date +%Y-%m-%d\ %H:%M) from $d" >/dev/null 2>&1; then
        conv="$target"; converted=$((converted+1))
      else
        conv="conversion failed"
      fi
    fi
    bytes=$(stat -c%s "$INBOX/$safe")
    printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$(date +%Y-%m-%dT%H:%M:%S)" "$remote" "$safe" "$bytes" "$hash" "$conv" >> "$MANIFEST"
    echo "NEW: $remote  ($bytes bytes)$([ "$conv" != "-" ] && echo "  -> $conv")"
    fetched=$((fetched+1))
  done <<< "$listing"
done

echo "---"
echo "newly fetched: $fetched   converted: $converted"
echo "Inbox: $INBOX"
ls -1 "$INBOX" | grep -v '^\.hashes$\|^manifest.tsv$' | sed 's/^/  /'
