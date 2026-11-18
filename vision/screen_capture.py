import os
import pyautogui
import logging
import cv2
import time
from datetime import datetime
from settings.config import config

logger = logging.getLogger("Vision")

# 🔧 FIX: this internal capture and AppController.take_screenshot() used to
# use unrelated filename prefixes ("screenshot_" here vs "Wave_" there) while
# saving into the SAME screenshots/ folder. That made it impossible to tell,
# from the filename alone, which files were screenshots the USER explicitly
# asked for vs. ones W.A.V.E. silently took for its own OCR needs (e.g. to
# verify a WhatsApp chat opened). "Delete my latest screenshot" would then
# happily delete whichever one happened to be newest - including an internal
# OCR capture the user never even knew existed, or worse, mistaking one for
# the other. Internal captures now get their own unambiguous prefix so
# AppController.delete_latest_screenshot() can filter them out entirely and
# only ever touch screenshots the user actually took.
INTERNAL_SCREENSHOT_PREFIX = "wave_internal_"
# Internal captures happen automatically as a side-effect of other actions
# (every WhatsApp send, etc.), so they can accumulate much faster than user
# screenshots - keep only the most recent few around.
MAX_INTERNAL_SCREENSHOTS = 15


def _enforce_retention(save_dir: str, prefix: str, max_files: int):
    """Keeps only the newest `max_files` files matching `prefix` in
    `save_dir`, deleting older ones. Never touches files with a different
    prefix, so this can run safely alongside AppController's own screenshots
    in the same folder without ever deleting the user's files."""
    try:
        matches = [
            os.path.join(save_dir, f) for f in os.listdir(save_dir)
            if f.startswith(prefix) and f.lower().endswith((".png", ".jpg"))
        ]
        if len(matches) <= max_files:
            return
        matches.sort(key=os.path.getctime, reverse=True)
        for stale_path in matches[max_files:]:
            try:
                os.remove(stale_path)
            except OSError as e:
                logger.warning(f"Could not remove old capture {stale_path}: {e}")
    except Exception as e:
        logger.warning(f"Retention cleanup failed for {save_dir}: {e}")


class VisionCapture:
    """Handles capturing the screen and webcam photos."""

    # 🔧 FIX: Settings > Privacy > "Allow screen capture (OCR/Vision)" used
    # to be purely decorative - both methods below now actually check this
    # before touching the screen or webcam.
    capture_enabled = True
    
    @staticmethod
    def take_screenshot() -> str:
        if not VisionCapture.capture_enabled:
            logger.info("Screenshot blocked - screen capture is disabled in Settings > Privacy.")
            return ""
        try:
            # 🔧 FIX: this used to save into assets/ while AppController's
            # own take_screenshot() (the "take a screenshot" voice command)
            # saves into screenshots/ - two different screenshot locations,
            # with the dedicated screenshots/ folder sitting empty and
            # assets/ silently becoming the real dumping ground. Both paths
            # now agree on screenshots/.
            save_dir = os.path.join(config.DATA_DIR, "screenshots")
            os.makedirs(save_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = os.path.join(save_dir, f"{INTERNAL_SCREENSHOT_PREFIX}{timestamp}.png")
            
            screenshot = pyautogui.screenshot()
            screenshot.save(file_path)
            _enforce_retention(save_dir, INTERNAL_SCREENSHOT_PREFIX, MAX_INTERNAL_SCREENSHOTS)
            return file_path
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return ""

    @staticmethod
    def take_photo() -> str:
        """Opens webcam secretly, clicks a picture, and shows it to user."""
        if not VisionCapture.capture_enabled:
            return "Screen/camera capture is turned off in Settings > Privacy."
        try:
            assets_dir = os.path.join(config.BASE_DIR, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = os.path.join(assets_dir, f"photo_{timestamp}.jpg")

            # Access default webcam using OpenCV (cv2.CAP_DSHOW makes it fast on Windows)
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                return "Error: Could not access the webcam."

            # Give camera 1 second to adjust lighting
            time.sleep(1)
            ret, frame = cap.read()
            cap.release()

            if ret:
                cv2.imwrite(file_path, frame)
                # Open the clicked photo automatically
                os.system(f'start "" "{file_path}"')
                return f"Picture clicked successfully! I've saved and opened it for you."
            else:
                return "Failed to capture image from webcam."
        except Exception as e:
            logger.error(f"Webcam error: {e}")
            return "Failed to click picture."