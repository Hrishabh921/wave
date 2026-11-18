"""
tests/test_tier18_vision_describe.py — Tier 18: describe_screen.

Covers two things:
  1. brain/groq_core.py's _vision_describe_image - the side-effect-free
     core pulled out of _think_with_image (which keeps its own
     db.save_message/return wrapping - see that method's own docstring).
     tests/test_tier10_fixes.py already pins _think_with_image's "no
     client configured" error-message wording; this file adds the
     success-path coverage that refactor didn't previously have, plus
     the new core method's own direct tests.
  2. brain/handlers/vision.py's new describe_screen skill: screenshot ->
     vision model, with an OCR fallback (same OCR read_screen_text
     already uses) when no vision-capable provider is configured or the
     vision call itself fails.

Run: python -m unittest discover -s tests -p "test_tier18_vision_describe.py"
(pytest will also collect it.)
"""
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import brain.groq_core as groq_core                        # noqa: E402
from brain.handlers import vision as vision_handlers        # noqa: E402


def _make_bare_brain(client=None, vision_model="vision-m1", problem=None):
    """A WaveBrain with none of __init__'s real setup (no DB/audit/threads) -
    same object.__new__ trick tests/test_tier10_fixes.py's own _brain()
    helper uses, extended with a settable .client/.vision_model."""
    brain = object.__new__(groq_core.WaveBrain)
    brain.client = client
    brain.vision_model = vision_model
    brain.provider_info = lambda: {"problem": problem or ""}
    return brain


def _fake_response(text):
    msg = mock.Mock(content=text)
    choice = mock.Mock(message=msg)
    return mock.Mock(choices=[choice])


class VisionDescribeImageCoreTests(unittest.TestCase):
    """Direct tests of _vision_describe_image - no db.save_message
    involved, unlike _think_with_image below."""

    def test_no_client_configured_returns_a_friendly_message_naming_the_real_problem(self):
        brain = _make_bare_brain(client=None, problem="OPENAI_API_KEY is missing")
        reply = brain._vision_describe_image("some/path.png", "what is this")
        self.assertIn("OPENAI_API_KEY is missing", reply)
        self.assertNotIn("Groq", reply)

    def test_no_client_and_no_known_problem_points_at_the_env(self):
        brain = _make_bare_brain(client=None, problem="")
        reply = brain._vision_describe_image("some/path.png", "what is this")
        self.assertIn("LLM_PROVIDER", reply)

    def test_no_client_never_touches_the_image_path(self):
        # The "no client" branch must return before ever needing a real,
        # readable image file - a None/garbage path must not raise.
        brain = _make_bare_brain(client=None)
        reply = brain._vision_describe_image(None, "what is this")
        self.assertIn("can't look at images", reply)

    def test_successful_call_passes_the_image_and_prompt_to_the_vision_model(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _fake_response("A browser window showing an error dialog.")
        brain = _make_bare_brain(client=client, vision_model="qwen/qwen3.8-27b")
        with mock.patch.object(groq_core.WaveBrain, "_encode_image_for_vision",
                                return_value="data:image/jpeg;base64,AAAA"):
            reply = brain._vision_describe_image("some/path.png", "what's this error?")
        self.assertEqual(reply, "A browser window showing an error dialog.")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "qwen/qwen3.8-27b")
        content = kwargs["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what's this error?"})
        self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,AAAA")

    def test_empty_model_reply_gets_a_friendly_fallback_sentence(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _fake_response("")
        brain = _make_bare_brain(client=client)
        with mock.patch.object(groq_core.WaveBrain, "_encode_image_for_vision", return_value="data:x"):
            reply = brain._vision_describe_image("some/path.png", "describe it")
        self.assertIn("didn't have anything to add", reply)

    def test_a_raised_exception_is_reported_plainly_not_propagated(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("connection reset")
        brain = _make_bare_brain(client=client)
        with mock.patch.object(groq_core.WaveBrain, "_encode_image_for_vision", return_value="data:x"):
            reply = brain._vision_describe_image("some/path.png", "describe it")
        self.assertIn("couldn't process that image", reply)
        self.assertIn("connection reset", reply)


class ThinkWithImageStillWorksAfterTheRefactorTests(unittest.TestCase):
    """_think_with_image's "no client" branch is already pinned by
    tests/test_tier10_fixes.py - this adds the SUCCESS path, which that
    file never covered, so the refactor that split _vision_describe_image
    out has real regression coverage on both branches."""

    def test_success_path_saves_and_returns_the_reply(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _fake_response("A cat sitting on a keyboard.")
        brain = _make_bare_brain(client=client)
        with mock.patch.object(groq_core.WaveBrain, "_encode_image_for_vision", return_value="data:x"), \
             mock.patch.object(groq_core.db, "save_message") as save_message:
            reply = groq_core.WaveBrain._think_with_image(brain, "what is this", {"path": "photo.jpg", "kind": "image"})
        self.assertEqual(reply, "A cat sitting on a keyboard.")
        save_message.assert_called_once_with("assistant", "A cat sitting on a keyboard.")

    def test_blank_user_text_gets_a_sensible_default_prompt(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _fake_response("ok")
        brain = _make_bare_brain(client=client)
        with mock.patch.object(groq_core.WaveBrain, "_encode_image_for_vision", return_value="data:x"), \
             mock.patch.object(groq_core.db, "save_message"):
            groq_core.WaveBrain._think_with_image(brain, "   ", {"path": "photo.jpg", "kind": "image"})
        content = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Describe it", content[0]["text"])

    def test_no_client_still_does_not_need_a_real_path_key(self):
        # The exact scenario tests/test_tier10_fixes.py exercises with {}
        # as the attachment - must not KeyError before the "no client"
        # guard gets a chance to run.
        brain = _make_bare_brain(client=None, problem="")
        with mock.patch.object(groq_core.db, "save_message"):
            reply = groq_core.WaveBrain._think_with_image(brain, "what is this", {})
        self.assertIn("LLM_PROVIDER", reply)


class DescribeScreenSkillTests(unittest.TestCase):
    def _args(self, question=""):
        return {"question": question}

    def test_screenshot_capture_failure_is_reported_plainly(self):
        brain = _make_bare_brain(client=mock.Mock())
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value=""):
            reply = vision_handlers._skill_describe_screen(brain, self._args())
        self.assertIn("couldn't capture the screen", reply)

    def test_vision_available_returns_the_description_wrapped_as_untrusted(self):
        brain = _make_bare_brain(client=mock.Mock())
        brain._vision_describe_image = mock.Mock(return_value="A terminal window with a red error message.")
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value="shot.png"), \
             mock.patch.object(vision_handlers.ocr, "extract_text") as extract_text:
            reply = vision_handlers._skill_describe_screen(brain, self._args("what's this error?"))
        extract_text.assert_not_called()   # vision succeeded - OCR fallback must not even run
        self.assertIn("A terminal window with a red error message.", reply)
        # content_trust.wrap_untrusted wraps the text with an instructional
        # header - same invariant test_content_trust_fixtures.py checks for
        # every other wrap_untrusted call site.
        self.assertIn("the screen (vision)", reply)
        brain._vision_describe_image.assert_called_once_with("shot.png", "what's this error?")

    def test_blank_question_gets_a_sensible_default_prompt(self):
        brain = _make_bare_brain(client=mock.Mock())
        brain._vision_describe_image = mock.Mock(return_value="A desktop with no open windows.")
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value="shot.png"):
            vision_handlers._skill_describe_screen(brain, self._args(""))
        prompt = brain._vision_describe_image.call_args.args[1]
        self.assertIn("What's on this screen", prompt)

    def test_no_vision_provider_falls_back_to_ocr(self):
        brain = _make_bare_brain(client=None, problem="")
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value="shot.png"), \
             mock.patch.object(vision_handlers.ocr, "extract_text", return_value="Error: file not found") as extract_text:
            reply = vision_handlers._skill_describe_screen(brain, self._args())
        extract_text.assert_called_once_with("shot.png")
        self.assertIn("Error: file not found", reply)
        self.assertIn("OCR fallback", reply)

    def test_vision_call_raising_also_falls_back_to_ocr(self):
        brain = _make_bare_brain(client=mock.Mock())
        brain._vision_describe_image = mock.Mock(
            return_value="I couldn't process that image — something went wrong talking to the vision model: boom")
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value="shot.png"), \
             mock.patch.object(vision_handlers.ocr, "extract_text", return_value="Settings > Privacy") as extract_text:
            reply = vision_handlers._skill_describe_screen(brain, self._args())
        extract_text.assert_called_once_with("shot.png")
        self.assertIn("Settings > Privacy", reply)

    def test_no_vision_and_ocr_finds_nothing_gives_one_combined_honest_message(self):
        brain = _make_bare_brain(client=None, problem="")
        with mock.patch.object(vision_handlers.VisionCapture, "take_screenshot", return_value="shot.png"), \
             mock.patch.object(vision_handlers.ocr, "extract_text", return_value=""):
            reply = vision_handlers._skill_describe_screen(brain, self._args())
        self.assertIn("can't look at images", reply)
        self.assertIn("found nothing readable either", reply)


if __name__ == "__main__":
    unittest.main()
