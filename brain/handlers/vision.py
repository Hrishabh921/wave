"""
brain/handlers/vision.py - screenshots, screen OCR, and the webcam.
take_screenshot/delete_latest_screenshot go through AppController (which
also wires them into the undo/audit log); read_screen_text/take_photo go
through VisionCapture directly, same split as before this file existed.

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown to
1942+ lines with 50 _skill_* methods - a "no behavior change, pure refactor" per
the roadmap. Each function here is registered via the exact same @skill(...)
decorator at module-import time (brain/skills_registry.py's register() has
always documented its handlers as plain `fn(brain, args: dict) -> str` functions,
not bound methods - both _dispatch_tool() and _replay_confirmed() already call
handler(self, args) with the WaveBrain instance passed explicitly, so moving a
method out of the class into a plain top-level function taking `brain` instead of
`self` needed no change to either call site). See brain/handlers/__init__.py for
why these 8 files, and groq_core.py's own top for why it still imports this
package even though nothing in it is referenced by name.
"""
from automation.app_control import AppController
from brain import content_trust
from brain.skills_registry import skill
from vision.ocr_engine import ocr
from vision.screen_capture import VisionCapture


@skill("take_screenshot", "Captures a screenshot of the current screen and saves it.")
def _skill_take_screenshot(brain, args: dict) -> str:
    return AppController.take_screenshot()


@skill("delete_latest_screenshot", "Deletes the most recently taken screenshot.", confirm=True)
def _skill_delete_latest_screenshot(brain, args: dict) -> str:
    return AppController.delete_latest_screenshot()


@skill("read_screen_text", "Captures the screen and reads/extracts any text visible on it (OCR).")
def _skill_read_screen_text(brain, args: dict) -> str:
    path = VisionCapture.take_screenshot()
    if not path:
        return "I couldn't capture the screen to read it."
    extracted = ocr.extract_text(path)
    if not extracted:
        return "I captured the screen but found no readable text."
    # Phase 7: whatever's on the screen could be anyone's content (a
    # browser tab, someone else's message to the user, a document) -
    # mark it as data to report back, not instructions to follow. See
    # brain/content_trust.py.
    return content_trust.wrap_untrusted(extracted, source="the screen (OCR)")


# Tier 18: "what's this error?" / "what's on my screen?" - unlike
# read_screen_text above (OCR only, so it can only ever report literal
# TEXT on screen), this asks the provider's actual vision model to look at
# and describe the screenshot, which also handles things OCR can't -
# icons, layout, a dialog's overall shape, colors, what an error actually
# MEANS rather than just its literal message text. Reuses
# WaveBrain._vision_describe_image (brain/groq_core.py) - the exact same
# encode/call/error-handling path _think_with_image uses for a user-
# attached image, just pointed at a fresh screenshot instead. Falls back
# to OCR (same call read_screen_text makes) when no vision-capable
# provider is configured, or when the vision call itself fails - a
# describe_screen call should still give the user SOMETHING readable
# whenever there's text on screen to extract, rather than a bare error.
_VISION_UNAVAILABLE_PREFIXES = ("i can't look at images", "i couldn't process that image")


@skill("describe_screen",
       "Takes a screenshot of the current screen and describes what's on it using the AI vision model "
       "(falls back to OCR text extraction if no vision model is available) - use for questions like "
       "'what's this error?', 'what's on my screen?', or 'what does this dialog say'.",
       {"question": {"type": "string",
                     "description": "Optional specific question about what's on screen, e.g. 'what does "
                                    "this error mean?'. Leave blank for a general description."}})
def _skill_describe_screen(brain, args: dict) -> str:
    question = (args.get("question") or "").strip()
    path = VisionCapture.take_screenshot()
    if not path:
        return "I couldn't capture the screen to look at it."

    prompt = question or "What's on this screen? Describe it, and point out anything notable like errors or dialogs."
    reply = brain._vision_describe_image(path, prompt)
    if not reply.lower().startswith(_VISION_UNAVAILABLE_PREFIXES):
        # Phase 7: same reasoning as read_screen_text - the screen could
        # show anyone's content, so what the vision model reports back is
        # data, not instructions.
        return content_trust.wrap_untrusted(reply, source="the screen (vision)")

    extracted = ocr.extract_text(path)
    if not extracted:
        return (f"{reply} I also tried reading the screen's text directly, but found nothing readable "
                f"either.")
    return content_trust.wrap_untrusted(extracted, source="the screen (OCR fallback - no vision model available)")


@skill("take_photo", "Takes a photo using the webcam.", confirm=True)
def _skill_take_photo(brain, args: dict) -> str:
    return VisionCapture.take_photo()
