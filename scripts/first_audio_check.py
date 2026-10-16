#!/usr/bin/env python3
"""Measure end-of-speech to first-audio latency without a microphone.

The specification asks for "time from end of speech to first audio", with the
method "time between [YOU] print and first audio write". `VoiceLoop` already
stamps both ends of that interval on the conversation machine:

    speech_end   marked when the turn begins (the VAD has released the floor)
    first_audio  marked once audio is queued and playback is about to begin

so the figure is a subtraction of two marks the production code wrote itself,
not a stopwatch bolted on for the test.

How this script gets a turn without hardware:

* Audio is synthesised with the project's own engine, resampled to the capture
  rate and pushed through the real `_on_frame` -> `vad.push` path. The VAD
  calibrates on room tone first, exactly as it does in a real room.
* Transcription is the real local `Transcriber` (faster-whisper).
* Synthesis is the real Kokoro engine through the default `SpeechPlayer`
  synthesiser.
* Playback is written to a no-op sink, so nothing reaches the speakers. The
  `first_audio` mark is placed before the player starts, so the number is
  unaffected.
* The reply text is scripted. **The model round trip is therefore NOT included**
  -- see the caveat printed at the end. With a real provider, add its latency.

Run:
    .venv/bin/python scripts/first_audio_check.py [--reps 3]
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from zoe.audio import MicrophoneStream  # noqa: E402
from zoe.speech_pipeline import SpeechPlayer, Transcriber  # noqa: E402
from zoe.voice_loop import VoiceLoop  # noqa: E402

RATE = 16000           # capture rate the loop expects
FRAME = 1280           # 80 ms, the shared frame size
PHRASE = "Please tell me what time it is right now in the evening."
REPLY = "It is twelve o'clock."


class _Sink:
    """A microphone that replays a prepared list of frames.

    When the list is exhausted it *blocks* for one frame period, the way a real
    PortAudio `read()` does. Returning `None` immediately instead would make
    `MicrophoneStream._pump` spin on its `if pcm is None: continue` branch and
    steal CPU from the synthesis thread, inflating this measurement by ~30x.
    """

    is_open = False

    def __init__(self, frames):
        self.frames = list(frames)

    def open(self):
        self.is_open = True

    def read(self):
        if not self.frames:
            time.sleep(FRAME / RATE)
            return None
        return self.frames.pop(0)

    def close(self):
        self.is_open = False


def _speech_frames(seconds):
    """Real synthesised speech at the capture rate, as int16 bytes."""
    from zoe import tts

    audio = tts.synthesize(PHRASE)
    n = int(len(audio) * RATE / tts.KOKORO_RATE)
    idx = np.linspace(0, len(audio) - 1, n)
    mono = np.interp(idx, np.arange(len(audio)), audio)
    pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
    return [pcm[i:i + FRAME].tobytes() for i in range(0, len(pcm) - FRAME + 1, FRAME)]


def _room_frames(count, seed=3):
    """Steady low-level room tone so the VAD has a noise floor to calibrate on.

    Deliberately well under `vad.energy_threshold` (0.01 RMS). Anything at or
    above it is itself speech and the detector correctly fires on it, which
    would split the utterance in two and transcribe two fragments.
    """
    rng = np.random.default_rng(seed)
    tone = (rng.standard_normal(count * FRAME) * 0.0015).astype(np.float32)
    tone = np.clip(tone, -1, 1)
    pcm = (tone * 32767).astype(np.int16)
    return [pcm[i * FRAME:(i + 1) * FRAME].tobytes() for i in range(count)]


def _run_once():
    from zoe.conversation import State

    # ~1.3 s of room tone: enough for the detector's 16-frame calibration.
    frames = _room_frames(20) + _speech_frames(3.0) + _room_frames(40)

    loop = VoiceLoop(
        microphone=MicrophoneStream(source=_Sink(frames)),
        player=SpeechPlayer(
            play=lambda d, r: None,      # nothing reaches the speakers
            stop_playback=lambda: None,
            chunk_ms=100,
        ),
        transcriber=Transcriber(),      # real local faster-whisper
        respond=lambda t: REPLY,        # scripted: no model round trip
        wake_enabled=False,             # no wake word needed for a turn
        follow_up_window=0.0,
    )

    loop.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            marks = loop.machine.snapshot()["marks"]
            if "first_audio" in marks and "speech_end" in marks:
                break
            time.sleep(0.01)
        marks = loop.machine.snapshot()["marks"]
    finally:
        try:
            loop.stop()
        except RuntimeError:
            pass

    if "speech_end" not in marks or "first_audio" not in marks:
        return None, marks
    return marks["first_audio"] - marks["speech_end"], marks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()

    print(f"python: {sys.version.split()[0]}")
    samples, last = [], None
    for i in range(args.reps):
        value, marks = _run_once()
        if value is None:
            print(f"rep {i + 1}: NO TURN PRODUCED; marks={sorted(marks)}")
            return 1
        samples.append(value)
        print(f"rep {i + 1}: speech_end -> first_audio = {value:.3f} s  "
              f"(marks: {', '.join(f'{k}={v:.3f}' for k, v in sorted(marks.items()))})")

    warm = samples[1:] or samples
    print()
    print(f"cold first run: {samples[0]:.3f} s")
    print(f"warm n={len(warm)}  min={min(warm):.3f}  median={statistics.median(warm):.3f}  "
          f"max={max(warm):.3f} s")
    print()
    print("SCOPE: transcription is the real local engine and synthesis is the real")
    print("Kokoro engine, but the reply is scripted, so this EXCLUDES the model")
    print("provider round trip. Add that latency to compare against the 2.0 s goal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
