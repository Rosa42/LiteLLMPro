@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   Stopping LiteLLMPro services...
echo   (litellm + redis + quota-worker)
echo.

set "COMPOSE=docker compose --env-file .env -f deploy/docker-compose.yaml"
if exist deploy/docker-compose.minimax-host-bridge.yaml (
  set "COMPOSE=%COMPOSE% -f deploy/docker-compose.minimax-host-bridge.yaml"
)

%COMPOSE% --profile core down 2>&1

if %errorlevel% equ 0 (
    echo.
    echo   LiteLLMPro stopped.
) else (
    echo.
    echo   [WARN] Stop may have encountered issues. Check with:
    echo          docker compose --env-file .env -f deploy/docker-compose.yaml --profile core ps
)

echo.
pause
