"""Thin HTTP bridge between the Zoe UI and the Zoe Brain.

The Brain is the product; this file only translates HTTP into ``ZoeBrain.ask``
and back. It never invents a reply: a failed turn becomes an HTTP error carrying
the real reason, which the UI renders verbatim. Provider selection, tool
execution, memory and safety confirmation all stay inside the Brain -- there is
no second assistant here.

Run:  uvicorn server:app --host 127.0.0.1 --port 8001

Endpoints
---------
GET  /health             readiness + honest capability flags
POST /chat               text turn through the Brain
POST /transcribe         uploaded browser audio -> text (local faster-whisper)
POST /tts                text -> WAV audio (the browser plays it)
POST /conversation/reset start a fresh conversation for one id
"""

import io
import os
import re
import threading
from typing import Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from zoe.brain import BrainError, ZoeBrain
from zoe.config import AUDIO_SAMPLE_RATE, TTS_ENGINE
from zoe.logger import logger
from zoe.speech_pipeline import ASRUnavailable, Transcriber, _to_pcm16
from zoe.tts import KOKORO_RATE, synthesize

#: One brain per conversation id, so the UI's chat list and the backend's
#: conversation context stay the same thing. Separate ids get separate histories;
#: the same id always reaches the same conversation.
_brains: Dict[str, ZoeBrain] = {}
_brains_lock = threading.Lock()

_transcriber: Optional[Transcriber] = None
_transcriber_lock = threading.Lock()

#: Client-supplied conversation ids index this dict, so they are bounded: a
#: restricted charset plus a cap stops unbounded growth from junk input.
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_CONVERSATIONS = 64

#: Ceiling on an uploaded recording. Browser MediaRecorder produces Opus at
#: roughly 16-48 kbps, so a minute of speech is well under a megabyte; 25 MB is
#: far above any real utterance and low enough to bound a hostile upload before
#: anything tries to decode it.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Ceiling on synthesis length. ``voice.max_spoken_words`` already caps spoken
#: turns; this is the backstop for a long typed reply handed to /tts.
MAX_TTS_CHARS = 4000


app = FastAPI(title="Zoe UI bridge")

#: Origins the UI is actually served from. A wildcard is deliberately NOT used:
#: this API reaches tools that can read files and launch allowlisted apps, so any
#: web page the user visits must not be able to drive it. Add a port here rather
#: than widening it.
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def get_brain(conversation_id: str) -> ZoeBrain:
    """The Brain for this conversation, created on first use."""
    if not _ID_RE.match(conversation_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid conversation id. Use letters, digits, '.', '_', ':' or '-'.",
        )
    with _brains_lock:
        brain = _brains.get(conversation_id)
        if brain is None:
            if len(_brains) >= MAX_CONVERSATIONS:
                # Evict the oldest rather than refusing to serve.
                oldest = next(iter(_brains))
                del _brains[oldest]
                logger.info("[SERVER] evicted conversation %r (pool full)", oldest)
            brain = ZoeBrain(conversation_id=conversation_id)
            _brains[conversation_id] = brain
        return brain


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------

def get_transcriber() -> Transcriber:
    """The shared transcriber, built on first use.

    Deliberately lazy: faster-whisper takes seconds to load, and a text-only
    session should not pay that at startup.
    """
    global _transcriber
    with _transcriber_lock:
        if _transcriber is None:
            _transcriber = Transcriber()
        return _transcriber


def _decode_to_pcm16(data: bytes) -> bytes:
    """Decode any browser-recorder container to mono int16 PCM at 16 kHz.

    MediaRecorder emits WebM/Opus, which ``soundfile`` cannot read; PyAV decodes
    it and resamples in one pass. The result is exactly what ``Transcriber``
    expects, so no format guessing is needed at the call site.

    ponytail: one decoder for every container. A second WAV-only path would be
    less code but two places to keep correct.
    """
    import av
    import numpy as np

    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.audio:
                raise ValueError("The uploaded file has no audio track.")
            resampler = av.audio.resampler.AudioResampler(
                format="flt", layout="mono", rate=AUDIO_SAMPLE_RATE
            )
            chunks = []
            for frame in container.decode(container.streams.audio[0]):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):  # flush the tail
                chunks.append(out.to_ndarray().reshape(-1))
    except av.error.FFmpegError as e:
        raise ValueError("Could not decode that audio. Try recording again.") from e

    if not chunks:
        raise ValueError("No audio was found in that file.")
    samples = np.concatenate(chunks).astype(np.float32)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _wav_header(data_size: int, sample_rate: int, channels: int) -> bytes:
    """WAV header for interleaved PCM16."""
    header = bytearray()
    header.extend(b"RIFF")
    header.extend((36 + data_size).to_bytes(4, "little"))
    header.extend(b"WAVE")
    header.extend(b"fmt ")
    header.extend((16).to_bytes(4, "little"))
    header.extend((1).to_bytes(2, "little"))
    header.extend(channels.to_bytes(2, "little"))
    header.extend(sample_rate.to_bytes(4, "little"))
    header.extend((sample_rate * channels * 2).to_bytes(4, "little"))
    header.extend((channels * 2).to_bytes(2, "little"))
    header.extend((16).to_bytes(2, "little"))
    header.extend(b"data")
    header.extend(data_size.to_bytes(4, "little"))
    return bytes(header)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=8000)
    conversation_id: str = Field(default="zoe-ui")
    spoken: bool = False  # reply will be spoken, so ask for a short form


class ChatResponse(BaseModel):
    response: str
    conversation_id: str


class TranscribeResponse(BaseModel):
    text: str
    engine: str


class TTSRequest(BaseModel):
    text: str = Field(default="", max_length=MAX_TTS_CHARS)


class HealthResponse(BaseModel):
    status: str
    voice_input_available: bool
    voice_output_available: bool
    models_usable: int
    tts_engine: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health() -> dict:
    """Readiness and real capability flags. Never loads a model to answer."""
    import importlib.util

    from zoe.model_layer import get_model_layer

    def installed(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, AttributeError):
            return False

    layer = get_model_layer()
    usable = [
        spec.key
        for spec in layer.registry.enabled()
        if (p := layer.providers.get(spec.provider)) and p.is_configured()
    ]
    return {
        "status": "ok",
        "voice_input_available": installed("faster_whisper") or installed("speech_recognition"),
        "voice_output_available": TTS_ENGINE in ("kokoro", "pyttsx3") and installed(TTS_ENGINE),
        "models_usable": len(usable),
        "tts_engine": TTS_ENGINE,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> dict:
    """One text turn through the real assistant."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message.")

    brain = get_brain(request.conversation_id)
    try:
        reply = brain.ask(message, spoken=request.spoken)
    except BrainError as error:
        # 503: the assistant is configured but could not serve this turn. The
        # reason is already user-safe; the internal detail stays in the log.
        logger.error("[SERVER] chat failed: %s", error.detail, exc_info=True)
        raise HTTPException(status_code=503, detail=error.message) from error
    return {"response": reply, "conversation_id": request.conversation_id}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(audio: UploadFile = File(...)) -> dict:
    """Uploaded browser audio -> text, using the existing local ASR pipeline."""
    content = await audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio file.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large. Try again.")

    try:
        pcm = _decode_to_pcm16(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ImportError as e:
        raise HTTPException(
            status_code=503, detail="Audio decoding is unavailable on the backend."
        ) from e

    if not pcm:
        raise HTTPException(status_code=400, detail="No speech was found in that recording.")

    try:
        transcriber = get_transcriber()
        text = transcriber.transcribe(pcm, AUDIO_SAMPLE_RATE)
    except ASRUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - surfaced as 500, logged in full
        logger.error("[SERVER] transcription failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not understand that recording.") from e

    return {"text": text, "engine": transcriber.engine or "unknown"}


@app.post("/tts")
async def tts(request: TTSRequest) -> StreamingResponse:
    """Synthesize a reply with the configured engine and stream it as WAV.

    Audio is returned as bytes rather than a file path: the browser cannot read
    the server's filesystem, and the UI plays this directly.
    """
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")

    try:
        audio = synthesize(text)
    except Exception as e:  # noqa: BLE001
        logger.error("[SERVER] TTS failed: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail="Speech synthesis is unavailable.") from e

    if audio.size == 0:
        raise HTTPException(status_code=500, detail="TTS returned empty audio.")

    pcm = _to_pcm16(audio, KOKORO_RATE)
    header = _wav_header(len(pcm), KOKORO_RATE, 1)

    def stream():
        yield header
        yield pcm

    return StreamingResponse(
        stream(),
        media_type="audio/wav",
        headers={"Content-Disposition": "inline; filename=speech.wav"},
    )


@app.post("/conversation/reset")
def reset_conversation(request: ChatRequest) -> dict:
    """Clear one conversation's context. Long-term memory is untouched."""
    brain = get_brain(request.conversation_id)
    brain.reset_conversation()
    return {"status": "reset", "conversation_id": request.conversation_id}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("ZOE_UI_PORT", "8001"))
    uvicorn.run(app, host="127.0.0.1", port=port)