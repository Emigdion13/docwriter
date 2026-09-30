@echo off
setlocal
REM ==========================================================================
REM  VaultNotes - makes the Windows app                             (Milestone M9)
REM
REM  Run it by double-clicking, or from a terminal in this folder:
REM      build.bat
REM
REM  It does four things, in order:
REM    1. .venv            creates it if missing, installs requirements.txt
REM    2. frontend         npm ci + npm run build  ->  src\vaultnotes\web
REM    3. app icon         packaging\make_icon.py redraws the hexagon logo
REM    4. VaultNotes.exe   PyInstaller, --windowed --onedir
REM
REM  Result:  dist\VaultNotes\VaultNotes.exe   (copy the whole folder to move it)
REM
REM  Needs Python 3.12 on THIS machine.  Node.js is used from .venv (installed
REM  there with nodeenv when it is missing), so nothing else goes on the PC.
REM  The finished app needs neither: it only needs Windows 10/11, and Microsoft
REM  Edge WebView2, which Windows 11 has built in.  On Windows 10, if the window
REM  opens blank, install the free "Microsoft Edge WebView2 Runtime" from
REM  Microsoft and try again.
REM ==========================================================================

cd /d "%~dp0"
set "SELF=%~f0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo [1/4]  Python environment
if not exist "%PY%" (
  echo        creating .venv ...
  python -m venv "%~dp0.venv"
  if errorlevel 1 goto :fail
)
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
"%PY%" -m pip install "pyinstaller>=6"
if errorlevel 1 goto :fail

echo.
echo [2/4]  Frontend (npm ci, npm run build)
REM  Node.js lives in .venv like everything else; put it first on PATH so npm
REM  is found when this is double-clicked, not only from an activated shell.
set "PATH=%~dp0.venv\Scripts;%PATH%"
if not exist "%~dp0.venv\Scripts\npm.cmd" (
  echo        installing Node.js LTS into .venv ...
  "%PY%" -m pip install nodeenv
  if errorlevel 1 goto :fail
  set "VIRTUAL_ENV=%~dp0.venv"
  "%PY%" -m nodeenv --python-virtualenv --node=lts --prebuilt
  if errorlevel 1 goto :fail
)
pushd frontend
call npm ci
if errorlevel 1 ( popd & goto :fail )
call npm run build
if errorlevel 1 ( popd & goto :fail )
popd
if not exist "src\vaultnotes\web\index.html" (
  echo        ERROR: src\vaultnotes\web\index.html is missing, so the build stopped.
  goto :fail
)

echo.
echo [3/4]  App icon
"%PY%" packaging\make_icon.py
if errorlevel 1 goto :fail

echo.
echo [4/4]  VaultNotes.exe
REM  --paths src lets PyInstaller find the vaultnotes package: run.py only adds
REM  src\ to sys.path at run time, which PyInstaller cannot see, so without it
REM  the app is built without its own code.
REM  --add-data ships the built frontend inside the app, where vaultnotes.app
REM  looks for it.  --collect-all webview is needed because pywebview picks its
REM  GUI backend (EdgeChromium on Windows) at run time, which PyInstaller cannot
REM  see by following imports; bottle and proxy_tools are that backend's own
REM  runtime dependencies.  --collect-all winpty ships the CMD space's
REM  pseudo-console: pywinpty's extension, conpty.dll and OpenConsole.exe.
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onedir --name VaultNotes --paths src --add-data "src/vaultnotes/web;vaultnotes/web" --icon packaging\vaultnotes.ico --collect-all webview --collect-all winpty --hidden-import bottle --hidden-import proxy_tools run.py
if errorlevel 1 goto :fail

echo.
echo  Built:  dist\VaultNotes\VaultNotes.exe
echo  To check the "no Python, no Node" rule, copy the whole dist\VaultNotes
echo  folder to another Windows PC and start VaultNotes.exe from there.
call :pause_if_double_clicked
exit /b 0

:fail
echo.
echo  Build stopped.  The message above says which step failed.
echo  Common causes: Python not on PATH, no network for "npm ci" or
echo  "pip install", or an open dist\VaultNotes folder (close the app).
call :pause_if_double_clicked
exit /b 1

:pause_if_double_clicked
REM  A double-click runs "cmd /c <this file>" and closes the window at the end,
REM  hiding the result.  Keep it open then; a run from a terminal does not pause.
echo %cmdcmdline% | find /i "%SELF%" >nul
if not errorlevel 1 pause
exit /b 0
