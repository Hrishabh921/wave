import psutil
import platform
import logging
import sys

logger = logging.getLogger("SysMonitor")

class SystemMonitor:
    """Fetches real-time system resource usage and hardware specs."""
    
    @staticmethod
    def get_stats() -> dict:
        try:
            battery = psutil.sensors_battery()
            battery_percent = battery.percent if battery else "N/A"
            is_plugged = battery.power_plugged if battery else False

            return {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "ram_percent": psutil.virtual_memory().percent,
                "ram_used_gb": round(psutil.virtual_memory().used / (1024**3), 1),
                "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
                "disk_percent": psutil.disk_usage('/').percent,
                "battery_percent": battery_percent,
                "is_plugged": is_plugged
            }
        except Exception as e:
            logger.error(f"Error fetching system stats: {e}")
            return {}

    @staticmethod
    def get_hardware_info() -> str:
        """Fetches static hardware details of the PC with Windows 11 fix."""
        try:
            os_system = platform.system()
            os_release = platform.release()
            
            # Windows 11 Fix: Check build number because Microsoft kept internal version as 10
            if os_system == "Windows" and os_release == "10":
                if hasattr(sys, 'getwindowsversion'):
                    if sys.getwindowsversion().build >= 22000:
                        os_release = "11"

            os_info = f"{os_system} {os_release}"
            cpu_info = platform.processor()
            ram_total = round(psutil.virtual_memory().total / (1024**3), 2)
            disk_total = round(psutil.disk_usage('/').total / (1024**3), 2)
            
            info = (
                f"Here is your actual system information:\n\n"
                f"🖥️ **Operating System:** {os_info}\n"
                f"🧠 **Processor (CPU):** {cpu_info}\n"
                f"⚡ **Total RAM:** {ram_total} GB\n"
                f"💾 **System Drive Capacity:** {disk_total} GB\n"
                f"🏷️ **PC Name:** {platform.node()}"
            )
            return info
        except Exception as e:
            logger.error(f"Error fetching hardware info: {e}")
            return "Sorry, I encountered an error while fetching your hardware details."