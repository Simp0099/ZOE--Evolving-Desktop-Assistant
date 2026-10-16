/**
 * Tests for the UI's API client.
 *
 * The backend is replaced by a stub `fetch`, so these check the transport
 * contract the UI depends on -- payload shape, cancellation, timeouts and error
 * wording -- with no server running.
 *
 * Run: node --test src/
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { BackendError, sendChat, transcribe, checkHealth, resetConversation } from "./api.js";

/** Install a stub fetch for one test. Returns the calls it recorded. */
function stubFetch(response) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return response(url, options);
  };
  return calls;
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => body,
    blob: async () => new Blob([JSON.stringify(body)]),
  };
}

function failResponse(status, detail) {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
    blob: async () => new Blob(),
  };
}

// --- Chat ----------------------------------------------------------------

test("chat sends the real conversation id, so separate chats keep separate context", async () => {
  const calls = stubFetch(() => jsonResponse({ response: "hi", conversation_id: "c-1" }));
  await sendChat({ message: "hello", conversationId: "c-1", spoken: true, signal: undefined });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.message, "hello");
  assert.equal(body.conversation_id, "c-1", "conversation id must be sent, not invented server-side");
  assert.equal(body.spoken, true);
  assert.ok(calls[0].url.endsWith("/chat"), `expected /chat, got ${calls[0].url}`);
});

test("chat returns the assistant's actual reply", async () => {
  stubFetch(() => jsonResponse({ response: "The real answer", conversation_id: "c-1" }));
  const data = await sendChat({ message: "q", conversationId: "c-1" });
  assert.equal(data.response, "The real answer");
});

// --- Error handling ------------------------------------------------------

test("a provider failure surfaces the backend's own message", async () => {
  stubFetch(() => failResponse(503, "The AI provider's usage limit has been reached."));
  await assert.rejects(
    () => sendChat({ message: "q", conversationId: "c-1" }),
    (error) => {
      assert.ok(error instanceof BackendError);
      assert.equal(error.status, 503);
      assert.equal(error.kind, "provider");
      assert.match(error.message, /usage limit/);
      return true;
    },
  );
});

test("an unreachable backend is reported as offline, not as an assistant reply", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await assert.rejects(
    () => sendChat({ message: "q", conversationId: "c-1" }),
    (error) => {
      assert.equal(error.kind, "offline");
      assert.match(error.message, /cannot connect/i);
      return true;
    },
  );
});

test("a cancelled request is reported as cancelled, not as a failure", async () => {
  // This distinction matters: the UI must not show "try again" for a turn the
  // user deliberately stopped.
  const controller = new AbortController();
  globalThis.fetch = async (_url, options) =>
    new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () =>
        reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    });

  const pending = sendChat({ message: "q", conversationId: "c-1", signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, (error) => {
    assert.equal(error.kind, "cancelled");
    assert.equal(error.message, "Stopped.");
    return true;
  });
});

test("a timeout stops the request instead of hanging forever", async () => {
  globalThis.fetch = async (_url, options) =>
    new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () =>
        reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    });
  await assert.rejects(
    () => checkHealth(undefined),
    (error) => {
      assert.ok(["timeout", "offline"].includes(error.kind));
      return true;
    },
  );
});

// --- Voice payloads ------------------------------------------------------

test("transcribe uploads audio as multipart, not JSON", async () => {
  const calls = stubFetch(() => jsonResponse({ text: "hello", engine: "faster-whisper" }));
  const result = await transcribe(new Blob(["audio"], { type: "audio/webm" }), undefined);

  assert.equal(calls[0].options.body instanceof FormData, true);
  assert.equal(calls[0].options.headers, undefined, "multipart must not carry a JSON content-type");
  assert.equal(result.text, "hello");
});

test("reset posts the conversation id being cleared", async () => {
  const calls = stubFetch(() => jsonResponse({ status: "reset" }));
  await resetConversation("c-42");
  assert.equal(JSON.parse(calls[0].options.body).conversation_id, "c-42");
});