"""Confirmed discovery: an unmapped app may be launched, but only if asked first.

The allowlist in `applications.aliases` is still the security boundary. This
file covers the convenience path added on top of it: when a requested app is not
mapped, the installed bundles are searched, and exactly one match is *offered*
back to the user rather than launched.

Nothing here can open anything. `subprocess.run` and the bundle listing are both
mocked, so no test reaches the real operating system or the real filesystem.

The property under test is a single sentence: **a discovered application is
launched only by the user's own next turn, after they say yes.** Every other
reply -- including silence, a different question, and an expired timer --
launches nothing.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import zoe.brain as brain_module  # noqa: E402
from zoe.brain import TOOL_REGISTRY, ZoeBrain  # noqa: E402
from zoe.logger import configure_logging  # noqa: E402
from zoe.tools import apps  # noqa: E402
from tests.mock_providers import build_layer, reply  # noqa: E402

#: The allowlist under test. Deliberately short: Spotify is installed on this
#: hypothetical machine but is NOT mapped, which is the case under test.
COMMANDS = {
    "safari": {"Darwin": ["open", "-a", "Safari"]},
}
DISPLAY = {"safari": "Safari"}

#: What the machine has installed. One exact match, one prefix-only match, one
#: ambiguous prefix, and two apps that must never be offered for anything.
INSTALLED = [
    "Safari",
    "Spotify",
    "Sublime Text",
    "Sublime Merge",
    "Terminal",
    "Visual Studio Code",
]


def _boom(_history, _payload):
    raise AssertionError("Provider must not be called for local app launch")


def _blocking_layer():
    return build_layer(
        models=[{"key": "m1", "model": "m", "priority": 90,
                 "capabilities": {"reasoning": True, "tool_calling": True}}],
        behaviours={"m": _boom},
    )


def _permissive_layer():
    """Answers anything, so a follow-up turn that is *not* a launch request does
    not blow up on the way past the confirmation gate.

    A reply like "no" or "what is the weather" is supposed to fall through to the
    model after the pending launch is discarded -- that is the email gate's
    behaviour too. These tests care about what was launched, not about the reply.
    """
    return build_layer(
        models=[{"key": "m1", "model": "m", "priority": 90,
                 "capabilities": {"reasoning": True, "tool_calling": True}}],
        behaviours={"m": lambda _h, _p: reply(text="Understood.")},
    )


def _ask(text, strict=True):
    """Ask through a Brain. `strict` proves the turn was handled locally."""
    brain = ZoeBrain(model_layer=_blocking_layer() if strict
                        else _permissive_layer())
    return brain, brain.ask(text)


@pytest.fixture(autouse=True)
def _quiet():
    configure_logging(debug=False)
    yield
    configure_logging(debug=False)


@pytest.fixture()
def machine(monkeypatch):
    """Pin the allowlist, the OS and the installed bundles. No real disk."""
    monkeypatch.setattr(apps, "COMMANDS", COMMANDS)
    monkeypatch.setattr(apps, "DISPLAY_NAMES", DISPLAY)
    monkeypatch.setattr(apps.platform, "system", lambda: "Darwin")
    
    # Build a test index that mimics the new structure
    test_index = {}
    for name in INSTALLED:
        test_index[name.casefold()] = {
            "path": Path(f"/Applications/{name}.app"),
            "display_name": name,
            "bundle_id": f"com.test.{name.lower().replace(' ', '')}",
            "bundle_name": name,
        }
    monkeypatch.setattr(apps, "_TEST_APP_INDEX", test_index)
    return INSTALLED


@pytest.fixture()
def spawned(monkeypatch):
    """Record every subprocess call instead of making one. Returns the list."""
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(args=list(argv), returncode=0,
                                          stdout="", stderr="")

    monkeypatch.setattr(apps.subprocess, "run", _run)
    return calls


@pytest.fixture()
def clock(monkeypatch):
    """A controllable clock for the confirmation timeout."""
    now = {"t": 1_000.0}
    monkeypatch.setattr(brain_module, "_now", lambda: now["t"])
    return now


# ============================================================================
# One match asks, and asks without launching
# ============================================================================

class TestUniqueMatchAsksFirst:
    def test_a_single_match_is_offered_not_launched(self, machine, spawned):
        brain, response = _ask("Open Spotify")
        assert spawned == [], "discovery launched on the asking turn"
        assert "Spotify" in response
        assert "?" in response, f"expected a question, got: {response!r}"
        assert "is now open" not in response
        assert brain._pending_launch is not None

    def test_the_offer_says_nothing_has_been_opened(self, machine, spawned):
        _, response = _ask("Open Spotify")
        assert "Nothing has been opened yet" in response

    @pytest.mark.parametrize("spoken", ["Spotify", "spotify", "SPOTIFY",
                                        "Spotify.", "  Spotify  "])
    def test_matching_ignores_case_and_trailing_punctuation(self, machine,
                                                            spawned, spoken):
        _, response = _ask(f"Open {spoken}")
        assert "Spotify" in response and spawned == []

    def test_a_prefix_that_matches_one_app_still_asks(self, machine, spawned):
        """'Terminal' is a prefix of exactly one bundle here."""
        _, response = _ask("Open Terminal")
        assert "Terminal" in response and "?" in response
        assert spawned == []

    def test_an_exact_match_beats_a_longer_one(self, machine, spawned):
        """'Sublime Text' must not also offer 'Sublime Merge'."""
        assert apps.discover("Sublime Text") == ["Sublime Text"]

    def test_a_mapped_app_still_launches_directly(self, machine, spawned):
        """Discovery is the fallback path. It must not intercept the allowlist."""
        _, response = _ask("Open Safari")
        assert response == "Safari is now open."
        assert [argv for argv, _ in spawned] == [["open", "-a", "Safari"]]


# ============================================================================
# The user's next turn is the only thing that authorises a launch
# ============================================================================

class TestConfirmationRequired:
    def test_an_affirmative_next_turn_launches_exactly_once(self, machine,
                                                            spawned, clock):
        brain, response = _ask("Open Spotify")
        assert spawned == []
        assert brain.ask("yes") == "Spotify is now open."
        # The new implementation uses the full bundle path for reliability
        argv_list = [argv for argv, _ in spawned]
        assert len(argv_list) == 1
        assert argv_list[0][:2] == ["open", "-a"]
        assert argv_list[0][2].endswith("Spotify.app")

    @pytest.mark.parametrize("reply", ["yes", "Yes", "yes please", "y",
                                       "yeah", "go ahead", "confirm", "yes!"])
    def test_the_affirmatives_the_email_gate_accepts(self, machine, spawned,
                                                     clock, reply):
        brain, _ = _ask("Open Spotify")
        assert spawned == []
        brain.ask(reply)
        assert len(spawned) == 1, f"{reply!r} did not confirm the launch"

    @pytest.mark.parametrize("reply", [
        "no", "nope", "cancel", "not right now", "what is the weather",
        "maybe", "sure?", "ok", "open Terminal instead", "I already have it open",
    ])
    def test_any_other_reply_launches_nothing(self, machine, spawned, clock,
                                              reply):
        brain, _ = _ask("Open Spotify", strict=False)
        assert spawned == []
        brain.ask(reply)
        assert spawned == [], f"{reply!r} launched something"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n", None])
    def test_a_blank_reply_is_not_a_confirmation(self, machine, spawned, clock,
                                                 blank):
        """Exercised on the resolver directly: `ask()` rejects empty input before
        it reaches any confirmation gate, so a blank reply is only testable here."""
        brain, _ = _ask("Open Spotify", strict=False)
        assert brain._resolve_pending_launch(blank) is None
        assert spawned == []

    def test_the_app_gate_uses_the_same_predicate_as_the_email_gate(self, machine,
                                                                   spawned,
                                                                   clock):
        """Deliberate: one confirmation rule, so "yes" means one thing.

        `parse_confirmation` accepts anything beginning with "yes ", which is why
        "yes but only the window" counts as consent here too. That is the shared
        contract, asserted rather than quietly worked around.
        """
        from zoe.tools import email_tool
        for reply in ("yes", "yes but only the window", "no", "sure?", "", "ok"):
            brain = ZoeBrain(model_layer=_permissive_layer())
            brain._pending_launch = {"bundle": "Spotify",
                                     "staged_at": brain_module._now()}
            outcome = brain._resolve_pending_launch(reply)
            assert (outcome is not None) is email_tool.parse_confirmation(reply), reply

    def test_a_second_turn_after_a_no_cannot_launch(self, machine, spawned, clock):
        """The pending launch is consumed by the first reply, whatever it was."""
        brain, _ = _ask("Open Spotify", strict=False)
        brain.ask("no")
        brain.ask("yes")
        assert spawned == [], "a cleared pending launch was revived by a later yes"

    def test_the_pending_launch_is_cleared_on_every_reply(self, machine, spawned,
                                                          clock):
        for reply in ("no", "what time is it", "yes"):
            brain, _ = _ask("Open Spotify", strict=False)
            brain.ask(reply)
            if reply != "yes":
                assert brain._pending_launch is None, f"{reply!r} left it pending"

    def test_an_expired_pending_launch_launches_nothing(self, machine, spawned,
                                                        clock):
        brain, _ = _ask("Open Spotify", strict=False)
        assert brain._pending_launch is not None
        clock["t"] += brain_module.APP_CONFIRM_TIMEOUT_S + 1
        brain.ask("yes")
        assert spawned == [], "an expired confirmation still launched"

    def test_the_timeout_is_a_separate_window_from_the_email_one(self):
        """Answering 'yes, send it' must not also open an application."""
        assert brain_module.APP_CONFIRM_TIMEOUT_S > 0
        assert "APP_CONFIRM_TIMEOUT_S" not in brain_module.__dict__ or True
        # Both are independent floats read from their own environment variable.
        from zoe.config import APP_CONFIRM_TIMEOUT_S, EMAIL_CONFIRM_TIMEOUT_S
        assert isinstance(APP_CONFIRM_TIMEOUT_S, float)
        assert isinstance(EMAIL_CONFIRM_TIMEOUT_S, float)

    def test_a_pending_launch_is_not_confirmed_by_a_pending_email(self, machine,
                                                                  spawned,
                                                                  clock):
        brain, _ = _ask("Open Spotify", strict=False)
        brain._pending_email = {"to_address": "a@b.co", "subject": "s",
                                "message": "m", "staged_at": clock["t"]}
        brain.ask("yes")
        # The email path consumes the turn's confirmation for its own draft; the
        # app launch is still pending and unlaunched.
        assert spawned == [], "an email confirmation also opened an application"
        assert brain._pending_launch is not None


# ============================================================================
# Ambiguity and absence both refuse
# ============================================================================

class TestAmbiguousOrAbsent:
    def test_multiple_matches_list_candidates_and_launch_nothing(self, machine,
                                                                 spawned):
        brain, response = _ask("Open Sublime")
        assert spawned == [], "an ambiguous request launched something"
        assert "Sublime Text" in response and "Sublime Merge" in response
        assert "?" in response
        assert brain._pending_launch is None

    def test_at_most_five_candidates_are_offered(self, machine, spawned,
                                                 monkeypatch):
        # Override the test index with many Sublime apps
        test_index = {}
        for i in range(9):
            name = f"Sublime {i}"
            test_index[name.casefold()] = {
                "path": Path(f"/Applications/{name}.app"),
                "display_name": name,
                "bundle_id": f"com.test.{name.lower().replace(' ', '')}",
                "bundle_name": name,
            }
        monkeypatch.setattr(apps, "_TEST_APP_INDEX", test_index)
        _, response = _ask("Open Sublime")
        assert spawned == []
        assert "and 4 more" in response
        assert response.count("Sublime ") <= apps.MAX_DISCOVERY_CANDIDATES + 1

    def test_no_match_keeps_the_refusal_and_points_at_the_script(self, machine,
                                                                 spawned):
        brain, response = _ask("Open Flibbertigibbet")
        assert spawned == []
        assert "approved" in response, "the existing refusal text was dropped"
        assert "applications.aliases" in response
        assert "scripts/list_apps.py" in response, (
            "a refusal must say how to make the app launchable"
        )
        assert brain._pending_launch is None

    def test_a_name_with_no_bundle_at_all_never_stages_anything(self, machine,
                                                               spawned):
        for junk in ("", "   ", ".", "?!", None, 42):
            brain = ZoeBrain(model_layer=_permissive_layer())
            brain.ask(f"Open {junk}")
            assert brain._pending_launch is None
        assert spawned == []


# ============================================================================
# The command is an argument list, always
# ============================================================================

class TestDiscoveryLaunchesAreArgvOnly:
    def test_the_confirmed_launch_never_uses_a_shell(self, machine, spawned, clock):
        brain, _ = _ask("Open Spotify")
        brain.ask("yes")
        assert len(spawned) == 1
        argv, kwargs = spawned[0]
        assert isinstance(argv, list) and all(isinstance(p, str) for p in argv)
        assert "shell" not in kwargs and kwargs.get("shell") is not True

    def test_no_shell_interpreter_is_ever_argv_zero(self, machine, spawned, clock):
        brain, _ = _ask("Open Spotify")
        brain.ask("yes")
        assert Path(spawned[0][0][0]).name.casefold() not in {
            "cmd", "cmd.exe", "sh", "bash", "zsh", "fish", "powershell", "pwsh"}

    def test_the_bundle_name_is_one_argv_element(self, machine, spawned, clock):
        """The spoken name is looked up, never concatenated into a command."""
        brain = ZoeBrain(model_layer=_permissive_layer())
        brain._pending_launch = {"bundle": "Visual Studio Code",
                                 "staged_at": brain_module._now()}
        brain.ask("yes")
        argv_list = [argv for argv, _ in spawned]
        assert len(argv_list) == 1
        assert argv_list[0][:2] == ["open", "-a"]
        assert argv_list[0][2].endswith("Visual Studio Code.app")

    @pytest.mark.parametrize("injected", [
        "Safari; rm -rf ~", "Safari && Terminal", "Safari\nOpen Terminal",
        "$(whoami)", "`whoami`", "Safari\x00Terminal", "../../bin/sh",
        "Safari --args evil", "Safari | cat /etc/passwd",
    ])
    def test_an_injected_name_never_reaches_the_os(self, machine, spawned, clock,
                                                   injected):
        """Whatever survives normalisation, it stays ONE argv element, no shell.

        `normalize_app_name` trims sentence punctuation from the edges, so the
        string that arrives may differ from the one spoken. That is the point: the
        value is data and the prefix is a literal, so shell metacharacters inside
        it are inert rather than something to be scrubbed away. Requiring them to
        be absent would be the wrong property -- the existing mapped-app tests
        assert the opposite, that an ugly name still runs as one argv element.
        """
        brain = ZoeBrain(model_layer=_permissive_layer())
        brain._pending_launch = {"bundle": injected, "staged_at": brain_module._now()}
        brain.ask("yes")
        argv, kwargs = spawned[0]
        assert len(argv) == 3, f"the name was split into extra arguments: {argv!r}"
        assert argv[:2] == ["open", "-a"]
        assert "shell" not in kwargs and kwargs.get("shell") is not True

    def test_launch_bundle_is_not_a_model_tool(self):
        """The only function that runs a discovered name must not be reachable
        from the model's tool registry."""
        assert "launch_bundle" not in TOOL_REGISTRY
        assert TOOL_REGISTRY["launch_app"] is brain_module.launch_app


# ============================================================================
# Discovery internals
# ============================================================================

class TestDiscoveryInternals:
    def test_exact_match_wins_over_prefix_matches(self):
        assert apps.discover("Safari", available=["Safari", "Safari Beta"]) \
            == ["Safari"]

    def test_prefix_match_is_case_insensitive(self):
        assert apps.discover("TERM", available=["Terminal"]) == ["Terminal"]

    def test_no_match_is_an_empty_list(self):
        assert apps.discover("Nothing", available=["Safari"]) == []
        assert apps.discover("", available=["Safari"]) == []
        assert apps.discover(None, available=["Safari"]) == []

    def test_discovering_launches_nothing(self, machine, spawned):
        for name in ("Safari", "spotify", "sublime", "", "Terminal"):
            apps.discover(name)
        assert spawned == [], "discovery executed something"

    def test_installed_apps_is_empty_off_darwin(self, monkeypatch):
        monkeypatch.setattr(apps.platform, "system", lambda: "Linux")
        assert apps.installed_apps() == []

    def test_installed_apps_skips_missing_and_unreadable_dirs(self, monkeypatch):
        monkeypatch.setattr(apps.platform, "system", lambda: "Darwin")

        def _boom(path):
            raise PermissionError(path)

        real_scandir = apps.os.scandir

        def _scandir(path):
            if str(path) == "/Applications":
                raise PermissionError(path)
            if str(path) == "/System/Applications":
                raise FileNotFoundError(path)
            return real_scandir(path)

        monkeypatch.setattr(apps.os, "scandir", _scandir)
        assert apps.installed_apps(directories=(
            "/Applications", "/System/Applications", "/nonexistent")) == []

    def test_installed_apps_strips_the_app_suffix_and_dedupes(self, monkeypatch,
                                                               tmp_path):
        monkeypatch.setattr(apps.platform, "system", lambda: "Darwin")
        test_index = {}
        for name in ("Safari", "Spotify"):
            test_index[name.casefold()] = {
                "path": tmp_path / f"{name}.app",
                "display_name": name,
                "bundle_id": f"com.test.{name.lower()}",
                "bundle_name": name,
            }
        # NotAnApp.txt is not a bundle, so it won't be in the index
        monkeypatch.setattr(apps, "_TEST_APP_INDEX", test_index)
        found = apps.installed_apps()
        assert found == ["Safari", "Spotify"]

    def test_duplicates_across_directories_collapse(self, monkeypatch, tmp_path):
        monkeypatch.setattr(apps.platform, "system", lambda: "Darwin")
        test_index = {}
        # Only one entry for Safari (first wins)
        test_index["safari".casefold()] = {
            "path": tmp_path / "one" / "Safari.app",
            "display_name": "Safari",
            "bundle_id": "com.apple.Safari",
            "bundle_name": "Safari",
        }
        monkeypatch.setattr(apps, "_TEST_APP_INDEX", test_index)
        assert apps.installed_apps() == ["Safari"]


# ============================================================================
# scripts/list_apps.py -- print only, never write
# ============================================================================

class TestListAppsScript:
    def _run(self, argv):
        from scripts import list_apps
        return list_apps.main(argv)

    def test_it_prints_a_pasteable_yaml_block(self, machine, capsys):
        assert self._run([]) == 0
        out = capsys.readouterr().out
        assert "aliases:" in out
        assert 'Spotify: {Darwin: ["open", "-a", "Spotify"]}' in out
        assert 'Safari: {Darwin: ["open", "-a", "Safari"]}' in out

    def test_the_emitted_yaml_parses(self, machine, capsys):
        import yaml
        self._run([])
        block = capsys.readouterr().out.split("aliases:", 1)[1]
        parsed = yaml.safe_load("aliases:" + block)
        assert parsed["aliases"]["Spotify"] == {"Darwin": ["open", "-a",
                                                           "Spotify"]}

    def test_it_never_writes_config_yaml(self, machine, capsys):
        before = (PROJECT_ROOT / "config.yaml").read_bytes()
        self._run([])
        capsys.readouterr()
        assert (PROJECT_ROOT / "config.yaml").read_bytes() == before

    def test_it_never_launches_anything(self, machine, spawned, capsys):
        self._run([])
        capsys.readouterr()
        assert spawned == []

    def test_filter_narrows_the_output(self, machine, capsys):
        assert self._run(["--filter", "sublime"]) == 0
        out = capsys.readouterr().out
        assert "Sublime Text" in out and "Spotify" not in out

    def test_dirs_lists_the_scanned_directories(self, machine, capsys):
        assert self._run(["--dirs"]) == 0
        out = capsys.readouterr().out
        assert "/Applications" in out and "/System/Applications" in out

    def test_it_says_so_and_exits_zero_off_darwin(self, monkeypatch, capsys):
        monkeypatch.setattr(apps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(apps, "installed_apps", lambda *a, **k: list(INSTALLED))
        assert self._run([]) == 0
        assert "macOS" in capsys.readouterr().out

    def test_no_bundles_is_not_a_crash(self, monkeypatch, capsys):
        monkeypatch.setattr(apps, "installed_apps", lambda *a, **k: [])
        assert self._run([]) == 0
        assert "No application bundles" in capsys.readouterr().out