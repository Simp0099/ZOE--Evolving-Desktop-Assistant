/**
 * Voice interaction states and the only legal moves between them.
 *
 * Kept as data rather than scattered booleans so the UI cannot show a
 * "listening" animation that is not true, cannot double-open the microphone,
 * and cannot get stuck in a loading state after a failure. Every transition the
 * UI performs goes through `canTransition`, and anything invalid returns the
 * current state unchanged instead of corrupting the machine.
 */

export const VoiceState = {
  IDLE: "idle",
  REQUESTING_PERMISSION: "requesting_permission",
  LISTENING: "listening",
  TRANSCRIBING: "transcribing",
  PROCESSING: "processing",
  SPEAKING: "speaking",
  ERROR: "error",
};

/**
 * Allowed transitions.
 *
 * `error` is reachable from anywhere: any stage can fail, and the machine must
 * always have a way back to a usable state. `idle` accepts from everything
 * except itself, which is what lets Stop and cleanup always land somewhere
 * valid.
 */
const TRANSITIONS = {
  [VoiceState.IDLE]: [VoiceState.REQUESTING_PERMISSION, VoiceState.ERROR],
  [VoiceState.REQUESTING_PERMISSION]: [VoiceState.LISTENING, VoiceState.ERROR, VoiceState.IDLE],
  [VoiceState.LISTENING]: [VoiceState.TRANSCRIBING, VoiceState.IDLE, VoiceState.ERROR],
  [VoiceState.TRANSCRIBING]: [VoiceState.PROCESSING, VoiceState.ERROR, VoiceState.IDLE],
  [VoiceState.PROCESSING]: [VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.ERROR],
  [VoiceState.SPEAKING]: [VoiceState.IDLE, VoiceState.ERROR, VoiceState.LISTENING],
  [VoiceState.ERROR]: [VoiceState.IDLE, VoiceState.REQUESTING_PERMISSION],
};

export function canTransition(from, to) {
  if (from === to) return true;
  return (TRANSITIONS[from] || []).includes(to);
}

/** Apply a transition, refusing illegal moves instead of corrupting the machine. */
export function transition(from, to) {
  return canTransition(from, to) ? to : from;
}

export const VOICE_LABELS = {
  [VoiceState.IDLE]: "Voice",
  [VoiceState.REQUESTING_PERMISSION]: "Requesting mic...",
  [VoiceState.LISTENING]: "Listening...",
  [VoiceState.TRANSCRIBING]: "Transcribing...",
  [VoiceState.PROCESSING]: "Thinking...",
  [VoiceState.SPEAKING]: "Speaking...",
  [VoiceState.ERROR]: "Error",
};

/** States in which the microphone or an audio stream is actually open. */
export function holdsAudioResources(state) {
  return state === VoiceState.LISTENING || state === VoiceState.REQUESTING_PERMISSION;
}

/** Whether the globe/UI should show an active animation. */
export function isActive(state) {
  return state !== VoiceState.IDLE && state !== VoiceState.ERROR;
}

/**
 * What the voice button should do when clicked.
 *
 * Returns "start", "stop-recording" or "stop-playback". Recording and
 * playback are separate actions, so a repeated click can never open a second
 * microphone stream or leave an orphaned player behind.
 */
export function voiceAction(state) {
  switch (state) {
    case VoiceState.IDLE:
    case VoiceState.ERROR:
    case VoiceState.REQUESTING_PERMISSION:
      return "start";
    case VoiceState.LISTENING:
      return "stop-recording";
    case VoiceState.SPEAKING:
      return "stop-playback";
    default:
      // Transcribing and processing are backend-bound; there is no local
      // resource for the button to stop.
      return "none";
  }
}