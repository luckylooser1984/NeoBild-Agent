#!/usr/bin/env bash
# deploy_drift_check.sh — "committed != live" watchdog for a statically deployed website.
#
# Reports ONLY on state changes:
#   - live site not answering HTTP 200
#   - newest website commit is newer than the live Last-Modified header
#     (committed but not deployed)
#   - live ETag changed although you did not deploy (unexpected live change)
# Silent when everything is fine — made for a cron job whose stdout is
# forwarded as a notification (empty stdout = no message).
#
# Configuration (environment):
#   LIVE_URL     site to check                 (required, e.g. https://example.org)
#   SITE_REPO    local git repo of the site    (required)
#   SITE_SUBDIR  path inside the repo that holds the site (default: .)
#   STATE_FILE   state file (default: ~/.local/state/deploy_drift_check.state)
#
# Usage:
#   LIVE_URL=https://example.org SITE_REPO=~/code/site ./deploy_drift_check.sh
#
# Read-only apart from its own state file. Needs curl and git.
#
# Author: Lukas Weißmann
# License: MIT
set -u

: "${LIVE_URL:?set LIVE_URL, e.g. https://example.org}"
: "${SITE_REPO:?set SITE_REPO to the local git repo of the site}"
SITE_SUBDIR="${SITE_SUBDIR:-.}"
STATE_FILE="${STATE_FILE:-$HOME/.local/state/deploy_drift_check.state}"
mkdir -p "$(dirname "$STATE_FILE")"

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT

# 1) HTTP status of the live site
status=$(curl -sI --max-time 20 -o "$headers" -w "%{http_code}" "$LIVE_URL" 2>/dev/null) || status="000"
live_modified=$(grep -i '^last-modified:' "$headers" 2>/dev/null | head -1 | tr -d '\r' | cut -d' ' -f2-)
live_etag=$(grep -i '^etag:' "$headers" 2>/dev/null | head -1 | tr -d '\r' | cut -d' ' -f2- | tr -cd 'A-Za-z0-9._:/+=-')

# 2) newest website commit in the repo (path filter)
last_commit=$(git -C "$SITE_REPO" log -1 --format='%cd|%h|%s' --date=format:'%Y-%m-%d %H:%M' -- "$SITE_SUBDIR" 2>/dev/null || echo "?|?|?")
commit_date=$(echo "$last_commit" | cut -d'|' -f1)
commit_hash=$(echo "$last_commit" | cut -d'|' -f2)
commit_msg=$(echo "$last_commit" | cut -d'|' -f3-)

# load state (parsed as key=value, never sourced — the ETag comes from the network)
prev_status=""; prev_etag=""; prev_commit=""
if [ -f "$STATE_FILE" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      prev_status) prev_status="$v" ;;
      prev_etag)   prev_etag="$v" ;;
      prev_commit) prev_commit="$v" ;;
    esac
  done < "$STATE_FILE"
fi

issues=""

# 3) live status
if [ "$status" != "200" ]; then
  issues+="LIVE STATUS: HTTP $status (expected 200) at $now\n"
fi

# 4) undeployed commits: newest site commit newer than live Last-Modified
if [ -n "$live_modified" ]; then
  lm=$(date -u -d "$live_modified" +%Y-%m-%d\ %H:%M 2>/dev/null || true)
  if [ -n "$lm" ] && [ -n "$commit_date" ] && [ "$commit_date" != "?" ] && [[ "$commit_date" > "$lm" ]]; then
    if [ -z "$prev_commit" ] || [ "$prev_commit" != "$commit_hash" ]; then
      issues+="NOT DEPLOYED: site commit $commit_hash ($commit_date) is NEWER than live (last-modified $lm). $commit_msg\n"
    fi
  fi
fi

# 5) unexpected live change (ETag changed without own deploy)
if [ -n "$prev_etag" ] && [ -n "$live_etag" ] && [ "$prev_etag" != "$live_etag" ]; then
  issues+="LIVE CHANGED: ETag $prev_etag -> $live_etag ($live_modified). If you did not deploy: check!\n"
fi

# save state before output, so nothing is reported twice
printf 'prev_status=%s\nprev_etag=%s\nprev_commit=%s\n' "$status" "$live_etag" "$commit_hash" > "$STATE_FILE"

if [ -n "$issues" ]; then
  printf "Deploy drift check %s (%s)\n" "$LIVE_URL" "$now"
  printf "%b" "$issues"
fi
exit 0
