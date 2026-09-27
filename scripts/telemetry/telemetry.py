#!/usr/bin/env python3
"""telemetry.py — deterministic Linux system sampler (stdlib only, read-only, no LLM).

Each run ("tick") captures one flat data point — power, thermals, CPU, memory,
disk, network, system state — and appends it to a daily JSONL file.
Delta-based values (rates, throttle increments, battery drain) are computed
against the previous tick; the raw counters for that are kept in a state file.

Why: run it periodically (e.g. every minute via a systemd timer) BEFORE you
schedule heavy background work, so that later anomaly detection (anomaly.py)
knows what "normal" looks like. It deliberately also runs on battery —
otherwise the battery baseline would be missing. Throttling heavy work is the
job of governor.py, not of the sampler.

Usage:
    python3 telemetry.py                 # one tick, silent (errors on stderr)
    python3 telemetry.py --print         # tick + human-readable summary
    python3 telemetry.py --json          # tick + sample as JSON on stdout
    python3 telemetry.py --state DIR     # override state/output directory
    TELEMETRY_DIR=DIR python3 telemetry.py

Exit codes: 0 ok, 1 sample written but some sources were missing, 2 usage error.

Tested on a ThinkPad-class laptop running Arch Linux; sensor names
(coretemp, thinkpad, nvme, ...) are looked up by name and simply stay empty
on hardware that does not expose them.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
    "telemetry",
)
SECTOR_BYTES = 512
# Expensive checks only every N-th tick, so a single sample stays cheap.
CADENCE_COREDUMP = 15

_WARN: list[str] = []


def warn(msg: str) -> None:
    if msg not in _WARN:
        _WARN.append(msg)


# ---------------------------------------------------------------- low-level readers

def read_text(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def run(cmd: list[str], timeout: float = 4.0) -> str | None:
    """Subprocess with a hard timeout; any failure -> None (never crash)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode not in (0, 1):
            return None
        return res.stdout
    except (OSError, subprocess.SubprocessError):
        warn(f"cmd-failed:{cmd[0]}")
        return None


# ---------------------------------------------------------------- sensor discovery

def hwmon_map() -> dict[str, str]:
    """hwmonN numbers are NOT stable across boots -> always resolve by name."""
    out: dict[str, str] = {}
    base = "/sys/class/hwmon"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return out
    for entry in entries:
        path = os.path.join(base, entry)
        name = read_text(os.path.join(path, "name"))
        if name and name not in out:
            out[name] = path
    return out


def hwmon_temp(hw: dict[str, str], name: str, index: int = 1) -> float | None:
    path = hw.get(name)
    if not path:
        return None
    val = read_int(os.path.join(path, f"temp{index}_input"))
    return round(val / 1000.0, 1) if val is not None else None


def thermal_zones() -> dict[str, float]:
    out: dict[str, float] = {}
    base = "/sys/class/thermal"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return out
    for entry in entries:
        if not entry.startswith("thermal_zone"):
            continue
        path = os.path.join(base, entry)
        ztype = read_text(os.path.join(path, "type"))
        temp = read_int(os.path.join(path, "temp"))
        if ztype and temp is not None:
            out[ztype] = round(temp / 1000.0, 1)
    return out


# ---------------------------------------------------------------- collectors

def collect_power(sample: dict) -> None:
    ac = None
    bat = None
    base = "/sys/class/power_supply"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        warn("power_supply-missing")
        return
    for entry in entries:
        path = os.path.join(base, entry)
        ptype = read_text(os.path.join(path, "type"))
        if ptype == "Mains" and ac is None:
            ac = path
        elif ptype == "Battery" and bat is None:
            bat = path

    sample["pwr_ac_online"] = read_int(os.path.join(ac, "online")) if ac else None
    if not bat:
        return
    sample["pwr_bat_pct"] = read_int(os.path.join(bat, "capacity"))
    sample["pwr_bat_status"] = read_text(os.path.join(bat, "status"))
    sample["pwr_cycles"] = read_int(os.path.join(bat, "cycle_count"))

    energy = read_int(os.path.join(bat, "energy_now"))          # uWh
    energy_full = read_int(os.path.join(bat, "energy_full"))
    if energy is not None:
        sample["pwr_energy_wh"] = round(energy / 1e6, 3)
        sample["_raw_energy_uwh"] = energy
    if energy_full:
        sample["pwr_energy_full_wh"] = round(energy_full / 1e6, 2)
        design = read_int(os.path.join(bat, "energy_full_design"))
        if design:
            sample["pwr_bat_health_pct"] = round(energy_full / design * 100, 1)

    # PITFALL (measured on a ThinkPad T490s): power_now constantly reports 0
    # although the battery is discharging. The value is recorded anyway, but
    # the reliable power figure is derived from the energy delta (see apply_deltas).
    power_now = read_int(os.path.join(bat, "power_now"))
    sample["pwr_power_now_w"] = round(power_now / 1e6, 2) if power_now is not None else None


def collect_thermal(sample: dict) -> None:
    hw = hwmon_map()
    zones = thermal_zones()
    sample["thm_pkg_c"] = zones.get("x86_pkg_temp") or hwmon_temp(hw, "coretemp")
    sample["thm_acpitz_c"] = zones.get("acpitz")
    sample["thm_thinkpad_c"] = hwmon_temp(hw, "thinkpad")
    sample["thm_nvme_c"] = hwmon_temp(hw, "nvme")
    sample["thm_pch_c"] = zones.get("pch_cannonlake")
    sample["thm_wifi_c"] = zones.get("iwlwifi_1")

    # Throttle counters are monotonic since boot -> only the delta is a signal.
    tt = "/sys/devices/system/cpu/cpu0/thermal_throttle"
    sample["_raw_pkg_throttle"] = read_int(os.path.join(tt, "package_throttle_count"))
    sample["_raw_core_throttle"] = read_int(os.path.join(tt, "core_throttle_count"))
    sample["_raw_pkg_throttle_ms"] = read_int(os.path.join(tt, "package_throttle_total_time_ms"))


def collect_cpu(sample: dict) -> None:
    load = read_text("/proc/loadavg")
    if load:
        parts = load.split()
        try:
            sample["cpu_load1"] = float(parts[0])
            sample["cpu_load5"] = float(parts[1])
            sample["cpu_load15"] = float(parts[2])
            running, _, total = parts[3].partition("/")
            sample["cpu_procs_running"] = int(running)
            sample["cpu_procs_total"] = int(total)
        except (ValueError, IndexError):
            warn("loadavg-unparsable")

    ncpu = os.cpu_count() or 1
    sample["cpu_count"] = ncpu
    if sample.get("cpu_load1") is not None:
        sample["cpu_load_per_core"] = round(sample["cpu_load1"] / ncpu, 3)

    stat = read_text("/proc/stat")
    if stat:
        for line in stat.splitlines():
            if line.startswith("cpu "):
                vals = [int(v) for v in line.split()[1:] if v.isdigit()]
                if len(vals) >= 5:
                    sample["_raw_cpu_total"] = sum(vals)
                    sample["_raw_cpu_idle"] = vals[3] + vals[4]
                    sample["_raw_cpu_iowait"] = vals[4]
                break
            if line.startswith("procs_blocked"):
                break
        for line in stat.splitlines():
            if line.startswith("procs_blocked"):
                try:
                    sample["cpu_procs_blocked"] = int(line.split()[1])
                except (ValueError, IndexError):
                    pass

    freq = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq:
        sample["cpu_freq_mhz"] = round(freq / 1000)
    sample["cpu_governor"] = read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    sample["cpu_epp"] = read_text(
        "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")


def collect_memory(sample: dict) -> None:
    meminfo = read_text("/proc/meminfo")
    if meminfo:
        info: dict[str, int] = {}
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            val = rest.strip().split(" ")[0]
            if val.isdigit():
                info[key] = int(val)  # kB
        mb = lambda k: round(info[k] / 1024) if k in info else None  # noqa: E731
        sample["mem_total_mb"] = mb("MemTotal")
        sample["mem_avail_mb"] = mb("MemAvailable")
        sample["mem_free_mb"] = mb("MemFree")
        sample["mem_cached_mb"] = mb("Cached")
        sample["swap_total_mb"] = mb("SwapTotal")
        sample["swap_free_mb"] = mb("SwapFree")
        if info.get("SwapTotal"):
            used = info["SwapTotal"] - info.get("SwapFree", 0)
            sample["swap_used_pct"] = round(used / info["SwapTotal"] * 100, 1)
        if info.get("MemTotal"):
            avail = info.get("MemAvailable", 0)
            sample["mem_avail_pct"] = round(avail / info["MemTotal"] * 100, 1)

    vm = read_text("/proc/vmstat")
    if vm:
        for line in vm.splitlines():
            key, _, val = line.partition(" ")
            if key in ("pswpin", "pswpout", "pgmajfault") and val.strip().isdigit():
                sample[f"_raw_{key}"] = int(val)


def collect_disk(sample: dict) -> None:
    for label, path in (("root", "/"), ("home", os.path.expanduser("~"))):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        sample[f"dsk_{label}_free_gb"] = round(free / 1e9, 2)
        if total:
            sample[f"dsk_{label}_used_pct"] = round((total - free) / total * 100, 1)

    stats = read_text("/proc/diskstats")
    if not stats:
        return
    for line in stats.splitlines():
        parts = line.split()
        # Physical NVMe disks only — no partitions, loop or zram devices.
        if len(parts) < 14 or not parts[2].startswith("nvme") or "p" in parts[2][4:]:
            continue
        try:
            sample["_raw_dsk_read_sectors"] = int(parts[5])
            sample["_raw_dsk_write_sectors"] = int(parts[9])
            sample["_raw_dsk_io_ms"] = int(parts[12])
        except (ValueError, IndexError):
            pass
        break


def collect_net(sample: dict) -> None:
    dev = read_text("/proc/net/dev")
    if not dev:
        return
    rx = tx = 0
    for line in dev.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name in ("lo",) or name.startswith(("veth", "docker", "br-")):
            continue
        vals = rest.split()
        if len(vals) >= 9:
            try:
                rx += int(vals[0])
                tx += int(vals[8])
            except ValueError:
                pass
    sample["_raw_net_rx"] = rx
    sample["_raw_net_tx"] = tx


def collect_system(sample: dict, tick: int) -> None:
    uptime = read_text("/proc/uptime")
    if uptime:
        try:
            sample["uptime_s"] = int(float(uptime.split()[0]))
        except (ValueError, IndexError):
            pass
    sample["boot_id"] = read_text("/proc/sys/kernel/random/boot_id")

    for scope, key in (("--user", "sys_failed_user"), ("--system", "sys_failed_system")):
        out = run(["systemctl", scope, "--failed", "--no-legend", "--no-pager", "--plain"])
        if out is not None:
            sample[key] = len([l for l in out.splitlines() if l.strip()])

    # IdleHint is not always maintained under Wayland/KDE -> treat it as ONE
    # signal, never as the only gate (see governor.py).
    out = run(["loginctl", "list-sessions", "--no-legend"])
    idle = None
    if out:
        for line in out.splitlines():
            fields = line.split()
            if not fields:
                continue
            info = run(["loginctl", "show-session", fields[0], "-p", "IdleHint", "-p", "State"])
            if info and "State=active" in info:
                idle = "IdleHint=yes" in info
                break
    sample["sys_session_idle"] = idle

    if tick % CADENCE_COREDUMP == 0:
        out = run(["coredumpctl", "list", "--since=today", "--no-pager", "--no-legend"], timeout=8)
        if out is not None:
            sample["sys_coredumps_today"] = len([l for l in out.splitlines() if l.strip()])


def collect_top_processes(sample: dict, limit: int = 3) -> None:
    """Top memory consumers — context for memory anomalies. No delta needed."""
    procs = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    page_kb = 4
    for entry in entries:
        if not entry.isdigit():
            continue
        statm = read_text(f"/proc/{entry}/statm")
        if not statm:
            continue
        parts = statm.split()
        if len(parts) < 2:
            continue
        try:
            rss_mb = int(parts[1]) * page_kb / 1024
        except ValueError:
            continue
        if rss_mb < 50:
            continue
        comm = read_text(f"/proc/{entry}/comm") or "?"
        procs.append((round(rss_mb), comm))
    procs.sort(reverse=True)
    sample["top_rss"] = [{"rss_mb": m, "name": n} for m, n in procs[:limit]]


# ---------------------------------------------------------------- delta computation

def apply_deltas(sample: dict, prev: dict | None) -> None:
    """Rates from raw counters. Without a predecessor all rates stay None (honest)."""
    if not prev:
        sample["interval_s"] = None
        return

    try:
        dt = sample["ts_epoch"] - prev.get("ts_epoch", 0)
    except (TypeError, KeyError):
        return
    sample["interval_s"] = round(dt, 1)
    if dt <= 0 or dt > 3600:
        # Suspend / clock jump: rates would be nonsense -> deliberately omitted.
        # NOT a warn(): suspend is a normal lifecycle event, not a defect. As a
        # warning it would make the unit report "failed" after every resume.
        sample["note"] = f"interval-implausible:{dt:.0f}s"
        return

    # Reboot detected -> monotonic counters were reset, no deltas.
    # NOT a warn() either — otherwise systemd reports the unit as failed after
    # EVERY boot. The finding stays in the sample as "note"; real defects
    # (cmd-failed, state-corrupt) remain warnings and therefore exit 1.
    if sample.get("boot_id") and prev.get("boot_id") and sample["boot_id"] != prev["boot_id"]:
        sample["note"] = "reboot-detected"
        return

    def delta(key: str) -> float | None:
        a, b = sample.get(key), prev.get(key)
        if a is None or b is None:
            return None
        d = a - b
        return d if d >= 0 else None  # counter overflow/reset -> discard

    # CPU utilisation
    d_total = delta("_raw_cpu_total")
    d_idle = delta("_raw_cpu_idle")
    d_iowait = delta("_raw_cpu_iowait")
    if d_total and d_total > 0:
        if d_idle is not None:
            sample["cpu_util_pct"] = round((1 - d_idle / d_total) * 100, 1)
        if d_iowait is not None:
            sample["cpu_iowait_pct"] = round(d_iowait / d_total * 100, 2)

    # Swap rates in pages/s — the fill level is a state, the RATE is the pain.
    for raw, out in (("_raw_pswpin", "mem_swapin_rate"),
                     ("_raw_pswpout", "mem_swapout_rate"),
                     ("_raw_pgmajfault", "mem_majfault_rate")):
        d = delta(raw)
        if d is not None:
            sample[out] = round(d / dt, 1)

    # Disk
    d_read = delta("_raw_dsk_read_sectors")
    d_write = delta("_raw_dsk_write_sectors")
    if d_read is not None:
        sample["dsk_read_mbs"] = round(d_read * SECTOR_BYTES / 1e6 / dt, 2)
    if d_write is not None:
        sample["dsk_write_mbs"] = round(d_write * SECTOR_BYTES / 1e6 / dt, 2)
    d_io = delta("_raw_dsk_io_ms")
    if d_io is not None:
        sample["dsk_io_util_pct"] = round(min(d_io / (dt * 1000) * 100, 100.0), 1)

    # Network
    for raw, out in (("_raw_net_rx", "net_rx_mbs"), ("_raw_net_tx", "net_tx_mbs")):
        d = delta(raw)
        if d is not None:
            sample[out] = round(d / 1e6 / dt, 3)

    # Throttle increments
    for raw, out in (("_raw_pkg_throttle", "thm_pkg_throttle_delta"),
                     ("_raw_core_throttle", "thm_core_throttle_delta"),
                     ("_raw_pkg_throttle_ms", "thm_throttle_ms_delta")):
        d = delta(raw)
        if d is not None:
            sample[out] = d

    # Battery power from the energy delta (power_now may report 0, see above).
    e_now, e_prev = sample.get("_raw_energy_uwh"), prev.get("_raw_energy_uwh")
    if e_now is not None and e_prev is not None:
        d_wh = (e_prev - e_now) / 1e6          # positive = discharging
        sample["pwr_drain_w"] = round(d_wh * 3600 / dt, 2)


# ---------------------------------------------------------------- orchestration

def build_sample(tick: int) -> dict:
    now = datetime.now(timezone.utc)
    sample: dict = {
        "ts": now.isoformat(timespec="seconds"),
        "ts_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ts_epoch": time.time(),
        "tick": tick,
    }
    for fn in (collect_power, collect_thermal, collect_cpu,
               collect_memory, collect_disk, collect_net):
        try:
            fn(sample)
        except Exception as exc:                      # the sampler must NEVER die
            warn(f"{fn.__name__}:{type(exc).__name__}")
    for fn2, arg in ((collect_system, tick), (collect_top_processes, 3)):
        try:
            fn2(sample, arg)
        except Exception as exc:
            warn(f"{fn2.__name__}:{type(exc).__name__}")
    return sample


def load_state(state_path: str) -> dict:
    raw = read_text(state_path)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        warn("state-corrupt-recreated")
        return {}


def write_atomic(path: str, payload: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def summarize(s: dict) -> str:
    def g(key: str, fmt: str = "{}", default: str = "?") -> str:
        val = s.get(key)
        return default if val is None else fmt.format(val)

    ac = s.get("pwr_ac_online")
    power = "AC" if ac == 1 else ("BATTERY" if ac == 0 else "?")
    lines = [
        f"[{s.get('ts_local', '?')}] tick={s.get('tick')} interval={g('interval_s')}s",
        f"  Power   : {power} bat={g('pwr_bat_pct')}% drain={g('pwr_drain_w')}W "
        f"health={g('pwr_bat_health_pct')}% cycles={g('pwr_cycles')}",
        f"  Thermal : pkg={g('thm_pkg_c')}C nvme={g('thm_nvme_c')}C "
        f"tp={g('thm_thinkpad_c')}C throttle+={g('thm_pkg_throttle_delta')}",
        f"  CPU     : util={g('cpu_util_pct')}% load1={g('cpu_load1')} "
        f"(/core {g('cpu_load_per_core')}) iowait={g('cpu_iowait_pct')}% "
        f"freq={g('cpu_freq_mhz')}MHz",
        f"  Memory  : avail={g('mem_avail_mb')}MB ({g('mem_avail_pct')}%) "
        f"swap={g('swap_used_pct')}% in/s={g('mem_swapin_rate')} out/s={g('mem_swapout_rate')}",
        f"  Disk    : home free={g('dsk_home_free_gb')}GB r={g('dsk_read_mbs')}MB/s "
        f"w={g('dsk_write_mbs')}MB/s util={g('dsk_io_util_pct')}%",
        f"  System  : failed u/s={g('sys_failed_user')}/{g('sys_failed_system')} "
        f"idle={g('sys_session_idle')} procs={g('cpu_procs_total')}",
    ]
    top = s.get("top_rss") or []
    if top:
        lines.append("  Top RSS : " + ", ".join(f"{p['name']} {p['rss_mb']}MB" for p in top))
    if _WARN:
        lines.append("  WARN    : " + ", ".join(_WARN))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="System telemetry sample (stdlib, read-only)")
    ap.add_argument("--state", default=os.environ.get("TELEMETRY_DIR", DEFAULT_DIR),
                    help=f"state/output directory (default: $TELEMETRY_DIR or {DEFAULT_DIR})")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="print a summary to stdout")
    ap.add_argument("--json", dest="do_json", action="store_true",
                    help="print the sample as JSON to stdout")
    ap.add_argument("--no-write", action="store_true",
                    help="write nothing (dry run)")
    args = ap.parse_args(argv)

    state_dir = os.path.abspath(os.path.expanduser(args.state))
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "last_sample.json")

    state = load_state(state_path)
    tick = int(state.get("tick", 0)) + 1

    sample = build_sample(tick)
    apply_deltas(sample, state or None)
    if _WARN:
        sample["warn"] = list(_WARN)

    if not args.no_write:
        # Raw counters + timestamp stay in the state file (for the next delta);
        # they are not written to the JSONL (noise).
        write_atomic(state_path, json.dumps(sample, ensure_ascii=False))
        public = {k: v for k, v in sample.items() if not k.startswith("_")}
        day = datetime.now().strftime("%Y%m%d")
        out_path = os.path.join(state_dir, f"samples_{day}.jsonl")
        with open(out_path, "a") as fh:
            fh.write(json.dumps(public, ensure_ascii=False) + "\n")

    if args.do_json:
        print(json.dumps({k: v for k, v in sample.items() if not k.startswith("_")},
                         ensure_ascii=False, indent=2))
    elif args.do_print:
        print(summarize(sample))

    for msg in _WARN:
        print(f"WARN: {msg}", file=sys.stderr)
    return 1 if _WARN else 0


if __name__ == "__main__":
    sys.exit(main())
