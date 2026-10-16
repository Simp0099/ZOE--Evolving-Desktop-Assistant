#!/usr/bin/env python3
"""Zoe — Unified Startup Entrypoint (Phase 9).

Initializes logging, loads configs, checks environment keys, and starts the
assistant.

There are two interactive modes, selected by `--text`. There is no third mode
for the wake word: without `--text` this always enters `run_voice_mode`, and
`wake_word.enabled` in config.yaml decides whether that loop rests in IDLE
(waiting for the wake phrase) or LISTENING (taking the next utterance).
`--no-wake` is parsed for compatibility and has no effect.

Usage:
    python3 start.py              # Voice mode (see wake_word.enabled above)
    python3 start.py --text       # Text mode, no microphone
    python3 start.py --status     # Print system status and exit
    python3 start.py --test       # Run tests/test_integration.py standalone
    python3 start.py --debug      # Console diagnostics on top of the mode
    python3 start.py --no-wake    # Deprecated alias; does nothing
"""

import sys
import os
import argparse
import platform
from pathlib import Path

# Ensure project root is on Python path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


#: Engines the speech layer can actually synthesize with. Chatterbox is absent
#: because its implementation was removed from `zoe.speech` in ddbff2e: the
#: package may still be installed, but nothing in the project can drive it, so
#: reporting it as available would be the same class of lie as the warning this
#: replaces.
_SUPPORTED_TTS_ENGINES = ("kokoro", "pyttsx3")


def _is_installed(module: str) -> bool:
    """Whether `module` is importable, without importing it.

    `find_spec` asks the import machinery to *locate* the package, which is the
    question a startup check has. A real `import kokoro` pulls in torch and
    costs about five seconds before the assistant has said anything, and it is
    exactly the "unnecessary initialization" this check must not do. It is also
    what makes the check safe: probing never mutates state or loads a model.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def check_voice_engine() -> bool:
    """Report the health of the *configured* voice engine.

    Deliberately engine-aware. A failure in an engine that is not selected says
    nothing about the one that is, and reporting it as though it did was the bug:
    startup printed a Chatterbox import error while Kokoro was selected and
    initializing perfectly.

    Checks are availability, not a warm-up. Loading a model here would make
    startup pay the full cost before the first request and would turn a slow
    optional engine into a startup failure.

    Returns True when the configured engine looks usable.
    """
    try:
        from zoe.config import KOKORO_LANG, KOKORO_VOICE, TTS_ENGINE
    except Exception as e:  # noqa: BLE001 -- config itself is broken; say so
        print(f"[Check] Voice engine: configuration unreadable ({e})")
        return False

    engine = (TTS_ENGINE or "").strip().lower()
    if engine not in _SUPPORTED_TTS_ENGINES:
        print(f"[Check] Voice engine: UNSUPPORTED '{TTS_ENGINE}'. "
              f"Supported: {', '.join(_SUPPORTED_TTS_ENGINES)}. "
              f"Speech output falls back to console text.")
        return False

    if engine == "kokoro":
        if not _is_installed("kokoro"):
            print("[Check] Voice engine: kokoro is selected but not installed. "
                  "Run: pip install kokoro soundfile")
            return False
        print(f"[Check] Voice engine: kokoro (voice={KOKORO_VOICE}, lang={KOKORO_LANG})")
        return True

    if engine == "pyttsx3":
        if not _is_installed("pyttsx3"):
            print("[Check] Voice engine: pyttsx3 is selected but not installed. "
                  "Run: pip install pyttsx3")
            return False
        print("[Check] Voice engine: pyttsx3 (platform TTS)")
        return True

    return False


def check_environment():
    """Verify that at least one enabled model has its provider credential."""
    from zoe.config import LLM_MODEL, ASSISTANT_NAME
    from zoe.logger import logger

    print(f"\n{'='*60}")
    print(f"  Zoe — System Startup")
    print(f"{'='*60}\n")

    # Check Python version
    py_version = platform.python_version()
    print(f"[Check] Python version: {py_version}")
    if tuple(map(int, py_version.split(".")[:2])) < (3, 10):
        print(f"  WARNING: Python 3.10+ recommended")
    else:
        print(f"  OK")

    # A .env file is convenient, not required when credentials are exported by
    # the shell or another secrets manager.
    env_file = PROJECT_ROOT / ".env"
    print(f"\n[Check] .env file: {'Found' if env_file.exists() else 'not present (environment variables are also supported)'}")
    try:
        from zoe.model_layer import ModelLayer
        layer = ModelLayer.from_config()
        usable = [
            model.key for model in layer.registry.enabled()
            if (provider := layer.providers.get(model.provider))
            and provider.is_configured()
        ]
    except Exception as e:
        print(f"  ERROR: Could not validate configured providers: {e}")
        return False
    print(f"[Check] Configured models: {len(usable)} usable")
    if not usable:
        print("  ERROR: No enabled model has usable provider credentials. "
              "Configure a key for an enabled provider in .env or the environment.")
        return False

    # Check config.yaml
    config_file = PROJECT_ROOT / "config.yaml"
    print(f"[Check] config.yaml: {'Found' if config_file.exists() else 'NOT FOUND'}")

    # Camera state, always shown. OpenCV is only required when the webcam is
    # actually enabled, so a missing install is not a startup error.
    try:
        from zoe.config import VISION_ENABLED, WEBCAM_ENABLED
        if VISION_ENABLED and WEBCAM_ENABLED:
            import cv2
            print(f"[Check] Webcam perception: ON, OpenCV {cv2.__version__}")
        else:
            print(f"[Check] Webcam perception: off by config (camera not opened)")
    except ImportError:
        print(f"[Check] Webcam perception: OpenCV not installed (needed only if enabled)")

    # Voice. Reported whether or not it is enabled, for the same reason as the
    # camera: you can see that the microphone is off.
    try:
        from zoe.config import (
            ACTIVE_AUDIO_PROFILE, VAD_BARGE_IN_MS, VAD_BARGE_IN_SNR,
            VAD_BARGE_IN_THRESHOLD, WAKE_WORD_ENABLED, WAKE_WORD_MODEL,
            WAKE_WORD_THRESHOLD, output_device_name, vad_overrides,
        )
        if WAKE_WORD_ENABLED:
            import speech_recognition  # noqa: F401
            print(f"[Check] Voice: ON — wake word '{WAKE_WORD_MODEL}' "
                  f"(threshold {WAKE_WORD_THRESHOLD})")
        else:
            print("[Check] Voice: off by config (microphone will not be opened)")
        # The device profile is invisible in every other line here: the same
        # barge-in bar would be in force on headphones and on the laptop
        # speakers unless the name is printed.
        device = output_device_name()
        overrides = vad_overrides()
        print(f"[Check] Output device: {device or 'unknown'}")
        print(f"[Check] Barge-in: snr={VAD_BARGE_IN_SNR} ms={VAD_BARGE_IN_MS} "
              f"threshold={VAD_BARGE_IN_THRESHOLD} profile="
              f"{ACTIVE_AUDIO_PROFILE.get('name', 'default')} overrides="
              f"{overrides or 'none'}")
    except ImportError as e:
        print(f"[Check] Voice: enabled but speech recognition is unavailable ({e}). "
              f"Text conversation still works.")

    # Check data directory
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"[Check] data directory: {data_dir}")

    # Check log file
    log_file = data_dir / "zoe.log"
    print(f"[Check] log file: {log_file}")

    # Check wake word model
    try:
        import openwakeword
        model_info = openwakeword.MODELS.get("hey_jarvis")
        if model_info:
            model_path = model_info["model_path"].replace(".tflite", ".onnx")
            model_exists = os.path.exists(model_path)
            print(f"[Check] Wake word model: {'Found' if model_exists else 'NOT FOUND'}")
            if not model_exists:
                print(f"  Note: Run 'python3 -m zoe.main --test' to download models")
    except ImportError:
        print(f"[Check] openwakeword: Not installed")

    # Voice engine. Checks the engine that is actually configured, not a fixed
    # one: this used to probe Chatterbox unconditionally, so a Kokoro install
    # reported a misleading failure about a helper Chatterbox no longer has.
    check_voice_engine()

    # System info
    print(f"\n[Info] OS: {platform.system()} {platform.release()}")
    print(f"[Info] Machine: {platform.machine()}")
    print(f"[Info] Assistant: {ASSISTANT_NAME}")
    print(f"[Info] LLM Model: {LLM_MODEL}")

    print(f"\n{'='*60}")
    print(f"  All checks passed. Starting {ASSISTANT_NAME}...")
    print(f"{'='*60}\n")

    return True


def show_status():
    """Display current system status."""
    from zoe.config import (
        ASSISTANT_NAME, GREETING_NAME, LLM_MODEL,
        WAKE_WORD_MODEL, WAKE_WORD_THRESHOLD,
        MAX_HISTORY_MESSAGES, TTS_ENGINE,
        VISION_ENABLED, WEBCAM_ENABLED, WEBCAM_INTERVAL_SECONDS,
        WEBCAM_ANALYSIS_COOLDOWN,
    )
    from zoe.logger import logger

    print(f"\n{'='*60}")
    print(f"  Zoe — System Status")
    print(f"{'='*60}\n")

    print(f"  Assistant:     {ASSISTANT_NAME}")
    print(f"  User:          {GREETING_NAME}")
    print(f"  LLM Model:     {LLM_MODEL}")
    print(f"  TTS Engine:    {TTS_ENGINE}")
    print(f"  Wake Word:     {WAKE_WORD_MODEL} (threshold: {WAKE_WORD_THRESHOLD})")
    print(f"  Max History:   {MAX_HISTORY_MESSAGES} messages")

    # The camera's state is printed unconditionally, not only when enabled: the
    # point of a privacy feature is that you can see it is off.
    print(f"  Vision:        {'enabled' if VISION_ENABLED else 'disabled'}")
    if VISION_ENABLED and WEBCAM_ENABLED:
        print(f"  Webcam:        ON — sampled every {WEBCAM_INTERVAL_SECONDS}s, "
              f"analysed at most every {WEBCAM_ANALYSIS_COOLDOWN}s. "
              f"Nothing is recorded or saved.")
    else:
        print(f"  Webcam:        off (no camera is opened)")

    try:
        from zoe.voice_loop import get_voice_loop
        loop = get_voice_loop()
    except Exception:
        loop = None
    if loop is not None and loop.is_running():
        print(f"  Voice:         ON — wake '{loop.wake_engine.model}', "
              f"follow-up {loop.follow_up_window:.0f}s, "
              f"mic {'open' if loop.microphone.available else 'unavailable'}")
    else:
        print(f"  Voice:         off (microphone is not opened)")

    # Memory stats
    try:
        from zoe import memory
        conn = memory.init_memory_db()
        count = memory.get_memory_count(conn)
        print(f"  Long-term Mem: {count} facts stored")
        conn.close()
    except Exception:
        print(f"  Long-term Mem: Not available")

    # Log file
    from zoe.config import LOG_FILE
    if LOG_FILE.exists():
        size = LOG_FILE.stat().st_size
        print(f"  Log File:      {LOG_FILE} ({size} bytes)")
    else:
        print(f"  Log File:      Not yet created")

    print(f"\n{'='*60}\n")


def main():
    """Parse arguments and start Zoe."""
    parser = argparse.ArgumentParser(description="Zoe — AI Assistant")
    parser.add_argument("--text", action="store_true", help="Text mode (skip wake word)")
    parser.add_argument("--test", action="store_true", help="Run integration tests")
    parser.add_argument("--no-wake", action="store_true", help="Voice mode without wake word")
    parser.add_argument("--status", action="store_true", help="Show system status")
    parser.add_argument("--debug", action="store_true",
                        help="Show internal diagnostic logs on the terminal "
                             "(normal mode keeps user-facing output only)")
    args = parser.parse_args()

    # Phase 1: terminal shows user-facing output only unless --debug opens
    # console diagnostics. The log file always receives full diagnostics.
    # --debug also enables the (logging-only) routing/model debug lines.
    from zoe.logger import configure_logging
    configure_logging(debug=args.debug)
    if args.debug:
        import zoe.brain as _brain_mod
        _brain_mod.DEBUG_INPUT = True

    if args.status:
        show_status()
        return

    if args.test:
        from tests.test_integration import run_all_tests
        run_all_tests()
        return

    # Check environment before starting
    if not check_environment():
        print("\nStartup aborted. Please fix the issues above.\n")
        sys.exit(1)

    # Start the appropriate mode. --no-wake is a deprecated alias that now
    # falls through to voice mode, so existing commands keep working.
    if args.text:
        from zoe.main import run_text_mode
        run_text_mode()
    else:
        from zoe.main import run_voice_mode
        try:
            run_voice_mode()
        except KeyboardInterrupt:
            print("\nSession ended by user.")
            sys.exit(0)


if __name__ == "__main__":
    main()
