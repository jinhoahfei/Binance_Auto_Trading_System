param([ValidateSet('set', 'check', 'delete', 'canary')][string]$Action = 'check')

# Windows native 도구의 입력은 action뿐이며 secret parameter와 environment fallback은 없다.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess) {
    throw 'Windows x64 PowerShell is required.'
}
Add-Type -Path (Join-Path $PSScriptRoot 'windows/CredentialManager.cs')
$api_key = $null
$api_secret = $null
$canary_secret = $null

try {
    switch ($Action) {
        'set' {
            # 두 입력을 먼저 검증해 잘못된 두번째 입력이 기존 pair를 부분 갱신하지 않게 한다.
            $api_key = Read-Host 'Spot Testnet API key' -AsSecureString
            $api_secret = Read-Host 'Spot Testnet API secret' -AsSecureString
            [CredentialManager]::validate_secret($api_key)
            [CredentialManager]::validate_secret($api_secret)
            [CredentialManager]::write_secret('api-key', $api_key)
            [CredentialManager]::write_secret('api-secret', $api_secret)
            if (-not [CredentialManager]::verify_secret('api-key', $api_key) -or
                -not [CredentialManager]::verify_secret('api-secret', $api_secret)) {
                throw 'Credential verification failed.'
            }
        }
        'check' {
            if (-not [CredentialManager]::verify_secret('api-key', $null) -or
                -not [CredentialManager]::verify_secret('api-secret', $null)) {
                throw 'Credential pair unavailable.'
            }
        }
        'delete' {
            # 삭제 대상은 이 도구의 fixed Testnet pair이며 history와 다른 credential은 건드리지 않는다.
            [CredentialManager]::delete_secret('api-key')
            [CredentialManager]::delete_secret('api-secret')
        }
        'canary' {
            # 별도 target의 공개 canary만 사용해 실제 pair와 Binance endpoint에 접근하지 않는다.
            $canary_secret = New-Object Security.SecureString
            foreach ($character in 'Session5-Canary-Only'.ToCharArray()) { $canary_secret.AppendChar($character) }
            try {
                [CredentialManager]::write_secret('session5-canary', $canary_secret)
                if (-not [CredentialManager]::verify_secret('session5-canary', $canary_secret)) {
                    throw 'Canary mismatch.'
                }
            } finally {
                [CredentialManager]::delete_secret('session5-canary')
            }
            if ([CredentialManager]::verify_secret('session5-canary', $null)) { throw 'Canary remains.' }
        }
    }
    Write-Output 'Credential operation completed.'  # Secret과 길이는 결과에 포함하지 않는다.
} catch {
    # Native 예외나 사용자 입력을 출력하지 않고 부분 저장 가능성에 대한 고정 안내를 남긴다.
    Write-Error 'Credential operation failed. For set, repeat both inputs before starting the app.' -ErrorAction Continue
    exit 1
} finally {
    if ($null -ne $api_key) { $api_key.Dispose() }
    if ($null -ne $api_secret) { $api_secret.Dispose() }
    if ($null -ne $canary_secret) { $canary_secret.Dispose() }
}
