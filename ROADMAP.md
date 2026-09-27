# NeoBild Roadmap

This document describes the technical direction of NeoBild and where things
stand today. It is a snapshot, not a promise: items marked **open direction**
are ideas being explored, with no committed date.

Status legend: **working** (in daily use), **in progress** (being built or
reworked), **open direction** (planned or under evaluation).

---

## 1. Agent base: Hermes Agent

NeoBild's day-to-day agent runs on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research,
MIT), extended with its own scripts, skills, cron jobs and a Kanban board that
serves as the hand-off channel between the agent and human/coding-agent work.

- **working:** Hermes as the central hub: scheduled no-LLM ("deterministic")
  cron jobs for monitoring, self-checks and briefings; LLM turns only where
  they add value.
- **working:** Governance by design: changes to the agent's own configuration,
  memory or skills go through an explicit proposal/approval step instead of
  being written directly.
- **open direction:** a NeoBild-specific fork or distribution of Hermes that
  bundles the local-first defaults, the deterministic watchdogs and the
  approval workflow from this repository.

## 2. Local inference / sovereign stack

Goal: inference on hardware you own, with no mandatory cloud dependency. The
reference machine is deliberately modest (a CPU-only laptop, no GPU, ~16 GB
RAM), so everything is designed for small quantized models.

- **working:** `llama.cpp` `llama-server` with a small instruct model
  (3B class, Q4 quantization) behind a local OpenAI-compatible endpoint,
  protected by an API key file.
- **working:** deterministic model routing for small tasks, majority voting
  over repeated local-model answers, and a critique → defense → judge loop to
  catch outliers of small models (see `scripts/local-inference/`).
- **working:** resource governance: a telemetry sampler, anomaly detection and
  a "governor" that decides whether heavy background work may run right now
  (see `scripts/telemetry/`).
- **in progress:** a deterministic edge orchestrator ("the LLM proposes, a
  policy engine decides, a sandboxed executor acts", every decision logged in
  a BLAKE3 hash chain). Not yet published.
- **open direction:** offloading always-on workloads (embeddings, speech
  input/output) to a small dedicated low-power machine instead of the main
  laptop.

## 3. Memory and knowledge graph

Goal: an agent memory that is local, inspectable (plain Markdown + SQLite) and
explains *why* something is relevant.

- **working:** Markdown vault with wikilinks as the single source of truth;
  a graph builder turns links into an edge database and a 3D force-graph export
  (see `scripts/vault-graph/`).
- **working:** a deterministic BLAKE3 file index as the lowest layer (what
  exists, what changed), feeding a compact fact store.
- **in progress:** unifying two earlier, parallel knowledge-graph
  implementations into one pipeline. Lessons learned so far: entity edges
  built as cliques flood PageRank with noise, and strict AND full-text search
  misses natural-language questions. Planned fixes: damp hub entities (top-k /
  IDF weighting), switch full-text search to OR + BM25, then add vector edges
  where they measurably help (see `scripts/knowledge-graph/`).
- **working (prototype):** nightly "dream" cycle for memory: importance decay,
  consolidation and reflection into higher-level notes
  (see `scripts/agent-memory/`).
- Guard rails: search instead of dumping whole notes into context, link
  *suggestions* instead of automatic writes, and strictly separated
  confidentiality zones inside the vault.

## 4. Second-brain pipeline

Goal: capture on the phone, process on the laptop, keep everything local.

- **working:** notes and tasks written on an Android phone (plain Markdown
  files) are pulled to the laptop, routed into folders by keyword rules and
  turned into a task list; files arriving via Syncthing are sorted
  automatically; saved web pages (MHTML) are converted to Markdown
  (see `scripts/second-brain/`).
- **open direction:** one documented capture → inbox → vault flow (today there
  are two parallel intake paths) with a single dashboard view over clippings,
  screenshots and LLM sessions.

## 5. Multi-agent orchestration: Trinity

[Trinity](https://github.com/NeoBild/Trinity) is a configurable multi-persona
discussion CLI: you define personas and a purpose, they discuss turn by turn
against any OpenAI-compatible endpoint, and every contribution is appended to
a tamper-evident BLAKE3 hash chain.

- **working:** Trinity 2.0, public under the NeoBild organization (MIT).
- **in progress:** a dashboard integration of the multi-persona loop where
  personas only speak when they have a relevant contribution (confidence-gated
  instead of fixed rounds). The confidence scoring has to be fixed first.

## 6. Android control and audit

Goal: treat an ordinary, non-rooted Android phone as a first-class,
auditable part of the local stack.

- **working:** read-only audits over ADB: app inventory with permissions,
  listening sockets mapped to the owning app, connection logging, device
  event hooks (see `scripts/android/`).
- **working (prototype):** UI control through the accessibility/UI tree
  (compact, numbered element lists instead of screenshots, so text-only models
  can operate the phone) with a verify loop and escalation after repeated
  failures.
- **open direction:** a "physical key" model where sensitive agent actions are
  only allowed while a trusted device is present or after a fingerprint
  re-arm; continuous on-device hardening checks.

## 7. Website and community

- **working:** [neobild.de](https://neobild.de/) as a static Hugo site
  (no tracking cookies, no JS framework), relaunched as an open research and
  development hub.
- **in progress:** an English project website.
- **working:** the [NeoBild GitHub organization](https://github.com/NeoBild)
  as the home for public code.

---

Contributions, corrections and counter-arguments are welcome: open an issue.
