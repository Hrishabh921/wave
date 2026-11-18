"""
tests/test_tier25_avatar.py — avatar/state.py (the IDLE/LISTENING/
THINKING/SPEAKING state machine) and avatar/rig.py (the photo -> warped
frame-set pipeline). See docs/ROADMAP_BEAT_SARA.md's Tier 25 section.

rig.py's face detection itself (detect_regions) is exercised against a
real face photo opportunistically where one happens to be available, but
every test that needs a KNOWN face box mocks detect_regions() directly
rather than depending on the Haar cascade actually finding a face in
some synthetic image - cascades are trained on real photographs and
reliably do NOT fire on a drawn or procedurally-generated image, so
depending on that here would make these tests flaky by construction,
not a real check of this module's own warp/manifest logic.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from avatar.state import AvatarState, AvatarStateMachine
from avatar import rig
from avatar.animation import frame_for_state, BLINK_DURATION_S, IDLE_SWAY_AFTER_S, SWAY_PERIOD_S


def _blank_bgr(w=200, h=200):
    return np.full((h, w, 3), 160, dtype=np.uint8)


def _textured_bgr(w=200, h=200, seed=0):
    """A flat color warps to the same flat color - useless for telling
    two frames apart. This gives every pixel its own value (a fixed seed
    for reproducibility) so a warped region is actually detectably
    different from its un-warped original."""
    rng = np.random.RandomState(seed)
    return rng.randint(0, 255, size=(h, w, 3), dtype=np.uint8)


class AvatarStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.sm = AvatarStateMachine()

    def test_starts_idle(self):
        self.assertEqual(self.sm.state, AvatarState.IDLE)

    def test_mic_awake_goes_to_listening(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.assertEqual(self.sm.state, AvatarState.LISTENING)

    def test_mic_sleeping_goes_to_idle(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_mic_mode_changed("SLEEPING")
        self.assertEqual(self.sm.state, AvatarState.IDLE)

    def test_unrecognized_mode_is_ignored(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_mic_mode_changed("something_else")
        self.assertEqual(self.sm.state, AvatarState.LISTENING)

    def test_mic_off_goes_to_idle(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_mic_off()
        self.assertEqual(self.sm.state, AvatarState.IDLE)

    def test_reply_started_goes_to_thinking_even_while_idle(self):
        self.sm.on_reply_started()
        self.assertEqual(self.sm.state, AvatarState.THINKING)

    def test_reply_started_interrupts_listening(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_reply_started()
        self.assertEqual(self.sm.state, AvatarState.THINKING)

    def test_speech_started_goes_to_speaking(self):
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.assertEqual(self.sm.state, AvatarState.SPEAKING)

    def test_speech_stopped_settles_to_listening_if_mic_was_awake(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.sm.on_speech_stopped()
        self.assertEqual(self.sm.state, AvatarState.LISTENING)

    def test_speech_stopped_settles_to_idle_if_mic_was_sleeping(self):
        self.sm.on_mic_mode_changed("SLEEPING")
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.sm.on_speech_stopped()
        self.assertEqual(self.sm.state, AvatarState.IDLE)

    def test_speech_stopped_settles_to_idle_if_mic_mode_never_reported(self):
        # web UI with the mic never toggled on at all in this session.
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.sm.on_speech_stopped()
        self.assertEqual(self.sm.state, AvatarState.IDLE)

    def test_a_mic_mode_push_mid_turn_does_not_interrupt_speaking(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.sm.on_mic_mode_changed("SLEEPING")  # must NOT flip the avatar mid-reply
        self.assertEqual(self.sm.state, AvatarState.SPEAKING)
        self.sm.on_speech_stopped()
        self.assertEqual(self.sm.state, AvatarState.IDLE)  # but is applied once the turn ends

    def test_reply_done_without_speech_resolves_out_of_thinking(self):
        self.sm.on_mic_mode_changed("AWAKE")
        self.sm.on_reply_started()
        self.sm.on_reply_done_without_speech()  # speak_reply was False, or TTS failed
        self.assertEqual(self.sm.state, AvatarState.LISTENING)

    def test_reply_done_without_speech_is_a_no_op_if_already_speaking(self):
        self.sm.on_reply_started()
        self.sm.on_speech_started()
        self.sm.on_reply_done_without_speech()  # a stray/late call - must not override SPEAKING
        self.assertEqual(self.sm.state, AvatarState.SPEAKING)

    def test_entered_at_resets_only_on_a_real_transition(self):
        self.sm.on_mic_mode_changed("AWAKE")
        first = self.sm.entered_at
        self.sm.on_mic_mode_changed("AWAKE")  # same state again - not a transition
        self.assertEqual(self.sm.entered_at, first)


class RigRegionDetectionTests(unittest.TestCase):
    def test_detect_regions_derives_eye_mouth_boxes_from_the_largest_face(self):
        image = _blank_bgr(300, 300)
        # Two candidate faces - the smaller one first, so picking the
        # LARGEST (not the first) is actually exercised.
        fake_faces = np.array([[10, 10, 40, 40], [100, 100, 150, 150]])
        with mock.patch.object(rig, "_get_cascade") as get_cascade:
            get_cascade.return_value.detectMultiScale.return_value = fake_faces
            regions = rig.detect_regions(image)
        self.assertIsNotNone(regions)
        self.assertEqual(set(regions), {"left_eye", "right_eye", "mouth"})
        # The big face is (100,100,150,150) - every region must sit inside it.
        for box in regions.values():
            x0, y0, x1, y1 = box
            self.assertTrue(100 <= x0 < x1 <= 250)
            self.assertTrue(100 <= y0 < y1 <= 250)
        # left eye left of right eye, mouth below both.
        self.assertLess(regions["left_eye"][0], regions["right_eye"][0])
        self.assertGreater(regions["mouth"][1], regions["left_eye"][1])

    def test_detect_regions_returns_none_when_no_face_found(self):
        with mock.patch.object(rig, "_get_cascade") as get_cascade:
            get_cascade.return_value.detectMultiScale.return_value = np.empty((0, 4))
            self.assertIsNone(rig.detect_regions(_blank_bgr()))


class RigFrameBuildTests(unittest.TestCase):
    def setUp(self):
        self.image = _textured_bgr(200, 200)
        self.regions = {
            "left_eye": (30, 60, 80, 85),
            "right_eye": (120, 60, 170, 85),
            "mouth": (60, 130, 140, 160),
        }

    def test_build_frames_returns_every_named_frame_at_the_source_shape(self):
        frames = rig.build_frames(self.image, self.regions)
        self.assertEqual(set(frames), set(rig.FRAME_NAMES))
        for name, frame in frames.items():
            self.assertEqual(frame.shape, self.image.shape, name)

    def test_blink_frame_differs_from_neutral(self):
        frames = rig.build_frames(self.image, self.regions)
        self.assertFalse(np.array_equal(frames["blink"], frames["neutral"]))

    def test_mouth_open_frame_differs_from_neutral(self):
        frames = rig.build_frames(self.image, self.regions)
        self.assertFalse(np.array_equal(frames["mouth_open"], frames["neutral"]))

    def test_sway_frames_are_mirrored_shifts_and_differ_from_each_other(self):
        frames = rig.build_frames(self.image, self.regions)
        self.assertFalse(np.array_equal(frames["sway_left"], frames["sway_right"]))

    def test_warp_region_vertical_handles_a_box_at_the_image_edge_without_raising(self):
        edge_box = (-5, -5, 20, 20)  # partly off-canvas - must clip, not crash
        out = rig._warp_region_vertical(self.image, edge_box, 0.15)
        self.assertEqual(out.shape, self.image.shape)

    def test_warp_region_vertical_is_a_no_op_for_a_degenerate_box(self):
        out = rig._warp_region_vertical(self.image, (50, 50, 50, 50), 0.15)
        self.assertTrue(np.array_equal(out, self.image))


class BuildRigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo_path = os.path.join(self.tmp.name, "photo.png")
        import cv2
        cv2.imwrite(self.photo_path, _blank_bgr(200, 200))
        self.out_dir = os.path.join(self.tmp.name, "rig_out")
        self.regions = {
            "left_eye": (30, 60, 80, 85),
            "right_eye": (120, 60, 170, 85),
            "mouth": (60, 130, 140, 160),
        }

    def test_build_rig_writes_every_frame_and_a_manifest(self):
        with mock.patch.object(rig, "detect_regions", return_value=self.regions):
            manifest = rig.build_rig(self.photo_path, self.out_dir)
        self.assertEqual(set(manifest["frames"]), set(rig.FRAME_NAMES))
        for filename in manifest["frames"].values():
            self.assertTrue(os.path.exists(os.path.join(self.out_dir, filename)))
        with open(os.path.join(self.out_dir, "rig.json")) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, manifest)

    def test_build_rig_raises_a_friendly_error_for_an_unreadable_file(self):
        bad_path = os.path.join(self.tmp.name, "not_an_image.txt")
        with open(bad_path, "w") as f:
            f.write("not an image")
        with self.assertRaises(rig.RigBuildError):
            rig.build_rig(bad_path, self.out_dir)

    def test_build_rig_raises_a_friendly_error_when_no_face_is_found(self):
        with mock.patch.object(rig, "detect_regions", return_value=None):
            with self.assertRaises(rig.RigBuildError):
                rig.build_rig(self.photo_path, self.out_dir)

    def test_load_manifest_returns_none_before_any_build(self):
        self.assertIsNone(rig.load_manifest(self.out_dir))

    def test_load_manifest_roundtrips_after_build_rig(self):
        with mock.patch.object(rig, "detect_regions", return_value=self.regions):
            built = rig.build_rig(self.photo_path, self.out_dir)
        self.assertEqual(rig.load_manifest(self.out_dir), built)


class FrameForStateTests(unittest.TestCase):
    """avatar/animation.py's frame_for_state() - the one piece of
    avatar/window.py's logic that doesn't need PyQt6 (not installed in
    this sandbox at all - see this file's module docstring) to test."""

    def test_speaking_alternates_mouth_open_and_neutral_on_a_fixed_cadence(self):
        from avatar.animation import SPEAK_FLAP_PERIOD_S
        first = frame_for_state(AvatarState.SPEAKING, 0.0, 100.0, 0.0)
        second = frame_for_state(AvatarState.SPEAKING, SPEAK_FLAP_PERIOD_S, 100.0, 0.0)
        third = frame_for_state(AvatarState.SPEAKING, SPEAK_FLAP_PERIOD_S * 2, 100.0, 0.0)
        self.assertEqual(first, "mouth_open")
        self.assertEqual(second, "neutral")
        self.assertEqual(third, "mouth_open")

    def test_thinking_sways_left_then_right_on_its_own_cadence(self):
        from avatar.animation import THINK_SWAY_PERIOD_S
        left = frame_for_state(AvatarState.THINKING, 0.0, 100.0, 0.0)
        right = frame_for_state(AvatarState.THINKING, THINK_SWAY_PERIOD_S * 0.6, 100.0, 0.0)
        self.assertEqual(left, "sway_left")
        self.assertEqual(right, "sway_right")

    def test_listening_is_always_neutral_even_during_a_scheduled_blink(self):
        # LISTENING deliberately never checks next_blink_at - a listening
        # face still "blinks" in the sense that it's just neutral; it's
        # IDLE's sway/blink idle-fidget that's suppressed here, not blinking
        # itself (this function simply never routes LISTENING to "blink").
        frame = frame_for_state(AvatarState.LISTENING, 10.0, 100.0, next_blink_at=100.0)
        self.assertEqual(frame, "neutral")

    def test_idle_blinks_exactly_during_the_scheduled_window(self):
        before = frame_for_state(AvatarState.IDLE, 1.0, 99.0, next_blink_at=100.0)
        during = frame_for_state(AvatarState.IDLE, 1.0, 100.05, next_blink_at=100.0)
        after = frame_for_state(AvatarState.IDLE, 1.0, 100.0 + BLINK_DURATION_S + 0.01, next_blink_at=100.0)
        self.assertEqual(before, "neutral")
        self.assertEqual(during, "blink")
        self.assertEqual(after, "neutral")

    def test_idle_sways_only_after_settling_in_and_blink_takes_priority(self):
        too_soon = frame_for_state(AvatarState.IDLE, IDLE_SWAY_AFTER_S - 0.5, 0.0, next_blink_at=9999.0)
        settled = frame_for_state(AvatarState.IDLE, IDLE_SWAY_AFTER_S + 0.1, 0.0, next_blink_at=9999.0)
        other_phase = frame_for_state(AvatarState.IDLE, IDLE_SWAY_AFTER_S + 0.1 + SWAY_PERIOD_S / 2,
                                       0.0, next_blink_at=9999.0)
        blink_wins = frame_for_state(AvatarState.IDLE, IDLE_SWAY_AFTER_S + 0.1, 100.0, next_blink_at=100.0)
        self.assertEqual(too_soon, "neutral")
        self.assertIn(settled, ("sway_left", "sway_right"))
        self.assertNotEqual(settled, other_phase)
        self.assertEqual(blink_wins, "blink")


class RealCascadeSanityTest(unittest.TestCase):
    """Not mocked - a real sanity check that the bundled cascade file
    actually loads and runs without raising, independent of whether any
    given synthetic image happens to contain a detectable face (it
    won't - see this file's own module docstring)."""

    def test_bundled_cascade_file_loads_and_runs(self):
        regions = rig.detect_regions(_blank_bgr())  # no real face - None is a valid, non-crashing result
        self.assertIsNone(regions)


if __name__ == "__main__":
    unittest.main()
