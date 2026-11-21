"""ParityMixin - the bridge methods that let the web UI do everything the
PyQt6 desktop UI (ui/main_window.py + ui/*_panel.py) does.

WaveWebBridge (webui/bridge.py) inherits this. Every public method here is
exposed to the page as window.pywebview.api.<name>() and, like the rest of
the bridge, returns a JSON-serialisable {"ok": ...} dict and never raises.

Each method calls the SAME backend function the matching desktop widget
calls (db.*, AppController.*, FileManager.*, CalendarManager.*, the skill
loader, the remote dashboard, ...) - there is no second implementation of
any feature here, only the glue the desktop UI keeps in its Qt slots.

Nothing in this module imports PyQt6 (nothing in webui/ does).
"""
import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta

import psutil

from settings.config import config
from database.db_manager import db
from automation.app_control import AppController
from automation.file_manager import FileManager
from automation.calendar_manager import CalendarManager
from automation import audit as audit_mod
from automation import security_log
from automation import undo as undo_mod
from monitoring.sys_monitor import SystemMonitor
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr as ocr_engine
import voice.tts as _tts_module
from voice.tts import tts_engine
from voice import speech_filter
from avatar import rig as avatar_rig
from avatar import live as avatar_live

logger = logging.getLogger("WebUI")

SETTINGS_FILE_NAME = "wave_settings.json"


def _ok(**kwargs):
    kwargs["ok"] = True
    return kwargs


def _err(message, **extra):
    out = {"ok": False, "error": str(message)}
    out.update(extra)
    return out


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"{fn.__name__} failed: {e}", exc_info=True)
            return _err(str(e))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---- optional dependencies (same HAS_X pattern as ui/common.py) ------------
try:
    import pyperclip
    HAS_PYPERCLIP = True
except Exception:  # pragma: no cover - depends on the machine
    pyperclip = None
    HAS_PYPERCLIP = False

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except Exception:  # pragma: no cover
    PdfReader = None
    HAS_PYPDF = False

try:
    import screen_brightness_control as sbc
    HAS_SBC = True
except Exception:  # pragma: no cover
    sbc = None
    HAS_SBC = False

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities
    HAS_PYCAW = True
except Exception:  # pragma: no cover
    comtypes = None
    AudioUtilities = None
    HAS_PYCAW = False

try:
    from monitoring.media_info import get_now_playing, seek_to, HAS_WINSDK
except Exception:  # pragma: no cover
    get_now_playing = None
    seek_to = None
    HAS_WINSDK = False

try:
    import keyboard as _keyboard
    HAS_KEYBOARD = True
except Exception:  # pragma: no cover
    _keyboard = None
    HAS_KEYBOARD = False

try:
    import markdown as markdown_lib
    HAS_MARKDOWN = True
except Exception:  # pragma: no cover
    markdown_lib = None
    HAS_MARKDOWN = False

try:
    import pygments  # noqa: F401
    HAS_PYGMENTS = True
except Exception:  # pragma: no cover
    HAS_PYGMENTS = False

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
ATTACH_FILE_TYPES = (
    "Attach a file (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp;*.pdf;*.txt;*.md;*.csv;*.json;*.log;*.py;*.js;*.ts;*.jsx;*.tsx;*.html;*.css;*.xml;*.yaml;*.yml;*.java;*.c;*.cpp)",
    "Images (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp)",
    "All files (*.*)",
)
# ui/main_window.py _run_action() targets the web UI may trigger, by name.
RUN_ACTIONS = {
    "open_app": AppController.open_app,
    "take_screenshot": AppController.take_screenshot,
    "delete_latest_screenshot": AppController.delete_latest_screenshot,
    "lock_pc": AppController.lock_pc,
    "open_terminal_at": AppController.open_terminal_at,
    "open_in_vscode": AppController.open_in_vscode,
    "create_folder": FileManager.create_folder,
    "create_file": FileManager.create_file,
}
MEDIA_KEYS = {"playpause", "nexttrack", "prevtrack", "volumemute", "volumeup", "volumedown"}
_PROACTIVE_HEALTH_LEVELS = ("ok", "recovered", "down")


def render_markdown_html(text: str) -> str:
    """ChatBubble._format_message()'s intended rendering: python-markdown
    with fenced code, tables, nl2br and sane lists, code blocks highlighted
    by Pygments (monokai, inline styles). Raw HTML in the source is escaped
    (the html_block preprocessor and inline html pattern are removed), so a
    reply can never inject markup; the page sanitises the result again."""
    text = text or ""
    if HAS_MARKDOWN:
        try:
            extensions = ["fenced_code", "tables", "nl2br", "sane_lists"]
            configs = {}
            if HAS_PYGMENTS:
                extensions.append("codehilite")
                configs["codehilite"] = {"noclasses": True, "pygments_style": "monokai", "guess_lang": False}
            md = markdown_lib.Markdown(extensions=extensions, extension_configs=configs)
            for name in ("html_block",):
                if name in md.preprocessors:
                    md.preprocessors.deregister(name)
            for name in ("html",):
                if name in md.inlinePatterns:
                    md.inlinePatterns.deregister(name)
            return md.convert(text)
        except Exception as e:
            logger.debug(f"markdown render failed, falling back to plain text: {e}")
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\n", "<br>")


def _fmt_size(n):
    if n is None:
        return ""
    units = ["bytes", "KB", "MB", "GB", "TB"]
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{int(size)} {units[i]}" if i == 0 else f"{size:.1f} {units[i]}" if size < 10 else f"{size:.0f} {units[i]}"


def _fmt_mtime(ts):
    try:
        d = datetime.fromtimestamp(ts)
        hour = d.hour % 12 or 12
        return f"{d.month}/{d.day}/{d.year} {hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    except Exception:
        return ""


class ParityMixin:
    """Mixed into WaveWebBridge - see this module's docstring."""

    # ------------------------------------------------------------ internals
    def _parity_init(self):
        self._services_started = False
        self._clip_thread = None
        self._clip_enabled = True
        self._clip_stop = threading.Event()
        self._tts_lock = threading.Lock()
        self._hotkeys_registered = False

    def _settings_path(self):
        import webui.bridge as bridge_mod
        return bridge_mod.SETTINGS_FILE

    def _read_settings(self):
        path = self._settings_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _dialog(self, kind, file_types=(), save_filename="", directory=""):
        """Native file dialog through pywebview (QFileDialog's role on the
        desktop). Returns a path string or None."""
        win = getattr(self, "_window", None)
        if win is None:
            return None
        import webview
        dialog_enum = getattr(webview, "FileDialog", None)
        if dialog_enum is not None:
            dtype = dialog_enum.SAVE if kind == "save" else dialog_enum.OPEN
        else:  # older pywebview
            dtype = webview.SAVE_DIALOG if kind == "save" else webview.OPEN_DIALOG
        kwargs = {"dialog_type": dtype, "directory": directory or "", "file_types": tuple(file_types)}
        if kind == "save":
            kwargs["save_filename"] = save_filename or ""
        result = win.create_file_dialog(**kwargs)
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return str(result)

    # ------------------------------------------------------------- bootstrap
    @_safe
    def get_capabilities(self):
        HAS_PIPER = bool(getattr(_tts_module, "HAS_PIPER", False))
        try:
            from remote.qr import HAS_QRCODE
        except Exception:
            HAS_QRCODE = False
        return _ok(winsdk=bool(HAS_WINSDK), pycaw=bool(HAS_PYCAW), sbc=bool(HAS_SBC), pyperclip=bool(HAS_PYPERCLIP),
                   pypdf=bool(HAS_PYPDF), qrcode=bool(HAS_QRCODE), piper=bool(HAS_PIPER), keyboard=bool(HAS_KEYBOARD),
                   usage_stats_enabled=bool(getattr(config, "USAGE_STATS_ENABLED", False)),
                   redact_secrets=bool(getattr(config, "REDACT_SECRETS_ENABLED", True)))

    def _custom_themes(self):
        themes_dir = os.path.join(config.DATA_DIR, "themes")
        out = []
        try:
            for fname in sorted(os.listdir(themes_dir)):
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(themes_dir, fname), "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict) and data.get("name"):
                            out.append(data)
                    except Exception as e:
                        logger.warning(f"Failed to load theme {fname}: {e}")
        except FileNotFoundError:
            pass
        return out

    @staticmethod
    def _local_ip():
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "Unavailable"

    @_safe
    def get_settings_info(self):
        path = self._settings_path()
        return _ok(settings=self._read_settings(), exists=os.path.exists(path))

    # --------------------------------------------------------------- chat
    @_safe
    def render_markdown(self, text: str):
        return _ok(html=render_markdown_html(text))

    @_safe
    def speech_text(self, text: str):
        """MainWindow._plain_text_for_speech(): what of a finished reply gets
        read aloud (no code blocks, no table pipes)."""
        return _ok(text=speech_filter.for_speech(text or ""))

    def _speak_with_events(self, text):
        """TTSWorker: speak a finished string, telling the page when it
        starts/stops so the visualizer + status line follow it."""
        self._push("tts_started", {})
        success = False
        try:
            # Tier 29: SPEAKING once the audio really starts (bridge's
            # _on_tts_audio_start), not while it's still being synthesised.
            pending = getattr(self, "_avatar_speech_pending", None)
            if pending is not None:
                pending()
            else:
                self._avatar_state.on_speech_started()
                self._push_avatar_state()
            success = bool(tts_engine.speak_and_wait(text))
        except Exception as e:
            logger.error(f"TTS Error: {e}")
            success = False
        finally:
            self._speech_pending = False
            self._avatar_state.on_speech_stopped()
            self._push_avatar_state()
            self._push("tts_finished", {"success": success})
        return success

    @_safe
    def speak(self, text: str):
        if not (text or "").strip():
            return _ok(started=False)
        threading.Thread(target=self._speak_with_events, args=(text,), name="WebTTS", daemon=True).start()
        return _ok(started=True)

    @_safe
    def stop_speaking(self):
        tts_engine.stop()
        return _ok()

    @_safe
    def copy_to_clipboard(self, text: str):
        if not HAS_PYPERCLIP:
            return _err("pyperclip not installed")
        pyperclip.copy(text or "")
        return _ok()

    @_safe
    def export_chat(self, content: str):
        path = self._dialog("save", ("Text Files (*.txt)",), save_filename="wave_chat.txt")
        if not path:
            return _ok(saved=False)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        return _ok(saved=True, path=path)

    @_safe
    def pick_attachment(self):
        """MainWindow._pick_attachment(): images are sent as a path (plus a
        small preview for the chip/bubble), PDFs and text files as their
        extracted text, capped at 12000 characters."""
        path = self._dialog("open", ATTACH_FILE_TYPES)
        if not path:
            return _ok(attachment=None)
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        if ext in IMAGE_EXTS:
            thumb = None
            try:
                from PIL import Image
                import io
                img = Image.open(path)
                img.thumbnail((260, 260))
                buf = io.BytesIO()
                img.convert("RGBA").save(buf, format="PNG")
                thumb = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                return _err("Couldn't open that image", level="error")
            return _ok(attachment={"kind": "image", "path": path, "name": name, "size_bytes": size_bytes,
                                   "text_content": None, "truncated": False, "thumb": thumb})
        if ext == ".pdf":
            if not HAS_PYPDF:
                return _err("PDF support needs: pip install pypdf", level="warning")
            try:
                reader = PdfReader(path)
                text_content = "\n".join((pg.extract_text() or "") for pg in reader.pages[:40]).strip()
            except Exception as e:
                return _err(f"Couldn't read that PDF: {e}", level="error")
            if not text_content:
                return _err("No readable text found in that PDF (it may be scanned).", level="warning")
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text_content = f.read()
            except (UnicodeDecodeError, OSError):
                return _err("That file type isn't supported yet - try an image, PDF, or text file.", level="warning")
        truncated = len(text_content) > 12000
        return _ok(attachment={"kind": "document", "path": path, "name": name, "size_bytes": size_bytes,
                               "text_content": text_content[:12000], "truncated": truncated})

    @_safe
    def open_url(self, url: str):
        if not re.match(r"^https?://", url or "", re.I):
            return _err("Only http(s) links can be opened.")
        webbrowser.open(url)
        return _ok()

    # ------------------------------------------------------ voice / mic
    @_safe
    def turn_on_mic(self):
        """MainWindow.turn_on_mic(): the continuous mic worker runs for the
        whole session, starting SLEEPING (waiting for the wake word)."""
        worker = getattr(self, "_mic_worker", None)
        if worker is not None and worker.is_alive():
            return _ok(listening=True, mode=worker.mode)
        self._start_mic_worker()
        return _ok(listening=True, mode="SLEEPING")

    @_safe
    def toggle_listening(self):
        """MainWindow.toggle_mic(): flips the running worker AWAKE <-> SLEEPING."""
        worker = getattr(self, "_mic_worker", None)
        if worker is None or not worker.is_alive():
            self._start_mic_worker()
            worker = self._mic_worker
        worker.set_mode("SLEEPING" if worker.mode == "AWAKE" else "AWAKE")
        return _ok(mode=worker.mode)

    @_safe
    def set_listening(self, awake: bool):
        worker = getattr(self, "_mic_worker", None)
        if worker is None or not worker.is_alive():
            return _ok(mode="OFF")
        worker.set_mode("AWAKE" if awake else "SLEEPING")
        return _ok(mode=worker.mode)

    def _on_barge_in(self):
        """MainWindow._on_barge_in()."""
        self.cancel_reply()
        try:
            tts_engine.stop()
        except Exception:
            pass
        self._push("voice_interrupt", {})

    def _on_voice_stop(self):
        """MainWindow._on_voice_stop(): end the turn in flight and drop a
        pending yes/no confirmation."""
        from telemetry import ttfa
        had_reply = bool(self.cancel_reply().get("cancelled"))
        was_speaking = bool(getattr(tts_engine, "is_speaking", False))
        try:
            tts_engine.stop()
        except Exception:
            pass
        try:
            ttfa.tracker.mark("audio_stopped")
        except Exception:
            pass
        cancelled_confirmation = False
        cancel = getattr(self.brain, "cancel_pending_confirmation", None)
        if callable(cancel):
            try:
                cancelled_confirmation = bool(cancel())
            except Exception as e:
                logger.warning(f"Couldn't cancel a pending confirmation: {e}")
        try:
            if ttfa.tracker.active_source() == "stop":
                ttfa.tracker.finish("ok", stopped_reply=had_reply or None, was_speaking=was_speaking or None)
        except Exception:
            pass
        self._push("voice_stop", {"cancelled_confirmation": cancelled_confirmation,
                                  "had_activity": had_reply or was_speaking})

    @_safe
    def emergency_stop(self):
        """MainWindow.emergency_stop() (Ctrl+Alt+X)."""
        self.cancel_reply()
        try:
            tts_engine.stop()
        except Exception:
            pass
        worker = getattr(self, "_mic_worker", None)
        if worker is not None and worker.is_alive():
            worker.set_mode("SLEEPING")
        logger.info("Emergency stop triggered (Ctrl+Alt+X) - all in-flight actions halted.")
        return _ok()

    # --------------------------------------------------- actions / system
    @_safe
    def run_action(self, name: str, args=None):
        fn = RUN_ACTIONS.get(name)
        if fn is None:
            return _err(f"Unknown action: {name}")
        result = fn(*(args or []))
        return _ok(result=result if isinstance(result, str) and result else "Done.")

    @_safe
    def press_key(self, key: str):
        if key not in MEDIA_KEYS:
            return _err(f"Key not allowed: {key}")
        import pyautogui
        pyautogui.press(key)
        return _ok()

    @_safe
    def power_command(self, kind: str):
        """MainWindow._confirm_and_run(): the page already asked; runs the
        same 5-second-delayed shutdown/restart command."""
        command = {"restart": "shutdown /r /t 5", "shutdown": "shutdown /s /t 5"}.get(kind)
        if command is None:
            return _err(f"Unknown power command: {kind}")
        os.system(command)
        return _ok()

    @_safe
    def get_system_snapshot(self):
        stats = SystemMonitor.get_stats() or {}
        disk = None
        try:
            d = psutil.disk_usage(os.path.abspath(os.sep))
            disk = {"percent": d.percent, "free_gb": d.free / (1024 ** 3)}
        except Exception:
            pass
        net = None
        try:
            io = psutil.net_io_counters()
            net = {"recv": io.bytes_recv, "sent": io.bytes_sent}
        except Exception:
            pass
        return _ok(stats=stats, disk=disk, net=net)

    @_safe
    def get_hardware_info(self):
        return _ok(text=SystemMonitor.get_hardware_info())

    @_safe
    def get_top_processes(self, n: int = 12):
        procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
        for p in procs:
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        time.sleep(0.2)
        rows = []
        for p in procs:
            try:
                cpu = p.cpu_percent(None)
                mem = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0) / (1024 * 1024)
                name = p.info.get("name") or "?"
                if p.info.get("pid") == 0:
                    continue
                rows.append({"pid": p.info.get("pid"), "name": name, "cpu": cpu, "memory_mb": mem})
            except Exception:
                continue
        rows.sort(key=lambda r: (r["cpu"], r["memory_mb"]), reverse=True)
        return _ok(processes=rows[:max(1, min(40, int(n or 12)))])

    # ---------------------------------------------------------------- music
    @_safe
    def get_now_playing(self):
        if get_now_playing is None:
            return _ok(info={})
        return _ok(info=get_now_playing() or {})

    @_safe
    def seek_media(self, seconds: float):
        if seek_to is None:
            return _err("winsdk isn't installed")
        seek_to(float(seconds))
        return _ok()

    @_safe
    def get_volume_percent(self):
        if not HAS_PYCAW:
            return _ok(percent=None)
        comtypes.CoInitialize()
        volume = AudioUtilities.GetSpeakers().EndpointVolume
        return _ok(percent=round(volume.GetMasterVolumeLevelScalar() * 100))

    @_safe
    def set_volume_percent(self, percent: int):
        if not HAS_PYCAW:
            return _ok(set=False)
        comtypes.CoInitialize()
        volume = AudioUtilities.GetSpeakers().EndpointVolume
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(percent) / 100.0)), None)
        return _ok(set=True)

    @_safe
    def get_brightness_percent(self):
        if not HAS_SBC:
            return _ok(percent=None)
        try:
            return _ok(percent=int(sbc.get_brightness(display=0)[0]))
        except Exception:
            return _ok(percent=None)

    @_safe
    def set_brightness_percent(self, percent: int):
        if not HAS_SBC:
            return _ok(set=False)
        sbc.set_brightness(int(percent))
        return _ok(set=True)

    # ---------------------------------------------------------------- files
    @_safe
    def list_dir(self, path: str = None):
        """The Files page's tree (QFileSystemModel columns: Name, Size, Type,
        Date Modified). An empty path lists the drives, like its root."""
        target = path if path is not None else FileManager.get_default_path()
        entries = []
        if not target or not os.path.isdir(target):
            # QFileSystemModel shows its root (the drives) for a path that
            # doesn't exist - e.g. a Desktop that OneDrive moved - while the
            # desktop's path label still reads the path it was given.
            for part in psutil.disk_partitions(all=False):
                label = part.device.rstrip("\\/")
                entries.append({"name": f"{label}", "path": part.mountpoint, "is_dir": True, "kind": "drive",
                                "size_text": "", "type": "Drive", "modified": ""})
            return _ok(path=target or "", entries=entries)
        with os.scandir(target) as it:
            items = list(it)
        dirs = [e for e in items if e.is_dir(follow_symlinks=False)]
        files_ = [e for e in items if not e.is_dir(follow_symlinks=False)]
        for e in sorted(dirs, key=lambda x: x.name.lower()) + sorted(files_, key=lambda x: x.name.lower()):
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                st = None
            is_dir = e.is_dir(follow_symlinks=False)
            ext = os.path.splitext(e.name)[1].lstrip(".")
            entries.append({
                "name": e.name, "path": e.path, "is_dir": is_dir, "kind": "dir" if is_dir else "file",
                "size_text": "" if is_dir else _fmt_size(st.st_size if st else None),
                "type": "File Folder" if is_dir else (f"{ext.upper()} File" if ext else "File"),
                "modified": _fmt_mtime(st.st_mtime) if st else "",
            })
        return _ok(path=os.path.abspath(target), entries=entries)

    @_safe
    def open_path(self, path: str):
        AppController.open_path(path)
        return _ok()

    @_safe
    def open_with(self, path: str):
        if sys.platform.startswith("win"):
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path])
            return _ok()
        return _err("'Open With' is only available on Windows.")

    @_safe
    def delete_path(self, path: str):
        """MainWindow._delete_file(): folders are removed for good (audited);
        files go to Wave's recoverable trash."""
        if os.path.isdir(path):
            with audit_mod.audit.action("delete_folder", {"folder_name": os.path.basename(path),
                                                          "path": os.path.dirname(path)}, source="ui") as act:
                shutil.rmtree(path)
                act.ok(f"Deleted folder '{os.path.basename(path)}' (permanently).")
            return _ok(message="Folder deleted (folders can't be undone)", level="success")
        msg = FileManager.delete_file(os.path.basename(path), os.path.dirname(path))
        if str(msg).startswith("Deleted"):
            return _ok(message="Deleted - you can undo it from Activity", level="success")
        return _ok(message=msg, level="error")

    # --------------------------------------------------------------- vision
    @_safe
    def ocr_capture(self):
        """MainWindow.run_ocr_capture(): minimise, capture + OCR, restore."""
        win = getattr(self, "_window", None)
        if win is not None:
            try:
                win.minimize()
            except Exception:
                pass
            time.sleep(0.35)
        try:
            path = VisionCapture.take_screenshot()
            text = ocr_engine.extract_text(path) if path else "Failed to capture the screen."
        except Exception as e:
            text = f"Error reading screen text: {e}"
        if win is not None:
            try:
                win.restore()
                win.show()
            except Exception:
                pass
        return _ok(text=text)

    # ------------------------------------------------------------- activity
    @_safe
    def get_activity(self, limit: int = 60):
        rows = audit_mod.audit.recent(limit)
        actions = [{"id": r.id, "tool": r.tool, "summary": r.summary(), "status": r.status, "result": r.result or "",
                    "source": r.source, "time": r.time_text, "reversible": r.reversible,
                    "undone_at": r.undone_at, "undo_label": r.undo_label} for r in (rows or [])]
        if not undo_mod.trash_enabled():
            note = "Undo for deleted files is switched off (UNDO_TRASH_ENABLED=false in .env), so deletes are permanent."
        else:
            mb = undo_mod.trash.size_bytes() / (1024 * 1024)
            days = undo_mod.trash.retention_days
            note = (f"Deleted files wait in Wave's trash for {days} day{'s' if days != 1 else ''} "
                    f"before they're gone for good ({mb:.1f} MB there now).")
        return _ok(actions=actions, trash_note=note)

    @_safe
    def empty_trash(self):
        return _ok(count=undo_mod.trash.empty())

    # ------------------------------------------------------------ workflows
    @_safe
    def save_workflow(self, name: str, steps: list):
        return _ok(saved=bool(db.save_workflow(name, list(steps or []))))

    @_safe
    def rename_workflow(self, old: str, new: str):
        return _ok(renamed=bool(db.rename_workflow(old, new)))

    @_safe
    def run_workflow_ui(self, name: str):
        """WorkflowsPanel._run_workflow(): through _dispatch_tool so it gets
        the same audit row and confirm gate as a voice/chat run."""
        return _ok(result=str(self.brain._dispatch_tool("run_workflow", {"name": name}, "ui")))

    # --------------------------------------------------------------- memory
    @_safe
    def save_memory(self, key: str, value: str):
        return _ok(saved=bool(db.save_memory(key, value, source="explicit")))

    @_safe
    def export_memory(self):
        rows = []
        for key, value in sorted(db.get_all_memory() or []):
            prov = db.get_memory_provenance(key) or {}
            rows.append({"key": key, "value": value, "source": prov.get("source"), "updated_at": prov.get("updated_at")})
        path = self._dialog("save", ("JSON (*.json)",), save_filename="wave_memory_export.json")
        if not path:
            return _ok(saved=False)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        return _ok(saved=True, count=len(rows), path=path)

    # ------------------------------------------------------------ reminders
    @_safe
    def get_reminder_rows(self):
        rows = []
        for reminder_id, message, due_at, recurrence in db.get_upcoming_reminders() or []:
            try:
                when = datetime.fromisoformat(due_at).strftime("%I:%M %p on %b %d")
            except Exception:
                when = due_at
            suffix = " (repeats daily)" if recurrence == "daily" else ""
            rows.append({"id": reminder_id, "label": f"{message} — {when}{suffix}"})
        return _ok(rows=rows)

    @_safe
    def cancel_reminder_id(self, reminder_id: int):
        return _ok(cancelled=bool(db.cancel_reminder_by_id(int(reminder_id))))

    @_safe
    def add_reminder(self, message: str, when_text: str, recurrence: str = "none"):
        return _ok(result=self.brain._set_reminder(message or "Reminder", when_text, recurrence or "none"))

    # -------------------------------------------------------- notes & todos
    @_safe
    def add_todo_raw(self, text: str):
        return _ok(added=db.add_todo(text) is not None)

    @_safe
    def complete_todo_id(self, todo_id: int):
        return _ok(done=bool(db.complete_todo_by_id(int(todo_id))))

    @_safe
    def delete_todo_id(self, todo_id: int):
        return _ok(deleted=bool(db.delete_todo_by_id(int(todo_id))))

    @_safe
    def add_note_raw(self, text: str):
        return _ok(added=bool(db.add_quick_note(text)))

    @_safe
    def delete_note_id(self, note_id: int):
        return _ok(deleted=bool(db.delete_quick_note_by_id(int(note_id))))

    # ------------------------------------------------------------- calendar
    @_safe
    def get_calendar_rows(self, days: int = 60):
        now = datetime.now()
        rows = []
        for event_id, title, start_at, end_at, location, notes, recurrence in \
                db.get_calendar_events_between(now.isoformat(), (now + timedelta(days=int(days))).isoformat()) or []:
            try:
                when = datetime.fromisoformat(start_at).strftime("%a %b %d, %I:%M %p")
            except Exception:
                when = start_at
            suffix = f" ({recurrence})" if recurrence else ""
            loc = f" @ {location}" if location else ""
            rows.append({"id": event_id, "label": f"{title} — {when}{loc}{suffix}"})
        return _ok(rows=rows)

    @_safe
    def cancel_event_id(self, event_id: int):
        return _ok(cancelled=bool(db.cancel_calendar_event_by_id(int(event_id))))

    @_safe
    def add_ics_feed(self, name: str, url: str):
        from automation.calendar_sync import CalendarSync
        return _ok(message=CalendarSync.add_ics_feed(name, url))

    # --------------------------------------------------------------- skills
    @_safe
    def get_skills_catalog(self):
        from brain.skills_registry import skills as skill_registry
        entries = sorted(skill_registry.describe(), key=lambda e: e["name"])
        return _ok(entries=[{"name": e["name"], "source": e.get("source", ""), "description": e.get("description", "")}
                            for e in entries])

    @_safe
    def get_skill_status(self):
        from brain.skill_loader import get_loader
        st = get_loader().status() or {}
        return _ok(status={"directory": str(st.get("directory", "?")), "watching": bool(st.get("watching")),
                           "loaded": {k: list(v or []) for k, v in (st.get("loaded") or {}).items()},
                           "errors": {k: str(v) for k, v in (st.get("errors") or {}).items()},
                           "disabled": list(st.get("disabled") or [])})

    @_safe
    def rescan_skills(self):
        from brain.skill_loader import get_loader
        return _ok(events=len(get_loader().load_now() or []))

    @_safe
    def set_skill_file_disabled(self, filename: str, disabled: bool):
        from brain.skill_loader import get_loader
        get_loader().set_disabled(filename, bool(disabled))
        return _ok()

    # ----------------------------------------------------------- performance
    @_safe
    def get_performance_report(self):
        from telemetry.ttfa_report import load_rows, summarize, by_language, _fmt
        from telemetry import usage_stats
        try:
            rows = load_rows()
        except Exception as e:
            return _ok(paths=[], languages=[], empty_text=f"Couldn't read the TTFA log: {e}", usage_text=self._usage_text())
        paths = []
        for s in (summarize(rows) if rows else []):
            met = ""
            if s["target_ms"] is not None and s["p50_ms"] is not None:
                met = "MET" if s["p50_ms"] <= s["target_ms"] else "NOT MET"
            paths.append([s["path"], str(s["n"]), _fmt(s["p50_ms"]), _fmt(s["p90_ms"]), _fmt(s["p50_first_token_ms"]), met])
        langs = [[l["language"], str(l["n"]), _fmt(l["p50_ms"])] for l in (by_language(rows) if rows else [])]
        if not rows:
            empty = ("No voice turns logged yet. Talk to Wave with the mic on (TTFA_ENABLED=true in settings), "
                     "then come back and Refresh.")
        else:
            empty = ("TTFA = time from the end of your speech to Wave's first audio. "
                     "Fast-path target: p50 ≤ 0.8s. LLM answers: p50 ≤ 2.0s.")
        return _ok(paths=paths, languages=langs, empty_text=empty, usage_text=self._usage_text())

    @staticmethod
    def _usage_text():
        from telemetry import usage_stats
        try:
            stats = usage_stats.summarize()
        except Exception as e:
            return f"Couldn't read usage stats: {e}"
        if not stats.get("enabled"):
            return ("Off by default. Turn on \"Show my local usage stats\" in Settings > Privacy "
                    "to see how you've been using Wave - voice vs. text, your most-used skills, "
                    "success rate. It's computed from data already on this device and is never sent anywhere.")
        if not stats.get("total_turns"):
            return f"No activity logged in the last {stats.get('days', 7)} day(s) yet."
        by_source = ", ".join(f"{k}: {v}" for k, v in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]))
        top = ", ".join(f"{name} ({n})" for name, n in stats["top_skills"][:5]) or "none yet"
        rate = stats["success_rate"]
        rate_text = f"{rate * 100:.0f}%" if rate is not None else "n/a"
        return (f"Last {stats['days']} day(s): {stats['total_turns']} turns ({by_source}). "
                f"Success rate: {rate_text}. Top skills: {top}.")

    # -------------------------------------------------------------- settings
    @_safe
    def apply_setting(self, key: str, value):
        """The immediate effect of one Settings control - the same thing the
        matching SettingsPanel slot does on the desktop (nothing is written
        to disk until "Save Settings", exactly like the desktop)."""
        if key == "clipboard_monitoring":
            self._clip_enabled = bool(value)
        elif key == "screen_capture_allowed":
            VisionCapture.capture_enabled = bool(value)
        elif key == "save_chat_history":
            db.history_enabled = bool(value)
        elif key == "usage_stats_enabled":
            config.USAGE_STATS_ENABLED = bool(value)
        elif key == "redact_secrets":
            config.REDACT_SECRETS_ENABLED = bool(value)
        elif key == "tts_voice":
            tts_engine.set_voice(str(value))
        elif key == "tts_rate":
            tts_engine.set_rate(f"{int(value) - 100:+d}%")
        elif key == "tts_volume":
            tts_engine.set_volume(int(value) / 100.0)
        elif key == "wake_phrases":
            phrases = [str(p) for p in (value or [])] or ["hey wave", "wake up"]
            self._wake_phrases = phrases
            worker = getattr(self, "_mic_worker", None)
            if worker is not None:
                worker.set_wake_phrases(phrases)
        elif key == "wake_engine":
            from voice.wakeword import get_wake_gate, reset_wake_gate
            config.WAKE_WORD_ENGINE = str(value or "auto")
            threshold = getattr(self, "_wake_threshold", None)
            reset_wake_gate()
            gate = get_wake_gate()
            if threshold is not None:
                gate.threshold = threshold
            worker = getattr(self, "_mic_worker", None)
            if worker is not None:
                worker.wake_gate = gate
        elif key == "wake_sensitivity":
            from voice.wakeword import get_wake_gate
            gate = get_wake_gate()
            gate.threshold = round(1.0 - int(value) / 100.0, 2)
            self._wake_threshold = gate.threshold
        elif key == "stt_lang":
            # Imported only here: voice.stt pulls in the whole audio stack,
            # and a privacy toggle must never fail because that stack can't load.
            from voice.stt import stt_engine
            stt_engine.set_language(str(value))
        elif key == "tts_local_auto_download":
            config.TTS_LOCAL_AUTO_DOWNLOAD = bool(value)
        else:
            return _err(f"Unknown setting: {key}")
        return _ok()

    def _wake_status_text(self):
        """SettingsPanel.wake_status_text()."""
        from voice.wakeword import get_wake_gate
        try:
            gate = get_wake_gate()
            phrases = list(getattr(self, "_wake_phrases", []) or [])
            engine = getattr(gate, "engine", "auto")
            if engine == "transcript":
                return "Speech recognition listens for your wake phrase (no on-device model)."
            if not gate.wanted():
                return ("No on-device wake model found - speech recognition is used. Install "
                        "requirements-voice-local.txt to use Wave's built-in 'Hey Wave' model.")
            from voice.commands import normalize_utterance
            if engine == "auto" and normalize_utterance(gate.phrase) not in {normalize_utterance(p) for p in phrases}:
                return (f"The on-device model listens for '{gate.phrase.title()}' - pick that wake word "
                        f"to use it; '{(phrases or ['?'])[0]}' uses speech recognition.")
            if getattr(gate, "_load_failed", False):
                return ("The on-device model couldn't load (is openwakeword installed, and were its "
                        "feature files downloaded?) - speech recognition is used instead.")
            state = "active" if getattr(gate, "_model", None) is not None else "ready (starts when Wave sleeps)"
            check = (" Every detection is double-checked against what was said."
                     if float(getattr(gate, "confirm_below", 0.0) or 0.0) >= 1.0 else "")
            return (f"On-device '{gate.phrase.title()}' model {state}, threshold {gate.threshold:.2f}. "
                    "While asleep nothing is transcribed until it hears the wake word." + check)
        except Exception as e:
            return f"Wake word status unavailable: {e}"

    @_safe
    def get_wake_status(self):
        return _ok(text=self._wake_status_text())

    @_safe
    def get_voice_settings(self):
        from voice.stt import stt_engine
        from voice.wakeword import get_wake_gate
        HAS_PIPER = bool(getattr(_tts_module, "HAS_PIPER", False))
        try:
            rate = 100 + int(str(tts_engine.rate).replace("%", ""))
        except (ValueError, TypeError):
            rate = 108
        try:
            sens = int(round((1.0 - get_wake_gate().threshold) * 100))
        except Exception:
            sens = 50
        if not HAS_PIPER:
            local_status = "piper-tts isn't installed - this stays off until it is (`pip install piper-tts`)."
        elif getattr(tts_engine, "_piper_voice", None) is not None:
            local_status = "A local Piper voice is already loaded."
        elif (getattr(config, "TTS_LOCAL_MODEL_PATH", "") or "").strip():
            local_status = f"Using the voice configured at {config.TTS_LOCAL_MODEL_PATH}."
        else:
            local_status = "No local voice downloaded yet - WAVE will fetch one the first time it's needed."
        return _ok(voice=getattr(tts_engine, "voice", ""), rate_slider=rate,
                   volume_slider=int(getattr(tts_engine, "volume_level", 1.0) * 100),
                   wake_engine=(getattr(config, "WAKE_WORD_ENGINE", "auto") or "auto").lower(),
                   wake_sensitivity=sens, stt_language=getattr(stt_engine, "language", "en-IN"),
                   local_voice=bool(getattr(config, "TTS_LOCAL_AUTO_DOWNLOAD", False)),
                   local_voice_status=local_status, wake_status=self._wake_status_text())

    @_safe
    def save_custom_theme(self, theme: dict):
        """SettingsPanel._save_custom_theme(): the name comes from the file
        the user picks; the theme itself is written to <DATA_DIR>/themes."""
        path = self._dialog("save", ("JSON Files (*.json)",), save_filename=f"{(theme or {}).get('name', 'Custom')}.json")
        if not path:
            return _ok(name=None)
        name = os.path.basename(path)
        name = name[:-5] if name.lower().endswith(".json") else name
        data = dict(theme or {})
        data["name"] = name
        themes_dir = os.path.join(config.DATA_DIR, "themes")
        os.makedirs(themes_dir, exist_ok=True)
        with open(os.path.join(themes_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return _ok(name=name)

    @_safe
    def pick_file(self, kind: str):
        types = {"photo": ("Images (*.png;*.jpg;*.jpeg;*.bmp;*.webp)",),
                 "voice": ("Audio (*.wav;*.mp3;*.m4a;*.flac;*.ogg)",)}.get(kind, ("All files (*.*)",))
        return _ok(path=self._dialog("open", types))

    @_safe
    def build_avatar(self, photo_path: str, voice_sample_path: str = ""):
        """MainWindow.build_avatar_from_files()."""
        import webui.bridge as bridge_mod
        try:
            avatar_rig.build_rig(photo_path, bridge_mod.AVATAR_RIG_DIR)
        except Exception as e:
            return _ok(result=f"Avatar nahi ban paya: {e}")
        voice_sample_path = (voice_sample_path or "").strip()
        note = ""
        if voice_sample_path:
            if os.path.exists(voice_sample_path):
                config.TTS_CLONE_REFERENCE_PATH = voice_sample_path
                tts_engine.reset_clone()   # Tier 31: stops the old sample's voice process
                self.save_settings({"avatar_voice_reference_path": voice_sample_path})
                tts_engine.warm_up_clone()
                note = " Aapki awaz clone hone ke liye set ho gayi hai."
            else:
                note = f" (Voice sample '{voice_sample_path}' nahi mili - awaz clone nahi hui.)"
        # Tier 27: a new photo makes the old lifelike clips stale - render
        # fresh ones right away when LivePortrait is set up.
        render_live = getattr(self, "render_avatar_live", None)
        if render_live is not None and not avatar_live.renderer_problem():
            r = render_live()
            if r.get("ok") and r.get("started"):
                note += " Lifelike animation ban rahi hai (kuch minute) - Dodo page par apne aap aa jayegi."
        return _ok(result="Avatar ban gaya aur dikhayi de raha hai." + note)

    @_safe
    def clear_all_data(self):
        """SettingsPanel._clear_all_data()."""
        path = self._settings_path()
        if os.path.exists(path):
            os.remove(path)
        try:
            undo_mod.trash.empty()
            audit_mod.audit.clear()
            security_log.clear()
        except Exception as e:
            logger.error(f"Couldn't clear trash/activity: {e}")
        return _ok(cleared=bool(db.clear_all()))

    @_safe
    def provider_info(self):
        info = self.brain.provider_info()
        line = f"{info['provider']} / {info['model']}  ·  {info['endpoint']}  ·  key {info['key']}"
        text = line if info["configured"] else f"{line}\nNot usable yet: {info['problem']}"
        return _ok(text=text, info=info)

    # ------------------------------------------------------------ onboarding
    @_safe
    def list_audio_devices(self):
        import voice.audio_devices as audio_devices
        return _ok(inputs=[list(d) for d in audio_devices.list_input_devices()],
                   outputs=[list(d) for d in audio_devices.list_output_devices()])

    @_safe
    def measure_input_level(self, device_index=None, seconds: float = 1.5):
        import voice.audio_devices as audio_devices
        return _ok(level=float(audio_devices.measure_input_level(device_index, seconds) or 0.0))

    @_safe
    def play_test_tone(self, device_index=None):
        import voice.audio_devices as audio_devices
        return _ok(played=bool(audio_devices.play_test_tone(device_index)))

    @_safe
    def recalibrate_mic(self):
        from voice.stt import stt_engine
        return _ok(result=stt_engine.recalibrate())

    # ----------------------------------------------------- background services
    @_safe
    def start_background_services(self):
        """MainWindow._start_background_workers() + the listeners init_ui()
        wires up (audit, proactive, thread supervisor, ollama, skills) +
        setup_global_hotkeys(). Idempotent."""
        if self._services_started:
            return _ok(started=False)
        self._services_started = True
        from automation.proactive import proactive_engine
        from brain.memory_consolidation import memory_consolidator
        from automation.thread_supervisor import thread_supervisor
        from brain.ollama_lifecycle import ollama_lifecycle
        from brain.skill_loader import get_loader

        audit_mod.audit.add_listener(self._on_audit_event)
        proactive_engine.add_listener(lambda nudge: self._on_proactive_nudge(nudge))
        proactive_engine.set_workflow_runner(self._run_scheduled_workflow)
        proactive_engine.start()
        memory_consolidator.start()
        thread_supervisor.add_listener(lambda name, event: self._push_thread_health())
        thread_supervisor.watch("proactive_engine", proactive_engine)
        thread_supervisor.watch("memory_consolidator", memory_consolidator)
        store = audit_mod.audit.store_for_supervision
        if store is not None:
            thread_supervisor.watch("audit_db_writer", store)
        thread_supervisor.start()
        ollama_lifecycle.add_listener(lambda event, message: self._push("ollama_event", {"event": event, "message": message}))
        ollama_lifecycle.ensure_ready()
        threading.Thread(target=self._run_health_check, name="WebHealthCheck", daemon=True).start()
        threading.Thread(target=self._fetch_weather, name="WebWeather", daemon=True).start()
        self._start_clipboard_monitor()

        def start_skills():
            try:
                loader = get_loader()
                loader.add_listener(lambda ev: self._push("skill_event", {"kind": ev.kind, "message": ev.message()}))
                loader.start(watch=config.SKILLS_HOT_RELOAD)
                if config.SKILLS_HOT_RELOAD:
                    thread_supervisor.watch("skill_loader", loader)
            except Exception as e:
                logger.error(f"Couldn't start the skills watcher: {e}")
        threading.Timer(1.5, start_skills).start()
        self._register_hotkeys()
        return _ok(started=True)

    def _on_audit_event(self, event, rec):
        if event == "started" and rec is not None:
            try:
                description = audit_mod.summarize(rec.tool, rec.args)
            except Exception:
                description = getattr(rec, "tool", "")
            self._push("tool_activity", {"description": description})
        else:
            self._push("audit_changed", {})

    def _on_proactive_nudge(self, nudge):
        self._push("proactive_nudge", {"kind": nudge.kind, "title": nudge.title, "message": nudge.message})

    def _run_scheduled_workflow(self, workflow_name):
        def run():
            try:
                result = self.brain._dispatch_tool("run_workflow", {"name": workflow_name}, "proactive")
            except Exception as e:
                result = f"Scheduled run of '{workflow_name}' failed: {e}"
            self._push("scheduled_workflow_result", {"text": str(result)})
        threading.Thread(target=run, name="WebScheduledWorkflow", daemon=True).start()

    def _push_thread_health(self):
        from automation.thread_supervisor import thread_supervisor
        status = thread_supervisor.status()
        gave_up = [n for n, i in status.items() if i["gave_up"]]
        recovered = [n for n, i in status.items() if not i["gave_up"] and i["restart_count"] > 0]
        if gave_up:
            level, text = "down", f"●  {len(gave_up)} WORKER{'S' if len(gave_up) != 1 else ''} DOWN"
            tip = "Stopped for good this session (hit the restart cap): " + ", ".join(gave_up)
            if recovered:
                tip += "\nRecovered after a restart: " + ", ".join(recovered)
        elif recovered:
            level, text = "recovered", "●  RECOVERED FROM A CRASH"
            tip = "Restarted after crashing, running fine now: " + ", ".join(recovered)
        else:
            level, text, tip = "ok", "●  SYSTEMS OK", "All background workers running normally."
        self._push("thread_health", {"level": level, "text": text, "tip": tip})

    def _run_health_check(self):
        try:
            from automation.health_check import run_health_check, Status
            results = run_health_check()
            out = []
            for r in results:
                status = "problem" if r.status is Status.PROBLEM else "warn" if r.status is Status.WARN else "ok"
                out.append({"name": r.name, "message": r.message, "status": status})
            self._push("health_check", {"results": out})
        except Exception as e:
            logger.warning(f"health check failed: {e}")

    def _fetch_weather(self):
        try:
            from web.web_ops import WebManager
            location = getattr(config, "WEATHER_DEFAULT_LOCATION", "") or None
            self._push("weather", {"text": WebManager.get_weather(location)})
        except Exception as e:
            logger.warning(f"weather fetch failed: {e}")

    def _start_clipboard_monitor(self):
        """ClipboardWorker: poll once a second, push new text."""
        if not HAS_PYPERCLIP or self._clip_thread is not None:
            return

        def run():
            last = None
            while not self._clip_stop.is_set():
                if self._clip_enabled:
                    try:
                        current = pyperclip.paste()
                        if current and current != last:
                            last = current
                            self._push("clip_changed", {"text": current})
                    except Exception:
                        pass
                self._clip_stop.wait(1.0)
        self._clip_thread = threading.Thread(target=run, name="WebClipboard", daemon=True)
        self._clip_thread.start()

    def _register_hotkeys(self):
        """MainWindow.setup_global_hotkeys()."""
        if not HAS_KEYBOARD or self._hotkeys_registered:
            return
        try:
            _keyboard.add_hotkey("ctrl+shift+space", lambda: self._push("hotkey", {"name": "mic_toggle"}))
            _keyboard.add_hotkey("ctrl+k", lambda: self._push("hotkey", {"name": "palette"}))
            _keyboard.add_hotkey("ctrl+alt+w", self._toggle_window_visibility)
            _keyboard.add_hotkey("ctrl+alt+x", lambda: self._push("hotkey", {"name": "emergency_stop"}))
            self._hotkeys_registered = True
        except Exception as e:
            logger.warning(f"Global hotkeys not available: {e}")

    def _toggle_window_visibility(self):
        win = getattr(self, "_window", None)
        if win is None:
            return
        try:
            hidden = getattr(self, "_window_hidden", False)
            if hidden:
                win.show()
                win.restore()
            else:
                win.hide()
            self._window_hidden = not hidden
        except Exception:
            logger.debug("toggle window failed", exc_info=True)

    def _shutdown_services(self):
        """Stops what start_background_services() started (window close)."""
        stop_live = getattr(self, "_stop_live", None)
        if stop_live is not None:
            stop_live()
        self._clip_stop.set()
        if HAS_KEYBOARD and self._hotkeys_registered:
            try:
                _keyboard.unhook_all()
            except Exception:
                pass
        if not self._services_started:
            return
        try:
            from automation.thread_supervisor import thread_supervisor
            from automation.proactive import proactive_engine
            from brain.memory_consolidation import memory_consolidator
            from brain.ollama_lifecycle import ollama_lifecycle
            from brain.skill_loader import get_loader
            thread_supervisor.stop()
            try:
                get_loader().stop()
            except Exception:
                pass
            proactive_engine.stop()
            memory_consolidator.stop()
            audit_mod.audit.shutdown()
            ollama_lifecycle.shutdown()
        except Exception:
            logger.debug("shutdown_services failed", exc_info=True)
