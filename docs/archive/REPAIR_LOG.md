# Repair log

## Structural
- Restored the `jarvis/` package layout. The uploaded archive was flat, so no
  `from jarvis.x import` import could resolve.
- Reconstructed the missing `jarvis/providers/__init__.py` (build_provider) and
  `jarvis/intents/__init__.py` (handle, match_only) against the contracts the
  tests pin. Originals were not in the archive; replace with yours if you have them.
- Removed .pyc files, screenshots, WAV recordings, rotating logs and the personal
  memory database from the shipped tree.

## Voice
- Stale-turn guard after transcription: a turn superseded by a barge-in is discarded
  before it touches state. Test: tests/test_race_regressions.py.
- A live turn whose state was moved by something other than a barge-in now continues
  instead of failing with "cannot go listening -> thinking" (151 occurrences in logs).
- Optional barge-in grace window (vad.barge_in_grace_ms), default 0 (off). Enabling it
  delays genuine early interruptions; the echo canceller remains the real fix.

## Behaviour
- Default weather city is Faridabad (was Delhi).

## Plugins
- New plugin loader (jarvis/plugins.py), template and example plugin, validation,
  collision checks, crash isolation and timeouts. Tests: tests/test_plugins.py.

## Tests
- Test suite runs offline with dummy keys (tests/conftest.py).
- Fake audio stream paced in real time (test_speech_self_interrupt).
- Stale expectations updated (default city; .env check now skips visibly).

### macOS baseline — docs/baseline_mac.txt
- Command: `python -m pytest -q --tb=short | tee docs/baseline_mac.txt`
- Host: macOS 27 (arm64), CPython 3.12.15 (Kokoro supports 3.10-3.12; the system
  `python3` is 3.14, so the venv is pinned to 3.12).
- Result: **1224 passed, 1 skipped, 0 failed** across 30 test files. (After the
  echo-guard tests below were added the suite is 1229 passed, 1 skipped.)
- The single skip is the live-provider probe `tests/test_providers.py:514`, reason
  `provider unavailable: network` — the test's own honest skip, not a count fix.
- The previously-listed platform failures do not reproduce once the optional
  speech packages are installed:
  - "macOS app launch (16)" — tests/test_phase3_applaunch.py (91 tests) passes on
    macOS; it mocks `subprocess`, so no real application is launched.
  - "Kokoro TTS not installed (~20)" — environment issue. Install step:
        pip install kokoro soundfile
    Present for this run (kokoro 0.9.4, soundfile 0.14.0).
  - "openwakeword model/API (2)" — install step: `pip install "openwakeword>=0.6.0"`
    (already in requirements.txt). Present for this run (0.6.0).
  - test_phase4_voice last_error ordering — all 178 tests in that file pass.
- Environment repair: the project directory was renamed from `Ai Partner ( Jarvis
  2.0)` to `Ai Partner`, leaving every `.venv/bin/*` console script (pip, pytest)
  with a stale shebang. Rewrote the stale path in place (74 scripts + pyvenv.cfg);
  `pip` and `pytest` run again. A fresh venv would also fix this, but would force a
  full torch/kokoro reinstall for no change in the result.
- No real defects open from this run; every nonzero result is accounted for above.

## Echo guard: barge-in answers "is this sound ours?" (uncommitted)

### Problem
Threshold-only barge-in confuses the assistant's own reply with the user. A
reply through the speakers is exactly as loud as the person and sits in the same
spectral bands, so *any* loudness bar that lets a person interrupt also lets the
assistant interrupt itself. The existing `EchoCanceller` removes only the direct
speaker-to-mic path (single-tap) and cannot remove reverberation, so the residual
still trips the VAD.

### Design (implemented in `jarvis/audio.py::EchoGuard`)
Subtract a band-energy estimate of the playback from the mic, with a learned
per-band gain, and gate barge-in on the residual:

```
residual_band = max(0, mic_band - gain_band * playback_band)
```

- Band energies: 8 equal-width bands (log-spaced edges let one wide top band
  dominate, which made unrelated sound look like our own), FFT on 20 ms frames,
  normalised so `sqrt(sum(bands))` is an RMS comparable with the VAD thresholds.
- Per-band echo gain learned during playback with no user speech, bounded to
  `[0, echo_guard_gain_max]` (default 1.5), refined only while the frame already
  looks like echo (double-talk detector) so it never learns to subtract the user.
- A frame is "ours" when its **spectral shape** (mean-centred log bands) matches
  the playback with correlation >= 0.9 for two consecutive frames. Shape is the
  discriminator, not energy: raw all-positive band vectors correlate near 1 for
  any two broadband sounds.
- Barge-in is allowed only when the residual clears the threshold for
  `echo_guard_min_consecutive_frames` (default 3) consecutive frames.
- The loop feeds the guard the canceller's already-aligned reference
  (`EchoCanceller.last_reference`), so it scores the same instant without a second
  alignment. The time-based `EchoGuard` API and `barge_in_grace_ms` are unchanged.

### Config (`config.yaml` under `vad:`)
- `echo_guard_enabled: true` — off makes the guard a passthrough (old behaviour).
- `echo_guard_min_consecutive_frames: 3`
- `echo_guard_gain_max: 1.5`

### Logging
Each cut now logs `residual_energy` and `playback_energy` alongside
`chunks_played`, so `scripts/analyse_barge_in.py` can be re-run once the live
logs carry the new fields.

### Tests (tests/test_speech_self_interrupt.py::TestEchoGuardBandResidual)
- `test_echo_only_never_fires_barge_in` — mic is a scaled, delayed copy of the
  playback; the guard must never call it the user.
- `test_burst_fires_barge_in_within_three_frames` — same echo plus a speech burst
  300 ms in; barge-in fires within 3 frames of the burst.
- `test_sustained_double_talk_still_interrupts`, and passthrough when nothing is
  playing / the guard is disabled.
- Verified robust across 50 noise seeds and output lags 1-9 frames for speech-like
  playback (0 false barge-ins, burst always caught within 3 frames).

### Acceptance note
The live re-run ("zero cuts with zero audio played") needs real audio hardware
and is not reproducible in this environment. The mechanism is proven by the unit
tests above; the numbers from the last hardware run remain in the earlier sections.

## Thinking acknowledgment (uncommitted)

### Problem
Median model thinking is ~4.5s and p90 ~6.6s. With no filler, a slow reply reads
as a hang — the user gets silence for seconds and cannot tell whether the assistant
heard them.

### Design (jarvis/voice_loop.py)
On entering THINKING, arm a timer for `ack_delay_ms` (default 800ms). If the main
model has not produced its first token by then, speak a short acknowledgment. The
timer is cancelled the moment the reply returns, so a fast model never gets one.

- **At most one ack per turn** (`_ack_played`, reset each turn).
- **Never if the reply starts first** (`_ack_ready`, set the instant `_respond`
  returns).
- **Goes through the same playback queue** (`SpeechPlayer.play_pcm`), so barge-in
  and cooperative cancellation apply to the ack exactly as to a reply. The ack
  waits for its own playback before returning, so it and the reply never overlap.
- **Source** (`voice.ack_mode`): `"cached"` (default) picks a fixed phrase and
  renders it at most once — no model call; `"model"` asks the configured model
  for a one-line acknowledgment. `ack_pcm` is injectable for tests.
- **Trace**: one `[VOICE] ack turn=<id> ack_played=<true|false>` line per turn.

### Config (`config.yaml` under `voice:`)
- `ack_enabled: true`
- `ack_delay_ms: 800`
- `ack_mode: "cached"`

### Tests (tests/test_phase4_voice.py::TestThinkingAck)
- slow model (300ms) → exactly one ack, then the reply after it;
- fast model (10ms) → no ack even well past the delay;
- barge-in during the ack → playback stops, turn returns to listening.

## Spoken replies: short and clean (uncommitted)

### Problem
Spoken replies had no length or format contract. A model could answer with a
wall of markdown — headings, bullet lists, URLs, bold — that the TTS then read
out character by character, and a long answer was a wall of sound the user could
not follow.

### Design
- **System prompt** (`jarvis/brain.py`): a `spoken` flag on `ask()` appends a
  short-form, no-markdown directive — but only for a turn whose reply will be
  heard. The voice loop passes `spoken=True`; text turns leave it off.
- **Hard cap** (`jarvis/voice_loop.py`): `_spoken_reply` strips markdown (bold,
  italics, bullets, headings, URLs, backticks) and enforces
  `voice.max_spoken_words` (default 60). A longer reply is truncated at the last
  sentence boundary under the cap and offered "Want more detail?", and the
  truncation is logged.

### Config (`config.yaml` under `voice:`)
- `max_spoken_words: 60`

### Tests
- `TestSpokenReply` (tests/test_phase4_voice.py): a 320-word reply is capped
  under 60 words with the truncation logged; markdown is stripped;
  a short reply is untouched.
- `TestBrainIntegration::test_spoken_block_added_only_for_spoken_turns`
  (tests/test_integration.py): the spoken directive is present for a spoken turn
  and absent for a text turn.

## file_reader plugin (uncommitted)

### Problem
The assistant could not read files the user pointed it at. A model tool that
reaches for an arbitrary path is also a path-traversal risk, so the plugin
enforces the boundary itself rather than trusting the model.

### Design (`plugins/file_reader.py`)
`read_file(path, max_chars=8000)` reads txt/md/csv/json (stdlib), pdf (pypdf),
docx (python-docx) and xlsx (openpyxl, read-only), returning at most `max_chars`
so a whole file never enters the model context. Path safety:
- `Path.resolve()` (which follows symlinks), then checked against
  `file_reader.roots` (default `~/Documents`, `~/Downloads`, `~/Desktop`).
- A symlink resolving outside the roots is rejected, as is a file over
  `file_reader.max_bytes` (default 20 MB).
- A missing optional dependency returns a plain install message, never a crash.

### Optional dependencies (`pyproject.toml`)
`pypdf`, `python-docx`, `openpyxl` are in the `reader` extra, not required:
`pip install -e ".[reader]"`.

### Config (`config.yaml` under `file_reader:`)
- `roots`, `max_bytes: 20971520`

### Tests (`TestFileReaderPlugin`, tests/test_plugins.py)
Each supported type against a small fixture in `tests/fixtures/`; `../../etc/passwd`
and a symlink to an outside file rejected; a 30 MB sparse file rejected with the
size message; missing `pypdf` returns the install message.

## Action safety: no confirmation flag from the model (uncommitted)

### Problem
An action tool (send email, launch an app, write a file) must run only because
the user confirmed on their own turn -- never because the model passed a flag.
A `confirmed`/`approve`/`force` parameter on a model-facing tool would let the
model bypass the core confirmation path.

### Audit
`grep -rn "confirm" jarvis/` reviewed. The only `confirmed` parameter anywhere is
on the internal `jarvis/tools/email_tool.py::send_email(..., confirmed=)`. The
model never sees it: the model-facing tool is `brain.send_email(to_address,
subject, message)`, which only stages a draft. The send runs solely from the
core confirmation path (`_resolve_pending_email` → `parse_confirmation` on the
user's turn → `email_tool.send_email(..., confirmed=True)`). No telegram tool
and no model-exposed system-command tool exist; `launch_app` is allowlisted.

### Test (tests/test_action_safety.py)
Enumerates every registered tool's JSON schema (built the way the model sees it,
via `openai_compat._function_to_schema`) and fails if any parameter name matches
`confirm|approved?|force|yes`. Verified non-vacuous: a tool with a `confirmed`
parameter is caught.

## Microphone auto-reconnection (uncommitted)

### Problem
Bluetooth/USB microphones can disconnect while the assistant is running. Previously
a read error killed the voice loop entirely, leaving the assistant deaf until
manually restarted. The log showed 242 "device disconnected" events across recent
sessions.

### Design
`_on_microphone_error` no longer kills the loop. Instead it spawns a background
thread that retries opening the microphone with exponential backoff
(`audio.input_reopen_backoff_ms`, default 500 ms, doubling to a 5 s cap) up to
`audio.input_reopen_attempts` (default 5) times. Each attempt logs
`mic_reopen attempt=N device=<name>`. The reconnection prefers the last device
that worked if it's still present. If all attempts fail, the assistant speaks
"I lost the microphone. Check the input device." once and transitions to IDLE.

### Config (`config.yaml` under `audio:`)
- `input_reopen_attempts: 5`
- `input_reopen_backoff_ms: 500`

### Tests
- `test_repeated_read_errors_terminate_the_capture_path` (tests/test_phase4_voice.py):
  verifies the old 20-failure threshold still marks the mic unavailable.
- `test_runtime_microphone_failure_starts_reconnection` (tests/test_phase8_wake.py):
  a microphone error now spawns a reconnection thread instead of immediately
  stopping the loop.
- The existing `test_repeated_read_errors_terminate_the_capture_path` remains
  as a safety net: if the device is truly dead for 20 consecutive frames, the
  capture path still terminates cleanly.

## App-launch tests run honestly on every OS (uncommitted)

### Problem
`launch_app` refuses on a non-macOS host *before* it builds `argv`, so on Linux
the tests that actually check what is handed to the OS would either fail or be
skipped — the security-relevant assertions silently going unrun.

### Design
- `tests/test_phase3_applaunch.py`: tests needing the real `open -a` path carry
  `MACOS_ONLY` (`skipif(sys.platform != "darwin")`), each with the reason
  "macOS app launch (uses `open -a`)".
- `tests/test_phase3_applaunch_linux.py`: pins only the *platform gate* to
  Darwin so the argv-construction path runs verbatim anywhere, with
  `subprocess.run` still mocked. Asserts the same properties as the macOS suite
  using the same allowlist: argv is a 3-element list, never a shell; only the
  resolved allowlist entry reaches the OS; normalization cannot smuggle
  arguments; aliases cannot authorize; allowlist is checked before any subprocess.
- Also asserts the opposite direction: a non-Darwin host refuses honestly and
  reaches no subprocess.

### Results
- macOS: 124 passed, 1 skipped (the non-Darwin refusal test, correctly skipped).
- Simulated Linux host: 90 passed, 35 skipped, **0 failed**, every skip with a reason.
- Full suite on macOS: 1308 passed, 2 skipped.

## Kokoro tests skip cleanly when the engine is absent (uncommitted)

### Problem
17 tests exercised the real Kokoro engine. Without the package they failed with
a bare `ImportError`, which reads like a broken repo rather than an uninstalled
optional dependency. Kokoro is deliberately not in `requirements.txt` (heaviest
dependency), so this was reachable by anyone who followed the documented install.

### Design
- Tests touching the real engine are marked `@pytest.mark.requires_kokoro` (17
  tests across test_phase6_tts_service.py, test_phase7_lifecycle.py,
  test_speech_self_interrupt.py, test_startup.py). The set was found empirically
  by blocking the import with a find_spec meta-path hook and reading the
  failures, not by grepping for "kokoro" -- several tests name it but never load
  it, and those were correctly left unmarked.
- The skip hook lives in a new repository-root `conftest.py`, not in
  `tests/conftest.py` as first planned. `.gitignore` line 22 excludes `/tests/`,
  so anything under it is untracked: the hook would not have shipped and a fresh
  clone would fail 17 tests instead of skipping them. Verified by `git ls-files`
  (tests/conftest.py is untracked) and by re-running the suite with that file
  removed -- the root conftest alone reproduces both results. The root file also
  carries the sys.path bootstrap and the dummy provider keys.
- The hook decides at collection time via `importlib.util.find_spec("kokoro")`.
  find_spec rather than import: it finds the package in microseconds and never
  pulls in torch.
- Marker registered in `pytest.ini` so the run emits no unknown-marker warning.
- `docs/SETUP.md` documents the install, the Python constraint and the marker.

### Results (verified by blocking the import with a find_spec meta-path hook)
- Without Kokoro: **1291 passed, 19 skipped, 0 failed**; all 17 skips read
  `kokoro not installed; see docs/SETUP.md`.
- With Kokoro (Phase 1 Mac: kokoro 0.9.4 / soundfile 0.14.0 / torch 2.6.0):
  **1308 passed, 2 skipped** -- all 17 run and pass.
- `pytest -m requires_kokoro -q` runs 17 with Kokoro, skips 17 without.

## openWakeWord fails fast when the installed version is too old

### Problem
`requirements.txt` pins `openwakeword>=0.6.0`, and this machine has 0.6.0 (not
0.4.0 as first reported), so the two wake-word tests were already passing. The
gap was diagnostic: with 0.4.x installed the engine loads and then dies on a
missing `openwakeword.MODELS` catalog attribute, reported as an AttributeError
from inside inference -- which reads like a broken repo, not a stale
dependency.

### Change
`jarvis/wake_word.py` now checks the installed version at import
(`MIN_OPENWAKEWORD_VERSION = (0, 6, 0)`, read from package metadata via
`importlib.metadata`, compared as a numeric tuple so 0.10.0 outranks 0.6.0). A
too-old install raises:

```
RuntimeError: openwakeword 0.4.0 is too old; 0.6.0 or newer is required.
Run: pip install "openwakeword>=0.6.0"
```

Two deliberate exclusions:
- An *absent* package is not an error. `WakeWordEngine.load()` already reports
  that honestly and the assistant falls back to text mode; raising would take
  text mode down over an optional feature.
- `jarvis.main` imports the voice loop lazily, so the import-time check fires
  only when voice is entered. Verified: `import jarvis.main` still succeeds.

### Results
- The two real-dependency wake tests (`test_bare_jarvis_disabled_without_verified_model`,
  `test_single_model_session_for_all_phrases`) pass on 0.6.0.
- 6 new tests in `TestVersionGuard` cover the floor, the requirements pin
  agreeing with it, the too-old message, absent-is-not-an-error, and numeric
  comparison.
- Full suite: 1314 passed, 2 skipped.

## Follow-up test flaked under load; the flake was a real product bug

### Symptom
`TestFollowUp::test_multiple_follow_ups_are_allowed` passed in isolation and
failed in the full suite. Measured before the change: **0 / 50** solo and
**0 / 20** in the full suite -- so the flake is rare, roughly 1 in 150 runs,
and cannot be reproduced on demand without load. With six CPU hogs running it
became **3 / 40**, which made it investigable.

### Two independent defects, neither of them a sleeping problem

**1. The detector was reset inside the utterance it was collecting.**

`_run_turn` ends with a `finally` that resets the VAD. But `_await_follow_up`
returns the moment the user speaks, and their speech is collected from that
instant onward -- so the turn thread outlives the follow-up window opening. The
teardown reset then landed *inside* the new utterance.

Traced frame by frame under load:

```
... speech start fired
... sil_ms = 80, 160, 240, 320, 400, 480, 560, 640   (end_silence_ms is 700)
... voiced_ms=0 sil_ms=0                             <- reset; one frame short
```

The reset arrives during the hangover, the `end` event never fires, the turn
never starts. The user is answered with silence for a request they can hear
themselves making.

Fix in `jarvis/voice_loop.py`: the teardown resets only when the detector is not
already mid-utterance (`if not self.vad.in_speech`). This is a product bug, not
a test artefact -- the follow-up window is precisely when speech must not be
dropped.

**2. The test asked for turn ids by polling a moving target.**

`_await_turn` sampled `machine.turn` every 10 ms. A turn can be superseded
before any poll lands, so a test waiting for `turn_003` waits out its timeout for
an id that is already gone and reports "never started" while three turns ran.
Replaced with `TurnRecorder`, which subscribes to the machine's event stream:
no sampling gap, so no turn can be missed. Failures now print the actual state
sequence instead of just the awaited id.

### Test changes
- `make_follow_up_loop` fixture: a private `ConversationMachine` per test, and
  it **asserts `start()` succeeded**. `_VoiceProcessLock` is an flock on a temp
  file keyed by uid, shared by every loop in the process; previously
  `loop.start()`'s result was discarded, so one leaked loop starved the next and
  the symptom was a silent full-timeout wait.
- Both follow-up tests now wait for the loop to be at rest *before* injecting and
  for the turn to reach FOLLOW_UP *before* the next utterance. Injecting while a
  turn is in flight is a barge-in by design, which cancels the turn and burns a
  turn id -- the original cause of the ids slipping.
- All waits block on a `Condition` with a 5 s timeout, not a sleep-and-retry.
- New `test_a_turn_ending_never_wipes_an_utterance_already_in_progress` covers
  defect 1 directly; verified non-vacuous (fails with the guard reverted).

### Results
- `TestFollowUp` under 6x CPU load: **0 / 80** (was 3 / 40).
- Full suite: 5 consecutive clean runs, zero failures.
- No retries, no skips added.

## App launch: per-OS alias map (option B)

### What changed
The allowlist was a flat list of macOS app names plus a single hardcoded
`open -a` call, and `launch_app` refused outright on anything but Darwin. That
meant "add another app" and "work on Windows" needed different mechanisms, and
the non-macOS suite existed only to prove the argv path was reachable at all.

`applications.aliases` is now a map from a spoken name to the launch command per
operating system:

```yaml
applications:
  aliases:
    Safari:
      Darwin: ["open", "-a", "Safari"]
      Windows: ["C:\\Program Files\\Safari\\Safari.exe"]
      Linux: ["safari"]
```

**The map is the allowlist.** A name that is not a key is refused without the OS
being consulted, exactly as before; there is simply no second list to keep in
sync. `ALLOWED_APPS`, `APP_ALIASES` and `_validated_app_allowlist` are gone,
replaced by `APP_COMMANDS` (casefolded name -> platform -> argv) and
`APP_DISPLAY_NAMES` (so the assistant says back "Safari", not "safari").

Two refusal modes are now distinguished, because they are different problems: a
name that is not mapped at all, and a mapped name with no command for the OS
you are running on. The second says so rather than guessing a command.

### Security posture is unchanged, and asserted
- `subprocess` is still called with an argument list and **never** with a shell,
  on every platform. A new test asserts argv[0] is never `cmd`, `sh`, `bash`,
  `powershell` or friends -- so argv-only cannot quietly become a fiction via a
  `cmd /c` mapping. The Windows entries are the executable itself for exactly
  that reason.
- A spoken name is looked up, never concatenated: the injected-name tests still
  assert nothing reaches the OS.
- Resolution is still pure data. `lookup`/`resolve_command` cannot execute
  anything, and a returned table is a copy, so a caller mutating it cannot
  corrupt the map.
- Default deny survives: an absent, empty or malformed map launches nothing.

### Tests
`tests/test_phase3_applaunch.py` is rewritten and no longer carries `MACOS_ONLY`;
per-OS coverage comes from monkeypatching `platform.system()` and asserting the
argv for Darwin, Windows and Linux, so those assertions now run on every host.
`tests/test_phase3_applaunch_linux.py` is deleted: it existed to cover what the
macOS-only suite skipped, and its final test asserted the exact opposite of what
option B requires (that a non-Darwin host is refused). Its still-valid
properties are all present in the single suite.

Every old property is retained or deliberately inverted. Inverted, by design:
the canonical-name indirection and the "an alias cannot authorize" pair (in B the
map *is* the authorization, so there is nothing for an alias to bypass), and the
two non-Darwin refusal tests, which are now per-OS success tests.

### Results
Full suite: 5 consecutive clean runs, zero failures.

## Every visual input is labelled with its source

### Problem
A vision model cannot tell a screen capture from a desk camera. Both arrive as
"a photo of something", so an unlabelled image invites a confidently wrong
answer -- and nothing in the stack recorded where an image had come from. The
scene prompt at `SCENE_INSTRUCTION` even asserted "this is a webcam frame", which
was true of the one producer that existed and would have been a lie for the next
one.

### Change
`jarvis/providers/base.py` gains the source vocabulary -- `screen`, `webcam`,
`attachment` -- and `image_part()` now takes **`source` as a required keyword
with no default**. That is the whole guarantee: an image cannot be built without
a label, so "no image is sent without a label" is structural rather than a
convention someone has to remember. An unlabelled image is refused with a
`ValueError` naming the valid sources.

Two places carry the label, because they are not redundant:

- the attachment dict, so the source is structured data any layer can read;
- a rendered text line in the turn context, because the adapters read only
  `mime` and `data` and drop every other field. A source that lived only on the
  dict would never reach the model. A test asserts this directly: the OpenAI
  wire format's image part contains nothing but `url`.

`vision.describe_sources()` renders one line per image, saying what each source
*means* ("a screen capture -- it shows windows, a desktop, or UI"), because a
bare word helps nobody. The mandated sentence --
`SOURCE_NOTE`, "Images are labelled by source. Do not assume a webcam image is a
screen capture." -- is in the system prompt, so it holds on every turn, and is
repeated in the per-turn block next to the labels it describes.

An unlabelled image reaching `describe_sources()` is reported as `UNLABELLED`
rather than omitted, since reaching one means something bypassed `image_part`
and silence would hide exactly that.

### Tests
New `TestImageSourceLabels` in `tests/test_phase3_vision.py`, 22 cases: the
three sources, the required-argument refusal, unknown/unhashable sources
rejected, every label appearing in the rendered block and in the outgoing
prompt, both provider adapters, multi-image turns, and a text-only turn staying
byte-for-byte unchanged.

Verified non-vacuous both ways: making `source` optional again fails 9 of them;
stopping the prompt rendering fails 5.

### Not done, deliberately
`take_screenshot` still saves a PNG and returns a path -- it never sends the image
to the model. `screen` therefore has no producer yet. Wiring screenshots into the
model is a new user-facing capability, not a labelling change, so it is left for
a deliberate decision rather than smuggled in here.
