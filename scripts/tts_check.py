#!/usr/bin/env python3
"""Render a test phrase with Kokoro, report timing and pauses, write a WAV, and play it."""
import sys
import time
from pathlib import Path
import numpy as np
import soundfile as sf
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zoe import tts  # noqa: E402
from zoe.speech_pipeline import _sounddevice_play_array  # noqa: E402
PHRASE = "Hello there... how are you doing today? Good. Let us get started."


def play(audio, rate):
    """Play audio through the project's own output path. Returns True if it finished.

    Uses `speech_pipeline._sounddevice_play_array`, the same call `zoe.speech`
    makes, so the speakers get the device selection, logging and blocking
    behaviour the rest of the app gets. That call is synchronous: it returns only
    after the stream's finished callback fires, so nothing has to wait here.
    """
    print(f"playback start: {len(audio) / rate:.2f}s of audio", flush=True)
    try:
        result = _sounddevice_play_array(audio, rate)
    except Exception as e:
        print(f"playback FAILED: {e}")
        return False
    if result != "completed":
        print(f"playback FAILED: {result}")
        return False
    print("playback complete")
    return True


def main():
    t0 = time.perf_counter()
    audio = tts.synthesize(PHRASE)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    audio = tts.synthesize(PHRASE)
    warm = time.perf_counter() - t0
    seconds = len(audio) / tts.KOKORO_RATE
    print(f"cold start {cold:.2f}s, warm {warm:.2f}s, audio {seconds:.2f}s, real-time factor {warm / seconds:.2f}")
    quiet = np.abs(audio) < 1e-3
    pauses, run = [], 0
    for is_quiet in quiet:
        if is_quiet:
            run += 1
        else:
            if run / tts.KOKORO_RATE > 0.2:
                pauses.append(round(run / tts.KOKORO_RATE, 2))
            run = 0
    print("pauses longer than 0.2s (seconds):", pauses)
    out = ROOT / "data" / "kokoro_check.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), audio, tts.KOKORO_RATE)
    print(f"wrote {out}")
    return 0 if play(audio, tts.KOKORO_RATE) else 1


if __name__ == "__main__":
    raise SystemExit(main())
