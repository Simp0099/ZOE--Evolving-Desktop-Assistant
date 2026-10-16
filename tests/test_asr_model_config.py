"""The ASR model and compute type must come from config, not a hard-coded default.

`Transcriber` used to take `model_size="base.en"` in its signature and
`compute_type="int8"` inline at the `WhisperModel` call, so neither was
configurable: changing the recogniser meant editing code. These tests pin that
both now reach the constructor, and that the Google fallback still catches an
absent `faster_whisper`.

`WhisperModel` is faked, so no model is downloaded and the suite stays offline.
"""

import sys
import types

import numpy as np
import pytest

from zoe.speech_pipeline import ASRUnavailable, Transcriber


class _FakeWhisperModel:
    """Records what the constructor was handed; returns no segments."""

    calls = []

    def __init__(self, model_size, device=None, compute_type=None, **kwargs):
        type(self).calls.append(
            {"model_size": model_size, "device": device, "compute_type": compute_type}
        )

    def transcribe(self, samples, **kwargs):
        return iter(()), None


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """Install a fake `faster_whisper` module and clear the recorded calls."""
    _FakeWhisperModel.calls = []
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


def _load(transcriber):
    transcriber._load_whisper()
    return _FakeWhisperModel.calls[-1]


class TestAsrConfigReachesWhisper:
    def test_configured_model_and_compute_type_reach_the_constructor(self, fake_faster_whisper):
        from zoe.config import ASR_COMPUTE_TYPE, ASR_MODEL

        call = _load(Transcriber())
        assert call["model_size"] == ASR_MODEL, (
            f"WhisperModel built {call['model_size']!r}, expected the configured "
            f"{ASR_MODEL!r}"
        )
        assert call["compute_type"] == ASR_COMPUTE_TYPE

    def test_config_default_is_a_known_model(self):
        """config.yaml is the single source of truth, so pin what it resolves to.

        `small.en` was chosen on measurement: `distil-large-v3` transcribes a
        3.5 s utterance with a 4.08 s warm median on this machine, well past the
        1.0 s target. See docs/VOICE_LATENCY.md for the full comparison.
        """
        from zoe.config import ASR_COMPUTE_TYPE, ASR_MODEL

        assert ASR_MODEL == "small.en", (
            f"asr.model changed to {ASR_MODEL!r} without re-measuring latency "
            f"and accuracy"
        )
        assert ASR_COMPUTE_TYPE == "int8"

    def test_changing_asr_model_changes_the_constructed_model(self, fake_faster_whisper,
                                                              monkeypatch):
        # Deliberately not the configured value, so this proves the *config* is
        # read rather than that a constant matches a constant.
        monkeypatch.setattr("zoe.speech_pipeline.ASR_MODEL", "distil-large-v3")
        call = _load(Transcriber())
        assert call["model_size"] == "distil-large-v3"

    def test_explicit_argument_still_beats_config(self, fake_faster_whisper,
                                                  monkeypatch):
        monkeypatch.setattr("zoe.speech_pipeline.ASR_MODEL", "small.en")
        call = _load(Transcriber(model_size="tiny"))
        assert call["model_size"] == "tiny"

    def test_explicit_compute_type_beats_config(self, fake_faster_whisper,
                                                monkeypatch):
        monkeypatch.setattr("zoe.speech_pipeline.ASR_COMPUTE_TYPE", "int8")
        call = _load(Transcriber(compute_type="float32"))
        assert call["compute_type"] == "float32"

    def test_transcription_still_runs_through_the_fake(self, fake_faster_whisper):
        """The constructor is the only thing that changed; the call path is intact."""
        transcriber = Transcriber()
        pcm = (np.zeros(16000, dtype=np.int16)).tobytes()
        assert transcriber.transcribe(pcm) == ""
        assert transcriber.engine == "faster-whisper"
        assert transcriber.last_error == "no speech recognised"


class TestGoogleFallback:
    def test_google_is_used_when_faster_whisper_is_missing(self, monkeypatch):
        """An absent faster_whisper must fall back, not raise."""
        import builtins

        real_import = builtins.__import__

        def _no_whisper(name, *args, **kwargs):
            if name == "faster_whisper":
                raise ImportError("No module named 'faster_whisper'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_whisper)

        class _Recognizer:
            def recognize_google(self, audio, language=None):
                return "hello there"

        class _UnknownValueError(Exception):
            pass

        monkeypatch.setitem(sys.modules, "speech_recognition", types.SimpleNamespace(
            Recognizer=_Recognizer,
            AudioData=lambda pcm, rate, width: ("audio", rate, width),
            # A real subclass, not Exception: aliasing it to Exception would make
            # `_google`'s except clause swallow unrelated errors and fake a pass.
            UnknownValueError=_UnknownValueError,
        ))

        transcriber = Transcriber(recognizer=_Recognizer())
        pcm = (np.zeros(16000, dtype=np.int16)).tobytes()
        assert transcriber.transcribe(pcm) == "hello there"
        assert transcriber.engine == "google"

    def test_asr_unavailable_is_raised_when_neither_engine_exists(self, monkeypatch):
        """No recogniser at all still raises ASRUnavailable rather than inventing text."""
        import builtins

        real_import = builtins.__import__

        def _nothing(name, *args, **kwargs):
            if name in ("faster_whisper", "speech_recognition"):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _nothing)
        with pytest.raises(ASRUnavailable):
            Transcriber().transcribe((np.zeros(1600, dtype=np.int16)).tobytes())