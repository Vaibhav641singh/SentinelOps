# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

On-call assistant: reads an error log, retrieves the matching runbook, **asks a second model whether that runbook is still true**, patches it against the web when it isn't, and streams recovery commands for a human to run. It suggests; it never executes.

`README.md` carries the full rationale and every measured number. This file holds the shape of the system and what is easy to get wrong or accidentally undo. When the two disagree, the README is the source of truth for *why*; this file is the source of truth for *how to run it*.

## Architecture

**Two entry points, and data flows one way.** `copilot/ingest.py` is the only thing that ever *writes* to Qdrant; the service only reads. This is deliberate — if the app embedded documents at startup, every restart and autoscale event would re-embed the corpus and burn API quota. Changing a runbook means re-running ingest by hand.

**One request, in `pipeline.diagnose_events`,** emitted as SSE so the caller can watch which path ran — that visibility is the product:

```
error log
  → clean_log()          redact + cap, ONCE, before anything downstream sees it
  → embed + BM25         dense (Gemini) + sparse (local, copilot/sparse.py)
  → retrieve()           DBSF fusion picks the chunk; dense cosine scores it
  → floor                below MIN_MATCH_SCORE the judge is skipped entirely
  → judge_runbook()      one call, TWO decisions: covers_incident AND current
  → research_findings()  one web search per stale finding, concurrent, capped
  → synthesize_stream()  streamed; path = runbook | web_patched | no_match
```

Four non-obvious things, each of which spans several files:

**`retrieve()` answers two different questions with two different signals.** *Which* chunk is a ranking question, and DBSF fusion answers it better than either arm alone. *Whether anything matched at all* is an absolute question, and a fused score cannot answer it — fusion scores are rank-derived, so the top hit scores high by construction (measured: RRF returned exactly `1.0` for queries nothing covered). So the floor reads raw dense cosine **of the chunk fusion actually served**, which is frequently not dense's own top hit. `dense_score_of` exists for the case where BM25 surfaces something dense ranked below `PREFETCH_DEPTH`.

**Coverage is the judge's decision, not the threshold's.** `JudgeVerdict` returns `covers_incident` alongside `current` in a single schema-constrained call, so it costs nothing extra. This is because cosine measures *topic*, not coverage — a Postgres crash log scores high against a disk-full section because both are Postgres. The floor is only a coarse pre-filter.

**Prompt assembly has three variants**, chosen in `synthesis.build_prompt`: merge (runbook + web research), plain, and plain + weak-match note. Prompts live in `prompts/*.md` as content, not Python literals. `MERGE_SYSTEM` is `SYNTH_SYSTEM` plus the merge rules, so formatting rules are written once.

**Untrusted input is handled at the boundary.** `clean_log()` runs once at the top of the pipeline, which is the only place that covers all four consumers — embedding, BM25, judge, and the outbound web search whose query the judge writes. A per-request `nonce` threads from `pipeline` into `judge_runbook` and `build_prompt` so untrusted content is fenced with markers it cannot forge.

**Layering:** `errors.py` exists so nothing below `api.py` imports FastAPI. `api.py` is the only module that knows status codes.

## Commands

The venv is Python 3.12 and is **not** always on PATH — call it explicitly:

```bash
venv/Scripts/python.exe -m pytest -q                  # 80 tests, no network needed
venv/Scripts/python.exe -m pytest tests/test_sanitize.py -q              # one file
venv/Scripts/python.exe -m pytest tests/test_sanitize.py::test_password_encryption_setting_survives -q
venv/Scripts/python.exe -m ruff check copilot/ tests/ evals/ scripts/
venv/Scripts/python.exe -m evals.run_eval             # retrieval eval, fully offline
venv/Scripts/python.exe -m evals.run_eval --sweep     # threshold sweep
venv/Scripts/python.exe -m evals.run_eval --check     # CI gate; exits 1 on regression
venv/Scripts/python.exe -m copilot.ingest             # writes runbooks to Qdrant (costs quota)
uvicorn copilot.api:app --port 8000
```

`pyproject.toml` pins `requires-python = ">=3.12"`. A 3.14 venv in this repo has caused trouble before; stay on 3.12.

## Things that are easy to break

**`MAX_CHUNK_CHARS = 3600` — do not lower it to get finer chunks.** One incident is one chunk. At 2600 every `.md` runbook overflowed and split into fragments like a bare Prevention section under a `(continued)` heading — such a fragment names no symptom, so it retrieves for the wrong incident and the judge is asked whether an excerpt covers an incident it never mentions. `tests/test_chunking.py` asserts one chunk per runbook against the real corpus.

**The judge's thinking budget stays on.** Setting `thinking_budget=0` dropped accuracy on naming the correct replacement parameter from 9/10 to 8/13, and was not faster. The comment in `copilot/judge.py` exists to stop this being "optimised" back.

**Redaction must not eat the subject matter.** `password_encryption = md5` is a Postgres setting the staleness audit exists to catch, not a credential. The `\b` before each keyword in `copilot/sanitize.py` is what saves it — in `password_encryption` the keyword never completes. The negative tests in `tests/test_sanitize.py` matter more than the positive ones, because over-redaction fails silently.

**`MIN_MATCH_SCORE = 0.62` is a measured trade, not a tuning knob.** It refuses 2 covered queries outright (`oldest xmin`, `password_encryption md5 no longer accepted`) to catch 8 of 12 off-topic ones. A floor refusal is final — it skips the judge, so nothing can overturn it. Changing it requires re-running `--write-baseline`, or the tests fail on a threshold mismatch by design.

**Re-ingest after changing `HYBRID_SEARCH` or `EMBED_DIM`.** Qdrant cannot add or drop a sparse vector on a live collection, so `ingest.py` recreates it.

## The eval

`evals/run_eval.py` is the single harness; `scripts/measure_hybrid.py` is superseded and kept only for history (it keyed in-memory points by bare `chunk_index`, so every file's chunk 0 overwrote the others — it cannot survive a multi-file corpus).

- **The default run is offline and must stay that way.** Query vectors and a corpus snapshot are committed under `evals/cache/`. No API key, no Qdrant, no network. That is what lets CI run it, and it means a change is measured against byte-identical embeddings.
- `evals/golden_set.jsonl` is the source of truth for labels — edit it directly. `expected` is a list because six queries genuinely have two right answers; an empty list means the correct behaviour is `no_match`.
- **`--rerank` is NOT offline** and must never gate CI or enter `baseline.json`. It costs one judge call per candidate.
- Reranking is **built but unmeasured**: three attempts reranked 1 of 87 rows because Gemini returned 503 on every failover model. The arm prints a number with 86 rows of passthrough behind it — do not quote it.

## Environment

- **Qdrant Cloud** via `QDRANT_URL`. Blank means embedded at `./qdrant_data`, which takes an exclusive directory lock — no `--reload`, no `--workers N`.
- **`ingest.py` has no resume.** A mid-run failure leaves a partial collection that `/healthz` reports as healthy. This happened: a `RemoteProtocolError` left 8 of 14 points. Re-running is safe and idempotent (`uuid5` ids, delete-by-source then upsert), so re-run rather than patch.
- **Cloud rejects filters without a payload index** where embedded Qdrant allows them. `ensure_source_index` exists for exactly this — it is the one thing that passes locally and fails on Cloud.
- **Gemini quota is easily exhausted**, and capacity is genuinely flaky — 503 on roughly one call in three, with multi-hour degraded periods.

## Known architectural gap

`with_failover` falls back from Gemini **to Gemini** (`FALLBACK_MODELS` are all Gemini models). It protects against one model being busy, not against the provider being down — which is what blocked the reranker measurement for ~45 minutes. Four of the five online steps share that single point of failure. A cross-provider fallback for synthesis and the judge is the obvious robustness win and is not built.

## Conventions

- **No Claude attribution in commits or PRs** — no `Co-Authored-By`, no "Generated with" line.
- **Prompts live in `prompts/*.md`**, not Python literals. They are content, not code.
- **`errors.py` exists so no module below `api.py` imports FastAPI.** Only `api.py` maps a domain error to a status code.
- **Ruff is pinned to `["E4", "E7", "E9", "F", "I"]`** — ruff's default plus import sorting. Widening to `E501` or `B` is worthwhile but is a package-wide reformat, so it belongs in its own change. Do not let `--fix` quietly modernise files outside the change you are making.
- Comments here explain *why*, with the measurement that justifies the choice. Match that density; a bare number with no provenance is the thing this codebase is trying not to have.

## Open work

Ranked, with the blocking reason:

1. **Finish the reranking measurement** — blocked on Gemini capacity, resumes from cache.
2. **Cross-provider fallback** — the gap the 503s exposed.
3. **Observability** — per-stage latency, branch ratios, cost per diagnosis. Nothing exists.
4. **The write-back loop** — stale detected → proposed runbook diff with provenance → review queue → versioned re-index. The genuinely differentiating feature, and the eval can now prove whether a written-back patch helps or pollutes.
5. **Faithfulness scoring** — needs live LLM calls, so it stays out of the PR gate.
