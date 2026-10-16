# Barge-in / self-interruption diagnosis

Phase B of the ASR + barge-in repair. Numbers only; no fix is proposed here.
Reproduce with:

```
python scripts/analyse_barge_in.py            # default: data/zoe.log*
```

## 1. Headline numbers (verbatim from `analyse_barge_in.py`)

```
-> total barge-in cuts      = 194
-> zero-audio-played cuts   = 2
```

Distribution of seconds since playback start (n=194, 0.1 s buckets):

```
   0.0s | ######################################## 181
   0.1s | ### 12
   0.2s |  0
   0.3s |  1
  at ~0.0s (<0.05): 181/194
  near 1s (0.8-1.2): 0/194 = 0%
  cluster near 1s?   NO
```

`playback_energy` across all 194 cuts:

```
    194 playback_energy=0.000000
```

`residual_energy` across all 194 cuts:

```
    193 residual_energy=0.000000
      1 residual_energy=0.000602
```

## 2. Only one of those 194 cuts is from a live session

`data/zoe.log*` is one file for both the application *and* the test suite —
pytest writes to the same logger. 193 of the 194 barge-in cut lines are test
output, not microphone sessions:

| evidence | pytest (193) | live (1) |
|---|---|---|
| turn id | `turn_001` | `turn_002` |
| `rms_raw` at cut | `0.0007` on 191/193, byte-identical | `0.0323` |
| position relative to the preceding `[AUDIO] playback` line | +7 s to +10 s | +1 s |
| timestamps | clusters at 07:19-07:21, 07:30-07:35, 07:43-07:49, 08:42-08:43, 08:53-08:54 | single point, 08:01:08 |

Filtering on "a real `[AUDIO] playback opening ... name=MacBook Air Speakers`
line within the previous 10 s" leaves exactly one cut:

```
2026-10-14 08:01:08  turn_002  chunks_played=9  elapsed_s=0.328
                   rms_raw=0.0323  residual_energy=0.000602  playback_energy=0.000000
```

**n = 1.** A distribution-based classification is not available.

## 3. What the one live cut actually was

Full window from `data/zoe.log`:

```
08:01:04 VAD speech ended bytes=43520 peak=0.7976; submitting to ASR
08:01:04 ASR returned transcript (11 characters)
08:01:04 [Thinking, Boss...] Processing with Gemini...
08:01:05 [AUDIO] callback frames=1194 ... rms_raw=0.0323 ... state=thinking
08:01:05 [AUDIO] playback opening pid=35570 ... samples=45000 duration_s=1.875
08:01:07 [TTS] synthesis complete turn=turn_002 ... samples=143040 duration_s=5.960
08:01:07 [TTS] chunks queued turn=turn_002 chunks=28 chunk_ms=220
08:01:07 [AUDIO] playback completed pid=35570 ... frames=45000/45000 duration_s=1.875
08:01:07 [AUDIO] playback turn=turn_002 dequeued=9 played=9 elapsed_s=2.127 cancelled=False
08:01:07 [AUDIO] playback opening pid=35570 ... samples=143040 duration_s=5.960
08:01:08 VAD speech started pid=35570 state=speaking barge_in=True
08:01:08 [AUDIO] playback cancelled reason=barge-in queued_chunks=0 playing=True
08:01:08 [VOICE] interrupted (dropped 0 queued audio chunks); listening
08:01:08 [AUDIO] playback cancelled pid=35570 ... frames=4875/143040 duration_s=0.203
08:01:08 [AUDIO] playback turn=turn_002 dequeued=28 played=0 elapsed_s=0.328 cancelled=True
08:01:08 reply cut short turn=turn_002 reason=barge-in chunks_played=9
               residual_energy=0.000602 playback_energy=0.000000
08:01:08 [VOICE] ack turn=turn_002 ack_played=true
08:01:10 [AUDIO] callback frames=1257 ... rms_raw=0.0428 rms_post_echo=0.0410
               preprocessing_changed=True echo_cancelled_frames=44 state=listening
```

The 1.875 s playback at 08:01:05 is the **thinking acknowledgment**
(`ack turn=turn_002 ack_played=true`), not the reply. The reply is the 5.96 s
waveform opened at 08:01:07, of which 0.203 s (4875/143040 samples) played
before the cut. `elapsed_s=0.328` in the script's output is measured from the
reply's playback start.

The cut therefore lands **0.328 s into the reply**, not in the 0.8-1.2 s window
the echo signature calls for, and not at 0 s either.

## 4. Why the discriminating fields cannot be used

The two fields that would settle it are unusable at this cut:

**`playback_energy=0.000000`.** `EchoGuard.observe` writes `last_playback_energy`
from the *last 20 ms sub-frame it scored*. `VoiceLoop._speak` logs that stored
value at the cut. The reply was demonstrably playing (0.203 s of it), so a zero
there means the guard did not have a usable aligned reference for the frame it
last scored - not that the room was silent.

**`rms_raw=0.0323` is not a measurement at the cut.** `analyse_barge_in.py`
carries the last `[AUDIO] callback` line forward, but that diagnostic is
emitted on a 5-second cadence. The last one before the cut is 08:01:05, i.e.
**three seconds before the reply's playback even opened**. It is a
`state=thinking` frame with `echo_cancelled_frames=0`. The same staleness
applies to `elapsed_s` in the other 193 rows, where it is inherited from a
*different turn's* playback line.

**`residual_energy=0.000602`.** √0.000602 = **0.0245** RMS. The relevant bar is
not that: `VoiceActivityDetector._level_needed(barge_in=True)` returns
`max(barge_in_threshold, noise_floor * barge_in_snr)`, i.e.
`max(0.02, noise_floor * 4.0)`. Measured ambient on this machine from the
callback log is 0.022-0.043 RMS, so the barge-in bar is **0.088 - 0.17**.
The logged residual sits 3.6x-6.9x *below* it. Whatever frame produced that
value was not one that could have tripped the detector.

No microphone audio was retained at the cut timestamp
(`data/diag_live_turn_mic.wav` is 0.38 s and covers 08:00:26-08:00:27), so the
waveform cannot be re-examined.

## 5. Related measured facts

* `vad.barge_in_grace_ms` is **0** in `config.yaml`, so the
  `playback_elapsed() < BARGE_IN_GRACE_S` early-playback guard in
  `VoiceLoop._on_speech_start` can never fire. It is documented as opt-in, not
  broken.
* `barge_in_ms = 300` (not 600), `min_speech_ms = 200`,
  `echo_guard_min_consecutive_frames = 3` (60 ms at 20 ms bands),
  `echo_guard_gain_max = 1.5`, `barge_in_snr = 4.0`, `energy_threshold = 0.01`
  => `barge_in_threshold = 0.02`.
* Measured room levels, from the `[AUDIO] callback` lines in the live sessions:
  ambient 0.022-0.043 RMS; user speech reaches 0.32-0.42 RMS with peak 1.0.
* Measured playback level, `data/diag_kokoro_full.wav`: **RMS 0.0443**, peak
  0.3829, 24 kHz.
* **UNMEASURED:** the speaker-to-microphone gain and the true output latency.
  `data/diag_mic_loopback.wav` is *not* a recording of `diag_kokoro_full.wav`
  playing - best cross-correlation between them is r=0.014 at lag 275 ms, i.e.
  no relationship. No pair of recordings in the repo establishes the acoustic
  path.

## 6. Classification

**UNDETERMINED.**

The prompt's three classes each need evidence the logs do not carry:

* *Echo* - expects a cluster at 0.8-1.2 s with residual near zero. Measured:
  **0/194** cuts in that window, and the one live cut is at 0.328 s. The
  recorded cuts are instead clustered at <0.05 s (181/194), which is the
  signature of the 193 test rows, not of a room.
* *Real user speech* - needs the residual to be high at the cut. Measured:
  the only live `residual_energy` is 0.000602 (√ = 0.0245 RMS), 3.6x-6.9x
  *below* the barge-in bar. It is not high.
* *Echo vs. user* cannot be separated because the fields are stale or zeroed
  (section 4), the cut was 0.328 s into the reply rather than in the echo
  window, and n=1.

The `residual_energy` / `playback_energy` fields **do** exist in these logs -
they are not missing as in older runs. The problem is that the values are
taken from the last-scored sub-frame and the last 5-second callback line
rather than from the cancelling frame.

## 7. What would settle it

Logging the cancel decision on the frame that actually triggers the
interruption - mic RMS, `canceller.last_gain`, `last_reduction`, `last_lag_ms`,
and the guard's residual and playback energy for *that frame* - plus retaining
a short ring buffer of raw mic frames around the cut. Phase C
(`tests/test_echo_reverb.py`) does the controlled equivalent with a synthetic
room instead, which is why the live question stays open here.