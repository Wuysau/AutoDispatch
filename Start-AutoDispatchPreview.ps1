[CmdletBinding()]
param(
    [ValidateRange(1024,65535)][int]$ApiPort = 8765,
    [ValidateRange(1024,65535)][int]$DevPort = 5174,
    [switch]$Dev,
    [switch]$Rebuild,
    [switch]$NoWindows
)
$ErrorActionPreference = 'Stop'
$uiDir = Join-Path $PSScriptRoot 'ui'
$dist = Join-Path $uiDir 'dist'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python venv not found at $python; run: python -m venv .venv && .venv\Scripts\pip install -e .[host]" }
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$vite = Join-Path $uiDir 'node_modules/.bin/vite.cmd'

$ui = $null
Push-Location $uiDir
try {
    if (-not (Test-Path -LiteralPath 'node_modules/@rolldown/binding-win32-x64-msvc')) {
        & $npm install
        if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
    }
    if ($Dev) {
        $ui = Start-Process -FilePath $vite -ArgumentList @('--host', '127.0.0.1', '--port', "$DevPort", '--strictPort') -WorkingDirectory $uiDir -WindowStyle Hidden -PassThru
        $base = "http://127.0.0.1:$DevPort/"
    } else {
        if ($Rebuild -or -not (Test-Path -LiteralPath (Join-Path $dist 'index.html'))) {
            & $npm run build
            if ($LASTEXITCODE -ne 0) { throw 'UI build failed' }
        }
        $base = "http://127.0.0.1:$ApiPort/"
    }
} finally {
    Pop-Location
}

$api = Start-Process -FilePath $python -ArgumentList @('-m', 'autodispatch', 'api', '--port', "$ApiPort") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru

function Stop-Preview {
    if ($ui) { taskkill /PID $ui.Id /T /F | Out-Null }
    taskkill /PID $api.Id /T /F | Out-Null
}

$ready = $false
$deadline = (Get-Date).AddSeconds(30)
while (-not $ready -and (Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -Uri "${base}api/autodispatch/snapshot" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ready = $true
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $ready) {
    Stop-Preview
    throw "Service did not start at $base (port in use? try -ApiPort 8766)"
}

Write-Host "AutoDispatch UI at $base (API PID $($api.Id)$(if ($ui) { ", vite PID $($ui.Id)" }))."

if ($NoWindows) {
    Write-Host "Stop it with: taskkill /PID $($api.Id) /T /F$(if ($ui) { " ; taskkill /PID $($ui.Id) /T /F" })"
    return
}

Push-Location $PSScriptRoot
try {
    & $python -m autodispatch widget --url "${base}#/widget"
    if ($LASTEXITCODE -ne 0) { throw "Native widget host exited with code $LASTEXITCODE (install with: $python -m pip install pywebview)" }
} finally {
    Pop-Location
    Stop-Preview
}
