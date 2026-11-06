@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PF_ROOT=%~dp0"
set "PF_VENV=%PF_ROOT%.venv"
set "PF_PYTHON=%PF_VENV%\Scripts\python.exe"

if not exist "%PF_PYTHON%" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
        if errorlevel 1 goto python_missing
        py -3 -m venv "%PF_VENV%"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 goto python_missing
        python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
        if errorlevel 1 goto python_missing
        python -m venv "%PF_VENV%"
    )
    if errorlevel 1 goto setup_failed
)

"%PF_PYTHON%" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto python_venv_old

if not exist "%PF_VENV%\Lib\site-packages\paramfabric_studio-0.6.0.dist-info" (
    echo Setting up Paramfabric on this computer. This can take a few minutes...
    "%PF_PYTHON%" -m pip install --disable-pip-version-check -e ".[desktop]"
    if errorlevel 1 goto setup_failed
)

"%PF_PYTHON%" -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo Installing the native desktop window...
    "%PF_PYTHON%" -m pip install --disable-pip-version-check -e ".[desktop]"
    if errorlevel 1 goto setup_failed
)

set "PF_FABRIC=%PF_ROOT%params.yaml"
if not exist "%PF_FABRIC%" set "PF_FABRIC=%PF_ROOT%examples\params.yaml"

echo Starting Paramfabric Studio...
"%PF_PYTHON%" -m paramfabric.desktop --fabric "%PF_FABRIC%"
if errorlevel 1 goto launch_failed
exit /b 0

:python_missing
echo Paramfabric needs Python 3.11 or newer. Install Python from https://www.python.org/downloads/
echo Then close this window and double-click this file again.
pause
exit /b 1

:python_venv_old
echo The existing .venv uses Python older than 3.11. Rename or remove that environment, then click this file again.
pause
exit /b 1

:setup_failed
echo Paramfabric setup failed. Check your Python installation and internet connection, then try again.
pause
exit /b 1

:launch_failed
echo Paramfabric Studio could not start. Check the error above and try again.
pause
exit /b 1
