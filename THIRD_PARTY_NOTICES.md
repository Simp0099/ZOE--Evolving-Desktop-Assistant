# Third-party notices and licence status

## This repository has no licence

There is no LICENSE file. Under default copyright, that means **All Rights
Reserved**: nobody may legally reuse, modify or redistribute this code until a
licence is added. That is a decision for the project owners, not something to
assume.

One item below may further constrain what can be licensed.

## Unresolved: `plugins/file_reader.py`

`plugins/file_reader.py` adapts the **tool half** of Mark LV's
`actions/file_processor.py`. The file's own docstring records this.

**What was taken:** the per-format extraction table and the per-format text
extraction for PDF, DOCX and XLSX.

**What was not taken:** the Gemini model call, the write actions (resize,
convert, `to_word`, `extract_text`-to-disk) and the action dispatch. Zoe's
plugin returns text and lets the model answer from it, so those parts are not
present.

**Why this is a problem.** The project paper (§13.3) records this component as
reported under **CC BY-NC 4.0**. That licence permits reuse with attribution
**for non-commercial purposes only**. It also requires the licence notice to
travel with the material.

**What that does and does not mean.**

- It does *not* mean this repository is CC BY-NC 4.0 as a whole. It means at
  least one file contains adapted material under those terms.
- If the project is ever used commercially, that file needs either a
  permission from the copyright holder or replacement with independently
  written code. The adaptation is small — the per-format extraction functions —
  so replacing it is realistic.
- The paper notes that the upstream repository location, version and licence
  file were never recorded, so the attribution cannot currently be completed
  with a version and a URL. **This needs the project owners to resolve.**

**Required before publication:** add an attribution notice for Mark LV's work
naming the upstream project and the CC BY-NC 4.0 licence, and decide whether to
replace the adapted code.

## Not third-party code

These are referenced, discussed or studied, but no third-party code is vendored
from them:

- The research paper's related work — prompt injection, LLM-agent security
  patterns, acoustic echo cancellation, full-duplex dialogue — is cited in
  `docs/research/Zoe_Research_Paper.docx`. Citations are attribution, not
  inclusion.
- The Rust crate the paper names for learned echo cancellation is a research
  direction. Nothing from it is in this repository.

## Runtime dependencies

Ordinary third-party packages, each under its own licence as declared by its
distributor:

- Python: `requirements.txt`, and the extras in `pyproject.toml`.
- Node: `frontend/zoe/package.json` and `frontend/zoe/package-lock.json`.

Neither list is vendored into the repository (`node_modules` and `.venv` are
git-ignored). Their licences are the upstream projects' concern; this file does
not restate them.

## Bundled assets

The Electron UI ships image assets in `frontend/zoe/public/` and
`frontend/zoe/src/assets/`. **Their provenance has not been verified** — this
file makes no claim about where they came from or whether they may be
redistributed. Resolve before publishing.

## Repo metadata

`github.com/Simp0099/Personal-Ai-Partner` has no licence metadata set, which is
consistent with the absence of a LICENSE file.