"""Safe local application launching (Phase 3).

A config-supplied map is the allowlist: a spoken name resolves to an argument
list, and only that literal argument list ever reaches the OS. The name is
DATA. Anything not in the map -- and anything mapped for a different operating
system -- is refused with a controlled local message.

``subprocess`` is called with an argument list and never with a shell, on any
platform. That is what makes a spoken name unable to become shell syntax: there
is no shell to parse it. On Windows the command is the executable itself, not
``cmd /c start``, precisely so that stays true.

Discovery exists so the allowlist is easy to *extend*, not so it is easy to
*bypass*. When a name is not mapped, :func:`discover` reports what is installed
and nothing is launched. Exactly one match becomes a question and a pending
launch on the Brain, resolved only by the user's own next turn, exactly like a
staged email draft. :func:`launch_bundle` -- the only function that runs a
discovered name -- is called from that one place and nowhere else.

Phase 3 additions:
- Application index built from Info.plist display names (cached at startup)
- Fuzzy matching with difflib (cutoff 0.75) for flexible name matching
- Denylist for sensitive apps (Terminal, System Settings, password managers)
  requiring explicit user confirmation
"""

import difflib
import os
import platform
import plistlib
import subprocess
import threading
import time
from pathlib import Path

from zoe.config import APP_COMMANDS, APP_DISPLAY_NAMES, CONFIG
from zoe.logger import logger

#: Read at call time so tests and config reloads both see the current map.
COMMANDS = APP_COMMANDS
DISPLAY_NAMES = APP_DISPLAY_NAMES

#: Where macOS keeps application bundles. Scanned read-only and depth 1: bundles
#: live directly in these directories, so there is no recursion to bound.
APP_BUNDLE_DIRS = ("/Applications", "/System/Applications", "~/Applications")

#: Cap on how many candidates a discovery question offers. Past this the answer
#: is "be more specific", not a menu nobody reads.
MAX_DISCOVERY_CANDIDATES = 5

#: Fuzzy matching cutoff for difflib. 0.75 balances flexibility with precision.
FUZZY_CUTOFF = 0.75

#: Denylist: apps that require explicit user confirmation even when discovered.
#: Configured in config.yaml under applications.denylist
_APP_DENYLIST: tuple[str, ...] = ()

#: Application index cache: {bundle_name: {"path": ..., "display_name": ..., "bundle_id": ...}}
_APP_INDEX: dict[str, dict] = {}
_APP_INDEX_LOCK = threading.Lock()
_APP_INDEX_BUILT = False
_APP_INDEX_BUILD_TIME: float = 0.0

#: Test override for the application index. Set by tests to avoid filesystem access.
_TEST_APP_INDEX: dict[str, dict] | None = None


#: Sentence punctuation that ends an utterance rather than belonging to a name.
#: Only ever trimmed from the *edges*, so "Safari" keeps its characters and
#: "WhatsApp" keeps its capital P. Never applied to an interior position.
_TRAILING_PUNCTUATION = ".!?,;:…‽。，！？"


def normalize_app_name(name) -> str:
    """Reduce an utterance's app name to its bare form. Whitespace + edges only.

    Speech and text both deliver "Open Safari." with the sentence punctuation
    still attached, so the name is compared without it. Interior characters are
    never touched: stripping arbitrary characters from every argument would make
    "C++" or a path unmatchable, and the lookup is exact by design.
    """
    if not isinstance(name, str):
        return ""
    cleaned = " ".join(name.split())
    cleaned = cleaned.strip(_TRAILING_PUNCTUATION + " \t")
    return cleaned.strip(_TRAILING_PUNCTUATION + " \t")


def _folded(name: str) -> str:
    """Casefolded comparison key, so 'SAFARI' and 'safari' are one name."""
    return normalize_app_name(name).casefold()


def lookup(name: str):
    """The whole per-OS command table for `name`, or None if it is not mapped.

    None means "not authorized": the name is absent from the map, and the OS
    must not be consulted about it. A returned table with no entry for the
    running OS is a different answer -- authorized, but with nothing to run.
    """
    wanted = _folded(name)
    if not wanted or not isinstance(COMMANDS, dict):
        return None
    table = COMMANDS.get(wanted)
    return dict(table) if isinstance(table, dict) else None


def display_name(name: str) -> str:
    """How the assistant should name `name`: the configured spelling."""
    wanted = _folded(name)
    if not wanted:
        return normalize_app_name(name)
    if not isinstance(DISPLAY_NAMES, dict):
        return normalize_app_name(name)
    return DISPLAY_NAMES.get(wanted, normalize_app_name(name))


def resolve_command(name: str):
    """(display name, argv) for `name` on this OS, or None.

    None when the name is unmapped, or mapped without a command for the running
    operating system. Use `lookup` first if you need to tell those apart.
    """
    table = lookup(name)
    if not table:
        return None
    argv = table.get(platform.system())
    if not argv:
        return None
    return display_name(name), list(argv)


def approval_hint(name: str) -> str:
    """How to approve `name`, in the words the map is configured with."""
    requested = normalize_app_name(name)
    if not requested:
        return ""
    return (f"Ask the assistant to open it, or add {requested!r} to "
            "applications.aliases in config.yaml.")


def discovery_hint() -> str:
    """Where to find the applications this machine actually has."""
    return ("Run `python scripts/list_apps.py` to list the applications installed "
            "on this machine, then add the one you want to applications.aliases "
            "in config.yaml.")


def not_approved(name: str) -> str:
    """The refusal for a name that is not in the allowlist.

    Shared by the mapped and discovered paths so both refusals read identically;
    they are the same decision, reached two different ways.
    """
    requested = normalize_app_name(name)
    logger.info(f"App launch rejected (not mapped): {requested!r}")
    return (f"{requested} isn't an approved application, so I won't open it. "
            f"{approval_hint(requested)}")


# ---------------------------------------------------------------------------
# Discovery. Read-only. Nothing here can launch anything.
# ---------------------------------------------------------------------------

def _load_denylist() -> tuple[str, ...]:
    """Load the denylist from config.yaml. Case-insensitive matching."""
    apps_config = CONFIG.get("applications", {}) or {}
    denylist = apps_config.get("denylist", [])
    if not isinstance(denylist, list):
        return ()
    # Normalize to casefolded for matching
    return tuple(str(item).strip().casefold() for item in denylist if str(item).strip())


def _ensure_denylist_loaded() -> None:
    """Ensure the denylist is loaded from config."""
    global _APP_DENYLIST
    if not _APP_DENYLIST:
        _APP_DENYLIST = _load_denylist()


def _read_info_plist(app_path: Path) -> dict:
    """Read and parse Info.plist from an application bundle.
    
    Returns a dict with display_name, bundle_id, and version if available.
    Never raises - returns empty dict on any error.
    """
    try:
        plist_path = app_path / "Contents" / "Info.plist"
        if not plist_path.exists():
            return {}
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        
        # Prefer CFBundleDisplayName, fall back to CFBundleName
        display_name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName")
        bundle_id = plist.get("CFBundleIdentifier")
        version = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
        
        result = {}
        if display_name:
            result["display_name"] = str(display_name)
        if bundle_id:
            result["bundle_id"] = str(bundle_id)
        if version:
            result["version"] = str(version)
        return result
    except Exception as e:
        logger.debug(f"Failed to read Info.plist for {app_path}: {e}")
        return {}


def _build_app_index(force_refresh: bool = False) -> dict[str, dict]:
    """Build the application index from macOS application directories.
    
    Scans APP_BUNDLE_DIRS for .app bundles, reads their Info.plist for
    display names, and returns a cached index. Thread-safe.
    
    The index maps casefolded bundle name -> {
        "path": Path to .app bundle,
        "display_name": Name from Info.plist (or bundle name),
        "bundle_id": Bundle identifier from Info.plist,
        "bundle_name": The filesystem bundle name (without .app)
    }
    
    Cache is refreshed after 1 hour or when force_refresh=True.
    """
    global _APP_INDEX, _APP_INDEX_BUILT, _APP_INDEX_BUILD_TIME
    
    with _APP_INDEX_LOCK:
        now = time.monotonic()
        # Cache for 1 hour (3600 seconds)
        if _APP_INDEX_BUILT and not force_refresh and (now - _APP_INDEX_BUILD_TIME) < 3600:
            return _APP_INDEX
        
        index: dict[str, dict] = {}
        if platform.system() != "Darwin":
            _APP_INDEX = index
            _APP_INDEX_BUILT = True
            _APP_INDEX_BUILD_TIME = now
            return index
        
        for raw in APP_BUNDLE_DIRS:
            path = Path(raw).expanduser()
            try:
                entries = list(path.iterdir())
            except (FileNotFoundError, NotADirectoryError):
                logger.debug(f"App bundle directory absent, skipping: {path}")
                continue
            except PermissionError:
                logger.warning(f"App bundle directory not readable, skipping: {path}")
                continue
            except OSError as e:
                logger.warning(f"App bundle directory unreadable ({e}), skipping: {path}")
                continue
            
            for entry in entries:
                if not entry.name.endswith(".app"):
                    continue
                if not entry.is_dir():
                    continue
                
                bundle_name = entry.name[:-len(".app")]
                if not bundle_name:
                    continue
                
                key = bundle_name.casefold()
                # First occurrence wins (directories scanned in order)
                if key in index:
                    continue
                
                info = _read_info_plist(entry)
                display_name = info.get("display_name", bundle_name)
                
                index[key] = {
                    "path": entry,
                    "display_name": display_name,
                    "bundle_id": info.get("bundle_id"),
                    "bundle_name": bundle_name,
                }
        
        _APP_INDEX = index
        _APP_INDEX_BUILT = True
        _APP_INDEX_BUILD_TIME = now
        logger.info(f"Built application index with {len(index)} bundles")
        return index


def _get_app_index() -> dict[str, dict]:
    """Get the application index, building it if necessary.
    
    Tests can override by setting _TEST_APP_INDEX.
    """
    if _TEST_APP_INDEX is not None:
        return _TEST_APP_INDEX
    return _build_app_index()


def installed_apps(directories=None) -> list:
    """Names of the application bundles installed on this machine, sorted.

    Bundle *names*, with the ``.app`` suffix removed -- the name the user says and
    the name `open -a` wants. Duplicates across directories are collapsed, first
    spelling wins.

    Returns [] on any platform but macOS: there is no bundle format to look for.
    A directory that is missing or unreadable is skipped with a log line, never
    an exception, because this runs inside a user turn.
    """
    index = _get_app_index()
    # Return sorted by display name for user-facing lists
    return sorted((info["bundle_name"] for info in index.values()), key=str.casefold)


def discover(name: str, available=None) -> list:
    """Installed bundles matching `name`. Empty means "nothing found".

    Matching priority:
    1. Exact case-insensitive match on bundle name or display name
    2. Prefix match on bundle name or display name
    3. Fuzzy match (difflib) with cutoff FUZZY_CUTOFF (0.75) on bundle name or display name

    Exact matches win outright. Only when there is no exact match does it fall
    back to prefix, then fuzzy matching.

    This is a lookup and nothing more. It never launches, and it never consults
    the allowlist -- that decision belongs to the caller.
    """
    wanted = _folded(name)
    if not wanted:
        return []
    
    index = _get_app_index() if available is None else None
    
    if index is None:
        # Fallback for tests that pass their own available list
        names = installed_apps() if available is None else list(available)
        exact = [n for n in names if _folded(n) == wanted]
        if exact:
            return sorted(exact, key=str.casefold)
        return sorted((n for n in names if _folded(n).startswith(wanted)),
                      key=str.casefold)
    
    # Build searchable lists: bundle names and display names
    bundle_names = [info["bundle_name"] for info in index.values()]
    display_names = [info["display_name"] for info in index.values()]
    
    # 1. Exact match on bundle name or display name
    exact_bundle = [n for n in bundle_names if _folded(n) == wanted]
    exact_display = [info["bundle_name"] for info in index.values() 
                     if _folded(info["display_name"]) == wanted]
    exact = sorted(set(exact_bundle + exact_display), key=str.casefold)
    if exact:
        return exact
    
    # 2. Prefix match on bundle name or display name
    prefix_bundle = [n for n in bundle_names if _folded(n).startswith(wanted)]
    prefix_display = [info["bundle_name"] for info in index.values() 
                      if _folded(info["display_name"]).startswith(wanted)]
    prefix = sorted(set(prefix_bundle + prefix_display), key=str.casefold)
    if prefix:
        return prefix
    
    # 3. Fuzzy match using difflib
    all_names = bundle_names + display_names
    fuzzy_matches = difflib.get_close_matches(wanted, [n.casefold() for n in all_names], 
                                               n=MAX_DISCOVERY_CANDIDATES, cutoff=FUZZY_CUTOFF)
    # Map back to bundle names
    fuzzy_bundles = set()
    for match in fuzzy_matches:
        # Find which bundle this match corresponds to
        for info in index.values():
            if info["bundle_name"].casefold() == match or info["display_name"].casefold() == match:
                fuzzy_bundles.add(info["bundle_name"])
                break
    
    return sorted(fuzzy_bundles, key=str.casefold)


# ---------------------------------------------------------------------------
# Launching. Mapped names only, except for the one confirmed discovery path.
# ---------------------------------------------------------------------------

def _is_denylisted(name: str) -> bool:
    """Check if an application name is in the denylist (case-insensitive)."""
    _ensure_denylist_loaded()
    folded = _folded(name)
    return folded in _APP_DENYLIST


def _get_bundle_path(bundle_name: str) -> Path | None:
    """Get the full path to an application bundle from the index."""
    index = _get_app_index()
    key = bundle_name.casefold()
    info = index.get(key)
    if info:
        return info["path"]
    return None


def launch_bundle(bundle: str) -> str:
    """Run a *confirmed* discovered bundle. The only path discovery may reach.

    This exists as a separate function so the boundary is visible: the model
    cannot call it, because it is not in TOOL_REGISTRY. The only caller is
    `ZoeBrain._resolve_pending_launch`, after the user's own next turn has
    said yes. The command is fixed here and is never assembled from the
    request: the bundle name is one argv element, there is no shell, and the
    prefix `["open", "-a"]` is a literal.
    """
    name = normalize_app_name(bundle)
    if not name:
        return "I need an application name to open."

    # Use the indexed path if available for more reliable launching
    bundle_path = _get_bundle_path(name)
    if bundle_path:
        argv = ["open", "-a", str(bundle_path)]
    else:
        argv = ["open", "-a", name]
    
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        logger.error(f"App launch failed: {argv[0]!r} not found.")
        return f"{name} isn't installed on this system, so I couldn't open it."
    except subprocess.TimeoutExpired:
        logger.error(f"App launch timed out: {name!r}")
        return f"Opening {name} timed out."
    except Exception as e:  # noqa: BLE001 -- controlled local failure, logged
        logger.error(f"App launch failed for {name!r}: {e}", exc_info=True)
        return f"I couldn't open {name}."

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        logger.error(f"App launch failed for {name!r}: rc={completed.returncode} {detail}")
        return f"{name} isn't installed on this system, so I couldn't open it."

    logger.info(f"Confirmed app launch succeeded: {name!r} argv={list(argv)}")
    return f"{name} is now open."


def launch_app(app_name: str) -> str:
    """Launch a mapped application. Never raises to the caller.

    Returns text and stays silent: the conversation layer speaks the reply.
    """
    requested = normalize_app_name(app_name)
    if not requested:
        return "I need an application name to open. For example: Open Safari."

    system = platform.system()
    table = lookup(requested)
    if table is None:
        return not_approved(requested)

    name = display_name(requested)
    argv = table.get(system)
    if not argv:
        logger.info(f"App launch unavailable on {system}: {name!r}")
        return (f"I don't have a way to open {name} on {system}, so I haven't "
                f"opened anything. {approval_hint(requested)}")

    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        logger.error(f"App launch failed: {argv[0]!r} not found.")
        return f"{name} isn't installed on this system, so I couldn't open it."
    except subprocess.TimeoutExpired:
        logger.error(f"App launch timed out: {name!r}")
        return f"Opening {name} timed out."
    except Exception as e:  # noqa: BLE001 -- controlled local failure, logged
        logger.error(f"App launch failed for {name!r}: {e}", exc_info=True)
        return f"I couldn't open {name}."

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        logger.error(f"App launch failed for {name!r}: rc={completed.returncode} {detail}")
        return f"{name} isn't installed on this system, so I couldn't open it."

    logger.info(f"App launch succeeded: {name!r} argv={list(argv)}")
    return f"{name} is now open."
