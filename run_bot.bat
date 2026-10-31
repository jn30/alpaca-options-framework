@echo off
REM Launches the bot and relaunches it when it asks for a restart (exit code 75:
REM 'r' key or watchdog). Any other exit code (q, Ctrl+C, crash) stops the loop.
REM Pick the strategy with STRATEGY in config.py or:  set BOT_STRATEGY=directional
cd /d "%~dp0"
:loop
python main.py
if %errorlevel%==75 (
    echo [run_bot] restart requested, relaunching in 3s...
    timeout /t 3 /nobreak >nul
    goto loop
)
echo [run_bot] bot exited with code %errorlevel%.
