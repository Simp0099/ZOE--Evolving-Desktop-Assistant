# Zoe

A personal AI desktop assistant: voice and text interaction, wake-word
activation, multimodal perception, memory, a model-driven tool registry, and an
optional [Electron desktop UI](docs/FRONTEND.md). The Python backend runs as a
single process — `python -m zoe` **is** the assistant. The desktop UI is a
presentation layer over the same brain, reached over a localhost HTTP bridge.

Repository: <https://github.com/Simp0099/Personal-Ai-Partner>

## Status

**Working prototype. Not production-ready.** The safety-relevant components are
the best tested; the experience-relevant ones (voice latency, echo handling) are
the least measured. See [docs/STATUS.md](docs/STATUS.md) for the full acceptance
table and [docs/VOICE_LATENCY.md](docs/VOICE_LATENCY.md) for the measured
figures with their exclusions.

Verified on macOS (Python 3.12, Node 26). Other platforms are untested; the
voice path needs macOS microphone permission and the launch allowlist in
`config.yaml` only has real commands for the OS it was written on.

## Two ways to run it

| | Terminal (core) | Electron UI |
| --- | --- | --- |
| Start | `python -m zoe --text` | `npm run dev` + `npm run desktop` in `frontend/zoe/`, plus a running backend |
| Input | microphone + keyboard | microphone (MediaRecorder) + keyboard |
| Speech out | direct to the speaker | WAV streamed from the backend and played in the renderer |
| Transport | in-process | HTTP to `127.0.0.1:8001` |
| Needs | Python deps | the above, plus `npm install` |

The terminal path is the product. The UI is a client of it and adds nothing to
the brain — see [docs/FRONTEND.md](docs/FRONTEND.md) for what it does not yet
do.

## Feature status

Labels: **TESTED** — implemented and covered by an offline test you can run.
**LIVE** — depends on a real provider or device; not covered offline.
**PARTIAL** — works, with a named limitation.
**PROPOSED** — designed or discussed, not built.

| Capability | Status | Evidence / limitation |
| --- | --- | --- |
| Offline test suite | TESTED | `python -m pytest -q` — as of 2026-10-15: 1542 passed, 6 failed, 11 xfailed, 1 skipped |
| Local intent bypass | TESTED | Time, weather, dictionary, screenshot, app launch, media run with zero model calls |
| Model routing and fallback | TESTED | Ranked selection, capability gating, health cooldowns, honest failure |
| Memory (SQLite facts + capped history) | TESTED | Relevance recall and subject index; recall quality is **not** benchmarked |
| Application launching | TESTED | Default-deny allowlist, argument lists only, never a shell |
| Email draft-then-confirm | TESTED | `send_email` cannot send without a strict affirmative within `EMAIL_CONFIRM_TIMEOUT_S` (120 s default) |
| Plugin loader | TESTED | Validation, name-collision rejection, timeout, crash containment |
| Startup self-check | TESTED | `python -m zoe --status` |
| UI voice state machine | TESTED | `npm test` — 20/20 (`node --test`) |
| Frontend build | TESTED | `npm run build` succeeds; a successful build is **not** an end-to-end result |
| Echo cancellation | PARTIAL | Single-tap canceller. The suite's own xfail records 0 % echo-energy removal in a 200 ms RT60 room |
| Barge-in, wake word, ASR | LIVE | No automated proxy for real audio |
| Speech output (Kokoro) | LIVE | Synthesis is measured; intelligibility has not been confirmed by ear |
| Time to first audio | LIVE | Measured **excluding** the provider round trip; no target is claimed met |
| Provider calls (OpenRouter, OpenCode Zen) | LIVE | Catalog-verified only. Gemini is the verified provider |
| Webcam perception, NASA/news/Wikipedia tools | LIVE | Each needs a device or a network account |
| Self-modification, Telegram, WhatsApp, full-duplex dialogue | PROPOSED | Directions only. A paper covering these directions was written but is not part of this repository |

## Requirements

- Python 3.10+ (developed and tested on 3.12)
- Node.js 18+ (tested on 26) and npm, for the UI only
- macOS microphone permission for the voice path

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[server]"      # the UI bridge: FastAPI, uvicorn, PyAV
pip install -e ".[reader]"      # optional: PDF/DOCX/XLSX reading
cp .env.example .env
```

Put at least one **enabled** provider key in `.env`. Full instructions, optional
dependencies and macOS permissions: [docs/SETUP.md](docs/SETUP.md).

> `pip install -e ".[server]"` also installs **PyAV**, which `server.py` needs to
> decode browser recordings. It is not in `requirements.txt`, so a plain
> `pip install -r requirements.txt` leaves `/transcribe` broken.

## Run

```bash
python -m zoe --status   # print configuration and exit
python -m zoe --text     # terminal, text only, no microphone
python -m zoe            # terminal, voice
```

With the UI:

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8001   # terminal 1
cd frontend/zoe && npm install                               # terminal 2
npm run dev                                                  # Vite on :5173
npm run desktop                                              # Electron window
```

Three processes, started by hand. `npm run desktop` currently loads the Vite dev
server URL unconditionally and does not start the backend — see
[docs/FRONTEND.md](docs/FRONTEND.md).

## Configuration

`config.yaml` holds behaviour and contains no secrets; it is documented key by
key in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) (**generated** — run
`python scripts/gen_config_reference.py --check` after editing `config.yaml`).
Secrets live only in `.env`, named in `.env.example`.

## Tests

```bash
python -m pytest -q                       # backend, offline, no network
python -m zoe.main --test                 # in-repo pipeline probe
cd frontend/zoe && npm test && npm run lint
```

Details, including which tests need hardware or credentials:
[docs/TESTING.md](docs/TESTING.md).

## Security model

- **The model cannot send email.** The tool it can call only stages a draft;
  the send function is unreachable from the model's registry and is called only
  after the user's own next turn parses as a strict affirmative within the
  timeout window.
- **Tools are default-deny.** Application launch reads an allowlist map and
  uses argument lists, never a shell. File reads resolve symlinks and are
  checked against `file_reader.roots`, then opened with `O_NOFOLLOW`.
- **Untrusted content stays data.** Documents are returned inside an untrusted
  content fence so their text cannot authorise an action.
- **Plugins are not a sandbox.** They are validated, timeout-bounded and
  crash-contained, but they run in the assistant's own process and are trusted
  to do what they declare. Treat `plugins/*.py` as source code you would run.
- **Known gaps**: indirect prompt injection through web/email/document content
  is only mitigated by the approval gate, not solved; there is no retention or
  deletion policy for memory, screenshots or webcam frames; there is no
  retention policy at all.

Full policy and how to report an issue: [SECURITY.md](SECURITY.md).

## Known limitations

- Reverberant rooms break echo cancellation; barge-in self-interrupts there.
- Latency figures exclude the provider round trip.
- Memory recall is lexical, so paraphrased questions ("who is my sister" against
  a stored fact naming a relation) are known to be weak; there is no recall
  detection, so this class of question is answered only if a stored fact happens
  to share words with the question.
- No retention or deletion policy for `data/`.
- The Electron main process does not yet launch the backend, and does not guard
  `window.open` or navigation.
- MIT licensed. One plugin is adapted from CC BY-NC 4.0 material, which
  constrains commercial use — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Troubleshooting

[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) covers the known failures.
Never paste `.env` or an API key into an issue or a log.

## Documentation

Full index: [docs/INDEX.md](docs/INDEX.md)

| Document | Covers |
| --- | --- |
| [docs/SETUP.md](docs/SETUP.md) | Install, optional deps, macOS permissions, backend and UI launch |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Request paths, state machines, the UI bridge |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every `config.yaml` key and `CONFIG` read |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Layout, workflow, adding tools and plugins |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Electron architecture and the backend bridge |
| [docs/TESTING.md](docs/TESTING.md) | Suites, discovery, what needs hardware |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Known failures and fixes |
| [docs/STATUS.md](docs/STATUS.md) | Acceptance table with evidence |
| [docs/PLUGINS.md](docs/PLUGINS.md) | The plugin contract |
| [docs/VOICE_LATENCY.md](docs/VOICE_LATENCY.md) | Measured latency, with method and exclusions |
| [docs/PLUGIN_AUDIT.md](docs/PLUGIN_AUDIT.md) | How the plugin loader compares with the upstream one it was adapted from |
| [docs/wake_phrases.md](docs/wake_phrases.md) | Which wake phrases are verified, and why the rest are disabled |

`docs/archive/` holds dated phase reports. They are the record of what was
decided, not of what is there — do not follow them for current behaviour.

## Extending

Copy `plugins/_template.py`, edit `TOOL` and `run()`, drop it in `plugins/`:

```python
TOOL = {
    "name": "word_count",
    "description": "Count the words in a piece of text.",
    "parameters": {"text": "str"},   # types: str, int, float, bool
}

def run(text: str) -> str:
    return f"{len(text.split())} words"
```

Contract and validation rules: [docs/PLUGINS.md](docs/PLUGINS.md).

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md). Short version: branch, run the suite, do not
commit secrets, document what you changed.

## Licence

**No licence file is present, so the repository is All Rights Reserved by
default.** Before publishing or reusing this code, settle the licence. In
particular `plugins/file_reader.py` is adapted from material the project paper
attributes to Mark LV under CC BY-NC 4.0, which is non-commercial — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Acknowledgements

- `plugins/file_reader.py` adapts the tool half of Mark LV's
  `actions/file_processor.py`; the model call and write actions were not
  ported. Attribution is owed and licence review is pending.
- Third-party packages are credited by their own licences in `requirements.txt`
  and `frontend/zoe/package.json`.