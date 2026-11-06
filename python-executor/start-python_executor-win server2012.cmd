@echo off
setlocal
chcp 866 >nul
python "%~dp0server\bootstrap.py"
set "executor_exit=%ERRORLEVEL%"
if not "%executor_exit%"=="0" (
    echo Server preparation or startup failed. See the error above.
    pause
)
exit /b %executor_exit%
