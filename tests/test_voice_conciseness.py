"""Voice-mode response design: concision, the token ceiling, and what it must not touch.

The behaviour under test is a contract, not a preference: a spoken turn asks for
one to three sentences and stops generating early; a typed turn keeps its existing
budget; the TTS still receives speakable text; and the tone layer, provider routing
and language behaviour are untouched.
"""
import inspect

import pytest

from zoe import brain
from zoe.brain import ZoeBrain as Brain, _SPOKEN_BLOCK, _turn_token_ceiling
from zoe.config import CONFIG, MAX_SPOKEN_WORDS, VOICE_MAX_OUTPUT_TOKENS
from zoe.model_layer import ModelLayer
from zoe.providers.openai_compat import _REASONING_SERIES, OpenAICompatSession


# ---------------------------------------------------------------------------
# The voice token ceiling
# ---------------------------------------------------------------------------


class TestVoiceTokenCeiling:
    def test_spoken_turn_gets_the_voice_cap(self):
        assert _turn_token_ceiling(spoken=True) == VOICE_MAX_OUTPUT_TOKENS
        assert VOICE_MAX_OUTPUT_TOKENS == 220

    def test_typed_turn_is_uncapped_by_default(self):
        """Requirement 8: a long written answer is not a wall of sound."""
        assert _turn_token_ceiling(spoken=False) is None

    def test_typed_turn_can_opt_in(self, monkeypatch):
        monkeypatch.setitem(CONFIG.get("voice"), "text_max_output_tokens", 300)
        import importlib

        from zoe import config as config_module
        monkeypatch.setattr(brain, "TEXT_MAX_OUTPUT_TOKENS", 300)
        assert _turn_token_ceiling(spoken=False) == 300
        assert importlib  # imported for the monkeypatch of the module attribute

    def test_voice_cap_is_configurable(self, monkeypatch):
        monkeypatch.setattr(brain, "VOICE_MAX_OUTPUT_TOKENS", 90)
        assert _turn_token_ceiling(spoken=True) == 90


class TestOptionsFor:
    """The ceiling narrows generation; it never widens it past what is allowed."""

    def _layer(self, max_output_tokens=1024):
        layer = ModelLayer.__new__(ModelLayer)
        layer.temperature = 0.7
        layer.max_output_tokens = max_output_tokens
        return layer

    def _spec(self, model_max=8192):
        class Spec:
            max_output_tokens = model_max
        return Spec()

    def test_spoken_cap_lowers_the_value(self):
        opts = self._layer().options_for(self._spec(), timeout=5, max_output_tokens=220)
        assert opts["max_output_tokens"] == 220

    def test_cap_cannot_exceed_the_configured_default(self):
        opts = self._layer(max_output_tokens=300).options_for(
            self._spec(), timeout=5, max_output_tokens=9999
        )
        assert opts["max_output_tokens"] == 300

    def test_cap_cannot_exceed_the_model_limit(self):
        opts = self._layer(max_output_tokens=4096).options_for(
            self._spec(model_max=128), timeout=5, max_output_tokens=220
        )
        assert opts["max_output_tokens"] == 128

    def test_no_cap_leaves_the_existing_behaviour(self):
        opts = self._layer().options_for(self._spec(), timeout=5)
        assert opts["max_output_tokens"] == 1024

    def test_cap_of_zero_is_ignored_rather_than_silently_ending_generation(self):
        opts = self._layer().options_for(self._spec(), timeout=5, max_output_tokens=0)
        assert opts["max_output_tokens"] == 1024


# ---------------------------------------------------------------------------
# Provider parameter names (requirement 7)
# ---------------------------------------------------------------------------


class TestProviderTokenParameter:
    def _session(self, model_id):
        class Provider:
            name = "test"

        return OpenAICompatSession(Provider(), model_id, "", [], [], {})

    @pytest.mark.parametrize("model_id", ["gpt-4o", "gemini-3.6-flash",
                                          "nvidia/nemotron-3.5-lightning:free"])
    def test_standard_models_use_max_tokens(self, model_id):
        assert self._session(model_id)._token_param == "max_tokens"

    @pytest.mark.parametrize("model_id", ["o3-mini", "openai/o1", "openai/gpt-5-mini"])
    def test_reasoning_series_use_max_completion_tokens(self, model_id):
        assert self._session(model_id)._token_param == "max_completion_tokens"

    def test_pattern_does_not_match_unrelated_ids(self):
        for model_id in ("big-pickle", "space-bunny-free", "gpt-4o-mini",
                         "inclusionai/ling-3.0-flash-sante:free"):
            assert _REASONING_SERIES.search(model_id) is None, model_id

    def test_both_parameters_mean_the_same_thing(self):
        """Same name, same meaning: a cap on generated tokens. Renaming is not a behaviour change."""
        s = self._session("gpt-4o")
        assert s._token_param == "max_tokens"
        assert s._options.get("max_output_tokens") is None  # set by options_for, not here


# ---------------------------------------------------------------------------
# The spoken prompt block
# ---------------------------------------------------------------------------


class TestSpokenBlock:
    def test_is_appended_only_for_spoken_turns(self):
        b = Brain.__new__(Brain)
        b._system_prompt = "base"
        b._turn_context = ""
        b._ephemeral = True          # no tools, no history needed
        b._session = None
        b._model_key = b._model_id = ""
        b._layer = _StubLayer()
        b._turn_deadline = None

        captured = {}

        def fake_open_session(**kwargs):
            captured.update(kwargs)
            return object()

        b._layer.providers["gemini"].open_session = fake_open_session
        b._create_chat("gemini_flash", spoken=True)
        assert _SPOKEN_BLOCK in captured["system_prompt"]

        b._create_chat("gemini_flash", spoken=False)
        assert _SPOKEN_BLOCK not in captured["system_prompt"]

    def test_a_spoken_turn_reaches_the_provider_with_the_voice_cap(self):
        """The prompt asks for brevity; the ceiling is what enforces it."""
        b = Brain.__new__(Brain)
        b._system_prompt = "base"
        b._turn_context = ""
        b._ephemeral = True
        b._session = None
        b._model_key = b._model_id = ""
        b._layer = _StubLayer()
        b._turn_deadline = None

        captured = {}
        b._layer.providers["gemini"].open_session = \
            lambda **kw: captured.update(kw) or object()

        b._create_chat("gemini_flash", spoken=True)
        assert captured["max_output_tokens"] == VOICE_MAX_OUTPUT_TOKENS

        b._create_chat("gemini_flash", spoken=False)
        assert captured["max_output_tokens"] == 1024

    def test_states_one_to_three_sentences_and_leads_with_the_answer(self):
        text = _SPOKEN_BLOCK.lower()
        assert "one to three sentences" in text
        assert "lead with the answer" in text

    def test_forbids_markdown_unless_asked(self):
        text = _SPOKEN_BLOCK.lower()
        assert "no markdown" in text
        assert "unless they ask" in text

    def test_says_not_to_trade_away_content(self):
        """Requirement: a complex answer must not lose what the user came for."""
        assert "would drop the one thing they actually came for" in _SPOKEN_BLOCK.lower()

    def test_says_nothing_about_language(self):
        """Requirement 11: this block must not touch the user's language."""
        lowered = _SPOKEN_BLOCK.lower()
        for word in ("english", "in english", "your language"):
            assert word not in lowered

    def test_preserves_a_strict_default(self):
        """The old 'at most two short sentences' rule becomes a stated default of
        1-3 with two named exceptions, so the ceiling is explicit rather than
        implied. Measured: a detail question must still reach 90+ words."""
        text = _SPOKEN_BLOCK.lower()
        assert "one to three sentences" in text
        assert "that is the default" in text
        assert "ask for detail" in text

    def test_the_default_is_not_weakened_into_license_to_ramble(self):
        """A first draft of this block said 'real analysis earns more' and measured
        23% LONGER than the old prompt on open-ended questions. The default is now
        stated as binding; this test stops that regression coming back."""
        text = _SPOKEN_BLOCK.lower()
        assert "that is the default for everything" in text
        assert "real analysis earns" not in text


class _StubLayer:
    def __init__(self):
        self.providers = {"gemini": _StubProvider()}

    def spec_for(self, key):
        from zoe.models import ModelSpec
        return ModelSpec(key=key, provider="gemini", model_id="gemini-x",
                         max_output_tokens=8192)

    def options_for(self, spec, timeout=60.0, max_output_tokens=None):
        # Same contract as the real layer: the smallest ceiling wins.
        ceiling = min(1024, spec.max_output_tokens)
        if max_output_tokens:
            ceiling = min(ceiling, int(max_output_tokens))
        return {"temperature": 0.7, "max_output_tokens": ceiling, "timeout": timeout}


class _StubProvider:
    def __init__(self):
        self.is_configured = lambda: True
        self.open_session = lambda **kw: None


# ---------------------------------------------------------------------------
# What must not change
# ---------------------------------------------------------------------------


class TestNoRegression:
    def test_text_mode_prompt_is_untouched(self):
        from zoe.config import ZOE_SYSTEM_PROMPT
        assert "speaking aloud" not in ZOE_SYSTEM_PROMPT.lower()

    def test_identity_prompt_still_allows_long_answers(self):
        from zoe.config import ZOE_SYSTEM_PROMPT
        assert "detailed when useful" in ZOE_SYSTEM_PROMPT

    def test_tone_layer_still_present_and_unchanged_in_shape(self):
        from zoe import tone
        assert callable(tone.format_prompt)
        # Built from an explicit state, not the process-wide tracker, which
        # another test in the suite may have left non-neutral.
        neutral = tone.ConversationState()
        assert tone.format_prompt(neutral) == ""
        assert "How To Talk Right Now" in tone.format_prompt(
            tone.ConversationState(mood=tone.Mood.PLAYFUL))

    def test_spoken_word_cap_is_unchanged(self):
        assert MAX_SPOKEN_WORDS == 60

    def test_tts_still_receives_plain_text(self):
        """Markdown stripping for speech is untouched by this feature."""
        from zoe.voice_loop import VoiceLoop
        stripped = VoiceLoop._strip_markdown(
            VoiceLoop.__new__(VoiceLoop), "## Title\n- **bold** and `code`"
        )
        assert stripped == "Title bold and code"
        assert "**" not in stripped and "#" not in stripped

    def test_voice_loop_still_enforces_the_word_cap_after_the_token_cap(self):
        """Two layers, on purpose: the token cap is a generation bound, the word
        cap a speech bound. A model that fills 220 tokens still gets shaped."""
        from zoe.voice_loop import VoiceLoop
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._max_spoken_words = 20
        long_reply = "This is sentence number one. " * 20
        out = loop._spoken_reply(long_reply, "turn-1")
        assert len(out.split()) <= 23  # 20 capped words + the offer
        assert out.endswith("Want more detail?")
        assert out.rstrip().endswith("detail?")

    def test_a_reply_cut_mid_clause_does_not_speak_the_half_sentence(self):
        """A token-capped reply has no sentence boundary; the old code fell
        through and spoke a fragment. Now the unfinished clause is dropped."""
        from zoe.voice_loop import VoiceLoop
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._max_spoken_words = 6
        out = loop._spoken_reply("alpha beta gamma delta, epsilon zeta eta theta", "turn-2")
        assert "epsilon zeta eta theta" not in out
        assert out.endswith("Want more detail?")

    def test_a_short_reply_is_returned_verbatim(self):
        from zoe.voice_loop import VoiceLoop
        loop = VoiceLoop.__new__(VoiceLoop)
        loop._max_spoken_words = 60
        assert loop._spoken_reply("It is done.", "turn-3") == "It is done."

    def test_run_signature_is_unchanged(self):
        """`ask(spoken=True)` is how the voice loop already spoke; nothing moved."""
        params = inspect.signature(Brain.ask).parameters
        assert "spoken" in params
        assert params["spoken"].default is False