"""Phase 4 tests: email sends only after explicit confirmation.

The SMTP layer is faked (no real mail ever). GMAIL_* are stubbed so tests never
depend on a real .env.

The confirmation gate used to live inside this module: `send_email` prompted
the user with `listen()` and read the answer itself. That is gone, and so is any
confirmation argument a model could set. The flow is now:

* the model calls the Brain's `send_email` tool, which only stages a draft and
  returns -- it never sends and takes no `confirmed` argument,
* `parse_confirmation(reply)` turns the user's own next message into a decision,
* only the Brain, on that next turn, calls `email_tool.send_email(..., confirmed=True)`.

These tests assert both halves against the real functions. Nothing is weakened:
"unconfirmed never sends", "explicit yes sends exactly once", "ambiguous and
cancel never send", "a model cannot send on its own" and the logging-privacy
guarantees are all still checked.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import zoe.brain as brain_module  # noqa: E402
from zoe.brain import ZoeBrain  # noqa: E402
from zoe.logger import configure_logging  # noqa: E402
from zoe.providers.base import ModelResponse, ToolCall, new_tool_call_id  # noqa: E402
from zoe.tools import email_tool  # noqa: E402
from tests.mock_providers import build_layer  # noqa: E402

BODY = "THIS_IS_PRIVATE_EMAIL_BODY_12345"
RECIPIENT = "alice@example.com"
SUBJECT = "Meeting Tomorrow"


class _FakeSMTP:
    """Recording SMTP stand-in. Instances are tracked on the class."""
    instances = []

    def __init__(self, *args, **kwargs):
        self.sent = []
        _FakeSMTP.instances.append(self)

    def ehlo(self): ...
    def starttls(self): ...
    def login(self, *args): ...
    def send_message(self, msg):
        self.sent.append(msg)
    def quit(self): ...


@pytest.fixture()
def _mail(monkeypatch):
    """Fake creds and fake SMTP. Yields (smtp_instances, answers)."""
    _FakeSMTP.instances = []
    monkeypatch.setattr(email_tool, "GMAIL_ADDRESS", "zoe@example.com")
    monkeypatch.setattr(email_tool, "GMAIL_APP_PASSWORD", "FAKEPASS123")
    monkeypatch.setattr(email_tool.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP.instances, {"reply": "none"}


@pytest.fixture()
def _quiet():
    configure_logging(debug=False)
    yield
    configure_logging(debug=False)


def _sends():
    return [m for inst in _FakeSMTP.instances for m in inst.sent]


def _send(confirmed=False):
    return email_tool.send_email(RECIPIENT, SUBJECT, BODY, confirmed=confirmed)


class TestConfirmationGate:
    def test_unconfirmed_send_never_executes(self, _mail, _quiet):
        result = _send(confirmed=False)
        assert _sends() == [], "an unconfirmed send reached SMTP"
        assert "not sent" in str(result).lower()

    @pytest.mark.parametrize("yes", ["yes", "YES", " yes ", "y", "yeah",
                                     "go ahead", "send it", "confirm"])
    def test_explicit_yes_sends_once(self, _mail, _quiet, yes):
        # What the user said is what opens the gate...
        assert email_tool.parse_confirmation(yes) is True
        # ...and only a confirmed call actually reaches SMTP.
        _send(confirmed=True)
        sent = _sends()
        assert len(sent) == 1, f"expected exactly one send, got {len(sent)}"
        assert sent[0]["To"] == RECIPIENT
        assert sent[0]["Subject"] == SUBJECT
        assert BODY in sent[0].as_string()

    @pytest.mark.parametrize("no", ["no", "n", "nope"])
    def test_no_does_not_send(self, _mail, _quiet, no):
        assert email_tool.parse_confirmation(no) is False
        _send(confirmed=False)
        assert _sends() == []

    @pytest.mark.parametrize("vague", ["maybe", "sure?", "I guess", "probably",
                                       "okay?", "", "sure", "ok", "okay",
                                       "yes? no", "cancel", "stop",
                                       "don't send", "don't send it"])
    def test_ambiguous_and_cancel_never_send(self, _mail, _quiet, vague):
        # A cancel, a maybe and a bare "okay?" are refusals, not consent.
        assert email_tool.parse_confirmation(vague) is False
        _send(confirmed=False)
        assert _sends() == []

    def test_parse_confirmation_unit(self):
        assert email_tool.parse_confirmation("yes") is True
        assert email_tool.parse_confirmation("  Yes, send it. ") is True
        assert email_tool.parse_confirmation("no") is False
        assert email_tool.parse_confirmation("okay") is False
        assert email_tool.parse_confirmation("") is False
        assert email_tool.parse_confirmation(None) is False


class TestLoggingPrivacy:
    def test_body_never_logged(self, _mail, _quiet, caplog):
        with caplog.at_level("INFO", logger="zoe"):
            _send(confirmed=True)
        text = "\n".join(r.message for r in caplog.records)
        assert RECIPIENT in text
        assert SUBJECT in text
        assert BODY not in text

    def test_unconfirmed_logged_without_body(self, _mail, _quiet, caplog):
        with caplog.at_level("INFO", logger="zoe"):
            _send(confirmed=False)
        text = "\n".join(r.message for r in caplog.records)
        assert BODY not in text

    def test_secrets_never_logged(self, _mail, _quiet, caplog):
        import smtplib as _real_smtp

        class _Boom(_FakeSMTP):
            def send_message(self, msg):
                raise _real_smtp.SMTPException("smtp refused")

        import smtplib
        smtplib.SMTP = _Boom
        try:
            with caplog.at_level("DEBUG", logger="zoe"):
                _send(confirmed=True)
        finally:
            smtplib.SMTP = _FakeSMTP
        text = "\n".join(r.message for r in caplog.records)
        assert "FAKEPASS123" not in text
        assert "smtp refused" in text  # raw error logged internally


class TestSMTPFailureReporting:
    """A failed send must name the failure that actually happened.

    Both defects below shipped together and hid each other: `quit()` ran in a
    `finally`, so when `login()` had already failed its own exception replaced the
    real one, and the text that reached the user was "please run connect() first"
    -- a network diagnosis for an authentication rejection.
    """

    def test_a_rejected_app_password_is_reported_as_such(self, _mail, _quiet):
        import smtplib as real_smtp

        class _AuthFails(_FakeSMTP):
            def login(self, *args):
                raise real_smtp.SMTPAuthenticationError(
                    535, b"5.7.8 Username and Password not accepted.")

            def quit(self):
                # Exactly what Gmail does: the connection is already gone, and a
                # `finally`-block quit() turns this into the reported error.
                raise real_smtp.SMTPServerDisconnected("please run connect() first")

        _FakeSMTP.instances = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(email_tool.smtplib, "SMTP", _AuthFails)
            result = _send(confirmed=True)

        assert "535" in result, result
        assert "App Password" in result, result
        assert "SMTPServerDisconnected" not in result, (
            "the teardown exception is still replacing the real failure"
        )
        assert "please run connect()" not in result, (
            "the user is being told the network failed when it was the credential"
        )

    def test_a_server_rejection_reports_its_code_and_reason(self, _mail, _quiet):
        import smtplib as real_smtp

        class _RecipientUnknown(_FakeSMTP):
            def send_message(self, msg):
                raise real_smtp.SMTPResponseException(550, b"5.1.1 user unknown")

        _FakeSMTP.instances = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(email_tool.smtplib, "SMTP", _RecipientUnknown)
            result = _send(confirmed=True)

        assert "550" in result, result
        assert "5.1.1" in result, result

    def test_the_password_appears_in_neither_the_reply_nor_the_log(
            self, _mail, _quiet, caplog):
        import smtplib as real_smtp

        class _EchoesTheSecret(_FakeSMTP):
            def login(self, *args):
                raise real_smtp.SMTPAuthenticationError(
                    535, f"5.7.8 bad password FAKEPASS123".encode())

        class _EchoesTheSecretOnSend(_FakeSMTP):
            def send_message(self, msg):
                raise real_smtp.SMTPResponseException(
                    550, f"5.1.1 user unknown FAKEPASS123".encode())

        for fake in (_EchoesTheSecret, _EchoesTheSecretOnSend):
            _FakeSMTP.instances = []
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(email_tool.smtplib, "SMTP", fake)
                with caplog.at_level("DEBUG", logger="zoe"):
                    caplog.clear()
                    result = _send(confirmed=True)
            text = "\n".join(r.message for r in caplog.records)
            assert "FAKEPASS123" not in result, result
            assert "FAKEPASS123" not in text, (
                "an SMTP error that echoes the credential verbatim would put the "
                "App Password in the log"
            )


class TestNoDuplicateSend:
    def test_repeated_tool_call_in_one_turn_stages_one_draft(self, _mail, _quiet, caplog):
        """A re-issued draft inside one user message must not stage twice.

        The guard is `_tool_cache_key` in `zoe/brain.py`: a side-effecting
        tool collapses to its name alone, so a model that issues `send_email`
        twice in one turn gets the first result back. Nothing is sent either
        way; the draft is staged once.
        """
        state = {"n": 0}

        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                state["n"] += 1
                if state["n"] < 2:  # model re-requests the same draft
                    return ModelResponse(text=None, tool_calls=[_call()])
                return ModelResponse(text="Drafted.")
            return ModelResponse(text=None, tool_calls=[_call()])

        def _call():
            return ToolCall(id=new_tool_call_id(), name="send_email",
                            arguments={"to_address": RECIPIENT,
                                       "subject": SUBJECT, "message": BODY})

        layer = build_layer(
            models=[{"key": "m", "model": "m", "priority": 90,
                     "capabilities": {"reasoning": True, "tool_calling": True}}],
            behaviours={"m": _script},
        )
        brain = ZoeBrain(model_layer=layer)
        with caplog.at_level("INFO", logger="zoe"):
            assert brain.ask("email alice the report") == "Drafted."
        assert _sends() == [], "a draft reached SMTP"
        assert brain._pending_email is not None, "the draft was not staged"
        staged = [r for r in caplog.records if "draft staged" in r.message]
        assert len(staged) == 1, f"draft staged {len(staged)} times"

    def test_an_unconfirmed_turn_cannot_be_escalated_into_a_send(self, _mail, _quiet):
        """A model cannot smuggle a confirmation in through the arguments.

        `send_email` takes no `confirmed` argument, so a tool call that carries
        one is rejected as unexpected and never stages a draft -- whatever the
        model chose to put in the call.
        """
        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                return ModelResponse(text="Done.")
            return ModelResponse(text=None, tool_calls=[_call(True)])

        def _call(confirmed):
            return ToolCall(id=new_tool_call_id(), name="send_email",
                            arguments={"to_address": RECIPIENT,
                                       "subject": SUBJECT, "message": BODY,
                                       "confirmed": confirmed})

        layer = build_layer(
            models=[{"key": "m", "model": "m", "priority": 90,
                     "capabilities": {"reasoning": True, "tool_calling": True}}],
            behaviours={"m": _script},
        )
        brain = ZoeBrain(model_layer=layer)
        brain.ask("email alice the report")
        assert _sends() == [], "an unconfirmed turn reached SMTP"
        assert brain._pending_email is None, "a rejected call must not stage a draft"


class TestModelCannotSend:
    """The model may draft an email; it may never send one.

    Confirmation is not an argument: `send_email` stages a draft in the
    conversation, and the send happens only on the user's own next turn.
    """

    def _layer(self, script):
        return build_layer(
            models=[{"key": "m", "model": "m", "priority": 90,
                     "capabilities": {"reasoning": True, "tool_calling": True}}],
            behaviours={"m": script},
        )

    def _draft_call(self, **overrides):
        args = {"to_address": RECIPIENT, "subject": SUBJECT, "message": BODY}
        args.update(overrides)
        return ToolCall(id=new_tool_call_id(), name="send_email", arguments=args)

    def test_a_model_tool_call_with_confirmed_true_never_sends(self, _mail, _quiet):
        """The acceptance gate: `confirmed=True` in the arguments is rejected."""
        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                return ModelResponse(text="done")
            return ModelResponse(text=None,
                                 tool_calls=[self._draft_call(confirmed=True)])

        brain = ZoeBrain(model_layer=self._layer(_script))
        brain.ask("email alice the report")
        assert _sends() == [], "a model tool call with confirmed=True reached SMTP"
        assert brain._pending_email is None, "a rejected call must not stage a draft"

    def test_a_draft_then_yes_sends_exactly_once(self, _mail, _quiet):
        """The acceptance path: draft, then the user's next turn says yes."""
        state = {"drafted": False}

        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                return ModelResponse(text="Drafted; shall I send it?")
            if not state["drafted"]:
                state["drafted"] = True
                return ModelResponse(text=None, tool_calls=[self._draft_call()])
            return ModelResponse(text="ok")

        brain = ZoeBrain(model_layer=self._layer(_script))
        brain.ask("email alice the report")
        assert _sends() == [], "the draft sent before the user confirmed"
        assert brain._pending_email is not None

        brain.ask("yes")
        sent = _sends()
        assert len(sent) == 1, f"expected exactly one send, got {len(sent)}"
        assert sent[0]["To"] == RECIPIENT
        assert brain._pending_email is None

    def test_a_draft_then_an_unrelated_question_never_sends(self, _mail, _quiet):
        """The acceptance guard: an unrelated turn drops the draft, never sends."""
        state = {"drafted": False}

        def _script(history, payload):
            if payload.get("kind") == "tool_results":
                return ModelResponse(text="Drafted; shall I send it?")
            if not state["drafted"]:
                state["drafted"] = True
                return ModelResponse(text=None, tool_calls=[self._draft_call()])
            return ModelResponse(text="Here is a summary.")

        brain = ZoeBrain(model_layer=self._layer(_script))
        brain.ask("email alice the report")
        assert brain._pending_email is not None

        brain.ask("summarize my meeting notes")
        assert _sends() == [], "an unrelated turn sent a stale draft"
        assert brain._pending_email is None, "the draft must be cleared"

    def test_a_draft_then_no_never_sends(self, _mail, _quiet):
        brain = ZoeBrain(model_layer=self._layer(
            lambda h, p: ModelResponse(text="Okay, I won't send it.")))
        brain.stage_email_draft(RECIPIENT, SUBJECT, BODY)
        brain.ask("no")
        assert _sends() == [], "a declined draft reached SMTP"
        assert brain._pending_email is None

    def test_a_stale_draft_is_not_sent(self, _mail, _quiet, monkeypatch):
        monkeypatch.setattr(brain_module, "EMAIL_CONFIRM_TIMEOUT_S", 0)
        brain = ZoeBrain(model_layer=self._layer(
            lambda h, p: ModelResponse(text="That draft is stale.")))
        brain.stage_email_draft(RECIPIENT, SUBJECT, BODY)
        brain.ask("yes")
        assert _sends() == [], "an expired draft was sent"
        assert brain._pending_email is None

    def test_a_second_draft_replaces_the_first(self, _mail, _quiet):
        brain = ZoeBrain(model_layer=self._layer(lambda h, p: ModelResponse(text="ok")))
        brain.stage_email_draft("first@example.com", "first", "body one")
        brain.stage_email_draft(RECIPIENT, SUBJECT, BODY)
        brain.ask("yes")
        sent = _sends()
        assert len(sent) == 1, f"expected exactly one send, got {len(sent)}"
        assert sent[0]["To"] == RECIPIENT, "the superseded draft was the one sent"


class TestAllPathsProtected:
    def test_registry_never_forwards_a_model_tool_call_to_a_send(self, _mail, _quiet, monkeypatch):
        """A model tool call stages a draft; it does not reach the gated send.

        The Brain's `send_email` wrapper takes no `confirmed` argument and never
        calls `email_tool.send_email`. If it ever did, the recorded call below
        would be non-empty and this fails.
        """
        seen = []
        monkeypatch.setattr(email_tool, "send_email",
                            lambda *a, **k: seen.append((a, k)) or "Email sent.")
        brain = ZoeBrain(model_layer=build_layer(
            models=[{"key": "m", "model": "m", "priority": 90,
                     "capabilities": {"reasoning": True, "tool_calling": True}}],
            behaviours={"m": lambda h, p: ModelResponse(text="ok")},
        ))
        out = brain._execute_tool_call("send_email", {
            "to_address": RECIPIENT, "subject": SUBJECT, "message": BODY})
        assert seen == [], "a model tool call reached the gated send"
        assert brain._pending_email is not None, "the tool call did not stage a draft"
        assert "confirm" in out.lower(), "the draft was not explained back to the model"

    def test_the_gate_is_the_only_thing_that_sends(self, _mail, _quiet, monkeypatch):
        """Replaces `test_interactive_handler_is_gated`.

        `email_tool.handle_email_command` no longer exists: the interactive
        prompt that used to live in this module was removed, because a tool
        cannot ask the user a question the conversation has not already answered.
        Confirmation is now the Brain's job. The guarantee this test used to
        make -- a contact-resolved send still cannot escape the gate -- is now
        expressed against the real boundary: contact resolution picks the
        address, and the gate still decides.
        """
        monkeypatch.setattr(email_tool, "CONTACTS", {"mom": "mom@example.com"})

        resolved = email_tool.resolve_contact("mom")
        assert resolved == "mom@example.com"

        # Not confirmed -> nothing reaches SMTP, whatever the address was.
        email_tool.send_email(resolved, "hello", BODY, confirmed=False)
        assert _sends() == [], "a contact-resolved send skipped the gate"

        # Confirmed -> exactly one send, to the resolved address.
        email_tool.send_email(resolved, "hello", BODY, confirmed=True)
        sent = _sends()
        assert len(sent) == 1, f"expected exactly one send, got {len(sent)}"
        assert sent[0]["To"] == "mom@example.com"
