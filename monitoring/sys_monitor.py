import os
import psutil
import platform
import logging
import sys
import time

logger = logging.getLogger("SysMonitor")

def _system_drive() -> str:
    """🔧 FIX: this used to be a hardcoded '/'. On Windows that quietly means
    "whichever drive the app's current working folder is on" - and this app
    chdir's into its own project folder at startup, so if the project lives
    on D:\\ the System Monitor would report D:\\ while labelling it "System
    Drive". This uses the drive Windows itself is actually installed on."""
    if platform.system() == "Windows":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"

# Phase 4 (UI smoothness): psutil.cpu_percent(interval=0.1) SLEEPS for 100 ms while it
# samples. get_stats() runs on the GUI thread once a second, so the whole interface
# froze for a tenth of every second (measured: ~100 ms per call). interval=None instead
# reports usage since the previous call and returns instantly. Priming it here means
# the very first on-screen reading is real rather than 0.
psutil.cpu_percent(interval=None)


class SystemMonitor:
    """Fetches real-time system resource usage and hardware specs."""

    @staticmethod
    def _disk_usage():
        """Disk usage for the system drive, or None if it can't be read.
        Kept separate so a disk problem can't take the whole stats dict down."""
        try:
            return psutil.disk_usage(_system_drive())
        except Exception as e:
            logger.error(f"Error reading disk usage for {_system_drive()!r}: {e}")
            return None
    
    @staticmethod
    def get_stats() -> dict:
        try:
            battery = psutil.sensors_battery()
            battery_percent = battery.percent if battery else "N/A"
            is_plugged = battery.power_plugged if battery else False
            # 🔧 FIX: disk used to be read inline inside this one big dict, so
            # a single failing disk read made the whole function return {} -
            # blanking CPU, RAM and battery too. Now it fails on its own.
            disk = SystemMonitor._disk_usage()
            memory = psutil.virtual_memory()   # one syscall instead of three

            return {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "ram_percent": memory.percent,
                "ram_used_gb": round(memory.used / (1024**3), 1),
                "ram_total_gb": round(memory.total / (1024**3), 1),
                "disk_percent": disk.percent if disk else 0,
                "battery_percent": battery_percent,
                "is_plugged": is_plugged
            }
        except Exception as e:
            logger.error(f"Error fetching system stats: {e}")
            return {}

    @staticmethod
    def get_hardware_info() -> str:
        """Fetches static hardware details of the PC with Windows 11 fix."""
        try:
            os_system = platform.system()
            os_release = platform.release()
            
            # Windows 11 Fix: Check build number because Microsoft kept internal version as 10
            if os_system == "Windows" and os_release == "10":
                if hasattr(sys, 'getwindowsversion'):
                    if sys.getwindowsversion().build >= 22000:
                        os_release = "11"

            os_info = f"{os_system} {os_release}"
            cpu_info = platform.processor()
            ram_total = round(psutil.virtual_memory().total / (1024**3), 2)
            disk = SystemMonitor._disk_usage()
            disk_total = f"{round(disk.total / (1024**3), 2)} GB" if disk else "unavailable"
            
            info = (
                f"Here is your actual system information:\n\n"
                f"🖥️ **Operating System:** {os_info}\n"
                f"🧠 **Processor (CPU):** {cpu_info}\n"
                f"⚡ **Total RAM:** {ram_total} GB\n"
                f"💾 **System Drive Capacity:** {disk_total}\n"
                f"🏷️ **PC Name:** {platform.node()}"
            )
            return info
        except Exception as e:
            logger.error(f"Error fetching hardware info: {e}")
            return "Sorry, I encountered an error while fetching your hardware details."

    # =========================================================================
    # Tier 14: system info - all plain psutil/socket/platform reads, nothing
    # OS-specific enough to need automation/backends/ (list_services is the
    # one new Tier 14 read-only query that genuinely IS OS-specific - it lives
    # as a PlatformBackend method instead, alongside the existing
    # start_service/stop_service; see automation/backends/*.py).
    # =========================================================================
    @staticmethod
    def get_uptime() -> str:
        try:
            elapsed = max(0.0, time.time() - psutil.boot_time())
            days, rem = divmod(int(elapsed), 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _ = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if hours:
                parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
            if minutes or not parts:
                parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
            return f"This PC has been on for {', '.join(parts)}."
        except Exception as e:
            logger.error(f"Error reading uptime: {e}")
            return "Sorry, I couldn't read the uptime."

    @staticmethod
    def get_local_ip() -> str:
        # The classic no-packets-actually-sent trick: UDP "connect" just
        # asks the OS which local interface/IP WOULD be used to reach that
        # address, without putting anything on the wire - 8.8.8.8 is never
        # actually contacted. Falls back to the hostname lookup if that
        # somehow fails (e.g. no route to any external network at all).
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
            return f"Your local IP address is {ip}."
        except OSError:
            try:
                ip = socket.gethostbyname(socket.gethostname())
                return f"Your local IP address is {ip}."
            except OSError as e:
                logger.error(f"Error reading local IP: {e}")
                return "Sorry, I couldn't determine the local IP address."

    @staticmethod
    def get_gpu_status() -> str:
        # Optional, NVIDIA-only (same "not in requirements.txt, opt in
        # yourself" reasoning as voice/tts.py's Piper/Kokoro - see
        # docs/VOICE_LOCAL.md) - pip install nvidia-ml-py for this one.
        try:
            import pynvml
        except ImportError:
            return "GPU monitoring needs the optional 'nvidia-ml-py' package (NVIDIA GPUs only) installed."
        try:
            pynvml.nvmlInit()
        except Exception as e:
            logger.error(f"Error initializing NVML: {e}")
            return "Sorry, I couldn't read GPU status - no NVIDIA GPU, or its driver isn't responding."
        try:
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                return "No NVIDIA GPU was detected."
            parts = []
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                name = name.decode() if isinstance(name, bytes) else name
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    temp_text = f", {temp}°C"
                except Exception:
                    temp_text = ""
                parts.append(f"{name}: {util.gpu}% utilization{temp_text}")
            return "; ".join(parts) + "."
        except Exception as e:
            logger.error(f"Error reading GPU status: {e}")
            return "Sorry, I couldn't read GPU status."
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    @staticmethod
    def get_cpu_temperature() -> str:
        # 🔧 Known, honest gap: psutil.sensors_temperatures() reliably
        # returns {} on Windows - there's no portable kernel sensor API
        # there for psutil to read (a real reading would need WMI access
        # to a 3rd-party tool like LibreHardwareMonitor, not attempted
        # here). Linux usually has real coretemp/k10temp/etc. sensors;
        # macOS has none via psutil either.
        try:
            temps = psutil.sensors_temperatures()
        except AttributeError:
            temps = {}
        except Exception as e:
            logger.error(f"Error reading CPU temperature: {e}")
            temps = {}
        if not temps:
            return ("CPU temperature isn't available on this system (this is common on Windows "
                    "and macOS - psutil has no portable sensor API there).")
        for key in ("coretemp", "k10temp", "cpu_thermal", "cpu-thermal"):
            if temps.get(key):
                return f"CPU temperature: {temps[key][0].current:.0f}°C."
        first_key = next(iter(temps))
        if temps[first_key]:
            return f"{first_key} temperature: {temps[first_key][0].current:.0f}°C."
        return "CPU temperature isn't available on this system."

    @staticmethod
    def get_top_processes(n: int = 5, by: str = "cpu") -> str:
        by = "memory" if (by or "").strip().lower() == "memory" else "cpu"
        n = max(1, min(15, int(n or 5)))
        try:
            if by == "cpu":
                # 🔧 psutil's per-process cpu_percent, like the system-wide
                # one this module primes at import time above, needs TWO
                # samples to mean anything - reading it immediately after
                # process_iter() is always 0.0 for every process. A short,
                # deliberate pause between two passes is the only honest
                # way to get real numbers here - this is a slower call on
                # purpose (~0.2s), fine for an explicit, infrequent voice
                # query, not something polled continuously.
                procs = list(psutil.process_iter(['name']))
                for p in procs:
                    try:
                        p.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                time.sleep(0.2)
                rows = []
                for p in procs:
                    try:
                        rows.append((p.info.get('name'), p.cpu_percent(interval=None)))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            else:
                rows = []
                for p in psutil.process_iter(['name', 'memory_percent']):
                    try:
                        rows.append((p.info.get('name'), p.info.get('memory_percent') or 0.0))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            rows = [(name, val) for name, val in rows if name]
            rows.sort(key=lambda t: t[1], reverse=True)
            top = rows[:n]
            if not top:
                return "Couldn't read the process list."
            label = "memory" if by == "memory" else "CPU"
            parts = [f"{name} ({val:.1f}%)" for name, val in top]
            return f"Top processes by {label} usage: " + ", ".join(parts) + "."
        except Exception as e:
            logger.error(f"Error reading top processes: {e}")
            return "Sorry, I couldn't read the process list."

    @staticmethod
    def get_disk_partitions_usage() -> list:
        """[{"device", "percent", "used_gb", "total_gb", "free_gb"}, ...] -
        Tier 18: pulled out of get_disk_usage() below (which now just
        formats THIS into a sentence) so automation/proactive.py's
        _check_low_disk trigger has real numbers to threshold on, instead
        of having to parse its own string back apart. Same filtering as
        before the split: skips an unreadable mount (e.g. an empty optical
        drive) and anything under 0.1 GB (tiny virtual/EFI partitions -
        not what someone means by "my drives")."""
        partitions = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            total_gb = usage.total / (1024 ** 3)
            if total_gb < 0.1:
                continue
            partitions.append({
                "device": part.device,
                "percent": usage.percent,
                "used_gb": usage.used / (1024 ** 3),
                "total_gb": total_gb,
                "free_gb": usage.free / (1024 ** 3),
            })
        return partitions

    @staticmethod
    def get_disk_usage() -> str:
        """Every mounted drive, not just the system drive _disk_usage() above
        covers - Tier 14's own widening of that existing system-drive-only
        reading. Tier 18: now just formats get_disk_partitions_usage()'s
        structured data - same exact wording as before that split."""
        try:
            partitions = SystemMonitor.get_disk_partitions_usage()
            if not partitions:
                return "Sorry, I couldn't read disk usage."
            parts = [f"{p['device']} {p['percent']:.0f}% used "
                     f"({p['used_gb']:.1f} of {p['total_gb']:.1f} GB)" for p in partitions]
            return "Disk usage - " + "; ".join(parts) + "."
        except Exception as e:
            logger.error(f"Error reading disk usage: {e}")
            return "Sorry, I couldn't read disk usage."

    @staticmethod
    def get_battery_status() -> str:
        try:
            battery = psutil.sensors_battery()
        except Exception as e:
            logger.error(f"Error reading battery status: {e}")
            return "Sorry, I couldn't read the battery status."
        if battery is None:
            return "This PC doesn't report a battery - likely a desktop, or a VM."
        parts = [f"{round(battery.percent)}% charged", "plugged in" if battery.power_plugged else "on battery"]
        if not battery.power_plugged and battery.secsleft and battery.secsleft > 0 and \
                battery.secsleft != psutil.POWER_TIME_UNLIMITED:
            hours, rem = divmod(int(battery.secsleft), 3600)
            minutes = rem // 60
            parts.append(f"about {hours}h {minutes}m remaining" if hours else f"about {minutes} minutes remaining")
        wear = SystemMonitor._windows_battery_wear()
        if wear is not None:
            parts.append(f"{wear}% of its original design capacity remaining")
        return "Battery: " + ", ".join(parts) + "."

    @staticmethod
    def _windows_battery_wear():
        """Best-effort battery HEALTH (design capacity vs full-charge
        capacity), not just percent-charged - genuinely Windows-only
        (powercfg /batteryreport), and only even attempted on a laptop that
        has a battery at all. None on any failure (missing tool, desktop
        PC, a report Windows didn't produce a parseable XML for) - an
        absent wear figure, not a wrong one."""
        if platform.system() != "Windows":
            return None
        try:
            import re
            import subprocess
            import tempfile
            report_path = os.path.join(tempfile.gettempdir(), "wave_battery_report.xml")
            result = subprocess.run(["powercfg", "/batteryreport", "/output", report_path, "/xml"],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0 or not os.path.isfile(report_path):
                return None
            with open(report_path, "r", encoding="utf-8", errors="ignore") as f:
                xml_text = f.read()
            design = re.search(r"<DesignCapacity>(\d+)</DesignCapacity>", xml_text)
            full = re.search(r"<FullChargeCapacity>(\d+)</FullChargeCapacity>", xml_text)
            os.remove(report_path)
            if design and full and int(design.group(1)) > 0:
                return round(int(full.group(1)) / int(design.group(1)) * 100)
        except Exception as e:
            logger.error(f"Error reading battery wear report: {e}")
        return None