# Phase E — `first_audio` Regression Report

**Report generated:** 2026-10-11
**Branch:** `voice-repair`
**HEAD:** `ddbff2e fix: load wake word model before microphone startup`
**Interpreter:** `.venv/bin/python` (Python 3.12.15)
**Scope of this task:** one reported test regression only.

> This report covers a single failing test. It does **not** mark Phase E
> complete. Phase E acceptance gaps are listed in §9.

---

## 1. Summary

A test introduced during Phase E,
`tests/test_phase9_trace.py::TestTTSReadiness::test_first_audio_is_marked_when_a_reply_starts_playing`,
failed because it called `ConversationMachine.status()`, a method that does not
exist. The failure was caused by the test, not by production code. It is now
fixed, and the fix is confined to the test file.

**No production file was modified by this task.**

---

## 2. Verified root cause

### 2.1 The failure

```
AttributeError: 'ConversationMachine' object has no attribute 'status'.
Did you mean: 'state'?
```

### 2.2 Why the method did not exist — verified, not assumed

`jarvis/conversation.py` contains two separate state machines:

| Class | Line | Has `status()`? | Has `snapshot()`? |
|---|---|---|---|
| `ConversationMachine` | 133 | **No** | **Yes** (line 333) |
| `AssistantStateMachine` | 420 | — | — |

The earlier investigation had located a `status()`-shaped dict containing a
`"marks"` key at `jarvis/conversation.py:348`. Reading the surrounding class
boundaries shows that dict is built by **`AssistantStateMachine.snapshot()`**,
not by anything on `ConversationMachine`. `VoiceLoop.machine` is a
`ConversationMachine`, so the test was calling a method on the wrong class.

`ConversationMachine` exposes marks through **`snapshot()`**:

```python
# jarvis/conversation.py:333
def snapshot(self, *, include_history: bool = True) -> Dict[str, Any]:
    ...
    "turn": turn.to_dict() if turn else None,
    "marks": ({k: round(v - min(self._marks.values()), 3)
               for k, v in self._marks.items()} if self._marks else {}),
```

- `snapshot()["marks"]` — **machine-level** offsets, relative to the earliest mark.
- `snapshot()["turn"]["marks"]` — **per-turn** offsets, relative to `Turn.started_at`.

Both are public. No private attribute access was required.

### 2.3 How `first_audio` is actually recorded — full path verified

1. `VoiceLoop.__init__` sets `self.machine = machine or ConversationMachine(clock=clock)`
   (`jarvis/voice_loop.py:138`). The machine is injectable.
2. `VoiceLoop._speak(reply, turn_id)` (`jarvis/voice_loop.py:644`) synthesizes:
   ```python
   queued = self.player.synthesize_to_queue(reply, turn_id, is_stale)
   if is_stale(turn_id) or queued == 0:
       return                                    # nothing queued -> no mark

   self.machine.mark("first_audio", turn_id)     # line 665
   self.echo.begin_playback()
   self.player.start(turn_id, is_stale)          # playback begins here
   ...
   self.machine.mark("playback_end", turn_id)    # line 685
   ```
3. `ConversationMachine.mark()` (`jarvis/conversation.py:289`) writes the
   machine-level mark unconditionally, then writes the **per-turn** copy only
   when the live turn id matches:
   ```python
   self._marks.setdefault(stage, now)
   turn = self._turn
   if turn is None or (turn_id is not None and turn.id != turn_id):
       return
   turn.mark(stage, now)
   ```
4. `"first_audio"` is a member of `STAGES` (`jarvis/conversation.py:93`), so
   `Turn.mark()` accepts it.

**Lifecycle placement is correct:** the mark fires only after audio is actually
queued, immediately before `player.start()`. A stale or empty reply returns
early and is correctly not marked.

### 2.4 What the original test intended to prove

The replaced test, `test_warm_model_reports_ready_not_loading`, asserted
`tts_ready` / `tts_load` trace spans. Those belonged to a removed TTS engine;
`grep -rn "tts_ready\|tts_load" jarvis/ scripts/` returns **zero** matches, so
no production code can ever satisfy them. The underlying intent — *the pipeline
records when audio actually starts* — is still a live contract, and it is the
input `docs/VOICE_LATENCY.md` needs for time-to-first-audio. That intent was
preserved rather than deleted.

### 2.5 Assumption explicitly rejected

An intermediate version of this task asserted
`marks["first_audio"] < marks["playback_end"]`. It failed:

```
AssertionError: first_audio must precede playback_end,
got {'first_audio': 0.0, 'playback_end': 0.0}
```

This is **not** a production defect. `snapshot()` rounds offsets to three
decimals, and the injected no-op player (`play=lambda d, r: None`) completes in
under a millisecond, so both marks collapse to `0.0` and carry no ordering
information. The assertion was speculative and unsupportable under a stub
player; it was removed rather than weakened into something vacuous. Ordering is
observable in a real session from the per-turn HUD offsets.

---

## 3. The change

**File changed:** `tests/test_phase9_trace.py` — **only**.

### 3.1 Functional changes

```diff
-            loop._speak("It is twelve.", loop.machine.begin_turn().id)
-            marks = loop.machine.status()["marks"]
+            turn = loop.machine.begin_turn()
+            loop._speak("It is twelve.", turn.id)
+            snapshot = loop.machine.snapshot()
         finally:
             loop.stop()

-        assert "first_audio" in marks, (...)
+        marks = snapshot["marks"]
+        assert "first_audio" in marks, (...)
+
+        turn_view = snapshot["turn"]
+        assert turn_view is not None and turn_view["id"] == turn.id, ...
+        assert "first_audio" in turn_view["marks"], ...
```

### 3.2 Import cleanup (dead imports orphaned by the same edit)

```diff
-from unittest.mock import MagicMock
...
-    span_of,
```

Both were used only by the code this task replaced. Verified by grep: each
appeared on exactly one line (the import) after the rewrite.

### 3.3 Scope statement

| Question | Answer |
|---|---|
| Production code changed? | **No** |
| Test weakened or deleted? | **No** — strengthened (added turn-attribution checks) |
| Other files touched? | **No** |
| Mutation testing performed? | **No** — excluded by this task's instructions |

---

## 4. Test commands and results

Every command was run independently. Exit codes are from a separate
re-invocation of the identical command, not inferred from output text.

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `.venv/bin/python -m pytest tests/test_phase9_trace.py::TestTTSReadiness::test_first_audio_is_marked_when_a_reply_starts_playing -q` | **1 passed** | **0** |
| 2 | `.venv/bin/python -m pytest tests/test_phase9_trace.py -q` | **15 passed** | **0** |
| 3 | `.venv/bin/python -m pytest tests/test_phase4_voice.py -q` | **178 passed, 2 warnings** | **0** |
| 4 | `.venv/bin/python -m pytest tests/test_phase5_tone.py -q` | **119 passed** | **0** |

No skips, no errors, no failures in any of the four commands.

Commands 1–4 were run once before the import cleanup and once after; both
sequences produced identical results. The table reports the post-cleanup run.

---

## 5. Is the regression fixed?

**Yes — verified.**

Evidence:

1. Command 1 returns `1 passed` with exit code **0**.
2. Command 2 returns `15 passed` with exit code **0**; the file previously
   reported `1 failed, 14 passed`.
3. The test exercises the real code path: it constructs a `VoiceLoop`, opens a
   loop with an injected silent microphone, runs `_speak`, and reads the marks
   the production code actually wrote. It does not stub `mark()` or `snapshot()`.
4. No production behavior was altered to accommodate it — `git diff` shows no
   change to any file under `jarvis/`.

**Not claimed:** that this test proves end-to-speech-to-first-audio latency.
It proves the mark is emitted and correctly attributed. The latency figure
itself is still unmeasured (§9).

---

## 6. Repository integrity

### 6.1 `git diff --check`

```
EXIT=0   (no output)
```

No whitespace errors introduced.

### 6.2 Initial status (before this task) vs final status

Both listings are identical — 24 modified, 2 deleted, 3 untracked:

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
 M jarvis/voice_loop.py                     M tests/test_phase9_trace.py   <-- this task
 M jarvis/wake_word.py                      M tests/test_startup.py
 M scripts/phase4_live_check.py
 M scripts/tts_benchmark.py
 D tests/test_tts_say.py
 D tests/test_wake_listener_lifecycle.py
?? docs/VOICE_LATENCY.md
?? jarvis/tools/news.py
?? tests/test_tts_engine.py
```

`tests/test_phase9_trace.py` was already listed as modified before this task
began — the earlier, failing version of the test was itself an uncommitted
Phase E change. This task amended that uncommitted work rather than adding a
newly-tracked change.

### 6.3 Attribution limitation (disclosed)

The working tree contains substantial uncommitted work spanning several phases.
`tests/test_phase9_trace.py` carried **both** pre-Phase-E edits and the failing
Phase E edit, and Git cannot separate them. The `git diff` in §3 therefore shows
the cumulative change against HEAD, not only this task's delta.

What *is* isolated confidently: the only file this task opened for writing was
`tests/test_phase9_trace.py`, and no file changed state between the initial and
final `git status --short` listings.

### 6.4 Preservation confirmations

| Check | Result |
|---|---|
| Unrelated modifications lost | **None** |
| Untracked files removed | **None** (all three still present) |
| Deletions altered | **No** — both deletions still staged as unstaged deletions |
| Files staged | **None** — `git diff --cached --stat` empty |
| Files committed | **None** — HEAD still `ddbff2e` |
| Destructive git commands used | **None** |
| Production files modified | **None** |

---

## 7. Remaining failures

This task's four commands are all green. Failures elsewhere in the suite are
**pre-existing and outside this task's scope**; they were last observed as:

| File | Failures | Nature |
|---|---|---|
| `tests/test_phase2_intents.py` | 3 | Weather tool interface mismatch |
| `tests/test_phase8_wake.py` | 4 | Wake score-list mismatch |
| `tests/test_routing.py` | 4 | Free/paid model selection ordering |
| `tests/test_phase4_email.py` | 32 errors | `email_tool` has no `speak` |

These were **not** re-measured by this task, which ran only the four mandated
commands. Treat the table as the last known state, not a fresh measurement.

---

## 8. Verified facts vs assumptions

**Verified by direct inspection or execution:**

- Branch `voice-repair`, HEAD `ddbff2e`.
- `ConversationMachine` has no `status()`; it has `snapshot()`.
- The `"marks"` dict at `conversation.py:348` belongs to a different class.
- `VoiceLoop.machine` is a `ConversationMachine` (default-constructed at
  `voice_loop.py:138`).
- `first_audio` is marked at `voice_loop.py:665`, after `queued != 0` and before
  `player.start()`.
- `tts_ready` / `tts_load` have zero matches in production.
- `MagicMock` and `span_of` were orphaned imports.
- All four test commands and their exit codes.
- `git diff --check` exit 0; nothing staged; HEAD unchanged.

**Assumptions / not established:**

- That mark *ordering* is correct in a real session. Not verifiable with a
  no-op player; see §2.5.
- The full-suite counts in §7 were not re-measured here.
- Nothing about the pre-existing failures' fixability was investigated.

---

## 9. Phase E status — unchanged by this task

This regression is fixed. **Phase E is not complete.** Outstanding items,
carried forward unchanged:

1. **E3 full suite red** — the full suite has not been re-run since this fix,
   and previously failed with 12 failures and 32 collection errors, all
   pre-existing.
2. **E2 hardware validation incomplete** — TTS audible quality and pauses,
   live wake-word interaction, follow-up without the wake word, and
   interruption/playback stability remain unverified.
3. **E4 end-of-speech-to-first-audio unmeasured** — still PENDING. This test
   verifies the mark exists; it does not produce a latency figure, and no
   figure was invented.
4. **Audio crackling** — reported and explicitly deferred by the user. Not
   investigated here and not counted as passing.
5. **E1 literal zero-output conflict** — production and configuration paths are
   clean; residual matches are deliberate absence-assertions in tests. Still
   requires an explicit standing decision.
6. **AC-8** (text mode, clean Ctrl+C exit) — no evidence gathered in this task.

---

## 10. Conclusion

The single Phase E regression is **fixed** and verified by four independently
run commands, all exiting 0. The change is confined to
`tests/test_phase9_trace.py`, touches no production code, and deletes or
weakens nothing. Repository state is otherwise byte-identical to the state at
the start of this task.

Fixing this regression is **not** equivalent to completing Phase E.
