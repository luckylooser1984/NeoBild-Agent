# local-inference

Tools for getting useful, *measurable* work out of small language models
running locally on a CPU-only machine (tested with Qwen3-1.7B in llama.cpp's
`llama-server`, ~13–15 tokens/s on an 8-thread laptop). Everything talks to an
OpenAI-compatible endpoint on `127.0.0.1`; nothing requires a cloud service.

| Path | What it does |
|---|---|
| `serve_llama.sh` | Starts `llama-server` bound to localhost (model/port via env vars). |
| `embed_on_demand.sh` | Starts/stops an embedding server only when needed (saves RAM on bursty workloads). |
| `discourse/discourse.py` | Critique → defense → judge review of a draft by a small model, with context isolation between roles and full JSONL logging. |
| `discourse/majority_vote.py` | Runs the discourse n times and decides by majority; a split vote is reported as "escalate". |
| `discourse/battery_runner.py` | Fixed task battery for long-running stability measurements (scheduler-driven, hard timeouts). |
| `discourse/prompt_bench.py` | Scores role outputs by text-derived criteria (point fidelity, truncation, JSON conformity, echo, independence) and compares prompt versions. |
| `discourse/ab_runs.sh` | Produces runs for an A/B comparison of `roles_<variant>.json` files. |
| `llm-router/router.py` | Deterministic local-first router: privacy markers force local, cloud only on explicit/complex request *and* a configured key. CLI or OpenAI-compatible proxy. |

## Quick start

```bash
LLAMA_MODEL=/path/to/Qwen3-1.7B-Q8_0.gguf ./serve_llama.sh &
cd discourse
python3 discourse.py run --task "Draft: delete all logs older than 7 days, no backup exists."
python3 majority_vote.py --task-file draft.md --runs 5
python3 prompt_bench.py single discourse-logs/runs.jsonl
```

Environment variables: `LLM_BASE_URL` (discourse; overrides `base_url` in
`roles.json`), `DISCOURSE_LOG_DIR`, and the `LLM_ROUTER_*` variables listed in
`llm-router/router.py`.

## Findings that shaped the design

- **Thinking blocks eat small token budgets.** Qwen3 emits `<think>…</think>`
  first; with a small `max_tokens` the answer comes back empty. A closed
  `<think>` block as assistant prefill suppresses it (`lib/llm_client.py`).
- **A single verdict from a 1.7B model is not reliable.** It once passed a
  risky draft with confidence 0.9; across several runs the majority was
  right, hence `majority_vote.py`.
- **Measure prompts, don't guess.** `prompt_bench.py` turns "the prompt feels
  better" into numbers per role and prompt version.

## Tests

```bash
cd discourse  && python3 -m pytest tests   # or: python3 -m unittest discover -s tests
cd llm-router && python3 -m pytest tests
```

All tests run offline (a throwaway local HTTP server or fake clients).
The router's live paths (real local model, real cloud API) are not covered
by tests.

Author: Lukas Weißmann · License: MIT
