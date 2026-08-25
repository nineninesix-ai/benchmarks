# Copyright 2026 The Coval Benchmarks Authors
# SPDX-License-Identifier: Apache-2.0

"""Repeat the TTS dataset N times against one model and pool the raw measurements.

``coval-bench probe`` reports one aggregate per invocation, which is the wrong
shape for characterising a latency *distribution*: a dataset pass is only 30
prompts, and averaging each pass's summary corrupts the pooled median and p90.
This script runs the production per-item pipeline (``_run_tts_item``) pass after
pass, writes one JSON row per synthesis, and reports percentiles computed over
every row at once.

It runs at concurrency 1 by design. A second in-flight stream contends with the
first and inflates TTFA, so the number would stop meaning what it claims to.
Voices are drawn from the registry pool exactly as a scheduled run draws them.

Nothing is written to the database.

Usage::

    python scripts/tts_repeat_bench.py --provider nineninesix --model gepard-1.0 \
        --passes 6 --out /tmp/run.jsonl

    # Characterise a candidate deployment instead of the production endpoint:
    NINENINESIX_BASE_URL=http://10.0.0.7:8000 python scripts/tts_repeat_bench.py \
        --provider nineninesix --model gepard-1.0 --passes 6 --out /tmp/pod.jsonl

    # Subtract a known network round trip so two regions can be compared:
    python scripts/tts_repeat_bench.py --provider nineninesix --model gepard-1.0 \
        --passes 6 --rtt-ms 48.7 --out /tmp/pod.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from coval_bench.config import get_settings
from coval_bench.datasets.loader import load_tts_dataset
from coval_bench.registries import MODEL_REGISTRY, Benchmark, Metric
from coval_bench.runner.orchestrator import _assign_tts_voices, _run_tts_item

# A request that lands in a second latency mode rather than the fast path. Not a
# provider SLA — just the reporting threshold for "how often is this slow".
SLOW_MS = 150.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, help="Registry provider name.")
    parser.add_argument("--model", required=True, help="Registry model id.")
    parser.add_argument(
        "--passes", type=int, default=1, help="Dataset passes; 30 prompts each. Default 1."
    )
    parser.add_argument("--dataset", default="tts-v1", help="Dataset id. Default tts-v1.")
    parser.add_argument("--out", type=Path, help="Write one JSON row per synthesis here.")
    parser.add_argument(
        "--pause-s",
        type=float,
        default=0.0,
        help="Idle this long before each request, to test load-dependence. Default 0.",
    )
    parser.add_argument(
        "--rtt-ms",
        type=float,
        default=0.0,
        help=(
            "Subtract this network round trip from TTFA and roundtrip figures, so an "
            "out-of-region endpoint is comparable to a local one. Measure it with "
            "`curl -o /dev/null -w '%%{time_connect}' <endpoint>/health`."
        ),
    )
    return parser.parse_args()


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _report(rows: list[dict[str, Any]], rtt_ms: float) -> None:
    """Print percentiles pooled over every row, not per pass."""
    ttfa = [r["ttfa_ms"] - rtt_ms for r in rows if r["ttfa_ms"] is not None]
    trip = [r["roundtrip_ms"] - rtt_ms for r in rows if r["roundtrip_ms"] is not None]
    silence = [r["leading_silence_ms"] for r in rows if r["leading_silence_ms"] is not None]
    wer = [r["wer"] for r in rows if r["wer"] is not None]
    errors = sum(1 for r in rows if r["error"])

    note = f"  (RTT-normalized: {rtt_ms:.1f} ms subtracted)" if rtt_ms else ""
    print(f"\nn={len(rows)}  errors={errors}{note}")
    if not ttfa:
        print("no successful syntheses")
        return

    def line(name: str, values: list[float], unit: str = "ms") -> None:
        print(
            f"  {name:<22} median={statistics.median(values):7.1f}{unit} "
            f"mean={statistics.mean(values):7.1f} p90={_pct(values, 0.9):7.1f} "
            f"p95={_pct(values, 0.95):7.1f} max={max(values):7.1f}"
        )

    line("TTFA (perceived)", ttfa)
    if trip:
        line("  roundtrip", trip)
    if silence:
        line("  leading silence", silence)
    if wer:
        line("WER", wer, "%")
        perfect = sum(1 for value in wer if value == 0)
        print(f"  {'':<22} perfect={perfect}/{len(wer)} ({100 * perfect / len(wer):.1f}%)")

    slow = [value for value in ttfa if value > SLOW_MS]
    rate = 100 * len(slow) / len(ttfa)
    print(f"  slow mode (>{SLOW_MS:.0f} ms): {len(slow)}/{len(ttfa)} = {rate:.1f}%")
    if slow:
        print(f"  slow values: {[round(value, 1) for value in sorted(slow)]}")


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    settings = get_settings()
    entry = next(
        (
            m
            for m in MODEL_REGISTRY
            if m.benchmark is Benchmark.TTS
            and m.provider == args.provider
            and m.model == args.model
        ),
        None,
    )
    if entry is None:
        known = sorted(
            f"{m.provider}/{m.model}" for m in MODEL_REGISTRY if m.benchmark is Benchmark.TTS
        )
        print(f"unknown TTS model {args.provider}/{args.model}. Known: {known}", file=sys.stderr)
        raise SystemExit(2)

    dataset = load_tts_dataset(args.dataset, settings=settings)
    semaphore = asyncio.Semaphore(1)
    rows: list[dict[str, Any]] = []
    handle = args.out.open("w") if args.out else None

    try:
        for run_id in range(1, args.passes + 1):
            voices = _assign_tts_voices(entry, len(dataset.items), run_id)
            for index, item in enumerate(dataset.items):
                if args.pause_s:
                    await asyncio.sleep(args.pause_s)
                results = await _run_tts_item(
                    entry=entry,
                    item=item,
                    run_id=run_id,
                    sem=semaphore,
                    settings=settings,
                    writer=None,
                    voice=voices[index],
                )
                by_metric = {r.metric_type: r for r in results}
                ttfa = by_metric.get(Metric.TTFA)
                wer = by_metric.get(Metric.WER)
                roundtrip = by_metric.get(Metric.TTFA_ROUNDTRIP)
                silence = by_metric.get(Metric.TTFA_LEADING_SILENCE)
                row = {
                    "pass": run_id,
                    "item": index,
                    "voice": voices[index],
                    "ttfa_ms": ttfa.metric_value if ttfa else None,
                    "roundtrip_ms": roundtrip.metric_value if roundtrip else None,
                    "leading_silence_ms": silence.metric_value if silence else None,
                    "wer": wer.metric_value if wer else None,
                    "source": item.transcript,
                    "heard": wer.transcript if wer else None,
                    "error": ttfa.error if ttfa else None,
                }
                rows.append(row)
                if handle:
                    handle.write(json.dumps(row) + "\n")
                    handle.flush()
            print(f"pass {run_id}/{args.passes} done ({len(rows)} samples)", file=sys.stderr)
    finally:
        if handle:
            handle.close()

    return rows


def main() -> None:
    args = _parse_args()
    rows = asyncio.run(_run(args))
    _report(rows, args.rtt_ms)
    if args.out:
        print(f"\nraw rows: {args.out}")


if __name__ == "__main__":
    main()
