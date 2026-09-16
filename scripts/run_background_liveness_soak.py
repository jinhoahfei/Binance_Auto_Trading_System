"""주문 없는 별도 WebView 시험을 실행하고 종료 후 검증 결과를 남긴다."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from validate_background_soak import validate


def main():
    """함수 이름: main()
    기능: 새 출력 경로만 사용해 시험 앱을 실행하며 종료/실패도 상태 파일에 남긴다.
    인자: --output, --seconds, --phase, --cycle-seconds, --freeze-seconds
    반환값: 정상 검증이면 0, 실패/미완료이면 1
    작성 날짜: 2026/09/17
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument("--phase", choices=("visible", "hidden", "minimized", "fullscreen-cover", "other-desktop"), default="hidden")
    parser.add_argument("--cycle-seconds", type=int, default=0)
    parser.add_argument("--freeze-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 20 <= args.seconds <= 604800 or not 0 <= args.freeze_seconds <= 120:
        parser.error("duration out of range")
    output = args.output.resolve()
    if output.exists():
        parser.error("output must not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    status_path = output.with_suffix(".status.json")
    stream_path = output.with_suffix(".runner.log")
    root = Path(__file__).resolve().parents[1]
    binary = root / "UI/apps/desktop/src-tauri/target/debug/background-liveness-soak"
    status = {"status": "starting", "started_at": datetime.now(timezone.utc).isoformat(),
              "target_seconds": args.seconds, "phase": args.phase, "output": str(output), "orders_enabled": False}
    with status_path.open("x") as stream:
        json.dump(status, stream, indent=2)
    try:
        with stream_path.open("x") as stream:
            child = subprocess.Popen([str(binary), "--output", str(output), "--seconds", str(args.seconds),
                "--phase", args.phase, "--cycle-seconds", str(args.cycle_seconds), "--freeze-seconds", str(args.freeze_seconds)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
            status.update(status="running", process_id=child.pid)
            status_path.write_text(json.dumps(status, indent=2) + "\n")
            code = child.wait()
        result = validate(output)
        status.update(status=result["status"], exit_code=code, validation=result,
                      finished_at=datetime.now(timezone.utc).isoformat())
        if code != 0:
            status["status"] = "failed_or_incomplete"
        (output / "validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        # 외부 오류 원문/환경 값은 저장하지 않는다.
        status.update(status="failed_or_incomplete", error_type=type(error).__name__)
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return 0 if status["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
