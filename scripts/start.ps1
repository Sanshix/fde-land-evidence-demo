# Windows 开发机启动（PowerShell）。正式部署仍建议 Linux 服务器 + scripts/install.sh。
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Error "先创建虚拟环境：py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -r requirements.in" }
if (-not $env:FDE_HOST) { $env:FDE_HOST = "127.0.0.1" }
if (-not $env:FDE_PORT) { $env:FDE_PORT = "8765" }
if ($env:FDE_HOST -notin @("127.0.0.1", "localhost") -and (-not $env:FDE_USERS_FILE -or -not $env:FDE_ALLOWED_ORIGINS)) {
  Write-Error "监听非本机地址需同时设置 FDE_USERS_FILE 和 FDE_ALLOWED_ORIGINS"
}
& $python -m uvicorn app.main:app --host $env:FDE_HOST --port $env:FDE_PORT --workers 1
