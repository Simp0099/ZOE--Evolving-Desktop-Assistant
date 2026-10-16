# Testing

## The offline suite

```bash
python -m pytest -q
```

That is the whole gate. It is offline, deterministic, and needs no microphone,
speaker, camera, network or credentials: provider calls are served by in-repo
fakes (`tests/mock_providers.py`) and by tests that construct their own doubles.

`pytest.ini` sets `testpaths = tests`, so bare `pytest` is the same thing. Do not
run `pytest` from inside `tests/` — most files insert the project root on
`sys.path` by relative parent traversal and will fail to import.

Run one area:

```bash
python -m pytest tests/test_phase4_voice.py -q
python -m pytest -k barge_in -q
```

Test counts are not written down anywhere in the docs. They drift every time a
test is added, and five different numbers had accumulated across the old
documentation. Run the suite for a current figure; the dated snapshot is
[`history/baseline_mac.txt`](history/baseline_mac.txt).

### Known xfails

`tests/test_echo_reverb.py` carries a `strict=True` xfail pair documenting a
real, measured gap: `EchoCanceller` is a single tap, so in a 200 ms RT60 room it
removes 0% of the echo energy and the assistant's own voice inflates the VAD
noise floor past the barge-in bar. Fixing it needs a partitioned or sub-band
canceller, which is outside this architecture. The xfail stays as written — if
one of those tests ever passes, it will fail the suite and tell us the gap
closed.

One test skips when no provider credential is available
(`tests/test_providers.py`, live round-trip). That is honest, not hidden.

## The in-repo check

```bash
python -m zoe.main --test
```

Eight pipeline checks against an in-process probe provider, so they verify this
project's routing and tool-loop logic rather than a model's mood: verbatim input
forwarding, tool-result feedback and loop termination, fallback when a model
fails, capability filtering, an honest error when every candidate fails,
empty-input rejection, model-layer load — and finally one **live** model call,
reported separately because provider availability is not something this
repository controls. Exits non-zero if any check fails.

Related: `python -m zoe --test` runs `tests/test_integration.py` through its
own standalone runner instead. Useful for reading the integration classes
without pytest.

## The `requires_kokoro` marker

Tests that exercise the real Kokoro engine are marked
`@pytest.mark.requires_kokoro`. The hook checks for the package with
`importlib.util.find_spec("kokoro")` — microseconds, and it never imports torch
— and skips with:

```
kokoro not installed; see docs/SETUP.md
```

The hook lives in the **repository-root `conftest.py`**. A root conftest is
loaded for every test under `tests/` regardless of how the suite is invoked.
(`tests/conftest.py` carries an identical copy of the same hook — redundant
rather than harmful.)

So on a machine without Kokoro the suite reports **skips, not failures**. Run
just those tests with:

```bash
python -m pytest -m requires_kokoro -q
```

## Live scripts

Live checks are deliberately **not** in the test suite. A test that needs a human
to speak into a microphone cannot be automated honestly, and one that pretends
otherwise proves nothing. Each script states what it can and cannot run, and
reports the rest as SKIPPED rather than PASS.

| Script | Needs | What it actually does |
| --- | --- | --- |
| `scripts/audio_devices.py` | PortAudio | Lists devices and the configured/default input and output |
| `scripts/list_apps.py` | **macOS** | Prints the installed `.app` bundles as a pasteable `aliases:` block. Print only — never writes `config.yaml`, never launches anything |
| `scripts/mic_check.py` | **microphone** | Records 4 s and reports the level per half second |
| `scripts/wake_eval.py` | Kokoro only with `--with-tts` | Scores the real wake engine on synthetic negatives, and on TTS-spoken positives if asked |
| `scripts/tts_check.py` | Kokoro + **speaker** | Renders a phrase, reports pauses, writes a WAV and plays it |
| `scripts/tts_latency.py` | Kokoro | Latency over four phrases, 5 warm trials each |
| `scripts/tts_benchmark.py` | Kokoro | Cold/warm latency, audio duration, real-time factor |
| `scripts/first_audio_check.py` | **nothing** | End-of-speech → first-audio, driven through the real pipeline with a synthetic mic |
| `scripts/analyse_barge_in.py` | a `data/zoe.log` | Reads a real barge-in cut out of the log |
| `scripts/phase3_live_check.py` | provider key; camera optional | Synthetic camera frames through the real perception loop and the real model |
| `scripts/phase4_live_check.py` | **mic + speaker** | Echo and shutdown fully; anything needing a human voice is SKIPPED |
| `scripts/phase5_live_check.py` | provider key | Scenarios against the real Brain; asserts state and guidance, never model wording |
| `scripts/phase6_live_check.py` | none for the logic path | Proactive gating against scripted readings; one real generation only with a provider |

One script is a doc generator rather than a check:

| Script | What it does |
| --- | --- |
| `scripts/gen_config_reference.py` | Regenerates `docs/CONFIGURATION.md` from `config.yaml`, `zoe/config.py` and a grep of the tree. `--check` exits 1 if the committed doc is stale, which is what stops the reference drifting from the code |

Run them with the project interpreter — Kokoro lives in `.venv`, not in system
Python:

```bash
.venv/bin/python scripts/tts_check.py
```

## Which checks need hardware

| Check | Hardware | Can anything else substitute? |
| --- | --- | --- |
| Audio level reaches the model | microphone | No. RMS rising while speaking is the only evidence |
| Wake word fires on "hey jarvis" | microphone | No. `wake_eval.py` proves the *detector chain*, not a human saying the phrase |
| TTS is audible and intelligible | speaker + ears | No. Synthesis is measurable; audibility is not |
| No self-interruption through speakers | mic + speaker | Partly. `analyse_barge_in.py` and the echo suite characterise it; the acoustic loop does not close without the room |
| Webcam perception | camera | Partly. Synthetic frames prove the pipeline and the model call, not the capture |
| Email delivery | real SMTP | No. The confirmation gate is fully covered offline; delivery is not |
| Provider quota / live routing | network + credentials | No |

The manual procedure for all of these is
[archive/PHASE_E_MANUAL_CHECKLIST.md](archive/PHASE_E_MANUAL_CHECKLIST.md).
**Nothing in that
file may be marked PASS from the file itself** — it has to be run at the
machine.

## What the suite deliberately does not cover

- Anything requiring a human to speak or listen. There is no proxy and none is
  substituted.
- End-of-speech-to-first-audio **as a latency assertion**. The marks are
  produced and tested; the figure is measured by `first_audio_check.py` and
  recorded in [VOICE_LATENCY.md](VOICE_LATENCY.md), and it excludes the model
  provider round trip, so no 2.0 s target is claimed.
- Reverberant echo cancellation. See the known xfails above.

## Writing tests here

- Provider-dependent tests use `tests/mock_providers.py` (`build_layer`,
  `reply`, `error`). `zoe/main.py::run_verification_test` uses it too, so it
  is production-adjacent — do not break its interface casually.
- Tests must be runnable with no network. If something genuinely cannot be,
  skip it with the real reason rather than faking a pass.
- A test that pins exact model wording is a test that lies: wording varies by
  provider and run. Assert state, structure and guidance instead. This is the
  house rule in `scripts/phase5_live_check.py` too.
- Several tests assert on source text (`inspect.getsource`, or reading a file by
  path relative to the project root). Moving a module or renaming a file breaks
  them **silently** until you run the suite — grep before you move anything.
---

## Frontend suite

The Electron UI has its own suite, independent of pytest. It uses the Node
built-in test runner — there is no test framework dependency.

```bash
cd frontend/zoe
npm test              # node --test src/
npm run lint          # oxlint
npm run build         # build validation; writes dist/
```

`node --test src/` only discovers tests **inside `src/`**. Two files, 20 tests
as of 2026-10-15, all passing:

| File | Covers |
| --- | --- |
| `src/voiceState.test.js` | 12 tests: every legal transition, every state reachable from `error`, illegal transitions refused rather than corrupting, labels present for every state, and what the mic button does in each state |
| `src/api.test.js` | 8 tests with a stubbed `fetch`: the real conversation id is sent, the assistant's actual reply is returned, a provider failure surfaces the backend's own message, an unreachable backend is reported as offline rather than as a reply, a cancelled request is distinguished from a failure, a timeout aborts instead of hanging, transcription uploads multipart rather than JSON, and reset posts the id being cleared |

Both are offline and need neither a backend nor a microphone.

**What the frontend suite does not cover:** React components, `App.jsx` itself,
`Globe.jsx`, the Electron main process, the preload bridge, and anything
requiring a live backend. `npm run build` passing is a compile check, not an
end-to-end result.

Lint currently reports warnings only (mostly `react-hooks/exhaustive-deps` in
`App.jsx`); no errors.

## Backend suite, current figure

Deliberately not written down in the body of this document, because it drifts.
For the record as of **2026-10-15**:

```
6 failed, 1542 passed, 1 skipped, 11 xfailed in ~116s
```

All 6 failures are in `tests/test_echo_reverb.py`, for the documented
single-tap-canceller reason. If your change makes it worse than that, it is a
regression.

## Test integrity

Several tests assert on source text or read files by path relative to the
project root. That makes a rename dangerous in a quiet way: `tests/test_integration.py`
contains the credential-literal scan, and it once pointed at the `jarvis/`
directory, which the package rename had emptied. It passed, having checked one
file.

**Before you move or rename anything under `zoe/`, grep the tests for the path.**
A test that silently checks nothing is worse than no test.
