"""HTTP-level Phase 4 tests: 429, cache API, admin auth, analytics, multi-instance."""

import time

from fastapi.testclient import TestClient

from app.main import create_app

CHAT = "/api/v1/chat"
Q = {"query": "What is the capital of France?"}


def client(settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


def flush(c: TestClient) -> None:
    recorder = c.app.state.container.recorder
    c.portal.call(recorder.flush)


# --- Rate limiting over HTTP ---------------------------------------------------
def test_429_with_headers(infra_settings):
    with client(infra_settings(rate_limit_per_minute=2, rate_limit_per_hour=0, cache_enabled=False)) as c:
        ok = [c.post(CHAT, json=Q) for _ in range(2)]
        blocked = c.post(CHAT, json=Q)
    assert [r.status_code for r in ok] == [200, 200]
    assert ok[0].headers["X-RateLimit-Limit"] == "2" and ok[0].headers["X-RateLimit-Remaining"] == "1"
    assert blocked.status_code == 429
    err = blocked.json()["error"]
    assert err["code"] == "rate_limit_exceeded" and err["details"]["window"] == "minute"
    assert int(blocked.headers["Retry-After"]) >= 1 and blocked.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.headers["X-Request-ID"]


def test_spoofed_forwarded_for_does_not_bypass_limit(infra_settings):
    with client(infra_settings(rate_limit_per_minute=1, rate_limit_per_hour=0, cache_enabled=False)) as c:
        first = c.post(CHAT, json=Q, headers={"X-Forwarded-For": "1.1.1.1"})
        second = c.post(CHAT, json=Q, headers={"X-Forwarded-For": "2.2.2.2"})
    assert first.status_code == 200 and second.status_code == 429


def test_classify_and_health_are_not_rate_limited(infra_settings):
    with client(infra_settings(rate_limit_per_minute=1, rate_limit_per_hour=0)) as c:
        codes = [c.post("/api/v1/classify", json=Q).status_code for _ in range(3)]
        codes += [c.get("/health").status_code for _ in range(3)]
    assert set(codes) == {200}


# --- Cache API -----------------------------------------------------------------
def test_cache_stats_and_hit_over_http(infra_settings):
    with client(infra_settings(rate_limit_enabled=False)) as c:
        first = c.post(CHAT, json=Q).json()
        second = c.post(CHAT, json=Q).json()
        stats = c.get("/api/v1/cache/stats").json()
    assert first["cache"]["status"] == "miss" and second["cache"]["status"] == "hit_exact"
    assert second["usage"]["source"] == "cache_hit" and second["answer"] == first["answer"]
    assert stats["enabled"] and stats["available"] and stats["total_entries"] == 1
    assert (stats["hits"], stats["misses"], stats["hit_rate"]) == (1, 1, 0.5)
    assert "capital" not in str(stats) and first["answer"] not in str(stats)  # no cached content exposed


def test_cache_stats_when_disabled(test_settings):
    with TestClient(create_app(test_settings)) as c:
        stats = c.get("/api/v1/cache/stats").json()
    assert stats["enabled"] is False and stats["total_entries"] is None


def test_cache_delete_requires_configured_admin_token(infra_settings):
    with client(infra_settings()) as c:
        r = c.delete("/api/v1/cache")
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_disabled"


def test_cache_delete_auth(infra_settings):
    with client(infra_settings(admin_api_token="s3cret-admin-token", rate_limit_enabled=False)) as c:
        c.post(CHAT, json=Q)
        missing = c.delete("/api/v1/cache")
        wrong = c.delete("/api/v1/cache", headers={"Authorization": "Bearer nope"})
        ok = c.delete("/api/v1/cache", headers={"Authorization": "Bearer s3cret-admin-token"})
        after = c.post(CHAT, json=Q).json()
    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert ok.status_code == 200 and ok.json()["cleared"] and ok.json()["deleted_keys"] > 0
    assert after["cache"]["status"] == "miss"


# --- Analytics -------------------------------------------------------------------
def test_analytics_unavailable_without_database(infra_settings):
    with client(infra_settings(database_enabled=False)) as c:
        r = c.get("/api/v1/analytics/summary")
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"


def test_analytics_invalid_range(infra_settings, pg_clean):
    with client(infra_settings(database_enabled=True)) as c:
        r = c.get("/api/v1/analytics/summary", params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"})
        bad = c.get("/api/v1/analytics/summary", params={"start": "yesterday"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_time_range"
    assert bad.status_code == 422


def test_end_to_end_persistence_and_analytics(infra_settings, pg_clean):
    s = infra_settings(database_enabled=True, rate_limit_enabled=False, mock_failures={"mock-advanced": "unavailable"},
                       provider_max_retries=0)
    complex_q = {"query": "Design a distributed cache with sharding and replication. Compare consistency models and analyze the trade-offs."}
    with client(s) as c:
        c.post(CHAT, json=Q)
        c.post(CHAT, json=Q)  # cache hit
        fb = c.post(CHAT, json=complex_q).json()
        flush(c)
        summary = c.get("/api/v1/analytics/summary").json()
        future = c.get("/api/v1/analytics/summary", params={"start": "2999-01-01T00:00:00"}).json()
    assert fb["fallback"]["used"] is True
    assert summary["total_requests"] == 3 and summary["successful_requests"] == 3
    assert summary["cache_hits"] == 1 and summary["cache_misses"] == 2
    assert summary["fallback_requests"] == 1
    assert summary["requests_by_complexity"] == {"SIMPLE": 2, "COMPLEX": 1}
    providers = {p["provider"]: p for p in summary["provider_attempts"]}
    assert providers["mock"]["failures"] == 1 and providers["mock"]["successes"] == 2
    assert future["total_requests"] == 0


def test_dependencies_endpoint(infra_settings, pg_clean):
    with client(infra_settings(database_enabled=True)) as c:
        body = c.get("/api/v1/health/dependencies").json()
    assert body["redis"] == {"enabled": True, "reachable": True}
    assert body["database"] == {"enabled": True, "reachable": True}
    assert body["health_backend"] == "redis" and body["cache_enabled"] and body["rate_limit_enabled"]


# --- Multi-instance: two independent app instances sharing Redis + PostgreSQL -----
def test_two_instances_share_cache_health_limits_and_history(infra_settings, pg_clean):
    shared = dict(database_enabled=True, rate_limit_per_minute=5, rate_limit_per_hour=0,
                  health_failure_threshold=1, health_cooldown_seconds=60, provider_max_retries=0)
    a_settings = infra_settings(**shared, mock_failures={"mock-fast": "unavailable"})
    b_settings = infra_settings(**shared)
    with client(a_settings) as a, client(b_settings) as b:
        # Health: instance A sees mock-fast fail; instance B then routes around it without trying it.
        a_resp = a.post(CHAT, json=Q).json()
        b_resp = b.post(CHAT, json=Q).json()
        b_models = {m["id"]: m["health"]["state"] for m in b.get("/api/v1/models").json()["models"]}

        # Cache: an answer produced on B is a hit on A.
        cq = {"query": "Explain Python variables"}
        b_first = b.post(CHAT, json=cq).json()
        a_hit = a.post(CHAT, json=cq).json()

        # Rate limit: both instances draw from one budget (5/min, 4 used so far).
        last_ok = a.post(CHAT, json={"query": "hi"})
        over = b.post(CHAT, json={"query": "hello"})

        flush(a)
        flush(b)
        summary = a.get("/api/v1/analytics/summary").json()

    assert a_resp["fallback"]["used"] is True
    assert b_models["mock-fast"] == "unhealthy"
    assert b_resp["selected_model"] != "mock-fast" and b_resp["fallback"]["used"] is False
    assert b_first["cache"]["status"] == "miss"
    assert a_hit["cache"]["status"] == "hit_exact" and a_hit["answer"] == b_first["answer"]
    assert last_ok.status_code == 200 and over.status_code == 429
    assert summary["total_requests"] == 5  # history from both instances (429s are not persisted)
    assert a.app.state.container.redis is not b.app.state.container.redis  # truly separate pools


def test_request_id_flows_to_persistence(infra_settings, pg_clean):
    from sqlalchemy import select

    from app.db.models import RequestLog

    with client(infra_settings(database_enabled=True, rate_limit_enabled=False)) as c:
        c.post(CHAT, json=Q, headers={"X-Request-ID": "trace-me-123"})
        flush(c)
        db = c.app.state.container.database
        ids = c.portal.call(db.run, lambda s: list(s.execute(select(RequestLog.request_id)).scalars()))
    assert ids == ["trace-me-123"]


def test_cache_entries_expire_over_http(infra_settings):
    with client(infra_settings(cache_ttl_seconds=1, rate_limit_enabled=False)) as c:
        c.post(CHAT, json=Q)
        time.sleep(1.2)
        again = c.post(CHAT, json=Q).json()
    assert again["cache"]["status"] == "miss"
