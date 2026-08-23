"""Python transport schema에서 renderer용 TypeScript 계약 파일을 생성한다."""

from pathlib import Path

from binance_auto_trader.transport.contracts import render_typescript_contracts


def main() -> None:
    """
    함수 이름: main()
    기능: deterministic renderer 출력을 workspace의 generated TypeScript 파일에 기록한다.
    인자: 없음
    반환값: 없음
    작성 날짜: 2026/08/23
    """
    # Script 위치에서 repository root를 계산해 실행 directory와 무관한 target을 선택한다.
    repository_root = Path(__file__).resolve().parents[2]
    output_path = (
        repository_root
        / "UI"
        / "src"
        / "shared"
        / "contracts"
        / "backendContracts.generated.ts"
    )
    rendered_contracts = render_typescript_contracts()

    # Renderer가 이미 마지막 newline까지 소유하므로 byte-for-byte drift 기준을 그대로 쓴다.
    output_path.write_text(
        rendered_contracts,
        encoding="utf-8",
        newline="",
    )  # 생성 파일을 수동 보정하지 않고 Python schema만 source of truth로 유지한다.


if __name__ == "__main__":
    main()  # CI와 로컬 명령이 같은 deterministic entry point를 사용한다.
