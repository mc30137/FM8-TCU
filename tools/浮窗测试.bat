@echo off
chcp 65001 >nul
title Overlay Self Test
pushd "%~dp0.."

set "PYCMD="

where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tk
set "PYCMD=py -3"
goto run

:try_python
python -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tk
set "PYCMD=python"
goto run

:no_tk
echo   ------------------------------------------------------------
echo   This Python does not have tkinter (the GUI module).
echo   Reinstall Python from python.org and tick "tcl/tk and IDLE".
echo   ------------------------------------------------------------
goto done

:run
echo   A borderless box will appear near the top-left corner for 8 seconds,
echo   then a normal window with a title bar for another 8 seconds.
echo.
%PYCMD% tools\overlay.py --test

:done
echo.
echo   ------------------------------------------------------------
echo   Tell the developer: did you see A? did you see B?
echo   Press any key to close.
echo   ------------------------------------------------------------
pause >nul
