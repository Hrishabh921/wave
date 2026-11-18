"""
tests/test_tier14_sysinfo.py — Tier 14: system info (get_uptime, get_local_ip,
get_gpu_status, get_cpu_temperature, get_top_processes, get_disk_usage,
get_battery_status, plus the private _windows_battery_wear() helper) and the
thin brain/handlers/system.py skill wiring around them (including
list_services's count/truncation formatting - the one piece of Tier 14 skill
behavior NOT already covered by test_tier14_power_session.py's per-backend
list_services() tests, since those only exercise the backends themselves, not
this handler's own message shaping on top).

SysMonitorTests below mocks only the psutil/socket/platform/subprocess calls
each SystemMonitor method makes, never the method under test itself - same
spirit as the backend-level tests in the other three Tier 14 files.
SkillDelegationTests is the thin layer on top: brain/handlers/system.py's
_skill_* functions do almost nothing but call through to SystemMonitor/
AppController/get_backend(), so those tests mock THAT next layer down instead.

Run: python -m unittest discover -s tests -p "test_tier14_sysinfo.py"
(pytest will also collect it.)
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

# brain/handlers/system.py -> automation.app_control imports pyautogui
# unconditionally at module level - same bootstrap every other Tier 14 test
# file (and test_handlers_structure.py, importing this exact package) needs
# in a sandbox without a real pyautogui/pywhatkit installed.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import psutil                                                     # noqa: E402

from monitoring.sys_monitor import SystemMonitor                  # noqa: E402
from automation.app_control import AppController                  # noqa: E402
from automation.backends.base import NotSupported                  # noqa: E402
from brain.handlers import system as system_handlers               # noqa: E402


# =============================================================================
# SystemMonitor - each method tested against mocked psutil/socket/platform/
# subprocess calls, never against this sandbox's own real hardware state.
# =============================================================================

class UptimeTests(unittest.TestCase):
    def _uptime(self, boot_time, now):
        with mock.patch("monitoring.sys_monitor.psutil.boot_time", return_value=boot_time), \
             mock.patch("monitoring.sys_monitor.time.time", return_value=now):
            return SystemMonitor.get_uptime()

    def test_plural_days_hours_minutes(self):
        # 2 days, 2 hours, 2 minutes = 172800 + 7200 + 120 seconds
        result = self._uptime(boot_time=0.0, now=180120.0)
        self.assertEqual(result, "This PC has been on for 2 days, 2 hours, 2 minutes.")

    def test_singular_wording_for_exactly_one_of_each(self):
        # 1 day, 1 hour, 1 minute = 86400 + 3600 + 60 seconds
        result = self._uptime(boot_time=0.0, now=90060.0)
        self.assertEqual(result, "This PC has been on for 1 day, 1 hour, 1 minute.")

    def test_zero_elapsed_still_shows_zero_minutes(self):
        result = self._uptime(boot_time=1000.0, now=1000.0)
        self.assertEqual(result, "This PC has been on for 0 minutes.")

    def test_minutes_only_when_under_an_hour(self):
        result = self._uptime(boot_time=0.0, now=300.0)  # 5 minutes, no days/hours parts
        self.assertEqual(result, "This PC has been on for 5 minutes.")

    def test_negative_elapsed_is_clamped_to_zero_not_negative(self):
        # boot_time() reported slightly in the future relative to time.time() -
        # clock skew, not a real negative uptime.
        result = self._uptime(boot_time=1000.0, now=999.0)
        self.assertEqual(result, "This PC has been on for 0 minutes.")

    def test_exception_is_a_friendly_failure(self):
        with mock.patch("monitoring.sys_monitor.psutil.boot_time", side_effect=OSError("boom")):
            result = SystemMonitor.get_uptime()
        self.assertEqual(result, "Sorry, I couldn't read the uptime.")


class LocalIpTests(unittest.TestCase):
    def test_udp_connect_trick_success_path(self):
        fake_sock = mock.Mock()
        fake_sock.getsockname.return_value = ("10.0.0.5", 54321)
        with mock.patch("socket.socket", return_value=fake_sock):
            result = SystemMonitor.get_local_ip()
        self.assertEqual(result, "Your local IP address is 10.0.0.5.")
        fake_sock.connect.assert_called_once_with(("8.8.8.8", 80))
        fake_sock.close.assert_called_once()

    def test_falls_back_to_hostname_lookup_when_connect_fails(self):
        fake_sock = mock.Mock()
        fake_sock.connect.side_effect = OSError("network is unreachable")
        with mock.patch("socket.socket", return_value=fake_sock), \
             mock.patch("socket.gethostbyname", return_value="127.0.1.1") as gethostbyname, \
             mock.patch("socket.gethostname", return_value="my-host"):
            result = SystemMonitor.get_local_ip()
        self.assertEqual(result, "Your local IP address is 127.0.1.1.")
        gethostbyname.assert_called_once_with("my-host")
        fake_sock.close.assert_called_once()  # the finally: still closes the socket before falling back

    def test_both_paths_failing_is_a_friendly_message(self):
        fake_sock = mock.Mock()
        fake_sock.connect.side_effect = OSError("network is unreachable")
        with mock.patch("socket.socket", return_value=fake_sock), \
             mock.patch("socket.gethostbyname", side_effect=OSError("no hostname either")):
            result = SystemMonitor.get_local_ip()
        self.assertEqual(result, "Sorry, I couldn't determine the local IP address.")


class GpuStatusTests(unittest.TestCase):
    def _install_pynvml(self, **attrs):
        fake = types.ModuleType("pynvml")
        fake.NVML_TEMPERATURE_GPU = 0
        for k, v in attrs.items():
            setattr(fake, k, v)
        return fake

    def test_missing_pynvml_is_a_friendly_message(self):
        # sys.modules[name] = None is the documented way to force ImportError
        # for a name regardless of whether the real package happens to be
        # installed in whatever environment this test runs in.
        with mock.patch.dict(sys.modules, {"pynvml": None}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "GPU monitoring needs the optional 'nvidia-ml-py' package "
                                  "(NVIDIA GPUs only) installed.")

    def test_nvml_init_failure_is_a_friendly_message_and_skips_shutdown(self):
        fake = self._install_pynvml(nvmlInit=mock.Mock(side_effect=RuntimeError("no driver")),
                                     nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "Sorry, I couldn't read GPU status - no NVIDIA GPU, or its driver isn't responding.")
        # nvmlShutdown is only inside the try/finally around the counting
        # step below nvmlInit - a failure IN nvmlInit itself returns before
        # that finally is ever entered.
        fake.nvmlShutdown.assert_not_called()

    def test_zero_gpus_detected(self):
        fake = self._install_pynvml(nvmlInit=mock.Mock(), nvmlDeviceGetCount=mock.Mock(return_value=0),
                                     nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "No NVIDIA GPU was detected.")
        fake.nvmlShutdown.assert_called_once()

    def test_one_gpu_with_temperature_and_bytes_name_is_decoded(self):
        util = types.SimpleNamespace(gpu=42)
        fake = self._install_pynvml(
            nvmlInit=mock.Mock(), nvmlDeviceGetCount=mock.Mock(return_value=1),
            nvmlDeviceGetHandleByIndex=mock.Mock(return_value="handle0"),
            nvmlDeviceGetName=mock.Mock(return_value=b"NVIDIA GeForce RTX 4090"),
            nvmlDeviceGetUtilizationRates=mock.Mock(return_value=util),
            nvmlDeviceGetTemperature=mock.Mock(return_value=65),
            nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "NVIDIA GeForce RTX 4090: 42% utilization, 65°C.")
        fake.nvmlShutdown.assert_called_once()

    def test_temperature_read_failure_is_tolerated_and_omitted(self):
        util = types.SimpleNamespace(gpu=17)
        fake = self._install_pynvml(
            nvmlInit=mock.Mock(), nvmlDeviceGetCount=mock.Mock(return_value=1),
            nvmlDeviceGetHandleByIndex=mock.Mock(return_value="handle0"),
            nvmlDeviceGetName=mock.Mock(return_value="Tesla T4"),  # already str, not bytes
            nvmlDeviceGetUtilizationRates=mock.Mock(return_value=util),
            nvmlDeviceGetTemperature=mock.Mock(side_effect=RuntimeError("no temp sensor")),
            nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "Tesla T4: 17% utilization.")

    def test_multiple_gpus_are_joined_with_semicolons(self):
        util0, util1 = types.SimpleNamespace(gpu=10), types.SimpleNamespace(gpu=20)
        fake = self._install_pynvml(
            nvmlInit=mock.Mock(), nvmlDeviceGetCount=mock.Mock(return_value=2),
            nvmlDeviceGetHandleByIndex=mock.Mock(side_effect=lambda i: f"handle{i}"),
            nvmlDeviceGetName=mock.Mock(side_effect=lambda h: {"handle0": "GPU0", "handle1": "GPU1"}[h]),
            nvmlDeviceGetUtilizationRates=mock.Mock(side_effect=lambda h: {"handle0": util0, "handle1": util1}[h]),
            nvmlDeviceGetTemperature=mock.Mock(side_effect=RuntimeError("n/a")),
            nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "GPU0: 10% utilization; GPU1: 20% utilization.")

    def test_counting_failure_after_init_is_a_friendly_message_and_still_shuts_down(self):
        fake = self._install_pynvml(nvmlInit=mock.Mock(),
                                     nvmlDeviceGetCount=mock.Mock(side_effect=RuntimeError("boom")),
                                     nvmlShutdown=mock.Mock())
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()
        self.assertEqual(result, "Sorry, I couldn't read GPU status.")
        fake.nvmlShutdown.assert_called_once()

    def test_shutdown_failure_itself_is_swallowed(self):
        fake = self._install_pynvml(nvmlInit=mock.Mock(), nvmlDeviceGetCount=mock.Mock(return_value=0),
                                     nvmlShutdown=mock.Mock(side_effect=RuntimeError("already down")))
        with mock.patch.dict(sys.modules, {"pynvml": fake}):
            result = SystemMonitor.get_gpu_status()  # must not raise
        self.assertEqual(result, "No NVIDIA GPU was detected.")


class CpuTemperatureTests(unittest.TestCase):
    def _temps(self, value):
        with mock.patch("monitoring.sys_monitor.psutil.sensors_temperatures", return_value=value):
            return SystemMonitor.get_cpu_temperature()

    def test_no_sensors_available_mentions_windows_and_macos(self):
        result = self._temps({})
        self.assertIn("isn't available on this system", result)
        self.assertIn("Windows", result)
        self.assertIn("macOS", result)

    def test_attribute_error_is_treated_as_no_sensors(self):
        with mock.patch("monitoring.sys_monitor.psutil.sensors_temperatures", side_effect=AttributeError):
            result = SystemMonitor.get_cpu_temperature()
        self.assertIn("isn't available on this system", result)

    def test_coretemp_is_preferred_over_other_keys(self):
        entry = types.SimpleNamespace(current=46.0)
        other = types.SimpleNamespace(current=99.0)
        result = self._temps({"coretemp": [entry], "some_other_sensor": [other]})
        self.assertEqual(result, "CPU temperature: 46°C.")

    def test_k10temp_used_when_no_coretemp(self):
        entry = types.SimpleNamespace(current=50.0)
        result = self._temps({"k10temp": [entry]})
        self.assertEqual(result, "CPU temperature: 50°C.")

    def test_cpu_thermal_key_variants_are_recognized(self):
        entry = types.SimpleNamespace(current=40.0)
        self.assertEqual(self._temps({"cpu_thermal": [entry]}), "CPU temperature: 40°C.")
        self.assertEqual(self._temps({"cpu-thermal": [entry]}), "CPU temperature: 40°C.")

    def test_unrecognized_key_falls_back_to_first_key_with_its_own_name(self):
        entry = types.SimpleNamespace(current=33.0)
        result = self._temps({"weird_sensor": [entry]})
        self.assertEqual(result, "weird_sensor temperature: 33°C.")

    def test_known_key_present_but_empty_list_falls_through_to_short_unavailable_message(self):
        # Distinct from the "not temps at all" case above: this is the SHORT
        # message (no Windows/macOS mention) reached only when temps itself
        # is non-empty but the selected key's own reading list is empty.
        result = self._temps({"coretemp": []})
        self.assertEqual(result, "CPU temperature isn't available on this system.")


class TopProcessesTests(unittest.TestCase):
    def _proc(self, name, cpu=None, memory_percent=None):
        info = {"name": name}
        if memory_percent is not None:
            info["memory_percent"] = memory_percent
        p = mock.Mock(info=info)
        if cpu is not None:
            p.cpu_percent = mock.Mock(return_value=cpu)
        return p

    def test_cpu_sort_default_count_and_label(self):
        procs = [self._proc("alpha", cpu=10.0), self._proc("beta", cpu=90.0), self._proc("gamma", cpu=50.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes()
        self.assertEqual(result, "Top processes by CPU usage: beta (90.0%), gamma (50.0%), alpha (10.0%).")

    def test_memory_sort_uses_memory_percent_and_different_label(self):
        procs = [self._proc("alpha", memory_percent=2.0), self._proc("beta", memory_percent=8.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs):
            result = SystemMonitor.get_top_processes(by="memory")
        self.assertEqual(result, "Top processes by memory usage: beta (8.0%), alpha (2.0%).")

    def test_sort_by_is_case_insensitive_and_anything_else_means_cpu(self):
        procs = [self._proc("alpha", memory_percent=2.0), self._proc("beta", memory_percent=8.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs):
            result = SystemMonitor.get_top_processes(by="MEMORY")
        self.assertIn("by memory usage", result)
        procs = [self._proc("alpha", cpu=2.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes(by="bogus")
        self.assertIn("by CPU usage", result)

    def test_count_is_clamped_to_a_maximum_of_15(self):
        procs = [self._proc(f"p{i}", cpu=float(i)) for i in range(20)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes(n=999)
        self.assertEqual(result.count("%)"), 15)
        self.assertIn("p19 ", result)   # highest values make the cut
        self.assertNotIn("p4 (", result)  # p4 and below don't (top 15 of 0..19 is 5..19)

    def test_negative_count_is_clamped_to_one(self):
        procs = [self._proc("solo", cpu=5.0), self._proc("other", cpu=99.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes(n=-3)
        self.assertEqual(result.count("%)"), 1)
        self.assertIn("other", result)

    def test_count_of_exactly_zero_is_falsy_so_it_defaults_to_five_not_one(self):
        # n = max(1, min(15, int(n or 5))) - "n or 5" treats 0 the same as
        # None/omitted (both falsy in Python) and substitutes the default of
        # 5, rather than reaching the max(1, ...) floor the way a genuinely
        # negative n does above. Only two processes exist here, so this just
        # confirms neither one gets dropped - not that exactly 5 would show.
        procs = [self._proc("solo", cpu=5.0), self._proc("other", cpu=99.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes(n=0)
        self.assertEqual(result.count("%)"), 2)

    def test_non_integer_count_falls_back_to_default_of_five(self):
        procs = [self._proc("only", cpu=1.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes(n=None)
        self.assertEqual(result.count("%)"), 1)  # only one real process either way, but must not raise

    def test_processes_with_no_name_are_skipped(self):
        procs = [self._proc(None, cpu=99.0), self._proc("real", cpu=1.0)]
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=procs), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes()
        self.assertEqual(result.count("%)"), 1)
        self.assertIn("real", result)

    def test_vanishing_process_during_the_second_pass_is_skipped_not_fatal(self):
        ghost = self._proc("ghost")
        ghost.cpu_percent = mock.Mock(side_effect=[None, psutil.NoSuchProcess(1234)])
        alive = self._proc("alive", cpu=7.0)
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=[ghost, alive]), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes()
        self.assertNotIn("ghost", result)
        self.assertIn("alive", result)

    def test_no_processes_is_a_friendly_message(self):
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", return_value=[]), \
             mock.patch("monitoring.sys_monitor.time.sleep"):
            result = SystemMonitor.get_top_processes()
        self.assertEqual(result, "Couldn't read the process list.")

    def test_outer_exception_is_a_friendly_failure(self):
        with mock.patch("monitoring.sys_monitor.psutil.process_iter", side_effect=RuntimeError("boom")):
            result = SystemMonitor.get_top_processes()
        self.assertEqual(result, "Sorry, I couldn't read the process list.")


class DiskUsageTests(unittest.TestCase):
    def _part(self, device, mountpoint):
        return types.SimpleNamespace(device=device, mountpoint=mountpoint)

    def _usage(self, total_gb, used_gb, percent):
        # Tier 18: `free` added alongside the pre-existing total/used/percent -
        # get_disk_partitions_usage() (which get_disk_usage() now builds its
        # string from) reads usage.free too, for automation/proactive.py's
        # _check_low_disk trigger. Real psutil.disk_usage() always has this
        # field; only this fake object needed updating to match.
        return types.SimpleNamespace(total=total_gb * (1024 ** 3), used=used_gb * (1024 ** 3),
                                     free=max(0.0, total_gb - used_gb) * (1024 ** 3), percent=percent)

    def test_formats_each_partition(self):
        parts = [self._part("/dev/sda1", "/")]
        usage = self._usage(100.0, 50.0, 50.0)
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", return_value=usage):
            result = SystemMonitor.get_disk_usage()
        self.assertEqual(result, "Disk usage - /dev/sda1 50% used (50.0 of 100.0 GB).")

    def test_skips_partitions_whose_usage_raises(self):
        parts = [self._part("/dev/sr0", "/media/cdrom"), self._part("/dev/sda1", "/")]

        def fake_usage(mountpoint):
            if mountpoint == "/media/cdrom":
                raise OSError("no medium")
            return self._usage(100.0, 10.0, 10.0)

        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", side_effect=fake_usage):
            result = SystemMonitor.get_disk_usage()
        self.assertNotIn("cdrom", result)
        self.assertIn("/dev/sda1", result)

    def test_skips_partitions_whose_usage_raises_permission_error_too(self):
        parts = [self._part("/dev/locked", "/locked"), self._part("/dev/sda1", "/")]

        def fake_usage(mountpoint):
            if mountpoint == "/locked":
                raise PermissionError("denied")
            return self._usage(100.0, 10.0, 10.0)

        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", side_effect=fake_usage):
            result = SystemMonitor.get_disk_usage()
        self.assertNotIn("locked", result)

    def test_skips_tiny_partitions_under_a_tenth_of_a_gb(self):
        parts = [self._part("efi", "/boot/efi"), self._part("/dev/sda1", "/")]

        def fake_usage(mountpoint):
            if mountpoint == "/boot/efi":
                return self._usage(0.05, 0.01, 20.0)
            return self._usage(100.0, 10.0, 10.0)

        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", side_effect=fake_usage):
            result = SystemMonitor.get_disk_usage()
        self.assertNotIn("efi", result)
        self.assertIn("/dev/sda1", result)

    def test_no_usable_partitions_is_a_friendly_message(self):
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=[]):
            result = SystemMonitor.get_disk_usage()
        self.assertEqual(result, "Sorry, I couldn't read disk usage.")

    def test_outer_exception_is_a_friendly_failure(self):
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", side_effect=RuntimeError("boom")):
            result = SystemMonitor.get_disk_usage()
        self.assertEqual(result, "Sorry, I couldn't read disk usage.")


class DiskPartitionsUsageTests(unittest.TestCase):
    """Tier 18: get_disk_partitions_usage() - the structured data
    get_disk_usage() above now builds its sentence FROM. Deliberately a
    separate class (not a subclass of DiskUsageTests) so unittest doesn't
    also re-collect/re-run that class's own test_ methods as part of this
    one - see this file's other independent test classes for the same
    "small local helper duplication over subclassing a TestCase" pattern."""

    def _part(self, device, mountpoint):
        return types.SimpleNamespace(device=device, mountpoint=mountpoint)

    def _usage(self, total_gb, used_gb, percent):
        return types.SimpleNamespace(total=total_gb * (1024 ** 3), used=used_gb * (1024 ** 3),
                                     free=max(0.0, total_gb - used_gb) * (1024 ** 3), percent=percent)

    def test_returns_one_dict_per_readable_partition(self):
        parts = [self._part("/dev/sda1", "/")]
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", return_value=self._usage(100.0, 92.0, 92.0)):
            result = SystemMonitor.get_disk_partitions_usage()
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["device"], "/dev/sda1")
        self.assertEqual(entry["percent"], 92.0)
        self.assertAlmostEqual(entry["used_gb"], 92.0)
        self.assertAlmostEqual(entry["total_gb"], 100.0)
        self.assertAlmostEqual(entry["free_gb"], 8.0)

    def test_skips_unreadable_and_tiny_partitions_same_as_get_disk_usage(self):
        parts = [self._part("/dev/sr0", "/media/cdrom"), self._part("efi", "/boot/efi"),
                 self._part("/dev/sda1", "/")]

        def fake_usage(mountpoint):
            if mountpoint == "/media/cdrom":
                raise OSError("no medium")
            if mountpoint == "/boot/efi":
                return self._usage(0.05, 0.01, 20.0)
            return self._usage(100.0, 50.0, 50.0)

        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", side_effect=fake_usage):
            result = SystemMonitor.get_disk_partitions_usage()
        self.assertEqual([p["device"] for p in result], ["/dev/sda1"])

    def test_no_partitions_is_an_empty_list_not_an_error(self):
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=[]):
            self.assertEqual(SystemMonitor.get_disk_partitions_usage(), [])

    def test_get_disk_usage_string_is_unchanged_by_the_refactor(self):
        """The whole point of pulling get_disk_partitions_usage() out was
        "no behavior change" for the existing string - pin that directly,
        on top of DiskUsageTests above already covering it indirectly."""
        parts = [self._part("/dev/sda1", "/")]
        with mock.patch("monitoring.sys_monitor.psutil.disk_partitions", return_value=parts), \
             mock.patch("monitoring.sys_monitor.psutil.disk_usage", return_value=self._usage(100.0, 50.0, 50.0)):
            result = SystemMonitor.get_disk_usage()
        self.assertEqual(result, "Disk usage - /dev/sda1 50% used (50.0 of 100.0 GB).")


class BatteryStatusTests(unittest.TestCase):
    def _battery(self, percent, power_plugged, secsleft):
        return types.SimpleNamespace(percent=percent, power_plugged=power_plugged, secsleft=secsleft)

    def _status(self, battery, wear=None):
        with mock.patch("monitoring.sys_monitor.psutil.sensors_battery", return_value=battery), \
             mock.patch.object(SystemMonitor, "_windows_battery_wear", return_value=wear):
            return SystemMonitor.get_battery_status()

    def test_no_battery_reports_desktop_or_vm(self):
        result = self._status(None)
        self.assertEqual(result, "This PC doesn't report a battery - likely a desktop, or a VM.")

    def test_plugged_in_never_shows_time_remaining(self):
        battery = self._battery(87.4, True, 999999)
        result = self._status(battery)
        self.assertEqual(result, "Battery: 87% charged, plugged in.")

    def test_on_battery_with_hours_and_minutes_remaining(self):
        battery = self._battery(100.0, False, 5400)  # 1h30m
        result = self._status(battery)
        self.assertEqual(result, "Battery: 100% charged, on battery, about 1h 30m remaining.")

    def test_on_battery_minutes_only_when_under_an_hour(self):
        battery = self._battery(42.0, False, 600)  # 10 minutes
        result = self._status(battery)
        self.assertEqual(result, "Battery: 42% charged, on battery, about 10 minutes remaining.")

    def test_power_time_unlimited_is_excluded(self):
        battery = self._battery(55.0, False, psutil.POWER_TIME_UNLIMITED)
        result = self._status(battery)
        self.assertEqual(result, "Battery: 55% charged, on battery.")

    def test_zero_or_none_secsleft_is_excluded(self):
        self.assertEqual(self._status(self._battery(10.0, False, 0)), "Battery: 10% charged, on battery.")
        self.assertEqual(self._status(self._battery(10.0, False, None)), "Battery: 10% charged, on battery.")

    def test_exception_is_a_friendly_failure(self):
        with mock.patch("monitoring.sys_monitor.psutil.sensors_battery", side_effect=RuntimeError("boom")):
            result = SystemMonitor.get_battery_status()
        self.assertEqual(result, "Sorry, I couldn't read the battery status.")

    def test_wear_percentage_is_appended_last_when_available(self):
        battery = self._battery(80.0, True, 0)
        result = self._status(battery, wear=87)
        self.assertEqual(result, "Battery: 80% charged, plugged in, 87% of its original design capacity remaining.")

    def test_wear_percentage_omitted_when_none(self):
        battery = self._battery(80.0, True, 0)
        result = self._status(battery, wear=None)
        self.assertEqual(result, "Battery: 80% charged, plugged in.")


class WindowsBatteryWearTests(unittest.TestCase):
    def _report_path(self):
        return os.path.join(tempfile.gettempdir(), "wave_battery_report.xml")

    def test_returns_none_immediately_on_non_windows(self):
        with mock.patch("monitoring.sys_monitor.platform.system", return_value="Linux"), \
             mock.patch("subprocess.run") as run:
            result = SystemMonitor._windows_battery_wear()
        self.assertIsNone(result)
        run.assert_not_called()

    def test_returns_none_when_powercfg_exits_non_zero(self):
        with mock.patch("monitoring.sys_monitor.platform.system", return_value="Windows"), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            result = SystemMonitor._windows_battery_wear()
        self.assertIsNone(result)

    def test_returns_none_when_report_file_was_never_written(self):
        report_path = self._report_path()
        if os.path.isfile(report_path):
            os.remove(report_path)  # guard against a stale leftover from a prior failed run
        with mock.patch("monitoring.sys_monitor.platform.system", return_value="Windows"), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            result = SystemMonitor._windows_battery_wear()
        self.assertIsNone(result)

    def test_subprocess_exception_is_caught_and_returns_none(self):
        with mock.patch("monitoring.sys_monitor.platform.system", return_value="Windows"), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError("no powercfg here")):
            result = SystemMonitor._windows_battery_wear()
        self.assertIsNone(result)

    def _run_with_report(self, xml_text):
        report_path = self._report_path()
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(xml_text)
        try:
            with mock.patch("monitoring.sys_monitor.platform.system", return_value="Windows"), \
                 mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
                result = SystemMonitor._windows_battery_wear()
        finally:
            if os.path.isfile(report_path):
                os.remove(report_path)
        return result, report_path

    def test_parses_design_and_full_charge_capacity_and_removes_the_report(self):
        xml = ("<root><DesignCapacity>50000</DesignCapacity>"
               "<FullChargeCapacity>45000</FullChargeCapacity></root>")
        result, report_path = self._run_with_report(xml)
        self.assertEqual(result, 90)  # round(45000 / 50000 * 100)
        self.assertFalse(os.path.isfile(report_path))  # the function cleans up its own temp file

    def test_zero_design_capacity_is_treated_as_unparseable(self):
        xml = "<root><DesignCapacity>0</DesignCapacity><FullChargeCapacity>0</FullChargeCapacity></root>"
        result, report_path = self._run_with_report(xml)
        self.assertIsNone(result)
        self.assertFalse(os.path.isfile(report_path))  # still cleaned up even though unparseable

    def test_missing_tags_in_the_xml_return_none(self):
        result, report_path = self._run_with_report("<root><SomethingElse>1</SomethingElse></root>")
        self.assertIsNone(result)
        self.assertFalse(os.path.isfile(report_path))


# =============================================================================
# brain/handlers/system.py - thin delegation to SystemMonitor/AppController/
# get_backend(), plus list_services's own count/truncation/NotSupported
# handling on top of the raw backend call.
# =============================================================================

class SkillDelegationTests(unittest.TestCase):
    def test_simple_zero_arg_skills_delegate_to_system_monitor(self):
        cases = [
            ("get_uptime", system_handlers._skill_get_uptime, "UPTIME"),
            ("get_local_ip", system_handlers._skill_get_local_ip, "IP"),
            ("get_gpu_status", system_handlers._skill_get_gpu_status, "GPU"),
            ("get_cpu_temperature", system_handlers._skill_get_cpu_temperature, "TEMP"),
            ("get_disk_usage", system_handlers._skill_get_disk_usage, "DISK"),
            ("get_battery_status", system_handlers._skill_get_battery_status, "BATTERY"),
        ]
        for method_name, handler, sentinel in cases:
            with mock.patch.object(SystemMonitor, method_name, return_value=sentinel) as m:
                self.assertEqual(handler(None, {}), sentinel)
            m.assert_called_once_with()

    def test_get_brightness_status_delegates_to_app_controller(self):
        with mock.patch.object(AppController, "get_brightness_status", return_value="55%") as m:
            result = system_handlers._skill_get_brightness_status(None, {})
        self.assertEqual(result, "55%")
        m.assert_called_once_with()

    def test_get_top_processes_passes_explicit_args_through(self):
        with mock.patch.object(SystemMonitor, "get_top_processes", return_value="TOP") as m:
            result = system_handlers._skill_get_top_processes(None, {"count": 10, "sort_by": "memory"})
        self.assertEqual(result, "TOP")
        m.assert_called_once_with(10, "memory")

    def test_get_top_processes_defaults_count_and_sort_by_when_omitted(self):
        with mock.patch.object(SystemMonitor, "get_top_processes", return_value="TOP") as m:
            system_handlers._skill_get_top_processes(None, {})
        m.assert_called_once_with(5, "cpu")

    def test_list_services_no_services_found(self):
        with mock.patch("brain.handlers.system.get_backend") as get_backend:
            get_backend.return_value.list_services.return_value = []
            result = system_handlers._skill_list_services(None, {})
        self.assertEqual(result, "No running services were found.")

    def test_list_services_under_the_truncation_threshold_lists_them_all(self):
        with mock.patch("brain.handlers.system.get_backend") as get_backend:
            get_backend.return_value.list_services.return_value = ["alpha", "beta", "gamma"]
            result = system_handlers._skill_list_services(None, {})
        self.assertEqual(result, "3 running service(s): alpha, beta, gamma.")

    def test_list_services_truncates_at_30_with_a_count_of_the_rest(self):
        names = [f"svc{i}" for i in range(35)]
        with mock.patch("brain.handlers.system.get_backend") as get_backend:
            get_backend.return_value.list_services.return_value = names
            result = system_handlers._skill_list_services(None, {})
        self.assertTrue(result.startswith("35 running service(s): "))
        self.assertIn("(and 5 more)", result)
        self.assertNotIn("svc30", result)  # the 31st service (index 30) is past the shown-30 cutoff
        self.assertIn("svc29", result)     # the 30th (index 29) is the last one actually shown

    def test_list_services_exactly_30_has_no_truncation_suffix(self):
        names = [f"svc{i}" for i in range(30)]
        with mock.patch("brain.handlers.system.get_backend") as get_backend:
            get_backend.return_value.list_services.return_value = names
            result = system_handlers._skill_list_services(None, {})
        self.assertNotIn("more", result)
        self.assertTrue(result.endswith("svc29."))

    def test_list_services_not_supported_is_a_plain_reply_not_a_raised_exception(self):
        with mock.patch("brain.handlers.system.get_backend") as get_backend:
            get_backend.return_value.list_services.side_effect = NotSupported("Listing services isn't supported here.")
            result = system_handlers._skill_list_services(None, {})
        self.assertEqual(result, "Listing services isn't supported here.")


if __name__ == "__main__":
    unittest.main()
