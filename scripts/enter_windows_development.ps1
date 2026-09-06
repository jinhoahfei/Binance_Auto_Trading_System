# Dot-source from PowerShell to use this checkout's isolated development tools.
# This only changes the current shell; user and machine PATH remain unchanged.
$developmentRoot = Split-Path -Parent $PSScriptRoot
$developmentTools = Join-Path $developmentRoot '.dev-tools'
$developmentNode = Join-Path $developmentTools 'node-v24.19.0-win-x64'
$developmentPaths = @(
    (Join-Path $developmentRoot 'backend/.venv/Scripts'),
    (Join-Path $developmentTools 'python-tools/bin'),
    $developmentNode,
    (Join-Path $developmentTools 'pnpm/node_modules/.bin'),
    (Join-Path $developmentTools 'cargo/bin'),
    'C:\Program Files\Git\cmd'
)
foreach ($developmentPath in $developmentPaths) {
    if (-not (Test-Path -LiteralPath $developmentPath)) {
        throw "Development dependency is missing: $developmentPath"
    }
}
$env:PATH = ($developmentPaths -join ';') + ';' + $env:PATH
$env:CARGO_HOME = Join-Path $developmentTools 'cargo'
$env:RUSTUP_HOME = Join-Path $developmentTools 'rustup'
$env:UV_CACHE_DIR = Join-Path $developmentTools 'uv-cache'
$env:npm_config_cache = Join-Path $developmentTools 'npm-cache'
$env:PYTHONUTF8 = '1'
$env:VIRTUAL_ENV = Join-Path $developmentRoot 'backend/.venv'
Write-Output 'Windows development environment activated for this PowerShell session.'
