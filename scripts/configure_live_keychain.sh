#!/bin/sh

# Live credential과 실행 profile을 renderer·환경·일반 파일 밖의 Keychain에만 보존한다.
set -eu
LIVE_SERVICE="com.binance-auto.trader.live"
PROFILE_SERVICE="com.binance-auto.trader.desktop-profile"
ACTION="${1:-check}"

# 함수 이름: fail()
# 기능: secret 없는 고정 진단으로 종료한다.
# 인자: message -> 안전한 오류 설명
# 반환값: exit 1
# 작성 날짜: 2026/09/08
fail() {
    echo "configure_live_keychain: $1" >&2
    exit 1
}

# 함수 이름: check_pair()
# 기능: live namespace 두 item 존재 여부만 확인하고 password를 읽지 않는다.
# 인자: 없음
# 반환값: 두 item 조회 성공 여부
# 작성 날짜: 2026/09/08
check_pair() {
    # -w 없이 조회하여 credential을 shell 변수나 stdout에 가져오지 않는다.
    /usr/bin/security find-generic-password -s "$LIVE_SERVICE" -a api-key >/dev/null 2>&1 &&
        /usr/bin/security find-generic-password -s "$LIVE_SERVICE" -a api-secret >/dev/null 2>&1
}

# 함수 이름: select_profile()
# 기능: secret이 아닌 exact profile만 Keychain에 저장한다.
# 인자: profile -> 허용된 native profile 값
# 반환값: security exit status
# 작성 날짜: 2026/09/08
select_profile() {
    /usr/bin/security add-generic-password -U -s "$PROFILE_SERVICE" -a execution-profile -w "$1" >/dev/null
}

# Check 외 profile 변경은 정확한 native LIVE 확인을 요구한다.
case "$ACTION" in
    check)
        check_pair || fail "live credential pair unavailable"
        echo "live credential pair present (values not read)"
        exit 0
        ;;
    set|read-only|orders|disable) ;;
    *) fail "usage: configure_live_keychain.sh [check|set|read-only|orders|disable]" ;;
esac
[ -t 0 ] || fail "native confirmation requires an interactive terminal"
echo "앱을 정상 종료한 뒤 진행하세요. Live 키의 withdrawal 권한은 비활성화되어 있어야 합니다."
printf '확인을 위해 LIVE를 입력하세요: '
IFS= read -r confirmation
[ "$confirmation" = "LIVE" ] || fail "exact LIVE confirmation required"

# 두 secret은 security의 hidden terminal prompt에서만 입력하고 profile은 마지막에 활성화한다.
if [ "$ACTION" = set ]; then
    select_profile TESTNET  # 중단된 credential 교체가 과거 live 주문 profile을 재사용하지 못하게 한다.
    echo "LIVE API key (hidden input): 같은 API key를 아래 두 프롬프트에 입력하세요."
    /usr/bin/security add-generic-password -U -s "$LIVE_SERVICE" -a api-key -w
    echo "LIVE API secret (hidden input): 같은 API secret을 아래 두 프롬프트에 입력하세요."
    /usr/bin/security add-generic-password -U -s "$LIVE_SERVICE" -a api-secret -w
    check_pair || fail "live credential pair unavailable"
    select_profile LIVE_READ_ONLY
elif [ "$ACTION" = disable ]; then
    select_profile TESTNET  # Live credential을 삭제하지 않고 live 실행만 비활성화한다.
elif [ "$ACTION" = orders ]; then
    check_pair || fail "live credential pair unavailable"
    printf 'Session 8의 별도 주문 승인: LIVE ORDERS 10 USDT를 입력하세요: '
    IFS= read -r order_confirmation
    [ "$order_confirmation" = "LIVE ORDERS 10 USDT" ] || fail "separate live order confirmation required"
    select_profile LIVE_ORDERS_V1
else
    check_pair || fail "live credential pair unavailable"
    select_profile LIVE_READ_ONLY
fi
echo "native profile saved; restart the app to apply"
