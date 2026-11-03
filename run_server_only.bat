@echo off
rem Piyasa Masasi - yalnizca sunucu (pencere yok). Tarayicida http://127.0.0.1:8765/ adresini acin. Kapatmak icin Ctrl+C.
chcp 65001 >nul
cd /d "%~dp0"
py -m marketdesk.main --no-window %*
if errorlevel 1 pause
