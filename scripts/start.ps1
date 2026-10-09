$ErrorActionPreference = 'Stop'
$sentinelProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $sentinelProjectDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '需要 uv。也可安装 requirements.txt，然后运行 python -m uvicorn backend.app.main:app。'
}

# Windows and WSL must not share a Python virtual environment.
if ([string]::IsNullOrWhiteSpace($env:UV_PROJECT_ENVIRONMENT)) {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $sentinelProjectDir '.venv-win'
}

$sentinelListenHost = if ($env:SENTINEL_HOST) { $env:SENTINEL_HOST } else { '127.0.0.1' }
$sentinelListenPort = if ($env:SENTINEL_PORT) { $env:SENTINEL_PORT } else { '8000' }

# The backend reads .env; this script does not create or overwrite it.
& uv run uvicorn backend.app.main:app --host $sentinelListenHost --port $sentinelListenPort
exit $LASTEXITCODE
