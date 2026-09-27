#!/usr/bin/env python3
"""anomaly.py — deterministic anomaly detection on telemetry samples.

No model, no network, no LLM: robust statistics (median + MAD) plus hard
rules, applied to the JSONL files written by telemetry.py. Two detection
paths, deliberately separate:

  1. STATISTICAL — per metric, a robust z-score against the metric's own
     history, bucketed by (weekday/weekend, quarter of the day). Without
     buckets every night would be an outlier. MAD instead of standard
     deviation, because single spikes would otherwise inflate the spread
     and afterwards nothing would stand out anymore.
  2. RULES — hard thresholds and state changes that apply even without
     history (disk full, battery health dropping, failed units, reboot,
     throttle storm).

Output discipline:
  facts        = measured values
  observations = threshold/statistic exceeded (evidenced)
  hypotheses   = possible interpretation (explicitly uncertain)
A hypothesis is never presented as a finding.

Usage:
    python3 anomaly.py                     # report over the last 24h
    python3 anomaly.py --hours 168         # one week
    python3 anomaly.py --json              # machine-readable
    python3 anomaly.py --watchdog          # ONLY new findings, empty = silent (cron)
    python3 anomaly.py --state DIR         # sample directory (default: $TELEMETRY_DIR)

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

DEFAULT_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
    "telemetry",
)

# Metrics for statistical analysis: field -> (label, direction, unit)
# direction: "up" = only upward deviations matter, "down" = only downward,
# "both" = either.
METRICS = {
    "pwr_drain_w":        ("Battery power (+ discharge/- charge)", "up", "W"),
    "thm_pkg_c":          ("CPU package temperature", "up", "C"),
    "thm_nvme_c":         ("NVMe temperature", "up", "C"),
    "cpu_util_pct":       ("CPU utilisation", "up", "%"),
    "cpu_load_per_core":  ("Load per core", "up", ""),
    "cpu_iowait_pct":     ("IO wait", "up", "%"),
    "mem_avail_mb":       ("Available memory", "down", "MB"),
    "mem_swapout_rate":   ("Swap-out rate", "up", "p/s"),
    "mem_majfault_rate":  ("Major faults", "up", "/s"),
    "dsk_write_mbs":      ("Disk write rate", "up", "MB/s"),
    "dsk_io_util_pct":    ("Disk utilisation", "up", "%"),
    "net_rx_mbs":         ("Network in", "up", "MB/s"),
    "net_tx_mbs":         ("Network out", "up", "MB/s"),
    "cpu_procs_total":    ("Process count", "both", ""),
}

Z_THRESHOLD = 4.0        # robust z-score from which a point is reported
MIN_SAMPLES = 30         # below this, statistics would be dishonest
MAD_SCALE = 1.4826       # MAD -> sigma equivalent for a normal distribution

HARD_RULES = {
    "disk_free_gb_crit": 5.0,
    "disk_free_gb_warn": 15.0,
    "bat_health_drop_pct": 2.0,     # health loss within the window
    "throttle_delta_burst": 400,
    "swap_used_pct_warn": 90.0,
    "mem_avail_mb_crit": 500,
    "temp_crit_c": 95.0,
}


# --------------------------------------------------------------------- loading

def load_samples(state_dir: str, hours: float) -> tuple[list[dict], list[str]]:
    """Reads samples_*.jsonl for the relevant days. Corrupt lines are skipped
    (never abort) and counted."""
    notes: list[str] = []
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    samples: list[dict] = []
    bad = 0

    try:
        files = sorted(f for f in os.listdir(state_dir)
                       if f.startswith("samples_") and f.endswith(".jsonl"))
    except OSError:
        return [], [f"no data directory: {state_dir}"]

    # Only files from the cutoff day onwards (one file per day), plus the day before as buffer.
    keep = [f for f in files
            if f[len("samples_"):-len(".jsonl")] >= (cutoff - timedelta(days=1)).strftime("%Y%m%d")]

    for fname in keep:
        path = os.path.join(state_dir, fname)
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    ts = obj.get("ts_local")
                    if not ts:
                        bad += 1
                        continue
                    try:
                        when = datetime.fromisoformat(ts)
                    except ValueError:
                        bad += 1
                        continue
                    if when >= cutoff:
                        obj["_when"] = when
                        samples.append(obj)
        except OSError as exc:
            notes.append(f"file not readable: {fname} ({exc.__class__.__name__})")

    if bad:
        notes.append(f"{bad} unreadable lines skipped")
    samples.sort(key=lambda s: s["_when"])
    return samples, notes


# --------------------------------------------------------------------- statistics

def bucket_of(when: datetime) -> str:
    """(weekday|weekend, quarter of the day). Coarse enough for buckets to fill
    up, fine enough that night != working hours."""
    day = "we" if when.weekday() >= 5 else "wd"
    quarter = ["night", "morning", "day", "evening"][min(when.hour // 6, 3)]
    return f"{day}-{quarter}"


def robust_z(value: float, series: list[float]) -> tuple[float, float, float]:
    """Robust z-score. If MAD=0 (constant series), fall back to the mean absolute
    deviation; if that is 0 too, there is no spread and therefore no meaningful
    z-score (0.0)."""
    med = statistics.median(series)
    mad = statistics.median([abs(x - med) for x in series])
    scale = mad * MAD_SCALE
    if scale == 0:
        mean_abs = sum(abs(x - med) for x in series) / len(series)
        scale = mean_abs * 1.2533
    if scale == 0:
        return 0.0, med, 0.0
    return (value - med) / scale, med, scale


def analyse_metric(field: str, samples: list[dict]) -> list[dict]:
    label, direction, unit = METRICS[field]
    by_bucket: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for s in samples:
        val = s.get(field)
        if isinstance(val, (int, float)):
            by_bucket[bucket_of(s["_when"])].append((s["_when"], float(val)))

    findings: list[dict] = []
    for bucket, points in by_bucket.items():
        if len(points) < MIN_SAMPLES:
            continue
        values = [v for _, v in points]
        # The evaluated point must not dominate its own reference; with
        # >= MIN_SAMPLES its influence on the median is negligible.
        for when, val in points:
            z, med, scale = robust_z(val, values)
            if direction == "up" and z < Z_THRESHOLD:
                continue
            if direction == "down" and z > -Z_THRESHOLD:
                continue
            if direction == "both" and abs(z) < Z_THRESHOLD:
                continue
            findings.append({
                "kind": "statistical",
                "field": field,
                "label": label,
                "bucket": bucket,
                "ts": when.isoformat(timespec="seconds"),
                "value": round(val, 2),
                "median": round(med, 2),
                "z": round(z, 1),
                "unit": unit,
                "text": (f"{label}: {val:.2f}{unit} vs. median {med:.2f}{unit} "
                         f"(z={z:.1f}, bucket {bucket})"),
            })

    # Report only the strongest deviations per metric — otherwise the report
    # drowns in a single long high-load phase.
    findings.sort(key=lambda f: abs(f["z"]), reverse=True)
    return findings[:3]


# --------------------------------------------------------------------- rules

def analyse_rules(samples: list[dict]) -> list[dict]:
    out: list[dict] = []
    if not samples:
        return out
    last = samples[-1]

    def add(sev: str, code: str, text: str) -> None:
        out.append({"kind": "rule", "severity": sev, "code": code,
                    "ts": last["_when"].isoformat(timespec="seconds"), "text": text})

    free = last.get("dsk_home_free_gb")
    if isinstance(free, (int, float)):
        if free < HARD_RULES["disk_free_gb_crit"]:
            add("critical", "disk_full", f"home filesystem has only {free}GB free")
        elif free < HARD_RULES["disk_free_gb_warn"]:
            add("warning", "disk_low", f"home filesystem has only {free}GB free")

    avail = last.get("mem_avail_mb")
    if isinstance(avail, (int, float)) and avail < HARD_RULES["mem_avail_mb_crit"]:
        add("critical", "mem_low", f"only {avail}MB memory available")

    swap = last.get("swap_used_pct")
    if isinstance(swap, (int, float)) and swap >= HARD_RULES["swap_used_pct_warn"]:
        add("warning", "swap_high", f"swap {swap}% used")

    # Battery health across the window (only meaningful with enough distance).
    healths = [(s["_when"], s["pwr_bat_health_pct"]) for s in samples
               if isinstance(s.get("pwr_bat_health_pct"), (int, float))]
    if len(healths) >= 2:
        drop = healths[0][1] - healths[-1][1]
        if drop >= HARD_RULES["bat_health_drop_pct"]:
            add("warning", "bat_health",
                f"battery health dropped by {drop:.1f}% in window "
                f"({healths[0][1]}% -> {healths[-1][1]}%)")

    cycles = [s["pwr_cycles"] for s in samples if isinstance(s.get("pwr_cycles"), int)]
    if cycles and cycles[-1] != cycles[0]:
        add("info", "bat_cycles", f"charge cycles {cycles[0]} -> {cycles[-1]}")

    bursts = [s for s in samples
              if isinstance(s.get("thm_pkg_throttle_delta"), int)
              and s["thm_pkg_throttle_delta"] >= HARD_RULES["throttle_delta_burst"]]
    if bursts:
        worst = max(bursts, key=lambda s: s["thm_pkg_throttle_delta"])
        add("warning", "throttle_burst",
            f"{len(bursts)}x heavy CPU throttling, peak +{worst['thm_pkg_throttle_delta']} "
            f"events at {worst['_when'].strftime('%H:%M')}")

    hot = [s["thm_pkg_c"] for s in samples
           if isinstance(s.get("thm_pkg_c"), (int, float))
           and s["thm_pkg_c"] >= HARD_RULES["temp_crit_c"]]
    if hot:
        add("warning", "temp_crit", f"{len(hot)}x package temperature >= "
                                    f"{HARD_RULES['temp_crit_c']}C (max {max(hot)}C)")

    # Failed units: the state change is the signal, not the steady state.
    for key, scope in (("sys_failed_user", "user"), ("sys_failed_system", "system")):
        series = [(s["_when"], s[key]) for s in samples if isinstance(s.get(key), int)]
        if not series:
            continue
        first, latest = series[0][1], series[-1][1]
        if latest > first:
            add("warning", f"failed_{scope}",
                f"failed {scope} units: {first} -> {latest}")
        elif latest > 0:
            add("info", f"failed_{scope}", f"{latest} failed {scope} unit(s) (constant)")

    boots = {s.get("boot_id") for s in samples if s.get("boot_id")}
    if len(boots) > 1:
        add("info", "reboot", f"{len(boots) - 1} reboot(s) in window")

    cores = [s["sys_coredumps_today"] for s in samples
             if isinstance(s.get("sys_coredumps_today"), int)]
    if cores and max(cores) > 0:
        add("warning", "coredumps", f"{max(cores)} coredump(s) today")

    warns: dict[str, int] = defaultdict(int)
    for s in samples:
        for w in s.get("warn") or []:
            warns[w] += 1
    for w, n in sorted(warns.items(), key=lambda kv: -kv[1])[:5]:
        add("info", "sampler_warn", f"sampler warning {n}x: {w}")

    return out


# --------------------------------------------------------------------- context

def summarize_context(samples: list[dict]) -> dict:
    """Facts for the report header — no judgement."""
    def stat(field: str) -> dict | None:
        vals = [s[field] for s in samples if isinstance(s.get(field), (int, float))]
        if not vals:
            return None
        return {"min": round(min(vals), 2), "median": round(statistics.median(vals), 2),
                "max": round(max(vals), 2), "n": len(vals)}

    ctx = {
        "samples": len(samples),
        "from": samples[0]["_when"].isoformat(timespec="seconds") if samples else None,
        "to": samples[-1]["_when"].isoformat(timespec="seconds") if samples else None,
    }
    if samples:
        ac = [int(s["pwr_ac_online"]) for s in samples
              if isinstance(s.get("pwr_ac_online"), int)]
        if ac:
            ctx["ac_share_pct"] = round(sum(ac) / len(ac) * 100, 1)
        for f in ("thm_pkg_c", "cpu_util_pct", "mem_avail_mb", "swap_used_pct",
                  "pwr_drain_w", "dsk_home_free_gb"):
            s = stat(f)
            if s:
                ctx[f] = s
        procs: dict[str, int] = defaultdict(int)
        for s in samples:
            for p in s.get("top_rss") or []:
                procs[p["name"]] = max(procs[p["name"]], p["rss_mb"])
        ctx["top_rss_max"] = dict(sorted(procs.items(), key=lambda kv: -kv[1])[:5])
    return ctx


# --------------------------------------------------------------------- report

def build_report(state_dir: str, hours: float) -> dict:
    samples, notes = load_samples(state_dir, hours)
    report: dict = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window_h": hours,
        "notes": notes,
        "facts": summarize_context(samples),
        "observations": [],
        "hypotheses": [],
    }
    if not samples:
        report["notes"].append("no samples in window")
        return report

    report["observations"].extend(analyse_rules(samples))
    if len(samples) < MIN_SAMPLES:
        report["notes"].append(
            f"only {len(samples)} samples — statistics start at {MIN_SAMPLES} per bucket "
            f"(baseline still growing)")
    else:
        for field in METRICS:
            report["observations"].extend(analyse_metric(field, samples))

    report["hypotheses"] = derive_hypotheses(report["observations"], samples)
    return report


def derive_hypotheses(observations: list[dict], samples: list[dict]) -> list[str]:
    """Interpretations — explicitly uncertain, never phrased as findings."""
    hyp: list[str] = []
    codes = {o.get("code") for o in observations}
    fields = {o.get("field") for o in observations}

    if "swap_high" in codes or "mem_swapout_rate" in fields:
        procs: dict[str, int] = {}
        for s in samples:
            for p in s.get("top_rss") or []:
                procs[p["name"]] = max(procs.get(p["name"], 0), p["rss_mb"])
        if procs:
            top = max(procs.items(), key=lambda kv: kv[1])
            hyp.append(f"memory pressure might originate from '{top[0]}' "
                       f"(largest RSS {top[1]}MB) — not proven, correlation only")
    if "throttle_burst" in codes or "thm_pkg_c" in fields:
        hyp.append("throttling can mean cooling issues (dust/paste) OR a normal load "
                   "spike — only meaningful if the pattern repeats without load")
    if "failed_user" in codes or "failed_system" in codes:
        hyp.append("failed units may point to dead paths after a directory move — "
                   "check ExecStart against the filesystem")
    if "bat_health" in codes:
        hyp.append("a jump in battery health is often a calibration artefact after a "
                   "full cycle, not real wear — observe over weeks")
    return hyp


def render(report: dict) -> str:
    L = [f"# Telemetry anomaly report ({report['window_h']:.0f}h)",
         f"Generated: {report['generated']}"]
    f = report["facts"]
    L.append("")
    L.append("## Facts (measured)")
    L.append(f"- Samples: {f.get('samples')} | {f.get('from')} to {f.get('to')}")
    if "ac_share_pct" in f:
        L.append(f"- Share on AC power: {f['ac_share_pct']}%")
    for key, name in (("thm_pkg_c", "CPU temp"), ("cpu_util_pct", "CPU load"),
                      ("mem_avail_mb", "Free RAM"), ("swap_used_pct", "Swap"),
                      ("pwr_drain_w", "Battery power (+dis/-charge)"),
                      ("dsk_home_free_gb", "home free")):
        if key in f:
            s = f[key]
            L.append(f"- {name}: min {s['min']} / median {s['median']} / max {s['max']} (n={s['n']})")
    if f.get("top_rss_max"):
        L.append("- Largest processes (max RSS MB): " +
                 ", ".join(f"{k} {v}" for k, v in f["top_rss_max"].items()))

    L.append("")
    L.append("## Observations (threshold/statistic exceeded)")
    obs = report["observations"]
    if not obs:
        L.append("- none")
    for o in obs:
        if o["kind"] == "rule":
            L.append(f"- [{o['severity']}] {o['text']}")
        else:
            L.append(f"- [statistical] {o['text']}")

    if report["hypotheses"]:
        L.append("")
        L.append("## Hypotheses (UNCERTAIN, not proven)")
        for h in report["hypotheses"]:
            L.append(f"- {h}")
    if report["notes"]:
        L.append("")
        L.append("## Notes")
        for n in report["notes"]:
            L.append(f"- {n}")
    return "\n".join(L)


def watchdog_filter(report: dict, state_dir: str) -> list[str]:
    """Report only NEW findings. The signature deliberately contains no
    timestamps or measured values — otherwise every run would re-report the
    same problem (permanent noise)."""
    seen_path = os.path.join(state_dir, "anomaly_seen.json")
    try:
        with open(seen_path) as fh:
            seen = set(json.load(fh))
    except (OSError, ValueError):
        seen = set()

    fresh: list[str] = []
    now: set[str] = set()
    for o in report["observations"]:
        if o["kind"] == "rule":
            sig = f"rule:{o['code']}:{o.get('severity')}"
        else:
            sig = f"stat:{o['field']}:{o['bucket']}"
        now.add(sig)
        if sig not in seen:
            fresh.append(o["text"])

    tmp = f"{seen_path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(sorted(now), fh)
    os.replace(tmp, seen_path)
    return fresh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Anomaly report on telemetry samples")
    ap.add_argument("--state", default=os.environ.get("TELEMETRY_DIR", DEFAULT_DIR),
                    help=f"sample directory (default: $TELEMETRY_DIR or {DEFAULT_DIR})")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watchdog", action="store_true",
                    help="only new findings on stdout, empty = silent (for cron jobs)")
    args = ap.parse_args(argv)

    state_dir = os.path.abspath(os.path.expanduser(args.state))
    report = build_report(state_dir, args.hours)

    if args.watchdog:
        fresh = watchdog_filter(report, state_dir)
        if fresh:
            print(f"Telemetry: {len(fresh)} new finding(s)")
            for t in fresh:
                print(f"  - {t}")
            return 1
        return 0

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str)
          if args.json else render(report))
    return 1 if report["observations"] else 0


if __name__ == "__main__":
    sys.exit(main())
