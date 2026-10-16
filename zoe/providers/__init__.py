"""Provider factory: builds one ModelProvider per enabled entry in config.yaml.

Reconstructed during the Zoe repair: the original `providers/__init__.py`
was missing from the uploaded archive, so `from zoe.providers import
build_provider` failed and the whole model layer could not import.

Contract (pinned by tests/test_providers.py::TestProviderFactory):
  build_provider(name, settings, env) -> ModelProvider | None
  * returns None for an unknown provider `type` (routing skips it);
  * always returns a provider for a known type, even when its key is absent;
    `is_configured()` is what reports the missing credential.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping, Optional

from zoe.providers.base import Capabilities, ModelProvider
from zoe.providers.gemini import GeminiProvider
from zoe.providers.openai_compat import OpenAICompatProvider

_CAPABILITY_FIELDS = {f.name for f in dataclasses.fields(Capabilities)}


def _capabilities_from(raw) -> Optional[Capabilities]:
    if not isinstance(raw, Mapping):
        return None
    # Keep only keys the dataclass knows; an unknown key in config must not
    # crash provider construction.
    return Capabilities(**{k: v for k, v in raw.items() if k in _CAPABILITY_FIELDS})


def build_provider(name: str, settings: Mapping, env: Mapping) -> Optional[ModelProvider]:
    """Construct a provider from its config block; None for an unknown type.

    Reconstructed; verify against original.
    """
    kind = str(settings.get("type", "")).strip().lower()
    key_env = settings.get("api_key_env", "")
    api_key = env.get(key_env, "") if key_env else ""
    context_window = int(settings.get("context_window", 128_000) or 128_000)

    if kind == "gemini":
        return GeminiProvider(
            api_key=api_key,
            default_model=str(settings.get("default_model", "")),
            context_window=context_window,
        )
    if kind == "openai_compatible":
        return OpenAICompatProvider(
            name=name,
            api_key=api_key,
            base_url=str(settings.get("base_url", "")),
            context_window=context_window,
            capabilities=_capabilities_from(settings.get("capabilities")),
            headers=settings.get("headers") or None,
        )
    return None
