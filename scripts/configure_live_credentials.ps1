param([ValidateSet('set', 'check', 'read-only', 'orders', 'disable')][string]$Action = 'check')

# Secret은 parameter·environment·평문 파일로 받지 않으며 native hidden prompt만 사용한다.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess) {
    throw 'Windows x64 PowerShell is required.'
}
Add-Type -Path (Join-Path $PSScriptRoot 'windows/CredentialManager.cs')
$api_key = $null
$api_secret = $null

function Confirm-LiveCredentialPair {
    if (-not [CredentialManager]::verify_live_secret('api-key', $null) -or
        -not [CredentialManager]::verify_live_secret('api-secret', $null)) {
        throw 'Live credential pair unavailable.'
    }
}

try {
    if ($Action -eq 'check') {
        Confirm-LiveCredentialPair
        Write-Output 'Live credential pair available (values not displayed).'
    } else {
        Write-Output 'Exit the app before continuing. Live API withdrawal permission must be disabled.'
        $confirmation = Read-Host 'Type LIVE to confirm'
        if ($confirmation -cne 'LIVE') { throw 'Exact LIVE confirmation required.' }
        switch ($Action) {
            'set' {
                # 중단·실패한 교체가 이전 live 주문 profile을 재사용하지 못하게 먼저 비활성화한다.
                [CredentialManager]::write_execution_profile('TESTNET')
                $api_key = Read-Host 'LIVE API key' -AsSecureString
                $api_secret = Read-Host 'LIVE API secret' -AsSecureString
                [CredentialManager]::validate_secret($api_key)
                [CredentialManager]::validate_secret($api_secret)
                [CredentialManager]::write_live_secret('api-key', $api_key)
                [CredentialManager]::write_live_secret('api-secret', $api_secret)
                if (-not [CredentialManager]::verify_live_secret('api-key', $api_key) -or
                    -not [CredentialManager]::verify_live_secret('api-secret', $api_secret)) {
                    throw 'Live credential verification failed.'
                }
                [CredentialManager]::write_execution_profile('LIVE_READ_ONLY')
            }
            'read-only' {
                Confirm-LiveCredentialPair
                [CredentialManager]::write_execution_profile('LIVE_READ_ONLY')
            }
            'orders' {
                Confirm-LiveCredentialPair
                $order_confirmation = Read-Host 'Type LIVE ORDERS 10 USDT for separate live order approval'
                if ($order_confirmation -cne 'LIVE ORDERS 10 USDT') { throw 'Separate live order confirmation required.' }
                [CredentialManager]::write_execution_profile('LIVE_ORDERS_V1')
            }
            'disable' {
                [CredentialManager]::write_execution_profile('TESTNET')
            }
        }
        Write-Output 'Native profile saved; restart the app to apply.'
    }
} catch {
    # Native 예외·입력·길이는 출력하지 않으며 실패 후 두 credential을 다시 설정하도록 안내한다.
    Write-Error 'Live credential operation failed. For set, repeat both inputs before starting the app.' -ErrorAction Continue
    exit 1
} finally {
    if ($null -ne $api_key) { $api_key.Dispose() }
    if ($null -ne $api_secret) { $api_secret.Dispose() }
}
