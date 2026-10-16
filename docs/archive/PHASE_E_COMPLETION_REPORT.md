# Phase E Completion Report

**Specification:** `AiPartner_Voice_Repair_Spec.pdf` — "Voice Repair and TTS
Upgrade Specification", **version 1.0, dated 10 October 2026** (24 pages).
`AiPartner_Voice_Repair_Spec2 .pdf` was **not** used as authority.

**Report date:** 2026-10-12
**Branch:** `voice-repair`
**HEAD at start and at finish:** `ddbff2e fix: load wake word model before microphone startup` (unchanged; nothing staged or committed)
**Interpreter:** `.venv/bin/python` — Python 3.12.15, kokoro 0.9.4, soundfile 0.14.0

Prior evidence preserved in this directory and not overwritten:
`PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md`, `PHASE_E_FULL_SUITE_REVALIDATION.md`,
`VOICE_LATENCY.md`.

---

## 1. Scope note: there is no Phase F

The authoritative specification defines **Phases A through E only**. A search of
the full extracted text returns no occurrence of "Phase F" (or any phase beyond
E), and its scope block reads:

> Scope of this version — Phase A (microphone capture), Phase B (local TTS with
> Kokoro, transcription), Phase C (voice loop fixes), Phase D (test updates),
> Phase E (verification).
> **Deferred to later specs** — LLM fallback routing, webcam identification and
> proactive speech, IoT and smart home control, local tool integrations.

Phase F exists only in `AiPartner_Voice_Repair_Spec2 .pdf`, which substitution was
explicitly prohibited. No Phase F work was started, planned, or invented.

---

## 2. Initial and final working-tree state

**Start:** 24 modified, 2 deleted, 4 untracked.
**Finish:** 30 modified, 2 deleted, 4 untracked — the 6 additional modifications
are this report's own fixes (section 5). The 2 deletions
(`tests/test_tts_say.py`, `tests/test_wake_listener_lifecycle.py`) and the 4
untracked files are preserved untouched.

`git diff --check` → exit 0, no output, at start and at finish.
`git diff --cached --stat` → empty. Nothing staged, nothing committed.

---

## 3. Test results

| Command | Passed | Failed | Errors | Skipped | Exit |
|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/ -q` (baseline) | 1118 | 11 | 32 | 0 | 1 |
| `.venv/bin/python -m pytest tests/ -q` (finish) | **1171** | **0** | **0** | 0–1 | **0** |

The suite reports 1170 passed + 1 skipped on some runs and 1171 passed on
others. The variance is a single pre-existing **live-network** test,
`tests/test_providers.py::TestLiveProviders::test_gemini_live_round_trip`, which
skips when `GEMINI_API_KEY` is unset. Verified in isolation: it skips with
`GEMINI_API_KEY not configured` and is unrelated to this work. It was not
suppressed, skipped artificially, or converted to a pass.

### Focused suites (all run after the final edit)

| Suite | Result | Exit |
|---|---|---|
| `tests/test_phase4_email.py` | 34 passed | 0 |
| `tests/test_routing.py` | 95 passed | 0 |
| `tests/test_phase2_intents.py` | 53 passed | 0 |
| `tests/test_phase8_wake.py` | 27 passed | 0 |
| `tests/test_phase4_voice.py` | 178 passed, 2 warnings | 0 |
| `tests/test_phase7_lifecycle.py` | 21 passed | 0 |
| `tests/test_phase9_trace.py` | 15 passed | 0 |
| `tests/test_phase5_tone.py` | 119 passed | 0 |
| `tests/test_startup.py` | 18 passed | 0 |
| `tests/test_integration.py` | 21 passed | 0 |

The Phase 4 audio/cancellation suite still passes at **178 passed in 23.9 s**,
matching its pre-change runtime. The PortAudio cancellation and stream-lifecycle
protections were not modified.

---

## 4. Static checks (E1)

The specification's three commands were run verbatim.

| Check | Production + configuration | Tests | Generated `__pycache__` |
|---|---|---|---|
| `chatterbox` | **0** | 29 | regenerates on each pytest run |
| `pyaudio` | **0** | 4 | regenerates on each pytest run |
| `run_wake_word_mode\|WakeWordListener\|wait_for_wake_word` | **0** (all paths) | 0 | — |

`prefer_free`, the removed routing flag, has **zero** references in `jarvis/` and
`config.yaml`.

**Interpretation applied:** E1's intent is that removed dependencies are gone
from the code path. `jarvis/`, `scripts/`, `config.yaml` and `requirements.txt`
are clean. The residual test matches are deliberate absence-assertions that only
mean something if they name the removed API — for example
`assert source.count("PyAudioSource(") == 0` in `tests/test_phase4_voice.py`,
which is AC-2's own evidence. Whole-tree literal zero output is unachievable
because `__pycache__` re-embeds those literals on every test run. **This is a
documented interpretation, not literal whole-repository compliance, and it
remains an item for explicit sign-off.**

---

## 5. Code changes made in this work

| File | Change | Why |
|---|---|---|
| `jarvis/intents/local_intents.py` | `handle_weather` dispatches `get_weather`, not `get_temperature` | **Genuine production defect.** Every weather request returned `"Error: Unknown tool 'get_temperature'."` The name was never in `TOOL_REGISTRY` |
| `jarvis/voice_loop.py` | `start()` returns `False` when the wake model is unavailable, before opening the microphone | **Genuine production defect.** The docstring already promised this; the code returned `True`, reporting voice as active while the only thing that can leave IDLE was dead. Microphone is never opened on this path |
| `jarvis/voice_loop.py` | `start()` releases the engine on the microphone-failure path | Partial-startup leak: the engine had already loaded and held a real ONNX session |
| `jarvis/voice_loop.py` | `stop()` now closes the wake engine, after the stream stops | **Genuine production defect.** The loop constructs and loads the engine and nothing else closed it, leaking the ONNX session for the process lifetime. Closing happens after `microphone.stop()` so no inference can be in flight |
| `jarvis/voice_loop.py` | engine close is duck-type tolerant (`getattr`) | `VoiceLoop`'s engine is a documented duck-typed contract; test doubles such as `RecordingEngine` have no `close()` |
| `jarvis/voice_loop.py` | `self._process_lock.release()` moved into the `finally` | **Latent defect exposed during this work.** A single failed cleanup step raised before the release, leaking device ownership so no later `start()` could ever open the microphone |
| `jarvis/voice_loop.py` | `_wake_fired` contains and logs inference exceptions | **Genuine production defect.** An exception from `scores()` escaped on the microphone callback thread and killed the stream, leaving the assistant deaf |
| `jarvis/voice_loop.py` | `StatusIndicator.wake_detected(score)` invoked once per real wake | The indicator was defined but never called; the score was logged and discarded |
| `tests/test_phase4_email.py` | Migrated off removed `email_tool.speak`/`listen` and the deleted `handle_email_command`; gate now asserted through `parse_confirmation` + `confirmed=` | Test encoded an obsolete API. All security coverage preserved and extended |
| `tests/test_routing.py` | `TestPreferFree` replaced by `TestTierOrdering` | `RoutingConfig.prefer_free` was intentionally removed; production is tier-based and correct |
| `tests/test_phase2_intents.py` | Weather stub and registry assertions moved to `get_weather`; added a regression test for the unknown-tool defect | Test assumed a tool name that does not exist |
| `docs/VOICE_LATENCY.md` | First-audio row measured and recorded with its exclusion; microphone criterion corrected to verified | The row had been PENDING since the specification was written |
| `docs/PHASE_E_COMPLETION_REPORT.md` | This report | — |
| `docs/PHASE_E_MANUAL_CHECKLIST.md` | **New.** Ordered manual checklist for the operator | Remaining checks need hardware |
| `scripts/first_audio_check.py` | **New.** Measures `speech_end` → `first_audio` through the real pipeline | AC-5's figure had no instrumentation at all |

### Tests migrated or retired, with justification

No test file was deleted to obtain a green suite. No assertion was weakened.

- **`test_interactive_handler_is_gated` → `test_the_gate_is_the_only_thing_that_sends`.**
  `email_tool.handle_email_command` no longer exists. The guarantee it made —
  contact resolution must not escape the confirmation gate — is re-expressed
  against the real boundary.
- **`test_repeated_tool_call_sends_once` → `test_repeated_tool_call_in_one_turn_sends_once`.**
  The old duplicate-send cache lived in the deleted handler. The real guard is
  `jarvis.brain._tool_cache_key`, which collapses side-effecting tools to the
  tool name. A second test was added for the sharp edge: an unconfirmed turn
  cannot be escalated into a send.
- **`test_warm_model_reports_ready_not_loading` → `test_first_audio_is_marked_when_a_reply_starts_playing`**
  (earlier work, documented in `PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md`).
- **`TestPreferFree` (4 tests) → `TestTierOrdering` (12 tests).** Tier precedence,
  ineligible models, within-tier ordering, determinism, key tie-break, capability
  gating over tier, fallback on failure, cap configuration, and registry
  `enabled` handling.

Two migration mistakes were made and corrected during this work, both recorded
rather than hidden: an ordering assertion on stage marks that rounding made
meaningless, and a duplicate-send test premised on a cache that does not exist.

---

## 6. Acceptance criteria (§9 of the specification)

| ID | Criterion | Status | Evidence |
|---|---|---|---|
| **AC-1** | Microphone capture works and failures are visible | **PASS** | `mic_check.py`: device opens, audio arrives, exit `OK. Audio is arriving.`; operator observed RMS rise ~0.05 → 0.46–0.74 while speaking |
| **AC-2** | Only one component opens the microphone | **PASS** | 0 PyAudio references in `jarvis/`, `scripts/`, config; regression guard `test_phase4_voice.py:2081` asserts `PyAudioSource(` never appears in the loop; `test_phase4_voice.py` 178 passed |
| **AC-3** | A spoken request gets a transcript and a spoken reply with no wake word | **NOT VERIFIED** | Requires a human speaking to a live instance |
| **AC-4** | Speech uses Kokoro with audible pauses at punctuation | **NOT VERIFIED** | Synthesis is automated and correct (4 pauses for 4 marks, reproducible). Nobody has confirmed the audio is **audible and intelligible** |
| **AC-5** | Speech output is measured for speed and first-audio time | **PARTIAL** | RTF 0.18 and transcription 0.47 s measured. First-audio now measured: **1.611 s median warm, 2.329 s cold**, but that figure **excludes the model provider round trip**, so the 2.0 s target is **not claimed met** |
| **AC-6** | The test suite passes, and removed tests are removed for stated reasons | **PASS** | `pytest tests/ -q` → 1171 passed, 0 failed, 0 errors, exit 0. Removals justified in section 5 |
| **AC-7** | Chatterbox and PyAudio are gone from the code path and dependencies | **PASS** (production/config) | 0 matches in `jarvis/`, `scripts/`, `config.yaml`, `requirements.txt`. Whole-tree literal silence not met — see section 4 |
| **AC-8** | Text mode still works, and the assistant exits cleanly on Ctrl+C | **NOT VERIFIED** | No human-observed evidence in this work |

---

## 7. Latency (E4)

| Measurement | Value | Method | Target | Result |
|---|---|---|---|---|
| Kokoro warm RTF | 0.17–0.18, median 0.18 | `scripts/tts_check.py`, 2 fresh processes | < 1.0 | **MET** |
| Kokoro cold start | 8.77–9.23 s | fresh interpreter | reported, not gated | reported |
| Transcription, 3.48 s clip | 0.47 s median warm, 1.90 s cold | timed `Transcriber.transcribe()` | < 1.0 s | **MET** |
| End of speech → first audio | 1.611 s median warm (n=4); 2.329 s cold | `scripts/first_audio_check.py`, marks `speech_end` → `first_audio` written by production code | < 2.0 s | **NOT CLAIMED** — excludes the provider round trip |

The first-audio row was PENDING until this session. It is now measured from the
marks `VoiceLoop` writes itself, through the real VAD, the real local transcriber
and the real Kokoro engine. The scripted reply means the provider round trip is
absent, so the real total is this figure plus that latency. The 2.0 s target is
therefore **not claimed**.

Two harness errors were found and fixed before these numbers were believed; both
had produced badly wrong figures, and both are recorded in `VOICE_LATENCY.md`:

- room tone generated at exactly `vad.energy_threshold`, so the detector fired on
  the noise and split the utterance into two empty fragments;
- the synthetic source returned `None` immediately, and
  `MicrophoneStream._pump` has no sleep on that branch, so the capture thread
  busy-spun and stretched synthesis of a 2 s phrase to 11.8 s.

The first is a harness mistake. The second is worth noting as a property of the
code: a source that yields `None` immediately will spin the capture thread.
Real PortAudio blocks, so production is unaffected.

---

## 8. Outstanding items requiring the operator

| Check | Command / procedure | Required observation |
|---|---|---|
| **All remaining checks** | see `docs/PHASE_E_MANUAL_CHECKLIST.md` | ordered, copy-pasteable, with the observable result to record |

The operator is currently unavailable for hardware work, so the checklist was
written to be run in one sitting rather than to be answered by inference.

---

## 9. Determination

# PHASE E IS **NOT** COMPLETE

The automated half is now green and stronger than at any earlier point in this
work: **AC-6 passes**, which previously did not. Three production defects were
found and fixed — a completely broken weather intent, an unreleased ONNX session,
and a leak of microphone ownership after any failed shutdown.

It is still not complete, because:

1. **AC-3, AC-4 and AC-8 are NOT VERIFIED.** Each requires a human to speak,
   listen and interact with a live instance. No automated proxy counts, and none
   has been substituted.
2. **AC-5 is PARTIAL** — end-of-speech-to-first-audio is now measured (1.611 s
   median warm), but that figure excludes the model provider round trip, so the
   specification's 2.0 s target is not claimed met.
3. **Audio crackling was explicitly deferred by the operator** and is not
   resolved.
4. **AC-7's whole-tree literal reading is unmet** and rests on a documented
   interpretation that has not been explicitly signed off.

A green suite is necessary but not sufficient here. The specification's exit
condition is *"The version is complete when every criterion below passes on the
target machine"* — "on the target machine" is doing real work in that sentence,
and three criteria have never been exercised on it.

### Gate to the next phase

**Not met.** There is no Phase F in this specification, and the gate is
independent of that: AC-3, AC-4, AC-5 and AC-8 are outstanding, so progression
would be premature regardless.
