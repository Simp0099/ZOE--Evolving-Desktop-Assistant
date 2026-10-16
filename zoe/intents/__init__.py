"""Deterministic local commands that bypass the AI router.

Reconstructed during the Zoe repair: the original `intents/__init__.py` was
missing from the archive. Public contract (pinned by tests/test_phase2_intents.py
and tests/test_phase3_applaunch.py):

  match_only(text)            -> intent name ("time", "weather", ...) or None
  handle(text, dispatch)      -> IntentResult(matched, response)

Doubt means fall-through: an unmatched or failed local command returns
matched=False so the AI router answers instead. Never raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from zoe.intents.patterns import match as _match
from zoe.logger import logger


@dataclass(frozen=True)
class IntentResult:
    matched: bool
    response: Optional[str] = None
    intent: Optional[str] = None


def match_only(text: str) -> Optional[str]:
    """Name of the local intent `text` maps to, without running it.

    Reconstructed; verify against original.
    """
    try:
        found = _match(text)
    except Exception as e:  # noqa: BLE001 -- matching must never break a turn
        logger.error(f"intent match failed: {e}")
        return None
    return found.intent if found else None


def handle(text: str, dispatch: Callable[[str, dict], str]) -> IntentResult:
    """Run the local handler for `text` if one matches; otherwise fall through.

    Reconstructed; verify against original.
    """
    from zoe.intents.local_intents import handle_intent

    try:
        found = _match(text)
    except Exception as e:  # noqa: BLE001
        logger.error(f"intent match failed: {e}")
        return IntentResult(matched=False)
    if not found:
        return IntentResult(matched=False)
    try:
        response = handle_intent(found.intent, found.args or {}, dispatch)
    except Exception as e:  # noqa: BLE001
        # A handler failure must not strand the user in silence; the router
        # gets the turn instead.
        logger.error(f"local intent '{found.intent}' failed: {e}")
        return IntentResult(matched=False, intent=found.intent)
    logger.debug(f"local intent '{found.intent}' handled; AI router skipped")
    return IntentResult(matched=True, response=response, intent=found.intent)
