"""Phase 8 / Tier 2 item 6 - centralized, rotating logging
(settings/logging_config.py), replacing the old inline
logging.basicConfig() call that used to end settings/config.py.

Each test snapshots and restores the root logger's handlers/level, since
setup() mutates genuinely global state (the root logger) that the rest of
the test suite - and the real app - also relies on.
"""
import glob
import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler

import _phase4_support as S  # noqa: F401
from settings import logging_config as lc


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave_logcfg_test_")
        root = logging.getLogger()
        self._saved_handlers = list(root.handlers)
        self._saved_level = root.level
        self._saved_configured = lc._configured
        for h in list(root.handlers):
            root.removeHandler(h)

        def restore():
            root = logging.getLogger()
            for h in list(root.handlers):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            for h in self._saved_handlers:
                root.addHandler(h)
            root.setLevel(self._saved_level)
            lc._configured = self._saved_configured
        self.addCleanup(restore)

    def log_path(self):
        return os.path.join(self.tmp, lc.LOG_FILENAME)

    # ---------- build_handlers() ----------
    def test_default_handlers_are_a_rotating_file_handler_plus_console(self):
        handlers = lc.build_handlers(self.tmp, env={})
        self.assertEqual(len(handlers), 2)
        file_handler = next(h for h in handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(file_handler.maxBytes, lc.DEFAULT_MAX_BYTES)
        self.assertEqual(file_handler.backupCount, lc.DEFAULT_BACKUP_COUNT)
        self.assertEqual(os.path.abspath(file_handler.baseFilename), os.path.abspath(self.log_path()))
        self.assertTrue(any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
                            for h in handlers))

    def test_env_overrides_max_bytes_and_backup_count(self):
        handlers = lc.build_handlers(self.tmp, env={"LOG_MAX_BYTES": "2048", "LOG_BACKUP_COUNT": "3"})
        fh = next(h for h in handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(fh.maxBytes, 2048)
        self.assertEqual(fh.backupCount, 3)

    def test_log_console_enabled_false_drops_the_console_handler(self):
        handlers = lc.build_handlers(self.tmp, env={"LOG_CONSOLE_ENABLED": "false"})
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], RotatingFileHandler)

    def test_garbage_numeric_env_values_fall_back_to_defaults_without_raising(self):
        handlers = lc.build_handlers(self.tmp, env={"LOG_MAX_BYTES": "not-a-number", "LOG_BACKUP_COUNT": "also-bad"})
        fh = next(h for h in handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(fh.maxBytes, lc.DEFAULT_MAX_BYTES)
        self.assertEqual(fh.backupCount, lc.DEFAULT_BACKUP_COUNT)

    # ---------- setup() ----------
    def test_setup_attaches_handlers_at_the_configured_level(self):
        lc.setup(self.tmp, env={"LOG_LEVEL": "WARNING"}, force=True)
        root = logging.getLogger()
        self.assertEqual(root.level, logging.WARNING)
        self.assertTrue(any(isinstance(h, RotatingFileHandler) for h in root.handlers))

    def test_unknown_log_level_falls_back_to_info(self):
        lc.setup(self.tmp, env={"LOG_LEVEL": "NOT_A_REAL_LEVEL"}, force=True)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_second_call_without_force_is_a_no_op(self):
        lc.setup(self.tmp, env={}, force=True)
        root = logging.getLogger()
        handlers_after_first = list(root.handlers)
        lc.setup(os.path.join(self.tmp, "ignored-should-not-apply"), env={"LOG_LEVEL": "ERROR"})
        self.assertEqual(root.handlers, handlers_after_first)
        self.assertNotEqual(root.level, logging.ERROR)

    def test_force_true_tears_down_and_rebuilds_against_a_new_directory(self):
        lc.setup(self.tmp, env={}, force=True)
        other_dir = tempfile.mkdtemp(prefix="wave_logcfg_test2_")
        lc.setup(other_dir, env={}, force=True)
        root = logging.getLogger()
        fh = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(os.path.abspath(fh.baseFilename), os.path.abspath(os.path.join(other_dir, lc.LOG_FILENAME)))

    def test_setup_actually_writes_to_the_log_file(self):
        lc.setup(self.tmp, env={}, force=True)
        logging.getLogger("SmokeTest").info("hello from the smoke test")
        with open(self.log_path(), encoding="utf-8") as f:
            self.assertIn("hello from the smoke test", f.read())

    # ---------- the actual point of Tier 2 item 6: rotation ----------
    def test_log_file_actually_rotates_once_past_max_bytes(self):
        lc.setup(self.tmp, env={"LOG_MAX_BYTES": "200", "LOG_BACKUP_COUNT": "2"}, force=True)
        log = logging.getLogger("RotationSmokeTest")
        for i in range(200):
            log.info(f"padding this log line out to make it roll over soon enough #{i}")
        rolled = glob.glob(self.log_path() + ".*")
        self.assertTrue(rolled, "expected at least one rotated backup file (wave_system.log.1, ...)")
        self.assertLessEqual(len(rolled), 2, "LOG_BACKUP_COUNT=2 should cap the number of backups kept")
        self.assertTrue(os.path.exists(self.log_path()), "the live log file itself should still exist after rotating")


if __name__ == "__main__":
    unittest.main()
