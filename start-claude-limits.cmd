@echo off
rem Launch Claude Limits with no console window attached.
rem pythonw.exe is the windowed Python host; python.exe would leave a black
rem console box open for as long as the tray icon lives.
setlocal

set "PYW=C:\Program Files\Python314\pythonw.exe"
if not exist "%PYW%" (
    for /f "delims=" %%i in ('where pythonw 2^>nul') do set "PYW=%%i"
)
if not exist "%PYW%" (
    echo Could not find pythonw.exe - is Python installed and on PATH?
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0claude_limits.py"
