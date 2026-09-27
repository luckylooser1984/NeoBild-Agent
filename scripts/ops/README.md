# ops/

Small operational watchdogs and tools for a single self-hosted machine that
runs local AI agents. Most of them follow the same **watchdog convention**:
*empty stdout = nothing new*, output only on a state change — so a scheduler
can forward stdout as a notification without spamming.

| Script | What it does | Needs |
|---|---|---|
| `zram_watch.py` | Warns when compressed zram swap occupies too much RAM (threshold ladder, state-change only). | Linux with zram |
| `cron_failure_watchdog.py` | Surfaces failing / overdue / paused jobs from a scheduler's JSON job store (e.g. Hermes Agent cron). No LLM, works offline. | a jobs JSON file |
| `change_review.py` | Local review of new git commits: syntax, secrets, dangerous calls, dead paths, TODOs, diff size, missing docs. Never writes to the repo, masks secret values. | git, optional pytest |
| `cve_daily.py` | Lists CVEs published in the last 24h from the public NVD API. | network |
| `cve_watch.py` | Filters `cve_daily.py` down to your stack (keyword list), drops noise (WordPress plugins, kernel flood), prints only unseen CVEs. | network |
| `rss_digest.py` | Last-N-days digest of RSS/Atom feeds as Markdown (privacy/Linux defaults). | network |
| `pcap_extract.py` | Extracts DNS names, TLS SNI, certificate strings, connections and protocol stats from packet captures — memory-friendly (tshark two-pass). | tshark, capinfos |
| `compress_and_archive.sh` | Compresses a large dormant file with xz, verifies the round trip byte-for-byte (sha256 + size), writes a receipt, and only then removes the original. | xz, python3 |
| `deploy_drift_check.sh` | "Committed != live" check for a static website: HTTP status, undeployed commits, unexpected ETag changes. | curl, git |
| `system_inventory.py` | Read-only hardware/software/security inventory as JSON: fingerprint sensor, listening ports (flags known P2P ports), services, firewall, secret-looking file *names*. | Linux; pacman optional |

All paths are CLI arguments or environment variables; state files default to
`~/.local/state/`.
