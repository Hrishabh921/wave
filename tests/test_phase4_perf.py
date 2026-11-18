"""Phase 4 / idea 1 - the UI-smoothness fixes that can be verified without a display."""
import time
import unittest

import _phase4_support as S  # noqa: F401
from monitoring.sys_monitor import SystemMonitor


class SystemMonitorTests(unittest.TestCase):
    def test_get_stats_never_sleeps_on_the_gui_thread(self):
        SystemMonitor.get_stats()
        t0 = time.perf_counter()
        for _ in range(20):
            stats = SystemMonitor.get_stats()
        per_call_ms = (time.perf_counter() - t0) / 20 * 1000
        self.assertLess(per_call_ms, 30, f"get_stats took {per_call_ms:.1f} ms/call (it used to block ~100 ms)")
        self.assertEqual(set(stats), {"cpu_percent", "ram_percent", "ram_used_gb", "ram_total_gb",
                                      "disk_percent", "battery_percent", "is_plugged"})
        self.assertTrue(0 <= stats["cpu_percent"] <= 100)

    def test_cpu_reading_is_real_not_stuck_at_zero(self):
        import threading
        stop = threading.Event()
        burner = threading.Thread(target=lambda: [None for _ in iter(lambda: stop.is_set(), True)], daemon=True)
        burner.start()
        try:
            time.sleep(0.4)
            readings = [SystemMonitor.get_stats()["cpu_percent"] for _ in range(3) if time.sleep(0.3) is None]
        finally:
            stop.set()
        self.assertTrue(any(r > 0 for r in readings), readings)


if __name__ == "__main__":
    unittest.main()
