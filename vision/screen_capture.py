import os
import pyautogui
import logging
import cv2
import time
from datetime import datetime
from settings.config import config

logger = logging.getLogger("Vision")

class VisionCapture:
    """Handles capturing the screen and webcam photos."""
    
    @staticmethod
    def take_screenshot() -> str:
        try:
            assets_dir = os.path.join(config.BASE_DIR, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = os.path.join(assets_dir, f"screenshot_{timestamp}.png")
            
            screenshot = pyautogui.screenshot()
            screenshot.save(file_path)
            return file_path
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return ""

    @staticmethod
    def take_photo() -> str:
        """Opens webcam secretly, clicks a picture, and shows it to user."""
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