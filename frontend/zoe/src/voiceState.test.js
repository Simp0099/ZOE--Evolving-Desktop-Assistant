/**
 * Tests for the voice state machine.
 *
 * Plain Node, no framework: these are pure functions, so the standard test
 * runner is enough and the UI keeps its dependency list unchanged.
 *
 * Run: node --test src/
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  VoiceState, canTransition, transition, voiceAction, isActive, holdsAudioResources, VOICE_LABELS,
} from "./voiceState.js";

// --- Transitions ---------------------------------------------------------

test("the happy path is legal: idle -> listening -> transcribing -> speaking", () => {
  let state = VoiceState.IDLE;
  state = transition(state, VoiceState.REQUESTING_PERMISSION);
  state = transition(state, VoiceState.LISTENING);
  state = transition(state, VoiceState.TRANSCRIBING);
  state = transition(state, VoiceState.PROCESSING);
  state = transition(state, VoiceState.SPEAKING);
  assert.equal(state, VoiceState.SPEAKING);
});

test("every state can reach idle, so the UI is never stuck", () => {
  for (const state of Object.values(VoiceState)) {
    assert.equal(transition(state, VoiceState.IDLE), VoiceState.IDLE, `${state} cannot reach idle`);
  }
});

test("every state can reach error, so a failure is always reportable", () => {
  for (const state of Object.values(VoiceState)) {
    assert.equal(transition(state, VoiceState.ERROR), VoiceState.ERROR, `${state} cannot reach error`);
  }
});

test("an illegal transition is refused instead of corrupting the machine", () => {
  // You cannot start speaking without the model having produced something.
  assert.equal(transition(VoiceState.IDLE, VoiceState.SPEAKING), VoiceState.IDLE);
  // You cannot be listening and speaking at once.
  assert.equal(transition(VoiceState.LISTENING, VoiceState.SPEAKING), VoiceState.LISTENING);
  // You cannot transcribe audio you never recorded.
  assert.equal(transition(VoiceState.IDLE, VoiceState.TRANSCRIBING), VoiceState.IDLE);
});

test("transitioning to the current state is a no-op, not a failure", () => {
  assert.equal(transition(VoiceState.LISTENING, VoiceState.LISTENING), VoiceState.LISTENING);
  assert.ok(canTransition(VoiceState.PROCESSING, VoiceState.PROCESSING));
});

test("error recovers into a fresh listening attempt", () => {
  assert.equal(transition(VoiceState.ERROR, VoiceState.IDLE), VoiceState.IDLE);
  assert.equal(
    transition(transition(VoiceState.ERROR, VoiceState.IDLE), VoiceState.REQUESTING_PERMISSION),
    VoiceState.REQUESTING_PERMISSION,
  );
});

// --- Button behaviour ----------------------------------------------------

test("the mic button starts, then stops recording, never two at once", () => {
  assert.equal(voiceAction(VoiceState.IDLE), "start");
  assert.equal(voiceAction(VoiceState.ERROR), "start");
  assert.equal(voiceAction(VoiceState.REQUESTING_PERMISSION), "start");
  assert.equal(voiceAction(VoiceState.LISTENING), "stop-recording");
});

test("the button stops playback while speaking", () => {
  assert.equal(voiceAction(VoiceState.SPEAKING), "stop-playback");
});

test("the button does nothing while the backend is still working", () => {
  // Transcribing and processing have no local resource to stop, so claiming
  // otherwise would show a Stop button that lies.
  assert.equal(voiceAction(VoiceState.TRANSCRIBING), "none");
  assert.equal(voiceAction(VoiceState.PROCESSING), "none");
});

// --- Resource tracking ---------------------------------------------------

test("only real recording states are reported as holding audio resources", () => {
  assert.ok(holdsAudioResources(VoiceState.LISTENING));
  assert.ok(holdsAudioResources(VoiceState.REQUESTING_PERMISSION));
  // Idle must not claim a microphone, or cleanup would look unnecessary.
  assert.equal(holdsAudioResources(VoiceState.IDLE), false);
  assert.equal(holdsAudioResources(VoiceState.PROCESSING), false);
  assert.equal(holdsAudioResources(VoiceState.SPEAKING), false);
});

test("only genuinely active states animate the UI", () => {
  assert.equal(isActive(VoiceState.IDLE), false);
  assert.equal(isActive(VoiceState.ERROR), false);
  assert.equal(isActive(VoiceState.LISTENING), true);
  assert.equal(isActive(VoiceState.SPEAKING), true);
});

test("every state has a label, so the button is never blank", () => {
  for (const state of Object.values(VoiceState)) {
    assert.ok(VOICE_LABELS[state], `no label for ${state}`);
  }
});