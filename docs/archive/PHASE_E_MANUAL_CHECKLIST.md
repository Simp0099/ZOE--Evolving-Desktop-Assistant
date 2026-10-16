# Phase E — Manual Hardware Checklist

Everything here needs a microphone, speakers, or a human ear. None of it was
performed by the coding agent, and **none of it may be marked PASS from this
file**. Run these in order at the machine, then record the outcome.

All commands are run from the project root with the project interpreter:

```
cd "<PROJECT_ROOT>"
```

Use `.venv/bin/python`. Do not use system Python — it lacks `kokoro`.

**Wear headphones for checks 3–5.** The specification notes the assistant can
hear itself through the speakers; without headphones, barge-in testing measures
echo rather than interruption.

---

## 1. Microphone speech capture — AC-1

```
.venv/bin/python scripts/mic_check.py
```

Speak during the 4-second window.

- **Expected:** `Input device: ...`, then RMS that is clearly higher while you
  speak than during silence, ending `OK. Audio is arriving.`
- **Record:** the device name, the quiet RMS range and the speaking RMS range.
- Previously reported: quiet ~0.05, speaking 0.46–0.74. Substantially different
  numbers are fine; what matters is that speaking is clearly above silence.

## 2. TTS output and punctuation pauses — AC-4

```
.venv/bin/python scripts/tts_check.py
```

Then **listen to `data/kokoro_check.wav`**.

- **Expected:** RTF below 1.0, and a pause list printed for the phrase
  `Hello there... how are you doing today? Good. Let us get started.`
- **Record:** the RTF, the pause list, and whether you heard four distinct
  pauses — after `...`, after `?`, and after each `.` — and whether the speech
  was intelligible.
- The automated pause list proves pauses are *rendered into the waveform*. Only
  listening proves they are *audible and correctly placed*. Do not conflate.

## 3. Live wake word — AC-3

```
.venv/bin/python start.py
```

Say `hey jarvis`, then ask something short, e.g. "what time is it".

- **Expected:** the assistant acknowledges the wake word and speaks a reply.
- **Record:** whether the wake word was detected on the first attempt, and any
  errors from the log.

## 4. Follow-up without the wake word — AC-3

In the same session, ask a second question **without** saying `hey jarvis`.

- **Expected:** the assistant answers it.
- **Record:** whether it answered, or whether the wake word was required again.

## 5. Interruption and playback stability — AC-3, AC-4

Still in the same session, speak over the assistant while it is replying.

- **Expected:** playback stops promptly and the new utterance is taken.
- **Record:** whether interruption worked, whether audio stopped cleanly, and
  whether you heard **clicks, repeated playback, or a hang**.

### Crackling — DEFERRED, NOT FIXED

Crackling in the output has been reported and was **explicitly deferred**. It is
**not** resolved, and no test in this repository demonstrates otherwise. If it is
still audible during check 2 or 5, record it as an open defect. Do not treat a
clean turn as evidence that it is fixed.

## 6. Text mode and clean shutdown — AC-8

```
.venv/bin/python start.py --text
```

Type a short message, then press `Ctrl+C`.

- **Expected:** a typed reply, and a clean exit back to the shell prompt with no
  traceback and no hang.
- **Record:** whether both happened.

---

## 7. Provider-inclusive first-audio latency — AC-5 (optional but useful)

The recorded figure (1.611 s median warm) **excludes the model provider round
trip**. To measure the real total, use the live session from checks 3–5:

```
grep -E "\[YOU\]|chunks queued|playback turn=" data/zoe.log | tail -20
```

- **Record:** the gap from the `[YOU]:` line to the `[TTS] chunks queued` line
  for one turn. That is transcript → first audio including the provider, and it
  is the number to compare against the 2.0 s goal.
- `scripts/phase4_live_check.py` prints this directly if you prefer a scripted
  run.

---

## Recording your results

Report, per check: PASS or FAIL, plus the observed numbers or a one-line
description. That is what is needed to move AC-1, AC-3, AC-4, AC-5 and AC-8
out of NOT VERIFIED. Checks that are not run stay NOT VERIFIED — they cannot be
inferred from the passing automated suite.
