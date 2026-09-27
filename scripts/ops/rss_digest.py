#!/usr/bin/env python3
"""rss_digest.py — collect the last N days of a set of RSS/Atom feeds as Markdown.

Default feeds cover privacy, Linux and freedom-tech news. The output (title,
link, date, short teaser per item) is designed to be injected into an agent
prompt that writes a weekly briefing — or simply read as is.

Usage:
    python3 rss_digest.py [--days 7] [--feeds feeds.txt]
      feeds.txt: one "Name|https://url" per line

Network: fetches the configured feeds. stdlib only.

Author: Lukas Weißmann
License: MIT
"""
import argparse
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

FEEDS = [
    ("Tor Blog", "https://blog.torproject.org/rss.xml"),
    ("EFF Deeplinks (Privacy)", "https://www.eff.org/rss/updates.xml"),
    ("Phoronix (Linux)", "https://www.phoronix.com/rss.php"),
    ("LWN (Linux/Kernel)", "https://lwn.net/headlines/rss"),
    ("Monero (getmonero.org)", "https://www.getmonero.org/feed.xml"),
    ("Tails (Privacy OS)", "https://tails.net/news/index.en.rss"),
    ("Qubes OS (Privacy OS)", "https://www.qubes-os.org/feed.xml"),
    ("heise Security", "https://www.heise.de/security/rss/news.rdf"),
]

ATOM = "{http://www.w3.org/2005/Atom}"
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "local-rss-digest/1.0 (cron)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse_date(s: str | None):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        # Atom uses ISO 8601
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def load_feeds(path: str | None):
    if not path:
        return FEEDS
    feeds = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                name, url = line.split("|", 1)
                feeds.append((name.strip(), url.strip()))
    return feeds


def main() -> int:
    ap = argparse.ArgumentParser(description="RSS/Atom digest as Markdown.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--feeds", help='file with "Name|URL" lines (replaces the default list)')
    ap.add_argument("--per-feed", type=int, default=12, help="max items per feed")
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    for name, url in load_feeds(args.feeds):
        print(f"## {name}")
        try:
            root = ET.fromstring(fetch(url))
            items = []
            for item in root.iter("item"):  # RSS 2.0
                items.append((
                    parse_date(item.findtext("pubDate")),
                    (item.findtext("title") or "").strip(),
                    (item.findtext("link") or "").strip(),
                    (item.findtext("description") or "").strip(),
                ))
            if not items:  # RDF (RSS 1.0) puts items in its own namespace
                for item in root.iter("{http://purl.org/rss/1.0/}item"):
                    ns = "{http://purl.org/rss/1.0/}"
                    items.append((
                        parse_date(item.findtext("{http://purl.org/dc/elements/1.1/}date")),
                        (item.findtext(ns + "title") or "").strip(),
                        (item.findtext(ns + "link") or "").strip(),
                        (item.findtext(ns + "description") or "").strip(),
                    ))
            if not items:  # Atom
                for e in root.iter(ATOM + "entry"):
                    link_el = e.find(ATOM + "link")
                    items.append((
                        parse_date(e.findtext(ATOM + "updated")),
                        (e.findtext(ATOM + "title") or "").strip(),
                        (link_el.get("href") if link_el is not None else "") or "",
                        (e.findtext(ATOM + "summary") or "").strip(),
                    ))
            items.sort(key=lambda x: x[0] or MIN_DT, reverse=True)
            recent = [i for i in items if (i[0] or MIN_DT) >= cutoff]
            if not recent:
                print(f"  (no new items in the last {args.days} days)")
            for pub, title, link, desc in recent[:args.per_feed]:
                d = pub.strftime("%Y-%m-%d") if pub else "?"
                teaser = " ".join(desc.split())[:140]
                print(f"- [{title}]({link}) ({d}): {teaser}")
        except Exception as e:
            print(f"  ERROR: {e}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
