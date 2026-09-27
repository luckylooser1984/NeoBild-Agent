#!/usr/bin/env bash
# compress_and_archive.sh — verified lossless compression of large, dormant files.
#
# Principle: big files that are no longer written to get compressed with xz,
# the archive is VERIFIED against the original (decompress + hash + size
# compare), and only after a successful verification is the original removed.
# The archive plus a small receipt file end up in one tidy folder, ready for
# offline or cloud backup. On any error everything stays as it was.
#
# Usage:
#   compress_and_archive.sh --check <file>             # analyse only, change nothing
#   compress_and_archive.sh --pack  <file> [category]  # compress + verify + replace
#
# Categories (subfolders of the archive dir): logs (default), db, models,
# media, archive, misc — or anything you like.
#
# Environment:
#   ARCHIVE_DIR   target base directory (default: ~/Archive-packed)
#   XZ_LEVEL      xz compression level (default: -6)
#
# Safety limits: no rm -rf, no sudo, refuses files held open by a process,
# replaces the original only with --pack and only after byte-identical
# round-trip verification.
#
# Author: Lukas Weißmann
# License: MIT

set -euo pipefail

ARCHIVE_BASE="${ARCHIVE_DIR:-${HOME}/Archive-packed}"
LEVEL="${XZ_LEVEL:--6}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
gb() { python3 -c "import sys; print('%.2f GB' % (int(sys.argv[1])/1073741824))" "$1"; }

# ---- arguments ---------------------------------------------------------------
MODE="${1:-}"
FILE="${2:-}"
CAT="${3:-logs}"

[ -n "$MODE" ] && [ -n "$FILE" ] || die "usage: $0 --check|--pack <file> [category]"
[ -f "$FILE" ] || die "file not found: $FILE"
case "$CAT" in */*|..|.) die "invalid category: $CAT" ;; esac

SIZE=$(stat -c %s "$FILE")
MTIME_EPOCH=$(stat -c %Y "$FILE")
MTIME_DAYS=$(python3 -c "import time,sys; print('%.1f' % ((time.time()-int(sys.argv[1]))/86400))" "$MTIME_EPOCH")
BASENAME=$(basename "$FILE")

# ---- analysis ----------------------------------------------------------------
blocked=0
[ "$SIZE" -lt 52428800 ] && { info "NOTE: file < 50 MB ($SIZE bytes) — compressing gains little."; }

# is a process holding the file open?
OPEN_BY=""
if command -v lsof >/dev/null 2>&1; then
  OPEN_BY=$(lsof -nP -- "$FILE" 2>/dev/null | awk 'NR>1{print $1" (PID "$2")"}' | sort -u || true)
fi

info "File:          $FILE"
info "Size:          $(gb "$SIZE") ($SIZE bytes)"
info "Last modified: $MTIME_DAYS days ago"
if [ -n "$OPEN_BY" ]; then
  info "OPEN BY:       $OPEN_BY"
  blocked=1
else
  info "OPEN BY:       nobody"
fi

if [ "$MODE" = "--check" ]; then
  [ "$blocked" = "1" ] && info "" && info "WARNING: file is still open — do NOT pack, stop the service first."
  info ""
  info "Target folder would be: $ARCHIVE_BASE/$CAT/"
  exit 0
fi
[ "$MODE" = "--pack" ] || die "unknown mode: $MODE (use --check or --pack)"

# ---- pack --------------------------------------------------------------------
[ "$blocked" = "1" ] && die "file is held open by a running process ($OPEN_BY) — stop it first."

TARGET="${ARCHIVE_BASE}/${CAT}"
mkdir -p "$TARGET"

STAMP=$(date +%Y%m%d-%H%M%S)
PACKPATH="${TARGET}/${BASENAME}.xz"
VERIFY_PATH="${TARGET}/.tmp-verify-${STAMP}.out"

# Name collision: append timestamp, never overwrite
if [ -e "$PACKPATH" ]; then
  PACKPATH="${TARGET}/${BASENAME}.${STAMP}.xz"
fi

info ""
info "== Pack =="
info "Source:  $FILE"
info "Target:  $PACKPATH"
info "Method:  xz ${LEVEL}"

info "Hashing original..."
SHA_ORIG=$(sha256sum "$FILE" | awk '{print $1}')
info "Original sha256: $SHA_ORIG"

info "Compressing (may take a while)..."
T0=$(date +%s)
xz "$LEVEL" -c "$FILE" > "$PACKPATH"
T1=$(date +%s)
PACKSIZE=$(stat -c %s "$PACKPATH")
info "Done in $((T1-T0))s. Packed size: $(gb "$PACKSIZE")"

# ---- verification: decompress and compare hash -------------------------------
info ""
info "== Verify (decompress + hash compare) =="
xz -dc "$PACKPATH" > "$VERIFY_PATH"

SHA_BACK=$(sha256sum "$VERIFY_PATH" | awk '{print $1}')
info "Round-trip sha256: $SHA_BACK"

if [ "$SHA_ORIG" != "$SHA_BACK" ]; then
  rm -f "$VERIFY_PATH" "$PACKPATH"
  die "VERIFICATION FAILED — original left untouched. Nothing changed."
fi
BACKSIZE=$(stat -c %s "$VERIFY_PATH")
[ "$BACKSIZE" = "$SIZE" ] || { rm -f "$VERIFY_PATH" "$PACKPATH"; die "size differs ($BACKSIZE vs $SIZE) — aborted."; }
rm -f "$VERIFY_PATH"
info "OK: byte-identical. Lossless confirmed."

# ---- receipt + replace original ---------------------------------------------
# No second full copy is kept (that would waste the space we want to free).
# Instead a receipt with hash + origin keeps the operation traceable.
cat > "${PACKPATH}.receipt.txt" <<EOF
Source:         $FILE
Packed at:      $(date '+%Y-%m-%d %H:%M:%S %z')
Method:         xz $LEVEL
Original bytes: $SIZE
Packed bytes:   $PACKSIZE
Original sha256: $SHA_ORIG
Verification:   OK (round trip byte-identical)
Source last modified: $(date -d @"$MTIME_EPOCH" '+%Y-%m-%d %H:%M:%S')
EOF

info ""
info "== Replace original =="
info "Removing original (content preserved as .xz in the archive)..."
rm -f -- "$FILE"

info ""
info "== Result =="
info "Archive: $PACKPATH"
info "Receipt: ${PACKPATH}.receipt.txt"
info "Freed:   $(gb $((SIZE-PACKSIZE)))"
df -h "$(dirname "$FILE")" | tail -1
