@echo off
rem ===========================================================================
rem  NIOS - 1-click launch (Windows)
rem
rem  1. Resolves a Python interpreter (NIOS_PYTHON > NIOS_VENV > .venv > venv
rem     > the pre-provisioned dev venv > py -3.12 > python).
rem  2. Selects a bindable port.  If NIOS_PORT was forced it is honoured or the
rem     launch fails loudly; otherwise a busy 8000 falls back to the first free
rem     port in 8000..8010 (Operator Note #2) and the exact live URL is shown.
rem  3. Runs the 17-gate release smoke test.  Any failure aborts the launch.
rem  4. Starts the unified 4-ring production server
rem     (REST + SSE + MCP + the compiled React 19 cockpit at /).
rem
rem  Usage:
rem    start_nios.bat                 smoke test, then serve
rem    start_nios.bat --skip-smoke    serve only
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
title NIOS - 4-Ring Sovereign Trading System

cd /d "%~dp0"

if not defined NIOS_HOST set "NIOS_HOST=127.0.0.1"

rem --- 1. resolve the interpreter ------------------------------------------
set "PYTHON="
set "PYTHON_ARGS="

if defined NIOS_PYTHON if exist "%NIOS_PYTHON%" set "PYTHON=%NIOS_PYTHON%"
if not defined PYTHON if defined NIOS_VENV if exist "%NIOS_VENV%\Scripts\python.exe" set "PYTHON=%NIOS_VENV%\Scripts\python.exe"
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON if exist "%~dp0venv\Scripts\python.exe" set "PYTHON=%~dp0venv\Scripts\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Temp\opencode\nios-venv\Scripts\python.exe" set "PYTHON=%LOCALAPPDATA%\Temp\opencode\nios-venv\Scripts\python.exe"

if not defined PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON=py"
        set "PYTHON_ARGS=-3.12"
    )
)
if not defined PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
    echo.
    echo [NIOS] FATAL: no Python interpreter found.
    echo        Create a virtualenv and point NIOS_VENV at it, e.g.
    echo            py -3.12 -m venv .venv
    echo            set NIOS_VENV=%%CD%%\.venv
    echo.
    exit /b 1
)

rem --- preflight: the interpreter must actually carry the NIOS runtime -------
"%PYTHON%" %PYTHON_ARGS% -c "import fastapi, uvicorn, pydantic, numpy" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [NIOS] FATAL: the selected interpreter cannot import the NIOS runtime.
    echo        Missing one of: fastapi, uvicorn, pydantic, numpy.
    echo        Point NIOS_PYTHON at the project interpreter, e.g.
    echo            set NIOS_PYTHON=%%CD%%\.venv\Scripts\python.exe
    echo.
    exit /b 1
)

rem --- 2. select a bindable port (Operator Note #2) -------------------------
if defined NIOS_PORT goto port_forced

set "NIOS_PORT=8000"
set "NIOS_PORT_FILE=%TEMP%\nios-port-%RANDOM%%RANDOM%.txt"
"%PYTHON%" %PYTHON_ARGS% scripts\nios_port_check.py %NIOS_HOST% --scan 8000 8010 > "%NIOS_PORT_FILE%" 2>nul
if errorlevel 1 (
    del "%NIOS_PORT_FILE%" >nul 2>nul
    echo.
    echo [NIOS] FATAL: no free port in 8000..8010 on %NIOS_HOST%.
    echo.
    exit /b 1
)
set /p NIOS_PORT=<"%NIOS_PORT_FILE%"
del "%NIOS_PORT_FILE%" >nul 2>nul
if not defined NIOS_PORT (
    echo.
    echo [NIOS] FATAL: the port scan produced no port.
    echo.
    exit /b 1
)
if not "%NIOS_PORT%"=="8000" echo [NIOS] port 8000 is busy -^> auto-selected %NIOS_PORT%
goto port_ready

:port_forced
"%PYTHON%" %PYTHON_ARGS% scripts\nios_port_check.py %NIOS_HOST% %NIOS_PORT% >nul 2>nul
if errorlevel 1 (
    echo.
    echo [NIOS] FATAL: %NIOS_HOST%:%NIOS_PORT% is already in use ^(NIOS_PORT was forced^).
    echo        Free the port, or unset NIOS_PORT to auto-select 8000..8010.
    echo.
    exit /b 1
)

:port_ready
echo.
echo  NIOS ==========================================================
echo   interpreter : %PYTHON% %PYTHON_ARGS%
echo   cockpit     : http://%NIOS_HOST%:%NIOS_PORT%
echo  ===============================================================

if /i "%~1"=="--skip-smoke" goto serve

rem --- 3. release smoke test ------------------------------------------------
echo.
echo [NIOS] 1/2  Release smoke test ^(17 gates^)...
echo.
"%PYTHON%" %PYTHON_ARGS% scripts\run_nios.py --smoke-test --host %NIOS_HOST% --port %NIOS_PORT%
if errorlevel 1 (
    echo.
    echo [NIOS] FATAL: smoke test failed. Refusing to launch a non-certified build.
    echo.
    exit /b 1
)

rem --- 4. serve ------------------------------------------------------------
:serve
echo.
echo [NIOS] 2/2  Launching unified production server on http://%NIOS_HOST%:%NIOS_PORT%
echo        Open the cockpit in a browser, or ^<Ctrl+C^> to stop.
echo.
"%PYTHON%" %PYTHON_ARGS% scripts\run_nios.py --host %NIOS_HOST% --port %NIOS_PORT%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [NIOS] Server exited with code %RC%.
) else (
    echo [NIOS] Server stopped cleanly. Kill latch state is persisted in the workspace.
)
endlocal & exit /b %RC%
