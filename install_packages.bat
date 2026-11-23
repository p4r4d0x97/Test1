@echo off
REM Installs the Python packages from the "wheels" folder - no internet needed.
cd /d "%~dp0"
python --version >nul 2>&1 || (
  echo Python is not installed or not in PATH.
  echo Install Python 64-bit first and tick "Add Python to PATH".
  pause
  exit /b 1
)
python -m pip install --no-index --find-links "%~dp0wheels" -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo INSTALL FAILED - Python must be 3.11, 3.12 or 3.13, 64-bit.
) else (
  echo.
  echo OK - packages installed. Start the app with start.bat
)
pause
