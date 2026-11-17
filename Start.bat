@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Расчёты с подрядчиками
where py >nul 2>nul
python run.py %*
echo.
echo Окно можно закрыть — сервер остановится.
pause >nul
