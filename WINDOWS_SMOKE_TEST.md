# Windows smoke test

The current automated runner is Linux and has no PyQt6, pygame, sounddevice, Piper, or physical audio devices installed. A real Windows device/UI test cannot honestly be marked passed from that environment.

On the Windows PC where Wave will run:

1. Extract this archive to a new folder (keep the previous project as a backup).
2. Open PowerShell in that folder and activate the same virtual environment used for Wave.
3. Install project dependencies with `python -m pip install -r requirements.txt`.
4. Run `python scripts/windows_smoke_check.py` and save its console output.
5. Launch `python wave_start.pyw` and confirm the PyQt6 window opens, renders, and closes normally.
6. In Wave's voice settings, select the intended microphone/speaker and perform a short manual microphone command and audible TTS reply. Confirm the correct devices are used and there are no crashes. The script deliberately does not record audio or play a tone automatically.
7. If building the executable, run `build_exe.bat`, launch the resulting EXE, and repeat the UI/audio check from the packaged build.

A passing script only verifies device enumeration, pygame mixer initialization, and Qt MainWindow construction. It does not replace a real spoken-command/playback check.
