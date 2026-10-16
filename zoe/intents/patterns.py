"""Deterministic command patterns for local-first intent routing.

Pure matching only: no LLM, no network, no provider, no disk. Every matcher
is anchored to the whole utterance so a keyword inside a longer request can
never trigger a local command ("explain why the weather affects aircraft"
is not a weather lookup). Precision over recall: doubt means no match and
the request falls through to the existing AI router.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class IntentMatch:
    """A recognized local command with extracted arguments."""

    intent: str
    args: Dict[str, str] = field(default_factory=dict)


def normalize(text: str) -> str:
    """Normalize for matching without destroying handler arguments."""
    cleaned = " ".join((text or "").strip().split())
    # The project was renamed Jarvis -> Zoe, but a wake address is muscle memory:
    # people already say "Jarvis" out of habit. Strip both for the transition.
    # The old spellings are deliberate, not leftovers -- remove them only once
    # the wake word itself has changed (see docs/wake_phrases.md).
    cleaned = re.sub(r"^(hey (?:jarvis|zoe)|(?:jarvis|zoe)|ok (?:jarvis|zoe))[,\s]+",
                     "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _bare(text: str) -> str:
    """Lowercased command stripped of trailing punctuation for shape matching."""
    return re.sub(r"[?!.\u2019']+$", "", text.strip().lower()).strip()


_TIME = re.compile(
    r"^(what time is it|what'?s the time|tell me the time|current time|"
    r"the time please|time please|do you have the time)\??$"
)


def match_time(text: str) -> Optional[IntentMatch]:
    if _TIME.match(_bare(text)):
        return IntentMatch(intent="time")
    return None


_DEFINE = re.compile(
    r"^(define|definition of|meaning of)\s+(?P<word>[a-zA-Z][a-zA-Z\-]{0,39})$"
)
_MEAN = re.compile(
    r"^what do(?:es)?\s+(?P<word>[a-zA-Z][a-zA-Z\-]{0,39})\s+mean$"
)
_MEANING_OF = re.compile(
    r"^what(?:'?s| is) the meaning of\s+(?P<word>[a-zA-Z][a-zA-Z\-]{0,39})$"
)


def match_dictionary(text: str) -> Optional[IntentMatch]:
    for pattern in (_DEFINE, _MEAN, _MEANING_OF):
        found = pattern.match(_bare(text))
        if found:
            return IntentMatch(intent="dictionary",
                               args={"word": found.group("word").lower()})
    return None


_CITY = r"(?P<city>[a-zA-Z][a-zA-Z .\-]{0,47}[a-zA-Z])"
_WEATHER_TODAY = re.compile(
    r"^(what(?:'?s| is) the weather|how(?:'?s| is) the weather)"
    r"( today| right now)?$"
)
_WEATHER_IN = re.compile(
    rf"^(?:weather|what(?:'?s| is) the weather|how(?:'?s| is) the weather)"
    rf" in {_CITY}$",
    re.IGNORECASE,
)


def match_weather(text: str) -> Optional[IntentMatch]:
    cleaned = _bare(text)
    if cleaned == "weather" or _WEATHER_TODAY.match(cleaned):
        return IntentMatch(intent="weather")
    # Shape matched case-insensitively; extract the city from the
    # case-preserving normalization so "Mumbai" stays "Mumbai".
    found = _WEATHER_IN.match(normalize(text).rstrip("?!."))
    if found:
        return IntentMatch(intent="weather",
                           args={"city": " ".join(found.group("city").split())})
    return None


_SCREENSHOT = re.compile(
    r"^(take a screenshot( of (the|my) screen)?|capture( my)? screen|"
    r"screenshot( my screen)?)\??$"
)


def match_screenshot(text: str) -> Optional[IntentMatch]:
    if _SCREENSHOT.match(_bare(text)):
        return IntentMatch(intent="screenshot")
    return None


#: "open"/"launch" name an application unambiguously. "start" does not: "start a
#: conversation with me" is a person asking to talk, not an application. It only
#: opens an application when the rest of the utterance looks like a name.
_LAUNCH = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(open|launch|start)\s+(?P<app>.{1,80}?)\s*[.!?,]?$",
    re.IGNORECASE,
)

#: Politeness wrappers a person puts around the actual command. Stripped only
#: from the ends, and only when something remains.
_POLITE_PREFIX = re.compile(
    r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?", re.IGNORECASE
)
_POLITE_SUFFIX = re.compile(r"[\s,]*please[.!?,]?\s*$", re.IGNORECASE)

#: Function words. An application name is a noun phrase, so a determiner,
#: preposition or pronoun in the target means the rest of the utterance is a
#: sentence, not a name. Structural rather than a phrase list: "start a
#: conversation with me" and "start working on the report" are both rejected
#: for the same reason, and neither needed to be listed.
_FUNCTION_WORDS = re.compile(
    r"\b(?:a|an|the|my|me|to|for|with|from|about|into|onto|up|down|off|out|"
    r"now|then|please|us|them|it|its|this|that|these|those|and|or|but|if|"
    r"so|because|is|are|was|were|be|been|being|do|does|did|can|could|"
    r"will|would|should|let)\b",
    re.IGNORECASE,
)

#: A name is short: "Visual Studio Code" fits, a sentence does not.
_MAX_NAME_WORDS = 4


def _strip_politeness(text: str) -> str:
    without_prefix = _POLITE_PREFIX.sub("", text.strip(), count=1)
    return _POLITE_SUFFIX.sub("", without_prefix).strip()


def _names_an_application(target: str) -> bool:
    """Is `target` a bare application name rather than the rest of a sentence?

    Checked against the raw target, article included: "start the music" carries a
    determiner and is a sentence, while "start Safari" is a name. Determiners and
    prepositions are what separate the two, so listing them is structural rather
    than guessing at every conversational phrasing.
    """
    if _FUNCTION_WORDS.search(target):
        return False
    return 0 < len(target.split()) <= _MAX_NAME_WORDS


def match_launch_app(text: str) -> Optional[IntentMatch]:
    """Launch command with a free-text app name. Safety lives in the
    allowlist, not here: anything not allowlisted is rejected locally.

    Precision over recall. An ambiguous target is not a guess -- it falls through
    to the AI router, which can ask the user to clarify. "Start a conversation
    with me" must never become an application named "a conversation with me".

    Only ``start`` is ambiguous enough to need the name check: it is also how
    people ask for a conversation, a song or a work task. ``open`` and ``launch``
    name an application in every ordinary use, so they go straight to the
    allowlist, which rejects an unapproved one locally and specifically rather
    than guessing at which application was meant.
    """
    found = _LAUNCH.match(_strip_politeness(text))
    if not found:
        return None
    app = found.group("app").strip(" .!?,;:")
    if not app or len(app) > 80:
        return None
    if found.group(1).lower() == "start" and not _names_an_application(app):
        return None
    return IntentMatch(intent="launch_app", args={"app_name": app})


_PLAY = re.compile(r"^play\s+(?P<song>.{3,100})$")
# Bare generics carry no track: let the LLM path ask which song, as today.
_GENERIC_SONGS = {"music", "song", "songs", "something", "anything", "it"}


def match_media(text: str) -> Optional[IntentMatch]:
    found = _PLAY.match(text.strip())
    if not found:
        return None
    song = " ".join(found.group("song").strip().split())
    if len(song) < 3 or song.lower().strip("?!.'") in _GENERIC_SONGS:
        return None
    return IntentMatch(intent="media_play", args={"song": song})


# NOTE: volume is intentionally absent. Phase 0 verified the repository has
# no existing volume tool, and inventing OS-level implementations here is out
# of scope. Such utterances fall through to the existing AI router.
_MATCHERS = (match_time, match_dictionary, match_weather, match_screenshot,
             match_launch_app, match_media)


def match(text: str) -> Optional[IntentMatch]:
    """First matching local intent for `text`, or None (fall through)."""
    cleaned = normalize(text)
    if not cleaned:
        return None
    for matcher in _MATCHERS:
        found = matcher(cleaned)
        if found:
            return found
    return None
