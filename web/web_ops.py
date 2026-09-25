import webbrowser
import logging
import pywhatkit 

logger = logging.getLogger("WebManager")

class WebManager:
    
    @classmethod
    def search_google(cls, query: str) -> str:
        try:
            webbrowser.open(f"https://www.google.com/search?q={query}")
            return f"Searched Google for {query}."
        except Exception as e:
            logger.error(f"Google search failed: {e}")
            return "Failed to search Google."

    @classmethod
    def play_on_youtube(cls, query: str) -> str:
        try:
            # Ye function direct YouTube par pehla result dhundhega aur Play kar dega!
            pywhatkit.playonyt(query)
            return f"Playing {query} directly on YouTube."
        except Exception as e:
            logger.error(f"YouTube play failed: {e}")
            return "Failed to play on YouTube."
            
    @classmethod
    def ask_internet(cls, query: str) -> str:
        try:
            from duckduckgo_search import DDGS
            results = DDGS().text(query, max_results=2)
            if results:
                return " ".join([res.get("body", "") for res in results])
            return ""
        except Exception as e:
            logger.error(f"Internet search failed: {e}")
            return ""