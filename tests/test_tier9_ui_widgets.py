"""Tier 9 - logic tests for five small, previously-untouched ui/ modules,
WITHOUT a display or PyQt6: WorkerPool (ui/worker_pool.py), CommandPalette
(ui/command_palette.py), DashboardTile (ui/dashboard_tile.py), ToastManager
(ui/toast.py), and CircularGauge/Sparkline/SlidingStack
(ui/widgets_basic.py).

All five were split out of the original ui/main_window.py (see
ui/common.py's docstring) and none of them - unlike AudioVisualizer,
AlbumArtDisc and AuroraBackdrop, which test_phase4_ui_logic.py already
touches for their hidden/minimized tick-skipping guard - have ever been
exercised by a test.

Extends test_tier5_ui_logic.py's QListWidget/QLineEdit stand-ins (needed
for CommandPalette's filtering) with one more: a QTimer whose singleShot()
*records* (delay, callback) pairs instead of silently discarding them the
way the base stand-in's generic absorber does. ToastManager's auto-dismiss
and SlidingStack's wheel-scroll cooldown both schedule real follow-up work
through QTimer.singleShot() - without a way to retrieve what was scheduled,
that logic would be untestable at all, not just untested.
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_phase4_ui_logic as P4
import test_tier5_ui_logic as T5


class _CapturingQTimer(P4._Any):
    """QTimer stand-in whose singleShot() remembers what was scheduled
    instead of swallowing it, so a test can invoke the callback itself at a
    point of its own choosing - explicit and deterministic, unlike an
    auto-firing timer that would hide ordering bugs."""
    scheduled = []

    @staticmethod
    def singleShot(ms, fn):
        _CapturingQTimer.scheduled.append((ms, fn))


def install_stub_for_tier9():
    """T5's list-widget stand-in, plus two more layered into PyQt6.QtCore's
    OVERRIDES the same way T5 layered QListWidget/QLineEdit into
    PyQt6.QtWidgets's: the capturing QTimer above, and a QThread whose
    `finished` is a real connectable signal rather than a generically
    absorbed (and therefore un-capturable) attribute - WorkerPool.submit()
    wires its cleanup to *that* signal (QThread's own native one, fired by
    real Qt when the thread's run() returns), not to Worker.signals.finished
    (its own separate, manually-emitted one for UI consumers)."""
    widgets = T5.install_stub_with_list_widgets()
    core = sys.modules["PyQt6.QtCore"]
    core.OVERRIDES["QTimer"] = _CapturingQTimer
    core.OVERRIDES["QThread"] = type("QThread", (P4._Any,), {"finished": P4._Signal()})
    return widgets


def import_with_fakes(module_name):
    return T5.import_with_fakes(module_name)


THEME = types.SimpleNamespace(
    accent_1="#00D4AA", accent_2="#FF6B9D", accent_3="#FFD60A",
    text_main="#FFFFFF", text_muted="#B0B0B0", text_faint="#808080",
    bg_surface="#1E1E1E", border="#333333", success="#00E676", warning="#FFD60A", danger="#FF3D5A",
)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class WorkerPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.wp = import_with_fakes("ui.worker_pool")

    def test_run_on_success_emits_result_then_finished(self):
        worker = self.wp.Worker(lambda x: x * 2, 21)
        seen = []
        worker.signals.result.connect(lambda v: seen.append(("result", v)))
        worker.signals.finished.connect(lambda: seen.append(("finished",)))
        worker.signals.error.connect(lambda e: seen.append(("error", e)))
        worker.run()
        self.assertEqual(seen, [("result", 42), ("finished",)])

    def test_run_on_exception_emits_error_then_still_emits_finished(self):
        def boom():
            raise ValueError("nope")
        worker = self.wp.Worker(boom)
        seen = []
        worker.signals.result.connect(lambda v: seen.append(("result", v)))
        worker.signals.error.connect(lambda e: seen.append(("error", e)))
        worker.signals.finished.connect(lambda: seen.append(("finished",)))
        worker.run()
        self.assertEqual(seen, [("error", "nope"), ("finished",)])

    def test_submit_starts_and_tracks_the_worker(self):
        pool = self.wp.WorkerPool()
        with mock.patch.object(self.wp.Worker, "start") as mock_start:
            worker = pool.submit(lambda: "done")
        mock_start.assert_called_once()
        self.assertIn(worker, pool._workers)

    def test_the_threads_own_finished_signal_triggers_cleanup(self):
        """WorkerPool.submit() wires worker.finished (QThread's native,
        Qt-fired-when-run()-returns signal) straight to self._cleanup -
        simulated here by emitting it directly, the same way every other
        signal in this test suite stands in for a real Qt-driven event."""
        pool = self.wp.WorkerPool()
        with mock.patch.object(self.wp.Worker, "start"):
            worker = pool.submit(lambda: "done")
        self.assertIn(worker, pool._workers)
        worker.finished.emit()
        self.assertNotIn(worker, pool._workers)

    def test_cleanup_removes_a_tracked_worker_and_ignores_an_untracked_one(self):
        pool = self.wp.WorkerPool()
        w1, w2 = object(), object()
        pool._workers = [w1, w2]
        pool._cleanup(w1)
        self.assertEqual(pool._workers, [w2])
        pool._cleanup(object())   # not in the list - must not raise
        self.assertEqual(pool._workers, [w2])

    def test_shutdown_terminates_and_waits_on_every_worker_then_clears_the_list(self):
        pool = self.wp.WorkerPool()
        w1, w2 = mock.Mock(), mock.Mock()
        pool._workers = [w1, w2]
        pool.shutdown()
        for w in (w1, w2):
            w.terminate.assert_called_once()
            w.wait.assert_called_once_with(1000)
        self.assertEqual(pool._workers, [])


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class CommandPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.cp = import_with_fakes("ui.command_palette")

    def setUp(self):
        self.calls = []
        actions = {
            "Open Chrome": lambda: self.calls.append("chrome"),
            "Open Calculator": lambda: self.calls.append("calc"),
            "Lock PC": lambda: self.calls.append("lock"),
        }
        parent = types.SimpleNamespace(geometry=lambda: types.SimpleNamespace(x=lambda: 0, y=lambda: 0, width=lambda: 800, height=lambda: 600))
        self.palette = self.cp.CommandPalette(parent, THEME, actions)

    def test_initial_population_lists_everything_and_selects_the_first_row(self):
        self.assertEqual(self.palette.list.count(), 3)
        self.assertEqual(self.palette.list.currentRow(), 0)

    def test_filtering_is_case_insensitive_and_substring_based(self):
        self.palette.input.setText("OPEN")
        self.assertEqual(self.palette.list.count(), 2)
        names = {self.palette.list.item(i).text() for i in range(self.palette.list.count())}
        self.assertEqual(names, {"Open Chrome", "Open Calculator"})

    def test_filtering_to_nothing_leaves_the_list_empty_without_crashing(self):
        self.palette.input.setText("xyz-not-a-command")
        self.assertEqual(self.palette.list.count(), 0)

    def test_activate_runs_the_stored_action_and_accepts_the_dialog(self):
        self.palette.accept = mock.Mock()
        item = self.palette.list.item(0)
        self.palette._activate(item)
        self.assertEqual(self.calls, ["chrome"])
        self.palette.accept.assert_called_once()

    def test_enter_key_activates_whatever_is_currently_selected(self):
        self.palette.accept = mock.Mock()
        self.palette.list.setCurrentRow(2)
        Qt = self.cp.Qt
        ev = types.SimpleNamespace(type=lambda: self.cp.QEvent.Type.KeyPress, key=lambda: Qt.Key.Key_Return)
        handled = self.palette.eventFilter(self.palette.input, ev)
        self.assertTrue(handled)
        self.assertEqual(self.calls, ["lock"])

    def test_down_arrow_moves_down_and_clamps_at_the_last_row(self):
        Qt = self.cp.Qt
        down = types.SimpleNamespace(type=lambda: self.cp.QEvent.Type.KeyPress, key=lambda: Qt.Key.Key_Down)
        self.palette.eventFilter(self.palette.input, down)
        self.assertEqual(self.palette.list.currentRow(), 1)
        self.palette.eventFilter(self.palette.input, down)
        self.palette.eventFilter(self.palette.input, down)   # already at the last row (2) - must clamp
        self.assertEqual(self.palette.list.currentRow(), 2)

    def test_up_arrow_moves_up_and_clamps_at_the_first_row(self):
        Qt = self.cp.Qt
        up = types.SimpleNamespace(type=lambda: self.cp.QEvent.Type.KeyPress, key=lambda: Qt.Key.Key_Up)
        self.palette.eventFilter(self.palette.input, up)   # already at row 0 - must clamp, not go negative
        self.assertEqual(self.palette.list.currentRow(), 0)

    def test_escape_rejects_the_dialog(self):
        self.palette.reject = mock.Mock()
        Qt = self.cp.Qt
        ev = types.SimpleNamespace(type=lambda: self.cp.QEvent.Type.KeyPress, key=lambda: Qt.Key.Key_Escape)
        handled = self.palette.eventFilter(self.palette.input, ev)
        self.assertTrue(handled)
        self.palette.reject.assert_called_once()

    def test_an_unhandled_key_falls_through_to_the_base_event_filter(self):
        QDialogCls = type(self.palette).__mro__[1]
        Qt = self.cp.Qt
        ev = types.SimpleNamespace(type=lambda: self.cp.QEvent.Type.KeyPress, key=lambda: Qt.Key.Key_A)
        with mock.patch.object(QDialogCls, "eventFilter", create=True, return_value="base-result") as base_ef:
            result = self.palette.eventFilter(self.palette.input, ev)
        base_ef.assert_called_once_with(self.palette.input, ev)
        self.assertEqual(result, "base-result")


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class DashboardTileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.dt = import_with_fakes("ui.dashboard_tile")

    def setUp(self):
        self.tile = self.dt.DashboardTile("🏠", "Home", THEME)

    def test_set_preview_with_a_null_pixmap_is_a_no_op(self):
        null_pixmap = types.SimpleNamespace(isNull=lambda: True)
        self.tile.preview_lbl = mock.Mock()
        self.tile.set_preview(null_pixmap)
        self.tile.preview_lbl.setPixmap.assert_not_called()

    def test_set_preview_with_none_is_a_no_op(self):
        self.tile.preview_lbl = mock.Mock()
        self.tile.set_preview(None)
        self.tile.preview_lbl.setPixmap.assert_not_called()

    def test_set_preview_with_a_valid_pixmap_scales_and_sets_it(self):
        scaled = object()
        real_pixmap = types.SimpleNamespace(isNull=lambda: False, scaled=mock.Mock(return_value=scaled))
        self.tile.preview_lbl = mock.Mock()
        self.tile.set_preview(real_pixmap)
        self.tile.preview_lbl.setPixmap.assert_called_once_with(scaled)

    def test_left_click_emits_the_clicked_signal(self):
        Qt = self.dt.Qt
        seen = []
        self.tile.clicked.connect(lambda: seen.append(True))
        ev = types.SimpleNamespace(button=lambda: Qt.MouseButton.LeftButton)
        base = type(self.tile).__mro__[1]
        with mock.patch.object(base, "mousePressEvent", create=True):
            self.tile.mousePressEvent(ev)
        self.assertEqual(seen, [True])

    def test_right_click_does_not_emit_the_clicked_signal(self):
        Qt = self.dt.Qt
        seen = []
        self.tile.clicked.connect(lambda: seen.append(True))
        ev = types.SimpleNamespace(button=lambda: Qt.MouseButton.RightButton)
        base = type(self.tile).__mro__[1]
        with mock.patch.object(base, "mousePressEvent", create=True):
            self.tile.mousePressEvent(ev)
        self.assertEqual(seen, [])


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ToastManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.toast_mod = import_with_fakes("ui.toast")
        # QFrame's stand-in has no width()/height() (never needed one before -
        # show() does real position arithmetic with them, so give them fixed,
        # predictable values here rather than teaching the shared QWidget
        # stand-in about a size it otherwise never tracks.
        cls.toast_mod.QFrame.width = lambda self: 260
        cls.toast_mod.QFrame.height = lambda self: 56
        cls.toast_mod.QFrame.move = lambda self, x, y: setattr(self, "_last_move", (x, y))

    def setUp(self):
        _CapturingQTimer.scheduled = []
        parent = types.SimpleNamespace(
            geometry=lambda: types.SimpleNamespace(x=lambda: 0, y=lambda: 0, width=lambda: 1000, height=lambda: 800))
        self.mgr = self.toast_mod.ToastManager(parent, THEME)

    def test_show_appends_a_tracked_toast(self):
        self.mgr.show("Saved.", icon="success")
        self.assertEqual(len(self.mgr._toasts), 1)

    def test_show_schedules_auto_dismiss_for_the_given_duration(self):
        self.mgr.show("Hi", duration=1500)
        self.assertEqual(len(_CapturingQTimer.scheduled), 1)
        ms, _fn = _CapturingQTimer.scheduled[0]
        self.assertEqual(ms, 1500)

    def test_auto_dismiss_removes_the_toast_from_the_tracked_list(self):
        self.mgr.show("bye")
        self.assertEqual(len(self.mgr._toasts), 1)
        _ms, remove_fn = _CapturingQTimer.scheduled[-1]
        remove_fn()   # simulate the timer firing
        self.assertEqual(self.mgr._toasts, [])

    def test_second_toast_is_positioned_below_the_first(self):
        self.mgr.show("first")
        self.mgr.show("second")
        _x0, y0 = self.mgr._toasts[0]._last_move
        _x1, y1 = self.mgr._toasts[1]._last_move
        self.assertLess(y0, y1)
        self.assertEqual(y1 - y0, self.mgr._toasts[0].height() + 8)

    def test_reposition_stacks_toasts_top_to_bottom_by_index(self):
        t0 = types.SimpleNamespace(height=lambda: 40, width=lambda: 200, move=mock.Mock())
        t1 = types.SimpleNamespace(height=lambda: 60, width=lambda: 200, move=mock.Mock())
        self.mgr._toasts = [t0, t1]
        self.mgr._reposition()
        y0 = t0.move.call_args.args[1]
        y1 = t1.move.call_args.args[1]
        self.assertLess(y0, y1)   # the first toast stays above the second


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class CircularGaugeAndSparklineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.wb = import_with_fakes("ui.widgets_basic")

    # ---------- CircularGauge ----------
    def test_gauge_clamps_a_value_above_100(self):
        g = self.wb.CircularGauge()
        g.update = mock.Mock()
        g.setValue(150)
        self.assertEqual(g._value, 100.0)

    def test_gauge_clamps_a_value_below_zero(self):
        g = self.wb.CircularGauge()
        g.update = mock.Mock()
        g.setValue(-10)
        self.assertEqual(g._value, 0.0)

    def test_gauge_coerces_a_non_numeric_value_to_zero(self):
        g = self.wb.CircularGauge()
        g.update = mock.Mock()
        g.setValue("not a number")
        self.assertEqual(g._value, 0.0)

    def test_gauge_accepts_a_numeric_string(self):
        g = self.wb.CircularGauge()
        g.update = mock.Mock()
        g.setValue("42")
        self.assertEqual(g._value, 42.0)

    def test_gauge_skips_the_repaint_when_the_value_is_unchanged(self):
        g = self.wb.CircularGauge()
        g.setValue(55)
        g.update = mock.Mock()
        g.setValue(55)   # same value again
        g.update.assert_not_called()

    def test_gauge_repaints_when_the_value_actually_changes(self):
        g = self.wb.CircularGauge()
        g.setValue(10)
        g.update = mock.Mock()
        g.setValue(20)
        g.update.assert_called_once()

    # ---------- Sparkline ----------
    def test_sparkline_clamps_high_and_low_values(self):
        s = self.wb.Sparkline(max_points=5)
        s.update = mock.Mock()
        s.add_value(999)
        s.add_value(-999)
        self.assertEqual(list(s.data)[-2:], [100.0, 0.0])

    def test_sparkline_drops_the_oldest_point_once_past_max_points(self):
        s = self.wb.Sparkline(max_points=3)
        s.update = mock.Mock()
        for v in (10, 20, 30, 40):
            s.add_value(v)
        self.assertEqual(list(s.data), [20.0, 30.0, 40.0])

    def test_sparkline_always_repaints_on_add(self):
        s = self.wb.Sparkline(max_points=5)
        s.update = mock.Mock()
        s.add_value(1)
        s.add_value(2)
        self.assertEqual(s.update.call_count, 2)

    # ---------- SlidingStack: wheel accumulator ----------
    def _stack(self, n_pages=3):
        st = self.wb.SlidingStack()
        st.pages = [mock.Mock() for _ in range(n_pages)]
        st.current_index = 0
        return st

    def setUp(self):
        _CapturingQTimer.scheduled = []

    def test_small_wheel_deltas_accumulate_without_triggering_a_slide(self):
        st = self._stack()
        st.slide_to = mock.Mock()
        ev = mock.Mock(angleDelta=lambda: types.SimpleNamespace(x=lambda: 30))
        st._handle_wheel_delta(30, ev)
        st._handle_wheel_delta(30, ev)
        st.slide_to.assert_not_called()
        self.assertEqual(st._wheel_accum_x, 60)

    def test_crossing_the_positive_threshold_slides_to_the_next_page(self):
        st = self._stack()
        st.slide_to = mock.Mock()
        ev = mock.Mock()
        st._handle_wheel_delta(100, ev)
        st.slide_to.assert_called_once_with(1)
        self.assertTrue(st._wheel_cooldown)
        self.assertEqual(st._wheel_accum_x, 0.0)

    def test_crossing_the_negative_threshold_slides_to_the_previous_page(self):
        st = self._stack()
        st.current_index = 2
        st.slide_to = mock.Mock()
        ev = mock.Mock()
        st._handle_wheel_delta(-100, ev)
        st.slide_to.assert_called_once_with(1)

    def test_wheel_delta_is_ignored_while_animating(self):
        st = self._stack()
        st.animating = True
        st.slide_to = mock.Mock()
        ev = mock.Mock()
        st._handle_wheel_delta(500, ev)
        st.slide_to.assert_not_called()
        self.assertEqual(st._wheel_accum_x, 0.0)   # never even accumulated

    def test_wheel_delta_is_ignored_during_the_post_trigger_cooldown(self):
        st = self._stack()
        st._wheel_cooldown = True
        st.slide_to = mock.Mock()
        ev = mock.Mock()
        st._handle_wheel_delta(500, ev)
        st.slide_to.assert_not_called()

    def test_wheel_trigger_at_the_last_page_cannot_advance_further(self):
        st = self._stack(n_pages=2)
        st.current_index = 1   # already last
        st.slide_to = mock.Mock()
        st._wheel_trigger(go_next=True)
        st.slide_to.assert_not_called()
        self.assertTrue(st._wheel_cooldown)   # cooldown still arms regardless

    def test_wheel_trigger_schedules_a_cooldown_reset_that_clears_state(self):
        st = self._stack()
        st.slide_to = mock.Mock()
        st._wheel_accum_x = 42.0
        st._wheel_trigger(go_next=True)
        self.assertEqual(len(_CapturingQTimer.scheduled), 1)
        ms, reset_fn = _CapturingQTimer.scheduled[0]
        self.assertEqual(ms, 450)
        st._wheel_cooldown = True   # simulate time passing, then the timer firing
        reset_fn()
        self.assertFalse(st._wheel_cooldown)
        self.assertEqual(st._wheel_accum_x, 0.0)

    # ---------- SlidingStack: slide_to guards ----------
    def test_slide_to_the_current_page_is_a_no_op(self):
        st = self._stack()
        st.slide_to(0)
        self.assertFalse(st.animating)

    def test_slide_to_an_out_of_range_index_is_a_no_op(self):
        st = self._stack(n_pages=2)
        st.slide_to(5)
        self.assertFalse(st.animating)

    def test_slide_to_while_already_animating_is_a_no_op(self):
        st = self._stack()
        st.animating = True
        st.slide_to(1)   # must not touch current_index or raise
        self.assertEqual(st.current_index, 0)

    def test_slide_to_a_different_valid_page_actually_proceeds(self):
        st = self._stack()
        st.width = lambda: 400
        st.height = lambda: 300
        st.slide_to(1)
        self.assertTrue(st.animating)   # set synchronously, before any animation runs

    # ---------- SlidingStack: drag-release threshold decision ----------
    def test_drag_release_past_threshold_advances_to_the_next_page(self):
        st = self._stack()
        st.width = lambda: 400
        st._dragging, st._drag_dx = True, -100   # past the 20% (80px) threshold, leftward
        st._drag_finish, st._snap_back = mock.Mock(), mock.Mock()
        st.mouseReleaseEvent(mock.Mock())
        st._drag_finish.assert_called_once_with(1, 1)
        st._snap_back.assert_not_called()

    def test_drag_release_past_threshold_goes_back_to_the_previous_page(self):
        st = self._stack()
        st.current_index = 1
        st.width = lambda: 400
        st._dragging, st._drag_dx = True, 100
        st._drag_finish, st._snap_back = mock.Mock(), mock.Mock()
        st.mouseReleaseEvent(mock.Mock())
        st._drag_finish.assert_called_once_with(0, -1)

    def test_drag_release_below_threshold_snaps_back(self):
        st = self._stack()
        st.width = lambda: 400
        st._dragging, st._drag_dx = True, 10   # well under the 80px threshold
        st._drag_finish, st._snap_back = mock.Mock(), mock.Mock()
        st.mouseReleaseEvent(mock.Mock())
        st._snap_back.assert_called_once()
        st._drag_finish.assert_not_called()

    def test_drag_release_past_threshold_at_the_last_page_snaps_back_instead(self):
        st = self._stack(n_pages=2)
        st.current_index = 1   # already last - can't advance further even past threshold
        st.width = lambda: 400
        st._dragging, st._drag_dx = True, -200
        st._drag_finish, st._snap_back = mock.Mock(), mock.Mock()
        st.mouseReleaseEvent(mock.Mock())
        st._snap_back.assert_called_once()
        st._drag_finish.assert_not_called()

    def test_mouse_release_while_not_dragging_is_a_no_op(self):
        st = self._stack()
        st._dragging = False
        st._drag_finish, st._snap_back = mock.Mock(), mock.Mock()
        st.mouseReleaseEvent(mock.Mock())
        st._drag_finish.assert_not_called()
        st._snap_back.assert_not_called()

    # ---------- SlidingStack: add_page ----------
    def test_add_page_shows_only_the_first_page_added(self):
        st = self.wb.SlidingStack()
        st.width = lambda: 400
        st.height = lambda: 300
        p0, p1 = mock.Mock(), mock.Mock()
        st.add_page(p0)
        st.add_page(p1)
        p0.show.assert_called_once()
        p1.hide.assert_called_once()
        p1.show.assert_not_called()


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class IconTests(unittest.TestCase):
    """ui/icons.py's Icon.set_btn_icon() carries a documented past bug (see
    its own comment: calling it on a plain QLabel used to crash with
    AttributeError, since QLabel has no setIcon) - exactly the kind of
    regression a quick test locks in once, for good."""

    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_for_tier9()
        cls.icons = import_with_fakes("ui.icons")

    def test_get_uses_qtawesome_when_available(self):
        fake_qta = types.SimpleNamespace(icon=mock.Mock(return_value="QTA_ICON"))
        with mock.patch.object(self.icons, "HAS_QTA", True), \
             mock.patch.object(self.icons, "qta", fake_qta, create=True):
            result = self.icons.Icon.get("house", color="#fff")
        self.assertEqual(result, "QTA_ICON")
        fake_qta.icon.assert_called_once_with("fa6s.house", color="#fff", options=[{"scale_factor": 1.2}])

    def test_get_falls_back_to_emoji_when_qtawesome_raises(self):
        fake_qta = types.SimpleNamespace(icon=mock.Mock(side_effect=RuntimeError("no such icon")))
        with mock.patch.object(self.icons, "HAS_QTA", True), \
             mock.patch.object(self.icons, "qta", fake_qta, create=True):
            result = self.icons.Icon.get("house")   # must not raise
        self.assertIsNotNone(result)

    def test_get_without_qtawesome_uses_the_known_emoji_mapping(self):
        seen = {}

        def fake_qlabel(text):
            seen["text"] = text
            return mock.Mock()

        with mock.patch.object(self.icons, "HAS_QTA", False), \
             mock.patch.object(self.icons, "QLabel", fake_qlabel):
            self.icons.Icon.get("trash")
        self.assertEqual(seen["text"], "🗑")

    def test_get_without_qtawesome_falls_back_to_a_blank_square_for_an_unknown_name(self):
        seen = {}

        def fake_qlabel(text):
            seen["text"] = text
            return mock.Mock()

        with mock.patch.object(self.icons, "HAS_QTA", False), \
             mock.patch.object(self.icons, "QLabel", fake_qlabel):
            self.icons.Icon.get("not-a-real-icon-name")
        self.assertEqual(seen["text"], "⬜")

    def test_set_btn_icon_on_a_real_button_uses_setIcon_and_setIconSize(self):
        btn = mock.Mock(spec=["setIcon", "setIconSize"])
        with mock.patch.object(self.icons.Icon, "get", return_value="SOME_ICON"):
            self.icons.Icon.set_btn_icon(btn, "house")
        btn.setIcon.assert_called_once_with("SOME_ICON")
        btn.setIconSize.assert_called_once()

    def test_set_btn_icon_on_a_plain_label_uses_setPixmap_not_setIcon(self):
        """The regression this guards against: a QLabel has setPixmap but
        not setIcon, and calling .setIcon() on it used to raise
        AttributeError before this dual-dispatch existed."""
        fake_icon = mock.Mock()
        fake_icon.pixmap.return_value = "PIXMAP"
        label = mock.Mock(spec=["setPixmap"])
        with mock.patch.object(self.icons.Icon, "get", return_value=fake_icon):
            self.icons.Icon.set_btn_icon(label, "house")
        label.setPixmap.assert_called_once_with("PIXMAP")

    def test_set_btn_icon_on_something_with_neither_method_does_not_crash(self):
        bare = mock.Mock(spec=[])
        with mock.patch.object(self.icons.Icon, "get", return_value=mock.Mock()):
            self.icons.Icon.set_btn_icon(bare, "house")   # must not raise


if __name__ == "__main__":
    unittest.main()
