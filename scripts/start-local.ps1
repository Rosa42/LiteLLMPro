# Compatibility wrapper. Prefer the full manager:
#   .\scripts\llm-router.ps1 start
# Docs: docs/配置套餐与启动.md

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
& (Join-Path $Root "scripts\llm-router.ps1") start @args
exit $LASTEXITCODE
