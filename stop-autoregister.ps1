param(
    [switch] $KeepRoxy
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimePids = Join-Path $Root 'data\runtime\pids'

function Stop-SavedProcess([string] $Name) {
    $pidFile = Join-Path $RuntimePids "$Name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $savedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($savedPid -match '^\d+$') {
        Stop-Process -Id ([int]$savedPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Write-Host '[FB注册机] stopping project services...' -ForegroundColor Cyan

foreach ($name in @('frontend', 'backend', 'easy-proxies', 'resin')) {
    Stop-SavedProcess $name
}
if (-not $KeepRoxy) {
    Stop-SavedProcess 'roxy-browser'
}

Write-Host 'FranklyBuilds-Register owned processes stopped. Untracked listeners and legacy services were left untouched.' -ForegroundColor Green