"""Kokoro TTS latency benchmark.

Measures the configured Kokoro path end to end (synthesis only, no playback)
over the task's four representative phrases, 5 warm trials each, reporting
median, range, audio duration, and real-time factor.

    .venv/bin/python scripts/tts_latency.py [--md docs/history/tts_latency_<date>.md]

`--md` writes a dated report; it defaults to off. Pass a fresh filename: the
committed baselines in docs/history/ are the record of what was measured then,
and re-running the documented command must not overwrite one of them.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PHRASES = [
    "The current time is five thirty.",
    "The weather is sunny today.",
    "I've opened the application.",
    "I'm ready. What would you like to do?",
]


def _stats(samples):
    return {
        "n": len(samples),
        "min": min(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "max": max(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--md", default=None)
    args = parser.parse_args()

    from zoe import tts
    from zoe.config import KOKORO_SPEED, KOKORO_VOICE, TTS_ENGINE

    print(f"engine: kokoro (configured: {TTS_ENGINE}, voice: {KOKORO_VOICE}, "
          f"speed: {KOKORO_SPEED}, rate: {tts.KOKORO_RATE})")
    print(f"python: {sys.version.split()[0]}")

    results = {}
    cold_ms, cold_dur = None, None
    for phrase in PHRASES:
        gens, rtf = [], []
        for rep in range(args.reps + 1):  # rep 0 == cold
            start = time.perf_counter_ns()
            audio = tts.synthesize(phrase)
            ms = (time.perf_counter_ns() - start) / 1_000_000.0
            dur_s = len(audio) / tts.KOKORO_RATE
            if rep == 0 and cold_ms is None and phrase == PHRASES[0]:
                cold_ms, cold_dur = ms, dur_s
            else:
                gens.append(ms)
                rtf.append(ms / 1000.0 / dur_s)
        s, r = _stats(gens), _stats(rtf)
        results[phrase] = (s, r, dur_s)
        print(f"{phrase[:34]:34s} n={args.reps} min={s['min']:.0f} "
              f"median={s['median']:.0f} mean={s['mean']:.0f} max={s['max']:.0f} "
              f"stdev={s['stdev']:.0f} ms | audio={dur_s:.2f}s "
              f"rtf med={r['median']:.2f}")

    print(f"cold first render: {cold_ms:.0f} ms (audio {cold_dur:.2f}s)")
    if args.md:
        rows = "\n".join(
            f"| {p[:40]} | {s['min']:.0f} | {s['median']:.0f} | {s['mean']:.0f} | "
            f"{s['max']:.0f} | {d:.2f} | {r['median']:.2f} |"
            for p, (s, r, d) in results.items())
        Path(args.md).write_text(
            "# TTS Latency Benchmark (Kokoro)\n\n"
            f"- Cold first render: {cold_ms:.0f} ms (audio {cold_dur:.2f}s)\n\n"
            "| Phrase | Min ms | Median ms | Mean ms | Max ms | Audio s | RTF med |\n"
            "|---|---:|---:|---:|---:|---:|---:|\n" + rows + "\n",
            encoding="utf-8")
        print(f"wrote {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
