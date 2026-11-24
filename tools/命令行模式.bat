@echo off
chcp 65001 >nul
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
cd /d "%~dp0..\src"
echo   Console panel mode (no window).
echo   F11 = enable / disable    F12 = quit
echo.
%PYCMD% -m fm8_tcu.app --port %PORT% --overlay --log "..\decisions.csv"
goto done

:no_python
echo   No working Python found.

:done
cd /d "%~dp0.."
pause >nul
