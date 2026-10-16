"""TTS engine selection, validation and spoken-output fallback.

Migrated from `tests/test_tts_say.py`, which covered the retired macOS ``say``
engine and the removed Chatterbox implementation. Neither is restored here.

What survived the migration is the part that is not engine-specific: the
configured engine is actually dispatched to, a broken or unsupported engine
raises a visible error instead of speaking nothing, a bad waveform is rejected
before it reaches the speakers, and ``speak()`` always shows the text even when
every audio path fails.

Nothing here loads a model or opens an audio device: ``zoe.tts.synthesize``
is stubbed, so the tests exercise dispatch and validation, not audio.
"""

import sys
import threading
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from zoe import speech  # noqa: E402
from zoe import tts  # noqa: E402
from zoe.config import TTS_ENGINE  # noqa: E402


@pytest.fixture()
def stub_synth(monkeypatch):
    """Replace Kokoro synthesis with a deterministic waveform."""
    calls = []

    def _fake(text, exaggeration=None):
        calls.append((text, exaggeration))
        return np.full(1024, 0.25, dtype=np.float32)

    monkeypatch.setattr(tts, "synthesize", _fake)
    return calls


class TestEngineSelection:
    def test_the_configured_engine_is_the_one_that_is_called(self, stub_synth):
        assert TTS_ENGINE == "kokoro", "this file tests the configured engine"
        audio = speech.synthesize_for_engine("Hello.")
        assert stub_synth == [("Hello.", None)]
        assert audio.dtype == np.float32

    def test_the_expressiveness_hint_reaches_the_engine(self, stub_synth):
        speech.synthesize_for_engine("Hello.", exaggeration=0.8)
        assert stub_synth == [("Hello.", 0.8)]

    def test_pyttsx3_is_reported_as_having_no_waveform(self, monkeypatch):
        """pyttsx3 speaks but cannot be cancelled, so synthesis refuses it."""
        monkeypatch.setattr(speech, "TTS_ENGINE", "pyttsx3")
        with pytest.raises(speech.TTSEngineError, match="cancellable waveform"):
            speech.synthesize_for_engine("Hello.")

    def test_an_unsupported_engine_is_rejected(self, monkeypatch):
        monkeypatch.setattr(speech, "TTS_ENGINE", "mystery")
        with pytest.raises(speech.TTSEngineError, match="Unsupported TTS engine"):
            speech.synthesize_for_engine("Hello.")

    def test_the_player_default_synthesizes_with_the_configured_engine(
            self, monkeypatch, stub_synth):
        """No injected synthesizer: the default must reach Kokoro.

        Every other player test injects a fake, so nothing else proves the
        shipped default is wired to the configured engine.
        """
        from zoe.speech_pipeline import SpeechPlayer

        player = SpeechPlayer(chunk_ms=100, stop_playback=lambda: None)
        assert player.synthesize is not None
        queued = player.synthesize_to_queue("hello", "turn_001", lambda t: False)
        assert queued >= 1, "the default synthesizer produced no audio"
        assert stub_synth == [("hello", None)], "the player bypassed the engine"


class TestWaveformValidation:
    """Bad audio must be refused at the boundary, not played."""

    def test_valid_audio_passes_through_as_float32(self, stub_synth):
        audio = speech.synthesize_for_engine("Hello.")
        assert audio.dtype == np.float32
        assert audio.ndim == 1
        assert np.isfinite(audio).all()
        assert np.max(np.abs(audio)) <= 1.0

    @pytest.mark.parametrize("samples", [
        np.array([], dtype=np.float32),                  # no audio at all
        np.array([np.nan], dtype=np.float32),           # not a number
        np.array([1.1], dtype=np.float32),              # outside [-1, 1]
    ])
    def test_empty_or_invalid_waveforms_are_rejected(self, monkeypatch, samples):
        monkeypatch.setattr(tts, "synthesize", lambda text, exaggeration=None: samples)
        with pytest.raises(speech.TTSEngineError):
            speech.synthesize_for_engine("Hello.")

    def test_an_unavailable_engine_is_a_visible_error(self, monkeypatch):
        """A missing engine must raise something actionable, never speak nothing.

        The exact type is Kokoro's own `RuntimeError`, raised from
        `zoe.tts._get_pipeline`; what matters is that it surfaces and names
        the fix rather than degrading to silence.
        """
        def _boom(lang_code=None):
            raise RuntimeError("Kokoro is not installed. Run: pip install kokoro soundfile")

        monkeypatch.setattr(tts, "_get_pipeline", _boom)
        with pytest.raises(RuntimeError, match="pip install kokoro"):
            speech.synthesize_for_engine("Hello.")


class TestSynthesisConcurrency:
    def test_concurrent_synthesis_returns_the_same_audio_every_time(self, monkeypatch):
        """One pipeline, many callers: results must not be corrupted.

        `zoe.tts` builds the pipeline once under a lock and reuses it, so
        concurrent callers must all get valid, identical audio.
        """
        def _slow(text, exaggeration=None):
            threading.Event().wait(0.01)
            return np.full(512, 0.1, dtype=np.float32)

        monkeypatch.setattr(tts, "synthesize", _slow)
        results, errors = [], []
        barrier = threading.Barrier(4, timeout=5)

        def _go():
            try:
                barrier.wait()
                results.append(speech.synthesize_for_engine("Hello."))
            except Exception as e:  # noqa: BLE001 - reported, not swallowed
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=_go) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), "a synthesis thread hung"
        assert errors == [], f"concurrent synthesis failed: {errors}"
        assert len(results) == 4
        assert all(r.dtype == np.float32 and r.size == 512 for r in results)


class TestSpokenOutputFallback:
    """`speak()` must always show the text, whatever the audio does."""

    def test_it_speaks_with_the_configured_engine(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(speech, "_speak_kokoro", lambda t: calls.append(t) or True)
        monkeypatch.setattr(speech, "_speak_pyttsx3", lambda t: pytest.fail("wrong engine"))
        speech.speak("Hello.")
        assert calls == ["Hello."]
        assert "[Zoe]: Hello." in capsys.readouterr().out

    def test_it_falls_through_to_the_alternative_engine(self, monkeypatch, capsys):
        order = []
        monkeypatch.setattr(speech, "_speak_kokoro", lambda t: order.append("kokoro") or False)
        monkeypatch.setattr(speech, "_speak_pyttsx3", lambda t: order.append("pyttsx3") or True)
        speech.speak("Hello.")
        assert order == ["kokoro", "pyttsx3"]

    def test_total_audio_failure_still_prints_and_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setattr(speech, "_speak_kokoro", lambda t: False)
        monkeypatch.setattr(speech, "_speak_pyttsx3", lambda t: False)
        speech.speak("Hello.")          # must not raise
        assert "[Zoe]: Hello." in capsys.readouterr().out

    def test_empty_text_is_not_spoken(self, monkeypatch, capsys):
        monkeypatch.setattr(speech, "_speak_kokoro",
                            lambda t: pytest.fail("empty text reached the engine"))
        speech.speak("")
        assert capsys.readouterr().out == ""
