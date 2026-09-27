#!/usr/bin/env python3
"""cve_daily.py — list CVEs published in the last 24h (NVD API 2.0) as compact text.

One line per CVE:  CVE-ID | CVSS <score> | <published date> | <description>

Originally written to be injected into an agent prompt, which then curates a
daily security briefing — but the output is also fine for humans or grep.
Pair it with cve_watch.py for stack filtering and de-duplication.

Usage:
    python3 cve_daily.py [--hours 24]

Network: calls the public NVD REST API (services.nvd.nist.gov), no API key.
Exit 1 if NVD is unreachable after retries (so a scheduler marks the run as
failed instead of producing an empty briefing).

Author: Lukas Weißmann
License: MIT
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "local-cve-monitor/1.0 (cron)"


def fetch(url: str, retries: int = 3) -> dict:
    """GET with retry/backoff — NVD is not always reachable for early-morning cron runs."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(5 * attempt)  # 5s, 10s backoff
    raise last


def main() -> int:
    ap = argparse.ArgumentParser(description="CVEs published in the last N hours (NVD).")
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000")  # noqa: E731
    base = "https://services.nvd.nist.gov/rest/json/cves/2.0?"
    params = {
        "pubStartDate": fmt(start),
        "pubEndDate": fmt(end),
        "resultsPerPage": 200,
        "startIndex": 0,
    }
    try:
        data = fetch(base + urllib.parse.urlencode(params))
    except Exception as e:
        print(f"ERROR fetching NVD (3 attempts): {e}")
        return 1  # non-zero -> run is marked FAILED instead of an empty briefing
    total = data.get("totalResults", 0)
    vulns = list(data.get("vulnerabilities", []))
    while len(vulns) < total:
        params["startIndex"] = len(vulns)
        try:
            more = fetch(base + urllib.parse.urlencode(params))
            vulns.extend(more.get("vulnerabilities", []))
        except Exception as e:
            print(f"WARNING pagination aborted at {len(vulns)}/{total}: {e}")
            break
    print(f"# NVD CVEs published {start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC: {total} total, {len(vulns)} loaded")
    print()
    for v in vulns:
        c = v.get("cve", {})
        cid = c.get("id", "?")
        desc = ""
        for d in c.get("descriptions", []):
            if d.get("lang") == "en":
                desc = " ".join(d.get("value", "").split())
                break
        score = ""
        for k in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if k in c.get("metrics", {}) and c["metrics"][k]:
                try:
                    score = str(c["metrics"][k][0].get("cvssData", {}).get("baseScore", ""))
                except Exception:
                    pass
                break
        pub = c.get("published", "")[:10]
        print(f"{cid} | CVSS {score} | {pub} | {desc[:220]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
