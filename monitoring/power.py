"""
monitoring/power.py — Tier 31: keep Wave's own process out of Windows'
power throttling (EcoQoS).

Windows 11 moves a process it treats as "background" onto the efficiency
cores at a low clock once it has been busy for a second or so. Measured on
the user's laptop (i5-13450HX): torch's matrix maths went from ~0.12 ms to
~1.8 ms per call - 15x slower - about a second into sustained work, which
turned a cloned-voice sentence that takes ~1.7x its own length to
synthesize into one that took up to 10x (and slows Whisper the same way).
Wave has to answer quickly whether its window is in front or not, so both
launchers opt the process out with SetProcessInformation(
ProcessPowerThrottling) - the documented per-process switch; no system
setting is changed. POWER_THROTTLING_OPT_OUT=false in .env turns it off.
"""
import logging
import sys

logger = logging.getLogger("Power")

_PROCESS_POWER_THROTTLING = 4      # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_EXECUTION_SPEED = 0x1             # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
_CURRENT_VERSION = 1               # PROCESS_POWER_THROTTLING_CURRENT_VERSION


def keep_full_speed() -> bool:
    """Opts this process out of Windows' execution-speed throttling.
    Returns True if Windows accepted it, False on other platforms or if
    the call failed. Never raises."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class _ThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        # Its own WinDLL instance, so these argtypes don't leak into anyone
        # else's ctypes.windll.kernel32.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                   wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        # ControlMask = "I decide execution speed", StateMask 0 = "never throttle".
        state = _ThrottlingState(_CURRENT_VERSION, _EXECUTION_SPEED, 0)
        if kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING,
                                          ctypes.byref(state), ctypes.sizeof(state)):
            logger.info("Windows power throttling (EcoQoS) turned off for Wave's process.")
            return True
        logger.warning(f"Couldn't turn off power throttling (Windows error {ctypes.get_last_error()}).")
    except Exception as e:
        logger.warning(f"Couldn't turn off power throttling: {e}")
    return False
