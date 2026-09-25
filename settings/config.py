import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Central configuration for the Wave AI Assistant."""
    APP_NAME = "Wave AI"
    VERSION = "1.0.0"
    
    # API Keys
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    
    # Directory Paths
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_DIR = os.path.join(BASE_DIR, "database")
    DB_PATH = os.path.join(DB_DIR, "wave_memory.db")
    
    # Ensure directories exist
    os.makedirs(DB_DIR, exist_ok=True)
    
    # Logging Setup
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO, 
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(os.path.join(BASE_DIR, "wave_system.log")),
            logging.StreamHandler()
        ]
    )

config = Config()