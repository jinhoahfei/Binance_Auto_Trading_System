"""검증된 앱을 종료 상태에서 설치하고 이전 설치본을 복구 가능하게 보존한다."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def manifest(root):
    """
    함수 이름: manifest()
    기능: 앱의 각 파일 내용을 상대 경로별 SHA-256으로 기록한다.
    인자: root -> 앱 디렉터리
    반환값: 파일 해시 사전
    작성 날짜: 2026/09/27
    """
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def main():
    """
    함수 이름: main()
    기능: 앱 소유권 잠금과 원본 검증 뒤 설치본을 교체하고 이전 버전을 백업한다.
    인자: 없음
    반환값: 없음
    작성 날짜: 2026/09/27
    """
    artifact = Path(__file__).resolve().parent
    built = artifact.parents[1] / "UI/apps/desktop/src-tauri/target/release/bundle/macos/Binance Auto Trader.app"
    installed = Path("/Applications/Binance Auto Trader.app")
    backup = artifact / "previous-installed-app" / installed.name
    lock_path = Path.home() / "Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live/.backend-runtime.lock"
    evidence = json.loads((artifact / "bundle-verification.json").read_bytes())
    expected = manifest(built)
    if not evidence["successful"] or expected["Contents/MacOS/binance-auto-sidecar"] != evidence["sha256"]:
        raise RuntimeError("Bundle verification mismatch")
    if backup.exists() or installed.is_symlink():
        raise RuntimeError("Backup must be unused and installation must be a directory")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(built)], check=True)
    with lock_path.open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if json.load(lock)["owner_state"] != "RELEASED":
            raise RuntimeError("Application must be stopped")
        previous = manifest(installed)
        backup.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".binance-force-sell-", dir=installed.parent) as staging:
            staged = Path(staging) / installed.name
            shutil.copytree(built, staged)
            if manifest(staged) != expected:
                raise RuntimeError("Staged copy mismatch")
            os.rename(installed, backup)
            try:
                os.rename(staged, installed)
                subprocess.run(["codesign", "--verify", "--deep", "--strict", str(installed)], check=True)
                if manifest(installed) != expected:
                    raise RuntimeError("Installed copy mismatch")
            except BaseException:
                if installed.exists():
                    os.rename(installed, Path(staging) / "failed-install.app")
                os.rename(backup, installed)
                raise
    report = {"installed": str(installed), "backup": str(backup), "previous_files": previous,
              "installed_files": expected, "all_files_match_build": True, "codesign_verified": True}
    (artifact / "installation-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
