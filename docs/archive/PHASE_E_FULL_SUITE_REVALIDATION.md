# Phase E — Full Test Suite Revalidation

**Report generated:** 2026-10-11
**Branch:** `voice-repair`
**HEAD:** `ddbff2e fix: load wake word model before microphone startup` (matches expected)
**Interpreter:** `.venv/bin/python` (Python 3.12.15)
**Scope:** verification only. No production code or tests were modified.

> This report revalidates the suite after the `first_audio` regression fix.
> It does **not** mark Phase E complete. See §9.

---

## 1. Headline result

| Check | Result | Exit |
|---|---|---|
| Phase 9 regression test | **1 passed** | **0** |
| Full suite `tests/` | **11 failed, 1118 passed, 4 warnings, 32 errors** | **1** |

The Phase 9 regression is **confirmed fixed**, including in full-suite context.
The full suite **does not pass**. **AC-6 is not satisfied.**

---

## 2. Initial repository state (recorded before any command)

```
branch: voice-repair
HEAD:   ddbff2e fix: load wake word model before microphone startup
git diff --check: exit 0, no output
docs/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md: present (13,595 bytes)
```

`git status --short` — 30 entries (24 modified, 2 deleted, 4 untracked):

```
 M config.yaml                              M scripts/tts_latency.py
 M data/kokoro_check.wav                    M scripts/wake_eval.py
 M jarvis/audio.py                          M start.py
 M jarvis/brain.py                          M tests/test_integration.py
 M jarvis/classify.py                       M tests/test_phase3_applaunch.py
 M jarvis/config.py                         M tests/test_phase4_voice.py
 M jarvis/intents/patterns.py               M tests/test_phase5_tone.py
 M jarvis/speech_pipeline.py                M tests/test_phase6_tts_service.py
 M jarvis/tools/apps.py                     M tests/test_phase7_lifecycle.py
 M jarvis/voice_loop.py                     M tests/test_phase9_trace.py
 M jarvis/wake_word.py                      M tests/test_startup.py
 M scripts/phase4_live_check.py
 M scripts/tts_benchmark.py
 D tests/test_tts_say.py
 D tests/test_wake_listener_lifecycle.py
?? docs/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md
?? docs/VOICE_LATENCY.md
?? jarvis/tools/news.py
?? tests/test_tts_engine.py
```

---

## 3. Regression test verification

Command:

```
.venv/bin/python -m pytest \
  tests/test_phase9_trace.py::TestTTSReadiness::test_first_audio_is_marked_when_a_reply_starts_playing -q
```

Output:

```
.                                                                        [100%]
1 passed in 2.09s
```

**Exit code: 0.** The test passes.

---

## 4. Full suite

Command:

```
.venv/bin/python -m pytest tests/ -q
```

Summary line:

```
11 failed, 1118 passed, 4 warnings, 32 errors in 43.22s
```

**Exit code: 1.**

| Metric | Count |
|---|---|
| Passed | 1118 |
| Failed | 11 |
| Errors | 32 |
| Skipped | 0 |
| Warnings | 4 |

No collection-time abort occurred; the suite ran to completion.

### 4.1 Failing tests — all 11

| File | Test | Reported message |
|---|---|---|
| `test_phase2_intents.py` | `TestProviderBypass::test_registry_slots_are_real_wrappers` | `KeyError: 'get_temperature'` |
| `test_phase2_intents.py` | `TestProviderBypass::test_weather_default_city` | `AssertionError: assert 'Delhi' in '32°C'` |
| `test_phase2_intents.py` | `TestProviderBypass::test_weather_with_city` | `AssertionError: assert ('Mumbai' in '32°C')` |
| `test_phase8_wake.py` | `TestLogging::test_wake_indicator_reports_actual_score` | set-comparison mismatch; `Right contains one more item: 0.91` |
| `test_phase8_wake.py` | `TestSharedPipeline::test_inference_exception_is_logged_and_does_not_kill_stream` | assertion failure in shutdown/close path (`start()` vs `FakeMultiEngine.closes`) |
| `test_phase8_wake.py` | `TestSharedPipeline::test_init_failure_does_not_report_dead_voice_as_active` | assertion failure |
| `test_phase8_wake.py` | `TestSharedPipeline::test_shutdown_cleans_up` | assertion failure |
| `test_routing.py` | `TestPreferFree::test_flag_is_read_not_dead_config` | set mismatch: `'free-a'` vs `'paid-pro'` |
| `test_routing.py` | `TestPreferFree::test_free_failure_never_escalates_into_a_paid_call` | set mismatch: `'free-a'` vs `'paid-pro'` |
| `test_routing.py` | `TestPreferFree::test_free_model_serves_the_request` | set mismatch: `'free-a'` vs `'paid-pro'` |
| `test_routing.py` | `TestPreferFree::test_paid_model_never_selected_while_free_exists` | set mismatch: `'free-a'` vs `'paid-pro'` |

**`tests/test_phase9_trace.py` does not appear in the failure list.** The
regression fix holds under full-suite execution, not only in isolation.

### 4.2 Collection errors — all 32, single root cause

All 32 errors come from **one** file, `tests/test_phase4_email.py`, with one
identical cause:

```
AttributeError: <module 'jarvis.tools.email_tool' from
  '.../jarvis/tools/email_tool.py'> has no attribute 'speak'
```

Running that file alone: `1 passed, 32 errors in 0.21s`, exit 1.

The errors span `TestConfirmationGate` (23, mostly parametrized),
`TestLoggingPrivacy` (3), `TestNoDuplicateSend` (1), `TestAllPathsProtected`
(2), `TestUnconfirmed*` and others — all fixture-time failures, so no test in
the file executes its body.

### 4.3 Environment / dependency problems

None observed. All four required dependencies import cleanly under
`.venv/bin/python`; `kokoro`, `soundfile` and the project's voice modules load
without error. The suite completes in ~43 s with no hangs, no timeouts, and no
segfaults. The failures are code/test contract mismatches, not environment
faults.

---

## 5. Comparison with previous results

Valid, because both runs used the same interpreter on the same machine.

| Stage | Passed | Failed | Errors | Exit |
|---|---|---|---|---|
| Before the `first_audio` fix | 1117 | 12 | 32 | 1 |
| **This revalidation** | **1118** | **11** | **32** | **1** |

Delta: **+1 passed, −1 failed, errors unchanged.** The single removed failure is
`test_phase9_trace.py::TestTTSReadiness::test_first_audio_is_marked_when_a_reply_starts_playing`
— the regression this task's predecessor fixed. **No new failure appeared.**

Earlier reference points, for context only:
- `test_phase4_voice.py`: 178 passed (unchanged, verified in the prior task)
- `test_phase5_tone.py`: 119 passed (unchanged, verified in the prior task)
- Detached-HEAD baseline (`ddbff2e`, excluding two files that fail collection at
  HEAD): 945 passed / 72 failed / 32 errors

The current tree remains **substantially better than HEAD** and the 11 remaining
failures are all carried forward, not newly introduced.

---

## 6. Repository preservation

| Check | Result |
|---|---|
| `git diff --check` | **exit 0**, no output |
| `git status --short` final | **byte-identical to initial** — 30 entries |
| `git diff --cached --stat` | **empty — nothing staged** |
| `HEAD` | unchanged, `ddbff2e` |
| Branch | unchanged, `voice-repair` |
| Files modified by this task | **none** |
| Tests modified | **none** |
| Production code modified | **none** |
| Destructive git commands | **none** |

All 24 modified files, both deletions, and all four untracked files are intact
and unchanged. `docs/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md` is still present
at 13,595 bytes.

**Ignored artifacts:** pytest regenerated `__pycache__/` directories and
`.pytest_cache` during the runs. These are gitignored build output
(`git ls-files | grep -c __pycache__` → `0`), are not listed by
`git status --short`, and were **not** cleaned.

**Single new file:** `docs/PHASE_E_FULL_SUITE_REVALIDATION.md`, created by this
task as instructed.

---

## 7. AC-6 assessment

> **AC-6:** *The test suite passes, and removed tests are removed for stated
> reasons.* — evidence: pytest output and Phase D table.

| Half of the criterion | Status | Basis |
|---|---|---|
| Removed tests removed for stated reasons | **Met** | `tests/test_tts_say.py` and `tests/test_wake_listener_lifecycle.py` are deleted, with documented reasons (retired `say` engine and removed listener); both fail collection at HEAD, so deletion repaired rather than suppressed |
| **The test suite passes** | **NOT met** | `pytest tests/ -q` exits **1**: 11 failed, 32 errors |

# AC-6: NOT SATISFIED

The criterion is conjunctive. The full suite does not pass, therefore AC-6
fails regardless of the state of the second half. Marked **unmet** rather than
partial, because its primary condition is plainly false.

---

## 8. Attribution of the remaining failures

These are **carried forward, pre-existing** failures. This task did not
investigate their root causes beyond capturing their reported messages, and did
not attempt any fix — both are outside its scope.

| Group | Nature | Introduced by this work? |
|---|---|---|
| `test_phase4_email.py` (32 errors) | Test monkeypatches `email_tool.speak`, which does not exist on the module | **No** — present at HEAD |
| `test_routing.py` (4) | Free/paid model preference ordering | **No** — present at HEAD |
| `test_phase8_wake.py` (4) | Wake-score list mismatch and shared-pipeline lifecycle assertions | **No** — present at HEAD |
| `test_phase2_intents.py` (3) | Weather tool contract: no `get_temperature`, city not echoed | **No** — present at HEAD |

None of these four groups relates to TTS, the microphone, or the voice repair
work. They concern email tooling, model routing, wake scoring, and weather —
all outside Phases A–E of the authoritative specification.

---

## 9. Remaining Phase E blockers

This revalidation changed no acceptance status except to confirm the Phase 9
regression is closed. Outstanding:

1. **AC-6 unmet** — full suite exits 1 (11 failures, 32 errors), all pre-existing
   and outside this specification's scope.
2. **E2 hardware validation incomplete** — TTS audible quality and pause
   correctness, live wake-word interaction, follow-up without repeating the wake
   word, and interruption / playback stability remain unverified. (Microphone
   speech capture passed on user-supplied RMS evidence: ~0.05 rising to
   0.46–0.74.)
3. **E4 end-of-speech-to-first-audio unmeasured** — still **PENDING**. The
   repaired test confirms the `first_audio` mark is emitted and correctly
   attributed to its turn; it produces no latency figure, and none was invented.
4. **Audio crackling** — reported and explicitly deferred by the user. Not
   investigated, not counted as passing.
5. **E1 literal zero-output conflict** — production and configuration paths
   (`jarvis/`, `scripts/`, `config.yaml`, `requirements.txt`) contain zero
   Chatterbox/PyAudio references, and the wake-word grep is clean; residual
   matches are deliberate absence-assertions in tests. Requires an explicit
   standing decision.
6. **AC-8** (text mode works; clean Ctrl+C exit) — no evidence gathered.

---

## 10. Conclusion

The `first_audio` regression is fixed and verified both in isolation and inside
a full-suite run. The verification itself changed nothing in the repository.

**Phase E is not complete.** AC-6 is unmet, hardware validation is partial, and
one required latency measurement remains unmeasured.
