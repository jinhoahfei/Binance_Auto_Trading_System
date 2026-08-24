#!/bin/sh

# Phase 12 Python entrypoint를 Tauri externalBin target suffix 규칙에 맞춰 패키징한다.
set -eu

# Script 위치에서 repository 경로를 결정해 호출 작업 directory에 의존하지 않는다.
SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "${SCRIPT_DIRECTORY}/.." && pwd)
BACKEND_DIRECTORY="${REPOSITORY_ROOT}/backend"
ENTRYPOINT_PATH="${BACKEND_DIRECTORY}/src/binance_auto_trader/sidecar.py"
BINARY_NAME="binance-auto-sidecar"
OUTPUT_DIRECTORY="${REPOSITORY_ROOT}/UI/apps/desktop/src-tauri/binaries"
REQUIRED_PYINSTALLER_VERSION="6.22.2"

# 함수 이름: fail()
# 기능: secret이나 environment 값을 반사하지 않는 packaging 오류로 중단한다.
# 인자: message -> 고정 운영 오류 설명
# 반환값: exit status 1
# 작성 날짜: 2026/08/24
fail() {
    message=$1
    echo "package_sidecar: ${message}" >&2
    exit 1
}

# 함수 이름: resolve_host_target()
# 기능: Tauri가 요구하는 Rust host target triple을 rustc에서 읽는다.
# 인자: 없음
# 반환값: stdout의 host target triple
# 작성 날짜: 2026/08/24
resolve_host_target() {
    rustc --print host-tuple 2>/dev/null || fail "Rust host target을 확인할 수 없습니다."
}

# 함수 이름: resolve_packaging_python()
# 기능: backend dependency와 PyInstaller가 같은 interpreter에서 해석되도록 packaging Python을 고른다.
# 인자: 없음
# 반환값: stdout의 Python executable absolute path
# 작성 날짜: 2026/08/24
resolve_packaging_python() {
    if [ -x "${BACKEND_DIRECTORY}/.venv/bin/python" ]; then
        echo "${BACKEND_DIRECTORY}/.venv/bin/python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    fail "packaging Python을 확인할 수 없습니다."
}

# 함수 이름: verify_packaging_dependencies()
# 기능: clean bundle에 필수인 PyInstaller와 lazy-import websocket-client가 같은 interpreter에 있는지 fail-fast 검증한다.
# 인자: 없음
# 반환값: 필수 module이 모두 있으면 0, 아니면 fail()
# 작성 날짜: 2026/08/24
verify_packaging_dependencies() {
    "${PACKAGING_PYTHON}" -c \
        'import PyInstaller, sys; raise SystemExit(PyInstaller.__version__ != sys.argv[1])' \
        "${REQUIRED_PYINSTALLER_VERSION}" >/dev/null 2>&1 || \
        fail "packaging Python의 PyInstaller version이 고정 계약과 다릅니다."
    "${PACKAGING_PYTHON}" -c 'import websocket' >/dev/null 2>&1 || \
        fail "packaging Python에 websocket-client가 설치되어 있지 않습니다."
}

# 함수 이름: run_pyinstaller()
# 기능: 검증된 packaging Python으로 exact entrypoint를 one-file build한다.
# 인자: PyInstaller option 목록
# 반환값: PyInstaller exit status
# 작성 날짜: 2026/08/24
run_pyinstaller() {
    "${PACKAGING_PYTHON}" -m PyInstaller "$@"
}

# 함수 이름: cleanup_package_work_directory()
# 기능: mktemp가 만든 이번 실행의 isolated build directory만 제거한다.
# 인자: 없음
# 반환값: 없음
# 작성 날짜: 2026/08/24
cleanup_package_work_directory() {
    if [ -n "${PACKAGE_WORK_DIRECTORY:-}" ] && [ -d "${PACKAGE_WORK_DIRECTORY}" ]; then
        rm -rf -- "${PACKAGE_WORK_DIRECTORY}"
    fi
}

# Optional 인자는 cross-build ambiguity 없이 exact output suffix 하나만 선택한다.
[ "$#" -le 1 ] || fail "target triple 인자는 하나만 허용합니다."
HOST_TARGET=$(resolve_host_target)
SELECTED_TARGET=${1:-${HOST_TARGET}}
PACKAGING_PYTHON=$(resolve_packaging_python)
case "${SELECTED_TARGET}" in
    aarch64-apple-darwin|x86_64-apple-darwin|universal-apple-darwin) ;;
    *) fail "지원하지 않는 macOS target triple입니다." ;;
esac
if [ "${SELECTED_TARGET}" != "${HOST_TARGET}" ] && [ "${SELECTED_TARGET}" != "universal-apple-darwin" ]; then
    fail "PyInstaller host와 다른 architecture cross-build는 지원하지 않습니다."
fi

# Source entrypoint와 output directory만 사용하며 .env나 credential 파일을 build input에 넣지 않는다.
[ -f "${ENTRYPOINT_PATH}" ] || fail "production sidecar entrypoint가 없습니다."
verify_packaging_dependencies
mkdir -p "${OUTPUT_DIRECTORY}"
PACKAGE_WORK_DIRECTORY=$(mktemp -d)
trap cleanup_package_work_directory EXIT HUP INT TERM
PYINSTALLER_CONFIG_DIR="${PACKAGE_WORK_DIRECTORY}/pyinstaller-cache"
export PYINSTALLER_CONFIG_DIR

# PyInstaller build/cache artifacts는 isolated temporary directory에 두고 final executable만 publication한다.
if [ "${SELECTED_TARGET}" = "universal-apple-darwin" ]; then
    run_pyinstaller \
        --clean \
        --noconfirm \
        --onefile \
        --noupx \
        --log-level WARN \
        --hidden-import websocket \
        --target-architecture universal2 \
        --name "${BINARY_NAME}" \
        --paths "${BACKEND_DIRECTORY}/src" \
        --distpath "${PACKAGE_WORK_DIRECTORY}/dist" \
        --workpath "${PACKAGE_WORK_DIRECTORY}/work" \
        --specpath "${PACKAGE_WORK_DIRECTORY}/spec" \
        "${ENTRYPOINT_PATH}"
else
    run_pyinstaller \
        --clean \
        --noconfirm \
        --onefile \
        --noupx \
        --log-level WARN \
        --hidden-import websocket \
        --name "${BINARY_NAME}" \
        --paths "${BACKEND_DIRECTORY}/src" \
        --distpath "${PACKAGE_WORK_DIRECTORY}/dist" \
        --workpath "${PACKAGE_WORK_DIRECTORY}/work" \
        --specpath "${PACKAGE_WORK_DIRECTORY}/spec" \
        "${ENTRYPOINT_PATH}"
fi

# Tauri는 logical externalBin 이름에 target suffix가 붙은 source artifact를 요구한다.
PACKAGED_BINARY="${PACKAGE_WORK_DIRECTORY}/dist/${BINARY_NAME}"
TARGET_BINARY="${OUTPUT_DIRECTORY}/${BINARY_NAME}-${SELECTED_TARGET}"
[ -x "${PACKAGED_BINARY}" ] || fail "PyInstaller executable이 생성되지 않았습니다."
install -m 0755 "${PACKAGED_BINARY}" "${TARGET_BINARY}"
[ -s "${TARGET_BINARY}" ] || fail "target-suffixed sidecar가 비어 있습니다."

echo "package_sidecar: ${TARGET_BINARY}"
