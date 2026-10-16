# Plugin system audit

An audit of Zoe's plugin system against Mark LV's `core/plugin_loader.py`,
`core/action_loader.py` and `plugins/_template.py`. Written after running both
loaders, not from reading them side by side.

**Outcome: keep Zoe's system as it is.** It is already at least as strict as
Mark LV's on every axis that matters, and the two designs are not
interchangeable. Three small defects were found and fixed; nothing was
restructured. Details and evidence below.

## What each system actually does

| | Zoe `zoe/plugins.py` | Mark LV `plugin_loader.py` + `action_loader.py` |
| --- | --- | --- |
| Declaration | `TOOL = {name, description, parameters}` | `PLUGIN = {...}` **and** `TOOL = {..., handler}` — two shapes, two loaders |
| Parameters | `{"a": "str"}` — flat, built by the loader | Full Gemini JSON schema: `{"type": "OBJECT", "properties": {...}, "required": [...]}` |
| `run()` signature | `run(**declared_params)` — the loader builds the signature | `run(parameters: dict, player=None, session_memory=None)`, dispatched by introspection |
| Arg validation | Every declared param coerced to its declared type; bad args return a sentence | None — `parameters` is handed over as a raw dict |
| Discovery | `sorted(glob("*.py"))`, `_`-prefixed skipped | `sorted(glob("*.py"))`, `_`-prefixed skipped |
| Registration | Wraps `run` into a typed callable, spliced into `TOOL_REGISTRY` | Two dicts, `get_tool_declarations()` for the API |
| Collisions | Rejects built-in names and duplicate plugin names | Same, plus `reserved_names` for actions |
| Isolation | Thread pool, per-call timeout, exception → sentence | None. `run()` catching is left to the plugin author |
| Enable/disable | No runtime toggle | `get_plugin_enabled` re-read per call |
| Extra features | — | `behavior`, `scheduling`, per-plugin settings schema, UI listing |
| Import-error message | `type: message` | Actionable: names a missing helper file and how to get it |

### Why Zoe's shape is the better one here

Three differences are not stylistic, and are the reason porting Mark LV's loader
would be a downgrade:

1. **Typed arguments are validated at the boundary.** Mark LV hands the plugin a
   raw dict and trusts it. Zoe coerces every parameter to its declared type
   before `run` sees it, so a model that stringifies `"220"` into an `int`
   parameter still gets an `int`, and a model that invents an argument cannot
   smuggle it through (verified: an undeclared kwarg is dropped, not forwarded).
2. **Timeouts are enforced by the framework.** Mark LV's template says "Never
   raise: catch your own errors... don't rely on it [the loader]" — the loader
   has no timeout at all. Zoe bounds every call and returns a sentence.
3. **One contract, not two.** Mark LV's `PLUGIN` and `TOOL` shapes overlap
   confusingly: `_validate` in each loader accepts a different dict, and the
   action loader silently skips a file with no `TOOL` while the plugin loader
   rejects it. Zoe has one `TOOL` and one `run`.

Mark LV's genuinely better ideas — the actionable import-error text and the
per-plugin settings schema — are UI concerns that do not apply to a project
whose plugins are maintained in-tree rather than downloaded by a user. Porting
them would add surface with no user to serve.

### Deliberately not copied

`behavior` / `scheduling` (a result re-entering the conversation) is tied to
Mark LV's live-VAD speech engine. Zoe's barge-in and playback queue already
own that path; a second, plugin-level scheduling channel would be a second way
to interrupt the user. `player` and `session_memory` injection has no Zoe
equivalent to inject.

## Findings

Three defects, all confirmed by running the loader. Each has a regression test.

### 1. `describe()` disagreed with the real loader — **fixed**

`describe()` called `_discover(..., reserved=set())`, so a plugin named
`send_email` was reported `valid: True` while `load_callables(reserved=...)`
rejected it. The diagnostic and reality could disagree about the single most
security-relevant question in the system.

Verified before the fix:

```
describe() says valid: [('send_email', True)]
loader(reserved) says: []
```

`describe()` now defaults `reserved` to the real `TOOL_REGISTRY` names, and
takes an explicit set to check collisions against something else.

### 2. A rejected record lost the plugin's name — **fixed**

A rejected record was appended with `name=""`, so `describe()` could say *that*
`impostor.py` was skipped but not *what* it claimed to be. The name is now kept,
which is what makes the error actionable.

### 3. A sub-second timeout was reported as "0 seconds" — **fixed**

`f"...{timeout_s:.0f} seconds"` on a `0.3`s cap told the model "took longer than
0 seconds and was stopped" — wrong, and unactionable for the user reading it.
Now `{timeout_s:g}`.

## What was checked and found already sound

- **Invalid declarations are rejected** with a reason, and one bad plugin never
  stops a good one. Import errors, `sys.exit()` at import, bad `TOOL`, bad
  parameter types, non-callable `run` — all covered by the existing suite.
- **Built-ins cannot be shadowed.** `zoe/brain.py:743` passes
  `reserved=TOOL_REGISTRY.keys()`. Verified: `TOOL_REGISTRY["send_email"]` is
  still `zoe.brain`, not the impostor.
- **Ambiguous registrations are refused.** Two plugins claiming one name: first
  by filename wins, second rejected with the reason. A duplicate spelling is not
  an override.
- **Failures do not crash the assistant.** A raising `run` returns
  `Plugin '<name>' failed: <error>`; the turn continues.
- **Timeouts and argument validation are the framework's job, not the model's.**
  Both enforced in `_make_callable`.
- **A plugin cannot reach the send path.** `brain.send_email` has no `confirmed`
  parameter; it routes to `stage_email_draft`, and only the user's own next turn
  reaches SMTP. Verified: a plugin calling it outside a turn gets "This email
  was not sent."
- **`file_reader` uses the contract correctly** — `TOOL` + `run(path, question)`,
  path safety enforced in the plugin rather than trusted from the model.

### Known and accepted

- **A plugin's module body executes at load**, before validation, so a rejected
  plugin's import-time code still ran (verified). Unavoidable: the declaration
  *is* the module. Mark LV has the same property. This is why plugins are
  in-tree and reviewed rather than downloaded.
- **A timed-out plugin keeps running.** The thread is abandoned, not killed —
  `future.result(timeout=)` bounds waiting, not execution. The plugin
  documentation already says so.
- **`reserved` is the caller's responsibility.** `load_callables()` with no
  `reserved` loads a plugin named `send_email`. Only `zoe/brain.py` calls it,
  and it passes the registry. A future second caller must do the same; the test
  `test_the_real_loader_still_rejects_what_describe_rejects` pins the behaviour
  against the real registry.

## On the Telegram plugin

There is no Telegram plugin in this project. `grep -ri telegram` across the
repo hits only one historical line in `docs/archive/REPAIR_LOG.md`. Mark LV's
`actions/send_message.py` routes to Telegram through desktop automation, which
is a different mechanism from a Zoe plugin and would not transfer directly.

Nothing was written. If one is wanted it must go through the same `TOOL` +
`run` contract, and — because sending is consequential — it belongs behind the
same staged-confirmation model as `send_email`, not as a direct action.