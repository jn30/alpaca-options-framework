@echo off
REM Runs the spread bot forever; relaunches it 30s after a crash.
REM Exit code 2 = refused to start (wrong STRATEGY / PAPER off) -> stop, do not loop.
cd /d "%~dp0"
:loop
python spread_bot.py
set RC=%errorlevel%
if %RC%==2 (
    echo [run_spreads] refused to start ^(exit 2^), not restarting.
    exit /b 2
)
echo [run_spreads] exited with code %RC%, restarting in 30s...
timeout /t 30 /nobreak >nul
goto loop
