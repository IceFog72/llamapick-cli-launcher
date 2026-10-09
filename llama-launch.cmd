@echo off
setlocal
where py >nul 2>&1
if errorlevel 1 (
    python "%~dp0llama-launch.py" %*
) else (
    py -3 "%~dp0llama-launch.py" %*
)
exit /b %errorlevel%
