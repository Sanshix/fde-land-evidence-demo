$ErrorActionPreference='Stop'
$source=Join-Path (Split-Path -Parent $PSScriptRoot) 'packaging/deploy-run.ps1'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Syntax errors'}
foreach($function in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false)) {
    Invoke-Expression $function.Extent.Text
}
# Runtime execution is stubbed; repair code and filesystem operations are real.
function Test-Runtime([string]$runtimeRoot) {
    $file=Join-Path $runtimeRoot 'python.exe'
    return ((Test-Path -LiteralPath $file) -and (Get-Content -LiteralPath $file -Raw) -eq 'good')
}
$root=Join-Path ([IO.Path]::GetTempPath()) ('fde repair 中文 '+[guid]::NewGuid().ToString('N'))
$seed=Join-Path $root 'seed'
try {
    foreach($dir in @('offline','data','config','seed/runtime','seed/app','seed/packaging','seed/scripts','seed/config','seed/练习集')) {
        New-Item -ItemType Directory -Path (Join-Path $root $dir) -Force | Out-Null
    }
    [IO.File]::WriteAllText((Join-Path $seed 'runtime/python.exe'),'good')
    [IO.File]::WriteAllText((Join-Path $seed 'app/main.py'),'release-source')
    [IO.File]::WriteAllText((Join-Path $seed 'packaging/launcher.py'),'release-launcher')
    [IO.File]::WriteAllText((Join-Path $seed 'scripts/test.py'),'release-script')
    [IO.File]::WriteAllText((Join-Path $seed 'config/providers.json'),'default-config')
    [IO.File]::WriteAllText((Join-Path $seed '练习集/base.xlsx'),'sample')
    [IO.File]::WriteAllText((Join-Path $root 'data/fde.sqlite'),'existing-business-data')
    [IO.File]::WriteAllText((Join-Path $root '.env'),'existing-secret')
    [IO.File]::WriteAllText((Join-Path $root 'config/providers.json'),'custom-config')
    $files=@{}
    foreach($name in @('app/main.py','packaging/launcher.py')) {
        $files[$name]=(Get-FileHash -LiteralPath (Join-Path $seed $name) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    @{files=$files;required_files=@('config/providers.json','练习集/base.xlsx')} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'offline/application-files.json') -Encoding utf8
    Compress-Archive -Path (Join-Path $seed '*') -DestinationPath (Join-Path $root 'offline/deployment.zip')
    (Get-FileHash -LiteralPath (Join-Path $root 'offline/deployment.zip') -Algorithm SHA256).Hash.ToLowerInvariant() | Set-Content -LiteralPath (Join-Path $root 'offline/deployment.sha256')
    if(Test-Application){throw 'Missing application should fail checks'}
    Repair-Deployment
    if(-not (Test-Application)){throw 'Repair failed application verification'}
    if(-not (Test-Runtime (Join-Path $root 'runtime'))){throw 'Repair failed runtime verification'}
    foreach($entry in @(@('data/fde.sqlite','existing-business-data'),@('.env','existing-secret'),@('config/providers.json','custom-config'))) {
        if((Get-Content -LiteralPath (Join-Path $root $entry[0]) -Raw) -ne $entry[1]){throw 'User state overwritten'}
    }
    [IO.File]::WriteAllText((Join-Path $root 'app/main.py'),'damaged')
    if(Test-Application){throw 'Source corruption not detected'}
    Repair-Deployment
    if(-not (Test-Application)){throw 'Corrupt source was not restored'}
    [IO.File]::WriteAllText((Join-Path $root 'offline/deployment.sha256'),'bad-hash')
    $rejected=$false
    try { Repair-Deployment } catch { $rejected=$true }
    if(-not $rejected){throw 'Bad payload checksum was accepted'}
    'PASS: missing environment restored; damaged application repaired; data/.env/custom config preserved; bad checksum rejected (PowerShell 7 macOS, runtime probe stubbed)'
} finally {
    if(Test-Path -LiteralPath $root){Remove-Item -LiteralPath $root -Recurse -Force}
}
