import os
import shutil
import logging
from pathlib import Path

logger = logging.getLogger("FileManager")

class FileManager:
    """Handles file system operations (Create, Delete, Search)."""

    @staticmethod
    def get_default_path() -> str:
        """Returns the current user's desktop path as default."""
        return os.path.join(os.path.expanduser("~"), "Desktop")

    @classmethod
    def create_folder(cls, folder_name: str, path: str = None) -> str:
        """Creates a new directory."""
        if not path:
            path = cls.get_default_path()
            
        full_path = os.path.join(path, folder_name)
        try:
            os.makedirs(full_path, exist_ok=True)
            return f"Folder '{folder_name}' created at {path}."
        except Exception as e:
            logger.error(f"Error creating folder: {e}")
            return f"Failed to create folder: {str(e)}"

    @classmethod
    def create_file(cls, file_name: str, content: str = "", path: str = None) -> str:
        """Creates a new file with optional content."""
        if not path:
            path = cls.get_default_path()
            
        full_path = os.path.join(path, file_name)
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"File '{file_name}' created successfully."
        except Exception as e:
            logger.error(f"Error creating file: {e}")
            return f"Failed to create file: {str(e)}"