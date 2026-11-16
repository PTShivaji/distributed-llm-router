"""Benchmark command line.

    cd backend
    python -m benchmarks.run                         # default groups, JSON + table
    python -m benchmarks.run --scenarios micro,cache --requests 1000 --concurrency 20
    python -m benchmarks.run --scenarios http        # real HTTP (spawns uvicorn processes)
    python -m benchmarks.run --quick                 # small, fast sanity run

Results: backend/benchmarks/results/<UTC timestamp>.json (+ latest.json).
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import benchmarks  # noqa: F401  (sets LLMR_DISABLE_ENV_FILE before app imports)
from benchmarks.environment import environment_info
from benchmarks.scenarios import (
    BENCH_HOST,
    BENCH_TEST_DB,
    DEFAULT_GROUPS,
    GROUPS,
    BenchConfig,
    BenchmarkDatabaseError,
    ScenarioResult,
    require_bench_database,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SCHEMA_VERSION = 1


def probe_redis(db: int) -> bool:
    import redis

    try:
        return bool(redis.Redis(host=BENCH_HOST, port=6379, db=db, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


def probe_postgres(test_db: str = BENCH_TEST_DB) -> bool:
    """Connect DIRECTLY to the existing isolated benchmark database and verify it.

    Exactly one connection attempt, always to BENCH_TEST_DB: no catalog lookup through
    another database, no database creation and no fallback. Returns True when usable;
    otherwise raises BenchmarkDatabaseError with a clear message (never credentials).
    """
    require_bench_database(test_db)
    import psycopg

    from app.core.config import Settings

    s = Settings(_env_file=None)
    where = f"{BENCH_HOST}:{s.postgres_port}"
    conninfo = (f"host={BENCH_HOST} port={s.postgres_port} user={s.postgres_user} "
                f"password={s.postgres_password.get_secret_value()} dbname={BENCH_TEST_DB} connect_timeout=2")
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            name = conn.execute("SELECT current_database()").fetchone()[0]
    except psycopg.Error as exc:
        if getattr(exc, "sqlstate", None) == "3D000" or "does not exist" in str(exc):
            raise BenchmarkDatabaseError(
                f"benchmark database {BENCH_TEST_DB!r} does not exist on {where}. Benchmarks never create "
                f"databases; create it yourself, or omit the postgres group with --scenarios.") from None
        raise BenchmarkDatabaseError(
            f"cannot connect to benchmark database {BENCH_TEST_DB!r} on {where} ({type(exc).__name__}); "
            f"start PostgreSQL or omit the postgres group with --scenarios.") from None
    if name != BENCH_TEST_DB:
        raise BenchmarkDatabaseError(f"connected to {name!r} instead of {BENCH_TEST_DB!r}; refusing to benchmark")
    return True


async def run_benchmarks(cfg: BenchConfig, groups: list[str]) -> dict[str, Any]:
    results: list[ScenarioResult] = []
    skipped: dict[str, str] = {}
    timings: dict[str, float] = {}
    for group in groups:
        fn, needs = GROUPS[group]
        if needs == "redis" and not cfg.redis_available:
            skipped[group] = "Redis not reachable on 127.0.0.1:6379"
            continue
        if needs == "postgres" and not cfg.postgres_available:
            skipped[group] = "PostgreSQL not reachable on 127.0.0.1:5433"
            continue
        started = time.perf_counter()
        print(f"[bench] running {group} ...", file=sys.stderr, flush=True)
        results.extend(await fn(cfg))
        timings[group] = round(time.perf_counter() - started, 2)
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": environment_info(),
        "parameters": {
            "requests": cfg.requests, "concurrency": cfg.concurrency, "warmup": cfg.warmup, "rounds": cfg.rounds,
            "concurrency_levels": list(cfg.concurrency_levels), "cache_scope_entries": cfg.cache_scope_entries,
            "groups": groups, "redis_db": cfg.redis_db, "postgres_db": cfg.postgres_db,
            "redis_available": cfg.redis_available, "postgres_available": cfg.postgres_available,
        },
        "group_durations_s": timings,
        "skipped_groups": skipped,
        "results": [r.to_dict() for r in results],
    }


def write_results(report: dict[str, Any], output: Path | None) -> list[Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    paths = [output] if output else [RESULTS_DIR / f"benchmark-{stamp}.json", RESULTS_DIR / "latest.json"]
    text = json.dumps(report, indent=2, default=str)
    for p in paths:
        if p.name == ".env":
            raise ValueError("refusing to write benchmark output to a .env file")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return paths


def format_table(report: dict[str, Any]) -> str:
    head = f"{'scenario':42} {'kind':10} {'n':>6} {'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'min':>7} {'max':>8} {'rps':>9} {'err%':>5} {'hit%':>5}"
    lines = [head, "-" * len(head)]
    for r in report["results"]:
        s = r["stats"]

        def ms(v: float | None) -> str:
            return "-" if v is None else (f"{v:.3f}" if v < 10 else f"{v:.1f}")

        hit = "-" if s["cache_hit_ratio"] is None else f"{s['cache_hit_ratio'] * 100:.0f}"
        rps = "-" if s["requests_per_second"] is None else f"{s['requests_per_second']:.0f}"
        lines.append(f"{r['name'][:42]:42} {r['kind']:10} {s['count']:>6} {ms(s['mean_ms']):>8} {ms(s['p50_ms']):>8} "
                     f"{ms(s['p95_ms']):>8} {ms(s['p99_ms']):>8} {ms(s['min_ms']):>7} {ms(s['max_ms']):>8} "
                     f"{rps:>9} {s['error_rate'] * 100:>5.1f} {hit:>5}")
    lines.append("latencies in ms; rps = completed operations / wall-clock seconds")
    for g, why in report["skipped_groups"].items():
        lines.append(f"SKIPPED {g}: {why}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Distributed LLM Router benchmarks (mock providers only)")
    p.add_argument("--scenarios", default=",".join(DEFAULT_GROUPS),
                   help=f"comma-separated groups: {','.join(GROUPS)} (http = real HTTP, opt-in)")
    p.add_argument("--requests", type=int, default=600, help="measured operations per scenario")
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--rounds", type=int, default=3, help="interleaved rounds for A/B comparisons")
    p.add_argument("--concurrency-levels", default="1,10,50")
    p.add_argument("--cache-scope-entries", type=int, default=1000)
    p.add_argument("--output", type=Path, default=None, help="JSON file (default: benchmarks/results/)")
    p.add_argument("--quick", action="store_true", help="tiny run for sanity checks")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Root level WARNING before app.main is imported (its module-level app would
    # configure INFO). The `logging` group measures INFO explicitly.
    logging.basicConfig(level=logging.WARNING)
    groups = [g.strip() for g in args.scenarios.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        print(f"unknown scenario group(s): {unknown}", file=sys.stderr)
        return 2
    cfg = BenchConfig(requests=args.requests, concurrency=args.concurrency, warmup=args.warmup, rounds=args.rounds,
                      concurrency_levels=tuple(int(x) for x in args.concurrency_levels.split(",")),
                      cache_scope_entries=args.cache_scope_entries)
    if args.quick:
        cfg.requests, cfg.warmup, cfg.rounds, cfg.concurrency_levels, cfg.cache_scope_entries = 60, 5, 2, (1, 5), 100
    cfg.redis_available = probe_redis(cfg.redis_db)
    # PostgreSQL is probed only when a requested group needs it; an unusable test database aborts.
    if any(GROUPS[g][1] == "postgres" for g in groups):
        try:
            cfg.postgres_available = probe_postgres(cfg.postgres_db)
        except BenchmarkDatabaseError as exc:
            print(f"benchmark aborted: {exc}", file=sys.stderr)
            return 2
    report = asyncio.run(run_benchmarks(cfg, groups))
    paths = write_results(report, args.output)
    print(format_table(report))
    for path in paths:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
