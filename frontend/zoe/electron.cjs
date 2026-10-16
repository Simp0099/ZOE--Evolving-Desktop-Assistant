const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");

//: The React app is served by the Vite dev server; `npm run dev` starts it.
//: Vite binds `localhost`, which resolves to ::1 on many machines and to
//: 127.0.0.1 on others, so every address is tried before concluding that the
//: dev server is not running.
const DEV_SERVER_URLS = [
  "http://localhost:5173",
  "http://127.0.0.1:5173",
  "http://[::1]:5173",
];
//: The Python bridge. `server.py` binds 127.0.0.1 explicitly and the renderer's
//: api.js calls that exact address, so one URL is unambiguous here.
const API_URL = "http://127.0.0.1:8001";
//: Project root, derived rather than hard-coded: server.py and config.yaml live
//: three levels above frontend/zoe/.
const PROJECT_ROOT = path.resolve(__dirname, "..", "..");

const HEALTH_PROBE_TIMEOUT_MS = 1500;
const BACKEND_READY_TIMEOUT_MS = 30000;
const DEV_SERVER_TIMEOUT_MS = 30000;

/** The backend this process started. Null means "we did not start one". */
let backendProcess = null;

/**
 * GET a URL and resolve true only on a 2xx, giving up after `timeoutMs`.
 *
 * Deliberately tiny and dependency-free: this is the same readiness check a
 * human would run with curl, and it must work before any window exists.
 */
function probe(url, timeoutMs) {
  return new Promise((resolve) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 300);
    });
    const giveUp = () => {
      request.destroy();
      resolve(false);
    };
    request.on("timeout", giveUp);
    request.on("error", giveUp);
  });
}

/** Poll `urls` until one answers, and return that URL. Null if none do. */
async function waitForAny(urls, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const url of urls) {
      if (await probe(url, HEALTH_PROBE_TIMEOUT_MS)) return url;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return null;
}

/** The interpreter to run the backend with: the project venv if it exists. */
function pythonExecutable() {
  const candidates =
    process.platform === "win32"
      ? [path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")]
      : [path.join(PROJECT_ROOT, ".venv", "bin", "python")];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return process.platform === "win32" ? "python" : "python3";
}

/**
 * Make sure something is serving /health, starting uvicorn if nothing is.
 *
 * A backend that was already running is left alone -- and never killed on
 * quit, because it was not ours. If the spawn never becomes healthy the error
 * is printed and the caller continues anyway: a window showing the honest
 * offline state is more useful than no window at all.
 */
async function ensureBackend() {
  if (await probe(`${API_URL}/health`, HEALTH_PROBE_TIMEOUT_MS)) {
    console.log(`[zoe] backend already running at ${API_URL}`);
    return;
  }

  const executable = pythonExecutable();
  console.log(`[zoe] starting backend: ${executable} -m uvicorn server:app`);

  backendProcess = spawn(executable, ["-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8001"], {
    cwd: PROJECT_ROOT,
    stdio: ["ignore", "inherit", "inherit"],
    env: process.env,
  });

  backendProcess.on("error", (error) => {
    console.error(`[zoe] could not start the backend with "${executable}": ${error.message}`);
    console.error("[zoe] create the virtualenv first: python3.12 -m venv .venv && pip install -r requirements.txt && pip install -e \".[server]\"");
    backendProcess = null;
  });
  backendProcess.on("exit", (code, signal) => {
    if (backendProcess) {
      console.error(`[zoe] backend exited (code ${code}, signal ${signal || "none"})`);
      backendProcess = null;
    }
  });

  if (await waitForAny([`${API_URL}/health`], BACKEND_READY_TIMEOUT_MS)) {
    console.log("[zoe] backend is ready");
    return;
  }

  console.error(`[zoe] the backend did not answer /health within ${BACKEND_READY_TIMEOUT_MS / 1000}s.`);
  console.error("[zoe] opening the window anyway: the UI stays usable, and messages will report the failure.");
}

/** Load the dev server only once it actually answers, so no blank window. */
async function loadRenderer(win) {
  const url = await waitForAny(DEV_SERVER_URLS, DEV_SERVER_TIMEOUT_MS);
  if (url) {
    win.loadURL(url);
    return;
  }
  console.error(`[zoe] no Vite dev server at ${DEV_SERVER_URLS[0]}. Run "npm run dev" (or "npm run dev:desktop") first.`);
  win.loadURL(
    `data:text/html,${encodeURIComponent(
      `<body style="font:14px system-ui;padding:2rem;background:#C98B9B;color:#2b1d20">
        <h1 style="font-size:1.2rem">Vite dev server is not running</h1>
        <p>Nothing is answering on port 5173.</p>
        <pre style="background:#ffffff33;padding:1rem;border-radius:8px">npm run dev        # Vite only
npm run dev:desktop  # Vite + Electron, one command</pre>
      </body>`,
    )}`,
  );
}

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1536,
    height: 1024,
    minWidth: 800,
    minHeight: 600,
    autoHideMenuBar: true,
    backgroundColor: "#C98B9B",
    title: "Zoe — Your Personal AI Assistant",
    titleBarStyle: "hidden",
    titleBarOverlay: false,
    webPreferences: {
      preload: path.join(__dirname, "electron-preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  loadRenderer(mainWindow);
  mainWindow.on("page-title-updated", (event) => event.preventDefault());
}

ipcMain.on("zoe-window-minimize", (event) => BrowserWindow.fromWebContents(event.sender)?.minimize());
ipcMain.on("zoe-window-maximize", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win) win.isMaximized() ? win.unmaximize() : win.maximize();
});
ipcMain.on("zoe-window-close", (event) => BrowserWindow.fromWebContents(event.sender)?.close());

app.whenReady().then(async () => {
  // The backend first: the window polls /health on load, and starting it
  // first means the first poll already succeeds.
  await ensureBackend();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

//: Only ever kills a backend this process spawned. A backend the user started
//: by hand is still theirs after Zoe closes.
app.on("before-quit", () => {
  if (!backendProcess) return;
  const child = backendProcess;
  backendProcess = null;
  child.kill();
  console.log("[zoe] stopped the backend this session started");
});