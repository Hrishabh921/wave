import os
import shutil
import pytesseract
from PIL import Image
import logging

logger = logging.getLogger("OCREngine")

# 🚨 FIX: Auto-detect Tesseract instead of relying on one hardcoded path.
# Checks PATH first, then common install locations (64-bit and 32-bit).
_CANDIDATE_PATHS = [
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
]

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
        # NOTE: For Windows, Tesseract needs to be installed on the system.
        # Download from: https://github.com/UB-Mannheim/tesseract/wiki
        self.tesseract_path = _find_tesseract()
        if self.tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_path
        else:
            logger.warning("Tesseract-OCR not found on PATH or in common install locations.")

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