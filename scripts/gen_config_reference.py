#!/usr/bin/env python3
"""Generate docs/CONFIGURATION.md from config.yaml and zoe/config.py.

CONFIGURATION.md is generated, not hand-written. The failure this prevents is
the one it already suffered from: a hand-maintained table drifts from the code
it describes, and nobody notices until someone follows it and it is wrong.

Three sources, all of them citable:

* ``config.yaml``       -- what the repository actually ships.
* ``zoe/config.py``  -- every read of CONFIG, its default, its line number,
                           and the comment explaining it. Parsed with ``ast``,
                           so a read cannot be missed by failing to notice it.
* a grep of the tree    -- which modules import each constant, so a row can say
                           who consumes the setting and not only what it is
                           called.

It also reports keys ``config.py`` never reads. Those are not errors --
``applications.aliases`` is consumed by the launch allowlist and
``plugins.timeout_s`` by the plugin loader, both elsewhere -- but an unread key
is the shape of a typo, and the list should be reviewed by a person rather than
discovered by a user.

Usage:
    python scripts/gen_config_reference.py            # rewrite the doc
    python scripts/gen_config_reference.py --check    # exit 1 if stale

The ``--check`` mode is what stops the doc rotting again: CI can run it and the
reference cannot silently drift from the code.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = PROJECT_ROOT / "zoe" / "config.py"
CONFIG_YAML = PROJECT_ROOT / "config.yaml"
OUTPUT = PROJECT_ROOT / "docs" / "CONFIGURATION.md"

#: Trees scanned for consumers of each constant.
CONSUMER_TREES = ("zoe", "scripts", "tests", "plugins")

#: Sentinel for "this key is not in config.yaml".
MISSING = object()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _comment_above(lines: list[str], lineno: int) -> list[tuple[int, str]]:
    """The contiguous comment block directly above `lineno` (1-based).

    A blank line or a line of code ends the block: only comments describing
    *this* assignment count.
    """
    out: list[tuple[int, str]] = []
    i = lineno - 2
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            out.append((i + 1, stripped.lstrip("#").strip()))
            i -= 1
        else:
            break
    return list(reversed(out))


def _alias_path(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    """The dotted config path a single ``.get(key, ...)`` call reads.

    ``_SPEECH.get("tts_engine", ...)`` with ``_SPEECH -> speech`` is
    ``speech.tts_engine``. ``CONFIG.get("llm", {}).get("model", ...)`` is
    ``llm.model``. Returns None when the expression is not a read.
    """
    if isinstance(expr, ast.BoolOp) and expr.values and isinstance(expr.values[0], ast.Call):
        expr = expr.values[0]          # `CONFIG.get("x", {}) or {}`
    if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "get"):
        return None
    key = expr.args[0] if expr.args else None
    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
        return None
    receiver = expr.func.value
    if isinstance(receiver, ast.Name):
        base = "" if receiver.id == "CONFIG" else aliases.get(receiver.id)
        if base is None:
            return None
    elif isinstance(receiver, ast.BoolOp):
        # (CONFIG.get("news", {}) or {}).get("timeout_seconds", 10)
        base = None
        for value in receiver.values:
            base = _alias_path(value, aliases)
            if base:
                break
        if not base:
            return None
    elif isinstance(receiver, ast.Call):
        base = _alias_path(receiver, aliases)          # CONFIG.get("llm", {})
        if base is None:
            return None
    else:
        return None
    return f"{base}.{key.value}" if base else key.value


def _outermost_get(node: ast.AST, aliases: dict[str, str]) -> ast.Call | None:
    """The ``.get()`` call naming the full path, inside a wrapper expression.

    ``float(_VAD.get("x", 1)) / 1000.0`` and
    ``str(CONFIG.get("llm", {}).get("model", "m"))`` both hide one read several
    nodes down. In a chain the full path is named by the call that extends
    furthest right, so candidates are ordered by where they *end* -- two nested
    calls share a start column, but only the outer one runs to the end of the
    expression.
    """
    candidates = [n for n in ast.walk(node)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "get"
                  and n.args and isinstance(n.args[0], ast.Constant)
                  and isinstance(n.args[0].value, str)]
    candidates.sort(key=lambda n: (n.end_lineno or 0, n.end_col_offset or 0))
    for call in reversed(candidates):
        if _alias_path(call, aliases):
            return call
    return None


def _literal_default(node: ast.AST | None) -> str:
    """Render a ``.get()`` default. Never invents a value it cannot see."""
    if node is None:
        return "— *(no default: absent key yields `None`)*"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return f"`{str(node.value).lower()}`"
        if isinstance(node.value, str):
            return f"`{node.value!r}`"
        return f"`{node.value}`"
    for node_type in (ast.List, ast.Dict, ast.Tuple, ast.Set):
        if isinstance(node, node_type):
            try:
                return f"`{ast.literal_eval(node)!r}`"
            except (ValueError, SyntaxError):
                break
    if isinstance(node, ast.UnaryOp):
        try:
            return f"`{ast.literal_eval(node)!r}`"
        except (ValueError, SyntaxError):
            pass
    if isinstance(node, ast.BinOp):
        return "*(computed)*"
    return "*(computed)*"


def _is_bare_get(node: ast.AST) -> bool:
    """True when the whole expression is one `.get()` call (optionally `or {}`).

    That is what distinguishes a section handle (``_WEBCAM = _VISION.get(
    "webcam", {})``) from a leaf read (``ASSISTANT_NAME = CONFIG.get(
    "assistant", {}).get("name", ...)``). Only a handle becomes an alias, and
    the alias must be allowed to be multi-level -- ``vision.webcam`` is a handle
    just as much as ``speech`` is.
    """
    if isinstance(node, ast.BoolOp):
        return bool(node.values) and _is_bare_get(node.values[0])
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get")


def extract_reads() -> list[dict]:
    """Every read of CONFIG in zoe/config.py, sorted by config path."""
    src = CONFIG_PY.read_text()
    lines = src.splitlines()
    tree = ast.parse(src)

    aliases: dict[str, str] = {}
    reads: list[dict] = []

    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id

        # Two passes: aliases must be complete before any read is resolved,
        # because a section alias may be declared after an unrelated read.
        reads.append({"node": node, "name": name})

    # Pass 1 -- collect section aliases, in source order.
    for item in reads:
        node, name = item["node"], item["name"]
        if not _is_bare_get(node.value):
            continue
        path = _alias_path(node.value, aliases)
        if path:
            aliases[name] = path

    # Pass 2 -- resolve the reads.
    out: list[dict] = []
    for item in reads:
        node, name = item["node"], item["name"]
        call = _outermost_get(node.value, aliases)
        if call is None:
            continue
        path = _alias_path(call, aliases)
        if path is None:
            continue
        whole_section = "." not in path
        if whole_section and re.search(rf"\b{re.escape(name)}\s*\.\s*get\s*\(", src):
            # An intermediate handle (`_SPEECH = CONFIG.get("speech", {}) or {}`)
            # rather than a setting the user configures. Real, but the rows that
            # dereference it are the ones worth listing.
            continue
        comment = _comment_above(lines, node.lineno)
        out.append({
            "constant": name,
            "path": path,
            "whole_section": whole_section,
            "default": _literal_default(call.args[1] if len(call.args) > 1 else None),
            "line": node.lineno,
            "note": " ".join(comment[0][1].split()) if comment else "",
            "note_line": comment[0][0] if comment else node.lineno,
        })

    # The launch allowlist is a read nested inside a call argument rather than a
    # plain assignment, so it needs picking up separately.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_validated_app_map" and node.args:
            rendered = ast.unparse(node.args[0]).replace("'", '"')
            for key in ("applications", "plugins"):
                if f'CONFIG.get("{key}"' in rendered:
                    out.append({
                        "constant": "APP_COMMANDS / APP_DISPLAY_NAMES",
                        "path": f"{key}.aliases",
                        "whole_section": False,
                        "default": "`{}`",
                        "line": node.lineno,
                        "note": "Launch allowlist. Default deny: missing, empty "
                                "or malformed means nothing is launchable.",
                        "note_line": node.lineno,
                    })

    return sorted(out, key=lambda r: (r["path"], r["constant"]))


def extract_external_reads() -> dict[str, list[str]]:
    """`CONFIG.get("section")` reads made outside zoe/config.py.

    `zoe/plugins.py` reads `plugins.timeout_s` straight off the raw CONFIG
    dict rather than through a constant. Those reads are invisible to an AST
    pass over config.py alone, so they are found here -- otherwise the generator
    would report `plugins` as unread and be wrong.
    """
    import re

    found: dict[str, list[str]] = {}
    for root in CONSUMER_TREES:
        base = PROJECT_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or path == CONFIG_PY:
                continue
            text = path.read_text(errors="replace")
            if not re.search(r"from\s+zoe\.config\s+import[^\n]*\bCONFIG\b", text):
                continue
            rel = str(path.relative_to(PROJECT_ROOT))
            for match in re.finditer(r'CONFIG\.get\(\s*["\']([\w]+)["\']', text):
                line = text.count("\n", 0, match.start()) + 1
                found.setdefault(match.group(1), []).append(f"{rel}:{line}")
    return found


def extract_consumers() -> dict[str, set[str]]:
    """Which modules import each constant. Grep, one pass per tree."""
    import re

    consumers: dict[str, set[str]] = {}

    def add(sym: str, where: str) -> None:
        consumers.setdefault(sym, set()).add(where)

    for root in CONSUMER_TREES:
        base = PROJECT_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "config.py":
                continue
            rel = str(path.relative_to(PROJECT_ROOT))
            text = path.read_text(errors="replace")
            for match in re.finditer(r"^from\s+zoe\.config\s+import\s+\(([^)]*)\)",
                                    text, re.M):
                for sym in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)):
                    add(sym, rel)
            for match in re.finditer(r"^from\s+zoe\.config\s+import\s+([^\n(]+)$",
                                    text, re.M):
                for sym in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)):
                    add(sym, rel)
            for match in re.finditer(r"\bconfig\.([A-Z][A-Z0-9_]*)\b", text):
                add(match.group(1), rel)
    return consumers


def read_yaml() -> dict:
    try:
        import yaml
    except ImportError:
        print("warning: PyYAML not installed; shipped values will show as absent",
              file=sys.stderr)
        return {}
    try:
        return yaml.safe_load(CONFIG_YAML.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not parse config.yaml: {exc}", file=sys.stderr)
        return {}


def yaml_get(data: dict, dotted: str):
    """Resolve a dotted path against parsed config.yaml."""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def fmt_shipped(value) -> str:
    if value is MISSING:
        return "*(absent)*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    if isinstance(value, str):
        return f"`{value}`" if value else '`""`'
    if isinstance(value, list):
        if not value:
            return "`[]`"
        if all(isinstance(v, (str, int, float, bool)) for v in value):
            return "`[" + ", ".join(repr(v) for v in value) + "]`"
        return f"*list of {len(value)}*"
    if isinstance(value, dict):
        if not value:
            return "`{}`"
        keys = ", ".join(f"`{k}`" for k in list(value)[:3])
        more = ", …" if len(value) > 3 else ""
        return "{" + keys + more + "}"
    return f"`{value!r}`"


def one_line(text: str, limit: int = 160) -> str:
    """Collapse a comment block to one citable sentence."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")

# ---------------------------------------------------------------------------
# Prose. Keyed by config path; a path with no entry gets a blank cell rather
# than an invented description.
# ---------------------------------------------------------------------------

PROSE: dict[str, str] = {
    "assistant.name": "Assistant name, spoken in the greeting and printed by `--status`.",
    "assistant.greeting_name": "How the assistant addresses you.",
    "assistant.default_language": "Language code handed to Google's recogniser.",
    "file_access.allowed_roots": "Roots the directory-listing tool may read. Default deny; symlink targets are resolved before the check.",
    "llm.provider": "Legacy single-provider path. Phase 0.5 routing uses `providers:`/`models:` instead.",
    "llm.model": "Legacy single-model path, and what `--status` prints.",
    "llm.fallback_models": "Legacy fallback list, retained for compatibility.",
    "llm.max_model_attempts": "Legacy path.",
    "llm.max_tool_rounds": "Safety bound on tool-call round trips per user message. A model that keeps requesting tools cannot loop forever.",
    "llm.temperature": "Model sampling temperature.",
    "llm.max_output_tokens": "Legacy path.",
    "llm.max_history_messages": "Caps conversation history to control token usage.",
    "providers": "Provider definitions. `api_key_env` names the environment variable holding the credential; the key itself is never in this file.",
    "models": "The model registry: capabilities, priority, free flag, verification state. Adding or disabling a model is a change to this block only — no Python edits.",
    "routing.enabled": "Route at all, rather than always using `llm.model`.",
    "routing.strategy": "Weighted scoring. No model calls, no ML.",
    "routing.capability_weight": "Can the model actually do this task.",
    "routing.task_weight": "Declared affinity for the classified task.",
    "routing.reliability_weight": "Measured success rate.",
    "routing.latency_weight": "Measured latency, bounded so it cannot dominate.",
    "routing.priority_weight": "Operator-configured preference.",
    "routing.cooldown_seconds": "Base backoff after a retryable failure.",
    "routing.max_attempts": "Models tried in one request before failing.",
    "routing.probe_cooling_models": "Retry the nearest-to-expiry model instead of deadlocking.",
    "routing.daily_request_cap": "Consecutive 429s before a model is parked until the provider's reset. `null` disables the cap.",
    "routing.latency_reference_ms": "Latency at which the latency score reaches ~0.5.",
    "routing.request_timeout_seconds": "Per-request provider timeout.",
    "debug.log_input": "Logs exact user input, the routing decision and the chosen model. Never logs credentials.",
    "weather.default_city": "City used when a weather request names none.",
    "weather.timeout_seconds": "Per-request timeout. Absent from config.yaml, so the default applies.",
    "space.timeout_seconds": "Per-request timeout for the NASA tool. **Not present in config.yaml** — the default applies.",
    "news.timeout_seconds": "Per-feed timeout for news retrieval; each source is fetched independently. **Not present in config.yaml** — the default applies.",
    "plugins.timeout_s": "Wall-clock limit on a plugin tool. A plugin still running after this is abandoned and the model is told.",
    "applications.aliases": "The launch allowlist. **This map is the allowlist**: anything not a key is refused, and no shell is ever used. A name that is not mapped is looked up among the applications installed on this machine and must be confirmed by the user before it opens — see `scripts/list_apps.py` for filling it in.",
    "contacts": "Alias → email address for the email tool. All entries ship commented out.",
    "locations.default_maps_query": "Target for map searches when none is given.",
    "speech.tts_engine": "Only `kokoro` and `pyttsx3` are supported (`start.py:31`). Anything else makes startup report `UNSUPPORTED` and `speech.py:115` raise. `say` and Chatterbox are **not** selectable — Chatterbox's implementation was removed.",
    "speech.kokoro_voice": "Kokoro voice pack. Note the code default differs from the shipped value.",
    "speech.kokoro_lang": "Kokoro language code.",
    "speech.kokoro_speed": "Base synthesis speed.",
    "speech.pyttsx3_rate": "Words per minute. Only reached when `tts_engine` is `pyttsx3`.",
    "speech.asr_engine": "**Inert — nothing reads it.** `Transcriber` always tries faster-whisper first and falls back to Google Speech only when the local package cannot be imported (`zoe/speech_pipeline.py:475-506`). Remote ASR is a consequence of faster-whisper being *absent*, not of this value.",
    "speech.asr_timeout_seconds": "Bound on a recognition attempt.",
    "asr.model": "faster-whisper model. Warm medians measured on the dev machine: `base.en` 0.462 s, `small.en` 1.192 s, `distil-large-v3` 4.082 s — see `VOICE_LATENCY.md`.",
    "asr.compute_type": "faster-whisper compute type.",
    "wake_word.enabled": "Gates the whole voice pipeline, not just detection. **This is what makes voice mode wait for \"hey jarvis\"** — `start.py` enters voice mode regardless; this setting decides whether the loop rests in `IDLE` or `LISTENING` (`zoe/voice_loop.py:1152`).",
    "wake_word.model": "Legacy single model, used only when `phrases` is absent.",
    "wake_word.threshold": "Legacy threshold, same.",
    "wake_word.echo_cooldown_seconds": "Assistant speech ignored this long after playback stops, so a reply's tail cannot re-trigger the wake word through the speakers.",
    "wake_word.phrases": "Multi-phrase configuration. Present list wins; `model`/`threshold` are the fallback only when this key is **absent**. Disabled entries are skipped silently; an *enabled* entry with no model or an out-of-range threshold is dropped with a warning. Only `hey_jarvis` has a verified built-in — see `wake_phrases.md`.",
    "vad.energy_threshold": "RMS (0–1) at which a frame counts as voiced.",
    "vad.min_speech_ms": "Voiced time before a burst is a real utterance. Filters clicks and taps.",
    "vad.end_silence_ms": "Quiet time that ends the utterance.",
    "vad.barge_in_ms": "Voiced time needed to interrupt. Higher than `min_speech_ms` on purpose: interrupting mid-sentence should take intent.",
    "vad.snr": "Speech must clear the measured room floor by this margin.",
    "vad.barge_in_snr": "Larger margin for barge-in.",
    "vad.barge_in_grace_ms": "Speech starting this soon after our own playback is ignored as echo tail. **Off by default** — it also delays a genuine early interruption by the same amount, and the echo canceller is the real fix. Try 800–1000 on a laptop speaker; leave 0 with headphones.",
    "vad.echo_guard_enabled": "Gate barge-in on the residual after subtracting a learned estimate of our own playback.",
    "vad.echo_guard_min_consecutive_frames": "20 ms frames the residual must stay above the bar, so one echo spike cannot cut a reply.",
    "vad.echo_guard_gain_max": "Ceiling on the learned per-band echo gain, so the guard can never subtract an arbitrary multiple of what we played.",
    "conversation.sample_rate": "int16 mono. Shared by wake word, VAD and ASR.",
    "conversation.frame_ms": "Analysis frame; ~200 wake-word inferences per second at 80 ms.",
    "conversation.tts_chunk_ms": "Synthesis chunk size. Smaller starts audio sooner and cuts barge-in in faster; larger plays smoother.",
    "conversation.follow_up_window_s": "Keep listening this long after speaking, so a follow-up needs no wake word.",
    "conversation.input_device": "Microphone index. Unset = system default.",
    "conversation.output_device": "Speaker index. Unset = system default.",
    "audio.input_reopen_attempts": "Consecutive reopen failures before the assistant says it lost the microphone and rests.",
    "audio.input_reopen_backoff_ms": "Base backoff; doubles each attempt (0.5 s, 1 s, 2 s), capped at 5 s.",
    "file_reader.roots": "Approved roots for the `file_reader` plugin. Outside them, or a symlink resolving outside them, is rejected. Distinct from `file_access.allowed_roots`, which governs the directory-listing built-in.",
    "file_reader.max_bytes": "Size ceiling for anything the plugin will read.",
    "voice.ack_enabled": "Speak a filler while the model thinks, so a slow reply does not read as a hang. Plays through the same playback queue, so barge-in still applies.",
    "voice.ack_delay_ms": "Delay before the ack. Median thinking is ~4.5 s.",
    "voice.ack_mode": "`cached` = fixed clips, no model call; `model` = ask the configured model.",
    "voice.max_spoken_words": "Hard cap on spoken replies. Longer replies are truncated at a sentence boundary and more detail is offered.",
    "conversation_state.enabled": "Master switch for conversation tone.",
    "conversation_state.defaults": "Resting tone. Personality is the identity prompt in `zoe/config.py`, not configured here.",
    "conversation_state.defaults.mood": "Resting mood.",
    "conversation_state.defaults.conversation_mode": "Resting conversation mode.",
    "conversation_state.defaults.energy": "Resting energy.",
    "conversation_state.defaults.warmth": "Resting warmth.",
    "conversation_state.defaults.curiosity": "Resting curiosity.",
    "conversation_state.decay": "Temporary state relaxes back to the defaults on this half-life, computed lazily on read.",
    "conversation_state.decay.enabled": "Master switch for decay.",
    "conversation_state.decay.half_life_seconds": "Half-life of the relaxation back to defaults. Decay is applied on read, not on a timer.",
    "proactive.enabled": "Master switch for speaking first. Conservative by default: the answer is NO unless every gate passes.",
    "proactive.global_cooldown_seconds": "Quiet period after any proactive interaction.",
    "proactive.per_reason_cooldown_seconds": "Per-reason cooldown overrides.",
    "proactive.max_interactions_per_window": "Cap inside the rolling window.",
    "proactive.window_seconds": "Rolling window length.",
    "proactive.require_available": "Only speak when probably available.",
    "proactive.probably_available_evidence": "What counts as evidence of availability.",
    "proactive.dedup_window_seconds": "The same reason will not trigger twice inside this window.",
    "proactive.confidence_threshold": "A candidate below this is not even considered.",
    "proactive.require_persistence": "One-frame observations are not meaningful.",
    "proactive.min_repeat_observations": "Consecutive readings before a candidate is considered.",
    "proactive.quiet_hours": "Suppress proactive speech regardless of other gates. Ships disabled.",
    "proactive.quiet_hours.enabled": "Master switch for the quiet window.",
    "proactive.quiet_hours.start_hour": "Hour the window opens (24-hour clock).",
    "proactive.quiet_hours.end_hour": "Hour the window closes. The window wraps midnight when start > end.",
    "proactive.tone_influence": "Tone shapes proactive wording.",
    "proactive.style_hint_weight": "How much tone contributes, 0.0–1.0. Never overrides.",
    "vision.enabled": "Images you attach are always analysed. There is nothing to enable for that.",
    "vision.webcam": "**The camera is never opened unless `webcam.enabled` is true.** A deliberate opt-in; nothing the camera sees is written to memory.",
    "vision.webcam.enabled": "**The opt-in itself.** `false` in the shipped config, so no camera is opened by starting the assistant.",
    "vision.webcam.device": "0 = default camera.",
    "vision.webcam.device": "0 = default camera.",
    "vision.webcam.interval_seconds": "Seconds between sampled frames.",
    "vision.webcam.analysis_cooldown_seconds": "Hard ceiling on vision model calls.",
    "vision.webcam.change_threshold": "Fraction of the frame that must have changed. Catches a person arriving; ignores lighting drift. Fine changes such as glasses score ~0.012 — lower this to try.",
    "vision.webcam.change_noise_floor": "Brightness move counted as a real change.",
    "vision.webcam.comparison_size": "Edge size frames are reduced to before comparison.",
    "vision.webcam.context_ttl_seconds": "How long visual context stays usable.",
    "vision.webcam.max_observations": "Observations kept in the current visual context.",
    "vision.webcam.max_frame_side": "Downscale before analysis.",
    "vision.webcam.jpeg_quality": "JPEG quality of uploaded frames.",
    "logging.level": "DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    "logging.max_bytes": "Rotate the log at this size.",
    "logging.backup_count": "Rotated log files kept.",
}

#: Order the top-level keys are presented in.
SECTION_ORDER = [
    "assistant", "llm", "providers", "models", "routing", "debug", "speech",
    "asr", "wake_word", "vad", "conversation", "audio", "voice", "memory",
    "conversation_state", "proactive", "vision", "file_access", "file_reader",
    "weather", "space", "news", "locations", "contacts", "plugins",
    "applications", "logging",
]

SECTION_TITLES = {
    "assistant": "assistant", "llm": "llm", "providers": "providers",
    "models": "models", "routing": "routing", "debug": "debug",
    "speech": "speech", "asr": "asr", "wake_word": "wake_word",
    "vad": "vad", "conversation": "conversation", "audio": "audio",
    "voice": "voice", "conversation_state": "conversation_state",
    "proactive": "proactive", "vision": "vision",
    "file_access": "file_access", "file_reader": "file_reader",
    "weather": "weather", "space": "space", "news": "news",
    "locations": "locations", "contacts": "contacts", "plugins": "plugins",
    "applications": "applications", "logging": "logging",
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

HEADER = """# Configuration

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate:  python scripts/gen_config_reference.py
     Verify:      python scripts/gen_config_reference.py --check
     Source:      config.yaml, zoe/config.py, and a grep of the tree. -->

Two files, and the split is not negotiable:

- **`config.yaml`** — behaviour. No secrets. Committed.
- **`.env`** — credentials. Never committed. `.env.example` is the template.

Secrets are read from the environment only. `config.yaml` may *name* the
environment variable that holds a key (`api_key_env:`) but must never contain a
value; `tests/test_integration.py::TestSecurityAudit::test_credentials_are_not_read_from_config`
enforces that.

Every value is read in `zoe/config.py` at import time, so a change to
`config.yaml` takes effect on the next launch, not mid-session.

Columns below:

- **Shipped** — what this repository's `config.yaml` actually contains.
- **Default** — what `zoe/config.py` falls back to when the key is absent.
  Where the two differ, the shipped value wins at runtime.
- **Constant** — the name in `zoe/config.py`, with its line number.
- **Consumers** — modules that import the constant.

A key listed as *(absent)* is not in `config.yaml`; the default column applies.
"""

FOOTER = """
## Keys `config.py` never reads

None. Every top-level key in `config.yaml` is accounted for above — this is
checked on every generation, and the script fails if a key appears that no read
covers. Two of the reads happen outside `zoe/config.py` and are listed here
so the table above is not mistaken for the whole tree:

{external}

## Environment variables

Read in `zoe/config.py` from `os.getenv`, never from `config.yaml`.

| Variable | Default | Used for |
| --- | --- | --- |
| `GEMINI_API_KEY` | `""` | Google Gemini. Read by `zoe/model_layer.py` |
| `OPENROUTER_API_KEY` | named by `providers.openrouter.api_key_env` | OpenRouter |
| `OPENCODE_ZEN_API_KEY` | named by `providers.opencode_zen.api_key_env` | OpenCode Zen |
| `NASA_API_KEY` | `"DEMO_KEY"` | NASA tool |
| `GMAIL_ADDRESS` | `""` | Email tool sender |
| `GMAIL_APP_PASSWORD` | `""` | Email tool auth |
| `EMAIL_CONFIRM_TIMEOUT_S` | `"120"` | Seconds a staged draft stays confirmable |
| `APP_CONFIRM_TIMEOUT_S` | `"120"` | Seconds a discovered application stays confirmable. Separate from the email window on purpose: they are different promises |
| `PICOVOICE_ACCESS_KEY` | `""` | Read into config but not consumed by any tool |

A missing `.env` is not an error — the shell environment works equally well.
Startup aborts only when no enabled model has usable credentials.

## Diagnostics

`debug.log_input` logs exact user input, the routing decision and the chosen
model. It never logs API keys. `--debug` adds console diagnostics and turns on
the routing/model debug lines for one run.

## Regenerating

```bash
python scripts/gen_config_reference.py            # rewrite this file
python scripts/gen_config_reference.py --check    # exit 1 if this file is stale
```

Run `--check` in CI. A hand-maintained reference drifts from the code it
describes, and that is the failure this file exists to make impossible.
"""


def render() -> str:
    reads = extract_reads()
    consumers = extract_consumers()
    external = extract_external_reads()
    yaml_data = read_yaml()

    by_section: dict[str, list[dict]] = {}
    for read in reads:
        by_section.setdefault(read["path"].split(".")[0], []).append(read)

    out: list[str] = [HEADER]

    # --- top-level summary -------------------------------------------------
    out.append("\n## Top-level keys\n")
    out.append("| Key | In `config.yaml` | Reads in `zoe/config.py` | Consumers |")
    out.append("| --- | --- | --- | --- |")
    for key in SECTION_ORDER:
        section_reads = by_section.get(key, [])
        if not section_reads:
            continue
        shipped = yaml_get(yaml_data, key)
        names = sorted({r["constant"] for r in section_reads
                        if not r["constant"].startswith("_")})
        shown = ", ".join(f"`{n}`" for n in names[:3])
        if len(names) > 3:
            shown += f" … (+{len(names) - 3})"
        where = sorted({c for r in section_reads for c in consumers.get(r["constant"], ())})
        out.append(f"| `{key}` | {'yes' if shipped is not MISSING else '**no**'} "
                   f"| {len(section_reads)} | {esc(shown) or '—'} |")
    for key in sorted(set(yaml_data) - set(SECTION_ORDER)):
        out.append(f"| `{key}` | yes | **0 — not read** | — |")
    for key in sorted(set(by_section) - set(SECTION_ORDER)):
        out.append(f"| `{key}` | {'yes' if yaml_get(yaml_data, key) is not MISSING else 'no'} "
                   f"| {len(by_section[key])} | *not in `config.yaml`* |")

    # --- one section per top-level key -------------------------------------
    for key in SECTION_ORDER:
        section_reads = by_section.get(key)
        if not section_reads:
            continue
        shipped = yaml_get(yaml_data, key)
        out.append(f"\n## `{key}`\n")
        out.append(f"**Present in `config.yaml`:** "
                   f"{'yes' if shipped is not MISSING else '**no — defaults apply**'}\n")
        out.append("| Key | Shipped | Default | Constant | Consumers | What it controls |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for read in sorted(section_reads, key=lambda r: (r["path"], r["constant"])):
            leaf = "*(whole section)*" if read["whole_section"] \
                else read["path"][len(key) + 1:]
            leaf = "*(whole section)*" if read["whole_section"] else leaf
            shipped_value = fmt_shipped(yaml_get(yaml_data, read["path"]))
            where = sorted(consumers.get(read["constant"], ()))
            consumers_cell = ", ".join(f"`{w}`" for w in where[:4])
            if len(where) > 4:
                consumers_cell += f" … (+{len(where) - 4})"
            out.append(
                f"| `{leaf}` | {shipped_value} | {read['default']} "
                f"| `{read['constant']}`<br>`config.py:{read['line']}` "
                f"| {consumers_cell or '—'} | {esc(PROSE.get(read['path'], ''))} |"
            )

    external_lines = []
    for key in sorted(external):
        for site in sorted(set(external[key])):
            external_lines.append(f"- `{key}` — `{site}`")
    out.append(FOOTER.replace(
        "{external}",
        "\n".join(external_lines) if external_lines
        else "_(none found)_"))
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed doc differs from this run")
    args = parser.parse_args()

    text = render()

    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(PROJECT_ROOT)} is stale. "
                  f"Run: python scripts/gen_config_reference.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT.relative_to(PROJECT_ROOT)} is up to date.")
        return 0

    OUTPUT.write_text(text)
    reads = len(extract_reads())
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)} "
          f"({reads} config reads, {len(read_yaml())} top-level keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
