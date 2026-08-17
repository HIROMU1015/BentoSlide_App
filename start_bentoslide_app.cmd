@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_bentoslide_app.ps1" %*
set "BENTO_EXIT=%ERRORLEVEL%"
if not "%BENTO_EXIT%"=="0" if not "%BENTO_EDITOR_NO_PAUSE%"=="1" pause
exit /b %BENTO_EXIT%
