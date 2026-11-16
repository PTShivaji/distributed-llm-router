"""Redis-backed response cache: exact-match fast path + embedding similarity.

Lookup order for a query within a *scope* (see `cache_scope`):
  1. Exact:    GET cache:{scope}:e:{sha256(normalised query)}     -> "hit_exact"
  2. Semantic: cosine(query vector, stored vectors of this scope)  -> "hit_semantic"
               (only if CACHE_SEMANTIC_ENABLED and similarity >= threshold)
  3. Miss:     take a short per-query lock so concurrent identical requests wait
               for the first one instead of all calling the provider (stampede guard).

Redis key layout (prefix omitted, {scope} is a Redis-cluster hash tag; qh = query
hash, which is also the entry id - schema v2, Phase 5 step 6B):
  cache:{scope}:e:{qh}       STR   JSON payload (answer + metadata), TTL  (exact key = entry)
  cache:{scope}:z            ZSET  qh -> expiry unix time (index + TTL + size cap)
  cache:{scope}:v            HASH  qh -> float32 vector bytes             (semantic only)
  cache:{scope}:g            HASH  qh -> critical-token signature digest  (semantic only)
  cache:{scope}:l:{qh}       STR   stampede-lock token, PX = lock timeout
  cache:{scope}:s            HASH  hits_exact, hits_semantic, misses, stores (per scope), TTL refreshed on use
  cache:{g}:idx              ZSET  "<scope>:<qh>" -> expiry   GLOBAL admission index, never above CACHE_MAX_ENTRIES
  cache:{g}:scopes           ZSET  scope -> latest expiry     live scopes (stats); bounded by the global index
  cache:scopes               SET   legacy scope registry (Phase 11.1: no longer written; removed by DELETE /cache)
  cache:stats                HASH  legacy global counters (still read for stats, no longer written)

Memory bound (Phase 11.1, H1). Every store is first ADMITTED by one atomic script on
cache:{g}:idx (expired members dropped, then: already present or below CACHE_MAX_ENTRIES
-> admit, else refuse). Entries above CACHE_MAX_ENTRY_BYTES are never stored. Cached
answers therefore occupy at most CACHE_MAX_ENTRIES x CACHE_MAX_ENTRY_BYTES (plus index
overhead), across all scopes and all instances. At the cap, new answers are simply not
cached until older entries expire (no cross-scope eviction: that would need multi-slot
deletes). Cleanup failures can only overstate the count until the members' expiry
scores pass, never let it exceed the cap. Every cache key starts with "cache:"; nothing
here touches rate-limit (rl:), budget (budget:) or health (health:) keys.

Round trips (measured, default exact-only config): a miss makes 1 counted GET,
1 lock script and 1 store transaction; an exact hit makes a single GET+count
script. Counters live in the scope's own hash so a hit and its counter are
updated in one atomic script without crossing cluster slots.

Privacy: the prompt text is never stored; only its SHA-256 and its vector.
The cached answer must be stored for the cache to work; it lives only in Redis
with a TTL and is never exposed by the stats API.

Redis vector search (RediSearch) is not available on plain redis:7, so the
similarity search loads the vectors of ONE scope (capped by
CACHE_MAX_ENTRIES_PER_SCOPE) and scores them in Python.
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.logging import log_event
from app.embeddings.base import (
    EmbeddingBudgetRefused, EmbeddingError, EmbeddingProvider, cosine_similarity, from_bytes, to_bytes,
)
from app.cache.equivalence import critical_signature, has_word_content
from app.infra.redis import REDIS_ERRORS, RedisManager, RedisPoolExhaustedError
from app.observability.tracing import NOOP_TRACER, set_attributes, start_span
from opentelemetry import trace
from opentelemetry.trace import Tracer

logger = logging.getLogger("app.cache")

CACHE_SCHEMA_VERSION = 2  # v2: entry stored under its exact key (step 6B)
CacheStatus = Literal["disabled", "bypass", "miss", "hit_exact", "hit_semantic", "error"]

# KEYS: scope index, vectors, signatures. ARGV: now, per-scope cap, entry-key prefix of this scope.
# Evicting an entry also DELETES its answer key (same {scope} slot): before Phase 11.1 only the
# index/vectors were trimmed and the answer stayed in Redis until its TTL. Returns
# {expired_count, evicted_count, evicted_id...} so the caller can free the global admission slots.
_PRUNE_LUA = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, id in ipairs(expired) do redis.call('HDEL', KEYS[2], id); redis.call('HDEL', KEYS[3], id) end
if #expired > 0 then redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1]) end
local over = redis.call('ZCARD', KEYS[1]) - tonumber(ARGV[2])
local out = {#expired, 0}
if over > 0 then
  local old = redis.call('ZPOPMIN', KEYS[1], over)
  for i = 1, #old, 2 do
    redis.call('HDEL', KEYS[2], old[i]); redis.call('HDEL', KEYS[3], old[i])
    redis.call('UNLINK', ARGV[3] .. old[i])
    out[2] = out[2] + 1
    table.insert(out, old[i])
  end
end
return out
"""

# Global admission (KEYS: global index, global scope registry; both {g}-tagged = one slot).
# ARGV: now, member "<scope>:<qh>", expiry, cap, scope, key TTL ms.
# Returns 1 = newly admitted, 2 = already admitted (refresh), 0 = refused (cap reached).
_ADMIT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local admitted = 0
if redis.call('ZSCORE', KEYS[1], ARGV[2]) then
  admitted = 2
elseif redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[4]) then
  admitted = 1
end
if admitted > 0 then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
  redis.call('ZADD', KEYS[2], 'GT', ARGV[3], ARGV[5])
  redis.call('PEXPIRE', KEYS[1], ARGV[6])
  redis.call('PEXPIRE', KEYS[2], ARGV[6])
end
return admitted
"""

# GET an entry and, if present, count the hit in the scope's stats hash (one round trip).
# ARGV: counter name, stats TTL ms (stats hashes expire when a scope goes idle: bounded metadata).
_GET_COUNT_LUA = """
local v = redis.call('GET', KEYS[1])
if v then
  redis.call('HINCRBY', KEYS[2], ARGV[1], 1)
  redis.call('PEXPIRE', KEYS[2], ARGV[2])
end
return v
"""

# Degradation reasons (bounded metric label values).
DEGRADED_REASONS = frozenset({"redis_unavailable", "pool_exhausted", "oom", "global_cap", "entry_too_large",
                              "embedding_failed"})
TEMPERATURE_GRID = 10  # cacheable temperatures: multiples of 0.1 (0.0 ... 2.0)


def degraded_reason(exc: BaseException) -> str:
    """Classify a cache-side Redis failure for logs/metrics (never changes the failure handling)."""
    if isinstance(exc, RedisPoolExhaustedError):
        return "pool_exhausted"
    if str(exc).startswith("OOM"):  # Redis: "OOM command not allowed when used memory > 'maxmemory'"
        return "oom"
    if isinstance(exc, EmbeddingError):
        return "embedding_failed"
    return "redis_unavailable"

# Double-checked stampede lock, atomically (one round trip):
#   1. if the entry now exists (a holder stored it after our first GET) -> count a hit, return it
#   2. else try to take the lock; only if won -> count the miss
# Without step 1, a request whose GET missed just before a holder's store+release
# transaction could win the freed lock and recompute (race found in step 6B).
# KEYS: lock, scope stats, entry (all share the {scope} hash tag).
_LOCK_LUA = """
local v = redis.call('GET', KEYS[3])
if v then
  redis.call('HINCRBY', KEYS[2], 'hits_exact', 1)
  redis.call('PEXPIRE', KEYS[2], ARGV[3])
  return {2, v}
end
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  redis.call('HINCRBY', KEYS[2], 'misses', 1)
  redis.call('PEXPIRE', KEYS[2], ARGV[3])
  return {1}
end
return {0}
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

_WS = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Exact-match normalisation: trim + collapse whitespace (case preserved on purpose)."""
    return _WS.sub(" ", query.strip())


def query_hash(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()


def cache_scope(
    *,
    model_id: str,
    provider: str,
    provider_model: str,
    temperature: float | None,
    max_output_tokens: int,
    mock_mode: bool,
    embedding_id: str,
) -> str:
    """Everything that makes a cached answer reusable. Different scope -> never shared."""
    material = {
        "v": CACHE_SCHEMA_VERSION,
        "model": model_id,
        "provider": provider,
        "provider_model": provider_model,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "mock_mode": mock_mode,
        "embedding": embedding_id,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:32]


@dataclass
class CacheLookup:
    status: CacheStatus
    scope: str
    query_hash: str
    payload: dict[str, Any] | None = None
    similarity: float | None = None
    embedding: list[float] | None = None
    lock_token: str | None = None
    error: str | None = None
    guard_filtered: int = 0  # candidates skipped because their critical signature differs
    latency_ms: float = 0.0
    counted: bool = False  # stats counter already incremented atomically with the lookup
    # Step 8A: approximate search skipped because the spend safeguards refused the paid
    # embedding (reason). Exact-match lookup, stampede lock and exact-only store still apply.
    # Step 9: also "embedding_failed" (the embedder raised), so store() never re-embeds.
    semantic_skipped: str | None = None

    @property
    def hit(self) -> bool:
        return self.status in ("hit_exact", "hit_semantic")


@dataclass
class CacheStats:
    enabled: bool
    semantic_enabled: bool
    available: bool
    embedding_provider: str
    embedding_is_semantic: bool
    similarity_threshold: float
    ttl_seconds: int
    total_entries: int | None = None
    scopes: int | None = None
    hits_exact: int = 0
    hits_semantic: int = 0
    misses: int = 0
    stores: int = 0
    approx_storage_bytes: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return self.hits_exact + self.hits_semantic

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else None


class SemanticCache:
    def __init__(
        self,
        redis: RedisManager,
        embedder: EmbeddingProvider,
        *,
        ttl_seconds: int = 3600,
        similarity_threshold: float = 0.92,
        semantic_enabled: bool = True,
        allow_lexical: bool = False,
        max_entries_per_scope: int = 1000,
        max_entries: int = 5000,
        max_entry_bytes: int = 32_768,
        cacheable_max_output_tokens: frozenset[int] | None = None,
        lock_timeout_seconds: float = 30.0,
        lock_wait_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
        tracer: Tracer | None = None,
    ) -> None:
        self.tracer: Tracer = tracer or NOOP_TRACER
        self.metrics: Any = None  # set by the container: Metrics.cache_degraded(operation, reason)
        self.redis = redis
        self.embedder = embedder
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        # Approximate hits require a meaning-aware embedder, unless the operator
        # explicitly accepts the lexical embedder's risks (allow_lexical).
        self.semantic_requested = semantic_enabled
        self.allow_lexical = allow_lexical
        self.semantic_enabled = semantic_enabled and (embedder.is_semantic or allow_lexical)
        self.semantic_gated = semantic_enabled and not self.semantic_enabled
        self.max_entries_per_scope = max_entries_per_scope
        self.max_entries = max_entries  # GLOBAL cap across every scope and instance
        self.max_entry_bytes = max_entry_bytes
        # None = no allow-list (every max_output_tokens value is cacheable; library/test default).
        self.cacheable_max_output_tokens = cacheable_max_output_tokens
        self.lock_timeout_seconds = lock_timeout_seconds
        self.lock_wait_seconds = lock_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.k = redis.keys

    # -- keys -------------------------------------------------------------
    def _zset(self, scope: str) -> str:
        return self.k("cache", "{" + scope + "}", "z")

    def _vectors(self, scope: str) -> str:
        return self.k("cache", "{" + scope + "}", "v")

    def _signatures(self, scope: str) -> str:
        return self.k("cache", "{" + scope + "}", "g")

    def _entry(self, scope: str, qh: str) -> str:
        return self.k("cache", "{" + scope + "}", "e", qh)

    def _lock(self, scope: str, qh: str) -> str:
        return self.k("cache", "{" + scope + "}", "l", qh)

    def _scope_stats(self, scope: str) -> str:
        return self.k("cache", "{" + scope + "}", "s")

    def _global_index(self) -> str:
        return self.k("cache", "{g}", "idx")

    def _global_scopes(self) -> str:
        return self.k("cache", "{g}", "scopes")

    @property
    def _meta_ttl_ms(self) -> int:
        """Indexes and stats hashes outlive the entries they describe by 60 s, then expire."""
        return (self.ttl_seconds + 60) * 1000

    def _degraded(self, operation: str, reason: str) -> None:
        if self.metrics is not None:
            self.metrics.cache_degraded(operation, reason)

    def cacheable_parameters(self, *, temperature: float | None, max_output_tokens: int,
                             always_cacheable_tokens: tuple[int, ...] = ()) -> bool:
        """Bounded scope cardinality (Phase 11.1): only grid temperatures and allow-listed token limits
        are cached. Anything else is still answered with the EXACT requested parameters - it just skips
        the cache - so generation semantics never change and the scope (exact values) stays correct."""
        if temperature is not None:
            scaled = temperature * TEMPERATURE_GRID
            if abs(scaled - round(scaled)) > 1e-9:
                return False
        if self.cacheable_max_output_tokens is None:
            return True
        return max_output_tokens in self.cacheable_max_output_tokens or max_output_tokens in always_cacheable_tokens

    async def _get_exact(self, scope: str, qh: str) -> dict[str, Any] | None:
        """Uncounted exact read (one GET). Kept for diagnostics and the redis micro-benchmark."""
        raw = await self.redis.client.get(self._entry(scope, qh))
        return json.loads(raw) if raw else None

    async def _get_counted(self, scope: str, qh: str, counter: str) -> dict[str, Any] | None:
        raw = await self.redis.client.eval(_GET_COUNT_LUA, 2, self._entry(scope, qh), self._scope_stats(scope), counter,
                                           self._meta_ttl_ms)
        return json.loads(raw) if raw else None

    # -- lookup -----------------------------------------------------------
    async def lookup(self, scope: str, query: str) -> CacheLookup:
        started = time.perf_counter()
        result = CacheLookup(status="miss", scope=scope, query_hash=query_hash(query))
        try:
            with start_span(self.tracer, "cache.exact_get", **{"db.system": "redis", "db.operation": "GET"}) as span:
                payload = await self._get_counted(scope, result.query_hash, "hits_exact")
                set_attributes(span, **{"cache.status": "hit_exact" if payload is not None else "miss"})
            if payload is not None:
                result.status, result.payload, result.similarity, result.counted = "hit_exact", payload, 1.0, True
            else:
                if self.semantic_enabled and has_word_content(query):
                    try:
                        with start_span(self.tracer, "cache.semantic_search", **{"db.system": "redis"}) as span:
                            await self._semantic_search(result, query)
                            set_attributes(span, **{"cache.status": result.status,
                                                    "cache.similarity": result.similarity,
                                                    "cache.guard_filtered": result.guard_filtered})
                    except EmbeddingBudgetRefused as exc:
                        # No paid call was made. Degrade to exact-match only for this request.
                        result.semantic_skipped = exc.reason
                        log_event(logger, "cache.semantic_skipped", logging.WARNING, reason=exc.reason,
                                  scope=scope[:12])
                if not result.hit:
                    with start_span(self.tracer, "cache.lock", **{"db.system": "redis", "db.operation": "SET NX"}) as span:
                        await self._acquire_or_wait(result)
                        set_attributes(span, **{"cache.lock_acquired": result.lock_token is not None,
                                                "cache.status": result.status})
            if not result.counted:
                await self._count(scope, result.status)  # only the rare "waiter gave up" path
        except REDIS_ERRORS as exc:
            result.status, result.error = "error", type(exc).__name__
            reason = degraded_reason(exc)
            self._degraded("lookup", reason)
            log_event(logger, "cache.redis_unavailable", logging.WARNING, error=result.error, reason=reason)
        except EmbeddingError as exc:
            # Exact-match still worked; semantic step failed. Treat as a miss (no lock wait).
            result.status, result.error = "miss", "embedding_failed"
            # Step 9: store() writes an exact-only entry instead of calling the (possibly
            # paid, possibly already billed) embedder a second time for the same request.
            result.semantic_skipped = "embedding_failed"
            log_event(logger, "embedding.failed", logging.WARNING, provider=self.embedder.id, detail=str(exc))
        result.latency_ms = round((time.perf_counter() - started) * 1000, 2)

        event = "cache.hit" if result.hit else ("cache.error" if result.status == "error" else "cache.miss")
        log_event(
            logger, event, status=result.status, similarity=result.similarity, scope=scope[:12],
            locked=result.lock_token is not None, latency_ms=result.latency_ms,
        )
        return result

    async def _embed(self, text: str) -> list[float]:
        with start_span(self.tracer, "embedding.generate", **{"embedding.provider": self.embedder.id}) as span:
            vector = await self.embedder.embed(text)
            set_attributes(span, **{"embedding.dimensions": len(vector)})
            return vector

    async def _semantic_search(self, result: CacheLookup, query: str) -> None:
        t0 = time.perf_counter()
        result.embedding = await self._embed(query)
        log_event(
            logger, "embedding.generated", provider=self.embedder.id, dimensions=len(result.embedding),
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
        )
        client = self.redis.client
        now = time.time()
        ids = await client.zrevrangebyscore(self._zset(result.scope), "+inf", now, start=0, num=self.max_entries_per_scope)
        set_attributes(trace.get_current_span(), **{"cache.candidates": len(ids)})
        if not ids:
            return
        if not any(result.embedding):  # zero vector: nothing comparable
            return
        signature = critical_signature(query)
        async with client.pipeline(transaction=False) as pipe:
            pipe.hmget(self._vectors(result.scope), ids)
            pipe.hmget(self._signatures(result.scope), ids)
            vectors, signatures = await pipe.execute()
        best_id, best = None, -1.0
        for entry_id, blob, sig in zip(ids, vectors, signatures):
            if blob is None:
                continue
            # Equivalence guard: never compare across differing numbers/negations/dates/...
            # (entries without a signature, e.g. written before the guard existed, never match).
            if sig is None or sig.decode() != signature:
                result.guard_filtered += 1
                continue
            stored = from_bytes(blob)
            if len(stored) != len(result.embedding):
                continue
            score = cosine_similarity(result.embedding, stored)
            if score > best:
                best_id, best = entry_id, score
        if best_id is None or best < self.similarity_threshold:
            result.similarity = round(best, 4) if best_id is not None else None
            return
        payload = await self._get_counted(result.scope, best_id.decode(), "hits_semantic")
        if payload is None:  # expired between index read and fetch: clean up lazily (one round trip)
            async with client.pipeline(transaction=False) as pipe:
                pipe.zrem(self._zset(result.scope), best_id)
                pipe.hdel(self._vectors(result.scope), best_id)
                pipe.hdel(self._signatures(result.scope), best_id)
                await pipe.execute()
            return
        result.status, result.payload, result.similarity, result.counted = "hit_semantic", payload, round(best, 4), True

    async def _acquire_or_wait(self, result: CacheLookup) -> None:
        """Stampede guard for identical queries: one caller computes, the rest wait briefly."""
        token = secrets.token_hex(8)
        lock_key = self._lock(result.scope, result.query_hash)
        reply = await self.redis.client.eval(
            _LOCK_LUA, 3, lock_key, self._scope_stats(result.scope), self._entry(result.scope, result.query_hash),
            token, int(self.lock_timeout_seconds * 1000), self._meta_ttl_ms,
        )
        if int(reply[0]) == 2:  # stored by another request since our first GET: a hit
            result.status, result.payload, result.similarity, result.counted = "hit_exact", json.loads(reply[1]), 1.0, True
            return
        if int(reply[0]) == 1:
            result.lock_token, result.counted = token, True  # miss counted atomically with the lock
            return
        log_event(logger, "cache.lock_wait", scope=result.scope[:12])
        deadline = time.monotonic() + self.lock_wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval_seconds)
            payload = await self._get_counted(result.scope, result.query_hash, "hits_exact")
            if payload is not None:
                result.status, result.payload, result.similarity, result.counted = "hit_exact", payload, 1.0, True
                return
            if not await self.redis.client.exists(lock_key):
                # The holder writes the entry and releases the lock in ONE transaction, so
                # "lock gone" can happen between our GET above and this EXISTS. Re-read once:
                # if the entry is there, it is a hit; only if it is still absent did the
                # holder fail (or its lock expire) and we compute ourselves.
                payload = await self._get_counted(result.scope, result.query_hash, "hits_exact")
                if payload is not None:
                    result.status, result.payload, result.similarity, result.counted = "hit_exact", payload, 1.0, True
                    return
                break
        # Give up waiting: proceed without the lock rather than block the user.

    # -- store / release -------------------------------------------------
    async def store(self, lookup: CacheLookup, query: str, payload: dict[str, Any]) -> bool:
        """Store an answer. Returns False (and records why) when it is not cached.

        Order: size check -> global admission (atomic, cap across all scopes/instances) ->
        per-scope MULTI/EXEC -> free the global slots of entries the per-scope cap evicted.
        A refused or failed store never raises: the request is still answered, uncached.
        The stampede lock (if held) is released by the caller's `release()` in that case.
        """
        admitted = 0
        member = f"{lookup.scope}:{lookup.query_hash}"
        try:
            entry_id = lookup.query_hash  # schema v2: the exact key IS the entry
            now = time.time()
            expiry = now + self.ttl_seconds
            body = json.dumps({**payload, "cached_at": now}, separators=(",", ":"))
            size = len(body.encode("utf-8"))
            if size > self.max_entry_bytes:
                self._degraded("store", "entry_too_large")
                log_event(logger, "cache.store_skipped", reason="entry_too_large", bytes=size,
                          limit=self.max_entry_bytes, scope=lookup.scope[:12])
                return False
            client = self.redis.client
            admitted = int(await client.eval(_ADMIT_LUA, 2, self._global_index(), self._global_scopes(), now, member,
                                             expiry, self.max_entries, lookup.scope, self._meta_ttl_ms))
            if not admitted:
                self._degraded("store", "global_cap")
                log_event(logger, "cache.store_skipped", logging.WARNING, reason="global_cap",
                          limit=self.max_entries, scope=lookup.scope[:12])
                return False
            vector = lookup.embedding
            if not (self.semantic_enabled and has_word_content(query)) or lookup.semantic_skipped:
                vector = None  # exact-only entry (never re-attempt a refused paid embedding)
            elif vector is None:
                vector = await self._embed(query)
            # ONE MULTI/EXEC round trip: entry + index (+ vector/signature) + counters + prune +
            # lock release. Same atomicity as before (it used to be 3 separate round trips).
            async with client.pipeline(transaction=True) as pipe:
                pipe.set(self._entry(lookup.scope, entry_id), body, ex=self.ttl_seconds)
                # Every entry is indexed (entry counts, TTL pruning and the per-scope cap
                # must work in exact-only mode too); vectors/signatures only when semantic.
                pipe.zadd(self._zset(lookup.scope), {entry_id: expiry})
                pipe.expire(self._zset(lookup.scope), self.ttl_seconds + 60)
                if vector is not None:
                    pipe.hset(self._vectors(lookup.scope), entry_id, to_bytes(vector))
                    pipe.hset(self._signatures(lookup.scope), entry_id, critical_signature(query))
                    pipe.expire(self._vectors(lookup.scope), self.ttl_seconds + 60)
                    pipe.expire(self._signatures(lookup.scope), self.ttl_seconds + 60)
                pipe.hincrby(self._scope_stats(lookup.scope), "stores", 1)
                pipe.pexpire(self._scope_stats(lookup.scope), self._meta_ttl_ms)
                pipe.eval(_PRUNE_LUA, 3, self._zset(lookup.scope), self._vectors(lookup.scope),
                          self._signatures(lookup.scope), now, self.max_entries_per_scope,
                          self._entry(lookup.scope, ""))
                if lookup.lock_token:  # release AFTER the entry is written, so waiters find it
                    pipe.eval(_RELEASE_LUA, 1, self._lock(lookup.scope, lookup.query_hash), lookup.lock_token)
                results = await pipe.execute()
            if lookup.lock_token:
                lookup.lock_token = None  # released inside the transaction
                results = results[:-1]
            expired, evicted, *evicted_ids = results[-1]
            if evicted_ids:
                await self._free_global(lookup.scope, evicted_ids)
            log_event(logger, "cache.stored", scope=lookup.scope[:12], semantic=vector is not None,
                      pruned=int(expired), evicted=int(evicted), bytes=size)
            return True
        except (*REDIS_ERRORS, EmbeddingError) as exc:
            reason = degraded_reason(exc)
            self._degraded("store", reason)
            log_event(logger, "cache.store_failed", logging.WARNING, error=type(exc).__name__, reason=reason)
            if admitted == 1:  # newly admitted but never written: give the slot back (best effort)
                await self._free_global(lookup.scope, [lookup.query_hash.encode()])
            return False

    async def _free_global(self, scope: str, entry_ids: list[bytes]) -> None:
        """Remove evicted/unwritten entries from the global index. Best effort: if this fails, the
        members expire by score, so the global count is only overstated - never above the cap."""
        members = [f"{scope}:{e.decode() if isinstance(e, bytes) else e}" for e in entry_ids]
        try:
            await self.redis.client.zrem(self._global_index(), *members)
        except REDIS_ERRORS as exc:
            log_event(logger, "cache.global_free_failed", logging.WARNING, error=type(exc).__name__,
                      members=len(members))

    async def release(self, lookup: CacheLookup) -> None:
        if not lookup.lock_token:
            return
        try:
            await self.redis.client.eval(_RELEASE_LUA, 1, self._lock(lookup.scope, lookup.query_hash), lookup.lock_token)
        except REDIS_ERRORS as exc:  # lock has a TTL, so it self-heals
            log_event(logger, "cache.lock_release_failed", logging.WARNING, error=type(exc).__name__)
        lookup.lock_token = None

    async def _count(self, scope: str, status: CacheStatus) -> None:
        field_name = {"hit_exact": "hits_exact", "hit_semantic": "hits_semantic", "miss": "misses"}.get(status)
        if field_name:
            async with self.redis.client.pipeline(transaction=False) as pipe:
                pipe.hincrby(self._scope_stats(scope), field_name, 1)
                pipe.pexpire(self._scope_stats(scope), self._meta_ttl_ms)
                await pipe.execute()

    def _stats_pattern(self) -> str:
        return self.k("cache", "{*}", "s")

    # -- management -------------------------------------------------------
    async def stats(self) -> CacheStats:
        s = CacheStats(
            enabled=True, semantic_enabled=self.semantic_enabled, available=False,
            embedding_provider=self.embedder.id, embedding_is_semantic=self.embedder.is_semantic,
            similarity_threshold=self.similarity_threshold, ttl_seconds=self.ttl_seconds,
        )
        if not self.embedder.is_semantic:
            s.notes.append("Local hashing embedder: similarity is lexical (shared words), not meaning-aware.")
        if not self.semantic_requested:
            s.notes.append("Approximate (semantic) hits are DISABLED (CACHE_SEMANTIC_ENABLED=false, the default). "
                           "Exact-match caching is active.")
        elif self.semantic_gated:
            s.notes.append("Approximate (semantic) hits are DISABLED: the lexical embedder cannot guarantee that two "
                           "queries are equivalent. Exact-match caching is active. "
                           "(CACHE_SEMANTIC_ALLOW_LEXICAL=true overrides this; unsafe.)")
        elif self.semantic_enabled and not self.embedder.is_semantic:
            s.notes.append("WARNING: approximate hits use the LEXICAL embedder (CACHE_SEMANTIC_ALLOW_LEXICAL=true); "
                           "changed entities or word order can still be served a wrong cached answer.")
        try:
            client = self.redis.client
            counters: dict[str, int] = {}
            stat_keys = [self.k("cache", "stats")]
            stat_keys += [k async for k in client.scan_iter(match=self._stats_pattern(), count=500)]
            for key in stat_keys:  # legacy global hash + one hash per scope
                for name, value in (await client.hgetall(key)).items():
                    counters[name.decode()] = counters.get(name.decode(), 0) + int(value)
            for name in ("hits_exact", "hits_semantic", "misses", "stores"):
                setattr(s, name, counters.get(name, 0))
            total, storage, now = 0, 0, time.time()
            scopes = [x.decode() for x in await client.zrangebyscore(self._global_scopes(), now, "+inf")]
            for scope in scopes:
                pruned = await client.eval(_PRUNE_LUA, 3, self._zset(scope), self._vectors(scope),
                                           self._signatures(scope), now, self.max_entries_per_scope,
                                           self._entry(scope, ""))
                if pruned[2:]:
                    await self._free_global(scope, pruned[2:])
                total += await client.zcard(self._zset(scope))
                for key in (self._zset(scope), self._vectors(scope), self._signatures(scope)):
                    storage += await client.memory_usage(key) or 0
            sample = [k async for k in client.scan_iter(match=self.k("cache", "{*}", "e", "*"), count=200)][:50]
            if sample:
                sizes = [await client.memory_usage(k) or 0 for k in sample]
                storage += int(sum(sizes) / len(sizes) * total)  # one entry key per indexed entry (approx.)
            s.total_entries, s.scopes, s.approx_storage_bytes, s.available = total, len(scopes), storage, True
            s.notes.append("approx_storage_bytes is an estimate from Redis MEMORY USAGE sampling.")
            s.notes.append(f"Global cap: at most {self.max_entries} entries of at most {self.max_entry_bytes} bytes "
                           "each (CACHE_MAX_ENTRIES / CACHE_MAX_ENTRY_BYTES); at the cap new answers are not cached "
                           "until older entries expire. Hit/miss counters cover scopes active within the cache TTL.")
        except REDIS_ERRORS as exc:
            s.notes.append(f"Redis unavailable ({type(exc).__name__}); counts unknown.")
        return s

    async def clear(self, *, reset_stats: bool = False) -> int:
        """Delete every cache key under this prefix (entries, indexes, locks)."""
        client = self.redis.client
        deleted = 0
        batch: list[bytes] = []
        stats_key = self.k("cache", "stats").encode()
        async for key in client.scan_iter(match=self.k.pattern("cache"), count=500):
            is_stats = key == stats_key or key.endswith(b"}:s")
            if is_stats and not reset_stats:
                continue
            batch.append(key)
            if len(batch) >= 500:
                deleted += await client.unlink(*batch)
                batch.clear()
        if batch:
            deleted += await client.unlink(*batch)
        log_event(logger, "cache.cleared", logging.WARNING, deleted_keys=deleted, reset_stats=reset_stats)
        return deleted
