import speech_recognition as sr
import logging
import pyaudio
from voice.tts import tts_engine

logger = logging.getLogger("STT")

class SpeechReceiver:
    """Auto-Detects Headphones vs Speakers for Dynamic Sensitivity."""
    
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.recognizer.non_speaking_duration = 0.3
        self.recognizer.dynamic_energy_threshold = False
        
        logger.info("Calibrating mic for accurate recognition...")
        with sr.Microphone() as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)

    def is_headphone_connected(self) -> bool:
        """🚨 NAYA: Checks Windows audio to see if headphones are plugged in 🚨"""
        try:
            p = pyaudio.PyAudio()
            default_host_api = p.get_default_host_api_info()
            default_output = p.get_device_info_by_index(default_host_api.get('defaultOutputDevice'))
            device_name = default_output.get('name', '').lower()
            p.terminate()
            
            # Agar system ke audio device ke naam mein ye words hain, matlab headphone laga hai
            keywords = ['headphone', 'bluetooth', 'ear', 'airpods', 'buds', 'hands-free']
            return any(kw in device_name for kw in keywords)
        except Exception as e:
            return False

    def listen(self) -> str:
        with sr.Microphone() as source:
            try:
                # 🚨 DYNAMIC SMART SENSITIVITY LOGIC 🚨
                if self.is_headphone_connected():
                    # =============== HEADPHONE MODE ===============
                    # No speaker-echo risk here, so we stay sensitive even
                    # while W.A.V.E. is talking - a shorter pause threshold
                    # while it's speaking makes an interruption register faster.
                    self.recognizer.energy_threshold = 400
                    self.recognizer.pause_threshold = 0.35 if tts_engine.is_speaking else 0.5
                else:
                    # =============== LAPTOP SPEAKER MODE ===============
                    if tts_engine.is_speaking:
                        # Raised (not deafened) - just enough that W.A.V.E.'s
                        # own voice leaking through the speakers doesn't
                        # self-trigger, while a normal-volume interruption
                        # still gets through without needing to shout.
                        self.recognizer.energy_threshold = 1800
                        # Shorter pause threshold here too, so an interrupt
                        # attempt is recognized as "done talking" quickly.
                        self.recognizer.pause_threshold = 0.35
                    else:
                        # Normal aawaz sunne ke liye sensitive ho jao
                        self.recognizer.energy_threshold = 200
                        # Speaker mode mein delay thoda zyada rakha taaki echo catch na ho
                        self.recognizer.pause_threshold = 0.6

                # Background listening
                audio = self.recognizer.listen(source, timeout=1, phrase_time_limit=8)
                
                # 🚨 FIX: Pehle confirm karo ki ye REAL user speech hai, tabhi AI ko
                # chup karao. Pehle audio capture hote hi turant stop() ho jaata tha,
                # jisse TTS ki apni hi awaaz mic me leak hoke "self-interrupt" kar
                # deti thi aur lambe jawab beech mein kat jaate the.
                text = self.recognizer.recognize_google(audio, language="en-IN")
                if text.strip():
                    tts_engine.stop()  # Ab sirf confirmed real speech par hi rokenge
                logger.info(f"Recognized: {text}")
                return text
                
            except sr.WaitTimeoutError:
                return ""
            except sr.UnknownValueError:
                return ""
            except Exception as e:
                return ""

stt_engine = SpeechReceiver()