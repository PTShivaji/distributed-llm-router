# Distributed Multi-Model Router & Load Balancer

A FastAPI backend that classifies each LLM request, routes it to the most
suitable model, and answers it with retries, fallback across models and
providers, a shared response cache, distributed rate limits, spend limits,
request history and full observability. Several instances can run side by side;
everything they share lives in Redis and PostgreSQL.

**Mock mode is the default** (`LLM_MOCK_MODE=true`). Out of the box every model
is served by a deterministic mock provider: no API keys, no network calls, no cost.
Real providers (OpenAI, Anthropic, Gemini, Groq, Mistral and a local Ollama) are
used only after an explicit, multi-part opt-in (see
[Activating real providers](#activating-real-providers)).

The backend is complete through Phase 5 step 9. The frontend is a later phase.

## Architecture

```
                 ┌────────────────────────────── one API instance ───────────────────────────────┐
 client ──HTTP──►│ middleware: request id → tracing → security headers → CORS → access log/metrics │
                 │             → body-size limit                                                 │
                 │ /api/v1/chat: auth → rate limit → ChatService                                  │
                 │   classifier → routing policy → cache → budget → provider chain → recorder    │
                 └──────┬───────────────────────────┬───────────────────────────┬────────────────┘
                        │                           │                           │
                  Redis (shared)              PostgreSQL (shared)     LLM providers via LiteLLM
   cache · health · rate limits · budgets      request history       (only when activated; mock otherwise)
```

| Layer | Package | Responsibility |
|---|---|---|
| API | `app/api/` | Validation, authentication, rate limiting, response schemas |
| Classification | `app/classifier/` | Rule-based SIMPLE / MEDIUM / COMPLEX, intent, coding detection |
| Routing | `app/routing/` | Registry, model selection, fallback chain, retries, health, usage and cost |
| Providers | `app/providers/` | Mock provider, LiteLLM adapter, error mapping, real-provider startup gate |
| Spend | `app/budget/` | Atomic reservations for chat attempts and paid embeddings |
| Cache | `app/cache/`, `app/embeddings/` | Exact and (optional) similarity cache, embedders, equivalence guard |
| Infrastructure | `app/infra/`, `app/db/`, `app/ratelimit/` | Redis pool, database engine, migrations, recorder, analytics, limiter |
| Observability | `app/observability/`, `app/core/logging.py` | Prometheus metrics, OpenTelemetry tracing, structured logs, redaction |
| Hardening | `app/core/production.py`, `app/core/security.py` | Production startup checks, security headers, body limit |

`app/infra/container.py` builds every component from settings, once per app
instance. One budget guard is shared by chat and paid embeddings, so both draw
from the same counters.

## Request lifecycle (`POST /api/v1/chat`)

1. **Middleware.** Assigns or accepts `X-Request-ID`. When tracing is on, starts a
   trace and returns `X-Trace-ID`. Adds security headers and enforces
   `MAX_REQUEST_BODY_BYTES` (413).
2. **Authentication.** With `CHAT_AUTH_MODE=api_key`, a valid
   `Authorization: Bearer <key>` is required (401 otherwise). This runs **before**
   rate limiting, so failed attempts consume no quota.
3. **Validation.** `query` 1–10,000 characters and not blank. Optional `model`,
   `temperature` (0–2), `max_output_tokens` (1–32,000) and `use_cache`. Unknown fields
   are rejected (422). Error details never echo the submitted value.
4. **Rate limit.** Per client (the API-key name, or the client IP when auth is off).
   One atomic Redis script checks every window (429 with `Retry-After`).
5. **Classification and model selection.** Classifies the query, reads shared
   health, and selects a model by tier, capabilities, context window and strategy.
   An explicit `model` must be registered (400 `unknown_model`) and enabled
   (400 `model_disabled`).
6. **Exact cache lookup.** A hit is served with no provider call, no embedding and
   no spend.
7. **Similarity lookup** (only with `CACHE_SEMANTIC_ENABLED=true` and a meaning-aware
   embedder). A paid embedding is **reserved against the budget before the call**.
   If the reservation is refused, the call is skipped and the lookup is a miss. An
   identical concurrent miss waits on a per-query lock.
8. **Provider chain.** The primary model, then the fallback chain. Before **every**
   real attempt, including retries, its worst-case cost is reserved atomically.
   Retries and fallback follow the error policy table below; unhealthy,
   rate-limited and over-budget models are skipped.
9. **Settlement.**
   - Success: settled to the provider-reported usage.
   - Auth, 400, 404, 429 and context-window errors: released to 0.
   - Timeouts, 5xx, connection, malformed-response and unknown errors: the reservation is kept, because the provider may have billed the call.
10. **Store.** Answers from the selected model are cached. Fallback answers are not.
    If the paid embedding was refused or failed, the entry is exact-only and
    nothing is re-embedded.
11. **Response.** Answer, classification, routing reason, fallback attempts, usage,
    cost, cache status and latency, with the request id in the body and header.
12. **Record.** A history row and one row per provider attempt are queued and
    written to PostgreSQL by a background worker. This covers successes, provider
    failures and budget refusals.
13. **Observability.**
    - Metrics, structured log events and trace spans are emitted at each step.
    - Prompts, answers, vectors, keys and raw IPs are never included.
    - History, metrics and cache statistics are read through the protected
      observability endpoints.

## Project layout

```
distributed-llm-router/
├── backend/
│   ├── app/
│   │   ├── main.py              # create_app(): middleware, routers, lifespan
│   │   ├── api/                 # chat, classify, models, health, cache, analytics, metrics, deps (auth + rate limit)
│   │   ├── budget/              # guard.py (reservations, Redis/local stores), embedding.py (BudgetedEmbedder)
│   │   ├── cache/               # semantic.py (exact + similarity cache), equivalence.py (critical-token guard)
│   │   ├── classifier/          # rule-based classification engine (pure Python)
│   │   ├── core/                # config, errors, logging, production checks, security middleware, request id
│   │   ├── db/                  # SQLAlchemy models, migrations, background recorder, analytics queries
│   │   ├── embeddings/          # EmbeddingProvider, local hashing embedder, LiteLLM embedder
│   │   ├── infra/               # redis.py (pool), database.py (engine), container.py (composition root)
│   │   ├── observability/       # metrics, HTTP middleware, tracing, redaction
│   │   ├── providers/           # mock, LiteLLM adapter, catalog.py (startup gate), litellm_env.py (import guard)
│   │   ├── ratelimit/           # atomic Redis limiter + client identity
│   │   ├── routing/             # registry, policy, health (+ Redis store), service, usage, factory
│   │   └── schemas/             # Pydantic request/response models
│   ├── config/
│   │   ├── models.example.json            # example registry (placeholder prices, includes Ollama)
│   │   └── models.providers.example.json  # verified IDs and prices per provider, all disabled
│   ├── benchmarks/              # benchmark harness, A/B comparison, profiler, stored results
│   ├── tests/                   # pytest suite (fakes only; no paid calls)
│   └── pytest.ini
│   └── Dockerfile               # production API image (Step 11.2)
├── frontend/                    # React + Vite UI (Dockerfile: production static image)
├── infrastructure/              # Caddyfiles (edge + static), PostgreSQL init, backup tooling, deploy.env.example
├── docker-compose.yml           # development Redis + PostgreSQL (127.0.0.1 only)
├── docker-compose.prod.yml      # self-contained production stack (see DEPLOYMENT.md)
├── DEPLOYMENT.md                # production runbook
├── .dockerignore                # allowlist build context (no .env, tests or caches in images)
├── .env.example                 # every setting, development defaults
├── .env.production.example      # production API settings template (placeholders only)
├── requirements.txt             # pinned RUNTIME dependencies (installed in the image)
├── requirements-dev.txt         # runtime + test/dev tools (pytest, watchfiles)
└── requirements-observability.txt  # optional OTLP trace exporter
```

## Quick start (Windows CMD)

```cmd
cd C:\Users\ptshi\distributed-llm-router
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env

docker compose up -d
docker compose ps

cd backend
uvicorn app.main:app --reload
```

The defaults need no keys and make no paid calls. If Redis or PostgreSQL is
unreachable, the app still starts and logs it; the cache, shared health, rate
limits, Redis budgets and history then degrade as described in their sections.

Then open:
- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs (Swagger UI; off by default in production)

**Settings.**
- All settings are defined in `backend/app/core/config.py`. They are read from
  environment variables and the project-root `.env`.
- `.env.example` lists every setting with its development default.
- Settings validation errors never echo the submitted values.
- `LLMR_DISABLE_ENV_FILE=1` makes the app ignore `.env`. The test suite and
  benchmarks set it, so they never read your `.env`.

## Endpoints and authentication

| Method | Path | Purpose | Protection |
|---|---|---|---|
| GET | `/health`, `/api/v1/health` | Liveness (no Redis/PostgreSQL access) | public |
| POST | `/api/v1/classify` | Classify a query | public |
| POST | `/api/v1/chat` | Classify, route, answer | `CHAT_API_KEYS` bearer key when `CHAT_AUTH_MODE=api_key` (required in production); rate limited |
| GET | `/api/v1/models` | Registry, pricing labels and live health (no keys or `api_base`) | public |
| GET | `/metrics` | Prometheus metrics | `OBSERVABILITY_API_TOKEN` |
| GET | `/api/v1/analytics/summary` | Requests, tokens, cost, latency, cache, fallbacks (`?start=&end=`) | `OBSERVABILITY_API_TOKEN` |
| GET | `/api/v1/cache/stats` | Cache counts and settings (no cached content) | `OBSERVABILITY_API_TOKEN` |
| GET | `/api/v1/health/dependencies` (also `/health/dependencies`) | Redis/PostgreSQL reachability, persistence counters | `OBSERVABILITY_API_TOKEN` |
| DELETE | `/api/v1/cache[?reset_stats=true]` | Clear the response cache | `ADMIN_API_TOKEN` (disabled with 403 when unset) |

- **Three separate credentials:** chat keys, the observability token and the admin
  token. Settings reject any reuse between them, and none is accepted on another's
  endpoints.
- The observability endpoints are **open when `OBSERVABILITY_API_TOKEN` is unset**
  (development only). Production refuses to start without the token.
- Tokens and keys are compared in constant time and are never logged.
- `/metrics` exists only with `METRICS_ENABLED=true` (the default).
- Every response has an `X-Request-ID`. You can send your own: `[A-Za-z0-9._-]`,
  up to 64 characters.
- With tracing enabled, responses also carry `X-Trace-ID`.

## Activating real providers

Real calls need **all** of the following. Otherwise the app **refuses to start**,
in every environment:

1. `LLM_MOCK_MODE=false` **and** `ALLOW_REAL_PROVIDER_CALLS=true`.
2. `BUDGET_DAILY_USD` > 0.
3. A registry (`MODEL_REGISTRY_FILE`). Every **enabled** non-mock model needs:
   - a supported provider (`openai`, `anthropic`, `gemini`, `groq`, `mistral`, `ollama`);
   - a `provider_model` starting with `<provider>/`;
   - that provider's key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` or `MISTRAL_API_KEY`);
   - `pricing_is_placeholder: false` with non-zero prices (Ollama may be zero).

   Disabled entries are not checked and can never be selected.

Start from `backend/config/models.providers.example.json`, which has verified IDs
and prices and every entry disabled. Paid embeddings
(`EMBEDDING_PROVIDER=litellm`) have their own gate, which also applies in mock
mode (see [Embedding spend protection](#embedding-spend-protection-phase-5-step-8a)).

## Running tests

```cmd
cd C:\Users\ptshi\distributed-llm-router\backend
..\venv\Scripts\activate
python -m pytest -v -rs
```

- **No real or paid calls.** LiteLLM is replaced by injected fakes, and keys are
  obvious fake strings. The end-to-end tests also make any real LiteLLM use fail
  the test.
- **Isolated data.** Tests that use Docker services use Redis logical DB 15 with
  a random `test:` key prefix (only that prefix is deleted) and the separate
  PostgreSQL database `llm_router_test` (only its tables are truncated). The suite
  never creates databases: if `llm_router_test` does not exist, the database
  tests stop with an error that explains how to create it once.
- **Enforced isolation.** `tests/conftest.py` installs two guards for the whole
  run. Each refuses a forbidden target *before* any network I/O and fails the
  test that tried it, even if application code swallowed the error:
  - PostgreSQL: `llm_router`, `postgres`, `template0` and `template1` are refused
    (psycopg, SQLAlchemy and async connections alike).
  - Redis: every logical database except 15 is refused, including `SELECT`,
    `SWAPDB` and `MOVE` to another database.
- **Hermetic by default.** Unit tests turn Redis and PostgreSQL off. If the
  services aren't running, the tests that need them are **skipped** (listed by
  `-rs`), not failed. CI treats any skip as a failure (see below).
- **End-to-end tests.** `tests/test_e2e_lifecycle.py` drives the production wiring
  (`create_app` → container) over HTTP, both in mock mode and with fake real
  providers. It covers:
  - startup and every protected endpoint;
  - authentication and rate limits;
  - validation;
  - exact and similarity cache hits;
  - a reservation before every paid call, and settlement;
  - retries and fallback;
  - the combined per-request limit;
  - degradation when the embedding budget is refused or exhausted;
  - Redis-outage policies;
  - persistence, analytics, metrics and traces;
  - no leaks of prompts, keys or tokens.

## Continuous integration

GitHub Actions runs [.github/workflows/ci.yml](.github/workflows/ci.yml) on every
push to `main`, every pull request, and on demand. It uses mock providers only,
references no repository secrets and needs no provider API keys. The token is
read-only (`contents: read`), a newer push cancels the run in progress, and every
action is pinned to a full commit SHA.

| Job | What it checks |
|---|---|
| Repository hygiene | No `.env`/key/dump files tracked; `.gitignore` still protects them; no generated, cache or log files; no CRLF in the repository; no credential-shaped strings outside test fixtures |
| Backend tests | Python 3.13 with PostgreSQL 16 (application database `llm_router_test` only) and Redis 7 services; `LLM_MOCK_MODE=true`, `ALLOW_REAL_PROVIDER_CALLS=false`; isolation-guard self-test, then the full suite with a JUnit report and a **zero-skip gate** |
| Frontend checks | Node 22: `npm ci`, typecheck, lint, tests (JUnit and zero-skip gate), production build, bundle secret scan |
| OpenAPI drift | Regenerates `openapi.json` and `schema.gen.ts` in a temporary folder and fails if the committed files differ (run `npm run gen:api` to update them) |
| Dependency security | `pip-audit` fails on **any** known vulnerability (it reports no severity, so it cannot filter to high/critical); `npm audit` prints the full report and fails on high or critical |

The zero-skip gate exists because the suite skips service tests when Redis or
PostgreSQL is unreachable. In CI the services are always provided, so any skip
means something is broken, and the job fails with the skipped tests listed.

Dependabot ([.github/dependabot.yml](.github/dependabot.yml)) opens weekly
update pull requests for GitHub Actions, pip and npm. Each one runs the full CI
before review.

### Running the CI checks locally

Start the services first (`docker compose up -d`), and create `llm_router_test`
once if it doesn't exist yet. Then:

```cmd
:: Backend: same environment as CI, JUnit report outside the repository
cd C:\Users\ptshi\distributed-llm-router\backend
set LLM_MOCK_MODE=true
set ALLOW_REAL_PROVIDER_CALLS=false
..\venv\Scripts\python -m pytest -ra --junitxml=%TEMP%\backend-junit.xml -o junit_family=xunit2

:: Frontend
cd ..\frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm run check:bundle

:: Dependency audits (pip-audit in a separate virtualenv; not a project dependency)
npm audit --audit-level=high
%TEMP%\auditenv\Scripts\pip-audit -r ..\requirements.txt -r ..\requirements-observability.txt -r ..\requirements-dev.txt --no-deps --disable-pip
```

`-ra` lists every skipped test at the end. Locally a skip only means a service
isn't running; in CI it fails the build.

## Benchmarks

```cmd
cd C:\Users\ptshi\distributed-llm-router\backend
python -m benchmarks.run --quick                                   :: small sanity run
python -m benchmarks.run                                           :: default groups
python -m benchmarks.run --scenarios micro,cache --requests 1000 --concurrency 20
python -m benchmarks.run --scenarios http                          :: real HTTP (spawns uvicorn processes)
python -m benchmarks.ab --a <baseline backend copy> --b . --scenarios cache --rounds 3
python -m benchmarks.profile --config cache --requests 300
```

- **Scenario groups:** `micro`, `inprocess`, `metrics`, `logging`, `tracing`,
  `cache`, `redis`, `postgres`, `multi` (the defaults), plus `http`.
- **Results:** `backend/benchmarks/results/<UTC timestamp>.json`, plus `latest.json`.
- **Isolation:** benchmarks run in mock mode and use the same data isolation as
  the tests (Redis DB 15 with a `bench:` prefix, PostgreSQL `llm_router_test`).
  Groups whose service is unreachable are skipped.
- **A/B rule:** `benchmarks.ab` alternates two source trees. It reports a change
  only when the per-round ranges don't overlap **and** the median moves by at
  least `--min-effect` percent (default 15).

## Observability

**Logs.**
- `LOG_FORMAT=text` (default) or `json` (one object per line).
- Every record carries the request id and, when a trace is active, the trace id.
- Structured events use the form `event key=value`.
- `ACCESS_LOG_ENABLED` adds one `http.request` event per request: method, route
  template, status, duration, client hash and error category.
- Known secret values and key patterns are redacted from every log record.
- Prompts, answers, vectors, headers, query strings and raw IPs are never logged.
- `CLIENT_ID_HASH_KEY` (an HMAC key; required in production) keys the client hashes
  used in logs, rate-limit keys and database rows.

**Metrics** (`GET /metrics`, Prometheus text format; every label has a bounded set of values):

| Metric | Labels |
|---|---|
| `llm_router_build_info` | version, mock_mode |
| `llm_router_http_requests_total` | method, route, status |
| `llm_router_http_request_duration_seconds` | method, route |
| `llm_router_http_requests_in_progress` | – |
| `llm_router_errors_total` | category, code |
| `llm_router_chat_requests_total` | complexity, selected_model, model_used, cache_status, outcome |
| `llm_router_provider_attempts_total` | model, provider, outcome, error_kind |
| `llm_router_provider_latency_seconds` | model, provider, outcome |
| `llm_router_retries_total` | model |
| `llm_router_fallbacks_total` | from_model, to_model |
| `llm_router_cache_lookups_total` | status |
| `llm_router_cache_hit_ratio` | – (per process; prefer `rate()` on lookups) |
| `llm_router_tokens_total` | model, direction, source |
| `llm_router_estimated_cost_usd_total` | model, pricing |
| `llm_router_rate_limit_rejections_total` | scope, window |
| `llm_router_budget_rejections_total` | reason |
| `llm_router_embedding_budget_events_total` | event, reason |
| `llm_router_embedding_estimated_cost_usd_total` | – |

Route labels are templates (unmatched paths become `unmatched`), so labels stay
bounded. Chat requests refused by a spend limit are counted with
`outcome="error"`.

**Tracing** (OpenTelemetry; off by default).
- Enable with `TRACING_ENABLED=true` and choose `TRACING_EXPORTER`: `memory`
  (tests), `console` or `otlp`.
  - `otlp` needs `pip install -r requirements-observability.txt` and
    `TRACING_OTLP_ENDPOINT`.
  - Sampling is set by `TRACING_SAMPLE_RATIO`.
- **Spans:**
  - `chat`
  - `classify`
  - `health.view`
  - `route.select`
  - `ratelimit.check`
  - `cache.lookup` / `cache.exact_get` / `cache.semantic_search` / `cache.lock` / `cache.store`
  - `embedding.generate`
  - `provider.chain` / `provider.attempt`
  - `health.record`
  - `db.persist_batch`
  - `db.analytics_summary`
- Span attributes come from an allow-list: no prompts, answers, headers, keys,
  addresses or exception messages.
- An incoming `traceparent` becomes the parent only with
  `TRACING_TRUST_INCOMING=true`. Otherwise it is attached as a link.

## Example health response

```json
{
  "status": "ok",
  "app_name": "Distributed LLM Router",
  "version": "0.1.0",
  "environment": "development",
  "timestamp": "2026-11-08T12:00:00.000000Z",
  "uptime_seconds": 12.345
}
```

## Query classification (Phase 2)

`POST /api/v1/classify` classifies a query with a deterministic, rule-based
engine. It needs no API keys and makes no LLM calls.

```cmd
curl -X POST http://127.0.0.1:8000/api/v1/classify -H "Content-Type: application/json" -d "{\"query\": \"Compare REST and GraphQL for a mobile app backend.\"}"
```

Response (abridged):

```json
{
  "complexity": "MEDIUM",
  "confidence": 0.877,
  "complexity_score": 0.382,
  "thresholds": {"simple_max": 0.28, "complex_min": 0.55},
  "intent": {"primary": "analysis", "confidence": 0.99, "alternatives": []},
  "reasoning": {"score": 0.382, "estimated_steps": 2, "signals": [
    {"name": "intent_prior", "weight": 0.2, "detail": "baseline demand of 'analysis' intent"},
    {"name": "reasoning_marker", "weight": 0.08, "detail": "comparison of alternatives"},
    {"name": "technical_depth", "weight": 0.09, "detail": "technical terms: backend, graphql, rest"}
  ]},
  "technical": {"is_technical": true, "is_coding": false, "domains": ["web"], "...": "..."},
  "features": {"word_count": 9, "question_count": 0, "has_code": false, "...": "..."},
  "explanation": "Classified as MEDIUM (score 0.38; ...). Primary intent: analysis ...",
  "classifier_version": "rule-based-1.0"
}
```

**How it works**

1. **Features**: length, sentences, questions, list items, code blocks and code lines.
2. **Technical detection**: whole-word matches against 8 domain vocabularies
   (programming, web, databases, devops, ai_ml, systems, security, algorithms).
   A query counts as *coding* if it contains code or is a coding request.
3. **Intent**: weighted patterns for 13 intents. An imperative opening verb
   ("Design…", "Compare…", "Fix…") gets a bonus because it states the main goal.
4. **Reasoning complexity**: the score is the sum of capped signals
   (intent baseline, length, reasoning markers, multi-part structure,
   constraints, technical depth, code, and simplicity markers that lower it), clamped to [0, 1].
   Every signal is returned, so each point of the score can be traced to a reason.
5. **Level**: `score < 0.28` → SIMPLE, `score ≥ 0.55` → COMPLEX, otherwise MEDIUM.
   Set `CLASSIFIER_SIMPLE_THRESHOLD` / `CLASSIFIER_COMPLEX_THRESHOLD` in `.env` to change these.
6. **Confidence**: `0.50 + 0.35 × boundary_margin + 0.14 × intent_confidence`.
   It is high when the score sits well inside its band and the intent is clear,
   and drops toward ~0.55 when the score is near a threshold.

Intents: `conversation`, `question_answering`, `explanation`, `code_generation`,
`debugging`, `analysis`, `system_design`, `planning`, `math`, `creative_writing`,
`summarization`, `translation`, `general`.

## Multi-model routing (Phase 3)

### Architecture

```
POST /api/v1/chat
   │
   ▼
ChatService ──► QueryClassifier (Phase 2) ──► SIMPLE / MEDIUM / COMPLEX + intent + coding
   │
   ├──► RoutingPolicy ──► ModelRegistry (JSON) + HealthTracker
   │        selects the primary model and builds a fallback chain
   │
   ├──► for each model in [primary, *fallbacks]:
   │        ProviderPool ──► MockProvider | LiteLLMProvider   (common LLMProvider interface)
   │        retry transient errors with exponential backoff, enforce timeout
   │        record health success/failure
   │
   └──► usage (reported / estimated) ──► cost estimate ──► ChatResponse
```

Each layer is small and replaceable. `providers/` has no routing logic,
`routing/` doesn't import FastAPI (except error types), and the API layer only
validates input and maps results to Pydantic schemas.

### Model registry

Models are defined in JSON, never in code. The built-in registry
(`backend/app/routing/default_models.json`) has 5 **mock** models with
**placeholder pricing**:

| id | tier | capabilities | enabled | fallbacks |
|----|------|--------------|---------|-----------|
| mock-fast | SIMPLE | chat | ✅ | mock-balanced |
| mock-balanced | MEDIUM | chat | ✅ | mock-advanced, mock-fast |
| mock-coder | MEDIUM | chat, coding | ✅ | mock-balanced, mock-advanced |
| mock-advanced | COMPLEX | chat, coding, reasoning, long_context | ✅ | mock-balanced |
| mock-legacy | SIMPLE | chat | ❌ | – |

Fields: `id`, `provider`, `provider_model` (LiteLLM `provider/model` format),
`tier`, `input_cost_per_million`, `output_cost_per_million`,
`pricing_is_placeholder`, `max_context_tokens`, `max_output_tokens`,
`timeout_seconds`, `enabled`, `capabilities`, `priority`, `fallbacks`,
`api_base` (optional), `description`.

The registry is validated at startup, so a bad file stops the app with a clear
error. It rejects duplicate ids, unknown or self-referencing fallbacks,
`max_output_tokens > max_context_tokens`, negative prices and unknown fields.

**Using real models:** copy `backend/config/models.providers.example.json` (verified
IDs and prices, all disabled) or `backend/config/models.example.json` (placeholder
prices, includes a keyless local Ollama model) to `backend/config/models.json`.
Re-check each provider's *current* prices, set `pricing_is_placeholder: false` and
`enabled: true` for the models you want, and set
`MODEL_REGISTRY_FILE=backend/config/models.json`. Then complete **every** step in
[Activating real providers](#activating-real-providers): `LLM_MOCK_MODE=false`,
`ALLOW_REAL_PROVIDER_CALLS=true`, `BUDGET_DAILY_USD` > 0, and the provider keys.
Otherwise the app refuses to start.

### Routing workflow

1. **Override:** if `model` is given it must be registered (`400 unknown_model`)
   and enabled (`400 model_disabled`). By default an override doesn't fall back
   (`FALLBACK_ON_OVERRIDE=false`).
2. **Tier:** SIMPLE → economical/fast, MEDIUM → balanced, COMPLEX → advanced.
3. **Candidates:** enabled models in the tier that fit the context window
   (estimated input + max output ≤ `max_context_tokens`), healthy ones first.
4. **Ranking:** first by fewest missing *preferred capabilities* (`coding` for
   coding queries, `reasoning` for COMPLEX, `long_context` for large inputs), then
   by strategy: `priority` (lowest number) or `lowest_cost` (input + output price).
5. **Escalation:** if a tier has no usable model, try higher tiers, then lower
   ones (`ROUTING_TIER_ESCALATION`).
6. The decision, with a written `reason` and the ranked `candidates`, is
   returned in `routing`.

### Fallback workflow

```
primary ──fail──► configured fallbacks ──► other models (same tier, higher, lower)
                  (deduplicated, never the primary, capped at MAX_FALLBACK_MODELS)
```

| Error kind | Retry same model? | Fall back? | Counts against health? |
|------------|:-:|:-:|:-:|
| rate_limit, unavailable (5xx), connection | ✅ (`PROVIDER_MAX_RETRIES`, exponential backoff) | ✅ | ✅ |
| timeout | ❌ (don't double the wait) | ✅ | ✅ |
| auth, not_found, invalid_response, unknown | ❌ | ✅ | ✅ |
| bad_request, context_window | ❌ | ✅ | ❌ |
| content_policy | ❌ | ❌ (`422 request_rejected_by_provider`) | ❌ |

Fallback can't loop: the chain is a finite, deduplicated list. If every model
fails, the response is `503 all_models_failed`, listing each attempt's model,
outcome and error kind. Raw provider messages and tracebacks are never returned.

**Health:** after `HEALTH_FAILURE_THRESHOLD` consecutive failures a model becomes
`unhealthy` and is skipped for `HEALTH_COOLDOWN_SECONDS`. After that, one request
is allowed through to test it, and a success resets it. Health is shared through
Redis by default (`HEALTH_BACKEND=redis`, see
[Distributed provider health](#distributed-provider-health)); `memory` keeps it per
process. See `GET /api/v1/models`.

**Demo fallback in mock mode:** add to `.env` and restart:

```
MOCK_FAILURES={"mock-fast": "unavailable"}
```

Modes: `timeout`, `unavailable`, `rate_limit`, `connection`, `auth`,
`bad_request`, `content_policy`, `flaky` (fails every other call).

### Usage and cost

- Token counts reported by the provider are used as-is: `source: "provider_reported"`.
- Missing counts are **estimated** (~4 chars/token) and labelled `estimated` or
  `partially_estimated`. Set `ESTIMATE_MISSING_USAGE=false` to report
  `unavailable` (null) instead. The mock provider doesn't report usage, so mock
  responses always show `estimated`.
- Cost = tokens × price / 1,000,000 for the model **actually used**.
  `pricing_is_placeholder` and `is_estimate` are always included.

### Example

```cmd
curl -X POST http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" -d "{\"query\": \"Explain Python variables\"}"
```

```json
{
  "request_id": "3b770982ba10492aaa4dab03a438b276",
  "answer": "[MOCK:mock-fast] Deterministic mock answer for a 3-word prompt (fingerprint 1dd3cf8fe5db). ...",
  "classification": { "complexity": "SIMPLE", "confidence": 0.989, "intent": {"primary": "explanation", "...": "..."}, "...": "..." },
  "selected_model": "mock-fast",
  "model_used": "mock-fast",
  "provider": "mock",
  "provider_adapter": "mock",
  "provider_model": "mock/fast-v1",
  "finish_reason": "stop",
  "routing": {
    "selected_model": "mock-fast", "requested_tier": "SIMPLE", "routed_tier": "SIMPLE",
    "strategy": "priority", "override": false,
    "reason": "Query classified as SIMPLE (confidence 0.99). Selected 'mock-fast' using the 'priority' strategy among 1 eligible model(s) in SIMPLE tier.",
    "candidates": ["mock-fast"], "preferred_capabilities": []
  },
  "fallback": {
    "used": false, "original_model": "mock-fast", "final_model": "mock-fast",
    "chain": ["mock-fast", "mock-balanced", "mock-coder", "mock-advanced"],
    "attempts": [{"attempt_number": 1, "model_id": "mock-fast", "provider": "mock", "outcome": "success",
                  "error_kind": null, "retryable": null, "latency_ms": 0.16}],
    "reason": null
  },
  "usage": {"input_tokens": 6, "output_tokens": 50, "total_tokens": 56, "source": "estimated"},
  "cost": {"input_cost": 6e-07, "output_cost": 2e-05, "total_cost": 2.06e-05, "currency": "USD",
           "input_cost_per_million": 0.1, "output_cost_per_million": 0.4,
           "pricing_is_placeholder": true, "is_estimate": true},
  "latency_ms": 0.91,
  "provider_status": "healthy",
  "mock_mode": true
}
```

Optional request fields: `model` (override), `temperature` (0–2),
`max_output_tokens` (1–32000, clamped to the model's limit). Unknown fields
are rejected with `422`.

### Observability

Structured `event key=value` logs with the request id: `chat.request_received`,
`chat.classified`, `chat.model_selected`, `provider.invoke`, `provider.success`,
`provider.failure`, `chat.retry`, `chat.fallback_attempt`,
`chat.fallback_skipped_unhealthy`, `chat.fallback_blocked`, `chat.completed`
(latency, tokens, cost) and `chat.all_models_failed`.

Query text is never logged, only its length. API keys are stored as `SecretStr`
and passed only to LiteLLM. Provider error messages are redacted (known key
values, `sk-…`, `AIza…`, bearer tokens) before logging.

## Distributed infrastructure (Phase 4)

### Architecture and data flow

```
POST /api/v1/chat
  │
  ├─ RequestIdMiddleware ── X-Request-ID (all logs + DB rows)
  ├─ Rate limit (Redis Lua, per client) ──► 429 + Retry-After
  ├─ Classify (Phase 2)
  ├─ Health view (Redis HGETALL per model, shared) ──► RoutingPolicy.select
  ├─ Cache lookup (scope = model + provider + temperature + max tokens + mock mode + embedder)
  │     exact SHA-256 key ──hit──► respond (no provider call)
  │     embedding + cosine ──hit──► respond (no provider call)
  │     miss ──► per-query lock (stampede guard)
  ├─ Provider chain (Phase 3) + per-model limits + recovery probes
  │     record success/failure ──► Redis health (atomic Lua)
  ├─ Store answer in cache (primary model only, never fallback answers)
  └─ Enqueue history record ──► background worker ──► PostgreSQL (thread pool)
```

Each instance owns **one** Redis connection pool and **one** SQLAlchemy engine,
created at startup and closed at shutdown. Everything shared lives in Redis or
PostgreSQL, so instances hold no distributed state of their own.

### Redis key design

All keys start with `REDIS_KEY_PREFIX` (default `llmr:`).

| Key | Type | Purpose |
|-----|------|---------|
| `cache:{scope}:e:{sha256}` | STRING | JSON answer + metadata (TTL); the exact-match key **is** the entry. **No prompt text.** |
| `cache:{scope}:z` | ZSET | query hash → expiry time (index, TTL pruning, size cap) |
| `cache:{scope}:v` | HASH | query hash → float32 embedding (semantic mode only) |
| `cache:{scope}:g` | HASH | query hash → critical-token signature digest (semantic mode only) |
| `cache:{scope}:l:{sha256}` | STRING | stampede lock (PX) |
| `cache:{scope}:s` | HASH | per-scope hit/miss/store counters |
| `cache:scopes`, `cache:stats` | SET, HASH | scope index; legacy global counters (still read, no longer written) |
| `health:{model}` | HASH | consecutive/total failures, successes, last error kind and time (TTL) |
| `health:probe:{model}` | STRING | recovery-probe lock (one instance at a time) |
| `rl:client:{client hash}:{minute,hour}` | STRING | fixed-window counters; the client (API-key name or IP) is stored only as a hash (HMAC with `CLIENT_ID_HASH_KEY`) |
| `rl:model:{model}:minute` | STRING | per-model counters (`MODEL_RATE_LIMITS`) |
| `budget:{YYYYMMDD}:global:usd` | STRING | global spend for the UTC day, integer micro-USD (chat and paid embeddings) |
| `budget:{YYYYMMDD}:client:{client hash}:usd`, `…:tokens` | STRING | per-client spend and tokens for the UTC day; all budget keys expire 2 days after their last write |

Cache schema v2 (Phase 5 step 6B): the entry is stored under its exact key and
every scope's keys share the `{scope}` hash tag. Each step is one Redis round trip:
a counted read, a double-checked lock (re-reads the entry before taking the lock),
and one transaction that stores, prunes and releases. With the default
configuration, a cache miss costs 6 round trips per request (was 10) and an
exact hit costs 3 (was 5). Entries written with schema v1 are not read; the
first request after an upgrade simply misses.

### Response cache: exact and semantic

1. **Exact match (always on):** SHA-256 of the whitespace-normalised query, within
   the same *scope*. Case and punctuation are kept, so only identical text matches.
2. **Similarity match (OFF by default since Phase 5 step 6A):** the query is
   embedded and compared by cosine similarity with stored vectors in the same
   scope. A candidate becomes a hit only if **all** of these are true:
   - `CACHE_SEMANTIC_ENABLED=true`, **and**
   - the embedder is meaning-aware (`EMBEDDING_PROVIDER=litellm`), **or** you
     explicitly accept the lexical embedder's risks with
     `CACHE_SEMANTIC_ALLOW_LEXICAL=true` (logged as `cache.semantic_lexical_unsafe`), **and**
   - the query contains word content (empty, punctuation-only or emoji-only
     queries are exact-only), **and**
   - the **equivalence guard** passes: both queries have the same ordered
     *critical tokens* (numbers, versions, IPs, dates, sizes, `+ - * / = < > %`,
     negations, enable/disable-style polarity words, comparisons, number words,
     months, weekdays, relative dates), **and**
   - cosine similarity ≥ `CACHE_SIMILARITY_THRESHOLD`.
3. **Miss:** a per-query lock (`SET NX PX`) makes identical concurrent requests
   wait for the first one's answer instead of all calling the provider.

> **Why similarity matching is off by default.** Measured with the local
> embedder at the 0.92 threshold, **14 of 16 meaning-changing edits** to a
> long prompt still scored ≥ 0.92, so the old behaviour would have served a
> cached answer to a *different* question. Examples:
>
> | Edit | Similarity |
> |------|-----------:|
> | "safe" → "**not** safe" | 0.982 |
> | "enable" → "**disable**" TLS | 0.970 |
> | 100 → **1000** requests per minute | 0.978 |
> | "1200 **+** 34" → "1200 **−** 34" | **1.000** |
> | 3.5 → **35** seconds | 0.966 |
> | March → **April** 3 2024 | 0.967 |
> | Python 3.12 → **3.13** | 0.972 |
> | port 8080 → **8443** | 0.958 |
> | **Python** → **Java** function | 0.949 |
> | PostgreSQL→MySQL vs **MySQL→PostgreSQL** | 0.978 |
>
> The equivalence guard rejects every numeric, operator, negation, polarity
> and date change above. It **cannot** detect changed entities or word order
> (the last two rows), which is why approximate hits stay disabled unless a
> meaning-aware embedder is configured. Even then the guard still applies,
> but real embedding models can also score entity swaps highly, so test with
> your own traffic before enabling.
>
> The previous claim in this README that 0.92 "only matches near-duplicates"
> was based on short queries and was **wrong for long prompts**.

**Stored data:** a semantic entry adds a 16-character SHA-256 digest of its
critical-token sequence (`cache:{scope}:g`). Prompts and tokens are never stored.
Entries written before step 6A have no signature and never semantically match.
`/cache/stats` reports the effective `semantic_enabled`, `embedding_is_semantic`
and a note explaining why approximate hits are on or off.

A **scope** is everything that makes an answer reusable: model id, provider,
provider model, temperature, effective max output tokens, mock mode and
embedder id. A request routed to a different model, or with different
generation settings, never sees another scope's entries. An explicit override
of the *same* model shares the scope, because the answer is equally valid.

Cache hits return `cache.status` = `hit_exact` or `hit_semantic` along with the
similarity, the entry's age and the original request id. Usage shows
`source: "cache_hit"` with 0 tokens and 0 cost, because no provider was called.
Answers that came from a **fallback** model are not cached. Send
`"use_cache": false` to skip the cache for a request.

Redis 7 has no vector index without the RediSearch module, so similarity is
computed in Python over one scope's vectors. That's fine for thousands of
entries per scope. For millions, use Redis Stack or pgvector (see limitations).

### Embedding providers

`EmbeddingProvider` interface: `embed(text) → normalised vector`, plus `id`
(the vector space; vectors are only compared within the same id) and
`is_semantic`.

| Provider | `EMBEDDING_PROVIDER` | Semantic? | Notes |
|----------|---------------------|-----------|-------|
| `LocalHashingEmbedder` | `local` (default) | **No: lexical** | Deterministic hashing of words, bigrams and character trigrams. No keys, used in tests. |
| `LiteLLMEmbedder` | `litellm` | Yes | Any LiteLLM embedding model (`EMBEDDING_MODEL`). Not tested against a live provider. |

**What the local embedder can and can't do (measured):**

| Pair | Similarity |
|------|-----------:|
| "Explain Python variables" vs "explain python variables!" | 1.000 |
| vs "Explain Python variables please" | 0.876 |
| vs "Can you explain Python variables?" | 0.811 |
| vs "Explain JavaScript variables" | 0.620 |
| "How do I reverse a **list** in Python?" vs "…reverse a **string**…" | 0.797 |
| "How do I reverse a list in Python?" vs "How can I reverse a list in Python?" | 0.815 |

Shared wording drives the score, not meaning, so no threshold separates
"different question" from "paraphrase". Short paraphrases often score *below*
0.92 (missed hits), while long prompts with one critical change score *above*
it (wrong hits). See the step 6A table above. For real paraphrase matching,
configure a LiteLLM embedding model, keep the equivalence guard, and tune the
threshold on your own traffic. `/cache/stats` reports `embedding_is_semantic`
so this is always visible.

### Distributed provider health

Health is stored in Redis and shared by every instance
(`HEALTH_BACKEND=redis`). Updates are single **Lua scripts** that use Redis's
own clock, so concurrent failures on several instances are counted exactly
and cooldowns agree.

- `HEALTH_FAILURE_THRESHOLD` consecutive failures make a model **unhealthy**:
  it's skipped by routing and fallback on every instance.
- After `HEALTH_COOLDOWN_SECONDS` the model allows a recovery probe. Exactly
  one instance wins the probe lock and calls it; the others skip it. A success
  resets it to healthy; a failure starts another cooldown.
- State changes log `health.state_change` with the old and new state.
- **If Redis is unavailable,** the store falls back to process-local tracking and logs
  `health.redis_unavailable` once. This is safe because health only orders and skips
  candidates; the worst case is calling a model another instance already knows is
  down. Local fallback state isn't merged back into Redis when it recovers.

### Distributed rate limiting

- `/api/v1/chat` is limited per client: `RATE_LIMIT_PER_MINUTE`,
  `RATE_LIMIT_PER_HOUR` (0 disables a window). Health, classify, models, cache
  and analytics endpoints aren't limited.
- All windows are checked and incremented in **one Lua script**, so many
  instances can't overshoot, and rejected requests don't use up quota.
- Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
  `X-RateLimit-Reset` and `X-RateLimit-Window`. A `429` adds `Retry-After`.
- **Client identity:** with `CHAT_AUTH_MODE=api_key`, the API-key **name**: all of a
  client's IPs share one quota, and authentication runs first, so a 401 costs no
  quota. Otherwise, by default, it is the direct peer IP. `X-Forwarded-For` is
  only used when `TRUST_PROXY_HEADERS=true` **and** the peer is listed in
  `TRUSTED_PROXIES`. The right-most untrusted hop is then taken as the client, so
  a spoofed left-most value is ignored.
- Keys hold a hash of the identity (HMAC-SHA-256 when `CLIENT_ID_HASH_KEY` is set),
  never the raw IP or key name.
- `MODEL_RATE_LIMITS={"mock-advanced": 30}` caps calls to a model across
  all instances. An over-limit model is skipped (`skipped_rate_limited`) and
  the fallback chain continues.
- **If Redis is unavailable:** `RATE_LIMIT_FAIL_OPEN=true` (default) allows requests
  and logs a warning; `false` rejects them with `503 rate_limiter_unavailable`.

### PostgreSQL persistence

Tables (created by the built-in migration runner on startup):

| Table | Contents | Indexes |
|-------|----------|---------|
| `request_log` | request id, UTC time, status and error code, client hash, query length, classification, selected and used model, provider, override, fallback, cache status and similarity, input/output/total tokens and their source, input/output/total cost, placeholder-pricing flag, latency | `created_at`, `request_id`, (`model_used`, `created_at`), (`complexity`, `created_at`), (`cache_status`, `created_at`), (`status`, `created_at`) |
| `provider_attempt` | one row per attempt: model, provider, outcome (success / error / skipped_*), error kind, retryable, latency | `request_log_id`, (`provider`, `outcome`), (`model_id`, `created_at`) |
| `schema_migrations` | applied migration versions | primary key |

- **Privacy:** `prompt_text` and `response_text` stay NULL unless
  `PERSIST_PROMPTS` / `PERSIST_RESPONSES` are true. Clients are stored only as
  a hash.
- **Doesn't block the event loop:** requests only add a record to a queue. A
  background worker writes batches using SQLAlchemy in a worker thread.
  (SQLAlchemy's async engine needs `greenlet`, which isn't installed; threads
  avoid a new dependency.) If the database is down or the queue is full,
  records are dropped, counted and logged, and the user request still succeeds.
- **Migrations:** there's no Alembic in this project. `app/db/migrations.py` is
  a versioned runner protected by a PostgreSQL advisory lock, so instances
  starting at the same time can't race. It only adds objects. For a future
  change, add a new numbered migration with explicit DDL, or adopt Alembic and
  mark the current version as applied.
- **What is recorded:**
  - successful requests, including cache hits (0 provider attempts);
  - provider failures (`all_models_failed`, `request_rejected_by_provider`);
  - requests refused by a spend limit (`budget_exceeded`, `budget_unavailable`), with
    their `skipped_budget` attempts.
- **What is not recorded:** 401, rate-limit 429, 422 validation errors, and
  unknown or disabled model overrides. This keeps abuse from flooding the
  database.

### Analytics

```cmd
curl -H "Authorization: Bearer <OBSERVABILITY_API_TOKEN>" "http://127.0.0.1:8000/api/v1/analytics/summary?start=2026-11-01T00:00:00Z&end=2026-12-01T00:00:00Z"
```

(The header is needed only when `OBSERVABILITY_API_TOKEN` is set. That is always
the case in production.)

Returns total, successful and failed requests; input, output and total tokens;
estimated total cost; average latency; cache hits, misses and hit rate; fallback
count and rate; requests by complexity; requests by model (with tokens, cost and
latency); and provider attempt successes, failures and skips. `start` is
inclusive and `end` exclusive; times without a timezone are treated as UTC.
Returns `503` when the database is unavailable.

### Cache management and admin access

- `GET /api/v1/cache/stats` returns counts only, never prompts or answers.
- `DELETE /api/v1/cache[?reset_stats=true]` requires
  `Authorization: Bearer <ADMIN_API_TOKEN>`. **If `ADMIN_API_TOKEN` isn't
  set, the endpoint is disabled (`403`).** This is a single shared static token
  compared in constant time; it is *not* user authentication, has no roles and
  can't be rotated without a restart. Keep it secret and behind TLS.
- `/analytics/summary`, `/cache/stats`, `/metrics` and `/health/dependencies`
  require `Authorization: Bearer <OBSERVABILITY_API_TOKEN>` when that token is set
  (Phase 5). The token is required in production. Without it (development) they are
  open, but expose only aggregates.

### Running with Docker

```cmd
cd C:\Users\ptshi\distributed-llm-router
docker compose up -d
docker compose ps
```

Redis (6379) and PostgreSQL (5433), bound to 127.0.0.1, with health checks and
persistent volumes. Tables are created in `llm_router` on the first app start
(`DATABASE_AUTO_MIGRATE=true`). For production, see
[Docker](#docker) under Production configuration.

### Multi-instance deployment

Start two instances in two CMD windows. They share Redis and PostgreSQL:

```cmd
cd C:\Users\ptshi\distributed-llm-router\backend
..\venv\Scripts\activate
uvicorn app.main:app --port 8000
```
```cmd
cd C:\Users\ptshi\distributed-llm-router\backend
..\venv\Scripts\activate
uvicorn app.main:app --port 8001
```

A query answered on port 8000 is a cache hit on 8001. A model failing on one
instance is skipped by both. Rate limits are shared, and
`/api/v1/analytics/summary` on either port shows all history. Behind a load
balancer, set `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXIES=["<lb ip>"]`.
Use multiple processes, not `--workers` with `--reload`.

### Testing

See [Running tests](#running-tests) for the current command (`python -m pytest -v -rs`).

- Phase 1–3 tests run without Docker: a fixture turns Redis and the database
  off unless a test opts in.
- Phase 4 tests use the real Docker services **in isolation**: Redis logical
  database 15 with a random key prefix per test (only that prefix is deleted),
  and a separate PostgreSQL database `llm_router_test` (created automatically,
  only its tables are truncated). Your development data and volumes are never
  touched.
- If Redis or PostgreSQL isn't running, those tests are **skipped** (shown
  with `-rs`), not failed. No paid APIs are called.

### Observability events (Phase 4)

`cache.hit`, `cache.miss`, `cache.error`, `cache.stored`, `cache.lock_wait`,
`cache.cleared`, `embedding.generated`, `embedding.failed`, `ratelimit.rejected`,
`ratelimit.model_rejected`, `ratelimit.redis_unavailable`, `health.state_change`,
`health.probe_acquired`, `health.redis_unavailable`, `db.persisted`,
`db.persist_failed`, `db.persist_dropped`, `db.migration_applied`,
`analytics.query`, `admin.unauthorized`, `redis.started`, `db.started`.
All carry the request id. None contain prompts, answers, raw IPs or keys.

### Known limitations (Phase 4)

1. **The local embedder is lexical, not semantic** (see the table above).
   Real paraphrase matching needs `EMBEDDING_PROVIDER=litellm`, which isn't
   tested against a live provider.
2. **Similarity search is brute force** over one scope's vectors in Python
   (Redis 7 has no vector index). It's capped per scope; it isn't designed for
   very large caches.
3. **The stampede guard covers identical queries only.** Near-duplicate queries
   arriving at the same moment may each call the provider.
4. **Fixed-window rate limits** can allow up to 2× the limit across a window boundary.
5. **The admin token is a single static secret,** not user authentication.
   (Analytics and stats endpoints are now protected by `OBSERVABILITY_API_TOKEN`
   since Phase 5; that is also a static shared token.)
6. **Persistence is best-effort:** records are dropped (and counted) if the
   database is down or the queue is full; there's no retry or outbox.
7. **Local health during a Redis outage isn't merged back** into Redis afterwards.
8. **Cached answers are stored in Redis in plain text** until their TTL expires.
   Secure Redis (password, network isolation) in production; the Compose Redis has no password.
9. **Retention:** `request_log` grows without limit; there's no cleanup job yet.
10. **Migrations are hand-rolled.** Adopting Alembic is recommended before complex
    schema changes.

## Production configuration and hardening (Phase 5 step 7)

### Startup validation
With `ENVIRONMENT=production`, the API **refuses to start** (`ProductionConfigError`)
when any of the following is true. Messages name the setting and the problem,
**never the value**.

| Setting | Requirement |
|---|---|
| `ADMIN_API_TOKEN`, `OBSERVABILITY_API_TOKEN`, `CLIENT_ID_HASH_KEY` | set, ≥ 32 characters, not a placeholder, not low-variety |
| `POSTGRES_PASSWORD` | set, ≥ 16 characters, not the development default `router_password` (when the database is enabled) |
| `REDIS_PASSWORD` | set, ≥ 16 characters (when Redis is enabled) |
| `DEBUG` | `false` |
| `CORS_ALLOWED_ORIGINS` | no `*` |
| `TRUST_PROXY_HEADERS` / `TRUSTED_PROXIES` | proxies must be listed, and not `0.0.0.0/0` or `::/0` |
| `CACHE_SEMANTIC_ALLOW_LEXICAL` | `false` (see step 6A) |
| `OBSERVABILITY_API_TOKEN` | different from `ADMIN_API_TOKEN` |
| `CHAT_AUTH_MODE` / `CHAT_API_KEYS` | `api_key`, every key ≥ 32 characters and not a placeholder (added in step 8) |

Warnings, logged but not fatal: docs enabled, HSTS off, rate limiting off or
fail-open, `TRACING_TRUST_INCOMING`, raw prompt/response persistence, mock mode,
DEBUG log level. With `ENVIRONMENT=staging` every check is logged as a warning.
Development and test behaviour is unchanged.

Template: `.env.production.example` (placeholders only; keep the real file
outside the repository).

### HTTP security
- **Body size limit** (`MAX_REQUEST_BODY_BYTES`, default 1 MiB): a declared
  `Content-Length` over the limit returns `413 request_too_large` without reading
  the body. Chunked bodies without `Content-Length` are counted as they stream and
  cut off at the limit. (Before step 7, a 5 MB body was read in full and echoed
  back in the 422 response.)
- **Validation errors (422) no longer echo the submitted value.** `details`
  contains `type`, `loc`, `msg` and primitive `ctx` only. This is the one approved
  contract change in this step.
- **Security headers** on every response, including errors, 413 and 500:
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, COOP/CORP `same-origin`, `Permissions-Policy`, and a
  strict `Content-Security-Policy` (relaxed only on `/docs` and `/redoc`).
  `/api/*` responses also get `Cache-Control: no-store`. HSTS is sent only when
  `HSTS_ENABLED=true`.
- **CORS:** no CORS headers unless `CORS_ALLOWED_ORIGINS` lists exact origins.
  Credentials are never allowed.
- **Docs:** `/docs`, `/redoc` and `/openapi.json` are off by default in production
  (`DOCS_ENABLED`).
- **Authentication:**

  | Endpoint | Protection |
  |---|---|
  | `DELETE /api/v1/cache` | `ADMIN_API_TOKEN` bearer token (disabled when unset) |
  | `GET /metrics`, `/api/v1/analytics/summary`, `/api/v1/cache/stats`, `/health/dependencies` | `OBSERVABILITY_API_TOKEN` bearer token when set; required in production |
  | `POST /api/v1/chat` | `CHAT_API_KEYS` bearer key when `CHAT_AUTH_MODE=api_key` (step 8; required in production); rate limited |
  | `/api/v1/classify`, `GET /api/v1/models`, `/health` | public |

  Tokens are compared in constant time and never logged.

### Docker
- `docker-compose.yml` (development) binds Redis and PostgreSQL to **127.0.0.1**
  only. To apply this to already-running containers (data volumes are kept):
  `docker compose up -d --force-recreate redis postgres`.
- `docker-compose.prod.yml` is a **self-contained production stack** (Step 11.2; it
  replaced the step 7 override and is no longer combined with `docker-compose.yml`):
  edge proxy (Caddy: HTTPS, HSTS, CSP, rate limits), static frontend, two API
  containers, Redis and PostgreSQL. Only the edge publishes ports; Redis and
  PostgreSQL sit on an internal network with no host ports.
  - Redis `requirepass` fed on stdin (not in process arguments), `maxmemory 384mb`, `noeviction`;
  - PostgreSQL scram-sha-256; the API connects as a non-superuser that owns only its schema;
  - every service: read-only root filesystem, `tmpfs`, `no-new-privileges`, `cap_drop: ALL`
    (data services add back only what their entrypoints need), CPU/memory/PID limits, log rotation.

  Compose refuses to start if a required value in `deploy.env` is missing. See
  [DEPLOYMENT.md](DEPLOYMENT.md) for configuration, the deployment sequence, backups and rollback.

```bash
docker compose -f docker-compose.prod.yml --env-file /etc/llm-router/deploy.env up -d
```

### Dependency audit (step 7)
`pip-audit 2.10.1` (run once, in a separate virtualenv; not a project dependency)
found **0 known vulnerabilities** in all 81 pinned packages, the optional OTLP
exporter, and the 82 installed packages. Installed versions match
`requirements.txt` exactly. To repeat the audit:

```cmd
python -m venv %TEMP%\auditenv
%TEMP%\auditenv\Scripts\pip install pip-audit
%TEMP%\auditenv\Scripts\pip-audit -r requirements.txt --no-deps --disable-pip
```

### Remaining limitations (not solved by configuration)
- ~~No end-user authentication on `/chat`~~: solved in step 8 (`CHAT_AUTH_MODE=api_key`, below).
- ~~No TLS in this stack~~: solved in Step 11.2 (Caddy edge proxy with automatic HTTPS; see DEPLOYMENT.md).
- **Static, shared tokens** (admin and observability): no rotation without a
  restart, no per-user audit trail.
- ~~No container image for the API itself~~: solved in Step 11.2 (`backend/Dockerfile`, non-root, read-only).
- **Existing Postgres volumes keep their original `pg_hba.conf`.**
  `POSTGRES_INITDB_ARGS` only applies to a newly created data directory.
- **Audits are point-in-time.** Re-run `pip-audit` regularly or in CI.

## Real providers, chat authentication and spend limits (Phase 5 step 8)

Mock mode is still the default. Nothing in this section changes behaviour until
you configure it.

### Chat authentication
| Setting | Default | Meaning |
|---|---|---|
| `CHAT_AUTH_MODE` | `none` | `none` keeps `/api/v1/chat` open (development). `api_key` requires `Authorization: Bearer <key>`. **Required in production.** |
| `CHAT_API_KEYS` | `{}` | JSON `{"client-name": "key"}`. Keys must be at least 16 characters (32 in production), all distinct, and different from `ADMIN_API_TOKEN` and `OBSERVABILITY_API_TOKEN`. |

- Every key is compared in constant time (SHA-256 digests, no early exit).
- On failure the API returns 401 `unauthorized` with `WWW-Authenticate: Bearer realm="chat"`.
  Authentication runs **before** rate limiting, so failed attempts consume no quota.
- The client **name** (`chat-key:<name>`) becomes the identity for rate limits,
  budgets and persistence (stored as an HMAC). All of a client's IPs share one quota.
- Keys are registered for log redaction and masked in settings dumps.
  Settings validation errors no longer echo input values.
- Three separate credentials: chat keys (`/chat`), the observability token
  (metrics and analytics) and the admin token (`DELETE /cache`). None of them
  is accepted on the others' endpoints.

### Enabling real providers (double opt-in)
Real calls need **all** of the following. Otherwise the app refuses to start,
in every environment:
1. `LLM_MOCK_MODE=false` **and** `ALLOW_REAL_PROVIDER_CALLS=true`
2. `BUDGET_DAILY_USD` > 0
3. For every enabled non-mock model: a supported provider, a matching
   LiteLLM prefix, that provider's API key, and real pricing
   (`pricing_is_placeholder=false`, non-zero prices).

`backend/config/models.providers.example.json` holds one or more entries per
provider. Their IDs and prices were checked against the official pages listed in
each entry's `pricing_source` (verified 2026-11-08, re-verified 2026-11-09):

| Registry id | provider_model | $/1M in | $/1M out | Notes |
|---|---|---|---|---|
| openai-gpt-6-luna | `openai/gpt-6-luna` | 0.10 | 0.50 | 0.20 / 0.75 above 272K input |
| openai-gpt-6-1-sol | `openai/gpt-6.1-sol` | 2.00 | 10.00 | 4.00 / 15.00 above 272K input |
| claude-haiku-5-5 | `anthropic/claude-haiku-5-5` | 0.10 | 0.50 | 0.50 / 2.50 for prompts over 100,000 tokens |
| claude-sonnet-5-5 | `anthropic/claude-sonnet-5-5` | 2.00 | 10.00 | |
| claude-opus-5-5 | `anthropic/claude-opus-5-5` | 4.00 | 20.00 | |
| gemini-3-5-flash-lite | `gemini/gemini-3.5-flash-lite` | 0.30 | 2.50 | paid tier |
| groq-gpt-oss-120b | `groq/openai/gpt-oss-120b` | 0.15 | 0.60 | |
| mistral-small-2603 | `mistral/mistral-small-2603` | (0.15) | (0.60) | **UNVERIFIED**: the page does not say which price is input and which is output, so `pricing_is_placeholder=true` and the app refuses to enable it |

All entries are **disabled**. Only standard-tier list prices are modelled;
batch, flex, priority, fast mode, regional and prompt-cache pricing are not.
`max_output_tokens` is the router's per-call cap (8192), not the provider
maximum. Prices change, so re-check them before enabling a model.

### Spend safeguards
| Setting | Default | Meaning |
|---|---|---|
| `BUDGET_DAILY_USD` | 0 | Global USD per UTC day; **required > 0** for real calls |
| `BUDGET_DAILY_USD_PER_CLIENT` | 0 | Per client (API key name, or IP when auth is off) |
| `BUDGET_DAILY_TOKENS_PER_CLIENT` | 0 | Per-client tokens per day |
| `BUDGET_MAX_COST_PER_REQUEST_USD` | 0 | Limit for one whole chat request: its paid embedding plus every attempt, including retries and fallbacks (step 8A; it was per attempt in step 8) |
| `BUDGET_FAIL_CLOSED` | true | Redis unreachable while budgets are configured: 503 instead of unlimited spend |
| `REAL_MAX_OUTPUT_TOKENS` | 4096 | Cap on `max_tokens` sent to real providers |

How it works:
- **Before** every real provider attempt (including retries), the worst-case
  cost is reserved atomically with one Lua script:
  prompt upper bound (UTF-8 bytes + 32) × input price, plus `max_tokens` ×
  output price, at the prompt-length tier that size falls into.
- **After** the call, the reservation is settled:
  - Success: the reported cost.
  - Auth, 400, 404, 429 or context-window errors: 0, since the provider did no billable work.
  - Timeouts, 5xx, connection errors and unknown errors: the reservation is
    kept, because the provider may have billed the request.
- If a model would exceed a limit, it is skipped (`skipped_budget`) and a
  cheaper fallback may still serve the request. If every attempted model was
  refused, the API returns 429 `budget_exceeded`, or 503 `budget_unavailable`
  when the budget store could not be reached.
- Counters live in Redis under one hash tag per day
  (`{prefix}budget:{YYYYMMDD}:...`), expire after 2 days, and are shared by all
  instances. With `REDIS_ENABLED=false` they are per-process only.
- Mock calls never touch the budget.
- New metric: `llm_router_budget_rejections_total{reason}`. Provider attempts
  gain the `skipped_budget` outcome.

### LiteLLM import side effects (fixed)
With `LITELLM_MODE` unset or `DEV`, `import litellm` calls `load_dotenv()`. That
search walks upward from site-packages and would load the project-root `.env`
into the process environment. LiteLLM also downloads a model cost map at
import. LiteLLM is imported **only** through `app/providers/litellm_env.py`,
which has three layers of protection:
1. It **forces** `LITELLM_MODE=PRODUCTION` and `LITELLM_LOCAL_MODEL_COST_MAP=True`,
   so a stray `LITELLM_MODE=DEV` in the shell cannot re-enable `.env` loading.
2. It sets `PYTHON_DOTENV_DISABLED=1` for the duration of the import, which
   turns any `load_dotenv()` into a no-op, then restores the previous value.
   The app's own settings are unaffected: pydantic-settings reads `ENV_FILE`
   with `dotenv_values()`, which ignores the flag.
3. It refuses (raises `LiteLLMEnvironmentError`) when `litellm` was already
   imported directly, bypassing the guard. It also logs (as a count only) any
   environment variable that appears during the import.

The test suite and benchmarks force the same variables. `tests/test_env_isolation.py`
proves each layer in subprocesses. There, python-dotenv's `load_dotenv` and
`find_dotenv` are replaced by recorders, so even a broken guard cannot read a
real `.env` file.

### Known limitations (step 8)
- Not verified against live APIs. No paid calls were made, so provider-specific
  parameter quirks are only covered by mapping and fallback (for example, a
  model that rejects `temperature` returns 400 `bad_request`, which falls back).
- The budget is an estimate based on registry prices and provider-reported
  usage. It is not reconciled with provider invoices, and thinking and
  reasoning tokens are only counted if the provider reports them in
  `completion_tokens`.
- Static API keys: rotating one requires a restart. There are no scopes or
  expiry, and no per-key model allow-lists.
- The token-length upper bound assumes at least one UTF-8 byte per token. That
  holds for byte-level BPE and SentencePiece tokenizers.

## Embedding spend protection (Phase 5 step 8A)

With `EMBEDDING_PROVIDER=litellm` and approximate caching on, every exact-cache
miss makes a **paid** embedding call. Before step 8A that call ignored the
opt-in and every budget, even in mock mode. It is now gated and budgeted like
chat. The default local hashing embedder is free and unchanged; it is never
wrapped and never touches the budget.

### Startup gate (every environment, including `LLM_MOCK_MODE=true`)
`EMBEDDING_PROVIDER=litellm` refuses to start unless all of these hold:

| Setting | Requirement |
|---|---|
| `ALLOW_REAL_PROVIDER_CALLS` | `true` |
| `BUDGET_DAILY_USD` | > 0 |
| `EMBEDDING_MODEL` | `<provider>/<model>` with a supported paid provider (not Ollama) |
| Provider key | for example `OPENAI_API_KEY` for `openai/...` |
| `EMBEDDING_PRICING_IS_PLACEHOLDER` | `false` (default `true`) |
| `EMBEDDING_INPUT_COST_PER_MILLION` | finite and > 0. Settings reject negative, infinite and NaN values. |
| `EMBEDDING_PRICING_SOURCE` | an `https://` URL of the official pricing page |

Error messages name settings only, never values. Even past the gate, a paid
embedder is built only wrapped in `BudgetedEmbedder`; without a budget guard,
construction fails.

### Budget calculation
- **Worst case per call:** (UTF-8 bytes of the text + 32) × `EMBEDDING_INPUT_COST_PER_MILLION` / 1,000,000.
  There are no output tokens.
- The worst case is reserved atomically against the **same Redis counters** as
  chat: the global daily budget, and the per-client daily USD and token limits
  (the client is the API-key name, or the IP when auth is off).
- **Settlement:**
  - With reported usage (`usage.prompt_tokens`, or `total_tokens`): reported tokens × price.
  - No usage reported: the reservation is kept.
  - Auth, 400, 404, 429 or context-window errors: released to 0.
  - Timeouts, 5xx, connection errors, malformed responses, unknown errors and
    cancellations: the reservation is kept.
- **`BUDGET_MAX_COST_PER_REQUEST_USD` now covers one whole chat request:** its
  embedding plus every chat attempt (retries and fallbacks). Each reservation
  adds its worst case to a per-request running total, and each settlement
  corrects it to the actual amount. Previously the limit was checked per attempt.

### Safe degradation
If the embedding is refused (budget exceeded, or the budget store is
unreachable), the provider is **not** called and approximate search is skipped
for that request:
- The lookup counts as a cache miss.
- Exact-match lookup, the stampede lock and storing an exact-only entry still work.
- Chat continues under its own reservation.
- Embeddings ignore `BUDGET_FAIL_CLOSED=false`: they are an optimisation, so
  they are never made without a reservation.

### Observability
- `llm_router_embedding_budget_events_total{event,reason}`. Both labels are bounded:
  - `event`: reserved | refused | settled | released | kept_uncertain | kept_no_usage
  - `reason`: the budget reasons, `none` or `other`
- `llm_router_embedding_estimated_cost_usd_total`
- Log events `embedding.budget_refused` and `cache.semantic_skipped` carry
  reasons, sizes and amounts only. Prompts, vectors and keys are never logged.

### Known limitations (step 8A)
- Embeddings without reported usage keep their worst-case reservation. Under
  heavy concurrency they can use up the daily budget before chat reserves
  (a test shows exactly this). Prefer providers that report usage, or size
  `BUDGET_DAILY_USD` accordingly.
- Embedding prices are a single setting, not a registry entry; there is no
  per-model table.
- Local (Ollama) embeddings through LiteLLM are not supported by the gate.

## Error format

Every error returns the same JSON shape (`details` only where useful):

```json
{"error": {"code": "budget_exceeded", "message": "...", "details": {"reasons": ["client_daily_budget"]}}}
```

| Status | Codes |
|---|---|
| 400 | `unknown_model`, `model_disabled`, `invalid_time_range` |
| 401 | `unauthorized` (chat key, observability or admin token; `WWW-Authenticate: Bearer`) |
| 403 | `admin_disabled` (`ADMIN_API_TOKEN` not set) |
| 404 / 405 | `not_found`, `method_not_allowed` |
| 413 | `request_too_large` |
| 422 | `validation_error` (details without the submitted values), `request_rejected_by_provider` (content policy) |
| 429 | `rate_limit_exceeded` (with `Retry-After`), `budget_exceeded` |
| 503 | `all_models_failed`, `no_model_available`, `budget_unavailable`, `rate_limiter_unavailable`, `cache_unavailable`, `database_unavailable` |
| 500 | `internal_error` (no internals returned; the traceback is logged server-side, redacted) |

## End-to-end validation (Phase 5 step 9)

Step 9 audited the finished backend end to end. `tests/test_e2e_lifecycle.py` (15
tests) exercises the production wiring over HTTP with fake providers and
embeddings (see [Running tests](#running-tests)). Two defects were found and
fixed, each with regression tests that fail when the fix is removed:

1. **Chats refused by a spend limit were not counted** in
   `llm_router_chat_requests_total`. They now count with `outcome="error"`, like
   other failed chats. (Persistence and `llm_router_budget_rejections_total`
   already recorded them.)
2. **A failed paid embedding was called a second time in the same request.** After
   a lookup embedding failed (timeout, 5xx and so on), storing the answer called
   the embedder again: a second reservation and possibly a second charge. The
   entry is now stored exact-only and nothing is re-embedded.

## Production checklist

The production startup check (`ENVIRONMENT=production`) **refuses to start**
without these:
- `ADMIN_API_TOKEN`, `OBSERVABILITY_API_TOKEN`, `CLIENT_ID_HASH_KEY` and every
  `CHAT_API_KEYS` key: random, at least 32 characters.
- `CHAT_AUTH_MODE=api_key`.
- `POSTGRES_PASSWORD` (at least 16 characters, not the development default) and
  `REDIS_PASSWORD` (at least 16).
- `DEBUG=false`, no wildcard CORS, `TRUSTED_PROXIES` listed when proxy headers are
  trusted, and `CACHE_SEMANTIC_ALLOW_LEXICAL=false`.

It logs warnings for:
- interactive docs on, security headers off, or HSTS off;
- rate limiting off or fail-open;
- trusted incoming traces;
- raw prompt or response persistence;
- mock mode;
- `DEBUG` log level;
- with real providers, no per-client budget or `BUDGET_FAIL_CLOSED=false`.

Use `.env.production.example` (placeholders only) and keep the real file outside
the repository.

These operational requirements are **not** enforced by the app:
- **TLS:** the edge proxy in `docker-compose.prod.yml` terminates HTTPS (automatic
  certificates); `HSTS_ENABLED=true` and `TRUSTED_PROXIES` (the edge only) are set for you.
- **Network:** run Redis and PostgreSQL on a private network with
  `docker-compose.prod.yml` (no host ports). Never expose them publicly.
- **Process model:** run several uvicorn processes or containers behind a load
  balancer. They share all state through Redis and PostgreSQL.
- **Containers:** `backend/Dockerfile`, `frontend/Dockerfile` and the production stack in
  `docker-compose.prod.yml` (Step 11.2); see [DEPLOYMENT.md](DEPLOYMENT.md).
- **Monitoring:** scrape `/metrics` with the observability token, and alert on
  `llm_router_budget_rejections_total`, `llm_router_errors_total` and
  `llm_router_provider_attempts_total{outcome="error"}`.
- **Real spend:** budgets are estimates from registry prices and reported usage.
  Reconcile them with provider invoices, and re-verify prices before enabling a model.
- **Retention:** `request_log` grows without limit. Plan a cleanup job or
  partitioning.
- **Secrets:** rotating a token or key requires a restart.

## Remaining known limitations

- **No live-provider testing.** Nothing has been verified against live provider
  APIs; no paid calls were ever made. Provider and embedding behaviour is covered
  by fakes and error mapping only.
- **Lexical local embedder.** Approximate cache hits stay off unless a
  meaning-aware (paid) embedder is configured. Even then, entity swaps can score
  highly, so measure on your own traffic.
- **Lost stampede protection after a failed embedding.** When a paid embedding
  fails during lookup, that request skips the stampede lock. Identical
  concurrent misses may each call the provider, but each call is still budgeted.
- **Worst-case reservations held under load.** Embeddings without reported usage,
  and 5xx or timeout chat attempts, keep their worst-case reservations. Under heavy
  concurrency they can use up a budget earlier than the actual spend would.
- **Simple data structures.** Rate limits are fixed-window; similarity search is
  brute force per scope; persistence is best-effort (no outbox); migrations are
  hand-rolled.
- **Static credentials.** Chat keys and the admin and observability tokens are
  static: no scopes, expiry or per-key model allow-lists.
- **Single embedding price.** Embedding pricing is one setting, not per model.
  Ollama embeddings are not supported by the paid-embedding gate.
- **Health is process-local during a Redis outage** and is not merged back into
  Redis afterwards.

## Configuration

All settings live in `backend/app/core/config.py` and can be overridden by
environment variables or the project-root `.env`. `.env.example` lists **every**
setting with its development default. `.env.production.example` is the
production template.

## Infrastructure

| Service    | Host port | Container port | Note |
|------------|-----------|----------------|------|
| Redis      | 6379      | 6379           | bound to 127.0.0.1; no password in development |
| PostgreSQL | **5433**  | 5432           | bound to 127.0.0.1; 5433 avoids conflict with a locally installed PostgreSQL |

Connect from your machine with `127.0.0.1:5433` (not `localhost`: on Windows it
resolves to `::1` first, which times out). Change `POSTGRES_PORT` in `.env` to use
a different host port (both Docker and the app read it).

```cmd
docker compose up -d        :: start Redis + PostgreSQL
docker compose ps           :: should show both as "healthy"
docker compose logs -f      :: follow logs
docker compose down         :: stop (data kept in volumes)
```

> **Warning:** `docker compose down -v` also **deletes the data volumes**
> (`llm-router_redis_data`, `llm-router_postgres_data`), which removes all
> development history and cache. Don't use it unless you mean to wipe the data.

Isolated test data lives in Redis DB 15 (`test:` and `bench:` prefixes) and the
PostgreSQL database `llm_router_test`. Development data is in Redis DB 0 and
`llm_router`.

## Roadmap

- [x] Phase 1: skeleton, config, health checks, error handling, tests, infra
- [x] Phase 2: rule-based query classification engine + `POST /api/v1/classify`
- [x] Phase 3: model registry, provider abstraction (mock + LiteLLM), routing policy,
      retries/fallback, health tracking, usage & cost, `POST /api/v1/chat`
- [x] Phase 4: Redis pool, exact + embedding cache, distributed health and rate limiting,
      PostgreSQL history, analytics, multi-instance readiness
- [x] Phase 5 (backend): structured logging, Prometheus metrics, OpenTelemetry tracing,
      benchmarks and performance work, cache-correctness guard, production hardening,
      chat authentication, real providers with spend limits, embedding spend
      protection, end-to-end validation
- [ ] Frontend and deployment (later phases)
