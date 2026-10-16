"""Secure email tool for Zoe.

SMTP over TLS, credentials from environment variables only -- never stored,
never logged.

Two properties matter more than the feature:

* **The body is never logged.** Recipient and subject may be; content never is.
* **Nothing is sent without an explicit confirmation** on every path. There is
  no timeout, no default-yes, and no partial match: silence is not consent.
"""

import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from zoe.config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, CONTACTS
from zoe.logger import logger
from zoe.providers.base import redact

#: Explicit affirmative replies. Anything else -- no, cancel, "sure?", "ok",
#: empty, "none" from a non-interactive session -- is unconfirmed.
AFFIRMATIVE = frozenset({
    "yes", "y", "yeah", "yep", "yes please", "send it", "do it",
    "confirm", "confirmed", "go ahead", "send",
})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_confirmation(reply) -> bool:
    """True only for a clearly affirmative confirmation. Strict by design."""
    text = " ".join(str(reply or "").strip().lower().split()).rstrip("?!.")
    return (text in AFFIRMATIVE or text.startswith("yes ")
            or text.startswith("yes,"))


def _safe(message: str) -> str:
    """Error text safe to log and to return.

    Key-shaped strings are redacted by prefix, but an SMTP error can echo this
    account's own app password verbatim, and that string looks like ordinary
    words. Removing the literal credential is the only thing that catches it.
    """
    text = redact(message)
    secret = str(GMAIL_APP_PASSWORD or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def resolve_contact(query: str):
    """Email address for a contact alias mentioned in `query`, or None."""
    lowered = (query or "").lower()
    for name, address in (CONTACTS or {}).items():
        if name.lower() in lowered:
            return address
    return None


def send_email(to_address: str, subject: str, content: str,
               confirmed: bool = False) -> str:
    """Send an email, if the caller already has the user's confirmation.

    The confirmation gate is on ``confirmed``, not on a prompt read here: a
    tool cannot ask the user a question the conversation has not already
    answered. The Brain sets ``confirmed`` only after the user has said yes in
    their own turn, which is also the only place the answer is available.

    Args:
        to_address: Recipient.
        subject: Subject line.
        content: Message body.
        confirmed: True only when the user explicitly approved this send.

    Returns:
        A short status string. Never raises.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning("send_email called without GMAIL_ADDRESS/GMAIL_APP_PASSWORD set.")
        return "Email is not configured: GMAIL_ADDRESS and GMAIL_APP_PASSWORD are missing from .env."

    recipient = (to_address or "").strip()
    if not _EMAIL_RE.match(recipient):
        logger.warning(f"Refusing to send to a malformed address: {to_address!r}")
        return f"'{to_address}' is not a valid email address, so nothing was sent."

    if not confirmed:
        logger.info(f"Email to {recipient} NOT sent: no explicit confirmation.")
        return ("This email was not sent. Ask the user to confirm first, then "
                "call send_email again with confirmed=true.")

    logger.info(f"Sending email: recipient={recipient} subject={subject!r}")
    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = recipient
        msg["Subject"] = subject or ""
        msg.attach(MIMEText(content or "", "plain"))

        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        try:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        finally:
            # Its own try/except, deliberately. `quit()` on a session whose login
            # already failed raises again, and in a `finally` block that second
            # exception *replaces* the first -- so the real cause was replaced by
            # "please run connect() first", which named a network problem that
            # did not happen. Debug level, no traceback: this is teardown noise,
            # not the story of the failure.
            try:
                server.quit()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"SMTP quit after a failed send reported: {_safe(str(e))}")

        logger.info(f"Email sent: recipient={recipient} subject={subject!r}")
        return f"Email sent to {recipient}."
    except smtplib.SMTPAuthenticationError as e:
        # Before the generic handler and before SMTPResponseException, because
        # SMTPAuthenticationError is a subclass of it. This is the single most
        # common failure here and its cause is never in the exception text, so it
        # gets one fixed, actionable message rather than a raw SMTP transcript.
        detail = _safe(str(e))
        logger.error(f"Email send rejected for {recipient}: smtp_auth_failed "
                     f"{getattr(e, 'smtp_code', 535)} {detail}")
        return ("Gmail rejected the App Password (535). Create a new App Password "
                "for this account in Google Account > Security > App passwords, "
                "then set GMAIL_APP_PASSWORD in .env.")
    except smtplib.SMTPResponseException as e:
        # Every other rejection the server can make -- unknown recipient, blocked
        # sender, bad sequence. The code is in the text; it is the server's reason.
        reason = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        detail = _safe(reason)
        logger.error(f"Email send rejected for {recipient}: smtp {e.smtp_code} {detail}")
        return f"Gmail rejected the email (SMTP {e.smtp_code}): {detail}"
    except Exception as e:  # noqa: BLE001
        detail = _safe(str(e))
        # No exc_info: the traceback would carry the unredacted exception text.
        logger.error(f"Email send failed for {recipient}: {detail}")
        return f"I could not send the email to {recipient}: {detail}"
