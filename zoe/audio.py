"""One microphone shared by VAD and ASR.

The rule this module exists to enforce: **exactly one process opens the
microphone.** Previously the wake-word listener and
`speech.listen()` could open two capture streams through ``speech_recognition.Microphone``
— two owners of one device, which is how you get "device busy" failures on some
machines and a silent mic on others.

```text
Microphone  ->  MicrophoneStream  ->  [sinks: wake word, VAD, ASR]
(one owner)        (one thread)
```

Everything downstream is a sink that receives the same PCM frames, so they can
never disagree about what was heard or about ordering.

Everything here is injectable: :class:`MicrophoneStream` takes a ``source``
object with ``open``/``read``/``close``, so the whole pipeline is testable with a
fake microphone and no audio hardware.
"""

from __future__ import annotations

import threading
import time
import math
import os
import queue
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from zoe.config import (
    AUDIO_CHANNELS,
    AUDIO_FRAME_MS,
    AUDIO_INPUT_DEVICE,
    AUDIO_SAMPLE_RATE,
    VAD_BARGE_IN_MS,
    VAD_BARGE_IN_SNR,
    VAD_ECHO_GUARD_ENABLED,
    VAD_ECHO_GUARD_ADAPTIVE_BAR,
    VAD_ECHO_GUARD_GAIN_MAX,
    VAD_ECHO_GUARD_MIN_CONSECUTIVE_FRAMES,
    VAD_END_SILENCE_MS,
    VAD_ENERGY_THRESHOLD,
    VAD_MIN_SPEECH_MS,
    VAD_SNR,
)
from zoe.logger import logger

SAMPLE_RATE = AUDIO_SAMPLE_RATE


def frame_bytes(duration_ms: float = AUDIO_FRAME_MS) -> int:
    """PCM bytes for one frame of `duration_ms` at the configured rate."""
    return int(SAMPLE_RATE * duration_ms / 1000) * 2  # int16 mono


# ---------------------------------------------------------------------------
# Frame maths
# ---------------------------------------------------------------------------

def rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of an int16 mono frame, normalised to 0..1.

    Cheaper than a full FFT and, for this purpose, better: voice energy is what
    matters, not spectral detail, and RMS cannot be fooled into reporting speech
    by a narrow-band tone the way a band-limited detector can.
    """
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * float(value)
    return (total / len(samples)) ** 0.5 / 32768.0


@dataclass
class SpeechEvent:
    """VAD found the start or the end of an utterance."""

    kind: str                      # "start" | "end"
    at: float
    #: Utterance audio captured between the two events, if it ended.
    audio: bytes = b""
    peak: float = 0.0


class VoiceActivityDetector:
    """Energy-based endpointing over the shared frame stream.

    Deliberately simple and fully local: RMS above an adaptive threshold for at
    least `min_speech_ms` is speech; `end_silence_ms` of quiet ends it.

    **The threshold is relative to the room, not absolute.** Measured on the
    development machine, ambient RMS sits at 0.046-0.14 (median 0.068) -- above
    any absolute threshold tuned in a quiet room, so every single frame looked
    like speech and the assistant interrupted itself out of its own reply. A
    fixed threshold cannot be right for both a quiet office and a room with a
    fan, so the detector tracks a noise floor and demands speech to clear it by a
    signal-to-noise margin. The configured `energy_threshold` remains the floor
    of that decision, so a genuinely quiet room behaves exactly as configured.

    Two further knobs:

    * ``min_speech_ms`` kills clicks, keyboard taps and single-frame pops.
    * ``barge_in_ms`` is a *separate, higher* bar used while Ai Partner is busy.
      Interrupting someone mid-sentence should take intent, not a door slam.
    """

    #: How fast the floor follows the room. Quiet arrives quickly (the detector
    #: notices a fan being switched off), loud more slowly, so a hum is not
    #: mistaken for speech -- and then not ignored forever either.
    _FALL = 0.5
    _RISE = 0.01
    #: Frames averaged into the initial estimate. ~1.3s at 80ms frames: long
    #: enough to characterise the room, short enough to feel instant.
    _CALIBRATION_FRAMES = 16

    def __init__(
        self,
        *,
        energy_threshold: float = VAD_ENERGY_THRESHOLD,
        end_silence_ms: float = VAD_END_SILENCE_MS,
        min_speech_ms: float = VAD_MIN_SPEECH_MS,
        barge_in_ms: float = VAD_BARGE_IN_MS,
        barge_in_threshold: Optional[float] = None,
        snr: float = VAD_SNR,
        barge_in_snr: float = VAD_BARGE_IN_SNR,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.energy_threshold = float(energy_threshold)
        self.end_silence_ms = float(end_silence_ms)
        self.min_speech_ms = float(min_speech_ms)
        self.barge_in_ms = float(barge_in_ms)
        #: Barge-in defaults to a stricter level than normal speech detection.
        self.barge_in_threshold = float(
            barge_in_threshold if barge_in_threshold is not None
            else max(self.energy_threshold * 2.0, 0.02)
        )
        self.snr = float(snr)
        self.barge_in_snr = float(barge_in_snr)
        self.clock = clock

        self._speaking = False
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._buffer = bytearray()
        self._peak = 0.0
        #: Running ambient level. None until the first frame arrives.
        self._noise_floor: Optional[float] = None
        self._calibration: List[float] = []

    @property
    def noise_floor(self) -> float:
        """Current ambient estimate; 0 before anything has been heard."""
        return self._noise_floor or 0.0

    def _track_noise(self, level: float) -> None:
        """Estimate the room, but never from frames that look like speech.

        The opening window seeds the floor from a low percentile rather than the
        mean, so a wake word landing inside it cannot set the floor to speech
        level. Tracking is not gated on `_speaking` during that window -- doing
        so deadlocked in exactly the room this exists for -- but no event can be
        emitted before the floor exists, so `_speaking` cannot rise there either.
        """
        if self._noise_floor is None:
            # Seed from a short window rather than from a single frame, so one
            # door slam does not become the room's noise floor.
            self._calibration.append(level)
            if len(self._calibration) >= self._CALIBRATION_FRAMES:
                self._noise_floor = float(np.percentile(self._calibration, 20))
                self._calibration.clear()
            return
        if self._speaking:
            return
        rate = self._FALL if level < self._noise_floor else self._RISE
        self._noise_floor += rate * (level - self._noise_floor)

    def _level_needed(self, barge_in: bool) -> float:
        """RMS a frame must reach to count as speech right now.

        Never below the configured absolute threshold, so a quiet room behaves
        exactly as configured rather than becoming hypersensitive.
        """
        floor = self.noise_floor
        if barge_in:
            return max(self.barge_in_threshold, floor * self.barge_in_snr)
        return max(self.energy_threshold, floor * self.snr)

    def level_needed(self, barge_in: bool) -> float:
        """Public alias for the bar a frame must clear right now.

        Exposed so the voice loop can log *the bar it actually applied* on the
        frame that decided, instead of a threshold copied into the log line by
        hand. A logged bar that drifted from the real one is worse than none:
        it is how a stale-field bug looks like a plausible measurement.
        """
        return self._level_needed(barge_in)

    def reset(self) -> None:
        """Forget any partial utterance. Called on every interruption."""
        self._speaking = False
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._buffer.clear()
        self._peak = 0.0

    @property
    def in_speech(self) -> bool:
        return self._speaking

    def push(self, pcm: bytes, *, barge_in: bool = False) -> Optional[SpeechEvent]:
        """Feed one frame. Returns an event when an utterance starts or ends.

        Args:
            pcm: One int16 mono frame.
            barge_in: Apply the stricter interruption bar instead of the normal
                one. Set while the assistant is busy.
        """
        now = self.clock()
        level = rms(pcm)
        self._peak = max(self._peak, level)
        self._track_noise(level)
        if self._noise_floor is None:
            # Still characterising the room (~1.3s from loop start, while the
            # assistant is idle). Emitting an event now would mean deciding
            # speech against a floor that does not exist yet, which in a noisy
            # room means hearing silence.
            return None
        frame_ms = len(pcm) / 2 / SAMPLE_RATE * 1000
        threshold = self._level_needed(barge_in)
        voiced = level >= threshold

        # Interrupting the assistant has to clear a longer bar than starting a
        # conversation, and it used the *normal* bar: `barge_in_ms` was stored,
        # configured and asserted, but never read, so barge-in fired on
        # `min_speech_ms` of anything that survived echo cancellation.
        needed = self.barge_in_ms if barge_in else self.min_speech_ms

        if not self._speaking:
            if voiced:
                self._voiced_ms += frame_ms
                self._buffer.extend(pcm)
            else:
                # A short burst is not an utterance; discard it rather than
                # making the user repeat themselves.
                self._voiced_ms = 0.0
                self._buffer.clear()
            if self._voiced_ms >= needed:
                self._speaking = True
                self._silence_ms = 0.0
                return SpeechEvent("start", now, bytes(self._buffer), self._peak)
            return None

        self._buffer.extend(pcm)
        if voiced:
            self._silence_ms = 0.0
        else:
            self._silence_ms += frame_ms
            if self._silence_ms >= self.end_silence_ms:
                audio = bytes(self._buffer)
                peak = self._peak
                self.reset()
                return SpeechEvent("end", now, audio, peak)
        return None


#: Band edges as fractions of Nyquist for :class:`EchoGuard`, equal width so a
#: band's share of the energy reflects the signal's spectral shape rather than
#: how wide the band is. Uneven bands let a single wide top band dominate the
#: vector, which made unrelated sound look like our own.
_ECHO_GUARD_EDGE_FRACTIONS = tuple(i / 8 for i in range(9))

# ---------------------------------------------------------------------------
# The adaptive bar, from Mark LV's `core/echo.py`.
#
# That module decides "is this the user?" on one number: the fraction of the
# microphone's energy that our own playback does *not* explain. Nothing here is a
# tuned constant -- the bar is placed just above the echo this room has actually
# been seen to produce.
# ---------------------------------------------------------------------------

#: Sub-frames of our own audio before the measured bar is trusted. ~1 s at the
#: 20 ms band window. Until then the fixed absolute threshold decides alone, so
#: the first half-second of a reply behaves exactly as it did before.
_ECHO_BAR_WARMUP = 16
#: Sub-frames kept to characterise the room's own echo.
_ECHO_BAR_WINDOW = 60
#: Percentiles of that window: "typical echo here", and "near-worst echo here".
_ECHO_BAR_FLOOR_Q, _ECHO_BAR_HEAD_Q = 35, 97
#: Headroom above the near-worst echo. The threshold has to clear *essentially
#: all* of this room's echo, not its median.
_ECHO_BAR_HEAD_MULT = 1.15
#: Floor under the bar, whatever the room measures. Below this fraction nothing
#: counts as a voice, however quiet the room is.
_ECHO_BAR_MIN_RATIO = 0.15
#: Echo residual above which this room's acoustics cannot separate the two
#: sources on content alone: the speaker-to-mic path is so poor that our own
#: voice survives the subtraction about as well as a quiet person does. Above
#: it, evidence has to be sustained for longer before it is believed.
_ECHO_BAR_UNRELIABLE = 0.22

#: Share of a frame's energy that our playback must account for before the frame
#: is treated as containing echo at all. Below it there is nothing to explain: with
#: headphones the speakers never reach the microphone, and fitting a scale to a
#: reference the microphone cannot hear removes the *user's* voice instead.
_ECHO_MIN_PLAY_SHARE = 0.05


class EchoGuard:
    """Knows when the assistant's own audio is in the air, and whether a frame is
    that audio or the user.

    The microphone hears the speakers. Without this, Ai Partner's own reply
    trips the VAD, trips the wake word, and starts a conversation with itself.

    Suppression is time-based rather than "turn the mic off": a hard mute during
    playback would make barge-in impossible, which is the opposite of what the
    user wants. Instead playback is *known*, so assistant audio is ignored while
    genuine user speech still interrupts, and a short cooldown covers the tail of
    the output after playback ends.

    The time layer alone cannot answer barge-in's real question -- *is this sound
    ours?* -- because a reply through the speakers is exactly as loud as the user
    and lands in the same bands; any threshold that lets a person interrupt also
    lets the assistant interrupt itself. So a second, band layer estimates the
    echo per band and subtracts it::

        residual_band = max(0, mic_band - gain_band * playback_band)

    ``gain_band`` is learned while playback is the only thing being heard, and is
    bounded. Barge-in is then allowed only when that *residual* -- what is left
    after our own voice is explained away -- clears the threshold for several
    consecutive frames. Loudness no longer decides; provenance does.

    **And the bar itself is learned** (adapted from Mark LV's ``core/echo.py``).
    A residual large enough to clear a fixed threshold is only large in absolute
    terms; what matters is how large it is *compared with what this room leaves
    behind*. On a laptop speaker the residual of a perfectly-explained echo is
    still an order of magnitude above a fixed bar tuned in a quiet room, so the
    absolute test eventually passes on nothing but our own voice. The guard
    therefore measures the echo this room actually produces and places the bar
    just above its worst case, so the decision is a comparison against the room
    rather than against a constant.

    Learning is double-talk aware in both directions, which is the part that
    matters: the bar is measured only from sub-frames whose spectral shape says
    they are our own audio, and once warm only from sub-frames the guard has
    *already* declined to call speech. Feeding the user's own frames into the
    estimate is the one mistake that makes this self-defeating -- the bar lifts
    over the very thing it is watching for, and barge-in goes deaf.

    ponytail: the bar is a percentile of a 60-sub-frame window of echo
    residuals, refreshed per reply. A room that drifts *within* one reply
    (a hand over the mic) is followed only as fast as the window; a per-frame
    statistic would chase the user instead of the room.
    """

    def __init__(self, *, cooldown_s: float = 0.0, clock: Callable[[], float] = time.monotonic,
                 enabled: bool = VAD_ECHO_GUARD_ENABLED,
                 min_consecutive_frames: int = VAD_ECHO_GUARD_MIN_CONSECUTIVE_FRAMES,
                 gain_max: float = VAD_ECHO_GUARD_GAIN_MAX,
                 threshold: Optional[float] = None,
                 band_frame_ms: float = 20.0,
                 sample_rate: int = SAMPLE_RATE,
                 history_s: float = 2.0,
                 adaptive_bar: bool = VAD_ECHO_GUARD_ADAPTIVE_BAR):
        # -- time layer (unchanged) -----------------------------------------
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self._playback_until = 0.0
        # -- band layer ------------------------------------------------------
        self.enabled = bool(enabled)
        self.min_consecutive_frames = max(1, int(min_consecutive_frames))
        self.gain_max = float(gain_max)
        #: Residual RMS that counts as a real interruption. Defaults to the same
        #: bar the VAD uses for barge-in, so the guard never asks for more than
        #: the detector already would.
        self.threshold = float(
            threshold if threshold is not None else max(VAD_ENERGY_THRESHOLD * 2.0, 0.02)
        )
        self.sample_rate = int(sample_rate)
        self.frame_samples = max(1, int(self.sample_rate * band_frame_ms / 1000.0))
        self.history_s = float(history_s)
        self._window = np.hanning(self.frame_samples).astype(np.float32)
        self._window_power = float((self._window ** 2).sum()) or 1.0
        self._band_bins = self._compute_band_bins(sample_rate)
        self.bands = len(self._band_bins)
        self._gains = np.zeros(self.bands, dtype=np.float32)
        #: Cross/dot accumulators behind the per-band gain, so the estimate is a
        #: projection over many frames rather than one frame's noisy ratio.
        self._sxy = np.zeros(self.bands, dtype=np.float32)
        self._sxx = np.ones(self.bands, dtype=np.float32)
        self._bootstrapped = False
        self._boot_streak = 0
        self._consecutive = 0
        #: Played band-energy frames, time-stamped, for standalone alignment.
        self._play_bands: List[Any] = []
        # -- the bar that is measured rather than configured ------------------
        #: Off reproduces the pre-Feature-4 decision exactly: a fixed absolute
        #: residual threshold. Kept as a switch because this layer has not yet
        #: been validated on real hardware.
        self.adaptive_bar = bool(adaptive_bar)
        #: Residual *ratios* -- what our playback does not explain, as a share
        #: of what the microphone heard -- from sub-frames known to be echo only.
        self._ratios: List[float] = []
        #: Typical and near-worst echo residual here; learned.
        self._ratio_floor = 0.0
        self._ratio_head = 0.0
        #: Diagnostics: the last ratio scored and the bar it was measured against.
        self.last_ratio = 0.0
        self.last_ratio_bar = 0.0
        #: Diagnostics: what was left after subtracting our own voice, and what we
        #: played, at the last scored frame. Logged per cut.
        self.last_residual_energy = 0.0
        self.last_playback_energy = 0.0

    @property
    def streak(self) -> int:
        """Consecutive sub-frames whose residual has cleared the guard bar.

        Public so the voice loop can log *this frame's* streak. The guard's own
        `last_*` fields are easy to read stale -- they describe the last sub-frame
        scored, which for a cut is not the frame that caused it.
        """
        return self._consecutive

    # -- the learned bar -----------------------------------------------------

    @property
    def calibrated(self) -> bool:
        """True once the bar rests on enough of this room's own echo to be used."""
        return len(self._ratios) >= _ECHO_BAR_WARMUP

    @property
    def ratio_floor(self) -> float:
        """Typical echo residual share here. Higher = a harder room to separate."""
        return self._ratio_floor

    @property
    def ratio_bar(self) -> float:
        """Residual share a sub-frame must clear to count as the user."""
        return max(_ECHO_BAR_MIN_RATIO, self._ratio_head * _ECHO_BAR_HEAD_MULT)

    @property
    def reliable(self) -> bool:
        """False when this room's echo is too poor to separate on content alone."""
        return (not self.calibrated) or self._ratio_floor < _ECHO_BAR_UNRELIABLE

    @property
    def required_frames(self) -> int:
        """Consecutive positive sub-frames an interruption needs to be believed.

        Doubled in a poor room, where a single echo spike looks exactly like a
        voice. Everywhere else it is the configured streak, unchanged.
        """
        return self.min_consecutive_frames if self.reliable else self.min_consecutive_frames * 2

    def _learn_ratio(self, ratio: float) -> None:
        """Fold one echo-only residual share into the room's statistics."""
        self._ratios.append(float(ratio))
        if len(self._ratios) > _ECHO_BAR_WINDOW:
            del self._ratios[:-_ECHO_BAR_WINDOW]
        if self.calibrated:
            self._ratio_floor = float(np.percentile(self._ratios, _ECHO_BAR_FLOOR_Q))
            self._ratio_head = float(np.percentile(self._ratios, _ECHO_BAR_HEAD_Q))

    # -- time layer ----------------------------------------------------------

    def begin_playback(self) -> None:
        self._playback_started = self.clock()
        self._playback_until = float("inf")
        # A new reply is a new echo path: relearn the gain rather than carrying
        # the previous reply's estimate into this one.
        self._bootstrapped = False
        self._boot_streak = 0
        self._gains = np.zeros(self.bands, dtype=np.float32)
        self._sxy = np.zeros(self.bands, dtype=np.float32)
        self._sxx = np.ones(self.bands, dtype=np.float32)
        self._consecutive = 0
        self._play_bands = []
        # The measured bar describes one echo path, and a new reply is a new one.
        # Carrying the previous reply's statistics over is what makes a volume
        # change or a room change read as "the user is talking over us".
        self._ratios = []
        self._ratio_floor = 0.0
        self._ratio_head = 0.0

    def playback_elapsed(self) -> float:
        """Seconds since the current playback began; inf when nothing is playing."""
        if not self.playing:
            return float("inf")
        return self.clock() - getattr(self, "_playback_started", self.clock())

    def end_playback(self) -> None:
        self._playback_until = self.clock() + self.cooldown_s
        self._consecutive = 0

    @property
    def playing(self) -> bool:
        return self.clock() < self._playback_until

    def should_suppress(self) -> bool:
        """True while the microphone is hearing assistant audio.

        During playback this gates the *wake word* only -- VAD keeps running, at
        its stricter barge-in level, so the user can still interrupt. The
        cooldown after playback gates both.
        """
        return self.playing

    def barge_in(self) -> bool:
        """True when VAD should use the stricter interruption bar."""
        return self.playing

    # -- band layer ----------------------------------------------------------

    def _compute_band_bins(self, sample_rate: int):
        """RFFT bin ranges for each band, from the edge fractions."""
        rfft_bins = self.frame_samples // 2 + 1
        nyquist = sample_rate / 2.0
        bins = []
        for lo, hi in zip(_ECHO_GUARD_EDGE_FRACTIONS[:-1], _ECHO_GUARD_EDGE_FRACTIONS[1:]):
            a = int(round(lo * (rfft_bins - 1)))
            b = int(round(hi * (rfft_bins - 1)))
            a = min(max(a, 0), rfft_bins - 1)
            b = min(max(b, a + 1), rfft_bins)
            bins.append((a, b))
        return bins

    @staticmethod
    def _samples(pcm):
        """PCM bytes or an array, as float samples in [-1, 1). None if unusable."""
        if pcm is None:
            return None
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            if len(pcm) % 2:
                return None
            return np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0
        return np.asarray(pcm, dtype=np.float32).reshape(-1)

    def _band_energies(self, samples) -> np.ndarray:
        """Mean-square energy per band for one ``frame_samples`` window.

        Normalised so the bands sum to the frame's mean square, i.e.
        ``sqrt(sum(bands))`` is an RMS comparable with :func:`rms` and the VAD
        thresholds. Same normalisation for mic and playback, which is all the
        residual needs.
        """
        n = self.frame_samples
        s = np.asarray(samples, dtype=np.float32).reshape(-1)[:n]
        if s.size < n:
            s = np.pad(s, (0, n - s.size))
        spectrum = np.fft.rfft(s * self._window)
        power = spectrum.real ** 2 + spectrum.imag ** 2
        if power.size > 2:
            power[1:-1] *= 2.0            # fold the negative frequencies back
        norm = self._n_fft_norm()
        out = np.zeros(self.bands, dtype=np.float32)
        for i, (lo, hi) in enumerate(self._band_bins):
            out[i] = float(power[lo:hi].sum()) / norm
        return out

    def _n_fft_norm(self) -> float:
        return self.frame_samples * self._window_power

    def play_reference(self, pcm) -> None:
        """Record audio about to be played, as time-stamped per-band energies.

        Only used by the standalone path; in the loop the guard is handed the
        canceller's already-aligned reference directly via :meth:`observe`.
        """
        if not self.enabled:
            return
        samples = self._samples(pcm)
        if samples is None or samples.size < self.frame_samples:
            return
        now = self.clock()
        n = self.frame_samples
        for i in range(0, samples.size - n + 1, n):
            self._play_bands.append(
                (now + i / self.sample_rate, self._band_energies(samples[i:i + n]))
            )
        cutoff = now - self.history_s
        while len(self._play_bands) > 1 and self._play_bands[1][0] < cutoff:
            self._play_bands.pop(0)

    def _shape_correlation(self, mic_band, play_band) -> float:
        """Correlation of the two mean-centred log spectra, in [-1, 1].

        Band powers are all positive, so a plain cosine of the raw vectors is
        near 1 for *any* two broadband sounds; it cannot tell our echo from the
        user. The mean-centred log spectrum removes overall level and compares
        spectral *shape*, which is identical for a scaled copy and different for
        unrelated sound.
        """
        a = np.log(np.asarray(mic_band, dtype=np.float64) + 1e-12)
        b = np.log(np.asarray(play_band, dtype=np.float64) + 1e-12)
        a = a - a.mean()
        b = b - b.mean()
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        if norm <= 0.0:
            return 0.0
        return float(a.dot(b) / norm)

    def _echo_scale(self, mic_band, play_band, mic_energy):
        """Per-band size of our own playback inside this frame, or None if the
        frame holds nothing of ours to explain.

        Two estimators, in order of preference:

        * the **learned** per-band gain, once :meth:`_bootstrap` has accepted
          frames as ours. It models a coloured speaker-to-microphone path.
        * otherwise the **least-squares fit for this sub-frame alone** -- Mark LV's
          ``alpha``, one scalar over all the bands. Eight numbers and one scale is
          an over-determined fit, so it removes only what is actually proportional
          to our playback; unlike a per-band fit it cannot remove the user's
          voice, whose formants sit in bands where ours were weak.

        The fallback is what makes the band layer work at all before the shape
        match has been accepted. Without it ``_gains`` stays zero, the residual is
        the raw microphone, and the "residual" test silently collapses back into
        the loudness test it replaced -- which passes on our own voice in exactly
        the loud rooms this exists for.

        None is returned when the reference cannot account for a real share of the
        frame. Fitting a scale to audio the microphone cannot hear is how a
        headset ends up cancelling the person instead of the speakers.
        """
        if mic_energy <= 1e-12:
            return None
        if float(play_band.sum()) < _ECHO_MIN_PLAY_SHARE * mic_energy:
            return None
        if self._bootstrapped:
            return self._gains
        denom = float(np.dot(play_band, play_band))
        if denom < 1e-12:
            return None
        alpha = float(np.dot(mic_band, play_band)) / denom
        return np.full(self.bands, max(0.0, min(alpha, self.gain_max)), dtype=np.float32)

    def _residual_energy(self, mic_band, play_band, mic_energy) -> float:
        scale = self._echo_scale(mic_band, play_band, mic_energy)
        if scale is None:
            return float(np.maximum(mic_band, 0.0).sum())
        residual = mic_band - scale * play_band
        return float(np.maximum(residual, 0.0).sum())

    def _nearest_playback(self, when: float):
        best, best_dt = None, None
        for played_at, band in self._play_bands:
            dt = abs(played_at - when)
            if best_dt is None or dt < best_dt:
                best, best_dt = band, dt
        return best

    def _match_playback(self, mic_subs, now: float):
        """Pick played frames explaining `mic_subs`, searching over output lag.

        The lag is unknown (device buffering), so every candidate on a 20 ms
        ladder up to ``MAX_LAG_MS`` is tried and the one with the strongest
        spectral-shape match wins -- the same idea as the canceller's sample-level
        search, at band resolution. The match is scale-invariant, so the choice does
        not depend on a gain the guard has not learned yet.
        """
        frame_s = self.frame_samples / self.sample_rate
        count = len(mic_subs)
        mic_bands = [self._band_energies(s) for s in mic_subs]
        times = [now - (count - i) * frame_s for i in range(count)]
        max_lag = int(EchoCanceller.MAX_LAG_MS / (frame_s * 1000)) + 1
        best_total, best_choice = None, []
        for lag in range(max_lag):
            choice = []
            for i, t in enumerate(times):
                band = self._nearest_playback(t - lag * frame_s)
                if band is None:
                    break
                choice.append(band)
            if len(choice) != count:
                continue
            # Score by spectral-shape correlation: the right lag makes the mic
            # look like a scaled copy of the played audio, a wrong lag does not,
            # whether or not the guard has learned a gain yet.
            total = sum(
                self._shape_correlation(mic_b, play_b)
                for mic_b, play_b in zip(mic_bands, choice)
            )
            if best_total is None or total > best_total:
                best_total, best_choice = total, choice
        return best_choice

    #: Spectral-shape correlation above which a frame is taken to be our own
    #: audio. Scaled copies score ~1; unrelated sound measured below 0.6.
    SHAPE_MATCH = 0.9

    #: Frames of consistent shape match before the gain is seeded. Two is enough
    #: to stop a single coincidental match on a no-echo frame from baking in a
    #: bogus gain, and costs 20 ms -- nothing against the 300 ms barge-in bar.
    SHAPE_MATCH_FRAMES = 2

    def _bootstrap(self, mic_bands, play_subs) -> None:
        """Seed the per-band gain from frames our playback already explains."""
        sxy = np.zeros(self.bands, dtype=np.float64)
        sxx = np.zeros(self.bands, dtype=np.float64)
        for mic_band, play_band in zip(mic_bands, play_subs):
            sxy += mic_band.astype(np.float64) * play_band.astype(np.float64)
            sxx += play_band.astype(np.float64) * play_band.astype(np.float64)
        sxx = np.maximum(sxx, 1e-12)
        self._gains = np.clip(sxy / sxx, 0.0, self.gain_max).astype(np.float32)
        self._sxy = sxy.astype(np.float32)
        self._sxx = sxx.astype(np.float32)
        self._bootstrapped = True

    def _refine(self, mic_band, play_band, residual_before: float,
                mic_energy: float) -> bool:
        """Refine the learned gain, but only while the frame looks like echo.

        Adapting during speech would teach the guard to subtract the user, so the
        update is frozen whenever the current residual is a large share of the
        frame -- the double-talk detector.

        Returns whether it adapted, which is also the only verdict on the frame
        worth *measuring* the room from: a frame our playback explains well enough
        to learn from is by construction not the user talking over us. One test,
        two jobs -- a second, looser gate would let the user's own speech into the
        estimate that decides where to draw the line.
        """
        if not self._bootstrapped:
            return mic_energy > 0.0 and residual_before < 0.25 * mic_energy
        if mic_energy > 0.0 and residual_before < 0.25 * mic_energy:
            self._sxy = 0.9 * self._sxy + mic_band * play_band
            self._sxx = 0.9 * self._sxx + play_band * play_band
            self._gains = np.clip(
                self._sxy / np.maximum(self._sxx, 1e-12), 0.0, self.gain_max
            ).astype(np.float32)
            return True
        return False

    def observe(self, mic_pcm, playback_pcm=None) -> bool:
        """Score one captured frame; True if it is the user, not our own audio.

        ``playback_pcm`` is the (already aligned) playback for this frame, as
        provided by the canceller in the loop. When omitted, the guard aligns
        against its own :meth:`play_reference` history. With nothing playing
        there is nothing to attribute, so the frame passes as the user.
        """
        if not self.enabled:
            return True
        mic = self._samples(mic_pcm)
        if mic is None or mic.size < self.frame_samples:
            self._consecutive = 0
            return True
        now = self.clock()
        n = self.frame_samples
        mic_subs = [mic[i:i + n] for i in range(0, mic.size - n + 1, n)]

        if playback_pcm is not None:
            ref = self._samples(playback_pcm)
            if ref is None or ref.size < len(mic_subs) * n:
                self._consecutive = 0
                return True
            play_subs = [self._band_energies(ref[i * n:(i + 1) * n]) for i in range(len(mic_subs))]
        elif self._play_bands:
            play_subs = self._match_playback(mic_subs, now)
            if len(play_subs) != len(mic_subs):
                self._consecutive = 0
                return True
        else:
            self._consecutive = 0
            return True

        mic_bands = [self._band_energies(sub) for sub in mic_subs]
        if not self._bootstrapped:
            correlations = [
                self._shape_correlation(mic_b, play_b)
                for mic_b, play_b in zip(mic_bands, play_subs)
            ]
            if correlations and min(correlations) >= self.SHAPE_MATCH:
                self._boot_streak += 1
            else:
                self._boot_streak = 0
            if self._boot_streak >= self.SHAPE_MATCH_FRAMES:
                self._bootstrap(mic_bands, play_subs)

        user = True
        # The floor of the bar applies from the very first frame: nothing counts
        # as the user until it clears a *share* of what our playback failed to
        # explain, not merely an absolute level. Mark LV's floor, not a tuned
        # constant -- and it is what stops a reverberant room, where the residual
        # of our own voice is most of the signal, from reading as a person.
        # Calibration only ever raises it from here.
        for mic_band, play_band in zip(mic_bands, play_subs):
            mic_energy = float(mic_band.sum())
            residual_before = self._residual_energy(mic_band, play_band, mic_energy)
            echo_frame = self._refine(mic_band, play_band, residual_before, mic_energy)
            residual = self._residual_energy(mic_band, play_band, mic_energy)
            self.last_residual_energy = residual
            self.last_playback_energy = float(play_band.sum())
            # Share of what the microphone heard that our playback does not
            # explain. Scale-invariant by construction, which is the point: the
            # same audio passes this bar whatever the speaker volume is.
            ratio = residual / mic_energy if mic_energy > 1e-12 else 0.0
            # Nothing is in force while warming, so the bar is reported as 0.0
            # rather than as a number that is not being applied -- a log that
            # shows a bar the decision ignored is worse than no bar.
            self.last_ratio = ratio
            self.last_ratio_bar = self.ratio_bar
            speech = (math.sqrt(max(residual, 0.0)) >= self.threshold) and (
                ratio >= self.ratio_bar
            )
            if speech:
                self._consecutive += 1
            else:
                self._consecutive = 0
            # Double-talk aware, and the same test at both stages: only a sub-frame
            # our own playback already explains well enough to *learn from* may
            # measure the room. A frame the user is talking over fails that test, so
            # their speech can neither train the gain nor lift the bar over the very
            # thing it watches for. Mark LV's "learn from every block while warming"
            # stage is deliberately not copied -- it protects the estimate by
            # refusing to interrupt until the estimate exists, and this guard cannot
            # afford that: an interruption inside the first second of a reply is a
            # real one.
            if self.adaptive_bar and echo_frame:
                self._learn_ratio(ratio)
            user = self._consecutive >= self.required_frames
        return user


class EchoCanceller:
    """Subtracts the assistant's own playback from the captured microphone.

    Necessary because a louder VAD threshold cannot answer the question barge-in
    actually asks. The assistant's voice through the speakers is exactly as loud
    as the user's and lasts exactly as long, so *any* threshold that lets a person
    interrupt will also let the assistant interrupt itself. Measured live: with
    thresholding alone the assistant cut itself off mid-sentence on its first
    reply.

    So the far-end reference is used directly. Everything Ai Partner plays is
    handed to :meth:`play_reference` as it is played, keeping a short history
    time-stamped. When a captured frame arrives, the reference covering the same
    wall-clock instant is resampled onto it, an echo gain is estimated by the
    usual projection, and that much of the reference is subtracted. What is left
    is the near end -- the user.

    Deliberately a single-tap affine canceller, not a filter bank. It is a few
    dozen lines, has no dependencies, and does the one job that matters: remove
    a direct speaker-to-mic leak while leaving a person alone. It cannot remove
    room reverberation, which is the honest ceiling of this approach.

    ponytail: alignment is exact but single-tap, so it cannot remove
    reverberation, and it gives up beyond ``MAX_LAG_MS`` of output latency. Widen
    the lag window or add a filter bank if the residual still trips the VAD.
    """

    #: How much played audio to keep for alignment. Long enough to cover a lag
    #: search, short enough that the history is a constant few hundred KB.
    HISTORY_S = 2.0

    #: Widest output latency the canceller will align across, in ms.
    #:
    #: The reference window is fetched this much *wider* than the frame it will
    #: be matched against, so every candidate lag lands on real played audio
    #: instead of a zero-padded edge.
    #:
    #: This must be greater than one frame (80 ms at the default capture rate),
    #: *not* equal to it. A captured frame is handed to `cancel` when it is
    #: delivered, so its samples span `[now - frame, now]`, while sound played
    #: `L` ms earlier sits at `[now - frame - L, now - L]`. Anchored at `end=now`
    #: that echo starts at offset `frame + pad - L`, and only offsets up to
    #: `_pad()` are searched -- so a lag is reachable only when
    #: `_pad() >= frame + L`. `_pad()` is sized in `_pad`, which covers a whole
    #: frame *plus* this; with a lead-in of `MAX_LAG_MS` alone, reachability
    #: required `L >= 80 ms`, so every faster device had its echo outside the
    #: search: the gain collapsed to zero, the assistant heard its own voice, the
    #: VAD called it barge-in, and playback stopped roughly 0.7 s into every
    #: reply. CoreAudio reports ~24 ms output latency here, plus buffering, so
    #: 200 ms leaves real margin. The window stays well inside `HISTORY_S` and
    #: costs ~9 ms per 80 ms frame.
    MAX_LAG_MS = 200

    def __init__(self, *, rate: int = SAMPLE_RATE, history_s: float = HISTORY_S,
                 clock: Callable[[], float] = time.monotonic,
                 playback_rate: Optional[int] = None):
        self.rate = int(rate)
        #: The rate the played audio was synthesised at. Phase B moved speech
        #: output to 24 kHz while capture stayed at 16 kHz, so these are no
        #: longer the same number and the far end must be resampled to be
        #: comparable with a captured frame at all.
        self.playback_rate = int(playback_rate or rate)
        g = math.gcd(self.playback_rate, self.rate) or 1
        self._up = max(1, self.rate // g)
        self._down = max(1, self.playback_rate // g)
        self.history_s = float(history_s)
        self.clock = clock
        self._history: List[Any] = []          # [(start_time, float32 array)]
        #: Last estimated echo gain, and how much energy was removed. Diagnostics
        #: only, but they are what tells you the canceller is working.
        self.last_gain = 0.0
        self.last_reduction = 0.0
        self.last_lag_ms = 0
        #: Alignment offset chosen for the current frame, in capture-rate samples.
        self._offset = 0
        #: The aligned playback reference for the last cancelled frame, exposed so
        #: the band-energy echo guard can score the same instant without doing its
        #: own alignment. None until a frame is actually cancelled.
        self.last_reference: Optional[np.ndarray] = None
        self.frames_cancelled = 0

    # -- far end ---------------------------------------------------------

    def play_reference(self, pcm: bytes) -> None:
        """Record audio about to be played, stamped with when it starts."""
        if not pcm or len(pcm) % 2:
            return
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._history.append((self.clock(), samples))
        self._trim()

    def _trim(self) -> None:
        cutoff = self.clock() - self.history_s
        while len(self._history) > 1 and self._history[1][0] <= cutoff:
            self._history.pop(0)

    #: Output latency between "we called play" and "the speakers moved" is a few
    #: tens of milliseconds and varies by device and buffer. Rather than assume
    #: it, the gain is estimated at *every* sample offset across
    #: ``MAX_LAG_MS`` and the best one is used. Measured as necessary: assuming
    #: zero lag left enough echo to interrupt the assistant out of its own
    #: reply, and so did a coarse 20 ms ladder -- the estimated gain collapsed
    #: to zero whenever the device's real latency fell between two rungs, which
    #: silently turned the canceller into a no-op.

    def _pad(self, count: int = 0) -> int:
        """Extra reference samples fetched to cover one frame plus the widest lag.

        The lead-in has to cover a whole captured frame *as well as* the acoustic
        lag, not the lag alone. `cancel` is handed a frame when it is delivered,
        so its samples span `[now - frame, now]`, while sound played `L` ms
        earlier occupies `[now - frame - L, now - L]`. Anchored at `end = now`
        the echo therefore starts at offset `frame + pad - L`, and `_best` only
        searches offsets `0 .. pad`, so a lag is reachable only when
        `pad >= frame + L`.

        With the lead-in covering `MAX_LAG_MS` alone, that inequality holds only
        for `L >= one frame` (80 ms), so every device with a faster round trip --
        i.e. essentially all of them -- had its echo outside the search. The gain
        collapsed to zero, the assistant heard its own voice, the VAD called it
        barge-in, and playback stopped ~0.7 s into every reply. Measured: CoreAudio
        reports ~24 ms output latency on the test machine.

        `count` is the frame length in samples; it defaults to the configured
        capture frame. The search loop is unchanged, so only the window grows,
        and it stays well inside `HISTORY_S`.
        """
        frame_samples = count or int(self.rate * AUDIO_FRAME_MS / 1000)
        return int(self.rate * self.MAX_LAG_MS / 1000) + frame_samples

    def _best(self, frame, reference) -> float:
        """Echo gain at the offset that best explains `frame`.

        The offset is chosen on the exact least-squares residual,
        ``||f||² - (f.c)²/(c.c)``, computed with the *unclamped* gain, and the
        clamp is applied only to the value that is actually subtracted.
        Selecting on the clamped gain instead distorted the choice and left echo
        behind -- measured, not assumed.

        `reference` is `_pad(count)` longer than `frame`, so the search covers
        offsets `0 .. pad`: offset 0 means the frame lines up with the tail of
        the window, and offset `pad` means the audio that reached the microphone
        was submitted a whole lead-in earlier.
        """
        count = len(frame)
        frame_energy = float((frame * frame).sum())
        best, best_gain, best_offset = float("inf"), 0.0, 0
        for offset in range(len(reference) - count + 1):
            candidate = reference[offset:offset + count]
            energy = float((candidate * candidate).sum())
            if energy < 1e-9:
                continue
            inner = float((frame * candidate).sum())
            residual = frame_energy - inner * inner / energy
            if residual < best:
                best = residual
                best_gain = inner / energy
                best_offset = offset
        self._offset = best_offset
        self.last_lag_ms = round(best_offset / self.rate * 1000, 1)
        return max(0.0, min(best_gain, 4.0))

    def _reference(self, end: float, count: int):
        """Played audio for the `count` frames ending at `end`, widened by
        `_pad(count)` at the front. Returned at the capture rate.

        The played audio is stored at `playback_rate`, which after Phase B is not
        the capture rate. A 16 kHz microphone hearing a 24 kHz speaker captures a
        downsampled copy, so the reference has to be resampled before it can be
        compared with a captured frame. Without this the projection is computed
        against the wrong samples, the estimated gain collapses to ~0, and the
        assistant hears its own voice as user speech.

        The window reaches one whole frame plus `MAX_LAG_MS` *before* the frame,
        so every candidate alignment is backed by real played samples. Shifting a
        frame-length reference instead zero-pads its front and drops its tail,
        leaving the part of the frame no candidate can explain simply
        uncancelled. See :meth:`_pad` for why the frame itself must be included.

        The time grid is built from integer sample indices, not from float
        arithmetic on a duration. `int((end - start) * rate)` is 1279 rather
        than 1280 often enough to matter, and a reference that is one sample
        short of `count + pad` is discarded by the length check in
        :meth:`cancel` -- which silently disabled echo cancellation entirely.
        """
        total = count + self._pad(count)
        if count <= 0:
            return np.zeros(0, dtype=np.float32)
        # Window in playback-rate sample indices, exactly `total` capture frames
        # wide, anchored on its *start* rather than derived from the end.
        # resample_poly maps output sample m onto input sample m*down/up, so the
        # output index that lands on `end - total/rate` fixes the whole grid;
        # getting that anchor off by one misregisters every candidate equally
        # and the projection finds nothing.
        wide = -(-total * self.playback_rate // self.rate)
        last = int(round(end * self.playback_rate))
        first = int(round((end - total / self.rate) * self.playback_rate))
        positions_all = first + np.arange(wide, dtype=np.int64)

        buf = np.zeros(wide, dtype=np.float32)
        lo_time, hi_time = first / self.playback_rate, last / self.playback_rate
        for played_at, samples in self._history:
            n = len(samples)
            if played_at + n / self.playback_rate < lo_time or played_at > hi_time:
                continue
            base = int(round(played_at * self.playback_rate))
            positions = positions_all - base
            valid = (positions >= 0) & (positions < n)
            if not valid.any():
                continue
            buf[valid] += samples[positions[valid]]

        if self._up == self._down:
            out = buf
        else:
            from scipy.signal import resample_poly

            # resample_poly maps output sample m onto input sample m*down/up, so
            # the resampled window starts at input index 0 and is `total` long.
            out = resample_poly(buf, self._up, self._down).astype(np.float32)
        if len(out) < total:
            out = np.pad(out, (0, total - len(out)))
        return out[:total]

    # -- near end --------------------------------------------------------

    def _align(self, frame, reference):
        """The reference shifted to the offset `_best` chose."""
        return reference[self._offset:self._offset + len(frame)]

    def cancel(self, pcm: bytes) -> bytes:
        """Return `pcm` with the assistant's own playback removed."""
        if not pcm or len(pcm) % 2:
            return pcm
        frame = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if not self._history:
            return pcm

        reference = self._reference(self.clock(), len(frame))
        if len(reference) != len(frame) + self._pad(len(frame)):
            return pcm

        energy = float((reference * reference).sum())
        if energy < 1e-9:
            return pcm                     # nothing of ours in this frame

        gain = self._best(frame, reference)
        reference = self._align(frame, reference)
        self.last_reference = reference

        residual = frame - gain * reference
        self.last_gain = gain
        self.last_reduction = 1.0 - (
            float((residual * residual).sum()) / max(float((frame * frame).sum()), 1e-12)
        )
        self.frames_cancelled += 1
        return np.clip(residual * 32767.0, -32768, 32767).astype("<i2").tobytes()

    def close(self) -> None:
        self._history.clear()

    def status(self) -> Dict[str, Any]:
        return {
            "frames_cancelled": self.frames_cancelled,
            "last_gain": round(self.last_gain, 3),
            "last_reduction": round(self.last_reduction, 3),
            "last_lag_ms": self.last_lag_ms,
            "history_s": self.history_s,
        }


@dataclass
class AudioChunk:
    """One queued piece of synthesised speech, tagged with its turn.

    The tag is the whole point: :meth:`AudioQueue.pop` refuses chunks whose turn
    is stale, so audio generated for an interrupted turn can never resume once a
    new turn is underway.
    """

    turn_id: str
    index: int
    data: bytes
    sample_rate: int = SAMPLE_RATE


class AudioQueue:
    """Ordered, turn-tagged, cancellable audio.

    ``discarded_stale`` counts what was thrown away, which is how a test proves
    old audio did not leak into the new turn rather than merely asserting the
    absence of a symptom.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: List[AudioChunk] = []
        self._next_index = 0
        self.discarded_stale = 0
        self.cleared = 0

    def push(self, turn_id: str, data: bytes, sample_rate: int = SAMPLE_RATE) -> AudioChunk:
        with self._lock:
            chunk = AudioChunk(turn_id, self._next_index, data, sample_rate)
            self._next_index += 1
            self._chunks.append(chunk)
            return chunk

    def pop(self, is_stale: Callable[[Optional[str]], bool]) -> Optional[AudioChunk]:
        """Next chunk that is still worth playing, or None.

        Chunks belonging to a stale turn are dropped rather than returned, so the
        caller cannot accidentally play them.
        """
        while True:
            with self._lock:
                if not self._chunks:
                    return None
                chunk = self._chunks.pop(0)
                if not is_stale(chunk.turn_id):
                    return chunk
                # Taken off the queue and thrown away. Anything still in flight
                # from a cancelled turn is discarded here rather than played.
                self.discarded_stale += 1

    def peek_len(self) -> int:
        with self._lock:
            return len(self._chunks)

    def clear(self) -> int:
        """Drop everything queued. Returns how many chunks were discarded."""
        with self._lock:
            count = len(self._chunks)
            self._chunks.clear()
            self.cleared += count
            return count


# ---------------------------------------------------------------------------
# The microphone owner
# ---------------------------------------------------------------------------

class MicrophoneUnavailable(RuntimeError):
    """The input device could not be opened. Never raised past ``start``."""


class SoundDeviceSource:
    """Microphone capture through sounddevice with a bounded callback queue."""
    def __init__(self, device=None, rate=SAMPLE_RATE, frame_ms=AUDIO_FRAME_MS, max_queued=200):
        self.device = device
        self.rate = int(rate)
        self.block = int(self.rate * frame_ms / 1000)
        self.frame_size = self.block * 2
        self._queue = queue.Queue(maxsize=max_queued)
        self._stream = None
        self.device_name = ""
        self.overflows = 0

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def open(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as e:
            raise MicrophoneUnavailable("sounddevice is not installed. Run: pip install sounddevice") from e
        try:
            info = sd.query_devices(self.device, "input")
            self.device_name = info.get("name", "unknown")
            self._stream = sd.InputStream(
                samplerate=self.rate, channels=1, dtype="int16", blocksize=self.block,
                device=self.device, callback=self._callback,
            )
            self._stream.start()
        except Exception as e:
            self._stream = None
            raise MicrophoneUnavailable(
                f"Could not open microphone '{self.device_name or self.device}': {e}. "
                "On macOS, allow your terminal under System Settings > Privacy and Security > Microphone."
            ) from e
        logger.info("[AUDIO] microphone: %s", self.device_name)

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.overflows += 1
        chunk = indata.tobytes()
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            self.overflows += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("microphone close reported: %s", e)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class MicrophoneStream:
    """The single owner of the microphone, fanning frames out to sinks.

    One reader thread. Sinks are called in registration order, on that thread, so
    a sink must be fast and must never block: a sink that blocks stalls every
    other consumer of the same audio.
    """

    def __init__(
        self,
        *,
        source: Any = None,
        clock: Callable[[], float] = time.monotonic,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.source = source or SoundDeviceSource(device=AUDIO_INPUT_DEVICE)
        self.clock = clock
        self.on_error = on_error
        self._sinks: List[Callable[[bytes], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.frames_read = 0
        self.available = False
        self.last_error: Optional[str] = None
        self._last_empty_read_diag = 0.0
        self._source_factory: Optional[Callable[[str], SoundDeviceSource]] = None

    def set_device(self, device: Optional[str]) -> None:
        """Change the input device. The stream must not be running."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("cannot change device while microphone is running")
        self.source.close()
        self.source = SoundDeviceSource(device=device)

    def set_source_factory(self, factory: Callable[[str], SoundDeviceSource]) -> None:
        """Set a factory for creating new SoundDeviceSource instances.

        This allows the VoiceLoop to reconnect without directly constructing
        SoundDeviceSource, keeping the dependency injected.
        """
        self._source_factory = factory

    # -- wiring ----------------------------------------------------------

    def add_sink(self, sink: Callable[[bytes], None]) -> None:
        """Register a consumer of raw frames."""
        with self._lock:
            if sink not in self._sinks:
                self._sinks.append(sink)

    def remove_sink(self, sink: Callable[[bytes], None]) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Open the device and start pumping frames. Honest False on failure."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("previous microphone reader is still stopping")
                return True
            if self._thread is not None:
                self.source.close()
                self._thread = None
            try:
                self.source.open()
            except Exception as e:  # noqa: BLE001 - unavailability is not a crash
                self.available = False
                self.last_error = str(e)
                logger.warning(f"[AUDIO] microphone unavailable: {e}")
                if self.on_error:
                    self.on_error(str(e))
                return False
            self._stop.clear()
            self.available = True
            self.last_error = None
            self._last_empty_read_diag = self.clock()
            self._thread = threading.Thread(target=self._pump, name="microphone", daemon=True)
            self._thread.start()
        logger.info(
            "[AUDIO] microphone stream active pid=%d device=%s index=%s rate=%d channels=%d "
            "format=int16 frame=%d samples/%dms",
            os.getpid(), getattr(self.source, "device_name", None) or "configured/default input",
            getattr(self.source, "device", None), SAMPLE_RATE, AUDIO_CHANNELS,
            int(SAMPLE_RATE * AUDIO_FRAME_MS / 1000), AUDIO_FRAME_MS,
        )
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """Stop pumping and release the device. Idempotent."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("microphone reader did not stop; device remains open")
        with self._lock:
            if thread is not self._thread:
                raise RuntimeError("microphone owner changed during shutdown")
            self.source.close()
            self._thread = None
            self.available = False

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    # -- reading ---------------------------------------------------------

    def _pump(self) -> None:
        """Read loop that reports repeated capture failures instead of hiding them."""
        failures = 0
        while not self._stop.is_set():
            try:
                pcm = self.source.read()
            except Exception as e:  # noqa: BLE001
                failures += 1
                self.last_error = f"read failed: {e}"
                logger.error("[AUDIO] microphone read failed (%d): %s", failures, e)
                if failures >= 20:
                    self._fail("giving up on the microphone after repeated read failures")
                    return
                time.sleep(min(0.05 * failures, 0.5))
                continue
            if pcm is None:
                continue
            failures = 0
            if not isinstance(pcm, (bytes, bytearray, memoryview)) or len(pcm) != frame_bytes():
                self._fail(
                    f"microphone returned malformed frame: expected {frame_bytes()} bytes "
                    f"of {SAMPLE_RATE} Hz mono int16 PCM"
                )
                return
            self.frames_read += 1
            with self._lock:
                sinks = list(self._sinks)
            for sink in sinks:
                try:
                    sink(pcm)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[AUDIO] sink failed: %s", e)

    def _fail(self, message: str) -> None:
        self.last_error = message
        self.available = False
        logger.error("[AUDIO] %s", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                logger.exception("[AUDIO] microphone error callback failed")

    # -- diagnostics -----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "available": self.available,
            "frames_read": self.frames_read,
            "sinks": len(self._sinks),
            "sample_rate": SAMPLE_RATE,
            "frame_ms": AUDIO_FRAME_MS,
            "device_index": getattr(self.source, "device", None),
            "device_name": getattr(self.source, "device_name", None),
            "last_error": self.last_error,
        }


__all__ = [
    "AudioChunk",
    "AudioQueue",
    "EchoCanceller",
    "EchoGuard",
    "MicrophoneStream",
    "MicrophoneUnavailable",
    "SoundDeviceSource",
    "SpeechEvent",
    "VoiceActivityDetector",
    "frame_bytes",
    "rms",
]
