@echo off
rem Piyasa Masasi - masaustu uygulamasini baslatir (pencere kapaninca uygulama da kapanir).
chcp 65001 >nul
cd /d "%~dp0"
py -m marketdesk.main %*
if errorlevel 1 pause
