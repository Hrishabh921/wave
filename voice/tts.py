import asyncio
import logging
import os
import re
import tempfile
import threading

import edge_tts
import pygame

logger = logging.getLogger("TTS")


class VoiceEngine:
    """TTS engine using Microsoft Edge's free online neural voices (edge-tts)
    for natural, human-sounding speech - a big step up from the robotic
    default Windows SAPI voice this used to use. Playback goes through
    pygame's mixer instead of SAPI's own playback, because pygame can be
    stopped INSTANTLY at any point (not just between sentences), which is
    what makes interrupting W.A.V.E. while it's talking actually feel
    responsive.

    If there's no internet (or Microsoft's service is briefly unreachable),
    this falls back to the old offline Windows voice automatically, so
    W.A.V.E. never just goes silent.
    """

    # Any voice name from `edge-tts --list-voices` works here. A few natural
    # alternatives worth trying: "en-IN-PrabhatNeural" (Indian English,
    # male), "en-US-AriaNeural" / "en-US-GuyNeural" (US English).
    VOICE = "en-IN-NeerjaNeural"
    RATE = "+8%"   # slightly brisker than default - reads as more responsive
    VOLUME = "+0%"

    def __init__(self):
        self.stop_event = threading.Event()
        self.speak_thread = None
        self.is_speaking = False  # Tracked so STT can avoid picking up our own voice
        self._seq = 0
        self._tmp_dir = tempfile.mkdtemp(prefix="wave_tts_")
        try:
            pygame.mixer.init()
        except Exception as e:
            logger.error(f"pygame mixer init failed: {e}")

    def speak(self, text: str):
        if not text:
            return

        self.stop()
        if self.speak_thread and self.speak_thread.is_alive():
            self.speak_thread.join(timeout=0.3)

        self.stop_event.clear()
        self.speak_thread = threading.Thread(target=self._speak_task, args=(text,), daemon=True)
        self.speak_thread.start()

    def _speak_task(self, text: str):
        self.is_speaking = True
        mp3_path = None
        try:
            clean_text = text.replace("*", "").replace("#", "").replace("_", "").strip()
            if not clean_text or self.stop_event.is_set():
                return

            self._seq += 1
            mp3_path = os.path.join(self._tmp_dir, f"seg_{self._seq}.mp3")

            try:
                asyncio.run(self._synthesize(clean_text, mp3_path))
            except Exception as e:
                logger.warning(f"edge-tts unavailable ({e}), falling back to offline voice")
                self._speak_offline_fallback(clean_text)
                return

            if self.stop_event.is_set() or not os.path.exists(mp3_path):
                return

            pygame.mixer.music.load(mp3_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if self.stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                pygame.time.wait(40)

        except Exception as e:
            logger.error(f"TTS Error: {e}")
        finally:
            self.is_speaking = False
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
            if mp3_path:
                try:
                    os.remove(mp3_path)
                except OSError:
                    pass

    async def _synthesize(self, text: str, out_path: str):
        communicate = edge_tts.Communicate(text, self.VOICE, rate=self.RATE, volume=self.VOLUME)
        await communicate.save(out_path)

    def _speak_offline_fallback(self, text: str):
        """No internet / Microsoft's TTS endpoint unreachable - use the old
        local Windows voice so W.A.V.E. can still speak instead of staying
        silent. Same sentence-by-sentence approach as before, since SAPI
        (unlike pygame) can only be interrupted cleanly between sentences."""
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Rate = 1

            voices = speaker.GetVoices()
            for i in range(voices.Count):
                if "Zira" in voices.Item(i).GetDescription():
                    speaker.Voice = voices.Item(i)
                    break

            sentences = re.split(r"(?<=[.!?]) +|\n+", text)
            for sentence in sentences:
                if self.stop_event.is_set():
                    break
                if sentence.strip():
                    speaker.Speak(sentence, 1)
                    while not self.stop_event.is_set():
                        if speaker.WaitUntilDone(10):
                            break
                    if self.stop_event.is_set():
                        speaker.Speak("", 2)
                        break
            pythoncom.CoUninitialize()
        except Exception as e:
            logger.error(f"Offline fallback TTS also failed: {e}")

    def stop(self):
        self.stop_event.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass


tts_engine = VoiceEngine()