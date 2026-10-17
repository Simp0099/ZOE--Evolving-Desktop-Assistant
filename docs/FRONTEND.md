# Frontend — the Electron UI

The desktop UI is a **client of the backend brain**. It adds no assistant
behaviour of its own: `server.py` translates HTTP into `ZoeBrain.ask()` and back,
and every provider, tool, memory and safety decision stays in Python. The
terminal (`python -m zoe`) and this UI are two front ends on the same core.

## Stack

| | |
| --- | --- |
| Framework | React 19 + Vite 8 |
| Shell | Electron 44 (`electron.cjs`, CommonJS) |
| Bridge | `electron-preload.cjs` (`contextBridge`) |
| Tests | `node --test`, no test framework |
| Lint | oxlint |
| Path | `frontend/zoe/` |

## Processes

There are **three** processes. Two are started by `npm run dev:desktop`; the
third is started by the Electron main process when it needs to be.

1. **Backend** — `python -m uvicorn server:app --host 127.0.0.1 --port 8001`.
   Electron checks `GET /health` with a 1.5 s timeout before it creates a
   window. If nothing answers, it spawns uvicorn itself with `cwd` at the
   project root (derived from `__dirname`, never hard-coded), preferring
   `.venv/bin/python` (`.venv\Scripts\python.exe` on Windows) and falling back to
   `python3` / `python`. It then polls `/health` for up to 30 s.
2. **Renderer** — the Vite dev server on `http://localhost:5173`.
3. **Electron main** — loads that URL in a `BrowserWindow`, but only after the
   dev server actually answers, so there is no blank-window race.

```bash
cd frontend/zoe
npm install
npm run dev:desktop     # vite, then electron once :5173 answers
```

### Backend lifecycle

- **A backend that was already running is used as-is and never killed.** Only a
  child process Electron spawned is killed, on `before-quit`.
- If `/health` never answers within 30 s, Electron logs a clear error to the
  terminal and **still opens the window**: seeing the honest offline state is
  more useful than no window, and the UI stays fully usable for typing.
- If the spawn fails outright (`ENOENT`, no interpreter), the failure and the
  venv setup command are printed instead.

### Offline is a state, not a dead end

The composer is never disabled because the backend is down — only an in-flight
request disables it. Instead:

- a single inline notice above the composer states that the backend is offline
  and gives the command to start it;
- the send button stays clickable and reports the real error from `api.js`,
  rather than silently doing nothing;
- the mic button is disabled only when `/health` **definitively** says
  `voice_input_available: false`. "Not checked yet" is not a reason to take the
  microphone away; clicking it offline gives a clear error.

The Settings modal shows **Checking…** during the first health check, then the
real `Connected` / `Disconnected`. It previously showed "Disconnected" from the
initial `false` state, which was a claim about the backend before anything had
been asked.

## npm scripts

| Script | Does |
| --- | --- |
| `npm run dev` | Vite dev server on :5173 |
| `npm run dev:desktop` | Vite + Electron + backend, one command |
| `npm run build` | Production bundle into `dist/` (git-ignored) |
| `npm run preview` | Serve the built bundle |
| `npm run desktop` | Launch Electron against :5173 (starts the backend if needed) |
| `npm run lint` | oxlint |
| `npm test` | `node --test "src/**/*.test.js"` |

`dev:desktop` runs `scripts/dev-desktop.mjs`, a ~50-line launcher, rather than
adding `concurrently`: it needs no new dependency and no lockfile churn, and it
gives exactly the two behaviours that matter here — Electron starts only once
Vite answers on :5173, and either process exiting takes the other down, so no
orphan is left behind.

## Renderer responsibilities

- Chat UI: conversation list, history, search, quick actions, activity log.
- Voice: `MediaRecorder` captures WebM/Opus; the blob is posted to
  `/transcribe`; the reply is fetched from `/chat` and synthesised by `/tts`,
  then played with an `AudioContext` element.
- Microphone level metering for the globe animation (`useAudioLevel`).

## Voice state machine

`src/voiceState.js` holds the interaction states as data rather than scattered
booleans, so the UI cannot show a "listening" animation that is untrue, cannot
open the microphone twice, and cannot get stuck loading after a failure.

```
idle → requesting_permission → listening → transcribing → processing → speaking → idle
                                                                 ↕
                                                              error
```

Every move goes through `canTransition` / `transition`. An illegal transition
returns the current state instead of corrupting the machine; `error` is
reachable from everywhere, so there is always a way back. `voiceAction(state)`
returns `start`, `stop-recording`, `stop-playback` or `none`, so one button
cannot do two conflicting things.

This is what the 20 frontend tests cover — `voiceState.test.js` (12) and
`api.test.js` (8).

## Backend communication

`src/api.js` is the **only** module that talks to the backend. Timeouts,
cancellation and error wording are decided once, there.

- Base URL: `http://127.0.0.1:8001`, overridable with `VITE_ZOE_API`.
- Timeouts: chat 120 s, transcribe 60 s, tts 60 s, health 5 s.
- A user-initiated Stop aborts the same request; the UI distinguishes
  "Stopped." from "took too long".
- Errors are `BackendError` with a `kind`: `offline`, `provider`,
  `rate_limit`, `too_large`, `timeout`, `cancelled`, `request`.

| Endpoint | Used for |
| --- | --- |
| `GET /health` | Readiness and real capability flags; polled on load |
| `POST /chat` | One text turn; `conversation_id` keeps chat context aligned with the backend |
| `POST /transcribe` | Recorded audio → text (local faster-whisper via PyAV decode) |
| `POST /tts` | Text → streamed WAV |
| `POST /conversation/reset` | Clears one conversation's context; long-term memory untouched |

**No secret ever reaches the renderer.** Only `VITE_`-prefixed Vite variables
are exposed, and the only one used is a localhost URL.

## The preload bridge

`electron-preload.cjs` exposes exactly three functions under
`window.electronWindow`: `minimize`, `maximize`, `close`. Nothing else. No
filesystem, no shell, no arbitrary IPC.

## Known integration gaps

These are real and not yet fixed. Do not read the app as production-ready
because `npm run build` succeeds.

1. **`electron.cjs` loads the dev-server URL unconditionally.** There is no
   `app.isPackaged` branch, so `dist/` is built but never loaded by Electron.
   `npm run build` currently produces an artefact nothing consumes. (The URL is
   now *waited for* rather than loaded blind, but it is still the dev server.)
2. **No navigation guards.** No `setWindowOpenHandler` and no `will-navigate`
   handler, so a link rendered by the UI could open a window outside those
   controls. The `ipcMain` handlers also do not verify `event.senderFrame`.
3. **Shutdown is only partially verified.** A backend Electron spawned is killed
   on `before-quit`, but the kill is not awaited and the window is not closed
   first, so a hung uvicorn can outlive Electron by a moment.
4. **The backend start is unverified on Linux and Windows.** The interpreter
   lookup covers all three, but only the macOS path has been run.
5. **Development and production differ only in that production does not exist
   yet.**

## Troubleshooting

**Blank window.** The Vite dev server is not running on :5173. Run
`npm run dev` before `npm run desktop`, or just `npm run dev:desktop`. If the
server never comes up, the window shows a short page saying exactly that,
rather than a blank page.

**"Zoe cannot connect to the assistant."** The backend is not up. Check
`curl http://127.0.0.1:8001/health`. If nothing answers, start uvicorn — or note
that `npm run dev:desktop` starts it for you, and prints the error to the
terminal if it could not. If `/health` answers but the UI still fails, you are
probably hitting a CORS origin that is not in `ALLOWED_ORIGINS` in `server.py` —
add the port there rather than widening it to a wildcard, because this API can
read files and launch applications.

**Mic button does nothing.** The renderer needs a secure context for
`getUserMedia`. `localhost` qualifies; a LAN IP over plain HTTP does not. Grant
macOS microphone permission to Electron.

**Transcription always fails.** `server.py` decodes uploads with **PyAV**
(`import av`), now pinned in both `requirements.txt` and the `[server]` extra.
An install predating that change still needs `pip install "av>=12.0.0"`.