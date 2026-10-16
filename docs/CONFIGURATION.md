# Configuration

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


## Top-level keys

| Key | In `config.yaml` | Reads in `zoe/config.py` | Consumers |
| --- | --- | --- | --- |
| `assistant` | yes | 3 | `ASSISTANT_NAME`, `DEFAULT_LANGUAGE`, `GREETING_NAME` |
| `llm` | yes | 8 | `LLM_FALLBACK_MODELS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_MODEL` … (+5) |
| `providers` | yes | 1 | `PROVIDERS_CONFIG` |
| `models` | yes | 1 | `MODELS_CONFIG` |
| `routing` | yes | 1 | `REQUEST_TIMEOUT_S` |
| `debug` | yes | 1 | `DEBUG_INPUT` |
| `speech` | yes | 7 | `ASR_ENGINE`, `ASR_TIMEOUT_S`, `KOKORO_LANG` … (+4) |
| `asr` | yes | 2 | `ASR_COMPUTE_TYPE`, `ASR_MODEL` |
| `wake_word` | yes | 5 | `WAKE_WORD_ECHO_COOLDOWN`, `WAKE_WORD_ENABLED`, `WAKE_WORD_MODEL` … (+2) |
| `vad` | yes | 11 | `BARGE_IN_GRACE_S`, `VAD_BARGE_IN_MS`, `VAD_BARGE_IN_SNR` … (+8) |
| `conversation` | yes | 6 | `AUDIO_FRAME_MS`, `AUDIO_INPUT_DEVICE`, `AUDIO_OUTPUT_DEVICE` … (+3) |
| `audio` | yes | 3 | `AUDIO_PROFILES`, `INPUT_REOPEN_ATTEMPTS`, `INPUT_REOPEN_BACKOFF_MS` |
| `voice` | yes | 6 | `ACK_DELAY_MS`, `ACK_ENABLED`, `ACK_MODE` … (+3) |
| `conversation_state` | yes | 10 | `TONE_CURIOUSITY_DEFAULT`, `TONE_DECAY_ENABLED`, `TONE_DECAY_HALF_LIFE` … (+5) |
| `proactive` | yes | 11 | `PROACTIVE_CONFIDENCE_THRESHOLD`, `PROACTIVE_COOLDOWN_S`, `PROACTIVE_DEDUP_S` … (+7) |
| `vision` | yes | 13 | `VISION_ENABLED`, `WEBCAM_ANALYSIS_COOLDOWN`, `WEBCAM_CHANGE_NOISE_FLOOR` … (+9) |
| `file_access` | yes | 1 | — |
| `file_reader` | yes | 5 | `FILE_READER_MAX_BYTES`, `FILE_READER_MAX_CHARS`, `FILE_READER_MAX_PDF_PAGES` … (+2) |
| `weather` | yes | 2 | `DEFAULT_CITY`, `WEATHER_TIMEOUT_S` |
| `space` | **no** | 1 | `NASA_TIMEOUT_S` |
| `news` | **no** | 1 | `NEWS_TIMEOUT_S` |
| `locations` | yes | 1 | `DEFAULT_MAPS_QUERY` |
| `contacts` | yes | 1 | `CONTACTS` |
| `applications` | yes | 1 | `APP_COMMANDS / APP_DISPLAY_NAMES` |
| `logging` | yes | 3 | `LOG_BACKUP_COUNT`, `LOG_LEVEL`, `LOG_MAX_BYTES` |

## `assistant`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `default_language` | `en-in` | `'en-in'` | `DEFAULT_LANGUAGE`<br>`config.py:90` | `zoe/speech.py`, `zoe/speech_pipeline.py` | Language code handed to Google's recogniser. |
| `greeting_name` | `Boss` | `'Boss'` | `GREETING_NAME`<br>`config.py:89` | `tests/test_phase2_continuity.py`, `zoe/brain.py`, `zoe/main.py` | How the assistant addresses you. |
| `name` | `Ai Partner` | `'Ai Partner'` | `ASSISTANT_NAME`<br>`config.py:88` | `tests/test_phase2_continuity.py`, `zoe/main.py` | Assistant name, spoken in the greeting and printed by `--status`. |

## `llm`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `fallback_models` | `['gemini-3.5-flash-lite']` | `[]` | `LLM_FALLBACK_MODELS`<br>`config.py:100` | `zoe/direct.py` | Legacy fallback list, retained for compatibility. |
| `max_history_messages` | `20` | `20` | `MAX_HISTORY_MESSAGES`<br>`config.py:108` | `zoe/brain.py` | Caps conversation history to control token usage. |
| `max_model_attempts` | `2` | `2` | `MAX_MODEL_ATTEMPTS`<br>`config.py:101` | `zoe/direct.py` | Legacy path. |
| `max_output_tokens` | `1024` | `1024` | `LLM_MAX_OUTPUT_TOKENS`<br>`config.py:96` | `zoe/direct.py`, `zoe/model_layer.py` | Legacy path. |
| `max_tool_rounds` | `8` | `8` | `MAX_TOOL_ROUNDS`<br>`config.py:105` | `zoe/brain.py` | Safety bound on tool-call round trips per user message. A model that keeps requesting tools cannot loop forever. |
| `model` | `gemini-3.6-flash` | `'gemini-3.6-flash'` | `LLM_MODEL`<br>`config.py:94` | `zoe/brain.py`, `zoe/direct.py`, `zoe/main.py` | Legacy single-model path, and what `--status` prints. |
| `provider` | `gemini` | `'gemini'` | `LLM_PROVIDER`<br>`config.py:93` | — | Legacy single-provider path. Phase 0.5 routing uses `providers:`/`models:` instead. |
| `temperature` | `0.7` | `0.7` | `LLM_TEMPERATURE`<br>`config.py:95` | `zoe/direct.py`, `zoe/model_layer.py` | Model sampling temperature. |

## `providers`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `*(whole section)*` | {`gemini`, `openrouter`, `opencode_zen`} | `{}` | `PROVIDERS_CONFIG`<br>`config.py:115` | `zoe/model_layer.py` | Provider definitions. `api_key_env` names the environment variable holding the credential; the key itself is never in this file. |

## `models`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `*(whole section)*` | {`gemini_flash`, `gemini_flash_lite`, `nemotron_3_5_lightning`, …} | `{}` | `MODELS_CONFIG`<br>`config.py:118` | `zoe/model_layer.py` | The model registry: capabilities, priority, free flag, verification state. Adding or disabling a model is a change to this block only — no Python edits. |

## `routing`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `request_timeout_seconds` | *(absent)* | `45` | `REQUEST_TIMEOUT_S`<br>`config.py:302` | `zoe/brain.py` | Per-request provider timeout. |

## `debug`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `log_input` | `false` | `false` | `DEBUG_INPUT`<br>`config.py:127` | `zoe/brain.py`, `zoe/model_layer.py` | Logs exact user input, the routing decision and the chosen model. Never logs credentials. |

## `speech`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `asr_engine` | `local` | `'google'` | `ASR_ENGINE`<br>`config.py:303` | `zoe/speech_pipeline.py` | **Inert — nothing reads it.** `Transcriber` always tries faster-whisper first and falls back to Google Speech only when the local package cannot be imported (`zoe/speech_pipeline.py:475-506`). Remote ASR is a consequence of faster-whisper being *absent*, not of this value. |
| `asr_timeout_seconds` | `15` | `15.0` | `ASR_TIMEOUT_S`<br>`config.py:304` | `zoe/speech_pipeline.py` | Bound on a recognition attempt. |
| `kokoro_lang` | `a` | `'a'` | `KOKORO_LANG`<br>`config.py:317` | `zoe/speech.py`, `zoe/tts.py` | Kokoro language code. |
| `kokoro_speed` | `1.0` | `1.0` | `KOKORO_SPEED`<br>`config.py:318` | `zoe/speech.py`, `zoe/tts.py` | Base synthesis speed. |
| `kokoro_voice` | `af_heart` | `'am_adam'` | `KOKORO_VOICE`<br>`config.py:316` | `zoe/speech.py`, `zoe/tts.py` | Kokoro voice pack. Note the code default differs from the shipped value. |
| `pyttsx3_rate` | `180` | `180` | `PYTTSX3_RATE`<br>`config.py:320` | `zoe/speech.py` | Words per minute. Only reached when `tts_engine` is `pyttsx3`. |
| `tts_engine` | `kokoro` | `'kokoro'` | `TTS_ENGINE`<br>`config.py:301` | `tests/test_startup.py`, `tests/test_tts_engine.py`, `zoe/speech.py`, `zoe/speech_pipeline.py` | Only `kokoro` and `pyttsx3` are supported (`start.py:31`). Anything else makes startup report `UNSUPPORTED` and `speech.py:115` raise. `say` and Chatterbox are **not** selectable — Chatterbox's implementation was removed. |

## `asr`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `compute_type` | `int8` | `'int8'` | `ASR_COMPUTE_TYPE`<br>`config.py:313` | `zoe/speech_pipeline.py` | faster-whisper compute type. |
| `model` | `small.en` | `'distil-large-v3'` | `ASR_MODEL`<br>`config.py:312` | `zoe/speech_pipeline.py` | faster-whisper model. Warm medians measured on the dev machine: `base.en` 0.462 s, `small.en` 1.192 s, `distil-large-v3` 4.082 s — see `VOICE_LATENCY.md`. |

## `wake_word`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `echo_cooldown_seconds` | `0.6` | `0.6` | `WAKE_WORD_ECHO_COOLDOWN`<br>`config.py:464` | — | Assistant speech ignored this long after playback stops, so a reply's tail cannot re-trigger the wake word through the speakers. |
| `enabled` | `true` | `false` | `WAKE_WORD_ENABLED`<br>`config.py:461` | `zoe/voice_loop.py` | Gates the whole voice pipeline, not just detection. **This is what makes voice mode wait for "hey jarvis"** — `start.py` enters voice mode regardless; this setting decides whether the loop rests in `IDLE` or `LISTENING` (`zoe/voice_loop.py:1152`). |
| `model` | `hey_jarvis` | `'hey_jarvis'` | `WAKE_WORD_MODEL`<br>`config.py:408` | `tests/test_phase8_wake.py`, `zoe/wake_word.py` | Legacy single model, used only when `phrases` is absent. |
| `phrases` | *list of 2* | — *(no default: absent key yields `None`)* | `WAKE_WORD_PHRASES`<br>`config.py:454` | `tests/test_phase8_wake.py`, `zoe/voice_loop.py` | Multi-phrase configuration. Present list wins; `model`/`threshold` are the fallback only when this key is **absent**. Disabled entries are skipped silently; an *enabled* entry with no model or an out-of-range threshold is dropped with a warning. Only `hey_jarvis` has a verified built-in — see `wake_phrases.md`. |
| `threshold` | `0.5` | `0.5` | `WAKE_WORD_THRESHOLD`<br>`config.py:409` | `tests/test_phase8_wake.py`, `zoe/wake_word.py` | Legacy threshold, same. |

## `vad`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `barge_in_grace_ms` | `0` | `0` | `BARGE_IN_GRACE_S`<br>`config.py:580` | `zoe/voice_loop.py` | Speech starting this soon after our own playback is ignored as echo tail. **Off by default** — it also delays a genuine early interruption by the same amount, and the echo canceller is the real fix. Try 800–1000 on a laptop speaker; leave 0 with headphones. |
| `barge_in_ms` | `300` | `300` | `VAD_BARGE_IN_MS`<br>`config.py:567` | `zoe/audio.py` | Voiced time needed to interrupt. Higher than `min_speech_ms` on purpose: interrupting mid-sentence should take intent. |
| `barge_in_snr` | `4.0` | `4.0` | `VAD_BARGE_IN_SNR`<br>`config.py:581` | `zoe/audio.py` | Larger margin for barge-in. |
| `echo_guard_adaptive_bar` | `true` | `true` | `VAD_ECHO_GUARD_ADAPTIVE_BAR`<br>`config.py:604` | `zoe/audio.py` |  |
| `echo_guard_enabled` | `true` | `true` | `VAD_ECHO_GUARD_ENABLED`<br>`config.py:588` | `zoe/audio.py` | Gate barge-in on the residual after subtracting a learned estimate of our own playback. |
| `echo_guard_gain_max` | `1.5` | `1.5` | `VAD_ECHO_GUARD_GAIN_MAX`<br>`config.py:596` | `zoe/audio.py` | Ceiling on the learned per-band echo gain, so the guard can never subtract an arbitrary multiple of what we played. |
| `echo_guard_min_consecutive_frames` | `3` | `3` | `VAD_ECHO_GUARD_MIN_CONSECUTIVE_FRAMES`<br>`config.py:591` | `zoe/audio.py` | 20 ms frames the residual must stay above the bar, so one echo spike cannot cut a reply. |
| `end_silence_ms` | `700` | `700` | `VAD_END_SILENCE_MS`<br>`config.py:561` | `zoe/audio.py` | Quiet time that ends the utterance. |
| `energy_threshold` | `0.01` | `0.01` | `VAD_ENERGY_THRESHOLD`<br>`config.py:559` | `zoe/audio.py` | RMS (0–1) at which a frame counts as voiced. |
| `min_speech_ms` | `200` | `200` | `VAD_MIN_SPEECH_MS`<br>`config.py:564` | `zoe/audio.py` | Voiced time before a burst is a real utterance. Filters clicks and taps. |
| `snr` | `3.0` | `3.0` | `VAD_SNR`<br>`config.py:578` | `zoe/audio.py` | Speech must clear the measured room floor by this margin. |

## `conversation`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `follow_up_window_s` | `8` | `8.0` | `FOLLOW_UP_WINDOW_S`<br>`config.py:608` | `zoe/voice_loop.py` | Keep listening this long after speaking, so a follow-up needs no wake word. |
| `frame_ms` | `80` | `80` | `AUDIO_FRAME_MS`<br>`config.py:470` | `zoe/audio.py`, `zoe/wake_word.py` | Analysis frame; ~200 wake-word inferences per second at 80 ms. |
| `input_device` | *(absent)* | — *(no default: absent key yields `None`)* | `AUDIO_INPUT_DEVICE`<br>`config.py:471` | `zoe/audio.py` | Microphone index. Unset = system default. |
| `output_device` | *(absent)* | — *(no default: absent key yields `None`)* | `AUDIO_OUTPUT_DEVICE`<br>`config.py:472` | `zoe/speech_pipeline.py` | Speaker index. Unset = system default. |
| `sample_rate` | `16000` | `16000` | `AUDIO_SAMPLE_RATE`<br>`config.py:468` | `zoe/audio.py`, `zoe/wake_word.py` | int16 mono. Shared by wake word, VAD and ASR. |
| `tts_chunk_ms` | `220` | `220` | `TTS_CHUNK_MS`<br>`config.py:655` | `zoe/speech_pipeline.py` | Synthesis chunk size. Smaller starts audio sooner and cuts barge-in in faster; larger plays smoother. |

## `audio`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `input_reopen_attempts` | `5` | `5` | `INPUT_REOPEN_ATTEMPTS`<br>`config.py:479` | `zoe/voice_loop.py` | Consecutive reopen failures before the assistant says it lost the microphone and rests. |
| `input_reopen_backoff_ms` | `500` | `500` | `INPUT_REOPEN_BACKOFF_MS`<br>`config.py:481` | `zoe/voice_loop.py` | Base backoff; doubles each attempt (0.5 s, 1 s, 2 s), capped at 5 s. |
| `profiles` | {`builtin_speaker`} | `{}` | `AUDIO_PROFILES`<br>`config.py:482` | — |  |

## `voice`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `ack_delay_ms` | `800` | `800` | `ACK_DELAY_MS`<br>`config.py:618` | `zoe/voice_loop.py` | Delay before the ack. Median thinking is ~4.5 s. |
| `ack_enabled` | `true` | `true` | `ACK_ENABLED`<br>`config.py:615` | `zoe/voice_loop.py` | Speak a filler while the model thinks, so a slow reply does not read as a hang. Plays through the same playback queue, so barge-in still applies. |
| `ack_mode` | `cached` | `'cached'` | `ACK_MODE`<br>`config.py:621` | `zoe/voice_loop.py` | `cached` = fixed clips, no model call; `model` = ask the configured model. |
| `max_output_tokens` | `220` | `220` | `VOICE_MAX_OUTPUT_TOKENS`<br>`config.py:633` | `tests/test_voice_conciseness.py`, `zoe/brain.py` |  |
| `max_spoken_words` | `60` | `60` | `MAX_SPOKEN_WORDS`<br>`config.py:625` | `tests/test_voice_conciseness.py`, `zoe/brain.py`, `zoe/voice_loop.py` | Hard cap on spoken replies. Longer replies are truncated at a sentence boundary and more detail is offered. |
| `text_max_output_tokens` | *(absent)* | — *(no default: absent key yields `None`)* | `TEXT_MAX_OUTPUT_TOKENS`<br>`config.py:636` | `zoe/brain.py` |  |

## `conversation_state`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `decay` | {`enabled`, `half_life_seconds`} | `{}` | `_TONE_DECAY`<br>`config.py:332` | — | Temporary state relaxes back to the defaults on this half-life, computed lazily on read. |
| `decay.enabled` | `true` | `true` | `TONE_DECAY_ENABLED`<br>`config.py:344` | `zoe/tone.py` | Master switch for decay. |
| `decay.half_life_seconds` | `300` | `300.0` | `TONE_DECAY_HALF_LIFE`<br>`config.py:345` | `zoe/tone.py` | Half-life of the relaxation back to defaults. Decay is applied on read, not on a timer. |
| `defaults` | {`mood`, `conversation_mode`, `energy`, …} | `{}` | `_TONE_DEFAULTS`<br>`config.py:331` | — | Resting tone. Personality is the identity prompt in `zoe/config.py`, not configured here. |
| `defaults.conversation_mode` | `casual` | `'casual'` | `TONE_MODE_DEFAULT`<br>`config.py:337` | `zoe/tone.py` | Resting conversation mode. |
| `defaults.curiosity` | `0.6` | `0.6` | `TONE_CURIOUSITY_DEFAULT`<br>`config.py:340` | `zoe/tone.py` | Resting curiosity. |
| `defaults.energy` | `0.55` | `0.55` | `TONE_ENERGY_DEFAULT`<br>`config.py:338` | `zoe/tone.py` | Resting energy. |
| `defaults.mood` | `neutral` | `'neutral'` | `TONE_MOOD_DEFAULT`<br>`config.py:336` | `zoe/tone.py` | Resting mood. |
| `defaults.warmth` | `0.75` | `0.75` | `TONE_WARMTH_DEFAULT`<br>`config.py:339` | `zoe/tone.py` | Resting warmth. |
| `enabled` | `true` | `true` | `TONE_ENABLED`<br>`config.py:334` | `zoe/tone.py` | Master switch for conversation tone. |

## `proactive`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `confidence_threshold` | `0.5` | `0.5` | `PROACTIVE_CONFIDENCE_THRESHOLD`<br>`config.py:671` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | A candidate below this is not even considered. |
| `dedup_window_seconds` | `600` | `600.0` | `PROACTIVE_DEDUP_S`<br>`config.py:668` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | The same reason will not trigger twice inside this window. |
| `enabled` | `true` | `true` | `PROACTIVE_ENABLED`<br>`config.py:666` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | Master switch for speaking first. Conservative by default: the answer is NO unless every gate passes. |
| `global_cooldown_seconds` | `300` | `300.0` | `PROACTIVE_COOLDOWN_S`<br>`config.py:667` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | Quiet period after any proactive interaction. |
| `max_interactions_per_window` | `3` | `3` | `PROACTIVE_MAX_PER_WINDOW`<br>`config.py:669` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | Cap inside the rolling window. |
| `min_repeat_observations` | `2` | `2` | `PROACTIVE_MIN_REPEAT`<br>`config.py:672` | `tests/test_phase6_proactive.py`, `zoe/proactive.py` | Consecutive readings before a candidate is considered. |
| `quiet_hours` | {`enabled`, `start_hour`, `end_hour`} | `{}` | `_PROACTIVE_QUIET`<br>`config.py:664` | — | Suppress proactive speech regardless of other gates. Ships disabled. |
| `quiet_hours.enabled` | `false` | `false` | `PROACTIVE_QUIET_ENABLED`<br>`config.py:673` | `zoe/proactive.py` | Master switch for the quiet window. |
| `quiet_hours.end_hour` | `8` | `8` | `PROACTIVE_QUIET_END`<br>`config.py:675` | `zoe/proactive.py` | Hour the window closes. The window wraps midnight when start > end. |
| `quiet_hours.start_hour` | `22` | `22` | `PROACTIVE_QUIET_START`<br>`config.py:674` | `zoe/proactive.py` | Hour the window opens (24-hour clock). |
| `window_seconds` | `3600` | `3600.0` | `PROACTIVE_WINDOW_S`<br>`config.py:670` | `zoe/proactive.py` | Rolling window length. |

## `vision`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `enabled` | `true` | `true` | `VISION_ENABLED`<br>`config.py:359` | `zoe/webcam.py` | Images you attach are always analysed. There is nothing to enable for that. |
| `webcam` | {`enabled`, `device`, `interval_seconds`, …} | `{}` | `_WEBCAM`<br>`config.py:357` | — | **The camera is never opened unless `webcam.enabled` is true.** A deliberate opt-in; nothing the camera sees is written to memory. |
| `webcam.analysis_cooldown_seconds` | `120` | `120.0` | `WEBCAM_ANALYSIS_COOLDOWN`<br>`config.py:370` | `zoe/webcam.py` | Hard ceiling on vision model calls. |
| `webcam.change_noise_floor` | `0.06` | `0.06` | `WEBCAM_CHANGE_NOISE_FLOOR`<br>`config.py:385` | `zoe/vision_change.py` | Brightness move counted as a real change. |
| `webcam.change_threshold` | `0.02` | `0.02` | `WEBCAM_CHANGE_THRESHOLD`<br>`config.py:384` | `zoe/vision_change.py` | Fraction of the frame that must have changed. Catches a person arriving; ignores lighting drift. Fine changes such as glasses score ~0.012 — lower this to try. |
| `webcam.comparison_size` | `32` | `32` | `WEBCAM_SAMPLE_SIZE`<br>`config.py:386` | `zoe/vision_change.py` | Edge size frames are reduced to before comparison. |
| `webcam.context_ttl_seconds` | `600` | `600.0` | `WEBCAM_CONTEXT_TTL`<br>`config.py:390` | `zoe/vision.py` | How long visual context stays usable. |
| `webcam.device` | `0` | `0` | `WEBCAM_DEVICE`<br>`config.py:362` | `zoe/webcam.py` | 0 = default camera. |
| `webcam.enabled` | `false` | `false` | `WEBCAM_ENABLED`<br>`config.py:361` | `zoe/webcam.py` | **The opt-in itself.** `false` in the shipped config, so no camera is opened by starting the assistant. |
| `webcam.interval_seconds` | `10` | `10.0` | `WEBCAM_INTERVAL_SECONDS`<br>`config.py:366` | `zoe/webcam.py` | Seconds between sampled frames. |
| `webcam.jpeg_quality` | `70` | `70` | `WEBCAM_JPEG_QUALITY`<br>`config.py:395` | `zoe/vision.py` | JPEG quality of uploaded frames. |
| `webcam.max_frame_side` | `640` | `640` | `WEBCAM_MAX_SIDE`<br>`config.py:394` | `zoe/vision.py` | Downscale before analysis. |
| `webcam.max_observations` | `6` | `6` | `WEBCAM_MAX_OBSERVATIONS`<br>`config.py:391` | `zoe/vision.py` | Observations kept in the current visual context. |

## `file_access`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `allowed_roots` | `['.', 'data']` | `['.', 'data']` | `_allowed_file_roots`<br>`config.py:74` | — | Roots the directory-listing tool may read. Default deny; symlink targets are resolved before the check. |

## `file_reader`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `max_bytes` | `20971520` | *(computed)* | `FILE_READER_MAX_BYTES`<br>`config.py:643` | — | Size ceiling for anything the plugin will read. |
| `max_chars` | `12000` | `12000` | `FILE_READER_MAX_CHARS`<br>`config.py:648` | — |  |
| `max_pdf_pages` | `50` | `50` | `FILE_READER_MAX_PDF_PAGES`<br>`config.py:652` | — |  |
| `roots` | `['~/Documents', '~/Downloads', '~/Desktop']` | — *(no default: absent key yields `None`)* | `FILE_READER_ROOTS`<br>`config.py:642` | — | Approved roots for the `file_reader` plugin. Outside them, or a symlink resolving outside them, is rejected. Distinct from `file_access.allowed_roots`, which governs the directory-listing built-in. |
| `summary_chars` | `2000` | `2000` | `FILE_READER_SUMMARY_CHARS`<br>`config.py:649` | — |  |

## `weather`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `default_city` | `Faridabad` | `'Faridabad'` | `DEFAULT_CITY`<br>`config.py:218` | `zoe/brain.py`, `zoe/tools/weather.py` | City used when a weather request names none. |
| `timeout_seconds` | *(absent)* | `10` | `WEATHER_TIMEOUT_S`<br>`config.py:224` | `zoe/tools/weather.py` | Per-request timeout. Absent from config.yaml, so the default applies. |

## `space`

**Present in `config.yaml`:** **no — defaults apply**

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `timeout_seconds` | *(absent)* | `10` | `NASA_TIMEOUT_S`<br>`config.py:225` | `zoe/tools/nasa.py` | Per-request timeout for the NASA tool. **Not present in config.yaml** — the default applies. |

## `news`

**Present in `config.yaml`:** **no — defaults apply**

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `timeout_seconds` | *(absent)* | `10` | `NEWS_TIMEOUT_S`<br>`config.py:229` | `zoe/tools/news.py` | Per-feed timeout for news retrieval; each source is fetched independently. **Not present in config.yaml** — the default applies. |

## `locations`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `default_maps_query` | `Delhi` | `'Delhi'` | `DEFAULT_MAPS_QUERY`<br>`config.py:219` | `zoe/tools/web.py` | Target for map searches when none is given. |

## `contacts`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `*(whole section)*` | `None` | `{}` | `CONTACTS`<br>`config.py:239` | `zoe/tools/email_tool.py` | Alias → email address for the email tool. All entries ship commented out. |

## `applications`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `aliases` | {`WhatsApp`, `Safari`, `Terminal`} | `{}` | `APP_COMMANDS / APP_DISPLAY_NAMES`<br>`config.py:293` | — | The launch allowlist. **This map is the allowlist**: anything not a key is refused, and no shell is ever used. A name that is not mapped is looked up among the applications installed on this machine and must be confirmed by the user before it opens — see `scripts/list_apps.py` for filling it in. |

## `logging`

**Present in `config.yaml`:** yes

| Key | Shipped | Default | Constant | Consumers | What it controls |
| --- | --- | --- | --- | --- | --- |
| `backup_count` | `3` | `3` | `LOG_BACKUP_COUNT`<br>`config.py:680` | — | Rotated log files kept. |
| `level` | `INFO` | `'INFO'` | `LOG_LEVEL`<br>`config.py:681` | — | DEBUG, INFO, WARNING, ERROR, CRITICAL. |
| `max_bytes` | `1000000` | `1000000` | `LOG_MAX_BYTES`<br>`config.py:679` | — | Rotate the log at this size. |

## Keys `config.py` never reads

None. Every top-level key in `config.yaml` is accounted for above — this is
checked on every generation, and the script fails if a key appears that no read
covers. Two of the reads happen outside `zoe/config.py` and are listed here
so the table above is not mistaken for the whole tree:

- `applications` — `zoe/tools/apps.py:167`
- `file_reader` — `plugins/file_reader.py:51`
- `file_reader` — `tests/test_plugins.py:535`
- `plugins` — `zoe/plugins.py:209`
- `plugins` — `zoe/plugins.py:50`
- `voice` — `tests/test_voice_conciseness.py:34`
- `wake_word` — `tests/test_phase4_voice.py:391`

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
