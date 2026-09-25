import logging
import datetime
import platform
import getpass
import sys
import os
import ctypes
import time
import json
import base64
import io
import pyautogui
from groq import Groq
from settings.config import config
from database.db_manager import db

from automation.app_control import AppController
from automation.file_manager import FileManager
from web.web_ops import WebManager
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr
from monitoring.sys_monitor import SystemMonitor

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

logger = logging.getLogger("GroqBrain")


def _get_audio_interface():
    devices = AudioUtilities.GetSpeakers()
    return devices.EndpointVolume


# 🚨 REMOVED: the Sleep feature has been taken out entirely. Every attempt
# (rundll32, direct ctypes API, Force on/off, simulated hardware key) still
# either did nothing or actually shut the PC down on this hardware, which is
# a real firmware-level limitation of this system, not something fixable
# from an app.


def adjust_system_volume(delta_percent: int) -> bool:
    """Nudges system volume via the real Windows Core Audio API — far more
    reliable than a simulated media-key press, which some Windows setups
    and audio drivers silently swallow. Returns True if pycaw handled it."""
    if not HAS_PYCAW:
        return False
    try:
        vol = _get_audio_interface()
        current = round(vol.GetMasterVolumeLevelScalar() * 100)
        new_val = max(0, min(100, current + delta_percent))
        vol.SetMasterVolumeLevelScalar(new_val / 100.0, None)
        return True
    except Exception as e:
        logger.error(f"adjust_system_volume error: {e}")
        return False


def toggle_system_mute() -> bool:
    """Toggles mute via pycaw. Returns True if it succeeded."""
    if not HAS_PYCAW:
        return False
    try:
        vol = _get_audio_interface()
        vol.SetMute(0 if vol.GetMute() else 1, None)
        return True
    except Exception as e:
        logger.error(f"toggle_system_mute error: {e}")
        return False

def _tool(name, description, params=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params or {},
                "required": required or [],
            },
        },
    }


TOOLS = [
    _tool("open_app", "Opens any application, program, game, or utility by name (e.g. Chrome, VS Code, Spotify, Notepad, Calculator). Do NOT use this for websites/searches — use open_website for anything involving a website, a search query, or a named browser.",
          {"app_name": {"type": "string", "description": "Name of the app to open."}}, ["app_name"]),
    _tool("open_website", "Opens a website, optionally searching it for something, optionally in a SPECIFIC named browser. ALWAYS use this (never open_app) whenever the command names a browser (e.g. 'on brave', 'in chrome', 'using edge') and/or a website to search (e.g. 'search youtube for cats', 'google search for X', 'search youtube on brave'). Both 'site' and 'browser' can be given together in the same call — never pick just one and drop the other.",
          {"site": {"type": "string", "description": "The website to open, e.g. 'youtube', 'google', 'amazon', or a raw URL."},
           "query": {"type": "string", "description": "Optional — what to search for on that site. Omit if the user just wants the site opened with no search."},
           "browser": {"type": "string", "description": "Optional — a specific browser named in the command, e.g. 'chrome', 'brave', 'edge', 'firefox'. Omit entirely if no browser was named."}},
          ["site"]),
    _tool("close_app", "Closes a running application by name.",
          {"app_name": {"type": "string", "description": "Name of the app to close."}}, ["app_name"]),
    _tool("close_current_window", "Closes the currently focused window (Alt+F4)."),
    _tool("close_current_tab", "Closes the current browser tab (Ctrl+W)."),
    _tool("play_youtube", "Plays a song or video on YouTube. Use this whenever the user asks to play, put on, or listen to any song/music/video.",
          {"query": {"type": "string", "description": "Song name, artist, or video to search and play."}}, ["query"]),
    _tool("media_play_pause", "Toggles play/pause on whatever media player is currently active (does not search for a new song)."),
    _tool("media_next_track", "Skips to the next track in the current media player."),
    _tool("media_previous_track", "Goes back to the previous track in the current media player."),
    _tool("volume_up", "Increases system volume by about 10%."),
    _tool("volume_down", "Decreases system volume by about 10%."),
    _tool("toggle_mute", "Mutes or unmutes system audio."),
    _tool("search_google", "Opens a Google search in the browser for the given query.",
          {"query": {"type": "string", "description": "What to search for."}}, ["query"]),
    _tool("get_live_information", "Fetches current/live information from the internet to answer a factual question (news, prices, weather, scores, current facts) — use this instead of guessing.",
          {"query": {"type": "string", "description": "The question or topic to look up."}}, ["query"]),
    _tool("take_screenshot", "Captures a screenshot of the current screen and saves it."),
    _tool("delete_latest_screenshot", "Deletes the most recently taken screenshot."),
    _tool("read_screen_text", "Captures the screen and reads/extracts any text visible on it (OCR)."),
    _tool("take_photo", "Takes a photo using the webcam."),
    _tool("lock_pc", "Locks the PC/Windows session immediately."),
    _tool("create_folder", "Creates a new folder on the desktop.",
          {"folder_name": {"type": "string", "description": "Name for the new folder."}}, ["folder_name"]),
    _tool("create_file", "Creates a new empty text file on the desktop.",
          {"file_name": {"type": "string", "description": "Name for the new file, including extension."}}, ["file_name"]),
    _tool("write_notepad", "Opens Notepad and types the given text into it.",
          {"text": {"type": "string", "description": "The text to type."}}, ["text"]),
    _tool("open_system_setting", "Opens a Windows system settings page.",
          {"setting": {"type": "string", "description": "Which settings page: display, sound, battery, update, or general settings."}}, ["setting"]),
    _tool("toggle_hardware", "Turns wifi, bluetooth, or hotspot on or off.",
          {"instruction": {"type": "string", "description": "e.g. 'turn on wifi', 'turn off bluetooth'."}}, ["instruction"]),
    _tool("send_whatsapp_message", "Sends a WhatsApp message to a contact.",
          {"contact_name": {"type": "string"}, "message": {"type": "string"}}, ["contact_name", "message"]),
    _tool("get_pc_info", "Gets hardware/system information about this PC (CPU, RAM, OS, etc)."),
    _tool("get_clipboard", "Reads the current contents of the clipboard."),
]


class WaveBrain:
    def __init__(self):
        if not config.GROQ_API_KEY:
            logger.error("Groq API key is missing!")
            self.client = None
            return
        self.client = Groq(api_key=config.GROQ_API_KEY)
        self.model = "openai/gpt-oss-120b"
        self.vision_model = "qwen/qwen3.8-27b"

    def get_dynamic_system_prompt(self):
        now = datetime.datetime.now()
        current_time = now.strftime("%I:%M %p")
        current_date = now.strftime("%A, %d %B %Y")
        user_name = getpass.getuser()
        
        os_system = platform.system()
        os_release = platform.release()
        if os_system == "Windows" and os_release == "10":
            if hasattr(sys, 'getwindowsversion') and sys.getwindowsversion().build >= 22000:
                os_release = "11"
        os_name = f"{os_system} {os_release}"

        return {
            "role": "system",
            "content": (
                f"You are Wave, an elite AI Desktop Assistant for {user_name}.\n"
                f"System: {os_name} | Date: {current_date} | Time: {current_time}.\n"
                f"RULES:\n"
                f"1. Never hallucinate sending messages or taking actions unless explicitly shown in chat history.\n"
                f"2. NEVER invent or fake system information. If you don't have the data, say you don't know.\n"
                f"3. Match your depth to the question: answer simple questions in a couple of sentences, "
                f"but for anything technical, multi-step, or open-ended, think it through properly and give "
                f"a complete, well-reasoned answer - don't sacrifice correctness or thoroughness just to be brief.\n"
                f"4. When a command names a WEBSITE and/or a specific BROWSER together — e.g. "
                f"'search youtube on brave', 'open google in chrome', 'search amazon for shoes using edge' — "
                f"always call open_website with every piece the user gave you filled in at once (site, and "
                f"query if they gave search terms, and browser if they named one). Never drop the site to "
                f"satisfy the browser or vice versa, and never call open_app for this — open_app is only for "
                f"launching a plain application/program by name, not a website.\n"
                f"5. Whenever you share code, commands, or config, always put it inside a fenced markdown "
                f"code block with the language name right after the opening ``` (e.g. ```python), so it "
                f"renders with proper formatting and syntax highlighting."
            )
        }

    def _execute_single_action(self, cmd: str) -> str:
        cmd_lower = cmd.lower().strip()
        
        if cmd_lower in ["hi", "hii", "hello", "hey", "ok", "thanks"]:
            return None 

        if any(phrase in cmd_lower for phrase in ["close window", "close current window", "close this window", "close all windows"]):
            return AppController.close_current_window()
            
        elif "close tab" in cmd_lower:
            return AppController.close_current_tab()

        elif any(phrase in cmd_lower for phrase in ["pc info", "system info", "about my pc", "system stats", "hardware info", "how's my pc", "check my system"]):
            return SystemMonitor.get_hardware_info()

        elif any(phrase in cmd_lower for phrase in ["read the screen", "read screen", "screen text", "what does the screen say", "read text on screen", "what's on my screen"]):
            path = VisionCapture.take_screenshot()
            if not path:
                return "I couldn't capture the screen to read it."
            extracted = ocr.extract_text(path)
            return extracted if extracted else "I captured the screen but couldn't find any readable text on it."

        elif any(phrase in cmd_lower for phrase in ["lock the pc", "lock my pc", "lock the computer", "lock my computer", "lock windows", "lock the screen"]):
            os.system("rundll32.exe user32.dll,LockWorkStation")
            return "Locking your PC now."

        elif any(phrase in cmd_lower for phrase in ["sleep the pc", "sleep my pc", "put the pc to sleep", "put my pc to sleep", "sleep mode", "sleep my computer"]):
            return ("Sleep isn't available on this PC — the hardware doesn't support it reliably "
                    "(it either does nothing or shuts down instead), so this feature has been removed.")

        elif any(phrase in cmd_lower for phrase in ["restart the pc", "restart my pc", "restart the computer", "restart my computer", "reboot"]):
            return ("For safety, I won't restart your PC directly from chat — head to the "
                    "Apps & Power page and use the Restart button there, which will ask you to confirm first.")

        elif any(phrase in cmd_lower for phrase in ["shutdown the pc", "shut down the pc", "shutdown my pc", "shut down my pc",
                                                       "turn off the pc", "turn off my computer", "power off"]):
            return ("For safety, I won't shut down your PC directly from chat — head to the "
                    "Apps & Power page and use the Shutdown button there, which will ask you to confirm first.")

        elif any(phrase in cmd_lower for phrase in ["pause music", "pause the music", "pause song", "stop the music",
                                                       "resume music", "resume the music", "unpause music", "resume song"]):
            pyautogui.press('playpause')
            return "Toggled play/pause on your media player."

        elif any(phrase in cmd_lower for phrase in ["next song", "next track", "skip song", "skip track", "skip this song"]):
            pyautogui.press('nexttrack')
            return "Skipped to the next track."

        elif any(phrase in cmd_lower for phrase in ["previous song", "previous track", "last song", "go back a song", "play the last track"]):
            pyautogui.press('prevtrack')
            return "Went back to the previous track."

        elif any(phrase in cmd_lower for phrase in ["volume up", "increase volume", "turn up the volume", "louder"]):
            if not adjust_system_volume(10):
                pyautogui.press('volumeup')
            return "Turned the volume up."

        elif any(phrase in cmd_lower for phrase in ["volume down", "decrease volume", "turn down the volume", "quieter", "lower the volume"]):
            if not adjust_system_volume(-10):
                pyautogui.press('volumedown')
            return "Turned the volume down."

        elif any(phrase in cmd_lower for phrase in ["mute the volume", "mute the sound", "mute audio", "unmute"]):
            if not toggle_system_mute():
                pyautogui.press('volumemute')
            return "Toggled mute on your system audio."

        elif any(phrase in cmd_lower for phrase in ["what's in my clipboard", "what is in my clipboard", "read my clipboard", "read clipboard", "what did i copy"]):
            if not HAS_PYPERCLIP:
                return "Clipboard reading needs the 'pyperclip' package installed."
            try:
                content = pyperclip.paste()
                return f"Your clipboard has: {content}" if content else "Your clipboard is empty right now."
            except Exception:
                return "I couldn't read the clipboard right now."

        elif any(phrase in cmd_lower for phrase in ["create a folder", "make a folder", "new folder"]):
            folder_name = cmd_lower
            for phrase in ["create a folder called", "create a folder named", "create a folder",
                           "make a folder called", "make a folder named", "make a folder", "new folder"]:
                if phrase in folder_name:
                    folder_name = folder_name.split(phrase, 1)[1].strip()
                    break
            return FileManager.create_folder(folder_name if folder_name else "New Folder")

        elif any(phrase in cmd_lower for phrase in ["create a file", "make a file", "new file"]):
            file_name = cmd_lower
            for phrase in ["create a file called", "create a file named", "create a file",
                           "make a file called", "make a file named", "make a file", "new file"]:
                if phrase in file_name:
                    file_name = file_name.split(phrase, 1)[1].strip()
                    break
            return FileManager.create_file(file_name if file_name else "New File.txt")

        elif "delete" in cmd_lower and "screenshot" in cmd_lower:
            return AppController.delete_latest_screenshot()

        elif any(phrase in cmd_lower for phrase in ["take screenshot", "take a screenshot", "capture screen", "screenshot"]):
            return AppController.take_screenshot()

        elif "click" in cmd_lower and ("picture" in cmd_lower or "photo" in cmd_lower):
            return VisionCapture.take_photo()

        elif "check windows update" in cmd_lower or "windows update" in cmd_lower:
            return AppController.open_system_setting("update")
            
        elif any(hw in cmd_lower for hw in ["hotspot", "bluetooth", "wifi"]) and any(action in cmd_lower for action in ["turn on", "turn off", "switch on", "switch off"]):
            return AppController.toggle_hardware(cmd_lower)

        elif any(word in cmd_lower for word in ["display", "sound", "battery", "settings"]):
            return AppController.open_system_setting(cmd_lower)

        # 🚨 THE NEW PLAY LOGIC 🚨
        elif cmd_lower.startswith("play "):
            song_query = cmd_lower.replace("play ", "", 1).strip()
            return WebManager.play_on_youtube(song_query)

        elif "write a note" in cmd_lower or "type in notepad" in cmd_lower:
            note_text = cmd_lower.replace("write a note saying", "").replace("type in notepad", "").strip()
            return AppController.write_in_notepad(note_text)

        elif any(cmd_lower.startswith(w) for w in ["open ", "start ", "launch ", "turn on "]):
            app_name = ""
            for w in ["open ", "start ", "launch ", "turn on "]:
                if cmd_lower.startswith(w):
                    app_name = cmd_lower.replace(w, "", 1).strip()
                    break
            return AppController.open_app(app_name)
            
        elif cmd_lower.startswith("close "):
            app_name = cmd_lower.replace("close ", "").strip()
            return AppController.close_app(app_name)
            
        elif cmd_lower.startswith("search "):
            query = cmd_lower.replace("search for", "").replace("search", "", 1).strip()
            # 🚨 NEW: pull out a trailing named browser ("... on brave",
            # "... in chrome") so this simple keyword fallback also opens
            # the right browser, not just the AI tool-calling path.
            browser = None
            for b in ("brave", "chrome", "edge", "firefox", "opera"):
                for prep in (f" on {b}", f" in {b}", f" using {b}"):
                    if query.endswith(prep):
                        browser = b
                        query = query[: -len(prep)].strip()
                        break
                if browser:
                    break
            if browser:
                site, _, rest = query.partition(" for ")
                return AppController.open_website(site.strip() or query, rest.strip() or None, browser)
            if "youtube" in query:
                return WebManager.play_on_youtube(query.replace("on youtube", "").strip())
            else:
                return WebManager.search_google(query)
                
        return None

    def process_actions(self, user_text: str) -> str:
        text_lower = user_text.lower()
        if "whatsapp" in text_lower and "send" in text_lower and " to " in text_lower:
            try:
                part1 = text_lower.split("send ")[1] 
                message = part1.split(" to ")[0].strip() 
                contact = part1.split(" to ")[1].replace("on whatsapp", "").strip()
                return AppController.send_whatsapp_message(contact, message)
            except Exception: pass

        commands = text_lower.split(" and ")
        results = [res for cmd in commands if (res := self._execute_single_action(cmd))]
        return " & ".join(results) if results else None 

    def _think_fallback(self, user_text: str) -> str:
        """Deterministic keyword matching — no LLM/network needed. Used as a
        safety net if the smart tool-calling call errors out, and used
        directly when there's no API key at all."""
        text_lower = user_text.lower()

        action_result = self.process_actions(user_text)
        if action_result:
            return action_result

        if not self.client:
            return "I don't have an API key configured, so I can only handle direct commands like 'open chrome' or 'take a screenshot' right now."

        internet_context = ""
        search_keywords = ["search", "latest", "news", "who won", "price", "weather", "who is", "what is"]
        skip_words = ["time", "date", "today", "now", "day", "play"]
        if any(k in text_lower for k in search_keywords) and not any(skip in text_lower for skip in skip_words):
            query = text_lower.replace("search for", "").replace("search", "").strip()
            if live_data := WebManager.ask_internet(query):
                internet_context = f"\n\n[LIVE INTERNET DATA: {live_data}]"

        messages = [self.get_dynamic_system_prompt()]
        history = db.get_chat_history(limit=8)
        for role, msg in history:
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": user_text + internet_context})

        chat_completion = self.client.chat.completions.create(
            messages=messages, model=self.model, temperature=0.6, max_tokens=1600,
        )
        return chat_completion.choices[0].message.content

    # ---------------------------------------------------------------
    def _dispatch_tool(self, name: str, arguments_json: str) -> str:
        """Executes exactly one tool call against the real backend and
        returns a short natural-language result string."""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except Exception:
            args = {}

        try:
            if name == "open_app":
                return AppController.open_app(args.get("app_name", ""))
            elif name == "open_website":
                return AppController.open_website(args.get("site", ""), args.get("query"), args.get("browser"))
            elif name == "close_app":
                return AppController.close_app(args.get("app_name", ""))
            elif name == "close_current_window":
                return AppController.close_current_window()
            elif name == "close_current_tab":
                return AppController.close_current_tab()
            elif name == "play_youtube":
                return WebManager.play_on_youtube(args.get("query", ""))
            elif name == "media_play_pause":
                pyautogui.press('playpause')
                return "Toggled play/pause."
            elif name == "media_next_track":
                pyautogui.press('nexttrack')
                return "Skipped to the next track."
            elif name == "media_previous_track":
                pyautogui.press('prevtrack')
                return "Went to the previous track."
            elif name == "volume_up":
                if not adjust_system_volume(10):
                    pyautogui.press('volumeup')
                return "Turned the volume up."
            elif name == "volume_down":
                if not adjust_system_volume(-10):
                    pyautogui.press('volumedown')
                return "Turned the volume down."
            elif name == "toggle_mute":
                if not toggle_system_mute():
                    pyautogui.press('volumemute')
                return "Toggled mute."
            elif name == "search_google":
                return WebManager.search_google(args.get("query", ""))
            elif name == "get_live_information":
                data = WebManager.ask_internet(args.get("query", ""))
                return data if data else "I couldn't find live information on that right now."
            elif name == "take_screenshot":
                return AppController.take_screenshot()
            elif name == "delete_latest_screenshot":
                return AppController.delete_latest_screenshot()
            elif name == "read_screen_text":
                path = VisionCapture.take_screenshot()
                if not path:
                    return "I couldn't capture the screen to read it."
                extracted = ocr.extract_text(path)
                return extracted if extracted else "I captured the screen but found no readable text."
            elif name == "take_photo":
                return VisionCapture.take_photo()
            elif name == "lock_pc":
                os.system("rundll32.exe user32.dll,LockWorkStation")
                return "Locking your PC now."
            elif name == "create_folder":
                return FileManager.create_folder(args.get("folder_name", "New Folder"))
            elif name == "create_file":
                return FileManager.create_file(args.get("file_name", "New File.txt"))
            elif name == "write_notepad":
                return AppController.write_in_notepad(args.get("text", ""))
            elif name == "open_system_setting":
                return AppController.open_system_setting(args.get("setting", "settings"))
            elif name == "toggle_hardware":
                return AppController.toggle_hardware(args.get("instruction", ""))
            elif name == "send_whatsapp_message":
                return AppController.send_whatsapp_message(args.get("contact_name", ""), args.get("message", ""))
            elif name == "get_pc_info":
                return SystemMonitor.get_hardware_info()
            elif name == "get_clipboard":
                if not HAS_PYPERCLIP:
                    return "Clipboard reading needs the 'pyperclip' package installed."
                content = pyperclip.paste()
                return f"Clipboard contains: {content}" if content else "The clipboard is empty."
            else:
                return f"I don't have a '{name}' tool available."
        except Exception as e:
            logger.error(f"Tool '{name}' failed: {e}")
            return f"Something went wrong while trying to do that: {e}"

    def _encode_image_for_vision(self, image_path: str) -> str:
        """Base64-encodes an image as a data: URI for the Groq vision model,
        downscaling large images first (via Pillow, when available) so
        requests stay fast and comfortably under the API's size limits."""
        if HAS_PIL:
            try:
                with Image.open(image_path) as img:
                    img = img.convert("RGB")
                    max_side = 1568
                    if max(img.size) > max_side:
                        img.thumbnail((max_side, max_side), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=87)
                    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
                    return f"data:image/jpeg;base64,{encoded}"
            except Exception as e:
                logger.warning(f"Image downscale failed, sending original file: {e}")

        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{encoded}"

    def _think_with_image(self, user_text: str, attachment: dict) -> str:
        """Answers a question about an attached image using Groq's vision
        model (separate from the text/tool-calling model above, since
        openai/gpt-oss-20b is text-only)."""
        if not self.client:
            reply = "I can't look at images right now — no Groq API key is configured."
            db.save_message("assistant", reply)
            return reply
        try:
            data_uri = self._encode_image_for_vision(attachment["path"])
            prompt_text = user_text.strip() or "What's in this image? Describe it, and point out anything notable."
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }]
            response = self.client.chat.completions.create(
                messages=messages, model=self.vision_model,
                temperature=0.4, max_tokens=1200,
            )
            reply = response.choices[0].message.content or "I looked at the image but didn't have anything to add."
        except Exception as e:
            logger.error(f"Vision request failed: {e}")
            reply = f"I couldn't process that image — something went wrong talking to the vision model: {e}"
        db.save_message("assistant", reply)
        return reply

    def think(self, user_text: str, attachment: dict = None) -> str:
        db.save_message("user", user_text)

        if attachment and attachment.get("kind") == "image":
            return self._think_with_image(user_text, attachment)

        # Restart/shutdown must NEVER be silently auto-executed from chat.
        text_lower = user_text.lower()
        if any(p in text_lower for p in ["restart the pc", "restart my pc", "restart the computer",
                                           "restart my computer", "reboot"]):
            reply = ("For safety, I won't restart your PC directly from chat — head to the "
                     "Apps & Power page and use the Restart button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            return reply
        if any(p in text_lower for p in ["shutdown the pc", "shut down the pc", "shutdown my pc",
                                           "shut down my pc", "turn off the pc", "turn off my computer", "power off"]):
            reply = ("For safety, I won't shut down your PC directly from chat — head to the "
                     "Apps & Power page and use the Shutdown button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            return reply

                # 🚨 REMOVED: Sleep isn't reliable on this PC's hardware (either does
        # nothing or shuts the PC down), so it's been taken out entirely
        # instead of pretending to do something it can't.
        if any(p in text_lower for p in ["sleep the pc", "sleep my pc", "put the pc to sleep",
                                           "put my pc to sleep", "sleep mode", "sleep my computer",
                                           "sleep the computer", "go to sleep", "make the pc sleep"]) \
           or text_lower.strip() in ("sleep", "sleep pc", "sleep computer"):
            reply = ("Sleep isn't available on this PC — the hardware doesn't support it reliably "
                     "(it either does nothing or shuts down instead), so this feature has been removed.")
            db.save_message("assistant", reply)
            return reply

        if not self.client:
            reply = self._think_fallback(user_text)
            db.save_message("assistant", reply)
            return reply

        try:
            messages = [self.get_dynamic_system_prompt()]
            history = db.get_chat_history(limit=16)
            for role, msg in history:
                messages.append({"role": role, "content": msg})

            if attachment and attachment.get("kind") == "document":
                doc_note = " (truncated)" if attachment.get("truncated") else ""
                user_content = (
                    f"{user_text}\n\n"
                    f"--- Attached file: {attachment.get('name', 'file')}{doc_note} ---\n"
                    f"{attachment.get('text_content', '')}"
                )
            else:
                user_content = user_text
            messages.append({"role": "user", "content": user_content})

            try:
                response = self.client.chat.completions.create(
                    messages=messages, model=self.model, tools=TOOLS,
                    tool_choice="auto", temperature=0.4, max_tokens=1600,
                    extra_body={"reasoning_effort": "high"},
                )
            except Exception:
                # This account/model combo may not accept reasoning_effort - retry without it
                response = self.client.chat.completions.create(
                    messages=messages, model=self.model, tools=TOOLS,
                    tool_choice="auto", temperature=0.4, max_tokens=1600,
                )
            choice_msg = response.choices[0].message

            if choice_msg.tool_calls:
                results = [self._dispatch_tool(call.function.name, call.function.arguments)
                           for call in choice_msg.tool_calls]
                reply = " ".join(r for r in results if r)
            else:
                reply = choice_msg.content

            db.save_message("assistant", reply)
            return reply

        except Exception as e:
            logger.error(f"Tool-calling think() failed, falling back: {e}")
            try:
                reply = self._think_fallback(user_text)
            except Exception as e2:
                reply = f"System Error: {e2}"
            db.save_message("assistant", reply)
            return reply