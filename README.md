# SentinelOps

An on-call assistant that reads a server error log, finds the matching runbook, **checks whether that runbook is still true**, and patches it against the live web when it isn't — then streams back recovery commands for a human to run.

The idea worth caring about: most runbooks rot. A doc written in 2023 confidently tells you to set a config parameter that a later release renamed or removed. This adds a second model that asks *"is this advice still correct?"* before handing it over, and goes looking for a current answer when it isn't.

**It suggests commands. It never runs them.**

## How it works

```
error log
   │
   ├─► embed (gemini-embedding-001, RETRIEVAL_QUERY)
   ├─► retrieve top runbook chunk (Qdrant, cosine)
   ├─► JUDGE: is this chunk still current?  (gemini-3.1-flash-lite, structured JSON)
   │        │
   │        ├── current ──────────────────────────┐
   │        │                                     │
   │        └── outdated ─► web search (ddgs) ─► merge
   │                                              │
   └─► synthesize recovery commands ◄─────────────┘
            │
            └─► stream to the engineer (SSE)
```

Retrieval is chunked **one chunk per runbook section**, because the section is the unit the judge reasons about — splitting mid-section hands it half an argument to rule on.

## Setup

Requires Python 3.12+ and a [Gemini API key](https://aistudio.google.com/apikey).

```bash
pip install -r requirements.txt
cp .env.example .env        # then put your real GEMINI_API_KEY in it
python -m copilot.ingest            # loads runbooks/ into Qdrant
uvicorn copilot.api:app --port 8000
```

Then:

```bash
curl -N -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d '{"error_log":"FATAL: sorry, too many clients already"}'
```

`-N` matters — without it curl buffers and you lose the streaming.

### Three ways to use it

**Browser** — <http://localhost:8000/>. Paste the API key once, then click a preset.

**Terminal** — `copilot.py` is a CLI client that renders the stream as Markdown:

```bash
python -m copilot.cli samples/connections.log        # stale runbook -> patched from web
python -m copilot.cli samples/disk_full.log          # current runbook -> straight answer
python -m copilot.cli samples/nginx_ssl.log          # no match -> honest refusal

tail -50 /var/log/postgresql/postgresql.log | python -m copilot.cli -
```

It reads `APP_API_KEY` from your environment or `.env`; `--key` overrides, `--url` points at a remote deployment, `--raw` prints plain text for piping. It needs `requests`, `typer` and `rich` — in `requirements.txt`, but the server itself does not use them.

**curl** — as above. Good for scripting; you get raw SSE frames.

### Where Qdrant runs

`QDRANT_URL` decides, and nothing else changes. Same client, same code.

| `QDRANT_URL` | Mode | Needs |
|---|---|---|
| *blank* | Embedded, `./qdrant_data` | nothing |
| `http://localhost:6333` | Docker container | `docker run -p 6333:6333 qdrant/qdrant` |
| `https://….cloud.qdrant.io:6333` | Qdrant Cloud | your Cloud API key |

Re-run `python -m copilot.ingest` after switching — the collection lives with the server.

> **Local and Cloud are not quite identical.** Embedded Qdrant filters on any payload field; a Qdrant *server* rejects the same filter with `400 Index required but not found for "source"`. `ingest.py` deletes a file's previous points by filtering on `source`, so it creates a keyword payload index on that field. Worth knowing because it is the one thing that passes locally and fails the moment you point at Cloud.

> **Embedded mode takes an exclusive directory lock.** One process at a time: no `uvicorn --reload`, no `--workers N`. Use a Qdrant server if you want either. `ingest.py` and `app.py` don't collide because they run at different times.

### Docker

```bash
docker network create copilot-net
docker run -d --name qdrant --network copilot-net -p 6333:6333 \
  -v qdrant_storage:/qdrant/storage qdrant/qdrant

docker build -t devops-ai-copilot .
docker run -d --name copilot --network copilot-net -p 8000:8000 \
  --env-file .env -e QDRANT_URL=http://qdrant:6333 devops-ai-copilot
```

`.env` is in `.dockerignore`, so no secret is ever baked into the image — it arrives at runtime via `--env-file`. Qdrant's dashboard is at <http://localhost:6333/dashboard>.

## Authentication

`POST /diagnose` requires a shared key, sent either way:

```bash
curl -N -X POST http://localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $APP_API_KEY" \
  -d '{"error_log":"FATAL: sorry, too many clients already"}'
```

```bash
-H "Authorization: Bearer $APP_API_KEY"   # equivalent
```

Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `.env` as `APP_API_KEY`. Comparison uses `secrets.compare_digest`, so a wrong key can't be recovered by timing the response.

`GET /` and `GET /healthz` stay open — the page contains no secrets, and health checks shouldn't need credentials. Only `/diagnose` is gated, because it's the endpoint that costs money to serve.

The browser UI asks `/healthz` whether auth is on and only shows a key field when it is. The key lives in `sessionStorage`, so it dies with the tab rather than sitting on disk; a `401` re-opens the field rather than failing silently.

**If `APP_API_KEY` is blank, auth is off.** That keeps local development frictionless, but it fails *open*, so it is stated out loud rather than hidden: a startup warning is logged and `/healthz` reports `"auth": "DISABLED"`. Set the key before the port is reachable from anywhere but your own machine.

## API

`POST /diagnose` → `text/event-stream` (authenticated). `GET /healthz` → auth mode, key status, Qdrant mode, point count.

| Event | Payload |
|---|---|
| `stage` | `{"stage":"retrieving"\|"retrieved"\|"judging"\|"web_search"\|"web_results"\|"synthesizing", …}` |
| `verdict` | `{"current":bool,"reason":str,"findings":[{"item","problem","search_query"}]}` |
| `token` | `{"text":"…"}` — incremental synthesis output |
| `done` | `{"path":"runbook"\|"web_patched"\|"no_match","findings":[…],"sources":[…]}` |
| `error` | `{"message":"…"}` |

The judge returns a **list** of findings, not one reason. A single-reason schema let it stop at the first problem it noticed, and these docs usually rot in several places at once. Each finding gets its own targeted web search, run concurrently, capped at `MAX_SEARCHES` (3) — findings past the cap still reach the merge step but are corrected from model knowledge and tagged `[unverified]` in the output.

The `path` field is the point of the whole project: it tells you whether the answer came straight from the runbook or was corrected against the web.

## Configuration

All of `.env`. Model IDs live here deliberately — Gemini model strings change often, and a wrong one is the most common first-day error.

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | required |
| `EMBED_MODEL` / `EMBED_DIM` | `gemini-embedding-001` / `768` | changing the dim recreates the collection |
| `JUDGE_MODEL` | `gemini-3.1-flash-lite` | cheap; thinking disabled |
| `SYNTH_MODEL` | `gemini-3.8-flash` | |
| `FALLBACK_MODELS` | `gemini-3.7-flash,gemini-3.1-flash-lite` | tried in order on 503/429 |
| `MIN_MATCH_SCORE` | `0.62` | below this, skip the audit — see below |

### The relevance gate

Retrieval always returns *something*, even for a log no runbook covers. Without a floor, the pipeline would audit whatever came back and burn web searches on staleness irrelevant to the incident — an nginx SSL error retrieved the Postgres runbook and triggered searches for `wal_keep_segments`, then labelled the answer `web_patched` as though it had corrected something useful.

So a chunk scoring below `MIN_MATCH_SCORE` short-circuits: no judge, no search, `path: "no_match"`. Synthesis still runs, told the match was weak, so it gives an honest "this runbook doesn't cover your error" rather than stretching.

Measured on this runbook: on-topic logs land at **0.74–0.78**, the unrelated nginx error at **0.55**. `0.62` sits in that gap. Re-measure if you swap the embedding model or change chunking — the number is not portable.

## Risks & honest limitations

**The judge is the shakiest link.** It has no way to *know* a doc is stale — it pattern-matches. In testing it reliably catches `wal_keep_segments` (removed in PG13), `password_encryption = md5` (default changed in PG14), and `recovery.conf` promotion (removed in PG12), while correctly leaving the version-independent disk-space section alone.

Getting there took real prompt tuning, and the failure modes are worth knowing because they will come back if you edit the prompt:

- **Too lenient** (a single `reason` field) and it stops at the first problem, leaving the rest of the section stale.
- **Too strict** (demanding every finding, without a bar) and it starts inventing: an early revision flagged `VACUUM FULL` as "deprecated in favour of pg_repack" and `du --max-depth` as non-portable. Neither is staleness — they are preferences. It also got a *fact* wrong under that pressure, claiming `wal_keep_segments` became `max_wal_size` rather than `wal_keep_size`.

The fix was an explicit test — *"would this line actually break on a supported release today?"* — plus a rule that a finding must name the concrete release that changed it, since an unnameable mechanism means the model is guessing.

- **Confidently wrong on the replacement.** The nastiest one, because the engineer will run it. Asked what replaced `wal_keep_segments`, the judge sometimes answered `max_wal_size`/`min_wal_size` instead of `wal_keep_size`. Measured over repeated runs it was right **8 times in 13**.

  Prompt wording did not fix this. Telling it "vague is safe, confidently wrong is not" and offering an explicit *"replacement to be confirmed"* escape hatch changed nothing — it never took the escape hatch. What fixed it was **re-enabling the thinking budget** on the judge, which had been set to `0` to save latency: accuracy went to **9 in 10**, and it was slightly *faster*. The comment at `copilot/judge.py` records this so nobody optimises it back.

  It is still not 100%. So the merge step is told explicitly that the audit's note is a detection hint, not an authority on the fix — where the web sources disagree with it, the web wins.

**Tuning the threshold is the work.** If you change the runbook, re-check both directions: that a stale section trips, *and* that a current one doesn't.

**Never auto-execute the output.** A human reads and runs the commands. An AI that pulls a "fix" off the open web and runs it against a production database is how you delete a company. This is a copilot that advises, not an autopilot that acts.

**Web results are unstructured and unranked.** DuckDuckGo snippets can be wrong, stale in their own way, or SEO spam. The merge step trusts whatever floats to the top. The synthesis prompt asks the model to say when results look thin, and it does — but treat the web path as "here's what I found, verify it," not gospel.

**Retrieval quality caps everything.** Bad chunking means the wrong runbook comes back and every downstream step is polluted. When answers feel off, suspect retrieval before blaming the model.

**Gemini Flash capacity is genuinely flaky.** Measured `503 UNAVAILABLE` on roughly one call in three during development. Hence retry-with-backoff plus model failover — without it the demo crashes at random. Once synthesis has emitted its first token the model is committed, so a mid-stream failure surfaces as an `error` event rather than a silent re-roll.

**Only one runbook ships here.** `runbooks/database_guide.txt` is deliberately written as a stale 2023 Postgres doc so the self-correction path is demonstrable. Real coverage would need many more, and retrieval would need re-tuning at that scale.

## Layout

```
copilot/
├─ config.py        all settings, read from the environment
├─ errors.py        domain errors — no HTTP concerns below the API layer
├─ clients.py       Gemini + Qdrant construction, retry/failover policy
├─ embeddings.py    embedding calls and L2 normalization
├─ chunking.py      splitting runbooks on SECTION headings
├─ models.py        JudgeVerdict, StaleFinding, DiagnoseRequest
├─ prompts.py       loads prompts/*.md
├─ judge.py         the staleness audit
├─ research.py      DuckDuckGo + grounding fallback, per-finding fan-out
├─ synthesis.py     prompt assembly and streaming generation
├─ pipeline.py      the diagnosis flow, emitted as SSE
├─ api.py           FastAPI routes — the only module that knows status codes
├─ security.py      shared-key auth
├─ ingest.py        standalone: WRITES runbooks into Qdrant
└─ cli.py           terminal client

prompts/*.md        every prompt, as readable text rather than Python literals
tests/              chunking, normalization, SSE framing, prompt assembly
runbooks/           the corpus — one deliberately outdated Postgres runbook
samples/*.log       example error logs, one per path
static/index.html   browser UI, no build step
```

**Why the app and ingestion are separate:** the service only ever *reads* from Qdrant. If it embedded documents at startup, every restart would re-embed everything and burn API quota.

**Why prompts are files:** they are content, not code. Tuning them shouldn't mean editing a module, and a reviewer can read them without scrolling past backslash-continued string literals.

**Why `errors.py` exists:** so `judge.py` and `research.py` never import FastAPI. Only `api.py` maps a domain error to a status code.

Ingestion is a separate script on purpose: if the app embedded documents at startup, every restart would re-embed everything and burn API quota.
