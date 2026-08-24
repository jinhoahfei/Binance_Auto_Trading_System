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

# 함수 이름: verify_stored_secret()
# 기능: 저장한 Keychain password를 화면에 반사하지 않고 non-empty 상태만 검증한다.
# 인자: account -> 검증할 non-secret account identifier
# 반환값: password가 비어 있지 않으면 exit status 0
# 작성 날짜: 2026/08/24
verify_stored_secret() {
    account=$1

    # Command substitution 안에서만 password를 읽고 값이나 길이를 stdout에 쓰지 않는다.
    stored_secret=$(security find-generic-password \
        -s "${KEYCHAIN_SERVICE}" \
        -a "${account}" \
        -w) || {
        unset stored_secret  # 조회 실패에도 이전 함수 호출의 secret 참조를 남기지 않는다.
        return 1
    }
    if [ -z "${stored_secret}" ]; then
        unset stored_secret  # 빈 password도 성공한 provisioning으로 해석하지 않는다.
        return 1
    fi
    unset stored_secret  # 검증 직후 shell의 장기 secret 참조를 제거한다.
}

command -v security >/dev/null 2>&1 || fail "macOS security command를 찾을 수 없습니다."
[ -t 0 ] || fail "credential prompt에는 interactive terminal이 필요합니다."

echo "Binance Spot Testnet API key를 입력합니다. 입력값은 화면에 표시되지 않습니다."
store_prompted_secret "api-key" || fail "Testnet API key를 저장하지 못했습니다."
verify_stored_secret "api-key" || fail "저장한 Testnet API key가 비어 있습니다."

echo "Binance Spot Testnet API secret을 입력합니다. 입력값은 화면에 표시되지 않습니다."
store_prompted_secret "api-secret" || fail "Testnet API secret을 저장하지 못했습니다."
verify_stored_secret "api-secret" || fail "저장한 Testnet API secret이 비어 있습니다."

echo "configure_testnet_keychain: macOS Keychain provisioning 완료"
