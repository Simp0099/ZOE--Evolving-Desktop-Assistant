"""Phase 9 tests: turn tracing measures without changing behavior.

Deterministic: fake clocks, scripted mock providers, stubbed tools. No
network, no microphone, no model weights. Exact durations are never
asserted -- only structure, statuses, isolation, and silence.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from zoe.brain import ZoeBrain  # noqa: E402
from zoe.logger import configure_logging  # noqa: E402
from zoe.providers.base import ErrorKind, ModelResponse  # noqa: E402
from zoe.trace import (  # noqa: E402
    TurnTrace,
    current_trace,
    new_trace,
    use_trace,
)
from tests.mock_providers import build_layer, error, reply, tool_then_reply  # noqa: E402


@pytest.fixture()
def _quiet():
    configure_logging(debug=False)
    yield
    configure_logging(debug=False)


def _layer(behaviours, **kwargs):
    return build_layer(
        models=[{"key": "m1", "model": "m", "priority": 90,
                 "capabilities": {"reasoning": True, "tool_calling": True}}],
        behaviours=behaviours, **kwargs,
    )


class TestTimer:
    def test_monotonic_clock_used(self, _quiet):
        ticks = iter([100.0, 100.5])
        trace = TurnTrace(clock=lambda: next(ticks))
        with trace.span("work"):
            pass
        assert trace.spans[0].ms == pytest.approx(500.0)

    def test_failed_span_keeps_duration_and_status(self, _quiet):
        trace = TurnTrace()
        with pytest.raises(RuntimeError):
            with trace.span("work"):
                raise RuntimeError("boom")
        assert trace.spans[0].status == "fail"
        assert trace.spans[0].ms >= 0.0

    def test_cancel_open_does_not_touch_finished(self, _quiet):
        trace = TurnTrace()
        with trace.span("done"):
            pass
        trace.cancel_open("stale")
        assert trace.spans[0].status == "ok"
        # A later turn starts clean.
        assert TurnTrace().spans == []


class TestCorrelation:
    def test_trace_ids_unique_per_turn(self, _quiet):
        assert new_trace().trace_id != new_trace().trace_id

    def test_nested_ask_reuses_enclosing_trace(self, _quiet):
        outer = new_trace()
        layer = _layer({"m": reply("hi")})
        with use_trace(outer):
            ZoeBrain(model_layer=layer).ask("Why is the sky blue?")
        assert current_trace() is None  # binding restored
        names = [s.name for s in outer.spans]
        assert "classify" in names and "provider_request" in names

    def test_threads_do_not_share_trace(self, _quiet):
        seen = []
        barrier = threading.Barrier(4)

        def _go():
            barrier.wait()
            with use_trace(new_trace()):
                time.sleep(0.01)
                seen.append(current_trace().trace_id)

        threads = [threading.Thread(target=_go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(seen)) == 4


class TestBrainStages:
    def test_local_command_has_no_provider_span(self, _quiet, monkeypatch):
        import zoe.brain as brain_module
        monkeypatch.setitem(brain_module.TOOL_REGISTRY, "tell_time",
                            lambda: "08:42 PM")
        layer = _layer({"m": reply("unused")})
        trace = new_trace()
        with use_trace(trace):
            out = ZoeBrain(model_layer=layer).ask("what time is it")
        assert "08:42 PM" in out
        names = [s.name for s in trace.spans]
        assert "intent_route" in names
        assert "provider_request" not in names
        assert trace._finished is False  # owner (voice loop) finishes

    def test_owned_trace_finishes_local(self, _quiet, monkeypatch, caplog):
        import zoe.brain as brain_module
        monkeypatch.setitem(brain_module.TOOL_REGISTRY, "tell_time",
                            lambda: "08:42 PM")
        layer = _layer({"m": reply("unused")})
        with caplog.at_level("DEBUG", logger="zoe"):
            out = ZoeBrain(model_layer=layer).ask("what time is it")
        assert "08:42 PM" in out
        assert any("route=local" in r.message for r in caplog.records)

    def test_provider_span_carries_model(self, _quiet):
        layer = _layer({"m": reply("hello there")})
        trace = new_trace()
        with use_trace(trace):
            ZoeBrain(model_layer=layer).ask("Why is the sky blue?")
        spans = [s for s in trace.spans if s.name == "provider_request"]
        assert len(spans) >= 1
        assert spans[0].attrs["model"] == "m1"
        assert spans[0].status == "ok"

    def test_fallback_attempts_each_get_a_span(self, _quiet):
        layer = build_layer(
            models=[
                {"key": "p", "model": "p", "priority": 100,
                 "capabilities": {"reasoning": True, "tool_calling": True}},
                {"key": "b", "model": "b", "priority": 10,
                 "capabilities": {"reasoning": True, "tool_calling": True}},
            ],
            behaviours={"p": error(ErrorKind.SERVER, "503"),
                         "b": reply("backup ok")},
        )
        trace = new_trace()
        with use_trace(trace):
            assert ZoeBrain(model_layer=layer).ask("Why?") == "backup ok"
        models = [s.attrs["model"] for s in trace.spans
                  if s.name == "provider_request"]
        assert models == ["p", "b"]  # one span per attempt, no double count

    def test_tool_span_names_tool_not_args(self, _quiet):
        from tests.mock_providers import ToolCall, new_tool_call_id
        from zoe.providers.base import ModelResponse as MR

        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                return MR(text="done")
            return MR(text=None, tool_calls=[ToolCall(
                id=new_tool_call_id(), name="get_system_time", arguments={})])

        layer = _layer({"m": _script})
        trace = new_trace()
        with use_trace(trace):
            ZoeBrain(model_layer=layer).ask("check my inbox")
        tools = [s for s in trace.spans if s.name == "tool_execute"]
        assert [t.attrs["tool"] for t in tools] == ["get_system_time"]
        assert all("arguments" not in t.attrs for t in tools)


class TestTTSReadiness:
    """The first-audio mark is what the E4 latency record is read from.

    Replaces `test_warm_model_reports_ready_not_loading`, which asserted
    `tts_ready`/`tts_load` spans. Those belonged to the removed engine and no
    production code emits them any more (grep: zero matches).

    The contract behind the test -- "the pipeline records when audio actually
    starts" -- still exists, and it is exactly what `docs/VOICE_LATENCY.md` needs
    in order to compute time-to-first-audio. In the current implementation that
    record is a stage mark written by `ConversationMachine.mark()` and surfaced
    through the public `snapshot()`: `["marks"]` for machine-level offsets and
    `["turn"]["marks"]` for the active turn's.
    """

    def test_first_audio_is_marked_when_a_reply_starts_playing(self, _quiet):
        import numpy as np

        from zoe.audio import MicrophoneStream
        from zoe.conversation import State
        from zoe.speech_pipeline import SpeechPlayer
        from zoe.voice_loop import VoiceLoop

        class Source:
            is_open = False

            def open(self):
                self.is_open = True

            def read(self):
                time.sleep(0.004)
                return None

            def close(self):
                self.is_open = False

        loop = VoiceLoop(
            microphone=MicrophoneStream(source=Source()),
            player=SpeechPlayer(
                synthesize=lambda t: np.zeros(2400, dtype=np.float32),
                play=lambda d, r: None,
                stop_playback=lambda: None,
                chunk_ms=100,
            ),
            transcriber=None,
            respond=lambda t: "It is twelve.",
            follow_up_window=0.2,
        )

        loop.start()
        try:
            loop.machine.transition(State.SPEAKING, force=True)
            turn = loop.machine.begin_turn()
            loop._speak("It is twelve.", turn.id)
            snapshot = loop.machine.snapshot()
        finally:
            loop.stop()

        marks = snapshot["marks"]
        assert "first_audio" in marks, (
            f"no first_audio mark in {sorted(marks)}: without it the E4 "
            f"latency record has no time-to-first-audio figure"
        )

        # The mark must belong to the turn this reply was for, not just to the
        # machine: `mark()` only writes the per-turn copy when the live turn id
        # matches, which is what stops a superseded turn claiming the audio.
        turn_view = snapshot["turn"]
        assert turn_view is not None and turn_view["id"] == turn.id, \
            f"the reply was not attributed to the turn it belongs to: {turn_view}"
        assert "first_audio" in turn_view["marks"], \
            f"first_audio is missing from the turn's own marks: {turn_view['marks']}"

        # Note: the relative order of marks is deliberately not asserted here.
        # `snapshot()` rounds offsets to three decimals, and with an injected
        # no-op player both `first_audio` and `playback_end` land in the same
        # millisecond and read as 0.0, so an ordering assertion would be
        # meaningless. Ordering is observable in a real session, from the
        # per-turn offsets in the HUD.


class TestOutput:
    def test_normal_output_clean(self, _quiet, monkeypatch, capsys):
        import zoe.brain as brain_module
        monkeypatch.setitem(brain_module.TOOL_REGISTRY, "tell_time",
                            lambda: "08:42 PM")
        layer = _layer({"m": reply("unused")})
        ZoeBrain(model_layer=layer).ask("what time is it")
        out, err = capsys.readouterr()
        assert "trace" not in out and "ms" not in out and err == ""

    def test_debug_shows_compact_trace(self, _quiet, monkeypatch, caplog):
        import zoe.brain as brain_module
        monkeypatch.setitem(brain_module.TOOL_REGISTRY, "tell_time",
                            lambda: "08:42 PM")
        layer = _layer({"m": reply("unused")})
        configure_logging(debug=True)
        try:
            with caplog.at_level("DEBUG", logger="zoe"):
                ZoeBrain(model_layer=layer).ask("what time is it")
            assert any(r.message.startswith("trace turn_") and "route=local" in r.message
                       for r in caplog.records)
        finally:
            configure_logging(debug=False)

    def test_no_duplicate_handlers_or_events(self, _quiet):
        import logging
        from zoe.logger import logger
        import logging.handlers as lh
        consoles = [h for h in logger.handlers
                    if isinstance(h, logging.StreamHandler)
                    and not isinstance(h, lh.RotatingFileHandler)]
        assert len(consoles) == 1
