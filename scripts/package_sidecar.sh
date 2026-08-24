#!/bin/sh

# Phase 12 Python entrypoint를 Tauri externalBin target suffix 규칙에 맞춰 패키징한다.
set -eu

# Script 위치에서 repository 경로를 결정해 호출 작업 directory에 의존하지 않는다.
SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "${SCRIPT_DIRECTORY}/.." && pwd)
BACKEND_DIRECTORY="${REPOSITORY_ROOT}/backend"
ENTRYPOINT_PATH="${BACKEND_DIRECTORY}/src/binance_auto_trader/sidecar.py"
BINARY_NAME="binance-auto-sidecar"
OUTPUT_DIRECTORY="${REPOSITORY_ROOT}/UI/apps/desktop/src-tauri/binaries"
TAURI_CONFIGURATION_DIRECTORY="${REPOSITORY_ROOT}/UI/apps/desktop/src-tauri"
REQUIRED_PYINSTALLER_VERSION="6.22.2"
PACKAGE_WORK_DIRECTORY=""
PACKAGE_WORK_DIRECTORY_DEVICE_INODE=""
PACKAGE_WORK_DIRECTORY_OWNED=0
PACKAGE_TEMPORARY_PARENT=""
PACKAGE_TEMPORARY_PARENT_DEVICE_INODE=""
RELEASE_PROVENANCE_PATH=""

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
    run_packaging_python -c \
        'import PyInstaller, sys; raise SystemExit(PyInstaller.__version__ != sys.argv[1])' \
        "${REQUIRED_PYINSTALLER_VERSION}" >/dev/null 2>&1 || \
        fail "packaging Python의 PyInstaller version이 고정 계약과 다릅니다."
    run_packaging_python -c 'import websocket' >/dev/null 2>&1 || \
        fail "packaging Python에 websocket-client가 설치되어 있지 않습니다."
    # Uv standalone Python에서 _ssl·_hashlib은 static builtin일 수 있으므로 file 유무가 아닌 import로 TLS runtime을 고정한다.
    run_packaging_python -c 'import _hashlib, _ssl, ssl' >/dev/null 2>&1 || \
        fail "packaging Python의 TLS runtime module을 확인할 수 없습니다."
}

# 함수 이름: run_packaging_python()
# 기능: Tauri 전용 certificate material을 제거한 environment에서 packaging Python을 실행한다.
# 인자: packaging Python argument 목록
# 반환값: packaging Python exit status
# 작성 날짜: 2026/08/24
run_packaging_python() {
    # PyInstaller와 config validator에 certificate·notarization·exchange credential을 상속하지 않는다.
    /usr/bin/env \
        -u AC_PASSWORD \
        -u APPLE_API_ISSUER \
        -u APPLE_API_KEY \
        -u APPLE_API_KEY_ID \
        -u APPLE_API_KEY_PATH \
        -u APPLE_CERTIFICATE \
        -u APPLE_CERTIFICATE_PASSWORD \
        -u APPLE_ID \
        -u APPLE_PASSWORD \
        -u APPLE_TEAM_ID \
        -u APPLE_NOTARY_PROFILE \
        -u APPLE_KEYCHAIN_PROFILE \
        -u ASC_API_KEY \
        -u ASC_API_KEY_PATH \
        -u ASC_ISSUER_ID \
        -u NOTARY_PROFILE \
        -u TAURI_CONFIG \
        -u BINANCE_API_KEY \
        -u BINANCE_API_SECRET \
        -u BINANCE_TESTNET_API_KEY \
        -u BINANCE_TESTNET_API_SECRET \
        -u PYTHONHOME \
        -u PYTHONPATH \
        -u PYTHONSTARTUP \
        -u PYTHONUSERBASE \
        PYTHONNOUSERSITE=1 \
        "${PACKAGING_PYTHON}" "$@"
}

# 함수 이름: run_pyinstaller()
# 기능: 검증된 packaging Python과 선택적 signing identity로 exact entrypoint를 one-file build한다.
# 인자: PyInstaller option 목록
# 반환값: PyInstaller exit status
# 작성 날짜: 2026/08/24
run_pyinstaller() {
    # One-file 내부 dylib는 사후 재서명할 수 없으므로 signing identity를 PyInstaller에도 전달한다.
    if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
        run_packaging_python -m PyInstaller \
            --codesign-identity "${APPLE_SIGNING_IDENTITY}" \
            --add-data "${RELEASE_PROVENANCE_PATH}:." \
            "$@"
        return
    fi

    run_packaging_python -m PyInstaller "$@"
}

# 함수 이름: create_release_provenance()
# 기능: 검증된 commit·version·build를 canonical exact-key JSON resource로 isolated work tree에 생성한다.
# 인자: provenance_path -> O_EXCL로 생성할 resource path
# 반환값: canonical resource를 생성하면 0, 아니면 nonzero
# 작성 날짜: 2026/08/24
create_release_provenance() {
    provenance_path=$1
    printf '%s' "${TAURI_CONFIG}" | run_packaging_python -c '
import json
import os
import re
import sys

configuration = json.load(sys.stdin)
provenance_path = sys.argv[2]
release_commit = sys.argv[3]
if re.fullmatch(r"[0-9a-f]{40}", release_commit) is None:
    raise SystemExit(1)
release_version = configuration["version"]
build_version = configuration["bundle"]["macOS"]["bundleVersion"]
if not isinstance(release_version, str) or not isinstance(build_version, str):
    raise SystemExit(1)
provenance = {
    "schema_version": 1,
    "commit": release_commit,
    "version": release_version,
    "build_version": build_version,
}
encoded_provenance = (
    json.dumps(
        provenance,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
descriptor = os.open(
    provenance_path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
    0o600,
)
try:
    with os.fdopen(descriptor, "wb", closefd=False) as provenance_file:
        provenance_file.write(encoded_provenance)
        provenance_file.flush()
finally:
    os.close(descriptor)
' --phase12-create-release-provenance \
        "${provenance_path}" \
        "${PHASE12_RELEASE_COMMIT}" >/dev/null 2>&1
}

# 함수 이름: verify_codesigning_configuration()
# 기능: APPLE_SIGNING_IDENTITY 외의 identity 추론과 불완전한 certificate 구성을 거부한다.
# 인자: 없음
# 반환값: local ad-hoc 또는 명시 signing identity 구성이면 0, 아니면 fail()
# 작성 날짜: 2026/08/24
verify_codesigning_configuration() {
    # Pseudo identity는 PyInstaller embedded library와 Tauri executable의 서명 계약을 보장하지 않는다.
    if [ "${APPLE_SIGNING_IDENTITY:-}" = "-" ]; then
        fail "one-file sidecar에는 APPLE_SIGNING_IDENTITY '-'를 사용할 수 없습니다."
    fi

    # Signed branch는 exact Developer ID Application subject와 canonical 10자 Team ID만 허용한다.
    if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
        case "${APPLE_SIGNING_IDENTITY}" in
            *'
'*)
                fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."
                ;;
            "Developer ID Application: "?*" ("??????????")") ;;
            *)
                fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."
                ;;
        esac

        identity_without_closing_parenthesis=${APPLE_SIGNING_IDENTITY%)}
        signing_team_identifier=${identity_without_closing_parenthesis##*" ("}
        case "${signing_team_identifier}" in
            *[!A-Z0-9]*|"")
                fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."
                ;;
        esac
        [ "${#signing_team_identifier}" -eq 10 ] || \
            fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."
    fi

    # Tauri가 certificate를 import하기 전에 sidecar hook이 실행되므로 identity 자동 추론을 허용하지 않는다.
    if [ -n "${APPLE_CERTIFICATE:-}" ] && [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
        fail "APPLE_CERTIFICATE build에는 APPLE_SIGNING_IDENTITY를 명시해야 합니다."
    fi
    if [ -n "${APPLE_CERTIFICATE_PASSWORD:-}" ] && [ -z "${APPLE_CERTIFICATE:-}" ]; then
        fail "APPLE_CERTIFICATE_PASSWORD에는 APPLE_CERTIFICATE가 필요합니다."
    fi
}

# 함수 이름: validate_tauri_json_configuration()
# 기능: JSON property를 decode한 뒤 signing identity key와 선택적 release version 계약을 검증한다.
# 인자: configuration_path -> JSON file 경로 또는 stdin을 뜻하는 '-'
#     validation_mode -> identity 또는 signed-release
# 반환값: 유효하면 0, JSON 1, signing identity 2, version 3, bundleVersion 4
# 작성 날짜: 2026/08/24
validate_tauri_json_configuration() {
    # JSON parser로 escaped key까지 decode하고 오류 출력은 configuration value를 반사하지 않도록 버린다.
    run_packaging_python -c '
import json
from pathlib import Path
import re
import sys


SEMANTIC_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
BUNDLE_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}"
)


def contains_signing_identity(configuration_value):
    """
    함수 이름: contains_signing_identity()
    기능: JSON object·array 안에 허용하지 않는 signing identity key가 있는지 재귀 검사한다.
    인자: configuration_value -> 현재 검사할 decoded JSON 값
    반환값: signing identity key가 있으면 True, 없으면 False
    작성 날짜: 2026/08/24
    """
    # Object key를 먼저 검사한 뒤 각 value에 같은 검사를 재귀 적용한다.
    if isinstance(configuration_value, dict):
        return any(
            configuration_key in {
                "signingIdentity",
                "signing-identity",
                "signing_identity",
            }
            or contains_signing_identity(nested_value)
            for configuration_key, nested_value in configuration_value.items()
        )

    # Array element도 object와 array를 포함할 수 있으므로 순서대로 재귀 검사한다.
    if isinstance(configuration_value, list):
        return any(
            contains_signing_identity(nested_value)
            for nested_value in configuration_value
        )
    return False


# 검증 대상 source를 file 또는 stdin에서 읽고 JSON decode 실패를 secret-free status로 바꾼다.
configuration_path = sys.argv[2]
validation_mode = sys.argv[3]
try:
    configuration_text = (
        sys.stdin.read()
        if configuration_path == "-"
        else Path(configuration_path).read_text(encoding="utf-8")
    )
    configuration = json.loads(configuration_text)
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)

# 재귀 검사 결과만 process status로 반환하고 configuration content는 출력하지 않는다.
if contains_signing_identity(configuration):
    raise SystemExit(2)

if validation_mode == "signed-release":
    # Tauri merge root에 exact SemVer가 있어야 base config version을 묵시적으로 재사용하지 않는다.
    release_version = (
        configuration.get("version")
        if isinstance(configuration, dict)
        else None
    )
    if not isinstance(release_version, str) or not SEMANTIC_VERSION_PATTERN.fullmatch(
        release_version
    ):
        raise SystemExit(3)

    # CFBundleVersion은 leading zero가 없는 1~3개 숫자 component며 전체 0은 금지한다.
    bundle_configuration = configuration.get("bundle")
    macos_configuration = (
        bundle_configuration.get("macOS")
        if isinstance(bundle_configuration, dict)
        else None
    )
    bundle_version = (
        macos_configuration.get("bundleVersion")
        if isinstance(macos_configuration, dict)
        else None
    )
    if (
        not isinstance(bundle_version, str)
        or not BUNDLE_VERSION_PATTERN.fullmatch(bundle_version)
        or not any(component != "0" for component in bundle_version.split("."))
    ):
        raise SystemExit(4)

raise SystemExit(0)
' --phase12-validate-tauri-config "${1}" "${2:-identity}" >/dev/null 2>&1
}

# 함수 이름: verify_tauri_signing_identity_sources()
# 기능: Tauri file과 runtime merge 설정이 environment identity 정책을 우회하지 않는지 검증한다.
# 인자: 없음
# 반환값: signingIdentity override가 없으면 0, 있으면 fail()
# 작성 날짜: 2026/08/24
verify_tauri_signing_identity_sources() {
    # Custom·platform JSON configuration을 parser로 검사해 escaped signingIdentity key도 거부한다.
    for configuration_path in "${TAURI_CONFIGURATION_DIRECTORY}"/*.json
    do
        [ -f "${configuration_path}" ] || continue
        if validate_tauri_json_configuration "${configuration_path}" identity; then
            :
        else
            configuration_status=$?
            case "${configuration_status}" in
                1) fail "Tauri JSON configuration을 유효한 JSON으로 decode할 수 없습니다." ;;
                2) fail "Tauri configuration signingIdentity 대신 APPLE_SIGNING_IDENTITY를 사용해야 합니다." ;;
                *) fail "Tauri JSON configuration을 검증할 수 없습니다." ;;
            esac
        fi
    done

    # 이 repository가 사용하지 않는 JSON5·TOML config는 comment parsing 우회 없이 fail-closed 처리한다.
    for unsupported_configuration_path in \
        "${TAURI_CONFIGURATION_DIRECTORY}"/*.json5 \
        "${TAURI_CONFIGURATION_DIRECTORY}"/Tauri.toml \
        "${TAURI_CONFIGURATION_DIRECTORY}"/Tauri.*.toml \
        "${TAURI_CONFIGURATION_DIRECTORY}"/tauri*.toml
    do
        [ -f "${unsupported_configuration_path}" ] || continue
        fail "Tauri JSON5/TOML signing configuration은 지원하지 않습니다."
    done

    # Developer ID release는 base config에 의존하지 않도록 runtime version/build merge를 반드시 제공한다.
    runtime_validation_mode=identity
    if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
        [ -n "${TAURI_CONFIG:-}" ] || \
            fail "APPLE_SIGNING_IDENTITY release에는 명시적 TAURI_CONFIG version/build가 필요합니다."
        runtime_validation_mode=signed-release
    fi

    # TAURI_CONFIG에 전달된 runtime merge도 저장된 configuration과 같은 identity 정책으로 닫는다.
    if [ -n "${TAURI_CONFIG:-}" ]; then
        if printf '%s' "${TAURI_CONFIG}" | \
            validate_tauri_json_configuration - "${runtime_validation_mode}"
        then
            :
        else
            configuration_status=$?
            case "${configuration_status}" in
                1) fail "TAURI_CONFIG는 유효한 JSON이어야 합니다." ;;
                2) fail "Tauri runtime signingIdentity 대신 APPLE_SIGNING_IDENTITY를 사용해야 합니다." ;;
                3) fail "서명 release TAURI_CONFIG root version은 명시적 SemVer여야 합니다." ;;
                4) fail "서명 release TAURI_CONFIG bundle.macOS.bundleVersion은 nonzero canonical build identifier여야 합니다." ;;
                *) fail "TAURI_CONFIG를 검증할 수 없습니다." ;;
            esac
        fi
    fi
}

# 함수 이름: run_repository_git()
# 기능: caller의 GIT_* control·credential을 전부 제거하고 fixed repository에서 platform Git을 실행한다.
# 인자: git subcommand와 argument 목록
# 반환값: fixed Git command exit status
# 작성 날짜: 2026/08/24
run_repository_git() {
    /usr/bin/env -i \
        PATH=/usr/bin:/bin \
        LC_ALL=C \
        /usr/bin/git \
        --git-dir "${REPOSITORY_ROOT}/.git" \
        --work-tree "${REPOSITORY_ROOT}" \
        "$@"
}

# 함수 이름: verify_release_commit()
# 기능: Developer ID build가 explicit lowercase commit의 clean fixed HEAD에서만 시작되게 한다.
# 인자: 없음
# 반환값: unsigned build 또는 commit·HEAD·clean gate가 일치하면 0, 아니면 fail()
# 작성 날짜: 2026/08/24
verify_release_commit() {
    [ -n "${APPLE_SIGNING_IDENTITY:-}" ] || return 0

    release_commit=${PHASE12_RELEASE_COMMIT:-}
    [ "${#release_commit}" -eq 40 ] || \
        fail "Developer ID release에는 exact lowercase PHASE12_RELEASE_COMMIT이 필요합니다."
    case "${release_commit}" in
        *[!0-9a-f]*)
            fail "Developer ID release에는 exact lowercase PHASE12_RELEASE_COMMIT이 필요합니다."
            ;;
    esac
    [ -d "${REPOSITORY_ROOT}/.git" ] && [ ! -L "${REPOSITORY_ROOT}/.git" ] || \
        fail "fixed release Git repository를 확인하지 못했습니다."
    [ -x /usr/bin/git ] || fail "platform Git command를 찾을 수 없습니다."

    repository_head=$(run_repository_git rev-parse --verify 'HEAD^{commit}' \
        2>/dev/null) || fail "fixed release HEAD를 확인하지 못했습니다."
    [ "${#repository_head}" -eq 40 ] || \
        fail "fixed release HEAD를 canonical commit으로 확인하지 못했습니다."
    case "${repository_head}" in
        *[!0-9a-f]*) fail "fixed release HEAD가 canonical commit이 아닙니다." ;;
    esac
    [ "${repository_head}" = "${release_commit}" ] || \
        fail "PHASE12_RELEASE_COMMIT이 fixed release HEAD와 일치하지 않습니다."

    repository_status=$(run_repository_git status \
        --porcelain=v1 \
        --untracked-files=all 2>/dev/null) || \
        fail "fixed release worktree 상태를 확인하지 못했습니다."
    [ -z "${repository_status}" ] || \
        fail "Developer ID release는 clean fixed HEAD에서만 생성할 수 있습니다."
}

# 함수 이름: cleanup_package_work_directory()
# 기능: mktemp가 만든 이번 실행의 isolated build directory만 제거한다.
# 인자: 없음
# 반환값: 없음
# 작성 날짜: 2026/08/24
cleanup_package_work_directory() {
    [ "${PACKAGE_WORK_DIRECTORY_OWNED:-0}" = "1" ] || return 0
    [ -n "${PACKAGE_WORK_DIRECTORY:-}" ] || return 0
    [ -n "${PACKAGE_TEMPORARY_PARENT:-}" ] || return 0
    [ "${PACKAGE_WORK_DIRECTORY}" != "/" ] || return 0
    case "${PACKAGE_WORK_DIRECTORY}" in
        "${PACKAGE_TEMPORARY_PARENT}"/binance-auto-sidecar-package.*) ;;
        *) return 0 ;;
    esac
    [ -d "${PACKAGE_WORK_DIRECTORY}" ] || return 0
    [ ! -L "${PACKAGE_WORK_DIRECTORY}" ] || return 0

    current_temporary_parent_device_inode=$(
        /usr/bin/stat -f '%d:%i' "${PACKAGE_TEMPORARY_PARENT}" 2>/dev/null
    ) || return 0
    [ "${current_temporary_parent_device_inode}" = \
        "${PACKAGE_TEMPORARY_PARENT_DEVICE_INODE}" ] || return 0
    current_package_work_device_inode=$(
        /usr/bin/stat -f '%d:%i' "${PACKAGE_WORK_DIRECTORY}" 2>/dev/null
    ) || return 0
    [ "${current_package_work_device_inode}" = \
        "${PACKAGE_WORK_DIRECTORY_DEVICE_INODE}" ] || return 0

    # Absolute platform rm은 PATH shim을 실행하지 않고, 소유 inode가 유지된 exact tree만 제거한다.
    /bin/rm -rf -- "${PACKAGE_WORK_DIRECTORY}" >/dev/null 2>&1 || :
}

# Optional 인자는 cross-build ambiguity 없이 exact output suffix 하나만 선택한다.
[ "$#" -le 1 ] || fail "target triple 인자는 하나만 허용합니다."
verify_codesigning_configuration
HOST_TARGET=$(resolve_host_target)
SELECTED_TARGET=${1:-${HOST_TARGET}}
PACKAGING_PYTHON=$(resolve_packaging_python)
verify_tauri_signing_identity_sources
verify_release_commit
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
/bin/mkdir -p "${OUTPUT_DIRECTORY}"

# TMPDIR을 physical absolute parent로 고정하고, prefix·type·inode를 기록한 mktemp tree만 소유한다.
PACKAGE_TEMPORARY_PARENT=$(CDPATH= cd -P -- "${TMPDIR:-/tmp}" && pwd) || \
    fail "temporary parent directory를 확인할 수 없습니다."
case "${PACKAGE_TEMPORARY_PARENT}" in
    /*) ;;
    *) fail "temporary parent directory는 absolute path여야 합니다." ;;
esac
[ "${PACKAGE_TEMPORARY_PARENT}" != "/" ] || \
    fail "temporary parent로 root directory를 사용할 수 없습니다."
[ -d "${PACKAGE_TEMPORARY_PARENT}" ] && [ ! -L "${PACKAGE_TEMPORARY_PARENT}" ] || \
    fail "temporary parent directory를 확인할 수 없습니다."
PACKAGE_TEMPORARY_PARENT_DEVICE_INODE=$(
    /usr/bin/stat -f '%d:%i' "${PACKAGE_TEMPORARY_PARENT}" 2>/dev/null
) || fail "temporary parent inode를 확인할 수 없습니다."
PACKAGE_WORK_DIRECTORY=$(
    /usr/bin/mktemp -d \
        "${PACKAGE_TEMPORARY_PARENT}/binance-auto-sidecar-package.XXXXXX"
) || fail "isolated package work directory를 만들지 못했습니다."
case "${PACKAGE_WORK_DIRECTORY}" in
    "${PACKAGE_TEMPORARY_PARENT}"/binance-auto-sidecar-package.*) ;;
    *) fail "isolated package work directory를 확인하지 못했습니다." ;;
esac
[ -d "${PACKAGE_WORK_DIRECTORY}" ] && [ ! -L "${PACKAGE_WORK_DIRECTORY}" ] || \
    fail "isolated package work directory를 확인하지 못했습니다."
PACKAGE_WORK_DIRECTORY_DEVICE_INODE=$(
    /usr/bin/stat -f '%d:%i' "${PACKAGE_WORK_DIRECTORY}" 2>/dev/null
) || fail "isolated package work inode를 확인하지 못했습니다."
PACKAGE_WORK_DIRECTORY_OWNED=1
trap cleanup_package_work_directory EXIT HUP INT TERM
PYINSTALLER_CONFIG_DIR="${PACKAGE_WORK_DIRECTORY}/pyinstaller-cache"
export PYINSTALLER_CONFIG_DIR

# Signed one-file archive에만 outer app seal과 함께 결합될 current-HEAD provenance resource를 포함한다.
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    RELEASE_PROVENANCE_PATH="${PACKAGE_WORK_DIRECTORY}/phase12-release-provenance.json"
    create_release_provenance "${RELEASE_PROVENANCE_PATH}" || \
        fail "signed release provenance resource를 생성하지 못했습니다."
    [ -f "${RELEASE_PROVENANCE_PATH}" ] && [ ! -L "${RELEASE_PROVENANCE_PATH}" ] && \
        [ -s "${RELEASE_PROVENANCE_PATH}" ] || \
        fail "signed release provenance regular file을 확인하지 못했습니다."
fi

# PyInstaller build/cache artifacts는 isolated temporary directory에 두고 final executable만 publication한다.
if [ "${SELECTED_TARGET}" = "universal-apple-darwin" ]; then
    run_pyinstaller \
        --clean \
        --noconfirm \
        --onefile \
        --noupx \
        --log-level WARN \
        --hidden-import websocket \
        --hidden-import _ssl \
        --hidden-import _hashlib \
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
        --hidden-import _ssl \
        --hidden-import _hashlib \
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
/usr/bin/install -m 0755 "${PACKAGED_BINARY}" "${TARGET_BINARY}"
[ -s "${TARGET_BINARY}" ] || fail "target-suffixed sidecar가 비어 있습니다."

echo "package_sidecar: ${TARGET_BINARY}"
