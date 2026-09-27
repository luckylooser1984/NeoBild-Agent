# telemetry/

A small, dependency-free telemetry family for a single Linux machine that runs
local AI workloads on limited hardware (CPU-only laptop, 16 GB RAM).

| Script | What it does |
|---|---|
| `telemetry.py` | Samples power, thermals, CPU, memory, disk, network and systemd state once per run and appends a JSONL line. Computes rates (swap, disk, throttling, battery drain) from the previous tick. |
| `governor.py` | Asked **before every work package** of a long job: returns `FULL`, `GENTLE` or `STOP` (with batch size and sleep time). Usable as a library or CLI (exit code 0/1/2). |
| `hardware_collector.py` | Lower-frequency hardware health snapshot: user-journal errors since the last run, lm-sensors / thermal zones, RAPL energy counters, battery (upower), network interfaces. One JSONL line per run; optional screenshot only when something looks abnormal. Output: `$HARDWARE_COLLECTOR_DIR`, default `~/.local/state/hardware-collector`. |
| `anomaly.py` | Reads the JSONL history and reports anomalies using robust statistics (median + MAD, bucketed by weekday/time of day) plus hard rules. Separates facts, observations and (explicitly uncertain) hypotheses. `--watchdog` prints only *new* findings, so it is quiet in cron. |

Design principles: stdlib only, read-only on the system, never crash, no LLM
in the loop, deterministic output.

```bash
# every minute, e.g. via a systemd user timer
python3 telemetry.py
# inside a long-running batch job
python3 -c 'from governor import Governor; print(Governor().decide())'
# daily report
python3 anomaly.py --hours 24
```

Data directory: `$TELEMETRY_DIR`, default `~/.local/state/telemetry`.
Thresholds in `governor.py` / `anomaly.py` were calibrated on one laptop —
adjust them for your hardware.
