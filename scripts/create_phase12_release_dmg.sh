#!/bin/sh

# Staple된 Phase 12 macOS app으로 notarization 제출 전 최종 서명 DMG를 생성한다.
set -eu

RELEASE_WORK_DIRECTORY=""
RELEASE_WORK_DIRECTORY_DEVICE_INODE=""
RELEASE_WORK_DIRECTORY_OWNED=0
STAGING_DMG_PATH=""
STAGING_DMG_DEVICE_INODE=""
STAGING_DMG_OWNED=0
OUTPUT_DIRECTORY_DEVICE_INODE=""

# 함수 이름: fail()
# 기능: identity나 artifact path를 반사하지 않는 고정 release 오류로 중단한다.
# 인자: message -> 고정 운영 오류 설명
# 반환값: exit status 1
# 작성 날짜: 2026/08/24
fail() {
    message=$1
    echo "create_phase12_release_dmg: ${message}" >&2
    exit 1
}

# 함수 이름: run_release_tool()
# 기능: signing identity만 유지하고 known Apple certificate·account secret을 제거해 child를 실행한다.
# 인자: command와 argument 목록
# 반환값: child command exit status
# 작성 날짜: 2026/08/24
run_release_tool() {
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
        -u ASC_API_KEY \
        -u ASC_API_KEY_PATH \
        -u ASC_ISSUER_ID \
        -u CODESIGN_ALLOCATE \
        -u DEVELOPER_DIR \
        -u DYLD_FALLBACK_FRAMEWORK_PATH \
        -u DYLD_FALLBACK_LIBRARY_PATH \
        -u DYLD_FRAMEWORK_PATH \
        -u DYLD_IMAGE_SUFFIX \
        -u DYLD_INSERT_LIBRARIES \
        -u DYLD_LIBRARY_PATH \
        -u DYLD_ROOT_PATH \
        -u NOTARY_PROFILE \
        -u PYTHONHOME \
        -u PYTHONPATH \
        -u PYTHONSTARTUP \
        -u PYTHONUSERBASE \
        -u SDKROOT \
        -u TOOLCHAINS \
        PATH=/usr/bin:/bin \
        PYTHONNOUSERSITE=1 \
        "$@"
}

# 함수 이름: path_device_inode()
# 기능: symlink를 따라가지 않은 exact path의 device·inode identity를 반환한다.
# 인자: artifact_path -> 검사할 path
# 반환값: regular file/directory의 device:inode, 그 외 nonzero
# 작성 날짜: 2026/08/24
path_device_inode() {
    guarded_path=$1
    [ ! -L "${guarded_path}" ] || return 1
    /usr/bin/stat -f '%d:%i' "${guarded_path}" 2>/dev/null
}

# 함수 이름: validate_absolute_artifact_path()
# 기능: option·상대 경로·dot component로 해석이 달라질 수 있는 artifact path를 거부한다.
# 인자: artifact_path -> 검사할 app 또는 DMG absolute path
# 반환값: 명확한 absolute path이면 0, 아니면 fail()
# 작성 날짜: 2026/08/24
validate_absolute_artifact_path() {
    artifact_path=$1

    case "${artifact_path}" in
        /*) ;;
        *) fail "artifact path는 명확한 absolute path여야 합니다." ;;
    esac
    case "${artifact_path}" in
        *//*|*/./*|*/../*|*/.|*/..)
            fail "artifact path에 모호한 path component를 사용할 수 없습니다."
            ;;
    esac
    case "${artifact_path}" in
        *'
'*|*'|'*)
            fail "artifact path에 제어 문자나 reserved delimiter를 사용할 수 없습니다."
            ;;
    esac
}

# 함수 이름: verify_owned_staging_dmg()
# 기능: output parent와 staging DMG의 exact inode를 재확인하고 final path race를 거부한다.
# 인자: 없음
# 반환값: 소유 path가 유지되면 0, 아니면 fail()
# 작성 날짜: 2026/08/24
verify_owned_staging_dmg() {
    current_output_directory_device_inode=$(path_device_inode \
        "${OUTPUT_DIRECTORY}") || \
        fail "출력 DMG parent inode를 재검증하지 못했습니다."
    [ "${current_output_directory_device_inode}" = \
        "${OUTPUT_DIRECTORY_DEVICE_INODE}" ] || \
        fail "출력 DMG parent inode가 변경되었습니다."
    [ "${STAGING_DMG_OWNED:-0}" = "1" ] || \
        fail "staging DMG 소유 상태를 확인하지 못했습니다."
    [ -f "${STAGING_DMG_PATH}" ] && [ ! -L "${STAGING_DMG_PATH}" ] || \
        fail "staging DMG regular file을 재검증하지 못했습니다."
    current_staging_dmg_device_inode=$(path_device_inode \
        "${STAGING_DMG_PATH}") || \
        fail "staging DMG inode를 재검증하지 못했습니다."
    [ "${current_staging_dmg_device_inode}" = \
        "${STAGING_DMG_DEVICE_INODE}" ] || \
        fail "staging DMG inode가 변경되었습니다."
    if [ -e "${OUTPUT_DMG_PATH}" ] || [ -L "${OUTPUT_DMG_PATH}" ]; then
        fail "출력 DMG가 이미 존재합니다."
    fi
}

# 함수 이름: publish_owned_staging_dmg()
# 기능: renameatx_np EXCL·NOFOLLOW_ANY로 검증된 inode를 final name으로 atomic no-clobber publish한다.
# 인자: 없음
# 반환값: publication이 완료되면 0, race·swap·existing output은 nonzero
# 작성 날짜: 2026/08/24
publish_owned_staging_dmg() {
    run_release_tool /usr/bin/python3 -c '
import ctypes
import os
import stat
import sys

(
    source_parent_path,
    expected_source_parent,
    staging_name,
    expected_staging,
    output_parent_path,
    expected_output_parent,
    final_name,
) = sys.argv[1:]
expected_source_parent_device, expected_source_parent_inode = map(
    int, expected_source_parent.split(":")
)
expected_output_parent_device, expected_output_parent_inode = map(
    int, expected_output_parent.split(":")
)
expected_staging_device, expected_staging_inode = map(int, expected_staging.split(":"))
open_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
source_directory_descriptor = os.open(source_parent_path, open_flags)
output_directory_descriptor = os.open(output_parent_path, open_flags)
try:
    source_parent_status = os.fstat(source_directory_descriptor)
    if (source_parent_status.st_dev, source_parent_status.st_ino) != (
        expected_source_parent_device,
        expected_source_parent_inode,
    ):
        raise SystemExit(1)
    output_parent_status = os.fstat(output_directory_descriptor)
    if (output_parent_status.st_dev, output_parent_status.st_ino) != (
        expected_output_parent_device,
        expected_output_parent_inode,
    ):
        raise SystemExit(1)
    staging_status = os.stat(
        staging_name,
        dir_fd=source_directory_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(staging_status.st_mode) or (
        staging_status.st_dev,
        staging_status.st_ino,
    ) != (expected_staging_device, expected_staging_inode):
        raise SystemExit(1)
    try:
        os.stat(
            final_name,
            dir_fd=output_directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise SystemExit(1)

    libc = ctypes.CDLL(None, use_errno=True)
    renameatx = libc.renameatx_np
    renameatx.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx.restype = ctypes.c_int
    rename_exclusive = 0x00000004
    rename_nofollow_any = 0x00000010
    if renameatx(
        source_directory_descriptor,
        os.fsencode(staging_name),
        output_directory_descriptor,
        os.fsencode(final_name),
        rename_exclusive | rename_nofollow_any,
    ) != 0:
        raise SystemExit(1)

    # Parent path를 다시 open해 rename 중 directory path swap이 있었으면 소유 inode만 즉시 회수한다.
    final_status = os.stat(
        final_name,
        dir_fd=output_directory_descriptor,
        follow_symlinks=False,
    )
    if (final_status.st_dev, final_status.st_ino) != (
        expected_staging_device,
        expected_staging_inode,
    ):
        raise SystemExit(1)
    try:
        reopened_source_descriptor = os.open(source_parent_path, open_flags)
        try:
            reopened_source_status = os.fstat(reopened_source_descriptor)
        finally:
            os.close(reopened_source_descriptor)
        reopened_output_descriptor = os.open(output_parent_path, open_flags)
        try:
            reopened_output_status = os.fstat(reopened_output_descriptor)
        finally:
            os.close(reopened_output_descriptor)
    except OSError:
        reopened_source_status = None
        reopened_output_status = None
    if (
        reopened_source_status is None
        or reopened_output_status is None
        or (reopened_source_status.st_dev, reopened_source_status.st_ino)
        != (expected_source_parent_device, expected_source_parent_inode)
        or (reopened_output_status.st_dev, reopened_output_status.st_ino)
        != (expected_output_parent_device, expected_output_parent_inode)
    ):
        os.unlink(final_name, dir_fd=output_directory_descriptor)
        raise SystemExit(1)
finally:
    os.close(source_directory_descriptor)
    os.close(output_directory_descriptor)
' \
        "${RELEASE_WORK_DIRECTORY}" \
        "${RELEASE_WORK_DIRECTORY_DEVICE_INODE}" \
        "${STAGING_DMG_PATH##*/}" \
        "${STAGING_DMG_DEVICE_INODE}" \
        "${OUTPUT_DIRECTORY}" \
        "${OUTPUT_DIRECTORY_DEVICE_INODE}" \
        "${OUTPUT_DMG_NAME}" >/dev/null 2>&1
}

# 함수 이름: cleanup_release_work_directory()
# 기능: mktemp가 이번 실행에 만든 prefix 검증 완료 directory만 제거한다.
# 인자: 없음
# 반환값: 없음
# 작성 날짜: 2026/08/24
cleanup_release_work_directory() {
    cleanup_work_tree_allowed=1
    if [ "${STAGING_DMG_OWNED:-0}" = "1" ]; then
        cleanup_staging_dmg_device_inode=$(path_device_inode \
            "${STAGING_DMG_PATH:-}" 2>/dev/null) || \
            cleanup_staging_dmg_device_inode=""
        cleanup_output_directory_device_inode=$(path_device_inode \
            "${OUTPUT_DIRECTORY}" 2>/dev/null) || \
            cleanup_output_directory_device_inode=""
        if [ -n "${STAGING_DMG_PATH:-}" ] && \
            [ "${STAGING_DMG_PATH}" != "/" ] && \
            [ -f "${STAGING_DMG_PATH}" ] && \
            [ ! -L "${STAGING_DMG_PATH}" ] && \
            [ "${cleanup_staging_dmg_device_inode}" = \
            "${STAGING_DMG_DEVICE_INODE}" ] && \
            [ "${cleanup_output_directory_device_inode}" = \
            "${OUTPUT_DIRECTORY_DEVICE_INODE}" ]
        then
            /bin/rm -f -- "${STAGING_DMG_PATH}" >/dev/null 2>&1 || :
        else
            # Staging entry swap이 보이면 foreign inode를 포함한 work tree 전체도 제거하지 않는다.
            cleanup_work_tree_allowed=0
        fi
    fi

    if [ "${cleanup_work_tree_allowed}" = "1" ] && \
        [ "${RELEASE_WORK_DIRECTORY_OWNED:-0}" = "1" ] && \
        [ -n "${RELEASE_WORK_DIRECTORY:-}" ] && \
        [ "${RELEASE_WORK_DIRECTORY}" != "/" ] && \
        [ -d "${RELEASE_WORK_DIRECTORY}" ] && \
        [ ! -L "${RELEASE_WORK_DIRECTORY}" ]
    then
        cleanup_work_directory_device_inode=$(path_device_inode \
            "${RELEASE_WORK_DIRECTORY}" 2>/dev/null) || \
            cleanup_work_directory_device_inode=""
        cleanup_work_output_parent_device_inode=$(path_device_inode \
            "${OUTPUT_DIRECTORY}" 2>/dev/null) || \
            cleanup_work_output_parent_device_inode=""
        if [ "${cleanup_work_directory_device_inode}" = \
            "${RELEASE_WORK_DIRECTORY_DEVICE_INODE}" ] && \
            [ "${cleanup_work_output_parent_device_inode}" = \
            "${OUTPUT_DIRECTORY_DEVICE_INODE}" ]
        then
            # Cleanup은 exact mktemp inode와 absolute platform rm만 사용한다.
            /bin/rm -rf -- \
                "${RELEASE_WORK_DIRECTORY}" >/dev/null 2>&1 || :
        fi
    fi
}

# 두 artifact를 positional argument로 명시해 stale/default target 선택을 허용하지 않는다.
[ "$#" -eq 2 ] || fail "APP_PATH와 OUTPUT_DMG_PATH 두 인자가 필요합니다."
APP_PATH=$1
OUTPUT_DMG_PATH=$2

validate_absolute_artifact_path "${APP_PATH}"
validate_absolute_artifact_path "${OUTPUT_DMG_PATH}"

# Source는 exact non-symlink app bundle이어야 하며 root나 일반 directory를 staging하지 않는다.
APP_BUNDLE_NAME=${APP_PATH##*/}
case "${APP_BUNDLE_NAME}" in
    ?*.app) ;;
    *) fail "입력은 이름이 있는 .app bundle이어야 합니다." ;;
esac
[ -d "${APP_PATH}" ] || fail "입력 .app bundle이 존재하지 않습니다."
[ ! -L "${APP_PATH}" ] || fail "입력 .app bundle symlink는 허용하지 않습니다."

# Output은 exact 새 DMG file만 허용하고 dangling symlink도 stale artifact로 취급한다.
OUTPUT_DMG_NAME=${OUTPUT_DMG_PATH##*/}
case "${OUTPUT_DMG_NAME}" in
    ?*.dmg) ;;
    *) fail "출력은 이름이 있는 .dmg path여야 합니다." ;;
esac
if [ -e "${OUTPUT_DMG_PATH}" ] || [ -L "${OUTPUT_DMG_PATH}" ]; then
    fail "출력 DMG가 이미 존재합니다."
fi
OUTPUT_DIRECTORY=${OUTPUT_DMG_PATH%/*}
[ -n "${OUTPUT_DIRECTORY}" ] || OUTPUT_DIRECTORY="/"
[ "${OUTPUT_DIRECTORY}" != "/" ] || fail "출력 DMG parent로 root directory를 사용할 수 없습니다."
[ -d "${OUTPUT_DIRECTORY}" ] || fail "출력 DMG parent directory가 존재하지 않습니다."
[ ! -L "${OUTPUT_DIRECTORY}" ] || fail "출력 DMG parent symlink는 허용하지 않습니다."
OUTPUT_DIRECTORY_DEVICE_INODE=$(path_device_inode "${OUTPUT_DIRECTORY}") || \
    fail "출력 DMG parent inode를 확인하지 못했습니다."

# Release signing은 explicit Developer ID Application identity 하나만 신뢰한다.
SIGNING_IDENTITY=${APPLE_SIGNING_IDENTITY:-}
[ -n "${SIGNING_IDENTITY}" ] || fail "APPLE_SIGNING_IDENTITY가 필요합니다."
[ "${SIGNING_IDENTITY}" != "-" ] || fail "release DMG에는 ad-hoc identity를 사용할 수 없습니다."
case "${SIGNING_IDENTITY}" in
    *'
'*) fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다." ;;
    "Developer ID Application: "?*" ("??????????")") ;;
    *) fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다." ;;
esac
identity_without_closing_parenthesis=${SIGNING_IDENTITY%)}
signing_team_identifier=${identity_without_closing_parenthesis##*" ("}
case "${signing_team_identifier}" in
    *[!A-Z0-9]*|"")
        fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."
        ;;
esac
[ "${#signing_team_identifier}" -eq 10 ] || \
    fail "APPLE_SIGNING_IDENTITY는 canonical Developer ID Application identity여야 합니다."

# Wrapper 실행 전 parent shell에서도 known certificate·notarization account secret 참조를 제거한다.
unset \
    APPLE_API_ISSUER \
    APPLE_API_KEY \
    APPLE_API_KEY_PATH \
    APPLE_CERTIFICATE \
    APPLE_CERTIFICATE_PASSWORD \
    APPLE_ID \
    APPLE_PASSWORD

# 필요한 macOS command를 staging이나 output mutation 전에 모두 확인한다.
[ -x /usr/bin/env ] || fail "platform env command를 찾을 수 없습니다."
[ -x /usr/bin/python3 ] || fail "platform Python command를 찾을 수 없습니다."
[ -x /usr/bin/mktemp ] || fail "platform mktemp command를 찾을 수 없습니다."
[ -x /usr/bin/stat ] || fail "platform stat command를 찾을 수 없습니다."
[ -x /bin/mkdir ] || fail "platform mkdir command를 찾을 수 없습니다."
[ -x /bin/ln ] || fail "platform ln command를 찾을 수 없습니다."
[ -x /bin/rm ] || fail "platform rm command를 찾을 수 없습니다."
[ -x /usr/bin/ditto ] || fail "platform ditto command를 찾을 수 없습니다."
[ -x /usr/bin/hdiutil ] || fail "platform hdiutil command를 찾을 수 없습니다."
[ -x /usr/bin/codesign ] || fail "platform codesign command를 찾을 수 없습니다."
[ -x /usr/bin/xcrun ] || fail "platform xcrun command를 찾을 수 없습니다."

# Notarization Accepted ticket이 없는 app으로 최종 DMG를 만들지 않도록 staging 전에 검증한다.
run_release_tool /usr/bin/xcrun stapler validate "${APP_PATH}" >/dev/null 2>&1 || \
    fail "입력 .app의 notarization ticket을 검증하지 못했습니다."

# App·DMG staging tree를 output parent에 두어 final publication이 항상 same-filesystem rename이되게 한다.
RELEASE_WORK_DIRECTORY=$(run_release_tool /usr/bin/mktemp -d \
    "${OUTPUT_DIRECTORY}/.${OUTPUT_DMG_NAME}.phase12-work.XXXXXX" 2>/dev/null) || \
    fail "isolated release work directory를 만들지 못했습니다."
case "${RELEASE_WORK_DIRECTORY}" in
    "${OUTPUT_DIRECTORY}"/."${OUTPUT_DMG_NAME}".phase12-work.*) ;;
    *) fail "isolated release work directory를 확인하지 못했습니다." ;;
esac
[ -d "${RELEASE_WORK_DIRECTORY}" ] && [ ! -L "${RELEASE_WORK_DIRECTORY}" ] || \
    fail "isolated release work directory를 확인하지 못했습니다."
RELEASE_WORK_DIRECTORY_DEVICE_INODE=$(path_device_inode \
    "${RELEASE_WORK_DIRECTORY}") || \
    fail "isolated release work directory inode를 확인하지 못했습니다."
RELEASE_WORK_DIRECTORY_OWNED=1
trap cleanup_release_work_directory EXIT HUP INT TERM

STAGING_DIRECTORY="${RELEASE_WORK_DIRECTORY}/staging"
STAGED_APP_PATH="${STAGING_DIRECTORY}/${APP_BUNDLE_NAME}"
run_release_tool /bin/mkdir "${STAGING_DIRECTORY}" >/dev/null 2>&1 || \
    fail "DMG staging directory를 만들지 못했습니다."

# Ditto로 app metadata와 stapled ticket을 보존하고 standard Applications link를 함께 배치한다.
run_release_tool /usr/bin/ditto "${APP_PATH}" "${STAGED_APP_PATH}" >/dev/null 2>&1 || \
    fail "입력 .app을 DMG staging directory에 복사하지 못했습니다."
[ -d "${STAGED_APP_PATH}" ] && [ ! -L "${STAGED_APP_PATH}" ] || \
    fail "staged .app bundle을 확인하지 못했습니다."

# Copy 과정에서 ticket 또는 nested signature가 손실된 app은 DMG 생성 전에 fail closed한다.
run_release_tool /usr/bin/xcrun stapler validate \
    "${STAGED_APP_PATH}" >/dev/null 2>&1 || \
    fail "staged .app의 notarization ticket을 검증하지 못했습니다."
run_release_tool /usr/bin/codesign \
    --verify \
    --deep \
    --strict \
    --verbose=4 \
    "${STAGED_APP_PATH}" >/dev/null 2>&1 || \
    fail "staged .app의 strict 서명을 검증하지 못했습니다."

run_release_tool /bin/ln -s \
    /Applications "${STAGING_DIRECTORY}/Applications" >/dev/null 2>&1 || \
    fail "DMG Applications link를 만들지 못했습니다."
[ -L "${STAGING_DIRECTORY}/Applications" ] || \
    fail "DMG Applications link를 확인하지 못했습니다."

# Hdiutil은 precreated file inode를 교체하므로, 0700 mktemp-owned directory의 nonexisting name에 create한다.
STAGING_DMG_PATH="${RELEASE_WORK_DIRECTORY}/phase12-staging-output.dmg"
[ ! -e "${STAGING_DMG_PATH}" ] && [ ! -L "${STAGING_DMG_PATH}" ] || \
    fail "staging DMG path가 create 전에 이미 존재합니다."

# UDZO는 compressed read-only image이며 source folder 밖의 file을 DMG에 포함하지 않는다.
VOLUME_NAME=${APP_BUNDLE_NAME%.app}
run_release_tool /usr/bin/hdiutil create \
    -volname "${VOLUME_NAME}" \
    -srcfolder "${STAGING_DIRECTORY}" \
    -format UDZO \
    "${STAGING_DMG_PATH}" >/dev/null 2>&1 || \
    fail "read-only compressed DMG를 만들지 못했습니다."
[ -f "${STAGING_DMG_PATH}" ] && [ ! -L "${STAGING_DMG_PATH}" ] || \
    fail "hdiutil staging DMG regular file을 확인하지 못했습니다."
STAGING_DMG_DEVICE_INODE=$(path_device_inode "${STAGING_DMG_PATH}") || \
    fail "hdiutil staging DMG inode를 확인하지 못했습니다."
STAGING_DMG_OWNED=1
verify_owned_staging_dmg

# DMG container는 --deep 없이 exact identity와 secure timestamp로 한 번만 서명한다.
run_release_tool /usr/bin/codesign \
    --force \
    --sign "${SIGNING_IDENTITY}" \
    --timestamp \
    "${STAGING_DMG_PATH}" >/dev/null 2>&1 || \
    fail "DMG Developer ID 서명에 실패했습니다."
verify_owned_staging_dmg

# 이 단계의 DMG는 아직 notarization 전이므로 DMG stapler validate는 후속 R-03에 남긴다.
run_release_tool /usr/bin/codesign \
    --verify --verbose=4 "${STAGING_DMG_PATH}" >/dev/null 2>&1 || \
    fail "DMG 서명을 검증하지 못했습니다."
verify_owned_staging_dmg
run_release_tool /usr/bin/hdiutil verify \
    "${STAGING_DMG_PATH}" >/dev/null 2>&1 || \
    fail "DMG image 무결성을 검증하지 못했습니다."
verify_owned_staging_dmg

# 모든 mutation·verification 후 renameatx_np exclusive rename이 성공한 경우만 final path가 보인다.
publish_owned_staging_dmg || \
    fail "검증된 DMG를 atomic no-clobber publication하지 못했습니다."
STAGING_DMG_OWNED=0

echo "create_phase12_release_dmg: signed release DMG 생성 및 검증 완료"
