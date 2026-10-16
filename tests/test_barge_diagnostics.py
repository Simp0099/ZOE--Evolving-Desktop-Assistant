"""The barge-in decision log line is a measurement, and measurements must be checkable.

`docs/barge_in_diagnosis.md` section 4 records why the previous diagnostic could
not settle the self-interruption question: its `rms_raw` was carried forward
from a `[AUDIO] callback` line emitted on a 5-second cadence, and its
`residual_energy`/`playback_energy` came from the last sub-frame the echo guard
happened to score -- 0.000000 while the reply was demonstrably playing.

So the properties pinned here are not cosmetic. They are what makes the next
round of diagnosis possible:

* every field is present, in a stable order, on the deciding frame;
* the values are the ones the canceller and guard actually held *now*;
* the analyser parses exactly what the loop writes.

If the line degrades to a stale or partial record, these fail.
"""
import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from zoe.audio import EchoCanceller, EchoGuard, VoiceActivityDetector  # noqa: E402
from zoe.logger import configure_logging  # noqa: E402
from zoe.voice_loop import VoiceLoop  # noqa: E402
from scripts import analyse_barge_in  # noqa: E402

RATE = 16000
FRAME_MS = 80
FRAME = RATE * FRAME_MS // 1000          # 1280 samples


def _pcm(samples) -> bytes:
    """int16 PCM from floats in -1..1.

    Rounds explicitly rather than leaning on `.astype()`, which truncates toward
    zero and would quietly halve every quiet sample. Pure Python because these
    arrays are tiny and built once per test; rounding is the point, not speed.
    """
    import array
    return array.array(
        "h", [max(-32768, min(32767, int(round(v * 32767)))) for v in samples]
    ).tobytes()


def _speech(n, level=0.3, seed=3):
    import numpy as np
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * level).tolist()


@pytest.fixture(autouse=True)
def _quiet():
    configure_logging(debug=False)
    yield
    configure_logging(debug=False)


class _StubLoop(VoiceLoop):
    """A VoiceLoop with the three collaborators the diagnostics read.

    Constructing a real one needs an audio device. The decision line is built
    from `canceller`, `echo`, `vad` and `machine` only, so those are what get
    replaced. Everything else is inherited untouched.
    """

    def __init__(self, *, canceller, echo, vad, machine):
        self.canceller = canceller
        self.echo = echo
        self.vad = vad
        self.machine = machine
        self._last_barge = None


class _Machine:
    class _Turn:
        id = "turn_042"

    def __init__(self, state="speaking"):
        self.state = type("S", (), {"value": state})()
        self.turn = self._Turn()


def _State(value):
    """A conversation state stand-in whose `.value` is what the line reads."""
    return type("S", (), {"value": value})()


class _RealMachine:
    """A real `ConversationMachine`, so `_on_frame` drives genuine state."""

    def __init__(self):
        from zoe.conversation import ConversationMachine, State
        self._inner = ConversationMachine()
        self._state = State.SPEAKING

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    @property
    def turn(self):
        return self._inner.turn


@pytest.fixture()
def parts():
    canceller = EchoCanceller(rate=RATE, playback_rate=24000)
    echo = EchoGuard(sample_rate=RATE)
    vad = VoiceActivityDetector()
    return canceller, echo, vad


@pytest.fixture()
def loop(parts):
    canceller, echo, vad = parts
    return _StubLoop(canceller=canceller, echo=echo, vad=vad,
                     machine=_Machine())


# ============================================================================
# The line has every field, every time
# ============================================================================

class TestLineShape:
    def test_the_field_set_is_the_documented_one(self):
        """Changing this list is a contract change: the analyser and
        docs/barge_in_diagnosis.md both name these fields."""
        assert VoiceLoop.BARGE_FIELDS == (
            "kind", "turn", "state", "barge_in", "elapsed_s",
            "rms_raw", "rms_post",
            "gain", "reduction", "lag_ms",
            "guard_residual", "guard_playback", "guard_streak",
            "guard_ratio", "guard_ratio_bar",
            "bar", "blocked",
        )

    def test_every_field_is_emitted(self, loop, caplog):
        with caplog.at_level("DEBUG", logger="zoe"):
            line = loop._format_barge(
                loop._barge_record("frame", b"\x00\x00", b"\x00\x00", True, False))
        assert line.startswith("[BARGE] ")
        emitted = [tok.split("=", 1)[0] for tok in line.split()[1:]]
        assert emitted == list(VoiceLoop.BARGE_FIELDS)

    def test_no_field_is_empty(self, loop):
        """An empty value makes a positional parser skip the line silently."""
        record = loop._barge_record("frame", b"\x00\x00", b"\x00\x00", True, False)
        for key in VoiceLoop.BARGE_FIELDS:
            assert str(record[key]) != "", f"{key} rendered empty"

    def test_the_values_are_this_frame_not_a_remembered_one(self, loop, parts):
        """The bug this line fixes: stale fields reported as measurements."""
        canceller, echo, vad = parts
        canceller.last_gain = 0.437
        canceller.last_reduction = 0.812
        canceller.last_lag_ms = 24.0
        echo.last_residual_energy = 0.000602
        echo.last_playback_energy = 0.00412
        echo._consecutive = 3

        raw = _pcm(_speech(FRAME, level=0.03))
        record = loop._barge_record("frame", raw, raw, True, False)

        assert record["gain"] == 0.437
        assert record["reduction"] == 0.812
        assert record["lag_ms"] == 24.0
        assert record["guard_residual"] == 0.000602
        assert record["guard_playback"] == 0.00412
        assert record["guard_streak"] == 3

    def test_the_bar_is_the_bar_actually_applied(self, loop, parts):
        """Read from the detector, not copied into the log by hand."""
        _c, _e, vad = parts
        barge_bar = vad.level_needed(True)
        normal_bar = vad.level_needed(False)
        raw = _pcm(_speech(FRAME, level=0.03))
        assert loop._barge_record("frame", raw, raw, True, False)["bar"] \
            == round(barge_bar, 4)
        assert loop._barge_record("frame", raw, raw, False, False)["bar"] \
            == round(normal_bar, 4)
        assert barge_bar > normal_bar, "barge-in is the stricter bar"

    def test_raw_and_post_rms_differ_when_cancellation_did_something(self, loop):
        loud = _pcm(_speech(FRAME, level=0.30))
        quiet = _pcm(_speech(FRAME, level=0.01, seed=9))
        record = loop._barge_record("frame", loud, quiet, True, False)
        assert record["rms_raw"] > record["rms_post"]

    def test_elapsed_is_infinite_when_nothing_is_playing(self, loop):
        raw = _pcm(_speech(FRAME, level=0.03))
        record = loop._barge_record("frame", raw, raw, True, False)
        assert record["elapsed_s"] == "inf"

    def test_elapsed_is_a_number_while_playing(self, loop, parts):
        _c, echo, _v = parts
        echo.begin_playback()
        raw = _pcm(_speech(FRAME, level=0.03))
        record = loop._barge_record("frame", raw, raw, True, False)
        assert isinstance(record["elapsed_s"], float)
        assert record["elapsed_s"] >= 0.0


# ============================================================================
# kind, and the speech-start line
# ============================================================================

class TestSpeechStartRecord:
    def test_interrupt_carries_the_last_frames_measurements(self, loop, parts):
        canceller, echo, _v = parts
        canceller.last_gain = 0.5
        canceller.last_reduction = 0.25
        canceller.last_lag_ms = 12.0
        echo.last_residual_energy = 0.5
        echo.last_playback_energy = 0.25
        raw = _pcm(_speech(FRAME, level=0.03))
        loop._last_barge = loop._barge_record("frame", raw, raw, True, False)

        record = loop._deciding_record("interrupt", True, blocked=False)
        assert record["kind"] == "interrupt"
        assert record["gain"] == 0.5 and record["lag_ms"] == 12.0
        assert record["guard_residual"] == 0.5 and record["bar"] > 0

    def test_a_record_is_produced_even_with_no_prior_frame(self, loop):
        """Never a short line: the analyser must not have to handle variants."""
        record = loop._deciding_record("interrupt", True, blocked=False)
        for key in VoiceLoop.BARGE_FIELDS:
            assert key in record

    def test_declined_is_marked_blocked(self, loop):
        record = loop._deciding_record("declined", True, blocked=True)
        assert record["kind"] == "declined" and record["blocked"] is True

    def test_the_measurement_is_not_re_read_at_speech_start(self, loop, parts):
        """Re-reading would race the next frame's update and reintroduce the
        staleness the line exists to remove."""
        _c, echo, _v = parts
        echo.last_residual_energy = 0.5
        loop._last_barge = loop._barge_record("frame", b"\x00\x00",
                                               b"\x00\x00", True, False)
        echo.last_residual_energy = 999.0          # the next frame has landed
        record = loop._deciding_record("interrupt", True, blocked=False)
        assert record["guard_residual"] == 0.5


# ============================================================================
# Round trip: what the loop writes, the analyser reads
# ============================================================================

class TestAnalyserRoundTrip:
    def _line(self, loop, kind="interrupt"):
        raw = _pcm(_speech(FRAME, level=0.03))
        return loop._format_barge(loop._barge_record(kind, raw, raw, True, False))

    def test_the_analyser_parses_a_line_the_loop_wrote(self, loop):
        parsed = analyse_barge_in.parse_line(self._line(loop))
        assert parsed is not None
        assert parsed["kind"] == "interrupt"
        assert parsed["state"] == "speaking"
        assert parsed["turn"] == "turn_042"
        for key in ("rms_raw", "rms_post", "gain", "reduction", "lag_ms",
                    "guard_residual", "guard_playback", "guard_streak", "bar"):
            assert isinstance(parsed[key], float), f"{key} is not numeric"
        assert parsed["elapsed_s"] == float("inf")    # inf survives, not a parse failure

    def test_every_written_field_survives_the_round_trip(self, loop):
        raw = _pcm(_speech(FRAME, level=0.03))
        record = loop._barge_record("frame", raw, raw, True, False)
        parsed = analyse_barge_in.parse_line(loop._format_barge(record))
        assert set(record) - {"file"} <= set(parsed), \
            f"analyser dropped {set(record) - set(parsed)}"

    def test_the_analyser_ignores_other_log_lines(self):
        for line in (
            "[AUDIO] callback frames=1 rms_raw=0.0007 rms_post_echo=0.0007",
            "[VOICE] reply cut short turn=turn_001 reason=barge-in chunks_played=4",
            "[AUDIO] playback turn=turn_001 dequeued=4 played=4 elapsed_s=0.1",
        ):
            assert analyse_barge_in.parse_line(line + "\n") is None, line

    def test_an_infiElapsed_line_still_formats(self, loop):
        parsed = analyse_barge_in.parse_line(self._line(loop))
        assert analyse_barge_in._fmt(parsed, "elapsed_s", ".3f") == "inf"

    def test_self_test_passes(self):
        assert analyse_barge_in.self_test() == 0


# ============================================================================
# End to end: a real barge-in through VoiceLoop._on_frame
# ============================================================================

class TestEndToEndThroughTheLoop:
    """The stub above proves the record is well-formed. This proves the loop
    actually emits it on the frame that decides -- the thing the previous
    instrumentation failed to do."""

    def _loop(self, monkeypatch, *, playing=True):
        import numpy as np
        from zoe.audio import MicrophoneStream  # noqa: F401  (import check)

        loop = VoiceLoop.__new__(VoiceLoop)
        loop.canceller = EchoCanceller(rate=RATE, playback_rate=24000)
        loop.echo = EchoGuard(sample_rate=RATE)
        loop.vad = VoiceActivityDetector()
        loop.machine = _RealMachine()
        loop.clock = lambda: 0.0
        loop._last_barge = None
        loop._invalid_frame_reported = False
        loop._last_audio_diag = 1e9          # suppress the 5 s callback diag
        loop._ignoring_speech = False
        loop._utterance = b""
        loop._wake_latched = False
        loop._wake_rejections = 0
        loop.wake_enabled = False            # no wake word in this path
        loop._utterances: list = []
        loop._starts: list = []
        loop._ends: list = []

        if playing:
            loop.echo.begin_playback()

        # Frame size must match what `_on_frame` validates against.
        monkeypatch.setattr("zoe.voice_loop.frame_bytes",
                            lambda: FRAME * 2, raising=False)

        # Bind the originals before replacing them, or the wrapper calls itself.
        real_push = loop.vad.push

        def _push(pcm, barge_in=False):
            return real_push(pcm, barge_in=barge_in)

        def _start(event, barge_in):
            loop._starts.append(event)
            # The real `_on_speech_start` runs: it is the thing that emits the
            # interrupt line. Only its terminal effects are stubbed.
            real_start(event, barge_in)

        def _end(event):
            loop._ends.append(event)
            loop._on_speech_end(event)

        real_start = VoiceLoop._on_speech_start.__get__(loop, VoiceLoop)
        loop.interrupt = lambda: None       # no player in this harness
        monkeypatch.setattr(loop.vad, "push", _push)
        monkeypatch.setattr(loop, "_on_speech_start", _start)
        monkeypatch.setattr(loop, "_on_speech_end", _end)
        return loop, np

    def _quiet_pcm(self):
        return _pcm(_speech(FRAME, level=0.002, seed=11))

    def _calibrate(self, loop, frames=20):
        """Establish the VAD noise floor.

        `VoiceActivityDetector` refuses to emit any event while
        `_noise_floor is None` -- it will not decide speech against a floor that
        does not exist yet. That takes ~16 frames. Without this the VAD is
        indistinguishable from one that never fires.
        """
        quiet = self._quiet_pcm()
        for _ in range(frames):
            loop._on_frame(quiet)

    def test_a_barge_in_frame_emits_a_bar_ge_line(self, monkeypatch, caplog):
        loop, _np = self._loop(monkeypatch)
        loud = _pcm(_speech(FRAME, level=0.5))
        with caplog.at_level("DEBUG", logger="zoe"):
            for _ in range(6):                # long enough to clear barge_in_ms
                loop._on_frame(loud)
        lines = [r.message for r in caplog.records if r.message.startswith("[BARGE]")]
        assert lines, "no [BARGE] line emitted on a barge-in frame"
        assert all(line.startswith("[BARGE] kind=frame ") for line in lines)

    def test_a_calm_frame_while_speaking_still_reports_the_bar(self, monkeypatch,
                                                               caplog):
        loop, _np = self._loop(monkeypatch)
        loud = _pcm(_speech(FRAME, level=0.5))
        with caplog.at_level("DEBUG", logger="zoe"):
            loop._on_frame(loud)
        lines = [r.message for r in caplog.records if r.message.startswith("[BARGE]")]
        assert len(lines) == 1
        parsed = analyse_barge_in.parse_line(lines[0])
        assert parsed["kind"] == "frame" and parsed["bar"] > 0

    def test_nothing_is_emitted_when_not_barge_in(self, monkeypatch, caplog):
        """IDLE is not a barge-in decision; logging it would be 12.5 lines/s of
        nothing in the normal case."""
        loop, _np = self._loop(monkeypatch, playing=False)
        loop.machine.state = _State("idle")
        loud = _pcm(_speech(FRAME, level=0.5))
        with caplog.at_level("DEBUG", logger="zoe"):
            for _ in range(6):
                loop._on_frame(loud)
        assert not [r for r in caplog.records if r.message.startswith("[BARGE]")]

    def test_the_speech_start_line_is_info_and_reuses_the_frame(self, monkeypatch,
                                                                caplog):
        loop, _np = self._loop(monkeypatch)
        with caplog.at_level("DEBUG", logger="zoe"):
            self._calibrate(loop)
            caplog.clear()
            loud = _pcm(_speech(FRAME, level=0.5))
            for _ in range(6):
                loop._on_frame(loud)
        assert loop._starts, "the VAD never declared a speech start"
        barge = [r for r in caplog.records if r.message.startswith("[BARGE]")]
        parsed = [analyse_barge_in.parse_line(r.message) for r in barge]
        index = next(i for i, p in enumerate(parsed) if p["kind"] == "interrupt")
        # Frame lines continue after the interrupt, so the interrupt is not last.
        assert barge[index].levelno == logging.INFO
        assert all(b.levelno == logging.DEBUG
                   for b in barge[:index]), "frame lines must stay DEBUG"
        # The interrupt line carries the *immediately preceding* frame's
        # measurements. Compared against a neighbour, not the first frame: the
        # bar is adaptive (it rose 0.02 -> 0.08 as the noise floor tracked the
        # loud frames), so only a neighbouring frame proves the value was carried
        # over rather than recomputed or inherited from something older.
        last, previous = parsed[index], parsed[index - 1]
        assert last["bar"] == previous["bar"]
        assert last["rms_raw"] == previous["rms_raw"]
        assert last["gain"] == previous["gain"]
        assert last["guard_streak"] == previous["guard_streak"]
        # `blocked` is a text field, so the analyser keeps it as the literal
        # string "False" -- its contract is "numeric fields are numeric, the rest
        # stays verbatim", not "everything is typed".
        assert last["kind"] == "interrupt" and last["blocked"] == "False"

    def test_the_instrumentation_changes_no_decision(self, monkeypatch):
        """Two runs, one with the log call removed, must agree on everything the
        VAD and guard did. Instrumentation that perturbs the thing it measures is
        worse than no instrumentation."""
        def _run(with_logging):
            loop, _np = self._loop(monkeypatch)
            self._calibrate(loop)
            loud = _pcm(_speech(FRAME, level=0.5))
            if not with_logging:
                monkeypatch.setattr(loop, "_log_barge", lambda *a: None)
            for _ in range(6):
                loop._on_frame(loud)
            return (loop.machine.state.value, len(loop._starts),
                    loop.vad._speaking, round(loop.vad._voiced_ms, 6),
                    loop.echo.streak)

        assert _run(True) == _run(False)


# ============================================================================
# The canceller and guard expose what the line reads
# ============================================================================

class TestInstrumentationAccessors:
    def test_streak_is_the_guards_own_counter(self):
        guard = EchoGuard(sample_rate=RATE)
        assert guard.streak == 0
        assert guard.streak == guard._consecutive

    def test_level_needed_is_the_detectors_own_bar(self):
        vad = VoiceActivityDetector()
        assert vad.level_needed(True) == vad._level_needed(True)
        assert vad.level_needed(False) == vad._level_needed(False)

    def test_the_fields_the_line_reads_exist_on_the_canceller(self):
        canceller = EchoCanceller(rate=RATE, playback_rate=24000)
        for attr in ("last_gain", "last_reduction", "last_lag_ms", "last_reference"):
            assert hasattr(canceller, attr), attr

    def test_the_fields_the_line_reads_exist_on_the_guard(self):
        guard = EchoGuard(sample_rate=RATE)
        for attr in ("last_residual_energy", "last_playback_energy", "streak"):
            assert hasattr(guard, attr), attr

    def test_a_duck_typed_canceller_does_not_break_the_line(self):
        """VoiceLoop's engine contract is duck-typed; test doubles have no
        `last_gain`. The line must still be complete."""

        class Bare:
            def cancel(self, pcm):
                return pcm

        loop = _StubLoop(canceller=Bare(), echo=EchoGuard(sample_rate=RATE),
                         vad=VoiceActivityDetector(),
                         machine=_Machine())
        raw = _pcm(_speech(FRAME, level=0.03))
        record = loop._barge_record("frame", raw, raw, True, False)
        for key in VoiceLoop.BARGE_FIELDS:
            assert str(record[key]) != "", key
        assert record["gain"] == 0.0 and record["lag_ms"] == 0.0