@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

echo.
echo ================================================================
echo   HDU Library Instant Booking - Web Console Launcher
echo ================================================================
echo.
echo [Step 1/5] Locating Python interpreter ...

:: --- Find a working Python ---
:: "python3" on Windows is often a Microsoft Store stub. We try
:: "python" first, skip paths containing "WindowsApps", and verify
:: the found interpreter actually works.
set "PYTHON_BIN="
set "PYTHON_FULL="
for %%p in (python python3) do (
    if not defined PYTHON_BIN (
        echo   Trying: %%p
        for /f "delims=" %%i in ('where %%p 2^>nul') do (
            if not defined PYTHON_BIN (
                set "CANDIDATE=%%i"
                :: Skip Microsoft Store stubs (contain "WindowsApps" in path)
                if "!CANDIDATE:WindowsApps=!"=="!CANDIDATE!" (
                    set "PYTHON_FULL=!CANDIDATE!"
                    set "PYTHON_BIN=%%p"
                    echo     Selected: !CANDIDATE!
                ) else (
                    echo     Skipped Store stub: !CANDIDATE!
                )
            )
        )
    )
)

if not defined PYTHON_BIN (
    echo.
    echo   [FAIL] No working Python found.
    echo.
    echo   Troubleshooting:
    echo     - Install Python from https://www.python.org/downloads/
    echo     - During installation, check "Add Python to PATH"
    echo     - If already installed, open a NEW Command Prompt and try again
    echo.
    pause
    exit /b 1
)

:: --- Verify the found Python actually runs ---
echo   Verifying ...
"%PYTHON_FULL%" --version >nul 2>&1
if errorlevel 1 (
    echo   [FAIL] Python at "%PYTHON_FULL%" cannot execute.
    echo   Try running "python" directly in a new Command Prompt.
    pause
    exit /b 1
)
echo   Python works: OK

echo.
echo [Step 2/5] Checking project files ...

:: --- Verify essential files ---
set "MISSING="
if not exist "web_app.py"      set "MISSING=!MISSING!  web_app.py"
if not exist "config.yaml"     set "MISSING=!MISSING!  config.yaml"
if not exist "instant_book.py" set "MISSING=!MISSING!  instant_book.py"
if defined MISSING (
    echo   [WARN] Missing files:!MISSING!
    echo   Project directory: %CD%
    echo   Make sure start_web.bat is in the project root folder.
) else (
    echo   All essential files present.
)

:: --- Clean stale port file ---
if exist ".port" (
    set /p STALE_PORT=<".port"
    echo   Removed stale .port file - was port !STALE_PORT!
    del ".port" >nul 2>&1
)

echo.
echo [Step 3/5] Checking Python dependencies ...
"%PYTHON_FULL%" -c "import yaml, requests" 2>nul
if errorlevel 1 (
    echo   [WARN] Missing dependencies detected.
    echo   Run: "%PYTHON_FULL%" -m pip install -r requirements.txt
    echo.
) else (
    echo   Dependencies - yaml, requests: OK
)

echo.
echo [Step 4/5] Starting web server ...

:: --- Port pre-scan ---
set "START_PORT=8765"
echo   Default port: !START_PORT!
"%PYTHON_FULL%" -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',!START_PORT!)); s.close(); exit(r)" 2>nul
if errorlevel 1 (
    echo   Port !START_PORT! is already in use - server will auto-switch.
) else (
    echo   Port !START_PORT! is available.
)

:: --- Background launcher: opens browser when server is ready ---
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & if exist .port for /f %%i in (.port) do start http://127.0.0.1:%%i/"

echo   Browser launcher: started - will open when server is ready.
echo.
echo [Step 5/5] Running server in foreground ...
echo ================================================================
echo.
echo   Server output will appear below. Press Ctrl+C to stop.
echo   Manual access:  http://127.0.0.1:!START_PORT!
echo.
echo ================================================================
echo.

:: --- Start server ---
set PYTHONDONTWRITEBYTECODE=1
"%PYTHON_FULL%" web_app.py --port !START_PORT!
set "EXIT_CODE=!ERRORLEVEL!"

:: --- Server exited ---
echo.
echo ================================================================
if !EXIT_CODE! neq 0 (
    echo   Server exited with error code: !EXIT_CODE!
    echo.
    echo   To see detailed errors, run this command manually:
    echo     "%PYTHON_FULL%" web_app.py --port !START_PORT!
    echo.
    echo   Common issues:
    echo     - Ports 8765-8784 all occupied by other programs
    echo     - config.yaml has invalid YAML syntax
    echo     - Missing Python dependency
) else (
    echo   Server stopped.
)
echo ================================================================

if exist ".port" del ".port" >nul 2>&1

echo.
pause
popd
