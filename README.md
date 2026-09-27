# neobild-agent

A curated collection of scripts from the NeoBild agent stack: local inference,
agent memory, knowledge graphs, a phone-to-laptop second brain and read-only
Android auditing, all designed to run on modest hardware you own.

## What is NeoBild?

NeoBild is an open research and development project on **sovereign AI
infrastructure**. Inference runs locally on your own hardware instead of in
someone else's cloud; data stays under your control by default. The project
explores digital sovereignty in practice (self-hosting, offline-first
tooling, auditable agents) and the privacy questions that come with
brain-computer interfaces (BCI) and neural data. Everything is documented in
the open, including measurements and failures.

NeoBild was founded and is maintained by **Lukas Weißmann**.

## What is in this repository?

Standalone scripts extracted from the day-to-day NeoBild/Hermes agent setup,
cleaned up so they make sense without the private environment they came
from. Most are plain Python 3 (standard library where possible) or POSIX
shell, configurable via command-line arguments or environment variables.

| Folder | What it does |
|---|---|
| [`scripts/local-inference/`](scripts/local-inference/) | Start a local `llama-server`, on-demand embeddings, a deterministic LLM router (local vs. cloud, privacy markers force local), and a critique → defense → judge discourse loop with majority voting and a prompt test bench for small models. |
| [`scripts/agent-memory/`](scripts/agent-memory/) | SQLite agent memory with LLM importance scoring, a nightly "dream" cycle (importance decay + consolidation), reflection into higher-level insights and a user-profile extractor. |
| [`scripts/vault-graph/`](scripts/vault-graph/) | Turn a Markdown/Obsidian vault into a link graph: wikilink parser, SQLite edge DB, graph queries (neighbours, paths, rank), JSON export for a 3D force-graph view, vector edges from embeddings and tools to measure whether they help. |
| [`scripts/knowledge-graph/`](scripts/knowledge-graph/) | Building blocks for a local knowledge base: PageRank / personalized PageRank / Adamic-Adar / label propagation on numpy, hybrid search fusion (full-text + vectors + graph), Markdown/front-matter helpers and a knowledge-base linter. |
| [`scripts/second-brain/`](scripts/second-brain/) | Phone-to-laptop capture: pull notes from an Android phone, route them into folders by keyword rules, sort a Syncthing inbox, build a task list from an inbox, convert saved web pages (MHTML) to Markdown. |
| [`scripts/android/`](scripts/android/) | Read-only auditing and helpers for your own Android device over ADB: hardening audit with baseline diff, listening sockets mapped to apps, app/permission inventory, connection log, UI-tree helper, file transfer, Termux background job. |
| [`scripts/telemetry/`](scripts/telemetry/) | Deterministic system sampler, a hardware health collector (sensors, RAPL, battery, journal errors), anomaly detection on the samples, and a governor that decides whether heavy background work may run right now (FULL / GENTLE / STOP). |
| [`scripts/indexing/`](scripts/indexing/) | Deterministic BLAKE3 file index (what exists, what changed) and a read-only workspace hygiene scan. |
| [`scripts/ops/`](scripts/ops/) | Cron-friendly watchdogs and briefings: failing cron jobs, zram swap, automatic change review for git repos, CVE and RSS digests, PCAP extraction, lossless archiving, deploy drift check, system inventory. |

Each folder has its own `README.md` with usage details. See
[ROADMAP.md](ROADMAP.md) for the technical vision and current status.

### Conventions

- No hard-coded machine paths: every location is an argument or environment
  variable with a neutral default.
- Local endpoints (e.g. a `llama-server` on `http://127.0.0.1:8080`) are
  defaults only and can be overridden.
- Secrets are never passed as literals; scripts read them from a file or an
  environment variable.
- Many scripts are deliberately deterministic (no LLM involved), so they can
  run as cheap cron jobs.

## Links

- Project website: <https://neobild.de/> (German). **An English project
  website is coming soon.**
- NeoBild organization on GitHub: <https://github.com/NeoBild>
- Trinity, configurable multi-persona/multi-agent discussions with a BLAKE3
  hash chain: <https://github.com/NeoBild/Trinity>
- Older repository: <https://github.com/weissmann93/NeoBild>

## License

MIT, see [LICENSE](LICENSE).
