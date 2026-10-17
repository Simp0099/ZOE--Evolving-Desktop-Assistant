# Troubleshooting

Start here. Most reports fall into one of the categories below.

---

## macOS microphone and accessibility permissions

macOS will silently give an app a **silent** microphone rather than an error,
which looks exactly like a broken assistant. This is the first thing to check
for anything voice-related.

Terminal-launched processes are the hard case: the permission is granted to the
**terminal application**, not to Python. Granting it to Python does nothing for
`python -m zoe` run from a terminal.

1. **Microphone** — System Settings → Privacy & Security → Microphone. Turn on
   the app you actually launch from: Terminal, iTerm, VS Code, whatever it is.
   Check both the switch and the per-app list.
2. **Accessibility** — Privacy & Security → Accessibility. Needed if anything
   synthesises or sends keystrokes (screenshot tools, UI automation).
3. **Camera** — Privacy & Security → Camera. Only needed if you set
   `vision.webcam.enabled: true`. Off by default, and nothing opens it until you
   do.
4. **Quit and relaunch the terminal app** after changing a permission. The
   decision is cached at launch.

If a permission was denied, macOS records it in the unified log:

```bash
log show --predicate 'process == "ttserver"' --last 10m | grep -i microphone
```

The symptom of a *silently* denied mic: `scripts/mic_check.py` reports a device
but the level never rises above the noise floor, and the voice loop rests in
`IDLE` forever because nothing ever wakes it.

### Which device is it actually using?

```bash
python scripts/audio_devices.py
```

Lists every PortAudio device with the configured and default input/output. Pin
explicit indices in `config.yaml` under `conversation.input_device` and
`conversation.output_device` if the default is wrong — Bluetooth headsets
routinely present a capture device that is not the one you are wearing.

---

## The assistant never wakes up

The loop rests in `IDLE` and only a wake word leaves that state. If nothing is
being heard:

1. **`wake_word.enabled` is `true`** in the shipped config, so voice mode waits
   for "hey jarvis". It does *not* start listening for commands directly.
2. Check the wake word model is downloaded. `start.py` reports this under
   `[Check] Wake word model: NOT FOUND`; `python -m zoe.main --test` downloads
   models.
3. Check the threshold. `wake_word.phrases[].threshold` is 0.5 by default; higher
   is stricter. `scripts/wake_eval.py` scores the real engine on synthetic
   negatives and reports false firings.
4. Check permissions (above).
5. To skip the wake word entirely, set `wake_word.enabled: false`. There is no
   flag for this — `--no-wake` is parsed and ignored.

Only `hey_jarvis` has a verified built-in model in openWakeWord 0.6.0. Other
phrases in `config.yaml` are inert until a model is supplied; see
[wake_phrases.md](wake_phrases.md).

## Voice mode says "Voice is unavailable"

`start.py`/`main.py` print this and return when the microphone or the wake-word
model cannot be opened. The reason is in `data/zoe.log`, not the terminal —
the terminal only gets the one-line summary.

Common causes, in order of likelihood: a denied or silently-denied microphone
permission; another process holding the device (the loop takes a process-wide
lock and refuses to start a second instance, naming the owning pid); a missing
openWakeWord model; a device index in `config.yaml` that no longer exists.

`--text` always works regardless; voice is a pathway, not the brain.

## The assistant won't open an app

The allowlist in `applications.aliases` is the security boundary and it stays
that way. A name that is not in it is never launched directly.

There are three outcomes, and the difference between them matters:

1. **It is in the allowlist** — it opens immediately.
2. **It is not, but exactly one installed app matches** — the assistant says
   *"I found X. Shall I open it?"* and opens **only if you say yes in your next
   message**. A single word is enough; anything else ("no", "cancel", silence, a
   different question) discards it. The offer expires after
   `APP_CONFIRM_TIMEOUT_S` (default 120 s).
3. **Nothing matches, or several do** — nothing is opened. For several matches
   you get a list of up to five and are asked which; say the full name.

If you want an app to open without the confirmation step, add it to
`applications.aliases` in `config.yaml`:

```bash
python scripts/list_apps.py            # prints the aliases: block to paste in
python scripts/list_apps.py --filter chrom
python scripts/list_apps.py --dirs     # which directories are scanned
```

It prints only. It never writes `config.yaml` — pasting and editing is a
deliberate step, because that file is the security boundary.

Scanned on macOS: `/Applications`, `/System/Applications`, `~/Applications`.
A missing or unreadable one is skipped, not fatal.

## The assistant interrupts itself

The reply comes out of the speakers and comes back in through the microphone,
which looks like the user talking. This is the most common voice complaint and
it is architectural, not a bug to be configured away.

Ordered things that actually help:

1. **Use headphones.** This removes the speaker-to-microphone path entirely and
   fixes it completely. Nothing else comes close.
2. Raise `vad.barge_in_ms` (default 300). Interrupting mid-sentence should take
   more intent than starting a new utterance.
3. Raise `vad.barge_in_snr` (default 4.0) if the room is loud. Ambient RMS on a
   typical desk measured 0.046–0.14, which is above any absolute threshold tuned
   in a quiet room — the SNR margins above the measured floor are what decide
   this, not the absolute threshold.
4. `vad.echo_guard_enabled` (default `true`) gates barge-in on the residual
   after subtracting a learned estimate of our own playback. Leave it on.
5. `vad.barge_in_grace_ms` (default `0`, i.e. off) suppresses speech starting
   just after our own audio began. Raising it to 800–1000 helps on a laptop
   speaker **and delays a genuine early interruption by the same amount**. Leave
   it 0 with headphones.

The measurements behind all of this are in
[barge_in_diagnosis.md](barge_in_diagnosis.md); to see what actually happened in
a real session:

```bash
python scripts/analyse_barge_in.py     # reads data/zoe.log*
```

A known, documented, **unresolved** gap: in a reverberant room (200 ms RT60) the
single-tap `EchoCanceller` removes 0% of the echo energy, and the assistant's
own voice inflates the VAD noise floor past the barge-in bar. This is pinned by
`strict=True` xfails in `tests/test_echo_reverb.py`. Headphones fix it; code
would need a partitioned or sub-band canceller.

**On a MacBook's built-in speaker and microphone this happens in an ordinary
room, not only a reverberant one.** Measured from a real session: every reply is
cut off 0.80–0.97 s in, with the microphone reading 0.19–0.51 RMS against a
barge-in bar of 0.023–0.046, and `lag_ms` swinging between 5 and 280 ms from
frame to frame because the search runs off the end of its 200 ms window. The
`builtin_speaker` profile in `config.yaml` raises the thresholds for this
device, and **does not fix it** — the leak is 4–20× above even the raised bar.
It is a mitigation, not a repair. Use headphones, or wait for a canceller that
models the reverberant tail.

## No sound comes out

1. `python -m zoe --status` prints the configured TTS engine. Only `kokoro`
   and `pyttsx3` are supported; `say` and Chatterbox are **not** selectable —
   Chatterbox's implementation was removed, and startup says `UNSUPPORTED`
   rather than misleadingly reporting an available engine as healthy.
2. Kokoro is not in `requirements.txt`. Without it spoken replies fail with an
   install hint. `pip install kokoro soundfile`.
3. Output device index wrong — see `scripts/audio_devices.py`.
4. Check the log for a synthesis error rather than guessing:
   `python scripts/tts_check.py` renders a phrase, reports the pauses, writes a
   WAV and plays it. If the WAV is fine, the problem is playback, not synthesis.

## Speech is not being recognised

`speech.asr_engine` in `config.yaml` does nothing — it is parsed and never read.
`Transcriber` always tries faster-whisper first and falls back to Google Speech
only when faster-whisper cannot be imported.

- **Want fully offline?** Uninstall faster-whisper; `speech_recognition` is then
  never reached.
- **Garbage transcripts / too slow?** Lower `asr.model` for speed
  (`small.en` → `base.en`) or raise it for accuracy (`distil-large-v3`). Measured
  warm medians: `base.en` 0.462 s, `small.en` 1.192 s, `distil-large-v3` 4.082 s.
  Re-measure after changing it:
  `python scripts/first_audio_check.py --reps 3`.
- **A `ValueError` about "captured PCM must contain complete int16 samples"**
  means an odd-length buffer reached the transcriber. That is a real bug in the
  caller, not a config problem.

## The model is never chosen / every request fails

`python -m zoe --status` shows which models are enabled and configured.
Startup aborts with a clear message when no enabled model has a usable
credential.

- **"No available model"** — the credential for every enabled provider is
  missing. Keys are read from `.env` or the shell environment, never from
  `config.yaml`.
- **OpenCode Zen returns 403 `FreeTierError`** — the free tier only works from
  inside the OpenCode client. This is why `big_pickle` ships disabled. Note
  that a 403 classifies as a *non-retryable* auth error, so selecting a blocked
  model aborts the request instead of falling through to the next candidate.
  Prefer Gemini, which is the only provider verified by a live call.
- **429s** — the daily request cap parks a model after `routing.daily_request_cap`
  consecutive quota failures, until the provider's reset.
- **Debug it**: `--debug` shows the exact routing decision and the chosen model;
  `debug.log_input: true` in `config.yaml` logs the same to the file. Never logs
  credentials.

## The weather tool says "Unknown tool"

Already fixed — `handle_weather` used to dispatch `get_temperature`, which was
never in `TOOL_REGISTRY`, so **every** weather request failed. If you see this,
you are on a commit before that fix.

## A reply is truncated mid-sentence

`voice.max_spoken_words` (default 60) caps spoken replies. Longer replies are cut
at a sentence boundary and the assistant offers more detail. Raise it, or ask
for the detail in a follow-up — the follow-up window is
`conversation.follow_up_window_s` (default 8 s).

## A plugin is not being loaded

It logs one line and is skipped. The usual reasons, in order:

1. The file starts with `_` — templates are never loaded.
2. `TOOL["name"]` collides with a built-in tool or another plugin.
3. `name` is not snake_case, 2–48 chars, starting with a letter.
4. More than 8 parameters, or a parameter type other than
   `str`/`int`/`float`/`bool`.
5. `run` is missing or not callable.

`zoe.plugins.describe()` returns every file with its load status and the
reason. Full rules: [PLUGINS.md](PLUGINS.md).

## Logs

Everything is in `data/zoe.log`, rotated at 1 MB with 3 backups
(`logging.max_bytes`, `logging.backup_count`).

```bash
tail -f data/zoe.log                 # follow
python -m zoe --debug                # add console diagnostics for one run
```

The terminal deliberately shows user-facing output only. `--debug` is how you
get internals there.

---

## The three reported runtime problems

The brief for this documentation pass referred to "the three problems below"
and the problems were not included in it. This section is a placeholder.

Once the three are named, each gets its own entry here: the observable
symptom, the command that reproduces it, the root cause, and the fix. Nothing
has been guessed or written speculatively in the meantime — a troubleshooting
entry that describes a problem nobody has confirmed is worse than no entry.
---

## Electron UI

The whole UI is one command now:

```bash
cd frontend/zoe
npm run dev:desktop
```

That starts Vite, waits for it to answer on `:5173`, opens Electron, and lets
the Electron main process start `uvicorn` if nothing is already serving `:8001`.

**Blank window on `npm run desktop`.**
The Vite dev server is not running. `npm run desktop` waits for `:5173` and, if
nothing answers within 30 s, shows a short page saying so instead of a blank
window. Either run `npm run dev` first or use `npm run dev:desktop`.

```bash
curl -sI http://localhost:5173 | head -1     # expects HTTP/1.1 200
```

**"Zoe cannot connect to the assistant." / the composer says "Backend offline".**
The backend is not up.

```bash
curl http://127.0.0.1:8001/health
```

No answer → `npm run dev:desktop` (or
`python -m uvicorn server:app --host 127.0.0.1 --port 8001`) in a terminal.
Electron prints its own reason when it cannot start one — usually no
`.venv` at the project root.
An answer but the UI still fails → the page origin is not in `ALLOWED_ORIGINS`
in `server.py`. Add the port there; do **not** widen it to `*`, because the API
can read files and launch applications.

**Typing does nothing in the UI.**
The textarea is disabled only while a request is in flight, never because of
backend state — so this means either a request is genuinely stuck (press
**Stop**, or restart), or the window is blank because Vite is not serving
`:5173` (see above).

**Transcription always fails (HTTP 503, "audio decoding is unavailable").**
`server.py` imports **PyAV** to decode WebM/Opus. PyAV is now pinned in both
`requirements.txt` and the `[server]` extra; an install predating that change
still needs it.

```bash
python -c "import av; print(av.__version__)" || pip install "av>=12.0.0"
```

**Mic button does nothing in the UI.**
`getUserMedia` needs a secure context. `localhost` qualifies; a LAN IP over plain
HTTP does not. Also check macOS microphone permission for Electron. Text mode
still works, which is the fastest way to confirm the backend is fine. The button
is only disabled when `/health` reports `voice_input_available: false`; an
offline backend gives a spoken error on click rather than a dead button.

**`npm test` finds no tests.**
It runs `node --test "src/**/*.test.js"`; frontend tests must live in `src/` and
end in `.test.js`.

## After renaming or moving anything

A test that reads source by path can silently check nothing. This already
happened: the credential-literal scan in `tests/test_integration.py` pointed at
`jarvis/` after the package was renamed to `zoe/`, and passed while scanning one
file instead of 47.

```bash
grep -rn "PROJECT_ROOT" tests/ | head
python -m pytest -q
```

Run the suite after any move. A suite that still passes is not proof the move
was safe.
