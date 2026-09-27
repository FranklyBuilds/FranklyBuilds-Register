$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $PSScriptRoot '.env'
$envExample = Join-Path $PSScriptRoot '.env.example'
if (-not (Test-Path -LiteralPath $envFile)) {
  Copy-Item -LiteralPath $envExample -Destination $envFile
}
$python = Join-Path $root 'register_env\Scripts\python.exe'
function Invoke-RequiredNative {
  param(
    [Parameter(Mandatory = $true)][string] $FilePath,
    [string[]] $ArgumentList = @()
  )

  & $FilePath @ArgumentList
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    $displayArgs = if ($ArgumentList.Count) { ' ' + ($ArgumentList -join ' ') } else { '' }
    throw "命令失败（退出码 $exitCode）：$FilePath$displayArgs"
  }
}

if (-not (Test-Path -LiteralPath $python)) {
  Invoke-RequiredNative -FilePath 'python' -ArgumentList @('-m', 'venv', (Join-Path $root 'register_env'))
}
if (-not (Test-Path -LiteralPath $python)) {
  throw "Python environment was not created: $python"
}

# Always install through the same interpreter that runs the main service. Calling a
# separately-resolved launcher can target a different Python/cache revision.
Invoke-RequiredNative -FilePath $python -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-RequiredNative -FilePath $python -ArgumentList @('-m', 'pip', 'install', '-r', 'requirements.txt', '-r', 'requirements-dev.txt')
Invoke-RequiredNative -FilePath $python -ArgumentList @('-m', 'patchright', 'install', 'chromium')
Invoke-RequiredNative -FilePath 'npm.cmd' -ArgumentList @('ci')

$mongoUriLine = Get-Content -LiteralPath $envFile |
  Where-Object { $_ -match '^AUTOREGISTER_MONGO_URI=' } |
  Select-Object -Last 1
$mongoUri = if ($mongoUriLine) { ($mongoUriLine -split '=', 2)[1].Trim() } else { '' }
if (-not $mongoUri -or $mongoUri -match 'mongodb(?:\+srv)?://(?:[^@/]+@)?(?:127\.0\.0\.1|localhost)(?::27017)?(?:/|$)') {
  & (Join-Path $PSScriptRoot 'scripts\install-mongodb-service.ps1')
} else {
  Write-Host 'Remote MongoDB configured; skipping local MongoDB installation.'
}

Write-Host 'Setup complete. Run FranklyBuilds-Register (FB注册机): Full stack in VS Code.'
