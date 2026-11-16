"""Profile in-process /api/v1/chat with cProfile (mock provider, isolated namespaces).

    cd backend
    python -m benchmarks.profile                    # hermetic (no Redis/DB)
    python -m benchmarks.profile --config cache     # Redis cache + Redis health (DB 15, bench prefix)
    python -m benchmarks.profile --requests 500 --top 25

Prints the top functions by own time (tottime) and cumulative time, and writes
the raw .pstats file to benchmarks/results/ for further inspection
(e.g. `python -m pstats benchmarks/results/profile-cache.pstats`).
"""

import argparse
import asyncio
import cProfile
import io
import logging
import os
import pstats
import sys

import benchmarks  # noqa: F401  (sets LLMR_DISABLE_ENV_FILE before app imports)
from benchmarks.run import RESULTS_DIR, probe_redis
from benchmarks.scenarios import BenchConfig, _clear_prefix, bench_settings, chat_op, corpus_query, running_app, unique_query

CONFIGS = {
    "hermetic": dict(),
    "cache": dict(redis_enabled=True, cache_enabled=True, health_backend="redis"),
    "tracing": dict(tracing_enabled=True, tracing_exporter="none"),
}


async def _drive(cfg: BenchConfig, config: str, requests: int, profiler: cProfile.Profile) -> None:
    settings = bench_settings(cfg, **CONFIGS[config])
    query = unique_query if config == "cache" else corpus_query  # cache: profile the (expensive) miss path
    async with running_app(settings) as (_app, client):
        op = chat_op(client, query)
        for i in range(20):  # warm-up, not profiled
            await op(-1 - i)
        profiler.enable()
        for i in range(requests):
            await op(i)
        profiler.disable()
    if config == "cache":
        await _clear_prefix(cfg)


def report(profiler: cProfile.Profile, top: int) -> str:
    out = io.StringIO()
    for key in ("tottime", "cumulative"):
        out.write(f"\n===== top {top} by {key} =====\n")
        pstats.Stats(profiler, stream=out).strip_dirs().sort_stats(key).print_stats(top)
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", choices=sorted(CONFIGS), default="hermetic")
    p.add_argument("--requests", type=int, default=300)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="root log level during profiling (log output goes to os.devnull)")
    args = p.parse_args(argv)
    # Set the process-wide level BEFORE app.main is imported (its module-level app
    # would otherwise configure INFO), and send log output to the null device.
    logging.basicConfig(level=args.log_level, stream=open(os.devnull, "w"))  # noqa: SIM115
    cfg = BenchConfig()
    if args.config == "cache" and not probe_redis(cfg.redis_db):
        print("Redis not reachable; cannot profile the cache configuration", file=sys.stderr)
        return 1
    profiler = cProfile.Profile()
    asyncio.run(_drive(cfg, args.config, args.requests, profiler))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"profile-{args.config}.pstats"
    profiler.dump_stats(path)
    print(report(profiler, args.top))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
