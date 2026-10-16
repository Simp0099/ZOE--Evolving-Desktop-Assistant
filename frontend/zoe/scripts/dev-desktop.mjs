/**
 * One-command desktop launch: Vite and Electron side by side.
 *
 * A ~50-line launcher rather than a `concurrently` dependency: it needs no new
 * package, no lockfile churn and no shell quoting per platform, and it gives
 * exactly the behaviour that matters here -- Vite first, Electron once Vite
 * answers on :5173, and either one exiting takes the other down so no orphan
 * is left behind.
 *
 * Usage: node scripts/dev-desktop.mjs
 */

import { spawn } from "node:child_process";
import { createConnection } from "node:net";

/** The dev server, and the loopback names it may be bound to. */
const DEV_SERVER_PORT = 5173;
//: Vite binds `localhost`, which resolves to ::1 on many machines and 127.0.0.1
//: on others. Probing one address is how you get a 30-second wait followed by
//: "nothing is serving :5173" on a server that is plainly up.
const DEV_SERVER_HOSTS = ["localhost", "127.0.0.1", "::1"];
const READY_TIMEOUT_MS = 30000;
const PROBE_INTERVAL_MS = 300;

/** The npm executable for this platform, so `npm run dev` behaves identically. */
const NPM = process.platform === "win32" ? "npm.cmd" : "npm";

const children = new Set();
let shuttingDown = false;

function stopAll(signal = "SIGTERM") {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) child.kill(signal);
}

process.on("SIGINT", () => stopAll("SIGINT"));
process.on("SIGTERM", () => stopAll("SIGTERM"));
process.on("exit", () => stopAll());

/** Start a child, remember it, and stop everything when it exits on its own. */
function run(command, args, label) {
  const child = spawn(command, args, { stdio: "inherit" });
  children.add(child);
  child.on("exit", (code, signal) => {
    children.delete(child);
    console.log(`[dev:desktop] ${label} exited (code ${code}, signal ${signal || "none"})`);
    stopAll();
    process.exitCode = code ?? 0;
  });
  child.on("error", (error) => {
    console.error(`[dev:desktop] could not start ${label}: ${error.message}`);
    stopAll();
    process.exit(1);
  });
  return child;
}

/** TCP connect: "something is listening on this address" is what we mean. */
function connects(host) {
  return new Promise((resolve) => {
    const socket = createConnection({ host, port: DEV_SERVER_PORT });
    socket.setTimeout(1000);
    const finish = (answer) => {
      socket.destroy();
      resolve(answer);
    };
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

async function isListening() {
  for (const host of DEV_SERVER_HOSTS) {
    if (await connects(host)) return true;
  }
  return false;
}

async function waitForVite() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await isListening()) return true;
    await new Promise((resolve) => setTimeout(resolve, PROBE_INTERVAL_MS));
  }
  return false;
}

run(NPM, ["run", "dev"], "vite");

if (!(await waitForVite())) {
  console.error(`[dev:desktop] nothing is serving :${DEV_SERVER_PORT} after ${READY_TIMEOUT_MS / 1000}s.`);
  stopAll();
  process.exit(1);
}

console.log(`[dev:desktop] vite is up on http://localhost:${DEV_SERVER_PORT}; starting Electron`);
run(NPM, ["run", "desktop"], "electron");