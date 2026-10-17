# Setup

How to install Ai Partner, and how to verify an install actually works.

## Requirements

- **Python 3.10, 3.11 or 3.12.** Kokoro declares `>=3.10,<3.13`, so a 3.13+ or
  3.14 interpreter cannot install it. Check with `python3 -V` and build the
  virtualenv with a 3.12 interpreter if your system default is newer.
- macOS for the voice path (the wake-word, microphone and speaker work is
  CoreAudio-specific). Text mode runs anywhere.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then put at least one enabled provider key in `.env`. See `.env.example`.

## Verify the install

Two commands, in this order.

```bash
python -m zoe --status
```

Prints the resolved configuration, which models are enabled and have
credentials, memory statistics, log file and voice state — then exits without
starting anything. Startup aborts with a clear message when no enabled model has
a usable credential.

```bash
python -m zoe.main --test
```

**This is the in-repo check.** Eight pipeline checks against an in-process probe
provider, so they verify this project's routing and tool-loop logic rather than
a model's mood: verbatim input forwarding, tool-result feedback and loop
termination, fallback when a model fails, capability filtering, an honest error
when every candidate fails, empty-input rejection, and model-layer load. The
eighth makes one **live** model call and reports it separately, because provider
availability is not something this repository controls. Exits non-zero if any
check fails.

Do not confuse it with `python -m zoe --test`, which runs
`tests/test_integration.py` through its own standalone runner instead.

Then start it:

```bash
python -m zoe --text     # typed, no microphone — works everywhere
python -m zoe            # voice; needs the permissions below
```

## macOS permissions

Voice mode needs two macOS permissions, and macOS grants them to the **terminal
application**, not to Python. Granting them to Python does nothing for
`python -m zoe` run from a terminal.

- **Microphone** — System Settings → Privacy & Security → **Microphone**. Enable
  the app you launch from (Terminal, iTerm, VS Code).
- **Accessibility** — Privacy & Security → **Accessibility**. Needed only by
  anything that synthesises or sends keystrokes.
- **Camera** — Privacy & Security → **Camera**. Only if you set
  `vision.webcam.enabled: true`. It is `false` by default and nothing opens a
  camera until you change that.

Quit and relaunch the terminal app after changing a permission; the decision is
cached at launch.

A denied microphone usually produces **silence, not an error**, which is
indistinguishable from a broken assistant. Check it directly:

```bash
python scripts/mic_check.py        # records 4 s, prints the level per half second
python scripts/audio_devices.py    # lists devices and the configured/default ones
```

If the level never rises above the noise floor, it is the permission, not the
assistant. Full procedure in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Optional: faster-whisper (speech recognition)

Local speech recognition runs on **faster-whisper**, which *is* in
`requirements.txt` because it is the primary ASR path. Its model is a config
value, not a hard-coded default, so the accuracy/latency tradeoff is a one-line
edit in `config.yaml`:

```yaml
asr:
  model: "small.en"
  compute_type: "int8"
```

The shipped `small.en` trades accuracy for warm latency (measured warm medians:
`base.en` 0.462 s, `small.en` 1.192 s, `distil-large-v3` 4.082 s). After
changing it, re-measure rather than trusting the number:

```bash
python scripts/first_audio_check.py --reps 3
```

If faster-whisper is **not installed**, `Transcriber` falls back to Google
Speech, which sends audio to a remote service. `speech.asr_engine` in
`config.yaml` does *not* control this — it is parsed and never read. To force
fully offline recognition, uninstall faster-whisper; `speech_recognition` is
then never reached. See [CONFIGURATION.md](CONFIGURATION.md).

## Optional: openWakeWord (wake word)

`openwakeword>=0.6.0` is in `requirements.txt` and is what lets the assistant
stay offline for activation. It is pinned **`; sys_platform == "darwin"` only**:
openWakeWord requires `tflite-runtime`, which has no Linux wheel for Python
3.12, so an unconditional pin makes `pip install -r requirements.txt` fail on
Linux. On other platforms install it explicitly if you want the wake word:

```bash
pip install "openwakeword>=0.6.0"      # Linux, or any platform
```

An absent package is **not** an error: `check_openwakeword_version()` returns
early when the distribution is missing, `WakeWordEngine.load()` reports the
failure and returns `False`, and the assistant falls back to text mode. The
wake-word tests that need the real package skip with a reason rather than
failing. macOS behaviour is unchanged.

0.6.0 is a hard floor, not a preference: it is the
first release exposing the installed-model catalog this code validates against,
and an older install otherwise fails much later as an opaque `AttributeError`
deep inside the engine.

Models download on first use. To fetch them without starting the assistant:

```bash
python -m zoe.main --test
```

With `wake_word.enabled: true` (the shipped default) the assistant waits for
"hey jarvis" before it does anything. That is the only phrase with a verified
built-in model — see [wake_phrases.md](wake_phrases.md).

## Optional: Kokoro text-to-speech

Kokoro is the configured TTS engine and is the heaviest optional dependency, so
it is deliberately **not** in `requirements.txt`. Without it the assistant still
starts and every test except the Kokoro-specific ones passes; spoken replies
simply fail with an install hint.

```bash
pip install kokoro soundfile
```

`pip install kokoro` pulls `torch` as a dependency, which is the bulk of the
download. On an Apple-silicon Mac, install torch for your platform first if you
want the MPS backend:

```bash
pip install torch torchaudio          # CPU by default; MPS wheels are selected
                                      # by the standard torch install selector
```

### Verify

```bash
python scripts/tts_check.py    # renders a phrase, reports pauses, writes a WAV, plays it
python -c "from zoe import tts; a = tts.synthesize('Ready.'); print(a.shape, a.dtype)"
```

The second command prints a `(samples,) float32` array. It loads the model on
first use, so the first run is slow and later runs are fast. If it prints a
`float32` array of zeros for non-empty text, the engine is installed but
misconfigured.

## Running the tests

```bash
python -m pytest -q
```

### The `requires_kokoro` marker

Tests that exercise the real Kokoro engine are marked
`@pytest.mark.requires_kokoro`. The **repository-root `conftest.py`** checks for
the package with `importlib.util.find_spec("kokoro")` — which finds the spec in
microseconds and never imports torch — and, when it is absent, skips those tests
with:

```
kokoro not installed; see docs/SETUP.md
```

The hook lives in the **repository-root `conftest.py`**, which is loaded for
every test under `tests/` however the suite is invoked. `tests/conftest.py`
carries an identical copy of the same hook — redundant rather than harmful.

So on a machine without Kokoro the suite reports **skips, not failures**. It
does not report failures for a package you chose not to install.

Other skips are unrelated to Kokoro — a live-provider probe that needs network
and credentials, for instance. `python -m pytest -q` always prints what it
skipped and why; read the `-ra` summary rather than a remembered number.

Run just the Kokoro tests with:

```bash
python -m pytest -m requires_kokoro -q
```

With Kokoro installed they run; without it they skip with the reason above and
the rest of the suite is unaffected.

## Optional extras

Everything in this section is imported lazily inside a `try/except`, so the app
starts and the whole test suite runs without it. Only the one feature listed
degrades. `requirements.txt` lists these three as commented-out lines with the
same notes; this is the single place that explains them.

| Package | Feature | Behaviour when absent |
| --- | --- | --- |
| `pyautogui` | `take_screenshot` | Logs a warning and returns `""` — no file is written, and the caller is told the capture failed rather than getting a silent success. |
| `pywhatkit` | `play_music` | Falls back to opening a YouTube search in the browser. |
| `pyttsx3` | `speech.tts_engine: "pyttsx3"` | Only reached when you select it; `kokoro` (the configured default) is unaffected. `start.py` reports the engine as selected-but-not-installed. |
| `kokoro` | `speech.tts_engine: "kokoro"` (the default) | Spoken replies fail with an install hint. See above. |

```bash
pip install pyautogui pywhatkit pyttsx3   # or any subset
```

### Spreadsheet / document reading

The `file_reader` plugin reads `.txt`, `.md`, `.csv` and `.json` with the
standard library. `.pdf`, `.docx` and `.xlsx` need three more packages:

```bash
pip install -e ".[reader]"
```

Without them the plugin answers with the install command rather than the file's
contents.
---

## Environment variables

Secrets live only in `.env`, which is git-ignored. `.env.example` holds
placeholders. Copy it and fill in only what you use:

```bash
cp .env.example .env
```

| Variable | Read by | Needed for |
| --- | --- | --- |
| `GEMINI_API_KEY` | `zoe/providers/gemini.py` | The verified provider |
| `OPENROUTER_API_KEY` | `zoe/providers/openai_compat.py` | OpenRouter models |
| `OPENCODE_ZEN_API_KEY` | `zoe/providers/openai_compat.py` | OpenCode Zen models |
| `NASA_API_KEY` | `zoe/tools/nasa.py` | The NASA APOD tool |
| `GMAIL_ADDRESS` | `zoe/tools/email_tool.py` | Sending email |
| `GMAIL_APP_PASSWORD` | `zoe/tools/email_tool.py` | Sending email — a Google **App Password**, not the account password: <https://myaccount.google.com/apppasswords> |

At least one **enabled** provider in `models:` must have a working key, or the
assistant starts and then cannot answer. `--status` reports how many models are
usable.

Tunable environment overrides read by `zoe/config.py`:
`EMAIL_CONFIRM_TIMEOUT_S` (default `120`) and `APP_CONFIRM_TIMEOUT_S`. Do not set
these casually — the first is a safety window.

## The UI bridge

`server.py` is a separate dependency set, because the terminal assistant does not
need it:

```bash
pip install -e ".[server]"     # fastapi, uvicorn, python-multipart, soundfile, scipy, PyAV
```

**PyAV is required by `server.py`** — it imports `av` to decode the browser's
WebM/Opus recordings, and without it `/transcribe` returns HTTP 503 with "audio
decoding is unavailable". It is now pinned in **both** `requirements.txt` and the
`[server]` extra, so either install path is enough. An install predating that
change needs `pip install "av>=12.0.0"`.

## Running the Electron UI

One command. It starts Vite, waits for `:5173`, opens Electron, and lets the
Electron main process start the backend if nothing is already serving `:8001`:

```bash
cd frontend/zoe
npm install          # once
npm run dev:desktop
```

Electron polls `/health` for up to 30 s while starting the backend. If it never
answers, the terminal says why and the window still opens in its offline state —
you can keep typing, and a message sent offline reports the real error instead of
failing silently. A backend **you** started by hand is never killed when Electron
exits; only one Electron started itself.

If you prefer to run the backend yourself, the individual scripts still work:

```bash
# terminal 1 — the backend
python -m uvicorn server:app --host 127.0.0.1 --port 8001

# terminal 2 — install once, then dev server and window
cd frontend/zoe
npm install
npm run dev        # Vite on http://localhost:5173
npm run desktop    # in a third terminal, or after dev is up
```

Install with **`npm ci`** if `frontend/zoe/package-lock.json` is intact; it is
the reproducible path. Use `npm install` only after deliberately changing a
dependency.

Verify the backend independently:

```bash
curl http://127.0.0.1:8001/health
```

`status`, `voice_input_available`, `voice_output_available`, `models_usable` and
`tts_engine` are real capability flags — the endpoint never loads a model to
answer.

Build validation:

```bash
cd frontend/zoe && npm run build    # writes dist/, which is git-ignored
```

A successful build is **not** an end-to-end result. See
[FRONTEND.md](FRONTEND.md) for the integration gaps, including that Electron
loads the dev-server URL unconditionally and never loads `dist/`.

## Clean reinstall

```bash
rm -rf .venv frontend/zoe/node_modules frontend/zoe/dist
find . -name __pycache__ -type d -prune -exec rm -rf {} +
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e ".[server,reader]"
cd frontend/zoe && npm ci
```

This does not touch `data/`. Your memory database, logs and recordings live
there and are yours; they are never cleaned automatically.
