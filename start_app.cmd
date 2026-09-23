@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "APP_PY="
if exist ".venv\Scripts\python.exe" set "APP_PY=%CD%\.venv\Scripts\python.exe"
if not defined APP_PY if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "APP_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if defined APP_PY goto :run
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 app.py --open
    goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
    python app.py --open
    goto :done
)
echo Python 3.12 is required. See README.md for installation commands.
pause
exit /b 1
:run
"%APP_PY%" app.py --open
:done
if errorlevel 1 (
    echo The app could not start. Install requirements.txt using Python 3.12.
    pause
)
