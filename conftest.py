"""Root pytest configuration.

Shared hooks live here rather than only in ``tests/conftest.py`` for one reason:
a conftest at the repository root is loaded for every test under ``tests/``
regardless of how the suite is invoked, so these hooks cannot be bypassed by
pointing pytest at a subdirectory.

It provides:

* the project root on ``sys.path``, so ``import zoe`` works from any cwd;
* dummy provider keys for the routing tests, set only when nothing real is
  present so a developer's genuine keys are never overwritten;
* the ``requires_kokoro`` skip, so tests that exercise the real Kokoro engine
  report a skip on a machine without it rather than a bare ``ImportError``.
"""
import importlib.util
import os as _os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
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

    ``find_spec`` rather than a real ``import``: locating the spec takes
    microseconds and never pulls in torch, which is the whole point of marking
    these tests optional in the first place.
    """
    try:
        return importlib.util.find_spec("kokoro") is not None
    except (ImportError, ValueError):
        return False


def pytest_collection_modifyitems(config, items):
    """Skip ``requires_kokoro`` tests when Kokoro is not installed.

    A collection-time decision, so the skip is reported in the normal summary
    with its reason rather than surfacing as 17 failures deep in the run.
    """
    if _kokoro_installed():
        return
    skip = pytest.mark.skip(reason=KOKORO_SKIP_REASON)
    for item in items:
        if "requires_kokoro" in item.keywords:
            item.add_marker(skip)