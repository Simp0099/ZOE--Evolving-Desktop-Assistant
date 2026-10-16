"""Plugin system: drop one `.py` file into `plugins/` and it becomes a tool.

Contract for a plugin file (see plugins/_template.py):

    TOOL = {
        "name": "word_count",                  # snake_case, unique, not built-in
        "description": "Count words in text.", # what the model reads to decide
        "parameters": {"text": "str"},         # name -> "str" | "int" | "float" | "bool"
    }

    def run(text: str) -> str:                 # every parameter is required
        return f"{len(text.split())} words"

Guarantees:
  * A plugin that fails to import, declares a bad TOOL dict, or collides with a
    built-in or another plugin is REJECTED with a logged reason. It never stops
    other plugins or the assistant from starting.
  * A plugin that raises, or runs longer than the timeout, returns a plain
    sentence to the model instead of crashing the turn.
  * Files starting with "_" are templates and are never loaded.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from zoe.logger import logger

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TYPES = {"str": str, "int": int, "float": float, "bool": bool}
_MAX_PARAMS = 8
_MAX_DESCRIPTION = 500
_MAX_RESULT_CHARS = 4000


def _result_cap() -> int:
    """Ceiling on what one plugin result may cost the model.

    Overridable via `plugins.max_result_chars` because some plugins legitimately
    return a document rather than a sentence; the default stays tight so an
    accidental megabyte cannot reach the model.
    """
    from zoe.config import CONFIG
    cfg = CONFIG.get("plugins")
    try:
        return int(cfg.get("max_result_chars", _MAX_RESULT_CHARS)) if isinstance(cfg, dict) \
            else _MAX_RESULT_CHARS
    except (TypeError, ValueError):
        return _MAX_RESULT_CHARS


@dataclass
class PluginRecord:
    file: str
    name: str = ""
    valid: bool = False
    error: str = ""
    run: Optional[Callable] = None
    description: str = ""
    parameters: dict = field(default_factory=dict)


def _validate(spec, run, reserved: set, taken: set) -> str:
    """Return '' when the declaration is acceptable, else the reason it is not."""
    if not isinstance(spec, dict):
        return "TOOL must be a dict"
    name = spec.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return "name must be snake_case, 2-48 chars, starting with a letter"
    if name in reserved:
        return f"name '{name}' is a built-in tool"
    if name in taken:
        return f"name '{name}' is already provided by another plugin"
    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        return "description must be a non-empty string"
    if len(description) > _MAX_DESCRIPTION:
        return f"description longer than {_MAX_DESCRIPTION} characters"
    params = spec.get("parameters", {})
    if not isinstance(params, dict):
        return "parameters must be a dict of name -> type"
    if len(params) > _MAX_PARAMS:
        return f"more than {_MAX_PARAMS} parameters"
    for pname, ptype in params.items():
        if not isinstance(pname, str) or not _PARAM_RE.match(pname):
            return f"parameter name '{pname}' is not snake_case"
        if ptype not in _TYPES:
            return f"parameter '{pname}' has unsupported type '{ptype}'"
    if not callable(run):
        return "run must be a function"
    return ""


def _load_file(path: Path) -> tuple:
    """Execute one plugin file in isolation. Returns (TOOL dict, run) or raises."""
    module_name = f"zoe_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("not a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "TOOL"):
        raise ValueError("no TOOL declaration")
    return module.TOOL, getattr(module, "run", None)


def _discover(plugin_dir: Path, reserved: set) -> List[PluginRecord]:
    records: List[PluginRecord] = []
    taken: set = set()
    if not plugin_dir.is_dir():
        return records
    for path in sorted(plugin_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        rec = PluginRecord(file=path.name)
        try:
            spec, run = _load_file(path)
        except BaseException as e:  # noqa: BLE001 -- a plugin may call sys.exit
            if isinstance(e, KeyboardInterrupt):
                raise
            rec.error = f"failed to import: {type(e).__name__}: {e}"
            records.append(rec)
            logger.warning(f"[PLUGIN] {path.name} rejected: {rec.error}")
            continue
        reason = _validate(spec, run, reserved, taken)
        if reason:
            rec.error = reason
            # Keep the claimed name on a rejected record. `describe` is the only
            # way to see why a file was skipped, and "some plugin called
            # send_email" is not actionable without knowing which file said so.
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                rec.name = spec["name"]
            records.append(rec)
            logger.warning(f"[PLUGIN] {path.name} rejected: {reason}")
            continue
        rec.name = spec["name"]
        rec.description = spec["description"].strip()
        rec.parameters = dict(spec.get("parameters", {}))
        rec.run = run
        rec.valid = True
        taken.add(rec.name)
        records.append(rec)
        logger.info(f"[PLUGIN] loaded '{rec.name}' from {path.name}")
    return records


def _coerce(value, type_name: str):
    target = _TYPES[type_name]
    if isinstance(value, target) and not (target is int and isinstance(value, bool)):
        return value
    if target is bool and isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return target(value)


def _make_callable(rec: PluginRecord, timeout_s: float) -> Callable:
    params = [
        inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                          annotation=_TYPES[ptype])
        for pname, ptype in rec.parameters.items()
    ]
    run = rec.run

    def tool(**kwargs) -> str:
        try:
            args = {k: _coerce(kwargs[k], rec.parameters[k]) for k in rec.parameters}
        except (KeyError, TypeError, ValueError) as e:
            return f"Plugin '{rec.name}' got bad arguments: {e}"
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(run, **args)
        try:
            result = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            logger.error(f"[PLUGIN] '{rec.name}' exceeded {timeout_s:g}s; abandoned")
            # %g, not %.0f: a sub-second cap used to be reported to the model as
            # "longer than 0 seconds", which is both wrong and unactionable.
            return f"Plugin '{rec.name}' took longer than {timeout_s:g} seconds and was stopped."
        except Exception as e:  # noqa: BLE001 -- plugin code is untrusted
            logger.error(f"[PLUGIN] '{rec.name}' raised: {e}", exc_info=True)
            return f"Plugin '{rec.name}' failed: {e}"
        finally:
            pool.shutdown(wait=False)
        text = str(result if result is not None else "")
        return text[:_result_cap()]

    tool.__name__ = rec.name
    tool.__qualname__ = rec.name
    tool.__doc__ = rec.description
    tool.__signature__ = inspect.Signature(params, return_annotation=str)
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return tool


def load_callables(plugin_dir: Optional[Path] = None,
                   reserved: Iterable[str] = (),
                   timeout_s: Optional[float] = None) -> List[Callable]:
    """Discover plugins and return model-callable functions for the valid ones."""
    if plugin_dir is None:
        from zoe.config import PROJECT_ROOT
        plugin_dir = PROJECT_ROOT / "plugins"
    if timeout_s is None:
        from zoe.config import CONFIG
        timeout_s = float((CONFIG.get("plugins") or {}).get("timeout_s", 10))
    records = _discover(Path(plugin_dir), set(reserved))
    return [_make_callable(r, timeout_s) for r in records if r.valid]


def describe(plugin_dir: Optional[Path] = None,
             reserved: Optional[Iterable[str]] = None) -> List[PluginRecord]:
    """Every plugin file and whether it loaded, for `--status` and the docs.

    `reserved` defaults to the built-in tool names, snapshotted before plugins
    are appended. Without it a plugin named after a built-in was reported valid
    here while the actual loader rejected it, so the diagnostic and reality could
    disagree. Pass an explicit set to check collisions against something else.
    """
    if plugin_dir is None:
        from zoe.config import PROJECT_ROOT
        plugin_dir = PROJECT_ROOT / "plugins"
    if reserved is None:
        from zoe.brain import BUILTIN_TOOL_NAMES
        reserved = BUILTIN_TOOL_NAMES
    return _discover(Path(plugin_dir), set(reserved))
