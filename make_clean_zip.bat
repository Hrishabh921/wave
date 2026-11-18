@echo off
REM make_clean_zip.bat - just runs clean_zip.py and keeps the window open
python "%~dp0clean_zip.py"
if errorlevel 1 (
    echo.
    echo Something failed. Try: py "%~dp0clean_zip.py"
    pause
)