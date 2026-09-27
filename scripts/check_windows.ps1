# Run offline application checks without reading stored credentials or starting trading.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'enter_windows_development.ps1')
& $developmentPython (Join-Path $PSScriptRoot 'check_platform.py') @args
exit $LASTEXITCODE
