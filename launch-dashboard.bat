@echo off
title HyperLiquid AI Perp Trader Launcher
color 0A

echo ==========================================
echo      HyperLiquid AI Perp Trader
echo ==========================================
echo.
echo Select mode:
echo.
echo [1] Paper Trading Suite
echo [2] Live Trading Suite
echo [3] Scanner Only (Paper)
echo [4] Scanner Only (Live)
echo [5] Trailing Only (Paper)
echo [6] Trailing Only (Live)
echo.
set /p choice=Enter option:

if "%choice%"=="1" goto paper
if "%choice%"=="2" goto live
if "%choice%"=="3" goto scannerpaper
if "%choice%"=="4" goto scannerlive
if "%choice%"=="5" goto trailpaper
if "%choice%"=="6" goto traillive

echo Invalid option.
pause
exit

:paper
echo Starting Paper Suite...
start "Paper Scanner" cmd /k python scanner_bot_v1.py
timeout /t 8 >nul
start "Paper Trailing" cmd /k python trailing_stop_bot_v1.py
goto end

:live
echo Starting Live Suite...
echo WARNING: LIVE MODE ENABLED
timeout /t 5 >nul
start "Live Scanner" cmd /k python scanner_bot_v2.py
timeout /t 8 >nul
start "Live Trailing" cmd /k python trailing_stop_bot_v2.py
goto end

:scannerpaper
start "Paper Scanner" cmd /k python scanner_bot_v1.py
goto end

:scannerlive
start "Live Scanner" cmd /k python scanner_bot_v2.py
goto end

:trailpaper
start "Paper Trailing" cmd /k python trailing_stop_bot_v1.py
goto end

:traillive
start "Live Trailing" cmd /k python trailing_stop_bot_v2.py
goto end

:end
echo.
echo Launcher started.
pause
