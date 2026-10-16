# Status

Every acceptance item and its evidence, in one table. This merges
`PHASE_E_COMPLETION_REPORT.md`, `PHASE_E_FULL_SUITE_REVALIDATION.md` and
`final_verification_phase10.md`; the originals are preserved unedited in
[`archive/`](archive/).

Read the status column literally. **NOT VERIFIED** means nobody has run it on
real hardware — not that it is probably fine. The distinction is the whole
point of this file, and the previous version of it was honest about it, so it
stays that way.

Test counts are deliberately absent. They drift with every test added, and five
different numbers had accumulated across the old docs. The dated record is
[`archive/baseline_mac.txt`](archive/baseline_mac.txt); run the suite for a
current one.

---

## Acceptance criteria — Phase E (Voice Repair specification v1.0)

The specification defines Phases A–E only. There is no Phase F; `Spec2.pdf`,
which contains one, was explicitly not used as authority.

| ID | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| **AC-1** | Microphone capture works and failures are visible | **PASS** | `scripts/mic_check.py`: device opens, audio arrives, exits `OK. Audio is arriving.` Operator observed RMS rising ~0.05 → 0.46–0.74 while speaking |
| **AC-2** | Only one component opens the microphone | **PASS** | 0 PyAudio references in `zoe/`, `scripts/`, config. `tests/test_phase4_voice.py` asserts `PyAudioSource(` never appears in the loop. `zoe/audio.py` is the sole device owner |
| **AC-3** | A spoken request gets a transcript and a spoken reply with no wake word | **NOT VERIFIED** | Requires a human speaking to a live instance |
| **AC-4** | Speech uses Kokoro with audible pauses at punctuation | **NOT VERIFIED** | Synthesis is automated and correct — 4 pauses for 4 marks, reproducible — but nobody has confirmed the audio is **audible and intelligible** |
| **AC-5** | Speech output is measured for speed and first-audio time | **PARTIAL** | RTF 0.18 and transcription 0.47 s measured. First-audio measured at 1.611 s median warm / 2.329 s cold — but that figure **excludes the model provider round trip**, so the 2.0 s target is **not claimed met** |
| **AC-6** | The test suite passes, and removed tests are removed for stated reasons | **PASS** | `python -m pytest -q` exits 0. Retirements are justified in `archive/PHASE_E_COMPLETION_REPORT.md` §5 and were recorded, not hidden |
| **AC-7** | Chatterbox and PyAudio are gone from the code path and dependencies | **PASS** for production/config | 0 matches in `zoe/`, `scripts/`, `config.yaml`, `requirements.txt`. See the interpretation note below |
| **AC-8** | Text mode still works, and the assistant exits cleanly on Ctrl+C | **NOT VERIFIED** | No human-observed evidence was gathered |

### AC-7 interpretation — still an open decision

E1's literal reading ("zero occurrences anywhere") is **not met and is not
achievable**: `__pycache__` re-embeds those literals on every test run, so a
whole-tree literal zero can never hold. The residual matches in `tests/` are
deliberate absence-assertions — for example
`assert source.count("PyAudioSource(") == 0` in `tests/test_phase4_voice.py`,
which is AC-2's own evidence and only means something if it names the removed
API.

This is a **documented interpretation, not literal whole-repository compliance,
and it has never been explicitly signed off.**

### Determination: Phase E is NOT COMPLETE

The automated half is green. It is still not complete because AC-3, AC-4 and
AC-8 have never been exercised on the target machine, AC-5 is partial by its own
account, AC-7 rests on an unsigned interpretation, and audio crackling was
explicitly deferred by the operator and is not resolved.

A green suite is necessary but not sufficient. The specification's exit
condition is *"every criterion below passes **on the target machine**"*, and that
last clause is doing real work.

---

## Latency (E4)

Measured on the development machine. Method and exclusions are recorded beside
each number; full record in [VOICE_LATENCY.md](VOICE_LATENCY.md).

| Measurement | Value | Method | Target | Result |
| --- | --- | --- | --- | --- |
| Kokoro warm RTF | 0.17–0.18, median 0.18 | `scripts/tts_check.py`, 2 fresh processes | < 1.0 | **MET** |
| Kokoro cold start | 8.77–9.23 s | fresh interpreter | reported, not gated | reported |
| Transcription, 3.48 s clip | 0.47 s median warm, 1.90 s cold | timed `Transcriber.transcribe()` | < 1.0 s | **MET** |
| End of speech → first audio | 1.611 s median warm (n=4); 2.329 s cold | `scripts/first_audio_check.py`, marks `speech_end` → `first_audio` written by production code | < 2.0 s | **NOT CLAIMED** — excludes the provider round trip |

The first-audio figure is a subtraction of two marks the production code wrote
itself, through the real VAD, the real local transcriber and the real Kokoro
engine. Because the reply is scripted, the real total is this figure **plus the
provider round trip**, which is why the target is not claimed.

Two harness errors were found and fixed before these numbers were believed; both
had produced badly wrong figures and both are recorded in `VOICE_LATENCY.md`:
room tone generated at exactly `vad.energy_threshold`, so the detector fired on
noise and split the utterance; and a synthetic source returning `None`
immediately, which spins `MicrophoneStream._pump` because that branch has no
sleep. The first is a harness mistake. The second is a real property of the
code — production is unaffected because real PortAudio blocks.

---

## Capability status

| Capability | Status | Notes |
| --- | --- | --- |
| Offline test suite | **VERIFIED** | The main correctness gate |
| Local intent bypass | **VERIFIED** | 6 intents, zero model calls on a match |
| Model routing, fallback, capability gating | **VERIFIED** | Deterministic scoring; offline tests |
| Memory (SQLite facts + session history) | **VERIFIED** | Offline |
| Application launch allowlist | **VERIFIED** | Default-deny, argument lists, never a shell |
| Email confirmation gate | **VERIFIED** | No path by which the model can send |
| Plugin loader | **VERIFIED** | Validation, isolation, timeout |
| Startup self-check | **VERIFIED** | `python -m zoe --status` |
| `python -m zoe.main --test` | **VERIFIED** | 8 pipeline checks |
| Gemini provider | **VERIFIED** | Confirmed responding live when the primary model was quota-limited |
| OpenRouter models | **UNVERIFIED** | Catalog-verified (1M context, tools). Live calls never made: no key configured at the time |
| OpenCode Zen `space-bunny-free` | **UNVERIFIED** | Catalog-verified only |
| OpenCode Zen `big-pickle` | **DISABLED, and unusable** | Live call returns HTTP 403 `FreeTierError` — free tier only works from inside the OpenCode client. Because a 403 classifies as a non-retryable auth error, selecting it aborted the whole request instead of falling back |
| Microphone capture | **PARTIAL** | Capture proven (AC-1). Wake latency on device not measured |
| Wake word on real audio | **UNVERIFIED** | Engine exercised offline; `hey_jarvis` is the only model with a verified built-in |
| Kokoro output audible and intelligible | **UNVERIFIED** | Synthesis measured and correct; audibility is a human observation |
| Barge-in | **PARTIAL** | Guard, grace and residual gating all tested. Reverberant echo is a measured, documented gap (`tests/test_echo_reverb.py` xfail) |
| Webcam perception | **UNVERIFIED** | Synthetic frames exercise the real loop and the real model; no camera has been driven |
| Live tool calls needing accounts (NASA, news, dictionary, Wikipedia) | **UNVERIFIED** | Network paths untested end to end |
| SMTP delivery | **UNVERIFIED** | The gate is verified offline; actual delivery is not |
| Proactive speech | **PARTIAL** | Gating logic verified offline; one real generation only with a provider configured |
| Audio crackling | **NOT RESOLVED** | Reported and explicitly deferred by the owner |

---

## Phase-by-phase verification

From `archive/final_verification_phase10.md`, retained as recorded.

| Phase | Verified by | Result |
| --- | --- | --- |
| 1 logging/output | suite + live smoke | clean normal, full debug |
| 2 local intents | suite + live local answer | provider bypass holds |
| 3 app launch | suite + grep (no shell) | allowlist deny-by-default holds |
| 4 email gate | suite + code audit (single SMTP choke point) | gate intact, no second path |
| 5 TTS baseline | doc intact, numbers preserved | untouched |
| 6 persistent TTS | suite + audit | singleton/conds/warm verified |
| 7 lifecycle | suite + live debug trace | full cycle observed |
| 8 wake phrases | suite + `wake_eval.py` re-check | `hey_jarvis` only; rest disabled |
| 9 tracing | suite + live trace line | honest mocked/live split |

Two defects were found and fixed during Phase 10, both **test-only**:

- **P2, FIXED** — standalone `tests/test_phase6_tts_service.py` segfaulted (exit
  139) at torch re-import. `patch.dict(sys.modules)` snapshots at enter and
  evicts modules imported inside the block; re-executing torch's native init
  segfaults. The full suite masked it, because collection pre-imported torch via
  another file. Fixed with a guarded top-level `import torch`, following the
  convention the suite already used elsewhere. Production untouched.
- **P3, noted, unchanged** — `torch` 2.14.1 on system Python is natively
  fragile. Environmental. The project venv (torch 2.6.0) is the supported
  runtime.

No P0 or P1 defects were found.

Three genuine **production** defects were found and fixed during Phase E:

- `handle_weather` dispatched `get_temperature`, a name never in
  `TOOL_REGISTRY`. Every weather request returned `Unknown tool`.
- `VoiceLoop.start()` returned `True` when the wake model was unavailable,
  reporting voice as active while the only thing that could leave `IDLE` was
  dead. It now returns `False` and opens no microphone.
- `VoiceLoop.stop()` never closed the wake engine, leaking the ONNX session for
  the process lifetime.

Plus two latent ones: an engine close skipped on the microphone-failure path,
and a `_process_lock.release()` that a failed cleanup step could skip, leaking
device ownership so no later `start()` could ever open the microphone.

---

## Outstanding, needs a human at a machine

The ordered procedure is [archive/PHASE_E_MANUAL_CHECKLIST.md](archive/PHASE_E_MANUAL_CHECKLIST.md).
Nothing in it may be marked PASS from the file itself.

1. **AC-3** — speak a request to a live instance, get a transcript and a spoken
   reply, with no wake word.
2. **AC-4** — confirm Kokoro audio is audible and intelligible, with pauses at
   punctuation.
3. **AC-8** — text mode works and Ctrl+C exits cleanly.
4. **AC-5** — measure end-of-speech-to-first-audio *including* the provider
   round trip. The current figure excludes it and no target is claimed.
5. **AC-7** — sign off the interpretation above, or reject it.
6. **Audio crackling** — reported, deferred, not investigated.
7. Live provider round trips for OpenRouter and OpenCode Zen.
8. Real SMTP delivery to a test address.
9. Webcam capture on a real camera.

## Dated records

These are snapshots taken at a point in time and are not updated:

| File | What it records |
| --- | --- |
| [`archive/baseline.txt`](archive/baseline.txt) | Phase 0 baseline, 2026-10-08 |
| [`archive/baseline_mac.txt`](archive/baseline_mac.txt) | Full pytest run output, the dated suite record |
| [`archive/baseline_tests_phase1.txt`](archive/baseline_tests_phase1.txt) | Phase 1 focused run |
| [`archive/PHASE_E_COMPLETION_REPORT.md`](archive/PHASE_E_COMPLETION_REPORT.md) | The Phase E determination this page summarises |
| [`archive/PHASE_E_FULL_SUITE_REVALIDATION.md`](archive/PHASE_E_FULL_SUITE_REVALIDATION.md) | The pre-fix suite state: 11 failed, 32 errors |
| [`archive/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md`](archive/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md) | The `first_audio` regression and its fix |
| [`archive/final_verification_phase10.md`](archive/final_verification_phase10.md) | Release-readiness verdict, "READY WITH KNOWN LIMITATIONS" |
| [`archive/REPAIR_LOG.md`](archive/REPAIR_LOG.md) | Reconstruction of the damaged archive, file by file |
| [`archive/PHASE0_DIAGNOSIS.md`](archive/PHASE0_DIAGNOSIS.md) | The Gemini-only system as it was on 2026-10-06. `tests/test_phase0_regression.py` pins a bug from it |

Several of these describe files and behaviour that no longer exist — the
Chatterbox engine, the wake-word listener, a 977-test suite. They are kept
because they are the record of what was decided, not of what is there.