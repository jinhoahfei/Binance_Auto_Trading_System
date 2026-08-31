#!/bin/sh

# Phase 13 local readiness 검사를 저장소 root 기준의 외부 주문 없는 한 경로로 실행한다.
set -eu

# Script 실제 위치에서 repository root를 고정해 호출자의 working directory를 신뢰하지 않는다.
SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "${SCRIPT_DIRECTORY}/.." && pwd)
BACKEND_DIRECTORY="${REPOSITORY_ROOT}/backend"
UI_DIRECTORY="${REPOSITORY_ROOT}/UI"
UI_NODE_BIN_DIRECTORY="${UI_DIRECTORY}/node_modules/.bin"
RUST_DIRECTORY="${UI_DIRECTORY}/apps/desktop/src-tauri"
PHASE12_SECRET_CHECKER="${SCRIPT_DIRECTORY}/check_phase12_secrets.py"
COMMUNICATION_TRACEABILITY_CHECKER="${SCRIPT_DIRECTORY}/check_communication_traceability.py"
PHASE13_REPLAY_RUNNER="${SCRIPT_DIRECTORY}/phase13_deterministic_replay.py"
PHASE13_REPLAY_TRACE="${BACKEND_DIRECTORY}/tests/fixtures/phase13/canonical_fault_trace.json"
PHASE13_SOAK_RUNNER="${SCRIPT_DIRECTORY}/phase13_soak.py"
PHASE13_READINESS_RUNNER="${SCRIPT_DIRECTORY}/phase13_readiness.py"
PHASE13_SUPPLY_EVIDENCE_CHECKER="${SCRIPT_DIRECTORY}/check_phase13_supply_chain_evidence.py"
PHASE13_OFFLINE_OSV_RUNNER="${SCRIPT_DIRECTORY}/run_phase13_offline_osv.py"
PHASE13_VISUAL_REGRESSION_CHECKER="${SCRIPT_DIRECTORY}/check_phase13_visual_regression.py"
PHASE13_VISUAL_CURRENT_DIRECTORY="${UI_DIRECTORY}/visual-regression/current"
UI_CONTRACT_GENERATOR="${BACKEND_DIRECTORY}/scripts/generate_ui_contracts.py"
PYTHON_LOCKFILE="${BACKEND_DIRECTORY}/uv.lock"
UI_LOCKFILE="${UI_DIRECTORY}/pnpm-lock.yaml"
RUST_LOCKFILE="${RUST_DIRECTORY}/Cargo.lock"

# 외부 Testnet mutation opt-in과 credential/cap 상속을 모든 child command 전에 제거한다.
BINANCE_RUN_TESTNET=0
BINANCE_RUN_TESTNET_ORDERS=0
export BINANCE_RUN_TESTNET BINANCE_RUN_TESTNET_ORDERS
unset BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET BINANCE_TESTNET_MAX_NOTIONAL
unset BINANCE_RUN_PHASE13_PUBLIC_CASE2
unset BINANCE_TESTNET_BASELINE_HISTORY_PATH BINANCE_TESTNET_BASELINE_HISTORY_FD
unset BINANCE_TESTNET_BASELINE_HISTORY_SHA256 BINANCE_TESTNET_BASELINE_PENDING_FD
unset BINANCE_TESTNET_BASELINE_PENDING_SHA256
PYTHONDONTWRITEBYTECODE=1
PYTHONWARNINGS=error
export PYTHONDONTWRITEBYTECODE PYTHONWARNINGS

# 함수 이름: fail()
# 기능: readiness 단계 이름이나 secret 원문을 반사하지 않는 고정 오류로 즉시 중단한다.
# 인자: message -> 사람이 읽을 고정 오류 설명
# 반환값: exit status 1
# 작성 날짜: 2026/08/24
fail() {
    message=$1
    echo "check_all: ERROR: ${message}" >&2
    exit 1
}

# 함수 이름: select_python()
# 기능: repository backend virtual environment를 우선하고 없으면 PATH의 python3를 선택한다.
# 인자: 없음
# 반환값: stdout의 Python executable 경로
# 작성 날짜: 2026/08/24
select_python() {
    if [ -x "${BACKEND_DIRECTORY}/.venv/bin/python" ]; then
        echo "${BACKEND_DIRECTORY}/.venv/bin/python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    fail "Python executable을 확인할 수 없습니다."
}

# 함수 이름: announce_run()
# 기능: 실행 직전 단계 이름만 출력하고 command 인자나 환경값은 노출하지 않는다.
# 인자: step_name -> 고정 readiness 단계 이름
# 반환값: 없음
# 작성 날짜: 2026/08/24
announce_run() {
    step_name=$1
    echo "check_all: RUN: ${step_name}"
}

# 함수 이름: announce_pass()
# 기능: 성공한 readiness 단계 이름을 사람이 읽을 수 있는 PASS line으로 출력한다.
# 인자: step_name -> 완료한 고정 readiness 단계 이름
# 반환값: 없음
# 작성 날짜: 2026/08/24
announce_pass() {
    step_name=$1
    echo "check_all: PASS: ${step_name}"
}

# 함수 이름: announce_blocked()
# 기능: 예상 가능한 readiness evidence GAP을 PASS와 구분해 고정 문구로 알린다.
# 인자: step_name -> 차단된 고정 readiness 단계 이름
# 반환값: 없음
# 작성 날짜: 2026/08/29
announce_blocked() {
    step_name=$1
    echo "check_all: BLOCKED: ${step_name}" >&2
}

# 함수 이름: run_backend_unittests()
# 기능: 외부 Testnet opt-in이 닫힌 환경에서 backend 전체 unittest를 실행한다.
# 인자: 없음
# 반환값: unittest exit status
# 작성 날짜: 2026/08/24
run_backend_unittests() {
    (
        cd "${BACKEND_DIRECTORY}"
        PYTHONPATH="${BACKEND_DIRECTORY}/src" "${PYTHON_COMMAND}" \
            -m unittest discover -s tests -p 'test_*.py' -v
    )
}

# 함수 이름: run_phase12_script_unittests()
# 기능: 기존 Phase 12 release script 회귀 테스트를 repository root에서 실행한다.
# 인자: 없음
# 반환값: unittest exit status
# 작성 날짜: 2026/08/24
run_phase12_script_unittests() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            -m unittest discover -s scripts -p 'test_*.py' -v
    )
}

# 함수 이름: run_phase13_deterministic_replay()
# 기능: secret 없는 canonical fault trace를 network 독립 runner로 5회 replay해 byte 안정성을 검증한다.
# 인자: 없음
# 반환값: deterministic replay CLI exit status
# 작성 날짜: 2026/08/25
run_phase13_deterministic_replay() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${BACKEND_DIRECTORY}/src:${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_REPLAY_RUNNER}" "${PHASE13_REPLAY_TRACE}" \
            --repeat 5 --quiet
    )
}

# 함수 이름: run_phase13_soak_preflight()
# 기능: 장시간 runner의 기본 mode가 credential·주문 없는 local configuration으로 닫혀 있는지 검증한다.
# 인자: 없음
# 반환값: soak validate-only CLI exit status
# 작성 날짜: 2026/08/25
run_phase13_soak_preflight() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${BACKEND_DIRECTORY}/src:${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_SOAK_RUNNER}" --validate-only
    )
}

# 함수 이름: run_ui_contract_drift_check()
# 기능: Python authoritative renderer와 checked-in TypeScript 계약의 byte drift를 쓰기 없이 검사한다.
# 인자: 없음
# 반환값: contract generator check exit status
# 작성 날짜: 2026/08/25
run_ui_contract_drift_check() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${BACKEND_DIRECTORY}/src" "${PYTHON_COMMAND}" \
            "${UI_CONTRACT_GENERATOR}" --check
    )
}

# 함수 이름: run_ui_vitest()
# 기능: lockfile로 설치된 local Vitest를 사용해 UI 전체 suite를 한 번 실행한다.
# 인자: 없음
# 반환값: pnpm test exit status
# 작성 날짜: 2026/08/24
run_ui_vitest() {
    (
        cd "${UI_DIRECTORY}"
        "${UI_NODE_BIN_DIRECTORY}/vitest" run
    )
}

# 함수 이름: run_ui_typecheck()
# 기능: local TypeScript compiler로 renderer와 adapter의 strict contract를 검사한다.
# 인자: 없음
# 반환값: pnpm typecheck exit status
# 작성 날짜: 2026/08/25
run_ui_typecheck() {
    (
        cd "${UI_DIRECTORY}"
        "${UI_NODE_BIN_DIRECTORY}/tsc" -b --pretty false
    )
}

# 함수 이름: run_ui_build()
# 기능: local TypeScript와 Vite로 production renderer bundle 및 import·asset 계약을 검사한다.
# 인자: 없음
# 반환값: pnpm build exit status
# 작성 날짜: 2026/08/25
run_ui_build() {
    (
        cd "${UI_DIRECTORY}"
        "${UI_NODE_BIN_DIRECTORY}/tsc" -b
        "${UI_NODE_BIN_DIRECTORY}/vite" build
    )
}

# 함수 이름: run_rust_tests()
# 기능: lockfile과 local Cargo cache만 사용해 Tauri Rust test를 실행한다.
# 인자: 없음
# 반환값: cargo test exit status
# 작성 날짜: 2026/08/24
run_rust_tests() {
    (
        cd "${RUST_DIRECTORY}"
        cargo test --locked --offline
    )
}

# 함수 이름: run_rust_format_check()
# 기능: native sidecar source가 rustfmt canonical 형식과 일치하는지 검사한다.
# 인자: 없음
# 반환값: cargo fmt check exit status
# 작성 날짜: 2026/08/25
run_rust_format_check() {
    (
        cd "${RUST_DIRECTORY}"
        cargo fmt --all -- --check
    )
}

# 함수 이름: run_rust_clippy()
# 기능: offline lockfile만 사용해 모든 native target의 warning을 오류로 검사한다.
# 인자: 없음
# 반환값: cargo clippy exit status
# 작성 날짜: 2026/08/25
run_rust_clippy() {
    (
        cd "${RUST_DIRECTORY}"
        cargo clippy --locked --offline --all-targets -- -D warnings
    )
}

# 함수 이름: run_phase12_secret_checker()
# 기능: 실제 .env credential canary를 출력하지 않고 repository 전체에서 누출 여부를 검사한다.
# 인자: 없음
# 반환값: Phase 12 secret checker exit status
# 작성 날짜: 2026/08/24
run_phase12_secret_checker() {
    (
        cd "${REPOSITORY_ROOT}"
        "${PYTHON_COMMAND}" "${PHASE12_SECRET_CHECKER}" \
            --env "${REPOSITORY_ROOT}/.env" "${REPOSITORY_ROOT}"
    )
}

# 함수 이름: run_phase13_supply_evidence_binding()
# 기능: Retained NO_GO를 현재 lockfile/local inventory와 대조하고 readiness를 계속 차단한다.
# 인자: 없음
# 반환값: 현재 schema의 validated NO_GO와 stale/malformed evidence 모두 non-zero
# 작성 날짜: 2026/08/29
run_phase13_supply_evidence_binding() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_SUPPLY_EVIDENCE_CHECKER}"
    )
}

# 함수 이름: run_supply_chain_vulnerability_scan()
# 기능: OS network sandbox와 cached OSV database로 세 fixed lockfile 취약점을 검사한다.
# 인자: 없음
# 반환값: finding이 없을 때 0, finding 또는 offline database 오류일 때 non-zero
# 작성 날짜: 2026/08/29
run_supply_chain_vulnerability_scan() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_OFFLINE_OSV_RUNNER}" vulnerability
    )
}

# 함수 이름: run_supply_chain_license_scan()
# 기능: OS network sandbox와 OSV offline mode로 fixed lockfile license를 독립 검사한다.
# 인자: 없음
# 반환값: Local license summary가 완결되면 0, metadata 부재 또는 scanner 오류면 non-zero
# 작성 날짜: 2026/08/29
run_supply_chain_license_scan() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_OFFLINE_OSV_RUNNER}" license
    )
}

# 함수 이름: run_phase13_visual_regression_checker()
# 기능: 고정 Figma PNG와 actual browser JPEG 16개를 read-only FFmpeg SSIM으로 비교한다.
# 인자: 없음
# 반환값: 모든 frame이 0.98 이상일 때만 0인 visual gate exit status
# 작성 날짜: 2026/08/29
run_phase13_visual_regression_checker() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_VISUAL_REGRESSION_CHECKER}" \
            --current-directory "${PHASE13_VISUAL_CURRENT_DIRECTORY}"
    )
}

# 함수 이름: run_phase13_readiness_gate()
# 기능: artifact byte binding 전에는 수동 license·SBOM·soak GAP을 final PASS로 승격하지 않는다.
# 인자: 없음
# 반환값: unresolved GAP가 있으면 non-zero인 readiness exit status
# 작성 날짜: 2026/08/29
run_phase13_readiness_gate() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${PHASE13_READINESS_RUNNER}" gate
    )
}

# 함수 이름: run_communication_traceability_checker()
# 기능: 필수 Communication message와 code/test 추적성 manifest를 검증한다.
# 인자: 없음
# 반환값: checker exit status
# 작성 날짜: 2026/08/24
run_communication_traceability_checker() {
    (
        cd "${REPOSITORY_ROOT}"
        PYTHONPATH="${REPOSITORY_ROOT}" "${PYTHON_COMMAND}" \
            "${COMMUNICATION_TRACEABILITY_CHECKER}"
    )
}

# 함수 이름: list_steps()
# 기능: 실제 suite를 실행하지 않고 고정 안전환경과 readiness 단계 순서를 출력한다.
# 인자: 없음
# 반환값: 항상 0
# 작성 날짜: 2026/08/24
list_steps() {
    echo "check_all: SAFETY: BINANCE_RUN_TESTNET=0"
    echo "check_all: SAFETY: BINANCE_RUN_TESTNET_ORDERS=0"
    echo "check_all: SAFETY: BINANCE_RUN_PHASE13_PUBLIC_CASE2=unset"
    echo "check_all: SAFETY: testnet-credentials-and-cap=unset"
    echo "check_all: SAFETY: testnet-baseline-evidence=unset"
    echo "check_all: SAFETY: PYTHONWARNINGS=error"
    echo "backend-unittests"
    echo "phase12-script-unittests"
    echo "ui-contract-drift-check"
    echo "phase13-deterministic-replay"
    echo "phase13-soak-no-order-preflight"
    echo "ui-vitest"
    echo "ui-typecheck"
    echo "ui-production-build"
    echo "rust-format-check"
    echo "rust-cargo-test-locked-offline"
    echo "rust-clippy-all-targets"
    echo "phase12-secret-checker"
    echo "phase13-supply-chain-evidence-binding"
    echo "phase13-offline-vulnerability-scan"
    echo "phase13-offline-license-scan"
    echo "phase13-visual-regression"
    echo "communication-traceability-checker"
    echo "phase13-readiness-gap-gate"
}

# `--list`와 `--dry-run`은 full suite를 중첩하지 않는 unit-test와 운영 preflight seam이다.
case "${1:-}" in
    --list|--dry-run)
        [ "$#" -eq 1 ] || fail "--list/--dry-run은 추가 인자를 받지 않습니다."
        list_steps
        exit 0
        ;;
    "") ;;
    *) fail "지원하지 않는 인자입니다." ;;
esac

# 필수 repository 경계와 local executable은 첫 suite 전에 검증해 partial gate를 피한다.
[ -d "${BACKEND_DIRECTORY}/tests" ] || fail "backend tests directory가 없습니다."
[ -f "${UI_DIRECTORY}/package.json" ] || fail "UI package.json이 없습니다."
[ -f "${RUST_DIRECTORY}/Cargo.toml" ] || fail "Rust Cargo.toml이 없습니다."
[ -f "${PHASE12_SECRET_CHECKER}" ] || fail "Phase 12 secret checker가 없습니다."
[ -f "${PHASE13_REPLAY_RUNNER}" ] || fail "Phase 13 deterministic replay runner가 없습니다."
[ -f "${PHASE13_REPLAY_TRACE}" ] || fail "Phase 13 canonical fault trace가 없습니다."
[ -f "${PHASE13_SOAK_RUNNER}" ] || fail "Phase 13 soak runner가 없습니다."
[ -f "${PHASE13_READINESS_RUNNER}" ] || fail "Phase 13 readiness runner가 없습니다."
[ -f "${PHASE13_SUPPLY_EVIDENCE_CHECKER}" ] || fail "Phase 13 supply evidence checker가 없습니다."
[ -f "${PHASE13_OFFLINE_OSV_RUNNER}" ] || fail "Phase 13 offline OSV runner가 없습니다."
[ -f "${PHASE13_VISUAL_REGRESSION_CHECKER}" ] || fail "Phase 13 visual regression checker가 없습니다."
[ -f "${COMMUNICATION_TRACEABILITY_CHECKER}" ] || fail "Communication traceability checker가 없습니다."
[ -f "${UI_CONTRACT_GENERATOR}" ] || fail "UI contract generator가 없습니다."
[ -f "${PYTHON_LOCKFILE}" ] || fail "Python lockfile이 없습니다."
[ -f "${UI_LOCKFILE}" ] || fail "UI lockfile이 없습니다."
[ -f "${RUST_LOCKFILE}" ] || fail "Rust lockfile이 없습니다."
# Lockfile로 설치된 local UI tool만 사용해 package-manager network/signature 상태와 검증을 분리한다.
[ -x "${UI_NODE_BIN_DIRECTORY}/vitest" ] || fail "local Vitest executable이 없습니다."
[ -x "${UI_NODE_BIN_DIRECTORY}/tsc" ] || fail "local TypeScript executable이 없습니다."
[ -x "${UI_NODE_BIN_DIRECTORY}/vite" ] || fail "local Vite executable이 없습니다."
command -v cargo >/dev/null 2>&1 || fail "cargo executable을 확인할 수 없습니다."
PYTHON_COMMAND=$(select_python)

# 기능·회귀 suite는 첫 실패에서 중단하고, 마지막 evidence gate는 모두 실행해 NO_GO 근거를 누락하지 않는다.
announce_run "backend-unittests"
run_backend_unittests
announce_pass "backend-unittests"

announce_run "phase12-script-unittests"
run_phase12_script_unittests
announce_pass "phase12-script-unittests"

announce_run "ui-contract-drift-check"
run_ui_contract_drift_check
announce_pass "ui-contract-drift-check"

announce_run "phase13-deterministic-replay"
run_phase13_deterministic_replay
announce_pass "phase13-deterministic-replay"

announce_run "phase13-soak-no-order-preflight"
run_phase13_soak_preflight
announce_pass "phase13-soak-no-order-preflight"

announce_run "ui-vitest"
run_ui_vitest
announce_pass "ui-vitest"

announce_run "ui-typecheck"
run_ui_typecheck
announce_pass "ui-typecheck"

announce_run "ui-production-build"
run_ui_build
announce_pass "ui-production-build"

announce_run "rust-format-check"
run_rust_format_check
announce_pass "rust-format-check"

announce_run "rust-cargo-test-locked-offline"
run_rust_tests
announce_pass "rust-cargo-test-locked-offline"

announce_run "rust-clippy-all-targets"
run_rust_clippy
announce_pass "rust-clippy-all-targets"

announce_run "phase12-secret-checker"
run_phase12_secret_checker
announce_pass "phase12-secret-checker"

# Evidence 단계는 각각 실패해도 뒤의 offline scan·trace·readiness 근거를 끝까지 수집한다.
READINESS_EVIDENCE_FAILED=0
announce_run "phase13-supply-chain-evidence-binding"
if run_phase13_supply_evidence_binding; then
    announce_pass "phase13-supply-chain-evidence-binding"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "phase13-supply-chain-evidence-binding"
fi

announce_run "phase13-offline-vulnerability-scan"
if run_supply_chain_vulnerability_scan; then
    announce_pass "phase13-offline-vulnerability-scan"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "phase13-offline-vulnerability-scan"
fi

announce_run "phase13-offline-license-scan"
if run_supply_chain_license_scan; then
    announce_pass "phase13-offline-license-scan"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "phase13-offline-license-scan"
fi

announce_run "phase13-visual-regression"
if run_phase13_visual_regression_checker; then
    announce_pass "phase13-visual-regression"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "phase13-visual-regression"
fi

announce_run "communication-traceability-checker"
if run_communication_traceability_checker; then
    announce_pass "communication-traceability-checker"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "communication-traceability-checker"
fi

announce_run "phase13-readiness-gap-gate"
if run_phase13_readiness_gate; then
    announce_pass "phase13-readiness-gap-gate"
else
    READINESS_EVIDENCE_FAILED=1
    announce_blocked "phase13-readiness-gap-gate"
fi

# 개별 evidence command가 실패해도 나머지 gate 근거를 수집한 뒤 단일 NO_GO로 반환한다.
if [ "${READINESS_EVIDENCE_FAILED}" -ne 0 ]; then
    fail "하나 이상의 readiness evidence gate가 차단됐습니다."
fi

echo "check_all: PASS: all local no-order readiness checks completed."
