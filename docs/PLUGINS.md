# Plugins

Drop one `.py` file into `plugins/` and it becomes a tool the model can call on
the next launch. No registration, no manifest, no import to edit.

`plugins/_template.py` is the starting point. Files beginning with `_` are
templates and are never loaded, so it is safe to keep in the directory.

## The contract

Two module-level names are required. Everything else is optional.

```python
TOOL = {
    "name": "word_count",                      # required
    "description": "Count the words in a piece of text.",   # required
    "parameters": {"text": "str"},             # optional, defaults to {}
}

def run(text: str) -> str:                     # required
    return f"{len(text.split())} words"
```

`TOOL["name"]` and `TOOL["description"]` are what the model sees. The
description is not documentation — it is the model's only evidence for deciding
when to call the tool, so write it for that audience.

`run` is called with keyword arguments matching `parameters` and must return a
string. Return short: the result goes to the model as tool output, and a wall of
text is worse than no tool at all.

## Validation

Enforced by `zoe/plugins.py::_validate` before a plugin is ever exposed.

| Rule | Limit |
| --- | --- |
| `TOOL` must be a dict | — |
| `name` must be snake_case | 2–48 chars, starting with a letter |
| `name` must not collide with a built-in tool | rejected |
| `name` must not collide with another plugin | rejected; first file wins, and a duplicate spelling is not an override |
| `description` must be a non-empty string | ≤ 500 chars |
| `parameters` must be a dict of name → type | ≤ 8 parameters |
| parameter names must be snake_case | ≤ 32 chars |
| parameter types | exactly `str`, `int`, `float`, `bool` |
| `run` must be callable | — |

Every parameter is **required** — there is no optional or defaulted parameter.
Booleans accept `"true"`/`"yes"`/`"1"` from a model that stringified them.

## Failure handling

The contract is that a plugin can never take down the assistant.

- **Rejected at load.** A bad `TOOL`, an import error, a name collision, or a
  plugin that calls `sys.exit()` while importing: logged with the reason at
  load, the plugin is skipped, everything else keeps working.
- **Raised at call.** `run` raising returns `Plugin '<name>' failed: <error>` to
  the model as the tool result. The turn continues and the model can report the
  failure honestly.
- **Too slow.** `run` still going after `plugins.timeout_s` (default 10 s)
  returns `Plugin '<name>' took longer than N seconds and was stopped.` The call
  is abandoned; the thread is not joined, so a plugin that ignores the timeout
  keeps running until it finishes.
- **Return value.** `None` becomes `""`, and the result is truncated to
  `plugins.max_result_chars` (default 4000). A result cut here loses whatever
  the plugin put at the end, so a plugin returning a document raises this above
  its own content budget.

Timeouts use a one-worker thread pool per call. That bounds *waiting*, not
execution — a plugin that ignores cancellation keeps running until it finishes.
Do not write plugins that loop forever.

## Shipped plugins

| File | Tool | Notes |
| --- | --- | --- |
| `plugins/file_reader.py` | `read_file` | Reads `.txt`, `.md`, `.log`, `.csv`, `.tsv`, `.json` from the standard library. `.pdf`, `.docx`, `.xlsx` need the optional `reader` extra: `pip install -e ".[reader]"`. Without them it returns the install command rather than the contents |
| `plugins/word_count.py` | `word_count` | Counts words in a string |

`read_file(path, question)` returns extracted text framed as untrusted data.
`question` is what the user asked; pass `""` to get the summary-oriented short
read. It is restricted to `file_reader.roots` in `config.yaml`
(`~/Documents`, `~/Downloads`, `~/Desktop` by default). A path outside those
roots, a symlink whose target resolves outside them, or a file over
`file_reader.max_bytes` is rejected with a plain message. Note this is a
*different* setting from `file_access.allowed_roots`, which governs the
directory-listing built-in.

What the plugin does about a hostile file:

| Risk | Response |
| --- | --- |
| Traversal, symlink escape, read outside the roots | `resolve()` then a containment check against the roots. One check, so the three cases cannot drift apart. |
| File swapped for a link between check and read | Opened once with `O_NOFOLLOW`; the size limit is `fstat` on that descriptor, so the bytes checked are the bytes read. |
| Directory, FIFO, device presented as a document | Refused at open: only a regular file is read. |
| Instructions inside a document | Contents come back between `BEGIN FILE` / `END FILE` fences, labelled as data to report on and not obey. |
| Execution or macros | No subprocess, no `exec`, no write action. `docx`/`xlsx` are unzipped as XML; macros do not run. |
| Memory | Text is read only as far as the character budget needs; containers are bounded by `max_bytes`. |
| Time | The loader's `plugins.timeout_s` abandons a call that overruns. |
| Truncation | Announced in the output with the real total, so the model knows it is looking at part of the file. |
| Oversized / unsupported / malformed / missing / unreadable | A plain sentence. A parser blowing up returns `Could not read '<name>' as <type>: <reason>`. |

Known ceilings, each marked `ponytail:` in the source: 50 PDF pages per call,
DOCX paragraphs only (no tables or headers), and `.doc`/`.pptx`/images are not
supported at all.

## Testing a plugin

`zoe.plugins.load_callables(plugin_dir, reserved, timeout_s)` returns the
model-callable wrappers; `zoe.plugins.describe(plugin_dir, reserved)`
returns every file and whether it loaded, with the rejection reason and the
name it claimed. `reserved` defaults to the real built-in tool names in both, so
a plugin cannot look valid here and be rejected at load.

```python
from zoe.config import PROJECT_ROOT
from zoe import plugins

records = plugins.describe()                       # what loaded, and why not
tools = plugins.load_callables(PROJECT_ROOT / "plugins", timeout_s=5)
```

Pass `reserved=` the built-in `TOOL_REGISTRY` names, exactly as
`zoe/brain.py` does. `describe()` defaults to them; `load_callables()` does
not, because `reserved` is its first positional argument and a caller that
supplies one means it. `tests/test_plugins.py` does this and is the fastest way
to check a new plugin loads.

[PLUGIN_AUDIT.md](PLUGIN_AUDIT.md) covers how this loader compares with Mark
LV's, and why the differences are not worth collapsing.