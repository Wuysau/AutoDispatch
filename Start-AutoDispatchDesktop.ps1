[CmdletBinding()]
param(
    [switch]$Demo,
    [switch]$Rebuild,
    [ValidateRange(0,65535)][int]$Port = 0
)
$ErrorActionPreference = 'Stop'
$desktopPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$desktopUi = Join-Path $PSScriptRoot 'ui'
if (-not (Test-Path -LiteralPath $desktopPython)) { throw 'Create .venv and install .[host] first.' }
Push-Location $PSScriptRoot
try {
    if ($Rebuild -or -not (Test-Path -LiteralPath (Join-Path $desktopUi 'dist/index.html'))) {
        & npm.cmd --prefix $desktopUi run build
        if ($LASTEXITCODE -ne 0) { throw 'UI build failed' }
    }
    $desktopArgs = @('-m', 'autodispatch', 'desktop', '--port', "$Port")
    if ($Demo) { $desktopArgs += '--demo' }
    & $desktopPython @desktopArgs
    if ($LASTEXITCODE -ne 0) { throw "Desktop host exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
