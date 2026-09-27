# second-brain

Small, deterministic tools that move notes from a phone into a local Markdown
knowledge base (e.g. an Obsidian vault). No LLM, no cloud - plain rules, adb
and SHA-256 checks. Python 3.9+, stdlib only (pandoc optional).

| Script | What it does |
|---|---|
| `note_router.py` | Keyword-rule router: files a note as `<root>/<category>/YYYY-MM-DD-HHMM-<slug>.md`, keeps `index.md`. Shared by the other scripts. |
| `phone_notes_pull.py` | Pulls new `.md` notes from the phone via adb, routes them, keeps raw copies, optional delete-on-phone only after SHA-256 verification. |
| `phone_intake.sh` | Read-only alternative: copies new text files from phone folders into an inbox, de-duplicates by hash, converts MHTML captures. |
| `mhtml_to_markdown.py` | Turns an MHTML page capture (often saved from mobile browsers/chat apps) into readable Markdown. |
| `task_list.py` | Builds `TASKS.md` + `tasks.json` from the inbox and suggests a destination per item. Decides nothing. |
| `sort_inbox.py` | Empties a synced inbox folder (e.g. Syncthing): text -> router, screenshots/media/docs -> device folders. |

Typical flow: `phone_intake.sh` (or `phone_notes_pull.py`) -> `task_list.py`;
a synced folder is handled by `sort_inbox.py`. Set `NOTE_ROUTER_ROOT` to your
vault folder and optionally `NOTE_ROUTER_RULES` to your own keyword rules.

Author: Lukas Weißmann - License: MIT
