#!/usr/bin/env python3
"""governor.py — decides whether heavy background work may run right now.

Core idea: systemd's `ConditionACPower=` is only checked WHEN A UNIT STARTS.
A scan that runs for 40 minutes does not notice that the charger was unplugged
after 3 minutes. This governor is therefore asked again BEFORE EVERY WORK
PACKAGE and returns one of three states:

    FULL    — all gates green, full batch size
    GENTLE  — reserves are getting tight: smaller batches, longer pauses
    STOP    — write a checkpoint and pause (do NOT abort)

Library usage (the normal case):

    from governor import Governor
    gov = Governor()
    for package in packages:
        d = gov.decide()
        if d.state == "STOP":
            checkpoint(); break
        process(package, batch=d.batch_size)
        time.sleep(d.sleep_s)

CLI usage:
    python3 governor.py            # human-readable decision
    python3 governor.py --json     # machine-readable
    python3 governor.py --quiet    # exit code only: 0=FULL 1=GENTLE 2=STOP

Read-only: only reads /sys, /proc and loginctl (via telemetry.py). Writes nothing.
Requires telemetry.py in the same directory.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import telemetry  # noqa: E402  (same family, deliberately reused)

# --------------------------------------------------------------------- thresholds
# Calibrated on a 4-core/8-thread laptop with 16 GB RAM and 4 GB zram swap.
# Adjust for your machine, or pass overrides via Governor(thresholds={...}).

THRESH = {
    # Power
    "bat_min_pct": 60,           # irrelevant on AC; hard limit on battery
    "bat_gentle_pct": 80,
    # Thermals — on the reference machine 80C+ at idle is real (measured), so
    # the THROTTLE INCREMENT is the leading signal, not the absolute temperature.
    "pkg_temp_stop_c": 92.0,
    "pkg_temp_gentle_c": 85.0,
    "throttle_delta_stop": 400,  # throttle events since the previous sample
    # Memory — the actual bottleneck on small machines.
    "mem_avail_stop_mb": 1500,
    "mem_avail_gentle_mb": 3000,
    "swapout_stop_rate": 200.0,  # pages/s of sustained swap-out = thrashing
    "swapout_gentle_rate": 50.0,
    # Load
    "load_stop_per_core": 1.5,
    "load_gentle_per_core": 0.8,
    "iowait_gentle_pct": 15.0,
    # Disk
    "disk_free_stop_gb": 5.0,
    "disk_free_gentle_gb": 15.0,
    # User presence
    "user_active_stop": True,    # active, non-idle session => back off
}

BATCH = {"FULL": 500, "GENTLE": 100, "STOP": 0}
SLEEP = {"FULL": 0.0, "GENTLE": 2.0, "STOP": 60.0}
RANK = {"FULL": 0, "GENTLE": 1, "STOP": 2}


@dataclass
class Decision:
    state: str = "FULL"
    batch_size: int = BATCH["FULL"]
    sleep_s: float = 0.0
    reasons: list[str] = field(default_factory=list)   # what slowed things down
    gates: dict = field(default_factory=dict)          # measured values, for review
    ts: str = ""

    @property
    def may_run(self) -> bool:
        return self.state != "STOP"


class Governor:
    """Takes fresh measurements on every decide(). No cache except the previous
    sample, which is needed for rates (swap, throttling)."""

    def __init__(self, thresholds: dict | None = None, respect_user: bool = True,
                 min_interval_s: float = 2.0):
        self.t = dict(THRESH)
        if thresholds:
            self.t.update(thresholds)
        self.respect_user = respect_user
        self.min_interval_s = min_interval_s
        self._prev: dict | None = None
        self._prev_ts: float = 0.0

    # ------------------------------------------------------------- measuring
    def _sample(self) -> dict:
        """Fresh sample; rates need a predecessor with enough distance."""
        telemetry._WARN.clear()
        s = telemetry.build_sample(tick=0)
        gap = s["ts_epoch"] - self._prev_ts
        if self._prev and gap >= self.min_interval_s:
            telemetry.apply_deltas(s, self._prev)
        self._prev = s
        self._prev_ts = s["ts_epoch"]
        return s

    # ------------------------------------------------------------- gates
    def _evaluate(self, s: dict) -> tuple[str, list[str], dict]:
        state = "FULL"
        reasons: list[str] = []
        gates: dict = {}

        def worse(new: str, why: str) -> None:
            nonlocal state
            reasons.append(why)
            if RANK[new] > RANK[state]:
                state = new

        # --- Gate 1: AC power. If the source is missing, battery operation is
        #     ASSUMED (the safe direction) instead of running blindly.
        ac = s.get("pwr_ac_online")
        gates["ac_online"] = ac
        on_ac = ac == 1
        if ac is None:
            worse("STOP", "ac power unknown (source missing -> assuming battery)")
        elif not on_ac:
            worse("STOP", "not on ac power")

        # --- Gate 2: battery reserve. Only relevant without AC or with a very empty battery.
        bat = s.get("pwr_bat_pct")
        gates["bat_pct"] = bat
        if bat is not None:
            if not on_ac and bat < self.t["bat_min_pct"]:
                worse("STOP", f"battery {bat}% < {self.t['bat_min_pct']}%")
            elif on_ac and bat < self.t["bat_min_pct"]:
                # On AC but battery still low: no reserve to keep working if
                # the cable gets pulled -> gentle only.
                worse("GENTLE", f"battery still charging ({bat}%), reserve low")
            elif not on_ac and bat < self.t["bat_gentle_pct"]:
                worse("GENTLE", f"battery {bat}% < {self.t['bat_gentle_pct']}%")

        # --- Gate 3: thermals. Throttle increment beats absolute temperature.
        thr = s.get("thm_pkg_throttle_delta")
        pkg = s.get("thm_pkg_c")
        gates["pkg_temp_c"] = pkg
        gates["throttle_delta"] = thr
        if thr is not None and thr >= self.t["throttle_delta_stop"]:
            worse("STOP", f"cpu throttling heavily (+{thr} events)")
        elif thr:
            worse("GENTLE", f"cpu throttling (+{thr} events)")
        if pkg is not None:
            if pkg >= self.t["pkg_temp_stop_c"]:
                worse("STOP", f"pkg {pkg}C >= {self.t['pkg_temp_stop_c']}C")
            elif pkg >= self.t["pkg_temp_gentle_c"]:
                worse("GENTLE", f"pkg {pkg}C >= {self.t['pkg_temp_gentle_c']}C")

        # --- Gate 4: memory — the decisive gate on small machines.
        avail = s.get("mem_avail_mb")
        swapout = s.get("mem_swapout_rate")
        gates["mem_avail_mb"] = avail
        gates["swapout_rate"] = swapout
        gates["swap_used_pct"] = s.get("swap_used_pct")
        if avail is not None:
            if avail < self.t["mem_avail_stop_mb"]:
                worse("STOP", f"only {avail}MB available < {self.t['mem_avail_stop_mb']}MB")
            elif avail < self.t["mem_avail_gentle_mb"]:
                worse("GENTLE", f"memory tight ({avail}MB)")
        if swapout is not None:
            if swapout >= self.t["swapout_stop_rate"]:
                worse("STOP", f"swap thrashing ({swapout:.0f} pages/s out)")
            elif swapout >= self.t["swapout_gentle_rate"]:
                worse("GENTLE", f"swap activity ({swapout:.0f} pages/s)")

        # --- Gate 5: load
        lpc = s.get("cpu_load_per_core")
        gates["load_per_core"] = lpc
        if lpc is not None:
            if lpc >= self.t["load_stop_per_core"]:
                worse("STOP", f"load {lpc}/core >= {self.t['load_stop_per_core']}")
            elif lpc >= self.t["load_gentle_per_core"]:
                worse("GENTLE", f"load {lpc}/core")
        iow = s.get("cpu_iowait_pct")
        gates["iowait_pct"] = iow
        if iow is not None and iow >= self.t["iowait_gentle_pct"]:
            worse("GENTLE", f"iowait {iow}%")

        # --- Gate 6: disk
        free = s.get("dsk_home_free_gb")
        gates["disk_free_gb"] = free
        if free is not None:
            if free < self.t["disk_free_stop_gb"]:
                worse("STOP", f"only {free}GB free")
            elif free < self.t["disk_free_gentle_gb"]:
                worse("GENTLE", f"disk space tight ({free}GB)")

        # --- Gate 7: user presence.
        # PITFALL: loginctl IdleHint is not reliably maintained under KDE/Wayland
        # (measured: permanently 'no', even on an unused machine). So IdleHint
        # is only a HINT here and never decisive on its own — otherwise nothing
        # would ever run. The hard presence protection comes from load + memory
        # (gates 4/5), which see real usage immediately.
        idle = s.get("sys_session_idle")
        gates["session_idle"] = idle
        if self.respect_user and idle is False and lpc is not None \
                and lpc >= self.t["load_gentle_per_core"]:
            worse("GENTLE", "session active and busy")

        if not reasons:
            reasons.append("all gates green")
        return state, reasons, gates

    # ------------------------------------------------------------- API
    def decide(self) -> Decision:
        s = self._sample()
        state, reasons, gates = self._evaluate(s)
        return Decision(
            state=state,
            batch_size=BATCH[state],
            sleep_s=SLEEP[state],
            reasons=reasons,
            gates=gates,
            ts=s.get("ts_local", ""),
        )

    def wait_until_ok(self, timeout_s: float = 3600, poll_s: float = 60) -> Decision:
        """Blocks until work is allowed again (or timeout)."""
        deadline = time.time() + timeout_s
        while True:
            d = self.decide()
            if d.may_run or time.time() >= deadline:
                return d
            time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="May heavy background work run right now?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--ignore-user", action="store_true",
                    help="ignore the user-presence gate")
    ap.add_argument("--sample-gap", type=float, default=2.0,
                    help="seconds between the two samples used for rates (default 2)")
    args = ap.parse_args(argv)

    gov = Governor(respect_user=not args.ignore_user)
    gov.decide()                      # first sample: reference for rates
    time.sleep(max(args.sample_gap, 0.1))
    d = gov.decide()                  # second sample: now with rates

    if args.json:
        print(json.dumps(asdict(d), ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"[{d.ts}] {d.state}  batch={d.batch_size} sleep={d.sleep_s}s")
        for r in d.reasons:
            print(f"  - {r}")
        keys = ("ac_online", "bat_pct", "pkg_temp_c", "throttle_delta", "mem_avail_mb",
                "swapout_rate", "load_per_core", "iowait_pct", "disk_free_gb",
                "session_idle")
        print("  gates: " + " ".join(
            f"{k}={d.gates.get(k)}" for k in keys if k in d.gates))
    return RANK[d.state]


if __name__ == "__main__":
    sys.exit(main())
