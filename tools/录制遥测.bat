@echo off
chcp 65001 >nul
title FM8 Telemetry Probe (record)
pushd "%~dp0.."

set "PORT=5300"
set "PYCMD="

echo   Working folder: %CD%
echo   Looking for a working Python ...
echo.

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
%PYCMD% tools\probe.py --port %PORT% --record
goto done

:no_python
echo   ------------------------------------------------------------
echo   No working Python found on this computer.
echo   See docs\02-阶段2-探针使用说明.md for instructions.
echo   ------------------------------------------------------------

:done
popd
echo.
echo   ------------------------------------------------------------
echo   Recording is saved in the folder shown above as fm8_*.fm8rec
echo   Send that file back for analysis.
echo   ------------------------------------------------------------
echo.
pause >nul
