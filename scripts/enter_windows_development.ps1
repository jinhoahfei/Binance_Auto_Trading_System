# Dot-source from PowerShell to use this checkout's isolated development tools.
# This only changes the current shell; user and machine PATH remain unchanged.
param([switch]$AllowMissingBackend)

$ErrorActionPreference = 'Stop'
$developmentRoot = Split-Path -Parent $PSScriptRoot
$developmentTools = Join-Path $developmentRoot '.dev-tools'
$developmentNode = Get-ChildItem -Path $developmentTools -Filter 'node-v*-win-x64' -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
$developmentPaths = @(
    (Join-Path $developmentRoot 'backend/.venv/Scripts'),
    (Join-Path $developmentTools 'python-tools/bin'),
    $developmentNode,
    (Join-Path $developmentTools 'pnpm/node_modules/.bin'),
    (Join-Path $developmentTools 'cargo/bin'),
    'C:\Program Files\Git\cmd'
)
# A normal system installation is supported alongside the older isolated tools.
$developmentPaths = @($developmentPaths | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
$env:PATH = ($developmentPaths -join ';') + ';' + $env:PATH
if (Test-Path -LiteralPath (Join-Path $developmentTools 'cargo/bin')) {
    $env:CARGO_HOME = Join-Path $developmentTools 'cargo'
}
if (Test-Path -LiteralPath (Join-Path $developmentTools 'rustup/toolchains')) {
    $env:RUSTUP_HOME = Join-Path $developmentTools 'rustup'
}
$env:PYTHONUTF8 = '1'
$developmentPython = Join-Path $developmentRoot 'backend/.venv/Scripts/python.exe'
if (Test-Path -LiteralPath $developmentPython) {
    $env:VIRTUAL_ENV = Join-Path $developmentRoot 'backend/.venv'
} elseif (-not $AllowMissingBackend) {
    throw 'Backend environment is missing. Run scripts/setup_windows_development.ps1 first.'
}
foreach ($developmentCommand in @('node.exe', 'pnpm.cmd', 'cargo.exe', 'git.exe')) {
    if (-not (Get-Command $developmentCommand -ErrorAction SilentlyContinue)) {
        throw "Development tool is missing from PATH: $developmentCommand"
    }
}
Write-Output 'Windows development environment activated for this PowerShell session.'
