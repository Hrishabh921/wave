"""
automation/backends — Phase 4: the platform-abstraction layer.

AppController and FileManager (the classes the rest of Wave calls) decide WHAT
to do; a PlatformBackend decides HOW the current operating system does it.
Everything that used to be hard-wired to Windows (the Win-key app launcher,
ms-settings: URIs, rundll32 lock, the registry browser lookup, WhatsApp
Desktop automation ...) now lives in backends/windows.py behind the interface
in backends/base.py. backends/linux.py and backends/macos.py are small
reference implementations; see docs/PLATFORM.md for how to add or finish one.

    from automation.backends import get_backend
    get_backend().lock_screen()

Backends are imported with plain static imports (never importlib with a computed
name), so PyInstaller finds all of them without any hidden-import hints.
"""
import platform as _platform

_backend = None


def current_os() -> str:
    """'windows' | 'macos' | 'linux' | whatever platform.system() says, lower-cased."""
    system = _platform.system()
    return {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(system, system.lower())


def get_backend():
    """The backend for this machine (created once, then cached)."""
    global _backend
    if _backend is None:
        os_name = current_os()
        if os_name == "windows":
            from automation.backends.windows import WindowsBackend
            _backend = WindowsBackend()
        elif os_name == "macos":
            from automation.backends.macos import MacBackend
            _backend = MacBackend()
        elif os_name == "linux":
            from automation.backends.linux import LinuxBackend
            _backend = LinuxBackend()
        else:
            from automation.backends.base import UnsupportedBackend
            _backend = UnsupportedBackend(os_name)
    return _backend


def set_backend(backend) -> None:
    """Override the detected backend (tests, or forcing one while developing). None = re-detect."""
    global _backend
    _backend = backend
