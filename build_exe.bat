@echo off
REM build_exe.bat - builds dist\WaveAI.exe from wave.spec.
REM Run from inside the wave_project folder, with the SAME venv Wave normally
REM runs in activated (PyInstaller bundles whatever is importable right now -
REM it does not cross-compile or see packages outside the active environment).
REM
REM Optional, set before running this script (see wave.spec / docs\PACKAGING.md):
REM   set WAVE_BUILD_CONSOLE=1        keep a console window for debugging the build
REM   set WAVE_BUILD_FULL_OFFLINE=1   also bundle faster-whisper + sentence-transformers

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python isn't on PATH. Activate your venv first:
    echo   .\wave_env\Scripts\Activate.ps1
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller isn't installed in this environment. Installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: couldn't install PyInstaller. Run: pip install pyinstaller
        pause
        exit /b 1
    )
)

echo.
echo Building WaveAI.exe (this can take a few minutes)...
echo.
python -m PyInstaller wave.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo Build failed - see docs\PACKAGING.md's Troubleshooting section for the
    echo common causes (missing hidden imports, comtypes/pycaw, winsdk).
    pause
    exit /b 1
)

echo.
echo Done: dist\WaveAI.exe
echo.
echo Before you hand this .exe to anyone else: close it, delete the database\,
echo trash\ and wave_system.log that just got created next to it (your own
echo test data), and actually run it from a DIFFERENT folder than this source
echo checkout once, to make sure nothing is secretly depending on files that
echo are only here because you're sitting in the project folder.
pause
