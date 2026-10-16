# Changelog

All notable changes to this project are recorded here.

This project has no release tags. **There are deliberately no historical release
notes**: rather than reconstruct entries from commit messages, the unreleased
work is described below and the dated record stays in
[`docs/history/`](docs/history/).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims for semantic versioning once a licence is chosen and a version
is tagged.

## [Unreleased]

### Added

- **Electron desktop UI** (`frontend/zoe/`): React 19 + Vite 8 renderer in an
  Electron 44 window, with text and microphone input and streamed spoken
  replies.
- **`server.py`**: a localhost HTTP bridge from the UI to the same
  `ZoeBrain` the terminal uses — `/health`, `/chat`, `/transcribe`, `/tts`,
  `/conversation/reset`. CORS is restricted to six localhost origins,
  conversations are pooled to 64 with oldest-first eviction, uploads are capped
  at 25 MB and synthesis input at 4000 characters.
- **Frontend voice state machine** with 20 unit tests (`node --test`).
- **Acknowledgement** (`voice.ack_*`): a cached clip plays when a reply has not
  started within `ack_delay_ms`, through the same playback queue so barge-in and
  cancel still work.
- **File-reading plugin** beyond directory listing: `txt`, `md`, `csv`, `json`,
  `pdf`, `docx`, `xlsx`, resolved against `file_reader.roots` and opened with
  `O_NOFOLLOW`.
- Application-launch denylist for sensitive applications requiring explicit
  confirmation.

### Changed

- **The Python package was renamed `jarvis` → `zoe`.** All modules, imports,
  tests, scripts and documentation follow it. `python -m zoe` is the entry
  point. The old name is preserved in git history and in `docs/history/`.
- The Electron UI moved from `Zoe-AI-Assistant-main/` to `frontend/zoe/` and is
  now tracked in git. It previously existed only as untracked local files.
- `pyproject.toml` package metadata now points at `zoe`; it still declared
  `jarvis`, so `pip install .` packaged nothing.

### Fixed

- The credential-literal scan in `tests/test_integration.py` pointed at
  `jarvis/`, which the rename had emptied. It was checking one file and passing
  vacuously. It now covers all 47 modules.

### Known limitations

These are current and measured, not regressions:

- Reverberant rooms: the single-tap echo canceller removes 0 % of echo energy at
  200 ms RT60, so barge-in self-interrupts. Six `tests/test_echo_reverb.py`
  cases fail for this reason and eleven more are marked `xfail`.
- `electron.cjs` loads the Vite dev-server URL unconditionally, does not start
  the backend, and has no `setWindowOpenHandler` or `will-navigate` guard.
- Memory recall is lexical and has no recall benchmark.
- There is no retention or deletion policy for `data/`, and no LICENSE file.

### Security

No secret has ever been committed in this repository's history. `.env` has never
been tracked, and `.env.example` holds placeholders only.

Outstanding, unresolved: indirect prompt injection through read content is
mitigated for one action (email send) and not otherwise. See
[SECURITY.md](SECURITY.md).