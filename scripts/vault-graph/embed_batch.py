#!/usr/bin/env python3
"""embed_batch.py — batch note embeddings for a Markdown vault, with a service watchdog.

Usage:
    python3 embed_batch.py <VAULT> [--limit N] [--only LIST] [--keep-alive]
                           [--embed-url URL] [--service UNIT | --no-manage-service]

What it does:
  - walks all .md notes of the vault (hidden folders excluded), chunks them
    (800 chars, 100 overlap), fetches embeddings via POST /embedding from a local
    llama.cpp embedding server and averages the chunk vectors per note (L2-normalised);
  - cache: <VAULT>/.embed-cache/vectors.npz (ids, vecs, mtime_ns, size, sha256),
    written atomically every SAVE_EVERY notes -> resumable after an abort; a note
    whose mtime changed but whose content hash did not is not re-embedded;
  - keep-alive: optionally starts the embedding server as a systemd --user unit
    ONCE at the beginning, waits for HTTP 200 on /health (503 "Loading model"
    does NOT count as ready), and a watchdog thread checks is-active + /health
    every WATCHDOG_S seconds and restarts the unit on failure. At the end the
    unit is stopped again unless --keep-alive is given, the unit was already
    running before, or other clients are still connected to the port;
  - progress: done/total, rate, elapsed, ETA (only after ETA_MIN notes, moving
    average over the last ETA_WINDOW) on stdout and in <VAULT>/.embed-cache/embed_batch.log.

With --no-manage-service the script never calls systemctl and simply expects the
server at --embed-url to be running.

Exit 0 if fewer than 1 % of the selected notes failed, otherwise 1.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- configuration

SERVICE = os.environ.get("EMBED_SERVICE", "llama-embed.service")   # systemd --user unit
EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8081")
MANAGE_SERVICE = True
EMBED_DIM = 768
CHUNK_CHARS = 800
CHUNK_OVERLAP = 100
BATCH = 16                  # texts per request
HTTP_TIMEOUT = 60.0
RETRIES = 3                 # 3 attempts, backoff 1/2/3 s
READY_TIMEOUT = 60.0        # max. wait until /health returns 200
WATCHDOG_S = 5.0            # watchdog check interval
ETA_MIN = 200               # ETA only after this many processed notes
ETA_WINDOW = 100            # moving average over the last N
PROGRESS_EVERY_N = 10
PROGRESS_EVERY_S = 5.0
SAVE_EVERY = 200

# ---------------------------------------------------------------- logging

class Log:
    def __init__(self, path: Path):
        self.fh = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with self.lock:
            print(line, flush=True)
            self.fh.write(line + "\n")
            self.fh.flush()

    def close(self) -> None:
        self.fh.close()

# ---------------------------------------------------------------- service

def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args, SERVICE],
                          capture_output=True, text=True, timeout=120)


def is_active() -> bool:
    if not MANAGE_SERVICE:
        return True   # externally managed: assume it runs, /health decides
    return systemctl("is-active").stdout.strip() == "active"


def embed_port() -> int:
    return urllib.parse.urlparse(EMBED_URL).port or 80


def foreign_clients() -> list[str]:
    """Other processes with an open connection to the embedding port — the unit
    must not be stopped under a parallel user."""
    try:
        out = subprocess.run(["ss", "-Htnp", "state", "established", "dport", f"= :{embed_port()}"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                                       # noqa: BLE001
        return []
    found = set()
    for name, pid in re.findall(r'\("([^"]+)",pid=(\d+)', out):
        if int(pid) != os.getpid():
            found.add(f"{name}[{pid}]")
    return sorted(found)


class Service:
    """Starts the embedding server, waits correctly for readiness, keeps it alive."""

    def __init__(self, log: Log, counter: collections.Counter):
        self.log = log
        self.counter = counter
        self.ready = threading.Event()
        self.check_now = threading.Event()
        self.stop_evt = threading.Event()
        self.restarts = 0
        self.was_active_before = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def healthy(self) -> bool:
        """True only on HTTP 200 + status ok (503 'Loading model' = not ready)."""
        self.counter["health"] += 1
        try:
            with urllib.request.urlopen(EMBED_URL + "/health", timeout=2) as r:
                return r.status == 200 and json.loads(r.read()).get("status") == "ok"
        except Exception:                                   # noqa: BLE001
            return False

    def _start_and_wait(self, reason: str) -> bool:
        with self.lock:
            self.ready.clear()
            t0 = time.monotonic()
            if MANAGE_SERVICE and not is_active():
                res = systemctl("start")
                if res.returncode != 0:
                    self.log(f"ERROR: systemctl start ({reason}): {res.stderr.strip()}")
            while time.monotonic() - t0 < READY_TIMEOUT:
                if self.healthy():
                    self.ready.set()
                    self.log(f"service ready ({reason}) after {time.monotonic() - t0:.2f} s")
                    return True
                time.sleep(0.1)
            self.log(f"ERROR: service not ready after {READY_TIMEOUT:.0f} s ({reason})")
            return False

    def start(self) -> bool:
        self.was_active_before = is_active()
        ok = self._start_and_wait("batch start" + (", already running" if self.was_active_before else ""))
        self.thread = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self.thread.start()
        return ok

    def _watchdog(self) -> None:
        while not self.stop_evt.is_set():
            self.check_now.wait(WATCHDOG_S)
            self.check_now.clear()
            if self.stop_evt.is_set():
                return
            active = is_active()
            if active and self.healthy():
                self.ready.set()
                continue
            if active:          # active but /health not ok -> re-check shortly
                time.sleep(1.0)
                if self.healthy():
                    self.ready.set()
                    continue
            self.restarts += 1
            self.log(f"WATCHDOG: service down (is-active={'active' if active else 'no'}), "
                     f"restart #{self.restarts}")
            self._start_and_wait(f"watchdog restart #{self.restarts}")

    def wait_ready(self) -> bool:
        return self.ready.wait(READY_TIMEOUT + WATCHDOG_S)

    def shutdown(self, keep_alive: bool) -> None:
        self.stop_evt.set()
        self.check_now.set()
        if self.thread:
            self.thread.join(timeout=READY_TIMEOUT + 5)
        if not MANAGE_SERVICE:
            return
        if keep_alive:
            self.log("service stays active (--keep-alive)")
            return
        if self.was_active_before:
            self.log("service was already running before the batch -> not stopped (other user)")
            return
        others = foreign_clients()
        if others:
            self.log(f"service NOT stopped: other connections to :{embed_port()} from {others}")
            return
        systemctl("stop")
        # Observed once: llama-server ignored SIGTERM and systemd killed it after
        # 90 s -> state 'failed'. reset-failed turns that into 'inactive'.
        state = systemctl("is-active").stdout.strip()
        if state == "failed":
            systemctl("reset-failed")
            self.log("service stop ended in 'failed' -> reset-failed executed")
        self.log(f"service stopped (is-active: {systemctl('is-active').stdout.strip()})")

# ---------------------------------------------------------------- embedding

def chunk_text(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Whitespace-normalised chunks, cut at word boundaries, with overlap."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end])
        start = max(end - overlap, start + 1)
    return chunks


class Embedder:
    def __init__(self, svc: Service, counter: collections.Counter):
        self.svc = svc
        self.counter = counter

    def _once(self, part: list[str]) -> np.ndarray:
        payload = json.dumps({"content": part}).encode()
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            if not self.svc.wait_ready():
                last_err = RuntimeError("service not ready (watchdog)")
            else:
                req = urllib.request.Request(EMBED_URL + "/embedding", data=payload,
                                             headers={"Content-Type": "application/json"})
                self.counter["embedding"] += 1
                try:
                    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                        out = json.loads(resp.read())
                    vecs = []
                    for item in sorted(out, key=lambda x: x.get("index", 0)):
                        emb = item["embedding"]
                        if emb and isinstance(emb[0], list):   # llama-server format [[...]]
                            emb = emb[0]
                        vecs.append(np.asarray(emb, dtype=np.float32))
                    m = np.vstack(vecs)
                    if m.shape != (len(part), EMBED_DIM):
                        raise ValueError(f"unexpected shape {m.shape}")
                    return m
                except Exception as e:                          # noqa: BLE001
                    last_err = e
                    # Connection error -> let the watchdog check immediately instead
                    # of waiting up to WATCHDOG_S for its next tick.
                    if not isinstance(e, urllib.error.HTTPError):
                        self.svc.ready.clear()
                        self.svc.check_now.set()
            time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"embedding failed: {last_err}")

    def _part(self, part: list[str]) -> np.ndarray:
        """Halve the batch on errors; halve a single long text and average."""
        try:
            return self._once(part)
        except Exception:
            if len(part) > 1:
                mid = len(part) // 2
                return np.vstack([self._part(part[:mid]), self._part(part[mid:])])
            text = part[0]
            if len(text) <= CHUNK_CHARS // 2:
                raise
            mid = text.rfind(" ", len(text) // 2)
            if mid <= 0:
                mid = len(text) // 2
            return (self._part([text[:mid]]) + self._part([text[mid:]])) / 2.0

    def note_vector(self, text: str) -> np.ndarray:
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("empty note")
        vecs = np.vstack([self._part(chunks[i:i + BATCH]) for i in range(0, len(chunks), BATCH)])
        vecs = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
        mean = vecs.mean(axis=0)
        return (mean / max(float(np.linalg.norm(mean)), 1e-9)).astype(np.float32)

# ---------------------------------------------------------------- cache

class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, tuple[np.ndarray, int, int, str]] = {}
        if path.exists():
            with np.load(path, allow_pickle=False) as d:
                for i, nid in enumerate(d["ids"]):
                    self.entries[str(nid)] = (d["vecs"][i], int(d["mtime_ns"][i]),
                                              int(d["size"][i]), str(d["sha256"][i]))

    def lookup(self, nid: str, st: os.stat_result, path: Path) -> bool:
        e = self.entries.get(nid)
        if e is None:
            return False
        if e[1] == st.st_mtime_ns and e[2] == st.st_size:
            return True
        # mtime/size changed -> compare content (e.g. only touched)
        if sha256(path) == e[3]:
            self.entries[nid] = (e[0], st.st_mtime_ns, st.st_size, e[3])
            return True
        return False

    def put(self, nid: str, vec: np.ndarray, st: os.stat_result, digest: str) -> None:
        self.entries[nid] = (vec, st.st_mtime_ns, st.st_size, digest)

    def save(self) -> None:
        ids = sorted(self.entries)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "wb") as fh:
            np.savez(fh,
                     ids=np.array(ids, dtype=str),
                     vecs=(np.vstack([self.entries[i][0] for i in ids]) if ids
                           else np.zeros((0, EMBED_DIM), np.float32)),
                     mtime_ns=np.array([self.entries[i][1] for i in ids], dtype=np.int64),
                     size=np.array([self.entries[i][2] for i in ids], dtype=np.int64),
                     sha256=np.array([self.entries[i][3] for i in ids], dtype=str))
        os.replace(tmp, self.path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

# ---------------------------------------------------------------- progress

def fmt_s(s: float) -> str:
    s = int(round(s))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Progress:
    def __init__(self, total: int, log: Log):
        self.total = total
        self.log = log
        self.t0 = time.monotonic()
        self.times: collections.deque[float] = collections.deque(maxlen=ETA_WINDOW + 1)
        self.times.append(self.t0)
        self.done = 0
        self.last_print = 0.0
        self.predictions: list[tuple[int, float, float]] = []   # (done, t, eta_s)

    def step(self) -> None:
        self.done += 1
        now = time.monotonic()
        self.times.append(now)
        if (self.done % PROGRESS_EVERY_N == 0 or self.done == self.total
                or now - self.last_print >= PROGRESS_EVERY_S):
            self.last_print = now
            self.report(now)

    def report(self, now: float) -> None:
        elapsed = now - self.t0
        rate_all = self.done / elapsed if elapsed > 0 else 0.0
        head = (f"PROGRESS {self.done}/{self.total} | {rate_all:.2f} notes/s | "
                f"elapsed {fmt_s(elapsed)}")
        if self.done < min(ETA_MIN, self.total):
            self.log(head + " | ETA: collecting samples")
            return
        span = self.times[-1] - self.times[0]
        rate = (len(self.times) - 1) / span if span > 0 else rate_all
        eta = (self.total - self.done) / rate if rate > 0 else 0.0
        self.predictions.append((self.done, now, eta))
        self.log(head + f" | moving {rate:.2f}/s (last {len(self.times) - 1}) | ETA {fmt_s(eta)}")

    def eta_check(self) -> None:
        """Compare the first ETA prediction (with work remaining) to the real remaining time."""
        end = time.monotonic()
        for done, t, eta in self.predictions:
            if done < self.total:
                real = end - t
                dev = abs(eta - real) / real * 100 if real > 0 else 0.0
                self.log(f"ETA CHECK: prediction at {done}/{self.total}: {eta:.1f} s remaining, "
                         f"actual {real:.1f} s, deviation {dev:.0f} %")
                return

# ---------------------------------------------------------------- main run

def find_notes(vault: Path) -> list[Path]:
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        out.extend(Path(root) / f for f in sorted(files) if f.endswith(".md"))
    return out


def main() -> int:
    global SERVICE, EMBED_URL, MANAGE_SERVICE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vault", type=Path, help="path to the Markdown vault")
    ap.add_argument("--limit", type=int, default=0, help="only the first N notes (sorted)")
    ap.add_argument("--only", default=None,
                    help="file with relative paths or stems (one per line) — embed only these")
    ap.add_argument("--keep-alive", action="store_true", help="do not stop the service at the end")
    ap.add_argument("--embed-url", default=EMBED_URL, help="embedding server (default: %(default)s)")
    ap.add_argument("--service", default=SERVICE, help="systemd --user unit to manage (default: %(default)s)")
    ap.add_argument("--no-manage-service", action="store_true",
                    help="never call systemctl; expect the server to be running already")
    args = ap.parse_args()
    EMBED_URL = args.embed_url.rstrip("/")
    SERVICE = args.service
    MANAGE_SERVICE = not args.no_manage_service

    vault = args.vault.resolve()
    if not vault.is_dir():
        print(f"not a directory: {vault}", file=sys.stderr)
        return 2
    cache_dir = vault / ".embed-cache"
    cache_dir.mkdir(exist_ok=True)
    log = Log(cache_dir / "embed_batch.log")
    t_start = time.monotonic()

    notes = find_notes(vault)
    if args.only:
        raw = [l.strip() for l in Path(args.only).read_text(encoding="utf-8").splitlines()]
        want_path = {l for l in raw if l}
        want_stem = {l.rsplit("/", 1)[-1][:-3] if l.endswith(".md") else l.rsplit("/", 1)[-1] for l in want_path}
        notes = [p for p in notes
                 if p.relative_to(vault).as_posix() in want_path or p.stem in want_stem]
        print(f"sample filter active: {len(notes)} notes from {len(want_path)} lines")
    if args.limit > 0:
        notes = notes[:args.limit]
    cache = Cache(cache_dir / "vectors.npz")
    todo, hits = [], 0
    for p in notes:
        nid = p.relative_to(vault).as_posix()
        if cache.lookup(nid, p.stat(), p):
            hits += 1
        else:
            todo.append((nid, p))
    log(f"START vault={vault} selected={len(notes)} from_cache={hits} "
        f"to_embed={len(todo)} limit={args.limit or '-'}")

    counter: collections.Counter = collections.Counter()
    svc = Service(log, counter)
    failed: list[tuple[str, str]] = []
    new = 0
    interrupted = False
    if todo:
        if not svc.start():
            log("WARNING: service not ready at start, watchdog keeps trying")
        emb = Embedder(svc, counter)
        prog = Progress(len(todo), log)
        try:
            for nid, p in todo:
                try:
                    st = p.stat()
                    data = p.read_bytes()
                    vec = emb.note_vector(data.decode("utf-8", errors="replace"))
                    cache.put(nid, vec, st, hashlib.sha256(data).hexdigest())
                    new += 1
                    if new % SAVE_EVERY == 0:
                        cache.save()
                except Exception as e:                      # noqa: BLE001
                    failed.append((nid, str(e)[:200]))
                    log(f"ERROR {nid}: {str(e)[:200]}")
                prog.step()
        except KeyboardInterrupt:
            interrupted = True
            log("ABORT via Ctrl+C — saving cache")
        finally:
            cache.save()
            svc.shutdown(args.keep_alive)
        prog.eta_check()
    else:
        if hits:
            cache.save()      # persist mtimes confirmed via sha256
        log("nothing to embed — service not started")

    total_s = time.monotonic() - t_start
    http = counter["embedding"] + counter["health"]
    log(f"END newly embedded: {new} | from cache: {hits} | errors: {len(failed)} | "
        f"HTTP requests: {http} (embedding {counter['embedding']}, health {counter['health']}) | "
        f"watchdog restarts: {svc.restarts} | total {total_s:.1f} s | "
        f"cache entries: {len(cache.entries)}")
    for nid, err in failed:
        log(f"  failed: {nid} — {err}")
    log.close()
    if interrupted:
        return 130
    return 0 if len(failed) < 0.01 * max(len(notes), 1) else 1


if __name__ == "__main__":
    sys.exit(main())
