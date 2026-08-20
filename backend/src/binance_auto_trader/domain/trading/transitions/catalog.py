"""명세의 109개 transition ID를 기계 판독 가능한 목록으로 정의한다."""


def _build_transition_ids(prefix: str, start: int, end: int) -> tuple[str, ...]:
    """
    함수 이름: _build_transition_ids()
    기능: 연속된 번호 범위의 transition ID를 0이 채워진 형식으로 생성한다.
    인자: prefix -> transition ID 접두사
        start -> 시작 번호
        end -> 종료 번호
    반환값: 생성된 transition ID tuple
    작성 날짜: 2026/08/14
    """
    return tuple(f"{prefix}-{number:02d}" for number in range(start, end + 1))


# Region별 ID를 분리해 누락된 transition 범위를 검토하기 쉽게 유지한다.
GLOBAL_TRANSITION_IDS = (
    "G-01",
    "G-02",
    "G-03",
    "G-04",
    "G-05",
    "G-06",
    "G-06P",
    "G-06F",
    "G-06R",
    "G-07",
)
OWNERSHIP_TRANSITION_IDS = _build_transition_ids("O", 1, 9)
CASE_B_POSITION_TRANSITION_IDS = (
    *_build_transition_ids("PB", 1, 22),
    "PB-23F",
    "PB-23",
    "PB-24",
)
CASE_C_POSITION_TRANSITION_IDS = (
    *_build_transition_ids("PC", 1, 23),
    "PC-23F",
    *_build_transition_ids("PC", 24, 28),
)
CASE_B_SIGNAL_TRANSITION_IDS = _build_transition_ids("B", 1, 19)
CASE_C_SIGNAL_TRANSITION_IDS = _build_transition_ids("C", 1, 17)

TRANSITION_IDS = (
    *GLOBAL_TRANSITION_IDS,
    *OWNERSHIP_TRANSITION_IDS,
    *CASE_B_POSITION_TRANSITION_IDS,
    *CASE_C_POSITION_TRANSITION_IDS,
    *CASE_B_SIGNAL_TRANSITION_IDS,
    *CASE_C_SIGNAL_TRANSITION_IDS,
)

# 문서의 transition 수와 중복 여부를 import 시점에 검증해 누락을 즉시 발견한다.
if len(TRANSITION_IDS) != 109 or len(set(TRANSITION_IDS)) != 109:
    raise RuntimeError("TradingSTM transition catalog must contain 109 unique IDs")
