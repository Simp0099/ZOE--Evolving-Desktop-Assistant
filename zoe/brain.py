"""Zoe Brain — provider-agnostic LLM reasoning engine.

The Brain owns the conversation and the tools. It does **not** own the model.

Flow for one user message:

    user text (verbatim)
      -> classify the request            (zoe.classify)
      -> route to a ranked fallback chain (zoe.router)
      -> open a provider-agnostic session (zoe.providers)
      -> serve tool requests until the model returns text
      -> return the text

Conversation history lives here in a neutral format and is handed to whichever
model is selected. That is what lets a conversation switch models mid-thread
without losing context, and what keeps the AI Partner identity independent of
the model behind it.

Error handling is honest: failures raise :class:`BrainError` rather than being
returned as if the assistant had said them.
"""

import json
import re
import threading
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from zoe.config import (
    MAX_TOOL_ROUNDS,
    MAX_HISTORY_MESSAGES,
    REQUEST_TIMEOUT_S,
    EMAIL_CONFIRM_TIMEOUT_S,
    APP_CONFIRM_TIMEOUT_S,
    DEBUG_INPUT,
    ZOE_SYSTEM_PROMPT,
    DEFAULT_CITY,
    LLM_MODEL,
    GREETING_NAME,
    MAX_SPOKEN_WORDS,
    VOICE_MAX_OUTPUT_TOKENS,
    TEXT_MAX_OUTPUT_TOKENS,
)
from zoe.tools import (
    web,
    email_tool,
    nasa,
    media,
    dictionary_tool,
    weather,
    general,
    system_tools,
    apps,
)
from zoe import behavior
from zoe import memory
from zoe import tone
from zoe import vision
from zoe.logger import logger, StatusIndicator
from zoe.trace import current_trace, new_trace, span_of, use_trace
from zoe.model_layer import (
    ModelLayer,
    ModelSpec,
    ProviderError,
    get_model_layer,
    set_model_layer,
)
from zoe.providers.base import (
    ChatSession,
    ModelResponse,
    ToolCall,
    estimate_tokens,
)

# Lazy-loaded memory database connection
_memory_conn = None

#: The Brain currently executing a tool call. Module-level tools have no
#: ``self``; ``send_email`` reads this to reach the conversation that must hold
#: its pending draft. Set for the duration of each tool call in
#: ``ZoeBrain._execute_tool_call``.
_current_brain: ContextVar = ContextVar("zoe_current_brain", default=None)

#: Appended to the system prompt for a turn whose reply will be spoken aloud.
#: Spoken replies have to be short and read cleanly; the same answer typed to
#: the screen can be longer and carry markdown.
#: Appended to the system prompt for a spoken turn only.
#:
#: Adapted from Mark LV's `[VOICE]` block: the answer leads, the reason follows
#: only when it changes something, length matches the task, and speech carries no
#: markup. The existing "at most two short sentences" rule is kept -- it was
#: already working -- and the parts that were missing are added: answer first,
#: 1-3 sentences rather than a hard two, structure on request, and no padding.
#:
#: Deliberately absent: anything about language. The user's language is decided
#: elsewhere and this block must not touch it.
_SPOKEN_BLOCK = (
    "\n\nYou are speaking aloud. This reply will be heard, not read.\n"
    "- One to three sentences. That is the default for everything, including "
    "questions that sound like they need a full explanation. Go longer only when "
    "they explicitly ask for detail, or when the short version would drop the one "
    "thing they actually came for.\n"
    "- Lead with the answer. The first sentence is the answer; explain after it "
    "only if it changes what they do next.\n"
    "- No markdown, bullets, headings, tables or URLs unless they ask for a "
    "structured answer. Numbers and units are spoken as words.\n"
    "- No filler: no opening praise, no closing question that only keeps the "
    "conversation going, nothing added to fill the length."
)


def _turn_token_ceiling(spoken: bool) -> Optional[int]:
    """Token ceiling for this turn, or None to use the model's own limit.

    A spoken turn gets the voice cap. A typed turn keeps the configured default
    unless `voice.text_max_output_tokens` is set explicitly -- a long written
    answer is not a wall of sound, and silently shortening every typed reply
    would be a regression nobody asked for.
    """
    if spoken:
        return VOICE_MAX_OUTPUT_TOKENS or None
    return int(TEXT_MAX_OUTPUT_TOKENS) if TEXT_MAX_OUTPUT_TOKENS else None


class BrainError(RuntimeError):
    """Raised when no configured model can serve the request.

    Carries a short, user-safe ``message`` plus the underlying ``detail`` for
    logs. Raising instead of returning a string is what keeps transport errors
    from masquerading as assistant replies.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def get_memory_conn():
    """Lazy-initialize the long-term memory database connection."""
    global _memory_conn
    if _memory_conn is None:
        _memory_conn = memory.init_memory_db()
    return _memory_conn


def _build_system_prompt() -> str:
    """Build the base identity prompt.

    Identity only. Long-term memory is deliberately *not* appended here: the
    prompt defines who Ai Partner is, while memory defines what it knows about
    the user, and the two are retrieved on different schedules. Memory is added
    per turn by :func:`_build_context_block`, which can scope it to the request.

    The same identity is passed to every compatible model, so switching models
    does not change who the AI Partner is.

    Returns:
        The base system prompt.
    """
    return ZOE_SYSTEM_PROMPT


def _build_context_block(
    message: str,
    classification: Any,
    history: List[Dict[str, Any]],
    images: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the per-turn context: relevant memory, then behavioral posture.

    Retrieval is scoped to this request rather than dumping the whole store,
    so an unrelated question does not carry a page of project history with it.

    Args:
        message: The user's message this turn.
        classification: The Brain's read of the request, for the directive.
        history: This conversation's earlier turns.
        images: Images attached to this turn, if any.

    Returns:
        Prompt text, or an empty string when there is nothing to add.
    """
    parts = []

    # Fold this message into the conversational state first, so the guidance
    # below describes the conversation the user just had. Perception turns never
    # reach here (see `_ask_locked`), so a camera observation can never move the
    # user's mood.
    try:
        tone.observe(message)
    except Exception as e:  # noqa: BLE001 - tone must never break a turn
        logger.warning(f"Conversation state skipped: {e}")

    try:
        # Query on the current message plus recent turns: "keep going" is only
        # resolvable if the last few turns are part of the query.
        recent = " ".join(
            str(m.get("content") or "") for m in history[-4:] if m.get("role") == "user"
        )
        # The connection is shared with the voice turn thread, so every use is
        # serialised. Without this, a voice turn races a typed message and
        # memory retrieval fails for whichever one loses.
        with memory.db_locked():
            conn = get_memory_conn()
            facts = memory.recall_relevant(conn, f"{message} {recent}")
            # Also get the subject index for facts not shown (like Mark LV's
            # [ALSO REMEMBERED] section) - helps the model know what it can
            # look up with recall_memories. Only build the index when we have
            # relevant facts, so ordinary queries don't carry the full index.
            if facts:
                all_memories = memory.list_memories(conn)
                retrieved_subjects = {f for f, _ in facts}
                index_subjects = [
                    m["subject"] for m in all_memories
                    if m["subject"] not in retrieved_subjects
                ]
            else:
                index_subjects = []
    except Exception as e:  # noqa: BLE001 - memory must never break a turn
        logger.warning(f"Memory retrieval skipped: {e}")
        facts = []
        index_subjects = []

    if facts:
        parts.append(memory.format_facts_for_prompt(facts))

    # Add subject index for memories not in the prompt (Mark LV pattern).
    # This lets the model know what it can recall before saying "I don't know".
    # Only shown when we have relevant facts to avoid polluting unrelated queries.
    if index_subjects and facts:
        # Limit index to avoid prompt bloat - show first 15 subjects
        index_display = index_subjects[:15]
        more = f" (+{len(index_subjects) - 15} more)" if len(index_subjects) > 15 else ""
        parts.append(
            "\n\n## Also in Memory (call recall_memories with a keyword to read)\n"
            + ", ".join(index_display) + more
        )

    # Existing keys are only fetched for turns that might write, and only so a
    # correction can reuse one instead of inventing a second key.
    subjects: List[str] = []
    if behavior.memory_posture(message):
        try:
            with memory.db_locked():
                subjects = memory.active_subjects(get_memory_conn())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Subject listing skipped: {e}")

    posture = behavior.memory_context(facts, GREETING_NAME)
    intent = behavior.memory_posture(message, subjects, has_images=bool(images))
    if intent:
        posture = f"{posture}\n- {intent}" if posture else f"- {intent}"
    if posture:
        parts.append("\n\n## Context\n" + posture)

    directive = behavior.turn_directive(
        classification,
        has_history=bool(history),
    )
    if directive:
        parts.append("\n\n## This turn\n" + directive)

    # Dynamic layer around the personality, never a replacement for it. Empty
    # for a neutral conversation, so the common case costs nothing. Guarded like
    # every other optional context piece: a broken tone renderer must not cost
    # the user their answer.
    try:
        parts.append(tone.format_prompt())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Conversation state guidance skipped: {e}")

    if images:
        parts.append("\n\n" + behavior.vision_directive(len(images)))
        # One label per image. A vision model cannot tell a screen capture from
        # a desk camera, so without this it will confidently describe a webcam
        # frame as whatever it expected to be looking at.
        parts.append(vision.describe_sources(images))

    # Short-lived camera view, when there is a fresh one. Empty otherwise, so an
    # idle camera costs nothing on unrelated turns.
    parts.append(vision.format_context_block())

    return "".join(parts)


def _model_candidates() -> List[str]:
    """Configured model keys, in declared order.

    Kept for backwards compatibility with configuration and diagnostics; the
    router is what actually decides the order.
    """
    layer = get_model_layer()
    return [s.key for s in layer.registry.enabled()]


# ============================================================================
# Callable Tools
# ============================================================================

def search_wikipedia(query: str) -> str:
    """Search Wikipedia for a summary of a person, place, concept, or event.

    Args:
        query: The search term or topic to look up.
    """
    return general.search_wikipedia(query)


def open_website(service_or_url: str) -> str:
    """Open a web service (e.g. YouTube, Google, Gmail, Amazon) or custom URL in the browser.

    Args:
        service_or_url: The name of the service or direct URL to open.
    """
    return web.open_service(service_or_url)


def launch_app(app_name: str) -> str:
    """Launch an APPROVED application on this computer.

    Use this only when the user wants a desktop application opened or brought to
    the front. Do NOT use it for: talking or chatting, playing music, creating or
    finding files, or any request that is not a named application. Never pass a
    command, a path or a shell fragment -- pass the application's name only.

    Two outcomes, and the second one is not yours to skip. A name in
    applications.aliases launches directly. A name that is not mapped is looked
    up among the applications installed on this machine; exactly one match is
    offered back as a question and opens only if the user says yes in their next
    message. Nothing launches on this call alone.

    Args:
        app_name: The application name, e.g. "Safari", "WhatsApp". Names in
            applications.aliases in config.yaml launch directly; anything else
            must be confirmed by the user.
    """
    name = apps.normalize_app_name(app_name)
    if not name or apps.lookup(name) is not None:
        # Mapped, or an empty name the tool already answers for. Both are the
        # pre-existing paths and keep their exact behaviour.
        return apps.launch_app(app_name)

    brain = _current_brain.get()
    if brain is None:
        # No conversation to hold a confirmation against, so there is nothing to
        # confirm. Refuse, exactly as the email tool refuses without a Brain.
        return apps.not_approved(name)
    return brain.offer_app_launch(name)


def search_youtube(query: str) -> str:
    """Search YouTube for videos matching the query.

    Args:
        query: The topic or title to search on YouTube.
    """
    return web.search_youtube(query)


def search_google(query: str) -> str:
    """Search Google for web results.

    Args:
        query: The search keywords or question.
    """
    return web.search_google(query)


def search_news(query: str = "", window_days: int = 3) -> str:
    """Fetch CURRENT news with sources and publication dates.

    You MUST use this for any question about what is happening now or recently:
    today's news, the latest developments, a recent update, what happened this
    week, current status. Do NOT answer those from memory -- your training data
    ends months or years before today, so a remembered "latest" event is stale
    by definition.

    Do NOT use it for background explanation. "What is a black hole", "how do
    rockets work" and "tell me about Apollo" are answered directly from what you
    know.

    The result lists each article with its publisher and publication date, and
    separates recent items from older background. Ground your answer only in the
    recent items, name the source, and state the date. If the result says
    retrieval failed, tell the user you could not verify current information
    instead of guessing.

    Args:
        query: Optional topic words such as "Artemis" or "SpaceX". Leave empty
            for general current news.
        window_days: How recent counts as current. Defaults to 3 days.
    """
    from zoe.tools import news

    return news.search_news(query, window_days=window_days)


def open_maps(location: str = "Delhi") -> str:
    """Open Google Maps for a specific city, address, or coordinates.

    Args:
        location: City or address to view on the map.
    """
    return web.open_maps(location)


def get_weather(city: str = "") -> str:
    """Check the current weather for a city: temperature, conditions and range.

    Args:
        city: Name of the city (e.g. Delhi, London, Tokyo). Defaults to the
            configured city when omitted.
    """
    return weather.get_weather(city or DEFAULT_CITY)


def get_nasa_apod(date: str = None) -> str:
    """Fetch NASA's Astronomy Picture of the Day: title, summary and image link.

    Args:
        date: Date in YYYY-MM-DD format, or None for today. Dates with no
            picture available return NASA's most recent one instead.
    """
    data = nasa.get_nasa_apod(date)
    if not data:
        return "NASA's astronomy picture service is not responding right now."
    return f"{nasa.format_apod(data)}\n\n{nasa.summarize_apod(data)}"


def take_screenshot(filename: str = None, *, return_image_part: bool = False) -> str:
    """Capture a screenshot of the user's screen.

    Args:
        filename: Optional name for the screenshot file.
        return_image_part: If True, return an image part with SOURCE_SCREEN label
            so the model can immediately analyze the screenshot. Default: False.
    """
    result = media.take_screenshot(filename, return_image_part=return_image_part)
    if return_image_part and isinstance(result, dict):
        path = result.get("path", "")
        part = result.get("image_part")
        if part:
            # Return the image part info for the model to use
            return f"Screenshot captured and saved to {path}. Image part ready for analysis with SOURCE_SCREEN label."
        return f"Screenshot captured and saved to {path}"
    return f"Screenshot captured and saved to {result}" if result else "Screenshot failed."


def screen_process(angle: str = "screen", text: str = "") -> str:
    """Capture the screen or webcam and analyze it with a vision model.

    This tool captures a live frame from the screen or webcam, attaches it with
    the correct source label (SOURCE_SCREEN or SOURCE_WEBCAM), and sends it to
    the vision model along with the user's question or instruction.

    Args:
        angle: Either "screen" to capture the display, or "camera" to capture
            the webcam. Default: "screen".
        text: The question or instruction about the captured image. Required.

    Returns:
        The model's analysis of the captured image, with the source clearly
        identified in the response.
    """
    if not text or not text.strip():
        return "Please provide a question or instruction about what to analyze."

    angle = angle.strip().lower()
    if angle not in ("screen", "camera"):
        return "Invalid angle. Use 'screen' or 'camera'."

    brain = _current_brain.get()
    if brain is None:
        return "No active conversation to process the image."

    try:
        import pyautogui
    except ImportError:
        return "pyautogui is not installed; cannot capture a screenshot."

    # Capture the image
    if angle == "screen":
        try:
            img = pyautogui.screenshot()
            # Save temporarily and create image part
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_data = buf.getvalue()
        except Exception as e:
            logger.error(f"Screen capture failed: {e}", exc_info=True)
            return f"Failed to capture screen: {e}"
    else:  # camera
        try:
            import cv2
        except ImportError:
            return "OpenCV (opencv-python) is not installed; cannot capture webcam."
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return "Webcam not available."
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return "Webcam returned no frame."
        import io
        from PIL import Image
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        img_data = buf.getvalue()

    # Create image part with correct source label
    from zoe.providers.base import image_part, SOURCE_SCREEN, SOURCE_WEBCAM
    source_label = SOURCE_SCREEN if angle == "screen" else SOURCE_WEBCAM
    part = image_part(img_data, "image/png" if angle == "screen" else "image/jpeg", source=source_label)

    # Send to vision model as an ephemeral turn
    try:
        response = brain.ask(text.strip(), images=[part], ephemeral=True)
        return f"[Analyzed {angle} capture]\n{response}"
    except Exception as e:
        logger.error(f"Screen process analysis failed: {e}", exc_info=True)
        return f"Failed to analyze {angle} capture: {e}"


def play_music(song_name: str) -> str:
    """Play a song or music track on YouTube.

    Args:
        song_name: Name of the track or artist to play.
    """
    return media.play_music(song_name)


def lookup_dictionary(word: str) -> str:
    """Look up dictionary definitions, meanings, or synonyms for an English word.

    Args:
        word: The word to define.
    """
    return dictionary_tool.get_meaning(word)


def send_email(to_address: str, subject: str, message: str) -> str:
    """Draft an email for the user to confirm. This tool never sends.

    Record the recipient, subject and body, then read them back and ask the user
    to confirm. The email is sent only after the user clearly agrees in their
    *next* message; there is no argument you can pass to send it yourself.

    Args:
        to_address: Recipient email address.
        subject: Subject line of the email.
        message: Body content of the email.
    """
    brain = _current_brain.get()
    if brain is None:
        # No conversation to hold the draft, so there is nothing to confirm
        # against. Refuse rather than send on an unconfirmed path.
        return ("This email was not sent. An email can only be drafted from "
                "inside a conversation, so the user can confirm it.")
    return brain.stage_email_draft(to_address, subject, message)


def tell_time() -> str:
    """Get the current system time."""
    return general.tell_time()


def tell_joke() -> str:
    """Get a random funny programming or general joke."""
    return general.tell_joke()


# --- System Introspection Tools ---

def get_system_time() -> str:
    """Get the current system date and time.

    Returns:
        A human-readable string with today's date and the current time.
    """
    return system_tools.get_system_time()


def get_directory_contents(directory: str = ".") -> str:
    """List the files and subdirectories in a local directory.

    Args:
        directory: Path to the directory to inspect. Defaults to the current working directory.

    Returns:
        A formatted listing of directory contents with file sizes.
    """
    return system_tools.get_directory_contents(directory)


def get_system_status() -> str:
    """Check local system status including OS, CPU, memory, and disk usage.

    Returns:
        A formatted summary of key system metrics.
    """
    return system_tools.get_system_status()


# --- Memory Tools ---

def save_memory(
    fact: str,
    category: str = "general",
    subject: str = "",
    source: str = "inferred",
) -> str:
    """Save a fact, preference, decision, or project note to long-term memory.

    Args:
        fact: The durable information to remember.
        category: One of preference, project, goal, decision, context, general.
        subject: Stable key so a later correction replaces this entry instead of
            contradicting it (e.g. 'user:name'). Defaults to the fact text.
        source: 'explicit' when the user asked for it to be remembered.

    Returns:
        Confirmation only when the write actually succeeded.
    """
    if not (fact or "").strip():
        return "Nothing to save -- the fact was empty."
    try:
        with memory.db_locked():
            conn = get_memory_conn()
            if memory.contains_secret(fact):
                return ("I didn't store that: it looks like it contains a credential "
                        "(a key, token, or password). Nothing has been saved.")
            if not memory.remember(
                conn, fact, category, subject=subject or None, source=source
            ):
                # Never promise persistence that did not happen.
                return "I couldn't save that to memory. It has not been stored."

            count = memory.get_memory_count(conn)
        StatusIndicator.memory(f"Saved fact #{count}: '{fact}'")
        superseded = ""
        if subject:
            superseded = " It replaced the earlier version of this."
        return f"Stored in long-term memory (fact #{count}).{superseded}"
    except Exception as e:
        logger.error(f"save_memory failed: {e}", exc_info=True)
        return "I couldn't save that to memory. It has not been stored."


def forget_memory(query: str) -> str:
    """Forget stored information the user asks to be forgotten.

    Deactivates matching memories so they are no longer retrieved. Reports
    honestly when nothing matched.

    Args:
        query: Free text identifying what to forget.

    Returns:
        Confirmation listing what was forgotten, or a message if none matched.
    """
    if not (query or "").strip():
        return "Nothing to forget -- no description was given."
    try:
        with memory.db_locked():
            conn = get_memory_conn()
            forgotten = memory.forget_matching(conn, query)
        if forgotten:
            for fact in forgotten:
                StatusIndicator.memory(f"Forgotten: '{fact}'")
            listed = "; ".join(forgotten)
            return f"Forgotten and no longer retrieved: {listed}"
        candidates = memory.find_matching(conn, query)
        if candidates:
            return "Several memories match. Please specify one fact to forget: " + "; ".join(candidates)
        if len(query.strip()) < 3:
            return "Please give at least three meaningful characters so I can identify the memory safely."
        return f"Nothing in memory matched '{query}', so nothing was forgotten."
    except Exception as e:
        logger.error(f"forget_memory failed: {e}", exc_info=True)
        return "I couldn't reach memory just now, so nothing was forgotten."


def recall_memories(query: str = "") -> str:
    """Recall facts from long-term memory, optionally filtered by a search query.

    Args:
        query: Optional search term to filter memories. If empty, returns all
            active stored facts.

    Returns:
        A formatted list of recalled facts, or a message if none match.
    """
    try:
        with memory.db_locked():
            conn = get_memory_conn()

            if query:
                facts = memory.search_memories(conn, query)
            else:
                facts = memory.recall_all(conn)

        if query:
            if facts:
                lines = [f"Here's what I remember about '{query}':"]
                for i, fact in enumerate(facts, 1):
                    lines.append(f"  {i}. {fact}")
                return "\n".join(lines)
            return f"Nothing in memory matches '{query}'."

        if facts:
            lines = ["Here's everything I currently remember:"]
            for i, fact in enumerate(facts, 1):
                lines.append(f"  {i}. {fact}")
            return "\n".join(lines)
        return "My long-term memory is empty. Tell me something about yourself and I'll remember it."
    except Exception as e:
        logger.error(f"recall_memories failed: {e}", exc_info=True)
        return "Sorry, I had trouble accessing my memory. Try again."


def list_memories_tool() -> str:
    """List all stored memories with stable identifiers.

    Returns a formatted list showing each memory's subject (stable key), fact,
    category, and source. The subject is used for precise forgetting.

    Returns:
        A formatted list of all memories, or a message if memory is empty.
    """
    try:
        with memory.db_locked():
            conn = get_memory_conn()
            memories = memory.list_memories(conn)

        if not memories:
            return "My long-term memory is empty. Tell me something about yourself and I'll remember it."

        lines = ["Here's everything in my long-term memory:"]
        for i, m in enumerate(memories, 1):
            subject = m["subject"]
            fact = m["fact"]
            category = m["category"]
            source = m["source"]
            lines.append(f"  {i}. [{category}] {subject}: \"{fact}\" (source: {source})")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"list_memories failed: {e}", exc_info=True)
        return "Sorry, I had trouble accessing my memory. Try again."


def forget_by_subject_tool(subject: str) -> str:
    """Forget a specific memory by its exact subject identifier.

    Use list_memories first to see available subjects. This tool requires the
    exact subject string - no fuzzy matching. This prevents accidental deletion.

    Args:
        subject: The exact subject identifier from list_memories.

    Returns:
        Confirmation of what was forgotten, or a message if not found.
    """
    if not (subject or "").strip():
        return "Nothing to forget -- no subject identifier was given. Use list_memories to see available subjects."

    try:
        with memory.db_locked():
            conn = get_memory_conn()
            forgotten = memory.forget_by_subject(conn, subject)

        if forgotten:
            for fact in forgotten:
                StatusIndicator.memory(f"Forgotten: '{fact}'")
            listed = "; ".join(forgotten)
            return f"Forgotten and no longer retrieved: {listed}"

        # Subject not found - show available subjects to help
        with memory.db_locked():
            conn = get_memory_conn()
            available = memory.list_memories(conn)
        subjects = [m["subject"] for m in available]
        if subjects:
            return f"No memory with subject '{subject}' found. Available subjects: {', '.join(subjects[:20])}"
        return f"No memory with subject '{subject}' found and memory is empty."
    except Exception as e:
        logger.error(f"forget_by_subject failed: {e}", exc_info=True)
        return "I couldn't reach memory just now, so nothing was forgotten."


GEMINI_TOOLS = [
    search_wikipedia,
    search_news,
    open_website,
    launch_app,
    search_youtube,
    search_google,
    open_maps,
    get_weather,
    get_nasa_apod,
    take_screenshot,
    screen_process,
    play_music,
    lookup_dictionary,
    send_email,
    tell_time,
    tell_joke,
    get_system_time,
    get_directory_contents,
    get_system_status,
    save_memory,
    recall_memories,
    forget_memory,
    list_memories_tool,
    forget_by_subject_tool,
]

#: Neutral name for the tool set; kept under the old name for compatibility.
TOOL_REGISTRY = {fn.__name__: fn for fn in GEMINI_TOOLS}

#: Snapshot taken BEFORE plugins are appended below. GEMINI_TOOLS and
#: TOOL_REGISTRY both end up holding plugins, so neither can answer "is this a
#: built-in name?" afterwards. Anything that needs the reserved set -- the
#: plugin collision check, for one -- must use this.
BUILTIN_TOOL_NAMES = frozenset(TOOL_REGISTRY)

#: Plugin tools from plugins/*.py. Loaded once at import. A bad plugin is rejected
#: with a log line; it can never stop the built-in tools from loading.
try:
    from zoe import plugins as _plugins

    for _fn in _plugins.load_callables(reserved=TOOL_REGISTRY.keys()):
        GEMINI_TOOLS.append(_fn)
        TOOL_REGISTRY[_fn.__name__] = _fn
except Exception as _plugin_error:  # noqa: BLE001
    logger.error(f"Plugin loading failed; built-in tools only: {_plugin_error}")

#: Tools with external side effects. These must not be executed twice for the
#: same user message -- not even with different arguments, and not even after a
#: model switch mid-turn. An email cannot be unsent and a song cannot be
#: un-played, so a repeated request reuses the first result instead.
#:
#: Read-only tools are deliberately absent: repeating `get_temperature` for two
#: different cities in one turn is legitimate.
NON_IDEMPOTENT_TOOLS = frozenset({
    "send_email",
    "play_music",
    "open_website",
    "launch_app",
    "open_maps",
    "take_screenshot",
    "save_memory",
    "forget_memory",
    "forget_by_subject_tool",
})


# ============================================================================
# Core LLM Reasoning Loop
# ============================================================================

class ZoeBrain:
    """Stateful conversation engine that delegates model choice to the router.

    Each instance owns exactly one conversation. Conversations are isolated by
    construction: two ``ZoeBrain`` objects never share history, and the model
    serving a conversation can change freely without losing it.
    """

    def __init__(self, conversation_id: str = "default", model_layer: Optional[ModelLayer] = None):
        self.conversation_id = conversation_id
        self._layer = model_layer
        self._session: Optional[ChatSession] = None
        self._model_key: Optional[str] = None
        self._model_id: Optional[str] = None
        #: Neutral conversation history, oldest first. Owned here, not by a model.
        self._history: List[Dict[str, Any]] = []
        self._system_prompt = ""
        #: Per-turn context: relevant memory + behavioral layer. Recomputed each
        #: turn, because relevance depends on what is being asked.
        self._turn_context = ""
        #: True while an ephemeral (perception) turn is in flight. Such a turn
        #: gets no tools at all: a camera frame must never be able to call
        #: save_memory, which is how "webcam observation" would become permanent
        #: memory.
        self._ephemeral = False
        self._message_count = 0
        #: Wall-clock deadline for the current turn, shared by every provider
        #: call in it. None outside a turn.
        self._turn_deadline: Optional[float] = None
        # One in-flight request per conversation, so overlapping requests cannot
        # interleave and reorder the user's messages.
        self._lock = threading.Lock()
        #: The last email draft the model staged, awaiting the user's next
        #: message. The model can fill this; only `_resolve_pending_email`, driven
        #: by the user's own turn, can turn it into a send. None when idle.
        self._pending_email: Optional[Dict[str, Any]] = None
        #: The bundle discovered for a requested app, awaiting the user's next
        #: message. Only `_resolve_pending_launch`, driven by the user's own
        #: turn, can turn it into a launch. None when idle. Separate from
        #: `_pending_email` so confirming one can never trigger the other.
        self._pending_launch: Optional[Dict[str, Any]] = None
        #: Which model actually served the last turn (diagnostics only).
        self.last_model_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @property
    def layer(self) -> ModelLayer:
        """The shared model layer, resolved lazily."""
        if self._layer is None:
            self._layer = get_model_layer()
        return self._layer

    def _resolve_spec(self, model: Any) -> ModelSpec:
        """Accept a ModelSpec or a registry key and return the spec."""
        if isinstance(model, ModelSpec):
            return model
        spec = self.layer.spec_for(str(model))
        if spec is None:
            raise BrainError(f"Unknown model '{model}'.", detail=f"unknown model key {model!r}")
        return spec

    def _create_chat(
        self,
        model: Any,
        history: Optional[List[Dict[str, Any]]] = None,
        *,
        spoken: bool = False,
    ) -> ChatSession:
        """Open a provider session for `model`, seeded with prior history.

        The Phase 1 identity prompt is cached on the instance and reused for the
        whole conversation. This turn's context block -- relevant memory plus
        behavioral posture -- is appended to it, so memory stays request-scoped
        while identity stays stable.

        When ``spoken`` is True the reply will be heard, not read, so a short-form
        no-markdown directive is appended as well.
        """
        spec = self._resolve_spec(model)
        provider = self.layer.providers.get(spec.provider)
        if provider is None:
            raise ProviderError(
                kind=_UNCONFIGURED_KIND,
                model_id=spec.model_id,
                detail=f"provider '{spec.provider}' is not configured",
                provider=spec.provider,
            )

        self._system_prompt = self._system_prompt or _build_system_prompt()

        system_prompt = self._system_prompt
        if self._turn_context:
            system_prompt += self._turn_context
        if spoken:
            system_prompt += _SPOKEN_BLOCK

        session = provider.open_session(
            model_id=spec.model_id,
            system_prompt=system_prompt,
            # No tools on a perception turn. The webcam is an observer, not an
            # actor: it may not write memory, send mail, or open a browser.
            tools=[] if self._ephemeral else GEMINI_TOOLS,
            history=list(history or []),
            # Whatever is left of this turn's budget, not a fresh per-call
            # allowance: a tool loop must not restart the clock each round.
            # A spoken turn additionally asks the model to stop early. Both are
            # ceilings on the same number; the smaller wins, so neither can push
            # generation past what the model itself allows.
            **self.layer.options_for(
                spec,
                timeout=self._turn_deadline_seconds(),
                max_output_tokens=_turn_token_ceiling(spoken),
            ),
        )
        self._session = session
        self._model_key = spec.key
        self._model_id = spec.model_id
        return session

    def _get_chat(self) -> ChatSession:
        """Return the current session, opening one on the best model if needed."""
        if self._session is not None:
            return self._session

        classification = self.layer.classify(
            "", tools_available=bool(GEMINI_TOOLS)
        )
        ranked = self.layer.plan(classification, context_tokens=0)
        if not ranked:
            raise BrainError(_NO_MODEL_MESSAGE, detail="no enabled, configured models")

        return self._create_chat(ranked[0].spec)

    def reset_conversation(self):
        """Clear this conversation's history, keeping the system prompt."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None
        self._history = []
        self._turn_context = ""
        self._ephemeral = False
        self._message_count = 0
        self._turn_deadline = None
        self._pending_email = None
        self._pending_launch = None
        # A clean slate is a new conversation, so the conversational state goes
        # with the history rather than describing a conversation that no longer
        # exists.
        tone.reset_tone()

    def _record(self, *messages: Dict[str, Any]) -> None:
        """Append neutral turns to the conversation history."""
        self._history.extend(messages)
        self._message_count += len(messages)
        cap = MAX_HISTORY_MESSAGES * 2
        while len(self._history) > cap:
            next_turn = next(
                (i for i, item in enumerate(self._history[1:], 1)
                 if item.get("role") == "user"),
                None,
            )
            if next_turn is None:
                break  # Keep the complete current turn, even if unusually large.
            del self._history[:next_turn]

    def _history_has_images(self) -> bool:
        """Whether any earlier turn in this conversation carried an image.

        Only the tail of the history is ever replayed, so only the tail needs
        checking -- but the tail is exactly what would be sent.
        """
        return any(turn.get("images") for turn in self._history[-MAX_HISTORY_MESSAGES * 2:])

    def _trim_history(self) -> None:
        """Trim history to a recent window, keeping the conversation usable.

        Trimming drops the oldest turns and rebuilds the session. The system
        prompt and recent context survive, so the model still knows who it is
        and what was just discussed.
        """
        if self._message_count < MAX_HISTORY_MESSAGES:
            return

        keep = max(2, MAX_HISTORY_MESSAGES)
        window = self._history[-keep:]
        first_user = next((i for i, item in enumerate(window)
                           if item.get("role") == "user"), None)
        if first_user is not None:
            window = window[first_user:]
        logger.info(
            f"[BRAIN] session={self.conversation_id} trimming history to "
            f"{len(window)} most recent turns"
        )
        self._session = None
        self._history = window
        self._message_count = len(window)
        if self._model_key:
            try:
                self._create_chat(self._model_key, history=window)
            except Exception as e:  # noqa: BLE001 - never fail a turn over trimming
                logger.warning(f"History trim could not rebuild session: {e}")
                self._session = None

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _execute_tool_call(self, name: str, args: dict) -> str:
        """Execute a registered tool by name with the given arguments.

        The Brain is published to module-level tools for the duration of the
        call, so a tool that needs the conversation (``send_email`` staging its
        draft) can reach it. A model that passes ``confirmed`` to ``send_email``
        gets an "unexpected arguments" error -- there is no path from a model
        tool call to a send.
        """
        func = TOOL_REGISTRY.get(name)
        if func is None:
            logger.error(f"Unknown tool requested: {name}")
            return f"Error: Unknown tool '{name}'."

        token = _current_brain.set(self)
        try:
            result = func(**args)
            return str(result) if result is not None else f"Tool '{name}' executed successfully."
        except TypeError as e:
            logger.error(f"Tool '{name}' got unexpected arguments: {e}")
            return f"Error: '{name}' received unexpected arguments ({e})."
        except Exception as e:
            logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
            return f"Error executing '{name}': {e}"
        finally:
            _current_brain.reset(token)

    # ------------------------------------------------------------------
    # Email confirmation
    # ------------------------------------------------------------------

    def stage_email_draft(self, to_address: str, subject: str, message: str) -> str:
        """Hold an email draft until the user confirms in their next message.

        The model may draft; it may never send. Only
        :meth:`_resolve_pending_email`, driven by the user's own next turn,
        reaches SMTP. Staging a second draft replaces the first, so at most one
        draft is ever awaiting confirmation.
        """
        self._pending_email = {
            "to_address": str(to_address or "").strip(),
            "subject": str(subject or ""),
            "message": str(message or ""),
            "staged_at": _now(),
        }
        # Recipient and subject may be logged; the body never is.
        logger.info(
            f"[EMAIL] draft staged for {self._pending_email['to_address']!r}; "
            "awaiting user confirmation"
        )
        return (
            f"Drafted an email to {self._pending_email['to_address']} "
            f"(subject: {self._pending_email['subject']!r}). Nothing has been sent. "
            "Read it back to the user and ask them to confirm; it is sent only "
            "after they say yes in their next message."
        )

    def _resolve_pending_email(self, message: str) -> Optional[str]:
        """Send a staged draft iff this user turn confirms it.

        A draft lives for exactly one user turn: it is cleared here whether the
        user confirmed, declined, said something unrelated, or took too long.
        Returns the send result, or None to let the turn proceed normally.
        """
        pending = self._pending_email
        self._pending_email = None
        if pending is None:
            return None
        if _now() - pending.get("staged_at", 0.0) > EMAIL_CONFIRM_TIMEOUT_S:
            logger.info("[EMAIL] pending draft expired; not sending")
            return None
        if not email_tool.parse_confirmation(message):
            logger.info("[EMAIL] pending draft not confirmed; discarded")
            return None
        return email_tool.send_email(
            pending["to_address"],
            pending["subject"],
            pending["message"],
            confirmed=True,
        )

    # ------------------------------------------------------------------
    # Discovered application confirmation
    # ------------------------------------------------------------------

    def offer_app_launch(self, name: str) -> str:
        """Offer a discovered application for the user to confirm. Launches nothing.

        Reached only when `name` is absent from the allowlist, so this is the
        convenience path, not a hole in the boundary: nothing runs here, and the
        bundle is launched only by :meth:`_resolve_pending_launch` on a later
        turn that says yes.

        If the discovered app is on the denylist (sensitive apps like Terminal,
        System Settings, password managers), the confirmation prompt includes
        an explicit warning.
        """
        matches = apps.discover(name)
        requested = apps.normalize_app_name(name)

        if not matches:
            logger.info(f"App discovery found nothing for {requested!r}")
            # The existing refusal, kept verbatim -- the decision is unchanged.
            # Discovery only adds the one thing that was missing from it: where
            # to look to make the app launchable next time.
            return f"{apps.not_approved(requested)} {apps.discovery_hint()}"

        if len(matches) > 1:
            shown = ", ".join(matches[:apps.MAX_DISCOVERY_CANDIDATES])
            more = (f" (and {len(matches) - apps.MAX_DISCOVERY_CANDIDATES} more)"
                    if len(matches) > apps.MAX_DISCOVERY_CANDIDATES else "")
            logger.info(f"App discovery ambiguous for {requested!r}: {matches}")
            return (f"I found several applications matching {requested!r}: {shown}"
                    f"{more}. Which one did you mean? I haven't opened anything.")

        bundle = matches[0]
        self._pending_launch = {"bundle": bundle, "staged_at": _now()}
        logger.info(f"[APP] staged discovered launch for {bundle!r}; "
                    "awaiting user confirmation")
        
        # Check if the app is on the denylist for sensitive apps
        denylist_warning = ""
        if apps._is_denylisted(bundle):
            denylist_warning = (" ⚠ This is a sensitive system application. "
                               "Please confirm explicitly.")
        
        return (f"I found {bundle}. Shall I open it? Nothing has been opened yet."
                f"{denylist_warning}")

    def _resolve_pending_launch(self, message: str) -> Optional[str]:
        """Launch a staged discovered bundle iff this user turn confirms it.

        The same contract as :meth:`_resolve_pending_email`: a pending launch
        lives for exactly one user turn and is cleared here whether the user
        confirmed, declined, said something unrelated, or took too long. Returns
        the launch result, or None to let the turn proceed normally.
        """
        pending = self._pending_launch
        self._pending_launch = None
        if pending is None:
            return None
        if _now() - pending.get("staged_at", 0.0) > APP_CONFIRM_TIMEOUT_S:
            logger.info("[APP] pending launch expired; not launching")
            return None
        if not email_tool.parse_confirmation(message):
            logger.info("[APP] pending launch not confirmed; discarded")
            return None
        return apps.launch_bundle(pending["bundle"])

    def _try_local_intent(self, message: str) -> Optional[str]:
        """Deterministic local command, or None to use the AI router.

        Dispatches through the existing TOOL_REGISTRY, so no provider is
        touched on a match. Never raises: doubt means fall-through.
        """
        try:
            from zoe.intents import handle as handle_local_intent
        except Exception as e:  # noqa: BLE001 -- router optional, never fatal
            logger.error(f"Local intent layer unavailable: {e}")
            return None
        result = handle_local_intent(message, self._execute_tool_call)
        return result.response if result.matched else None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def ask(
        self,
        user_text: str,
        images: Optional[List[Dict[str, Any]]] = None,
        *,
        ephemeral: bool = False,
        spoken: bool = False,
    ) -> str:
        """Process one user message, optionally with images.

        The user's text is forwarded verbatim as the latest user turn. Nothing in
        this method rewrites, truncates, or replaces it. Images ride alongside
        the text in the provider-neutral format and are rendered by each
        provider adapter.

        Args:
            user_text: The user's input message. May be empty when images are
                attached.
            images: Optional provider-neutral image parts from
                :func:`zoe.providers.base.image_part`.
            ephemeral: Run this turn outside the conversation. Used by webcam
                perception: the model sees the frame and answers, but the turn
                is not recorded in history and does not carry the user's
                conversation, so camera perception can never appear as something
                the user said, or as a turn they now have to scroll past.
            spoken: True when the reply will be spoken aloud. Adds a short-form,
                no-markdown directive to the system prompt for this turn. Text
                turns leave it False.

        Returns:
            The model's final text response after any tool calls.

        Raises:
            ValueError: If both `user_text` and `images` are empty.
            BrainError: If no configured model can serve the request, including
                when images are attached but no vision-capable model exists.
        """
        images = images or []
        if user_text is None or not user_text.strip():
            if not images:
                raise ValueError("ask() requires a non-empty message or an image.")
            # An image-only turn is legitimate: the picture is the message.
            user_text = ""

        message = user_text.strip()

        if DEBUG_INPUT and not ephemeral:
            logger.info(
                f"[INPUT] session={self.conversation_id} message={message!r} "
                f"images={len(images)}"
            )

        # Serialize turns so rapid messages cannot interleave or reorder.
        # One trace per turn: reuse an enclosing trace (voice loop) or own one.
        trace = current_trace()
        own_trace = trace is None
        if own_trace:
            trace = new_trace()
        with use_trace(trace):
            try:
                with self._lock:
                    self._ephemeral = ephemeral
                    # A staged email draft lives for exactly one user turn. If
                    # the user's next message confirms it, send here -- before any
                    # local intent or model routing -- so the send is driven by
                    # the user, never by a tool call the model issued.
                    if not ephemeral and not images:
                        pending_reply = self._resolve_pending_email(message)
                        if pending_reply is not None:
                            self._trim_history()
                            self._record(
                                {"role": "user", "content": message},
                                {"role": "assistant", "content": pending_reply},
                            )
                            trace.route = "email_confirm"
                            return pending_reply
                    # A discovered application is confirmed the same way, and on
                    # the same turn boundary, as a staged email: the user's own
                    # next message is the only thing that can authorise it.
                    if not ephemeral and not images:
                        launched_reply = self._resolve_pending_launch(message)
                        if launched_reply is not None:
                            self._trim_history()
                            self._record(
                                {"role": "user", "content": message},
                                {"role": "assistant", "content": launched_reply},
                            )
                            trace.route = "app_confirm"
                            return launched_reply
                    # Phase 2: deterministic local intents run before any provider
                    # call. A match executes an existing registry tool with zero LLM
                    # involvement; no match falls through to the AI router unchanged.
                    # Ephemeral (perception) and image turns never match: a camera
                    # frame is an analysis request, not a user command.
                    if not ephemeral and not images:
                        with span_of("intent_route"):
                            local_response = self._try_local_intent(message)
                        if local_response is not None:
                            self._trim_history()
                            self._record(
                                {"role": "user", "content": message},
                                {"role": "assistant", "content": local_response},
                            )
                            trace.route = "local"
                            return local_response
                    # Only an ephemeral turn borrows the turn context. It has to hand it
                    # back, or the conversation's next history rebuild would open a
                    # session carrying a camera directive. A normal turn must NOT be
                    # restored: a later trim rebuilds the session from this turn's
                    # context, so clearing it here left those rebuilds working from a
                    # stale or empty prompt.
                    saved_context = self._turn_context if ephemeral else None
                    try:
                        return self._ask_locked(message, images, ephemeral=ephemeral, spoken=spoken)
                    finally:
                        self._ephemeral = False
                        if ephemeral:
                            self._turn_context = saved_context
            finally:
                self._turn_deadline = None
                # Single finish point: local hits set route above, provider
                # turns are recognized by their request spans. Voice-owned
                # traces are finished by the voice loop, not here.
                if not trace.route:
                    names = {s.name for s in trace.spans}
                    trace.route = ("provider" if "provider_request" in names
                                   else "unserved")
                if own_trace:
                    trace.finish()

    def _ask_locked(
        self,
        message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        *,
        ephemeral: bool = False,
        spoken: bool = False,
    ) -> str:
        images = images or []
        if not ephemeral:
            StatusIndicator.thinking()

        self._trim_history()

        with span_of("classify"):
            classification = self.layer.classify(
                message,
                conversation_tokens=estimate_tokens(self._history),
                tools_available=bool(GEMINI_TOOLS) and not ephemeral,
                # Vision is required if *this* turn carries an image OR an earlier
                # turn in the conversation did. History is replayed to whichever
                # model serves this turn, so a text-only model would be handed the
                # conversation's images too -- which providers reject outright
                # ("No endpoints found that support image input"), failing the turn
                # with a non-retryable error instead of falling back.
                vision_required=bool(images) or self._history_has_images(),
            )
        context_tokens = estimate_tokens(self._history) + estimate_tokens([
            {"content": message, "images": images}
        ])

        # Phase 2: relevant long-term memory plus this turn's behavioral posture.
        # Deterministic, no extra model call -- the classification already
        # exists for model selection.
        #
        # An ephemeral turn (webcam perception) gets neither: it must not read the
        # user's private conversation to describe a room, and must not carry the
        # user's memory into an image analysis.
        self._turn_context = (
            behavior.perception_directive()
            if ephemeral
            else _build_context_block(message, classification, self._history, images)
        )

        ranked = self.layer.plan(classification, context_tokens=context_tokens)

        if DEBUG_INPUT:
            logger.info(
                f"[ROUTING] session={self.conversation_id} task={classification.task_type.value} "
                f"tool_required={classification.tool_required} signals={classification.signals} "
                f"candidates={[c.key for c in ranked] or 'none'}"
            )

        if not ranked:
            raise BrainError(
                _NO_VISION_MESSAGE if images else _NO_MODEL_MESSAGE,
                detail=(
                    "no eligible model. "
                    f"task={classification.task_type.value} "
                    f"tool_required={classification.tool_required} "
                    f"vision_required={classification.vision_required} "
                    f"enabled={[s.key for s in self.layer.registry.enabled()]}"
                ),
            )

        attempts = ranked[: max(1, self.layer.router.config.max_attempts)]
        failures: List[str] = []
        executed_this_turn: Dict[str, str] = {}
        # One budget for the whole turn, not per model. A chain of four slow
        # models must not be able to hold the user for four timeouts.
        self._turn_deadline = _now() + REQUEST_TIMEOUT_S
        deadline = self._turn_deadline

        for attempt, candidate in enumerate(attempts, 1):
            spec = candidate.spec
            provider = self.layer.providers.get(spec.provider)
            if provider is None or not provider.is_configured():
                failures.append(f"{spec.key}: provider not configured")
                continue

            started = _now()

            try:
                # An ephemeral turn starts a clean session: the user's history
                # has no business in a camera analysis, and perception is not a
                # turn of this conversation.
                session = self._create_chat(
                    spec, history=[] if ephemeral else self._history, spoken=spoken
                )
                payload = {"kind": "user", "text": message}
                if images:
                    payload["images"] = images
                with span_of("provider_request", model=spec.key,
                             provider=spec.provider, attempt=attempt):
                    response = session.send_message(payload)
            except ProviderError as e:
                latency = _now() - started
                self.layer.record_failure(spec, e, latency)
                failures.append(f"{spec.key}: {e.kind.value} ({e.safe_detail})")
                if not e.retryable:
                    # Auth / bad request: retrying other models cannot help, and
                    # hiding a misconfiguration behind retries is how outages get
                    # misdiagnosed. Surface it immediately.
                    logger.error(f"[ROUTER] fatal error from {spec.key}: {e.safe_detail}")
                    raise BrainError(_friendly_error_message(e), detail="; ".join(failures)) from e
                self._reset_after_failure()
                continue

            latency = _now() - started
            self.layer.record_success(spec, latency)
            self.last_model_key = spec.key
            # Tool exchanges from a failed provider attempt must not leak into
            # the next candidate's committed conversation history.
            turn_messages: List[Dict[str, Any]] = []

            if DEBUG_INPUT:
                logger.info(f"[MODEL OUTPUT] session={self.conversation_id} model={spec.key} "
                            f"latency={latency:.2f}s")

            try:
                reply = self._run_tool_loop(
                    session=session,
                    spec=spec,
                    response=response,
                    turn_messages=turn_messages,
                    executed_this_turn=executed_this_turn,
                    deadline=deadline,
                )
            except ProviderError as e:
                self.layer.record_failure(spec, e)
                failures.append(f"{spec.key}: {e.kind.value} ({e.safe_detail})")
                self._reset_after_failure()
                if not e.retryable:
                    raise BrainError(_friendly_error_message(e), detail="; ".join(failures)) from e
                continue

            # Commit the turn: user message plus everything the model produced.
            # Images stay with their turn so a mid-conversation model switch
            # still has the visual context the discussion referred to. A
            # text-only turn records exactly what it always did.
            if ephemeral:
                # Perception turns update visual context, not conversation.
                # Nothing is recorded, so nothing to speak about later.
                return reply
            user_turn: Dict[str, Any] = {"role": "user", "content": message}
            if images:
                user_turn["images"] = images
            self._record(user_turn, *turn_messages)
            return reply

        raise BrainError(_friendly_error_message_from_failures(failures), detail="; ".join(failures))

    def _turn_deadline_seconds(self) -> float:
        """Seconds left in this turn's budget, floored so a call still goes out.

        A turn that has already overrun passes 1s: returning 0 would let a
        provider treat "no time" as "no limit", which is the opposite of the
        point.
        """
        if self._turn_deadline is None:
            return REQUEST_TIMEOUT_S
        return max(1.0, self._turn_deadline - _now())

    def _reset_after_failure(self) -> None:
        """Drop the failed session so the next candidate starts clean."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None
        self._model_key = None

    def _run_tool_loop(
        self,
        *,
        session: ChatSession,
        spec: ModelSpec,
        response: ModelResponse,
        turn_messages: List[Dict[str, Any]],
        executed_this_turn: Dict[str, str],
        deadline: float,
    ) -> str:
        """Serve tool requests until the model returns text.

        `turn_messages` accumulates the assistant/tool turns produced so far, so
        they are committed to history even if the turn ultimately fails -- the
        conversation must not lose the fact that a tool ran.
        """
        # What this attempt actually executed, so an empty final model message
        # can report the tool output instead of pretending nothing happened.
        executed: List[Tuple[str, str]] = []

        for round_index in range(MAX_TOOL_ROUNDS):
            if not response.has_tool_calls:
                break

            # Two reasons to stop, told apart because they mean different
            # things to the user: a runaway tool loop is our bug, an expired
            # budget is just a long turn.
            out_of_time = _now() >= deadline
            if round_index == MAX_TOOL_ROUNDS - 1 or out_of_time:
                reason = ("turn time limit reached" if out_of_time
                          else f"tool round limit ({MAX_TOOL_ROUNDS}) reached")
                logger.warning(
                    f"[BRAIN] session={self.conversation_id} {reason}; "
                    f"answering without further tools"
                )
                assistant_calls = [{
                    "id": call.id, "name": call.name,
                    "arguments": dict(call.arguments or {}),
                } for call in response.tool_calls]
                results = [{
                    "id": call.id, "name": call.name,
                    "result": f"Error: {reason}; this tool was not executed.",
                } for call in response.tool_calls]
                turn_messages.extend((
                    {"role": "assistant", "content": response.text or "",
                     "tool_calls": assistant_calls},
                    {"role": "tool", "results": results},
                ))
                executed.extend((call.name, result) for call, result in zip(response.tool_calls, [r["result"] for r in results]))
                break

            results: List[Dict[str, Any]] = []
            assistant_calls: List[Dict[str, Any]] = []

            for call in response.tool_calls:
                tool_name = call.name
                tool_args = dict(call.arguments or {})

                StatusIndicator.tool_call(tool_name, tool_args)

                # Duplicate-execution guard. After a model switch mid-turn, or a
                # retry, the model may re-request a tool it already got a
                # result for -- possibly with different or empty arguments.
                # For side-effecting tools that would duplicate a real-world
                # action, so the cached result is reused instead.
                cache_key = _tool_cache_key(tool_name, tool_args)
                if cache_key in executed_this_turn:
                    result = executed_this_turn[cache_key]
                    logger.info(
                        f"[BRAIN] reusing result for already-executed tool {tool_name}"
                    )
                else:
                    with span_of("tool_execute", tool=tool_name):
                        result = self._execute_tool_call(tool_name, tool_args)
                    executed_this_turn[cache_key] = result

                StatusIndicator.tool_result(tool_name, result)
                executed.append((tool_name, result))

                assistant_calls.append({
                    "id": call.id, "name": tool_name, "arguments": tool_args,
                })
                results.append({"id": call.id, "name": tool_name, "result": result})

            turn_messages.append({
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": assistant_calls,
            })
            turn_messages.append({"role": "tool", "results": results})

            with span_of("provider_request", model=spec.key,
                         provider=spec.provider, followup=True):
                response = session.send_message({"kind": "tool_results", "results": results})

        reply = (response.text or "").strip()
        if reply:
            reply += behavior.tool_failure_note([r for _, r in executed])
        elif executed:
            # The model produced nothing after the tools ran. That is not the
            # same as "nothing happened", so report what the tools returned.
            reply = _report_executed(executed)
        else:
            reply = "Standing by, Boss."

        turn_messages.append({"role": "assistant", "content": reply})
        return reply


# ============================================================================
# Helpers
# ============================================================================

def _report_executed(executed: List[Tuple[str, str]]) -> str:
    """Truthful reply when the model returned no text after tools ran.

    Surfacing the tool's own output is the honest minimum: the action did
    happen, and the user needs its result. Failures stay labelled as failures so
    this can never read as success.
    """
    lines = []
    for name, result in executed:
        text = str(result or "").strip() or "(no output)"
        if text.startswith("Error"):
            lines.append(f"- {name} did not complete: {text}")
        else:
            lines.append(f"- {name}: {text}")
    return "\n".join(lines)


def _tool_cache_key(name: str, args: Dict[str, Any]) -> str:
    """Stable key identifying a tool invocation within one user turn.

    Side-effecting tools collapse to the tool name alone: within a single user
    message the same side effect must happen at most once, regardless of the
    arguments a re-issuing model chooses. Read-only tools keep their arguments
    in the key, so genuinely distinct calls (weather in two cities) still run.
    """
    if name in NON_IDEMPOTENT_TOOLS:
        return name
    try:
        return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name}:{sorted(args.items(), key=lambda kv: str(kv[0]))}"


def _now() -> float:
    import time
    return time.time()


def _friendly_error_message(exc: ProviderError) -> str:
    """Short, honest, user-facing text for a provider failure."""
    kind = getattr(exc, "kind", None)
    value = getattr(kind, "value", str(kind or ""))
    if value == "rate_limit":
        return "The AI provider's usage limit has been reached. Please try again later."
    if value == "server":
        return "The AI model is temporarily unavailable. Please try again shortly."
    if value == "timeout":
        return "The AI model took too long to respond. Please try again."
    if value == "network":
        return "Cannot reach the AI provider. Please check your connection."
    if value == "auth":
        return "The AI provider rejected the configured API key. Please check your .env file."
    if value == "invalid_request":
        return "The request was rejected by the AI provider."
    if value == "unsupported":
        return "The selected AI model does not support this request."
    return "I could not reach any available AI model just now. Please try again."


def _friendly_error_message_from_failures(failures: List[str]) -> str:
    """Summarise why every candidate failed."""
    kinds = set()
    for failure in failures:
        if ":" in failure:
            kinds.add(failure.split(":", 1)[1].strip().split(" ")[0])
    if "rate_limit" in kinds:
        # Every candidate was rate limited, so this is the quota, not one
        # provider: naming a single one would send the user chasing a key that
        # was never the problem.
        return (
            "Every available model is rate limited right now, so the assistant "
            "cannot answer until your provider quota resets. Please try again later."
        )
    if "server" in kinds:
        return "All available AI models are temporarily unavailable. Please try again shortly."
    if "timeout" in kinds:
        return "The AI model took too long to respond. Please try again."
    return "I could not reach any available AI model just now. Please try again."


_NO_MODEL_MESSAGE = (
    "No AI model is currently available to handle that request. Please try again shortly."
)

#: Said plainly rather than answering as though the image had been seen. Silently
#: dropping the picture is the one outcome that must never happen.
_NO_VISION_MESSAGE = (
    "I can't look at images right now — no available model supports vision. "
    "The image has not been processed. Please try again once a vision-capable "
    "model is available, or describe what you need from it."
)


class _UnconfiguredKind:
    """Marker kind for 'provider has no credentials'."""

    value = "invalid_request"
    name = "INVALID_REQUEST"


_UNCONFIGURED_KIND = _UnconfiguredKind()
