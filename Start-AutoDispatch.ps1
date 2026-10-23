[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$Config = '',
    [string]$Approval = (Join-Path $PSScriptRoot 'approval.json'),
    [ValidateRange(0,2)][int]$MaxWorkers = 0,
    [ValidateRange(0,39)][int]$MaxTasks = 0,
    [switch]$Watch,
    [switch]$Background,
    [ValidateRange(1,1440)][int]$MaxCycles = 60,
    [ValidateRange(10,3600)][int]$PollSeconds = 60
)
$ErrorActionPreference = 'Stop'
if (-not $Config) {
    $localConfig = Join-Path $PSScriptRoot 'config.local.json'
    $Config = if (Test-Path -LiteralPath $localConfig) { $localConfig } else { Join-Path $PSScriptRoot 'config.example.json' }
}
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = 'python' }
Push-Location $PSScriptRoot
try {
    if (-not $Execute) {
        & $pythonPath -m autodispatch --config $Config doctor
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $pythonPath -m autodispatch --config $Config plan
        exit $LASTEXITCODE
    }
    if (-not (Test-Path -LiteralPath $Approval)) {
        throw 'Explicit human approval is missing. Review README.md and approval.example.json; the runner cannot approve itself.'
    }
    $policy = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
    if ($MaxWorkers -eq 0) { $MaxWorkers = $policy.max_workers }
    if ($MaxTasks -eq 0) { $MaxTasks = $policy.max_tasks }
    if ($MaxWorkers -ne $policy.max_workers -or $MaxTasks -ne $policy.max_tasks) {
        throw 'Service limits come from the approved config. Use run --max-workers/--max-tasks for a smaller foreground batch.'
    }
    $cycles = if ($Watch) { $MaxCycles } else { 1 }
    $action = if ($Background) { 'start' } else { 'serve' }
    & $pythonPath -m autodispatch --config $Config $action --execute --approval $Approval --max-cycles $cycles --poll-seconds $PollSeconds
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
