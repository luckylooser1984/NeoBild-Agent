#!/usr/bin/env python3
"""
change_review.py — automatic, local change review for git repositories (watchdog pattern).

Checks new commits (since the last reviewed HEAD, tracked in a state file)
along four dimensions:

  1. Functionality   : syntax check (py_compile) of changed .py files,
                       health check of a local test port (if configured),
                       pytest if available
  2. Security        : secret files in the commit, secret patterns in added
                       lines, dangerous calls (eval/exec/os.system/shell=True/
                       pickle), SQL string interpolation, DEAD PATH REFERENCES
                       in changed scripts, committed __pycache__/*.pyc
  3. Maintainability : TODO/FIXME/HACK in new code, very large diffs,
                       extremely short commit messages
  4. Documentation   : code change without README/CHANGELOG/docs update

Behaviour (designed for a quiet cron job):
  - no new commits          -> empty stdout, exit 0 (silent)
  - findings                -> findings on stdout, exit 0
  - script/system error     -> message on stderr, exit 1

Sovereign & local: no cloud call, never writes to the repos, never prints
secret values (they are masked).

Usage:
    python3 change_review.py --repo ~/code/myproject
    python3 change_review.py --repo ~/code/a --repo ~/code/b --test-port 8000
    python3 change_review.py --config repos.json
      repos.json: [{"name": "myproject", "path": "/path/to/repo",
                    "test_port": 8000, "content_dirs": ["docs", "images"]}]

State: $CHANGE_REVIEW_STATE or ~/.local/state/change_review_state.json
       (last reviewed HEAD per repo)

Author: Lukas Weißmann
License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Extensions treated as code (security scan, docs requirement, change counting)
CODE_EXTENSIONS = {
    ".py", ".sh", ".bash", ".js", ".ts", ".jsx", ".tsx", ".php", ".go", ".rs",
    ".c", ".h", ".cpp", ".java", ".rb", ".pl", ".lua", ".sql",
}

STATE_FILE = Path(os.environ.get(
    "CHANGE_REVIEW_STATE",
    Path.home() / ".local" / "state" / "change_review_state.json",
))

# Security-critical file names that must NEVER end up in a commit
SECRET_FILENAMES = re.compile(
    r"(^|/)(\.env(\.|$)|secrets\.env|\.netrc|id_rsa|id_ed25519|.*\.pem|.*\.key$|"
    r"credentials\.json|token(s)?\.json|\.htpasswd|auth\.json|service-account.*\.json)",
    re.IGNORECASE,
)

# Secret patterns in added lines (literal assignments, well-known token prefixes)
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|auth)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"\b(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,})\b"),
]

# Dangerous calls in new code
DANGEROUS_PATTERNS = [
    (re.compile(r"os\.system\s*\("), "os.system() - shell call"),
    (re.compile(r"subprocess\.[A-Za-z]+\([^)]*shell\s*=\s*True"), "subprocess shell=True"),
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bexec\s*\("), "exec()"),
    (re.compile(r"pickle\.(loads?|load)\s*\("), "pickle.load - unsafe deserialisation"),
    (re.compile(r"yaml\.load\s*\([^)]*Loader\s*=\s*(?!SafeLoader|BaseLoader)"), "yaml.load without SafeLoader"),
    (re.compile(r"execute\s*\(\s*f['\"]"), "SQL string interpolation (f-string)"),
    (re.compile(r"cursor\.execute\s*\([^)]*\.format\s*\("), "SQL string interpolation (.format)"),
]

TODO_PATTERNS = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

MAX_ADDED_LINES_WARN = 500
MIN_COMMIT_MSG_LEN = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mask_secret(line: str, max_len: int = 90) -> str:
    """Defensively shortens lines containing secret values (never print full credentials)."""
    cleaned = line.strip()
    for pat in SECRET_PATTERNS:
        m = pat.search(cleaned)
        if m and m.group(0) != cleaned:
            # Only cut the value side: "key = sk-XXX..." -> "key = sk-…[value masked]"
            head = cleaned[: m.start() + len(m.group(1)) + 4] if m.lastindex else cleaned[:40]
            return f"{head}…[value masked]"
    return cleaned[:max_len]


def sh(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """Subprocess without a shell — list form, no injection."""
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)  # atomic


def is_git_repo(repo_path: Path) -> bool:
    r = sh(["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0 and r.stdout.strip() == "true"


def is_code_file(f: str, cfg: dict) -> bool:
    """True if the file counts as code (extension or explicitly listed wrapper)."""
    if f in cfg.get("always_code", []):
        return True
    return Path(f).suffix.lower() in CODE_EXTENSIONS


def is_content_file(f: str, cfg: dict) -> bool:
    """True if the file lives in a content/asset directory."""
    return any(f.startswith(d + "/") or f == d for d in cfg.get("content_dirs", []))


def extract_local_paths(text: str) -> set[str]:
    """Extracts absolute home paths (/home/... or ~/...) from text to check that they exist."""
    paths = set()
    for m in re.finditer(r"((?:/home|~)/[A-Za-z0-9_ .\-/]+)", text):
        raw = m.group(1).strip().rstrip('"\'`,;)')
        # Only paths with at least 2 segments that look like dirs/files
        if raw.count("/") >= 2 and "/ " not in raw:
            paths.add(raw)
    return paths


# ---------------------------------------------------------------------------
# Checks (each returns a list of (level, message))
# ---------------------------------------------------------------------------

def check_repo(cfg: dict) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    repo_path = Path(cfg["path"]).expanduser()
    name = cfg["name"]

    if not repo_path.exists() or not repo_path.is_dir():
        findings.append(("CRITICAL", f"[{name}] repo path does not exist: {repo_path}"))
        return findings
    if not is_git_repo(repo_path):
        findings.append(("CRITICAL", f"[{name}] not a git repo: {repo_path}"))
        return findings

    # --- new commits since last state ----------------------------------------
    state = load_state()
    last_head = state.get("repos", {}).get(name)

    head_res = sh(["git", "-C", str(repo_path), "rev-parse", "HEAD"])
    if head_res.returncode != 0:
        findings.append(("ERROR", f"[{name}] HEAD not readable: {head_res.stderr.strip()}"))
        return findings
    head = head_res.stdout.strip()

    if last_head == head:
        return []  # no new commits -> silent

    if last_head:
        merge_base = sh(["git", "-C", str(repo_path), "merge-base", "--is-ancestor", last_head, "HEAD"])
        if merge_base.returncode != 0:
            findings.append((
                "WARN",
                f"[{name}] history was rewritten or force-pushed (last state {last_head[:8]} "
                f"not in history) — review base reset, checking the last 20 commits.",
            ))
            log = sh(["git", "-C", str(repo_path), "log", "--oneline", "-20"])
        else:
            log = sh(["git", "-C", str(repo_path), "log", "--oneline", f"{last_head}..HEAD"])
        diff_files = sh(["git", "-C", str(repo_path), "diff", "--name-only", f"{last_head}..HEAD"])
        diff_unified = sh(["git", "-C", str(repo_path), "diff", "-U0", f"{last_head}..HEAD"])
    else:
        # First run: last 10 commits as window (base = oldest available,
        # so repos with <10 commits diff correctly too)
        revs = sh(["git", "-C", str(repo_path), "rev-list", "--max-count=10", "HEAD"])
        rev_list = [r for r in revs.stdout.splitlines() if r.strip()]
        if not rev_list:
            return []
        base = rev_list[-1]
        # Diff base: parent of the oldest commit, or the empty tree for a root commit
        parent = sh(["git", "-C", str(repo_path), "rev-parse", "--verify", f"{base}^"])
        root_diff = parent.returncode != 0
        log = sh(["git", "-C", str(repo_path), "log", "--oneline", "-10"])
        if root_diff:
            empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
            diff_files = sh(["git", "-C", str(repo_path), "diff", "--name-only", empty_tree, "HEAD"])
            diff_unified = sh(["git", "-C", str(repo_path), "diff", "-U0", empty_tree, "HEAD"])
        else:
            diff_files = sh(["git", "-C", str(repo_path), "diff", "--name-only", f"{base}^..HEAD"])
            diff_unified = sh(["git", "-C", str(repo_path), "diff", "-U0", f"{base}^..HEAD"])

    commits = [l for l in log.stdout.splitlines() if l.strip()]
    if not commits:
        return []
    changed_files = [f for f in diff_files.stdout.splitlines() if f.strip()]

    header = f"[{name}] {len(commits)} new commit(s): " + " | ".join(
        c[:80] for c in commits
    )
    findings.append(("INFO", header))

    code_changed = any(
        is_code_file(f, cfg) and not is_content_file(f, cfg) for f in changed_files
    )

    # Collect added lines ONLY from code files (no Markdown/content false positives),
    # tracking the file context of the hunks via "+++ b/..." markers.
    added_lines: list[str] = []
    current_diff_file: str | None = None
    for l in diff_unified.stdout.splitlines():
        if l.startswith("+++ b/"):
            current_diff_file = l[6:].strip()
            continue
        if l.startswith("+") and not l.startswith("+++"):
            if current_diff_file and is_code_file(current_diff_file, cfg) \
                    and not is_content_file(current_diff_file, cfg):
                added_lines.append(l)

    # --- 1. Functionality -------------------------------------------------------
    changed_py = [f for f in changed_files if f.endswith(".py") and not is_content_file(f, cfg)]
    for f in changed_py:
        abs_f = repo_path / f
        if not abs_f.exists():
            findings.append(("ERROR", f"[{name}] changed .py file missing in worktree: {f}"))
            continue
        r = sh([sys.executable, "-m", "py_compile", str(abs_f)])
        if r.returncode != 0:
            first_err = [l for l in r.stderr.splitlines() if l.strip()][:3]
            findings.append(("CRITICAL", f"[{name}] syntax error in {f}: {' | '.join(first_err)}"))

    if code_changed and cfg.get("test_port"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            sock.connect(("127.0.0.1", cfg["test_port"]))
            reachable = True
        except OSError:
            reachable = False
        finally:
            sock.close()
        if not reachable:
            findings.append((
                "WARN",
                f"[{name}] code changed, but the test instance (127.0.0.1:{cfg['test_port']}) "
                f"is not reachable — run the smoke test manually.",
            ))

    # pytest if available (only on code changes)
    if code_changed:
        venv_pytest = repo_path / ".venv" / "bin" / "pytest"
        pytest_bin = str(venv_pytest) if venv_pytest.exists() else "pytest"
        which = sh(["bash", "-lc", f"command -v {pytest_bin} || true"])
        if which.stdout.strip():
            r = sh([which.stdout.strip(), "-q", "--tb=no"], cwd=repo_path, timeout=300)
            if r.returncode != 0:
                tail = " | ".join(r.stdout.splitlines()[-3:])
                findings.append(("CRITICAL", f"[{name}] pytest failed: {tail}"))

    # --- 2. Security ------------------------------------------------------------
    for f in changed_files:
        if SECRET_FILENAMES.search(f):
            findings.append((
                "CRITICAL",
                f"[{name}] SECRETS FILE in commit: {f} — credentials are in the git history!",
            ))

    for line in added_lines:
        for pat in SECRET_PATTERNS:
            if pat.search(line):
                findings.append((
                    "CRITICAL",
                    f"[{name}] possible secret in new code: {mask_secret(line)}",
                ))
                break
        for pat, desc in DANGEROUS_PATTERNS:
            if pat.search(line):
                findings.append((
                    "WARN",
                    f"[{name}] {desc} in new code: {mask_secret(line)}",
                ))
                break

    # Dead path references in changed scripts (.sh/.py or shebang wrappers)
    always_code = cfg.get("always_code", [])
    for f in changed_files:
        if not (f.endswith(".sh") or f.endswith(".py") or f in always_code):
            continue
        abs_f = repo_path / f
        if not abs_f.exists():
            continue
        try:
            with abs_f.open("rb") as fh:
                magic = fh.read(2)
            content = abs_f.read_text(errors="replace")
        except OSError:
            continue
        if magic != b"#!" and not (f.endswith(".sh") or f.endswith(".py")):
            continue  # not a script
        for p in extract_local_paths(content):
            expanded = Path(os.path.expanduser(p))
            if not expanded.exists():
                findings.append((
                    "WARN",
                    f"[{name}] dead path in {f}: '{p}' does not exist "
                    f"(what the commit promises vs. what is on disk).",
                ))

    pyc_committed = [f for f in changed_files if "__pycache__" in f or f.endswith((".pyc", ".pyo"))]
    for f in pyc_committed:
        findings.append(("WARN", f"[{name}] build artefact committed: {f} — add it to .gitignore."))

    # --- 3. Maintainability -----------------------------------------------------
    if code_changed:
        for line in added_lines:
            if TODO_PATTERNS.search(line):
                findings.append(("WARN", f"[{name}] TODO/FIXME in new code: {line[1:].strip()[:90]}"))
                break

    code_added = sum(1 for l in added_lines if not l.startswith("+++"))
    if code_added > MAX_ADDED_LINES_WARN:
        findings.append((
            "WARN",
            f"[{name}] very large change: {code_added} added lines — "
            f"manual review recommended.",
        ))

    for c in commits:
        msg = c.split(" ", 1)[1] if " " in c else c
        if len(msg.strip()) < MIN_COMMIT_MSG_LEN:
            findings.append((
                "WARN",
                f"[{name}] very short commit message ({len(msg.strip())} chars): '{msg.strip()}'",
            ))

    # --- 4. Documentation -------------------------------------------------------
    if code_changed:
        doc_files = ["README.md", "CHANGELOG.md", "CHANGELOG", "docs/", "DOCUMENTATION.md"]
        has_doc_update = any(
            f.lower().startswith(d.lower()) or f.lower() == d.lower() for f in changed_files for d in doc_files
        )
        if not has_doc_update:
            findings.append((
                "WARN",
                f"[{name}] code change without README/CHANGELOG update — update the docs?",
            ))

    # --- update state -------------------------------------------------------------
    state.setdefault("repos", {})[name] = head
    save_state(state)
    return findings


def build_repo_list(args: argparse.Namespace) -> list[dict]:
    repos: list[dict] = []
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            repos.extend(json.load(fh))
    for p in args.repo or []:
        path = Path(p).expanduser()
        repos.append({
            "name": path.name,
            "path": str(path),
            "test_port": args.test_port,
            "content_dirs": args.content_dir or [],
        })
    return repos


def main() -> int:
    ap = argparse.ArgumentParser(description="Local change review for git repos (silent when nothing new).")
    ap.add_argument("--repo", action="append", help="repository path (repeatable)")
    ap.add_argument("--config", help="JSON list of repo configs (name, path, test_port, content_dirs, always_code)")
    ap.add_argument("--test-port", type=int, default=None,
                    help="local port that must be reachable after code changes (for --repo entries)")
    ap.add_argument("--content-dir", action="append",
                    help="repo-relative dir that holds content, not code (repeatable, for --repo entries)")
    args = ap.parse_args()

    repos = build_repo_list(args)
    if not repos:
        ap.error("give at least one --repo or a --config file")

    all_findings: list[tuple[str, str]] = []
    for cfg in repos:
        try:
            all_findings.extend(check_repo(cfg))
        except Exception as e:  # noqa: BLE001 — a watchdog must not die silently
            all_findings.append(("ERROR", f"[{cfg.get('name', '?')}] review exception: {e}"))

    if not all_findings:
        return 0  # silent

    order = {"CRITICAL": 0, "ERROR": 1, "WARN": 2, "INFO": 3}
    all_findings.sort(key=lambda f: order.get(f[0], 9))
    for level, msg in all_findings:
        print(f"{level}: {msg}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
