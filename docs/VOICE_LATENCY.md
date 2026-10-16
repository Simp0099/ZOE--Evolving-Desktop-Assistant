# Voice Latency Record

Filled in during Phase E (2026-10-11). Every number below was measured on this
machine by the method stated beside it. Values that were not measured are marked
**PENDING** and are not estimated.

## A. Environment

| Item | Value |
|---|---|
| macOS | 27.0 |
| Architecture | arm64 (Apple Silicon) |
| Python | 3.12.15 |
| Interpreter | `.venv/bin/python` (project virtualenv) |
| TTS engine | Kokoro, voice `af_heart`, speed 1.0 |
| TTS sample rate | 24000 Hz (Kokoro native) |
| ASR engine | local — faster-whisper `base.en` |
| Capture sample rate | 16000 Hz |
| Input device | MacBook Air Microphone |
| Output device | MacBook Air Speakers |
| Reported output latency | 0.02442 s |
| Wake word | enabled, `hey_jarvis` |

## B. TTS real-time factor

RTF = synthesis wall-clock time / generated audio duration.

| Item | Value |
|---|---|
| Text | `Hello there... how are you doing today? Good. Let us get started.` |
| Audio duration | 7.84 s |
| Warm synthesis | 1.37 s, 1.38 s (re-measured 2026-10-11) |
| **Warm RTF** | **0.17, 0.18** (median 0.18) |
| Earlier record (2026-10-10) | warm RTF 0.21, 0.18 |
| Target | below 1.0 — **met** |

Method: `.venv/bin/python scripts/tts_check.py`, run twice as two independent
fresh processes. Both runs printed the same audio duration and the same pause
list, so the figure is reproducible rather than a single lucky sample. The first
in-process call is also a cold call and is reported under C.

Pauses longer than 0.2 s detected in the rendered audio, identical across both
runs: `[0.36, 1.2, 1.12, 1.04]`. The phrase carries four pause-producing marks
(`...`, `?`, `.`, `.`), so the count matches what the segmenter should emit.

## C. Cold start

Measured in a **fresh interpreter** so nothing is preloaded; the timer wraps the
first `tts.synthesize()` call, which includes importing `kokoro`, loading the
pipeline and generating.

| Run | Synthesis | Audio | RTF |
|---|---|---|---|
| Fresh process (re-measured 2026-10-11) | 9.23 s | 7.84 s | 1.18 |
| Fresh process (re-measured 2026-10-11) | 8.77 s | 7.84 s | 1.12 |
| Fresh process (earlier record) | 9.81 s | 7.84 s | 1.25 |
| `scripts/tts_check.py` (earlier record) | 12.89 s | 7.84 s | — |

Cold start is **reported, not gated** by the specification. It is slower than the
2 s first-audio goal, which is why the engine is warmed before the microphone
opens (`tts.warm_up()` in `scripts/phase4_live_check.py`).

## D. Transcription

| Item | Value |
|---|---|
| Clip | `please tell me what time it is right now in the evening` |
| Clip duration | 3.48 s @ 16000 Hz, int16 (re-measured 2026-10-11) |
| Engine | faster-whisper `base.en` (local, model already cached) |
| Cold | 1.90 s |
| Warm | 0.49 s, 0.47 s, 0.47 s |
| **Median (warm)** | **0.47 s** |
| Transcript returned | `Please tell me what time it is right now in the evening.` |
| Earlier record | 3.10 s clip; cold 1.75 s; warm 0.61 s, 0.47 s; median 0.61 s |
| Target | under 1.0 s — **met** |

A second 1.80 s clip gave cold 1.65 s, median 0.46 s (earlier session). These are local-model
transcription times for short, clean, synthesised speech in a quiet-ish clip.
They do **not** represent all possible input: no measurement was made with
background speech, a noisy room, or a longer utterance.

## E. End of speech to first audio

**MEASURED 2026-10-12 — with a stated exclusion.**

Method: `.venv/bin/python scripts/first_audio_check.py --reps 5`. Both ends come
from marks the production code writes itself: `speech_end` (the VAD released the
floor and the turn began) and `first_audio` (audio queued, playback about to
begin). The figure is their difference, not a stopwatch added for the test.

Input: real Kokoro-synthesised speech, "Please tell me what time it is right now
in the evening", resampled to 16 kHz int16 and pushed through the real
`_on_frame` → `vad.push` path after 1.6 s of room tone for calibration.
Transcription was the real local `faster-whisper base.en`; synthesis was the real
Kokoro engine through the default `SpeechPlayer` synthesiser.

| Run | speech_end → first_audio |
|---|---|
| Cold (first run) | 2.329 s |
| Warm n=4 | min 1.346 s, **median 1.611 s**, max 1.977 s |

**What this number excludes: the model provider round trip.** The reply was
scripted, so the time between `[YOU]` and the provider's first token is zero in
this harness. On the real path that provider latency sits inside the same
interval, so **the true end-of-speech-to-first-audio figure is this number plus
the provider's round trip, which has not been measured.**

Therefore the specification's 2.0 s target is **NOT claimed to be met**. The
measured portion is under it; the total is unknown.

Two harness details were corrected before these numbers were believed, and are
recorded because both produced badly wrong figures:

- The injected room tone was first generated at 0.01 RMS, exactly
  `vad.energy_threshold`, so the detector correctly fired on the noise itself and
  split the utterance into two fragments that each transcribed to `""`. The tone
  was dropped to 0.0015 RMS, well under the threshold.
- The synthetic source originally returned `None` the instant its frames ran out.
  `MicrophoneStream._pump` has no sleep on that branch, so the capture thread
  busy-spun at 100% CPU and starved synthesis: a 2 s phrase took 11.8 s to
  synthesize, inflating the interval to ~17 s. The source now blocks for one
  frame period, the way real PortAudio does.

The earlier note that this row could not be automated at all was wrong about the
cause. The VAD does not need a live microphone; it needed room tone of a
sufficiently low level and a source that blocks.

**How to measure it (requires a human).** The voice loop already timestamps the
stages. Run:

```
.venv/bin/python start.py
```

Speak `hey jarvis`, then a short question. Then:

```
grep -E "\[YOU\]|chunks queued|playback turn=" data/zoe.log | tail -20
```

The gap from the `[YOU]:` line to the `[TTS] chunks queued` line for the same
turn is transcript → first audio; add the VAD end-of-speech time that precedes
`[YOU]` to get the full figure. Alternatively `scripts/phase4_live_check.py`
prints `transcript -> first audio` directly in check R.

## F. Results and limitations

| Measurement | Value | Status |
|---|---|---|
| Warm Kokoro RTF | 0.18 median (0.17, 0.18) | **verified 2026-10-11** |
| Kokoro cold start | 8.77–9.23 s (2 fresh processes) | **verified 2026-10-11** |
| Transcription, ~3 s clip | 0.47 s median warm (1.90 s cold) | **verified 2026-10-11** |
| End of speech → first audio | 1.611 s median warm (2.329 s cold), **excludes provider round trip** | `scripts/first_audio_check.py`, real marks | under 2.0 s | **not claimed — partial** |

Re-measurement note (2026-10-11): all three automated rows were re-run from a
clean `.venv` interpreter and agree with the earlier session within normal
run-to-run variation. No figure in this file is estimated.

Limitations:

- Transcription was measured with synthesised speech through the local
  faster-whisper model. The Google fallback path was not measured.
- Microphone speech capture is **verified**. The operator ran
  `scripts/mic_check.py` and reported RMS rising from ~0.05 in the initial
  window to **0.46-0.74 while speaking**, which satisfies the E2.1 "rising RMS
  while you speak" criterion. An unattended run on the same machine (nobody
  speaking) shows the device opens and delivers audio at RMS 0.021-0.186 and
  exits `OK. Audio is arriving.` Ambient variation alone does not satisfy the
  criterion and is not counted as if it did.
- Audio output is **crackling**. Observed by the operator and explicitly
  deferred; not investigated here and not counted as resolved.
- Human-observable checks still outstanding: intelligibility of the rendered
  speech, audible correctness of the punctuation pauses, live wake-word
  interaction, a follow-up without repeating the wake word, interruption and
  playback stability, and text mode with a clean Ctrl+C exit.
- Output latency reported by PortAudio is 24.4 ms; no round-trip measurement of
  the actual speaker-to-microphone delay was made.
- All figures come from this one machine in this one room.

## ASR model selection (2026-10-14)

`Transcriber` hard-coded `model_size="base.en"` and `compute_type="int8"` inline,
so neither was configurable. Both are now read from `asr.model` /
`asr.compute_type` in `config.yaml`.

Transcription latency, 3.48 s Kokoro-synthesised utterance at 16 kHz, CPU
`compute_type=int8`, `beam_size=1`, `vad_filter=False`, one cold call then three
warm calls in the same process:

| model | cold (first call) | warm median | transcript |
|---|---|---|---|
| `distil-large-v3` | 6.861 s | **4.082 s** | exact |
| `small.en` | 2.533 s | **1.192 s** | exact |
| `base.en` (previous default) | 1.291 s | **0.462 s** | exact |

`distil-large-v3` is 3.4x past the 1.0 s warm target, so it was not kept.
`small.en` is what `asr.model` is set to. **It still misses the 1.0 s target**,
at 1.192 s; `base.en` is the only option measured under it, at 0.462 s. That
trade is a judgement call about how much accuracy is worth 730 ms and has been
left to the operator rather than silently resolved.

The first cold number for `small.en` on its first-ever run was 44.081 s, which
was the model download. The 2.533 s above is the same measurement with the model
already in the HuggingFace cache, and is the number that reflects real start-up.

`scripts/first_audio_check.py --reps 3`, scripted reply, no provider round trip:

```
cold first run: 2.969 s
warm n=2  min=2.193  median=2.290  max=2.388 s
```

**Accuracy is UNMEASURED.** Every transcript above came from one synthesised
phrase. Exact transcription of clean TTS output says nothing about noisy-room or
accented speech, and no labelled audio set exists in this repository. No accuracy
improvement is claimed.


## Turn stage table — measured 2026-10-15

Two sources, because `data/zoe.log` from the 2026-10-14 20:22 voice session
was overwritten by a later pytest run and could not be recovered. Neither
source is a single end-to-end voice turn, and the VAD mark on human speech is
still unmeasured.

### ASR / TTS path — `scripts/first_audio_check.py --reps 3`

Real Whisper, real Kokoro, scripted reply, playback to a null sink. **Excludes
the provider round trip.** Marks are ones production code writes itself,
relative to `speech_end = 0`.

| Stage | rep 1 (cold) | rep 2 | rep 3 |
|---|---|---|---|
| VAD end-of-speech | 0 (mark origin; no human) | 0 | 0 |
| **ASR** `speech_end`→`transcript` | **3.027 s** | **2.054 s** | **1.947 s** |
| First TTS chunk `transcript`→`first_token` | 0.026 s | 0.001 s | 0.000 s |
| `first_audio` mark | 3.510 s | 2.487 s | 2.453 s |

Cold 3.510 s; warm n=2 min 2.453 / median 2.470 / max 2.487 s.

**ASR is 82% of this budget.** TTS emits its first chunk in under 30 ms.

### Provider path — `python -m zoe --text --debug`, three live turns

| # | Prompt | Route | intent_route | classify | provider_request | total | Model |
|---|---|---|---|---|---|---|---|
| A | explain why the sky is blue | provider | 2.2 ms | 3.2 ms | 2842.6 ms ok | 2848.0 ms | nemotron_3_ultra |
| B | two-line poem about the sea | provider | 2.2 ms | 3.1 ms | 3101.8 ms fail → 1597.6 ms ok | 4704.7 ms | nemotron_3_5_lightning |
| C | photosynthesis for a ten-year-old | provider | 2.2 ms | 3.2 ms | 55.9 ms fail | 61.3 ms | none — turn returned nothing |

Two of three provider attempts failed. n=2 successes, so no p50 is claimed.
Acknowledgment: every turn in the last real voice session logged
`ack_played=false`.

### Defect in the measurement itself

`first_audio` does not mark first audio. `zoe/voice_loop.py:1205` fires it
after `synthesize_to_queue()` returns, i.e. once the **entire** reply is
synthesised. The 0.43–0.51 s gap between `first_token` and `first_audio` is
full-reply synthesis, not queueing delay, so the headline end-of-speech →
first-audio figure overstates first-audio latency.

### Candidates the data rejected

- **TTS chunk size (220 ms).** First chunk lands at 0.000–0.026 s. Nothing to win.
- **Startup network calls.** `check_environment` takes 1.23 s and makes no
  provider network calls; `ModelLayer.from_config()` only calls
  `provider.is_configured()`, which is a local credential check
  (`bool(self.api_key) and bool(self.base_url)`). `router.status_snapshot()`
  and `probe()` are not reached from startup.

### Not done

Steps B and D of the barge-in plan were not applied. The built-in speaker still
interrupts itself, and the diagnosis is unchanged: see
`docs/barge_in_diagnosis.md`. The `builtin_speaker` audio profile in
`config.yaml` raises `barge_in_snr` and `barge_in_ms`, but the measurement shows
the leak sits at 0.19–0.51 RMS against a bar of 0.023–0.046, so that profile
does **not** fix the problem — it only makes real interruptions harder.
