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

There are **three** processes, and Electron does not start any of them:

1. **Backend** — `python -m uvicorn server:app --host 127.0.0.1 --port 8001`
2. **Renderer** — the Vite dev server on `http://localhost:5173`
3. **Electron main** — loads that URL in a `BrowserWindow`

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8001   # terminal 1
cd frontend/zoe
npm install
npm run dev                                                  # terminal 2
npm run desktop                                              # terminal 3
```

## npm scripts

| Script | Does |
| --- | --- |
| `npm run dev` | Vite dev server on :5173 |
| `npm run build` | Production bundle into `dist/` (git-ignored) |
| `npm run preview` | Serve the built bundle |
| `npm run desktop` | Launch Electron against :5173 |
| `npm run lint` | oxlint |
| `npm test` | `node --test src/` |

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

1. **The main process does not launch the backend.** A failed or missing
   backend shows the UI's offline state, but nothing starts `uvicorn` for you.
2. **`electron.cjs` loads the dev-server URL unconditionally.** There is no
   `app.isPackaged` branch, so `dist/` is built but never loaded by Electron.
   `npm run build` currently produces an artefact nothing consumes.
3. **No navigation guards.** No `setWindowOpenHandler` and no `will-navigate`
   handler, so a link rendered by the UI could open a window outside those
   controls. The `ipcMain` handlers also do not verify `event.senderFrame`.
4. **Shutdown is unverified.** The backend is not stopped by Electron; it is an
   independent process you started by hand.
5. **Development and production differ only in that production does not exist
   yet.**

## Troubleshooting

**Blank window.** The Vite dev server is not running on :5173. Run
`npm run dev` before `npm run desktop`.

**"Zoe cannot connect to the assistant."** The backend is not up. Check
`curl http://127.0.0.1:8001/health`. If nothing answers, start uvicorn. If it
answers but the UI still fails, you are probably hitting a CORS origin that is
not in `ALLOWED_ORIGINS` in `server.py` — add the port there rather than
widening it to a wildcard, because this API can read files and launch
applications.

**Mic button does nothing.** The renderer needs a secure context for
`getUserMedia`. `localhost` qualifies; a LAN IP over plain HTTP does not. Grant
macOS microphone permission to Electron.

**Transcription always fails.** `server.py` decodes uploads with **PyAV**
(`import av`). It is in the `[server]` extra but not in `requirements.txt`, so a
plain install leaves `/transcribe` broken with a 503.

**`npm test` finds no tests.** It runs `node --test src/`, which only picks up
tests inside `src/`.