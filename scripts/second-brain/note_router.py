#!/usr/bin/env python3
"""note_router.py — rule-based router that files short notes/prompts into topic folders.

Purpose:
    A tiny, deterministic "inbox router" for a Markdown second brain. A note or
    prompt is classified by keyword rules (first matching rule wins, order =
    priority) and written to <root>/<category>/YYYY-MM-DD-HHMM-<slug>.md.
    An index.md table is kept up to date. No LLM, no cloud, stdlib only.

    The module is also imported by phone_notes_pull.py and sort_inbox.py so
    that every ingestion path uses exactly the same file naming.

Usage:
    note_router.py init                      create category folders + README
    note_router.py route "<text>" [category] file a new note, print its path
    note_router.py list                      folder table with note counts
    note_router.py reindex                   rebuild index.md from folder contents

Configuration:
    NOTE_ROUTER_ROOT   target root folder (default: ./notes)
    NOTE_ROUTER_RULES  optional JSON file: [["slug", ["kw1", "kw2"]], ...]
                       replaces the built-in rules below

Author: Lukas Weißmann
License: MIT
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("NOTE_ROUTER_ROOT", "notes")).expanduser()
STATE = ROOT / ".router" / "routing.json"

# slug -> keywords (first match wins; list order = priority).
# Keywords are matched as lowercase substrings, English and German mixed on purpose.
DEFAULT_RULES = [
    ("website", ["website", "webseite", "hugo", "blog", "article", "artikel", "deploy",
                 "seo", "traffic", "sitemap", "analytics"]),
    ("local-ai", ["sovereign", "llm", "llama", "gguf", "inference", "inferenz", "vllm",
                  "rag", "agent", "model", "modell", "quant"]),
    ("hardware", ["thinkpad", "rapl", "thermal", "thermik", "fan", "cpu", "ram", "zram",
                  "swap", "battery", "akku", "ssd", "hardware", "tuning"]),
    ("linux-system", ["linux", "arch", "plasma", "kde", "systemd", "cron", "package",
                      "flatpak", "service", "log", "shell", "script", "skript"]),
    ("android", ["android", "adb", "phone", "handy", "smartphone", "termux", "scrcpy",
                 "apk"]),
    ("network", ["network", "netzwerk", "wlan", "wifi", "vpn", "dns", "firewall",
                 "pcap", "tcp", "port", "router", "proxy"]),
    ("security-privacy", ["security", "sicherheit", "privacy", "datenschutz", "crypto",
                          "encrypt", "verschluessel", "password", "passwort", "tor",
                          "tracker", "secret"]),
    ("audio", ["music", "musik", "song", "audio", "voice", "tts", "stt", "whisper"]),
    ("todos", ["todo", "to-do", "task", "aufgabe", "checklist", "checkliste",
               "reminder", "erinnerung", "backlog"]),
    ("research", ["research", "recherche", "study", "studie", "paper", "market",
                  "price", "compare", "vergleich", "overview", "ueberblick"]),
]


def load_rules():
    path = os.environ.get("NOTE_ROUTER_RULES")
    if path and Path(path).is_file():
        return [(slug, list(kws)) for slug, kws in json.loads(Path(path).read_text(encoding="utf-8"))]
    return DEFAULT_RULES


RULES = load_rules()

# Filler words dropped from slugs (German + English).
STOP = {"bitte", "mal", "kurz", "denn", "doch", "dann", "noch", "alles", "etwas",
        "einen", "einer", "einem", "eines", "einfach", "nach", "aber", "kannst",
        "mach", "mache", "machen", "will", "muss", "soll", "fuer", "für",
        "und", "oder", "der", "die", "das", "den", "dem", "ein", "eine", "ist",
        "von", "vom", "mit", "auf", "aus", "bei", "zum", "zur", "ich", "du", "es",
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "please", "is"}

CATEGORIES = [s for s, _ in RULES] + ["unsorted"]


def now():
    return time.strftime("%Y-%m-%d %H:%M")


README_TEXT = ("# Notes\n\nTarget of note_router.py. Every note is filed as a short "
               "Markdown file in the matching category folder.\n\n## Categories\n\n"
               + "\n".join(f"- `{s}/`" for s in CATEGORIES)
               + "\n\n## File name\n\n`YYYY-MM-DD-HHMM-<slug>.md`\n")


def init():
    ROOT.mkdir(parents=True, exist_ok=True)
    for s in CATEGORIES:
        (ROOT / s).mkdir(exist_ok=True)
    (ROOT / "README.md").write_text(README_TEXT, encoding="utf-8")
    (ROOT / ".router").mkdir(exist_ok=True)
    print("init ok ->", ROOT)
    for s in CATEGORIES:
        print("  ", s)


def classify(text):
    """Return (category_slug, matched_keyword); ('unsorted', '') if nothing matches."""
    low = text.lower()
    for slug, kws in RULES:
        for k in kws:
            if k in low:
                return slug, k
    return "unsorted", ""


def slugify(text, maxlen=48):
    s = text.lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    words = [w for w in s.split("-") if w and w not in STOP]
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > maxlen:
            break
        out = f"{out}-{w}" if out else w
    return out or "note"


def title_of(text):
    t = " ".join(text.split())
    t = t.split(". ")[0]
    return t[:80]


# --- Shared naming (YYYY-MM-DD-HHMM-<slug>.md) -------------------------------
# Single source of truth for file names below ROOT. Used by route() AND by the
# sync paths (phone_notes_pull.py, sort_inbox.py) so all of them name files
# identically.

def stamp(epoch=None):
    """Timestamp YYYY-MM-DD-HHMM, from a source mtime (epoch) or now."""
    return time.strftime("%Y-%m-%d-%H%M",
                         time.localtime(epoch) if epoch is not None else time.localtime())


def target_name(raw_title, epoch=None, maxlen=48):
    """File name YYYY-MM-DD-HHMM-<slug>.md from a raw title (no side effects)."""
    return f"{stamp(epoch)}-{slugify(raw_title, maxlen)}.md"


def target_path(dirpath, raw_title, epoch=None, maxlen=48):
    """Target path YYYY-MM-DD-HHMM-<slug>.md; creates the folder.

    Collisions within the same minute are resolved with -2, -3, ...
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    stem = target_name(raw_title, epoch, maxlen)[:-len(".md")]
    path = dirpath / f"{stem}.md"
    n = 2
    while path.exists():
        path = dirpath / f"{stem}-{n}.md"
        n += 1
    return path


def route(prompt, typ=None):
    if not ROOT.exists():
        init()
    slug, kw = classify(prompt)
    if typ:
        slug = typ if typ in CATEGORIES else slug
    path = target_path(ROOT / slug, prompt)
    body = (f"# {title_of(prompt)}\n\n"
            f"Category: {slug} | Created: {now()} | Router: rule-based\n\n"
            f"## Prompt\n{prompt.strip()}\n\n"
            f"## Task\n- [ ] Clarify and do\n")
    path.write_text(body, encoding="utf-8")
    # Append an index line.
    idx = ROOT / "index.md"
    if not idx.exists():
        idx.write_text("# Notes - index\n\n| Time | Category | Note |\n|---|---|---|\n",
                       encoding="utf-8")
    with idx.open("a", encoding="utf-8") as f:
        f.write(f"| {now()} | {slug} | [{path.stem}]({slug}/{path.name}) |\n")
    STATE.parent.mkdir(exist_ok=True)
    try:
        hist = json.loads(STATE.read_text()) if STATE.exists() else []
    except Exception:
        hist = []
    hist.append({"t": now(), "type": slug, "kw": kw, "file": str(path)})
    STATE.write_text(json.dumps(hist[-500:], ensure_ascii=False), encoding="utf-8")
    print(str(path))
    print("category:", slug, "| keyword:", kw or "-")


def list_all():
    if not ROOT.exists():
        print("no notes folder yet:", ROOT)
        return
    print(f"ROOT {ROOT}")
    for s in CATEGORIES:
        d = ROOT / s
        n = len(list(d.glob("*.md"))) if d.exists() else 0
        print(f"  {n:3d}  {s}/")


NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4})-")


def reindex():
    """Rebuild index.md from what actually lies in the category folders (not only
    what went through route()) - repairs files created by other paths."""
    if not ROOT.exists():
        print("no notes folder yet:", ROOT)
        return
    rows = []
    for s in CATEGORIES:
        d = ROOT / s
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            m = NAME_RE.match(f.name)
            if m:
                y, mo, dd, hhmm = m.group(1).split("-")
                when = f"{y}-{mo}-{dd} {hhmm[:2]}:{hhmm[2:]}"
            else:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
            rows.append((when, s, f.stem, f"{s}/{f.name}"))
    rows.sort(key=lambda r: r[0])
    lines = ["# Notes - index\n\n| Time | Category | Note |\n|---|---|---|\n"]
    for when, s, stem, rel in rows:
        lines.append(f"| {when} | {s} | [{stem}]({rel}) |\n")
    (ROOT / "index.md").write_text("".join(lines), encoding="utf-8")
    print(f"reindex ok -> {len(rows)} entries")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
    elif a[0] == "init":
        init()
    elif a[0] == "route" and len(a) > 1:
        route(a[1], a[2] if len(a) > 2 else None)
    elif a[0] == "list":
        list_all()
    elif a[0] == "reindex":
        reindex()
    else:
        print(__doc__)
