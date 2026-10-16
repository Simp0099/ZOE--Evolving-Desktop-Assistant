"""Measure the echo guard, per room: false barge-ins, and missed interruptions.

The numbers this prints are the only evidence there is for the guard's decisions.
A threshold nobody measured is a guess wearing a constant's clothes, so every
figure here comes from a room that is generated, seeded and repeatable rather than
from one that was hoped about.

One run is a whole simulated turn through the composition `VoiceLoop._on_frame`
actually uses:

    pcm  = EchoCanceller.cancel(raw)
    if not EchoGuard.observe(raw, canceller.last_reference):
        pcm  = silence
    event = VoiceActivityDetector.push(pcm, barge_in=True)

`raw` is the playback as a room delivers it to a microphone: a seeded impulse
response with the room's RT60, the device's output latency, the speaker-to-mic
gain, ambient noise, and on the loudest room a hard clip.

Two runs per room and per method:

* **echo only** -- the user is silent. Every `start` event is the assistant
  interrupting itself. Anything above zero is a self-interruption.
* **with a user** -- the user starts talking 1.0 s in, louder than the assistant,
  from an unrelated signal. The first `start` is the interruption; `None` is a
  missed one.

Run:

    python scripts/measure_echo_guard.py            # all rooms, both methods
    python scripts/measure_echo_guard.py --room reverberant --verbose

Nothing here touches an audio device, so none of it is a statement about the
room this machine is in. TODO: the dedicated hardware validation write-up was
never written; docs/archive/PHASE_E_MANUAL_CHECKLIST.md carries the manual
procedure that has to answer that question.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zoe import audio as audio_mod  # noqa: E402
from zoe.audio import EchoCanceller, EchoGuard, VoiceActivityDetector, rms
from zoe.tts import KOKORO_RATE

RATE = audio_mod.SAMPLE_RATE            # capture rate
FRAME_MS = int(audio_mod.AUDIO_FRAME_MS)
FRAME = int(RATE * FRAME_MS / 1000)
CHUNK_MS = 220                          # TTS_CHUNK_MS
CHUNK = int(KOKORO_RATE * CHUNK_MS / 1000)

#: Playback level, measured: data/diag_kokoro_full.wav is RMS 0.0443.
PLAYBACK_RMS = 0.0443

#: When the user starts talking. Long enough that the reply is well established
#: and the guard has measured the room, so this is not a warm-up test.
USER_FROM_S = 1.0
#: The user is louder than the assistant, as in every measured live interruption.
USER_GAIN = 3.0


@dataclass(frozen=True)
class Room:
    """One speaker-to-microphone path."""

    name: str
    #: RT60 of the room. 0.15 is a small carpeted room, 0.45 a hard one.
    rt60_s: float
    #: Speaker-to-microphone gain. UNMEASURED for this machine; see
    #: docs/barge_in_diagnosis.md section 5. Headphones are the one room where
    #: the number is known, because there is no path at all.
    spk_to_mic: float
    #: Ambient noise RMS at the microphone.
    ambient: float
    #: Output latency, ms.
    latency_ms: float = 40.0
    #: Hard clip the microphone signal at this level, as a hot speaker does.
    clip_at: Optional[float] = None


#: The four conditions the brief asks for: no acoustic path, a quiet room, a
#: reverberant one, and a loud distorting one.
ROOMS = (
    Room("headphones", 0.15, 0.0, 0.008, latency_ms=20),
    Room("quiet_desk", 0.15, 0.08, 0.008),
    Room("desk", 0.20, 0.35, 0.012),
    Room("reverberant", 0.35, 0.60, 0.020, latency_ms=80),
    Room("loud_noisy", 0.30, 1.20, 0.050, latency_ms=60, clip_at=0.35),
)


class _FakeClock:
    """Deterministic clock; a frame is handed to the canceller when delivered."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def pcm(samples) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def room_ir(rt60_s: float, seed: int = 7, rate: int = KOKORO_RATE) -> np.ndarray:
    """A seeded exponentially-decaying room impulse response.

    One early reflection plus a noise tail whose decay follows
    ``10 ** (-3 t / RT60)``, which is what RT60 means: -60 dB at `rt60_s`.
    """
    n = max(4, int(rate * rt60_s * 2))
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    tail = rng.normal(0.0, 1.0, n) * (10.0 ** (-3.0 * t / rt60_s))
    ir = np.zeros(n, dtype=np.float64)
    ir[0] = 1.0
    ir[min(n - 1, int(rate * 0.011))] = 0.35
    ir[min(n - 1, int(rate * 0.037))] = 0.18
    ir += tail * 0.5
    return ir / np.abs(ir).max()


def _shape(n: int, rate: int, rng) -> np.ndarray:
    """A smooth random spectral envelope, drawn as a handful of control points.

    Two voices have to differ in *timbre*, not only in their noise seed. A
    least-squares fit removes whatever is proportional to our playback, so a user
    built from the same noise generator as the TTS output is explained away by it
    almost completely -- which measures the harness being kind to the guard rather
    than the guard working. A different envelope per voice is what makes the test
    about separating two voices.
    """
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    control = rng.uniform(-6.0, 6.0, 7)
    points = np.logspace(np.log10(200), np.log10(min(5000, rate / 2)), 7)
    env = np.interp(freqs, points, control, left=control[0], right=control[-1])
    return 10.0 ** (env / 20.0)


def speechlike(seconds: float, rate: int = KOKORO_RATE, seed: int = 5) -> np.ndarray:
    """Amplitude-modulated noise at a ~3 Hz syllable rate, at PLAYBACK_RMS."""
    rng = np.random.default_rng(seed)
    n = max(1, int(rate * seconds))
    t = np.arange(n) / rate
    envelope = np.clip(np.sin(2 * np.pi * 3.0 * t), 0.0, 1.0) ** 2
    sig = rng.normal(0.0, 1.0, n) * (0.15 + 0.85 * envelope)
    spec = np.fft.rfft(sig) * _shape(n, rate, np.random.default_rng(seed + 500))
    sig = np.fft.irfft(spec, n).astype(np.float64)
    return (sig * (PLAYBACK_RMS / np.sqrt((sig * sig).mean()))).astype(np.float32)


def _to_mic_rate(played, rate: int = RATE) -> np.ndarray:
    g = np.gcd(KOKORO_RATE, rate)
    return resample_poly(played, rate // g, KOKORO_RATE // g).astype(np.float64)


def room_signal(played, room: Room, seed: int = 11) -> np.ndarray:
    """Playback as the microphone hears it: room IR, device latency, speaker leak."""
    at_mic = _to_mic_rate(played)
    g = np.gcd(KOKORO_RATE, RATE)
    ir_mic = resample_poly(room_ir(room.rt60_s), RATE // g, KOKORO_RATE // g)
    heard = np.convolve(at_mic, ir_mic)[: at_mic.size]
    lag = int(round(room.latency_ms * RATE / 1000.0))
    leaked = np.zeros_like(heard)
    if lag < heard.size:
        leaked[lag:] = heard[:-lag or None]
    leaked *= room.spk_to_mic
    if room.clip_at is not None:
        leaked = np.clip(leaked, -room.clip_at, room.clip_at)
    return leaked


@dataclass
class Outcome:
    """What one simulated turn did."""

    cut_at: Optional[int] = None          # frame index of the first barge-in
    cuts: int = 0                        # every barge-in, not just the first
    blocked_frames: int = 0              # frames the guard replaced with silence
    user_frames: int = 0                 # frames that carried the user's speech
    user_blocked: int = 0                # ... of those, how many were replaced
    ratio_floor: float = 0.0
    ratio_bar: float = 0.0
    calibrated: bool = False
    reliable: bool = True
    required_frames: int = 0

    @property
    def cut_at_s(self) -> Optional[float]:
        return None if self.cut_at is None else round(self.cut_at * FRAME_MS / 1000.0, 3)

    @property
    def muted_user(self) -> float:
        """Share of the user's own frames the guard replaced with silence.

        Near 1.0 is the failure mode this feature must not introduce: the
        microphone working, the assistant deaf to it.
        """
        return 0.0 if not self.user_frames else self.user_blocked / self.user_frames


def run(room: Room, *, adaptive_bar: bool = True, user: bool = False,
        spoken_s: float = 4.0, frames: int = 100, seed: int = 3) -> Outcome:
    """One turn through cancel -> guard -> VAD. `user` mixes a speaker in at 1.0 s."""
    clock = _FakeClock()
    rng = np.random.default_rng(seed)
    canceller = EchoCanceller(clock=clock, playback_rate=KOKORO_RATE)
    guard = EchoGuard(clock=clock, adaptive_bar=adaptive_bar)
    vad = VoiceActivityDetector(clock=clock)

    played = speechlike(spoken_s)
    echo = room_signal(played, room)
    voice = speechlike(spoken_s, seed=101) * USER_GAIN if user else None
    user_from = int(USER_FROM_S * RATE)
    n_chunks = int(np.ceil(spoken_s * 1000 / CHUNK_MS))
    chunks = [pcm(played[i * CHUNK:(i + 1) * CHUNK]) for i in range(n_chunks)]

    out = Outcome()
    # The VAD calibrates on the room alone first, exactly as the loop does after
    # start-up. Its floor is what the barge-in bar is measured against, so a run
    # that skipped this would measure the wrong thing.
    for i in range(20):
        clock.advance(FRAME_MS / 1000.0)
        vad.push(pcm(rng.normal(0.0, room.ambient, FRAME)))

    for index in range(frames):
        now = index * FRAME_MS / 1000.0
        for k, data in enumerate(chunks):          # the player's on_play hook
            if k * CHUNK_MS / 1000.0 <= now < (k + 1) * CHUNK_MS / 1000.0:
                canceller.play_reference(data)
                break
        start = int(round(now * RATE))
        raw = echo[start:start + FRAME]
        if raw.size < FRAME:
            break
        speaking = voice is not None and start >= user_from
        if speaking:
            seg = voice[start - user_from: start - user_from + raw.size]
            raw = raw + seg[: raw.size]
        raw = raw + rng.normal(0.0, room.ambient, FRAME)
        raw_pcm = pcm(raw)

        clock.advance(FRAME_MS / 1000.0)
        frame = canceller.cancel(raw_pcm)
        is_user = guard.observe(raw_pcm, getattr(canceller, "last_reference", None))
        if not is_user:
            frame = bytes(len(frame))
            out.blocked_frames += 1
            if speaking:
                out.user_blocked += 1
        if speaking:
            out.user_frames += 1
        event = vad.push(frame, barge_in=True)
        if event is not None and event.kind == "start":
            out.cuts += 1
            if out.cut_at is None:
                out.cut_at = index

    out.ratio_floor = guard.ratio_floor
    out.ratio_bar = guard.ratio_bar
    out.calibrated = guard.calibrated
    out.reliable = guard.reliable
    out.required_frames = guard.required_frames
    return out


def measure(room: Room, **kw) -> Dict[str, Any]:
    """Both runs for one room and method, reduced to the two questions asked."""
    silent = run(room, user=False, **kw)
    talking = run(room, user=True, **kw)
    return {
        "room": room.name,
        "false_bargeins": silent.cuts,
        "missed": talking.cut_at is None,
        "cut_at_s": talking.cut_at_s,
        # An interruption that lands in the first 300 ms has cut the reply off
        # before anything meaningful was played -- a cost, not an interruption.
        "premature": talking.cut_at is not None and talking.cut_at * FRAME_MS / 1000.0 < 0.3,
        "muted_user": round(talking.muted_user, 3),
        "ratio_floor": round(talking.ratio_floor, 3),
        "ratio_bar": round(talking.ratio_bar, 3),
        "reliable": talking.reliable,
    }


def table(rows) -> str:
    head = ("room", "method", "false", "missed", "cut_s", "premature",
            "muted", "floor", "bar", "reliable")
    widths = [max(len(head[i]), *(len(str(r[i])) for r in rows)) for i in range(len(head))]
    line = "  ".join(h.ljust(w) for h, w in zip(head, widths))
    body = "\n".join("  ".join(str(v).ljust(w) for v, w in zip(r, widths)) for r in rows)
    return f"{line}\n{body}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--room", action="append", default=None,
                        help="only this room (repeatable)")
    parser.add_argument("--frames", type=int, default=100, help="80 ms frames to simulate")
    args = parser.parse_args(argv)

    rooms = [r for r in ROOMS if args.room is None or r.name in args.room]
    if not rooms:
        parser.error(f"no such room; known: {', '.join(r.name for r in ROOMS)}")

    rows = []
    for room in rooms:
        for adaptive in (False, True):
            r = measure(room, adaptive_bar=adaptive, frames=args.frames)
            rows.append((r["room"], "fixed" if not adaptive else "adaptive",
                         r["false_bargeins"], "yes" if r["missed"] else "no",
                         r["cut_at_s"], r["premature"], r["muted_user"],
                         r["ratio_floor"], r["ratio_bar"], r["reliable"]))
    print(table(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by tests instead
    raise SystemExit(main())
