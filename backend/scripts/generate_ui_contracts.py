"""Python transport schema에서 renderer용 TypeScript 계약 파일을 생성한다."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from binance_auto_trader.transport.contracts import render_typescript_contracts


def _resolve_output_path() -> Path:
    """
    함수 이름: _resolve_output_path()
    기능: script 위치에서 repository의 generated TypeScript 계약 경로를 계산한다.
    인자: 없음
    반환값: generated TypeScript 계약의 절대 Path
    작성 날짜: 2026/08/25
    """
    # Script 위치에서 repository root를 계산해 실행 directory와 무관한 target을 선택한다.
    repository_root = Path(__file__).resolve().parents[2]
    return (
        repository_root
        / "UI"
        / "src"
        / "shared"
        / "contracts"
        / "backendContracts.generated.ts"
    )


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: deterministic 계약을 생성하거나 기존 파일과의 byte drift를 검사한다.
    인자: argument_values -> CLI 인자 sequence 또는 실제 argv를 뜻하는 None
    반환값: 생성·일치 0, drift 또는 누락 1
    작성 날짜: 2026/08/25
    """
    parser = argparse.ArgumentParser(
        description="Generate or check the renderer TypeScript contract.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when the generated contract has drifted",
    )
    arguments = parser.parse_args(argument_values)
    output_path = _resolve_output_path()
    rendered_contracts = render_typescript_contracts()

    # Read-only gate는 현재 source byte와 renderer byte를 비교하고 절대 경로를 출력하지 않는다.
    if arguments.check:
        try:
            current_contracts = output_path.read_text(encoding="utf-8")
        except OSError:
            print("ui-contracts: ERROR: generated contract is unavailable.")
            return 1
        if current_contracts != rendered_contracts:
            print("ui-contracts: ERROR: generated contract drift detected.")
            return 1
        print("ui-contracts: PASS: generated contract is current.")
        return 0

    # Renderer가 이미 마지막 newline까지 소유하므로 byte-for-byte drift 기준을 그대로 쓴다.
    output_path.write_text(
        rendered_contracts,
        encoding="utf-8",
        newline="",
    )  # 생성 파일을 수동 보정하지 않고 Python schema만 source of truth로 유지한다.
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )  # CI와 로컬 명령이 같은 deterministic entry point를 사용한다.
