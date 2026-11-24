@echo off
chcp 65001 >nul
title FM8 Telemetry Probe (read only)
pushd "%~dp0.."

set "PORT=5300"
set "PYCMD="

echo   Working folder: %CD%
echo   Looking for a working Python ...
echo.

rem 注意：不能用 "where python" 判断。Windows 10/11 自带一个微软商店的
rem python.exe 存根，它存在于 PATH 里但运行后什么都不做。必须真的跑一次。

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto found

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto found

python3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=python3"
if defined PYCMD goto found

goto no_python

:found
%PYCMD% -c "import sys; print('   Python', sys.version.split()[0], '->', sys.executable)"
if errorlevel 1 goto no_python
echo   When you are done, press Q in this window to see the summary.
echo.
%PYCMD% tools\probe.py --port %PORT%
goto done

:no_python
echo   ------------------------------------------------------------
echo   No working Python found on this computer.
echo.
echo   Diagnostics:
where python
where py
echo.
echo   Fix:
echo   1. Open  https://www.python.org/downloads/
echo   2. Download Python 3.11 or newer, run the installer
echo   3. IMPORTANT: on the first screen tick "Add python.exe to PATH"
echo   4. Reboot, then double-click this file again
echo   ------------------------------------------------------------
goto done

:done
popd
echo.
echo   ------------------------------------------------------------
echo   Probe stopped.
echo   If NO telemetry panel appeared above, Python did not run the
echo   probe. Screenshot this whole window and send it back.
echo   ------------------------------------------------------------
echo.
pause >nul
