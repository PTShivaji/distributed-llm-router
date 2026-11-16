"""Distributed fixed-window rate limiter (one atomic Lua script per check).

All windows of a check (e.g. per-minute AND per-hour) are evaluated and
incremented in a single script, so concurrent requests on any number of
instances can never exceed a limit, and a rejected request consumes nothing.

Keys: {prefix}rl:{kind}:{subject}:{window}   e.g. llmr:rl:client:3f2a..:minute
Each window starts on its first request and expires after its length.
Trade-off (documented): fixed windows allow up to 2x the limit across a window
boundary; sliding windows would need a sorted set per subject.
"""

import hashlib
import hmac
import ipaddress
import logging
from dataclasses import dataclass

from starlette.requests import Request

from app.core.logging import log_event
from app.infra.redis import REDIS_ERRORS, RedisManager, RedisPoolExhaustedError

logger = logging.getLogger("app.ratelimit")

# ARGV: limit_1, window_ms_1, limit_2, window_ms_2, ...
# Returns {allowed(0/1), failing_index, count_1, ttl_1, count_2, ttl_2, ...}
_LIMIT_LUA = """
local n = #KEYS
for i = 1, n do
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current + 1 > tonumber(ARGV[2 * i - 1]) then
    local out = {0, i}
    for j = 1, n do
      local c = tonumber(redis.call('GET', KEYS[j]) or '0')
      local ttl = redis.call('PTTL', KEYS[j])
      if ttl < 0 then ttl = tonumber(ARGV[2 * j]) end
      table.insert(out, c); table.insert(out, ttl)
    end
    return out
  end
end
local out = {1, 0}
for i = 1, n do
  local c = redis.call('INCR', KEYS[i])
  if c == 1 then redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[2 * i])) end
  local ttl = redis.call('PTTL', KEYS[i])
  table.insert(out, c); table.insert(out, ttl)
end
return out
"""


@dataclass(frozen=True, slots=True)
class Window:
    name: str  # "minute" | "hour"
    limit: int
    seconds: int


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int | None  # the most restrictive window (for headers)
    remaining: int | None
    reset_seconds: int | None
    window: str | None
    degraded: bool = False  # Redis unavailable -> decision made without it
    busy: bool = False  # connection-pool contention (Redis healthy) -> rejected, never failed open

    def headers(self) -> dict[str, str]:
        if self.limit is None:
            return {}
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining or 0, 0)),
            "X-RateLimit-Reset": str(self.reset_seconds or 0),
            "X-RateLimit-Window": self.window or "",
        }
        if not self.allowed:
            h["Retry-After"] = str(max(self.reset_seconds or 1, 1))
        return h


ALLOW_UNLIMITED = RateLimitResult(True, None, None, None, None)


_hash_key: bytes | None = None


def configure_client_hashing(key: str | None) -> None:
    """Use a keyed HMAC for client identifiers (CLIENT_ID_HASH_KEY).

    Unkeyed SHA-256 of an IPv4 address can be reversed by brute force (2^32
    values), so production should set a key. All instances must share the key
    so rate-limit counters and logs stay consistent across instances.
    """
    global _hash_key
    _hash_key = key.encode("utf-8") if key else None


def hash_subject(value: str) -> str:
    """Rate-limit keys, logs and DB rows never contain raw IPs or client identifiers."""
    data = value.encode("utf-8")
    if _hash_key is not None:
        return hmac.new(_hash_key, data, hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(data).hexdigest()[:24]


class RateLimiter:
    def __init__(
        self,
        redis: RedisManager,
        *,
        per_minute: int = 60,
        per_hour: int = 1000,
        model_limits: dict[str, int] | None = None,
        fail_open: bool = True,
    ) -> None:
        self.redis = redis
        self.client_windows = [
            w for w in (Window("minute", per_minute, 60), Window("hour", per_hour, 3600)) if w.limit > 0
        ]
        self.model_limits = dict(model_limits or {})
        self.fail_open = fail_open

    async def check(self, kind: str, subject: str, windows: list[Window]) -> RateLimitResult:
        if not windows:
            return ALLOW_UNLIMITED
        keys = [self.redis.keys("rl", kind, subject, w.name) for w in windows]
        args: list[int] = []
        for w in windows:
            args += [w.limit, w.seconds * 1000]
        try:
            raw = await self.redis.client.eval(_LIMIT_LUA, len(keys), *keys, *args)
        except RedisPoolExhaustedError as exc:
            # Redis is up but no pooled connection freed in time. Failing open here would let a burst
            # bypass the limit, so reject (503 + Retry-After) regardless of RATE_LIMIT_FAIL_OPEN.
            log_event(logger, "ratelimit.pool_exhausted", logging.WARNING, kind=kind, error=type(exc).__name__)
            return RateLimitResult(False, None, None, None, None, degraded=True, busy=True)
        except REDIS_ERRORS as exc:
            log_event(logger, "ratelimit.redis_unavailable", logging.WARNING, kind=kind,
                      error=type(exc).__name__, fail_open=self.fail_open)
            return RateLimitResult(self.fail_open, None, None, None, None, degraded=True)

        allowed, failing = bool(raw[0]), int(raw[1])
        stats = [(int(raw[2 + 2 * i]), int(raw[3 + 2 * i])) for i in range(len(windows))]
        if not allowed:
            w = windows[failing - 1]
            count, ttl_ms = stats[failing - 1]
        else:  # report the window with the fewest remaining requests
            i = min(range(len(windows)), key=lambda j: windows[j].limit - stats[j][0])
            w, (count, ttl_ms) = windows[i], stats[i]
        return RateLimitResult(allowed, w.limit, w.limit - count, max(1, -(-ttl_ms // 1000)), w.name)

    async def check_client(self, client_id: str) -> RateLimitResult:
        return await self.check("client", hash_subject(client_id), self.client_windows)

    async def check_model(self, model_id: str) -> RateLimitResult:
        limit = self.model_limits.get(model_id)
        if not limit:
            return ALLOW_UNLIMITED
        return await self.check("model", model_id, [Window("minute", limit, 60)])


# --------------------------------------------------------------------------- #
class ClientIdentityResolver:
    """Determines the client address without trusting spoofable headers by default.

    X-Forwarded-For is only honoured when TRUST_PROXY_HEADERS=true AND the
    direct peer is in TRUSTED_PROXIES; then the right-most untrusted hop wins.
    """

    def __init__(self, *, trust_proxy_headers: bool = False, trusted_proxies: list[str] | None = None) -> None:
        self.trust = trust_proxy_headers
        self.networks = [ipaddress.ip_network(p, strict=False) for p in (trusted_proxies or [])]

    def _trusted(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in net for net in self.networks)

    def resolve(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if not (self.trust and self._trusted(peer)):
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        for hop in reversed(hops):
            if not self._trusted(hop):
                return hop
        return peer
