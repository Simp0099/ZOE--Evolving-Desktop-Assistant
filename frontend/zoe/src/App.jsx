import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  Search, Plus, MessageCircleMore, GraduationCap, Code2, Dumbbell, Plane,
  FileText, Gamepad2, CalendarDays, Settings, Send, Mic, Sun, Sparkles,
  Lightbulb, Globe2, Image, MoreHorizontal, Clock3, X, Paperclip, Pin, Minus,
  Maximize2, BrainCircuit, StopCircle, Volume2, VolumeX,
} from "lucide-react";
import "./App.css";
import assistantConfig from "./config/assistant.json";
import useAudioLevel from "./useAudioLevel";
import Globe from "./Globe";
import {
  BackendError, checkHealth, fetchSpeech, resetConversation, sendChat, transcribe,
} from "./api.js";
import {
  VOICE_LABELS, VoiceState, holdsAudioResources, isActive, transition, voiceAction,
} from "./voiceState.js";

const STORAGE_KEY = "zoe-conversations-v1";
const ACTIVITY_KEY = "zoe-activity-log";

const initialConversation = () => ({
  id: newConversationId(),
  title: "New chat",
  updatedAt: new Date().toISOString(),
  messages: [],
});

/**
 * A fresh conversation id.
 *
 * The characters are restricted to what the backend accepts for a
 * conversation id (letters, digits, and . _ : -), because this same id is sent
 * with every turn and the server rejects anything else.
 */
function newConversationId() {
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function readConversations() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    // Only real conversations are restored. An empty list is a legitimate
    // state (a fresh profile), so it is honoured rather than replaced with
    // invented sample history.
    if (Array.isArray(saved)) return saved.length ? saved : [initialConversation()];
  } catch (error) {
    console.warn("Could not load Zoe conversations:", error);
  }
  return [initialConversation()];
}

function relativeTime(dateString) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(dateString).getTime()) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
  if (seconds < 172800) return "Yesterday";
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} days ago`;
  return `${Math.floor(seconds / 604800)} week${seconds >= 1209600 ? "s" : ""} ago`;
}

function getGreeting(date) {
  const hour = date.getHours();
  if (hour < 5) return "Good night!";
  if (hour < 12) return "Good morning!";
  if (hour < 17) return "Good afternoon!";
  if (hour < 21) return "Good evening!";
  return "Good night!";
}

const iconMap = {
  search: Search, plus: Plus, chat: MessageCircleMore, messageMore: MessageCircleMore,
  cap: GraduationCap, code: Code2, dumbbell: Dumbbell, plane: Plane, file: FileText,
  game: Gamepad2, calendar: CalendarDays, settings: Settings, send: Send, mic: Mic,
  sun: Sun, sparkles: Sparkles, help: Lightbulb, globe: Globe2, image: Image,
  more: MoreHorizontal, clock: Clock3, close: X, attach: Paperclip, pin: Pin,
  minimize: Minus, maximize: Maximize2, brain: BrainCircuit, stop: StopCircle,
  volume2: Volume2, volumex: VolumeX,
};

function Icon({ name, size = 22, strokeWidth = 1.75 }) {
  const LucideIcon = iconMap[name] || Sparkles;
  return <LucideIcon size={size} strokeWidth={strokeWidth} aria-hidden="true" />;
}

function App() {
  const [now, setNow] = useState(() => new Date());
  const [conversations, setConversations] = useState(readConversations);
  const [activeId, setActiveId] = useState(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [voiceState, setVoiceState] = useState(VoiceState.IDLE);
  const [voiceOutputEnabled, setVoiceOutputEnabled] = useState(true);
  const [search, setSearch] = useState("");
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [toast, setToast] = useState("");
  const [error, setError] = useState("");
  const [retryMessage, setRetryMessage] = useState("");
  const [devMode, setDevMode] = useState("idle");
  const [openMenuId, setOpenMenuId] = useState(null);
  const [backendReady, setBackendReady] = useState(false);
  const [capabilities, setCapabilities] = useState(null);

  const audio = useAudioLevel();
  const [activityLog, setActivityLog] = useState(() => {
    try { return JSON.parse(localStorage.getItem(ACTIVITY_KEY) || "[]"); } catch { return []; }
  });
  const messageEndRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const playbackRef = useRef(null);
  const requestRef = useRef(null);
  const currentActiveId = activeId || conversations[0]?.id;
  const activeConversation = conversations.find((chat) => chat.id === currentActiveId) || conversations[0];
  const messages = activeConversation?.messages || [];

  // Backend readiness. Polled because the backend may start after the UI, and
  // polled on a short interval because a model call can take seconds.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const ping = async () => {
      try {
        const data = await checkHealth(controller.signal);
        if (!cancelled) {
          setBackendReady(true);
          setCapabilities(data);
        }
      } catch {
        if (!cancelled) setBackendReady(false);
      }
    };
    ping();
    const interval = setInterval(ping, 15000);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(interval);
    };
  }, []);

  // Release every audio resource and cancel in-flight work when the app
  // unmounts, so the microphone is never left held by a dead component.
  useEffect(() => () => {
    requestRef.current?.abort();
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = null;
      recorder.stop();
    }
    mediaRecorderRef.current?.stream?.getTracks().forEach((track) => track.stop());
    playbackRef.current?.pause();
    playbackRef.current = null;
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations)); } catch (error) { console.warn("Could not save Zoe conversations:", error); }
  }, [conversations]);

  useEffect(() => {
    try { localStorage.setItem(ACTIVITY_KEY, JSON.stringify(activityLog.slice(-40))); } catch {}
  }, [activityLog]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, loading]);

  function addActivity(type, text) {
    setActivityLog((previous) => [...previous, {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      type,
      text: String(text),
    }].slice(-40));
  }

  function updateActiveMessages(updater) {
    setConversations((previous) => previous.map((chat) => {
      if (chat.id !== currentActiveId) return chat;
      const nextMessages = typeof updater === "function" ? updater(chat.messages) : updater;
      const firstUser = nextMessages.find((item) => item.role === "user");
      return { ...chat, messages: nextMessages, updatedAt: new Date().toISOString(), title: firstUser ? firstUser.text.slice(0, 34) + (firstUser.text.length > 34 ? "…" : "") : chat.title };
    }));
  }

  function newChat() {
    const chat = initialConversation();
    setConversations((previous) => [chat, ...previous]);
    setActiveId(chat.id);
    setMessage("");
    setError("");
    setMobileSidebarOpen(false);
  }

  /** Start a genuinely fresh conversation on the backend, not just in the UI. */
  async function restartConversation(id) {
    try {
      await resetConversation(id);
    } catch (failure) {
      // The local transcript is cleared either way; a stale backend context is
      // worth reporting but is not worth blocking the user for.
      setToast(`Could not reset the assistant's context: ${failure?.message || "unknown error"}`);
      setTimeout(() => setToast(""), 5000);
    }
  }

  function selectChat(id) {
    setActiveId(id);
    setMessage("");
    setMobileSidebarOpen(false);
  }

  function deleteChat(id) {
    // Deleting a chat clears the backend conversation too, so its context
    // cannot leak into a later chat that reuses the same window.
    restartConversation(id);
    setConversations((previous) => {
      const remaining = previous.filter((chat) => chat.id !== id);
      if (!remaining.length) {
        const fresh = initialConversation();
        setActiveId(fresh.id);
        return [fresh];
      }
      if (currentActiveId === id) setActiveId(remaining[0].id);
      return remaining;
    });
  }

  // Voice recording. Every stage releases the microphone before moving on, so
  // a failed turn can never leave the input device held open.
  const startRecording = useCallback(async () => {
    // Refuse a second microphone: a repeated click must not open two streams.
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") return;
    if (!navigator.mediaDevices?.getUserMedia) {
      setToast("This browser cannot record audio. Use the text box instead.");
      setTimeout(() => setToast(""), 4000);
      return;
    }

    setVoiceState((state) => transition(state, VoiceState.REQUESTING_PERMISSION));
    setError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      await audio.start(stream);

      // Feature-detect the container rather than assuming: Safari cannot
      // record WebM/Opus and would throw inside MediaRecorder.
      const mimeType = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]
        .find((type) => MediaRecorder.isTypeSupported?.(type));
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      audioChunksRef.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) audioChunksRef.current.push(event.data);
      };

      recorder.onstop = () => {
        releaseMicrophone(recorder);
        const blob = new Blob(audioChunksRef.current, { type: recorder.mimeType || "audio/webm" });
        transcribeAudio(blob);
      };

      recorder.onerror = () => {
        releaseMicrophone(recorder);
        setError("Recording failed. Please try again.");
        setVoiceState((state) => transition(state, VoiceState.ERROR));
      };

      recorder.start(100);
      setVoiceState((state) => transition(state, VoiceState.LISTENING));
      addActivity("VOICE", "Voice input started");
    } catch (failure) {
      console.error("Microphone access error:", failure);
      // Denied permission and missing hardware need different instructions,
      // so they are told apart rather than both called "mic denied".
      const denied = failure?.name === "NotAllowedError" || failure?.name === "SecurityError";
      setToast(
        denied
          ? "Microphone permission was denied. Enable it in your browser's site settings, or use the text box."
          : `No microphone is available (${failure?.name || "unknown"}). You can keep using the text box.`,
      );
      setTimeout(() => setToast(""), 6000);
      setVoiceState((state) => transition(state, VoiceState.ERROR));
      addActivity("SYSTEM", `Voice input error: ${failure?.message || failure}`);
    }
  }, [audio, addActivity]);

  /** Stop the recorder's tracks and hand back the audio level hook. */
  const releaseMicrophone = useCallback((recorder) => {
    recorder?.stream?.getTracks().forEach((track) => track.stop());
    mediaRecorderRef.current = null;
    audio.stop();
  }, [audio]);

  const stopRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      // onstop releases the microphone and runs transcription.
      recorder.stop();
    }
  }, []);

  const transcribeAudio = useCallback(async (blob) => {
    setVoiceState((state) => transition(state, VoiceState.TRANSCRIBING));
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const data = await transcribe(blob, controller.signal);
      const transcript = data.text?.trim() || "";
      if (!transcript) {
        setToast("I could not hear anything. Try again, or type your message.");
        setTimeout(() => setToast(""), 4000);
        addActivity("SYSTEM", "Voice input: empty transcript");
        setVoiceState((state) => transition(state, VoiceState.IDLE));
        return;
      }
      addActivity("VOICE", `Transcribed: ${transcript}`);
      // Shown in the composer before sending, so the user can see what was
      // heard and correct it by editing rather than re-recording.
      setMessage(transcript);
      await sendMessage(transcript);
    } catch (failure) {
      if (failure?.kind === "cancelled") return;
      console.error("Transcription error:", failure);
      setToast(`Speech recognition failed: ${failure?.message || "unknown error"}. You can type your message instead.`);
      setTimeout(() => setToast(""), 6000);
      setVoiceState((state) => transition(state, VoiceState.ERROR));
      addActivity("SYSTEM", `Voice input error: ${failure?.message}`);
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
      setVoiceState((state) =>
        state === VoiceState.SPEAKING || state === VoiceState.LISTENING || state === VoiceState.PROCESSING
          ? state
          : transition(state, VoiceState.IDLE),
      );
    }
  }, [addActivity, sendMessage]);

  const playTTS = useCallback(async (text) => {
    if (!voiceOutputEnabled) return;
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const response = await fetchSpeech(text, controller.signal);
      const audioBlob = await response.blob();
      if (!audioBlob.size) throw new Error("empty audio");

      const audioUrl = URL.createObjectURL(audioBlob);
      const player = new Audio(audioUrl);
      playbackRef.current = player;

      // Release the URL and the element whether playback finished, failed or
      // was stopped, so nothing leaks and the same reply never plays twice.
      const finish = () => {
        URL.revokeObjectURL(audioUrl);
        player.onended = null;
        player.onerror = null;
        if (playbackRef.current === player) playbackRef.current = null;
      };

      player.onended = () => {
        finish();
        setVoiceState((state) => transition(state, VoiceState.IDLE));
      };
      player.onerror = () => {
        finish();
        // The text reply is already on screen, so a TTS failure is a warning,
        // not a broken conversation.
        setToast("Could not play the audio. The text reply is still here.");
        setTimeout(() => setToast(""), 4000);
        setVoiceState((state) => transition(state, VoiceState.IDLE));
      };

      setVoiceState((state) => transition(state, VoiceState.SPEAKING));
      await player.play();
    } catch (failure) {
      console.error("TTS playback error:", failure);
      if (failure?.kind !== "cancelled") {
        addActivity("SYSTEM", `TTS error: ${failure?.message}`);
        setToast("Speech output is unavailable. Continuing with text.");
        setTimeout(() => setToast(""), 4000);
      }
      setVoiceState((state) => transition(state, VoiceState.IDLE));
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  }, [voiceOutputEnabled, addActivity]);

  const stopPlayback = useCallback(() => {
    const player = playbackRef.current;
    if (player) {
      player.pause();
      player.currentTime = 0;
      playbackRef.current = null;
    }
    setVoiceState((state) => transition(state, VoiceState.IDLE));
  }, []);

  const stopEverything = useCallback(() => {
    // Cancel an in-flight request, release the microphone and the speaker.
    requestRef.current?.abort(new DOMException("user", "AbortError"));
    requestRef.current = null;
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      // Detach first: stopping normally would fire onstop and transcribe a
      // recording the user just cancelled.
      recorder.onstop = null;
      recorder.stop();
    }
    releaseMicrophone(recorder);
    stopPlayback();
  }, [releaseMicrophone, stopPlayback]);

  const toggleVoice = useCallback(async () => {
    switch (voiceAction(voiceState)) {
      case "start":
        await startRecording();
        break;
      case "stop-recording":
        stopRecording();
        break;
      case "stop-playback":
        stopPlayback();
        break;
      default:
        break;
    }
  }, [voiceState, startRecording, stopRecording, stopPlayback]);

  async function sendMessage(textOverride) {
    const userMessage = (textOverride ?? message).trim();
    if (!userMessage || loading || !activeConversation) return;
    if (!backendReady) {
      setError("Zoe cannot connect to the assistant. Check that the backend is running and try again.");
      return;
    }

    const conversationId = activeConversation.id;
    addActivity("INPUT", userMessage);
    updateActiveMessages((previous) => [...previous, { role: "user", text: userMessage, id: `user-${Date.now()}` }]);
    setMessage("");
    setLoading(true);
    setError("");
    // Keep the retry available: the failed turn is remembered so the user can
    // resend the same text without retyping it.
    setRetryMessage(userMessage);
    setVoiceState((state) => transition(state, VoiceState.PROCESSING));

    const controller = new AbortController();
    requestRef.current = controller;

    try {
      const data = await sendChat({
        message: userMessage,
        conversationId,
        spoken: voiceOutputEnabled,
        signal: controller.signal,
      });
      const replyText = typeof data.response === "string" ? data.response.trim() : "";
      if (!replyText) throw new BackendError("The assistant returned an empty reply.");

      addActivity("REPLY", replyText);
      // Guard against a stale response landing after the user switched chats.
      if (currentActiveId !== conversationId) return;
      updateActiveMessages((previous) => [...previous, { role: "zoe", text: replyText, id: `zoe-${Date.now()}` }]);
      setRetryMessage("");

      if (voiceOutputEnabled) {
        await playTTS(replyText);
      } else {
        setVoiceState((state) => transition(state, VoiceState.IDLE));
      }
    } catch (failure) {
      // A cancelled turn is not a failure, so it is not shown as one.
      if (failure?.kind === "cancelled") return;
      const reason = failure?.message || "Something went wrong reaching the assistant.";
      addActivity("SYSTEM", `Chat failed: ${reason}`);
      // Rendered as an error, NOT as a message from Zoe. Pasting an error into
      // the transcript is how a failure starts reading as something she said.
      setError(reason);
      setVoiceState((state) => transition(state, VoiceState.ERROR));
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
      setLoading(false);
      setVoiceState((state) =>
        state === VoiceState.SPEAKING || state === VoiceState.LISTENING || state === VoiceState.TRANSCRIBING
          ? state
          : transition(state, VoiceState.IDLE),
      );
    }
  }

  function onComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  }

  // Handle voice state changes for globe animation
  const globeMode = useMemo(() => {
    if (import.meta.env.DEV && devMode !== "idle") return devMode;
    switch (voiceState) {
      case VoiceState.LISTENING: return "listening";
      case VoiceState.SPEAKING: return "speaking";
      case VoiceState.PROCESSING:
      case VoiceState.TRANSCRIBING: return "speaking";
      default: return "idle";
    }
  }, [voiceState, devMode]);

  const visibleConversations = useMemo(() => {
    const query = search.trim().toLowerCase();
    return [...conversations]
      .sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt))
      .filter((chat) => !query || chat.title.toLowerCase().includes(query) || chat.messages.some((m) => m.text.toLowerCase().includes(query)));
  }, [conversations, search]);

  const timeText = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dateText = now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const quickActions = [
    { name: "Chat", caption: "Have a conversation", icon: "chat", prompt: "Let's have a conversation." },
    { name: "Get Help", caption: "Solve your doubts", icon: "help", prompt: "Help me understand a topic step by step." },
    { name: "Create", caption: "Write, design, plan", icon: "sparkles", prompt: "Help me create something. Ask me what I have in mind." },
    { name: "Code", caption: "Build and debug", icon: "code", prompt: "Help me with a coding task. Ask me what I'm building." },
    { name: "Search", caption: "Get latest information", icon: "globe", prompt: "Help me research a topic and explain what you know." },
  ];

  // Voice state display
  const voiceStateLabel = VOICE_LABELS;
  const isVoiceActive = isActive(voiceState);

  return (
    <div className="zoe-app">
      <div className="scene-backdrop" aria-hidden="true">
        <div className="scene-window"><div className="scene-sky" /><div className="scene-mountains" /><div className="scene-lake" /><div className="scene-glow" /></div>
        <div className="edge-glow edge-glow-left" /><div className="edge-glow edge-glow-right" />
      </div>

      <div className="window-frame">
        <header className="window-titlebar">
          <div className="titlebar-brand"><span className="brand-dot"><Icon name="sparkles" size={17} /></span><span>{assistantConfig.assistantName}</span><span className="brand-sparkle">✦</span></div>
          <span className="titlebar-subtitle">{assistantConfig.tagline}</span>
          <div className="window-controls" aria-label="Window controls">
            <button aria-label="Pin window"><Icon name="pin" size={15} /></button>
            <button aria-label="Minimize window" onClick={() => window.electronWindow?.minimize?.()}><Icon name="minimize" size={16} /></button>
            <button aria-label="Maximize window" onClick={() => window.electronWindow?.maximize?.()}><Icon name="maximize" size={15} /></button>
            <button aria-label="Close window" onClick={() => window.electronWindow?.close?.()}><Icon name="close" size={18} /></button>
          </div>
        </header>

        <aside className={`sidebar glass-panel ${mobileSidebarOpen ? "sidebar-open" : ""}`}>
          <div className="sidebar-brand">
            <div className="brand-name">{assistantConfig.assistantName}<span>✦</span></div>
            <div className="brand-tagline">{assistantConfig.tagline}</div>
          </div>
          <div className="sidebar-search-row">
            <label className="search-box"><Icon name="search" size={18} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search conversations" aria-label="Search conversations" /></label>
            <button className="new-chat-button" onClick={newChat} aria-label="New chat" title="New chat"><Icon name="plus" size={22} /></button>
          </div>
          <div className="conversation-scroll">
            {visibleConversations.length === 0 && <p className="empty-search">No conversations found.</p>}
            {["Today", "Last 7 days"].map((group, groupIndex) => {
              const rows = visibleConversations.filter((chat) => {
                const age = (Date.now() - new Date(chat.updatedAt).getTime()) / 86400000;
                return groupIndex === 0 ? age < 1 : age >= 1 && age <= 7.01;
              });
              return rows.length > 0 && <section className="conversation-group" key={group}><div className="list-label">{group}</div>{rows.map((chat) => (
                <div className={`conversation-row ${chat.id === currentActiveId ? "active" : ""}`} key={chat.id}>
                  <button className="conversation-select" onClick={() => selectChat(chat.id)} aria-current={chat.id === currentActiveId ? "page" : undefined}>
                    <span className="conversation-icon"><Icon name={chat.icon || "chat"} size={22} /></span>
                    <span className="conversation-copy"><span className="conversation-title">{chat.title}</span><span className="conversation-time">{relativeTime(chat.updatedAt)}</span></span>
                  </button>
                  <button className="conversation-more" onClick={() => setOpenMenuId(openMenuId === chat.id ? null : chat.id)} title="Conversation options" aria-label={`More options for ${chat.title}`}><Icon name="more" size={18} /></button>
                  {openMenuId === chat.id && <button className="conversation-delete" onClick={() => { deleteChat(chat.id); setOpenMenuId(null); }}>Delete</button>}
                </div>
              ))}</section>;
            })}
          </div>
          <button className="settings-button" onClick={() => setShowSettings(true)}><Icon name="settings" size={22} /><span>Settings</span></button>
        </aside>

        {mobileSidebarOpen && <button className="mobile-scrim" aria-label="Close sidebar" onClick={() => setMobileSidebarOpen(false)} />}

        <main className="main-content">
          <div className="mobile-topline"><button onClick={() => setMobileSidebarOpen(true)} aria-label="Open conversations"><Icon name="chat" /></button><span>{assistantConfig.assistantName}</span><span className="mobile-status"><i className={backendReady ? "online" : "offline"} /> {backendReady ? "Online" : "Starting..."}</span></div>
          <aside className="right-widgets">
            <div className="clock-card glass-panel"><div className="clock-card-top"><span>{dateText}</span><Icon name="sun" size={22} /></div><div className="clock-card-time">{timeText}</div><div className="clock-card-greeting">{getGreeting(now)}</div></div>
            <div className="quote-card glass-panel"><span className="quote-mark">“</span><p>Small steps every day lead to big results.</p></div>
          </aside>

          <section className={`hero-section ${messages.length ? "has-messages" : ""}`}>
            {messages.length === 0 ? (
              <>
                <div className="hero-copy"><h1>Hi, I'm <span>{assistantConfig.assistantName}</span><b>✦</b></h1><p>{assistantConfig.greetingSub}</p></div>
                <Globe mode={globeMode} levelRef={audio.levelRef} />
                <div className="quick-actions">
                  {quickActions.map((action, index) => <button key={action.name} className={`quick-card quick-${index}`} onClick={() => { setMessage(action.prompt); document.querySelector(".composer-input")?.focus(); }}><span className="quick-icon"><Icon name={action.icon} size={25} /></span><span className="quick-title">{action.name}</span><span className="quick-caption">{action.caption}</span></button>)}
                </div>
              </>
            ) : (
              <div className="conversation-view">
                <div className="conversation-heading"><span className="eyebrow">YOUR SPACE, YOUR PACE</span><h1>{activeConversation.title}</h1><p>Keep the conversation going with {assistantConfig.assistantName}.</p></div>
                <div className="message-list" aria-live="polite">
                  {messages.map((item) => <article className={`chat-message ${item.role === "user" ? "from-user" : "from-assistant"}`} key={item.id || `${item.role}-${item.text.slice(0, 12)}`}><div className="message-avatar">{item.role === "user" ? "Y" : <Icon name="sparkles" size={17} />}</div><div className="message-body"><div className="message-name">{item.role === "user" ? "You" : assistantConfig.assistantName}</div><div className="message-text">{item.text}</div></div></article>)}
                  {loading && <article className="chat-message from-assistant"><div className="message-avatar"><Icon name="sparkles" size={17} /></div><div className="message-body"><div className="message-name">{assistantConfig.assistantName}</div><div className="typing-dots"><i /><i /><i /></div></div></article>}
                  {/* An error is shown as an error, never as something Zoe said,
                      and it offers a retry rather than dead-ending the turn. */}
                  {error && (
                    <div className="chat-error" role="alert">
                      <span className="chat-error-text">{error}</span>
                      {retryMessage && !loading && (
                        <button className="chat-error-retry" onClick={() => sendMessage(retryMessage)}>
                          Try again
                        </button>
                      )}
                      <button className="chat-error-dismiss" onClick={() => setError("")} aria-label="Dismiss error">
                        <Icon name="close" size={16} />
                      </button>
                    </div>
                  )}
                  <div ref={messageEndRef} />
                </div>
              </div>
            )}
          </section>

          <div className="composer-zone">
            <form className="composer glass-panel" onSubmit={(event) => { event.preventDefault(); sendMessage(); }}>
              <button type="button" className="attach-button" aria-label="Attachment (not yet available)" title="Attachments are not available yet"><Icon name="attach" size={21} /></button>
              <textarea className="composer-input" rows="1" value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={onComposerKeyDown} disabled={loading} placeholder={`Message ${assistantConfig.assistantName}...`} aria-label={`Message ${assistantConfig.assistantName}`} />
              <button type="submit" className="send-button" disabled={loading || !message.trim()} aria-label="Send message" title="Send message"><Icon name="send" size={21} /></button>
            </form>
            <div className="composer-tools">
              {/* Voice input: disabled only when the backend says it cannot
                  transcribe, and its label always reflects real state. */}
              <button
                className={`tool-chip ${isVoiceActive ? "is-on" : ""} ${voiceState === VoiceState.ERROR ? "has-error" : ""}`}
                onClick={toggleVoice}
                disabled={!backendReady || capabilities?.voice_input_available === false}
                aria-label={voiceStateLabel[voiceState]}
                title={
                  capabilities?.voice_input_available === false
                    ? "Speech recognition is unavailable on the backend. Use the text box."
                    : voiceStateLabel[voiceState]
                }
              >
                {voiceAction(voiceState) === "stop-playback" ? <Icon name="stop" size={16} /> : <Icon name="mic" size={16} />}
                {voiceStateLabel[voiceState]}
              </button>
              <button
                className={`tool-chip ${voiceOutputEnabled ? "is-on" : ""}`}
                onClick={() => setVoiceOutputEnabled((enabled) => !enabled)}
                disabled={capabilities?.voice_output_available === false}
                aria-label={voiceOutputEnabled ? "Voice output on" : "Voice output off"}
                title={
                  capabilities?.voice_output_available === false
                    ? "Speech synthesis is unavailable on the backend."
                    : voiceOutputEnabled ? "Voice output on" : "Voice output off"
                }
              >
                <Icon name={voiceOutputEnabled ? "volume2" : "volumex"} size={16} />
                Voice Out
              </button>
              {/* Stop is only enabled while something is actually running, so
                  it never claims to cancel work that has already finished. */}
              <button
                className={`tool-chip ${loading || isVoiceActive ? "is-on" : ""}`}
                onClick={stopEverything}
                disabled={!loading && !holdsAudioResources(voiceState) && voiceState !== VoiceState.SPEAKING && voiceState !== VoiceState.TRANSCRIBING && voiceState !== VoiceState.PROCESSING}
                aria-label="Stop"
                title="Stop recording, playback or the current reply"
              >
                <Icon name="stop" size={16} />
                Stop
              </button>
              <button className="tool-chip" disabled title="Web search runs when the assistant decides to use it"><Icon name="globe" size={16} /> Web Search</button>
              <button className="tool-chip" disabled title="Not available in this build"><Icon name="brain" size={16} /> Deep Think</button>
              <button className="tool-chip" disabled title="Not available in this build"><Icon name="image" size={16} /> Generate Image</button>
            </div>
            {toast && <div className="mic-toast" role="status">{toast}</div>}
            {import.meta.env.DEV && <label className="dev-mode">Globe <select value={devMode} onChange={(event) => setDevMode(event.target.value)}><option value="idle">Idle</option><option value="listening">Listening</option><option value="speaking">Speaking</option></select></label>}
          </div>
        </main>
      </div>

      {showSettings && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowSettings(false); }}><section className="settings-modal glass-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title"><button className="modal-close" onClick={() => setShowSettings(false)} aria-label="Close settings"><Icon name="close" /></button><div className="settings-icon"><Icon name="settings" size={26} /></div><h2 id="settings-title">Settings</h2><p>Zoe's interface preferences</p><div className="setting-row"><span><strong>Assistant</strong><small>Personal AI companion</small></span><b>{assistantConfig.assistantName}</b></div><div className="setting-row"><span><strong>Chat history</strong><small>Saved in this browser</small></span><b>{conversations.length} chats</b></div><div className="setting-row"><span><strong>Voice output</strong><small>Speak responses aloud</small></span><b>{voiceOutputEnabled ? "On" : "Off"}</b></div><div className="setting-row"><span><strong>Backend</strong><small>Zoe assistant API</small></span><b>{backendReady ? "Connected" : "Disconnected"}</b></div>
            {capabilities && <div className="setting-row"><span><strong>Models ready</strong><small>Configured and usable right now</small></span><b>{capabilities.models_usable}</b></div>}
            {capabilities && <div className="setting-row"><span><strong>Speech input</strong><small>Local speech recognition</small></span><b>{capabilities.voice_input_available ? "Available" : "Unavailable"}</b></div>}
            {capabilities && <div className="setting-row"><span><strong>Speech output</strong><small>{capabilities.tts_engine} engine</small></span><b>{capabilities.voice_output_available ? "Available" : "Unavailable"}</b></div>}
            <button className="clear-history" onClick={() => { if (window.confirm("Delete all saved conversations from this browser?")) { conversations.forEach((chat) => restartConversation(chat.id)); const fresh = initialConversation(); setConversations([fresh]); setActiveId(fresh.id); setShowSettings(false); } }}>Clear all conversations</button></section></div>}
    </div>
  );
}

export default App;