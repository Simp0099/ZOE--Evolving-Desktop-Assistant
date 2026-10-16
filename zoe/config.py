"""Configuration loader for Zoe.

Centralizes all environment variable secrets and non-sensitive YAML configurations.
Modules should always import settings from here rather than reading os.environ directly.
"""

import os
from pathlib import Path
from zoe.logger import logger

try:
    import yaml
except ImportError:
    yaml = None

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path=None):
        """Minimal fallback parser if python-dotenv is not yet installed."""
        path = dotenv_path or Path(".env")
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())

# Base Project Directories
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

# Ensure runtime directories exist
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Load secrets from .env file
load_dotenv(PROJECT_ROOT / ".env")

# API Keys & Secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

#: Seconds a staged email draft stays confirmable. The user's next message is
#: the confirmation; if it arrives after this window, the draft is stale and is
#: dropped rather than sent to a recipient the user may have moved on from.
EMAIL_CONFIRM_TIMEOUT_S = float(os.getenv("EMAIL_CONFIRM_TIMEOUT_S", "120"))
#: Seconds a discovered-but-unmapped application stays confirmable. Separate from
#: the email window on purpose: they are different promises, and a user answering
#: "yes, send it" must not also open an application.
APP_CONFIRM_TIMEOUT_S = float(os.getenv("APP_CONFIRM_TIMEOUT_S", "120"))
PICOVOICE_ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")

# Load User Preferences from config.yaml
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
if CONFIG_FILE.exists() and yaml is not None:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            CONFIG = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to parse config.yaml: {e}")
        CONFIG = {}
else:
    CONFIG = {}

# Read-only directory access for the listing tool. Resolve roots once so
# traversal and symlink targets are checked against canonical paths.
_file_access = CONFIG.get("file_access", {})
if not isinstance(_file_access, dict):
    _file_access = {}
_allowed_file_roots = _file_access.get("allowed_roots", [".", "data"])
if not isinstance(_allowed_file_roots, list) or not all(
    isinstance(root, str) and root.strip() for root in _allowed_file_roots
):
    logger.warning("Invalid file_access.allowed_roots; using project and data directories.")
    _allowed_file_roots = [".", "data"]
ALLOWED_FILE_ROOTS = tuple(
    (PROJECT_ROOT / Path(root).expanduser()).resolve()
    if not Path(root).expanduser().is_absolute()
    else Path(root).expanduser().resolve()
    for root in _allowed_file_roots
)

# Assistant Settings
ASSISTANT_NAME = CONFIG.get("assistant", {}).get("name", "Ai Partner")
GREETING_NAME = CONFIG.get("assistant", {}).get("greeting_name", "Boss")
DEFAULT_LANGUAGE = CONFIG.get("assistant", {}).get("default_language", "en-in")

# LLM Brain Settings (Google Gemini)
LLM_PROVIDER = CONFIG.get("llm", {}).get("provider", "gemini")
LLM_MODEL = CONFIG.get("llm", {}).get("model", "gemini-3.6-flash")
LLM_TEMPERATURE = float(CONFIG.get("llm", {}).get("temperature", 0.7))
LLM_MAX_OUTPUT_TOKENS = int(CONFIG.get("llm", {}).get("max_output_tokens", 1024))

# Model resilience: the primary model is tried first, then each fallback in order.
# A model that 404s (retired) or 503s (unavailable) must not take down the agent.
LLM_FALLBACK_MODELS = CONFIG.get("llm", {}).get("fallback_models", []) or []
MAX_MODEL_ATTEMPTS = int(CONFIG.get("llm", {}).get("max_model_attempts", 2))

# Bound on tool-call round trips per user message, so a model that keeps
# requesting tools cannot loop forever and starve the next user message.
MAX_TOOL_ROUNDS = int(CONFIG.get("llm", {}).get("max_tool_rounds", 8))

# Phase 7: Conversation history cap to control token consumption
MAX_HISTORY_MESSAGES = int(CONFIG.get("llm", {}).get("max_history_messages", 20))

# ---------------------------------------------------------------------------
# Phase 0.5: provider-agnostic model layer
# ---------------------------------------------------------------------------
# Provider definitions. `api_key_env` names the environment variable holding the
# credential; keys themselves are never stored in config.yaml.
PROVIDERS_CONFIG = CONFIG.get("providers", {}) or {}

# Model registry: capability profiles, priorities and verification state.
MODELS_CONFIG = CONFIG.get("models", {}) or {}

# Router behaviour.
ROUTING_CONFIG = CONFIG.get("routing", {}) or {}

# ---------------------------------------------------------------------------
# Dev-only diagnostics: log the exact user input, routing decision, and the
# message list handed to the model. Never logs API keys or credentials.
# ---------------------------------------------------------------------------
DEBUG_INPUT = str(CONFIG.get("debug", {}).get("log_input", False)).lower() in (
    "1", "true", "yes", "on",
)

ZOE_SYSTEM_PROMPT = """
You are Ai Partner, a long-term AI partner — not a generic customer-service chatbot.

## Identity
Be intelligent, proactive, context-aware, practical, honest, warm, concise when
appropriate, detailed when useful, action-oriented, and adaptable to the user's
situation. Your primary objective: help the user make meaningful progress toward
their goals. Light humour and emojis are fine, but never at the cost of usefulness.
Match the user's register: focused if serious, energetic if excited, calm and
solution-oriented if frustrated, creative if brainstorming. Never robotic, corporate,
scripted, or padded.

## Relationship
Treat the user as a long-term partner, not a sequence of isolated requests. Maintain
continuity. The literal request may not be the real objective: identify what they are
trying to achieve, why, what is blocking progress, and the most useful next step. Make
reasonable assumptions and proceed; ask only when a missing detail would likely produce
the wrong result.

## Thinking
Before answering, work out what the user actually wants, what you already know, what
is safe to assume, what is missing, the simplest effective path, whether a better
approach exists, and what should happen next. Prefer practical solutions over theory
when they are trying to accomplish something. For multi-stage work: define the
objective, phase it, name the current phase, complete it, verify, continue. Focus on
the next meaningful action, not on the whole future map.

## Proactiveness
Surface the next logical step, important risks, missing considerations, simplifications,
priorities, and concrete actions when they create value — never as noise. Do not
suggest work just to look helpful, and never manufacture urgency.

## Communication
Clarity over length. Use headings, steps, bullets, tables only when they genuinely help.
Never repeat what the user already knows unless clarity requires it. Make instructions
executable: what to do, where, what to enter, and the expected result.

## Honesty
Never claim to have done, accessed, or verified anything you did not. Never invent
facts, results, sources, or capabilities. If uncertain, say so, give the best available
answer, and name what would resolve it. Accuracy beats the appearance of confidence.

## Decisions
Do not dump a long list of options. Determine the objective and constraints, recommend
the strongest option and why, note real trade-offs, and offer alternatives only when
genuinely useful. Help the user decide; don't endlessly push the decision back.

## User agency
Support the user's judgement, don't replace it. Be clear about consequences of
trade-offs. Do not manipulate, and do not pretend certainty that doesn't exist.

## State awareness
Distinguish user-provided facts, tool-obtained information, assumptions, and unknowns.
Never turn an assumption into a fact. When context conflicts, surface the conflict
instead of silently picking one reading.

## Response shape
Simple request: answer directly.
Moderate request: answer clearly, then the relevant next steps.
Complex objective: structured plan, start with the most important actionable step.
Ambiguous request: infer intent from context where possible.
Execution task: do the work, don't just describe it.

You have tools — use them whenever a request needs real-world action. Do not expose
hidden chain-of-thought; give conclusions, reasoning summaries, decisions, assumptions,
and actionable steps instead.

## Irreversible actions
Some tools have real-world effects that cannot be undone: sending an email, opening
a browser, launching an application, taking a screenshot, saving to memory. For these:
act when the user asked for that specific action, and otherwise confirm first.
`send_email` never sends on its own: it records a draft and returns. Read the exact
recipient, subject and body back to the user and ask whether to send. The email is sent
only after the user clearly agrees in their next message, and there is no argument you
can pass to send it yourself. Silence, "ok", or ambiguity is not agreement.

Never claim a tool did something unless its result says so. If a result reports a
failure, say it failed.

## Visual input
Images are labelled by source. Do not assume a webcam image is a screen capture.
A screen capture shows windows and UI; a webcam image shows the room; an attachment
is something the user chose to send. Describe only what the image actually shows,
and say so plainly when the source makes a question unanswerable.
"""

# Weather & Locations
DEFAULT_CITY = CONFIG.get("weather", {}).get("default_city", "Faridabad")
DEFAULT_MAPS_QUERY = CONFIG.get("locations", {}).get("default_maps_query", "Delhi")

# Weather & NASA tool timeouts. Every outbound tool call is bounded: an
# unbounded request is how one slow API turns into an assistant that never
# answers.
WEATHER_TIMEOUT_S = float((CONFIG.get("weather", {}) or {}).get("timeout_seconds", 10))
NASA_TIMEOUT_S = float((CONFIG.get("space", {}) or {}).get("timeout_seconds", 10))

#: Per-feed timeout for news retrieval. Each source is fetched independently, so
#: this bounds the slowest feed rather than the sum of all of them.
NEWS_TIMEOUT_S = float((CONFIG.get("news", {}) or {}).get("timeout_seconds", 10))

#: Sent on every outbound HTTP request. Some public APIs (notably wttr.in and
#: Nominatim) reject requests without one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Contacts Map
CONTACTS = CONFIG.get("contacts", {}) or {}


def _validated_argv(raw):
    """One launch command as an argument list, or None when it is unusable.

    A bare string is accepted as the whole command, because the common case is
    an application with no arguments. Every element must be a non-empty string:
    the executable plus its arguments, nothing else. Nothing here is ever
    escaped or quoted, because nothing here is ever passed to a shell.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None
    if not all(isinstance(part, str) and part for part in raw):
        return None
    return list(raw)


def _validated_app_map(raw):
    """Spoken name -> {operating system -> argv}, dropping malformed entries.

    Returns two dicts keyed the same way: the command table, and the name as
    the user wrote it, so the assistant can say back "Safari" rather than
    "safari". Keys are casefolded here because the lookup has to be
    case-insensitive and there is exactly one place that decides that.
    """
    commands, display = {}, {}
    if not isinstance(raw, dict):
        return commands, display
    for name, per_os in raw.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(per_os, dict):
            continue
        table = {}
        for system, argv in per_os.items():
            if not isinstance(system, str):
                continue
            validated = _validated_argv(argv)
            if validated:
                table[system.strip()] = validated
        if not table:
            continue
        key = name.strip().casefold()
        if key in commands:
            continue  # first mapping wins; a duplicate spelling is not an override
        commands[key] = table
        display[key] = name.strip()
    return commands, display


#: Local application launching. Default deny: missing, empty or malformed means
#: nothing is launchable. The map is the allowlist -- a name that is not a key
#: here is refused without the operating system ever being consulted.
APP_COMMANDS, APP_DISPLAY_NAMES = _validated_app_map(
    (CONFIG.get("applications", {}) or {}).get("aliases", {})
)

# Speech & TTS Settings
# Paths are relative to PROJECT_ROOT (the repo root) unless absolute.
_SPEECH = CONFIG.get("speech", {}) or {}

TTS_ENGINE = _SPEECH.get("tts_engine", "kokoro")
REQUEST_TIMEOUT_S = float(ROUTING_CONFIG.get("request_timeout_seconds", 45))
ASR_ENGINE = str(_SPEECH.get("asr_engine", "google")).strip().lower()
ASR_TIMEOUT_S = float(_SPEECH.get("asr_timeout_seconds", 15.0))

# Local faster-whisper model. `base.en` used to be hard-coded in Transcriber, so
# neither the model nor the compute type was configurable at all. config.yaml
# selects `small.en`; this fallback only applies if the `asr` block is missing.
# Measured on this machine (docs/VOICE_LATENCY.md): base.en 0.462 s warm median,
# small.en 1.192 s, distil-large-v3 4.082 s.
_ASR = CONFIG.get("asr", {}) or {}
ASR_MODEL = str(_ASR.get("model", "distil-large-v3"))
ASR_COMPUTE_TYPE = str(_ASR.get("compute_type", "int8"))

# Kokoro local TTS.
KOKORO_VOICE = _SPEECH.get("kokoro_voice", "am_adam")
KOKORO_LANG = _SPEECH.get("kokoro_lang", "a")
KOKORO_SPEED = float(_SPEECH.get("kokoro_speed", 1.0))

PYTTSX3_RATE = int(_SPEECH.get("pyttsx3_rate", 180))

# ---------------------------------------------------------------------------
# Conversation state (Phase 5) -- how Zoe is talking right now
# ---------------------------------------------------------------------------
# Personality ("who Zoe is") stays in ZOE_SYSTEM_PROMPT and is untouched.
# This is the dynamic layer around it: mood, energy, warmth, curiosity, the mode
# of the conversation, and what the user said about themselves.
#
# Nothing here is written to memory. A mood is not a fact about a person.
_TONE = CONFIG.get("conversation_state", {}) or {}
_TONE_DEFAULTS = _TONE.get("defaults", {}) or {}
_TONE_DECAY = _TONE.get("decay", {}) or {}

TONE_ENABLED = bool(_TONE.get("enabled", True))

TONE_MOOD_DEFAULT = str(_TONE_DEFAULTS.get("mood", "neutral"))
TONE_MODE_DEFAULT = str(_TONE_DEFAULTS.get("conversation_mode", "casual"))
TONE_ENERGY_DEFAULT = float(_TONE_DEFAULTS.get("energy", 0.55))
TONE_WARMTH_DEFAULT = float(_TONE_DEFAULTS.get("warmth", 0.75))
TONE_CURIOUSITY_DEFAULT = float(_TONE_DEFAULTS.get("curiosity", 0.60))

# Temporary emotional state relaxes back toward the defaults on this half-life.
# Computed lazily on read, so an idle assistant spends nothing on it.
TONE_DECAY_ENABLED = bool(_TONE_DECAY.get("enabled", True))
TONE_DECAY_HALF_LIFE = float(_TONE_DECAY.get("half_life_seconds", 300.0))

# ---------------------------------------------------------------------------
# Vision / Perception (Phase 3)
# ---------------------------------------------------------------------------
# Two independent visual inputs share one perception layer:
#   * images the user attaches (always available, never automatic), and
#   * the webcam (opt-in, off by default).
#
# The webcam being off by default is the point: opening a camera is a decision
# the user makes in this file, never a side effect of starting the assistant.
_VISION = CONFIG.get("vision", {}) or {}
_WEBCAM = _VISION.get("webcam", {}) or {}

VISION_ENABLED = bool(_VISION.get("enabled", True))

WEBCAM_ENABLED = bool(_WEBCAM.get("enabled", False))
WEBCAM_DEVICE = int(_WEBCAM.get("device", 0))

# Seconds between sampled frames. The camera runs continuously; nothing is
# captured or analysed faster than this.
WEBCAM_INTERVAL_SECONDS = float(_WEBCAM.get("interval_seconds", 10.0))

# Minimum seconds between vision model calls. The ceiling on cost: however
# much the scene changes, the model is consulted at most this often.
WEBCAM_ANALYSIS_COOLDOWN = float(_WEBCAM.get("analysis_cooldown_seconds", 120.0))

# Local change detection. A frame is compared against the previous one at this
# resolution, and the change score is the fraction of cells whose brightness
# moved by more than `change_noise_floor`.
#
# The threshold is a fraction of the frame, not an average brightness change: a
# mean cannot tell a person sitting down (0.0240) from a window shade opening
# (0.0235), while as materially-changed-cell fractions those are 0.054 and 0.000.
# 0.02 catches people and objects and ignores lighting drift and sensor noise.
#
# It does not catch fine appearance changes such as glasses — those are ~1% of
# the frame, too close to real sensor noise to threshold honestly. Lower
# `change_threshold` to try; the analysis cooldown still bounds the cost.
WEBCAM_CHANGE_THRESHOLD = float(_WEBCAM.get("change_threshold", 0.02))
WEBCAM_CHANGE_NOISE_FLOOR = float(_WEBCAM.get("change_noise_floor", 0.06))
WEBCAM_SAMPLE_SIZE = int(_WEBCAM.get("comparison_size", 32))

# How long the current visual context stays usable, and how many observations it
# holds. Short-lived by design: this is context, not memory.
WEBCAM_CONTEXT_TTL = float(_WEBCAM.get("context_ttl_seconds", 600.0))
WEBCAM_MAX_OBSERVATIONS = int(_WEBCAM.get("max_observations", 6))

# Frame downscale before analysis: keeps uploads and vision cost down.
WEBCAM_MAX_SIDE = int(_WEBCAM.get("max_frame_side", 640))
WEBCAM_JPEG_QUALITY = int(_WEBCAM.get("jpeg_quality", 70))

# ---------------------------------------------------------------------------
# Voice (Phase 4) -- wake word, endpointing, conversational state machine
# ---------------------------------------------------------------------------
# Reuses the existing `wake_word:` block rather than adding a parallel `wake:`
# section -- two sections meaning the same thing is how a threshold ends up set
# in one place and read from the other.
_WAKE = CONFIG.get("wake_word", {}) or {}
_VAD = CONFIG.get("vad", {}) or {}
_CONVERSATION = CONFIG.get("conversation", {}) or {}

# openWakeWord settings (Phase 6)
WAKE_WORD_MODEL = _WAKE.get("model", "hey_jarvis")
WAKE_WORD_THRESHOLD = float(_WAKE.get("threshold", 0.5))


def _validated_wake_phrases(raw, default_model, default_threshold) -> list:
    """Parse the `wake_word.phrases` list into active phrase entries.

    Returns [{phrase, model, threshold, enabled}] for valid entries only.
    Disabled entries are skipped silently (inert by definition); ENABLED
    entries with a missing model or an out-of-range threshold are dropped
    with a warning, never activated. Absent/invalid lists fall back to the
    legacy single model so existing configuration keeps working.
    """
    fallback = [{"phrase": "hey jarvis", "model": default_model,
                 "threshold": default_threshold, "enabled": True}]
    if not isinstance(raw, list):
        return fallback
    parsed = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        phrase = entry.get("phrase", "")
        phrase = " ".join(str(phrase).split())
        enabled = bool(entry.get("enabled", True))
        model = str(entry.get("model", "") or "").strip()
        try:
            threshold = float(entry.get("threshold", default_threshold))
        except (TypeError, ValueError):
            threshold = default_threshold
        if not phrase or not model:
            if enabled:
                logger.warning(f"Ignoring wake phrase with no model: {phrase!r}.")
            continue
        if not 0.0 < threshold < 1.0:
            if enabled:
                logger.warning(f"Ignoring wake phrase with bad threshold: {phrase!r}.")
            continue
        parsed.append({"phrase": phrase, "model": model,
                       "threshold": threshold, "enabled": enabled})
    # NOTE: an explicitly emptied list is respected (no usable detector --
    # the engine fails clearly at load). Only a missing key falls back.
    return [p for p in parsed if p["enabled"]]


#: Active wake phrases after validation. Empty model catalog at this layer is
#: fine: the engine verifies names against installed models at load time.
WAKE_WORD_PHRASES = _validated_wake_phrases(
    _WAKE.get("phrases"), WAKE_WORD_MODEL, WAKE_WORD_THRESHOLD
)

# Phase 4: the wake word is opt-in like the webcam. `enabled` gates the whole
# voice pipeline, not just detection, so a disabled wake word leaves text mode
# completely untouched.
WAKE_WORD_ENABLED = bool(_WAKE.get("enabled", False))
#: Seconds of assistant speech that must be ignored after playback stops, so the
#: tail of a reply cannot re-trigger the wake word through the speakers.
WAKE_WORD_ECHO_COOLDOWN = float(_WAKE.get("echo_cooldown_seconds", 0.6))

# Audio capture. int16 mono at 16kHz is what both openWakeWord and
# SpeechRecognition expect, so one stream serves all three consumers.
AUDIO_SAMPLE_RATE = int(_CONVERSATION.get("sample_rate", 16000))
AUDIO_CHANNELS = 1
AUDIO_FRAME_MS = int(_CONVERSATION.get("frame_ms", 80))
AUDIO_INPUT_DEVICE = _CONVERSATION.get("input_device")   # None = system default
AUDIO_OUTPUT_DEVICE = _CONVERSATION.get("output_device") # None = system default

# Input-device reconnection. A disconnected microphone is reopened with a
# backoff instead of killing the voice loop; after `input_reopen_attempts`
# consecutive failures the assistant says so and rests.
_AUDIO = CONFIG.get("audio", {}) or {}
#: Consecutive failed reopens before the assistant gives up and idles.
INPUT_REOPEN_ATTEMPTS = int(_AUDIO.get("input_reopen_attempts", 5))
#: Base backoff between reopen attempts, in ms. Doubles each attempt, capped at 5 s.
INPUT_REOPEN_BACKOFF_MS = int(_AUDIO.get("input_reopen_backoff_ms", 500))
AUDIO_PROFILES = _AUDIO.get("profiles", {}) or {}


def resolve_audio_profile(device_name: str = "") -> dict:
    """The audio profile that matches `device_name`, or `{}` for the defaults.

    First match wins, so order the map from most to least specific. Matching is
    a case-insensitive substring test against the configured `match` list, which
    is deliberately loose: device names vary by machine ("MacBook Air Speakers",
    "Built-in Output", "MacBook Pro Speakers") and a false negative means the
    user silently gets the laptop-speaker defaults on their earphones.

    Returns the profile with its `name` added, so the caller can report which one
    is active. An unrecognised, empty or unreadable device name returns `{}`:
    the whole point is that the defaults keep working.
    """
    name = str(device_name or "").casefold()
    if not name or not isinstance(AUDIO_PROFILES, dict):
        return {}
    for key, profile in AUDIO_PROFILES.items():
        if not isinstance(profile, dict):
            continue
        for token in profile.get("match", []) or []:
            if str(token).casefold() in name:
                return {"name": key, **profile}
    return {}


def output_device_name(device=None) -> str:
    """Name of the output device that will actually be used, or `""`.

    Never raises. Startup must not fall over because PortAudio is unhappy, and
    an empty name is handled: it selects the defaults.
    """
    try:
        import sounddevice as sd

        index = device
        if index is None:
            index = AUDIO_OUTPUT_DEVICE if AUDIO_OUTPUT_DEVICE is not None \
                else sd.default.device[1]
        if index is None or int(index) < 0:
            return ""
        return str(sd.query_devices(int(index), "output").get("name", ""))
    except Exception:  # noqa: BLE001 -- diagnostics must not break startup
        return ""


#: Resolved once per process. The VAD is built from these at VoiceLoop
#: construction, and a config change is a restart like everything else here.
ACTIVE_AUDIO_PROFILE = resolve_audio_profile(output_device_name())


def vad_overrides(profile: dict = None) -> dict:
    """`VoiceActivityDetector` kwargs implied by `profile` (default: the active one).

    Only keys the detector actually accepts are returned, so a typo in
    config.yaml degrades to "ignored" rather than to a TypeError at startup.
    """
    profile = ACTIVE_AUDIO_PROFILE if profile is None else (profile or {})
    if not profile:
        return {}
    out = {}
    for key, config_key in (("barge_in_snr", "barge_in_snr"),
                            ("barge_in_ms", "barge_in_ms"),
                            ("snr", "snr"),
                            ("barge_in_threshold", "barge_in_threshold")):
        if config_key in profile:
            try:
                out[key] = float(profile[config_key])
            except (TypeError, ValueError):
                logger.warning(f"audio profile {profile.get('name')!r}: "
                               f"{config_key}={profile[config_key]!r} is not a number")
    return out

# Voice activity detection. Energy over RMS, local and deterministic.
#: Minimum RMS (0..1) for a frame to count as voiced during normal listening.
VAD_ENERGY_THRESHOLD = float(_VAD.get("energy_threshold", 0.01))
#: Quiet time that ends an utterance.
VAD_END_SILENCE_MS = float(_VAD.get("end_silence_ms", 700))
#: Voiced time required before a burst counts as speech. Filters clicks, taps
#: and other single-frame pops.
VAD_MIN_SPEECH_MS = float(_VAD.get("min_speech_ms", 200))
#: Voiced time required to interrupt the assistant. Higher than min_speech on
#: purpose: interrupting mid-sentence should take intent.
VAD_BARGE_IN_MS = float(_VAD.get("barge_in_ms", 300))
#: Energy floor for barge-in. Defaults to 2x the normal threshold (see
#: VoiceActivityDetector), because the microphone is also hearing the speakers.
VAD_BARGE_IN_THRESHOLD = (
    float(_VAD["barge_in_threshold"]) if "barge_in_threshold" in _VAD else None
)

# Signal-to-noise margins above the measured room floor. These, not the absolute
# thresholds, are what decide what counts as speech in a loud room: ambient RMS
# measured 0.046-0.14 on the development machine, above any absolute threshold
# tuned in a quiet one.
VAD_SNR = float(_VAD.get("snr", 3.0))
#: Speech starting this soon after our own audio began is treated as echo tail.
BARGE_IN_GRACE_S = float(_VAD.get("barge_in_grace_ms", 0)) / 1000.0
VAD_BARGE_IN_SNR = float(_VAD.get("barge_in_snr", 4.0))

# Band-energy echo guard (EchoGuard): barge-in is gated on the *residual* of the
# microphone minus a learned per-band estimate of our own playback, not on raw
# loudness. A reply through the speakers is as loud as the user and sits in the
# same bands, so loudness alone cannot tell them apart; the residual can.
#: Off makes the guard a passthrough (pre-guard behaviour).
VAD_ECHO_GUARD_ENABLED = bool(_VAD.get("echo_guard_enabled", True))
#: Residual must clear the barge-in threshold this many 20 ms frames in a row
#: before a frame counts as the user, so a brief echo spike cannot cut a reply.
VAD_ECHO_GUARD_MIN_CONSECUTIVE_FRAMES = int(
    _VAD.get("echo_guard_min_consecutive_frames", 3)
)
#: Upper bound on the learned per-band echo gain. Bounded so the guard can only
#: ever subtract a *bounded* multiple of what we played, never an arbitrary one.
VAD_ECHO_GUARD_GAIN_MAX = float(_VAD.get("echo_guard_gain_max", 1.5))
#: Also measure the bar instead of only configuring it. The guard compares the
#: share of the microphone's energy that our own playback does not explain
#: against the worst share *this room* has been seen to leave, instead of against
#: a fixed constant -- which is what keeps a loud, reverberant speaker from
#: making its own residual look like a person. Set false for the pre-existing
#: fixed-threshold decision (see docs/ECHO_GUARD_VALIDATION.md; not yet
#: validated on hardware).
VAD_ECHO_GUARD_ADAPTIVE_BAR = bool(_VAD.get("echo_guard_adaptive_bar", True))

#: How long the assistant keeps listening after it finishes speaking, so a
#: follow-up does not need the wake word again.
FOLLOW_UP_WINDOW_S = float(_CONVERSATION.get("follow_up_window_s", 8.0))

# Spoken acknowledgment while the main model thinks. Median thinking is ~4.5s and
# p90 ~6.6s; without a filler a slow reply reads as a hang. The ack plays through
# the same playback queue, so barge-in and cancellation still apply to it.
_VOICE = CONFIG.get("voice", {}) or {}
#: Master switch for the thinking acknowledgment.
ACK_ENABLED = bool(_VOICE.get("ack_enabled", True))
#: Milliseconds after entering THINKING before the ack is spoken. Shorter feels
#: responsive but can ack before the model would have answered anyway.
ACK_DELAY_MS = float(_VOICE.get("ack_delay_ms", 800))
#: "cached" speaks a fixed clip with no model call; "model" asks the configured
#: model for a one-line acknowledgment instead.
ACK_MODE = _VOICE.get("ack_mode", "cached")
#: Hard cap on spoken replies, in words. A reply longer than this is truncated
#: at a sentence boundary and offered more detail, because a long spoken answer
#: reads as a wall of sound the user cannot follow.
MAX_SPOKEN_WORDS = int(_VOICE.get("max_spoken_words", 60))
#: Generation cap for a spoken turn, in tokens. The prompt asks for one to three
#: sentences; this is what stops a model that ignores the ask from spending a
#: minute of audio on a paragraph. 220 tokens ~= 150 words ~= a short paragraph,
#: so a reply that genuinely needs more is cut at a sentence boundary and offered
#: more detail rather than ending mid-clause. Applies to voice turns ONLY:
#: a typed conversation keeps llm.max_output_tokens, because a long written
#: answer is not a wall of sound.
VOICE_MAX_OUTPUT_TOKENS = int(_VOICE.get("max_output_tokens", 220))
#: Opt-in for text conversations to use the same cap. Off by default; turning it
#: on is a real behaviour change for every typed turn, so it is never implicit.
TEXT_MAX_OUTPUT_TOKENS = _VOICE.get("text_max_output_tokens")

# Approved roots for the file_reader plugin, and the size ceiling for anything
# it will read. Anything outside the roots, a symlink pointing outside them, or a
# file over the ceiling is rejected with a plain message.
_FILE_READER = CONFIG.get("file_reader", {}) or {}
FILE_READER_ROOTS = _FILE_READER.get("roots") or ["~/Documents", "~/Downloads", "~/Desktop"]
FILE_READER_MAX_BYTES = int(_FILE_READER.get("max_bytes", 20 * 1024 * 1024))
#: Characters of extracted text handed to the model. The full budget when a
#: question is given, the summary budget when it is not. Keep MAX_RESULT_CHARS
#: at or above FILE_READER_MAX_CHARS, or the plugin loader cuts the result --
#: and the closing fence and truncation notice with it -- before the model sees it.
FILE_READER_MAX_CHARS = int(_FILE_READER.get("max_chars", 12000))
FILE_READER_SUMMARY_CHARS = int(_FILE_READER.get("summary_chars", 2000))
#: PDF pages read per call. ponytail ceiling: a scanned 500-page report is a
#: different feature, not a longer read.
FILE_READER_MAX_PDF_PAGES = int(_FILE_READER.get("max_pdf_pages", 50))
#: Speech synthesis chunk size. Smaller chunks start audio sooner and make
#: barge-in cut in faster; larger chunks play more smoothly.
TTS_CHUNK_MS = int(_CONVERSATION.get("tts_chunk_ms", 220))

# ---------------------------------------------------------------------------
# Proactive partner (Phase 6) -- when Zoe may initiate conversation
# ---------------------------------------------------------------------------
# Conservative by default: the decision engine's answer is NO unless every
# gate passes. Nothing here opens a camera or microphone; those stay opt-in
# under `vision:` and `wake_word:`.
_PROACTIVE = CONFIG.get("proactive", {}) or {}
_PROACTIVE_QUIET = _PROACTIVE.get("quiet_hours", {}) or {}

PROACTIVE_ENABLED = bool(_PROACTIVE.get("enabled", True))
PROACTIVE_COOLDOWN_S = float(_PROACTIVE.get("global_cooldown_seconds", 300.0))
PROACTIVE_DEDUP_S = float(_PROACTIVE.get("dedup_window_seconds", 600.0))
PROACTIVE_MAX_PER_WINDOW = int(_PROACTIVE.get("max_interactions_per_window", 3))
PROACTIVE_WINDOW_S = float(_PROACTIVE.get("window_seconds", 3600.0))
PROACTIVE_CONFIDENCE_THRESHOLD = float(_PROACTIVE.get("confidence_threshold", 0.5))
PROACTIVE_MIN_REPEAT = int(_PROACTIVE.get("min_repeat_observations", 2))
PROACTIVE_QUIET_ENABLED = bool(_PROACTIVE_QUIET.get("enabled", False))
PROACTIVE_QUIET_START = int(_PROACTIVE_QUIET.get("start_hour", 22))
PROACTIVE_QUIET_END = int(_PROACTIVE_QUIET.get("end_hour", 8))

# Logging Settings (Phase 8)
LOG_FILE = DATA_DIR / "zoe.log"
LOG_MAX_BYTES = int(CONFIG.get("logging", {}).get("max_bytes", 1_000_000))  # 1 MB
LOG_BACKUP_COUNT = int(CONFIG.get("logging", {}).get("backup_count", 3))
LOG_LEVEL = CONFIG.get("logging", {}).get("level", "INFO")
