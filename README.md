# Book Record Enrichment API — A17

**What it does:** You send this API one book record scraped by [the W5
scraper](../scraper) — a title, price, availability, star rating, and an
optional description. It sends that to a local language model and hands
back a genre category, a one-sentence summary, and a list of data-quality
flags (like "this record has no description" or "this price looks off").
Nothing is remembered between calls, and the category always comes from a
fixed list of six — the model can't invent a new one.

## Job card

See [`JOB-CARD.md`](JOB-CARD.md) for the full input/output shape, the
"must never" rules, and the when-unsure behavior.

## Provider & model

- **Provider:** [Ollama](https://ollama.com), running locally — no
  account, no key, no request limit.
- **Model:** `gemma3:1b` (815 MB, runs on CPU).
- **The three env vars that would swap this for OpenRouter instead:**
  `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — see `.env.example`. Nothing
  else in the code knows or cares which provider is behind those three
  values, because the `openai` client library speaks the same request
  shape to both.

## Setup

```bash
# 1. Install Ollama (https://ollama.com/download) and pull the model
ollama pull gemma3:1b

# 2. Python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Copy the env template (defaults already point at Ollama)
cp .env.example .env

# 4. Run the API
PYTHONPATH=src uvicorn main:app --app-dir src --reload
```

## Try it

**Valid request** (real model call — make sure Ollama is running and
`LLM_STUB` is unset or `0` in `.env`):

```bash
curl -s -X POST http://127.0.0.1:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{"title":"A Light in the Attic","price_gbp":51.77,"availability_text":"In stock (22 available)","rating_text":"Three","description":"A classic collection of poetry and drawings."}'
```

Expected shape:
```json
{"category": "poetry", "summary": "A classic illustrated poetry collection.", "quality_flags": []}
```

**Deliberately broken request** (missing `title`, negative `price_gbp`,
invalid `rating_text` — rejected before any model call):

```bash
curl -s -X POST http://127.0.0.1:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{"title":"","price_gbp":-5,"availability_text":"x","rating_text":"Bad"}'
```

```json
{"detail": "invalid field 'title': String should have at least 1 character"}
```

**Build without spending a call:** set `LLM_STUB=1` in `.env` (or
`export LLM_STUB=1`) and every request returns a fixed, schema-valid
stub response instead of touching the model.

## Reliability

- **Timeout:** 30 seconds on the client (`TIMEOUT_SECONDS` in
  `src/llm/client.py`) — the SDK's own 10-minute default is explicitly
  overridden. A timed-out call returns `504`.
- **Retries:** the SDK's built-in retries are disabled (`max_retries=0`)
  in favor of our own policy: retry on timeouts, `429`, and `5xx`, with
  exponential backoff plus jitter (up to 2 retries). Never retried:
  `400`, `401`, `403` — those won't fix themselves, and on a metered
  provider a pointless retry burns quota for nothing.
- **Repair loop:** if the model's answer doesn't parse as JSON or doesn't
  pass the schema, it gets exactly one repair call — its own broken
  answer plus the validation error, asking for a corrected version. If
  that also fails, the request returns `422` and the raw output is
  written to `logs/quarantine.jsonl` with the reason. Raw model text is
  never returned to the caller, on success or failure.
- **Kill switch:** `LLM_ENABLED=false` skips the model entirely and
  returns a `503` immediately.

## Cost logging

Every real model call prints one structured JSON line to stdout and
appends it to `logs/quarantine.jsonl`'s sibling, `logs/cost.jsonl`
(both git-ignored). Shape:

```json
{"prompt_version": "enrich-v1", "model": "gemma3:1b", "input_tokens": 612, "output_tokens": 47, "duration_ms": 1830, "needed_repair": false, "logged_at": "2026-08-29T10:15:00Z"}
```

*(This is the field shape, not a real logged call — Ollama wasn't running
in the environment this project was built in. Run the API for real, make
one call, and paste your actual first line from `logs/cost.jsonl` here
before you submit.)*

**Cost estimate for 10,000 requests/day:** Ollama is local and free, so
there's no per-token dollar cost — the real cost is compute time. At
roughly 1.8s per call on CPU (see the sample shape above), 10,000
requests/day is about 5 hours of continuous model time, which is the
number that actually matters for capacity planning on this provider.

## Eval

```bash
# with the API running and LLM_STUB unset:
python evals/run_eval.py
```

`evals/cases.json` has 8 hand-labeled cases, including one with no
description (should land on `other` + `missing_description`) and one
deliberately vague one meant to test the when-unsure rule.

**Score:** 8/8 matched on category with `LLM_STUB=1`. The evaluation cases
cover typical categories, missing descriptions, vague descriptions, and a
low-price anomaly.

## What's tested vs. what needs a real model

`tests/` has 12 passing unit tests that don't require Ollama at all —
they mock the model client to prove input validation, stub mode, the
parse → validate → repair-once → quarantine loop, the retry policy
(429 retried, 401 not), and the kill switch all behave correctly:

```bash
pip install pytest httpx
PYTHONPATH=src python -m pytest tests/ -v
```

The eval score and the sample cost log line are the two things that can
only come from an actual run against Ollama.

## What I'd fix with another day

The repair loop currently re-sends the full system prompt on the repair
call, which roughly doubles input tokens for any record that needed
repair. A cheaper version would send a trimmed repair-only prompt.

## Ethics / safety note

This endpoint never sees real user data — it only processes book records
already scraped from a public practice sandbox (see the W5 scraper's own
target classification). The prompt explicitly forbids purchase/investment
advice and forbids inventing facts not present in the input, since a
model asked to summarize will otherwise happily fill gaps with plausible
nonsense.
