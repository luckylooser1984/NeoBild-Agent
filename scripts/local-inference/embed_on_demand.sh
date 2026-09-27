#!/usr/bin/env bash
# embed_on_demand.sh — start an embedding server only when it is needed.
#
# Usage:  embed_on_demand.sh start   -> start the service if it is not active,
#                                       wait until its /health answers
#         embed_on_demand.sh stop    -> stop the service (free the RAM)
#
# Background: an always-on embedding server (llama-server in embedding mode)
# held ~330 MB RAM on a memory-tight machine, while real usage was extremely
# bursty — about 11,000 requests on a single day during a full knowledge-base
# rebuild, then days or weeks of zero. A cold start takes only ~0.3-0.5 s, so
# keeping it running permanently is not worth it. Callers run `start` before
# embedding and the service can be stopped again afterwards.
#
# Environment (optional):
#   EMBED_SERVICE  systemd --user unit name (default llama-embed.service)
#   EMBED_PORT     port the unit listens on (default 8081)
#
# Author: Lukas Weißmann
# License: MIT
set -uo pipefail

SVC="${EMBED_SERVICE:-llama-embed.service}"
PORT="${EMBED_PORT:-8081}"
case "${1:-start}" in
  start)
    if systemctl --user is-active --quiet "$SVC"; then
      exit 0          # already running
    fi
    systemctl --user start "$SVC" || exit 1
    # Wait until the port answers (cold start ~0.3-0.6 s)
    for _ in $(seq 1 40); do
      if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/health"; then
        exit 0
      fi
      sleep 0.25
    done
    echo "WARN: $SVC started, but port $PORT does not answer" >&2
    exit 1
    ;;
  stop)
    systemctl --user stop "$SVC" 2>/dev/null
    exit 0
    ;;
  *)
    echo "Usage: $0 {start|stop}" >&2
    exit 2
    ;;
esac
