"""Windows x64에서 credential 없는 production sidecar 실행 파일을 패키징한다."""

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET_TRIPLE = "x86_64-pc-windows-msvc"
BINARY_NAME = "binance-auto-sidecar"
PYINSTALLER_VERSION = "6.22.2"


def packaging_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """
    함수 이름: packaging_environment()
    기능: 빌드에 필요한 OS/toolchain 값만 전달하고 credential과 Python 주입 변수를 제거한다.
    인자: environment -> 부모 process 환경
    반환값: 허용된 환경 사본
    작성 날짜: 2026/09/27
    """
    allowed_names = {
        "systemroot", "windir", "temp", "tmp", "localappdata", "appdata",
        "userprofile", "path", "pathext", "comspec", "cargo_home", "rustup_home",
    }
    return {
        **{name: value for name, value in environment.items() if name.lower() in allowed_names},
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
    }


def pyinstaller_arguments(repository: Path, work: Path) -> list[str]:
    """
    함수 이름: pyinstaller_arguments()
    기능: stdio IPC와 Windows 시간대 자료를 포함하는 고정 빌드 인자를 만든다.
    인자: repository -> checkout 경로, work -> 임시 빌드 경로
    반환값: Python interpreter 뒤에 전달할 argument 목록
    작성 날짜: 2026/09/27
    """
    return [
        "-I", "-m", "PyInstaller", "--clean", "--noconfirm", "--onefile",
        # --windowed는 sys.stdin/stdout을 없애므로 pipe IPC용 console binary를 유지한다.
        "--console", "--noupx", "--log-level", "WARN",
        "--hidden-import", "websocket", "--hidden-import", "_ssl",
        "--hidden-import", "_hashlib", "--collect-data", "tzdata",
        "--name", BINARY_NAME, "--paths", str(repository / "backend/src"),
        "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
        "--specpath", str(work / "spec"),
        str(repository / "backend/src/binance_auto_trader/sidecar.py"),
    ]


def package_sidecar(repository: Path = REPOSITORY_ROOT) -> Path:
    """
    함수 이름: package_sidecar()
    기능: native Windows toolchain을 확인하고 완성된 exe만 Tauri binaries에 원자 게시한다.
    인자: repository -> source checkout root
    반환값: target suffix를 포함한 executable 경로
    작성 날짜: 2026/09/27
    """
    if sys.platform != "win32" or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Native Windows x64 Python is required; cross-compilation is not supported.")
    python = repository / "backend/.venv/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("Run uv sync --locked --extra desktop in backend first.")
    environment = packaging_environment(os.environ)
    host = subprocess.run(
        ["rustc", "--print", "host-tuple"], env=environment, check=True,
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    if host != TARGET_TRIPLE:
        raise RuntimeError("The x86_64-pc-windows-msvc Rust host toolchain is required.")
    subprocess.run(
        [str(python), "-I", "-c",
         "import PyInstaller, struct, sys, websocket, _ssl, _hashlib; "
         "from zoneinfo import ZoneInfo; "
         f"assert PyInstaller.__version__ == '{PYINSTALLER_VERSION}'; "
         "assert sys.platform == 'win32' and struct.calcsize('P') == 8; "
         "ZoneInfo('Asia/Seoul'); import binance_auto_trader.sidecar"],
        env=environment, check=True, capture_output=True, timeout=30,
    )
    output = repository / "UI/apps/desktop/src-tauri/binaries"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{BINARY_NAME}-{TARGET_TRIPLE}.exe"
    # 동일 filesystem의 임시 tree에서 완성한 뒤 replace하므로 실패한 build가 기존 exe를 훼손하지 않는다.
    with tempfile.TemporaryDirectory(prefix=".windows-package-", dir=output) as temporary:
        work = Path(temporary)
        environment["PYINSTALLER_CONFIG_DIR"] = str(work / "cache")
        subprocess.run(
            [str(python), *pyinstaller_arguments(repository, work)],
            cwd=work, env=environment, check=True,
        )
        packaged = work / "dist" / f"{BINARY_NAME}.exe"
        with packaged.open("rb") as executable:
            if executable.read(2) != b"MZ":
                raise RuntimeError("PyInstaller did not produce a Windows executable.")
        staged = work / "publish.exe"
        shutil.copyfile(packaged, staged)
        with staged.open("rb+") as executable:
            os.fsync(executable.fileno())
        os.replace(staged, destination)
    return destination


def main(arguments: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: 고정 Windows packaging 경로만 허용하고 비밀 없는 실패 안내를 출력한다.
    인자: arguments -> 선택적 exact native target 하나
    반환값: 성공 0, 실패 1
    작성 날짜: 2026/09/27
    """
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments not in ([], [TARGET_TRIPLE]):
        print("package_sidecar_windows: Only the native Windows x64 MSVC target is supported.", file=sys.stderr)
        return 1
    try:
        package_sidecar()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("package_sidecar_windows: Build failed; verify Windows x64, MSVC Rust and uv sync --locked --extra desktop.", file=sys.stderr)
        return 1
    print(f"package_sidecar_windows: {BINARY_NAME}-{TARGET_TRIPLE}.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
