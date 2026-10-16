"""Action safety: no action tool may accept a confirmation flag from the model.

A tool that performs an external action -- sending email, launching an app,
writing a file -- must run only because the core confirmation path allowed it,
never because the model passed a flag. The confirmation itself is the user's
"yes" on their own turn, resolved in the Brain; it is never a parameter the
model can set.

This enumerates every registered tool's JSON schema (built the same way the
model sees it) and fails if any parameter name looks like a confirmation or
override switch.
"""
import re

import pytest

from zoe.brain import TOOL_REGISTRY
from zoe.providers.openai_compat import _function_to_schema

#: Parameter names that would let the model bypass the confirmation path.
#: Matched as a substring so "confirmed", "is_confirmed", "force_send" and
#: friends are all caught.
FORBIDDEN = re.compile(r"confirm|approved?|force|yes", re.IGNORECASE)


def _tool_schemas():
    """(name, json schema) for every registered tool that produces one."""
    out = []
    for name, tool in sorted(TOOL_REGISTRY.items()):
        schema = _function_to_schema(tool)
        if schema is not None:
            out.append((name, schema))
    return out


SCHEMAS = _tool_schemas()


def test_registry_is_not_empty():
    """Guard against the enumeration silently seeing nothing."""
    assert SCHEMAS, "no tool schemas were produced -- the check proves nothing"


@pytest.mark.parametrize("name,schema", SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_no_tool_exposes_a_confirmation_flag(name, schema):
    properties = schema["function"]["parameters"].get("properties", {})
    offenders = [p for p in properties if FORBIDDEN.search(p)]
    assert not offenders, (
        f"tool '{name}' exposes {offenders}; a confirmation flag would let the "
        f"model bypass the core confirmation path"
    )


def test_the_forbidden_pattern_catches_real_flags():
    """The regex is not vacuous -- it must match the names it exists to catch."""
    for bad in ("confirmed", "is_confirmed", "approve", "approved", "force", "yes"):
        assert FORBIDDEN.search(bad), f"'{bad}' should be forbidden"
    for good in ("to_address", "subject", "message", "path", "query", "app_name"):
        assert not FORBIDDEN.search(good), f"'{good}' should be allowed"
