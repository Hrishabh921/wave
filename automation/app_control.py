import os
import psutil
import logging
import time
import shutil
import subprocess
import urllib.parse
import pyautogui
import webbrowser

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

logger = logging.getLogger("AppControl")

# 🚨 NEW: known websites (open as-is) and ones that also support a direct
# search-results URL (used when the user gives a search query too).
_SITE_URLS = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "twitter": "https://www.twitter.com", "x": "https://www.x.com",
    "github": "https://www.github.com", "amazon": "https://www.amazon.com",
    "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com", "whatsapp": "https://web.whatsapp.com",
    "gmail": "https://mail.google.com", "maps": "https://maps.google.com",
    "wikipedia": "https://www.wikipedia.org", "linkedin": "https://www.linkedin.com",
}
_SEARCH_URL_TEMPLATES = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
}
# 🚨 NEW: lets a command name a SPECIFIC browser ("on brave", "in chrome").
_BROWSER_EXE_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "brave": "brave.exe", "brave browser": "brave.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "mozilla firefox": "firefox.exe",
    "opera": "opera.exe",
}


def _find_browser_path(browser_name: str):
    """Finds a specific browser's .exe regardless of where it's installed,
    using Windows' own "App Paths" registry (the same mechanism every major
    browser registers itself with) — falls back to checking PATH."""
    exe_name = _BROWSER_EXE_NAMES.get((browser_name or "").lower().strip())
    if not exe_name:
        return None
    if HAS_WINREG:
        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    path, _ = winreg.QueryValueEx(key, "")
                    if path and os.path.isfile(path):
                        return path
            except (FileNotFoundError, OSError):
                continue
    return shutil.which(exe_name)


class AppController:
    """Handles deep interaction, UI automation, and Window Controls."""

    @classmethod
    def open_app(cls, app_name: str) -> str:
        app_name = app_name.lower().strip()
        
        websites = ["youtube", "google", "facebook", "instagram", "twitter", "github"]
        for site in websites:
            if site in app_name:
                webbrowser.open(f"https://www.{site}.com")
                return f"Successfully opened {site} in browser."

        try:
            pyautogui.press('win')
            time.sleep(0.5)
            pyautogui.write(app_name, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')
            return f"Successfully opened {app_name}."
        except Exception as e:
            logger.error(f"Failed to open {app_name}: {e}")
            return f"Error opening {app_name}."

    # 🚨 NEW: handles "search <site> [for <query>] [on/in <browser>]" as ONE
    # combined action, instead of forcing open_app to guess between the
    # site name and the browser name (which is what caused "search youtube
    # on brave" to just open Brave, losing the "youtube" part).
    @classmethod
    def open_website(cls, site: str, query: str = None, browser: str = None) -> str:
        site_raw = (site or "").strip()
        site_key = site_raw.lower()

        if site_key.startswith("http://") or site_key.startswith("https://"):
            url = site_raw
        else:
            matched = next((k for k in _SITE_URLS if k in site_key or site_key in k), None)
            if matched and query and matched in _SEARCH_URL_TEMPLATES:
                url = _SEARCH_URL_TEMPLATES[matched].format(q=urllib.parse.quote_plus(query))
            elif matched:
                url = _SITE_URLS[matched]
            elif query:
                # Unknown site name but a search term was given — search
                # for "<query> <site>" on Google as a sane fallback.
                url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query + ' ' + site_raw)}"
            else:
                url = f"https://www.{site_key.replace(' ', '')}.com"

        browser_path = _find_browser_path(browser) if browser else None
        label = f"{query} on {site_raw}" if query else site_raw
        try:
            if browser_path:
                subprocess.Popen([browser_path, url])
                return f"Opened {label} in {browser.strip().title()}."
            webbrowser.open(url)
            if browser:
                return f"Couldn't find {browser} installed, so I opened {label} in your default browser instead."
            return f"Opened {label} in your default browser."
        except Exception as e:
            logger.error(f"open_website error: {e}")
            return f"Error opening {site_raw}."

    @classmethod
    def toggle_hardware(cls, command: str) -> str:
        command = command.lower()
        try:
            if "hotspot" in command:
                os.system("start ms-settings:network-mobilehotspot")
                time.sleep(1.2) 
                pyautogui.press('space') 
                time.sleep(0.2)
                pyautogui.hotkey('alt', 'f4') 
                action = "Turned ON" if "on" in command else "Turned OFF"
                return f"{action} Mobile Hotspot."

            elif "bluetooth" in command:
                os.system("start ms-settings:bluetooth")
                time.sleep(1.2)
                pyautogui.press('space')
                time.sleep(0.2)
                pyautogui.hotkey('alt', 'f4')
                action = "Turned ON" if "on" in command else "Turned OFF"
                return f"{action} Bluetooth."

            elif "wifi" in command:
                os.system("start ms-settings:network-wifi")
                time.sleep(1.2)
                pyautogui.press('space')
                time.sleep(0.2)
                pyautogui.hotkey('alt', 'f4')
                action = "Turned ON" if "on" in command else "Turned OFF"
                return f"{action} Wi-Fi."
                
            return "Hardware toggle not recognized."
        except Exception as e:
            return "Failed to toggle hardware settings."

    @classmethod
    def close_app(cls, app_name: str) -> str:
        app_name = app_name.lower().strip()
        closed = False
        try:
            for process in psutil.process_iter(['pid', 'name']):
                if process.info['name'] and app_name in process.info['name'].lower():
                    psutil.Process(process.info['pid']).terminate()
                    closed = True
            return f"Successfully closed {app_name}." if closed else f"App not running."
        except Exception as e:
            return f"Error closing {app_name}."

    @classmethod
    def close_current_window(cls) -> str:
        pyautogui.hotkey('alt', 'f4')
        return "Closed the current window."

    @classmethod
    def close_current_tab(cls) -> str:
        pyautogui.hotkey('ctrl', 'w')
        return "Closed the current tab."

    @classmethod
    def write_in_notepad(cls, text: str) -> str:
        try:
            cls.open_app("notepad")
            time.sleep(2) 
            pyautogui.write(text, interval=0.05) 
            return "Successfully opened Notepad and wrote the text."
        except Exception as e:
            return "Failed to type the text."

    @classmethod
    def send_whatsapp_message(cls, contact_name: str, message: str) -> str:
        try:
            os.system("start whatsapp:")
            time.sleep(6) 
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(1.5)
            pyautogui.write(contact_name, interval=0.05)
            time.sleep(2) 
            pyautogui.press('enter')
            time.sleep(1)
            pyautogui.write(message, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')
            return f"Sent '{message}' to {contact_name} on WhatsApp."
        except Exception as e:
            return "Failed to send WhatsApp message."

    @classmethod
    def open_system_setting(cls, setting_type: str) -> str:
        setting_type = setting_type.lower()
        settings_map = {
            "display": "ms-settings:display",
            "sound": "ms-settings:sound",
            "battery": "ms-settings:batterysaver",
            "update": "ms-settings:windowsupdate",
            "settings": "ms-settings:"
        }
        for key, command in settings_map.items():
            if key in setting_type:
                os.system(f"start {command}")
                return f"Successfully opened {key.capitalize()} settings for you."
        return "Could not find the requested system setting."

    @classmethod
    def take_screenshot(cls) -> str:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            save_dir = os.path.join(base_dir, "screenshots")
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
                
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            file_path = os.path.join(save_dir, f"Wave_{timestamp}.png")
            
            pyautogui.screenshot(file_path)
            os.system(f'explorer "{file_path}"')
            return f"Screenshot captured and saved in your project folder."
        except Exception as e:
            logger.error(f"Failed to capture screen: {e}")
            return "Error capturing the screen."

    # 🚨 LATEST: Delete Only Latest Screenshot Logic 🚨
    @classmethod
    def delete_latest_screenshot(cls) -> str:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            save_dir = os.path.join(base_dir, "screenshots")
            
            if not os.path.exists(save_dir):
                return "You don't have any saved screenshots to delete."
                
            # Folder ke saare screenshots ki list banao
            files = [os.path.join(save_dir, f) for f in os.listdir(save_dir) if f.endswith((".png", ".jpg"))]
            
            if not files:
                return "No screenshots found in the folder to delete."
                
            # Sabse naya file dhundho (Time ke hisaab se)
            latest_file = max(files, key=os.path.getctime)
            
            # Sirf latest file ko delete karo
            os.remove(latest_file)
            
            return "Successfully deleted the latest screenshot."
        except Exception as e:
            logger.error(f"Failed to delete screenshot: {e}")
            return "Error deleting the screenshot."