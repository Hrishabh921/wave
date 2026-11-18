import os
import shutil
import pytesseract
from PIL import Image
import logging
from settings.config import config

logger = logging.getLogger("OCREngine")

# 🚨 FIX: Auto-detect Tesseract instead of relying on one hardcoded path.
# Checks PATH first, then common install locations (64-bit and 32-bit),
# then finally the portable copy already bundled in this project's own
# root folder (tesseract.exe + tessdata/) - which was sitting there totally
# unused before this fix, meaning OCR only ever worked if you separately
# installed Tesseract system-wide.
_CANDIDATE_PATHS = [
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    os.path.join(config.BASE_DIR, "tesseract.exe"),
]
_BUNDLED_TESSERACT = os.path.join(config.BASE_DIR, "tesseract.exe")
_BUNDLED_TESSDATA = os.path.join(config.BASE_DIR, "tessdata")

def _find_tesseract():
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    for p in _CANDIDATE_PATHS:
        if os.path.isfile(p):
            return p
    return None

class OCREngine:
    """Extracts text from images using Tesseract OCR."""
    
    def __init__(self):
        # NOTE: If no system-wide Tesseract is found, falls back to the
        # tesseract.exe + tessdata/ already bundled in the project folder,
        # so OCR works out of the box with no separate install needed.
        self.tesseract_path = _find_tesseract()
        if self.tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_path
            # A system-wide install already knows where its own language
            # files are; the bundled portable .exe needs to be told
            # explicitly via this environment variable.
            if self.tesseract_path == _BUNDLED_TESSERACT and os.path.isdir(_BUNDLED_TESSDATA):
                os.environ["TESSDATA_PREFIX"] = _BUNDLED_TESSDATA
        else:
            logger.warning("Tesseract-OCR not found on PATH, in common install locations, or bundled in the project folder.")

    def extract_text(self, image_path: str) -> str:
        """Reads text from the provided image file."""
        if not image_path:
            return "Error: No image path provided."

        if not self.tesseract_path:
            return ("Error: Tesseract OCR engine is not installed on this system. "
                    "Download and install it from https://github.com/UB-Mannheim/tesseract/wiki, "
                    "then restart Wave AI.")

        try:
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img)
            return text.strip() if text.strip() else "No readable text found in image."
        except FileNotFoundError:
            logger.error("Tesseract is not installed or path is incorrect.")
            return "Error: Tesseract OCR engine is not installed on this system."
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return f"Error extracting text: {e}"

ocr = OCREngine()