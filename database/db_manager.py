import sqlite3
import logging
from settings.config import config

logger = logging.getLogger("DBManager")

class DatabaseManager:
    """Manages SQLite database operations for Wave's memory."""
    
    def __init__(self):
        self.db_path = config.DB_PATH
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """Returns a database connection."""
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Initializes the database schema for history, preferences, and workflows."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Chat History
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        role TEXT NOT NULL,
                        message TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # User Preferences & Memory
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS memory (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                ''')
                
                # Workflows / Automation
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS workflows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        actions TEXT NOT NULL
                    )
                ''')
                
                conn.commit()
                logger.info("Database schemas initialized successfully.")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")

    def save_message(self, role: str, message: str):
        """Saves a chat message to history."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO chat_history (role, message) VALUES (?, ?)", 
                    (role, message)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save message: {e}")

    def get_chat_history(self, limit: int = 10) -> list:
        """Retrieves recent chat history for AI context."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", 
                    (limit,)
                )
                # Reverse to get chronological order
                return cursor.fetchall()[::-1]
        except Exception as e:
            logger.error(f"Failed to fetch history: {e}")
            return []

db = DatabaseManager()