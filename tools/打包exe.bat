@echo off
chcp 65001 >nul
title Build FM8 AutoShift EXE
pushd "%~dp0.."

set "PYCMD="
echo   Looking for a working Python ...
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto found
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto found
goto no_python

:found
%PYCMD% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tk

rem 锁定版本：不锁的话，半年后重打包会得到不一样的东西，
rem 而"用户跑的到底是哪一版"就再也说不清了。
set "PYI_VER=6.22.3"
echo   [1/3] installing PyInstaller %PYI_VER% (skip if already installed) ...
%PYCMD% -m pip install --disable-pip-version-check -q "pyinstaller==%PYI_VER%"
if errorlevel 1 goto pip_fail

echo   [2/4] version resource + building. this takes about a minute ...
%PYCMD% tools\make_version_info.py build\version_info.txt
if errorlevel 1 goto build_fail
%PYCMD% -m PyInstaller --noconfirm --onefile --windowed --name "FM8自动换挡" ^
    --paths src --distpath dist --workpath build ^
    --version-file build\version_info.txt run_gui.py
if errorlevel 1 goto build_fail

rem 把二进制和提交绑定：出问题时能回答"用户跑的是哪一版"。
set "APPVER=unknown"
for /f "delims=" %%v in ('%PYCMD% -c "import sys; sys.path.insert(0, 'src'); from fm8_tcu import __version__; print(__version__)"') do set "APPVER=%%v"
set "GITREV=unknown"
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set "GITREV=%%i"
> "dist\version.txt" echo version %APPVER%  commit %GITREV%  built %DATE% %TIME%
>> "dist\version.txt" echo pyinstaller %PYI_VER%
>> "dist\version.txt" echo test  python tests\*.py  (313 assertions)

echo   [3/4] wrote dist\version.txt  (version %APPVER%, commit %GITREV%)

echo   [4/4] done.
echo.
echo   The exe is here:  %CD%\dist\FM8自动换挡.exe
echo   You can copy that single file anywhere and double-click it.
echo   It writes profiles\ and decisions.csv next to itself.
echo   If that folder is read-only (e.g. Program Files), profiles
echo   move to %%LOCALAPPDATA%%\FM8AutoShift automatically.
goto done

:no_python
echo   No working Python found.
goto done

:no_tk
echo   This Python has no tkinter, so the GUI cannot be built.
echo   Reinstall Python from python.org and tick "tcl/tk and IDLE".
goto done

:pip_fail
echo   pip install failed. If you are behind a proxy, run first:
echo       set HTTP_PROXY=http://127.0.0.1:PORT
echo       set HTTPS_PROXY=http://127.0.0.1:PORT
goto done

:build_fail
echo   Build failed. Read the messages above.

:done
popd
echo.
pause >nul
