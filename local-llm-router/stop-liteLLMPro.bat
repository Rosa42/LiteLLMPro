@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   Stopping LiteLLMPro services...
echo   (litellm + redis + quota-worker)
echo.

docker compose --env-file .env -f deploy/docker-compose.yaml --profile core down 2>&1

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
