"""
tests/test_tier29_reply_reliability.py — replies that don't stall, and a
Dodo who only moves her lips while there is sound.

What went wrong before this tier (all seen in the user's own log):
  * a 429 made the Groq SDK sit out Retry-After (~30 s) twice, then the
    turn fell to a local Ollama that wasn't running (~9 s more), then to a
    raw "System Error: ...";
  * every stray transcript of room noise while AWAKE became a cloud request,
    using up the per-minute quota (and cutting off the reply in progress);
  * a cancelled turn's late chat_done ended the NEXT reply's bubble with
    "(no reply)";
  * the avatar switched to SPEAKING the moment a reply was requested, so the
    page moved her lips through the whole wait.

    python -m unittest discover -s tests -p "test_tier29_reply_reliability.py"
"""
import os
import socket
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import brain.groq_core as gc  # noqa: E402
from brain.groq_core import WaveBrain  # noqa: E402
from settings.config import config  # noqa: E402
from voice.stt import Transcript  # noqa: E402
from webui.voice_worker import WebContinuousMicWorker  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _RateLimited(Exception):
    status_code = 429

    def __init__(self, retry_after=12):
        super().__init__(f"Error code: 429 - Rate limit reached. Please try again in {retry_after}s.")
        self.response = type("R", (), {"headers": {"retry-after": str(retry_after)}, "status_code": 429})()


class _ServerError(Exception):
    status_code = 503


class _Client:
    """chat.completions.create() that answers from a per-model script."""

    def __init__(self, script):
        self.script = {m: list(v) for m, v in script.items()}
        self.calls = []
        outer = self

        class Completions:
            def create(self, **kw):
                outer.calls.append(kw["model"])
                step = outer.script[kw["model"]].pop(0)
                if isinstance(step, Exception):
                    raise step
                return step

        class Chat:
            completions = Completions()
        self.chat = Chat()


def _brain(script, provider_name="groq"):
    b = object.__new__(WaveBrain)                 # the real methods, no network/provider setup
    b.model = "openai/gpt-oss-120b"
    b.provider = type("P", (), {"name": provider_name})()
    b.client = _Client(script)
    b._model_cooldown = {}
    b._ollama_down_until = 0.0
    return b


class FailoverTests(unittest.TestCase):
    def test_a_rate_limited_model_hands_the_request_to_the_backup_at_once(self):
        b = _brain({"openai/gpt-oss-120b": [_RateLimited(12)], "openai/gpt-oss-20b": ["answer"]})
        t = time.monotonic()
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "answer")
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(b.client.calls, ["openai/gpt-oss-120b", "openai/gpt-oss-20b"])
        self.assertGreater(b._model_cooldown["openai/gpt-oss-120b"], time.monotonic() + 10)

    def test_while_cooling_down_the_backup_is_asked_first(self):
        b = _brain({"openai/gpt-oss-120b": [_RateLimited(30)], "openai/gpt-oss-20b": ["one", "two"]})
        b._llm_create(model=b.model, messages=[])
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "two")
        self.assertEqual(b.client.calls, ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-20b"])

    def test_when_both_are_limited_briefly_it_waits_once_then_answers(self):
        b = _brain({"openai/gpt-oss-120b": [_RateLimited(3), "late answer"], "openai/gpt-oss-20b": [_RateLimited(5)]})
        with mock.patch.object(gc.time, "sleep") as sleep:
            self.assertEqual(b._llm_create(model=b.model, messages=[]), "late answer")
        self.assertEqual(len(sleep.call_args_list), 1)
        self.assertLessEqual(sleep.call_args_list[0][0][0], 4.0)

    def test_when_both_are_limited_for_long_the_user_hears_busy_not_an_error(self):
        b = _brain({"openai/gpt-oss-120b": [_RateLimited(60)], "openai/gpt-oss-20b": [_RateLimited(60)]})
        with self.assertRaises(_RateLimited) as ctx:
            b._llm_create(model=b.model, messages=[])
        with mock.patch.object(config, "OLLAMA_ENABLED", False):
            reply = b._reply_after_failure(ctx.exception, "hello")
        self.assertIn("busy", reply)
        self.assertNotIn("System Error", reply)

    def test_a_5xx_gets_one_quick_retry_on_the_same_model(self):
        b = _brain({"openai/gpt-oss-120b": [_ServerError("503"), "ok"], "openai/gpt-oss-20b": []})
        with mock.patch.object(gc.time, "sleep"):
            self.assertEqual(b._llm_create(model=b.model, messages=[]), "ok")
        self.assertEqual(b.client.calls, ["openai/gpt-oss-120b", "openai/gpt-oss-120b"])

    def test_backup_model_choice(self):
        with mock.patch.object(config, "LLM_FALLBACK_MODEL", ""):
            self.assertEqual(_brain({})._backup_model(), "openai/gpt-oss-20b")
            self.assertIsNone(_brain({}, provider_name="openai")._backup_model())
        with mock.patch.object(config, "LLM_FALLBACK_MODEL", "off"):
            self.assertIsNone(_brain({})._backup_model())
        with mock.patch.object(config, "LLM_FALLBACK_MODEL", "my/other-model"):
            self.assertEqual(_brain({}, provider_name="openai")._backup_model(), "my/other-model")

    def test_retry_after_is_read_from_header_or_message(self):
        self.assertEqual(gc._retry_after_s(_RateLimited(7)), 7.0)
        self.assertAlmostEqual(gc._retry_after_s(Exception("Please try again in 1m2.5s.")), 62.5)
        self.assertEqual(gc._retry_after_s(Exception("no hint")), 20.0)
        self.assertTrue(gc._is_rate_limited(Exception("Request too large for model on tokens per minute")))
        self.assertFalse(gc._is_rate_limited(Exception("invalid api key")))


class FallbackPathTests(unittest.TestCase):
    def test_a_stopped_ollama_is_skipped_within_half_a_second_and_remembered(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                  # nothing listens there now
        b = _brain({})
        with mock.patch.object(config, "OLLAMA_HOST", f"http://localhost:{port}"):
            t = time.monotonic()
            self.assertFalse(b._ollama_reachable())
            self.assertLess(time.monotonic() - t, 1.5)
            t = time.monotonic()
            self.assertFalse(b._ollama_reachable())          # remembered - no second wait
            self.assertLess(time.monotonic() - t, 0.05)

    def test_history_stays_within_its_budget_newest_first(self):
        rows = [("user", "old " * 600), ("assistant", "x" * 5000), ("user", "recent question")]
        with mock.patch.object(gc.db, "get_chat_history", return_value=rows), \
                mock.patch.object(config, "LLM_HISTORY_CHARS", 1500):
            kept = WaveBrain._recent_history(16)
        self.assertEqual(kept[-1], ("user", "recent question"))
        self.assertTrue(all(len(m) <= 1202 for _, m in kept))
        self.assertLessEqual(sum(len(m) for _, m in kept), 1500 + 1202)


def _heard(text, confidence=None, engine="whisper"):
    t = Transcript(text, engine=engine)
    t.confidence = confidence
    return t


class MicNoiseTests(unittest.TestCase):
    def _worker(self):
        got = {"text": [], "modes": []}
        w = WebContinuousMicWorker(wake_phrases=["hey wave"], on_text_ready=got["text"].append,
                                   on_mode_changed=lambda m, a: got["modes"].append((m, a)))
        w.mode = "AWAKE"
        return w, got

    def test_short_unsure_unaddressed_noise_is_not_sent(self):
        w, got = self._worker()
        for noise in ("Gatya", "Salas un tini", "You have eaten."):
            w.handle_text(_heard(noise, confidence=0.41))
        self.assertEqual(got["text"], [])

    def test_clear_speech_or_longer_speech_or_the_wake_word_still_goes_through(self):
        w, got = self._worker()
        w.handle_text(_heard("what is the time", confidence=0.82))
        w.handle_text(_heard("can you tell me something about the moon", confidence=0.42))
        w.handle_text(_heard("hey wave joke", confidence=0.40))
        self.assertEqual(got["text"], ["what is the time", "can you tell me something about the moon", "joke"])

    def test_commands_the_intent_router_knows_are_never_dropped(self):
        w, got = self._worker()
        w.handle_text(_heard("volume up", confidence=0.38))
        self.assertEqual(got["text"], ["volume up"])

    def test_after_a_quiet_minute_awake_goes_back_to_waiting_for_the_wake_word(self):
        w, got = self._worker()
        w._last_activity = time.monotonic() - 3600
        with mock.patch.object(config, "AWAKE_IDLE_SLEEP_S", 60):
            w.handle_text(_heard("what is the weather", confidence=0.9))
        self.assertEqual(w.mode, "SLEEPING")
        self.assertEqual(got["text"], [])                   # needs "hey wave" now
        self.assertEqual(got["modes"][-1], ("SLEEPING", False))
        w.handle_text(_heard("hey wave what is the weather", confidence=0.9))
        self.assertEqual(got["text"], ["what is the weather"])


class AvatarTimingTests(unittest.TestCase):
    """The bridge only marks the avatar SPEAKING once audio really starts."""

    def test_speaking_starts_with_the_audio_not_with_the_request(self):
        from webui.bridge import WaveWebBridge
        bridge = object.__new__(WaveWebBridge)
        pushed = []
        bridge._push = lambda event, payload=None: pushed.append((event, payload))
        from avatar.state import AvatarStateMachine
        bridge._avatar_state = AvatarStateMachine()
        bridge._speech_pending = False
        bridge._avatar_state.on_reply_started()
        bridge._avatar_speech_pending()
        self.assertEqual(bridge._avatar_state.state.value, "thinking")
        bridge._on_tts_audio_start()
        self.assertEqual(bridge._avatar_state.state.value, "speaking")
        n = len(pushed)
        bridge._on_tts_audio_start()                        # the next sentence starting changes nothing
        self.assertEqual(len(pushed), n)


class PageTests(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(REPO, "webui", "static", *parts), encoding="utf-8") as f:
            return f.read()

    def test_lips_move_only_with_real_audio(self):
        js = self._read("js", "avatar.js")
        self.assertNotIn("speakingSince", js)
        self.assertNotIn("gotEnv", js)
        self.assertIn("if (L < 2 || !speech) return 0;", js)
        self.assertIn('wave.on("tts_finished"', js)

    def test_replies_carry_their_turn_number(self):
        js = self._read("js", "app.js")
        self.assertIn("!!fromVoice, turn);", js)
        self.assertIn("if (forCurrentTurn(turn)) onReplyDone(text);", js)
        self.assertNotIn('"(no reply)"', js)


if __name__ == "__main__":
    unittest.main()
