"""Phase 6 tests: the persistent warm Kokoro pipeline.

Migrated from the removed Chatterbox service. What survived the migration is
every contract the current architecture still has: build the pipeline once,
build it once *even under concurrency*, warm up without letting a warm-up
failure reach the user, and never unload a model that is about to be needed
again. Those are properties of `zoe.tts`, not of any particular engine.

What is gone, and why:

* ``prepare_chatterbox_reference`` / the approved-reference-wav cache. Kokoro
  uses built-in voices and has no reference audio, so there is nothing to
  resolve, cache or prepare. No coverage is lost: the contract does not exist.
* ``cfg_weight`` / ``temperature`` / a fixed ``exaggeration`` of 0.5 passed to
  ``generate()``. Kokoro takes no such kwargs; expressiveness is speed plus the
  optional ``exaggeration`` hint that maps onto it, and that mapping is covered
  in `tests/test_tts_engine.py`. No coverage is lost.
* "warm-up runs once automatically, inside the first synthesis". Phase B made
  warm-up an explicit `tts.warm_up()` call the application makes before opening
  the microphone. The automatic first-call warm-up no longer exists; what is
  tested here is that an explicit warm-up failure is survivable, which is the
  part that still matters.
* "generation runs under ``torch.inference_mode``". That wrapper lived in the
  removed implementation; `zoe.tts` does not set an inference context. No
  coverage is lost because there is no longer a mode to assert.

Engine dispatch, waveform validation and the concurrency of *synthesis itself*
are covered in `tests/test_tts_engine.py`. This file covers pipeline
*lifecycle*.

The real model is never loaded here (see `scripts/tts_benchmark.py` for live
numbers). A fake pipeline proves lifecycle semantics without a model.
"""

import sys
import threading

import numpy as np
import pytest

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from zoe import tts  # noqa: E402

#: Audio a fake segment yields: 0.1 s of tone at the native 24 kHz.
_FAKE_SAMPLES = int(tts.KOKORO_RATE * 0.1)


@pytest.fixture(autouse=True)
def _reset_pipeline():
    """The pipeline is module-global; no test may inherit another's."""
    tts._pipeline = None
    yield
    tts._pipeline = None


class _FakePipeline:
    """Stands in for `kokoro.KPipeline`: callable, yields 3-tuples."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0):
        self.calls.append((text, voice, speed))
        yield ("phonemes", 0.1, np.full(_FAKE_SAMPLES, 0.2, dtype=np.float32))


def _install(monkeypatch):
    """Point `tts._get_pipeline` at a fresh fake; return (fake, factory_calls)."""
    fake = _FakePipeline()
    built = []

    def _factory(lang_code):
        built.append(lang_code)
        return fake

    import kokoro

    monkeypatch.setattr(kokoro, "KPipeline", _factory)
    return fake, built


class TestPipelineSingleton:
    """The point of a warm pipeline: pay the load cost once."""

    @pytest.mark.requires_kokoro
    def test_pipeline_is_built_once_across_many_syntheses(self, monkeypatch):
        fake, built = _install(monkeypatch)
        for _ in range(3):
            tts.synthesize("Hello.")
        assert len(built) == 1, "the pipeline was rebuilt"
        assert len(fake.calls) >= 3, "asked often, built once"
        assert tts._pipeline is fake

    @pytest.mark.requires_kokoro
    def test_concurrent_first_use_builds_once(self, monkeypatch):
        """The lock is the guarantee; without it a cold start builds N times."""
        fake, built = _install(monkeypatch)
        threads = [threading.Thread(target=tts._get_pipeline) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), "a pipeline init thread hung"
        assert len(built) == 1, f"pipeline built {len(built)} times under contention"

    @pytest.mark.requires_kokoro
    def test_the_pipeline_is_never_unloaded(self, monkeypatch):
        """Idle must not drop a model that is about to be needed again."""
        fake, _ = _install(monkeypatch)
        for _ in range(3):
            tts.synthesize("Hi.")
        assert tts._pipeline is fake, "the warm pipeline was discarded"


class TestWarmUp:
    @pytest.mark.requires_kokoro
    def test_warm_up_prepares_the_pipeline(self, monkeypatch):
        fake, built = _install(monkeypatch)
        tts.warm_up()
        assert len(built) == 1
        assert fake.calls, "warm-up did not synthesize anything"

    @pytest.mark.requires_kokoro
    def test_a_failed_warm_up_is_logged_and_never_reaches_the_user(
            self, monkeypatch, caplog):
        """A warm-up hiccup must not become a speech failure.

        `warm_up()` exists so a slow first load is paid before the microphone
        opens. If that warm-up fails for any reason, speech still has to work:
        the failure is logged and the next `synthesize` call proceeds normally.
        """
        real_synthesize = tts.synthesize
        calls = []

        def _sometimes_fails(text, exaggeration=None):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("mps hiccup")
            return real_synthesize(text, exaggeration=exaggeration)

        _install(monkeypatch)
        monkeypatch.setattr(tts, "synthesize", _sometimes_fails)

        with caplog.at_level("WARNING", logger="zoe"):
            tts.warm_up()          # must not raise

        assert any("warm-up failed" in r.message for r in caplog.records)
        assert tts.synthesize("Hello.").size > 0, "speech blocked by warm-up"


class TestSegmentation:
    """Punctuation pauses are Kokoro's equivalent of expression."""

    def test_pause_lengths_follow_the_punctuation(self):
        segments = dict(tts._segments("Wait. Yes, go on! Really?"))
        assert segments["Wait."] > 0
        assert segments["Yes,"] == pytest.approx(0.14, abs=0.01)
        assert segments["go on!"] == pytest.approx(0.30, abs=0.01)
        assert segments["Really?"] == pytest.approx(0.32, abs=0.01)

    def test_an_ellipsis_is_the_longest_pause(self):
        segments = dict(tts._segments("Wait... Go."))
        assert segments["Wait..."] == pytest.approx(0.45, abs=0.01)
        assert segments["Wait..."] > segments["Go."]

    @pytest.mark.requires_kokoro
    def test_pauses_are_actually_rendered_into_the_audio(self, monkeypatch):
        """The pause must exist in the waveform, not just in the table."""
        _install(monkeypatch)
        audio = tts.synthesize("Wait. Go.")
        # The fake pipeline emits constant tone, so silence is only ever a pause.
        quiet = np.abs(audio) < 1e-6
        longest, run = 0, 0
        for is_quiet in quiet:
            run = run + 1 if is_quiet else 0
            longest = max(longest, run)
        assert longest == pytest.approx(int(tts.KOKORO_RATE * 0.30),
                                        abs=int(tts.KOKORO_RATE * 0.02)), \
            "the declared 0.30 s pause is not present in the rendered audio"

    @pytest.mark.requires_kokoro
    def test_empty_text_is_not_synthesized(self, monkeypatch):
        _install(monkeypatch)
        assert tts.synthesize("").size == 0
        assert tts.synthesize("   ").size == 0


class TestFailureReporting:
    @pytest.mark.requires_kokoro
    def test_a_pipeline_failure_is_visible_not_silent(self, monkeypatch):
        def _boom(lang_code):
            raise RuntimeError("Kokoro is not installed. Run: pip install kokoro soundfile")

        import kokoro
        monkeypatch.setattr(kokoro, "KPipeline", _boom)
        with pytest.raises(RuntimeError, match="pip install kokoro"):
            tts.synthesize("Hello.")
        assert tts._pipeline is None, "a failed build must not be cached"

    @pytest.mark.requires_kokoro
    def test_the_service_recovers_after_a_failed_build(self, monkeypatch):
        fake, built = _install(monkeypatch)
        tts._pipeline = None
        import kokoro
        monkeypatch.setattr(kokoro, "KPipeline",
                            lambda lang_code: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            tts.synthesize("Hello.")

        monkeypatch.setattr(kokoro, "KPipeline", lambda lang_code: fake)
        assert tts.synthesize("Hello.").size > 0, "a transient failure was sticky"
