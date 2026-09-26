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
REM  Needs Python 3.12 and Node.js LTS on THIS machine.  The finished app needs
REM  neither: it only needs Windows 10/11, and Microsoft Edge WebView2, which
REM  Windows 11 has built in.  On Windows 10, if the window opens blank, install
REM  the free "Microsoft Edge WebView2 Runtime" from Microsoft and try again.
REM ==========================================================================

cd /d "%~dp0"
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
REM  --add-data ships the built frontend inside the app, where vaultnotes.app
REM  looks for it.  --collect-all webview is needed because pywebview picks its
REM  GUI backend (EdgeChromium on Windows) at run time, which PyInstaller cannot
REM  see by following imports; bottle and proxy_tools are that backend's own
REM  runtime dependencies.
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onedir --name VaultNotes --add-data "src/vaultnotes/web;vaultnotes/web" --icon packaging\vaultnotes.ico --collect-all webview --hidden-import bottle --hidden-import proxy_tools run.py
if errorlevel 1 goto :fail

echo.
echo  Built:  dist\VaultNotes\VaultNotes.exe
echo  To check the "no Python, no Node" rule, copy the whole dist\VaultNotes
echo  folder to another Windows PC and start VaultNotes.exe from there.
exit /b 0

:fail
echo.
echo  Build stopped.  The message above says which step failed.
echo  Common causes: Node.js or Python not on PATH, no network for "npm ci"
echo  or "pip install", or an open dist\VaultNotes folder (close the app).
exit /b 1
