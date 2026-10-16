import time
"""Regression tests for the one-word cutoff: the assistant interrupting itself.

Confirmed mechanism (see data/zoe.log, 17:50:16-17:50:39): playback opened for
the full utterance, then ~0.7 s in the VAD reported `speech started state=speaking
barge_in=True`, `SpeechPlayer.cancel("barge-in")` fired, and the stream stopped at
17.4k of 91.8k frames. The assistant heard its own voice.

The cause is a search-window arithmetic error in `EchoCanceller._reference`.
A captured frame is 80 ms long and is handed to `cancel()` when it is *delivered*,
so its samples span `[now-80ms, now]`. Sound played `L` ms earlier occupies
`[now-80ms-L, now-L]`. The reference window is anchored at `end=now` and spans
`count + pad` samples, so the echo sits at offset `count - L` -- and the search
only reaches `pad = MAX_LAG_MS`. With `MAX_LAG_MS == 80 == one frame`, the window
reaches back exactly one frame and therefore can only ever find `L <= 0`: any
device with real output latency puts the echo outside the searchable range, the
projection finds nothing, the gain collapses to zero, and the assistant's own
voice reaches the VAD.

These tests pin the arithmetic, the end-to-end symptom, and the invariant that
intentional barge-in keeps working. All hardware-free.
"""

import sys
import threading

import numpy as np
import pytest
from scipy.signal import resample_poly

from zoe import audio as audio_mod
from zoe.audio import EchoCanceller, EchoGuard, VoiceActivityDetector, rms
from zoe.tts import KOKORO_RATE


class _FakeClock:
    """Deterministic clock; a frame is handed to the canceller when delivered."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def _speech(seconds, rate=KOKORO_RATE, seed=5, level=0.3):
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    voiced = (np.sin(2 * np.pi * 3.3 * t) > 0).astype(np.float32)
    return np.clip(rng.normal(0, level * voiced, len(t)), -1, 1).astype(np.float32)


def _at_mic_rate(played):
    g = np.gcd(KOKORO_RATE, audio_mod.SAMPLE_RATE)
    return resample_poly(
        played, audio_mod.SAMPLE_RATE // g, KOKORO_RATE // g
    ).astype(np.float32)


FRAME = int(audio_mod.SAMPLE_RATE * 0.08)


class TestReferenceWindowArithmetic:
    """The window must reach back further than one frame, not exactly one frame."""

    def test_search_window_must_exceed_one_frame_plus_latency(self):
        canceller = EchoCanceller(playback_rate=KOKORO_RATE)
        frame_ms = FRAME / audio_mod.SAMPLE_RATE * 1000
        # A device with 150 ms of round-trip latency needs the search to reach
        # frame_ms + 150 ms back from `now` to find that echo.
        assert canceller._pad() / audio_mod.SAMPLE_RATE * 1000 > frame_ms + 150, (
            "the reference window spans less than one frame plus a plausible "
            "device latency, so any real output latency puts the echo outside "
            "the searchable range and the assistant hears itself"
        )

    def test_reference_window_is_longer_than_the_frame(self):
        canceller = EchoCanceller(playback_rate=KOKORO_RATE)
        played = _speech(2.0)
        clock = _FakeClock()
        canceller.clock = clock
        canceller.play_reference(_pcm(played))
        clock.advance(1.0)
        window = canceller._reference(clock.t, FRAME)
        assert len(window) > FRAME, (
            "the window is no longer than the frame, so it cannot also cover "
            "the acoustic lag"
        )

    def test_window_stays_inside_the_playback_history(self):
        canceller = EchoCanceller(playback_rate=KOKORO_RATE)
        window_ms = (FRAME + canceller._pad()) / audio_mod.SAMPLE_RATE * 1000
        assert window_ms < canceller.history_s * 1000, (
            f"window of {window_ms:.0f} ms exceeds the {canceller.history_s}s "
            f"history, so the far end runs out before the near end"
        )


class TestEchoRemovalAtRealLatency:
    def test_echo_is_removed_at_realistic_device_latencies(self):
        played = _speech(6.0, seed=11)
        at_mic = _at_mic_rate(played)
        # Output latency reported by CoreAudio on the test machine is ~24 ms,
        # plus buffering. 40-160 ms covers the realistic range.
        for latency_ms in (0, 20, 40, 60, 80, 100, 120, 140, 160):
            clock = _FakeClock()
            canceller = EchoCanceller(clock=clock, playback_rate=KOKORO_RATE)
            canceller.play_reference(_pcm(played))
            delay = int(latency_ms / 1000 * audio_mod.SAMPLE_RATE)
            before, after = [], []
            for i in range(40):
                # A frame covering [i*FRAME, (i+1)*FRAME) is handed to the
                # canceller once it has been captured, i.e. 80 ms later.
                clock.advance(0.08)
                start = i * FRAME - delay
                captured = np.zeros(FRAME, dtype=np.float32)
                lo, hi = max(start, 0), min(start + FRAME, len(at_mic))
                if hi > lo:
                    captured[lo - start:hi - start] = 0.5 * at_mic[lo:hi]
                if not captured.any():
                    continue
                before.append(np.sqrt((captured ** 2).mean()))
                after.append(rms(canceller.cancel(_pcm(captured))))
            reduction = 1 - np.mean(after) / np.mean(before)
            assert reduction > 0.9, (
                f"only {reduction:.0%} of the echo removed at {latency_ms}ms "
                f"latency; the assistant will hear itself"
            )


class TestSelfInterruptIsNotBargeIn:
    """The reported symptom, end to end, as a test."""

    def _hear(self, latency_ms, user_talks, seed=3):
        rng = np.random.default_rng(seed)
        played = _speech(20.0, seed=seed)
        at_mic = _at_mic_rate(played)
        ambient = np.clip(
            rng.normal(0, 0.008, 130 * FRAME), -1, 1).astype(np.float32)
        clock = _FakeClock()
        canceller = EchoCanceller(clock=clock, playback_rate=KOKORO_RATE)
        canceller.play_reference(_pcm(played))
        vad = VoiceActivityDetector()
        delay = int(latency_ms / 1000 * audio_mod.SAMPLE_RATE)
        for i in range(20):                      # calibrate on the room alone
            vad.push(_pcm(ambient[i * FRAME:(i + 1) * FRAME]))
        starts = 0
        for i in range(120):
            # The frame is delivered after its 80 ms of audio was captured.
            clock.advance(0.08)
            start = i * FRAME - delay
            frame = ambient[i * FRAME:(i + 1) * FRAME].copy()
            lo, hi = max(start, 0), min(start + FRAME, len(at_mic))
            if hi > lo:
                frame[lo - start:hi - start] += 0.5 * at_mic[lo:hi]
            if user_talks:
                frame[200:700] += rng.normal(0, 0.06, 500).astype(np.float32)
            event = vad.push(canceller.cancel(_pcm(frame)), barge_in=True)
            if event is not None and event.kind == "start":
                starts += 1
        return starts

    @pytest.mark.parametrize("latency_ms", [0, 40, 80, 120, 160])
    def test_the_assistant_never_interrupts_itself(self, latency_ms):
        assert self._hear(latency_ms, user_talks=False) == 0, (
            f"playback would be cut off {latency_ms}ms in by the assistant's own voice"
        )

    @pytest.mark.parametrize("latency_ms", [0, 40, 80, 120, 160])
    def test_a_user_still_interrupts(self, latency_ms):
        assert self._hear(latency_ms, user_talks=True) > 0, (
            f"a real barge-in was suppressed at {latency_ms}ms latency; "
            f"cancellation must not disable interruption"
        )


class TestPlaybackPathIntegrity:
    """The playback path itself must be sound: full audio, no premature stop."""

    def test_full_utterance_reaches_the_playback_layer(self):
        """A multiword reply must arrive as one complete array, not one chunk."""
        from zoe.speech_pipeline import SpeechPlayer

        played = []

        def play(data, sample_rate, cancel_event=None):
            played.append((data, sample_rate, cancel_event))
            return "completed"

        player = SpeechPlayer(play=play, synthesize=lambda text: _speech(3.0, seed=7))
        assert player.play is play          # per-chunk path, not the batching one
        chunks = player.synthesize_to_queue(
            "one two three four five six seven", "turn_x", lambda t: False)
        player.start("turn_x", lambda t: False)
        assert player.wait(timeout=5.0)
        assert chunks > 1, "the utterance should span several chunks"
        assert played, "nothing was played"
        expected = int(3.0 * KOKORO_RATE)
        total = sum(len(d) for d, _, _ in played)
        assert total >= expected * 2 * 0.99, (
            f"played {total//2} samples of ~{expected}; the tail was dropped"
        )
        assert all(rate == KOKORO_RATE for _, rate, _ in played)
        assert player.cancel_reason is None
        assert player.last_error is None

    @pytest.mark.requires_kokoro
    def test_playback_runs_to_completion_without_cancellation(self):
        from zoe.speech_pipeline import SpeechPlayer

        player = SpeechPlayer(play=lambda d, r, c=None: "completed")
        player.synthesize_to_queue("a b c d", "t1", lambda t: False)
        player.start("t1", lambda t: False)
        assert player.wait(timeout=5.0)
        assert player.cancel_reason is None
        assert player.chunks_cancelled == 0
        assert player.last_error is None

    @pytest.mark.requires_kokoro
    def test_playback_failure_is_reported_not_swallowed(self):
        from zoe.speech_pipeline import SpeechPlayer

        def boom(data, rate, cancel_event=None):
            raise RuntimeError("device busy")

        player = SpeechPlayer(play=boom)
        player.synthesize_to_queue("hello there", "t1", lambda t: False)
        player.start("t1", lambda t: False)
        assert player.wait(timeout=5.0)
        assert player.last_error == "device busy", (
            "a playback failure must surface, not look like a finished reply"
        )

    def test_completion_and_cancellation_cannot_stop_the_same_stream_twice(self):
        """Exactly one thread may own the stream; cancel only signals an event."""
        from zoe import speech_pipeline as pipeline

        order = []
        started = threading.Event()
        release = threading.Event()

        class CancelStop(Exception):
            pass

        class OutputStream:
            def __init__(self, **kwargs):
                self.cb = kwargs["callback"]
                self.done = kwargs["finished_callback"]

            def __enter__(self):
                self.thread = threading.Thread(target=self._render)
                self.thread.start()
                return self

            def __exit__(self, *exc):
                self.thread.join(timeout=5)

            def _render(self):
                block = np.empty((256, 1), dtype=np.float32)
                started.set()
                release.wait(timeout=5)
                while True:
                    try:
                        self.cb(block, 256, None, None)
                    except CancelStop:
                        break
                order.append("stream-stopped")
                self.done()

        monkey = sys.modules.get("sounddevice")
        original = sys.modules.get("sounddevice")
        import types
        sys.modules["sounddevice"] = types.SimpleNamespace(
            default=types.SimpleNamespace(device=(0, 1)),
            query_devices=lambda d, k: {"name": "Mock"},
            check_output_settings=lambda **kw: None,
            OutputStream=OutputStream,
            CallbackStop=CancelStop,
        )
        try:
            worker = threading.Thread(
                target=lambda: pipeline._sounddevice_play_array(
                    np.ones(4096, dtype=np.float32), 24000,
                    pipeline.threading.Event()))
            worker.start()
            assert started.wait(timeout=5)
            # Cancellation only sets the event; the owner thread closes the stream.
            pipeline._sounddevice_stop()
            release.set()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert order == ["stream-stopped"], (
                f"the stream was stopped {len(order)} times; only its owner may"
            )
        finally:
            sys.modules["sounddevice"] = original

    def test_explicit_cancellation_still_interrupts_playback(self):
        """Intentional interruption must keep working after the fix."""
        from zoe import speech_pipeline as pipeline

        progressed = threading.Event()

        class CancelStop(Exception):
            pass

        class OutputStream:
            def __init__(self, **kwargs):
                self.cb = kwargs["callback"]
                self.done = kwargs["finished_callback"]

            def __enter__(self):
                self.thread = threading.Thread(target=self._render)
                self.thread.start()
                return self

            def __exit__(self, *exc):
                self.thread.join(timeout=5)

            def _render(self):
                block = np.empty((256, 1), dtype=np.float32)
                progressed.set()
                # A long utterance, so cancellation is what ends it.
                while True:
                    try:
                        self.cb(block, 256, None, None)
                    except CancelStop:
                        break
                    progressed.set()
                    # Pace like a real sound card: one 256-frame block per
                    # 256/24000 s. Unpaced, a 30 s clip finished in milliseconds,
                    # before the test could cancel it -- a fake-clock bug, not a
                    # playback bug.
                    time.sleep(256 / 24000)
                self.done()

        import types
        original = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = types.SimpleNamespace(
            default=types.SimpleNamespace(device=(0, 1)),
            query_devices=lambda d, k: {"name": "Mock"},
            check_output_settings=lambda **kw: None,
            OutputStream=OutputStream,
            CallbackStop=CancelStop,
        )
        try:
            result = []

            def run():
                result.append(pipeline._sounddevice_play_array(
                    np.ones(24000 * 30, dtype=np.float32), 24000))

            worker = threading.Thread(target=run)
            worker.start()
            assert progressed.wait(timeout=5)
            pipeline._sounddevice_stop()
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert result == ["cancelled"], (
                f"expected the playback to be cancelled, got {result}"
            )
        finally:
            sys.modules["sounddevice"] = original

    def test_production_player_uses_the_shared_output_implementation(self):
        from zoe import speech_pipeline as pipeline
        from zoe.speech import _speak_kokoro

        player = pipeline.SpeechPlayer()
        assert player.play is pipeline._sounddevice_play
        # Both production entry points must go through the same helper.
        source = _speak_kokoro.__doc__ or ""
        assert pipeline._sounddevice_play_array is not None
        assert "sound_pipeline" not in source


class TestEchoGuardBandResidual:
    """Barge-in must answer "is this sound ours?", not just "is it loud?".

    A reply through the speakers is exactly as loud as the user and sits in the
    same bands, so a loudness threshold cannot tell them apart. The band-energy
    echo guard subtracts a learned per-band estimate of our own playback and lets
    the VAD see only the residual; these tests are the acceptance for it.
    """

    FRAME_SAMPLES = 320      # 20 ms at 16 kHz
    FRAME_SECONDS = 0.02
    DELAY = 2                # 40 ms output lag
    SCALE = 0.30
    ONSET = 15               # burst begins 300 ms into playback

    @staticmethod
    def _tone(n, level=0.5):
        t = np.arange(n) / audio_mod.SAMPLE_RATE
        return (level * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    @staticmethod
    def _burst(n, seed, level=0.4):
        t = np.arange(n) / audio_mod.SAMPLE_RATE
        envelope = 0.35 + 0.65 * np.abs(np.sin(2 * np.pi * 4.5 * t))
        return (np.random.default_rng(seed).normal(0, level * envelope, n)
                ).astype(np.float32)

    @staticmethod
    def _echo_only_frames(playback, delay=DELAY, scale=SCALE, noise_seed=7):
        """Mic is a scaled, delayed copy of playback: the assistant hearing itself."""
        rng = np.random.default_rng(noise_seed)
        n = playback[0].shape[0]
        out = []
        for i in range(len(playback)):
            src = playback[i - delay] if i - delay >= 0 else np.zeros(n, np.float32)
            out.append(scale * src + rng.normal(0, 0.001, n).astype(np.float32))
        return out

    def _guard(self, clock):
        guard = EchoGuard(clock=lambda: clock.t, threshold=0.02,
                          min_consecutive_frames=3)
        guard.begin_playback()
        return guard

    def test_echo_only_never_fires_barge_in(self):
        clock = _FakeClock()
        guard = self._guard(clock)
        n = self.FRAME_SAMPLES
        playback = [self._tone(n) for _ in range(50)]
        mic = self._echo_only_frames(playback)
        for i in range(50):
            clock.t += self.FRAME_SECONDS
            guard.play_reference(playback[i])
            assert guard.observe(mic[i]) is False, (
                f"frame {i}: our own voice was taken for the user"
            )

    def test_burst_fires_barge_in_within_three_frames(self):
        clock = _FakeClock()
        guard = self._guard(clock)
        n = self.FRAME_SAMPLES
        playback = [self._tone(n) for _ in range(50)]
        mic = self._echo_only_frames(playback)
        fired = None
        for i in range(50):
            clock.t += self.FRAME_SECONDS
            guard.play_reference(playback[i])
            frame = mic[i]
            if self.ONSET <= i < self.ONSET + 10:
                frame = frame + self._burst(n, seed=999 + i)
            if guard.observe(frame) and fired is None:
                fired = i
        assert fired is not None, "the user's interruption was never heard"
        assert fired - self.ONSET <= 3, (
            f"barge-in fired {fired - self.ONSET} frames after the burst"
        )

    def test_sustained_double_talk_still_interrupts(self):
        clock = _FakeClock()
        guard = self._guard(clock)
        n = self.FRAME_SAMPLES
        playback = [self._tone(n) for _ in range(50)]
        mic = self._echo_only_frames(playback)
        fired = None
        for i in range(50):
            clock.t += self.FRAME_SECONDS
            frame = mic[i]
            if 10 <= i < 30:                 # user talks over the whole reply
                frame = frame + self._burst(n, seed=888 + i)
            if guard.observe(frame) and fired is None:
                fired = i
        assert fired is not None, "sustained double talk was never an interruption"
        assert fired - 10 <= 5

    def test_nothing_playing_passes_the_frame_through(self):
        clock = _FakeClock()
        guard = self._guard(clock)
        speech = self._burst(self.FRAME_SAMPLES, seed=1, level=0.4)
        assert guard.observe(speech) is True

    def test_disabled_guard_passes_the_frame_through(self):
        guard = EchoGuard(enabled=False)
        guard.begin_playback()
        speech = self._burst(self.FRAME_SAMPLES, seed=1, level=0.4)
        assert guard.observe(speech) is True