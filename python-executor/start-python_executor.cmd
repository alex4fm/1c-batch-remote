@echo off
setlocal
chcp 65001 >nul
python "%~dp0server\bootstrap.py"
set "executor_exit=%ERRORLEVEL%"
if not "%executor_exit%"=="0" (
    echo Не удалось подготовить или запустить сервер. Передайте текст ошибки разработчику.
    pause
)
exit /b %executor_exit%
