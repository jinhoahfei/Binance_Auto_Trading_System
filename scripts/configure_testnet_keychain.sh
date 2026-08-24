#!/bin/sh

# Packaged sidecar가 읽는 Testnet credential 두 개를 macOS Keychain prompt로 저장한다.
set -eu

KEYCHAIN_SERVICE="com.binance-auto.trader.testnet"

# 함수 이름: fail()
# 기능: credential이나 command output을 반사하지 않는 provisioning 오류로 중단한다.
# 인자: message -> 고정 운영 오류 설명
# 반환값: exit status 1
# 작성 날짜: 2026/08/24
fail() {
    message=$1
    echo "configure_testnet_keychain: ${message}" >&2
    exit 1
}

# 함수 이름: store_prompted_secret()
# 기능: password를 argv로 받지 않고 macOS security의 interactive prompt에서 Keychain에 저장한다.
# 인자: account -> native sidecar와 합의한 non-secret account identifier
# 반환값: security command exit status
# 작성 날짜: 2026/08/24
store_prompted_secret() {
    account=$1

    # `-w`를 마지막 인자로 값 없이 전달하면 security가 terminal에서 secret을 안전하게 묻는다.
    security add-generic-password \
        -U \
        -s "${KEYCHAIN_SERVICE}" \
        -a "${account}" \
        -w
}

command -v security >/dev/null 2>&1 || fail "macOS security command를 찾을 수 없습니다."
[ -t 0 ] || fail "credential prompt에는 interactive terminal이 필요합니다."

echo "Binance Spot Testnet API key를 입력합니다. 입력값은 화면에 표시되지 않습니다."
store_prompted_secret "api-key" || fail "Testnet API key를 저장하지 못했습니다."

echo "Binance Spot Testnet API secret을 입력합니다. 입력값은 화면에 표시되지 않습니다."
store_prompted_secret "api-secret" || fail "Testnet API secret을 저장하지 못했습니다."

echo "configure_testnet_keychain: macOS Keychain provisioning 완료"
