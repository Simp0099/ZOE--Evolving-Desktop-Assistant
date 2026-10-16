"""The TTS diagnostic must actually reach the speakers, and must not lie about it."""

import numpy as np
import pytest

from scripts import tts_check


def test_play_sends_audio_through_the_shared_output_path(monkeypatch, capsys):
    seen = {}

    def fake(data, rate, cancel_event=None):
        seen["data"] = data
        seen["rate"] = rate
        return "completed"

    monkeypatch.setattr(tts_check, "_sounddevice_play_array", fake)
    audio = np.zeros(24000, dtype=np.float32)

    assert tts_check.play(audio, 24000) is True

    assert seen["data"] is audio
    assert seen["rate"] == 24000
    out = capsys.readouterr().out
    assert "playback start" in out
    assert "playback complete" in out


def test_play_reports_failure_and_returns_false(monkeypatch, capsys):
    def boom(data, rate, cancel_event=None):
        raise RuntimeError("no output device")

    monkeypatch.setattr(tts_check, "_sounddevice_play_array", boom)

    assert tts_check.play(np.zeros(10, dtype=np.float32), 24000) is False
    assert "playback FAILED: no output device" in capsys.readouterr().out


def test_play_treats_a_cancelled_stream_as_failure(monkeypatch):
    monkeypatch.setattr(
        tts_check, "_sounddevice_play_array", lambda d, r, cancel_event=None: "cancelled"
    )
    assert tts_check.play(np.zeros(10, dtype=np.float32), 24000) is False


def test_main_writes_wav_then_plays_and_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_check, "ROOT", tmp_path)
    monkeypatch.setattr(tts_check.tts, "synthesize", lambda text: np.ones(4800, dtype=np.float32))
    played = []
    monkeypatch.setattr(tts_check, "play", lambda a, r: played.append((len(a), r)) or True)

    assert tts_check.main() == 0

    assert (tmp_path / "data" / "kokoro_check.wav").exists()
    assert played == [(4800, tts_check.tts.KOKORO_RATE)]


def test_main_exits_nonzero_when_playback_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_check, "ROOT", tmp_path)
    monkeypatch.setattr(tts_check.tts, "synthesize", lambda text: np.ones(4800, dtype=np.float32))
    monkeypatch.setattr(tts_check, "play", lambda a, r: False)

    assert tts_check.main() == 1


@pytest.mark.parametrize("result", ["cancelled", "completed"])
def test_main_survives_either_playback_result(monkeypatch, tmp_path, result):
    monkeypatch.setattr(tts_check, "ROOT", tmp_path)
    monkeypatch.setattr(tts_check.tts, "synthesize", lambda text: np.ones(4800, dtype=np.float32))
    monkeypatch.setattr(
        tts_check, "_sounddevice_play_array", lambda d, r, cancel_event=None: result
    )

    expected = 0 if result == "completed" else 1
    assert tts_check.main() == expected