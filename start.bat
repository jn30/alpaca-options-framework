@echo off
REM One-click launcher. Runs whichever strategy config.py selects (STRATEGY),
REM or override for this run:  set BOT_STRATEGY=directional  (before calling).
cd /d "%~dp0"
for /f %%s in ('python -c "import config;print(config.STRATEGY)"') do set STRAT=%%s
for /f %%s in ('python -c "import config;print('submit' if config.CONDOR_SUBMIT else 'dry')"') do set MODE=%%s

echo Strategy: %STRAT%
if /i "%STRAT%"=="directional" (
    call "%~dp0run_bot.bat"
) else if /i "%STRAT%"=="condor" (
    echo Condor mode: %MODE%  ^(CONDOR_SUBMIT in config.py^)
    call "%~dp0run_condor.bat"
) else (
    echo Unknown STRATEGY "%STRAT%" in config.py
)
echo.
pause
