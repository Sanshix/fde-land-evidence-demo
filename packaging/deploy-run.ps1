# Windows PowerShell 5.1; all restoration uses local files, never pip or network.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$transcribing = $false
$exitCode = 1
$probe = 'import sys,struct,sqlite3,ssl,ctypes,fastapi,uvicorn,httpx,openpyxl,PIL.Image,pypdfium2,pydantic_core,multipart; assert sys.version_info[:2]==(3,12); assert struct.calcsize(chr(80))==8; print(sys.version)'

function Test-Runtime([string]$runtimeRoot) {
    $python = Join-Path $runtimeRoot 'python.exe'
    if (-not (Test-Path -LiteralPath $python)) { return $false }
    try {
        $result = & $python -X utf8 -c $probe 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Test-Application {
    $manifestPath = Join-Path $root 'offline\application-files.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { return $false }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $false }
    foreach ($required in $manifest.required_files) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $required))) { return $false }
    }
    $checked = 0
    foreach ($item in $manifest.files.PSObject.Properties) {
        # Only release-managed source is immutable. User configuration and data are preserved.
        if ($item.Name.StartsWith('app/') -or $item.Name -eq 'packaging/launcher.py') {
            $path = Join-Path $root $item.Name
            if (-not (Test-Path -LiteralPath $path)) { return $false }
            if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Value) {
                return $false
            }
            $checked++
        }
    }
    return ($checked -gt 0)
}

function Repair-Deployment {
    $payload = Join-Path $root 'offline\deployment.zip'
    $checksum = Join-Path $root 'offline\deployment.sha256'
    if (-not (Test-Path -LiteralPath $payload) -or -not (Test-Path -LiteralPath $checksum)) {
        throw 'Offline deployment files are missing. Extract the complete new ZIP again.'
    }
    $expected = (Get-Content -LiteralPath $checksum -Raw).Trim()
    if ((Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'Offline deployment ZIP checksum failed. Copy or download the complete ZIP again.'
    }
    $stage = Join-Path $root ('.repair-' + [guid]::NewGuid().ToString('N'))
    $runtime = Join-Path $root 'runtime'
    $old = Join-Path $stage 'previous-runtime'
    try {
        Expand-Archive -LiteralPath $payload -DestinationPath $stage
        if (-not (Test-Runtime (Join-Path $stage 'runtime'))) {
            throw 'Bundled runtime cannot run on this Windows computer. Check Windows x64 support and security software.'
        }
        if (Test-Path -LiteralPath $runtime) { Move-Item -LiteralPath $runtime -Destination $old }
        try {
            Move-Item -LiteralPath (Join-Path $stage 'runtime') -Destination $runtime
            if (-not (Test-Runtime $runtime)) { throw 'Runtime verification failed after restoration.' }
        } catch {
            if (Test-Path -LiteralPath $runtime) { Remove-Item -LiteralPath $runtime -Recurse -Force }
            if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination $runtime }
            throw
        }
        foreach ($folder in @('app', 'packaging', 'scripts')) {
            $source = Join-Path $stage $folder
            foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
                $relative = $file.FullName.Substring($stage.Length + 1)
                $target = Join-Path $root $relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $file.FullName -Destination $target -Force
            }
        }
        # Never overwrite business data, active provider settings, .env or user accounts.
        foreach ($folder in @('config', '练习集')) {
            $source = Join-Path $stage $folder
            foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
                $relative = $file.FullName.Substring($stage.Length + 1)
                $target = Join-Path $root $relative
                if (-not (Test-Path -LiteralPath $target)) {
                    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                    Copy-Item -LiteralPath $file.FullName -Destination $target
                }
            }
        }
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'This package requires Windows x64 (Intel/AMD).'
    }
    $writable = Join-Path $root ('.write-test-' + [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($writable, 'ok')
    Remove-Item -LiteralPath $writable
    New-Item -ItemType Directory -Path (Join-Path $root 'logs') -Force | Out-Null
    $log = Join-Path $root ('logs\startup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Start-Transcript -LiteralPath $log -Force | Out-Null
    $transcribing = $true
    Write-Host 'FDE launcher v20261029.3 - checking offline deployment...'
    if (-not (Test-Runtime (Join-Path $root 'runtime')) -or -not (Test-Application)) {
        Write-Host 'Deployment incomplete. Restoring bundled runtime and application offline...'
        Repair-Deployment
        if (-not (Test-Runtime (Join-Path $root 'runtime')) -or -not (Test-Application)) {
            throw 'Deployment verification failed. Extract the complete new ZIP into a new writable directory.'
        }
    } else {
        Write-Host 'Deployment OK. Starting directly.'
    }
    if ($env:FDE_PORT -eq '8765') {
        Write-Host 'Legacy FDE_PORT=8765 detected; using 18765 with automatic fallback.'
        $env:FDE_PORT = '18765'
    }
    $python = Join-Path $root 'runtime\python.exe'
    # Existing databases are migrated idempotently by launcher; never deleted or reseeded.
    & $python -X utf8 (Join-Path $root 'packaging\launcher.py') --auto-port
    $exitCode = $LASTEXITCODE
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Keep this window open. Check logs; no business database has been deleted.'
    $exitCode = 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
}
exit $exitCode
