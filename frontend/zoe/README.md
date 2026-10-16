# Zoe — Personal AI Assistant UI

This folder is a UI-only redesign of the existing React + Vite frontend. It keeps the existing FastAPI chat endpoint at `http://127.0.0.1:8001/chat` and leaves the teammate-owned `jarvis/`, IoT, and backend code outside this folder untouched.

## Run in the browser

1. Make a backup of your current `frontend` folder.
2. Extract this `Zoe-UI` folder.
3. Replace the contents of your current `frontend` folder with the contents of `Zoe-UI` (do not copy the outer folder inside `frontend`).
4. Open a terminal in `frontend` and run:
   ```bash
   npm install
   npm run dev
   ```
5. Keep your FastAPI backend running separately on port `8001`, as before.

## Desktop window

One command starts Vite, waits for it, opens the Electron window, and starts the backend if it is not already running:

```bash
npm run dev:desktop
```

`npm run dev` and `npm run desktop` still work separately, in two terminals, if you want to run the backend yourself. The main process checks `http://127.0.0.1:8001/health` before it creates a window and only stops a backend it started itself.

The window controls use a small Electron preload bridge in `electron-preload.cjs`, which exposes exactly `minimize`, `maximize` and `close`.

## What's included

- Rose-pink glassmorphism theme, responsive sidebar, conversation search and new-chat control.
- Local browser persistence for multiple UI conversations.
- Live clock and time-aware greeting.
- Animated CSS globe and five quick-action cards.
- Chat composer, Enter-to-send / Shift+Enter-newline, voice input and existing browser speech synthesis behavior.
- Settings dialog and conversation deletion.
- Centralized assistant identity in `src/config/assistant.json` and CSS color tokens.

## Important behavior notes

- The UI still calls the same backend endpoint (`127.0.0.1:8001/chat`).
- The composer stays usable when the backend is offline: you can always type, a notice names the command that starts it, and sending reports the real error instead of failing silently.
- Conversation lists are saved in this browser's `localStorage`. The existing backend is shared across UI sessions, so this front-end session list does **not** automatically create separate backend/LLM memory contexts.
- Web Search and Deep Think are shown as “soon” indicators because this frontend/backend contract does not currently expose those separate capabilities; they are not presented as working buttons.
- The attachment icon is intentionally non-functional and labeled as unavailable.
- No `node_modules` folder is included; `npm install` recreates dependencies on your machine.
