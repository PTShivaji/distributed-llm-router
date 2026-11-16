"""Benchmark scenarios.

Groups (select with --scenarios):
  micro      classification, routing, embedding, cosine scan, mock provider
  inprocess  full /api/v1/chat through the ASGI app, concurrency sweep
  metrics    metrics enabled vs disabled (interleaved rounds)
  logging    root log level INFO (default) vs WARNING (interleaved rounds)
  tracing    tracing off vs on, exporters, sampling ratios (interleaved rounds)
  cache      cache miss / exact hit / semantic hit / miss with a full scope   [Redis]
  redis      raw Redis + rate limiter + health store + cache lookup ops       [Redis]
  postgres   batch persistence and analytics query                           [PostgreSQL]
  multi      one vs two in-process app instances sharing Redis                [Redis]
  http       REAL HTTP: spawned uvicorn processes, one vs two instances       (opt-in)

Isolation: Redis DB 15 + unique key prefix; PostgreSQL database llm_router_test.
The development database, development Redis keys and Docker volumes are never touched.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from benchmarks.environment import safe_settings
from benchmarks.runner import OpResult, RunOutcome, run_async, run_sync

BACKEND_DIR = Path(__file__).resolve().parents[1]
CHAT = "/api/v1/chat"

# Fixed, deterministic workload covering all three complexity tiers.
CORPUS: tuple[str, ...] = (
    "What is the capital of France?",
    "Who wrote Hamlet?",
    "Translate 'good morning' into Spanish.",
    "Explain how a hash map works.",
    "Write a Python function that parses a CSV file and returns the average of each column, handling missing values.",
    "Compare REST and GraphQL for a mobile app backend.",
    "What are the pros and cons of microservices versus a monolith?",
    "Explain the difference between processes and threads in Python and when to use each.",
    "Design a distributed rate limiter for a multi-region API gateway that must handle 1M requests per second. "
    "Compare token bucket and sliding window approaches and analyze the trade-offs.",
    "Architect a scalable, fault-tolerant event-driven microservices platform on Kubernetes with Kafka, "
    "explain consistency trade-offs, and outline a migration plan from our monolith.",
    "Summarize the plot of Romeo and Juliet.",
    "Prove that the square root of 2 is irrational, step by step.",
)


def corpus_query(i: int) -> str:
    return CORPUS[i % len(CORPUS)]


def unique_query(i: int) -> str:
    """Corpus query + a numbered tag. NOTE: these are near-duplicates for the lexical
    embedder (a tag is a tiny part of a long prompt), so many become semantic hits."""
    tag = f"warm {-i}" if i < 0 else f"case {i}"
    return f"{CORPUS[i % len(CORPUS)]} [{tag}]"


def distinct_query(i: int, salt: str = "q") -> str:
    """Deterministic, lexically unrelated queries (hex 'words' from SHA-256): a true miss workload."""
    import hashlib

    # Letters only (digits mapped to g..p): no numbers, so every query has the same
    # (empty) critical signature and the semantic guard lets the scan score them all.
    digest = hashlib.sha256(f"{salt}:{i}".encode()).hexdigest().translate(str.maketrans("0123456789", "ghijklmnop"))
    return "What is " + " ".join(digest[k:k + 8] for k in range(0, 48, 8)) + "?"


# --------------------------------------------------------------------------- #
# IPv4 literal: Docker publishes on 127.0.0.1 only; on Windows "localhost" tries ::1 first and times out.
BENCH_HOST = "127.0.0.1"
# The ONLY PostgreSQL database benchmarks may open; it must already exist (never created here).
BENCH_TEST_DB = "llm_router_test"
FORBIDDEN_DBS = frozenset({"llm_router", "postgres", "template0", "template1"})


class BenchmarkDatabaseError(RuntimeError):
    """The isolated benchmark database is unusable or a non-test database was requested."""


def require_bench_database(dbname: str) -> None:
    """Refuse (before any connection) every database except BENCH_TEST_DB."""
    if dbname != BENCH_TEST_DB:
        kind = "a protected database" if dbname in FORBIDDEN_DBS else "a non-test database"
        raise BenchmarkDatabaseError(
            f"refusing to benchmark against {kind} ({dbname!r}); only {BENCH_TEST_DB!r} is allowed")


@dataclass
class BenchConfig:
    requests: int = 500
    concurrency: int = 10
    warmup: int = 50
    rounds: int = 3
    concurrency_levels: tuple[int, ...] = (1, 10, 50)
    cache_scope_entries: int = 1000
    redis_db: int = 15
    postgres_db: str = "llm_router_test"
    prefix: str = field(default_factory=lambda: f"bench:{uuid.uuid4().hex[:10]}:")
    redis_available: bool = False
    postgres_available: bool = False

    def micro_n(self) -> int:
        return max(self.requests * 4, 1000)


@dataclass
class ScenarioResult:
    name: str
    group: str
    kind: str  # micro | in_process | redis | postgres | real_http
    description: str
    parameters: dict[str, Any]
    stats: dict[str, Any]
    settings: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def result(name: str, group: str, kind: str, description: str, outcome: RunOutcome, *,
           with_cache: bool = False, settings: Any = None, **parameters: Any) -> ScenarioResult:
    return ScenarioResult(
        name=name, group=group, kind=kind, description=description, parameters=parameters,
        stats=outcome.stats(with_cache=with_cache).to_dict(),
        settings=safe_settings(settings) if settings is not None else None,
    )


def bench_settings(cfg: BenchConfig, **overrides: Any):
    """Isolated, hermetic defaults: mock mode, no .env, test namespaces, features off unless asked."""
    from app.core.config import Settings

    base: dict[str, Any] = dict(
        _env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, mock_latency_ms=0,
        log_level="WARNING", redis_db=cfg.redis_db, redis_key_prefix=cfg.prefix, postgres_db=cfg.postgres_db,
        redis_host=BENCH_HOST, postgres_host=BENCH_HOST,
        redis_enabled=False, cache_enabled=False, rate_limit_enabled=False, database_enabled=False,
        health_backend="memory", metrics_enabled=True, access_log_enabled=True, tracing_enabled=False,
        provider_retry_backoff_seconds=0,
    )
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def running_app(settings) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """In-process app with its lifespan (Redis/DB connections) and an ASGI client."""
    from app.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:
            yield app, client


def chat_op(client: httpx.AsyncClient, query: Callable[[int], str]):
    async def op(i: int) -> OpResult:
        r = await client.post(CHAT, json={"query": query(i)})
        if r.status_code != 200:
            return OpResult(ok=False)
        return OpResult(ok=True, cache_status=r.json()["cache"]["status"])

    return op


def clear_trace_exporter(app: Any) -> None:
    exporter = app.state.container.tracing.memory_exporter
    if exporter is not None:
        exporter.clear()  # keep memory bounded between rounds


async def compare(cfg: BenchConfig, group: str, variants: dict[str, Any], *, query: Callable[[int], str] = corpus_query,
                  description: str, concurrency: int | None = None) -> list[ScenarioResult]:
    """Run each variant in interleaved rounds (A B C A B C ...) to cancel machine drift."""
    concurrency = concurrency or cfg.concurrency
    per_round = max(cfg.requests // cfg.rounds, 1)
    outcomes = {name: RunOutcome() for name in variants}
    async with AsyncExitStack() as stack:
        clients = {name: await stack.enter_async_context(running_app(s)) for name, s in variants.items()}
        for name, (_app, client) in clients.items():
            await run_async(chat_op(client, query), requests=0, warmup=cfg.warmup)
        for _ in range(cfg.rounds):
            for name, (app, client) in clients.items():
                run = await run_async(chat_op(client, query), requests=per_round, concurrency=concurrency)
                outcomes[name] = outcomes[name].merge(run)
                clear_trace_exporter(app)
    return [
        result(f"{group}.{name}", group, "in_process", description, outcomes[name], settings=variants[name],
               requests=per_round * cfg.rounds, concurrency=concurrency, rounds=cfg.rounds, warmup=cfg.warmup)
        for name in variants
    ]


# --------------------------------------------------------------------------- #
# micro
# --------------------------------------------------------------------------- #
async def scenario_micro(cfg: BenchConfig) -> list[ScenarioResult]:
    from app.classifier import QueryClassifier
    from app.embeddings.base import cosine_similarity, from_bytes, to_bytes
    from app.embeddings.local import LocalHashingEmbedder
    from app.providers.base import ProviderRequest
    from app.providers.mock import MockProvider
    from app.routing.health import HealthTracker
    from app.routing.policy import RoutingPolicy
    from app.routing.registry import load_registry

    n, out = cfg.micro_n(), []
    clf = QueryClassifier()
    out.append(result("micro.classification", "micro", "micro", "QueryClassifier.classify over the fixed corpus",
                      run_sync(lambda i: clf.classify(corpus_query(i)), requests=n, warmup=cfg.warmup), iterations=n))
    long_query = (CORPUS[8] + " ") * 50  # ~10k characters: worst case allowed by the API
    out.append(result("micro.classification_10k_chars", "micro", "micro", "classify a ~10,000-character query",
                      run_sync(lambda i: clf.classify(long_query[:10_000]), requests=max(n // 10, 100), warmup=10),
                      iterations=max(n // 10, 100), query_chars=len(long_query[:10_000])))

    policy = RoutingPolicy(load_registry(None), HealthTracker())
    classified = [clf.classify(q) for q in CORPUS]
    out.append(result("micro.routing_select", "micro", "micro", "RoutingPolicy.select (built-in registry)",
                      run_sync(lambda i: policy.select(classified[i % len(classified)]), requests=n, warmup=cfg.warmup),
                      iterations=n))

    embedder = LocalHashingEmbedder(256)
    out.append(result("micro.embedding_local", "micro", "micro", "LocalHashingEmbedder (256 dims)",
                      run_sync(lambda i: embedder.embed_sync(corpus_query(i)), requests=n, warmup=cfg.warmup),
                      iterations=n))

    # Exactly the semantic-cache scoring loop: decode float32 bytes + cosine, over a full scope.
    entries = cfg.cache_scope_entries
    blobs = [to_bytes(embedder.embed_sync(unique_query(i))) for i in range(entries)]
    q = embedder.embed_sync("Explain Python variables")

    def scan(_: int) -> float:
        return max(cosine_similarity(q, from_bytes(b)) for b in blobs)

    iters = max(n // 40, 30)
    out.append(result(f"micro.cosine_scan_{entries}", "micro", "micro",
                      f"semantic-cache scoring loop over {entries} x 256-dim vectors (current implementation)",
                      run_sync(scan, requests=iters, warmup=3), iterations=iters, vectors=entries, dimensions=256))

    mock = MockProvider()
    req = ProviderRequest("mock-fast", "mock", "mock/fast-v1", ({"role": "user", "content": CORPUS[0]},), 256, 5.0)

    async def provider_op(i: int) -> OpResult:
        await mock.complete(req)
        return OpResult()

    out.append(result("micro.mock_provider", "micro", "micro", "MockProvider.complete (deterministic, no I/O)",
                      await run_async(provider_op, requests=n, warmup=cfg.warmup), iterations=n))
    return out


# --------------------------------------------------------------------------- #
# in-process /chat
# --------------------------------------------------------------------------- #
async def scenario_inprocess(cfg: BenchConfig) -> list[ScenarioResult]:
    out = []
    settings = bench_settings(cfg)
    async with running_app(settings) as (_app, client):
        op = chat_op(client, corpus_query)
        for level in cfg.concurrency_levels:
            run = await run_async(op, requests=cfg.requests, concurrency=level, warmup=cfg.warmup)
            out.append(result(f"inprocess.chat_c{level}", "inprocess", "in_process",
                              f"POST /api/v1/chat, hermetic defaults (no Redis/DB), concurrency {level}",
                              run, settings=settings, requests=cfg.requests, concurrency=level, warmup=cfg.warmup))
    slow = bench_settings(cfg, mock_latency_ms=20)
    async with running_app(slow) as (_app, client):
        run = await run_async(chat_op(client, corpus_query), requests=max(cfg.requests // 2, 50),
                              concurrency=max(cfg.concurrency_levels), warmup=10)
        out.append(result("inprocess.chat_mock_latency_20ms", "inprocess", "in_process",
                          "chat with a simulated 20 ms provider: shows concurrency hides provider latency",
                          run, settings=slow, requests=max(cfg.requests // 2, 50),
                          concurrency=max(cfg.concurrency_levels), mock_latency_ms=20))
    return out


async def scenario_metrics(cfg: BenchConfig) -> list[ScenarioResult]:
    return await compare(cfg, "metrics", {
        "enabled": bench_settings(cfg, metrics_enabled=True, access_log_enabled=False),
        "disabled": bench_settings(cfg, metrics_enabled=False, access_log_enabled=False),
    }, description="POST /api/v1/chat: metrics on vs off (access log off in both to isolate metrics)",
       concurrency=1)


async def scenario_logging(cfg: BenchConfig) -> list[ScenarioResult]:
    """INFO (the default LOG_LEVEL) vs WARNING. The root level is process-wide, so it
    is switched before each interleaved round; output goes to os.devnull so the
    formatting/redaction/write cost is measured without flooding the console."""
    import logging

    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    sink = open(os.devnull, "w")  # noqa: SIM115
    handler = logging.StreamHandler(sink)
    from app.core.logging import _FORMAT
    from app.observability.redaction import RedactionFilter

    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(RedactionFilter())
    root.handlers = [handler]
    levels = {"level_info": logging.INFO, "level_warning": logging.WARNING}
    settings = bench_settings(cfg)
    per_round = max(cfg.requests // cfg.rounds, 1)
    outcomes = {name: RunOutcome() for name in levels}
    try:
        async with running_app(settings) as (_app, client):
            op = chat_op(client, corpus_query)
            await run_async(op, requests=0, warmup=cfg.warmup)
            for _ in range(cfg.rounds):
                for name, level in levels.items():
                    root.setLevel(level)
                    outcomes[name] = outcomes[name].merge(await run_async(op, requests=per_round, concurrency=1))
    finally:
        root.handlers, _ = saved_handlers, root.setLevel(saved_level)
        sink.close()
    return [result(f"logging.{name}", "logging", "in_process",
                   "POST /api/v1/chat with root log level INFO (default) vs WARNING; output to os.devnull",
                   outcomes[name], settings=settings, requests=per_round * cfg.rounds, concurrency=1, rounds=cfg.rounds)
            for name in levels]


async def scenario_tracing(cfg: BenchConfig) -> list[ScenarioResult]:
    variants = {
        "off": bench_settings(cfg, tracing_enabled=False),
        "on_exporter_none": bench_settings(cfg, tracing_enabled=True, tracing_exporter="none"),
        "on_exporter_memory": bench_settings(cfg, tracing_enabled=True, tracing_exporter="memory"),
    }
    for ratio in (0.0, 0.1, 0.5, 1.0):
        variants[f"sample_{ratio:g}"] = bench_settings(cfg, tracing_enabled=True, tracing_exporter="memory",
                                                       tracing_sample_ratio=ratio)
    return await compare(cfg, "tracing", variants, concurrency=1,
                         description="POST /api/v1/chat: tracing off / on (exporters) / sampling ratios")


# --------------------------------------------------------------------------- #
# cache (Redis)
# --------------------------------------------------------------------------- #
def _cache_settings(cfg: BenchConfig, *, semantic: bool = False, **kw: Any):
    """semantic=False: the DEFAULT configuration (exact-match cache only).
    semantic=True: explicit opt-in to approximate hits with the LEXICAL embedder
    (CACHE_SEMANTIC_ENABLED + CACHE_SEMANTIC_ALLOW_LEXICAL), guarded by the equivalence check."""
    if semantic:
        kw.update(cache_semantic_enabled=True, cache_semantic_allow_lexical=True)
    return bench_settings(cfg, redis_enabled=True, cache_enabled=True, health_backend="redis", **kw)


async def _clear_prefix(cfg: BenchConfig) -> int:
    from app.infra.redis import RedisManager

    m = RedisManager(f"redis://{BENCH_HOST}:6379/{cfg.redis_db}", key_prefix=cfg.prefix)
    await m.start()
    try:
        keys = [k async for k in m.client.scan_iter(match=cfg.prefix + "*", count=1000)]
        if keys:
            await m.client.unlink(*keys)
        return len(keys)
    finally:
        await m.close()


async def scenario_cache(cfg: BenchConfig) -> list[ScenarioResult]:
    out = []
    s = _cache_settings(cfg)                  # default: exact-only
    sem = _cache_settings(cfg, semantic=True)  # opt-in: guarded lexical semantic matching

    await _clear_prefix(cfg)
    async with running_app(s) as (_app, client):
        run = await run_async(chat_op(client, lambda i: distinct_query(i, "miss")), requests=cfg.requests,
                              concurrency=cfg.concurrency, warmup=cfg.warmup)
    out.append(result("cache.miss_distinct_queries", "cache", "in_process",
                      "DEFAULT config (exact-only): unrelated queries -> exact miss + lock + store",
                      run, with_cache=True, settings=s, requests=cfg.requests, concurrency=cfg.concurrency))

    await _clear_prefix(cfg)
    async with running_app(sem) as (_app, client):
        run = await run_async(chat_op(client, lambda i: distinct_query(i, "miss")), requests=cfg.requests,
                              concurrency=cfg.concurrency, warmup=cfg.warmup)
    out.append(result("cache.miss_distinct_queries_semantic", "cache", "in_process",
                      "OPT-IN lexical semantic: unrelated queries -> exact miss + guarded scan + store",
                      run, with_cache=True, settings=sem, requests=cfg.requests, concurrency=cfg.concurrency))

    # Correctness probe (not a latency scenario): numbered variants of the same prompts.
    # Run with semantic opted in to show the equivalence guard rejecting them.
    await _clear_prefix(cfg)
    async with running_app(sem) as (_app, client):
        run = await run_async(chat_op(client, unique_query), requests=cfg.requests, concurrency=cfg.concurrency,
                              warmup=cfg.warmup)
    r = result("cache.near_duplicate_numbered_queries", "cache", "in_process",
               "corpus prompts + '[case N]' tags: distinct requests that the lexical embedder treats as "
               "near-duplicates; the hit ratio shows how often a DIFFERENT request is served from cache",
               run, with_cache=True, settings=sem, requests=cfg.requests, concurrency=cfg.concurrency)
    r.extra["warning"] = "every request here is distinct; any hit is a cross-request semantic match"
    out.append(r)

    await _clear_prefix(cfg)
    async with running_app(s) as (_app, client):
        run = await run_async(chat_op(client, corpus_query), requests=cfg.requests, concurrency=cfg.concurrency,
                              warmup=len(CORPUS))
    out.append(result("cache.hit_exact", "cache", "in_process",
                      "fixed corpus, already cached during warm-up: exact-match hits, no provider call",
                      run, with_cache=True, settings=s, requests=cfg.requests, concurrency=cfg.concurrency))

    await _clear_prefix(cfg)
    stored = "Explain Python variables"
    variants = ("explain python variables", "Explain python variables!", "EXPLAIN PYTHON VARIABLES?",
                "explain Python Variables.")
    async with running_app(sem) as (_app, client):
        await client.post(CHAT, json={"query": stored})
        run = await run_async(chat_op(client, lambda i: variants[i % len(variants)]), requests=cfg.requests,
                              concurrency=cfg.concurrency, warmup=0)
    out.append(result("cache.hit_semantic", "cache", "in_process",
                      "OPT-IN lexical semantic: case/punctuation variants of a cached query -> guarded semantic hit",
                      run, with_cache=True, settings=sem, requests=cfg.requests, concurrency=cfg.concurrency))

    # Miss latency when the scope is full: the similarity scan visits every stored vector.
    await _clear_prefix(cfg)
    entries = cfg.cache_scope_entries
    sf = _cache_settings(cfg, semantic=True, cache_max_entries_per_scope=entries)
    async with running_app(sf) as (_app, client):
        # distinct_query() prompts are all SIMPLE -> same model -> same scope.
        fill = await run_async(chat_op(client, lambda i: distinct_query(i, "fill")), requests=entries,
                               concurrency=cfg.concurrency)
        n = max(cfg.requests // 2, 50)
        run = await run_async(chat_op(client, lambda i: distinct_query(i, "probe")), requests=n,
                              concurrency=1, warmup=0)
    r = result(f"cache.miss_full_scope_{entries}", "cache", "in_process",
               f"OPT-IN lexical semantic: miss with {entries} same-signature vectors in the scope "
               f"(each miss scores them all), concurrency 1",
               run, with_cache=True, settings=sf, requests=n, concurrency=1, scope_entries=entries)
    r.extra["fill_errors"] = fill.errors
    out.append(r)
    await _clear_prefix(cfg)
    return out


# --------------------------------------------------------------------------- #
# redis primitives
# --------------------------------------------------------------------------- #
async def scenario_redis(cfg: BenchConfig) -> list[ScenarioResult]:
    from app.cache.semantic import SemanticCache
    from app.embeddings.local import LocalHashingEmbedder
    from app.infra.redis import RedisManager
    from app.ratelimit.limiter import RateLimiter
    from app.routing.health_store import RedisHealthStore

    m = RedisManager(f"redis://{BENCH_HOST}:6379/{cfg.redis_db}", key_prefix=cfg.prefix)
    await m.start()
    out = []
    try:
        limiter = RateLimiter(m, per_minute=10**9, per_hour=10**9)
        health = RedisHealthStore(m)
        cache = SemanticCache(m, LocalHashingEmbedder(256))
        models = ["mock-fast", "mock-balanced", "mock-coder", "mock-advanced", "mock-legacy"]
        await m.client.set(m.keys("bench", "k"), "v")

        async def op_wrap(fn):
            async def op(i: int) -> OpResult:
                await fn(i)
                return OpResult()
            return op

        ops = {
            "redis.ping": ("PING round trip", lambda i: m.client.ping()),
            "redis.get": ("GET existing key", lambda i: m.client.get(m.keys("bench", "k"))),
            "redis.set": ("SET with TTL", lambda i: m.client.set(m.keys("bench", f"s{i % 100}"), "v", ex=60)),
            "redis.ratelimit_check": ("rate limiter Lua (minute+hour windows)",
                                      lambda i: limiter.check_client(f"client-{i % 50}")),
            "redis.health_view_5_models": ("health store pipeline: TIME + 5x HGETALL", lambda i: health.view(models)),
            "redis.health_record_success": ("health store success Lua", lambda i: health.record_success(models[i % 5])),
            "redis.cache_exact_get_miss": ("cache exact lookup (miss)", lambda i: cache._get_exact("scope", f"q{i}")),
        }
        for name, (desc, fn) in ops.items():
            run = await run_async(await op_wrap(fn), requests=cfg.requests, concurrency=cfg.concurrency,
                                  warmup=cfg.warmup)
            out.append(result(name, "redis", "redis", desc, run, requests=cfg.requests, concurrency=cfg.concurrency))
    finally:
        await m.close()
        await _clear_prefix(cfg)
    return out


# --------------------------------------------------------------------------- #
# postgres
# --------------------------------------------------------------------------- #
def _pg_url(cfg: BenchConfig) -> str:
    s = bench_settings(cfg)
    return s.database_url  # points at cfg.postgres_db (llm_router_test)


async def _truncate_test_tables(db) -> None:
    from sqlalchemy import text

    await db.run(lambda s: s.execute(text("TRUNCATE request_log, provider_attempt RESTART IDENTITY CASCADE")))


async def scenario_postgres(cfg: BenchConfig) -> list[ScenarioResult]:
    from app.db.analytics import summarize
    from app.db.recorder import RequestRecord, _write_batch
    from app.infra.database import Database

    require_bench_database(cfg.postgres_db)  # BenchmarkDatabaseError is a RuntimeError
    db = Database(_pg_url(cfg))
    if not await db.start():
        return []
    out = []
    try:
        await _truncate_test_tables(db)

        def record(i: int) -> RequestRecord:
            return RequestRecord(
                request_id=f"bench-{i}", status="success", query_chars=40, latency_ms=2.5, complexity="SIMPLE",
                selected_model="mock-fast", model_used="mock-fast", provider="mock", cache_status="miss",
                input_tokens=10, output_tokens=50, total_tokens=60, usage_source="estimated", total_cost=1e-5,
                attempts=[{"attempt_number": 1, "model_id": "mock-fast", "provider": "mock", "outcome": "success",
                           "error_kind": None, "retryable": None, "latency_ms": 1.0}],
            )

        for batch_size in (1, 50):
            batches = max(cfg.requests // (5 if batch_size > 1 else 1), 20)

            async def op(i: int, size: int = batch_size) -> OpResult:
                batch = [record(i * size + j) for j in range(size)]
                await db.run(lambda s: _write_batch(s, batch))
                return OpResult()

            run = await run_async(op, requests=batches, concurrency=min(cfg.concurrency, 5), warmup=5)
            r = result(f"postgres.persist_batch_{batch_size}", "postgres", "postgres",
                       f"insert {batch_size} request_log row(s) + attempts per transaction (worker thread)",
                       run, batches=batches, batch_size=batch_size, concurrency=min(cfg.concurrency, 5))
            stats = r.stats
            if stats["requests_per_second"]:
                r.extra["rows_per_second"] = round(stats["requests_per_second"] * batch_size, 1)
            out.append(r)

        async def analytics_op(i: int) -> OpResult:
            await db.run(lambda s: summarize(s, None, None))
            return OpResult()

        n = max(cfg.requests // 10, 30)
        run = await run_async(analytics_op, requests=n, concurrency=1, warmup=3)
        rows = await db.run(lambda s: summarize(s, None, None).total_requests)
        r = result("postgres.analytics_summary", "postgres", "postgres",
                   "GET /analytics/summary queries (5 aggregate SELECTs) over the benchmark rows",
                   run, requests=n, concurrency=1)
        r.extra["rows_in_table"] = rows
        out.append(r)
    finally:
        await _truncate_test_tables(db)
        await db.close()
    return out


# --------------------------------------------------------------------------- #
# multi-instance (in-process)
# --------------------------------------------------------------------------- #
async def scenario_multi(cfg: BenchConfig) -> list[ScenarioResult]:
    """Instances share Redis (health) but run in ONE process/event loop: this measures
    shared-state correctness/overhead, not CPU scaling (see the `http` group for that)."""
    out = []
    s = bench_settings(cfg, redis_enabled=True, health_backend="redis")
    for instances in (1, 2):
        async with AsyncExitStack() as stack:
            clients = [(await stack.enter_async_context(running_app(s)))[1] for _ in range(instances)]

            async def op(i: int) -> OpResult:
                r = await clients[i % instances].post(CHAT, json={"query": corpus_query(i)})
                return OpResult(ok=r.status_code == 200)

            run = await run_async(op, requests=cfg.requests, concurrency=cfg.concurrency, warmup=cfg.warmup)
        out.append(result(f"multi.inprocess_{instances}_instance{'s' if instances > 1 else ''}", "multi",
                          "in_process", f"{instances} app instance(s) in one process sharing Redis health, round-robin",
                          run, settings=s, requests=cfg.requests, concurrency=cfg.concurrency, instances=instances))
    await _clear_prefix(cfg)
    return out


# --------------------------------------------------------------------------- #
# real HTTP (opt-in): spawned uvicorn processes
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_env(cfg: BenchConfig) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "LLMR_DISABLE_ENV_FILE": "1", "LLM_MOCK_MODE": "true", "MOCK_FAILURES": "{}", "LOG_LEVEL": "WARNING",
        "REDIS_DB": str(cfg.redis_db), "REDIS_KEY_PREFIX": cfg.prefix, "POSTGRES_DB": cfg.postgres_db,
        "REDIS_ENABLED": "true" if cfg.redis_available else "false",
        "HEALTH_BACKEND": "redis" if cfg.redis_available else "memory",
        "CACHE_ENABLED": "false", "RATE_LIMIT_ENABLED": "false", "DATABASE_ENABLED": "false",
        "TRACING_ENABLED": "false", "METRICS_ENABLED": "true",
    })
    return env


@asynccontextmanager
async def spawned_servers(cfg: BenchConfig, count: int) -> AsyncIterator[list[str]]:
    """Start `count` uvicorn processes on free ports; terminate exactly those processes afterwards."""
    procs, urls = [], []
    try:
        for _ in range(count):
            port = _free_port()
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                 "--no-access-log", "--log-level", "warning"],
                cwd=BACKEND_DIR, env=_server_env(cfg), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            urls.append(f"http://127.0.0.1:{port}")
        async with httpx.AsyncClient(timeout=2) as probe:
            deadline = time.monotonic() + 90
            for url in urls:
                while True:
                    try:
                        if (await probe.get(url + "/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"server {url} did not start")
                    await asyncio.sleep(0.25)
        yield urls
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()


async def scenario_http(cfg: BenchConfig) -> list[ScenarioResult]:
    out = []
    for instances in (1, 2):
        async with spawned_servers(cfg, instances) as urls:
            limits = httpx.Limits(max_connections=cfg.concurrency * 2, max_keepalive_connections=cfg.concurrency * 2)
            async with httpx.AsyncClient(limits=limits, timeout=30) as client:

                async def op(i: int) -> OpResult:
                    r = await client.post(urls[i % len(urls)] + CHAT, json={"query": corpus_query(i)})
                    return OpResult(ok=r.status_code == 200)

                run = await run_async(op, requests=cfg.requests, concurrency=cfg.concurrency, warmup=cfg.warmup)
        out.append(result(f"http.real_{instances}_instance{'s' if instances > 1 else ''}", "http", "real_http",
                          f"loopback HTTP to {instances} spawned uvicorn process(es), round-robin client",
                          run, requests=cfg.requests, concurrency=cfg.concurrency, instances=instances,
                          server_config={k: v for k, v in _server_env(cfg).items()
                                         if k in ("REDIS_ENABLED", "HEALTH_BACKEND", "CACHE_ENABLED",
                                                  "RATE_LIMIT_ENABLED", "DATABASE_ENABLED", "TRACING_ENABLED",
                                                  "METRICS_ENABLED", "LLM_MOCK_MODE")}))
    if cfg.redis_available:
        await _clear_prefix(cfg)
    return out


GROUPS: dict[str, tuple[Callable[[BenchConfig], Any], str | None]] = {
    "micro": (scenario_micro, None),
    "inprocess": (scenario_inprocess, None),
    "metrics": (scenario_metrics, None),
    "logging": (scenario_logging, None),
    "tracing": (scenario_tracing, None),
    "cache": (scenario_cache, "redis"),
    "redis": (scenario_redis, "redis"),
    "postgres": (scenario_postgres, "postgres"),
    "multi": (scenario_multi, "redis"),
    "http": (scenario_http, None),
}
DEFAULT_GROUPS = ("micro", "inprocess", "metrics", "logging", "tracing", "cache", "redis", "postgres", "multi")
