from types import SimpleNamespace
from unittest.mock import patch

import pytest

import start


def _layer(configured):
    provider = SimpleNamespace(is_configured=lambda: configured)
    model = SimpleNamespace(key="openrouter-model", provider="openrouter")
    return SimpleNamespace(
        registry=SimpleNamespace(enabled=lambda: [model]),
        providers={"openrouter": provider},
    )


def test_startup_accepts_configured_non_gemini_provider(capsys):
    with patch("zoe.model_layer.ModelLayer.from_config", return_value=_layer(True)):
        assert start.check_environment() is True
    assert "1 usable" in capsys.readouterr().out


def test_startup_rejects_when_no_enabled_provider_is_configured(capsys):
    with patch("zoe.model_layer.ModelLayer.from_config", return_value=_layer(False)):
        assert start.check_environment() is False
    assert "No enabled model" in capsys.readouterr().out


def test_unavailable_voice_reports_and_cleans_up_without_starting_audio(
        monkeypatch, capsys):
    """Voice that cannot start must say so and not leave the session running.

    Phase C removed the dedicated wake-word entry point along with the second,
    single-purpose listener stream. Voice entry is `run_voice_mode`, and when
    the loop cannot start it reports the reason, stops perception, and returns
    -- it must not fall through into a text session the user never asked for,
    and it must not leave the camera running.
    """
    import zoe.main as main
    import zoe.tts as tts
    import zoe.voice_loop as voice_loop

    perception = patch.object(main, "start_perception")
    stop_perception = patch.object(main, "stop_perception")
    warm_up = patch.object(tts, "warm_up")
    start_loop = patch.object(voice_loop, "start_voice_loop", return_value=None)
    with patch.object(main, "ZoeBrain"), perception as started, \
            stop_perception as stopped, warm_up, start_loop as loop_started:
        main.run_voice_mode()

    loop_started.assert_called_once()
    started.assert_called_once()
    stopped.assert_called_once()
    assert "Voice is unavailable" in capsys.readouterr().out


# ============================================================================
# Voice-engine health check
# ============================================================================
#
# The check must follow the *configured* engine. It used to probe Chatterbox
# unconditionally, so with Kokoro selected startup printed
#
#   [Check] Chatterbox TTS: cannot import name '_resolve_chatterbox_reference'
#   from 'zoe.speech'
#
# which was both true and irrelevant: that helper was removed with the rest of
# the Chatterbox implementation in ddbff2e, and it says nothing about Kokoro.

def _engine(monkeypatch, name):
    """Point zoe.config.TTS_ENGINE at `name` for this test."""
    import zoe.config as config

    monkeypatch.setattr(config, "TTS_ENGINE", name, raising=False)


def _installed(monkeypatch, *present):
    """Report exactly `present` as installed, without importing anything.

    The check probes with `find_spec`, so that is the boundary to mock. Blocking
    real imports instead would make every test pay the multi-second torch import
    that the check exists to avoid.
    """
    import importlib.util

    real = importlib.util.find_spec
    keep = set(present)

    def _spec(name, *args, **kwargs):
        return real(name, *args, **kwargs) if name in keep else None

    monkeypatch.setattr(importlib.util, "find_spec", _spec)


class TestVoiceEngineHealth:
    @pytest.mark.requires_kokoro
    def test_kokoro_selected_reports_kokoro(self, monkeypatch, capsys):
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch, "kokoro")
        assert start.check_voice_engine() is True
        out = capsys.readouterr().out
        assert "kokoro" in out
        assert "Chatterbox" not in out

    @pytest.mark.requires_kokoro
    def test_kokoro_startup_never_mentions_chatterbox(self, monkeypatch, capsys):
        """The reported failure: the stale import warning must be gone.

        With the chatterbox package installed and its helper long removed, a
        Kokoro startup must not reference Chatterbox at all.
        """
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch, "kokoro", "chatterbox")
        assert start.check_voice_engine() is True
        out = capsys.readouterr().out
        assert "Chatterbox" not in out
        assert "_resolve_chatterbox_reference" not in out
        assert "cannot import name" not in out

    def test_full_startup_check_does_not_probe_chatterbox(self, monkeypatch, capsys):
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch, "kokoro", "chatterbox", "pyttsx3")
        with patch("zoe.model_layer.ModelLayer.from_config", return_value=_layer(True)):
            assert start.check_environment() is True
        out = capsys.readouterr().out
        assert "_resolve_chatterbox_reference" not in out
        assert "Chatterbox" not in out

    @pytest.mark.requires_kokoro
    def test_a_missing_chatterbox_package_does_not_affect_kokoro(
            self, monkeypatch, capsys):
        """Chatterbox is optional; its absence is not a Kokoro failure."""
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch, "kokoro")          # chatterbox deliberately absent
        assert start.check_voice_engine() is True
        assert "kokoro" in capsys.readouterr().out

    def test_a_genuine_kokoro_failure_is_still_reported(self, monkeypatch, capsys):
        """The fix must not hide a real problem with the active engine."""
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch)                     # nothing installed
        assert start.check_voice_engine() is False
        assert "kokoro is selected but not installed" in capsys.readouterr().out

    def test_pyttsx3_selected_checks_pyttsx3(self, monkeypatch, capsys):
        _engine(monkeypatch, "pyttsx3")
        _installed(monkeypatch, "pyttsx3")
        assert start.check_voice_engine() is True
        assert "pyttsx3" in capsys.readouterr().out

    def test_pyttsx3_selected_but_missing_is_reported(self, monkeypatch, capsys):
        _engine(monkeypatch, "pyttsx3")
        _installed(monkeypatch)
        assert start.check_voice_engine() is False
        assert "pyttsx3 is selected but not installed" in capsys.readouterr().out

    def test_chatterbox_selected_is_reported_unsupported_not_healthy(
            self, monkeypatch, capsys):
        """Chatterbox must not be reported as working when it cannot be driven.

        Its implementation is gone from `zoe.speech`, so selecting it cannot
        synthesize. Claiming otherwise would be the same misleading health check
        in the opposite direction.
        """
        _engine(monkeypatch, "chatterbox")
        _installed(monkeypatch, "chatterbox")
        assert start.check_voice_engine() is False
        assert "UNSUPPORTED 'chatterbox'" in capsys.readouterr().out

    def test_an_unknown_engine_is_reported_not_silently_ok(self, monkeypatch, capsys):
        _engine(monkeypatch, "banana")
        _installed(monkeypatch, "banana")
        assert start.check_voice_engine() is False
        assert "UNSUPPORTED 'banana'" in capsys.readouterr().out

    @pytest.mark.requires_kokoro
    def test_engine_selection_is_case_insensitive(self, monkeypatch, capsys):
        _engine(monkeypatch, "Kokoro")
        _installed(monkeypatch, "kokoro")
        assert start.check_voice_engine() is True
        assert "kokoro" in capsys.readouterr().out

    def test_unreadable_configuration_is_reported(self, monkeypatch, capsys):
        """A broken config must say so, not be reported as a healthy engine."""
        import sys

        import zoe.config as config

        class _Exploding:
            def __getattr__(self, _name):
                raise RuntimeError("config is broken")

        monkeypatch.setitem(sys.modules, "zoe.config", _Exploding())
        assert start.check_voice_engine() is False
        assert "configuration unreadable" in capsys.readouterr().out


class TestVoiceEngineSelectionUnchanged:
    def test_kokoro_remains_the_selected_engine(self):
        """This phase must not switch engines."""
        from zoe.config import TTS_ENGINE

        assert TTS_ENGINE.strip().lower() == "kokoro"

    def test_kokoro_is_still_able_to_synthesize(self):
        """Kokoro was working before this change and must still work.

        Only the *check* changed. Exercising the real pipeline would download
        and load a model, so this asserts the dispatch accepts Kokoro -- that it
        does not raise "Unsupported TTS engine" -- without generating audio.
        """
        import zoe.speech as speech
        import zoe.tts as tts

        assert callable(tts.synthesize)
        assert callable(speech.synthesize_for_engine)
        # An empty utterance returns empty audio rather than reaching the model.
        assert tts.synthesize("").size == 0

    @pytest.mark.requires_kokoro
    def test_the_check_does_not_import_the_engine(self, monkeypatch):
        """A health check must not load the model it is checking.

        The question is "is it installed", which `find_spec` answers in
        microseconds. A real import pulls in torch and costs seconds at startup
        for a package the assistant may not even use.
        """
        import zoe.tts as tts

        loaded = []
        monkeypatch.setattr(tts, "_get_pipeline", lambda: loaded.append(True))
        _engine(monkeypatch, "kokoro")
        _installed(monkeypatch, "kokoro")
        assert start.check_voice_engine() is True
        assert loaded == []

    def test_the_removed_helper_is_not_referenced_at_startup(self):
        """No startup path may ask for a helper that no longer exists."""
        from pathlib import Path

        source = Path(start.__file__).read_text(encoding="utf-8")
        assert "_resolve_chatterbox_reference" not in source
