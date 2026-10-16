# Security Policy

## Reporting a vulnerability

Open a private security advisory on the repository
(<https://github.com/Simp0099/Personal-Ai-Partner/security/advisories/new>).
Please include the version or commit, the reproduction steps, and what an
attacker gains. Do not open a public issue for an unfixed vulnerability.

This is student project work with no formal response SLA. You can expect an
acknowledgement, not a same-day fix.

## What this software does

Zoe is a personal desktop assistant that can read files, open allowlisted
applications, capture the screen and (opt-in) the webcam, and send email on the
user's behalf. It is a **working prototype, not a hardened product.** It is not
suitable as the only control on a machine that holds anything you would mind
losing.

## Supported versions

Only the current default branch is supported. There are no released tags and no
maintenance window for older commits.

## Secret handling

- Secrets live only in `.env`, which is git-ignored and has never been committed
  in this repository's history.
- `.env.example` contains placeholders only.
- Nothing in the assistant should log a key. `zoe/providers/base.py::redact`
  and `zoe/tools/email_tool.py::_safe` exist for that; if you add a code path
  that handles a credential, route it through them.
- `tests/test_integration.py` scans the package for credential-shaped string
  literals. If you add a module under `zoe/`, it is covered automatically.

If you ever commit a real key: rotate it first, then remove it from the file.
Assume anything pushed is compromised even if you delete it in a later commit —
history rewriting does not un-publish it.

## Trust boundaries

### The model is untrusted input, not an authority

The language model chooses which tool to call and supplies its arguments. It
cannot complete a consequential action on its own.

**Email.** The model's `send_email` tool only *stages* a draft and returns the
body. The real send function is not in the model's tool registry and is
reachable only from `ZoeBrain._resolve_pending_email`, which runs on the user's
own next turn and only for a strict affirmative parsed by
`zoe/tools/email_tool.py::parse_confirmation`, within
`EMAIL_CONFIRM_TIMEOUT_S` (default 120 s). A draft is cleared on any turn,
including an unconfirmed one. See `docs/ARCHITECTURE.md`.

### Applications

`applications.aliases` in `config.yaml` is the allowlist; anything absent is
refused. Launches use argument lists, never a shell, so a spoken name cannot
become shell syntax. `applications.denylist` names sensitive applications that
need explicit confirmation.

### Files

The `file_reader` plugin resolves the path (following symlinks), checks it
against `file_reader.roots`, then opens with `O_NOFOLLOW` and checks the size on
the open descriptor. Do not reorder those steps: checking the path and opening
it later is a time-of-check/time-of-use hole. Root defaults to `~/Documents`,
`~/Downloads`, `~/Desktop` — narrow these if your documents are sensitive.

### Indirect prompt injection

**This is the main unresolved threat.** Any text the assistant reads — a web
page, an email, a document, an image — can influence the model. The approval
gate stops that content from *authorising* a send, but it does not stop it from
*shaping* the draft. A crafted document can still cause the assistant to ask you
to confirm something you would not otherwise have asked for.

Do not describe Zoe as resistant to prompt injection. It reduces the blast
radius of one action and nothing else.

### Plugins are not a sandbox

Plugins are validated, name-checked against built-ins, timeout-bounded and
crash-contained, but they execute **inside the assistant's process** with the
same OS permissions you do. There is no capability model, no dependency trust
policy and no versioning. A plugin is arbitrary code you chose to run.

Treat `plugins/*.py` as untrusted: read it before it is added, and do not
install plugins from an unknown source.

### Electron

`contextIsolation` is on and `nodeIntegration` is off. The preload exposes three
window-control functions and nothing else.

Known gaps, not yet fixed: `electron.cjs` has no `setWindowOpenHandler` or
`will-navigate` guard, so a link rendered by the UI could open a window outside
those controls, and its `ipcMain` handlers do not verify the sender's frame.
The renderer is a client of a localhost API, so anything it can be made to
render is worth treating as untrusted input.

## Privacy and data retention

`data/` is git-ignored and holds everything at runtime: the SQLite memory
database, rolling logs, diagnostic WAV recordings and screenshots.

**There is no retention or deletion policy.** Nothing expires on its own, and
the assistant has no command to list or delete stored facts. Memory holds
whatever you told it, and it refuses to store obviously key-shaped strings at
write time — that is a guard on one path, not a policy.

If that matters to you, delete `data/` yourself while the assistant is stopped.

Screenshots and webcam frames can contain personal data. The webcam is opt-in
(`vision.webcam.enabled`, off by default) and nothing it captures is written to
memory — but the model still sees it.

## What is implemented vs. recommended

Implemented: the email approval gate, the application allowlist, the file-read
root check with `O_NOFOLLOW`, secret redaction in logs, the `.env`-only secret
store, plugin timeout and crash containment.

Recommended, **not** implemented: a plugin capability model, a memory retention
and deletion policy, an audit log of consequential actions, prompt-injection
evaluation, a double-talk detector, and navigation guards in the Electron main
process.

## Not in scope

Zoe is not a security product and does not replace human judgement about
health, money or safety. It does not defend against a malicious local user, a
compromised OS, or a malicious model provider.