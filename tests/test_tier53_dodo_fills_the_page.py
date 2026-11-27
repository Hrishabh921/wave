"""tests/test_tier53_dodo_fills_the_page.py - Tier 53: Dodo shows the whole
photo, as big as the window allows.

The owner (2026-11-27): the photo sat small in the middle of the Dodo page;
it should fill the window, and nothing of it may be cut off. There's no
browser in a test run, so these read the page's own files for the pieces
that make that so. (Checked by eye when it was built: the real page in a
headless Edge with a fake bridge kept the photo's exact 16:9 shape at five
window sizes, filling the width or the height every time.)
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _rule(css, selector):
    """The declarations of the first unindented `selector { ... }` rule."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


class DodoFillsThePageTests(unittest.TestCase):
    def setUp(self):
        self.js = _read("webui", "static", "js", "avatar.js")
        self.css = _read("webui", "static", "css", "main.css")

    def test_the_frame_gets_the_largest_uncropped_fit(self):
        # Full width unless that makes it too tall, then full height - the
        # photo's own shape either way - and again whenever the window resizes.
        self.assertIn("let fw = w, fh = w / aspect;", self.js)
        self.assertIn("if (fh > h) { fh = h; fw = h * aspect; }", self.js)
        self.assertIn("new ResizeObserver(fit)", self.js)
        self.assertIn("fitFrame(mountEl, aspect || 1);", self.js)
        self.assertIn("const [w, h] = info.manifest.photo || [1, 1];", self.js)

    def test_a_rebuild_drops_the_old_size_watcher(self):
        reload_body = self.js[self.js.index("async function reload()"):][:600]
        self.assertIn("fitObserver.disconnect(); fitObserver = null;", reload_body)

    def test_nothing_in_the_css_caps_or_crops_the_photo(self):
        frame = _rule(self.css, ".avatar-frame")
        self.assertIn("max-width: 100%", frame)
        self.assertIn("max-height: 100%", frame)
        self.assertNotRegex(frame, r"(?<![-\w])(width|height)\s*:\s*\d+px")     # no fixed size
        self.assertIn("object-fit: contain", _rule(self.css, ".avatar-img"))    # the 5-frame rig too
        canvas = _rule(self.css, ".avatar-canvas")
        self.assertIn("width: 100%", canvas)
        self.assertIn("height: 100%", canvas)
        for selector in (".dodo-stage", ".dodo-mount"):
            self.assertIn("flex: 1 1 0", _rule(self.css, selector))

    def test_the_state_line_floats_on_the_photo(self):
        self.assertIn("position: absolute", _rule(self.css, ".dodo-state"))
        self.assertIn("anchor-name: --dodo-frame", _rule(self.css, ".avatar-frame"))
        self.assertIn("position-anchor: --dodo-frame", self.css)
        # ...and above the render-progress strip while one shows
        self.assertEqual(self.css.count(".dodo-stage:has(.avatar-live-progress:not(.hidden)) .dodo-state"), 2)

    def test_the_lifelike_frames_are_stored_full_size(self):
        self.assertRegex(_read("avatar", "live_render.py"),
                         r'add_argument\("--scale", type=float, default=1\.0\)')
        # Settings -> Avatar's render (avatar/live.py) uses that default
        self.assertNotIn("--scale", _read("avatar", "live.py"))


if __name__ == "__main__":
    unittest.main()
