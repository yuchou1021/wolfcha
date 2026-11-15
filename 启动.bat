@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0.pylibs
set PORT=8777

echo ========================================
echo   宿舍狼人杀 - 4 真人 + AI
echo ========================================
echo.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do echo   手机访问: http://%%b:8777
)
echo.
echo   本机访问: http://127.0.0.1:8777
echo ========================================
echo.
python main.py
pause
