@echo off
REM Launch_Setup.bat
REM
REM Double-click THIS file to start CaveViewer Setup.
REM
REM Why this file exists: Windows blocks .ps1 (PowerShell script) files from
REM running directly when double-clicked, as a security default -- instead
REM of running them, double-clicking a .ps1 normally just opens it in a text
REM editor. This .bat file is what you actually double-click; it launches
REM the real setup script (CaveViewerSetup.ps1, in this same folder) with
REM the flags needed to run it as an actual program instead of opening it
REM as text.
REM
REM -ExecutionPolicy Bypass applies ONLY to this one launch, not to your
REM whole system's PowerShell settings -- it does not change any permanent
REM Windows security setting.

REM Optional runtime tuning for CaveViewer's chunk streaming workers.
REM Leave empty to use the app default auto-detected worker count.
REM Example: set "CAVEVIEWER_IO_WORKERS=4"
set "CAVEVIEWER_IO_WORKERS="

set "IO_WORKERS_ARG="
if not "%CAVEVIEWER_IO_WORKERS%"=="" set "IO_WORKERS_ARG=-IoWorkers %CAVEVIEWER_IO_WORKERS%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %IO_WORKERS_ARG%