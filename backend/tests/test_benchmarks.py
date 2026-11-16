"""Phase 5 step 5: benchmark utilities, result generation, JSON output and smoke runs."""

import asyncio
import json
import statistics

import pytest

from benchmarks.environment import SAFE_SETTING_KEYS, environment_info, safe_settings
from benchmarks.run import format_table, parse_args, run_benchmarks, write_results
from benchmarks.runner import OpResult, RunOutcome, run_async, run_sync
from benchmarks.scenarios import CORPUS, GROUPS, BenchConfig, bench_settings, corpus_query, unique_query
from benchmarks.stats import percentile, summarize

STAT_KEYS = {"count", "errors", "error_rate", "mean_ms", "median_ms", "p50_ms", "p95_ms", "p99_ms", "min_ms",
             "max_ms", "stdev_ms", "duration_s", "requests_per_second", "cache_hit_ratio", "cache_lookups"}


# --------------------------------------------------------------------------- #
# Percentiles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("p, expected", [(0, 1), (25, 1.75), (50, 2.5), (75, 3.25), (90, 3.7), (100, 4)])
def test_percentile_linear_interpolation(p, expected):
    assert percentile([4, 1, 3, 2], p) == pytest.approx(expected)


def test_percentile_matches_reference_definition_on_1_to_100():
    values = list(range(1, 101))  # numpy.percentile(1..100, q, method="linear")
    assert percentile(values, 50) == pytest.approx(50.5)
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile(values, 99) == pytest.approx(99.01)


def test_percentile_edge_cases():
    assert percentile([], 50) is None
    assert percentile([7.5], 99) == 7.5
    assert percentile([3, 3, 3], 95) == 3
    with pytest.raises(ValueError):
        percentile([1, 2], 101)
    with pytest.raises(ValueError):
        percentile([1, 2], -1)


def test_percentile_does_not_mutate_input():
    data = [5, 1, 4]
    percentile(data, 50)
    assert data == [5, 1, 4]


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def test_summarize_fields_and_rates():
    lat = [1.0, 2.0, 3.0, 4.0, 10.0]
    s = summarize(lat, errors=1, duration_s=0.5)
    assert s.count == 5 and s.errors == 1 and s.error_rate == pytest.approx(0.2)
    assert s.mean_ms == pytest.approx(4.0) and s.median_ms == 3.0 and s.p50_ms == 3.0
    assert s.min_ms == 1.0 and s.max_ms == 10.0
    assert s.stdev_ms == pytest.approx(statistics.stdev(lat))
    assert s.requests_per_second == pytest.approx(10.0)
    assert s.cache_hit_ratio is None and s.cache_lookups is None
    assert set(s.to_dict()) == STAT_KEYS


def test_summarize_cache_hit_ratio_ignores_non_lookups():
    s = summarize([1] * 6, duration_s=1, cache_statuses=["hit_exact", "hit_semantic", "miss", "bypass", "disabled", None])
    assert s.cache_lookups == 3 and s.cache_hit_ratio == pytest.approx(2 / 3)


def test_summarize_empty():
    s = summarize([], duration_s=0)
    assert s.count == 0 and s.mean_ms is None and s.requests_per_second is None and s.error_rate == 0.0


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
def test_run_async_counts_requests_concurrency_and_errors():
    seen, in_flight, peak = [], [0], [0]

    async def op(i):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0.001)
        in_flight[0] -= 1
        seen.append(i)
        if i == 3:
            raise RuntimeError("boom")
        return OpResult(ok=i != 5, cache_status="miss")

    out = asyncio.run(run_async(op, requests=20, concurrency=4, warmup=3))
    measured = [i for i in seen if i >= 0]
    assert sorted(measured) == list(range(20))           # every index exactly once
    assert sorted(i for i in seen if i < 0) == [-3, -2, -1]  # warm-up not measured
    assert len(out.latencies_ms) == 20 and out.errors == 2 and peak[0] == 4
    assert out.duration_s > 0


def test_run_async_validates_arguments():
    async def op(i):
        return OpResult()

    with pytest.raises(ValueError):
        asyncio.run(run_async(op, requests=1, concurrency=0))


def test_run_sync_and_merge():
    a = run_sync(lambda i: i * 2, requests=10, warmup=2)
    b = run_sync(lambda i: 1 / 0, requests=3)
    merged = a.merge(b)
    assert len(merged.latencies_ms) == 13 and merged.errors == 3


def test_distinct_queries_are_deterministic_and_never_semantic_matches():
    from app.embeddings.base import cosine_similarity
    from app.embeddings.local import LocalHashingEmbedder
    from benchmarks.scenarios import distinct_query

    assert distinct_query(3, "a") == distinct_query(3, "a") != distinct_query(3, "b")
    e = LocalHashingEmbedder(256)
    vecs = [e.embed_sync(distinct_query(i, "t")) for i in range(60)]
    worst = max(cosine_similarity(vecs[i], vecs[j]) for i in range(60) for j in range(i + 1, 60))
    assert worst < 0.92


def test_workload_is_deterministic():
    assert [corpus_query(i) for i in range(30)] == [corpus_query(i) for i in range(30)]
    assert len({unique_query(i) for i in range(500)}) == 500
    assert unique_query(-1) != unique_query(1)
    assert len(CORPUS) == 12


# --------------------------------------------------------------------------- #
# Configuration safety
# --------------------------------------------------------------------------- #
def test_bench_settings_are_isolated_and_hermetic():
    s = bench_settings(BenchConfig(prefix="bench:xyz:"))
    assert s.llm_mock_mode is True and s.mock_failures == {}
    assert s.redis_db == 15 and s.redis_key_prefix == "bench:xyz:" and s.postgres_db == "llm_router_test"
    assert not (s.redis_enabled or s.cache_enabled or s.rate_limit_enabled or s.database_enabled or s.tracing_enabled)


def test_environment_info_and_safe_settings_contain_no_secrets():
    info = environment_info()
    for key in ("python_version", "os", "cpu_count_logical", "packages", "measurement_notes", "generated_at"):
        assert key in info
    s = bench_settings(BenchConfig(), openai_api_key="sk-proj-BENCHSECRET0001", admin_api_token="bench-admin-002")
    dumped = json.dumps({"env": info, "settings": safe_settings(s)}, default=str)
    assert "BENCHSECRET0001" not in dumped and "bench-admin-002" not in dumped and "router_password" not in dumped
    assert set(safe_settings(s)) <= set(SAFE_SETTING_KEYS)


def test_cli_defaults_and_http_is_opt_in():
    args = parse_args([])
    assert "http" not in args.scenarios.split(",")
    assert set(args.scenarios.split(",")) <= set(GROUPS)
    assert args.requests > 0 and args.concurrency > 0 and args.warmup > 0


def test_write_results_refuses_env_file(tmp_path):
    with pytest.raises(ValueError):
        write_results({"results": []}, tmp_path / ".env")


# --------------------------------------------------------------------------- #
# Smoke: real (small) runs + JSON output
# --------------------------------------------------------------------------- #
SMOKE = dict(requests=12, concurrency=3, warmup=2, rounds=2, concurrency_levels=(1, 3), cache_scope_entries=20)


def _validate_report(report):
    json.loads(json.dumps(report, default=str))  # serialisable
    assert report["schema_version"] == 1 and report["environment"]["python_version"]
    for r in report["results"]:
        assert {"name", "group", "kind", "description", "parameters", "stats"} <= set(r)
        assert r["kind"] in {"micro", "in_process", "redis", "postgres", "real_http"}
        assert set(r["stats"]) == STAT_KEYS
        assert r["stats"]["count"] > 0 and r["stats"]["error_rate"] == 0.0, r["name"]
        assert r["stats"]["p50_ms"] <= r["stats"]["p95_ms"] <= r["stats"]["p99_ms"] <= r["stats"]["max_ms"]


def test_smoke_hermetic_groups(tmp_path):
    import logging

    root = logging.getLogger()
    handlers_before, level_before = list(root.handlers), root.level
    cfg = BenchConfig(**SMOKE)
    report = asyncio.run(run_benchmarks(cfg, ["micro", "inprocess", "metrics", "logging", "tracing"]))
    assert root.handlers == handlers_before and root.level == level_before  # logging group restored state
    _validate_report(report)
    names = {r["name"] for r in report["results"]}
    assert {"micro.classification", "micro.routing_select", "micro.mock_provider", "inprocess.chat_c1",
            "metrics.enabled", "metrics.disabled", "tracing.off", "tracing.sample_0.1",
            "logging.level_info", "logging.level_warning"} <= names
    path = write_results(report, tmp_path / "out.json")[0]
    assert json.loads(path.read_text())["results"][0]["stats"]["count"] > 0
    assert "micro.classification" in format_table(report)


def test_smoke_redis_and_cache_groups(redis_prefix):
    import redis

    cfg = BenchConfig(**SMOKE, prefix=redis_prefix, redis_available=True)
    report = asyncio.run(run_benchmarks(cfg, ["cache", "redis", "multi"]))
    _validate_report(report)
    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["cache.hit_exact"]["stats"]["cache_hit_ratio"] == 1.0
    assert by_name["cache.hit_semantic"]["stats"]["cache_hit_ratio"] == 1.0
    assert by_name["cache.miss_full_scope_20"]["stats"]["cache_hit_ratio"] == 0.0
    assert by_name["cache.miss_distinct_queries"]["stats"]["cache_hit_ratio"] == 0.0
    assert "warning" in by_name["cache.near_duplicate_numbered_queries"]["extra"]
    # Step 6A: the equivalence guard rejects numbered near-duplicates (was 62% wrong hits before).
    assert by_name["cache.near_duplicate_numbered_queries"]["stats"]["cache_hit_ratio"] == 0.0
    assert by_name["cache.miss_distinct_queries_semantic"]["stats"]["cache_hit_ratio"] == 0.0
    assert by_name["cache.miss_distinct_queries"]["settings"]["cache_semantic_enabled"] is False  # default
    leftover = list(redis.Redis(db=15).scan_iter(match=redis_prefix + "*"))
    assert leftover == []  # benchmarks clean their own namespace


def test_smoke_postgres_group(pg_clean):
    cfg = BenchConfig(**SMOKE, postgres_available=True)
    report = asyncio.run(run_benchmarks(cfg, ["postgres"]))
    _validate_report(report)
    assert {r["name"] for r in report["results"]} == {
        "postgres.persist_batch_1", "postgres.persist_batch_50", "postgres.analytics_summary"}


def test_unavailable_dependencies_are_reported_as_skipped():
    report = asyncio.run(run_benchmarks(BenchConfig(**SMOKE), ["cache", "postgres"]))
    assert report["results"] == []
    assert set(report["skipped_groups"]) == {"cache", "postgres"}


def test_postgres_benchmark_refuses_development_database():
    from benchmarks.scenarios import scenario_postgres

    with pytest.raises(RuntimeError):
        asyncio.run(scenario_postgres(BenchConfig(**{**SMOKE, "postgres_db": "llm_router"})))
