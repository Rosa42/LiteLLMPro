@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title LiteLLMPro - Starting

echo.
echo ============================================================
echo   LiteLLMPro Local LLM Router
echo   API: http://127.0.0.1:4000/v1
echo ============================================================
echo.

call :ensure_docker
if errorlevel 1 goto :failed

call :check_environment
if errorlevel 1 goto :failed

echo [3/4] Starting Docker services...
echo        First-time image builds may take several minutes.
echo.
docker compose --env-file .env -f deploy\docker-compose.yaml --profile core up -d --build
if errorlevel 1 (
    echo.
    echo [ERROR] Docker Compose failed. Review the output above.
    goto :failed
)

call :wait_for_proxy
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo   LiteLLMPro is ready
echo   API Base: http://127.0.0.1:4000/v1
echo   API Key : LITELLM_MASTER_KEY from .env
echo ============================================================
echo.
echo Commands:
echo   Logs  : docker compose --env-file .env -f deploy\docker-compose.yaml --profile core logs -f litellm
echo   Status: docker compose --env-file .env -f deploy\docker-compose.yaml --profile core ps
echo   Stop  : stop-liteLLMPro.bat
echo.
title LiteLLMPro - Running
pause
exit /b 0

:ensure_docker
echo [1/4] Checking Docker engine...
docker info >nul 2>&1
if not errorlevel 1 (
    echo        Docker engine is ready.
    exit /b 0
)

set "DOCKER_DESKTOP="
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" set "DOCKER_DESKTOP=C:\Program Files\Docker\Docker\Docker Desktop.exe"
if not defined DOCKER_DESKTOP if exist "%LocalAppData%\Docker\Docker Desktop.exe" set "DOCKER_DESKTOP=%LocalAppData%\Docker\Docker Desktop.exe"

if not defined DOCKER_DESKTOP (
    echo [ERROR] Docker Desktop was not found.
    echo         Install it from https://www.docker.com/products/docker-desktop/
    exit /b 1
)

tasklist /FI "IMAGENAME eq Docker Desktop.exe" 2>nul | find /I "Docker Desktop.exe" >nul
if errorlevel 1 (
    echo        Starting Docker Desktop...
    start "" "%DOCKER_DESKTOP%"
) else (
    echo        Docker Desktop is running; waiting for its engine...
)

set /a docker_waited=0
:docker_wait_loop
docker info >nul 2>&1
if not errorlevel 1 (
    echo        Docker engine is ready after !docker_waited! seconds.
    exit /b 0
)

if !docker_waited! geq 180 (
    echo [ERROR] Docker engine did not become ready within 180 seconds.
    echo         Open Docker Desktop to inspect its status, then retry.
    exit /b 1
)

ping 127.0.0.1 -n 4 >nul
set /a docker_waited+=3
if !docker_waited! equ 30 echo        Still waiting for Docker engine...
if !docker_waited! equ 60 echo        Still waiting for Docker engine...
if !docker_waited! equ 120 echo        Still waiting for Docker engine...
goto :docker_wait_loop

:check_environment
echo [2/4] Checking configuration...
if not exist ".env" (
    echo [ERROR] Missing .env file.
    echo         Run scripts\llm-router.ps1 init and configure your keys.
    exit /b 1
)

if not exist "config\litellm.yaml" (
    echo [ERROR] Missing config\litellm.yaml file.
    echo         Run scripts\llm-router.ps1 apply first.
    exit /b 1
)

echo        Configuration files are present.
exit /b 0

:wait_for_proxy
echo.
echo [4/4] Waiting for LiteLLMPro health check...
set /a proxy_waited=0

:proxy_wait_loop
curl.exe -fsS --max-time 2 http://127.0.0.1:4000/health/liveliness >nul 2>&1
if not errorlevel 1 (
    echo        LiteLLMPro health check passed.
    exit /b 0
)

docker compose --env-file .env -f deploy\docker-compose.yaml --profile core ps -a 2>nul | findstr /I "unhealthy exited" >nul
if not errorlevel 1 (
    echo [ERROR] One or more services stopped or became unhealthy.
    docker compose --env-file .env -f deploy\docker-compose.yaml --profile core ps -a
    echo.
    echo Recent LiteLLM logs:
    docker compose --env-file .env -f deploy\docker-compose.yaml --profile core logs --tail 30 litellm
    exit /b 1
)

if !proxy_waited! geq 180 (
    echo [ERROR] LiteLLMPro did not become healthy within 180 seconds.
    docker compose --env-file .env -f deploy\docker-compose.yaml --profile core ps -a
    echo.
    echo Recent LiteLLM logs:
    docker compose --env-file .env -f deploy\docker-compose.yaml --profile core logs --tail 30 litellm
    exit /b 1
)

ping 127.0.0.1 -n 4 >nul
set /a proxy_waited+=3
if !proxy_waited! equ 30 echo        Still waiting for LiteLLMPro...
if !proxy_waited! equ 60 echo        Still waiting for LiteLLMPro...
if !proxy_waited! equ 120 echo        Still waiting for LiteLLMPro...
goto :proxy_wait_loop

:failed
echo.
echo Startup failed. See the error above.
pause
exit /b 1
