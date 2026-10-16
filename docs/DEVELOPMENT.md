# Development

## Layout

```text
zoe/                  the assistant package — everything the brain needs
  __main__.py         python -m zoe  ->  start.main()
  brain.py            ZoeBrain.ask(), the tool registry, the bounded tool loop
  voice_loop.py       wake word -> VAD -> ASR -> reply -> barge-in
  audio.py            the single owner of the microphone and the speaker
  speech_pipeline.py  Transcriber, SpeechPlayer, playback queue
  speech.py tts.py    engine selection, Kokoro loader, console fallback
  router.py           deterministic ranked model selection
  model_layer.py      provider construction from config; the registry
  providers/          base contracts, gemini, openai_compat
  intents/            local commands that bypass the model router
  tools/              built-in tool implementations
  plugins.py          drop-in tool loader
  conversation.py     the interaction state machines
  tone.py             conversation tone: mood, energy, warmth, decay
  memory.py           SQLite facts + capped session history
  config.py           every CONFIG read from config.yaml and the environment
plugins/              drop-in tools, loaded on next launch
scripts/              live diagnostics, benchmarks, doc generation
tests/                offline suite
frontend/zoe/         the Electron UI (see FRONTEND.md)
docs/                 current documentation
docs/history/         dated phase reports — history, not current behaviour
data/                 runtime state; git-ignored; never cleaned automatically
```

Root files: `start.py` (startup checks and mode dispatch), `server.py` (the UI
HTTP bridge), `config.yaml` (behaviour, no secrets), `.env` (secrets, ignored),
`pytest.ini`, `pyproject.toml`, `requirements.txt`, `conftest.py`.

## Local workflow

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e ".[server]"
cp .env.example .env

python -m pytest -q                    # the main correctness gate
python -m zoe --status                 # config, provider, memory, voice state
python -m zoe --text                   # text mode: no microphone, no speaker
python -m zoe --debug                  # voice mode plus console diagnostics
python -m zoe.main --test              # in-repo pipeline probe
```

`start.py` is the only mode dispatcher. There are exactly two interactive modes
(text and voice) and three non-interactive commands (`--status`, `--test`,
`--debug`). `--no-wake` is parsed and ignored; wake-word behaviour is
`wake_word.enabled` in `config.yaml`.

## Running the UI

See [FRONTEND.md](FRONTEND.md). Short version: `uvicorn server:app` on port
8001, then `npm run dev` and `npm run desktop` in `frontend/zoe/`.

## Adding a tool

Built-in tools live in `zoe/tools/` and are registered in `zoe/brain.py`:

1. Write the function. It takes plain arguments and returns a **string** — the
   text the model will read. Raise or return an explanatory string; never print.
2. Add a registry entry in `brain.py`'s `TOOL_REGISTRY`: the name, a description
   the model reads to decide when to call it, and the parameter names.
3. Decide the trust level. Anything that reads files, opens applications,
   reaches the network or sends mail needs the same scrutiny as the model
   itself, because the model chooses its arguments.
4. Add tests. Offline, no network.
5. If it needs a credential, read it in `zoe/config.py` from the environment and
   redact anything that could echo it back.

## Adding a plugin

Prefer a plugin to a built-in tool: it needs no edit to the core and is
independently testable.

```bash
cp plugins/_template.py plugins/my_tool.py
```

The contract, taken from `plugins/_template.py` and enforced by `zoe/plugins.py`:

```python
TOOL = {
    "name": "my_tool",                  # snake_case, unique, not a built-in name
    "description": "What it does, and when the model should reach for it.",
    "parameters": {"text": "str"},      # every param required; str, int, float, bool
}

def run(text: str) -> str:
    return "..."                        # the text the model reads
```

The loader rejects a plugin whose name collides with a built-in tool, coerces
declared argument types, applies a timeout, and contains a crash so it returns a
controlled error instead of killing the assistant.

Two things it does **not** do: sandbox, and permission-check. A plugin runs in
the assistant's process with your permissions. Validation is a correctness
guarantee, not a security boundary — see [SECURITY.md](../SECURITY.md).

Rules that are not obvious: files starting with `_` are never loaded; a `run()`
that takes longer than the timeout returns a timeout message, it is not killed
mid-send; and a plugin that needs a third-party package should import it lazily
so the assistant still starts when it is absent.

Shipped examples: `word_count.py` (minimal), `file_reader.py` (path resolution,
size limits, untrusted-content fencing), `Philips.py` (a LAN device, untracked —
needs `pywizlight`).

## Adding UI functionality

`src/api.js` is the only place that talks to the backend; add endpoints there,
not in a component. If the endpoint does not exist yet, add it to `server.py`
first — it is the boundary where HTTP becomes brain calls, and it is the right
place for validation and caps.

Voice behaviour goes through the state machine in `src/voiceState.js`. Do not
add a boolean for a new state; add the state and its transitions, or the UI can
lie about what it is doing.

## Conventions

- Comments explain **why**, and name the failure they prevent. The codebase
  already does this; a comment that restates the code is noise.
- Never log a body, a key or a credential. Route through
  `zoe/providers/base.py::redact`.
- Paths are resolved relative to the project root, never the cwd.
- Prefer a bounded failure that returns a clear message over an exception that
  reaches the user as a stack trace.
- Lazily import heavy or optional dependencies inside `try/except` so the
  assistant starts and the rest of the suite runs without them.

## Testing expectations

Every behavioural change gets an offline test. The rules are in
[TESTING.md](TESTING.md); the two that bite hardest:

- Tests must pass with no network and no hardware. Skip with the real reason
  rather than faking a pass.
- Several tests read source by path relative to the project root. **Before you
  move or rename anything, grep for it.** A rename breaks those tests silently
  until you run the suite — that is exactly how the credential-literal scan in
  `tests/test_integration.py` came to check a single file for a whole release.

## Dependency management

- Python: `requirements.txt` for what the app needs, extras in `pyproject.toml`
  for optional groups (`server`, `reader`). Anything optional must be imported
  lazily inside `try/except` and must degrade with a message, not a crash.
- Node: `frontend/zoe/package.json` + `package-lock.json`, installed with
  `npm ci`. Do not introduce a second package manager or a second lockfile.
- Do not upgrade dependencies as part of an unrelated change.

## Generated documentation

`docs/CONFIGURATION.md` is **generated** from `config.yaml` and `zoe/config.py`:

```bash
python scripts/gen_config_reference.py            # rewrite
python scripts/gen_config_reference.py --check    # exit 1 if stale; use in CI
```

Never hand-edit it.

## Git workflow

Branch per topic (`feat/`, `fix/`, `docs/`, `chore/`), small commits with
descriptive messages, no force-push. Review `git diff` before committing.
Behaviour changes to tools, routing, memory, the email approval gate or the
launch allowlist get agreed before implementation — see
[CONTRIBUTING.md](../CONTRIBUTING.md).

Dated phase records live in `docs/history/` and are never deleted; when a
document is superseded, move it there and cross-link it rather than replacing
it.