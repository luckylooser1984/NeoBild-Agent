#!/usr/bin/env python3
"""pcap_extract.py — extract the meaningful parts of packet captures, memory-friendly.

Rationale: raw capture files barely compress (mostly encrypted TLS traffic,
ratio ~1.1). Instead of archiving gigabytes of raw data, extract the parts
that carry information, archive those permanently, and only then consider
removing the raw files.

Extracted per capture:
  - DNS queries          (which names were resolved)
  - TLS server names     (SNI — which destinations were contacted via TLS)
  - certificate strings  (issuer/subject strings from server certificates)
  - connections          (IP pairs with packet counts)
  - HTTP hosts           (if unencrypted)
  - summary              (time span, packet/byte counts, protocol distribution)

Output: JSONL (one line per capture) on stdout or into --output.

Usage:
    pcap_extract.py <pcap> [<pcap> ...]
    pcap_extract.py --dir <directory>                  # all *.pcap in it
    pcap_extract.py --dir <directory> --output out.jsonl

Requires tshark and capinfos (Wireshark CLI tools). Read-only: never
modifies a capture file.

Author: Lukas Weißmann
License: MIT
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _tshark_stream(pcap: str, fields: list[str], display_filter: str = "",
                   timeout: int = 1800) -> list[list[str]]:
    """One tshark pass, memory-friendly (two-pass mode).

    IMPORTANT (measured): WITHOUT `-2` tshark keeps the whole file in RAM —
    a 3.5 GB capture needed 4.2 GB and got OOM-killed. With `-2` (two-pass)
    memory stays small and output arrives line by line. Output is also
    processed line by line, never buffered as a whole.
    """
    cmd = ["tshark", "-2", "-r", pcap, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    if display_filter:
        cmd += ["-Y", display_filter]
    rows: list[list[str]] = []
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, bufsize=1) as p:
            for line in p.stdout:  # type: ignore[union-attr]
                parts = line.rstrip("\n").split("\t")
                if any(t.strip() for t in parts):
                    rows.append(parts)
            p.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return rows
    return rows


def tshark_field(pcap: str, field: str, limit: int = 0) -> list[str]:
    """Extracts one tshark field, de-duplicated, optionally limited."""
    values: list[str] = []
    seen: set[str] = set()
    for parts in _tshark_stream(pcap, [field], f'{field} != ""'):
        for v in parts:
            v = v.strip()
            if not v or v in seen:
                continue
            seen.add(v)
            values.append(v)
            if limit and len(values) >= limit:
                return values
    return values


def capinfos_summary(pcap: str) -> dict:
    try:
        out = subprocess.run(
            ["capinfos", "-M", pcap], capture_output=True, text=True, timeout=300
        ).stdout
    except subprocess.TimeoutExpired:
        return {}
    vals: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            vals[k.strip()] = v.strip()
    return {
        "packets": vals.get("Number of packets"),
        "bytes": vals.get("File size"),
        "first_packet": vals.get("First packet time"),
        "last_packet": vals.get("Last packet time"),
        "duration_s": vals.get("Capture duration"),
    }


def protocol_distribution(pcap: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for parts in _tshark_stream(pcap, ["frame.protocols"], timeout=600):
        for p in parts[0].strip().split(":"):
            if p:
                counts[p] = counts.get(p, 0) + 1
    # most informative first
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:15])


def analyse(pcap: str) -> dict:
    path = Path(pcap)
    result: dict = {
        "file": path.name,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "analysed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    result["summary"] = capinfos_summary(pcap)

    print("  DNS names ...", file=sys.stderr, flush=True)
    result["dns_queries"] = tshark_field(pcap, "dns.qry.name", limit=2000)

    print("  TLS SNI ...", file=sys.stderr, flush=True)
    result["tls_sni"] = tshark_field(pcap, "tls.handshake.extensions_server_name", limit=2000)

    print("  HTTP hosts ...", file=sys.stderr, flush=True)
    result["http_hosts"] = tshark_field(pcap, "http.host", limit=1000)

    print("  certificates ...", file=sys.stderr, flush=True)
    certs: list[dict] = []
    seen_c: set[str] = set()
    for parts in _tshark_stream(
        pcap, ["x509sat.uTF8String", "x509sat.printableString"],
        "tls.handshake.type == 11"
    ):
        for part in parts:
            part = part.strip()
            if part and len(part) > 3 and part not in seen_c:
                seen_c.add(part)
                certs.append({"value": part})
    result["certificates"] = certs[:500]

    print("  connections ...", file=sys.stderr, flush=True)
    pairs: dict[str, int] = {}
    for parts in _tshark_stream(pcap, ["ip.src", "ip.dst"], "ip.src && ip.dst"):
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            key = f"{parts[0].strip()} -> {parts[1].strip()}"
            pairs[key] = pairs.get(key, 0) + 1
    result["connections"] = [f"{k}  ({v} packets)"
                             for k, v in sorted(pairs.items(), key=lambda kv: -kv[1])[:300]]

    print("  protocol distribution ...", file=sys.stderr, flush=True)
    result["protocols"] = protocol_distribution(pcap)

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="PCAP extraction (read-only)")
    ap.add_argument("files", nargs="*", help="PCAP files")
    ap.add_argument("--dir", help="directory containing .pcap files")
    ap.add_argument("--output", help="JSONL target file (default: stdout)")
    args = ap.parse_args()

    files: list[str] = list(args.files)
    if args.dir:
        files += sorted(str(p) for p in Path(args.dir).glob("*.pcap"))
    if not files:
        print("No files given.", file=sys.stderr)
        return 2

    target = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        for i, f in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {os.path.basename(f)}", file=sys.stderr, flush=True)
            try:
                data = analyse(f)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                data = {"file": os.path.basename(f), "path": f, "error": str(e)}
            target.write(json.dumps(data, ensure_ascii=False) + "\n")
            target.flush()
    finally:
        if args.output:
            target.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
