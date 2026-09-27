# Install this checkout's locked dependencies using already installed tools.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'enter_windows_development.ps1') -AllowMissingBackend
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv and x64 Python 3.11 or newer, then run setup again.'
}
$setupRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $setupRoot 'backend')
try {
    & uv sync --locked --extra desktop
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
} finally {
    Pop-Location
}
Push-Location (Join-Path $setupRoot 'UI')
try {
    & pnpm.cmd install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'UI dependency installation failed.' }
} finally {
    Pop-Location
}
. (Join-Path $PSScriptRoot 'enter_windows_development.ps1')
Write-Output 'Windows dependencies installed. Run scripts/check_windows.ps1 before starting the app.'
