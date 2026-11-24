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
rem 有 tkinter 就开图形界面，没有就退回命令行面板
%PYCMD% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto cli
echo   Opening the window ...
%PYCMD% -m fm8_tcu.gui --port %PORT% --log "..\decisions.csv"
goto done

:cli
echo   tkinter not found, using the console panel instead.
%PYCMD% -m fm8_tcu.app --port %PORT% --overlay --log "..\decisions.csv"
goto done

:no_python
echo   ------------------------------------------------------------
echo   No working Python found. See docs\02 for instructions.
echo   ------------------------------------------------------------

:done
cd /d "%~dp0.."
echo.
echo   ------------------------------------------------------------
echo   Finished. Press any key to close.
echo   ------------------------------------------------------------
pause >nul
