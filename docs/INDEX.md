# Documentation index

Everything in `docs/`, one line each. The project overview is the
[root README](../README.md).

Files under `docs/archive/` use the old project name Jarvis on purpose; they are
dated records.

## Current

| Document | What it covers |
| --- | --- |
| [SETUP.md](SETUP.md) | Install, optional dependencies, macOS permissions, the backend, and the Electron UI |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The voice and text request paths, the local-intent bypass, the three state machines, and the UI bridge |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Repository layout, local workflow, and how to add a tool, a plugin or UI functionality |
| [FRONTEND.md](FRONTEND.md) | Electron architecture, the preload bridge, the `server.py` HTTP bridge, and the known integration gaps |

| [CONFIGURATION.md](CONFIGURATION.md) | Every `config.yaml` key and every `CONFIG` read in `zoe/config.py`. **Generated** by `scripts/gen_config_reference.py` — do not edit by hand; run `python scripts/gen_config_reference.py --check` to prove it is current |
| [TESTING.md](TESTING.md) | Running the suites (backend and frontend), the `requires_kokoro` marker, live scripts, and which checks need hardware |
| [PLUGINS.md](PLUGINS.md) | The plugin contract from `plugins/_template.py` and `zoe/plugins.py` |
| [PLUGIN_AUDIT.md](PLUGIN_AUDIT.md) | Audit of the loader against Mark LV's, the differences, and what was deliberately not ported |
| [STATUS.md](STATUS.md) | Acceptance table with evidence, including what is **NOT VERIFIED** |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Known failure modes, and what to do about each |
| [VOICE_LATENCY.md](VOICE_LATENCY.md) | Measured voice latency, each figure with its method and exclusions |
| [wake_phrases.md](wake_phrases.md) | Which wake phrases are verified, and why the rest are disabled |
| [barge_in_diagnosis.md](barge_in_diagnosis.md) | Measurements behind the self-interruption (barge-in) guard |
| [archive/PHASE_E_MANUAL_CHECKLIST.md](archive/PHASE_E_MANUAL_CHECKLIST.md) | Ordered manual hardware checklist. **Nothing in it may be marked PASS from the file itself** |

Project-level documents live at the repository root: [README.md](../README.md),
[SECURITY.md](../SECURITY.md), [CONTRIBUTING.md](../CONTRIBUTING.md),
[CHANGELOG.md](../CHANGELOG.md),
[LICENSE](../LICENSE) (MIT) and
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) (third-party licence
status).

## History

`docs/archive/` holds superseded phase reports, dated baselines, the manual
hardware checklist and one internal UI working note. Absolute machine paths in
the archived baselines were replaced with `<PROJECT_ROOT>`; the recorded text is
otherwise verbatim. It also holds
reconstruction logs. They are kept because they are the record of what was
decided, not of what is there — several describe files and behaviour that no
longer exist. **Do not follow them for current behaviour**; follow the links in
the table above.

| File | What it records |
| --- | --- |
| [baseline.txt](archive/baseline.txt) | Phase 0 baseline, 2026-10-08 |
| [baseline_mac.txt](archive/baseline_mac.txt) | Full pytest run output — the dated suite record |
| [baseline_tests_phase1.txt](archive/baseline_tests_phase1.txt) | Phase 1 focused run |
| [PHASE0_DIAGNOSIS.md](archive/PHASE0_DIAGNOSIS.md) | The Gemini-only system as it was on 2026-10-06. Pinned by `tests/test_phase0_regression.py` |
| [REPAIR_LOG.md](archive/REPAIR_LOG.md) | Reconstruction of the damaged archive, file by file |
| [PHASE_E_COMPLETION_REPORT.md](archive/PHASE_E_COMPLETION_REPORT.md) | The Phase E acceptance determination, summarised in `STATUS.md` |
| [PHASE_E_FULL_SUITE_REVALIDATION.md](archive/PHASE_E_FULL_SUITE_REVALIDATION.md) | The pre-fix suite state: 11 failed, 32 errors |
| [PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md](archive/PHASE_E_FIRST_AUDIO_REGRESSION_REPORT.md) | The `first_audio` regression and its fix |
| [final_verification_phase10.md](archive/final_verification_phase10.md) | Release-readiness verdict: READY WITH KNOWN LIMITATIONS |
| [latency_baseline_phase9.md](archive/latency_baseline_phase9.md) | Turn-tracing design and overhead measurements |
| [phase1_audio_verification.md](archive/phase1_audio_verification.md) | Phase 1 audio device verification |
| [tts_baseline.md](archive/tts_baseline.md) | Chatterbox-era TTS baseline. **Engine no longer in the code path** |
| [tts_optimization_phase6.md](archive/tts_optimization_phase6.md) | Chatterbox-era optimisation report. **Engine no longer in the code path** |
| [tts_latency_optimization.md](archive/tts_latency_optimization.md) | Chatterbox-era latency analysis. **Engine no longer in the code path** |

`STATUS.md` §"Dated records" carries the same list with a note on which claims
in each file are still true.