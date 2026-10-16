"""Contract tests for the Zoe UI bridge (server.py).

These exercise the HTTP layer with the Brain, the transcriber and the TTS engine
replaced by fakes, so they run with no API key, no microphone and no speakers.
That is the point: the transport, validation and error mapping are what this
file is about, and they are testable in isolation from the model.

The real Brain, provider fallback, tool loop and memory are covered by the
existing suite (tests/test_*.py) and are not re-tested here.
"""

import io
import wave

import pytest
from fastapi.testclient import TestClient

import server
from zoe.brain import BrainError


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose Brain and speech engines are fake and observable."""

    class FakeBrain:
        conversation_id = None

        def __init__(self, conversation_id="zoe-ui"):
            self.conversation_id = conversation_id
            self.turns = []
            self.error = None
            self.reset_count = 0

        def ask(self, text, spoken=False):
            if self.error:
                raise self.error
            self.turns.append((text, spoken))
            return f"echo: {text}"

        def reset_conversation(self):
            self.reset_count += 1
            self.turns.clear()

    brains = {}

    # Patch the Brain CLASS, not `get_brain`, so the real pooling, id validation
    # and eviction logic in server.py is what these tests actually exercise.
    class RecordingBrain(FakeBrain):
        def __init__(self, conversation_id="zoe-ui"):
            super().__init__(conversation_id)
            brains[conversation_id] = self

    monkeypatch.setattr(server, "ZoeBrain", RecordingBrain)

    class FakeTranscriber:
        engine = "fake-whisper"

        def transcribe(self, pcm, sample_rate):
            FakeTranscriber.last = (len(pcm), sample_rate)
            return "hello from the microphone"

    monkeypatch.setattr(server, "get_transcriber", lambda: FakeTranscriber())

    import numpy as np

    monkeypatch.setattr(server, "synthesize", lambda text: np.full(2400, 0.1, dtype=np.float32))

    monkeypatch.setattr(
        server, "get_model_layer",
        lambda: type(
            "Layer",
            (),
            {
                "registry": type("R", (), {"enabled": lambda self: []})(),
                "providers": {},
            },
        )(),
        raising=False,
    )

    server._brains.clear()
    with TestClient(server.app) as test_client:
        test_client.brains = brains
        yield test_client
    server._brains.clear()


def _wav_bytes(rate=8000, seconds=0.1):
    """A real, decodable WAV so the decode path is genuinely exercised."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_real_voice_capability(client):
    """The flags must reflect installed packages, not optimism."""
    body = client.get("/health").json()
    assert isinstance(body["voice_input_available"], bool)
    assert isinstance(body["voice_output_available"], bool)
    assert body["tts_engine"]


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def test_chat_returns_assistant_response(client):
    response = client.post("/chat", json={"message": "hello", "conversation_id": "c1"})
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "echo: hello"
    assert body["conversation_id"] == "c1"


def test_chat_passes_spoken_flag_through(client):
    """Voice replies must stay short, and only the backend knows that."""
    client.post("/chat", json={"message": "hi", "conversation_id": "c1", "spoken": True})
    assert client.brains["c1"].turns == [("hi", True)]


def test_chat_keeps_conversations_separate(client):
    client.post("/chat", json={"message": "first", "conversation_id": "a"})
    client.post("/chat", json={"message": "second", "conversation_id": "b"})
    assert client.brains["a"].turns == [("first", False)]
    assert client.brains["b"].turns == [("second", False)]


def test_chat_preserves_unicode_and_newlines(client):
    message = "héllo ✦\nsecond line\ttabbed"
    client.post("/chat", json={"message": message, "conversation_id": "c1"})
    assert client.brains["c1"].turns[0][0] == message


def test_chat_rejects_empty_message(client):
    assert client.post("/chat", json={"message": "   "}).status_code == 400


def test_chat_rejects_invalid_conversation_id(client):
    """Client ids index a dict, so they are bounded."""
    response = client.post("/chat", json={"message": "hi", "conversation_id": "../etc"})
    assert response.status_code == 400


def test_brain_failure_becomes_503_not_a_fake_reply(client):
    """A provider failure must never be rendered as something Zoe said."""
    client.post("/chat", json={"message": "hi", "conversation_id": "c1"})
    client.brains["c1"].error = BrainError("The AI provider's usage limit has been reached.")

    response = client.post("/chat", json={"message": "hi again", "conversation_id": "c1"})
    assert response.status_code == 503
    assert "usage limit" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Conversation reset
# ---------------------------------------------------------------------------

def test_reset_clears_only_the_named_conversation(client):
    client.post("/chat", json={"message": "remember this", "conversation_id": "a"})
    client.post("/chat", json={"message": "keep this", "conversation_id": "b"})

    client.post("/conversation/reset", json={"conversation_id": "a"})

    assert client.brains["a"].reset_count == 1
    assert client.brains["a"].turns == []
    assert client.brains["b"].turns == [("keep this", False)]


# ---------------------------------------------------------------------------
# Transcribe
# ---------------------------------------------------------------------------

def test_transcribe_decodes_audio_and_returns_text(client):
    response = client.post(
        "/transcribe",
        files={"audio": ("recording.wav", _wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "hello from the microphone"


def test_transcribe_resamples_to_16k_mono_int16(client, monkeypatch):
    """faster-whisper needs int16 mono at 16 kHz; anything else is garbage in."""
    captured = {}

    class CapturingTranscriber:
        engine = "fake-whisper"

        def transcribe(self, pcm, sample_rate):
            captured["pcm"] = pcm
            captured["rate"] = sample_rate
            return "ok"

    monkeypatch.setattr(server, "get_transcriber", lambda: CapturingTranscriber())

    # 8 kHz input: the decoder must resample it, not pass the rate through.
    client.post("/transcribe", files={"audio": ("r.wav", _wav_bytes(rate=8000), "audio/wav")})

    assert captured["rate"] == server.AUDIO_SAMPLE_RATE == 16000
    assert len(captured["pcm"]) % 2 == 0, "PCM must be whole int16 samples"
    assert captured["pcm"], "no audio decoded"
    # 0.1 s at 16 kHz, within resampler rounding.
    assert 1200 <= len(captured["pcm"]) // 2 <= 2000


def test_transcribe_rejects_empty_upload(client):
    response = client.post("/transcribe", files={"audio": ("r.wav", b"", "audio/wav")})
    assert response.status_code == 400


def test_transcribe_rejects_undecodable_audio(client):
    response = client.post(
        "/transcribe", files={"audio": ("r.wav", b"not audio at all", "audio/wav")}
    )
    assert response.status_code == 400


def test_transcribe_rejects_oversized_upload(client):
    oversized = b"RIFF" + b"\x00" * (server.MAX_UPLOAD_BYTES + 1)
    response = client.post("/transcribe", files={"audio": ("r.wav", oversized, "audio/wav")})
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def test_tts_returns_playable_wav(client):
    response = client.post("/tts", json={"text": "hello"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    # A browser can only play this if the header and the payload agree.
    with wave.open(io.BytesIO(response.content)) as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 2400


def test_tts_rejects_empty_text(client):
    assert client.post("/tts", json={"text": "  "}).status_code == 400


def test_tts_failure_is_503_and_leaks_nothing(client, monkeypatch):
    def boom(_text):
        raise RuntimeError("/Users/someone/.env GEMINI_API_KEY=sk-secret-leak")

    monkeypatch.setattr(server, "synthesize", boom)
    response = client.post("/tts", json={"text": "hello"})
    assert response.status_code == 503
    assert "sk-secret-leak" not in response.text
    assert ".env" not in response.text


# ---------------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------------

def test_cors_is_not_wildcard(client):
    """This API can read files and launch apps; any page must not reach it."""
    assert "*" not in server.ALLOWED_ORIGINS
    response = client.options(
        "/chat",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in {
        k.lower() for k in response.headers
    }


def test_cors_allows_the_dev_server(client):
    response = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"