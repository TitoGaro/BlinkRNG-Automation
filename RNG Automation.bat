@echo off
rem Opens RNG Automation without a console window. / Abre RNG Automation sin consola.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo The .venv environment was not found. Install first:
  echo No se encuentra el entorno .venv. Instala primero:
  echo.
  echo     py -m venv .venv
  echo     .venv\Scripts\python -m pip install -e .
  echo.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m rngauto.gui.main
