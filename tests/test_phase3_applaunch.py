"""Phase 3 tests: safe, map-authorized application launching.

Providers are boom-mocked: ANY provider contact raises AssertionError, so
every test below proves the local path. subprocess is mocked, so the real
operating system is never asked to launch anything.

There is no macOS gate. The config supplies the command per operating system,
so these tests monkeypatch ``platform.system()`` and assert the argument list
for each of Darwin, Windows and Linux on every machine that runs them.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import zoe.config as config_module  # noqa: E402
from zoe.brain import TOOL_REGISTRY, ZoeBrain  # noqa: E402
import zoe.brain as brain_module  # noqa: E402
from zoe.intents.patterns import match as match_intent  # noqa: E402
from zoe.intents.patterns import match_launch_app  # noqa: E402
from zoe.logger import configure_logging  # noqa: E402
from zoe.tools import apps  # noqa: E402
from tests.mock_providers import build_layer, reply  # noqa: E402

#: The three systems a mapping may declare. Every per-OS test runs for each.
SYSTEMS = ("Darwin", "Windows", "Linux")

#: A two-application map covering all three systems. `Slack` deliberately has no
#: Windows command, so "mapped but not here" is exercised by the data itself.
COMMANDS = {
    "safari": {
        "Darwin": ["open", "-a", "Safari"],
        "Windows": ["C:\\Program Files\\Safari\\Safari.exe"],
        "Linux": ["safari"],
    },
    "whatsapp": {
        "Darwin": ["open", "-a", "WhatsApp"],
        "Windows": ["C:\\Program Files\\WhatsApp\\WhatsApp.exe"],
        "Linux": ["whatsapp"],
    },
    "slack": {"Darwin": ["open", "-a", "Slack"], "Linux": ["slack"]},
}
DISPLAY = {"safari": "Safari", "whatsapp": "WhatsApp", "slack": "Slack"}


def _boom(_history, _payload):
    raise AssertionError("Provider must not be called for local app launch")


def _blocking_layer():
    return build_layer(
        models=[{"key": "m1", "model": "m", "priority": 90,
                 "capabilities": {"reasoning": True, "tool_calling": True}}],
        behaviours={"m": _boom},
    )


@pytest.fixture()
def _quiet():
    configure_logging(debug=False)
    yield
    configure_logging(debug=False)


@pytest.fixture()
def mapped(monkeypatch):
    """Install the map above, and pin the OS so argv assertions are exact.

    Discovery is pinned empty as well: an unmapped name now falls through to
    `installed_apps()`, and without this the suite would read the real
    filesystem and pass or fail depending on what is installed on the machine
    running it. Every assertion below is unchanged.
    """
    monkeypatch.setattr(apps, "COMMANDS", COMMANDS)
    monkeypatch.setattr(apps, "DISPLAY_NAMES", DISPLAY)
    monkeypatch.setattr(apps.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apps, "installed_apps", lambda *a, **k: [])
    return COMMANDS


def _ask_blocked(text):
    brain = ZoeBrain(model_layer=_blocking_layer())
    return brain, brain.ask(text)


class _Done(subprocess.CompletedProcess):
    def __init__(self, rc=0, err=""):
        super().__init__(args=["launch"], returncode=rc, stdout="", stderr=err)


@pytest.fixture()
def spawned(monkeypatch):
    """Record every subprocess call instead of making one. Returns the list."""
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Done()

    monkeypatch.setattr(apps.subprocess, "run", _run)
    return calls


# ============================================================================
# The map is the allowlist
# ============================================================================

class TestMapIsTheAllowlist:
    def test_a_mapped_name_launches_the_mapped_command(self, _quiet, mapped, spawned):
        _, response = _ask_blocked("Open WhatsApp")
        assert response == "WhatsApp is now open."
        assert len(spawned) == 1
        argv, kwargs = spawned[0]
        assert argv == ["open", "-a", "WhatsApp"]
        assert kwargs.get("shell") is not True and "shell" not in kwargs

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_each_os_uses_its_own_mapping(self, _quiet, mapped, spawned,
                                           monkeypatch, system):
        """The same spoken name resolves differently per OS -- the point of B."""
        monkeypatch.setattr(apps.platform, "system", lambda: system)
        assert apps.launch_app("Safari") == "Safari is now open."
        assert spawned[0][0] == mapped["safari"][system]

    def test_a_name_outside_the_map_is_refused(self, _quiet, mapped, spawned):
        _, response = _ask_blocked("Open RandomApp")
        assert spawned == [], "an unmapped name reached the OS"
        assert "RandomApp" in response and "approved" in response

    def test_every_system_is_reachable_for_every_mapped_app(self, _quiet, mapped,
                                                             spawned, monkeypatch):
        """Every declared (app, OS) pair must be runnable, not just the host's."""
        for system in SYSTEMS:
            monkeypatch.setattr(apps.platform, "system", lambda: system)
            for key, table in mapped.items():
                if system not in table:
                    continue
                spawned.clear()
                assert apps.launch_app(DISPLAY[key]) == f"{DISPLAY[key]} is now open."
                assert spawned[0][0] == table[system]

    def test_mapped_but_absent_for_this_os_says_so(self, _quiet, mapped, spawned,
                                                   monkeypatch):
        """Authorized, but nothing configured to run here. Say that, don't guess."""
        monkeypatch.setattr(apps.platform, "system", lambda: "Windows")
        response = apps.launch_app("Slack")
        assert spawned == [], "an unmapped OS fell back to some other command"
        assert "Slack" in response and "Windows" in response
        assert "is now open" not in response

    def test_the_rejection_distinguishes_the_two_refusals(self, _quiet, mapped,
                                                           spawned, monkeypatch):
        unmapped = apps.launch_app("RandomApp")
        monkeypatch.setattr(apps.platform, "system", lambda: "Windows")
        unavailable = apps.launch_app("Slack")
        assert unmapped != unavailable
        assert "approved" in unmapped and "approved" not in unavailable

    def test_an_empty_map_denies_everything(self, _quiet, spawned, monkeypatch):
        """Default deny is the invariant, not the shipped list."""
        monkeypatch.setattr(apps, "installed_apps", lambda *a, **k: [])
        for empty in ({}, None, "Safari", [None, 7]):
            monkeypatch.setattr(apps, "COMMANDS", empty)
            assert apps.resolve_command("Safari") is None
            assert "approved" in apps.launch_app("Safari")
        assert spawned == []

    def test_registry_slot_is_real_wrapper(self):
        assert TOOL_REGISTRY["launch_app"] is brain_module.launch_app


# ============================================================================
# argv-only, on every platform. No shell, no `cmd /c`.
# ============================================================================

class TestArgvOnly:
    @pytest.mark.parametrize("system", SYSTEMS)
    def test_no_shell_on_any_system(self, _quiet, mapped, spawned, monkeypatch,
                                    system):
        monkeypatch.setattr(apps.platform, "system", lambda: system)
        apps.launch_app("Safari")
        argv, kwargs = spawned[0]
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)
        assert "shell" not in kwargs and kwargs.get("shell") is not True

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_no_shell_interpreter_is_ever_argv_zero(self, _quiet, mapped, spawned,
                                                    monkeypatch, system):
        """`cmd /c`, `sh -c` and friends would make argv-only a fiction."""
        monkeypatch.setattr(apps.platform, "system", lambda: system)
        apps.launch_app("Safari")
        argv = spawned[0][0]
        assert Path(argv[0]).name.casefold() not in {"cmd", "cmd.exe", "sh",
                                                     "bash", "powershell",
                                                     "pwsh", "zsh", "fish"}

    def test_a_malformed_table_never_becomes_an_argv(self, _quiet, spawned,
                                                     monkeypatch):
        """Non-string or empty parts are dropped at load, not sanitised later."""
        for bad in ({"Darwin": [""]}, {"Darwin": ["open", None]},
                    {"Darwin": []}, {"Darwin": ""}, {"Darwin": 7}):
            monkeypatch.setattr(config_module, "APP_COMMANDS",
                                config_module._validated_app_map({"x": bad})[0])
            monkeypatch.setattr(apps, "COMMANDS",
                                config_module._validated_app_map({"x": bad})[0])
            apps.launch_app("x")
        assert spawned == []

    def test_a_mapped_evil_name_still_cannot_smuggle_arguments(self, _quiet,
                                                               mapped, spawned,
                                                               monkeypatch):
        """Belt and braces: an ugly but configured name still runs as one argv
        element, with no shell to interpret anything."""
        weird = "Weird; rm -rf ~"
        monkeypatch.setattr(apps, "COMMANDS", {"weird; rm -rf ~": {"Darwin": ["open", "-a"]}})
        monkeypatch.setattr(apps, "DISPLAY_NAMES", {"weird; rm -rf ~": weird})
        apps.launch_app(weird)
        argv, kwargs = spawned[0]
        assert argv == ["open", "-a"]
        assert "shell" not in kwargs

    @pytest.mark.parametrize("injected", [
        "Safari --args evil", "Safari -a", "Safari\nOpen Terminal",
        "Safari\x00Terminal", "Safari; Terminal", "Safari && Terminal",
        "Safari | cat /etc/passwd", "$(whoami)", "`whoami`", "../../something",
    ])
    def test_injection_never_reaches_the_os(self, _quiet, mapped, spawned, injected):
        """A spoken name is data. It is looked up, never concatenated."""
        apps.launch_app(injected)
        assert spawned == [], f"{injected!r} reached the OS"

    def test_the_os_is_never_consulted_to_authorize_a_name(self, _quiet, mapped,
                                                           spawned):
        """Ordering is the whole security property: look up first, then run."""
        assert apps.resolve_command("NotAllowed") is None
        assert spawned == [], "resolution consulted the OS before authorizing"
        apps.resolve_command("Safari")
        assert spawned == [], "resolution asked the OS to verify the name"
        apps.launch_app("Safari")
        assert spawned[0][0] == ["open", "-a", "Safari"]

    def test_lookup_never_executes_anything(self, _quiet, mapped, spawned):
        """`lookup` is pure data. Nothing may run just because we resolved a name."""
        for name in ("Safari", "slack", "nonsense", "", None):
            apps.lookup(name)
            apps.resolve_command(name)
        assert spawned == []


# ============================================================================
# Local failures, no provider
# ============================================================================

class TestLocalFailures:
    def test_not_installed_is_a_local_error(self, _quiet, mapped, monkeypatch):
        monkeypatch.setattr(apps.subprocess, "run",
                            lambda argv, **kw: _Done(rc=1, err="No such file"))
        assert "isn't installed" in apps.launch_app("Safari")

    def test_a_missing_executable_is_a_local_error(self, _quiet, mapped, monkeypatch):
        def _gone(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(apps.subprocess, "run", _gone)
        response = apps.launch_app("Safari")
        assert "isn't installed" in response
        assert "is now open" not in response

    def test_a_timeout_is_a_local_error(self, _quiet, mapped, monkeypatch):
        def _slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30)

        monkeypatch.setattr(apps.subprocess, "run", _slow)
        assert "timed out" in apps.launch_app("Safari")

    @pytest.mark.parametrize("junk", [None, 42, [], {}, True])
    def test_non_string_names_are_empty_not_a_crash(self, _quiet, mapped, junk):
        assert apps.normalize_app_name(junk) == ""
        assert apps.lookup(junk) is None
        assert "need an application name" in apps.launch_app(junk)

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n", ".", "!!!", " . . "])
    def test_punctuation_only_input_is_empty(self, _quiet, mapped, blank):
        assert apps.normalize_app_name(blank) == ""
        assert apps.lookup(blank) is None

    def test_empty_input_rejected(self, _quiet, mapped, spawned):
        for blank in ("", "   ", None):
            assert "need an application name" in apps.launch_app(blank)
        assert spawned == []


# ============================================================================
# Name resolution
# ============================================================================

class TestNameResolution:
    @pytest.mark.parametrize("spoken", [
        "Safari.", "Safari!", "Safari?", "Safari,", "Safari...", " safari  ",
        "SAFARI", "Safari", " safari.\t",
    ])
    def test_punctuation_and_case_reach_the_same_application(
            self, _quiet, mapped, spawned, spoken):
        """Failure A: "Open Safari." was rejected as 'safari. isn't approved'."""
        assert apps.resolve_command(spoken) == ("Safari", ["open", "-a", "Safari"])
        assert apps.launch_app(spoken) == "Safari is now open."
        assert [argv for argv, _ in spawned] == [["open", "-a", "Safari"]]

    def test_the_reply_uses_the_configured_spelling(self, _quiet, mapped,
                                                   monkeypatch):
        """Lowercase input still answers 'WhatsApp', not 'whatsapp'."""
        assert apps.display_name("whatsapp") == "WhatsApp"
        assert apps.display_name("  WHATSAPP.  ") == "WhatsApp"
        assert apps.launch_app("whatsapp") == "WhatsApp is now open."

    def test_an_unknown_name_falls_back_to_what_was_said(self, _quiet, mapped):
        assert apps.display_name("  some App.  ") == "some App"

    def test_interior_characters_are_never_stripped(self, _quiet, monkeypatch):
        """Trimming must not turn 'C++' into 'C' or eat a path separator."""
        monkeypatch.setattr(apps, "COMMANDS", {"c++": {"Darwin": ["open", "-a", "C++"]},
                                              "visual.studio": {"Darwin": ["code"]}})
        monkeypatch.setattr(apps, "DISPLAY_NAMES", {"c++": "C++",
                                                    "visual.studio": "Visual.Studio"})
        assert apps.resolve_command("C++") == ("C++", ["open", "-a", "C++"])
        assert apps.resolve_command("C++.") == ("C++", ["open", "-a", "C++"])
        # An interior dot is part of the name, so "Visual.Studio" resolves while
        # "Visual Studio" -- a different spoken name -- does not.
        assert apps.resolve_command("Visual.Studio") == ("Visual.Studio", ["code"])
        assert apps.lookup("Visual Studio") is None

    def test_normalization_is_idempotent(self, _quiet):
        once = apps.normalize_app_name("  Safari...  ")
        assert apps.normalize_app_name(once) == once == "Safari"

    def test_lookup_returns_a_copy(self, _quiet, mapped, monkeypatch):
        """A caller mutating the returned table must not corrupt the map."""
        table = apps.lookup("Safari")
        table["Darwin"] = ["rm", "-rf", "/"]
        assert apps.launch_app("Safari") == "Safari is now open."


# ============================================================================
# Config parsing
# ============================================================================

class TestConfigParsing:
    def test_argv_accepts_a_bare_string_or_a_list(self):
        v = config_module._validated_argv
        assert v("safari") == ["safari"]
        assert v(["open", "-a", "Safari"]) == ["open", "-a", "Safari"]
        assert v([]) is None and v("") is None and v(None) is None
        assert v(["open", ""]) is None and v(["open", 7]) is None
        assert v(("open", "-a")) is None  # a tuple is not an argument list

    def test_map_drops_malformed_entries(self):
        commands, display = config_module._validated_app_map({
            "  Safari  ": {"Darwin": ["open", "-a", "Safari"]},
            "bad": {"Darwin": []},
            "worse": {"Darwin": 7},
            "nope": "not a table",
            "": {"Darwin": ["x"]},
            9: {"Darwin": ["x"]},
        })
        assert set(commands) == {"safari"}
        assert display == {"safari": "Safari"}

    def test_a_non_dict_map_is_empty(self):
        for raw in (None, [], "Safari", 7):
            assert config_module._validated_app_map(raw) == ({}, {})

    def test_a_duplicate_spelling_keeps_the_first(self):
        commands, _ = config_module._validated_app_map({
            "Safari": {"Darwin": ["first"]},
            "safari": {"Darwin": ["second"]},
        })
        assert commands["safari"]["Darwin"] == ["first"]

    def test_a_missing_key_denies_everything(self, _quiet, spawned):
        for raw in (None, {}, [], "Safari", [None, 7]):
            commands, display = config_module._validated_app_map(raw)
            assert commands == {} and display == {}

    def test_the_shipped_map_is_well_formed(self):
        """Whatever the user configured must be usable as-is."""
        commands, display = config_module.APP_COMMANDS, config_module.APP_DISPLAY_NAMES
        assert commands, "config.yaml maps no applications at all"
        for key, table in commands.items():
            assert key == key.casefold() and key.strip() == key
            assert display[key] and display[key].strip()
            for system, argv in table.items():
                assert system and isinstance(system, str)
                assert argv and all(isinstance(p, str) and p for p in argv)

    def test_the_shipped_map_declares_more_than_one_system(self):
        """A single-system map would mean option B was not really implemented."""
        systems = {s for table in config_module.APP_COMMANDS.values() for s in table}
        assert len(systems) > 1, f"only {systems} present in the shipped map"


# ============================================================================
# The refusal must be actionable
# ============================================================================

class TestApprovalGuidance:
    def test_rejection_says_how_to_approve(self, _quiet, mapped):
        response = apps.launch_app("RandomApp")
        assert "approved" in response
        assert "applications.aliases" in response, (
            "an unmapped app must be told how to become mapped, not just denied"
        )

    def test_rejection_does_not_claim_to_have_opened_anything(self, _quiet, mapped):
        assert "is now open" not in apps.launch_app("RandomApp")


# ============================================================================
# Intent classification
# ============================================================================

class TestIntentClassification:
    @pytest.mark.parametrize("text", [
        "Open Safari.", "Open Safari!", "open safari", "Launch Safari.",
        "Could you open Safari, please?", "Launch SAFARI!", "Open WhatsApp",
        "Start Safari", "Can you start Safari?", "please open Terminal",
        "Launch Visual Studio Code", "Open Safari, please",
    ])
    def test_application_requests_are_captured(self, text):
        found = match_launch_app(text)
        assert found is not None and found.intent == "launch_app", text

    @pytest.mark.parametrize("text", [
        "Start a conversation with me.", "start a chat", "start talking to me",
        "Let's talk.", "Tell me about yourself.", "Can we chat?",
        "Start the music", "start a game", "Let's start over", "start my day",
        "start working on my presentation", "start up my music",
    ])
    def test_conversational_requests_are_not_application_requests(self, text):
        assert match_launch_app(text) is None, (
            f"{text!r} was classified as an application launch"
        )

    def test_punctuation_never_reaches_the_app_name(self):
        """The name handed on must be bare, for lookup and for display."""
        for text, expected in (("Open Safari.", "Safari"),
                               ("Could you open Safari, please?", "Safari"),
                               ("Launch SAFARI!", "SAFARI")):
            assert match_launch_app(text).args["app_name"] == expected

    @pytest.mark.parametrize("text,other", [
        ("What is the weather in Delhi?", "weather"),
        ("weather", "weather"),
        ("what time is it", "time"),
        ("define lucid", "dictionary"),
        ("take a screenshot", "screenshot"),
        ("play jazz", "media_play"),
    ])
    def test_other_local_routes_are_untouched(self, text, other):
        found = match_intent(text)
        assert found is not None and found.intent == other, text

    @pytest.mark.parametrize("text", [
        "Create a file on my Desktop.",
        "Find a file in my project folder.",
        "Tell me today's space news.",
    ])
    def test_non_application_requests_still_fall_through(self, text):
        assert match_launch_app(text) is None


# ============================================================================
# Logging separation
# ============================================================================

class TestLogging:
    def test_normal_terminal_clean(self, _quiet, mapped, spawned, capsys):
        _ask_blocked("Open WhatsApp")
        out, err = capsys.readouterr()
        assert out == "" and err == ""

    def test_debug_names_launch(self, _quiet, mapped, spawned, caplog):
        configure_logging(debug=True)
        try:
            with caplog.at_level("DEBUG", logger="zoe"):
                _ask_blocked("Launch Safari")
            assert any("launch_app" in r.message and "skipped" in r.message
                       for r in caplog.records)
        finally:
            configure_logging(debug=False)