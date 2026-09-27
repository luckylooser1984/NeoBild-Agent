#!/usr/bin/env python3
"""mhtml_to_markdown.py — turn an MHTML page capture into readable Markdown.

Purpose:
    Mobile browsers and chat apps often "save as" a page that is really an MHTML
    capture (HTML wrapped in MIME parts), sometimes even with a .md extension.
    Raw HTML is useless in a Markdown knowledge base, so this script cuts out the
    main text/html part, extracts the visible text (dropping script/style/SVG and
    base64 noise) and - if that yields too little - falls back to pandoc (GFM).

Usage:
    mhtml_to_markdown.py <input.mhtml|.md> <output.md> ["Title"] ["Source"]
    The original file is never modified. pandoc is optional (fallback only).

Author: Lukas Weißmann
License: MIT
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path


def html_part(text: str) -> str:
    """Return the first text/html part of an MHTML file."""
    parts = re.split(r"-{4,}MultipartBoundary[^\r\n]*-{2,}", text)
    for part in parts:
        head, sep, body = part.partition("\r\n\r\n")
        if not sep and part.count("\n\n"):
            head, _, body = part.partition("\n\n")
        if "text/html" in head.lower() and "<" in body:
            return body
    # Fallback: everything from the first <!DOCTYPE or <html
    m = re.search(r"<(!DOCTYPE|html)", text, re.IGNORECASE)
    return text[m.start():] if m else text


def text_from_html(html: str) -> str:
    """Extract visible text from HTML - without script/style/SVG/base64 ballast."""
    from html.parser import HTMLParser

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.chunks: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)
            if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "pre"):
                self.chunks.append("\n")

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "tr", "pre"):
                self.chunks.append("\n")

        def handle_data(self, data):
            if any(t in ("script", "style", "svg", "noscript") for t in self.stack):
                return
            if data.strip():
                self.chunks.append(data)

    c = Collector()
    c.feed(html)
    raw = "".join(c.chunks)
    raw = re.sub(r"data:[^\s)\"']{80,}", "", raw)
    clean: list[str] = []
    for line in (z.strip() for z in raw.splitlines()):
        if not line:
            if clean and clean[-1] != "":
                clean.append("")
            continue
        if len(line) > 40 and re.fullmatch(r"[A-Za-z0-9+/=._-]+", line):
            continue  # base64 leftovers
        clean.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(clean)).strip()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    title = sys.argv[3] if len(sys.argv) > 3 else src.stem
    origin = sys.argv[4] if len(sys.argv) > 4 else "unknown"

    raw = src.read_text(encoding="utf-8", errors="replace")
    html = html_part(raw)
    md = text_from_html(html)
    pandoc = shutil.which("pandoc")
    if len(md) < 500 and pandoc:
        res = subprocess.run([pandoc, "-f", "html", "-t", "gfm", "--wrap=none"],
                             input=html, capture_output=True, text=True)
        md = res.stdout if res.returncode == 0 else md
    # Normalise whitespace but keep paragraphs.
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if not md:
        print("WARNING: no text output produced")
        return 1

    header = (
        f"# {title}\n\n"
        f"- Source: {origin}\n"
        f"- Original file: {src.name} ({src.stat().st_size} bytes, MHTML page capture)\n"
        f"- Converted with mhtml_to_markdown.py, content unchanged\n\n"
        "---\n\n"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(header + md + "\n", encoding="utf-8")
    print(f"written: {dst} ({dst.stat().st_size} bytes, {md.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
