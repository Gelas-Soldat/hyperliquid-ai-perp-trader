@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo   HyperLiquid Trading Stack Launcher
echo ==========================================
echo.

echo [1/4] Starting trailing stop bot...
start "Trailing Stop Bot" cmd /k py trailing_stop_bot.py

echo Waiting for trailing bot to initialize...
timeout /t 3 /nobreak >nul

echo [2/4] Starting scanner bot...
start "Scanner Bot" cmd /k py scanner_bot.py

echo Waiting for scanner to initialize...
timeout /t 2 /nobreak >nul

echo [3/4] Starting local proxy server...
start "Dashboard Proxy" cmd /k py proxy.py

echo Waiting for proxy to come online...
timeout /t 2 /nobreak >nul

echo [4/4] Opening dashboard...
start "" http://localhost:8081/scalping-dashboard.html

echo.
echo Launch sequence complete.
echo.
echo Order used:
echo   1. trailing_stop_bot.py
echo   2. scanner_bot.py
echo   3. proxy.py
echo   4. dashboard in browser
echo.
echo Press any key to close this launcher window.
pause >nul
