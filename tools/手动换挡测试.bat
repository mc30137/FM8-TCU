@echo off
chcp 65001 >nul
title FM8 Manual Shift Test
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
%PYCMD% -c "import sys; print('   Python', sys.version.split()[0])"
echo.
echo   An overlay window will appear at the top-left of the screen.
echo   It stays on top and lets mouse clicks pass through to the game.
echo   If the game is in EXCLUSIVE FULLSCREEN the overlay cannot show:
echo   switch the game to BORDERLESS / WINDOWED mode.
echo.
echo   Keys:  F6 up   F5 down   F7 dry-run/live   F12 quit
echo.
%PYCMD% tools\manual_shift.py --port %PORT% --overlay
goto done

:no_python
echo   ------------------------------------------------------------
echo   No working Python found. See docs\02 for instructions.
echo   ------------------------------------------------------------

:done
popd
echo.
echo   ------------------------------------------------------------
echo   Finished. Press any key to close.
echo   ------------------------------------------------------------
pause >nul
