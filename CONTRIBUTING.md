# Contributing

Thanks for looking. This is student project work on a personal assistant, and
the bar is "true and reproducible", not "impressive".

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[server,reader]"
cp .env.example .env        # add at least one enabled provider key
cd frontend/zoe && npm install
```

The full guide, including optional speech dependencies and macOS permissions,
is [docs/SETUP.md](docs/SETUP.md).

## Before you open a pull request

```bash
python -m pytest -q                        # must not get worse than 6 known failures
python scripts/gen_config_reference.py --check   # only if you touched config.yaml or zoe/config.py
cd frontend/zoe && npm test && npm run lint
```

The 6 failures in `tests/test_echo_reverb.py` are a documented architectural
gap (single-tap echo canceller in a reverberant room), not a broken baseline.
See [docs/TESTING.md](docs/TESTING.md). If your change fixes one, say so
explicitly and update the xfail.

Tests must pass with no network and no hardware. If something genuinely cannot
run offline, skip it with the real reason rather than faking a pass.

## Branches

- `feat/…`, `fix/…`, `docs/…`, `chore/…`
- One topic per branch. Do not reformat code you are not changing.
- Never force-push.

## House rules

- **Do not change behaviour silently.** This assistant reads files, opens
  applications and sends email. A change to tool registration, model routing,
  memory, the email approval gate or the launch allowlist is a behaviour change:
  say so in the description and get it agreed first.
- **Never commit secrets.** `.env` is ignored; keep it that way. `.env.example`
  gets placeholders only.
- **Do not weaken a safety check to make a test pass.** If a check is wrong,
  fix the check and explain why. The email approval gate, the application
  allowlist and the file-root check are the load-bearing parts.
- **Move nothing without grepping.** Several tests read source by path relative
  to the project root, so a rename breaks them *silently* until you run the
  suite. That already happened once with the `jarvis` → `zoe` rename: the
  credential-literal scan was pointed at an empty directory and passed without
  checking anything.
- **Assert structure, not wording.** Model phrasing varies by provider and run.
- **State the evidence.** If a claim is measured, say where the number came
  from and what it excludes. If it is not measured, label it.

## Documentation

Docs are part of the change, not a follow-up.

- `docs/CONFIGURATION.md` is **generated**. Edit `config.yaml` or
  `zoe/config.py` and re-run `python scripts/gen_config_reference.py`; never
  hand-edit the file.
- Claimed features carry a status: tested, live, partial or proposed. Do not
  promote a proposed feature to tested without a test.
- New dependencies go in `requirements.txt` with a note on what degrades when
  they are absent.

## Issues

Say what you did, what you expected, what happened, and which commit. Logs from
`data/zoe.log` are useful — but strip keys first, and never attach `.env`.

## Licence

There is no licence file yet, and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records an unresolved
non-commercial attribution. Do not assume you can reuse this code commercially.