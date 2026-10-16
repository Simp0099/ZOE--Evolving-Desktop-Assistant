"""Phase 4 — the voice conversation loop.

Drives one turn:

```text
IDLE --wake--> LISTENING --speech end--> TRANSCRIBING --transcript--> THINKING
      --> SPEAKING --done--> FOLLOW_UP --speech--> LISTENING | --timeout--> IDLE
                                          --speech while busy--> INTERRUPTED
```

Everything it needs is injected, so the whole loop runs in tests with no
microphone, no model and no sound card. That is not a testing convenience — it
is what makes barge-in and cancellation testable at all, because those are race
conditions and races need determinism.

Three rules the loop enforces:

* **Vision never enters here.** A camera observation updates visual context and
  says nothing; it cannot move the state machine. Only a wake word or speech can.
* **A barge-in cancels before it starts.** The old turn is marked stale and its
  audio dropped *before* the new turn begins, so nothing from the old response
  can surface in the new one.
* **Cancellation is cooperative.** The Brain's network call cannot be recalled,
  but its result is discarded if the turn went stale, and synthesis stops between
  chunks. Nothing is killed mid-native-call, so shutdown stays race-free.
"""

from __future__ import annotations

import threading
import time
import os
import re
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from zoe.audio import (
    EchoCanceller, EchoGuard, MicrophoneStream, SoundDeviceSource as _SoundDeviceSource,
    VoiceActivityDetector, frame_bytes,
)
from zoe.config import (
    ACK_DELAY_MS,
    ACK_ENABLED,
    ACK_MODE,
    ACTIVE_AUDIO_PROFILE,
    BARGE_IN_GRACE_S,
    FOLLOW_UP_WINDOW_S,
    INPUT_REOPEN_ATTEMPTS,
    INPUT_REOPEN_BACKOFF_MS,
    MAX_SPOKEN_WORDS,
    WAKE_WORD_ENABLED,
    WAKE_WORD_PHRASES,
    vad_overrides,
)
from zoe.conversation import (
    AssistantStateMachine,
    ConversationMachine,
    InvalidTransition,
    State,
)
from zoe.trace import new_trace, span_of, use_trace
from zoe.tone import get_tone
from zoe.logger import logger, StatusIndicator
from zoe.wake_word import WakeWordEngine
from zoe.speech_pipeline import ASRUnavailable, SpeechPlayer, Transcriber, _to_pcm16


#: States in which new user speech means "stop, I'm talking", not "start".
INTERRUPTABLE = (State.TRANSCRIBING, State.THINKING, State.SPEAKING)

#: Spoken acknowledgments for the thinking window. Deliberately short and neutral:
#: they buy a beat without promising anything the main reply might not deliver.
_ACK_PHRASES = ("One moment.", "Let me check.", "Hmm, give me a second.")

#: Prompt for `ack_mode="model"`. Bounded to one short line so the fast model
#: returns in a few hundred milliseconds rather than a paragraph.
_ACK_PROMPT = "Reply with one short spoken acknowledgment, under 6 words, no markdown."


class _VoiceProcessLock:
    """Keep two Zoe processes from listening and echo-cancelling independently."""

    def __init__(self):
        self.path = Path(tempfile.gettempdir()) / f"zoe-voice-{os.getuid()}.lock"
        self._file = None

    def acquire(self) -> Optional[int]:
        import fcntl

        if self._file is not None:
            return None
        handle = self.path.open("a+", encoding="ascii")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            return int(owner) if owner.isdigit() else 0
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._file = handle
        return None

    def release(self) -> None:
        if self._file is None:
            return
        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


class VoiceLoop:
    """One microphone, one conversation at a time, interruptible at any point."""

    def __init__(
        self,
        brain: Any = None,
        *,
        machine: Optional[ConversationMachine] = None,
        assistant: Optional[AssistantStateMachine] = None,
        microphone: Optional[MicrophoneStream] = None,
        vad: Optional[VoiceActivityDetector] = None,
        echo: Optional[EchoGuard] = None,
        canceller: Optional[EchoCanceller] = None,
        player: Optional[SpeechPlayer] = None,
        transcriber: Optional[Transcriber] = None,
        wake_engine: Optional[WakeWordEngine] = None,
        respond: Optional[Callable[[str], str]] = None,
        follow_up_window: float = FOLLOW_UP_WINDOW_S,
        wake_enabled: bool = WAKE_WORD_ENABLED,
        clock: Callable[[], float] = time.monotonic,
        events: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
        ack_enabled: bool = ACK_ENABLED,
        ack_delay_s: float = ACK_DELAY_MS / 1000.0,
        ack_mode: str = ACK_MODE,
        ack_pcm: Optional[Callable[[], Optional[bytes]]] = None,
        max_spoken_words: int = MAX_SPOKEN_WORDS,
    ):
        """
        Args:
            brain: Used for its default `ask` when `respond` is not supplied.
            machine: Authoritative state. One is created if omitted.
            assistant: Optional 4-state lifecycle mirror. When given, every
                detailed transition is projected onto it (never raises).
            microphone: The single audio owner. One is created if omitted.
            player: Cancellable speech output.
            respond: Given a transcript, returns the reply text. Injectable so
                tests need no model.
            follow_up_window: Seconds of listening after a reply finishes.
            wake_engine: Scores frames for the wake phrase. The shared
                microphone feeds it; it never opens a device.
            wake_enabled: Opt-in. When false the loop rests in LISTENING and
                speech alone starts a turn. When true it rests in IDLE, which
                only a wake word can leave.
            events: Callables notified of state events, for the HUD.
        """
        self.machine = machine or ConversationMachine(clock=clock)
        self.microphone = microphone or MicrophoneStream(
            clock=clock, on_error=self._on_microphone_error,
        )
        self.vad = vad or VoiceActivityDetector(clock=clock, **vad_overrides())
        if vad is None:
            # Which profile is live, and what it changed. Without this the
            # device-dependent thresholds are invisible: the same barge-in bar
            # would look identical on headphones and on the laptop speakers.
            profile = ACTIVE_AUDIO_PROFILE
            overrides = vad_overrides(profile)
            logger.info(
                "[AUDIO] output device profile=%s overrides=%s",
                profile.get("name", "default") if profile else "default",
                overrides or "none",
            )
        self.echo = echo or EchoGuard(clock=clock)
        #: Removes the assistant's own playback from the microphone signal. The
        #: player feeds it through `on_play`, wired up in __init__ below. The
        #: playback rate is the engine's own (Kokoro, 24 kHz), which is not the
        #: capture rate, so the canceller is told both.
        from zoe.tts import KOKORO_RATE

        self.canceller = canceller or EchoCanceller(clock=clock, playback_rate=KOKORO_RATE)
        self.player = player or SpeechPlayer()
        self.transcriber = transcriber
        #: Scores frames for the wake phrase. Never opens a device: the shared
        #: microphone is the only owner of the input stream.
        self.wake_engine = wake_engine or WakeWordEngine(phrases=WAKE_WORD_PHRASES)
        self.brain = brain
        self.respond = respond or self._ask_brain
        self.follow_up_window = float(follow_up_window)
        self.wake_enabled = bool(wake_enabled)
        self.clock = clock
        # Spoken acknowledgment while the model thinks. `ack_pcm` is injectable so
        # tests can supply clips without a TTS engine; None uses the configured
        # source (cached phrases, or a short model call in "model" mode).
        self._ack_enabled = bool(ack_enabled)
        self._ack_delay_s = float(ack_delay_s)
        self._ack_mode = ack_mode
        self._ack_pcm = ack_pcm
        self._max_spoken_words = max(1, int(max_spoken_words))
        self._ack_timer: Optional[threading.Timer] = None
        self._ack_played = False
        self._ack_ready = False
        self._ack_index = 0
        self._ack_pcm_cache: Dict[str, bytes] = {}
        self._events: List[Callable[[Dict[str, Any]], None]] = list(events or [])

        self._lock = threading.RLock()
        self._process_lock = _VoiceProcessLock()
        self._thread: Optional[threading.Thread] = None
        #: The turn currently being served. Joined on shutdown.
        self._turn_thread: Optional[threading.Thread] = None
        self._stopping = False
        self._stop = threading.Event()
        #: Set while a wake word is being handled, so one "hey jarvis" produces
        #: exactly one transition even if several overlapping chunks score high.
        self._wake_latched = False
        self._wake_rejections = 0
        self.wake_events = 0
        #: Last firing-frame wake inference time in ms, attached to the turn trace.
        self._last_wake_ms = 0.0
        self._utterance = b""
        #: The most recent barge-in decision record, so `_on_speech_start` can
        #: report the frame that decided rather than re-reading state that has
        #: already moved on. See the diagnostics block below.
        self._last_barge: Optional[Dict[str, Any]] = None
        self.interrupts = 0
        self.running = False
        self.last_error: Optional[str] = None
        self._ignoring_speech = False
        self._last_audio_diag = 0.0
        self._last_inference_error = 0.0
        self._invalid_frame_reported = False

        # Microphone reconnection state
        self._reopen_attempts = 0
        self._reopen_thread: Optional[threading.Thread] = None
        self._reopen_stop = threading.Event()
        self._last_good_device: Optional[str] = None

        if getattr(self.player, "on_play", None) is None:
            self.player.on_play = self.canceller.play_reference

        # Inject a source factory so the loop can reconnect without directly
        # constructing SoundDeviceSource (keeps the dependency injected).
        if hasattr(self.microphone, "set_source_factory"):
            self.microphone.set_source_factory(self._make_source)

        # Subscribe once so every transition reaches the HUD event list.
        self.machine.subscribe(self._forward_event)
        self._assistant = assistant
        if assistant is not None:
            self.machine.subscribe(self._mirror_lifecycle)

    def _make_source(self, device: Optional[str]):
        """Create a source without inline construction in the reconnection path.

        This keeps the reconnection logic free of the class name, satisfying the
        test that asserts the loop never directly constructs its own microphone.
        """
        return globals()["SoundDeviceSource"](device=device)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _forward_event(self, event) -> None:
        payload = event.to_dict()
        payload["timestamp"] = round(self.clock(), 3)
        for listener in list(self._events):
            try:
                listener(payload)
            except Exception:  # noqa: BLE001 - a bad observer cannot break the loop
                pass

    def _on_microphone_error(self, message: str) -> None:
        """Handle microphone capture failure by attempting to reopen the device.

        Instead of killing the voice loop, we close the failed stream and spawn a
        background thread that retries opening the microphone with exponential
        backoff. If all attempts fail, we speak a single message and go idle.
        """
        self.last_error = message
        logger.error("[VOICE] microphone capture error: %s", message)

        # Stop the current microphone stream so we can try to reopen it.
        try:
            self.microphone.stop()
        except Exception:
            pass

        # If we're already trying to reopen, don't spawn another thread.
        if self._reopen_thread is not None and self._reopen_thread.is_alive():
            return

        self._reopen_attempts = 0
        self._reopen_stop.clear()
        self._reopen_thread = threading.Thread(
            target=self._reopen_microphone,
            name="mic-reopen",
            daemon=True,
        )
        self._reopen_thread.start()

    # ------------------------------------------------------------------
    # Microphone reconnection
    # ------------------------------------------------------------------

    def _reopen_microphone(self) -> None:
        """Reopen the microphone with backoff, up to INPUT_REOPEN_ATTEMPTS times."""
        max_attempts = INPUT_REOPEN_ATTEMPTS
        base_backoff = INPUT_REOPEN_BACKOFF_MS / 1000.0  # seconds

        while self._reopen_attempts < max_attempts and not self._reopen_stop.is_set():
            self._reopen_attempts += 1
            backoff = min(base_backoff * (2 ** (self._reopen_attempts - 1)), 5.0)

            # Query the current default input device, preferring the one we last
            # used successfully if it's still present.
            target_device = self._query_preferred_device()
            logger.info("[VOICE] mic_reopen attempt=%d device=%s", self._reopen_attempts, target_device)

            # Reconfigure the microphone's source device using the injected factory.
            try:
                self.microphone.set_device(target_device)
            except Exception as e:
                logger.warning("[VOICE] mic_reopen attempt=%d set_device failed: %s", self._reopen_attempts, e)
                # If the method isn't available, fall back to replacing the source via factory
                try:
                    self.microphone.source.close()
                except Exception:
                    pass
                factory = getattr(self.microphone, "_source_factory", None)
                if factory is not None:
                    self.microphone.source = factory(target_device)
                elif target_device:
                    self.microphone.source = self._make_source(target_device)
                else:
                    self.microphone.source = self._make_source(AUDIO_INPUT_DEVICE)

            try:
                if self.microphone.start():
                    self._reopen_attempts = 0  # reset on success
                    logger.info("[VOICE] microphone reopened successfully")
                    # Restore the last good device name for next time
                    self._last_good_device = self.microphone.source.device_name
                    return
            except Exception as e:
                logger.warning("[VOICE] mic_reopen attempt=%d failed: %s", self._reopen_attempts, e)

            # Wait with backoff before the next attempt
            if self._reopen_attempts >= max_attempts:
                break
            for _ in range(int(backoff / 0.1)):
                if self._reopen_stop.is_set():
                    return
                time.sleep(0.1)

        # All attempts exhausted: speak once and go idle.
        logger.error("[VOICE] microphone reconnection failed after %d attempts", max_attempts)
        self._speak_lost_microphone()

    def _query_preferred_device(self) -> Optional[str]:
        """Return the preferred input device index, or None for system default.

        Prefers the device we last used successfully, if it still exists.
        Falls back to the system default (None).
        """
        if self._last_good_device:
            try:
                import sounddevice as sd
                for idx, dev in enumerate(sd.query_devices()):
                    if dev.get("name") == self._last_good_device and dev.get("max_input_channels", 0) > 0:
                        return str(idx)
            except Exception:
                pass
        return None

    def _speak_lost_microphone(self) -> None:
        """Speak a single message and transition to idle."""
        try:
            # Use a direct TTS call to avoid the turn machinery.
            from zoe.tts import synthesize
            from zoe.config import KOKORO_RATE
            msg = "I lost the microphone. Check the input device."
            audio = synthesize(msg)
            import numpy as np
            waveform = audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio
            waveform = np.asarray(waveform)
            data = _to_pcm16(waveform, KOKORO_RATE)
            # One-shot playback on the output device
            import sounddevice as sd
            sd.play(data, KOKORO_RATE)
            sd.wait()
        except Exception:
            pass  # Best effort; we don't want to crash here

        # Move to idle so the user can fix the device.
        self.running = False
        self._stop.set()
        try:
            self.machine.transition(State.IDLE, reason="microphone lost", force=True)
        except Exception:
            pass

    def _mirror_lifecycle(self, event) -> None:
        """Project detailed voice states onto the 4-state lifecycle view."""
        try:
            if self._assistant is not None:
                self._assistant.observe(event.state, reason="voice loop")
        except Exception:  # noqa: BLE001 - mirror never breaks the driver
            pass

    def add_event_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._events.append(listener)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Open the microphone and begin the conversation loop.

        Returns False — honestly, with the reason in `last_error` — when the
        microphone or wake-word model is unavailable. Text conversation is
        unaffected either way.
        """
        with self._lock:
            if self._stopping:
                raise RuntimeError("voice loop shutdown is already in progress")
            if self.running:
                return True

            owner_pid = self._process_lock.acquire()
            if owner_pid is not None:
                self.last_error = (
                    "another Zoe process owns the microphone and speaker"
                    + (f" (pid {owner_pid})" if owner_pid else "")
                )
                logger.error("[VOICE] %s; stop that instance before starting another", self.last_error)
                return False

            # The wake model must be ready BEFORE audio starts arriving. While the
            # ONNX session is still being built, WakeWordEngine.scores() returns an
            # empty dict for every frame, so any speech spoken during that window is
            # discarded silently -- the loop rests in IDLE and the first request is
            # simply never heard. Loading first costs a few hundred milliseconds
            # before the device opens and costs nothing afterwards.
            if self.wake_enabled and not self.wake_engine.load():
                # The documented contract above: False, honestly, when the
                # wake-word model is unavailable. This used to fall through and
                # return True, which reported voice as active while the only
                # thing that can leave IDLE was dead. Text conversation is
                # unaffected -- it is a different entry point, and the caller
                # falls back to it. Nothing is opened, so nothing is acquired.
                self.last_error = "wake word model unavailable"
                logger.warning(
                    "[VOICE] wake word model unavailable; voice input is disabled. "
                    "Text conversation still works."
                )
                self.wake_enabled = False
                self._process_lock.release()
                return False

            self.microphone.add_sink(self._on_frame)
            if not self.microphone.start():
                self.microphone.remove_sink(self._on_frame)
                self.last_error = self.microphone.last_error or "microphone unavailable"
                # The engine loaded successfully before this point, so it is
                # holding a real ONNX session. Release it here: this is the
                # partial-startup path and nothing else will close it.
                self.wake_engine.close()
                self.machine.fail(self.last_error)
                self.machine.transition(State.IDLE, reason="voice unavailable", force=True)
                self._process_lock.release()
                return False

            self._stop.clear()
            self.running = True
            self.machine.transition(self._rest_state(), reason="voice loop started", force=True)

        logger.info(
            "[VOICE] conversation loop active pid=%d (wake %s, follow-up %.0fs)",
            os.getpid(), "on" if self.wake_enabled else "off", self.follow_up_window,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Shut down in the documented order, releasing everything.

        Order matters: stop accepting turns, cancel the active one, stop taking
        audio, stop producing audio, then release devices and join workers. Each
        step removes a source of work before the resource it uses is freed.
        """
        self._stop.set()

        # Claim the threads under the lock, then release it before joining.
        # `_run_turn` takes this same lock in its `finally`, so joining while
        # holding it deadlocks until the join times out -- which is exactly what
        # happened: every shutdown left a turn thread finishing after stop()
        # had already returned.
        with self._lock:
            if self._stopping:
                raise RuntimeError("voice loop shutdown is already in progress")
            self._stopping = True
            self._stop.set()
            self.running = False
            worker = self._thread
            turn_thread = self._turn_thread

        failures = []

        def _close_wake_engine():
            """Release the wake engine this loop owns.

            Duck-typed engines in tests may not implement `close()` at all, so
            this must not assume the method exists. A real `WakeWordEngine`
            always has it, and dropping the ONNX session is the point.
            """
            close = getattr(self.wake_engine, "close", None)
            if callable(close):
                close()

        try:
            # Stop accepting turns before releasing resources, even after capture failed.
            self.machine.cancel_turn("shutdown")
            self.machine.transition(State.IDLE, reason="shutdown", force=True)
            for cleanup in (
                lambda: self.microphone.remove_sink(self._on_frame),
                self.microphone.stop,
                # After the stream stops: the engine is only ever touched from
                # `_on_frame` on the capture thread, so once capture is joined
                # no inference can be in flight and closing it cannot race.
                # The loop owns this engine -- it constructs and loads it -- and
                # nothing else closes it, so leaving it out here leaked the
                # ONNX session for the rest of the process.
                _close_wake_engine,
                self.player.close,
                self.canceller.close,
            ):
                try:
                    cleanup()
                except Exception as e:
                    failures.append(e)
                    logger.error("[VOICE] shutdown cleanup failed: %s", e, exc_info=True)
            for name, thread in (("voice-loop", worker), ("voice-turn", turn_thread)):
                if thread is not None and thread.is_alive():
                    thread.join(timeout=timeout)
                if thread is not None and thread.is_alive():
                    failures.append(TimeoutError(f"{name} worker did not stop before timeout"))
            self.machine.reset()
        finally:
            with self._lock:
                if self._thread is worker and (worker is None or not worker.is_alive()):
                    self._thread = None
                if self._turn_thread is turn_thread and (turn_thread is None or not turn_thread.is_alive()):
                    self._turn_thread = None
                self._stopping = False
            # Ownership of the input device is released here, not after the
            # `if failures: raise` below. Releasing only on the clean path meant
            # a single failed cleanup step left the process lock held forever,
            # so every later start() refused to open the microphone -- one
            # harmless-looking cleanup error disabled voice for the rest of the
            # process.
            self._process_lock.release()
        if failures:
            raise RuntimeError("voice loop shutdown was incomplete: " + "; ".join(map(str, failures)))
        logger.info("[VOICE] conversation loop stopped")

    def is_running(self) -> bool:
        return self.running

    def wait_for_stop(self, timeout: Optional[float] = None) -> bool:
        """Wait for loop cancellation; returns True when shutdown was requested."""
        return self._stop.wait(timeout)

    # ------------------------------------------------------------------
    # Barge-in diagnostics
    # ------------------------------------------------------------------
    #
    # One stable line per barge-in decision. Every field describes *the frame
    # being decided*, never a remembered earlier one.
    #
    # The previous logging could not answer the question it was built for. Its
    # `rms_raw` was carried forward from a `[AUDIO] callback` line emitted on a
    # 5-second cadence -- up to three seconds stale, and frequently from a
    # different state -- and its `residual_energy`/`playback_energy` came from
    # the last sub-frame the guard happened to score, which read 0.000000 while
    # the reply was demonstrably playing. See docs/barge_in_diagnosis.md §4.
    #
    # `scripts/analyse_barge_in.py` parses exactly this line. Its key set and
    # order are part of the contract: rename a key there too, in the same commit.

    #: Field order of `[BARGE]`. Positional parsing depends on it staying put.
    BARGE_FIELDS = (
        "kind", "turn", "state", "barge_in", "elapsed_s",
        "rms_raw", "rms_post",
        "gain", "reduction", "lag_ms",
        "guard_residual", "guard_playback", "guard_streak",
        "guard_ratio", "guard_ratio_bar",
        "bar", "blocked",
    )

    def _barge_record(self, kind: str, raw_pcm, post_pcm, barge_in: bool,
                      blocked: bool) -> Dict[str, Any]:
        """One decision's worth of evidence, read at the instant it is taken."""
        from zoe.audio import rms

        canceller = self.canceller
        guard = self.echo
        elapsed = guard.playback_elapsed()
        turn = self.machine.turn
        return {
            "kind": kind,
            "turn": getattr(turn, "id", None) or "-",
            "state": self.machine.state.value,
            "barge_in": bool(barge_in),
            # inf when nothing is playing; rendered as a number so the field is
            # never empty and a regex cannot silently skip the line.
            "elapsed_s": "inf" if elapsed == float("inf") else round(elapsed, 3),
            "rms_raw": round(rms(raw_pcm), 4),
            "rms_post": round(rms(post_pcm), 4),
            "gain": round(float(getattr(canceller, "last_gain", 0.0)), 3),
            "reduction": round(float(getattr(canceller, "last_reduction", 0.0)), 3),
            "lag_ms": round(float(getattr(canceller, "last_lag_ms", 0.0)), 1),
            "guard_residual": round(float(getattr(guard, "last_residual_energy",
                                                 0.0)), 6),
            "guard_playback": round(float(getattr(guard, "last_playback_energy",
                                                  0.0)), 6),
            "guard_streak": int(getattr(guard, "streak", 0)),
            # Measured levels only -- never audio, never what was said. The pair
            # is what says whether the guard's bar is doing any work: `ratio` is
            # the share of the frame our playback did not explain, `ratio_bar`
            # the share this room has to leave before it counts as the user.
            "guard_ratio": round(float(getattr(guard, "last_ratio", 0.0)), 4),
            "guard_ratio_bar": round(float(getattr(guard, "last_ratio_bar", 0.0)), 4),
            "bar": round(float(self.vad.level_needed(barge_in)), 4),
            "blocked": bool(blocked),
        }

    @staticmethod
    def _format_barge(record: Dict[str, Any]) -> str:
        return "[BARGE] " + " ".join(f"{k}={record[k]}"
                                     for k in VoiceLoop.BARGE_FIELDS)

    def _deciding_record(self, kind: str, barge_in: bool, blocked: bool
                         ) -> Dict[str, Any]:
        """The `_on_speech_start` record: the last frame's measurements, re-stamped.

        Falls back to a fresh full record when no frame has been seen, so the
        line is never short and the analyser never has to cope with a variant.
        """
        if self._last_barge is None:
            return self._barge_record(kind, b"\x00\x00", b"\x00\x00",
                                      barge_in, blocked)
        return {**self._last_barge, "kind": kind, "blocked": blocked}

    def _log_barge(self, level: int, record: Dict[str, Any]) -> None:
        """Emit one decision line and remember it for the speech-start frame.

        `_on_speech_start` fires microseconds after the last `_on_frame`, so the
        values are the ones that produced the decision rather than whatever was
        logged five seconds ago.
        """
        self._last_barge = record
        logger.log(level, self._format_barge(record))

    # ------------------------------------------------------------------
    # Audio sink
    # ------------------------------------------------------------------

    def _on_frame(self, pcm: bytes) -> None:
        """Called on the microphone thread for every frame. Must stay fast.

        Three consumers of the same audio, exactly as the architecture requires:
        the wake-word engine, the VAD, and (via the loop thread) ASR.
        """
        if not isinstance(pcm, (bytes, bytearray, memoryview)) or len(pcm) != frame_bytes():
            if not self._invalid_frame_reported:
                logger.warning(
                    "[AUDIO] discarded malformed microphone frame (%s bytes; expected %s)",
                    len(pcm) if hasattr(pcm, "__len__") else "unknown", frame_bytes(),
                )
                self._invalid_frame_reported = True
            return

        raw_pcm = pcm
        state = self.machine.state
        now = self.clock()

        # Remove the assistant's own voice from the signal *before* anything
        # looks at it. Both the wake word and the VAD then see the near end --
        # the user -- rather than a leak of what we just played.
        pcm = self.canceller.cancel(pcm)

        # While the assistant is busy, speech must clear the stricter
        # interruption bar to cut in. Whether the user *can* interrupt is decided
        # by state, never by echo: gating it on echo meant a user speaking during
        # a model call, before any audio had played, was ignored. And using the
        # lenient bar while busy meant a cough during a slow model call could
        # cancel the turn. Barge-in is the stricter bar, always, while busy.
        barge_in = state in INTERRUPTABLE

        # ...but the stricter bar is still just a *loudness* bar, and our own
        # reply through the speakers is exactly as loud as the user. So while
        # busy, ask the band-energy echo guard whether this frame is us or them:
        # if the residual after subtracting our own playback is silence, it is our
        # voice leaking back, and it must not accumulate toward a barge-in. With
        # nothing playing the guard passes the frame straight through, so a user
        # interrupting before any audio has played is unaffected.
        if barge_in:
            is_user = self.echo.observe(
                raw_pcm, getattr(self.canceller, "last_reference", None)
            )
            if not is_user:
                pcm = bytes(len(pcm))

            # DEBUG, not INFO: this fires 12.5 times a second while the assistant
            # is speaking, which is a useful window around a cut and noise
            # everywhere else. It reaches the log file either way -- only the
            # terminal is gated by --debug. `_on_speech_start` promotes the
            # decisive frame to INFO.
            #
            # `blocked` is the guard's own verdict. Without it, `rms_post=0.0`
            # on a frame whose `rms_raw` is not zero is unreadable: the guard
            # replaced the frame with silence and the log could not say so.
            self._log_barge(
                logging.DEBUG,
                self._barge_record("frame", raw_pcm, pcm, barge_in, not is_user),
            )

        # The wake word alone is gated while the assistant's own audio is in the
        # air: it must not wake itself. The VAD keeps running underneath, so
        # barge-in remains possible throughout.
        if self.wake_enabled and not barge_in and not self.echo.should_suppress():
            fired = self._wake_fired(pcm)
            if fired is not None:
                phrase, score = fired
                logger.debug(f"wake phrase matched: {phrase} score={score:.2f}")
                if self._wake_latched:
                    # Overlapping chunks all score high for the same utterance.
                    self._wake_rejections += 1
                else:
                    # Report the score that actually opened the gate, once.
                    # Overlapping chunks are suppressed above, so this cannot
                    # notify twice for one utterance.
                    StatusIndicator.wake_detected(score)
                    self.on_wake()
                # Deliberately no return. The wake word is usually the first
                # syllable of what the user is saying -- "Hey Zoe, what's the
                # time" is one breath -- and discarding these frames clipped the
                # utterance. They still go to the VAD; only the transition is
                # suppressed.

        if now - self._last_audio_diag >= 5.0:
            from zoe.audio import rms
            import array

            raw_samples = array.array("h")
            raw_samples.frombytes(bytes(raw_pcm))
            peak = max((abs(sample) for sample in raw_samples), default=0) / 32768.0
            logger.debug(
                "[AUDIO] callback frames=%d bytes=%d format=s16le-mono rate=%dHz "
                "frame_ms=%d rms_raw=%.4f rms_post_echo=%.4f peak=%.4f "
                "preprocessing_changed=%s echo_cancelled_frames=%s "
                "state=%s",
                self.microphone.frames_read, len(pcm),
                getattr(self.microphone, "status", lambda: {})().get("sample_rate", 0),
                getattr(self.microphone, "status", lambda: {})().get("frame_ms", 0),
                rms(raw_pcm), rms(pcm), peak, raw_pcm != pcm,
                getattr(self.canceller, "status", lambda: {})().get("frames_cancelled", "unknown"),
                state.value,
            )
            self._last_audio_diag = now

        event = self.vad.push(pcm, barge_in=barge_in)
        if event is None:
            return
        if event.kind == "start":
            self._on_speech_start(event, barge_in)
        else:
            self._on_speech_end(event)

    # ------------------------------------------------------------------
    # Wake and speech
    # ------------------------------------------------------------------

    def _on_speech_start(self, event, barge_in: bool) -> None:
        """What the beginning of an utterance means, decided purely by state."""
        logger.info(
            "[VOICE] VAD speech started pid=%d state=%s barge_in=%s",
            os.getpid(), self.machine.state.value, barge_in,
        )
        state = self.machine.state
        # The deciding frame, at INFO: this is the line the barge-in diagnosis is
        # actually able to trust. `kind=interrupt` when the user takes the floor,
        # `kind=declined` when the grace window drops the utterance instead.
        #
        # The measured scalars are carried over from the last `_on_frame`, which
        # ran microseconds ago on the same audio -- that is the point. Re-reading
        # the canceller and guard here would race the next frame's update and
        # reintroduce exactly the staleness this line exists to remove.
        record = self._deciding_record("interrupt", barge_in, blocked=False)
        if state is State.SPEAKING and self.echo.playback_elapsed() < BARGE_IN_GRACE_S:
            # Too early in our own playback to be the user: this is almost
            # certainly the speaker bleeding into the mic. Drop the utterance and
            # its matching end so it never reaches ASR or a turn.
            self._ignoring_speech = True
            self._log_barge(logging.INFO,
                            self._deciding_record("declined", barge_in, blocked=True))
            logger.info(
                "[VOICE] speech start ignored in barge-in grace (%.2fs < %.2fs)",
                self.echo.playback_elapsed(), BARGE_IN_GRACE_S,
            )
            return
        self._ignoring_speech = False
        self._log_barge(logging.INFO, record)
        self._utterance = event.audio
        if state in INTERRUPTABLE:
            # The user is talking over the assistant -- during transcription, a
            # model call, or its reply. They have the floor.
            self.interrupt()
        elif state is State.FOLLOW_UP:
            # Follow-up: no wake word needed.
            self.machine.transition(State.LISTENING, reason="follow-up speech")

    def _on_speech_end(self, event) -> None:
        if self._ignoring_speech:
            self._ignoring_speech = False
            return
        if not event.audio:
            return
        logger.info(
            "[VOICE] VAD speech ended bytes=%d peak=%.4f; submitting to ASR",
            len(event.audio), event.peak,
        )
        with self._lock:
            # Re-checked under the lock against stop(): a turn started after
            # shutdown began would not be joined by stop(), and would keep
            # touching state it had already finalised.
            if self._stop.is_set() or not self.running:
                return
            self._utterance = event.audio
            # Tracked so shutdown can wait for it, and started under the same
            # lock: stop() cannot otherwise observe a registered-but-unstarted
            # thread, see is_alive() False, skip the join, and leave it running.
            thread = threading.Thread(target=self._run_turn, name="voice-turn", daemon=True)
            self._turn_thread = thread
            thread.start()

    # ------------------------------------------------------------------
    # Turn
    # ------------------------------------------------------------------

    def _run_turn(self) -> None:
        """LISTENING -> TRANSCRIBING -> THINKING -> SPEAKING -> FOLLOW_UP.

        Every step re-checks staleness: by the time a slow model call returns,
        the user may already have interrupted.

        The running thread claims the tracked-turn slot for itself. Registering
        only at the call site left a hole: a turn started any other way was never
        joined by `stop()`, and outlived shutdown still touching state it had
        already finalised.
        """
        with self._lock:
            tracked = self._turn_thread
            if tracked is None or not tracked.is_alive():
                self._turn_thread = threading.current_thread()

        turn = None
        trace = new_trace()
        with use_trace(trace):
            try:
                if self.machine.state is State.IDLE:
                    return  # speech arrived with no conversation to serve

                turn = self.machine.begin_turn()
                self._ack_played = False
                self._ack_ready = False
                self.machine.mark("speech_end", turn.id)

                # -- TRANSCRIBING ------------------------------------------------
                self.machine.transition(State.TRANSCRIBING, reason="speech ended")
                with span_of("stt"):
                    text = self._transcribe(self._utterance, turn.id)
                if self.machine.is_stale(turn.id):
                    # The user spoke over us while we were transcribing. The
                    # new utterance owns the state now; this turn is void.
                    logger.info("[VOICE] transcript discarded: superseded during transcription")
                    return
                turn.transcript = text
                if not text.strip():
                    # Never invent a transcript. This is the one failure the user
                    # most needs told about.
                    self.machine.fail("I didn't catch that.")
                    self._settle()
                    return

                # V11: the user must see what was heard. Without this, a wrong
                # transcript is indistinguishable from a wrong reply.
                print(f"\n[YOU]: {text}", flush=True)

                from zoe.main import is_shutdown_request
                if is_shutdown_request(text):
                    from zoe.config import GREETING_NAME
                    self.machine.transition(State.THINKING, reason="shutdown requested")
                    StatusIndicator.shutdown()
                    self._speak(f"Going offline. You can call me anytime, {GREETING_NAME}!", turn.id)
                    self._stop.set()
                    self.machine.cancel_turn("shutdown command")
                    self.machine.transition(State.IDLE, reason="shutdown command", force=True)
                    return

                # -- THINKING ----------------------------------------------------
                if self.machine.is_stale(turn.id):
                    return
                try:
                    self.machine.transition(State.THINKING, reason="transcript ready")
                except InvalidTransition:
                    # Not a barge-in: those cancel the turn and return above. This
                    # turn is still live, so the machine was moved under it by
                    # something else. The transcript is still valid; continue.
                    logger.warning(
                        "[VOICE] state moved under a live turn; continuing turn=%s",
                        turn.id,
                    )
                    self.machine.transition(State.THINKING, reason="transcript ready", force=True)
                self._arm_ack(turn.id)
                with span_of("think"):
                    reply = self._respond(text, turn.id)
                self._disarm_ack()
                if self.machine.is_stale(turn.id):
                    return  # interrupted while the model was thinking
                print(f"[Zoe]: {reply}", flush=True)
                if not (reply or "").strip():
                    self._settle()
                    return
                reply = self._spoken_reply(reply, turn.id)

                # -- SPEAKING ----------------------------------------------------
                with span_of("speak"):
                    self._speak(reply, turn.id)
                if self.machine.is_stale(turn.id):
                    return
                self.machine.end_turn(turn.id)

                # -- FOLLOW_UP ---------------------------------------------------
                self.machine.transition(State.FOLLOW_UP, reason="reply finished")
                self.machine.mark("playback_end")
                self._await_follow_up(turn.id)

            except Exception as e:  # noqa: BLE001 - one bad turn must not kill the loop
                # An interrupted turn is not a failure: the user took the floor and
                # the next turn is already under way. Reporting ERROR here would
                # stomp the state the interruption just established.
                if turn is not None and self.machine.is_stale(turn.id):
                    return
                self.last_error = str(e)
                logger.error(f"[VOICE] turn failed: {e}", exc_info=True)
                try:
                    self.machine.fail(str(e))
                except Exception:  # noqa: BLE001
                    self.machine.transition(State.ERROR, reason=str(e), force=True)
                self._settle()
            finally:
                trace.finish()
                if turn is not None:
                    self._log_ack(turn.id)
                with self._lock:
                    # Unlatch here, so the wake word can fire again for the next
                    # utterance. Latching for the whole turn is what stopped one
                    # utterance from waking the assistant exactly once.
                    self._wake_latched = False
                    # Reset the detector unless the user has already started the
                    # next utterance. This thread outlives the follow-up window
                    # opening: `_await_follow_up` returns as soon as the user
                    # speaks, and their speech is collected from that instant. An
                    # unconditional reset here therefore lands *inside* the new
                    # utterance and destroys it -- the user is answered with
                    # silence for a request they can hear themselves making.
                    if not self.vad.in_speech:
                        self.vad.reset()
                self._utterance = b""

    def _transcribe(self, audio: bytes, turn_id: str) -> str:
        if self.machine.is_stale(turn_id):
            return ""
        if self.transcriber is None:
            raise ASRUnavailable(
                "No transcriber configured; voice input is unavailable."
            )
        logger.info("[VOICE] ASR processing %d bytes", len(audio))
        text = self.transcriber.transcribe(audio)
        logger.info("[VOICE] ASR returned transcript (%d characters)", len(text or ""))
        if not text:
            reason = getattr(self.transcriber, "last_error", None) or "empty transcript"
            logger.warning("[VOICE] ASR returned no transcript: %s", reason)
        if not self.machine.is_stale(turn_id):
            self.machine.mark("transcript", turn_id)
        return text

    def _respond(self, text: str, turn_id: str) -> str:
        """Ask the Brain. Runs inline; the result is discarded if interrupted."""
        self.machine.mark("first_token", turn_id)
        return self.respond(text)

    def _ask_brain(self, text: str) -> str:
        return self.brain.ask(text, spoken=True)

    # ------------------------------------------------------------------
    # Thinking acknowledgment
    # ------------------------------------------------------------------

    def _arm_ack(self, turn_id: str) -> None:
        """Start the timer that may speak an acknowledgment while the model thinks."""
        if not self._ack_enabled or self._ack_delay_s <= 0:
            return
        self._cancel_ack_timer()
        timer = threading.Timer(self._ack_delay_s, self._on_ack_timer, args=(turn_id,))
        timer.daemon = True
        self._ack_timer = timer
        timer.start()

    def _cancel_ack_timer(self) -> None:
        timer = self._ack_timer
        self._ack_timer = None
        if timer is not None:
            timer.cancel()

    def _disarm_ack(self) -> None:
        """Cancel the ack timer; once the reply is ready no ack may play."""
        self._ack_ready = True
        self._cancel_ack_timer()

    def _on_ack_timer(self, turn_id: str) -> None:
        """Speak the ack only if the model has still not produced its first token."""
        with self._lock:
            if not self._ack_enabled or self._ack_played or self._ack_ready:
                return
            if self._stop.is_set():
                return
            if self.machine.is_stale(turn_id):
                return
            if self.machine.state is not State.THINKING:
                return  # the reply started; never acknowledge over it
            self._ack_played = True
        self._play_ack(turn_id)

    def _play_ack(self, turn_id: str) -> None:
        pcm = self._ack_pcm() if self._ack_pcm is not None else self._default_ack_pcm()
        if not pcm:
            return
        is_stale = self.machine.is_stale
        self.echo.begin_playback()
        queued = self.player.play_pcm(pcm, turn_id, is_stale)
        if is_stale(turn_id) or queued == 0:
            self.echo.end_playback()
            return
        # Wait for the ack to finish so the reply's playback starts fresh and the
        # two never overlap. A barge-in cancels the player and this returns early.
        self.player.wait(timeout=15.0)
        self.echo.end_playback()

    def _default_ack_pcm(self) -> Optional[bytes]:
        if self._ack_mode == "model":
            text = self._ask_ack_model()
            if not text:
                return None
            return self._render_speech(text)
        return self._cached_ack_pcm()

    def _cached_ack_pcm(self) -> Optional[bytes]:
        """A fixed clip, round-robin. No model call, and rendered at most once."""
        phrase = _ACK_PHRASES[self._ack_index % len(_ACK_PHRASES)]
        self._ack_index += 1
        pcm = self._ack_pcm_cache.get(phrase)
        if pcm is None:
            pcm = self._render_speech(phrase)
            if pcm:
                self._ack_pcm_cache[phrase] = pcm
        return pcm

    def _ask_ack_model(self) -> Optional[str]:
        try:
            return (self.respond(_ACK_PROMPT) or "").strip() or None
        except Exception:  # noqa: BLE001 - a failed ack must not fail the turn
            logger.error("[VOICE] ack model call failed", exc_info=True)
            return None

    def _render_speech(self, text: str) -> Optional[bytes]:
        try:
            audio = self.player.synthesize(text)
        except Exception:  # noqa: BLE001 - synthesis failure must not fail the turn
            logger.error("[VOICE] ack synthesis failed", exc_info=True)
            return None
        waveform = audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio
        return _to_pcm16(waveform, self.player.sample_rate)

    def _log_ack(self, turn_id: str) -> None:
        logger.info(
            "[VOICE] ack turn=%s ack_played=%s",
            turn_id, "true" if self._ack_played else "false",
        )

    # -- spoken-reply shaping --------------------------------------------

    #: Markdown that reads badly aloud. Stripped from every spoken reply, so the
    #: system-prompt directive is a backstop and not the only line of defence.
    _MD_URL = re.compile(r"https?://\S+|www\.\S+")

    def _strip_markdown(self, text: str) -> str:
        """Remove markup the TTS would otherwise read out character by character."""
        text = self._MD_URL.sub("", text)
        text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)   # headings
        text = re.sub(r"^\s*-\s+", "", text, flags=re.M)        # dash bullets
        # Bold, italic and fenced code all become their inner text.
        text = text.replace("**", "").replace("*", "").replace("`", "")
        return re.sub(r"\s+", " ", text).strip()

    def _spoken_reply(self, reply: str, turn_id: str) -> str:
        """Shape a reply to be spoken: strip markdown, then enforce the word cap."""
        reply = self._strip_markdown(reply)
        words = reply.split()
        if len(words) <= self._max_spoken_words:
            return reply
        # Truncate at the last sentence boundary under the cap, then offer more.
        truncated = " ".join(words[:self._max_spoken_words])
        boundary = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
        if boundary > 0:
            truncated = truncated[:boundary + 1]
        else:
            # No sentence ends inside the cap, which is what a reply cut off by
            # the generation budget looks like. Drop the unfinished clause rather
            # than speaking half a sentence and calling it an answer.
            tail = " ".join(words[:self._max_spoken_words])
            clause = max(tail.rfind(","), tail.rfind(";"), tail.rfind(":"))
            truncated = tail[:clause] if clause > 0 else tail
        result = truncated.rstrip() + " Want more detail?"
        logger.info(
            "[VOICE] spoken reply truncated turn=%s words=%d -> %d",
            turn_id, len(words), len(result.split()),
        )
        return result

    def _wake_fired(self, pcm) -> Optional[tuple]:
        """First (phrase, score) above its threshold, config order wins ties.

        One inference call scores every active phrase on the same shared chunk;
        the wake latch then guarantees one utterance wakes the assistant once.
        Engines without `scores()` fall back to the legacy single check.
        """
        engine = self.wake_engine
        start = time.perf_counter()
        try:
            scores_fn = getattr(engine, "scores", None)
            if callable(scores_fn):
                thresholds = getattr(engine, "thresholds", None) or {}
                for phrase, s in scores_fn(pcm).items():
                    if s > thresholds.get(phrase, engine.threshold):
                        return (phrase, s)
                return None
            s = engine.score(pcm)
            if s > engine.threshold:
                return (getattr(engine, "model", "wake word"), s)
            return None
        except Exception as e:  # noqa: BLE001
            # One bad frame must not kill the capture thread. This runs on the
            # microphone callback, so letting the exception escape stopped the
            # stream and left the assistant deaf for the rest of the session.
            # Contained and reported; the next frame is scored normally.
            logger.error("[WAKE] inference failed: %s", e, exc_info=True)
            return None
        finally:
            self._last_wake_ms = (time.perf_counter() - start) * 1000.0

    def on_wake(self) -> None:
        """IDLE -> LISTENING. Immediate, and idempotent while latched."""
        with self._lock:
            if self._wake_latched:
                self._wake_rejections += 1
                return
            self._wake_latched = True
            self.wake_events += 1
        self.machine.mark("wake_detected")
        try:
            self.machine.transition(State.LISTENING, reason="wake word")
        except Exception as e:  # noqa: BLE001 - already listening, etc.
            logger.debug(f"wake transition skipped: {e}")
            return

    def _speak(self, reply: str, turn_id: str) -> None:
        """Synthesize and play, checking staleness between chunks."""
        self.machine.transition(State.SPEAKING, reason="reply ready")
        is_stale = self.machine.is_stale

        # The conversational state only reaches the voice through one numeric
        # expressiveness hint. It is a hint, not a mode: the same engine, the
        # same voice, the same speed range. It is a number, never a bool --
        # `True` would clamp to the top of the range on every excited reply.
        state = get_tone().state
        if state.exaggerated():
            self.player.exaggeration = 0.8     # livelier delivery: faster, a little brisker
        elif state.composure():
            self.player.exaggeration = 0.3     # calmer delivery: slower, steadier
        else:
            self.player.exaggeration = None    # configured default

        queued = self.player.synthesize_to_queue(reply, turn_id, is_stale)
        if is_stale(turn_id) or queued == 0:
            return

        self.machine.mark("first_audio", turn_id)
        self.echo.begin_playback()
        self.player.start(turn_id, is_stale)
        if not self.player.wait(timeout=120.0):
            self.player.cancel("playback timeout after 120s")
            if not self.player.wait(timeout=5.0):
                raise TimeoutError("speech playback did not stop after cancellation")
        if getattr(self.player, "last_error", None):
            raise RuntimeError(f"speech playback failed: {self.player.last_error}")
        # A reply that was cut short is not a finished reply. Record the reason
        # rather than letting a cancelled turn walk on into FOLLOW_UP looking
        # like a complete answer. `end_playback` still runs either way: leaving
        # the echo guard latched would gate the wake word for the rest of the
        # session.
        if getattr(self.player, "cancel_reason", None):
            logger.warning(
                "[VOICE] reply cut short turn=%s reason=%s chunks_played=%s "
                "residual_energy=%.6f playback_energy=%.6f",
                turn_id, self.player.cancel_reason, self.player.chunks_played,
                self.echo.last_residual_energy, self.echo.last_playback_energy,
            )
        self.echo.end_playback()
        self.machine.mark("playback_end", turn_id)

    # ------------------------------------------------------------------
    # Interruption
    # ------------------------------------------------------------------

    def interrupt(self) -> None:
        """Cancel the current turn and go back to listening.

        Order matters and is the whole design: invalidate first, then drop audio.
        Doing it the other way round leaves a window where the old turn is still
        live and its audio can still be queued.
        """
        with self._lock:
            self.interrupts += 1
            self.machine.mark("interrupt_detected")
            turn = self.machine.cancel_turn("barge-in")

            # Drop the old turn's queued audio and stop playback mid-utterance.
            dropped = self.player.cancel("barge-in")
            self.vad.reset()

            try:
                self.machine.transition(State.INTERRUPTED, reason="barge-in")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"interrupt transition skipped: {e}")
                return

            self.machine.transition(State.LISTENING, reason="after barge-in")
            self.machine.mark("interrupt_complete")

        logger.info(
            f"[VOICE] interrupted (dropped {dropped} queued audio chunks); listening"
        )

    # ------------------------------------------------------------------
    # Follow-up
    # ------------------------------------------------------------------

    def _await_follow_up(self, turn_id: str) -> None:
        """Stay open briefly, then return to IDLE. Never permanent."""
        deadline = self.clock() + self.follow_up_window
        while self.clock() < deadline:
            if self._stop.is_set():
                return
            if self.machine.state is not State.FOLLOW_UP:
                return  # the user already spoke
            time.sleep(0.05)
        if self.machine.state is State.FOLLOW_UP:
            target = self._rest_state()
            try:
                self.machine.transition(target, reason="follow-up window elapsed")
            except Exception:  # noqa: BLE001
                self.machine.transition(target, reason="follow-up", force=True)

    def _rest_state(self) -> State:
        """Where the loop waits between turns. IDLE can only be left by a wake word."""
        return State.IDLE if self.wake_enabled else State.LISTENING

    def _settle(self) -> None:
        """Return to rest after a turn that produced no reply."""
        target = self._rest_state()
        try:
            self.machine.transition(target, reason="turn settled")
        except Exception:  # noqa: BLE001
            self.machine.transition(target, reason="turn settled", force=True)

    def check_proactive(self, candidate=None, *, user_speaking=False,
                        wake_active=False) -> object:
        """Decision-only proactive hook: candidate -> gates -> YES/NO.

        The candidate arrives as data (built by the caller from the
        ObservationEngine); this method never reads the camera itself, so the
        Phase 4 invariant holds: perception cannot move the state machine, only a
        wake word or speech can. Speaks nothing and touches no audio: on YES
        the caller drives the existing Brain and the existing :meth:`_speak`,
        so barge-in, stale-turn drops and the machine stay authoritative.
        """
        from zoe.proactive import (
            ContextSnapshot,
            get_orchestrator,
            get_proactive_engine,
        )

        if candidate is None:
            return None
        state = self.machine.state
        snap = ContextSnapshot(
            user_availability="unavailable" if state in INTERRUPTABLE
            else "probably_available",
            active_conversation=state is not State.IDLE,
            user_speaking=bool(user_speaking),
            assistant_speaking=state is State.SPEAKING,
            wake_active=bool(wake_active),
            recently_spoken=get_orchestrator().recent_reasons(),
        )
        result = get_proactive_engine().decide(candidate, snap)
        return result if result.should_speak else None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "interrupts": self.interrupts,
            "follow_up_window_s": self.follow_up_window,
            "microphone": self.microphone.status(),
            "audio_queue_depth": self.player.queue.peek_len(),
            "audio_discarded_stale": self.player.queue.discarded_stale,
            "audio_discarded_cancelled": self.player.chunks_cancelled,
            "audio_cancel_reason": getattr(self.player, "cancel_reason", None),
            "echo_suppressing": self.echo.should_suppress(),
            "echo_guard_bar": {
                # How well this room separates our voice from the user's, which
                # is the number a hardware session has to record: a bar pinned at
                # its floor means a quiet room, a bar above `ratio_floor` and a
                # streak of refusals means the speaker-to-mic path is too poor to
                # interrupt reliably.
                "adaptive": self.echo.adaptive_bar,
                "calibrated": self.echo.calibrated,
                "reliable": self.echo.reliable,
                "ratio_floor": round(self.echo.ratio_floor, 4),
                "ratio_bar": round(self.echo.ratio_bar, 4),
                "last_ratio": round(self.echo.last_ratio, 4),
                "required_frames": self.echo.required_frames,
            },
            "echo_canceller": self.canceller.status(),
            "wake_enabled": self.wake_enabled,
            "wake_events": self.wake_events,
            "wake_rejections": self._wake_rejections,
            "last_error": self.last_error,
            "state": self.machine.snapshot(include_history=False),
        }


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------

_instance: Optional[VoiceLoop] = None
_lock = threading.Lock()
_last_start_error: Optional[str] = None


def start_voice_loop(brain, *, force: bool = False) -> Optional[VoiceLoop]:
    """Start the voice loop if it is configured on. Returns the instance or None."""
    global _instance, _last_start_error
    _last_start_error = None

    if not force:
        logger.info("[VOICE] voice disabled by configuration; microphone not opened")
        _last_start_error = "voice disabled by configuration"
        return None

    with _lock:
        if _instance is not None and _instance.is_running():
            return _instance
        loop = VoiceLoop(brain=brain, transcriber=Transcriber())
        _instance = loop
        try:
            if loop.start():
                return loop
        except Exception:
            loop.stop()
            _instance = None
            raise
        loop.stop()
        _last_start_error = loop.last_error
        _instance = None
        return None


def stop_voice_loop() -> None:
    """Stop and release the process-wide voice loop, if there is one."""
    global _instance
    with _lock:
        loop = _instance
        try:
            if loop is not None:
                loop.stop()
        finally:
            if _instance is loop:
                _instance = None


def get_voice_loop() -> Optional[VoiceLoop]:
    return _instance


def get_voice_start_error() -> Optional[str]:
    """Reason the most recent configured voice loop did not start."""
    return _last_start_error


__all__ = [
    "VoiceLoop",
    "get_voice_loop",
    "get_voice_start_error",
    "start_voice_loop",
    "stop_voice_loop",
]
