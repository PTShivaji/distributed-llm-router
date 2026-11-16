"""Interleaved A/B comparison of two source trees with the same benchmark workload.

    cd backend
    python -m benchmarks.ab --a <baseline backend dir> --b . --scenarios cache --rounds 3 --requests 300

Each tree is a directory containing `app/` and `benchmarks/` (e.g. a snapshot of
backend/ taken before a change). For every round both trees run
`python -m benchmarks.run` in separate processes, alternating the order
(A B, B A, A B, ...) to cancel drift and warm-up bias. Per scenario we report the
median across rounds and the min..max range for each side.

Verdict rule (conservative): a change is reported as an improvement or a
regression only when BOTH hold:
  1. the per-round ranges do NOT overlap (e.g. B's worst p50 beats A's best p50), and
  2. the median changes by at least --min-effect percent (default 15%).
An A/A control (same tree on both sides, 3 rounds) on this machine showed median
differences up to ~25% and one non-overlapping 10.4% difference, hence both rules.
Otherwise: "within noise".
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

METRICS = (("p50_ms", "lower"), ("p95_ms", "lower"), ("p99_ms", "lower"),
           ("requests_per_second", "higher"), ("error_rate", "lower"))


def run_tree(tree: Path, args: argparse.Namespace, out: Path) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "benchmarks.run", "--scenarios", args.scenarios, "--requests", str(args.requests),
           "--concurrency", str(args.concurrency), "--warmup", str(args.warmup), "--rounds", str(args.inner_rounds),
           "--cache-scope-entries", str(args.cache_scope_entries), "--output", str(out)]
    subprocess.run(cmd, cwd=tree, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(out.read_text(encoding="utf-8"))


def verdict(a: list[float], b: list[float], direction: str, min_effect_pct: float = 15.0) -> str:
    if not a or not b or any(v is None for v in a + b):
        return "n/a"
    med_a, med_b = statistics.median(a), statistics.median(b)
    if med_a == 0 or abs(med_b - med_a) / abs(med_a) * 100 < min_effect_pct:
        return "within noise"
    if direction == "lower":
        if max(b) < min(a):
            return "improved"
        if min(b) > max(a):
            return "regressed"
    else:
        if min(b) > max(a):
            return "improved"
        if max(b) < min(a):
            return "regressed"
    return "within noise"


def summarize(rounds: dict[str, list[dict[str, Any]]], min_effect_pct: float = 15.0) -> list[dict[str, Any]]:
    names = [r["name"] for r in rounds["A"][0]["results"]]
    rows = []
    for name in names:
        per_side = {side: [next(r for r in rep["results"] if r["name"] == name)["stats"] for rep in reps]
                    for side, reps in rounds.items()}
        row: dict[str, Any] = {"scenario": name}
        for metric, direction in METRICS:
            a = [s[metric] for s in per_side["A"]]
            b = [s[metric] for s in per_side["B"]]
            med_a, med_b = statistics.median(a), statistics.median(b)
            row[metric] = {
                "A_median": med_a, "B_median": med_b, "A_range": [min(a), max(a)], "B_range": [min(b), max(b)],
                "change_pct": round((med_b - med_a) / med_a * 100, 1) if med_a else None,
                "verdict": verdict(a, b, direction, min_effect_pct),
            }
        rows.append(row)
    return rows


def format_rows(rows: list[dict[str, Any]]) -> str:
    out = [f"{'scenario':44} {'metric':8} {'A median':>10} {'B median':>10} {'change':>8}  {'A range':>19}  {'B range':>19}  verdict"]
    for row in rows:
        for metric, _ in METRICS[:4]:
            m = row[metric]
            label = {"p50_ms": "p50", "p95_ms": "p95", "p99_ms": "p99", "requests_per_second": "rps"}[metric]
            out.append(f"{row['scenario'][:44]:44} {label:8} {m['A_median']:>10.3f} {m['B_median']:>10.3f} "
                       f"{(m['change_pct'] or 0):>7.1f}%  {m['A_range'][0]:>8.3f}..{m['A_range'][1]:<8.3f}  "
                       f"{m['B_range'][0]:>8.3f}..{m['B_range'][1]:<8.3f}  {m['verdict']}")
        e = row["error_rate"]
        out.append(f"{'':44} {'errors':8} {e['A_median']:>10.3f} {e['B_median']:>10.3f}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--a", type=Path, required=True, help="baseline tree (contains app/ and benchmarks/)")
    p.add_argument("--b", type=Path, required=True, help="candidate tree")
    p.add_argument("--scenarios", required=True)
    p.add_argument("--rounds", type=int, default=3, help="interleaved A/B process rounds")
    p.add_argument("--inner-rounds", type=int, default=1, help="--rounds passed to benchmarks.run")
    p.add_argument("--requests", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--cache-scope-entries", type=int, default=1000)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--min-effect", type=float, default=15.0, help="minimum median change (%%) to call a verdict")
    args = p.parse_args(argv)

    rounds: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(args.rounds):
            order = ("A", "B") if i % 2 == 0 else ("B", "A")
            for side in order:
                tree = (args.a if side == "A" else args.b).resolve()
                print(f"[ab] round {i + 1}/{args.rounds}: {side}", file=sys.stderr, flush=True)
                rounds[side].append(run_tree(tree, args, Path(tmp) / f"{side}-{i}.json"))
    rows = summarize(rounds, args.min_effect)
    report = {"a": str(args.a), "b": str(args.b), "parameters": vars(args) | {"a": str(args.a), "b": str(args.b),
                                                                            "output": str(args.output)},
              "rows": rows, "raw": rounds}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(format_rows(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
