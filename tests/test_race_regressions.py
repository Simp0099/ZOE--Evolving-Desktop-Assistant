"""Regression tests for voice-turn races found in the Zoe field logs.

Field log: "cannot go listening -> thinking (transcript ready)" appeared 151
times. Cause: the user spoke over the assistant while it was still
transcribing. The interrupt moved the machine to LISTENING, then the superseded
turn tried to enter THINKING, which is illegal from LISTENING. The fix discards
a superseded turn before it can touch state.
"""
import time

from zoe.conversation import State
from tests.test_phase4_voice import FakeASR, _make_loop, _speak_into, _rest, _await_state


class InterruptingASR(FakeASR):
    """Simulates the user talking over the assistant during transcription."""

    def __init__(self, text="what time is it"):
        super().__init__(text=text)
        self.loop = None

    def transcribe(self, pcm, sample_rate=16000):
        self.calls += 1
        if self.loop is not None:
            self.loop.interrupt()       # barge-in arrives mid-transcription
        time.sleep(0.05)
        return self.text


def test_barge_in_during_transcription_is_not_reported_as_an_error():
    asr = InterruptingASR()
    loop = _make_loop(transcriber=asr)
    asr.loop = loop
    loop.start()
    try:
        _speak_into(loop)
        assert _await_state(loop, _rest(loop), State.LISTENING, State.IDLE)
        time.sleep(0.2)
        assert "cannot go" not in (loop.last_error or ""), loop.last_error
        assert loop.machine.state is not State.ERROR
    finally:
        loop.stop()


class StateMovedASR(FakeASR):
    """Something other than a barge-in moves the machine to LISTENING mid-turn,
    e.g. a rest-state settle from a neighbouring path. The turn is NOT cancelled."""

    def __init__(self, text="what time is it"):
        super().__init__(text=text)
        self.loop = None

    def transcribe(self, pcm, sample_rate=16000):
        self.calls += 1
        if self.loop is not None:
            self.loop.machine.transition(State.LISTENING, reason="rest settle", force=True)
        return self.text


def test_state_moved_under_a_live_turn_still_gets_its_reply_without_error():
    asr = StateMovedASR()
    loop = _make_loop(transcriber=asr)
    asr.loop = loop
    loop.start()
    try:
        _speak_into(loop)
        assert _await_state(loop, _rest(loop), State.LISTENING, State.IDLE)
        time.sleep(0.2)
        assert "cannot go" not in (loop.last_error or ""), loop.last_error
        assert loop.machine.state is not State.ERROR
    finally:
        loop.stop()
