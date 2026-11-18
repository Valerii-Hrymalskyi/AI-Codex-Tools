@echo off
rem Double-click launcher for python_setup.ps1 (installs the portable Python into Utils\Bin\Windows\Python3).
rem Arguments are passed through, e.g.:  python_setup.bat -Force
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0python_setup.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Done.) else (echo Failed with exit code %RC%.)
rem Keep the window open when started by a double-click.
pause
exit /b %RC%
