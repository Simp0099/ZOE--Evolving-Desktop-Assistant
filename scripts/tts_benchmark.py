"""Kokoro TTS performance baseline (measurement only).

Exercises the REAL Zoe TTS path -- same pipeline loader, same voice, same
speed and punctuation pauses as `zoe.tts.synthesize` -- and reports
cold/warm latency, audio duration and real-time factor. No optimization, no
network, no providers, no tools, no playback, no disk I/O in the hot loop.

Run with the project environment (Kokoro lives there, not in system python):
    .venv/bin/python scripts/tts_benchmark.py [--reps 5] [--md docs/history/tts_benchmark_<date>.md]

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

PHRASES = {
    # id: (label, text) -- P4 deliberately repeats P2 verbatim.
    "P1": ("Very short (cold)", "Yes, Boss?"),
    "P2": ("Short (~10 words)",
           "Boss, your system is ready and waiting for your next command."),
    "P3": ("Medium (~30 words)",
           "Boss, I have checked your schedule, the weather in Delhi, and "
           "your latest messages. Everything looks completely normal, and "
           "there is nothing urgent that needs your attention right now."),
    "P4": ("Repeated short (== P2)",
           "Boss, your system is ready and waiting for your next command."),
    "P5": ("Representative response",
           "The current time is nine twenty PM. Your meeting tomorrow is "
           "at ten, and traffic looks light."),
}


def _ms(nanoseconds: int) -> float:
    return nanoseconds / 1_000_000.0


def _stats(samples):
    return {
        "n": len(samples),
        "min": min(samples),
        "max": max(samples),
        "mean": statistics.fmean(samples),
        "median": statistics.median(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=5,
                        help="warm repetitions per phrase (default: 5)")
    parser.add_argument("--md", default=None,
                        help="write measured tables to this markdown file")
    args = parser.parse_args()

    import numpy as np
    from zoe import tts
    from zoe.config import (KOKORO_LANG, KOKORO_SPEED, KOKORO_VOICE,
                               TTS_ENGINE)

    try:
        import torch
        torch_version, mps = torch.__version__, torch.backends.mps.is_available()
    except ImportError:  # pragma: no cover -- torch is a Kokoro dependency
        torch_version, mps = "not installed", False

    header = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "python": sys.version.split()[0],
        "torch": torch_version,
        "mps_available": mps,
        "engine": TTS_ENGINE,
        "voice": KOKORO_VOICE,
        "lang": KOKORO_LANG,
        "speed": KOKORO_SPEED,
        "sample_rate": tts.KOKORO_RATE,
    }
    print("TTS benchmark header:")
    for key, value in header.items():
        print(f"  {key}: {value}")

    # -- model initialization (singleton, as in production) ------------------
    start = time.perf_counter_ns()
    tts._get_pipeline()
    load_ms = _ms(time.perf_counter_ns() - start)
    print(f"model load: {load_ms:.0f} ms")

    def _generate(text: str):
        """One timed generation; returns (generate_ms, convert_ms, samples)."""
        start = time.perf_counter_ns()
        wav = tts.synthesize(text)
        gen_ms = _ms(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        array = np.asarray(wav, dtype="float32").reshape(-1)
        conv_ms = _ms(time.perf_counter_ns() - start)
        assert array.size > 0 and abs(array).max() > 0, "silent/empty output"
        return gen_ms, conv_ms, int(array.size)

    # -- cold: first generation after init -----------------------------------
    label, text = PHRASES["P1"]
    cold_gen_ms, cold_conv_ms, cold_samples = _generate(text)
    cold_dur = cold_samples / tts.KOKORO_RATE
    print(f"cold P1 {label!r}: generate={cold_gen_ms:.0f} ms "
          f"convert={cold_conv_ms:.1f} ms samples={cold_samples} "
          f"rtf={cold_gen_ms / 1000.0 / cold_dur:.2f}")

    # -- warm-up (discarded; one controlled pass like production) -------------
    start = time.perf_counter_ns()
    tts.warm_up()
    warm_ms = _ms(time.perf_counter_ns() - start)
    print(f"warm-up (discarded): {warm_ms:.0f} ms")

    # -- warm repetitions -----------------------------------------------------
    results = {}
    for pid in ("P2", "P3", "P4", "P5"):
        label, text = PHRASES[pid]
        gens, convs = [], []
        for _ in range(args.reps):
            gen_ms, conv_ms, samples = _generate(text)
            gens.append(gen_ms)
            convs.append(conv_ms)
        results[pid] = (_stats(gens), _stats(convs), samples / tts.KOKORO_RATE)
        s = results[pid][0]
        print(f"warm {pid} {label!r} x{args.reps}: min={s['min']:.0f} "
              f"median={s['median']:.0f} mean={s['mean']:.0f} "
              f"max={s['max']:.0f} stdev={s['stdev']:.0f} ms "
              f"audio={results[pid][2]:.2f}s "
              f"rtf_med={s['median'] / 1000.0 / results[pid][2]:.2f}")

    if args.md:
        _write_md(args.md, header, load_ms, model_device=None,
                  cold=(cold_gen_ms, cold_conv_ms, cold_samples, cold_dur),
                  warmup=(warm_ms,), results=results)
        print(f"wrote {args.md}")
    return 0


def _write_md(path, header, load_ms, model_device, cold, warmup, results):
    pid_rows = "\n".join(
        f"| {pid} {PHRASES[pid][0]} | {PHRASES[pid][1]} |" for pid in results)
    warm_rows = "\n".join(
        f"| {pid} | {g['min']:.0f} | {g['median']:.0f} | {g['mean']:.0f} | "
        f"{g['max']:.0f} | {g['stdev']:.0f} | {dur:.2f} | "
        f"{g['median'] / 1000.0 / dur:.2f} |"
        for pid, (g, _, dur) in results.items())
    Path(path).write_text(f"""# TTS Baseline

## Environment

- Date: {header['timestamp']}
- Python: {header['python']}
- PyTorch: {header['torch']} (MPS available: {header['mps_available']})
- Engine: {header['engine']} (Kokoro)
- Voice: {header['voice']} (lang {header['lang']})
- Speed: {header['speed']}
- Sample rate: {header['sample_rate']} Hz

## Configuration

- Kokoro takes no `exaggeration`/`cfg_weight`/`temperature` generation kwargs;
  expression comes from pauses, speed and voice choice. `tts.synthesize` accepts
  an optional `exaggeration` hint that maps onto speed.
- No reference audio: Kokoro uses built-in voices, so there is nothing to resolve
  or prepare.

## Initialization

- Model load: {load_ms:.0f} ms
- Warm-up (discarded): {warmup[0]:.0f} ms

## Cold Generation

- Phrase: "Yes, Boss?"
- Generation: {cold[0]:.0f} ms
- Array conversion: {cold[1]:.1f} ms
- Samples: {cold[2]} ({cold[3]:.2f} s)
- Cold total (load + warm-up + generation): {load_ms + warmup[0] + cold[0]:.0f} ms

## Warm Generation (reps per phrase)

| Phrase | Text |
|---|---|
{pid_rows}

| Phrase | Min ms | Median ms | Mean ms | Max ms | Stdev ms | Audio s | RTF med |
|---|---:|---:|---:|---:|---:|---:|---:|
{warm_rows}

## Observations

<!-- Added by hand after reviewing the numbers. -->

## Bottleneck Assessment

<!-- Added by hand after reviewing the numbers. -->

## Optimization Decision

<!-- Added by hand after reviewing the numbers. -->
""", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
