"""Shared pytest configuration.

Two jobs:

* make the project root importable so ``import zoe`` works from any cwd;
* skip the tests that exercise the real Kokoro engine when it is not installed.

The Kokoro skip is a collection-time decision, not an import-time one: the engine
is a heavy optional dependency, so ``import kokoro`` is attempted here, once,
and the ``requires_kokoro`` marker is applied to matching tests when it fails.
Those tests would otherwise fail with a bare ImportError on a machine that
simply never installed an optional TTS engine.
"""
import importlib.util
import os as _os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Routing tests need at least one provider to look configured; they stub the
# network themselves. Dummy values are set only when nothing real is present,
# so a developer's genuine keys are never overwritten.
for _name in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_ZEN_API_KEY"):
    _os.environ.setdefault(_name, "test-dummy-key")


#: Reason shown for every test skipped because the Kokoro engine is absent.
KOKORO_SKIP_REASON = "kokoro not installed; see docs/SETUP.md"


def _kokoro_installed() -> bool:
    """True when the Kokoro package can be imported.

    ``find_spec`` is used rather than a real ``import`` because finding the
    spec is microseconds and never pulls in torch.
    """
    try:
        return importlib.util.find_spec("kokoro") is not None
    except (ImportError, ValueError):
        return False


def pytest_collection_modifyitems(config, items):
    """Skip ``requires_kokoro`` tests when Kokoro is not installed."""
    if _kokoro_installed():
        return
    skip = pytest.mark.skip(reason=KOKORO_SKIP_REASON)
    for item in items:
        if "requires_kokoro" in item.keywords:
            item.add_marker(skip)