@echo off
REM One-time setup: start the bot automatically when you log in to Windows,
REM and stop the PC from sleeping while plugged in. Run by double-click.
cd /d "%~dp0"
schtasks /Create /F /TN "AlpacaOptionsBot" /SC ONLOGON /RL LIMITED /TR "cmd /c \"%~dp0start.bat\"" 
if errorlevel 1 ( echo Could not create the task. & pause & exit /b 1 )
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo.
echo Done. The bot now starts at every login. Remove with:  schtasks /Delete /TN AlpacaOptionsBot /F
pause
