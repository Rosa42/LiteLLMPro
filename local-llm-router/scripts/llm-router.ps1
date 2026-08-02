# LiteLLMPro local-llm-router manager (Windows PowerShell 5.1+)
# Docs: docs/配置套餐与启动.md
#
# Usage:
#   .\scripts\llm-router.ps1 help
#   .\scripts\llm-router.ps1 init
#   .\scripts\llm-router.ps1 add-plan -Id volc-d -BaseUrl "https://..." -ApiKey "ark-xxx" -Models "glm-5.2,ark-code-latest" -Priority 20
#   .\scripts\llm-router.ps1 apply
#   .\scripts\llm-router.ps1 start | stop | restart | status
#   .\scripts\llm-router.ps1 smoke -Model glm-5.2

[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("init", "start", "stop", "restart", "status", "apply", "add-plan", "set-env", "smoke", "help")]
  [string]$Command = "help",

  [string]$Id,
  [string]$BaseUrl,
  [string]$ApiKey,
  [string]$Models = "ark-code-latest,glm-5.2",
  [int]$Priority = 10,
  [string]$ProviderId = "volcengine",
  [string]$BaseUrlEnv,
  [string]$ApiKeyEnv,
  [string]$DisplayName,

  [string]$Model = "glm-5.2",
  [string]$Prompt = "ping",
  [switch]$NoOpenCodeEnv
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Paths = @{
  Root        = $Root
  Env         = Join-Path $Root ".env"
  EnvExample  = Join-Path $Root ".env.example"
  Plans       = Join-Path $Root "config\plans.yaml"
  PlansEx     = Join-Path $Root "config\plans.example.yaml"
  LiteLLM     = Join-Path $Root "config\litellm.yaml"
  Callback    = Join-Path $Root "plugins\shared_quota_callback.py"
  CallbackCfg = Join-Path $Root "config\shared_quota_callback.py"
  RedisDir    = Join-Path $Root ".tools\redis"
  VenvLite    = Join-Path $Root ".venv\Scripts\litellm.exe"
  Log         = Join-Path $Root ".tools\litellm-local.log"
  Err         = Join-Path $Root ".tools\litellm-local.err.log"
  PidFile     = Join-Path $Root ".tools\litellm.pid"
}

function Write-Info([string]$Msg) { Write-Host ("[INFO] " + $Msg) -ForegroundColor Cyan }
function Write-Ok([string]$Msg)   { Write-Host ("[ OK ] " + $Msg) -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host ("[WARN] " + $Msg) -ForegroundColor Yellow }
function Write-ErrMsg([string]$Msg) { Write-Host ("[ERR ] " + $Msg) -ForegroundColor Red }

function Load-DotEnv {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return }
  Get-Content $Path -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $k = $line.Substring(0, $i).Trim()
    $v = $line.Substring($i + 1).Trim()
    Set-Item -Path ("Env:" + $k) -Value $v
  }
}

function Get-EnvValue([string]$Key) {
  if (-not (Test-Path $Paths.Env)) { return $null }
  $escaped = [regex]::Escape($Key)
  $line = Get-Content $Paths.Env -Encoding UTF8 | Where-Object { $_ -match ("^" + $escaped + "=") } | Select-Object -First 1
  if ($line) { return ($line.Substring($Key.Length + 1)) }
  return $null
}

function Set-EnvFileValue {
  param([string]$Key, [string]$Value)
  $lines = @()
  if (Test-Path $Paths.Env) {
    $lines = @(Get-Content $Paths.Env -Encoding UTF8)
  }
  $found = $false
  $out = New-Object System.Collections.Generic.List[string]
  foreach ($line in $lines) {
    if ($line -match ("^" + [regex]::Escape($Key) + "=")) {
      $out.Add("$Key=$Value") | Out-Null
      $found = $true
    } else {
      $out.Add($line) | Out-Null
    }
  }
  if (-not $found) { $out.Add("$Key=$Value") | Out-Null }
  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllLines($Paths.Env, $out.ToArray(), $utf8NoBom)
}

function Ensure-Prereqs {
  if (-not (Test-Path $Paths.Env)) {
    throw ".env missing. Run: .\scripts\llm-router.ps1 init"
  }
  if (-not (Test-Path $Paths.VenvLite)) {
    throw ("Missing " + $Paths.VenvLite + ". Create venv and: pip install 'litellm[proxy]==1.90.5'")
  }
  if (-not (Test-Path $Paths.CallbackCfg) -and (Test-Path $Paths.Callback)) {
    Copy-Item $Paths.Callback $Paths.CallbackCfg -Force
  }
}

function ConvertFrom-SimpleYamlPlans {
  param([string]$Path)
  if (-not (Test-Path $Path)) { throw ("plans file missing: " + $Path) }

  # Use PSCustomObject (NOT hashtable). Hashtables enumerate as key/value pairs
  # in PowerShell pipelines and merge multi-plan fields into garbage YAML.
  $plans = New-Object System.Collections.Generic.List[object]
  $cur = $null
  $inModels = $false

  foreach ($raw in (Get-Content $Path -Encoding UTF8)) {
    $line = $raw.TrimEnd()
    if ($line -match '^\s*#' -or $line.Trim() -eq "") { continue }
    if ($line -match '^\s*plans\s*:') { continue }

    if ($line -match '^\s*-\s+id\s*:\s*(.+)$') {
      if ($null -ne $cur) { [void]$plans.Add($cur) }
      $cur = [pscustomobject]@{
        id           = $Matches[1].Trim().Trim('"').Trim("'")
        display_name = ""
        provider_id  = "volcengine"
        priority     = 10
        base_url_env = ""
        api_key_env  = ""
        models       = New-Object System.Collections.Generic.List[string]
      }
      $inModels = $false
      continue
    }
    if ($null -eq $cur) { continue }

    if ($line -match '^\s+models\s*:') { $inModels = $true; continue }

    if ($inModels -and $line -match '^\s+-\s+(.+)$') {
      $m = $Matches[1].Trim().Trim('"').Trim("'")
      if (-not $m.StartsWith("#")) { [void]$cur.models.Add($m) }
      continue
    }

    if ($line -match '^\s+\w') { $inModels = $false }

    if ($line -match '^\s+display_name\s*:\s*(.+)$') {
      $cur.display_name = $Matches[1].Trim().Trim('"').Trim("'"); continue
    }
    if ($line -match '^\s+provider_id\s*:\s*(.+)$') {
      $cur.provider_id = $Matches[1].Trim().Trim('"').Trim("'"); continue
    }
    if ($line -match '^\s+priority\s*:\s*(\d+)') {
      $cur.priority = [int]$Matches[1]; continue
    }
    if ($line -match '^\s+base_url_env\s*:\s*(.+)$') {
      $cur.base_url_env = $Matches[1].Trim().Trim('"').Trim("'"); continue
    }
    if ($line -match '^\s+api_key_env\s*:\s*(.+)$') {
      $cur.api_key_env = $Matches[1].Trim().Trim('"').Trim("'"); continue
    }
  }
  if ($null -ne $cur) { [void]$plans.Add($cur) }
  if ($plans.Count -eq 0) { throw "No plans parsed from plans.yaml (check indent and '- id:')" }

  # Return as List wrapper so caller always sees discrete plan objects
  $out = New-Object System.Collections.ArrayList
  foreach ($p in $plans) { [void]$out.Add($p) }
  return , $out
}

function ConvertTo-AsciiSafe([string]$Text) {
  # LiteLLM on Chinese Windows opens yaml with locale (GBK). Keep generated file ASCII-only.
  if ([string]::IsNullOrEmpty($Text)) { return "" }
  $sb = New-Object System.Text.StringBuilder
  foreach ($ch in $Text.ToCharArray()) {
    $code = [int]$ch
    if ($code -ge 32 -and $code -le 126) {
      [void]$sb.Append($ch)
    } elseif ($ch -eq "`t") {
      [void]$sb.Append(" ")
    } else {
      [void]$sb.Append("_")
    }
  }
  return $sb.ToString()
}

function New-LiteLLMYamlFromPlans {
  param($Plans)

  # IMPORTANT: ASCII only. Windows Python often opens config as GBK; UTF-8 punctuation
  # (em-dash, CJK in comments) causes UnicodeDecodeError before yaml parses.
  $lines = New-Object System.Collections.Generic.List[string]
  $lines.Add("# AUTO-GENERATED by scripts/llm-router.ps1 apply - do not hand-edit model_list.") | Out-Null
  $lines.Add("# Source: config/plans.yaml | Secrets: .env only") | Out-Null
  $lines.Add("# Shared Quota strategy via LITELLM_WORKER_STARTUP_HOOKS bootstrap.") | Out-Null
  $lines.Add("") | Out-Null
  $lines.Add("model_list:") | Out-Null

  foreach ($p in $Plans) {
    $qg = ConvertTo-AsciiSafe ([string]$p.id)
    $prio = $p.priority
    $prov = ConvertTo-AsciiSafe ([string]$p.provider_id)
    $baseEnv = ConvertTo-AsciiSafe ([string]$p.base_url_env)
    $keyEnv = ConvertTo-AsciiSafe ([string]$p.api_key_env)
    if (-not $baseEnv -or -not $keyEnv) {
      throw ("plan " + $p.id + " missing base_url_env or api_key_env")
    }
    if ($p.models.Count -eq 0) {
      throw ("plan " + $p.id + " has no models")
    }
    $title = ConvertTo-AsciiSafe ([string]$p.display_name)
    if (-not $title) { $title = $qg }
    $lines.Add("") | Out-Null
    $lines.Add(("  # --- {0} (quota_group: {1}, priority: {2}) ---" -f $title, $qg, $prio)) | Out-Null
    foreach ($model in $p.models) {
      $modelName = ConvertTo-AsciiSafe ([string]$model)
      $depId = ("{0}-{1}" -f $qg, $modelName) -replace '[^a-zA-Z0-9._-]', '-'
      $lines.Add(("  - model_name: {0}" -f $modelName)) | Out-Null
      $lines.Add("    model_info:") | Out-Null
      $lines.Add(("      deployment_id: {0}" -f $depId)) | Out-Null
      $lines.Add(("      provider_id: {0}" -f $prov)) | Out-Null
      $lines.Add(("      account_id: {0}" -f $qg)) | Out-Null
      $lines.Add(("      quota_group_id: {0}" -f $qg)) | Out-Null
      $lines.Add(("      priority: {0}" -f $prio)) | Out-Null
      $lines.Add("    litellm_params:") | Out-Null
      $lines.Add(("      model: openai/{0}" -f $modelName)) | Out-Null
      $lines.Add(("      api_base: os.environ/{0}" -f $baseEnv)) | Out-Null
      $lines.Add(("      api_key: os.environ/{0}" -f $keyEnv)) | Out-Null
      $lines.Add("      timeout: 300") | Out-Null
      $lines.Add("") | Out-Null
    }
  }

  $lines.Add("router_settings:") | Out-Null
  $lines.Add("  routing_strategy: simple-shuffle") | Out-Null
  $lines.Add("  num_retries: 2") | Out-Null
  $lines.Add("  allowed_fails: 1") | Out-Null
  $lines.Add("  cooldown_time: 30") | Out-Null
  $lines.Add("  retry_after: 1") | Out-Null
  $lines.Add("") | Out-Null
  $lines.Add("general_settings:") | Out-Null
  $lines.Add("  master_key: os.environ/LITELLM_MASTER_KEY") | Out-Null
  $lines.Add("") | Out-Null
  $lines.Add("litellm_settings:") | Out-Null
  $lines.Add("  callbacks:") | Out-Null
  $lines.Add("    - shared_quota_callback.callback_instance") | Out-Null
  $lines.Add("  drop_params: true") | Out-Null

  # ASCII encoding = safe for Windows locale (GBK) open() in LiteLLM
  $ascii = [System.Text.Encoding]::ASCII
  [System.IO.File]::WriteAllLines($Paths.LiteLLM, $lines.ToArray(), $ascii)
}

function New-RandomToken([int]$Bytes = 32) {
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $buf = New-Object byte[] $Bytes
  $rng.GetBytes($buf)
  return ([Convert]::ToBase64String($buf).TrimEnd('=') -replace '[+/]', 'x')
}

function Invoke-Init {
  if (-not (Test-Path $Paths.Env)) {
    if (-not (Test-Path $Paths.EnvExample)) { throw "Missing .env.example" }
    Copy-Item $Paths.EnvExample $Paths.Env
    Set-EnvFileValue -Key "LITELLM_MASTER_KEY" -Value (New-RandomToken 32)
    Set-EnvFileValue -Key "REDIS_PASSWORD" -Value (New-RandomToken 18)
    Set-EnvFileValue -Key "LITELLM_WORKER_STARTUP_HOOKS" -Value "shared_quota_router.bootstrap:register_proxy_startup"
    Write-Ok "Created .env with generated LITELLM_MASTER_KEY / REDIS_PASSWORD"
  } else {
    Write-Info ".env already exists"
  }

  if (-not (Test-Path $Paths.Plans)) {
    if (Test-Path $Paths.PlansEx) {
      Copy-Item $Paths.PlansEx $Paths.Plans
      Write-Ok "Copied config/plans.example.yaml -> config/plans.yaml"
    }
  } else {
    Write-Info "config/plans.yaml already exists"
  }

  if ((Test-Path $Paths.Callback) -and -not (Test-Path $Paths.CallbackCfg)) {
    Copy-Item $Paths.Callback $Paths.CallbackCfg -Force
  }

  Write-Host ""
  Write-Host "Next steps:" -ForegroundColor Yellow
  Write-Host "  1. Edit .env  -> set BASE_URL and KEY for each plan"
  Write-Host "  2. Edit config/plans.yaml -> plan ids, priority, models"
  Write-Host "  3. .\scripts\llm-router.ps1 apply"
  Write-Host "  4. .\scripts\llm-router.ps1 start"
  Write-Host ""
  Write-Host "Or one-shot add-plan:"
  Write-Host '  .\scripts\llm-router.ps1 add-plan -Id volc-c -BaseUrl "https://ark.cn-beijing.volces.com/api/coding/v3" -ApiKey "ark-xxx" -Models "glm-5.2,ark-code-latest" -Priority 10'
}

function Get-PythonExe {
  $candidates = @(
    (Join-Path $Root ".venv\Scripts\python.exe"),
    "python",
    "py"
  )
  foreach ($c in $candidates) {
    if ($c -eq "python" -or $c -eq "py") {
      try {
        $null = & $c -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
      } catch { }
      continue
    }
    if (Test-Path $c) { return $c }
  }
  throw "Python not found. Install Python 3.11+ or create .venv"
}

function Invoke-Apply {
  if (-not (Test-Path $Paths.Plans)) {
    throw "Missing config/plans.yaml. Run init or copy plans.example.yaml."
  }
  # M1-04: Python generator (validate + atomic write + backup). Fail-closed.
  $py = Get-PythonExe
  $env:PYTHONPATH = ((Join-Path $Root "plugins") + ";" + $env:PYTHONPATH)
  $env:PYTHONUTF8 = "1"
  $args = @(
    "-m", "shared_quota_router.cli_config", "apply",
    "--plans", $Paths.Plans,
    "--output", $Paths.LiteLLM,
    "--backup-dir", (Join-Path $Root "config\backups"),
    # Keep Messages→Chat G0-Native when plans still have convert (e.g. kimi-k3)
    "--enable-messages-chat-native"
  )
  & $py @args
  if ($LASTEXITCODE -ne 0) {
    throw "apply failed: plans validation or generation error (previous litellm.yaml left untouched)"
  }
  Write-Ok "Generated config/litellm.yaml from plans.yaml (protocol-aware generator)"
  # Summary via simple YAML plan parse (env presence only; no secrets printed)
  try {
    $parsed = ConvertFrom-SimpleYamlPlans -Path $Paths.Plans
    foreach ($p in $parsed) {
      $base = Get-EnvValue $p.base_url_env
      $key = Get-EnvValue $p.api_key_env
      $baseOk = if ($base) { "base=set" } else { "base=MISSING" }
      $keyOk = if ($key) { "key=set" } else { "key=MISSING" }
      $modelCsv = ($p.models -join ",")
      Write-Host ("  - {0} prio={1} models=[{2}] [{3}, {4}] env={5}/{6}" -f `
        $p.id, $p.priority, $modelCsv, $baseOk, $keyOk, $p.base_url_env, $p.api_key_env)
    }
  } catch {
    Write-Info "plan summary skipped (Python apply already succeeded)"
  }
  Write-Info "After .env / plans change: run restart"
}

function Invoke-AddPlan {
  if (-not $Id) { throw "add-plan requires -Id (e.g. volc-d)" }
  if (-not $BaseUrl) { throw "add-plan requires -BaseUrl" }
  if (-not $ApiKey) { throw "add-plan requires -ApiKey" }

  if (-not (Test-Path $Paths.Env)) { Invoke-Init }

  $safe = ($Id -replace '[^a-zA-Z0-9]+', '_').ToUpperInvariant()
  # Prefer existing env names already used by plans.yaml / .env
  if (-not $BaseUrlEnv -or -not $ApiKeyEnv) {
    if (Test-Path $Paths.Plans) {
      $existing = @(ConvertFrom-SimpleYamlPlans -Path $Paths.Plans) | Where-Object { $_.id -eq $Id } | Select-Object -First 1
      if ($existing) {
        if (-not $BaseUrlEnv) { $BaseUrlEnv = $existing.base_url_env }
        if (-not $ApiKeyEnv) { $ApiKeyEnv = $existing.api_key_env }
      }
    }
  }
  # Well-known aliases
  if ($Id -eq "volc-c" -and -not $BaseUrlEnv) { $BaseUrlEnv = "VOLC_CODING_BASE_URL" }
  if ($Id -eq "volc-c" -and -not $ApiKeyEnv) { $ApiKeyEnv = "VOLC_CODING_KEY_C" }
  if (-not $BaseUrlEnv) { $BaseUrlEnv = "PLAN_${safe}_BASE_URL" }
  if (-not $ApiKeyEnv) { $ApiKeyEnv = "PLAN_${safe}_API_KEY" }
  if (-not $DisplayName) { $DisplayName = $Id }

  # Refuse obvious placeholders
  if ($ApiKey -match '你的|YOUR|xxx|changeme|placeholder' -or $ApiKey.Length -lt 12) {
    Write-Warn "ApiKey looks like a placeholder. Not overwriting real key in .env."
    Write-Warn "Re-run with your real key, e.g. -ApiKey `"ark-....`""
  } else {
    Set-EnvFileValue -Key $BaseUrlEnv -Value $BaseUrl
    Set-EnvFileValue -Key $ApiKeyEnv -Value $ApiKey
    Write-Ok ("Wrote .env keys: {0} / {1}" -f $BaseUrlEnv, $ApiKeyEnv)
  }

  if (-not (Test-Path $Paths.Plans)) {
    if (Test-Path $Paths.PlansEx) { Copy-Item $Paths.PlansEx $Paths.Plans }
    else {
      $utf8NoBom = New-Object System.Text.UTF8Encoding $false
      [System.IO.File]::WriteAllText($Paths.Plans, "plans:`r`n", $utf8NoBom)
    }
  }

  $modelList = @($Models.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $content = Get-Content $Paths.Plans -Raw -Encoding UTF8
  $idPattern = '(?m)^\s*-\s+id\s*:\s*' + [regex]::Escape($Id) + '\s*$'
  if ($content -match $idPattern) {
    Write-Warn ("plans.yaml already has id={0}; .env updated only. Edit models/priority manually then apply." -f $Id)
  } else {
    $modelLines = ($modelList | ForEach-Object { "      - $_" }) -join "`r`n"
    # Default new plans to verified Chat capability (P0). Opt-in public_protocols
    # still required under logical_models (edit plans.yaml after add-plan).
    $block = @"

  - id: $Id
    display_name: $DisplayName
    provider_id: $ProviderId
    priority: $Priority
    base_url_env: $BaseUrlEnv
    api_key_env: $ApiKeyEnv
    upstream_protocol: openai_chat
    supported_features: [text, streaming, tools]
    supports_streaming: true
    models:
$modelLines
"@
    if ($content -notmatch '(?m)^plans\s*:') {
      $content = "plans:`r`n" + $content
      $utf8NoBom = New-Object System.Text.UTF8Encoding $false
      [System.IO.File]::WriteAllText($Paths.Plans, $content, $utf8NoBom)
    }
    Add-Content -Path $Paths.Plans -Value $block.TrimEnd() -Encoding UTF8
    Write-Ok ("Appended plan to config/plans.yaml: {0}" -f $Id)
  }

  Invoke-Apply
}

function Test-PortListening([int]$Port) {
  $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  return ($null -ne $c)
}

function Test-RedisUp {
  $pw = Get-EnvValue "REDIS_PASSWORD"
  if (-not $pw) { $pw = $env:REDIS_PASSWORD }
  if (-not $pw) { return $false }

  # Prefer Python redis client (handles passwords containing "--")
  $py = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $py) {
    try {
      $code = "import redis; r=redis.Redis(host='127.0.0.1',port=6379,password=r'''$pw''',protocol=2,decode_responses=True,socket_connect_timeout=1); print('PONG' if r.ping() else 'NO')"
      $out = & $py -c $code 2>$null
      if ("$out" -match "PONG") { return $true }
    } catch {}
  }

  # Fallback: redis-cli with REDISCLI_AUTH (avoid -a pass--with-dashes issues)
  $cli = Join-Path $Paths.RedisDir "redis-cli.exe"
  if (Test-Path $cli) {
    try {
      $prev = $env:REDISCLI_AUTH
      $env:REDISCLI_AUTH = $pw
      $pong = & $cli -h 127.0.0.1 -p 6379 PING 2>$null
      if ($null -ne $prev) { $env:REDISCLI_AUTH = $prev } else { Remove-Item Env:REDISCLI_AUTH -ErrorAction SilentlyContinue }
      if ("$pong" -match "PONG") { return $true }
    } catch {}
  }

  # Last resort: port open (may be redis without auth check)
  return (Test-PortListening -Port 6379)
}

function Start-RedisLocal {
  if (Test-RedisUp) { Write-Info "Redis already running"; return }
  $exe = Join-Path $Paths.RedisDir "redis-server.exe"
  $conf = Join-Path $Paths.RedisDir "redis.local.conf"
  if (-not (Test-Path $exe)) {
    throw ("Redis binary not found: " + $exe + "  Place redis-server.exe under .tools\redis")
  }
  $pw = Get-EnvValue "REDIS_PASSWORD"
  if (-not $pw) { throw "REDIS_PASSWORD not set in .env" }

  # Free stale listener if any
  Get-NetTCPConnection -LocalPort 6379 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  }
  Get-Process redis-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1

  # Simple conf — no logfile path (Windows redis 5 can fail on logfile= with drive paths)
  $confLines = @(
    "port 6379",
    "bind 127.0.0.1",
    "requirepass $pw",
    "protected-mode yes"
  )
  $confLines | Set-Content -Path $conf -Encoding ascii

  Write-Info "Starting Redis..."
  $rp = Start-Process -FilePath $exe -ArgumentList @($conf) -WorkingDirectory $Paths.RedisDir -PassThru -WindowStyle Minimized
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    if (Test-RedisUp) {
      Write-Ok ("Redis OK (pid={0})" -f $rp.Id)
      return
    }
    if ($rp.HasExited) { break }
  }
  $hint = "Redis failed to start."
  if ($rp.HasExited) { $hint += (" Process exited early (pid={0})." -f $rp.Id) }
  if (Test-PortListening -Port 6379) {
    $hint += " Port 6379 is in use by another process; stop it and retry."
  }
  $hint += " Check that .tools\redis\redis-server.exe runs, and REDIS_PASSWORD in .env is set."
  throw $hint
}

function Stop-PortProcess([int]$Port) {
  Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  }
}

function Invoke-Start {
  Ensure-Prereqs
  Load-DotEnv $Paths.Env

  # Always regenerate litellm.yaml so encoding/content stay valid for Windows LiteLLM
  if (Test-Path $Paths.Plans) {
    try {
      Invoke-Apply
    } catch {
      Write-Warn ("apply skipped: " + $_.Exception.Message)
    }
  }

  # Force UTF-8 for child Python process (extra safety if non-ASCII appears)
  $env:PYTHONUTF8 = "1"
  $env:PYTHONIOENCODING = "utf-8"

  $env:REDIS_HOST = "127.0.0.1"
  $env:REDIS_PORT = "6379"
  $pw = $env:REDIS_PASSWORD
  if (-not $pw) { throw "REDIS_PASSWORD missing" }
  $env:REDIS_URL = "redis://:${pw}@127.0.0.1:6379/0"
  $env:PYTHONPATH = ((Join-Path $Root "plugins") + ";" + (Join-Path $Root "config"))
  if (-not $env:LITELLM_WORKER_STARTUP_HOOKS) {
    $env:LITELLM_WORKER_STARTUP_HOOKS = "shared_quota_router.bootstrap:register_proxy_startup"
  }

  Start-RedisLocal
  Stop-PortProcess -Port 4000
  Start-Sleep -Seconds 1

  New-Item -ItemType Directory -Force -Path (Join-Path $Root ".tools") | Out-Null
  Write-Info "Starting LiteLLM on http://127.0.0.1:4000 ..."
  $p = Start-Process -FilePath $Paths.VenvLite `
    -ArgumentList @("--config", $Paths.LiteLLM, "--port", "4000", "--host", "127.0.0.1") `
    -WorkingDirectory $Root -PassThru -WindowStyle Minimized `
    -RedirectStandardOutput $Paths.Log -RedirectStandardError $Paths.Err
  $p.Id | Set-Content $Paths.PidFile -Encoding ascii

  for ($i = 0; $i -lt 45; $i++) {
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:4000/health/liveliness" -TimeoutSec 2 -UseBasicParsing
      if ($r.StatusCode -eq 200) {
        Write-Ok ("Proxy ready (pid={0})" -f $p.Id)
        $mk = Get-EnvValue "LITELLM_MASTER_KEY"
        if ($mk -and -not $NoOpenCodeEnv) {
          [Environment]::SetEnvironmentVariable("LITELLM_API_KEY", $mk, "User")
          $env:LITELLM_API_KEY = $mk
          Write-Info "Synced User env LITELLM_API_KEY (restart OpenCode to pick up)"
        }
        Write-Host ""
        Write-Host "  API Base : http://127.0.0.1:4000/v1" -ForegroundColor Green
        Write-Host "  API Key  : LITELLM_MASTER_KEY from .env (NOT upstream key)"
        Write-Host "  Models   : GET /v1/models"
        Write-Host "  Logs     : .tools\litellm-local.err.log"
        Write-Host "  OpenCode : baseURL=http://127.0.0.1:4000/v1  model=local-litellm/<name>"
        Write-Host ""
        return
      }
    } catch {}
    if ($p.HasExited) {
      Write-ErrMsg "LiteLLM exited. Recent log:"
      Get-Content $Paths.Err -Tail 40 -ErrorAction SilentlyContinue
      exit 1
    }
    Start-Sleep -Seconds 1
  }
  Write-ErrMsg ("Health check timeout. See " + $Paths.Err)
  exit 1
}

function Invoke-Stop {
  Write-Info "Stopping LiteLLM (port 4000)..."
  Stop-PortProcess -Port 4000
  if (Test-Path $Paths.PidFile) { Remove-Item $Paths.PidFile -Force -ErrorAction SilentlyContinue }
  Write-Ok "Proxy stopped (Redis left running)"
}

function Invoke-Status {
  $alive = $false
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:4000/health/liveliness" -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $alive = $true }
  } catch {}
  if ($alive) { Write-Ok "Proxy: UP  http://127.0.0.1:4000" } else { Write-Warn "Proxy: DOWN" }
  if (Test-RedisUp) { Write-Ok "Redis: UP   127.0.0.1:6379" } else { Write-Warn "Redis: DOWN" }

  if ($alive) {
    $mk = Get-EnvValue "LITELLM_MASTER_KEY"
    if ($mk) {
      try {
        $models = Invoke-RestMethod -Uri "http://127.0.0.1:4000/v1/models" -Headers @{ Authorization = ("Bearer " + $mk) }
        Write-Host ("Models: " + (($models.data | ForEach-Object { $_.id }) -join ", "))
      } catch {
        Write-Warn ("Cannot list models: " + $_.Exception.Message)
      }
    }
  }
}

function Invoke-Smoke {
  Load-DotEnv $Paths.Env
  $mk = Get-EnvValue "LITELLM_MASTER_KEY"
  if (-not $mk) { throw "LITELLM_MASTER_KEY missing" }
  $bodyObj = @{ model = $Model; messages = @(@{ role = "user"; content = $Prompt }); max_tokens = 64 }
  $body = $bodyObj | ConvertTo-Json -Depth 5
  Write-Info ("POST /v1/chat/completions model=" + $Model)
  try {
    $chat = Invoke-RestMethod -Uri "http://127.0.0.1:4000/v1/chat/completions" -Method Post `
      -Headers @{ Authorization = ("Bearer " + $mk); "Content-Type" = "application/json" } `
      -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 120
    Write-Ok ("finish={0} tokens={1}" -f $chat.choices[0].finish_reason, $chat.usage.total_tokens)
    $c = $chat.choices[0].message.content
    if ($c) { Write-Host ("content: " + $c) } else { Write-Host "(content empty; model may use reasoning_content)" }
  } catch {
    Write-ErrMsg $_.Exception.Message
    if ($_.ErrorDetails) { Write-Host $_.ErrorDetails.Message }
    exit 1
  }
}

function Show-Help {
  Write-Host @"
LiteLLMPro local router manager

Commands:
  init       Create .env + config/plans.yaml
  add-plan   Add a plan (writes .env + plans.yaml, then apply)
  apply      Regenerate config/litellm.yaml from plans.yaml
  start      Start Redis + LiteLLM (127.0.0.1:4000)
  stop       Stop LiteLLM
  restart    stop then start
  status     Health + model list
  smoke      One test chat completion
  help       This help

Add a new plan:
  .\scripts\llm-router.ps1 add-plan ``
    -Id volc-d ``
    -BaseUrl "https://ark.cn-beijing.volces.com/api/coding/v3" ``
    -ApiKey "ark-YOUR-KEY" ``
    -Models "glm-5.2,ark-code-latest" ``
    -Priority 20

  .\scripts\llm-router.ps1 restart

Concepts:
  - One plan = one quota_group (shared API key / quota pool)
  - Smaller priority fills first; exhaust switches to higher priority plans
  - models must be supported by that upstream plan (else 404 UnsupportedModel)
  - Client uses base http://127.0.0.1:4000/v1 and LITELLM_MASTER_KEY

Files:
  .env                 secrets
  config/plans.yaml    plan declarations
  config/litellm.yaml  generated (apply)
  docs/配置套餐与启动.md
"@
}

switch ($Command) {
  "help"     { Show-Help }
  "init"     { Invoke-Init }
  "apply"    { Invoke-Apply }
  "add-plan" { Invoke-AddPlan }
  "start"    { Invoke-Start }
  "stop"     { Invoke-Stop }
  "restart"  { Invoke-Stop; Start-Sleep 1; Invoke-Start }
  "status"   { Invoke-Status }
  "smoke"    { Invoke-Smoke }
  "set-env"  {
    if (-not $Id -or -not $BaseUrl -or -not $ApiKey) {
      throw "set-env needs -Id -BaseUrl -ApiKey"
    }
    $safe = ($Id -replace '[^a-zA-Z0-9]+', '_').ToUpperInvariant()
    if (-not $BaseUrlEnv) { $BaseUrlEnv = "PLAN_${safe}_BASE_URL" }
    if (-not $ApiKeyEnv) { $ApiKeyEnv = "PLAN_${safe}_API_KEY" }
    Set-EnvFileValue -Key $BaseUrlEnv -Value $BaseUrl
    Set-EnvFileValue -Key $ApiKeyEnv -Value $ApiKey
    Write-Ok ("Updated .env {0} / {1}" -f $BaseUrlEnv, $ApiKeyEnv)
  }
  default    { Show-Help }
}
