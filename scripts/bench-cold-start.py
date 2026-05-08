"""Cold-start latency benchmark for Shout.

Measures the four phases of starting a fresh Shout session:
    T0 → T1   import time            (Python + parakeet_mlx + mlx imports)
    T1 → T2   model load              (from_pretrained, including HF cache hit/miss)
    T2 → T3   first audio chunk pushed (synthetic 1s of silence)
    T3 → T4   first non-empty token   (any draft or finalized token emitted)

The acceptance threshold from the v0 PRD is <1.5s perceived latency from
word-spoken to word-typed. The dominant phase here is model load, which
this bench isolates so we can decide whether cold-start-per-session is
viable or whether we need a warm daemon.

Usage:
    uv run python scripts/bench-cold-start.py [--model HF_ID] [--runs N]
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_RUNS = 3


def run_single_trial(model_id: str) -> dict:
    """Run one cold-start measurement in a fresh subprocess.

    Subprocessing is the only way to get a true cold start: even a
    fresh function call inside a long-running interpreter would benefit
    from already-imported modules and a warm MLX runtime.
    """
    script = f"""
import time, sys, json
T0 = time.perf_counter()

import numpy as np
import mlx.core as mx
import parakeet_mlx
T1 = time.perf_counter()

model = parakeet_mlx.from_pretrained({model_id!r})
T2 = time.perf_counter()

sample_rate = model.preprocessor_config.sample_rate
silence = mx.zeros(sample_rate)  # 1 second of silence at the model's rate

with model.transcribe_stream(context_size=(256, 8)) as streamer:
    streamer.add_audio(silence)
    T3 = time.perf_counter()
    # Note: silence is unlikely to produce tokens; what we are measuring
    # here is the time-to-first-decode-pass (one call to add_audio), which
    # is the actual gate on the user seeing anything happen.

print(json.dumps({{
    "import_s": T1 - T0,
    "model_load_s": T2 - T1,
    "first_chunk_s": T3 - T2,
    "total_s": T3 - T0,
    "sample_rate": sample_rate,
}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  trial failed: {result.stderr}", file=sys.stderr)
        return {}
    # Last line of stdout is the JSON; earlier lines may be MLX setup chatter.
    last_line = result.stdout.strip().split("\n")[-1]
    return json.loads(last_line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    print(f"Benchmarking cold start for {args.model} ({args.runs} runs)\n")

    trials: list[dict] = []
    for i in range(args.runs):
        print(f"  run {i + 1}/{args.runs} ...", end=" ", flush=True)
        t = run_single_trial(args.model)
        if not t:
            print("FAILED")
            continue
        trials.append(t)
        print(f"{t['total_s']:.2f}s total")

    if not trials:
        print("All runs failed.", file=sys.stderr)
        return 1

    print("\nResults (seconds):")
    print(f"  {'phase':<18}  {'first':>8}  {'median':>8}  {'min':>8}  {'max':>8}")
    for phase in ("import_s", "model_load_s", "first_chunk_s", "total_s"):
        values = [t[phase] for t in trials]
        print(
            f"  {phase:<18}  "
            f"{values[0]:>8.2f}  "
            f"{statistics.median(values):>8.2f}  "
            f"{min(values):>8.2f}  "
            f"{max(values):>8.2f}"
        )

    first_total = trials[0]["total_s"]
    warm_median = (
        statistics.median(t["total_s"] for t in trials[1:]) if len(trials) > 1 else None
    )

    print("\nVerdict (vs <1.5s acceptance criterion):")
    print(
        f"  first run total: {first_total:.2f}s  "
        f"({'PASS' if first_total < 1.5 else 'FAIL'})"
    )
    if warm_median is not None:
        print(
            f"  warm-cache median: {warm_median:.2f}s  "
            f"({'PASS' if warm_median < 1.5 else 'FAIL'})"
        )

    print("\nInterpretation:")
    if first_total < 1.5:
        print("  Cold start is fast enough. Spawn-per-session is viable.")
    elif warm_median is not None and warm_median < 1.5:
        print(
            "  Cold start is too slow on first run, but warm-cache cold-start is fine.\n"
            "  Mitigation: warm the HF cache + MLX shaders at install time, then\n"
            "  spawn-per-session is still viable for daily use."
        )
    else:
        print(
            "  Cold start exceeds the 1.5s budget. Switch to a long-running\n"
            "  daemon (loads model once, accepts start/stop signals over a\n"
            "  Unix socket or via SIGTERM)."
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(trials, indent=2))
        print(f"\nWrote raw trials to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
