# Platform layer (`automation/backends/`)

`AppController` and `FileManager` are what the rest of Wave calls. They decide **what** to do (which website,
which process, what to say back). A `PlatformBackend` decides **how this operating system does it**.

```
brain / UI  ->  AppController.lock_pc()  ->  get_backend().lock_screen()
                (audit + wording)            WindowsBackend | LinuxBackend | MacBackend | UnsupportedBackend
```

* `base.py` - the interface (`PlatformBackend`), `NotSupported`, and `Shell` (the only way non-Windows
  backends touch the OS, so tests can fake it).
* `windows.py` - the original Windows code, moved over unchanged. `tests/test_phase4_platform.py` pins every
  keystroke, sleep, shell command and reply against a log captured from the pre-refactor code.
* `linux.py`, `macos.py` - **experimental** reference backends. Their command construction is unit-tested with a
  fake `Shell`; they have **not** been run on a real Linux/macOS desktop.

## What a backend implements

Every method is optional: anything not overridden raises `NotSupported`, which `AppController` turns into
"... isn't supported on <OS> yet." instead of a crash. `backend.capabilities()` reports what is really implemented.

| Method | Purpose |
|---|---|
| `launch_app(name)` | start an app by name |
| `find_browser(name)` / `launch_in_browser(handle, url)` / `open_url_default(url)` | open a URL, optionally in a named browser |
| `toggle_radio(command)` | Wi-Fi / Bluetooth / hotspot |
| `close_window()` / `close_tab()` | focused window / tab |
| `open_system_setting(kind)` | display, sound, battery, update, settings |
| `lock_screen()` | lock the session |
| `type_in_text_editor(text)` | the "write in Notepad" skill |
| `send_whatsapp_message(contact, message)` | WhatsApp automation |
| `start_service(name)` / `stop_service(name)` | OS-level background services (Tier 8) - `sc.exe` / `systemctl` / `launchctl`, **not** a regular application |
| `list_open_windows()` / `focus_window(name)` / `minimize_window(name)` / `snap_window(name, position)` | per-window management (Tier 8) - one *specific* window by (a piece of) its title, unlike `close_window()` above, which only ever acts on whatever already has focus |
| `open_path` / `reveal_path` / `open_image_viewer` | default app / show in file manager / viewer |
| `open_terminal_at(path)` / `open_in_editor(path)` | the Files page context menu |
| `desktop_dir()` | where "the Desktop" is |

## Adding or finishing an OS

1. Subclass `PlatformBackend` (see `linux.py` for a compact example) and override what the OS can do.
2. Register it in `backends/__init__.py::get_backend()` with a plain `from ... import ...` (not `importlib`
   with a computed name, so PyInstaller can see it).
3. Copy the `LinuxBackendTests` pattern: a `FakeShell` and assertions on the exact argv lists.

## Still Windows-specific (not behind the interface yet)

Voice (`voice/`: edge-tts + SAPI fallback), system volume (`pycaw`), now-playing (`winsdk`), global hotkeys
(`keyboard`), the Tesseract/OCR bundle location, "Open With..." and the About-page link buttons in
`ui/main_window.py` (`os.system("start ...")`, `rundll32`), and the media-key skills in `brain/groq_core.py`.
Wave will not run end-to-end on Linux/macOS until those are ported; the layer exists so that porting is a series of
small, testable steps rather than a rewrite.
