/**
 * The only place the UI talks to the backend.
 *
 * Every request goes through here so that timeouts, cancellation and error
 * wording are decided once. Nothing in this file invents an assistant reply:
 * a failed call throws an Error carrying the backend's own message, and the
 * caller decides how to show it.
 */

const DEFAULT_BASE = "http://127.0.0.1:8001";

// Vite only exposes VITE_-prefixed vars, so a secret can never land here by
// accident: this is a localhost URL, not a credential.
export const API_BASE = (import.meta.env?.VITE_ZOE_API || DEFAULT_BASE).replace(/\/$/, "");

/** Model turns are slow; voice and health are not. Kept per-call so the
 *  UI never sits in a loading state forever. */
const TIMEOUTS = { chat: 120000, transcribe: 60000, tts: 60000, health: 5000 };

export class BackendError extends Error {
  constructor(message, { status = 0, kind = "unknown" } = {}) {
    super(message);
    this.name = "BackendError";
    this.status = status;
    this.kind = kind;
  }
}

/** Turn a status code into something a person can act on. */
function kindFor(status) {
  if (status === 0) return "offline";
  if (status === 503) return "provider";
  if (status === 429) return "rate_limit";
  if (status === 413) return "too_large";
  return "request";
}

async function request(path, { method = "GET", body, timeout, signal } = {}) {
  const controller = new AbortController();
  const limit = timeout ?? TIMEOUTS[path.slice(1)] ?? 30000;
  const timer = setTimeout(() => controller.abort(new Error("timeout")), limit);

  // Caller-supplied signal (a Stop button) cancels the same request.
  const onAbort = () => controller.abort(signal?.reason);
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener("abort", onAbort, { once: true });
  }

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body instanceof FormData ? undefined : { "Content-Type": "application/json" },
      body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });

    if (!response.ok) {
      // The backend already speaks user-safe words; pass them through.
      const detail = await response.json().catch(() => ({}));
      throw new BackendError(
        detail?.detail || `The assistant returned HTTP ${response.status}.`,
        { status: response.status, kind: kindFor(response.status) },
      );
    }
    return response;
  } catch (error) {
    if (error instanceof BackendError) throw error;
    if (error?.name === "AbortError") {
      // Distinguish "we gave up" from "the user pressed stop": they are not
      // the same event and the UI must not report a cancelled turn as failed.
      const cancelled = signal?.aborted;
      throw new BackendError(
        cancelled ? "Stopped." : "The assistant took too long to respond. Please try again.",
        { kind: cancelled ? "cancelled" : "timeout" },
      );
    }
    throw new BackendError(
      "Zoe cannot connect to the assistant. Check that the backend is running and try again.",
      { kind: "offline" },
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener?.("abort", onAbort);
  }
}

export async function checkHealth(signal) {
  const response = await request("/health", { signal });
  return response.json();
}

export async function sendChat({ message, conversationId, spoken, signal }) {
  const response = await request("/chat", {
    method: "POST",
    body: { message, conversation_id: conversationId, spoken },
    signal,
  });
  return response.json();
}

export async function transcribe(blob, signal) {
  const form = new FormData();
  form.append("audio", blob, "recording.webm");
  const response = await request("/transcribe", { method: "POST", body: form, signal });
  return response.json();
}

export async function fetchSpeech(text, signal) {
  return request("/tts", { method: "POST", body: { text }, signal });
}

export async function resetConversation(conversationId) {
  await request("/conversation/reset", {
    method: "POST",
    body: { conversation_id: conversationId },
  });
}