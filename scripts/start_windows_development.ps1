# Start the desktop source application with this checkout's local tools.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'enter_windows_development.ps1')
Push-Location (Join-Path (Split-Path -Parent $PSScriptRoot) 'UI')
try {
    & pnpm.cmd desktop:dev
    $developmentExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $developmentExitCode
