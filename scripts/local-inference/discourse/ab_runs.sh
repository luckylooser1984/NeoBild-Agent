#!/usr/bin/env bash
# ab_runs.sh — produce discourse runs for an A/B comparison of prompt variants.
#
# Usage:  ab_runs.sh <variant> [<variant> ...]
#   Each <variant> refers to a roles file roles_<variant>.json next to this
#   script. All runs go into the same runs.jsonl; prompt_bench.py ab separates
#   them afterwards by the prompt_version hash stored in the log.
#
# Environment:
#   REPEATS            repetitions per task (default 2)
#   DISCOURSE_LOG_DIR  log directory (default ./discourse-logs)
#
# Author: Lukas Weißmann
# License: MIT
set -uo pipefail

DISC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPEATS="${REPEATS:-2}"
LOG_DIR="$(realpath -m "${DISCOURSE_LOG_DIR:-discourse-logs}")"

cd "$DISC" || exit 1

TASKS=(
  "ethics|medium|Draft: We introduce a weekly status email for all users. Every week a report with usage numbers is sent automatically. Users cannot opt out, because it keeps the system simpler."
  "architecture|high|Architecture draft: The token index will be stored in SQLite instead of JSONL. Rationale: faster queries. Migration happens in one step with no way back. All agents write directly into the same table, with a lock."
  "trivial|low|Draft: We rename the file README.md to INFO.md. Reason: the name is shorter and looks more modern."
  "security|high|Draft: The local agent may execute commands without confirmation as long as they do not start with rm. All other commands run directly. Rationale: confirmations cost time."
  "data|medium|Draft: We automatically delete all logs older than 7 days via cron. Rationale: saves space. No backup exists."
)

for VARIANT in "$@"; do
  ROLES="$DISC/roles_${VARIANT}.json"
  [ -f "$ROLES" ] || { echo "missing: $ROLES"; continue; }
  echo "=== variant $VARIANT (roles file $ROLES) ==="
  n=0
  for rep in $(seq 1 "$REPEATS"); do
    for entry in "${TASKS[@]}"; do
      IFS='|' read -r name level task <<< "$entry"
      n=$((n + 1))
      timeout 400 python3 discourse.py run \
        --task "$task" --iteration "$((2000 + n))" \
        --roles-file "$ROLES" --log-dir "$LOG_DIR" > /dev/null 2>&1
      echo "  [$VARIANT] rep=$rep $name rc=$?"
    done
  done
done
echo "DONE — compare with: python3 prompt_bench.py ab $LOG_DIR/runs.jsonl"
